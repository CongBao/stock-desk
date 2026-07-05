from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from stock_desk.market.types import Period, TradingSession


MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
_SESSION_TIMES = (
    (time(9, 30), time(11, 30)),
    (time(13), time(15)),
)
_ONE_HOUR = timedelta(hours=1)


def _market_time(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("market timestamp must be timezone-aware")
    return timestamp.astimezone(MARKET_TIMEZONE)


def regular_trading_sessions(day: date) -> tuple[TradingSession, TradingSession]:
    """Return regular session windows without inferring whether the date is open."""
    morning, afternoon = (
        TradingSession(
            opens_at=datetime.combine(day, opens_at, tzinfo=MARKET_TIMEZONE),
            closes_at=datetime.combine(day, closes_at, tzinfo=MARKET_TIMEZONE),
        )
        for opens_at, closes_at in _SESSION_TIMES
    )
    return morning, afternoon


def is_regular_session_time(timestamp: datetime) -> bool:
    """Return whether a timestamp falls within a regular A-share session window."""
    local_timestamp = _market_time(timestamp)
    return any(
        session.opens_at <= local_timestamp <= session.closes_at
        for session in regular_trading_sessions(local_timestamp.date())
    )


def normalize_period_start(timestamp: datetime, period: Period) -> datetime:
    """Normalize a timestamp to its canonical Asia/Shanghai bar bucket start."""
    local_timestamp = _market_time(timestamp)
    canonical_period = Period(period)
    day_start = local_timestamp.replace(hour=0, minute=0, second=0, microsecond=0)

    if canonical_period is Period.DAY:
        return day_start.astimezone(timezone.utc)
    if canonical_period is Period.WEEK:
        return (day_start - timedelta(days=day_start.weekday())).astimezone(
            timezone.utc
        )

    for session in regular_trading_sessions(local_timestamp.date()):
        if session.opens_at <= local_timestamp <= session.closes_at:
            elapsed_hours = int(
                (local_timestamp - session.opens_at).total_seconds()
                // _ONE_HOUR.total_seconds()
            )
            bucket_count = int(
                (session.closes_at - session.opens_at).total_seconds()
                // _ONE_HOUR.total_seconds()
            )
            return session.opens_at + min(elapsed_hours, bucket_count - 1) * _ONE_HOUR

    raise ValueError("60m timestamp must fall within a trading session")
