# ruff: noqa: F403, F405
"""Routing isolation, transition, and import safety contracts."""

from __future__ import annotations

from tests.unit.market.routing_test_helpers import *  # noqa: F403


def test_malicious_provider_client_reason_is_invalid_at_capability_and_fetch() -> None:
    from stock_desk.market.provenance import RoutedBarSuccess, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    class MaliciousReason(ProviderClientError):
        reason = "token=TOP-SECRET /private/provider/path"

    capability_attack = BarProvider(
        ProviderId.TUSHARE,
        MaliciousReason(),
        AssertionError("capability failure must not fetch"),
    )
    fetch_attack = BarProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        MaliciousReason(),
    )
    selected = BarProvider(
        ProviderId.BAOSTOCK,
        full_report(ProviderId.BAOSTOCK),
        complete_bar_result(ProviderId.BAOSTOCK),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, capability_attack),
            (ProviderId.AKSHARE, fetch_attack),
            (ProviderId.BAOSTOCK, selected),
        ),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarSuccess)
    assert tuple(item.decision for item in outcome.manifest.attempts) == (
        RoutingDecision.CAPABILITY_FAILURE,
        RoutingDecision.FETCH_FAILURE,
    )
    assert tuple(item.reason for item in outcome.manifest.attempts) == (
        FailureReason.INVALID_RESPONSE,
        FailureReason.INVALID_RESPONSE,
    )
    assert capability_attack.bar_queries == []
    assert fetch_attack.bar_queries == [BAR_QUERY]
    serialized = outcome.model_dump_json()
    assert "TOP-SECRET" not in serialized
    assert "/private/provider/path" not in serialized


def test_explosive_provider_client_reason_property_fails_closed() -> None:
    from stock_desk.market.provenance import RoutedBarFailure, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    class ExplosiveReason(ProviderClientError):
        @property
        def reason(self) -> FailureReason:
            raise RuntimeError("token=TOP-SECRET /private/provider/path")

    provider = BarProvider(
        ProviderId.TUSHARE,
        ExplosiveReason(),
        AssertionError("capability failure must not fetch"),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, provider),),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE,),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarFailure)
    assert outcome.audit.attempts[0].decision is RoutingDecision.CAPABILITY_FAILURE
    assert outcome.audit.attempts[0].reason is FailureReason.INVALID_RESPONSE
    assert "TOP-SECRET" not in outcome.model_dump_json()


def test_router_attaches_transition_from_previous_manifest_to_current_success() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutedBarSuccess,
        TransitionReason,
        make_routing_manifest,
    )
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    priority = (ProviderId.TUSHARE, ProviderId.AKSHARE)
    previous_result = complete_bar_result(ProviderId.TUSHARE)
    previous = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=BAR_QUERY),
        priority=priority,
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=previous_result.provenance.dataset_version,
        upstream_fetched_at=previous_result.provenance.fetched_at,
        upstream_data_cutoff=previous_result.provenance.data_cutoff,
        upstream_adjustment=previous_result.provenance.adjustment,
    )
    failed = BarProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        ProviderUnavailable("unsafe"),
    )
    selected = BarProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        complete_bar_result(ProviderId.AKSHARE),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, failed), (ProviderId.AKSHARE, selected)),
        priorities=SourcePriorities(
            bars=priority,
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY, previous_manifest=previous)

    assert isinstance(outcome, RoutedBarSuccess)
    assert outcome.manifest.transition is not None
    assert outcome.manifest.transition.reason is TransitionReason.FALLBACK_AFTER_FAILURE
    assert outcome.manifest.transition.from_source is ProviderId.TUSHARE
    assert outcome.manifest.transition.to_source is ProviderId.AKSHARE
    assert outcome.manifest.transition.effective_at == BAR_QUERY.start


def test_router_revalidates_previous_manifest_before_provider_access() -> None:
    from stock_desk.market.provenance import BarRoutingRequest, make_routing_manifest
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    previous_result = complete_bar_result(ProviderId.TUSHARE)
    previous = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=BAR_QUERY),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=previous_result.provenance.dataset_version,
        upstream_fetched_at=previous_result.provenance.fetched_at,
        upstream_data_cutoff=previous_result.provenance.data_cutoff,
        upstream_adjustment=previous_result.provenance.adjustment,
    )
    tampered = previous.model_copy(update={"route_version": "sha256:" + "f" * 64})
    provider = BarProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        previous_result,
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, provider),),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE,),
            instruments=(),
            trading_calendar=(),
        ),
    )

    with pytest.raises(ValidationError, match="route_version"):
        router.fetch_bars(BAR_QUERY, previous_manifest=tampered)

    assert provider.capability_calls == 0
    assert provider.bar_queries == []


def test_same_provider_serializes_capability_and_fetch_as_one_critical_section() -> (
    None
):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    from stock_desk.market.provenance import (
        RoutedBarSuccess,
        RoutedInstrumentSuccess,
    )
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    class SerializedProvider:
        name = ProviderId.TUSHARE

        def __init__(self) -> None:
            self.events: list[tuple[str, int]] = []
            self.event_lock = threading.Lock()

        def record(self, label: str) -> None:
            with self.event_lock:
                self.events.append((label, threading.get_ident()))
            time.sleep(0.01)

        def capabilities(self) -> CapabilityReport:
            self.record("capability")
            return full_report(self.name)

        def fetch_bars(self, query: BarQuery) -> BarResult:
            assert query is BAR_QUERY
            self.record("bars")
            return complete_bar_result(self.name)

        def fetch_instruments(self) -> ProviderBatch[Instrument]:
            self.record("instruments")
            return instrument_batch(self.name)

        def fetch_calendar(
            self,
            exchange: Exchange,
            start: date,
            end: date,
        ) -> object:
            raise AssertionError("unused")

    provider = SerializedProvider()
    router = SourceRouter(
        ((ProviderId.TUSHARE, provider),),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE,),
            instruments=(ProviderId.TUSHARE,),
            trading_calendar=(),
        ),
    )
    start_barrier = threading.Barrier(2)

    def bars() -> object:
        start_barrier.wait(timeout=2)
        return router.fetch_bars(BAR_QUERY)

    def instruments() -> object:
        start_barrier.wait(timeout=2)
        return router.fetch_instruments()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(
            future.result(timeout=3)
            for future in (executor.submit(bars), executor.submit(instruments))
        )

    assert any(isinstance(item, RoutedBarSuccess) for item in outcomes)
    assert any(isinstance(item, RoutedInstrumentSuccess) for item in outcomes)
    assert [label for label, _thread in provider.events] in (
        ["capability", "bars", "capability", "instruments"],
        ["capability", "instruments", "capability", "bars"],
    )
    assert provider.events[0][1] == provider.events[1][1]
    assert provider.events[2][1] == provider.events[3][1]
    assert provider.events[0][1] != provider.events[2][1]


def test_different_provider_locks_overlap_and_attempts_stay_call_local() -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    from stock_desk.market.provenance import (
        RoutedBarSuccess,
        RoutedInstrumentSuccess,
    )
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    fetch_barrier = threading.Barrier(2)

    class ParallelBars(BarProvider):
        def fetch_bars(self, query: object) -> object:
            fetch_barrier.wait(timeout=2)
            return super().fetch_bars(query)

    class ParallelInstruments(BatchProvider):
        def fetch_instruments(self) -> object:
            fetch_barrier.wait(timeout=2)
            return super().fetch_instruments()

    bars_provider = ParallelBars(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        complete_bar_result(ProviderId.TUSHARE),
    )
    instrument_provider = ParallelInstruments(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        instruments=instrument_batch(ProviderId.AKSHARE),
        calendar=AssertionError("unused"),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, bars_provider),
            (ProviderId.AKSHARE, instrument_provider),
        ),
        priorities=SourcePriorities(
            bars=(ProviderId.TDX_LOCAL, ProviderId.TUSHARE),
            instruments=(ProviderId.AKSHARE,),
            trading_calendar=(),
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        bars_future = executor.submit(router.fetch_bars, BAR_QUERY)
        instruments_future = executor.submit(router.fetch_instruments)
        bars_outcome = bars_future.result(timeout=3)
        instrument_outcome = instruments_future.result(timeout=3)

    assert isinstance(bars_outcome, RoutedBarSuccess)
    assert isinstance(instrument_outcome, RoutedInstrumentSuccess)
    assert tuple(item.source for item in bars_outcome.manifest.attempts) == (
        ProviderId.TDX_LOCAL,
    )
    assert instrument_outcome.manifest.attempts == ()


def test_router_does_not_catch_base_exception_from_capability_or_fetch() -> None:
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    class CapabilityInterrupt(BarProvider):
        def capabilities(self) -> CapabilityReport:
            raise KeyboardInterrupt

    priorities = SourcePriorities(
        bars=(ProviderId.TUSHARE,),
        instruments=(),
        trading_calendar=(),
    )
    capability_interrupt = CapabilityInterrupt(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        complete_bar_result(ProviderId.TUSHARE),
    )
    fetch_interrupt = BarProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        KeyboardInterrupt(),
    )

    with pytest.raises(KeyboardInterrupt):
        SourceRouter(
            ((ProviderId.TUSHARE, capability_interrupt),),
            priorities=priorities,
        ).fetch_bars(BAR_QUERY)
    with pytest.raises(KeyboardInterrupt):
        SourceRouter(
            ((ProviderId.TUSHARE, fetch_interrupt),),
            priorities=priorities,
        ).fetch_bars(BAR_QUERY)


def test_routing_import_has_no_provider_network_or_filesystem_side_effects() -> None:
    from pathlib import Path
    import subprocess
    import sys

    project_root = Path(__file__).parents[3]
    script = """
import os
import socket
import sys

def fail(*args, **kwargs):
    raise AssertionError("import side effect")

os.open = fail
os.scandir = fail
socket.socket = fail
for name in ("pandas", "tushare", "akshare", "baostock"):
    sys.modules[name] = None
import stock_desk.market.provenance
import stock_desk.market.routing
"""

    subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_market_package_exports_only_stable_routing_entry_points() -> None:
    from stock_desk.market import (
        RoutingManifest,
        SourcePriorities,
        SourceRouter,
        SourceTransition,
    )

    assert SourceRouter.__module__ == "stock_desk.market.routing"
    assert SourcePriorities.__module__ == "stock_desk.market.routing"
    assert RoutingManifest.__module__ == "stock_desk.market.provenance"
    assert SourceTransition.__module__ == "stock_desk.market.provenance"
