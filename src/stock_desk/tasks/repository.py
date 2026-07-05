from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import JSON, Engine, bindparam, case, insert, null, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql.elements import ColumnElement

from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import TaskRun
from stock_desk.tasks.models import TaskSnapshot, TaskStatus


class TaskRepositoryError(Exception):
    """Base class for task persistence errors."""


class TaskNotFound(TaskRepositoryError):
    """Raised when a task id does not exist."""


class TaskConflict(TaskRepositoryError):
    """Raised when a state transition is not allowed."""


class TaskValidationError(TaskRepositoryError, ValueError):
    """Raised when task input is invalid."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validated_json_object(
    value: Mapping[str, Any], *, field_name: str
) -> dict[str, Any]:
    copied = dict(value)
    try:
        json.dumps(copied, allow_nan=False)
    except (RecursionError, TypeError, ValueError) as error:
        raise TaskValidationError(
            f"Task {field_name} must be a JSON-compatible object"
        ) from error

    stack: list[Any] = [copied]
    visited_containers: set[int] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise TaskValidationError(
                        f"Task {field_name} JSON object keys must be strings"
                    )
                stack.append(nested)
        elif isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in visited_containers:
                continue
            visited_containers.add(identity)
            stack.extend(item)
    return copied


def _transition_time(sampled_at: datetime) -> ColumnElement[datetime]:
    """Keep task timestamps monotonic when clocks regress or writers wait."""
    return case(
        (TaskRun.updated_at > sampled_at, TaskRun.updated_at),
        else_=sampled_at,
    )


def _snapshot(row: RowMapping) -> TaskSnapshot:
    created_at = _aware_utc(cast(datetime, row["created_at"]))
    updated_at = _aware_utc(cast(datetime, row["updated_at"]))
    if created_at is None or updated_at is None:
        raise RuntimeError("Task timestamps must not be null")
    return TaskSnapshot(
        id=cast(str, row["id"]),
        kind=cast(str, row["kind"]),
        status=cast(TaskStatus, row["status"]),
        progress=cast(float, row["progress"]),
        payload=MappingProxyType(dict(cast(Mapping[str, Any], row["payload_json"]))),
        result=(
            MappingProxyType(dict(cast(Mapping[str, Any], row["result_json"])))
            if row["result_json"] is not None
            else None
        ),
        error=(
            MappingProxyType(dict(cast(Mapping[str, Any], row["error_json"])))
            if row["error_json"] is not None
            else None
        ),
        cancel_requested=cast(bool, row["cancel_requested"]),
        worker_id=cast(str | None, row["worker_id"]),
        created_at=created_at,
        updated_at=updated_at,
        started_at=_aware_utc(cast(datetime | None, row["started_at"])),
        finished_at=_aware_utc(cast(datetime | None, row["finished_at"])),
    )


class TaskRepository:
    """Transactional access to the durable task queue."""

    def __init__(self, engine: Engine, *, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine

    @classmethod
    def open(cls, url: str) -> "TaskRepository":
        """Migrate and open an owned repository for an application process."""
        migrate(url)
        return cls(create_engine_for_url(url), owns_engine=True)

    def create(self, kind: str, payload: Mapping[str, Any]) -> TaskSnapshot:
        if not kind or kind != kind.strip() or len(kind) > 64:
            raise TaskValidationError("Task kind must contain 1 to 64 characters")
        validated_payload = _validated_json_object(payload, field_name="payload")
        now = _utc_now()
        values = {
            "id": str(uuid4()),
            "kind": kind,
            "status": "queued",
            "progress": 0.0,
            "payload_json": validated_payload,
            "result_json": None,
            "error_json": None,
            "cancel_requested": False,
            "worker_id": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
        }
        statement = insert(TaskRun).values(**values).returning(TaskRun)
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one()
        return _snapshot(row)

    def get(self, task_id: str) -> TaskSnapshot:
        statement = select(TaskRun).where(TaskRun.id == task_id)
        with self._engine.connect() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise TaskNotFound(f"Task {task_id} was not found")
        return _snapshot(row)

    def list_recent(self, *, limit: int = 50) -> list[TaskSnapshot]:
        if not 1 <= limit <= 100:
            raise TaskValidationError("Task list limit must be between 1 and 100")
        statement = (
            select(TaskRun)
            .order_by(TaskRun.created_at.desc(), TaskRun.id.desc())
            .limit(limit)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_snapshot(row) for row in rows]

    def claim_next(self, worker_id: str) -> TaskSnapshot | None:
        if not worker_id or worker_id != worker_id.strip() or len(worker_id) > 255:
            raise TaskValidationError("Worker id must contain 1 to 255 characters")
        now = _utc_now()
        transition_time = _transition_time(now)
        candidate = (
            select(TaskRun.id)
            .where(TaskRun.status == "queued", TaskRun.cancel_requested.is_(False))
            .order_by(TaskRun.created_at, TaskRun.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == candidate,
                TaskRun.status == "queued",
                TaskRun.cancel_requested.is_(False),
            )
            .values(
                status="running",
                worker_id=worker_id,
                started_at=transition_time,
                updated_at=transition_time,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return _snapshot(row) if row is not None else None

    def set_progress(self, task_id: str, progress: float) -> TaskSnapshot:
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0.0 <= progress <= 1.0
        ):
            raise TaskValidationError(
                "Task progress must be a finite number from 0 to 1"
            )
        now = _utc_now()
        transition_time = _transition_time(now)
        statement = (
            update(TaskRun)
            .where(TaskRun.id == task_id, TaskRun.status == "running")
            .values(progress=float(progress), updated_at=transition_time)
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is not None:
            return _snapshot(row)
        current = self.get(task_id)
        raise TaskConflict(f"Task {current.id} is not running")

    def complete(self, task_id: str, result: Mapping[str, Any]) -> TaskSnapshot:
        validated_result = _validated_json_object(result, field_name="result")
        now = _utc_now()
        transition_time = _transition_time(now)
        cancelling = TaskRun.cancel_requested.is_(True)
        statement = (
            update(TaskRun)
            .where(TaskRun.id == task_id, TaskRun.status == "running")
            .values(
                status=case((cancelling, "cancelled"), else_="succeeded"),
                progress=case((cancelling, TaskRun.progress), else_=1.0),
                result_json=case(
                    (cancelling, null()),
                    else_=bindparam(
                        "_completed_result", validated_result, type_=JSON()
                    ),
                ),
                error_json=None,
                updated_at=transition_time,
                finished_at=transition_time,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is not None:
            return _snapshot(row)
        current = self.get(task_id)
        if current.status == "succeeded":
            return current
        raise TaskConflict(f"Task {current.id} cannot be completed")

    def fail(self, task_id: str, error: Mapping[str, Any]) -> TaskSnapshot:
        validated_error = _validated_json_object(error, field_name="error")
        now = _utc_now()
        transition_time = _transition_time(now)
        cancelling = TaskRun.cancel_requested.is_(True)
        statement = (
            update(TaskRun)
            .where(TaskRun.id == task_id, TaskRun.status == "running")
            .values(
                status=case((cancelling, "cancelled"), else_="failed"),
                result_json=None,
                error_json=case(
                    (cancelling, null()),
                    else_=bindparam("_failure_error", validated_error, type_=JSON()),
                ),
                updated_at=transition_time,
                finished_at=transition_time,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is not None:
            return _snapshot(row)
        current = self.get(task_id)
        if current.status == "failed":
            return current
        raise TaskConflict(f"Task {current.id} cannot be failed")

    def request_cancel(self, task_id: str) -> TaskSnapshot:
        now = _utc_now()
        transition_time = _transition_time(now)
        queued_statement = (
            update(TaskRun)
            .where(TaskRun.id == task_id, TaskRun.status == "queued")
            .values(
                status="cancelled",
                cancel_requested=True,
                updated_at=transition_time,
                finished_at=transition_time,
            )
            .returning(TaskRun)
        )
        running_statement = (
            update(TaskRun)
            .where(TaskRun.id == task_id, TaskRun.status == "running")
            .values(cancel_requested=True, updated_at=transition_time)
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(queued_statement).mappings().one_or_none()
            if row is None:
                row = connection.execute(running_statement).mappings().one_or_none()
        if row is not None:
            return _snapshot(row)
        current = self.get(task_id)
        if current.status == "cancelled":
            return current
        raise TaskConflict(f"Task {current.id} cannot be cancelled")

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()
