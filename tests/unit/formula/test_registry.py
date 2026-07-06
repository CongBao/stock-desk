from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from stock_desk.formula.compatibility import (
    COMPATIBILITY_VERSION,
    compatibility_data,
    compatibility_json,
    main as compatibility_main,
    render_compatibility_markdown,
)
from stock_desk.formula.functions.base import (
    FieldSpec,
    FunctionSpec,
    ParameterSpec,
    RelationSpec,
    accepts_value_kind,
    MAX_IDENTIFIER_CHARS,
)
from stock_desk.formula.functions.registry import V1_REGISTRY, CompatibilityRegistry
from stock_desk.formula.parser import parse_formula


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPATIBILITY_DOC = REPO_ROOT / "docs" / "formula-compatibility.md"


def test_v1_registry_contains_documented_common_subset() -> None:
    assert {
        "ABS",
        "MAX",
        "MIN",
        "REF",
        "MA",
        "EMA",
        "SMA",
        "HHV",
        "LLV",
        "SUM",
        "COUNT",
        "STD",
        "IF",
        "CROSS",
        "LONGCROSS",
        "BARSLAST",
        "FILTER",
    } == set(V1_REGISTRY.names())


def test_v1_registry_exposes_canonical_ohlcv_names_and_tdx_aliases() -> None:
    assert set(V1_REGISTRY.field_names()) == {
        "OPEN",
        "O",
        "HIGH",
        "H",
        "LOW",
        "L",
        "CLOSE",
        "C",
        "VOLUME",
        "VOL",
        "V",
    }
    assert "AMOUNT" not in V1_REGISTRY.field_names()
    assert V1_REGISTRY.field("O").canonical_name == "OPEN"
    assert V1_REGISTRY.field("H").canonical_name == "HIGH"
    assert V1_REGISTRY.field("L").canonical_name == "LOW"
    assert V1_REGISTRY.field("C").canonical_name == "CLOSE"
    assert V1_REGISTRY.field("VOL").canonical_name == "VOLUME"
    assert V1_REGISTRY.field("V").canonical_name == "VOLUME"
    assert V1_REGISTRY.field("CLOSE").value_type == "number_series"


def test_function_specs_are_immutable_and_have_explicit_arity() -> None:
    expected_arities = {
        "ABS": (1, 1),
        "MAX": (2, 2),
        "MIN": (2, 2),
        "REF": (2, 2),
        "MA": (2, 2),
        "EMA": (2, 2),
        "SMA": (3, 3),
        "HHV": (2, 2),
        "LLV": (2, 2),
        "SUM": (2, 2),
        "COUNT": (2, 2),
        "STD": (2, 2),
        "IF": (3, 3),
        "CROSS": (2, 2),
        "LONGCROSS": (3, 3),
        "BARSLAST": (1, 1),
        "FILTER": (2, 2),
    }
    assert {
        name: (V1_REGISTRY.function(name).min_args, V1_REGISTRY.function(name).max_args)
        for name in V1_REGISTRY.names()
    } == expected_arities
    assert all(
        spec.future_behavior in {"current_only", "past_only"}
        for spec in V1_REGISTRY.functions()
    )

    with pytest.raises(FrozenInstanceError):
        V1_REGISTRY.function("MA").parameters = ()  # type: ignore[misc]


def test_parameter_metadata_expresses_window_and_constant_constraints() -> None:
    ref_n = V1_REGISTRY.function("REF").parameters[1]
    filter_n = V1_REGISTRY.function("FILTER").parameters[1]
    longcross_n = V1_REGISTRY.function("LONGCROSS").parameters[2]
    sma_m = V1_REGISTRY.function("SMA").parameters[2]

    assert ref_n.accepted_kinds == ("integer_scalar",)
    assert ref_n.minimum == 0
    assert ref_n.constant is False
    assert filter_n.minimum == 1 and filter_n.constant is True
    assert longcross_n.minimum == 1 and longcross_n.constant is True
    assert sma_m.minimum == 1
    assert V1_REGISTRY.function("SMA").relations == (
        RelationSpec(left="M", operator="<=", right="N"),
    )


def test_window_semantics_use_bar_positions_without_compressing_nulls() -> None:
    ma = V1_REGISTRY.function("MA").semantics_zh
    std = V1_REGISTRY.function("STD").semantics_zh
    for name in ("HHV", "LLV", "SUM", "COUNT"):
        semantics = V1_REGISTRY.function(name).semantics_zh
        assert "最近 N 个 bar" in semantics
        assert "忽略 null" in semantics
        assert "至少一个有效值" in semantics
    assert "最近 N 个 bar 位置全部有效" in ma
    assert "最近 N 个 bar 位置全部有效" in std
    assert "最近 N 个有效值" not in ma + std


def test_signal_null_semantics_are_explicit_exceptions_to_strict_propagation() -> None:
    barslast = V1_REGISTRY.function("BARSLAST").semantics_zh
    filtered = V1_REGISTRY.function("FILTER").semantics_zh
    assert "null 视为未命中" in barslast
    assert "已有状态" in barslast
    assert "null 视为未命中" in filtered
    assert "抑制期仍按 bar 推进" in filtered


def test_scalar_broadcast_kinds_cover_cross_and_pointwise_functions() -> None:
    accepted = ("scalar", "number_series")
    for name in ("CROSS", "LONGCROSS"):
        assert V1_REGISTRY.function(name).parameters[0].accepted_kinds == accepted
        assert V1_REGISTRY.function(name).parameters[1].accepted_kinds == accepted
    assert V1_REGISTRY.function("ABS").parameters[0].accepted_kinds == accepted
    for name in ("MAX", "MIN"):
        assert all(
            parameter.accepted_kinds == accepted
            for parameter in V1_REGISTRY.function(name).parameters
        )
    if_spec = V1_REGISTRY.function("IF")
    assert "scalar" in if_spec.parameters[0].accepted_kinds
    assert if_spec.parameters[1].accepted_kinds == accepted
    assert if_spec.parameters[2].accepted_kinds == accepted
    for name in ("COUNT", "BARSLAST", "FILTER"):
        assert "scalar" in V1_REGISTRY.function(name).parameters[0].accepted_kinds
    assert accepts_value_kind(("scalar", "number_series"), "integer_scalar")
    assert not accepts_value_kind(("number_series",), "integer_scalar")


@pytest.mark.parametrize(
    "source", ["X:CROSS(CLOSE,1);", "X:CROSS(1,CLOSE);", "X:CROSS(1,2);"]
)
def test_cross_metadata_accepts_scalar_broadcast_shapes(source: str) -> None:
    assert V1_REGISTRY.validate(parse_formula(source)) == ()


def test_function_signature_arity_result_and_dispatch_are_derived_metadata() -> None:
    sma = V1_REGISTRY.function("SMA")

    assert sma.signature == "SMA(X, N, M)"
    assert (sma.min_args, sma.max_args) == (3, 3)
    assert sma.result_kind == "number_series"
    assert sma.dispatch_key == "series.sma"
    assert "首个有效值" in sma.semantics_zh


def test_unknown_function_is_rejected_at_its_source_span() -> None:
    diagnostics = V1_REGISTRY.validate(parse_formula("X:UNKNOWN(CLOSE, 3);"))

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.code == "unsupported_function"
    assert diagnostic.function == "UNKNOWN"
    assert (diagnostic.line, diagnostic.column) == (1, 3)
    assert "UNKNOWN" in diagnostic.message


def test_invalid_argument_count_is_rejected_without_temporal_analysis() -> None:
    diagnostics = V1_REGISTRY.validate(parse_formula("X:MA(CLOSE);"))

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.code == "invalid_argument_count"
    assert diagnostic.function == "MA"
    assert diagnostic.line == 1
    assert "2" in diagnostic.message


def test_unknown_identifier_uses_fields_declarations_and_explicit_parameters() -> None:
    program = parse_formula("DIF:=EMA(C,FAST)-EMA(CLOSE,SLOW);BUY:CROSS(DIF,MISSING);")

    diagnostics = V1_REGISTRY.validate(
        program,
        parameter_names=("FAST", "SLOW"),
    )

    assert [(item.code, item.identifier, item.line) for item in diagnostics] == [
        ("unknown_identifier", "MISSING", 1)
    ]


def test_declarations_are_validated_in_statement_order() -> None:
    diagnostics = V1_REGISTRY.validate(parse_formula("A:B+1;B:2;SELF:SELF+1;"))
    assert [(item.code, item.identifier) for item in diagnostics] == [
        ("unknown_identifier", "B"),
        ("unknown_identifier", "SELF"),
    ]


@pytest.mark.parametrize(
    ("source", "parameters", "code"),
    [
        ("X:1;X:2;", (), "duplicate_declaration"),
        ("CLOSE:1;", (), "identifier_conflict"),
        ("FAST:1;", ("FAST",), "identifier_conflict"),
    ],
)
def test_declaration_conflicts_are_source_located(
    source: str, parameters: tuple[str, ...], code: str
) -> None:
    diagnostic = V1_REGISTRY.validate(
        parse_formula(source), parameter_names=parameters
    )[0]
    assert diagnostic.code == code
    assert diagnostic.identifier in {"X", "CLOSE", "FAST"}
    assert diagnostic.line == 1


def test_nested_diagnostics_have_stable_source_order() -> None:
    diagnostics = V1_REGISTRY.validate(
        parse_formula("X:UNKNOWN1(MA(MISSING,1),UNKNOWN2(C));")
    )

    assert [(item.code, item.name) for item in diagnostics] == [
        ("unsupported_function", "UNKNOWN1"),
        ("unknown_identifier", "MISSING"),
        ("unsupported_function", "UNKNOWN2"),
    ]


def test_compatibility_export_is_deterministic_json_for_shared_consumers() -> None:
    first = compatibility_json()
    second = compatibility_json()
    payload = json.loads(first)

    assert first == second
    assert first.endswith("\n")
    assert payload == compatibility_data()
    assert payload["compatibility_version"] == COMPATIBILITY_VERSION == "tdx-v1"
    assert payload["parser_limits"]["identifier_chars"] == MAX_IDENTIFIER_CHARS == 64
    assert [item["name"] for item in payload["functions"]] == sorted(
        V1_REGISTRY.names()
    )
    assert [item["name"] for item in payload["fields"]] == sorted(
        V1_REGISTRY.field_names()
    )
    assert all("summary_zh" in item for item in payload["functions"])
    assert all("parameters" in item for item in payload["functions"])
    assert all("dispatch_key" in item for item in payload["functions"])
    assert all("semantics_zh" in item for item in payload["functions"])
    assert payload["runtime_semantics"]["numeric_storage"] == "float64"
    assert "null" in payload["runtime_semantics"]["division_by_zero"]
    assert payload["value_kind_hierarchy"]["integer_scalar"] == ["scalar"]


def test_checked_in_compatibility_document_is_generated_without_drift() -> None:
    expected = render_compatibility_markdown()

    assert COMPATIBILITY_DOC.read_text(encoding="utf-8") == expected
    assert compatibility_main(["--check", str(COMPATIBILITY_DOC)]) == 0


def test_compatibility_cli_can_regenerate_document(tmp_path: Path) -> None:
    target = tmp_path / "formula-compatibility.md"

    assert compatibility_main(["--write", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == render_compatibility_markdown()


def test_function_spec_rejects_inconsistent_metadata() -> None:
    with pytest.raises(ValueError, match="identifier"):
        ParameterSpec(name="N-1", accepted_kinds=("integer_scalar",))

    with pytest.raises(ValueError, match="identifier"):
        FunctionSpec(
            name="BAD-NAME",
            category="math",
            summary_zh="错误元数据",
            future_behavior="current_only",
            parameters=(),
            result_kind="number_series",
            dispatch_key="math.bad",
            semantics_zh="无效名称。",
        )

    with pytest.raises(ValueError, match="identifier"):
        FieldSpec(
            "BAD-NAME", "CLOSE", "number_series", "错误字段。", "CLOSE", "price", 1, 1
        )


def test_registry_accepts_single_pass_metadata_iterables() -> None:
    function = V1_REGISTRY.function("ABS")
    field = V1_REGISTRY.field("CLOSE")

    registry = CompatibilityRegistry(
        version="test-v1",
        functions=(item for item in (function,)),
        fields=(item for item in (field,)),
    )

    assert registry.functions() == (function,)
    assert registry.fields() == (field,)


def test_registry_is_deeply_immutable_and_requires_a_version() -> None:
    with pytest.raises(FrozenInstanceError):
        V1_REGISTRY.version = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        V1_REGISTRY._functions = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        V1_REGISTRY._functions["ABS"] = V1_REGISTRY.function("ABS")  # type: ignore[index]

    with pytest.raises(ValueError, match="version"):
        CompatibilityRegistry(
            version="",
            functions=(V1_REGISTRY.function("ABS"),),
            fields=(V1_REGISTRY.field("CLOSE"),),
        )


def test_volume_field_units_and_scales_are_task3_executable() -> None:
    volume = V1_REGISTRY.field("VOLUME")
    assert (
        volume.source_name,
        volume.unit,
        volume.scale_numerator,
        volume.scale_denominator,
    ) == ("VOLUME", "shares", 1, 1)
    for alias in ("VOL", "V"):
        field = V1_REGISTRY.field(alias)
        assert (
            field.source_name,
            field.unit,
            field.scale_numerator,
            field.scale_denominator,
        ) == ("VOLUME", "hands", 1, 100)
    assert "AMOUNT" not in V1_REGISTRY.field_names()


@pytest.mark.parametrize(
    ("name", "required_text"),
    [
        ("EMA", "Y=2*X/(N+1)+(N-1)*Y_PREV/(N+1)"),
        ("SMA", "Y=(M*X+(N-M)*Y_PREV)/N"),
        ("MA", "N 个 bar 位置全部有效"),
        ("CROSS", "X[t]>Y[t] 且 X[t-1]<=Y[t-1]"),
        ("LONGCROSS", "此前连续 N 个完整周期"),
        ("BARSLAST", "从未成立则为 null"),
        ("STD", "样本标准差"),
        ("REF", "历史不足返回 null"),
        ("FILTER", "后续 N 个周期"),
        ("SUM", "N=0 从首个有效值累计"),
        ("COUNT", "N=0 从首个有效值累计"),
        ("HHV", "N=0 从首个有效值累计"),
        ("LLV", "N=0 从首个有效值累计"),
    ],
)
def test_frozen_v1_semantics_are_precise(name: str, required_text: str) -> None:
    assert required_text in V1_REGISTRY.function(name).semantics_zh
