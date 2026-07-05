from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from stock_desk.storage.database import create_engine_for_url, downgrade, migrate


CORE_TABLES = {"app_setting", "task_run"}
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
    finally:
        _dispose(engine)


def test_sqlite_connections_enable_safety_pragmas(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'pragmas.db'}"
    engine = create_engine_for_url(url)

    try:
        with engine.connect() as connection:
            foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
            journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()

        assert foreign_keys == 1
        assert str(journal_mode).lower() == "wal"
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
