# ruff: noqa: F401
"""Shared imports, fixtures, provider stubs, constants, and builders."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFailure,
    BarQuery,
    BarResult,
    Exchange,
    FailureReason,
    Instrument,
    InstrumentKind,
    ListingStatus,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingDay,
)
from stock_desk.market.providers.base import DatasetProvenance, ProviderBatch


QUERY = BarQuery(
    symbol="600000.SH",
    period=Period.DAY,
    adjustment=Adjustment.NONE,
    start=datetime(2024, 7, 1, tzinfo=timezone.utc),
    end=datetime(2024, 7, 3, tzinfo=timezone.utc),
)
FETCHED_AT = datetime(2024, 7, 3, 8, tzinfo=timezone.utc)
DATA_CUTOFF = datetime(2024, 7, 2, 7, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def upstream_fields(category: MarketCapability) -> dict[str, object]:
    return {
        "upstream_fetched_at": FETCHED_AT,
        "upstream_data_cutoff": DATA_CUTOFF,
        "upstream_adjustment": (
            Adjustment.NONE if category is MarketCapability.BARS else None
        ),
    }


def bar_result(
    source: ProviderId,
    dataset_version: str,
    *,
    fetched_at: datetime = FETCHED_AT,
) -> BarResult:
    return BarResult(
        query=QUERY,
        bars=(
            Bar(
                symbol=QUERY.symbol,
                timestamp=datetime(2024, 7, 1, 16, tzinfo=timezone.utc),
                period=QUERY.period,
                adjustment=QUERY.adjustment,
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=100,
            ),
        ),
        coverage_start=QUERY.start,
        coverage_end=QUERY.end,
        provenance=Provenance(
            source=source,
            fetched_at=fetched_at,
            data_cutoff=DATA_CUTOFF,
            adjustment=QUERY.adjustment,
            dataset_version=dataset_version,
        ),
    )
