from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

from sqlalchemy import insert

from stock_desk.storage.backup import create_backup, inspect_backup
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import AppSetting
from stock_desk.market.lake import MarketLake
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def test_portable_backup_is_canonical_secret_free_and_catalog_bounded(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "stock-desk.db"
    database_url = f"sqlite:///{database}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    lake = MarketLake(engine=engine, root=(data_dir / "market").resolve())
    stored = lake.write(
        routed_daily_bars(
            (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
        )
    )
    secret = "ciphertext-marker-that-must-not-remain"
    with engine.begin() as connection:
        connection.execute(
            insert(AppSetting).values(
                key="secret.tushare_token",
                encrypted_value=secret,
                updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            )
        )
    referenced = data_dir / "market" / stored.partitions[0].relative_path
    unreferenced = referenced.with_name("unreferenced.parquet")
    unreferenced.write_bytes(b"not catalog owned")
    unreferenced.chmod(0o600)
    (data_dir / "tdx").mkdir(mode=0o700)
    (data_dir / "tdx" / "external.day").write_bytes(b"external")
    (data_dir / "exports").mkdir(mode=0o700)
    (data_dir / "exports" / "report.csv").write_text("private", encoding="utf-8")
    (data_dir / ".env").write_text("STOCK_DESK_MASTER_KEY=unsafe", encoding="utf-8")
    archive = tmp_path / "portable.stockdesk-backup"

    result = create_backup(
        database_url=database_url,
        data_dir=data_dir,
        destination=archive,
    )
    engine.dispose()

    assert result.archive == archive
    assert not tuple(tmp_path.glob(".portable.stockdesk-backup.*.tmp"))
    manifest = inspect_backup(archive)
    assert manifest.secret_policy == "omitted"
    assert manifest.master_key_included is False
    assert manifest.task_barrier.running_count == 0
    expected_partition = f"market/{stored.partitions[0].relative_path}"
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert names == [
            "manifest.json",
            "manifest.sha256",
            "database/stock-desk.db",
            "market/.stock-desk-market-lake",
            expected_partition,
        ]
        assert len(names) == len(set(names))
        manifest_bytes = bundle.read("manifest.json")
        assert manifest_bytes == json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        assert bundle.read("manifest.sha256") == (
            _sha256(manifest_bytes) + "\n"
        ).encode("ascii")
        for info in bundle.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
        cloned = tmp_path / "cloned.db"
        cloned.write_bytes(bundle.read("database/stock-desk.db"))

    with sqlite3.connect(cloned) as connection:
        assert connection.execute(
            "SELECT count(*) FROM app_setting WHERE key LIKE 'secret.%'"
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert secret.encode("utf-8") not in cloned.read_bytes()
    assert unreferenced.name not in names
    assert not any(
        token in name for name in names for token in (".locks", "tdx", "exports", ".env")
    )
    partition_entry = next(
        item for item in manifest.files if item.archive_path == expected_partition
    )
    assert partition_entry.sha256 == stored.partitions[0].physical_sha256
    assert partition_entry.sha256 == _sha256(referenced.read_bytes())


def test_backup_clone_contains_committed_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "stock-desk.db"
    database_url = f"sqlite:///{database}"
    migrate(database_url)
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(
        "INSERT INTO app_setting(key, encrypted_value, updated_at) VALUES(?,?,?)",
        ("public.wal-proof", "present", "2025-01-01 00:00:00.000000"),
    )
    writer.commit()
    assert database.with_name(f"{database.name}-wal").stat().st_size > 0
    archive = tmp_path / "wal.stockdesk-backup"

    create_backup(
        database_url=database_url,
        data_dir=data_dir,
        destination=archive,
    )
    writer.close()

    with zipfile.ZipFile(archive) as bundle:
        cloned = tmp_path / "wal-clone.db"
        cloned.write_bytes(bundle.read("database/stock-desk.db"))
    with sqlite3.connect(cloned) as connection:
        assert connection.execute(
            "SELECT encrypted_value FROM app_setting WHERE key = 'public.wal-proof'"
        ).fetchone() == ("present",)


def test_recovery_backup_can_retain_encrypted_secrets(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "stock-desk.db"
    database_url = f"sqlite:///{database}"
    migrate(database_url)
    encrypted_value = "encrypted-local-recovery-value"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_setting(key, encrypted_value, updated_at) VALUES(?,?,?)",
            ("secret.provider", encrypted_value, "2025-01-01 00:00:00.000000"),
        )

    archive = tmp_path / "recovery.stockdesk-backup"
    manifest = create_backup(
        database_url=database_url,
        data_dir=data_dir,
        destination=archive,
        include_encrypted_secrets=True,
    ).manifest

    assert manifest.secret_policy == "encrypted_included"
    assert manifest.master_key_included is False
    with zipfile.ZipFile(archive) as bundle:
        clone = tmp_path / "recovery.db"
        clone.write_bytes(bundle.read("database/stock-desk.db"))
    with sqlite3.connect(clone) as connection:
        assert connection.execute(
            "SELECT encrypted_value FROM app_setting WHERE key = 'secret.provider'"
        ).fetchone() == (encrypted_value,)
