from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_desk.market.types import Exchange, Instrument, InstrumentKind, ListingStatus
from stock_desk.workspace.models import (
    WorkspaceInstrument,
    WorkspacePreferences,
    WorkspacePut,
    WorkspaceState,
)
from stock_desk.workspace.service import WorkspaceConflict, WorkspaceService
from stock_desk.workspace.store import WorkspaceStateStore


NOW = datetime(2026, 7, 12, 6, tzinfo=timezone.utc)


class _Instruments:
    def __init__(self, items: tuple[Instrument, ...]) -> None:
        self._items = {item.symbol: item for item in items}

    def get(self, symbol: str):
        try:
            instrument = self._items[symbol]
        except KeyError as error:
            raise LookupError from error
        return type("Result", (), {"instrument": instrument})()


class _Market:
    def __init__(self, items: tuple[Instrument, ...]) -> None:
        self.instruments = _Instruments(items)


class _Formulas:
    def __init__(self, placement: str | None) -> None:
        self.placement = placement

    def get_version(self, _version_id: str):
        if self.placement is None:
            raise LookupError
        return type("Formula", (), {"placement": self.placement})()


PUDONG = Instrument(
    symbol="600000.SH",
    exchange=Exchange.SH,
    name="浦发银行",
    instrument_kind=InstrumentKind.STOCK,
    listing_status=ListingStatus.LISTED,
)


def _service(tmp_path: Path, *, now: datetime = NOW) -> WorkspaceService:
    return WorkspaceService(
        store=WorkspaceStateStore(tmp_path / "state-v1.json"),
        market=_Market((PUDONG,)),
        clock=lambda: now,
    )


def test_missing_workspace_recovers_to_non_blocking_market_default(
    tmp_path: Path,
) -> None:
    restored = _service(tmp_path).restore()

    assert restored.restored is False
    assert restored.notice == "workspace_missing"
    assert restored.workspace == WorkspacePreferences.safe_default()


@pytest.mark.parametrize(
    ("mutation", "notice"),
    [
        ({"updated_at": NOW - timedelta(days=181)}, "workspace_expired"),
        (
            {
                "preferences": WorkspacePreferences(
                    current_page="/market",
                    instrument=WorkspaceInstrument(
                        symbol="600000.SH",
                        name="伪造名称",
                        exchange=Exchange.SH,
                        kind=InstrumentKind.STOCK,
                    ),
                )
            },
            "workspace_instrument_unavailable",
        ),
    ],
)
def test_expired_or_catalog_mismatched_workspace_safely_falls_back(
    tmp_path: Path, mutation: dict[str, object], notice: str
) -> None:
    service = _service(tmp_path)
    state = WorkspaceState(
        revision=1,
        updated_at=NOW,
        preferences=WorkspacePreferences.safe_default(),
    ).model_copy(update=mutation)
    service.store.save(state)

    restored = service.restore()

    assert restored.restored is False
    assert restored.notice == notice
    assert restored.workspace == WorkspacePreferences.safe_default()


def test_valid_workspace_round_trips_and_revision_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    body = WorkspacePut(
        expected_revision=0,
        current_page="/formulas",
        instrument=WorkspaceInstrument(
            symbol="600000.SH",
            name="浦发银行",
            exchange=Exchange.SH,
            kind=InstrumentKind.STOCK,
        ),
        period="1w",
        adjustment="hfq",
        zoom={"start": 25.0, "end": 75.0},
        main_chart="candlestick",
        subchart={"kind": "volume"},
    )
    saved = service.update(body)

    assert saved.revision == 1
    assert saved.restored is True
    assert saved.notice is None
    assert _service(tmp_path).restore() == saved
    with pytest.raises(WorkspaceConflict, match="workspace_revision_conflict"):
        service.update(body)


def test_delete_is_idempotent_and_recovery_does_not_persist_a_default(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.delete()
    service.delete()

    assert service.restore().notice == "workspace_missing"
    assert not service.store.path.exists()


def test_corrupt_and_illegal_route_states_return_specific_safe_notices(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store.path.write_text("not-json", encoding="utf-8")
    assert service.restore().notice == "workspace_corrupt"

    service.store.path.write_text(
        '{"schema_version":1,"preferences":{"current_page":"/market?token=x"}}',
        encoding="utf-8",
    )
    assert service.restore().notice == "workspace_route_invalid"


def test_missing_formula_subchart_reference_falls_back_without_echoing_it(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    state = WorkspaceState(
        revision=1,
        updated_at=NOW,
        preferences=WorkspacePreferences(
            instrument=WorkspaceInstrument.default(),
            subchart={
                "kind": "formula",
                "formula_version_id": "00000000-0000-4000-8000-000000000001",
            },
        ),
    )
    service.store.save(state)

    restored = service.restore()

    assert restored.notice == "workspace_chart_unavailable"
    assert restored.workspace.subchart.kind == "volume"


def test_onboarding_initialization_replaces_an_expired_workspace(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store.save(
        WorkspaceState(
            revision=4,
            updated_at=NOW - timedelta(days=181),
            preferences=WorkspacePreferences.safe_default(),
        )
    )

    initialized = service.initialize(WorkspaceInstrument.default())

    assert initialized.restored is True
    assert initialized.notice is None
    assert initialized.revision == 5


def test_future_workspace_is_rejected_and_valid_initialization_is_idempotent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store.save(
        WorkspaceState(
            revision=2,
            updated_at=NOW + timedelta(minutes=6),
            preferences=WorkspacePreferences.safe_default(),
        )
    )
    assert service.restore().notice == "workspace_corrupt"

    service.store.save(
        WorkspaceState(
            revision=3,
            updated_at=NOW,
            preferences=WorkspacePreferences.safe_default(),
        )
    )
    initialized = service.initialize(WorkspaceInstrument.default())
    assert initialized.restored is True
    assert initialized.revision == 3


def test_workspace_update_and_initialization_reject_missing_instruments(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    missing = WorkspaceInstrument(
        symbol="999999.SH",
        name="不存在标的",
        exchange=Exchange.SH,
        kind=InstrumentKind.STOCK,
    )
    request = WorkspacePut(
        expected_revision=0,
        current_page="/market",
        instrument=missing,
        period="1d",
        adjustment="none",
        zoom={"start": 0.0, "end": 100.0},
        main_chart="candlestick",
        subchart={"kind": "volume"},
    )

    with pytest.raises(WorkspaceConflict, match="workspace_instrument_unavailable"):
        service.update(request)

    initialized = service.initialize(missing)
    assert initialized.restored is True
    assert initialized.workspace.instrument == WorkspaceInstrument.default()


def test_corrupt_storage_is_replaced_by_update_and_initialization(
    tmp_path: Path,
) -> None:
    update_service = _service(tmp_path / "update")
    update_service.store.path.parent.mkdir(parents=True)
    update_service.store.path.write_text("not-json", encoding="utf-8")
    saved = update_service.update(
        WorkspacePut(
            expected_revision=0,
            current_page="/market",
            instrument=WorkspaceInstrument.default(),
            period="1d",
            adjustment="none",
            zoom={"start": 0.0, "end": 100.0},
            main_chart="candlestick",
            subchart={"kind": "volume"},
        )
    )
    assert saved.revision == 1

    initialize_service = _service(tmp_path / "initialize")
    initialize_service.store.path.parent.mkdir(parents=True)
    initialize_service.store.path.write_text("not-json", encoding="utf-8")
    initialized = initialize_service.initialize(WorkspaceInstrument.default())
    assert initialized.revision == 1


def test_formula_subchart_requires_a_resolved_subchart_formula(tmp_path: Path) -> None:
    formula_version_id = "00000000-0000-4000-8000-000000000001"
    state = WorkspaceState(
        revision=1,
        updated_at=NOW,
        preferences=WorkspacePreferences(
            instrument=WorkspaceInstrument.default(),
            subchart={
                "kind": "formula",
                "formula_version_id": formula_version_id,
            },
        ),
    )
    store = WorkspaceStateStore(tmp_path / "state-v1.json")
    store.save(state)

    available = WorkspaceService(
        store=store,
        market=_Market((PUDONG,)),
        formula_repository=_Formulas("subchart"),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    assert available.restore().restored is True

    wrong_placement = WorkspaceService(
        store=store,
        market=_Market((PUDONG,)),
        formula_repository=_Formulas("main"),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    assert wrong_placement.restore().notice == "workspace_chart_unavailable"
    with pytest.raises(WorkspaceConflict, match="workspace_chart_unavailable"):
        wrong_placement.update(
            WorkspacePut(
                expected_revision=1,
                current_page="/market",
                instrument=WorkspaceInstrument.default(),
                period="1d",
                adjustment="none",
                zoom={"start": 0.0, "end": 100.0},
                main_chart="candlestick",
                subchart={
                    "kind": "formula",
                    "formula_version_id": formula_version_id,
                },
            )
        )

    missing_formula = WorkspaceService(
        store=store,
        market=_Market((PUDONG,)),
        formula_repository=_Formulas(None),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    assert missing_formula.restore().notice == "workspace_chart_unavailable"
