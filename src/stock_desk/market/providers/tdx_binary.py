from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import math
from struct import Struct

from stock_desk.market.providers.base import ProviderCorrupt


DAY_RECORD_STRUCT = Struct("<IIIIIfII")
assert DAY_RECORD_STRUCT.size == 32

MAX_DAY_RECORDS = 10_000
MAX_DAY_BYTES = DAY_RECORD_STRUCT.size * MAX_DAY_RECORDS
MIN_DAY = date(1990, 1, 1)
_PRICE_SCALE = Decimal("0.01")
# Deliberately generous corruption guards in yuan, not expected market maxima.
MAX_TDX_PRICE_YUAN = Decimal("1000000")
MAX_TDX_DAILY_AMOUNT_YUAN = 10**16
_MAX_RAW_PRICE_CENTS = 100_000_000


@dataclass(frozen=True, slots=True)
class TdxDayRecord:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    amount: float
    volume: int


def _parse_day(raw: int, observed_on: date) -> date:
    try:
        value = date(raw // 10_000, raw // 100 % 100, raw % 100)
    except ValueError:
        raise ProviderCorrupt() from None
    if value < MIN_DAY or value > observed_on:
        raise ProviderCorrupt()
    return value


def parse_day_bytes(
    data: bytes,
    *,
    observed_on: date,
) -> tuple[TdxDayRecord, ...]:
    if (
        not isinstance(data, bytes)
        or not data
        or len(data) > MAX_DAY_BYTES
        or len(data) % DAY_RECORD_STRUCT.size != 0
    ):
        raise ProviderCorrupt()

    records: list[TdxDayRecord] = []
    previous_day: date | None = None
    for offset in range(0, len(data), DAY_RECORD_STRUCT.size):
        (
            raw_day,
            raw_open,
            raw_high,
            raw_low,
            raw_close,
            amount,
            volume,
            _reserved,
        ) = DAY_RECORD_STRUCT.unpack_from(data, offset)
        day = _parse_day(raw_day, observed_on)
        if previous_day is not None and day <= previous_day:
            raise ProviderCorrupt()
        if (
            min(raw_open, raw_high, raw_low, raw_close) <= 0
            or max(raw_open, raw_high, raw_low, raw_close) > _MAX_RAW_PRICE_CENTS
            or raw_high < max(raw_open, raw_close)
            or raw_low > min(raw_open, raw_close)
            or not math.isfinite(amount)
            or amount < 0
            or amount > MAX_TDX_DAILY_AMOUNT_YUAN
        ):
            raise ProviderCorrupt()
        records.append(
            TdxDayRecord(
                day=day,
                open=Decimal(raw_open) * _PRICE_SCALE,
                high=Decimal(raw_high) * _PRICE_SCALE,
                low=Decimal(raw_low) * _PRICE_SCALE,
                close=Decimal(raw_close) * _PRICE_SCALE,
                amount=amount,
                volume=volume,
            )
        )
        previous_day = day
    return tuple(records)
