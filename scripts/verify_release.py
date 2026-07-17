from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Callable, Mapping
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
import tempfile
from threading import Thread
import time
import tomllib
from typing import IO, Protocol
import zipfile

from packaging.metadata import Metadata

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from scripts.check_public_tree import forbidden_paths
from scripts.check_requirement_coverage import RELEASE_EVIDENCE_TIMEOUT_BUDGET
from scripts.main_validation_proof import (
    MainValidationProofError,
    verify_proof,
    verify_post_gh_attestation_binding,
    verify_proved_artifacts,
)
from scripts.source_fingerprint import compute_source_fingerprint


EXPECTED_GIT_NAME = "CongBao"
EXPECTED_GIT_EMAIL = "bao_cong@outlook.com"
EXPECTED_REMOTE = "git@github.com:CongBao/stock-desk.git"
GITHUB_MERGE_AUTHOR_NAMES = frozenset({EXPECTED_GIT_NAME, "Cong Bao"})
GITHUB_MERGE_COMMITTER = ("GitHub", "noreply@github.com")
GIT_TIMEOUT_SECONDS = 30
E2E_BASE_URL = "http://127.0.0.1:8000"
VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
GITHUB_MERGE_SUBJECT_PATTERN = re.compile(
    r"Merge pull request #(?P<pull_request>[1-9][0-9]*) "
    r"from CongBao/(?P<branch>[A-Za-z0-9._/-]+)"
)
CANDIDATE_REPORT_SCHEMA = "stock-desk-release-candidate-report-v1"
CANDIDATE_REPORT_DIRECTORY = PurePosixPath("test-results/release")
CANDIDATE_FULL_PYTHON_TIMEOUT_SECONDS = 90 * 60


@dataclass(frozen=True, slots=True)
class GateCommand:
    command: tuple[str, ...]
    timeout_seconds: int
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ProvedReleaseInputs:
    proof_path: Path
    proof_verification_binding_path: Path
    proof_gh_verification_path: Path
    artifact_roots: Mapping[str, Path]
    artifact_attestation_paths: Mapping[str, Path]


PRE_PUBLISH_EVIDENCE_GATE = GateCommand(
    (
        "uv",
        "run",
        "--frozen",
        "python",
        "scripts/check_requirement_coverage.py",
        "--mode",
        "pre-publish",
    ),
    timeout_seconds=RELEASE_EVIDENCE_TIMEOUT_BUDGET.outer_gate_timeout_seconds,
)

_RELEASE_SCAN_CHUNK_SIZE = 64 * 1024
_RELEASE_SCAN_OVERLAP = 4096
_SYNTHETIC_HOME_USERS = frozenset(
    {"alice", "example", "owner", "operator", "user", "username"}
)
_CASE_SENSITIVE_SYNTHETIC_HOME_USERS = frozenset({"Bao"})
_POSIX_HOME_PATH = re.compile(
    rb"/(?:home|" + rb"Users)/(?P<user>(?!\[\^)[^/'\"\x00-\x1f\x7f]{1,512})/"
)
_WINDOWS_HOME_PATH = re.compile(
    rb"(?i)(?:[A-Z]:)?[\\/]Users[\\/]"
    rb"(?P<user>(?!\[\^)[^\\/'\"\x00-\x1f\x7f]{1,512})[\\/]"
)
_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
    b"-----BEGIN " + b"RSA PRIVATE KEY-----",
    b"-----BEGIN " + b"EC PRIVATE KEY-----",
    b"-----BEGIN " + b"PRIVATE KEY-----",
)
_DIRECT_CREDENTIAL_PATTERNS = (
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{32,}\b"),
)
_PROVIDER_CREDENTIAL = re.compile(
    rb"(?ix)\b(?:OPENAI_API_KEY|DEEPSEEK_API_KEY|DASHSCOPE_API_KEY|"
    rb"QWEN_API_KEY|MOONSHOT_API_KEY|ZHIPU_API_KEY|TUSHARE_TOKEN|"
    rb"MODEL_API_KEY|STOCK_DESK_MASTER_KEY)\s*[:=]\s*[\"']?"
    rb"(?P<value>(?:sk-[A-Za-z0-9._-]{24,}|[A-Za-z0-9][A-Za-z0-9._-]{31,}))"
)
_GENERATED_OR_PRIVATE_COMPONENTS = frozenset(
    {
        ".agents",
        ".codex",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".superpowers",
        ".venv",
        "__pycache__",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "openspec",
        "outputs",
        "test-results",
        "work",
    }
)


class ReleaseLeakScanner:
    """Incrementally reject high-confidence private release payloads."""

    def __init__(self, *, label: str) -> None:
        self._label = label
        self._tail = b""

    def feed(self, payload: bytes) -> None:
        if not payload:
            return
        window = self._tail + payload
        self._scan(window)
        self._tail = window[-_RELEASE_SCAN_OVERLAP:]

    def finish(self) -> None:
        self._tail = b""

    def _scan(self, payload: bytes) -> None:
        for pattern in (_POSIX_HOME_PATH, _WINDOWS_HOME_PATH):
            for match in pattern.finditer(payload):
                raw_user = match.group("user")
                try:
                    user = raw_user.decode("utf-8")
                except UnicodeDecodeError:
                    user = ""
                if (
                    user not in _CASE_SENSITIVE_SYNTHETIC_HOME_USERS
                    and user.casefold() not in _SYNTHETIC_HOME_USERS
                ):
                    self._reject()
        if any(marker in payload for marker in _PRIVATE_KEY_MARKERS):
            self._reject()
        if any(pattern.search(payload) for pattern in _DIRECT_CREDENTIAL_PATTERNS):
            self._reject()
        if _PROVIDER_CREDENTIAL.search(payload) is not None:
            self._reject()

    def _reject(self) -> None:
        raise ReleaseVerificationError(
            f"release payload contains private path or credential material: {self._label}"
        )


def _scan_stream(stream: object, *, label: str) -> None:
    reader = getattr(stream, "read", None)
    if not callable(reader):
        raise ReleaseVerificationError("release payload stream is unreadable")
    scanner = ReleaseLeakScanner(label=label)
    while True:
        chunk = reader(_RELEASE_SCAN_CHUNK_SIZE)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ReleaseVerificationError("release payload stream is not binary")
        scanner.feed(chunk)
    scanner.finish()


def _scan_path(path: Path, *, label: str) -> None:
    try:
        with path.open("rb") as payload:
            _scan_stream(payload, label=label)
    except ReleaseVerificationError:
        raise
    except OSError as error:
        raise ReleaseVerificationError("unable to scan release payload") from error


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


def _candidate_gates(*, target_performance: bool) -> tuple[GateCommand, ...]:
    performance = "performance-target" if target_performance else "performance"
    return (
        (PRE_PUBLISH_EVIDENCE_GATE,)
        + tuple(
            GateCommand(
                ("make", target),
                timeout_seconds=(
                    CANDIDATE_FULL_PYTHON_TIMEOUT_SECONDS if target == "test" else 1800
                ),
            )
            for target in (
                "test",
                "acceptance",
                "acceptance-formula",
                "acceptance-backtest",
                "acceptance-analysis",
                "acceptance-domain-contracts",
                "acceptance-full-journey",
                "performance-regressions",
                performance,
                "e2e-foundation",
                "e2e-market",
                "e2e-formula",
                "e2e-backtest",
                "e2e-analysis",
                "e2e-task-center",
                "e2e-accessibility",
                "lint",
                "typecheck",
                "security",
            )
        )
        + (
            GateCommand(
                ("uv", "run", "--frozen", "python", "scripts/verify_docs.py"),
                timeout_seconds=300,
            ),
            GateCommand(("make", "public-tree"), timeout_seconds=300),
        )
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


def _reachable_object_ids(repo: Path) -> tuple[bytes, ...]:
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "-C", os.fspath(repo), "rev-list", "--objects", "HEAD"),
            check=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ReleaseVerificationError(
            "unable to inspect reachable Git objects"
        ) from error
    object_ids = tuple(
        dict.fromkeys(line.split(maxsplit=1)[0] for line in result.stdout.splitlines())
    )
    if not object_ids:
        raise ReleaseVerificationError("reachable Git history has no objects")
    return object_ids


def _terminate_streaming_process(
    process: subprocess.Popen[bytes], worker: Thread
) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
    if process.stdout is not None:
        process.stdout.close()
    worker.join(timeout=1)
    if worker.is_alive():
        raise ReleaseVerificationError("release payload reader did not stop")


def _consume_process_with_deadline(
    process: subprocess.Popen[bytes],
    consume: Callable[[IO[bytes]], None],
    *,
    timeout_seconds: float,
    timeout_message: str,
    failure_message: str,
) -> None:
    if process.stdout is None:
        raise ReleaseVerificationError(failure_message)
    stdout = process.stdout
    failures: list[Exception] = []

    def consume_stdout() -> None:
        try:
            consume(stdout)
        except Exception as error:  # noqa: BLE001 -- propagated on the caller thread
            failures.append(error)

    deadline = time.monotonic() + timeout_seconds
    worker = Thread(
        target=consume_stdout,
        name=f"release-payload-reader-{id(process)}",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        _terminate_streaming_process(process, worker)
        raise ReleaseVerificationError(timeout_message)
    if failures:
        _terminate_streaming_process(process, worker)
        error = failures[0]
        if isinstance(error, ReleaseVerificationError):
            raise error
        raise ReleaseVerificationError(failure_message) from error
    try:
        return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        _terminate_streaming_process(process, worker)
        raise ReleaseVerificationError(timeout_message) from error
    finally:
        stdout.close()
    if return_code != 0:
        raise ReleaseVerificationError(failure_message)


def _scan_reachable_git_blobs(
    repo: Path, *, timeout_seconds: float = GIT_TIMEOUT_SECONDS
) -> None:
    object_ids = _reachable_object_ids(repo)
    with tempfile.TemporaryFile() as query:
        for object_id in object_ids:
            query.write(object_id + b"\n")
        query.seek(0)
        try:
            process = subprocess.Popen(  # noqa: S603
                ("git", "-C", os.fspath(repo), "cat-file", "--batch"),
                stdin=query,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise ReleaseVerificationError(
                "unable to scan reachable Git blobs"
            ) from error

        def consume(stdout: IO[bytes]) -> None:
            readline = stdout.readline
            read = stdout.read
            for expected_id in object_ids:
                header = readline()
                if not isinstance(header, bytes):
                    raise ReleaseVerificationError(
                        "reachable Git object framing is invalid"
                    )
                fields = header.rstrip(b"\n").split()
                if len(fields) != 3:
                    raise ReleaseVerificationError(
                        "reachable Git object framing is invalid"
                    )
                object_id, object_type, raw_size = fields
                if object_id != expected_id or not raw_size.isdigit():
                    raise ReleaseVerificationError(
                        "reachable Git object framing is invalid"
                    )
                remaining = int(raw_size)
                scanner = (
                    ReleaseLeakScanner(
                        label=f"reachable Git blob {object_id.decode('ascii')}"
                    )
                    if object_type == b"blob"
                    else None
                )
                while remaining:
                    chunk = read(min(_RELEASE_SCAN_CHUNK_SIZE, remaining))
                    if not isinstance(chunk, bytes) or not chunk:
                        raise ReleaseVerificationError(
                            "reachable Git object ended unexpectedly"
                        )
                    remaining -= len(chunk)
                    if scanner is not None:
                        scanner.feed(chunk)
                if scanner is not None:
                    scanner.finish()
                if read(1) != b"\n":
                    raise ReleaseVerificationError(
                        "reachable Git object framing is invalid"
                    )
            if read(1):
                raise ReleaseVerificationError(
                    "reachable Git object stream has trailing data"
                )

        _consume_process_with_deadline(
            process,
            consume,
            timeout_seconds=timeout_seconds,
            timeout_message="reachable Git object scan timed out",
            failure_message="unable to scan reachable Git blobs",
        )


def _safe_release_member_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and "\x00" not in name
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == name
        and _GENERATED_OR_PRIVATE_COMPONENTS.isdisjoint(path.parts)
    )


def _scan_git_archive(
    repo: Path, *, timeout_seconds: float = GIT_TIMEOUT_SECONDS
) -> None:
    try:
        process = subprocess.Popen(  # noqa: S603
            ("git", "-C", os.fspath(repo), "archive", "--format=tar", "HEAD"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ReleaseVerificationError("unable to scan the source archive") from error

    def consume(stdout: IO[bytes]) -> None:
        with tarfile.open(fileobj=stdout, mode="r|") as archive:
            for member in archive:
                if not _safe_release_member_path(member.name):
                    raise ReleaseVerificationError(
                        "release source archive contains a private or generated path"
                    )
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ReleaseVerificationError(
                        "release source archive contains an unsupported member"
                    )
                payload = archive.extractfile(member)
                if payload is None:
                    raise ReleaseVerificationError(
                        "release source archive member is unreadable"
                    )
                _scan_stream(payload, label="source archive member")

    _consume_process_with_deadline(
        process,
        consume,
        timeout_seconds=timeout_seconds,
        timeout_message="source archive scan timed out",
        failure_message="unable to scan the source archive",
    )


def compute_fixture_hashes(repo: Path) -> dict[str, str]:
    paths = _git_paths(
        repo,
        "ls-files",
        "-z",
        "--",
        "tests/fixtures",
        "tests/acceptance/requirements.yml",
        "tests/acceptance/v1_1_requirements.yml",
    )
    hashes: dict[str, str] = {}
    for raw_path in sorted(paths):
        relative = PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != raw_path
        ):
            raise ReleaseVerificationError("release fixture path is invalid")
        fixture = repo.joinpath(*relative.parts)
        if fixture.is_symlink() or not fixture.is_file():
            raise ReleaseVerificationError("release fixture is not a regular file")
        hashes[raw_path] = f"sha256:{hashlib.sha256(fixture.read_bytes()).hexdigest()}"
    if not hashes:
        raise ReleaseVerificationError("release fixtures are missing")
    return hashes


def _candidate_report_target(repo: Path, requested: Path) -> Path:
    expected_root = repo.joinpath(*CANDIDATE_REPORT_DIRECTORY.parts)
    try:
        relative = requested.absolute().relative_to(repo)
    except ValueError as error:
        raise ReleaseVerificationError("candidate report path is invalid") from error
    if (
        PurePosixPath(relative.as_posix()).parent != CANDIDATE_REPORT_DIRECTORY
        or requested.suffix != ".json"
        or requested.name.startswith(".")
    ):
        raise ReleaseVerificationError("candidate report path is invalid")
    current = repo
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ReleaseVerificationError("candidate report path is invalid")
    expected_root.mkdir(parents=True, exist_ok=True)
    if requested.is_symlink() or (requested.exists() and not requested.is_file()):
        raise ReleaseVerificationError("candidate report path is invalid")
    return requested


def _write_candidate_report(repo: Path, requested: Path, payload: object) -> None:
    target = _candidate_report_target(repo, requested)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ReleaseVerificationError("candidate report temporary path is unsafe")
    try:
        with temporary.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except OSError as error:
        raise ReleaseVerificationError("unable to write candidate report") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def check_clean_worktree(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseVerificationError("worktree is not clean")


def check_branch(repo: Path) -> None:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != "main" and not branch.startswith("phase/"):
        raise ReleaseVerificationError("release branch must be main or phase/*")


def check_exact_release_tag(
    repo: Path, version: str, *, tag_name: str | None = None
) -> None:
    expected = tag_name or f"v{version}"
    if (
        re.fullmatch(
            rf"v{re.escape(version)}(?:-(?:alpha|beta)\.[1-9][0-9]*)?", expected
        )
        is None
    ):
        raise ReleaseVerificationError(
            "release tag name is not allowed for this version"
        )
    try:
        tagged_commit = _git(
            repo, "rev-parse", "--verify", f"refs/tags/{expected}^{{commit}}"
        ).strip()
        head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip()
    except ReleaseVerificationError as error:
        raise ReleaseVerificationError(
            "release tag is missing or does not resolve to a commit"
        ) from error
    if tagged_commit != head:
        raise ReleaseVerificationError(
            "release tag does not point to the proved commit"
        )


def _load_strict_json(path: Path, *, label: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise ReleaseVerificationError(f"{label} is missing or unsafe")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError(f"{label} is invalid") from error


def _verify_controlled_gh_proof_output(
    value: object,
    *,
    proof_sha256: str,
    commit_sha: str,
) -> None:
    """Accept gh's verification result only inside the exact release runner context.

    This file is not a signature or a portable trust token.  The preceding workflow
    step performs the cryptographic verification; these checks prevent the reusable
    Python entry point from silently treating a caller-authored JSON receipt as one.
    """
    expected_environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "CongBao/stock-desk",
        "GITHUB_SHA": commit_sha,
        "GITHUB_WORKFLOW": "Release",
    }
    if any(
        os.environ.get(name) != expected
        for name, expected in expected_environment.items()
    ):
        raise ReleaseVerificationError(
            "proved release reuse requires the controlled GitHub Release workflow"
        )
    if not isinstance(value, list) or not value:
        raise ReleaseVerificationError("GitHub proof verification evidence is empty")
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ReleaseVerificationError(
                f"GitHub proof verification entry {index} is invalid"
            )
        result = entry.get("verificationResult")
        statement = result.get("statement") if isinstance(result, dict) else None
        subjects = statement.get("subject") if isinstance(statement, dict) else None
        if not isinstance(subjects, list):
            continue
        for subject in subjects:
            digest = subject.get("digest") if isinstance(subject, dict) else None
            if isinstance(digest, dict) and digest.get("sha256") == proof_sha256:
                return
    raise ReleaseVerificationError(
        "GitHub proof verification evidence does not bind the exact proof"
    )


def verify_proved_release_inputs(
    repo: Path,
    inputs: ProvedReleaseInputs,
) -> dict[str, object]:
    proof_value = _load_strict_json(inputs.proof_path, label="main validation proof")
    if not isinstance(proof_value, dict):
        raise ReleaseVerificationError("main validation proof must be an object")
    try:
        proof_bytes = inputs.proof_path.read_bytes()
        verify_proof(
            proof_value,
            repo_root=repo,
            expected_repository="CongBao/stock-desk",
            expected_ref="refs/heads/main",
        )
        _verify_controlled_gh_proof_output(
            _load_strict_json(
                inputs.proof_gh_verification_path,
                label="controlled GitHub proof verification evidence",
            ),
            proof_sha256=hashlib.sha256(proof_bytes).hexdigest(),
            commit_sha=str(proof_value.get("commit_sha", "")),
        )
        verify_post_gh_attestation_binding(
            proof_value,
            proof_bytes=proof_bytes,
            binding_value=_load_strict_json(
                inputs.proof_verification_binding_path,
                label="main proof post-gh-verify binding",
            ),
            expected_repository="CongBao/stock-desk",
        )
        verify_proved_artifacts(
            proof_value,
            artifact_roots=inputs.artifact_roots,
            artifact_attestations={
                name: _load_strict_json(path, label=f"{name} attestation")
                for name, path in inputs.artifact_attestation_paths.items()
            },
        )
    except (MainValidationProofError, OSError) as error:
        raise ReleaseVerificationError(
            f"proved release input verification failed: {error}"
        ) from error
    return proof_value


def _is_safe_github_branch(branch: str) -> bool:
    components = branch.split("/")
    return ".." not in branch and all(
        component
        and not component.startswith((".", "-"))
        and not component.endswith((".", ".lock"))
        for component in components
    )


def _is_github_merge_identity(
    parents: str,
    author_name: str,
    author_email: str,
    committer_name: str,
    committer_email: str,
    subject: str,
) -> bool:
    subject_match = GITHUB_MERGE_SUBJECT_PATTERN.fullmatch(subject)
    return (
        len(parents.split()) == 2
        and author_name in GITHUB_MERGE_AUTHOR_NAMES
        and author_email == EXPECTED_GIT_EMAIL
        and (committer_name, committer_email) == GITHUB_MERGE_COMMITTER
        and subject_match is not None
        and _is_safe_github_branch(subject_match.group("branch"))
    )


def check_identity(repo: Path) -> None:
    configured = (
        _git(repo, "config", "--get", "user.name").strip(),
        _git(repo, "config", "--get", "user.email").strip(),
    )
    if configured != (EXPECTED_GIT_NAME, EXPECTED_GIT_EMAIL):
        raise ReleaseVerificationError(
            "configured Git identity is not the release identity"
        )

    history = _git(
        repo,
        "log",
        "--format=%P%x00%an%x00%ae%x00%cn%x00%ce%x00%s",
        "HEAD",
    )
    expected = (
        EXPECTED_GIT_NAME,
        EXPECTED_GIT_EMAIL,
        EXPECTED_GIT_NAME,
        EXPECTED_GIT_EMAIL,
    )
    for record in history.splitlines():
        fields = record.split("\0")
        if len(fields) != 6:
            raise ReleaseVerificationError(
                "reachable commit identities are not all the release identity"
            )
        parents, author_name, author_email, committer_name, committer_email, subject = (
            fields
        )
        if (
            author_name,
            author_email,
            committer_name,
            committer_email,
        ) == expected:
            continue
        if _is_github_merge_identity(
            parents,
            author_name,
            author_email,
            committer_name,
            committer_email,
            subject,
        ):
            continue
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


def check_changelog(repo: Path, version: str, *, tag_name: str | None = None) -> None:
    try:
        changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseVerificationError(
            "unable to read the release changelog"
        ) from error
    prerelease_tag_pattern = rf"v{re.escape(version)}-(?:alpha|beta)\.[1-9][0-9]*"
    if tag_name is not None and re.fullmatch(prerelease_tag_pattern, tag_name):
        if changelog.count("## [Unreleased]") != 1 or changelog.count(tag_name) != 1:
            raise ReleaseVerificationError(
                "prerelease changelog entry is not uniquely recorded under Unreleased"
            )
        release_note = repo / "docs" / "releases" / f"{tag_name}.md"
        try:
            note = release_note.read_text(encoding="utf-8")
        except OSError as error:
            raise ReleaseVerificationError(
                "prerelease release note is missing"
            ) from error
        if (
            f"# Stock Desk {tag_name}" not in note
            or "unsigned prerelease" not in note.casefold()
        ):
            raise ReleaseVerificationError(
                "prerelease release note must identify an unsigned prerelease"
            )
        return
    if tag_name is not None and tag_name != f"v{version}":
        raise ReleaseVerificationError(
            "release tag name is not allowed for this version"
        )
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
    _scan_reachable_git_blobs(repo)
    _scan_git_archive(repo)


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


def _read_bound_repository_file(
    repo: Path, relative_path: PurePosixPath
) -> bytes | None:
    candidate = repo
    for part in relative_path.parts:
        candidate /= part
        if candidate.is_symlink():
            return None
    if not candidate.is_file():
        return None
    return candidate.read_bytes()


def _check_wheel_artifact(repo: Path, wheel_path: Path, version: str) -> bytes:
    dist_info = f"stock_desk-{version}.dist-info"
    metadata_path = f"{dist_info}/METADATA"
    wheel_metadata_path = f"{dist_info}/WHEEL"
    record_path = f"{dist_info}/RECORD"
    license_path = f"{dist_info}/licenses/LICENSE"
    required_members = {
        "stock_desk/__init__.py",
        metadata_path,
        wheel_metadata_path,
        record_path,
        license_path,
    }
    expected_package_payloads = _expected_wheel_package_payloads(repo)
    allowed_members = set(expected_package_payloads) | {
        metadata_path,
        wheel_metadata_path,
        record_path,
        license_path,
    }
    with zipfile.ZipFile(wheel_path) as wheel:
        members = wheel.namelist()
        if (
            wheel.testzip() is not None
            or len(members) != len(set(members))
            or not required_members.issubset(members)
            or set(members) != allowed_members
            or any(member.is_dir() for member in wheel.infolist())
        ):
            raise _invalid_wheel()
        for member in wheel.infolist():
            with wheel.open(member) as payload:
                _scan_stream(payload, label="wheel member")
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
        repository_license = repo / "LICENSE"
        if (
            package_payloads != expected_package_payloads
            or repository_license.is_symlink()
            or not repository_license.is_file()
            or archive_payloads[license_path] != repository_license.read_bytes()
        ):
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


def _safe_sdist_relative_path(name: str, root: str) -> PurePosixPath | None:
    prefix = f"{root}/"
    if not name.startswith(prefix):
        return None
    relative_name = name.removeprefix(prefix)
    relative_path = PurePosixPath(relative_name)
    if (
        not relative_name
        or "\\" in relative_name
        or "\x00" in relative_name
        or ":" in relative_name
        or relative_path.is_absolute()
        or ".." in relative_path.parts
        or relative_path.as_posix() != relative_name
    ):
        return None
    return relative_path


def _check_source_artifact(repo: Path, source_path: Path, version: str) -> bytes:
    root = f"stock_desk-{version}"
    required_relative_paths = {
        "pyproject.toml",
        "PKG-INFO",
        "src/stock_desk/__init__.py",
    }
    with tarfile.open(source_path, "r:gz") as source:
        members = source.getmembers()
        members_by_name = {member.name: member for member in members}
        if len(members) != len(members_by_name):
            raise _invalid_source()
        member_kinds: dict[PurePosixPath, bool] = {}
        regular_members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for member in members:
            if member.name == root:
                if not member.isdir():
                    raise _invalid_source()
                continue
            relative_path = _safe_sdist_relative_path(member.name, root)
            if relative_path is None:
                raise _invalid_source()
            is_file = member.isfile()
            if not member.isdir() and not is_file:
                raise _invalid_source()
            if relative_path in member_kinds:
                raise _invalid_source()
            member_kinds[relative_path] = is_file
            if is_file:
                regular_members.append((member, relative_path))
        for relative_path in member_kinds:
            if any(
                member_kinds.get(parent) is True for parent in relative_path.parents
            ):
                raise _invalid_source()

        archive_payloads: dict[str, bytes] = {}
        for member, relative_path in regular_members:
            member_file = source.extractfile(member)
            if member_file is None:
                raise _invalid_source()
            payload = member_file.read()
            relative_name = relative_path.as_posix()
            scanner = ReleaseLeakScanner(label="source distribution member")
            scanner.feed(payload)
            scanner.finish()
            archive_payloads[relative_name] = payload
            if relative_name != "PKG-INFO":
                repository_payload = _read_bound_repository_file(repo, relative_path)
                if repository_payload is None or payload != repository_payload:
                    raise _invalid_source()
        if not required_relative_paths.issubset(archive_payloads):
            raise _invalid_source()
        pyproject_payload = archive_payloads["pyproject.toml"]
        _check_sdist_pyproject(pyproject_payload, version)
        metadata_payload = archive_payloads["PKG-INFO"]
        _check_package_metadata(metadata_payload, version, "source")
        package_payloads = {
            f"{root}/{relative_name}": payload
            for relative_name, payload in archive_payloads.items()
            if relative_name.startswith("src/stock_desk/")
        }
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
        for web_path in sorted(web_entrypoint.parent.rglob("*")):
            relative = web_path.relative_to(web_entrypoint.parent).as_posix()
            if not _safe_release_member_path(relative) or web_path.is_symlink():
                raise ReleaseVerificationError(
                    "release web build artifact contains an unsafe path"
                )
            if web_path.is_file():
                _scan_path(web_path, label="web build member")
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


def verify_candidate(
    repo: Path,
    version: str,
    runner: GateRunner,
    *,
    report_path: Path,
    target_performance: bool = False,
    fingerprint: Callable[[Path], str] = compute_source_fingerprint,
    fixture_hashes: Callable[[Path], dict[str, str]] = compute_fixture_hashes,
    proved_inputs: ProvedReleaseInputs | None = None,
) -> None:
    resolved_repo = repo.resolve(strict=True)
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseVerificationError(
            "release version must be a stable numeric version"
        )
    try:
        revision = _git(resolved_repo, "rev-parse", "HEAD").strip()
        initial_fingerprint = fingerprint(resolved_repo)
        initial_fixture_hashes = fixture_hashes(resolved_repo)
    except ReleaseVerificationError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseVerificationError(
            "unable to initialize release candidate verification"
        ) from error

    precheck_error: ReleaseVerificationError | None = None
    source_unchanged = True
    try:
        check_clean_worktree(resolved_repo)
    except ReleaseVerificationError as error:
        precheck_error = error
        source_unchanged = False
    if precheck_error is None:
        try:
            check_public_history(resolved_repo)
        except ReleaseVerificationError as error:
            precheck_error = error
    if precheck_error is not None:
        _write_candidate_report(
            resolved_repo,
            report_path,
            {
                "schema_version": CANDIDATE_REPORT_SCHEMA,
                "mode": "candidate",
                "version": version,
                "status": "failed",
                "source_revision": revision,
                "source_fingerprint": initial_fingerprint,
                "source_unchanged": source_unchanged,
                "fixture_hashes": initial_fixture_hashes,
                "gates": [],
                "failure": {
                    "kind": "precheck_failed",
                    "gate": None,
                    "message": "release candidate precheck failed",
                },
            },
        )
        raise precheck_error

    gate_reports: list[dict[str, object]] = []
    failure: dict[str, object] | None = None
    candidate_gates = (
        ()
        if proved_inputs is not None
        else _candidate_gates(target_performance=target_performance)
    )
    if proved_inputs is not None:
        try:
            verify_proved_release_inputs(resolved_repo, proved_inputs)
        except ReleaseVerificationError:
            failure = {
                "kind": "proved_inputs_failed",
                "gate": ["reuse-main-validation-proof"],
                "message": "proved release inputs failed verification",
            }
            gate_reports.append(
                {"command": ["reuse-main-validation-proof"], "status": "failed"}
            )
        else:
            gate_reports.append(
                {"command": ["reuse-main-validation-proof"], "status": "passed"}
            )
    for gate in candidate_gates:
        gate_error: BaseException | None = None
        try:
            runner.run(gate)
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            gate_error = error

        source_unchanged = True
        try:
            check_clean_worktree(resolved_repo)
            current_revision = _git(resolved_repo, "rev-parse", "HEAD").strip()
            current_fingerprint = fingerprint(resolved_repo)
            current_fixture_hashes = fixture_hashes(resolved_repo)
            source_unchanged = (
                current_revision == revision
                and current_fingerprint == initial_fingerprint
                and current_fixture_hashes == initial_fixture_hashes
            )
        except (OSError, RuntimeError, ValueError, ReleaseVerificationError):
            source_unchanged = False

        command = list(gate.command)
        if not source_unchanged:
            gate_reports.append({"command": command, "status": "failed"})
            failure = {
                "kind": "source_changed",
                "gate": command,
                "message": "release candidate gate modified release sources",
            }
        elif gate_error is not None:
            gate_reports.append({"command": command, "status": "failed"})
            failure = {
                "kind": "gate_failed",
                "gate": command,
                "message": "release candidate gate failed",
            }
        else:
            gate_reports.append({"command": command, "status": "passed"})
        if failure is not None:
            break

    source_unchanged = failure is None or failure["kind"] != "source_changed"
    payload = {
        "schema_version": CANDIDATE_REPORT_SCHEMA,
        "mode": "candidate",
        "version": version,
        "status": "passed" if failure is None else "failed",
        "source_revision": revision,
        "source_fingerprint": initial_fingerprint,
        "source_unchanged": source_unchanged,
        "fixture_hashes": initial_fixture_hashes,
        "gates": gate_reports,
        "failure": failure,
    }
    _write_candidate_report(resolved_repo, report_path, payload)
    if failure is not None:
        if failure["kind"] == "source_changed":
            raise ReleaseVerificationError(
                "release candidate gate modified release sources"
            )
        raise ReleaseVerificationError("release candidate gate failed")


def verify_release(
    repo: Path,
    version: str,
    runner: GateRunner,
    *,
    fingerprint: Callable[[Path], str] = compute_source_fingerprint,
    proved_inputs: ProvedReleaseInputs | None = None,
    tag_name: str | None = None,
) -> None:
    resolved_repo = repo.resolve(strict=True)
    check_clean_worktree(resolved_repo)
    if proved_inputs is None:
        check_branch(resolved_repo)
    check_identity(resolved_repo)
    check_remote(resolved_repo)
    check_versions(resolved_repo, version)
    check_changelog(resolved_repo, version, tag_name=tag_name)
    check_public_history(resolved_repo)
    try:
        initial_fingerprint = fingerprint(resolved_repo)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReleaseVerificationError(
            "unable to fingerprint release sources"
        ) from error

    gates = (
        (
            PRE_PUBLISH_EVIDENCE_GATE,
            GateCommand(("make", "release-check"), timeout_seconds=1800),
            GateCommand(
                ("pnpm", "e2e"),
                timeout_seconds=600,
                environment=(("STOCK_DESK_E2E_BASE_URL", E2E_BASE_URL),),
            ),
        )
        if proved_inputs is None
        else ()
    )
    if proved_inputs is not None:
        check_exact_release_tag(resolved_repo, version, tag_name=tag_name)
        verify_proved_release_inputs(resolved_repo, proved_inputs)
    elif tag_name is not None:
        raise ReleaseVerificationError("--tag requires exact-SHA proved release inputs")
    for gate in gates:
        try:
            runner.run(gate)
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            raise ReleaseVerificationError("release gate failed") from error

    if proved_inputs is None:
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
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="run release-candidate gates and write a machine-readable report",
    )
    parser.add_argument(
        "--target-performance",
        action="store_true",
        help="use the target-hardware performance gate in candidate mode",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("test-results/release/candidate.json"),
        help="candidate JSON report path inside test-results/release",
    )
    parser.add_argument(
        "--main-proof",
        type=Path,
        help="exact-SHA immutable main proof to reuse instead of source test reruns",
    )
    parser.add_argument(
        "--main-proof-verification-binding",
        type=Path,
        help="post-gh-verify identity binding for --main-proof (not a signature)",
    )
    parser.add_argument(
        "--main-proof-gh-verification",
        type=Path,
        help="JSON output from the controlled gh attestation verify step",
    )
    parser.add_argument(
        "--tag",
        help="exact stable or supported prerelease tag expected to point at the proved commit",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME=ROOT",
        help="downloaded proved artifact root; repeat for every proof artifact",
    )
    parser.add_argument(
        "--artifact-attestation",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="attestation binding for a proved artifact; repeat for every artifact",
    )
    return parser


def _named_paths(values: list[str], *, label: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not raw_path or name in result:
            raise ReleaseVerificationError(
                f"{label} values must uniquely use NAME=PATH"
            )
        result[name] = Path(raw_path)
    return result


def _proved_inputs_from_options(
    options: argparse.Namespace,
) -> ProvedReleaseInputs | None:
    supplied = any(
        (
            options.main_proof is not None,
            options.main_proof_verification_binding is not None,
            options.main_proof_gh_verification is not None,
            bool(options.artifact),
            bool(options.artifact_attestation),
        )
    )
    if not supplied:
        return None
    if (
        options.main_proof is None
        or options.main_proof_verification_binding is None
        or options.main_proof_gh_verification is None
    ):
        raise ReleaseVerificationError(
            "proved release reuse requires proof, post-gh-verify binding, and controlled gh verification evidence"
        )
    roots = _named_paths(options.artifact, label="--artifact")
    attestations = _named_paths(
        options.artifact_attestation, label="--artifact-attestation"
    )
    if not roots or set(roots) != set(attestations):
        raise ReleaseVerificationError(
            "proved release reuse requires matching artifact roots and attestations"
        )
    return ProvedReleaseInputs(
        proof_path=options.main_proof,
        proof_verification_binding_path=options.main_proof_verification_binding,
        proof_gh_verification_path=options.main_proof_gh_verification,
        artifact_roots=roots,
        artifact_attestation_paths=attestations,
    )


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    repo = Path(__file__).resolve().parent.parent
    try:
        proved_inputs = _proved_inputs_from_options(options)
        if options.tag is not None and proved_inputs is None:
            raise ReleaseVerificationError(
                "--tag requires exact-SHA proved release inputs"
            )
        if options.candidate and options.tag is not None:
            raise ReleaseVerificationError("--tag is only valid for final release mode")
        if options.candidate:
            report_path = (
                options.report
                if options.report.is_absolute()
                else repo / options.report
            )
            if proved_inputs is None:
                verify_candidate(
                    repo,
                    options.version,
                    SubprocessGateRunner(repo),
                    report_path=report_path,
                    target_performance=options.target_performance,
                )
            else:
                verify_candidate(
                    repo,
                    options.version,
                    SubprocessGateRunner(repo),
                    report_path=report_path,
                    target_performance=options.target_performance,
                    proved_inputs=proved_inputs,
                )
        else:
            if options.target_performance or options.report != Path(
                "test-results/release/candidate.json"
            ):
                raise ReleaseVerificationError(
                    "candidate report options require --candidate"
                )
            if proved_inputs is None:
                verify_release(repo, options.version, SubprocessGateRunner(repo))
            else:
                verify_release(
                    repo,
                    options.version,
                    SubprocessGateRunner(repo),
                    proved_inputs=proved_inputs,
                    tag_name=options.tag,
                )
    except ReleaseVerificationError as error:
        print(f"Release verification failed: {error}", file=sys.stderr)
        return 1
    label = "candidate " if options.candidate else ""
    print(f"Release {label}verification passed for {options.version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
