from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Literal

from stock_desk.formula.ast import SourceSpan
from stock_desk.formula.compiler import (
    BinaryExpression,
    BoundParameter,
    CallExpression,
    CompiledExpression,
    CompiledFormula,
    LiteralExpression,
    ReferenceExpression,
    UnaryExpression,
    MAX_LOOKBACK,
)
from stock_desk.formula.functions import V1_REGISTRY, CompatibilityRegistry
from stock_desk.formula.functions import accepts_value_kind
from stock_desk.formula.functions.base import IDENTIFIER_PATTERN
from stock_desk.formula.signal_series import ENGINE_VERSION
from stock_desk.formula.signal_series import MAX_PUBLIC_OUTPUTS
from stock_desk.formula.values import IntegerScalar, NumberScalar
from stock_desk.formula.parser import MAX_AST_NODES, MAX_STATEMENTS
from stock_desk.formula.context import MAX_PARAMETERS


@dataclass(frozen=True, slots=True)
class TemporalDependency:
    min_offset: int | None
    max_offset: int
    has_data: bool = True


@dataclass(frozen=True, slots=True)
class StatementDependency:
    name: str
    dependency: TemporalDependency
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class TemporalDiagnostic:
    code: str
    function: str | None
    span: SourceSpan
    explanation: str
    blocks_preview: bool
    blocks_save: bool
    blocks_backtest: bool


@dataclass(frozen=True, slots=True)
class TemporalAnalysis:
    source_checksum: str
    parameter_bindings: tuple[BoundParameter, ...]
    compatibility_version: str
    engine_version: str
    statements: tuple[StatementDependency, ...]
    diagnostics: tuple[TemporalDiagnostic, ...]
    append_only_stable: bool


@dataclass(frozen=True, slots=True)
class CompiledStructure:
    numeric_outputs: tuple[str, ...]
    signal_outputs: tuple[str, ...]
    expression_count: int
    weighted_work_units: int
    diagnostics: tuple[TemporalDiagnostic, ...]


_CURRENT = TemporalDependency(0, 0)
_CONFIGURATION = TemporalDependency(0, 0, False)
type TemporalRule = Literal[
    "current",
    "ref",
    "bounded_window",
    "zero_unbounded_window",
    "unbounded_recursive",
    "cross",
    "longcross",
]
TEMPORAL_RULES: Mapping[str, TemporalRule] = MappingProxyType(
    {
        "math.abs": "current",
        "math.max": "current",
        "math.min": "current",
        "logic.if": "current",
        "series.ref": "ref",
        "series.ma": "bounded_window",
        "statistics.std": "bounded_window",
        "series.hhv": "zero_unbounded_window",
        "series.llv": "zero_unbounded_window",
        "series.sum": "zero_unbounded_window",
        "series.count": "zero_unbounded_window",
        "series.ema": "unbounded_recursive",
        "series.sma": "unbounded_recursive",
        "signal.barslast": "unbounded_recursive",
        "signal.filter": "unbounded_recursive",
        "signal.cross": "cross",
        "signal.longcross": "longcross",
    }
)
DISPATCH_WORK_WEIGHTS: Mapping[str, int] = MappingProxyType(
    {
        key: (
            32
            if key in {"series.ma", "series.sum", "statistics.std"}
            else 8
            if key
            in {
                "series.ema",
                "series.sma",
                "series.hhv",
                "series.llv",
                "series.count",
            }
            else 4
            if key.startswith("signal.")
            else 2
        )
        for key in TEMPORAL_RULES
    }
)


def _combine(values: tuple[TemporalDependency, ...]) -> TemporalDependency:
    data_values = tuple(value for value in values if value.has_data)
    if not data_values:
        return _CONFIGURATION
    minimum = (
        None
        if any(value.min_offset is None for value in data_values)
        else min(
            value.min_offset for value in data_values if value.min_offset is not None
        )
    )
    return TemporalDependency(
        minimum, max(value.max_offset for value in data_values), True
    )


def _shift(value: TemporalDependency, amount: int) -> TemporalDependency:
    if not value.has_data:
        return value
    minimum = None if value.min_offset is None else value.min_offset + amount
    return TemporalDependency(minimum, value.max_offset + amount)


def _extend_past(value: TemporalDependency, bars: int | None) -> TemporalDependency:
    if not value.has_data:
        return value
    if bars is None or value.min_offset is None:
        minimum = None
    else:
        minimum = value.min_offset - bars
    return TemporalDependency(minimum, value.max_offset)


def _bound_scalars(compiled: CompiledFormula) -> dict[str, int | float]:
    return {item.name: item.value for item in compiled.parameter_bindings}


def _scalar_value(
    expression: CompiledExpression, bindings: dict[str, int | float]
) -> int | float | None:
    if isinstance(expression, LiteralExpression):
        return expression.value.value
    if isinstance(expression, ReferenceExpression) and expression.source == "parameter":
        return bindings.get(expression.name)
    if isinstance(expression, UnaryExpression) and expression.operator in {"+", "-"}:
        value = _scalar_value(expression.operand, bindings)
        if value is not None:
            return value if expression.operator == "+" else -value
    return None


def _unsafe(
    *, code: str, function: str | None, span: SourceSpan, explanation: str
) -> TemporalDiagnostic:
    return TemporalDiagnostic(
        code=code,
        function=function,
        span=span,
        explanation=explanation,
        blocks_preview=True,
        blocks_save=True,
        blocks_backtest=True,
    )


def _call_dependency(
    expression: CallExpression,
    arguments: tuple[TemporalDependency, ...],
    bindings: dict[str, int | float],
) -> TemporalDependency:
    rule = TEMPORAL_RULES.get(expression.dispatch_key)
    if rule == "current":
        return _combine(arguments)
    if rule == "ref":
        offset = _scalar_value(expression.arguments[1], bindings)
        return (
            _shift(arguments[0], -int(offset)) if offset is not None else arguments[0]
        )
    if rule in {"bounded_window", "zero_unbounded_window"}:
        combined = arguments[0]
        window = _scalar_value(expression.arguments[1], bindings)
        if window is None or (rule == "zero_unbounded_window" and int(window) == 0):
            return _extend_past(combined, None)
        return _extend_past(combined, int(window) - 1)
    if rule == "cross":
        return _extend_past(_combine(arguments[:2]), 1)
    if rule == "longcross":
        window = _scalar_value(expression.arguments[2], bindings)
        return _extend_past(
            _combine(arguments[:2]), int(window) if window is not None else None
        )
    if rule == "unbounded_recursive":
        return _extend_past(arguments[0], None)
    return _combine(arguments)


def _expression_dependency(
    expression: CompiledExpression,
    *,
    declarations: dict[str, TemporalDependency],
    bindings: dict[str, int | float],
    registry: CompatibilityRegistry,
) -> tuple[TemporalDependency, tuple[TemporalDiagnostic, ...]]:
    if isinstance(expression, LiteralExpression):
        return _CONFIGURATION, ()
    if isinstance(expression, ReferenceExpression):
        if expression.source == "declaration":
            return declarations[expression.name], ()
        if expression.source == "parameter":
            return _CONFIGURATION, ()
        return _CURRENT, ()
    if isinstance(expression, UnaryExpression):
        return _expression_dependency(
            expression.operand,
            declarations=declarations,
            bindings=bindings,
            registry=registry,
        )
    if isinstance(expression, BinaryExpression):
        left, left_diagnostics = _expression_dependency(
            expression.left,
            declarations=declarations,
            bindings=bindings,
            registry=registry,
        )
        right, right_diagnostics = _expression_dependency(
            expression.right,
            declarations=declarations,
            bindings=bindings,
            registry=registry,
        )
        return _combine((left, right)), left_diagnostics + right_diagnostics
    if isinstance(expression, CallExpression):
        dependencies: list[TemporalDependency] = []
        diagnostics: list[TemporalDiagnostic] = []
        for argument in expression.arguments:
            dependency, nested = _expression_dependency(
                argument,
                declarations=declarations,
                bindings=bindings,
                registry=registry,
            )
            dependencies.append(dependency)
            diagnostics.extend(nested)
        try:
            spec = registry.function(expression.function)
        except KeyError:
            diagnostics.append(
                _unsafe(
                    code="unknown_temporal_rule",
                    function=expression.function,
                    span=expression.span,
                    explanation="Function is absent from the selected analysis registry.",
                )
            )
            return _combine(tuple(dependencies)), tuple(diagnostics)
        behavior = spec.future_behavior
        dependency = _call_dependency(expression, tuple(dependencies), bindings)
        if expression.dispatch_key not in TEMPORAL_RULES:
            diagnostics.append(
                _unsafe(
                    code="unknown_temporal_rule",
                    function=expression.function,
                    span=expression.span,
                    explanation="Function has no reviewed dependency propagation rule.",
                )
            )
        elif behavior == "future":
            dependency = TemporalDependency(
                dependency.min_offset if dependency.has_data else 0,
                max(1, dependency.max_offset),
                True,
            )
            diagnostics.append(
                _unsafe(
                    code="future_data",
                    function=expression.function,
                    span=expression.span,
                    explanation="Function metadata declares a future data dependency.",
                )
            )
        elif behavior == "repainting":
            diagnostics.append(
                _unsafe(
                    code="repainting",
                    function=expression.function,
                    span=expression.span,
                    explanation="Function metadata allows historical values to repaint.",
                )
            )
        elif behavior not in {"current_only", "past_only"}:
            diagnostics.append(
                _unsafe(
                    code="unknown_temporal_behavior",
                    function=expression.function,
                    span=expression.span,
                    explanation="Function temporal behavior is not recognized by this engine.",
                )
            )
        if dependency.max_offset > 0 and not any(
            item.code == "future_data" and item.function == expression.function
            for item in diagnostics
        ):
            diagnostics.append(
                _unsafe(
                    code="future_data",
                    function=expression.function,
                    span=expression.span,
                    explanation="Function dependency reaches a future bar.",
                )
            )
        return dependency, tuple(diagnostics)
    raise TypeError("compiled expression is unsupported by temporal analysis")


def _invalid_ir(
    span: SourceSpan, explanation: str, *, function: str | None = None
) -> TemporalDiagnostic:
    return _unsafe(
        code="invalid_compiled_ir",
        function=function,
        span=span,
        explanation=explanation,
    )


def _inspect_expression(
    expression: CompiledExpression,
    *,
    scope: Mapping[str, str],
    bindings: Mapping[str, BoundParameter],
    registry: CompatibilityRegistry,
) -> tuple[int, tuple[TemporalDiagnostic, ...]]:
    diagnostics: list[TemporalDiagnostic] = []
    count = 1
    expected_kind: str | None = None
    if isinstance(expression, LiteralExpression):
        expected_kind = (
            "integer_scalar"
            if type(expression.value) is IntegerScalar
            else "scalar"
            if type(expression.value) is NumberScalar
            else None
        )
        if expression.kind != expected_kind:
            diagnostics.append(_invalid_ir(expression.span, "Literal kind is invalid."))
    elif isinstance(expression, ReferenceExpression):
        if expression.source == "field":
            try:
                expected_kind = registry.field(expression.name).value_type
            except KeyError:
                pass
        elif expression.source == "parameter":
            binding = bindings.get(expression.name)
            expected_kind = binding.kind if binding is not None else None
        elif expression.source == "declaration":
            expected_kind = scope.get(expression.name)
        if expected_kind is None or expression.kind != expected_kind:
            diagnostics.append(
                _invalid_ir(expression.span, "Reference identity or kind is invalid.")
            )
    elif isinstance(expression, UnaryExpression):
        nested_count, nested = _inspect_expression(
            expression.operand,
            scope=scope,
            bindings=bindings,
            registry=registry,
        )
        count += nested_count
        diagnostics.extend(nested)
        expected_kind = (
            "boolean_series"
            if expression.operator == "NOT"
            else expression.operand.kind
            if expression.operator in {"+", "-"}
            else None
        )
        if expression.kind != expected_kind:
            diagnostics.append(
                _invalid_ir(expression.span, "Unary operator IR is invalid.")
            )
    elif isinstance(expression, BinaryExpression):
        for child in (expression.left, expression.right):
            nested_count, nested = _inspect_expression(
                child,
                scope=scope,
                bindings=bindings,
                registry=registry,
            )
            count += nested_count
            diagnostics.extend(nested)
        if expression.operator in {
            "AND",
            "OR",
            "=",
            "==",
            "<>",
            "!=",
            "<",
            "<=",
            ">",
            ">=",
        }:
            expected_kind = "boolean_series"
        elif expression.operator in {"+", "-", "*", "/", "%"}:
            expected_kind = "number_series"
        else:
            expected_kind = None
        if expression.kind != expected_kind:
            diagnostics.append(
                _invalid_ir(expression.span, "Binary operator IR is invalid.")
            )
    elif isinstance(expression, CallExpression):
        for child in expression.arguments:
            nested_count, nested = _inspect_expression(
                child,
                scope=scope,
                bindings=bindings,
                registry=registry,
            )
            count += nested_count
            diagnostics.extend(nested)
        try:
            spec = registry.function(expression.function)
        except KeyError:
            diagnostics.append(
                _invalid_ir(
                    expression.span,
                    "Call function is absent from the registry.",
                    function=expression.function,
                )
            )
        else:
            identity_valid = (
                spec.dispatch_key == expression.dispatch_key
                and spec.result_kind == expression.kind
                and spec.min_args <= len(expression.arguments) <= spec.max_args
            )
            if not identity_valid:
                diagnostics.append(
                    _invalid_ir(
                        expression.span,
                        "Call function, dispatch, result, or arity identity is invalid.",
                        function=expression.function,
                    )
                )
            if len(expression.arguments) == len(spec.parameters):
                constants: dict[str, float] = {}
                raw_bindings = {name: value.value for name, value in bindings.items()}
                for parameter, argument in zip(
                    spec.parameters, expression.arguments, strict=True
                ):
                    if not accepts_value_kind(parameter.accepted_kinds, argument.kind):
                        diagnostics.append(
                            _invalid_ir(
                                argument.span,
                                "Call argument kind is invalid.",
                                function=expression.function,
                            )
                        )
                    scalar = _scalar_value(argument, raw_bindings)
                    if parameter.constant and scalar is None:
                        diagnostics.append(
                            _invalid_ir(
                                argument.span,
                                "Call constant argument is not bound.",
                                function=expression.function,
                            )
                        )
                    if scalar is not None:
                        numeric = float(scalar)
                        constants[parameter.name] = numeric
                        maximum = (
                            min(parameter.maximum, MAX_LOOKBACK)
                            if parameter.maximum is not None
                            else MAX_LOOKBACK
                            if parameter.name == "N"
                            else None
                        )
                        if (
                            parameter.minimum is not None
                            and numeric < parameter.minimum
                        ) or (maximum is not None and numeric > maximum):
                            diagnostics.append(
                                _invalid_ir(
                                    argument.span,
                                    "Call scalar constraint is invalid.",
                                    function=expression.function,
                                )
                            )
                relation_checks = {
                    "<=": lambda left, right: left <= right,
                    "<": lambda left, right: left < right,
                    ">=": lambda left, right: left >= right,
                    ">": lambda left, right: left > right,
                    "==": lambda left, right: left == right,
                }
                for relation in spec.relations:
                    if (
                        relation.left not in constants
                        or relation.right not in constants
                        or not relation_checks[relation.operator](
                            constants[relation.left], constants[relation.right]
                        )
                    ):
                        diagnostics.append(
                            _invalid_ir(
                                expression.span,
                                "Call scalar relation is invalid.",
                                function=expression.function,
                            )
                        )
    else:
        diagnostics.append(
            _invalid_ir(
                SourceSpan(1, 1, 1, 1), "Compiled expression variant is invalid."
            )
        )
    return count, tuple(diagnostics)


def _weighted_work(compiled: CompiledFormula) -> int:
    cost = 0
    pending = [statement.expression for statement in compiled.statements]
    while pending:
        expression = pending.pop()
        cost += (
            DISPATCH_WORK_WEIGHTS.get(expression.dispatch_key, 32)
            if isinstance(expression, CallExpression)
            else 1
        )
        if isinstance(expression, UnaryExpression):
            pending.append(expression.operand)
        elif isinstance(expression, BinaryExpression):
            pending.extend((expression.left, expression.right))
        elif isinstance(expression, CallExpression):
            pending.extend(expression.arguments)
    return cost


def inspect_compiled_structure(
    compiled: CompiledFormula,
    *,
    registry: CompatibilityRegistry = V1_REGISTRY,
) -> CompiledStructure:
    scope: dict[str, str] = {}
    diagnostics: list[TemporalDiagnostic] = []
    raw_bindings = compiled.parameter_bindings
    bindings: dict[str, BoundParameter] = {}
    if type(raw_bindings) is not tuple:
        diagnostics.append(
            _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled bindings are not a tuple.")
        )
        raw_bindings = ()
    for item in raw_bindings:
        if type(item) is not BoundParameter:
            diagnostics.append(
                _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled binding type is invalid.")
            )
            continue
        valid_value = (
            item.kind == "integer_scalar"
            and type(item.value) is int
            and abs(item.value) <= 2**53
        ) or (
            item.kind == "scalar"
            and type(item.value) is float
            and math.isfinite(item.value)
        )
        if (
            IDENTIFIER_PATTERN.fullmatch(item.name) is None
            or item.name in bindings
            or item.name in registry.field_names()
            or not valid_value
        ):
            diagnostics.append(
                _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled binding is invalid.")
            )
            continue
        bindings[item.name] = item
    if tuple(bindings) != tuple(sorted(bindings)):
        diagnostics.append(
            _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled bindings are not sorted.")
        )
    numeric: list[str] = []
    signals: list[str] = []
    count = 0
    for statement in compiled.statements:
        try:
            expression_count, nested = _inspect_expression(
                statement.expression,
                scope=scope,
                bindings=bindings,
                registry=registry,
            )
        except RecursionError:
            expression_count = MAX_AST_NODES + 1
            nested = (
                _invalid_ir(
                    statement.span,
                    "Compiled expression nesting exceeds the analysis limit.",
                ),
            )
        count += expression_count
        diagnostics.extend(nested)
        if statement.name in scope or statement.kind != statement.expression.kind:
            diagnostics.append(
                _invalid_ir(statement.span, "Statement identity or kind is invalid.")
            )
        if statement.name in {"BUY", "SELL"}:
            if not statement.visible or statement.kind != "boolean_series":
                diagnostics.append(
                    _invalid_ir(statement.span, "Signal statement IR is invalid.")
                )
            signals.append(statement.name)
        elif statement.visible:
            if statement.kind == "boolean_series":
                diagnostics.append(
                    _invalid_ir(
                        statement.span, "Visible numeric output kind is invalid."
                    )
                )
            numeric.append(statement.name)
        scope[statement.name] = statement.kind
    normalized_signals = ("BUY", "SELL") if signals else ()
    if signals and set(signals) != {"BUY", "SELL"}:
        diagnostics.append(
            _invalid_ir(
                compiled.statements[-1].span,
                "Compiled signal outputs are not a complete pair.",
            )
        )
    if (
        tuple(numeric) != compiled.numeric_outputs
        or normalized_signals != compiled.signal_outputs
    ):
        diagnostics.append(
            _invalid_ir(
                compiled.statements[0].span
                if compiled.statements
                else SourceSpan(1, 1, 1, 1),
                "Compiled output summaries do not match visible statements.",
            )
        )
    if len(compiled.statements) > MAX_STATEMENTS:
        diagnostics.append(
            _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled statement limit is exceeded.")
        )
    if count > MAX_AST_NODES:
        diagnostics.append(
            _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled node limit is exceeded.")
        )
    if len(numeric) > MAX_PUBLIC_OUTPUTS:
        diagnostics.append(
            _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled output limit is exceeded.")
        )
    if len(bindings) > MAX_PARAMETERS:
        diagnostics.append(
            _invalid_ir(SourceSpan(1, 1, 1, 1), "Compiled parameter limit is exceeded.")
        )
    canonical = tuple(
        sorted(
            diagnostics,
            key=lambda item: (
                item.span.line,
                item.span.column,
                item.code,
                item.function or "",
                item.explanation,
            ),
        )
    )
    return CompiledStructure(
        tuple(numeric),
        normalized_signals,
        count,
        _weighted_work(compiled),
        canonical,
    )


def compiled_expression_count(compiled: CompiledFormula) -> int:
    return inspect_compiled_structure(compiled).expression_count


def analyze_compiled_formula(
    compiled: CompiledFormula,
    *,
    registry: CompatibilityRegistry = V1_REGISTRY,
) -> TemporalAnalysis:
    declarations: dict[str, TemporalDependency] = {}
    statements: list[StatementDependency] = []
    diagnostics: list[TemporalDiagnostic] = []
    identity_span = (
        compiled.statements[0].span if compiled.statements else SourceSpan(1, 1, 1, 1)
    )
    if compiled.compatibility_version != registry.version:
        diagnostics.append(
            _unsafe(
                code="registry_version_mismatch",
                function=None,
                span=identity_span,
                explanation="Compiled compatibility version does not match the analysis registry.",
            )
        )
    if compiled.engine_version != ENGINE_VERSION:
        diagnostics.append(
            _unsafe(
                code="engine_version_mismatch",
                function=None,
                span=identity_span,
                explanation="Compiled engine version does not match the analysis engine.",
            )
        )
    if diagnostics:
        return TemporalAnalysis(
            source_checksum=compiled.source_checksum,
            parameter_bindings=compiled.parameter_bindings,
            compatibility_version=compiled.compatibility_version,
            engine_version=compiled.engine_version,
            statements=(),
            diagnostics=tuple(diagnostics),
            append_only_stable=False,
        )
    structure = inspect_compiled_structure(compiled, registry=registry)
    if structure.diagnostics:
        return TemporalAnalysis(
            source_checksum=compiled.source_checksum,
            parameter_bindings=compiled.parameter_bindings,
            compatibility_version=compiled.compatibility_version,
            engine_version=compiled.engine_version,
            statements=(),
            diagnostics=structure.diagnostics,
            append_only_stable=False,
        )
    bindings = _bound_scalars(compiled)
    for statement in compiled.statements:
        dependency, nested = _expression_dependency(
            statement.expression,
            declarations=declarations,
            bindings=bindings,
            registry=registry,
        )
        declarations[statement.name] = dependency
        statements.append(
            StatementDependency(statement.name, dependency, statement.span)
        )
        diagnostics.extend(nested)
        if dependency.min_offset is not None and dependency.min_offset < -MAX_LOOKBACK:
            diagnostics.append(
                _unsafe(
                    code="lookback_limit_exceeded",
                    function=None,
                    span=statement.span,
                    explanation="Cumulative bounded lookback exceeds the public limit.",
                )
            )
    canonical_diagnostics = tuple(
        sorted(
            {
                (
                    item.code,
                    item.function,
                    item.span,
                    item.explanation,
                    item.blocks_preview,
                    item.blocks_save,
                    item.blocks_backtest,
                ): item
                for item in diagnostics
            }.values(),
            key=lambda item: (
                item.span.line,
                item.span.column,
                item.code,
                item.function or "",
            ),
        )
    )
    stable = not canonical_diagnostics and all(
        item.dependency.max_offset <= 0 for item in statements
    )
    return TemporalAnalysis(
        source_checksum=compiled.source_checksum,
        parameter_bindings=compiled.parameter_bindings,
        compatibility_version=compiled.compatibility_version,
        engine_version=compiled.engine_version,
        statements=tuple(statements),
        diagnostics=canonical_diagnostics,
        append_only_stable=stable,
    )
