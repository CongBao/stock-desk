from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, func, select

from stock_desk.storage.database import create_engine_for_url
from stock_desk.storage.models import Base, TaskEvent, TaskRun
from stock_desk.tasks.repository import TaskRepository, TaskValidationError


NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _memory_engine() -> Engine:
    engine = create_engine_for_url("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def test_in_memory_connection_from_same_pool_is_allowed() -> None:
    engine = _memory_engine()
    same_pool_engine = engine.execution_options()
    repository = TaskRepository(engine)
    try:
        with same_pool_engine.begin() as connection:
            task = repository.enqueue_in_transaction(
                connection,
                "market.update",
                {},
                task_id=str(uuid4()),
                now=NOW,
            )
        assert repository.get(task.id) == task
    finally:
        same_pool_engine.dispose()
        engine.dispose()


def test_in_memory_connection_from_different_pool_is_rejected() -> None:
    repository_engine = _memory_engine()
    transaction_engine = _memory_engine()
    repository = TaskRepository(repository_engine)
    try:
        with transaction_engine.begin() as connection:
            with pytest.raises(TaskValidationError, match="database"):
                repository.enqueue_in_transaction(
                    connection,
                    "market.update",
                    {},
                    task_id=str(uuid4()),
                    now=NOW,
                )
            assert connection.in_transaction()
    finally:
        repository_engine.dispose()
        transaction_engine.dispose()


def test_singleton_pool_worker_database_is_rejected_before_writes() -> None:
    engine = _memory_engine()
    repository = TaskRepository(engine)

    def enqueue_in_worker_database() -> tuple[TaskValidationError | None, int, int]:
        with engine.connect() as connection:
            try:
                Base.metadata.create_all(connection)
                connection.commit()
                error: TaskValidationError | None = None
                with connection.begin():
                    try:
                        repository.enqueue_in_transaction(
                            connection,
                            "market.update",
                            {},
                            task_id=str(uuid4()),
                            now=NOW,
                        )
                    except TaskValidationError as caught:
                        error = caught
                result = (
                    error,
                    int(
                        connection.execute(select(func.count(TaskRun.id))).scalar_one()
                    ),
                    int(
                        connection.execute(
                            select(func.count(TaskEvent.id))
                        ).scalar_one()
                    ),
                )
                connection.rollback()
                return result
            finally:
                connection.invalidate()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            error, task_count, event_count = executor.submit(
                enqueue_in_worker_database
            ).result(timeout=10)

        assert isinstance(error, TaskValidationError)
        assert "database" in str(error)
        assert (task_count, event_count) == (0, 0)
        assert repository.list_recent() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        (),
        ("sqlite-memory",),
        ("sqlite-memory", "not-an-id"),
        ("sqlite-file", "relative.db", 1, 2),
        ("sqlite-file", "/tmp/database.db", "1", 2),
    ],
    ids=(
        "missing",
        "empty",
        "short-memory",
        "invalid-memory-id",
        "relative-file",
        "invalid-file-device",
    ),
)
def test_missing_or_malformed_connection_stamp_fails_before_writes(
    stamp: object,
) -> None:
    engine = _memory_engine()
    repository = TaskRepository(engine)
    try:
        with engine.begin() as connection:
            if stamp is None:
                connection.info.pop("stock_desk.sqlite_database_identity", None)
            else:
                connection.info["stock_desk.sqlite_database_identity"] = stamp
            with pytest.raises(TaskValidationError, match="database identity"):
                repository.enqueue_in_transaction(
                    connection,
                    "market.update",
                    {},
                    task_id=str(uuid4()),
                    now=NOW,
                )
        assert repository.list_recent() == []
    finally:
        engine.dispose()


def test_identity_check_uses_only_the_passed_active_connection() -> None:
    engine = _memory_engine()
    repository = TaskRepository(engine)
    checkout_count = 0
    statements: list[str] = []

    def count_checkout(*_args: Any) -> None:
        nonlocal checkout_count
        checkout_count += 1

    def capture_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "checkout", count_checkout)
    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        with engine.begin() as connection:
            before = checkout_count
            repository.enqueue_in_transaction(
                connection,
                "market.update",
                {},
                task_id=str(uuid4()),
                now=NOW,
            )
            assert checkout_count == before
            assert connection.in_transaction()
        assert not any("database_list" in statement for statement in statements)
    finally:
        event.remove(engine, "checkout", count_checkout)
        event.remove(engine, "before_cursor_execute", capture_statement)
        engine.dispose()
