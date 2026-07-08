from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from types import MappingProxyType
from threading import RLock
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
from filelock import FileLock, Timeout as FileLockTimeout

from stock_desk.security.redaction import clean_active_secrets
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
    TaskEventPresentationSnapshot,
    TaskPresentationSnapshot,
    TaskPresentationTarget,
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
_TASK_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
_BACKTEST_TASK_KIND = "backtest.run"
_ANALYSIS_TASK_KIND = "analysis.run"
_LEASED_TASK_KINDS = frozenset({_BACKTEST_TASK_KIND, _ANALYSIS_TASK_KIND})
_DEFAULT_LEASE_DURATION = timedelta(minutes=2)
_MAX_LEASE_DURATION = timedelta(hours=1)
_PRESENTATION_STAGES = frozenset(
    {"queued", "executing", "completed", "failed", "cancelled"}
)
_EVENT_LABELS = {
    "task.created": "任务已创建",
    "task.claimed": "任务已开始",
    "task.progressed": "任务进度已更新",
    "backtest.progressed": "已处理回测标的",
    "task.cancel_requested": "已请求取消",
    "task.cancelled": "任务已取消",
    "task.succeeded": "任务已完成",
    "task.failed": "任务失败",
}
_CLAIM_GATE_THREAD_LOCK = RLock()


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


def _redacted_json_object(
    value: Mapping[str, Any], *, field_name: str
) -> dict[str, Any]:
    cleaned = clean_active_secrets(value)
    if not isinstance(cleaned, Mapping):
        raise _json_validation_error(field_name)
    return _validated_json_object(cleaned, field_name=field_name)


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


def _snapshot(row: RowMapping | Mapping[str, Any]) -> TaskSnapshot:
    raw_status = row["status"]
    if type(raw_status) is not str or raw_status not in _TASK_STATUSES:
        raise TaskRepositoryError("Stored task status is invalid")
    raw_progress = row["progress"]
    if type(raw_progress) not in {int, float}:
        raise TaskRepositoryError("Stored task progress is invalid")
    progress = float(raw_progress)
    if not math.isfinite(progress) or not 0.0 <= progress <= 1.0:
        raise TaskRepositoryError("Stored task progress is invalid")
    created_at = _aware_utc(cast(datetime, row["created_at"]))
    updated_at = _aware_utc(cast(datetime, row["updated_at"]))
    if created_at is None or updated_at is None:
        raise RuntimeError("Task timestamps must not be null")
    return TaskSnapshot(
        id=cast(str, row["id"]),
        kind=cast(str, row["kind"]),
        status=cast(TaskStatus, raw_status),
        progress=progress,
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
    safe_detail = _redacted_json_object(detail, field_name="event detail")
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

    @property
    def engine(self) -> Engine:
        """Expose the bound engine for same-database application composition."""
        return self._engine

    @staticmethod
    def snapshot_from_mapping(row: Mapping[str, Any]) -> TaskSnapshot:
        """Build the validated immutable task projection used by joined reads."""
        return _snapshot(row)

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
        validated_payload = _redacted_json_object(payload, field_name="payload")
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

    def presentation(self, task: TaskSnapshot) -> TaskPresentationSnapshot:
        """Return the only browser-displayable task domain projection.

        Raw task JSON is deliberately excluded. Backtest counts come from the
        constrained domain row, not from task payload/result/error values.
        """

        return self.presentation_many((task,))[task.id]

    def presentation_many(
        self, tasks: tuple[TaskSnapshot, ...] | list[TaskSnapshot]
    ) -> dict[str, TaskPresentationSnapshot]:
        """Build browser-safe task projections with at most one domain query."""

        task_items = tuple(tasks)
        backtest_task_ids = tuple(
            task.id for task in task_items if task.kind == _BACKTEST_TASK_KIND
        )
        backtest_rows: dict[str, tuple[object, ...]] = {}
        if backtest_task_ids:
            from stock_desk.backtest.models import BacktestRunRow

            statement = select(
                BacktestRunRow.task_id,
                BacktestRunRow.id,
                BacktestRunRow.stage,
                BacktestRunRow.processed,
                BacktestRunRow.total,
                BacktestRunRow.failed_count,
            ).where(BacktestRunRow.task_id.in_(backtest_task_ids))
            with self._engine.connect() as connection:
                backtest_rows = {
                    cast(str, row[0]): tuple(row[1:])
                    for row in connection.execute(statement).all()
                }

        presentations: dict[str, TaskPresentationSnapshot] = {}
        for task in task_items:
            if task.kind == _BACKTEST_TASK_KIND:
                row = backtest_rows.get(task.id)
                if row is not None:
                    run_id = cast(str, row[0])
                    stage = cast(str, row[1])
                    processed = int(cast(int, row[2]))
                    total = int(cast(int, row[3]))
                    failed = int(cast(int, row[4]))
                else:
                    run_id = ""
                    stage = ""
                    processed = total = failed = -1
                if (
                    stage in _PRESENTATION_STAGES
                    and 0 <= failed <= processed <= total <= 10_000
                ):
                    try:
                        _validated_uuid(run_id, field_name="backtest run id")
                    except TaskValidationError:
                        pass
                    else:
                        presentations[task.id] = TaskPresentationSnapshot(
                            label="股票池回测",
                            stage=cast(Any, stage),
                            processed=processed,
                            total=total,
                            failed=failed,
                            target=TaskPresentationTarget(
                                type="backtest_run", id=run_id
                            ),
                        )
                        continue
                label: Any = "股票池回测"
            elif task.kind == _ANALYSIS_TASK_KIND:
                label = "智能分析"
            elif task.kind in {"market.update", "market.catalog.update"}:
                label = "数据更新"
            else:
                label = "后台任务"
            presentations[task.id] = TaskPresentationSnapshot(
                label=label,
                stage=None,
                processed=None,
                total=None,
                failed=None,
                target=None,
            )
        return presentations

    def event_presentation(
        self,
        task_event: TaskEventSnapshot,
        *,
        task_kind: str | None = None,
    ) -> TaskEventPresentationSnapshot:
        label = _EVENT_LABELS.get(task_event.event_name, "任务事件")
        stage: str | None = None
        processed: int | None = None
        total: int | None = None
        failed: int | None = None
        detail = task_event.detail
        raw_stage = detail.get("stage")
        raw_processed = detail.get("processed")
        raw_total = detail.get("total")
        raw_failed = detail.get("failed")
        if task_event.event_name == "backtest.progressed" and task_kind is None:
            with self._engine.connect() as connection:
                task_kind = connection.execute(
                    select(TaskRun.kind).where(TaskRun.id == task_event.task_id)
                ).scalar_one_or_none()
        if (
            task_event.event_name == "backtest.progressed"
            and task_kind == _BACKTEST_TASK_KIND
            and isinstance(raw_stage, str)
            and raw_stage in _PRESENTATION_STAGES
            and type(raw_processed) is int
            and type(raw_total) is int
            and type(raw_failed) is int
            and 0 <= raw_failed <= raw_processed <= raw_total <= 10_000
        ):
            stage = raw_stage
            processed = raw_processed
            total = raw_total
            failed = raw_failed
            label = "已处理回测标的"
        return TaskEventPresentationSnapshot(
            label=cast(Any, label),
            stage=cast(Any, stage),
            processed=processed,
            total=total,
            failed=failed,
        )

    def append_backtest_progress_event_in_transaction(
        self,
        connection: Connection,
        task_id: str,
        *,
        progress: float,
        stage: str,
        processed: int,
        total: int,
        failed: int,
        now: datetime,
    ) -> None:
        """Append a coalesced domain event inside an owning checkpoint transaction."""

        if connection.closed or not connection.in_transaction():
            raise TaskValidationError(
                "Task progress event requires an active transaction connection"
            )
        if connection_database_identity(connection) != self._database_identity:
            raise TaskValidationError(
                "Task transaction connection targets a different database"
            )
        if (
            stage not in _PRESENTATION_STAGES
            or isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0 <= float(progress) <= 1
            or type(processed) is not int
            or type(total) is not int
            or type(failed) is not int
            or not 0 <= failed <= processed <= total <= 10_000
        ):
            raise TaskValidationError("Task progress event is invalid")
        first_checkpoint = processed == 1
        final_checkpoint = total > 0 and processed == total
        crossed_percent_bucket = (
            total > 0
            and processed > 0
            and (processed * 100) // total > ((processed - 1) * 100) // total
        )
        if not (first_checkpoint or crossed_percent_bucket or final_checkpoint):
            return
        _append_event(
            connection,
            task_id=task_id,
            event_name="backtest.progressed",
            level="info",
            progress=float(progress),
            detail={
                "stage": stage,
                "processed": processed,
                "total": total,
                "failed": failed,
            },
            occurred_at=_validated_aware_utc(now, field_name="progress event time"),
        )

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

    @contextmanager
    def hold_claim_gate(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterator[None]:
        """Block new claims across threads and processes without blocking enqueue."""
        if timeout_seconds is not None and timeout_seconds < 0:
            raise TaskValidationError("Claim gate timeout must be nonnegative")
        lock_path = (
            Path(cast(str, self._database_identity[1])).with_name(
                f"{Path(cast(str, self._database_identity[1])).name}.claim.lock"
            )
            if self._database_identity[0] == "sqlite-file"
            else None
        )
        acquired = (
            _CLAIM_GATE_THREAD_LOCK.acquire()
            if timeout_seconds is None
            else _CLAIM_GATE_THREAD_LOCK.acquire(timeout=timeout_seconds)
        )
        if not acquired:
            raise FileLockTimeout(str(lock_path or "in-memory claim gate"))
        try:
            if lock_path is None:
                yield
                return
            with FileLock(
                lock_path,
                timeout=-1 if timeout_seconds is None else timeout_seconds,
            ):
                yield
        finally:
            _CLAIM_GATE_THREAD_LOCK.release()

    def running_task_count(self) -> int:
        with self._engine.connect() as connection:
            value = connection.scalar(
                select(func.count())
                .select_from(TaskRun)
                .where(TaskRun.status == "running")
            )
        return int(value or 0)

    def _terminalize_expired_cancellations(
        self,
        connection: Connection,
        *,
        sampled_at: datetime,
        transition_time: ColumnElement[datetime],
    ) -> list[RowMapping]:
        expired_cancellations = (
            update(TaskRun)
            .where(
                TaskRun.kind.in_(_LEASED_TASK_KINDS),
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
        cancelled_rows = connection.execute(expired_cancellations).mappings().all()
        if cancelled_rows:
            from stock_desk.analysis.models import (
                AnalysisAttemptRow,
                AnalysisRunRow,
                AnalysisStageRow,
            )
            from stock_desk.backtest.models import BacktestRunRow

            cancelled_task_ids = tuple(row["id"] for row in cancelled_rows)
            connection.execute(
                update(BacktestRunRow)
                .where(
                    BacktestRunRow.task_id.in_(cancelled_task_ids),
                    BacktestRunRow.status.in_(("queued", "running")),
                )
                .values(
                    status="cancelled",
                    stage="cancelled",
                    updated_at=sampled_at,
                    finished_at=sampled_at,
                )
            )
            analysis_run_ids = tuple(
                connection.execute(
                    select(AnalysisRunRow.id).where(
                        AnalysisRunRow.task_id.in_(cancelled_task_ids),
                        AnalysisRunRow.status.in_(("queued", "running")),
                    )
                ).scalars()
            )
            if analysis_run_ids:
                connection.execute(
                    update(AnalysisAttemptRow)
                    .where(
                        AnalysisAttemptRow.run_id.in_(analysis_run_ids),
                        AnalysisAttemptRow.status == "running",
                    )
                    .values(
                        status="cancelled",
                        error_json=None,
                        retryable=None,
                        backoff_seconds=None,
                        finished_at=sampled_at,
                    )
                )
                connection.execute(
                    update(AnalysisStageRow)
                    .where(
                        AnalysisStageRow.run_id.in_(analysis_run_ids),
                        AnalysisStageRow.status.in_(("pending", "running")),
                    )
                    .values(
                        status="cancelled",
                        failure_code=None,
                        retryable=None,
                        updated_at=sampled_at,
                        finished_at=sampled_at,
                    )
                )
                connection.execute(
                    update(AnalysisRunRow)
                    .where(
                        AnalysisRunRow.id.in_(analysis_run_ids),
                        AnalysisRunRow.status.in_(("queued", "running")),
                    )
                    .values(
                        status="cancelled",
                        current_stage=None,
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
        return list(cancelled_rows)

    def requeue_expired_leases_for_offline_snapshot(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """Resolve abandoned leased tasks while the offline claim gate is held."""
        sampled_at = (
            _utc_now()
            if now is None
            else _validated_aware_utc(now, field_name="offline snapshot time")
        )
        transition_time = _transition_time(sampled_at)
        statement = (
            update(TaskRun)
            .where(
                TaskRun.kind.in_(_LEASED_TASK_KINDS),
                TaskRun.status == "running",
                TaskRun.cancel_requested.is_(False),
                TaskRun.lease_expires_at.is_not(None),
                TaskRun.lease_expires_at <= sampled_at,
            )
            .values(
                status="queued",
                worker_id=None,
                updated_at=transition_time,
                claim_token=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            cancelled_rows = self._terminalize_expired_cancellations(
                connection,
                sampled_at=sampled_at,
                transition_time=transition_time,
            )
            rows = connection.execute(statement).mappings().all()
            for row in rows:
                task = _snapshot(row)
                _append_event(
                    connection,
                    task_id=task.id,
                    event_name="task.lease_requeued",
                    level="warning",
                    progress=task.progress,
                    detail={"code": "offline_restore_expired_lease"},
                    occurred_at=task.updated_at,
                )
        return len(rows) + len(cancelled_rows)

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_duration: timedelta = _DEFAULT_LEASE_DURATION,
    ) -> TaskSnapshot | TaskClaim | None:
        with self.hold_claim_gate():
            return self._claim_next_without_gate(
                worker_id,
                now=now,
                lease_duration=lease_duration,
            )

    def _claim_next_without_gate(
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
                (TaskRun.kind.in_(_LEASED_TASK_KINDS))
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
                    (TaskRun.kind.in_(_LEASED_TASK_KINDS), claim_token),
                    else_=null(),
                ),
                lease_expires_at=case(
                    (TaskRun.kind.in_(_LEASED_TASK_KINDS), lease_expires_at),
                    else_=null(),
                ),
                heartbeat_at=case(
                    (TaskRun.kind.in_(_LEASED_TASK_KINDS), sampled_at),
                    else_=null(),
                ),
                attempt_count=case(
                    (
                        TaskRun.kind.in_(_LEASED_TASK_KINDS),
                        TaskRun.attempt_count + 1,
                    ),
                    else_=TaskRun.attempt_count,
                ),
            )
            .returning(TaskRun)
        )
        with self._engine.begin() as connection:
            self._terminalize_expired_cancellations(
                connection,
                sampled_at=sampled_at,
                transition_time=transition_time,
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
        if task.kind not in _LEASED_TASK_KINDS:
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
            raise RuntimeError("Leased task claim is invalid")
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
                TaskRun.kind.in_(_LEASED_TASK_KINDS),
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
                    raise RuntimeError("Leased task heartbeat result is invalid")
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
        validated_result = _redacted_json_object(result, field_name="result")
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
        validated_error = _redacted_json_object(error, field_name="error")
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
        validated_detail = _redacted_json_object(
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
        validated_result = _redacted_json_object(result, field_name="result")
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
        validated_error = _redacted_json_object(error, field_name="error")
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
            return TaskRun.kind.not_in(_LEASED_TASK_KINDS)
        validated_token = _validated_uuid(claim_token, field_name="claim token")
        return (
            (TaskRun.kind.in_(_LEASED_TASK_KINDS))
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
                if queued_cancelled and task.kind == _ANALYSIS_TASK_KIND:
                    from stock_desk.analysis.models import (
                        AnalysisRunRow,
                        AnalysisStageRow,
                    )

                    analysis_run_id = connection.execute(
                        select(AnalysisRunRow.id).where(
                            AnalysisRunRow.task_id == task.id,
                            AnalysisRunRow.status == "queued",
                        )
                    ).scalar_one_or_none()
                    if analysis_run_id is not None:
                        connection.execute(
                            update(AnalysisStageRow)
                            .where(
                                AnalysisStageRow.run_id == analysis_run_id,
                                AnalysisStageRow.status == "pending",
                            )
                            .values(
                                status="cancelled",
                                updated_at=task.updated_at,
                                finished_at=task.finished_at,
                            )
                        )
                        connection.execute(
                            update(AnalysisRunRow)
                            .where(
                                AnalysisRunRow.id == analysis_run_id,
                                AnalysisRunRow.status == "queued",
                            )
                            .values(
                                status="cancelled",
                                current_stage=None,
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
