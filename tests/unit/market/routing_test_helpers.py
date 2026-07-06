# ruff: noqa: F401
"""Shared imports, fixtures, provider stubs, constants, and builders."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from stock_desk.market.providers.base import (
    ProviderBatch,
    ProviderBatchFailure,
    ProviderClientError,
    ProviderOperation,
    ProviderPermissionDenied,
    ProviderTimeout,
    ProviderUnavailable,
    ProviderUnsupported,
)
from stock_desk.market.providers.normalization import dataset_version, make_batch
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFailure,
    BarQuery,
    BarResult,
    CapabilityGap,
    CapabilityReport,
    CapabilityState,
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


class StubProvider:
    def __init__(self, name: ProviderId, report: object) -> None:
        self.name = name
        self.report = report
        self.capability_calls = 0

    def capabilities(self) -> object:
        self.capability_calls += 1
        if isinstance(self.report, Exception):
            raise self.report
        return self.report

    def fetch_bars(self, query: object) -> object:
        raise AssertionError("not used in registry model tests")

    def fetch_instruments(self) -> object:
        raise AssertionError("not used in registry model tests")

    def fetch_calendar(self, exchange: Exchange, start: date, end: date) -> object:
        raise AssertionError("not used in registry model tests")


def full_report(source: ProviderId) -> CapabilityReport:
    return CapabilityReport(
        source=source,
        state=CapabilityState.AVAILABLE,
        capabilities=frozenset(MarketCapability),
        available_periods=frozenset(Period),
        available_adjustments=frozenset(Adjustment),
        markets=frozenset(Exchange),
        data_cutoff=datetime(2024, 7, 1, 15, tzinfo=timezone.utc),
        gaps=(),
    )


BAR_QUERY = BarQuery(
    symbol="600000.SH",
    period=Period.DAY,
    adjustment=Adjustment.NONE,
    start=datetime(2024, 7, 1, tzinfo=timezone.utc),
    end=datetime(2024, 7, 3, tzinfo=timezone.utc),
)


def complete_bar_result(source: ProviderId) -> BarResult:
    bars = (
        Bar(
            symbol=BAR_QUERY.symbol,
            timestamp=datetime(2024, 7, 1, 16, tzinfo=timezone.utc),
            period=BAR_QUERY.period,
            adjustment=BAR_QUERY.adjustment,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=100,
        ),
    )
    cutoff = datetime(2024, 7, 2, 7, tzinfo=timezone.utc)
    version = dataset_version(
        source=source,
        operation="bars",
        request={"query": BAR_QUERY},
        data_cutoff=cutoff,
        items=bars,
    )
    return BarResult(
        query=BAR_QUERY,
        bars=bars,
        coverage_start=BAR_QUERY.start,
        coverage_end=BAR_QUERY.end,
        provenance=Provenance(
            source=source,
            fetched_at=datetime(2024, 7, 3, 8, tzinfo=timezone.utc),
            data_cutoff=cutoff,
            adjustment=BAR_QUERY.adjustment,
            dataset_version=version,
        ),
    )


class BarProvider(StubProvider):
    def __init__(
        self,
        name: ProviderId,
        report: object,
        outcome: object,
    ) -> None:
        super().__init__(name, report)
        self.outcome = outcome
        self.bar_queries: list[object] = []

    def fetch_bars(self, query: object) -> object:
        self.bar_queries.append(query)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class BatchProvider(StubProvider):
    def __init__(
        self,
        name: ProviderId,
        report: object,
        *,
        instruments: object,
        calendar: object,
    ) -> None:
        super().__init__(name, report)
        self.instrument_outcome = instruments
        self.calendar_outcome = calendar
        self.instrument_calls = 0
        self.calendar_calls: list[tuple[Exchange, date, date]] = []

    def fetch_instruments(self) -> object:
        self.instrument_calls += 1
        if isinstance(self.instrument_outcome, BaseException):
            raise self.instrument_outcome
        return self.instrument_outcome

    def fetch_calendar(self, exchange: Exchange, start: date, end: date) -> object:
        self.calendar_calls.append((exchange, start, end))
        if isinstance(self.calendar_outcome, BaseException):
            raise self.calendar_outcome
        return self.calendar_outcome


def instrument_batch(
    source: ProviderId,
    *,
    symbols: tuple[str, ...] = ("000001.SZ", "600000.SH"),
) -> ProviderBatch[Instrument]:
    exchanges = {"SH": Exchange.SH, "SZ": Exchange.SZ, "BJ": Exchange.BJ}
    items = tuple(
        Instrument(
            symbol=symbol,
            exchange=exchanges[symbol[-2:]],
            name=f"name-{symbol}",
            instrument_kind=InstrumentKind.STOCK,
            listing_status=ListingStatus.LISTED,
            listed_on=date(2000, 1, 1),
        )
        for symbol in symbols
    )
    observed = datetime(2024, 7, 3, 8, tzinfo=timezone.utc)
    return make_batch(
        source=source,
        operation=ProviderOperation.INSTRUMENTS,
        request={},
        items=items,
        data_cutoff=observed,
        observed_at=observed,
    )


def calendar_batch(
    source: ProviderId,
    *,
    exchange: Exchange = Exchange.SH,
    days: tuple[date, ...] = (date(2024, 7, 1), date(2024, 7, 2)),
) -> ProviderBatch[TradingDay]:
    items = tuple(
        TradingDay(day=day, exchange=exchange, is_open=day.weekday() < 5)
        for day in days
    )
    observed = datetime(2024, 7, 3, 8, tzinfo=timezone.utc)
    return make_batch(
        source=source,
        operation=ProviderOperation.CALENDAR,
        request={
            "exchange": exchange,
            "start": date(2024, 7, 1),
            "end": date(2024, 7, 3),
        },
        items=items,
        data_cutoff=datetime(2024, 7, 2, 8, tzinfo=timezone.utc),
        observed_at=observed,
    )


def unsupported_category_report(source: ProviderId) -> CapabilityReport:
    return CapabilityReport(
        source=source,
        state=CapabilityState.AVAILABLE,
        capabilities=frozenset(
            {MarketCapability.INSTRUMENTS, MarketCapability.TRADING_CALENDAR}
        ),
        gaps=(
            CapabilityGap(
                capability=MarketCapability.BARS,
                state=CapabilityState.UNSUPPORTED,
                reason=FailureReason.UNSUPPORTED,
                detail="provider does not support this request",
            ),
        ),
    )


def calendar_only_report(source: ProviderId) -> CapabilityReport:
    return CapabilityReport(
        source=source,
        state=CapabilityState.AVAILABLE,
        capabilities=frozenset({MarketCapability.TRADING_CALENDAR}),
    )
