from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from stock_desk.formula.analysis import TEMPORAL_RULES, analyze_compiled_formula
from stock_desk.formula.ast import SourceSpan
from stock_desk.formula.compiler import (
    CallExpression,
    LiteralExpression,
    compile_formula,
)
from stock_desk.formula.functions import V1_REGISTRY
from stock_desk.formula.functions.base import FutureBehavior
from stock_desk.formula.functions.registry import CompatibilityRegistry
from stock_desk.formula.validator import (
    FORMULA_RESOURCE_POLICY,
    FormulaValidator,
    estimate_signal_series_response_bytes,
)
from stock_desk.formula.values import IntegerScalar
from stock_desk.market.types import MAX_BAR_SERIES_ROWS


def test_negative_literal_and_bound_ref_are_blocked_with_structured_diagnostics() -> (
    None
):
    validator = FormulaValidator()

    literal = validator.validate("X:REF(CLOSE,-1);")[0]
    bound = validator.validate("X:REF(CLOSE,N);", parameters={"N": IntegerScalar(-1)})[
        0
    ]

    for diagnostic in (literal, bound):
        assert diagnostic.code == "future_data"
        assert diagnostic.function == "REF"
        assert diagnostic.span.line == 1
        assert diagnostic.explanation
        assert diagnostic.blocks_preview is True
        assert diagnostic.blocks_save is True
        assert diagnostic.blocks_backtest is True


def test_positive_bound_ref_has_a_precise_past_dependency_interval() -> None:
    compiled = compile_formula("PAST:REF(C,N);", parameters={"N": IntegerScalar(3)})

    result = analyze_compiled_formula(compiled)

    assert result.diagnostics == ()
    assert result.statements[0].name == "PAST"
    assert result.statements[0].dependency.min_offset == -3
    assert result.statements[0].dependency.max_offset == -3
    assert result.append_only_stable is True
    assert result.source_checksum == compiled.source_checksum
    assert result.parameter_bindings == compiled.parameter_bindings
    assert result.compatibility_version == compiled.compatibility_version
    assert result.engine_version == compiled.engine_version


@pytest.mark.parametrize(
    ("source", "minimum", "maximum"),
    [
        ("X:MA(REF(C,3),2);", -4, -3),
        ("X:EMA(REF(C,3),2);", None, -3),
        ("X:SMA(REF(C,3),2,1);", None, -3),
        ("X:=FILTER(REF(C,3)>0,2);", None, -3),
        ("X:=LONGCROSS(REF(C,3),REF(C,4),2);", -6, -3),
    ],
)
def test_configuration_arguments_do_not_pollute_data_dependency_intervals(
    source: str, minimum: int | None, maximum: int
) -> None:
    result = analyze_compiled_formula(compile_formula(source))
    dependency = result.statements[0].dependency
    assert (dependency.min_offset, dependency.max_offset) == (minimum, maximum)
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "source",
    [
        "X:EMA(C,3);",
        "X:SMA(C,3,2);",
        "X:BARSLAST(C>0);",
        "X:=FILTER(C>0,2);",
        "X:SUM(C,0);",
        "X:HHV(C,0);",
    ],
)
def test_unbounded_past_dependencies_are_explicit_and_safe(source: str) -> None:
    result = analyze_compiled_formula(compile_formula(source))
    assert result.diagnostics == ()
    assert result.statements[0].dependency.min_offset is None
    assert result.statements[0].dependency.max_offset == 0
    assert result.append_only_stable


def test_nested_bounded_lookback_is_limited_cumulatively() -> None:
    result = analyze_compiled_formula(compile_formula("X:MA(MA(C,100000),3);"))
    diagnostic = result.diagnostics[0]
    assert diagnostic.code == "lookback_limit_exceeded"
    assert diagnostic.blocks_preview and diagnostic.blocks_save
    assert diagnostic.blocks_backtest
    assert result.append_only_stable is False


@pytest.mark.parametrize(
    ("behavior", "code"),
    [
        ("future", "future_data"),
        ("repainting", "repainting"),
        ("unknown", "unknown_temporal_behavior"),
    ],
)
def test_registry_temporal_metadata_blocks_unsafe_or_unknown_behavior(
    behavior: str, code: str
) -> None:
    functions = tuple(
        replace(
            spec,
            future_behavior=cast(FutureBehavior, behavior),
        )
        if spec.name == "ABS"
        else spec
        for spec in V1_REGISTRY.functions()
    )
    registry = CompatibilityRegistry(
        version="test-temporal-v1",
        functions=functions,
        fields=V1_REGISTRY.fields(),
    )
    compiled = compile_formula("X:ABS(C);", registry=registry)

    diagnostic = analyze_compiled_formula(compiled, registry=registry).diagnostics[0]

    assert diagnostic.code == code
    assert diagnostic.function == "ABS"
    assert diagnostic.span.column == 3
    assert diagnostic.blocks_save and diagnostic.blocks_backtest


def test_temporal_rule_mapping_exactly_covers_the_registry_and_fails_closed() -> None:
    assert set(TEMPORAL_RULES) == {
        spec.dispatch_key for spec in V1_REGISTRY.functions()
    }
    mystery = replace(
        V1_REGISTRY.function("ABS"),
        name="MYSTERY",
        dispatch_key="test.mystery",
    )
    registry = CompatibilityRegistry(
        version="test-mystery-v1",
        functions=(
            mystery,
            *(spec for spec in V1_REGISTRY.functions() if spec.name != "ABS"),
        ),
        fields=V1_REGISTRY.fields(),
    )
    compiled = compile_formula("X:MYSTERY(C);", registry=registry)
    diagnostic = analyze_compiled_formula(compiled, registry=registry).diagnostics[0]
    assert diagnostic.code == "unknown_temporal_rule"
    assert diagnostic.function == "MYSTERY"
    assert diagnostic.blocks_preview and diagnostic.blocks_save
    assert diagnostic.blocks_backtest


def test_defensive_analysis_blocks_a_forged_negative_ref_dependency() -> None:
    compiled = compile_formula("X:REF(C,1);")
    statement = compiled.statements[0]
    assert isinstance(statement.expression, CallExpression)
    forged_call = replace(
        statement.expression,
        arguments=(
            statement.expression.arguments[0],
            LiteralExpression(
                IntegerScalar(-1),
                "integer_scalar",
                SourceSpan(1, 9, 1, 11),
            ),
        ),
    )
    forged = replace(
        compiled,
        statements=(replace(statement, expression=forged_call),),
    )
    diagnostic = analyze_compiled_formula(forged).diagnostics[0]
    assert diagnostic.code == "invalid_compiled_ir"
    assert diagnostic.function == "REF"
    assert diagnostic.blocks_preview and diagnostic.blocks_save
    assert diagnostic.blocks_backtest


def test_call_identity_mismatch_is_fail_closed_before_temporal_propagation() -> None:
    compiled = compile_formula("R:=REF(C,100000);X:MA(R,3);")
    outer_statement = compiled.statements[1]
    assert isinstance(outer_statement.expression, CallExpression)
    forged = replace(
        compiled,
        statements=(
            compiled.statements[0],
            replace(
                outer_statement,
                expression=replace(outer_statement.expression, function="ABS"),
            ),
        ),
    )

    result = analyze_compiled_formula(forged)

    assert result.diagnostics[0].code == "invalid_compiled_ir"
    assert result.diagnostics[0].function == "ABS"
    assert result.statements == ()
    assert result.append_only_stable is False


def test_supported_registry_is_append_only_and_validator_is_deterministic() -> None:
    source = "FAST:=EMA(C,3);SLOW:=EMA(C,5);BUY:CROSS(FAST,SLOW);SELL:CROSS(SLOW,FAST);"
    validator = FormulaValidator()

    first = validator.validate(source)
    second = validator.validate(source)

    assert first == second == ()
    analysis = analyze_compiled_formula(compile_formula(source))
    assert analysis.append_only_stable is True
    assert all(item.dependency.max_offset <= 0 for item in analysis.statements)


def test_compile_errors_are_returned_without_source_or_side_effects() -> None:
    diagnostic = FormulaValidator().validate("X:UNKNOWN(C);")[0]
    assert diagnostic.code == "unsupported_function"
    assert diagnostic.function == "UNKNOWN"
    assert "X:UNKNOWN" not in diagnostic.explanation
    assert diagnostic.blocks_preview


def test_resource_policy_reuses_public_limits_and_avoids_wall_clock_semantics() -> None:
    assert FORMULA_RESOURCE_POLICY.max_input_rows == MAX_BAR_SERIES_ROWS == 100_000
    assert FORMULA_RESOURCE_POLICY.max_lookback == 100_000
    assert FORMULA_RESOURCE_POLICY.max_parameters == 64
    assert FORMULA_RESOURCE_POLICY.max_public_outputs == 32
    assert (
        FORMULA_RESOURCE_POLICY.ten_year_daily_preview_target_seconds
        == pytest.approx(3.0)
    )
    assert (
        FORMULA_RESOURCE_POLICY.timeout_strategy
        == "deterministic_preflight_service_isolation_required"
    )
    assert FORMULA_RESOURCE_POLICY.hard_timeout_layer == "formula_service_task6"


def test_execution_budget_preflight_rejects_rows_work_and_response_before_runtime() -> (
    None
):
    validator = FormulaValidator()
    compiled = compile_formula("X:C;")
    assert validator.validate_execution_budget(compiled, row_count=10) == ()

    diagnostic = validator.validate_execution_budget(compiled, row_count=100_001)[0]
    assert diagnostic.code == "resource_limit_exceeded"
    assert diagnostic.blocks_preview and diagnostic.blocks_save
    assert diagnostic.blocks_backtest

    source = "".join(f"X{index}:=C+{index};" for index in range(50)) + "OUT:X49;"
    complex_formula = compile_formula(source)
    work = validator.validate_execution_budget(complex_formula, row_count=100_000)[0]
    assert work.code == "resource_limit_exceeded"
    assert FORMULA_RESOURCE_POLICY.max_work_cells == 10_000_000


def test_weighted_work_budget_blocks_expensive_large_requests_but_allows_daily() -> (
    None
):
    validator = FormulaValidator()
    for count in (10, 33):
        source = "".join(f"S{index}:=STD(C,2);" for index in range(count))
        source += f"OUT:S{count - 1};"
        compiled = compile_formula(source)
        assert (
            validator.validate_execution_budget(compiled, row_count=100_000)[0].code
            == "resource_limit_exceeded"
        )

    macd = (
        "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"
        "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
    )
    matrix = (
        "R:=REF(C,1);M:=MA(C,3);E:=EMA(C,3);S:=SMA(C,3,2);"
        "HV:=HHV(C,3);LV:=LLV(C,3);T:=SUM(C,2);N:=COUNT(C>0,2);"
        "D:=STD(C,2);CR:=CROSS(E,S);LC:=LONGCROSS(C,R,2);"
        "B:=BARSLAST(LC);F:=FILTER(CR,2);A:=ABS(T);"
        "MX:=MAX(A,HV);MN:=MIN(LV,B);X:IF(F,MX,MN);"
    )
    assert (
        validator.validate_execution_budget(compile_formula(macd), row_count=2_500)
        == ()
    )
    assert (
        validator.validate_execution_budget(compile_formula(matrix), row_count=2_500)
        == ()
    )


def test_forged_output_summary_cannot_understate_budget_or_allocations() -> None:
    source = "".join(f"X{index}:{index};" for index in range(32))
    compiled = compile_formula(source)
    forged = replace(compiled, numeric_outputs=())

    analysis = analyze_compiled_formula(forged)
    budget = FormulaValidator().validate_execution_budget(forged, row_count=100_000)

    assert analysis.diagnostics[0].code == "invalid_compiled_ir"
    assert budget[0].code == "invalid_compiled_ir"
    assert analysis.statements == ()


def test_analysis_fails_closed_on_registry_identity_mismatch() -> None:
    compiled = compile_formula("X:C;")
    registry = CompatibilityRegistry(
        version="different-v1",
        functions=V1_REGISTRY.functions(),
        fields=V1_REGISTRY.fields(),
    )
    diagnostic = analyze_compiled_formula(compiled, registry=registry).diagnostics[0]
    assert diagnostic.code == "registry_version_mismatch"
    assert diagnostic.blocks_preview and diagnostic.blocks_save
    assert diagnostic.blocks_backtest


def test_registry_mismatch_short_circuits_before_missing_function_lookup() -> None:
    compiled = compile_formula("X:MA(C,3);")
    registry = CompatibilityRegistry(
        version="abs-only-v1",
        functions=(V1_REGISTRY.function("ABS"),),
        fields=V1_REGISTRY.fields(),
    )

    result = analyze_compiled_formula(compiled, registry=registry)

    assert tuple(item.code for item in result.diagnostics) == (
        "registry_version_mismatch",
    )
    assert result.statements == ()
    assert result.append_only_stable is False


def test_response_estimate_is_conservative_under_the_cell_limit() -> None:
    policy = FORMULA_RESOURCE_POLICY
    assert policy.response_bytes_per_cell == 32
    maximum = (
        policy.max_output_cells * policy.response_bytes_per_cell
        + policy.max_input_rows * policy.timestamp_bytes_per_row
        + policy.response_fixed_overhead_bytes
    )
    assert maximum < policy.max_response_bytes

    short = compile_formula("X:C;", parameters={"P": IntegerScalar(1)})
    long = compile_formula(f"{'X' * 64}:C;", parameters={"P" * 64: IntegerScalar(1)})
    assert estimate_signal_series_response_bytes(long, 10) > (
        estimate_signal_series_response_bytes(short, 10)
    )
