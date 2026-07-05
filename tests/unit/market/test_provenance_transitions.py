# ruff: noqa: F403, F405
"""Source-transition chain and boundary contracts."""

from __future__ import annotations

from tests.unit.market.provenance_test_helpers import *  # noqa: F403


@pytest.mark.parametrize(
    ("previous_source", "selected_source", "previous_priority", "priority", "reason"),
    [
        (
            ProviderId.TUSHARE,
            ProviderId.AKSHARE,
            (ProviderId.TUSHARE, ProviderId.AKSHARE),
            (ProviderId.TUSHARE, ProviderId.AKSHARE),
            "fallback_after_failure",
        ),
        (
            ProviderId.AKSHARE,
            ProviderId.TUSHARE,
            (ProviderId.TUSHARE, ProviderId.AKSHARE),
            (ProviderId.TUSHARE, ProviderId.AKSHARE),
            "higher_priority_recovered",
        ),
        (
            ProviderId.TUSHARE,
            ProviderId.AKSHARE,
            (ProviderId.TUSHARE, ProviderId.AKSHARE),
            (ProviderId.AKSHARE, ProviderId.TUSHARE),
            "priority_changed",
        ),
    ],
)
def test_derive_source_transition_classifies_source_changes(
    previous_source: ProviderId,
    selected_source: ProviderId,
    previous_priority: tuple[ProviderId, ...],
    priority: tuple[ProviderId, ...],
    reason: str,
) -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutingAttempt,
        RoutingDecision,
        derive_source_transition,
        make_routing_manifest,
    )

    request = BarRoutingRequest(query=QUERY)
    previous_attempts = tuple(
        RoutingAttempt.create(
            ordinal=index,
            source=source,
            category=MarketCapability.BARS,
            decision=RoutingDecision.FETCH_FAILURE,
            reason=FailureReason.TIMEOUT,
        )
        for index, source in enumerate(
            previous_priority[: previous_priority.index(previous_source)],
            start=1,
        )
    )
    previous = make_routing_manifest(
        category=MarketCapability.BARS,
        request=request,
        priority=previous_priority,
        attempts=previous_attempts,
        selected_source=previous_source,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )

    transition = derive_source_transition(
        previous=previous,
        category=MarketCapability.BARS,
        request=request,
        priority=priority,
        selected_source=selected_source,
        upstream_dataset_version=DIGEST_B,
        observed_at=None,
    )

    assert transition is not None
    assert transition.reason.value == reason
    assert transition.from_source is previous_source
    assert transition.to_source is selected_source
    assert transition.effective_at == QUERY.start


def test_derive_source_transition_returns_none_for_same_source() -> None:
    from stock_desk.market.provenance import (
        InstrumentRoutingRequest,
        derive_source_transition,
        make_routing_manifest,
    )

    request = InstrumentRoutingRequest()
    previous = make_routing_manifest(
        category=MarketCapability.INSTRUMENTS,
        request=request,
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.INSTRUMENTS),
    )

    transition = derive_source_transition(
        previous=previous,
        category=MarketCapability.INSTRUMENTS,
        request=request,
        priority=previous.priority,
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_B,
        observed_at=datetime(2024, 7, 4, 8, tzinfo=timezone.utc),
    )

    assert transition is None


def test_transition_uses_instrument_observation_and_calendar_range_boundaries() -> None:
    from stock_desk.market.provenance import (
        CalendarRoutingRequest,
        InstrumentRoutingRequest,
        derive_source_transition,
        make_routing_manifest,
    )

    observed_at = datetime(2024, 7, 4, 8, tzinfo=timezone.utc)
    instrument_request = InstrumentRoutingRequest()
    instrument_previous = make_routing_manifest(
        category=MarketCapability.INSTRUMENTS,
        request=instrument_request,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.INSTRUMENTS),
    )
    instrument = derive_source_transition(
        previous=instrument_previous,
        category=MarketCapability.INSTRUMENTS,
        request=instrument_request,
        priority=instrument_previous.priority,
        selected_source=ProviderId.AKSHARE,
        upstream_dataset_version=DIGEST_B,
        observed_at=observed_at,
    )
    calendar_request = CalendarRoutingRequest(
        exchange=Exchange.SH,
        start=date(2024, 7, 1),
        end=date(2024, 7, 3),
    )
    calendar_previous = make_routing_manifest(
        category=MarketCapability.TRADING_CALENDAR,
        request=calendar_request,
        priority=(ProviderId.TUSHARE, ProviderId.BAOSTOCK),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.TRADING_CALENDAR),
    )
    calendar = derive_source_transition(
        previous=calendar_previous,
        category=MarketCapability.TRADING_CALENDAR,
        request=calendar_request,
        priority=calendar_previous.priority,
        selected_source=ProviderId.BAOSTOCK,
        upstream_dataset_version=DIGEST_B,
        observed_at=None,
    )

    assert instrument is not None and instrument.effective_at == observed_at
    assert calendar is not None
    assert calendar.effective_at is None
    assert calendar.calendar_start == calendar_request.start
    assert calendar.calendar_end == calendar_request.end


def test_derive_source_transition_rejects_cross_request_comparison() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        derive_source_transition,
        make_routing_manifest,
    )

    previous_request = BarRoutingRequest(query=QUERY)
    previous = make_routing_manifest(
        category=MarketCapability.BARS,
        request=previous_request,
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )
    other_request = BarRoutingRequest(
        query=QUERY.model_copy(update={"symbol": "000001.SZ"})
    )

    with pytest.raises(ValueError, match="canonical request"):
        derive_source_transition(
            previous=previous,
            category=MarketCapability.BARS,
            request=other_request,
            priority=previous.priority,
            selected_source=ProviderId.TUSHARE,
            upstream_dataset_version=DIGEST_B,
            observed_at=None,
        )


def test_derive_source_transition_rejects_cross_calendar_range() -> None:
    from stock_desk.market.provenance import (
        CalendarRoutingRequest,
        derive_source_transition,
        make_routing_manifest,
    )

    previous_request = CalendarRoutingRequest(
        exchange=Exchange.SH,
        start=date(2024, 7, 1),
        end=date(2024, 7, 3),
    )
    previous = make_routing_manifest(
        category=MarketCapability.TRADING_CALENDAR,
        request=previous_request,
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.TRADING_CALENDAR),
    )
    other_request = CalendarRoutingRequest(
        exchange=previous_request.exchange,
        start=previous_request.start,
        end=date(2024, 7, 4),
    )

    with pytest.raises(ValueError, match="canonical request"):
        derive_source_transition(
            previous=previous,
            category=MarketCapability.TRADING_CALENDAR,
            request=other_request,
            priority=previous.priority,
            selected_source=ProviderId.TUSHARE,
            upstream_dataset_version=DIGEST_B,
            observed_at=None,
        )


@pytest.mark.parametrize(
    "field",
    ("from_dataset_version", "to_dataset_version", "from_route_version"),
)
def test_transition_versions_require_canonical_lowercase_sha256(field: str) -> None:
    from stock_desk.market.provenance import SourceTransition, TransitionReason

    values = {
        "category": MarketCapability.BARS,
        "from_source": ProviderId.TUSHARE,
        "to_source": ProviderId.AKSHARE,
        "from_dataset_version": DIGEST_A,
        "to_dataset_version": DIGEST_B,
        "from_route_version": DIGEST_C,
        "effective_at": QUERY.start,
        "calendar_start": None,
        "calendar_end": None,
        "reason": TransitionReason.FALLBACK_AFTER_FAILURE,
    }
    values[field] = "sha256:" + "A" * 64

    with pytest.raises(ValidationError, match=field):
        SourceTransition.model_validate(values)


def test_derived_transition_binds_exact_previous_route_version() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        derive_source_transition,
        make_routing_manifest,
    )

    request = BarRoutingRequest(query=QUERY)
    previous = make_routing_manifest(
        category=MarketCapability.BARS,
        request=request,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )

    transition = derive_source_transition(
        previous=previous,
        category=MarketCapability.BARS,
        request=request,
        priority=previous.priority,
        selected_source=ProviderId.AKSHARE,
        upstream_dataset_version=DIGEST_B,
        observed_at=None,
    )

    assert transition is not None
    assert transition.from_route_version == previous.route_version


def test_bar_manifest_rejects_rehashed_transition_at_wrong_query_boundary() -> None:
    from datetime import timedelta

    from stock_desk.market.provenance import (
        BarRoutingRequest,
        SourceTransition,
        TransitionReason,
        make_routing_manifest,
    )

    transition = SourceTransition(
        category=MarketCapability.BARS,
        from_source=ProviderId.AKSHARE,
        to_source=ProviderId.TUSHARE,
        from_dataset_version=DIGEST_A,
        to_dataset_version=DIGEST_B,
        from_route_version=DIGEST_C,
        effective_at=QUERY.start + timedelta(minutes=1),
        calendar_start=None,
        calendar_end=None,
        reason=TransitionReason.FALLBACK_AFTER_FAILURE,
    )

    with pytest.raises(ValidationError, match="boundary"):
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=QUERY),
            priority=(ProviderId.TUSHARE,),
            attempts=(),
            selected_source=ProviderId.TUSHARE,
            upstream_dataset_version=DIGEST_B,
            transition=transition,
            **upstream_fields(MarketCapability.BARS),
        )


def test_instrument_manifest_rejects_rehashed_transition_at_wrong_observation() -> None:
    from datetime import timedelta

    from stock_desk.market.provenance import (
        InstrumentRoutingRequest,
        SourceTransition,
        TransitionReason,
        make_routing_manifest,
    )

    transition = SourceTransition(
        category=MarketCapability.INSTRUMENTS,
        from_source=ProviderId.AKSHARE,
        to_source=ProviderId.TUSHARE,
        from_dataset_version=DIGEST_A,
        to_dataset_version=DIGEST_B,
        from_route_version=DIGEST_C,
        effective_at=FETCHED_AT + timedelta(seconds=1),
        calendar_start=None,
        calendar_end=None,
        reason=TransitionReason.FALLBACK_AFTER_FAILURE,
    )

    with pytest.raises(ValidationError, match="boundary"):
        make_routing_manifest(
            category=MarketCapability.INSTRUMENTS,
            request=InstrumentRoutingRequest(),
            priority=(ProviderId.TUSHARE,),
            attempts=(),
            selected_source=ProviderId.TUSHARE,
            upstream_dataset_version=DIGEST_B,
            transition=transition,
            **upstream_fields(MarketCapability.INSTRUMENTS),
        )


def test_calendar_manifest_rejects_rehashed_transition_at_wrong_range() -> None:
    from stock_desk.market.provenance import (
        CalendarRoutingRequest,
        SourceTransition,
        TransitionReason,
        make_routing_manifest,
    )

    request = CalendarRoutingRequest(
        exchange=Exchange.SH,
        start=date(2024, 7, 1),
        end=date(2024, 7, 3),
    )
    transition = SourceTransition(
        category=MarketCapability.TRADING_CALENDAR,
        from_source=ProviderId.BAOSTOCK,
        to_source=ProviderId.TUSHARE,
        from_dataset_version=DIGEST_A,
        to_dataset_version=DIGEST_B,
        from_route_version=DIGEST_C,
        effective_at=None,
        calendar_start=request.start,
        calendar_end=date(2024, 7, 4),
        reason=TransitionReason.FALLBACK_AFTER_FAILURE,
    )

    with pytest.raises(ValidationError, match="boundary"):
        make_routing_manifest(
            category=MarketCapability.TRADING_CALENDAR,
            request=request,
            priority=(ProviderId.TUSHARE,),
            attempts=(),
            selected_source=ProviderId.TUSHARE,
            upstream_dataset_version=DIGEST_B,
            transition=transition,
            **upstream_fields(MarketCapability.TRADING_CALENDAR),
        )
