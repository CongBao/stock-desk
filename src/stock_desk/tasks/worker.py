from collections.abc import Callable, Mapping
import logging
import math
import os
import signal
import socket
import threading
from typing import Any, TypeAlias

from stock_desk.config import get_settings
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import TaskRepository, TaskValidationError


TaskHandler: TypeAlias = Callable[[TaskSnapshot], Mapping[str, Any]]

_UNKNOWN_KIND_ERROR = {"code": "unknown_task_kind"}
_HANDLER_FAILURE_ERROR = {"code": "task_handler_failed"}
_MINIMUM_IDLE_WAIT_SECONDS = 0.01
_LOGGER = logging.getLogger(__name__)


class TaskWorker:
    """Claim and execute at most one durable task at a time."""

    def __init__(
        self,
        repository: TaskRepository,
        *,
        worker_id: str,
        poll_interval: float = 1.0,
    ) -> None:
        if not worker_id or worker_id != worker_id.strip() or len(worker_id) > 255:
            raise ValueError("Worker id must contain 1 to 255 characters")
        if not math.isfinite(poll_interval) or poll_interval < 0:
            raise ValueError("Poll interval must be finite and nonnegative")
        self._repository = repository
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, kind: str, handler: TaskHandler) -> None:
        if not kind or kind != kind.strip() or len(kind) > 64:
            raise ValueError("Task kind must contain 1 to 64 characters")
        self._handlers[kind] = handler

    def run_once(self) -> TaskSnapshot | None:
        task = self._repository.claim_next(self._worker_id)
        if task is None:
            return None

        handler = self._handlers.get(task.kind)
        if handler is None:
            return self._repository.fail(task.id, _UNKNOWN_KIND_ERROR)

        try:
            result = dict(handler(task))
        except Exception as error:
            self._log_handler_failure(task, error)
            return self._repository.fail(task.id, _HANDLER_FAILURE_ERROR)
        try:
            return self._repository.complete(task.id, result)
        except TaskValidationError as error:
            self._log_handler_failure(task, error)
            return self._repository.fail(task.id, _HANDLER_FAILURE_ERROR)

    @staticmethod
    def _log_handler_failure(task: TaskSnapshot, error: Exception) -> None:
        _LOGGER.warning(
            "Task handler failed (task_id=%s, kind=%s, exception_type=%s)",
            task.id,
            task.kind,
            type(error).__name__,
        )

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            completed = self.run_once()
            if completed is None:
                stop_event.wait(max(self._poll_interval, _MINIMUM_IDLE_WAIT_SECONDS))


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
    repository = TaskRepository.open(settings.database_url)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    worker = TaskWorker(repository, worker_id=worker_id)
    worker.register("demo.double", demo_double)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    logger.info("Stock Desk task worker ready (worker_id=%s)", worker_id)
    try:
        worker.run_forever(stop_event)
    finally:
        repository.close()


if __name__ == "__main__":
    main()
