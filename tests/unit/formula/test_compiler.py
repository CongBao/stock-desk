from __future__ import annotations

import pytest

from stock_desk.formula.compiler import (
    FormulaCompileError,
    compile_formula,
    formula_source_checksum,
)
from stock_desk.formula.context import MAX_PARAMETERS
from stock_desk.formula.errors import FormulaLimitError
from stock_desk.formula.functions import V1_REGISTRY
from stock_desk.formula.functions.base import MAX_IDENTIFIER_CHARS
from stock_desk.formula.runtime.dispatch import KERNELS
from stock_desk.formula.values import IntegerScalar, NumberScalar


MACD = "DIF:EMA(CLOSE,12)-EMA(CLOSE,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"


def test_compiler_is_typed_source_ordered_and_dispatch_complete() -> None:
    compiled = compile_formula(MACD)

    assert tuple(item.name for item in compiled.statements) == (
        "DIF",
        "DEA",
        "MACD",
        "BUY",
        "SELL",
    )
    assert compiled.numeric_outputs == ("DIF", "DEA", "MACD")
    assert compiled.signal_outputs == ("BUY", "SELL")
    assert compiled.source_checksum == formula_source_checksum(MACD)
    assert {spec.dispatch_key for spec in V1_REGISTRY.functions()} == set(KERNELS)
    assert {
        spec.dispatch_key: spec.result_kind for spec in V1_REGISTRY.functions()
    } == {key: kernel.result_kind for key, kernel in KERNELS.items()}


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("A:B+1;B:2;", "unknown_identifier"),
        ("A:A+1;", "unknown_identifier"),
        ("A:1;A:2;", "duplicate_declaration"),
        ("CLOSE:1;", "identifier_conflict"),
        ("X:'not executable';", "invalid_type"),
        ("X:REF(CLOSE,-1);", "future_data"),
        ("BUY:CLOSE;", "invalid_signal_output"),
        ("SELL:=CROSS(CLOSE,1);", "invalid_signal_output"),
        ("X:MA(CLOSE,100001);", "argument_out_of_range"),
        ("X:SMA(CLOSE,2,3);", "invalid_argument_relation"),
    ],
)
def test_compiler_rejects_unsafe_names_types_and_constraints(
    source: str, code: str
) -> None:
    with pytest.raises(FormulaCompileError) as error:
        compile_formula(source)
    assert error.value.code == code
    assert error.value.line >= 1


def test_compiler_accepts_parameters_and_hidden_values_without_outputting_them() -> (
    None
):
    compiled = compile_formula(
        "TMP:=EMA(C,N);RESULT:TMP+1;",
        parameters={"N": IntegerScalar(3)},
    )

    assert compiled.numeric_outputs == ("RESULT",)
    assert compiled.signal_outputs == ()
    assert compiled.statements[0].visible is False


def test_parameter_values_execute_metadata_bounds_but_not_literal_only_contracts() -> (
    None
):
    with pytest.raises(FormulaCompileError) as negative:
        compile_formula("X:REF(C,N);", parameters={"N": IntegerScalar(-1)})
    assert negative.value.code == "future_data"

    with pytest.raises(FormulaCompileError) as derived_negative:
        compile_formula("X:REF(C,-N);", parameters={"N": IntegerScalar(1)})
    assert derived_negative.value.code == "future_data"

    with pytest.raises(FormulaCompileError) as too_large:
        compile_formula("X:MA(C,N);", parameters={"N": IntegerScalar(100_001)})
    assert too_large.value.code == "argument_out_of_range"

    direct = compile_formula("X:=FILTER(C,N);", parameters={"N": IntegerScalar(2)})
    unary = compile_formula("X:=FILTER(C,+N);", parameters={"N": IntegerScalar(2)})
    assert direct.statements[0].kind == unary.statements[0].kind == "boolean_series"


@pytest.mark.parametrize(
    "parameters",
    [
        {"n": IntegerScalar(1)},
        {"CLOSE": IntegerScalar(1)},
        {"N": NumberScalar(1.0)},
    ],
)
def test_compiler_rejects_noncanonical_parameter_bindings(parameters: object) -> None:
    with pytest.raises((FormulaCompileError, ValueError, TypeError)):
        compile_formula("X:REF(C,N);", parameters=parameters)  # type: ignore[arg-type]


@pytest.mark.parametrize("source", ["BUY:C>1;", "SELL:C<1;"])
def test_trading_signals_must_be_a_complete_pair(source: str) -> None:
    with pytest.raises(FormulaCompileError) as error:
        compile_formula(source)
    assert error.value.code == "incomplete_signal_pair"


def test_signal_source_order_is_normalized_in_compiled_contract() -> None:
    compiled = compile_formula("SELL:C<1;BUY:C>1;")
    assert tuple(item.name for item in compiled.statements) == ("SELL", "BUY")
    assert compiled.signal_outputs == ("BUY", "SELL")


def test_constant_arithmetic_is_deferred_to_runtime_semantics() -> None:
    assert compile_formula("X:1/0;").numeric_outputs == ("X",)
    assert compile_formula("X:1e308*1e308;").numeric_outputs == ("X",)


def test_source_checksum_applies_utf8_and_byte_limits_before_encoding() -> None:
    with pytest.raises(ValueError, match="source byte limit"):
        formula_source_checksum("X" * 64_001)
    with pytest.raises(ValueError, match="UTF-8"):
        formula_source_checksum("\ud800")


def test_compiler_enforces_the_shared_parameter_limit_before_binding() -> None:
    maximum = {f"P{index}": IntegerScalar(index) for index in range(MAX_PARAMETERS)}
    assert len(compile_formula("X:C;", parameters=maximum).parameter_bindings) == 64

    oversized = {**maximum, "OVER": IntegerScalar(1)}
    with pytest.raises(FormulaLimitError) as error:
        compile_formula("X:C;", parameters=oversized)
    assert error.value.code == "formula_limit_exceeded"
    assert error.value.limit == "parameters"
    assert error.value.maximum == MAX_PARAMETERS == 64


def test_identifier_length_is_bounded_for_parameters_and_outputs() -> None:
    maximum = "A" * MAX_IDENTIFIER_CHARS
    too_long = maximum + "A"

    assert compile_formula(f"{maximum}:1;").numeric_outputs == (maximum,)
    assert (
        compile_formula("X:C;", parameters={maximum: IntegerScalar(1)})
        .parameter_bindings[0]
        .name
        == maximum
    )

    with pytest.raises(FormulaCompileError) as output_error:
        compile_formula(f"{too_long}:1;")
    assert output_error.value.code == "invalid_identifier"
    with pytest.raises(ValueError, match="canonical"):
        compile_formula("X:C;", parameters={too_long: IntegerScalar(1)})


def test_compiler_limits_visible_numeric_outputs_before_runtime_allocation() -> None:
    source = "".join(f"X{i}:{i};" for i in range(33))
    with pytest.raises(FormulaCompileError) as error:
        compile_formula(source)
    assert error.value.code == "output_limit_exceeded"
