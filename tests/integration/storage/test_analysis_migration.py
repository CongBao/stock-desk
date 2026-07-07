from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from stock_desk.analysis.repository import AnalysisRepository
from stock_desk.analysis.retry import RetryPolicy
from stock_desk.storage.database import create_engine_for_url, downgrade, migrate
from stock_desk.tasks.repository import TaskRepository


ANALYSIS_COLUMNS = {
    "analysis_run": {
        "id",
        "task_id",
        "parent_run_id",
        "requested_stage",
        "symbol",
        "model_config_id",
        "model_provider",
        "model_name",
        "model_config_json",
        "model_config_hash",
        "status",
        "current_stage",
        "error_json",
        "config_fingerprint",
        "snapshot_id",
        "snapshot_json",
        "snapshot_hash",
        "evidence_graph_json",
        "evidence_graph_hash",
        "retry_policy_json",
        "retry_policy_hash",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    },
    "analysis_stage": {
        "run_id",
        "role",
        "ordinal",
        "status",
        "source_run_id",
        "source_role",
        "output_json",
        "output_hash",
        "trace_json",
        "trace_hash",
        "failure_code",
        "retryable",
        "attempt_count",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    },
    "analysis_attempt": {
        "run_id",
        "role",
        "attempt_no",
        "status",
        "provider",
        "model",
        "request_hash",
        "error_json",
        "retryable",
        "backoff_seconds",
        "template_version",
        "template_hash",
        "usage_json",
        "started_at",
        "finished_at",
    },
    "analysis_report": {
        "run_id",
        "report_id",
        "report_json",
        "report_hash",
        "created_at",
    },
}


def test_analysis_revision_upgrades_downgrades_and_reupgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-migration.db'}"

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        inspector = inspect(engine)
        assert ANALYSIS_COLUMNS.keys() <= set(inspector.get_table_names())
        for table, columns in ANALYSIS_COLUMNS.items():
            assert columns == {
                column["name"] for column in inspector.get_columns(table)
            }
        report_indexes = {
            (item["name"], bool(item["unique"]))
            for item in inspector.get_indexes("analysis_report")
        }
        assert ("ix_analysis_report_id", False) in report_indexes
    finally:
        engine.dispose()

    downgrade(url, "0007_backtest_runs")
    engine = create_engine_for_url(url)
    try:
        assert ANALYSIS_COLUMNS.keys().isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert ANALYSIS_COLUMNS.keys() <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_active_retry_index_upgrades_parent_globally_and_downgrades_to_stage_scope(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'retry-uniqueness.db'}"
    migrate(url, "0009_analysis_model_configs")
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    now = datetime(2026, 7, 8, 9, tzinfo=timezone.utc)
    parent_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    parent = repository._create_run_for_existing_task(
        task_id=parent_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
    )
    first_child_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    first_child = repository._create_run_for_existing_task(
        task_id=first_child_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
        parent_run_id=parent.id,
        requested_stage="bull",
    )

    def retry_index_columns() -> tuple[str, ...]:
        index = next(
            item
            for item in inspect(engine).get_indexes("analysis_run")
            if item["name"] == "uq_analysis_run_active_retry"
        )
        assert bool(index["unique"]) is True
        return tuple(index["column_names"])

    assert retry_index_columns() == ("parent_run_id", "requested_stage")
    with engine.connect() as connection:
        preserved = tuple(
            connection.execute(
                text(
                    "SELECT id,parent_run_id,requested_stage,status "
                    "FROM analysis_run ORDER BY id"
                )
            )
        )

    migrate(url, "0010_parent_active_retry")
    assert retry_index_columns() == ("parent_run_id",)
    with engine.connect() as connection:
        assert (
            tuple(
                connection.execute(
                    text(
                        "SELECT id,parent_run_id,requested_stage,status "
                        "FROM analysis_run ORDER BY id"
                    )
                )
            )
            == preserved
        )

    second_child_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    with pytest.raises(IntegrityError):
        repository._create_run_for_existing_task(
            task_id=second_child_task.id,
            symbol="600000.SH",
            retry_policy=RetryPolicy(max_retries=0),
            now=now,
            parent_run_id=parent.id,
            requested_stage="bear",
        )

    downgrade(url, "0009_analysis_model_configs")
    assert retry_index_columns() == ("parent_run_id", "requested_stage")
    second_child = repository._create_run_for_existing_task(
        task_id=second_child_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
        parent_run_id=parent.id,
        requested_stage="bear",
    )
    assert first_child.requested_stage == "bull"
    assert second_child.requested_stage == "bear"
    engine.dispose()


def test_active_retry_index_upgrade_fails_safely_before_existing_duplicate_rows(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'retry-duplicate-preflight.db'}"
    migrate(url, "0009_analysis_model_configs")
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    now = datetime(2026, 7, 8, 9, tzinfo=timezone.utc)
    parent_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    parent = repository._create_run_for_existing_task(
        task_id=parent_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
    )
    for stage in ("bull", "bear"):
        child_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
        repository._create_run_for_existing_task(
            task_id=child_task.id,
            symbol="600000.SH",
            retry_policy=RetryPolicy(max_retries=0),
            now=now,
            parent_run_id=parent.id,
            requested_stage=stage,
        )

    with pytest.raises(
        RuntimeError,
        match="multiple active analysis retries exist for one parent",
    ):
        migrate(url, "0010_parent_active_retry")

    index = next(
        item
        for item in inspect(engine).get_indexes("analysis_run")
        if item["name"] == "uq_analysis_run_active_retry"
    )
    assert tuple(index["column_names"]) == ("parent_run_id", "requested_stage")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0009_analysis_model_configs"
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM analysis_run WHERE parent_run_id=:id"),
                {"id": parent.id},
            ).scalar_one()
            == 2
        )
    engine.dispose()


def test_active_retry_migration_holds_sqlite_write_lock_against_racing_child(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'retry-migration-race.db'}"
    migrate(url, "0009_analysis_model_configs")
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    now = datetime(2026, 7, 8, 9, tzinfo=timezone.utc)
    parent_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    parent = repository._create_run_for_existing_task(
        task_id=parent_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
    )
    first_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    repository._create_run_for_existing_task(
        task_id=first_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
        parent_run_id=parent.id,
        requested_stage="bull",
    )
    racing_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    migration_locked = Event()
    release_migration = Event()
    writer_started = Event()

    def observe_migration_lock(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized == "update alembic_version set version_num=version_num":
            migration_locked.set()
            assert release_migration.wait(timeout=5)

    def create_racing_child() -> object:
        writer_started.set()
        return repository._create_run_for_existing_task(
            task_id=racing_task.id,
            symbol="600000.SH",
            retry_policy=RetryPolicy(max_retries=0),
            now=now,
            parent_run_id=parent.id,
            requested_stage="bear",
        )

    event.listen(Engine, "after_cursor_execute", observe_migration_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            migration = executor.submit(migrate, url, "0010_parent_active_retry")
            assert migration_locked.wait(timeout=5)
            writer = executor.submit(create_racing_child)
            assert writer_started.wait(timeout=5)
            release_migration.set()
            migration.result(timeout=5)
            with pytest.raises(IntegrityError):
                writer.result(timeout=5)
    finally:
        migration_locked.set()
        release_migration.set()
        event.remove(Engine, "after_cursor_execute", observe_migration_lock)

    index = next(
        item
        for item in inspect(engine).get_indexes("analysis_run")
        if item["name"] == "uq_analysis_run_active_retry"
    )
    assert tuple(index["column_names"]) == ("parent_run_id",)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0010_parent_active_retry"
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM analysis_run WHERE parent_run_id=:id"),
                {"id": parent.id},
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_active_retry_downgrade_holds_sqlite_write_lock_against_same_stage_child(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'retry-downgrade-race.db'}"
    migrate(url, "0010_parent_active_retry")
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    now = datetime(2026, 7, 8, 9, tzinfo=timezone.utc)
    parent_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    parent = repository._create_run_for_existing_task(
        task_id=parent_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
    )
    first_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    repository._create_run_for_existing_task(
        task_id=first_task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=now,
        parent_run_id=parent.id,
        requested_stage="bull",
    )
    racing_task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    downgrade_locked = Event()
    release_downgrade = Event()
    writer_started = Event()

    def observe_downgrade_lock(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized == "update alembic_version set version_num=version_num":
            downgrade_locked.set()
            assert release_downgrade.wait(timeout=5)

    def create_racing_child() -> object:
        writer_started.set()
        return repository._create_run_for_existing_task(
            task_id=racing_task.id,
            symbol="600000.SH",
            retry_policy=RetryPolicy(max_retries=0),
            now=now,
            parent_run_id=parent.id,
            requested_stage="bull",
        )

    event.listen(Engine, "after_cursor_execute", observe_downgrade_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            migration = executor.submit(downgrade, url, "0009_analysis_model_configs")
            assert downgrade_locked.wait(timeout=5)
            writer = executor.submit(create_racing_child)
            assert writer_started.wait(timeout=5)
            release_downgrade.set()
            migration.result(timeout=5)
            with pytest.raises(IntegrityError):
                writer.result(timeout=5)
    finally:
        downgrade_locked.set()
        release_downgrade.set()
        event.remove(Engine, "after_cursor_execute", observe_downgrade_lock)

    index = next(
        item
        for item in inspect(engine).get_indexes("analysis_run")
        if item["name"] == "uq_analysis_run_active_retry"
    )
    assert tuple(index["column_names"]) == ("parent_run_id", "requested_stage")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0009_analysis_model_configs"
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM analysis_run WHERE parent_run_id=:id"),
                {"id": parent.id},
            ).scalar_one()
            == 1
        )
    engine.dispose()


def test_active_retry_downgrade_ddl_failure_preserves_0010_index_and_revision(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'retry-downgrade-failure.db'}"
    migrate(url, "0010_parent_active_retry")
    engine = create_engine_for_url(url)

    def fail_old_index_creation(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if (
            normalized.startswith("create unique index uq_analysis_run_active_retry")
            and "(parent_run_id, requested_stage)" in normalized
        ):
            raise RuntimeError("injected index creation failure")

    event.listen(Engine, "before_cursor_execute", fail_old_index_creation)
    try:
        with pytest.raises(RuntimeError, match="injected index creation failure"):
            downgrade(url, "0009_analysis_model_configs")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_old_index_creation)

    index = next(
        item
        for item in inspect(engine).get_indexes("analysis_run")
        if item["name"] == "uq_analysis_run_active_retry"
    )
    assert tuple(index["column_names"]) == ("parent_run_id",)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0010_parent_active_retry"
        )
    engine.dispose()
