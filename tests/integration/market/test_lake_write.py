from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from stock_desk.market.lake import MarketLake, StoredRoutingManifest
from stock_desk.market.partitions import (
    PartitionKey,
    partition_manifest_id,
    partition_path,
)
from stock_desk.market.provenance import RoutedBarSuccess
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarResult,
    MAX_BAR_SERIES_ROWS,
    Period,
    TradingStatus,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import (
    SHANGHAI,
    expected_manifest_record_id,
    local_time,
    routed_daily_bars,
)


@pytest.fixture
def catalog_engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _expected_relative_path(routed: RoutedBarSuccess, year: int) -> str:
    key = PartitionKey(
        category="bars",
        source=routed.result.provenance.source,
        symbol=routed.result.query.symbol,
        period=routed.result.query.period,
        adjustment=routed.result.query.adjustment,
        year=year,
    )
    dataset_hex = routed.result.provenance.dataset_version.removeprefix("sha256:")
    return (
        partition_path(key) / f"dataset={dataset_hex}" / "part-00000.parquet"
    ).as_posix()


def test_write_rejects_constructed_over_limit_result_before_publication(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    forged_result = BarResult.model_construct(
        query=routed.result.query,
        bars=routed.result.bars * (MAX_BAR_SERIES_ROWS + 1),
        coverage_start=routed.result.coverage_start,
        coverage_end=routed.result.coverage_end,
        provenance=routed.result.provenance,
    )
    forged = RoutedBarSuccess.model_construct(
        result=forged_result,
        manifest=routed.manifest,
    )
    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)

    with pytest.raises(ValueError):
        lake.write(forged)

    assert not (root / "layout=v1").exists()


def _parquet_description(path: Path) -> tuple[tuple[str, str], ...]:
    with duckdb.connect(":memory:") as connection:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning = false)",
                [str(path)],
            ).fetchall()
        )


def _parquet_bars(path: Path) -> tuple[Bar, ...]:
    with duckdb.connect(":memory:") as connection:
        rows = connection.execute(
            "SELECT symbol, epoch_us(timestamp), period, adjustment, status, "
            '"open", high, low, "close", volume '
            "FROM read_parquet(?, hive_partitioning = false) ORDER BY timestamp",
            [str(path)],
        ).fetchall()
    return tuple(
        Bar(
            symbol=str(row[0]),
            timestamp=datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(microseconds=int(row[1])),
            period=Period(str(row[2])),
            adjustment=Adjustment(str(row[3])),
            status=TradingStatus(str(row[4])),
            open=Decimal(row[5]),
            high=Decimal(row[6]),
            low=Decimal(row[7]),
            close=Decimal(row[8]),
            volume=int(row[9]),
        )
        for row in rows
    )


def test_write_splits_shanghai_years_and_round_trips_fixed_schema(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    routed = routed_daily_bars((date(2023, 12, 31), date(2024, 1, 1)))
    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)

    stored = lake.write(routed)

    assert isinstance(stored, StoredRoutingManifest)
    assert stored.manifest_record_id == expected_manifest_record_id(routed)
    assert stored.dataset_version == routed.result.provenance.dataset_version
    assert stored.route_version == routed.manifest.route_version
    assert tuple(partition.year for partition in stored.partitions) == (2023, 2024)
    assert tuple(partition.relative_path for partition in stored.partitions) == (
        _expected_relative_path(routed, 2023),
        _expected_relative_path(routed, 2024),
    )

    expected_schema = (
        ("symbol", "VARCHAR"),
        ("timestamp", "TIMESTAMP WITH TIME ZONE"),
        ("period", "VARCHAR"),
        ("adjustment", "VARCHAR"),
        ("status", "VARCHAR"),
        ("open", "DECIMAL(24,8)"),
        ("high", "DECIMAL(24,8)"),
        ("low", "DECIMAL(24,8)"),
        ("close", "DECIMAL(24,8)"),
        ("volume", "BIGINT"),
    )
    round_tripped: list[Bar] = []
    for partition in stored.partitions:
        object_path = root / partition.relative_path
        assert object_path.is_file()
        assert _parquet_description(object_path) == expected_schema
        bars = _parquet_bars(object_path)
        assert len(bars) == partition.row_count == 1
        assert bars[0].timestamp.astimezone(SHANGHAI).year == partition.year
        assert partition.dataset_version == stored.dataset_version
        expected_key = PartitionKey(
            category="bars",
            source=routed.result.provenance.source,
            symbol=routed.result.query.symbol,
            period=routed.result.query.period,
            adjustment=routed.result.query.adjustment,
            year=partition.year,
        )
        assert partition.partition_manifest_id == partition_manifest_id(expected_key)
        assert partition.partition_manifest_id not in {
            partition.dataset_version,
            partition.physical_sha256,
        }
        assert partition.physical_sha256 != partition.dataset_version
        round_tripped.extend(bars)

    assert tuple(round_tripped) == routed.result.bars
    assert round_tripped[0].open == Decimal("-2.12500000")
    assert round_tripped[-1].volume == 2**63 - 1
    assert routed.result.bars[-1].timestamp.year == 2023
    assert routed.result.bars[-1].timestamp.astimezone(SHANGHAI).year == 2024


def test_write_is_idempotent_and_keeps_full_fetched_manifest_records(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    first_routed = routed_daily_bars((date(2024, 1, 2),))
    later_routed = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=first_routed.result.provenance.fetched_at + timedelta(days=1),
    )
    assert first_routed.result.provenance.dataset_version == (
        later_routed.result.provenance.dataset_version
    )
    assert first_routed.manifest.route_version == later_routed.manifest.route_version
    lake = MarketLake(engine=catalog_engine, root=tmp_path / "market")

    first = lake.write(first_routed)
    duplicate = lake.write(first_routed)
    later = lake.write(later_routed)

    assert duplicate == first
    assert later.dataset_version == first.dataset_version
    assert later.route_version == first.route_version
    assert later.partitions == first.partitions
    assert later.manifest_record_id != first.manifest_record_id
    assert first.manifest_record_id == expected_manifest_record_id(first_routed)
    assert later.manifest_record_id == expected_manifest_record_id(later_routed)
    with catalog_engine.connect() as connection:
        counts = tuple(
            int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            for table in (
                "market_dataset",
                "market_dataset_partition",
                "market_routing_manifest",
            )
        )
        route_versions = (
            connection.execute(
                text(
                    "SELECT route_version FROM market_routing_manifest "
                    "ORDER BY fetched_at"
                )
            )
            .scalars()
            .all()
        )
    assert counts == (1, 1, 2)
    assert route_versions == [first.route_version, first.route_version]


def test_write_revalidates_boundary_and_recomputes_dataset_digest(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    original = routed.result.bars[0]
    changed = original.model_copy(update={"volume": original.volume - 1})
    forged_result = routed.result.model_copy(update={"bars": (changed,)})
    forged = RoutedBarSuccess.model_construct(
        result=forged_result,
        manifest=routed.manifest,
    )
    lake = MarketLake(engine=catalog_engine, root=tmp_path / "market")

    with pytest.raises(ValueError, match="dataset_version"):
        lake.write(forged)

    with catalog_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM market_dataset")).scalar_one()
            == 0
        )
    assert not tuple((tmp_path / "market").rglob("*.parquet"))


def test_manifest_record_hash_changes_with_fetched_at_not_route_version() -> None:
    first = routed_daily_bars((date(2024, 1, 2),))
    later = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=first.result.provenance.fetched_at + timedelta(days=1),
    )

    assert first.manifest.route_version == later.manifest.route_version
    assert expected_manifest_record_id(first) != expected_manifest_record_id(later)
    assert first.manifest.upstream_fetched_at.tzinfo is not None
    assert local_time(date(2024, 1, 2)).utcoffset() is not None


def test_two_dataset_versions_share_logical_partition_id_but_keep_two_rows(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    first_routed = routed_daily_bars((date(2024, 1, 2),))
    changed_routed = routed_daily_bars(
        (date(2024, 1, 2),),
        volume_delta=-1,
    )
    assert first_routed.result.provenance.dataset_version != (
        changed_routed.result.provenance.dataset_version
    )
    lake = MarketLake(engine=catalog_engine, root=tmp_path / "market")

    first = lake.write(first_routed)
    changed = lake.write(changed_routed)

    assert first.partitions[0].partition_manifest_id == (
        changed.partitions[0].partition_manifest_id
    )
    assert first.partitions[0].dataset_version != changed.partitions[0].dataset_version
    with catalog_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM market_dataset_partition")
            ).scalar_one()
            == 2
        )
