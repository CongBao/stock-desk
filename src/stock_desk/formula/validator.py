from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from stock_desk.formula.analysis import (
    TemporalDiagnostic,
    analyze_compiled_formula,
    inspect_compiled_structure,
)
from stock_desk.formula.ast import SourceSpan
from stock_desk.formula.compiler import CompiledFormula, MAX_LOOKBACK, compile_formula
from stock_desk.formula.context import MAX_PARAMETERS
from stock_desk.formula.errors import FormulaError
from stock_desk.formula.functions import V1_REGISTRY, CompatibilityRegistry
from stock_desk.formula.parser import (
    MAX_AST_NODES,
    MAX_NESTING_DEPTH,
    MAX_SOURCE_BYTES,
    MAX_STATEMENTS,
)
from stock_desk.formula.signal_series import (
    MAX_PUBLIC_OUTPUTS,
    MAX_OUTPUT_CELLS,
    MAX_SIGNAL_SERIES_BYTES,
)
from stock_desk.formula.values import ScalarValue
from stock_desk.market.types import MAX_BAR_SERIES_ROWS


@dataclass(frozen=True, slots=True)
class FormulaResourcePolicy:
    max_source_bytes: int
    max_nesting_depth: int
    max_ast_nodes: int
    max_statements: int
    max_public_outputs: int
    max_parameters: int
    max_lookback: int
    max_input_rows: int
    ten_year_daily_preview_target_seconds: float
    max_response_bytes: int
    max_output_cells: int
    max_work_cells: int
    response_bytes_per_cell: int
    timestamp_bytes_per_row: int
    response_fixed_overhead_bytes: int
    timeout_strategy: Literal["deterministic_preflight_service_isolation_required"]
    hard_timeout_layer: Literal["formula_service_task6"]


FORMULA_RESOURCE_POLICY = FormulaResourcePolicy(
    max_source_bytes=MAX_SOURCE_BYTES,
    max_nesting_depth=MAX_NESTING_DEPTH,
    max_ast_nodes=MAX_AST_NODES,
    max_statements=MAX_STATEMENTS,
    max_public_outputs=MAX_PUBLIC_OUTPUTS,
    max_parameters=MAX_PARAMETERS,
    max_lookback=MAX_LOOKBACK,
    max_input_rows=MAX_BAR_SERIES_ROWS,
    ten_year_daily_preview_target_seconds=3.0,
    max_response_bytes=MAX_SIGNAL_SERIES_BYTES,
    max_output_cells=MAX_OUTPUT_CELLS,
    max_work_cells=10_000_000,
    response_bytes_per_cell=32,
    timestamp_bytes_per_row=40,
    response_fixed_overhead_bytes=4096,
    timeout_strategy="deterministic_preflight_service_isolation_required",
    hard_timeout_layer="formula_service_task6",
)


def _estimate_response_bytes(
    compiled: CompiledFormula,
    row_count: int,
    *,
    numeric_outputs: tuple[str, ...],
    signal_outputs: tuple[str, ...],
) -> int:
    output_count = len(numeric_outputs) + 2
    identifier_bytes = sum(
        len(name.encode("utf-8")) for name in (*numeric_outputs, *signal_outputs)
    )
    parameter_bytes = sum(
        len(item.name.encode("utf-8")) + len(str(item.value).encode("utf-8")) + 16
        for item in compiled.parameter_bindings
    )
    return (
        FORMULA_RESOURCE_POLICY.response_fixed_overhead_bytes
        + row_count * FORMULA_RESOURCE_POLICY.timestamp_bytes_per_row
        + row_count * output_count * FORMULA_RESOURCE_POLICY.response_bytes_per_cell
        + identifier_bytes
        + parameter_bytes
    )


def estimate_signal_series_response_bytes(
    compiled: CompiledFormula, row_count: int
) -> int:
    structure = inspect_compiled_structure(compiled)
    return _estimate_response_bytes(
        compiled,
        row_count,
        numeric_outputs=structure.numeric_outputs,
        signal_outputs=structure.signal_outputs,
    )


def _compile_diagnostic(error: FormulaError) -> TemporalDiagnostic:
    function = getattr(error, "function", None)
    if function is None and error.code == "future_data":
        function = "REF"
    return TemporalDiagnostic(
        code=error.code,
        function=function,
        span=SourceSpan(error.line, error.column, error.end_line, error.end_column),
        explanation=str(error),
        blocks_preview=True,
        blocks_save=True,
        blocks_backtest=True,
    )


class FormulaValidator:
    def __init__(self, registry: CompatibilityRegistry = V1_REGISTRY) -> None:
        self._registry = registry

    @property
    def resource_policy(self) -> FormulaResourcePolicy:
        return FORMULA_RESOURCE_POLICY

    def validate(
        self,
        source: str,
        *,
        parameters: Mapping[str, ScalarValue] | None = None,
    ) -> tuple[TemporalDiagnostic, ...]:
        try:
            compiled = compile_formula(
                source,
                parameters=parameters,
                registry=self._registry,
            )
        except FormulaError as error:
            return (_compile_diagnostic(error),)
        except (TypeError, ValueError):
            return (
                TemporalDiagnostic(
                    code="invalid_parameter_binding",
                    function=None,
                    span=SourceSpan(1, 1, 1, 1),
                    explanation="Formula parameter binding is invalid.",
                    blocks_preview=True,
                    blocks_save=True,
                    blocks_backtest=True,
                ),
            )
        return analyze_compiled_formula(
            compiled,
            registry=self._registry,
        ).diagnostics

    def validate_execution_budget(
        self,
        compiled: CompiledFormula,
        *,
        row_count: int,
    ) -> tuple[TemporalDiagnostic, ...]:
        structure = inspect_compiled_structure(compiled, registry=self._registry)
        if structure.diagnostics:
            return structure.diagnostics
        output_count = len(structure.numeric_outputs) + 2
        work_cells = (
            row_count * structure.weighted_work_units
            if type(row_count) is int and row_count >= 0
            else FORMULA_RESOURCE_POLICY.max_work_cells + 1
        )
        conservative_response_bytes = (
            _estimate_response_bytes(
                compiled,
                row_count,
                numeric_outputs=structure.numeric_outputs,
                signal_outputs=structure.signal_outputs,
            )
            if type(row_count) is int and row_count >= 0
            else FORMULA_RESOURCE_POLICY.max_response_bytes + 1
        )
        valid = (
            type(row_count) is int
            and 0 < row_count <= FORMULA_RESOURCE_POLICY.max_input_rows
            and row_count * output_count <= FORMULA_RESOURCE_POLICY.max_output_cells
            and work_cells <= FORMULA_RESOURCE_POLICY.max_work_cells
            and conservative_response_bytes
            <= FORMULA_RESOURCE_POLICY.max_response_bytes
        )
        if valid:
            return ()
        return (
            TemporalDiagnostic(
                code="resource_limit_exceeded",
                function=None,
                span=(
                    compiled.statements[0].span
                    if compiled.statements
                    else SourceSpan(1, 1, 1, 1)
                ),
                explanation="Execution request exceeds a deterministic formula resource budget.",
                blocks_preview=True,
                blocks_save=True,
                blocks_backtest=True,
            ),
        )
