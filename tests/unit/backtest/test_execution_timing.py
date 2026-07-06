from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from stock_desk.backtest.execution import (
    ExecutionEngine,
    ExecutionRequest,
    FillCandidate,
    SignalBar,
    ReferenceOpen,
    candidates_from_status,
)
from stock_desk.market.execution_status import (
    ExecutionEligibility,
    ExecutionStatusDay,
    ExecutionStatusQuery,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.types import Exchange, Period, ProviderId


SHANGHAI = ZoneInfo("Asia/Shanghai")


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def candidate(value: str, price: str, *, suspended: bool = False) -> FillCandidate:
    at = ts(value)
    return FillCandidate(
        timestamp=at,
        open_price=Decimal(price),
        eligibility=ExecutionEligibility(
            timestamp=at,
            trading_day=at.date(),
            is_exchange_open=True,
            suspension_state=(
                SuspensionState.SUSPENDED if suspended else SuspensionState.NORMAL
            ),
            buy_blocked_at_open=False,
            sell_blocked_at_open=False,
            evidence_complete=True,
        ),
    )


def test_daily_signal_executes_at_next_trade_day_open() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.DAY,
            signals=(SignalBar(timestamp=ts("2026-01-05 15:00"), buy=True),),
            candidates=(candidate("2026-01-06 09:30", "10"),),
        )
    )

    assert result.failure is None
    assert result.trades[0].entry.timestamp == ts("2026-01-06 09:30")


def test_suspended_order_waits_for_first_executable_open() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.DAY,
            signals=(SignalBar(timestamp=ts("2026-01-05 15:00"), buy=True),),
            candidates=(
                candidate("2026-01-06 09:30", "10", suspended=True),
                candidate("2026-01-08 09:30", "10.2"),
            ),
        )
    )

    assert result.trades[0].entry.timestamp == ts("2026-01-08 09:30")
    assert result.blocked_events[0].reason == "suspended"


def test_weekly_signal_uses_first_executable_daily_open() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.WEEK,
            signals=(SignalBar(timestamp=ts("2026-01-09 15:00"), buy=True),),
            candidates=(
                candidate("2026-01-12 09:30", "10", suspended=True),
                candidate("2026-01-13 09:30", "10.1"),
            ),
        )
    )

    assert result.trades[0].entry.timestamp == ts("2026-01-13 09:30")


def test_60m_signal_never_fills_same_bar_timestamp() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.MIN60,
            signals=(SignalBar(timestamp=ts("2026-01-05 10:30"), buy=True),),
            candidates=(
                candidate("2026-01-05 10:30", "10"),
                candidate("2026-01-05 13:00", "10.1"),
            ),
        )
    )

    assert result.trades[0].entry.timestamp == ts("2026-01-05 13:00")


def test_daily_reference_midnight_pairs_with_same_day_0930_status_opportunity() -> None:
    day = ts("2026-01-06 00:00").date()
    query = ExecutionStatusQuery(
        symbol="600000.SH",
        exchange=Exchange.SH,
        start=day,
        end=day + timedelta(days=1),
        period=Period.DAY,
    )
    status = materialize_execution_status(
        query=query,
        days=(
            ExecutionStatusDay(
                day=day,
                exchange=Exchange.SH,
                is_exchange_open=True,
                suspension_state=SuspensionState.NORMAL,
                raw_upper_limit=Decimal("20"),
                raw_lower_limit=Decimal("1"),
            ),
        ),
        raw_opens=(),
        source=ProviderId.TUSHARE,
        fetched_at=ts("2026-01-07 00:00"),
        data_cutoff=ts("2026-01-07 00:00"),
    )

    candidates = candidates_from_status(
        status,
        reference_opens=(
            ReferenceOpen(timestamp=ts("2026-01-06 00:00"), price=Decimal("10")),
        ),
    )

    assert len(candidates) == 1
    assert candidates[0].timestamp == ts("2026-01-06 09:30")
    assert candidates[0].open_price == Decimal("10")
    assert candidates[0].eligibility is not None


def test_execution_end_calls_terminal_path_for_open_trade() -> None:
    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.DAY,
            signals=(SignalBar(timestamp=ts("2026-01-05 15:00"), buy=True),),
            candidates=(candidate("2026-01-06 09:30", "10"),),
            ended_at=ts("2026-01-06 15:00"),
            mark_price=Decimal("10.5"),
        )
    )

    assert result.order_events[-1].__class__.__name__ == "OpenTradeMarked"
