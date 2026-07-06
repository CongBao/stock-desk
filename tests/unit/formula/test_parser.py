from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import stock_desk.formula.parser as parser_module
from stock_desk.formula.ast import Binary, Call, Name, Number, String
from stock_desk.formula.errors import FormulaLimitError, FormulaSyntaxError
from stock_desk.formula.parser import (
    MAX_AST_NODES,
    MAX_NESTING_DEPTH,
    MAX_SOURCE_BYTES,
    MAX_STATEMENTS,
    parse_formula,
)


def test_parser_understands_assignment_output_and_cross() -> None:
    program = parse_formula("DIF:=EMA(CLOSE,12)-EMA(CLOSE,26);BUY:CROSS(DIF,DEA);")

    assert [statement.name for statement in program.statements] == ["DIF", "BUY"]
    assert program.statements[0].visible is False
    assert program.statements[1].visible is True
    assert isinstance(program.statements[0].expression, Binary)
    assert program.statements[0].expression.operator == "-"
    assert isinstance(program.statements[1].expression, Call)
    assert program.statements[1].expression.function == "CROSS"


def test_market_open_field_is_not_treated_as_executable_python() -> None:
    program = parse_formula("X:OPEN+HIGH+LOW+CLOSE;")

    identifiers: list[str] = []
    expression = program.statements[0].expression
    pending = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, Name):
            identifiers.append(node.identifier)
        elif isinstance(node, Binary):
            pending.extend((node.left, node.right))
    assert set(identifiers) == {"OPEN", "HIGH", "LOW", "CLOSE"}


def test_zero_argument_call_has_no_placeholder_argument() -> None:
    expression = parse_formula("X:FOO();").statements[0].expression

    assert isinstance(expression, Call)
    assert expression.function == "FOO"
    assert expression.arguments == ()


def test_parser_is_case_insensitive_and_preserves_typed_literals() -> None:
    program = parse_formula(
        'message:"buy"; result:if(close >= 12.50 and not flag, 1, 0);'
    )

    message, result = program.statements
    assert message.name == "MESSAGE"
    assert isinstance(message.expression, String)
    assert message.expression.value == "buy"
    assert result.name == "RESULT"
    assert isinstance(result.expression, Call)
    assert result.expression.function == "IF"
    condition = result.expression.arguments[0]
    assert isinstance(condition, Binary)
    assert condition.operator == "AND"
    comparison = condition.left
    assert isinstance(comparison, Binary)
    assert comparison.operator == ">="
    assert isinstance(comparison.left, Name)
    assert comparison.left.identifier == "CLOSE"
    assert isinstance(comparison.right, Number)
    assert comparison.right.value == Decimal("12.50")


def test_comments_and_operator_precedence_are_supported() -> None:
    program = parse_formula(
        "// compatible TDX subset\nX:(1+2)*3=9 OR 2<>3; // output\n"
    )

    expression = program.statements[0].expression
    assert isinstance(expression, Binary)
    assert expression.operator == "OR"
    assert expression.span.line == 2
    assert program.span.end_line == 2


def test_parser_reports_line_and_column() -> None:
    with pytest.raises(FormulaSyntaxError) as error:
        parse_formula("DIF:=EMA(CLOSE,);")

    assert error.value.code == "formula_syntax_error"
    assert (error.value.line, error.value.column) == (1, 16)


@pytest.mark.parametrize(
    ("source", "limit_name"),
    [
        ("X:" + "1" * 129 + ";", "numeric_literal_chars"),
        ("X:1e10001;", "numeric_exponent"),
        ("X:1e" + "9" * 129 + ";", "numeric_literal_chars"),
    ],
)
def test_extreme_numeric_literals_return_stable_limit_errors(
    source: str, limit_name: str
) -> None:
    with pytest.raises(FormulaLimitError) as error:
        parse_formula(source)

    assert error.value.code == "formula_limit_exceeded"
    assert error.value.limit == limit_name


def test_numeric_limits_are_public() -> None:
    assert parser_module.MAX_NUMERIC_LITERAL_CHARS == 128
    assert parser_module.MAX_ABSOLUTE_EXPONENT == 10_000


@pytest.mark.parametrize("newline", ["\r", "\r\n"])
def test_comment_newlines_cannot_bypass_nesting_limit(newline: str) -> None:
    source = (
        "// comment"
        + newline
        + "X:"
        + "(" * (MAX_NESTING_DEPTH + 1)
        + "1"
        + ")" * (MAX_NESTING_DEPTH + 1)
        + ";"
    )

    with pytest.raises(FormulaLimitError) as error:
        parse_formula(source)

    assert error.value.limit == "nesting_depth"


def test_lone_surrogate_returns_stable_syntax_error() -> None:
    with pytest.raises(FormulaSyntaxError) as error:
        parse_formula('X:"\ud800";')

    assert error.value.code == "formula_syntax_error"
    assert (error.value.line, error.value.column) == (1, 4)


@pytest.mark.parametrize(
    "source",
    [
        "X:CLOSE.__class__;",
        "X:CLOSE[0];",
        'X:__import__("os");',
        "X:LAMBDA A:A;",
        "X:(IF)(A,B,C);",
        "X:GETTER()(CLOSE);",
    ],
)
def test_parser_rejects_executable_or_dynamic_python_shapes(source: str) -> None:
    with pytest.raises(FormulaSyntaxError):
        parse_formula(source)


def test_malicious_formula_has_no_filesystem_side_effect(tmp_path: Path) -> None:
    marker = tmp_path / "formula-side-effect"
    source = f'X:__import__("pathlib").Path("{marker}").touch();'

    with pytest.raises(FormulaSyntaxError):
        parse_formula(source)

    assert not marker.exists()


@pytest.mark.parametrize(
    ("source", "limit_name"),
    [
        ("X:" + "1" * (MAX_SOURCE_BYTES + 1) + ";", "source_bytes"),
        (
            "X:"
            + "(" * (MAX_NESTING_DEPTH + 1)
            + "1"
            + ")" * (MAX_NESTING_DEPTH + 1)
            + ";",
            "nesting_depth",
        ),
        ("".join(f"X{index}:1;" for index in range(MAX_STATEMENTS + 1)), "statements"),
        (
            "X:" + "+".join("1" for _ in range(MAX_AST_NODES * 8)) + ";",
            "ast_nodes",
        ),
    ],
)
def test_parser_enforces_resource_limits(source: str, limit_name: str) -> None:
    with pytest.raises(FormulaLimitError) as error:
        parse_formula(source)

    assert error.value.code == "formula_limit_exceeded"
    assert error.value.limit == limit_name


def test_parser_returns_immutable_ast() -> None:
    program = parse_formula("X:1;")

    with pytest.raises((AttributeError, TypeError)):
        program.statements[0].name = "CHANGED"  # type: ignore[misc]
