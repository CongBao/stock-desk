from __future__ import annotations

from datetime import date, datetime, time, timezone
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from stock_desk.market.calendar import (
    MARKET_TIMEZONE,
    is_regular_session_time,
    normalize_period_start,
    regular_trading_sessions,
)
from stock_desk.market.types import Period


SHANGHAI = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 7, 6)


def market_time(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SHANGHAI)


def test_trading_sessions_are_explicit_and_in_asia_shanghai() -> None:
    morning, afternoon = regular_trading_sessions(MONDAY)

    assert MARKET_TIMEZONE.key == "Asia/Shanghai"
    assert (morning.opens_at, morning.closes_at) == (
        market_time(MONDAY, 9, 30),
        market_time(MONDAY, 11, 30),
    )
    assert (afternoon.opens_at, afternoon.closes_at) == (
        market_time(MONDAY, 13),
        market_time(MONDAY, 15),
    )
    assert morning.closes_at < afternoon.opens_at
    assert morning.opens_at.tzinfo is timezone.utc
    assert afternoon.closes_at.tzinfo is timezone.utc


def test_session_clock_is_timezone_aware_and_preserves_the_lunch_break() -> None:
    assert is_regular_session_time(market_time(MONDAY, 10)) is True
    assert is_regular_session_time(market_time(MONDAY, 12)) is False
    assert (
        is_regular_session_time(market_time(MONDAY, 14).astimezone(timezone.utc))
        is True
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        is_regular_session_time(datetime(2026, 7, 6, 10))


def test_period_start_normalization_uses_market_sessions() -> None:
    assert normalize_period_start(
        market_time(MONDAY, 10, 45), Period.MIN60
    ) == market_time(MONDAY, 10, 30)
    assert normalize_period_start(
        market_time(MONDAY, 11, 30), Period.MIN60
    ) == market_time(MONDAY, 10, 30)
    assert normalize_period_start(
        market_time(MONDAY, 14, 37), Period.MIN60
    ) == market_time(MONDAY, 14)
    assert normalize_period_start(market_time(MONDAY, 16), Period.DAY) == market_time(
        MONDAY, 0
    )
    assert normalize_period_start(
        datetime(2026, 7, 8, 16, tzinfo=SHANGHAI), Period.WEEK
    ) == market_time(MONDAY, 0)
    assert (
        normalize_period_start(market_time(MONDAY, 16), Period.DAY).tzinfo
        is timezone.utc
    )
    with pytest.raises(ValueError, match="trading session"):
        normalize_period_start(market_time(MONDAY, 12), Period.MIN60)


def test_session_templates_do_not_claim_to_be_a_real_trading_calendar() -> None:
    sunday = date(2026, 7, 5)

    morning, afternoon = regular_trading_sessions(sunday)

    assert morning.opens_at.astimezone(SHANGHAI).date() == sunday
    assert afternoon.closes_at.astimezone(SHANGHAI).date() == sunday


def test_market_timezone_loads_without_a_system_timezone_database() -> None:
    environment = os.environ.copy()
    environment["PYTHONTZPATH"] = ""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from stock_desk.market import MARKET_TIMEZONE; "
            "assert MARKET_TIMEZONE.key == 'Asia/Shanghai'",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
