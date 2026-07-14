from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from scripts import verify_windows_desktop_raw_evidence as verifier
from scripts import windows_vm_broker_client as broker


SHA = "a" * 40
DIGEST = "b" * 64


@pytest.mark.parametrize(
    "public_text",
    [
        "C:\\" + "Users\\Alice\\evidence.json",
        "/" + "home/alice/evidence.json",
        "/" + "users/alice/evidence.json",
    ],
    ids=("windows-profile", "linux-home", "macos-users"),
)
def test_public_text_leak_pattern_rejects_profile_paths(public_text: str) -> None:
    assert verifier.PUBLIC_TEXT_LEAK_RE.search(public_text)


@pytest.mark.parametrize(
    "public_text",
    [
        "C:\\Program Files\\Stock Desk\\stock-desk.exe",
        "The home page lists active users.",
        "raw/window-capture-640x360.png",
    ],
    ids=("program-files", "ordinary-words", "repository-relative-path"),
)
def test_public_text_leak_pattern_allows_public_paths(public_text: str) -> None:
    assert verifier.PUBLIC_TEXT_LEAK_RE.search(public_text) is None


def _assignment(profile: str, dpi: int, *, offline: bool = False) -> dict[str, object]:
    case_id = f"{profile}-dpi-{dpi}" + ("-webview-offline" if offline else "")
    fallback = case_id == "win11-dpi-150"
    family = "windows-10" if profile == "win10-22h2" else "windows-11"
    return {
        "case_id": case_id,
        "guest_profile": profile,
        "controller_label": f"stock-desk-vm-controller-{profile}",
        "scenario": "webview-install-failure" if offline else "installed-first-use",
        "dpi_percent": dpi,
        "snapshot_id": f"snapshot-{case_id}",
        "snapshot_sha256": hashlib.sha256(f"snapshot:{case_id}".encode()).hexdigest(),
        "image_sha256": hashlib.sha256(f"image:{profile}".encode()).hexdigest(),
        "system": {
            "family": family,
            "display_version": "22H2" if profile == "win10-22h2" else "24H2",
            "build_number": 19045 if profile == "win10-22h2" else 26100,
            "update_build_revision": 1000,
            "architecture": "x86_64",
        },
        "webview_initial_state": "absent" if dpi == 100 or offline else "present",
        "failure_injection": (
            {
                "identity": "stock-desk-webview2-offline-install-failure-v1",
                "sha256": "c" * 64,
            }
            if offline
            else None
        ),
        "data_path": "primary-blocked-fallback" if fallback else "primary",
        "network": {
            "profile": "webview-offline-fixed"
            if offline
            else ("primary-blocked" if fallback else "normal"),
            "policy_sha256": hashlib.sha256(f"network:{case_id}".encode()).hexdigest(),
            "expected_provider": "none"
            if offline
            else ("baostock" if fallback else "akshare"),
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


def _policy() -> dict[str, object]:
    return {
        "schema": "stock-desk-windows-vm-snapshot-policy-v2",
        "broker": {
            "identity": "stock-desk-ephemeral-windows-vm-broker-v1",
            "oidc_audience": "stock-desk-windows-installed-acceptance",
            "lease_ttl_seconds": 3600,
            "restore_after_each_case": True,
            "raw_only": True,
        },
        "assignments": [
            *(_assignment("win10-22h2", dpi) for dpi in verifier.EXPECTED_DPIS),
            *(_assignment("win11", dpi) for dpi in verifier.EXPECTED_DPIS),
            _assignment("win10-22h2", 100, offline=True),
        ],
    }


def _component(
    identity: str,
    *,
    x: int,
    y: int,
    hit: str | None = None,
) -> dict[str, object]:
    return {
        "id": identity,
        "parent_id": "root",
        "x": x,
        "y": y,
        "width": 80,
        "height": 30,
        "is_offscreen": False,
        "is_enabled": True,
        "keyboard_focusable": True,
        "hit_test_id": identity if hit is None else hit,
    }


def _layout(width: int = 640, height: int = 360) -> dict[str, object]:
    return {
        "logical_size": {"width": width, "height": height},
        "window_bounds": {"x": 0, "y": 0, "width": width, "height": height},
        "component_bounds": [
            _component("first", x=10, y=10),
            _component("second", x=10, y=60),
        ],
        "clipped_component_count": 0,
        "overlap_count": 0,
        "tab_sequence": ["first", "second"],
        "focused_element_id": "first",
        "focus_visible": True,
        "focus_evidence": {
            "target_id": "first",
            "target_name": "First",
            "initial_focus_id": "none-or-external",
            "tab_sequence": ["second", "first"],
            "tab_input_count": 2,
            "focus_observation_method": "uia-focused-element-after-real-tab",
            "target_has_keyboard_focus": True,
            "unfocused_region_id": "focus-before",
            "focused_region_id": "focus-after",
            "focus_region_changed": True,
        },
        "escape_result": "closed-safe",
    }


def test_policy_verifier_requires_exact_eleven_case_matrix() -> None:
    assignments = verifier.validate_snapshot_policy(_policy())
    assert set(assignments) == set(verifier.expected_case_ids())
    assert assignments["win11-dpi-150"]["data_path"] == "primary-blocked-fallback"

    missing = copy.deepcopy(_policy())
    missing["assignments"].pop()  # type: ignore[union-attr]
    with pytest.raises(verifier.DesktopEvidenceError, match="exactly 11"):
        verifier.validate_snapshot_policy(missing)

    wrong_fallback = copy.deepcopy(_policy())
    wrong_fallback["assignments"][7]["data_path"] = "primary"  # type: ignore[index]
    wrong_fallback["assignments"][7]["network"] = {  # type: ignore[index]
        "profile": "normal",
        "policy_sha256": "d" * 64,
        "expected_provider": "akshare",
    }
    with pytest.raises(verifier.DesktopEvidenceError, match="fallback"):
        verifier.validate_snapshot_policy(wrong_fallback)


def test_geometry_is_derived_from_raw_rectangles_not_guest_booleans() -> None:
    assert verifier._validate_layout_check(_layout(), label="route") == (640, 360)

    overlap = _layout()
    overlap["component_bounds"][1]["y"] = 20  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="overlap"):
        verifier._validate_layout_check(overlap, label="route")

    occluded = _layout()
    occluded["component_bounds"][0]["hit_test_id"] = "attacker"  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="occluded"):
        verifier._validate_layout_check(occluded, label="route")

    fake_summary = _layout()
    fake_summary["overlap_count"] = 1
    with pytest.raises(verifier.DesktopEvidenceError, match="summary"):
        verifier._validate_layout_check(fake_summary, label="route")

    set_focus_only = _layout()
    set_focus_only["focus_evidence"]["focus_observation_method"] = "set-focus"  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="real-Tab"):
        verifier._validate_layout_check(set_focus_only, label="route")

    no_visible_focus_delta = _layout()
    no_visible_focus_delta["focus_evidence"]["focused_region_id"] = (  # type: ignore[index]
        "focus-before"
    )
    with pytest.raises(verifier.DesktopEvidenceError, match="real-Tab"):
        verifier._validate_layout_check(no_visible_focus_delta, label="route")


def _onboarding_path(name: str) -> dict[str, object]:
    return {
        "target_id": f"button:{name}",
        "target_name": name,
        "initial_focus_id": "button:previous",
        "tab_sequence": [f"button:{name}"],
        "tab_input_count": 1,
        "focus_observation_method": "uia-focused-element-after-real-tab",
        "target_has_keyboard_focus": True,
        "unfocused_region_id": "focus-before",
        "focused_region_id": "focus-after",
        "focus_region_changed": True,
        "activated": True,
    }


def _uia_matrix() -> dict[str, object]:
    def groups(identities: list[str]) -> list[dict[str, object]]:
        return [
            {
                "id": identity,
                "checks": [_layout(1366, 768), _layout(640, 360)],
            }
            for identity in identities
        ]

    value: dict[str, object] = {
        "schema": "stock-desk-windows-uia-matrix-v1",
        "api": "Windows UI Automation 3 + Win32",
        "driver_sha256": "5" * 64,
        "routes": groups(sorted(verifier.EXPECTED_ROUTES)),
        "dialogs": groups(sorted(verifier.EXPECTED_DIALOGS)),
        "keyboard": {
            "pure_keyboard_journey": True,
            "focus_visible": True,
            "tab_order_valid": True,
            "safe_escape": True,
            "focus_observation_count": 30,
            "onboarding_tab_paths": [
                _onboarding_path("开始设置"),
                _onboarding_path("使用此来源并继续"),
                _onboarding_path("同步并继续"),
                _onboarding_path("进入行情工作区"),
            ],
            "auxiliary_tab_paths": [],
        },
        "focus_regions": {},
        "narrow_sidebar": {
            "logical_size": {"width": 640, "height": 360},
            "collapsed_before": True,
            "toggle_control_type": "button",
            "toggle_semantic_name": "展开导航",
            "expanded_after": True,
            "expanded_reflow": True,
            "chart_x_shift": 32,
            "sidebar_chart_overlap_pixels": 0,
        },
    }
    focus_paths = [
        check["focus_evidence"]
        for group_name in ("routes", "dialogs")
        for group in value[group_name]  # type: ignore[union-attr]
        for check in group["checks"]
    ]
    focus_paths.extend(value["keyboard"]["onboarding_tab_paths"])  # type: ignore[index]
    captures: list[dict[str, object]] = []
    offset = 0
    for index, path in enumerate(focus_paths, start=1):
        before_id = f"focus-{index:03d}-before"
        after_id = f"focus-{index:03d}-after"
        path["unfocused_region_id"] = before_id
        path["focused_region_id"] = after_id
        for capture_id in (before_id, after_id):
            captures.append(
                {"id": capture_id, "x": 0, "y": offset, "width": 80, "height": 30}
            )
            offset += 30
    value["focus_regions"] = {
        "schema": "stock-desk-focus-region-contact-sheet-v1",
        "media_kind": "focus-region-contact-sheet",
        "width": 80,
        "height": offset,
        "captures": captures,
    }
    return value


def test_uia_summary_requires_four_real_tab_onboarding_paths() -> None:
    verifier._validate_uia(_uia_matrix(), expected_driver_sha256="5" * 64)

    hardcoded_focus = copy.deepcopy(_uia_matrix())
    hardcoded_focus["keyboard"]["onboarding_tab_paths"][0][  # type: ignore[index]
        "focus_observation_method"
    ] = "set-focus"
    with pytest.raises(verifier.DesktopEvidenceError, match="real-Tab"):
        verifier._validate_uia(hardcoded_focus, expected_driver_sha256="5" * 64)

    skipped_button = copy.deepcopy(_uia_matrix())
    skipped_button["keyboard"]["onboarding_tab_paths"][2]["target_name"] = (  # type: ignore[index]
        "进入行情工作区"
    )
    with pytest.raises(verifier.DesktopEvidenceError, match="incomplete"):
        verifier._validate_uia(skipped_button, expected_driver_sha256="5" * 64)


def _focus_contact_sheet(uia: dict[str, object], *, flatten_first_pair: bool) -> bytes:
    manifest = uia["focus_regions"]  # type: ignore[assignment]
    image = Image.new("RGB", (manifest["width"], manifest["height"]), "black")  # type: ignore[index]
    for index, capture in enumerate(manifest["captures"]):  # type: ignore[index]
        pair_index = 0 if flatten_first_pair and index < 2 else index
        color = ((pair_index * 37) % 255, (pair_index * 71) % 255, 80)
        for x in range(capture["width"]):
            for y in range(capture["height"]):
                image.putpixel((x, capture["y"] + y), color)
        image.putpixel((0, capture["y"]), (255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_focus_visibility_is_recomputed_from_manifest_bound_raw_pixels() -> None:
    uia = _uia_matrix()
    actions = [
        {
            "sequence": sequence,
            "captured_at_utc": "2026-07-14T00:00:01Z",
            "action": "keyboard-enter",
            "target_id": f"target-{sequence}",
            "target_name": "target",
            "target_control_type": "ControlType.Button",
            "major_click": True,
            "outcome": "activated",
        }
        for sequence in range(1, 5)
    ]
    trees = []
    for group_name in ("routes", "dialogs"):
        for group in uia[group_name]:  # type: ignore[union-attr]
            for check in group["checks"]:
                width = check["logical_size"]["width"]
                kind = "route" if group_name == "routes" else "dialog"
                suffix = (
                    "standard"
                    if kind == "route" and width == 1366
                    else ("narrow" if kind == "route" else str(width))
                )
                trees.append(
                    {"identity": f"{kind}:{group['id']}:{suffix}", "check": check}
                )
    verifier._validate_uia_raw_records(
        json.dumps(actions).encode(),
        json.dumps(trees).encode(),
        _focus_contact_sheet(uia, flatten_first_pair=False),
        uia=uia,
        expected_primary_actions=4,
    )
    with pytest.raises(verifier.DesktopEvidenceError, match="raw region pixels"):
        verifier._validate_uia_raw_records(
            json.dumps(actions).encode(),
            json.dumps(trees).encode(),
            _focus_contact_sheet(uia, flatten_first_pair=True),
            uia=uia,
            expected_primary_actions=4,
        )


def _browser_window() -> dict[str, object]:
    baseline: list[object] = []
    return {
        "observed": True,
        "main_window_count": 1,
        "title": "Stock Desk",
        "external_browser_window_count": 0,
        "rendered_content_sha256": "a" * 64,
        "capture_scope": "target-window-only",
        "ready_marker": "000001.SS",
        "uia_text_sha256": "b" * 64,
        "uia_entry_count": 10,
        "external_browser_observations": [
            {
                "captured_at_utc": "2026-07-14T00:00:01Z",
                "phase": "baseline",
                "windows": baseline,
            },
            {
                "captured_at_utc": "2026-07-14T00:00:02Z",
                "phase": "stable",
                "windows": baseline,
            },
            {
                "captured_at_utc": "2026-07-14T00:00:03Z",
                "phase": "final",
                "windows": baseline,
            },
        ],
        "external_browser_window_events": [],
        "external_browser_observer": {
            "schema": "stock-desk-browser-window-observer-v1",
            "api": "Win32 EnumWindows + SetWinEventHook",
            "hook_started_at_utc": "2026-07-14T00:00:00Z",
            "baseline_captured_at_utc": "2026-07-14T00:00:01Z",
            "baseline_event_sequence": 0,
            "final_captured_at_utc": "2026-07-14T00:00:03Z",
            "final_event_sequence": 0,
            "hook_stopped_at_utc": "2026-07-14T00:00:04Z",
            "subscribed_events": ["create", "show", "hide", "destroy"],
            "lifecycle_event_count": 0,
            "lifecycle_events_sha256": hashlib.sha256(b"").hexdigest(),
        },
    }


def test_external_browser_raw_timeline_is_independently_recomputed() -> None:
    verifier._validate_window(_browser_window())

    nonempty_baseline = copy.deepcopy(_browser_window())
    existing = {"process_name": "msedge", "process_id": 101, "window_handle": 202}
    for sample in nonempty_baseline["external_browser_observations"]:  # type: ignore[union-attr]
        sample["windows"] = [existing]
    with pytest.raises(verifier.DesktopEvidenceError, match="baseline must be empty"):
        verifier._validate_window(nonempty_baseline)

    changed_inventory = copy.deepcopy(_browser_window())
    changed_inventory["external_browser_observations"][1]["windows"] = [  # type: ignore[index]
        {"process_name": "chrome", "process_id": 303, "window_handle": 404}
    ]
    with pytest.raises(verifier.DesktopEvidenceError, match="inventory changed"):
        verifier._validate_window(changed_inventory)

    nonbaseline_event = copy.deepcopy(_browser_window())
    event = {
        "sequence": 1,
        "captured_at_utc": "2026-07-14T00:00:02Z",
        "event": "show",
        "process_name": "chrome",
        "process_id": 303,
        "window_handle": 404,
    }
    nonbaseline_event["external_browser_window_events"] = [event]
    nonbaseline_event["external_browser_observer"]["final_event_sequence"] = 1  # type: ignore[index]
    nonbaseline_event["external_browser_observer"]["lifecycle_event_count"] = 1  # type: ignore[index]
    nonbaseline_event["external_browser_observer"]["lifecycle_events_sha256"] = (  # type: ignore[index]
        hashlib.sha256(b"1|2026-07-14T00:00:02Z|show|chrome|303|404").hexdigest()
    )
    with pytest.raises(verifier.DesktopEvidenceError, match="non-baseline"):
        verifier._validate_window(nonbaseline_event)

    contradictory_count = copy.deepcopy(_browser_window())
    contradictory_count["external_browser_observer"]["lifecycle_event_count"] = 1  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="count"):
        verifier._validate_window(contradictory_count)

    boolean_count = copy.deepcopy(_browser_window())
    boolean_count["external_browser_observer"]["lifecycle_event_count"] = False  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="count"):
        verifier._validate_window(boolean_count)

    wrong_digest = copy.deepcopy(_browser_window())
    wrong_digest["external_browser_observer"]["lifecycle_events_sha256"] = "9" * 64  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="digest"):
        verifier._validate_window(wrong_digest)

    non_monotonic = copy.deepcopy(_browser_window())
    non_monotonic["external_browser_observations"][1]["captured_at_utc"] = (  # type: ignore[index]
        "2026-07-13T23:59:59Z"
    )
    with pytest.raises(verifier.DesktopEvidenceError, match="monotonic"):
        verifier._validate_window(non_monotonic)


def test_dpi_requires_four_win32_observations_pmv2_and_roundtrip() -> None:
    value = {
        "requested_scale_percent": 175,
        "get_dpi_for_window": 168,
        "get_dpi_for_system": 168,
        "get_dpi_for_monitor_x": 168,
        "get_dpi_for_monitor_y": 168,
        "window_dpi_awareness_context": "per-monitor-v2",
        "logical_to_physical_roundtrip_max_error_px": 1,
        "dpi_virtualized": False,
        "logical_window_sizes": [
            {
                "width": 1366,
                "height": 768,
                "physical_width": 2391,
                "physical_height": 1344,
                "within_work_area": True,
                "clipped_component_count": 0,
                "overlap_count": 0,
            },
            {
                "width": 640,
                "height": 360,
                "physical_width": 1120,
                "physical_height": 630,
                "within_work_area": True,
                "clipped_component_count": 0,
                "overlap_count": 0,
            },
        ],
    }
    verifier._validate_display(value, dpi_percent=175)
    for field, mutation in (
        ("get_dpi_for_window", 96),
        ("window_dpi_awareness_context", "system-aware"),
        ("dpi_virtualized", True),
        ("logical_to_physical_roundtrip_max_error_px", 2),
    ):
        invalid = {**value, field: mutation}
        with pytest.raises(verifier.DesktopEvidenceError, match="DPI"):
            verifier._validate_display(invalid, dpi_percent=175)


def test_signed_broker_receipt_rejects_tampering_and_wrong_oidc(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_path = tmp_path / "broker.pem"
    public_path.write_bytes(public_bytes)
    public_digest = hashlib.sha256(public_bytes).hexdigest()
    manifest = {
        "case_id": "win10-22h2-dpi-100",
        "identity": {"source_sha": SHA},
        "execution": {"job_id": "windows-installed-win10-22h2-dpi-100"},
        "capture": {
            "started_at_utc": "2026-07-14T00:01:00Z",
            "completed_at_utc": "2026-07-14T00:04:00Z",
        },
        "_raw_sha256": "e" * 64,
    }
    assignment = _assignment("win10-22h2", 100)
    receipt = {
        "schema": "stock-desk-windows-vm-lifecycle-receipt-v2",
        "status": "completed",
        "raw_only": True,
        "case_id": manifest["case_id"],
        "source_sha": SHA,
        "snapshot_policy_sha256": DIGEST,
        "adapter_sha256": "c" * 64,
        "broker_public_key_sha256": public_digest,
        "controller_request_sha256": "3" * 64,
        "guest_harness_sha256": "4" * 64,
        "uia_driver_sha256": "5" * 64,
        "workflow_sha256": "6" * 64,
        "snapshot_sha256": assignment["snapshot_sha256"],
        "image_sha256": assignment["image_sha256"],
        "raw_manifest_sha256": manifest["_raw_sha256"],
        "force_kill": False,
        "restored_before_at_utc": "2026-07-14T00:00:00Z",
        "acceptance_completed_at_utc": "2026-07-14T00:05:00Z",
        "cleanup_restored_at_utc": "2026-07-14T00:06:00Z",
        "lease_expires_at_utc": "2026-07-14T01:00:00Z",
        "lease_released_at_utc": "2026-07-14T00:06:00Z",
        "watchdog_armed_during_run": True,
        "lease_state": "released-after-restore",
        "lease_digest": "f" * 64,
        "broker_request_nonce_sha256": "1" * 64,
        "request_job_id": manifest["execution"]["job_id"],
        "oidc_jti_sha256": "2" * 64,
        "oidc": {
            "issuer": "https://token.actions.githubusercontent.com",
            "audience": "stock-desk-windows-installed-acceptance",
            "repository": "CongBao/stock-desk",
            "repository_id": "1",
            "repository_owner_id": "2",
            "ref": "refs/heads/main",
            "sha": SHA,
            "workflow_ref": "CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
            "workflow_sha": manifest["identity"]["source_sha"],
            "run_id": "42",
            "run_attempt": "1",
            "check_run_id": "314159",
            "runner_environment": "github-hosted",
            "environment": "windows-installed-acceptance",
            "sub": "repo:CongBao/stock-desk:environment:windows-installed-acceptance",
        },
    }
    data = json.dumps(receipt, sort_keys=True).encode()
    signature = private_key.sign(data)
    verifier._validate_lifecycle(
        data,
        signature,
        manifest=manifest,
        assignment=assignment,
        expected_policy_sha256=DIGEST,
        expected_adapter_sha256="c" * 64,
        expected_controller_request_sha256="3" * 64,
        expected_guest_harness_sha256="4" * 64,
        expected_uia_driver_sha256="5" * 64,
        expected_workflow_sha256="6" * 64,
        broker_public_key=public_path,
        expected_broker_public_key_sha256=public_digest,
        expected_repository="CongBao/stock-desk",
        expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
        expected_run_id=42,
        expected_run_attempt=1,
    )
    with pytest.raises(verifier.DesktopEvidenceError, match="signature"):
        verifier._validate_lifecycle(
            data + b" ",
            signature,
            manifest=manifest,
            assignment=assignment,
            expected_policy_sha256=DIGEST,
            expected_adapter_sha256="c" * 64,
            expected_controller_request_sha256="3" * 64,
            expected_guest_harness_sha256="4" * 64,
            expected_uia_driver_sha256="5" * 64,
            expected_workflow_sha256="6" * 64,
            broker_public_key=public_path,
            expected_broker_public_key_sha256=public_digest,
            expected_repository="CongBao/stock-desk",
            expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
            expected_run_id=42,
            expected_run_attempt=1,
        )

    wrong_oidc = copy.deepcopy(receipt)
    wrong_oidc["oidc"]["ref"] = "refs/heads/attacker"
    wrong_oidc_data = json.dumps(wrong_oidc, sort_keys=True).encode()
    with pytest.raises(verifier.DesktopEvidenceError, match="protected main"):
        verifier._validate_lifecycle(
            wrong_oidc_data,
            private_key.sign(wrong_oidc_data),
            manifest=manifest,
            assignment=assignment,
            expected_policy_sha256=DIGEST,
            expected_adapter_sha256="c" * 64,
            expected_controller_request_sha256="3" * 64,
            expected_guest_harness_sha256="4" * 64,
            expected_uia_driver_sha256="5" * 64,
            expected_workflow_sha256="6" * 64,
            broker_public_key=public_path,
            expected_broker_public_key_sha256=public_digest,
            expected_repository="CongBao/stock-desk",
            expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
            expected_run_id=42,
            expected_run_attempt=1,
        )

    for field, invalid in (
        ("workflow_sha", "9" * 40),
        ("check_run_id", "0"),
        ("runner_environment", "self-hosted"),
        ("run_id", 42),
    ):
        wrong_claim = copy.deepcopy(receipt)
        wrong_claim["oidc"][field] = invalid
        wrong_claim_data = json.dumps(wrong_claim, sort_keys=True).encode()
        with pytest.raises(verifier.DesktopEvidenceError, match="protected main"):
            verifier._validate_lifecycle(
                wrong_claim_data,
                private_key.sign(wrong_claim_data),
                manifest=manifest,
                assignment=assignment,
                expected_policy_sha256=DIGEST,
                expected_adapter_sha256="c" * 64,
                expected_controller_request_sha256="3" * 64,
                expected_guest_harness_sha256="4" * 64,
                expected_uia_driver_sha256="5" * 64,
                expected_workflow_sha256="6" * 64,
                broker_public_key=public_path,
                expected_broker_public_key_sha256=public_digest,
                expected_repository="CongBao/stock-desk",
                expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
                expected_run_id=42,
                expected_run_attempt=1,
            )

    wrong_harness = copy.deepcopy(receipt)
    wrong_harness["guest_harness_sha256"] = "9" * 64
    wrong_harness_data = json.dumps(wrong_harness, sort_keys=True).encode()
    with pytest.raises(verifier.DesktopEvidenceError, match="not bound"):
        verifier._validate_lifecycle(
            wrong_harness_data,
            private_key.sign(wrong_harness_data),
            manifest=manifest,
            assignment=assignment,
            expected_policy_sha256=DIGEST,
            expected_adapter_sha256="c" * 64,
            expected_controller_request_sha256="3" * 64,
            expected_guest_harness_sha256="4" * 64,
            expected_uia_driver_sha256="5" * 64,
            expected_workflow_sha256="6" * 64,
            broker_public_key=public_path,
            expected_broker_public_key_sha256=public_digest,
            expected_repository="CongBao/stock-desk",
            expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
            expected_run_id=42,
            expected_run_attempt=1,
        )

    wrong_request_job = copy.deepcopy(receipt)
    wrong_request_job["request_job_id"] = "windows-installed-attacker"
    wrong_request_job_data = json.dumps(wrong_request_job, sort_keys=True).encode()
    with pytest.raises(verifier.DesktopEvidenceError, match="not bound"):
        verifier._validate_lifecycle(
            wrong_request_job_data,
            private_key.sign(wrong_request_job_data),
            manifest=manifest,
            assignment=assignment,
            expected_policy_sha256=DIGEST,
            expected_adapter_sha256="c" * 64,
            expected_controller_request_sha256="3" * 64,
            expected_guest_harness_sha256="4" * 64,
            expected_uia_driver_sha256="5" * 64,
            expected_workflow_sha256="6" * 64,
            broker_public_key=public_path,
            expected_broker_public_key_sha256=public_digest,
            expected_repository="CongBao/stock-desk",
            expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
            expected_run_id=42,
            expected_run_attempt=1,
        )

    fabricated_oidc_job = copy.deepcopy(receipt)
    fabricated_oidc_job["oidc"]["job_id"] = manifest["execution"]["job_id"]
    fabricated_oidc_job_data = json.dumps(fabricated_oidc_job, sort_keys=True).encode()
    with pytest.raises(verifier.DesktopEvidenceError, match="fields are not closed"):
        verifier._validate_lifecycle(
            fabricated_oidc_job_data,
            private_key.sign(fabricated_oidc_job_data),
            manifest=manifest,
            assignment=assignment,
            expected_policy_sha256=DIGEST,
            expected_adapter_sha256="c" * 64,
            expected_controller_request_sha256="3" * 64,
            expected_guest_harness_sha256="4" * 64,
            expected_uia_driver_sha256="5" * 64,
            expected_workflow_sha256="6" * 64,
            broker_public_key=public_path,
            expected_broker_public_key_sha256=public_digest,
            expected_repository="CongBao/stock-desk",
            expected_workflow_ref="CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
            expected_run_id=42,
            expected_run_attempt=1,
        )


def test_raw_record_roles_media_and_nested_objects_are_closed(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    observations = raw / "observations.jsonl"
    install_log = raw / "install.log"
    diagnostic = raw / "failure-diagnostic.txt"
    observations.write_text("{}\n", encoding="utf-8")
    install_log.write_text("log\n", encoding="utf-8")
    diagnostic.write_text("failure\n", encoding="utf-8")

    def record(kind: str, path: Path, media_type: str) -> dict[str, object]:
        data = path.read_bytes()
        return {
            "kind": kind,
            "path": f"raw/{path.name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "media_type": media_type,
        }

    manifest = {
        "records": [
            record("observation-stream", observations, "application/x-ndjson"),
            record("install-log", install_log, "text/plain; charset=utf-8"),
            record("failure-diagnostic", diagnostic, "text/plain; charset=utf-8"),
        ]
    }
    verifier._records(tmp_path, manifest)
    malicious = copy.deepcopy(manifest)
    malicious["records"][2]["media_type"] = "application/x-executable"
    with pytest.raises(verifier.DesktopEvidenceError, match="media type"):
        verifier._records(tmp_path, malicious)

    account = {
        "account_type": "standard",
        "is_admin": False,
        "administrator_group_member": False,
        "linked_token_present": False,
        "token_elevation_type": "default",
        "integrity_level": "medium",
        "integrity_rid": 8192,
        "username_contains_non_ascii": True,
        "profile_path_contains_space": True,
        "private_username": "should-not-survive",
    }
    with pytest.raises(verifier.DesktopEvidenceError, match="not closed"):
        verifier._validate_account(account)


def test_broker_client_rejects_cross_origin_urls_and_zip_traversal(
    tmp_path: Path,
) -> None:
    assert (
        broker._same_broker_url(
            "https://vm.example/v1/upload/lease",
            endpoint="https://vm.example",
            label="upload",
        )
        == "https://vm.example/v1/upload/lease"
    )
    with pytest.raises(broker.BrokerError, match="escapes"):
        broker._same_broker_url(
            "https://attacker.example/steal",
            endpoint="https://vm.example",
            label="upload",
        )
    with pytest.raises(broker.BrokerError, match="HTTPS origin"):
        broker._broker_origin("http://vm.example")
    assert (
        broker._NoRedirectHandler().redirect_request(
            object(), object(), 302, "Found", {}, "https://attacker.example/steal"
        )
        is None
    )

    import io
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../escape.json", "{}")
    with pytest.raises(broker.BrokerError, match="unsafe"):
        broker._extract_result(output.getvalue(), tmp_path / "result")

    broker_source = Path("scripts/windows_vm_broker_client.py").read_text(
        encoding="utf-8"
    )
    assert '"request_job_id": job_id' in broker_source
    assert '"job_id": job_id' not in broker_source


def test_v2_schemas_are_closed_at_every_declared_object() -> None:
    for name in (
        "windows-installed-raw-evidence-v2.schema.json",
        "windows-installed-evidence-v2.schema.json",
        "windows-vm-lifecycle-receipt-v2.schema.json",
        "windows-vm-snapshot-policy-v2.schema.json",
    ):
        value = json.loads((Path("schemas") / name).read_text(encoding="utf-8"))
        stack = [value]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


def test_reviewed_uia_driver_observes_real_tab_escape_and_target_window_dpi() -> None:
    source = Path("scripts/windows_desktop_uia_driver.ps1").read_text(encoding="utf-8")
    for required in (
        "GetWindowDpiAwarenessContext(IntPtr hwnd)",
        "GetWindowDpiAwarenessContext($hwnd)",
        "Send-Key -Keys '{TAB}'",
        "Observed Tab order differs",
        "Send-Key -Keys '{ESC}'",
        "Escape did not safely close",
        "$script:KeyboardMatrixCheckCount -eq 26",
        "$script:EscapeBehaviorCheckCount -eq 14",
        "Move-FocusToElementByTab",
        "uia-focused-element-after-real-tab",
        "onboarding_tab_paths",
        "RuntimeProbe",
    ):
        assert required in source
    assert "GetThreadDpiAwarenessContext" not in source
    assert "tab_sequence = $visualSequence" not in source
    assert "pure_keyboard_journey = $true" not in source
    assert "focus_visible = $true" not in source
    assert "focus_region_changed = $true" not in source
    assert ".SetFocus()" not in source


def test_runtime_dialog_lookup_remains_process_bound_and_top_level() -> None:
    source = Path("scripts/windows_desktop_uia_driver.ps1").read_text(encoding="utf-8")
    dialog_finder = source.split("function Find-TopLevelWindow", 1)[1].split(
        "function Add-Action", 1
    )[0]

    assert "TreeScope]::Descendants" in dialog_finder
    assert "ProcessIdProperty" in dialog_finder
    assert "ControlTypeProperty" in dialog_finder
    assert "NameProperty" in dialog_finder
    assert "GetAncestor" in dialog_finder
    assert "GA_ROOT" in dialog_finder
    assert "TreeScope]::Children" not in dialog_finder


def test_focus_contact_sheet_uses_explicit_bounded_dimensions() -> None:
    source = Path("scripts/windows_desktop_uia_driver.ps1").read_text(encoding="utf-8")
    contact_sheet = source.split("function Write-FocusRegionContactSheet", 1)[1].split(
        "function Move-FocusToElementByTab", 1
    )[0]

    assert "Measure-Object" not in contact_sheet
    assert "foreach ($capture in $script:FocusRegionCaptures)" in contact_sheet
    assert "$captureWidth = [int]$capture.width" in contact_sheet
    assert "$captureHeight = [int]$capture.height" in contact_sheet
    assert "$sheetHeight = [long]0" in contact_sheet
    assert "$sheetHeight -gt 32768" in contact_sheet


def test_windows_ci_executes_controlled_uia_runtime_fixture() -> None:
    integration = Path(
        "tests/windows/windows_desktop_uia_driver_integration.ps1"
    ).read_text(encoding="utf-8")
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    for required in (
        "StockDeskUiaRuntimeFixture",
        "Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe",
        "& $driverPath",
        "-RuntimeProbe",
        "actual_tab_activation_observed",
        "actual_escape_close_observed",
        "uia-focused-element-after-real-tab",
        "focus_region_changed",
        "focused_region_id",
        "unfocused_region_id",
        "focus_region_contact_sheet_sha256",
        "runtime_actions_sha256",
        "runtime_tree_sha256",
        "runtime_probe_sha256",
        "target_window_capture_sha256",
    ):
        assert required in integration
    assert "UIA driver runtime integration requires Windows" in integration
    assert "Execute Windows browser and UIA observer integrations" in workflow
    assert "$uiaReceipt.executed_on_windows -ne $true" in workflow
    assert "$uiaReceipt.controlled_uia_fixture -ne $true" in workflow
    assert "$uiaReceipt.actual_tab_activation_observed -ne $true" in workflow
    assert "$uiaReceipt.actual_escape_close_observed -ne $true" in workflow
    assert "$uiaReceipt.focus_region_changed -ne $true" in workflow
    assert (
        "$uiaReceipt.focused_region_id -ceq $uiaReceipt.unfocused_region_id" in workflow
    )
    assert "raw-uia-runtime" not in workflow
    for provenance_path in (
        "uia-runtime-probe/driver-result.json:provenance",
        "uia-runtime-probe/uia-actions.json:provenance",
        "uia-runtime-probe/uia-tree.json:provenance",
        "uia-runtime-probe/runtime-probe-window.png:provenance",
        "uia-runtime-probe/focus-region-contact-sheet.png:provenance",
    ):
        assert provenance_path in workflow
