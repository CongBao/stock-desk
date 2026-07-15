from __future__ import annotations

import copy
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from scripts import verify_windows_desktop_raw_evidence as verifier
from tests.unit import test_windows_desktop_raw_evidence_v2 as existing


DIGEST = "a" * 64
GIT_OBJECT = "b" * 40
TIMESTAMP = "2026-07-15T00:00:00Z"


def _event(
    sequence: int = 1,
    *,
    kind: str = "system",
    producer: str = "protected-observer",
    value: object | None = None,
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "captured_at_utc": TIMESTAMP,
        "kind": kind,
        "producer": producer,
        "value": {} if value is None else value,
    }


def _event_bytes(*events: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(event) for event in events) + "\n").encode()


def _png_bytes(
    size: tuple[int, int],
    *,
    mode: str = "RGB",
    image_format: str = "PNG",
    varied: bool = True,
) -> bytes:
    image = Image.new(mode, size)
    for y in range(size[1]):
        for x in range(size[0]):
            if not varied:
                value: int | tuple[int, ...] = 0
            elif mode == "RGB":
                value = ((x * 17) % 256, (y * 29) % 256, ((x + y) * 11) % 256)
            elif mode == "RGBA":
                value = (
                    (x * 17) % 256,
                    (y * 29) % 256,
                    ((x + y) * 11) % 256,
                    255,
                )
            else:
                value = (x * 17 + y * 29) % 256
            image.putpixel((x, y), value)
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _account() -> dict[str, object]:
    return {
        "account_type": "standard",
        "is_admin": False,
        "administrator_group_member": False,
        "linked_token_present": False,
        "token_elevation_type": "default",
        "integrity_level": "medium",
        "integrity_rid": 8192,
        "username_contains_non_ascii": True,
        "profile_path_contains_space": True,
    }


def _hardware() -> dict[str, object]:
    return {
        "architecture": "x86_64",
        "logical_processor_count": 4,
        "memory_bytes": 8 * 1024**3,
        "free_disk_bytes": 20 * 1024**3,
        "graphics_adapter_sha256": DIGEST,
        "screen_physical_pixels": {"width": 1920, "height": 1080},
        "timezone": "China Standard Time",
        "locale": "zh-CN",
    }


def _process(*, started: bool) -> dict[str, object]:
    return {
        "role": "desktop_host",
        "started": started,
        "elevated": False if started else None,
        "integrity_level": "medium" if started else None,
        "integrity_rid": 8192 if started else None,
    }


def _webview_present() -> dict[str, object]:
    return {
        "state": "present",
        "product_guid": "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        "version": "120.0.2210.91",
        "channel": "evergreen",
        "signer": {
            "status": "Valid",
            "subject": "CN=Microsoft Corporation",
            "certificate_sha256": DIGEST,
        },
        "scope": "machine",
    }


def _webview_absent() -> dict[str, object]:
    return {
        "state": "absent",
        "product_guid": None,
        "version": None,
        "channel": None,
        "signer": None,
        "scope": None,
    }


def _network_record(
    provider: str,
    operation: str,
    outcome: str,
    *,
    payload: bool = False,
) -> dict[str, object]:
    record: dict[str, object] = {
        "provider": provider,
        "operation": operation,
        "host": "example.invalid",
        "started_at_utc": TIMESTAMP,
        "completed_at_utc": "2026-07-15T00:00:01Z",
        "tls_system_validation": True,
        "outcome": outcome,
    }
    if payload:
        record["payload_sha256"] = DIGEST
        record["cutoff_utc"] = TIMESTAMP
        record["row_count"] = 100
    return record


def _network(
    *,
    profile: str,
    policy_sha256: str,
    records: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "capture_api": "DNS Client + WFP/ETW",
        "profile": profile,
        "policy_sha256": policy_sha256,
        "unexpected_host_count": 0,
        "telemetry_request_count": 0,
        "proxy_used": False,
        "records": records,
    }


def _journey(*, fallback: bool) -> dict[str, object]:
    provider = "baostock" if fallback else "akshare"
    return {
        "elapsed_seconds": 42.5,
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
            "exchange": "SH",
            "instrument_kind": "index",
            "period": "1d",
        },
        "real_data": True,
        "demo": False,
        "kline_rendered": True,
        "source": {
            "provider": provider,
            "provider_label": "BaoStock" if fallback else "AKShare",
            "cutoff_utc": TIMESTAMP,
            "row_count": 100,
            "catalog_sha256": DIGEST,
            "bars_sha256": "c" * 64,
        },
        "fallback": {
            "primary_blocked": fallback,
            "fallback_used": fallback,
            "whole_segment": True,
            "primary_provider": "akshare",
            "fallback_provider": "baostock" if fallback else None,
        },
    }


def _main_args(tmp_path: Path) -> list[str]:
    return [
        "--policy",
        str(tmp_path / "policy.json"),
        "--output-root",
        str(tmp_path / "output"),
        "--source-sha",
        GIT_OBJECT,
        "--source-tree",
        "c" * 40,
        "--main-proof-sha256",
        DIGEST,
        "--candidate-sha256",
        DIGEST,
        "--desktop-host-sha256",
        "6" * 64,
        "--sidecar-sha256",
        "7" * 64,
        "--signer-subject",
        "CN=Stock Desk Release",
        "--certificate-thumbprint",
        "A" * 40,
        "--timestamp-subject",
        "CN=Trusted Timestamp",
        "--webview-installer-sha256",
        DIGEST,
        "--snapshot-policy-sha256",
        DIGEST,
        "--adapter-sha256",
        DIGEST,
        "--controller-request-sha256",
        DIGEST,
        "--guest-harness-sha256",
        DIGEST,
        "--uia-driver-sha256",
        DIGEST,
        "--broker-public-key",
        str(tmp_path / "broker.pem"),
        "--broker-public-key-sha256",
        DIGEST,
        "--repository",
        "CongBao/stock-desk",
        "--workflow",
        "Windows installed VM acceptance",
        "--workflow-ref",
        "CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main",
        "--workflow-sha256",
        DIGEST,
        "--run-id",
        "42",
        "--run-attempt",
        "1",
        str(tmp_path / "package"),
    ]


def _uia_actions() -> list[dict[str, object]]:
    return [
        {
            "sequence": sequence,
            "captured_at_utc": TIMESTAMP,
            "action": "keyboard-enter",
            "target_id": f"target-{sequence}",
            "target_name": "target",
            "target_control_type": "ControlType.Button",
            "major_click": True,
            "outcome": "activated",
        }
        for sequence in range(1, 5)
    ]


def _uia_trees(uia: dict[str, object]) -> list[dict[str, object]]:
    trees: list[dict[str, object]] = []
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
    return trees


def test_read_json_covers_closed_file_boundaries(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text('{"case_id":"win11-dpi-100"}', encoding="utf-8")
    assert verifier._read_json(valid, "fixture") == {"case_id": "win11-dpi-100"}

    missing = tmp_path / "missing.json"
    with pytest.raises(verifier.DesktopEvidenceError, match="missing or unsafe"):
        verifier._read_json(missing, "fixture")

    link = tmp_path / "link.json"
    link.symlink_to(valid)
    with pytest.raises(verifier.DesktopEvidenceError, match="missing or unsafe"):
        verifier._read_json(link, "fixture")

    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(verifier.DesktopEvidenceError, match="invalid size"):
        verifier._read_json(empty, "fixture")

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{}")
    with pytest.raises(verifier.DesktopEvidenceError, match="invalid size"):
        verifier._read_json(oversized, "fixture", maximum=1)

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_bytes(b"\xff")
    with pytest.raises(verifier.DesktopEvidenceError, match="unreadable"):
        verifier._read_json(unreadable, "fixture")

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(verifier.DesktopEvidenceError, match="unreadable"):
        verifier._read_json(malformed, "fixture")

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(verifier.DesktopEvidenceError, match="must be an object"):
        verifier._read_json(array, "fixture")


def test_parse_events_covers_valid_and_fail_closed_streams() -> None:
    first = _event()
    second = _event(2, kind="network-observation", value={"records": []})
    parsed = verifier._parse_events(_event_bytes(first, second))
    assert list(parsed) == ["system", "network-observation"]
    assert verifier._value(parsed, "network-observation") == {"records": []}

    invalid_streams: list[tuple[bytes, str]] = [
        (b"\xff", "not UTF-8"),
        (b"", "empty"),
        (b"{\n", "invalid JSON"),
        (_event_bytes({**first, "unexpected": True}), "fields are not closed"),
        (_event_bytes({**first, "sequence": 2}), "not contiguous"),
        (_event_bytes({**first, "captured_at_utc": "yesterday"}), "timestamp"),
        (_event_bytes({**first, "kind": ""}), "kind is invalid"),
        (_event_bytes({**first, "producer": ""}), "producer is invalid"),
        (_event_bytes(first, {**second, "kind": "system"}), "duplicated"),
        (_event_bytes({**first, "value": []}), "must be an object"),
    ]
    for data, message in invalid_streams:
        with pytest.raises(verifier.DesktopEvidenceError, match=message):
            verifier._parse_events(data)

    with pytest.raises(verifier.DesktopEvidenceError, match="required observation"):
        verifier._value(parsed, "missing")
    with pytest.raises(verifier.DesktopEvidenceError, match="required observation"):
        verifier._value({"system": {}}, "system")
    with pytest.raises(verifier.DesktopEvidenceError, match="must be an object"):
        verifier._value({"system": {"value": []}}, "system")


def test_png_capture_covers_format_geometry_and_content_boundaries() -> None:
    verifier._validate_png_capture(
        _png_bytes((64, 36)), logical_size=(64, 36), dpi_percent=100
    )
    verifier._validate_png_capture(
        _png_bytes((80, 45), mode="RGBA"),
        logical_size=(64, 36),
        dpi_percent=125,
    )

    invalid_captures: list[tuple[bytes, tuple[int, int], str]] = [
        (_png_bytes((64, 36), image_format="JPEG"), (64, 36), "valid bounded PNG"),
        (_png_bytes((64, 36), mode="L"), (64, 36), "valid bounded PNG"),
        (_png_bytes((80, 36)), (64, 36), "valid bounded PNG"),
        (_png_bytes((64, 36), varied=False), (64, 36), "valid bounded PNG"),
        (b"not-an-image", (64, 36), "valid bounded PNG"),
    ]
    for data, logical_size, message in invalid_captures:
        with pytest.raises(verifier.DesktopEvidenceError, match=message):
            verifier._validate_png_capture(
                data, logical_size=logical_size, dpi_percent=100
            )


def test_account_validator_covers_authority_and_profile_boundaries() -> None:
    assert verifier._validate_account(_account()) == _account()

    administrator = _account()
    administrator["administrator_group_member"] = True
    with pytest.raises(verifier.DesktopEvidenceError, match="administrator authority"):
        verifier._validate_account(administrator)

    malformed_boolean = _account()
    malformed_boolean["linked_token_present"] = 0
    with pytest.raises(verifier.DesktopEvidenceError, match="must be boolean"):
        verifier._validate_account(malformed_boolean)

    wrong_profile = _account()
    wrong_profile["username_contains_non_ascii"] = False
    with pytest.raises(verifier.DesktopEvidenceError, match="Chinese-name"):
        verifier._validate_account(wrong_profile)

    expanded = _account()
    expanded["private_username"] = "secret"
    with pytest.raises(verifier.DesktopEvidenceError, match="fields are not closed"):
        verifier._validate_account(expanded)


def test_hardware_validator_covers_capacity_identity_and_screen_boundaries() -> None:
    assert verifier._validate_hardware(_hardware()) == _hardware()

    unsupported = _hardware()
    unsupported["architecture"] = "arm64"
    with pytest.raises(verifier.DesktopEvidenceError, match="below"):
        verifier._validate_hardware(unsupported)

    malformed_count = _hardware()
    malformed_count["logical_processor_count"] = True
    with pytest.raises(verifier.DesktopEvidenceError, match="invalid"):
        verifier._validate_hardware(malformed_count)

    bad_digest = _hardware()
    bad_digest["graphics_adapter_sha256"] = "INVALID"
    with pytest.raises(verifier.DesktopEvidenceError, match="SHA-256"):
        verifier._validate_hardware(bad_digest)

    small_screen = _hardware()
    small_screen["screen_physical_pixels"] = {"width": 639, "height": 360}
    with pytest.raises(verifier.DesktopEvidenceError, match="invalid"):
        verifier._validate_hardware(small_screen)

    malformed_screen = _hardware()
    malformed_screen["screen_physical_pixels"] = {"width": 1920}
    with pytest.raises(verifier.DesktopEvidenceError, match="fields are not closed"):
        verifier._validate_hardware(malformed_screen)

    empty_locale = _hardware()
    empty_locale["locale"] = ""
    with pytest.raises(verifier.DesktopEvidenceError, match="invalid"):
        verifier._validate_hardware(empty_locale)


def test_process_validator_covers_started_stopped_and_elevation_states() -> None:
    running = _process(started=True)
    stopped = _process(started=False)
    assert (
        verifier._validate_process(running, "desktop host", expected_started=True)
        == running
    )
    assert (
        verifier._validate_process(stopped, "sidecar", expected_started=False)
        == stopped
    )

    contradictory = _process(started=False)
    with pytest.raises(verifier.DesktopEvidenceError, match="contradictory"):
        verifier._validate_process(contradictory, "desktop host", expected_started=True)

    elevated = _process(started=True)
    elevated["elevated"] = True
    with pytest.raises(verifier.DesktopEvidenceError, match="elevated"):
        verifier._validate_process(elevated, "desktop host", expected_started=True)

    expanded = _process(started=True)
    expanded["process_id"] = 42
    with pytest.raises(verifier.DesktopEvidenceError, match="fields are not closed"):
        verifier._validate_process(expanded, "desktop host", expected_started=True)


def test_webview_validator_covers_present_absent_and_trust_boundaries() -> None:
    present = _webview_present()
    absent = _webview_absent()
    assert verifier._validate_webview_state(present, "before") == present
    assert verifier._validate_webview_state(absent, "before") == absent

    malformed_version = _webview_present()
    malformed_version["version"] = "120.bad.2210.91"
    with pytest.raises(verifier.DesktopEvidenceError, match="version is malformed"):
        verifier._validate_webview_state(malformed_version, "before")

    unsupported_mutations: tuple[tuple[str, object], ...] = (
        ("product_guid", "{00000000-0000-0000-0000-000000000000}"),
        ("version", "119.0.0.0"),
        ("version", "120.0.2210"),
        ("channel", "fixed"),
        ("scope", "portable"),
    )
    for field, replacement in unsupported_mutations:
        unsupported = _webview_present()
        unsupported[field] = replacement
        with pytest.raises(verifier.DesktopEvidenceError, match="supported production"):
            verifier._validate_webview_state(unsupported, "before")

    bad_signer = _webview_present()
    bad_signer["signer"] = {
        "status": "UnknownError",
        "subject": "CN=Microsoft Corporation",
        "certificate_sha256": DIGEST,
    }
    with pytest.raises(verifier.DesktopEvidenceError, match="Authenticode"):
        verifier._validate_webview_state(bad_signer, "before")

    wrong_subject = _webview_present()
    wrong_subject["signer"] = {
        "status": "Valid",
        "subject": "CN=Attacker",
        "certificate_sha256": DIGEST,
    }
    with pytest.raises(verifier.DesktopEvidenceError, match="Authenticode"):
        verifier._validate_webview_state(wrong_subject, "before")

    bad_certificate = _webview_present()
    bad_certificate["signer"] = {
        "status": "Valid",
        "subject": "CN=Microsoft Corporation",
        "certificate_sha256": "INVALID",
    }
    with pytest.raises(verifier.DesktopEvidenceError, match="SHA-256"):
        verifier._validate_webview_state(bad_certificate, "before")

    fabricated_absence = _webview_absent()
    fabricated_absence["version"] = "120.0.2210.91"
    with pytest.raises(verifier.DesktopEvidenceError, match="fabricated"):
        verifier._validate_webview_state(fabricated_absence, "before")

    unsupported_state = _webview_absent()
    unsupported_state["state"] = "unknown"
    with pytest.raises(verifier.DesktopEvidenceError, match="unsupported"):
        verifier._validate_webview_state(unsupported_state, "before")

    expanded = _webview_present()
    expanded["private_path"] = "secret"
    with pytest.raises(verifier.DesktopEvidenceError, match="fields are not closed"):
        verifier._validate_webview_state(expanded, "before")


def test_network_validator_covers_primary_fallback_and_offline_paths() -> None:
    normal_assignment = {
        "network": {
            "profile": "normal",
            "policy_sha256": DIGEST,
            "expected_provider": "akshare",
        },
        "data_path": "primary",
    }
    normal = _network(
        profile="normal",
        policy_sha256=DIGEST,
        records=[
            _network_record("akshare", "catalog", "success", payload=True),
            _network_record("akshare", "daily-bars", "success", payload=True),
        ],
    )
    assert (
        verifier._validate_network(normal, assignment=normal_assignment, success=True)
        == normal
    )

    fallback_assignment = {
        "network": {
            "profile": "primary-blocked",
            "policy_sha256": "d" * 64,
            "expected_provider": "baostock",
        },
        "data_path": "primary-blocked-fallback",
    }
    fallback = _network(
        profile="primary-blocked",
        policy_sha256="d" * 64,
        records=[
            _network_record("akshare", "catalog", "blocked-by-policy"),
            _network_record("baostock", "catalog", "success", payload=True),
            _network_record("baostock", "daily-bars", "success", payload=True),
        ],
    )
    verifier._validate_network(fallback, assignment=fallback_assignment, success=True)

    offline_assignment = {
        "network": {
            "profile": "webview-offline-fixed",
            "policy_sha256": "e" * 64,
            "expected_provider": "none",
        },
        "data_path": "primary",
    }
    offline = _network(
        profile="webview-offline-fixed",
        policy_sha256="e" * 64,
        records=[_network_record("webview2", "webview-runtime", "offline-failure")],
    )
    verifier._validate_network(offline, assignment=offline_assignment, success=False)

    wrong_boundary = {**normal, "capture_api": "browser-log"}
    with pytest.raises(verifier.DesktopEvidenceError, match="no-telemetry policy"):
        verifier._validate_network(
            wrong_boundary, assignment=normal_assignment, success=True
        )

    bad_tls = copy.deepcopy(normal)
    bad_tls["records"][0]["tls_system_validation"] = False  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="system TLS"):
        verifier._validate_network(bad_tls, assignment=normal_assignment, success=True)

    expanded_record = copy.deepcopy(normal)
    expanded_record["records"][0]["authorization"] = "secret"  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="fields are not closed"):
        verifier._validate_network(
            expanded_record, assignment=normal_assignment, success=True
        )

    missing_payload = copy.deepcopy(normal)
    del missing_payload["records"][0]["payload_sha256"]  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="invalid"):
        verifier._validate_network(
            missing_payload, assignment=normal_assignment, success=True
        )

    incomplete = copy.deepcopy(normal)
    incomplete["records"] = incomplete["records"][:1]  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="catalog and bars"):
        verifier._validate_network(
            incomplete, assignment=normal_assignment, success=True
        )

    no_primary_block = copy.deepcopy(fallback)
    no_primary_block["records"] = no_primary_block["records"][1:]  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="AKShare block"):
        verifier._validate_network(
            no_primary_block, assignment=fallback_assignment, success=True
        )

    missing_offline_failure = {**offline, "records": []}
    with pytest.raises(verifier.DesktopEvidenceError, match="offline failure"):
        verifier._validate_network(
            missing_offline_failure, assignment=offline_assignment, success=False
        )


def test_journey_validator_covers_primary_fallback_and_budget_boundaries() -> None:
    primary = _journey(fallback=False)
    fallback = _journey(fallback=True)
    assert verifier._validate_journey(primary, data_path="primary") == primary
    assert (
        verifier._validate_journey(fallback, data_path="primary-blocked-fallback")
        == fallback
    )

    for elapsed in (True, 0, 181):
        invalid = copy.deepcopy(primary)
        invalid["elapsed_seconds"] = elapsed
        with pytest.raises(verifier.DesktopEvidenceError, match="180-second"):
            verifier._validate_journey(invalid, data_path="primary")

    too_many_clicks = copy.deepcopy(primary)
    too_many_clicks["primary_click_count"] = 6
    with pytest.raises(verifier.DesktopEvidenceError, match="five primary clicks"):
        verifier._validate_journey(too_many_clicks, data_path="primary")

    skipped_step = copy.deepcopy(primary)
    skipped_step["onboarding_steps"] = ["welcome"]
    with pytest.raises(verifier.DesktopEvidenceError, match="four-step"):
        verifier._validate_journey(skipped_step, data_path="primary")

    wrong_instrument = copy.deepcopy(primary)
    wrong_instrument["instrument"]["symbol"] = "600000.SH"  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="Shanghai Composite"):
        verifier._validate_journey(wrong_instrument, data_path="primary")

    bad_source = copy.deepcopy(primary)
    bad_source["source"]["catalog_sha256"] = "INVALID"  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="SHA-256"):
        verifier._validate_journey(bad_source, data_path="primary")

    contradictory_primary = copy.deepcopy(primary)
    contradictory_primary["fallback"]["fallback_used"] = True  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="normal provider"):
        verifier._validate_journey(contradictory_primary, data_path="primary")

    contradictory_fallback = copy.deepcopy(fallback)
    contradictory_fallback["fallback"]["whole_segment"] = False  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="whole segment"):
        verifier._validate_journey(
            contradictory_fallback, data_path="primary-blocked-fallback"
        )


def test_cli_parser_and_main_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = _main_args(tmp_path)
    calls: list[tuple[object, ...]] = []

    def succeed(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append((*args, kwargs))
        return {"status": "accepted"}

    monkeypatch.setattr(verifier, "verify_matrix", succeed)
    assert verifier.main(arguments) == 0
    assert len(calls) == 1

    invalid_sha = list(arguments)
    invalid_sha[invalid_sha.index("--source-sha") + 1] = "INVALID"
    assert verifier.main(invalid_sha) == 1
    assert "raw evidence rejected" in capsys.readouterr().out

    def fail(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise OSError("simulated closed output boundary")

    monkeypatch.setattr(verifier, "verify_matrix", fail)
    assert verifier.main(arguments) == 1
    assert "simulated closed output boundary" in capsys.readouterr().out


def test_snapshot_policy_rejects_each_protected_assignment_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations: list[tuple[dict[str, object], str]] = []

    wrong_schema = copy.deepcopy(existing._policy())
    wrong_schema["schema"] = "unsupported"
    mutations.append((wrong_schema, "schema is unsupported"))

    open_broker = copy.deepcopy(existing._policy())
    open_broker["broker"]["raw_only"] = False  # type: ignore[index]
    mutations.append((open_broker, "broker policy is not fail closed"))

    invalid_case = copy.deepcopy(existing._policy())
    invalid_case["assignments"][0]["case_id"] = "attacker-case"  # type: ignore[index]
    mutations.append((invalid_case, "identity is invalid or duplicated"))

    mismatched_case = copy.deepcopy(existing._policy())
    mismatched_case["assignments"][0]["guest_profile"] = "win11"  # type: ignore[index]
    mutations.append((mismatched_case, "identity mismatch"))

    unsupported_os = copy.deepcopy(existing._policy())
    unsupported_os["assignments"][0]["system"]["architecture"] = "arm64"  # type: ignore[index]
    mutations.append((unsupported_os, "unsupported OS assignment"))

    wrong_webview = copy.deepcopy(existing._policy())
    wrong_webview["assignments"][0]["webview_initial_state"] = "present"  # type: ignore[index]
    mutations.append((wrong_webview, "WebView2 initial state mismatch"))

    wrong_injection = copy.deepcopy(existing._policy())
    wrong_injection["assignments"][-1]["failure_injection"]["identity"] = (  # type: ignore[index]
        "unreviewed-injection"
    )
    mutations.append((wrong_injection, "failure injection identity is not fixed"))

    unexpected_injection = copy.deepcopy(existing._policy())
    unexpected_injection["assignments"][0]["failure_injection"] = {  # type: ignore[index]
        "identity": "stock-desk-webview2-offline-install-failure-v1",
        "sha256": DIGEST,
    }
    mutations.append((unexpected_injection, "unexpected failure injection"))

    admin_account = copy.deepcopy(existing._policy())
    admin_account["assignments"][0]["account"]["is_admin"] = True  # type: ignore[index]
    mutations.append((admin_account, "Chinese-name standard user"))

    wrong_sizes = copy.deepcopy(existing._policy())
    wrong_sizes["assignments"][0]["logical_window_sizes"] = [  # type: ignore[index]
        {"width": 1366, "height": 768}
    ]
    mutations.append((wrong_sizes, "logical window matrix mismatch"))

    wrong_network = copy.deepcopy(existing._policy())
    wrong_network["assignments"][0]["network"]["expected_provider"] = "none"  # type: ignore[index]
    mutations.append((wrong_network, "network assignment"))

    for policy, message in mutations:
        with pytest.raises(verifier.DesktopEvidenceError, match=message):
            verifier.validate_snapshot_policy(policy)

    expanded_cases = (*verifier.expected_case_ids(), "win12-dpi-100")
    monkeypatch.setattr(verifier, "expected_case_ids", lambda: expanded_cases)
    with pytest.raises(verifier.DesktopEvidenceError, match="exact 11-case matrix"):
        verifier.validate_snapshot_policy(existing._policy())


def test_layout_check_rejects_each_raw_geometry_and_keyboard_contradiction() -> None:
    mutations: list[tuple[dict[str, object], str]] = []

    malformed_window = existing._layout()
    malformed_window["window_bounds"]["x"] = "zero"  # type: ignore[index]
    mutations.append((malformed_window, "malformed window geometry"))

    empty_window = existing._layout()
    empty_window["window_bounds"]["width"] = 0  # type: ignore[index]
    mutations.append((empty_window, "empty window geometry"))

    no_components = existing._layout()
    no_components["component_bounds"] = []
    mutations.append((no_components, "no component geometry"))

    duplicate = existing._layout()
    duplicate["component_bounds"][1]["id"] = "first"  # type: ignore[index]
    mutations.append((duplicate, "duplicates a component identity"))

    malformed_component = existing._layout()
    malformed_component["component_bounds"][0]["x"] = "ten"  # type: ignore[index]
    mutations.append((malformed_component, "malformed component geometry"))

    empty_component = existing._layout()
    empty_component["component_bounds"][0]["width"] = 0  # type: ignore[index]
    mutations.append((empty_component, "empty component geometry"))

    bad_parent = existing._layout()
    bad_parent["component_bounds"][0]["parent_id"] = 7  # type: ignore[index]
    mutations.append((bad_parent, "parent identity is malformed"))

    clipped = existing._layout()
    clipped["component_bounds"][0]["x"] = -1  # type: ignore[index]
    mutations.append((clipped, "clipping or peer overlap"))

    bad_tab_set = existing._layout()
    bad_tab_set["tab_sequence"] = ["first"]
    mutations.append((bad_tab_set, "cover each focusable control once"))

    wrong_tab_order = existing._layout()
    wrong_tab_order["tab_sequence"] = ["second", "first"]
    mutations.append((wrong_tab_order, "differs from visual order"))

    invisible_focus = existing._layout()
    invisible_focus["focus_visible"] = False
    mutations.append((invisible_focus, "failed focus or safe Esc"))

    for layout, message in mutations:
        with pytest.raises(verifier.DesktopEvidenceError, match=message):
            verifier._validate_layout_check(layout, label="coverage layout")

    separate_parents = existing._layout()
    separate_parents["component_bounds"][1]["parent_id"] = "other-root"  # type: ignore[index]
    assert verifier._validate_layout_check(
        separate_parents, label="coverage layout"
    ) == (640, 360)


def test_uia_summary_rejects_each_matrix_and_focus_manifest_contradiction() -> None:
    mutations: list[tuple[dict[str, object], str]] = []

    wrong_driver = existing._uia_matrix()
    wrong_driver["driver_sha256"] = "9" * 64
    mutations.append((wrong_driver, "driver identity"))

    duplicate_route = existing._uia_matrix()
    duplicate_route["routes"].append(copy.deepcopy(duplicate_route["routes"][0]))  # type: ignore[union-attr,index]
    mutations.append((duplicate_route, "route is duplicated"))

    incomplete_route = existing._uia_matrix()
    incomplete_route["routes"][0]["checks"] = incomplete_route["routes"][0][  # type: ignore[index]
        "checks"
    ][:1]
    mutations.append((incomplete_route, "route size matrix is incomplete"))

    missing_route = existing._uia_matrix()
    missing_route["routes"].pop()  # type: ignore[union-attr]
    mutations.append((missing_route, "all six core routes"))

    duplicate_dialog = existing._uia_matrix()
    duplicate_dialog["dialogs"].append(  # type: ignore[union-attr]
        copy.deepcopy(duplicate_dialog["dialogs"][0])  # type: ignore[index]
    )
    mutations.append((duplicate_dialog, "dialog is duplicated"))

    incomplete_dialog = existing._uia_matrix()
    incomplete_dialog["dialogs"][0]["checks"] = incomplete_dialog["dialogs"][0][  # type: ignore[index]
        "checks"
    ][:1]
    mutations.append((incomplete_dialog, "dialog size matrix is incomplete"))

    missing_dialog = existing._uia_matrix()
    missing_dialog["dialogs"].pop()  # type: ignore[union-attr]
    mutations.append((missing_dialog, "release-relevant dialog"))

    short_onboarding = existing._uia_matrix()
    short_onboarding["keyboard"]["onboarding_tab_paths"].pop()  # type: ignore[index]
    mutations.append((short_onboarding, "exactly four"))

    oversized_sheet = existing._uia_matrix()
    oversized_sheet["focus_regions"]["width"] = 2049  # type: ignore[index]
    mutations.append((oversized_sheet, "contact sheet contract"))

    wrong_capture_count = existing._uia_matrix()
    wrong_capture_count["focus_regions"]["captures"].pop()  # type: ignore[index]
    mutations.append((wrong_capture_count, "capture count"))

    contradictory_capture = existing._uia_matrix()
    contradictory_capture["focus_regions"]["captures"][0]["x"] = 1  # type: ignore[index]
    mutations.append((contradictory_capture, "capture layout"))

    undeclared_pixels = existing._uia_matrix()
    undeclared_pixels["focus_regions"]["height"] += 1  # type: ignore[index,operator]
    mutations.append((undeclared_pixels, "undeclared pixels"))

    mismatched_regions = existing._uia_matrix()
    mismatched_regions["focus_regions"]["captures"][-1]["id"] = (  # type: ignore[index]
        "unexpected-region"
    )
    mutations.append((mismatched_regions, "differs from UIA focus evidence"))

    invalid_sidebar = existing._uia_matrix()
    invalid_sidebar["narrow_sidebar"]["expanded_reflow"] = False  # type: ignore[index]
    mutations.append((invalid_sidebar, "semantic icon control"))

    for uia, message in mutations:
        with pytest.raises(verifier.DesktopEvidenceError, match=message):
            verifier._validate_uia(uia, expected_driver_sha256="5" * 64)


def test_uia_raw_records_reject_each_trace_and_pixel_contradiction() -> None:
    uia = existing._uia_matrix()
    actions = _uia_actions()
    trees = _uia_trees(uia)
    contact_sheet = existing._focus_contact_sheet(uia, flatten_first_pair=False)

    with pytest.raises(verifier.DesktopEvidenceError, match="not valid JSON"):
        verifier._validate_uia_raw_records(
            b"{",
            json.dumps(trees).encode(),
            contact_sheet,
            uia=uia,
            expected_primary_actions=4,
        )

    invalid_action = copy.deepcopy(actions)
    invalid_action[0]["sequence"] = 2
    with pytest.raises(verifier.DesktopEvidenceError, match="input method"):
        verifier._validate_uia_raw_records(
            json.dumps(invalid_action).encode(),
            json.dumps(trees).encode(),
            contact_sheet,
            uia=uia,
            expected_primary_actions=4,
        )

    malformed_outcome = copy.deepcopy(actions)
    malformed_outcome[0]["major_click"] = "yes"
    with pytest.raises(verifier.DesktopEvidenceError, match="outcome is malformed"):
        verifier._validate_uia_raw_records(
            json.dumps(malformed_outcome).encode(),
            json.dumps(trees).encode(),
            contact_sheet,
            uia=uia,
            expected_primary_actions=4,
        )

    with pytest.raises(verifier.DesktopEvidenceError, match="action budget"):
        verifier._validate_uia_raw_records(
            json.dumps(actions).encode(),
            json.dumps(trees).encode(),
            contact_sheet,
            uia=uia,
            expected_primary_actions=5,
        )

    duplicate_trees = [*trees, copy.deepcopy(trees[0])]
    with pytest.raises(verifier.DesktopEvidenceError, match="identity is duplicated"):
        verifier._validate_uia_raw_records(
            json.dumps(actions).encode(),
            json.dumps(duplicate_trees).encode(),
            contact_sheet,
            uia=uia,
            expected_primary_actions=4,
        )

    with pytest.raises(verifier.DesktopEvidenceError, match="differs from"):
        verifier._validate_uia_raw_records(
            json.dumps(actions).encode(),
            json.dumps(trees[:-1]).encode(),
            contact_sheet,
            uia=uia,
            expected_primary_actions=4,
        )

    wrong_size_sheet = _png_bytes((10, 10))
    with pytest.raises(verifier.DesktopEvidenceError, match="valid bounded PNG"):
        verifier._validate_uia_raw_records(
            json.dumps(actions).encode(),
            json.dumps(trees).encode(),
            wrong_size_sheet,
            uia=uia,
            expected_primary_actions=4,
        )

    focus_manifest = uia["focus_regions"]  # type: ignore[assignment]
    degenerate = Image.new(
        "RGB", (focus_manifest["width"], focus_manifest["height"]), "black"
    )
    output = io.BytesIO()
    degenerate.save(output, format="PNG")
    with pytest.raises(verifier.DesktopEvidenceError, match="valid bounded PNG"):
        verifier._validate_uia_raw_records(
            json.dumps(actions).encode(),
            json.dumps(trees).encode(),
            output.getvalue(),
            uia=uia,
            expected_primary_actions=4,
        )

    missing_region = copy.deepcopy(uia)
    missing_region["routes"][0]["checks"][0]["focus_evidence"][  # type: ignore[index]
        "unfocused_region_id"
    ] = "missing-region"
    with pytest.raises(verifier.DesktopEvidenceError, match="undeclared raw region"):
        verifier._validate_uia_raw_records(
            json.dumps(actions).encode(),
            json.dumps(_uia_trees(missing_region)).encode(),
            contact_sheet,
            uia=missing_region,
            expected_primary_actions=4,
        )


def test_raw_record_inventory_rejects_each_package_boundary(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    files = {
        "observation-stream": (raw / "observations.jsonl", "application/x-ndjson"),
        "install-log": (raw / "install.log", "text/plain; charset=utf-8"),
        "failure-diagnostic": (
            raw / "failure-diagnostic.txt",
            "text/plain; charset=utf-8",
        ),
        "uia-action-trace": (raw / "actions.json", "application/json"),
        "uia-tree": (raw / "tree.json", "application/json"),
    }
    for path, _media in files.values():
        path.write_text("evidence\n", encoding="utf-8")

    def record(kind: str) -> dict[str, object]:
        path, media = files[kind]
        data = path.read_bytes()
        return {
            "kind": kind,
            "path": f"raw/{path.name}",
            "sha256": verifier._sha256(data),
            "size_bytes": len(data),
            "media_type": media,
        }

    valid = {
        "records": [
            record("observation-stream"),
            record("install-log"),
            record("failure-diagnostic"),
        ]
    }

    with pytest.raises(verifier.DesktopEvidenceError, match="record count"):
        verifier._records(tmp_path, {"records": valid["records"][:2]})

    duplicate = copy.deepcopy(valid)
    duplicate["records"][2]["path"] = duplicate["records"][1]["path"]  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="unsafe or duplicated"):
        verifier._records(tmp_path, duplicate)

    missing_file = copy.deepcopy(valid)
    missing_file["records"][2]["path"] = "raw/missing.txt"  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="escapes its package"):
        verifier._records(tmp_path, missing_file)

    wrong_digest = copy.deepcopy(valid)
    wrong_digest["records"][2]["sha256"] = DIGEST  # type: ignore[index]
    with pytest.raises(verifier.DesktopEvidenceError, match="do not match"):
        verifier._records(tmp_path, wrong_digest)

    no_mandatory_roles = {
        "records": [
            record("failure-diagnostic"),
            record("uia-action-trace"),
            record("uia-tree"),
        ]
    }
    with pytest.raises(verifier.DesktopEvidenceError, match="mandatory"):
        verifier._records(tmp_path, no_mandatory_roles)
