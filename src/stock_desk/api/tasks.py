from datetime import datetime
from typing import Annotated, Any, Callable, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from stock_desk.tasks.models import TaskSnapshot, TaskStatus
from stock_desk.tasks.repository import (
    TaskConflict,
    TaskNotFound,
    TaskRepository,
    TaskValidationError,
)


class CreateTaskRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def kind_must_not_be_blank(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("Task kind must not be blank or padded")
        return value


class TaskResponse(BaseModel):
    id: str
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

    @classmethod
    def from_snapshot(cls, task: TaskSnapshot) -> "TaskResponse":
        return cls(
            id=task.id,
            kind=task.kind,
            status=task.status,
            progress=task.progress,
            payload=dict(task.payload),
            result=dict(task.result) if task.result is not None else None,
            error=dict(task.error) if task.error is not None else None,
            cancel_requested=task.cancel_requested,
            worker_id=task.worker_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )


def get_task_repository(request: Request) -> TaskRepository:
    provider = cast(
        Callable[[], TaskRepository], request.app.state.task_repository_provider
    )
    return provider()


RepositoryDependency = Annotated[TaskRepository, Depends(get_task_repository)]

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    request: CreateTaskRequest, repository: RepositoryDependency
) -> TaskResponse:
    try:
        task = repository.create(request.kind, request.payload)
    except TaskValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid task",
        ) from error
    return TaskResponse.from_snapshot(task)


@router.get("", response_model=list[TaskResponse])
def list_tasks(
    repository: RepositoryDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[TaskResponse]:
    return [
        TaskResponse.from_snapshot(task) for task in repository.list_recent(limit=limit)
    ]


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, repository: RepositoryDependency) -> TaskResponse:
    try:
        task = repository.get(task_id)
    except TaskNotFound as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        ) from error
    return TaskResponse.from_snapshot(task)


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
    return TaskResponse.from_snapshot(task)
