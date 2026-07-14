from pathlib import Path
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import multiprocessing
import sqlite3
import threading
import time
from typing import Any, cast

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql, sqlite

import stock_desk.tasks.repository as repository_module
import stock_desk.tasks.worker as worker_module
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


def _wait_until(condition: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        threading.Event().wait(0.01)
    assert condition()


class _DelayStatusUntilControllerSettlement:
    def __init__(self, receiver: Any, controller: Any) -> None:
        self._receiver = receiver
        self._controller = controller

    def poll(self, timeout: float = 0.0) -> bool:
        if not self._controller._stopping:  # noqa: SLF001
            threading.Event().wait(timeout)
            return False
        return bool(self._receiver.poll(timeout))

    def recv(self) -> Any:
        return self._receiver.recv()

    def close(self) -> None:
        self._receiver.close()


def _delay_status_until_settlement(controller: Any) -> None:
    controller._receiver = _DelayStatusUntilControllerSettlement(  # noqa: SLF001
        controller._receiver,  # noqa: SLF001
        controller,
    )


def test_worker_heartbeat_status_uses_newest_fresh_utc_sample(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    sampled_at = datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)
    try:
        missing = repository.worker_status(now=sampled_at)
        assert missing.state == "not_detected"
        assert missing.last_seen_at is None

        repository.record_worker_heartbeat("worker-older", now=sampled_at)
        repository.record_worker_heartbeat(
            "worker-newer",
            now=sampled_at + timedelta(seconds=4),
        )

        fresh = repository.worker_status(now=sampled_at + timedelta(seconds=10))
        assert fresh.state == "running"
        assert fresh.last_seen_at == sampled_at + timedelta(seconds=4)

        stale = repository.worker_status(now=sampled_at + timedelta(seconds=20))
        assert stale.state == "not_detected"
        assert stale.last_seen_at == sampled_at + timedelta(seconds=4)
    finally:
        repository.close()


def test_historical_task_worker_id_never_counts_as_live_heartbeat(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("historical.worker", {})
        claimed = repository.claim_next("historical-host-4242")
        assert claimed is not None and claimed.id == created.id
        repository.complete(created.id, {})

        status = repository.worker_status()

        assert status.state == "not_detected"
        assert status.last_seen_at is None
    finally:
        repository.close()


def test_worker_heartbeat_upsert_replaces_corrupt_existing_sample(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    sampled_at = datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)
    try:
        repository.record_worker_heartbeat(
            "worker-1", now=sampled_at + timedelta(seconds=5)
        )
        repository.record_worker_heartbeat("worker-1", now=sampled_at)

        status = repository.worker_status(now=sampled_at + timedelta(seconds=10))
        assert status.last_seen_at == sampled_at
    finally:
        repository.close()


def test_worker_heartbeat_replaces_corrupt_future_sample_with_database_time(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    future = datetime.now(timezone.utc) + timedelta(days=365)
    try:
        repository.record_worker_heartbeat("worker-1", now=future)
        repository.record_worker_heartbeat("worker-1")

        status = repository.worker_status()

        assert status.state == "running"
        assert status.last_seen_at is not None
        assert status.last_seen_at < future
    finally:
        repository.close()


def test_worker_status_ignores_future_worker_before_selecting_newest_valid_row(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    sampled_at = datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)
    try:
        repository.record_worker_heartbeat(
            "corrupt-future-worker", now=sampled_at + timedelta(days=365)
        )
        repository.record_worker_heartbeat("normal-worker", now=sampled_at)

        status = repository.worker_status(now=sampled_at + timedelta(seconds=5))

        assert status.state == "running"
        assert status.last_seen_at == sampled_at
    finally:
        repository.close()


def test_default_worker_status_uses_database_time_for_multiple_workers(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        with repository.engine.connect() as connection:
            database_now = connection.scalar(
                select(repository_module.func.current_timestamp())
            )
        assert isinstance(database_now, datetime)
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=timezone.utc)
        repository.record_worker_heartbeat(
            "older-normal-worker",
            now=database_now - timedelta(seconds=2),
        )
        repository.record_worker_heartbeat(
            "malicious-future-worker",
            now=database_now + timedelta(days=365),
        )
        repository.record_worker_heartbeat("newest-normal-worker")

        status = repository.worker_status()

        assert status.state == "running"
        assert status.last_seen_at is not None
        assert status.last_seen_at > database_now - timedelta(seconds=2)
        assert status.last_seen_at < database_now + timedelta(days=365)
    finally:
        repository.close()


def test_blocked_heartbeat_io_has_bounded_startup_and_leaves_no_live_executor(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "blocked-heartbeat.db"
    url = f"sqlite:///{database_path}"
    migrate(url)
    repository = TaskRepository(create_engine_for_url(url), owns_engine=True)
    blocker = sqlite3.connect(database_path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    worker = TaskWorker(
        repository,
        worker_id="blocked-heartbeat",
        heartbeat_interval=0.02,
        heartbeat_start_timeout=0.3,
        heartbeat_stop_timeout=0.3,
        heartbeat_io_timeout=5.0,
    )
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as error:
            worker.run_forever(threading.Event())

        assert str(error.value) == (
            "Task worker heartbeat did not become ready within 0.300 seconds; "
            "subprocess was stopped"
        )
        assert time.monotonic() - started < 1.5
        assert not any(
            child.name == "task-worker-heartbeat-blocked-heartbeat"
            for child in multiprocessing.active_children()
        )
        assert not any(
            thread.name == "task-worker-heartbeat-blocked-heartbeat"
            for thread in threading.enumerate()
        )
    finally:
        blocker.rollback()
        blocker.close()
        repository.close()


def test_heartbeat_death_before_readiness_settles_delayed_error_payload(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    database_url = repository.engine.url.render_as_string(hide_password=False)
    with repository.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE task_worker_heartbeat")
    controller = worker_module._HeartbeatProcessController(  # noqa: SLF001
        database_url=database_url,
        worker_id="delayed-before-ready-error",
        interval=0.01,
        # Match the production startup allowance: process spawn can be slow on
        # saturated or low-spec desktop systems before the child publishes.
        start_timeout=worker_module._DEFAULT_HEARTBEAT_START_TIMEOUT_SECONDS,
        stop_timeout=0.3,
        io_timeout=0.05,
    )
    _delay_status_until_settlement(controller)
    try:
        with pytest.raises(RuntimeError) as captured:
            controller.start()

        message = str(captured.value)
        assert "heartbeat failed before readiness" in message
        assert "sqlalchemy.exc.OperationalError" in message
        assert "sqlite3.OperationalError" in message
        assert "no such table: task_worker_heartbeat" in message
        assert "SQLITE_ERROR" in message
        assert not any(
            child.name == "task-worker-heartbeat-delayed-before-ready-error"
            for child in multiprocessing.active_children()
        )
    finally:
        controller.stop()
        repository.close()


def test_running_heartbeat_death_joins_before_consuming_delayed_error_payload(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    controller = worker_module._HeartbeatProcessController(  # noqa: SLF001
        database_url=repository.engine.url.render_as_string(hide_password=False),
        worker_id="delayed-running-error",
        interval=0.01,
        # Match the production startup allowance so this test isolates the
        # post-readiness settlement race instead of runner spawn latency.
        start_timeout=worker_module._DEFAULT_HEARTBEAT_START_TIMEOUT_SECONDS,
        stop_timeout=0.3,
        io_timeout=0.05,
    )
    controller.start()
    _delay_status_until_settlement(controller)
    try:
        with repository.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE task_worker_heartbeat")
        _wait_until(lambda: not controller._process.is_alive())  # noqa: SLF001

        started = time.monotonic()
        with pytest.raises(RuntimeError) as captured:
            controller.raise_if_failed()

        assert time.monotonic() - started < 1.0
        message = str(captured.value)
        assert "heartbeat process failed" in message
        assert "sqlalchemy.exc.OperationalError" in message
        assert "sqlite3.OperationalError" in message
        assert "no such table: task_worker_heartbeat" in message
        assert "SQLITE_ERROR" in message
        assert not any(
            child.name == "task-worker-heartbeat-delayed-running-error"
            for child in multiprocessing.active_children()
        )
    finally:
        controller.stop()
        repository.close()


def test_stop_terminates_heartbeat_blocked_after_readiness_before_engine_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "blocked-after-ready.db"
    url = f"sqlite:///{database_path}"
    migrate(url)
    repository = TaskRepository(create_engine_for_url(url), owns_engine=True)
    worker = TaskWorker(
        repository,
        worker_id="blocked-after-ready",
        poll_interval=0.01,
        heartbeat_interval=0.02,
        heartbeat_stop_timeout=0.3,
        heartbeat_io_timeout=5.0,
    )
    monkeypatch.setattr(worker, "run_once", lambda *, stop_event=None: None)
    stop_event = threading.Event()
    runner = threading.Thread(target=worker.run_forever, args=(stop_event,))
    blocker = sqlite3.connect(database_path, isolation_level=None)
    try:
        runner.start()
        _wait_until(
            lambda: repository.worker_status().state == "running",
            timeout=6.0,
        )
        blocker.execute("BEGIN EXCLUSIVE")
        threading.Event().wait(0.1)

        started = time.monotonic()
        stop_event.set()
        runner.join(timeout=1.5)

        assert time.monotonic() - started < 1.5
        assert not runner.is_alive()
        assert not any(
            child.name == "task-worker-heartbeat-blocked-after-ready"
            for child in multiprocessing.active_children()
        )
        assert not any(
            thread.name == "task-worker-heartbeat-blocked-after-ready"
            for thread in threading.enumerate()
        )
        repository.close()
        threading.Event().wait(0.1)
        assert not any(
            child.name == "task-worker-heartbeat-blocked-after-ready"
            for child in multiprocessing.active_children()
        )
    finally:
        stop_event.set()
        runner.join(timeout=2)
        blocker.rollback()
        blocker.close()
        repository.close()


def test_transient_sqlite_heartbeat_lock_recovers_without_stopping_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "transient-heartbeat-lock.db"
    url = f"sqlite:///{database_path}"
    migrate(url)
    repository = TaskRepository(create_engine_for_url(url), owns_engine=True)
    worker = TaskWorker(
        repository,
        worker_id="transient-heartbeat-lock",
        poll_interval=0.01,
        heartbeat_interval=0.01,
        heartbeat_stop_timeout=0.3,
        heartbeat_io_timeout=0.05,
        heartbeat_contention_grace=0.3,
    )
    monkeypatch.setattr(worker, "run_once", lambda *, stop_event=None: None)
    stop_event = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            worker.run_forever(stop_event)
        except BaseException as error:
            errors.append(error)

    runner = threading.Thread(target=run)
    blocker = sqlite3.connect(database_path, isolation_level=None)
    try:
        runner.start()
        _wait_until(lambda: repository.worker_status().state == "running")
        before_lock = repository.worker_status().last_seen_at
        assert before_lock is not None

        blocker.execute("BEGIN EXCLUSIVE")
        # This spans at least two heartbeat I/O timeouts. The prior one-miss
        # policy killed the worker before the legitimate writer released.
        threading.Event().wait(0.14)
        blocker.rollback()

        _wait_until(lambda: repository.worker_status().last_seen_at > before_lock)
        assert runner.is_alive()
        assert errors == []

        stop_event.set()
        runner.join(timeout=2)
        assert not runner.is_alive()
        assert not any(
            child.name == "task-worker-heartbeat-transient-heartbeat-lock"
            for child in multiprocessing.active_children()
        )
    finally:
        stop_event.set()
        runner.join(timeout=2)
        blocker.rollback()
        blocker.close()
        repository.close()


def test_persistent_sqlite_heartbeat_lock_fails_bounded_with_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "persistent-heartbeat-lock.db"
    url = f"sqlite:///{database_path}"
    migrate(url)
    repository = TaskRepository(create_engine_for_url(url), owns_engine=True)
    worker = TaskWorker(
        repository,
        worker_id="persistent-heartbeat-lock",
        poll_interval=0.01,
        heartbeat_interval=0.5,
        heartbeat_stop_timeout=0.3,
        heartbeat_io_timeout=0.05,
        heartbeat_contention_grace=0.1,
    )
    monkeypatch.setattr(worker, "run_once", lambda *, stop_event=None: None)
    stop_event = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            worker.run_forever(stop_event)
        except BaseException as error:
            errors.append(error)

    runner = threading.Thread(target=run)
    blocker = sqlite3.connect(database_path, isolation_level=None)
    try:
        runner.start()
        _wait_until(lambda: repository.worker_status().state == "running")
        blocker.execute("BEGIN EXCLUSIVE")

        # A persistent lock must fail at the contention deadline instead of
        # sleeping for the next regular 500 ms heartbeat interval.
        _wait_until(lambda: not runner.is_alive(), timeout=0.9)
        assert len(errors) == 1
        message = str(errors[0])
        assert "sqlalchemy.exc.OperationalError" in message
        assert "database is locked" in message
        assert "SQLITE_BUSY" in message
        assert not any(
            child.name == "task-worker-heartbeat-persistent-heartbeat-lock"
            for child in multiprocessing.active_children()
        )

        last_seen = repository.worker_status().last_seen_at
        assert last_seen is not None
        assert (
            repository.worker_status(
                now=last_seen + timedelta(seconds=0.2),
                stale_after=timedelta(seconds=0.1),
            ).state
            == "not_detected"
        )
    finally:
        stop_event.set()
        runner.join(timeout=2)
        blocker.rollback()
        blocker.close()
        repository.close()


def test_heartbeat_storage_failure_after_readiness_propagates_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    worker = TaskWorker(
        repository,
        worker_id="failure-after-ready",
        poll_interval=0.01,
        heartbeat_interval=0.05,
        heartbeat_stop_timeout=0.3,
    )
    monkeypatch.setattr(worker, "run_once", lambda *, stop_event=None: None)
    stop_event = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            worker.run_forever(stop_event)
        except BaseException as error:
            errors.append(error)

    runner = threading.Thread(target=run)
    try:
        runner.start()
        _wait_until(
            lambda: repository.worker_status().state == "running",
            timeout=6.0,
        )
        with repository.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE task_worker_heartbeat")
        _wait_until(lambda: not runner.is_alive())

        assert len(errors) == 1
        message = str(errors[0])
        assert "heartbeat process failed" in message
        assert "sqlalchemy.exc.OperationalError" in message
        assert "no such table: task_worker_heartbeat" in message
        assert not any(
            child.name == "task-worker-heartbeat-failure-after-ready"
            for child in multiprocessing.active_children()
        )
    finally:
        stop_event.set()
        runner.join(timeout=2)
        repository.close()


def test_worker_heartbeat_rejects_unsafe_identity_time_and_freshness(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(TaskValidationError):
            repository.record_worker_heartbeat(" host-1 ")
        with pytest.raises(TaskValidationError):
            repository.record_worker_heartbeat(
                "worker-1", now=datetime(2026, 7, 9, 2, 0)
            )
        with pytest.raises(TaskValidationError):
            repository.worker_status(stale_after=timedelta(0))
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        (sqlite.dialect(), "ON CONFLICT"),
        (postgresql.dialect(), "ON CONFLICT"),
    ],
)
def test_worker_heartbeat_upsert_compiles_for_supported_databases(
    dialect: object, expected: str
) -> None:
    statement_factory = getattr(repository_module, "_worker_heartbeat_upsert_statement")
    statement = statement_factory(
        dialect.name,
        worker_id="worker-1",
        sampled_at=datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc),
    )

    assert expected in str(statement.compile(dialect=dialect)).upper()


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


def test_repository_appends_ordered_immutable_events_for_success(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("demo.double", {"value": 21})
        claimed = repository.claim_next("worker-1")
        assert claimed is not None
        repository.set_progress(created.id, 0.5)
        repository.complete(created.id, {"value": 42})

        events = repository.list_events(created.id)

        assert [task_event.event_name for task_event in events] == [
            "task.created",
            "task.claimed",
            "task.progressed",
            "task.succeeded",
        ]
        assert [task_event.level for task_event in events] == [
            "info",
            "info",
            "info",
            "info",
        ]
        assert [task_event.progress for task_event in events] == [0.0, 0.0, 0.5, 1.0]
        assert events[0].detail == {"kind": "demo.double"}
        assert events[1].detail == {"worker_id": "worker-1"}
        assert events[2].detail == {}
        assert events[3].detail == {}
        assert all(task_event.task_id == created.id for task_event in events)
        assert events == sorted(events, key=lambda task_event: task_event.occurred_at)
        with pytest.raises(FrozenInstanceError):
            setattr(events[0], "level", "error")
        with pytest.raises(TypeError):
            cast(Any, events[0].detail)["secret"] = "hunter2"
    finally:
        repository.close()


def test_failure_and_cancellation_events_are_structured_and_secret_safe(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        failed_task = repository.create("secret.failure", {})
        assert repository.claim_next("worker-failure") is not None
        repository.fail(
            failed_task.id,
            {"code": "unsafe", "raw_exception": "database password is hunter2"},
        )

        queued_cancel = repository.create("cancel.queued", {})
        repository.request_cancel(queued_cancel.id)

        running_cancel = repository.create("cancel.running", {})
        assert repository.claim_next("worker-cancel") is not None
        repository.request_cancel(running_cancel.id)
        repository.complete(running_cancel.id, {"discarded": True})

        failed_events = repository.list_events(failed_task.id)
        assert [task_event.event_name for task_event in failed_events] == [
            "task.created",
            "task.claimed",
            "task.failed",
        ]
        assert failed_events[-1].level == "error"
        assert failed_events[-1].detail == {"code": "task_failed"}
        assert "hunter2" not in repr(failed_events)
        assert "raw_exception" not in repr(failed_events)

        assert [
            task_event.event_name
            for task_event in repository.list_events(queued_cancel.id)
        ] == ["task.created", "task.cancel_requested", "task.cancelled"]
        assert [
            task_event.event_name
            for task_event in repository.list_events(running_cancel.id)
        ] == [
            "task.created",
            "task.claimed",
            "task.cancel_requested",
            "task.cancelled",
        ]
    finally:
        repository.close()


def test_event_queries_are_recently_bounded_and_require_an_existing_task(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        created = repository.create("events", {})
        assert repository.claim_next("worker") is not None
        repository.set_progress(created.id, 0.25)
        repository.complete(created.id, {})

        assert [
            task_event.event_name
            for task_event in repository.list_events(created.id, limit=2)
        ] == ["task.progressed", "task.succeeded"]
        with pytest.raises(TaskValidationError):
            repository.list_events(created.id, limit=0)
        with pytest.raises(TaskValidationError):
            repository.list_events(created.id, limit=101)
        with pytest.raises(TaskNotFound):
            repository.list_events("missing")
    finally:
        repository.close()


def test_metrics_aggregate_statuses_and_terminal_durations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    sampled_times = iter(
        [
            base,
            base + timedelta(seconds=1),
            base + timedelta(seconds=1, milliseconds=100),
            base + timedelta(seconds=2),
            base + timedelta(seconds=3),
            base + timedelta(seconds=3, milliseconds=300),
            base + timedelta(seconds=4),
            base + timedelta(seconds=5),
            base + timedelta(seconds=5, milliseconds=50),
        ]
    )
    monkeypatch.setattr(repository_module, "_utc_now", lambda: next(sampled_times))
    repository = _repository(tmp_path)
    try:
        succeeded = repository.create("succeeded", {})
        assert repository.claim_next("worker-success") is not None
        succeeded = repository.complete(succeeded.id, {})

        failed = repository.create("failed", {})
        assert repository.claim_next("worker-failure") is not None
        failed = repository.fail(failed.id, {"code": "failure"})

        repository.create("queued", {})
        cancelled = repository.create("cancelled", {})
        repository.request_cancel(cancelled.id)

        metrics = repository.metrics()

        assert dict(metrics.by_status) == {
            "queued": 1,
            "running": 0,
            "succeeded": 1,
            "failed": 1,
            "cancelled": 1,
        }
        assert metrics.total == 4
        assert metrics.failure_count == 1
        assert metrics.completed_count == 2
        assert metrics.average_duration_ms == pytest.approx(200.0, abs=0.1)
        assert metrics.min_duration_ms == pytest.approx(100.0, abs=0.1)
        assert metrics.max_duration_ms == pytest.approx(300.0, abs=0.1)
        assert succeeded.duration_ms == pytest.approx(100.0)
        assert failed.duration_ms == pytest.approx(300.0)
        assert repository.get(cancelled.id).duration_ms is None
    finally:
        repository.close()


def test_event_insert_failure_rolls_back_the_state_transition(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'event-rollback.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = TaskRepository(engine)
    try:
        created = repository.create("rollback", {})

        def reject_event_insert(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("INSERT INTO TASK_EVENT"):
                raise RuntimeError("event insert rejected")

        event.listen(engine, "before_cursor_execute", reject_event_insert)
        try:
            with pytest.raises(RuntimeError, match="event insert rejected"):
                repository.claim_next("worker")
        finally:
            event.remove(engine, "before_cursor_execute", reject_event_insert)

        assert repository.get(created.id).status == "queued"
        assert [
            task_event.event_name for task_event in repository.list_events(created.id)
        ] == ["task.created"]
    finally:
        engine.dispose()


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
        task_events = repository.list_events(created.id)
        assert [task_event.event_name for task_event in task_events] == [
            "task.created",
            "task.claimed",
            "task.progressed",
            "task.cancel_requested",
            "task.cancelled",
        ]
        assert all(
            earlier.occurred_at < later.occurred_at
            for earlier, later in zip(task_events, task_events[1:])
        )
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


@pytest.mark.parametrize("heartbeat_interval", [0.0, -0.1, math.nan, math.inf])
def test_worker_rejects_invalid_heartbeat_interval(
    tmp_path: Path, heartbeat_interval: float
) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="Heartbeat interval"):
            TaskWorker(
                repository,
                worker_id="worker",
                heartbeat_interval=heartbeat_interval,
            )
    finally:
        repository.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("heartbeat_start_timeout", 0.0),
        ("heartbeat_start_timeout", True),
        ("heartbeat_stop_timeout", math.nan),
        ("heartbeat_io_timeout", math.inf),
        ("heartbeat_contention_grace", -0.1),
    ],
)
def test_worker_rejects_invalid_heartbeat_process_timeout(
    tmp_path: Path, field: str, value: float
) -> None:
    repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="Heartbeat .* timeout"):
            TaskWorker(
                repository,
                worker_id="worker",
                **{field: value},
            )
    finally:
        repository.close()


def test_default_heartbeat_startup_budget_tolerates_saturated_desktop_spawn(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    try:
        worker = TaskWorker(repository, worker_id="worker")

        assert worker._heartbeat_start_timeout == 15.0  # noqa: SLF001
        assert worker._heartbeat_contention_grace == 8.0  # noqa: SLF001
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


def test_worker_heartbeat_thread_publishes_when_idle_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    stop_event = threading.Event()
    worker = TaskWorker(
        repository,
        worker_id="heartbeat-idle",
        poll_interval=0.01,
        heartbeat_interval=0.02,
    )
    runner = threading.Thread(
        target=worker.run_forever,
        args=(stop_event,),
        name="worker-test-idle",
    )
    try:
        runner.start()
        _wait_until(lambda: repository.worker_status().state == "running")

        stop_event.set()
        runner.join(timeout=2)

        assert not runner.is_alive()
        assert all(
            thread.name != "task-worker-heartbeat-heartbeat-idle"
            for thread in threading.enumerate()
        )
    finally:
        stop_event.set()
        runner.join(timeout=2)
        repository.close()


def test_worker_heartbeat_advances_while_task_handler_is_blocked(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    stop_event = threading.Event()
    repository.create("heartbeat.block", {})
    worker = TaskWorker(
        repository,
        worker_id="heartbeat-blocked",
        poll_interval=0.01,
        heartbeat_interval=0.02,
    )

    def block(_task: object) -> dict[str, bool]:
        entered.set()
        assert release.wait(timeout=2)
        return {"released": True}

    worker.register("heartbeat.block", block)
    runner = threading.Thread(target=worker.run_forever, args=(stop_event,))
    try:
        runner.start()
        assert entered.wait(timeout=2)
        first_seen = repository.worker_status().last_seen_at
        assert first_seen is not None
        _wait_until(lambda: repository.worker_status().last_seen_at > first_seen)

        release.set()
        stop_event.set()
        runner.join(timeout=2)

        assert not runner.is_alive()
    finally:
        release.set()
        stop_event.set()
        runner.join(timeout=2)
        repository.close()


def test_worker_heartbeat_failure_stops_loop_and_cleans_executor(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    worker = TaskWorker(
        repository,
        worker_id="heartbeat-failure",
        poll_interval=0.01,
        heartbeat_interval=0.01,
    )

    with repository.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE task_worker_heartbeat")
    try:
        with pytest.raises(RuntimeError, match="failed before readiness"):
            worker.run_forever(threading.Event())

        assert not any(
            child.name == "task-worker-heartbeat-heartbeat-failure"
            for child in multiprocessing.active_children()
        )
        assert all(
            thread.name != "task-worker-heartbeat-heartbeat-failure"
            for thread in threading.enumerate()
        )
    finally:
        repository.close()


def test_multiple_worker_heartbeat_threads_stop_without_leaks(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    stops = [threading.Event(), threading.Event()]
    workers = [
        TaskWorker(
            repository,
            worker_id=f"heartbeat-multi-{index}",
            poll_interval=0.01,
            heartbeat_interval=0.02,
        )
        for index in range(2)
    ]
    runners = [
        threading.Thread(target=worker.run_forever, args=(stop,))
        for worker, stop in zip(workers, stops)
    ]
    try:
        for runner in runners:
            runner.start()
        _wait_until(lambda: repository.worker_status().state == "running")

        for stop in stops:
            stop.set()
        for runner in runners:
            runner.join(timeout=2)

        assert all(not runner.is_alive() for runner in runners)
        assert not any(
            thread.name.startswith("task-worker-heartbeat-heartbeat-multi-")
            for thread in threading.enumerate()
        )
    finally:
        for stop in stops:
            stop.set()
        for runner in runners:
            runner.join(timeout=2)
        repository.close()
