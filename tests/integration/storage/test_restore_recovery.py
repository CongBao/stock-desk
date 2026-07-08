from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3

import pytest

import stock_desk.storage.backup as backup_module
from stock_desk.market.lake import MarketLake
from stock_desk.storage.backup import (
    BackupValidationError,
    RestoreRecoveryRequired,
    create_backup,
    recover_interrupted_restore,
    restore_backup,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


JOURNAL = ".stock-desk-restore-journal.json"
CRASH_PHASES = (
    "prepared",
    "database_old_moved",
    "database_installed",
    "market_old_moved",
    "market_installed",
)


def _instance(root: Path, *, marker: str, day: date) -> tuple[str, bytes]:
    database = root / "stock-desk.db"
    url = f"sqlite:///{database}"
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=(root / "market").resolve())
    stored = lake.write(routed_daily_bars((day,)))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_setting(key, encrypted_value, updated_at) VALUES(?,?,?)",
            ("public.restore-marker", marker, "2025-01-01 00:00:00.000000"),
        )
        connection.execute(
            "INSERT INTO app_setting(key, encrypted_value, updated_at) VALUES(?,?,?)",
            ("secret.restore-proof", f"cipher-{marker}", "2025-01-01 00:00:00.000000"),
        )
    partition = root / "market" / stored.partitions[0].relative_path
    payload = partition.read_bytes()
    engine.dispose()
    return url, payload


def _marker(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        return str(
            connection.execute(
                "SELECT encrypted_value FROM app_setting "
                "WHERE key = 'public.restore-marker'"
            ).fetchone()[0]
        )


@pytest.mark.parametrize("crash_phase", CRASH_PHASES)
def test_interrupted_restore_rolls_back_original_components(
    tmp_path: Path, crash_phase: str
) -> None:
    source = tmp_path / "source"
    source_url, _new_partition = _instance(source, marker="new", day=date(2024, 5, 6))
    archive = tmp_path / "source.stockdesk-backup"
    create_backup(database_url=source_url, data_dir=source, destination=archive)
    destination = tmp_path / "destination"
    destination_url, old_partition = _instance(
        destination, marker="old", day=date(2023, 5, 6)
    )

    def crash_after(phase: str) -> None:
        if phase == crash_phase:
            raise RuntimeError(f"simulated crash after {phase}")

    with pytest.raises(RuntimeError, match="simulated crash"):
        restore_backup(
            archive=archive,
            database_url=destination_url,
            data_dir=destination,
            offline=True,
            _phase_hook=crash_after,
        )
    assert (destination / JOURNAL).is_file()

    assert recover_interrupted_restore(data_dir=destination) is True

    assert _marker(destination / "stock-desk.db") == "old"
    parquet = next((destination / "market").rglob("*.parquet"))
    assert parquet.read_bytes() == old_partition
    assert not (destination / JOURNAL).exists()
    assert not tuple(destination.glob(".stock-desk-restore-*"))


def test_committed_restore_is_finalized_not_rolled_back(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source_url, new_partition = _instance(source, marker="new", day=date(2024, 6, 3))
    archive = tmp_path / "source.stockdesk-backup"
    create_backup(database_url=source_url, data_dir=source, destination=archive)
    destination = tmp_path / "destination"
    destination_url, _old_partition = _instance(
        destination, marker="old", day=date(2023, 6, 3)
    )

    with pytest.raises(RuntimeError, match="after committed"):
        restore_backup(
            archive=archive,
            database_url=destination_url,
            data_dir=destination,
            offline=True,
            _phase_hook=lambda phase: (
                (_ for _ in ()).throw(RuntimeError("after committed"))
                if phase == "committed"
                else None
            ),
        )

    assert recover_interrupted_restore(data_dir=destination) is True
    assert _marker(destination / "stock-desk.db") == "new"
    assert next((destination / "market").rglob("*.parquet")).read_bytes() == (
        new_partition
    )


def test_nonempty_restore_requires_offline_confirmation_and_keeps_secrets_backup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source_url, _ = _instance(source, marker="new", day=date(2024, 7, 1))
    archive = tmp_path / "source.stockdesk-backup"
    create_backup(database_url=source_url, data_dir=source, destination=archive)
    destination = tmp_path / "destination"
    destination_url, _ = _instance(destination, marker="old", day=date(2023, 7, 3))

    with pytest.raises(BackupValidationError, match="offline"):
        restore_backup(
            archive=archive,
            database_url=destination_url,
            data_dir=destination,
        )

    restore_backup(
        archive=archive,
        database_url=destination_url,
        data_dir=destination,
        offline=True,
    )
    recovery_archives = tuple(
        (destination / ".stock-desk-recovery").glob("*.stockdesk-backup")
    )
    assert len(recovery_archives) == 1
    from stock_desk.storage.backup import inspect_backup

    assert inspect_backup(recovery_archives[0]).secret_policy == "encrypted_included"


def test_corrupt_recovery_journal_refuses_startup(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / JOURNAL).write_text('{"phase":"database_installed"}', encoding="utf-8")

    with pytest.raises(RestoreRecoveryRequired, match="journal"):
        recover_interrupted_restore(data_dir=tmp_path)


@pytest.mark.parametrize("failure", ("disk", "migration"))
def test_staging_failure_leaves_original_components_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source = tmp_path / "source"
    source_url, _ = _instance(source, marker="new", day=date(2024, 8, 5))
    archive = tmp_path / "source.stockdesk-backup"
    create_backup(database_url=source_url, data_dir=source, destination=archive)
    destination = tmp_path / "destination"
    destination_url, old_partition = _instance(
        destination, marker="old", day=date(2023, 8, 7)
    )
    if failure == "disk":
        monkeypatch.setattr(
            backup_module,
            "_extract_verified_archive",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        expected_error: type[BaseException] = OSError
    else:
        monkeypatch.setattr(
            backup_module,
            "migrate",
            lambda _url: (_ for _ in ()).throw(RuntimeError("migration failed")),
        )
        expected_error = BackupValidationError

    with pytest.raises(expected_error):
        restore_backup(
            archive=archive,
            database_url=destination_url,
            data_dir=destination,
            offline=True,
        )

    assert _marker(destination / "stock-desk.db") == "old"
    assert next((destination / "market").rglob("*.parquet")).read_bytes() == (
        old_partition
    )
    assert not (destination / JOURNAL).exists()
    assert not tuple(destination.glob(".stock-desk-restore-*"))
