from pathlib import Path
import multiprocessing
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

import stock_desk.market.worker_runtime as worker_runtime
from stock_desk.market.instruments import InstrumentNotFound
from stock_desk.market.update import MARKET_CATALOG_UPDATE_TASK_KIND
from stock_desk.market.worker_runtime import (
    ProductionMarketWorker,
    SettingsBackedCatalogUpdateHandler,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import DesktopCheckpointPause, TaskRepository
from stock_desk.tasks.worker import TaskWorker
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


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


def test_catalog_worker_acknowledges_checkpoint_after_manifest_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    identity = repository.database_identity
    routed = routed_instruments((instrument("600000.SH", "浦发银行"),))
    ingested: list[object] = []
    runtime_closed: list[bool] = []

    class SourceSettings:
        database_identity = identity

        @staticmethod
        def runtime_snapshot() -> object:
            return SimpleNamespace(
                configuration_fingerprint="fixture-fingerprint",
                redaction_values=lambda: (),
            )

    class Instruments:
        database_identity = identity

        @staticmethod
        def current_manifest() -> object:
            raise InstrumentNotFound("no current catalog")

        @staticmethod
        def ingest(outcome: object) -> object:
            ingested.append(outcome)
            return routed.manifest

    class Pools:
        database_identity = identity

    runtime = SimpleNamespace(
        router=SimpleNamespace(
            fetch_instruments=lambda *, previous_manifest: (
                routed
                if previous_manifest is None
                else pytest.fail("unexpected manifest")
            )
        ),
        close=lambda: runtime_closed.append(True),
    )
    monkeypatch.setattr(
        worker_runtime.MarketProviderRuntime,
        "build",
        staticmethod(lambda _snapshot, *, factory: runtime),
    )
    handler = SettingsBackedCatalogUpdateHandler(
        source_settings=SourceSettings(),  # type: ignore[arg-type]
        instruments=Instruments(),  # type: ignore[arg-type]
        pools=Pools(),  # type: ignore[arg-type]
        tasks=repository,
        provider_factory=object(),  # type: ignore[arg-type]
    )
    task = repository.create(MARKET_CATALOG_UPDATE_TASK_KIND, {})
    claimed = repository.claim_next("catalog-checkpoint-worker")
    assert isinstance(claimed, TaskSnapshot)
    assert claimed.id == task.id
    repository.request_desktop_checkpoint()
    try:
        with pytest.raises(DesktopCheckpointPause):
            handler(claimed)

        assert ingested == [routed]
        assert runtime_closed == [True]
        assert any(
            event.event_name == "task.desktop_checkpointed"
            for event in repository.list_events(task.id)
        )
    finally:
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
