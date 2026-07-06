from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Literal


IDENTIFIER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

type FutureBehavior = Literal["current_only", "past_only", "future", "repainting"]
type FunctionCategory = Literal["math", "logic", "series", "statistics", "signal"]
type ValueKind = Literal["scalar", "integer_scalar", "number_series", "boolean_series"]
type ResultKind = Literal["number_series", "boolean_series"]
type DiagnosticCode = Literal[
    "unsupported_function",
    "invalid_argument_count",
    "unknown_identifier",
    "duplicate_declaration",
    "identifier_conflict",
]
type RelationOperator = Literal["<=", "<", ">=", ">", "=="]

VALUE_KIND_HIERARCHY = MappingProxyType({"integer_scalar": ("scalar",)})


def accepts_value_kind(accepted: tuple[ValueKind, ...], actual: ValueKind) -> bool:
    return actual in accepted or any(
        parent in accepted for parent in VALUE_KIND_HIERARCHY.get(actual, ())
    )


def _require_identifier(value: str, label: str) -> None:
    if IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must match the canonical identifier grammar")


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    accepted_kinds: tuple[ValueKind, ...]
    required: bool = True
    constant: bool = False
    minimum: int | None = None
    maximum: int | None = None
    constraints_zh: str = ""

    def __post_init__(self) -> None:
        _require_identifier(self.name, "parameter identifier")
        if not self.accepted_kinds or len(set(self.accepted_kinds)) != len(
            self.accepted_kinds
        ):
            raise ValueError(
                "parameter accepted value kinds must be unique and non-empty"
            )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.maximum < self.minimum
        ):
            raise ValueError("parameter bounds are inconsistent")

    def to_data(self) -> dict[str, object]:
        return {
            "accepted_kinds": list(self.accepted_kinds),
            "constant": self.constant,
            "constraints_zh": self.constraints_zh,
            "maximum": self.maximum,
            "minimum": self.minimum,
            "name": self.name,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class RelationSpec:
    left: str
    operator: RelationOperator
    right: str

    def __post_init__(self) -> None:
        _require_identifier(self.left, "relation left identifier")
        _require_identifier(self.right, "relation right identifier")

    def to_data(self) -> dict[str, object]:
        return {"left": self.left, "operator": self.operator, "right": self.right}


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    name: str
    category: FunctionCategory
    summary_zh: str
    future_behavior: FutureBehavior
    parameters: tuple[ParameterSpec, ...]
    result_kind: ResultKind
    dispatch_key: str
    semantics_zh: str
    relations: tuple[RelationSpec, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.name, "function identifier")
        if not self.summary_zh or not self.semantics_zh:
            raise ValueError("function documentation and result semantics are required")
        if re.fullmatch(r"[a-z][a-z0-9_.]*", self.dispatch_key) is None:
            raise ValueError("dispatch key is invalid")
        optional_seen = False
        parameter_names: set[str] = set()
        for parameter in self.parameters:
            optional_seen = optional_seen or not parameter.required
            if optional_seen and parameter.required:
                raise ValueError(
                    "required parameters cannot follow optional parameters"
                )
            if parameter.name in parameter_names:
                raise ValueError("parameter identifiers must be unique")
            parameter_names.add(parameter.name)
        if any(
            relation.left not in parameter_names
            or relation.right not in parameter_names
            for relation in self.relations
        ):
            raise ValueError("relations must reference declared parameters")

    @property
    def min_args(self) -> int:
        return sum(parameter.required for parameter in self.parameters)

    @property
    def max_args(self) -> int:
        return len(self.parameters)

    @property
    def signature(self) -> str:
        arguments = ", ".join(
            parameter.name if parameter.required else f"[{parameter.name}]"
            for parameter in self.parameters
        )
        return f"{self.name}({arguments})"

    def to_data(self) -> dict[str, object]:
        return {
            "category": self.category,
            "dispatch_key": self.dispatch_key,
            "future_behavior": self.future_behavior,
            "max_args": self.max_args,
            "min_args": self.min_args,
            "name": self.name,
            "parameters": [parameter.to_data() for parameter in self.parameters],
            "result_kind": self.result_kind,
            "relations": [relation.to_data() for relation in self.relations],
            "semantics_zh": self.semantics_zh,
            "signature": self.signature,
            "summary_zh": self.summary_zh,
        }


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    canonical_name: str
    value_type: ResultKind
    summary_zh: str
    source_name: str
    unit: Literal["price", "shares", "hands"]
    scale_numerator: int
    scale_denominator: int

    def __post_init__(self) -> None:
        _require_identifier(self.name, "field identifier")
        _require_identifier(self.canonical_name, "canonical field identifier")
        _require_identifier(self.source_name, "source field identifier")
        if not self.summary_zh:
            raise ValueError("field documentation metadata is required")
        if self.scale_numerator <= 0 or self.scale_denominator <= 0:
            raise ValueError("field scale must be positive")

    def to_data(self) -> dict[str, object]:
        return {
            "canonical_name": self.canonical_name,
            "name": self.name,
            "scale_denominator": self.scale_denominator,
            "scale_numerator": self.scale_numerator,
            "source_name": self.source_name,
            "summary_zh": self.summary_zh,
            "unit": self.unit,
            "value_type": self.value_type,
        }


@dataclass(frozen=True, slots=True)
class FormulaDiagnostic:
    code: DiagnosticCode
    message: str
    name: str
    function: str | None
    identifier: str | None
    line: int
    column: int
    end_line: int
    end_column: int
