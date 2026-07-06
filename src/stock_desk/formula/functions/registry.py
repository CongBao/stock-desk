from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from stock_desk.formula.ast import Binary, Call, Expression, Name, Program, Unary
from stock_desk.formula.functions.base import (
    DiagnosticCode,
    FieldSpec,
    FormulaDiagnostic,
    FunctionSpec,
    IDENTIFIER_PATTERN,
)
from stock_desk.formula.functions.series import SERIES_FUNCTIONS
from stock_desk.formula.functions.signals import SIGNAL_FUNCTIONS
from stock_desk.formula.functions.statistics import STATISTICS_FUNCTIONS


COMPATIBILITY_VERSION = "tdx-v1"


@dataclass(frozen=True, slots=True, init=False)
class CompatibilityRegistry:
    version: str
    _functions: Mapping[str, FunctionSpec] = field(repr=False)
    _fields: Mapping[str, FieldSpec] = field(repr=False)

    def __init__(
        self,
        *,
        version: str,
        functions: Iterable[FunctionSpec],
        fields: Iterable[FieldSpec],
    ) -> None:
        if not version.strip():
            raise ValueError("compatibility registry version is required")
        function_specs = tuple(functions)
        field_specs = tuple(fields)
        function_map = {spec.name: spec for spec in function_specs}
        field_map = {spec.name: spec for spec in field_specs}
        if not function_map or not field_map:
            raise ValueError("compatibility registry cannot be empty")
        if len(function_map) != len(function_specs):
            raise ValueError("function names must be unique")
        if len(field_map) != len(field_specs):
            raise ValueError("field names must be unique")
        dispatch_keys = {spec.dispatch_key for spec in function_specs}
        if len(dispatch_keys) != len(function_specs):
            raise ValueError("function dispatch keys must be unique")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "_functions", MappingProxyType(function_map))
        object.__setattr__(self, "_fields", MappingProxyType(field_map))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._functions))

    def field_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._fields))

    def function(self, name: str) -> FunctionSpec:
        return self._functions[name.upper()]

    def field(self, name: str) -> FieldSpec:
        return self._fields[name.upper()]

    def functions(self) -> tuple[FunctionSpec, ...]:
        return tuple(self._functions[name] for name in self.names())

    def fields(self) -> tuple[FieldSpec, ...]:
        return tuple(self._fields[name] for name in self.field_names())

    def validate(
        self,
        program: Program,
        *,
        parameter_names: Iterable[str] = (),
    ) -> tuple[FormulaDiagnostic, ...]:
        parameters = {name.upper() for name in parameter_names}
        if any(IDENTIFIER_PATTERN.fullmatch(name) is None for name in parameters):
            raise ValueError(
                "parameter names must match the canonical identifier grammar"
            )
        declared_so_far: set[str] = set()
        diagnostics: list[FormulaDiagnostic] = []
        for statement in program.statements:
            known_identifiers = set(self._fields) | declared_so_far | parameters
            for expression in _expressions(statement.expression):
                if isinstance(expression, Call):
                    spec = self._functions.get(expression.function)
                    if spec is None:
                        diagnostics.append(
                            _diagnostic(
                                expression,
                                "unsupported_function",
                                f"函数 {expression.function} 不在 {self.version} 兼容清单中。",
                            )
                        )
                    elif (
                        not spec.min_args <= len(expression.arguments) <= spec.max_args
                    ):
                        diagnostics.append(
                            _diagnostic(
                                expression,
                                "invalid_argument_count",
                                f"函数 {expression.function} 需要 {spec.min_args} 个参数，实际收到 {len(expression.arguments)} 个。",
                            )
                        )
                elif (
                    isinstance(expression, Name)
                    and expression.identifier not in known_identifiers
                ):
                    diagnostics.append(
                        FormulaDiagnostic(
                            "unknown_identifier",
                            f"标识符 {expression.identifier} 未声明且不是兼容行情字段。",
                            expression.identifier,
                            None,
                            expression.identifier,
                            expression.span.line,
                            expression.span.column,
                            expression.span.end_line,
                            expression.span.end_column,
                        )
                    )
            if statement.name in declared_so_far:
                diagnostics.append(
                    _declaration_diagnostic(
                        statement, "duplicate_declaration", "公式变量重复声明"
                    )
                )
            elif statement.name in self._fields or statement.name in parameters:
                diagnostics.append(
                    _declaration_diagnostic(
                        statement, "identifier_conflict", "声明名称与行情字段或参数冲突"
                    )
                )
            else:
                declared_so_far.add(statement.name)
        return tuple(
            sorted(
                diagnostics,
                key=lambda item: (item.line, item.column, item.code, item.name),
            )
        )


def _expressions(root: Expression) -> Iterator[Expression]:
    pending: list[Expression] = [root]
    while pending:
        expression = pending.pop()
        yield expression
        if isinstance(expression, Call):
            pending.extend(reversed(expression.arguments))
        elif isinstance(expression, Binary):
            pending.extend((expression.right, expression.left))
        elif isinstance(expression, Unary):
            pending.append(expression.operand)


def _diagnostic(
    call: Call,
    code: Literal["unsupported_function", "invalid_argument_count"],
    message: str,
) -> FormulaDiagnostic:
    diagnostic_code: DiagnosticCode = code
    return FormulaDiagnostic(
        code=diagnostic_code,
        message=message,
        name=call.function,
        function=call.function,
        identifier=None,
        line=call.span.line,
        column=call.span.column,
        end_line=call.span.end_line,
        end_column=call.span.end_column,
    )


def _declaration_diagnostic(
    statement: object,
    code: Literal["duplicate_declaration", "identifier_conflict"],
    message: str,
) -> FormulaDiagnostic:
    from stock_desk.formula.ast import Statement

    assert isinstance(statement, Statement)
    return FormulaDiagnostic(
        code,
        f"{message}：{statement.name}",
        statement.name,
        None,
        statement.name,
        statement.span.line,
        statement.span.column,
        statement.span.end_line,
        statement.span.end_column,
    )


def _field(
    name: str,
    canonical: str,
    summary: str,
    *,
    unit: Literal["price", "shares", "hands"] = "price",
    denominator: int = 1,
) -> FieldSpec:
    return FieldSpec(
        name, canonical, "number_series", summary, canonical, unit, 1, denominator
    )


_FIELDS = (
    _field("OPEN", "OPEN", "当前周期的开盘价。"),
    _field("O", "OPEN", "开盘价 OPEN 的通达信别名。"),
    _field("HIGH", "HIGH", "当前周期的最高价。"),
    _field("H", "HIGH", "最高价 HIGH 的通达信别名。"),
    _field("LOW", "LOW", "当前周期的最低价。"),
    _field("L", "LOW", "最低价 LOW 的通达信别名。"),
    _field("CLOSE", "CLOSE", "当前周期的收盘价。"),
    _field("C", "CLOSE", "收盘价 CLOSE 的通达信别名。"),
    _field("VOLUME", "VOLUME", "stock-desk 扩展成交量，单位为股。", unit="shares"),
    _field(
        "VOL",
        "VOLUME",
        "通达信成交量，A股按 100 股/手换算。",
        unit="hands",
        denominator=100,
    ),
    _field(
        "V",
        "VOLUME",
        "VOL 的通达信短别名，A股按 100 股/手换算。",
        unit="hands",
        denominator=100,
    ),
)

V1_REGISTRY = CompatibilityRegistry(
    version=COMPATIBILITY_VERSION,
    functions=(*SERIES_FUNCTIONS, *STATISTICS_FUNCTIONS, *SIGNAL_FUNCTIONS),
    fields=_FIELDS,
)
