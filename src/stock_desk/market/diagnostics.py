"""Bounded, secret-safe diagnostics for configured market-data providers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from functools import partial
import logging
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict

from stock_desk.market.providers import (
    AkShareProvider,
    BaoStockProvider,
    ProviderClientError,
    ProviderMissingCoverage,
    ProviderPermissionDenied,
    ProviderUnsupported,
    TdxInspectionFailure,
    TdxLocalProvider,
    TushareProvider,
)
from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    Clock,
    InstrumentFetchOutcome,
    ProviderBatch,
    ProviderBatchFailure,
    ProviderOperation,
)
from stock_desk.market.types import (
    Adjustment,
    BarFailure,
    BarQuery,
    BarResult,
    CapabilityReport,
    CapabilityState,
    Exchange,
    FailureReason,
    Instrument,
    MarketCapability,
    Period,
    ProviderId,
    TradingDay,
    UtcDatetime,
)


logger = logging.getLogger(__name__)


class SourceCategory(StrEnum):
    MINUTE_BARS = "minute_bars"
    DAILY_BARS = "daily_bars"
    WEEKLY_BARS = "weekly_bars"
    INSTRUMENTS = "instruments"
    TRADING_CALENDAR = "trading_calendar"


SOURCE_CATEGORY_ORDER = tuple(SourceCategory)


class _DiagnosticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DiagnosticGap(_DiagnosticModel):
    category: SourceCategory
    state: CapabilityState
    reason: FailureReason
    detail: str


class DiagnosticPermission(_DiagnosticModel):
    category: SourceCategory
    state: CapabilityState


class DiagnosticFallback(_DiagnosticModel):
    reason: FailureReason
    detail: str


class SourceDiagnostic(_DiagnosticModel):
    source: ProviderId
    status: CapabilityState
    capabilities: tuple[MarketCapability, ...]
    permissions: tuple[DiagnosticPermission, ...]
    available_periods: tuple[Period, ...]
    gaps: tuple[DiagnosticGap, ...]
    last_checked: UtcDatetime
    last_update: UtcDatetime | None
    data_cutoff: UtcDatetime | None
    fallback_reason: DiagnosticFallback | None


class DiagnosticProvider(Protocol):
    name: ProviderId

    def capabilities(self) -> CapabilityReport: ...


class TdxDiagnosticProvider(DiagnosticProvider, Protocol):
    def preflight(self) -> object: ...


class TushareDiagnosticProvider(DiagnosticProvider, Protocol):
    def fetch_bars(self, query: BarQuery) -> BarResult | BarFailure: ...

    def fetch_instruments(self) -> InstrumentFetchOutcome: ...

    def fetch_calendar(
        self,
        exchange: Exchange,
        start: date,
        end: date,
    ) -> CalendarFetchOutcome: ...


class DiagnosticProviderFactory(Protocol):
    def __call__(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
        clock: Clock,
    ) -> DiagnosticProvider: ...


_REASON_STATE = {
    FailureReason.PERMISSION_DENIED: CapabilityState.PERMISSION_DENIED,
    FailureReason.UNSUPPORTED: CapabilityState.UNSUPPORTED,
    FailureReason.TRANSIENT_FAILURE: CapabilityState.TRANSIENT_FAILURE,
    FailureReason.TIMEOUT: CapabilityState.TRANSIENT_FAILURE,
    FailureReason.PROVIDER_UNAVAILABLE: CapabilityState.UNAVAILABLE,
    FailureReason.MISSING: CapabilityState.UNAVAILABLE,
    FailureReason.NO_DATA: CapabilityState.UNAVAILABLE,
    FailureReason.CORRUPT: CapabilityState.UNAVAILABLE,
    FailureReason.INVALID_RESPONSE: CapabilityState.UNAVAILABLE,
}
_SAFE_DETAILS = {
    FailureReason.PERMISSION_DENIED: "provider permission was denied",
    FailureReason.UNSUPPORTED: "provider does not support this request",
    FailureReason.TRANSIENT_FAILURE: "provider failed transiently",
    FailureReason.TIMEOUT: "provider request timed out",
    FailureReason.PROVIDER_UNAVAILABLE: "provider is unavailable",
    FailureReason.MISSING: "provider configuration or data is missing",
    FailureReason.NO_DATA: "provider returned no data",
    FailureReason.CORRUPT: "provider data is corrupt",
    FailureReason.INVALID_RESPONSE: "provider response is invalid",
}
_TDX_DETAILS = {
    FailureReason.PERMISSION_DENIED: "TDX vipdoc access was denied",
    FailureReason.MISSING: "TDX vipdoc layout is missing",
    FailureReason.CORRUPT: "TDX vipdoc contents are corrupt",
    FailureReason.INVALID_RESPONSE: "TDX vipdoc layout is invalid",
    FailureReason.TRANSIENT_FAILURE: "TDX vipdoc changed during inspection",
    FailureReason.PROVIDER_UNAVAILABLE: "TDX vipdoc inspection is unavailable",
}
_CATEGORY_DETAILS = {
    SourceCategory.MINUTE_BARS: "provider does not support 60-minute bars",
    SourceCategory.DAILY_BARS: "provider does not support daily bars",
    SourceCategory.WEEKLY_BARS: "provider does not support weekly bars",
    SourceCategory.INSTRUMENTS: "provider does not support instruments",
    SourceCategory.TRADING_CALENDAR: "provider does not support trading calendar",
}
_CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
_TUSHARE_BAR_PROBES = (
    (
        SourceCategory.MINUTE_BARS,
        BarQuery(
            symbol="600000.SH",
            period=Period.MIN60,
            adjustment=Adjustment.NONE,
            start=datetime(2024, 1, 2, 9, 30, tzinfo=_CHINA_STANDARD_TIME),
            end=datetime(2024, 1, 2, 15, 0, tzinfo=_CHINA_STANDARD_TIME),
        ),
    ),
    (
        SourceCategory.DAILY_BARS,
        BarQuery(
            symbol="600000.SH",
            period=Period.DAY,
            adjustment=Adjustment.NONE,
            start=datetime(2024, 1, 2, tzinfo=_CHINA_STANDARD_TIME),
            end=datetime(2024, 1, 3, tzinfo=_CHINA_STANDARD_TIME),
        ),
    ),
    (
        SourceCategory.WEEKLY_BARS,
        BarQuery(
            symbol="600000.SH",
            period=Period.WEEK,
            adjustment=Adjustment.NONE,
            start=datetime(2024, 1, 1, tzinfo=_CHINA_STANDARD_TIME),
            end=datetime(2024, 1, 8, tzinfo=_CHINA_STANDARD_TIME),
        ),
    ),
)
_TUSHARE_CALENDAR_START = date(2024, 1, 2)
_TUSHARE_CALENDAR_END = date(2024, 1, 3)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_diagnostic_provider_factory(
    source: ProviderId,
    *,
    token: str | None,
    tdx_path: Path | None,
    clock: Clock,
) -> DiagnosticProvider:
    if source is ProviderId.TUSHARE:
        if token is None:
            raise ProviderPermissionDenied()
        return TushareProvider.from_sdk(token=token, clock=clock)
    if source is ProviderId.BAOSTOCK:
        return BaoStockProvider.from_sdk(clock=clock)
    if source is ProviderId.AKSHARE:
        return AkShareProvider.from_sdk(clock=clock)
    if source is ProviderId.TDX_LOCAL:
        if tdx_path is None:
            raise ProviderMissingCoverage()
        return TdxLocalProvider(root=tdx_path, clock=clock)
    raise ProviderUnsupported()


def _safe_detail(source: ProviderId, reason: FailureReason) -> str:
    if source is ProviderId.TDX_LOCAL and reason in _TDX_DETAILS:
        return _TDX_DETAILS[reason]
    return _SAFE_DETAILS.get(reason, "provider diagnostic failed safely")


def _supports(report: CapabilityReport, category: SourceCategory) -> bool:
    if category is SourceCategory.MINUTE_BARS:
        return (
            MarketCapability.BARS in report.capabilities
            and Period.MIN60 in report.available_periods
        )
    if category is SourceCategory.DAILY_BARS:
        return (
            MarketCapability.BARS in report.capabilities
            and Period.DAY in report.available_periods
        )
    if category is SourceCategory.WEEKLY_BARS:
        return (
            MarketCapability.BARS in report.capabilities
            and Period.WEEK in report.available_periods
        )
    if category is SourceCategory.INSTRUMENTS:
        return MarketCapability.INSTRUMENTS in report.capabilities
    return MarketCapability.TRADING_CALENDAR in report.capabilities


def _failure_diagnostic(
    *,
    source: ProviderId,
    reason: FailureReason,
    detail: str,
    checked_at: datetime,
) -> SourceDiagnostic:
    state = _REASON_STATE.get(reason, CapabilityState.UNAVAILABLE)
    gaps = tuple(
        DiagnosticGap(
            category=category,
            state=state,
            reason=reason,
            detail=detail,
        )
        for category in SOURCE_CATEGORY_ORDER
    )
    return SourceDiagnostic(
        source=source,
        status=state,
        capabilities=(),
        permissions=tuple(
            DiagnosticPermission(category=category, state=state)
            for category in SOURCE_CATEGORY_ORDER
        ),
        available_periods=(),
        gaps=gaps,
        last_checked=checked_at,
        last_update=None,
        data_cutoff=None,
        fallback_reason=DiagnosticFallback(reason=reason, detail=detail),
    )


def unavailable_diagnostic(
    source: ProviderId,
    *,
    reason: FailureReason,
    detail: str,
    checked_at: datetime,
) -> SourceDiagnostic:
    """Create a fixed failure report without constructing a provider."""
    return _failure_diagnostic(
        source=source,
        reason=reason,
        detail=detail,
        checked_at=checked_at,
    )


def _success_diagnostic(
    report: CapabilityReport,
    *,
    checked_at: datetime,
) -> SourceDiagnostic:
    gaps = tuple(
        DiagnosticGap(
            category=category,
            state=CapabilityState.UNSUPPORTED,
            reason=FailureReason.UNSUPPORTED,
            detail=_CATEGORY_DETAILS[category],
        )
        for category in SOURCE_CATEGORY_ORDER
        if not _supports(report, category)
    )
    gap_categories = {gap.category for gap in gaps}
    return SourceDiagnostic(
        source=report.source,
        status=report.state,
        capabilities=tuple(sorted(report.capabilities, key=lambda item: item.value)),
        permissions=tuple(
            DiagnosticPermission(
                category=category,
                state=(
                    CapabilityState.UNSUPPORTED
                    if category in gap_categories
                    else CapabilityState.AVAILABLE
                ),
            )
            for category in SOURCE_CATEGORY_ORDER
        ),
        available_periods=tuple(
            sorted(report.available_periods, key=lambda item: item.value)
        ),
        gaps=gaps,
        last_checked=checked_at,
        last_update=None,
        data_cutoff=report.data_cutoff,
        fallback_reason=None,
    )


def _probe_failure(
    category: SourceCategory,
    reason: FailureReason,
    *,
    source: ProviderId,
) -> tuple[DiagnosticPermission, DiagnosticGap]:
    state = _REASON_STATE.get(reason, CapabilityState.UNAVAILABLE)
    detail = _safe_detail(source, reason)
    return (
        DiagnosticPermission(category=category, state=state),
        DiagnosticGap(
            category=category,
            state=state,
            reason=reason,
            detail=detail,
        ),
    )


def _valid_tushare_calendar_batch(
    outcome: ProviderBatch[Instrument] | ProviderBatch[TradingDay],
) -> bool:
    expected_days = tuple(
        _TUSHARE_CALENDAR_START + timedelta(days=offset)
        for offset in range((_TUSHARE_CALENDAR_END - _TUSHARE_CALENDAR_START).days)
    )
    if len(outcome.items) != len(expected_days):
        return False
    actual_days: list[date] = []
    for item in outcome.items:
        if (
            type(item) is not TradingDay
            or item.exchange is not Exchange.SH
            or not _TUSHARE_CALENDAR_START <= item.day < _TUSHARE_CALENDAR_END
        ):
            return False
        actual_days.append(item.day)
    return len(actual_days) == len(frozenset(actual_days)) and set(actual_days) == set(
        expected_days
    )


def _generic_report_diagnostic(
    source: ProviderId,
    provider: DiagnosticProvider,
    report: object,
    *,
    checked_at: datetime,
) -> SourceDiagnostic:
    if (
        provider.name is not source
        or not isinstance(report, CapabilityReport)
        or report.source is not source
    ):
        return _failure_diagnostic(
            source=source,
            reason=FailureReason.INVALID_RESPONSE,
            detail=_safe_detail(source, FailureReason.INVALID_RESPONSE),
            checked_at=checked_at,
        )
    if report.state is CapabilityState.AVAILABLE:
        return _success_diagnostic(report, checked_at=checked_at)
    reason = next(
        (
            gap.reason
            for gap in report.gaps
            if gap.state is report.state
            and _REASON_STATE.get(gap.reason) is report.state
        ),
        FailureReason.PROVIDER_UNAVAILABLE,
    )
    return _failure_diagnostic(
        source=source,
        reason=reason,
        detail=_safe_detail(source, reason),
        checked_at=checked_at,
    )


def _tushare_diagnostic(
    provider: TushareDiagnosticProvider,
    *,
    checked_at: datetime,
) -> SourceDiagnostic:
    permissions: list[DiagnosticPermission] = []
    gaps: list[DiagnosticGap] = []
    successful_categories: set[SourceCategory] = set()
    successful_periods: set[Period] = set()
    successful_cutoffs: list[datetime] = []

    probes: list[tuple[SourceCategory, Callable[[], object]]] = [
        (category, partial(provider.fetch_bars, query))
        for category, query in _TUSHARE_BAR_PROBES
    ]
    probes.extend(
        (
            (SourceCategory.INSTRUMENTS, provider.fetch_instruments),
            (
                SourceCategory.TRADING_CALENDAR,
                lambda: provider.fetch_calendar(
                    Exchange.SH,
                    _TUSHARE_CALENDAR_START,
                    _TUSHARE_CALENDAR_END,
                ),
            ),
        )
    )

    for category, probe in probes:
        outcome: object
        try:
            outcome = probe()
        except ProviderClientError as error:
            permission, gap = _probe_failure(
                category,
                error.reason,
                source=ProviderId.TUSHARE,
            )
            permissions.append(permission)
            gaps.append(gap)
            continue
        except Exception:
            permission, gap = _probe_failure(
                category,
                FailureReason.PROVIDER_UNAVAILABLE,
                source=ProviderId.TUSHARE,
            )
            permissions.append(permission)
            gaps.append(gap)
            continue

        reason: FailureReason | None = None
        cutoff: datetime | None = None
        if category in {
            SourceCategory.MINUTE_BARS,
            SourceCategory.DAILY_BARS,
            SourceCategory.WEEKLY_BARS,
        }:
            if isinstance(outcome, BarFailure):
                expected_query = dict(_TUSHARE_BAR_PROBES)[category]
                reason = (
                    outcome.reason
                    if outcome.source is ProviderId.TUSHARE
                    and outcome.query == expected_query
                    else FailureReason.INVALID_RESPONSE
                )
            elif isinstance(outcome, BarResult):
                expected_query = dict(_TUSHARE_BAR_PROBES)[category]
                if (
                    outcome.provenance.source is not ProviderId.TUSHARE
                    or outcome.query != expected_query
                ):
                    reason = FailureReason.INVALID_RESPONSE
                else:
                    cutoff = outcome.provenance.data_cutoff
                    successful_periods.add(expected_query.period)
            else:
                reason = FailureReason.INVALID_RESPONSE
        elif isinstance(outcome, ProviderBatchFailure):
            if category is SourceCategory.INSTRUMENTS:
                attributed = (
                    outcome.source is ProviderId.TUSHARE
                    and outcome.operation is ProviderOperation.INSTRUMENTS
                )
            else:
                attributed = (
                    outcome.source is ProviderId.TUSHARE
                    and outcome.operation is ProviderOperation.CALENDAR
                    and outcome.exchange is Exchange.SH
                    and outcome.start == _TUSHARE_CALENDAR_START
                    and outcome.end == _TUSHARE_CALENDAR_END
                )
            reason = outcome.reason if attributed else FailureReason.INVALID_RESPONSE
        elif isinstance(outcome, ProviderBatch):
            attributed = outcome.provenance.source is ProviderId.TUSHARE
            if category is SourceCategory.INSTRUMENTS:
                attributed = attributed and all(
                    type(item) is Instrument for item in outcome.items
                )
            else:
                attributed = attributed and _valid_tushare_calendar_batch(outcome)
            if not attributed:
                reason = FailureReason.INVALID_RESPONSE
            else:
                cutoff = outcome.provenance.data_cutoff
        else:
            reason = FailureReason.INVALID_RESPONSE

        if reason is not None:
            permission, gap = _probe_failure(
                category,
                reason,
                source=ProviderId.TUSHARE,
            )
            permissions.append(permission)
            gaps.append(gap)
            continue
        permissions.append(
            DiagnosticPermission(category=category, state=CapabilityState.AVAILABLE)
        )
        successful_categories.add(category)
        if cutoff is not None:
            successful_cutoffs.append(cutoff)

    capabilities: set[MarketCapability] = set()
    if successful_categories.intersection(
        {
            SourceCategory.MINUTE_BARS,
            SourceCategory.DAILY_BARS,
            SourceCategory.WEEKLY_BARS,
        }
    ):
        capabilities.add(MarketCapability.BARS)
    if SourceCategory.INSTRUMENTS in successful_categories:
        capabilities.add(MarketCapability.INSTRUMENTS)
    if SourceCategory.TRADING_CALENDAR in successful_categories:
        capabilities.add(MarketCapability.TRADING_CALENDAR)

    primary_gap = gaps[0] if gaps else None
    return SourceDiagnostic(
        source=ProviderId.TUSHARE,
        status=(
            CapabilityState.AVAILABLE if primary_gap is None else primary_gap.state
        ),
        capabilities=tuple(sorted(capabilities, key=lambda item: item.value)),
        permissions=tuple(permissions),
        available_periods=tuple(
            sorted(successful_periods, key=lambda item: item.value)
        ),
        gaps=tuple(gaps),
        last_checked=checked_at,
        last_update=None,
        data_cutoff=(
            min(successful_cutoffs) if not gaps and successful_cutoffs else None
        ),
        fallback_reason=(
            None
            if primary_gap is None
            else DiagnosticFallback(
                reason=primary_gap.reason,
                detail=primary_gap.detail,
            )
        ),
    )


def diagnose_source(
    source: ProviderId,
    *,
    token: str | None,
    tdx_path: Path | None,
    factory: DiagnosticProviderFactory = default_diagnostic_provider_factory,
    clock: Callable[[], datetime] = _utc_now,
) -> SourceDiagnostic:
    """Inspect one provider while keeping all unsafe context out of results/logs."""
    provisional_checked_at = datetime(1990, 1, 1, tzinfo=timezone.utc)
    provider: DiagnosticProvider | None = None
    result: SourceDiagnostic | None = None
    try:
        provider = factory(
            source,
            token=token,
            tdx_path=tdx_path,
            clock=clock,
        )
        if provider.name is not source:
            result = _failure_diagnostic(
                source=source,
                reason=FailureReason.INVALID_RESPONSE,
                detail=_safe_detail(source, FailureReason.INVALID_RESPONSE),
                checked_at=provisional_checked_at,
            )
        elif source is ProviderId.TUSHARE:
            result = _tushare_diagnostic(
                cast(TushareDiagnosticProvider, provider),
                checked_at=provisional_checked_at,
            )
        elif source is ProviderId.TDX_LOCAL:
            outcome = cast(TdxDiagnosticProvider, provider).preflight()
            if isinstance(outcome, TdxInspectionFailure):
                result = _failure_diagnostic(
                    source=source,
                    reason=outcome.reason,
                    detail=_safe_detail(source, outcome.reason),
                    checked_at=provisional_checked_at,
                )
            else:
                result = _generic_report_diagnostic(
                    source,
                    provider,
                    provider.capabilities(),
                    checked_at=provisional_checked_at,
                )
        else:
            result = _generic_report_diagnostic(
                source,
                provider,
                provider.capabilities(),
                checked_at=provisional_checked_at,
            )
    except ProviderClientError as error:
        reason = error.reason
        result = _failure_diagnostic(
            source=source,
            reason=reason,
            detail=_safe_detail(source, reason),
            checked_at=provisional_checked_at,
        )
    except Exception:
        result = _failure_diagnostic(
            source=source,
            reason=FailureReason.PROVIDER_UNAVAILABLE,
            detail="provider diagnostic failed safely",
            checked_at=provisional_checked_at,
        )
    finally:
        if provider is not None:
            try:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()
            except Exception:
                result = _failure_diagnostic(
                    source=source,
                    reason=FailureReason.PROVIDER_UNAVAILABLE,
                    detail="provider diagnostic failed safely",
                    checked_at=provisional_checked_at,
                )
    if result is None:
        result = _failure_diagnostic(
            source=source,
            reason=FailureReason.PROVIDER_UNAVAILABLE,
            detail="provider diagnostic failed safely",
            checked_at=provisional_checked_at,
        )
    completed_at = clock()
    result = SourceDiagnostic.model_validate(
        {**result.model_dump(), "last_checked": completed_at}
    )
    if result.status is not CapabilityState.AVAILABLE:
        logger.warning(
            "Source diagnostic failed source=%s reason=%s",
            source.value,
            result.fallback_reason.reason.value
            if result.fallback_reason is not None
            else FailureReason.PROVIDER_UNAVAILABLE.value,
        )
    return result


__all__ = [
    "DiagnosticFallback",
    "DiagnosticGap",
    "DiagnosticPermission",
    "DiagnosticProviderFactory",
    "SOURCE_CATEGORY_ORDER",
    "SourceCategory",
    "SourceDiagnostic",
    "default_diagnostic_provider_factory",
    "diagnose_source",
    "unavailable_diagnostic",
]
