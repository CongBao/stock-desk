from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from scripts.v1_backtest_oracle import (
    EXPECTED_ALLOWED_DIFFERENCE_IDS,
    ORACLE_PATH,
    V1_COMMIT,
    V1_TREE,
    OracleValidationError,
    load_inputs,
    load_oracle,
    validate_capture_source,
)


ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "tests/fixtures/backtest/v1_0_oracle_inputs.json"


def _resign(payload: dict[str, object]) -> None:
    unsigned = dict(payload)
    unsigned.pop("payload_digest", None)
    encoded = json.dumps(
        unsigned,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    payload["payload_digest"] = f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_v1_oracle_inputs_pin_immutable_source_and_complete_case_inventory() -> None:
    inputs = load_inputs(INPUTS)

    assert inputs["source"] == {
        "tag": "v1.0.0",
        "commit": V1_COMMIT,
        "tree": V1_TREE,
    }
    matrix = inputs["matrix"]
    case_ids = {
        f"{formula['id']}_{scope}_{period}"
        for formula in matrix["formulas"]
        for scope in matrix["scopes"]
        for period in matrix["periods"]
    }
    assert len(case_ids) == 12
    assert {item["id"] for item in inputs["special_cases"]} == {
        "a_share_constraints_60m",
        "open_position_costs_1d",
        "partial_pool_gap_1d",
    }
    assert {
        item["id"] for item in inputs["allowed_versioned_differences"]
    } == EXPECTED_ALLOWED_DIFFERENCE_IDS


def test_frozen_v1_oracle_is_self_verifying_and_covers_every_input() -> None:
    inputs = load_inputs(INPUTS)
    oracle = load_oracle(ORACLE_PATH, inputs_path=INPUTS)

    matrix = inputs["matrix"]
    expected = {
        f"{formula['id']}_{scope}_{period}"
        for formula in matrix["formulas"]
        for scope in matrix["scopes"]
        for period in matrix["periods"]
    } | {item["id"] for item in inputs["special_cases"]}
    assert set(oracle["cases"]) == expected
    assert oracle["source"]["commit"] == V1_COMMIT
    assert oracle["source"]["tree"] == V1_TREE
    assert oracle["case_count"] == 15


def test_oracle_validation_fails_closed_on_payload_or_allowlist_tampering(
    tmp_path: Path,
) -> None:
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    first_case = next(iter(oracle["cases"].values()))
    first_case["semantic"]["run"]["status"] = "tampered"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(oracle), encoding="utf-8")

    with pytest.raises(OracleValidationError, match="oracle payload digest"):
        load_oracle(tampered, inputs_path=INPUTS)

    changed_inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    changed_inputs["allowed_versioned_differences"].pop()
    changed = tmp_path / "inputs.json"
    changed.write_text(json.dumps(changed_inputs), encoding="utf-8")
    with pytest.raises(OracleValidationError, match="allowed versioned differences"):
        load_inputs(changed)


@pytest.mark.parametrize("field", ("paths", "normalization"))
def test_input_allowlist_rejects_same_id_with_expanded_authority(
    tmp_path: Path,
    field: str,
) -> None:
    changed_inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    difference = changed_inputs["allowed_versioned_differences"][0]
    if field == "paths":
        difference["paths"].append("snapshot.everything")
    else:
        difference["normalization"] = "accept-anything"
    changed = tmp_path / "inputs.json"
    changed.write_text(json.dumps(changed_inputs), encoding="utf-8")

    with pytest.raises(OracleValidationError, match="allowed versioned differences"):
        load_inputs(changed)


def test_input_matrix_and_special_case_drift_fail_closed(tmp_path: Path) -> None:
    changed_inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    changed_inputs["matrix"]["costs"]["commission_bps"] = "0"
    changed = tmp_path / "matrix.json"
    changed.write_text(json.dumps(changed_inputs), encoding="utf-8")
    with pytest.raises(OracleValidationError, match="input matrix"):
        load_inputs(changed)

    changed_inputs = json.loads(INPUTS.read_text(encoding="utf-8"))
    changed_inputs["special_cases"][0]["kind"] = "weakened"
    changed = tmp_path / "special.json"
    changed.write_text(json.dumps(changed_inputs), encoding="utf-8")
    with pytest.raises(OracleValidationError, match="special cases"):
        load_inputs(changed)


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ("source", "source identity"),
        ("schema", "schema is unsupported"),
        ("generator", "generator identity"),
        ("fixture", "case digest"),
    ),
)
def test_oracle_source_generator_and_fixture_schema_drift_fail_closed(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    if mutation == "source":
        oracle["source"]["tree"] = "0" * 40
    elif mutation == "schema":
        oracle["schema_version"] = "stock-desk-v1-backtest-oracle-v2"
    elif mutation == "generator":
        oracle["generator"]["projection_schema"] = "accept-anything"
    else:
        first_case = next(iter(oracle["cases"].values()))
        first_case.pop("input_digest")
    _resign(oracle)
    changed = tmp_path / f"{mutation}.json"
    changed.write_text(json.dumps(oracle), encoding="utf-8")

    with pytest.raises(OracleValidationError, match=error):
        load_oracle(changed, inputs_path=INPUTS)


def test_capture_refuses_current_main_or_a_mismatched_v1_tree(tmp_path: Path) -> None:
    with pytest.raises(OracleValidationError, match="immutable v1.0.0 source"):
        validate_capture_source(ROOT)

    fake = tmp_path / "identity"
    fake.mkdir()
    with pytest.raises(OracleValidationError, match="immutable v1.0.0 source"):
        validate_capture_source(
            fake,
            identity_reader=lambda _root: (V1_COMMIT, "0" * 40),
        )
