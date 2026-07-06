from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_desk.formula.context import EvaluationContext
from stock_desk.formula.functions import V1_REGISTRY
from stock_desk.formula.values import IntegerScalar, NumberScalar
from stock_desk.market.lake import manifest_record_id
from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
    make_routing_manifest,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
)


UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
DATASET_VERSION = "sha256:" + "a" * 64


def _routed(period: Period, local_timestamps: tuple[datetime, ...]) -> RoutedBarSuccess:
    timestamps = tuple(value.astimezone(UTC) for value in local_timestamps)
    query = BarQuery(
        symbol="600000.SH",
        period=period,
        adjustment=Adjustment.QFQ,
        start=timestamps[0] - timedelta(hours=1),
        end=timestamps[-1] + timedelta(days=8),
    )
    bars = tuple(
        Bar(
            symbol=query.symbol,
            timestamp=timestamp,
            period=period,
            adjustment=query.adjustment,
            open=Decimal(f"{10 + index}.1"),
            high=Decimal(f"{11 + index}.1"),
            low=Decimal(f"{9 + index}.1"),
            close=Decimal(f"{10 + index}.5"),
            volume=12_300 + index * 100,
        )
        for index, timestamp in enumerate(timestamps)
    )
    fetched_at = timestamps[-1] + timedelta(days=10)
    provenance = Provenance(
        source=ProviderId.TUSHARE,
        fetched_at=fetched_at,
        data_cutoff=timestamps[-1],
        adjustment=query.adjustment,
        dataset_version=DATASET_VERSION,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=provenance,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=DATASET_VERSION,
        upstream_fetched_at=fetched_at,
        upstream_data_cutoff=timestamps[-1],
        upstream_adjustment=query.adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


@pytest.mark.parametrize(
    ("period", "local_timestamps"),
    [
        (
            Period.DAY,
            (
                datetime(2024, 7, 1, tzinfo=SHANGHAI),
                datetime(2024, 7, 3, tzinfo=SHANGHAI),
            ),
        ),
        (
            Period.WEEK,
            (
                datetime(2024, 7, 1, tzinfo=SHANGHAI),
                datetime(2024, 7, 15, tzinfo=SHANGHAI),
            ),
        ),
        (
            Period.MIN60,
            (
                datetime(2024, 7, 1, 9, 30, tzinfo=SHANGHAI),
                datetime(2024, 7, 1, 13, 0, tzinfo=SHANGHAI),
            ),
        ),
    ],
)
def test_context_preserves_every_routed_timestamp_without_resampling(
    period: Period,
    local_timestamps: tuple[datetime, ...],
) -> None:
    routed = _routed(period, local_timestamps)

    context = EvaluationContext.from_routed(routed)

    assert context.timestamps == tuple(bar.timestamp for bar in routed.result.bars)
    assert len(context.field("CLOSE")) == len(routed.result.bars)
    assert context.period is period


def test_context_uses_registry_field_source_unit_and_scale() -> None:
    routed = _routed(
        Period.DAY,
        (datetime(2024, 7, 1, tzinfo=SHANGHAI),),
    )

    context = EvaluationContext.from_routed(routed, registry=V1_REGISTRY)

    assert context.field("VOLUME").to_optional_tuple() == (12_300.0,)
    assert context.field("VOL").to_optional_tuple() == (123.0,)
    assert context.field("V").to_optional_tuple() == (123.0,)
    assert context.field("C").to_optional_tuple() == (10.5,)
    assert context.field_names == V1_REGISTRY.field_names()


def test_context_retains_complete_market_provenance_and_declared_parameters() -> None:
    routed = _routed(
        Period.DAY,
        (datetime(2024, 7, 1, tzinfo=SHANGHAI),),
    )

    context = EvaluationContext.from_routed(
        routed,
        parameters={"N": IntegerScalar(12), "ALPHA": NumberScalar(0.5)},
    )

    assert context.symbol == routed.result.query.symbol
    assert context.adjustment is routed.result.query.adjustment
    assert context.source is routed.result.provenance.source
    assert context.dataset_version == routed.result.provenance.dataset_version
    assert context.route_version == routed.manifest.route_version
    assert context.manifest_record_id == manifest_record_id(routed.manifest)
    assert context.data_cutoff == routed.result.provenance.data_cutoff
    assert context.query_start == routed.result.query.start
    assert context.query_end == routed.result.query.end
    assert context.parameter("N") == IntegerScalar(12)
    assert context.parameter_names == ("ALPHA", "N")

    with pytest.raises(TypeError):
        context.fields["CLOSE"] = context.field("OPEN")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        context.symbol = "000001.SZ"  # type: ignore[misc]
    assert not hasattr(context, "__dict__")


def test_context_rejects_undeclared_or_noncanonical_parameters() -> None:
    routed = _routed(
        Period.DAY,
        (datetime(2024, 7, 1, tzinfo=SHANGHAI),),
    )
    with pytest.raises(ValueError, match="parameter"):
        EvaluationContext.from_routed(
            routed,
            parameters={"not-canonical": IntegerScalar(1)},
        )


def _valid_direct_context_kwargs() -> dict[str, object]:
    routed = _routed(
        Period.DAY,
        (
            datetime(2024, 7, 1, tzinfo=SHANGHAI),
            datetime(2024, 7, 2, tzinfo=SHANGHAI),
        ),
    )
    context = EvaluationContext.from_routed(
        routed,
        parameters={"ALPHA": NumberScalar(0.5), "N": IntegerScalar(12)},
    )
    return {
        "symbol": context.symbol,
        "period": context.period,
        "adjustment": context.adjustment,
        "source": context.source,
        "dataset_version": context.dataset_version,
        "route_version": context.route_version,
        "manifest_record_id": context.manifest_record_id,
        "data_cutoff": context.data_cutoff,
        "query_start": context.query_start,
        "query_end": context.query_end,
        "timestamps": context.timestamps,
        "fields": context.fields,
        "parameters": context.parameters,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("symbol", "bad", "symbol"),
        ("period", "1d", "period"),
        ("adjustment", "qfq", "adjustment"),
        ("source", "tushare", "source"),
        ("dataset_version", "bad", "digest"),
        ("route_version", "bad", "digest"),
        ("manifest_record_id", "bad", "digest"),
        ("data_cutoff", datetime(2024, 7, 3), "UTC"),
    ],
)
def test_direct_context_rejects_invalid_strict_identity_and_provenance(
    field: str,
    value: object,
    message: str,
) -> None:
    kwargs = _valid_direct_context_kwargs()
    kwargs[field] = value
    with pytest.raises((TypeError, ValueError), match=message):
        EvaluationContext(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"timestamps": ()}, "nonempty"),
        (
            {
                "timestamps": (
                    datetime(2024, 7, 1, 16, tzinfo=UTC),
                    datetime(2024, 7, 1, 16, tzinfo=UTC),
                )
            },
            "increasing",
        ),
        (
            {"query_start": datetime(2024, 7, 1, 17, tzinfo=UTC)},
            "query range",
        ),
        (
            {"data_cutoff": datetime(2024, 6, 30, tzinfo=UTC)},
            "cutoff",
        ),
        (
            {"timestamps": (datetime(2024, 7, 1, 12, tzinfo=UTC),)},
            "bucket",
        ),
    ],
)
def test_direct_context_rejects_invalid_temporal_invariants(
    updates: dict[str, object],
    message: str,
) -> None:
    kwargs = _valid_direct_context_kwargs()
    kwargs.update(updates)
    with pytest.raises(ValueError, match=message):
        EvaluationContext(**kwargs)  # type: ignore[arg-type]


def test_direct_context_requires_exact_registry_fields_and_lengths() -> None:
    kwargs = _valid_direct_context_kwargs()
    fields = dict(kwargs["fields"])  # type: ignore[arg-type]
    fields.pop("CLOSE")
    kwargs["fields"] = fields
    with pytest.raises(ValueError, match="fields"):
        EvaluationContext(**kwargs)  # type: ignore[arg-type]

    kwargs = _valid_direct_context_kwargs()
    fields = dict(kwargs["fields"])  # type: ignore[arg-type]
    fields["CLOSE"] = fields["CLOSE"].__class__.from_optional((1.0,))
    kwargs["fields"] = fields
    with pytest.raises(ValueError, match="length"):
        EvaluationContext(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["C", "O", "H", "L", "VOL", "V"])
def test_direct_context_rejects_alias_value_drift(name: str) -> None:
    kwargs = _valid_direct_context_kwargs()
    fields = dict(kwargs["fields"])  # type: ignore[arg-type]
    fields[name] = fields[name].__class__.from_optional((999.0, 999.0))
    kwargs["fields"] = fields
    with pytest.raises(ValueError, match="alias"):
        EvaluationContext(**kwargs)  # type: ignore[arg-type]


def test_direct_context_rejects_alias_validity_mask_drift() -> None:
    kwargs = _valid_direct_context_kwargs()
    fields = dict(kwargs["fields"])  # type: ignore[arg-type]
    fields["VOL"] = fields["VOL"].__class__.from_optional((None, 124.0))
    kwargs["fields"] = fields
    with pytest.raises(ValueError, match="alias"):
        EvaluationContext(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "parameters",
    [
        {"not-canonical": IntegerScalar(1)},
        {"N": 1},
        {"CLOSE": IntegerScalar(1)},
        {"N": IntegerScalar(1), "ALPHA": NumberScalar(0.5)},
        {f"P{index:02d}": IntegerScalar(index) for index in range(65)},
    ],
)
def test_direct_context_rejects_invalid_parameters(parameters: object) -> None:
    kwargs = _valid_direct_context_kwargs()
    kwargs["parameters"] = parameters
    with pytest.raises((TypeError, ValueError), match="parameter"):
        EvaluationContext(**kwargs)  # type: ignore[arg-type]


def test_market_bucket_validator_is_public_and_shared() -> None:
    from stock_desk.market.types import is_canonical_bucket_start

    assert is_canonical_bucket_start(datetime(2024, 6, 30, 16, tzinfo=UTC), Period.DAY)
    assert not is_canonical_bucket_start(
        datetime(2024, 7, 1, 12, tzinfo=UTC), Period.DAY
    )
