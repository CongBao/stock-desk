from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from stock_desk.api.diagnostics import router
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.desktop_session import (
    DesktopSession,
    DesktopSessionMiddleware,
    TAURI_WINDOWS_ORIGIN,
)
from stock_desk.diagnostics.models import (
    DiagnosticConfiguration,
    DiagnosticEventCode,
    DiagnosticEventSink,
    DiagnosticSnapshotService,
)
from stock_desk.storage.database import migrate


SECRET = "desktop-diagnostic-session-secret-value"


def _synthetic_windows_home(user: str, suffix: str) -> str:
    return "C:\\" + "Users\\" + user + suffix


def _client(
    service_provider: Callable[[], DiagnosticSnapshotService] | None = None,
) -> TestClient:
    session = DesktopSession(
        origin=TAURI_WINDOWS_ORIGIN,
        secret=SECRET,
        host_version="1.1.0",
        frontend_version="1.1.0",
        sidecar_version="1.1.0",
        source_revision="a" * 40,
    )
    service = DiagnosticSnapshotService(
        version="1.1.0",
        source_revision="a" * 40,
        configuration_provider=lambda: DiagnosticConfiguration(available=True),
        health_provider=lambda: (True, True),
        clock=lambda: datetime(2026, 7, 13, tzinfo=timezone.utc),
        platform_system="Windows",
        platform_machine="AMD64",
    )
    app = FastAPI()
    app.state.diagnostic_snapshot_service_provider = service_provider or (
        lambda: service
    )
    app.include_router(router, prefix="/api")
    app.add_middleware(DesktopSessionMiddleware, session=session)
    return TestClient(app)


def test_snapshot_is_post_only_and_requires_exact_desktop_authority() -> None:
    with _client() as client:
        assert client.get("/api/v1/diagnostics/snapshot").status_code == 403
        assert client.post("/api/v1/diagnostics/snapshot").status_code == 403
        headers = {
            "Origin": TAURI_WINDOWS_ORIGIN,
            "Authorization": f"Bearer {SECRET}",
        }
        assert (
            client.get("/api/v1/diagnostics/snapshot", headers=headers).status_code
            == 405
        )
        response = client.post("/api/v1/diagnostics/snapshot", headers=headers)

    assert response.status_code == 200
    assert response.json()["schema_version"] == "stock-desk-diagnostic-snapshot-v1"
    assert SECRET not in response.text
    assert response.headers["access-control-allow-origin"] == TAURI_WINDOWS_ORIGIN


def test_snapshot_failure_returns_only_a_stable_code() -> None:
    def fail() -> DiagnosticSnapshotService:
        raise RuntimeError("private " + _synthetic_windows_home("Bao", r"\token"))

    client = _client(fail)
    with client:
        response = client.post(
            "/api/v1/diagnostics/snapshot",
            headers={
                "Origin": TAURI_WINDOWS_ORIGIN,
                "Authorization": f"Bearer {SECRET}",
            },
        )
    assert response.status_code == 503
    assert response.json() == {"code": "diagnostic_snapshot_unavailable"}
    assert "Users" not in response.text
    assert "private" not in response.text


def test_main_registers_diagnostics_only_for_the_authenticated_desktop() -> None:
    service = DiagnosticSnapshotService(
        version="1.1.0",
        source_revision="a" * 40,
        configuration_provider=lambda: DiagnosticConfiguration(available=True),
        health_provider=lambda: (True, True),
        platform_system="Windows",
        platform_machine="AMD64",
    )
    session = DesktopSession(
        origin=TAURI_WINDOWS_ORIGIN,
        secret=SECRET,
        host_version="1.1.0",
        frontend_version="1.1.0",
        sidecar_version="1.1.0",
        source_revision="a" * 40,
    )
    browser_paths = create_app().openapi()["paths"]
    desktop_paths = create_app(
        desktop_session=session,
        diagnostic_snapshot_service=service,
    ).openapi()["paths"]
    assert "/api/v1/diagnostics/snapshot" not in browser_paths
    assert "/api/v1/diagnostics/snapshot" in desktop_paths


def test_production_snapshot_uses_shared_sink_and_exports_lifecycle_events(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'diagnostics.db'}"
    migrate(database_url)
    sink = DiagnosticEventSink()
    sink.emit(DiagnosticEventCode.SIDECAR_STARTING)
    session = DesktopSession(
        origin=TAURI_WINDOWS_ORIGIN,
        secret=SECRET,
        host_version="1.1.0",
        frontend_version="1.1.0",
        sidecar_version="1.1.0",
        source_revision="a" * 40,
    )
    application = create_app(
        Settings(database_url=database_url, data_dir=tmp_path),
        desktop_session=session,
        diagnostic_event_sink=sink,
    )

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/diagnostics/snapshot",
            headers={
                "Origin": TAURI_WINDOWS_ORIGIN,
                "Authorization": f"Bearer {SECRET}",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    event_codes = [event["event_code"] for event in payload["events"]]
    assert event_codes[0] == "sidecar.starting"
    assert "sidecar.api_configured" in event_codes
    assert "storage.ready" in event_codes
    assert "worker.unavailable" in event_codes
    assert payload["failure_ids"] == ["market_worker_unavailable"]
    assert SECRET not in response.text
