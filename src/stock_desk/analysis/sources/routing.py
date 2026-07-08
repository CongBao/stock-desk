from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from stock_desk.analysis.data_service import (
    ResearchDataUnavailable,
    ResearchLoadDiagnostic,
    ResearchSourceCandidate,
)
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchRouteMetadata,
    ResearchSection,
    ResearchSectionKind,
)
from stock_desk.analysis.sources.base import (
    RESEARCH_SOURCE_CATEGORIES,
    ResearchSourceAdapter,
    ResearchSourceCapability,
)
from stock_desk.market.providers.base import (
    ProviderClientError,
    ProviderInvalidResponse,
)
from stock_desk.market.providers.sdk import is_sdk_timeout
from stock_desk.market.types import CanonicalSymbol, FailureReason, ProviderId


RESEARCH_SOURCE_CAPABILITIES: Mapping[ProviderId, ResearchSourceCapability] = (
    MappingProxyType(
        {
            ProviderId.TUSHARE: ResearchSourceCapability(
                source=ProviderId.TUSHARE,
                categories=frozenset(
                    {
                        ResearchSectionKind.FUNDAMENTALS,
                        ResearchSectionKind.ANNOUNCEMENTS,
                    }
                ),
            ),
            ProviderId.AKSHARE: ResearchSourceCapability(
                source=ProviderId.AKSHARE,
                categories=RESEARCH_SOURCE_CATEGORIES,
            ),
            ProviderId.BAOSTOCK: ResearchSourceCapability(
                source=ProviderId.BAOSTOCK,
                categories=frozenset(),
            ),
            ProviderId.TDX_LOCAL: ResearchSourceCapability(
                source=ProviderId.TDX_LOCAL,
                categories=frozenset(),
            ),
            ProviderId.EASTMONEY: ResearchSourceCapability(
                source=ProviderId.EASTMONEY,
                categories=frozenset(),
            ),
        }
    )
)


def supported_research_sources(
    kind: ResearchSectionKind,
) -> frozenset[ProviderId]:
    if kind not in RESEARCH_SOURCE_CATEGORIES:
        raise ValueError("research capability requires a network category")
    return frozenset(
        source
        for source, capability in RESEARCH_SOURCE_CAPABILITIES.items()
        if capability.supports(kind)
    )


def _supports_research_kind(
    source: ProviderId,
    kind: ResearchSectionKind,
) -> bool:
    capability = RESEARCH_SOURCE_CAPABILITIES.get(source)
    return capability is not None and capability.supports(kind)


_MISSING_REASONS = {
    FailureReason.NO_PROVIDER: ResearchMissingReason.NO_PROVIDER,
    FailureReason.MISSING: ResearchMissingReason.MISSING,
    FailureReason.NO_DATA: ResearchMissingReason.NO_DATA,
    FailureReason.PERMISSION_DENIED: ResearchMissingReason.PERMISSION_DENIED,
    FailureReason.UNSUPPORTED: ResearchMissingReason.UNSUPPORTED,
    FailureReason.PROVIDER_UNAVAILABLE: ResearchMissingReason.PROVIDER_UNAVAILABLE,
    FailureReason.TRANSIENT_FAILURE: ResearchMissingReason.PROVIDER_UNAVAILABLE,
    FailureReason.TIMEOUT: ResearchMissingReason.TIMEOUT,
    FailureReason.INVALID_RESPONSE: ResearchMissingReason.INVALID_RESPONSE,
    FailureReason.CORRUPT: ResearchMissingReason.INVALID_RESPONSE,
}


def _validated_section(
    section: object,
    *,
    kind: ResearchSectionKind,
    source: ProviderId,
) -> ResearchSection:
    if (
        not isinstance(section, ResearchSection)
        or section.kind is not kind
        or section.canonical_source != source.value
        or section.route is not None
        or ResearchQualityFlag.DEGRADED_SOURCE in section.quality_flags
    ):
        raise ProviderInvalidResponse()
    return section


def _attach_route(
    section: ResearchSection,
    *,
    attempted_sources: tuple[str, ...],
    failure_reasons: tuple[ResearchMissingReason, ...],
) -> ResearchSection:
    flags = set(section.quality_flags)
    if attempted_sources:
        flags.add(ResearchQualityFlag.DEGRADED_SOURCE)
    route = ResearchRouteMetadata(
        selected_source=section.canonical_source,
        attempted_sources=attempted_sources,
        failure_reasons=failure_reasons,
        primary_failure_reason=failure_reasons[0] if failure_reasons else None,
        degraded_from=attempted_sources[0] if attempted_sources else None,
    )
    try:
        return ResearchSection.model_validate(
            {
                **section.model_dump(mode="python"),
                "quality_flags": tuple(sorted(flags, key=lambda item: item.value)),
                "route": route,
            }
        )
    except Exception:
        pass
    raise ProviderInvalidResponse() from None


class ResearchSourceRouter:
    """Fail-closed source boundary that never exposes adapter exception internals."""

    def __init__(
        self,
        *,
        kind: ResearchSectionKind,
        priority: tuple[ProviderId, ...],
        sources: Sequence[ResearchSourceAdapter],
    ) -> None:
        if kind not in RESEARCH_SOURCE_CATEGORIES:
            raise ValueError("research router requires a network research category")
        if (
            not priority
            or len(priority) > len(ProviderId)
            or len(priority) != len(frozenset(priority))
        ):
            raise ValueError("research source priority is invalid")
        registered: dict[ProviderId, ResearchSourceAdapter] = {}
        for source in sources:
            if not isinstance(source, ResearchSourceAdapter):
                raise TypeError("research source does not satisfy its protocol")
            if source.name in registered:
                raise ValueError("research source names must be unique")
            registered[source.name] = source
        self.kind = kind
        self._priority = priority
        self._sources = registered

    def load(self, symbol: CanonicalSymbol) -> ResearchSection:
        section, _diagnostic = self.load_with_diagnostics(symbol)
        return section

    def diagnostic_template(self) -> ResearchLoadDiagnostic:
        candidates: list[ResearchSourceCandidate] = []
        for position, source_id in enumerate(self._priority):
            supported = _supports_research_kind(source_id, self.kind)
            source = self._sources.get(source_id)
            configured = source is not None and bool(
                getattr(source, "configured", True)
            )
            failure_reason = (
                getattr(source, "unavailable_reason", None)
                if source is not None and not configured
                else None
            )
            candidates.append(
                ResearchSourceCandidate(
                    source=source_id.value,
                    position=position,
                    supported=supported,
                    configured=configured,
                    outcome=(
                        "unsupported"
                        if not supported
                        else "unconfigured"
                        if not configured
                        else "not_attempted"
                    ),
                    failure_reason=(
                        ResearchMissingReason.UNSUPPORTED
                        if not supported
                        else failure_reason
                        if isinstance(failure_reason, ResearchMissingReason)
                        else None
                    ),
                )
            )
        return ResearchLoadDiagnostic(
            kind=self.kind,
            route_source=self._priority[0].value,
            actual_source=None,
            attempted_sources=tuple(
                item.source
                for item in candidates
                if item.outcome == "unconfigured" and item.failure_reason is not None
            ),
            ordered_candidates=tuple(candidates),
        )

    def load_with_diagnostics(
        self, symbol: CanonicalSymbol
    ) -> tuple[ResearchSection, ResearchLoadDiagnostic]:
        attempted: list[str] = []
        failure_reasons: list[ResearchMissingReason] = []
        candidates: list[ResearchSourceCandidate] = []
        selected: ResearchSection | None = None
        selected_position: int | None = None
        for position, source_id in enumerate(self._priority):
            if not _supports_research_kind(source_id, self.kind):
                candidates.append(
                    ResearchSourceCandidate(
                        source=source_id.value,
                        position=position,
                        supported=False,
                        configured=source_id in self._sources,
                        outcome="unsupported",
                        failure_reason=ResearchMissingReason.UNSUPPORTED,
                    )
                )
                continue
            source = self._sources.get(source_id)
            if source is None:
                candidates.append(
                    ResearchSourceCandidate(
                        source=source_id.value,
                        position=position,
                        supported=True,
                        configured=False,
                        outcome="unconfigured",
                    )
                )
                continue
            if selected is not None:
                configured = bool(getattr(source, "configured", True))
                candidates.append(
                    ResearchSourceCandidate(
                        source=source_id.value,
                        position=position,
                        supported=True,
                        configured=configured,
                        outcome=("not_attempted" if configured else "unconfigured"),
                    )
                )
                continue
            configured = bool(getattr(source, "configured", True))
            if not configured:
                unavailable_reason = getattr(source, "unavailable_reason", None)
                reason = (
                    unavailable_reason
                    if isinstance(unavailable_reason, ResearchMissingReason)
                    else ResearchMissingReason.PROVIDER_UNAVAILABLE
                )
                attempted.append(source_id.value)
                failure_reasons.append(reason)
                candidates.append(
                    ResearchSourceCandidate(
                        source=source_id.value,
                        position=position,
                        supported=True,
                        configured=False,
                        outcome="unconfigured",
                        failure_reason=reason,
                    )
                )
                continue
            try:
                section = _validated_section(
                    source.fetch(symbol, self.kind),
                    kind=self.kind,
                    source=source_id,
                )
                if attempted:
                    section = _attach_route(
                        section,
                        attempted_sources=tuple(attempted),
                        failure_reasons=tuple(failure_reasons),
                    )
            except ResearchDataUnavailable as error:
                reason = (
                    error.reason
                    if isinstance(error.reason, ResearchMissingReason)
                    else ResearchMissingReason.INVALID_RESPONSE
                )
            except ProviderClientError as error:
                reason = (
                    _MISSING_REASONS.get(
                        error.reason,
                        ResearchMissingReason.INVALID_RESPONSE,
                    )
                    if isinstance(error.reason, FailureReason)
                    else ResearchMissingReason.INVALID_RESPONSE
                )
            except Exception as error:
                # Third-party adapters are a trust boundary. Unknown failures become
                # a typed invalid response so raw messages, chains, and credentials
                # cannot enter snapshot state.
                reason = (
                    ResearchMissingReason.TIMEOUT
                    if is_sdk_timeout(error)
                    else ResearchMissingReason.INVALID_RESPONSE
                )
            else:
                selected = section
                selected_position = position
                candidates.append(
                    ResearchSourceCandidate(
                        source=source_id.value,
                        position=position,
                        supported=True,
                        configured=bool(getattr(source, "configured", True)),
                        outcome="selected",
                    )
                )
                continue
            attempted.append(source_id.value)
            failure_reasons.append(reason)
            candidates.append(
                ResearchSourceCandidate(
                    source=source_id.value,
                    position=position,
                    supported=True,
                    configured=bool(getattr(source, "configured", True)),
                    outcome="failed",
                    failure_reason=reason,
                )
            )
        if selected is not None:
            assert selected_position is not None
            return selected, ResearchLoadDiagnostic(
                kind=self.kind,
                route_source=self._priority[0].value,
                actual_source=selected.canonical_source,
                attempted_sources=tuple(
                    candidate.source
                    for candidate in candidates
                    if candidate.outcome in {"failed", "selected"}
                    or (
                        candidate.outcome == "unconfigured"
                        and candidate.failure_reason is not None
                    )
                ),
                ordered_candidates=tuple(candidates),
            )
        raise ResearchDataUnavailable(
            kind=self.kind,
            reason=(
                failure_reasons[0]
                if failure_reasons
                else ResearchMissingReason.NO_PROVIDER
            ),
            attempted_sources=tuple(attempted),
            ordered_candidates=tuple(candidates),
            route_source=self._priority[0].value,
        ) from None


__all__ = [
    "RESEARCH_SOURCE_CAPABILITIES",
    "ResearchSourceRouter",
    "supported_research_sources",
]
