from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from stock_desk.formula.compiler import (
    BinaryExpression,
    CallExpression,
    CompiledExpression,
    CompiledFormula,
    LiteralExpression,
    ReferenceExpression,
    UnaryExpression,
    compile_formula,
)
from stock_desk.formula.context import EvaluationContext
from stock_desk.formula.ast import SourceSpan
from stock_desk.formula.runtime.base import RuntimeIssue
from stock_desk.formula.runtime.dispatch import execute_kernel
from stock_desk.formula.runtime.elementwise import (
    binary_number,
    boolean,
    compare,
    number_series,
    unary_number,
)
from stock_desk.formula.signal_series import (
    COMPATIBILITY_VERSION,
    ENGINE_VERSION,
    MAX_RUNTIME_DIAGNOSTICS,
    MAX_OUTPUT_CELLS,
    BooleanSignal,
    DiagnosticSpan,
    FormulaReference,
    NumericOutput,
    RuntimeDiagnostic,
    SignalSeries,
    make_signal_series,
)
from stock_desk.formula.values import (
    BooleanSeries,
    IntegerScalar,
    NumberScalar,
    ScalarValue,
    SeriesValue,
)


type RuntimeValue = ScalarValue | SeriesValue


@dataclass(frozen=True, slots=True)
class _LocatedIssue:
    issue: RuntimeIssue
    span: SourceSpan


def _warmup(values: tuple[object | None, ...]) -> int:
    count = 0
    for item in values:
        if item is not None:
            return count
        count += 1
    return count


def _evaluate_expression(
    expression: CompiledExpression,
    *,
    context: EvaluationContext,
    declarations: dict[str, RuntimeValue],
) -> tuple[RuntimeValue, tuple[_LocatedIssue, ...]]:
    rows = len(context.timestamps)
    if isinstance(expression, LiteralExpression):
        return expression.value, ()
    if isinstance(expression, ReferenceExpression):
        if expression.source == "field":
            return context.field(expression.name), ()
        if expression.source == "parameter":
            return context.parameter(expression.name), ()
        return declarations[expression.name], ()
    if isinstance(expression, UnaryExpression):
        operand, nested = _evaluate_expression(
            expression.operand, context=context, declarations=declarations
        )
        if expression.operator in {"+", "-"} and isinstance(
            operand, (NumberScalar, IntegerScalar)
        ):
            raw = operand.value if expression.operator == "+" else -operand.value
            scalar: ScalarValue = (
                IntegerScalar(int(raw))
                if isinstance(operand, IntegerScalar)
                else NumberScalar(float(raw))
            )
            return scalar, nested
        result = (
            boolean("NOT", operand, None, rows)
            if expression.operator == "NOT"
            else unary_number(expression.operator, operand, rows)
        )
        return result.value, nested + tuple(
            _LocatedIssue(item, expression.span) for item in result.issues
        )
    if isinstance(expression, BinaryExpression):
        left, left_issues = _evaluate_expression(
            expression.left, context=context, declarations=declarations
        )
        right, right_issues = _evaluate_expression(
            expression.right, context=context, declarations=declarations
        )
        if expression.operator in {"AND", "OR"}:
            result = boolean(expression.operator, left, right, rows)
        elif expression.operator in {"=", "==", "<>", "!=", "<", "<=", ">", ">="}:
            result = compare(expression.operator, left, right, rows)
        else:
            result = binary_number(expression.operator, left, right, rows)
        return result.value, left_issues + right_issues + tuple(
            _LocatedIssue(item, expression.span) for item in result.issues
        )
    if isinstance(expression, CallExpression):
        values: list[object] = []
        issues: list[_LocatedIssue] = []
        for argument in expression.arguments:
            value, nested = _evaluate_expression(
                argument, context=context, declarations=declarations
            )
            values.append(value)
            issues.extend(nested)
        result = execute_kernel(expression.dispatch_key, tuple(values), rows)
        issues.extend(_LocatedIssue(item, expression.span) for item in result.issues)
        return result.value, tuple(issues)
    raise TypeError("compiled expression is unsupported")


def _diagnostics(
    issues: list[tuple[_LocatedIssue, str | None]],
) -> tuple[RuntimeDiagnostic, ...]:
    aggregated: dict[tuple[str, SourceSpan, str | None], RuntimeIssue] = {}
    for located, output in issues:
        key = (located.issue.code, located.span, output)
        current = aggregated.get(key)
        if current is None:
            aggregated[key] = located.issue
        else:
            aggregated[key] = RuntimeIssue(
                current.code,
                current.count + located.issue.count,
                min(current.first_index, located.issue.first_index),
            )
    values: list[RuntimeDiagnostic] = []
    for (code, raw_span, output), issue in aggregated.items():
        span = raw_span
        values.append(
            RuntimeDiagnostic(
                code=code,
                span=DiagnosticSpan(
                    line=span.line,
                    column=span.column,
                    end_line=span.end_line,
                    end_column=span.end_column,
                ),
                output=output,
                count=issue.count,
                first_index=issue.first_index,
            )
        )
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.code,
                item.output or "",
                item.span.line if item.span else 0,
                item.span.column if item.span else 0,
            ),
        )[:MAX_RUNTIME_DIAGNOSTICS]
    )


class FormulaEvaluator:
    def evaluate(
        self, source: str, context: EvaluationContext, formula: FormulaReference
    ) -> SignalSeries:
        compiled = compile_formula(source, parameters=context.parameters)
        checksum = compiled.source_checksum
        if formula.checksum != checksum:
            raise ValueError("formula checksum does not match source")
        return self.evaluate_compiled(compiled, context, formula)

    def evaluate_compiled(
        self,
        compiled: CompiledFormula,
        context: EvaluationContext,
        formula: FormulaReference,
    ) -> SignalSeries:
        if formula.checksum != compiled.source_checksum:
            raise ValueError("formula checksum does not match compiled source")
        if (
            compiled.compatibility_version != COMPATIBILITY_VERSION
            or compiled.engine_version != ENGINE_VERSION
        ):
            raise ValueError("compiled formula version is incompatible")
        context_bindings = tuple(
            (
                name,
                "integer_scalar" if isinstance(value, IntegerScalar) else "scalar",
                value.value,
            )
            for name, value in context.parameters.items()
        )
        compiled_bindings = tuple(
            (item.name, item.kind, item.value) for item in compiled.parameter_bindings
        )
        if context_bindings != compiled_bindings:
            raise ValueError(
                "context parameter binding does not match compiled formula"
            )
        declarations: dict[str, RuntimeValue] = {}
        numeric: list[NumericOutput] = []
        signal_values: dict[str, BooleanSeries] = {}
        issues: list[tuple[_LocatedIssue, str | None]] = []
        rows = len(context.timestamps)
        if rows * (len(compiled.numeric_outputs) + 2) > MAX_OUTPUT_CELLS:
            raise ValueError("formula public output cell limit exceeded")
        for statement in compiled.statements:
            value, located = _evaluate_expression(
                statement.expression, context=context, declarations=declarations
            )
            declarations[statement.name] = value
            output = statement.name if statement.visible else None
            issues.extend((item, output) for item in located)
            if statement.name in {"BUY", "SELL"}:
                if not isinstance(value, BooleanSeries):
                    raise TypeError("compiled signal output is not boolean")
                signal_values[statement.name] = value
            elif statement.visible:
                numeric_series = number_series(value, rows)
                optional = numeric_series.to_optional_tuple()
                numeric.append(
                    NumericOutput(
                        name=statement.name,
                        values=optional,
                        warmup_null_count=_warmup(optional),
                    )
                )
        signals = []
        for name in ("BUY", "SELL"):
            signal_series = signal_values.get(name)
            if signal_series is None:
                signal_series = BooleanSeries(
                    np.zeros(rows, dtype=np.bool_), np.ones(rows, dtype=np.bool_)
                )
            optional = signal_series.to_optional_tuple()
            signals.append(
                BooleanSignal(
                    name=name, values=optional, warmup_null_count=_warmup(optional)
                )
            )
        return make_signal_series(
            formula=formula,
            context=context,
            numeric_outputs=numeric,
            signals=signals,
            runtime_diagnostics=_diagnostics(issues),
        )
