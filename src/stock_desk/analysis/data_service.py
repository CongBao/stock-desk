from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal, Protocol, runtime_checkable, TYPE_CHECKING

from pydantic import TypeAdapter

from stock_desk.analysis.snapshot import (
    MissingResearchSection,
    RESEARCH_SECTION_ORDER,
    ResearchMissingReason,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.market.types import CanonicalSymbol

if TYPE_CHECKING:
    from stock_desk.analysis.sources.base import ResearchSourceAdapter
    from stock_desk.analysis.sources.market_cache import MarketSeriesCache
    from stock_desk.market.types import ProviderId


_SYMBOL_ADAPTER = TypeAdapter(CanonicalSymbol)
_RECOVERY_CODES = {
    ResearchMissingReason.NO_PROVIDER: "configure_data_source",
    ResearchMissingReason.MISSING: "refresh_source_data",
    ResearchMissingReason.NO_DATA: "refresh_source_data",
    ResearchMissingReason.PERMISSION_DENIED: "check_source_permissions",
    ResearchMissingReason.UNSUPPORTED: "configure_supported_source",
    ResearchMissingReason.PROVIDER_UNAVAILABLE: "retry_source_connection",
    ResearchMissingReason.TIMEOUT: "retry_source_connection",
    ResearchMissingReason.INVALID_RESPONSE: "check_source_health",
}


CandidateOutcome = Literal[
    "selected", "failed", "not_attempted", "unsupported", "unconfigured"
]
_DIAGNOSTIC_SOURCE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ResearchSourceCandidate:
    source: str
    position: int
    supported: bool
    configured: bool
    outcome: CandidateOutcome
    failure_reason: ResearchMissingReason | None = None

    def __post_init__(self) -> None:
        if (
            type(self.source) is not str
            or _DIAGNOSTIC_SOURCE.fullmatch(self.source) is None
            or type(self.position) is not int
            or self.position < 0
            or type(self.supported) is not bool
            or type(self.configured) is not bool
            or self.outcome
            not in {
                "selected",
                "failed",
                "not_attempted",
                "unsupported",
                "unconfigured",
            }
        ):
            raise ValueError("research source candidate is invalid")
        valid = (
            (
                self.outcome == "selected"
                and self.supported
                and self.configured
                and self.failure_reason is None
            )
            or (
                self.outcome == "failed"
                and self.supported
                and self.configured
                and isinstance(self.failure_reason, ResearchMissingReason)
            )
            or (
                self.outcome == "not_attempted"
                and self.supported
                and self.configured
                and self.failure_reason is None
            )
            or (
                self.outcome == "unsupported"
                and not self.supported
                and self.failure_reason is ResearchMissingReason.UNSUPPORTED
            )
            or (
                self.outcome == "unconfigured"
                and self.supported
                and not self.configured
                and (
                    self.failure_reason is None
                    or self.failure_reason
                    in {
                        ResearchMissingReason.PERMISSION_DENIED,
                        ResearchMissingReason.PROVIDER_UNAVAILABLE,
                    }
                )
            )
        )
        if not valid:
            raise ValueError("research source candidate state is invalid")


@dataclass(frozen=True, slots=True)
class ResearchLoadDiagnostic:
    kind: ResearchSectionKind
    route_source: str
    actual_source: str | None
    attempted_sources: tuple[str, ...]
    ordered_candidates: tuple[ResearchSourceCandidate, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, ResearchSectionKind)
            or type(self.route_source) is not str
            or _DIAGNOSTIC_SOURCE.fullmatch(self.route_source) is None
            or (
                self.actual_source is not None
                and (
                    type(self.actual_source) is not str
                    or _DIAGNOSTIC_SOURCE.fullmatch(self.actual_source) is None
                )
            )
        ):
            raise ValueError("research load diagnostic identity is invalid")
        if tuple(item.position for item in self.ordered_candidates) != tuple(
            range(len(self.ordered_candidates))
        ) or len({item.source for item in self.ordered_candidates}) != len(
            self.ordered_candidates
        ):
            raise ValueError("research load diagnostic candidates are invalid")
        selected = tuple(
            item for item in self.ordered_candidates if item.outcome == "selected"
        )
        if len(selected) > 1 or (
            (self.actual_source is None and selected)
            or (
                self.actual_source is not None
                and (
                    len(selected) != 1
                    or (
                        selected[0].source != self.actual_source
                        and not (
                            self.kind is ResearchSectionKind.MARKET
                            and selected[0].source == self.route_source
                        )
                    )
                )
            )
        ):
            raise ValueError("research load diagnostic selection is invalid")
        expected_attempts = tuple(
            item.source
            for item in self.ordered_candidates
            if item.outcome in {"failed", "selected"}
            or (item.outcome == "unconfigured" and item.failure_reason is not None)
        )
        if (
            self.attempted_sources != expected_attempts
            or len(set(self.attempted_sources)) != len(self.attempted_sources)
            or any(
                _DIAGNOSTIC_SOURCE.fullmatch(source) is None
                for source in self.attempted_sources
            )
        ):
            raise ValueError("research load diagnostic attempts are invalid")


class ResearchDataUnavailable(RuntimeError):
    """Typed, public-safe section failure that discards provider internals."""

    def __init__(
        self,
        *,
        kind: ResearchSectionKind,
        reason: ResearchMissingReason,
        attempted_sources: tuple[str, ...],
        ordered_candidates: tuple[ResearchSourceCandidate, ...] = (),
        route_source: str | None = None,
        unsafe_context: object | None = None,
    ) -> None:
        del unsafe_context
        self.kind = kind
        self.reason = reason
        self.attempted_sources = attempted_sources
        self.ordered_candidates = ordered_candidates
        self.route_source = route_source
        super().__init__("research data is unavailable")


@runtime_checkable
class ResearchSectionLoader(Protocol):
    kind: ResearchSectionKind

    def load(self, symbol: CanonicalSymbol) -> ResearchSection: ...


class ResearchPrioritySettings(Protocol):
    fundamentals: tuple[ProviderId, ...]
    announcements: tuple[ProviderId, ...]
    news: tuple[ProviderId, ...]


class ResearchDataService:
    def __init__(
        self,
        *,
        loaders: Sequence[ResearchSectionLoader],
        clock: Callable[[], datetime],
    ) -> None:
        registered: dict[ResearchSectionKind, ResearchSectionLoader] = {}
        for loader in loaders:
            if not isinstance(loader, ResearchSectionLoader):
                raise TypeError("research loader does not satisfy its protocol")
            if loader.kind in registered:
                raise ValueError("research loader kinds must be unique")
            registered[loader.kind] = loader
        self._loaders = registered
        self._clock = clock

    def load_all(
        self,
        symbol: CanonicalSymbol,
    ) -> tuple[ResearchSection | MissingResearchSection, ...]:
        canonical_symbol = _SYMBOL_ADAPTER.validate_python(symbol, strict=True)
        outcomes: list[ResearchSection | MissingResearchSection] = []
        for kind in RESEARCH_SECTION_ORDER:
            loader = self._loaders.get(kind)
            if loader is None:
                outcomes.append(
                    self._missing(
                        kind=kind,
                        reason=ResearchMissingReason.NO_PROVIDER,
                        attempted_sources=(),
                    )
                )
                continue
            unavailable: ResearchDataUnavailable | None = None
            try:
                section = loader.load(canonical_symbol)
            except ResearchDataUnavailable as error:
                unavailable = error
                section = None
            if unavailable is not None:
                if unavailable.kind is not kind:
                    raise ValueError(
                        "research failure kind does not match loader kind"
                    ) from None
                outcomes.append(
                    self._missing(
                        kind=kind,
                        reason=unavailable.reason,
                        attempted_sources=unavailable.attempted_sources,
                    )
                )
                continue
            assert section is not None
            if section.kind is not kind:
                raise ValueError("research loader kind does not match section kind")
            outcomes.append(section)
        return tuple(outcomes)

    def load_all_with_diagnostics(
        self,
        symbol: CanonicalSymbol,
    ) -> tuple[
        tuple[ResearchSection | MissingResearchSection, ...],
        tuple[ResearchLoadDiagnostic, ...],
    ]:
        canonical_symbol = _SYMBOL_ADAPTER.validate_python(symbol, strict=True)
        outcomes: list[ResearchSection | MissingResearchSection] = []
        diagnostics: list[ResearchLoadDiagnostic] = []
        for kind in RESEARCH_SECTION_ORDER:
            loader = self._loaders.get(kind)
            if loader is None:
                outcomes.append(
                    self._missing(
                        kind=kind,
                        reason=ResearchMissingReason.NO_PROVIDER,
                        attempted_sources=(),
                    )
                )
                diagnostics.append(
                    ResearchLoadDiagnostic(
                        kind=kind,
                        route_source=(
                            "market_cache"
                            if kind is ResearchSectionKind.MARKET
                            else "unconfigured"
                        ),
                        actual_source=None,
                        attempted_sources=(),
                        ordered_candidates=(),
                    )
                )
                continue
            try:
                detailed_loader = getattr(loader, "load_with_diagnostics", None)
                if callable(detailed_loader):
                    section, diagnostic = detailed_loader(canonical_symbol)
                else:
                    section = loader.load(canonical_symbol)
                    source = section.canonical_source
                    diagnostic = ResearchLoadDiagnostic(
                        kind=kind,
                        route_source=source,
                        actual_source=source,
                        attempted_sources=(source,),
                        ordered_candidates=(
                            ResearchSourceCandidate(
                                source=source,
                                position=0,
                                supported=True,
                                configured=True,
                                outcome="selected",
                            ),
                        ),
                    )
            except ResearchDataUnavailable as error:
                if error.kind is not kind:
                    raise ValueError(
                        "research failure kind does not match loader kind"
                    ) from None
                error_candidates = error.ordered_candidates or tuple(
                    ResearchSourceCandidate(
                        source=source,
                        position=position,
                        supported=True,
                        configured=True,
                        outcome="failed",
                        failure_reason=error.reason,
                    )
                    for position, source in enumerate(error.attempted_sources)
                )
                outcomes.append(self.missing_from_error(error))
                diagnostics.append(
                    ResearchLoadDiagnostic(
                        kind=kind,
                        route_source=(
                            error.route_source
                            or (
                                "market_cache"
                                if kind is ResearchSectionKind.MARKET
                                else (
                                    error.ordered_candidates[0].source
                                    if error.ordered_candidates
                                    else error.attempted_sources[0]
                                    if error.attempted_sources
                                    else "unconfigured"
                                )
                            )
                        ),
                        actual_source=None,
                        attempted_sources=(
                            error.attempted_sources if error_candidates else ()
                        ),
                        ordered_candidates=error_candidates,
                    )
                )
                continue
            if section.kind is not kind or diagnostic.kind is not kind:
                raise ValueError("research loader kind does not match section kind")
            outcomes.append(section)
            diagnostics.append(diagnostic)
        return tuple(outcomes), tuple(diagnostics)

    def build_snapshot(
        self,
        symbol: CanonicalSymbol,
        *,
        frozen_at: datetime,
    ) -> tuple[ResearchSnapshot, tuple[ResearchLoadDiagnostic, ...]]:
        outcomes, diagnostics = self.load_all_with_diagnostics(symbol)
        snapshot = ResearchSnapshot.create(
            symbol=symbol,
            frozen_at=frozen_at,
            sections=tuple(
                item for item in outcomes if isinstance(item, ResearchSection)
            ),
            missing_sections=tuple(
                item for item in outcomes if isinstance(item, MissingResearchSection)
            ),
        )
        return snapshot, diagnostics

    def load_kind(
        self,
        symbol: CanonicalSymbol,
        kind: ResearchSectionKind,
    ) -> ResearchSection:
        canonical_symbol = _SYMBOL_ADAPTER.validate_python(symbol, strict=True)
        loader = self._loaders.get(kind)
        if loader is None:
            raise ResearchDataUnavailable(
                kind=kind,
                reason=ResearchMissingReason.NO_PROVIDER,
                attempted_sources=(),
            )
        section = loader.load(canonical_symbol)
        if section.kind is not kind:
            raise ValueError("research loader kind does not match section kind")
        return section

    def load_kind_with_diagnostics(
        self,
        symbol: CanonicalSymbol,
        kind: ResearchSectionKind,
    ) -> tuple[ResearchSection, ResearchLoadDiagnostic]:
        canonical_symbol = _SYMBOL_ADAPTER.validate_python(symbol, strict=True)
        loader = self._loaders.get(kind)
        if loader is None:
            raise ResearchDataUnavailable(
                kind=kind,
                reason=ResearchMissingReason.NO_PROVIDER,
                attempted_sources=(),
            )
        detailed_loader = getattr(loader, "load_with_diagnostics", None)
        if callable(detailed_loader):
            section, diagnostic = detailed_loader(canonical_symbol)
        else:
            section = loader.load(canonical_symbol)
            diagnostic = ResearchLoadDiagnostic(
                kind=kind,
                route_source=(
                    "market_cache"
                    if kind is ResearchSectionKind.MARKET
                    else section.canonical_source
                ),
                actual_source=section.canonical_source,
                attempted_sources=(
                    "market_cache"
                    if kind is ResearchSectionKind.MARKET
                    else section.canonical_source,
                ),
                ordered_candidates=(
                    ResearchSourceCandidate(
                        source=(
                            "market_cache"
                            if kind is ResearchSectionKind.MARKET
                            else section.canonical_source
                        ),
                        position=0,
                        supported=True,
                        configured=True,
                        outcome="selected",
                    ),
                ),
            )
        if section.kind is not kind or diagnostic.kind is not kind:
            raise ValueError("research loader kind does not match section kind")
        return section, diagnostic

    def diagnostic_template(self, kind: ResearchSectionKind) -> ResearchLoadDiagnostic:
        loader = self._loaders.get(kind)
        if loader is None:
            return ResearchLoadDiagnostic(
                kind=kind,
                route_source=(
                    "market_cache"
                    if kind is ResearchSectionKind.MARKET
                    else "unconfigured"
                ),
                actual_source=None,
                attempted_sources=(),
                ordered_candidates=(),
            )
        template = getattr(loader, "diagnostic_template", None)
        if callable(template):
            diagnostic = template()
            if (
                not isinstance(diagnostic, ResearchLoadDiagnostic)
                or diagnostic.kind is not kind
            ):
                raise ValueError("research diagnostic template is invalid")
            return diagnostic
        source = "market_cache" if kind is ResearchSectionKind.MARKET else "loader"
        return ResearchLoadDiagnostic(
            kind=kind,
            route_source=source,
            actual_source=None,
            attempted_sources=(),
            ordered_candidates=(
                ResearchSourceCandidate(
                    source=source,
                    position=0,
                    supported=True,
                    configured=True,
                    outcome="not_attempted",
                ),
            ),
        )

    def missing_from_error(
        self, error: ResearchDataUnavailable
    ) -> MissingResearchSection:
        return self._missing(
            kind=error.kind,
            reason=error.reason,
            attempted_sources=error.attempted_sources,
        )

    def _missing(
        self,
        *,
        kind: ResearchSectionKind,
        reason: ResearchMissingReason,
        attempted_sources: tuple[str, ...],
    ) -> MissingResearchSection:
        return MissingResearchSection(
            kind=kind,
            reason=reason,
            checked_at=self._clock(),
            attempted_sources=attempted_sources,
            recovery_code=_RECOVERY_CODES[reason],
        )


def compose_research_data_service(
    *,
    market_lake: MarketSeriesCache,
    sources: Sequence[ResearchSourceAdapter],
    priorities: ResearchPrioritySettings,
    clock: Callable[[], datetime],
) -> ResearchDataService:
    """Compose cache-only market data and capability-aware research routes."""
    from stock_desk.analysis.sources.market_cache import MarketCacheLoader
    from stock_desk.analysis.sources.routing import ResearchSourceRouter

    return ResearchDataService(
        loaders=(
            MarketCacheLoader(lake=market_lake),
            ResearchSourceRouter(
                kind=ResearchSectionKind.FUNDAMENTALS,
                priority=priorities.fundamentals,
                sources=sources,
            ),
            ResearchSourceRouter(
                kind=ResearchSectionKind.ANNOUNCEMENTS,
                priority=priorities.announcements,
                sources=sources,
            ),
            ResearchSourceRouter(
                kind=ResearchSectionKind.NEWS,
                priority=priorities.news,
                sources=sources,
            ),
        ),
        clock=clock,
    )
