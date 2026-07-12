from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from fastapi.testclient import TestClient

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.types import Exchange, Instrument, InstrumentKind, ListingStatus
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


@contextmanager
def _api(tmp_path: Path) -> Iterator[TestClient]:
    database_url = f"sqlite:///{tmp_path / 'navigation-api.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    services = MarketServices(engine=engine, lake_root=(tmp_path / "lake").resolve())
    services.instruments.ingest(
        routed_instruments(
            (
                Instrument(
                    symbol="000001.SS",
                    exchange=Exchange.SH,
                    name="上证指数",
                    instrument_kind=InstrumentKind.INDEX,
                    listing_status=ListingStatus.LISTED,
                    listed_on=date(1991, 7, 15),
                ),
                instrument("000001.SZ", "平安银行"),
                instrument("600000.SH", "浦发银行"),
            )
        )
    )
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            yield client
    finally:
        services.close()


def _item(symbol: str, name: str, kind: str = "stock") -> dict[str, str]:
    return {"symbol": symbol, "name": name, "instrument_kind": kind}


def test_navigation_get_put_contract_and_conflict(tmp_path: Path) -> None:
    with _api(tmp_path) as client:
        initial = client.get("/api/v1/market/navigation")
        updated = client.put(
            "/api/v1/market/navigation",
            json={
                "expected_revision": 0,
                "watchlist": [
                    _item("000001.SS", "上证指数", "index"),
                    _item("000001.SZ", "平安银行"),
                ],
                "recent": [_item("600000.SH", "浦发银行")],
            },
        )
        conflict = client.put(
            "/api/v1/market/navigation",
            json={"expected_revision": 0, "watchlist": [], "recent": []},
        )

    assert initial.status_code == 200
    assert initial.json() == {
        "schema_version": 1,
        "revision": 0,
        "watchlist": [],
        "recent": [],
        "notice": None,
    }
    assert updated.status_code == 200
    assert updated.json()["revision"] == 1
    assert [item["symbol"] for item in updated.json()["watchlist"]] == [
        "000001.SS",
        "000001.SZ",
    ]
    assert conflict.status_code == 409
    assert conflict.json() == {"code": "market_navigation_revision_conflict"}


def test_navigation_rejects_unknown_sensitive_fields_and_forged_identity(
    tmp_path: Path,
) -> None:
    with _api(tmp_path) as client:
        unknown = client.put(
            "/api/v1/market/navigation",
            json={
                "expected_revision": 0,
                "watchlist": [],
                "recent": [],
                "token": "must-not-persist",
            },
        )
        forged = client.put(
            "/api/v1/market/navigation",
            json={
                "expected_revision": 0,
                "watchlist": [_item("000001.SS", "平安银行", "stock")],
                "recent": [],
            },
        )

    assert unknown.status_code == 422
    assert unknown.json() == {"code": "invalid_request"}
    assert forged.status_code == 422
    assert forged.json() == {"code": "invalid_market_navigation_instrument"}


def test_navigation_corrupt_file_returns_safe_default_and_notice(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market" / "navigation-v1.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":999}', encoding="utf-8")

    with _api(tmp_path) as client:
        response = client.get("/api/v1/market/navigation")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "revision": 0,
        "watchlist": [],
        "recent": [],
        "notice": {
            "code": "market_navigation_state_reset",
            "reason": "unsupported_schema",
        },
    }


def test_navigation_duplicate_symbol_returns_stable_validation_error(
    tmp_path: Path,
) -> None:
    duplicate = _item("600000.SH", "浦发银行")
    with _api(tmp_path) as client:
        response = client.put(
            "/api/v1/market/navigation",
            json={
                "expected_revision": 0,
                "watchlist": [duplicate, duplicate],
                "recent": [],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}
