from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable, TYPE_CHECKING

from pydantic import TypeAdapter

from stock_desk.analysis.snapshot import (
    MissingResearchSection,
    RESEARCH_SECTION_ORDER,
    ResearchMissingReason,
    ResearchSection,
    ResearchSectionKind,
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


class ResearchDataUnavailable(RuntimeError):
    """Typed, public-safe section failure that discards provider internals."""

    def __init__(
        self,
        *,
        kind: ResearchSectionKind,
        reason: ResearchMissingReason,
        attempted_sources: tuple[str, ...],
        unsafe_context: object | None = None,
    ) -> None:
        del unsafe_context
        self.kind = kind
        self.reason = reason
        self.attempted_sources = attempted_sources
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
