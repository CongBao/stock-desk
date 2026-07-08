from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import sqlite3
from typing import Any

import pytest

import stock_desk.storage.backup as backup_module
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


def _marker_archive(root: Path, archive: Path, marker: str) -> None:
    database = root / "stock-desk.db"
    url = f"sqlite:///{database}"
    migrate(url)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_setting(key, encrypted_value, updated_at) VALUES(?,?,?)",
            ("public.archive-identity", marker, "2025-01-01 00:00:00.000000"),
        )
    create_backup(database_url=url, data_dir=root, destination=archive)


def test_restore_extracts_the_same_open_archive_that_was_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "selected.stockdesk-backup"
    replacement = tmp_path / "replacement.stockdesk-backup"
    _marker_archive(tmp_path / "selected-source", archive, "selected")
    _marker_archive(tmp_path / "replacement-source", replacement, "replacement")
    original_restore = backup_module._restore_backup_locked

    def replace_path_before_restore(**kwargs: Any) -> backup_module.RestoreResult:
        os.replace(replacement, archive)
        return original_restore(**kwargs)

    monkeypatch.setattr(
        backup_module,
        "_restore_backup_locked",
        replace_path_before_restore,
    )
    destination = tmp_path / "identity-restored"
    result = restore_backup(
        archive=archive,
        database_url=f"sqlite:///{destination / 'stock-desk.db'}",
        data_dir=destination,
    )

    assert result.database == destination / "stock-desk.db"
    with sqlite3.connect(result.database) as connection:
        assert connection.execute(
            "SELECT encrypted_value FROM app_setting "
            "WHERE key = 'public.archive-identity'"
        ).fetchone() == ("selected",)
