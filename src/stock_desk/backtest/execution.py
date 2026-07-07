"""Deterministic signal-close to executable-open simulation."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable

from stock_desk.backtest.constraints import (
    ConstraintDecision,
    SHANGHAI,
    assess_execution,
)
from stock_desk.backtest.events import OrderBlocked, OrderEvent
from stock_desk.backtest.state_machine import SymbolStateMachine
from stock_desk.market.execution_status import ExecutionEligibility
from stock_desk.market.execution_status import ExecutionStatusSnapshot
from stock_desk.market.types import Period


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SignalBar:
    timestamp: datetime
    buy: bool | None = None
    sell: bool | None = None

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "signal timestamp")


@dataclass(frozen=True, slots=True)
class FillCandidate:
    timestamp: datetime
    open_price: Decimal | None
    eligibility: ExecutionEligibility | None

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "candidate timestamp")
        if self.open_price is not None and (
            not self.open_price.is_finite() or self.open_price <= 0
        ):
            raise ValueError("candidate open must be a positive finite Decimal")


@dataclass(frozen=True, slots=True)
class ReferenceOpen:
    timestamp: datetime
    price: Decimal

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "reference-open timestamp")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("reference open must be a positive finite Decimal")


def candidates_from_status(
    status: ExecutionStatusSnapshot,
    *,
    reference_opens: tuple[ReferenceOpen, ...],
) -> tuple[FillCandidate, ...]:
    """Union frozen fill opens with status opportunities by exact timestamp."""
    if status.query.period in {Period.DAY, Period.WEEK}:
        opens_by_day: dict[object, Decimal] = {}
        for item in reference_opens:
            day = item.timestamp.astimezone(SHANGHAI).date()
            if day in opens_by_day:
                raise ValueError("reference-open trading days must be unique")
            opens_by_day[day] = item.price
        eligibility_days = {item.trading_day for item in status.eligibility}
        candidates = [
            FillCandidate(
                timestamp=item.timestamp,
                open_price=opens_by_day.get(item.trading_day),
                eligibility=item,
            )
            for item in status.eligibility
        ]
        candidates.extend(
            FillCandidate(
                timestamp=item.timestamp,
                open_price=item.price,
                eligibility=None,
            )
            for item in reference_opens
            if item.timestamp.astimezone(SHANGHAI).date() not in eligibility_days
        )
        return tuple(sorted(candidates, key=lambda item: item.timestamp))
    opens: dict[datetime, Decimal] = {}
    for item in reference_opens:
        if item.timestamp in opens:
            raise ValueError("reference-open timestamps must be unique")
        opens[item.timestamp] = item.price
    eligibility = {item.timestamp: item for item in status.eligibility}
    timestamps = sorted(opens.keys() | eligibility.keys())
    return tuple(
        FillCandidate(
            timestamp=timestamp,
            open_price=opens.get(timestamp),
            eligibility=eligibility.get(timestamp),
        )
        for timestamp in timestamps
    )


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    period: Period
    signals: tuple[SignalBar, ...]
    candidates: tuple[FillCandidate, ...]
    quantity: int = 1_000
    ended_at: datetime | None = None
    mark_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.quantity % 100 != 0:
            raise ValueError("quantity must use a positive 100-share lot")
        if tuple(sorted(self.signals, key=lambda item: item.timestamp)) != self.signals:
            raise ValueError("signals must be ordered")
        if (
            tuple(sorted(self.candidates, key=lambda item: item.timestamp))
            != self.candidates
        ):
            raise ValueError("fill candidates must be ordered")
        if self.ended_at is not None:
            _require_aware(self.ended_at, "ended_at")
        if self.mark_price is not None and (
            not self.mark_price.is_finite() or self.mark_price <= 0
        ):
            raise ValueError("mark_price must be a positive finite Decimal")
        if self.mark_price is not None and self.ended_at is None:
            raise ValueError("mark_price requires ended_at")


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    timestamp: datetime
    price: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionTrade:
    entry: ExecutionFill
    exit: ExecutionFill | None = None


@dataclass(frozen=True, slots=True)
class ExecutionFailure:
    reason: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    trades: tuple[ExecutionTrade, ...]
    order_events: tuple[OrderEvent, ...]
    blocked_events: tuple[OrderBlocked, ...]
    failure: ExecutionFailure | None


def _eligibility_key(period: Period, value: datetime) -> datetime:
    local = value.astimezone(SHANGHAI)
    if period is Period.DAY:
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is Period.WEEK:
        monday = local - timedelta(days=local.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return value


def _eligible_at(
    request: ExecutionRequest,
    signal_at: datetime,
    candidate_keys: tuple[datetime, ...],
) -> datetime:
    index = bisect_right(candidate_keys, _eligibility_key(request.period, signal_at))
    return (
        request.candidates[index].timestamp
        if index < len(request.candidates)
        else signal_at + timedelta(microseconds=1)
    )


def _timeline(
    request: ExecutionRequest,
) -> Iterable[tuple[datetime, int, SignalBar | FillCandidate]]:
    # Opens precede closes at an identical timestamp, preventing same-bar fills.
    entries: list[tuple[datetime, int, SignalBar | FillCandidate]] = [
        *((item.timestamp, 0, item) for item in request.candidates),
        *((item.timestamp, 1, item) for item in request.signals),
    ]
    return sorted(entries, key=lambda item: (item[0], item[1]))


class ExecutionEngine:
    def run(self, request: ExecutionRequest) -> ExecutionResult:
        machine = SymbolStateMachine()
        trades: list[ExecutionTrade] = []
        failure: ExecutionFailure | None = None
        candidate_keys = tuple(
            _eligibility_key(request.period, item.timestamp)
            for item in request.candidates
        )

        for at, _ordinal, item in _timeline(request):
            if isinstance(item, SignalBar):
                machine.on_signals(
                    buy=item.buy,
                    sell=item.sell,
                    at=item.timestamp,
                    eligible_at=_eligible_at(request, item.timestamp, candidate_keys),
                )
                continue

            pending = machine.pending_order
            if pending is None or item.timestamp < pending.eligible_at:
                continue
            position_entry_at = (
                machine.position.entry_at if machine.position is not None else None
            )
            decision = assess_execution(
                side=pending.side,
                at=item.timestamp,
                eligibility=item.eligibility,
                position_entry_at=position_entry_at,
            )
            if decision.decision is ConstraintDecision.DATA_INSUFFICIENT:
                failure = ExecutionFailure(
                    reason=decision.reason or "data_insufficient_execution_status",
                    timestamp=item.timestamp,
                )
                break
            if decision.decision is ConstraintDecision.BLOCKED:
                machine.block_pending(
                    at=item.timestamp,
                    reason=decision.reason or "not_executable",
                )
                continue
            if item.open_price is None:
                failure = ExecutionFailure(
                    reason="data_insufficient_fill_open",
                    timestamp=item.timestamp,
                )
                break
            side = pending.side
            machine.fill_pending(
                at=item.timestamp,
                price=item.open_price,
                quantity=request.quantity if side == "buy" else None,
            )
            fill = ExecutionFill(timestamp=item.timestamp, price=item.open_price)
            if side == "buy":
                trades.append(ExecutionTrade(entry=fill))
            elif trades:
                trades[-1] = replace(trades[-1], exit=fill)

        if failure is None and request.ended_at is not None:
            machine.finish_range(
                at=request.ended_at,
                mark_price=request.mark_price if machine.position is not None else None,
            )

        events = machine.events
        return ExecutionResult(
            trades=tuple(trades),
            order_events=events,
            blocked_events=tuple(
                item for item in events if isinstance(item, OrderBlocked)
            ),
            failure=failure,
        )


__all__ = [
    "ExecutionEngine",
    "ExecutionFailure",
    "ExecutionFill",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionTrade",
    "FillCandidate",
    "ReferenceOpen",
    "SignalBar",
    "candidates_from_status",
]
