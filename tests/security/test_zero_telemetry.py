from __future__ import annotations

from pathlib import Path
import socket

import pytest

from scripts.verify_zero_telemetry import (
    MANIFESTS,
    PRODUCTION_ROOTS,
    ZeroTelemetryError,
    audit_repository,
    verify_repository,
)
from stock_desk.diagnostics.models import (
    DiagnosticConfiguration,
    DiagnosticSnapshotService,
)


ROOT = Path(__file__).resolve().parents[2]


def _minimal_repository(root: Path) -> None:
    for relative in MANIFESTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# locked dependency input\n", encoding="utf-8")
    for relative in PRODUCTION_ROOTS:
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / "safe.py").write_text("VALUE = 'local-only'\n", encoding="utf-8")


def test_current_locked_application_has_no_telemetry_or_crash_sdk() -> None:
    assert audit_repository(ROOT) == ()


@pytest.mark.parametrize(
    ("relative", "payload", "expected"),
    [
        ("pyproject.toml", 'sentry-sdk = "1"', "telemetry-sdk"),
        ("web/src/hidden.ts", 'fetch("https://app.posthog.com/capture")', "telemetry"),
        ("src-tauri/Cargo.toml", 'opentelemetry = "1"', "telemetry-sdk"),
    ],
)
def test_hidden_sdk_or_collection_endpoint_fails_static_policy(
    tmp_path: Path,
    relative: str,
    payload: str,
    expected: str,
) -> None:
    _minimal_repository(tmp_path)
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ZeroTelemetryError, match=expected):
        verify_repository(tmp_path)


def test_missing_manifest_or_source_symlink_fails_closed(tmp_path: Path) -> None:
    _minimal_repository(tmp_path)
    (tmp_path / "uv.lock").unlink()
    linked = tmp_path / "src/stock_desk/linked.py"
    linked.symlink_to(tmp_path / "outside.py")

    violations = audit_repository(tmp_path)
    assert "missing-or-unsafe-manifest:uv.lock" in violations
    assert "unsafe-source-link:src/stock_desk/linked.py" in violations


def test_snapshot_and_failure_collection_make_no_network_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[object] = []

    def reject_connect(_socket: socket.socket, address: object) -> None:
        attempts.append(address)
        raise AssertionError("diagnostic collection attempted network access")

    monkeypatch.setattr(socket.socket, "connect", reject_connect)

    service = DiagnosticSnapshotService(
        version="1.1.0",
        source_revision="a" * 40,
        configuration_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("local configuration failure")
        ),
        health_provider=lambda: (_ for _ in ()).throw(
            RuntimeError("local health failure")
        ),
        platform_system="Windows",
        platform_machine="AMD64",
    )
    snapshot = service.snapshot()

    assert attempts == []
    assert snapshot.configuration == DiagnosticConfiguration(available=False)
    assert snapshot.privacy.telemetry_enabled is False
    assert snapshot.privacy.automatic_crash_upload is False
    assert snapshot.privacy.automatic_diagnostic_upload is False
