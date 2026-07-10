from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_impact import (
    DOCS_PROFILE,
    FULL_PROFILE,
    RELEASE_DESKTOP_PROFILE,
    changed_files_between,
    classify_impact,
    main,
)


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md", "docs/architecture.md"],
        ["CHANGELOG.md", "SECURITY.md"],
    ],
)
def test_pull_request_allows_only_explicit_documentation(paths: list[str]) -> None:
    impact = classify_impact("pull_request", paths)

    assert impact.profile == DOCS_PROFILE
    assert impact.full is False
    assert impact.reason == "explicit-docs-only"


def test_pull_request_allows_explicit_release_desktop_scope_with_docs() -> None:
    impact = classify_impact(
        "pull_request",
        [
            "src/stock_desk/desktop.py",
            "packaging/windows/stock-desk.iss",
            "tests/unit/test_installer_scripts.py",
            "docs/installation-windows.md",
        ],
    )

    assert impact.profile == RELEASE_DESKTOP_PROFILE
    assert impact.full is False


def test_windows_first_start_fix_uses_targeted_release_desktop_scope() -> None:
    impact = classify_impact(
        "pull_request",
        [
            ".github/workflows/ci.yml",
            "scripts/ci_impact.py",
            "src/stock_desk/storage/backup.py",
            "tests/integration/storage/test_restore_recovery.py",
            "tests/unit/storage/test_backup.py",
            "tests/unit/test_ci_impact.py",
        ],
    )

    assert impact.profile == RELEASE_DESKTOP_PROFILE
    assert impact.full is False


def test_pre_publish_timeout_budget_uses_targeted_release_desktop_scope() -> None:
    impact = classify_impact(
        "pull_request",
        [
            ".github/workflows/ci.yml",
            "scripts/check_requirement_coverage.py",
            "scripts/ci_impact.py",
            "tests/unit/test_ci_impact.py",
            "tests/unit/test_requirement_coverage.py",
        ],
    )

    assert impact.profile == RELEASE_DESKTOP_PROFILE
    assert impact.full is False


def test_current_release_proof_change_set_uses_targeted_profile() -> None:
    impact = classify_impact(
        "pull_request",
        [
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            "scripts/ci_impact.py",
            "scripts/main_validation_proof.py",
            "scripts/verify_installed_app.py",
            "src/stock_desk/desktop.py",
            "tests/acceptance/test_installed_distribution.py",
            "tests/acceptance/test_release_artifacts.py",
            "tests/integration/test_windows_runtime_acl.py",
            "tests/unit/test_ci_impact.py",
            "tests/unit/test_desktop_launcher.py",
            "tests/unit/test_installer_scripts.py",
            "tests/unit/test_main_validation_proof.py",
            "tests/unit/test_repository_health.py",
        ],
    )

    assert impact.profile == RELEASE_DESKTOP_PROFILE
    assert impact.full is False


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/codeql.yml",
        ".github/workflows/security.yml",
        "uv.lock",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "package.json",
        "Makefile",
        "Dockerfile",
        "compose.yaml",
        "migrations/versions/001_schema.py",
        "src/stock_desk/api/schemas.py",
        "scripts/security_scan.py",
        "scripts/verify_release.py",
        "web/src/unknown.ts",
        ".github/dependabot.yml",
    ],
)
def test_sensitive_and_unknown_paths_fail_closed_to_full(path: str) -> None:
    impact = classify_impact("pull_request", [path])

    assert impact.profile == FULL_PROFILE
    assert impact.full is True
    assert impact.reason == f"unclassified-path:{path}"


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


def test_paths_are_deduplicated_and_sorted() -> None:
    impact = classify_impact("pull_request", ["README.md", "docs/z.md", "README.md"])

    assert impact.changed_files == ("README.md", "docs/z.md")


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


def test_cli_writes_stdout_and_github_outputs(
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

    expected = "profile=docs-only\nfull=false\nreason=explicit-docs-only\n"
    assert result == 0
    assert capsys.readouterr().out == expected
    assert github_output.read_text(encoding="utf-8") == expected


def test_cli_missing_change_source_is_full(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["--event-name", "pull_request"])

    assert result == 0
    assert capsys.readouterr().out == (
        "profile=full\nfull=true\nreason=missing-change-source\n"
    )


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
    assert result.stdout == (
        "profile=release-infra-desktop\n"
        "full=false\n"
        "reason=explicit-release-infra-desktop-only\n"
    )
