from __future__ import annotations

import json
from typing import Final
from typing import Protocol

from stock_desk.analysis.data_service import (
    ResearchDataUnavailable,
    ResearchLoadDiagnostic,
    ResearchSourceCandidate,
)
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchSection,
    ResearchSectionKind,
)
from stock_desk.market.lake import manifest_record_id
from stock_desk.market.provenance import RoutedBarSuccess
from stock_desk.market.types import Adjustment, CanonicalSymbol, Period


MAX_RESEARCH_MARKET_BARS: Final = 768
MAX_RESEARCH_MARKET_SECTION_BYTES: Final = 61_440
MARKET_RESEARCH_PROJECTION_VERSION: Final = "market-research-projection-v1"


def _canonical_section_size(section: ResearchSection) -> int:
    return len(
        json.dumps(
            section.model_dump(mode="json", by_alias=True),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


class MarketSeriesCache(Protocol):
    def read_latest_series(
        self,
        symbol: str,
        period: Period,
        adjustment: Adjustment,
    ) -> RoutedBarSuccess | None: ...


class MarketCacheLoader:
    kind = ResearchSectionKind.MARKET

    def __init__(
        self,
        *,
        lake: MarketSeriesCache,
        period: Period = Period.DAY,
        adjustment: Adjustment = Adjustment.QFQ,
    ) -> None:
        self._lake = lake
        self._period = period
        self._adjustment = adjustment

    def load(self, symbol: CanonicalSymbol) -> ResearchSection:
        cache_failed = False
        routed: RoutedBarSuccess | None = None
        try:
            routed = self._lake.read_latest_series(
                symbol,
                self._period,
                self._adjustment,
            )
        except Exception:
            cache_failed = True
        if cache_failed:
            raise ResearchDataUnavailable(
                kind=self.kind,
                reason=ResearchMissingReason.INVALID_RESPONSE,
                attempted_sources=("market_cache",),
            ) from None
        if routed is None:
            raise ResearchDataUnavailable(
                kind=self.kind,
                reason=ResearchMissingReason.NO_DATA,
                attempted_sources=("market_cache",),
            ) from None
        if not isinstance(routed, RoutedBarSuccess):
            raise ResearchDataUnavailable(
                kind=self.kind,
                reason=ResearchMissingReason.INVALID_RESPONSE,
                attempted_sources=("market_cache",),
            ) from None
        query = routed.result.query
        if (
            query.symbol != symbol
            or query.period is not self._period
            or query.adjustment is not self._adjustment
        ):
            raise ResearchDataUnavailable(
                kind=self.kind,
                reason=ResearchMissingReason.INVALID_RESPONSE,
                attempted_sources=("market_cache",),
            ) from None
        bars = routed.result.bars
        maximum_count = min(len(bars), MAX_RESEARCH_MARKET_BARS)

        def project(selected_count: int) -> ResearchSection:
            selected = bars[-selected_count:]
            quality_flags = (
                (ResearchQualityFlag.PARTIAL,) if selected_count != len(bars) else ()
            )
            return ResearchSection.model_validate(
                {
                    "kind": self.kind,
                    "canonical_source": routed.result.provenance.source.value,
                    "source_record": manifest_record_id(routed.manifest),
                    "source_url": None,
                    "published_at": None,
                    "data_cutoff": routed.result.provenance.data_cutoff,
                    "fetched_at": routed.result.provenance.fetched_at,
                    "dataset_version": routed.result.provenance.dataset_version,
                    "quality_flags": quality_flags,
                    "content": {
                        "symbol": symbol,
                        "period": self._period.value,
                        "adjustment": self._adjustment.value,
                        "bars": tuple(bar.model_dump(mode="json") for bar in selected),
                        "projection": {
                            "schema_version": MARKET_RESEARCH_PROJECTION_VERSION,
                            "selection": "latest_suffix",
                            "source_bar_count": len(bars),
                            "selected_bar_count": selected_count,
                            "maximum_bars": MAX_RESEARCH_MARKET_BARS,
                            "maximum_section_bytes": (
                                MAX_RESEARCH_MARKET_SECTION_BYTES
                            ),
                        },
                    },
                }
            )

        try:
            lower = 1
            upper = maximum_count
            selected_count = 0
            selected_section: ResearchSection | None = None
            while lower <= upper:
                candidate_count = (lower + upper) // 2
                candidate = project(candidate_count)
                if (
                    _canonical_section_size(candidate)
                    <= MAX_RESEARCH_MARKET_SECTION_BYTES
                ):
                    selected_count = candidate_count
                    selected_section = candidate
                    lower = candidate_count + 1
                else:
                    upper = candidate_count - 1
            if selected_section is None or selected_count <= 0:
                raise ValueError
            return selected_section
        except Exception:
            pass
        raise ResearchDataUnavailable(
            kind=self.kind,
            reason=ResearchMissingReason.INVALID_RESPONSE,
            attempted_sources=("market_cache",),
        ) from None

    def load_with_diagnostics(
        self, symbol: CanonicalSymbol
    ) -> tuple[ResearchSection, ResearchLoadDiagnostic]:
        try:
            section = self.load(symbol)
        except ResearchDataUnavailable as error:
            candidate = ResearchSourceCandidate(
                source="market_cache",
                position=0,
                supported=True,
                configured=True,
                outcome="failed",
                failure_reason=error.reason,
            )
            raise ResearchDataUnavailable(
                kind=self.kind,
                reason=error.reason,
                attempted_sources=error.attempted_sources,
                ordered_candidates=(candidate,),
                route_source="market_cache",
            ) from None
        candidate = ResearchSourceCandidate(
            source="market_cache",
            position=0,
            supported=True,
            configured=True,
            outcome="selected",
        )
        return section, ResearchLoadDiagnostic(
            kind=self.kind,
            route_source="market_cache",
            actual_source=section.canonical_source,
            attempted_sources=("market_cache",),
            ordered_candidates=(candidate,),
        )

    def diagnostic_template(self) -> ResearchLoadDiagnostic:
        return ResearchLoadDiagnostic(
            kind=self.kind,
            route_source="market_cache",
            actual_source=None,
            attempted_sources=(),
            ordered_candidates=(
                ResearchSourceCandidate(
                    source="market_cache",
                    position=0,
                    supported=True,
                    configured=True,
                    outcome="not_attempted",
                ),
            ),
        )


__all__ = [
    "MARKET_RESEARCH_PROJECTION_VERSION",
    "MAX_RESEARCH_MARKET_BARS",
    "MAX_RESEARCH_MARKET_SECTION_BYTES",
    "MarketCacheLoader",
    "MarketSeriesCache",
]
