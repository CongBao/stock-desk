from __future__ import annotations

from datetime import date
import multiprocessing
import os
from pathlib import Path
import subprocess

import pytest

from stock_desk.config import Settings
from stock_desk.desktop import _restrict_owner_access
from stock_desk.market.lake import (
    MarketLake,
    MarketLakeCorruptionError,
    SqliteMarketLake,
    create_market_lake,
    manifest_record_id,
)
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.lifecycle import service_lifecycle
from tests.integration.market.lake_test_helpers import routed_daily_bars


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="requires the Windows ACL implementation"
)


class SimulatedWindowsPublishCrash(BaseException):
    pass


def _construct_market_lake_in_spawned_process(payload: tuple[str, str]) -> str:
    database_url, root_value = payload
    engine = create_engine_for_url(database_url)
    try:
        return type(MarketLake(engine=engine, root=Path(root_value))).__name__
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("relative", "directory"),
    [
        (Path("directory with spaces") / "owner's 数据", True),
        (Path("file with spaces") / "owner's 记录.txt", False),
    ],
)
def test_windows_runtime_acl_executes_for_untrusted_path_characters(
    tmp_path: Path,
    relative: Path,
    directory: bool,
) -> None:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if directory:
        target.mkdir()
    else:
        target.write_text("private\n", encoding="utf-8")

    _restrict_owner_access(target, directory=directory)


def test_windows_service_lifecycle_reuses_existing_directory(tmp_path: Path) -> None:
    with service_lifecycle(tmp_path, role="api"):
        pass

    with service_lifecycle(tmp_path, role="worker"):
        pass


def test_windows_market_lake_factory_initializes_sqlite_backend(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    try:
        lake = create_market_lake(
            engine=engine,
            root=(tmp_path / "market").resolve(),
        )
        routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
        stored = lake.write(routed)

        assert isinstance(lake, SqliteMarketLake)
        assert lake.database_identity
        assert stored.partitions == ()
        assert lake.read(stored.manifest_record_id) == routed
    finally:
        engine.dispose()


def test_windows_market_lake_direct_constructor_initializes_private_root(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    root = (tmp_path / "market").resolve()
    try:
        lake = MarketLake(engine=engine, root=root)

        assert isinstance(lake, SqliteMarketLake)
        assert root.is_dir()
        assert (root / ".stock-desk-market-lake").read_bytes() == (
            b"stock-desk-market-lake-v1\n"
        )
        marker = root / ".stock-desk-market-lake"
        routed = routed_daily_bars((date(2024, 1, 2),))
        deletion_observed = False

        def marker_is_pinned_before_commit() -> None:
            nonlocal deletion_observed
            try:
                marker.unlink()
            except PermissionError:
                return
            deletion_observed = True

        try:
            lake._commit_catalog(  # noqa: SLF001 -- transaction binding contract
                routed,
                manifest_record_id(routed.manifest),
                (),
                before_commit=marker_is_pinned_before_commit,
            )
        except MarketLakeCorruptionError:
            assert deletion_observed
            with engine.connect() as connection:
                assert (
                    connection.exec_driver_sql(
                        "SELECT COUNT(*) FROM market_dataset"
                    ).scalar_one()
                    == 0
                )
            marker.write_bytes(b"stock-desk-market-lake-v1\n")
        else:
            assert not deletion_observed
        marker.unlink()
        marker.write_bytes(b"stock-desk-market-lake-v1\n")
        with pytest.raises(MarketLakeCorruptionError, match="root"):
            lake.write(routed)
    finally:
        engine.dispose()


def test_windows_market_lake_rejects_junction_root(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    external = tmp_path / "external"
    external.mkdir()
    junction = tmp_path / "junction"
    command = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    created = subprocess.run(  # noqa: S603 -- fixed Windows system command
        (str(command), "/d", "/c", "mklink", "/J", str(junction), str(external)),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if created.returncode != 0:
        engine.dispose()
        pytest.skip("Windows junction creation is unavailable")
    try:
        with pytest.raises(ValueError, match="reparse"):
            MarketLake(engine=engine, root=junction)

        assert tuple(external.iterdir()) == ()
    finally:
        junction.rmdir()
        engine.dispose()


def test_windows_market_lake_named_mutex_serializes_processes(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    root = (tmp_path / "concurrent-market").resolve()
    context = multiprocessing.get_context("spawn")

    with context.Pool(processes=4) as pool:
        backends = pool.map(
            _construct_market_lake_in_spawned_process,
            ((database_url, str(root)),) * 8,
        )

    assert backends == ["SqliteMarketLake"] * 8
    assert (root / ".stock-desk-market-lake").read_bytes() == (
        b"stock-desk-market-lake-v1\n"
    )
    assert not tuple(root.glob(".stock-desk-market-lake.init-*.tmp"))


def test_windows_market_lake_recovers_crash_before_atomic_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stock_desk.market.lake as lake_module

    database_url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    root = (tmp_path / "crash-market").resolve()
    original_publish = lake_module._windows_move_no_replace

    def crash_before_publish(source: Path, destination: Path) -> None:
        raise SimulatedWindowsPublishCrash

    monkeypatch.setattr(lake_module, "_windows_move_no_replace", crash_before_publish)
    try:
        with pytest.raises(SimulatedWindowsPublishCrash):
            MarketLake(engine=engine, root=root)
        temporary = tuple(root.glob(".stock-desk-market-lake.init-*.tmp"))
        assert len(temporary) == 1
        assert temporary[0].read_bytes() == b"stock-desk-market-lake-v1\n"

        monkeypatch.setattr(lake_module, "_windows_move_no_replace", original_publish)
        assert isinstance(MarketLake(engine=engine, root=root), SqliteMarketLake)
        assert not tuple(root.glob(".stock-desk-market-lake.init-*.tmp"))
    finally:
        engine.dispose()


def test_windows_production_worker_opens_with_sqlite_market_backend(
    tmp_path: Path,
) -> None:
    data_dir = (tmp_path / "worker-data").resolve()
    database_url = f"sqlite:///{data_dir / 'stock-desk.db'}"
    runtime = ProductionMarketWorker.open(
        Settings(data_dir=data_dir, database_url=database_url),
        worker_id="windows-open-smoke",
    )
    try:
        assert runtime.run_once() is None
    finally:
        runtime.close()
