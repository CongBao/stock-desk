from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
import math
import random

import pytest

from tests.unit.market.providers.tdx_test_helpers import (
    FLOAT32,
    MAX_VOLUME_RECORD,
    VALID_SH_RECORDS,
    provider_corrupt,
    raw_record,
    tdx_binary,
)


def test_tdx_day_layout_and_exact_golden_values() -> None:
    module = tdx_binary()

    records = module.parse_day_bytes(
        VALID_SH_RECORDS,
        observed_on=date(2024, 7, 2),
    )

    assert module.DAY_RECORD_STRUCT.format == "<IIIIIfII"
    assert module.DAY_RECORD_STRUCT.size == 32
    assert records == (
        module.TdxDayRecord(
            day=date(2024, 7, 1),
            open=Decimal("10"),
            high=Decimal("10.5"),
            low=Decimal("9.9"),
            close=Decimal("10.2"),
            amount=12345.5,
            volume=1000,
        ),
        module.TdxDayRecord(
            day=date(2024, 7, 2),
            open=Decimal("10.2"),
            high=Decimal("10.8"),
            low=Decimal("10.1"),
            close=Decimal("10.7"),
            amount=23456.0,
            volume=0,
        ),
    )
    with pytest.raises(FrozenInstanceError):
        records[0].volume = 7


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        VALID_SH_RECORDS + b"\x00",
        raw_record() * 10_001,
    ],
    ids=["empty", "trailing-byte", "record-and-byte-limit"],
)
def test_tdx_parser_rejects_empty_misaligned_or_oversized_payload(
    payload: bytes,
) -> None:
    module = tdx_binary()

    with pytest.raises(provider_corrupt()):
        module.parse_day_bytes(payload, observed_on=date(2024, 7, 2))


@pytest.mark.parametrize(
    ("raw_day", "observed_on"),
    [
        (0, date(2024, 7, 2)),
        (19891231, date(2024, 7, 2)),
        (20240230, date(2024, 7, 2)),
        (20240703, date(2024, 7, 2)),
    ],
    ids=["zero", "before-1990", "invalid-gregorian", "future"],
)
def test_tdx_parser_rejects_invalid_or_out_of_range_dates(
    raw_day: int,
    observed_on: date,
) -> None:
    module = tdx_binary()

    with pytest.raises(provider_corrupt()):
        module.parse_day_bytes(
            raw_record(raw_date=raw_day),
            observed_on=observed_on,
        )


@pytest.mark.parametrize(
    "payload",
    [
        raw_record(open_price=0),
        raw_record(high=0),
        raw_record(low=0),
        raw_record(close=0),
        raw_record(high=999),
        raw_record(low=1021),
    ],
    ids=["zero-open", "zero-high", "zero-low", "zero-close", "low-high", "high-low"],
)
def test_tdx_parser_rejects_nonpositive_or_invalid_ohlc(payload: bytes) -> None:
    module = tdx_binary()

    with pytest.raises(provider_corrupt()):
        module.parse_day_bytes(payload, observed_on=date(2024, 7, 2))


@pytest.mark.parametrize("amount", [math.nan, math.inf, -math.inf, -1.0])
def test_tdx_parser_rejects_invalid_amount(amount: float) -> None:
    module = tdx_binary()

    with pytest.raises(provider_corrupt()):
        module.parse_day_bytes(
            raw_record(amount=amount),
            observed_on=date(2024, 7, 2),
        )


@pytest.mark.parametrize(
    "payload",
    [
        raw_record() + raw_record(),
        raw_record(raw_date=20240702) + raw_record(raw_date=20240701),
    ],
    ids=["duplicate", "descending"],
)
def test_tdx_parser_rejects_nonascending_dates(payload: bytes) -> None:
    module = tdx_binary()

    with pytest.raises(provider_corrupt()):
        module.parse_day_bytes(payload, observed_on=date(2024, 7, 2))


def test_tdx_parser_accepts_unsigned_volume_boundaries() -> None:
    module = tdx_binary()

    zero = module.parse_day_bytes(
        raw_record(volume=0),
        observed_on=date(2024, 7, 2),
    )
    maximum = module.parse_day_bytes(
        MAX_VOLUME_RECORD,
        observed_on=date(2024, 7, 2),
    )

    assert zero[0].volume == 0
    assert maximum[0].volume == 2**32 - 1


def test_tdx_parser_enforces_generous_raw_price_ceiling() -> None:
    module = tdx_binary()
    boundary = raw_record(
        open_price=100_000_000,
        high=100_000_000,
        low=100_000_000,
        close=100_000_000,
    )
    above = raw_record(
        open_price=100_000_001,
        high=100_000_001,
        low=100_000_001,
        close=100_000_001,
    )

    parsed = module.parse_day_bytes(boundary, observed_on=date(2024, 7, 2))

    assert module.MAX_TDX_PRICE_YUAN == Decimal("1000000")
    assert parsed[0].open == Decimal("1000000")
    with pytest.raises(provider_corrupt()):
        module.parse_day_bytes(above, observed_on=date(2024, 7, 2))


def test_tdx_parser_enforces_float32_daily_amount_ceiling() -> None:
    module = tdx_binary()
    boundary_bytes = (0x5A0E1BC9).to_bytes(4, "little")
    next_bytes = (0x5A0E1BCA).to_bytes(4, "little")

    parsed = module.parse_day_bytes(
        raw_record(amount_bytes=boundary_bytes),
        observed_on=date(2024, 7, 2),
    )

    assert module.MAX_TDX_DAILY_AMOUNT_YUAN == 10**16
    assert parsed[0].amount == FLOAT32.unpack(boundary_bytes)[0]
    with pytest.raises(provider_corrupt()):
        module.parse_day_bytes(
            raw_record(amount_bytes=next_bytes),
            observed_on=date(2024, 7, 2),
        )


def test_tdx_parser_fixed_seed_random_bytes_have_only_typed_outcomes() -> None:
    module = tdx_binary()
    rng = random.Random(20260706)
    lengths = [0, 1, 31, 32, 33, 64, module.MAX_DAY_BYTES]
    lengths.extend(rng.randrange(module.MAX_DAY_BYTES + 1) for _ in range(32))
    lengths.extend(32 * rng.randrange(0, 128) for _ in range(32))

    for length in lengths:
        payload = rng.randbytes(length)
        try:
            records = module.parse_day_bytes(
                payload,
                observed_on=date(2024, 7, 2),
            )
        except provider_corrupt():
            continue
        assert isinstance(records, tuple)
        assert all(isinstance(record, module.TdxDayRecord) for record in records)
        if records:
            with pytest.raises(FrozenInstanceError):
                records[0].volume = 7


def test_tdx_parser_structured_little_endian_records_preserve_exact_values() -> None:
    module = tdx_binary()
    rng = random.Random(20260706)
    payloads: list[bytes] = []
    expected: list[tuple[date, Decimal, Decimal, Decimal, Decimal, int]] = []
    for day_number in range(1, 29):
        raw_open = rng.randrange(100, 100_000)
        raw_close = raw_open + rng.randrange(-50, 51)
        raw_close = max(raw_close, 1)
        raw_high = max(raw_open, raw_close) + rng.randrange(0, 51)
        raw_low = max(min(raw_open, raw_close) - rng.randrange(0, 51), 1)
        volume = rng.randrange(0, 2**32)
        value_day = date(2024, 1, day_number)
        payloads.append(
            raw_record(
                raw_date=int(value_day.strftime("%Y%m%d")),
                open_price=raw_open,
                high=raw_high,
                low=raw_low,
                close=raw_close,
                amount=float(rng.randrange(0, 10**12)),
                volume=volume,
            )
        )
        expected.append(
            (
                value_day,
                Decimal(raw_open) * Decimal("0.01"),
                Decimal(raw_high) * Decimal("0.01"),
                Decimal(raw_low) * Decimal("0.01"),
                Decimal(raw_close) * Decimal("0.01"),
                volume,
            )
        )

    records = module.parse_day_bytes(
        b"".join(payloads),
        observed_on=date(2024, 7, 2),
    )

    assert tuple(
        (record.day, record.open, record.high, record.low, record.close, record.volume)
        for record in records
    ) == tuple(expected)
