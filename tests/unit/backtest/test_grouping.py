from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

from stock_desk.backtest.grouping import (
    GroupSummary,
    GroupedMetrics,
    group_by_entry_month,
    group_by_entry_year,
    group_by_symbol,
)
from stock_desk.backtest.costs import CostModel
from stock_desk.backtest.events import (
    OpenTradeMarked,
    OrderEvent,
    OrderFilled,
    OrderPending,
)
from stock_desk.backtest.metrics import INDEPENDENT_SAMPLE_LABEL
from stock_desk.backtest.trades import TradeSample, close_trade, mark_open_trade


UTC = ZoneInfo("UTC")
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
    sequence: int = 0,
) -> TradeSample:
    entry_fill = entry_fill_at or datetime(
        2025, 1, 3, 9, 30, tzinfo=SHANGHAI
    ) + timedelta(days=sequence * 3)
    entry_signal = entry_fill - timedelta(hours=18, minutes=30)
    exit_signal = entry_fill + timedelta(days=1, hours=5, minutes=30)
    exit_fill = entry_fill + timedelta(days=2)
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
        holding_bars=3,
        formula_version_id="formula-v1",
        signal_series_id="series-v1",
        market_manifest_ids=("market",),
        status_manifest_ids=("status",),
        order_events=events,
    )


def open_trade(floating_return: Decimal, *, symbol: str = "600000.SH") -> TradeSample:
    entry_signal = datetime(2025, 1, 2, 15, tzinfo=SHANGHAI)
    entry_fill = datetime(2025, 1, 3, 9, 30, tzinfo=SHANGHAI)
    mark_at = datetime(2025, 1, 5, 15, tzinfo=SHANGHAI)
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


def test_symbol_groups_reconcile_to_global_realized_denominator() -> None:
    samples = (
        realized(Decimal("0.1"), symbol="600000.SH", sequence=1),
        realized(Decimal("-0.1"), symbol="600000.SH", sequence=2),
        realized(Decimal("0"), symbol="000001.SZ"),
        open_trade(Decimal("1"), symbol="300001.SZ"),
    )

    grouped = group_by_symbol(samples)

    assert grouped.dimension == "symbol"
    assert grouped.realized_denominator == 3
    assert tuple(group.key for group in grouped.groups) == (
        "000001.SZ",
        "600000.SH",
    )
    assert sum(group.realized_count for group in grouped.groups) == 3
    assert all(group.realized_denominator == 3 for group in grouped.groups)
    assert sum(group.share_of_all for group in grouped.groups) == Decimal("1.000000")
    assert grouped.groups[1].win_rate == Decimal("0.500000")
    assert grouped.label == INDEPENDENT_SAMPLE_LABEL


def test_entry_month_and_year_use_shanghai_calendar_dates() -> None:
    jan_utc_but_feb_shanghai = datetime(2025, 1, 31, 16, 30, tzinfo=UTC)
    dec_utc_but_next_year_shanghai = datetime(2025, 12, 31, 16, 30, tzinfo=UTC)
    samples = (
        realized(Decimal("0.1"), entry_fill_at=jan_utc_but_feb_shanghai),
        realized(Decimal("-0.1"), entry_fill_at=dec_utc_but_next_year_shanghai),
    )

    monthly = group_by_entry_month(samples)
    yearly = group_by_entry_year(samples)

    assert tuple(group.key for group in monthly.groups) == ("2025-02", "2026-01")
    assert tuple(group.key for group in yearly.groups) == ("2025", "2026")
    assert all(group.realized_denominator == 2 for group in monthly.groups)
    assert all(group.realized_denominator == 2 for group in yearly.groups)


def test_group_statistics_use_each_group_samples_but_same_global_denominator() -> None:
    grouped = group_by_symbol(
        (
            realized(Decimal("0.2"), symbol="600000.SH", sequence=1),
            realized(Decimal("-0.1"), symbol="600000.SH", sequence=2),
            realized(Decimal("0"), symbol="000001.SZ"),
        )
    )
    by_key = {group.key: group for group in grouped.groups}

    assert by_key["600000.SH"].positive_count == 1
    assert by_key["600000.SH"].negative_count == 1
    assert by_key["600000.SH"].mean_net_return == Decimal("0.050000")
    assert by_key["600000.SH"].median_net_return == Decimal("0.050000")
    assert by_key["000001.SZ"].zero_count == 1
    assert by_key["000001.SZ"].win_rate == Decimal("0.000000")


def test_no_realized_samples_returns_empty_groups_with_reason() -> None:
    grouped = group_by_symbol((open_trade(Decimal("0.2")),))

    assert grouped.realized_denominator == 0
    assert grouped.groups == ()
    assert grouped.reason == "no_realized_samples"


def test_all_group_surfaces_are_deterministic_and_json_safe() -> None:
    samples = (
        realized(Decimal("0.1"), symbol="600000.SH"),
        realized(Decimal("-0.1"), symbol="000001.SZ"),
    )

    for grouped in (
        group_by_symbol(samples),
        group_by_entry_month(samples),
        group_by_entry_year(samples),
    ):
        payload = grouped.to_json_dict()
        encoded = json.dumps(payload, allow_nan=False, sort_keys=True)
        assert json.loads(encoded)["label"] == INDEPENDENT_SAMPLE_LABEL
        assert payload["equity_curve"] is None


def test_grouping_rejects_non_trade_sample_input() -> None:
    try:
        group_by_symbol((object(),))  # type: ignore[arg-type]
    except TypeError as error:
        assert "TradeSample" in str(error)
    else:
        raise AssertionError("invalid group sample was accepted")


def test_group_summary_rejects_inconsistent_value_copies() -> None:
    grouped = group_by_symbol((realized(Decimal("0.1")),))
    group = grouped.groups[0]

    for changes, message in (
        ({"realized_count": 0}, "contain"),
        ({"realized_denominator": 0}, "positive"),
        ({"positive_count": 0}, "reconcile"),
        ({"payoff_ratio_reason": None}, "reason"),
    ):
        try:
            replace(group, **changes)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("inconsistent group was accepted")


def test_grouped_metrics_reject_inconsistent_value_copies() -> None:
    grouped = group_by_symbol((realized(Decimal("0.1")),))

    for changes, message in (
        ({"dimension": "bad"}, "dimension"),
        ({"label": "portfolio"}, "label"),
        ({"realized_denominator": 2}, "reconcile"),
        ({"reason": "unexpected"}, "reason"),
    ):
        try:
            replace(grouped, **changes)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("inconsistent grouped metrics were accepted")


def test_group_types_are_exported_as_immutable_values() -> None:
    grouped: GroupedMetrics = group_by_symbol((realized(Decimal("0.1")),))
    group: GroupSummary = grouped.groups[0]
    try:
        group.key = "changed"  # type: ignore[misc]
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("group summary was mutable")


def test_grouping_rejects_duplicate_trade_identity() -> None:
    trade = realized(Decimal("0.1"))

    try:
        group_by_symbol((trade, trade))
    except ValueError as error:
        assert "duplicate trade identity" in str(error)
    else:
        raise AssertionError("duplicate group sample was accepted")


def test_group_summary_enforces_ratio_and_nonnegative_invariants() -> None:
    group = group_by_symbol((realized(Decimal("0")),)).groups[0]

    for changes, message in (
        ({"share_of_all": Decimal("0.5")}, "share"),
        ({"win_rate": Decimal("0.5")}, "win_rate"),
        ({"average_holding_days": Decimal("-1")}, "holding"),
        (
            {"payoff_ratio": Decimal("-1"), "payoff_ratio_reason": None},
            "payoff",
        ),
    ):
        try:
            replace(group, **changes)
        except ValueError as error:
            assert message in str(error)
        else:
            raise AssertionError("invalid group ratio was accepted")


def test_grouped_metrics_reconcile_quantized_share_sum_with_error_bound() -> None:
    grouped = group_by_symbol(
        (
            realized(Decimal("0.1"), symbol="600000.SH"),
            realized(Decimal("0.2"), symbol="000001.SZ"),
            realized(Decimal("0.3"), symbol="300001.SZ"),
        )
    )
    assert sum(group.share_of_all for group in grouped.groups) == Decimal("0.999999")

    try:
        tampered = replace(grouped.groups[0], share_of_all=Decimal("0.333334"))
        replace(grouped, groups=(tampered, *grouped.groups[1:]))
    except ValueError as error:
        assert "share" in str(error)
    else:
        raise AssertionError("tampered group share was accepted")


def test_group_decimal_zero_fields_are_canonical_positive_zero() -> None:
    group = group_by_symbol((realized(Decimal("0")),)).groups[0]
    canonical = replace(
        group,
        win_rate=Decimal("-0.000000"),
        mean_net_return=Decimal("-0.000000"),
        median_net_return=Decimal("-0.000000"),
        net_pnl_total=Decimal("-0.00"),
    )

    assert all(
        value.as_tuple().sign == 0
        for value in (
            canonical.win_rate,
            canonical.mean_net_return,
            canonical.median_net_return,
            canonical.net_pnl_total,
        )
    )


def test_group_derived_mean_cannot_be_tampered_with_counts_unchanged() -> None:
    group = group_by_symbol(
        (
            realized(Decimal("0.2"), sequence=1),
            realized(Decimal("-0.1"), sequence=2),
        )
    ).groups[0]

    try:
        replace(group, mean_net_return=Decimal("0.9"))
    except ValueError as error:
        assert "mean_net_return" in str(error)
    else:
        raise AssertionError("tampered group mean was accepted")
