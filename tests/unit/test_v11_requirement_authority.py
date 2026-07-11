from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from scripts import check_requirement_coverage as checker


ROOT = Path(__file__).resolve().parents[2]
V1_MANIFEST = ROOT / "tests" / "acceptance" / "requirements.yml"
V11_MANIFEST = ROOT / "tests" / "acceptance" / "v1_1_requirements.yml"


def test_v11_authority_uses_a_disjoint_frozen_namespace() -> None:
    v1 = checker.load_manifest(V1_MANIFEST)
    v11 = checker.load_manifest(V11_MANIFEST)

    assert [item["id"] for item in v11["requirements"]] == [
        "V11-R-001",
        "V11-R-002",
    ]
    assert not (
        {item["id"] for item in v1["requirements"]}
        & {item["id"] for item in v11["requirements"]}
    )
    assert {item["id"]: item["behavior_key"] for item in v11["requirements"]} == (
        checker.V11_AUTHORITATIVE_BEHAVIOR_KEYS
    )
    assert {
        item["id"]: hashlib.sha256(item["acceptance"].encode()).hexdigest()
        for item in v11["requirements"]
    } == checker.V11_AUTHORITATIVE_ACCEPTANCE_SHA256


def test_all_authorities_validate_together_and_reject_cross_namespace_semantics() -> (
    None
):
    counts = checker.validate_all_manifests(
        repo_root=ROOT,
        mode="mapping",
        verify_selectors=False,
    )
    assert counts == {
        "v1_requirements": 82,
        "v1_non_goals": 10,
        "v11_requirements": 2,
        "planned": 0,
        "manual": 20,
    }

    v11 = checker.load_manifest(V11_MANIFEST)
    changed = copy.deepcopy(v11)
    changed["requirements"][0]["behavior_key"] = "a_share_analysis_focus"
    with pytest.raises(checker.ValidationError, match="authorities.*behavior_key"):
        checker._validate_cross_authority_uniqueness(
            {"v1": checker.load_manifest(V1_MANIFEST), "v1.1": changed}
        )


def test_v11_pre_publish_accepts_only_delivered_selectors() -> None:
    counts = checker.validate_all_manifests(
        repo_root=ROOT,
        mode="pre-publish",
        verify_selectors=False,
    )
    assert counts["v11_requirements"] == 2
    assert counts["planned"] == 0


def test_v11_authority_rejects_meaning_or_id_drift() -> None:
    manifest = checker.load_manifest(V11_MANIFEST)
    changed = copy.deepcopy(manifest)
    changed["requirements"][0]["acceptance"] += " Drift."
    with pytest.raises(checker.ValidationError, match="authoritative meaning"):
        checker.validate_v11_manifest(
            changed, repo_root=ROOT, mode="mapping", verify_selectors=False
        )

    missing = copy.deepcopy(manifest)
    missing["requirements"].pop()
    with pytest.raises(
        checker.ValidationError, match="exactly V11-R-001 through V11-R-002"
    ):
        checker.validate_v11_manifest(
            missing, repo_root=ROOT, mode="mapping", verify_selectors=False
        )


def test_main_ci_aggregates_and_hashes_both_requirement_authorities() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "--manifest tests/acceptance/requirements.yml" in workflow
    assert "--manifest tests/acceptance/v1_1_requirements.yml" in workflow
    assert (
        '--critical-input "v1.1-requirements=$(sha256sum '
        "tests/acceptance/v1_1_requirements.yml"
    ) in workflow
