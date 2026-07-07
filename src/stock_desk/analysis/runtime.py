"""Per-invocation production research composition and read-only preflight."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import count
import json
import threading
from typing import Literal, Protocol

from stock_desk.analysis.data_service import (
    ResearchDataService,
    ResearchDataUnavailable,
    ResearchLoadDiagnostic,
    ResearchSourceCandidate,
    compose_research_data_service,
)
from stock_desk.analysis.evidence import (
    EvidenceGraph,
    EvidenceItem,
    critical_evidence_eligible,
)
from stock_desk.analysis.snapshot import (
    MissingResearchSection,
    RESEARCH_SECTION_ORDER,
    ResearchMissingReason,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.analysis.sources.akshare import AkShareResearchSource
from stock_desk.analysis.sources.base import ResearchSourceAdapter
from stock_desk.analysis.sources.market_cache import MarketSeriesCache
from stock_desk.analysis.sources.tushare import TushareResearchSource
from stock_desk.api.settings import RuntimeSourceSettings
from stock_desk.market.providers.base import (
    ProviderClientError,
    ProviderPermissionDenied,
    ProviderUnavailable,
)
from stock_desk.market.types import CanonicalSymbol, ProviderId
from stock_desk.security.redaction import scoped_log_redaction


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_PREFLIGHT_WORKER_LIMIT = 4
_PREFLIGHT_WORKER_SLOTS = threading.BoundedSemaphore(_PREFLIGHT_WORKER_LIMIT)
_PREFLIGHT_WORKER_SEQUENCE = count(1)


class RuntimeSourceSettingsProvider(Protocol):
    @property
    def database_identity(self) -> object: ...

    def runtime_snapshot(self) -> RuntimeSourceSettings: ...


class ResearchRuntimeUnavailable(RuntimeError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("research runtime is unavailable")


class DatabaseBoundResearchDataServiceFactory(Protocol):
    @property
    def database_identity(self) -> object: ...

    def __call__(self) -> ResearchDataService: ...


class _UnavailableResearchSource:
    def __init__(
        self,
        *,
        name: ProviderId,
        error_factory: Callable[[], ProviderClientError],
        configured: bool,
    ) -> None:
        self.name = name
        self.configured = configured
        self._error_factory = error_factory
        self.unavailable_reason = (
            ResearchMissingReason.PERMISSION_DENIED
            if error_factory is ProviderPermissionDenied
            else ResearchMissingReason.PROVIDER_UNAVAILABLE
        )

    def fetch(
        self,
        _symbol: CanonicalSymbol,
        _kind: ResearchSectionKind,
    ) -> ResearchSection:
        raise self._error_factory()


class ResearchDataServiceFactory:
    """Read fresh source settings and build isolated adapters for one invocation."""

    def __init__(
        self,
        *,
        source_settings: RuntimeSourceSettingsProvider,
        market_lake: MarketSeriesCache,
        clock: Callable[[], datetime] = _utc_now,
        tushare_factory: Callable[[str], ResearchSourceAdapter] | None = None,
        akshare_factory: Callable[[], ResearchSourceAdapter] | None = None,
    ) -> None:
        source_identity = getattr(source_settings, "database_identity", None)
        lake_identity = getattr(market_lake, "database_identity", None)
        if (
            source_identity is None
            or lake_identity is None
            or source_identity != lake_identity
        ):
            raise ResearchRuntimeUnavailable()
        self._source_settings = source_settings
        self._market_lake = market_lake
        self._database_identity = source_identity
        self._clock = clock
        self._tushare_factory = tushare_factory or (
            lambda token: TushareResearchSource.from_sdk(token=token, clock=clock)
        )
        self._akshare_factory = akshare_factory or (
            lambda: AkShareResearchSource.from_sdk(clock=clock)
        )

    @property
    def database_identity(self) -> object:
        return self._database_identity

    def __call__(self) -> ResearchDataService:
        settings = self._source_settings.runtime_snapshot()
        token, _path = settings.credentials_for(ProviderId.TUSHARE)
        with scoped_log_redaction(*settings.redaction_values()):
            sources = (
                self._build_tushare(token),
                self._build_akshare(),
            )
        return compose_research_data_service(
            market_lake=self._market_lake,
            sources=sources,
            priorities=settings.priorities,
            clock=self._clock,
        )

    def _build_tushare(self, token: str | None) -> ResearchSourceAdapter:
        if token is None:
            return _UnavailableResearchSource(
                name=ProviderId.TUSHARE,
                error_factory=ProviderPermissionDenied,
                configured=False,
            )
        try:
            source = self._tushare_factory(token)
            if (
                not isinstance(source, ResearchSourceAdapter)
                or source.name is not ProviderId.TUSHARE
            ):
                raise TypeError("invalid Tushare research adapter")
            return source
        except ProviderPermissionDenied:
            return _UnavailableResearchSource(
                name=ProviderId.TUSHARE,
                error_factory=ProviderPermissionDenied,
                configured=True,
            )
        except ProviderClientError:
            return _UnavailableResearchSource(
                name=ProviderId.TUSHARE,
                error_factory=ProviderUnavailable,
                configured=True,
            )
        except Exception:
            return _UnavailableResearchSource(
                name=ProviderId.TUSHARE,
                error_factory=ProviderUnavailable,
                configured=True,
            )

    def _build_akshare(self) -> ResearchSourceAdapter:
        try:
            source = self._akshare_factory()
            if (
                not isinstance(source, ResearchSourceAdapter)
                or source.name is not ProviderId.AKSHARE
            ):
                raise TypeError("invalid AKShare research adapter")
            return source
        except Exception:
            return _UnavailableResearchSource(
                name=ProviderId.AKSHARE,
                error_factory=ProviderUnavailable,
                configured=True,
            )


@dataclass(frozen=True, slots=True)
class PreflightCategory:
    kind: ResearchSectionKind
    critical: bool
    connection_state: Literal["available", "degraded", "missing"]
    route_source: str
    actual_source: str | None
    ordered_candidates: tuple[ResearchSourceCandidate, ...]
    attempted_sources: tuple[str, ...]
    missing_reason: str | None
    recovery_code: str | None
    permission_gap: bool
    data_cutoff: datetime | None
    fetched_at: datetime | None
    dataset_version: str | None
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisPreflightResult:
    symbol: str
    preview_snapshot_id: str
    reservation: bool
    rating_eligible: bool
    checked_at: datetime
    categories: tuple[PreflightCategory, ...]


class AnalysisPreflightService:
    def __init__(
        self,
        *,
        data_service_factory: DatabaseBoundResearchDataServiceFactory,
        clock: Callable[[], datetime] = _utc_now,
        category_timeout_seconds: float = 20.0,
    ) -> None:
        database_identity = getattr(data_service_factory, "database_identity", None)
        if database_identity is None:
            raise ResearchRuntimeUnavailable()
        if not 0 < category_timeout_seconds <= 120:
            raise ValueError("preflight category deadline is invalid")
        self._data_service_factory = data_service_factory
        self._clock = clock
        self._category_timeout_seconds = category_timeout_seconds
        self.database_identity = database_identity

    def check(self, symbol: CanonicalSymbol) -> AnalysisPreflightResult:
        service = self._data_service_factory()
        outcomes: list[ResearchSection | MissingResearchSection] = []
        diagnostics: list[ResearchLoadDiagnostic] = []
        for kind in RESEARCH_SECTION_ORDER:
            template = service.diagnostic_template(kind)
            try:
                section, diagnostic = self._load_category(
                    service=service,
                    symbol=symbol,
                    kind=kind,
                )
            except ResearchDataUnavailable as error:
                outcomes.append(service.missing_from_error(error))
                diagnostics.append(
                    self._failure_diagnostic(
                        template,
                        reason=error.reason,
                        attempted_sources=error.attempted_sources,
                        candidates=error.ordered_candidates,
                    )
                )
            else:
                outcomes.append(section)
                diagnostics.append(diagnostic)
        observed_times = [self._clock()]
        observed_times.extend(
            outcome.fetched_at
            for outcome in outcomes
            if isinstance(outcome, ResearchSection)
        )
        observed_times.extend(
            outcome.checked_at
            for outcome in outcomes
            if isinstance(outcome, MissingResearchSection)
        )
        if any(
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
            for value in observed_times
        ):
            raise ResearchRuntimeUnavailable()
        checked_at = max(value.astimezone(timezone.utc) for value in observed_times)
        snapshot = ResearchSnapshot.create(
            symbol=symbol,
            frozen_at=checked_at,
            sections=tuple(
                outcome for outcome in outcomes if isinstance(outcome, ResearchSection)
            ),
            missing_sections=tuple(
                outcome
                for outcome in outcomes
                if isinstance(outcome, MissingResearchSection)
            ),
        )
        graph = production_evidence_factory(snapshot)
        categories = tuple(
            self._category(snapshot, diagnostic) for diagnostic in diagnostics
        )
        return AnalysisPreflightResult(
            symbol=snapshot.symbol,
            preview_snapshot_id=snapshot.snapshot_id,
            reservation=False,
            rating_eligible=critical_evidence_eligible(snapshot, graph),
            checked_at=checked_at,
            categories=categories,
        )

    def _load_category(
        self,
        *,
        service: ResearchDataService,
        symbol: CanonicalSymbol,
        kind: ResearchSectionKind,
    ) -> tuple[ResearchSection, ResearchLoadDiagnostic]:
        if not _PREFLIGHT_WORKER_SLOTS.acquire(blocking=False):
            raise ResearchDataUnavailable(
                kind=kind,
                reason=ResearchMissingReason.PROVIDER_UNAVAILABLE,
                attempted_sources=(),
            )
        future: Future[tuple[ResearchSection, ResearchLoadDiagnostic]] = Future()

        def invoke() -> None:
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(
                            service.load_kind_with_diagnostics(symbol, kind)
                        )
                    except BaseException as error:
                        future.set_exception(error)
            finally:
                _PREFLIGHT_WORKER_SLOTS.release()

        worker = threading.Thread(
            target=invoke,
            name=f"analysis-preflight-{kind.value}-{next(_PREFLIGHT_WORKER_SEQUENCE)}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            _PREFLIGHT_WORKER_SLOTS.release()
            raise
        try:
            return future.result(timeout=self._category_timeout_seconds)
        except FutureTimeoutError:
            template = service.diagnostic_template(kind)
            attempted = tuple(
                item.source
                for item in template.ordered_candidates
                if item.outcome == "unconfigured" and item.failure_reason is not None
            )
            runnable = next(
                (
                    item.source
                    for item in template.ordered_candidates
                    if item.supported and item.configured
                ),
                None,
            )
            raise ResearchDataUnavailable(
                kind=kind,
                reason=ResearchMissingReason.TIMEOUT,
                attempted_sources=(
                    (*attempted, runnable) if runnable is not None else attempted
                ),
            ) from None

    @staticmethod
    def _failure_diagnostic(
        template: ResearchLoadDiagnostic,
        *,
        reason: ResearchMissingReason,
        attempted_sources: tuple[str, ...],
        candidates: tuple[ResearchSourceCandidate, ...],
    ) -> ResearchLoadDiagnostic:
        if candidates:
            return ResearchLoadDiagnostic(
                kind=template.kind,
                route_source=template.route_source,
                actual_source=None,
                attempted_sources=attempted_sources,
                ordered_candidates=candidates,
            )
        attempted = set(attempted_sources)
        ordered: list[ResearchSourceCandidate] = []
        failure_assigned = False
        for item in template.ordered_candidates:
            if (
                not failure_assigned
                and item.supported
                and item.configured
                and (item.source in attempted or not attempted)
            ):
                ordered.append(replace(item, outcome="failed", failure_reason=reason))
                failure_assigned = True
            else:
                ordered.append(item)
        normalized_attempts = tuple(
            item.source
            for item in ordered
            if item.outcome in {"failed", "selected"}
            or (item.outcome == "unconfigured" and item.failure_reason is not None)
        )
        return ResearchLoadDiagnostic(
            kind=template.kind,
            route_source=template.route_source,
            actual_source=None,
            attempted_sources=normalized_attempts,
            ordered_candidates=tuple(ordered),
        )

    @staticmethod
    def _category(
        snapshot: ResearchSnapshot,
        diagnostic: ResearchLoadDiagnostic,
    ) -> PreflightCategory:
        section = snapshot.section(diagnostic.kind)
        missing = next(
            (
                item
                for item in snapshot.missing_sections
                if item.kind is diagnostic.kind
            ),
            None,
        )
        failed_reasons = tuple(
            candidate.failure_reason
            for candidate in diagnostic.ordered_candidates
            if candidate.failure_reason is not None
        )
        degraded = section is not None and any(
            item.outcome in {"failed", "unconfigured"}
            and item.failure_reason is not None
            for item in diagnostic.ordered_candidates
        )
        return PreflightCategory(
            kind=diagnostic.kind,
            critical=diagnostic.kind
            in {ResearchSectionKind.MARKET, ResearchSectionKind.FUNDAMENTALS},
            connection_state=(
                "missing"
                if section is None
                else "degraded"
                if degraded
                else "available"
            ),
            route_source=diagnostic.route_source,
            actual_source=diagnostic.actual_source,
            ordered_candidates=diagnostic.ordered_candidates,
            attempted_sources=diagnostic.attempted_sources,
            missing_reason=(missing.reason.value if missing is not None else None),
            recovery_code=(missing.recovery_code if missing is not None else None),
            permission_gap=(
                ResearchMissingReason.PERMISSION_DENIED in failed_reasons
                or (
                    missing is not None
                    and missing.reason is ResearchMissingReason.PERMISSION_DENIED
                )
            ),
            data_cutoff=(section.data_cutoff if section is not None else None),
            fetched_at=(section.fetched_at if section is not None else None),
            dataset_version=(section.dataset_version if section is not None else None),
            quality_flags=(
                tuple(flag.value for flag in section.quality_flags)
                if section is not None
                else ()
            ),
        )


def _canonical_excerpt(content: object) -> str:
    encoded = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return encoded[:4096]


def production_evidence_factory(snapshot: ResearchSnapshot) -> EvidenceGraph:
    items = tuple(
        EvidenceItem.create(
            snapshot=snapshot,
            section_kind=section.kind,
            excerpt=_canonical_excerpt(section.content),
        )
        for section in snapshot.sections
    )
    return EvidenceGraph(snapshot=snapshot, evidence_items=items, claims=())


__all__ = [
    "AnalysisPreflightResult",
    "AnalysisPreflightService",
    "PreflightCategory",
    "ResearchDataServiceFactory",
    "ResearchRuntimeUnavailable",
    "production_evidence_factory",
]
