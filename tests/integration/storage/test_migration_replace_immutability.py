from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import Base


IMMUTABLE_TABLES = (
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
DATASET_A = "sha256:" + "a" * 64
DATASET_B = "sha256:" + "b" * 64
PARTITION_A = "sha256:" + "c" * 64
PARTITION_B = "sha256:" + "d" * 64
MANIFEST_A = "sha256:" + "e" * 64
ROUTE_A = "sha256:" + "f" * 64
PHYSICAL_A = "sha256:" + "1" * 64
SNAPSHOT_A = "sha256:" + "2" * 64


def _engine(tmp_path: Path, name: str) -> Engine:
    url = f"sqlite:///{tmp_path / name}"
    migrate(url)
    return create_engine_for_url(url)


def _insert_dataset(
    connection: Connection,
    dataset_version: str,
    symbol: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO market_dataset "
            "(dataset_version, source, symbol, period, adjustment, query_start, "
            "query_end, data_cutoff, row_count) "
            "VALUES (:dataset, 'tushare', :symbol, '1d', 'none', "
            "'2025-01-01', '2026-01-01', '2025-12-31', 1)"
        ),
        {"dataset": dataset_version, "symbol": symbol},
    )


def _insert_instrument_dataset(
    connection: Connection,
    dataset_version: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO instrument_dataset "
            "(dataset_version, source, data_cutoff, row_count) "
            "VALUES (:dataset, 'tushare', '2026-07-06 08:00:00', 1)"
        ),
        {"dataset": dataset_version},
    )


def _insert_instrument_catalog(connection: Connection) -> None:
    _insert_instrument_dataset(connection, DATASET_A)
    connection.execute(
        text(
            "INSERT INTO instrument_dataset_item "
            "(dataset_version, symbol, ordinal, exchange, name, "
            "instrument_kind, listing_status) "
            "VALUES (:dataset, '600000.SH', 0, 'SH', '浦发银行', "
            "'stock', 'listed')"
        ),
        {"dataset": DATASET_A},
    )
    connection.execute(
        text(
            "INSERT INTO instrument_routing_manifest "
            "(manifest_record_id, dataset_version, route_version, "
            "manifest_json, fetched_at, data_cutoff) "
            "VALUES (:manifest, :dataset, :route, '{}', "
            "'2026-07-06 09:00:00', '2026-07-06 08:00:00')"
        ),
        {"manifest": MANIFEST_A, "dataset": DATASET_A, "route": ROUTE_A},
    )


def _insert_preset_snapshot(connection: Connection) -> None:
    _insert_instrument_catalog(connection)
    connection.execute(
        text(
            "INSERT INTO preset_pool_snapshot "
            "(snapshot_id, pool_id, preset_key, category, display_name, source, "
            "composition_dataset_version, composition_route_version, fetched_at, "
            "data_cutoff, complete, instrument_manifest_record_id, "
            "instrument_dataset_version, member_count) "
            "VALUES (:snapshot, 'preset:test', 'test', 'index', 'Test', "
            "'tushare', :composition_dataset, :route, '2026-07-06 09:00:00', "
            "'2026-07-06 08:00:00', 1, :manifest, :instrument_dataset, 1)"
        ),
        {
            "snapshot": SNAPSHOT_A,
            "composition_dataset": DATASET_B,
            "route": ROUTE_A,
            "manifest": MANIFEST_A,
            "instrument_dataset": DATASET_A,
        },
    )


def _insert_task(connection: Connection, task_id: str) -> None:
    connection.execute(
        text(
            "INSERT INTO task_run (id, kind, status) "
            "VALUES (:task_id, 'market.update', 'running')"
        ),
        {"task_id": task_id},
    )


def _insert_schedule(connection: Connection, schedule_id: str) -> None:
    connection.execute(
        text(
            "INSERT INTO market_update_schedule "
            "(id, enabled, timezone, local_time, payload_json) "
            "VALUES (:schedule_id, 1, 'Asia/Shanghai', '18:00:00', '{}')"
        ),
        {"schedule_id": schedule_id},
    )


def _insert_partition(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO market_dataset_partition "
            "(dataset_version, partition_manifest_id, partition_year, "
            "relative_path, row_count, byte_size, physical_sha256) "
            "VALUES (:dataset, :partition, 2025, 'year=2025/original.parquet', "
            "1, 100, :physical)"
        ),
        {
            "dataset": DATASET_A,
            "partition": PARTITION_A,
            "physical": PHYSICAL_A,
        },
    )


def _prepare_replacement_case(
    connection: Connection,
    case: str,
) -> tuple[str, str, dict[str, Any]]:
    if case == "preset-snapshot-primary":
        _insert_preset_snapshot(connection)
        return (
            "preset_pool_snapshot",
            "INSERT OR REPLACE INTO preset_pool_snapshot "
            "(snapshot_id, pool_id, preset_key, category, display_name, source, "
            "composition_dataset_version, composition_route_version, fetched_at, "
            "data_cutoff, complete, instrument_manifest_record_id, "
            "instrument_dataset_version, member_count) "
            "VALUES (:snapshot, 'preset:test', 'test', 'industry', 'Changed', "
            "'akshare', :composition_dataset, :route, '2026-07-07 09:00:00', "
            "'2026-07-07 08:00:00', 1, :manifest, :instrument_dataset, 1)",
            {
                "snapshot": SNAPSHOT_A,
                "composition_dataset": DATASET_B,
                "route": ROUTE_A,
                "manifest": MANIFEST_A,
                "instrument_dataset": DATASET_A,
            },
        )

    if case.startswith("preset-member-"):
        _insert_preset_snapshot(connection)
        connection.execute(
            text(
                "INSERT INTO preset_pool_member "
                "(snapshot_id, ordinal, instrument_dataset_version, symbol) "
                "VALUES (:snapshot, 0, :dataset, '600000.SH')"
            ),
            {"snapshot": SNAPSHOT_A, "dataset": DATASET_A},
        )
        if case == "preset-member-primary":
            connection.execute(
                text(
                    "INSERT INTO instrument_dataset_item "
                    "(dataset_version, symbol, ordinal, exchange, name, "
                    "instrument_kind, listing_status) "
                    "VALUES (:dataset, '000001.SZ', 1, 'SZ', '平安银行', "
                    "'stock', 'listed')"
                ),
                {"dataset": DATASET_A},
            )
        replacement_ordinal = 0 if case == "preset-member-primary" else 1
        replacement_symbol = (
            "000001.SZ" if case == "preset-member-primary" else "600000.SH"
        )
        return (
            "preset_pool_member",
            "INSERT OR REPLACE INTO preset_pool_member "
            "(snapshot_id, ordinal, instrument_dataset_version, symbol) "
            "VALUES (:snapshot, :ordinal, :dataset, :symbol)",
            {
                "snapshot": SNAPSHOT_A,
                "ordinal": replacement_ordinal,
                "dataset": DATASET_A,
                "symbol": replacement_symbol,
            },
        )

    if case == "instrument-dataset-primary":
        _insert_instrument_dataset(connection, DATASET_A)
        return (
            "instrument_dataset",
            "INSERT OR REPLACE INTO instrument_dataset "
            "(dataset_version, source, data_cutoff, row_count) "
            "VALUES (:dataset, 'akshare', '2026-07-07 08:00:00', 2)",
            {"dataset": DATASET_A},
        )

    if case.startswith("instrument-item-"):
        _insert_instrument_dataset(connection, DATASET_A)
        connection.execute(
            text(
                "INSERT INTO instrument_dataset_item "
                "(dataset_version, symbol, ordinal, exchange, name, "
                "instrument_kind, listing_status) "
                "VALUES (:dataset, '600000.SH', 0, 'SH', '浦发银行', "
                "'stock', 'listed')"
            ),
            {"dataset": DATASET_A},
        )
        replacement_symbol = (
            "600000.SH" if case == "instrument-item-primary" else "000001.SZ"
        )
        replacement_ordinal = 1 if case == "instrument-item-primary" else 0
        return (
            "instrument_dataset_item",
            "INSERT OR REPLACE INTO instrument_dataset_item "
            "(dataset_version, symbol, ordinal, exchange, name, "
            "instrument_kind, listing_status) "
            "VALUES (:dataset, :symbol, :ordinal, 'SZ', 'changed', "
            "'index', 'unknown')",
            {
                "dataset": DATASET_A,
                "symbol": replacement_symbol,
                "ordinal": replacement_ordinal,
            },
        )

    if case == "instrument-routing-primary":
        _insert_instrument_dataset(connection, DATASET_A)
        connection.execute(
            text(
                "INSERT INTO instrument_routing_manifest "
                "(manifest_record_id, dataset_version, route_version, "
                "manifest_json, fetched_at, data_cutoff) "
                "VALUES (:manifest, :dataset, :route, '{}', "
                "'2026-07-06 09:00:00', '2026-07-06 08:00:00')"
            ),
            {"manifest": MANIFEST_A, "dataset": DATASET_A, "route": ROUTE_A},
        )
        return (
            "instrument_routing_manifest",
            "INSERT OR REPLACE INTO instrument_routing_manifest "
            "(manifest_record_id, dataset_version, route_version, "
            "manifest_json, fetched_at, data_cutoff) "
            "VALUES (:manifest, :dataset, :route, :manifest_json, "
            "'2026-07-07 09:00:00', '2026-07-07 08:00:00')",
            {
                "manifest": MANIFEST_A,
                "dataset": DATASET_A,
                "route": ROUTE_A,
                "manifest_json": '{"changed":true}',
            },
        )

    if case == "dataset-primary":
        _insert_dataset(connection, DATASET_A, "600000.SH")
        return (
            "market_dataset",
            "INSERT OR REPLACE INTO market_dataset "
            "(dataset_version, source, symbol, period, adjustment, query_start, "
            "query_end, data_cutoff, row_count) "
            "VALUES (:dataset, 'akshare', '000001.SZ', '1m', 'qfq', "
            "'2026-01-01', '2026-02-01', '2026-01-31', 2)",
            {"dataset": DATASET_A},
        )

    if case.startswith("partition-"):
        _insert_dataset(connection, DATASET_A, "600000.SH")
        if case == "partition-relative-path":
            _insert_dataset(connection, DATASET_B, "000001.SZ")
        _insert_partition(connection)
        if case == "partition-primary":
            return (
                "market_dataset_partition",
                "INSERT OR REPLACE INTO market_dataset_partition "
                "(dataset_version, partition_manifest_id, partition_year, "
                "relative_path, row_count, byte_size, physical_sha256) "
                "VALUES (:dataset, :partition, 2026, 'year=2026/changed.parquet', "
                "2, 200, :physical)",
                {
                    "dataset": DATASET_A,
                    "partition": PARTITION_A,
                    "physical": PHYSICAL_A,
                },
            )
        if case == "partition-dataset-year":
            return (
                "market_dataset_partition",
                "INSERT OR REPLACE INTO market_dataset_partition "
                "(dataset_version, partition_manifest_id, partition_year, "
                "relative_path, row_count, byte_size, physical_sha256) "
                "VALUES (:dataset, :partition, 2025, 'year=2025/changed.parquet', "
                "2, 200, :physical)",
                {
                    "dataset": DATASET_A,
                    "partition": PARTITION_B,
                    "physical": PHYSICAL_A,
                },
            )
        return (
            "market_dataset_partition",
            "INSERT OR REPLACE INTO market_dataset_partition "
            "(dataset_version, partition_manifest_id, partition_year, "
            "relative_path, row_count, byte_size, physical_sha256) "
            "VALUES (:dataset, :partition, 2026, 'year=2025/original.parquet', "
            "2, 200, :physical)",
            {
                "dataset": DATASET_B,
                "partition": PARTITION_B,
                "physical": PHYSICAL_A,
            },
        )

    if case == "routing-primary":
        _insert_dataset(connection, DATASET_A, "600000.SH")
        connection.execute(
            text(
                "INSERT INTO market_routing_manifest "
                "(manifest_record_id, dataset_version, symbol, route_version, "
                "manifest_json, fetched_at) "
                "VALUES (:manifest, :dataset, '600000.SH', :route, '{}', "
                "'2026-01-01')"
            ),
            {"manifest": MANIFEST_A, "dataset": DATASET_A, "route": ROUTE_A},
        )
        return (
            "market_routing_manifest",
            "INSERT OR REPLACE INTO market_routing_manifest "
            "(manifest_record_id, dataset_version, symbol, route_version, "
            "manifest_json, fetched_at) "
            "VALUES (:manifest, :dataset, '600000.SH', :route, :manifest_json, "
            "'2026-02-01')",
            {
                "manifest": MANIFEST_A,
                "dataset": DATASET_A,
                "route": ROUTE_A,
                "manifest_json": '{"changed":true}',
            },
        )

    if case.startswith("item-"):
        _insert_task(connection, "item-task")
        connection.execute(
            text(
                "INSERT INTO market_update_item "
                "(task_id, ordinal, symbol, status, reason) "
                "VALUES ('item-task', 0, '600000.SH', 'failed', "
                "'routing:no_provider')"
            )
        )
        replacement_ordinal = 0 if case == "item-primary" else 1
        replacement_symbol = "000001.SZ" if case == "item-primary" else "600000.SH"
        return (
            "market_update_item",
            "INSERT OR REPLACE INTO market_update_item "
            "(task_id, ordinal, symbol, status, reason) "
            "VALUES ('item-task', :ordinal, :symbol, 'cancelled', "
            "'cancel_requested')",
            {"ordinal": replacement_ordinal, "symbol": replacement_symbol},
        )

    _insert_schedule(connection, "schedule-1")
    _insert_task(connection, "occurrence-task-1")
    connection.execute(
        text(
            "INSERT INTO market_update_occurrence "
            "(schedule_id, local_date, task_id) "
            "VALUES ('schedule-1', '2026-07-06', 'occurrence-task-1')"
        )
    )
    if case == "occurrence-primary":
        _insert_task(connection, "occurrence-task-2")
        return (
            "market_update_occurrence",
            "INSERT OR REPLACE INTO market_update_occurrence "
            "(schedule_id, local_date, task_id) "
            "VALUES ('schedule-1', '2026-07-06', 'occurrence-task-2')",
            {},
        )
    _insert_schedule(connection, "schedule-2")
    return (
        "market_update_occurrence",
        "INSERT OR REPLACE INTO market_update_occurrence "
        "(schedule_id, local_date, task_id) "
        "VALUES ('schedule-2', '2026-07-07', 'occurrence-task-1')",
        {},
    )


def _snapshot_table(connection: Connection, table: str) -> tuple[object, ...]:
    columns = tuple(
        str(row[1])
        for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
    )
    expressions = [f'"{column}"' for column in columns]
    expressions.extend(f'hex(CAST("{column}" AS BLOB))' for column in columns)
    row = connection.exec_driver_sql(
        f'SELECT {", ".join(expressions)} FROM "{table}"'
    ).one()
    return tuple(row)


@pytest.mark.parametrize(
    "case",
    [
        "preset-snapshot-primary",
        "preset-member-primary",
        "preset-member-symbol",
        "instrument-dataset-primary",
        "instrument-item-primary",
        "instrument-item-ordinal",
        "instrument-routing-primary",
        "dataset-primary",
        "partition-primary",
        "partition-dataset-year",
        "partition-relative-path",
        "routing-primary",
        "item-primary",
        "item-task-symbol",
        "occurrence-primary",
        "occurrence-task",
    ],
)
def test_insert_or_replace_rejects_every_immutable_key_and_preserves_bytes(
    tmp_path: Path,
    case: str,
) -> None:
    engine = _engine(tmp_path, f"replace-{case}.db")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA recursive_triggers = OFF")
            table, replacement_sql, params = _prepare_replacement_case(connection, case)
            original = _snapshot_table(connection, table)

        with pytest.raises(DBAPIError, match="immutable"):
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA recursive_triggers = OFF")
                connection.execute(text(replacement_sql), params)

        with engine.connect() as connection:
            assert _snapshot_table(connection, table) == original
    finally:
        engine.dispose()


def test_update_item_hidden_rowid_cannot_replace_an_unrelated_immutable_row(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "replace-hidden-rowid.db")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA recursive_triggers = OFF")
            _insert_task(connection, "rowid-old-task")
            _insert_task(connection, "rowid-new-task")
            connection.execute(
                text(
                    "INSERT INTO market_update_item "
                    "(task_id, ordinal, symbol, status, reason) "
                    "VALUES ('rowid-old-task', 0, '600000.SH', 'failed', "
                    "'routing:no_provider')"
                )
            )
            original = _snapshot_table(connection, "market_update_item")

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA recursive_triggers = OFF")
                connection.execute(
                    text(
                        "INSERT OR REPLACE INTO market_update_item "
                        "(rowid, task_id, ordinal, symbol, status, reason) "
                        "VALUES (1, 'rowid-new-task', 1, '000001.SZ', 'failed', "
                        "'routing:no_provider')"
                    )
                )

        with engine.connect() as connection:
            assert _snapshot_table(connection, "market_update_item") == original
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", IMMUTABLE_TABLES)
def test_immutable_tables_are_without_rowid_in_migration_and_orm(
    tmp_path: Path,
    table: str,
) -> None:
    engine = _engine(tmp_path, f"without-rowid-{table}.db")
    try:
        with engine.connect() as connection:
            table_sql = str(
                connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = :table"
                    ),
                    {"table": table},
                ).scalar_one()
            )
            assert "WITHOUT ROWID" in table_sql.upper()
            with pytest.raises(DBAPIError, match="no such column: rowid"):
                connection.exec_driver_sql(f'SELECT rowid FROM "{table}"').all()
        assert (
            Base.metadata.tables[table].dialect_options["sqlite"]["with_rowid"] is False
        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("table", "fragments"),
    [
        ("instrument_dataset", ("dataset_version = NEW.dataset_version",)),
        (
            "instrument_dataset_item",
            (
                "dataset_version = NEW.dataset_version",
                "symbol = NEW.symbol",
                "ordinal = NEW.ordinal",
            ),
        ),
        (
            "instrument_routing_manifest",
            (
                "manifest_record_id = NEW.manifest_record_id",
                "dataset_version = NEW.dataset_version",
            ),
        ),
        (
            "preset_pool_snapshot",
            (
                "snapshot_id = NEW.snapshot_id",
                "instrument_dataset_version = NEW.instrument_dataset_version",
            ),
        ),
        (
            "preset_pool_member",
            (
                "snapshot_id = NEW.snapshot_id",
                "ordinal = NEW.ordinal",
                "symbol = NEW.symbol",
            ),
        ),
        ("market_dataset", ("dataset_version = NEW.dataset_version",)),
        (
            "market_dataset_partition",
            (
                "dataset_version = NEW.dataset_version",
                "partition_manifest_id = NEW.partition_manifest_id",
                "partition_year = NEW.partition_year",
                "relative_path = NEW.relative_path",
            ),
        ),
        (
            "market_routing_manifest",
            ("manifest_record_id = NEW.manifest_record_id",),
        ),
        (
            "market_update_item",
            (
                "task_id = NEW.task_id",
                "ordinal = NEW.ordinal",
                "symbol = NEW.symbol",
            ),
        ),
        (
            "market_update_occurrence",
            (
                "schedule_id = NEW.schedule_id",
                "local_date = NEW.local_date",
                "task_id = NEW.task_id",
            ),
        ),
    ],
)
def test_immutable_insert_guard_contains_every_conflict_key(
    tmp_path: Path,
    table: str,
    fragments: tuple[str, ...],
) -> None:
    engine = _engine(tmp_path, f"guard-shape-{table}.db")
    try:
        with engine.connect() as connection:
            trigger_sql = str(
                connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'trigger' AND name = :name"
                    ),
                    {"name": f"trg_{table}_immutable_insert"},
                ).scalar_one()
            )
        assert f"BEFORE INSERT ON {table}" in trigger_sql
        assert "WHEN EXISTS" in trigger_sql
        assert all(fragment in trigger_sql for fragment in fragments)
    finally:
        engine.dispose()


def test_schedule_remains_a_mutable_rowid_table(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "mutable-schedule.db")
    try:
        with engine.begin() as connection:
            _insert_schedule(connection, "mutable-schedule")
            connection.execute(
                text(
                    "INSERT OR REPLACE INTO market_update_schedule "
                    "(id, enabled, timezone, local_time, payload_json) "
                    "VALUES ('mutable-schedule', 0, 'Asia/Shanghai', "
                    "'19:30:00', :payload_json)"
                ),
                {"payload_json": '{"changed":true}'},
            )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT rowid, enabled, local_time, payload_json "
                    "FROM market_update_schedule WHERE id = 'mutable-schedule'"
                )
            ).one()
            table_sql = str(
                connection.execute(
                    text(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'market_update_schedule'"
                    )
                ).scalar_one()
            )
        assert isinstance(row[0], int)
        assert row[0] >= 1
        assert tuple(row[1:]) == (0, "19:30:00", '{"changed":true}')
        assert "WITHOUT ROWID" not in table_sql.upper()
        assert (
            Base.metadata.tables["market_update_schedule"].dialect_options["sqlite"][
                "with_rowid"
            ]
            is True
        )
    finally:
        engine.dispose()
