# ruff: noqa: F403, F405
"""Instrument and calendar snapshot routing contracts."""

from __future__ import annotations

from tests.unit.market.routing_test_helpers import *  # noqa: F403


def test_fetch_instruments_selects_one_sorted_complete_batch() -> None:
    from stock_desk.market.provenance import RoutedInstrumentSuccess, RoutingDecision
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    unsupported = BatchProvider(
        ProviderId.TUSHARE,
        CapabilityReport(
            source=ProviderId.TUSHARE,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(
                {MarketCapability.BARS, MarketCapability.TRADING_CALENDAR}
            ),
            available_periods=frozenset(Period),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset(Exchange),
            gaps=(
                CapabilityGap(
                    capability=MarketCapability.INSTRUMENTS,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                    detail="provider does not support this request",
                ),
            ),
        ),
        instruments=AssertionError("must not fetch unsupported category"),
        calendar=AssertionError("unused"),
    )
    failed = BatchProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        instruments=ProviderBatchFailure(
            source=ProviderId.AKSHARE,
            operation=ProviderOperation.INSTRUMENTS,
            reason=FailureReason.TIMEOUT,
            detail="token=TOP-SECRET",
        ),
        calendar=AssertionError("unused"),
    )
    selected = BatchProvider(
        ProviderId.BAOSTOCK,
        full_report(ProviderId.BAOSTOCK),
        instruments=instrument_batch(ProviderId.BAOSTOCK),
        calendar=AssertionError("unused"),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, unsupported),
            (ProviderId.AKSHARE, failed),
            (ProviderId.BAOSTOCK, selected),
        ),
        priorities=SourcePriorities(
            bars=(),
            instruments=(
                ProviderId.TDX_LOCAL,
                ProviderId.TUSHARE,
                ProviderId.AKSHARE,
                ProviderId.BAOSTOCK,
            ),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_instruments()

    assert isinstance(outcome, RoutedInstrumentSuccess)
    assert tuple(item.symbol for item in outcome.batch.items) == (
        "000001.SZ",
        "600000.SH",
    )
    assert tuple(item.decision for item in outcome.manifest.attempts) == (
        RoutingDecision.REGISTRY_MISSING,
        RoutingDecision.CAPABILITY_SKIP,
        RoutingDecision.FETCH_FAILURE,
    )
    assert selected.instrument_calls == 1
    assert "TOP-SECRET" not in outcome.model_dump_json()


def test_fetch_instruments_rejects_unsorted_or_wrong_context_and_stops_on_success() -> (
    None
):
    from stock_desk.market.provenance import RoutedInstrumentSuccess
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    unsorted = BatchProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        instruments=instrument_batch(
            ProviderId.TUSHARE,
            symbols=("600000.SH", "000001.SZ"),
        ),
        calendar=AssertionError("unused"),
    )
    wrong_context = BatchProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        instruments=ProviderBatchFailure.model_construct(
            source=ProviderId.AKSHARE,
            operation=ProviderOperation.CALENDAR,
            exchange=Exchange.SH,
            start=date(2024, 7, 1),
            end=date(2024, 7, 3),
            reason=FailureReason.MISSING,
            detail="unsafe path /private/provider",
        ),
        calendar=AssertionError("unused"),
    )
    selected = BatchProvider(
        ProviderId.BAOSTOCK,
        full_report(ProviderId.BAOSTOCK),
        instruments=instrument_batch(ProviderId.BAOSTOCK),
        calendar=AssertionError("unused"),
    )
    router = SourceRouter(
        (
            (ProviderId.TUSHARE, unsorted),
            (ProviderId.AKSHARE, wrong_context),
            (ProviderId.BAOSTOCK, selected),
        ),
        priorities=SourcePriorities(
            bars=(),
            instruments=(ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_instruments()

    assert isinstance(outcome, RoutedInstrumentSuccess)
    assert tuple(item.reason for item in outcome.manifest.attempts) == (
        FailureReason.INVALID_RESPONSE,
        FailureReason.INVALID_RESPONSE,
    )
    assert selected.instrument_calls == 1


def test_fetch_calendar_uses_category_only_capability_and_full_context() -> None:
    from stock_desk.market.provenance import RoutedCalendarSuccess
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    selected = BatchProvider(
        ProviderId.BAOSTOCK,
        calendar_only_report(ProviderId.BAOSTOCK),
        instruments=AssertionError("unused"),
        calendar=calendar_batch(ProviderId.BAOSTOCK),
    )
    router = SourceRouter(
        ((ProviderId.BAOSTOCK, selected),),
        priorities=SourcePriorities(
            bars=(),
            instruments=(),
            trading_calendar=(ProviderId.BAOSTOCK,),
        ),
    )

    outcome = router.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 1),
        date(2024, 7, 3),
    )

    assert isinstance(outcome, RoutedCalendarSuccess)
    assert selected.calendar_calls == [
        (Exchange.SH, date(2024, 7, 1), date(2024, 7, 3))
    ]
    assert tuple(item.day for item in outcome.batch.items) == (
        date(2024, 7, 1),
        date(2024, 7, 2),
    )


def test_fetch_calendar_rejects_complementary_partial_batches_without_splicing() -> (
    None
):
    from stock_desk.market.provenance import RoutedCalendarFailure
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    first = BatchProvider(
        ProviderId.TUSHARE,
        calendar_only_report(ProviderId.TUSHARE),
        instruments=AssertionError("unused"),
        calendar=calendar_batch(
            ProviderId.TUSHARE,
            days=(date(2024, 7, 1),),
        ),
    )
    second = BatchProvider(
        ProviderId.BAOSTOCK,
        calendar_only_report(ProviderId.BAOSTOCK),
        instruments=AssertionError("unused"),
        calendar=calendar_batch(
            ProviderId.BAOSTOCK,
            days=(date(2024, 7, 2),),
        ),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, first), (ProviderId.BAOSTOCK, second)),
        priorities=SourcePriorities(
            bars=(),
            instruments=(),
            trading_calendar=(ProviderId.TUSHARE, ProviderId.BAOSTOCK),
        ),
    )

    outcome = router.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 1),
        date(2024, 7, 3),
    )

    assert isinstance(outcome, RoutedCalendarFailure)
    assert outcome.failure.reason is FailureReason.NO_PROVIDER
    assert tuple(item.reason for item in outcome.audit.attempts) == (
        FailureReason.INVALID_RESPONSE,
        FailureReason.INVALID_RESPONSE,
    )
    assert first.calendar_calls == second.calendar_calls


def test_empty_category_priorities_return_router_only_no_provider() -> None:
    from stock_desk.market.provenance import (
        RoutedBarFailure,
        RoutedCalendarFailure,
        RoutedInstrumentFailure,
    )
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    router = SourceRouter(
        (),
        priorities=SourcePriorities(
            bars=(),
            instruments=(),
            trading_calendar=(),
        ),
    )

    outcomes = (
        router.fetch_bars(BAR_QUERY),
        router.fetch_instruments(),
        router.fetch_calendar(
            Exchange.SH,
            date(2024, 7, 1),
            date(2024, 7, 3),
        ),
    )

    assert isinstance(outcomes[0], RoutedBarFailure)
    assert isinstance(outcomes[1], RoutedInstrumentFailure)
    assert isinstance(outcomes[2], RoutedCalendarFailure)
    assert all(
        outcome.failure.reason is FailureReason.NO_PROVIDER for outcome in outcomes
    )
    assert all(outcome.audit.attempts == () for outcome in outcomes)


@pytest.mark.parametrize(
    "case",
    ("wrong_item", "duplicate", "unsorted", "wrong_source", "wrong_version"),
)
def test_fetch_instruments_rejects_forged_batch_state_table(case: str) -> None:
    from stock_desk.market.provenance import RoutedInstrumentSuccess
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    base = instrument_batch(ProviderId.TUSHARE)
    if case == "wrong_item":
        malformed = base.model_copy(
            update={
                "items": (
                    TradingDay(
                        day=date(2024, 7, 1),
                        exchange=Exchange.SH,
                        is_open=True,
                    ),
                )
            }
        )
    elif case == "duplicate":
        malformed = instrument_batch(
            ProviderId.TUSHARE,
            symbols=("600000.SH", "600000.SH"),
        )
    elif case == "unsorted":
        malformed = instrument_batch(
            ProviderId.TUSHARE,
            symbols=("600000.SH", "000001.SZ"),
        )
    elif case == "wrong_source":
        malformed = instrument_batch(ProviderId.AKSHARE)
    else:
        malformed = base.model_copy(
            update={
                "provenance": base.provenance.model_copy(
                    update={"dataset_version": "sha256:forged"}
                )
            }
        )
    rejected = BatchProvider(
        ProviderId.TUSHARE,
        full_report(ProviderId.TUSHARE),
        instruments=malformed,
        calendar=AssertionError("unused"),
    )
    selected = BatchProvider(
        ProviderId.AKSHARE,
        full_report(ProviderId.AKSHARE),
        instruments=instrument_batch(ProviderId.AKSHARE),
        calendar=AssertionError("unused"),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, rejected), (ProviderId.AKSHARE, selected)),
        priorities=SourcePriorities(
            bars=(),
            instruments=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            trading_calendar=(),
        ),
    )

    outcome = router.fetch_instruments()

    assert isinstance(outcome, RoutedInstrumentSuccess)
    assert outcome.manifest.attempts[0].reason is FailureReason.INVALID_RESPONSE
    assert selected.instrument_calls == 1


@pytest.mark.parametrize(
    "case",
    (
        "wrong_item",
        "missing",
        "duplicate",
        "out_of_order",
        "wrong_exchange",
        "wrong_source",
        "wrong_version",
    ),
)
def test_fetch_calendar_rejects_forged_batch_state_table(case: str) -> None:
    from stock_desk.market.provenance import RoutedCalendarSuccess
    from stock_desk.market.routing import SourcePriorities, SourceRouter

    base = calendar_batch(ProviderId.TUSHARE)
    if case == "wrong_item":
        malformed: object = instrument_batch(ProviderId.TUSHARE)
    elif case == "missing":
        malformed = calendar_batch(
            ProviderId.TUSHARE,
            days=(date(2024, 7, 1),),
        )
    elif case == "duplicate":
        malformed = calendar_batch(
            ProviderId.TUSHARE,
            days=(date(2024, 7, 1), date(2024, 7, 1)),
        )
    elif case == "out_of_order":
        malformed = calendar_batch(
            ProviderId.TUSHARE,
            days=(date(2024, 7, 2), date(2024, 7, 1)),
        )
    elif case == "wrong_exchange":
        malformed = calendar_batch(ProviderId.TUSHARE, exchange=Exchange.SZ)
    elif case == "wrong_source":
        malformed = calendar_batch(ProviderId.BAOSTOCK)
    else:
        malformed = base.model_copy(
            update={
                "provenance": base.provenance.model_copy(
                    update={"dataset_version": "sha256:forged"}
                )
            }
        )
    rejected = BatchProvider(
        ProviderId.TUSHARE,
        calendar_only_report(ProviderId.TUSHARE),
        instruments=AssertionError("unused"),
        calendar=malformed,
    )
    selected = BatchProvider(
        ProviderId.BAOSTOCK,
        calendar_only_report(ProviderId.BAOSTOCK),
        instruments=AssertionError("unused"),
        calendar=calendar_batch(ProviderId.BAOSTOCK),
    )
    router = SourceRouter(
        ((ProviderId.TUSHARE, rejected), (ProviderId.BAOSTOCK, selected)),
        priorities=SourcePriorities(
            bars=(),
            instruments=(),
            trading_calendar=(ProviderId.TUSHARE, ProviderId.BAOSTOCK),
        ),
    )

    outcome = router.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 1),
        date(2024, 7, 3),
    )

    assert isinstance(outcome, RoutedCalendarSuccess)
    assert outcome.manifest.attempts[0].reason is FailureReason.INVALID_RESPONSE
    assert selected.calendar_calls == [
        (Exchange.SH, date(2024, 7, 1), date(2024, 7, 3))
    ]
