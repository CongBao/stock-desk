from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from stock_desk.market.provenance import RoutedBarSuccess, make_routing_manifest
from stock_desk.market.types import Adjustment, BarQuery, Period, ProviderId
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
