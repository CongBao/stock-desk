from __future__ import annotations

import multiprocessing
import os
from datetime import timedelta
from pathlib import Path
import signal
import sqlite3
import threading
import time
from typing import Any

import pytest

from stock_desk import desktop
from stock_desk.storage.database import migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker


def _blocked_worker_parent(
    database_url: str,
    status_sender: Any,
    heartbeat_io_timeout: float = 0.2,
) -> None:
    repository = TaskRepository.open(database_url)
    worker = TaskWorker(
        repository,
        worker_id=f"desktop-parent-liveness-{os.getpid()}",
        heartbeat_interval=0.05,
        heartbeat_start_timeout=2.0,
        heartbeat_stop_timeout=0.2,
        heartbeat_io_timeout=heartbeat_io_timeout,
    )

    def block(_task: object) -> dict[str, bool]:
        status_sender.send(("task-entered", None))
        status_sender.close()
        time.sleep(30)
        return {"released": True}

    worker.register("desktop.parent-liveness", block)
    try:
        with worker.heartbeat_lifecycle(threading.Event()) as heartbeat:
            status_sender.send(
                ("heartbeat-ready", heartbeat._process.pid)  # noqa: SLF001
            )
            worker.run_once()
    finally:
        repository.close()


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_handle = kernel32.OpenProcess(0x1000, False, pid)
        if not process_handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            return (
                bool(
                    kernel32.GetExitCodeProcess(
                        process_handle,
                        ctypes.byref(exit_code),
                    )
                )
                and exit_code.value == 259
            )
        finally:
            kernel32.CloseHandle(process_handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(condition: Any, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    assert condition()


@pytest.mark.parametrize("hard_stop", ["desktop-timeout", "kill"])
def test_heartbeat_exits_when_blocked_desktop_worker_is_hard_stopped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hard_stop: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'parent-liveness-{hard_stop}.db'}"
    migrate(database_url)
    observer = TaskRepository.open(database_url)
    observer.create("desktop.parent-liveness", {})
    context = multiprocessing.get_context("spawn")
    status_receiver, status_sender = context.Pipe(duplex=False)
    worker_process = context.Process(
        target=_blocked_worker_parent,
        args=(database_url, status_sender),
        name=f"desktop-blocked-worker-{hard_stop}",
    )
    worker_process.start()
    status_sender.close()
    heartbeat_pid: int | None = None
    try:
        assert status_receiver.poll(5)
        ready_status, received_pid = status_receiver.recv()
        assert ready_status == "heartbeat-ready"
        assert isinstance(received_pid, int)
        heartbeat_pid = received_pid
        assert status_receiver.poll(2)
        assert status_receiver.recv() == ("task-entered", None)
        freshness = timedelta(seconds=10)
        first_seen = observer.worker_status(stale_after=freshness).last_seen_at
        assert first_seen is not None
        _wait_until(
            lambda: (
                observer.worker_status(stale_after=freshness).last_seen_at > first_seen
            )
        )

        if hard_stop == "desktop-timeout":
            monkeypatch.setattr(desktop, "_PROCESS_STOP_TIMEOUT_SECONDS", 0.1)
            desktop._stop_process(worker_process, threading.Event())
        else:
            worker_process.join(timeout=0.1)
            assert worker_process.is_alive()
            worker_process.kill()
            worker_process.join(timeout=2)

        assert not worker_process.is_alive()
        _wait_until(lambda: not _pid_is_alive(received_pid))
        stopped_at = observer.worker_status(stale_after=freshness).last_seen_at
        assert stopped_at is not None
        time.sleep(0.25)
        assert observer.worker_status(stale_after=freshness).last_seen_at == stopped_at
    finally:
        if worker_process.is_alive():
            worker_process.kill()
            worker_process.join(timeout=2)
        if heartbeat_pid is not None and _pid_is_alive(heartbeat_pid):
            os.kill(heartbeat_pid, signal.SIGTERM)
            time.sleep(0.1)
        if heartbeat_pid is not None and _pid_is_alive(heartbeat_pid):
            os.kill(heartbeat_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            _wait_until(lambda: not _pid_is_alive(heartbeat_pid))
        status_receiver.close()
        worker_process.close()
        observer.close()


def test_parent_watchdog_exits_while_heartbeat_database_write_is_blocked(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "blocked-parent-liveness.db"
    database_url = f"sqlite:///{database_path}"
    migrate(database_url)
    observer = TaskRepository.open(database_url)
    observer.create("desktop.parent-liveness", {})
    context = multiprocessing.get_context("spawn")
    status_receiver, status_sender = context.Pipe(duplex=False)
    worker_process = context.Process(
        target=_blocked_worker_parent,
        args=(database_url, status_sender, 30.0),
        name="desktop-blocked-worker-database-io",
    )
    blocker: sqlite3.Connection | None = None
    heartbeat_pid: int | None = None
    worker_process.start()
    status_sender.close()
    try:
        assert status_receiver.poll(5)
        ready_status, received_pid = status_receiver.recv()
        assert ready_status == "heartbeat-ready"
        assert isinstance(received_pid, int)
        heartbeat_pid = received_pid
        assert status_receiver.poll(2)
        assert status_receiver.recv() == ("task-entered", None)

        blocker = sqlite3.connect(database_path, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        time.sleep(0.2)
        worker_process.kill()
        worker_process.join(timeout=2)
        assert not worker_process.is_alive()

        started = time.monotonic()
        _wait_until(lambda: not _pid_is_alive(received_pid), timeout=1.5)
        assert time.monotonic() - started < 1.5
        stopped_at = observer.worker_status(
            stale_after=timedelta(seconds=10)
        ).last_seen_at
        assert stopped_at is not None
        blocker.rollback()
        blocker.close()
        blocker = None
        time.sleep(0.25)
        assert (
            observer.worker_status(stale_after=timedelta(seconds=10)).last_seen_at
            == stopped_at
        )
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        if worker_process.is_alive():
            worker_process.kill()
            worker_process.join(timeout=2)
        if heartbeat_pid is not None and _pid_is_alive(heartbeat_pid):
            _wait_until(lambda: not _pid_is_alive(heartbeat_pid))
        status_receiver.close()
        worker_process.close()
        observer.close()
