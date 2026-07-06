from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from stock_desk.formula.ast import SourceSpan
from stock_desk.formula.compiler import (
    CallExpression,
    LiteralExpression,
    compile_formula,
    formula_source_checksum,
)
from stock_desk.formula.context import EvaluationContext
from stock_desk.formula.evaluator import FormulaEvaluator
from stock_desk.formula.functions import V1_REGISTRY
from stock_desk.formula.errors import FormulaSyntaxError
from stock_desk.formula.signal_series import FormulaReference
from stock_desk.formula.values import IntegerScalar, NumberSeries, ScalarValue
from stock_desk.market.types import Adjustment, Period, ProviderId


MACD = "DIF:EMA(CLOSE,12)-EMA(CLOSE,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/formulas/macd.json"
UTC = timezone.utc
DIGEST = "sha256:" + "a" * 64


def context(
    prices: tuple[float, ...],
    period: Period = Period.DAY,
    *,
    parameters: dict[str, ScalarValue] | None = None,
    timestamps: tuple[datetime, ...] | None = None,
) -> EvaluationContext:
    if period is Period.MIN60:
        start = datetime(2024, 1, 2, 1, 30, tzinfo=UTC)
        session_offsets = (
            timedelta(),
            timedelta(hours=1),
            timedelta(hours=3, minutes=30),
        )
        points = tuple(start + session_offsets[index] for index in range(len(prices)))
    elif period is Period.WEEK:
        start = datetime(2023, 12, 31, 16, tzinfo=UTC)
        points = tuple(start + timedelta(weeks=index) for index in range(len(prices)))
    else:
        start = datetime(2023, 12, 31, 16, tzinfo=UTC)
        points = tuple(start + timedelta(days=index) for index in range(len(prices)))
    if timestamps is not None:
        points = timestamps
    source = {
        "OPEN": prices,
        "HIGH": tuple(value + 1 for value in prices),
        "LOW": tuple(value - 1 for value in prices),
        "CLOSE": prices,
        "VOLUME": tuple(10000.0 for _ in prices),
    }
    fields = {}
    for spec in V1_REGISTRY.fields():
        scale = spec.scale_numerator / spec.scale_denominator
        fields[spec.name] = NumberSeries.from_optional(
            tuple(float(value * scale) for value in source[spec.source_name])
        )
    return EvaluationContext(
        symbol="600000.SH",
        period=period,
        adjustment=Adjustment.QFQ,
        source=ProviderId.TUSHARE,
        dataset_version=DIGEST,
        route_version=DIGEST,
        manifest_record_id=DIGEST,
        data_cutoff=points[-1],
        query_start=points[0] - timedelta(days=1),
        query_end=points[-1] + timedelta(days=8),
        timestamps=points,
        fields=fields,
        parameters=parameters or {},
    )


def reference(source: str = MACD) -> FormulaReference:
    return FormulaReference(
        formula_id="macd",
        formula_version_id="macd-v1",
        version=1,
        checksum=formula_source_checksum(source),
    )


def test_macd_matches_independent_decimal_golden() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prices = tuple(float(value) for value in fixture["input"]["close"])

    timestamps = tuple(
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        for value in fixture["input"]["timestamps"]
    )
    result = FormulaEvaluator().evaluate(
        MACD, context(prices, timestamps=timestamps), reference()
    )
    outputs = {item.name: item.values for item in result.numeric_outputs}
    signals = {item.name: item.values for item in result.signals}

    for name in ("DIF", "DEA", "MACD"):
        assert tuple(
            round(value or 0.0, 12) for value in outputs[name]
        ) == pytest.approx(tuple(fixture["expected"][name]), rel=0.0, abs=1e-12)
    assert signals == {
        "BUY": tuple(fixture["expected"]["BUY"]),
        "SELL": tuple(fixture["expected"]["SELL"]),
    }
    assert sum(signals["BUY"]) >= 2
    assert sum(signals["SELL"]) >= 2
    assert result.timestamps == timestamps


def test_macd_fixture_generator_has_no_drift() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(FIXTURE.with_name("generate_macd.py")), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_short_macd_initializes_at_zero_without_first_bar_cross() -> None:
    result = FormulaEvaluator().evaluate(MACD, context((10.0,)), reference())
    assert [item.values for item in result.numeric_outputs] == [(0.0,), (0.0,), (0.0,)]
    assert [item.values for item in result.signals] == [(False,), (False,)]


def test_evaluation_is_byte_deterministic_and_checksum_bound() -> None:
    evaluator = FormulaEvaluator()
    ctx = context((1.0, 2.0, 3.0))
    first = evaluator.evaluate("X:CLOSE/0;", ctx, reference("X:CLOSE/0;"))
    second = evaluator.evaluate("X:CLOSE/0;", ctx, reference("X:CLOSE/0;"))

    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    assert first.numeric_outputs[0].values == (None, None, None)
    assert [
        (item.code, item.count, item.first_index) for item in first.runtime_diagnostics
    ] == [("division_by_zero", 3, 0)]
    with pytest.raises(ValueError, match="checksum"):
        evaluator.evaluate("X:CLOSE;", ctx, reference("X:CLOSE+1;"))
    with pytest.raises(FormulaSyntaxError):
        evaluator.evaluate("X:;", ctx, reference("X:CLOSE+1;"))

    compiled = compile_formula("X:C;")
    with pytest.raises(ValueError, match="version"):
        evaluator.evaluate_compiled(
            replace(compiled, engine_version="formula-engine-v0"),
            ctx,
            reference("X:C;"),
        )


@pytest.mark.parametrize("replacement", [-1, 2, 100_001])
def test_compiled_formula_rejects_parameter_substitution(replacement: int) -> None:
    source = "X:REF(C,N);"
    compiled = compile_formula(source, parameters={"N": IntegerScalar(1)})
    changed = context((1.0, 2.0, 3.0), parameters={"N": IntegerScalar(replacement)})
    with pytest.raises(ValueError, match="parameter binding"):
        FormulaEvaluator().evaluate_compiled(compiled, changed, reference(source))


@pytest.mark.parametrize(
    ("source", "code"),
    [("X:1/0;", "division_by_zero"), ("X:1e308*1e308;", "numeric_overflow")],
)
def test_constant_invalid_math_uses_runtime_null_diagnostics(
    source: str, code: str
) -> None:
    result = FormulaEvaluator().evaluate(source, context((1.0, 2.0)), reference(source))
    assert result.numeric_outputs[0].values == (None, None)
    assert result.runtime_diagnostics[0].code == code


def test_evaluator_checks_output_cell_budget_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stock_desk.formula.evaluator as module

    source = "X:C;"
    monkeypatch.setattr(module, "MAX_OUTPUT_CELLS", 2)
    monkeypatch.setattr(
        module,
        "execute_kernel",
        lambda *_args, **_kwargs: pytest.fail("kernel must not execute"),
    )
    with pytest.raises(ValueError, match="cell limit"):
        FormulaEvaluator().evaluate_compiled(
            compile_formula(source), context((1.0, 2.0)), reference(source)
        )


@pytest.mark.parametrize("period", [Period.DAY, Period.WEEK, Period.MIN60])
def test_evaluator_preserves_context_alignment_without_resampling(
    period: Period,
) -> None:
    ctx = context((1.0, 2.0, 3.0), period)
    result = FormulaEvaluator().evaluate("X:C;", ctx, reference("X:C;"))
    assert result.timestamps == ctx.timestamps
    assert result.period is period
    assert result.numeric_outputs[0].values == (1.0, 2.0, 3.0)


def test_missing_signals_are_filled_false_and_hidden_values_are_not_exposed() -> None:
    source = "TMP:=CLOSE+1;X:TMP*2;"
    result = FormulaEvaluator().evaluate(source, context((1.0, 2.0)), reference(source))
    assert tuple(item.name for item in result.numeric_outputs) == ("X",)
    assert tuple(item.name for item in result.signals) == ("BUY", "SELL")
    assert all(item.values == (False, False) for item in result.signals)


def test_reverse_source_signal_order_still_serializes_buy_then_sell() -> None:
    source = "SELL:C<2;BUY:C>2;"
    result = FormulaEvaluator().evaluate(
        source, context((1.0, 2.0, 3.0)), reference(source)
    )
    assert tuple(item.name for item in result.signals) == ("BUY", "SELL")
    assert result.signals[0].values == (False, False, True)
    assert result.signals[1].values == (True, False, False)


def test_evaluator_cannot_bypass_temporal_lookback_validation() -> None:
    source = "X:MA(MA(C,100000),3);"
    with pytest.raises(ValueError, match="temporal validation"):
        FormulaEvaluator().evaluate(source, context((1.0, 2.0, 3.0)), reference(source))


def test_evaluator_cannot_bypass_a_forged_future_ref() -> None:
    source = "X:REF(C,1);"
    compiled = compile_formula(source)
    statement = compiled.statements[0]
    assert isinstance(statement.expression, CallExpression)
    forged = replace(
        compiled,
        statements=(
            replace(
                statement,
                expression=replace(
                    statement.expression,
                    arguments=(
                        statement.expression.arguments[0],
                        LiteralExpression(
                            IntegerScalar(-1),
                            "integer_scalar",
                            SourceSpan(1, 9, 1, 11),
                        ),
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="invalid_compiled_ir"):
        FormulaEvaluator().evaluate_compiled(
            forged, context((1.0, 2.0, 3.0)), reference(source)
        )


def test_evaluator_rejects_forged_call_identity_before_dispatch() -> None:
    source = "R:=REF(C,100000);X:MA(R,3);"
    compiled = compile_formula(source)
    outer = compiled.statements[1]
    assert isinstance(outer.expression, CallExpression)
    forged = replace(
        compiled,
        statements=(
            compiled.statements[0],
            replace(outer, expression=replace(outer.expression, function="ABS")),
        ),
    )
    with pytest.raises(ValueError, match="invalid_compiled_ir"):
        FormulaEvaluator().evaluate_compiled(
            forged, context((1.0, 2.0, 3.0)), reference(source)
        )


def test_operators_numeric_truthiness_and_field_aliases_share_one_runtime() -> None:
    source = (
        "ABOVE:=C>1;EQ:=C=2;"
        "X:(O+H+L+C)*2/2%100+VOL+VOLUME;"
        "BUY:ABOVE AND NOT EQ;SELL:(C<0) OR (C>=3);"
    )
    result = FormulaEvaluator().evaluate(
        source, context((1.0, 2.0, 3.0)), reference(source)
    )
    assert result.numeric_outputs[0].values == (10104.0, 10108.0, 10112.0)
    assert result.signals[0].values == (False, False, True)
    assert result.signals[1].values == (False, False, True)
