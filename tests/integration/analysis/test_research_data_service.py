from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from stock_desk.analysis.data_service import (
    ResearchDataUnavailable,
    compose_research_data_service,
)
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchSectionKind,
    ResearchSnapshotBuilder,
)
from stock_desk.analysis.sources.akshare import AkShareResearchSource
from stock_desk.analysis.sources.market_cache import MarketCacheLoader
from stock_desk.analysis.sources.tushare import TushareResearchSource
from stock_desk.api.settings import SourcePriorities
from stock_desk.market.provenance import RoutedBarSuccess
from stock_desk.market.types import Adjustment, Period
from tests.integration.market.lake_test_helpers import routed_daily_bars


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads(
    (ROOT / "tests/fixtures/analysis/research_sources.json").read_text(encoding="utf-8")
)
NOW = datetime(2025, 7, 6, 9, tzinfo=timezone.utc)
SYMBOL = "600000.SH"


class FakeMarketLake:
    def __init__(self, outcome: RoutedBarSuccess | None) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, Period, Adjustment]] = []

    def read_latest_series(
        self, symbol: str, period: Period, adjustment: Adjustment
    ) -> RoutedBarSuccess | None:
        self.calls.append((symbol, period, adjustment))
        return self.outcome


class FakeTushareResearchClient:
    def income(self, **_kwargs: object) -> object:
        return FIXTURE["tushare"]["fundamentals"]

    def anns_d(self, **_kwargs: object) -> object:
        return FIXTURE["tushare"]["announcements"]


class FakeAkShareResearchClient:
    def stock_financial_analysis_indicator_em(self, **_kwargs: object) -> object:
        return FIXTURE["akshare"]["fundamentals"]

    def stock_individual_notice_report(self, **_kwargs: object) -> object:
        return FIXTURE["akshare"]["announcements"]

    def stock_news_em(self, **_kwargs: object) -> object:
        return FIXTURE["akshare"]["news"]


def test_market_loader_is_cache_only_and_preserves_manifest_provenance() -> None:
    routed = routed_daily_bars(
        (date(2025, 7, 3), date(2025, 7, 4)),
        symbol=SYMBOL,
        adjustment=Adjustment.QFQ,
    )
    lake = FakeMarketLake(routed)
    loader = MarketCacheLoader(lake=lake)

    section = loader.load(SYMBOL)

    assert lake.calls == [(SYMBOL, Period.DAY, Adjustment.QFQ)]
    assert section.kind is ResearchSectionKind.MARKET
    assert section.canonical_source == routed.result.provenance.source.value
    assert section.source_record.startswith("sha256:")
    assert section.dataset_version == routed.result.provenance.dataset_version
    assert section.data_cutoff == routed.result.provenance.data_cutoff
    assert section.fetched_at == routed.result.provenance.fetched_at
    assert section.content["period"] == "1d"
    assert section.content["adjustment"] == "qfq"
    assert len(section.content["bars"]) == 2


def test_market_loader_returns_typed_missing_when_cache_has_no_series() -> None:
    lake = FakeMarketLake(None)

    with pytest.raises(ResearchDataUnavailable) as captured:
        MarketCacheLoader(lake=lake).load(SYMBOL)

    assert captured.value.kind is ResearchSectionKind.MARKET
    assert captured.value.reason is ResearchMissingReason.NO_DATA
    assert captured.value.attempted_sources == ("market_cache",)
    assert lake.calls == [(SYMBOL, Period.DAY, Adjustment.QFQ)]


def test_market_loader_removes_cache_exception_context() -> None:
    class FailingMarketLake:
        def read_latest_series(
            self,
            _symbol: str,
            _period: Period,
            _adjustment: Adjustment,
        ) -> RoutedBarSuccess | None:
            raise RuntimeError("database-url=TOP-SECRET")

    with pytest.raises(ResearchDataUnavailable) as captured:
        MarketCacheLoader(lake=FailingMarketLake()).load(SYMBOL)

    assert captured.value.reason is ResearchMissingReason.INVALID_RESPONSE
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "TOP-SECRET" not in repr(captured.value)


@pytest.mark.parametrize(
    ("requested_symbol", "period", "adjustment"),
    [
        (SYMBOL, Period.WEEK, Adjustment.QFQ),
        (SYMBOL, Period.DAY, Adjustment.NONE),
        ("000001.SZ", Period.DAY, Adjustment.QFQ),
    ],
)
def test_market_loader_rejects_cache_results_for_a_different_query(
    requested_symbol: str,
    period: Period,
    adjustment: Adjustment,
) -> None:
    routed = routed_daily_bars(
        (date(2025, 7, 4),),
        symbol=SYMBOL,
        adjustment=Adjustment.QFQ,
    )
    loader = MarketCacheLoader(
        lake=FakeMarketLake(routed),
        period=period,
        adjustment=adjustment,
    )

    with pytest.raises(ResearchDataUnavailable) as captured:
        loader.load(requested_symbol)

    assert captured.value.reason is ResearchMissingReason.INVALID_RESPONSE


def test_composed_service_builds_snapshot_from_real_or_explicit_missing_only() -> None:
    routed = routed_daily_bars(
        (date(2025, 7, 3), date(2025, 7, 4)),
        symbol=SYMBOL,
        adjustment=Adjustment.QFQ,
    )
    tushare = TushareResearchSource(
        client=FakeTushareResearchClient(), clock=lambda: NOW
    )
    akshare = AkShareResearchSource(
        client=FakeAkShareResearchClient(), clock=lambda: NOW
    )
    service = compose_research_data_service(
        market_lake=FakeMarketLake(routed),
        sources=(tushare, akshare),
        priorities=SourcePriorities(),
        clock=lambda: NOW,
    )

    snapshot = ResearchSnapshotBuilder(data_service=service, clock=lambda: NOW).build(
        SYMBOL
    )

    assert snapshot.missing_sections == ()
    assert tuple(item.kind for item in snapshot.sections) == (
        ResearchSectionKind.MARKET,
        ResearchSectionKind.FUNDAMENTALS,
        ResearchSectionKind.ANNOUNCEMENTS,
        ResearchSectionKind.NEWS,
    )
    assert snapshot.section(ResearchSectionKind.MARKET).canonical_source == "tushare"  # type: ignore[union-attr]
    assert (
        snapshot.section(ResearchSectionKind.FUNDAMENTALS).canonical_source == "tushare"
    )  # type: ignore[union-attr]
    assert (
        snapshot.section(ResearchSectionKind.ANNOUNCEMENTS).canonical_source
        == "tushare"
    )  # type: ignore[union-attr]
    assert snapshot.section(ResearchSectionKind.NEWS).canonical_source == "akshare"  # type: ignore[union-attr]
    assert all(section.content.get("items") for section in snapshot.sections[1:])
    assert all("placeholder" not in section.content for section in snapshot.sections)


def test_composed_service_exposes_no_provider_instead_of_empty_success() -> None:
    routed = routed_daily_bars(
        (date(2025, 7, 4),),
        symbol=SYMBOL,
        adjustment=Adjustment.QFQ,
    )
    service = compose_research_data_service(
        market_lake=FakeMarketLake(routed),
        sources=(
            TushareResearchSource(
                client=FakeTushareResearchClient(), clock=lambda: NOW
            ),
        ),
        priorities=SourcePriorities(),
        clock=lambda: NOW,
    )

    snapshot = ResearchSnapshotBuilder(data_service=service, clock=lambda: NOW).build(
        SYMBOL
    )

    assert snapshot.section(ResearchSectionKind.NEWS) is None
    news_missing = next(
        item
        for item in snapshot.missing_sections
        if item.kind is ResearchSectionKind.NEWS
    )
    assert news_missing.reason is ResearchMissingReason.NO_PROVIDER
    assert news_missing.attempted_sources == ()
