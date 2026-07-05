from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess

import pytest

from scripts.verify_release import (
    GateCommand,
    ReleaseVerificationError,
    check_remote,
    verify_release,
)


EXPECTED_IDENTITY = ("CongBao", "bao_cong@outlook.com")
EXPECTED_REMOTE = "git@github.com:CongBao/stock-desk.git"


def git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@dataclass
class FakeGateRunner:
    repo: Path
    fail_command: tuple[str, ...] | None = None
    calls: list[GateCommand] = field(default_factory=list)

    def run(self, gate: GateCommand) -> None:
        self.calls.append(gate)
        if gate.command == self.fail_command:
            raise subprocess.CalledProcessError(7, gate.command)
        if gate.command == ("make", "release-check"):
            (self.repo / "web" / "dist").mkdir(parents=True, exist_ok=True)
            (self.repo / "web" / "dist" / "index.html").write_text(
                "<title>stock-desk</title>", encoding="utf-8"
            )
            (self.repo / "dist").mkdir(exist_ok=True)
            (self.repo / "dist" / "stock_desk-0.1.0-py3-none-any.whl").write_bytes(
                b"fixture"
            )
            (self.repo / "dist" / "stock_desk-0.1.0.tar.gz").write_bytes(b"fixture")


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "phase/release-test")
    git(repo, "config", "user.name", EXPECTED_IDENTITY[0])
    git(repo, "config", "user.email", EXPECTED_IDENTITY[1])
    git(repo, "remote", "add", "origin", EXPECTED_REMOTE)

    (repo / ".gitignore").write_text("dist/\nweb/dist/\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "stock-desk"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repo / "web").mkdir()
    (repo / "web" / "package.json").write_text(
        '{"name":"@stock-desk/web","version":"0.1.0"}\n',
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-07-05\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "test fixture")
    return repo


def run_verifier(repo: Path, runner: FakeGateRunner) -> None:
    verify_release(repo, "0.1.0", runner, fingerprint=lambda _repo: "stable")


def test_rejects_a_dirty_worktree_before_running_gates(release_repo: Path) -> None:
    (release_repo / "README.md").write_text("dirty\n", encoding="utf-8")
    runner = FakeGateRunner(release_repo)

    with pytest.raises(ReleaseVerificationError, match="worktree is not clean"):
        run_verifier(release_repo, runner)

    assert runner.calls == []


def test_rejects_detached_head_with_a_branch_policy_diagnostic(
    release_repo: Path,
) -> None:
    git(release_repo, "checkout", "-q", "--detach")

    with pytest.raises(ReleaseVerificationError, match="release branch"):
        run_verifier(release_repo, FakeGateRunner(release_repo))


@pytest.mark.parametrize(
    "push_urls",
    [
        ("ssh://bad.example.invalid/private/repository.git",),
        (EXPECTED_REMOTE, "ssh://bad.example.invalid/second.git"),
        (EXPECTED_REMOTE, EXPECTED_REMOTE),
    ],
)
def test_rejects_any_noncanonical_or_multiple_push_destinations(
    release_repo: Path, push_urls: tuple[str, ...]
) -> None:
    for push_url in push_urls:
        git(release_repo, "config", "--add", "remote.origin.pushurl", push_url)

    with pytest.raises(ReleaseVerificationError, match="origin remote") as captured:
        check_remote(release_repo)

    assert all(push_url not in str(captured.value) for push_url in push_urls)


def test_accepts_one_explicit_canonical_push_destination(release_repo: Path) -> None:
    git(release_repo, "config", "remote.origin.pushurl", EXPECTED_REMOTE)

    check_remote(release_repo)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("identity", "configured Git identity"),
        ("remote", "origin remote"),
        ("python-version", "project versions"),
        ("web-version", "project versions"),
        ("changelog", "release changelog entry"),
    ],
)
def test_rejects_bad_release_metadata(
    release_repo: Path, mutation: str, message: str
) -> None:
    if mutation == "identity":
        git(release_repo, "config", "user.email", "wrong@example.com")
    elif mutation == "remote":
        git(release_repo, "remote", "set-url", "origin", "git@example.invalid:x/y.git")
    elif mutation == "python-version":
        path = release_repo / "pyproject.toml"
        path.write_text(path.read_text().replace("0.1.0", "0.2.0"), encoding="utf-8")
        git(release_repo, "add", str(path))
        git(release_repo, "commit", "-q", "-m", "change Python version")
    elif mutation == "web-version":
        path = release_repo / "web" / "package.json"
        path.write_text(path.read_text().replace("0.1.0", "0.2.0"), encoding="utf-8")
        git(release_repo, "add", str(path))
        git(release_repo, "commit", "-q", "-m", "change web version")
    else:
        path = release_repo / "CHANGELOG.md"
        path.write_text(
            path.read_text().replace("2026-07-05", "Unreleased"), encoding="utf-8"
        )
        git(release_repo, "add", str(path))
        git(release_repo, "commit", "-q", "-m", "remove release date")

    with pytest.raises(ReleaseVerificationError, match=message):
        run_verifier(release_repo, FakeGateRunner(release_repo))


def test_rejects_a_bad_identity_anywhere_in_reachable_history(
    release_repo: Path,
) -> None:
    path = release_repo / "CHANGELOG.md"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    git(release_repo, "add", str(path))
    git(
        release_repo,
        "-c",
        "user.name=Someone Else",
        "-c",
        "user.email=else@example.com",
        "commit",
        "-q",
        "-m",
        "bad identity",
    )
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    git(release_repo, "add", str(path))
    git(release_repo, "commit", "-q", "-m", "restore expected identity at HEAD")

    with pytest.raises(ReleaseVerificationError, match="reachable commit identities"):
        run_verifier(release_repo, FakeGateRunner(release_repo))


def test_rejects_forbidden_paths_even_when_deleted_from_head(
    release_repo: Path,
) -> None:
    forbidden = release_repo / "docs" / "superpowers" / "private.md"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("private\n", encoding="utf-8")
    git(release_repo, "add", str(forbidden))
    git(release_repo, "commit", "-q", "-m", "add forbidden history")
    git(release_repo, "rm", "-q", str(forbidden))
    git(release_repo, "commit", "-q", "-m", "remove forbidden history")

    with pytest.raises(ReleaseVerificationError, match="reachable Git history"):
        run_verifier(release_repo, FakeGateRunner(release_repo))


def test_reports_a_failed_canonical_gate(release_repo: Path) -> None:
    runner = FakeGateRunner(release_repo, fail_command=("make", "release-check"))

    with pytest.raises(ReleaseVerificationError, match="release gate failed"):
        run_verifier(release_repo, runner)

    assert [call.command for call in runner.calls] == [("make", "release-check")]


def test_success_runs_timed_gates_and_rechecks_clean_sources(
    release_repo: Path,
) -> None:
    runner = FakeGateRunner(release_repo)

    run_verifier(release_repo, runner)

    assert runner.calls == [
        GateCommand(("make", "release-check"), timeout_seconds=1800),
        GateCommand(("pnpm", "e2e"), timeout_seconds=600),
    ]
