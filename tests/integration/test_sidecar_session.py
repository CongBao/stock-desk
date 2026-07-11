import secrets
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest

from stock_desk.desktop_session import DesktopLifecycleController, DesktopSession
from stock_desk.main import create_app
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker


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

        assert response.status_code == 202
        assert response.json() == {"status": "shutdown_requested"}
        assert lifecycle.shutdown_requested is True
        assert lifecycle.stop_event.is_set()
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
