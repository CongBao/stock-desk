from __future__ import annotations

from pathlib import Path

from scripts.ci_impact import ALL_GATES, classify_impact
from scripts.main_validation_proof import CRITICAL_INPUTS


ROOT = Path(__file__).resolve().parents[2]

FORMAL_RELEASE_INPUTS = {
    ".github/workflows/signpath.yml",
    ".github/workflows/release.yml",
    ".github/workflows/windows-installed.yml",
    "schemas/deployment-latency-ledger-v1.schema.json",
    "schemas/deployment-latency-report-v1.schema.json",
    "schemas/deployment-latency-sample-v1.schema.json",
    "schemas/deployment-latency-seal-v1.schema.json",
    "scripts/deployment_latency.py",
    "scripts/signpath_contract.py",
    "scripts/trusted_updater_release.py",
}


def test_formal_release_control_plane_is_bound_into_the_main_proof() -> None:
    assert FORMAL_RELEASE_INPUTS <= set(CRITICAL_INPUTS)
    assert all((ROOT / path).is_file() for path in FORMAL_RELEASE_INPUTS)


def test_formal_release_and_latency_paths_always_select_full_pr_gates() -> None:
    expected_domains = {
        ".github/workflows/signpath.yml": "delivery",
        "scripts/signpath_contract.py": "signing",
        "scripts/deployment_latency.py": "delivery",
        "schemas/deployment-latency-ledger-v1.schema.json": "delivery",
    }

    for path, domain in expected_domains.items():
        impact = classify_impact("pull_request", [path])
        assert impact.full is True
        assert impact.required_jobs == ALL_GATES
        assert impact.domains == (domain,)
        assert impact.reason == f"high-risk-path:{path}"


def test_bilingual_docs_disclose_hard_disabled_formal_scaffold() -> None:
    chinese = (ROOT / "README.md").read_text(encoding="utf-8")
    english = (ROOT / "README.en.md").read_text(encoding="utf-8")
    signing = (ROOT / "docs" / "code-signing-policy.md").read_text(encoding="utf-8")
    ci = (ROOT / "docs" / "ci.md").read_text(encoding="utf-8")

    assert "正式签名发布控制面" in chinese
    assert "production updater 仍保持关闭" in chinese
    assert "hard-disabled formal" in english
    assert "production updater remains disabled" in english
    assert "application-submitted / pending-review" in signing
    assert "硬禁用骨架" in signing
    assert "hard-disabled scaffold" in signing
    assert "连续五次" in ci
    assert "five consecutive" in ci
    assert "incomplete" in ci
