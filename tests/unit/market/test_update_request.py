from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from stock_desk.market.types import (
    Adjustment,
    MAX_MARKET_UPDATE_PERIOD_BUCKETS,
    Period,
)
from stock_desk.market.update import MarketUpdateRequest


def _payload(**updates: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbols": ["600000.SH", "000001.SZ"],
        "period": "1d",
        "adjustment": "qfq",
        "start": "2024-01-01T16:00:00Z",
        "end": "2024-01-03T16:00:00Z",
    }
    payload.update(updates)
    return payload


def test_request_parses_strict_json_into_frozen_canonical_values() -> None:
    request = MarketUpdateRequest.from_payload(_payload())

    assert request.symbols == ("600000.SH", "000001.SZ")
    assert request.period is Period.DAY
    assert request.adjustment is Adjustment.QFQ
    assert request.start == datetime(2024, 1, 1, 16, tzinfo=timezone.utc)
    assert request.end == datetime(2024, 1, 3, 16, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        request.symbols = ("600000.SH",)


@pytest.mark.parametrize(
    "symbols",
    [
        [],
        ["600000.SH", "600000.SH"],
        ["600000.sh"],
        ["600000.SH", 1],
        "600000.SH",
    ],
)
def test_request_rejects_empty_duplicate_or_noncanonical_symbols(
    symbols: object,
) -> None:
    with pytest.raises(ValidationError):
        MarketUpdateRequest.from_payload(_payload(symbols=symbols))


@pytest.mark.parametrize(
    "updates",
    [
        {"extra": True},
        {"period": 1},
        {"adjustment": True},
        {"start": "2024-01-03T16:00:00Z"},
        {"start": "not-a-datetime"},
    ],
)
def test_request_rejects_extra_coerced_and_invalid_range_values(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        MarketUpdateRequest.from_payload(_payload(**updates))


def test_request_symbols_have_10000_item_boundary() -> None:
    boundary = [f"{index:06d}.SZ" for index in range(10_000)]
    request = MarketUpdateRequest.from_payload(_payload(symbols=boundary))
    assert len(request.symbols) == 10_000

    with pytest.raises(ValidationError):
        MarketUpdateRequest.from_payload(_payload(symbols=[*boundary, "010000.SZ"]))


def test_request_rejects_aggregate_period_work_above_100_million() -> None:
    symbols = [f"{index:06d}.SZ" for index in range(1000)]
    with pytest.raises(ValidationError, match="work"):
        MarketUpdateRequest.from_payload(
            _payload(
                symbols=symbols,
                period="60m",
                start="2024-01-01T00:00:00Z",
                end="2039-01-01T00:00:00Z",
            )
        )


def test_request_aggregate_work_has_two_million_bucket_boundary() -> None:
    assert MAX_MARKET_UPDATE_PERIOD_BUCKETS == 2_000_000
    symbols = [f"{index:06d}.SZ" for index in range(10_000)]
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    accepted = MarketUpdateRequest(
        symbols=tuple(symbols),
        period=Period.DAY,
        adjustment=Adjustment.QFQ,
        start=start,
        end=start + timedelta(days=200),
    )
    assert len(accepted.symbols) == 10_000

    with pytest.raises(ValidationError, match="work"):
        MarketUpdateRequest(
            symbols=tuple(symbols),
            period=Period.DAY,
            adjustment=Adjustment.QFQ,
            start=start,
            end=start + timedelta(days=201),
        )


@pytest.mark.parametrize(
    "python_only",
    [
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        b"2024-01-01T00:00:00Z",
        {"2024-01-01T00:00:00Z"},
    ],
)
def test_request_boundary_rejects_python_only_values(python_only: object) -> None:
    with pytest.raises((TypeError, ValidationError)):
        MarketUpdateRequest.from_payload(_payload(start=python_only))
