"""Independently verify raw installed-Windows observations and derive evidence.

The VM guest is allowed to record facts, but it cannot declare acceptance.  This
module reopens every bounded raw record on a GitHub-hosted runner, verifies its
digest and strict semantics, derives the public installed-evidence document, and
then delegates the final cross-scenario policy check to the existing verifier.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import copy
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Final, cast

from PIL import Image, UnidentifiedImageError

from scripts import verify_windows_installed_evidence as installed


SCHEMA_VERSION: Final = 1
ARTIFACT: Final = "windows-installed-raw-evidence"
MAX_MANIFEST_BYTES: Final = 1024 * 1024
MAX_RECORD_BYTES: Final = 8 * 1024 * 1024
MAX_PACKAGE_BYTES: Final = 16 * 1024 * 1024
MAX_PUBLIC_TEXT_BYTES: Final = 2 * 1024 * 1024
MAX_EVENT_BYTES: Final = 64 * 1024
_HEX_40: Final = re.compile(r"^[0-9a-f]{40}$")
_HEX_64: Final = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PATH: Final = re.compile(r"^raw/[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_ATTEMPT: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_RFC3339_UTC: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_WEBVIEW_VERSION: Final = re.compile(r"^[0-9]+(?:\.[0-9]+){3}$")
WEBVIEW2_PRODUCTION_GUID: Final = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
MINIMUM_WEBVIEW2_VERSION: Final = (120, 0, 2210, 91)
_PUBLIC_SECRET: Final = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._-]+|"
    r"(?:api[_-]?key|token|password|secret)\s*[=:]\s*[^\s,]+|github[_-]?token)"
)
_PUBLIC_USER_PATH: Final = re.compile(
    r"(?i)(?:[a-z]:\\" + r"users\\|/" + r"home/|/" + r"users/)[^\s\"']+"
)

EXPECTED_EVENTS: Final = (
    "system",
    "account-token",
    "webview-before",
    "webview-installation",
    "webview-child-process-token",
    "webview-after",
    "installer-process-token",
    "desktop-host-process-token",
    "sidecar-process-token",
    "uninstaller-process-token",
    "uac-observation",
    "install-observation",
    "window-observation",
    "v1-canary-before",
    "v1-canary-after",
    "redaction-scan",
    "uninstall-observation",
)
EXPECTED_PRODUCERS: Final = {
    "system": "powershell-cim",
    "account-token": "windows-token",
    "webview-before": "windows-registry-authenticode",
    "webview-installation": "windows-process",
    "webview-child-process-token": "windows-process-token",
    "webview-after": "windows-registry-authenticode",
    "installer-process-token": "windows-process-token",
    "desktop-host-process-token": "windows-process-token",
    "sidecar-process-token": "windows-process-token",
    "uninstaller-process-token": "windows-process-token",
    "uac-observation": "windows-event-observer",
    "install-observation": "windows-filesystem",
    "window-observation": "windows-window-observer",
    "v1-canary-before": "windows-filesystem",
    "v1-canary-after": "windows-filesystem",
    "redaction-scan": "stock-desk-redaction-scan",
    "uninstall-observation": "windows-filesystem",
}


class RawEvidenceError(ValueError):
    """Raw evidence is malformed, contradictory, incomplete, or untrusted."""


def _object(value: object, *, field: str, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RawEvidenceError(f"{field} must be an object")
    result = cast(dict[str, object], value)
    actual = frozenset(result)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        raise RawEvidenceError(f"{field} has {'; '.join(details)}")
    return result


def _string(
    value: object,
    *,
    field: str,
    maximum: int = 128,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise RawEvidenceError(f"{field} is invalid")
    return value


def _integer(value: object, *, field: str, minimum: int | None = None) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise RawEvidenceError(f"{field} must be an integer")
    return value


def _boolean(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise RawEvidenceError(f"{field} must be a boolean")
    return value


def _nullable_integer(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field=field)


def _nullable_boolean(value: object, *, field: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, field=field)


def _sha256(value: object, *, field: str) -> str:
    return _string(value, field=field, maximum=64, pattern=_HEX_64)


def _git_object(value: object, *, field: str) -> str:
    return _string(value, field=field, maximum=40, pattern=_HEX_40)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RawEvidenceError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def _load_json_bytes(payload: bytes, *, field: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RawEvidenceError(f"{field} is not valid UTF-8 JSON") from error


def _read_regular(path: Path, *, maximum: int, field: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RawEvidenceError(f"{field} is missing or not a regular file")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise RawEvidenceError(f"cannot read {field}") from error
    if not payload or len(payload) > maximum:
        raise RawEvidenceError(f"{field} has an invalid size")
    return payload


def _validate_identity(value: object, *, expected: Mapping[str, str]) -> dict[str, str]:
    raw = _object(
        value,
        field="identity",
        fields=frozenset(
            {
                "source_sha",
                "source_tree",
                "main_proof_sha256",
                "candidate_sha256",
                "webview_installer_sha256",
            }
        ),
    )
    result = {
        "source_sha": _git_object(raw["source_sha"], field="identity.source_sha"),
        "source_tree": _git_object(raw["source_tree"], field="identity.source_tree"),
        "main_proof_sha256": _sha256(
            raw["main_proof_sha256"], field="identity.main_proof_sha256"
        ),
        "candidate_sha256": _sha256(
            raw["candidate_sha256"], field="identity.candidate_sha256"
        ),
        "webview_installer_sha256": _sha256(
            raw["webview_installer_sha256"],
            field="identity.webview_installer_sha256",
        ),
    }
    mismatch = sorted(key for key, item in result.items() if item != expected[key])
    if mismatch:
        raise RawEvidenceError(f"raw evidence identity mismatch: {', '.join(mismatch)}")
    return result


def _validate_execution(
    value: object,
    *,
    repository: str,
    workflow: str,
    workflow_ref: str,
    workflow_sha: str,
    workflow_path: str,
    workflow_sha256: str,
    run_id: int,
    run_attempt: int,
    job_id: str,
    controller_label: str,
    scenario: str,
) -> dict[str, object]:
    result = _object(
        value,
        field="execution",
        fields=frozenset(
            {
                "workflow",
                "repository",
                "workflow_ref",
                "workflow_sha",
                "workflow_path",
                "workflow_sha256",
                "run_id",
                "run_attempt",
                "job_id",
                "job_name",
                "matrix_guest_profile",
                "matrix_scenario",
                "matrix_controller_label",
                "scenario_attempt",
                "attempt_id",
            }
        ),
    )
    actual_workflow = _string(result["workflow"], field="execution.workflow")
    actual_repository = _string(
        result["repository"], field="execution.repository", maximum=256
    )
    actual_workflow_ref = _string(
        result["workflow_ref"], field="execution.workflow_ref", maximum=512
    )
    actual_workflow_sha = _git_object(
        result["workflow_sha"], field="execution.workflow_sha"
    )
    actual_workflow_path = _string(
        result["workflow_path"], field="execution.workflow_path", maximum=256
    )
    actual_workflow_sha256 = _sha256(
        result["workflow_sha256"], field="execution.workflow_sha256"
    )
    actual_run_id = _integer(result["run_id"], field="execution.run_id", minimum=1)
    actual_run_attempt = _integer(
        result["run_attempt"], field="execution.run_attempt", minimum=1
    )
    actual_job_id = _string(
        result["job_id"], field="execution.job_id", pattern=_SAFE_ID
    )
    _string(result["job_name"], field="execution.job_name")
    matrix_profile = _string(
        result["matrix_guest_profile"], field="execution.matrix_guest_profile"
    )
    matrix_scenario = _string(
        result["matrix_scenario"], field="execution.matrix_scenario"
    )
    matrix_label = _string(
        result["matrix_controller_label"],
        field="execution.matrix_controller_label",
    )
    scenario_attempt = _integer(
        result["scenario_attempt"], field="execution.scenario_attempt", minimum=1
    )
    _string(
        result["attempt_id"],
        field="execution.attempt_id",
        pattern=_SAFE_ATTEMPT,
    )
    if run_attempt != 1 or actual_run_attempt != 1 or scenario_attempt != 1:
        raise RawEvidenceError(
            "retry-only evidence cannot replace first-attempt raw observations"
        )
    if (
        actual_repository != repository
        or actual_workflow != workflow
        or actual_workflow_ref != workflow_ref
        or actual_workflow_sha != workflow_sha
        or actual_workflow_path != workflow_path
        or actual_workflow_sha256 != workflow_sha256
        or actual_run_id != run_id
        or actual_run_attempt != run_attempt
        or actual_job_id != job_id
        or matrix_scenario != scenario
        or matrix_label != controller_label
        or matrix_profile != ("win11" if scenario == "webview-absent" else "win10-22h2")
    ):
        raise RawEvidenceError("raw evidence execution identity mismatch")
    return result


def _parse_utc(value: object, *, field: str) -> datetime:
    text = _string(value, field=field, maximum=20, pattern=_RFC3339_UTC)
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise RawEvidenceError(f"{field} is invalid") from error


def _validate_browser_observer_summary(
    value: object, *, capture_started: datetime, capture_completed: datetime
) -> dict[str, object]:
    summary = _object(
        value,
        field="capture.browser_window_observer",
        fields=frozenset(
            {
                "schema",
                "api",
                "hook_started_at_utc",
                "baseline_captured_at_utc",
                "baseline_event_sequence",
                "final_captured_at_utc",
                "final_event_sequence",
                "hook_stopped_at_utc",
                "subscribed_events",
                "lifecycle_event_count",
                "lifecycle_events_sha256",
            }
        ),
    )
    if summary["schema"] != "stock-desk-browser-window-observer-v1":
        raise RawEvidenceError("browser window observer schema is invalid")
    if summary["api"] != "Win32 EnumWindows + SetWinEventHook":
        raise RawEvidenceError("browser window observer API is not production Win32")
    if summary["subscribed_events"] != ["create", "show", "hide", "destroy"]:
        raise RawEvidenceError("browser window observer lifecycle hooks are incomplete")
    hook_started = _parse_utc(
        summary["hook_started_at_utc"], field="browser observer hook start"
    )
    baseline_captured = _parse_utc(
        summary["baseline_captured_at_utc"], field="browser observer baseline"
    )
    final_captured = _parse_utc(
        summary["final_captured_at_utc"], field="browser observer final"
    )
    hook_stopped = _parse_utc(
        summary["hook_stopped_at_utc"], field="browser observer hook stop"
    )
    if not (
        capture_started
        <= hook_started
        <= baseline_captured
        <= final_captured
        <= hook_stopped
        <= capture_completed
    ):
        raise RawEvidenceError(
            "browser window hook does not continuously bound baseline through final"
        )
    _integer(
        summary["lifecycle_event_count"],
        field="browser observer lifecycle_event_count",
        minimum=0,
    )
    baseline_sequence = _integer(
        summary["baseline_event_sequence"],
        field="browser observer baseline_event_sequence",
        minimum=0,
    )
    final_sequence = _integer(
        summary["final_event_sequence"],
        field="browser observer final_event_sequence",
        minimum=0,
    )
    lifecycle_count = cast(int, summary["lifecycle_event_count"])
    if not 0 <= baseline_sequence <= final_sequence <= lifecycle_count:
        raise RawEvidenceError("browser hook event boundaries are inconsistent")
    _sha256(
        summary["lifecycle_events_sha256"],
        field="browser observer lifecycle_events_sha256",
    )
    return summary


def _validate_capture(
    value: object,
    *,
    scenario: str,
    controller_label: str,
    guest_harness_sha256: str,
    controller_request_sha256: str,
    snapshot_policy_sha256: str,
) -> dict[str, object]:
    capture = _object(
        value,
        field="capture",
        fields=frozenset(
            {
                "started_at_utc",
                "completed_at_utc",
                "guest_profile",
                "controller_label",
                "guest_harness_sha256",
                "controller_request_sha256",
                "snapshot_policy_sha256",
                "clean_snapshot_sha256",
                "image_sha256",
                "webview_product_guid",
                "minimum_webview_version",
                "failure_injection",
                "browser_window_observer",
                "redaction_version",
            }
        ),
    )
    started = _parse_utc(capture["started_at_utc"], field="capture.started_at_utc")
    completed = _parse_utc(
        capture["completed_at_utc"], field="capture.completed_at_utc"
    )
    if completed < started:
        raise RawEvidenceError("capture time moved backwards")
    profile = _string(capture["guest_profile"], field="capture.guest_profile")
    if profile not in {"win10-22h2", "win11"}:
        raise RawEvidenceError("capture guest profile is unsupported")
    if scenario in {"webview-preinstalled", "webview-install-failure"}:
        if profile != "win10-22h2":
            raise RawEvidenceError(
                "raw scenario is assigned to the wrong guest profile"
            )
    elif profile != "win11":
        raise RawEvidenceError("raw scenario is assigned to the wrong guest profile")
    if capture["controller_label"] != controller_label:
        raise RawEvidenceError("capture controller label does not match matrix")
    for name in (
        "guest_harness_sha256",
        "controller_request_sha256",
        "snapshot_policy_sha256",
        "clean_snapshot_sha256",
        "image_sha256",
    ):
        _sha256(capture[name], field=f"capture.{name}")
    expected_digests = {
        "guest_harness_sha256": guest_harness_sha256,
        "controller_request_sha256": controller_request_sha256,
        "snapshot_policy_sha256": snapshot_policy_sha256,
    }
    if any(capture[name] != digest for name, digest in expected_digests.items()):
        raise RawEvidenceError(
            "capture is not bound to independently recomputed inputs"
        )
    if capture["webview_product_guid"] != WEBVIEW2_PRODUCTION_GUID:
        raise RawEvidenceError("capture is not bound to the production WebView2 GUID")
    if capture["minimum_webview_version"] != ".".join(
        str(component) for component in MINIMUM_WEBVIEW2_VERSION
    ):
        raise RawEvidenceError("capture is not bound to the locked WebView2 minimum")
    injection = capture["failure_injection"]
    if scenario == "webview-install-failure":
        injection_value = _object(
            injection,
            field="capture.failure_injection",
            fields=frozenset({"identity", "sha256"}),
        )
        if (
            injection_value["identity"]
            != "stock-desk-webview2-offline-install-failure-v1"
        ):
            raise RawEvidenceError("failure injection identity is invalid")
        _sha256(injection_value["sha256"], field="capture.failure_injection.sha256")
    elif injection is not None:
        raise RawEvidenceError("non-failure scenario contains a failure injection")
    capture["browser_window_observer"] = _validate_browser_observer_summary(
        capture["browser_window_observer"],
        capture_started=started,
        capture_completed=completed,
    )
    if capture["redaction_version"] != "stock-desk-public-redaction-v2":
        raise RawEvidenceError("raw evidence uses an unsupported redaction contract")
    return capture


def _record_bytes(package: Path, value: object) -> tuple[dict[str, object], bytes]:
    record = _object(
        value,
        field="record",
        fields=frozenset({"kind", "path", "sha256", "size_bytes", "media_type"}),
    )
    _string(record["kind"], field="record.kind")
    path_text = _string(record["path"], field="record.path", pattern=_SAFE_PATH)
    pure = PurePosixPath(path_text)
    if pure.is_absolute() or ".." in pure.parts:
        raise RawEvidenceError("raw record path escapes its package")
    target = package.joinpath(*pure.parts)
    try:
        target.resolve(strict=True).relative_to(package.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise RawEvidenceError("raw record path escapes its package") from error
    payload = _read_regular(
        target, maximum=MAX_RECORD_BYTES, field=f"raw record {path_text}"
    )
    expected_size = _integer(record["size_bytes"], field="record.size_bytes", minimum=1)
    if len(payload) != expected_size:
        raise RawEvidenceError(f"raw record size mismatch: {path_text}")
    if hashlib.sha256(payload).hexdigest() != _sha256(
        record["sha256"], field="record.sha256"
    ):
        raise RawEvidenceError(f"raw record digest mismatch: {path_text}")
    return record, payload


def _reject_unbound_package_entries(
    package: Path, *, expected_files: set[Path]
) -> None:
    actual_files: set[Path] = set()
    for entry in package.rglob("*"):
        if entry.is_symlink():
            raise RawEvidenceError("raw package contains a symlink")
        if entry.is_file():
            actual_files.add(entry.relative_to(package))
        elif not entry.is_dir():
            raise RawEvidenceError("raw package contains an unsupported entry")
    if actual_files != expected_files:
        missing = sorted(str(path) for path in expected_files - actual_files)
        unbound = sorted(str(path) for path in actual_files - expected_files)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unbound:
            details.append(f"unbound: {', '.join(unbound)}")
        raise RawEvidenceError(
            f"raw package file set is not closed ({'; '.join(details)})"
        )


def _value_fields(
    value: object, *, field: str, fields: frozenset[str]
) -> dict[str, object]:
    return _object(value, field=field, fields=fields)


def _validate_system(value: object) -> dict[str, object]:
    system = _value_fields(
        value,
        field="system observation",
        fields=frozenset(
            {
                "family",
                "display_version",
                "build_number",
                "update_build_revision",
                "architecture",
                "image_sha256",
            }
        ),
    )
    family = _string(system["family"], field="system.family")
    version = _string(system["display_version"], field="system.display_version")
    build = _integer(system["build_number"], field="system.build_number", minimum=1)
    _integer(
        system["update_build_revision"],
        field="system.update_build_revision",
        minimum=0,
    )
    if system["architecture"] != "x86_64":
        raise RawEvidenceError("raw evidence requires Windows x86_64")
    _sha256(system["image_sha256"], field="system.image_sha256")
    if not (
        (family == "windows-10" and version == "22H2" and build == 19045)
        or (family == "windows-11" and build >= 22000)
    ):
        raise RawEvidenceError("raw evidence uses an unsupported Windows system")
    return system


def _validate_account(value: object) -> dict[str, object]:
    account = _value_fields(
        value,
        field="account token",
        fields=frozenset(
            {
                "account_type",
                "is_admin",
                "administrator_group_member",
                "linked_token_present",
                "token_elevation_type",
                "integrity_level",
                "integrity_rid",
                "username_contains_non_ascii",
                "profile_path_contains_space",
            }
        ),
    )
    if (
        account["account_type"] != "standard"
        or _boolean(account["is_admin"], field="account.is_admin")
        or _boolean(
            account["administrator_group_member"],
            field="account.administrator_group_member",
        )
        or _boolean(
            account["linked_token_present"], field="account.linked_token_present"
        )
        or account["token_elevation_type"] != "default"
        or account["integrity_level"] != "medium"
        or _integer(account["integrity_rid"], field="account.integrity_rid") != 8192
    ):
        raise RawEvidenceError("raw account token is not a standard-user token")
    _boolean(
        account["username_contains_non_ascii"],
        field="account.username_contains_non_ascii",
    )
    _boolean(
        account["profile_path_contains_space"],
        field="account.profile_path_contains_space",
    )
    return account


def _validate_runtime(value: object, *, field: str) -> dict[str, object]:
    runtime = _value_fields(
        value,
        field=field,
        fields=frozenset(
            {"state", "product_guid", "version", "channel", "signer", "scope"}
        ),
    )
    state = runtime["state"]
    if state == "absent":
        if any(
            runtime[name] is not None
            for name in ("product_guid", "version", "channel", "signer", "scope")
        ):
            raise RawEvidenceError(f"{field} absent state is contradictory")
    elif state == "present":
        if runtime["product_guid"] != WEBVIEW2_PRODUCTION_GUID:
            raise RawEvidenceError(f"{field} is not the production WebView2 GUID")
        version = _string(
            runtime["version"],
            field=f"{field}.version",
            maximum=64,
            pattern=_WEBVIEW_VERSION,
        )
        if (
            tuple(int(component) for component in version.split("."))
            < MINIMUM_WEBVIEW2_VERSION
        ):
            raise RawEvidenceError(f"{field} is below the locked WebView2 minimum")
        if runtime["channel"] != "evergreen":
            raise RawEvidenceError(f"{field} is not Evergreen WebView2")
        signer = _value_fields(
            runtime["signer"],
            field=f"{field}.signer",
            fields=frozenset({"status", "subject", "certificate_sha256"}),
        )
        if (
            signer["status"] != "Valid"
            or signer["subject"] != "CN=Microsoft Corporation"
        ):
            raise RawEvidenceError(f"{field} Microsoft signature is invalid")
        _sha256(
            signer["certificate_sha256"],
            field=f"{field}.signer.certificate_sha256",
        )
        if runtime["scope"] not in {"machine", "current-user"}:
            raise RawEvidenceError(f"{field} has an invalid installation scope")
    else:
        raise RawEvidenceError(f"{field}.state is invalid")
    return runtime


def _validate_installation(value: object, *, webview_sha256: str) -> dict[str, object]:
    result = _value_fields(
        value,
        field="WebView2 installation observation",
        fields=frozenset(
            {"attempted", "exit_code", "installer_sha256", "fault_injection"}
        ),
    )
    attempted = _boolean(result["attempted"], field="webview.installation.attempted")
    exit_code = _nullable_integer(
        result["exit_code"], field="webview.installation.exit_code"
    )
    if attempted != (exit_code is not None):
        raise RawEvidenceError("WebView2 attempt and exit code contradict")
    if (
        _sha256(
            result["installer_sha256"], field="webview.installation.installer_sha256"
        )
        != webview_sha256
    ):
        raise RawEvidenceError("raw WebView2 installer digest mismatch")
    return result


def _validate_webview_child(
    value: object,
    *,
    scenario: str,
    webview_sha256: str,
    failure_injection: object,
) -> dict[str, object]:
    result = _value_fields(
        value,
        field="WebView2 child process token",
        fields=frozenset(
            {
                "observed",
                "executable_name",
                "executable_path_sha256",
                "executable_sha256",
                "signer",
                "elevated",
                "integrity_level",
                "integrity_rid",
                "exit_code",
            }
        ),
    )
    observed = _boolean(result["observed"], field="webview_child.observed")
    if scenario == "webview-preinstalled":
        if observed or any(
            result[name] is not None for name in result if name != "observed"
        ):
            raise RawEvidenceError(
                "preinstalled scenario unexpectedly launched WebView2 installer"
            )
        return result
    if not observed:
        raise RawEvidenceError("WebView2 installer child process was not observed")
    if result["executable_name"] != "MicrosoftEdgeWebView2RuntimeInstaller.exe":
        raise RawEvidenceError("unexpected WebView2 installer child executable")
    _sha256(
        result["executable_path_sha256"],
        field="webview_child.executable_path_sha256",
    )
    if (
        _sha256(result["executable_sha256"], field="webview_child.executable_sha256")
        != webview_sha256
    ):
        raise RawEvidenceError("WebView2 child executable digest mismatch")
    signer = _value_fields(
        result["signer"],
        field="webview_child.signer",
        fields=frozenset({"status", "subject", "certificate_sha256"}),
    )
    if signer["status"] != "Valid" or signer["subject"] != "CN=Microsoft Corporation":
        raise RawEvidenceError("WebView2 child signer is invalid")
    _sha256(
        signer["certificate_sha256"], field="webview_child.signer.certificate_sha256"
    )
    if _boolean(result["elevated"], field="webview_child.elevated"):
        raise RawEvidenceError("WebView2 child unexpectedly elevated")
    if result["integrity_level"] != "medium":
        raise RawEvidenceError("WebView2 child integrity level is not medium")
    if _integer(result["integrity_rid"], field="webview_child.integrity_rid") != 8192:
        raise RawEvidenceError("WebView2 child integrity RID is not medium")
    exit_code = _integer(result["exit_code"], field="webview_child.exit_code")
    if scenario == "webview-absent" and exit_code != 0:
        raise RawEvidenceError("WebView2 install did not succeed")
    if scenario == "webview-install-failure":
        if exit_code == 0 or failure_injection is None:
            raise RawEvidenceError(
                "WebView2 failure was not observed under fixed injection"
            )
    return result


def _validate_process(value: object, *, role: str) -> dict[str, object]:
    result = _value_fields(
        value,
        field=f"{role} process token",
        fields=frozenset(
            {"role", "started", "elevated", "integrity_level", "integrity_rid"}
        ),
    )
    if result["role"] != role:
        raise RawEvidenceError(f"{role} process role mismatch")
    started = _boolean(result["started"], field=f"{role}.started")
    elevated = _nullable_boolean(result["elevated"], field=f"{role}.elevated")
    integrity = result["integrity_level"]
    integrity_rid = _nullable_integer(
        result["integrity_rid"], field=f"{role}.integrity_rid"
    )
    if started and elevated is not False:
        raise RawEvidenceError(f"{role} was not observed as non-elevated")
    if not started and elevated is not None:
        raise RawEvidenceError(f"{role} reports a token without a process")
    if started and integrity != "medium":
        raise RawEvidenceError(f"{role} integrity level is not medium")
    if started and integrity_rid != 8192:
        raise RawEvidenceError(f"{role} integrity RID is not medium")
    if not started and integrity is not None:
        raise RawEvidenceError(f"{role} reports integrity without a process")
    if not started and integrity_rid is not None:
        raise RawEvidenceError(f"{role} reports an integrity RID without a process")
    return {"started": started, "elevated": elevated}


def _validate_security(value: object) -> dict[str, object]:
    result = _value_fields(
        value,
        field="UAC observation",
        fields=frozenset({"uac_prompt_count", "elevation_requested"}),
    )
    if _integer(
        result["uac_prompt_count"], field="security.uac_prompt_count", minimum=0
    ) != 0 or _boolean(
        result["elevation_requested"], field="security.elevation_requested"
    ):
        raise RawEvidenceError("raw installation observed UAC or elevation")
    return result


def _validate_install(value: object) -> dict[str, object]:
    result = _value_fields(
        value,
        field="install observation",
        fields=frozenset(
            {"exit_code", "application_files_present", "shortcut_present", "launchable"}
        ),
    )
    _integer(result["exit_code"], field="install.exit_code")
    for name in ("application_files_present", "shortcut_present", "launchable"):
        _boolean(result[name], field=f"install.{name}")
    return result


def _validate_window(
    value: object, *, scenario: str, observer_summary: dict[str, object]
) -> dict[str, object]:
    result = _value_fields(
        value,
        field="window observation",
        fields=frozenset(
            {
                "observed",
                "main_window_count",
                "title",
                "external_browser_window_count",
                "rendered_content_sha256",
                "capture_scope",
                "ready_marker",
                "uia_text_sha256",
                "uia_entry_count",
                "external_browser_observations",
                "external_browser_window_events",
                "external_browser_observer",
            }
        ),
    )
    _boolean(result["observed"], field="window.observed")
    _integer(result["main_window_count"], field="window.main_window_count", minimum=0)
    if result["title"] is not None:
        _string(result["title"], field="window.title")
    if (
        _integer(
            result["external_browser_window_count"],
            field="window.external_browser_window_count",
            minimum=0,
        )
        != 0
    ):
        raise RawEvidenceError("an external browser window changed during the journey")
    if result["rendered_content_sha256"] is not None:
        _sha256(
            result["rendered_content_sha256"],
            field="window.rendered_content_sha256",
        )
    expected_scope = (
        "none" if scenario == "webview-install-failure" else "target-window-only"
    )
    if result["capture_scope"] != expected_scope:
        raise RawEvidenceError("window capture scope does not match the scenario")
    if result["ready_marker"] is not None:
        _string(result["ready_marker"], field="window.ready_marker", maximum=128)
    if result["uia_text_sha256"] is not None:
        _sha256(result["uia_text_sha256"], field="window.uia_text_sha256")
    entry_count = _integer(
        result["uia_entry_count"], field="window.uia_entry_count", minimum=0
    )
    timeline = result["external_browser_observations"]
    if not isinstance(timeline, list) or not 3 <= len(timeline) <= 512:
        raise RawEvidenceError("external browser observation timeline is incomplete")
    baseline: list[tuple[str, int, int]] | None = None
    previous_time: datetime | None = None
    phases: list[str] = []
    sample_times: list[datetime] = []
    for index, raw_sample in enumerate(timeline):
        sample = _value_fields(
            raw_sample,
            field=f"browser timeline sample {index}",
            fields=frozenset({"captured_at_utc", "phase", "windows"}),
        )
        captured = _parse_utc(
            sample["captured_at_utc"], field="browser timeline captured_at_utc"
        )
        if previous_time is not None and captured < previous_time:
            raise RawEvidenceError("browser observation timestamps moved backwards")
        previous_time = captured
        sample_times.append(captured)
        phase = _string(sample["phase"], field="browser timeline phase")
        if phase not in {"baseline", "installer", "app-readiness", "stable", "final"}:
            raise RawEvidenceError("browser observation phase is invalid")
        phases.append(phase)
        windows = sample["windows"]
        if not isinstance(windows, list) or len(windows) > 64:
            raise RawEvidenceError("browser top-level window inventory is invalid")
        normalized: list[tuple[str, int, int]] = []
        for raw_window in windows:
            browser = _value_fields(
                raw_window,
                field="browser top-level window",
                fields=frozenset({"process_name", "process_id", "window_handle"}),
            )
            name = _string(browser["process_name"], field="browser process name")
            if name not in {"chrome", "msedge", "firefox", "brave"}:
                raise RawEvidenceError("browser process name is unsupported")
            normalized.append(
                (
                    name,
                    _integer(
                        browser["process_id"], field="browser process id", minimum=1
                    ),
                    _integer(
                        browser["window_handle"],
                        field="browser window handle",
                        minimum=1,
                    ),
                )
            )
        if len(set(normalized)) != len(normalized) or normalized != sorted(normalized):
            raise RawEvidenceError(
                "browser top-level window inventory is not canonical"
            )
        if baseline is None:
            baseline = normalized
        elif normalized != baseline:
            raise RawEvidenceError(
                "a transient, replaced, opened, or closed browser window was observed"
            )
    if phases[0] != "baseline" or phases[-1] != "final" or "installer" not in phases:
        raise RawEvidenceError(
            "browser observation does not span the installer journey"
        )
    if scenario != "webview-install-failure" and not {"app-readiness", "stable"} <= set(
        phases
    ):
        raise RawEvidenceError("browser observation does not span stable app readiness")
    if result["external_browser_observer"] != observer_summary:
        raise RawEvidenceError(
            "browser window observer summary is not bound to the raw manifest"
        )
    if sample_times[0] != _parse_utc(
        observer_summary["baseline_captured_at_utc"],
        field="browser observer baseline",
    ) or sample_times[-1] != _parse_utc(
        observer_summary["final_captured_at_utc"],
        field="browser observer final",
    ):
        raise RawEvidenceError(
            "browser polling baseline/final is not bound to the hook interval"
        )
    lifecycle_events = result["external_browser_window_events"]
    if not isinstance(lifecycle_events, list) or len(lifecycle_events) > 4096:
        raise RawEvidenceError("browser lifecycle event stream is invalid")
    if len(lifecycle_events) != _integer(
        observer_summary["lifecycle_event_count"],
        field="browser observer lifecycle_event_count",
        minimum=0,
    ):
        raise RawEvidenceError("browser lifecycle event count is not manifest-bound")
    hook_started = _parse_utc(
        observer_summary["hook_started_at_utc"], field="browser observer hook start"
    )
    hook_stopped = _parse_utc(
        observer_summary["hook_stopped_at_utc"], field="browser observer hook stop"
    )
    baseline_event_sequence = _integer(
        observer_summary["baseline_event_sequence"],
        field="browser observer baseline_event_sequence",
        minimum=0,
    )
    final_event_sequence = _integer(
        observer_summary["final_event_sequence"],
        field="browser observer final_event_sequence",
        minimum=0,
    )
    baseline_captured = sample_times[0]
    final_captured = sample_times[-1]
    previous_event_time: datetime | None = None
    baseline_identities = set(baseline or [])
    digest_lines: list[str] = []
    for index, raw_event in enumerate(lifecycle_events, start=1):
        event = _value_fields(
            raw_event,
            field=f"browser lifecycle event {index}",
            fields=frozenset(
                {
                    "sequence",
                    "captured_at_utc",
                    "event",
                    "process_name",
                    "process_id",
                    "window_handle",
                }
            ),
        )
        sequence = _integer(
            event["sequence"], field="browser lifecycle sequence", minimum=1
        )
        if sequence != index:
            raise RawEvidenceError("browser lifecycle sequence is not contiguous")
        captured = _parse_utc(
            event["captured_at_utc"], field="browser lifecycle captured_at_utc"
        )
        if (
            captured < hook_started
            or captured > hook_stopped
            or (previous_event_time is not None and captured < previous_event_time)
        ):
            raise RawEvidenceError(
                "browser lifecycle event is outside the hook interval"
            )
        previous_event_time = captured
        if (sequence <= baseline_event_sequence and captured > baseline_captured) or (
            sequence > baseline_event_sequence and captured < baseline_captured
        ):
            raise RawEvidenceError(
                "browser lifecycle event is in the wrong baseline sequence slice"
            )
        if (sequence <= final_event_sequence and captured > final_captured) or (
            sequence > final_event_sequence and captured < final_captured
        ):
            raise RawEvidenceError(
                "browser lifecycle event is in the wrong final sequence slice"
            )
        event_name = _string(event["event"], field="browser lifecycle event name")
        if event_name not in {"create", "show", "hide", "destroy"}:
            raise RawEvidenceError("browser lifecycle event name is invalid")
        name = _string(event["process_name"], field="browser lifecycle process name")
        if name not in {"chrome", "msedge", "firefox", "brave"}:
            raise RawEvidenceError("browser lifecycle process name is unsupported")
        process_id = _integer(
            event["process_id"], field="browser lifecycle process id", minimum=1
        )
        window_handle = _integer(
            event["window_handle"], field="browser lifecycle HWND", minimum=1
        )
        identity = (name, process_id, window_handle)
        if identity not in baseline_identities:
            raise RawEvidenceError(
                "a non-baseline browser HWND emitted a lifecycle event"
            )
        if sequence > baseline_event_sequence and event_name in {
            "create",
            "hide",
            "destroy",
        }:
            raise RawEvidenceError(
                "a baseline browser HWND changed lifecycle after baseline capture"
            )
        digest_lines.append(
            f"{sequence}|{event['captured_at_utc']}|{event_name}|"
            f"{name}|{process_id}|{window_handle}"
        )
    lifecycle_digest = hashlib.sha256("\n".join(digest_lines).encode()).hexdigest()
    if lifecycle_digest != observer_summary["lifecycle_events_sha256"]:
        raise RawEvidenceError("browser lifecycle event stream digest mismatch")
    if scenario == "webview-install-failure":
        if (
            any(
                result[name] is not None
                for name in (
                    "rendered_content_sha256",
                    "ready_marker",
                    "uia_text_sha256",
                )
            )
            or entry_count != 0
        ):
            raise RawEvidenceError("failure scenario contains synthetic UI evidence")
    elif entry_count < 1:
        raise RawEvidenceError("success scenario lacks descendant UI Automation text")
    return result


def _validate_canary(value: object, *, field: str) -> dict[str, object]:
    result = _value_fields(
        value,
        field=field,
        fields=frozenset({"entry_count", "content_sha256"}),
    )
    _integer(result["entry_count"], field=f"{field}.entry_count", minimum=1)
    _sha256(result["content_sha256"], field=f"{field}.content_sha256")
    return result


def _validate_redaction(value: object) -> dict[str, object]:
    result = _value_fields(
        value,
        field="redaction scan",
        fields=frozenset(
            {"secret_match_count", "username_match_count", "absolute_path_match_count"}
        ),
    )
    for name in result:
        if _integer(result[name], field=f"redaction_scan.{name}", minimum=0) != 0:
            raise RawEvidenceError("raw public evidence failed redaction scan")
    return result


def _validate_public_text(payload: bytes, *, field: str) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RawEvidenceError(f"{field} is not UTF-8") from error
    if _PUBLIC_SECRET.search(text) or _PUBLIC_USER_PATH.search(text):
        raise RawEvidenceError(f"{field} contains a secret or user-profile path")
    return text


def _validate_uninstall(value: object) -> dict[str, object]:
    result = _value_fields(
        value,
        field="uninstall observation",
        fields=frozenset(
            {"attempted", "exit_code", "application_files_removed", "shortcuts_removed"}
        ),
    )
    attempted = _boolean(result["attempted"], field="uninstall.attempted")
    exit_code = _nullable_integer(result["exit_code"], field="uninstall.exit_code")
    files = _nullable_boolean(
        result["application_files_removed"],
        field="uninstall.application_files_removed",
    )
    shortcuts = _nullable_boolean(
        result["shortcuts_removed"], field="uninstall.shortcuts_removed"
    )
    if attempted != (exit_code is not None):
        raise RawEvidenceError("uninstall attempt and exit code contradict")
    if not attempted and (files is not None or shortcuts is not None):
        raise RawEvidenceError("unattempted uninstall contains result claims")
    return result


def _read_events(
    payload: bytes,
) -> tuple[dict[str, dict[str, object]], dict[str, bytes]]:
    lines = payload.splitlines()
    if len(lines) != len(EXPECTED_EVENTS):
        raise RawEvidenceError("raw observation stream has missing or extra events")
    events: dict[str, dict[str, object]] = {}
    event_bytes: dict[str, bytes] = {}
    previous_time: datetime | None = None
    for index, line in enumerate(lines, start=1):
        if not line or len(line) > MAX_EVENT_BYTES:
            raise RawEvidenceError("raw observation event has an invalid size")
        event = _object(
            _load_json_bytes(line, field=f"observation event {index}"),
            field=f"observation event {index}",
            fields=frozenset(
                {"sequence", "captured_at_utc", "kind", "producer", "value"}
            ),
        )
        if _integer(event["sequence"], field="event.sequence", minimum=1) != index:
            raise RawEvidenceError("raw event sequence is not contiguous")
        kind = _string(event["kind"], field="event.kind")
        if kind != EXPECTED_EVENTS[index - 1] or kind in events:
            raise RawEvidenceError("raw event kind order is invalid")
        if event["producer"] != EXPECTED_PRODUCERS[kind]:
            raise RawEvidenceError(f"raw event producer is invalid: {kind}")
        captured = _parse_utc(event["captured_at_utc"], field="event.captured_at_utc")
        if previous_time is not None and captured < previous_time:
            raise RawEvidenceError("raw event timestamps moved backwards")
        previous_time = captured
        events[kind] = event
        event_bytes[kind] = line
    return events, event_bytes


def _validate_png(payload: bytes, *, kind: str) -> str:
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG":
                raise RawEvidenceError("raw capture is not a PNG")
            width, height = image.size
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            rgb = image.convert("RGB")
            colors = rgb.resize((64, 64)).getcolors(maxcolors=4096)
    except (OSError, UnidentifiedImageError) as error:
        raise RawEvidenceError("raw capture is not a valid PNG") from error
    minimum = (640, 480) if kind == "window-capture" else (320, 180)
    if width < minimum[0] or height < minimum[1] or width > 8192 or height > 8192:
        raise RawEvidenceError(f"raw {kind} dimensions are outside the safe range")
    if colors is None or len(colors) < 8:
        raise RawEvidenceError(f"raw {kind} is visually empty or synthetic")
    return hashlib.sha256(payload).hexdigest()


def _digest(parts: Sequence[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _validate_lifecycle_receipt(
    package: Path,
    *,
    manifest_bytes: bytes,
    scenario: str,
    capture: Mapping[str, object],
    execution: Mapping[str, object],
    expected_adapter_sha256: str,
) -> dict[str, object]:
    receipt_path = package.parent / "controller" / "lifecycle-receipt.json"
    payload = _read_regular(
        receipt_path, maximum=MAX_MANIFEST_BYTES, field="controller lifecycle receipt"
    )
    receipt = _object(
        _load_json_bytes(payload, field="controller lifecycle receipt"),
        field="controller lifecycle receipt",
        fields=frozenset(
            {
                "schema",
                "guest_profile",
                "controller_label",
                "scenario",
                "snapshot_policy_sha256",
                "snapshot_sha256",
                "image_sha256",
                "system",
                "webview_initial_state",
                "failure_injection",
                "controller_request_sha256",
                "guest_harness_sha256",
                "guest_executed_harness_sha256",
                "workflow_sha256",
                "raw_manifest_sha256",
                "restored_before_at_utc",
                "acceptance_completed_at_utc",
                "cleanup_restored_at_utc",
                "adapter_sha256",
                "controller_binding_sha256",
                "lease_digest",
                "lease_expires_at_utc",
                "watchdog_armed",
                "lease_state",
                "lease_released_at_utc",
            }
        ),
    )
    if receipt["schema"] != "stock-desk-windows-vm-lifecycle-receipt-v1":
        raise RawEvidenceError("controller lifecycle receipt schema is invalid")
    matches = {
        "guest_profile": capture["guest_profile"],
        "controller_label": capture["controller_label"],
        "scenario": scenario,
        "snapshot_policy_sha256": capture["snapshot_policy_sha256"],
        "snapshot_sha256": capture["clean_snapshot_sha256"],
        "image_sha256": capture["image_sha256"],
        "failure_injection": capture["failure_injection"],
        "controller_request_sha256": capture["controller_request_sha256"],
        "guest_harness_sha256": capture["guest_harness_sha256"],
        "guest_executed_harness_sha256": capture["guest_harness_sha256"],
        "workflow_sha256": execution["workflow_sha256"],
        "raw_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "adapter_sha256": expected_adapter_sha256,
    }
    if any(receipt[name] != expected for name, expected in matches.items()):
        raise RawEvidenceError("controller lifecycle receipt binding mismatch")
    _sha256(receipt["adapter_sha256"], field="lifecycle.adapter_sha256")
    _sha256(
        receipt["controller_binding_sha256"],
        field="lifecycle.controller_binding_sha256",
    )
    _sha256(receipt["lease_digest"], field="lifecycle.lease_digest")
    expected_lease_digest = hashlib.sha256(
        "\0".join(
            (
                "stock-desk-controller-lease-v1",
                cast(str, execution["repository"]),
                cast(str, execution["workflow_sha"]),
                str(cast(int, execution["run_id"])),
                cast(str, execution["job_id"]),
                cast(str, receipt["controller_binding_sha256"]),
            )
        ).encode("utf-8")
    ).hexdigest()
    if receipt["lease_digest"] != expected_lease_digest:
        raise RawEvidenceError("controller lease digest is not execution-bound")
    if receipt["watchdog_armed"] is not False:
        raise RawEvidenceError("controller watchdog remained armed after cleanup")
    if receipt["lease_state"] != "released-after-restore":
        raise RawEvidenceError("controller lease was not released after restore")
    restored = _parse_utc(
        receipt["restored_before_at_utc"], field="lifecycle.restored_before_at_utc"
    )
    acceptance_completed = _parse_utc(
        receipt["acceptance_completed_at_utc"],
        field="lifecycle.acceptance_completed_at_utc",
    )
    cleanup_restored = _parse_utc(
        receipt["cleanup_restored_at_utc"],
        field="lifecycle.cleanup_restored_at_utc",
    )
    lease_expires = _parse_utc(
        receipt["lease_expires_at_utc"], field="lifecycle.lease_expires_at_utc"
    )
    lease_released = _parse_utc(
        receipt["lease_released_at_utc"], field="lifecycle.lease_released_at_utc"
    )
    capture_started = _parse_utc(
        capture["started_at_utc"], field="capture.started_at_utc"
    )
    capture_completed = _parse_utc(
        capture["completed_at_utc"], field="capture.completed_at_utc"
    )
    if not (
        restored
        <= capture_started
        <= capture_completed
        <= acceptance_completed
        <= cleanup_restored
        == lease_released
        <= lease_expires
    ):
        raise RawEvidenceError(
            "controller lifecycle timestamps do not bound capture and cleanup"
        )
    _string(receipt["webview_initial_state"], field="lifecycle.webview_initial_state")
    _validate_system(
        {
            **cast(dict[str, object], receipt["system"]),
            "image_sha256": receipt["image_sha256"],
        }
    )
    return receipt


def _diagnostic_record(kind: str, parts: Sequence[bytes]) -> dict[str, object]:
    payload = b"\n".join(parts)
    return {
        "kind": kind,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def derive_package(
    package: Path,
    *,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_main_proof_sha256: str,
    expected_candidate_sha256: str,
    expected_webview_installer_sha256: str,
    expected_repository: str,
    expected_workflow: str,
    expected_workflow_ref: str,
    expected_workflow_sha: str,
    expected_workflow_path: str,
    expected_workflow_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_job_id: str,
    expected_guest_harness_sha256: str,
    expected_controller_request_sha256: str,
    expected_snapshot_policy_sha256: str,
    expected_adapter_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify one raw package and return derived evidence plus immutable receipt data."""

    if package.is_symlink() or not package.is_dir():
        raise RawEvidenceError("raw evidence package is missing")
    manifest_path = package / "raw-manifest.json"
    manifest_bytes = _read_regular(
        manifest_path, maximum=MAX_MANIFEST_BYTES, field="raw manifest"
    )
    manifest = _object(
        _load_json_bytes(manifest_bytes, field="raw manifest"),
        field="raw manifest",
        fields=frozenset(
            {
                "schema_version",
                "artifact",
                "scenario",
                "identity",
                "execution",
                "capture",
                "records",
            }
        ),
    )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != SCHEMA_VERSION
        or manifest["artifact"] != ARTIFACT
    ):
        raise RawEvidenceError("raw evidence schema identity is invalid")
    scenario = _string(manifest["scenario"], field="scenario")
    if scenario not in installed.SCENARIOS:
        raise RawEvidenceError("raw evidence scenario is unsupported")
    expected = {
        "source_sha": _git_object(expected_source_sha, field="expected source_sha"),
        "source_tree": _git_object(expected_source_tree, field="expected source_tree"),
        "main_proof_sha256": _sha256(
            expected_main_proof_sha256, field="expected main proof"
        ),
        "candidate_sha256": _sha256(
            expected_candidate_sha256, field="expected candidate"
        ),
        "webview_installer_sha256": _sha256(
            expected_webview_installer_sha256, field="expected WebView2 installer"
        ),
    }
    identity = _validate_identity(manifest["identity"], expected=expected)
    execution = _validate_execution(
        manifest["execution"],
        repository=_string(
            expected_repository, field="expected repository", maximum=256
        ),
        workflow=_string(expected_workflow, field="expected workflow"),
        workflow_ref=_string(
            expected_workflow_ref, field="expected workflow ref", maximum=512
        ),
        workflow_sha=_git_object(expected_workflow_sha, field="expected workflow SHA"),
        workflow_path=_string(
            expected_workflow_path, field="expected workflow path", maximum=256
        ),
        workflow_sha256=_sha256(
            expected_workflow_sha256, field="expected workflow digest"
        ),
        run_id=_integer(expected_run_id, field="expected run id", minimum=1),
        run_attempt=_integer(
            expected_run_attempt, field="expected run attempt", minimum=1
        ),
        job_id=_string(expected_job_id, field="expected job id", pattern=_SAFE_ID),
        controller_label=(
            "stock-desk-vm-controller-win11"
            if scenario == "webview-absent"
            else "stock-desk-vm-controller-win10-22h2"
        ),
        scenario=scenario,
    )
    capture = _validate_capture(
        manifest["capture"],
        scenario=scenario,
        controller_label=cast(str, execution["matrix_controller_label"]),
        guest_harness_sha256=_sha256(
            expected_guest_harness_sha256, field="expected guest harness digest"
        ),
        controller_request_sha256=_sha256(
            expected_controller_request_sha256,
            field="expected controller request digest",
        ),
        snapshot_policy_sha256=_sha256(
            expected_snapshot_policy_sha256, field="expected snapshot policy digest"
        ),
    )
    raw_records = manifest["records"]
    expected_record_count = 3 if scenario == "webview-install-failure" else 4
    if not isinstance(raw_records, list) or len(raw_records) != expected_record_count:
        raise RawEvidenceError("raw manifest has the wrong closed record count")
    records: dict[str, tuple[dict[str, object], bytes]] = {}
    paths: set[str] = set()
    total = len(manifest_bytes)
    for value in raw_records:
        record, payload = _record_bytes(package, value)
        kind = cast(str, record["kind"])
        path_text = cast(str, record["path"])
        if kind in records or path_text in paths:
            raise RawEvidenceError("raw record kinds and paths must be unique")
        records[kind] = (record, payload)
        paths.add(path_text)
        total += len(payload)
    _reject_unbound_package_entries(
        package,
        expected_files={Path("raw-manifest.json")}
        | {Path(path_text) for path_text in paths},
    )
    detail_kind = (
        "failure-diagnostic"
        if scenario == "webview-install-failure"
        else "ui-automation-text"
    )
    expected_record_kinds = {"observation-stream", "install-log", detail_kind}
    if scenario != "webview-install-failure":
        expected_record_kinds.add("window-capture")
    if set(records) != expected_record_kinds:
        raise RawEvidenceError("raw manifest record set does not match scenario")
    expected_media = {
        "observation-stream": "application/x-ndjson",
        "install-log": "text/plain; charset=utf-8",
        detail_kind: "text/plain; charset=utf-8",
    }
    if scenario != "webview-install-failure":
        expected_media["window-capture"] = "image/png"
    for kind, (record, _payload) in records.items():
        if record["media_type"] != expected_media[kind]:
            raise RawEvidenceError(f"raw record media type is invalid: {kind}")
    if total > MAX_PACKAGE_BYTES:
        raise RawEvidenceError("raw evidence package exceeds total size limit")
    if (
        len(records["install-log"][1])
        + len(records["observation-stream"][1])
        + len(records[detail_kind][1])
        > MAX_PUBLIC_TEXT_BYTES
    ):
        raise RawEvidenceError("raw public text exceeds total size limit")
    install_log = records["install-log"][1]
    _validate_public_text(install_log, field="raw install log")
    _validate_public_text(
        records["observation-stream"][1], field="raw observation stream"
    )
    detail_bytes = records[detail_kind][1]
    detail_text = _validate_public_text(detail_bytes, field="raw scenario detail")
    if not detail_text.strip():
        raise RawEvidenceError("raw scenario detail is empty")
    capture_bytes: bytes | None = None
    capture_digest: str | None = None
    if scenario != "webview-install-failure":
        capture_bytes = records["window-capture"][1]
        capture_digest = _validate_png(capture_bytes, kind="window-capture")

    lifecycle = _validate_lifecycle_receipt(
        package,
        manifest_bytes=manifest_bytes,
        scenario=scenario,
        capture=capture,
        execution=execution,
        expected_adapter_sha256=_sha256(
            expected_adapter_sha256, field="expected adapter digest"
        ),
    )

    events, event_bytes = _read_events(records["observation-stream"][1])
    first_event = _parse_utc(
        events[EXPECTED_EVENTS[0]]["captured_at_utc"],
        field="first event captured_at_utc",
    )
    last_event = _parse_utc(
        events[EXPECTED_EVENTS[-1]]["captured_at_utc"],
        field="last event captured_at_utc",
    )
    capture_started = _parse_utc(
        capture["started_at_utc"], field="capture.started_at_utc"
    )
    capture_completed = _parse_utc(
        capture["completed_at_utc"], field="capture.completed_at_utc"
    )
    if first_event < capture_started or last_event > capture_completed:
        raise RawEvidenceError("raw event timestamps fall outside the capture window")

    def event_value(name: str) -> object:
        return events[name]["value"]

    system = _validate_system(event_value("system"))
    lifecycle_system = cast(dict[str, object], lifecycle["system"])
    if any(system[name] != lifecycle_system[name] for name in lifecycle_system):
        raise RawEvidenceError(
            "observed OS does not match protected snapshot assignment"
        )
    if system["image_sha256"] != capture["image_sha256"]:
        raise RawEvidenceError(
            "observed OS image digest does not match snapshot assignment"
        )
    account = _validate_account(event_value("account-token"))
    webview_before = _validate_runtime(
        event_value("webview-before"), field="webview.before"
    )
    if webview_before["state"] != lifecycle["webview_initial_state"]:
        raise RawEvidenceError(
            "observed WebView2 initial state does not match snapshot assignment"
        )
    webview_installation = _validate_installation(
        event_value("webview-installation"),
        webview_sha256=identity["webview_installer_sha256"],
    )
    if webview_installation["fault_injection"] != capture["failure_injection"]:
        raise RawEvidenceError(
            "WebView2 installation is not bound to fixed fault injection"
        )
    webview_child = _validate_webview_child(
        event_value("webview-child-process-token"),
        scenario=scenario,
        webview_sha256=identity["webview_installer_sha256"],
        failure_injection=capture["failure_injection"],
    )
    if (
        webview_installation["attempted"] != webview_child["observed"]
        or webview_installation["exit_code"] != webview_child["exit_code"]
    ):
        raise RawEvidenceError(
            "WebView2 installation result does not match observed child process"
        )
    webview_after = _validate_runtime(
        event_value("webview-after"), field="webview.after"
    )
    roles = {
        "installer": "installer-process-token",
        "desktop_host": "desktop-host-process-token",
        "sidecar": "sidecar-process-token",
        "uninstaller": "uninstaller-process-token",
    }
    processes = {
        role: _validate_process(event_value(event), role=role)
        for role, event in roles.items()
    }
    security = _validate_security(event_value("uac-observation"))
    install_observation = _validate_install(event_value("install-observation"))
    window = _validate_window(
        event_value("window-observation"),
        scenario=scenario,
        observer_summary=cast(dict[str, object], capture["browser_window_observer"]),
    )
    marker = window["ready_marker"]
    if scenario == "webview-install-failure":
        child_exit = cast(int, webview_child["exit_code"])
        parent_exit = cast(int, install_observation["exit_code"])
        required_failure_lines = {
            f"webview_child_exit_code={child_exit}",
            f"nsis_parent_exit_code={parent_exit}",
            "failure_injection_identity=stock-desk-webview2-offline-install-failure-v1",
        }
        if (
            child_exit == 0
            or parent_exit == 0
            or not required_failure_lines <= set(detail_text.splitlines())
        ):
            raise RawEvidenceError(
                "failure diagnostic is not bound to real child and NSIS parent exits"
            )
    else:
        if window["rendered_content_sha256"] != capture_digest:
            raise RawEvidenceError("window rendered digest does not match PNG capture")
        if window["uia_text_sha256"] != hashlib.sha256(detail_bytes).hexdigest():
            raise RawEvidenceError(
                "window UI Automation digest does not match capture text"
            )
        if cast(int, window["uia_entry_count"]) != len(
            [line for line in detail_text.splitlines() if line.strip()]
        ):
            raise RawEvidenceError("window UI Automation entry count is inconsistent")
        if (
            not isinstance(marker, str)
            or marker not in detail_text
            or re.fullmatch(
                r"(?i)(Stock Desk|Stock Desk ready|Stock Desk desktop)", marker
            )
        ):
            raise RawEvidenceError(
                "success capture lacks meaningful descendant WebView readiness"
            )
    canary_before = _validate_canary(
        event_value("v1-canary-before"), field="v1_canary.before"
    )
    canary_after = _validate_canary(
        event_value("v1-canary-after"), field="v1_canary.after"
    )
    redaction = _validate_redaction(event_value("redaction-scan"))
    uninstall = _validate_uninstall(event_value("uninstall-observation"))

    derived_capture_kind = (
        "failure-capture" if scenario == "webview-install-failure" else "window-capture"
    )
    derived_capture_parts = (
        [detail_bytes] if capture_bytes is None else [capture_bytes, detail_bytes]
    )
    diagnostic_parts = {
        "account-token": [event_bytes["account-token"]],
        "diagnostic-summary": [manifest_bytes],
        "install-log": [install_log],
        "process-token": [event_bytes[name] for name in roles.values()],
        "uac-observation": [event_bytes["uac-observation"]],
        "v1-canary": [event_bytes["v1-canary-before"], event_bytes["v1-canary-after"]],
        "webview-inventory": [
            event_bytes["webview-before"],
            event_bytes["webview-installation"],
            event_bytes["webview-child-process-token"],
            event_bytes["webview-after"],
        ],
        derived_capture_kind: derived_capture_parts,
    }
    if scenario != "webview-install-failure":
        diagnostic_parts["uninstall-log"] = [event_bytes["uninstall-observation"]]
    diagnostics = [
        _diagnostic_record(kind, parts)
        for kind, parts in sorted(diagnostic_parts.items())
    ]
    derived: dict[str, Any] = {
        "schema_version": 1,
        "artifact": "windows-installed-evidence",
        "scenario": scenario,
        "identity": copy.deepcopy(identity),
        "execution": {
            key: execution[key]
            for key in (
                "workflow",
                "run_id",
                "run_attempt",
                "job_name",
                "scenario_attempt",
                "attempt_id",
            )
        }
        | {"job_id": f"{execution['job_id']}-{scenario}"},
        "system": copy.deepcopy(system),
        "account": {
            key: account[key]
            for key in (
                "account_type",
                "is_admin",
                "username_contains_non_ascii",
                "profile_path_contains_space",
            )
        },
        "webview": {
            "before": {
                key: value for key, value in webview_before.items() if key != "scope"
            },
            "installation": {
                key: value
                for key, value in webview_installation.items()
                if key != "fault_injection"
            },
            "after": {
                key: value for key, value in webview_after.items() if key != "scope"
            },
        },
        "processes": processes,
        "security": copy.deepcopy(security),
        "install": copy.deepcopy(install_observation),
        "window": {
            key: (
                None
                if scenario == "webview-install-failure"
                and key == "rendered_content_sha256"
                else window[key]
            )
            for key in (
                "observed",
                "main_window_count",
                "title",
                "external_browser_window_count",
                "rendered_content_sha256",
            )
        },
        "v1_canary": {"before": canary_before, "after": canary_after},
        "diagnostic_summary": {
            "archive_sha256": _digest(
                [manifest_bytes] + [records[kind][1] for kind in sorted(records)]
            ),
            "entry_count": len(diagnostics),
            "redaction_scan": copy.deepcopy(redaction),
            "records": diagnostics,
        },
        "uninstall": copy.deepcopy(uninstall),
    }
    installed.validate_evidence(
        derived,
        expected_source_sha=expected["source_sha"],
        expected_source_tree=expected["source_tree"],
        expected_main_proof_sha256=expected["main_proof_sha256"],
        expected_candidate_sha256=expected["candidate_sha256"],
        expected_webview_installer_sha256=expected["webview_installer_sha256"],
        expected_workflow=expected_workflow,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt,
        expected_job_id_prefix=expected_job_id,
    )
    derived_bytes = (
        json.dumps(derived, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    receipt = {
        "scenario": scenario,
        "guest_profile": capture["guest_profile"],
        "raw_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "lifecycle_receipt_sha256": hashlib.sha256(
            (package.parent / "controller" / "lifecycle-receipt.json").read_bytes()
        ).hexdigest(),
        "observation_stream_sha256": hashlib.sha256(
            records["observation-stream"][1]
        ).hexdigest(),
        "derived_evidence_sha256": hashlib.sha256(derived_bytes).hexdigest(),
        "webview_child_observation_sha256": hashlib.sha256(
            event_bytes["webview-child-process-token"]
        ).hexdigest(),
    }
    return derived, receipt


def verify_matrix(
    packages: Sequence[Path],
    *,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_main_proof_sha256: str,
    expected_candidate_sha256: str,
    expected_webview_installer_sha256: str,
    expected_repository: str,
    expected_workflow: str,
    expected_workflow_ref: str,
    expected_workflow_sha: str,
    expected_workflow_path: str,
    expected_workflow_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_job_id: str,
    expected_guest_harness_sha256: str,
    expected_controller_request_sha256: str,
    expected_snapshot_policy_sha256: str,
    expected_adapter_sha256: str,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    results = [
        derive_package(
            package,
            expected_source_sha=expected_source_sha,
            expected_source_tree=expected_source_tree,
            expected_main_proof_sha256=expected_main_proof_sha256,
            expected_candidate_sha256=expected_candidate_sha256,
            expected_webview_installer_sha256=expected_webview_installer_sha256,
            expected_repository=expected_repository,
            expected_workflow=expected_workflow,
            expected_workflow_ref=expected_workflow_ref,
            expected_workflow_sha=expected_workflow_sha,
            expected_workflow_path=expected_workflow_path,
            expected_workflow_sha256=expected_workflow_sha256,
            expected_run_id=expected_run_id,
            expected_run_attempt=expected_run_attempt,
            expected_job_id=expected_job_id,
            expected_guest_harness_sha256=expected_guest_harness_sha256,
            expected_controller_request_sha256=expected_controller_request_sha256,
            expected_snapshot_policy_sha256=expected_snapshot_policy_sha256,
            expected_adapter_sha256=expected_adapter_sha256,
        )
        for package in packages
    ]
    derived = tuple(result[0] for result in results)
    installed.validate_matrix(
        derived,
        expected_source_sha=expected_source_sha,
        expected_source_tree=expected_source_tree,
        expected_main_proof_sha256=expected_main_proof_sha256,
        expected_candidate_sha256=expected_candidate_sha256,
        expected_webview_installer_sha256=expected_webview_installer_sha256,
        expected_workflow=expected_workflow,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt,
        expected_job_id_prefix=expected_job_id,
    )
    receipt = {
        "schema": "stock-desk-windows-installed-raw-verification-v1",
        "evidence_kind": "independently-verified-windows-vm",
        "source_sha": expected_source_sha,
        "source_tree": expected_source_tree,
        "workflow": expected_workflow,
        "repository": expected_repository,
        "workflow_ref": expected_workflow_ref,
        "workflow_sha": expected_workflow_sha,
        "workflow_path": expected_workflow_path,
        "workflow_sha256": expected_workflow_sha256,
        "snapshot_policy_sha256": expected_snapshot_policy_sha256,
        "adapter_sha256": expected_adapter_sha256,
        "run_id": expected_run_id,
        "run_attempt": expected_run_attempt,
        "scenario_evidence": sorted(
            (result[1] for result in results), key=lambda item: item["scenario"]
        ),
        "status": "verified",
    }
    return derived, receipt


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify raw first-attempt Windows VM observations"
    )
    parser.add_argument("package", nargs="+", type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--main-proof-sha256", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--webview-installer-sha256", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--workflow-sha256", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--guest-harness-sha256", required=True)
    parser.add_argument("--controller-request-sha256", required=True)
    parser.add_argument("--snapshot-policy-sha256", required=True)
    parser.add_argument("--adapter-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        derived, receipt = verify_matrix(
            arguments.package,
            expected_source_sha=arguments.source_sha,
            expected_source_tree=arguments.source_tree,
            expected_main_proof_sha256=arguments.main_proof_sha256,
            expected_candidate_sha256=arguments.candidate_sha256,
            expected_webview_installer_sha256=arguments.webview_installer_sha256,
            expected_repository=arguments.repository,
            expected_workflow=arguments.workflow,
            expected_workflow_ref=arguments.workflow_ref,
            expected_workflow_sha=arguments.workflow_sha,
            expected_workflow_path=arguments.workflow_path,
            expected_workflow_sha256=arguments.workflow_sha256,
            expected_run_id=arguments.run_id,
            expected_run_attempt=arguments.run_attempt,
            expected_job_id=arguments.job_id,
            expected_guest_harness_sha256=arguments.guest_harness_sha256,
            expected_controller_request_sha256=arguments.controller_request_sha256,
            expected_snapshot_policy_sha256=arguments.snapshot_policy_sha256,
            expected_adapter_sha256=arguments.adapter_sha256,
        )
        if arguments.output.exists() and (
            arguments.output.is_symlink() or not arguments.output.is_dir()
        ):
            raise RawEvidenceError("raw verification output is unsafe")
        arguments.output.mkdir(parents=True, exist_ok=True)
        for document in derived:
            _write_json(
                arguments.output / f"derived-{document['scenario']}.json", document
            )
        _write_json(arguments.output / "verification-receipt.json", receipt)
    except (OSError, RawEvidenceError, installed.InstalledEvidenceError) as error:
        print(f"raw Windows evidence verification failed: {error}", file=sys.stderr)
        return 1
    print("raw Windows evidence independently verified: 3 first-attempt VM scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
