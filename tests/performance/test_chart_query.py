from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.types import Adjustment, Period
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


def test_cached_ten_year_daily_query_under_one_second(
    tmp_path: Path,
    benchmark: Any,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'performance.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    services = MarketServices(engine=engine, lake_root=(tmp_path / "market").resolve())
    try:
        start = date(2016, 1, 1)
        end = date(2026, 1, 1)
        days = tuple(
            start + timedelta(days=index)
            for index in range((end - start).days)
            if (start + timedelta(days=index)).weekday() < 5
        )
        stored = services.lake.write(routed_daily_bars(days))

        settings = Settings(database_url=database_url, data_dir=tmp_path)
        with TestClient(create_app(settings, market_services=services)) as client:

            def query_cached_chart() -> dict[str, Any]:
                response = client.get(
                    "/api/market/bars",
                    params={
                        "symbol": "600000.SH",
                        "period": Period.DAY.value,
                        "adjustment": Adjustment.QFQ.value,
                    },
                )
                assert response.status_code == 200
                body = response.json()
                assert isinstance(body, dict)
                return body

            warmed = query_cached_chart()
            assert len(warmed["bars"]) == len(days)
            assert warmed["route_version"] == stored.route_version

            result = benchmark.pedantic(
                query_cached_chart,
                rounds=5,
                iterations=1,
            )

            assert len(result["bars"]) >= 2_400
            assert result["dataset_version"] == stored.dataset_version
            assert result["route_version"] == stored.route_version
            assert benchmark.stats.stats.mean < 1.0
    finally:
        services.close()
