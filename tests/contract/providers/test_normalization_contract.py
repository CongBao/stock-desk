from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_desk.market.providers.base import ProviderInvalidResponse
from stock_desk.market.providers.normalization import (
    binary_flag,
    parse_date,
    parse_datetime,
    parse_optional_date,
    period_bounds,
    records_from_table,
)
from stock_desk.market.types import Period
from tests.contract.providers.conftest import FakeFrame


SHANGHAI = ZoneInfo("Asia/Shanghai")


class TimestampLike:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def to_pydatetime(self) -> datetime:
        return self._value


@pytest.mark.parametrize(
    "raw",
    [
        date(2024, 7, 1),
        datetime(2024, 7, 1, 12),
        datetime(2024, 6, 30, 16, tzinfo=timezone.utc),
        TimestampLike(datetime(2024, 6, 30, 16, tzinfo=timezone.utc)),
    ],
)
def test_parse_date_accepts_real_dataframe_date_scalars(raw: object) -> None:
    assert parse_date(raw) == date(2024, 7, 1)


@pytest.mark.parametrize(
    "raw",
    [
        datetime(2024, 7, 1, 15),
        datetime(2024, 7, 1, 7, tzinfo=timezone.utc),
        TimestampLike(datetime(2024, 7, 1, 7, tzinfo=timezone.utc)),
    ],
)
def test_parse_datetime_accepts_real_dataframe_datetime_scalars(raw: object) -> None:
    assert parse_datetime(raw) == datetime(2024, 7, 1, 15, tzinfo=SHANGHAI)


@pytest.mark.parametrize("raw", [None, "", float("nan"), Decimal("NaN")])
def test_parse_optional_date_accepts_only_safe_missing_sentinels(raw: object) -> None:
    assert parse_optional_date(raw) is None


@pytest.mark.parametrize("raw", [True, False, 2, -1, "2", "true"])
def test_binary_flag_rejects_bool_and_non_binary_values(raw: object) -> None:
    with pytest.raises(ProviderInvalidResponse):
        binary_flag(raw)


@pytest.mark.parametrize(
    ("raw", "expected"), [(0, False), (1, True), ("0", False), ("1", True)]
)
def test_binary_flag_accepts_only_explicit_int_or_string_values(
    raw: object,
    expected: bool,
) -> None:
    assert binary_flag(raw) is expected


def test_empty_zero_column_dataframe_is_an_empty_table() -> None:
    assert (
        records_from_table(FakeFrame([], columns=[]), required=frozenset({"x"})) == ()
    )


@pytest.mark.parametrize(
    ("raw", "period", "expected_start", "expected_cutoff"),
    [
        (
            "2024-07-02",
            Period.DAY,
            datetime(2024, 7, 2, tzinfo=SHANGHAI),
            datetime(2024, 7, 2, 15, tzinfo=SHANGHAI),
        ),
        (
            "2024-07-05",
            Period.WEEK,
            datetime(2024, 7, 1, tzinfo=SHANGHAI),
            datetime(2024, 7, 5, 15, tzinfo=SHANGHAI),
        ),
    ],
)
def test_day_and_week_cutoffs_use_the_source_final_trading_session(
    raw: str,
    period: Period,
    expected_start: datetime,
    expected_cutoff: datetime,
) -> None:
    timestamp, cutoff = period_bounds(raw, period)

    assert timestamp == expected_start.astimezone(timezone.utc)
    assert cutoff == expected_cutoff.astimezone(timezone.utc)
