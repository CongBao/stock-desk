from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
import math
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Engine,
    bindparam,
    case,
    func,
    insert,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.elements import ColumnElement

from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
    create_engine_for_url,
    migrate,
)
from stock_desk.storage.models import TaskEvent, TaskRun
from stock_desk.tasks.models import (
    TaskClaim,
    TaskEventLevel,
    TaskEventSnapshot,
    TaskMetricsSnapshot,
    TaskSnapshot,
    TaskStatus,
)


class TaskRepositoryError(Exception):
    """Base class for task persistence errors."""


class TaskNotFound(TaskRepositoryError):
    """Raised when a task id does not exist."""


class TaskConflict(TaskRepositoryError):
    """Raised when a state transition is not allowed."""


class TaskValidationError(TaskRepositoryError, ValueError):
    """Raised when task input is invalid."""


_MAX_JSON_DEPTH = 128
_BACKTEST_TASK_KIND = "backtest.run"
_DEFAULT_LEASE_DURATION = timedelta(minutes=2)
_MAX_LEASE_DURATION = timedelta(hours=1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validated_aware_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TaskValidationError(f"Task {field_name} must be an aware datetime")
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(timezone.utc)
    except Exception as error:
        raise TaskValidationError(
            f"Task {field_name} must be an aware datetime"
        ) from error
    if offset is None:
        raise TaskValidationError(f"Task {field_name} must be an aware datetime")
    return normalized


def _validated_uuid(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise TaskValidationError(f"Task {field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise TaskValidationError(
            f"Task {field_name} must be a canonical UUID"
        ) from error
    if str(parsed) != value:
        raise TaskValidationError(f"Task {field_name} must be a canonical UUID")
    return value


def validate_lease_duration(value: object) -> timedelta:
    if (
        not isinstance(value, timedelta)
        or value <= timedelta(0)
        or value > _MAX_LEASE_DURATION
    ):
        raise TaskValidationError(
            "Task lease duration must be positive and at most one hour"
        )
    return value


def _validated_json_object(
    value: Mapping[str, Any], *, field_name: str
) -> dict[str, Any]:
    normalized = _normalize_json_object(value, field_name=field_name)
    try:
        encoded = json.dumps(normalized, allow_nan=False)
        decoded = json.loads(encoded)
    except UnicodeError as error:
        raise _json_validation_error(field_name) from error
    except (ValueError, OverflowError, RecursionError, TypeError) as error:
        raise _json_validation_error(field_name) from error
    if not isinstance(decoded, dict):
        raise _json_validation_error(field_name)
    return cast(dict[str, Any], decoded)


def _assign_json_value(
    parent: dict[str, Any] | list[Any],
    slot: str | int,
    value: Any,
) -> None:
    if isinstance(parent, list):
        parent[cast(int, slot)] = value
    else:
        parent[cast(str, slot)] = value


def _json_validation_error(field_name: str) -> TaskValidationError:
    return TaskValidationError(f"Task {field_name} must be a JSON-compatible object")


def _validate_json_string(value: str, *, field_name: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise _json_validation_error(field_name) from error


def _normalize_json_object(
    value: Mapping[str, Any], *, field_name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _json_validation_error(field_name)
    root: list[Any] = [None]
    active_containers: set[int] = set()
    stack: list[
        tuple[
            bool,
            Any,
            dict[str, Any] | list[Any],
            str | int,
            int,
        ]
    ] = [(False, value, root, 0, 0)]
    while stack:
        exiting, source, parent, slot, depth = stack.pop()
        if exiting:
            active_containers.remove(id(source))
            continue
        if depth > _MAX_JSON_DEPTH:
            raise _json_validation_error(field_name)
        if source is None or type(source) in (bool, int):
            _assign_json_value(parent, slot, source)
            continue
        if type(source) is str:
            _validate_json_string(source, field_name=field_name)
            _assign_json_value(parent, slot, source)
            continue
        if type(source) is float:
            if not math.isfinite(source):
                raise _json_validation_error(field_name)
            _assign_json_value(parent, slot, source)
            continue
        if isinstance(source, Mapping):
            identity = id(source)
            if identity in active_containers:
                raise _json_validation_error(field_name)
            try:
                raw_items = tuple(source.items())
                items = tuple((item[0], item[1]) for item in raw_items)
            except Exception as error:
                raise _json_validation_error(field_name) from error
            if any(type(key) is not str for key, _nested in items):
                raise TaskValidationError(
                    f"Task {field_name} JSON object keys must be strings"
                )
            for key, _nested in items:
                _validate_json_string(cast(str, key), field_name=field_name)
            normalized_mapping: dict[str, Any] = {}
            _assign_json_value(parent, slot, normalized_mapping)
            active_containers.add(identity)
            stack.append((True, source, parent, slot, depth))
            for key, nested in reversed(items):
                stack.append(
                    (False, nested, normalized_mapping, cast(str, key), depth + 1)
                )
            continue
        if isinstance(source, (list, tuple)):
            identity = id(source)
            if identity in active_containers:
                raise _json_validation_error(field_name)
            try:
                source_length = len(source)
                source_items = tuple(source[index] for index in range(source_length))
            except Exception as error:
                raise _json_validation_error(field_name) from error
            normalized_items: list[Any] = [None] * len(source_items)
            _assign_json_value(parent, slot, normalized_items)
            active_containers.add(identity)
            stack.append((True, source, parent, slot, depth))
            for index in range(len(source_items) - 1, -1, -1):
                stack.append(
                    (False, source_items[index], normalized_items, index, depth + 1)
                )
            continue
        raise _json_validation_error(field_name)
    normalized = root[0]
    if not isinstance(normalized, dict):
        raise _json_validation_error(field_name)
    return cast(dict[str, Any], normalized)


def _freeze_normalized_json(value: dict[str, Any]) -> Mapping[str, Any]:
    root: list[Any] = [None]
    stack: list[
        tuple[
            str,
            Any,
            dict[str, Any] | list[Any],
            str | int,
        ]
    ] = [("enter", value, root, 0)]
    while stack:
        action, source, parent, slot = stack.pop()
        if action == "finish_mapping":
            _assign_json_value(parent, slot, MappingProxyType(source))
            continue
        if action == "finish_list":
            _assign_json_value(parent, slot, tuple(source))
            continue
        if isinstance(source, dict):
            frozen_mapping: dict[str, Any] = {}
            stack.append(("finish_mapping", frozen_mapping, parent, slot))
            for key, nested in reversed(tuple(source.items())):
                stack.append(("enter", nested, frozen_mapping, key))
            continue
        if isinstance(source, list):
            frozen_items: list[Any] = [None] * len(source)
            stack.append(("finish_list", frozen_items, parent, slot))
            for index in range(len(source) - 1, -1, -1):
                stack.append(("enter", source[index], frozen_items, index))
            continue
        _assign_json_value(parent, slot, source)
    return cast(Mapping[str, Any], root[0])


def _freeze_json_object(value: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = _validated_json_object(value, field_name="stored JSON")
    return _freeze_normalized_json(normalized)


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
        payload=_freeze_json_object(cast(Mapping[str, Any], row["payload_json"])),
        result=(
            _freeze_json_object(cast(Mapping[str, Any], row["result_json"]))
            if row["result_json"] is not None
            else None
        ),
        error=(
            _freeze_json_object(cast(Mapping[str, Any], row["error_json"]))
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


def _event_snapshot(row: RowMapping) -> TaskEventSnapshot:
    occurred_at = _aware_utc(cast(datetime, row["occurred_at"]))
    if occurred_at is None:
        raise RuntimeError("Task event timestamp must not be null")
    return TaskEventSnapshot(
        id=cast(str, row["id"]),
        task_id=cast(str, row["task_id"]),
        event_name=cast(str, row["event_name"]),
        level=cast(TaskEventLevel, row["level"]),
        progress=cast(float | None, row["progress"]),
        detail=_freeze_json_object(cast(Mapping[str, Any], row["detail_json"])),
        occurred_at=occurred_at,
    )


def _append_event(
    connection: Connection,
    *,
    task_id: str,
    event_name: str,
    level: TaskEventLevel,
    progress: float | None,
    detail: Mapping[str, Any],
    occurred_at: datetime,
) -> None:
    safe_detail = _validated_json_object(detail, field_name="event detail")
    latest_event_time = _aware_utc(
        connection.execute(
            select(func.max(TaskEvent.occurred_at)).where(TaskEvent.task_id == task_id)
        ).scalar_one()
    )
    effective_time = occurred_at
    if latest_event_time is not None and latest_event_time >= effective_time:
        effective_time = latest_event_time + timedelta(microseconds=1)
    connection.execute(
        insert(TaskEvent).values(
            id=str(uuid4()),
            task_id=task_id,
            event_name=event_name,
            level=level,
            progress=progress,
            detail_json=safe_detail,
            occurred_at=effective_time,
        )
    )


class TaskRepository:
    """Transactional access to the durable task queue."""

    def __init__(self, engine: Engine, *, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine
        try:
            with engine.connect() as connection:
                self._database_identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise TaskValidationError(
                "Task database identity could not be determined"
            ) from error

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    @classmethod
    def open(cls, url: str) -> "TaskRepository":
        """Migrate and open an owned repository for an application process."""
        migrate(url)
        return cls(create_engine_for_url(url), owns_engine=True)

    def create(self, kind: str, payload: Mapping[str, Any]) -> TaskSnapshot:
        task_id = str(uuid4())
        now = _utc_now()
        with self._engine.begin() as connection:
            return self.enqueue_in_transaction(
                connection,
                kind,
                payload,
                task_id=task_id,
                now=now,
            )

    def enqueue_in_transaction(
        self,
        connection: Connection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        task_id: str,
        now: datetime,
    ) -> TaskSnapshot:
        if connection.closed or not connection.in_transaction():
            raise TaskValidationError(
                "Task enqueue requires an active transaction connection"
            )
        try:
            transaction_identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise TaskValidationError(
                "Task database identity could not be determined"
            ) from error
        if transaction_identity != self._database_identity:
            raise TaskValidationError(
                "Task transaction connection targets a different database"
            )
        if not kind or kind != kind.strip() or len(kind) > 64:
            raise TaskValidationError("Task kind must contain 1 to 64 characters")
        validated_task_id = _validated_uuid(task_id, field_name="id")
        validated_now = _validated_aware_utc(now, field_name="enqueue time")
        validated_payload = _validated_json_object(payload, field_name="payload")
        values = {
            "id": validated_task_id,
            "kind": kind,
            "status": "queued",
            "progress": 0.0,
            "payload_json": validated_payload,
            "result_json": None,
            "error_json": None,
            "cancel_requested": False,
            "worker_id": None,
            "created_at": validated_now,
            "updated_at": validated_now,
            "started_at": None,
            "finished_at": None,
        }
        statement = insert(TaskRun).values(**values).returning(TaskRun)
        row = connection.execute(statement).mappings().one()
        task = _snapshot(row)
        _append_event(
            connection,
            task_id=task.id,
            event_name="task.created",
            level="info",
            progress=task.progress,
            detail={"kind": task.kind},
            occurred_at=task.created_at,
        )
        return task

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

    def list_events(self, task_id: str, *, limit: int = 100) -> list[TaskEventSnapshot]:
        if not 1 <= limit <= 100:
            raise TaskValidationError("Task event limit must be between 1 and 100")
        task_statement = select(TaskRun.id).where(TaskRun.id == task_id)
        event_statement = (
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.occurred_at.desc(), TaskEvent.id.desc())
            .limit(limit)
        )
        with self._engine.connect() as connection:
            if connection.execute(task_statement).scalar_one_or_none() is None:
                raise TaskNotFound(f"Task {task_id} was not found")
            rows = connection.execute(event_statement).mappings().all()
        return [_event_snapshot(row) for row in reversed(rows)]

    def metrics(self) -> TaskMetricsSnapshot:
        status_statement = (
            select(TaskRun.status, func.count(TaskRun.id).label("task_count"))
            .group_by(TaskRun.status)
            .order_by(TaskRun.status)
        )
        duration_ms = (
            func.julianday(TaskRun.finished_at) - func.julianday(TaskRun.started_at)
        ) * 86_400_000.0
        duration_statement = select(
            func.count(TaskRun.id).label("completed_count"),
            func.avg(duration_ms).label("average_duration_ms"),
            func.min(duration_ms).label("min_duration_ms"),
            func.max(duration_ms).label("max_duration_ms"),
        ).where(TaskRun.started_at.is_not(None), TaskRun.finished_at.is_not(None))
        with self._engine.connect() as connection:
            status_rows = connection.execute(status_statement).mappings().all()
            duration_row = connection.execute(duration_statement).mappings().one()

        counts: dict[TaskStatus, int] = {
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for row in status_rows:
            counts[cast(TaskStatus, row["status"])] = int(row["task_count"])

        def optional_float(value: object) -> float | None:
            return float(cast(float | int, value)) if value is not None else None

        return TaskMetricsSnapshot(
            total=sum(counts.values()),
            by_status=MappingProxyType(counts),
            failure_count=counts["failed"],
            completed_count=int(duration_row["completed_count"]),
            average_duration_ms=optional_float(duration_row["average_duration_ms"]),
            min_duration_ms=optional_float(duration_row["min_duration_ms"]),
            max_duration_ms=optional_float(duration_row["max_duration_ms"]),
        )

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    ) -> TaskSnapshot | TaskClaim | None:
        if not worker_id or worker_id != worker_id.strip() or len(worker_id) > 255:
            raise TaskValidationError("Worker id must contain 1 to 255 characters")
        sampled_at = (
            _utc_now()
            if now is None
            else _validated_aware_utc(now, field_name="claim time")
        )
        duration = validate_lease_duration(lease_duration)
        lease_expires_at = sampled_at + duration
        transition_time = _transition_time(sampled_at)
        claim_token = str(uuid4())
        claimable = or_(
            TaskRun.status == "queued",
            (
                (TaskRun.kind == _BACKTEST_TASK_KIND)
                & (TaskRun.status == "running")
                & (TaskRun.lease_expires_at.is_not(None))
                & (TaskRun.lease_expires_at <= sampled_at)
            ),
        )
        candidate = (
            select(TaskRun.id)
            .where(claimable, TaskRun.cancel_requested.is_(False))
            .order_by(TaskRun.created_at, TaskRun.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == candidate,
                claimable,
                TaskRun.cancel_requested.is_(False),
            )
            .values(
                status="running",
                worker_id=worker_id,
                started_at=case(
                    (TaskRun.started_at.is_(None), transition_time),
                    else_=TaskRun.started_at,
                ),
                updated_at=transition_time,
                claim_token=case(
                    (TaskRun.kind == _BACKTEST_TASK_KIND, claim_token),
                    else_=null(),
                ),
                lease_expires_at=case(
                    (TaskRun.kind == _BACKTEST_TASK_KIND, lease_expires_at),
                    else_=null(),
                ),
                heartbeat_at=case(
                    (TaskRun.kind == _BACKTEST_TASK_KIND, sampled_at),
                    else_=null(),
                ),
                attempt_count=case(
                    (
                        TaskRun.kind == _BACKTEST_TASK_KIND,
                        TaskRun.attempt_count + 1,
                    ),
                    else_=TaskRun.attempt_count,
                ),
            )
            .returning(TaskRun)
        )
        expired_cancellations = (
            update(TaskRun)
            .where(
                TaskRun.kind == _BACKTEST_TASK_KIND,
                TaskRun.status == "running",
                TaskRun.cancel_requested.is_(True),
                TaskRun.lease_expires_at.is_not(None),
                TaskRun.lease_expires_at <= sampled_at,
            )
            .values(
                status="cancelled",
                result_json=None,
                error_json=None,
                updated_at=transition_time,
                finished_at=transition_time,
                claim_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            cancelled_rows = connection.execute(expired_cancellations).mappings().all()
            if cancelled_rows:
                from stock_desk.backtest.models import BacktestRunRow

                connection.execute(
                    update(BacktestRunRow)
                    .where(
                        BacktestRunRow.task_id.in_(
                            tuple(row["id"] for row in cancelled_rows)
                        ),
                        BacktestRunRow.status.in_(("queued", "running")),
                    )
                    .values(
                        status="cancelled",
                        stage="cancelled",
                        updated_at=sampled_at,
                        finished_at=sampled_at,
                    )
                )
            for cancelled_row in cancelled_rows:
                cancelled = _snapshot(cancelled_row)
                _append_event(
                    connection,
                    task_id=cancelled.id,
                    event_name="task.cancelled",
                    level="info",
                    progress=cancelled.progress,
                    detail={},
                    occurred_at=cancelled.updated_at,
                )
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                return None
            task = _snapshot(row)
            _append_event(
                connection,
                task_id=task.id,
                event_name="task.claimed",
                level="info",
                progress=task.progress,
                detail={"worker_id": worker_id},
                occurred_at=task.updated_at,
            )
        if task.kind != _BACKTEST_TASK_KIND:
            return task
        stored_token = row["claim_token"]
        stored_expiry = _aware_utc(cast(datetime | None, row["lease_expires_at"]))
        attempts = row["attempt_count"]
        if (
            type(stored_token) is not str
            or stored_token != claim_token
            or stored_expiry is None
            or type(attempts) is not int
            or attempts < 1
        ):
            raise RuntimeError("Backtest task claim is invalid")
        return TaskClaim(
            snapshot=task,
            claim_token=stored_token,
            lease_expires_at=stored_expiry,
            attempt_count=attempts,
        )

    def heartbeat(
        self,
        task_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    ) -> TaskClaim:
        validated_token = _validated_uuid(claim_token, field_name="claim token")
        sampled_at = (
            _utc_now()
            if now is None
            else _validated_aware_utc(now, field_name="heartbeat time")
        )
        duration = validate_lease_duration(lease_duration)
        transition_time = _transition_time(sampled_at)
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == task_id,
                TaskRun.kind == _BACKTEST_TASK_KIND,
                TaskRun.status == "running",
                TaskRun.claim_token == validated_token,
                TaskRun.lease_expires_at.is_not(None),
                TaskRun.lease_expires_at > sampled_at,
            )
            .values(
                heartbeat_at=sampled_at,
                lease_expires_at=sampled_at + duration,
                updated_at=transition_time,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is not None:
                snapshot = _snapshot(row)
                expires_at = _aware_utc(cast(datetime | None, row["lease_expires_at"]))
                attempt_count = row["attempt_count"]
                if expires_at is None or type(attempt_count) is not int:
                    raise RuntimeError("Backtest heartbeat result is invalid")
                return TaskClaim(
                    snapshot=snapshot,
                    claim_token=validated_token,
                    lease_expires_at=expires_at,
                    attempt_count=attempt_count,
                )
        current = self.get(task_id)
        raise TaskConflict(f"Task {current.id} claim is not current")

    def guard_claim_in_transaction(
        self,
        connection: Connection,
        task_id: str,
        claim_token: str,
        *,
        progress: float,
        now: datetime,
    ) -> TaskSnapshot:
        """Fence one caller-owned checkpoint transaction with the live lease."""

        if connection.closed or not connection.in_transaction():
            raise TaskValidationError(
                "Task checkpoint guard requires an active transaction"
            )
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise TaskValidationError(
                "Task database identity could not be determined"
            ) from error
        if identity != self._database_identity:
            raise TaskValidationError(
                "Task transaction connection targets a different database"
            )
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0.0 <= progress <= 1.0
        ):
            raise TaskValidationError(
                "Task progress must be a finite number from 0 to 1"
            )
        sampled_at = _validated_aware_utc(now, field_name="checkpoint time")
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == task_id,
                TaskRun.status == "running",
                TaskRun.progress <= float(progress),
                self._ownership_condition(claim_token, sampled_at),
            )
            .values(
                progress=float(progress),
                updated_at=_transition_time(sampled_at),
            )
            .returning(TaskRun)
        )
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise TaskConflict(f"Task {task_id} claim is not current")
        return _snapshot(row)

    def complete_claim_in_transaction(
        self,
        connection: Connection,
        task_id: str,
        claim_token: str,
        result: Mapping[str, Any],
        *,
        now: datetime,
    ) -> TaskSnapshot:
        """Atomically terminalize a leased task in its domain transaction."""

        if connection.closed or not connection.in_transaction():
            raise TaskValidationError(
                "Task completion requires an active transaction connection"
            )
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise TaskValidationError(
                "Task database identity could not be determined"
            ) from error
        if identity != self._database_identity:
            raise TaskValidationError(
                "Task transaction connection targets a different database"
            )
        validated_result = _validated_json_object(result, field_name="result")
        sampled_at = _validated_aware_utc(now, field_name="completion time")
        cancelling = TaskRun.cancel_requested.is_(True)
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == task_id,
                TaskRun.status == "running",
                self._ownership_condition(claim_token, sampled_at),
            )
            .values(
                status=case((cancelling, "cancelled"), else_="succeeded"),
                progress=case((cancelling, TaskRun.progress), else_=1.0),
                result_json=case(
                    (cancelling, null()),
                    else_=bindparam(
                        "_transaction_completed_result",
                        validated_result,
                        type_=JSON(),
                    ),
                ),
                error_json=None,
                updated_at=_transition_time(sampled_at),
                finished_at=_transition_time(sampled_at),
                claim_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
            .returning(TaskRun)
        )
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise TaskConflict(f"Task {task_id} claim is not current")
        task = _snapshot(row)
        _append_event(
            connection,
            task_id=task.id,
            event_name=(
                "task.cancelled" if task.status == "cancelled" else "task.succeeded"
            ),
            level="info",
            progress=task.progress,
            detail={},
            occurred_at=task.updated_at,
        )
        return task

    def fail_claim_in_transaction(
        self,
        connection: Connection,
        task_id: str,
        claim_token: str,
        error: Mapping[str, Any],
        *,
        now: datetime,
    ) -> TaskSnapshot:
        """Atomically fail a leased task in its domain transaction."""

        if connection.closed or not connection.in_transaction():
            raise TaskValidationError(
                "Task failure requires an active transaction connection"
            )
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error_identity:
            raise TaskValidationError(
                "Task database identity could not be determined"
            ) from error_identity
        if identity != self._database_identity:
            raise TaskValidationError(
                "Task transaction connection targets a different database"
            )
        validated_error = _validated_json_object(error, field_name="error")
        sampled_at = _validated_aware_utc(now, field_name="failure time")
        cancelling = TaskRun.cancel_requested.is_(True)
        row = (
            connection.execute(
                update(TaskRun)
                .where(
                    TaskRun.id == task_id,
                    TaskRun.status == "running",
                    self._ownership_condition(claim_token, sampled_at),
                )
                .values(
                    status=case((cancelling, "cancelled"), else_="failed"),
                    result_json=None,
                    error_json=case(
                        (cancelling, null()),
                        else_=bindparam(
                            "_transaction_failure_error",
                            validated_error,
                            type_=JSON(),
                        ),
                    ),
                    updated_at=_transition_time(sampled_at),
                    finished_at=_transition_time(sampled_at),
                    claim_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
                .returning(TaskRun)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TaskConflict(f"Task {task_id} claim is not current")
        task = _snapshot(row)
        _append_event(
            connection,
            task_id=task.id,
            event_name="task.cancelled"
            if task.status == "cancelled"
            else "task.failed",
            level="info" if task.status == "cancelled" else "error",
            progress=task.progress,
            detail={} if task.status == "cancelled" else {"code": "task_failed"},
            occurred_at=task.updated_at,
        )
        return task

    def set_progress(
        self,
        task_id: str,
        progress: float,
        detail: Mapping[str, Any] | None = None,
        *,
        claim_token: str | None = None,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0.0 <= progress <= 1.0
        ):
            raise TaskValidationError(
                "Task progress must be a finite number from 0 to 1"
            )
        validated_detail = _validated_json_object(
            detail if detail is not None else {},
            field_name="progress detail",
        )
        sampled_at = (
            _utc_now()
            if now is None
            else _validated_aware_utc(now, field_name="progress time")
        )
        transition_time = _transition_time(sampled_at)
        ownership = self._ownership_condition(claim_token, sampled_at)
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == task_id,
                TaskRun.status == "running",
                TaskRun.progress <= float(progress),
                ownership,
            )
            .values(progress=float(progress), updated_at=transition_time)
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is not None:
                task = _snapshot(row)
                _append_event(
                    connection,
                    task_id=task.id,
                    event_name="task.progressed",
                    level="info",
                    progress=task.progress,
                    detail=validated_detail,
                    occurred_at=task.updated_at,
                )
                return task
        current = self.get(task_id)
        raise TaskConflict(f"Task {current.id} is not running")

    def complete(
        self,
        task_id: str,
        result: Mapping[str, Any],
        *,
        claim_token: str | None = None,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        validated_result = _validated_json_object(result, field_name="result")
        sampled_at = (
            _utc_now()
            if now is None
            else _validated_aware_utc(now, field_name="completion time")
        )
        transition_time = _transition_time(sampled_at)
        cancelling = TaskRun.cancel_requested.is_(True)
        ownership = self._ownership_condition(claim_token, sampled_at)
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == task_id,
                TaskRun.status == "running",
                ownership,
            )
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
                claim_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is not None:
                task = _snapshot(row)
                _append_event(
                    connection,
                    task_id=task.id,
                    event_name=(
                        "task.cancelled"
                        if task.status == "cancelled"
                        else "task.succeeded"
                    ),
                    level="info",
                    progress=task.progress,
                    detail={},
                    occurred_at=task.updated_at,
                )
                return task
        current = self.get(task_id)
        if current.status == "succeeded":
            return current
        raise TaskConflict(f"Task {current.id} cannot be completed")

    def fail(
        self,
        task_id: str,
        error: Mapping[str, Any],
        *,
        claim_token: str | None = None,
        now: datetime | None = None,
    ) -> TaskSnapshot:
        validated_error = _validated_json_object(error, field_name="error")
        sampled_at = (
            _utc_now()
            if now is None
            else _validated_aware_utc(now, field_name="failure time")
        )
        transition_time = _transition_time(sampled_at)
        cancelling = TaskRun.cancel_requested.is_(True)
        ownership = self._ownership_condition(claim_token, sampled_at)
        statement = (
            update(TaskRun)
            .where(
                TaskRun.id == task_id,
                TaskRun.status == "running",
                ownership,
            )
            .values(
                status=case((cancelling, "cancelled"), else_="failed"),
                result_json=None,
                error_json=case(
                    (cancelling, null()),
                    else_=bindparam("_failure_error", validated_error, type_=JSON()),
                ),
                updated_at=transition_time,
                finished_at=transition_time,
                claim_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is not None:
                task = _snapshot(row)
                _append_event(
                    connection,
                    task_id=task.id,
                    event_name=(
                        "task.cancelled"
                        if task.status == "cancelled"
                        else "task.failed"
                    ),
                    level="info" if task.status == "cancelled" else "error",
                    progress=task.progress,
                    detail={}
                    if task.status == "cancelled"
                    else {"code": "task_failed"},
                    occurred_at=task.updated_at,
                )
                return task
        current = self.get(task_id)
        if current.status == "failed":
            return current
        raise TaskConflict(f"Task {current.id} cannot be failed")

    @staticmethod
    def _ownership_condition(
        claim_token: str | None,
        operation_time: datetime,
    ) -> ColumnElement[bool]:
        if claim_token is None:
            return TaskRun.kind != _BACKTEST_TASK_KIND
        validated_token = _validated_uuid(claim_token, field_name="claim token")
        return (
            (TaskRun.kind == _BACKTEST_TASK_KIND)
            & (TaskRun.claim_token == validated_token)
            & (TaskRun.lease_expires_at.is_not(None))
            & (TaskRun.lease_expires_at > operation_time)
        )

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
            queued_cancelled = row is not None
            if row is None:
                row = connection.execute(running_statement).mappings().one_or_none()
            if row is not None:
                task = _snapshot(row)
                if queued_cancelled and task.kind == _BACKTEST_TASK_KIND:
                    from stock_desk.backtest.models import BacktestRunRow

                    connection.execute(
                        update(BacktestRunRow)
                        .where(
                            BacktestRunRow.task_id == task.id,
                            BacktestRunRow.status == "queued",
                        )
                        .values(
                            status="cancelled",
                            stage="cancelled",
                            updated_at=task.updated_at,
                            finished_at=task.finished_at,
                        )
                    )
                _append_event(
                    connection,
                    task_id=task.id,
                    event_name="task.cancel_requested",
                    level="info",
                    progress=task.progress,
                    detail={},
                    occurred_at=task.updated_at,
                )
                if task.status == "cancelled":
                    _append_event(
                        connection,
                        task_id=task.id,
                        event_name="task.cancelled",
                        level="info",
                        progress=task.progress,
                        detail={},
                        occurred_at=task.updated_at + timedelta(microseconds=1),
                    )
                return task
        current = self.get(task_id)
        if current.status == "cancelled":
            return current
        raise TaskConflict(f"Task {current.id} cannot be cancelled")

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()
