from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.types import Exchange, Instrument, InstrumentKind, ListingStatus


def test_index_identity_is_distinct_and_default_symbol_is_sse_composite(
    tmp_path: Path,
) -> None:
    index = Instrument(
        symbol="000001.SS",
        exchange=Exchange.SH,
        name="上证指数",
        instrument_kind=InstrumentKind.INDEX,
        listing_status=ListingStatus.LISTED,
    )
    equity = Instrument(
        symbol="000001.SZ",
        exchange=Exchange.SZ,
        name="平安银行",
        instrument_kind=InstrumentKind.STOCK,
        listing_status=ListingStatus.LISTED,
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'stock-desk.db'}",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/onboarding/state")

    assert index != equity
    assert index.symbol == "000001.SS"
    assert equity.symbol == "000001.SZ"
    assert response.status_code == 200
    state = response.json()
    assert state["schema_version"] == 1
    assert state["current_step"] == "welcome"
    assert state["instrument"] == {
        "symbol": "000001.SS",
        "name": "上证指数",
        "exchange": "SH",
        "instrument_kind": "index",
    }
    datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
