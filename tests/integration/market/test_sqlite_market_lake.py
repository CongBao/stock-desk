from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import (
    MarketLakeCorruptionError,
    SqliteMarketLake,
    create_market_lake,
)
from stock_desk.formula.service import FormulaService
from stock_desk.market.types import Period
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.backtest_test_helpers import (
    WAVE_FORMULA,
    BacktestHarness,
    local_time,
    routed_status,
    routed_wave_bars,
    weekday_range,
)
from tests.integration.market.lake_read_test_helpers import corrupt_catalog
from tests.integration.market.lake_test_helpers import routed_daily_bars


def test_windows_factory_selects_sqlite_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    try:
        lake = create_market_lake(engine=engine, root=(tmp_path / "market").resolve())

        assert isinstance(lake, SqliteMarketLake)
    finally:
        engine.dispose()


def test_sqlite_market_lake_write_read_duplicate_and_reopen(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    root = (tmp_path / "market").resolve()
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    migrate(database_url)

    first_engine = create_engine_for_url(database_url)
    try:
        lake = SqliteMarketLake(engine=first_engine, root=root)

        stored = lake.write(routed)

        assert stored.partitions == ()
        assert lake.write(routed) == stored
        assert lake.read(stored.manifest_record_id) == routed
        assert lake.read_latest_exact(routed.result.query) == routed
        assert (
            lake.read_latest_series(
                routed.result.query.symbol,
                routed.result.query.period,
                routed.result.query.adjustment,
            )
            == routed
        )
    finally:
        first_engine.dispose()

    reopened_engine = create_engine_for_url(database_url)
    try:
        reopened = SqliteMarketLake(engine=reopened_engine, root=root)

        assert reopened.read(stored.manifest_record_id) == routed
        assert reopened.latest_exact(routed.result.query) == stored
    finally:
        reopened_engine.dispose()


def test_sqlite_market_lake_concurrent_writes_are_idempotent(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    try:
        routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
        lake = SqliteMarketLake(engine=engine, root=(tmp_path / "market").resolve())

        with ThreadPoolExecutor(max_workers=8) as pool:
            stored = tuple(pool.map(lambda _index: lake.write(routed), range(16)))

        assert len(set(stored)) == 1
        assert lake.read(stored[0].manifest_record_id) == routed
    finally:
        engine.dispose()


def test_sqlite_market_lake_concurrently_keeps_distinct_fetch_manifests(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    try:
        first = routed_daily_bars((date(2024, 1, 2),))
        later = routed_daily_bars(
            (date(2024, 1, 2),),
            fetched_at=first.result.provenance.fetched_at + timedelta(hours=1),
        )
        lake = SqliteMarketLake(engine=engine, root=(tmp_path / "market").resolve())
        routed = (first, later) * 8

        with ThreadPoolExecutor(max_workers=8) as pool:
            stored = tuple(pool.map(lake.write, routed))

        assert len({item.manifest_record_id for item in stored}) == 2
        assert lake.read_latest_exact(first.result.query) == later
    finally:
        engine.dispose()


def test_sqlite_market_lake_rejects_tampered_payload(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    try:
        routed = routed_daily_bars((date(2024, 1, 2),))
        lake = SqliteMarketLake(engine=engine, root=(tmp_path / "market").resolve())
        stored = lake.write(routed)
        corrupt_catalog(
            engine,
            table="market_dataset_timestamp",
            sql=(
                "UPDATE market_dataset_timestamp SET close = close + 1 "
                "WHERE dataset_version = ? AND ordinal = 0"
            ),
            parameters=(stored.dataset_version,),
        )

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)
    finally:
        engine.dispose()


def test_sqlite_market_lake_drives_formula_signals_and_backtest(
    tmp_path: Path,
) -> None:
    with BacktestHarness.create(tmp_path) as harness:
        sqlite_lake = SqliteMarketLake(
            engine=harness.engine,
            root=(tmp_path / "sqlite-market").resolve(),
        )
        harness.market = sqlite_lake
        harness.seed_instruments("600000.SH")
        days = weekday_range(date(2024, 1, 2), date(2024, 4, 2))
        routed = routed_wave_bars("600000.SH", Period.DAY, days)
        stored = sqlite_lake.write(routed)
        harness.statuses.write(routed_status("600000.SH", Period.DAY, routed))
        version = harness.create_formula("SQLite 核心链路", WAVE_FORMULA)

        preview = FormulaService(
            repository=harness.formula_repository,
            lake=sqlite_lake,
        ).preview(version.id, routed.result.query, {})
        signals = {signal.name: signal.values for signal in preview.signals}

        assert preview.manifest_record_id == stored.manifest_record_id
        assert any(value is True for value in signals["BUY"])
        assert any(value is True for value in signals["SELL"])

        completed = harness.run_single(
            version.id,
            symbol="600000.SH",
            period=Period.DAY,
            scoring_start=local_time(days[10]),
            scoring_end=local_time(days[-1] + timedelta(days=1)),
        )

        assert completed.run.status == "succeeded"
        assert completed.report.metrics["realized_count"] > 0
        assert completed.run.symbols[0].signal_series_id == preview.signal_series_id
