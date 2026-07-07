from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

import pytest

from stock_desk.backtest.costs import CostModel
from stock_desk.backtest.events import (
    OpenTradeMarked,
    OrderEvent,
    OrderFilled,
    OrderPending,
)
from stock_desk.backtest.metrics import (
    BacktestMetrics,
    INDEPENDENT_SAMPLE_LABEL,
    HistogramBin,
    OpenTradeMetrics,
    Reliability,
    summarize,
)
from stock_desk.backtest.trades import TradeSample, close_trade, mark_open_trade


SHANGHAI = ZoneInfo("Asia/Shanghai")
ZERO_COST = CostModel(
    commission_bps=Decimal("0"),
    minimum_commission=Decimal("0"),
    sell_tax_bps=Decimal("0"),
    slippage_bps=Decimal("0"),
)


def realized(
    net_return: Decimal,
    *,
    symbol: str = "600000.SH",
    entry_fill_at: datetime | None = None,
    holding_days: int = 2,
    sequence: int = 0,
) -> TradeSample:
    entry_fill = entry_fill_at or datetime(
        2025, 1, 3, 9, 30, tzinfo=SHANGHAI
    ) + timedelta(days=sequence * 3)
    entry_signal = entry_fill - timedelta(hours=18, minutes=30)
    exit_signal = entry_fill + timedelta(days=holding_days - 1, hours=5, minutes=30)
    exit_fill = entry_fill + timedelta(days=holding_days)
    exit_price = Decimal("10") * (Decimal("1") + net_return)
    events: tuple[OrderEvent, ...] = (
        OrderPending(side="buy", signal_at=entry_signal, eligible_at=entry_fill),
        OrderFilled(
            side="buy",
            signal_at=entry_signal,
            filled_at=entry_fill,
            price=Decimal("10"),
            quantity=1_000,
        ),
        OrderPending(side="sell", signal_at=exit_signal, eligible_at=exit_fill),
        OrderFilled(
            side="sell",
            signal_at=exit_signal,
            filled_at=exit_fill,
            price=exit_price,
            quantity=1_000,
        ),
    )
    return close_trade(
        entry=Decimal("10"),
        exit=exit_price,
        quantity=1_000,
        cost_model=ZERO_COST,
        symbol=symbol,
        entry_signal_at=entry_signal,
        entry_fill_at=entry_fill,
        exit_signal_at=exit_signal,
        exit_fill_at=exit_fill,
        holding_bars=holding_days + 1,
        formula_version_id="formula-v1",
        signal_series_id="series-v1",
        market_manifest_ids=("market",),
        status_manifest_ids=("status",),
        order_events=events,
    )


def open_trade(
    floating_return: Decimal,
    *,
    symbol: str = "600000.SH",
    sequence: int = 0,
) -> TradeSample:
    offset = timedelta(days=sequence * 3)
    entry_signal = datetime(2025, 1, 2, 15, tzinfo=SHANGHAI) + offset
    entry_fill = datetime(2025, 1, 3, 9, 30, tzinfo=SHANGHAI) + offset
    mark_at = datetime(2025, 1, 5, 15, tzinfo=SHANGHAI) + offset
    mark_price = Decimal("10") * (Decimal("1") + floating_return)
    events: tuple[OrderEvent, ...] = (
        OrderPending(side="buy", signal_at=entry_signal, eligible_at=entry_fill),
        OrderFilled(
            side="buy",
            signal_at=entry_signal,
            filled_at=entry_fill,
            price=Decimal("10"),
            quantity=1_000,
        ),
        OpenTradeMarked(
            entry_at=entry_fill,
            entry_price=Decimal("10"),
            quantity=1_000,
            mark_at=mark_at,
            mark_price=mark_price,
            floating_pnl=(mark_price - Decimal("10")) * 1_000,
        ),
    )
    return mark_open_trade(
        entry=Decimal("10"),
        mark=mark_price,
        mark_at=mark_at,
        quantity=1_000,
        cost_model=ZERO_COST,
        symbol=symbol,
        entry_signal_at=entry_signal,
        entry_fill_at=entry_fill,
        holding_bars=3,
        formula_version_id="formula-v1",
        signal_series_id="series-v1",
        market_manifest_ids=("market",),
        status_manifest_ids=("status",),
        order_events=events,
    )


def test_win_rate_uses_positive_net_realized_trades_only() -> None:
    metrics = summarize(
        (
            realized(Decimal("0.1"), sequence=1),
            realized(Decimal("-0.02"), sequence=2),
            realized(Decimal("0"), sequence=3),
            open_trade(Decimal("1"), sequence=4),
        )
    )

    assert metrics.realized_count == 3
    assert metrics.win_rate_denominator == 3
    assert metrics.positive_count == 1
    assert metrics.negative_count == 1
    assert metrics.zero_count == 1
    assert metrics.win_rate == Decimal("0.333333")
    assert metrics.win_rate_reason is None


def test_no_realized_trade_has_explicit_unknown_statistics() -> None:
    metrics = summarize((open_trade(Decimal("0.05")),))

    assert metrics.realized_count == 0
    assert metrics.win_rate is None
    assert metrics.win_rate_reason == "no_realized_samples"
    assert metrics.mean_net_return is None
    assert metrics.mean_net_return_reason == "no_realized_samples"
    assert metrics.median_net_return is None
    assert metrics.payoff_ratio is None
    assert metrics.payoff_ratio_reason == "no_positive_or_negative_returns"
    assert metrics.average_holding_days is None
    assert metrics.reliability.reason == "no_realized_samples"


def test_summary_discloses_returns_pnl_payoff_extremes_and_holding() -> None:
    metrics = summarize(
        (
            realized(Decimal("0.2"), holding_days=2, sequence=1),
            realized(Decimal("-0.1"), holding_days=4, sequence=2),
            realized(Decimal("0"), holding_days=6, sequence=3),
        )
    )

    assert metrics.mean_net_return == Decimal("0.033333")
    assert metrics.median_net_return == Decimal("0.000000")
    assert metrics.payoff_ratio == Decimal("2.000000")
    assert metrics.payoff_ratio_reason is None
    assert metrics.max_win_return == Decimal("0.200000")
    assert metrics.max_loss_return == Decimal("-0.100000")
    assert metrics.realized_net_pnl_total == Decimal("1000.00")
    assert metrics.average_holding_bars == Decimal("5.000000")
    assert metrics.average_holding_days == Decimal("4.000000")


def test_missing_positive_or_negative_side_has_explicit_payoff_reason() -> None:
    only_wins = summarize(
        (realized(Decimal("0.1"), sequence=1), realized(Decimal("0.2"), sequence=2))
    )
    only_losses = summarize((realized(Decimal("-0.1")),))

    assert only_wins.payoff_ratio is None
    assert only_wins.payoff_ratio_reason == "no_negative_returns"
    assert only_wins.max_loss_return is None
    assert only_wins.max_loss_return_reason == "no_negative_returns"
    assert only_losses.payoff_ratio is None
    assert only_losses.payoff_ratio_reason == "no_positive_returns"
    assert only_losses.max_win_return is None
    assert only_losses.max_win_return_reason == "no_positive_returns"


def test_histogram_uses_fixed_deterministic_bins_and_reconciles() -> None:
    returns = (
        Decimal("-0.25"),
        Decimal("-0.20"),
        Decimal("-0.10"),
        Decimal("-0.05"),
        Decimal("0"),
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("0.20"),
        Decimal("0.25"),
    )
    metrics = summarize(
        tuple(realized(value, sequence=index) for index, value in enumerate(returns))
    )

    assert tuple(bin_.code for bin_ in metrics.histogram) == (
        "lt_neg_20pct",
        "neg_20_to_10pct",
        "neg_10_to_5pct",
        "neg_5_to_0pct",
        "zero",
        "pos_0_to_5pct",
        "pos_5_to_10pct",
        "pos_10_to_20pct",
        "gt_20pct",
    )
    assert tuple(bin_.count for bin_ in metrics.histogram) == (1,) * 9
    assert sum(bin_.count for bin_ in metrics.histogram) == metrics.realized_count
    assert sum((bin_.share or Decimal("0")) for bin_ in metrics.histogram) == Decimal(
        "0.999999"
    )


def test_open_floating_totals_are_separate_from_realized_metrics() -> None:
    metrics = summarize(
        (
            realized(Decimal("0.1"), sequence=1),
            open_trade(Decimal("0.2"), sequence=2),
            open_trade(Decimal("-0.1"), symbol="000001.SZ"),
        )
    )

    assert metrics.realized_count == 1
    assert metrics.open_trades.count == 2
    assert metrics.open_trades.floating_pnl_total == Decimal("1000.00")
    assert metrics.open_trades.mean_floating_return == Decimal("0.050000")
    assert metrics.open_trades.mean_floating_return_reason is None


def test_no_open_samples_has_explicit_mean_reason_and_positive_zero_total() -> None:
    metrics = summarize((realized(Decimal("0.1")),))

    assert metrics.open_trades.count == 0
    assert metrics.open_trades.floating_pnl_total == Decimal("0.00")
    assert metrics.open_trades.floating_pnl_total.as_tuple().sign == 0
    assert metrics.open_trades.mean_floating_return is None
    assert metrics.open_trades.mean_floating_return_reason == "no_open_samples"


def test_reliability_reports_sample_size_and_symbol_concentration() -> None:
    small = summarize(
        tuple(realized(Decimal("0.01"), sequence=index) for index in range(3))
    )
    concentrated = summarize(
        tuple(
            realized(Decimal("0.01"), symbol="600000.SH", sequence=index)
            for index in range(20)
        )
        + tuple(
            realized(Decimal("0.01"), symbol="000001.SZ", sequence=index + 20)
            for index in range(10)
        )
    )
    moderate = summarize(
        tuple(
            realized(
                Decimal("0.01"),
                symbol="600000.SH" if index % 2 else "000001.SZ",
                sequence=index,
            )
            for index in range(30)
        )
    )
    high = summarize(
        tuple(
            realized(
                Decimal("0.01"),
                symbol="600000.SH" if index % 2 else "000001.SZ",
                sequence=index,
            )
            for index in range(100)
        )
    )

    assert (small.reliability.level, small.reliability.reason) == (
        "low",
        "small_sample",
    )
    assert concentrated.reliability.reason == "high_symbol_concentration"
    assert concentrated.reliability.largest_symbol_share == Decimal("0.666667")
    assert (moderate.reliability.level, moderate.reliability.reason) == (
        "medium",
        "moderate_sample",
    )
    assert (high.reliability.level, high.reliability.reason) == ("high", None)


def test_summary_is_immutable_strict_and_json_safe() -> None:
    metrics = summarize(
        (realized(Decimal("0"), sequence=1), open_trade(Decimal("0"), sequence=2))
    )
    payload = metrics.to_json_dict()

    assert metrics.label == INDEPENDENT_SAMPLE_LABEL
    assert "portfolio" in metrics.label
    assert json.loads(json.dumps(payload, allow_nan=False))["win_rate"] == "0"
    assert payload["equity_curve"] is None
    with pytest.raises((AttributeError, TypeError)):
        metrics.realized_count = 2  # type: ignore[misc]


def test_histogram_bin_rejects_invalid_count() -> None:
    with pytest.raises((TypeError, ValueError), match="count"):
        HistogramBin(
            code="bad",
            lower_bound=None,
            upper_bound=None,
            lower_inclusive=False,
            upper_inclusive=False,
            count=True,
            share=None,
            share_reason="no_realized_samples",
        )


def test_summary_rejects_non_trade_sample_input() -> None:
    with pytest.raises(TypeError, match="TradeSample"):
        summarize((object(),))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"label": "portfolio return"}, "label"),
        ({"win_rate_denominator": 99}, "denominator"),
        ({"positive_count": 99}, "sign counts"),
        ({"histogram": ()}, "histogram"),
        ({"mean_net_return_reason": "unexpected"}, "mean_net_return"),
    ],
)
def test_backtest_metrics_reject_inconsistent_value_copies(
    changes: dict[str, object], message: str
) -> None:
    metrics = summarize((realized(Decimal("0.1")),))

    with pytest.raises((TypeError, ValueError), match=message):
        replace(metrics, **changes)  # type: ignore[arg-type]


def test_open_and_reliability_value_objects_are_strict() -> None:
    with pytest.raises(ValueError, match="mean"):
        OpenTradeMetrics(
            count=0,
            floating_pnl_total=Decimal("0"),
            mean_floating_return=Decimal("0"),
            mean_floating_return_reason=None,
        )
    with pytest.raises(ValueError, match="concentration"):
        Reliability(
            level="low",
            reason="no_realized_samples",
            realized_count=0,
            largest_symbol_share=Decimal("0"),
        )
    with pytest.raises(ValueError, match="level"):
        Reliability(
            level="unknown",
            reason=None,
            realized_count=1,
            largest_symbol_share=Decimal("1"),
        )


def test_metric_classes_are_exported_as_immutable_values() -> None:
    metrics: BacktestMetrics = summarize((realized(Decimal("0.1")),))
    with pytest.raises((AttributeError, TypeError)):
        metrics.label = "changed"  # type: ignore[misc]


def test_summary_rejects_duplicate_trade_identity() -> None:
    trade = realized(Decimal("0.1"))

    with pytest.raises(ValueError, match="duplicate trade identity"):
        summarize((trade, trade))


def test_backtest_metrics_reconcile_reliability_to_global_count() -> None:
    metrics = summarize((realized(Decimal("0.1")),))
    inconsistent = replace(
        metrics.reliability,
        realized_count=99,
        largest_symbol_share=Decimal("0.5"),
        level="medium",
        reason="moderate_sample",
    )

    with pytest.raises(ValueError, match="reliability.*realized_count"):
        replace(metrics, reliability=inconsistent)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"largest_symbol_share": Decimal("1.000001")}, "share"),
        ({"level": "high", "reason": None}, "small_sample"),
        ({"level": "medium", "reason": "moderate_sample"}, "small_sample"),
    ],
)
def test_reliability_enforces_share_range_and_exact_level_semantics(
    changes: dict[str, object], message: str
) -> None:
    reliability = summarize((realized(Decimal("0.1")),)).reliability

    with pytest.raises(ValueError, match=message):
        replace(reliability, **changes)  # type: ignore[arg-type]


def test_histogram_bin_enforces_bounds_and_share_range() -> None:
    bin_ = summarize((realized(Decimal("0")),)).histogram[4]

    with pytest.raises(ValueError, match="bounds"):
        replace(bin_, lower_bound=Decimal("0.1"), upper_bound=Decimal("-0.1"))
    with pytest.raises(ValueError, match="share"):
        replace(bin_, share=Decimal("1.000001"))


def test_backtest_metrics_enforce_fixed_histogram_contract_and_ratios() -> None:
    metrics = summarize((realized(Decimal("0")),))
    wrong_code = replace(metrics.histogram[0], code="other")
    wrong_share = replace(metrics.histogram[4], share=Decimal("0.5"))

    with pytest.raises(ValueError, match="fixed 9-bin contract"):
        replace(metrics, histogram=(wrong_code, *metrics.histogram[1:]))
    with pytest.raises(ValueError, match="share.*count"):
        replace(
            metrics,
            histogram=(
                *metrics.histogram[:4],
                wrong_share,
                *metrics.histogram[5:],
            ),
        )


def test_metric_decimal_zero_fields_are_canonical_positive_zero() -> None:
    metrics = summarize(
        (realized(Decimal("0"), sequence=1), open_trade(Decimal("0"), sequence=2))
    )
    negative_zero_metrics = replace(
        metrics,
        win_rate=Decimal("-0.000000"),
        mean_net_return=Decimal("-0.000000"),
        realized_net_pnl_total=Decimal("-0.00"),
    )
    negative_zero_open = replace(
        metrics.open_trades,
        floating_pnl_total=Decimal("-0.00"),
        mean_floating_return=Decimal("-0.000000"),
    )
    negative_zero_reliability = replace(
        metrics.reliability, largest_symbol_share=Decimal("-0.000000")
    )
    negative_zero_bin = replace(
        metrics.histogram[4],
        lower_bound=Decimal("-0"),
        upper_bound=Decimal("-0"),
        share=Decimal("-0.000000"),
    )

    values = (
        negative_zero_metrics.win_rate,
        negative_zero_metrics.mean_net_return,
        negative_zero_metrics.realized_net_pnl_total,
        negative_zero_open.floating_pnl_total,
        negative_zero_open.mean_floating_return,
        negative_zero_reliability.largest_symbol_share,
        negative_zero_bin.lower_bound,
        negative_zero_bin.upper_bound,
        negative_zero_bin.share,
    )
    assert all(value is not None and value.as_tuple().sign == 0 for value in values)


def test_same_entry_with_different_exit_is_a_conflicting_duplicate() -> None:
    first = realized(Decimal("0.1"))
    conflicting = realized(Decimal("-0.1"))

    with pytest.raises(ValueError, match="duplicate trade identity"):
        summarize((first, conflicting))


def test_open_and_realized_versions_of_same_entry_conflict() -> None:
    opened = open_trade(Decimal("0.1"))
    closed = realized(Decimal("0.1"))

    with pytest.raises(ValueError, match="duplicate trade identity"):
        summarize((opened, closed))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"win_rate": Decimal("0.9")}, "win_rate"),
        ({"mean_net_return": Decimal("0.9")}, "mean_net_return"),
        ({"median_net_return": Decimal("0.9")}, "median_net_return"),
        ({"average_holding_days": Decimal("99")}, "average_holding_days"),
        (
            {
                "max_win_return": None,
                "max_win_return_reason": "no_positive_returns",
            },
            "max_win_return",
        ),
        (
            {"payoff_ratio": None, "payoff_ratio_reason": "no_negative_returns"},
            "payoff_ratio",
        ),
    ],
)
def test_global_metrics_cannot_be_tampered_independently_of_audit_values(
    changes: dict[str, object], message: str
) -> None:
    metrics = summarize(
        (
            realized(Decimal("0.2"), sequence=1),
            realized(Decimal("-0.1"), sequence=2),
        )
    )

    with pytest.raises(ValueError, match=message):
        replace(metrics, **changes)  # type: ignore[arg-type]


def test_empty_global_metrics_cannot_claim_defined_statistics() -> None:
    metrics = summarize(())

    with pytest.raises(ValueError, match="no_realized_samples"):
        replace(metrics, win_rate=Decimal("0"), win_rate_reason=None)


def test_synced_forged_histogram_distribution_fails_return_audit() -> None:
    metrics = summarize((realized(Decimal("0.1")),))
    emptied_real_bin = replace(metrics.histogram[6], count=0, share=Decimal("0.000000"))
    forged_other_bin = replace(metrics.histogram[8], count=1, share=Decimal("1.000000"))
    forged = (
        *metrics.histogram[:6],
        emptied_real_bin,
        metrics.histogram[7],
        forged_other_bin,
    )

    with pytest.raises(ValueError, match="histogram.*return audit"):
        replace(metrics, histogram=forged)


def test_forged_reliability_concentration_fails_symbol_audit() -> None:
    metrics = summarize((realized(Decimal("0.1")),))
    forged = replace(metrics.reliability, largest_symbol_share=Decimal("0.5"))

    with pytest.raises(ValueError, match="reliability.*symbol audit"):
        replace(metrics, reliability=forged)


def test_forged_open_totals_fail_open_value_audit() -> None:
    metrics = summarize((open_trade(Decimal("0.1")),))
    forged = replace(
        metrics.open_trades,
        floating_pnl_total=Decimal("999.00"),
        mean_floating_return=Decimal("0.9"),
    )

    with pytest.raises(ValueError, match="open metrics.*audit"):
        replace(metrics, open_trades=forged)
