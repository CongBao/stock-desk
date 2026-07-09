from pathlib import Path
import multiprocessing
import sqlite3
import threading
import time
from typing import Any

import pytest

from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker


class _Scheduler:
    def __init__(self) -> None:
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1


def _repository(tmp_path: Path) -> TaskRepository:
    url = f"sqlite:///{tmp_path / 'production-worker.db'}"
    migrate(url)
    return TaskRepository(create_engine_for_url(url), owns_engine=True)


def _runtime(repository: TaskRepository, worker: TaskWorker) -> ProductionMarketWorker:
    return ProductionMarketWorker(
        engine=repository.engine,
        tasks=repository,
        source_settings=object(),  # type: ignore[arg-type]
        worker=worker,
        scheduler=_Scheduler(),  # type: ignore[arg-type]
        analysis_repository=object(),  # type: ignore[arg-type]
        model_catalog=object(),  # type: ignore[arg-type]
        lifecycle_guard=object(),  # type: ignore[arg-type]
    )


def _wait_until(condition: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        threading.Event().wait(0.01)
    assert condition()


def _run_in_thread(
    runtime: ProductionMarketWorker,
    stop_event: threading.Event,
) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run_forever(stop_event)
        except BaseException as error:
            errors.append(error)

    runner = threading.Thread(target=run, name="production-worker-test")
    runner.start()
    return runner, errors


def test_production_worker_run_forever_publishes_and_stops_heartbeat(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    stop_event = threading.Event()
    worker = TaskWorker(
        repository,
        worker_id="production-idle",
        poll_interval=0.01,
        heartbeat_interval=0.02,
    )
    runtime = _runtime(repository, worker)
    runner, errors = _run_in_thread(runtime, stop_event)
    try:
        _wait_until(lambda: repository.worker_status().state == "running")
        first_seen = repository.worker_status().last_seen_at
        assert first_seen is not None
        _wait_until(lambda: repository.worker_status().last_seen_at > first_seen)

        stop_event.set()
        runner.join(timeout=2)

        assert not runner.is_alive()
        assert errors == []
        assert all(
            thread.name != "task-worker-heartbeat-production-idle"
            for thread in threading.enumerate()
        )
    finally:
        stop_event.set()
        runner.join(timeout=2)
        repository.close()


def test_production_worker_heartbeat_advances_during_long_task(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    stop_event = threading.Event()
    repository.create("production.block", {})
    worker = TaskWorker(
        repository,
        worker_id="production-blocked",
        poll_interval=0.01,
        heartbeat_interval=0.02,
    )

    def block(_task: object) -> dict[str, bool]:
        entered.set()
        assert release.wait(timeout=2)
        return {"released": True}

    worker.register("production.block", block)
    runtime = _runtime(repository, worker)
    runner, errors = _run_in_thread(runtime, stop_event)
    try:
        assert entered.wait(timeout=2)
        first_seen = repository.worker_status().last_seen_at
        assert first_seen is not None
        _wait_until(lambda: repository.worker_status().last_seen_at > first_seen)

        release.set()
        stop_event.set()
        runner.join(timeout=2)

        assert not runner.is_alive()
        assert errors == []
        assert all(
            thread.name != "task-worker-heartbeat-production-blocked"
            for thread in threading.enumerate()
        )
    finally:
        release.set()
        stop_event.set()
        runner.join(timeout=2)
        repository.close()


def test_production_worker_survives_transient_sqlite_lock_during_long_task(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "production-worker.db"
    repository = _repository(tmp_path)
    entered = threading.Event()
    stop_event = threading.Event()
    worker = TaskWorker(
        repository,
        worker_id="production-transient-lock",
        poll_interval=0.01,
        heartbeat_interval=0.01,
        heartbeat_stop_timeout=0.3,
        heartbeat_io_timeout=0.05,
    )

    def hold_write_lock(_task: object) -> dict[str, bool]:
        blocker = sqlite3.connect(database_path, isolation_level=None)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            entered.set()
            threading.Event().wait(0.08)
            blocker.rollback()
        finally:
            blocker.close()
        return {"released": True}

    worker.register("production.transient-lock", hold_write_lock)
    runtime = _runtime(repository, worker)
    runner, errors = _run_in_thread(runtime, stop_event)
    try:
        _wait_until(lambda: repository.worker_status().state == "running")
        before_task = repository.worker_status().last_seen_at
        assert before_task is not None
        task = repository.create("production.transient-lock", {})

        assert entered.wait(timeout=2)
        _wait_until(lambda: repository.get(task.id).status == "succeeded")
        _wait_until(lambda: repository.worker_status().last_seen_at > before_task)
        assert runner.is_alive()
        assert errors == []

        stop_event.set()
        runner.join(timeout=2)
        assert not runner.is_alive()
        assert not any(
            child.name == "task-worker-heartbeat-production-transient-lock"
            for child in multiprocessing.active_children()
        )
    finally:
        stop_event.set()
        runner.join(timeout=2)
        repository.close()


def test_production_worker_propagates_heartbeat_failure_without_executor_leak(
    tmp_path: Path,
) -> None:
    class StopAfterFirstWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            result = super().wait(timeout)
            self.set()
            return result

    repository = _repository(tmp_path)
    worker = TaskWorker(
        repository,
        worker_id="production-failure",
        poll_interval=0.01,
        heartbeat_interval=0.01,
    )
    runtime = _runtime(repository, worker)

    with repository.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE task_worker_heartbeat")
    try:
        with pytest.raises(RuntimeError, match="failed before readiness"):
            runtime.run_forever(StopAfterFirstWait())

        assert not any(
            child.name == "task-worker-heartbeat-production-failure"
            for child in multiprocessing.active_children()
        )
        assert all(
            thread.name != "task-worker-heartbeat-production-failure"
            for thread in threading.enumerate()
        )
    finally:
        repository.close()
