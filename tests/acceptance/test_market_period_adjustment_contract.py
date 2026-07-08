from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from stock_desk.formula.repository import FormulaRepository
from stock_desk.market.types import Adjustment, Period
from tests.backtest_test_helpers import (
    intraday_timestamps,
    routed_bars_from_closes,
    weekly_timestamps,
    weekday_range,
)
from tests.unit.api.test_market_api import market_api


def test_period_and_adjustment_switches_recalculate_visible_market_and_indicator_values(
    tmp_path: Path,
) -> None:
    timelines = {
        Period.DAY: weekday_range(date(2024, 7, 1), date(2024, 7, 5)),
        Period.WEEK: weekly_timestamps(date(2024, 6, 3), 4),
        Period.MIN60: intraday_timestamps(date(2024, 7, 1), trading_days=1),
    }
    expected: dict[tuple[str, str], tuple[str, ...]] = {}

    with market_api(tmp_path) as context:
        formula = FormulaRepository(context.services.engine).create(
            "行情切换契约",
            "indicator",
            "VISIBLE:C;",
            {},
            placement="subchart",
        )
        for period_index, period in enumerate(Period):
            for adjustment_index, adjustment in enumerate(Adjustment):
                base = Decimal(100 * period_index + 10 * adjustment_index + 1)
                closes = tuple(base + index for index in range(4))
                routed = routed_bars_from_closes(
                    "600000.SH",
                    period,
                    timelines[period],
                    closes,
                    adjustment=adjustment,
                )
                context.services.lake.write(routed)
                expected[(period.value, adjustment.value)] = tuple(
                    str(value) for value in closes
                )

        responses = {
            key: context.client.get(
                "/api/market/bars",
                params={
                    "symbol": "600000.SH",
                    "period": key[0],
                    "adjustment": key[1],
                    "formula_version_id": formula.id,
                },
            )
            for key in expected
        }

    signal_ids: set[str] = set()
    for key, response in responses.items():
        assert response.status_code == 200
        body = response.json()
        visible_closes = tuple(bar["close"] for bar in body["bars"])
        indicator = body["formula"]["numeric_outputs"]
        assert {bar["period"] for bar in body["bars"]} == {key[0]}
        assert {bar["adjustment"] for bar in body["bars"]} == {key[1]}
        assert body["provenance"]["adjustment"] == key[1]
        assert visible_closes == expected[key]
        assert len(indicator) == 1
        assert indicator[0]["name"] == "VISIBLE"
        assert indicator[0]["warmup_null_count"] == 0
        assert tuple(Decimal(str(value)) for value in indicator[0]["values"]) == tuple(
            Decimal(value) for value in expected[key]
        )
        signal_ids.add(body["formula"]["signal_series_id"])

    assert len(signal_ids) == len(expected)
    latest_daily = responses[("1d", "qfq")].json()
    assert latest_daily["bars"][-1]["timestamp"] == "2024-07-03T16:00:00Z"
    assert latest_daily["provenance"]["data_cutoff"] == "2024-07-03T17:00:00Z"
