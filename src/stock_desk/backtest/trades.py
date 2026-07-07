from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable
from zoneinfo import ZoneInfo

from stock_desk.backtest.costs import (
    COST_MODEL_VERSION,
    CostModel,
    divide_decimal,
    price_order,
    round_money,
)
from stock_desk.backtest.events import (
    IgnoredSignal,
    IgnoredSignalReason,
    OpenTradeMarked,
    OrderBlocked,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderPending,
    OrderUnfilled,
)


SIZING_VERSION = "fixed-lot-v1"
PRICE_BASIS_CONVENTION = "selected_adjustment_series_internal_basis"
OPEN_PNL_CONVENTION = "last_price_without_exit_costs"
SHANGHAI = ZoneInfo("Asia/Shanghai")

_ORDER_EVENT_TYPES = (
    OrderPending,
    IgnoredSignal,
    OrderCancelled,
    OrderBlocked,
    OrderFilled,
    OrderUnfilled,
    OpenTradeMarked,
)


@dataclass(frozen=True, slots=True)
class RatioMetric:
    value: Decimal | None
    reason: str | None
    sample_count: int

    def __post_init__(self) -> None:
        if type(self.sample_count) is not int:
            raise TypeError("sample_count must be an integer")
        if self.sample_count < 0:
            raise ValueError("sample_count cannot be negative")
        if self.value is not None:
            if not isinstance(self.value, Decimal):
                raise TypeError("value must be a Decimal or None")
            if not self.value.is_finite():
                raise ValueError("value must be a finite Decimal")
            if self.value < 0:
                raise ValueError("value cannot be negative")
            if self.value.is_zero():
                object.__setattr__(self, "value", self.value.copy_abs())
        if self.reason is not None and (
            type(self.reason) is not str or not self.reason.strip()
        ):
            raise TypeError("reason must be a nonblank string or None")
        if self.value is None and not self.reason:
            raise ValueError("an undefined ratio requires a reason")
        if self.value is not None and self.reason is not None:
            raise ValueError("a defined ratio cannot include an undefined reason")

    def to_json_dict(self) -> dict[str, str | int | None]:
        """Return finite JSON primitives without float conversion or NaN tokens."""

        return {
            "value": _decimal_json_text(self.value) if self.value is not None else None,
            "reason": self.reason,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class TradeSample:
    """Immutable realized or open independent-trade sample.

    Prices use the selected none/qfq/hfq series as one internally consistent
    signal-and-return basis. This is intentionally not a claim that cash values
    are comparable across adjustment modes.
    """

    symbol: str
    realized: bool
    sizing_version: str
    cost_model_version: str
    price_basis_convention: str
    open_pnl_convention: str | None
    quantity: int
    entry_signal_at: datetime
    entry_fill_at: datetime
    exit_signal_at: datetime | None
    exit_fill_at: datetime | None
    mark_at: datetime | None
    entry_reference_open: Decimal
    exit_reference_open: Decimal | None
    mark_price: Decimal | None
    buy_fill_price: Decimal
    sell_fill_price: Decimal | None
    buy_commission: Decimal
    sell_commission: Decimal
    sell_tax: Decimal
    slippage_cost: Decimal
    reference_gross_pnl: Decimal
    fill_gross_pnl: Decimal
    invested_cost: Decimal
    net_pnl: Decimal | None
    net_return: Decimal | None
    floating_pnl: Decimal | None
    floating_return: Decimal | None
    holding_bars: int
    holding_days: int
    formula_version_id: str
    signal_series_id: str
    market_manifest_ids: tuple[str, ...]
    status_manifest_ids: tuple[str, ...]
    order_events: tuple[OrderEvent, ...]

    def __post_init__(self) -> None:
        _canonicalize_decimal_zeros(self)
        _validate_trade_sample(self)


def close_trade(
    *,
    entry: Decimal,
    exit: Decimal,
    quantity: int,
    cost_model: CostModel,
    symbol: str,
    entry_signal_at: datetime,
    entry_fill_at: datetime,
    exit_signal_at: datetime,
    exit_fill_at: datetime,
    holding_bars: int,
    formula_version_id: str,
    signal_series_id: str,
    market_manifest_ids: tuple[str, ...],
    status_manifest_ids: tuple[str, ...],
    order_events: tuple[OrderEvent, ...],
) -> TradeSample:
    """Close one fixed-lot trade with an exact reference-to-fill-to-net bridge."""

    _validate_realized_times(
        entry_signal_at=entry_signal_at,
        entry_fill_at=entry_fill_at,
        exit_signal_at=exit_signal_at,
        exit_fill_at=exit_fill_at,
    )
    buy = price_order(
        side="buy", reference_open=entry, quantity=quantity, model=cost_model
    )
    sell = price_order(
        side="sell", reference_open=exit, quantity=quantity, model=cost_model
    )
    reference_gross_pnl = round_money(
        (sell.reference_open - buy.reference_open) * quantity
    )
    slippage_cost = round_money(buy.slippage_cost + sell.slippage_cost)
    fill_gross_pnl = round_money(reference_gross_pnl - slippage_cost)
    net_pnl = round_money(
        fill_gross_pnl - buy.commission - sell.commission - sell.sell_tax
    )
    invested_cost = round_money(buy.fill_price * quantity + buy.commission)
    net_return = divide_decimal(net_pnl, invested_cost)

    return TradeSample(
        symbol=symbol,
        realized=True,
        sizing_version=SIZING_VERSION,
        cost_model_version=cost_model.version,
        price_basis_convention=PRICE_BASIS_CONVENTION,
        open_pnl_convention=None,
        quantity=quantity,
        entry_signal_at=entry_signal_at,
        entry_fill_at=entry_fill_at,
        exit_signal_at=exit_signal_at,
        exit_fill_at=exit_fill_at,
        mark_at=None,
        entry_reference_open=buy.reference_open,
        exit_reference_open=sell.reference_open,
        mark_price=None,
        buy_fill_price=buy.fill_price,
        sell_fill_price=sell.fill_price,
        buy_commission=buy.commission,
        sell_commission=sell.commission,
        sell_tax=sell.sell_tax,
        slippage_cost=slippage_cost,
        reference_gross_pnl=reference_gross_pnl,
        fill_gross_pnl=fill_gross_pnl,
        invested_cost=invested_cost,
        net_pnl=net_pnl,
        net_return=net_return,
        floating_pnl=None,
        floating_return=None,
        holding_bars=holding_bars,
        holding_days=_holding_days(entry_fill_at, exit_fill_at),
        formula_version_id=formula_version_id,
        signal_series_id=signal_series_id,
        market_manifest_ids=market_manifest_ids,
        status_manifest_ids=status_manifest_ids,
        order_events=order_events,
    )


def mark_open_trade(
    *,
    entry: Decimal,
    mark: Decimal,
    mark_at: datetime,
    quantity: int,
    cost_model: CostModel,
    symbol: str,
    entry_signal_at: datetime,
    entry_fill_at: datetime,
    holding_bars: int,
    formula_version_id: str,
    signal_series_id: str,
    market_manifest_ids: tuple[str, ...],
    status_manifest_ids: tuple[str, ...],
    order_events: tuple[OrderEvent, ...],
) -> TradeSample:
    """Mark an open position without hypothetical sell slippage, commission, or tax."""

    _validate_timestamp(mark_at, field_name="mark_at")
    _validate_timestamp(entry_fill_at, field_name="entry_fill_at")
    if mark_at < entry_fill_at:
        raise ValueError("mark_at cannot precede entry_fill_at")
    buy = price_order(
        side="buy", reference_open=entry, quantity=quantity, model=cost_model
    )
    mark_price = _normalize_mark_price(mark)
    reference_gross_pnl = round_money((mark_price - buy.reference_open) * quantity)
    slippage_cost = buy.slippage_cost
    fill_gross_pnl = round_money(reference_gross_pnl - slippage_cost)
    invested_cost = round_money(buy.fill_price * quantity + buy.commission)
    floating_pnl = round_money(fill_gross_pnl - buy.commission)
    floating_return = divide_decimal(floating_pnl, invested_cost)

    return TradeSample(
        symbol=symbol,
        realized=False,
        sizing_version=SIZING_VERSION,
        cost_model_version=cost_model.version,
        price_basis_convention=PRICE_BASIS_CONVENTION,
        open_pnl_convention=OPEN_PNL_CONVENTION,
        quantity=quantity,
        entry_signal_at=entry_signal_at,
        entry_fill_at=entry_fill_at,
        exit_signal_at=None,
        exit_fill_at=None,
        mark_at=mark_at,
        entry_reference_open=buy.reference_open,
        exit_reference_open=None,
        mark_price=mark_price,
        buy_fill_price=buy.fill_price,
        sell_fill_price=None,
        buy_commission=buy.commission,
        sell_commission=Decimal("0.00"),
        sell_tax=Decimal("0.00"),
        slippage_cost=slippage_cost,
        reference_gross_pnl=reference_gross_pnl,
        fill_gross_pnl=fill_gross_pnl,
        invested_cost=invested_cost,
        net_pnl=None,
        net_return=None,
        floating_pnl=floating_pnl,
        floating_return=floating_return,
        holding_bars=holding_bars,
        holding_days=_holding_days(entry_fill_at, mark_at),
        formula_version_id=formula_version_id,
        signal_series_id=signal_series_id,
        market_manifest_ids=market_manifest_ids,
        status_manifest_ids=status_manifest_ids,
        order_events=order_events,
    )


def calculate_win_rate(samples: Iterable[TradeSample]) -> RatioMetric:
    realized = tuple(sample for sample in samples if sample.realized)
    if not realized:
        return RatioMetric(value=None, reason="no_realized_samples", sample_count=0)
    winners = sum(1 for sample in realized if _required_net_pnl(sample) > 0)
    return RatioMetric(
        value=divide_decimal(Decimal(winners), Decimal(len(realized))),
        reason=None,
        sample_count=len(realized),
    )


def calculate_payoff_ratio(samples: Iterable[TradeSample]) -> RatioMetric:
    realized = tuple(sample for sample in samples if sample.realized)
    positive = tuple(
        value for sample in realized if (value := _required_net_return(sample)) > 0
    )
    negative = tuple(
        value for sample in realized if (value := _required_net_return(sample)) < 0
    )
    if not positive and not negative:
        reason = "no_positive_or_negative_returns"
    elif not positive:
        reason = "no_positive_returns"
    elif not negative:
        reason = "no_negative_returns"
    else:
        positive_mean = divide_decimal(
            sum(positive, Decimal("0")), Decimal(len(positive))
        )
        negative_mean = divide_decimal(
            sum(negative, Decimal("0")), Decimal(len(negative))
        )
        return RatioMetric(
            value=divide_decimal(positive_mean, abs(negative_mean)),
            reason=None,
            sample_count=len(realized),
        )
    return RatioMetric(value=None, reason=reason, sample_count=len(realized))


def _validate_trade_sample(sample: TradeSample) -> None:
    _validate_identity(sample.symbol, field_name="symbol")
    if type(sample.realized) is not bool:
        raise TypeError("realized must be a bool")
    if sample.sizing_version != SIZING_VERSION:
        raise ValueError(f"sizing_version must be {SIZING_VERSION}")
    if sample.cost_model_version != COST_MODEL_VERSION:
        raise ValueError(f"cost_model_version must be {COST_MODEL_VERSION}")
    if sample.price_basis_convention != PRICE_BASIS_CONVENTION:
        raise ValueError("price_basis_convention is not supported")
    if type(sample.quantity) is not int or sample.quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    if sample.quantity % 100 != 0:
        raise ValueError("quantity must use a 100-share lot")
    if type(sample.holding_bars) is not int or sample.holding_bars < 0:
        raise ValueError("holding_bars must be a nonnegative integer")
    if type(sample.holding_days) is not int or sample.holding_days < 0:
        raise ValueError("holding_days must be a nonnegative integer")
    _validate_positive_decimal(
        sample.entry_reference_open, field_name="entry_reference_open"
    )
    _validate_positive_decimal(sample.buy_fill_price, field_name="buy_fill_price")
    _validate_positive_decimal(sample.invested_cost, field_name="invested_cost")
    for field_name, value in (
        ("buy_commission", sample.buy_commission),
        ("sell_commission", sample.sell_commission),
        ("sell_tax", sample.sell_tax),
        ("slippage_cost", sample.slippage_cost),
    ):
        _validate_nonnegative_money(value, field_name=field_name)
    for field_name, value in (
        ("reference_gross_pnl", sample.reference_gross_pnl),
        ("fill_gross_pnl", sample.fill_gross_pnl),
    ):
        _validate_money(value, field_name=field_name)
    if sample.invested_cost != round_money(
        sample.buy_fill_price * sample.quantity + sample.buy_commission
    ):
        raise ValueError("invested_cost does not equal buy notional plus commission")
    for field_name, identity_value in (
        ("formula_version_id", sample.formula_version_id),
        ("signal_series_id", sample.signal_series_id),
    ):
        _validate_identity(identity_value, field_name=field_name)
    _validate_id_tuple(sample.market_manifest_ids, field_name="market_manifest_ids")
    _validate_id_tuple(sample.status_manifest_ids, field_name="status_manifest_ids")
    if type(sample.order_events) is not tuple or any(
        not isinstance(event, _ORDER_EVENT_TYPES) for event in sample.order_events
    ):
        raise TypeError("order_events must be a tuple of immutable order events")
    if not sample.order_events:
        raise ValueError("order_events must contain the full trade lifecycle")

    _validate_realized_times(
        entry_signal_at=sample.entry_signal_at,
        entry_fill_at=sample.entry_fill_at,
        exit_signal_at=sample.exit_signal_at,
        exit_fill_at=sample.exit_fill_at,
        allow_open=not sample.realized,
    )
    if sample.realized:
        if sample.open_pnl_convention is not None:
            raise ValueError("realized trade cannot use an open PnL convention")
        if sample.mark_at is not None or sample.mark_price is not None:
            raise ValueError("realized trade cannot contain a mark")
        if sample.exit_reference_open is None or sample.sell_fill_price is None:
            raise ValueError("realized trade requires exit prices")
        if sample.net_pnl is None or sample.net_return is None:
            raise ValueError("realized trade requires net results")
        assert sample.exit_fill_at is not None
        _validate_positive_decimal(
            sample.exit_reference_open, field_name="exit_reference_open"
        )
        _validate_positive_decimal(sample.sell_fill_price, field_name="sell_fill_price")
        _validate_money(sample.net_pnl, field_name="net_pnl")
        _validate_ratio(sample.net_return, field_name="net_return")
        if sample.floating_pnl is not None or sample.floating_return is not None:
            raise ValueError("realized trade cannot contain floating results")
        expected_holding_days = _holding_days(sample.entry_fill_at, sample.exit_fill_at)
        if sample.holding_days != expected_holding_days:
            raise ValueError("holding_days does not match fill timestamps")
        expected_reference = round_money(
            (sample.exit_reference_open - sample.entry_reference_open) * sample.quantity
        )
        expected_slippage = round_money(
            (sample.buy_fill_price - sample.entry_reference_open) * sample.quantity
            + (sample.exit_reference_open - sample.sell_fill_price) * sample.quantity
        )
        _validate_accounting_bridge(sample, expected_reference, expected_slippage)
        expected_net = round_money(
            sample.fill_gross_pnl
            - sample.buy_commission
            - sample.sell_commission
            - sample.sell_tax
        )
        if sample.net_pnl != expected_net:
            raise ValueError("net_pnl does not equal fill PnL less disclosed costs")
        if sample.net_return != divide_decimal(sample.net_pnl, sample.invested_cost):
            raise ValueError(
                "net_return does not equal net_pnl divided by invested_cost"
            )
    else:
        if sample.open_pnl_convention != OPEN_PNL_CONVENTION:
            raise ValueError("open trade must disclose its PnL convention")
        if sample.mark_at is None or sample.mark_price is None:
            raise ValueError("open trade requires a last-price mark")
        if sample.exit_reference_open is not None or sample.sell_fill_price is not None:
            raise ValueError("open trade cannot contain exit prices")
        if sample.net_pnl is not None or sample.net_return is not None:
            raise ValueError("open trade cannot contain realized net results")
        if sample.floating_pnl is None or sample.floating_return is None:
            raise ValueError("open trade requires floating results")
        _validate_positive_decimal(sample.mark_price, field_name="mark_price")
        _validate_money(sample.floating_pnl, field_name="floating_pnl")
        _validate_ratio(sample.floating_return, field_name="floating_return")
        if sample.sell_commission != 0 or sample.sell_tax != 0:
            raise ValueError("open trade cannot include hypothetical exit costs")
        assert sample.mark_at is not None
        _validate_timestamp(sample.mark_at, field_name="mark_at")
        if sample.mark_at < sample.entry_fill_at:
            raise ValueError("mark_at cannot precede entry_fill_at")
        expected_holding_days = _holding_days(sample.entry_fill_at, sample.mark_at)
        if sample.holding_days != expected_holding_days:
            raise ValueError("holding_days does not match mark timestamp")
        expected_reference = round_money(
            (sample.mark_price - sample.entry_reference_open) * sample.quantity
        )
        expected_slippage = round_money(
            (sample.buy_fill_price - sample.entry_reference_open) * sample.quantity
        )
        _validate_accounting_bridge(sample, expected_reference, expected_slippage)
        expected_floating = round_money(sample.fill_gross_pnl - sample.buy_commission)
        if sample.floating_pnl != expected_floating:
            raise ValueError(
                "floating_pnl does not match the disclosed open convention"
            )
        if sample.floating_return != divide_decimal(
            sample.floating_pnl, sample.invested_cost
        ):
            raise ValueError(
                "floating_return does not equal floating_pnl divided by invested_cost"
            )
    _validate_order_event_identity(sample)


def _validate_realized_times(
    *,
    entry_signal_at: datetime,
    entry_fill_at: datetime,
    exit_signal_at: datetime | None,
    exit_fill_at: datetime | None,
    allow_open: bool = False,
) -> None:
    _validate_timestamp(entry_signal_at, field_name="entry_signal_at")
    _validate_timestamp(entry_fill_at, field_name="entry_fill_at")
    if entry_fill_at < entry_signal_at:
        raise ValueError("entry_fill_at cannot precede entry_signal_at")
    if allow_open and exit_signal_at is None and exit_fill_at is None:
        return
    if exit_signal_at is None or exit_fill_at is None:
        raise ValueError("exit_signal_at and exit_fill_at must both be present")
    _validate_timestamp(exit_signal_at, field_name="exit_signal_at")
    _validate_timestamp(exit_fill_at, field_name="exit_fill_at")
    if exit_signal_at < entry_fill_at:
        raise ValueError("exit_signal_at cannot precede entry_fill_at")
    if exit_fill_at < exit_signal_at:
        raise ValueError("exit_fill_at cannot precede exit_signal_at")


def _validate_timestamp(value: datetime, *, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_identity(value: object, *, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")


def _validate_id_tuple(value: object, *, field_name: str) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    for item in value:
        _validate_identity(item, field_name=field_name)


def _validate_positive_decimal(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a positive finite Decimal")


def _validate_money(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")
    if value != round_money(value):
        raise ValueError(f"{field_name} must be rounded to fen")


def _validate_nonnegative_money(value: object, *, field_name: str) -> None:
    _validate_money(value, field_name=field_name)
    assert isinstance(value, Decimal)
    if value < 0:
        raise ValueError(f"{field_name} must be nonnegative")


def _validate_ratio(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")


def _validate_accounting_bridge(
    sample: TradeSample, expected_reference: Decimal, expected_slippage: Decimal
) -> None:
    if sample.reference_gross_pnl != expected_reference:
        raise ValueError("reference_gross_pnl does not match reference prices")
    if sample.slippage_cost != expected_slippage:
        raise ValueError("slippage_cost does not bridge reference and fill prices")
    if sample.fill_gross_pnl != round_money(
        sample.reference_gross_pnl - sample.slippage_cost
    ):
        raise ValueError("fill_gross_pnl does not equal reference PnL less slippage")


def _validate_order_event_identity(sample: TradeSample) -> None:
    held_entry: OrderFilled | None = None
    pending: OrderPending | None = None
    matched_buy = False
    matched_sell = False
    matched_mark = False
    range_ended = False
    last_at: datetime | None = None
    final_index = len(sample.order_events) - 1

    for index, event in enumerate(sample.order_events):
        at = _event_operation_time(event)
        if last_at is not None and at < last_at:
            raise ValueError("order_events must be replayed in chronological order")
        last_at = at
        if range_ended and not isinstance(event, OpenTradeMarked):
            raise ValueError("range-ended events may only be followed by an open mark")

        if isinstance(event, OrderPending):
            if pending is not None:
                raise ValueError("a new order cannot replace an active pending order")
            if event.side == "buy" and held_entry is not None:
                raise ValueError("buy pending order requires a flat position")
            if event.side == "sell" and held_entry is None:
                raise ValueError("sell pending order requires a held position")
            pending = event
            continue

        if isinstance(event, IgnoredSignal):
            _validate_ignored_signal_state(
                event, held_entry=held_entry, pending=pending
            )
            continue

        if isinstance(event, OrderCancelled):
            if pending is None or pending.side != event.side:
                raise ValueError("cancellation requires its active pending order")
            pending = None
            continue

        if isinstance(event, OrderBlocked):
            if pending is None or pending.side != event.side:
                raise ValueError("blocked event requires its active pending order")
            if event.at < pending.eligible_at:
                raise ValueError("blocked event cannot precede pending eligibility")
            continue

        if isinstance(event, OrderFilled):
            if pending is None or pending.side != event.side:
                raise ValueError("fill requires its active pending order")
            if (
                event.signal_at != pending.signal_at
                or event.filled_at < pending.eligible_at
            ):
                raise ValueError("fill does not match its active pending order")
            pending = None
            if event.side == "buy":
                if held_entry is not None or matched_buy:
                    raise ValueError("buy fill requires one flat sample lifecycle")
                _bind_buy_fill(sample, event)
                held_entry = event
                matched_buy = True
                continue
            if held_entry is None or matched_sell:
                raise ValueError("sell fill requires one held sample lifecycle")
            _bind_sell_fill(sample, event)
            held_entry = None
            matched_sell = True
            if index != final_index:
                raise ValueError("realized sell fill must be final")
            continue

        if isinstance(event, OrderUnfilled):
            if pending is None or pending.side != event.side:
                raise ValueError("unfilled event requires its active pending order")
            if (
                event.signal_at != pending.signal_at
                or event.eligible_at != pending.eligible_at
            ):
                raise ValueError(
                    "unfilled event does not match its active pending order"
                )
            pending = None
            range_ended = True
            continue

        if not isinstance(event, OpenTradeMarked):
            raise TypeError("unsupported order event")
        if index != final_index:
            raise ValueError("terminal open mark must be final")
        if pending is not None or held_entry is None:
            raise ValueError("open mark requires a held position and no pending order")
        _bind_open_mark(sample, event, held_entry=held_entry)
        matched_mark = True

    if sample.realized:
        if not matched_buy:
            raise ValueError("order_events must contain exactly one matching buy fill")
        if not matched_sell:
            raise ValueError("order_events must contain exactly one matching sell fill")
        if matched_mark or held_entry is not None or pending is not None:
            raise ValueError(
                "realized order_events must end flat at the matching sell fill"
            )
        return
    if not matched_buy:
        raise ValueError("order_events must contain exactly one matching buy fill")
    if matched_sell:
        raise ValueError("open order_events cannot contain a sell fill")
    if not matched_mark:
        raise ValueError("order_events must contain exactly one matching open mark")
    if held_entry is None or pending is not None:
        raise ValueError("open order_events must end held at the matching open mark")


def _event_operation_time(event: OrderEvent) -> datetime:
    if isinstance(event, OrderPending):
        return event.signal_at
    if isinstance(event, IgnoredSignal | OrderCancelled | OrderBlocked):
        return event.at
    if isinstance(event, OrderFilled):
        return event.filled_at
    if isinstance(event, OrderUnfilled):
        return event.ended_at
    return event.mark_at


def _validate_ignored_signal_state(
    event: IgnoredSignal,
    *,
    held_entry: OrderFilled | None,
    pending: OrderPending | None,
) -> None:
    if event.reason is IgnoredSignalReason.CONFLICTING_SIGNALS:
        return
    if event.reason is IgnoredSignalReason.ALREADY_HOLDING:
        if held_entry is None or pending is not None:
            raise ValueError("already_holding signal requires an idle held position")
        return
    if event.reason is IgnoredSignalReason.NOT_HOLDING:
        if held_entry is not None or pending is not None:
            raise ValueError("not_holding signal requires an idle flat position")
        return
    if pending is None or event.signal is None or event.signal.value != pending.side:
        raise ValueError("same-side ignored signal requires its active pending order")


def _bind_buy_fill(sample: TradeSample, event: OrderFilled) -> None:
    if (
        event.signal_at != sample.entry_signal_at
        or event.filled_at != sample.entry_fill_at
        or event.price != sample.entry_reference_open
        or event.quantity != sample.quantity
    ):
        raise ValueError("buy fill event does not match the trade reference identity")


def _bind_sell_fill(sample: TradeSample, event: OrderFilled) -> None:
    if not sample.realized:
        raise ValueError("open order_events cannot contain a sell fill")
    assert sample.exit_signal_at is not None
    assert sample.exit_fill_at is not None
    assert sample.exit_reference_open is not None
    if (
        event.signal_at != sample.exit_signal_at
        or event.filled_at != sample.exit_fill_at
        or event.price != sample.exit_reference_open
        or event.quantity != sample.quantity
    ):
        raise ValueError("sell fill event does not match the trade reference identity")


def _bind_open_mark(
    sample: TradeSample, event: OpenTradeMarked, *, held_entry: OrderFilled
) -> None:
    if sample.realized:
        raise ValueError("realized order_events cannot contain an open mark")
    assert sample.mark_at is not None
    assert sample.mark_price is not None
    if (
        event.entry_at != held_entry.filled_at
        or event.entry_at != sample.entry_fill_at
        or event.entry_price != held_entry.price
        or event.entry_price != sample.entry_reference_open
        or event.quantity != held_entry.quantity
        or event.quantity != sample.quantity
        or event.mark_at != sample.mark_at
        or event.mark_price != sample.mark_price
        or event.floating_pnl != sample.reference_gross_pnl
    ):
        raise ValueError("open mark event does not match the trade reference identity")


def _holding_days(start: datetime, end: datetime) -> int:
    return (end.astimezone(SHANGHAI).date() - start.astimezone(SHANGHAI).date()).days


def _canonicalize_decimal_zeros(sample: TradeSample) -> None:
    for field_name in (
        "entry_reference_open",
        "exit_reference_open",
        "mark_price",
        "buy_fill_price",
        "sell_fill_price",
        "buy_commission",
        "sell_commission",
        "sell_tax",
        "slippage_cost",
        "reference_gross_pnl",
        "fill_gross_pnl",
        "invested_cost",
        "net_pnl",
        "net_return",
        "floating_pnl",
        "floating_return",
    ):
        value = getattr(sample, field_name)
        if isinstance(value, Decimal) and value.is_zero():
            object.__setattr__(sample, field_name, value.copy_abs())


def _decimal_json_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized.copy_abs() if normalized.is_zero() else normalized, "f")


def _normalize_mark_price(value: Decimal) -> Decimal:
    zero_cost_model = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )
    return price_order(
        side="buy", reference_open=value, quantity=100, model=zero_cost_model
    ).reference_open


def _required_net_pnl(sample: TradeSample) -> Decimal:
    if sample.net_pnl is None:
        raise ValueError("realized trade is missing net_pnl")
    return sample.net_pnl


def _required_net_return(sample: TradeSample) -> Decimal:
    if sample.net_return is None:
        raise ValueError("realized trade is missing net_return")
    return sample.net_return
