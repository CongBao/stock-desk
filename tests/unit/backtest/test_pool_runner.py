from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from types import SimpleNamespace
import time

import pytest

from stock_desk.backtest.events import OrderFilled, OrderPending
from stock_desk.backtest.execution import (
    ExecutionFill,
    ExecutionResult,
    ExecutionTrade,
)
from stock_desk.backtest.metrics import summarize
from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.tasks.repository import TaskConflict


def _runner_dependencies(*, heartbeat_error: BaseException | None = None):
    identity = object()

    class Tasks:
        database_identity = identity

        def heartbeat(self, *_args, **_kwargs):
            if heartbeat_error is not None:
                raise heartbeat_error

    dependency = SimpleNamespace(database_identity=identity)
    return Tasks(), dependency


@pytest.mark.parametrize("interval", [math.nan, math.inf, -math.inf, 0, -1, True])
def test_runner_rejects_invalid_heartbeat_interval(interval: object) -> None:
    tasks, dependency = _runner_dependencies()

    with pytest.raises(ValueError, match="positive finite"):
        PoolBacktestRunner(
            engine=object(),  # type: ignore[arg-type]
            tasks=tasks,  # type: ignore[arg-type]
            repository=dependency,
            market_lake=dependency,
            status_lake=dependency,
            formulas=dependency,
            heartbeat_interval_seconds=interval,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "duration",
    [
        timedelta(0),
        timedelta(microseconds=-1),
        timedelta(hours=1, microseconds=1),
        "one minute",
    ],
)
def test_runner_rejects_heartbeat_duration_outside_task_bounds(
    duration: object,
) -> None:
    tasks, dependency = _runner_dependencies()

    with pytest.raises(ValueError, match="at most one hour"):
        PoolBacktestRunner(
            engine=object(),  # type: ignore[arg-type]
            tasks=tasks,  # type: ignore[arg-type]
            repository=dependency,
            market_lake=dependency,
            status_lake=dependency,
            formulas=dependency,
            heartbeat_lease_duration=duration,  # type: ignore[arg-type]
        )


def test_keepalive_thread_failure_is_surfaced_to_fence_work() -> None:
    tasks, dependency = _runner_dependencies(
        heartbeat_error=RuntimeError("heartbeat storage failed")
    )
    runner = PoolBacktestRunner(
        engine=object(),  # type: ignore[arg-type]
        tasks=tasks,  # type: ignore[arg-type]
        repository=dependency,
        market_lake=dependency,
        status_lake=dependency,
        formulas=dependency,
        heartbeat_interval_seconds=0.001,
    )
    claim = SimpleNamespace(
        snapshot=SimpleNamespace(id="task-id"),
        claim_token="claim-token",
    )

    with pytest.raises(RuntimeError, match="heartbeat storage failed"):
        with runner._keep_claim_alive(claim):  # type: ignore[arg-type]
            time.sleep(0.02)


def test_keepalive_ignores_only_expected_fence_after_terminal_success() -> None:
    tasks, dependency = _runner_dependencies(
        heartbeat_error=TaskConflict("terminal task no longer has a lease")
    )
    runner = PoolBacktestRunner(
        engine=object(),  # type: ignore[arg-type]
        tasks=tasks,  # type: ignore[arg-type]
        repository=dependency,
        market_lake=dependency,
        status_lake=dependency,
        formulas=dependency,
        heartbeat_interval_seconds=0.001,
    )
    claim = SimpleNamespace(
        snapshot=SimpleNamespace(id="task-id"),
        claim_token="claim-token",
    )

    with runner._keep_claim_alive(claim) as state:  # type: ignore[arg-type]
        state.terminal_succeeded = True
        time.sleep(0.02)


def test_trade_holding_bars_are_counted_per_position_timeline() -> None:
    start = datetime(2024, 1, 1, 15, tzinfo=timezone.utc)
    at = lambda offset: start + timedelta(days=offset)  # noqa: E731
    events = (
        OrderPending(side="buy", signal_at=at(0), eligible_at=at(1)),
        OrderFilled(
            side="buy",
            signal_at=at(0),
            filled_at=at(1),
            price=Decimal("10"),
            quantity=100,
        ),
        OrderPending(side="sell", signal_at=at(2), eligible_at=at(3)),
        OrderFilled(
            side="sell",
            signal_at=at(2),
            filled_at=at(3),
            price=Decimal("11"),
            quantity=100,
        ),
        OrderPending(side="buy", signal_at=at(3), eligible_at=at(4)),
        OrderFilled(
            side="buy",
            signal_at=at(3),
            filled_at=at(4),
            price=Decimal("12"),
            quantity=100,
        ),
        OrderPending(side="sell", signal_at=at(7), eligible_at=at(8)),
        OrderFilled(
            side="sell",
            signal_at=at(7),
            filled_at=at(8),
            price=Decimal("13"),
            quantity=100,
        ),
    )
    execution = ExecutionResult(
        trades=(
            ExecutionTrade(
                entry=ExecutionFill(at(1), Decimal("10")),
                exit=ExecutionFill(at(3), Decimal("11")),
            ),
            ExecutionTrade(
                entry=ExecutionFill(at(4), Decimal("12")),
                exit=ExecutionFill(at(8), Decimal("13")),
            ),
        ),
        order_events=events,
        blocked_events=(),
        failure=None,
    )
    snapshot = SimpleNamespace(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        quantity_shares=100,
        formula_version_id="formula-v1",
    )
    reference = SimpleNamespace(
        symbol="600000.SH",
        signal_manifest_record_id="signal-manifest",
        execution_manifest_record_id="execution-manifest",
        execution_status_manifest_record_id="status-manifest",
    )

    samples = PoolBacktestRunner._trade_samples(
        snapshot=snapshot,
        reference=reference,
        signal_series_id="series-v1",
        execution=execution,
        signal_timestamps=tuple(at(offset) for offset in range(8)),
    )

    assert tuple(sample.holding_bars for sample in samples) == (2, 4)
    assert summarize(samples).average_holding_bars == Decimal("3.000000")


def test_trade_events_normalize_real_adjusted_prices_to_cost_contract() -> None:
    start = datetime(2024, 1, 1, 15, tzinfo=timezone.utc)
    entry_at = start + timedelta(days=1)
    exit_at = start + timedelta(days=3)
    events = (
        OrderPending(side="buy", signal_at=start, eligible_at=entry_at),
        OrderFilled(
            side="buy",
            signal_at=start,
            filled_at=entry_at,
            price=Decimal("256.12345678"),
            quantity=100,
        ),
        OrderPending(
            side="sell",
            signal_at=start + timedelta(days=2),
            eligible_at=exit_at,
        ),
        OrderFilled(
            side="sell",
            signal_at=start + timedelta(days=2),
            filled_at=exit_at,
            price=Decimal("271.98765432"),
            quantity=100,
        ),
    )
    execution = ExecutionResult(
        trades=(
            ExecutionTrade(
                entry=ExecutionFill(entry_at, Decimal("256.12345678")),
                exit=ExecutionFill(exit_at, Decimal("271.98765432")),
            ),
        ),
        order_events=events,
        blocked_events=(),
        failure=None,
    )
    snapshot = SimpleNamespace(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        quantity_shares=100,
        formula_version_id="formula-v1",
    )
    reference = SimpleNamespace(
        symbol="300750.SZ",
        signal_manifest_record_id="signal-manifest",
        execution_manifest_record_id="execution-manifest",
        execution_status_manifest_record_id="status-manifest",
    )

    samples = PoolBacktestRunner._trade_samples(
        snapshot=snapshot,
        reference=reference,
        signal_series_id="series-v1",
        execution=execution,
        signal_timestamps=(start, start + timedelta(days=2)),
    )

    assert samples[0].entry_reference_open == Decimal("256.1235")
    assert samples[0].exit_reference_open == Decimal("271.9877")
    fill_prices = tuple(
        event.price
        for event in samples[0].order_events
        if isinstance(event, OrderFilled)
    )
    assert fill_prices == (Decimal("256.1235"), Decimal("271.9877"))
