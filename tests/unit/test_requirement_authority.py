from __future__ import annotations

import hashlib
import importlib.util
import re
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


def test_public_requirement_artifacts_do_not_name_operator_specific_paths_or_keys() -> (
    None
):
    private_key_basename = re.compile(
        r"\bid_(?:ed25519|rsa|ecdsa)(?:_[A-Za-z0-9.-]+)?\b", re.IGNORECASE
    )
    canonical_checkout = re.compile(
        r"(?:~|/Users/[^/]+)/Workspace/stock[-_]desk", re.IGNORECASE
    )
    public_artifacts = [MANIFEST, *sorted((ROOT / "tests").rglob("*.py"))]

    for artifact in public_artifacts:
        content = artifact.read_text(encoding="utf-8")
        assert private_key_basename.search(content) is None, artifact
        assert canonical_checkout.search(content) is None, artifact


def test_market_provenance_and_schedule_require_exact_delivered_evidence() -> None:
    checker = _load_checker()
    matrix = checker.load_manifest(MANIFEST)
    by_id = {item["id"]: item for item in matrix["requirements"]}

    provenance = {
        item.get("selector"): item["state"] for item in by_id["R-036"]["evidence"]
    }
    assert (
        provenance[
            "tests/contract/providers/test_provider_contract.py::"
            "test_bar_contract_normalizes_schema_units_order_and_provenance"
        ]
        == "existing"
    )
    assert (
        provenance[
            "tests/unit/market/test_provenance_hashes.py::"
            "test_manifest_preserves_upstream_provenance_but_excludes_fetched_at_from_hash"
        ]
        == "existing"
    )

    schedule = {
        item.get("selector"): item["state"] for item in by_id["R-042"]["evidence"]
    }
    assert (
        schedule[
            "tests/acceptance/test_market_flow.py::"
            "test_settings_route_worker_cache_api_and_schedule_flow_without_network"
        ]
        == "existing"
    )
