from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from types import MappingProxyType

import numpy as np

from stock_desk.formula.functions.base import IDENTIFIER_PATTERN
from stock_desk.formula.functions.base import FieldSpec
from stock_desk.formula.functions.registry import (
    V1_REGISTRY,
    CompatibilityRegistry,
)
from stock_desk.formula.values import (
    IntegerScalar,
    NumberScalar,
    NumberSeries,
    ScalarValue,
)
from stock_desk.market.lake import manifest_record_id
from stock_desk.market.provenance import RoutedBarSuccess, Sha256Digest
from stock_desk.market.types import (
    MAX_BAR_SERIES_ROWS,
    Adjustment,
    CanonicalSymbol,
    Period,
    ProviderId,
    UtcDatetime,
    is_canonical_bucket_start,
)


MAX_PARAMETERS = 64
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _require_exact_utc(value: object, label: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise TypeError(f"{label} must be an exact UTC datetime")
    return value


def _scaled_values(
    values: np.ndarray[tuple[int], np.dtype[np.float64]],
    spec: FieldSpec,
) -> np.ndarray[tuple[int], np.dtype[np.float64]]:
    return values * (spec.scale_numerator / spec.scale_denominator)


def _field_matches_spec(
    fields: Mapping[str, NumberSeries],
    spec: FieldSpec,
) -> bool:
    source = fields[spec.source_name]
    candidate = fields[spec.name]
    return bool(
        np.array_equal(candidate.valid, source.valid)
        and np.array_equal(candidate.values, _scaled_values(source.values, spec))
    )


@dataclass(frozen=True, slots=True, init=False)
class EvaluationContext:
    symbol: CanonicalSymbol
    period: Period
    adjustment: Adjustment
    source: ProviderId
    dataset_version: Sha256Digest
    route_version: Sha256Digest
    manifest_record_id: Sha256Digest
    data_cutoff: UtcDatetime
    query_start: UtcDatetime
    query_end: UtcDatetime
    timestamps: tuple[UtcDatetime, ...]
    fields: Mapping[str, NumberSeries] = field(repr=False)
    parameters: Mapping[str, ScalarValue] = field(repr=False)

    def __init__(
        self,
        *,
        symbol: CanonicalSymbol,
        period: Period,
        adjustment: Adjustment,
        source: ProviderId,
        dataset_version: Sha256Digest,
        route_version: Sha256Digest,
        manifest_record_id: Sha256Digest,
        data_cutoff: UtcDatetime,
        query_start: UtcDatetime,
        query_end: UtcDatetime,
        timestamps: tuple[UtcDatetime, ...],
        fields: Mapping[str, NumberSeries],
        parameters: Mapping[str, ScalarValue],
        registry: CompatibilityRegistry = V1_REGISTRY,
    ) -> None:
        if type(symbol) is not str or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            raise ValueError("context symbol is invalid")
        if type(period) is not Period:
            raise TypeError("context period must be a Period")
        if type(adjustment) is not Adjustment:
            raise TypeError("context adjustment must be an Adjustment")
        if type(source) is not ProviderId:
            raise TypeError("context source must be a ProviderId")
        for value, label in (
            (dataset_version, "dataset version"),
            (route_version, "route version"),
            (manifest_record_id, "manifest record id"),
        ):
            if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
                raise ValueError(f"context {label} must be a sha256 digest")

        cutoff = _require_exact_utc(data_cutoff, "context data cutoff")
        start = _require_exact_utc(query_start, "context query start")
        end = _require_exact_utc(query_end, "context query end")
        if start >= end:
            raise ValueError("context query range must be nonempty")
        if type(timestamps) is not tuple or not timestamps:
            raise ValueError("context timestamps must be a nonempty tuple")
        if len(timestamps) > MAX_BAR_SERIES_ROWS:
            raise ValueError("context timestamps exceed the public row limit")
        canonical_timestamps = tuple(
            _require_exact_utc(value, "context timestamp") for value in timestamps
        )
        if any(
            current <= previous
            for previous, current in zip(
                canonical_timestamps[:-1], canonical_timestamps[1:], strict=True
            )
        ):
            raise ValueError("context timestamps must be strictly increasing")
        if not all(start <= value < end for value in canonical_timestamps):
            raise ValueError("context timestamps must remain inside the query range")
        if cutoff < canonical_timestamps[-1]:
            raise ValueError("context data cutoff must include the final timestamp")
        if not all(
            is_canonical_bucket_start(value, period) for value in canonical_timestamps
        ):
            raise ValueError("context timestamp is not a canonical period bucket")

        if not isinstance(fields, Mapping):
            raise TypeError("context fields must be a mapping")
        expected_fields = frozenset(registry.field_names())
        if frozenset(fields) != expected_fields:
            raise ValueError("context fields must exactly match the registry fields")
        if any(type(value) is not NumberSeries for value in fields.values()):
            raise TypeError("context fields must contain exact NumberSeries values")
        if any(len(value) != len(canonical_timestamps) for value in fields.values()):
            raise ValueError("context field length must match timestamps")
        if any(not _field_matches_spec(fields, spec) for spec in registry.fields()):
            raise ValueError(
                "context field aliases must match source values, scale, and validity"
            )

        if not isinstance(parameters, Mapping):
            raise TypeError("context parameters must be a mapping")
        parameter_names = tuple(parameters)
        if len(parameter_names) > MAX_PARAMETERS:
            raise ValueError("context parameter limit exceeded")
        if parameter_names != tuple(sorted(parameter_names)):
            raise ValueError("context parameters must be sorted by name")
        if any(
            type(name) is not str
            or IDENTIFIER_PATTERN.fullmatch(name) is None
            or name in expected_fields
            for name in parameter_names
        ):
            raise ValueError("context parameter names must be canonical and unreserved")
        if any(
            type(value) not in (NumberScalar, IntegerScalar)
            for value in parameters.values()
        ):
            raise TypeError("context parameter values must be exact ScalarValue types")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "period", period)
        object.__setattr__(self, "adjustment", adjustment)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "dataset_version", dataset_version)
        object.__setattr__(self, "route_version", route_version)
        object.__setattr__(self, "manifest_record_id", manifest_record_id)
        object.__setattr__(self, "data_cutoff", cutoff)
        object.__setattr__(self, "query_start", start)
        object.__setattr__(self, "query_end", end)
        object.__setattr__(self, "timestamps", canonical_timestamps)
        object.__setattr__(self, "fields", MappingProxyType(dict(fields)))
        object.__setattr__(self, "parameters", MappingProxyType(dict(parameters)))

    @classmethod
    def from_routed(
        cls,
        routed: RoutedBarSuccess,
        *,
        registry: CompatibilityRegistry = V1_REGISTRY,
        parameters: Mapping[str, ScalarValue] | None = None,
    ) -> EvaluationContext:
        bars = routed.result.bars
        row_count = len(bars)
        canonical_parameters: dict[str, ScalarValue] = {}
        for raw_name, value in (parameters or {}).items():
            name = raw_name.upper()
            if (
                IDENTIFIER_PATTERN.fullmatch(raw_name) is None
                or name in canonical_parameters
                or not isinstance(value, (NumberScalar, IntegerScalar))
            ):
                raise ValueError("formula parameter declarations must be canonical")
            canonical_parameters[name] = value

        source_values = {
            "OPEN": tuple(float(bar.open) for bar in bars),
            "HIGH": tuple(float(bar.high) for bar in bars),
            "LOW": tuple(float(bar.low) for bar in bars),
            "CLOSE": tuple(float(bar.close) for bar in bars),
            "VOLUME": tuple(float(bar.volume) for bar in bars),
        }
        fields: dict[str, NumberSeries] = {}
        for spec in registry.fields():
            values = source_values[spec.source_name]
            source_data = np.fromiter(
                values,
                dtype=np.float64,
                count=row_count,
            )
            data = _scaled_values(source_data, spec)
            fields[spec.name] = NumberSeries(
                data,
                np.ones(row_count, dtype=np.bool_),
            )

        result = routed.result
        query = result.query
        return cls(
            symbol=query.symbol,
            period=query.period,
            adjustment=query.adjustment,
            source=result.provenance.source,
            dataset_version=result.provenance.dataset_version,
            route_version=routed.manifest.route_version,
            manifest_record_id=manifest_record_id(routed.manifest),
            data_cutoff=result.provenance.data_cutoff,
            query_start=query.start,
            query_end=query.end,
            timestamps=tuple(bar.timestamp for bar in bars),
            fields=fields,
            parameters=dict(sorted(canonical_parameters.items())),
            registry=registry,
        )

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.fields))

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(self.parameters)

    def field(self, name: str) -> NumberSeries:
        return self.fields[name.upper()]

    def parameter(self, name: str) -> ScalarValue:
        return self.parameters[name.upper()]
