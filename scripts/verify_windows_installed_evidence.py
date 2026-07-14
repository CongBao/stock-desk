"""Verify public-safe installed Windows and WebView2 matrix evidence."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast


SCHEMA_VERSION: Final = 1
ARTIFACT: Final = "windows-installed-evidence"
SCENARIOS: Final = frozenset(
    {
        "webview-preinstalled",
        "webview-absent",
        "webview-install-failure",
    }
)
COMMON_EVIDENCE_KINDS: Final = frozenset(
    {
        "account-token",
        "diagnostic-summary",
        "install-log",
        "process-token",
        "uac-observation",
        "v1-canary",
        "webview-inventory",
    }
)
SUCCESS_EVIDENCE_KINDS: Final = COMMON_EVIDENCE_KINDS | {
    "uninstall-log",
    "window-capture",
}
FAILURE_EVIDENCE_KINDS: Final = COMMON_EVIDENCE_KINDS | {"failure-capture"}
EVIDENCE_KINDS: Final = SUCCESS_EVIDENCE_KINDS | FAILURE_EVIDENCE_KINDS
MAX_EVIDENCE_BYTES: Final = 2 * 1024 * 1024
_HEX_40: Final = re.compile(r"^[0-9a-f]{40}$")
_HEX_64: Final = re.compile(r"^[0-9a-f]{64}$")
_WEBVIEW_VERSION: Final = re.compile(r"^[0-9]+(?:\.[0-9]+){3}$")
WEBVIEW2_PRODUCTION_GUID: Final = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
MINIMUM_WEBVIEW2_VERSION: Final = (120, 0, 2210, 91)
_DISPLAY_VERSION: Final = re.compile(r"^[0-9]{2}H[12]$")
_SAFE_JOB_ID: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_ATTEMPT_ID: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")


class InstalledEvidenceError(ValueError):
    """Installed Windows evidence is incomplete, contradictory, or untrusted."""


def _expect_object(
    value: object, *, field: str, fields: frozenset[str]
) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InstalledEvidenceError(f"{field} must be an object")
    result = cast(dict[str, object], value)
    actual = frozenset(result)
    if actual != fields:
        unknown = sorted(actual - fields)
        missing = sorted(fields - actual)
        details: list[str] = []
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        raise InstalledEvidenceError(f"{field} has {'; '.join(details)}")
    return result


def _expect_string(
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
        raise InstalledEvidenceError(f"{field} is invalid")
    return value


def _expect_sha256(value: object, *, field: str) -> str:
    return _expect_string(value, field=field, maximum=64, pattern=_HEX_64)


def _expect_git_object(value: object, *, field: str) -> str:
    return _expect_string(value, field=field, maximum=40, pattern=_HEX_40)


def _expect_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise InstalledEvidenceError(f"{field} must be a boolean")
    return value


def _expect_int(value: object, *, field: str, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise InstalledEvidenceError(f"{field} must be an integer")
    result = value
    if minimum is not None and result < minimum:
        raise InstalledEvidenceError(f"{field} must be at least {minimum}")
    return result


def _expect_nullable_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _expect_int(value, field=field)


def _expect_nullable_bool(value: object, *, field: str) -> bool | None:
    if value is None:
        return None
    return _expect_bool(value, field=field)


def _validate_expected_identity(
    *,
    source_sha: str,
    source_tree: str,
    main_proof_sha256: str,
    candidate_sha256: str,
    webview_installer_sha256: str,
) -> dict[str, str]:
    return {
        "source_sha": _expect_git_object(source_sha, field="expected source_sha"),
        "source_tree": _expect_git_object(source_tree, field="expected source_tree"),
        "main_proof_sha256": _expect_sha256(
            main_proof_sha256, field="expected main_proof_sha256"
        ),
        "candidate_sha256": _expect_sha256(
            candidate_sha256, field="expected candidate_sha256"
        ),
        "webview_installer_sha256": _expect_sha256(
            webview_installer_sha256,
            field="expected webview_installer_sha256",
        ),
    }


def _validate_identity(
    value: object, *, expected: Mapping[str, str]
) -> dict[str, object]:
    identity = _expect_object(
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
    actual = {
        "source_sha": _expect_git_object(
            identity["source_sha"], field="identity.source_sha"
        ),
        "source_tree": _expect_git_object(
            identity["source_tree"], field="identity.source_tree"
        ),
        "main_proof_sha256": _expect_sha256(
            identity["main_proof_sha256"], field="identity.main_proof_sha256"
        ),
        "candidate_sha256": _expect_sha256(
            identity["candidate_sha256"], field="identity.candidate_sha256"
        ),
        "webview_installer_sha256": _expect_sha256(
            identity["webview_installer_sha256"],
            field="identity.webview_installer_sha256",
        ),
    }
    mismatches = sorted(key for key, value in actual.items() if value != expected[key])
    if mismatches:
        raise InstalledEvidenceError(
            f"installed evidence identity mismatch: {', '.join(mismatches)}"
        )
    return identity


def _validate_execution(
    value: object,
    *,
    expected_workflow: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_job_id: str,
) -> dict[str, object]:
    execution = _expect_object(
        value,
        field="execution",
        fields=frozenset(
            {
                "workflow",
                "run_id",
                "run_attempt",
                "job_id",
                "job_name",
                "scenario_attempt",
                "attempt_id",
            }
        ),
    )
    workflow = _expect_string(execution["workflow"], field="execution.workflow")
    run_id = _expect_int(execution["run_id"], field="execution.run_id", minimum=1)
    run_attempt = _expect_int(
        execution["run_attempt"], field="execution.run_attempt", minimum=1
    )
    _expect_string(execution["job_id"], field="execution.job_id", pattern=_SAFE_JOB_ID)
    _expect_string(execution["job_name"], field="execution.job_name")
    scenario_attempt = _expect_int(
        execution["scenario_attempt"],
        field="execution.scenario_attempt",
        minimum=1,
    )
    _expect_string(
        execution["attempt_id"],
        field="execution.attempt_id",
        pattern=_SAFE_ATTEMPT_ID,
    )
    if expected_run_attempt != 1 or run_attempt != 1 or scenario_attempt != 1:
        raise InstalledEvidenceError(
            "retry-only evidence cannot replace first-attempt installed evidence"
        )
    if (
        workflow != expected_workflow
        or run_id != expected_run_id
        or run_attempt != expected_run_attempt
        or execution["job_id"] != expected_job_id
    ):
        raise InstalledEvidenceError("installed evidence execution identity mismatch")
    return execution


def _validate_system(value: object) -> dict[str, object]:
    system = _expect_object(
        value,
        field="system",
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
    family = _expect_string(system["family"], field="system.family")
    display_version = _expect_string(
        system["display_version"],
        field="system.display_version",
        pattern=_DISPLAY_VERSION,
    )
    build = _expect_int(system["build_number"], field="system.build_number", minimum=1)
    _expect_int(
        system["update_build_revision"],
        field="system.update_build_revision",
        minimum=0,
    )
    if system["architecture"] != "x86_64":
        raise InstalledEvidenceError("installed matrix supports only Windows x86_64")
    _expect_sha256(system["image_sha256"], field="system.image_sha256")
    supported = (
        family == "windows-10" and display_version == "22H2" and build == 19045
    ) or (family == "windows-11" and build >= 22000)
    if not supported:
        raise InstalledEvidenceError(
            "installed evidence uses an unsupported Windows system"
        )
    return system


def _validate_account(value: object) -> dict[str, object]:
    account = _expect_object(
        value,
        field="account",
        fields=frozenset(
            {
                "account_type",
                "is_admin",
                "username_contains_non_ascii",
                "profile_path_contains_space",
            }
        ),
    )
    if account["account_type"] != "standard":
        raise InstalledEvidenceError("installed evidence requires a standard user")
    if _expect_bool(account["is_admin"], field="account.is_admin"):
        raise InstalledEvidenceError("administrator evidence is forbidden")
    _expect_bool(
        account["username_contains_non_ascii"],
        field="account.username_contains_non_ascii",
    )
    _expect_bool(
        account["profile_path_contains_space"],
        field="account.profile_path_contains_space",
    )
    return account


def _validate_signer(value: object, *, field: str) -> dict[str, object]:
    signer = _expect_object(
        value,
        field=field,
        fields=frozenset({"status", "subject", "certificate_sha256"}),
    )
    if signer["status"] != "Valid" or signer["subject"] != "CN=Microsoft Corporation":
        raise InstalledEvidenceError(f"{field} is not a valid Microsoft signer")
    _expect_sha256(signer["certificate_sha256"], field=f"{field}.certificate_sha256")
    return signer


def _validate_runtime(value: object, *, field: str) -> dict[str, object]:
    runtime = _expect_object(
        value,
        field=field,
        fields=frozenset({"state", "product_guid", "version", "channel", "signer"}),
    )
    state = runtime["state"]
    if state == "present":
        if runtime["product_guid"] != WEBVIEW2_PRODUCTION_GUID:
            raise InstalledEvidenceError(
                f"{field} is not the production WebView2 Runtime"
            )
        version = _expect_string(
            runtime["version"], field=f"{field}.version", pattern=_WEBVIEW_VERSION
        )
        if (
            tuple(int(component) for component in version.split("."))
            < MINIMUM_WEBVIEW2_VERSION
        ):
            raise InstalledEvidenceError(
                f"{field} is below the locked WebView2 minimum"
            )
        if runtime["channel"] != "evergreen":
            raise InstalledEvidenceError(
                f"{field} must use production Evergreen WebView2"
            )
        _validate_signer(runtime["signer"], field=f"{field}.signer")
    elif state == "absent":
        if any(
            runtime[name] is not None
            for name in ("product_guid", "version", "channel", "signer")
        ):
            raise InstalledEvidenceError(
                f"{field} absent state has contradictory metadata"
            )
    else:
        raise InstalledEvidenceError(f"{field}.state is invalid")
    return runtime


def _validate_webview(
    value: object, *, expected_installer_sha256: str
) -> dict[str, object]:
    webview = _expect_object(
        value,
        field="webview",
        fields=frozenset({"before", "installation", "after"}),
    )
    _validate_runtime(webview["before"], field="webview.before")
    installation = _expect_object(
        webview["installation"],
        field="webview.installation",
        fields=frozenset({"attempted", "exit_code", "installer_sha256"}),
    )
    attempted = _expect_bool(
        installation["attempted"], field="webview.installation.attempted"
    )
    exit_code = _expect_nullable_int(
        installation["exit_code"], field="webview.installation.exit_code"
    )
    digest = _expect_sha256(
        installation["installer_sha256"],
        field="webview.installation.installer_sha256",
    )
    if digest != expected_installer_sha256:
        raise InstalledEvidenceError(
            "WebView2 installer digest does not match identity"
        )
    if attempted != (exit_code is not None):
        raise InstalledEvidenceError(
            "WebView2 installation attempt and exit code contradict"
        )
    _validate_runtime(webview["after"], field="webview.after")
    return webview


def _validate_processes(value: object) -> dict[str, object]:
    processes = _expect_object(
        value,
        field="processes",
        fields=frozenset({"installer", "desktop_host", "sidecar", "uninstaller"}),
    )
    for name in ("installer", "desktop_host", "sidecar", "uninstaller"):
        process = _expect_object(
            processes[name],
            field=f"processes.{name}",
            fields=frozenset({"started", "elevated"}),
        )
        started = _expect_bool(process["started"], field=f"processes.{name}.started")
        elevated = _expect_nullable_bool(
            process["elevated"], field=f"processes.{name}.elevated"
        )
        if started and elevated is not False:
            raise InstalledEvidenceError(
                f"processes.{name} must be observed non-elevated"
            )
        if not started and elevated is not None:
            raise InstalledEvidenceError(
                f"processes.{name} cannot report elevation without starting"
            )
    installer = cast(dict[str, object], processes["installer"])
    if installer["started"] is not True:
        raise InstalledEvidenceError("installer process evidence is missing")
    return processes


def _validate_security(value: object) -> dict[str, object]:
    security = _expect_object(
        value,
        field="security",
        fields=frozenset({"uac_prompt_count", "elevation_requested"}),
    )
    if (
        _expect_int(
            security["uac_prompt_count"], field="security.uac_prompt_count", minimum=0
        )
        != 0
    ):
        raise InstalledEvidenceError("current-user installation must not trigger UAC")
    if _expect_bool(
        security["elevation_requested"], field="security.elevation_requested"
    ):
        raise InstalledEvidenceError(
            "current-user installation must not request elevation"
        )
    return security


def _validate_install(value: object) -> dict[str, object]:
    install = _expect_object(
        value,
        field="install",
        fields=frozenset(
            {"exit_code", "application_files_present", "shortcut_present", "launchable"}
        ),
    )
    _expect_int(install["exit_code"], field="install.exit_code")
    for name in ("application_files_present", "shortcut_present", "launchable"):
        _expect_bool(install[name], field=f"install.{name}")
    return install


def _validate_window(value: object) -> dict[str, object]:
    window = _expect_object(
        value,
        field="window",
        fields=frozenset(
            {
                "observed",
                "main_window_count",
                "title",
                "external_browser_window_count",
                "rendered_content_sha256",
            }
        ),
    )
    _expect_bool(window["observed"], field="window.observed")
    _expect_int(
        window["main_window_count"], field="window.main_window_count", minimum=0
    )
    title = window["title"]
    if title is not None:
        _expect_string(title, field="window.title")
    _expect_int(
        window["external_browser_window_count"],
        field="window.external_browser_window_count",
        minimum=0,
    )
    digest = window["rendered_content_sha256"]
    if digest is not None:
        _expect_sha256(digest, field="window.rendered_content_sha256")
    return window


def _validate_v1_canary(value: object) -> dict[str, object]:
    canary = _expect_object(
        value, field="v1_canary", fields=frozenset({"before", "after"})
    )
    snapshots: list[dict[str, object]] = []
    for name in ("before", "after"):
        snapshot = _expect_object(
            canary[name],
            field=f"v1_canary.{name}",
            fields=frozenset({"entry_count", "content_sha256"}),
        )
        _expect_int(
            snapshot["entry_count"],
            field=f"v1_canary.{name}.entry_count",
            minimum=1,
        )
        _expect_sha256(
            snapshot["content_sha256"],
            field=f"v1_canary.{name}.content_sha256",
        )
        snapshots.append(snapshot)
    if snapshots[0] != snapshots[1]:
        raise InstalledEvidenceError(
            "legacy v1 canary changed during installed journey"
        )
    return canary


def _validate_diagnostics(
    value: object, *, required_kinds: frozenset[str]
) -> dict[str, object]:
    summary = _expect_object(
        value,
        field="diagnostic_summary",
        fields=frozenset(
            {"archive_sha256", "entry_count", "redaction_scan", "records"}
        ),
    )
    _expect_sha256(summary["archive_sha256"], field="diagnostic_summary.archive_sha256")
    entry_count = _expect_int(
        summary["entry_count"], field="diagnostic_summary.entry_count", minimum=1
    )
    scan = _expect_object(
        summary["redaction_scan"],
        field="diagnostic_summary.redaction_scan",
        fields=frozenset(
            {"secret_match_count", "username_match_count", "absolute_path_match_count"}
        ),
    )
    for name in (
        "secret_match_count",
        "username_match_count",
        "absolute_path_match_count",
    ):
        if (
            _expect_int(
                scan[name], field=f"diagnostic_summary.redaction_scan.{name}", minimum=0
            )
            != 0
        ):
            raise InstalledEvidenceError("diagnostic evidence is not safely redacted")
    records = summary["records"]
    if not isinstance(records, list) or not records or len(records) > 64:
        raise InstalledEvidenceError(
            "diagnostic_summary.records must be a bounded array"
        )
    kinds: list[str] = []
    for index, raw_record in enumerate(records):
        record = _expect_object(
            raw_record,
            field=f"diagnostic_summary.records[{index}]",
            fields=frozenset({"kind", "sha256", "size_bytes"}),
        )
        kind = _expect_string(
            record["kind"], field=f"diagnostic_summary.records[{index}].kind"
        )
        if kind not in EVIDENCE_KINDS:
            raise InstalledEvidenceError(
                f"unsupported diagnostic evidence kind: {kind}"
            )
        _expect_sha256(
            record["sha256"], field=f"diagnostic_summary.records[{index}].sha256"
        )
        _expect_int(
            record["size_bytes"],
            field=f"diagnostic_summary.records[{index}].size_bytes",
            minimum=1,
        )
        kinds.append(kind)
    if entry_count != len(records):
        raise InstalledEvidenceError("diagnostic evidence entry count is inconsistent")
    if len(kinds) != len(set(kinds)):
        raise InstalledEvidenceError("diagnostic evidence kinds must be unique")
    missing = sorted(required_kinds - set(kinds))
    if missing:
        raise InstalledEvidenceError(
            f"installed evidence records are missing: {', '.join(missing)}"
        )
    return summary


def _validate_uninstall(value: object) -> dict[str, object]:
    uninstall = _expect_object(
        value,
        field="uninstall",
        fields=frozenset(
            {"attempted", "exit_code", "application_files_removed", "shortcuts_removed"}
        ),
    )
    attempted = _expect_bool(uninstall["attempted"], field="uninstall.attempted")
    exit_code = _expect_nullable_int(
        uninstall["exit_code"], field="uninstall.exit_code"
    )
    files_removed = _expect_nullable_bool(
        uninstall["application_files_removed"],
        field="uninstall.application_files_removed",
    )
    shortcuts_removed = _expect_nullable_bool(
        uninstall["shortcuts_removed"], field="uninstall.shortcuts_removed"
    )
    if attempted != (exit_code is not None):
        raise InstalledEvidenceError("uninstall attempt and exit code contradict")
    if not attempted and (files_removed is not None or shortcuts_removed is not None):
        raise InstalledEvidenceError("unattempted uninstall has contradictory results")
    return uninstall


def _require_success_semantics(
    document: Mapping[str, object], *, preinstalled: bool
) -> None:
    webview = cast(dict[str, object], document["webview"])
    before = cast(dict[str, object], webview["before"])
    after = cast(dict[str, object], webview["after"])
    installation = cast(dict[str, object], webview["installation"])
    expected_before = "present" if preinstalled else "absent"
    if before["state"] != expected_before or after["state"] != "present":
        raise InstalledEvidenceError(
            "WebView2 success scenario has contradictory states"
        )
    if preinstalled:
        if (
            installation["attempted"] is not False
            or installation["exit_code"] is not None
        ):
            raise InstalledEvidenceError(
                "preinstalled WebView2 must not be reinstalled"
            )
        if before != after:
            raise InstalledEvidenceError(
                "preinstalled WebView2 identity changed unexpectedly"
            )
    elif installation["attempted"] is not True or installation["exit_code"] != 0:
        raise InstalledEvidenceError("absent WebView2 must be installed successfully")

    install = cast(dict[str, object], document["install"])
    if install != {
        "exit_code": 0,
        "application_files_present": True,
        "shortcut_present": True,
        "launchable": True,
    }:
        raise InstalledEvidenceError(
            "successful installed scenario has contradictory install facts"
        )
    window = cast(dict[str, object], document["window"])
    if (
        window["observed"] is not True
        or window["main_window_count"] != 1
        or window["title"] != "Stock Desk"
        or window["external_browser_window_count"] != 0
        or window["rendered_content_sha256"] is None
    ):
        raise InstalledEvidenceError(
            "successful installed scenario lacks desktop window evidence"
        )
    uninstall = cast(dict[str, object], document["uninstall"])
    if uninstall != {
        "attempted": True,
        "exit_code": 0,
        "application_files_removed": True,
        "shortcuts_removed": True,
    }:
        raise InstalledEvidenceError(
            "successful installed scenario lacks clean uninstall evidence"
        )
    processes = cast(dict[str, object], document["processes"])
    if any(
        cast(dict[str, object], processes[name])["started"] is not True
        for name in processes
    ):
        raise InstalledEvidenceError(
            "successful installed scenario lacks process evidence"
        )


def _require_failure_semantics(document: Mapping[str, object]) -> None:
    webview = cast(dict[str, object], document["webview"])
    before = cast(dict[str, object], webview["before"])
    after = cast(dict[str, object], webview["after"])
    installation = cast(dict[str, object], webview["installation"])
    if (
        before["state"] != "absent"
        or after["state"] != "absent"
        or installation["attempted"] is not True
        or not isinstance(installation["exit_code"], int)
        or isinstance(installation["exit_code"], bool)
        or installation["exit_code"] == 0
    ):
        raise InstalledEvidenceError(
            "WebView2 failure scenario has contradictory states"
        )
    install = cast(dict[str, object], document["install"])
    if (
        install["exit_code"] == 0
        or install["application_files_present"] is not False
        or install["shortcut_present"] is not False
        or install["launchable"] is not False
    ):
        raise InstalledEvidenceError(
            "WebView2 failure must not leave a launchable installed application"
        )
    window = cast(dict[str, object], document["window"])
    if window != {
        "observed": False,
        "main_window_count": 0,
        "title": None,
        "external_browser_window_count": 0,
        "rendered_content_sha256": None,
    }:
        raise InstalledEvidenceError("WebView2 failure must not leave a desktop window")
    uninstall = cast(dict[str, object], document["uninstall"])
    if uninstall != {
        "attempted": False,
        "exit_code": None,
        "application_files_removed": None,
        "shortcuts_removed": None,
    }:
        raise InstalledEvidenceError(
            "failed installation must not claim uninstall evidence"
        )
    processes = cast(dict[str, object], document["processes"])
    for name in ("desktop_host", "sidecar", "uninstaller"):
        if cast(dict[str, object], processes[name])["started"] is not False:
            raise InstalledEvidenceError(
                "WebView2 failure started a process that could expose a partial application"
            )


def validate_evidence(
    raw: object,
    *,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_main_proof_sha256: str,
    expected_candidate_sha256: str,
    expected_webview_installer_sha256: str,
    expected_workflow: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_job_id_prefix: str,
) -> dict[str, Any]:
    """Validate one first-attempt installed journey against exact artifact identity."""

    expected = _validate_expected_identity(
        source_sha=expected_source_sha,
        source_tree=expected_source_tree,
        main_proof_sha256=expected_main_proof_sha256,
        candidate_sha256=expected_candidate_sha256,
        webview_installer_sha256=expected_webview_installer_sha256,
    )
    document = _expect_object(
        raw,
        field="installed evidence",
        fields=frozenset(
            {
                "schema_version",
                "artifact",
                "scenario",
                "identity",
                "execution",
                "system",
                "account",
                "webview",
                "processes",
                "security",
                "install",
                "window",
                "v1_canary",
                "diagnostic_summary",
                "uninstall",
            }
        ),
    )
    if document["schema_version"] != SCHEMA_VERSION or document["artifact"] != ARTIFACT:
        raise InstalledEvidenceError("installed evidence schema identity is invalid")
    scenario = _expect_string(document["scenario"], field="scenario")
    if scenario not in SCENARIOS:
        raise InstalledEvidenceError(
            f"unsupported installed evidence scenario: {scenario}"
        )
    identity = _validate_identity(document["identity"], expected=expected)
    _validate_execution(
        document["execution"],
        expected_workflow=_expect_string(expected_workflow, field="expected workflow"),
        expected_run_id=_expect_int(
            expected_run_id, field="expected run_id", minimum=1
        ),
        expected_run_attempt=_expect_int(
            expected_run_attempt, field="expected run_attempt", minimum=1
        ),
        expected_job_id=f"{_expect_string(expected_job_id_prefix, field='expected job id prefix', pattern=_SAFE_JOB_ID)}-{scenario}",
    )
    _validate_system(document["system"])
    _validate_account(document["account"])
    _validate_webview(
        document["webview"],
        expected_installer_sha256=cast(str, identity["webview_installer_sha256"]),
    )
    _validate_processes(document["processes"])
    _validate_security(document["security"])
    _validate_install(document["install"])
    _validate_window(document["window"])
    _validate_v1_canary(document["v1_canary"])
    required_kinds = (
        FAILURE_EVIDENCE_KINDS
        if scenario == "webview-install-failure"
        else SUCCESS_EVIDENCE_KINDS
    )
    _validate_diagnostics(document["diagnostic_summary"], required_kinds=required_kinds)
    _validate_uninstall(document["uninstall"])
    if scenario == "webview-install-failure":
        _require_failure_semantics(document)
    else:
        _require_success_semantics(
            document, preinstalled=scenario == "webview-preinstalled"
        )
    return copy.deepcopy(cast(dict[str, Any], document))


verify_evidence = validate_evidence


def validate_matrix(
    raw_documents: Sequence[object],
    *,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_main_proof_sha256: str,
    expected_candidate_sha256: str,
    expected_webview_installer_sha256: str,
    expected_workflow: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_job_id_prefix: str,
) -> tuple[dict[str, Any], ...]:
    """Validate the complete three-scenario first-attempt Windows matrix."""

    documents = tuple(
        validate_evidence(
            raw,
            expected_source_sha=expected_source_sha,
            expected_source_tree=expected_source_tree,
            expected_main_proof_sha256=expected_main_proof_sha256,
            expected_candidate_sha256=expected_candidate_sha256,
            expected_webview_installer_sha256=expected_webview_installer_sha256,
            expected_workflow=expected_workflow,
            expected_run_id=expected_run_id,
            expected_run_attempt=expected_run_attempt,
            expected_job_id_prefix=expected_job_id_prefix,
        )
        for raw in raw_documents
    )
    scenarios = [cast(str, document["scenario"]) for document in documents]
    if len(documents) != len(SCENARIOS) or set(scenarios) != SCENARIOS:
        raise InstalledEvidenceError(
            "installed evidence matrix must contain exactly one preinstalled, absent, and failure scenario"
        )
    executions = [
        cast(dict[str, object], document["execution"]) for document in documents
    ]
    run_identities = {
        (item["workflow"], item["run_id"], item["run_attempt"]) for item in executions
    }
    if len(run_identities) != 1:
        raise InstalledEvidenceError(
            "installed matrix execution identity does not match"
        )
    if len({item["job_id"] for item in executions}) != len(documents) or len(
        {item["attempt_id"] for item in executions}
    ) != len(documents):
        raise InstalledEvidenceError(
            "installed matrix first-attempt identities must be unique"
        )
    systems = [cast(dict[str, object], document["system"]) for document in documents]
    if {item["family"] for item in systems} != {"windows-10", "windows-11"}:
        raise InstalledEvidenceError(
            "installed matrix must cover Windows 10 and Windows 11"
        )
    accounts = [cast(dict[str, object], document["account"]) for document in documents]
    if not any(item["username_contains_non_ascii"] is True for item in accounts):
        raise InstalledEvidenceError(
            "installed matrix lacks a non-ASCII username profile"
        )
    if not any(item["profile_path_contains_space"] is True for item in accounts):
        raise InstalledEvidenceError(
            "installed matrix lacks a profile path containing spaces"
        )
    return documents


verify_matrix = validate_matrix


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InstalledEvidenceError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def read_evidence(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise InstalledEvidenceError(f"installed evidence is missing: {path.name}")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise InstalledEvidenceError(
            f"cannot read installed evidence: {path.name}"
        ) from error
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise InstalledEvidenceError(
            f"installed evidence exceeds size limit: {path.name}"
        )
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstalledEvidenceError(
            f"installed evidence JSON is invalid: {path.name}"
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify first-attempt installed Windows and WebView2 evidence"
    )
    parser.add_argument("evidence", nargs="+", type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--main-proof-sha256", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--webview-installer-sha256", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--job-id-prefix", required=True)
    arguments = parser.parse_args(argv)
    try:
        documents = [read_evidence(path) for path in arguments.evidence]
        validate_matrix(
            documents,
            expected_source_sha=arguments.source_sha,
            expected_source_tree=arguments.source_tree,
            expected_main_proof_sha256=arguments.main_proof_sha256,
            expected_candidate_sha256=arguments.candidate_sha256,
            expected_webview_installer_sha256=arguments.webview_installer_sha256,
            expected_workflow=arguments.workflow,
            expected_run_id=arguments.run_id,
            expected_run_attempt=arguments.run_attempt,
            expected_job_id_prefix=arguments.job_id_prefix,
        )
    except InstalledEvidenceError as error:
        print(
            f"windows installed evidence verification failed: {error}", file=sys.stderr
        )
        return 1
    print(
        "windows installed evidence schema verified: 3 first-attempt scenarios; "
        "raw observations require separate verification"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
