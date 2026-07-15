#!/usr/bin/env python3
"""Verify the real Windows desktop VM matrix from raw, broker-bound bytes.

The guest and the external broker are observation producers only.  This module
is the sole public acceptance authority: it validates the protected 11-case
policy, hashes every raw byte, derives public-safe per-case evidence, and emits
an exact-SHA aggregate receipt.  Synthetic or GitHub-hosted browser evidence is
never accepted by this verifier.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any, Final
import urllib.parse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from PIL import Image


SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
GIT_RE: Final = re.compile(r"^[0-9a-f]{40}$")
UTC_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SAFE_PATH_RE: Final = re.compile(r"^raw/[a-z0-9][a-z0-9._-]{0,95}$")
CERTIFICATE_THUMBPRINT_RE: Final = re.compile(r"^[0-9A-F]{40,64}$")
PUBLIC_TEXT_LEAK_RE: Final = re.compile(
    r"(authorization\s*:|bearer\s+[a-z0-9._-]+|api[_-]?key\s*[=:]"
    r"|password\s*[=:]|github[_-]?token"
    + r"|[a-z]:\\"
    + r"users\\"
    + r"|/"
    + r"home/"
    + r"|/"
    + r"users/)",
    re.IGNORECASE,
)
CASE_RE: Final = re.compile(
    r"^(?P<profile>win10-22h2|win11)-dpi-(?P<dpi>100|125|150|175|200)"
    r"(?P<offline>-webview-offline)?$"
)
WORKFLOW_PATH: Final = ".github/workflows/windows-installed.yml"
BROKER_AUDIENCE: Final = "stock-desk-windows-installed-acceptance"
EXPECTED_ROUTES: Final = {
    "market",
    "formula",
    "backtest",
    "analysis",
    "tasks",
    "settings",
}
EXPECTED_DIALOGS: Final = {
    "about",
    "exit-confirmation",
    "update-confirmation",
    "sidecar-recovery",
    "model-settings",
    "market-pool",
    "contextual-guidance",
}
EXPECTED_SIZES: Final = ((1366, 768), (640, 360))
EXPECTED_DPIS: Final = (100, 125, 150, 175, 200)
STOCK_DESK_SIGNED_FILENAMES: Final = {
    "desktop-host": "stock-desk-desktop.exe",
    "sidecar": "stock-desk-sidecar.exe",
    "nsis-installer": "stock-desk-signed-nsis.exe",
}
EXPECTED_RECORD_MEDIA: Final = {
    "observation-stream": "application/x-ndjson",
    "install-log": "text/plain; charset=utf-8",
    "smartscreen-observation": "application/json",
    "motw-zone-identifier": "text/plain; charset=utf-8",
    "uia-action-trace": "application/json",
    "uia-tree": "application/json",
    "focus-region-contact-sheet": "image/png",
    "window-capture-standard": "image/png",
    "window-capture-narrow": "image/png",
    "failure-diagnostic": "text/plain; charset=utf-8",
}
SUCCESS_EVENT_KINDS: Final = {
    "system",
    "account-token",
    "hardware-observation",
    "network-observation",
    "display-observation",
    "webview-before",
    "webview-installation",
    "webview-child-process-token",
    "webview-after",
    "installer-process-token",
    "desktop-host-process-token",
    "sidecar-process-token",
    "stock-desk-authenticode",
    "uninstaller-process-token",
    "uac-observation",
    "install-observation",
    "first-use-journey",
    "uia-matrix",
    "filesystem-observation",
    "window-observation",
    "v1-canary-before",
    "v1-canary-after",
    "redaction-scan",
    "uninstall-observation",
}
FAILURE_EVENT_KINDS: Final = {
    "system",
    "account-token",
    "hardware-observation",
    "network-observation",
    "webview-before",
    "webview-installation",
    "webview-child-process-token",
    "webview-after",
    "installer-process-token",
    "desktop-host-process-token",
    "sidecar-process-token",
    "stock-desk-authenticode",
    "uninstaller-process-token",
    "uac-observation",
    "install-observation",
    "filesystem-observation",
    "window-observation",
    "v1-canary-before",
    "v1-canary-after",
    "redaction-scan",
    "uninstall-observation",
}


class DesktopEvidenceError(ValueError):
    """Raised when raw Windows evidence is not release-authoritative."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(value: object) -> str:
    return _sha256(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode()
    )


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DesktopEvidenceError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise DesktopEvidenceError(f"{label} must be an array")
    return value


def _exact(value: object, keys: Iterable[str], label: str) -> dict[str, Any]:
    result = _object(value, label)
    expected = set(keys)
    if set(result) != expected:
        raise DesktopEvidenceError(f"{label} fields are not closed")
    return result


def _allowed(
    value: object,
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    label: str,
) -> dict[str, Any]:
    result = _object(value, label)
    required_keys = set(required)
    allowed_keys = required_keys | set(optional)
    if not required_keys <= set(result) or not set(result) <= allowed_keys:
        raise DesktopEvidenceError(f"{label} fields are not closed")
    return result


def _text(value: object, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise DesktopEvidenceError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    result = _text(value, label, maximum=64)
    if SHA256_RE.fullmatch(result) is None:
        raise DesktopEvidenceError(f"{label} is not a lowercase SHA-256")
    return result


def _git(value: object, label: str) -> str:
    result = _text(value, label, maximum=40)
    if GIT_RE.fullmatch(result) is None:
        raise DesktopEvidenceError(f"{label} is not a lowercase Git object")
    return result


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DesktopEvidenceError(f"{label} is invalid")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise DesktopEvidenceError(f"{label} must be boolean")
    return value


def _timestamp(value: object, label: str) -> str:
    result = _text(value, label, maximum=20)
    if UTC_RE.fullmatch(result) is None:
        raise DesktopEvidenceError(f"{label} is not a closed UTC timestamp")
    datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return result


def expected_case_ids() -> tuple[str, ...]:
    cases = [
        f"{profile}-dpi-{dpi}"
        for profile in ("win10-22h2", "win11")
        for dpi in EXPECTED_DPIS
    ]
    cases.append("win10-22h2-dpi-100-webview-offline")
    return tuple(cases)


def _read_json(
    path: Path, label: str, *, maximum: int = 2 * 1024 * 1024
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise DesktopEvidenceError(f"{label} is missing or unsafe")
    data = path.read_bytes()
    if not data or len(data) > maximum:
        raise DesktopEvidenceError(f"{label} has an invalid size")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopEvidenceError(f"{label} is unreadable") from error
    return _object(value, label)


def validate_snapshot_policy(value: object) -> dict[str, dict[str, Any]]:
    policy = _exact(value, ("schema", "broker", "assignments"), "snapshot policy")
    if policy["schema"] != "stock-desk-windows-vm-snapshot-policy-v2":
        raise DesktopEvidenceError("snapshot policy schema is unsupported")
    broker = _exact(
        policy["broker"],
        (
            "identity",
            "oidc_audience",
            "lease_ttl_seconds",
            "restore_after_each_case",
            "raw_only",
        ),
        "snapshot broker policy",
    )
    if (
        broker["identity"] != "stock-desk-ephemeral-windows-vm-broker-v1"
        or broker["oidc_audience"] != BROKER_AUDIENCE
        or _integer(broker["lease_ttl_seconds"], "broker lease TTL", minimum=2700)
        > 3600
        or broker["restore_after_each_case"] is not True
        or broker["raw_only"] is not True
    ):
        raise DesktopEvidenceError("snapshot broker policy is not fail closed")

    assignments = _array(policy["assignments"], "snapshot assignments")
    if len(assignments) != 11:
        raise DesktopEvidenceError(
            "snapshot policy must contain exactly 11 assignments"
        )
    by_case: dict[str, dict[str, Any]] = {}
    fallback_cases: list[str] = []
    for index, raw in enumerate(assignments):
        assignment = _exact(
            raw,
            (
                "case_id",
                "guest_profile",
                "controller_label",
                "scenario",
                "dpi_percent",
                "snapshot_id",
                "snapshot_sha256",
                "image_sha256",
                "system",
                "webview_initial_state",
                "failure_injection",
                "data_path",
                "network",
                "account",
                "logical_window_sizes",
            ),
            f"snapshot assignment {index}",
        )
        case_id = _text(assignment["case_id"], "case id", maximum=64)
        match = CASE_RE.fullmatch(case_id)
        if match is None or case_id in by_case:
            raise DesktopEvidenceError(
                "snapshot policy case identity is invalid or duplicated"
            )
        profile = match.group("profile")
        dpi = int(match.group("dpi"))
        offline = match.group("offline") is not None
        if (
            assignment["guest_profile"] != profile
            or assignment["controller_label"] != f"stock-desk-vm-controller-{profile}"
            or assignment["dpi_percent"] != dpi
            or assignment["scenario"]
            != ("webview-install-failure" if offline else "installed-first-use")
        ):
            raise DesktopEvidenceError(
                f"snapshot assignment identity mismatch: {case_id}"
            )
        _digest(assignment["snapshot_sha256"], "snapshot digest")
        _digest(assignment["image_sha256"], "image digest")
        _text(assignment["snapshot_id"], "snapshot id", maximum=128)
        system = _exact(
            assignment["system"],
            (
                "family",
                "display_version",
                "build_number",
                "update_build_revision",
                "architecture",
            ),
            "assigned system",
        )
        expected_family = "windows-10" if profile == "win10-22h2" else "windows-11"
        if (
            system["family"] != expected_family
            or system["architecture"] != "x86_64"
            or (
                profile == "win10-22h2"
                and (
                    system["display_version"] != "22H2"
                    or system["build_number"] != 19045
                )
            )
            or (
                profile == "win11"
                and _integer(system["build_number"], "Windows 11 build", minimum=22000)
                < 22000
            )
        ):
            raise DesktopEvidenceError(f"unsupported OS assignment: {case_id}")
        expected_webview = "absent" if dpi == 100 or offline else "present"
        if assignment["webview_initial_state"] != expected_webview:
            raise DesktopEvidenceError(f"WebView2 initial state mismatch: {case_id}")
        injection = assignment["failure_injection"]
        if offline:
            injection_value = _exact(
                injection, ("identity", "sha256"), "failure injection"
            )
            if (
                injection_value["identity"]
                != "stock-desk-webview2-offline-install-failure-v1"
            ):
                raise DesktopEvidenceError("failure injection identity is not fixed")
            _digest(injection_value["sha256"], "failure injection digest")
        elif injection is not None:
            raise DesktopEvidenceError(f"unexpected failure injection: {case_id}")
        account = _exact(
            assignment["account"],
            (
                "account_type",
                "is_admin",
                "username_contains_non_ascii",
                "profile_path_contains_space",
            ),
            "assigned account",
        )
        if account != {
            "account_type": "standard",
            "is_admin": False,
            "username_contains_non_ascii": True,
            "profile_path_contains_space": True,
        }:
            raise DesktopEvidenceError(
                f"assignment is not a Chinese-name standard user: {case_id}"
            )
        sizes = _array(assignment["logical_window_sizes"], "logical window sizes")
        observed_sizes = tuple(
            (
                _integer(
                    _exact(item, ("width", "height"), "logical size")["width"],
                    "logical width",
                ),
                _integer(_object(item, "logical size")["height"], "logical height"),
            )
            for item in sizes
        )
        if observed_sizes != EXPECTED_SIZES:
            raise DesktopEvidenceError(f"logical window matrix mismatch: {case_id}")
        expected_data_path = (
            "primary-blocked-fallback" if case_id == "win11-dpi-150" else "primary"
        )
        if assignment["data_path"] != expected_data_path:
            raise DesktopEvidenceError(f"fixed fallback assignment mismatch: {case_id}")
        if assignment["data_path"] == "primary-blocked-fallback":
            fallback_cases.append(case_id)
        network = _exact(
            assignment["network"],
            ("profile", "policy_sha256", "expected_provider"),
            "assigned network",
        )
        _digest(network["policy_sha256"], "assigned network policy")
        expected_network = (
            ("webview-offline-fixed", "none")
            if offline
            else (
                ("primary-blocked", "baostock")
                if case_id == "win11-dpi-150"
                else ("normal", "akshare")
            )
        )
        if (network["profile"], network["expected_provider"]) != expected_network:
            raise DesktopEvidenceError(
                f"network assignment is not the fixed provider route: {case_id}"
            )
        by_case[case_id] = assignment
    if set(by_case) != set(expected_case_ids()):
        raise DesktopEvidenceError(
            "snapshot policy does not cover the exact 11-case matrix"
        )
    if fallback_cases != ["win11-dpi-150"]:
        raise DesktopEvidenceError(
            "exactly win11-dpi-150 must prove primary-provider fallback"
        )
    return by_case


def _records(
    package: Path, manifest: Mapping[str, Any]
) -> tuple[dict[str, bytes], str]:
    records = _array(manifest.get("records"), "raw records")
    if not 3 <= len(records) <= 12:
        raise DesktopEvidenceError("raw record count is outside the closed boundary")
    package_root = package.resolve()
    seen_paths: set[str] = set()
    by_kind: dict[str, bytes] = {}
    inventory: list[dict[str, object]] = []
    total = 0
    for index, raw in enumerate(records):
        record = _exact(
            raw,
            ("kind", "path", "sha256", "size_bytes", "media_type"),
            f"raw record {index}",
        )
        kind = _text(record["kind"], "raw record kind", maximum=32)
        relative = _text(record["path"], "raw record path", maximum=100)
        if (
            SAFE_PATH_RE.fullmatch(relative) is None
            or relative in seen_paths
            or kind in by_kind
        ):
            raise DesktopEvidenceError(
                "raw record path or role is unsafe or duplicated"
            )
        if record["media_type"] != EXPECTED_RECORD_MEDIA.get(kind):
            raise DesktopEvidenceError("raw record role or media type is not reviewed")
        path = (package / relative).resolve()
        if package_root not in path.parents or not path.is_file() or path.is_symlink():
            raise DesktopEvidenceError("raw record escapes its package")
        data = path.read_bytes()
        total += len(data)
        if (
            not data
            or len(data) > 8 * 1024 * 1024
            or total > 32 * 1024 * 1024
            or len(data) != _integer(record["size_bytes"], "raw record size", minimum=1)
            or _sha256(data) != _digest(record["sha256"], "raw record digest")
        ):
            raise DesktopEvidenceError(
                "raw record bytes do not match the closed manifest"
            )
        seen_paths.add(relative)
        by_kind[kind] = data
        inventory.append(
            {"path": relative, "sha256": _sha256(data), "size_bytes": len(data)}
        )
    if "observation-stream" not in by_kind or "install-log" not in by_kind:
        raise DesktopEvidenceError("raw package lacks mandatory observation roles")
    return by_kind, _canonical_digest(inventory)


def _parse_events(data: bytes) -> dict[str, dict[str, Any]]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise DesktopEvidenceError("observation stream is not UTF-8") from error
    if not lines:
        raise DesktopEvidenceError("observation stream is empty")
    events: dict[str, dict[str, Any]] = {}
    for expected_sequence, line in enumerate(lines, start=1):
        try:
            event = _exact(
                json.loads(line),
                ("sequence", "captured_at_utc", "kind", "producer", "value"),
                "raw observation event",
            )
        except json.JSONDecodeError as error:
            raise DesktopEvidenceError(
                "observation stream contains invalid JSON"
            ) from error
        if event["sequence"] != expected_sequence:
            raise DesktopEvidenceError("observation sequence is not contiguous")
        _timestamp(event["captured_at_utc"], "observation timestamp")
        kind = _text(event["kind"], "observation kind", maximum=64)
        _text(event["producer"], "observation producer", maximum=128)
        if kind in events:
            raise DesktopEvidenceError(f"observation kind is duplicated: {kind}")
        event["value"] = _object(event["value"], f"{kind} observation")
        events[kind] = event
    return events


def _validate_png_capture(
    data: bytes, *, logical_size: tuple[int, int], dpi_percent: int
) -> None:
    Image.MAX_IMAGE_PIXELS = 20_000_000
    try:
        with Image.open(io.BytesIO(data)) as candidate:
            candidate.verify()
        with Image.open(io.BytesIO(data)) as candidate:
            if candidate.format != "PNG" or candidate.mode not in ("RGB", "RGBA"):
                raise DesktopEvidenceError(
                    "target-window capture is not a reviewed RGB PNG"
                )
            expected = tuple(round(value * dpi_percent / 100) for value in logical_size)
            if any(
                abs(actual - planned) > 4
                for actual, planned in zip(candidate.size, expected, strict=True)
            ):
                raise DesktopEvidenceError(
                    "target-window capture dimensions do not match logical size and DPI"
                )
            sample = candidate.convert("RGB").resize((64, 64))
            if len(sample.getcolors(maxcolors=4096) or []) < 8:
                raise DesktopEvidenceError(
                    "target-window capture is blank or visually degenerate"
                )
    except (OSError, ValueError) as error:
        raise DesktopEvidenceError(
            "target-window capture is not a valid bounded PNG"
        ) from error


def _validate_uia_raw_records(
    action_bytes: bytes,
    tree_bytes: bytes,
    focus_region_bytes: bytes,
    *,
    uia: Mapping[str, Any],
    expected_primary_actions: int,
) -> None:
    try:
        actions = _array(json.loads(action_bytes), "UIA action trace")
        trees = _array(json.loads(tree_bytes), "UIA tree trace")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopEvidenceError("UIA trace records are not valid JSON") from error
    primary_actions = 0
    for sequence, raw in enumerate(actions, start=1):
        action = _exact(
            raw,
            (
                "sequence",
                "captured_at_utc",
                "action",
                "target_id",
                "target_name",
                "target_control_type",
                "major_click",
                "outcome",
            ),
            "UIA action",
        )
        if action["sequence"] != sequence or action["action"] not in (
            "invoke",
            "keyboard-enter",
        ):
            raise DesktopEvidenceError(
                "UIA action sequence or input method is unsupported"
            )
        _timestamp(action["captured_at_utc"], "UIA action timestamp")
        if not isinstance(action["major_click"], bool) or action["outcome"] not in (
            "invoked",
            "activated",
        ):
            raise DesktopEvidenceError("UIA action outcome is malformed")
        primary_actions += int(action["major_click"])
    if primary_actions != expected_primary_actions:
        raise DesktopEvidenceError(
            "UIA action trace differs from the first-use action budget"
        )
    traced_checks: dict[str, object] = {}
    for raw in trees:
        tree = _exact(raw, ("identity", "check"), "UIA tree")
        identity = _text(tree["identity"], "UIA tree identity", maximum=128)
        if identity in traced_checks:
            raise DesktopEvidenceError("UIA tree identity is duplicated")
        traced_checks[identity] = tree["check"]
    embedded_checks: dict[str, object] = {}
    for group_name in ("routes", "dialogs"):
        for group in _array(uia.get(group_name), f"UIA {group_name}"):
            item = _object(group, "UIA group")
            for check in _array(item.get("checks"), "UIA group checks"):
                size = _object(
                    _object(check, "UIA embedded check").get("logical_size"), "UIA size"
                )
                kind = "route" if group_name == "routes" else "dialog"
                suffix = (
                    "standard"
                    if kind == "route" and size.get("width") == 1366
                    else ("narrow" if kind == "route" else str(size.get("width")))
                )
                embedded_checks[f"{kind}:{item.get('id')}:{suffix}"] = check
    if _canonical_digest(traced_checks) != _canonical_digest(embedded_checks):
        raise DesktopEvidenceError(
            "UIA public tree trace differs from the derived matrix"
        )
    focus_manifest = _object(uia.get("focus_regions"), "focus-region manifest")
    try:
        with Image.open(io.BytesIO(focus_region_bytes)) as raw_sheet:
            raw_sheet.verify()
        with Image.open(io.BytesIO(focus_region_bytes)) as raw_sheet:
            sheet = raw_sheet.convert("RGB")
            if raw_sheet.format != "PNG" or sheet.size != (
                focus_manifest["width"],
                focus_manifest["height"],
            ):
                raise DesktopEvidenceError(
                    "focus-region contact sheet dimensions are contradictory"
                )
            raw_regions: dict[str, tuple[tuple[int, int], bytes]] = {}
            for raw_capture in _array(
                focus_manifest.get("captures"), "focus-region captures"
            ):
                capture = _object(raw_capture, "focus-region capture")
                region = sheet.crop(
                    (
                        capture["x"],
                        capture["y"],
                        capture["x"] + capture["width"],
                        capture["y"] + capture["height"],
                    )
                )
                pixels = region.tobytes()
                sample = region.resize((32, 32))
                if len(sample.getcolors(maxcolors=1024) or []) < 2:
                    raise DesktopEvidenceError("focus-region raw pixels are degenerate")
                raw_regions[capture["id"]] = (region.size, pixels)
    except (OSError, ValueError, KeyError) as error:
        raise DesktopEvidenceError(
            "focus-region contact sheet is not a valid bounded PNG"
        ) from error
    focus_paths = [
        _object(check, "UIA focus check")["focus_evidence"]
        for group_name in ("routes", "dialogs")
        for group in _array(uia.get(group_name), f"UIA {group_name}")
        for check in _array(_object(group, "UIA group").get("checks"), "UIA checks")
    ]
    keyboard = _object(uia.get("keyboard"), "UIA keyboard")
    focus_paths.extend(
        [
            *(_array(keyboard.get("onboarding_tab_paths"), "onboarding paths")),
            *(_array(keyboard.get("auxiliary_tab_paths"), "auxiliary paths")),
        ]
    )
    for raw_path in focus_paths:
        focus_path = _object(raw_path, "focus path")
        try:
            before = raw_regions[focus_path["unfocused_region_id"]]
            after = raw_regions[focus_path["focused_region_id"]]
        except KeyError as error:
            raise DesktopEvidenceError(
                "focus path references undeclared raw region pixels"
            ) from error
        if before[0] != after[0] or before[1] == after[1]:
            raise DesktopEvidenceError(
                "visible focus is not independently derived from raw region pixels"
            )


def _value(events: Mapping[str, Mapping[str, Any]], kind: str) -> dict[str, Any]:
    try:
        return _object(events[kind]["value"], f"{kind} value")
    except KeyError as error:
        raise DesktopEvidenceError(
            f"required observation is missing: {kind}"
        ) from error


def _validate_account(value: object) -> dict[str, Any]:
    account = _exact(
        value,
        (
            "account_type",
            "is_admin",
            "administrator_group_member",
            "linked_token_present",
            "token_elevation_type",
            "integrity_level",
            "integrity_rid",
            "username_contains_non_ascii",
            "profile_path_contains_space",
        ),
        "account observation",
    )
    for field in ("administrator_group_member", "linked_token_present", "is_admin"):
        if _boolean(account.get(field), f"account {field}"):
            raise DesktopEvidenceError("guest account has administrator authority")
    if (
        account.get("account_type") != "standard"
        or account.get("token_elevation_type") != "default"
        or account.get("integrity_level") != "medium"
        or account.get("integrity_rid") != 8192
        or account.get("username_contains_non_ascii") is not True
        or account.get("profile_path_contains_space") is not True
    ):
        raise DesktopEvidenceError(
            "guest is not the required Chinese-name standard-user profile"
        )
    return account


def _validate_display(value: object, *, dpi_percent: int) -> dict[str, Any]:
    display = _exact(
        value,
        (
            "requested_scale_percent",
            "get_dpi_for_window",
            "get_dpi_for_system",
            "get_dpi_for_monitor_x",
            "get_dpi_for_monitor_y",
            "window_dpi_awareness_context",
            "logical_to_physical_roundtrip_max_error_px",
            "dpi_virtualized",
            "logical_window_sizes",
        ),
        "display observation",
    )
    expected_dpi = dpi_percent * 96 // 100
    roundtrip_error = display.get("logical_to_physical_roundtrip_max_error_px")
    if (
        display.get("requested_scale_percent") != dpi_percent
        or display.get("get_dpi_for_window") != expected_dpi
        or display.get("get_dpi_for_system") != expected_dpi
        or display.get("get_dpi_for_monitor_x") != expected_dpi
        or display.get("get_dpi_for_monitor_y") != expected_dpi
        or display.get("window_dpi_awareness_context") != "per-monitor-v2"
        or display.get("dpi_virtualized") is not False
        or not isinstance(roundtrip_error, (int, float))
        or roundtrip_error > 1
    ):
        raise DesktopEvidenceError(
            "Win32 DPI APIs do not prove the assigned physical scale"
        )
    sizes = _array(display.get("logical_window_sizes"), "display logical window sizes")
    observed = []
    for raw in sizes:
        size = _exact(
            raw,
            (
                "width",
                "height",
                "physical_width",
                "physical_height",
                "within_work_area",
                "clipped_component_count",
                "overlap_count",
            ),
            "display logical window size",
        )
        observed.append((size.get("width"), size.get("height")))
        if (
            size.get("within_work_area") is not True
            or size.get("clipped_component_count") != 0
            or size.get("overlap_count") != 0
        ):
            raise DesktopEvidenceError("window geometry is clipped or overlapping")
    if tuple(observed) != EXPECTED_SIZES:
        raise DesktopEvidenceError("display observation omits a required window size")
    return display


def _validate_hardware(value: object) -> dict[str, Any]:
    hardware = _exact(
        value,
        (
            "architecture",
            "logical_processor_count",
            "memory_bytes",
            "free_disk_bytes",
            "graphics_adapter_sha256",
            "screen_physical_pixels",
            "timezone",
            "locale",
        ),
        "hardware observation",
    )
    if (
        hardware.get("architecture") != "x86_64"
        or _integer(
            hardware.get("logical_processor_count"),
            "logical processor count",
            minimum=2,
        )
        < 2
        or _integer(
            hardware.get("memory_bytes"), "physical memory", minimum=4 * 1024**3
        )
        < 4 * 1024**3
        or _integer(hardware.get("free_disk_bytes"), "free disk", minimum=5 * 1024**3)
        < 5 * 1024**3
    ):
        raise DesktopEvidenceError("VM hardware is below the recorded release baseline")
    _digest(hardware.get("graphics_adapter_sha256"), "graphics adapter identity")
    screen = _exact(
        hardware.get("screen_physical_pixels"), ("width", "height"), "physical screen"
    )
    if (
        _integer(screen.get("width"), "physical screen width", minimum=640) < 640
        or _integer(screen.get("height"), "physical screen height", minimum=360) < 360
    ):
        raise DesktopEvidenceError(
            "VM physical screen cannot host the required viewport"
        )
    _text(hardware.get("timezone"), "VM timezone", maximum=64)
    _text(hardware.get("locale"), "VM locale", maximum=32)
    return hardware


def _validate_network(
    value: object, *, assignment: Mapping[str, Any], success: bool
) -> dict[str, Any]:
    network = _exact(
        value,
        (
            "capture_api",
            "profile",
            "policy_sha256",
            "unexpected_host_count",
            "telemetry_request_count",
            "proxy_used",
            "records",
        ),
        "network observation",
    )
    policy = _object(assignment["network"], "assigned network")
    if (
        network.get("capture_api") != "DNS Client + WFP/ETW"
        or network.get("profile") != policy["profile"]
        or network.get("policy_sha256") != policy["policy_sha256"]
        or network.get("unexpected_host_count") != 0
        or network.get("telemetry_request_count") != 0
        or network.get("proxy_used") is not False
    ):
        raise DesktopEvidenceError(
            "network capture is not bound to the fixed no-telemetry policy"
        )
    records = _array(network.get("records"), "network records")
    providers: set[str] = set()
    operations: set[str] = set()
    for raw in records:
        record = _allowed(
            raw,
            required=(
                "provider",
                "operation",
                "host",
                "started_at_utc",
                "completed_at_utc",
                "tls_system_validation",
                "outcome",
            ),
            optional=("payload_sha256", "cutoff_utc", "row_count"),
            label="network record",
        )
        provider = _text(record.get("provider"), "network provider", maximum=32)
        operation = _text(record.get("operation"), "network operation", maximum=32)
        _text(record.get("host"), "network host", maximum=253)
        _timestamp(record.get("started_at_utc"), "network start")
        _timestamp(record.get("completed_at_utc"), "network completion")
        if record.get("tls_system_validation") is not True or record.get(
            "outcome"
        ) not in ("success", "blocked-by-policy", "offline-failure"):
            raise DesktopEvidenceError(
                "network record lacks system TLS validation or a closed outcome"
            )
        providers.add(provider)
        operations.add(operation)
        if record.get("outcome") == "success" and operation in (
            "catalog",
            "daily-bars",
        ):
            _digest(record.get("payload_sha256"), f"{operation} network payload")
    if success:
        expected_provider = policy["expected_provider"]
        if expected_provider not in providers or not {"catalog", "daily-bars"}.issubset(
            operations
        ):
            raise DesktopEvidenceError(
                "network capture does not prove catalog and bars from the assigned provider"
            )
        if assignment["data_path"] == "primary-blocked-fallback":
            if "akshare" not in providers or not any(
                record.get("outcome") == "blocked-by-policy" for record in records
            ):
                raise DesktopEvidenceError(
                    "network capture lacks the fixed AKShare block before fallback"
                )
    elif not records or not any(
        record.get("operation") == "webview-runtime"
        and record.get("outcome") == "offline-failure"
        for record in records
    ):
        raise DesktopEvidenceError(
            "offline failure lacks its fixed network observation"
        )
    return network


def _validate_tab_focus_evidence(
    raw: object,
    *,
    label: str,
    expected_target_id: str | None = None,
    require_activated: bool,
) -> dict[str, Any]:
    keys = {
        "target_id",
        "target_name",
        "initial_focus_id",
        "tab_sequence",
        "tab_input_count",
        "focus_observation_method",
        "target_has_keyboard_focus",
        "unfocused_region_id",
        "focused_region_id",
        "focus_region_changed",
    }
    if require_activated:
        keys.add("activated")
    evidence = _exact(raw, keys, label)
    target_id = _text(evidence.get("target_id"), f"{label} target", maximum=128)
    _text(evidence.get("target_name"), f"{label} target name", maximum=128)
    _text(evidence.get("initial_focus_id"), f"{label} initial focus", maximum=128)
    tab_sequence = _array(evidence.get("tab_sequence"), f"{label} Tab sequence")
    unfocused_region_id = _text(
        evidence.get("unfocused_region_id"), f"{label} unfocused pixel region"
    )
    focused_region_id = _text(
        evidence.get("focused_region_id"), f"{label} focused pixel region"
    )
    if (
        not tab_sequence
        or len(tab_sequence) > 128
        or any(
            not isinstance(identity, str) or not identity for identity in tab_sequence
        )
        or tab_sequence[-1] != target_id
        or evidence.get("tab_input_count") != len(tab_sequence)
        or evidence.get("focus_observation_method")
        != "uia-focused-element-after-real-tab"
        or evidence.get("target_has_keyboard_focus") is not True
        or evidence.get("focus_region_changed") is not True
        or focused_region_id == unfocused_region_id
        or (expected_target_id is not None and target_id != expected_target_id)
        or (require_activated and evidence.get("activated") is not True)
    ):
        raise DesktopEvidenceError(
            f"{label} is not an observed real-Tab focus transition"
        )
    return evidence


def _validate_layout_check(raw: object, *, label: str) -> tuple[int, int]:
    check = _exact(
        raw,
        (
            "logical_size",
            "window_bounds",
            "component_bounds",
            "clipped_component_count",
            "overlap_count",
            "tab_sequence",
            "focused_element_id",
            "focus_visible",
            "focus_evidence",
            "escape_result",
        ),
        label,
    )
    size = _exact(
        check.get("logical_size"), ("width", "height"), f"{label} logical size"
    )
    observed = (
        _integer(size.get("width"), "layout width"),
        _integer(size.get("height"), "layout height"),
    )
    window = _exact(
        check.get("window_bounds"),
        ("x", "y", "width", "height"),
        f"{label} window bounds",
    )
    if any(
        not isinstance(window.get(field), int)
        for field in ("x", "y", "width", "height")
    ):
        raise DesktopEvidenceError(f"{label} has malformed window geometry")
    if window["width"] <= 0 or window["height"] <= 0:
        raise DesktopEvidenceError(f"{label} has empty window geometry")
    bounds = _array(check.get("component_bounds"), f"{label} component bounds")
    if not bounds:
        raise DesktopEvidenceError(f"{label} has no component geometry")
    components: dict[str, dict[str, Any]] = {}
    for item in bounds:
        rectangle = _exact(
            item,
            (
                "id",
                "parent_id",
                "x",
                "y",
                "width",
                "height",
                "is_offscreen",
                "is_enabled",
                "keyboard_focusable",
                "hit_test_id",
            ),
            f"{label} component rectangle",
        )
        component_id = _text(rectangle.get("id"), f"{label} component id", maximum=128)
        if component_id in components:
            raise DesktopEvidenceError(f"{label} duplicates a component identity")
        if any(
            not isinstance(rectangle.get(field), int)
            for field in ("x", "y", "width", "height")
        ):
            raise DesktopEvidenceError(f"{label} has malformed component geometry")
        if rectangle["width"] <= 0 or rectangle["height"] <= 0:
            raise DesktopEvidenceError(f"{label} has empty component geometry")
        if (
            rectangle.get("is_offscreen") is not False
            or rectangle.get("is_enabled") is not True
            or rectangle.get("hit_test_id") != component_id
        ):
            raise DesktopEvidenceError(
                f"{label} component is offscreen, disabled, or occluded"
            )
        if not isinstance(rectangle.get("parent_id"), (str, type(None))):
            raise DesktopEvidenceError(
                f"{label} component parent identity is malformed"
            )
        components[component_id] = rectangle
    clipped = 0
    for rectangle in components.values():
        if (
            rectangle["x"] < window["x"]
            or rectangle["y"] < window["y"]
            or rectangle["x"] + rectangle["width"] > window["x"] + window["width"]
            or rectangle["y"] + rectangle["height"] > window["y"] + window["height"]
        ):
            clipped += 1
    overlap = 0
    component_list = list(components.values())
    for index, left in enumerate(component_list):
        for right in component_list[index + 1 :]:
            if left.get("parent_id") != right.get("parent_id"):
                continue
            horizontal = min(
                left["x"] + left["width"], right["x"] + right["width"]
            ) - max(left["x"], right["x"])
            vertical = min(
                left["y"] + left["height"], right["y"] + right["height"]
            ) - max(left["y"], right["y"])
            if horizontal > 1 and vertical > 1:
                overlap += 1
    if clipped != 0 or overlap != 0:
        raise DesktopEvidenceError(
            f"{label} has verifier-derived clipping or peer overlap"
        )
    if (
        check.get("clipped_component_count") != clipped
        or check.get("overlap_count") != overlap
    ):
        raise DesktopEvidenceError(
            f"{label} guest geometry summary does not match raw rectangles"
        )
    tab_sequence = _array(check.get("tab_sequence"), f"{label} tab sequence")
    focusable = {
        component_id
        for component_id, rectangle in components.items()
        if rectangle.get("keyboard_focusable") is True
    }
    if set(tab_sequence) != focusable or len(tab_sequence) != len(set(tab_sequence)):
        raise DesktopEvidenceError(
            f"{label} Tab sequence does not cover each focusable control once"
        )
    visual_order = [
        component_id
        for component_id, rectangle in sorted(
            components.items(), key=lambda item: (item[1]["y"], item[1]["x"], item[0])
        )
        if component_id in focusable
    ]
    if tab_sequence != visual_order:
        raise DesktopEvidenceError(f"{label} Tab order differs from visual order")
    _validate_tab_focus_evidence(
        check.get("focus_evidence"),
        label=f"{label} focus evidence",
        expected_target_id=check.get("focused_element_id"),
        require_activated=False,
    )
    if (
        check.get("focused_element_id") not in focusable
        or check.get("focus_visible") is not True
        or check.get("escape_result") not in ("closed-safe", "confirmation-preserved")
    ):
        raise DesktopEvidenceError(f"{label} failed focus or safe Esc acceptance")
    return observed


def _validate_uia(value: object, *, expected_driver_sha256: str) -> dict[str, Any]:
    uia = _exact(
        value,
        (
            "schema",
            "api",
            "driver_sha256",
            "routes",
            "dialogs",
            "keyboard",
            "focus_regions",
            "narrow_sidebar",
        ),
        "UI Automation matrix",
    )
    if (
        uia.get("schema") != "stock-desk-windows-uia-matrix-v1"
        or uia.get("api") != "Windows UI Automation 3 + Win32"
        or uia.get("driver_sha256") != expected_driver_sha256
    ):
        raise DesktopEvidenceError(
            "UI Automation driver identity is not reviewed and digest-bound"
        )
    routes = _array(uia.get("routes"), "UIA routes")
    route_ids: set[str] = set()
    for route in routes:
        item = _exact(route, ("id", "checks"), "UIA route")
        route_id = _text(item.get("id"), "UIA route id", maximum=32)
        if route_id in route_ids:
            raise DesktopEvidenceError("UIA route is duplicated")
        route_ids.add(route_id)
        checks = _array(item.get("checks"), f"UIA route {route_id} checks")
        if (
            tuple(
                _validate_layout_check(check, label=f"route {route_id}")
                for check in checks
            )
            != EXPECTED_SIZES
        ):
            raise DesktopEvidenceError(
                f"UIA route size matrix is incomplete: {route_id}"
            )
    if route_ids != EXPECTED_ROUTES:
        raise DesktopEvidenceError(
            "UIA route matrix does not cover all six core routes"
        )
    dialogs = _array(uia.get("dialogs"), "UIA dialogs")
    dialog_ids: set[str] = set()
    for dialog in dialogs:
        item = _exact(dialog, ("id", "checks"), "UIA dialog")
        dialog_id = _text(item.get("id"), "UIA dialog id", maximum=64)
        if dialog_id in dialog_ids:
            raise DesktopEvidenceError("UIA dialog is duplicated")
        dialog_ids.add(dialog_id)
        checks = _array(item.get("checks"), f"UIA dialog {dialog_id} checks")
        if (
            tuple(
                _validate_layout_check(check, label=f"dialog {dialog_id}")
                for check in checks
            )
            != EXPECTED_SIZES
        ):
            raise DesktopEvidenceError(
                f"UIA dialog size matrix is incomplete: {dialog_id}"
            )
    if dialog_ids != EXPECTED_DIALOGS:
        raise DesktopEvidenceError(
            "UIA matrix does not cover every release-relevant dialog"
        )
    keyboard = _exact(
        uia.get("keyboard"),
        (
            "pure_keyboard_journey",
            "focus_visible",
            "tab_order_valid",
            "safe_escape",
            "focus_observation_count",
            "onboarding_tab_paths",
            "auxiliary_tab_paths",
        ),
        "UIA keyboard summary",
    )
    onboarding_paths = _array(
        keyboard.get("onboarding_tab_paths"), "onboarding keyboard paths"
    )
    expected_targets = (
        "开始设置",
        "使用此来源并继续",
        "同步并继续",
        "进入行情工作区",
    )
    if len(onboarding_paths) != len(expected_targets):
        raise DesktopEvidenceError("onboarding keyboard path count is not exactly four")
    validated_paths = [
        _validate_tab_focus_evidence(
            path,
            label=f"onboarding keyboard path {index}",
            require_activated=True,
        )
        for index, path in enumerate(onboarding_paths, start=1)
    ]
    auxiliary_paths = [
        _validate_tab_focus_evidence(
            path,
            label=f"auxiliary keyboard path {index}",
            require_activated=True,
        )
        for index, path in enumerate(
            _array(keyboard.get("auxiliary_tab_paths"), "auxiliary keyboard paths"),
            start=1,
        )
    ]
    focus_observation_count = _integer(
        keyboard.get("focus_observation_count"),
        "UIA focus observation count",
    )
    if (
        keyboard.get("pure_keyboard_journey") is not True
        or keyboard.get("focus_visible") is not True
        or keyboard.get("tab_order_valid") is not True
        or keyboard.get("safe_escape") is not True
        or focus_observation_count < 30
        or focus_observation_count != 26 + len(validated_paths) + len(auxiliary_paths)
        or tuple(path["target_name"] for path in validated_paths) != expected_targets
    ):
        raise DesktopEvidenceError("UIA keyboard journey is incomplete")
    focus_regions = _exact(
        uia.get("focus_regions"),
        ("schema", "media_kind", "width", "height", "captures"),
        "UIA focus-region manifest",
    )
    sheet_width = _integer(
        focus_regions.get("width"), "focus-region sheet width", minimum=2
    )
    sheet_height = _integer(
        focus_regions.get("height"), "focus-region sheet height", minimum=4
    )
    if (
        focus_regions.get("schema") != "stock-desk-focus-region-contact-sheet-v1"
        or focus_regions.get("media_kind") != "focus-region-contact-sheet"
        or sheet_width > 2048
        or sheet_height > 32768
    ):
        raise DesktopEvidenceError("focus-region contact sheet contract is unsupported")
    focus_captures = _array(
        focus_regions.get("captures"), "focus-region capture entries"
    )
    if len(focus_captures) != focus_observation_count * 2:
        raise DesktopEvidenceError("focus-region capture count is not raw-derived")
    capture_ids: set[str] = set()
    expected_y = 0
    for raw_capture in focus_captures:
        capture = _exact(
            raw_capture,
            ("id", "x", "y", "width", "height"),
            "focus-region capture",
        )
        capture_id = _text(capture.get("id"), "focus-region capture id", maximum=64)
        width = _integer(capture.get("width"), "focus-region capture width", minimum=2)
        height = _integer(
            capture.get("height"), "focus-region capture height", minimum=2
        )
        if (
            capture_id in capture_ids
            or capture.get("x") != 0
            or capture.get("y") != expected_y
            or width > sheet_width
            or expected_y + height > sheet_height
        ):
            raise DesktopEvidenceError("focus-region capture layout is contradictory")
        capture_ids.add(capture_id)
        expected_y += height
    if expected_y != sheet_height:
        raise DesktopEvidenceError("focus-region contact sheet has undeclared pixels")
    evidence_region_ids = {
        region_id
        for group_name in ("routes", "dialogs")
        for group in _array(uia.get(group_name), f"UIA {group_name}")
        for check in _array(_object(group, "UIA group").get("checks"), "UIA checks")
        for region_id in (
            _object(check, "UIA check")["focus_evidence"]["unfocused_region_id"],
            _object(check, "UIA check")["focus_evidence"]["focused_region_id"],
        )
    }
    evidence_region_ids.update(
        region_id
        for path in (*validated_paths, *auxiliary_paths)
        for region_id in (path["unfocused_region_id"], path["focused_region_id"])
    )
    if evidence_region_ids != capture_ids:
        raise DesktopEvidenceError(
            "focus-region contact sheet differs from UIA focus evidence"
        )
    sidebar = _exact(
        uia.get("narrow_sidebar"),
        (
            "logical_size",
            "collapsed_before",
            "toggle_control_type",
            "toggle_semantic_name",
            "expanded_after",
            "expanded_reflow",
            "chart_x_shift",
            "sidebar_chart_overlap_pixels",
        ),
        "UIA narrow sidebar evidence",
    )
    chart_x_shift = sidebar.get("chart_x_shift")
    if (
        sidebar.get("logical_size") != {"width": 640, "height": 360}
        or sidebar.get("collapsed_before") is not True
        or sidebar.get("toggle_control_type") != "button"
        or sidebar.get("toggle_semantic_name") not in ("展开导航", "展开自选与最近访问")
        or sidebar.get("expanded_after") is not True
        or sidebar.get("expanded_reflow") is not True
        or not isinstance(chart_x_shift, int)
        or chart_x_shift <= 0
        or sidebar.get("sidebar_chart_overlap_pixels") != 0
    ):
        raise DesktopEvidenceError(
            "narrow sidebar is not a semantic icon control with chart reflow"
        )
    return uia


def _validate_journey(value: object, *, data_path: str) -> dict[str, Any]:
    journey = _exact(
        value,
        (
            "elapsed_seconds",
            "primary_click_count",
            "onboarding_steps",
            "instrument",
            "real_data",
            "demo",
            "kline_rendered",
            "source",
            "fallback",
        ),
        "first-use journey",
    )
    elapsed = journey.get("elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not 0 < elapsed <= 180
    ):
        raise DesktopEvidenceError("first real K-line exceeded the 180-second budget")
    if _integer(journey.get("primary_click_count"), "primary click count") > 5:
        raise DesktopEvidenceError("first real K-line exceeded five primary clicks")
    if journey.get("onboarding_steps") != [
        "welcome",
        "data_preparation",
        "instrument_selection",
        "synchronization",
    ]:
        raise DesktopEvidenceError(
            "first-use journey did not complete the four-step onboarding"
        )
    instrument = _exact(
        journey.get("instrument"),
        ("symbol", "name", "exchange", "instrument_kind", "period"),
        "first-use instrument",
    )
    if (
        instrument.get("symbol") != "000001.SS"
        or instrument.get("name") != "上证指数"
        or instrument.get("instrument_kind") != "index"
        or journey.get("real_data") is not True
        or journey.get("demo") is not False
        or journey.get("kline_rendered") is not True
    ):
        raise DesktopEvidenceError(
            "first chart is not the real canonical Shanghai Composite index"
        )
    source = _exact(
        journey.get("source"),
        (
            "provider",
            "provider_label",
            "cutoff_utc",
            "row_count",
            "catalog_sha256",
            "bars_sha256",
        ),
        "first-use source",
    )
    _text(source.get("provider"), "data provider", maximum=64)
    _timestamp(source.get("cutoff_utc"), "data cutoff")
    _integer(source.get("row_count"), "real K-line row count", minimum=1)
    _digest(source.get("catalog_sha256"), "catalog dataset digest")
    _digest(source.get("bars_sha256"), "daily bars dataset digest")
    fallback = _exact(
        journey.get("fallback"),
        (
            "primary_blocked",
            "fallback_used",
            "whole_segment",
            "primary_provider",
            "fallback_provider",
        ),
        "provider fallback",
    )
    if data_path == "primary-blocked-fallback":
        if (
            fallback.get("primary_blocked") is not True
            or fallback.get("fallback_used") is not True
            or fallback.get("whole_segment") is not True
            or source.get("provider") != fallback.get("fallback_provider")
        ):
            raise DesktopEvidenceError(
                "primary-provider blocked fallback was not proven as a whole segment"
            )
        _text(fallback.get("primary_provider"), "blocked primary provider", maximum=64)
        _text(fallback.get("fallback_provider"), "fallback provider", maximum=64)
    elif (
        fallback.get("primary_blocked") is not False
        or fallback.get("fallback_used") is not False
        or fallback.get("whole_segment") is not True
    ):
        raise DesktopEvidenceError(
            "normal provider path contradicts its policy assignment"
        )
    return journey


def _validate_process(
    value: object, label: str, *, expected_started: bool
) -> dict[str, Any]:
    process = _exact(
        value,
        ("role", "started", "elevated", "integrity_level", "integrity_rid"),
        label,
    )
    if process.get("started") is not expected_started:
        raise DesktopEvidenceError(f"{label} start observation is contradictory")
    if expected_started and (
        process.get("elevated") is not False
        or process.get("integrity_level") != "medium"
        or process.get("integrity_rid") != 8192
    ):
        raise DesktopEvidenceError(f"{label} was elevated")
    return process


def _validate_stock_desk_authenticode(
    value: object,
    *,
    expected_components: Mapping[str, str],
    expected_signer_subject: str,
    expected_certificate_thumbprint: str,
    expected_timestamp_subject: str,
    include_installed_components: bool,
) -> dict[str, Any]:
    """Derive trusted Stock Desk identities from raw WinVerifyTrust observations."""
    observation = _exact(
        value,
        ("verification_api", "artifacts"),
        "Stock Desk Authenticode observation",
    )
    if (
        observation.get("verification_api")
        != "Get-AuthenticodeSignature/WinVerifyTrust"
    ):
        raise DesktopEvidenceError(
            "Stock Desk signature observation did not use WinVerifyTrust"
        )
    expected_roles = (
        set(STOCK_DESK_SIGNED_FILENAMES)
        if include_installed_components
        else {"nsis-installer"}
    )
    if set(expected_components) != set(STOCK_DESK_SIGNED_FILENAMES):
        raise DesktopEvidenceError("signed Stock Desk component identity is not closed")
    artifacts_raw = _array(observation.get("artifacts"), "Stock Desk signatures")
    if len(artifacts_raw) != len(expected_roles):
        raise DesktopEvidenceError("Stock Desk signature role inventory is incomplete")
    artifacts: dict[str, dict[str, Any]] = {}
    for raw in artifacts_raw:
        artifact = _exact(
            raw,
            (
                "role",
                "file_name",
                "sha256",
                "status",
                "signer_subject",
                "certificate_thumbprint",
                "timestamp_subject",
                "timestamp_thumbprint",
            ),
            "Stock Desk Authenticode artifact",
        )
        role = _text(artifact.get("role"), "Stock Desk signature role", maximum=32)
        if role not in expected_roles or role in artifacts:
            raise DesktopEvidenceError(
                "Stock Desk signature role is missing or duplicated"
            )
        digest = _digest(artifact.get("sha256"), f"{role} signed digest")
        signer_subject = _text(artifact.get("signer_subject"), f"{role} signer subject")
        certificate_thumbprint = _text(
            artifact.get("certificate_thumbprint"),
            f"{role} certificate thumbprint",
            maximum=64,
        )
        timestamp_subject = _text(
            artifact.get("timestamp_subject"), f"{role} timestamp subject"
        )
        timestamp_thumbprint = _text(
            artifact.get("timestamp_thumbprint"),
            f"{role} timestamp thumbprint",
            maximum=64,
        )
        if (
            artifact.get("status") != "Valid"
            or artifact.get("file_name") != STOCK_DESK_SIGNED_FILENAMES[role]
            or digest != expected_components[role]
            or signer_subject != expected_signer_subject
            or certificate_thumbprint != expected_certificate_thumbprint
            or timestamp_subject != expected_timestamp_subject
            or CERTIFICATE_THUMBPRINT_RE.fullmatch(certificate_thumbprint) is None
            or CERTIFICATE_THUMBPRINT_RE.fullmatch(timestamp_thumbprint) is None
        ):
            raise DesktopEvidenceError(
                f"{role} is unsigned, substituted, or not bound to the approved signer"
            )
        artifacts[role] = artifact
    if set(artifacts) != expected_roles:
        raise DesktopEvidenceError("Stock Desk signature role inventory is incomplete")
    return {
        "verification_api": observation["verification_api"],
        "artifacts": artifacts,
    }


def _validate_smartscreen_raw_records(
    observation_bytes: bytes,
    motw_bytes: bytes,
    *,
    assignment: Mapping[str, Any],
    expected_candidate_sha256: str,
    expected_adapter_sha256: str,
) -> dict[str, Any]:
    """Validate broker-bound raw SmartScreen evidence from a fresh Windows VM."""
    try:
        motw_text = motw_bytes.decode("utf-8")
        observation = _exact(
            json.loads(observation_bytes),
            (
                "schema",
                "case_id",
                "candidate",
                "observer",
                "download",
                "launch",
                "reputation",
            ),
            "SmartScreen raw observation",
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopEvidenceError("SmartScreen raw evidence is unreadable") from error
    lines = [line.strip() for line in motw_text.replace("\r\n", "\n").split("\n")]
    if not lines or lines[0] != "[ZoneTransfer]":
        raise DesktopEvidenceError("candidate lacks a raw ZoneTransfer MOTW stream")
    zone_fields: dict[str, str] = {}
    allowed_zone_fields = {
        "ZoneId",
        "HostUrl",
        "ReferrerUrl",
        "LastWriterPackageFamilyName",
    }
    for line in lines[1:]:
        if not line:
            continue
        if "=" not in line:
            raise DesktopEvidenceError("candidate MOTW stream is malformed")
        key, value = line.split("=", 1)
        if key not in allowed_zone_fields or key in zone_fields or not value:
            raise DesktopEvidenceError("candidate MOTW stream fields are not closed")
        zone_fields[key] = value
    if zone_fields.get("ZoneId") != "3" or "HostUrl" not in zone_fields:
        raise DesktopEvidenceError("candidate is not marked as an Internet download")
    for field in ("HostUrl", "ReferrerUrl"):
        url = zone_fields.get(field)
        if url is None:
            continue
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise DesktopEvidenceError("candidate MOTW contains a non-public URL")

    if (
        observation.get("schema") != "stock-desk-smartscreen-raw-observation-v1"
        or observation.get("case_id") != assignment["case_id"]
    ):
        raise DesktopEvidenceError("SmartScreen observation identity is invalid")
    candidate = _exact(
        observation.get("candidate"),
        ("sha256", "file_name"),
        "SmartScreen candidate",
    )
    if (
        _digest(candidate.get("sha256"), "SmartScreen candidate digest")
        != expected_candidate_sha256
        or candidate.get("file_name") != "stock-desk-signed-nsis.exe"
    ):
        raise DesktopEvidenceError("SmartScreen observed a substituted candidate")
    observer = _exact(
        observation.get("observer"),
        (
            "identity",
            "source",
            "adapter_sha256",
            "snapshot_sha256",
            "image_sha256",
            "machine_state",
        ),
        "SmartScreen observer",
    )
    if (
        observer.get("identity") != "stock-desk-protected-smartscreen-observer-v1"
        or observer.get("source") != "external-protected-vm-observer"
        or observer.get("adapter_sha256") != expected_adapter_sha256
        or observer.get("snapshot_sha256") != assignment["snapshot_sha256"]
        or observer.get("image_sha256") != assignment["image_sha256"]
        or observer.get("machine_state") != "restored-clean-snapshot"
    ):
        raise DesktopEvidenceError(
            "SmartScreen evidence is not from the protected fresh-machine observer"
        )
    download = _exact(
        observation.get("download"),
        (
            "method",
            "source_url",
            "tls_system_validation",
            "completed_at_utc",
            "byte_count",
            "candidate_sha256",
            "zone_identifier_sha256",
        ),
        "SmartScreen download",
    )
    if (
        download.get("method") != "https-internet-download"
        or download.get("source_url") != zone_fields["HostUrl"]
        or download.get("tls_system_validation") is not True
        or _integer(download.get("byte_count"), "SmartScreen download bytes", minimum=1)
        < 1
        or download.get("candidate_sha256") != expected_candidate_sha256
        or download.get("zone_identifier_sha256") != _sha256(motw_bytes)
    ):
        raise DesktopEvidenceError(
            "SmartScreen evidence lacks the real HTTPS download and MOTW boundary"
        )
    download_time = datetime.strptime(
        _timestamp(download.get("completed_at_utc"), "SmartScreen download completion"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    launch = _exact(
        observation.get("launch"),
        (
            "api",
            "requested_at_utc",
            "account_token",
            "candidate_sha256",
        ),
        "SmartScreen launch",
    )
    launch_time = datetime.strptime(
        _timestamp(launch.get("requested_at_utc"), "SmartScreen launch request"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    if (
        launch.get("api") != "ShellExecuteExW"
        or launch.get("account_token") != "standard-user-medium-integrity"
        or launch.get("candidate_sha256") != expected_candidate_sha256
        or launch_time < download_time
    ):
        raise DesktopEvidenceError(
            "SmartScreen candidate was not ShellExecute-launched as a standard user"
        )
    reputation = _exact(
        observation.get("reputation"),
        (
            "provider",
            "policy_mode",
            "disposition",
            "decision_source",
            "cloud_service_contact_count",
            "correlation_sha256",
            "completed_at_utc",
            "event_records",
            "window_samples",
        ),
        "SmartScreen reputation",
    )
    correlation = _digest(
        reputation.get("correlation_sha256"), "SmartScreen correlation"
    )
    completed_time = datetime.strptime(
        _timestamp(reputation.get("completed_at_utc"), "SmartScreen completion"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    if (
        reputation.get("provider") != "Microsoft Defender SmartScreen"
        or reputation.get("policy_mode") != "warn-or-block-unrecognized-apps"
        or reputation.get("disposition") != "allowed-no-warning"
        or reputation.get("decision_source") != "cloud-reputation"
        or _integer(
            reputation.get("cloud_service_contact_count"),
            "SmartScreen cloud contact count",
            minimum=1,
        )
        < 1
        or completed_time < launch_time
    ):
        raise DesktopEvidenceError(
            "SmartScreen reputation was unavailable, warned, blocked, or unobserved"
        )
    event_records = _array(reputation.get("event_records"), "SmartScreen event records")
    if not event_records:
        raise DesktopEvidenceError("SmartScreen evidence has no raw provider events")
    previous_record_id = 0
    for raw_event in event_records:
        event = _exact(
            raw_event,
            (
                "provider",
                "event_id",
                "record_id",
                "captured_at_utc",
                "correlation_sha256",
                "payload_sha256",
            ),
            "SmartScreen provider event",
        )
        record_id = _integer(
            event.get("record_id"), "SmartScreen event record id", minimum=1
        )
        if (
            event.get("provider") != "Microsoft-Windows-SmartScreen"
            or _integer(event.get("event_id"), "SmartScreen event id", minimum=1) < 1
            or record_id <= previous_record_id
            or event.get("correlation_sha256") != correlation
        ):
            raise DesktopEvidenceError(
                "SmartScreen raw provider event is contradictory"
            )
        _timestamp(event.get("captured_at_utc"), "SmartScreen event time")
        _digest(event.get("payload_sha256"), "SmartScreen raw event payload")
        previous_record_id = record_id
    samples = _array(reputation.get("window_samples"), "SmartScreen window samples")
    if len(samples) < 3:
        raise DesktopEvidenceError("SmartScreen window timeline is incomplete")
    installer_observed = False
    sample_times: list[datetime] = []
    for sequence, raw_sample in enumerate(samples, start=1):
        sample = _exact(
            raw_sample,
            (
                "sequence",
                "captured_at_utc",
                "smartscreen_window_count",
                "installer_process_observed",
            ),
            "SmartScreen window sample",
        )
        sample_time = datetime.strptime(
            _timestamp(sample.get("captured_at_utc"), "SmartScreen sample time"),
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
        if (
            sample.get("sequence") != sequence
            or sample.get("smartscreen_window_count") != 0
            or not isinstance(sample.get("installer_process_observed"), bool)
        ):
            raise DesktopEvidenceError(
                "SmartScreen window timeline observed a warning or is malformed"
            )
        installer_observed = installer_observed or bool(
            sample.get("installer_process_observed")
        )
        sample_times.append(sample_time)
    if (
        sample_times != sorted(sample_times)
        or sample_times[0] < launch_time
        or sample_times[-1] > completed_time
        or not installer_observed
    ):
        raise DesktopEvidenceError(
            "SmartScreen window timeline does not prove an allowed installer launch"
        )
    return {
        "observer": observer,
        "download": download,
        "launch": launch,
        "reputation": reputation,
        "motw_sha256": _sha256(motw_bytes),
        "observation_sha256": _sha256(observation_bytes),
    }


def _validate_webview_state(value: object, label: str) -> dict[str, Any]:
    state = _exact(
        value,
        ("state", "product_guid", "version", "channel", "signer", "scope"),
        label,
    )
    if state["state"] == "present":
        version = state.get("version")
        try:
            version_parts = tuple(int(part) for part in str(version).split("."))
        except ValueError as error:
            raise DesktopEvidenceError(f"{label} version is malformed") from error
        if (
            state.get("product_guid") != "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
            or len(version_parts) != 4
            or version_parts < (120, 0, 2210, 91)
            or state.get("channel") != "evergreen"
            or state.get("scope") not in ("machine", "current-user")
        ):
            raise DesktopEvidenceError(
                f"{label} is not the supported production WebView2 runtime"
            )
        signer = _exact(
            state["signer"],
            ("status", "subject", "certificate_sha256"),
            f"{label} signer",
        )
        if (
            signer["status"] != "Valid"
            or signer["subject"] != "CN=Microsoft Corporation"
        ):
            raise DesktopEvidenceError(
                f"{label} is not Authenticode-trusted Microsoft WebView2"
            )
        _digest(signer["certificate_sha256"], f"{label} signer certificate")
    elif state["state"] == "absent":
        if any(
            state[field] is not None
            for field in ("product_guid", "version", "channel", "signer", "scope")
        ):
            raise DesktopEvidenceError(
                f"{label} absent state contains fabricated runtime details"
            )
    else:
        raise DesktopEvidenceError(f"{label} state is unsupported")
    return state


def _validate_window(value: object) -> dict[str, Any]:
    window = _exact(
        value,
        (
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
        ),
        "window observation",
    )
    samples = _array(window["external_browser_observations"], "browser observations")
    if len(samples) < 2:
        raise DesktopEvidenceError("browser observations lack baseline/final samples")
    sample_times: list[datetime] = []
    sample_phases: list[str] = []
    sample_identities: list[list[tuple[str, int, int]]] = []
    for raw_sample in samples:
        sample = _exact(
            raw_sample, ("captured_at_utc", "phase", "windows"), "browser observation"
        )
        timestamp = _timestamp(
            sample["captured_at_utc"], "browser observation timestamp"
        )
        sample_times.append(
            datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        )
        phase = sample.get("phase")
        if phase not in ("baseline", "installer", "app-readiness", "stable", "final"):
            raise DesktopEvidenceError("browser observation phase is unsupported")
        sample_phases.append(phase)
        identities: list[tuple[str, int, int]] = []
        for raw_identity in _array(sample["windows"], "browser identities"):
            identity = _exact(
                raw_identity,
                ("process_name", "process_id", "window_handle"),
                "browser identity",
            )
            process_name = _text(
                identity["process_name"], "browser process", maximum=64
            )
            if process_name not in ("chrome", "msedge", "firefox", "brave"):
                raise DesktopEvidenceError("browser process identity is unsupported")
            identities.append(
                (
                    process_name,
                    _integer(identity["process_id"], "browser process id", minimum=1),
                    _integer(
                        identity["window_handle"],
                        "browser window handle",
                        minimum=1,
                    ),
                )
            )
        if (
            identities != sorted(identities)
            or len(identities) != len(set(identities))
            or len({identity[2] for identity in identities}) != len(identities)
        ):
            raise DesktopEvidenceError(
                "browser identities are not sorted and unique raw observations"
            )
        sample_identities.append(identities)
    if (
        sample_phases[0] != "baseline"
        or sample_phases[-1] != "final"
        or sample_phases.count("baseline") != 1
        or sample_phases.count("final") != 1
        or sample_times != sorted(sample_times)
    ):
        raise DesktopEvidenceError(
            "browser observation baseline/final timeline is not monotonic"
        )
    baseline_identities = sample_identities[0]
    if baseline_identities:
        raise DesktopEvidenceError(
            "clean snapshot browser baseline must be empty to exclude same-HWND tabs"
        )
    if any(identities != baseline_identities for identities in sample_identities[1:]):
        raise DesktopEvidenceError(
            "external browser baseline/final inventory changed during the journey"
        )

    raw_events = _array(
        window["external_browser_window_events"], "browser window events"
    )
    events: list[dict[str, Any]] = []
    event_times: list[datetime] = []
    event_lines: list[str] = []
    for expected_sequence, raw_event in enumerate(raw_events, start=1):
        event = _exact(
            raw_event,
            (
                "sequence",
                "captured_at_utc",
                "event",
                "process_name",
                "process_id",
                "window_handle",
            ),
            "browser window event",
        )
        if (
            _integer(event["sequence"], "browser event sequence", minimum=1)
            != expected_sequence
        ):
            raise DesktopEvidenceError("browser event sequence is not contiguous")
        event_timestamp = _timestamp(
            event["captured_at_utc"], "browser event timestamp"
        )
        event_times.append(
            datetime.strptime(event_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        )
        if event.get("event") not in ("create", "show", "hide", "destroy"):
            raise DesktopEvidenceError("browser lifecycle event is unsupported")
        event_process = _text(
            event.get("process_name"), "browser event process", maximum=64
        )
        event_pid = _integer(
            event.get("process_id"), "browser event process id", minimum=1
        )
        event_hwnd = _integer(
            event.get("window_handle"), "browser event window handle", minimum=1
        )
        if (event_process, event_pid, event_hwnd) not in set(baseline_identities):
            raise DesktopEvidenceError(
                "non-baseline external browser HWND emitted a lifecycle event"
            )
        event_lines.append(
            f"{expected_sequence}|{event_timestamp}|{event['event']}|"
            f"{event_process}|{event_pid}|{event_hwnd}"
        )
        events.append(event)
    if event_times != sorted(event_times):
        raise DesktopEvidenceError("browser lifecycle event times are not monotonic")
    observer = _exact(
        window["external_browser_observer"],
        (
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
        ),
        "browser window observer",
    )
    if (
        observer["schema"] != "stock-desk-browser-window-observer-v1"
        or observer["api"] != "Win32 EnumWindows + SetWinEventHook"
        or observer["subscribed_events"] != ["create", "show", "hide", "destroy"]
    ):
        raise DesktopEvidenceError("browser window observer contract is unsupported")
    hook_time = datetime.strptime(
        _timestamp(observer["hook_started_at_utc"], "browser hook start"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    baseline_time = datetime.strptime(
        _timestamp(observer["baseline_captured_at_utc"], "browser baseline time"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    final_time = datetime.strptime(
        _timestamp(observer["final_captured_at_utc"], "browser final time"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    stopped_time = datetime.strptime(
        _timestamp(observer["hook_stopped_at_utc"], "browser hook stop"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    baseline_sequence = _integer(
        observer["baseline_event_sequence"], "browser baseline event sequence"
    )
    final_sequence = _integer(
        observer["final_event_sequence"], "browser final event sequence"
    )
    if not (
        hook_time <= baseline_time == sample_times[0]
        and baseline_time <= final_time == sample_times[-1] <= stopped_time
        and baseline_sequence <= final_sequence <= len(events)
    ):
        raise DesktopEvidenceError(
            "browser observer boundaries contradict the raw sample/event timeline"
        )
    for index, event_time in enumerate(event_times, start=1):
        if not hook_time <= event_time <= stopped_time:
            raise DesktopEvidenceError("browser event escaped the observer lifecycle")
        if index <= baseline_sequence and event_time > baseline_time:
            raise DesktopEvidenceError(
                "pre-baseline browser event has a later timestamp"
            )
        if baseline_sequence < index <= final_sequence and not (
            baseline_time <= event_time <= final_time
        ):
            raise DesktopEvidenceError(
                "journey browser event escaped capture boundaries"
            )
        if index > final_sequence and event_time < final_time:
            raise DesktopEvidenceError(
                "post-final browser event has an earlier timestamp"
            )
        if index > baseline_sequence and events[index - 1]["event"] in (
            "create",
            "hide",
            "destroy",
        ):
            raise DesktopEvidenceError(
                "baseline external browser HWND changed lifecycle during the journey"
            )
    lifecycle_event_count = _integer(
        observer["lifecycle_event_count"], "browser lifecycle event count"
    )
    if lifecycle_event_count != len(events):
        raise DesktopEvidenceError("browser lifecycle event count is not raw-derived")
    expected_event_digest = _sha256("\n".join(event_lines).encode())
    if (
        _digest(observer["lifecycle_events_sha256"], "browser lifecycle digest")
        != expected_event_digest
    ):
        raise DesktopEvidenceError("browser lifecycle event digest is not raw-derived")
    if (
        _integer(
            window.get("external_browser_window_count"), "external browser window count"
        )
        != 0
    ):
        raise DesktopEvidenceError(
            "external browser count contradicts raw observations"
        )
    return window


def _validate_lifecycle(
    receipt_bytes: bytes,
    signature_bytes: bytes,
    *,
    manifest: Mapping[str, Any],
    assignment: Mapping[str, Any],
    expected_policy_sha256: str,
    expected_adapter_sha256: str,
    expected_controller_request_sha256: str,
    expected_guest_harness_sha256: str,
    expected_uia_driver_sha256: str,
    expected_workflow_sha256: str,
    broker_public_key: Path,
    expected_broker_public_key_sha256: str,
    expected_repository: str,
    expected_workflow_ref: str,
    expected_run_id: int,
    expected_run_attempt: int,
) -> dict[str, Any]:
    public_key_bytes = broker_public_key.read_bytes()
    if _sha256(public_key_bytes) != expected_broker_public_key_sha256:
        raise DesktopEvidenceError(
            "broker public key differs from the reviewed exact-source key"
        )
    try:
        loaded_key = serialization.load_pem_public_key(public_key_bytes)
    except (ValueError, TypeError) as error:
        raise DesktopEvidenceError("broker public key is unreadable") from error
    if not isinstance(loaded_key, Ed25519PublicKey):
        raise DesktopEvidenceError("broker receipt key must be Ed25519")
    try:
        loaded_key.verify(signature_bytes, receipt_bytes)
    except InvalidSignature as error:
        raise DesktopEvidenceError(
            "broker lifecycle receipt signature is invalid"
        ) from error
    try:
        receipt = _exact(
            json.loads(receipt_bytes),
            (
                "schema",
                "status",
                "raw_only",
                "case_id",
                "source_sha",
                "snapshot_policy_sha256",
                "adapter_sha256",
                "broker_public_key_sha256",
                "controller_request_sha256",
                "guest_harness_sha256",
                "uia_driver_sha256",
                "workflow_sha256",
                "snapshot_sha256",
                "image_sha256",
                "raw_manifest_sha256",
                "force_kill",
                "restored_before_at_utc",
                "acceptance_completed_at_utc",
                "cleanup_restored_at_utc",
                "lease_expires_at_utc",
                "lease_released_at_utc",
                "watchdog_armed_during_run",
                "lease_state",
                "lease_digest",
                "broker_request_nonce_sha256",
                "request_job_id",
                "oidc_jti_sha256",
                "oidc",
            ),
            "broker lifecycle receipt",
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopEvidenceError("broker lifecycle receipt is unreadable") from error
    if (
        receipt.get("schema") != "stock-desk-windows-vm-lifecycle-receipt-v2"
        or receipt.get("status") != "completed"
        or receipt.get("raw_only") is not True
        or receipt.get("case_id") != manifest["case_id"]
        or receipt.get("source_sha") != manifest["identity"]["source_sha"]
        or receipt.get("snapshot_policy_sha256") != expected_policy_sha256
        or receipt.get("adapter_sha256") != expected_adapter_sha256
        or receipt.get("broker_public_key_sha256") != expected_broker_public_key_sha256
        or receipt.get("controller_request_sha256")
        != expected_controller_request_sha256
        or receipt.get("guest_harness_sha256") != expected_guest_harness_sha256
        or receipt.get("uia_driver_sha256") != expected_uia_driver_sha256
        or receipt.get("workflow_sha256") != expected_workflow_sha256
        or receipt.get("snapshot_sha256") != assignment["snapshot_sha256"]
        or receipt.get("image_sha256") != assignment["image_sha256"]
        or receipt.get("raw_manifest_sha256") != manifest["_raw_sha256"]
        or receipt.get("request_job_id") != manifest["execution"]["job_id"]
        or receipt.get("force_kill") is not False
    ):
        raise DesktopEvidenceError(
            "broker lifecycle receipt is not bound to the raw package"
        )
    lifecycle_fields = (
        "restored_before_at_utc",
        "acceptance_completed_at_utc",
        "cleanup_restored_at_utc",
        "lease_released_at_utc",
        "lease_expires_at_utc",
    )
    for field in lifecycle_fields:
        _timestamp(receipt.get(field), f"lifecycle {field}")
    lifecycle_times = [
        datetime.strptime(str(receipt[field]), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        for field in lifecycle_fields
    ]
    if lifecycle_times != sorted(lifecycle_times):
        raise DesktopEvidenceError("broker lifecycle timestamps are not monotonic")
    if (lifecycle_times[-1] - lifecycle_times[0]).total_seconds() > 3600:
        raise DesktopEvidenceError("broker lifecycle exceeded its one-hour lease")
    capture = _object(manifest.get("capture"), "raw capture lifecycle")
    capture_started = datetime.strptime(
        _timestamp(capture.get("started_at_utc"), "capture start"), "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    capture_completed = datetime.strptime(
        _timestamp(capture.get("completed_at_utc"), "capture completion"),
        "%Y-%m-%dT%H:%M:%SZ",
    ).replace(tzinfo=timezone.utc)
    if not (
        lifecycle_times[0]
        <= capture_started
        <= capture_completed
        <= lifecycle_times[1]
        <= lifecycle_times[2]
    ):
        raise DesktopEvidenceError(
            "raw capture timestamps escape the signed broker lifecycle"
        )
    if (
        receipt.get("watchdog_armed_during_run") is not True
        or receipt.get("lease_state") != "released-after-restore"
    ):
        raise DesktopEvidenceError(
            "broker did not restore and release its bounded watchdog lease"
        )
    _digest(receipt.get("lease_digest"), "lease digest")
    _digest(receipt.get("broker_request_nonce_sha256"), "broker request nonce")
    _digest(receipt.get("oidc_jti_sha256"), "broker OIDC JTI")
    oidc = _exact(
        receipt.get("oidc"),
        (
            "issuer",
            "audience",
            "repository",
            "repository_id",
            "repository_owner_id",
            "ref",
            "sha",
            "workflow_ref",
            "workflow_sha",
            "run_id",
            "run_attempt",
            "check_run_id",
            "runner_environment",
            "environment",
            "sub",
        ),
        "broker OIDC identity",
    )
    if (
        oidc.get("issuer") != "https://token.actions.githubusercontent.com"
        or oidc.get("audience") != BROKER_AUDIENCE
        or oidc.get("repository") != expected_repository
        or re.fullmatch(r"[1-9][0-9]*", str(oidc.get("repository_id"))) is None
        or not isinstance(oidc.get("repository_id"), str)
        or re.fullmatch(r"[1-9][0-9]*", str(oidc.get("repository_owner_id"))) is None
        or not isinstance(oidc.get("repository_owner_id"), str)
        or oidc.get("ref") != "refs/heads/main"
        or oidc.get("sha") != manifest["identity"]["source_sha"]
        or oidc.get("workflow_ref") != expected_workflow_ref
        or oidc.get("workflow_sha") != manifest["identity"]["source_sha"]
        or oidc.get("run_id") != str(expected_run_id)
        or oidc.get("run_attempt") != str(expected_run_attempt)
        or re.fullmatch(r"[1-9][0-9]*", str(oidc.get("check_run_id"))) is None
        or not isinstance(oidc.get("check_run_id"), str)
        or oidc.get("runner_environment") != "github-hosted"
        or oidc.get("environment") != "windows-installed-acceptance"
        or oidc.get("sub")
        != f"repo:{expected_repository}:environment:windows-installed-acceptance"
    ):
        raise DesktopEvidenceError(
            "external VM broker OIDC identity is not exact protected main"
        )
    return receipt


def verify_package(
    package: Path,
    *,
    assignment: Mapping[str, Any],
    expected_source_sha: str,
    expected_source_tree: str,
    expected_main_proof_sha256: str,
    expected_candidate_sha256: str,
    expected_desktop_host_sha256: str,
    expected_sidecar_sha256: str,
    expected_signer_subject: str,
    expected_certificate_thumbprint: str,
    expected_timestamp_subject: str,
    expected_webview_installer_sha256: str,
    expected_policy_sha256: str,
    expected_adapter_sha256: str,
    expected_controller_request_sha256: str,
    expected_guest_harness_sha256: str,
    expected_uia_driver_sha256: str,
    broker_public_key: Path,
    expected_broker_public_key_sha256: str,
    expected_repository: str,
    expected_workflow: str,
    expected_workflow_ref: str,
    expected_workflow_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
) -> dict[str, Any]:
    manifest_path = package / "raw-manifest.json"
    manifest_bytes = manifest_path.read_bytes() if manifest_path.is_file() else b""
    manifest = _exact(
        _read_json(manifest_path, "raw manifest"),
        (
            "schema_version",
            "artifact",
            "case_id",
            "scenario",
            "identity",
            "execution",
            "capture",
            "records",
        ),
        "raw manifest",
    )
    manifest["_raw_sha256"] = _sha256(manifest_bytes)
    if '"passed"' in manifest_bytes.decode("utf-8", errors="ignore"):
        raise DesktopEvidenceError("raw guest evidence cannot declare passed")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("artifact") != "windows-installed-raw-evidence"
    ):
        raise DesktopEvidenceError("raw manifest schema identity is unsupported")
    case_id = _text(manifest.get("case_id"), "raw case id", maximum=64)
    if (
        case_id != assignment["case_id"]
        or manifest.get("scenario") != assignment["scenario"]
    ):
        raise DesktopEvidenceError("raw manifest does not match its policy assignment")
    identity = _object(manifest.get("identity"), "raw identity")
    expected_components = {
        "desktop-host": expected_desktop_host_sha256,
        "sidecar": expected_sidecar_sha256,
        "nsis-installer": expected_candidate_sha256,
    }
    expected_authenticode = {
        "signer_subject": expected_signer_subject,
        "certificate_thumbprint": expected_certificate_thumbprint,
        "timestamp_subject": expected_timestamp_subject,
    }
    expected_identity = {
        "source_sha": expected_source_sha,
        "source_tree": expected_source_tree,
        "main_proof_sha256": expected_main_proof_sha256,
        "candidate_sha256": expected_candidate_sha256,
        "webview_installer_sha256": expected_webview_installer_sha256,
        "signed_components": expected_components,
        "authenticode_expectation": expected_authenticode,
    }
    if identity != expected_identity:
        raise DesktopEvidenceError("raw package immutable identity mismatch")
    _git(identity["source_sha"], "source SHA")
    _git(identity["source_tree"], "source tree")
    for field in ("main_proof_sha256", "candidate_sha256", "webview_installer_sha256"):
        _digest(identity[field], field)
    for role, digest in expected_components.items():
        _digest(digest, f"{role} expected signed digest")
    _text(expected_signer_subject, "expected signer subject")
    _text(expected_timestamp_subject, "expected timestamp subject")
    if CERTIFICATE_THUMBPRINT_RE.fullmatch(expected_certificate_thumbprint) is None:
        raise DesktopEvidenceError("expected signing certificate thumbprint is invalid")
    execution = _exact(
        manifest.get("execution"),
        (
            "repository",
            "workflow",
            "workflow_ref",
            "workflow_sha",
            "workflow_path",
            "workflow_sha256",
            "run_id",
            "run_attempt",
            "job_id",
            "job_name",
            "matrix_case_id",
            "matrix_guest_profile",
            "matrix_scenario",
            "matrix_dpi_percent",
            "matrix_controller_label",
            "scenario_attempt",
            "attempt_id",
        ),
        "raw execution",
    )
    if (
        execution.get("repository") != expected_repository
        or execution.get("workflow") != expected_workflow
        or execution.get("workflow_ref") != expected_workflow_ref
        or execution.get("workflow_sha") != expected_source_sha
        or execution.get("workflow_path") != WORKFLOW_PATH
        or execution.get("workflow_sha256") != expected_workflow_sha256
        or execution.get("run_id") != expected_run_id
        or execution.get("run_attempt") != expected_run_attempt
        or execution.get("scenario_attempt") != 1
        or execution.get("matrix_case_id") != case_id
        or execution.get("matrix_guest_profile") != assignment["guest_profile"]
        or execution.get("matrix_scenario") != assignment["scenario"]
        or execution.get("matrix_dpi_percent") != assignment["dpi_percent"]
        or execution.get("matrix_controller_label") != assignment["controller_label"]
        or execution.get("job_id") != f"windows-installed-{case_id}"
    ):
        raise DesktopEvidenceError("raw package Actions execution identity mismatch")
    if (
        execution.get("attempt_id")
        != f"{assignment['scenario']}-first-{expected_run_id}"
    ):
        raise DesktopEvidenceError(
            "retry evidence cannot replace first-attempt evidence"
        )
    capture = _exact(
        manifest.get("capture"),
        (
            "started_at_utc",
            "completed_at_utc",
            "guest_profile",
            "controller_label",
            "dpi_percent",
            "guest_harness_sha256",
            "uia_driver_sha256",
            "controller_request_sha256",
            "snapshot_policy_sha256",
            "clean_snapshot_sha256",
            "image_sha256",
            "webview_product_guid",
            "minimum_webview_version",
            "failure_injection",
            "data_path",
            "redaction_version",
        ),
        "raw capture",
    )
    if (
        capture.get("guest_profile") != assignment["guest_profile"]
        or capture.get("controller_label") != assignment["controller_label"]
        or capture.get("dpi_percent") != assignment["dpi_percent"]
        or capture.get("snapshot_policy_sha256") != expected_policy_sha256
        or capture.get("clean_snapshot_sha256") != assignment["snapshot_sha256"]
        or capture.get("image_sha256") != assignment["image_sha256"]
        or capture.get("data_path") != assignment["data_path"]
        or capture.get("failure_injection") != assignment["failure_injection"]
        or capture.get("redaction_version") != "stock-desk-public-redaction-v2"
        or capture.get("controller_request_sha256")
        != expected_controller_request_sha256
        or capture.get("guest_harness_sha256") != expected_guest_harness_sha256
        or capture.get("uia_driver_sha256") != expected_uia_driver_sha256
        or capture.get("webview_product_guid")
        != "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        or capture.get("minimum_webview_version") != "120.0.2210.91"
    ):
        raise DesktopEvidenceError(
            "raw capture is not bound to its protected assignment"
        )
    driver_sha256 = _digest(capture.get("uia_driver_sha256"), "UIA driver digest")
    _digest(capture.get("guest_harness_sha256"), "guest harness digest")
    _digest(capture.get("controller_request_sha256"), "controller request digest")
    _timestamp(capture.get("started_at_utc"), "capture start")
    _timestamp(capture.get("completed_at_utc"), "capture completion")

    records, inventory_digest = _records(package, manifest)
    expected_record_roles = (
        {
            "observation-stream",
            "install-log",
            "failure-diagnostic",
            "smartscreen-observation",
            "motw-zone-identifier",
        }
        if assignment["scenario"] == "webview-install-failure"
        else {
            "observation-stream",
            "install-log",
            "smartscreen-observation",
            "motw-zone-identifier",
            "uia-action-trace",
            "uia-tree",
            "focus-region-contact-sheet",
            "window-capture-standard",
            "window-capture-narrow",
        }
    )
    if set(records) != expected_record_roles:
        raise DesktopEvidenceError(
            "raw record role inventory is incomplete or expanded"
        )
    smartscreen = _validate_smartscreen_raw_records(
        records["smartscreen-observation"],
        records["motw-zone-identifier"],
        assignment=assignment,
        expected_candidate_sha256=expected_candidate_sha256,
        expected_adapter_sha256=expected_adapter_sha256,
    )
    if assignment["scenario"] == "installed-first-use":
        _validate_png_capture(
            records["window-capture-standard"],
            logical_size=(1366, 768),
            dpi_percent=assignment["dpi_percent"],
        )
        _validate_png_capture(
            records["window-capture-narrow"],
            logical_size=(640, 360),
            dpi_percent=assignment["dpi_percent"],
        )
    expected_files = {
        "raw-manifest.json",
        "controller/lifecycle-receipt.json",
        "controller/lifecycle-receipt.sig",
        "controller/snapshot-policy.json",
        *(
            str(record["path"])
            for record in _array(manifest.get("records"), "raw records")
        ),
    }
    actual_files: set[str] = set()
    for path in package.rglob("*"):
        if path.is_symlink():
            raise DesktopEvidenceError(
                "raw package contains a symlink or reparse-like entry"
            )
        if path.is_file():
            actual_files.add(path.relative_to(package).as_posix())
    if actual_files != expected_files:
        raise DesktopEvidenceError(
            "raw package file set is not closed by its manifest contract"
        )
    packaged_policy = package / "controller" / "snapshot-policy.json"
    if _sha256(packaged_policy.read_bytes()) != expected_policy_sha256:
        raise DesktopEvidenceError(
            "raw package snapshot policy differs from the approved bytes"
        )
    public_text = b"\n".join(
        data for kind, data in records.items() if not kind.startswith("window-capture-")
    ).decode("utf-8", errors="ignore")
    if PUBLIC_TEXT_LEAK_RE.search(public_text):
        raise DesktopEvidenceError(
            "raw public bytes contain a secret or user-profile path"
        )
    events = _parse_events(records["observation-stream"])
    expected_kinds = (
        FAILURE_EVENT_KINDS
        if assignment["scenario"] == "webview-install-failure"
        else SUCCESS_EVENT_KINDS
    )
    if set(events) != expected_kinds:
        raise DesktopEvidenceError(
            "raw event inventory is incomplete or contains unreviewed observations"
        )
    system = _value(events, "system")
    if system != {**assignment["system"], "image_sha256": assignment["image_sha256"]}:
        raise DesktopEvidenceError(
            "observed operating system differs from the protected image"
        )
    account = _validate_account(_value(events, "account-token"))
    hardware = _validate_hardware(_value(events, "hardware-observation"))
    uac = _exact(
        _value(events, "uac-observation"),
        ("uac_prompt_count", "elevation_requested"),
        "UAC observation",
    )
    if uac.get("uac_prompt_count") != 0 or uac.get("elevation_requested") is not False:
        raise DesktopEvidenceError("installed journey requested elevation or UAC")
    installer = _validate_process(
        _value(events, "installer-process-token"),
        "installer process",
        expected_started=True,
    )
    authenticode = _validate_stock_desk_authenticode(
        _value(events, "stock-desk-authenticode"),
        expected_components=expected_components,
        expected_signer_subject=expected_signer_subject,
        expected_certificate_thumbprint=expected_certificate_thumbprint,
        expected_timestamp_subject=expected_timestamp_subject,
        include_installed_components=assignment["scenario"] == "installed-first-use",
    )
    before = _validate_webview_state(
        _value(events, "webview-before"), "WebView2 before"
    )
    installation = _exact(
        _value(events, "webview-installation"),
        ("attempted", "exit_code", "installer_sha256", "fault_injection"),
        "WebView2 installation",
    )
    webview_child = _exact(
        _value(events, "webview-child-process-token"),
        (
            "observed",
            "executable_name",
            "executable_path_sha256",
            "executable_sha256",
            "signer",
            "elevated",
            "integrity_level",
            "integrity_rid",
            "exit_code",
        ),
        "WebView2 child process",
    )
    after = _validate_webview_state(_value(events, "webview-after"), "WebView2 after")
    install = _exact(
        _value(events, "install-observation"),
        ("exit_code", "application_files_present", "shortcut_present", "launchable"),
        "install observation",
    )
    filesystem = _exact(
        _value(events, "filesystem-observation"),
        (
            "install_root_read_only",
            "install_root_runtime_write_count",
            "mutable_root_identity",
            "unexpected_mutable_root_write_count",
            "legacy_v1_open_count",
            "legacy_v1_write_count",
        ),
        "filesystem observation",
    )
    window = _validate_window(_value(events, "window-observation"))
    canary_before = _exact(
        _value(events, "v1-canary-before"),
        ("entry_count", "content_sha256"),
        "v1 canary before",
    )
    canary_after = _exact(
        _value(events, "v1-canary-after"),
        ("entry_count", "content_sha256"),
        "v1 canary after",
    )
    redaction = _exact(
        _value(events, "redaction-scan"),
        ("secret_match_count", "username_match_count", "absolute_path_match_count"),
        "redaction scan",
    )
    uninstall = _exact(
        _value(events, "uninstall-observation"),
        ("attempted", "exit_code", "application_files_removed", "shortcuts_removed"),
        "uninstall observation",
    )
    if any(
        redaction.get(field) != 0
        for field in (
            "secret_match_count",
            "username_match_count",
            "absolute_path_match_count",
        )
    ):
        raise DesktopEvidenceError("in-guest redaction scan did not pass")
    if canary_before != canary_after:
        raise DesktopEvidenceError("legacy v1 data canary changed")
    if webview_child.get("observed") is True:
        child_signer = _exact(
            webview_child.get("signer"),
            ("status", "subject", "certificate_sha256"),
            "WebView2 child signer",
        )
        if (
            child_signer["status"] != "Valid"
            or child_signer["subject"] != "CN=Microsoft Corporation"
        ):
            raise DesktopEvidenceError("WebView2 child is not signed by Microsoft")
        _digest(child_signer["certificate_sha256"], "WebView2 child signer certificate")
        _digest(webview_child.get("executable_path_sha256"), "WebView2 child path")
    if (
        filesystem.get("install_root_read_only") is not True
        or filesystem.get("install_root_runtime_write_count") != 0
        or filesystem.get("mutable_root_identity") != "localappdata-stock-desk-v1.1"
        or filesystem.get("unexpected_mutable_root_write_count") != 0
        or filesystem.get("legacy_v1_open_count") != 0
        or filesystem.get("legacy_v1_write_count") != 0
    ):
        raise DesktopEvidenceError(
            "runtime writes escaped v1.1 AppData or touched the install/v1 roots"
        )

    success = assignment["scenario"] == "installed-first-use"
    network = _validate_network(
        _value(events, "network-observation"), assignment=assignment, success=success
    )
    display: dict[str, Any] | None = None
    journey: dict[str, Any] | None = None
    uia: dict[str, Any] | None = None
    if success:
        if (
            before.get("state") != assignment["webview_initial_state"]
            or after.get("state") != "present"
            or install.get("exit_code") != 0
            or install.get("application_files_present") is not True
            or install.get("shortcut_present") is not True
            or install.get("launchable") is not True
            or window.get("observed") is not True
            or window.get("main_window_count") != 1
            or window.get("external_browser_window_count") != 0
        ):
            raise DesktopEvidenceError(
                "successful installed journey contradicts its install/window state"
            )
        expected_attempt = assignment["webview_initial_state"] == "absent"
        if installation.get("attempted") is not expected_attempt or (
            expected_attempt and installation.get("exit_code") != 0
        ):
            raise DesktopEvidenceError(
                "WebView2 install behavior contradicts the clean snapshot"
            )
        if expected_attempt:
            if (
                webview_child.get("observed") is not True
                or webview_child.get("executable_name")
                != "MicrosoftEdgeWebView2RuntimeInstaller.exe"
                or webview_child.get("executable_sha256")
                != expected_webview_installer_sha256
                or webview_child.get("exit_code") != 0
                or webview_child.get("elevated") is not False
                or webview_child.get("integrity_level") != "medium"
                or child_signer.get("status") != "Valid"
                or child_signer.get("subject") != "CN=Microsoft Corporation"
            ):
                raise DesktopEvidenceError(
                    "WebView2 child process is not the proved medium-integrity installer"
                )
        elif webview_child.get("observed") is not False:
            raise DesktopEvidenceError(
                "preinstalled WebView2 case unexpectedly launched an installer"
            )
        desktop = _validate_process(
            _value(events, "desktop-host-process-token"),
            "desktop host",
            expected_started=True,
        )
        sidecar = _validate_process(
            _value(events, "sidecar-process-token"), "sidecar", expected_started=True
        )
        uninstaller = _validate_process(
            _value(events, "uninstaller-process-token"),
            "uninstaller",
            expected_started=True,
        )
        display = _validate_display(
            _value(events, "display-observation"), dpi_percent=assignment["dpi_percent"]
        )
        journey = _validate_journey(
            _value(events, "first-use-journey"), data_path=assignment["data_path"]
        )
        successful_provider_records = {
            record.get("operation"): record
            for record in _array(network.get("records"), "network records")
            if record.get("provider") == journey["source"]["provider"]
            and record.get("outcome") == "success"
            and record.get("operation") in ("catalog", "daily-bars")
        }
        if set(successful_provider_records) != {"catalog", "daily-bars"}:
            raise DesktopEvidenceError(
                "journey provider is not bound to one catalog and bars segment"
            )
        if (
            successful_provider_records["catalog"].get("payload_sha256")
            != journey["source"].get("catalog_sha256")
            or successful_provider_records["daily-bars"].get("payload_sha256")
            != journey["source"].get("bars_sha256")
            or successful_provider_records["daily-bars"].get("cutoff_utc")
            != journey["source"].get("cutoff_utc")
            or successful_provider_records["daily-bars"].get("row_count")
            != journey["source"].get("row_count")
        ):
            raise DesktopEvidenceError(
                "visible source/cutoff/row count differs from captured provider bytes"
            )
        uia = _validate_uia(
            _value(events, "uia-matrix"), expected_driver_sha256=driver_sha256
        )
        _validate_uia_raw_records(
            records["uia-action-trace"],
            records["uia-tree"],
            records["focus-region-contact-sheet"],
            uia=uia,
            expected_primary_actions=journey["primary_click_count"],
        )
        if (
            uninstall.get("attempted") is not True
            or uninstall.get("exit_code") != 0
            or uninstall.get("application_files_removed") is not True
            or uninstall.get("shortcuts_removed") is not True
        ):
            raise DesktopEvidenceError(
                "standard-user uninstall did not remove application files and shortcuts"
            )
        processes = {
            "installer": installer,
            "desktop_host": desktop,
            "sidecar": sidecar,
            "uninstaller": uninstaller,
        }
    else:
        if (
            before.get("state") != "absent"
            or installation.get("attempted") is not True
            or not isinstance(installation.get("exit_code"), int)
            or installation.get("exit_code") == 0
            or install.get("exit_code") == 0
            or install.get("launchable") is not False
            or window.get("observed") is not False
            or window.get("main_window_count") != 0
            or window.get("external_browser_window_count") != 0
            or uninstall.get("attempted") is not False
        ):
            raise DesktopEvidenceError(
                "fixed WebView2 offline failure did not fail closed"
            )
        if (
            webview_child.get("observed") is not True
            or webview_child.get("executable_sha256")
            != expected_webview_installer_sha256
            or webview_child.get("exit_code") == 0
            or webview_child.get("elevated") is not False
            or webview_child.get("integrity_level") != "medium"
        ):
            raise DesktopEvidenceError(
                "offline failure is not bound to the proved WebView2 child"
            )
        if (
            "failure-diagnostic" not in records
            or "uia-action-trace" in records
            or any(kind.startswith("window-capture-") for kind in records)
        ):
            raise DesktopEvidenceError(
                "failure package contains a UI shim or lacks its fixed diagnostic"
            )
        _validate_process(
            _value(events, "desktop-host-process-token"),
            "desktop host",
            expected_started=False,
        )
        _validate_process(
            _value(events, "sidecar-process-token"), "sidecar", expected_started=False
        )
        _validate_process(
            _value(events, "uninstaller-process-token"),
            "uninstaller",
            expected_started=False,
        )
        processes = {"installer": installer}

    lifecycle_path = package / "controller" / "lifecycle-receipt.json"
    signature_path = package / "controller" / "lifecycle-receipt.sig"
    if (
        not lifecycle_path.is_file()
        or lifecycle_path.is_symlink()
        or not signature_path.is_file()
        or signature_path.is_symlink()
    ):
        raise DesktopEvidenceError(
            "signed broker lifecycle receipt is missing or unsafe"
        )
    lifecycle_bytes = lifecycle_path.read_bytes()
    signature_bytes = signature_path.read_bytes()
    if (
        not lifecycle_bytes
        or len(lifecycle_bytes) > 1024 * 1024
        or len(signature_bytes) != 64
    ):
        raise DesktopEvidenceError(
            "signed broker lifecycle receipt has an invalid size"
        )
    _validate_lifecycle(
        lifecycle_bytes,
        signature_bytes,
        manifest=manifest,
        assignment=assignment,
        expected_policy_sha256=expected_policy_sha256,
        expected_adapter_sha256=expected_adapter_sha256,
        expected_controller_request_sha256=expected_controller_request_sha256,
        expected_guest_harness_sha256=expected_guest_harness_sha256,
        expected_uia_driver_sha256=expected_uia_driver_sha256,
        expected_workflow_sha256=expected_workflow_sha256,
        broker_public_key=broker_public_key,
        expected_broker_public_key_sha256=expected_broker_public_key_sha256,
        expected_repository=expected_repository,
        expected_workflow_ref=expected_workflow_ref,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt,
    )
    package_digest = _canonical_digest(
        {
            "raw_manifest_sha256": _sha256(manifest_bytes),
            "record_inventory_sha256": inventory_digest,
            "lifecycle_receipt_sha256": _sha256(lifecycle_bytes),
            "lifecycle_signature_sha256": _sha256(signature_bytes),
        }
    )
    return {
        "schema_version": 2,
        "artifact": "windows-installed-evidence",
        "case_id": case_id,
        "scenario": assignment["scenario"],
        "identity": identity,
        "execution": execution,
        "system": system,
        "hardware": hardware,
        "account": account,
        "display": display,
        "webview": {"before": before, "installation": installation, "after": after},
        "network": network,
        "security": {
            "uac": uac,
            "processes": processes,
            "authenticode": authenticode,
            "smartscreen": smartscreen,
        },
        "install": install,
        "journey": journey,
        "uia": uia,
        "filesystem": filesystem,
        "window": window,
        "v1_canary": {"before": canary_before, "after": canary_after},
        "uninstall": uninstall,
        "raw_package_sha256": package_digest,
    }


def verify_matrix(
    packages: Sequence[Path],
    *,
    policy_path: Path,
    output_root: Path,
    expected_source_sha: str,
    expected_source_tree: str,
    expected_main_proof_sha256: str,
    expected_candidate_sha256: str,
    expected_desktop_host_sha256: str,
    expected_sidecar_sha256: str,
    expected_signer_subject: str,
    expected_certificate_thumbprint: str,
    expected_timestamp_subject: str,
    expected_webview_installer_sha256: str,
    expected_policy_sha256: str,
    expected_adapter_sha256: str,
    expected_controller_request_sha256: str,
    expected_guest_harness_sha256: str,
    expected_uia_driver_sha256: str,
    broker_public_key: Path,
    expected_broker_public_key_sha256: str,
    expected_repository: str,
    expected_workflow: str,
    expected_workflow_ref: str,
    expected_workflow_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
) -> dict[str, Any]:
    policy_bytes = policy_path.read_bytes() if policy_path.is_file() else b""
    if _sha256(policy_bytes) != expected_policy_sha256:
        raise DesktopEvidenceError(
            "snapshot policy bytes do not match the externally approved digest"
        )
    assignments = validate_snapshot_policy(_read_json(policy_path, "snapshot policy"))
    if len(packages) != 11:
        raise DesktopEvidenceError("exactly 11 first-attempt raw packages are required")
    derived: dict[str, dict[str, Any]] = {}
    for package in packages:
        manifest = _read_json(package / "raw-manifest.json", "raw manifest identity")
        case_id = _text(manifest.get("case_id"), "raw case id", maximum=64)
        if case_id in derived or case_id not in assignments:
            raise DesktopEvidenceError("raw matrix case is duplicated or unauthorized")
        derived[case_id] = verify_package(
            package,
            assignment=assignments[case_id],
            expected_source_sha=expected_source_sha,
            expected_source_tree=expected_source_tree,
            expected_main_proof_sha256=expected_main_proof_sha256,
            expected_candidate_sha256=expected_candidate_sha256,
            expected_desktop_host_sha256=expected_desktop_host_sha256,
            expected_sidecar_sha256=expected_sidecar_sha256,
            expected_signer_subject=expected_signer_subject,
            expected_certificate_thumbprint=expected_certificate_thumbprint,
            expected_timestamp_subject=expected_timestamp_subject,
            expected_webview_installer_sha256=expected_webview_installer_sha256,
            expected_policy_sha256=expected_policy_sha256,
            expected_adapter_sha256=expected_adapter_sha256,
            expected_controller_request_sha256=expected_controller_request_sha256,
            expected_guest_harness_sha256=expected_guest_harness_sha256,
            expected_uia_driver_sha256=expected_uia_driver_sha256,
            broker_public_key=broker_public_key,
            expected_broker_public_key_sha256=expected_broker_public_key_sha256,
            expected_repository=expected_repository,
            expected_workflow=expected_workflow,
            expected_workflow_ref=expected_workflow_ref,
            expected_workflow_sha256=expected_workflow_sha256,
            expected_run_id=expected_run_id,
            expected_run_attempt=expected_run_attempt,
        )
    if set(derived) != set(expected_case_ids()):
        raise DesktopEvidenceError(
            "raw package set does not cover the exact 11-case matrix"
        )
    output_root.mkdir(parents=True, exist_ok=False)
    case_receipts = []
    for case_id in expected_case_ids():
        value = derived[case_id]
        data = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode()
            + b"\n"
        )
        path = output_root / f"{case_id}.json"
        path.write_bytes(data)
        case_receipts.append(
            {
                "case_id": case_id,
                "derived_sha256": _sha256(data),
                "raw_package_sha256": value["raw_package_sha256"],
            }
        )
    for profile, filename, platform in (
        ("win10-22h2", "windows-10-trust-receipt.json", "windows_10_22h2_x64"),
        ("win11", "windows-11-trust-receipt.json", "windows_11_x64"),
    ):
        platform_cases = [
            derived[case_id]
            for case_id in expected_case_ids()
            if case_id.startswith(profile)
        ]
        signature_receipts = []
        smartscreen_receipts = []
        timestamp_thumbprints: set[str] = set()
        for value in platform_cases:
            security = _object(value["security"], "derived security")
            authenticode = _object(security["authenticode"], "derived Authenticode")
            smartscreen = _object(security["smartscreen"], "derived SmartScreen")
            artifacts = _object(
                authenticode["artifacts"], "derived Authenticode artifacts"
            )
            timestamp_thumbprints.update(
                str(
                    _object(artifact, "derived signed artifact")["timestamp_thumbprint"]
                )
                for artifact in artifacts.values()
            )
            signature_receipts.append(
                {
                    "case_id": value["case_id"],
                    "roles": sorted(artifacts),
                    "authenticode_sha256": _canonical_digest(authenticode),
                }
            )
            smartscreen_receipts.append(
                {
                    "case_id": value["case_id"],
                    "observation_sha256": smartscreen["observation_sha256"],
                    "motw_sha256": smartscreen["motw_sha256"],
                    "evidence_sha256": _canonical_digest(smartscreen),
                }
            )
        platform_receipt = {
            "schema": "stock-desk-windows-trust-receipt-v1",
            "source_sha": expected_source_sha,
            "payload_sha256": expected_candidate_sha256,
            "verifier": "WinVerifyTrust",
            "authenticode_status": "Valid",
            "standard_user_install": "passed",
            "smartscreen_status": "allowed-no-warning",
            "smartscreen_observer": "external-protected-vm-observer",
            "platform": platform,
            "signed_components": {
                "desktop-host": expected_desktop_host_sha256,
                "sidecar": expected_sidecar_sha256,
                "nsis-installer": expected_candidate_sha256,
            },
            "signer_subject": expected_signer_subject,
            "certificate_thumbprint": expected_certificate_thumbprint,
            "timestamp_subject": expected_timestamp_subject,
            "timestamp_thumbprints": sorted(timestamp_thumbprints),
            "case_receipts": signature_receipts,
            "smartscreen_case_receipts": smartscreen_receipts,
        }
        (output_root / filename).write_bytes(
            json.dumps(
                platform_receipt, ensure_ascii=False, indent=2, sort_keys=True
            ).encode()
            + b"\n"
        )
    receipt = {
        "schema": "stock-desk-windows-installed-acceptance-receipt-v2",
        "artifact": "windows-installed-acceptance-receipt",
        "evidence_kind": "observed-windows-vm",
        "source_sha": expected_source_sha,
        "source_tree": expected_source_tree,
        "main_proof_sha256": expected_main_proof_sha256,
        "candidate_sha256": expected_candidate_sha256,
        "webview_installer_sha256": expected_webview_installer_sha256,
        "snapshot_policy_sha256": expected_policy_sha256,
        "adapter_sha256": expected_adapter_sha256,
        "broker_public_key_sha256": expected_broker_public_key_sha256,
        "repository": expected_repository,
        "workflow": expected_workflow,
        "workflow_ref": expected_workflow_ref,
        "workflow_sha256": expected_workflow_sha256,
        "run_id": expected_run_id,
        "run_attempt": expected_run_attempt,
        "case_receipts": case_receipts,
        "status": "accepted",
    }
    receipt_data = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True).encode()
        + b"\n"
    )
    (output_root / "acceptance-receipt.json").write_bytes(receipt_data)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--main-proof-sha256", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--desktop-host-sha256", required=True)
    parser.add_argument("--sidecar-sha256", required=True)
    parser.add_argument("--signer-subject", required=True)
    parser.add_argument("--certificate-thumbprint", required=True)
    parser.add_argument("--timestamp-subject", required=True)
    parser.add_argument("--webview-installer-sha256", required=True)
    parser.add_argument("--snapshot-policy-sha256", required=True)
    parser.add_argument("--adapter-sha256", required=True)
    parser.add_argument("--controller-request-sha256", required=True)
    parser.add_argument("--guest-harness-sha256", required=True)
    parser.add_argument("--uia-driver-sha256", required=True)
    parser.add_argument("--broker-public-key", type=Path, required=True)
    parser.add_argument("--broker-public-key-sha256", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-sha256", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("packages", type=Path, nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        verify_matrix(
            arguments.packages,
            policy_path=arguments.policy,
            output_root=arguments.output_root,
            expected_source_sha=_git(arguments.source_sha, "expected source SHA"),
            expected_source_tree=_git(arguments.source_tree, "expected source tree"),
            expected_main_proof_sha256=_digest(
                arguments.main_proof_sha256, "expected main proof"
            ),
            expected_candidate_sha256=_digest(
                arguments.candidate_sha256, "expected candidate"
            ),
            expected_desktop_host_sha256=_digest(
                arguments.desktop_host_sha256, "expected desktop host"
            ),
            expected_sidecar_sha256=_digest(
                arguments.sidecar_sha256, "expected sidecar"
            ),
            expected_signer_subject=_text(
                arguments.signer_subject, "expected signer subject"
            ),
            expected_certificate_thumbprint=_text(
                arguments.certificate_thumbprint,
                "expected certificate thumbprint",
                maximum=64,
            ),
            expected_timestamp_subject=_text(
                arguments.timestamp_subject, "expected timestamp subject"
            ),
            expected_webview_installer_sha256=_digest(
                arguments.webview_installer_sha256, "expected WebView2 installer"
            ),
            expected_policy_sha256=_digest(
                arguments.snapshot_policy_sha256, "expected snapshot policy"
            ),
            expected_adapter_sha256=_digest(
                arguments.adapter_sha256, "expected broker adapter"
            ),
            expected_controller_request_sha256=_digest(
                arguments.controller_request_sha256, "expected controller request"
            ),
            expected_guest_harness_sha256=_digest(
                arguments.guest_harness_sha256, "expected guest harness"
            ),
            expected_uia_driver_sha256=_digest(
                arguments.uia_driver_sha256, "expected UIA driver"
            ),
            broker_public_key=arguments.broker_public_key,
            expected_broker_public_key_sha256=_digest(
                arguments.broker_public_key_sha256, "expected broker public key"
            ),
            expected_repository=arguments.repository,
            expected_workflow=arguments.workflow,
            expected_workflow_ref=arguments.workflow_ref,
            expected_workflow_sha256=_digest(
                arguments.workflow_sha256, "expected workflow"
            ),
            expected_run_id=_integer(arguments.run_id, "expected run id", minimum=1),
            expected_run_attempt=_integer(
                arguments.run_attempt, "expected run attempt", minimum=1
            ),
        )
    except (DesktopEvidenceError, OSError) as error:
        print(f"windows desktop raw evidence rejected: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
