from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from stock_desk.market.provenance import RoutedBarSuccess, make_routing_manifest
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Adjustment,
    BarQuery,
    BarResult,
    Period,
    Provenance,
    ProviderId,
)
from tests.integration.market.lake_read_test_helpers import (
    corrupt_catalog,
    open_catalog_engine,
)
from tests.integration.market.lake_test_helpers import local_time, routed_daily_bars


def test_latest_exact_returns_none_without_full_query_match(tmp_path: Path) -> None:
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    query = routed.result.query
    mismatches = (
        query.model_copy(update={"symbol": "000001.SZ"}),
        query.model_copy(update={"period": Period.WEEK}),
        query.model_copy(update={"adjustment": Adjustment.HFQ}),
        query.model_copy(update={"start": query.start + timedelta(days=1)}),
        query.model_copy(update={"end": query.end - timedelta(days=1)}),
    )
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        lake.write(routed)

        assert all(lake.latest_exact(candidate) is None for candidate in mismatches)


def test_latest_exact_returns_validated_manifest_with_year_order(
    tmp_path: Path,
) -> None:
    routed = routed_daily_bars((date(2023, 12, 29), date(2024, 1, 2)))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed)

        latest = lake.latest_exact(routed.result.query)

    assert latest == stored
    assert latest is not None
    assert tuple(partition.year for partition in latest.partitions) == (2023, 2024)


def test_multi_partition_read_reuses_one_private_duckdb_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed = routed_daily_bars(
        (
            date(2023, 12, 27),
            date(2023, 12, 28),
            date(2023, 12, 29),
            date(2024, 1, 2),
        )
    )
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed)
        original_connect = lake_module.duckdb.connect
        connections: list[Any] = []

        def connect(*args: object, **kwargs: object) -> Any:
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        monkeypatch.setattr(lake_module.duckdb, "connect", connect)

        result = lake.read_latest_series(
            routed.result.query.symbol,
            routed.result.query.period,
            routed.result.query.adjustment,
        )

    assert len(stored.partitions) == 2
    assert result == routed
    assert len(connections) == 1
    with pytest.raises(lake_module.duckdb.ConnectionException):
        connections[0].execute("SELECT 1")


def test_partition_parser_connect_failure_is_bounded_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed = routed_daily_bars((date(2023, 12, 29), date(2024, 1, 2)))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        lake.write(routed)
        original_connect = lake_module.duckdb.connect
        attempts = 0

        def flaky_connect(*args: object, **kwargs: object) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise lake_module.duckdb.IOException("synthetic connect failure")
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(lake_module.duckdb, "connect", flaky_connect)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read_latest_series(
                routed.result.query.symbol,
                routed.result.query.period,
                routed.result.query.adjustment,
            )
        recovered = lake.read_latest_series(
            routed.result.query.symbol,
            routed.result.query.period,
            routed.result.query.adjustment,
        )

    assert attempts == 2
    assert recovered == routed


def test_latest_exact_selects_newest_fetch_for_same_route(tmp_path: Path) -> None:
    older = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=local_time(date(2024, 1, 2), 16),
    )
    newer = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=local_time(date(2024, 1, 2), 17),
    )
    assert older.manifest.route_version == newer.manifest.route_version
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        newest_stored = lake.write(newer)
        lake.write(older)

        latest = lake.latest_exact(older.result.query)

    assert latest == newest_stored


def test_latest_exact_prioritizes_data_cutoff_before_fetch_time(
    tmp_path: Path,
) -> None:
    older_cutoff = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=local_time(date(2024, 1, 10), 16),
    )
    newer_cutoff_at = local_time(date(2024, 1, 3), 15)
    newer_fetch_at = local_time(date(2024, 1, 3), 16)
    newer_version = dataset_version(
        source=older_cutoff.result.provenance.source,
        operation="bars",
        request={"query": older_cutoff.result.query},
        data_cutoff=newer_cutoff_at,
        items=older_cutoff.result.bars,
    )
    newer_result = BarResult(
        query=older_cutoff.result.query,
        bars=older_cutoff.result.bars,
        coverage_start=older_cutoff.result.coverage_start,
        coverage_end=older_cutoff.result.coverage_end,
        provenance=Provenance(
            source=older_cutoff.result.provenance.source,
            fetched_at=newer_fetch_at,
            data_cutoff=newer_cutoff_at,
            adjustment=older_cutoff.result.provenance.adjustment,
            dataset_version=newer_version,
        ),
    )
    newer_cutoff = RoutedBarSuccess(
        result=newer_result,
        manifest=make_routing_manifest(
            category=older_cutoff.manifest.category,
            request=older_cutoff.manifest.request,
            priority=older_cutoff.manifest.priority,
            attempts=(),
            selected_source=older_cutoff.manifest.selected_source,
            upstream_dataset_version=newer_version,
            upstream_fetched_at=newer_fetch_at,
            upstream_data_cutoff=newer_cutoff_at,
            upstream_adjustment=newer_result.query.adjustment,
        ),
    )
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        lake.write(older_cutoff)
        expected = lake.write(newer_cutoff)

        latest = lake.latest_exact(older_cutoff.result.query)

    assert latest == expected


def test_latest_exact_breaks_fetch_tie_by_manifest_record_id(tmp_path: Path) -> None:
    first = routed_daily_bars((date(2024, 1, 2),))
    result = first.result
    alternate_manifest = make_routing_manifest(
        category=first.manifest.category,
        request=first.manifest.request,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        attempts=(),
        selected_source=first.manifest.selected_source,
        upstream_dataset_version=first.manifest.upstream_dataset_version,
        upstream_fetched_at=first.manifest.upstream_fetched_at,
        upstream_data_cutoff=first.manifest.upstream_data_cutoff,
        upstream_adjustment=first.manifest.upstream_adjustment,
    )
    alternate = RoutedBarSuccess(result=result, manifest=alternate_manifest)
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = (lake.write(first), lake.write(alternate))

        latest = lake.latest_exact(result.query)

    assert latest is not None
    assert latest.manifest_record_id == max(item.manifest_record_id for item in stored)


def test_latest_exact_canonically_revalidates_query(tmp_path: Path) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    query = routed.result.query
    invalid = BarQuery.model_construct(
        symbol=query.symbol,
        period=query.period,
        adjustment=query.adjustment,
        start=query.end,
        end=query.start,
    )
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")

        with pytest.raises(ValueError, match="start"):
            lake.latest_exact(invalid)


def test_latest_exact_rejects_corrupt_latest_without_falling_back(
    tmp_path: Path,
) -> None:
    older = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=local_time(date(2024, 1, 2), 16),
    )
    newer = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=local_time(date(2024, 1, 2), 17),
    )
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        lake.write(older)
        newest = lake.write(newer)
        corrupt_catalog(
            engine,
            table="market_routing_manifest",
            sql=(
                "UPDATE market_routing_manifest SET route_version = ? "
                "WHERE manifest_record_id = ?"
            ),
            parameters=(f"sha256:{'0' * 64}", newest.manifest_record_id),
        )

        with pytest.raises(MarketLakeCorruptionError):
            lake.latest_exact(older.result.query)


def test_read_latest_exact_returns_routed_data_or_none(tmp_path: Path) -> None:
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        lake.write(routed)

        assert lake.read_latest_exact(routed.result.query) == routed
        assert (
            lake.read_latest_exact(
                routed.result.query.model_copy(update={"symbol": "000001.SZ"})
            )
            is None
        )


def test_read_latest_series_prioritizes_data_cutoff_before_fetch_time(
    tmp_path: Path,
) -> None:
    older_cutoff = routed_daily_bars(
        (date(2024, 1, 2),),
        fetched_at=local_time(date(2024, 1, 10), 16),
    )
    newer_cutoff = routed_daily_bars(
        (date(2024, 1, 2), date(2024, 1, 3)),
        fetched_at=local_time(date(2024, 1, 3), 16),
    )
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        lake.write(older_cutoff)
        lake.write(newer_cutoff)

        latest = lake.read_latest_series(
            newer_cutoff.result.query.symbol,
            newer_cutoff.result.query.period,
            newer_cutoff.result.query.adjustment,
        )

    assert latest == newer_cutoff


def test_read_latest_series_returns_none_for_cache_miss(tmp_path: Path) -> None:
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")

        assert (
            lake.read_latest_series(
                "600000.SH",
                Period.DAY,
                Adjustment.QFQ,
            )
            is None
        )


def test_read_latest_series_rejects_corrupt_newest_without_fallback(
    tmp_path: Path,
) -> None:
    older = routed_daily_bars((date(2024, 1, 2),))
    newer = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        lake.write(older)
        newest = lake.write(newer)
        corrupt_catalog(
            engine,
            table="market_routing_manifest",
            sql=(
                "UPDATE market_routing_manifest SET route_version = ? "
                "WHERE manifest_record_id = ?"
            ),
            parameters=(f"sha256:{'0' * 64}", newest.manifest_record_id),
        )

        with pytest.raises(MarketLakeCorruptionError):
            lake.read_latest_series(
                newer.result.query.symbol,
                newer.result.query.period,
                newer.result.query.adjustment,
            )
