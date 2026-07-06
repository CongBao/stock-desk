from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository, TaskValidationError


class _NullOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


def _database_url(tmp_path: Path, name: str = "enqueue.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def test_enqueue_in_transaction_allows_distinct_engine_for_same_database(
    tmp_path: Path,
) -> None:
    url = _database_url(tmp_path)
    migrate(url)
    repository_engine = create_engine_for_url(url)
    transaction_engine = create_engine_for_url(url)
    repository = TaskRepository(repository_engine)
    task_id = str(uuid4())
    now = datetime(2026, 7, 6, 1, 2, 3, tzinfo=timezone(timedelta(hours=8)))
    try:
        with transaction_engine.begin() as connection:
            task = repository.enqueue_in_transaction(
                connection,
                "market.update",
                {"nested": {"items": [1, 2]}},
                task_id=task_id,
                now=now,
            )

        assert task.id == task_id
        assert task.status == "queued"
        assert task.progress == 0.0
        assert task.payload == {"nested": {"items": (1, 2)}}
        assert task.created_at == now.astimezone(timezone.utc)
        assert repository.get(task_id) == task
        events = repository.list_events(task_id)
        assert [event.event_name for event in events] == ["task.created"]
        assert events[0].detail == {"kind": "market.update"}
        assert events[0].occurred_at == task.created_at
    finally:
        repository_engine.dispose()
        transaction_engine.dispose()


def test_enqueue_in_transaction_rejects_different_database_before_writing(
    tmp_path: Path,
) -> None:
    repository_url = _database_url(tmp_path, "repository.db")
    other_url = _database_url(tmp_path, "other.db")
    migrate(repository_url)
    migrate(other_url)
    repository_engine = create_engine_for_url(repository_url)
    other_engine = create_engine_for_url(other_url)
    repository = TaskRepository(repository_engine)
    task_id = str(uuid4())
    try:
        with other_engine.begin() as connection:
            with pytest.raises(TaskValidationError, match="database"):
                repository.enqueue_in_transaction(
                    connection,
                    "market.update",
                    {},
                    task_id=task_id,
                    now=datetime(2026, 7, 6, tzinfo=timezone.utc),
                )

        assert repository.list_recent() == []
        assert TaskRepository(other_engine).list_recent() == []
    finally:
        repository_engine.dispose()
        other_engine.dispose()


def test_enqueue_in_transaction_requires_an_active_transaction(
    tmp_path: Path,
) -> None:
    url = _database_url(tmp_path)
    migrate(url)
    engine = create_engine_for_url(url)
    repository = TaskRepository(engine)
    try:
        with engine.connect() as connection:
            assert connection.in_transaction() is False
            with pytest.raises(TaskValidationError, match="active transaction"):
                repository.enqueue_in_transaction(
                    connection,
                    "market.update",
                    {},
                    task_id=str(uuid4()),
                    now=datetime(2026, 7, 6, tzinfo=timezone.utc),
                )
        assert repository.list_recent() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("kind", "payload", "task_id", "now"),
    [
        (" market.update ", {}, str(uuid4()), datetime.now(timezone.utc)),
        (
            "market.update",
            {"value": object()},
            str(uuid4()),
            datetime.now(timezone.utc),
        ),
        ("market.update", {}, "not-a-uuid", datetime.now(timezone.utc)),
        (
            "market.update",
            {},
            str(uuid4()).upper(),
            datetime.now(timezone.utc),
        ),
        ("market.update", {}, str(uuid4()), datetime(2026, 7, 6)),
        (
            "market.update",
            {},
            str(uuid4()),
            datetime(2026, 7, 6, tzinfo=_NullOffset()),
        ),
        (
            "market.update",
            {},
            str(uuid4()),
            cast(Any, "2026-07-06T00:00:00Z"),
        ),
    ],
    ids=(
        "kind",
        "payload",
        "task-id-shape",
        "task-id-canonical",
        "naive-now",
        "null-offset",
        "non-datetime",
    ),
)
def test_enqueue_in_transaction_rejects_invalid_inputs_without_rows(
    tmp_path: Path,
    kind: str,
    payload: dict[str, object],
    task_id: str,
    now: datetime,
) -> None:
    url = _database_url(tmp_path)
    migrate(url)
    engine = create_engine_for_url(url)
    repository = TaskRepository(engine)
    try:
        with pytest.raises(TaskValidationError):
            with engine.begin() as connection:
                repository.enqueue_in_transaction(
                    connection,
                    kind,
                    payload,
                    task_id=task_id,
                    now=now,
                )
        assert repository.list_recent() == []
    finally:
        engine.dispose()


def test_create_and_transactional_enqueue_share_task_and_event_semantics(
    tmp_path: Path,
) -> None:
    url = _database_url(tmp_path)
    migrate(url)
    engine = create_engine_for_url(url)
    repository = TaskRepository(engine)
    try:
        created = repository.create("market.update", {"symbols": ["600000.SH"]})
        manual_id = str(uuid4())
        assert UUID(manual_id).version == 4
        with engine.begin() as connection:
            manual = repository.enqueue_in_transaction(
                connection,
                "market.update",
                {"symbols": ["600000.SH"]},
                task_id=manual_id,
                now=created.created_at + timedelta(seconds=1),
            )

        assert (
            manual.kind,
            manual.status,
            manual.progress,
            manual.payload,
            manual.result,
            manual.error,
            manual.cancel_requested,
            manual.worker_id,
            manual.started_at,
            manual.finished_at,
        ) == (
            created.kind,
            created.status,
            created.progress,
            created.payload,
            created.result,
            created.error,
            created.cancel_requested,
            created.worker_id,
            created.started_at,
            created.finished_at,
        )
        for task in (created, manual):
            events = repository.list_events(task.id)
            assert len(events) == 1
            assert events[0].event_name == "task.created"
            assert events[0].progress == 0.0
            assert events[0].detail == {"kind": "market.update"}
    finally:
        engine.dispose()
