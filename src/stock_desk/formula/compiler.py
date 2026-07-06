from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import math
from typing import Literal

from stock_desk.formula.ast import (
    Binary,
    Call,
    Expression,
    Name,
    Number,
    SourceSpan,
    String,
    Unary,
)
from stock_desk.formula.context import MAX_PARAMETERS
from stock_desk.formula.errors import FormulaError, FormulaLimitError
from stock_desk.formula.functions import (
    V1_REGISTRY,
    CompatibilityRegistry,
    accepts_value_kind,
)
from stock_desk.formula.functions.base import FunctionSpec, ValueKind
from stock_desk.formula.functions.base import IDENTIFIER_PATTERN
from stock_desk.formula.parser import MAX_SOURCE_BYTES, parse_formula
from stock_desk.formula.signal_series import ENGINE_VERSION, MAX_PUBLIC_OUTPUTS
from stock_desk.formula.values import IntegerScalar, NumberScalar, ScalarValue


MAX_LOOKBACK = 100_000
type CompiledKind = ValueKind


class FormulaCompileError(FormulaError):
    def __init__(
        self,
        code: str,
        message: str,
        span: SourceSpan,
        *,
        function: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            line=span.line,
            column=span.column,
            end_line=span.end_line,
            end_column=span.end_column,
        )
        self.function = function


@dataclass(frozen=True, slots=True)
class LiteralExpression:
    value: ScalarValue
    kind: CompiledKind
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ReferenceExpression:
    name: str
    source: Literal["field", "parameter", "declaration"]
    kind: CompiledKind
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UnaryExpression:
    operator: str
    operand: CompiledExpression
    kind: CompiledKind
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BinaryExpression:
    operator: str
    left: CompiledExpression
    right: CompiledExpression
    kind: CompiledKind
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class CallExpression:
    function: str
    dispatch_key: str
    arguments: tuple[CompiledExpression, ...]
    kind: CompiledKind
    span: SourceSpan


type CompiledExpression = (
    LiteralExpression
    | ReferenceExpression
    | UnaryExpression
    | BinaryExpression
    | CallExpression
)


@dataclass(frozen=True, slots=True)
class CompiledStatement:
    name: str
    expression: CompiledExpression
    kind: CompiledKind
    visible: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundParameter:
    name: str
    kind: Literal["integer_scalar", "scalar"]
    value: int | float


@dataclass(frozen=True, slots=True)
class CompiledFormula:
    source_checksum: str
    compatibility_version: str
    engine_version: str
    statements: tuple[CompiledStatement, ...]
    numeric_outputs: tuple[str, ...]
    signal_outputs: tuple[str, ...]
    parameter_bindings: tuple[BoundParameter, ...]


def formula_source_checksum(source: str) -> str:
    if type(source) is not str:
        raise TypeError("formula source must be text")
    if len(source) > MAX_SOURCE_BYTES:
        raise ValueError("formula source exceeds source byte limit")
    try:
        payload = source.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("formula source must be valid UTF-8") from error
    if len(payload) > MAX_SOURCE_BYTES:
        raise ValueError("formula source exceeds source byte limit")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _fail(
    code: str,
    message: str,
    expression: Expression | SourceSpan,
    *,
    function: str | None = None,
) -> FormulaCompileError:
    span = expression if isinstance(expression, SourceSpan) else expression.span
    return FormulaCompileError(code, message, span, function=function)


def _literal(number: Decimal, span: SourceSpan) -> LiteralExpression:
    try:
        value = float(number)
    except (OverflowError, ValueError) as error:
        raise FormulaCompileError(
            "invalid_number", "numeric literal is not finite float64", span
        ) from error
    if not math.isfinite(value):
        raise FormulaCompileError(
            "invalid_number", "numeric literal is not finite float64", span
        )
    if number == number.to_integral_value() and abs(number) <= 2**53:
        scalar = IntegerScalar(int(number))
        return LiteralExpression(scalar, "integer_scalar", span)
    return LiteralExpression(NumberScalar(value), "scalar", span)


def _constant(expression: CompiledExpression) -> ScalarValue | None:
    if isinstance(expression, LiteralExpression):
        return expression.value
    return None


def _constraint_value(
    expression: CompiledExpression, parameters: Mapping[str, ScalarValue]
) -> ScalarValue | None:
    value = _constant(expression)
    if value is not None:
        return value
    if isinstance(expression, ReferenceExpression) and expression.source == "parameter":
        return parameters[expression.name]
    if isinstance(expression, UnaryExpression) and expression.operator in {"+", "-"}:
        operand = _constraint_value(expression.operand, parameters)
        if operand is not None:
            raw = operand.value if expression.operator == "+" else -operand.value
            if isinstance(operand, IntegerScalar):
                return IntegerScalar(int(raw))
            return NumberScalar(float(raw))
    return None


def _compile_expression(
    expression: Expression,
    scope: Mapping[str, CompiledKind],
    parameters: Mapping[str, ScalarValue],
    registry: CompatibilityRegistry,
) -> CompiledExpression:
    if isinstance(expression, Number):
        return _literal(expression.value, expression.span)
    if isinstance(expression, String):
        raise _fail(
            "invalid_type",
            "string literals are data only and cannot be evaluated",
            expression,
        )
    if isinstance(expression, Name):
        if expression.identifier in registry.field_names():
            return ReferenceExpression(
                expression.identifier, "field", "number_series", expression.span
            )
        if expression.identifier in parameters:
            value = parameters[expression.identifier]
            kind: CompiledKind = (
                "integer_scalar" if isinstance(value, IntegerScalar) else "scalar"
            )
            return ReferenceExpression(
                expression.identifier, "parameter", kind, expression.span
            )
        if expression.identifier in scope:
            return ReferenceExpression(
                expression.identifier,
                "declaration",
                scope[expression.identifier],
                expression.span,
            )
        raise _fail(
            "unknown_identifier",
            "identifier is not available in source order",
            expression,
        )
    if isinstance(expression, Unary):
        operand = _compile_expression(expression.operand, scope, parameters, registry)
        if expression.operator in {"+", "-"}:
            if operand.kind not in {"scalar", "integer_scalar", "number_series"}:
                raise _fail(
                    "invalid_type",
                    "numeric unary operator requires a number",
                    expression,
                )
            constant = _constant(operand)
            if constant is not None:
                raw = constant.value if expression.operator == "+" else -constant.value
                return _literal(Decimal(str(raw)), expression.span)
            return UnaryExpression(
                expression.operator, operand, operand.kind, expression.span
            )
        if operand.kind not in {
            "scalar",
            "integer_scalar",
            "number_series",
            "boolean_series",
        }:
            raise _fail("invalid_type", "NOT requires a condition", expression)
        return UnaryExpression("NOT", operand, "boolean_series", expression.span)
    if isinstance(expression, Binary):
        left = _compile_expression(expression.left, scope, parameters, registry)
        right = _compile_expression(expression.right, scope, parameters, registry)
        if expression.operator in {"AND", "OR"}:
            return BinaryExpression(
                expression.operator, left, right, "boolean_series", expression.span
            )
        if left.kind not in {
            "scalar",
            "integer_scalar",
            "number_series",
        } or right.kind not in {"scalar", "integer_scalar", "number_series"}:
            raise _fail(
                "invalid_type", "operator requires numeric operands", expression
            )
        if expression.operator in {"=", "==", "<>", "!=", "<", "<=", ">", ">="}:
            return BinaryExpression(
                expression.operator, left, right, "boolean_series", expression.span
            )
        return BinaryExpression(
            expression.operator, left, right, "number_series", expression.span
        )
    if isinstance(expression, Call):
        try:
            spec = registry.function(expression.function)
        except KeyError as error:
            raise _fail(
                "unsupported_function",
                "function is not in the compatibility registry",
                expression,
                function=expression.function,
            ) from error
        arguments = tuple(
            _compile_expression(item, scope, parameters, registry)
            for item in expression.arguments
        )
        if not spec.min_args <= len(arguments) <= spec.max_args:
            raise _fail(
                "invalid_argument_count",
                "function argument count is invalid",
                expression,
            )
        _validate_arguments(spec, arguments, expression, parameters)
        return CallExpression(
            spec.name, spec.dispatch_key, arguments, spec.result_kind, expression.span
        )
    raise TypeError("unsupported AST expression")


def _validate_arguments(
    spec: FunctionSpec,
    arguments: tuple[CompiledExpression, ...],
    call: Call,
    parameters: Mapping[str, ScalarValue],
) -> None:
    constants: dict[str, float] = {}
    for parameter, argument in zip(spec.parameters, arguments, strict=True):
        if not accepts_value_kind(parameter.accepted_kinds, argument.kind):
            raise _fail(
                "invalid_type",
                f"argument {parameter.name} has an invalid type",
                argument.span,
                function=spec.name,
            )
        value = _constraint_value(argument, parameters)
        if parameter.constant and value is None:
            raise _fail(
                "constant_required",
                f"argument {parameter.name} must be constant",
                argument.span,
                function=spec.name,
            )
        if value is not None:
            numeric = float(value.value)
            constants[parameter.name] = numeric
            if parameter.minimum is not None and numeric < parameter.minimum:
                code = (
                    "future_data"
                    if spec.name == "REF" and parameter.name == "N"
                    else "argument_out_of_range"
                )
                raise _fail(
                    code,
                    f"argument {parameter.name} is below its minimum",
                    argument.span,
                    function=spec.name,
                )
            maximum = (
                min(parameter.maximum, MAX_LOOKBACK)
                if parameter.maximum is not None
                else MAX_LOOKBACK
                if parameter.name == "N"
                else None
            )
            if maximum is not None and numeric > maximum:
                raise _fail(
                    "argument_out_of_range",
                    f"argument {parameter.name} exceeds its maximum",
                    argument.span,
                    function=spec.name,
                )
    operations = {
        "<=": lambda a, b: a <= b,
        "<": lambda a, b: a < b,
        ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b,
        "==": lambda a, b: a == b,
    }
    for relation in spec.relations:
        if (
            relation.left in constants
            and relation.right in constants
            and not operations[relation.operator](
                constants[relation.left], constants[relation.right]
            )
        ):
            raise _fail(
                "invalid_argument_relation",
                "function argument relation is invalid",
                call,
                function=spec.name,
            )


def compile_formula(
    source: str,
    *,
    parameters: Mapping[str, ScalarValue] | None = None,
    registry: CompatibilityRegistry = V1_REGISTRY,
) -> CompiledFormula:
    canonical_parameters = _canonical_parameters(
        parameters if parameters is not None else {}, registry
    )
    program = parse_formula(source)
    invalid_statement = next(
        (
            statement
            for statement in program.statements
            if IDENTIFIER_PATTERN.fullmatch(statement.name) is None
        ),
        None,
    )
    if invalid_statement is not None:
        raise FormulaCompileError(
            "invalid_identifier",
            "formula declaration identifier is not canonical",
            invalid_statement.span,
        )
    diagnostics = registry.validate(program, parameter_names=canonical_parameters)
    if diagnostics:
        item = diagnostics[0]
        raise FormulaCompileError(
            item.code,
            item.message,
            SourceSpan(item.line, item.column, item.end_line, item.end_column),
            function=item.function,
        )
    scope: dict[str, CompiledKind] = {}
    statements: list[CompiledStatement] = []
    numeric: list[str] = []
    signals: list[str] = []
    for statement in program.statements:
        expression = _compile_expression(
            statement.expression, scope, canonical_parameters, registry
        )
        if statement.name in {"BUY", "SELL"}:
            if not statement.visible or expression.kind != "boolean_series":
                raise FormulaCompileError(
                    "invalid_signal_output",
                    "BUY and SELL must be visible boolean outputs",
                    statement.span,
                )
            signals.append(statement.name)
        elif statement.visible:
            if expression.kind == "boolean_series":
                raise FormulaCompileError(
                    "invalid_output_type",
                    "public non-signal outputs must be numeric",
                    statement.span,
                )
            numeric.append(statement.name)
            if len(numeric) > MAX_PUBLIC_OUTPUTS:
                raise FormulaCompileError(
                    "output_limit_exceeded",
                    "public numeric output limit exceeded",
                    statement.span,
                )
        compiled = CompiledStatement(
            statement.name,
            expression,
            expression.kind,
            statement.visible,
            statement.span,
        )
        statements.append(compiled)
        scope[statement.name] = expression.kind
    if signals and set(signals) != {"BUY", "SELL"}:
        raise FormulaCompileError(
            "incomplete_signal_pair",
            "trading formulas require both visible BUY and SELL outputs",
            program.span,
        )
    normalized_signals = ("BUY", "SELL") if signals else ()
    bindings = tuple(
        BoundParameter(
            name=name,
            kind=("integer_scalar" if isinstance(value, IntegerScalar) else "scalar"),
            value=value.value,
        )
        for name, value in canonical_parameters.items()
    )
    return CompiledFormula(
        formula_source_checksum(source),
        registry.version,
        ENGINE_VERSION,
        tuple(statements),
        tuple(numeric),
        normalized_signals,
        bindings,
    )


def _canonical_parameters(
    parameters: Mapping[str, ScalarValue], registry: CompatibilityRegistry
) -> dict[str, ScalarValue]:
    if not isinstance(parameters, Mapping):
        raise TypeError("formula parameters must be a mapping")
    if len(parameters) > MAX_PARAMETERS:
        raise FormulaLimitError(limit="parameters", maximum=MAX_PARAMETERS)
    canonical: dict[str, ScalarValue] = {}
    fields = set(registry.field_names())
    for name, value in parameters.items():
        if (
            type(name) is not str
            or IDENTIFIER_PATTERN.fullmatch(name) is None
            or name != name.upper()
            or name in fields
        ):
            raise ValueError("formula parameter name is not canonical")
        if type(value) not in (IntegerScalar, NumberScalar):
            raise TypeError("formula parameter value must be an exact scalar")
        canonical[name] = value
    return dict(sorted(canonical.items()))
