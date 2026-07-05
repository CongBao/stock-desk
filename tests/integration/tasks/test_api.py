from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import Engine, event

from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository


def _injected_repository(tmp_path: Path) -> tuple[TaskRepository, Engine]:
    url = f"sqlite:///{tmp_path / 'api.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    return TaskRepository(engine), engine


def test_task_api_exact_lifecycle_and_health_regression(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        with TestClient(create_app(task_repository=repository)) as client:
            health = client.get("/api/health")
            created_response = client.post(
                "/api/tasks",
                json={"kind": "demo.double", "payload": {"value": 21}},
            )

            assert health.status_code == 200
            assert health.json() == {
                "name": "stock-desk",
                "status": "ok",
                "api_version": "v1",
            }
            assert created_response.status_code == 201
            created = created_response.json()
            assert created["kind"] == "demo.double"
            assert created["payload"] == {"value": 21}
            assert created["status"] == "queued"
            assert created["progress"] == 0.0
            assert created["result"] is None
            assert created["error"] is None
            assert created["cancel_requested"] is False
            assert created["worker_id"] is None
            assert created["started_at"] is None
            assert created["finished_at"] is None
            assert created["created_at"].endswith("Z")
            assert created["updated_at"].endswith("Z")

            listed = client.get("/api/tasks", params={"limit": 10})
            fetched = client.get(f"/api/tasks/{created['id']}")
            cancelled = client.post(f"/api/tasks/{created['id']}/cancel")

            assert listed.status_code == 200
            assert listed.json() == [created]
            assert fetched.status_code == 200
            assert fetched.json() == created
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["cancel_requested"] is True
            assert cancelled.json()["finished_at"] is not None
    finally:
        engine.dispose()


def test_task_api_maps_not_found_conflict_and_validation(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": 1})
        assert repository.claim_next("worker") is not None
        repository.complete(created.id, {"value": 2})

        with TestClient(create_app(task_repository=repository)) as client:
            missing_get = client.get("/api/tasks/missing")
            missing_cancel = client.post("/api/tasks/missing/cancel")
            conflict = client.post(f"/api/tasks/{created.id}/cancel")
            invalid_limit = client.get("/api/tasks", params={"limit": 101})
            invalid_kind = client.post(
                "/api/tasks", json={"kind": "   ", "payload": {}}
            )
            padded_kind = client.post(
                "/api/tasks", json={"kind": " demo.double ", "payload": {}}
            )

        assert missing_get.status_code == 404
        assert missing_get.json() == {"detail": "Task not found"}
        assert missing_cancel.status_code == 404
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "Task state conflict"}
        assert invalid_limit.status_code == 422
        assert invalid_kind.status_code == 422
        assert padded_kind.status_code == 422
    finally:
        engine.dispose()


def test_app_creates_owned_repository_lazily_on_first_task_request(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lazy.db"
    application = create_app(Settings(database_url=f"sqlite:///{database_path}"))

    assert not database_path.exists()
    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200
        assert not database_path.exists()

        response = client.get("/api/tasks")
        assert response.status_code == 200
        assert response.json() == []
        assert database_path.exists()


def test_app_does_not_dispose_injected_repository(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    disposal_events: list[bool] = []

    def record_disposal(_engine: object) -> None:
        disposal_events.append(True)

    event.listen(engine, "engine_disposed", record_disposal)
    try:
        with TestClient(create_app(task_repository=repository)) as client:
            assert client.get("/api/tasks").status_code == 200

        assert disposal_events == []
    finally:
        engine.dispose()


def test_app_closes_lazily_owned_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, engine = _injected_repository(tmp_path)
    opened_urls: list[str] = []
    close_calls: list[bool] = []

    def fake_open(_cls: type[TaskRepository], url: str) -> TaskRepository:
        opened_urls.append(url)
        return repository

    def record_close() -> None:
        close_calls.append(True)

    monkeypatch.setattr(TaskRepository, "open", classmethod(fake_open))
    monkeypatch.setattr(repository, "close", record_close)
    database_url = f"sqlite:///{tmp_path / 'owned.db'}"
    try:
        with TestClient(create_app(Settings(database_url=database_url))) as client:
            assert opened_urls == []
            assert client.get("/api/tasks").status_code == 200
            assert opened_urls == [database_url]

        assert close_calls == [True]
    finally:
        engine.dispose()
