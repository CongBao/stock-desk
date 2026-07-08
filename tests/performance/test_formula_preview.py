from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from stock_desk.api.market import MarketServices
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import MACD_TEMPLATE_SOURCE, FormulaService
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


def test_legacy_macd_preview_preserves_output_correctness(tmp_path: Path) -> None:
    """Correctness regression only; the aggregate browser gate owns timing."""
    database_url = f"sqlite:///{tmp_path / 'formula-performance.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    start = date(2016, 1, 1)
    end = date(2026, 1, 1)
    days = tuple(
        start + timedelta(days=index)
        for index in range((end - start).days)
        if (start + timedelta(days=index)).weekday() < 5
    )
    routed = routed_daily_bars(days)
    services.lake.write(routed)
    repository = FormulaRepository(services.engine)
    version = repository.create(
        "MACD ten-year benchmark",
        "trading",
        MACD_TEMPLATE_SOURCE,
        {},
        placement="subchart",
    )

    def preview_from_cached_market_data() -> Any:
        service = FormulaService(repository=repository, lake=services.lake)
        return service.preview(version.id, routed.result.query, {})

    try:
        result = preview_from_cached_market_data()
    finally:
        services.close()

    assert len(result.timestamps) >= 2_400
    assert [output.name for output in result.numeric_outputs] == [
        "DIF",
        "DEA",
        "MACD",
    ]
