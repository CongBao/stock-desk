from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from stock_desk.market.providers.base import ProviderBatch
from stock_desk.market.execution_status import (
    ExecutionStatusQuery,
    ExecutionStatusSnapshot,
)
from stock_desk.market.types import (
    Adjustment,
    BarFailure,
    BarQuery,
    BarResult,
    Exchange,
    FailureDetail,
    FailureReason,
    Instrument,
    MarketCapability,
    ProviderId,
    TradingDay,
    UtcDatetime,
)


ROUTING_MANIFEST_SCHEMA: Literal["stock-desk-routing-manifest-v1"] = (
    "stock-desk-routing-manifest-v1"
)
Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]


class RoutingDecision(StrEnum):
    REGISTRY_MISSING = "registry_missing"
    CAPABILITY_SKIP = "capability_skip"
    CAPABILITY_FAILURE = "capability_failure"
    FETCH_FAILURE = "fetch_failure"


class TransitionReason(StrEnum):
    FALLBACK_AFTER_FAILURE = "fallback_after_failure"
    HIGHER_PRIORITY_RECOVERED = "higher_priority_recovered"
    PRIORITY_CHANGED = "priority_changed"


class _FrozenRoutingModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class BarRoutingRequest(_FrozenRoutingModel):
    query: BarQuery


class InstrumentRoutingRequest(_FrozenRoutingModel):
    pass


class CalendarRoutingRequest(_FrozenRoutingModel):
    exchange: Exchange
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.start >= self.end:
            raise ValueError("calendar routing range must be nonempty")
        return self


class ExecutionStatusRoutingRequest(_FrozenRoutingModel):
    query: ExecutionStatusQuery


RoutingRequest: TypeAlias = (
    BarRoutingRequest
    | InstrumentRoutingRequest
    | CalendarRoutingRequest
    | ExecutionStatusRoutingRequest
)


def _request_type(category: MarketCapability) -> type[BaseModel]:
    request_types: dict[MarketCapability, type[BaseModel]] = {
        MarketCapability.BARS: BarRoutingRequest,
        MarketCapability.EXECUTION_STATUS: ExecutionStatusRoutingRequest,
        MarketCapability.INSTRUMENTS: InstrumentRoutingRequest,
        MarketCapability.TRADING_CALENDAR: CalendarRoutingRequest,
    }
    return request_types[category]


_FETCH_FAILURE_DETAILS = {
    FailureReason.PERMISSION_DENIED: "provider permission was denied",
    FailureReason.UNSUPPORTED: "provider does not support this request",
    FailureReason.MISSING: "provider response does not cover the full request",
    FailureReason.NO_DATA: "provider returned no data",
    FailureReason.PROVIDER_UNAVAILABLE: "provider is unavailable",
    FailureReason.TRANSIENT_FAILURE: "provider failed transiently",
    FailureReason.TIMEOUT: "provider request timed out",
    FailureReason.CORRUPT: "provider data is corrupt",
    FailureReason.INVALID_RESPONSE: "provider response is invalid",
}


def _fixed_attempt_detail(
    decision: RoutingDecision,
    reason: FailureReason,
) -> str:
    if decision is RoutingDecision.REGISTRY_MISSING:
        if reason is not FailureReason.PROVIDER_UNAVAILABLE:
            raise ValueError("REGISTRY_MISSING requires PROVIDER_UNAVAILABLE")
        return "provider is not registered"
    if decision is RoutingDecision.CAPABILITY_SKIP:
        if reason is not FailureReason.UNSUPPORTED:
            raise ValueError("CAPABILITY_SKIP requires UNSUPPORTED")
        return "provider capability does not support this request"
    if decision is RoutingDecision.CAPABILITY_FAILURE:
        if reason is FailureReason.NO_PROVIDER:
            raise ValueError("CAPABILITY_FAILURE cannot use NO_PROVIDER")
        return _FETCH_FAILURE_DETAILS[reason]
    if reason is FailureReason.NO_PROVIDER:
        raise ValueError("routing attempt cannot use NO_PROVIDER")
    try:
        return _FETCH_FAILURE_DETAILS[reason]
    except KeyError:
        raise ValueError("FETCH_FAILURE has an unsupported reason") from None


class RoutingAttempt(_FrozenRoutingModel):
    ordinal: int = Field(ge=1)
    source: ProviderId
    category: MarketCapability
    decision: RoutingDecision
    reason: FailureReason
    detail: FailureDetail

    @classmethod
    def create(
        cls,
        *,
        ordinal: int,
        source: ProviderId,
        category: MarketCapability,
        decision: RoutingDecision,
        reason: FailureReason,
    ) -> Self:
        try:
            detail = _fixed_attempt_detail(decision, reason)
        except ValueError:
            detail = "invalid routing attempt"
        return cls(
            ordinal=ordinal,
            source=source,
            category=category,
            decision=decision,
            reason=reason,
            detail=detail,
        )

    @model_validator(mode="after")
    def validate_fixed_detail(self) -> Self:
        expected = _fixed_attempt_detail(self.decision, self.reason)
        if self.detail != expected:
            raise ValueError("routing attempt must use its fixed safe detail")
        return self


class SourceTransition(_FrozenRoutingModel):
    category: MarketCapability
    from_source: ProviderId
    to_source: ProviderId
    from_dataset_version: Sha256Digest
    to_dataset_version: Sha256Digest
    from_route_version: Sha256Digest
    effective_at: UtcDatetime | None
    calendar_start: date | None
    calendar_end: date | None
    reason: TransitionReason

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        if self.from_source is self.to_source:
            raise ValueError("source transition must change source")
        if self.from_dataset_version == self.to_dataset_version:
            raise ValueError("source transition must change dataset version")
        if self.category in {
            MarketCapability.TRADING_CALENDAR,
            MarketCapability.EXECUTION_STATUS,
        }:
            if (
                self.effective_at is not None
                or self.calendar_start is None
                or self.calendar_end is None
                or self.calendar_start >= self.calendar_end
            ):
                raise ValueError("calendar boundary requires a nonempty date range")
        elif (
            self.effective_at is None
            or self.calendar_start is not None
            or self.calendar_end is not None
        ):
            raise ValueError("instant boundary requires only effective_at")
        return self


def derive_source_transition(
    *,
    previous: RoutingManifest | None,
    category: MarketCapability,
    request: RoutingRequest,
    priority: tuple[ProviderId, ...],
    selected_source: ProviderId,
    upstream_dataset_version: str,
    observed_at: UtcDatetime | None,
) -> SourceTransition | None:
    if previous is None:
        return None
    previous = RoutingManifest.model_validate(previous.model_dump(mode="python"))
    if previous.category is not category:
        raise ValueError("previous routing manifest category does not match")
    if not isinstance(request, _request_type(category)):
        raise ValueError("transition request does not match its category")
    if previous.request != request:
        raise ValueError("previous and current canonical requests must match exactly")
    if previous.selected_source is selected_source:
        return None
    if len(priority) != len(frozenset(priority)) or selected_source not in priority:
        raise ValueError("transition priority must be unique and contain selection")
    if previous.priority != priority:
        reason = TransitionReason.PRIORITY_CHANGED
    elif priority.index(selected_source) < priority.index(previous.selected_source):
        reason = TransitionReason.HIGHER_PRIORITY_RECOVERED
    else:
        reason = TransitionReason.FALLBACK_AFTER_FAILURE

    effective_at: UtcDatetime | None
    calendar_start: date | None
    calendar_end: date | None
    if isinstance(request, BarRoutingRequest):
        effective_at = request.query.start
        calendar_start = None
        calendar_end = None
    elif isinstance(request, InstrumentRoutingRequest):
        if observed_at is None:
            raise ValueError("instrument transition requires its observation time")
        effective_at = observed_at
        calendar_start = None
        calendar_end = None
    elif isinstance(request, CalendarRoutingRequest):
        effective_at = None
        calendar_start = request.start
        calendar_end = request.end
    else:
        effective_at = None
        calendar_start = request.query.start
        calendar_end = request.query.end

    return SourceTransition(
        category=category,
        from_source=previous.selected_source,
        to_source=selected_source,
        from_dataset_version=previous.upstream_dataset_version,
        to_dataset_version=upstream_dataset_version,
        from_route_version=previous.route_version,
        effective_at=effective_at,
        calendar_start=calendar_start,
        calendar_end=calendar_end,
        reason=reason,
    )


class RoutingManifest(_FrozenRoutingModel):
    schema_version: Literal["stock-desk-routing-manifest-v1"] = ROUTING_MANIFEST_SCHEMA
    category: MarketCapability
    request: RoutingRequest
    priority: tuple[ProviderId, ...]
    attempts: tuple[RoutingAttempt, ...]
    selected_source: ProviderId
    upstream_dataset_version: Sha256Digest
    upstream_fetched_at: UtcDatetime
    upstream_data_cutoff: UtcDatetime
    upstream_adjustment: Adjustment | None
    route_version: Sha256Digest
    transition: SourceTransition | None = None

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if not isinstance(self.request, _request_type(self.category)):
            raise ValueError("routing manifest request does not match its category")
        if len(self.priority) != len(frozenset(self.priority)):
            raise ValueError("routing manifest priority contains a duplicate")
        if tuple(item.ordinal for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("routing attempt ordinals must be continuous")
        if any(item.category is not self.category for item in self.attempts):
            raise ValueError("routing attempt category must match manifest category")
        expected_sources = self.priority[: len(self.attempts)]
        if tuple(item.source for item in self.attempts) != expected_sources:
            raise ValueError("routing attempts must follow configured priority")
        selected_index = len(self.attempts)
        if (
            selected_index >= len(self.priority)
            or self.priority[selected_index] is not self.selected_source
        ):
            raise ValueError("selected source must follow routing attempts")
        if self.upstream_data_cutoff > self.upstream_fetched_at:
            raise ValueError("upstream cutoff cannot be later than fetched time")
        if self.category is MarketCapability.BARS:
            if (
                not isinstance(self.request, BarRoutingRequest)
                or self.upstream_adjustment is not self.request.query.adjustment
            ):
                raise ValueError("bar upstream adjustment must match routing request")
        elif self.upstream_adjustment is not None:
            raise ValueError("batch routing manifest cannot contain bar adjustment")
        if self.transition is not None:
            if (
                self.transition.category is not self.category
                or self.transition.to_source is not self.selected_source
                or self.transition.to_dataset_version != self.upstream_dataset_version
            ):
                raise ValueError("source transition must terminate at selected dataset")
            if isinstance(self.request, BarRoutingRequest):
                boundary_matches = (
                    self.transition.effective_at == self.request.query.start
                )
            elif isinstance(self.request, InstrumentRoutingRequest):
                boundary_matches = (
                    self.transition.effective_at == self.upstream_fetched_at
                )
            elif isinstance(self.request, CalendarRoutingRequest):
                boundary_matches = (
                    self.transition.calendar_start == self.request.start
                    and self.transition.calendar_end == self.request.end
                )
            else:
                boundary_matches = (
                    self.transition.calendar_start == self.request.query.start
                    and self.transition.calendar_end == self.request.query.end
                )
            if not boundary_matches:
                raise ValueError(
                    "source transition boundary must match current routing manifest"
                )
        expected_route_version = _canonical_route_version(
            _route_payload(
                category=self.category,
                request=self.request,
                priority=self.priority,
                attempts=self.attempts,
                selected_source=self.selected_source,
                upstream_dataset_version=self.upstream_dataset_version,
                upstream_data_cutoff=self.upstream_data_cutoff,
                upstream_adjustment=self.upstream_adjustment,
                transition=self.transition,
            )
        )
        if self.route_version != expected_route_version:
            raise ValueError("route_version does not match canonical routing payload")
        return self


class RoutingFailureAudit(_FrozenRoutingModel):
    schema_version: Literal["stock-desk-routing-manifest-v1"] = ROUTING_MANIFEST_SCHEMA
    category: MarketCapability
    request: RoutingRequest
    priority: tuple[ProviderId, ...]
    attempts: tuple[RoutingAttempt, ...]
    route_version: Sha256Digest

    @model_validator(mode="after")
    def validate_audit(self) -> Self:
        if not isinstance(self.request, _request_type(self.category)):
            raise ValueError("routing failure request does not match its category")
        if len(self.priority) != len(frozenset(self.priority)):
            raise ValueError("routing failure priority contains a duplicate")
        if tuple(item.ordinal for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("routing attempt ordinals must be continuous")
        if any(item.category is not self.category for item in self.attempts):
            raise ValueError("routing attempt category must match failure category")
        if tuple(item.source for item in self.attempts) != self.priority:
            raise ValueError("routing failure attempts must cover priority in order")
        expected_route_version = _canonical_route_version(
            _route_payload(
                category=self.category,
                request=self.request,
                priority=self.priority,
                attempts=self.attempts,
                selected_source=None,
                upstream_dataset_version=None,
                upstream_data_cutoff=None,
                upstream_adjustment=None,
                transition=None,
            )
        )
        if self.route_version != expected_route_version:
            raise ValueError("route_version does not match canonical routing payload")
        return self


def _canonical_route_version(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc)


def _route_payload(
    *,
    category: MarketCapability,
    request: RoutingRequest,
    priority: tuple[ProviderId, ...],
    attempts: tuple[RoutingAttempt, ...],
    selected_source: ProviderId | None,
    upstream_dataset_version: str | None,
    upstream_data_cutoff: UtcDatetime | None,
    upstream_adjustment: Adjustment | None,
    transition: SourceTransition | None,
) -> dict[str, Any]:
    return {
        "schema_version": ROUTING_MANIFEST_SCHEMA,
        "category": category.value,
        "request": request.model_dump(mode="json"),
        "priority": [source.value for source in priority],
        "attempts": [attempt.model_dump(mode="json") for attempt in attempts],
        "selected_source": selected_source.value
        if selected_source is not None
        else None,
        "upstream_dataset_version": upstream_dataset_version,
        "upstream_data_cutoff": (
            upstream_data_cutoff.isoformat().replace("+00:00", "Z")
            if upstream_data_cutoff is not None
            else None
        ),
        "upstream_adjustment": (
            upstream_adjustment.value if upstream_adjustment is not None else None
        ),
        "transition": (
            transition.model_dump(mode="json") if transition is not None else None
        ),
    }


def make_routing_manifest(
    *,
    category: MarketCapability,
    request: RoutingRequest,
    priority: tuple[ProviderId, ...],
    attempts: tuple[RoutingAttempt, ...],
    selected_source: ProviderId,
    upstream_dataset_version: str,
    upstream_fetched_at: UtcDatetime,
    upstream_data_cutoff: UtcDatetime,
    upstream_adjustment: Adjustment | None,
    transition: SourceTransition | None = None,
) -> RoutingManifest:
    canonical_fetched_at = _canonical_utc(upstream_fetched_at)
    canonical_data_cutoff = _canonical_utc(upstream_data_cutoff)
    route_version = _canonical_route_version(
        _route_payload(
            category=category,
            request=request,
            priority=priority,
            attempts=attempts,
            selected_source=selected_source,
            upstream_dataset_version=upstream_dataset_version,
            upstream_data_cutoff=canonical_data_cutoff,
            upstream_adjustment=upstream_adjustment,
            transition=transition,
        )
    )
    return RoutingManifest(
        category=category,
        request=request,
        priority=priority,
        attempts=attempts,
        selected_source=selected_source,
        upstream_dataset_version=upstream_dataset_version,
        upstream_fetched_at=canonical_fetched_at,
        upstream_data_cutoff=canonical_data_cutoff,
        upstream_adjustment=upstream_adjustment,
        route_version=route_version,
        transition=transition,
    )


def make_failure_audit(
    *,
    category: MarketCapability,
    request: RoutingRequest,
    priority: tuple[ProviderId, ...],
    attempts: tuple[RoutingAttempt, ...],
) -> RoutingFailureAudit:
    route_version = _canonical_route_version(
        _route_payload(
            category=category,
            request=request,
            priority=priority,
            attempts=attempts,
            selected_source=None,
            upstream_dataset_version=None,
            upstream_data_cutoff=None,
            upstream_adjustment=None,
            transition=None,
        )
    )
    return RoutingFailureAudit(
        category=category,
        request=request,
        priority=priority,
        attempts=attempts,
        route_version=route_version,
    )


class RoutedBarSuccess(_FrozenRoutingModel):
    result: BarResult
    manifest: RoutingManifest

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.manifest.category is not MarketCapability.BARS:
            raise ValueError("routed bar manifest must use bars category")
        if not isinstance(self.manifest.request, BarRoutingRequest):
            raise ValueError("routed bar manifest must use a bar request")
        if self.manifest.request.query != self.result.query:
            raise ValueError("routed bar manifest query must match result query")
        if self.manifest.selected_source is not self.result.provenance.source:
            raise ValueError("routed bar manifest source must match result source")
        if (
            self.manifest.upstream_dataset_version
            != self.result.provenance.dataset_version
        ):
            raise ValueError("routed bar manifest version must match result version")
        if self.manifest.upstream_fetched_at != self.result.provenance.fetched_at:
            raise ValueError("routed bar manifest fetched time must match result")
        if self.manifest.upstream_data_cutoff != self.result.provenance.data_cutoff:
            raise ValueError("routed bar manifest cutoff must match result")
        if self.manifest.upstream_adjustment is not self.result.provenance.adjustment:
            raise ValueError("routed bar manifest adjustment must match result")
        return self


class RoutedBarFailure(_FrozenRoutingModel):
    failure: BarFailure
    audit: RoutingFailureAudit

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.audit.category is not MarketCapability.BARS:
            raise ValueError("routed bar failure audit must use bars category")
        if not isinstance(self.audit.request, BarRoutingRequest):
            raise ValueError("routed bar failure audit must use a bar request")
        if self.audit.request.query != self.failure.query:
            raise ValueError("routed bar failure query must match audit request")
        if (
            self.failure.source is not None
            or self.failure.reason is not FailureReason.NO_PROVIDER
            or self.failure.failed_start != self.failure.query.start
            or self.failure.failed_end != self.failure.query.end
        ):
            raise ValueError("routed bar terminal failure must cover the full query")
        if self.failure.detail != "no configured provider can satisfy this query":
            raise ValueError(
                "routed bar terminal failure must use its fixed safe detail"
            )
        return self


_BATCH_TERMINAL_DETAIL = "no configured provider can satisfy this request"


class RouterBatchFailure(_FrozenRoutingModel):
    category: MarketCapability
    exchange: Exchange | None
    start: date | None
    end: date | None
    reason: FailureReason
    detail: FailureDetail

    @classmethod
    def no_provider(
        cls,
        *,
        category: MarketCapability,
        exchange: Exchange | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> Self:
        return cls(
            category=category,
            exchange=exchange,
            start=start,
            end=end,
            reason=FailureReason.NO_PROVIDER,
            detail=_BATCH_TERMINAL_DETAIL,
        )

    @model_validator(mode="after")
    def validate_terminal_failure(self) -> Self:
        if self.category is MarketCapability.BARS:
            raise ValueError("bar routing uses BarFailure")
        if self.reason is not FailureReason.NO_PROVIDER:
            raise ValueError("router batch failure requires NO_PROVIDER")
        if self.detail != _BATCH_TERMINAL_DETAIL:
            raise ValueError("router batch failure must use its fixed safe detail")
        if self.category is MarketCapability.INSTRUMENTS:
            if (
                self.exchange is not None
                or self.start is not None
                or self.end is not None
            ):
                raise ValueError("instrument routing failure has no request context")
        elif (
            self.exchange is None
            or self.start is None
            or self.end is None
            or self.start >= self.end
        ):
            raise ValueError("calendar routing failure requires its full date range")
        return self


class RoutedInstrumentSuccess(_FrozenRoutingModel):
    batch: ProviderBatch[Instrument]
    manifest: RoutingManifest

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if self.manifest.category is not MarketCapability.INSTRUMENTS or not isinstance(
            self.manifest.request, InstrumentRoutingRequest
        ):
            raise ValueError("routed instrument manifest must use instrument request")
        symbols = tuple(item.symbol for item in self.batch.items)
        if symbols != tuple(sorted(frozenset(symbols))):
            raise ValueError("routed instruments must have sorted unique symbols")
        if self.manifest.selected_source is not self.batch.provenance.source:
            raise ValueError("routed instrument source must match manifest source")
        if (
            self.manifest.upstream_dataset_version
            != self.batch.provenance.dataset_version
        ):
            raise ValueError("routed instrument version must match manifest version")
        if self.manifest.upstream_fetched_at != self.batch.provenance.fetched_at:
            raise ValueError("routed instrument fetched time must match manifest")
        if self.manifest.upstream_data_cutoff != self.batch.provenance.data_cutoff:
            raise ValueError("routed instrument cutoff must match manifest")
        return self


class RoutedCalendarSuccess(_FrozenRoutingModel):
    batch: ProviderBatch[TradingDay]
    manifest: RoutingManifest

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if (
            self.manifest.category is not MarketCapability.TRADING_CALENDAR
            or not isinstance(self.manifest.request, CalendarRoutingRequest)
        ):
            raise ValueError("routed calendar manifest must use calendar request")
        request = self.manifest.request
        expected_days = tuple(
            request.start + timedelta(days=offset)
            for offset in range((request.end - request.start).days)
        )
        if tuple(item.day for item in self.batch.items) != expected_days or any(
            item.exchange is not request.exchange for item in self.batch.items
        ):
            raise ValueError("routed calendar must cover every natural date in order")
        if self.manifest.selected_source is not self.batch.provenance.source:
            raise ValueError("routed calendar source must match manifest source")
        if (
            self.manifest.upstream_dataset_version
            != self.batch.provenance.dataset_version
        ):
            raise ValueError("routed calendar version must match manifest version")
        if self.manifest.upstream_fetched_at != self.batch.provenance.fetched_at:
            raise ValueError("routed calendar fetched time must match manifest")
        if self.manifest.upstream_data_cutoff != self.batch.provenance.data_cutoff:
            raise ValueError("routed calendar cutoff must match manifest")
        return self


class RoutedExecutionStatusSuccess(_FrozenRoutingModel):
    result: ExecutionStatusSnapshot
    manifest: RoutingManifest

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if (
            self.manifest.category is not MarketCapability.EXECUTION_STATUS
            or not isinstance(self.manifest.request, ExecutionStatusRoutingRequest)
            or self.manifest.request.query != self.result.query
        ):
            raise ValueError("routed execution status must retain its request")
        if self.manifest.selected_source is not self.result.source:
            raise ValueError("routed execution-status source must match manifest")
        if self.manifest.upstream_dataset_version != self.result.dataset_version:
            raise ValueError("routed execution-status version must match manifest")
        if self.manifest.upstream_fetched_at != self.result.fetched_at:
            raise ValueError("routed execution-status fetched time must match manifest")
        if self.manifest.upstream_data_cutoff != self.result.data_cutoff:
            raise ValueError("routed execution-status cutoff must match manifest")
        return self


class RoutedExecutionStatusFailure(_FrozenRoutingModel):
    query: ExecutionStatusQuery
    reason: FailureReason
    detail: FailureDetail
    audit: RoutingFailureAudit

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if (
            self.reason is not FailureReason.NO_PROVIDER
            or self.detail != _BATCH_TERMINAL_DETAIL
            or self.audit.category is not MarketCapability.EXECUTION_STATUS
            or not isinstance(self.audit.request, ExecutionStatusRoutingRequest)
            or self.audit.request.query != self.query
        ):
            raise ValueError("execution-status failure must retain its full request")
        return self


class RoutedInstrumentFailure(_FrozenRoutingModel):
    failure: RouterBatchFailure
    audit: RoutingFailureAudit

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if (
            self.failure.category is not MarketCapability.INSTRUMENTS
            or self.audit.category is not MarketCapability.INSTRUMENTS
            or not isinstance(self.audit.request, InstrumentRoutingRequest)
        ):
            raise ValueError("routed instrument failure context must match its audit")
        return self


class RoutedCalendarFailure(_FrozenRoutingModel):
    failure: RouterBatchFailure
    audit: RoutingFailureAudit

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        if (
            self.failure.category is not MarketCapability.TRADING_CALENDAR
            or self.audit.category is not MarketCapability.TRADING_CALENDAR
            or not isinstance(self.audit.request, CalendarRoutingRequest)
        ):
            raise ValueError("routed calendar failure context must match its audit")
        request = self.audit.request
        if (
            self.failure.exchange is not request.exchange
            or self.failure.start != request.start
            or self.failure.end != request.end
        ):
            raise ValueError("routed calendar failure must retain its full request")
        return self
