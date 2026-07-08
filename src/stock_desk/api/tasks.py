from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Callable, cast, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from stock_desk.tasks.models import (
    TaskEventLevel,
    TaskEventSnapshot,
    TaskMetricsSnapshot,
    TaskEventPresentationSnapshot,
    TaskPresentationSnapshot,
    TaskSnapshot,
    TaskStatus,
)
from stock_desk.tasks.repository import (
    TaskConflict,
    TaskNotFound,
    TaskRepository,
    TaskRepositoryError,
    TaskValidationError,
)


_RESERVED_DOMAIN_TASK_KINDS = frozenset(
    {
        "analysis.run",
        "backtest.run",
        "market.update",
        "market.catalog.update",
    }
)


def _json_response_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_response_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_response_value(nested) for nested in value]
    return value


def _json_response_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _json_response_value(value))


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def kind_must_not_be_blank(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("Task kind must not be blank or padded")
        return value


class TaskErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "invalid_request",
        "reserved_task_kind",
        "storage_unavailable",
    ]


class TaskResponse(BaseModel):
    id: str
    correlation_id: str
    kind: str
    status: TaskStatus
    progress: float
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    cancel_requested: bool
    worker_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: float | None
    presentation: "TaskPresentationResponse"

    @classmethod
    def from_snapshot(
        cls,
        task: TaskSnapshot,
        presentation: TaskPresentationSnapshot | None = None,
    ) -> "TaskResponse":
        if presentation is None:
            label: Literal["股票池回测", "智能分析", "数据更新", "后台任务"] = (
                "数据更新"
                if task.kind in {"market.update", "market.catalog.update"}
                else "后台任务"
            )
            presentation = TaskPresentationSnapshot(
                label=label,
                stage=None,
                processed=None,
                total=None,
                failed=None,
                target=None,
            )
        return cls(
            id=task.id,
            correlation_id=task.id,
            kind=task.kind,
            status=task.status,
            progress=task.progress,
            payload=_json_response_object(task.payload),
            result=(
                _json_response_object(task.result) if task.result is not None else None
            ),
            error=(
                _json_response_object(task.error) if task.error is not None else None
            ),
            cancel_requested=task.cancel_requested,
            worker_id=task.worker_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            duration_ms=task.duration_ms,
            presentation=TaskPresentationResponse.from_snapshot(presentation),
        )


class TaskPresentationTargetResponse(BaseModel):
    type: Literal["backtest_run"]
    id: str


class TaskPresentationResponse(BaseModel):
    label: Literal["股票池回测", "智能分析", "数据更新", "后台任务"]
    stage: Literal["queued", "executing", "completed", "failed", "cancelled"] | None
    processed: int | None
    total: int | None
    failed: int | None
    target: TaskPresentationTargetResponse | None

    @classmethod
    def from_snapshot(
        cls, presentation: TaskPresentationSnapshot
    ) -> "TaskPresentationResponse":
        target = presentation.target
        return cls(
            label=presentation.label,
            stage=presentation.stage,
            processed=presentation.processed,
            total=presentation.total,
            failed=presentation.failed,
            target=(
                TaskPresentationTargetResponse(type=target.type, id=target.id)
                if target is not None
                else None
            ),
        )


class TaskEventResponse(BaseModel):
    id: str
    task_id: str
    correlation_id: str
    event_name: str
    level: TaskEventLevel
    progress: float | None
    detail: dict[str, Any]
    occurred_at: datetime
    presentation: "TaskEventPresentationResponse"

    @classmethod
    def from_snapshot(
        cls,
        task_event: TaskEventSnapshot,
        presentation: TaskEventPresentationSnapshot | None = None,
    ) -> "TaskEventResponse":
        if presentation is None:
            labels: dict[
                str,
                Literal[
                    "任务已创建",
                    "任务已开始",
                    "任务进度已更新",
                    "已请求取消",
                    "任务已取消",
                    "任务已完成",
                    "任务失败",
                    "任务事件",
                ],
            ] = {
                "task.created": "任务已创建",
                "task.claimed": "任务已开始",
                "task.progressed": "任务进度已更新",
                "task.cancel_requested": "已请求取消",
                "task.cancelled": "任务已取消",
                "task.succeeded": "任务已完成",
                "task.failed": "任务失败",
            }
            presentation = TaskEventPresentationSnapshot(
                label=labels.get(task_event.event_name, "任务事件"),
                stage=None,
                processed=None,
                total=None,
                failed=None,
            )
        return cls(
            id=task_event.id,
            task_id=task_event.task_id,
            correlation_id=task_event.task_id,
            event_name=task_event.event_name,
            level=task_event.level,
            progress=task_event.progress,
            detail=_json_response_object(task_event.detail),
            occurred_at=task_event.occurred_at,
            presentation=TaskEventPresentationResponse.from_snapshot(presentation),
        )


class TaskEventPresentationResponse(BaseModel):
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
    stage: Literal["queued", "executing", "completed", "failed", "cancelled"] | None
    processed: int | None
    total: int | None
    failed: int | None

    @classmethod
    def from_snapshot(
        cls, presentation: TaskEventPresentationSnapshot
    ) -> "TaskEventPresentationResponse":
        return cls(
            label=presentation.label,
            stage=presentation.stage,
            processed=presentation.processed,
            total=presentation.total,
            failed=presentation.failed,
        )


class TaskMetricsResponse(BaseModel):
    total: int
    by_status: dict[TaskStatus, int]
    failure_count: int
    completed_count: int
    average_duration_ms: float | None
    min_duration_ms: float | None
    max_duration_ms: float | None

    @classmethod
    def from_snapshot(cls, metrics: TaskMetricsSnapshot) -> "TaskMetricsResponse":
        return cls(
            total=metrics.total,
            by_status=dict(metrics.by_status),
            failure_count=metrics.failure_count,
            completed_count=metrics.completed_count,
            average_duration_ms=metrics.average_duration_ms,
            min_duration_ms=metrics.min_duration_ms,
            max_duration_ms=metrics.max_duration_ms,
        )


def get_task_repository(request: Request) -> TaskRepository:
    provider = cast(
        Callable[[], TaskRepository], request.app.state.task_repository_provider
    )
    return provider()


RepositoryDependency = Annotated[TaskRepository, Depends(get_task_repository)]


class _SafeTaskRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Any]:
        route_handler = super().get_route_handler()

        async def safe_route_handler(request: Request) -> Any:
            try:
                return await route_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"code": "invalid_request"},
                )
            except TaskRepositoryError:
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"code": "storage_unavailable"},
                )

        return safe_route_handler


router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    route_class=_SafeTaskRoute,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": TaskErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": TaskErrorResponse},
    },
)


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": TaskErrorResponse},
    },
)
def create_task(
    request: CreateTaskRequest, repository: RepositoryDependency
) -> TaskResponse | JSONResponse:
    if request.kind in _RESERVED_DOMAIN_TASK_KINDS:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"code": "reserved_task_kind"},
        )
    try:
        task = repository.create(request.kind, request.payload)
    except TaskValidationError:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"code": "invalid_request"},
        )
    return TaskResponse.from_snapshot(task, repository.presentation(task))


@router.get("", response_model=list[TaskResponse])
def list_tasks(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[TaskResponse]:
    return [
        TaskResponse.from_snapshot(task, repository.presentation(task))
        for task in repository.list_recent(limit=limit)
    ]


@router.get("/metrics", response_model=TaskMetricsResponse)
def get_task_metrics(repository: RepositoryDependency) -> TaskMetricsResponse:
    return TaskMetricsResponse.from_snapshot(repository.metrics())


@router.get("/{task_id}/events", response_model=list[TaskEventResponse])
def list_task_events(
    task_id: str,
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[TaskEventResponse]:
    try:
        task_events = repository.list_events(task_id, limit=limit)
        task_kind = repository.get(task_id).kind
    except TaskNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        ) from error
    return [
        TaskEventResponse.from_snapshot(
            task_event,
            repository.event_presentation(task_event, task_kind=task_kind),
        )
        for task_event in task_events
    ]


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, repository: RepositoryDependency) -> TaskResponse:
    try:
        task = repository.get(task_id)
    except TaskNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        ) from error
    return TaskResponse.from_snapshot(task, repository.presentation(task))


@router.post("/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: str, repository: RepositoryDependency) -> TaskResponse:
    try:
        task = repository.request_cancel(task_id)
    except TaskNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        ) from error
    except TaskConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task state conflict",
        ) from error
    return TaskResponse.from_snapshot(task, repository.presentation(task))
