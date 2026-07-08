from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_requirement_coverage.py"
MANIFEST = ROOT / "tests" / "acceptance" / "requirements.yml"
DOC = ROOT / "docs" / "acceptance.md"
NON_GOAL_TEST = ROOT / "tests" / "acceptance" / "test_non_goal_inventory.py"


def load_checker() -> ModuleType:
    assert SCRIPT.is_file(), "requirement coverage checker has not been implemented"
    spec = importlib.util.spec_from_file_location("requirement_coverage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_non_goal_inventory() -> ModuleType:
    spec = importlib.util.spec_from_file_location("non_goal_inventory", NON_GOAL_TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    return load_checker()


@pytest.fixture()
def matrix(checker: ModuleType) -> dict[str, object]:
    return checker.load_manifest(MANIFEST)


def validate_without_collecting(checker: ModuleType, matrix: dict[str, object]) -> None:
    checker.validate_manifest(
        matrix, repo_root=ROOT, mode="mapping", verify_selectors=False
    )


def test_mapping_cli_collects_every_existing_selector() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "mapping"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "77/77 requirements mapped" in result.stdout
    assert "10/10 non-goals mapped to absence checks" in result.stdout
    assert "planned/manual evidence explicitly enumerated" in result.stdout


def test_manifest_has_exact_ids_and_unique_semantics(matrix: dict[str, object]) -> None:
    requirements = matrix["requirements"]
    non_goals = matrix["non_goals"]
    assert isinstance(requirements, list)
    assert isinstance(non_goals, list)
    assert [item["id"] for item in requirements] == [
        f"R-{number:03d}" for number in range(1, 78)
    ]
    assert [item["id"] for item in non_goals] == [
        f"N-{number:03d}" for number in range(1, 11)
    ]
    behavior_keys = [item["behavior_key"] for item in requirements + non_goals]
    assert len(behavior_keys) == len(set(behavior_keys)) == 87
    assert all(item["acceptance"].strip() for item in requirements + non_goals)
    assert all(item["source_refs"] for item in requirements + non_goals)
    assert all(
        evidence["assertion"].strip()
        for item in requirements + non_goals
        for evidence in item["evidence"]
    )


def test_manifest_matches_the_frozen_authoritative_registry(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    registry = checker.CANONICAL_REQUIREMENTS
    assert list(registry) == [f"R-{number:03d}" for number in range(1, 78)]
    assert list(checker.AUTHORITATIVE_BEHAVIOR_KEYS) == list(registry)
    assert list(checker.AUTHORITATIVE_ACCEPTANCE_SHA256) == list(registry)

    for item in matrix["requirements"]:
        canonical = registry[item["id"]]
        assert item["behavior_key"] == canonical["behavior_key"]
        assert item["category"] == canonical["category"]
        assert item["kind"] == canonical["kind"]
        assert item["owning_stage"] == canonical["owning_stage"]
        assert (
            hashlib.sha256(item["acceptance"].encode("utf-8")).hexdigest()
            == (canonical["acceptance_sha256"])
        )
        assert {
            (ref["capability"], ref["requirement"], ref["scenario"])
            for ref in item["source_refs"]
        } == canonical["source_refs"]


def test_canonical_scenario_set_cannot_be_missing_added_or_duplicated(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    missing = copy.deepcopy(matrix)
    missing["requirements"][0]["source_refs"].pop()
    with pytest.raises(checker.ValidationError, match="authoritative reference set"):
        validate_without_collecting(checker, missing)

    added = copy.deepcopy(matrix)
    fabricated = copy.deepcopy(added["requirements"][0]["source_refs"][0])
    fabricated["scenario"] = "fabricated-scenario"
    added["requirements"][0]["source_refs"].append(fabricated)
    with pytest.raises(checker.ValidationError, match="authoritative reference set"):
        validate_without_collecting(checker, added)

    duplicated = copy.deepcopy(matrix)
    duplicated["requirements"][0]["source_refs"].append(
        copy.deepcopy(duplicated["requirements"][0]["source_refs"][0])
    )
    with pytest.raises(checker.ValidationError, match="duplicate canonical scenario"):
        validate_without_collecting(checker, duplicated)


def test_duplicate_evidence_records_are_rejected(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    changed = copy.deepcopy(matrix)
    duplicate = copy.deepcopy(changed["requirements"][0]["evidence"][0])
    duplicate["assertion"] = (
        "Changing prose must not disguise a duplicate evidence identity."
    )
    changed["requirements"][0]["evidence"].append(duplicate)

    with pytest.raises(checker.ValidationError, match="duplicate evidence record"):
        validate_without_collecting(checker, changed)


def test_manifest_matches_the_canonical_non_goal_registry(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    registry = checker.CANONICAL_NON_GOALS
    assert list(registry) == [f"N-{number:03d}" for number in range(1, 11)]
    for item in matrix["non_goals"]:
        canonical = registry[item["id"]]
        for field in ("category", "kind", "behavior_key", "owning_stage"):
            assert item[field] == canonical[field]
        assert (
            hashlib.sha256(item["acceptance"].encode("utf-8")).hexdigest()
            == (canonical["acceptance_sha256"])
        )
        assert {
            (ref["capability"], ref["requirement"], ref["scenario"])
            for ref in item["source_refs"]
        } == canonical["source_refs"]


@pytest.mark.parametrize("field", ["category", "behavior_key", "owning_stage"])
def test_non_goal_registry_fields_cannot_drift(
    checker: ModuleType,
    matrix: dict[str, object],
    field: str,
) -> None:
    changed = copy.deepcopy(matrix)
    replacements: dict[str, object] = {
        "category": "analysis",
        "behavior_key": "fabricated_non_goal_behavior",
        "owning_stage": 5,
    }
    changed["non_goals"][0][field] = replacements[field]

    with pytest.raises(checker.ValidationError, match="canonical non-goal registry"):
        validate_without_collecting(checker, changed)


@pytest.mark.parametrize("field", ["capability", "requirement", "scenario"])
def test_non_goal_source_binding_cannot_drift(
    checker: ModuleType,
    matrix: dict[str, object],
    field: str,
) -> None:
    changed = copy.deepcopy(matrix)
    changed["non_goals"][0]["source_refs"][0][field] = "fabricated-semantic-key"

    with pytest.raises(checker.ValidationError, match="canonical non-goal registry"):
        validate_without_collecting(checker, changed)


def test_fabricated_or_cross_requirement_source_refs_are_rejected(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    fabricated = copy.deepcopy(matrix)
    fabricated["requirements"][0]["source_refs"][0].update(
        {"capability": "fabricated-capability", "requirement": "fabricated-requirement"}
    )
    with pytest.raises(checker.ValidationError, match="authoritative reference set"):
        validate_without_collecting(checker, fabricated)

    wrong_scenario = copy.deepcopy(matrix)
    wrong_scenario["requirements"][0]["source_refs"][0]["scenario"] = (
        "fabricated-scenario"
    )
    with pytest.raises(checker.ValidationError, match="authoritative reference set"):
        validate_without_collecting(checker, wrong_scenario)


def test_requirement_evidence_does_not_overclaim_unproven_clauses(
    matrix: dict[str, object],
) -> None:
    by_id = {item["id"]: item for item in matrix["requirements"]}

    delivered = {
        "R-003": "tests/acceptance/test_full_user_journey.py::test_complete_no_network_application_journey",
        "R-021": "tests/acceptance/test_full_user_journey.py::test_demo_data_categories_and_missing_category_are_visible",
        "R-022": "tests/acceptance/test_full_user_journey.py::test_analysis_remains_independent",
    }
    for requirement_id, selector in delivered.items():
        evidence = next(
            item
            for item in by_id[requirement_id]["evidence"]
            if item.get("selector") == selector
        )
        assert evidence["state"] == "existing"

    still_planned = {
        "R-014": "tests/acceptance/test_backtest_scope_matrix.py::test_all_a_index_industry_custom_failure_and_insufficient_scopes",
        "R-040": "collects immutable formula scope period dates and costs and discloses T+1 suspension price limits and pool semantics",
        "R-052": "tests/acceptance/test_release_acceptance_scope.py::test_all_first_release_acceptance_domains_and_full_journey_are_gated",
    }
    for requirement_id, selector in still_planned.items():
        assert any(
            item["state"] == "planned" and item.get("selector") == selector
            for item in by_id[requirement_id]["evidence"]
        )


def test_reviewed_multiclause_rows_keep_clause_level_evidence(
    matrix: dict[str, object],
) -> None:
    by_id = {item["id"]: item for item in matrix["requirements"]}

    expected = {
        "R-018": {
            "tests/acceptance/test_backtest_semantics.py::test_runner_persists_complete_deferred_constraint_and_cancellation_chains": "existing",
            "tests/unit/backtest/test_trades.py::test_realized_net_return_discloses_each_cost": "existing",
        },
        "R-045": {
            "tests/unit/backtest/test_state_machine.py::test_duplicate_buy_is_ignored_when_already_holding": "existing",
            "tests/unit/backtest/test_state_machine.py::test_flat_sell_is_ignored": "existing",
            "tests/unit/backtest/test_state_machine.py::test_blocked_attempt_is_audited_without_resetting_pending_order": "existing",
            "tests/unit/backtest/test_state_machine.py::test_opposite_signal_cancels_pending_buy": "existing",
            "tests/unit/backtest/test_state_machine.py::test_fill_pending_buy_then_sell_updates_exactly_one_position": "existing",
        },
        "R-050": {
            "tests/security/test_secret_surfaces.py::test_market_token_never_leaves_masked_state_across_legacy_and_new_tasks": "existing",
            "tests/security/test_analysis_boundaries.py::test_real_worker_error_redacts_logs_task_http_and_report": "existing",
        },
    }
    for requirement_id, selectors in expected.items():
        actual = {
            evidence.get("selector"): evidence["state"]
            for evidence in by_id[requirement_id]["evidence"]
        }
        assert selectors.items() <= actual.items()

    responsive = {item.get("selector") for item in by_id["R-077"]["evidence"]}
    assert {
        "/market has bounded non-overlapping layout",
        "/formulas has bounded non-overlapping layout",
        "/backtests has bounded non-overlapping layout",
        "/analysis has bounded non-overlapping layout",
        "/tasks has bounded non-overlapping layout",
        "/settings has bounded non-overlapping layout",
        "navigation auto-collapses only when crossing the narrow breakpoint",
        "collapsed navigation renders icons without textual abbreviations",
    } <= responsive


def test_final_multiclause_audit_keeps_explicit_plans_for_unproven_groups(
    matrix: dict[str, object],
) -> None:
    by_id = {item["id"]: item for item in matrix["requirements"]}
    audited_plans = {
        "R-002": "tests/acceptance/test_market_period_adjustment_contract.py::test_period_and_adjustment_switches_recalculate_visible_market_and_indicator_values",
        "R-014": "tests/acceptance/test_backtest_scope_matrix.py::test_all_a_index_industry_custom_failure_and_insufficient_scopes",
        "R-029": "tests/acceptance/test_formula_editing_assistance.py::test_highlight_hints_templates_preview_save_and_copy",
        "R-031": "tests/acceptance/test_tdx_local_user_flow.py::test_valid_tdx_directory_shows_markets_period_and_data_cutoff",
        "R-033": "tests/acceptance/test_formula_safety_boundary.py::test_future_or_repainting_formula_cannot_be_saved_or_backtested",
        "R-039": "tests/acceptance/test_formula_validation_boundary.py::test_all_validation_stages_block_invalid_save_preview_and_backtest_while_preserving_draft",
        "R-052": "tests/acceptance/test_release_acceptance_scope.py::test_all_first_release_acceptance_domains_and_full_journey_are_gated",
        "R-066": "tests/acceptance/test_release_artifacts.py::test_release_history_contains_only_public_artifacts",
        "R-073": "tests/acceptance/test_release_docs.py::test_readmes_are_concise_reciprocal_and_install_verified",
    }
    for requirement_id, selector in audited_plans.items():
        assert any(
            evidence["state"] == "planned" and evidence.get("selector") == selector
            for evidence in by_id[requirement_id]["evidence"]
        ), requirement_id

    analysis_run = by_id["R-023"]["evidence"]
    assert any(
        evidence["state"] == "existing"
        and evidence.get("selector")
        == "configures a model and completes traceable analysis, retry, and insufficient flows"
        for evidence in analysis_run
    )


def test_release_mode_rejects_planned_and_incomplete_manual_evidence() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--mode", "release"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "planned evidence" in result.stderr
    assert "incomplete manual evidence" in result.stderr


def test_duplicate_yaml_keys_are_rejected(checker: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yml"
    path.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")

    with pytest.raises(checker.ValidationError, match="duplicate YAML key"):
        checker.load_manifest(path)


def test_yaml_aliases_are_rejected(checker: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "alias.yml"
    path.write_text("schema_version: &version 1\ncopy: *version\n", encoding="utf-8")

    with pytest.raises(checker.ValidationError, match="aliases are not allowed"):
        checker.load_manifest(path)


def test_yaml_anchor_without_alias_is_also_rejected(
    checker: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "anchor.yml"
    path.write_text("schema_version: &version 1\n", encoding="utf-8")

    with pytest.raises(checker.ValidationError, match="aliases are not allowed"):
        checker.load_manifest(path)


def test_bounded_yaml_loader_retains_safe_loader_invariant(checker: ModuleType) -> None:
    assert issubclass(checker.BoundedUniqueKeyLoader, checker.yaml.SafeLoader)


def test_yaml_size_depth_and_node_limits_are_enforced(
    checker: ModuleType, tmp_path: Path
) -> None:
    oversized = tmp_path / "oversized.yml"
    oversized.write_bytes(b"x" * (checker.MAX_YAML_BYTES + 1))
    with pytest.raises(checker.ValidationError, match="byte limit"):
        checker.load_manifest(oversized)

    deep = tmp_path / "deep.yml"
    deep.write_text("value: " + "[" * 30 + "0" + "]" * 30 + "\n", encoding="utf-8")
    with pytest.raises(checker.ValidationError, match="depth limit"):
        checker.load_manifest(deep)

    nodes = tmp_path / "nodes.yml"
    nodes.write_text(
        "values: [" + ",".join("0" for _ in range(checker.MAX_YAML_NODES + 1)) + "]\n",
        encoding="utf-8",
    )
    with pytest.raises(checker.ValidationError, match="node limit"):
        checker.load_manifest(nodes)


def test_schema_version_requires_an_exact_integer(
    checker: ModuleType, matrix: dict[str, object]
) -> None:
    changed = copy.deepcopy(matrix)
    changed["schema_version"] = 1.0

    with pytest.raises(checker.ValidationError, match="schema_version"):
        validate_without_collecting(checker, changed)


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("id", ["R-001"]),
        ("category", ["market"]),
        ("kind", {"value": "user_visible"}),
        ("status", ["mapped"]),
    ],
)
def test_item_scalar_fields_reject_containers_deterministically(
    checker: ModuleType,
    matrix: dict[str, object],
    field: str,
    malformed: object,
) -> None:
    changed = copy.deepcopy(matrix)
    changed["requirements"][0][field] = malformed

    with pytest.raises(checker.ValidationError, match=field):
        validate_without_collecting(checker, changed)


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("state", ["existing"]),
        ("runner", {"value": "pytest"}),
        ("kind", ["acceptance"]),
    ],
)
def test_evidence_enum_fields_reject_containers_deterministically(
    checker: ModuleType,
    matrix: dict[str, object],
    field: str,
    malformed: object,
) -> None:
    changed = copy.deepcopy(matrix)
    changed["requirements"][0]["evidence"][0][field] = malformed

    with pytest.raises(checker.ValidationError, match=field):
        validate_without_collecting(checker, changed)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update({"unexpected": "field"}), "unknown fields"),
        (lambda item: item.update({"owning_stage": True}), "owning_stage"),
        (lambda item: item.update({"acceptance": "x"}), "acceptance"),
        (lambda item: item.update({"status": "verified"}), "verified item"),
    ],
)
def test_exact_schema_and_status_strength_are_enforced(
    checker: ModuleType,
    matrix: dict[str, object],
    mutation: object,
    message: str,
) -> None:
    changed = copy.deepcopy(matrix)
    item = (
        next(
            requirement
            for requirement in changed["requirements"]
            if requirement["status"] == "mapped"
            and any(
                evidence["state"] == "planned" for evidence in requirement["evidence"]
            )
        )
        if message == "verified item"
        else changed["requirements"][0]
    )
    mutation(item)

    with pytest.raises(checker.ValidationError, match=message):
        validate_without_collecting(checker, changed)


@pytest.mark.parametrize(
    "unsafe",
    [
        "openspec/change/spec.md",
        "docs/superpowers/private.md",
        "outputs/report.txt",
        ".agents/state.json",
        ".codex/session.json",
        ".superpowers/notes.md",
        "work/scratch.md",
        "/Users/example",
        "/Users/example/private-project",
        "/root",
        "/root/private/session/requirements.yml",
        "/home/example",
        "/home/example/private/session/requirements.yml",
        "/private/var/folders/example/session/requirements.yml",
        "/var/folders/example/session/requirements.yml",
        "C:\\Users\\example",
        "C:\\Users\\example\\private\\session\\requirements.yml",
        "~/private-project",
    ],
)
def test_publication_boundary_strings_are_rejected_recursively(
    checker: ModuleType,
    matrix: dict[str, object],
    unsafe: str,
) -> None:
    changed = copy.deepcopy(matrix)
    changed["requirements"][0]["source_refs"][0]["scenario"] = unsafe

    with pytest.raises(checker.ValidationError, match="publication-boundary"):
        validate_without_collecting(checker, changed)


def test_source_refs_are_semantic_keys_not_paths(
    checker: ModuleType, matrix: dict[str, object]
) -> None:
    changed = copy.deepcopy(matrix)
    changed["requirements"][0]["source_refs"][0]["requirement"] = "docs/identity.md"

    with pytest.raises(checker.ValidationError, match="semantic key"):
        validate_without_collecting(checker, changed)


def test_bilingual_gate_requires_reciprocal_markdown_link_targets(
    checker: ModuleType,
    tmp_path: Path,
) -> None:
    english = tmp_path / "README.md"
    chinese = tmp_path / "README.zh-CN.md"
    english.write_text("README.zh-CN.md is available.\n", encoding="utf-8")
    chinese.write_text("README.md is available.\n", encoding="utf-8")

    with pytest.raises(checker.ValidationError, match="Markdown links"):
        checker._bilingual_readme_gate(tmp_path)

    english.write_text("![简体中文](README.zh-CN.md)\n", encoding="utf-8")
    chinese.write_text("![English](README.md)\n", encoding="utf-8")
    with pytest.raises(checker.ValidationError, match="Markdown links"):
        checker._bilingual_readme_gate(tmp_path)

    english.write_text(
        "```markdown\n[简体中文](README.zh-CN.md)\n```\n",
        encoding="utf-8",
    )
    chinese.write_text(
        "```markdown\n[English](README.md)\n```\n",
        encoding="utf-8",
    )
    with pytest.raises(checker.ValidationError, match="Markdown links"):
        checker._bilingual_readme_gate(tmp_path)

    english.write_text(
        "<!-- [简体中文](README.zh-CN.md) -->\n",
        encoding="utf-8",
    )
    chinese.write_text("<!-- [English](README.md) -->\n", encoding="utf-8")
    with pytest.raises(checker.ValidationError, match="Markdown links"):
        checker._bilingual_readme_gate(tmp_path)

    english.write_text("`[简体中文](README.zh-CN.md)`\n", encoding="utf-8")
    chinese.write_text("`[English](README.md)`\n", encoding="utf-8")
    with pytest.raises(checker.ValidationError, match="Markdown links"):
        checker._bilingual_readme_gate(tmp_path)

    english.write_text(r"\[简体中文](README.zh-CN.md)" "\n", encoding="utf-8")
    chinese.write_text(r"\[English](README.md)" "\n", encoding="utf-8")
    with pytest.raises(checker.ValidationError, match="Markdown links"):
        checker._bilingual_readme_gate(tmp_path)

    english.write_text("[简体中文](README.zh-CN.md)\n", encoding="utf-8")
    chinese.write_text("[English](README.md)\n", encoding="utf-8")
    checker._bilingual_readme_gate(tmp_path)


def test_broad_selectors_and_unregistered_gates_are_rejected(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    changed = copy.deepcopy(matrix)
    changed["requirements"][0]["evidence"][0]["selector"] = "tests/acceptance"
    with pytest.raises(checker.ValidationError, match="assertion-level selector"):
        validate_without_collecting(checker, changed)

    changed = copy.deepcopy(matrix)
    changed["requirements"][0]["evidence"] = [
        {
            "state": "existing",
            "runner": "gate",
            "kind": "gate",
            "gate_id": "shell:rm-rf",
            "assertion": "An arbitrary shell command must never be accepted as gate evidence.",
        }
    ]
    with pytest.raises(checker.ValidationError, match="registered gate_id"):
        validate_without_collecting(checker, changed)


def test_pytest_selectors_must_end_at_a_test_function(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    changed = copy.deepcopy(matrix)
    evidence = next(
        evidence
        for item in changed["requirements"]
        for evidence in item["evidence"]
        if evidence["runner"] == "pytest" and evidence["state"] == "existing"
    )
    evidence["selector"] = f"{evidence['path']}::TestRequirementCoverage"

    with pytest.raises(checker.ValidationError, match="function-level selector"):
        validate_without_collecting(checker, changed)


def test_evidence_runner_kind_combinations_are_compatible(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    changed = copy.deepcopy(matrix)
    gate = next(
        evidence
        for item in changed["requirements"]
        for evidence in item["evidence"]
        if evidence["runner"] == "gate"
    )
    gate["kind"] = "acceptance"
    with pytest.raises(checker.ValidationError, match="gate runner requires gate kind"):
        validate_without_collecting(checker, changed)

    changed = copy.deepcopy(matrix)
    pytest_evidence = next(
        evidence
        for item in changed["requirements"]
        for evidence in item["evidence"]
        if evidence["runner"] == "pytest"
    )
    pytest_evidence["kind"] = "gate"
    with pytest.raises(
        checker.ValidationError, match="pytest runner does not support gate kind"
    ):
        validate_without_collecting(checker, changed)


def test_user_visible_strength_requires_one_compatible_evidence_item(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    item = copy.deepcopy(matrix["requirements"][1])
    evidence = [
        {
            "state": "existing",
            "runner": "gate",
            "kind": "acceptance",
            "gate_id": "public-tree",
            "assertion": "The public tree gate cannot prove a user-visible product behavior.",
        },
        {
            "state": "existing",
            "runner": "pytest",
            "kind": "security",
            "path": "tests/acceptance/test_non_goal_inventory.py",
            "selector": "tests/acceptance/test_non_goal_inventory.py::test_every_non_goal_is_checked_on_every_declared_public_surface",
            "assertion": "A security-kind pytest item cannot combine with a separate gate kind.",
        },
    ]

    with pytest.raises(checker.ValidationError, match="user-visible behavior lacks"):
        checker._validate_evidence_strength(item, evidence)


def test_planned_evidence_is_an_exact_file_and_selector_pair(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    changed = copy.deepcopy(matrix)
    planned = next(
        evidence
        for item in changed["requirements"]
        for evidence in item["evidence"]
        if evidence["state"] == "planned" and evidence["runner"] == "pytest"
    )
    planned["path"] = "tests/acceptance"
    with pytest.raises(checker.ValidationError, match="file"):
        validate_without_collecting(checker, changed)

    changed = copy.deepcopy(matrix)
    planned = next(
        evidence
        for item in changed["requirements"]
        for evidence in item["evidence"]
        if evidence["state"] == "planned" and evidence["runner"] == "pytest"
    )
    planned["selector"] = "tests/acceptance/a_different_test.py::test_specific_behavior"
    with pytest.raises(checker.ValidationError, match="selector path"):
        validate_without_collecting(checker, changed)


def test_evidence_paths_reject_option_shaped_components(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    changed = copy.deepcopy(matrix)
    planned = next(
        evidence
        for item in changed["requirements"]
        for evidence in item["evidence"]
        if evidence["state"] == "planned" and evidence["runner"] == "playwright"
    )
    planned["path"] = "web/--config=untrusted.spec.ts"

    with pytest.raises(checker.ValidationError, match="option-shaped"):
        validate_without_collecting(checker, changed)


@pytest.mark.parametrize("state", ["existing", "planned"])
def test_evidence_paths_reject_symlinked_parent_escape(
    checker: ModuleType,
    matrix: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "future.py").write_text("def test_future(): pass\n", encoding="utf-8")
    parent_link = ROOT / "coverage-parent-link"
    parent_link.symlink_to(external, target_is_directory=True)
    try:
        changed = copy.deepcopy(matrix)
        evidence = next(
            entry
            for item in changed["requirements"]
            for entry in item["evidence"]
            if entry["runner"] == "pytest" and entry["state"] == state
        )
        path = f"{parent_link.name}/future.py"
        evidence["path"] = path
        evidence["selector"] = f"{path}::test_future"
        if state == "existing":
            tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
            monkeypatch.setattr(
                checker.subprocess,
                "check_output",
                lambda *_args, **_kwargs: tracked + path.encode() + b"\0",
            )

        with pytest.raises(checker.ValidationError, match="symlinked path component"):
            validate_without_collecting(checker, changed)
    finally:
        parent_link.unlink()


def test_git_tracked_paths_are_loaded_once_per_validation(
    checker: ModuleType,
    matrix: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    calls = 0

    def counted(*_args: object, **_kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        return tracked

    monkeypatch.setattr(checker.subprocess, "check_output", counted)
    validate_without_collecting(checker, copy.deepcopy(matrix))

    assert calls == 1


def test_frontend_listings_require_an_exact_test_title(checker: ModuleType) -> None:
    vitest = "src/feature.test.tsx > nested describe > complete behavior title\n"
    playwright = "  [chromium] › feature.spec.ts:12:1 › complete browser title\n"

    assert checker.listed_test_titles("vitest", vitest) == {"complete behavior title"}
    assert checker.listed_test_titles("playwright", playwright) == {
        "complete browser title"
    }
    assert "complete" not in checker.listed_test_titles("vitest", vitest)


@pytest.mark.parametrize("runner", ["pytest", "vitest", "playwright"])
def test_selector_collection_timeout_is_bounded_and_deterministic(
    checker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    runner: str,
) -> None:
    path = {
        "pytest": "tests/unit/test_requirement_coverage.py",
        "vitest": "web/src/app/App.test.tsx",
        "playwright": "web/e2e/foundation.spec.ts",
    }[runner]
    selector = {
        "pytest": f"{path}::test_frontend_listings_require_an_exact_test_title",
        "vitest": "shows the product identity and all primary navigation items",
        "playwright": "fresh user sees the live foundation shell and completed demo task",
    }[runner]
    item = {
        "evidence": [
            {
                "state": "existing",
                "runner": runner,
                "path": path,
                "selector": selector,
            }
        ]
    }

    def timeout(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr(checker.subprocess, "run", timeout)
    with pytest.raises(
        checker.ValidationError, match=f"{runner} selector collection timed out"
    ):
        checker._collect_existing_selectors([item], ROOT)


def test_non_goal_inventory_catches_normalized_synonyms() -> None:
    inventory = load_non_goal_inventory()
    exposed = inventory.find_non_goal_exposures(
        " ".join(
            (
                "broker_order shared_cash_portfolio order_book_depth position_percentage",
                "native_desktop_ui login stock_screener condition_selection chart_drawing",
                "formula_generation_ai",
            )
        ),
        claims=False,
    )

    assert exposed == {f"N-{number:03d}" for number in range(1, 11)}


@pytest.mark.parametrize(
    ("claim", "non_goal_id"),
    [
        ("personalized investment recommendation", "N-004"),
        ("Electron app", "N-005"),
        ("Tauri app", "N-005"),
        ("multi-user account", "N-006"),
        ("organization", "N-006"),
        ("payment", "N-006"),
        ("invoicing", "N-006"),
        ("rules builder", "N-007"),
        ("prompt-based formula authoring", "N-010"),
        ("automatic formula repair", "N-010"),
        ("formula explanation", "N-010"),
    ],
)
def test_non_goal_inventory_catches_public_wording_variants(
    claim: str,
    non_goal_id: str,
) -> None:
    inventory = load_non_goal_inventory()

    assert inventory.find_non_goal_exposures(claim, claims=True) == {non_goal_id}


def test_non_goal_claim_inventory_handles_mixed_positive_and_negative_statements() -> (
    None
):
    inventory = load_non_goal_inventory()
    text = (
        "No broker order or live trading is provided, but a stock screener is available. "
        "The browser launcher opens the Web UI without a native desktop UI; "
        "formula_generation_ai is supported."
    )

    assert inventory.find_non_goal_exposures(text, claims=True) == {"N-007", "N-010"}


def test_non_goal_claim_inventory_scopes_negation_to_the_matching_claim() -> None:
    inventory = load_non_goal_inventory()

    assert inventory.find_non_goal_exposures(
        "No broker order is provided and a stock screener is available",
        claims=True,
    ) == {"N-007"}


def test_non_goal_claim_inventory_handles_coordinated_positive_verb() -> None:
    inventory = load_non_goal_inventory()

    assert inventory.find_non_goal_exposures(
        "No broker order is provided and the application supports a stock screener",
        claims=True,
    ) == {"N-007"}


def test_non_goal_claim_inventory_understands_common_negative_contractions() -> None:
    inventory = load_non_goal_inventory()

    assert (
        inventory.find_non_goal_exposures(
            "The application doesn't include a stock screener",
            claims=True,
        )
        == set()
    )


@pytest.mark.parametrize(
    "claim",
    [
        "The stock screener is unavailable",
        "The stock screener is disabled",
        "The stock screener is prohibited",
        "Neither broker order nor a stock screener is supported",
    ],
)
def test_non_goal_claim_inventory_understands_explicit_absence_states(
    claim: str,
) -> None:
    inventory = load_non_goal_inventory()

    assert inventory.find_non_goal_exposures(claim, claims=True) == set()


def test_non_goal_inventory_allows_browser_launcher_and_explicit_new_absences() -> None:
    inventory = load_non_goal_inventory()

    assert (
        inventory.find_non_goal_exposures(
            "The browser launcher is available. No Tauri app, automatic formula repair, "
            "or formula explanation is provided.",
            claims=True,
        )
        == set()
    )


def test_openapi_inventory_recurses_through_nested_keys_and_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = load_non_goal_inventory()
    schema = {
        "paths": {"/features": {"get": {"operationId": "list_features"}}},
        "components": {
            "schemas": {
                "Feature": {
                    "type": "object",
                    "properties": {
                        "stock_screener": {
                            "type": "string",
                            "enum": ["dynamic_screening"],
                        }
                    },
                }
            }
        },
    }

    class FakeApp:
        @staticmethod
        def openapi() -> dict[str, object]:
            return schema

    monkeypatch.setattr(inventory, "create_app", FakeApp)
    exposed = inventory.find_non_goal_exposures(
        inventory._openapi_inventory(),
        claims=False,
    )

    assert exposed == {"N-007"}


def test_openapi_inventory_separates_structure_from_free_text_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = load_non_goal_inventory()

    class FakeApp:
        schema: dict[str, object] = {}

        @classmethod
        def openapi(cls) -> dict[str, object]:
            return cls.schema

    monkeypatch.setattr(inventory, "create_app", FakeApp)

    def scan(schema: dict[str, object]) -> tuple[set[str], set[str]]:
        FakeApp.schema = schema
        split = getattr(
            inventory,
            "_openapi_inventories",
            lambda: (inventory._openapi_inventory(), ""),
        )
        structural, claims = split()
        return (
            inventory.find_non_goal_exposures(structural, claims=False),
            inventory.find_non_goal_exposures(claims, claims=True),
        )

    assert scan(
        {
            "components": {
                "schemas": {
                    "Feature": {
                        "properties": {"stock_screener": {"type": "boolean"}},
                        "description": "This API does not support a stock screener",
                    }
                }
            }
        }
    ) == ({"N-007"}, set())
    assert scan(
        {"info": {"description": "This API does not support a stock screener"}}
    ) == (set(), set())
    assert scan({"info": {"description": "This API supports a stock screener"}}) == (
        set(),
        {"N-007"},
    )


def test_non_goal_git_paths_use_filesystem_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = load_non_goal_inventory()
    raw = b"docs/\xff.md"
    monkeypatch.setattr(
        inventory.subprocess,
        "check_output",
        lambda *_args, **_kwargs: raw + b"\0",
    )

    assert inventory._tracked_repo_paths(ROOT) == frozenset({os.fsdecode(raw)})


def test_ui_inventory_excludes_test_story_and_fixture_sources(tmp_path: Path) -> None:
    inventory = load_non_goal_inventory()
    source_root = tmp_path / "src"
    fixture = source_root / "features" / "fixtures" / "claims.ts"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "export const capability = 'stock_screener';\n", encoding="utf-8"
    )
    for name in ("Feature.test.tsx", "Feature.spec.ts", "Feature.stories.tsx"):
        (source_root / name).write_text(
            "export const capability = 'stock_screener';\n",
            encoding="utf-8",
        )
    shipped = source_root / "Feature.tsx"
    shipped.write_text("export const capability = 'market_chart';\n", encoding="utf-8")
    discover = getattr(
        inventory,
        "_public_ui_source_paths",
        lambda root: tuple(root.rglob("*.ts")) + tuple(root.rglob("*.tsx")),
    )
    paths = discover(source_root)

    assert paths == (shipped,)
    assert (
        inventory.find_non_goal_exposures(
            inventory._source_inventory(paths),
            claims=False,
        )
        == set()
    )


def test_nested_tracked_public_docs_are_inventoried(tmp_path: Path) -> None:
    inventory = load_non_goal_inventory()
    nested = tmp_path / "docs" / "guides" / "feature.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("The application supports a stock screener.\n", encoding="utf-8")
    internal = tmp_path / "docs" / "superpowers" / "private.md"
    internal.parent.mkdir(parents=True)
    internal.write_text(
        "The application supports a stock screener.\n", encoding="utf-8"
    )
    discover = getattr(inventory, "_public_doc_paths", lambda *_args: ())
    paths = discover(
        tmp_path,
        {"docs/guides/feature.md", "docs/superpowers/private.md"},
    )

    assert paths == (nested,)
    assert inventory.find_non_goal_exposures(
        inventory._source_inventory(paths),
        claims=True,
    ) == {"N-007"}


def test_existing_evidence_must_be_tracked_regular_file_without_symlinks(
    checker: ModuleType,
    matrix: dict[str, object],
    tmp_path: Path,
) -> None:
    changed = copy.deepcopy(matrix)
    changed["requirements"][0]["evidence"][0]["path"] = "not-tracked.py"
    with pytest.raises(checker.ValidationError, match="tracked regular file"):
        validate_without_collecting(checker, changed)

    target = tmp_path / "target.py"
    target.write_text("def test_target(): pass\n", encoding="utf-8")
    link = ROOT / "untracked-coverage-link.py"
    link.symlink_to(target)
    try:
        changed["requirements"][0]["evidence"][0]["path"] = link.name
        with pytest.raises(checker.ValidationError, match="symlink"):
            validate_without_collecting(checker, changed)
    finally:
        link.unlink()


def test_manual_evidence_has_a_final_artifact_contract(
    checker: ModuleType,
    matrix: dict[str, object],
) -> None:
    changed = copy.deepcopy(matrix)
    manual = next(
        evidence
        for item in changed["requirements"]
        for evidence in item["evidence"]
        if evidence["state"] == "manual"
    )
    manual.pop("final_artifact_contract")

    with pytest.raises(checker.ValidationError, match="manual evidence fields"):
        validate_without_collecting(checker, changed)


def test_docs_digest_matches_manifest(checker: ModuleType) -> None:
    checker.verify_document_digest(MANIFEST, DOC)
    expected = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    assert f"requirements-yaml-sha256: {expected}" in DOC.read_text(encoding="utf-8")


def test_docs_digest_detects_drift(checker: ModuleType, tmp_path: Path) -> None:
    changed_manifest = tmp_path / "requirements.yml"
    changed_manifest.write_bytes(MANIFEST.read_bytes() + b"\n")

    with pytest.raises(checker.ValidationError, match="digest"):
        checker.verify_document_digest(changed_manifest, DOC)
