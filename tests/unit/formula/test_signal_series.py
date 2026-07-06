from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from pydantic import ValidationError
import pytest

from stock_desk.formula.context import EvaluationContext
from stock_desk.formula.functions import V1_REGISTRY
from stock_desk.formula.functions.base import MAX_IDENTIFIER_CHARS
import stock_desk.formula.signal_series as signal_series_module
from stock_desk.formula.signal_series import (
    MAX_OUTPUT_CELLS,
    MAX_PUBLIC_OUTPUTS,
    MAX_RUNTIME_DIAGNOSTICS,
    BooleanSignal,
    DiagnosticSpan,
    FormulaReference,
    NormalizedParameter,
    NumericOutput,
    RuntimeDiagnostic,
    SignalSeries,
    make_signal_series,
)
from stock_desk.formula.values import IntegerScalar, NumberScalar, NumberSeries
from stock_desk.market.types import (
    MAX_BAR_SERIES_ROWS,
    Adjustment,
    Period,
    ProviderId,
)


UTC = timezone.utc
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


class _ExplodingSequence(Sequence[object]):
    def __init__(self, length: int) -> None:
        self._length = length

    def __len__(self) -> int:
        return self._length

    def __getitem__(self, index: int) -> object:
        raise AssertionError(f"sequence was copied at index {index}")


def _context(
    timestamps: tuple[datetime, ...] | None = None,
    *,
    parameters: dict[str, IntegerScalar | NumberScalar] | None = None,
    period: Period = Period.DAY,
) -> EvaluationContext:
    points = timestamps or (
        datetime(2024, 6, 30, 16, tzinfo=UTC),
        datetime(2024, 7, 1, 16, tzinfo=UTC),
        datetime(2024, 7, 2, 16, tzinfo=UTC),
    )
    source_values = {
        "OPEN": (1.0,) * len(points),
        "HIGH": (2.0,) * len(points),
        "LOW": (0.5,) * len(points),
        "CLOSE": (1.5,) * len(points),
        "VOLUME": (10_000.0,) * len(points),
    }
    fields = {}
    for spec in V1_REGISTRY.fields():
        scale = spec.scale_numerator / spec.scale_denominator
        fields[spec.name] = NumberSeries.from_optional(
            tuple(value * scale for value in source_values[spec.source_name])
        )
    return EvaluationContext(
        symbol="600000.SH",
        period=period,
        adjustment=Adjustment.QFQ,
        source=ProviderId.TUSHARE,
        dataset_version=DIGEST_A,
        route_version=DIGEST_B,
        manifest_record_id=DIGEST_C,
        data_cutoff=points[-1],
        query_start=points[0] - timedelta(days=1),
        query_end=points[-1] + timedelta(days=1),
        timestamps=points,
        fields=fields,
        parameters=dict(sorted((parameters or {}).items())),
    )


def _formula() -> FormulaReference:
    return FormulaReference(
        formula_id="formula-macd",
        formula_version_id="formula-macd-v3",
        version=3,
        checksum=DIGEST_A,
    )


def _series() -> SignalSeries:
    return make_signal_series(
        formula=_formula(),
        context=_context(
            parameters={
                "FAST": IntegerScalar(12),
                "SLOW": IntegerScalar(26),
            }
        ),
        numeric_outputs=(
            NumericOutput(name="DIF", values=(None, -0.0, 1.25), warmup_null_count=1),
            NumericOutput(name="DEA", values=(None, 0.0, 0.75), warmup_null_count=1),
        ),
        signals=(
            BooleanSignal(
                name="BUY",
                values=(None, False, True),
                warmup_null_count=1,
            ),
            BooleanSignal(
                name="SELL",
                values=(None, False, False),
                warmup_null_count=1,
            ),
        ),
        runtime_diagnostics=(
            RuntimeDiagnostic(
                code="division_by_zero",
                span=DiagnosticSpan(line=1, column=5, end_line=1, end_column=10),
                output="DIF",
                count=1,
                first_index=1,
            ),
        ),
    )


def test_signal_series_is_complete_immutable_and_source_ordered() -> None:
    result = _series()

    assert result.schema_version == "stock-desk-signal-series-v1"
    assert result.engine_version == "formula-engine-v1"
    assert result.compatibility_version == "tdx-v1"
    assert result.formula_id == "formula-macd"
    assert result.formula_version_id == "formula-macd-v3"
    assert result.formula_version == 3
    assert result.formula_checksum == DIGEST_A
    assert result.symbol == "600000.SH"
    assert result.dataset_version == DIGEST_A
    assert result.route_version == DIGEST_B
    assert result.manifest_record_id == DIGEST_C
    assert tuple(output.name for output in result.numeric_outputs) == ("DIF", "DEA")
    assert tuple(parameter.name for parameter in result.parameters) == ("FAST", "SLOW")
    assert tuple(parameter.kind for parameter in result.parameters) == (
        "integer",
        "integer",
    )
    assert tuple(signal.name for signal in result.signals) == ("BUY", "SELL")
    assert result.numeric_outputs[0].values == (None, 0.0, 1.25)
    assert math.copysign(1.0, result.numeric_outputs[0].values[1] or 0.0) == 1.0
    assert result.runtime_diagnostics[0].count == 1

    with pytest.raises(ValidationError, match="frozen"):
        result.symbol = "000001.SZ"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        result.numeric_outputs[0].name = "CHANGED"  # type: ignore[misc]


def test_canonical_json_and_content_identity_are_byte_stable() -> None:
    first = _series()
    second = _series()

    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    assert first.signal_series_id == second.signal_series_id
    raw = first.canonical_json_bytes()
    assert b"generated_at" not in raw
    assert raw == json.dumps(
        first.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    payload = first.model_dump(mode="json", exclude={"signal_series_id"})
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    assert first.signal_series_id == f"sha256:{expected}"
    assert SignalSeries.from_canonical_json_bytes(raw) == first


def test_canonical_json_reader_rejects_noncanonical_or_nonfinite_payloads() -> None:
    result = _series()
    raw = result.canonical_json_bytes()

    with pytest.raises(ValueError, match="canonical"):
        SignalSeries.from_canonical_json_bytes(b" " + raw)
    with pytest.raises(ValueError):
        SignalSeries.from_canonical_json_bytes(raw.replace(b"0.0", b"NaN", 1))


def test_signal_series_rejects_identity_tampering_and_extra_fields() -> None:
    result = _series()
    tampered = result.model_dump(mode="python")
    tampered["formula_version"] = 4
    with pytest.raises(ValidationError, match="signal_series_id"):
        SignalSeries.model_validate(tampered)

    with pytest.raises(ValidationError, match="extra"):
        SignalSeries.model_validate(
            {**result.model_dump(mode="python"), "generated_at": datetime.now(UTC)}
        )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_numeric_outputs_reject_non_finite_values(bad: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        NumericOutput(name="DIF", values=(bad,), warmup_null_count=0)


def test_formula_reference_and_parameters_require_saved_canonical_versions() -> None:
    with pytest.raises(ValidationError):
        FormulaReference(
            formula_id="",
            formula_version_id="draft",
            version=0,
            checksum="not-a-digest",
        )
    for value in ("01", "1.0", "-0", "+1", "NaN"):
        with pytest.raises(ValidationError, match="canonical"):
            NormalizedParameter(name="N", kind="integer", value=value)
    with pytest.raises(ValidationError, match="pattern"):
        NormalizedParameter(name="not-canonical", kind="integer", value="1")
    with pytest.raises(ValidationError, match="at most"):
        NormalizedParameter(
            name="A" * (MAX_IDENTIFIER_CHARS + 1), kind="integer", value="1"
        )


def test_lengths_timestamp_order_output_names_and_signal_order_are_enforced() -> None:
    base = _series()
    payload = base.model_dump(mode="python", exclude={"signal_series_id"})

    with pytest.raises(ValueError, match="length"):
        make_signal_series(
            formula=_formula(),
            context=_context(),
            numeric_outputs=(
                NumericOutput(name="DIF", values=(1.0,), warmup_null_count=0),
            ),
            signals=(
                BooleanSignal(name="BUY", values=(True,), warmup_null_count=0),
                BooleanSignal(name="SELL", values=(False,), warmup_null_count=0),
            ),
        )
    with pytest.raises(ValidationError, match="BUY or SELL"):
        NumericOutput(name="BUY", values=(1.0,), warmup_null_count=0)
    with pytest.raises(ValidationError, match="fixed order"):
        SignalSeries.model_validate(
            {
                **payload,
                "signals": tuple(reversed(payload["signals"])),
                "signal_series_id": DIGEST_A,
            }
        )


def test_warmup_prefix_and_aggregated_diagnostics_are_validated() -> None:
    with pytest.raises(ValidationError, match="warm-up"):
        NumericOutput(name="DIF", values=(None, 1.0), warmup_null_count=0)
    with pytest.raises(ValidationError, match="warm-up"):
        BooleanSignal(name="BUY", values=(None, False), warmup_null_count=2)
    with pytest.raises(ValidationError):
        RuntimeDiagnostic(
            code="division_by_zero",
            span=None,
            output="DIF",
            count=0,
            first_index=-1,
        )

    with pytest.raises(ValidationError, match="count"):
        make_signal_series(
            formula=_formula(),
            context=_context((datetime(2024, 6, 30, 16, tzinfo=UTC),)),
            numeric_outputs=(),
            signals=(
                BooleanSignal(name="BUY", values=(False,), warmup_null_count=0),
                BooleanSignal(name="SELL", values=(False,), warmup_null_count=0),
            ),
            runtime_diagnostics=(
                RuntimeDiagnostic(
                    code="division_by_zero",
                    span=None,
                    output=None,
                    count=2,
                    first_index=0,
                ),
            ),
        )


def test_public_limits_reject_work_before_signal_series_construction() -> None:
    assert MAX_OUTPUT_CELLS == MAX_BAR_SERIES_ROWS * MAX_PUBLIC_OUTPUTS == 3_200_000
    too_many_outputs = tuple(
        NumericOutput(name=f"X{index}", values=(1.0,), warmup_null_count=0)
        for index in range(MAX_PUBLIC_OUTPUTS + 1)
    )
    with pytest.raises(ValueError, match="output limit"):
        make_signal_series(
            formula=_formula(),
            context=_context((datetime(2024, 6, 30, 16, tzinfo=UTC),)),
            numeric_outputs=too_many_outputs,
            signals=(
                BooleanSignal(name="BUY", values=(False,), warmup_null_count=0),
                BooleanSignal(name="SELL", values=(False,), warmup_null_count=0),
            ),
        )

    diagnostic = RuntimeDiagnostic(
        code="division_by_zero",
        span=None,
        output=None,
        count=1,
        first_index=0,
    )
    with pytest.raises(ValueError, match="diagnostic limit"):
        make_signal_series(
            formula=_formula(),
            context=_context((datetime(2024, 6, 30, 16, tzinfo=UTC),)),
            numeric_outputs=(),
            signals=(
                BooleanSignal(name="BUY", values=(False,), warmup_null_count=0),
                BooleanSignal(name="SELL", values=(False,), warmup_null_count=0),
            ),
            runtime_diagnostics=(diagnostic,) * (MAX_RUNTIME_DIAGNOSTICS + 1),
        )


def test_factory_has_no_independent_parameter_channel() -> None:
    with pytest.raises(TypeError, match="parameters"):
        make_signal_series(
            formula=_formula(),
            context=_context(parameters={"N": IntegerScalar(12)}),
            parameters=(NormalizedParameter(name="N", kind="integer", value="13"),),
            numeric_outputs=(),
            signals=(
                BooleanSignal(
                    name="BUY", values=(False, False, False), warmup_null_count=0
                ),
                BooleanSignal(
                    name="SELL", values=(False, False, False), warmup_null_count=0
                ),
            ),
        )


def test_context_parameters_are_the_only_serialized_replay_parameters() -> None:
    result = make_signal_series(
        formula=_formula(),
        context=_context(
            parameters={"ALPHA": NumberScalar(-0.0), "N": IntegerScalar(12)}
        ),
        numeric_outputs=(),
        signals=(
            BooleanSignal(
                name="BUY", values=(False, False, False), warmup_null_count=0
            ),
            BooleanSignal(
                name="SELL", values=(False, False, False), warmup_null_count=0
            ),
        ),
    )
    assert tuple((item.name, item.kind, item.value) for item in result.parameters) == (
        ("ALPHA", "number", "0"),
        ("N", "integer", "12"),
    )


def test_number_parameters_use_shortest_exact_float_roundtrip_text() -> None:
    result = make_signal_series(
        formula=_formula(),
        context=_context(
            parameters={
                "PA": NumberScalar(1e308),
                "PB": NumberScalar(1e-200),
                "PC": NumberScalar(5e-324),
                "PD": NumberScalar(1.0),
                "PE": NumberScalar(-0.0),
            }
        ),
        numeric_outputs=(),
        signals=(
            BooleanSignal(
                name="BUY", values=(False, False, False), warmup_null_count=0
            ),
            BooleanSignal(
                name="SELL", values=(False, False, False), warmup_null_count=0
            ),
        ),
    )
    assert tuple(item.value for item in result.parameters) == (
        "1e+308",
        "1e-200",
        "5e-324",
        "1.0",
        "0",
    )
    assert tuple(
        0.0 if item.value == "0" else float(item.value) for item in result.parameters
    ) == (1e308, 1e-200, 5e-324, 1.0, 0.0)


@pytest.mark.parametrize(
    "value",
    ["1E+308", "1e308", "1e+0308", "1.00", "0.0", "-0.0", "01e+2"],
)
def test_number_parameter_rejects_noncanonical_float_text(value: str) -> None:
    with pytest.raises(ValidationError, match="canonical"):
        NormalizedParameter(name="N", kind="number", value=value)


@pytest.mark.parametrize("value", ["1.0", "1e+3", "-0", "01"])
def test_integer_parameter_still_requires_canonical_integer_text(value: str) -> None:
    with pytest.raises(ValidationError, match="integer"):
        NormalizedParameter(name="N", kind="integer", value=value)


def test_model_copy_cannot_bypass_frozen_contract_validation() -> None:
    with pytest.raises(TypeError, match="update"):
        _formula().model_copy(update={"version": 0})


def test_canonical_writer_rejects_parent_and_nested_dict_tampering() -> None:
    parent = _series()
    parent.__dict__["formula_version"] = 0
    with pytest.raises(ValidationError):
        parent.canonical_json_bytes()

    nested = _series()
    nested.numeric_outputs[0].__dict__["name"] = "BUY"
    with pytest.raises(ValidationError):
        nested.canonical_json_bytes()


def test_signal_series_bytes_limit_is_enforced_before_parse_and_after_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert signal_series_module.MAX_SIGNAL_SERIES_BYTES == 128 * 1024 * 1024
    raw = _series().canonical_json_bytes()
    monkeypatch.setattr(signal_series_module, "MAX_SIGNAL_SERIES_BYTES", 8)
    with pytest.raises(ValueError, match="byte limit"):
        SignalSeries.from_canonical_json_bytes(b"{" * 9)
    monkeypatch.setattr(signal_series_module, "MAX_SIGNAL_SERIES_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="byte limit"):
        SignalSeries.from_canonical_json_bytes(raw)
    with pytest.raises(ValueError, match="byte limit"):
        _series().canonical_json_bytes()


def test_diagnostic_input_order_does_not_change_bytes_or_identity() -> None:
    first = RuntimeDiagnostic(
        code="division_by_zero", span=None, output="DEA", count=1, first_index=1
    )
    second = RuntimeDiagnostic(
        code="invalid_math", span=None, output="DIF", count=1, first_index=2
    )

    def build(items: tuple[RuntimeDiagnostic, ...]) -> SignalSeries:
        return make_signal_series(
            formula=_formula(),
            context=_context(),
            numeric_outputs=(
                NumericOutput(name="DIF", values=(1.0, 1.0, 1.0), warmup_null_count=0),
                NumericOutput(name="DEA", values=(1.0, 1.0, 1.0), warmup_null_count=0),
            ),
            signals=(
                BooleanSignal(
                    name="BUY", values=(False, False, False), warmup_null_count=0
                ),
                BooleanSignal(
                    name="SELL", values=(False, False, False), warmup_null_count=0
                ),
            ),
            runtime_diagnostics=items,
        )

    forward = build((first, second))
    reverse = build((second, first))
    assert forward.canonical_json_bytes() == reverse.canonical_json_bytes()
    assert forward.signal_series_id == reverse.signal_series_id

    payload = forward.model_dump(mode="python")
    payload["runtime_diagnostics"] = tuple(reversed(payload["runtime_diagnostics"]))
    with pytest.raises(ValidationError, match="canonical order"):
        SignalSeries.model_validate(payload)


@pytest.mark.parametrize(
    ("period", "timestamps"),
    [
        (Period.DAY, (datetime(2024, 7, 1, 12, tzinfo=UTC),) * 3),
        (Period.WEEK, (datetime(2024, 7, 1, 16, tzinfo=UTC),) * 3),
        (Period.MIN60, (datetime(2024, 7, 1, 3, tzinfo=UTC),) * 3),
    ],
)
def test_signal_series_import_rejects_noncanonical_period_buckets(
    period: Period,
    timestamps: tuple[datetime, ...],
) -> None:
    payload = _series().model_dump(mode="python")
    invalid = tuple(
        value + timedelta(days=index) for index, value in enumerate(timestamps)
    )
    payload.update(
        period=period,
        timestamps=invalid,
        data_cutoff=invalid[-1],
        query_start=invalid[0] - timedelta(days=1),
        query_end=invalid[-1] + timedelta(days=1),
    )
    with pytest.raises(ValidationError, match="bucket"):
        SignalSeries.model_validate(payload)


def test_output_cell_budget_includes_fixed_buy_and_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(signal_series_module, "MAX_OUTPUT_CELLS", 33)
    outputs = tuple(
        NumericOutput(name=f"X{index}", values=(1.0,), warmup_null_count=0)
        for index in range(MAX_PUBLIC_OUTPUTS)
    )
    with pytest.raises(ValueError, match="cell limit"):
        make_signal_series(
            formula=_formula(),
            context=_context((datetime(2024, 6, 30, 16, tzinfo=UTC),)),
            numeric_outputs=outputs,
            signals=(
                BooleanSignal(name="BUY", values=(False,), warmup_null_count=0),
                BooleanSignal(name="SELL", values=(False,), warmup_null_count=0),
            ),
        )


@pytest.mark.parametrize(
    ("argument", "length", "message"),
    [
        ("numeric_outputs", MAX_PUBLIC_OUTPUTS + 1, "output limit"),
        ("signals", 3, "exactly BUY and SELL"),
        ("runtime_diagnostics", MAX_RUNTIME_DIAGNOSTICS + 1, "diagnostic limit"),
    ],
)
def test_factory_checks_sequence_lengths_before_copying(
    argument: str,
    length: int,
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "formula": _formula(),
        "context": _context(),
        "numeric_outputs": (),
        "signals": (
            BooleanSignal(
                name="BUY", values=(False, False, False), warmup_null_count=0
            ),
            BooleanSignal(
                name="SELL", values=(False, False, False), warmup_null_count=0
            ),
        ),
        "runtime_diagnostics": (),
    }
    kwargs[argument] = _ExplodingSequence(length)
    with pytest.raises(ValueError, match=message):
        make_signal_series(**kwargs)  # type: ignore[arg-type]
