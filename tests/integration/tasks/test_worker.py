from pathlib import Path
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import threading
from typing import Any, cast

import pytest
from sqlalchemy import event

import stock_desk.tasks.repository as repository_module
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import (
    TaskConflict,
    TaskNotFound,
    TaskRepository,
    TaskValidationError,
)
from stock_desk.tasks.worker import TaskWorker, demo_double


def _repository(tmp_path: Path) -> TaskRepository:
    url = f"sqlite:///{tmp_path / 'tasks.db'}"
    migrate(url)
    return TaskRepository(create_engine_for_url(url), owns_engine=True)


def test_task_can_be_created_claimed_and_completed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": 21})

        claimed = repository.claim_next("worker-1")
        assert claimed is not None
        assert claimed.id == created.id
        assert claimed.status == "running"

        completed = repository.complete(claimed.id, {"value": 42})
        assert completed.status == "succeeded"
        assert completed.progress == 1.0
        assert completed.result == {"value": 42}
    finally:
        repository.close()


def test_cancelled_queued_task_cannot_be_claimed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": 21})

        cancelled = repository.request_cancel(created.id)

        assert cancelled.status == "cancelled"
        assert cancelled.cancel_requested is True
        assert repository.claim_next("worker-1") is None
    finally:
        repository.close()


def test_owned_repository_persists_tasks_across_reopen(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'persistent.db'}"
    repository = TaskRepository.open(url)
    created = repository.create("demo.double", {"value": 3})
    repository.close()

    reopened = TaskRepository.open(url)
    try:
        loaded = reopened.get(created.id)

        assert loaded == created
        assert loaded.created_at.tzinfo is timezone.utc
        assert loaded.updated_at.tzinfo is timezone.utc
        with pytest.raises(FrozenInstanceError):
            setattr(loaded, "status", "failed")
        with pytest.raises(TypeError):
            cast(Any, loaded.payload)["value"] = 4
    finally:
        reopened.close()


def test_get_missing_task_raises_typed_not_found(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskNotFound):
            repository.get("missing")
    finally:
        repository.close()


def test_list_recent_is_newest_first_and_bounded(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        first = repository.create("first", {})
        second = repository.create("second", {})

        assert [task.id for task in repository.list_recent(limit=1)] == [second.id]
        assert [task.id for task in repository.list_recent(limit=2)] == [
            second.id,
            first.id,
        ]
        with pytest.raises(TaskValidationError):
            repository.list_recent(limit=0)
        with pytest.raises(TaskValidationError):
            repository.list_recent(limit=101)
    finally:
        repository.close()


@pytest.mark.parametrize("kind", ["", "   ", "x" * 65])
def test_create_rejects_invalid_kind(tmp_path: Path, kind: str) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.create(kind, {})
    finally:
        repository.close()


def test_repository_rejects_surrounding_identifier_whitespace(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.create(" demo.double ", {})
        with pytest.raises(TaskValidationError):
            repository.claim_next(" worker-1 ")
    finally:
        repository.close()


@pytest.mark.parametrize(
    "payload",
    [
        cast(dict[str, Any], {"nested": {1: "invalid"}}),
        cast(dict[str, Any], {"items": [{"nested": {2: "invalid"}}]}),
    ],
)
def test_repository_rejects_non_string_keys_in_nested_json_objects(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.create("invalid.keys", payload)
    finally:
        repository.close()


def test_repository_rejects_cyclic_payload_as_typed_validation_error(
    tmp_path: Path,
) -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.create("invalid.cycle", cyclic)
    finally:
        repository.close()


def test_repository_rejects_excessive_json_nesting_as_typed_validation_error(
    tmp_path: Path,
) -> None:
    deeply_nested: dict[str, Any] = {}
    cursor = deeply_nested
    for _ in range(20_000):
        nested: dict[str, Any] = {}
        cursor["nested"] = nested
        cursor = nested

    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.create("invalid.depth", deeply_nested)
    finally:
        repository.close()


def test_repository_rejects_non_json_values_with_typed_validation_error(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.create("invalid", {"value": object()})

        complete_task = repository.create("complete", {})
        assert repository.claim_next("worker") is not None
        with pytest.raises(TaskValidationError):
            repository.complete(complete_task.id, {"value": object()})

        repository.fail(complete_task.id, {"code": "recovered"})
        fail_task = repository.create("fail", {})
        assert repository.claim_next("worker") is not None
        with pytest.raises(TaskValidationError):
            repository.fail(fail_task.id, {"value": object()})
    finally:
        repository.close()


def test_progress_requires_running_task_and_valid_fraction(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        queued = repository.create("demo.double", {"value": 1})
        with pytest.raises(TaskConflict):
            repository.set_progress(queued.id, 0.1)

        running = repository.claim_next("worker-1")
        assert running is not None
        progressed = repository.set_progress(running.id, 0.5)
        assert progressed.status == "running"
        assert progressed.progress == 0.5
        assert progressed.updated_at >= running.updated_at

        for invalid in (-0.1, 1.1, math.nan, math.inf, True):
            with pytest.raises(TaskValidationError):
                repository.set_progress(running.id, invalid)
    finally:
        repository.close()


@pytest.mark.parametrize("terminal_operation", ["complete", "fail"])
def test_transition_timestamps_never_move_backward_when_clock_regresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_operation: str,
) -> None:
    sampled = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    clock_values = iter(
        [
            sampled,
            sampled - timedelta(seconds=1),
            sampled - timedelta(seconds=2),
            sampled - timedelta(seconds=3),
            sampled - timedelta(seconds=4),
        ]
    )
    monkeypatch.setattr(repository_module, "_utc_now", lambda: next(clock_values))
    repository = _repository(tmp_path)
    try:
        created = repository.create("clock.test", {})
        running = repository.claim_next("worker")
        assert running is not None
        assert running.started_at is not None
        assert running.started_at >= created.created_at
        assert running.updated_at >= created.updated_at

        progressed = repository.set_progress(created.id, 0.5)
        assert progressed.updated_at >= running.updated_at

        cancelling = repository.request_cancel(created.id)
        assert cancelling.updated_at >= progressed.updated_at

        if terminal_operation == "complete":
            terminal = repository.complete(created.id, {"ok": True})
        else:
            terminal = repository.fail(created.id, {"code": "failure"})
        assert terminal.finished_at is not None
        assert terminal.updated_at >= cancelling.updated_at
        assert terminal.finished_at >= cancelling.updated_at
        assert terminal.finished_at == terminal.updated_at
    finally:
        repository.close()


def test_failure_is_terminal_and_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": "bad"})
        running = repository.claim_next("worker-1")
        assert running is not None

        failed = repository.fail(created.id, {"code": "invalid_payload"})
        repeated = repository.fail(created.id, {"code": "ignored"})

        assert failed.status == "failed"
        assert failed.error == {"code": "invalid_payload"}
        assert failed.finished_at is not None
        assert repeated == failed
        with pytest.raises(TaskConflict):
            repository.complete(created.id, {"value": 2})
        with pytest.raises(TaskConflict):
            repository.set_progress(created.id, 0.9)
        with pytest.raises(TaskConflict):
            repository.request_cancel(created.id)
    finally:
        repository.close()


def test_success_is_terminal_and_idempotent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": 1})
        assert repository.claim_next("worker-1") is not None

        succeeded = repository.complete(created.id, {"value": 2})
        repeated = repository.complete(created.id, {"value": 999})

        assert succeeded.status == "succeeded"
        assert succeeded.result == {"value": 2}
        assert repeated == succeeded
        with pytest.raises(TaskConflict):
            repository.fail(created.id, {"code": "too_late"})
    finally:
        repository.close()


@pytest.mark.parametrize("terminal_operation", ["complete", "fail"])
def test_running_cancellation_wins_over_terminal_transition(
    tmp_path: Path, terminal_operation: str
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": 1})
        assert repository.claim_next("worker-1") is not None

        cancelling = repository.request_cancel(created.id)
        assert cancelling.status == "running"
        assert cancelling.cancel_requested is True
        assert cancelling.finished_at is None

        if terminal_operation == "complete":
            terminal = repository.complete(created.id, {"value": 2})
        else:
            terminal = repository.fail(created.id, {"code": "failed"})

        assert terminal.status == "cancelled"
        assert terminal.cancel_requested is True
        assert terminal.result is None
        assert terminal.error is None
        assert terminal.finished_at is not None
        assert repository.request_cancel(created.id) == terminal
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("set_progress", (0.5,)),
        ("complete", ({"value": 1},)),
        ("fail", ({"code": "failure"},)),
        ("request_cancel", ()),
    ],
)
def test_transition_of_missing_task_raises_typed_not_found(
    tmp_path: Path, operation: str, arguments: tuple[object, ...]
) -> None:
    repository = _repository(tmp_path)
    try:
        method = getattr(repository, operation)
        with pytest.raises(TaskNotFound):
            method("missing", *arguments)
    finally:
        repository.close()


def test_atomic_claims_are_unique_across_repository_instances(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'concurrent.db'}"
    repository = TaskRepository.open(url)
    try:
        task_ids = [
            repository.create("demo.double", {"value": value}).id for value in range(8)
        ]
    finally:
        repository.close()

    barrier = threading.Barrier(len(task_ids))

    def claim_one(index: int) -> str | None:
        worker_repository = TaskRepository.open(url)
        try:
            barrier.wait(timeout=10)
            claimed = worker_repository.claim_next(f"worker-{index}")
            return claimed.id if claimed is not None else None
        finally:
            worker_repository.close()

    with ThreadPoolExecutor(max_workers=len(task_ids)) as executor:
        claims = list(executor.map(claim_one, range(len(task_ids))))

    assert None not in claims
    assert len(set(claims)) == len(task_ids)
    assert set(claims) == set(task_ids)


def test_claims_choose_oldest_queued_task(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        first = repository.create("first", {})
        second = repository.create("second", {})

        first_claim = repository.claim_next("worker-1")
        second_claim = repository.claim_next("worker-2")

        assert first_claim is not None and first_claim.id == first.id
        assert second_claim is not None and second_claim.id == second.id
    finally:
        repository.close()


@pytest.mark.parametrize("worker_id", ["", "   ", "x" * 256])
def test_claim_rejects_invalid_worker_id(tmp_path: Path, worker_id: str) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.claim_next(worker_id)
    finally:
        repository.close()


def test_close_disposes_only_owned_engine(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'ownership.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    disposal_events: list[bool] = []

    def record_disposal(_engine: object) -> None:
        disposal_events.append(True)

    event.listen(engine, "engine_disposed", record_disposal)
    TaskRepository(engine).close()
    assert disposal_events == []

    TaskRepository(engine, owns_engine=True).close()
    assert disposal_events == [True]


@pytest.mark.parametrize("terminal_operation", ["complete", "fail"])
def test_terminal_transition_race_with_cancellation_never_leaves_task_running(
    tmp_path: Path, terminal_operation: str
) -> None:
    url = f"sqlite:///{tmp_path / f'race-{terminal_operation}.db'}"
    observer = TaskRepository.open(url)
    terminal_repository = TaskRepository.open(url)
    cancel_repository = TaskRepository.open(url)
    try:
        created = observer.create("race", {})
        assert observer.claim_next("worker") is not None
        barrier = threading.Barrier(2)

        def terminate() -> str:
            barrier.wait(timeout=10)
            if terminal_operation == "complete":
                return terminal_repository.complete(created.id, {"ok": True}).status
            return terminal_repository.fail(created.id, {"code": "failure"}).status

        def cancel() -> str:
            barrier.wait(timeout=10)
            try:
                return cancel_repository.request_cancel(created.id).status
            except TaskConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            terminal_future = executor.submit(terminate)
            cancel_future = executor.submit(cancel)
            terminal_status = terminal_future.result(timeout=10)
            cancel_status = cancel_future.result(timeout=10)

        final = observer.get(created.id)
        expected_terminal = (
            "succeeded" if terminal_operation == "complete" else "failed"
        )
        assert final.status in {"cancelled", expected_terminal}
        assert final.status != "running"
        assert terminal_status == final.status
        if final.status == expected_terminal:
            assert cancel_status == "conflict"
        else:
            assert cancel_status in {"running", "cancelled"}
    finally:
        terminal_repository.close()
        cancel_repository.close()
        observer.close()


def test_worker_dispatches_demo_handler_and_claims_at_most_one(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        first = repository.create("demo.double", {"value": 21})
        second = repository.create("demo.double", {"value": 10})
        worker = TaskWorker(repository, worker_id="worker-1", poll_interval=0.01)
        worker.register("demo.double", demo_double)

        completed = worker.run_once()

        assert completed is not None
        assert completed.id == first.id
        assert completed.status == "succeeded"
        assert completed.result == {"value": 42}
        assert repository.get(second.id).status == "queued"
    finally:
        repository.close()


def test_worker_records_unknown_kind_as_structured_failure(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("unknown", {})
        worker = TaskWorker(repository, worker_id="worker-1")

        completed = worker.run_once()

        assert completed is not None and completed.id == created.id
        assert completed.status == "failed"
        assert completed.error == {"code": "unknown_task_kind"}
    finally:
        repository.close()


def test_worker_does_not_leak_handler_exception_details(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("secret.failure", {})
        worker = TaskWorker(repository, worker_id="worker-1")

        def leak_secret(_task: object) -> dict[str, object]:
            raise RuntimeError("database password is hunter2")

        worker.register("secret.failure", leak_secret)
        completed = worker.run_once()

        assert completed is not None and completed.id == created.id
        assert completed.status == "failed"
        assert completed.error == {"code": "task_handler_failed"}
        assert "hunter2" not in repr(completed.error)
    finally:
        repository.close()


def test_worker_logs_only_safe_handler_failure_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("secret.failure", {})
        worker = TaskWorker(repository, worker_id="worker-1")

        def leak_secret(_task: object) -> dict[str, object]:
            raise RuntimeError("database password is hunter2")

        worker.register("secret.failure", leak_secret)
        with caplog.at_level(logging.WARNING, logger="stock_desk.tasks.worker"):
            completed = worker.run_once()

        assert completed is not None and completed.status == "failed"
        assert created.id in caplog.text
        assert "secret.failure" in caplog.text
        assert "RuntimeError" in caplog.text
        assert "hunter2" not in caplog.text
        assert "Traceback" not in caplog.text
    finally:
        repository.close()


def test_worker_converts_non_serializable_handler_result_to_failure(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        repository.create("invalid.result", {})
        worker = TaskWorker(repository, worker_id="worker-1")

        def invalid_result(_task: object) -> dict[str, object]:
            return {"not_json": object()}

        worker.register("invalid.result", invalid_result)
        completed = worker.run_once()

        assert completed is not None
        assert completed.status == "failed"
        assert completed.error == {"code": "task_handler_failed"}
    finally:
        repository.close()


def test_worker_converts_cyclic_handler_result_to_sanitized_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("cyclic.result", {})
        worker = TaskWorker(repository, worker_id="worker-1")

        def cyclic_result(_task: object) -> dict[str, Any]:
            result: dict[str, Any] = {"secret": "hunter2"}
            result["self"] = result
            return result

        worker.register("cyclic.result", cyclic_result)
        with caplog.at_level(logging.WARNING, logger="stock_desk.tasks.worker"):
            completed = worker.run_once()

        assert completed is not None and completed.id == created.id
        assert completed.status == "failed"
        assert completed.error == {"code": "task_handler_failed"}
        assert repository.get(created.id).status == "failed"
        assert "TaskValidationError" in caplog.text
        assert "hunter2" not in caplog.text
        assert "Traceback" not in caplog.text
    finally:
        repository.close()


def test_worker_observes_cancellation_requested_by_handler(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("cancel.me", {})
        worker = TaskWorker(repository, worker_id="worker-1")

        def cancel_handler(task: object) -> dict[str, object]:
            task_id = getattr(task, "id")
            repository.request_cancel(task_id)
            return {"should": "be discarded"}

        worker.register("cancel.me", cancel_handler)
        completed = worker.run_once()

        assert completed is not None and completed.id == created.id
        assert completed.status == "cancelled"
        assert completed.result is None
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("worker_id", "poll_interval"),
    [
        ("", 1.0),
        ("   ", 1.0),
        (" worker ", 1.0),
        ("x" * 256, 1.0),
        ("worker", -0.1),
        ("worker", math.nan),
        ("worker", math.inf),
    ],
)
def test_worker_rejects_invalid_configuration(
    tmp_path: Path, worker_id: str, poll_interval: float
) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError):
            TaskWorker(
                repository,
                worker_id=worker_id,
                poll_interval=poll_interval,
            )
    finally:
        repository.close()


def test_worker_rejects_registration_kind_with_surrounding_whitespace(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        worker = TaskWorker(repository, worker_id="worker")
        with pytest.raises(ValueError):
            worker.register(" demo.double ", demo_double)
    finally:
        repository.close()


def test_run_forever_waits_on_stop_event_instead_of_busy_spinning(
    tmp_path: Path,
) -> None:
    class StopAfterFirstWait(threading.Event):
        def __init__(self) -> None:
            super().__init__()
            self.wait_calls: list[float | None] = []

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_calls.append(timeout)
            self.set()
            return True

    repository = _repository(tmp_path)
    try:
        worker = TaskWorker(repository, worker_id="worker-1", poll_interval=0.25)
        stop_event = StopAfterFirstWait()

        worker.run_forever(stop_event)

        assert stop_event.wait_calls == [0.25]
    finally:
        repository.close()
