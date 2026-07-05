from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from typing import Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from scripts.check_public_tree import forbidden_paths
from scripts.source_fingerprint import compute_source_fingerprint


EXPECTED_GIT_NAME = "CongBao"
EXPECTED_GIT_EMAIL = "bao_cong@outlook.com"
EXPECTED_REMOTE = "git@github.com:CongBao/stock-desk.git"
RELEASE_DATE = "2026-07-05"
GIT_TIMEOUT_SECONDS = 30
VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)


@dataclass(frozen=True, slots=True)
class GateCommand:
    command: tuple[str, ...]
    timeout_seconds: int


class GateRunner(Protocol):
    def run(self, gate: GateCommand) -> None: ...


class ReleaseVerificationError(RuntimeError):
    pass


class SubprocessGateRunner:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def run(self, gate: GateCommand) -> None:
        subprocess.run(  # noqa: S603
            gate.command,
            cwd=self._repo,
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=gate.timeout_seconds,
        )


def _git(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "-C", os.fspath(repo), *arguments),
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReleaseVerificationError(
            "unable to inspect the Git repository"
        ) from error
    return result.stdout


def _git_paths(repo: Path, *arguments: str) -> list[str]:
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "-C", os.fspath(repo), *arguments),
            check=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReleaseVerificationError("unable to inspect Git paths") from error
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def check_clean_worktree(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseVerificationError("worktree is not clean")


def check_branch(repo: Path) -> None:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != "main" and not branch.startswith("phase/"):
        raise ReleaseVerificationError("release branch must be main or phase/*")


def check_identity(repo: Path) -> None:
    configured = (
        _git(repo, "config", "--get", "user.name").strip(),
        _git(repo, "config", "--get", "user.email").strip(),
    )
    if configured != (EXPECTED_GIT_NAME, EXPECTED_GIT_EMAIL):
        raise ReleaseVerificationError(
            "configured Git identity is not the release identity"
        )

    history = _git(repo, "log", "--format=%an%x00%ae%x00%cn%x00%ce", "HEAD")
    expected = (
        EXPECTED_GIT_NAME,
        EXPECTED_GIT_EMAIL,
        EXPECTED_GIT_NAME,
        EXPECTED_GIT_EMAIL,
    )
    for record in history.splitlines():
        if tuple(record.split("\0")) != expected:
            raise ReleaseVerificationError(
                "reachable commit identities are not all the release identity"
            )


def check_remote(repo: Path) -> None:
    fetch_url = _git(repo, "remote", "get-url", "origin").strip()
    push_urls = _git(
        repo, "remote", "get-url", "--push", "--all", "origin"
    ).splitlines()
    if fetch_url != EXPECTED_REMOTE or push_urls != [EXPECTED_REMOTE]:
        raise ReleaseVerificationError(
            "origin remote is not the public release repository"
        )


def check_versions(repo: Path, version: str) -> None:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseVerificationError(
            "release version must be a stable numeric version"
        )
    try:
        with (repo / "pyproject.toml").open("rb") as project_file:
            python_version = tomllib.load(project_file)["project"]["version"]
        web_package = json.loads(
            (repo / "web" / "package.json").read_text(encoding="utf-8")
        )
        web_version = web_package["version"]
    except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise ReleaseVerificationError("unable to read project versions") from error
    if python_version != version or web_version != version:
        raise ReleaseVerificationError(
            "project versions do not match the release version"
        )


def check_changelog(repo: Path, version: str) -> None:
    try:
        changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseVerificationError(
            "unable to read the release changelog"
        ) from error
    dated_heading = f"## [{version}] - {RELEASE_DATE}"
    unreleased_heading = f"## [{version}] - Unreleased"
    if changelog.count(dated_heading) != 1 or unreleased_heading in changelog:
        raise ReleaseVerificationError("release changelog entry is not finalized")


def check_public_history(repo: Path) -> None:
    current_paths = _git_paths(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    if forbidden_paths(current_paths):
        raise ReleaseVerificationError("HEAD tree contains forbidden internal paths")

    historical_paths = _git_paths(
        repo,
        "log",
        "--format=",
        "--name-only",
        "-z",
        "HEAD",
    )
    if forbidden_paths(historical_paths):
        raise ReleaseVerificationError(
            "reachable Git history contains forbidden internal paths"
        )


def check_build_artifacts(repo: Path) -> None:
    web_entrypoint = repo / "web" / "dist" / "index.html"
    package_dir = repo / "dist"
    if web_entrypoint.is_symlink() or not web_entrypoint.is_file():
        raise ReleaseVerificationError("release web build artifact is missing")
    wheels = list(package_dir.glob("*.whl"))
    source_archives = list(package_dir.glob("*.tar.gz"))
    if (
        not wheels
        or not source_archives
        or any(
            path.is_symlink() or not path.is_file() for path in wheels + source_archives
        )
    ):
        raise ReleaseVerificationError("release Python build artifacts are missing")


def verify_release(
    repo: Path,
    version: str,
    runner: GateRunner,
    *,
    fingerprint: Callable[[Path], str] = compute_source_fingerprint,
) -> None:
    resolved_repo = repo.resolve(strict=True)
    check_clean_worktree(resolved_repo)
    check_branch(resolved_repo)
    check_identity(resolved_repo)
    check_remote(resolved_repo)
    check_versions(resolved_repo, version)
    check_changelog(resolved_repo, version)
    check_public_history(resolved_repo)
    try:
        initial_fingerprint = fingerprint(resolved_repo)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseVerificationError(
            "unable to fingerprint release sources"
        ) from error

    gates = (
        GateCommand(("make", "release-check"), timeout_seconds=1800),
        GateCommand(("pnpm", "e2e"), timeout_seconds=600),
    )
    for gate in gates:
        try:
            runner.run(gate)
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise ReleaseVerificationError("release gate failed") from error

    check_clean_worktree(resolved_repo)
    try:
        final_fingerprint = fingerprint(resolved_repo)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseVerificationError("unable to recheck release sources") from error
    if final_fingerprint != initial_fingerprint:
        raise ReleaseVerificationError(
            "release source fingerprint changed during gates"
        )
    check_build_artifacts(resolved_repo)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and run the canonical Stock Desk release gates."
    )
    parser.add_argument("version", help="stable release version, for example 0.1.0")
    return parser


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    repo = Path(__file__).resolve().parent.parent
    try:
        verify_release(repo, options.version, SubprocessGateRunner(repo))
    except ReleaseVerificationError as error:
        print(f"Release verification failed: {error}", file=sys.stderr)
        return 1
    print(f"Release verification passed for {options.version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
