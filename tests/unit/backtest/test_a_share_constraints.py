from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from stock_desk.backtest.execution import (
    ExecutionEngine,
    ExecutionRequest,
    FillCandidate,
    SignalBar,
)
from stock_desk.market.execution_status import ExecutionEligibility, SuspensionState
from stock_desk.market.types import Period


SHANGHAI = ZoneInfo("Asia/Shanghai")


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def candidate(
    value: str,
    price: str,
    *,
    buy_blocked: bool = False,
    sell_blocked: bool = False,
    complete: bool = True,
) -> FillCandidate:
    at = ts(value)
    return FillCandidate(
        timestamp=at,
        open_price=Decimal(price),
        eligibility=ExecutionEligibility(
            timestamp=at,
            trading_day=at.date(),
            is_exchange_open=True,
            suspension_state=(
                SuspensionState.NORMAL if complete else SuspensionState.UNKNOWN
            ),
            buy_blocked_at_open=buy_blocked,
            sell_blocked_at_open=sell_blocked,
            evidence_complete=complete,
        ),
    )


def test_t_plus_one_blocks_same_day_sell_until_next_shanghai_date() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.MIN60,
            signals=(
                SignalBar(timestamp=ts("2026-01-05 09:30"), buy=True),
                SignalBar(timestamp=ts("2026-01-05 10:30"), sell=True),
            ),
            candidates=(
                candidate("2026-01-05 10:30", "10"),
                candidate("2026-01-05 13:00", "10.1"),
                candidate("2026-01-06 09:30", "10.2"),
            ),
        )
    )

    assert result.trades[0].exit is not None
    assert (
        result.trades[0].exit.timestamp.date() > result.trades[0].entry.timestamp.date()
    )
    assert any(item.reason == "t_plus_one" for item in result.blocked_events)


def test_unknown_execution_status_fails_closed() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.DAY,
            signals=(SignalBar(timestamp=ts("2026-01-05 15:00"), buy=True),),
            candidates=(candidate("2026-01-06 09:30", "10", complete=False),),
        )
    )

    assert result.failure is not None
    assert result.failure.reason == "data_insufficient_execution_status"


def test_side_specific_limits_block_only_the_prohibited_side() -> None:
    buy_blocked = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.DAY,
            signals=(SignalBar(timestamp=ts("2026-01-05 15:00"), buy=True),),
            candidates=(
                candidate("2026-01-06 09:30", "11", buy_blocked=True),
                candidate("2026-01-07 09:30", "10.8"),
            ),
        )
    )
    assert buy_blocked.trades[0].entry.timestamp == ts("2026-01-07 09:30")
    assert buy_blocked.blocked_events[0].reason == "limit_up"

    sell_allowed = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.DAY,
            signals=(
                SignalBar(timestamp=ts("2026-01-05 15:00"), buy=True),
                SignalBar(timestamp=ts("2026-01-06 15:00"), sell=True),
            ),
            candidates=(
                candidate("2026-01-06 09:30", "10"),
                candidate("2026-01-07 09:30", "11", buy_blocked=True),
            ),
        )
    )
    assert sell_allowed.trades[0].exit is not None
    assert sell_allowed.trades[0].exit.timestamp == ts("2026-01-07 09:30")


def test_sell_at_limit_down_stays_pending_but_buy_remains_allowed() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.DAY,
            signals=(
                SignalBar(timestamp=ts("2026-01-05 15:00"), buy=True),
                SignalBar(timestamp=ts("2026-01-06 15:00"), sell=True),
            ),
            candidates=(
                candidate("2026-01-06 09:30", "10", sell_blocked=True),
                candidate("2026-01-07 09:30", "9", sell_blocked=True),
                candidate("2026-01-08 09:30", "9.2"),
            ),
        )
    )

    assert result.trades[0].entry.timestamp == ts("2026-01-06 09:30")
    assert result.trades[0].exit is not None
    assert result.trades[0].exit.timestamp == ts("2026-01-08 09:30")
    assert result.blocked_events[0].reason == "limit_down"
