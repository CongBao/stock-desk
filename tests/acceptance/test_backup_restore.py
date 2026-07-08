from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3

from stock_desk.market.lake import MarketLake
from stock_desk.storage.backup import create_backup, restore_backup
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from tests.integration.market.lake_test_helpers import routed_daily_bars


def test_backup_restore_round_trip_preserves_inventory_and_dataset_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source_url = f"sqlite:///{source / 'stock-desk.db'}"
    migrate(source_url)
    source_engine = create_engine_for_url(source_url)
    source_lake = MarketLake(
        engine=source_engine,
        root=(source / "market").resolve(),
    )
    stored = source_lake.write(routed_daily_bars((date(2024, 4, 1), date(2024, 4, 2))))
    tasks = TaskRepository(source_engine)
    queued = tasks.create("market.update", {"reason": "backup acceptance"})
    archive = tmp_path / "round-trip.stockdesk-backup"
    backup = create_backup(
        database_url=source_url,
        data_dir=source,
        destination=archive,
    )
    tasks.close()
    source_engine.dispose()

    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    (restored / "tdx").mkdir(mode=0o700)
    external = restored / "tdx" / "sh600000.day"
    external.write_bytes(b"external input must survive owned-component restore")
    result = restore_backup(
        archive=archive,
        database_url=f"sqlite:///{restored / 'stock-desk.db'}",
        data_dir=restored,
        offline=True,
    )

    assert result.manifest.dataset_partitions == backup.manifest.dataset_partitions
    assert result.manifest.logical_inventory == backup.manifest.logical_inventory
    assert (
        external.read_bytes() == b"external input must survive owned-component restore"
    )
    restored_partition = restored / "market" / stored.partitions[0].relative_path
    source_partition = source / "market" / stored.partitions[0].relative_path
    assert restored_partition.read_bytes() == source_partition.read_bytes()
    with sqlite3.connect(restored / "stock-desk.db") as connection:
        assert connection.execute(
            "SELECT status FROM task_run WHERE id = ?", (queued.id,)
        ).fetchone() == ("queued",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
