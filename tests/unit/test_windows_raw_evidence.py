from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pytest
from PIL import Image

from scripts import verify_windows_raw_evidence as verifier


DERIVED_FIXTURES = Path("tests/fixtures/windows-installed-evidence")
SCHEMA = Path("schemas/windows-installed-raw-evidence-v1.schema.json")
SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40
MAIN_PROOF_SHA256 = "c" * 64
CANDIDATE_SHA256 = "d" * 64
WEBVIEW_SHA256 = "e" * 64
REPOSITORY = "CongBao/stock-desk"
WORKFLOW = "Installed Windows validation"
WORKFLOW_REF = (
    "CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main"
)
WORKFLOW_PATH = ".github/workflows/windows-installed.yml"
WORKFLOW_SHA256 = "f" * 64
RUN_ID = 424242
RUN_ATTEMPT = 1
JOB_ID = "installed"
GUEST_HARNESS_SHA256 = "1" * 64
CONTROLLER_REQUEST_SHA256 = "2" * 64
SNAPSHOT_POLICY_SHA256 = "3" * 64
ADAPTER_SHA256 = "4" * 64
CONTROLLER_BINDING_SHA256 = "8" * 64
LEASE_DIGEST = hashlib.sha256(
    "\0".join(
        (
            "stock-desk-controller-lease-v1",
            REPOSITORY,
            SOURCE_SHA,
            str(RUN_ID),
            JOB_ID,
            CONTROLLER_BINDING_SHA256,
        )
    ).encode()
).hexdigest()


def _png(*, width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    assert pixels is not None
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _solid_png(*, width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), color=(10, 20, 30))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _closed_objects(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
        for value in node.values():
            _closed_objects(value)
    elif isinstance(node, list):
        for value in node:
            _closed_objects(value)


def _derived(name: str) -> dict[str, Any]:
    value = json.loads((DERIVED_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _event(sequence: int, kind: str, value: object) -> dict[str, object]:
    captured = datetime(2026, 7, 14, tzinfo=timezone.utc) + timedelta(seconds=sequence)
    return {
        "sequence": sequence,
        "captured_at_utc": captured.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": kind,
        "producer": verifier.EXPECTED_PRODUCERS[kind],
        "value": value,
    }


def _browser_event_digest(events: list[dict[str, object]]) -> str:
    lines = [
        "|".join(
            str(event[field])
            for field in (
                "sequence",
                "captured_at_utc",
                "event",
                "process_name",
                "process_id",
                "window_handle",
            )
        )
        for event in events
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _write_raw_fixture(
    root: Path,
    name: str,
    *,
    browser_window_events: list[dict[str, object]] | None = None,
    browser_observer_overrides: dict[str, object] | None = None,
) -> Path:
    """Materialize synthetic unit-only raw records; workflows never consume them."""

    source = _derived(name)
    scenario = source["scenario"]
    profile = "win11" if scenario == "webview-absent" else "win10-22h2"
    label = f"stock-desk-vm-controller-{profile}"
    if scenario == "webview-install-failure":
        source["system"] = copy.deepcopy(_derived("valid-preinstalled.json")["system"])
        source["system"]["image_sha256"] = "9" * 64
    image_sha256 = source["system"]["image_sha256"]
    injection = (
        {
            "identity": "stock-desk-webview2-offline-install-failure-v1",
            "sha256": "8" * 64,
        }
        if scenario == "webview-install-failure"
        else None
    )
    account = {
        **source["account"],
        "administrator_group_member": False,
        "linked_token_present": False,
        "token_elevation_type": "default",
        "integrity_level": "medium",
        "integrity_rid": 8192,
    }
    before = {**source["webview"]["before"], "scope": None}
    after = {**source["webview"]["after"], "scope": None}
    if before["state"] == "present":
        before["scope"] = "machine"
    if after["state"] == "present":
        after["scope"] = "current-user" if scenario == "webview-absent" else "machine"
    installation = {**source["webview"]["installation"], "fault_injection": injection}
    child = {
        "observed": scenario != "webview-preinstalled",
        "executable_name": None,
        "executable_path_sha256": None,
        "executable_sha256": None,
        "signer": None,
        "elevated": None,
        "integrity_level": None,
        "integrity_rid": None,
        "exit_code": None,
    }
    if child["observed"]:
        child.update(
            {
                "executable_name": "MicrosoftEdgeWebView2RuntimeInstaller.exe",
                "executable_path_sha256": "5" * 64,
                "executable_sha256": WEBVIEW_SHA256,
                "signer": {
                    "status": "Valid",
                    "subject": "CN=Microsoft Corporation",
                    "certificate_sha256": "7" * 64,
                },
                "elevated": False,
                "integrity_level": "medium",
                "integrity_rid": 8192,
                "exit_code": source["webview"]["installation"]["exit_code"],
            }
        )
    capture = _png(width=800, height=600)
    capture_digest = hashlib.sha256(capture).hexdigest()
    marker = "000001.SS" if scenario != "webview-install-failure" else None
    capture_text = "000001.SS\n上证指数\n".encode()
    failure_text = (
        "Stock Desk WebView2 install failure observation\n"
        f"webview_child_exit_code={source['webview']['installation']['exit_code']}\n"
        f"nsis_parent_exit_code={source['install']['exit_code']}\n"
        "failure_injection_identity=stock-desk-webview2-offline-install-failure-v1\n"
        "application_files_present=False\nshortcut_present=False\n"
    ).encode()
    phases = (
        ["baseline", "installer", "final"]
        if scenario == "webview-install-failure"
        else ["baseline", "installer", "app-readiness", "stable", "final"]
    )
    stable_browser_windows = (
        [
            {"process_name": "chrome", "process_id": 7, "window_handle": 11},
            {"process_name": "chrome", "process_id": 7, "window_handle": 12},
        ]
        if scenario == "webview-absent"
        else []
    )
    browser_timeline = [
        {
            "captured_at_utc": f"2026-07-14T00:00:{index:02d}Z",
            "phase": phase,
            "windows": copy.deepcopy(stable_browser_windows),
        }
        for index, phase in enumerate(phases, start=1)
    ]
    lifecycle_events = copy.deepcopy(browser_window_events or [])
    browser_observer = {
        "schema": "stock-desk-browser-window-observer-v1",
        "api": "Win32 EnumWindows + SetWinEventHook",
        "hook_started_at_utc": "2026-07-14T00:00:00Z",
        "baseline_captured_at_utc": browser_timeline[0]["captured_at_utc"],
        "baseline_event_sequence": 0,
        "final_captured_at_utc": browser_timeline[-1]["captured_at_utc"],
        "final_event_sequence": len(lifecycle_events),
        "hook_stopped_at_utc": "2026-07-14T00:00:06Z",
        "subscribed_events": ["create", "show", "hide", "destroy"],
        "lifecycle_event_count": len(lifecycle_events),
        "lifecycle_events_sha256": _browser_event_digest(lifecycle_events),
    }
    browser_observer.update(browser_observer_overrides or {})
    raw_window = {
        **source["window"],
        "rendered_content_sha256": (
            None if scenario == "webview-install-failure" else capture_digest
        ),
        "capture_scope": (
            "none" if scenario == "webview-install-failure" else "target-window-only"
        ),
        "ready_marker": marker,
        "uia_text_sha256": (
            None
            if scenario == "webview-install-failure"
            else hashlib.sha256(capture_text).hexdigest()
        ),
        "uia_entry_count": 0 if scenario == "webview-install-failure" else 2,
        "external_browser_observations": browser_timeline,
        "external_browser_window_events": lifecycle_events,
        "external_browser_observer": browser_observer,
    }
    event_values: list[tuple[str, object]] = [
        ("system", source["system"]),
        ("account-token", account),
        ("webview-before", before),
        ("webview-installation", installation),
        ("webview-child-process-token", child),
        ("webview-after", after),
    ]
    for role, kind in {
        "installer": "installer-process-token",
        "desktop_host": "desktop-host-process-token",
        "sidecar": "sidecar-process-token",
        "uninstaller": "uninstaller-process-token",
    }.items():
        process = source["processes"][role]
        event_values.append(
            (
                kind,
                {
                    "role": role,
                    **process,
                    "integrity_level": "medium" if process["started"] else None,
                    "integrity_rid": 8192 if process["started"] else None,
                },
            )
        )
    event_values.extend(
        [
            ("uac-observation", source["security"]),
            ("install-observation", source["install"]),
            ("window-observation", raw_window),
            ("v1-canary-before", source["v1_canary"]["before"]),
            ("v1-canary-after", source["v1_canary"]["after"]),
            ("redaction-scan", source["diagnostic_summary"]["redaction_scan"]),
            ("uninstall-observation", source["uninstall"]),
        ]
    )
    assert [item[0] for item in event_values] == list(verifier.EXPECTED_EVENTS)
    event_stream = (
        b"\n".join(
            json.dumps(
                _event(index, kind, value), separators=(",", ":"), sort_keys=True
            ).encode()
            for index, (kind, value) in enumerate(event_values, start=1)
        )
        + b"\n"
    )

    artifact = root / scenario
    package = artifact / "public"
    raw = package / "raw"
    raw.mkdir(parents=True)
    payloads = {
        "observation-stream": (
            "raw/observations.jsonl",
            "application/x-ndjson",
            event_stream,
        ),
        "install-log": (
            "raw/install.log",
            "text/plain; charset=utf-8",
            b"Stock Desk synthetic unit fixture; no real VM acceptance claim.\n",
        ),
    }
    if scenario == "webview-install-failure":
        payloads["failure-diagnostic"] = (
            "raw/failure-diagnostic.txt",
            "text/plain; charset=utf-8",
            failure_text,
        )
    else:
        payloads["ui-automation-text"] = (
            "raw/capture.txt",
            "text/plain; charset=utf-8",
            capture_text,
        )
        payloads["window-capture"] = ("raw/window.png", "image/png", capture)
    records = []
    for kind, (path_text, media_type, payload) in payloads.items():
        path = package.joinpath(*Path(path_text).parts)
        path.write_bytes(payload)
        records.append(
            {
                "kind": kind,
                "path": path_text,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "media_type": media_type,
            }
        )
    execution = {
        **source["execution"],
        "repository": REPOSITORY,
        "workflow_ref": WORKFLOW_REF,
        "workflow_sha": SOURCE_SHA,
        "workflow_path": WORKFLOW_PATH,
        "workflow_sha256": WORKFLOW_SHA256,
        "matrix_guest_profile": profile,
        "matrix_scenario": scenario,
        "matrix_controller_label": label,
        "job_id": JOB_ID,
    }
    manifest = {
        "schema_version": 1,
        "artifact": "windows-installed-raw-evidence",
        "scenario": scenario,
        "identity": source["identity"],
        "execution": execution,
        "capture": {
            "started_at_utc": "2026-07-14T00:00:00Z",
            "completed_at_utc": "2026-07-14T00:01:00Z",
            "guest_profile": profile,
            "controller_label": label,
            "guest_harness_sha256": GUEST_HARNESS_SHA256,
            "controller_request_sha256": CONTROLLER_REQUEST_SHA256,
            "snapshot_policy_sha256": SNAPSHOT_POLICY_SHA256,
            "clean_snapshot_sha256": "6" * 64,
            "image_sha256": image_sha256,
            "failure_injection": injection,
            "browser_window_observer": browser_observer,
            "redaction_version": "stock-desk-public-redaction-v2",
        },
        "records": records,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    (package / "raw-manifest.json").write_bytes(manifest_bytes)
    controller = artifact / "controller"
    controller.mkdir()
    system_assignment = {
        key: source["system"][key]
        for key in (
            "family",
            "display_version",
            "build_number",
            "update_build_revision",
            "architecture",
        )
    }
    lifecycle = {
        "schema": "stock-desk-windows-vm-lifecycle-receipt-v1",
        "guest_profile": profile,
        "controller_label": label,
        "scenario": scenario,
        "snapshot_policy_sha256": SNAPSHOT_POLICY_SHA256,
        "snapshot_sha256": manifest["capture"]["clean_snapshot_sha256"],
        "image_sha256": image_sha256,
        "system": system_assignment,
        "webview_initial_state": before["state"],
        "failure_injection": injection,
        "controller_request_sha256": CONTROLLER_REQUEST_SHA256,
        "guest_harness_sha256": GUEST_HARNESS_SHA256,
        "guest_executed_harness_sha256": GUEST_HARNESS_SHA256,
        "workflow_sha256": WORKFLOW_SHA256,
        "raw_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "restored_before_at_utc": "2026-07-14T00:00:00Z",
        "acceptance_completed_at_utc": "2026-07-14T00:01:00Z",
        "cleanup_restored_at_utc": "2026-07-14T00:02:00Z",
        "adapter_sha256": ADAPTER_SHA256,
        "controller_binding_sha256": CONTROLLER_BINDING_SHA256,
        "lease_digest": LEASE_DIGEST,
        "lease_expires_at_utc": "2026-07-14T01:00:00Z",
        "watchdog_armed": False,
        "lease_state": "released-after-restore",
        "lease_released_at_utc": "2026-07-14T00:02:00Z",
    }
    (controller / "lifecycle-receipt.json").write_text(
        json.dumps(lifecycle, indent=2, sort_keys=True) + "\n"
    )
    return package


def _packages(tmp_path: Path) -> list[Path]:
    return [
        _write_raw_fixture(tmp_path, name)
        for name in (
            "valid-preinstalled.json",
            "valid-absent.json",
            "valid-failure.json",
        )
    ]


def _verify(packages: list[Path]) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    return verifier.verify_matrix(
        packages,
        expected_source_sha=SOURCE_SHA,
        expected_source_tree=SOURCE_TREE,
        expected_main_proof_sha256=MAIN_PROOF_SHA256,
        expected_candidate_sha256=CANDIDATE_SHA256,
        expected_webview_installer_sha256=WEBVIEW_SHA256,
        expected_repository=REPOSITORY,
        expected_workflow=WORKFLOW,
        expected_workflow_ref=WORKFLOW_REF,
        expected_workflow_sha=SOURCE_SHA,
        expected_workflow_path=WORKFLOW_PATH,
        expected_workflow_sha256=WORKFLOW_SHA256,
        expected_run_id=RUN_ID,
        expected_run_attempt=RUN_ATTEMPT,
        expected_job_id=JOB_ID,
        expected_guest_harness_sha256=GUEST_HARNESS_SHA256,
        expected_controller_request_sha256=CONTROLLER_REQUEST_SHA256,
        expected_snapshot_policy_sha256=SNAPSHOT_POLICY_SHA256,
        expected_adapter_sha256=ADAPTER_SHA256,
    )


def _manifest(package: Path) -> dict[str, Any]:
    return json.loads((package / "raw-manifest.json").read_text())


def _write_manifest(
    package: Path, value: object, *, bind_receipt: bool = False
) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    (package / "raw-manifest.json").write_bytes(payload)
    if bind_receipt:
        receipt_path = package.parent / "controller" / "lifecycle-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["raw_manifest_sha256"] = hashlib.sha256(payload).hexdigest()
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def _rewrite_record(package: Path, kind: str, payload: bytes) -> None:
    manifest = _manifest(package)
    record = next(item for item in manifest["records"] if item["kind"] == kind)
    package.joinpath(*Path(record["path"]).parts).write_bytes(payload)
    record["sha256"] = hashlib.sha256(payload).hexdigest()
    record["size_bytes"] = len(payload)
    _write_manifest(package, manifest, bind_receipt=True)


def _derive_one(package: Path) -> None:
    verifier.derive_package(
        package,
        expected_source_sha=SOURCE_SHA,
        expected_source_tree=SOURCE_TREE,
        expected_main_proof_sha256=MAIN_PROOF_SHA256,
        expected_candidate_sha256=CANDIDATE_SHA256,
        expected_webview_installer_sha256=WEBVIEW_SHA256,
        expected_repository=REPOSITORY,
        expected_workflow=WORKFLOW,
        expected_workflow_ref=WORKFLOW_REF,
        expected_workflow_sha=SOURCE_SHA,
        expected_workflow_path=WORKFLOW_PATH,
        expected_workflow_sha256=WORKFLOW_SHA256,
        expected_run_id=RUN_ID,
        expected_run_attempt=RUN_ATTEMPT,
        expected_job_id=JOB_ID,
        expected_guest_harness_sha256=GUEST_HARNESS_SHA256,
        expected_controller_request_sha256=CONTROLLER_REQUEST_SHA256,
        expected_snapshot_policy_sha256=SNAPSHOT_POLICY_SHA256,
        expected_adapter_sha256=ADAPTER_SHA256,
    )


def test_raw_manifest_schema_is_closed_and_has_no_pass_field() -> None:
    schema = json.loads(SCHEMA.read_text())
    _closed_objects(schema)
    assert '"passed"' not in SCHEMA.read_text()
    assert schema["properties"]["records"]["minItems"] == 3
    assert schema["properties"]["records"]["maxItems"] == 4
    observer = schema["properties"]["capture"]["properties"]["browser_window_observer"]
    assert observer["additionalProperties"] is False
    assert observer["properties"]["api"]["const"] == (
        "Win32 EnumWindows + SetWinEventHook"
    )


@pytest.mark.parametrize("schema_version", (True, "1", 1.0))
def test_raw_manifest_schema_version_is_type_strict(
    tmp_path: Path, schema_version: object
) -> None:
    packages = _packages(tmp_path)
    manifest = _manifest(packages[0])
    manifest["schema_version"] = schema_version
    _write_manifest(packages[0], manifest, bind_receipt=True)

    with pytest.raises(verifier.RawEvidenceError, match="schema identity"):
        _verify(packages)


@pytest.mark.parametrize(
    "operation",
    (
        lambda: verifier._object(None, field="value", fields=frozenset()),
        lambda: verifier._object({}, field="value", fields=frozenset({"required"})),
        lambda: verifier._object({"unknown": True}, field="value", fields=frozenset()),
        lambda: verifier._string("", field="value"),
        lambda: verifier._integer(True, field="value"),
        lambda: verifier._integer(-1, field="value", minimum=0),
        lambda: verifier._boolean(1, field="value"),
        lambda: verifier._load_json_bytes(b'{"a":1,"a":2}', field="value"),
        lambda: verifier._load_json_bytes(b"\xff", field="value"),
        lambda: verifier._load_json_bytes(b"[", field="value"),
        lambda: verifier._parse_utc("2026-02-31T00:00:00Z", field="value"),
    ),
)
def test_low_level_parsers_reject_ambiguous_or_malformed_values(
    operation: Callable[[], object],
) -> None:
    with pytest.raises(verifier.RawEvidenceError):
        operation()


def test_regular_file_reader_rejects_missing_empty_oversized_and_symlinked_files(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"ab")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(oversized)

    for path, maximum in (
        (missing, 1),
        (empty, 1),
        (oversized, 1),
        (symlink, 2),
    ):
        with pytest.raises(verifier.RawEvidenceError):
            verifier._read_regular(path, maximum=maximum, field="fixture")


@pytest.mark.parametrize(
    "payload",
    (
        b"not-an-image",
        _png(width=32, height=32),
        _solid_png(width=800, height=600),
    ),
)
def test_png_validator_rejects_invalid_or_undersized_evidence(payload: bytes) -> None:
    with pytest.raises(verifier.RawEvidenceError):
        verifier._validate_png(payload, kind="window-capture")


@pytest.mark.parametrize(
    "payload",
    (
        b"\xff",
        b"C:\\" + b"Users\\private-user\\secret.txt",
        b"/" + b"home/private/token",
        b"/" + b"Users/private-user/secret.txt",
    ),
)
def test_public_text_validator_rejects_invalid_text_and_user_paths(
    payload: bytes,
) -> None:
    with pytest.raises(verifier.RawEvidenceError):
        verifier._validate_public_text(payload, field="public text")


def test_positive_unit_fixtures_derive_complete_matrix_and_receipt(
    tmp_path: Path,
) -> None:
    packages = _packages(tmp_path)
    derived, receipt = _verify(packages)
    assert {item["scenario"] for item in derived} == verifier.installed.SCENARIOS
    assert receipt["status"] == "verified"
    assert len(receipt["scenario_evidence"]) == 3
    failure_kinds = {item["kind"] for item in _manifest(packages[2])["records"]}
    assert failure_kinds == {
        "observation-stream",
        "install-log",
        "failure-diagnostic",
    }
    absent_events = [
        json.loads(line)
        for line in (packages[1] / "raw" / "observations.jsonl")
        .read_text()
        .splitlines()
    ]
    absent_window = next(
        event["value"]
        for event in absent_events
        if event["kind"] == "window-observation"
    )
    baseline = absent_window["external_browser_observations"][0]["windows"]
    assert [(item["process_id"], item["window_handle"]) for item in baseline] == [
        (7, 11),
        (7, 12),
    ]


@pytest.mark.parametrize(
    "mutation", ["digest", "size", "path", "symlink", "extra-record", "unbound-file"]
)
def test_record_boundary_attacks_fail_closed(tmp_path: Path, mutation: str) -> None:
    package = _packages(tmp_path)[0]
    manifest = _manifest(package)
    record = manifest["records"][0]
    if mutation == "digest":
        record["sha256"] = "0" * 64
    elif mutation == "size":
        record["size_bytes"] += 1
    elif mutation == "path":
        record["path"] = "raw/../secret"
    elif mutation == "extra-record":
        manifest["records"].append(copy.deepcopy(record))
    elif mutation == "unbound-file":
        (package / "raw" / "unbound.txt").write_text("secret")
    else:
        target = package.joinpath(*Path(record["path"]).parts)
        payload = target.read_bytes()
        target.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(payload)
        target.symlink_to(outside)
    _write_manifest(package, manifest, bind_receipt=True)
    with pytest.raises(verifier.RawEvidenceError):
        _derive_one(package)


def test_combined_public_text_size_attack_fails_closed(tmp_path: Path) -> None:
    package = _packages(tmp_path)[0]
    _rewrite_record(
        package,
        "install-log",
        b"x" * (verifier.MAX_PUBLIC_TEXT_BYTES + 1),
    )

    with pytest.raises(verifier.RawEvidenceError, match="public text exceeds"):
        _derive_one(package)


@pytest.mark.parametrize(
    "mutation",
    [
        "passed",
        "retry",
        "workflow-ref",
        "matrix",
        "profile",
        "policy",
        "observer-hook",
        "observer-digest",
        "unknown-field",
    ],
)
def test_manifest_identity_and_workflow_attacks_fail_closed(
    tmp_path: Path, mutation: str
) -> None:
    package = _packages(tmp_path)[0]
    manifest = _manifest(package)
    if mutation == "passed":
        manifest["passed"] = True
    elif mutation == "retry":
        manifest["execution"]["run_attempt"] = 2
    elif mutation == "workflow-ref":
        manifest["execution"]["workflow_ref"] = "evil/ref"
    elif mutation == "matrix":
        manifest["execution"]["matrix_controller_label"] = (
            "stock-desk-vm-controller-win11"
        )
    elif mutation == "profile":
        manifest["capture"]["guest_profile"] = "win11"
    elif mutation == "policy":
        manifest["capture"]["snapshot_policy_sha256"] = "0" * 64
    elif mutation == "observer-hook":
        manifest["capture"]["browser_window_observer"]["hook_started_at_utc"] = (
            "2026-07-14T00:00:02Z"
        )
    elif mutation == "observer-digest":
        manifest["capture"]["browser_window_observer"]["lifecycle_events_sha256"] = (
            "0" * 64
        )
    else:
        manifest["capture"]["controller_secret"] = "forbidden"
    _write_manifest(package, manifest, bind_receipt=True)
    with pytest.raises(verifier.RawEvidenceError):
        _derive_one(package)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-cleanup",
        "image",
        "os-build",
        "raw-binding",
        "adapter-field",
        "adapter-digest",
        "executed-harness",
        "controller-binding",
        "lease-digest",
        "watchdog",
        "lease-state",
        "lease-release",
        "lease-expiry",
    ],
)
def test_controller_lifecycle_and_snapshot_attacks_fail_closed(
    tmp_path: Path, mutation: str
) -> None:
    package = _packages(tmp_path)[0]
    path = package.parent / "controller" / "lifecycle-receipt.json"
    receipt = json.loads(path.read_text())
    if mutation == "missing-cleanup":
        receipt["cleanup_restored_at_utc"] = None
    elif mutation == "image":
        receipt["image_sha256"] = "0" * 64
    elif mutation == "os-build":
        receipt["system"]["build_number"] = 19046
    elif mutation == "raw-binding":
        receipt["raw_manifest_sha256"] = "0" * 64
    elif mutation == "adapter-digest":
        receipt["adapter_sha256"] = "0" * 64
    elif mutation == "executed-harness":
        receipt["guest_executed_harness_sha256"] = "0" * 64
    elif mutation == "controller-binding":
        receipt["controller_binding_sha256"] = "invalid"
    elif mutation == "lease-digest":
        receipt["lease_digest"] = "0" * 64
    elif mutation == "watchdog":
        receipt["watchdog_armed"] = True
    elif mutation == "lease-state":
        receipt["lease_state"] = "armed"
    elif mutation == "lease-release":
        receipt["lease_released_at_utc"] = "2026-07-14T00:01:59Z"
    elif mutation == "lease-expiry":
        receipt["lease_expires_at_utc"] = "2026-07-14T00:01:59Z"
    else:
        receipt["hypervisor_secret"] = "forbidden"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    with pytest.raises(verifier.RawEvidenceError):
        _derive_one(package)


@pytest.mark.parametrize(
    ("field", "timestamp"),
    (
        ("restored_before_at_utc", "2026-07-14T00:00:01Z"),
        ("acceptance_completed_at_utc", "2026-07-13T23:59:59Z"),
        ("cleanup_restored_at_utc", "2026-07-13T23:59:58Z"),
    ),
)
def test_lifecycle_timestamps_must_bound_capture_and_cleanup(
    tmp_path: Path, field: str, timestamp: str
) -> None:
    package = _packages(tmp_path)[0]
    path = package.parent / "controller" / "lifecycle-receipt.json"
    receipt = json.loads(path.read_text())
    receipt[field] = timestamp
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    with pytest.raises(verifier.RawEvidenceError, match="timestamps do not bound"):
        _derive_one(package)


@pytest.mark.parametrize(
    "mutation",
    [
        "admin-group",
        "linked-token",
        "high-integrity",
        "integrity-rid",
        "webview-child",
        "child-integrity-rid",
        "webview-hash",
        "browser",
        "transient-browser",
        "capture-hash",
        "uia-hash",
        "root-title-only",
        "secret",
    ],
)
def test_observation_and_capture_attacks_fail_closed(
    tmp_path: Path, mutation: str
) -> None:
    package = _packages(tmp_path)[1]
    stream = package / "raw" / "observations.jsonl"
    events = [json.loads(line) for line in stream.read_bytes().splitlines()]
    by_kind = {event["kind"]: event for event in events}
    if mutation == "admin-group":
        by_kind["account-token"]["value"]["administrator_group_member"] = True
    elif mutation == "linked-token":
        by_kind["account-token"]["value"]["linked_token_present"] = True
    elif mutation == "high-integrity":
        by_kind["installer-process-token"]["value"]["integrity_level"] = "high"
    elif mutation == "integrity-rid":
        by_kind["installer-process-token"]["value"]["integrity_rid"] = 12288
    elif mutation == "webview-child":
        by_kind["webview-child-process-token"]["value"]["observed"] = False
    elif mutation == "child-integrity-rid":
        by_kind["webview-child-process-token"]["value"]["integrity_rid"] = 12288
    elif mutation == "webview-hash":
        by_kind["webview-child-process-token"]["value"]["executable_sha256"] = "0" * 64
    elif mutation == "browser":
        by_kind["window-observation"]["value"]["external_browser_window_count"] = 1
    elif mutation == "transient-browser":
        by_kind["window-observation"]["value"]["external_browser_observations"][1][
            "windows"
        ] = [{"process_name": "msedge", "process_id": 42, "window_handle": 99}]
    elif mutation == "capture-hash":
        by_kind["window-observation"]["value"]["rendered_content_sha256"] = "0" * 64
    elif mutation == "uia-hash":
        by_kind["window-observation"]["value"]["uia_text_sha256"] = "0" * 64
    elif mutation == "root-title-only":
        root_only = b"Stock Desk\n"
        _rewrite_record(package, "ui-automation-text", root_only)
        by_kind["window-observation"]["value"].update(
            {
                "ready_marker": "Stock Desk",
                "uia_text_sha256": hashlib.sha256(root_only).hexdigest(),
                "uia_entry_count": 1,
            }
        )
    else:
        _rewrite_record(
            package, "ui-automation-text", b"Authorization: Bearer secret\n"
        )
        return _assert_fails(package)
    _rewrite_record(
        package,
        "observation-stream",
        b"\n".join(
            json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
            for event in events
        )
        + b"\n",
    )
    with pytest.raises(
        (verifier.RawEvidenceError, verifier.installed.InstalledEvidenceError)
    ):
        _derive_one(package)


@pytest.mark.parametrize(
    "mutation",
    (
        "system-architecture",
        "system-build",
        "runtime-absent-metadata",
        "runtime-channel",
        "runtime-signer",
        "runtime-scope",
        "runtime-state",
        "installation-exit",
        "installation-digest",
        "child-name",
        "child-signer",
        "child-elevated",
        "child-integrity",
        "child-exit",
        "process-role",
        "process-token-without-start",
        "process-integrity-without-start",
        "uac",
        "redaction-count",
        "uninstall-contradiction",
        "event-producer",
        "event-sequence",
        "event-time",
    ),
)
def test_raw_semantic_contradictions_fail_before_derivation(
    tmp_path: Path, mutation: str
) -> None:
    package = _packages(tmp_path)[1]
    stream = package / "raw" / "observations.jsonl"
    events = [json.loads(line) for line in stream.read_bytes().splitlines()]
    by_kind = {event["kind"]: event for event in events}

    if mutation == "system-architecture":
        by_kind["system"]["value"]["architecture"] = "aarch64"
    elif mutation == "system-build":
        by_kind["system"]["value"]["build_number"] = 1
    elif mutation == "runtime-absent-metadata":
        by_kind["webview-before"]["value"]["version"] = "1.0"
    elif mutation == "runtime-channel":
        by_kind["webview-after"]["value"]["channel"] = "fixed"
    elif mutation == "runtime-signer":
        by_kind["webview-after"]["value"]["signer"]["status"] = "UnknownError"
    elif mutation == "runtime-scope":
        by_kind["webview-after"]["value"]["scope"] = "system"
    elif mutation == "runtime-state":
        by_kind["webview-after"]["value"]["state"] = "unknown"
    elif mutation == "installation-exit":
        by_kind["webview-installation"]["value"]["exit_code"] = None
    elif mutation == "installation-digest":
        by_kind["webview-installation"]["value"]["installer_sha256"] = "0" * 64
    elif mutation == "child-name":
        by_kind["webview-child-process-token"]["value"]["executable_name"] = (
            "untrusted.exe"
        )
    elif mutation == "child-signer":
        by_kind["webview-child-process-token"]["value"]["signer"]["subject"] = (
            "CN=Unknown"
        )
    elif mutation == "child-elevated":
        by_kind["webview-child-process-token"]["value"]["elevated"] = True
    elif mutation == "child-integrity":
        by_kind["webview-child-process-token"]["value"]["integrity_level"] = "high"
    elif mutation == "child-exit":
        by_kind["webview-child-process-token"]["value"]["exit_code"] = 1603
        by_kind["webview-installation"]["value"]["exit_code"] = 1603
    elif mutation == "process-role":
        by_kind["installer-process-token"]["value"]["role"] = "sidecar"
    elif mutation == "process-token-without-start":
        process = by_kind["installer-process-token"]["value"]
        process.update({"started": False, "elevated": False})
    elif mutation == "process-integrity-without-start":
        process = by_kind["installer-process-token"]["value"]
        process.update({"started": False, "elevated": None})
    elif mutation == "uac":
        by_kind["uac-observation"]["value"]["uac_prompt_count"] = 1
    elif mutation == "redaction-count":
        by_kind["redaction-scan"]["value"]["secret_match_count"] = 1
    elif mutation == "uninstall-contradiction":
        by_kind["uninstall-observation"]["value"]["attempted"] = False
    elif mutation == "event-producer":
        events[0]["producer"] = "claimed-system"
    elif mutation == "event-sequence":
        events[1]["sequence"] = 99
    else:
        events[1]["captured_at_utc"] = "2026-07-13T23:59:59Z"

    _rewrite_record(
        package,
        "observation-stream",
        b"\n".join(
            json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
            for event in events
        )
        + b"\n",
    )
    with pytest.raises(verifier.RawEvidenceError):
        _derive_one(package)


@pytest.mark.parametrize(
    "events",
    [
        [
            {
                "sequence": 1,
                "captured_at_utc": "2026-07-14T00:00:02Z",
                "event": "show",
                "process_name": "chrome",
                "process_id": 7,
                "window_handle": 13,
            }
        ],
        [
            {
                "sequence": 1,
                "captured_at_utc": "2026-07-14T00:00:02Z",
                "event": "show",
                "process_name": "chrome",
                "process_id": 7,
                "window_handle": 13,
            },
            {
                "sequence": 2,
                "captured_at_utc": "2026-07-14T00:00:02Z",
                "event": "hide",
                "process_name": "chrome",
                "process_id": 7,
                "window_handle": 13,
            },
            {
                "sequence": 3,
                "captured_at_utc": "2026-07-14T00:00:02Z",
                "event": "destroy",
                "process_name": "chrome",
                "process_id": 7,
                "window_handle": 13,
            },
        ],
    ],
    ids=["same-pid-new-hwnd", "between-polls-short-lived-hwnd"],
)
def test_hook_fixture_rejects_nonbaseline_hwnd_even_when_polling_is_stable(
    tmp_path: Path, events: list[dict[str, object]]
) -> None:
    package = _write_raw_fixture(
        tmp_path,
        "valid-absent.json",
        browser_window_events=events,
    )
    with pytest.raises(verifier.RawEvidenceError, match="non-baseline browser HWND"):
        _derive_one(package)


@pytest.mark.parametrize(
    ("captured_at", "baseline_sequence", "final_sequence", "boundary"),
    [
        ("2026-07-14T00:00:04Z", 1, 1, "baseline"),
        ("2026-07-14T00:00:00Z", 0, 1, "baseline"),
        ("2026-07-14T00:00:04Z", 0, 0, "final"),
        ("2026-07-14T00:00:06Z", 0, 1, "final"),
    ],
    ids=[
        "post-baseline-event-in-pre-baseline-slice",
        "pre-baseline-event-in-post-baseline-slice",
        "pre-final-event-in-post-final-slice",
        "post-final-event-in-pre-final-slice",
    ],
)
def test_hook_fixture_rejects_timestamp_sequence_slice_attacks(
    tmp_path: Path,
    captured_at: str,
    baseline_sequence: int,
    final_sequence: int,
    boundary: str,
) -> None:
    event = {
        "sequence": 1,
        "captured_at_utc": captured_at,
        "event": "show",
        "process_name": "chrome",
        "process_id": 7,
        "window_handle": 11,
    }
    package = _write_raw_fixture(
        tmp_path,
        "valid-absent.json",
        browser_window_events=[event],
        browser_observer_overrides={
            "baseline_event_sequence": baseline_sequence,
            "final_event_sequence": final_sequence,
        },
    )
    with pytest.raises(
        verifier.RawEvidenceError,
        match=rf"wrong {boundary} sequence slice",
    ):
        _derive_one(package)


def _assert_fails(package: Path) -> None:
    with pytest.raises(verifier.RawEvidenceError):
        _derive_one(package)


def test_failure_diagnostic_must_bind_child_and_parent_nonzero_exits(
    tmp_path: Path,
) -> None:
    package = _packages(tmp_path)[2]
    _rewrite_record(
        package,
        "failure-diagnostic",
        b"webview_child_exit_code=1603\nnsis_parent_exit_code=0\n",
    )
    with pytest.raises(verifier.RawEvidenceError, match="parent exits"):
        _derive_one(package)


@pytest.mark.parametrize(
    "kind", ("install-log", "ui-automation-text", "observation-stream")
)
@pytest.mark.parametrize("label", ("secret", "token"))
def test_all_public_text_records_reject_generic_secrets(
    tmp_path: Path, kind: str, label: str
) -> None:
    package = _packages(tmp_path)[0]
    _rewrite_record(package, kind, f"{label}=private-value\n".encode())

    with pytest.raises(verifier.RawEvidenceError, match="contains a secret"):
        _derive_one(package)


def test_cli_writes_only_derived_evidence_and_receipt(tmp_path: Path) -> None:
    packages = _packages(tmp_path / "packages")
    output = tmp_path / "receipt"
    arguments = [
        *(str(path) for path in packages),
        "--source-sha",
        SOURCE_SHA,
        "--source-tree",
        SOURCE_TREE,
        "--main-proof-sha256",
        MAIN_PROOF_SHA256,
        "--candidate-sha256",
        CANDIDATE_SHA256,
        "--webview-installer-sha256",
        WEBVIEW_SHA256,
        "--repository",
        REPOSITORY,
        "--workflow",
        WORKFLOW,
        "--workflow-ref",
        WORKFLOW_REF,
        "--workflow-sha",
        SOURCE_SHA,
        "--workflow-path",
        WORKFLOW_PATH,
        "--workflow-sha256",
        WORKFLOW_SHA256,
        "--run-id",
        str(RUN_ID),
        "--run-attempt",
        str(RUN_ATTEMPT),
        "--job-id",
        JOB_ID,
        "--guest-harness-sha256",
        GUEST_HARNESS_SHA256,
        "--controller-request-sha256",
        CONTROLLER_REQUEST_SHA256,
        "--snapshot-policy-sha256",
        SNAPSHOT_POLICY_SHA256,
        "--adapter-sha256",
        ADAPTER_SHA256,
        "--output",
        str(output),
    ]
    assert verifier.main(arguments) == 0
    assert (
        json.loads((output / "verification-receipt.json").read_text())["status"]
        == "verified"
    )


def test_unit_fixture_text_cannot_be_confused_with_observed_vm_evidence() -> None:
    source = Path(__file__).read_text()
    assert "synthetic unit-only" in source
    assert "no real VM acceptance claim" in source
