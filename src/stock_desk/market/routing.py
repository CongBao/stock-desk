from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from _thread import RLock
from types import MappingProxyType
from typing import Self
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from stock_desk.market.provenance import (
    BarRoutingRequest,
    CalendarRoutingRequest,
    ExecutionStatusRoutingRequest,
    RoutedBarFailure,
    RoutedBarSuccess,
    RoutedCalendarFailure,
    RoutedCalendarSuccess,
    RoutedExecutionStatusFailure,
    RoutedExecutionStatusSuccess,
    RoutedInstrumentFailure,
    RoutedInstrumentSuccess,
    RouterBatchFailure,
    InstrumentRoutingRequest,
    RoutingAttempt,
    RoutingDecision,
    RoutingRequest,
    RoutingManifest,
    derive_source_transition,
    make_failure_audit,
    make_routing_manifest,
)
from stock_desk.market.execution_status import (
    ExecutionStatusQuery,
    ExecutionStatusSnapshot,
)
from stock_desk.market.providers.execution_status import ExecutionStatusFailure
from stock_desk.market.providers.base import (
    ExecutionStatusProvider,
    MarketDataProvider,
    ProviderBatch,
    ProviderBatchFailure,
    ProviderClientError,
    ProviderOperation,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    BarFailure,
    BarQuery,
    BarResult,
    BAR_SOURCE_PROVIDER_IDS,
    CapabilityGap,
    CapabilityReport,
    CapabilityState,
    Exchange,
    FailureReason,
    MarketCapability,
    Period,
    Instrument,
    ProviderId,
    TradingDay,
)


class SourcePriorities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    bars: tuple[ProviderId, ...] = BAR_SOURCE_PROVIDER_IDS
    daily_bars: tuple[ProviderId, ...] | None = None
    weekly_bars: tuple[ProviderId, ...] | None = None
    minute_bars: tuple[ProviderId, ...] | None = None
    instruments: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
    )
    trading_calendar: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.BAOSTOCK,
    )
    execution_status: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.BAOSTOCK,
    )

    @model_validator(mode="after")
    def validate_unique_categories(self) -> Self:
        for category in (
            "bars",
            "daily_bars",
            "weekly_bars",
            "minute_bars",
            "instruments",
            "trading_calendar",
            "execution_status",
        ):
            values = getattr(self, category)
            if values is None:
                continue
            if len(values) != len(frozenset(values)):
                raise ValueError(f"{category} priority contains a duplicate provider")
        return self

    def for_period(self, period: object) -> tuple[ProviderId, ...]:
        if period is Period.DAY:
            return self.daily_bars or self.bars
        if period is Period.WEEK:
            return self.weekly_bars or self.bars
        if period is Period.MIN60:
            return self.minute_bars or self.bars
        raise ValueError("unsupported market bar period")

    def for_category(self, category: MarketCapability) -> tuple[ProviderId, ...]:
        if category is MarketCapability.BARS:
            return self.bars
        if category is MarketCapability.INSTRUMENTS:
            return self.instruments
        if category is MarketCapability.TRADING_CALENDAR:
            return self.trading_calendar
        return self.execution_status


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    provider: MarketDataProvider
    lock: RLock


_SAFE_CAPABILITY_DETAILS = {
    FailureReason.PERMISSION_DENIED: "provider permission was denied",
    FailureReason.UNSUPPORTED: "provider does not support this request",
    FailureReason.MISSING: "provider response does not cover the request",
    FailureReason.NO_DATA: "provider returned no data",
    FailureReason.PROVIDER_UNAVAILABLE: "provider is unavailable",
    FailureReason.TRANSIENT_FAILURE: "provider failed transiently",
    FailureReason.TIMEOUT: "provider request timed out",
    FailureReason.CORRUPT: "provider data is corrupt",
    FailureReason.INVALID_RESPONSE: "provider response is invalid",
}


def _safe_capability_reason(error: Exception) -> FailureReason:
    if isinstance(error, ProviderClientError):
        try:
            reason = error.reason
        except Exception:
            return FailureReason.INVALID_RESPONSE
        if type(reason) is not FailureReason or reason is FailureReason.NO_PROVIDER:
            return FailureReason.INVALID_RESPONSE
        return reason
    if isinstance(error, TimeoutError):
        return FailureReason.TIMEOUT
    return FailureReason.INVALID_RESPONSE


def _capability_state(reason: FailureReason) -> CapabilityState:
    if reason is FailureReason.PERMISSION_DENIED:
        return CapabilityState.PERMISSION_DENIED
    if reason is FailureReason.UNSUPPORTED:
        return CapabilityState.UNSUPPORTED
    if reason in {FailureReason.TRANSIENT_FAILURE, FailureReason.TIMEOUT}:
        return CapabilityState.TRANSIENT_FAILURE
    return CapabilityState.UNAVAILABLE


def _failed_capability_report(
    source: ProviderId,
    reason: FailureReason,
) -> CapabilityReport:
    state = _capability_state(reason)
    return CapabilityReport(
        source=source,
        state=state,
        capabilities=frozenset(),
        available_periods=frozenset(),
        available_adjustments=frozenset(),
        markets=frozenset(),
        data_cutoff=None,
        gaps=tuple(
            CapabilityGap(
                capability=category,
                state=state,
                reason=reason,
                detail=_SAFE_CAPABILITY_DETAILS[reason],
            )
            for category in MarketCapability
        ),
    )


def _category_capability_reason(
    report: CapabilityReport,
    category: MarketCapability,
) -> FailureReason | None:
    for gap in report.gaps:
        if gap.capability is category:
            return gap.reason
    if category not in report.capabilities:
        return FailureReason.INVALID_RESPONSE
    if report.state is not CapabilityState.AVAILABLE:
        return FailureReason.INVALID_RESPONSE
    return None


def _validated_previous_manifest(
    previous: RoutingManifest | None,
    *,
    category: MarketCapability,
    request: RoutingRequest,
) -> RoutingManifest | None:
    if previous is None:
        return None
    validated = RoutingManifest.model_validate(previous.model_dump(mode="python"))
    if validated.category is not category or validated.request != request:
        raise ValueError("previous manifest must match the canonical routing request")
    return validated


class SourceRouter:
    def __init__(
        self,
        entries: Sequence[tuple[ProviderId, MarketDataProvider]],
        *,
        priorities: SourcePriorities | None = None,
    ) -> None:
        copied: dict[ProviderId, _RegisteredProvider] = {}
        seen_names: set[ProviderId] = set()
        seen_instances: list[MarketDataProvider] = []
        for entry in entries:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("registry entries must be provider pairs")
            key, provider = entry
            if type(key) is not ProviderId:
                raise ValueError("registry key must be a ProviderId")
            if any(provider is seen for seen in seen_instances):
                raise ValueError("same provider instance cannot be registered twice")
            if key in copied:
                raise ValueError("duplicate registry key")
            try:
                name = provider.name
            except Exception:
                raise ValueError("provider name is invalid") from None
            if type(name) is not ProviderId:
                raise ValueError("provider name must be a ProviderId")
            if name in seen_names:
                raise ValueError("duplicate provider name")
            if key is not name:
                raise ValueError("registry key/name mismatch")
            if not isinstance(provider, MarketDataProvider):
                raise ValueError("registry value does not implement provider contract")
            copied[key] = _RegisteredProvider(provider=provider, lock=RLock())
            seen_names.add(name)
            seen_instances.append(provider)
        self._registry = MappingProxyType(copied)
        configured = priorities or SourcePriorities()
        self._priorities = SourcePriorities.model_validate(
            configured.model_dump(mode="python")
        )

    def priorities(self) -> SourcePriorities:
        return self._priorities

    def capability_reports(self) -> tuple[CapabilityReport, ...]:
        reports: list[CapabilityReport] = []
        for source, registration in self._registry.items():
            with registration.lock:
                report, _reason = self._capability_report(source, registration)
            reports.append(report)
        return tuple(reports)

    @staticmethod
    def _capability_report(
        source: ProviderId,
        registration: _RegisteredProvider,
    ) -> tuple[CapabilityReport, FailureReason | None]:
        try:
            if registration.provider.name is not source:
                raise ValueError("provider name changed")
            raw = registration.provider.capabilities()
            if not isinstance(raw, CapabilityReport):
                raise TypeError("capability report has wrong type")
            report = CapabilityReport.model_validate(raw.model_dump(mode="python"))
            if report.source is not source:
                raise ValueError("capability report source mismatch")
        except Exception as error:
            reason = _safe_capability_reason(error)
            return _failed_capability_report(source, reason), reason
        return report, None

    @staticmethod
    def _supports_bars(report: CapabilityReport, query: BarQuery) -> bool:
        exchange = (
            Exchange.SH
            if query.symbol == "000001.SS"
            else Exchange(query.symbol.rsplit(".", maxsplit=1)[1])
        )
        return (
            MarketCapability.BARS in report.capabilities
            and query.period in report.available_periods
            and query.adjustment in report.available_adjustments
            and exchange in report.markets
        )

    def fetch_bars(
        self,
        query: BarQuery,
        *,
        previous_manifest: RoutingManifest | None = None,
    ) -> RoutedBarSuccess | RoutedBarFailure:
        category = MarketCapability.BARS
        priority = self._priorities.for_period(query.period)
        attempts: list[RoutingAttempt] = []
        request = BarRoutingRequest(query=query)
        previous_manifest = _validated_previous_manifest(
            previous_manifest,
            category=category,
            request=request,
        )

        def record(
            source: ProviderId,
            decision: RoutingDecision,
            reason: FailureReason,
        ) -> None:
            attempts.append(
                RoutingAttempt.create(
                    ordinal=len(attempts) + 1,
                    source=source,
                    category=category,
                    decision=decision,
                    reason=reason,
                )
            )

        for source in priority:
            registration = self._registry.get(source)
            if registration is None:
                record(
                    source,
                    RoutingDecision.REGISTRY_MISSING,
                    FailureReason.PROVIDER_UNAVAILABLE,
                )
                continue

            with registration.lock:
                report, capability_failure = self._capability_report(
                    source,
                    registration,
                )
                if capability_failure is not None:
                    record(
                        source,
                        RoutingDecision.CAPABILITY_FAILURE,
                        capability_failure,
                    )
                    continue
                capability_reason = _category_capability_reason(report, category)
                if capability_reason is not None:
                    record(
                        source,
                        (
                            RoutingDecision.CAPABILITY_SKIP
                            if capability_reason is FailureReason.UNSUPPORTED
                            else RoutingDecision.CAPABILITY_FAILURE
                        ),
                        capability_reason,
                    )
                    continue
                if not self._supports_bars(report, query):
                    record(
                        source,
                        RoutingDecision.CAPABILITY_SKIP,
                        FailureReason.UNSUPPORTED,
                    )
                    continue
                try:
                    raw = registration.provider.fetch_bars(query)
                    if registration.provider.name is not source:
                        raise ValueError("provider name changed")
                    if isinstance(raw, BarResult):
                        outcome: BarResult | BarFailure = BarResult.model_validate(
                            raw.model_dump(mode="python")
                        )
                    elif isinstance(raw, BarFailure):
                        outcome = BarFailure.model_validate(
                            raw.model_dump(mode="python")
                        )
                    else:
                        raise TypeError("bar outcome has wrong type")
                except Exception as error:
                    record(
                        source,
                        RoutingDecision.FETCH_FAILURE,
                        _safe_capability_reason(error),
                    )
                    continue

            if outcome.query != query:
                record(
                    source,
                    RoutingDecision.FETCH_FAILURE,
                    FailureReason.INVALID_RESPONSE,
                )
                continue
            if isinstance(outcome, BarFailure):
                if (
                    outcome.source is not source
                    or outcome.reason is FailureReason.NO_PROVIDER
                ):
                    reason = FailureReason.INVALID_RESPONSE
                else:
                    reason = outcome.reason
                record(source, RoutingDecision.FETCH_FAILURE, reason)
                continue
            expected_version = dataset_version(
                source=source,
                operation="bars",
                request={"query": query},
                data_cutoff=outcome.provenance.data_cutoff,
                items=outcome.bars,
            )
            if (
                outcome.provenance.source is not source
                or outcome.provenance.dataset_version != expected_version
            ):
                record(
                    source,
                    RoutingDecision.FETCH_FAILURE,
                    FailureReason.INVALID_RESPONSE,
                )
                continue
            transition = derive_source_transition(
                previous=previous_manifest,
                category=category,
                request=request,
                priority=priority,
                selected_source=source,
                upstream_dataset_version=outcome.provenance.dataset_version,
                observed_at=None,
            )
            manifest = make_routing_manifest(
                category=category,
                request=request,
                priority=priority,
                attempts=tuple(attempts),
                selected_source=source,
                upstream_dataset_version=outcome.provenance.dataset_version,
                upstream_fetched_at=outcome.provenance.fetched_at,
                upstream_data_cutoff=outcome.provenance.data_cutoff,
                upstream_adjustment=outcome.provenance.adjustment,
                transition=transition,
            )
            return RoutedBarSuccess(result=outcome, manifest=manifest)

        audit = make_failure_audit(
            category=category,
            request=request,
            priority=priority,
            attempts=tuple(attempts),
        )
        failure = BarFailure(
            query=query,
            source=None,
            reason=FailureReason.NO_PROVIDER,
            failed_start=query.start,
            failed_end=query.end,
            detail="no configured provider can satisfy this query",
        )
        return RoutedBarFailure(failure=failure, audit=audit)

    def fetch_instruments(
        self,
        *,
        previous_manifest: RoutingManifest | None = None,
    ) -> RoutedInstrumentSuccess | RoutedInstrumentFailure:
        category = MarketCapability.INSTRUMENTS
        priority = self._priorities.instruments
        attempts: list[RoutingAttempt] = []
        request = InstrumentRoutingRequest()
        previous_manifest = _validated_previous_manifest(
            previous_manifest,
            category=category,
            request=request,
        )

        def record(
            source: ProviderId,
            decision: RoutingDecision,
            reason: FailureReason,
        ) -> None:
            attempts.append(
                RoutingAttempt.create(
                    ordinal=len(attempts) + 1,
                    source=source,
                    category=category,
                    decision=decision,
                    reason=reason,
                )
            )

        for source in priority:
            registration = self._registry.get(source)
            if registration is None:
                record(
                    source,
                    RoutingDecision.REGISTRY_MISSING,
                    FailureReason.PROVIDER_UNAVAILABLE,
                )
                continue
            with registration.lock:
                report, capability_failure = self._capability_report(
                    source,
                    registration,
                )
                if capability_failure is not None:
                    record(
                        source,
                        RoutingDecision.CAPABILITY_FAILURE,
                        capability_failure,
                    )
                    continue
                capability_reason = _category_capability_reason(report, category)
                if capability_reason is not None:
                    record(
                        source,
                        (
                            RoutingDecision.CAPABILITY_SKIP
                            if capability_reason is FailureReason.UNSUPPORTED
                            else RoutingDecision.CAPABILITY_FAILURE
                        ),
                        capability_reason,
                    )
                    continue
                try:
                    raw = registration.provider.fetch_instruments()
                    if registration.provider.name is not source:
                        raise ValueError("provider name changed")
                    if isinstance(raw, ProviderBatch):
                        outcome: ProviderBatch[Instrument] | ProviderBatchFailure = (
                            ProviderBatch[Instrument].model_validate(
                                raw.model_dump(mode="python")
                            )
                        )
                    elif isinstance(raw, ProviderBatchFailure):
                        outcome = ProviderBatchFailure.model_validate(
                            raw.model_dump(mode="python")
                        )
                    else:
                        raise TypeError("instrument outcome has wrong type")
                except Exception as error:
                    record(
                        source,
                        RoutingDecision.FETCH_FAILURE,
                        _safe_capability_reason(error),
                    )
                    continue

            if isinstance(outcome, ProviderBatchFailure):
                if (
                    outcome.source is not source
                    or outcome.operation is not ProviderOperation.INSTRUMENTS
                    or outcome.exchange is not None
                    or outcome.start is not None
                    or outcome.end is not None
                    or outcome.reason is FailureReason.NO_PROVIDER
                ):
                    reason = FailureReason.INVALID_RESPONSE
                else:
                    reason = outcome.reason
                record(source, RoutingDecision.FETCH_FAILURE, reason)
                continue
            symbols = tuple(item.symbol for item in outcome.items)
            expected_version = dataset_version(
                source=source,
                operation=ProviderOperation.INSTRUMENTS.value,
                request={},
                data_cutoff=outcome.provenance.data_cutoff,
                items=outcome.items,
            )
            if (
                outcome.provenance.source is not source
                or symbols != tuple(sorted(frozenset(symbols)))
                or outcome.provenance.dataset_version != expected_version
            ):
                record(
                    source,
                    RoutingDecision.FETCH_FAILURE,
                    FailureReason.INVALID_RESPONSE,
                )
                continue
            transition = derive_source_transition(
                previous=previous_manifest,
                category=category,
                request=request,
                priority=priority,
                selected_source=source,
                upstream_dataset_version=outcome.provenance.dataset_version,
                observed_at=outcome.provenance.fetched_at,
            )
            manifest = make_routing_manifest(
                category=category,
                request=request,
                priority=priority,
                attempts=tuple(attempts),
                selected_source=source,
                upstream_dataset_version=outcome.provenance.dataset_version,
                upstream_fetched_at=outcome.provenance.fetched_at,
                upstream_data_cutoff=outcome.provenance.data_cutoff,
                upstream_adjustment=None,
                transition=transition,
            )
            return RoutedInstrumentSuccess(batch=outcome, manifest=manifest)

        audit = make_failure_audit(
            category=category,
            request=request,
            priority=priority,
            attempts=tuple(attempts),
        )
        failure = RouterBatchFailure.no_provider(category=category)
        return RoutedInstrumentFailure(failure=failure, audit=audit)

    def fetch_calendar(
        self,
        exchange: Exchange,
        start: date,
        end: date,
        *,
        previous_manifest: RoutingManifest | None = None,
    ) -> RoutedCalendarSuccess | RoutedCalendarFailure:
        category = MarketCapability.TRADING_CALENDAR
        priority = self._priorities.trading_calendar
        attempts: list[RoutingAttempt] = []
        request = CalendarRoutingRequest(exchange=exchange, start=start, end=end)
        previous_manifest = _validated_previous_manifest(
            previous_manifest,
            category=category,
            request=request,
        )

        def record(
            source: ProviderId,
            decision: RoutingDecision,
            reason: FailureReason,
        ) -> None:
            attempts.append(
                RoutingAttempt.create(
                    ordinal=len(attempts) + 1,
                    source=source,
                    category=category,
                    decision=decision,
                    reason=reason,
                )
            )

        for source in priority:
            registration = self._registry.get(source)
            if registration is None:
                record(
                    source,
                    RoutingDecision.REGISTRY_MISSING,
                    FailureReason.PROVIDER_UNAVAILABLE,
                )
                continue
            with registration.lock:
                report, capability_failure = self._capability_report(
                    source,
                    registration,
                )
                if capability_failure is not None:
                    record(
                        source,
                        RoutingDecision.CAPABILITY_FAILURE,
                        capability_failure,
                    )
                    continue
                capability_reason = _category_capability_reason(report, category)
                if capability_reason is not None:
                    record(
                        source,
                        (
                            RoutingDecision.CAPABILITY_SKIP
                            if capability_reason is FailureReason.UNSUPPORTED
                            else RoutingDecision.CAPABILITY_FAILURE
                        ),
                        capability_reason,
                    )
                    continue
                try:
                    raw = registration.provider.fetch_calendar(exchange, start, end)
                    if registration.provider.name is not source:
                        raise ValueError("provider name changed")
                    if isinstance(raw, ProviderBatch):
                        outcome: ProviderBatch[TradingDay] | ProviderBatchFailure = (
                            ProviderBatch[TradingDay].model_validate(
                                raw.model_dump(mode="python")
                            )
                        )
                    elif isinstance(raw, ProviderBatchFailure):
                        outcome = ProviderBatchFailure.model_validate(
                            raw.model_dump(mode="python")
                        )
                    else:
                        raise TypeError("calendar outcome has wrong type")
                except Exception as error:
                    record(
                        source,
                        RoutingDecision.FETCH_FAILURE,
                        _safe_capability_reason(error),
                    )
                    continue

            if isinstance(outcome, ProviderBatchFailure):
                if (
                    outcome.source is not source
                    or outcome.operation is not ProviderOperation.CALENDAR
                    or outcome.exchange is not exchange
                    or outcome.start != start
                    or outcome.end != end
                    or outcome.reason is FailureReason.NO_PROVIDER
                ):
                    reason = FailureReason.INVALID_RESPONSE
                else:
                    reason = outcome.reason
                record(source, RoutingDecision.FETCH_FAILURE, reason)
                continue
            expected_days = tuple(
                start + timedelta(days=offset) for offset in range((end - start).days)
            )
            expected_version = dataset_version(
                source=source,
                operation=ProviderOperation.CALENDAR.value,
                request={"exchange": exchange, "start": start, "end": end},
                data_cutoff=outcome.provenance.data_cutoff,
                items=outcome.items,
            )
            if (
                outcome.provenance.source is not source
                or tuple(item.day for item in outcome.items) != expected_days
                or any(item.exchange is not exchange for item in outcome.items)
                or outcome.provenance.dataset_version != expected_version
            ):
                record(
                    source,
                    RoutingDecision.FETCH_FAILURE,
                    FailureReason.INVALID_RESPONSE,
                )
                continue
            transition = derive_source_transition(
                previous=previous_manifest,
                category=category,
                request=request,
                priority=priority,
                selected_source=source,
                upstream_dataset_version=outcome.provenance.dataset_version,
                observed_at=None,
            )
            manifest = make_routing_manifest(
                category=category,
                request=request,
                priority=priority,
                attempts=tuple(attempts),
                selected_source=source,
                upstream_dataset_version=outcome.provenance.dataset_version,
                upstream_fetched_at=outcome.provenance.fetched_at,
                upstream_data_cutoff=outcome.provenance.data_cutoff,
                upstream_adjustment=None,
                transition=transition,
            )
            return RoutedCalendarSuccess(batch=outcome, manifest=manifest)

        audit = make_failure_audit(
            category=category,
            request=request,
            priority=priority,
            attempts=tuple(attempts),
        )
        failure = RouterBatchFailure.no_provider(
            category=category,
            exchange=exchange,
            start=start,
            end=end,
        )
        return RoutedCalendarFailure(failure=failure, audit=audit)

    def fetch_execution_status(
        self,
        query: ExecutionStatusQuery,
        *,
        previous_manifest: RoutingManifest | None = None,
    ) -> RoutedExecutionStatusSuccess | RoutedExecutionStatusFailure:
        category = MarketCapability.EXECUTION_STATUS
        priority = self._priorities.execution_status
        attempts: list[RoutingAttempt] = []
        request = ExecutionStatusRoutingRequest(query=query)
        previous_manifest = _validated_previous_manifest(
            previous_manifest,
            category=category,
            request=request,
        )

        def record(
            source: ProviderId,
            decision: RoutingDecision,
            reason: FailureReason,
        ) -> None:
            attempts.append(
                RoutingAttempt.create(
                    ordinal=len(attempts) + 1,
                    source=source,
                    category=category,
                    decision=decision,
                    reason=reason,
                )
            )

        for source in priority:
            registration = self._registry.get(source)
            if registration is None:
                record(
                    source,
                    RoutingDecision.REGISTRY_MISSING,
                    FailureReason.PROVIDER_UNAVAILABLE,
                )
                continue
            with registration.lock:
                report, capability_failure = self._capability_report(
                    source, registration
                )
                if capability_failure is not None:
                    record(
                        source,
                        RoutingDecision.CAPABILITY_FAILURE,
                        capability_failure,
                    )
                    continue
                capability_reason = _category_capability_reason(report, category)
                if capability_reason is not None:
                    record(
                        source,
                        (
                            RoutingDecision.CAPABILITY_SKIP
                            if capability_reason is FailureReason.UNSUPPORTED
                            else RoutingDecision.CAPABILITY_FAILURE
                        ),
                        capability_reason,
                    )
                    continue
                try:
                    if not isinstance(registration.provider, ExecutionStatusProvider):
                        raise TypeError("provider execution-status contract is missing")
                    raw = registration.provider.fetch_execution_status(query)
                    if registration.provider.name is not source:
                        raise ValueError("provider name changed")
                    if isinstance(raw, ExecutionStatusSnapshot):
                        outcome: ExecutionStatusSnapshot | ExecutionStatusFailure = (
                            ExecutionStatusSnapshot.model_validate(
                                raw.model_dump(mode="python")
                            )
                        )
                    elif isinstance(raw, ExecutionStatusFailure):
                        outcome = ExecutionStatusFailure.model_validate(
                            raw.model_dump(mode="python")
                        )
                    else:
                        raise TypeError("execution-status outcome has wrong type")
                except Exception as error:
                    record(
                        source,
                        RoutingDecision.FETCH_FAILURE,
                        _safe_capability_reason(error),
                    )
                    continue

            if isinstance(outcome, ExecutionStatusFailure):
                reason = (
                    outcome.reason
                    if outcome.source is source
                    and outcome.query == query
                    and outcome.reason is not FailureReason.NO_PROVIDER
                    else FailureReason.INVALID_RESPONSE
                )
                record(source, RoutingDecision.FETCH_FAILURE, reason)
                continue
            if outcome.query != query or outcome.source is not source:
                record(
                    source,
                    RoutingDecision.FETCH_FAILURE,
                    FailureReason.INVALID_RESPONSE,
                )
                continue
            transition = derive_source_transition(
                previous=previous_manifest,
                category=category,
                request=request,
                priority=priority,
                selected_source=source,
                upstream_dataset_version=outcome.dataset_version,
                observed_at=None,
            )
            manifest = make_routing_manifest(
                category=category,
                request=request,
                priority=priority,
                attempts=tuple(attempts),
                selected_source=source,
                upstream_dataset_version=outcome.dataset_version,
                upstream_fetched_at=outcome.fetched_at,
                upstream_data_cutoff=outcome.data_cutoff,
                upstream_adjustment=None,
                transition=transition,
            )
            return RoutedExecutionStatusSuccess(result=outcome, manifest=manifest)

        audit = make_failure_audit(
            category=category,
            request=request,
            priority=priority,
            attempts=tuple(attempts),
        )
        return RoutedExecutionStatusFailure(
            query=query,
            reason=FailureReason.NO_PROVIDER,
            detail="no configured provider can satisfy this request",
            audit=audit,
        )
