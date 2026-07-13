import secrets
from datetime import date
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.desktop_session import DesktopLifecycleController, DesktopSession
from stock_desk.formula.service import MACD_TEMPLATE_SOURCE
from stock_desk.main import create_app
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker
from tests.integration.market.lake_test_helpers import routed_daily_bars


TAURI_ORIGIN = "http://tauri.localhost"
SOURCE_REVISION = "a" * 40


def _session() -> DesktopSession:
    return DesktopSession(
        origin=TAURI_ORIGIN,
        secret=secrets.token_urlsafe(32),
        host_version="1.1.0",
        frontend_version="1.1.0",
        sidecar_version="1.1.0",
        source_revision=SOURCE_REVISION,
    )


def _headers(session: DesktopSession) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {session.secret_for_host()}",
        "Origin": session.origin,
    }


def _repository(tmp_path: Path) -> TaskRepository:
    url = f"sqlite:///{tmp_path / 'desktop-session.db'}"
    migrate(url)
    return TaskRepository(create_engine_for_url(url), owns_engine=True)


def test_desktop_session_accepts_only_the_exact_origin_and_bearer_secret() -> None:
    session = _session()
    with TestClient(create_app(desktop_session=session)) as client:
        response = client.get("/api/health", headers=_headers(session))

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TAURI_ORIGIN
    assert response.headers["vary"] == "Origin"


def test_desktop_session_rejects_missing_and_wrong_credentials_without_leaking() -> (
    None
):
    session = _session()
    candidates = (
        {"Origin": TAURI_ORIGIN},
        {"Origin": TAURI_ORIGIN, "Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer {session.secret_for_host()}"},
        {
            "Origin": "http://evil.invalid",
            "Authorization": f"Bearer {session.secret_for_host()}",
        },
    )

    with TestClient(create_app(desktop_session=session)) as client:
        responses = [
            client.get("/api/health", headers=headers) for headers in candidates
        ]

    assert [response.status_code for response in responses] == [401, 401, 403, 403]
    serialized = "\n".join(response.text for response in responses)
    assert session.secret_for_host() not in serialized
    assert "evil.invalid" not in serialized
    assert "traceback" not in serialized.casefold()


def test_formula_studio_requires_desktop_authority_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    session = _session()
    database_url = f"sqlite:///{tmp_path / 'desktop-formula.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)))
    stored = services.lake.write(routed)
    settings = Settings(database_url=database_url, data_dir=tmp_path)
    preview_path = "/api/formulas/not-authorized/preview"
    try:
        with TestClient(
            create_app(
                settings,
                market_services=services,
                desktop_session=session,
            )
        ) as client:
            unauthenticated = (
                client.get("/api/formulas/templates"),
                client.post("/api/formulas/validate", json={}),
                client.post("/api/formulas", json={}),
                client.post(preview_path, json={}),
            )
            assert [item.status_code for item in unauthenticated] == [403] * 4

            headers = _headers(session)
            validated = client.post(
                "/api/formulas/validate",
                headers=headers,
                json={
                    "source": MACD_TEMPLATE_SOURCE,
                    "parameter_schema": {},
                    "formula_type": "trading",
                },
            )
            assert validated.status_code == 200
            assert validated.json() == {"valid": True, "diagnostics": []}

            created = client.post(
                "/api/formulas",
                headers=headers,
                json={
                    "name": "Desktop authenticated MACD",
                    "formula_type": "trading",
                    "placement": "subchart",
                    "source": MACD_TEMPLATE_SOURCE,
                    "parameter_schema": {},
                },
            )
            assert created.status_code == 201
            version_id = created.json()["draft"]["executable_version_id"]
            query = routed.result.query
            preview = client.post(
                f"/api/formulas/{version_id}/preview",
                headers=headers,
                json={
                    "symbol": query.symbol,
                    "period": query.period.value,
                    "adjustment": query.adjustment.value,
                    "start": query.start.isoformat(),
                    "end": query.end.isoformat(),
                    "parameters": {},
                },
            )
    finally:
        services.close()

    assert preview.status_code == 200
    payload = preview.json()
    assert payload["formula_version_id"] == version_id
    assert payload["formula_checksum"].startswith("sha256:")
    assert payload["engine_version"]
    assert payload["compatibility_version"]
    assert payload["source"] == routed.result.provenance.source.value
    assert payload["dataset_version"] == routed.result.provenance.dataset_version
    assert payload["route_version"] == routed.manifest.route_version
    assert payload["manifest_record_id"] == stored.manifest_record_id
    assert session.secret_for_host() not in preview.text


def test_desktop_session_preflight_is_exact_and_rejects_header_expansion() -> None:
    session = _session()
    preflight = {
        "Origin": TAURI_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization, content-type",
    }
    expanded = {
        **preflight,
        "Access-Control-Request-Headers": "authorization, x-debug-token",
    }

    with TestClient(create_app(desktop_session=session)) as client:
        accepted = client.options("/api/tasks", headers=preflight)
        rejected = client.options("/api/tasks", headers=expanded)

    assert accepted.status_code == 204
    assert accepted.headers["access-control-allow-origin"] == TAURI_ORIGIN
    assert accepted.headers["access-control-allow-headers"] == (
        "Authorization, Content-Type"
    )
    assert "*" not in "\n".join(
        f"{key}: {value}" for key, value in accepted.headers.items()
    )
    assert rejected.status_code == 403
    assert rejected.json() == {"code": "desktop_origin_forbidden"}


def test_desktop_handshake_exposes_versions_and_revision_without_private_paths() -> (
    None
):
    session = _session()
    with TestClient(create_app(desktop_session=session)) as client:
        response = client.get("/api/desktop/handshake", headers=_headers(session))

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "api_version": "v1",
        "host_version": "1.1.0",
        "frontend_version": "1.1.0",
        "sidecar_version": "1.1.0",
        "source_revision": SOURCE_REVISION,
        "storage": "ready",
    }
    serialized = response.text
    assert session.secret_for_host() not in serialized
    assert "stock-desk.db" not in serialized
    assert "/Users/" not in serialized


def test_source_and_container_apps_remain_compatible_without_desktop_session() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/health")
        activity = client.get("/api/desktop/activity")
        shutdown = client.post("/api/desktop/shutdown")

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert activity.status_code == 404
    assert shutdown.status_code == 404


def test_desktop_activity_uses_authoritative_queued_and_running_metrics(
    tmp_path: Path,
) -> None:
    session = _session()
    repository = _repository(tmp_path)
    lifecycle = DesktopLifecycleController()
    queued = repository.create("demo.double", {"value": 1})
    running = repository.create("demo.double", {"value": 2})
    claimed = repository.claim_next("desktop-test-worker")
    assert claimed is not None and claimed.id == queued.id
    try:
        with TestClient(
            create_app(
                task_repository=repository,
                desktop_session=session,
                desktop_lifecycle=lifecycle,
            )
        ) as client:
            response = client.get("/api/desktop/activity", headers=_headers(session))

        assert response.status_code == 200
        assert response.json() == {"queued": 1, "running": 1}
        assert running.id not in response.text
        assert session.secret_for_host() not in response.text
    finally:
        repository.close()


def test_desktop_shutdown_rechecks_storage_and_refuses_active_tasks(
    tmp_path: Path,
) -> None:
    session = _session()
    repository = _repository(tmp_path)
    lifecycle = DesktopLifecycleController()
    queued = repository.create("demo.double", {"value": 1})
    running = repository.create("demo.double", {"value": 2})
    claimed = repository.claim_next("desktop-test-worker")
    assert claimed is not None and claimed.id == queued.id
    try:
        with TestClient(
            create_app(
                task_repository=repository,
                desktop_session=session,
                desktop_lifecycle=lifecycle,
            )
        ) as client:
            response = client.post(
                "/api/desktop/shutdown",
                headers=_headers(session),
                json={"queued": 0, "running": 0},
            )

        assert response.status_code == 409
        assert response.json() == {
            "code": "desktop_tasks_active",
            "queued": 1,
            "running": 1,
        }
        assert lifecycle.shutdown_requested is False
        assert running.id not in response.text
        assert session.secret_for_host() not in response.text
    finally:
        repository.close()


def test_desktop_shutdown_accepts_only_terminal_storage_and_signals_lifecycle(
    tmp_path: Path,
) -> None:
    session = _session()
    repository = _repository(tmp_path)
    lifecycle = DesktopLifecycleController()
    server = SimpleNamespace(should_exit=False)
    lifecycle.bind_server(server)
    succeeded = repository.create("demo.double", {"value": 1})
    claimed = repository.claim_next("desktop-test-worker")
    assert claimed is not None and claimed.id == succeeded.id
    repository.complete(succeeded.id, {"value": 2})
    repository.request_cancel(repository.create("demo.double", {"value": 3}).id)
    try:
        with TestClient(
            create_app(
                task_repository=repository,
                desktop_session=session,
                desktop_lifecycle=lifecycle,
            )
        ) as client:
            response = client.post("/api/desktop/shutdown", headers=_headers(session))
            assert server.should_exit is False
            assert lifecycle.shutdown_prepared is True
            assert lifecycle.claim_stop_event.is_set() is True
            assert lifecycle.shutdown_requested is False
            committed = client.post(
                "/api/desktop/shutdown/commit", headers=_headers(session)
            )

        assert response.status_code == 202
        assert response.json() == {"status": "shutdown_requested"}
        assert committed.status_code == 202
        assert committed.json() == {"status": "shutdown_committed"}
        assert lifecycle.shutdown_requested is True
        assert lifecycle.stop_event.is_set()
        assert server.should_exit is True
    finally:
        repository.close()


def test_desktop_shutdown_remains_available_in_read_only_demo_mode(
    tmp_path: Path,
) -> None:
    session = _session()
    database_url = f"sqlite:///{tmp_path / 'desktop-demo-exit.db'}"
    migrate(database_url)
    repository = TaskRepository(
        create_engine_for_url(database_url),
        owns_engine=True,
    )
    lifecycle = DesktopLifecycleController()
    server = SimpleNamespace(should_exit=False)
    lifecycle.bind_server(server)
    settings = Settings(
        database_url=database_url,
        data_dir=tmp_path / "Stock Desk" / "v1.1",
    )
    try:
        with TestClient(
            create_app(
                settings,
                task_repository=repository,
                desktop_session=session,
                desktop_lifecycle=lifecycle,
            )
        ) as client:
            headers = _headers(session)
            demo = client.post(
                "/api/v1/onboarding/actions/demo",
                headers=headers,
            )
            unauthorized = client.post("/api/desktop/shutdown")
            assert lifecycle.shutdown_prepared is False
            assert lifecycle.shutdown_requested is False
            prepared = client.post(
                "/api/desktop/shutdown",
                headers=headers,
            )
            assert server.should_exit is False
            assert lifecycle.shutdown_prepared is True
            assert lifecycle.shutdown_requested is False
            committed = client.post(
                "/api/desktop/shutdown/commit",
                headers=headers,
            )

        assert demo.status_code == 200
        assert demo.json()["demo_mode"] is True
        assert unauthorized.status_code == 403
        assert unauthorized.json() == {"code": "desktop_origin_forbidden"}
        assert prepared.status_code == 202
        assert prepared.json() == {"status": "shutdown_requested"}
        assert committed.status_code == 202
        assert committed.json() == {"status": "shutdown_committed"}
        assert lifecycle.shutdown_requested is True
        assert server.should_exit is True
    finally:
        repository.close()


def test_desktop_shutdown_commit_cannot_bypass_prepare_gate(tmp_path: Path) -> None:
    session = _session()
    repository = _repository(tmp_path)
    lifecycle = DesktopLifecycleController()
    server = SimpleNamespace(should_exit=False)
    lifecycle.bind_server(server)
    try:
        with TestClient(
            create_app(
                task_repository=repository,
                desktop_session=session,
                desktop_lifecycle=lifecycle,
            )
        ) as client:
            response = client.post(
                "/api/desktop/shutdown/commit", headers=_headers(session)
            )

        assert response.status_code == 409
        assert response.json() == {"code": "desktop_shutdown_not_prepared"}
        assert lifecycle.shutdown_prepared is False
        assert lifecycle.shutdown_requested is False
        assert server.should_exit is False
    finally:
        repository.close()


def test_worker_waiting_on_claim_gate_rechecks_stop_before_claiming(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    task = repository.create("demo.double", {"value": 1})
    worker = TaskWorker(repository, worker_id="desktop-test-worker")
    stop_event = threading.Event()
    result: list[object] = []
    started = threading.Event()

    def attempt_claim() -> None:
        started.set()
        result.append(worker.run_once(stop_event=stop_event))

    try:
        with repository.hold_claim_gate():
            thread = threading.Thread(target=attempt_claim)
            thread.start()
            assert started.wait(timeout=1)
            stop_event.set()
        thread.join(timeout=1)

        assert not thread.is_alive()
        assert result == [None]
        assert repository.get(task.id).status == "queued"
    finally:
        repository.close()


def test_desktop_lifecycle_storage_failures_are_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _session()
    repository = _repository(tmp_path)
    lifecycle = DesktopLifecycleController()
    monkeypatch.setattr(
        repository,
        "metrics",
        lambda: (_ for _ in ()).throw(RuntimeError("TOP-SECRET /private/storage/path")),
    )
    try:
        with TestClient(
            create_app(
                task_repository=repository,
                desktop_session=session,
                desktop_lifecycle=lifecycle,
            ),
            raise_server_exceptions=False,
        ) as client:
            activity = client.get("/api/desktop/activity", headers=_headers(session))
            shutdown = client.post("/api/desktop/shutdown", headers=_headers(session))

        assert activity.status_code == 503
        assert shutdown.status_code == 503
        assert activity.json() == {"code": "storage_unavailable"}
        assert shutdown.json() == {"code": "storage_unavailable"}
        serialized = activity.text + shutdown.text
        assert "TOP-SECRET" not in serialized
        assert "/private/" not in serialized
        assert lifecycle.shutdown_requested is False
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("origin", "secret", "version", "revision"),
    [
        ("http://evil.invalid", secrets.token_urlsafe(32), "1.1.0", SOURCE_REVISION),
        (TAURI_ORIGIN, "short", "1.1.0", SOURCE_REVISION),
        (TAURI_ORIGIN, secrets.token_urlsafe(32), "unknown", SOURCE_REVISION),
        (TAURI_ORIGIN, secrets.token_urlsafe(32), "1.1.0", "not-a-revision"),
    ],
    ids=("origin", "secret", "version", "revision"),
)
def test_desktop_session_rejects_unsafe_authority_at_construction(
    origin: str, secret: str, version: str, revision: str
) -> None:
    with pytest.raises(ValueError):
        DesktopSession(
            origin=origin,
            secret=secret,
            host_version=version,
            frontend_version="1.1.0",
            sidecar_version="1.1.0",
            source_revision=revision,
        )
