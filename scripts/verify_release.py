from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Callable
import csv
from dataclasses import dataclass
from datetime import date
from email.parser import Parser
from email.policy import default as default_email_policy
import hashlib
import io
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import subprocess
import sys
import tarfile
import tomllib
from typing import Protocol
import zipfile

from packaging.metadata import Metadata

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
    try:
        metadata = Metadata.from_email(payload, validate=True)
    except (ExceptionGroup, UnicodeError, ValueError) as error:
        raise ReleaseVerificationError(
            f"release {artifact_description} build artifact is invalid"
        ) from error
    if metadata.name != "stock-desk" or str(metadata.version) != version:
        raise ReleaseVerificationError(
            f"release {artifact_description} build artifact is invalid"
        )


def _invalid_wheel() -> ReleaseVerificationError:
    return ReleaseVerificationError("release wheel build artifact is invalid")


def _check_wheel_metadata(payload: bytes) -> None:
    try:
        content = payload.decode("utf-8")
    except UnicodeError as error:
        raise _invalid_wheel() from error
    metadata = Parser(policy=default_email_policy).parsestr(content)
    wheel_versions = [str(value) for value in metadata.get_all("Wheel-Version", [])]
    purelib_values = [str(value) for value in metadata.get_all("Root-Is-Purelib", [])]
    tags = [str(value) for value in metadata.get_all("Tag", [])]
    if (
        metadata.defects
        or wheel_versions != ["1.0"]
        or purelib_values not in (["true"], ["false"])
        or tags != ["py3-none-any"]
    ):
        raise _invalid_wheel()


def _is_safe_wheel_path(path: str) -> bool:
    normalized = PurePosixPath(path)
    return (
        bool(path)
        and "\\" not in path
        and "\x00" not in path
        and ":" not in path
        and not normalized.is_absolute()
        and ".." not in normalized.parts
        and normalized.as_posix() == path
    )


def _check_wheel_record(
    payload: bytes,
    archive_payloads: dict[str, bytes],
    required_members: set[str],
    record_path: str,
) -> None:
    try:
        content = payload.decode("utf-8")
        rows = list(csv.reader(io.StringIO(content, newline=""), strict=True))
    except (UnicodeError, csv.Error) as error:
        raise _invalid_wheel() from error
    if any(len(row) != 3 for row in rows):
        raise _invalid_wheel()
    paths = [row[0] for row in rows]
    path_set = set(paths)
    if (
        len(paths) != len(path_set)
        or not all(_is_safe_wheel_path(path) for path in paths)
        or not required_members.issubset(path_set)
        or path_set != archive_payloads.keys()
    ):
        raise _invalid_wheel()
    for path, hash_value, size in rows:
        if path == record_path:
            if hash_value or size:
                raise _invalid_wheel()
            continue
        encoded_digest = hash_value.removeprefix("sha256=")
        if (
            encoded_digest == hash_value
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded_digest) is None
            or re.fullmatch(r"[0-9]+", size) is None
        ):
            raise _invalid_wheel()
        try:
            digest = base64.urlsafe_b64decode(f"{encoded_digest}=")
        except (ValueError, binascii.Error) as error:
            raise _invalid_wheel() from error
        member_payload = archive_payloads[path]
        canonical_digest = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if (
            len(digest) != 32
            or canonical_digest != encoded_digest
            or digest != hashlib.sha256(member_payload).digest()
            or int(size) != len(member_payload)
        ):
            raise _invalid_wheel()


def _read_repository_payloads(root: Path, archive_prefix: str) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseVerificationError("release package source tree is invalid")
    payloads: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ReleaseVerificationError("release package source tree is invalid")
        if path.is_file():
            payloads[f"{archive_prefix}/{relative_path.as_posix()}"] = path.read_bytes()
    return payloads


def _expected_wheel_package_payloads(repo: Path) -> dict[str, bytes]:
    payloads = _read_repository_payloads(repo / "src" / "stock_desk", "stock_desk")
    alembic_config = repo / "alembic.ini"
    if alembic_config.is_file() and not alembic_config.is_symlink():
        payloads["stock_desk/alembic.ini"] = alembic_config.read_bytes()
    migrations = repo / "migrations"
    if migrations.exists():
        payloads.update(_read_repository_payloads(migrations, "stock_desk/migrations"))
    return payloads


def _check_wheel_artifact(repo: Path, wheel_path: Path, version: str) -> bytes:
    dist_info = f"stock_desk-{version}.dist-info"
    metadata_path = f"{dist_info}/METADATA"
    wheel_metadata_path = f"{dist_info}/WHEEL"
    record_path = f"{dist_info}/RECORD"
    required_members = {
        "stock_desk/__init__.py",
        metadata_path,
        wheel_metadata_path,
        record_path,
    }
    with zipfile.ZipFile(wheel_path) as wheel:
        members = wheel.namelist()
        if (
            wheel.testzip() is not None
            or len(members) != len(set(members))
            or not required_members.issubset(members)
            or any(wheel.getinfo(member).is_dir() for member in required_members)
        ):
            raise _invalid_wheel()
        archive_payloads = {
            member.filename: wheel.read(member)
            for member in wheel.infolist()
            if not member.is_dir()
        }
        metadata_payload = archive_payloads[metadata_path]
        _check_package_metadata(metadata_payload, version, "wheel")
        _check_wheel_metadata(wheel.read(wheel_metadata_path))
        _check_wheel_record(
            wheel.read(record_path),
            archive_payloads,
            required_members,
            record_path,
        )
        package_payloads = {
            path: payload
            for path, payload in archive_payloads.items()
            if path.startswith("stock_desk/")
        }
        if package_payloads != _expected_wheel_package_payloads(repo):
            raise _invalid_wheel()
        return metadata_payload


def _invalid_source() -> ReleaseVerificationError:
    return ReleaseVerificationError("release source build artifact is invalid")


def _check_sdist_pyproject(payload: bytes, version: str) -> None:
    try:
        pyproject = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise _invalid_source() from error
    project = pyproject.get("project")
    build_system = pyproject.get("build-system")
    if (
        not isinstance(project, dict)
        or project.get("name") != "stock-desk"
        or project.get("version") != version
        or not isinstance(build_system, dict)
        or build_system.get("requires") != ["hatchling>=1.27,<2"]
        or build_system.get("build-backend") != "hatchling.build"
    ):
        raise _invalid_source()


def _check_source_artifact(repo: Path, source_path: Path, version: str) -> bytes:
    root = f"stock_desk-{version}"
    pyproject_path = f"{root}/pyproject.toml"
    metadata_path = f"{root}/PKG-INFO"
    required_members = {
        pyproject_path,
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
            raise _invalid_source()
        pyproject_file = source.extractfile(members_by_name[pyproject_path])
        metadata_file = source.extractfile(members_by_name[metadata_path])
        if pyproject_file is None or metadata_file is None:
            raise _invalid_source()
        pyproject_payload = pyproject_file.read()
        if pyproject_payload != (repo / "pyproject.toml").read_bytes():
            raise _invalid_source()
        _check_sdist_pyproject(pyproject_payload, version)
        metadata_payload = metadata_file.read()
        _check_package_metadata(metadata_payload, version, "source")
        source_prefix = f"{root}/src/stock_desk/"
        package_payloads: dict[str, bytes] = {}
        for name, member in members_by_name.items():
            if name.startswith(source_prefix) and member.isfile():
                package_file = source.extractfile(member)
                if package_file is None:
                    raise _invalid_source()
                package_payloads[name] = package_file.read()
        expected_package_payloads = _read_repository_payloads(
            repo / "src" / "stock_desk", f"{root}/src/stock_desk"
        )
        if package_payloads != expected_package_payloads:
            raise _invalid_source()
        return metadata_payload


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
        wheel_metadata = _check_wheel_artifact(repo, expected_wheel, version)
        source_metadata = _check_source_artifact(repo, expected_source, version)
        if wheel_metadata != source_metadata:
            raise ReleaseVerificationError(
                "release wheel and source metadata do not match"
            )
    except ReleaseVerificationError:
        raise
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

    check_build_artifacts(resolved_repo, version)
    check_clean_worktree(resolved_repo)
    try:
        final_fingerprint = fingerprint(resolved_repo)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseVerificationError("unable to recheck release sources") from error
    if final_fingerprint != initial_fingerprint:
        raise ReleaseVerificationError(
            "release source fingerprint changed during gates"
        )


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
