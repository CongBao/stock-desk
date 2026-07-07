from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import inspect
import threading
from types import SimpleNamespace

import pytest
import stock_desk.analysis.runtime as runtime_module

from stock_desk.analysis.runtime import (
    AnalysisPreflightService,
    ResearchDataServiceFactory,
    production_evidence_factory,
)
from stock_desk.analysis.data_service import (
    ResearchDataService,
    ResearchLoadDiagnostic,
    ResearchSourceCandidate,
)
from stock_desk.analysis.snapshot import ResearchSection, ResearchSectionKind
from stock_desk.analysis.sources.akshare import AkShareResearchSource
from stock_desk.analysis.sources.tushare import TushareResearchSource
from stock_desk.api.settings import SourcePriorities
from stock_desk.market.providers.base import ProviderPermissionDenied
from stock_desk.market.types import ProviderId
from tests.integration.analysis.test_research_data_service import (
    FakeAkShareResearchClient,
    FakeMarketLake,
    FakeTushareResearchClient,
)
from tests.integration.market.lake_test_helpers import routed_daily_bars


NOW = datetime(2025, 7, 6, 9, tzinfo=timezone.utc)
SYMBOL = "600000.SH"


class _AdvancingClock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


class _SuccessLoader:
    def __init__(self, kind: ResearchSectionKind, clock: _AdvancingClock) -> None:
        self.kind = kind
        self._clock = clock

    def load(self, _symbol: str) -> ResearchSection:
        fetched_at = self._clock()
        return ResearchSection(
            kind=self.kind,
            canonical_source="fixture",
            source_record=f"fixture:{self.kind.value}",
            source_url="https://example.com/source",
            published_at=(
                fetched_at
                if self.kind
                in {ResearchSectionKind.ANNOUNCEMENTS, ResearchSectionKind.NEWS}
                else None
            ),
            data_cutoff=fetched_at,
            fetched_at=fetched_at,
            dataset_version="fixture-v1",
            content={"kind": self.kind.value},
        )


class _HangingLoader:
    kind = ResearchSectionKind.MARKET

    def load(self, _symbol: str) -> ResearchSection:
        threading.Event().wait()
        raise AssertionError("unreachable")


class _BoundFactory:
    database_identity = ("sqlite", "runtime")

    def __init__(self, service: ResearchDataService) -> None:
        self._service = service

    def __call__(self) -> ResearchDataService:
        return self._service


def test_preflight_freezes_after_advancing_clock_missing_checks() -> None:
    clock = _AdvancingClock()
    data_service = ResearchDataService(loaders=(), clock=clock)
    service = AnalysisPreflightService(
        data_service_factory=_BoundFactory(data_service),
        clock=clock,
    )

    result = service.check(SYMBOL)

    assert result.checked_at == NOW + timedelta(microseconds=4)
    assert all(category.connection_state == "missing" for category in result.categories)


def test_preflight_freezes_after_advancing_clock_success_fetches() -> None:
    clock = _AdvancingClock()
    data_service = ResearchDataService(
        loaders=tuple(_SuccessLoader(kind, clock) for kind in ResearchSectionKind),
        clock=clock,
    )
    service = AnalysisPreflightService(
        data_service_factory=_BoundFactory(data_service),
        clock=clock,
    )

    result = service.check(SYMBOL)

    assert result.checked_at == NOW + timedelta(microseconds=4)
    assert all(
        category.fetched_at is not None and category.fetched_at <= result.checked_at
        for category in result.categories
    )


def test_preflight_clock_rollback_uses_latest_observed_utc_time() -> None:
    fetched_clock = _AdvancingClock()
    data_service = ResearchDataService(
        loaders=tuple(
            _SuccessLoader(kind, fetched_clock) for kind in ResearchSectionKind
        ),
        clock=lambda: NOW,
    )
    preflight = AnalysisPreflightService(
        data_service_factory=_BoundFactory(data_service),
        clock=lambda: NOW - timedelta(days=1),
    )

    result = preflight.check(SYMBOL)

    assert result.checked_at == NOW + timedelta(microseconds=3)
    assert all(
        category.fetched_at is not None and category.fetched_at <= result.checked_at
        for category in result.categories
    )


class _Settings:
    database_identity = ("sqlite", "runtime")

    def __init__(self, snapshots: tuple[object, ...]) -> None:
        self._snapshots = iter(snapshots)
        self.calls = 0

    def runtime_snapshot(self) -> object:
        self.calls += 1
        return next(self._snapshots)


@pytest.mark.parametrize(
    ("settings_identity", "lake_identity"),
    [
        (("sqlite", "settings"), ("sqlite", "lake")),
        (None, ("sqlite", "lake")),
        (("sqlite", "settings"), None),
    ],
)
def test_runtime_factory_rejects_missing_or_mismatched_database_identity(
    settings_identity: object,
    lake_identity: object,
) -> None:
    settings = _Settings((_snapshot(token=None),))
    settings.database_identity = settings_identity
    lake = FakeMarketLake(None)
    lake.database_identity = lake_identity

    with pytest.raises(RuntimeError, match="research runtime is unavailable"):
        ResearchDataServiceFactory(
            source_settings=settings,
            market_lake=lake,
            clock=lambda: NOW,
        )


def test_preflight_identity_comes_only_from_validated_runtime_factory() -> None:
    assert (
        "database_identity"
        not in inspect.signature(AnalysisPreflightService).parameters
    )


def test_preflight_exposes_bounded_category_deadline() -> None:
    assert (
        "category_timeout_seconds"
        in inspect.signature(AnalysisPreflightService).parameters
    )


def test_preflight_hard_deadline_preserves_other_categories() -> None:
    clock = _AdvancingClock()
    service = ResearchDataService(
        loaders=(
            _HangingLoader(),
            *(
                _SuccessLoader(kind, clock)
                for kind in (
                    ResearchSectionKind.FUNDAMENTALS,
                    ResearchSectionKind.ANNOUNCEMENTS,
                    ResearchSectionKind.NEWS,
                )
            ),
        ),
        clock=clock,
    )
    preflight = AnalysisPreflightService(
        data_service_factory=_BoundFactory(service),
        clock=clock,
        category_timeout_seconds=0.02,
    )
    captured: list[object] = []
    caller = threading.Thread(
        target=lambda: captured.append(preflight.check(SYMBOL)),
        daemon=True,
    )

    caller.start()
    caller.join(timeout=0.2)

    assert not caller.is_alive()
    result = captured[0]
    assert result.categories[0].connection_state == "missing"
    assert result.categories[0].missing_reason == "timeout"
    assert all(
        category.connection_state == "available" for category in result.categories[1:]
    )


def test_preflight_concurrency_keeps_hanging_worker_threads_bounded() -> None:
    class HangingKind:
        def __init__(self, kind: ResearchSectionKind) -> None:
            self.kind = kind

        def load(self, _symbol: str) -> ResearchSection:
            threading.Event().wait()
            raise AssertionError("unreachable")

    data_service = ResearchDataService(
        loaders=tuple(HangingKind(kind) for kind in ResearchSectionKind),
        clock=lambda: NOW,
    )
    preflight = AnalysisPreflightService(
        data_service_factory=_BoundFactory(data_service),
        clock=lambda: NOW,
        category_timeout_seconds=0.02,
    )
    results: list[object] = []
    callers = tuple(
        threading.Thread(
            target=lambda: results.append(preflight.check(SYMBOL)),
            daemon=True,
        )
        for _ in range(40)
    )

    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=0.5)

    assert all(not caller.is_alive() for caller in callers)
    assert len(results) == 40
    assert all(
        category.connection_state == "missing"
        and category.missing_reason in {"timeout", "provider_unavailable"}
        for result in results
        for category in result.categories
    )
    assert (
        sum(
            thread.name.startswith("analysis-preflight-")
            for thread in threading.enumerate()
        )
        <= 4
    )
    runtime_module._PREFLIGHT_WORKER_SLOTS = threading.BoundedSemaphore(4)


def test_candidate_and_diagnostic_reject_unsafe_or_contradictory_identity() -> None:
    with pytest.raises(ValueError) as unsafe:
        ResearchSourceCandidate(
            source="token=TOP-SECRET",
            position=-1,
            supported=True,
            configured=True,
            outcome="selected",
        )
    assert "TOP-SECRET" not in repr(unsafe.value)

    selected = ResearchSourceCandidate(
        source="akshare",
        position=0,
        supported=True,
        configured=True,
        outcome="selected",
    )
    contradictory = ResearchSourceCandidate(
        source="tushare",
        position=1,
        supported=True,
        configured=True,
        outcome="selected",
    )
    with pytest.raises(ValueError):
        ResearchLoadDiagnostic(
            kind=ResearchSectionKind.FUNDAMENTALS,
            route_source="tushare",
            actual_source="akshare",
            attempted_sources=("akshare", "tushare"),
            ordered_candidates=(selected, contradictory),
        )


def _snapshot(*, token: str | None, priorities: SourcePriorities | None = None):
    return SimpleNamespace(
        priorities=priorities or SourcePriorities(),
        configuration_fingerprint="sha256:" + "a" * 64,
        credentials_for=lambda source: (
            (token, None) if source is ProviderId.TUSHARE else (None, None)
        ),
        redaction_values=lambda: () if token is None else (token,),
    )


class _PermissionTushare:
    name = ProviderId.TUSHARE

    def fetch(self, _symbol: str, _kind: ResearchSectionKind):
        raise ProviderPermissionDenied("token=TOP-SECRET")


def test_preflight_uses_fresh_settings_and_preserves_complete_route_diagnostics() -> (
    None
):
    settings = _Settings((_snapshot(token="secret"), _snapshot(token="secret")))
    lake = FakeMarketLake(routed_daily_bars((date(2025, 7, 4),), symbol=SYMBOL))
    factory = ResearchDataServiceFactory(
        source_settings=settings,
        market_lake=lake,
        clock=lambda: NOW,
        tushare_factory=lambda _token: _PermissionTushare(),
        akshare_factory=lambda: AkShareResearchSource(
            client=FakeAkShareResearchClient(), clock=lambda: NOW
        ),
    )
    service = AnalysisPreflightService(
        data_service_factory=factory,
        clock=lambda: NOW,
    )

    first = service.check(SYMBOL)
    second = service.check(SYMBOL)

    assert settings.calls == 2
    assert first.symbol == SYMBOL
    assert first.reservation is False
    assert first.checked_at == NOW
    assert first.preview_snapshot_id != ""
    assert first.preview_snapshot_id == second.preview_snapshot_id
    assert tuple(category.kind for category in first.categories) == (
        ResearchSectionKind.MARKET,
        ResearchSectionKind.FUNDAMENTALS,
        ResearchSectionKind.ANNOUNCEMENTS,
        ResearchSectionKind.NEWS,
    )
    market, fundamentals, announcements, news = first.categories
    assert market.route_source == "market_cache"
    assert market.actual_source == "tushare"
    assert market.attempted_sources == ("market_cache",)
    assert market.ordered_candidates[0].outcome == "selected"
    assert fundamentals.route_source == "tushare"
    assert fundamentals.actual_source == "akshare"
    assert fundamentals.connection_state == "degraded"
    assert fundamentals.permission_gap is True
    assert fundamentals.attempted_sources == ("tushare", "akshare")
    assert [candidate.source for candidate in fundamentals.ordered_candidates] == [
        "tushare",
        "akshare",
    ]
    assert [candidate.outcome for candidate in fundamentals.ordered_candidates] == [
        "failed",
        "selected",
    ]
    assert fundamentals.ordered_candidates[0].failure_reason == "permission_denied"
    assert announcements.actual_source == "akshare"
    assert news.actual_source == "akshare"
    assert first.rating_eligible is True


def test_preflight_keeps_unsupported_and_unconfigured_candidates_in_priority_order() -> (
    None
):
    priorities = SourcePriorities(
        fundamentals=(
            "baostock",
            "tushare",
            "akshare",
        )
    )
    settings = _Settings((_snapshot(token="secret", priorities=priorities),))
    factory = ResearchDataServiceFactory(
        source_settings=settings,
        market_lake=FakeMarketLake(None),
        clock=lambda: NOW,
        tushare_factory=lambda _token: TushareResearchSource(
            client=FakeTushareResearchClient(), clock=lambda: NOW
        ),
        akshare_factory=lambda: (_ for _ in ()).throw(RuntimeError("sdk missing")),
    )

    result = AnalysisPreflightService(
        data_service_factory=factory,
        clock=lambda: NOW,
    ).check(SYMBOL)

    fundamentals = result.categories[1]
    assert [item.source for item in fundamentals.ordered_candidates] == [
        "baostock",
        "tushare",
        "akshare",
    ]
    assert [item.outcome for item in fundamentals.ordered_candidates] == [
        "unsupported",
        "selected",
        "not_attempted",
    ]
    assert fundamentals.ordered_candidates[0].supported is False
    assert result.categories[0].missing_reason == "no_data"
    assert result.rating_eligible is False


def test_missing_tushare_token_is_typed_unconfigured_before_akshare() -> None:
    settings = _Settings((_snapshot(token=None),))
    result = AnalysisPreflightService(
        data_service_factory=ResearchDataServiceFactory(
            source_settings=settings,
            market_lake=FakeMarketLake(None),
            clock=lambda: NOW,
            akshare_factory=lambda: AkShareResearchSource(
                client=FakeAkShareResearchClient(), clock=lambda: NOW
            ),
        ),
        clock=lambda: NOW,
    ).check(SYMBOL)

    fundamentals = result.categories[1]
    assert fundamentals.actual_source == "akshare"
    assert fundamentals.ordered_candidates[0].source == "tushare"
    assert fundamentals.ordered_candidates[0].configured is False
    assert fundamentals.ordered_candidates[0].outcome == "unconfigured"
    assert fundamentals.ordered_candidates[0].failure_reason == "permission_denied"
    assert fundamentals.permission_gap is True
    assert fundamentals.route_source == "tushare"
    assert fundamentals.attempted_sources == ("tushare", "akshare")


def test_configured_tushare_factory_failure_is_failed_before_akshare() -> None:
    settings = _Settings((_snapshot(token="configured-secret"),))
    result = AnalysisPreflightService(
        data_service_factory=ResearchDataServiceFactory(
            source_settings=settings,
            market_lake=FakeMarketLake(None),
            clock=lambda: NOW,
            tushare_factory=lambda _token: (_ for _ in ()).throw(
                RuntimeError("TOP-SECRET provider construction detail")
            ),
            akshare_factory=lambda: AkShareResearchSource(
                client=FakeAkShareResearchClient(), clock=lambda: NOW
            ),
        ),
        clock=lambda: NOW,
    ).check(SYMBOL)

    candidate = result.categories[1].ordered_candidates[0]
    assert candidate.source == "tushare"
    assert candidate.configured is True
    assert candidate.outcome == "failed"
    assert candidate.failure_reason == "provider_unavailable"
    assert "TOP-SECRET" not in repr(result)


def test_missing_token_and_failed_akshare_preserve_terminal_attempt_trace() -> None:
    settings = _Settings((_snapshot(token=None), _snapshot(token=None)))

    def factory() -> ResearchDataServiceFactory:
        return ResearchDataServiceFactory(
            source_settings=settings,
            market_lake=FakeMarketLake(None),
            clock=lambda: NOW,
            akshare_factory=lambda: (_ for _ in ()).throw(
                RuntimeError("TOP-SECRET AKShare construction detail")
            ),
        )

    snapshot, diagnostics = factory()().build_snapshot(SYMBOL, frozen_at=NOW)
    preflight = AnalysisPreflightService(
        data_service_factory=factory(),
        clock=lambda: NOW,
    ).check(SYMBOL)

    fundamentals = diagnostics[1]
    assert snapshot.section(ResearchSectionKind.FUNDAMENTALS) is None
    assert fundamentals.attempted_sources == ("tushare", "akshare")
    assert [item.outcome for item in fundamentals.ordered_candidates] == [
        "unconfigured",
        "failed",
    ]
    assert [item.failure_reason for item in fundamentals.ordered_candidates] == [
        "permission_denied",
        "provider_unavailable",
    ]
    category = preflight.categories[1]
    assert category.connection_state == "missing"
    assert category.permission_gap is True
    assert category.attempted_sources == ("tushare", "akshare")
    assert "TOP-SECRET" not in repr((snapshot, diagnostics, preflight))


def test_production_evidence_is_deterministic_and_matches_each_real_section() -> None:
    settings = _Settings((_snapshot(token="secret"),))
    data_service = ResearchDataServiceFactory(
        source_settings=settings,
        market_lake=FakeMarketLake(
            routed_daily_bars((date(2025, 7, 4),), symbol=SYMBOL)
        ),
        clock=lambda: NOW,
        tushare_factory=lambda _token: TushareResearchSource(
            client=FakeTushareResearchClient(), clock=lambda: NOW
        ),
        akshare_factory=lambda: AkShareResearchSource(
            client=FakeAkShareResearchClient(), clock=lambda: NOW
        ),
    )()
    snapshot, _diagnostics = data_service.build_snapshot(SYMBOL, frozen_at=NOW)

    first = production_evidence_factory(snapshot)
    second = production_evidence_factory(snapshot)

    assert first == second
    assert first.snapshot == snapshot
    assert first.claims == ()
    assert tuple(item.section_kind for item in first.evidence_items) == tuple(
        section.kind for section in snapshot.sections
    )
    assert all(0 < len(item.excerpt) <= 4096 for item in first.evidence_items)
    assert all(
        item.section_id == snapshot.section(item.section_kind).section_id
        for item in first.evidence_items
    )
