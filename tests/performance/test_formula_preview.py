from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from stock_desk.api.market import MarketServices
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import MACD_TEMPLATE_SOURCE, FormulaService
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


def test_macd_ten_year_cached_data_preview_under_three_seconds(
    tmp_path: Path,
    benchmark: Any,
) -> None:
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

    # Each measured round creates a fresh formula service, so its formula-result
    # LRU is cold. The market snapshot remains locally cached and the measured
    # path still includes DuckDB read, AST compilation, process spawn and compute.
    def preview_from_cached_market_data() -> Any:
        service = FormulaService(repository=repository, lake=services.lake)
        return service.preview(version.id, routed.result.query, {})

    try:
        result = benchmark.pedantic(
            preview_from_cached_market_data,
            rounds=3,
            iterations=1,
            warmup_rounds=1,
        )
    finally:
        services.close()

    assert len(result.timestamps) >= 2_400
    assert [output.name for output in result.numeric_outputs] == [
        "DIF",
        "DEA",
        "MACD",
    ]
    assert benchmark.stats.stats.mean < 3.0
