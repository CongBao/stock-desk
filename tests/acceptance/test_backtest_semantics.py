from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from stock_desk.backtest.models import BacktestOrderEventRow
from stock_desk.backtest.types import PinnedMarketRef
from stock_desk.formula.service import MACD_TEMPLATE_SOURCE, FormulaService
from stock_desk.market.types import Period
from tests.backtest_test_helpers import (
    OPEN_ONLY_FORMULA,
    WAVE_FORMULA,
    BacktestHarness,
    intraday_timestamps,
    local_time,
    routed_bars_from_closes,
    routed_status,
    weekday_range,
    weekly_timestamps,
)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_runner_persists_complete_deferred_constraint_and_cancellation_chains(
    tmp_path: Path,
) -> None:
    timestamps = intraday_timestamps(date(2024, 1, 2), trading_days=5)
    day_one, day_two, day_three, day_four, day_five = tuple(
        dict.fromkeys(timestamp.date() for timestamp in timestamps)
    )
    closes = [Decimal("10")] * len(timestamps)
    closes[0] = Decimal("11")
    closes[1] = Decimal("9")
    closes[7] = Decimal("11")
    closes[13] = Decimal("9")

    with BacktestHarness.create(tmp_path) as harness:
        harness.seed_instruments("600000.SH")
        bars = routed_bars_from_closes(
            "600000.SH",
            Period.MIN60,
            timestamps,
            tuple(closes),
        )
        harness.market.write(bars)
        harness.statuses.write(
            routed_status(
                "600000.SH",
                Period.MIN60,
                bars,
                suspended_days=frozenset({day_three}),
                raw_open_overrides={
                    timestamps[1]: Decimal("12"),
                    timestamps[12]: Decimal("12"),
                    timestamps[16]: Decimal("8"),
                },
            )
        )
        version = harness.create_formula(
            "约束事件链",
            "BUY:C=11;SELL:C=9;",
        )

        completed = harness.run_single(
            version.id,
            symbol="600000.SH",
            period=Period.MIN60,
            scoring_start=timestamps[0],
            scoring_end=timestamps[-1] + timedelta(hours=1),
        )
        with harness.engine.connect() as connection:
            events = tuple(
                connection.execute(
                    select(
                        BacktestOrderEventRow.event_type,
                        BacktestOrderEventRow.payload_json,
                    )
                    .where(
                        BacktestOrderEventRow.run_id == completed.run.id,
                        BacktestOrderEventRow.symbol == "600000.SH",
                    )
                    .order_by(BacktestOrderEventRow.ordinal)
                ).mappings()
            )

        assert completed.run.status == "succeeded"
        assert completed.report.metrics["realized_count"] == 1
        assert [
            (
                event["event_type"],
                event["payload_json"].get("side"),
                event["payload_json"].get("reason"),
            )
            for event in events
        ] == [
            ("OrderPending", "buy", None),
            ("OrderBlocked", "buy", "limit_up"),
            ("OrderCancelled", "buy", "opposite_signal"),
            ("OrderPending", "buy", None),
            ("OrderBlocked", "buy", "suspended"),
            ("OrderBlocked", "buy", "suspended"),
            ("OrderBlocked", "buy", "suspended"),
            ("OrderBlocked", "buy", "suspended"),
            ("OrderBlocked", "buy", "limit_up"),
            ("OrderFilled", "buy", None),
            ("OrderPending", "sell", None),
            ("OrderBlocked", "sell", "t_plus_one"),
            ("OrderBlocked", "sell", "t_plus_one"),
            ("OrderBlocked", "sell", "limit_down"),
            ("OrderFilled", "sell", None),
        ]
        assert events[2]["payload_json"]["at"] == _utc_text(timestamps[1])
        assert events[3]["payload_json"]["signal_at"] == _utc_text(timestamps[7])
        assert events[9]["payload_json"]["signal_at"] == _utc_text(timestamps[7])
        assert events[9]["payload_json"]["filled_at"] == _utc_text(timestamps[13])
        assert events[10]["payload_json"]["signal_at"] == _utc_text(timestamps[13])
        assert events[11]["payload_json"]["at"] == _utc_text(timestamps[14])
        assert events[13]["payload_json"]["at"] == _utc_text(timestamps[16])
        assert events[14]["payload_json"]["signal_at"] == _utc_text(timestamps[13])
        assert events[14]["payload_json"]["filled_at"] == _utc_text(timestamps[17])


def test_custom_saved_formula_persists_the_exact_computed_signal_series(
    tmp_path: Path,
) -> None:
    with BacktestHarness.create(tmp_path) as harness:
        days = weekday_range(date(2024, 1, 1), date(2024, 5, 1))
        harness.seed_instruments("600000.SH")
        harness.seed_symbol("600000.SH", Period.DAY, days)
        version = harness.create_formula("自定义波段", WAVE_FORMULA)

        completed = harness.run_single(
            version.id,
            symbol="600000.SH",
            period=Period.DAY,
            scoring_start=local_time(days[5]),
            scoring_end=local_time(days[-1]) + timedelta(days=1),
        )
        reference = completed.run.symbols[0].reference
        assert isinstance(reference, PinnedMarketRef)
        cold_formulas = FormulaService(
            repository=harness.formula_repository,
            lake=harness.market,
        )
        expected = cold_formulas.preview_routed(
            version.id,
            harness.market.read(reference.signal_manifest_record_id),
            {},
        )

        assert completed.run.status == "succeeded"
        assert completed.run.symbols[0].signal_series_id == expected.signal_series_id
        assert completed.run.snapshot.formula_version_id == expected.formula_version_id
        assert completed.run.snapshot.formula_checksum == expected.formula_checksum
        assert completed.report.metrics["realized_count"] > 0


@pytest.mark.parametrize("scope", ["single", "pool"])
@pytest.mark.parametrize("period", [Period.DAY, Period.WEEK, Period.MIN60])
def test_complete_runner_matrix_supports_single_and_pool_scopes_for_every_period(
    tmp_path: Path,
    scope: str,
    period: Period,
) -> None:
    symbols = ("600000.SH",) if scope == "single" else ("600000.SH", "000001.SZ")
    with BacktestHarness.create(tmp_path) as harness:
        harness.seed_instruments(*symbols)
        start = date(2024, 1, 1)
        if period is Period.DAY:
            timeline = weekday_range(start, date(2024, 5, 1))
            scoring_start = local_time(timeline[5])
            scoring_end = local_time(timeline[-1]) + timedelta(days=1)
        elif period is Period.WEEK:
            timeline = weekly_timestamps(start, 24)
            scoring_start = timeline[6]
            scoring_end = timeline[-1] + timedelta(days=7)
        else:
            timeline = intraday_timestamps(start, trading_days=35)
            scoring_start = timeline[8]
            scoring_end = timeline[-1] + timedelta(hours=1)
        for offset, symbol in enumerate(symbols):
            harness.seed_symbol(symbol, period, timeline, phase_offset=offset * 3)
        version = harness.create_formula("周期矩阵", WAVE_FORMULA)

        completed = (
            harness.run_single(
                version.id,
                symbol=symbols[0],
                period=period,
                scoring_start=scoring_start,
                scoring_end=scoring_end,
            )
            if scope == "single"
            else harness.run_pool(
                version.id,
                symbols=symbols,
                period=period,
                scoring_start=scoring_start,
                scoring_end=scoring_end,
            )
        )

        assert completed.run.status == "succeeded"
        assert completed.run.total == len(symbols)
        assert completed.run.processed == len(symbols)
        assert completed.run.failed == 0
        assert all(item.signal_series_id for item in completed.run.symbols)
        assert completed.report.period == period.value
        assert completed.report.outcomes.succeeded == len(symbols)
        for item in completed.run.symbols:
            reference = item.reference
            assert reference.signal_query.period is period
            assert reference.execution_query.period is (
                Period.DAY if period is Period.WEEK else period
            )


def test_two_symbol_pool_reconciles_independent_samples_without_equity_curve(
    tmp_path: Path,
) -> None:
    symbols = ("600000.SH", "000001.SZ")
    with BacktestHarness.create(tmp_path) as harness:
        days = weekday_range(date(2024, 1, 1), date(2024, 7, 1))
        harness.seed_instruments(*symbols)
        for offset, symbol in enumerate(symbols):
            harness.seed_symbol(symbol, Period.DAY, days, phase_offset=offset * 4)
        version = harness.create_formula("独立样本", MACD_TEMPLATE_SOURCE)

        completed = harness.run_pool(
            version.id,
            symbols=symbols,
            period=Period.DAY,
            scoring_start=local_time(days[10]),
            scoring_end=local_time(days[-1]) + timedelta(days=1),
        )
        groups = completed.service.page(
            completed.run.id,
            collection="groups",
            dimension="symbol",
            limit=10,
            cursor=None,
        ).items
        metrics = completed.report.metrics

        assert completed.run.status == "succeeded"
        assert metrics["label"] == "independent trade samples, not portfolio return"
        assert metrics["equity_curve"] is None
        assert metrics["realized_count"] > 0
        assert {group.key for group in groups} == set(symbols)
        assert (
            sum(group.payload["realized_count"] for group in groups)
            == metrics["realized_count"]
        )
        assert all(
            group.payload["realized_denominator"] == metrics["realized_count"]
            for group in groups
        )


def test_persisted_costs_and_open_trade_stay_out_of_realized_metrics(
    tmp_path: Path,
) -> None:
    with BacktestHarness.create(tmp_path) as harness:
        days = weekday_range(date(2024, 1, 1), date(2024, 2, 1))
        harness.seed_instruments("600000.SH")
        harness.seed_symbol("600000.SH", Period.DAY, days)
        version = harness.create_formula("只买不卖", OPEN_ONLY_FORMULA)

        completed = harness.run_single(
            version.id,
            symbol="600000.SH",
            period=Period.DAY,
            scoring_start=local_time(days[1]),
            scoring_end=local_time(days[-1]) + timedelta(days=1),
            quantity_shares=1_000,
            commission_bps=Decimal("2.5"),
            minimum_commission=Decimal("5"),
            sell_tax_bps=Decimal("5"),
            slippage_bps=Decimal("3"),
        )
        open_page = completed.service.page(
            completed.run.id,
            collection="open",
            limit=10,
            cursor=None,
        )
        metrics = completed.report.metrics

        assert completed.run.status == "succeeded"
        assert len(open_page.items) == 1
        assert metrics["realized_count"] == 0
        assert metrics["win_rate"] is None
        assert metrics["win_rate_reason"] == "no_realized_samples"
        assert metrics["open_trades"]["count"] == 1
        assert completed.report.quantity_shares == 1_000
        assert completed.report.commission_bps == "2.5"
        assert completed.report.minimum_commission == "5"
        assert completed.report.sell_tax_bps == "5"
        assert completed.report.slippage_bps == "3"
        payload = open_page.items[0].payload
        assert payload["buy_commission"] == "5"
        assert payload["slippage_cost"] != "0"
        assert payload["sell_fill_price"] is None
        assert payload["sell_commission"] == "0"
        assert payload["sell_tax"] == "0"
