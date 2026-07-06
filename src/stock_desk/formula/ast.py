from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SourceSpan:
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class Number:
    value: Decimal
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class String:
    value: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Name:
    identifier: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Unary:
    operator: str
    operand: Expression
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Binary:
    left: Expression
    operator: str
    right: Expression
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Call:
    function: str
    arguments: tuple[Expression, ...]
    span: SourceSpan


type Expression = Number | String | Name | Unary | Binary | Call


@dataclass(frozen=True, slots=True)
class Statement:
    name: str
    expression: Expression
    visible: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Program:
    statements: tuple[Statement, ...]
    span: SourceSpan
