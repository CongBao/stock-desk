from __future__ import annotations

from decimal import Decimal, DecimalException
from functools import lru_cache
from importlib import resources
from typing import cast

from lark import Lark, Token, Tree
from lark.exceptions import UnexpectedInput

from stock_desk.formula.ast import (
    Binary,
    Call,
    Expression,
    Name,
    Number,
    Program,
    SourceSpan,
    Statement,
    String,
    Unary,
)
from stock_desk.formula.errors import FormulaLimitError, FormulaSyntaxError


MAX_SOURCE_BYTES = 64_000
MAX_NESTING_DEPTH = 64
MAX_AST_NODES = 256
MAX_STATEMENTS = 128
MAX_NUMERIC_LITERAL_CHARS = 128
MAX_ABSOLUTE_EXPONENT = 10_000

_BINARY_OPERATORS = {
    "add": "+",
    "and_operation": "AND",
    "divide": "/",
    "equal": "=",
    "greater_or_equal": ">=",
    "greater_than": ">",
    "less_or_equal": "<=",
    "less_than": "<",
    "modulo": "%",
    "multiply": "*",
    "not_equal": "<>",
    "or_operation": "OR",
    "subtract": "-",
}
_UNARY_OPERATORS = {
    "negative": "-",
    "not_operation": "NOT",
    "positive": "+",
}


@lru_cache(maxsize=1)
def _parser() -> Lark:
    grammar = (
        resources.files("stock_desk.formula")
        .joinpath("grammar.lark")
        .read_text(encoding="utf-8")
    )
    return Lark(
        grammar,
        parser="lalr",
        start="start",
        propagate_positions=True,
        maybe_placeholders=False,
    )


def _span(node: Tree[Token] | Token) -> SourceSpan:
    if isinstance(node, Token):
        return SourceSpan(
            line=cast(int, node.line),
            column=cast(int, node.column),
            end_line=cast(int, node.end_line),
            end_column=cast(int, node.end_column),
        )
    return SourceSpan(
        line=node.meta.line,
        column=node.meta.column,
        end_line=node.meta.end_line,
        end_column=node.meta.end_column,
    )


def _syntax_error(message: str, node: Tree[Token] | Token) -> FormulaSyntaxError:
    span = _span(node)
    return FormulaSyntaxError(
        message,
        line=span.line,
        column=span.column,
        end_line=span.end_line,
        end_column=span.end_column,
    )


def _identifier(token: Token) -> str:
    return str(token).upper()


def _decode_string(token: Token) -> str:
    raw = str(token)
    quote = raw[0]
    output: list[str] = []
    index = 1
    while index < len(raw) - 1:
        character = raw[index]
        if character != "\\":
            output.append(character)
            index += 1
            continue
        index += 1
        if index >= len(raw) - 1:
            raise _syntax_error("String literal ends with an invalid escape.", token)
        escaped = raw[index]
        translations = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
        if escaped in translations:
            output.append(translations[escaped])
        elif escaped in {"\\", quote, "'", '"'}:
            output.append(escaped)
        else:
            raise _syntax_error("String literal contains an unsupported escape.", token)
        index += 1
    return "".join(output)


def _number(token: Token) -> Number:
    literal = str(token)
    span = _span(token)
    if len(literal) > MAX_NUMERIC_LITERAL_CHARS:
        raise FormulaLimitError(
            limit="numeric_literal_chars",
            maximum=MAX_NUMERIC_LITERAL_CHARS,
            line=span.line,
            column=span.column,
        )
    exponent_marker = max(literal.find("e"), literal.find("E"))
    if exponent_marker >= 0:
        exponent = int(literal[exponent_marker + 1 :])
        if abs(exponent) > MAX_ABSOLUTE_EXPONENT:
            raise FormulaLimitError(
                limit="numeric_exponent",
                maximum=MAX_ABSOLUTE_EXPONENT,
                line=span.line,
                column=span.column,
            )
    try:
        value = Decimal(literal)
    except DecimalException:
        raise _syntax_error("Numeric literal is invalid.", token) from None
    return Number(value=value, span=span)


def _expression(tree: Tree[Token]) -> Expression:
    node_type = str(tree.data)
    if node_type == "number":
        token = cast(Token, tree.children[0])
        return _number(token)
    if node_type == "string":
        token = cast(Token, tree.children[0])
        return String(value=_decode_string(token), span=_span(tree))
    if node_type == "name":
        token = cast(Token, tree.children[0])
        return Name(identifier=_identifier(token), span=_span(tree))
    if node_type == "call":
        token = cast(Token, tree.children[0])
        arguments = tuple(
            _expression(cast(Tree[Token], argument)) for argument in tree.children[1:]
        )
        return Call(
            function=_identifier(token),
            arguments=arguments,
            span=_span(tree),
        )
    if node_type in _UNARY_OPERATORS:
        operand = _expression(cast(Tree[Token], tree.children[0]))
        return Unary(
            operator=_UNARY_OPERATORS[node_type],
            operand=operand,
            span=_span(tree),
        )
    if node_type in _BINARY_OPERATORS:
        left, right = tree.children
        return Binary(
            left=_expression(cast(Tree[Token], left)),
            operator=_BINARY_OPERATORS[node_type],
            right=_expression(cast(Tree[Token], right)),
            span=_span(tree),
        )
    raise _syntax_error("Expression is outside the supported formula grammar.", tree)


def _statement(tree: Tree[Token]) -> Statement:
    name_node, assignment_node, expression_node = tree.children
    name = _identifier(cast(Token, name_node))
    assignment = cast(Tree[Token], assignment_node)
    return Statement(
        name=name,
        expression=_expression(cast(Tree[Token], expression_node)),
        visible=str(assignment.data) == "visible_assignment",
        span=_span(tree),
    )


def _enforce_parse_tree_limits(tree: Tree[Token]) -> None:
    if len(tree.children) > MAX_STATEMENTS:
        raise FormulaLimitError(limit="statements", maximum=MAX_STATEMENTS)

    node_count = 1
    pending = [cast(Tree[Token], statement) for statement in tree.children]
    while pending:
        node = pending.pop()
        if str(node.data) not in {"hidden_assignment", "visible_assignment"}:
            node_count += 1
            if node_count > MAX_AST_NODES:
                raise FormulaLimitError(limit="ast_nodes", maximum=MAX_AST_NODES)
        pending.extend(child for child in node.children if isinstance(child, Tree))


def _enforce_source_limits(source: str) -> None:
    if len(source) > MAX_SOURCE_BYTES:
        raise FormulaLimitError(limit="source_bytes", maximum=MAX_SOURCE_BYTES)
    try:
        encoded_source = source.encode("utf-8")
    except UnicodeEncodeError as error:
        line = 1
        column = 1
        index = 0
        while index < error.start:
            character = source[index]
            if character in {"\r", "\n"}:
                line += 1
                column = 1
                index += (
                    2
                    if character == "\r" and source[index + 1 : index + 2] == "\n"
                    else 1
                )
                continue
            column += 1
            index += 1
        raise FormulaSyntaxError(
            "Formula source contains invalid Unicode.",
            line=line,
            column=column,
        ) from None
    if len(encoded_source) > MAX_SOURCE_BYTES:
        raise FormulaLimitError(limit="source_bytes", maximum=MAX_SOURCE_BYTES)

    depth = 0
    line = 1
    column = 0
    quote: str | None = None
    escaped = False
    in_comment = False
    index = 0
    while index < len(source):
        character = source[index]
        column += 1
        if character in {"\r", "\n"}:
            line += 1
            column = 0
            in_comment = False
            index += (
                2 if character == "\r" and source[index + 1 : index + 2] == "\n" else 1
            )
            continue
        if in_comment:
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "/" and index + 1 < len(source) and source[index + 1] == "/":
            in_comment = True
        elif character == "(":
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise FormulaLimitError(
                    limit="nesting_depth",
                    maximum=MAX_NESTING_DEPTH,
                    line=line,
                    column=column,
                )
        elif character == ")":
            depth = max(depth - 1, 0)
        index += 1


def parse_formula(source: str) -> Program:
    """Parse source into an immutable AST without executing source text."""

    _enforce_source_limits(source)
    try:
        tree = _parser().parse(source)
    except UnexpectedInput as error:
        raise FormulaSyntaxError(
            "Formula is outside the supported TDX-compatible grammar.",
            line=error.line,
            column=error.column,
        ) from None
    _enforce_parse_tree_limits(tree)
    statements = tuple(
        _statement(cast(Tree[Token], statement)) for statement in tree.children
    )
    return Program(statements=statements, span=_span(tree))
