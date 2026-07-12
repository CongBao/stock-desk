from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_desk.market.navigation import (
    MarketNavigationConflict,
    MarketNavigationInstrument,
    MarketNavigationService,
    MarketNavigationState,
    MarketNavigationStateStore,
)
from stock_desk.market.instruments import InstrumentNotFound
from stock_desk.market.types import InstrumentKind


def _item(
    symbol: str = "600000.SH", name: str = "浦发银行"
) -> MarketNavigationInstrument:
    return MarketNavigationInstrument(
        symbol=symbol,
        name=name,
        instrument_kind=InstrumentKind.STOCK,
    )


class _Catalog:
    def __init__(self, *items: MarketNavigationInstrument) -> None:
        self._items = {item.symbol: item for item in items}

    def current_catalog(self) -> object:
        return type(
            "Catalog",
            (),
            {
                "instruments": tuple(
                    type(
                        "Instrument",
                        (),
                        {
                            "symbol": item.symbol,
                            "name": item.name,
                            "instrument_kind": item.instrument_kind,
                        },
                    )()
                    for item in self._items.values()
                )
            },
        )()


class _UnavailableCatalog:
    def current_catalog(self) -> object:
        raise InstrumentNotFound()


def test_navigation_models_reject_unknown_fields_limits_and_duplicate_symbols() -> None:
    with pytest.raises(ValidationError, match="extra"):
        MarketNavigationInstrument.model_validate(
            {
                "symbol": "600000.SH",
                "name": "浦发银行",
                "instrument_kind": "stock",
                "url": "https://example.invalid/secret?token=x",
            }
        )

    with pytest.raises(ValidationError, match="watchlist"):
        MarketNavigationState(
            revision=0,
            watchlist=tuple(
                _item(f"{index:06d}.SH", str(index)) for index in range(101)
            ),
            recent=(),
        )

    with pytest.raises(ValidationError, match="unique"):
        MarketNavigationState(
            revision=0,
            watchlist=(_item(), _item()),
            recent=(),
        )


def test_store_round_trips_order_atomically_below_data_root(tmp_path: Path) -> None:
    path = tmp_path / "market" / "navigation-v1.json"
    store = MarketNavigationStateStore(path)
    second = _item("600036.SH", "招商银行")

    saved = store.save(
        MarketNavigationState(revision=3, watchlist=(_item(), second), recent=(second,))
    )
    loaded = store.load()

    assert saved.revision == 3
    assert loaded.state == saved
    assert loaded.notice is None
    assert [item.symbol for item in loaded.state.watchlist] == [
        "600000.SH",
        "600036.SH",
    ]
    assert path.is_file()
    assert list(path.parent.glob(".*.tmp")) == []
    assert "token" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("not-json", "corrupt"),
        (json.dumps({"schema_version": 2}), "unsupported_schema"),
    ],
)
def test_store_fails_safe_for_corrupt_or_unsupported_state(
    tmp_path: Path,
    payload: str,
    reason: str,
) -> None:
    path = tmp_path / "navigation-v1.json"
    path.write_text(payload, encoding="utf-8")

    loaded = MarketNavigationStateStore(path).load()

    assert loaded.state == MarketNavigationState(revision=0, watchlist=(), recent=())
    assert loaded.notice is not None
    assert loaded.notice.model_dump(mode="json") == {
        "code": "market_navigation_state_reset",
        "reason": reason,
    }


def test_service_uses_full_cas_preserves_order_and_validates_catalog_identity(
    tmp_path: Path,
) -> None:
    first = _item()
    second = _item("600036.SH", "招商银行")
    service = MarketNavigationService(
        store=MarketNavigationStateStore(tmp_path / "navigation.json"),
        instruments=_Catalog(first, second),
    )

    updated = service.replace(
        expected_revision=0,
        watchlist=(second, first),
        recent=(first,),
    )

    assert updated.revision == 1
    assert [item.symbol for item in updated.watchlist] == [
        "600036.SH",
        "600000.SH",
    ]
    assert service.state().revision == 1
    with pytest.raises(MarketNavigationConflict) as conflict:
        service.replace(expected_revision=0, watchlist=(), recent=())
    assert conflict.value.code == "market_navigation_revision_conflict"

    forged = first.model_copy(update={"name": "伪造名称"})
    with pytest.raises(MarketNavigationConflict) as invalid:
        service.replace(expected_revision=1, watchlist=(forged,), recent=())
    assert invalid.value.code == "invalid_market_navigation_instrument"
    assert service.state().revision == 1


def test_index_and_similarly_numbered_stock_are_distinct_identities(
    tmp_path: Path,
) -> None:
    index = MarketNavigationInstrument(
        symbol="000001.SS",
        name="上证指数",
        instrument_kind=InstrumentKind.INDEX,
    )
    stock = MarketNavigationInstrument(
        symbol="000001.SZ",
        name="平安银行",
        instrument_kind=InstrumentKind.STOCK,
    )
    service = MarketNavigationService(
        store=MarketNavigationStateStore(tmp_path / "navigation.json"),
        instruments=_Catalog(index, stock),
    )

    state = service.replace(
        expected_revision=0,
        watchlist=(index, stock),
        recent=(stock, index),
    )

    assert [item.symbol for item in state.watchlist] == ["000001.SS", "000001.SZ"]
    assert [item.instrument_kind for item in state.watchlist] == [
        InstrumentKind.INDEX,
        InstrumentKind.STOCK,
    ]


def test_default_index_allowlist_survives_temporarily_unavailable_catalog(
    tmp_path: Path,
) -> None:
    index = MarketNavigationInstrument(
        symbol="000001.SS",
        name="上证指数",
        instrument_kind=InstrumentKind.INDEX,
    )
    service = MarketNavigationService(
        store=MarketNavigationStateStore(tmp_path / "navigation.json"),
        instruments=_UnavailableCatalog(),
    )

    saved = service.replace(expected_revision=0, watchlist=(index,), recent=())

    assert saved.watchlist == (index,)
    with pytest.raises(MarketNavigationConflict) as unavailable:
        service.replace(expected_revision=1, watchlist=(_item(),), recent=())
    assert unavailable.value.code == "market_navigation_catalog_unavailable"


def test_duplicate_update_is_invalid_without_mutating_revision(tmp_path: Path) -> None:
    item = _item()
    service = MarketNavigationService(
        store=MarketNavigationStateStore(tmp_path / "navigation.json"),
        instruments=_Catalog(item),
    )

    with pytest.raises(MarketNavigationConflict) as invalid:
        service.replace(
            expected_revision=0,
            watchlist=(item, item),
            recent=(),
        )

    assert invalid.value.code == "invalid_request"
    assert service.state().revision == 0
