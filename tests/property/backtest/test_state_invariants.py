from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import given, strategies as st
import pytest

from stock_desk.backtest.events import (
    IgnoredSignal,
    OpenTradeMarked,
    OrderBlocked,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderPending,
    OrderUnfilled,
)
from stock_desk.backtest.state_machine import PositionState, SymbolStateMachine


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
SignalPair = tuple[bool | None, bool | None]


SIGNAL_PAIRS = st.tuples(
    st.one_of(st.none(), st.booleans()),
    st.one_of(st.none(), st.booleans()),
)


@given(signals=st.lists(SIGNAL_PAIRS, min_size=1, max_size=200))
def test_generated_signal_sequences_never_create_impossible_state(
    signals: list[SignalPair],
) -> None:
    machine = SymbolStateMachine()

    for index, (buy, sell) in enumerate(signals):
        machine.on_signals(
            buy=buy,
            sell=sell,
            at=START + timedelta(minutes=index),
        )
        _assert_state_invariants(machine)


@given(
    duplicate_count=st.integers(min_value=1, max_value=100),
    block_reasons=st.lists(
        st.sampled_from(["suspended", "limit_up", "calendar_closed"]),
        min_size=0,
        max_size=100,
    ),
)
def test_duplicate_and_blocked_attempts_never_reset_pending_eligibility(
    duplicate_count: int, block_reasons: list[str]
) -> None:
    machine = SymbolStateMachine()
    machine.on_signals(buy=True, sell=False, at=START)
    original = machine.pending_order
    assert original is not None

    minute = 1
    for _ in range(duplicate_count):
        machine.on_signals(
            buy=True,
            sell=False,
            at=START + timedelta(minutes=minute),
        )
        minute += 1
        assert machine.pending_order is original
    for reason in block_reasons:
        events = machine.block_pending(
            at=START + timedelta(minutes=minute), reason=reason
        )
        minute += 1
        event = events[0]
        assert isinstance(event, OrderBlocked)
        assert events == [OrderBlocked(side="buy", at=event.at, reason=reason)]
        assert machine.pending_order is original

    pending = machine.pending_order
    assert pending is not None
    assert pending.eligible_at == START
    _assert_state_invariants(machine)


@given(
    actions=st.lists(
        st.sampled_from(
            [
                "buy",
                "sell",
                "conflict",
                "none",
                "block",
                "fill",
            ]
        ),
        min_size=1,
        max_size=200,
    )
)
def test_generated_execution_sequences_preserve_single_position_and_terminal_state(
    actions: list[str],
) -> None:
    machine = SymbolStateMachine()

    for index, action in enumerate(actions, start=1):
        at = START + timedelta(minutes=index)
        if action == "buy":
            machine.on_signals(buy=True, sell=False, at=at)
        elif action == "sell":
            machine.on_signals(buy=False, sell=True, at=at)
        elif action == "conflict":
            machine.on_signals(buy=True, sell=True, at=at)
        elif action == "none":
            machine.on_signals(buy=None, sell=None, at=at)
        elif action == "block" and machine.pending_order is not None:
            machine.block_pending(at=at, reason="generated_block")
        elif action == "fill" and machine.pending_order is not None:
            quantity = 100 if machine.state is PositionState.PENDING_BUY else None
            machine.fill_pending(at=at, price=Decimal("10.00"), quantity=quantity)
        _assert_state_invariants(machine)

    terminal_events = machine.finish_range(
        at=START + timedelta(minutes=len(actions) + 1),
        mark_price=Decimal("10.25") if machine.holding_count else None,
    )

    assert machine.is_terminal
    assert machine.pending_count == 0
    assert machine.state in {PositionState.FLAT, PositionState.HELD}
    assert sum(isinstance(event, OrderUnfilled) for event in terminal_events) <= 1
    _assert_state_invariants(machine)


@given(
    data=st.data(),
    operation=st.sampled_from(["signal", "block", "fill", "finish"]),
    later_minutes=st.integers(min_value=1, max_value=100_000),
)
def test_generated_backwards_operations_are_rejected_without_state_change(
    data: st.DataObject, operation: str, later_minutes: int
) -> None:
    later = START + timedelta(minutes=later_minutes)
    backwards_by = data.draw(st.integers(min_value=1, max_value=later_minutes))
    earlier = later - timedelta(minutes=backwards_by)
    machine = SymbolStateMachine()
    if operation in {"block", "fill"}:
        machine.on_signals(buy=True, sell=False, at=START)
        machine.block_pending(at=later, reason="generated_block")
    elif operation == "finish":
        machine.on_signals(buy=True, sell=False, at=START)
        machine.on_signals(buy=None, sell=None, at=later)
    else:
        machine.on_signals(buy=None, sell=None, at=later)
    before_events = machine.events
    before_state = machine.state
    before_pending = machine.pending_order

    with pytest.raises(ValueError, match="chronological"):
        if operation == "block":
            machine.block_pending(at=earlier, reason="generated_block")
        elif operation == "fill":
            machine.fill_pending(at=earlier, price=Decimal("10.00"), quantity=100)
        elif operation == "finish":
            machine.finish_range(at=earlier)
        else:
            machine.on_signals(buy=True, sell=False, at=earlier)

    assert machine.last_processed_at == later
    assert machine.events == before_events
    assert machine.state is before_state
    assert machine.pending_order is before_pending
    _assert_state_invariants(machine)


@given(
    malformed_operation=st.sampled_from(
        ["buy_flag", "sell_flag", "block_reason", "finish_mark"]
    ),
    malformed_value=st.sampled_from([1, "", " ", "UPPER_CASE", "a-b", "a" * 65]),
)
def test_generated_malformed_input_preserves_complete_machine_snapshot(
    malformed_operation: str, malformed_value: object
) -> None:
    machine = SymbolStateMachine()
    if malformed_operation == "block_reason":
        machine.on_signals(buy=True, sell=False, at=START)
    before = _machine_snapshot(machine)

    with pytest.raises((TypeError, ValueError)):
        if malformed_operation == "buy_flag":
            machine.on_signals(
                buy=malformed_value,  # type: ignore[arg-type]
                sell=False,
                at=START + timedelta(minutes=1),
            )
        elif malformed_operation == "sell_flag":
            machine.on_signals(
                buy=False,
                sell=malformed_value,  # type: ignore[arg-type]
                at=START + timedelta(minutes=1),
            )
        elif malformed_operation == "block_reason":
            machine.block_pending(
                at=START + timedelta(minutes=1),
                reason=malformed_value,  # type: ignore[arg-type]
            )
        else:
            machine.finish_range(at=START, mark_price=Decimal("10.00"))

    assert _machine_snapshot(machine) == before
    _assert_state_invariants(machine)


def _assert_state_invariants(machine: SymbolStateMachine) -> None:
    assert machine.holding_count in {0, 1}
    assert machine.pending_count in {0, 1}
    assert not (
        machine.holding_count == 0 and machine.state is PositionState.PENDING_SELL
    )
    assert not (
        machine.holding_count == 1 and machine.state is PositionState.PENDING_BUY
    )
    assert (machine.pending_order is None) == (
        machine.state not in {PositionState.PENDING_BUY, PositionState.PENDING_SELL}
    )
    assert (machine.position is None) == (
        machine.state in {PositionState.FLAT, PositionState.PENDING_BUY}
    )
    if machine.pending_order is not None:
        expected_side = "buy" if machine.state is PositionState.PENDING_BUY else "sell"
        assert machine.pending_order.side == expected_side
    if machine.position is not None:
        assert type(machine.position.quantity) is int
        assert machine.position.quantity > 0
    audit_times = [_event_timestamp(event) for event in machine.events]
    assert audit_times == sorted(audit_times)


def _event_timestamp(event: OrderEvent) -> datetime:
    if isinstance(event, OrderPending):
        return event.signal_at
    if isinstance(event, IgnoredSignal | OrderCancelled | OrderBlocked):
        return event.at
    if isinstance(event, OrderFilled):
        return event.filled_at
    if isinstance(event, OrderUnfilled):
        return event.ended_at
    if isinstance(event, OpenTradeMarked):
        return event.mark_at
    raise AssertionError(f"unknown event type: {type(event)!r}")


def _machine_snapshot(machine: SymbolStateMachine) -> tuple[object, ...]:
    return (
        machine.state,
        machine.pending_order,
        machine.position,
        machine.events,
        machine.is_terminal,
        machine.last_processed_at,
    )
