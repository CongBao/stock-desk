from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, cast

import pytest

from stock_desk.diagnostics.models import (
    DiagnosticConfiguration,
    DiagnosticEvent,
    DiagnosticEventBuffer,
    DiagnosticEventCode,
    DiagnosticEventLevel,
    DiagnosticEventSink,
    DiagnosticModelProvider,
    DiagnosticSnapshotService,
)
from stock_desk.diagnostics.redaction import (
    DiagnosticSafetyError,
    REDACTED,
    assert_safe_bytes,
    sanitize_tree,
)


NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def _windows_user_path(user: str, suffix: str) -> str:
    """Build synthetic private paths without publishing a home-path literal."""
    return "C:\\" + "Users\\" + user + suffix


ALLOWED = frozenset(
    {
        "configuration",
        "events",
        "name",
        "nested",
        "token",
        "authorization",
        "private_prompt",
        "username",
        "path",
    }
)


def test_allowlisted_recursive_redaction_removes_all_private_classes() -> None:
    secret = "sk-private-diagnostic-value-1234567890"
    username = "包"
    hostname = "DESKTOP-PRIVATE"
    raw = {
        "configuration": {
            "token": secret,
            "nested": [
                {"authorization": f"Bearer {secret}"},
                {"private_prompt": "my private investing prompt"},
                {"username": username},
                {"path": _windows_user_path(username, r"\Stock Desk\config")},
                {"name": hostname},
            ],
        },
        "events": [],
    }

    cleaned = sanitize_tree(
        raw,
        allowed_keys=ALLOWED,
        secrets=(secret,),
        private_identities=(username, hostname),
    )
    rendered = json.dumps(cleaned, ensure_ascii=False)

    assert secret not in rendered
    assert username not in rendered
    assert hostname not in rendered
    assert rendered.count(REDACTED) >= 6
    assert_safe_bytes(
        rendered.encode(),
        secrets=(secret,),
        private_identities=(username, hostname),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": "ordinary"},
        {"events": {1: "invalid key"}},
        {"events": object()},
        {"events": float("nan")},
        {"events": "x" * 8_193},
    ],
)
def test_unknown_or_unserializable_diagnostic_material_fails_closed(
    payload: object,
) -> None:
    with pytest.raises(DiagnosticSafetyError):
        sanitize_tree(payload, allowed_keys=ALLOWED)


def test_cycles_and_excessive_depth_fail_closed_without_partial_output() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(DiagnosticSafetyError):
        sanitize_tree({"events": cyclic}, allowed_keys=ALLOWED)

    nested: object = "leaf"
    for _ in range(18):
        nested = [nested]
    with pytest.raises(DiagnosticSafetyError):
        sanitize_tree({"events": nested}, allowed_keys=ALLOWED)


def test_final_byte_scan_rejects_unredacted_paths_credentials_and_invalid_utf8() -> (
    None
):
    unsafe = (
        b'{"path":"'
        + _windows_user_path("PrivateUser", r"\file").encode()
        + b'","authorization":"Bearer unsafe"}'
    )
    with pytest.raises(DiagnosticSafetyError):
        assert_safe_bytes(unsafe)
    with pytest.raises(DiagnosticSafetyError):
        assert_safe_bytes(b"\xff")


def test_snapshot_is_fixed_schema_bounded_and_contains_no_free_form_log_message() -> (
    None
):
    events = DiagnosticEventBuffer(maximum=2)
    events.append(
        DiagnosticEvent(
            timestamp=NOW,
            level=DiagnosticEventLevel.ERROR,
            component="sidecar",
            event_code="sidecar.storage_failed",
            failure_id="storage_unavailable",
        )
    )
    service = DiagnosticSnapshotService(
        version="1.1.0",
        source_revision="a" * 40,
        configuration_provider=lambda: DiagnosticConfiguration(
            available=True,
            daily_sources=("akshare", "baostock"),
            weekly_sources=("akshare",),
            minute_sources=("baostock",),
            instrument_sources=("akshare",),
            tushare_token_configured=True,
            local_tdx_configured=True,
            model_providers=(DiagnosticModelProvider.DEEPSEEK,),
        ),
        health_provider=lambda: (True, True),
        event_buffer=events,
        clock=lambda: NOW,
        platform_system="Windows",
        platform_machine="AMD64",
    )

    snapshot = service.snapshot()
    payload = snapshot.model_dump_json().encode()
    assert snapshot.failure_ids == ("storage_unavailable",)
    assert snapshot.privacy.telemetry_enabled is False
    assert snapshot.privacy.automatic_crash_upload is False
    assert snapshot.privacy.automatic_diagnostic_upload is False
    assert snapshot.privacy.stable_device_identifier is False
    assert b"message" not in payload
    assert b"hostname" not in payload
    assert b"path" not in payload
    assert_safe_bytes(payload)


def test_snapshot_dependency_failure_degrades_to_safe_identifiers() -> None:
    def fail_configuration() -> DiagnosticConfiguration:
        raise RuntimeError("secret " + _windows_user_path("Bao", r"\private"))

    service = DiagnosticSnapshotService(
        version="1.1.0",
        source_revision=None,
        configuration_provider=fail_configuration,
        health_provider=lambda: (_ for _ in ()).throw(RuntimeError("private")),
        clock=lambda: NOW,
        platform_system="Darwin",
        platform_machine="arm64",
    )
    payload = service.snapshot().model_dump_json()
    assert "private" not in payload
    assert "Users" not in payload
    assert "diagnostic_configuration_unavailable" in payload
    assert "diagnostic_health_unavailable" in payload


def test_production_event_sink_is_bounded_and_rejects_unstructured_material() -> None:
    buffer = DiagnosticEventBuffer(maximum=2)
    sink = DiagnosticEventSink(event_buffer=buffer, clock=lambda: NOW)
    sink.emit(DiagnosticEventCode.SIDECAR_STARTING)
    sink.emit(DiagnosticEventCode.STORAGE_UNAVAILABLE)
    sink.emit(DiagnosticEventCode.WORKER_RUNTIME_FAILED)

    events = buffer.snapshot()
    assert [event.event_code for event in events] == [
        "storage.unavailable",
        "worker.runtime_failed",
    ]
    assert [event.failure_id for event in events] == [
        "storage_unavailable",
        "market_worker_unavailable",
    ]

    private_material = (
        "Bearer private-token",
        _windows_user_path("PrivateUser", r"\Stock Desk"),
        "private investing prompt",
        "PrivateUser",
    )
    for value in private_material:
        with pytest.raises(TypeError):
            sink.emit(cast(Any, value))

    rendered = json.dumps(
        [event.model_dump(mode="json") for event in buffer.snapshot()],
        ensure_ascii=False,
        default=str,
    )
    assert all(value not in rendered for value in private_material)
