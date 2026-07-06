from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import re
from typing import Literal, TypeAlias


OrderSide: TypeAlias = Literal["buy", "sell"]
MAX_REASON_CODE_LENGTH = 64
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class SignalCode(StrEnum):
    BUY = "buy"
    SELL = "sell"


class IgnoredSignalReason(StrEnum):
    ALREADY_HOLDING = "already_holding"
    NOT_HOLDING = "not_holding"
    SAME_SIDE_ORDER_PENDING = "same_side_order_pending"
    CONFLICTING_SIGNALS = "conflicting_signals"


class CancellationReason(StrEnum):
    OPPOSITE_SIGNAL = "opposite_signal"


@dataclass(frozen=True, slots=True)
class OrderPending:
    side: OrderSide
    signal_at: datetime
    eligible_at: datetime

    def __post_init__(self) -> None:
        _validate_side(self.side)
        _validate_timestamp(self.signal_at, field_name="signal_at")
        _validate_timestamp(self.eligible_at, field_name="eligible_at")
        if self.eligible_at < self.signal_at:
            raise ValueError("eligible_at cannot precede signal_at")


@dataclass(frozen=True, slots=True)
class IgnoredSignal:
    reason: IgnoredSignalReason
    signal: SignalCode | None
    at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.reason, IgnoredSignalReason):
            raise TypeError("reason must be an IgnoredSignalReason")
        if self.signal is not None and not isinstance(self.signal, SignalCode):
            raise TypeError("signal must be a SignalCode or None")
        if self.reason is IgnoredSignalReason.CONFLICTING_SIGNALS:
            if self.signal is not None:
                raise ValueError("conflicting signal event must use signal None")
        elif self.signal is None:
            raise ValueError("non-conflicting ignored event requires a signal")
        _validate_timestamp(self.at, field_name="at")


@dataclass(frozen=True, slots=True)
class OrderCancelled:
    side: OrderSide
    reason: CancellationReason
    at: datetime

    def __post_init__(self) -> None:
        _validate_side(self.side)
        if not isinstance(self.reason, CancellationReason):
            raise TypeError("reason must be a CancellationReason")
        _validate_timestamp(self.at, field_name="at")


@dataclass(frozen=True, slots=True)
class OrderBlocked:
    side: OrderSide
    at: datetime
    reason: str

    def __post_init__(self) -> None:
        _validate_side(self.side)
        _validate_timestamp(self.at, field_name="at")
        validate_reason_code(self.reason)


@dataclass(frozen=True, slots=True)
class OrderFilled:
    side: OrderSide
    signal_at: datetime
    filled_at: datetime
    price: Decimal
    quantity: int

    def __post_init__(self) -> None:
        _validate_side(self.side)
        _validate_timestamp(self.signal_at, field_name="signal_at")
        _validate_timestamp(self.filled_at, field_name="filled_at")
        if self.filled_at < self.signal_at:
            raise ValueError("filled_at cannot precede signal_at")
        _validate_price(self.price, field_name="price")
        _validate_quantity(self.quantity)


@dataclass(frozen=True, slots=True)
class OrderUnfilled:
    side: OrderSide
    signal_at: datetime
    eligible_at: datetime
    ended_at: datetime
    reason: Literal["range_ended_unfilled"] = "range_ended_unfilled"

    def __post_init__(self) -> None:
        _validate_side(self.side)
        _validate_timestamp(self.signal_at, field_name="signal_at")
        _validate_timestamp(self.eligible_at, field_name="eligible_at")
        _validate_timestamp(self.ended_at, field_name="ended_at")
        if self.eligible_at < self.signal_at:
            raise ValueError("eligible_at cannot precede signal_at")
        if self.ended_at < self.signal_at:
            raise ValueError("ended_at cannot precede signal_at")
        if self.reason != "range_ended_unfilled":
            raise ValueError("reason must be range_ended_unfilled")


@dataclass(frozen=True, slots=True)
class OpenTradeMarked:
    entry_at: datetime
    entry_price: Decimal
    quantity: int
    mark_at: datetime
    mark_price: Decimal
    floating_pnl: Decimal

    def __post_init__(self) -> None:
        _validate_timestamp(self.entry_at, field_name="entry_at")
        _validate_timestamp(self.mark_at, field_name="mark_at")
        if self.mark_at < self.entry_at:
            raise ValueError("mark_at cannot precede entry_at")
        _validate_price(self.entry_price, field_name="entry_price")
        _validate_price(self.mark_price, field_name="mark_price")
        _validate_quantity(self.quantity)
        if (
            not isinstance(self.floating_pnl, Decimal)
            or not self.floating_pnl.is_finite()
        ):
            raise ValueError("floating_pnl must be a finite Decimal")


OrderEvent: TypeAlias = (
    OrderPending
    | IgnoredSignal
    | OrderCancelled
    | OrderBlocked
    | OrderFilled
    | OrderUnfilled
    | OpenTradeMarked
)


def validate_reason_code(value: object) -> None:
    if type(value) is not str:
        raise TypeError("reason must be a string code")
    if (
        len(value) > MAX_REASON_CODE_LENGTH
        or _REASON_CODE_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            "reason must be a nonblank lowercase snake_case code of at most 64 characters"
        )


def _validate_side(value: object) -> None:
    if type(value) is not str or value not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")


def _validate_timestamp(value: object, *, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_price(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a positive finite Decimal")


def _validate_quantity(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("quantity must be a positive integer")
