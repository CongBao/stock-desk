"""Fail-closed verifier for GitHub Hosted Windows interaction evidence."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import re
import sys
from typing import Any, cast


SCHEMA_VERSION = "stock-desk-windows-hosted-automation-v1"
INPUT_METHOD = "windows-uia-and-cdp-automation"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "input_method",
    "physical_mouse_click",
    "source_sha",
    "source_tree",
    "candidate_sha256",
    "installed_executable_sha256",
    "capture_nonce",
    "process_id",
    "main_window_handle",
    "webview2",
    "actions",
    "host_exit_code",
    "hosted_runner_limitations",
}
_ACTION_FIELDS = {
    "sequence",
    "action",
    "target",
    "invocation",
    "physical_mouse_click",
    "observed_state",
}
_NATIVE_TARGET_FIELDS = {
    "process_id",
    "window_handle",
    "automation_id",
    "name",
    "runtime_id",
    "titlebar_ancestor",
    "enabled",
    "offscreen",
}
_WEBVIEW_TARGET_FIELDS = {"role", "name", "exact"}
EXPECTED_ACTIONS = (
    (1, "native-close-open-dialog", "uia-invoke-pattern", "exit-dialog-visible"),
    (
        2,
        "webview-cancel-dialog",
        "playwright-cdp-click",
        "dialog-hidden-host-alive",
    ),
    (
        3,
        "native-close-reopen-dialog",
        "uia-invoke-pattern",
        "exit-dialog-visible",
    ),
    (4, "webview-confirm-exit", "playwright-cdp-click", "host-exited-zero"),
)
EXPECTED_LIMITATIONS = [
    "github-hosted-windows-server-is-not-win10-or-win11-desktop",
    "runner-account-is-not-real-standard-user-acceptance",
    "automation-does-not-prove-uac-secure-desktop-or-physical-input",
]


class HostedAutomationEvidenceError(ValueError):
    """Hosted evidence does not prove the required bounded automation flow."""


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 256 * 1024:
            raise HostedAutomationEvidenceError("hosted evidence size is invalid")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HostedAutomationEvidenceError(
            "hosted evidence JSON is unreadable"
        ) from error
    if not isinstance(value, dict):
        raise HostedAutomationEvidenceError("hosted evidence root must be an object")
    return cast(dict[str, Any], value)


def _exact_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise HostedAutomationEvidenceError(f"{label} fields are not canonical")


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HostedAutomationEvidenceError(f"{label} must be a positive integer")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostedAutomationEvidenceError(f"{label} must be a nonempty string")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise HostedAutomationEvidenceError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _validate_native_target(
    value: object, *, process_id: int, window_handle: int
) -> None:
    target = _mapping(value, "native target")
    _exact_fields(target, _NATIVE_TARGET_FIELDS, "native target")
    if target["process_id"] != process_id or target["window_handle"] != window_handle:
        raise HostedAutomationEvidenceError("native target does not bind the host")
    automation_id = target["automation_id"]
    name = target["name"]
    if not isinstance(automation_id, str) or not isinstance(name, str):
        raise HostedAutomationEvidenceError("native target identity is invalid")
    if not automation_id.strip() and not name.strip():
        raise HostedAutomationEvidenceError("native target identity is empty")
    _nonempty_string(target["runtime_id"], "native runtime ID")
    if (
        target["titlebar_ancestor"] is not True
        or target["enabled"] is not True
        or target["offscreen"] is not False
    ):
        raise HostedAutomationEvidenceError("native target state is invalid")


def _validate_webview_target(value: object, *, expected_name: str) -> None:
    target = _mapping(value, "WebView target")
    _exact_fields(target, _WEBVIEW_TARGET_FIELDS, "WebView target")
    if target != {"role": "button", "name": expected_name, "exact": True}:
        raise HostedAutomationEvidenceError("WebView target identity is invalid")


def _validate_actions(value: object, *, process_id: int, window_handle: int) -> None:
    if not isinstance(value, list) or len(value) != len(EXPECTED_ACTIONS):
        raise HostedAutomationEvidenceError("hosted action sequence is invalid")
    expected_names = (None, "取消", None, "退出应用")
    for index, (item, expected, expected_name) in enumerate(
        zip(value, EXPECTED_ACTIONS, expected_names, strict=True)
    ):
        action = _mapping(item, f"action {index + 1}")
        _exact_fields(action, _ACTION_FIELDS, f"action {index + 1}")
        observed = (
            action["sequence"],
            action["action"],
            action["invocation"],
            action["observed_state"],
        )
        if observed != expected or action["physical_mouse_click"] is not False:
            raise HostedAutomationEvidenceError("hosted action sequence is invalid")
        if expected_name is None:
            _validate_native_target(
                action["target"],
                process_id=process_id,
                window_handle=window_handle,
            )
        else:
            _validate_webview_target(action["target"], expected_name=expected_name)


def verify_evidence(
    path: Path,
    *,
    source_sha: str,
    source_tree: str,
    candidate_sha256: str,
) -> dict[str, object]:
    """Verify one exact-source Windows Hosted interaction evidence document."""

    evidence = _load(path)
    _exact_fields(evidence, _TOP_LEVEL_FIELDS, "hosted evidence")
    if (
        evidence["schema_version"] != SCHEMA_VERSION
        or evidence["input_method"] != INPUT_METHOD
        or evidence["physical_mouse_click"] is not False
    ):
        raise HostedAutomationEvidenceError("hosted input claim is invalid")
    for supplied, pattern, label in (
        (source_sha, _HEX40, "expected source SHA"),
        (source_tree, _HEX40, "expected source tree"),
        (candidate_sha256, _HEX64, "expected candidate SHA-256"),
        (
            evidence["installed_executable_sha256"],
            _HEX64,
            "installed executable SHA-256",
        ),
    ):
        if not isinstance(supplied, str) or pattern.fullmatch(supplied) is None:
            raise HostedAutomationEvidenceError(f"{label} is invalid")
    if evidence["source_sha"] != source_sha or evidence["source_tree"] != source_tree:
        raise HostedAutomationEvidenceError("source identity does not match")
    if evidence["candidate_sha256"] != candidate_sha256:
        raise HostedAutomationEvidenceError("candidate identity does not match")
    capture_nonce = evidence["capture_nonce"]
    if not isinstance(capture_nonce, str) or _UUID4.fullmatch(capture_nonce) is None:
        raise HostedAutomationEvidenceError("capture nonce is invalid")
    process_id = _positive_integer(evidence["process_id"], "process ID")
    window_handle = _positive_integer(
        evidence["main_window_handle"], "main window handle"
    )
    webview = _mapping(evidence["webview2"], "WebView2 identity")
    _exact_fields(
        webview,
        {"user_data_dir", "cdp_port", "browser_process_id"},
        "WebView2 identity",
    )
    _nonempty_string(webview["user_data_dir"], "WebView2 user data directory")
    cdp_port = _positive_integer(webview["cdp_port"], "CDP port")
    if cdp_port > 65535:
        raise HostedAutomationEvidenceError("CDP port is outside the valid range")
    _positive_integer(webview["browser_process_id"], "WebView2 browser process ID")
    _validate_actions(
        evidence["actions"], process_id=process_id, window_handle=window_handle
    )
    if evidence["host_exit_code"] != 0:
        raise HostedAutomationEvidenceError("host exit code is not zero")
    if evidence["hosted_runner_limitations"] != EXPECTED_LIMITATIONS:
        raise HostedAutomationEvidenceError("hosted runner limitations are incomplete")
    return cast(dict[str, object], evidence)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    try:
        verify_evidence(
            parsed.evidence,
            source_sha=parsed.source_sha,
            source_tree=parsed.source_tree,
            candidate_sha256=parsed.candidate_sha256,
        )
    except HostedAutomationEvidenceError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
