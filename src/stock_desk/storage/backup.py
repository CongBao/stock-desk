"""Verified, task-consistent portable backup archives for Stock Desk."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Final, Literal, Self, cast
from urllib.parse import unquote
from uuid import uuid4
import zipfile

from filelock import Timeout as FileLockTimeout
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from sqlalchemy.engine import make_url

from stock_desk.market.lake import (
    MarketLake,
    _descriptor_sha256,
    _open_absolute_root,
    _open_catalog_leaf,
    _ownership_marker_stat,
)
from stock_desk.storage.database import (
    create_engine_for_url,
    migrate,
    migration_lock,
)
from stock_desk.tasks.repository import TaskRepository


BACKUP_SUFFIX: Final = ".stockdesk-backup"
BACKUP_SCHEMA_VERSION: Final = "stock-desk-backup-v1"
_MARKET_MARKER = ".stock-desk-market-lake"
_MAX_ARCHIVE_ENTRIES = 100_000
_MAX_ARCHIVE_FILE_BYTES = 64 * 1024 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1_000
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_RESTORE_JOURNAL = ".stock-desk-restore-journal.json"
_RECOVERY_DIRECTORY = ".stock-desk-recovery"
_INVENTORY_QUERIES = {
    "task_run": "SELECT id FROM task_run ORDER BY id",
    "formula_version": "SELECT id FROM formula_version ORDER BY id",
    "backtest_run": "SELECT id FROM backtest_run ORDER BY id",
    "analysis_run": "SELECT id FROM analysis_run ORDER BY id",
}


class BackupError(RuntimeError):
    """A backup could not be created or verified safely."""


class BackupBusyError(BackupError):
    """The backup barrier could not become quiescent within its bound."""


class BackupValidationError(BackupError):
    """A backup archive is not canonical or internally consistent."""


class RestoreRecoveryRequired(BackupError):
    """Startup cannot safely recover an interrupted component replacement."""


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BackupFile(_Contract):
    archive_path: str = Field(min_length=1, max_length=4096)
    kind: Literal["database", "market_marker", "market_partition"]
    size: int = Field(ge=0, le=_MAX_ARCHIVE_FILE_BYTES)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_relative_path: str | None = Field(default=None, max_length=4096)


class BackupDatasetPartition(_Contract):
    dataset_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    partition_manifest_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    relative_path: str = Field(min_length=1, max_length=4096)
    byte_size: int = Field(gt=0, le=_MAX_ARCHIVE_FILE_BYTES)
    physical_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class BackupLogicalInventory(_Contract):
    table: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    count: int = Field(ge=0)
    identity_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class BackupTaskBarrier(_Contract):
    claim_gate: Literal["held"] = "held"
    running_count: Literal[0] = 0
    queued_count: int = Field(ge=0)
    scheduler_enqueue_policy: Literal["consistent_sqlite_snapshot"] = (
        "consistent_sqlite_snapshot"
    )


class BackupManifest(_Contract):
    schema_version: Literal["stock-desk-backup-v1"] = BACKUP_SCHEMA_VERSION
    created_at: AwareDatetime
    app_version: str = Field(min_length=1, max_length=64)
    schema_revision: str = Field(min_length=1, max_length=128)
    market_layout_version: Literal["v1"] = "v1"
    secret_policy: Literal["omitted", "encrypted_included"]
    master_key_included: Literal[False] = False
    task_barrier: BackupTaskBarrier
    files: tuple[BackupFile, ...] = Field(max_length=_MAX_ARCHIVE_ENTRIES)
    dataset_partitions: tuple[BackupDatasetPartition, ...] = Field(
        max_length=_MAX_ARCHIVE_ENTRIES
    )
    logical_inventory: tuple[BackupLogicalInventory, ...]
    external_tdx_path: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        paths = tuple(item.archive_path for item in self.files)
        if paths != tuple(dict.fromkeys(paths)):
            raise ValueError("backup manifest file paths must be unique")
        if not paths or paths[0] != "database/stock-desk.db":
            raise ValueError("backup manifest must begin with the database")
        if tuple(sorted(paths[1:])) != paths[1:]:
            raise ValueError("backup manifest market paths must be sorted")
        partition_paths = tuple(
            f"market/{item.relative_path}" for item in self.dataset_partitions
        )
        file_partition_paths = tuple(
            item.archive_path for item in self.files if item.kind == "market_partition"
        )
        if partition_paths != file_partition_paths:
            raise ValueError("backup dataset relationships do not match files")
        return self


class _RestoreJournal(_Contract):
    schema_version: Literal["stock-desk-restore-journal-v1"] = (
        "stock-desk-restore-journal-v1"
    )
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    database_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
    phase: Literal[
        "prepared",
        "database_old_moved",
        "database_installed",
        "market_old_moved",
        "market_installed",
        "committed",
    ]
    had_database: bool
    had_market: bool
    database_old_moved: bool = False
    database_installed: bool = False
    market_old_moved: bool = False
    market_installed: bool = False
    archive_manifest_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BackupResult:
    archive: Path
    manifest: BackupManifest


@dataclass(frozen=True, slots=True)
class RestoreResult:
    database: Path
    market: Path | None
    manifest: BackupManifest
    recovery_archive: Path | None


@dataclass(slots=True)
class _HeldFile:
    entry: BackupFile
    descriptor: int

    def close(self) -> None:
        os.close(self.descriptor)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sqlite_path(database_url: str) -> Path:
    parsed = make_url(database_url)
    database = parsed.database
    if (
        parsed.get_backend_name() != "sqlite"
        or database is None
        or database in {"", ":memory:"}
        or database.startswith("file:")
        or parsed.query.get("mode") == "memory"
    ):
        raise BackupValidationError("backup requires a file-backed SQLite database")
    path = Path(unquote(database))
    if not path.is_absolute():
        path = path.resolve()
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as error:
        raise BackupValidationError("backup database does not exist") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BackupValidationError("backup database must be a regular non-link file")
    return path


def _open_regular(path: Path) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise BackupValidationError("backup requires no-follow filesystem access")
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise BackupValidationError("backup input must be a regular non-link file")
    descriptor = os.open(path, os.O_RDONLY | no_follow)
    try:
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise BackupValidationError("backup input identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _copy_database(source_path: Path, clone_path: Path) -> None:
    source = sqlite3.connect(source_path, timeout=5.0)
    clone = sqlite3.connect(clone_path)
    try:
        checkpoint = source.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        if checkpoint is None or len(checkpoint) != 3 or int(checkpoint[0]) != 0:
            raise BackupBusyError("database WAL checkpoint is busy")
        source.backup(clone)
        clone.commit()
    finally:
        clone.close()
        source.close()
    clone_path.chmod(0o600)


def _validate_clone(path: Path, *, include_encrypted_secrets: bool) -> None:
    with sqlite3.connect(path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "delete":
            raise BackupValidationError("backup clone journal mode is unsafe")
        if not include_encrypted_secrets:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("DELETE FROM app_setting WHERE key LIKE 'secret.%'")
            connection.commit()
            connection.execute("VACUUM")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupValidationError("backup database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise BackupValidationError("backup database foreign keys are invalid")
    if (
        path.with_name(f"{path.name}-wal").exists()
        or path.with_name(f"{path.name}-shm").exists()
    ):
        raise BackupValidationError("backup clone retained SQLite sidecar files")


def _database_rows(
    database: Path,
) -> tuple[
    str,
    tuple[BackupDatasetPartition, ...],
    tuple[BackupLogicalInventory, ...],
    int,
    str | None,
]:
    with sqlite3.connect(database) as connection:
        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        if revision_row is None or type(revision_row[0]) is not str:
            raise BackupValidationError("backup database schema revision is missing")
        partitions = tuple(
            BackupDatasetPartition(
                dataset_version=cast(str, row[0]),
                partition_manifest_id=cast(str, row[1]),
                relative_path=cast(str, row[2]),
                byte_size=cast(int, row[3]),
                physical_sha256=cast(str, row[4]),
            )
            for row in connection.execute(
                "SELECT dataset_version, partition_manifest_id, relative_path, "
                "byte_size, physical_sha256 FROM market_dataset_partition "
                "ORDER BY relative_path"
            )
        )
        inventories: list[BackupLogicalInventory] = []
        existing_tables = {
            cast(str, row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table, identity_query in _INVENTORY_QUERIES.items():
            if table not in existing_tables:
                continue
            primary_keys = tuple(
                cast(str, row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if int(row[5]) > 0
            )
            if not primary_keys:
                raise BackupValidationError("backup inventory table has no identity")
            if primary_keys != ("id",):
                raise BackupValidationError("backup inventory identity changed")
            rows = [
                [str(value) for value in row]
                for row in connection.execute(identity_query)
            ]
            inventories.append(
                BackupLogicalInventory(
                    table=table,
                    count=len(rows),
                    identity_sha256=_sha256_bytes(_canonical_json(rows)),
                )
            )
        queued_count = int(
            connection.execute(
                "SELECT count(*) FROM task_run WHERE status = 'queued'"
            ).fetchone()[0]
        )
        tdx_path: str | None = None
        public_row = connection.execute(
            "SELECT encrypted_value FROM app_setting "
            "WHERE key = 'public.market.source_settings.v1'"
        ).fetchone()
        if public_row is not None and type(public_row[0]) is str:
            try:
                decoded = json.loads(public_row[0])
                candidate = (
                    decoded.get("tdx_path") if isinstance(decoded, dict) else None
                )
                if isinstance(candidate, str):
                    tdx_path = candidate
            except (TypeError, ValueError):
                tdx_path = None
    return (
        revision_row[0],
        partitions,
        tuple(inventories),
        queued_count,
        tdx_path,
    )


def _read_descriptor(descriptor: int) -> Iterator[bytes]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        yield chunk
    os.lseek(descriptor, 0, os.SEEK_SET)


def _held_market_files(
    data_dir: Path,
    partitions: tuple[BackupDatasetPartition, ...],
) -> list[_HeldFile]:
    market_root = (data_dir / "market").resolve()
    if not market_root.exists():
        if partitions:
            raise BackupValidationError("catalog references a missing market lake")
        return []
    marker = market_root / _MARKET_MARKER
    _ownership_marker_stat(marker, require_single_link=True)
    root_descriptor = _open_absolute_root(market_root)
    held: list[_HeldFile] = []
    try:
        marker_descriptor = _open_catalog_leaf(root_descriptor, _MARKET_MARKER)
        marker_metadata = os.fstat(marker_descriptor)
        held.append(
            _HeldFile(
                entry=BackupFile(
                    archive_path=f"market/{_MARKET_MARKER}",
                    kind="market_marker",
                    size=marker_metadata.st_size,
                    sha256=_descriptor_sha256(marker_descriptor),
                    source_relative_path=_MARKET_MARKER,
                ),
                descriptor=marker_descriptor,
            )
        )
        for partition in partitions:
            descriptor = _open_catalog_leaf(root_descriptor, partition.relative_path)
            metadata = os.fstat(descriptor)
            digest = _descriptor_sha256(descriptor)
            if (
                metadata.st_size != partition.byte_size
                or digest != partition.physical_sha256
            ):
                os.close(descriptor)
                raise BackupValidationError(
                    "market partition does not match the database catalog"
                )
            held.append(
                _HeldFile(
                    entry=BackupFile(
                        archive_path=f"market/{partition.relative_path}",
                        kind="market_partition",
                        size=metadata.st_size,
                        sha256=digest,
                        source_relative_path=partition.relative_path,
                    ),
                    descriptor=descriptor,
                )
            )
    except BaseException:
        for item in held:
            item.close()
        raise
    finally:
        os.close(root_descriptor)
    return held


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _write_archive(
    path: Path,
    manifest: BackupManifest,
    held_files: tuple[_HeldFile, ...],
) -> None:
    manifest_bytes = _canonical_json(manifest.model_dump(mode="json"))
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as bundle:
        bundle.writestr(_zip_info("manifest.json"), manifest_bytes)
        bundle.writestr(
            _zip_info("manifest.sha256"),
            (_sha256_bytes(manifest_bytes) + "\n").encode("ascii"),
        )
        for held in held_files:
            with bundle.open(
                _zip_info(held.entry.archive_path),
                mode="w",
                force_zip64=True,
            ) as destination:
                for chunk in _read_descriptor(held.descriptor):
                    destination.write(chunk)
    path.chmod(0o600)


def create_backup(
    *,
    database_url: str,
    data_dir: Path,
    destination: Path,
    include_encrypted_secrets: bool = False,
    drain_timeout_seconds: float = 30.0,
    drain_poll_seconds: float = 0.05,
) -> BackupResult:
    """Create and atomically publish one verified portable backup."""
    if drain_timeout_seconds < 0 or not 0 < drain_poll_seconds <= 1:
        raise ValueError("backup drain bounds are invalid")
    destination = Path(destination)
    if destination.suffix != BACKUP_SUFFIX or destination.exists():
        raise BackupValidationError(
            "backup destination must be a new .stockdesk-backup file"
        )
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir = Path(data_dir).resolve(strict=True)
    source_database = _sqlite_path(database_url)
    temporary_archive = destination.parent / (f".{destination.name}.{uuid4().hex}.tmp")
    held_files: list[_HeldFile] = []
    engine = None
    try:
        with migration_lock(database_url, timeout_seconds=drain_timeout_seconds):
            engine = create_engine_for_url(database_url)
            tasks = TaskRepository(engine)
            with tasks.hold_claim_gate(timeout_seconds=drain_timeout_seconds):
                deadline = time.monotonic() + drain_timeout_seconds
                while tasks.running_task_count() != 0:
                    if time.monotonic() >= deadline:
                        raise BackupBusyError(
                            "backup timed out waiting for running tasks"
                        )
                    time.sleep(drain_poll_seconds)
                with tempfile.TemporaryDirectory(
                    prefix=".stock-desk-backup-work-",
                    dir=destination.parent,
                ) as raw_work:
                    work = Path(raw_work)
                    work.chmod(0o700)
                    clone = work / "stock-desk.db"
                    _copy_database(source_database, clone)
                    _validate_clone(
                        clone,
                        include_encrypted_secrets=include_encrypted_secrets,
                    )
                    (
                        schema_revision,
                        partitions,
                        inventories,
                        queued_count,
                        tdx_path,
                    ) = _database_rows(clone)
                    database_descriptor = _open_regular(clone)
                    database_metadata = os.fstat(database_descriptor)
                    held_files.append(
                        _HeldFile(
                            entry=BackupFile(
                                archive_path="database/stock-desk.db",
                                kind="database",
                                size=database_metadata.st_size,
                                sha256=_descriptor_sha256(database_descriptor),
                            ),
                            descriptor=database_descriptor,
                        )
                    )
                    held_files.extend(_held_market_files(data_dir, partitions))
                    files = tuple(item.entry for item in held_files)
                    manifest = BackupManifest(
                        created_at=datetime.now(timezone.utc),
                        app_version=package_version("stock-desk"),
                        schema_revision=schema_revision,
                        secret_policy=(
                            "encrypted_included"
                            if include_encrypted_secrets
                            else "omitted"
                        ),
                        task_barrier=BackupTaskBarrier(queued_count=queued_count),
                        files=files,
                        dataset_partitions=partitions,
                        logical_inventory=inventories,
                        external_tdx_path=tdx_path,
                    )
                    descriptor = os.open(
                        temporary_archive,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    os.close(descriptor)
                    _write_archive(temporary_archive, manifest, tuple(held_files))
                    verified = inspect_backup(temporary_archive)
                    if verified != manifest:
                        raise BackupValidationError(
                            "published backup manifest changed during verification"
                        )
                    os.replace(temporary_archive, destination)
                    return BackupResult(archive=destination, manifest=manifest)
    except FileLockTimeout as error:
        raise BackupBusyError(
            "backup could not acquire its consistency locks"
        ) from error
    finally:
        for held in held_files:
            held.close()
        if engine is not None:
            engine.dispose()
        try:
            temporary_archive.unlink()
        except FileNotFoundError:
            pass


def _validated_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BackupValidationError("backup archive contains an unsafe path")
    return path


def _hash_zip_member(bundle: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with bundle.open(info) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def inspect_backup(archive: Path) -> BackupManifest:
    """Validate archive structure, limits, canonical manifest, and all file hashes."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if bundle.comment:
                raise BackupValidationError("backup archive comment is not canonical")
            if not 3 <= len(infos) <= _MAX_ARCHIVE_ENTRIES:
                raise BackupValidationError("backup archive entry count is invalid")
            if len(names) != len(set(names)):
                raise BackupValidationError("backup archive contains duplicate entries")
            for info in infos:
                _validated_archive_path(info.filename)
                if info.is_dir() or not 0 <= info.file_size <= _MAX_ARCHIVE_FILE_BYTES:
                    raise BackupValidationError("backup archive entry is invalid")
                file_type = stat.S_IFMT(info.external_attr >> 16)
                if (
                    info.compress_type != zipfile.ZIP_DEFLATED
                    or info.flag_bits & 0x1
                    or info.create_system != 3
                    or file_type != stat.S_IFREG
                    or stat.S_IMODE(info.external_attr >> 16) != 0o600
                    or info.date_time != _FIXED_ZIP_TIME
                    or bool(info.comment)
                    or (
                        info.file_size > 1024 * 1024
                        and info.file_size
                        > max(1, info.compress_size) * _MAX_COMPRESSION_RATIO
                    )
                ):
                    raise BackupValidationError(
                        "backup archive entry encoding is unsafe"
                    )
            if sum(info.file_size for info in infos) > _MAX_ARCHIVE_TOTAL_BYTES:
                raise BackupValidationError("backup archive exceeds the size limit")
            if names[:2] != ["manifest.json", "manifest.sha256"]:
                raise BackupValidationError("backup archive metadata order is invalid")
            manifest_info = infos[0]
            if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise BackupValidationError("backup manifest exceeds the size limit")
            if infos[1].file_size != 72:
                raise BackupValidationError("backup manifest digest size is invalid")
            manifest_bytes = bundle.read(manifest_info)
            digest_bytes = bundle.read(infos[1])
            if digest_bytes != (_sha256_bytes(manifest_bytes) + "\n").encode("ascii"):
                raise BackupValidationError("backup manifest digest is invalid")
            manifest = BackupManifest.model_validate_json(manifest_bytes)
            if _canonical_json(manifest.model_dump(mode="json")) != manifest_bytes:
                raise BackupValidationError("backup manifest is not canonical")
            expected_names = [
                "manifest.json",
                "manifest.sha256",
                *(item.archive_path for item in manifest.files),
            ]
            if names != expected_names:
                raise BackupValidationError("backup entries do not match the manifest")
            by_name = {info.filename: info for info in infos}
            for item in manifest.files:
                info = by_name[item.archive_path]
                if (
                    info.file_size != item.size
                    or _hash_zip_member(bundle, info) != item.sha256
                ):
                    raise BackupValidationError("backup file hash is invalid")
            return manifest
    except BackupValidationError:
        raise
    except (
        EOFError,
        KeyError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise BackupValidationError("backup archive is invalid") from error


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_durably(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


def _journal_path(data_dir: Path) -> Path:
    return data_dir / _RESTORE_JOURNAL


def _journal_bytes(journal: _RestoreJournal) -> bytes:
    payload = journal.model_dump(mode="json")
    payload_bytes = _canonical_json(payload)
    return _canonical_json({"journal": payload, "sha256": _sha256_bytes(payload_bytes)})


def _write_restore_journal(data_dir: Path, journal: _RestoreJournal) -> None:
    path = _journal_path(data_dir)
    temporary = data_dir / f".{_RESTORE_JOURNAL}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = _journal_bytes(journal)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    _replace_durably(temporary, path)


def _read_restore_journal(data_dir: Path) -> _RestoreJournal | None:
    path = _journal_path(data_dir)
    try:
        descriptor = _open_regular(path)
    except FileNotFoundError:
        return None
    except (OSError, BackupValidationError) as error:
        raise RestoreRecoveryRequired(
            "restore journal is not a safe regular file"
        ) from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_MANIFEST_BYTES + 1)
    except OSError as error:
        raise RestoreRecoveryRequired("restore journal cannot be read") from error
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise RestoreRecoveryRequired("restore journal exceeds its size limit")
    try:
        envelope = json.loads(raw)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"journal", "sha256"}
            or not isinstance(envelope["journal"], dict)
            or envelope["sha256"] != _sha256_bytes(_canonical_json(envelope["journal"]))
        ):
            raise ValueError("invalid restore journal envelope")
        journal = _RestoreJournal.model_validate(envelope["journal"])
        if _journal_bytes(journal) != raw:
            raise ValueError("non-canonical restore journal")
        return journal
    except (TypeError, ValueError, ValidationError, UnicodeError) as error:
        raise RestoreRecoveryRequired("restore journal is corrupt") from error


def _restore_database_path(database_url: str, data_dir: Path) -> Path:
    parsed = make_url(database_url)
    database = parsed.database
    if (
        parsed.get_backend_name() != "sqlite"
        or database is None
        or database in {"", ":memory:"}
        or database.startswith("file:")
        or parsed.query.get("mode") == "memory"
    ):
        raise BackupValidationError("restore requires a file-backed SQLite database")
    path = Path(unquote(database))
    if not path.is_absolute():
        path = path.resolve()
    if path.parent.resolve(strict=True) != data_dir:
        raise BackupValidationError("restore database must be directly inside data_dir")
    if path.name in {"", ".", ".."}:
        raise BackupValidationError("restore database filename is invalid")
    if os.path.lexists(path):
        metadata = os.lstat(path)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise BackupValidationError(
                "restore database must be a regular single-link file"
            )
    return path


def _quiesce_offline_database(database: Path) -> None:
    if not database.exists():
        return
    try:
        with sqlite3.connect(database, timeout=0) as connection:
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("BEGIN EXCLUSIVE")
            connection.commit()
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise BackupValidationError("offline restore database WAL is busy")
    except sqlite3.Error as error:
        raise BackupValidationError(
            "offline restore could not obtain exclusive SQLite access"
        ) from error
    for sidecar in (
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        if sidecar.exists():
            metadata = os.lstat(sidecar)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise BackupValidationError("offline restore SQLite sidecar is unsafe")
            sidecar.unlink()
    _fsync_directory(database.parent)


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)


def _private_existing_or_create(path: Path) -> None:
    try:
        _private_directory(path)
        return
    except FileExistsError:
        metadata = os.lstat(path)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BackupValidationError("restore recovery directory is unsafe")


def _private_parents(path: Path, boundary: Path) -> None:
    missing: list[Path] = []
    current = path
    while current != boundary and not current.exists():
        missing.append(current)
        current = current.parent
    if current != boundary and boundary not in current.parents:
        raise BackupValidationError("restore extraction escaped its staging directory")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)


def _extract_verified_archive(
    archive: Path,
    manifest: BackupManifest,
    *,
    new_root: Path,
    database_name: str,
) -> tuple[Path, Path | None]:
    staged_database = new_root / database_name
    staged_market: Path | None = None
    with zipfile.ZipFile(archive) as bundle:
        for item in manifest.files:
            if item.kind == "database":
                destination = staged_database
            else:
                relative = PurePosixPath(item.archive_path).relative_to("market")
                staged_market = new_root / "market"
                destination = staged_market.joinpath(*relative.parts)
            _private_parents(destination.parent, new_root)
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            written = 0
            digest = hashlib.sha256()
            try:
                with bundle.open(item.archive_path) as source:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > item.size:
                            raise BackupValidationError(
                                "backup member exceeded its declared size"
                            )
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            count = os.write(descriptor, view)
                            if count <= 0:
                                raise OSError("restore staging write made no progress")
                            view = view[count:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if written != item.size or f"sha256:{digest.hexdigest()}" != item.sha256:
                raise BackupValidationError("staged backup member hash is invalid")
    return staged_database, staged_market


def _validate_staged_restore(
    *,
    database: Path,
    market: Path | None,
    manifest: BackupManifest,
) -> None:
    _validate_clone(database, include_encrypted_secrets=True)
    revision, partitions, inventories, _queued, _tdx = _database_rows(database)
    if revision != manifest.schema_revision:
        raise BackupValidationError(
            "staged database schema does not match the manifest"
        )
    if (
        partitions != manifest.dataset_partitions
        or inventories != manifest.logical_inventory
    ):
        raise BackupValidationError(
            "staged database inventory does not match the manifest"
        )
    if bool(market) != any(item.kind == "market_marker" for item in manifest.files):
        raise BackupValidationError(
            "staged market component does not match the manifest"
        )

    staged_url = f"sqlite:///{database}"
    try:
        migrate(staged_url)
    except Exception as error:
        raise BackupValidationError("staged database migration failed") from error
    _validate_clone(database, include_encrypted_secrets=True)
    _revision, migrated_partitions, migrated_inventories, _queued, _tdx = (
        _database_rows(database)
    )
    if (
        migrated_partitions != manifest.dataset_partitions
        or migrated_inventories != manifest.logical_inventory
    ):
        raise BackupValidationError("migration changed the backup logical inventory")

    if market is None:
        if migrated_partitions:
            raise BackupValidationError("restored catalog has no market component")
        return
    engine = create_engine_for_url(staged_url)
    try:
        lake = MarketLake(engine=engine, root=market.resolve(strict=True))
        with sqlite3.connect(database) as connection:
            manifest_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT manifest_record_id FROM market_routing_manifest "
                    "ORDER BY manifest_record_id"
                )
            )
        for manifest_id in manifest_ids:
            lake.read(manifest_id)
    except Exception as error:
        raise BackupValidationError("staged MarketLake validation failed") from error
    finally:
        engine.dispose()


def _remove_restore_stage(data_dir: Path, token: str) -> None:
    stage = data_dir / f".stock-desk-restore-{token}"
    if not os.path.lexists(stage):
        return
    metadata = os.lstat(stage)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RestoreRecoveryRequired("restore staging path is unsafe")
    shutil.rmtree(stage)
    _fsync_directory(data_dir)


def _finish_restore_cleanup(data_dir: Path, journal: _RestoreJournal) -> None:
    _remove_restore_stage(data_dir, journal.token)
    path = _journal_path(data_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(data_dir)


def _advance_journal(
    data_dir: Path,
    journal: _RestoreJournal,
    phase: str,
    hook: Callable[[str], None] | None,
    **changes: bool,
) -> _RestoreJournal:
    updated = journal.model_copy(update={"phase": phase, **changes})
    _write_restore_journal(data_dir, updated)
    if hook is not None:
        hook(phase)
    return updated


def restore_backup(
    *,
    archive: Path,
    database_url: str,
    data_dir: Path,
    offline: bool = False,
    _phase_hook: Callable[[str], None] | None = None,
) -> RestoreResult:
    """Verify, stage, migrate, and journal an owned-component restore."""
    archive = Path(archive).resolve(strict=True)
    manifest = inspect_backup(archive)
    data_dir = Path(data_dir)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir = data_dir.resolve(strict=True)
    if _read_restore_journal(data_dir) is not None:
        raise RestoreRecoveryRequired(
            "an unfinished restore journal must be recovered before restoring"
        )
    database = _restore_database_path(database_url, data_dir)
    market = data_dir / "market"
    nonempty = any(data_dir.iterdir())
    if nonempty and not offline:
        raise BackupValidationError(
            "restore into a non-empty destination requires offline"
        )
    had_database = database.exists()
    had_market = market.exists()
    if had_market:
        try:
            _ownership_marker_stat(
                market / _MARKET_MARKER,
                require_single_link=True,
            )
        except (OSError, ValueError) as error:
            raise BackupValidationError(
                "existing market component is not application-owned"
            ) from error
    if had_market and not had_database:
        raise BackupValidationError("existing owned components are incomplete")
    _quiesce_offline_database(database)

    token = uuid4().hex
    recovery_archive: Path | None = None
    if had_database:
        recovery_dir = data_dir / _RECOVERY_DIRECTORY
        _private_existing_or_create(recovery_dir)
        recovery_archive = recovery_dir / f"pre-restore-{token}{BACKUP_SUFFIX}"
        create_backup(
            database_url=database_url,
            data_dir=data_dir,
            destination=recovery_archive,
            include_encrypted_secrets=True,
        )
        _quiesce_offline_database(database)

    stage = data_dir / f".stock-desk-restore-{token}"
    new_root = stage / "new"
    rollback_root = stage / "rollback"
    journal_written = False
    try:
        _private_directory(stage)
        _private_directory(new_root)
        _private_directory(rollback_root)
        staged_database, staged_market = _extract_verified_archive(
            archive,
            manifest,
            new_root=new_root,
            database_name=database.name,
        )
        _validate_staged_restore(
            database=staged_database,
            market=staged_market,
            manifest=manifest,
        )
        journal = _RestoreJournal(
            token=token,
            database_name=database.name,
            phase="prepared",
            had_database=had_database,
            had_market=had_market,
            archive_manifest_sha256=_sha256_bytes(
                _canonical_json(manifest.model_dump(mode="json"))
            ),
        )
        _write_restore_journal(data_dir, journal)
        journal_written = True
        if _phase_hook is not None:
            _phase_hook("prepared")

        if had_database:
            _replace_durably(database, rollback_root / database.name)
            journal = _advance_journal(
                data_dir,
                journal,
                "database_old_moved",
                _phase_hook,
                database_old_moved=True,
            )
        _replace_durably(staged_database, database)
        journal = _advance_journal(
            data_dir,
            journal,
            "database_installed",
            _phase_hook,
            database_installed=True,
        )
        if had_market:
            _replace_durably(market, rollback_root / "market")
            journal = _advance_journal(
                data_dir,
                journal,
                "market_old_moved",
                _phase_hook,
                market_old_moved=True,
            )
        if staged_market is not None:
            _replace_durably(staged_market, market)
            journal = _advance_journal(
                data_dir,
                journal,
                "market_installed",
                _phase_hook,
                market_installed=True,
            )
        journal = _advance_journal(
            data_dir,
            journal,
            "committed",
            _phase_hook,
        )
        _finish_restore_cleanup(data_dir, journal)
        return RestoreResult(
            database=database,
            market=market if staged_market is not None else None,
            manifest=manifest,
            recovery_archive=recovery_archive,
        )
    finally:
        if not journal_written:
            try:
                _remove_restore_stage(data_dir, token)
            except RestoreRecoveryRequired:
                pass


def _recovery_paths(
    data_dir: Path, journal: _RestoreJournal
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    stage = data_dir / f".stock-desk-restore-{journal.token}"
    rollback = stage / "rollback"
    new = stage / "new"
    database = data_dir / journal.database_name
    return (
        stage,
        database,
        data_dir / "market",
        rollback / journal.database_name,
        rollback / "market",
        new / journal.database_name,
        new / "market",
    )


def recover_interrupted_restore(*, data_dir: Path) -> bool:
    """Roll back an unfinished restore, or finish cleanup after commit."""
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return False
    data_dir = data_dir.resolve(strict=True)
    journal = _read_restore_journal(data_dir)
    if journal is None:
        return False
    (
        stage,
        database,
        market,
        old_database,
        old_market,
        staged_database,
        staged_market,
    ) = _recovery_paths(data_dir, journal)
    if journal.phase == "committed":
        _finish_restore_cleanup(data_dir, journal)
        return True
    if not stage.is_dir() or stage.is_symlink():
        raise RestoreRecoveryRequired("restore journal staging directory is missing")

    # Reconcile a crash between an atomic rename and the following journal fsync.
    if not journal.database_old_moved and old_database.exists():
        if journal.had_database and not database.exists():
            journal = journal.model_copy(update={"database_old_moved": True})
        elif (
            journal.had_database and database.exists() and not staged_database.exists()
        ):
            journal = journal.model_copy(
                update={"database_old_moved": True, "database_installed": True}
            )
        else:
            raise RestoreRecoveryRequired("restore database state is ambiguous")
        _write_restore_journal(data_dir, journal)
    if (
        not journal.database_installed
        and database.exists()
        and not staged_database.exists()
        and (journal.database_old_moved or not journal.had_database)
    ):
        journal = journal.model_copy(update={"database_installed": True})
        _write_restore_journal(data_dir, journal)
    if not journal.market_old_moved and old_market.exists():
        if journal.had_market and not market.exists():
            journal = journal.model_copy(update={"market_old_moved": True})
        elif journal.had_market and market.exists() and not staged_market.exists():
            journal = journal.model_copy(
                update={"market_old_moved": True, "market_installed": True}
            )
        else:
            raise RestoreRecoveryRequired("restore market state is ambiguous")
        _write_restore_journal(data_dir, journal)
    if (
        not journal.market_installed
        and market.exists()
        and not staged_market.exists()
        and (journal.market_old_moved or not journal.had_market)
    ):
        journal = journal.model_copy(update={"market_installed": True})
        _write_restore_journal(data_dir, journal)

    if journal.market_installed:
        discarded_market = stage / "discarded-market"
        if market.exists():
            if discarded_market.exists():
                raise RestoreRecoveryRequired("restore market rollback is ambiguous")
            _replace_durably(market, discarded_market)
        elif not discarded_market.exists():
            raise RestoreRecoveryRequired("installed restore market is missing")
        journal = journal.model_copy(update={"market_installed": False})
        _write_restore_journal(data_dir, journal)
    if journal.market_old_moved:
        if old_market.exists():
            if market.exists():
                raise RestoreRecoveryRequired(
                    "original restore market target is occupied"
                )
            _replace_durably(old_market, market)
        elif not market.exists():
            raise RestoreRecoveryRequired("original restore market is missing")
        journal = journal.model_copy(update={"market_old_moved": False})
        _write_restore_journal(data_dir, journal)
    if journal.database_installed:
        discarded_database = stage / "discarded-database"
        if database.exists():
            if discarded_database.exists():
                raise RestoreRecoveryRequired("restore database rollback is ambiguous")
            _replace_durably(database, discarded_database)
        elif not discarded_database.exists():
            raise RestoreRecoveryRequired("installed restore database is missing")
        journal = journal.model_copy(update={"database_installed": False})
        _write_restore_journal(data_dir, journal)
    if journal.database_old_moved:
        if old_database.exists():
            if database.exists():
                raise RestoreRecoveryRequired(
                    "original restore database target is occupied"
                )
            _replace_durably(old_database, database)
        elif not database.exists():
            raise RestoreRecoveryRequired("original restore database is missing")
        journal = journal.model_copy(update={"database_old_moved": False})
        _write_restore_journal(data_dir, journal)

    _finish_restore_cleanup(data_dir, journal)
    return True
