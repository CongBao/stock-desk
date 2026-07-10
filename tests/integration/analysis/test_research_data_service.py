from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from stock_desk.analysis.data_service import (
    ResearchDataUnavailable,
    compose_research_data_service,
)
from stock_desk.analysis.evidence import EvidenceItem
from stock_desk.analysis.prompt_builder import build_role_request
from stock_desk.analysis.roles import RoleName
from stock_desk.analysis.snapshot import (
    MissingResearchSection,
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
    ResearchSnapshotBuilder,
)
from stock_desk.analysis.sources.akshare import AkShareResearchSource
from stock_desk.analysis.sources.market_cache import (
    MARKET_RESEARCH_PROJECTION_VERSION,
    MAX_RESEARCH_MARKET_BARS,
    MAX_RESEARCH_MARKET_SECTION_BYTES,
    MarketCacheLoader,
)
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
    database_identity = ("sqlite", "runtime")

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
    bars = section.content["bars"]
    assert isinstance(bars, list)
    assert len(bars) == 2


def test_market_loader_keeps_the_largest_recent_suffix_within_stage_budget() -> None:
    first_day = date(2022, 1, 1)
    routed = routed_daily_bars(
        tuple(first_day + timedelta(days=offset) for offset in range(900)),
        symbol=SYMBOL,
        adjustment=Adjustment.QFQ,
    )
    loader = MarketCacheLoader(lake=FakeMarketLake(routed))

    first = loader.load(SYMBOL)
    second = loader.load(SYMBOL)

    encoded = json.dumps(
        first.model_dump(mode="json", by_alias=True),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    content = first.content
    projection = content["projection"]
    assert isinstance(projection, dict)
    selected_count = projection["selected_bar_count"]
    assert isinstance(selected_count, int)
    assert 0 < selected_count < MAX_RESEARCH_MARKET_BARS
    assert len(encoded) <= MAX_RESEARCH_MARKET_SECTION_BYTES
    assert projection == {
        "schema_version": MARKET_RESEARCH_PROJECTION_VERSION,
        "selection": "latest_suffix",
        "source_bar_count": len(routed.result.bars),
        "selected_bar_count": selected_count,
        "maximum_bars": MAX_RESEARCH_MARKET_BARS,
        "maximum_section_bytes": MAX_RESEARCH_MARKET_SECTION_BYTES,
    }
    assert first.quality_flags == (ResearchQualityFlag.PARTIAL,)
    selected_bars = content["bars"]
    assert isinstance(selected_bars, list)
    assert selected_bars == [
        bar.model_dump(mode="json") for bar in routed.result.bars[-selected_count:]
    ]
    assert first == second
    assert first.section_id == second.section_id

    missing = tuple(
        MissingResearchSection(
            kind=kind,
            reason=ResearchMissingReason.NO_DATA,
            checked_at=first.fetched_at,
            attempted_sources=(),
            recovery_code="refresh_research_data",
        )
        for kind in (
            ResearchSectionKind.FUNDAMENTALS,
            ResearchSectionKind.ANNOUNCEMENTS,
            ResearchSectionKind.NEWS,
        )
    )
    snapshot = ResearchSnapshot.create(
        symbol=SYMBOL,
        frozen_at=first.fetched_at,
        sections=(first,),
        missing_sections=missing,
    )
    evidence = EvidenceItem.create(
        snapshot=snapshot,
        section_kind=ResearchSectionKind.MARKET,
        excerpt="Bounded real market projection.",
    )
    request = build_role_request(
        role=RoleName.TECHNICAL,
        snapshot=snapshot,
        evidence=(evidence,),
        dependencies=(),
    ).request
    assert len(request.data_blocks) == 2

    one_more_payload = first.model_dump(mode="python", by_alias=True)
    one_more_content = dict(first.content)
    one_more_content["bars"] = [
        routed.result.bars[-selected_count - 1].model_dump(mode="json"),
        *selected_bars,
    ]
    one_more_projection = dict(projection)
    one_more_projection["selected_bar_count"] = selected_count + 1
    one_more_content["projection"] = one_more_projection
    one_more_payload["content"] = one_more_content
    one_more = ResearchSection.model_validate(one_more_payload)
    one_more_size = len(
        json.dumps(
            one_more.model_dump(mode="json", by_alias=True),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    assert one_more_size > MAX_RESEARCH_MARKET_SECTION_BYTES


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
