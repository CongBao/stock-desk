from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import (
    MarketLake,
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


class SimulatedWindowsCrash(BaseException):
    pass


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


def test_windows_direct_constructor_selects_transactional_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    try:
        root = (tmp_path / "market").resolve()

        lake = MarketLake(engine=engine, root=root)

        assert isinstance(lake, SqliteMarketLake)
        assert root.is_dir()
        assert (root / ".stock-desk-market-lake").read_bytes() == (
            b"stock-desk-market-lake-v1\n"
        )
    finally:
        engine.dispose()


def test_windows_backend_rejects_relative_and_unowned_roots_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    try:
        with pytest.raises(ValueError, match="absolute"):
            MarketLake(engine=engine, root=Path("market"))

        root = (tmp_path / "unowned").resolve()
        root.mkdir()
        unrelated = root / "do-not-touch.txt"
        unrelated.write_bytes(b"operator data\n")
        with pytest.raises(ValueError, match="ownership marker"):
            MarketLake(engine=engine, root=root)

        assert unrelated.read_bytes() == b"operator data\n"
        assert {item.name for item in root.iterdir()} == {"do-not-touch.txt"}
    finally:
        engine.dispose()


def test_windows_backend_rejects_symlink_or_reparse_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    real_root = (tmp_path / "real").resolve()
    real_root.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_root, target_is_directory=True)
    except OSError:
        engine.dispose()
        pytest.skip("symlink creation is unavailable")
    try:
        with pytest.raises(ValueError, match="reparse|symlink"):
            MarketLake(engine=engine, root=alias)

        assert tuple(real_root.iterdir()) == ()
    finally:
        engine.dispose()


def test_windows_backend_concurrent_initialization_and_writes_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    root = (tmp_path / "market").resolve()
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    try:

        def initialize_and_write(_index: int) -> object:
            return MarketLake(engine=engine, root=root).write(routed)

        with ThreadPoolExecutor(max_workers=8) as pool:
            stored = tuple(pool.map(initialize_and_write, range(16)))

        assert len(set(stored)) == 1
        assert (root / ".stock-desk-market-lake").read_bytes() == (
            b"stock-desk-market-lake-v1\n"
        )
        assert not tuple(root.glob(".stock-desk-market-lake.init-*.tmp"))
    finally:
        engine.dispose()


def test_windows_backend_recovers_fsynced_marker_after_publish_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    root = (tmp_path / "market").resolve()
    original_publish = lake_module._windows_move_no_replace
    crashed = False

    def crash_before_publish(source: Path, destination: Path) -> None:
        nonlocal crashed
        crashed = True
        raise SimulatedWindowsCrash

    monkeypatch.setattr(lake_module, "_windows_move_no_replace", crash_before_publish)
    try:
        with pytest.raises(SimulatedWindowsCrash):
            MarketLake(engine=engine, root=root)

        temporary = tuple(root.glob(".stock-desk-market-lake.init-*.tmp"))
        assert crashed
        assert len(temporary) == 1
        assert temporary[0].read_bytes() == b"stock-desk-market-lake-v1\n"
        assert not (root / ".stock-desk-market-lake").exists()

        monkeypatch.setattr(lake_module, "_windows_move_no_replace", original_publish)
        lake = MarketLake(engine=engine, root=root)

        assert isinstance(lake, SqliteMarketLake)
        assert (root / ".stock-desk-market-lake").read_bytes() == (
            b"stock-desk-market-lake-v1\n"
        )
        assert not tuple(root.glob(".stock-desk-market-lake.init-*.tmp"))
    finally:
        engine.dispose()


def test_windows_backend_rejects_marker_rebinding_before_catalog_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    root = (tmp_path / "market").resolve()
    routed = routed_daily_bars((date(2024, 1, 2),))
    try:
        lake = MarketLake(engine=engine, root=root)
        marker = root / ".stock-desk-market-lake"
        original_identity = (marker.stat().st_dev, marker.stat().st_ino)
        replacement = root / ".stock-desk-market-lake.replacement"
        replacement.write_bytes(b"stock-desk-market-lake-v1\n")
        replacement.replace(marker)
        assert (marker.stat().st_dev, marker.stat().st_ino) != original_identity

        with pytest.raises(MarketLakeCorruptionError, match="root"):
            lake.write(routed)

        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM market_dataset"
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_windows_backend_revalidates_root_before_transaction_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    monkeypatch.setattr(lake_module, "_PLATFORM", "nt")
    root = (tmp_path / "market").resolve()
    routed = routed_daily_bars((date(2024, 1, 2),))
    try:
        lake = MarketLake(engine=engine, root=root)
        original_validate = lake_module._validate_windows_root_binding
        validation_count = 0

        def reject_final_binding(
            bound_root: Path,
            root_identity: tuple[int, int],
            marker_identity: tuple[int, int],
        ) -> None:
            nonlocal validation_count
            validation_count += 1
            original_validate(bound_root, root_identity, marker_identity)
            if validation_count == 2:
                raise ValueError("simulated final root rebinding")

        monkeypatch.setattr(
            lake_module,
            "_validate_windows_root_binding",
            reject_final_binding,
        )

        with pytest.raises(MarketLakeCorruptionError, match="transaction binding"):
            lake.write(routed)

        assert validation_count == 2
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM market_dataset"
                ).scalar_one()
                == 0
            )
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
