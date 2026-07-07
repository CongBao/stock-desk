from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from stock_desk.analysis.data_service import ResearchDataUnavailable
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchQualityFlag,
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
    ):
        raise ProviderInvalidResponse()
    return section


def _mark_degraded(section: ResearchSection) -> ResearchSection:
    flags = tuple(
        sorted(
            {*section.quality_flags, ResearchQualityFlag.DEGRADED_SOURCE},
            key=lambda item: item.value,
        )
    )
    try:
        return ResearchSection.model_validate(
            {**section.model_dump(mode="python"), "quality_flags": flags}
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
        attempted: list[str] = []
        first_reason: ResearchMissingReason | None = None
        for source_id in self._priority:
            capability = RESEARCH_SOURCE_CAPABILITIES[source_id]
            if not capability.supports(self.kind):
                continue
            source = self._sources.get(source_id)
            if source is None:
                continue
            attempted.append(source_id.value)
            try:
                section = _validated_section(
                    source.fetch(symbol, self.kind),
                    kind=self.kind,
                    source=source_id,
                )
                if len(attempted) > 1:
                    section = _mark_degraded(section)
            except ResearchDataUnavailable as error:
                reason = error.reason
            except ProviderClientError as error:
                reason = _MISSING_REASONS[error.reason]
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
                return section
            if first_reason is None:
                first_reason = reason
        raise ResearchDataUnavailable(
            kind=self.kind,
            reason=(
                first_reason
                if first_reason is not None
                else ResearchMissingReason.NO_PROVIDER
            ),
            attempted_sources=tuple(attempted),
        ) from None


__all__ = [
    "RESEARCH_SOURCE_CAPABILITIES",
    "ResearchSourceRouter",
    "supported_research_sources",
]
