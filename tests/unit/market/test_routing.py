# ruff: noqa: F403, F405
"""Priority, registry, and capability routing contracts."""

from __future__ import annotations

from tests.unit.market.routing_test_helpers import *  # noqa: F403


def test_source_priorities_have_exact_independent_defaults() -> None:
    from stock_desk.market.routing import SourcePriorities

    priorities = SourcePriorities()

    assert priorities.bars == (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
        ProviderId.TDX_LOCAL,
    )
    assert priorities.instruments == (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
    )
    assert priorities.trading_calendar == (
        ProviderId.TUSHARE,
        ProviderId.BAOSTOCK,
    )
    assert priorities.for_category(MarketCapability.BARS) == priorities.bars
    assert (
        priorities.for_category(MarketCapability.INSTRUMENTS) == priorities.instruments
    )
    assert (
        priorities.for_category(MarketCapability.TRADING_CALENDAR)
        == priorities.trading_calendar
    )


def test_source_priorities_allow_empty_custom_categories_and_are_frozen() -> None:
    from stock_desk.market.routing import SourcePriorities

    priorities = SourcePriorities(
        bars=(),
        instruments=(ProviderId.AKSHARE,),
        trading_calendar=(ProviderId.BAOSTOCK, ProviderId.TUSHARE),
    )

    assert priorities.bars == ()
    assert priorities.instruments == (ProviderId.AKSHARE,)
    assert priorities.trading_calendar == (
        ProviderId.BAOSTOCK,
        ProviderId.TUSHARE,
    )
    with pytest.raises(ValidationError, match="frozen"):
        priorities.bars = (ProviderId.TUSHARE,)


@pytest.mark.parametrize("category", ["bars", "instruments", "trading_calendar"])
def test_source_priorities_reject_per_category_duplicates(category: str) -> None:
    from stock_desk.market.routing import SourcePriorities

    values: dict[str, object] = {
        "bars": (),
        "instruments": (),
        "trading_calendar": (),
        category: (ProviderId.TUSHARE, ProviderId.TUSHARE),
    }

    with pytest.raises(ValidationError, match="duplicate"):
        SourcePriorities.model_validate(values)


def test_source_priorities_are_strict_and_extra_forbidden() -> None:
    from stock_desk.market.routing import SourcePriorities

    with pytest.raises(ValidationError):
        SourcePriorities.model_validate(
            {
                "bars": [ProviderId.TUSHARE],
                "instruments": (),
                "trading_calendar": (),
            }
        )
    with pytest.raises(ValidationError, match="extra"):
        SourcePriorities.model_validate({"unknown": ()})


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            lambda: [
                (
                    ProviderId.TUSHARE,
                    StubProvider(ProviderId.TUSHARE, full_report(ProviderId.TUSHARE)),
                ),
                (
                    ProviderId.TUSHARE,
                    StubProvider(ProviderId.TUSHARE, full_report(ProviderId.TUSHARE)),
                ),
            ],
            "duplicate registry key",
        ),
        (
            lambda: [
                (
                    ProviderId.TUSHARE,
                    StubProvider(ProviderId.TUSHARE, full_report(ProviderId.TUSHARE)),
                ),
                (
                    ProviderId.AKSHARE,
                    StubProvider(ProviderId.TUSHARE, full_report(ProviderId.TUSHARE)),
                ),
            ],
            "duplicate provider name",
        ),
        (
            lambda: [
                (
                    ProviderId.TUSHARE,
                    StubProvider(ProviderId.AKSHARE, full_report(ProviderId.AKSHARE)),
                ),
            ],
            "key/name mismatch",
        ),
    ],
)
def test_router_rejects_ambiguous_registry_entries(
    entries: object,
    message: str,
) -> None:
    from stock_desk.market.routing import SourceRouter

    with pytest.raises(ValueError, match=message):
        SourceRouter(entries())


def test_router_rejects_registering_same_instance_twice() -> None:
    from stock_desk.market.routing import SourceRouter

    provider = StubProvider(ProviderId.TUSHARE, full_report(ProviderId.TUSHARE))

    with pytest.raises(ValueError, match="same provider instance"):
        SourceRouter(
            (
                (ProviderId.TUSHARE, provider),
                (ProviderId.TUSHARE, provider),
            )
        )


def test_router_copies_registry_and_priorities_behind_immutable_state() -> None:
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    provider = StubProvider(ProviderId.TUSHARE, full_report(ProviderId.TUSHARE))
    entries = [(ProviderId.TUSHARE, provider)]
    priorities = SourcePriorities(
        bars=(ProviderId.EASTMONEY,),
        instruments=(),
        trading_calendar=(),
    )
    router = SourceRouter(entries, priorities=priorities)
    entries.clear()

    assert router.priorities() == priorities
    assert isinstance(router._registry, MappingProxyType)
    assert tuple(router._registry) == (ProviderId.TUSHARE,)
    assert router.capability_reports() == (full_report(ProviderId.TUSHARE),)


def test_capability_reports_preserve_registry_order_and_fail_closed() -> None:
    from stock_desk.market.routing import SourceRouter

    valid = StubProvider(ProviderId.TUSHARE, full_report(ProviderId.TUSHARE))
    wrong_source = StubProvider(
        ProviderId.AKSHARE,
        CapabilityReport.model_construct(
            source=ProviderId.BAOSTOCK,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset({MarketCapability.BARS}),
            available_periods=frozenset({Period.DAY}),
            available_adjustments=frozenset({Adjustment.NONE}),
            markets=frozenset({Exchange.SH}),
            data_cutoff=None,
            gaps=(),
        ),
    )
    unavailable = StubProvider(
        ProviderId.BAOSTOCK,
        ProviderUnavailable("token=TOP-SECRET /private/path"),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, valid),
            (ProviderId.AKSHARE, wrong_source),
            (ProviderId.BAOSTOCK, unavailable),
        )
    )

    reports = router.capability_reports()

    assert tuple(report.source for report in reports) == (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
    )
    assert reports[0].state is CapabilityState.AVAILABLE
    assert reports[1].state is CapabilityState.UNAVAILABLE
    assert {gap.reason for gap in reports[1].gaps} == {FailureReason.INVALID_RESPONSE}
    assert reports[2].state is CapabilityState.UNAVAILABLE
    assert {gap.reason for gap in reports[2].gaps} == {
        FailureReason.PROVIDER_UNAVAILABLE
    }
    serialized = " ".join(report.model_dump_json() for report in reports)
    assert "TOP-SECRET" not in serialized
    assert "/private/path" not in serialized
    assert valid.capability_calls == wrong_source.capability_calls == 1
    assert unavailable.capability_calls == 1


def test_capability_reports_preserve_safe_permission_and_timeout_states() -> None:
    from stock_desk.market.routing import SourceRouter

    denied = StubProvider(
        ProviderId.TUSHARE,
        ProviderPermissionDenied("token=TOP-SECRET"),
    )
    timed_out = StubProvider(
        ProviderId.AKSHARE,
        ProviderTimeout("/private/provider/path"),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, denied), (ProviderId.AKSHARE, timed_out))
    )

    reports = router.capability_reports()

    assert reports[0].state is CapabilityState.PERMISSION_DENIED
    assert {gap.reason for gap in reports[0].gaps} == {FailureReason.PERMISSION_DENIED}
    assert reports[1].state is CapabilityState.TRANSIENT_FAILURE
    assert {gap.reason for gap in reports[1].gaps} == {FailureReason.TIMEOUT}
    serialized = " ".join(item.model_dump_json() for item in reports)
    assert "TOP-SECRET" not in serialized
    assert "/private/provider/path" not in serialized


def test_routing_preserves_valid_target_capability_failure_reason_without_fetch() -> (
    None
):
    from stock_desk.market.provenance import RoutedBarFailure, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    def unavailable_report(
        source: ProviderId,
        state: CapabilityState,
        reason: FailureReason,
    ) -> CapabilityReport:
        return CapabilityReport(
            source=source,
            state=state,
            gaps=(
                CapabilityGap(
                    capability=MarketCapability.BARS,
                    state=state,
                    reason=reason,
                    detail="provider capability is unavailable",
                ),
            ),
        )

    denied = BarProvider(
        ProviderId.TUSHARE,
        unavailable_report(
            ProviderId.TUSHARE,
            CapabilityState.PERMISSION_DENIED,
            FailureReason.PERMISSION_DENIED,
        ),
        AssertionError("capability failure must not fetch"),
    )
    corrupt = BarProvider(
        ProviderId.AKSHARE,
        unavailable_report(
            ProviderId.AKSHARE,
            CapabilityState.UNAVAILABLE,
            FailureReason.CORRUPT,
        ),
        AssertionError("capability failure must not fetch"),
    )
    unsupported = BarProvider(
        ProviderId.BAOSTOCK,
        unsupported_category_report(ProviderId.BAOSTOCK),
        AssertionError("unsupported capability must not fetch"),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, denied),
            (ProviderId.AKSHARE, corrupt),
            (ProviderId.BAOSTOCK, unsupported),
        ),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarFailure)
    assert tuple(item.decision for item in outcome.audit.attempts) == (
        RoutingDecision.CAPABILITY_FAILURE,
        RoutingDecision.CAPABILITY_FAILURE,
        RoutingDecision.CAPABILITY_SKIP,
    )
    assert tuple(item.reason for item in outcome.audit.attempts) == (
        FailureReason.PERMISSION_DENIED,
        FailureReason.CORRUPT,
        FailureReason.UNSUPPORTED,
    )
    assert denied.bar_queries == corrupt.bar_queries == unsupported.bar_queries == []


def test_capability_missing_target_gap_fails_closed_without_fetch() -> None:
    from stock_desk.market.provenance import RoutedBarFailure, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    ambiguous = BarProvider(
        ProviderId.TUSHARE,
        CapabilityReport(
            source=ProviderId.TUSHARE,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset({MarketCapability.INSTRUMENTS}),
        ),
        AssertionError("ambiguous capability must not fetch"),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, ambiguous),),
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
    assert ambiguous.bar_queries == []
