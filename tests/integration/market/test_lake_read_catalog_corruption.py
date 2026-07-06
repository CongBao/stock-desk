from __future__ import annotations

from datetime import date
import os
from pathlib import Path

import duckdb
import pytest

from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from tests.integration.market.lake_read_test_helpers import (
    corrupt_catalog,
    open_catalog_engine,
    refresh_partition_file_metadata,
)
from tests.integration.market.lake_test_helpers import routed_daily_bars


@pytest.mark.parametrize(
    "invalid_path",
    [
        "",
        ".",
        "/absolute",
        "../parent",
        "a/./dot",
        "a//double",
        "a/trailing/",
        "a\\backslash",
        "file://lake",
        "a\x00nul",
    ],
)
def test_read_rejects_noncanonical_catalog_relative_path(
    tmp_path: Path,
    invalid_path: str,
) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        corrupt_catalog(
            engine,
            table="market_dataset_partition",
            sql="UPDATE market_dataset_partition SET relative_path = ?",
            parameters=(invalid_path,),
        )

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


@pytest.mark.parametrize(
    ("table", "sql", "parameters"),
    [
        (
            "market_dataset_partition",
            "UPDATE market_dataset_partition SET byte_size = byte_size + 1",
            (),
        ),
        (
            "market_dataset_partition",
            "UPDATE market_dataset_partition SET physical_sha256 = ?",
            (f"sha256:{'0' * 64}",),
        ),
        (
            "market_dataset_partition",
            "UPDATE market_dataset_partition SET row_count = row_count + 1",
            (),
        ),
        (
            "market_dataset_partition",
            "UPDATE market_dataset_partition SET partition_year = partition_year + 1",
            (),
        ),
        (
            "market_dataset_partition",
            "UPDATE market_dataset_partition SET partition_manifest_id = ?",
            (f"sha256:{'1' * 64}",),
        ),
        ("market_dataset", "UPDATE market_dataset SET row_count = row_count + 1", ()),
        ("market_dataset", "UPDATE market_dataset SET source = 'akshare'", ()),
        ("market_dataset", "UPDATE market_dataset SET symbol = '000001.SZ'", ()),
        ("market_dataset", "UPDATE market_dataset SET period = '1w'", ()),
        ("market_dataset", "UPDATE market_dataset SET adjustment = 'hfq'", ()),
        (
            "market_dataset",
            "UPDATE market_dataset SET query_start = '2023-01-01 00:00:00'",
            (),
        ),
        (
            "market_dataset",
            "UPDATE market_dataset SET query_end = '2025-01-01 00:00:00'",
            (),
        ),
        (
            "market_dataset",
            "UPDATE market_dataset SET data_cutoff = '2025-01-01 00:00:00'",
            (),
        ),
        (
            "market_routing_manifest",
            "UPDATE market_routing_manifest SET symbol = '000001.SZ'",
            (),
        ),
        (
            "market_routing_manifest",
            "UPDATE market_routing_manifest SET route_version = ?",
            (f"sha256:{'2' * 64}",),
        ),
        (
            "market_routing_manifest",
            "UPDATE market_routing_manifest SET manifest_json = '{}'",
            (),
        ),
    ],
)
def test_read_rejects_catalog_metadata_mismatch(
    tmp_path: Path,
    table: str,
    sql: str,
    parameters: tuple[object, ...],
) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        corrupt_catalog(engine, table=table, sql=sql, parameters=parameters)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_wraps_invalid_catalog_datetime_as_typed_corruption(
    tmp_path: Path,
) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        corrupt_catalog(
            engine,
            table="market_routing_manifest",
            sql="UPDATE market_routing_manifest SET fetched_at = 1",
        )

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_recomputes_full_manifest_record_identity(tmp_path: Path) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        changed_fetch = "2025-01-02T08:00:00Z"
        corrupt_catalog(
            engine,
            table="market_routing_manifest",
            sql=(
                "UPDATE market_routing_manifest "
                "SET fetched_at = ?, manifest_json = "
                "json_set(manifest_json, '$.upstream_fetched_at', ?)"
            ),
            parameters=(changed_fetch, changed_fetch),
        )

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_rejects_manifest_relinked_to_another_dataset(tmp_path: Path) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        first = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        second = lake.write(routed_daily_bars((date(2024, 2, 2),)))
        corrupt_catalog(
            engine,
            table="market_routing_manifest",
            sql=(
                "UPDATE market_routing_manifest SET dataset_version = ?, symbol = ? "
                "WHERE manifest_record_id = ?"
            ),
            parameters=(
                second.dataset_version,
                "600000.SH",
                first.manifest_record_id,
            ),
        )

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(first.manifest_record_id)


@pytest.mark.parametrize(
    "projection",
    [
        "'000001.SZ'::VARCHAR AS symbol, timestamp, period, adjustment, status, open, high, low, close, volume",
        "symbol, timestamp + INTERVAL '1 year' AS timestamp, period, adjustment, status, open, high, low, close, volume",
        "symbol, timestamp, period, adjustment, 'invalid'::VARCHAR AS status, open, high, low, close, volume",
        "symbol, timestamp, period, adjustment, status, open + 1 AS open, high + 1 AS high, low + 1 AS low, close + 1 AS close, volume",
    ],
)
def test_read_rejects_semantically_mismatched_partition_content(
    tmp_path: Path,
    projection: str,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        target = root / stored.partitions[0].relative_path
        replacement = target.with_name("semantic-mismatch.parquet")
        with duckdb.connect(":memory:") as connection:
            connection.execute(
                f"CREATE TABLE rewritten AS SELECT {projection} "
                "FROM read_parquet(?, hive_partitioning = false)",
                [str(target)],
            )
            connection.execute(
                "COPY rewritten TO ? (FORMAT PARQUET)",
                [str(replacement)],
            )
        replacement.chmod(0o600)
        os.replace(replacement, target)
        refresh_partition_file_metadata(engine, target)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


@pytest.mark.parametrize("ordering", ["DESC", "ASC"])
def test_read_rejects_nonascending_or_duplicate_global_timestamps(
    tmp_path: Path,
    ordering: str,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3))))
        target = root / stored.partitions[0].relative_path
        replacement = target.with_name("bad-order.parquet")
        select_sql = (
            "SELECT * FROM read_parquet(?, hive_partitioning = false) "
            "ORDER BY timestamp DESC"
            if ordering == "DESC"
            else "SELECT * FROM read_parquet(?, hive_partitioning = false) UNION ALL SELECT * FROM read_parquet(?, hive_partitioning = false)"
        )
        parameters = [str(target)] if ordering == "DESC" else [str(target), str(target)]
        with duckdb.connect(":memory:") as connection:
            connection.execute(
                f"CREATE TABLE rewritten AS {select_sql}",
                parameters,
            )
            connection.execute(
                "COPY rewritten TO ? (FORMAT PARQUET)",
                [str(replacement)],
            )
        replacement.chmod(0o600)
        os.replace(replacement, target)
        refresh_partition_file_metadata(engine, target)
        if ordering == "ASC":
            corrupt_catalog(
                engine,
                table="market_dataset_partition",
                sql="UPDATE market_dataset_partition SET row_count = row_count * 2",
            )
            corrupt_catalog(
                engine,
                table="market_dataset",
                sql="UPDATE market_dataset SET row_count = row_count * 2",
            )

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)
