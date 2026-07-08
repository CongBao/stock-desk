from __future__ import annotations

from datetime import date
from pathlib import Path

from stock_desk.formula.service import MACD_TEMPLATE_SOURCE, FormulaService
from stock_desk.market.types import Period
from tests.backtest_test_helpers import (
    BacktestHarness,
    local_time,
    weekday_range,
)


def test_legacy_single_backtest_preserves_report_correctness(tmp_path: Path) -> None:
    """Correctness regression only; the aggregate worker/browser gate owns timing."""
    with BacktestHarness.create(tmp_path) as harness:
        warmup_start = date(2015, 12, 1)
        scoring_start = date(2016, 1, 1)
        scoring_end = date(2026, 1, 1)
        days = weekday_range(warmup_start, scoring_end)
        harness.seed_instruments("600000.SH")
        harness.seed_symbol("600000.SH", Period.DAY, days, wave_period=40)
        version = harness.create_formula("MACD 十年基准", MACD_TEMPLATE_SOURCE)
        formula_services: list[FormulaService] = []

        def cold_run() -> dict[str, object]:
            completed = harness.run_single(
                version.id,
                symbol="600000.SH",
                period=Period.DAY,
                scoring_start=local_time(scoring_start),
                scoring_end=local_time(scoring_end),
            )
            formula_services.append(completed.formulas)
            return {
                "input_bar_count": len(days),
                "realized_count": completed.report.metrics["realized_count"],
                "result_hash": completed.run.result_hash,
                "signal_series_id": completed.run.symbols[0].signal_series_id,
                "status": completed.run.status,
            }

        result = cold_run()

        assert result["input_bar_count"] >= 2_400
        assert result["realized_count"] > 0
        assert result["result_hash"] is not None
        assert result["signal_series_id"]
        assert result["status"] == "succeeded"
        assert len(formula_services) == 1
