from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stock_desk.backtest.events import (
    CancellationReason,
    IgnoredSignal,
    IgnoredSignalReason,
    OpenTradeMarked,
    OrderBlocked,
    OrderCancelled,
    OrderFilled,
    OrderPending,
    OrderUnfilled,
    SignalCode,
)
from stock_desk.backtest.state_machine import (
    PositionState,
    Signal,
    SymbolStateMachine,
)


UTC = timezone.utc
BAR_1 = datetime(2026, 1, 5, 7, tzinfo=UTC)
BAR_2 = BAR_1 + timedelta(days=1)
BAR_3 = BAR_2 + timedelta(days=1)
BAR_4 = BAR_3 + timedelta(days=1)


def _held_machine() -> SymbolStateMachine:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)
    machine.fill_pending(at=BAR_2, price=Decimal("10.00"), quantity=1_000)
    return machine


def _pending_sell_machine() -> SymbolStateMachine:
    machine = _held_machine()
    machine.on_signal(Signal.SELL, at=BAR_3)
    return machine


def test_buy_while_flat_creates_auditable_pending_order() -> None:
    machine = SymbolStateMachine()

    events = machine.on_signal(Signal.BUY, at=BAR_1)

    assert events == [
        OrderPending(
            side="buy",
            signal_at=BAR_1,
            eligible_at=BAR_1,
        )
    ]
    assert machine.state is PositionState.PENDING_BUY
    assert machine.pending_order is not None
    assert machine.pending_order.signal_at == BAR_1
    assert machine.pending_order.eligible_at == BAR_1


def test_duplicate_buy_is_ignored_when_already_holding() -> None:
    machine = _held_machine()

    events = machine.on_signal(Signal.BUY, at=BAR_3)

    assert events == [
        IgnoredSignal(
            reason=IgnoredSignalReason.ALREADY_HOLDING,
            signal=SignalCode.BUY,
            at=BAR_3,
        )
    ]
    assert machine.state is PositionState.HELD


def test_flat_sell_is_ignored() -> None:
    machine = SymbolStateMachine()

    assert machine.on_signal(Signal.SELL, at=BAR_1) == [
        IgnoredSignal(
            reason=IgnoredSignalReason.NOT_HOLDING,
            signal=SignalCode.SELL,
            at=BAR_1,
        )
    ]
    assert machine.state.value == PositionState.FLAT.value


def test_same_side_pending_signal_does_not_reset_order_eligibility() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)
    original = machine.pending_order

    events = machine.on_signal(Signal.BUY, at=BAR_3)

    assert events == [
        IgnoredSignal(
            reason=IgnoredSignalReason.SAME_SIDE_ORDER_PENDING,
            signal=SignalCode.BUY,
            at=BAR_3,
        )
    ]
    assert machine.pending_order is original
    assert machine.pending_order is not None
    assert machine.pending_order.eligible_at == BAR_1


def test_blocked_attempt_is_audited_without_resetting_pending_order() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)
    original = machine.pending_order

    events = machine.block_pending(at=BAR_2, reason="suspended")

    assert events == [OrderBlocked(side="buy", at=BAR_2, reason="suspended")]
    assert machine.state is PositionState.PENDING_BUY
    assert machine.pending_order is original


def test_opposite_signal_cancels_pending_buy() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)

    events = machine.on_signal(Signal.SELL, at=BAR_3)

    assert events[-1] == OrderCancelled(
        side="buy",
        reason=CancellationReason.OPPOSITE_SIGNAL,
        at=BAR_3,
    )
    assert machine.state is PositionState.FLAT
    assert machine.pending_order is None


def test_sell_while_held_creates_pending_sell() -> None:
    machine = _held_machine()

    events = machine.on_signal(Signal.SELL, at=BAR_3)

    assert events == [OrderPending(side="sell", signal_at=BAR_3, eligible_at=BAR_3)]
    assert machine.state is PositionState.PENDING_SELL
    assert machine.position is not None


def test_opposite_signal_cancels_pending_sell_back_to_held() -> None:
    machine = _pending_sell_machine()

    events = machine.on_signal(Signal.BUY, at=BAR_4)

    assert events == [
        OrderCancelled(
            side="sell",
            reason=CancellationReason.OPPOSITE_SIGNAL,
            at=BAR_4,
        )
    ]
    assert machine.state is PositionState.HELD
    assert machine.pending_order is None
    assert machine.position is not None


def test_conflicting_signals_are_ignored() -> None:
    machine = SymbolStateMachine()

    assert machine.on_signals(buy=True, sell=True, at=BAR_3) == [
        IgnoredSignal(
            reason=IgnoredSignalReason.CONFLICTING_SIGNALS,
            signal=None,
            at=BAR_3,
        )
    ]
    assert machine.state is PositionState.FLAT


@pytest.mark.parametrize(
    ("buy", "sell"),
    [(None, None), (None, False), (False, None), (False, False)],
)
def test_null_warmup_and_false_signals_are_no_ops(
    buy: bool | None, sell: bool | None
) -> None:
    machine = SymbolStateMachine()

    assert machine.on_signals(buy=buy, sell=sell, at=BAR_1) == []
    assert machine.state is PositionState.FLAT


def test_fill_pending_buy_then_sell_updates_exactly_one_position() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)

    buy_events = machine.fill_pending(at=BAR_2, price=Decimal("10.00"), quantity=1_000)

    assert buy_events == [
        OrderFilled(
            side="buy",
            signal_at=BAR_1,
            filled_at=BAR_2,
            price=Decimal("10.00"),
            quantity=1_000,
        )
    ]
    assert machine.state is PositionState.HELD
    assert machine.holding_count == 1
    assert machine.pending_count == 0

    machine.on_signal(Signal.SELL, at=BAR_3)
    sell_events = machine.fill_pending(at=BAR_4, price=Decimal("10.50"))

    assert sell_events == [
        OrderFilled(
            side="sell",
            signal_at=BAR_3,
            filled_at=BAR_4,
            price=Decimal("10.50"),
            quantity=1_000,
        )
    ]
    assert machine.state.value == PositionState.FLAT.value
    assert machine.position is None
    assert machine.holding_count == 0


def test_range_end_expires_pending_buy_as_unfilled() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)

    events = machine.finish_range(at=BAR_4)

    assert events == [
        OrderUnfilled(
            side="buy",
            signal_at=BAR_1,
            eligible_at=BAR_1,
            ended_at=BAR_4,
            reason="range_ended_unfilled",
        )
    ]
    assert machine.state is PositionState.FLAT
    assert machine.is_terminal


def test_range_end_marks_held_position_with_floating_pnl() -> None:
    machine = _held_machine()

    events = machine.finish_range(at=BAR_4, mark_price=Decimal("10.25"))

    assert events == [
        OpenTradeMarked(
            entry_at=BAR_2,
            entry_price=Decimal("10.00"),
            quantity=1_000,
            mark_at=BAR_4,
            mark_price=Decimal("10.25"),
            floating_pnl=Decimal("250.00"),
        )
    ]
    assert machine.state is PositionState.HELD
    assert machine.is_terminal


def test_range_end_expires_pending_sell_and_marks_underlying_position() -> None:
    machine = _pending_sell_machine()

    events = machine.finish_range(at=BAR_4, mark_price=Decimal("9.75"))

    assert events == [
        OrderUnfilled(
            side="sell",
            signal_at=BAR_3,
            eligible_at=BAR_3,
            ended_at=BAR_4,
            reason="range_ended_unfilled",
        ),
        OpenTradeMarked(
            entry_at=BAR_2,
            entry_price=Decimal("10.00"),
            quantity=1_000,
            mark_at=BAR_4,
            mark_price=Decimal("9.75"),
            floating_pnl=Decimal("-250.00"),
        ),
    ]
    assert machine.state is PositionState.HELD
    assert machine.is_terminal


def test_events_and_machine_snapshots_are_immutable() -> None:
    machine = SymbolStateMachine()
    event = machine.on_signal(Signal.BUY, at=BAR_1)[0]
    pending = machine.pending_order
    assert isinstance(event, OrderPending)
    assert pending is not None

    with pytest.raises(FrozenInstanceError):
        event.side = "sell"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        pending.eligible_at = BAR_3  # type: ignore[misc]


def test_terminal_machine_rejects_more_input() -> None:
    machine = SymbolStateMachine()
    machine.finish_range(at=BAR_4)

    with pytest.raises(RuntimeError, match="terminal"):
        machine.on_signal(Signal.BUY, at=BAR_4)


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            lambda machine: machine.block_pending(at=BAR_2, reason="suspended"),
            "pending",
        ),
        (
            lambda machine: machine.fill_pending(
                at=BAR_2, price=Decimal("10.00"), quantity=1_000
            ),
            "pending",
        ),
    ],
)
def test_pending_operations_reject_flat_state(operation: object, message: str) -> None:
    machine = SymbolStateMachine()

    with pytest.raises(RuntimeError, match=message):
        operation(machine)  # type: ignore[operator]


def test_fill_validates_price_and_quantity() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)

    with pytest.raises(ValueError, match="price"):
        machine.fill_pending(at=BAR_2, price=Decimal("0"), quantity=1_000)
    with pytest.raises(ValueError, match="quantity"):
        machine.fill_pending(at=BAR_2, price=Decimal("10"), quantity=0)


@pytest.mark.parametrize("quantity", [True, 100.0, Decimal("100")])
def test_buy_fill_rejects_non_integer_quantity(quantity: object) -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)

    with pytest.raises(ValueError, match="positive integer"):
        machine.fill_pending(
            at=BAR_2,
            price=Decimal("10.00"),
            quantity=quantity,  # type: ignore[arg-type]
        )

    assert machine.state is PositionState.PENDING_BUY
    assert machine.position is None


def test_opposite_signal_cannot_precede_pending_signal() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_2)
    original = machine.pending_order

    with pytest.raises(ValueError, match="chronological"):
        machine.on_signal(Signal.SELL, at=BAR_1)

    assert machine.state is PositionState.PENDING_BUY
    assert machine.pending_order is original


def test_sell_signal_cannot_precede_position_entry() -> None:
    machine = _held_machine()

    with pytest.raises(ValueError, match="chronological"):
        machine.on_signal(Signal.SELL, at=BAR_1)

    assert machine.state is PositionState.HELD
    assert machine.pending_order is None


def test_block_and_fill_cannot_move_backwards_in_time() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)
    machine.block_pending(at=BAR_3, reason="suspended")
    original = machine.pending_order

    with pytest.raises(ValueError, match="chronological"):
        machine.block_pending(at=BAR_2, reason="suspended")
    with pytest.raises(ValueError, match="chronological"):
        machine.fill_pending(at=BAR_2, price=Decimal("10.00"), quantity=1_000)

    assert machine.state is PositionState.PENDING_BUY
    assert machine.pending_order is original


def test_finish_cannot_precede_latest_processed_operation() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_2)

    with pytest.raises(ValueError, match="chronological"):
        machine.finish_range(at=BAR_1)

    assert not machine.is_terminal
    assert machine.state is PositionState.PENDING_BUY


@pytest.mark.parametrize(
    ("buy", "sell"),
    [(None, None), (True, True), (False, True)],
)
def test_every_processed_signal_timestamp_advances_monotonic_clock(
    buy: bool | None, sell: bool | None
) -> None:
    machine = SymbolStateMachine()
    machine.on_signals(buy=buy, sell=sell, at=BAR_3)

    with pytest.raises(ValueError, match="chronological"):
        machine.on_signal(Signal.BUY, at=BAR_2)

    assert machine.last_processed_at == BAR_3


def test_equal_operation_timestamps_are_allowed() -> None:
    machine = SymbolStateMachine()
    machine.on_signals(buy=None, sell=None, at=BAR_1)

    machine.on_signal(Signal.BUY, at=BAR_1)
    machine.block_pending(at=BAR_1, reason="not_executable")

    assert machine.state is PositionState.PENDING_BUY
    assert machine.last_processed_at == BAR_1


def test_range_may_end_before_future_pending_eligibility() -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1, eligible_at=BAR_4)

    events = machine.finish_range(at=BAR_3)

    assert events == [
        OrderUnfilled(
            side="buy",
            signal_at=BAR_1,
            eligible_at=BAR_4,
            ended_at=BAR_3,
            reason="range_ended_unfilled",
        )
    ]
    assert machine.is_terminal


def test_ignored_and_cancelled_event_equality_includes_audit_fields() -> None:
    ignored = IgnoredSignal(
        reason=IgnoredSignalReason.ALREADY_HOLDING,
        signal=SignalCode.BUY,
        at=BAR_2,
    )
    cancelled = OrderCancelled(
        side="buy",
        reason=CancellationReason.OPPOSITE_SIGNAL,
        at=BAR_2,
    )

    assert ignored != IgnoredSignal(
        reason=IgnoredSignalReason.ALREADY_HOLDING,
        signal=SignalCode.SELL,
        at=BAR_2,
    )
    assert ignored != IgnoredSignal(
        reason=IgnoredSignalReason.ALREADY_HOLDING,
        signal=SignalCode.BUY,
        at=BAR_3,
    )
    assert cancelled != OrderCancelled(
        side="buy",
        reason=CancellationReason.OPPOSITE_SIGNAL,
        at=BAR_3,
    )


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: IgnoredSignal(
                reason="already_holding",  # type: ignore[arg-type]
                signal=SignalCode.BUY,
                at=BAR_1,
            ),
            "reason",
        ),
        (
            lambda: IgnoredSignal(
                reason=IgnoredSignalReason.ALREADY_HOLDING,
                signal="buy",  # type: ignore[arg-type]
                at=BAR_1,
            ),
            "signal",
        ),
        (
            lambda: OrderCancelled(
                side="buy",
                reason="opposite_signal",  # type: ignore[arg-type]
                at=BAR_1,
            ),
            "reason",
        ),
    ],
)
def test_audit_events_reject_untyped_codes(factory: object, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    ("buy", "sell"),
    [(1, False), ("true", False), (False, 0), (False, "false")],
)
def test_on_signals_rejects_non_boolean_values_atomically(
    buy: object, sell: object
) -> None:
    machine = SymbolStateMachine()
    machine.on_signals(buy=None, sell=None, at=BAR_1)
    before = _machine_snapshot(machine)

    with pytest.raises(TypeError, match="bool or None"):
        machine.on_signals(
            buy=buy,  # type: ignore[arg-type]
            sell=sell,  # type: ignore[arg-type]
            at=BAR_2,
        )

    assert _machine_snapshot(machine) == before


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        (1, TypeError),
        ("", ValueError),
        ("   ", ValueError),
        ("Suspended", ValueError),
        ("limit-up", ValueError),
        ("a" * 65, ValueError),
    ],
)
def test_block_reason_requires_stable_bounded_code_atomically(
    reason: object, error: type[Exception]
) -> None:
    machine = SymbolStateMachine()
    machine.on_signal(Signal.BUY, at=BAR_1)
    before = _machine_snapshot(machine)

    with pytest.raises(error, match="reason"):
        machine.block_pending(at=BAR_2, reason=reason)  # type: ignore[arg-type]

    assert _machine_snapshot(machine) == before


@pytest.mark.parametrize("with_pending", [False, True])
def test_finish_without_position_rejects_mark_price_atomically(
    with_pending: bool,
) -> None:
    machine = SymbolStateMachine()
    if with_pending:
        machine.on_signal(Signal.BUY, at=BAR_1)
    before = _machine_snapshot(machine)

    with pytest.raises(ValueError, match="mark_price"):
        machine.finish_range(at=BAR_2, mark_price=Decimal("10.00"))

    assert _machine_snapshot(machine) == before


def _machine_snapshot(machine: SymbolStateMachine) -> tuple[object, ...]:
    return (
        machine.state,
        machine.pending_order,
        machine.position,
        machine.events,
        machine.is_terminal,
        machine.last_processed_at,
    )
