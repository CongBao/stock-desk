from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from stat import S_IMODE
import subprocess
import sys
import threading

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from stock_desk.storage.database import create_engine_for_url, downgrade, migrate
from stock_desk.storage.metadata import Base
from stock_desk.tasks.repository import TaskRepository


HEAD_REVISION = "0011_worker_heartbeat"
INSTRUMENT_TABLES = {
    "instrument_dataset",
    "instrument_dataset_item",
    "instrument_routing_manifest",
}
POOL_TABLES = {
    "preset_pool_snapshot",
    "preset_pool_member",
    "custom_pool",
    "custom_pool_member",
}
CATALOG_TABLES = {
    "market_dataset",
    "market_dataset_timestamp",
    "market_dataset_timestamp_seal",
    "market_dataset_partition",
    "market_routing_manifest",
    "market_update_item",
    "market_update_occurrence",
    "market_update_schedule",
    *INSTRUMENT_TABLES,
    *POOL_TABLES,
}
FORMULA_TABLES = {"formula", "formula_draft", "formula_version"}
EXECUTION_STATUS_TABLES = {
    "execution_status_dataset",
    "execution_status_routing_manifest",
}
BACKTEST_TABLES = {
    "backtest_run",
    "backtest_symbol",
    "backtest_trade",
    "backtest_order_event",
    "backtest_failure",
    "backtest_log",
    "backtest_aggregate_metric",
    "backtest_group_metric",
}
ANALYSIS_TABLES = {
    "analysis_run",
    "analysis_stage",
    "analysis_attempt",
    "analysis_report",
}
MODEL_CONFIG_TABLES = {"analysis_model_config"}
WORKER_HEARTBEAT_TABLES = {"task_worker_heartbeat"}
CORE_TABLES = {
    "app_setting",
    "task_event",
    "task_run",
    *CATALOG_TABLES,
    *FORMULA_TABLES,
    *EXECUTION_STATUS_TABLES,
    *BACKTEST_TABLES,
    *ANALYSIS_TABLES,
    *MODEL_CONFIG_TABLES,
    *WORKER_HEARTBEAT_TABLES,
}
BACKTEST_TABLE_COLUMNS = {
    "backtest_run": {
        "id",
        "task_id",
        "snapshot_id",
        "snapshot_json",
        "status",
        "stage",
        "total",
        "processed",
        "failed_count",
        "result_hash",
        "actual_warmup_start",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    },
    "backtest_symbol": {
        "run_id",
        "ordinal",
        "symbol",
        "input_kind",
        "reference_json",
        "status",
        "signal_series_id",
        "warmup_start",
        "failure_reason",
        "created_at",
        "updated_at",
    },
    "backtest_trade": {"run_id", "symbol", "ordinal", "realized", "payload_json"},
    "backtest_order_event": {
        "run_id",
        "symbol",
        "ordinal",
        "event_type",
        "payload_json",
    },
    "backtest_failure": {"run_id", "symbol", "ordinal", "reason", "detail_json"},
    "backtest_log": {"run_id", "ordinal", "level", "message", "detail_json"},
    "backtest_aggregate_metric": {"run_id", "metric_key", "payload_json"},
    "backtest_group_metric": {"run_id", "dimension", "group_key", "payload_json"},
}
LEGACY_CORE_TABLES = {"app_setting", "task_run"}
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
    "claim_token",
    "lease_expires_at",
    "heartbeat_at",
    "attempt_count",
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
WORKER_HEARTBEAT_COLUMNS = {"worker_id", "heartbeat_at"}
MARKET_TABLE_COLUMNS = {
    "execution_status_dataset": {
        "dataset_version",
        "source",
        "symbol",
        "exchange",
        "query_start",
        "query_end",
        "period",
        "fetched_at",
        "data_cutoff",
        "row_count",
        "snapshot_json",
        "created_at",
    },
    "execution_status_routing_manifest": {
        "manifest_record_id",
        "dataset_version",
        "route_version",
        "manifest_json",
        "fetched_at",
        "created_at",
    },
    "instrument_dataset": {
        "dataset_version",
        "source",
        "data_cutoff",
        "row_count",
        "created_at",
    },
    "instrument_dataset_item": {
        "dataset_version",
        "symbol",
        "ordinal",
        "exchange",
        "name",
        "instrument_kind",
        "listing_status",
        "listed_on",
        "delisted_on",
        "created_at",
    },
    "instrument_routing_manifest": {
        "manifest_record_id",
        "dataset_version",
        "route_version",
        "manifest_json",
        "fetched_at",
        "data_cutoff",
        "created_at",
    },
    "preset_pool_snapshot": {
        "snapshot_id",
        "pool_id",
        "preset_key",
        "category",
        "display_name",
        "source",
        "composition_dataset_version",
        "composition_route_version",
        "fetched_at",
        "data_cutoff",
        "complete",
        "instrument_manifest_record_id",
        "instrument_dataset_version",
        "member_count",
        "created_at",
    },
    "preset_pool_member": {
        "snapshot_id",
        "ordinal",
        "instrument_dataset_version",
        "symbol",
        "created_at",
    },
    "custom_pool": {
        "pool_id",
        "name",
        "revision",
        "instrument_manifest_record_id",
        "instrument_dataset_version",
        "member_count",
        "member_digest",
        "state_digest",
        "created_at",
        "updated_at",
    },
    "custom_pool_member": {
        "pool_id",
        "ordinal",
        "member_revision",
        "instrument_dataset_version",
        "symbol",
    },
    "market_dataset": {
        "dataset_version",
        "source",
        "symbol",
        "period",
        "adjustment",
        "query_start",
        "query_end",
        "data_cutoff",
        "row_count",
        "created_at",
    },
    "market_dataset_partition": {
        "partition_manifest_id",
        "dataset_version",
        "partition_year",
        "relative_path",
        "row_count",
        "byte_size",
        "physical_sha256",
        "created_at",
    },
    "market_dataset_timestamp": {
        "dataset_version",
        "ordinal",
        "timestamp",
    },
    "market_dataset_timestamp_seal": {
        "dataset_version",
        "index_version",
        "row_count",
        "timestamp_digest",
    },
    "market_routing_manifest": {
        "manifest_record_id",
        "dataset_version",
        "symbol",
        "route_version",
        "manifest_json",
        "fetched_at",
        "created_at",
    },
    "market_update_item": {
        "task_id",
        "ordinal",
        "symbol",
        "status",
        "manifest_record_id",
        "dataset_version",
        "reason",
        "created_at",
    },
    "market_update_schedule": {
        "id",
        "enabled",
        "timezone",
        "local_time",
        "payload_json",
        "last_enqueued_local_date",
        "created_at",
        "updated_at",
    },
    "market_update_occurrence": {
        "schedule_id",
        "local_date",
        "task_id",
        "created_at",
    },
}
FORMULA_TABLE_COLUMNS = {
    "formula": {
        "id",
        "name",
        "formula_type",
        "placement",
        "latest_version",
        "created_at",
        "updated_at",
    },
    "formula_version": {
        "id",
        "formula_id",
        "version",
        "name",
        "formula_type",
        "placement",
        "source",
        "parameter_schema_json",
        "compatibility_version",
        "engine_version",
        "checksum",
        "validation_result_json",
        "copied_from_version_id",
        "created_at",
    },
    "formula_draft": {
        "formula_id",
        "revision",
        "source",
        "source_checksum",
        "parameter_schema_json",
        "validation_result_json",
        "executable_version_id",
        "updated_at",
    },
}
IMMUTABLE_TRIGGER_NAMES = {
    f"trg_{table}_{operation}"
    for table in (
        "instrument_dataset",
        "instrument_dataset_item",
        "instrument_routing_manifest",
        "preset_pool_snapshot",
        "preset_pool_member",
        "market_dataset",
        "market_dataset_partition",
        "market_routing_manifest",
        "market_update_item",
        "market_update_occurrence",
    )
    for operation in ("immutable_insert", "immutable_update", "immutable_delete")
}
UPDATE_ITEM_OWNER_TRIGGER = "trg_market_update_item_owner_running"
UPDATE_ITEM_DUPLICATE_TRIGGER = "trg_market_update_item_immutable_insert"
MARKET_TRIGGER_NAMES = {*IMMUTABLE_TRIGGER_NAMES, UPDATE_ITEM_OWNER_TRIGGER}
EXECUTION_STATUS_TRIGGER_NAMES = {
    f"trg_{table}_immutable_{operation}"
    for table in (
        "execution_status_dataset",
        "execution_status_routing_manifest",
    )
    for operation in ("insert", "update", "delete")
}
BACKTEST_TRIGGER_NAMES = {
    f"trg_{table}_terminal_{operation}"
    for table in BACKTEST_TABLES
    for operation in ("insert", "update", "delete")
}
ANALYSIS_TRIGGER_NAMES = {
    *{
        f"trg_{table}_immutable_{operation}"
        for table in ANALYSIS_TABLES
        for operation in ("insert", "update", "delete")
    },
    *{
        f"trg_{table}_owner_terminal_{operation}"
        for table in {"analysis_stage", "analysis_attempt", "analysis_report"}
        for operation in ("insert", "update", "delete")
    },
    "trg_analysis_run_bind_once",
    "trg_analysis_run_terminal_guard",
    "trg_analysis_report_identity_guard",
    "trg_analysis_run_config_immutable",
    "trg_analysis_stage_reuse_identity",
    "trg_analysis_stage_reuse_identity_insert",
    "trg_analysis_stage_identity_immutable",
    "trg_analysis_attempt_identity_immutable",
    "trg_analysis_report_identity_immutable",
}
MARKET_TIMESTAMP_TRIGGER_NAMES = {
    f"trg_market_dataset_timestamp_immutable_{operation}"
    for operation in ("insert", "update", "delete")
}
MARKET_TIMESTAMP_TRIGGER_NAMES |= {
    f"trg_market_dataset_timestamp_seal_immutable_{operation}"
    for operation in ("insert", "update", "delete")
}
FORMULA_TRIGGER_NAMES = {
    "trg_formula_version_immutable_insert",
    "trg_formula_version_immutable_update",
    "trg_formula_version_immutable_delete",
    "trg_formula_version_owner",
    "trg_formula_draft_executable_insert",
    "trg_formula_draft_executable_update",
}
ALL_TRIGGER_NAMES = {
    *MARKET_TRIGGER_NAMES,
    *FORMULA_TRIGGER_NAMES,
    *EXECUTION_STATUS_TRIGGER_NAMES,
    *BACKTEST_TRIGGER_NAMES,
    *ANALYSIS_TRIGGER_NAMES,
    *MARKET_TIMESTAMP_TRIGGER_NAMES,
    "trg_analysis_model_config_immutable_update",
    "trg_analysis_model_config_no_replace",
    "trg_analysis_model_config_no_delete",
    "trg_analysis_model_config_mutation_guard",
    "trg_analysis_model_config_disabled_terminal",
    "trg_analysis_model_config_initial_revision",
}


def _dispose(engine: Engine) -> None:
    engine.dispose()


def _current_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        )


def _index_signatures(
    engine: Engine, table: str
) -> set[tuple[str, tuple[str, ...], bool]]:
    return {
        (str(index["name"]), tuple(index["column_names"]), bool(index["unique"]))
        for index in inspect(engine).get_indexes(table)
    }


def _unique_signatures(engine: Engine, table: str) -> set[tuple[str, ...]]:
    return {
        tuple(constraint["column_names"])
        for constraint in inspect(engine).get_unique_constraints(table)
    }


def _check_names(engine: Engine, table: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in inspect(engine).get_check_constraints(table)
    }


def _trigger_names(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {
            str(name)
            for name in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).scalars()
        }


def _trigger_sql(engine: Engine, name: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND name = :name"
                ),
                {"name": name},
            ).scalar_one()
        )


def _create_legacy_0001_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL,
                CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
            );
            INSERT INTO alembic_version (version_num)
            VALUES ('0001_core_tables');

            CREATE TABLE app_setting (
                key VARCHAR(255) NOT NULL,
                encrypted_value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                PRIMARY KEY (key)
            );
            CREATE TABLE task_run (
                id VARCHAR(36) NOT NULL,
                kind VARCHAR(64) NOT NULL,
                status VARCHAR(32) DEFAULT 'queued' NOT NULL,
                progress FLOAT DEFAULT '0' NOT NULL,
                payload_json JSON DEFAULT '{}' NOT NULL,
                result_json JSON,
                error_json JSON,
                cancel_requested BOOLEAN DEFAULT 0 NOT NULL,
                worker_id VARCHAR(255),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                started_at DATETIME,
                finished_at DATETIME,
                PRIMARY KEY (id)
            );
            CREATE INDEX ix_task_run_status_created_at
            ON task_run (status, created_at);
            """
        )


def test_upgrade_creates_core_tables(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'test.db'}"

    migrate(url, "head")
    engine = create_engine_for_url(url)

    try:
        inspector = inspect(engine)
        assert _current_revision(engine) == HEAD_REVISION
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
        assert WORKER_HEARTBEAT_COLUMNS == {
            column["name"] for column in inspector.get_columns("task_worker_heartbeat")
        }
        for table, expected_columns in MARKET_TABLE_COLUMNS.items():
            assert expected_columns == {
                column["name"] for column in inspector.get_columns(table)
            }
        for table, expected_columns in FORMULA_TABLE_COLUMNS.items():
            assert expected_columns == {
                column["name"] for column in inspector.get_columns(table)
            }
        for table, expected_columns in BACKTEST_TABLE_COLUMNS.items():
            assert expected_columns == {
                column["name"] for column in inspector.get_columns(table)
            }
        assert inspector.get_pk_constraint("app_setting")["constrained_columns"] == [
            "key"
        ]
        assert inspector.get_pk_constraint("task_run")["constrained_columns"] == ["id"]
        assert inspector.get_pk_constraint("task_event")["constrained_columns"] == [
            "id"
        ]
        assert inspector.get_pk_constraint("task_worker_heartbeat")[
            "constrained_columns"
        ] == ["worker_id"]
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
        assert {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes("task_worker_heartbeat")
        } == {("ix_task_worker_heartbeat_at", ("heartbeat_at",))}
        assert _trigger_names(engine) == ALL_TRIGGER_NAMES
    finally:
        _dispose(engine)


def test_market_catalog_has_exact_keys_constraints_and_indexes(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-shape.db'}"
    migrate(url)
    engine = create_engine_for_url(url)

    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("market_dataset")["constrained_columns"] == [
            "dataset_version"
        ]
        assert inspector.get_pk_constraint("market_dataset_partition")[
            "constrained_columns"
        ] == ["dataset_version", "partition_manifest_id"]
        assert inspector.get_pk_constraint("market_dataset_timestamp")[
            "constrained_columns"
        ] == ["dataset_version", "ordinal"]
        assert inspector.get_pk_constraint("market_dataset_timestamp_seal")[
            "constrained_columns"
        ] == ["dataset_version"]
        assert inspector.get_pk_constraint("market_routing_manifest")[
            "constrained_columns"
        ] == ["manifest_record_id"]
        assert inspector.get_pk_constraint("market_update_item")[
            "constrained_columns"
        ] == ["task_id", "ordinal"]
        assert inspector.get_pk_constraint("market_update_schedule")[
            "constrained_columns"
        ] == ["id"]
        assert inspector.get_pk_constraint("market_update_occurrence")[
            "constrained_columns"
        ] == ["schedule_id", "local_date"]
        assert inspector.get_pk_constraint("execution_status_dataset")[
            "constrained_columns"
        ] == ["dataset_version"]
        assert inspector.get_pk_constraint("execution_status_routing_manifest")[
            "constrained_columns"
        ] == ["manifest_record_id"]

        assert _unique_signatures(engine, "market_dataset_partition") == {
            ("dataset_version", "partition_year"),
            ("relative_path",),
        }
        assert _unique_signatures(engine, "market_dataset") == {
            ("dataset_version", "symbol"),
        }
        assert _unique_signatures(engine, "market_dataset_timestamp") == {
            ("dataset_version", "timestamp"),
        }
        assert _unique_signatures(engine, "market_update_item") == {
            ("task_id", "symbol"),
        }
        assert _unique_signatures(engine, "market_routing_manifest") == {
            ("manifest_record_id", "dataset_version", "symbol"),
        }
        assert _unique_signatures(engine, "market_update_occurrence") == {
            ("task_id",),
        }

        assert _index_signatures(engine, "market_dataset") == {
            (
                "ix_market_dataset_exact_query",
                ("symbol", "period", "adjustment", "query_start", "query_end"),
                False,
            )
        }
        assert _index_signatures(engine, "market_routing_manifest") == {
            (
                "ix_market_routing_manifest_dataset_fetched_at",
                ("dataset_version", "fetched_at"),
                False,
            ),
            ("ix_market_routing_manifest_route_version", ("route_version",), False),
        }
        assert _index_signatures(engine, "market_dataset_timestamp") == {
            (
                "ix_market_dataset_timestamp_lookup",
                ("dataset_version", "timestamp"),
                False,
            )
        }
        assert _index_signatures(engine, "market_update_schedule") == {
            (
                "ix_market_update_schedule_due",
                ("enabled", "local_time", "last_enqueued_local_date"),
                False,
            )
        }
        assert _index_signatures(engine, "execution_status_dataset") == {
            (
                "ix_execution_status_dataset_exact_query",
                ("symbol", "exchange", "period", "query_start", "query_end"),
                False,
            )
        }

        assert _check_names(engine, "market_dataset") == {
            "ck_market_dataset_row_count_positive"
        }
        assert _check_names(engine, "market_dataset_partition") == {
            "ck_market_dataset_partition_byte_size_positive",
            "ck_market_dataset_partition_row_count_positive",
            "ck_market_dataset_partition_year",
        }
        assert _check_names(engine, "market_dataset_timestamp") == {
            "ck_market_dataset_timestamp_ordinal"
        }
        assert _check_names(engine, "market_dataset_timestamp_seal") == {
            "ck_market_dataset_timestamp_seal_row_count"
        }
        assert _check_names(engine, "market_update_item") == {
            "ck_market_update_item_ordinal",
            "ck_market_update_item_outcome",
            "ck_market_update_item_status",
        }
        assert _check_names(engine, "market_update_schedule") == {
            "ck_market_update_schedule_timezone"
        }
        assert _check_names(engine, "execution_status_dataset") == {
            "ck_execution_status_dataset_period",
            "ck_execution_status_dataset_row_count_positive",
        }

        partition_fks = inspector.get_foreign_keys("market_dataset_partition")
        assert [
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in partition_fks
        ] == [(("dataset_version",), "market_dataset", ("dataset_version",))]

        timestamp_fks = inspector.get_foreign_keys("market_dataset_timestamp")
        assert [
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in timestamp_fks
        ] == [(("dataset_version",), "market_dataset", ("dataset_version",))]

        timestamp_seal_fks = inspector.get_foreign_keys("market_dataset_timestamp_seal")
        assert [
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in timestamp_seal_fks
        ] == [(("dataset_version",), "market_dataset", ("dataset_version",))]

        routing_fks = inspector.get_foreign_keys("market_routing_manifest")
        assert [
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in routing_fks
        ] == [
            (
                ("dataset_version", "symbol"),
                "market_dataset",
                ("dataset_version", "symbol"),
            )
        ]

        item_fks = {
            tuple(fk["constrained_columns"]): (
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys("market_update_item")
        }
        assert item_fks == {
            ("task_id",): ("task_run", ("id",)),
            ("dataset_version",): ("market_dataset", ("dataset_version",)),
            ("manifest_record_id", "dataset_version", "symbol"): (
                "market_routing_manifest",
                ("manifest_record_id", "dataset_version", "symbol"),
            ),
        }

        occurrence_fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspector.get_foreign_keys("market_update_occurrence")
        }
        assert occurrence_fks[("schedule_id",)]["referred_table"] == (
            "market_update_schedule"
        )
        assert occurrence_fks[("task_id",)]["referred_table"] == "task_run"
        assert occurrence_fks[("task_id",)]["options"] == {
            "deferrable": True,
            "initially": "DEFERRED",
        }
    finally:
        _dispose(engine)


def test_backtest_tables_have_keys_checks_indexes_and_foreign_keys(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'backtest-shape.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        inspector = inspect(engine)
        expected_primary_keys = {
            "backtest_run": ["id"],
            "backtest_symbol": ["run_id", "ordinal"],
            "backtest_trade": ["run_id", "symbol", "ordinal"],
            "backtest_order_event": ["run_id", "symbol", "ordinal"],
            "backtest_failure": ["run_id", "symbol", "ordinal"],
            "backtest_log": ["run_id", "ordinal"],
            "backtest_aggregate_metric": ["run_id", "metric_key"],
            "backtest_group_metric": ["run_id", "dimension", "group_key"],
        }
        for table, columns in expected_primary_keys.items():
            assert inspector.get_pk_constraint(table)["constrained_columns"] == columns

        assert _check_names(engine, "backtest_run") == {
            "ck_backtest_run_status",
            "ck_backtest_run_stage",
            "ck_backtest_run_total",
            "ck_backtest_run_counts",
        }
        assert _check_names(engine, "backtest_symbol") == {
            "ck_backtest_symbol_ordinal",
            "ck_backtest_symbol_input_kind",
            "ck_backtest_symbol_status",
        }
        assert _check_names(engine, "backtest_trade") == {"ck_backtest_trade_ordinal"}
        assert _check_names(engine, "backtest_order_event") == {
            "ck_backtest_order_event_ordinal"
        }
        assert _check_names(engine, "backtest_failure") == {
            "ck_backtest_failure_ordinal"
        }
        assert _check_names(engine, "backtest_log") == {
            "ck_backtest_log_ordinal",
            "ck_backtest_log_level",
        }
        assert _check_names(engine, "backtest_group_metric") == {
            "ck_backtest_group_dimension"
        }

        assert _index_signatures(engine, "backtest_run") == {
            ("ix_backtest_run_created", ("created_at", "id"), False),
            ("ix_backtest_run_status", ("status", "updated_at"), False),
        }
        assert _index_signatures(engine, "backtest_symbol") == {
            ("ix_backtest_symbol_status", ("run_id", "status", "ordinal"), False)
        }
        for table in BACKTEST_TABLES - {"backtest_run"}:
            foreign_keys = inspector.get_foreign_keys(table)
            assert foreign_keys, f"{table} must be owned by a backtest parent"
    finally:
        _dispose(engine)


def test_formula_catalog_has_exact_keys_constraints_indexes_and_triggers(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'formula-shape.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("formula")["constrained_columns"] == ["id"]
        assert inspector.get_pk_constraint("formula_version")[
            "constrained_columns"
        ] == ["id"]
        assert inspector.get_pk_constraint("formula_draft")["constrained_columns"] == [
            "formula_id"
        ]
        assert _unique_signatures(engine, "formula_version") == {
            ("formula_id", "id"),
            ("formula_id", "version"),
        }
        assert _index_signatures(engine, "formula_version") == {
            ("ix_formula_version_formula", ("formula_id", "version"), False)
        }
        draft_foreign_keys = {
            (
                tuple(item["constrained_columns"]),
                str(item["referred_table"]),
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys("formula_draft")
        }
        assert draft_foreign_keys == {
            (("formula_id",), "formula", ("id",)),
            (
                ("formula_id", "executable_version_id"),
                "formula_version",
                ("formula_id", "id"),
            ),
        }
        assert FORMULA_TRIGGER_NAMES <= _trigger_names(engine)
        owner_sql = _trigger_sql(engine, "trg_formula_version_owner")
        assert "name = NEW.name" in owner_sql
        assert "latest_version = NEW.version" in owner_sql
    finally:
        _dispose(engine)


def test_instrument_catalog_has_exact_keys_constraints_and_indexes(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'instrument-shape.db'}"
    migrate(url)
    engine = create_engine_for_url(url)

    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("instrument_dataset")[
            "constrained_columns"
        ] == ["dataset_version"]
        assert inspector.get_pk_constraint("instrument_dataset_item")[
            "constrained_columns"
        ] == ["dataset_version", "symbol"]
        assert inspector.get_pk_constraint("instrument_routing_manifest")[
            "constrained_columns"
        ] == ["manifest_record_id"]
        assert _unique_signatures(engine, "instrument_dataset_item") == {
            ("dataset_version", "ordinal"),
        }
        assert _unique_signatures(engine, "instrument_routing_manifest") == {
            ("manifest_record_id", "dataset_version"),
        }
        assert _index_signatures(engine, "instrument_routing_manifest") == {
            (
                "ix_instrument_routing_manifest_current",
                ("data_cutoff", "fetched_at", "manifest_record_id"),
                False,
            )
        }
        assert _check_names(engine, "instrument_dataset") == {
            "ck_instrument_dataset_row_count_bounded",
        }
        assert _check_names(engine, "instrument_dataset_item") == {
            "ck_instrument_dataset_item_name_length",
            "ck_instrument_dataset_item_ordinal",
        }
        assert [
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys("instrument_dataset_item")
        ] == [(("dataset_version",), "instrument_dataset", ("dataset_version",))]
        assert [
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
            )
            for fk in inspector.get_foreign_keys("instrument_routing_manifest")
        ] == [(("dataset_version",), "instrument_dataset", ("dataset_version",))]
    finally:
        _dispose(engine)


def test_pool_tables_have_exact_keys_constraints_indexes_and_foreign_keys(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'pool-shape.db'}"
    migrate(url)
    engine = create_engine_for_url(url)

    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("preset_pool_snapshot")[
            "constrained_columns"
        ] == ["snapshot_id"]
        assert inspector.get_pk_constraint("preset_pool_member")[
            "constrained_columns"
        ] == ["snapshot_id", "ordinal"]
        assert inspector.get_pk_constraint("custom_pool")["constrained_columns"] == [
            "pool_id"
        ]
        assert inspector.get_pk_constraint("custom_pool_member")[
            "constrained_columns"
        ] == ["pool_id", "ordinal"]
        assert _unique_signatures(engine, "preset_pool_snapshot") == {
            ("snapshot_id", "instrument_dataset_version"),
        }
        assert _unique_signatures(engine, "preset_pool_member") == {
            ("snapshot_id", "symbol"),
        }
        assert _unique_signatures(engine, "custom_pool") == {
            ("pool_id", "revision", "instrument_dataset_version"),
        }
        assert _unique_signatures(engine, "custom_pool_member") == {
            ("pool_id", "symbol"),
        }
        assert _index_signatures(engine, "preset_pool_snapshot") == {
            (
                "ix_preset_pool_snapshot_latest",
                ("preset_key", "data_cutoff", "fetched_at", "snapshot_id"),
                False,
            )
        }
        assert _check_names(engine, "preset_pool_snapshot") == {
            "ck_preset_pool_snapshot_category",
            "ck_preset_pool_snapshot_complete",
            "ck_preset_pool_snapshot_logical_id",
            "ck_preset_pool_snapshot_member_count",
        }
        assert _check_names(engine, "preset_pool_member") == {
            "ck_preset_pool_member_ordinal",
        }
        assert _check_names(engine, "custom_pool") == {
            "ck_custom_pool_member_count",
            "ck_custom_pool_member_digest",
            "ck_custom_pool_state_digest",
            "ck_custom_pool_name",
            "ck_custom_pool_revision",
        }
        assert _check_names(engine, "custom_pool_member") == {
            "ck_custom_pool_member_ordinal",
            "ck_custom_pool_member_revision",
        }

        def foreign_key_signatures(
            table: str,
        ) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
            return {
                (
                    tuple(fk["constrained_columns"]),
                    str(fk["referred_table"]),
                    tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(table)
            }

        instrument_pin = (
            ("instrument_manifest_record_id", "instrument_dataset_version"),
            "instrument_routing_manifest",
            ("manifest_record_id", "dataset_version"),
        )
        instrument_member = (
            ("instrument_dataset_version", "symbol"),
            "instrument_dataset_item",
            ("dataset_version", "symbol"),
        )
        assert foreign_key_signatures("preset_pool_snapshot") == {instrument_pin}
        assert foreign_key_signatures("preset_pool_member") == {
            (
                ("snapshot_id", "instrument_dataset_version"),
                "preset_pool_snapshot",
                ("snapshot_id", "instrument_dataset_version"),
            ),
            instrument_member,
        }
        assert foreign_key_signatures("custom_pool") == {instrument_pin}
        assert foreign_key_signatures("custom_pool_member") == {
            (
                ("pool_id", "member_revision", "instrument_dataset_version"),
                "custom_pool",
                ("pool_id", "revision", "instrument_dataset_version"),
            ),
            instrument_member,
        }
        custom_owner_fk = next(
            fk
            for fk in inspector.get_foreign_keys("custom_pool_member")
            if fk["referred_table"] == "custom_pool"
        )
        assert custom_owner_fk["options"] == {"ondelete": "CASCADE"}
    finally:
        _dispose(engine)


def test_market_catalog_deferred_task_fk_allows_atomic_occurrence_enqueue(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'deferred-occurrence.db'}"
    migrate(url)
    engine = create_engine_for_url(url)

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO market_update_schedule "
                    "(id, enabled, timezone, local_time, payload_json) "
                    "VALUES ('schedule-1', 1, 'Asia/Shanghai', '18:00:00', '{}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO market_update_occurrence "
                    "(schedule_id, local_date, task_id) "
                    "VALUES ('schedule-1', '2026-07-06', 'task-1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO task_run (id, kind, status) "
                    "VALUES ('task-1', 'market.update', 'running')"
                )
            )

        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT task_id FROM market_update_occurrence "
                        "WHERE schedule_id = 'schedule-1' AND local_date = '2026-07-06'"
                    )
                ).scalar_one()
                == "task-1"
            )
    finally:
        _dispose(engine)


def test_market_update_item_rejects_cross_linked_manifest_dataset_and_symbol(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'update-item-provenance.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    dataset_a = "sha256:" + "a" * 64
    dataset_b = "sha256:" + "b" * 64
    manifest_a = "sha256:" + "c" * 64
    route_a = "sha256:" + "d" * 64

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_run (id, kind, status) "
                    "VALUES ('task-1', 'market.update', 'running')"
                )
            )
            for version, symbol in (
                (dataset_a, "600000.SH"),
                (dataset_b, "000001.SZ"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO market_dataset "
                        "(dataset_version, source, symbol, period, adjustment, "
                        "query_start, query_end, data_cutoff, row_count) "
                        "VALUES (:version, 'tushare', :symbol, '1d', 'none', "
                        "'2026-01-01', '2026-02-01', '2026-01-31', 1)"
                    ),
                    {"version": version, "symbol": symbol},
                )
            connection.execute(
                text(
                    "INSERT INTO market_routing_manifest "
                    "(manifest_record_id, dataset_version, symbol, route_version, "
                    "manifest_json, fetched_at) "
                    "VALUES (:manifest, :dataset, '600000.SH', :route, "
                    "'{}', '2026-02-01')"
                ),
                {"manifest": manifest_a, "dataset": dataset_a, "route": route_a},
            )

        with pytest.raises(DBAPIError, match="FOREIGN KEY constraint failed"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO market_update_item "
                        "(task_id, ordinal, symbol, status, manifest_record_id, "
                        "dataset_version) "
                        "VALUES ('task-1', 0, '000001.SZ', 'succeeded', "
                        ":manifest, :dataset)"
                    ),
                    {"manifest": manifest_a, "dataset": dataset_b},
                )
    finally:
        _dispose(engine)


@pytest.mark.parametrize(
    ("task_id", "kind", "status", "cancel_requested"),
    [
        ("missing-task", None, None, False),
        ("wrong-kind", "demo.double", "running", False),
        ("queued-update", "market.update", "queued", False),
        ("succeeded-update", "market.update", "succeeded", False),
        ("failed-update", "market.update", "failed", False),
        ("cancelled-update", "market.update", "cancelled", True),
    ],
)
def test_market_update_item_insert_requires_running_market_update_owner(
    tmp_path: Path,
    task_id: str,
    kind: str | None,
    status: str | None,
    cancel_requested: bool,
) -> None:
    url = f"sqlite:///{tmp_path / f'item-owner-{task_id}.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        if kind is not None and status is not None:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO task_run "
                        "(id, kind, status, cancel_requested) "
                        "VALUES (:id, :kind, :status, :cancel_requested)"
                    ),
                    {
                        "id": task_id,
                        "kind": kind,
                        "status": status,
                        "cancel_requested": cancel_requested,
                    },
                )

        with pytest.raises(DBAPIError, match="running market update task"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO market_update_item "
                        "(task_id, ordinal, symbol, status, reason) "
                        "VALUES (:task_id, 0, '600000.SH', 'failed', "
                        "'routing:no_provider')"
                    ),
                    {"task_id": task_id},
                )
    finally:
        _dispose(engine)


def test_market_update_item_insert_allows_cancel_requested_running_owner(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'item-owner-cancel-requested.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_run "
                    "(id, kind, status, cancel_requested) "
                    "VALUES ('running-cancel-requested', 'market.update', "
                    "'running', 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO market_update_item "
                    "(task_id, ordinal, symbol, status, reason) "
                    "VALUES ('running-cancel-requested', 0, '600000.SH', "
                    "'cancelled', 'cancel_requested')"
                )
            )

        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT status FROM market_update_item "
                        "WHERE task_id = 'running-cancel-requested'"
                    )
                ).scalar_one()
                == "cancelled"
            )
    finally:
        _dispose(engine)


@pytest.mark.parametrize(
    "replacement_ordinal",
    [0, 1],
    ids=("same-task-ordinal", "same-task-symbol"),
)
def test_market_update_item_insert_or_replace_cannot_overwrite_immutable_row(
    tmp_path: Path,
    replacement_ordinal: int,
) -> None:
    url = f"sqlite:///{tmp_path / 'item-replace.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    original: tuple[object, ...]
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_run (id, kind, status) "
                    "VALUES ('replace-task', 'market.update', 'running')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO market_update_item "
                    "(task_id, ordinal, symbol, status, reason) "
                    "VALUES ('replace-task', 0, '600000.SH', 'failed', "
                    "'routing:no_provider')"
                )
            )
            original = tuple(
                connection.execute(
                    text(
                        "SELECT task_id, ordinal, symbol, status, "
                        "manifest_record_id, dataset_version, reason, created_at, "
                        "hex(CAST(task_id AS BLOB)), "
                        "hex(CAST(symbol AS BLOB)), "
                        "hex(CAST(status AS BLOB)), "
                        "hex(CAST(reason AS BLOB)) "
                        "FROM market_update_item WHERE task_id = 'replace-task'"
                    )
                ).one()
            )

        with pytest.raises(DBAPIError, match="market_update_item rows are immutable"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT OR REPLACE INTO market_update_item "
                        "(task_id, ordinal, symbol, status, reason) "
                        "VALUES ('replace-task', :ordinal, '600000.SH', "
                        "'cancelled', 'cancel_requested')"
                    ),
                    {"ordinal": replacement_ordinal},
                )

        with engine.connect() as connection:
            persisted = tuple(
                connection.execute(
                    text(
                        "SELECT task_id, ordinal, symbol, status, "
                        "manifest_record_id, dataset_version, reason, created_at, "
                        "hex(CAST(task_id AS BLOB)), "
                        "hex(CAST(symbol AS BLOB)), "
                        "hex(CAST(status AS BLOB)), "
                        "hex(CAST(reason AS BLOB)) "
                        "FROM market_update_item WHERE task_id = 'replace-task'"
                    )
                ).one()
            )
            assert persisted == original
    finally:
        _dispose(engine)


def test_market_update_item_insert_guard_has_both_immutable_keys(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'item-duplicate-trigger.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        trigger_sql = _trigger_sql(engine, UPDATE_ITEM_DUPLICATE_TRIGGER)
        assert "BEFORE INSERT ON market_update_item" in trigger_sql
        assert "EXISTS" in trigger_sql
        assert "task_id = NEW.task_id" in trigger_sql
        assert "ordinal = NEW.ordinal" in trigger_sql
        assert "symbol = NEW.symbol" in trigger_sql
        assert len(_trigger_names(engine)) == len(ALL_TRIGGER_NAMES)
    finally:
        _dispose(engine)


def test_market_routing_manifest_rejects_dataset_symbol_mismatch(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'routing-manifest-provenance.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    dataset = "sha256:" + "a" * 64
    manifest = "sha256:" + "b" * 64
    route = "sha256:" + "c" * 64

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO market_dataset "
                    "(dataset_version, source, symbol, period, adjustment, "
                    "query_start, query_end, data_cutoff, row_count) "
                    "VALUES (:dataset, 'tushare', '600000.SH', '1d', 'none', "
                    "'2026-01-01', '2026-02-01', '2026-01-31', 1)"
                ),
                {"dataset": dataset},
            )

        with pytest.raises(DBAPIError, match="FOREIGN KEY constraint failed"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO market_routing_manifest "
                        "(manifest_record_id, dataset_version, symbol, route_version, "
                        "manifest_json, fetched_at) "
                        "VALUES (:manifest, :dataset, '000001.SZ', :route, "
                        "'{}', '2026-02-01')"
                    ),
                    {"manifest": manifest, "dataset": dataset, "route": route},
                )
    finally:
        _dispose(engine)


def test_existing_0001_database_upgrades_to_task_observability(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-0001.db"
    _create_legacy_0001_database(database_path)
    url = f"sqlite:///{database_path}"
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == "0001_core_tables"
        assert set(inspect(engine).get_table_names()) >= {
            "alembic_version",
            *LEGACY_CORE_TABLES,
        }
        assert "task_event" not in inspect(engine).get_table_names()
    finally:
        _dispose(engine)

    migrate(url)

    engine = create_engine_for_url(url)
    repository = TaskRepository(engine)
    try:
        assert _current_revision(engine) == HEAD_REVISION
        assert "task_event" in inspect(engine).get_table_names()
        task = repository.create("upgrade.check", {"source": "legacy-0001"})
        assert [event.event_name for event in repository.list_events(task.id)] == [
            "task.created"
        ]
    finally:
        repository.close()


def test_existing_0002_database_upgrades_to_market_catalog(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'existing-0002.db'}"
    migrate(url, "0002_task_observability")
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == "0002_task_observability"
        assert CATALOG_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        _dispose(engine)

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == HEAD_REVISION
        assert CATALOG_TABLES <= set(inspect(engine).get_table_names())
        assert FORMULA_TABLES <= set(inspect(engine).get_table_names())
        assert _trigger_names(engine) == ALL_TRIGGER_NAMES
    finally:
        _dispose(engine)


def test_formula_revision_downgrades_to_0004_and_reupgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'formula-roundtrip.db'}"
    migrate(url)

    downgrade(url, "0004_instruments_and_pools")
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == "0004_instruments_and_pools"
        assert FORMULA_TABLES.isdisjoint(inspect(engine).get_table_names())
        assert FORMULA_TRIGGER_NAMES.isdisjoint(_trigger_names(engine))
        assert _trigger_names(engine) == MARKET_TRIGGER_NAMES
    finally:
        _dispose(engine)

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == HEAD_REVISION
        assert FORMULA_TABLES <= set(inspect(engine).get_table_names())
        assert _trigger_names(engine) == ALL_TRIGGER_NAMES
    finally:
        _dispose(engine)


def test_instrument_revision_downgrades_to_0003_and_reupgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'instrument-roundtrip.db'}"
    migrate(url)

    downgrade(url, "0003_market_catalog")
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == "0003_market_catalog"
        assert INSTRUMENT_TABLES.isdisjoint(inspect(engine).get_table_names())
        assert {
            name for name in MARKET_TRIGGER_NAMES if name.startswith("trg_instrument_")
        }.isdisjoint(_trigger_names(engine))
        assert "market_dataset" in inspect(engine).get_table_names()
    finally:
        _dispose(engine)

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == HEAD_REVISION
        assert INSTRUMENT_TABLES <= set(inspect(engine).get_table_names())
        assert _trigger_names(engine) == ALL_TRIGGER_NAMES
    finally:
        _dispose(engine)


def test_market_catalog_revision_downgrades_and_reupgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-roundtrip.db'}"
    migrate(url)

    downgrade(url, "0002_task_observability")
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == "0002_task_observability"
        assert CATALOG_TABLES.isdisjoint(inspect(engine).get_table_names())
        assert MARKET_TRIGGER_NAMES.isdisjoint(_trigger_names(engine))
    finally:
        _dispose(engine)

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == HEAD_REVISION
        assert CATALOG_TABLES <= set(inspect(engine).get_table_names())
        assert _trigger_names(engine) == ALL_TRIGGER_NAMES
    finally:
        _dispose(engine)


def test_observability_revision_downgrades_and_reupgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'observability-roundtrip.db'}"
    migrate(url)

    downgrade(url, "0001_core_tables")
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == "0001_core_tables"
        assert LEGACY_CORE_TABLES <= set(inspect(engine).get_table_names())
        assert "task_event" not in inspect(engine).get_table_names()
    finally:
        _dispose(engine)

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == HEAD_REVISION
        assert CORE_TABLES <= set(inspect(engine).get_table_names())
    finally:
        _dispose(engine)


def test_worker_heartbeat_revision_downgrades_and_reupgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'worker-heartbeat-roundtrip.db'}"
    migrate(url)

    downgrade(url, "0010_parent_active_retry")
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == "0010_parent_active_retry"
        assert WORKER_HEARTBEAT_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        _dispose(engine)

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert _current_revision(engine) == HEAD_REVISION
        assert WORKER_HEARTBEAT_TABLES <= set(inspect(engine).get_table_names())
    finally:
        _dispose(engine)


def test_market_catalog_rows_are_append_only_and_schedule_remains_mutable(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-immutability.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    digest_c = "sha256:" + "c" * 64
    digest_d = "sha256:" + "d" * 64

    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO task_run (id, kind, status) "
                    "VALUES ('task-1', 'market.update', 'running')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO market_dataset "
                    "(dataset_version, source, symbol, period, adjustment, "
                    "query_start, query_end, data_cutoff, row_count) "
                    "VALUES (:dataset_version, 'tushare', '600000.SH', '1d', "
                    "'none', '2026-01-01', '2026-02-01', '2026-01-31', 1)"
                ),
                {"dataset_version": digest_a},
            )
            connection.execute(
                text(
                    "INSERT INTO market_dataset_partition "
                    "(partition_manifest_id, dataset_version, partition_year, "
                    "relative_path, row_count, byte_size, physical_sha256) "
                    "VALUES (:partition, :dataset, 2026, "
                    "'year=2026/dataset=abc/part-00000.parquet', 1, 100, :physical)"
                ),
                {"partition": digest_b, "dataset": digest_a, "physical": digest_c},
            )
            connection.execute(
                text(
                    "INSERT INTO market_routing_manifest "
                    "(manifest_record_id, dataset_version, symbol, route_version, "
                    "manifest_json, fetched_at) "
                    "VALUES (:manifest, :dataset, '600000.SH', :route, "
                    "'{}', '2026-02-01')"
                ),
                {"manifest": digest_d, "dataset": digest_a, "route": digest_c},
            )
            connection.execute(
                text(
                    "INSERT INTO market_update_item "
                    "(task_id, ordinal, symbol, status, manifest_record_id, "
                    "dataset_version) "
                    "VALUES ('task-1', 0, '600000.SH', 'succeeded', "
                    ":manifest, :dataset)"
                ),
                {"manifest": digest_d, "dataset": digest_a},
            )
            connection.execute(
                text(
                    "INSERT INTO market_update_schedule "
                    "(id, enabled, timezone, local_time, payload_json) "
                    "VALUES ('schedule-1', 1, 'Asia/Shanghai', '18:00:00', '{}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO market_update_occurrence "
                    "(schedule_id, local_date, task_id) "
                    "VALUES ('schedule-1', '2026-07-06', 'task-1')"
                )
            )

        mutations = (
            (
                "market_dataset",
                "source = 'akshare'",
                "dataset_version = :identity",
                digest_a,
            ),
            (
                "market_dataset_partition",
                "byte_size = 101",
                "partition_manifest_id = :identity",
                digest_b,
            ),
            (
                "market_routing_manifest",
                "fetched_at = '2026-02-02'",
                "manifest_record_id = :identity",
                digest_d,
            ),
            (
                "market_update_item",
                "status = 'failed'",
                "task_id = 'task-1' AND ordinal = 0",
                "unused",
            ),
            (
                "market_update_occurrence",
                "local_date = '2026-07-07'",
                "schedule_id = 'schedule-1' AND local_date = '2026-07-06'",
                "unused",
            ),
        )
        for table, assignment, predicate, identity in mutations:
            with pytest.raises(DBAPIError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        text(f"UPDATE {table} SET {assignment} WHERE {predicate}"),
                        {"identity": identity},
                    )
            with pytest.raises(DBAPIError, match="immutable"):
                with engine.begin() as connection:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE {predicate}"),
                        {"identity": identity},
                    )

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE market_update_schedule SET enabled = 0 "
                    "WHERE id = 'schedule-1'"
                )
            )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT enabled FROM market_update_schedule WHERE id = 'schedule-1'"
                    )
                ).scalar_one()
                == 0
            )
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


def test_market_lake_can_be_the_first_import_in_a_fresh_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import stock_desk.market.lake; "
                "from stock_desk.storage.metadata import Base; "
                "assert {'formula','formula_draft','formula_version'} "
                "<= set(Base.metadata.tables)"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


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
