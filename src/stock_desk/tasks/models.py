from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, TypeAlias


TaskStatus: TypeAlias = Literal["queued", "running", "succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """Immutable client-facing view of a durable task."""

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
