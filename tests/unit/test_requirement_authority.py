from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_requirement_coverage.py"
MANIFEST = ROOT / "tests" / "acceptance" / "requirements.yml"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("requirement_authority", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stable_ids_are_bound_to_the_authoritative_acceptance_meaning() -> None:
    checker = _load_checker()
    matrix = checker.load_manifest(MANIFEST)
    requirements = matrix["requirements"]

    assert [item["id"] for item in requirements] == [
        f"R-{number:03d}" for number in range(1, 78)
    ]
    assert {item["id"]: item["behavior_key"] for item in requirements} == (
        checker.AUTHORITATIVE_BEHAVIOR_KEYS
    )
    assert {
        item["id"]: hashlib.sha256(item["acceptance"].encode("utf-8")).hexdigest()
        for item in requirements
    } == checker.AUTHORITATIVE_ACCEPTANCE_SHA256

    assert checker.AUTHORITATIVE_BEHAVIOR_KEYS["R-001"] == "a_share_analysis_focus"
    assert (
        checker.AUTHORITATIVE_BEHAVIOR_KEYS["R-006"]
        == "confirmed_specs_are_hosted_in_openspec"
    )
    assert (
        checker.AUTHORITATIVE_BEHAVIOR_KEYS["R-077"]
        == "responsive_ui_across_screen_ratios"
    )


def test_non_goal_ids_keep_the_authoritative_exclusion_order() -> None:
    checker = _load_checker()
    matrix = checker.load_manifest(MANIFEST)

    assert {
        item["id"]: item["behavior_key"] for item in matrix["non_goals"]
    } == checker.AUTHORITATIVE_NON_GOAL_BEHAVIOR_KEYS
    assert checker.AUTHORITATIVE_NON_GOAL_BEHAVIOR_KEYS["N-007"] == (
        "no_drawing_multistock_or_linked_periods"
    )
    assert checker.AUTHORITATIVE_NON_GOAL_BEHAVIOR_KEYS["N-009"] == (
        "no_dynamic_market_screening"
    )
