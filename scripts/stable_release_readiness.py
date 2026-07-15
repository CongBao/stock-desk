"""Fail-closed aggregation of every external gate for a stable v1.1 release.

The readiness manifest is only an index.  A digest, a claimed boolean, or a
successful-looking JSON fixture is never evidence: every indexed receipt is
hashed, semantically verified, and verified with a GitHub artifact attestation
issued by a fixed workflow for the exact protected-main commit.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from typing import Final, TypedDict, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from scripts import deployment_latency
from scripts.check_requirement_coverage import (
    load_manifest as load_requirement_manifest,
)
from scripts.trusted_updater_release import (
    AuthenticodeEvidence,
    TrustedUpdaterReleaseError,
    _verify_signpath_receipt,
    _verify_windows_receipt,
)


ROOT: Final = Path(__file__).resolve().parents[1]
SCHEMA: Final = "stock-desk-stable-release-readiness-v1"
REPOSITORY: Final = "CongBao/stock-desk"
UPDATER_PUBLIC_KEY: Final = ROOT / "config/tauri-updater-public-key.pub"
RELEASE_AUDITOR_PUBLIC_KEY_PATH: Final = "config/release-auditor-public-key.pem"
RELEASE_TAG_ALLOWED_SIGNERS_PATH: Final = "config/release-tag-allowed-signers"
PUBLIC_REQUIREMENTS_AUTHORITY: Final = "tests/acceptance/requirements.yml"
V11_REQUIREMENTS_AUTHORITY: Final = "tests/acceptance/v1_1_requirements.yml"
SHA1: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
SEMVER: Final = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
MAX_JSON_BYTES: Final = 8 * 1024 * 1024
MAX_INSTALLER_BYTES: Final = 512 * 1024 * 1024
MAX_RELATIVE_PATH_LENGTH: Final = 1024
LATENCY_CATEGORIES: Final = frozenset(
    {
        "typical-pr",
        "high-risk-pr",
        "main",
        "candidate",
        "signpath-queue",
        "proved-tag-to-release",
    }
)
WINDOWS_CASE_IDS: Final = frozenset(
    {
        "win10-22h2-dpi-100",
        "win10-22h2-dpi-125",
        "win10-22h2-dpi-150",
        "win10-22h2-dpi-175",
        "win10-22h2-dpi-200",
        "win10-22h2-dpi-100-webview-offline",
        "win11-dpi-100",
        "win11-dpi-125",
        "win11-dpi-150",
        "win11-dpi-175",
        "win11-dpi-200",
    }
)
NSIS_CASE_RESULTS: Final = {
    "non-admin-current-user": "passed",
    "read-only-install-directory": "passed",
    "chinese-username-spaced-profile": "passed",
    "no-uac-or-elevation": "passed",
    "extraction-ancestor-reparse-race": "blocked",
}
ARTIFACT_WORKFLOWS: Final = {
    "nsis_control_proof": ".github/workflows/windows-installed.yml",
    "signpath_receipt": ".github/workflows/signpath.yml",
    "windows_acceptance_receipt": ".github/workflows/windows-installed.yml",
    "windows_10_trust_receipt": ".github/workflows/windows-installed.yml",
    "windows_11_trust_receipt": ".github/workflows/windows-installed.yml",
    "windows_ux_evidence": ".github/workflows/windows-installed.yml",
    "updater_key_ceremony": ".github/workflows/signpath.yml",
    "latency_ledger": ".github/workflows/release.yml",
    "latency_seal": ".github/workflows/release.yml",
    "latency_report": ".github/workflows/release.yml",
    "final_wiki_evidence": ".github/workflows/release.yml",
    "requirements_completion_evidence": ".github/workflows/release.yml",
    "openspec_completion_evidence": ".github/workflows/release.yml",
}
REQUIREMENT_IDS: Final = frozenset(
    f"R-{number:03d}" for number in range(1, 53) if number not in {6, 8, 9}
)
NON_GOAL_IDS: Final = frozenset({"N-001", "N-002", "N-003"})
PRE_RELEASE_OPENSPEC_TASK_IDS: Final = frozenset(
    [f"1.{number}" for number in range(1, 12)]
    + [f"2.{number}" for number in range(1, 7)]
    + [f"3.{number}" for number in range(1, 8)]
    + [f"4.{number}" for number in range(1, 20)]
)
POST_RELEASE_OPENSPEC_TASK_IDS: Final = frozenset({"4.20"})


class StableReleaseReadinessError(ValueError):
    """Raised when any stable-release authority is missing or untrustworthy."""


class StableReleaseDecision(TypedDict):
    eligible: bool
    version: str
    source_sha: str
    source_tree: str
    release_tag: str
    tag_object_sha: str
    candidate_manifest_sha256: str
    candidate_sha256: str
    evidence_count: int


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StableReleaseReadinessError(
                f"duplicate JSON field is forbidden: {key}"
            )
        result[key] = value
    return result


def _read_regular(path: Path, label: str, *, limit: int = MAX_JSON_BYTES) -> bytes:
    descriptor = -1
    stream = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise StableReleaseReadinessError(f"{label} must be a bounded regular file")
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = -1
        payload = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        try:
            path_after = os.lstat(path)
        except OSError as error:
            raise StableReleaseReadinessError(
                f"{label} path changed while being read"
            ) from error
        if len(payload) > limit or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise StableReleaseReadinessError(f"{label} changed while being read")
        if (
            stat.S_ISLNK(path_after.st_mode)
            or bool(
                getattr(path_after, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise StableReleaseReadinessError(f"{label} path changed while being read")
        return payload
    except (OSError, ValueError) as error:
        if isinstance(error, StableReleaseReadinessError):
            raise
        raise StableReleaseReadinessError(f"cannot safely read {label}") from error
    finally:
        if stream is not None:
            stream.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _decode_json(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StableReleaseReadinessError(
            f"{label} must be strict UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise StableReleaseReadinessError(f"{label} must contain a JSON object")
    return value


def _read_json(path: Path, label: str) -> dict[str, object]:
    return _decode_json(_read_regular(path, label), label)


def _exact(value: Mapping[str, object], fields: set[str], label: str) -> None:
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing:
        raise StableReleaseReadinessError(
            f"{label} missing required field: {missing[0]}"
        )
    if extra:
        raise StableReleaseReadinessError(f"{label} has unknown field: {extra[0]}")


def _git_id(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA1.fullmatch(value) is None:
        raise StableReleaseReadinessError(f"{label} must be a lowercase 40-hex Git id")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise StableReleaseReadinessError(f"{label} must be a lowercase SHA-256")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StableReleaseReadinessError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StableReleaseReadinessError(f"{label} must be a non-negative integer")
    return value


def _safe_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise StableReleaseReadinessError(f"{label} must be a safe relative POSIX path")
    if len(value) > MAX_RELATIVE_PATH_LENGTH:
        raise StableReleaseReadinessError(
            f"{label} must contain at most {MAX_RELATIVE_PATH_LENGTH} characters"
        )
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise StableReleaseReadinessError(f"{label} must be a safe relative POSIX path")
    return Path(*parsed.parts)


def _resolve_under(root: Path, relative: Path, label: str) -> Path:
    root_resolved = root.resolve(strict=True)
    candidate = root_resolved.joinpath(relative)
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as error:
        raise StableReleaseReadinessError(f"{label} parent is unavailable") from error
    if parent != root_resolved and root_resolved not in parent.parents:
        raise StableReleaseReadinessError(f"{label} escapes the evidence root")
    return candidate


def _verify_github_attestation(
    subject: Path, bundle: Path, source_sha: str, signer_workflow: str
) -> None:
    command = [
        "gh",
        "attestation",
        "verify",
        str(subject),
        "--bundle",
        str(bundle),
        "--repo",
        REPOSITORY,
        "--source-digest",
        source_sha,
        "--source-ref",
        "refs/heads/main",
        "--signer-digest",
        source_sha,
        "--signer-workflow",
        f"{REPOSITORY}/{signer_workflow}",
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StableReleaseReadinessError(
            "GitHub attestation verification unavailable"
        ) from error
    if result.returncode != 0:
        raise StableReleaseReadinessError("GitHub attestation verification failed")
    try:
        verification = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise StableReleaseReadinessError(
            "GitHub attestation verification returned invalid JSON"
        ) from error
    if not isinstance(verification, (dict, list)) or not verification:
        raise StableReleaseReadinessError(
            "GitHub attestation verification returned no verified subject"
        )


def _verify_annotated_tag(
    release_tag: str, tag_object_sha: str, source_sha: str
) -> None:
    ref = f"refs/tags/{release_tag}"
    commands = (
        (["git", "-C", os.fspath(ROOT), "cat-file", "-t", ref], "tag"),
        (["git", "-C", os.fspath(ROOT), "rev-parse", ref], tag_object_sha),
        (
            ["git", "-C", os.fspath(ROOT), "rev-parse", f"{ref}^{{commit}}"],
            source_sha,
        ),
    )
    for command, expected in commands:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise StableReleaseReadinessError(
                "annotated release tag verification unavailable"
            ) from error
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise StableReleaseReadinessError(
                "release tag is not an annotated tag for the exact source commit"
            )
    allowed_signers = _read_tracked_source_file(
        source_sha, RELEASE_TAG_ALLOWED_SIGNERS_PATH
    )
    with tempfile.TemporaryDirectory(
        prefix="stock-desk-readiness-tag-signers-"
    ) as temporary:
        private_root = Path(temporary)
        private_root.chmod(0o700)
        allowed_path = private_root / "allowed-signers"
        descriptor = os.open(allowed_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(allowed_signers)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            verification = subprocess.run(
                [
                    "git",
                    "-C",
                    os.fspath(ROOT),
                    "-c",
                    "gpg.format=ssh",
                    "-c",
                    f"gpg.ssh.allowedSignersFile={allowed_path}",
                    "verify-tag",
                    "--raw",
                    ref,
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise StableReleaseReadinessError(
                "release tag signature verification unavailable"
            ) from error
    if verification.returncode != 0:
        raise StableReleaseReadinessError(
            "release tag signature is not valid for the pinned CongBao signer"
        )


def _read_tracked_source_file(source_sha: str, relative_path: str) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(ROOT),
                "show",
                f"{source_sha}:{relative_path}",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StableReleaseReadinessError(
            f"cannot read trusted source blob: {relative_path}"
        ) from error
    if result.returncode != 0 or len(result.stdout) > MAX_JSON_BYTES:
        raise StableReleaseReadinessError(
            f"trusted source blob is missing or oversized: {relative_path}"
        )
    return result.stdout


def _load_artifacts(
    manifest: Mapping[str, object], evidence_root: Path, source_sha: str
) -> dict[str, tuple[bytes, dict[str, object]]]:
    raw = manifest["artifacts"]
    if not isinstance(raw, dict):
        raise StableReleaseReadinessError("artifacts must be an object")
    _exact(raw, set(ARTIFACT_WORKFLOWS), "artifacts")
    loaded: dict[str, tuple[bytes, dict[str, object]]] = {}
    used_paths: set[Path] = set()
    for name, workflow in ARTIFACT_WORKFLOWS.items():
        ref = raw[name]
        if not isinstance(ref, dict):
            raise StableReleaseReadinessError(f"artifacts.{name} must be an object")
        _exact(
            ref,
            {"path", "sha256", "attestation_path", "attestation_sha256"},
            f"artifacts.{name}",
        )
        subject = _resolve_under(
            evidence_root, _safe_relative(ref["path"], f"artifacts.{name}.path"), name
        )
        bundle = _resolve_under(
            evidence_root,
            _safe_relative(
                ref["attestation_path"], f"artifacts.{name}.attestation_path"
            ),
            f"{name} attestation",
        )
        if subject in used_paths or bundle in used_paths or subject == bundle:
            raise StableReleaseReadinessError(
                "evidence and attestation paths must be unique"
            )
        used_paths.update({subject, bundle})
        subject_bytes = _read_regular(subject, name)
        bundle_bytes = _read_regular(bundle, f"{name} attestation")
        if hashlib.sha256(subject_bytes).hexdigest() != _digest(
            ref["sha256"], f"artifacts.{name}.sha256"
        ):
            raise StableReleaseReadinessError(f"{name} digest does not match its bytes")
        if hashlib.sha256(bundle_bytes).hexdigest() != _digest(
            ref["attestation_sha256"], f"artifacts.{name}.attestation_sha256"
        ):
            raise StableReleaseReadinessError(
                f"{name} attestation digest does not match its bytes"
            )
        with tempfile.TemporaryDirectory(
            prefix="stock-desk-readiness-attestation-"
        ) as temporary:
            private_root = Path(temporary)
            private_root.chmod(0o700)
            private_subject = private_root / subject.name
            private_bundle = private_root / bundle.name
            for target, payload in (
                (private_subject, subject_bytes),
                (private_bundle, bundle_bytes),
            ):
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            _verify_github_attestation(
                private_subject, private_bundle, source_sha, workflow
            )
        loaded[name] = (subject_bytes, _decode_json(subject_bytes, name))
    return loaded


def _identity(
    record: Mapping[str, object],
    source_sha: str,
    source_tree: str,
    main_proof: str,
    candidate: str,
    label: str,
) -> None:
    if (
        record.get("source_sha") != source_sha
        or record.get("source_tree") != source_tree
        or record.get("main_proof_sha256") != main_proof
        or record.get("candidate_sha256") != candidate
    ):
        raise StableReleaseReadinessError(f"{label} is not exact-SHA bound")


def _verify_nsis(
    record: Mapping[str, object],
    *,
    source_sha: str,
    source_tree: str,
    main_proof: str,
    candidate_manifest: str,
    candidate: str,
) -> None:
    _exact(
        record,
        {
            "schema",
            "evidence_kind",
            "source_sha",
            "source_tree",
            "main_proof_sha256",
            "candidate_manifest_sha256",
            "candidate_sha256",
            "verifier",
            "run_id",
            "run_attempt",
            "cases",
        },
        "NSIS control proof",
    )
    _identity(
        record, source_sha, source_tree, main_proof, candidate, "NSIS control proof"
    )
    if record["candidate_manifest_sha256"] != candidate_manifest:
        raise StableReleaseReadinessError(
            "NSIS control proof is not bound to the proved candidate manifest"
        )
    cases = record["cases"]
    if (
        record["schema"] != "stock-desk-nsis-installation-control-proof-v1"
        or record["evidence_kind"] != "observed-windows-install-control"
        or record["verifier"] != "external-protected-windows-controller"
        or _positive_int(record["run_id"], "NSIS run_id") < 1
        or _positive_int(record["run_attempt"], "NSIS run_attempt") != 1
        or not isinstance(cases, list)
    ):
        raise StableReleaseReadinessError(
            "NSIS control proof is not authoritative observed evidence"
        )
    observed: dict[str, str] = {}
    for item in cases:
        if not isinstance(item, dict):
            raise StableReleaseReadinessError("NSIS cases must be objects")
        _exact(item, {"case_id", "result", "observation_sha256"}, "NSIS case")
        case_id = item["case_id"]
        if not isinstance(case_id, str) or case_id in observed:
            raise StableReleaseReadinessError(
                "NSIS case identity is invalid or duplicated"
            )
        _digest(item["observation_sha256"], "NSIS observation_sha256")
        if not isinstance(item["result"], str):
            raise StableReleaseReadinessError("NSIS case result is invalid")
        observed[case_id] = item["result"]
    if observed != NSIS_CASE_RESULTS:
        raise StableReleaseReadinessError(
            "NSIS control proof is incomplete, skipped, or failed"
        )


def _verify_windows_acceptance(
    record: Mapping[str, object],
    *,
    source_sha: str,
    source_tree: str,
    main_proof: str,
    candidate: str,
) -> None:
    fields = {
        "schema",
        "artifact",
        "evidence_kind",
        "source_sha",
        "source_tree",
        "main_proof_sha256",
        "candidate_sha256",
        "webview_installer_sha256",
        "snapshot_policy_sha256",
        "adapter_sha256",
        "broker_public_key_sha256",
        "repository",
        "workflow",
        "workflow_ref",
        "workflow_sha256",
        "run_id",
        "run_attempt",
        "case_receipts",
        "status",
    }
    _exact(record, fields, "Windows acceptance receipt")
    _identity(
        record,
        source_sha,
        source_tree,
        main_proof,
        candidate,
        "Windows acceptance receipt",
    )
    if (
        record["schema"] != "stock-desk-windows-installed-acceptance-receipt-v2"
        or record["artifact"] != "windows-installed-acceptance-receipt"
        or record["evidence_kind"] != "observed-windows-vm"
        or record["repository"] != REPOSITORY
        or record["workflow"] != "Windows Installed Acceptance"
        or record["workflow_ref"]
        != f"{REPOSITORY}/.github/workflows/windows-installed.yml@refs/heads/main"
        or record["status"] != "accepted"
        or _positive_int(record["run_attempt"], "Windows acceptance run_attempt") != 1
    ):
        raise StableReleaseReadinessError(
            "Windows acceptance is not an authoritative first-attempt receipt"
        )
    _positive_int(record["run_id"], "Windows acceptance run_id")
    for field in (
        "webview_installer_sha256",
        "snapshot_policy_sha256",
        "adapter_sha256",
        "broker_public_key_sha256",
        "workflow_sha256",
    ):
        _digest(record[field], f"Windows acceptance {field}")
    cases = record["case_receipts"]
    if not isinstance(cases, list):
        raise StableReleaseReadinessError(
            "Windows acceptance case_receipts must be a list"
        )
    observed: set[str] = set()
    for item in cases:
        if not isinstance(item, dict):
            raise StableReleaseReadinessError(
                "Windows acceptance case receipt must be an object"
            )
        _exact(
            item,
            {"case_id", "derived_sha256", "raw_package_sha256"},
            "Windows acceptance case receipt",
        )
        case_id = item["case_id"]
        if not isinstance(case_id, str) or case_id in observed:
            raise StableReleaseReadinessError("Windows acceptance case is duplicated")
        observed.add(case_id)
        _digest(item["derived_sha256"], "Windows derived receipt")
        _digest(item["raw_package_sha256"], "Windows raw package")
    if observed != WINDOWS_CASE_IDS:
        raise StableReleaseReadinessError(
            "Windows acceptance must contain all eleven real cases"
        )


def _verify_windows_ux(
    record: Mapping[str, object],
    *,
    source_sha: str,
    source_tree: str,
    main_proof: str,
    candidate: str,
) -> None:
    _exact(
        record,
        {
            "schema",
            "evidence_kind",
            "source_sha",
            "source_tree",
            "main_proof_sha256",
            "candidate_sha256",
            "verifier",
            "run_id",
            "run_attempt",
            "first_kline_ready_seconds",
            "first_kline_click_count",
            "case_receipts",
            "result",
        },
        "Windows UX evidence",
    )
    _identity(
        record, source_sha, source_tree, main_proof, candidate, "Windows UX evidence"
    )
    ready = record["first_kline_ready_seconds"]
    clicks = record["first_kline_click_count"]
    if (
        record["schema"] != "stock-desk-windows-ux-evidence-v1"
        or record["evidence_kind"] != "observed-windows-desktop-ux"
        or record["verifier"] != "external-protected-windows-controller"
        or _positive_int(record["run_attempt"], "Windows UX run_attempt") != 1
        or record["result"] != "passed"
        or isinstance(ready, bool)
        or not isinstance(ready, (int, float))
        or not math.isfinite(float(ready))
        or not 0 < float(ready) <= 180
        or isinstance(clicks, bool)
        or not isinstance(clicks, int)
        or not 1 <= clicks <= 5
    ):
        raise StableReleaseReadinessError(
            "Windows UX evidence is failed, skipped, or outside the first-use baseline"
        )
    _positive_int(record["run_id"], "Windows UX run_id")
    cases = record["case_receipts"]
    if not isinstance(cases, list):
        raise StableReleaseReadinessError("Windows UX case receipts must be a list")
    observed: set[str] = set()
    for item in cases:
        if not isinstance(item, dict):
            raise StableReleaseReadinessError(
                "Windows UX case receipt must be an object"
            )
        _exact(
            item,
            {
                "case_id",
                "evidence_sha256",
                "screenshot_sha256",
                "video_sha256",
                "journey_event_sha256",
            },
            "Windows UX case receipt",
        )
        case_id = item["case_id"]
        if not isinstance(case_id, str) or case_id in observed:
            raise StableReleaseReadinessError("Windows UX case is duplicated")
        observed.add(case_id)
        for field in (
            "evidence_sha256",
            "screenshot_sha256",
            "video_sha256",
            "journey_event_sha256",
        ):
            _digest(item[field], f"Windows UX {field}")
    if observed != WINDOWS_CASE_IDS:
        raise StableReleaseReadinessError(
            "Windows UX evidence must cover all eleven cases"
        )


def _verify_key_ceremony(
    record: Mapping[str, object],
    *,
    source_sha: str,
    source_tree: str,
    public_key_path: Path,
) -> None:
    _exact(
        record,
        {
            "schema",
            "evidence_kind",
            "source_sha",
            "source_tree",
            "public_key_sha256",
            "key_id",
            "challenge",
            "challenge_signature",
            "ceremony_evidence_sha256",
            "status",
        },
        "updater key ceremony",
    )
    expected_challenge = (
        f"stock-desk-updater-key-ceremony-v1:{source_sha}:{source_tree}"
    )
    public_bytes = _read_regular(
        public_key_path, "pinned updater public key", limit=4096
    )
    lines = public_bytes.decode("utf-8").splitlines()
    if (
        record["schema"] != "stock-desk-updater-key-ceremony-v1"
        or record["evidence_kind"] != "offline-key-possession-ceremony"
        or record["source_sha"] != source_sha
        or record["source_tree"] != source_tree
        or record["public_key_sha256"] != hashlib.sha256(public_bytes).hexdigest()
        or record["challenge"] != expected_challenge
        or record["status"] != "witnessed"
        or len(lines) != 2
    ):
        raise StableReleaseReadinessError(
            "updater key ceremony is not exact-SHA witnessed evidence"
        )
    _digest(record["ceremony_evidence_sha256"], "updater key ceremony evidence")
    try:
        packet = base64.b64decode(lines[1], validate=True)
        signature = base64.b64decode(
            cast(str, record["challenge_signature"]), validate=True
        )
    except (ValueError, TypeError, binascii.Error) as error:
        raise StableReleaseReadinessError(
            "updater key ceremony signature is invalid"
        ) from error
    if (
        len(packet) != 42
        or packet[:2] not in {b"Ed", b"ED"}
        or len(signature) != 64
        or record["key_id"] != packet[2:10].hex()
    ):
        raise StableReleaseReadinessError("updater key ceremony identity is invalid")
    try:
        Ed25519PublicKey.from_public_bytes(packet[10:]).verify(
            signature, expected_challenge.encode("utf-8")
        )
    except InvalidSignature as error:
        raise StableReleaseReadinessError(
            "updater key ceremony does not prove private-key possession"
        ) from error


def _verify_latency(
    ledger: Mapping[str, object],
    seal: Mapping[str, object],
    report: Mapping[str, object],
) -> None:
    try:
        deployment_latency.validate_ledger(ledger)
        deployment_latency._require_seal(ledger, seal)
        recomputed = deployment_latency.aggregate_ledger(ledger, expected_seal=seal)
    except deployment_latency.DeploymentLatencyError as error:
        raise StableReleaseReadinessError(
            "deployment latency ledger or seal is invalid"
        ) from error
    if recomputed != report:
        raise StableReleaseReadinessError(
            "deployment latency report was not derived from the attested ledger"
        )
    categories = report.get("categories")
    if not isinstance(categories, dict) or set(categories) != LATENCY_CATEGORIES:
        raise StableReleaseReadinessError(
            "deployment latency report lacks all six categories"
        )
    records = ledger.get("records")
    if not isinstance(records, list):
        raise StableReleaseReadinessError(
            "deployment latency ledger records are invalid"
        )
    by_category: dict[str, list[Mapping[str, object]]] = {
        name: [] for name in LATENCY_CATEGORIES
    }
    for raw_record in records:
        if isinstance(raw_record, dict) and isinstance(raw_record.get("sample"), dict):
            sample = cast(dict[str, object], raw_record["sample"])
            category = sample.get("category")
            if isinstance(category, str) and category in by_category:
                by_category[category].append(sample)
    for name in LATENCY_CATEGORIES:
        category = categories[name]
        if not isinstance(category, dict):
            raise StableReleaseReadinessError(f"latency category {name} is invalid")
        active_identity = category.get("active_comparison_identity")
        if (
            category.get("status") != "complete"
            or category.get("minimum_sample_count") != 5
            or category.get("minimum_consecutive_run_count") != 5
            or isinstance(category.get("consecutive_run_count"), bool)
            or not isinstance(category.get("consecutive_run_count"), int)
            or cast(int, category["consecutive_run_count"]) < 5
            or not isinstance(active_identity, dict)
        ):
            raise StableReleaseReadinessError(
                f"latency category {name} lacks five consecutive runs"
            )
        active = [
            sample
            for sample in by_category[name]
            if deployment_latency._comparison_identity(sample) == active_identity
        ]
        run_ids = {str(sample.get("run_id")) for sample in active}
        if len(run_ids) < 5 or any(
            sample.get("outcome") != "success" or sample.get("invalidated") is not False
            for sample in active
        ):
            raise StableReleaseReadinessError(
                f"latency category {name} contains skipped, failed, or invalidated evidence"
            )


def _verify_final_wiki(
    record: Mapping[str, object], *, source_sha: str, source_tree: str
) -> None:
    _exact(
        record,
        {
            "schema",
            "evidence_kind",
            "source_sha",
            "source_tree",
            "wiki_commit",
            "readme_sha256",
            "wiki_manifest_sha256",
            "screenshot_manifest_sha256",
            "screenshot_count",
            "locales",
            "verifier",
            "result",
        },
        "final Wiki evidence",
    )
    if (
        record["schema"] != "stock-desk-final-wiki-evidence-v1"
        or record["evidence_kind"] != "published-bilingual-wiki-and-real-screenshots"
        or record["source_sha"] != source_sha
        or record["source_tree"] != source_tree
        or record["locales"] != ["zh-CN", "en"]
        or record["verifier"] != "stock-desk-docs-final-gate-v1"
        or record["result"] != "passed"
    ):
        raise StableReleaseReadinessError(
            "final Wiki evidence is not exact-SHA final-gate output"
        )
    _git_id(record["wiki_commit"], "Wiki commit")
    for field in (
        "readme_sha256",
        "wiki_manifest_sha256",
        "screenshot_manifest_sha256",
    ):
        _digest(record[field], f"final Wiki {field}")
    if _positive_int(record["screenshot_count"], "final Wiki screenshot_count") < 5:
        raise StableReleaseReadinessError(
            "final Wiki must include all five core workflow screenshots"
        )


def _public_requirements_authority(
    authority_path: str,
    requirement_pattern: str,
    non_goal_pattern: str,
    source_sha: str,
) -> tuple[str, frozenset[str], frozenset[str]]:
    payload = _read_tracked_source_file(source_sha, authority_path)
    with tempfile.TemporaryDirectory(
        prefix="stock-desk-readiness-requirements-"
    ) as temporary:
        private_root = Path(temporary)
        private_root.chmod(0o700)
        private_manifest = private_root / "requirements.yml"
        descriptor = os.open(
            private_manifest,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            authority = load_requirement_manifest(private_manifest)
        except (OSError, ValueError) as error:
            raise StableReleaseReadinessError(
                "public requirements authority is invalid"
            ) from error

    def exact_ids(value: object, pattern: str, label: str) -> frozenset[str]:
        if not isinstance(value, list):
            raise StableReleaseReadinessError(f"{label} must be a list")
        ids: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                raise StableReleaseReadinessError(f"{label} entry must be an object")
            requirement_id = item.get("id")
            status = item.get("status")
            if (
                not isinstance(requirement_id, str)
                or re.fullmatch(pattern, requirement_id) is None
                or status not in {"mapped", "verified"}
            ):
                raise StableReleaseReadinessError(
                    f"{label} contains an inactive or invalid requirement"
                )
            ids.append(requirement_id)
        if len(ids) != len(set(ids)):
            raise StableReleaseReadinessError(f"{label} contains duplicate IDs")
        return frozenset(ids)

    return (
        hashlib.sha256(payload).hexdigest(),
        exact_ids(
            authority.get("requirements"), requirement_pattern, "public requirements"
        ),
        exact_ids(authority.get("non_goals"), non_goal_pattern, "public non-goals"),
    )


def _verify_private_audit_signature(
    record: Mapping[str, object], label: str, source_sha: str
) -> None:
    public_bytes = _read_tracked_source_file(
        source_sha, RELEASE_AUDITOR_PUBLIC_KEY_PATH
    )
    if len(public_bytes) > 64 * 1024:
        raise StableReleaseReadinessError("pinned release auditor key is oversized")
    if (
        record.get("auditor_public_key_sha256")
        != hashlib.sha256(public_bytes).hexdigest()
    ):
        raise StableReleaseReadinessError(
            f"{label} is not signed by the pinned release auditor"
        )
    signature_text = record.get("audit_signature")
    if not isinstance(signature_text, str):
        raise StableReleaseReadinessError(f"{label} audit signature is missing")
    try:
        signature = base64.b64decode(signature_text, validate=True)
        public_key = serialization.load_pem_public_key(public_bytes)
    except (ValueError, binascii.Error) as error:
        raise StableReleaseReadinessError(
            f"{label} auditor key or signature is invalid"
        ) from error
    if not isinstance(public_key, Ed25519PublicKey) or len(signature) != 64:
        raise StableReleaseReadinessError(
            f"{label} must use a pinned Ed25519 auditor signature"
        )
    signed_record = dict(record)
    del signed_record["audit_signature"]
    try:
        signed_payload = json.dumps(
            signed_record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        public_key.verify(signature, signed_payload)
    except (InvalidSignature, TypeError, ValueError) as error:
        raise StableReleaseReadinessError(
            f"{label} auditor signature verification failed"
        ) from error


def _verify_requirements(
    record: Mapping[str, object],
    *,
    source_sha: str,
    source_tree: str,
    release_tag: str,
    tag_object_sha: str,
) -> None:
    _exact(
        record,
        {
            "schema",
            "evidence_kind",
            "source_sha",
            "source_tree",
            "release_tag",
            "tag_object_sha",
            "private_requirements_sha256",
            "active_requirement_ids",
            "non_goal_ids",
            "public_acceptance_sha256",
            "public_requirement_ids",
            "public_non_goal_ids",
            "public_v11_acceptance_sha256",
            "public_v11_requirement_ids",
            "public_v11_non_goal_ids",
            "verified_requirement_count",
            "verified_public_requirement_count",
            "verified_public_v11_requirement_count",
            "failed_requirement_count",
            "xfail_requirement_count",
            "stale_requirement_count",
            "publication_boundary",
            "verifier",
            "status",
            "auditor_public_key_sha256",
            "audit_signature",
        },
        "requirements completion evidence",
    )
    active = record["active_requirement_ids"]
    non_goals = record["non_goal_ids"]
    public_digest, public_ids, public_non_goals = _public_requirements_authority(
        PUBLIC_REQUIREMENTS_AUTHORITY,
        r"R-[0-9]{3}",
        r"N-[0-9]{3}",
        source_sha,
    )
    v11_digest, v11_ids, v11_non_goals = _public_requirements_authority(
        V11_REQUIREMENTS_AUTHORITY,
        r"V11-R-[0-9]{3}",
        r"V11-N-[0-9]{3}",
        source_sha,
    )
    claimed_public = record["public_requirement_ids"]
    claimed_public_non_goals = record["public_non_goal_ids"]
    claimed_v11 = record["public_v11_requirement_ids"]
    claimed_v11_non_goals = record["public_v11_non_goal_ids"]
    if (
        record["schema"] != "stock-desk-requirements-completion-evidence-v1"
        or record["evidence_kind"] != "redacted-private-requirements-audit"
        or record["source_sha"] != source_sha
        or record["source_tree"] != source_tree
        or record["release_tag"] != release_tag
        or record["tag_object_sha"] != tag_object_sha
        or not isinstance(active, list)
        or len(active) != len(REQUIREMENT_IDS)
        or any(not isinstance(item, str) for item in active)
        or set(cast(list[str], active)) != REQUIREMENT_IDS
        or not isinstance(non_goals, list)
        or len(non_goals) != len(NON_GOAL_IDS)
        or any(not isinstance(item, str) for item in non_goals)
        or set(cast(list[str], non_goals)) != NON_GOAL_IDS
        or record["public_acceptance_sha256"] != public_digest
        or not isinstance(claimed_public, list)
        or len(claimed_public) != len(public_ids)
        or any(not isinstance(item, str) for item in claimed_public)
        or set(cast(list[str], claimed_public)) != public_ids
        or not isinstance(claimed_public_non_goals, list)
        or len(claimed_public_non_goals) != len(public_non_goals)
        or any(not isinstance(item, str) for item in claimed_public_non_goals)
        or set(cast(list[str], claimed_public_non_goals)) != public_non_goals
        or record["public_v11_acceptance_sha256"] != v11_digest
        or not isinstance(claimed_v11, list)
        or len(claimed_v11) != len(v11_ids)
        or any(not isinstance(item, str) for item in claimed_v11)
        or set(cast(list[str], claimed_v11)) != v11_ids
        or not isinstance(claimed_v11_non_goals, list)
        or len(claimed_v11_non_goals) != len(v11_non_goals)
        or any(not isinstance(item, str) for item in claimed_v11_non_goals)
        or set(cast(list[str], claimed_v11_non_goals)) != v11_non_goals
        or record["verified_requirement_count"] != len(REQUIREMENT_IDS)
        or record["verified_public_requirement_count"] != len(public_ids)
        or record["verified_public_v11_requirement_count"] != len(v11_ids)
        or _nonnegative_int(
            record["failed_requirement_count"], "failed_requirement_count"
        )
        != 0
        or _nonnegative_int(
            record["xfail_requirement_count"], "xfail_requirement_count"
        )
        != 0
        or _nonnegative_int(
            record["stale_requirement_count"], "stale_requirement_count"
        )
        != 0
        or record["publication_boundary"] != "private-source-redacted-proof-only"
        or record["verifier"] != "requirements-ledger-final-audit-v1"
        or record["status"] != "complete"
    ):
        raise StableReleaseReadinessError(
            "requirements completion proof does not verify every private and public requirement"
        )
    _digest(record["private_requirements_sha256"], "private requirements source digest")
    _verify_private_audit_signature(record, "requirements completion proof", source_sha)


def _verify_openspec(
    record: Mapping[str, object],
    *,
    source_sha: str,
    source_tree: str,
    release_tag: str,
    tag_object_sha: str,
) -> None:
    _exact(
        record,
        {
            "schema",
            "evidence_kind",
            "source_sha",
            "source_tree",
            "release_tag",
            "tag_object_sha",
            "change_id",
            "tasks_sha256",
            "completed_task_ids",
            "remaining_task_ids",
            "completed_task_count",
            "total_task_count",
            "remaining_task_count",
            "publication_boundary",
            "verifier",
            "status",
            "auditor_public_key_sha256",
            "audit_signature",
        },
        "OpenSpec completion evidence",
    )
    completed = record["completed_task_count"]
    total = record["total_task_count"]
    completed_ids = record["completed_task_ids"]
    remaining_ids = record["remaining_task_ids"]
    if (
        record["schema"] != "stock-desk-openspec-completion-evidence-v1"
        or record["evidence_kind"] != "redacted-private-pre-release-specification-audit"
        or record["source_sha"] != source_sha
        or record["source_tree"] != source_tree
        or record["release_tag"] != release_tag
        or record["tag_object_sha"] != tag_object_sha
        or record["change_id"] != "build-windows-desktop-ux-v1-1"
        or record["publication_boundary"] != "private-source-redacted-proof-only"
        or record["verifier"] != "openspec-strict-pre-release-audit-v1"
        or record["status"] != "pre-release-complete"
        or not isinstance(completed_ids, list)
        or len(completed_ids) != len(PRE_RELEASE_OPENSPEC_TASK_IDS)
        or any(not isinstance(item, str) for item in completed_ids)
        or set(cast(list[str], completed_ids)) != PRE_RELEASE_OPENSPEC_TASK_IDS
        or not isinstance(remaining_ids, list)
        or len(remaining_ids) != len(POST_RELEASE_OPENSPEC_TASK_IDS)
        or any(not isinstance(item, str) for item in remaining_ids)
        or set(cast(list[str], remaining_ids)) != POST_RELEASE_OPENSPEC_TASK_IDS
        or isinstance(completed, bool)
        or not isinstance(completed, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total
        != len(PRE_RELEASE_OPENSPEC_TASK_IDS) + len(POST_RELEASE_OPENSPEC_TASK_IDS)
        or completed != len(PRE_RELEASE_OPENSPEC_TASK_IDS)
        or _nonnegative_int(
            record["remaining_task_count"], "OpenSpec remaining_task_count"
        )
        != len(POST_RELEASE_OPENSPEC_TASK_IDS)
    ):
        raise StableReleaseReadinessError(
            "OpenSpec pre-release proof does not cover the exact non-circular task set"
        )
    _digest(record["tasks_sha256"], "OpenSpec tasks digest")
    _verify_private_audit_signature(record, "OpenSpec pre-release proof", source_sha)


def evaluate_stable_release_readiness(
    *,
    manifest_path: Path,
    evidence_root: Path,
    expected_version: str,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_main_proof_sha256: str,
    expected_candidate_manifest_sha256: str,
    expected_candidate_sha256: str,
    signed_candidate_path: Path,
    updater_public_key_path: Path = UPDATER_PUBLIC_KEY,
) -> StableReleaseDecision:
    """Verify the full external evidence closure for one stable release."""
    _git_id(expected_source_sha, "expected source_sha")
    _git_id(expected_source_tree, "expected source_tree")
    _digest(expected_main_proof_sha256, "expected main proof digest")
    _digest(expected_candidate_manifest_sha256, "expected candidate manifest digest")
    _digest(expected_candidate_sha256, "expected signed candidate digest")
    if SEMVER.fullmatch(expected_version) is None:
        raise StableReleaseReadinessError(
            "expected_version must be a stable semantic version"
        )
    manifest = _read_json(manifest_path, "stable readiness manifest")
    _exact(
        manifest,
        {
            "schema_version",
            "release_version",
            "source_sha",
            "source_tree",
            "release_tag",
            "tag_object_sha",
            "tag_target_sha",
            "main_proof_sha256",
            "candidate_manifest_sha256",
            "candidate_sha256",
            "signer",
            "artifacts",
        },
        "stable readiness manifest",
    )
    if manifest["schema_version"] != SCHEMA:
        raise StableReleaseReadinessError("unsupported stable readiness schema")
    version = manifest["release_version"]
    if version != expected_version:
        raise StableReleaseReadinessError(
            "release_version does not match the expected stable version"
        )
    release_tag = manifest["release_tag"]
    tag_object_sha = _git_id(manifest["tag_object_sha"], "tag object SHA")
    tag_target_sha = _git_id(manifest["tag_target_sha"], "tag target SHA")
    if release_tag != f"v{version}" or tag_target_sha != expected_source_sha:
        raise StableReleaseReadinessError(
            "release tag identity is not exact-SHA/version bound"
        )
    _verify_annotated_tag(release_tag, tag_object_sha, expected_source_sha)
    if (
        manifest["source_sha"] != expected_source_sha
        or manifest["source_tree"] != expected_source_tree
    ):
        raise StableReleaseReadinessError(
            "stable readiness manifest is not exact-SHA bound"
        )
    main_proof = _digest(manifest["main_proof_sha256"], "main proof digest")
    candidate_manifest = _digest(
        manifest["candidate_manifest_sha256"], "candidate manifest digest"
    )
    candidate = _digest(manifest["candidate_sha256"], "candidate digest")
    if candidate_manifest == candidate:
        raise StableReleaseReadinessError(
            "candidate manifest and signed installer identities must be distinct"
        )
    actual_candidate = hashlib.sha256(
        _read_regular(
            signed_candidate_path,
            "signed candidate installer",
            limit=MAX_INSTALLER_BYTES,
        )
    ).hexdigest()
    if (
        main_proof != expected_main_proof_sha256
        or candidate_manifest != expected_candidate_manifest_sha256
        or candidate != expected_candidate_sha256
        or candidate != actual_candidate
    ):
        raise StableReleaseReadinessError(
            "stable evidence identities do not match trusted inputs and actual installer bytes"
        )
    signer = manifest["signer"]
    if not isinstance(signer, dict):
        raise StableReleaseReadinessError("signer must be an object")
    _exact(signer, {"subject", "certificate_thumbprint", "timestamp_subject"}, "signer")
    subject = signer["subject"]
    thumbprint = signer["certificate_thumbprint"]
    timestamp_subject = signer["timestamp_subject"]
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 512
        or not isinstance(thumbprint, str)
        or re.fullmatch(r"[0-9A-F]{40,64}", thumbprint) is None
        or not isinstance(timestamp_subject, str)
        or not timestamp_subject.strip()
        or len(timestamp_subject) > 512
    ):
        raise StableReleaseReadinessError("signer identity is invalid")
    authenticode = AuthenticodeEvidence(
        signer_subject=subject,
        certificate_thumbprint=thumbprint,
        timestamp_subject=timestamp_subject,
    )
    artifacts = _load_artifacts(manifest, evidence_root, expected_source_sha)
    _verify_nsis(
        artifacts["nsis_control_proof"][1],
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        main_proof=main_proof,
        candidate_manifest=candidate_manifest,
        candidate=candidate,
    )
    with tempfile.TemporaryDirectory(
        prefix="stock-desk-readiness-receipts-"
    ) as temporary:
        receipt_root = Path(temporary)
        receipt_root.chmod(0o700)
        receipt_paths: dict[str, Path] = {}
        for name in (
            "signpath_receipt",
            "windows_10_trust_receipt",
            "windows_11_trust_receipt",
        ):
            target = receipt_root / f"{name}.json"
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(artifacts[name][0])
                stream.flush()
                os.fsync(stream.fileno())
            receipt_paths[name] = target
        try:
            _verify_signpath_receipt(
                receipt_paths["signpath_receipt"],
                expected_source_sha,
                candidate,
                authenticode,
            )
            _verify_windows_receipt(
                receipt_paths["windows_10_trust_receipt"],
                "windows_10_22h2_x64",
                expected_source_sha,
                candidate,
                authenticode,
            )
            _verify_windows_receipt(
                receipt_paths["windows_11_trust_receipt"],
                "windows_11_x64",
                expected_source_sha,
                candidate,
                authenticode,
            )
        except TrustedUpdaterReleaseError as error:
            raise StableReleaseReadinessError(
                "signed Windows trust closure is invalid"
            ) from error
    _verify_windows_acceptance(
        artifacts["windows_acceptance_receipt"][1],
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        main_proof=main_proof,
        candidate=candidate,
    )
    _verify_windows_ux(
        artifacts["windows_ux_evidence"][1],
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        main_proof=main_proof,
        candidate=candidate,
    )
    _verify_key_ceremony(
        artifacts["updater_key_ceremony"][1],
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        public_key_path=updater_public_key_path,
    )
    _verify_latency(
        artifacts["latency_ledger"][1],
        artifacts["latency_seal"][1],
        artifacts["latency_report"][1],
    )
    _verify_final_wiki(
        artifacts["final_wiki_evidence"][1],
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
    )
    _verify_requirements(
        artifacts["requirements_completion_evidence"][1],
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        release_tag=release_tag,
        tag_object_sha=tag_object_sha,
    )
    _verify_openspec(
        artifacts["openspec_completion_evidence"][1],
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        release_tag=release_tag,
        tag_object_sha=tag_object_sha,
    )
    return StableReleaseDecision(
        eligible=True,
        version=version,
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        release_tag=release_tag,
        tag_object_sha=tag_object_sha,
        candidate_manifest_sha256=candidate_manifest,
        candidate_sha256=candidate,
        evidence_count=len(artifacts),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--expected-main-proof-sha256", required=True)
    parser.add_argument("--expected-candidate-manifest-sha256", required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument("--signed-candidate", type=Path, required=True)
    parser.add_argument("--updater-public-key", type=Path, default=UPDATER_PUBLIC_KEY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    try:
        decision = evaluate_stable_release_readiness(
            manifest_path=options.manifest,
            evidence_root=options.evidence_root,
            expected_version=options.expected_version,
            expected_source_sha=options.source_sha,
            expected_source_tree=options.source_tree,
            expected_main_proof_sha256=options.expected_main_proof_sha256,
            expected_candidate_manifest_sha256=(
                options.expected_candidate_manifest_sha256
            ),
            expected_candidate_sha256=options.expected_candidate_sha256,
            signed_candidate_path=options.signed_candidate,
            updater_public_key_path=options.updater_public_key,
        )
    except StableReleaseReadinessError as error:
        raise SystemExit(f"stable release readiness failed: {error}") from error
    print(json.dumps(decision, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
