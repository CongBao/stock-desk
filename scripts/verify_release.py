from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from email.parser import BytesParser
from email.policy import default as default_email_policy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tomllib
from typing import Protocol
import zipfile

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from scripts.check_public_tree import forbidden_paths
from scripts.source_fingerprint import compute_source_fingerprint


EXPECTED_GIT_NAME = "CongBao"
EXPECTED_GIT_EMAIL = "bao_cong@outlook.com"
EXPECTED_REMOTE = "git@github.com:CongBao/stock-desk.git"
GIT_TIMEOUT_SECONDS = 30
E2E_BASE_URL = "http://127.0.0.1:8000"
VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)


@dataclass(frozen=True, slots=True)
class GateCommand:
    command: tuple[str, ...]
    timeout_seconds: int
    environment: tuple[tuple[str, str], ...] = ()


class GateRunner(Protocol):
    def run(self, gate: GateCommand) -> None: ...


class ReleaseVerificationError(RuntimeError):
    pass


class SubprocessGateRunner:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def run(self, gate: GateCommand) -> None:
        environment = os.environ.copy()
        environment.update(gate.environment)
        subprocess.run(  # noqa: S603
            gate.command,
            cwd=self._repo,
            check=True,
            env=environment,
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
    fetch_urls = _git(repo, "remote", "get-url", "--all", "origin").splitlines()
    push_urls = _git(
        repo, "remote", "get-url", "--push", "--all", "origin"
    ).splitlines()
    if fetch_urls != [EXPECTED_REMOTE] or push_urls != [EXPECTED_REMOTE]:
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
    headings = re.findall(
        rf"^## \[{re.escape(version)}\] - (?P<release_date>\S+)$",
        changelog,
        re.MULTILINE,
    )
    if (
        len(headings) != 1
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", headings[0]) is None
    ):
        raise ReleaseVerificationError("release changelog entry is not finalized")
    try:
        parsed_date = date.fromisoformat(headings[0])
    except ValueError as error:
        raise ReleaseVerificationError(
            "release changelog entry is not finalized"
        ) from error
    if parsed_date.isoformat() != headings[0]:
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


def _check_package_metadata(
    payload: bytes, version: str, artifact_description: str
) -> None:
    metadata = BytesParser(policy=default_email_policy).parsebytes(payload)
    names = [str(value) for value in metadata.get_all("Name", [])]
    versions = [str(value) for value in metadata.get_all("Version", [])]
    if metadata.defects or names != ["stock-desk"] or versions != [version]:
        raise ReleaseVerificationError(
            f"release {artifact_description} build artifact is invalid"
        )


def _check_wheel_artifact(wheel_path: Path, version: str) -> None:
    dist_info = f"stock_desk-{version}.dist-info"
    metadata_path = f"{dist_info}/METADATA"
    required_members = {
        "stock_desk/__init__.py",
        metadata_path,
        f"{dist_info}/WHEEL",
        f"{dist_info}/RECORD",
    }
    with zipfile.ZipFile(wheel_path) as wheel:
        members = wheel.namelist()
        if (
            wheel.testzip() is not None
            or len(members) != len(set(members))
            or not required_members.issubset(members)
            or any(wheel.getinfo(member).is_dir() for member in required_members)
        ):
            raise ReleaseVerificationError("release wheel build artifact is invalid")
        _check_package_metadata(wheel.read(metadata_path), version, "wheel")


def _check_source_artifact(source_path: Path, version: str) -> None:
    root = f"stock_desk-{version}"
    metadata_path = f"{root}/PKG-INFO"
    required_members = {
        f"{root}/pyproject.toml",
        metadata_path,
        f"{root}/src/stock_desk/__init__.py",
    }
    with tarfile.open(source_path, "r:gz") as source:
        members = source.getmembers()
        members_by_name = {member.name: member for member in members}
        if (
            len(members) != len(members_by_name)
            or not required_members.issubset(members_by_name)
            or any(not members_by_name[name].isfile() for name in required_members)
        ):
            raise ReleaseVerificationError("release source build artifact is invalid")
        metadata_file = source.extractfile(members_by_name[metadata_path])
        if metadata_file is None:
            raise ReleaseVerificationError("release source build artifact is invalid")
        _check_package_metadata(metadata_file.read(), version, "source")


def check_build_artifacts(repo: Path, version: str) -> None:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseVerificationError("release build artifact version is invalid")
    web_entrypoint = repo / "web" / "dist" / "index.html"
    package_dir = repo / "dist"
    if (
        web_entrypoint.is_symlink()
        or not web_entrypoint.is_file()
        or web_entrypoint.stat().st_size == 0
    ):
        raise ReleaseVerificationError("release web build artifact is missing")
    try:
        web_html = web_entrypoint.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseVerificationError(
            "release web build artifact is invalid"
        ) from error
    if re.search(r"<title>\s*stock-desk\s*</title>", web_html, re.IGNORECASE) is None:
        raise ReleaseVerificationError("release web build artifact is invalid")

    expected_wheel = package_dir / f"stock_desk-{version}-py3-none-any.whl"
    expected_source = package_dir / f"stock_desk-{version}.tar.gz"
    wheels = set(package_dir.glob("*.whl"))
    source_archives = set(package_dir.glob("*.tar.gz"))
    if (
        wheels != {expected_wheel}
        or source_archives != {expected_source}
        or any(
            path.is_symlink() or not path.is_file() or path.stat().st_size == 0
            for path in (expected_wheel, expected_source)
        )
    ):
        raise ReleaseVerificationError("release Python build artifact set is invalid")
    try:
        _check_wheel_artifact(expected_wheel, version)
        _check_source_artifact(expected_source, version)
    except (
        KeyError,
        OSError,
        RuntimeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        raise ReleaseVerificationError(
            "release Python build artifact is invalid"
        ) from error


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
        GateCommand(
            ("pnpm", "e2e"),
            timeout_seconds=600,
            environment=(("STOCK_DESK_E2E_BASE_URL", E2E_BASE_URL),),
        ),
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
    check_build_artifacts(resolved_repo, version)


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
