from __future__ import annotations

from datetime import date
import os
from pathlib import Path

import pytest

from stock_desk.config import Settings
from stock_desk.desktop import _restrict_owner_access
from stock_desk.market.lake import SqliteMarketLake, create_market_lake
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.lifecycle import service_lifecycle
from tests.integration.market.lake_test_helpers import routed_daily_bars


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="requires the Windows ACL implementation"
)


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
