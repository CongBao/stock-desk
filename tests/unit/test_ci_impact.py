from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_impact import (
    ALL_GATES,
    BACKEND_PROFILE,
    DOCS_PROFILE,
    FULL_PROFILE,
    RELEASE_DESKTOP_PROFILE,
    WEB_PROFILE,
    changed_files_between,
    classify_impact,
    main,
    unclassified_tracked_paths,
)


@pytest.mark.parametrize(
    ("path", "profile", "domain", "expected_gate"),
    [
        ("src/stock_desk/api/main.py", BACKEND_PROFILE, "backend", "python-unit"),
        ("web/src/App.tsx", WEB_PROFILE, "web", "e2e"),
        ("src-tauri/src/main.rs", RELEASE_DESKTOP_PROFILE, "tauri", "rust"),
        (
            "packaging/windows/stock-desk.iss",
            RELEASE_DESKTOP_PROFILE,
            "installer",
            "artifact-proof",
        ),
        (
            "scripts/build_windows_desktop.py",
            RELEASE_DESKTOP_PROFILE,
            "installer",
            "artifact-proof",
        ),
        (
            "src/stock_desk/market/lake.py",
            RELEASE_DESKTOP_PROFILE,
            "windows-storage",
            "artifact-proof",
        ),
        (
            "tests/integration/market/test_sqlite_market_lake.py",
            RELEASE_DESKTOP_PROFILE,
            "windows-storage",
            "artifact-proof",
        ),
        (
            "tests/unit/test_verify_windows_desktop_bundle.py",
            RELEASE_DESKTOP_PROFILE,
            "installer",
            "artifact-proof",
        ),
        ("README.md", DOCS_PROFILE, "documentation", "docs"),
    ],
)
def test_single_domain_paths_select_explicit_gates(
    path: str, profile: str, domain: str, expected_gate: str
) -> None:
    impact = classify_impact("pull_request", [path])

    assert impact.profile == profile
    assert impact.full is False
    assert impact.domains == (domain,)
    assert expected_gate in impact.required_jobs
    assert set(impact.required_jobs).isdisjoint(impact.skipped_jobs)
    assert set(impact.required_jobs) | set(impact.skipped_jobs) == set(ALL_GATES)


def test_documentation_can_accompany_one_functional_domain() -> None:
    impact = classify_impact(
        "pull_request", ["src/stock_desk/desktop.py", "docs/architecture.md"]
    )

    assert impact.profile == BACKEND_PROFILE
    assert impact.full is False
    assert impact.domains == ("backend", "documentation")
    assert impact.reason == "explicit-backend-only"


@pytest.mark.parametrize(
    "paths",
    [
        ["src/stock_desk/api/main.py", "web/src/App.tsx"],
        ["web/src/App.tsx", "src-tauri/src/main.rs"],
        ["src/stock_desk/desktop.py", "packaging/windows/stock-desk.iss"],
    ],
)
def test_cross_domain_changes_fail_closed_to_full(paths: list[str]) -> None:
    impact = classify_impact("pull_request", paths)

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.reason == "cross-domain-change"
    assert impact.required_jobs == ALL_GATES
    assert impact.skipped_jobs == ()


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        "uv.lock",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "rust-toolchain.toml",
        "src-tauri/Cargo.lock",
        "src-tauri/Cargo.toml",
        "package.json",
        "Makefile",
        "Dockerfile",
        "compose.yaml",
        "scripts/security_scan.py",
        "scripts/verify_release.py",
        "scripts/main_validation_proof.py",
        "scripts/artifact_manifest.py",
        "scripts/verify_ci_cache_policy.py",
        "config/release-tag-allowed-signers",
        "config/release-auditor-public-key.pem",
        "config/desktop-network-privacy.json",
        "config/tauri-updater-runtime.json",
        "tests/unit/test_ci_impact.py",
        "tests/unit/test_artifact_manifest.py",
    ],
)
def test_workflow_dependency_permission_signing_and_proof_paths_are_full(
    path: str,
) -> None:
    impact = classify_impact("pull_request", [path])

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.reason == f"high-risk-path:{path}"


@pytest.mark.parametrize(
    ("path", "domain"),
    [
        ("uv.lock", "dependency"),
        (".github/workflows/ci.yml", "delivery"),
        (".github/CODEOWNERS", "permissions"),
        ("config/desktop-network-privacy.json", "delivery"),
        ("config/tauri-updater-runtime.json", "delivery"),
        ("config/release-tag-allowed-signers", "signing"),
        ("config/release-auditor-public-key.pem", "signing"),
        ("scripts/signpath_contract.py", "signing"),
    ],
)
def test_high_risk_paths_keep_an_auditable_domain(path: str, domain: str) -> None:
    impact = classify_impact("pull_request", [path])

    assert impact.domains == (domain,)
    assert impact.required_jobs == ALL_GATES


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/windows-installed.yml",
        "scripts/verify_windows_installed_evidence.py",
        "scripts/windows_installed_environment_policy.py",
        "scripts/windows_installed_vm_harness.ps1",
    ],
)
def test_windows_installed_controller_paths_are_high_risk_installer_inputs(
    path: str,
) -> None:
    impact = classify_impact("pull_request", [path])

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.domains == ("installer",)
    assert impact.reason == f"high-risk-path:{path}"
    assert impact.required_jobs == ALL_GATES


@pytest.mark.parametrize(
    "path",
    [
        "scripts/capture_windows_desktop_evidence.ps1",
        "scripts/windows_desktop_webview_evidence.mjs",
        "scripts/windows_packaged_backtest_evidence.mjs",
        "scripts/prepare_windows_packaged_backtest_evidence.py",
        "scripts/capture_packaged_backtest_semantics.py",
        "scripts/verify_packaged_backtest_evidence.py",
        "schemas/packaged-backtest-evidence-v1.schema.json",
        "schemas/packaged-backtest-host-observation-v1.schema.json",
        "schemas/windows-packaged-backtest-promotion-v1.schema.json",
        "tests/fixtures/backtest/v1_0_oracle.json",
        "tests/fixtures/backtest/v1_0_oracle_inputs.json",
        "scripts/v1_backtest_oracle.py",
        "scripts/main_validation_proof.py",
    ],
)
def test_packaged_backtest_proof_chain_paths_require_full_artifact_proof(
    path: str,
) -> None:
    impact = classify_impact("pull_request", [path])

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.domains == ("delivery",)
    assert impact.reason == f"high-risk-path:{path}"
    assert "artifact-proof" in impact.required_jobs
    assert impact.required_jobs == ALL_GATES
    assert impact.skipped_jobs == ()


def test_one_unknown_path_makes_an_otherwise_targeted_change_full() -> None:
    impact = classify_impact(
        "pull_request", ["README.md", "src/stock_desk/desktop.py", "new-file.txt"]
    )

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.reason == "unclassified-path:new-file.txt"


@pytest.mark.parametrize("paths", [[], ["README.md"], ["src/stock_desk/desktop.py"]])
def test_every_push_is_full(paths: list[str]) -> None:
    impact = classify_impact("push", paths)

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.reason == "push-events-require-full"
    assert impact.required_jobs == ALL_GATES


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule", "merge_group", ""])
def test_unknown_events_are_full(event: str) -> None:
    impact = classify_impact(event, ["README.md"])

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.reason == "unsupported-event"


@pytest.mark.parametrize("path", ["", "../README.md", "/README.md", "docs\\README.md"])
def test_invalid_paths_are_full(path: str) -> None:
    impact = classify_impact("pull_request", [path])

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.reason == "invalid-or-empty-path"


def test_empty_pull_request_diff_is_full() -> None:
    impact = classify_impact("pull_request", [])

    assert impact.full is True
    assert impact.reason == "empty-change-set"


def test_fork_pull_request_is_full() -> None:
    impact = classify_impact("pull_request", ["README.md"], fork_pull_request=True)

    assert impact.full is True
    assert impact.reason == "fork-pull-request-requires-full"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"base_reachable": False}, "unreachable-base-sha"),
        (
            {"base_sha": "a" * 40, "expected_base_sha": "b" * 40},
            "stale-base-sha",
        ),
    ],
)
def test_untrusted_or_stale_base_is_full(
    kwargs: dict[str, object], reason: str
) -> None:
    impact = classify_impact("pull_request", ["README.md"], **kwargs)  # type: ignore[arg-type]

    assert impact.full is True
    assert impact.reason == reason


def test_paths_are_deduplicated_and_sorted() -> None:
    impact = classify_impact("pull_request", ["README.md", "docs/z.md", "README.md"])

    assert impact.changed_files == ("README.md", "docs/z.md")


def test_every_current_tracked_path_has_a_risk_owner() -> None:
    repo_root = Path(__file__).parents[2]

    assert unclassified_tracked_paths(repo_root) == ()


def test_changed_files_between_reads_nul_delimited_git_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    first = tmp_path / "README.md"
    first.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "first"], check=True)
    base = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    first.write_text("two\n", encoding="utf-8")
    spaced = tmp_path / "docs" / "spaced name.md"
    spaced.parent.mkdir()
    spaced.write_text("docs\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "second"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()

    assert changed_files_between(tmp_path, base, head) == (
        "README.md",
        "docs/spaced name.md",
    )


def test_cli_writes_auditable_stdout_and_github_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github_output = tmp_path / "github-output"

    result = main(
        [
            "--event-name",
            "pull_request",
            "--changed-file",
            "README.md",
            "--github-output",
            str(github_output),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert github_output.read_text(encoding="utf-8") == output
    fields = dict(line.split("=", 1) for line in output.splitlines())
    assert fields["profile"] == "docs-only"
    assert fields["full"] == "false"
    assert fields["reason"] == "explicit-docs-only"
    assert json.loads(fields["domains"]) == ["documentation"]
    assert json.loads(fields["required_jobs"]) == [
        "change-policy",
        "public-tree",
        "docs",
    ]
    assert "python-unit" in json.loads(fields["skipped_jobs"])


def test_cli_missing_change_source_is_full(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["--event-name", "pull_request"])

    assert result == 0
    fields = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert fields["profile"] == "full"
    assert fields["full"] == "true"
    assert fields["reason"] == "missing-change-source"


def test_script_cli_can_be_invoked_directly() -> None:
    script = Path(__file__).parents[2] / "scripts" / "ci_impact.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--event-name",
            "pull_request",
            "--changed-file",
            "src/stock_desk/desktop.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert fields["profile"] == "backend"
    assert fields["reason"] == "explicit-backend-only"
