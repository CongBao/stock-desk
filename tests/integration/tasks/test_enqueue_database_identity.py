from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.engine import Connection

from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import TaskEvent, TaskRun
from stock_desk.tasks.repository import TaskRepository, TaskValidationError


NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        return (
            int(connection.execute(select(func.count(TaskRun.id))).scalar_one()),
            int(connection.execute(select(func.count(TaskEvent.id))).scalar_one()),
        )


def _immutable_url(path: Path) -> str:
    return f"sqlite:///file:{path.as_posix()}?mode=ro&immutable=1&uri=true"


def _attempt_enqueue(
    repository: TaskRepository,
    connection: Connection,
) -> TaskValidationError | None:
    try:
        repository.enqueue_in_transaction(
            connection,
            "market.update",
            {},
            task_id=str(uuid4()),
            now=NOW,
        )
    except TaskValidationError as error:
        return error
    return None


def test_relative_urls_connected_in_different_cwds_are_rejected_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_a = tmp_path / "a"
    directory_b = tmp_path / "b"
    directory_a.mkdir()
    directory_b.mkdir()
    relative_url = "sqlite:///same.db"

    monkeypatch.chdir(directory_a)
    migrate(relative_url)
    repository_engine = create_engine_for_url(relative_url)
    repository = TaskRepository(repository_engine)

    monkeypatch.chdir(directory_b)
    migrate(relative_url)
    transaction_engine = create_engine_for_url(relative_url)
    try:
        with transaction_engine.begin() as connection:
            error = _attempt_enqueue(repository, connection)

        assert isinstance(error, TaskValidationError)
        assert "database" in str(error)
        assert _counts(repository_engine) == (0, 0)
        assert _counts(transaction_engine) == (0, 0)
    finally:
        repository_engine.dispose()
        transaction_engine.dispose()


def test_relative_urls_remain_bound_after_connections_and_cwd_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_directory = tmp_path / "opened"
    later_directory = tmp_path / "later"
    opened_directory.mkdir()
    later_directory.mkdir()
    relative_url = "sqlite:///same.db"

    monkeypatch.chdir(opened_directory)
    migrate(relative_url)
    repository_engine = create_engine_for_url(relative_url)
    transaction_engine = create_engine_for_url(relative_url)
    repository = TaskRepository(repository_engine)
    connection = transaction_engine.connect()
    transaction = connection.begin()
    try:
        monkeypatch.chdir(later_directory)
        task = repository.enqueue_in_transaction(
            connection,
            "market.update",
            {},
            task_id=str(uuid4()),
            now=NOW,
        )
        transaction.commit()

        assert repository.get(task.id) == task
        assert _counts(repository_engine) == (1, 1)
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        repository_engine.dispose()
        transaction_engine.dispose()


def test_open_connection_keeps_original_inode_across_atomic_path_replace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.db"
    replacement = tmp_path / "replacement.db"
    original_inode = tmp_path / "original-inode.db"
    migrate(f"sqlite:///{database}")
    migrate(f"sqlite:///{replacement}")

    old_engine = create_engine_for_url(_immutable_url(database))
    old_connection = old_engine.connect()
    old_transaction = old_connection.begin()
    os.replace(database, original_inode)
    os.replace(replacement, database)

    repository_engine = create_engine_for_url(_immutable_url(database))
    repository = TaskRepository(repository_engine)
    try:
        error = _attempt_enqueue(repository, old_connection)
        assert isinstance(error, TaskValidationError)
        assert "database" in str(error)
        assert old_connection.in_transaction()

        old_transaction.rollback()
        old_connection.close()
        original_engine = create_engine_for_url(_immutable_url(original_inode))
        try:
            assert _counts(original_engine) == (0, 0)
            assert _counts(repository_engine) == (0, 0)
        finally:
            original_engine.dispose()
    finally:
        if old_transaction.is_active:
            old_transaction.rollback()
        old_connection.close()
        old_engine.dispose()
        repository_engine.dispose()


def test_stable_symlink_alias_to_same_file_is_allowed(tmp_path: Path) -> None:
    database = tmp_path / "database.db"
    alias = tmp_path / "alias.db"
    url = f"sqlite:///{database}"
    migrate(url)
    alias.symlink_to(database)
    repository_engine = create_engine_for_url(url)
    transaction_engine = create_engine_for_url(f"sqlite:///{alias}")
    repository = TaskRepository(repository_engine)
    try:
        with transaction_engine.begin() as connection:
            task = repository.enqueue_in_transaction(
                connection,
                "market.update",
                {},
                task_id=str(uuid4()),
                now=NOW,
            )
        assert repository.get(task.id) == task
        assert _counts(repository_engine) == (1, 1)
    finally:
        repository_engine.dispose()
        transaction_engine.dispose()


def test_symlink_rebind_rejects_new_connection_to_new_target(tmp_path: Path) -> None:
    database_a = tmp_path / "a.db"
    database_b = tmp_path / "b.db"
    alias = tmp_path / "alias.db"
    migrate(f"sqlite:///{database_a}")
    migrate(f"sqlite:///{database_b}")
    alias.symlink_to(database_a)
    repository_engine = create_engine_for_url(f"sqlite:///{alias}")
    repository = TaskRepository(repository_engine)

    alias.unlink()
    alias.symlink_to(database_b)
    transaction_engine = create_engine_for_url(f"sqlite:///{alias}")
    try:
        with transaction_engine.begin() as connection:
            error = _attempt_enqueue(repository, connection)
        assert isinstance(error, TaskValidationError)
        assert _counts(repository_engine) == (0, 0)
        assert _counts(transaction_engine) == (0, 0)
    finally:
        repository_engine.dispose()
        transaction_engine.dispose()


def test_open_connection_keeps_live_target_across_symlink_rebind(
    tmp_path: Path,
) -> None:
    database_a = tmp_path / "a.db"
    database_b = tmp_path / "b.db"
    alias = tmp_path / "alias.db"
    migrate(f"sqlite:///{database_a}")
    migrate(f"sqlite:///{database_b}")
    alias.symlink_to(database_a)
    repository_engine = create_engine_for_url(f"sqlite:///{alias}")
    transaction_engine = create_engine_for_url(f"sqlite:///{alias}")
    repository = TaskRepository(repository_engine)
    connection = transaction_engine.connect()
    transaction = connection.begin()
    try:
        alias.unlink()
        alias.symlink_to(database_b)
        task = repository.enqueue_in_transaction(
            connection,
            "market.update",
            {},
            task_id=str(uuid4()),
            now=NOW,
        )
        transaction.commit()
        assert repository.get(task.id) == task
        assert _counts(repository_engine) == (1, 1)
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        repository_engine.dispose()
        transaction_engine.dispose()


def test_hardlink_alias_is_rejected_even_with_same_inode(tmp_path: Path) -> None:
    database = tmp_path / "database.db"
    alias = tmp_path / "hardlink.db"
    migrate(f"sqlite:///{database}")
    os.link(database, alias)
    repository_engine = create_engine_for_url(f"sqlite:///{database}")
    transaction_engine = create_engine_for_url(f"sqlite:///{alias}")
    repository = TaskRepository(repository_engine)
    try:
        with transaction_engine.begin() as connection:
            error = _attempt_enqueue(repository, connection)
        assert isinstance(error, TaskValidationError)
        assert _counts(repository_engine) == (0, 0)
    finally:
        repository_engine.dispose()
        transaction_engine.dispose()
