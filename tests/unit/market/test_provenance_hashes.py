# ruff: noqa: F403, F405
"""Deterministic route hash and tamper-resistance contracts."""

from __future__ import annotations

from tests.unit.market.provenance_test_helpers import *  # noqa: F403


def _single_source_bar_manifest(query: BarQuery):
    from stock_desk.market.provenance import BarRoutingRequest, make_routing_manifest

    return make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )


def test_default_stock_query_retains_published_v1_manifest_identity() -> None:
    from stock_desk.market.lake import manifest_record_id

    manifest = _single_source_bar_manifest(QUERY)

    assert (
        manifest.model_dump(mode="json")["request"]["query"]["instrument_kind"]
        == "stock"
    )
    assert (
        manifest.route_version
        == "sha256:a2391b572c60a8ff4b47c457298abe97799c8407965de77da3ed2f9f2518ddc8"
    )
    assert (
        manifest_record_id(manifest)
        == "sha256:fd50c060baeef0cafd14927c67011247bb43470eaf5fe07674670db8f7066890"
    )


def test_explicit_non_stock_query_kind_remains_identity_bound() -> None:
    from stock_desk.market.lake import manifest_record_id

    common = {
        "symbol": "510300.SH",
        "period": Period.DAY,
        "adjustment": Adjustment.NONE,
        "start": QUERY.start,
        "end": QUERY.end,
    }
    stock = _single_source_bar_manifest(BarQuery(**common))
    etf = _single_source_bar_manifest(
        BarQuery(**common, instrument_kind=InstrumentKind.ETF)
    )
    index = _single_source_bar_manifest(
        BarQuery(
            **{**common, "symbol": "000001.SS"},
            instrument_kind=InstrumentKind.INDEX,
        )
    )

    assert (
        stock.model_dump(mode="json")["request"]["query"]["instrument_kind"] == "stock"
    )
    assert etf.model_dump(mode="json")["request"]["query"]["instrument_kind"] == "etf"
    assert (
        index.model_dump(mode="json")["request"]["query"]["instrument_kind"] == "index"
    )
    assert stock.route_version != etf.route_version
    assert manifest_record_id(stock) != manifest_record_id(etf)


def test_default_stock_provider_dataset_identity_remains_published() -> None:
    from stock_desk.market.providers.normalization import dataset_version

    stock = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": QUERY},
        data_cutoff=DATA_CUTOFF,
        items=(),
    )
    etf = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={
            "query": QUERY.model_copy(update={"instrument_kind": InstrumentKind.ETF})
        },
        data_cutoff=DATA_CUTOFF,
        items=(),
    )

    assert (
        stock
        == "sha256:36a68655d1676bdd736365ce9a883a5830cdd771b951c6d6556f8049e19d6e52"
    )
    assert etf != stock


def test_route_version_changes_when_transition_semantics_change() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        SourceTransition,
        TransitionReason,
        make_routing_manifest,
    )

    request = BarRoutingRequest(query=QUERY)
    base = dict(
        category=MarketCapability.BARS,
        request=request,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_B,
        **upstream_fields(MarketCapability.BARS),
    )
    without_transition = make_routing_manifest(**base)
    transition = SourceTransition(
        category=MarketCapability.BARS,
        from_source=ProviderId.AKSHARE,
        to_source=ProviderId.TUSHARE,
        from_dataset_version=DIGEST_A,
        to_dataset_version=DIGEST_B,
        from_route_version=DIGEST_C,
        effective_at=QUERY.start,
        calendar_start=None,
        calendar_end=None,
        reason=TransitionReason.HIGHER_PRIORITY_RECOVERED,
    )
    with_transition = make_routing_manifest(**base, transition=transition)

    assert without_transition.route_version != with_transition.route_version


def test_route_version_semantic_state_table_changes_every_hash() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutingAttempt,
        RoutingDecision,
        make_routing_manifest,
    )

    def attempt(
        ordinal: int,
        source: ProviderId,
        decision: RoutingDecision = RoutingDecision.FETCH_FAILURE,
        reason: FailureReason = FailureReason.TIMEOUT,
    ) -> RoutingAttempt:
        return RoutingAttempt.create(
            ordinal=ordinal,
            source=source,
            category=MarketCapability.BARS,
            decision=decision,
            reason=reason,
        )

    changed_query = QUERY.model_copy(
        update={"end": datetime(2024, 7, 4, tzinfo=timezone.utc)}
    )
    manifests = (
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=QUERY),
            priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            attempts=(attempt(1, ProviderId.TUSHARE),),
            selected_source=ProviderId.AKSHARE,
            upstream_dataset_version=DIGEST_A,
            **upstream_fields(MarketCapability.BARS),
        ),
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=changed_query),
            priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            attempts=(attempt(1, ProviderId.TUSHARE),),
            selected_source=ProviderId.AKSHARE,
            upstream_dataset_version=DIGEST_A,
            **upstream_fields(MarketCapability.BARS),
        ),
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=QUERY),
            priority=(ProviderId.BAOSTOCK, ProviderId.TUSHARE, ProviderId.AKSHARE),
            attempts=(
                attempt(1, ProviderId.BAOSTOCK),
                attempt(2, ProviderId.TUSHARE),
            ),
            selected_source=ProviderId.AKSHARE,
            upstream_dataset_version=DIGEST_A,
            **upstream_fields(MarketCapability.BARS),
        ),
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=QUERY),
            priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            attempts=(
                attempt(
                    1,
                    ProviderId.TUSHARE,
                    RoutingDecision.REGISTRY_MISSING,
                    FailureReason.PROVIDER_UNAVAILABLE,
                ),
            ),
            selected_source=ProviderId.AKSHARE,
            upstream_dataset_version=DIGEST_A,
            **upstream_fields(MarketCapability.BARS),
        ),
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=QUERY),
            priority=(ProviderId.TUSHARE, ProviderId.BAOSTOCK),
            attempts=(attempt(1, ProviderId.TUSHARE),),
            selected_source=ProviderId.BAOSTOCK,
            upstream_dataset_version=DIGEST_A,
            **upstream_fields(MarketCapability.BARS),
        ),
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=QUERY),
            priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
            attempts=(attempt(1, ProviderId.TUSHARE),),
            selected_source=ProviderId.AKSHARE,
            upstream_dataset_version=DIGEST_B,
            **upstream_fields(MarketCapability.BARS),
        ),
    )

    assert len({item.route_version for item in manifests}) == len(manifests)


def test_route_version_normalizes_equivalent_timezones() -> None:
    from datetime import timedelta

    from stock_desk.market.provenance import BarRoutingRequest, make_routing_manifest

    china_timezone = timezone(timedelta(hours=8))
    equivalent_query = BarQuery(
        symbol=QUERY.symbol,
        period=QUERY.period,
        adjustment=QUERY.adjustment,
        start=datetime(2024, 7, 1, 8, tzinfo=china_timezone),
        end=datetime(2024, 7, 3, 8, tzinfo=china_timezone),
    )
    common = {
        "category": MarketCapability.BARS,
        "priority": (ProviderId.TUSHARE,),
        "attempts": (),
        "selected_source": ProviderId.TUSHARE,
        "upstream_dataset_version": DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    }

    utc_manifest = make_routing_manifest(
        request=BarRoutingRequest(query=QUERY),
        **common,
    )
    china_manifest = make_routing_manifest(
        request=BarRoutingRequest(query=equivalent_query),
        **common,
    )

    assert utc_manifest.route_version == china_manifest.route_version


def test_manifest_builder_normalizes_equivalent_upstream_cutoff_timezones() -> None:
    from datetime import timedelta

    from stock_desk.market.provenance import BarRoutingRequest, make_routing_manifest

    china_timezone = timezone(timedelta(hours=8))
    common = {
        "category": MarketCapability.BARS,
        "request": BarRoutingRequest(query=QUERY),
        "priority": (ProviderId.TUSHARE,),
        "attempts": (),
        "selected_source": ProviderId.TUSHARE,
        "upstream_dataset_version": DIGEST_A,
        "upstream_adjustment": Adjustment.NONE,
    }
    utc_manifest = make_routing_manifest(
        upstream_fetched_at=FETCHED_AT,
        upstream_data_cutoff=DATA_CUTOFF,
        **common,
    )
    china_manifest = make_routing_manifest(
        upstream_fetched_at=FETCHED_AT.astimezone(china_timezone),
        upstream_data_cutoff=DATA_CUTOFF.astimezone(china_timezone),
        **common,
    )

    assert utc_manifest.route_version == china_manifest.route_version
    assert utc_manifest.upstream_data_cutoff == china_manifest.upstream_data_cutoff


def test_route_version_is_stable_across_python_hash_seeds() -> None:
    from pathlib import Path
    import os
    import subprocess
    import sys

    project_root = Path(__file__).parents[3]
    script = """
from stock_desk.market.provenance import InstrumentRoutingRequest, make_routing_manifest
from datetime import datetime, timezone
from stock_desk.market.types import MarketCapability, ProviderId

manifest = make_routing_manifest(
    category=MarketCapability.INSTRUMENTS,
    request=InstrumentRoutingRequest(),
    priority=(ProviderId.TUSHARE,),
    attempts=(),
    selected_source=ProviderId.TUSHARE,
    upstream_dataset_version="sha256:" + "a" * 64,
    upstream_fetched_at=datetime(2024, 7, 3, 8, tzinfo=timezone.utc),
    upstream_data_cutoff=datetime(2024, 7, 2, 8, tzinfo=timezone.utc),
    upstream_adjustment=None,
)
print(manifest.route_version)
"""

    versions = {
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for seed in ("1", "7", "123")
    }

    assert len(versions) == 1


def test_manifest_and_failure_audit_reject_tampered_route_version() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutingFailureAudit,
        RoutingManifest,
        make_failure_audit,
        make_routing_manifest,
    )

    request = BarRoutingRequest(query=QUERY)
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=request,
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )
    audit = make_failure_audit(
        category=MarketCapability.BARS,
        request=request,
        priority=(),
        attempts=(),
    )

    for model, model_type in (
        (manifest, RoutingManifest),
        (audit, RoutingFailureAudit),
    ):
        forged = model_type.model_construct(
            **{**model.__dict__, "route_version": DIGEST_D}
        )
        with pytest.raises(ValidationError, match="route_version"):
            model_type.model_validate(forged.model_dump(mode="python"))


def test_manifest_preserves_upstream_provenance_but_excludes_fetched_at_from_hash() -> (
    None
):
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutedBarSuccess,
        make_routing_manifest,
    )

    result = bar_result(ProviderId.TUSHARE, DIGEST_A)
    request = BarRoutingRequest(query=QUERY)
    common = {
        "category": MarketCapability.BARS,
        "request": request,
        "priority": (ProviderId.TUSHARE,),
        "attempts": (),
        "selected_source": ProviderId.TUSHARE,
        "upstream_dataset_version": result.provenance.dataset_version,
        "upstream_data_cutoff": result.provenance.data_cutoff,
        "upstream_adjustment": result.provenance.adjustment,
    }
    manifest = make_routing_manifest(
        upstream_fetched_at=result.provenance.fetched_at,
        **common,
    )
    later_manifest = make_routing_manifest(
        upstream_fetched_at=datetime(2024, 7, 4, 8, tzinfo=timezone.utc),
        **common,
    )

    assert manifest.upstream_fetched_at == result.provenance.fetched_at
    assert manifest.upstream_data_cutoff == result.provenance.data_cutoff
    assert manifest.upstream_adjustment is result.provenance.adjustment
    assert manifest.route_version == later_manifest.route_version
    assert RoutedBarSuccess(result=result, manifest=manifest).manifest == manifest
    with pytest.raises(ValidationError, match="fetched"):
        RoutedBarSuccess(result=result, manifest=later_manifest)


def test_manifest_versions_require_canonical_lowercase_sha256() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutingFailureAudit,
        RoutingManifest,
        make_failure_audit,
        make_routing_manifest,
    )

    request = BarRoutingRequest(query=QUERY)
    with pytest.raises(ValidationError, match="upstream_dataset_version"):
        make_routing_manifest(
            category=MarketCapability.BARS,
            request=request,
            priority=(ProviderId.TUSHARE,),
            attempts=(),
            selected_source=ProviderId.TUSHARE,
            upstream_dataset_version="sha256:" + "A" * 64,
            **upstream_fields(MarketCapability.BARS),
        )

    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=request,
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )
    audit = make_failure_audit(
        category=MarketCapability.BARS,
        request=request,
        priority=(),
        attempts=(),
    )
    for model, model_type in (
        (manifest, RoutingManifest),
        (audit, RoutingFailureAudit),
    ):
        invalid = model.model_copy(update={"route_version": "sha256:" + "A" * 64})
        with pytest.raises(ValidationError, match="route_version"):
            model_type.model_validate(invalid.model_dump(mode="python"))


def test_manifest_hash_validator_rejects_each_semantic_field_tamper() -> None:
    from datetime import timedelta

    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutingAttempt,
        RoutingDecision,
        RoutingManifest,
        SourceTransition,
        TransitionReason,
        make_routing_manifest,
    )

    request = BarRoutingRequest(query=QUERY)
    attempt = RoutingAttempt.create(
        ordinal=1,
        source=ProviderId.TUSHARE,
        category=MarketCapability.BARS,
        decision=RoutingDecision.FETCH_FAILURE,
        reason=FailureReason.TIMEOUT,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=request,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        attempts=(attempt,),
        selected_source=ProviderId.AKSHARE,
        upstream_dataset_version=DIGEST_A,
        **upstream_fields(MarketCapability.BARS),
    )
    qfq_request = BarRoutingRequest(
        query=QUERY.model_copy(update={"adjustment": Adjustment.QFQ})
    )
    semantic_updates = (
        {
            "request": BarRoutingRequest(
                query=QUERY.model_copy(update={"end": QUERY.end + timedelta(days=1)})
            )
        },
        {"priority": (*manifest.priority, ProviderId.BAOSTOCK)},
        {
            "attempts": (
                RoutingAttempt.create(
                    ordinal=1,
                    source=ProviderId.TUSHARE,
                    category=MarketCapability.BARS,
                    decision=RoutingDecision.FETCH_FAILURE,
                    reason=FailureReason.CORRUPT,
                ),
            )
        },
        {
            "priority": (ProviderId.TUSHARE, ProviderId.BAOSTOCK),
            "selected_source": ProviderId.BAOSTOCK,
        },
        {"upstream_dataset_version": DIGEST_B},
        {"upstream_data_cutoff": DATA_CUTOFF - timedelta(hours=1)},
        {"request": qfq_request, "upstream_adjustment": Adjustment.QFQ},
        {
            "transition": SourceTransition(
                category=MarketCapability.BARS,
                from_source=ProviderId.BAOSTOCK,
                to_source=ProviderId.AKSHARE,
                from_dataset_version=DIGEST_C,
                to_dataset_version=DIGEST_A,
                from_route_version=DIGEST_D,
                effective_at=QUERY.start,
                calendar_start=None,
                calendar_end=None,
                reason=TransitionReason.FALLBACK_AFTER_FAILURE,
            )
        },
    )

    for update in semantic_updates:
        tampered = manifest.model_copy(update=update)
        with pytest.raises(ValidationError, match="route_version"):
            RoutingManifest.model_validate(tampered.model_dump(mode="python"))

    fetched_only = manifest.model_copy(
        update={"upstream_fetched_at": FETCHED_AT + timedelta(days=1)}
    )
    assert (
        RoutingManifest.model_validate(
            fetched_only.model_dump(mode="python")
        ).route_version
        == manifest.route_version
    )


def test_failure_audit_hash_rejects_consistent_priority_and_attempt_tamper() -> None:
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutingAttempt,
        RoutingDecision,
        RoutingFailureAudit,
        make_failure_audit,
    )

    audit = make_failure_audit(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=QUERY),
        priority=(),
        attempts=(),
    )
    tampered = audit.model_copy(
        update={
            "priority": (ProviderId.TUSHARE,),
            "attempts": (
                RoutingAttempt.create(
                    ordinal=1,
                    source=ProviderId.TUSHARE,
                    category=MarketCapability.BARS,
                    decision=RoutingDecision.FETCH_FAILURE,
                    reason=FailureReason.TIMEOUT,
                ),
            ),
        }
    )

    with pytest.raises(ValidationError, match="route_version"):
        RoutingFailureAudit.model_validate(tampered.model_dump(mode="python"))
