from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import Engine, event, text

from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.api.tasks import router as tasks_router
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import (
    TaskRepository,
    TaskRepositoryError,
    TaskValidationError,
)


def _injected_repository(tmp_path: Path) -> tuple[TaskRepository, Engine]:
    url = f"sqlite:///{tmp_path / 'api.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    return TaskRepository(engine), engine


def _task_event_counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM task_run), "
                "(SELECT count(*) FROM task_event)"
            )
        ).one()
    return int(row[0]), int(row[1])


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
            assert created["correlation_id"] == created["id"]
            assert created["duration_ms"] is None
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
            assert cancelled.json()["correlation_id"] == created["id"]
            assert cancelled.json()["duration_ms"] is None
    finally:
        engine.dispose()


def test_task_events_metrics_correlation_and_duration_api(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        succeeded = repository.create("success", {})
        assert repository.claim_next("worker-success") is not None
        repository.set_progress(succeeded.id, 0.5)
        repository.complete(succeeded.id, {"ok": True})

        failed = repository.create("failure", {})
        assert repository.claim_next("worker-failure") is not None
        repository.fail(
            failed.id,
            {"code": "unsafe", "raw_exception": "database password is hunter2"},
        )
        queued = repository.create("queued", {})
        expected_metrics = repository.metrics()

        with TestClient(create_app(task_repository=repository)) as client:
            metrics_response = client.get("/api/tasks/metrics")
            events_response = client.get(
                f"/api/tasks/{failed.id}/events", params={"limit": 100}
            )
            succeeded_response = client.get(f"/api/tasks/{succeeded.id}")
            queued_response = client.get(f"/api/tasks/{queued.id}")

        assert metrics_response.status_code == 200
        metrics = metrics_response.json()
        assert metrics["total"] == 3
        assert metrics["by_status"] == {
            "queued": 1,
            "running": 0,
            "succeeded": 1,
            "failed": 1,
            "cancelled": 0,
        }
        assert metrics["failure_count"] == 1
        assert metrics["completed_count"] == 2
        assert metrics["average_duration_ms"] == pytest.approx(
            expected_metrics.average_duration_ms
        )
        assert metrics["min_duration_ms"] == pytest.approx(
            expected_metrics.min_duration_ms
        )
        assert metrics["max_duration_ms"] == pytest.approx(
            expected_metrics.max_duration_ms
        )

        assert events_response.status_code == 200
        events = events_response.json()
        assert [task_event["event_name"] for task_event in events] == [
            "task.created",
            "task.claimed",
            "task.failed",
        ]
        assert all(task_event["task_id"] == failed.id for task_event in events)
        assert all(task_event["correlation_id"] == failed.id for task_event in events)
        assert events[-1]["detail"] == {"code": "task_failed"}
        assert "hunter2" not in repr(events)
        assert "raw_exception" not in repr(events)

        succeeded_body = succeeded_response.json()
        assert succeeded_body["correlation_id"] == succeeded.id
        assert succeeded_body["duration_ms"] is not None
        assert succeeded_body["duration_ms"] >= 0
        assert queued_response.json()["correlation_id"] == queued.id
        assert queued_response.json()["duration_ms"] is None
        assert succeeded_body["presentation"] == {
            "label": "后台任务",
            "stage": None,
            "processed": None,
            "total": None,
            "failed": None,
            "target": None,
        }
        assert events[-1]["presentation"] == {
            "label": "任务失败",
            "stage": None,
            "processed": None,
            "total": None,
            "failed": None,
        }
    finally:
        engine.dispose()


def test_task_presentation_never_copies_arbitrary_stored_json(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        created = repository.create(
            "unknown.secret.kind", {"label": "PAYLOAD-SENTINEL"}
        )
        assert repository.claim_next("worker") is not None
        repository.set_progress(
            created.id,
            0.5,
            {
                "stage": "executing",
                "processed": 1,
                "total": 2,
                "failed": 0,
                "private": "EVENT-SENTINEL",
            },
        )
        repository.fail(
            created.id,
            {"message": "ERROR-SENTINEL", "run_id": "RESULT-SENTINEL"},
        )

        with TestClient(create_app(task_repository=repository)) as client:
            task_body = client.get(f"/api/tasks/{created.id}").json()
            event_bodies = client.get(f"/api/tasks/{created.id}/events").json()

        assert task_body["presentation"] == {
            "label": "后台任务",
            "stage": None,
            "processed": None,
            "total": None,
            "failed": None,
            "target": None,
        }
        assert [event["presentation"]["label"] for event in event_bodies] == [
            "任务已创建",
            "任务已开始",
            "任务进度已更新",
            "任务失败",
        ]
        assert "SENTINEL" not in repr(
            [task_body["presentation"]]
            + [event["presentation"] for event in event_bodies]
        )
    finally:
        engine.dispose()


def test_safe_task_views_omit_raw_json_and_keep_legacy_default(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        created = repository.create("secret.task", {"secret": "PAYLOAD-SENTINEL"})
        assert repository.claim_next("private-worker") is not None
        repository.set_progress(created.id, 0.5, {"secret": "EVENT-SENTINEL"})
        repository.fail(created.id, {"secret": "ERROR-SENTINEL"})

        with TestClient(create_app(task_repository=repository)) as client:
            legacy = client.get(f"/api/tasks/{created.id}")
            safe = client.get(f"/api/tasks/{created.id}", params={"view": "safe"})
            safe_list = client.get("/api/tasks", params={"view": "safe", "limit": 100})
            safe_events = client.get(
                f"/api/tasks/{created.id}/events",
                params={"view": "safe", "limit": 100},
            )

        assert "PAYLOAD-SENTINEL" in legacy.text
        for response in (safe, safe_list, safe_events):
            assert response.status_code == 200
            assert "SENTINEL" not in response.text
            assert "private-worker" not in response.text
        assert set(safe.json()) == {
            "id",
            "kind",
            "status",
            "progress",
            "cancel_requested",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
            "duration_ms",
            "presentation",
        }
        assert set(safe_events.json()[-1]) == {
            "id",
            "task_id",
            "level",
            "progress",
            "occurred_at",
            "presentation",
        }

        schema = create_app(task_repository=repository).openapi()
        task_response = schema["paths"]["/api/tasks/{task_id}"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        assert len(task_response["anyOf"]) == 2
        safe_schema = schema["components"]["schemas"]["TaskSafeResponse"]
        assert safe_schema["additionalProperties"] is False
        assert "payload" not in safe_schema["properties"]
    finally:
        engine.dispose()


def test_task_list_bulk_presentation_uses_constant_query_count(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        for _index in range(100):
            repository.create("backtest.run", {})
        statements = 0

        def count_select(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal statements
            if statement.lstrip().upper().startswith("SELECT"):
                statements += 1

        event.listen(engine, "before_cursor_execute", count_select)
        try:
            with TestClient(create_app(task_repository=repository)) as client:
                response = client.get(
                    "/api/tasks", params={"view": "safe", "limit": 100}
                )
        finally:
            event.remove(engine, "before_cursor_execute", count_select)

        assert response.status_code == 200
        assert len(response.json()) == 100
        assert statements <= 2
    finally:
        engine.dispose()


def test_queued_cancellation_is_terminal_without_a_duration_sample(
    tmp_path: Path,
) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        created = repository.create("queued.cancel", {})
        repository.request_cancel(created.id)
        with TestClient(create_app(task_repository=repository)) as client:
            metrics = client.get("/api/tasks/metrics").json()

        assert metrics["by_status"]["cancelled"] == 1
        assert metrics["completed_count"] == 0
        assert metrics["average_duration_ms"] is None
    finally:
        engine.dispose()


def test_task_event_api_maps_missing_tasks_and_invalid_limits(tmp_path: Path) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        created = repository.create("events", {})
        with TestClient(create_app(task_repository=repository)) as client:
            missing = client.get("/api/tasks/missing/events")
            too_small = client.get(
                f"/api/tasks/{created.id}/events", params={"limit": 0}
            )
            too_large = client.get(
                f"/api/tasks/{created.id}/events", params={"limit": 101}
            )

        assert missing.status_code == 404
        assert missing.json() == {"detail": "Task not found"}
        assert too_small.status_code == 422
        assert too_small.json() == {"code": "invalid_request"}
        assert too_large.status_code == 422
        assert too_large.json() == {"code": "invalid_request"}

        schema = create_app(task_repository=repository).openapi()
        expected = {"$ref": "#/components/schemas/TaskErrorResponse"}
        assert (
            schema["paths"]["/api/tasks"]["get"]["responses"]["422"]["content"][
                "application/json"
            ]["schema"]
            == expected
        )
        assert (
            schema["paths"]["/api/tasks/{task_id}/events"]["get"]["responses"]["422"][
                "content"
            ]["application/json"]["schema"]
            == expected
        )
        for path, path_item in schema["paths"].items():
            if not path.startswith("/api/tasks"):
                continue
            for operation in path_item.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                assert (
                    operation["responses"]["422"]["content"]["application/json"][
                        "schema"
                    ]
                    == expected
                )
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
        assert invalid_limit.json() == {"code": "invalid_request"}
        assert invalid_kind.status_code == 422
        assert padded_kind.status_code == 422
    finally:
        engine.dispose()


def test_generic_task_create_rejects_analysis_run_without_orphan_task(
    tmp_path: Path,
) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        with TestClient(create_app(task_repository=repository)) as client:
            response = client.post(
                "/api/tasks",
                json={"kind": "analysis.run", "payload": {"symbol": "600000.SH"}},
            )

        assert response.status_code == 422
        assert response.json() == {"code": "reserved_task_kind"}
        assert _task_event_counts(engine) == (0, 0)

        schema = create_app(task_repository=repository).openapi()
        reserved_schema = schema["components"]["schemas"]["TaskErrorResponse"]
        assert set(reserved_schema["properties"]) == {"code"}
        assert reserved_schema["properties"]["code"]["enum"] == [
            "invalid_request",
            "reserved_task_kind",
            "storage_unavailable",
        ]
        assert schema["paths"]["/api/tasks"]["post"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/TaskErrorResponse"}
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "", "payload": {}},
        {"kind": "demo.double", "payload": []},
        {"kind": "demo.double", "payload": {}, "secret_extra": "TOP-SECRET"},
    ],
    ids=("blank-kind", "non-object-payload", "extra-field"),
)
def test_task_create_request_validation_returns_single_safe_code(
    tmp_path: Path,
    body: dict[str, object],
) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        before = _task_event_counts(engine)
        with TestClient(create_app(task_repository=repository)) as client:
            response = client.post("/api/tasks", json=body)

        assert response.status_code == 422
        assert response.json() == {"code": "invalid_request"}
        assert "TOP-SECRET" not in response.text
        assert _task_event_counts(engine) == before
    finally:
        engine.dispose()


def test_standalone_task_router_uses_same_safe_validation_contract() -> None:
    class RejectingRepository:
        def create(self, _kind: str, _payload: object) -> object:
            raise TaskValidationError("TOP-SECRET repository validation detail")

    application = FastAPI()
    application.state.task_repository_provider = RejectingRepository
    application.include_router(tasks_router)

    with TestClient(application) as client:
        request_invalid = client.post(
            "/tasks",
            json={"kind": "demo.double", "payload": {}, "extra": "TOP-SECRET"},
        )
        repository_invalid = client.post(
            "/tasks",
            json={"kind": "demo.double", "payload": {}},
        )

    assert request_invalid.status_code == 422
    assert request_invalid.json() == {"code": "invalid_request"}
    assert repository_invalid.status_code == 422
    assert repository_invalid.json() == {"code": "invalid_request"}
    assert "TOP-SECRET" not in request_invalid.text
    assert "TOP-SECRET" not in repository_invalid.text


def test_task_storage_failures_return_single_safe_503_contract() -> None:
    class CorruptRepository:
        def list_recent(self, *, limit: int) -> object:
            del limit
            raise TaskRepositoryError("TOP-SECRET list corruption")

        def get(self, _task_id: str) -> object:
            raise TaskRepositoryError("TOP-SECRET get corruption")

    application = FastAPI()
    application.state.task_repository_provider = CorruptRepository
    application.include_router(tasks_router)

    with TestClient(application, raise_server_exceptions=False) as client:
        listed = client.get("/tasks")
        fetched = client.get("/tasks/task-id")

    assert listed.status_code == 503
    assert listed.json() == {"code": "storage_unavailable"}
    assert fetched.status_code == 503
    assert fetched.json() == {"code": "storage_unavailable"}
    assert "TOP-SECRET" not in listed.text
    assert "TOP-SECRET" not in fetched.text

    schema = application.openapi()
    expected = {"$ref": "#/components/schemas/TaskErrorResponse"}
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            assert (
                operation["responses"]["503"]["content"]["application/json"]["schema"]
                == expected
            )


@pytest.mark.parametrize(
    "body",
    [
        b'{"kind":"json.surrogate","payload":{"value":"\\ud800"}}',
        b'{"kind":"json.surrogate","payload":{"\\udfff":"value"}}',
    ],
    ids=("surrogate-value", "surrogate-key"),
)
def test_task_api_rejects_isolated_surrogates_without_committing(
    tmp_path: Path,
    body: bytes,
) -> None:
    repository, engine = _injected_repository(tmp_path)
    try:
        before = _task_event_counts(engine)
        with TestClient(
            create_app(task_repository=repository),
            raise_server_exceptions=False,
        ) as client:
            response = client.post(
                "/api/tasks",
                content=body,
                headers={"content-type": "application/json"},
            )

        assert response.status_code == 422
        assert response.json() == {"code": "invalid_request"}
        assert _task_event_counts(engine) == before
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
