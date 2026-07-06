from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_desk.formula.context import EvaluationContext
from stock_desk.formula.context import MAX_PARAMETERS
from stock_desk.formula.functions.base import IDENTIFIER_PATTERN, MAX_IDENTIFIER_CHARS
from stock_desk.formula.values import IntegerScalar, NumberScalar
from stock_desk.market.provenance import Sha256Digest
from stock_desk.market.types import (
    MAX_BAR_SERIES_ROWS,
    Adjustment,
    CanonicalSymbol,
    Period,
    ProviderId,
    UtcDatetime,
    is_canonical_bucket_start,
)


MAX_PUBLIC_OUTPUTS = 32
MAX_OUTPUT_CELLS = 3_200_000
MAX_RUNTIME_DIAGNOSTICS = 32
MAX_SIGNAL_SERIES_BYTES = 128 * 1024 * 1024
SIGNAL_SERIES_SCHEMA: Literal["stock-desk-signal-series-v1"] = (
    "stock-desk-signal-series-v1"
)
ENGINE_VERSION: Literal["formula-engine-v1"] = "formula-engine-v1"
COMPATIBILITY_VERSION: Literal["tdx-v1"] = "tdx-v1"

BoundedId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=r"^\S+$"),
]
CanonicalName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=MAX_IDENTIFIER_CHARS,
        pattern=IDENTIFIER_PATTERN.pattern,
    ),
]
DiagnosticCode = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$", max_length=64),
]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        revalidate_instances="always",
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is not None:
            raise TypeError("frozen contract model_copy does not accept update")
        return super().model_copy(deep=deep)


class FormulaReference(_FrozenContract):
    formula_id: BoundedId
    formula_version_id: BoundedId
    version: Annotated[int, Field(ge=1)]
    checksum: Sha256Digest


class NormalizedParameter(_FrozenContract):
    name: CanonicalName
    kind: Literal["integer", "number"]
    value: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        if self.kind == "integer":
            if (
                re.fullmatch(r"-?(?:0|[1-9][0-9]*)", self.value) is None
                or self.value == "-0"
                or abs(int(self.value)) > 2**53
            ):
                raise ValueError("integer parameter must use canonical integer text")
            return self
        try:
            number = float(self.value)
        except ValueError as error:
            raise ValueError(
                "number parameter must use canonical float text"
            ) from error
        if not math.isfinite(number) or _canonical_number_text(number) != self.value:
            raise ValueError("number parameter must use canonical float text")
        return self


def _leading_null_count(values: tuple[object | None, ...]) -> int:
    count = 0
    for value in values:
        if value is not None:
            break
        count += 1
    return count


class NumericOutput(_FrozenContract):
    name: CanonicalName
    values: Annotated[tuple[float | None, ...], Field(max_length=MAX_BAR_SERIES_ROWS)]
    warmup_null_count: Annotated[int, Field(ge=0)]

    @field_validator("name")
    @classmethod
    def reserve_signal_names(cls, value: str) -> str:
        if value in {"BUY", "SELL"}:
            raise ValueError("numeric output name cannot be BUY or SELL")
        return value

    @field_validator("values", mode="before")
    @classmethod
    def canonicalize_values(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)):
            return value
        if len(value) > MAX_BAR_SERIES_ROWS:
            raise ValueError("numeric output exceeds the public row limit")
        normalized: list[float | None] = []
        for item in value:
            if item is None:
                normalized.append(None)
                continue
            if type(item) is not float:
                raise ValueError("numeric output values must be floats or null")
            if not math.isfinite(item):
                raise ValueError("numeric output values must be finite or null")
            normalized.append(0.0 if item == 0.0 else item)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_warmup(self) -> Self:
        if self.warmup_null_count != _leading_null_count(self.values):
            raise ValueError("warm-up null count must match the leading null prefix")
        if len(self.values) > MAX_BAR_SERIES_ROWS:
            raise ValueError("numeric output exceeds the public row limit")
        return self


class BooleanSignal(_FrozenContract):
    name: Literal["BUY", "SELL"]
    values: Annotated[tuple[bool | None, ...], Field(max_length=MAX_BAR_SERIES_ROWS)]
    warmup_null_count: Annotated[int, Field(ge=0)]

    @field_validator("values", mode="before")
    @classmethod
    def reject_oversized_values(cls, value: object) -> object:
        if isinstance(value, (tuple, list)) and len(value) > MAX_BAR_SERIES_ROWS:
            raise ValueError("boolean signal exceeds the public row limit")
        return value

    @model_validator(mode="after")
    def validate_warmup(self) -> Self:
        if self.warmup_null_count != _leading_null_count(self.values):
            raise ValueError("warm-up null count must match the leading null prefix")
        if len(self.values) > MAX_BAR_SERIES_ROWS:
            raise ValueError("boolean signal exceeds the public row limit")
        return self


class DiagnosticSpan(_FrozenContract):
    line: Annotated[int, Field(ge=1)]
    column: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    end_column: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if (self.end_line, self.end_column) < (self.line, self.column):
            raise ValueError("diagnostic span end cannot precede its start")
        return self


class RuntimeDiagnostic(_FrozenContract):
    code: DiagnosticCode
    span: DiagnosticSpan | None
    output: CanonicalName | None
    count: Annotated[int, Field(ge=1, le=MAX_BAR_SERIES_ROWS)]
    first_index: Annotated[int, Field(ge=0, lt=MAX_BAR_SERIES_ROWS)]


class SignalSeries(_FrozenContract):
    schema_version: Literal["stock-desk-signal-series-v1"] = SIGNAL_SERIES_SCHEMA
    signal_series_id: Sha256Digest
    formula_id: BoundedId
    formula_version_id: BoundedId
    formula_version: Annotated[int, Field(ge=1)]
    formula_checksum: Sha256Digest
    engine_version: Literal["formula-engine-v1"] = ENGINE_VERSION
    compatibility_version: Literal["tdx-v1"] = COMPATIBILITY_VERSION
    symbol: CanonicalSymbol
    source: ProviderId
    period: Period
    adjustment: Adjustment
    dataset_version: Sha256Digest
    route_version: Sha256Digest
    manifest_record_id: Sha256Digest
    data_cutoff: UtcDatetime
    query_start: UtcDatetime
    query_end: UtcDatetime
    parameters: Annotated[
        tuple[NormalizedParameter, ...], Field(max_length=MAX_PARAMETERS)
    ]
    timestamps: Annotated[
        tuple[UtcDatetime, ...], Field(max_length=MAX_BAR_SERIES_ROWS)
    ]
    numeric_outputs: Annotated[
        tuple[NumericOutput, ...], Field(max_length=MAX_PUBLIC_OUTPUTS)
    ]
    signals: Annotated[tuple[BooleanSignal, ...], Field(max_length=2)]
    runtime_diagnostics: Annotated[
        tuple[RuntimeDiagnostic, ...], Field(max_length=MAX_RUNTIME_DIAGNOSTICS)
    ]

    @model_validator(mode="before")
    @classmethod
    def reject_oversized_payload(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        timestamps = value.get("timestamps")
        parameters = value.get("parameters")
        outputs = value.get("numeric_outputs")
        signals = value.get("signals")
        diagnostics = value.get("runtime_diagnostics")
        row_count = len(timestamps) if isinstance(timestamps, (tuple, list)) else 0
        output_count = len(outputs) if isinstance(outputs, (tuple, list)) else 0
        signal_count = len(signals) if isinstance(signals, (tuple, list)) else 0
        if row_count > MAX_BAR_SERIES_ROWS:
            raise ValueError("timestamp count exceeds the public row limit")
        if isinstance(parameters, (tuple, list)) and len(parameters) > MAX_PARAMETERS:
            raise ValueError("parameter limit exceeded")
        if output_count > MAX_PUBLIC_OUTPUTS:
            raise ValueError("public output limit exceeded")
        if row_count * (output_count + signal_count) > MAX_OUTPUT_CELLS:
            raise ValueError("public output cell limit exceeded")
        if (
            isinstance(diagnostics, (tuple, list))
            and len(diagnostics) > MAX_RUNTIME_DIAGNOSTICS
        ):
            raise ValueError("runtime diagnostic limit exceeded")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        row_count = len(self.timestamps)
        if not 0 < row_count <= MAX_BAR_SERIES_ROWS:
            raise ValueError("timestamp count exceeds the public row limit")
        if len(self.parameters) > MAX_PARAMETERS:
            raise ValueError("parameter limit exceeded")
        if len(self.numeric_outputs) > MAX_PUBLIC_OUTPUTS:
            raise ValueError("public output limit exceeded")
        if (
            row_count * (len(self.numeric_outputs) + len(self.signals))
            > MAX_OUTPUT_CELLS
        ):
            raise ValueError("public output cell limit exceeded")
        if len(self.runtime_diagnostics) > MAX_RUNTIME_DIAGNOSTICS:
            raise ValueError("runtime diagnostic limit exceeded")
        if self.query_start >= self.query_end:
            raise ValueError("query range must be nonempty")
        if any(
            current <= previous
            for previous, current in zip(
                self.timestamps[:-1],
                self.timestamps[1:],
                strict=True,
            )
        ):
            raise ValueError("timestamps must be strictly increasing")
        if not all(
            self.query_start <= item < self.query_end for item in self.timestamps
        ):
            raise ValueError("timestamps must remain inside the query range")
        if self.data_cutoff < self.timestamps[-1]:
            raise ValueError("data cutoff must include the final timestamp")
        if not all(
            is_canonical_bucket_start(item, self.period) for item in self.timestamps
        ):
            raise ValueError("timestamp is not a canonical period bucket")

        parameter_names = tuple(item.name for item in self.parameters)
        if parameter_names != tuple(sorted(parameter_names)) or len(
            parameter_names
        ) != len(set(parameter_names)):
            raise ValueError("parameters must be uniquely sorted by name")
        output_names = tuple(item.name for item in self.numeric_outputs)
        if len(output_names) != len(set(output_names)):
            raise ValueError("numeric output names must be unique")
        if tuple(item.name for item in self.signals) != ("BUY", "SELL"):
            raise ValueError("signals must use BUY, SELL fixed order")
        if any(len(item.values) != row_count for item in self.numeric_outputs):
            raise ValueError("numeric output length must match timestamps")
        if any(len(item.values) != row_count for item in self.signals):
            raise ValueError("signal length must match timestamps")
        known_outputs = set(output_names) | {"BUY", "SELL"}
        if self.runtime_diagnostics != tuple(
            sorted(self.runtime_diagnostics, key=_diagnostic_sort_key)
        ):
            raise ValueError("runtime diagnostics must use canonical order")
        diagnostic_keys: set[tuple[object, ...]] = set()
        for item in self.runtime_diagnostics:
            if item.first_index >= row_count:
                raise ValueError("diagnostic first index must address an input row")
            if item.count > row_count:
                raise ValueError("diagnostic count cannot exceed the input row count")
            if item.output is not None and item.output not in known_outputs:
                raise ValueError("diagnostic output must name a public result")
            key = (item.code, item.span, item.output)
            if key in diagnostic_keys:
                raise ValueError("runtime diagnostics must be aggregated")
            diagnostic_keys.add(key)

        if self.signal_series_id != _signal_series_identity(self):
            raise ValueError("signal_series_id does not match canonical payload")
        return self

    def canonical_json_bytes(self) -> bytes:
        validated = SignalSeries.model_validate(self.model_dump(mode="python"))
        payload = _canonical_bytes(validated.model_dump(mode="json"))
        if len(payload) > MAX_SIGNAL_SERIES_BYTES:
            raise ValueError(
                "SignalSeries canonical JSON exceeds the public byte limit"
            )
        return payload

    @classmethod
    def from_canonical_json_bytes(cls, payload: bytes) -> Self:
        if type(payload) is not bytes:
            raise TypeError("canonical SignalSeries payload must be bytes")
        if len(payload) > MAX_SIGNAL_SERIES_BYTES:
            raise ValueError("SignalSeries payload exceeds the public byte limit")
        value = cls.model_validate_json(payload, strict=False)
        if value.canonical_json_bytes() != payload:
            raise ValueError("SignalSeries payload is not canonical JSON")
        return value


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _signal_series_identity(value: SignalSeries) -> str:
    payload = value.model_dump(mode="json", exclude={"signal_series_id"})
    return f"sha256:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"


def _diagnostic_sort_key(
    value: RuntimeDiagnostic,
) -> tuple[str, str, int, int, int, int, int]:
    span = value.span
    return (
        value.code,
        value.output or "",
        span.line if span is not None else 0,
        span.column if span is not None else 0,
        span.end_line if span is not None else 0,
        span.end_column if span is not None else 0,
        value.first_index,
    )


def _canonical_number_text(value: float) -> str:
    if value == 0.0:
        return "0"
    text = repr(value)
    if "e" not in text:
        return text
    mantissa, exponent = text.split("e", maxsplit=1)
    sign = exponent[0]
    digits = exponent[1:].lstrip("0") or "0"
    return f"{mantissa}e{sign}{digits}"


def _context_parameters(context: EvaluationContext) -> tuple[NormalizedParameter, ...]:
    normalized: list[NormalizedParameter] = []
    for name, value in context.parameters.items():
        if type(value) is IntegerScalar:
            normalized.append(
                NormalizedParameter(name=name, kind="integer", value=str(value.value))
            )
        elif type(value) is NumberScalar:
            normalized.append(
                NormalizedParameter(
                    name=name,
                    kind="number",
                    value=_canonical_number_text(value.value),
                )
            )
        else:  # pragma: no cover - guarded by EvaluationContext
            raise TypeError("context contains an invalid parameter value")
    return tuple(normalized)


def make_signal_series(
    *,
    formula: FormulaReference,
    context: EvaluationContext,
    numeric_outputs: Sequence[NumericOutput],
    signals: Sequence[BooleanSignal],
    runtime_diagnostics: Sequence[RuntimeDiagnostic] = (),
) -> SignalSeries:
    row_count = len(context.timestamps)
    if row_count > MAX_BAR_SERIES_ROWS:
        raise ValueError("formula input exceeds the public row limit")
    if len(context.parameters) > MAX_PARAMETERS:
        raise ValueError("formula parameter limit exceeded")
    if len(numeric_outputs) > MAX_PUBLIC_OUTPUTS:
        raise ValueError("formula public output limit exceeded")
    if len(signals) != 2:
        raise ValueError("formula signals must contain exactly BUY and SELL")
    if len(runtime_diagnostics) > MAX_RUNTIME_DIAGNOSTICS:
        raise ValueError("formula runtime diagnostic limit exceeded")
    if row_count * (len(numeric_outputs) + len(signals)) > MAX_OUTPUT_CELLS:
        raise ValueError("formula public output cell limit exceeded")
    parameter_values = _context_parameters(context)
    output_values = tuple(numeric_outputs)
    signal_values = tuple(signals)
    diagnostic_values = tuple(sorted(runtime_diagnostics, key=_diagnostic_sort_key))

    fields: dict[str, Any] = {
        "schema_version": SIGNAL_SERIES_SCHEMA,
        "formula_id": formula.formula_id,
        "formula_version_id": formula.formula_version_id,
        "formula_version": formula.version,
        "formula_checksum": formula.checksum,
        "engine_version": ENGINE_VERSION,
        "compatibility_version": COMPATIBILITY_VERSION,
        "symbol": context.symbol,
        "source": context.source,
        "period": context.period,
        "adjustment": context.adjustment,
        "dataset_version": context.dataset_version,
        "route_version": context.route_version,
        "manifest_record_id": context.manifest_record_id,
        "data_cutoff": context.data_cutoff,
        "query_start": context.query_start,
        "query_end": context.query_end,
        "parameters": parameter_values,
        "timestamps": context.timestamps,
        "numeric_outputs": output_values,
        "signals": signal_values,
        "runtime_diagnostics": diagnostic_values,
    }
    provisional = SignalSeries.model_construct(
        signal_series_id="sha256:" + "0" * 64,
        **fields,
    )
    fields["signal_series_id"] = _signal_series_identity(provisional)
    return SignalSeries(**fields)
