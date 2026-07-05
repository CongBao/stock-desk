# ruff: noqa: F403, F405
"""Complete single-source bar routing contracts."""

from __future__ import annotations

from tests.unit.market.routing_test_helpers import *  # noqa: F403


def test_fetch_bars_routes_whole_query_and_records_safe_attempts() -> None:
    from stock_desk.market.provenance import RoutedBarSuccess, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    unsupported_report = full_report(ProviderId.AKSHARE).model_copy(
        update={"available_periods": frozenset({Period.WEEK})}
    )
    unsupported = BarProvider(
        ProviderId.AKSHARE,
        unsupported_report,
        AssertionError("must not fetch unsupported provider"),
    )
    failed = BarProvider(
        ProviderId.BAOSTOCK,
        full_report(ProviderId.BAOSTOCK),
        ProviderUnavailable("token=TOP-SECRET /private/path"),
    )
    selected = BarProvider(
        ProviderId.TDX_LOCAL,
        full_report(ProviderId.TDX_LOCAL),
        complete_bar_result(ProviderId.TDX_LOCAL),
    )
    router = SourceRouter(
        (
            (ProviderId.AKSHARE, unsupported),
            (ProviderId.BAOSTOCK, failed),
            (ProviderId.TDX_LOCAL, selected),
        ),
        priorities=SourcePriorities(
            bars=(
                ProviderId.TUSHARE,
                ProviderId.AKSHARE,
                ProviderId.BAOSTOCK,
                ProviderId.TDX_LOCAL,
            ),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarSuccess)
    assert selected.bar_queries == [BAR_QUERY]
    assert selected.bar_queries[0] is BAR_QUERY
    assert unsupported.bar_queries == []
    assert tuple(attempt.source for attempt in outcome.manifest.attempts) == (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
    )
    assert tuple(attempt.decision for attempt in outcome.manifest.attempts) == (
        RoutingDecision.REGISTRY_MISSING,
        RoutingDecision.CAPABILITY_SKIP,
        RoutingDecision.FETCH_FAILURE,
    )
    assert tuple(attempt.reason for attempt in outcome.manifest.attempts) == (
        FailureReason.PROVIDER_UNAVAILABLE,
        FailureReason.UNSUPPORTED,
        FailureReason.PROVIDER_UNAVAILABLE,
    )
    assert "TOP-SECRET" not in outcome.model_dump_json()
    assert "/private/path" not in outcome.model_dump_json()
    assert outcome.manifest.upstream_dataset_version == (
        outcome.result.provenance.dataset_version
    )


def test_fetch_bars_rejects_partial_results_without_splicing() -> None:
    from stock_desk.market.provenance import RoutedBarFailure
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    first = BarProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        BarFailure(
            query=BAR_QUERY,
            source=ProviderId.TUSHARE,
            reason=FailureReason.MISSING,
            failed_start=BAR_QUERY.start,
            failed_end=datetime(2024, 7, 2, tzinfo=timezone.utc),
            detail="provider response does not cover the full request",
        ),
    )
    second = BarProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        BarFailure(
            query=BAR_QUERY,
            source=ProviderId.AKSHARE,
            reason=FailureReason.MISSING,
            failed_start=datetime(2024, 7, 2, tzinfo=timezone.utc),
            failed_end=BAR_QUERY.end,
            detail="provider response does not cover the full request",
        ),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, first), (ProviderId.AKSHARE, second)),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarFailure)
    assert outcome.failure.reason is FailureReason.NO_PROVIDER
    assert outcome.failure.failed_start == BAR_QUERY.start
    assert outcome.failure.failed_end == BAR_QUERY.end
    assert tuple(item.reason for item in outcome.audit.attempts) == (
        FailureReason.MISSING,
        FailureReason.MISSING,
    )
    assert first.bar_queries == [BAR_QUERY]
    assert second.bar_queries == [BAR_QUERY]


def test_fetch_bars_rejects_wrong_source_or_dataset_version_and_stops_on_success() -> (
    None
):
    from stock_desk.market.provenance import RoutedBarSuccess
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    invalid = BarProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        complete_bar_result(ProviderId.TUSHARE).model_copy(
            update={
                "provenance": complete_bar_result(
                    ProviderId.TUSHARE
                ).provenance.model_copy(update={"dataset_version": "sha256:forged"})
            }
        ),
    )
    selected = BarProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        complete_bar_result(ProviderId.AKSHARE),
    )
    unused = BarProvider(
        ProviderId.BAOSTOCK,
        full_report(ProviderId.BAOSTOCK),
        AssertionError("must stop after complete success"),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, invalid),
            (ProviderId.AKSHARE, selected),
            (ProviderId.BAOSTOCK, unused),
        ),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarSuccess)
    assert outcome.manifest.attempts[0].reason is FailureReason.INVALID_RESPONSE
    assert invalid.bar_queries == [BAR_QUERY]
    assert selected.bar_queries == [BAR_QUERY]
    assert unused.capability_calls == 0
    assert unused.bar_queries == []


@pytest.mark.parametrize(
    "reason",
    [reason for reason in FailureReason if reason is not FailureReason.NO_PROVIDER],
)
def test_fetch_bars_preserves_every_provider_failure_reason_safely(
    reason: FailureReason,
) -> None:
    from stock_desk.market.provenance import RoutedBarFailure
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    provider = BarProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        BarFailure(
            query=BAR_QUERY,
            source=ProviderId.TUSHARE,
            reason=reason,
            failed_start=BAR_QUERY.start,
            failed_end=BAR_QUERY.end,
            detail="token=TOP-SECRET /private/provider/path",
        ),
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
    assert outcome.audit.attempts[0].reason is reason
    serialized = outcome.model_dump_json()
    assert "TOP-SECRET" not in serialized
    assert "/private/provider/path" not in serialized


@pytest.mark.parametrize(
    "report",
    [
        unsupported_category_report(ProviderId.TUSHARE),
        full_report(ProviderId.TUSHARE).model_copy(
            update={"available_periods": frozenset({Period.WEEK})}
        ),
        full_report(ProviderId.TUSHARE).model_copy(
            update={"available_adjustments": frozenset({Adjustment.QFQ})}
        ),
        full_report(ProviderId.TUSHARE).model_copy(
            update={"markets": frozenset({Exchange.SZ})}
        ),
    ],
    ids=("category", "period", "adjustment", "exchange"),
)
def test_fetch_bars_skips_each_unsupported_capability_axis(
    report: CapabilityReport,
) -> None:
    from stock_desk.market.provenance import RoutedBarFailure, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    provider = BarProvider(
        ProviderId.TUSHARE,
        report,
        AssertionError("unsupported provider must not fetch"),
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
    assert outcome.audit.attempts[0].decision is RoutingDecision.CAPABILITY_SKIP
    assert outcome.audit.attempts[0].reason is FailureReason.UNSUPPORTED
    assert provider.bar_queries == []


def test_fetch_bars_treats_capability_unsupported_as_skip_and_other_errors_as_failure() -> (
    None
):
    from stock_desk.market.provenance import RoutedBarFailure, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    unsupported = BarProvider(
        ProviderId.TUSHARE,
        ProviderUnsupported("unsafe"),
        AssertionError("must not fetch"),
    )
    unavailable = BarProvider(
        ProviderId.AKSHARE,
        ProviderUnavailable("token=TOP-SECRET"),
        AssertionError("must not fetch"),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, unsupported),
            (ProviderId.AKSHARE, unavailable),
        ),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarFailure)
    assert tuple(item.decision for item in outcome.audit.attempts) == (
        RoutingDecision.CAPABILITY_FAILURE,
        RoutingDecision.CAPABILITY_FAILURE,
    )
    assert tuple(item.reason for item in outcome.audit.attempts) == (
        FailureReason.UNSUPPORTED,
        FailureReason.PROVIDER_UNAVAILABLE,
    )


@pytest.mark.parametrize(
    "case",
    (
        "wrong_type",
        "wrong_query",
        "wrong_source",
        "wrong_coverage",
        "wrong_bar_semantics",
        "wrong_failure_source",
        "wrong_failure_range",
        "router_only_failure_reason",
    ),
)
def test_fetch_bars_rejects_forged_outcome_state_table(case: str) -> None:
    from stock_desk.market.provenance import RoutedBarSuccess
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    base = complete_bar_result(ProviderId.TUSHARE)
    if case == "wrong_type":
        malformed: object = object()
    elif case == "wrong_query":
        malformed = base.model_copy(
            update={"query": BAR_QUERY.model_copy(update={"symbol": "000001.SZ"})}
        )
    elif case == "wrong_source":
        malformed = complete_bar_result(ProviderId.AKSHARE)
    elif case == "wrong_coverage":
        malformed = base.model_copy(
            update={"coverage_end": datetime(2024, 7, 2, tzinfo=timezone.utc)}
        )
    elif case == "wrong_bar_semantics":
        malformed = base.model_copy(
            update={"bars": (base.bars[0].model_copy(update={"symbol": "000001.SZ"}),)}
        )
    elif case == "wrong_failure_source":
        malformed = BarFailure(
            query=BAR_QUERY,
            source=ProviderId.AKSHARE,
            reason=FailureReason.MISSING,
            failed_start=BAR_QUERY.start,
            failed_end=BAR_QUERY.end,
            detail="provider response does not cover the full request",
        )
    elif case == "wrong_failure_range":
        malformed = BarFailure.model_construct(
            query=BAR_QUERY,
            source=ProviderId.TUSHARE,
            reason=FailureReason.MISSING,
            failed_start=datetime(2024, 6, 30, tzinfo=timezone.utc),
            failed_end=BAR_QUERY.end,
            detail="unsafe /private/range",
        )
    else:
        malformed = BarFailure.model_construct(
            query=BAR_QUERY,
            source=None,
            reason=FailureReason.NO_PROVIDER,
            failed_start=BAR_QUERY.start,
            failed_end=BAR_QUERY.end,
            detail="unsafe router-only reason",
        )
    rejected = BarProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        malformed,
    )
    selected = BarProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        complete_bar_result(ProviderId.AKSHARE),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, rejected), (ProviderId.AKSHARE, selected)),
        priorities=SourcePriorities(
            bars=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_bars(BAR_QUERY)

    assert isinstance(outcome, RoutedBarSuccess)
    assert outcome.manifest.attempts[0].reason is FailureReason.INVALID_RESPONSE
    assert selected.bar_queries == [BAR_QUERY]
