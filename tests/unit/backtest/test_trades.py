from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from typing import TypedDict
from zoneinfo import ZoneInfo

import pytest

from stock_desk.backtest.costs import CostModel, price_order
from stock_desk.backtest.events import (
    CancellationReason,
    IgnoredSignal,
    IgnoredSignalReason,
    OpenTradeMarked,
    OrderBlocked,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderPending,
    OrderUnfilled,
    SignalCode,
)
from stock_desk.backtest.trades import (
    OPEN_PNL_CONVENTION,
    PRICE_BASIS_CONVENTION,
    RatioMetric,
    SIZING_VERSION,
    TradeSample,
    calculate_payoff_ratio,
    calculate_win_rate,
    close_trade,
    mark_open_trade,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
BUY_SIGNAL_AT = datetime(2025, 1, 2, 15, tzinfo=SHANGHAI)
BUY_FILL_AT = datetime(2025, 1, 3, 9, 30, tzinfo=SHANGHAI)
SELL_SIGNAL_AT = datetime(2025, 1, 8, 15, tzinfo=SHANGHAI)
SELL_FILL_AT = datetime(2025, 1, 9, 9, 30, tzinfo=SHANGHAI)
MARK_AT = datetime(2025, 1, 10, 15, tzinfo=SHANGHAI)


@pytest.fixture
def cost_model() -> CostModel:
    return CostModel(
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        sell_tax_bps=Decimal("5"),
        slippage_bps=Decimal("3"),
    )


class IdentityKwargs(TypedDict):
    symbol: str
    entry_signal_at: datetime
    entry_fill_at: datetime
    holding_bars: int
    formula_version_id: str
    signal_series_id: str
    market_manifest_ids: tuple[str, ...]
    status_manifest_ids: tuple[str, ...]
    order_events: tuple[OrderEvent, ...]


def identity_kwargs(*, order_events: tuple[OrderEvent, ...]) -> IdentityKwargs:
    return {
        "symbol": "600000.SH",
        "entry_signal_at": BUY_SIGNAL_AT,
        "entry_fill_at": BUY_FILL_AT,
        "holding_bars": 4,
        "formula_version_id": "formula-v1",
        "signal_series_id": "signal-series-sha256",
        "market_manifest_ids": ("signal-manifest", "fill-manifest"),
        "status_manifest_ids": ("status-manifest",),
        "order_events": order_events,
    }


def realized_events(
    *, entry: Decimal, exit: Decimal, cost_model: CostModel
) -> tuple[OrderEvent, ...]:
    buy = price_order(
        side="buy", reference_open=entry, quantity=1_000, model=cost_model
    )
    sell = price_order(
        side="sell", reference_open=exit, quantity=1_000, model=cost_model
    )
    return (
        OrderPending(side="buy", signal_at=BUY_SIGNAL_AT, eligible_at=BUY_FILL_AT),
        OrderFilled(
            side="buy",
            signal_at=BUY_SIGNAL_AT,
            filled_at=BUY_FILL_AT,
            price=buy.reference_open,
            quantity=1_000,
        ),
        OrderPending(side="sell", signal_at=SELL_SIGNAL_AT, eligible_at=SELL_FILL_AT),
        OrderFilled(
            side="sell",
            signal_at=SELL_SIGNAL_AT,
            filled_at=SELL_FILL_AT,
            price=sell.reference_open,
            quantity=1_000,
        ),
    )


def open_events(*, mark: Decimal, cost_model: CostModel) -> tuple[OrderEvent, ...]:
    buy = price_order(
        side="buy", reference_open=Decimal("10"), quantity=1_000, model=cost_model
    )
    return (
        OrderPending(side="buy", signal_at=BUY_SIGNAL_AT, eligible_at=BUY_FILL_AT),
        OrderFilled(
            side="buy",
            signal_at=BUY_SIGNAL_AT,
            filled_at=BUY_FILL_AT,
            price=buy.reference_open,
            quantity=1_000,
        ),
        OpenTradeMarked(
            entry_at=BUY_FILL_AT,
            entry_price=buy.reference_open,
            quantity=1_000,
            mark_at=MARK_AT,
            mark_price=mark,
            floating_pnl=(mark - buy.reference_open) * 1_000,
        ),
    )


def realized_trade(
    *,
    entry: Decimal = Decimal("10"),
    exit: Decimal = Decimal("11"),
    cost_model: CostModel,
) -> TradeSample:
    return close_trade(
        entry=entry,
        exit=exit,
        quantity=1_000,
        cost_model=cost_model,
        exit_signal_at=SELL_SIGNAL_AT,
        exit_fill_at=SELL_FILL_AT,
        **identity_kwargs(
            order_events=realized_events(entry=entry, exit=exit, cost_model=cost_model)
        ),
    )


def open_trade(*, mark: Decimal, cost_model: CostModel) -> TradeSample:
    return mark_open_trade(
        entry=Decimal("10"),
        mark=mark,
        mark_at=MARK_AT,
        quantity=1_000,
        cost_model=cost_model,
        **identity_kwargs(order_events=open_events(mark=mark, cost_model=cost_model)),
    )


def test_realized_net_return_discloses_each_cost(cost_model: CostModel) -> None:
    trade = realized_trade(cost_model=cost_model)

    assert trade.realized is True
    assert trade.sizing_version == SIZING_VERSION == "fixed-lot-v1"
    assert trade.quantity == 1_000
    assert trade.entry_reference_open == Decimal("10.0000")
    assert trade.exit_reference_open == Decimal("11.0000")
    assert trade.buy_fill_price == Decimal("10.0030")
    assert trade.sell_fill_price == Decimal("10.9967")
    assert trade.reference_gross_pnl == Decimal("1000.00")
    assert trade.slippage_cost == Decimal("6.30")
    assert trade.fill_gross_pnl == Decimal("993.70")
    assert trade.buy_commission == Decimal("5.00")
    assert trade.sell_commission == Decimal("5.00")
    assert trade.sell_tax == Decimal("5.50")
    assert trade.net_pnl == Decimal("978.20")
    assert trade.net_pnl == (
        trade.reference_gross_pnl
        - trade.buy_commission
        - trade.sell_commission
        - trade.sell_tax
        - trade.slippage_cost
    )
    assert trade.fill_gross_pnl == trade.reference_gross_pnl - trade.slippage_cost
    assert trade.invested_cost == (
        trade.buy_fill_price * trade.quantity + trade.buy_commission
    )
    assert trade.net_return == trade.net_pnl / trade.invested_cost
    assert trade.floating_pnl is None
    assert trade.floating_return is None


def test_trade_retains_identity_timing_manifests_and_events(
    cost_model: CostModel,
) -> None:
    trade = realized_trade(cost_model=cost_model)

    assert trade.symbol == "600000.SH"
    assert trade.entry_signal_at == BUY_SIGNAL_AT
    assert trade.entry_fill_at == BUY_FILL_AT
    assert trade.exit_signal_at == SELL_SIGNAL_AT
    assert trade.exit_fill_at == SELL_FILL_AT
    assert trade.holding_bars == 4
    assert trade.holding_days == 6
    assert trade.formula_version_id == "formula-v1"
    assert trade.signal_series_id == "signal-series-sha256"
    assert trade.market_manifest_ids == ("signal-manifest", "fill-manifest")
    assert trade.status_manifest_ids == ("status-manifest",)
    assert trade.cost_model_version == "a-share-cost-v1"
    assert len(trade.order_events) == 4


def test_zero_reference_return_is_negative_after_costs(
    cost_model: CostModel,
) -> None:
    trade = realized_trade(
        entry=Decimal("10"), exit=Decimal("10"), cost_model=cost_model
    )

    assert trade.reference_gross_pnl == Decimal("0.00")
    assert trade.fill_gross_pnl == -trade.slippage_cost
    assert trade.net_pnl is not None
    assert trade.net_pnl < 0


@pytest.mark.parametrize("entry, exit", [("10", "11"), ("5", "5.5"), ("20", "22")])
def test_adjustment_mode_price_bases_use_the_same_accounting_bridge(
    entry: str, exit: str, cost_model: CostModel
) -> None:
    trade = realized_trade(
        entry=Decimal(entry), exit=Decimal(exit), cost_model=cost_model
    )

    assert trade.fill_gross_pnl == trade.reference_gross_pnl - trade.slippage_cost
    assert trade.net_pnl == (
        trade.fill_gross_pnl
        - trade.buy_commission
        - trade.sell_commission
        - trade.sell_tax
    )


def test_open_trade_uses_last_price_without_hypothetical_exit_cost(
    cost_model: CostModel,
) -> None:
    trade = open_trade(mark=Decimal("11"), cost_model=cost_model)

    assert trade.realized is False
    assert trade.exit_signal_at is None
    assert trade.exit_fill_at is None
    assert trade.exit_reference_open is None
    assert trade.sell_fill_price is None
    assert trade.sell_commission == Decimal("0.00")
    assert trade.sell_tax == Decimal("0.00")
    assert trade.reference_gross_pnl == Decimal("1000.00")
    assert trade.slippage_cost == Decimal("3.00")
    assert trade.fill_gross_pnl == Decimal("997.00")
    assert trade.net_pnl is None
    assert trade.net_return is None
    assert trade.floating_pnl == Decimal("992.00")
    assert trade.floating_return == trade.floating_pnl / trade.invested_cost
    assert trade.open_pnl_convention == "last_price_without_exit_costs"


def test_open_trade_is_excluded_from_win_rate(cost_model: CostModel) -> None:
    winner = realized_trade(cost_model=cost_model)
    loser = realized_trade(
        entry=Decimal("11"), exit=Decimal("10"), cost_model=cost_model
    )
    still_open = open_trade(mark=Decimal("100"), cost_model=cost_model)

    metric = calculate_win_rate((winner, loser, still_open))

    assert metric.value == Decimal("0.5")
    assert metric.sample_count == 2
    assert metric.reason is None


def test_no_realized_trade_has_undefined_win_rate(cost_model: CostModel) -> None:
    metric = calculate_win_rate(
        (open_trade(mark=Decimal("100"), cost_model=cost_model),)
    )

    assert metric.value is None
    assert metric.sample_count == 0
    assert metric.reason == "no_realized_samples"


def test_payoff_ratio_uses_mean_positive_and_negative_realized_returns() -> None:
    zero_cost = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    winner = realized_trade(
        entry=Decimal("10"), exit=Decimal("12"), cost_model=zero_cost
    )
    loser = realized_trade(entry=Decimal("10"), exit=Decimal("9"), cost_model=zero_cost)
    ignored_open = open_trade(mark=Decimal("100"), cost_model=zero_cost)

    metric = calculate_payoff_ratio((winner, loser, ignored_open))

    assert metric.value == Decimal("2")
    assert metric.sample_count == 2
    assert metric.reason is None


@pytest.mark.parametrize(
    ("returns", "reason"),
    [
        ((Decimal("11"), Decimal("12")), "no_negative_returns"),
        ((Decimal("9"), Decimal("8")), "no_positive_returns"),
        ((Decimal("10"), Decimal("10")), "no_positive_or_negative_returns"),
    ],
)
def test_payoff_ratio_is_explicit_when_one_side_is_empty(
    returns: tuple[Decimal, Decimal], reason: str
) -> None:
    zero_cost = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    samples = tuple(
        realized_trade(entry=Decimal("10"), exit=value, cost_model=zero_cost)
        for value in returns
    )

    metric = calculate_payoff_ratio(samples)

    assert metric.value is None
    assert metric.sample_count == 2
    assert metric.reason == reason


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"quantity": 150}, "100-share"),
        ({"exit_fill_at": BUY_FILL_AT - timedelta(minutes=1)}, "exit_fill_at"),
        ({"holding_bars": -1}, "holding_bars"),
        ({"market_manifest_ids": []}, "market_manifest_ids"),
        ({"signal_series_id": ""}, "signal_series_id"),
    ],
)
def test_realized_trade_rejects_invalid_input(
    changes: dict[str, object], message: str, cost_model: CostModel
) -> None:
    values: dict[str, object] = {
        "entry": Decimal("10"),
        "exit": Decimal("11"),
        "quantity": 1_000,
        "cost_model": cost_model,
        "exit_signal_at": SELL_SIGNAL_AT,
        "exit_fill_at": SELL_FILL_AT,
        **identity_kwargs(
            order_events=realized_events(
                entry=Decimal("10"), exit=Decimal("11"), cost_model=cost_model
            )
        ),
    }
    values.update(changes)

    with pytest.raises((TypeError, ValueError), match=message):
        close_trade(**values)  # type: ignore[arg-type]


def test_trade_sample_is_a_frozen_value_object(cost_model: CostModel) -> None:
    left = realized_trade(cost_model=cost_model)
    right = realized_trade(cost_model=cost_model)

    assert left == right
    assert hash(left) == hash(right)
    with pytest.raises((AttributeError, TypeError)):
        left.quantity = 2_000  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"fill_gross_pnl": Decimal("0.01")}, "fill_gross_pnl"),
        ({"net_pnl": Decimal("NaN")}, "net_pnl"),
        ({"net_return": Decimal("1")}, "net_return"),
        ({"invested_cost": Decimal("1")}, "invested_cost"),
        ({"holding_days": 99}, "holding_days"),
    ],
)
def test_trade_sample_rejects_inconsistent_value_copies(
    changes: dict[str, object], message: str, cost_model: CostModel
) -> None:
    trade = realized_trade(cost_model=cost_model)

    with pytest.raises(ValueError, match=message):
        replace(trade, **changes)  # type: ignore[arg-type]


def test_ratio_metric_is_a_frozen_value_object() -> None:
    metric = RatioMetric(value=None, reason="missing", sample_count=0)

    assert metric.reason == "missing"
    with pytest.raises((AttributeError, TypeError)):
        metric.reason = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"value": None, "reason": None, "sample_count": 0}, "reason"),
        ({"value": Decimal("1"), "reason": "unused", "sample_count": 1}, "reason"),
        ({"value": Decimal("1"), "reason": None, "sample_count": -1}, "sample_count"),
    ],
)
def test_ratio_metric_rejects_inconsistent_values(
    values: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        RatioMetric(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("values", "error", "message"),
    [
        ({"value": 1.0, "reason": None, "sample_count": 1}, TypeError, "value"),
        (
            {"value": Decimal("NaN"), "reason": None, "sample_count": 1},
            ValueError,
            "value",
        ),
        (
            {"value": Decimal("Infinity"), "reason": None, "sample_count": 1},
            ValueError,
            "value",
        ),
        (
            {"value": Decimal("1"), "reason": None, "sample_count": True},
            TypeError,
            "sample_count",
        ),
        (
            {"value": Decimal("1"), "reason": None, "sample_count": 1.0},
            TypeError,
            "sample_count",
        ),
    ],
)
def test_ratio_metric_rejects_non_json_safe_values(
    values: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        RatioMetric(**values)  # type: ignore[arg-type]


def test_ratio_metric_exposes_json_safe_primitive_values() -> None:
    metric = RatioMetric(value=Decimal("0.5000"), reason=None, sample_count=2)

    assert json.dumps(metric.to_json_dict(), allow_nan=False) == (
        '{"value": "0.5", "reason": null, "sample_count": 2}'
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sizing_version": "future"}, "sizing_version"),
        ({"realized": 1}, "realized"),
        ({"cost_model_version": "future"}, "cost_model_version"),
        ({"price_basis_convention": "raw_cash"}, "price_basis_convention"),
        ({"quantity": 0}, "quantity"),
        ({"holding_bars": True}, "holding_bars"),
        ({"order_events": (object(),)}, "order_events"),
        ({"open_pnl_convention": OPEN_PNL_CONVENTION}, "open PnL"),
        ({"mark_at": MARK_AT}, "mark"),
        ({"exit_reference_open": None}, "exit prices"),
        ({"net_pnl": None}, "net results"),
        ({"floating_pnl": Decimal("1")}, "floating results"),
        ({"reference_gross_pnl": Decimal("999")}, "reference_gross_pnl"),
        ({"slippage_cost": Decimal("0")}, "slippage_cost"),
    ],
)
def test_realized_trade_value_object_rejects_invalid_shape(
    changes: dict[str, object], message: str, cost_model: CostModel
) -> None:
    trade = realized_trade(cost_model=cost_model)

    with pytest.raises((TypeError, ValueError), match=message):
        replace(trade, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"open_pnl_convention": None}, "convention"),
        ({"mark_price": None}, "mark"),
        ({"exit_reference_open": Decimal("11")}, "exit prices"),
        ({"net_pnl": Decimal("1")}, "net results"),
        ({"floating_pnl": None}, "floating results"),
        ({"sell_commission": Decimal("1")}, "exit costs"),
        ({"floating_pnl": Decimal("999")}, "floating_pnl"),
        ({"floating_return": Decimal("999")}, "floating_return"),
    ],
)
def test_open_trade_value_object_rejects_invalid_shape(
    changes: dict[str, object], message: str, cost_model: CostModel
) -> None:
    trade = open_trade(mark=Decimal("11"), cost_model=cost_model)

    assert trade.price_basis_convention == PRICE_BASIS_CONVENTION
    with pytest.raises((TypeError, ValueError), match=message):
        replace(trade, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("event_transform", "message"),
    [
        (lambda events: (), "order_events"),
        (lambda events: events[:-1], "sell fill"),
        (
            lambda events: (
                events[0],
                replace(events[1], price=Decimal("99")),
                *events[2:],
            ),
            "buy fill",
        ),
        (
            lambda events: (
                *events[:3],
                replace(events[3], quantity=2_000),
            ),
            "sell fill",
        ),
    ],
)
def test_realized_trade_binds_full_order_event_identity(
    event_transform: Callable[[tuple[OrderEvent, ...]], tuple[OrderEvent, ...]],
    message: str,
    cost_model: CostModel,
) -> None:
    trade = realized_trade(cost_model=cost_model)

    with pytest.raises(ValueError, match=message):
        replace(trade, order_events=event_transform(trade.order_events))


@pytest.mark.parametrize(
    ("event_transform", "message"),
    [
        (lambda events: (), "order_events"),
        (lambda events: events[:-1], "open mark"),
        (
            lambda events: (
                *events[:2],
                replace(events[2], mark_price=Decimal("99")),
            ),
            "open mark",
        ),
    ],
)
def test_open_trade_binds_buy_fill_and_terminal_mark_events(
    event_transform: Callable[[tuple[OrderEvent, ...]], tuple[OrderEvent, ...]],
    message: str,
    cost_model: CostModel,
) -> None:
    trade = open_trade(mark=Decimal("11"), cost_model=cost_model)

    with pytest.raises(ValueError, match=message):
        replace(trade, order_events=event_transform(trade.order_events))


def test_holding_days_use_shanghai_dates_for_foreign_timezone_instants() -> None:
    utc = ZoneInfo("UTC")
    entry_signal_at = datetime(2025, 1, 2, 23, 30, tzinfo=utc)
    entry_fill_at = datetime(2025, 1, 3, 0, 30, tzinfo=utc)  # Shanghai Jan 3
    exit_signal_at = datetime(2025, 1, 4, 15, 30, tzinfo=utc)
    exit_fill_at = datetime(2025, 1, 4, 16, 30, tzinfo=utc)  # Shanghai Jan 5
    zero_cost = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    events: tuple[OrderEvent, ...] = (
        OrderPending(side="buy", signal_at=entry_signal_at, eligible_at=entry_fill_at),
        OrderFilled(
            side="buy",
            signal_at=entry_signal_at,
            filled_at=entry_fill_at,
            price=Decimal("10"),
            quantity=1_000,
        ),
        OrderPending(side="sell", signal_at=exit_signal_at, eligible_at=exit_fill_at),
        OrderFilled(
            side="sell",
            signal_at=exit_signal_at,
            filled_at=exit_fill_at,
            price=Decimal("11"),
            quantity=1_000,
        ),
    )

    trade = close_trade(
        entry=Decimal("10"),
        exit=Decimal("11"),
        quantity=1_000,
        cost_model=zero_cost,
        symbol="600000.SH",
        entry_signal_at=entry_signal_at,
        entry_fill_at=entry_fill_at,
        exit_signal_at=exit_signal_at,
        exit_fill_at=exit_fill_at,
        holding_bars=2,
        formula_version_id="formula-v1",
        signal_series_id="series-v1",
        market_manifest_ids=("market",),
        status_manifest_ids=("status",),
        order_events=events,
    )

    assert trade.holding_days == 2


def test_open_holding_days_use_shanghai_mark_date() -> None:
    utc = ZoneInfo("UTC")
    entry_signal_at = datetime(2025, 1, 2, 23, 30, tzinfo=utc)
    entry_fill_at = datetime(2025, 1, 3, 0, 30, tzinfo=utc)  # Shanghai Jan 3
    mark_at = datetime(2025, 1, 4, 16, 30, tzinfo=utc)  # Shanghai Jan 5
    zero_cost = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    events: tuple[OrderEvent, ...] = (
        OrderPending(side="buy", signal_at=entry_signal_at, eligible_at=entry_fill_at),
        OrderFilled(
            side="buy",
            signal_at=entry_signal_at,
            filled_at=entry_fill_at,
            price=Decimal("10"),
            quantity=1_000,
        ),
        OpenTradeMarked(
            entry_at=entry_fill_at,
            entry_price=Decimal("10"),
            quantity=1_000,
            mark_at=mark_at,
            mark_price=Decimal("11"),
            floating_pnl=Decimal("1000"),
        ),
    )

    trade = mark_open_trade(
        entry=Decimal("10"),
        mark=Decimal("11"),
        mark_at=mark_at,
        quantity=1_000,
        cost_model=zero_cost,
        symbol="600000.SH",
        entry_signal_at=entry_signal_at,
        entry_fill_at=entry_fill_at,
        holding_bars=2,
        formula_version_id="formula-v1",
        signal_series_id="series-v1",
        market_manifest_ids=("market",),
        status_manifest_ids=("status",),
        order_events=events,
    )

    assert trade.holding_days == 2


def test_trade_monetary_zero_values_are_positive_zero() -> None:
    zero_cost = CostModel(
        commission_bps=Decimal("-0"),
        minimum_commission=Decimal("-0"),
        sell_tax_bps=Decimal("-0"),
        slippage_bps=Decimal("-0"),
    )
    trade = realized_trade(
        entry=Decimal("10"), exit=Decimal("10"), cost_model=zero_cost
    )

    for value in (
        trade.buy_commission,
        trade.sell_commission,
        trade.sell_tax,
        trade.slippage_cost,
        trade.reference_gross_pnl,
        trade.fill_gross_pnl,
        trade.net_pnl,
        trade.net_return,
    ):
        assert value is not None
        assert value.as_tuple().sign == 0


def test_replay_rejects_fill_after_pending_was_cancelled(
    cost_model: CostModel,
) -> None:
    trade = realized_trade(cost_model=cost_model)
    impossible = (
        trade.order_events[0],
        OrderCancelled(
            side="buy",
            reason=CancellationReason.OPPOSITE_SIGNAL,
            at=BUY_FILL_AT - timedelta(minutes=1),
        ),
        *trade.order_events[1:],
    )

    with pytest.raises(ValueError, match="active pending"):
        replace(trade, order_events=impossible)


def test_replay_rejects_events_after_realized_sell_fill(
    cost_model: CostModel,
) -> None:
    trade = realized_trade(cost_model=cost_model)
    after_sell = SELL_FILL_AT + timedelta(minutes=1)
    impossible = (
        *trade.order_events,
        OrderPending(side="buy", signal_at=after_sell, eligible_at=after_sell),
    )

    with pytest.raises(ValueError, match="sell fill must be final"):
        replace(trade, order_events=impossible)


def test_replay_rejects_time_reversed_ignored_event(
    cost_model: CostModel,
) -> None:
    trade = realized_trade(cost_model=cost_model)
    impossible = (
        *trade.order_events[:2],
        IgnoredSignal(
            reason=IgnoredSignalReason.ALREADY_HOLDING,
            signal=SignalCode.BUY,
            at=BUY_SIGNAL_AT,
        ),
        *trade.order_events[2:],
    )

    with pytest.raises(ValueError, match="chronological"):
        replace(trade, order_events=impossible)


def test_replay_rejects_event_after_terminal_open_mark(
    cost_model: CostModel,
) -> None:
    trade = open_trade(mark=Decimal("11"), cost_model=cost_model)
    impossible = (
        *trade.order_events,
        IgnoredSignal(
            reason=IgnoredSignalReason.ALREADY_HOLDING,
            signal=SignalCode.BUY,
            at=MARK_AT + timedelta(minutes=1),
        ),
    )

    with pytest.raises(ValueError, match="open mark must be final"):
        replace(trade, order_events=impossible)


def test_replay_allows_legitimate_blocked_and_ignored_events(
    cost_model: CostModel,
) -> None:
    trade = realized_trade(cost_model=cost_model)
    valid = (
        trade.order_events[0],
        OrderBlocked(side="buy", at=BUY_FILL_AT, reason="suspended"),
        trade.order_events[1],
        IgnoredSignal(
            reason=IgnoredSignalReason.ALREADY_HOLDING,
            signal=SignalCode.BUY,
            at=SELL_SIGNAL_AT - timedelta(minutes=1),
        ),
        *trade.order_events[2:],
    )

    replayed = replace(trade, order_events=valid)

    assert replayed.order_events == valid


def test_open_replay_allows_pending_sell_to_end_unfilled_before_mark(
    cost_model: CostModel,
) -> None:
    trade = open_trade(mark=Decimal("11"), cost_model=cost_model)
    sell_signal_at = MARK_AT - timedelta(days=1)
    valid = (
        *trade.order_events[:2],
        OrderPending(side="sell", signal_at=sell_signal_at, eligible_at=MARK_AT),
        OrderUnfilled(
            side="sell",
            signal_at=sell_signal_at,
            eligible_at=MARK_AT,
            ended_at=MARK_AT,
        ),
        trade.order_events[-1],
    )

    replayed = replace(trade, order_events=valid)

    assert replayed.order_events == valid
