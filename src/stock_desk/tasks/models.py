from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, TypeAlias


TaskStatus: TypeAlias = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TaskEventLevel: TypeAlias = Literal["info", "warning", "error"]
TaskPresentationStage: TypeAlias = Literal[
    "queued", "executing", "completed", "failed", "cancelled"
]
WorkerState: TypeAlias = Literal["running", "not_detected"]


@dataclass(frozen=True, slots=True)
class TaskPresentationTarget:
    type: Literal["backtest_run"]
    id: str


@dataclass(frozen=True, slots=True)
class TaskPresentationSnapshot:
    label: Literal["股票池回测", "智能分析", "数据更新", "后台任务"]
    stage: TaskPresentationStage | None
    processed: int | None
    total: int | None
    failed: int | None
    target: TaskPresentationTarget | None


@dataclass(frozen=True, slots=True)
class TaskEventPresentationSnapshot:
    label: Literal[
        "任务已创建",
        "任务已开始",
        "任务进度已更新",
        "已处理回测标的",
        "已请求取消",
        "任务已取消",
        "任务已完成",
        "任务失败",
        "任务事件",
    ]
    stage: TaskPresentationStage | None
    processed: int | None
    total: int | None
    failed: int | None


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Immutable task view with recursively immutable JSON values."""

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
class TaskClaim:
    """Internal leased ownership for a recoverable task.

    The token deliberately remains outside :class:`TaskSnapshot`, which is the
    only task object exposed by the HTTP API.
    """

    snapshot: TaskSnapshot
    claim_token: str
    lease_expires_at: datetime
    attempt_count: int


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


@dataclass(frozen=True, slots=True)
class WorkerStatusSnapshot:
    state: WorkerState
    last_seen_at: datetime | None
