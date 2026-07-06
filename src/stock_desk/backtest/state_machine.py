from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

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
    OrderSide,
    OrderUnfilled,
    SignalCode,
    validate_reason_code,
)


Signal = SignalCode


class PositionState(StrEnum):
    FLAT = "flat"
    PENDING_BUY = "pending_buy"
    HELD = "held"
    PENDING_SELL = "pending_sell"


@dataclass(frozen=True, slots=True)
class PendingOrder:
    side: OrderSide
    signal_at: datetime
    eligible_at: datetime


@dataclass(frozen=True, slots=True)
class PositionLot:
    entry_at: datetime
    entry_price: Decimal
    quantity: int


class SymbolStateMachine:
    """Pure, auditable order state for one symbol and one backtest range."""

    def __init__(self) -> None:
        self._state = PositionState.FLAT
        self._pending_order: PendingOrder | None = None
        self._position: PositionLot | None = None
        self._events: list[OrderEvent] = []
        self._is_terminal = False
        self._last_processed_at: datetime | None = None

    @property
    def state(self) -> PositionState:
        return self._state

    @property
    def pending_order(self) -> PendingOrder | None:
        return self._pending_order

    @property
    def position(self) -> PositionLot | None:
        return self._position

    @property
    def events(self) -> tuple[OrderEvent, ...]:
        return tuple(self._events)

    @property
    def is_terminal(self) -> bool:
        return self._is_terminal

    @property
    def last_processed_at(self) -> datetime | None:
        return self._last_processed_at

    @property
    def holding_count(self) -> int:
        return int(self._position is not None)

    @property
    def pending_count(self) -> int:
        return int(self._pending_order is not None)

    def on_signals(
        self,
        *,
        buy: bool | None,
        sell: bool | None,
        at: datetime,
        eligible_at: datetime | None = None,
    ) -> list[OrderEvent]:
        _validate_signal_flag(buy, field_name="buy")
        _validate_signal_flag(sell, field_name="sell")
        self._require_active()
        self._validate_operation_time(at)
        if buy is True and sell is True:
            self._advance_clock(at)
            return self._record(
                [
                    IgnoredSignal(
                        reason=IgnoredSignalReason.CONFLICTING_SIGNALS,
                        signal=None,
                        at=at,
                    )
                ]
            )
        if buy is True:
            return self.on_signal(Signal.BUY, at=at, eligible_at=eligible_at)
        if sell is True:
            return self.on_signal(Signal.SELL, at=at, eligible_at=eligible_at)
        self._advance_clock(at)
        return []

    def on_signal(
        self,
        signal: Signal | None,
        *,
        at: datetime,
        eligible_at: datetime | None = None,
    ) -> list[OrderEvent]:
        self._require_active()
        self._validate_operation_time(at)
        if signal is None:
            self._advance_clock(at)
            return []
        if not isinstance(signal, Signal):
            raise TypeError("signal must be a Signal or None")
        first_eligible_at = at if eligible_at is None else eligible_at
        _validate_timestamp(first_eligible_at, field_name="eligible_at")
        if first_eligible_at < at:
            raise ValueError("eligible_at cannot precede signal timestamp")
        self._advance_clock(at)

        if self._state is PositionState.FLAT:
            if signal is Signal.SELL:
                return self._ignored(IgnoredSignalReason.NOT_HOLDING, signal, at)
            return self._open_pending("buy", at, first_eligible_at)

        if self._state is PositionState.PENDING_BUY:
            if signal is Signal.BUY:
                return self._ignored(
                    IgnoredSignalReason.SAME_SIDE_ORDER_PENDING, signal, at
                )
            return self._cancel_pending(at=at, next_state=PositionState.FLAT)

        if self._state is PositionState.HELD:
            if signal is Signal.BUY:
                return self._ignored(IgnoredSignalReason.ALREADY_HOLDING, signal, at)
            return self._open_pending("sell", at, first_eligible_at)

        if signal is Signal.SELL:
            return self._ignored(
                IgnoredSignalReason.SAME_SIDE_ORDER_PENDING, signal, at
            )
        return self._cancel_pending(at=at, next_state=PositionState.HELD)

    def block_pending(self, *, at: datetime, reason: str) -> list[OrderEvent]:
        validate_reason_code(reason)
        self._require_active()
        pending = self._require_pending()
        self._validate_operation_time(at)
        if at < pending.eligible_at:
            raise ValueError("blocked timestamp cannot precede order eligibility")
        self._advance_clock(at)
        return self._record([OrderBlocked(side=pending.side, at=at, reason=reason)])

    def fill_pending(
        self,
        *,
        at: datetime,
        price: Decimal,
        quantity: int | None = None,
    ) -> list[OrderEvent]:
        self._require_active()
        pending = self._require_pending()
        self._validate_operation_time(at)
        if at < pending.eligible_at:
            raise ValueError("fill timestamp cannot precede order eligibility")
        _validate_price(price, field_name="price")

        if pending.side == "buy":
            if type(quantity) is not int or quantity <= 0:
                raise ValueError("buy fill quantity must be a positive integer")
            filled_quantity = quantity
            self._advance_clock(at)
            self._position = PositionLot(
                entry_at=at,
                entry_price=price,
                quantity=filled_quantity,
            )
            self._state = PositionState.HELD
        else:
            if self._position is None:
                raise RuntimeError("pending sell requires a held position")
            filled_quantity = self._position.quantity
            if quantity is not None and (
                type(quantity) is not int or quantity != filled_quantity
            ):
                raise ValueError("sell fill quantity must equal the held quantity")
            self._advance_clock(at)
            self._position = None
            self._state = PositionState.FLAT

        self._pending_order = None
        return self._record(
            [
                OrderFilled(
                    side=pending.side,
                    signal_at=pending.signal_at,
                    filled_at=at,
                    price=price,
                    quantity=filled_quantity,
                )
            ]
        )

    def finish_range(
        self, *, at: datetime, mark_price: Decimal | None = None
    ) -> list[OrderEvent]:
        self._require_active()
        self._validate_operation_time(at)
        pending = self._pending_order
        position = self._position
        if position is not None:
            if mark_price is None:
                raise ValueError("mark_price is required for a held position")
            _validate_price(mark_price, field_name="mark_price")
        elif mark_price is not None:
            raise ValueError("mark_price requires a held position")
        self._advance_clock(at)

        terminal_events: list[OrderEvent] = []
        if pending is not None:
            terminal_events.append(
                OrderUnfilled(
                    side=pending.side,
                    signal_at=pending.signal_at,
                    eligible_at=pending.eligible_at,
                    ended_at=at,
                )
            )
            self._pending_order = None

        if position is not None:
            assert mark_price is not None
            terminal_events.append(
                OpenTradeMarked(
                    entry_at=position.entry_at,
                    entry_price=position.entry_price,
                    quantity=position.quantity,
                    mark_at=at,
                    mark_price=mark_price,
                    floating_pnl=(mark_price - position.entry_price)
                    * position.quantity,
                )
            )
            self._state = PositionState.HELD
        else:
            self._state = PositionState.FLAT
        self._is_terminal = True
        return self._record(terminal_events)

    def _open_pending(
        self, side: OrderSide, signal_at: datetime, eligible_at: datetime
    ) -> list[OrderEvent]:
        pending = PendingOrder(
            side=side,
            signal_at=signal_at,
            eligible_at=eligible_at,
        )
        self._pending_order = pending
        self._state = (
            PositionState.PENDING_BUY if side == "buy" else PositionState.PENDING_SELL
        )
        return self._record(
            [
                OrderPending(
                    side=side,
                    signal_at=signal_at,
                    eligible_at=eligible_at,
                )
            ]
        )

    def _cancel_pending(
        self, *, at: datetime, next_state: PositionState
    ) -> list[OrderEvent]:
        pending = self._require_pending()
        self._pending_order = None
        self._state = next_state
        return self._record(
            [
                OrderCancelled(
                    side=pending.side,
                    reason=CancellationReason.OPPOSITE_SIGNAL,
                    at=at,
                )
            ]
        )

    def _ignored(
        self, reason: IgnoredSignalReason, signal: Signal, at: datetime
    ) -> list[OrderEvent]:
        return self._record([IgnoredSignal(reason=reason, signal=signal, at=at)])

    def _require_active(self) -> None:
        if self._is_terminal:
            raise RuntimeError("state machine is terminal")

    def _require_pending(self) -> PendingOrder:
        if self._pending_order is None:
            raise RuntimeError("operation requires a pending order")
        return self._pending_order

    def _validate_operation_time(self, at: datetime) -> None:
        _validate_timestamp(at, field_name="at")
        if self._last_processed_at is not None and at < self._last_processed_at:
            raise ValueError("operations must be processed in chronological order")

    def _advance_clock(self, at: datetime) -> None:
        self._last_processed_at = at

    def _record(self, events: list[OrderEvent]) -> list[OrderEvent]:
        self._events.extend(events)
        return events


def _validate_timestamp(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_price(value: Decimal, *, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a positive finite Decimal")


def _validate_signal_flag(value: object, *, field_name: str) -> None:
    if value is not None and type(value) is not bool:
        raise TypeError(f"{field_name} must be bool or None")
