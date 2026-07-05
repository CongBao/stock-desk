# ruff: noqa: F403, F405
"""Frozen routing model and routed-envelope contracts."""

from __future__ import annotations

from tests.unit.market.provenance_test_helpers import *  # noqa: F403


def test_routing_attempt_uses_continuous_ordinal_and_fixed_safe_detail() -> None:
    from stock_desk.market.provenance import RoutingAttempt, RoutingDecision

    attempt = RoutingAttempt.create(
        ordinal=1,
        source=ProviderId.TUSHARE,
        category=MarketCapability.BARS,
        decision=RoutingDecision.FETCH_FAILURE,
        reason=FailureReason.TIMEOUT,
    )

    assert attempt.detail == "provider request timed out"
    with pytest.raises(ValidationError, match="fixed safe detail"):
        RoutingAttempt(
            ordinal=1,
            source=ProviderId.TUSHARE,
            category=MarketCapability.BARS,
            decision=RoutingDecision.FETCH_FAILURE,
            reason=FailureReason.TIMEOUT,
            detail="token=TOP-SECRET",
        )
    with pytest.raises(ValidationError, match="NO_PROVIDER"):
        RoutingAttempt.create(
            ordinal=1,
            source=ProviderId.TUSHARE,
            category=MarketCapability.BARS,
            decision=RoutingDecision.FETCH_FAILURE,
            reason=FailureReason.NO_PROVIDER,
        )


def test_decision_specific_attempt_reason_is_validated() -> None:
    from stock_desk.market.provenance import RoutingAttempt, RoutingDecision

    missing = RoutingAttempt.create(
        ordinal=1,
        source=ProviderId.TDX_LOCAL,
        category=MarketCapability.BARS,
        decision=RoutingDecision.REGISTRY_MISSING,
        reason=FailureReason.PROVIDER_UNAVAILABLE,
    )
    skipped = RoutingAttempt.create(
        ordinal=2,
        source=ProviderId.TDX_LOCAL,
        category=MarketCapability.INSTRUMENTS,
        decision=RoutingDecision.CAPABILITY_SKIP,
        reason=FailureReason.UNSUPPORTED,
    )

    assert missing.detail == "provider is not registered"
    assert skipped.detail == "provider capability does not support this request"
    with pytest.raises(ValidationError, match="REGISTRY_MISSING"):
        RoutingAttempt.create(
            ordinal=3,
            source=ProviderId.AKSHARE,
            category=MarketCapability.BARS,
            decision=RoutingDecision.REGISTRY_MISSING,
            reason=FailureReason.TIMEOUT,
        )


def test_canonical_routing_requests_are_strict_frozen_and_validated() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        CalendarRoutingRequest,
        InstrumentRoutingRequest,
    )

    bars = BarRoutingRequest(query=QUERY)
    instruments = InstrumentRoutingRequest()
    calendar = CalendarRoutingRequest(
        exchange=Exchange.SH,
        start=date(2024, 7, 1),
        end=date(2024, 7, 3),
    )

    assert bars.query == QUERY
    assert instruments.model_dump() == {}
    assert calendar.end > calendar.start
    with pytest.raises(ValidationError, match="range"):
        CalendarRoutingRequest(
            exchange=Exchange.SH,
            start=date(2024, 7, 3),
            end=date(2024, 7, 3),
        )
    with pytest.raises(ValidationError, match="extra"):
        InstrumentRoutingRequest.model_validate({"unexpected": True})
    with pytest.raises(ValidationError, match="frozen"):
        calendar.exchange = Exchange.SZ


def test_routing_manifest_rejects_category_request_mismatch_and_bad_ordinals() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        InstrumentRoutingRequest,
        RoutingAttempt,
        RoutingDecision,
        RoutingManifest,
        make_routing_manifest,
    )

    attempts = (
        RoutingAttempt.create(
            ordinal=1,
            source=ProviderId.TUSHARE,
            category=MarketCapability.BARS,
            decision=RoutingDecision.FETCH_FAILURE,
            reason=FailureReason.TIMEOUT,
        ),
        RoutingAttempt.create(
            ordinal=2,
            source=ProviderId.AKSHARE,
            category=MarketCapability.BARS,
            decision=RoutingDecision.FETCH_FAILURE,
            reason=FailureReason.CORRUPT,
        ),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=QUERY),
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK),
        attempts=attempts,
        selected_source=ProviderId.BAOSTOCK,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )

    assert manifest.schema_version == "stock-desk-routing-manifest-v1"
    assert manifest.attempts == attempts
    with pytest.raises(ValidationError, match="request"):
        RoutingManifest.model_validate(
            manifest.model_copy(
                update={"request": InstrumentRoutingRequest()}
            ).model_dump(mode="python")
        )
    with pytest.raises(ValidationError, match="continuous"):
        RoutingManifest.model_validate(
            manifest.model_copy(
                update={
                    "attempts": (
                        attempts[0],
                        attempts[1].model_copy(update={"ordinal": 3}),
                    )
                }
            ).model_dump(mode="python")
        )


def test_source_transition_requires_category_specific_boundary() -> None:
    from stock_desk.market.provenance import SourceTransition, TransitionReason

    transition = SourceTransition(
        category=MarketCapability.BARS,
        from_source=ProviderId.TUSHARE,
        to_source=ProviderId.AKSHARE,
        from_dataset_version=DIGEST_A,
        to_dataset_version=DIGEST_B,
        from_route_version=DIGEST_C,
        effective_at=QUERY.start,
        calendar_start=None,
        calendar_end=None,
        reason=TransitionReason.FALLBACK_AFTER_FAILURE,
    )

    assert transition.effective_at == QUERY.start
    with pytest.raises(ValidationError, match="calendar boundary"):
        SourceTransition(
            category=MarketCapability.TRADING_CALENDAR,
            from_source=ProviderId.TUSHARE,
            to_source=ProviderId.BAOSTOCK,
            from_dataset_version=DIGEST_A,
            to_dataset_version=DIGEST_B,
            from_route_version=DIGEST_C,
            effective_at=QUERY.start,
            calendar_start=None,
            calendar_end=None,
            reason=TransitionReason.PRIORITY_CHANGED,
        )


def test_success_manifest_hash_excludes_provider_fetch_time() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutedBarSuccess,
        RoutingAttempt,
        RoutingDecision,
        make_routing_manifest,
    )

    first_result = bar_result(ProviderId.AKSHARE, DIGEST_A)
    later_result = bar_result(
        ProviderId.AKSHARE,
        DIGEST_A,
        fetched_at=datetime(2024, 7, 4, 8, tzinfo=timezone.utc),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=QUERY),
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        attempts=(
            RoutingAttempt.create(
                ordinal=1,
                source=ProviderId.TUSHARE,
                category=MarketCapability.BARS,
                decision=RoutingDecision.FETCH_FAILURE,
                reason=FailureReason.TIMEOUT,
            ),
        ),
        selected_source=ProviderId.AKSHARE,
        upstream_dataset_version=DIGEST_A,
        upstream_fetched_at=first_result.provenance.fetched_at,
        upstream_data_cutoff=first_result.provenance.data_cutoff,
        upstream_adjustment=first_result.provenance.adjustment,
    )
    later_manifest = make_routing_manifest(
        category=manifest.category,
        request=manifest.request,
        priority=manifest.priority,
        attempts=manifest.attempts,
        selected_source=manifest.selected_source,
        upstream_dataset_version=manifest.upstream_dataset_version,
        upstream_fetched_at=later_result.provenance.fetched_at,
        upstream_data_cutoff=later_result.provenance.data_cutoff,
        upstream_adjustment=later_result.provenance.adjustment,
    )

    first = RoutedBarSuccess(result=first_result, manifest=manifest)
    later = RoutedBarSuccess(result=later_result, manifest=later_manifest)

    assert first.manifest.route_version.startswith("sha256:")
    assert first.manifest.route_version == later.manifest.route_version


def test_routed_bar_success_rejects_manifest_source_or_version_mismatch() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutedBarSuccess,
        make_routing_manifest,
    )

    result = bar_result(ProviderId.AKSHARE, DIGEST_A)
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=QUERY),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_B,
        **upstream_fields(MarketCapability.BARS),
    )

    with pytest.raises(ValidationError, match="source"):
        RoutedBarSuccess(result=result, manifest=manifest)


def test_bar_no_provider_wrapper_keeps_full_failure_and_ordered_audit() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutedBarFailure,
        RoutingAttempt,
        RoutingDecision,
        make_failure_audit,
    )

    attempts = tuple(
        RoutingAttempt.create(
            ordinal=index,
            source=source,
            category=MarketCapability.BARS,
            decision=RoutingDecision.FETCH_FAILURE,
            reason=reason,
        )
        for index, (source, reason) in enumerate(
            (
                (ProviderId.TUSHARE, FailureReason.TIMEOUT),
                (ProviderId.AKSHARE, FailureReason.CORRUPT),
            ),
            start=1,
        )
    )
    audit = make_failure_audit(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=QUERY),
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        attempts=attempts,
    )
    failure = BarFailure(
        query=QUERY,
        source=None,
        reason=FailureReason.NO_PROVIDER,
        failed_start=QUERY.start,
        failed_end=QUERY.end,
        detail="no configured provider can satisfy this query",
    )

    outcome = RoutedBarFailure(failure=failure, audit=audit)

    assert outcome.audit.attempts == attempts
    assert outcome.failure.source is None
    assert outcome.failure.reason is FailureReason.NO_PROVIDER
    assert outcome.audit.route_version.startswith("sha256:")


def test_failure_audit_requires_every_priority_entry_in_order() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutingAttempt,
        RoutingDecision,
        make_failure_audit,
    )

    attempt = RoutingAttempt.create(
        ordinal=1,
        source=ProviderId.AKSHARE,
        category=MarketCapability.BARS,
        decision=RoutingDecision.FETCH_FAILURE,
        reason=FailureReason.TIMEOUT,
    )

    with pytest.raises(ValidationError, match="priority"):
        make_failure_audit(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=QUERY),
            priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            attempts=(attempt,),
        )


def test_routed_instrument_success_requires_sorted_unique_matching_batch() -> None:
    from stock_desk.market.provenance import (
        InstrumentRoutingRequest,
        RoutedInstrumentSuccess,
        make_routing_manifest,
    )

    provenance = DatasetProvenance(
        source=ProviderId.TUSHARE,
        fetched_at=datetime(2024, 7, 3, 8, tzinfo=timezone.utc),
        data_cutoff=datetime(2024, 7, 3, 7, tzinfo=timezone.utc),
        dataset_version=DIGEST_A,
    )
    item = Instrument(
        symbol="600000.SH",
        exchange=Exchange.SH,
        name="浦发银行",
        instrument_kind=InstrumentKind.STOCK,
        listing_status=ListingStatus.LISTED,
        listed_on=date(1999, 11, 10),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.INSTRUMENTS,
        request=InstrumentRoutingRequest(),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        upstream_fetched_at=provenance.fetched_at,
        upstream_data_cutoff=provenance.data_cutoff,
        upstream_adjustment=None,
    )

    success = RoutedInstrumentSuccess(
        batch=ProviderBatch[Instrument](items=(item,), provenance=provenance),
        manifest=manifest,
    )

    assert success.batch.items == (item,)
    with pytest.raises(ValidationError, match="sorted unique"):
        RoutedInstrumentSuccess(
            batch=ProviderBatch[Instrument](items=(item, item), provenance=provenance),
            manifest=manifest,
        )


def test_routed_calendar_success_requires_exact_natural_date_range() -> None:
    from stock_desk.market.provenance import (
        CalendarRoutingRequest,
        RoutedCalendarSuccess,
        make_routing_manifest,
    )

    request = CalendarRoutingRequest(
        exchange=Exchange.SH,
        start=date(2024, 7, 1),
        end=date(2024, 7, 3),
    )
    provenance = DatasetProvenance(
        source=ProviderId.BAOSTOCK,
        fetched_at=datetime(2024, 7, 3, 8, tzinfo=timezone.utc),
        data_cutoff=datetime(2024, 7, 2, 8, tzinfo=timezone.utc),
        dataset_version=DIGEST_A,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.TRADING_CALENDAR,
        request=request,
        priority=(ProviderId.BAOSTOCK,),
        attempts=(),
        selected_source=ProviderId.BAOSTOCK,
        upstream_dataset_version=DIGEST_A,
        upstream_fetched_at=provenance.fetched_at,
        upstream_data_cutoff=provenance.data_cutoff,
        upstream_adjustment=None,
    )
    complete = ProviderBatch[TradingDay](
        items=(
            TradingDay(day=date(2024, 7, 1), exchange=Exchange.SH, is_open=True),
            TradingDay(day=date(2024, 7, 2), exchange=Exchange.SH, is_open=True),
        ),
        provenance=provenance,
    )

    success = RoutedCalendarSuccess(batch=complete, manifest=manifest)

    assert len(success.batch.items) == 2
    with pytest.raises(ValidationError, match="natural date"):
        RoutedCalendarSuccess(
            batch=complete.model_copy(update={"items": complete.items[:1]}),
            manifest=manifest,
        )


def test_router_batch_terminal_failure_has_fixed_no_provider_context() -> None:
    from stock_desk.market.provenance import (
        InstrumentRoutingRequest,
        RoutedInstrumentFailure,
        RouterBatchFailure,
        make_failure_audit,
    )

    audit = make_failure_audit(
        category=MarketCapability.INSTRUMENTS,
        request=InstrumentRoutingRequest(),
        priority=(),
        attempts=(),
    )
    failure = RouterBatchFailure.no_provider(
        category=MarketCapability.INSTRUMENTS,
    )

    outcome = RoutedInstrumentFailure(failure=failure, audit=audit)

    assert outcome.failure.reason is FailureReason.NO_PROVIDER
    assert outcome.failure.exchange is None
    with pytest.raises(ValidationError, match="fixed safe detail"):
        RouterBatchFailure(
            category=MarketCapability.INSTRUMENTS,
            exchange=None,
            start=None,
            end=None,
            reason=FailureReason.NO_PROVIDER,
            detail="token=TOP-SECRET",
        )
