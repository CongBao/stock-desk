from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import verify_windows_desktop_raw_evidence as verifier


SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40
MAIN_PROOF_SHA256 = "1" * 64
CANDIDATE_SHA256 = "2" * 64
WEBVIEW_INSTALLER_SHA256 = "3" * 64
ADAPTER_SHA256 = "4" * 64
CONTROLLER_REQUEST_SHA256 = "5" * 64
GUEST_HARNESS_SHA256 = "6" * 64
UIA_DRIVER_SHA256 = "7" * 64
WORKFLOW_SHA256 = "8" * 64
BROKER_KEY_SHA256 = "9" * 64
NETWORK_POLICY_SHA256 = "c" * 64
CATALOG_SHA256 = "d" * 64
BARS_SHA256 = "e" * 64
CUTOFF = "2026-07-14T00:00:00Z"


def _assignment(*, offline: bool) -> dict[str, Any]:
    scenario = "webview-install-failure" if offline else "installed-first-use"
    case_id = "win10-22h2-dpi-100-webview-offline" if offline else "win11-dpi-125"
    return {
        "case_id": case_id,
        "guest_profile": "win10-22h2" if offline else "win11",
        "controller_label": "stock-desk-vm-controller-win10-22h2"
        if offline
        else "stock-desk-vm-controller-win11",
        "scenario": scenario,
        "dpi_percent": 100 if offline else 125,
        "snapshot_id": f"snapshot-{case_id}",
        "snapshot_sha256": "a" * 64,
        "image_sha256": "b" * 64,
        "system": {
            "family": "windows-10" if offline else "windows-11",
            "display_version": "22H2" if offline else "24H2",
            "build_number": 19045 if offline else 26100,
            "update_build_revision": 1000,
            "architecture": "x86_64",
        },
        "webview_initial_state": "absent" if offline else "present",
        "failure_injection": {
            "identity": "stock-desk-webview2-offline-install-failure-v1",
            "sha256": "f" * 64,
        }
        if offline
        else None,
        "data_path": "primary",
        "network": {
            "profile": "webview-offline-fixed" if offline else "normal",
            "policy_sha256": NETWORK_POLICY_SHA256,
            "expected_provider": "none" if offline else "akshare",
        },
        "account": {
            "account_type": "standard",
            "is_admin": False,
            "username_contains_non_ascii": True,
            "profile_path_contains_space": True,
        },
        "logical_window_sizes": [
            {"width": 1366, "height": 768},
            {"width": 640, "height": 360},
        ],
    }


def _process(role: str, *, started: bool) -> dict[str, Any]:
    return {
        "role": role,
        "started": started,
        "elevated": False if started else None,
        "integrity_level": "medium" if started else None,
        "integrity_rid": 8192 if started else None,
    }


def _webview_state(*, present: bool) -> dict[str, Any]:
    return {
        "state": "present" if present else "absent",
        "product_guid": "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" if present else None,
        "version": "120.0.2210.91" if present else None,
        "channel": "evergreen" if present else None,
        "signer": {
            "status": "Valid",
            "subject": "CN=Microsoft Corporation",
            "certificate_sha256": "f" * 64,
        }
        if present
        else None,
        "scope": "current-user" if present else None,
    }


def _network(*, offline: bool) -> dict[str, Any]:
    common = {
        "host": "data.example.cn",
        "started_at_utc": "2026-07-14T00:00:00Z",
        "completed_at_utc": "2026-07-14T00:00:01Z",
        "tls_system_validation": True,
    }
    records: list[dict[str, Any]]
    if offline:
        records = [
            {
                **common,
                "provider": "webview2",
                "operation": "webview-runtime",
                "outcome": "offline-failure",
            }
        ]
    else:
        records = [
            {
                **common,
                "provider": "akshare",
                "operation": "catalog",
                "outcome": "success",
                "payload_sha256": CATALOG_SHA256,
                "cutoff_utc": CUTOFF,
                "row_count": 2,
            },
            {
                **common,
                "provider": "akshare",
                "operation": "daily-bars",
                "outcome": "success",
                "payload_sha256": BARS_SHA256,
                "cutoff_utc": CUTOFF,
                "row_count": 2,
            },
        ]
    return {
        "capture_api": "DNS Client + WFP/ETW",
        "profile": "webview-offline-fixed" if offline else "normal",
        "policy_sha256": NETWORK_POLICY_SHA256,
        "unexpected_host_count": 0,
        "telemetry_request_count": 0,
        "proxy_used": False,
        "records": records,
    }


def _journey() -> dict[str, Any]:
    return {
        "elapsed_seconds": 45.0,
        "primary_click_count": 4,
        "onboarding_steps": [
            "welcome",
            "data_preparation",
            "instrument_selection",
            "synchronization",
        ],
        "instrument": {
            "symbol": "000001.SS",
            "name": "上证指数",
            "exchange": "SSE",
            "instrument_kind": "index",
            "period": "daily",
        },
        "real_data": True,
        "demo": False,
        "kline_rendered": True,
        "source": {
            "provider": "akshare",
            "provider_label": "AKShare",
            "cutoff_utc": CUTOFF,
            "row_count": 2,
            "catalog_sha256": CATALOG_SHA256,
            "bars_sha256": BARS_SHA256,
        },
        "fallback": {
            "primary_blocked": False,
            "fallback_used": False,
            "whole_segment": True,
            "primary_provider": "akshare",
            "fallback_provider": None,
        },
    }


def _values(assignment: dict[str, Any], *, offline: bool) -> dict[str, Any]:
    started = not offline
    webview_child_observed = offline
    values: dict[str, Any] = {
        "system": {**assignment["system"], "image_sha256": assignment["image_sha256"]},
        "account-token": {
            "account_type": "standard",
            "is_admin": False,
            "administrator_group_member": False,
            "linked_token_present": False,
            "token_elevation_type": "default",
            "integrity_level": "medium",
            "integrity_rid": 8192,
            "username_contains_non_ascii": True,
            "profile_path_contains_space": True,
        },
        "hardware-observation": {
            "architecture": "x86_64",
            "logical_processor_count": 4,
            "memory_bytes": 8 * 1024**3,
            "free_disk_bytes": 10 * 1024**3,
            "graphics_adapter_sha256": "a" * 64,
            "screen_physical_pixels": {"width": 1920, "height": 1080},
            "timezone": "China Standard Time",
            "locale": "zh-CN",
        },
        "network-observation": _network(offline=offline),
        "webview-before": _webview_state(present=not offline),
        "webview-installation": {
            "attempted": offline,
            "exit_code": 31 if offline else None,
            "installer_sha256": WEBVIEW_INSTALLER_SHA256 if offline else None,
            "fault_injection": assignment["failure_injection"],
        },
        "webview-child-process-token": {
            "observed": webview_child_observed,
            "executable_name": "MicrosoftEdgeWebView2RuntimeInstaller.exe"
            if webview_child_observed
            else None,
            "executable_path_sha256": "b" * 64 if webview_child_observed else None,
            "executable_sha256": WEBVIEW_INSTALLER_SHA256
            if webview_child_observed
            else None,
            "signer": {
                "status": "Valid",
                "subject": "CN=Microsoft Corporation",
                "certificate_sha256": "c" * 64,
            }
            if webview_child_observed
            else None,
            "elevated": False if webview_child_observed else None,
            "integrity_level": "medium" if webview_child_observed else None,
            "integrity_rid": 8192 if webview_child_observed else None,
            "exit_code": 31 if webview_child_observed else None,
        },
        "webview-after": _webview_state(present=not offline),
        "installer-process-token": _process("installer", started=True),
        "desktop-host-process-token": _process("desktop-host", started=started),
        "sidecar-process-token": _process("sidecar", started=started),
        "uninstaller-process-token": _process("uninstaller", started=started),
        "uac-observation": {"uac_prompt_count": 0, "elevation_requested": False},
        "install-observation": {
            "exit_code": 31 if offline else 0,
            "application_files_present": not offline,
            "shortcut_present": not offline,
            "launchable": not offline,
        },
        "filesystem-observation": {
            "install_root_read_only": True,
            "install_root_runtime_write_count": 0,
            "mutable_root_identity": "localappdata-stock-desk-v1.1",
            "unexpected_mutable_root_write_count": 0,
            "legacy_v1_open_count": 0,
            "legacy_v1_write_count": 0,
        },
        "window-observation": {
            "observed": not offline,
            "main_window_count": 0 if offline else 1,
            "external_browser_window_count": 0,
        },
        "v1-canary-before": {"entry_count": 1, "content_sha256": "d" * 64},
        "v1-canary-after": {"entry_count": 1, "content_sha256": "d" * 64},
        "redaction-scan": {
            "secret_match_count": 0,
            "username_match_count": 0,
            "absolute_path_match_count": 0,
        },
        "uninstall-observation": {
            "attempted": not offline,
            "exit_code": None if offline else 0,
            "application_files_removed": False if offline else True,
            "shortcuts_removed": False if offline else True,
        },
    }
    if not offline:
        values.update(
            {
                "display-observation": {"proof": "display"},
                "first-use-journey": _journey(),
                "uia-matrix": {"proof": "uia"},
            }
        )
    return values


def _exercise_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, offline: bool
) -> dict[str, Any]:
    assignment = _assignment(offline=offline)
    package = tmp_path / ("offline" if offline else "success")
    controller = package / "controller"
    raw = package / "raw"
    controller.mkdir(parents=True)
    raw.mkdir()
    policy_bytes = b'{"policy":"approved"}\n'
    policy_sha256 = hashlib.sha256(policy_bytes).hexdigest()
    (controller / "snapshot-policy.json").write_bytes(policy_bytes)
    (controller / "lifecycle-receipt.json").write_text("{}\n", encoding="utf-8")
    (controller / "lifecycle-receipt.sig").write_bytes(b"s" * 64)
    broker_key = tmp_path / "broker-public-key.pem"
    broker_key.write_text("public-key\n", encoding="utf-8")

    record_roles = (
        ("observation-stream", "observations.jsonl")
        if offline
        else ("observation-stream", "observations.jsonl")
    )
    records_spec = [record_roles, ("install-log", "install.log")]
    if offline:
        records_spec.append(("failure-diagnostic", "failure-diagnostic.txt"))
    else:
        records_spec.extend(
            [
                ("uia-action-trace", "uia-actions.json"),
                ("uia-tree", "uia-tree.json"),
                ("focus-region-contact-sheet", "focus-regions.png"),
                ("window-capture-standard", "window-standard.png"),
                ("window-capture-narrow", "window-narrow.png"),
            ]
        )
    record_bytes: dict[str, bytes] = {}
    manifest_records: list[dict[str, str]] = []
    for role, name in records_spec:
        data = b"{}\n" if name.endswith((".json", ".jsonl")) else b"public evidence\n"
        (raw / name).write_bytes(data)
        record_bytes[role] = data
        manifest_records.append({"kind": role, "path": f"raw/{name}"})

    manifest = {
        "schema_version": 2,
        "artifact": "windows-installed-raw-evidence",
        "case_id": assignment["case_id"],
        "scenario": assignment["scenario"],
        "identity": {
            "source_sha": SOURCE_SHA,
            "source_tree": SOURCE_TREE,
            "main_proof_sha256": MAIN_PROOF_SHA256,
            "candidate_sha256": CANDIDATE_SHA256,
            "webview_installer_sha256": WEBVIEW_INSTALLER_SHA256,
        },
        "execution": {
            "repository": "CongBao/stock-desk",
            "workflow": "Windows Installed Acceptance",
            "workflow_ref": "CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
            "workflow_sha": SOURCE_SHA,
            "workflow_path": verifier.WORKFLOW_PATH,
            "workflow_sha256": WORKFLOW_SHA256,
            "run_id": 42,
            "run_attempt": 1,
            "job_id": f"windows-installed-{assignment['case_id']}",
            "job_name": f"Windows installed {assignment['case_id']}",
            "matrix_case_id": assignment["case_id"],
            "matrix_guest_profile": assignment["guest_profile"],
            "matrix_scenario": assignment["scenario"],
            "matrix_dpi_percent": assignment["dpi_percent"],
            "matrix_controller_label": assignment["controller_label"],
            "scenario_attempt": 1,
            "attempt_id": f"{assignment['scenario']}-first-42",
        },
        "capture": {
            "started_at_utc": "2026-07-14T00:00:00Z",
            "completed_at_utc": "2026-07-14T00:01:00Z",
            "guest_profile": assignment["guest_profile"],
            "controller_label": assignment["controller_label"],
            "dpi_percent": assignment["dpi_percent"],
            "guest_harness_sha256": GUEST_HARNESS_SHA256,
            "uia_driver_sha256": UIA_DRIVER_SHA256,
            "controller_request_sha256": CONTROLLER_REQUEST_SHA256,
            "snapshot_policy_sha256": policy_sha256,
            "clean_snapshot_sha256": assignment["snapshot_sha256"],
            "image_sha256": assignment["image_sha256"],
            "webview_product_guid": "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            "minimum_webview_version": "120.0.2210.91",
            "failure_injection": assignment["failure_injection"],
            "data_path": assignment["data_path"],
            "redaction_version": "stock-desk-public-redaction-v2",
        },
        "records": manifest_records,
    }
    (package / "raw-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    values = _values(assignment, offline=offline)
    kinds = verifier.FAILURE_EVENT_KINDS if offline else verifier.SUCCESS_EVENT_KINDS
    events = {kind: {"value": values[kind]} for kind in kinds}

    monkeypatch.setattr(
        verifier, "_records", lambda _package, _manifest: (record_bytes, "f" * 64)
    )
    monkeypatch.setattr(verifier, "_parse_events", lambda _data: events)
    monkeypatch.setattr(verifier, "_validate_window", lambda value: value)
    monkeypatch.setattr(verifier, "_validate_png_capture", lambda *args, **kwargs: None)
    monkeypatch.setattr(verifier, "_validate_lifecycle", lambda *args, **kwargs: {})
    monkeypatch.setattr(verifier, "_validate_display", lambda value, **kwargs: value)
    monkeypatch.setattr(verifier, "_validate_uia", lambda value, **kwargs: value)
    monkeypatch.setattr(
        verifier, "_validate_uia_raw_records", lambda *args, **kwargs: None
    )

    return verifier.verify_package(
        package,
        assignment=assignment,
        expected_source_sha=SOURCE_SHA,
        expected_source_tree=SOURCE_TREE,
        expected_main_proof_sha256=MAIN_PROOF_SHA256,
        expected_candidate_sha256=CANDIDATE_SHA256,
        expected_webview_installer_sha256=WEBVIEW_INSTALLER_SHA256,
        expected_policy_sha256=policy_sha256,
        expected_adapter_sha256=ADAPTER_SHA256,
        expected_controller_request_sha256=CONTROLLER_REQUEST_SHA256,
        expected_guest_harness_sha256=GUEST_HARNESS_SHA256,
        expected_uia_driver_sha256=UIA_DRIVER_SHA256,
        broker_public_key=broker_key,
        expected_broker_public_key_sha256=BROKER_KEY_SHA256,
        expected_repository="CongBao/stock-desk",
        expected_workflow="Windows Installed Acceptance",
        expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
        expected_workflow_sha256=WORKFLOW_SHA256,
        expected_run_id=42,
        expected_run_attempt=1,
    )


def test_verify_package_derives_success_from_raw_bound_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _exercise_package(tmp_path, monkeypatch, offline=False)

    assert evidence["scenario"] == "installed-first-use"
    assert evidence["journey"]["source"]["provider"] == "akshare"
    assert set(evidence["security"]["processes"]) == {
        "installer",
        "desktop_host",
        "sidecar",
        "uninstaller",
    }
    assert evidence["raw_package_sha256"]


def test_verify_package_derives_fixed_offline_failure_without_ui_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _exercise_package(tmp_path, monkeypatch, offline=True)

    assert evidence["scenario"] == "webview-install-failure"
    assert evidence["journey"] is None
    assert evidence["uia"] is None
    assert evidence["security"]["processes"] == {
        "installer": _process("installer", started=True)
    }
