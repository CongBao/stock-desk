from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, TypeAlias


TaskStatus: TypeAlias = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TaskEventLevel: TypeAlias = Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Immutable task view with read-only outer JSON mappings."""

    id: str
    kind: str
    status: TaskStatus
    progress: float
    payload: Mapping[str, Any]
    result: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    cancel_requested: bool
    worker_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return max(
            0.0,
            (self.finished_at - self.started_at).total_seconds() * 1_000.0,
        )


@dataclass(frozen=True, slots=True)
class TaskEventSnapshot:
    id: str
    task_id: str
    event_name: str
    level: TaskEventLevel
    progress: float | None
    detail: Mapping[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TaskMetricsSnapshot:
    total: int
    by_status: Mapping[TaskStatus, int]
    failure_count: int
    completed_count: int
    average_duration_ms: float | None
    min_duration_ms: float | None
    max_duration_ms: float | None
