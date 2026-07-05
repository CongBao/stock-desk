from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from stat import S_IMODE
import threading

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from stock_desk.storage.database import create_engine_for_url, downgrade, migrate
from stock_desk.storage.models import Base


CORE_TABLES = {"app_setting", "task_event", "task_run"}
APP_SETTING_COLUMNS = {"key", "encrypted_value", "updated_at"}
TASK_RUN_COLUMNS = {
    "id",
    "kind",
    "status",
    "progress",
    "payload_json",
    "result_json",
    "error_json",
    "cancel_requested",
    "worker_id",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
}
TASK_EVENT_COLUMNS = {
    "id",
    "task_id",
    "event_name",
    "level",
    "progress",
    "detail_json",
    "occurred_at",
}


def _dispose(engine: Engine) -> None:
    engine.dispose()


def test_upgrade_creates_core_tables(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'test.db'}"

    migrate(url, "head")
    engine = create_engine_for_url(url)

    try:
        inspector = inspect(engine)
        assert CORE_TABLES <= set(inspector.get_table_names())
        assert APP_SETTING_COLUMNS == {
            column["name"] for column in inspector.get_columns("app_setting")
        }
        assert TASK_RUN_COLUMNS <= {
            column["name"] for column in inspector.get_columns("task_run")
        }
        assert TASK_EVENT_COLUMNS == {
            column["name"] for column in inspector.get_columns("task_event")
        }
        assert inspector.get_pk_constraint("app_setting")["constrained_columns"] == [
            "key"
        ]
        assert inspector.get_pk_constraint("task_run")["constrained_columns"] == ["id"]
        assert inspector.get_pk_constraint("task_event")["constrained_columns"] == [
            "id"
        ]
        assert inspector.get_foreign_keys("task_event") == [
            {
                "name": None,
                "constrained_columns": ["task_id"],
                "referred_schema": None,
                "referred_table": "task_run",
                "referred_columns": ["id"],
                "options": {"ondelete": "CASCADE"},
            }
        ]
        assert {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes("task_run")
        } >= {("ix_task_run_status_created_at", ("status", "created_at"))}
        assert {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes("task_event")
        } == {("ix_task_event_task_id_occurred_at", ("task_id", "occurred_at"))}
    finally:
        _dispose(engine)


def test_migration_defaults_support_raw_task_run_crud(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'raw-crud.db'}"
    migrate(url)
    engine = create_engine_for_url(url)

    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO task_run (id, kind) VALUES (:id, :kind)"),
                {"id": "task-1", "kind": "backtest"},
            )
            created = (
                connection.execute(
                    text(
                        "SELECT id, kind, status, progress, payload_json, "
                        "cancel_requested, created_at, updated_at "
                        "FROM task_run WHERE id = :id"
                    ),
                    {"id": "task-1"},
                )
                .mappings()
                .one()
            )

            assert created["id"] == "task-1"
            assert created["kind"] == "backtest"
            assert created["status"] == "queued"
            assert float(created["progress"]) == 0.0
            assert json.loads(str(created["payload_json"])) == {}
            assert bool(created["cancel_requested"]) is False
            assert created["created_at"] is not None
            assert created["updated_at"] is not None

            connection.execute(
                text("UPDATE task_run SET status = :status WHERE id = :id"),
                {"id": "task-1", "status": "running"},
            )
            assert (
                connection.execute(
                    text("SELECT status FROM task_run WHERE id = :id"),
                    {"id": "task-1"},
                ).scalar_one()
                == "running"
            )

            connection.execute(
                text("DELETE FROM task_run WHERE id = :id"), {"id": "task-1"}
            )
            assert (
                connection.execute(
                    text("SELECT id FROM task_run WHERE id = :id"), {"id": "task-1"}
                ).scalar_one_or_none()
                is None
            )
    finally:
        _dispose(engine)


def test_alembic_schema_matches_orm_metadata(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'metadata.db'}"
    migrate(url)
    engine = create_engine_for_url(url)

    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, Base.metadata) == []
    finally:
        _dispose(engine)


def test_sqlite_connections_enable_safety_pragmas(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'pragmas.db'}"
    engine = create_engine_for_url(url)

    try:
        with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                foreign_keys = connection.execute(
                    text("PRAGMA foreign_keys")
                ).scalar_one()
                journal_mode = connection.execute(
                    text("PRAGMA journal_mode")
                ).scalar_one()
                busy_timeout = connection.execute(
                    text("PRAGMA busy_timeout")
                ).scalar_one()

                assert foreign_keys == 1
                assert str(journal_mode).lower() == "wal"
                assert busy_timeout >= 5_000
    finally:
        _dispose(engine)


def test_relative_sqlite_url_creates_private_parent_from_caller_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)

    engine = create_engine_for_url("sqlite:///data/nested/stock-desk.db")
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        _dispose(engine)

    database_path = foreign_cwd / "data" / "nested" / "stock-desk.db"
    assert database_path.is_file()
    assert S_IMODE(database_path.parent.stat().st_mode) == 0o700


def test_read_only_sqlite_uri_does_not_attempt_journal_mode_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "read-only.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (1)")

    url = f"sqlite:///file:{database_path.as_posix()}?mode=ro&uri=true"
    engine = create_engine_for_url(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT value FROM marker")).scalar_one() == 1
            )
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() >= 5_000
            assert (
                connection.execute(text("PRAGMA journal_mode")).scalar_one() == "delete"
            )
    finally:
        _dispose(engine)


def test_memory_sqlite_keeps_memory_journal_mode() -> None:
    engine = create_engine_for_url("sqlite:///:memory:")
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("PRAGMA journal_mode")).scalar_one() == "memory"
            )
    finally:
        _dispose(engine)


def test_sqlite_file_connections_open_safely_under_concurrency(
    tmp_path: Path,
) -> None:
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'concurrent.db'}")
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def connect_once(_worker: int) -> tuple[int, int]:
        with engine.connect() as connection:
            barrier.wait(timeout=5)
            return (
                connection.execute(text("PRAGMA foreign_keys")).scalar_one(),
                connection.execute(text("PRAGMA busy_timeout")).scalar_one(),
            )

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(connect_once, range(worker_count)))
        assert results == [(1, 5_000)] * worker_count
    finally:
        _dispose(engine)


def test_downgrade_to_base_removes_core_tables(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    migrate(url)

    downgrade(url, "base")
    engine = create_engine_for_url(url)

    try:
        assert CORE_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        _dispose(engine)


def test_migration_paths_do_not_depend_on_caller_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested_cwd = tmp_path / "elsewhere" / "nested"
    nested_cwd.mkdir(parents=True)
    database_path = tmp_path / "from-anywhere.db"
    url = f"sqlite:///{database_path}"
    monkeypatch.chdir(nested_cwd)

    migrate(url)
    engine = create_engine_for_url(url)

    try:
        assert CORE_TABLES <= set(inspect(engine).get_table_names())
    finally:
        _dispose(engine)

    downgrade(url, "base")
    engine = create_engine_for_url(url)

    try:
        assert CORE_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        _dispose(engine)
