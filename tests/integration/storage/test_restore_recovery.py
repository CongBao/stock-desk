from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil
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


def _crashed_restore(tmp_path: Path, phase: str) -> Path:
    source = tmp_path / "source"
    source_url, _ = _instance(source, marker="new", day=date(2024, 9, 2))
    archive = tmp_path / "source.stockdesk-backup"
    create_backup(database_url=source_url, data_dir=source, destination=archive)
    destination = tmp_path / "destination"
    destination_url, _ = _instance(destination, marker="old", day=date(2023, 9, 4))

    with pytest.raises(RuntimeError, match="simulated crash"):
        restore_backup(
            archive=archive,
            database_url=destination_url,
            data_dir=destination,
            offline=True,
            _phase_hook=lambda observed: (
                (_ for _ in ()).throw(RuntimeError("simulated crash"))
                if observed == phase
                else None
            ),
        )
    return destination


def _restore_stage(destination: Path) -> Path:
    return next(
        path
        for path in destination.glob(".stock-desk-restore-*")
        if path.name != JOURNAL and path.is_dir()
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


@pytest.mark.parametrize("mutation", ("symlink", "hardlink", "content"))
def test_recovery_rejects_unsafe_or_changed_rollback_database(
    tmp_path: Path,
    mutation: str,
) -> None:
    destination = _crashed_restore(tmp_path, "database_installed")
    rollback = _restore_stage(destination) / "rollback" / "stock-desk.db"
    if mutation == "symlink":
        rollback.unlink()
        rollback.symlink_to(destination / "stock-desk.db")
    elif mutation == "hardlink":
        decoy = tmp_path / "rollback-copy.db"
        shutil.copy2(rollback, decoy)
        rollback.unlink()
        os.link(decoy, rollback)
    else:
        rollback.write_bytes(rollback.read_bytes() + b"changed")

    with pytest.raises(RestoreRecoveryRequired, match="database"):
        recover_interrupted_restore(data_dir=destination)


def test_recovery_rejects_symlinked_staged_market(tmp_path: Path) -> None:
    destination = _crashed_restore(tmp_path, "prepared")
    staged_market = _restore_stage(destination) / "new" / "market"
    shutil.rmtree(staged_market)
    staged_market.symlink_to(destination / "market", target_is_directory=True)

    with pytest.raises(RestoreRecoveryRequired, match="market"):
        recover_interrupted_restore(data_dir=destination)


def test_recovery_rejects_symlinked_staged_database(tmp_path: Path) -> None:
    destination = _crashed_restore(tmp_path, "prepared")
    staged_database = _restore_stage(destination) / "new" / "stock-desk.db"
    staged_database.unlink()
    staged_database.symlink_to(destination / "stock-desk.db")

    with pytest.raises(RestoreRecoveryRequired, match="database"):
        recover_interrupted_restore(data_dir=destination)


def test_recovery_rejects_symlinked_rollback_market(tmp_path: Path) -> None:
    destination = _crashed_restore(tmp_path, "market_installed")
    rollback_market = _restore_stage(destination) / "rollback" / "market"
    shutil.rmtree(rollback_market)
    rollback_market.symlink_to(destination / "market", target_is_directory=True)

    with pytest.raises(RestoreRecoveryRequired, match="market"):
        recover_interrupted_restore(data_dir=destination)


def test_recovery_validates_archive_manifest_binding(tmp_path: Path) -> None:
    destination = _crashed_restore(tmp_path, "prepared")
    staged_manifest = _restore_stage(destination) / "archive-manifest.json"
    assert staged_manifest.is_file()
    staged_manifest.write_bytes(staged_manifest.read_bytes() + b"changed")

    with pytest.raises(RestoreRecoveryRequired, match="manifest"):
        recover_interrupted_restore(data_dir=destination)


def test_committed_recovery_requires_manifest_while_stage_remains(
    tmp_path: Path,
) -> None:
    destination = _crashed_restore(tmp_path, "committed")
    (_restore_stage(destination) / "archive-manifest.json").unlink()

    with pytest.raises(RestoreRecoveryRequired, match="manifest"):
        recover_interrupted_restore(data_dir=destination)


def test_rolled_back_recovery_requires_manifest_while_stage_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _crashed_restore(tmp_path, "database_installed")
    original_remove = backup_module._remove_restore_stage
    monkeypatch.setattr(
        backup_module,
        "_remove_restore_stage",
        lambda _data_dir, _token: (_ for _ in ()).throw(
            RuntimeError("crash before stage removal")
        ),
    )
    with pytest.raises(RuntimeError, match="before stage removal"):
        recover_interrupted_restore(data_dir=destination)

    (_restore_stage(destination) / "archive-manifest.json").unlink()
    monkeypatch.setattr(backup_module, "_remove_restore_stage", original_remove)
    with pytest.raises(RestoreRecoveryRequired, match="manifest"):
        recover_interrupted_restore(data_dir=destination)


@pytest.mark.parametrize("mutation", ("content", "symlink", "hardlink"))
def test_committed_recovery_rejects_changed_or_unsafe_live_database(
    tmp_path: Path,
    mutation: str,
) -> None:
    destination = _crashed_restore(tmp_path, "committed")
    database = destination / "stock-desk.db"
    if mutation == "content":
        database.write_bytes(database.read_bytes() + b"changed")
    elif mutation == "symlink":
        database.unlink()
        database.symlink_to(tmp_path / "source" / "stock-desk.db")
    else:
        decoy = tmp_path / "live-copy.db"
        shutil.copy2(database, decoy)
        database.unlink()
        os.link(decoy, database)

    with pytest.raises(RestoreRecoveryRequired, match="database"):
        recover_interrupted_restore(data_dir=destination)


def test_recovery_resumes_after_rollback_stage_cleanup_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _crashed_restore(tmp_path, "database_installed")
    original_remove = backup_module._remove_restore_stage

    def remove_then_crash(data_dir: Path, token: str) -> None:
        original_remove(data_dir, token)
        raise RuntimeError("crash after rollback stage removal")

    monkeypatch.setattr(backup_module, "_remove_restore_stage", remove_then_crash)
    with pytest.raises(RuntimeError, match="stage removal"):
        recover_interrupted_restore(data_dir=destination)
    monkeypatch.setattr(backup_module, "_remove_restore_stage", original_remove)

    assert recover_interrupted_restore(data_dir=destination) is True
    assert _marker(destination / "stock-desk.db") == "old"
    assert not (destination / JOURNAL).exists()


def test_staged_market_directories_are_fsynced_bottom_up_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source_url, _ = _instance(source, marker="new", day=date(2024, 10, 7))
    archive = tmp_path / "source.stockdesk-backup"
    create_backup(database_url=source_url, data_dir=source, destination=archive)
    destination = tmp_path / "destination"
    observed: list[Path] = []
    original_fsync = backup_module._fsync_directory
    original_write_journal = backup_module._write_restore_journal

    def record_fsync(path: Path) -> None:
        observed.append(path)
        original_fsync(path)

    def assert_durable_before_journal(
        data_dir: Path,
        journal: backup_module._RestoreJournal,
    ) -> None:
        if journal.phase == "prepared":
            new_root = data_dir / f".stock-desk-restore-{journal.token}" / "new"
            directories = [
                path for path in (new_root, *new_root.rglob("*")) if path.is_dir()
            ]
            positions = {
                path: max(
                    index
                    for index, observed_path in enumerate(observed)
                    if observed_path == path
                )
                for path in directories
            }
            assert all(
                positions[path] < positions[path.parent]
                for path in directories
                if path != new_root
            )
        original_write_journal(data_dir, journal)

    monkeypatch.setattr(backup_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(
        backup_module,
        "_write_restore_journal",
        assert_durable_before_journal,
    )

    restore_backup(
        archive=archive,
        database_url=f"sqlite:///{destination / 'stock-desk.db'}",
        data_dir=destination,
    )
