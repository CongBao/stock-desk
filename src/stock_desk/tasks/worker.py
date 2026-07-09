from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import logging
import math
import multiprocessing
import os
import signal
import sqlite3
import socket
import threading
import time
from typing import Any, TypeAlias

from sqlalchemy.exc import DBAPIError, OperationalError

from stock_desk.config import get_settings
from stock_desk.tasks.models import TaskClaim, TaskSnapshot
from stock_desk.tasks.repository import (
    TaskConflict,
    TaskRepository,
    TaskValidationError,
)


TaskHandler: TypeAlias = Callable[[TaskSnapshot], Mapping[str, Any]]
ClaimedTaskHandler: TypeAlias = Callable[[TaskClaim], Mapping[str, Any]]

_UNKNOWN_KIND_ERROR = {"code": "unknown_task_kind"}
_HANDLER_FAILURE_ERROR = {"code": "task_handler_failed"}
_MINIMUM_IDLE_WAIT_SECONDS = 0.01
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
_DEFAULT_HEARTBEAT_START_TIMEOUT_SECONDS = 5.0
_DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS = 1.0
_DEFAULT_HEARTBEAT_IO_TIMEOUT_SECONDS = 1.0
_PARENT_LIVENESS_POLL_SECONDS = 0.05
_LOGGER = logging.getLogger(__name__)

_HeartbeatFailurePayload: TypeAlias = tuple[str, str, str, str | None]


def _heartbeat_failure_payload(error: BaseException) -> _HeartbeatFailurePayload:
    original = error.orig if isinstance(error, DBAPIError) else error
    error_code = getattr(original, "sqlite_errorname", None)
    return (
        f"{type(error).__module__}.{type(error).__qualname__}",
        f"{type(original).__module__}.{type(original).__qualname__}",
        str(original),
        error_code if isinstance(error_code, str) else None,
    )


def _is_transient_sqlite_contention(error: BaseException) -> bool:
    if not isinstance(error, OperationalError):
        return False
    original = error.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False
    error_code = getattr(original, "sqlite_errorcode", None)
    return isinstance(error_code, int) and error_code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


def _format_heartbeat_failure(payload: object) -> str:
    if (
        not isinstance(payload, tuple)
        or len(payload) != 4
        or not all(isinstance(value, str) for value in payload[:3])
        or (payload[3] is not None and not isinstance(payload[3], str))
    ):
        return "invalid heartbeat failure status"
    error_type, original_type, message, error_code = payload
    detail = f"{error_type} ({original_type}): {message}"
    return f"{detail} [{error_code}]" if error_code is not None else detail


def _watch_parent_liveness(
    *,
    stop_event: Any,
    parent_liveness_receiver: Any,
    shutdown_event: threading.Event,
) -> None:
    while not shutdown_event.is_set():
        try:
            parent_closed = parent_liveness_receiver.poll(_PARENT_LIVENESS_POLL_SECONDS)
        except (EOFError, OSError):
            parent_closed = True
        if not parent_closed:
            continue
        if shutdown_event.is_set() or stop_event.is_set():
            return
        try:
            parent_liveness_receiver.recv_bytes()
        except (EOFError, OSError):
            pass
        if shutdown_event.is_set() or stop_event.is_set():
            return
        os._exit(1)


def _heartbeat_process_main(
    database_url: str,
    worker_id: str,
    interval: float,
    io_timeout: float,
    stop_event: Any,
    status_sender: Any,
    parent_liveness_receiver: Any,
) -> None:
    from stock_desk.storage.database import create_engine_for_url

    repository: TaskRepository | None = None
    watchdog_shutdown = threading.Event()
    watchdog = threading.Thread(
        target=_watch_parent_liveness,
        kwargs={
            "stop_event": stop_event,
            "parent_liveness_receiver": parent_liveness_receiver,
            "shutdown_event": watchdog_shutdown,
        },
        name=f"task-worker-parent-watchdog-{worker_id}",
        daemon=True,
    )
    watchdog.start()
    try:
        repository = TaskRepository(
            create_engine_for_url(database_url),
            owns_engine=True,
        )
        repository.record_worker_heartbeat(
            worker_id,
            timeout_seconds=io_timeout,
        )
        status_sender.send(("ready", None))
        transient_contention_seen = False
        while not stop_event.wait(interval):
            try:
                repository.record_worker_heartbeat(
                    worker_id,
                    timeout_seconds=io_timeout,
                )
            except BaseException as error:
                if stop_event.is_set():
                    return
                if (
                    _is_transient_sqlite_contention(error)
                    and not transient_contention_seen
                ):
                    transient_contention_seen = True
                    continue
                raise
            else:
                transient_contention_seen = False
    except BaseException as error:
        try:
            status_sender.send(("error", _heartbeat_failure_payload(error)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if repository is not None:
            repository.close()
        watchdog_shutdown.set()
        watchdog.join(timeout=_PARENT_LIVENESS_POLL_SECONDS * 2)
        status_sender.close()
        parent_liveness_receiver.close()


class _HeartbeatProcessController:
    def __init__(
        self,
        *,
        database_url: str,
        worker_id: str,
        interval: float,
        start_timeout: float,
        stop_timeout: float,
        io_timeout: float,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        parent_liveness_receiver, parent_liveness_sender = context.Pipe(duplex=False)
        self._receiver = receiver
        self._sender = sender
        self._parent_liveness_receiver = parent_liveness_receiver
        self._parent_liveness_sender = parent_liveness_sender
        self._stop_event = context.Event()
        self._process = context.Process(
            target=_heartbeat_process_main,
            args=(
                database_url,
                worker_id,
                interval,
                io_timeout,
                self._stop_event,
                sender,
                parent_liveness_receiver,
            ),
            name=f"task-worker-heartbeat-{worker_id}",
        )
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._failure_detail: str | None = None
        self._started = False
        self._stopping = False

    def _consume_status(self) -> str | None:
        try:
            message = self._receiver.recv()
        except (EOFError, OSError):
            return None
        if (
            not isinstance(message, tuple)
            or len(message) != 2
            or message[0] not in {"ready", "error"}
        ):
            self._failure_detail = "invalid heartbeat status"
            return "error"
        if message[0] == "error":
            self._failure_detail = _format_heartbeat_failure(message[1])
        return str(message[0])

    def _failure_message(self, summary: str) -> str:
        if self._failure_detail is None:
            return summary
        return f"{summary}: {self._failure_detail}"

    def start(self) -> None:
        try:
            self._process.start()
        except BaseException:
            self._sender.close()
            self._receiver.close()
            self._parent_liveness_receiver.close()
            self._parent_liveness_sender.close()
            self._process.close()
            raise
        self._started = True
        self._sender.close()
        self._parent_liveness_receiver.close()
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if self._receiver.poll(0.01):
                status = self._consume_status()
                if status == "ready":
                    return
                self.stop()
                raise RuntimeError(
                    self._failure_message(
                        "Task worker heartbeat failed before readiness"
                    )
                )
            if not self._process.is_alive():
                self.stop()
                raise RuntimeError(
                    self._failure_message(
                        "Task worker heartbeat failed before readiness"
                    )
                )
        self.stop()
        raise RuntimeError(
            "Task worker heartbeat did not become ready within "
            f"{self._start_timeout:.3f} seconds; subprocess was stopped"
        )

    def raise_if_failed(self) -> None:
        if self._failure_detail is not None:
            raise RuntimeError(
                self._failure_message("Task worker heartbeat process failed")
            )
        if self._stopping:
            return
        if self._receiver.poll():
            self._consume_status()
        if self._failure_detail is not None:
            raise RuntimeError(
                self._failure_message("Task worker heartbeat process failed")
            )
        if self._started and not self._stopping and not self._process.is_alive():
            self.stop()
            if self._failure_detail is not None:
                raise RuntimeError(
                    self._failure_message("Task worker heartbeat process failed")
                )
            raise RuntimeError("Task worker heartbeat process exited")

    def stop(self) -> None:
        if not self._started or self._stopping:
            return
        self._stopping = True
        self._stop_event.set()
        self._parent_liveness_sender.close()
        self._process.join(timeout=self._stop_timeout)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=self._stop_timeout)
        if self._process.is_alive() and hasattr(self._process, "kill"):
            self._process.kill()
            self._process.join(timeout=self._stop_timeout)
        if self._process.is_alive():
            raise RuntimeError("Task worker heartbeat process did not stop")
        if self._receiver.poll(self._stop_timeout):
            self._consume_status()
        self._receiver.close()
        self._process.close()


class TaskWorker:
    """Claim and execute at most one durable task at a time."""

    def __init__(
        self,
        repository: TaskRepository,
        *,
        worker_id: str,
        poll_interval: float = 1.0,
        heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        heartbeat_start_timeout: float = _DEFAULT_HEARTBEAT_START_TIMEOUT_SECONDS,
        heartbeat_stop_timeout: float = _DEFAULT_HEARTBEAT_STOP_TIMEOUT_SECONDS,
        heartbeat_io_timeout: float = _DEFAULT_HEARTBEAT_IO_TIMEOUT_SECONDS,
    ) -> None:
        if not worker_id or worker_id != worker_id.strip() or len(worker_id) > 255:
            raise ValueError("Worker id must contain 1 to 255 characters")
        if not math.isfinite(poll_interval) or poll_interval < 0:
            raise ValueError("Poll interval must be finite and nonnegative")
        if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
            raise ValueError("Heartbeat interval must be finite and positive")
        for value, label in (
            (heartbeat_start_timeout, "start"),
            (heartbeat_stop_timeout, "stop"),
            (heartbeat_io_timeout, "I/O"),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"Heartbeat {label} timeout must be finite and positive"
                )
        self._repository = repository
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_start_timeout = heartbeat_start_timeout
        self._heartbeat_stop_timeout = heartbeat_stop_timeout
        self._heartbeat_io_timeout = heartbeat_io_timeout
        self._handlers: dict[str, TaskHandler] = {}
        self._claimed_handlers: dict[str, ClaimedTaskHandler] = {}

    def register(self, kind: str, handler: TaskHandler) -> None:
        if not kind or kind != kind.strip() or len(kind) > 64:
            raise ValueError("Task kind must contain 1 to 64 characters")
        self._handlers[kind] = handler

    def register_claimed(self, kind: str, handler: ClaimedTaskHandler) -> None:
        if not kind or kind != kind.strip() or len(kind) > 64:
            raise ValueError("Task kind must contain 1 to 64 characters")
        self._claimed_handlers[kind] = handler

    @property
    def registered_claimed_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._claimed_handlers))

    def run_once(self) -> TaskSnapshot | None:
        claimed = self._repository.claim_next(self._worker_id)
        if claimed is None:
            return None
        if isinstance(claimed, TaskClaim):
            claim: TaskClaim | None = claimed
            task = claimed.snapshot
            claimed_handler = self._claimed_handlers.get(task.kind)
            if claimed_handler is None:
                return self._fail_current(task, claim, _UNKNOWN_KIND_ERROR)
            invoke_claimed = claimed_handler
        else:
            claim = None
            task = claimed
            handler = self._handlers.get(task.kind)
            if handler is None:
                return self._fail_current(task, claim, _UNKNOWN_KIND_ERROR)
            invoke_legacy = handler

        try:
            result = dict(
                invoke_claimed(claimed)
                if isinstance(claimed, TaskClaim)
                else invoke_legacy(task)
            )
        except Exception as error:
            self._log_handler_failure(task, error)
            return self._fail_current(task, claim, _HANDLER_FAILURE_ERROR)
        try:
            return self._repository.complete(
                task.id,
                result,
                claim_token=claim.claim_token if claim is not None else None,
            )
        except TaskValidationError as error:
            self._log_handler_failure(task, error)
            return self._fail_current(task, claim, _HANDLER_FAILURE_ERROR)
        except TaskConflict:
            return self._repository.get(task.id)

    def _fail_current(
        self,
        task: TaskSnapshot,
        claim: TaskClaim | None,
        error: Mapping[str, Any],
    ) -> TaskSnapshot:
        try:
            return self._repository.fail(
                task.id,
                error,
                claim_token=claim.claim_token if claim is not None else None,
            )
        except TaskConflict:
            return self._repository.get(task.id)

    @staticmethod
    def _log_handler_failure(task: TaskSnapshot, error: Exception) -> None:
        _LOGGER.warning(
            "Task handler failed (task_id=%s, kind=%s, exception_type=%s)",
            task.id,
            task.kind,
            type(error).__name__,
        )

    @contextmanager
    def heartbeat_lifecycle(
        self, _stop_event: threading.Event
    ) -> Iterator[_HeartbeatProcessController]:
        heartbeat = _HeartbeatProcessController(
            database_url=self._repository.engine.url.render_as_string(
                hide_password=False
            ),
            worker_id=self._worker_id,
            interval=self._heartbeat_interval,
            start_timeout=self._heartbeat_start_timeout,
            stop_timeout=self._heartbeat_stop_timeout,
            io_timeout=self._heartbeat_io_timeout,
        )
        heartbeat.start()
        try:
            yield heartbeat
        finally:
            heartbeat.stop()
        heartbeat.raise_if_failed()

    def run_forever(self, stop_event: threading.Event) -> None:
        with self.heartbeat_lifecycle(stop_event) as heartbeat:
            while not stop_event.is_set():
                heartbeat.raise_if_failed()
                completed = self.run_once()
                if completed is None:
                    stop_event.wait(
                        max(self._poll_interval, _MINIMUM_IDLE_WAIT_SECONDS)
                    )
            heartbeat.raise_if_failed()


def demo_double(task: TaskSnapshot) -> Mapping[str, Any]:
    """Double the numeric ``value`` in a demo task payload."""
    value = task.payload.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Demo task value must be numeric")
    return {"value": value * 2}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    settings = get_settings()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    from stock_desk.market.worker_runtime import ProductionMarketWorker

    runtime = ProductionMarketWorker.open(settings, worker_id=worker_id)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    logger.info("Stock Desk task worker ready (worker_id=%s)", worker_id)
    try:
        runtime.run_forever(stop_event)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
