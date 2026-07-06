from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Annotated, Final, cast
from uuid import uuid4

import duckdb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from sqlalchemy import Engine, and_, insert, select
from sqlalchemy.engine import Connection, RowMapping

from stock_desk.market.calendar import MARKET_TIMEZONE
from stock_desk.market.partitions import (
    PartitionKey,
    partition_manifest_id,
    partition_path,
)
from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
    RoutingManifest,
    make_routing_manifest,
)
from stock_desk.market.providers.normalization import (
    dataset_version as provider_dataset_version,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    CanonicalSymbol,
    MAX_BAR_SERIES_ROWS,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)
from stock_desk.storage.models import (
    MarketDataset,
    MarketDatasetPartition,
    MarketRoutingManifest,
)
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
)


Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]
_PARQUET_SCHEMA: Final[tuple[tuple[str, str], ...]] = (
    ("symbol", "VARCHAR"),
    ("timestamp", "TIMESTAMP WITH TIME ZONE"),
    ("period", "VARCHAR"),
    ("adjustment", "VARCHAR"),
    ("status", "VARCHAR"),
    ("open", "DECIMAL(24,8)"),
    ("high", "DECIMAL(24,8)"),
    ("low", "DECIMAL(24,8)"),
    ("close", "DECIMAL(24,8)"),
    ("volume", "BIGINT"),
)
_CREATE_BAR_TABLE: Final[str] = """
CREATE TABLE market_bars (
    symbol VARCHAR NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    period VARCHAR NOT NULL,
    adjustment VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    open DECIMAL(24,8) NOT NULL,
    high DECIMAL(24,8) NOT NULL,
    low DECIMAL(24,8) NOT NULL,
    close DECIMAL(24,8) NOT NULL,
    volume BIGINT NOT NULL
)
"""
_INSERT_BAR: Final[str] = """
INSERT INTO market_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_FILESYSTEM_ROOT: Final[Path] = Path(os.sep)
_DANGEROUS_ROOTS: Final[frozenset[Path]] = frozenset(
    {
        _FILESYSTEM_ROOT,
        _FILESYSTEM_ROOT / "tmp",
        _FILESYSTEM_ROOT / "private" / "tmp",
    }
)
_OWNERSHIP_MARKER_NAME: Final[str] = ".stock-desk-market-lake"
_OWNERSHIP_MARKER_CONTENT: Final[bytes] = b"stock-desk-market-lake-v1\n"
_OWNERSHIP_TEMP_PREFIX: Final[str] = f"{_OWNERSHIP_MARKER_NAME}.init-"
_OWNERSHIP_TEMP_SUFFIX: Final[str] = ".tmp"
_SYMBOL_ADAPTER = TypeAdapter(CanonicalSymbol)


class _FrozenLakeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class StoredPartition(_FrozenLakeModel):
    partition_manifest_id: Sha256Digest
    dataset_version: Sha256Digest
    year: int
    relative_path: str
    row_count: Annotated[int, Field(ge=1, le=MAX_BAR_SERIES_ROWS)]
    byte_size: int
    physical_sha256: Sha256Digest


class StoredRoutingManifest(_FrozenLakeModel):
    manifest_record_id: Sha256Digest
    dataset_version: Sha256Digest
    route_version: Sha256Digest
    fetched_at: datetime
    partitions: tuple[StoredPartition, ...]


class MarketLakeError(RuntimeError):
    """Base class for externally distinguishable market-lake read failures."""


class MarketLakeNotFoundError(MarketLakeError):
    """The requested routing manifest is not present in the catalog."""


class MarketLakeCorruptionError(MarketLakeError):
    """Catalog metadata or a referenced immutable object failed validation."""


class _IntegrityValidationError(ValueError):
    """Untrusted catalog or object data failed an explicit integrity check."""


@dataclass(frozen=True)
class _CatalogSnapshot:
    manifest: RowMapping
    dataset: RowMapping
    partitions: tuple[RowMapping, ...]


@dataclass(frozen=True)
class _HeldCatalogObject:
    descriptor: int
    initial_stat: os.stat_result


@dataclass(frozen=True)
class _OperationContext:
    root_descriptor: int
    locks_descriptor: int


@dataclass(frozen=True)
class _DatasetLock:
    descriptor: int
    name: str
    device: int
    inode: int


@dataclass(frozen=True)
class _PublishedPartition:
    stored: StoredPartition
    expected_bars: tuple[Bar, ...]
    parent_descriptor: int
    target_name: str
    device: int
    inode: int
    created: bool


def manifest_record_id(manifest: RoutingManifest) -> str:
    canonical = RoutingManifest.model_validate(manifest.model_dump(mode="python"))
    encoded = json.dumps(
        canonical.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _secure_regular_stat(path: Path) -> os.stat_result:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("market lake object cannot be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("market lake object must be a regular file")
    if metadata.st_nlink != 1:
        raise ValueError("market lake object cannot be a hard link")
    return metadata


def _secure_read_descriptor(path: Path) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise ValueError("market lake requires POSIX no-follow file access")
    descriptor = os.open(path, os.O_RDONLY | no_follow)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("market lake object must be an unlinked regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _physical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = _secure_read_descriptor(path)
    with os.fdopen(descriptor, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _fsync_file(path: Path) -> None:
    descriptor = _secure_read_descriptor(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if no_follow == 0 or directory_only == 0:
        raise ValueError("market lake requires POSIX no-follow directory access")
    descriptor = os.open(path, os.O_RDONLY | no_follow | directory_only)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("market lake fsync target must be a directory")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_descriptor(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise ValueError("market lake fsync target must be a directory")
    os.fsync(descriptor)


def _is_ownership_temp(path: Path) -> bool:
    name = path.name
    if not name.startswith(_OWNERSHIP_TEMP_PREFIX) or not name.endswith(
        _OWNERSHIP_TEMP_SUFFIX
    ):
        return False
    token = name[len(_OWNERSHIP_TEMP_PREFIX) : -len(_OWNERSHIP_TEMP_SUFFIX)]
    return len(token) == 32 and all(
        character in "0123456789abcdef" for character in token
    )


def _ownership_marker_stat(path: Path, *, require_single_link: bool) -> os.stat_result:
    try:
        before = os.lstat(path)
    except FileNotFoundError as error:
        raise ValueError("market lake ownership marker is missing") from error
    if stat.S_ISLNK(before.st_mode):
        raise ValueError("market lake ownership marker cannot be a symbolic link")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("market lake ownership marker must be a regular file")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise ValueError("market lake ownership marker must have mode 0600")

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise ValueError("market lake requires POSIX no-follow file access")
    descriptor = os.open(path, os.O_RDONLY | no_follow)
    try:
        opened = os.fstat(descriptor)
        content = b""
        while len(content) <= len(_OWNERSHIP_MARKER_CONTENT):
            chunk = os.read(
                descriptor,
                len(_OWNERSHIP_MARKER_CONTENT) + 1 - len(content),
            )
            if not chunk:
                break
            content += chunk
    finally:
        os.close(descriptor)

    after = os.lstat(path)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise ValueError("market lake ownership marker changed during validation")
    if content != _OWNERSHIP_MARKER_CONTENT:
        raise ValueError("market lake ownership marker content is invalid")
    if require_single_link and after.st_nlink != 1:
        raise ValueError("market lake ownership marker cannot be a hard link")
    return after


def _ownership_temps(root: Path) -> tuple[Path, ...]:
    return tuple(entry for entry in root.iterdir() if _is_ownership_temp(entry))


def _recover_ownership_marker(root: Path, marker: Path) -> None:
    marker_metadata = _ownership_marker_stat(marker, require_single_link=False)
    temporary_markers = _ownership_temps(root)
    linked_temporary_markers: list[Path] = []
    independent_temporary_markers: list[Path] = []
    for temporary_marker in temporary_markers:
        temporary_metadata = _ownership_marker_stat(
            temporary_marker,
            require_single_link=False,
        )
        if (temporary_metadata.st_dev, temporary_metadata.st_ino) == (
            marker_metadata.st_dev,
            marker_metadata.st_ino,
        ):
            linked_temporary_markers.append(temporary_marker)
        else:
            if temporary_metadata.st_nlink != 1:
                raise ValueError(
                    "market lake ownership marker temporary file is hard linked"
                )
            independent_temporary_markers.append(temporary_marker)

    if marker_metadata.st_nlink != 1 + len(linked_temporary_markers):
        raise ValueError("market lake ownership marker cannot be a hard link")

    removed = False
    for temporary_marker in (
        *linked_temporary_markers,
        *independent_temporary_markers,
    ):
        try:
            os.unlink(temporary_marker)
        except FileNotFoundError:
            continue
        removed = True
    if removed:
        _fsync_directory(root)
    _ownership_marker_stat(marker, require_single_link=True)


def _write_ownership_temp(path: Path) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise ValueError("market lake requires POSIX no-follow file access")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(_OWNERSHIP_MARKER_CONTENT)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("failed to write market lake ownership marker")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    os.close(descriptor)
    _ownership_marker_stat(path, require_single_link=True)


def _publish_ownership_marker(root: Path, marker: Path) -> None:
    temporary_marker = root / (
        f"{_OWNERSHIP_TEMP_PREFIX}{uuid4().hex}{_OWNERSHIP_TEMP_SUFFIX}"
    )
    _write_ownership_temp(temporary_marker)
    try:
        os.link(temporary_marker, marker, follow_symlinks=False)
        os.unlink(temporary_marker)
        _fsync_directory(root)
    except FileExistsError:
        try:
            os.unlink(temporary_marker)
        except FileNotFoundError:
            pass
        _fsync_directory(root)
        _recover_ownership_marker(root, marker)
        return
    except Exception:
        try:
            os.unlink(temporary_marker)
        except FileNotFoundError:
            pass
        _fsync_directory(root)
        raise
    _ownership_marker_stat(marker, require_single_link=True)


def _initialize_ownership_marker(root: Path) -> None:
    import fcntl

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if no_follow == 0 or directory_only == 0:
        raise ValueError("market lake requires POSIX no-follow directory access")
    descriptor = os.open(root, os.O_RDONLY | no_follow | directory_only)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        marker = root / _OWNERSHIP_MARKER_NAME
        if _lexists(marker):
            _recover_ownership_marker(root, marker)
            return

        entries = tuple(root.iterdir())
        if entries and not all(_is_ownership_temp(entry) for entry in entries):
            raise ValueError(
                "existing nonempty market lake root has no ownership marker"
            )
        for entry in entries:
            _ownership_marker_stat(entry, require_single_link=True)
            os.unlink(entry)
        if entries:
            _fsync_directory(root)
        _publish_ownership_marker(root, marker)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _assert_directory(path: Path, *, private: bool) -> None:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("market lake directory cannot be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("market lake path must be a directory")
    if private and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("existing market lake directory must be private")


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for segment in path.parts[1:]:
        current /= segment
        if not _lexists(current):
            break
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("market lake path cannot contain a symlink")


def _create_dedicated_root(path: Path) -> None:
    current = Path(path.anchor)
    for segment in path.parts[1:]:
        current /= segment
        if _lexists(current):
            _assert_directory(current, private=False)
            continue
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            _assert_directory(current, private=False)
            continue
        os.chmod(current, 0o700)
        _fsync_directory(current.parent)
    _assert_directory(path, private=True)


def _mkdir_private(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError("market lake directory escaped its root") from error
    _assert_directory(root, private=True)
    current = root
    for segment in relative.parts:
        current /= segment
        if _lexists(current):
            _assert_directory(current, private=True)
            continue
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            _assert_directory(current, private=True)
            continue
        os.chmod(current, 0o700)
        _fsync_directory(current.parent)


def _open_private_chain(
    root_descriptor: int,
    relative: PurePosixPath,
    *,
    create: bool,
) -> int:
    current = os.dup(root_descriptor)
    try:
        metadata = os.fstat(current)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise _IntegrityValidationError("market lake root descriptor is invalid")
        for component in relative.parts:
            created = False
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    pass
            child = _open_private_directory(Path(component), dir_fd=current)
            if created:
                _fsync_directory_descriptor(current)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    return current


def _bar_parameters(bar: Bar) -> tuple[object, ...]:
    return (
        bar.symbol,
        bar.timestamp,
        bar.period.value,
        bar.adjustment.value,
        bar.status.value,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
    )


def _row_to_bar(row: tuple[object, ...]) -> Bar:
    timestamp_us = row[1]
    volume = row[9]
    if type(timestamp_us) is not int or type(volume) is not int:
        raise ValueError("persisted partition integer value is invalid")
    if not all(type(value) is Decimal for value in row[5:9]):
        raise ValueError("persisted partition decimal value is invalid")
    prices = cast(tuple[Decimal, ...], row[5:9])
    timestamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        microseconds=timestamp_us
    )
    return Bar(
        symbol=str(row[0]),
        timestamp=timestamp,
        period=Period(str(row[2])),
        adjustment=Adjustment(str(row[3])),
        status=TradingStatus(str(row[4])),
        open=prices[0],
        high=prices[1],
        low=prices[2],
        close=prices[3],
        volume=volume,
    )


def _validate_parquet(path: Path, expected_bars: tuple[Bar, ...]) -> None:
    before = _secure_regular_stat(path)
    with duckdb.connect(":memory:") as connection:
        description = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning = false)",
                [str(path)],
            ).fetchall()
        )
        if description != _PARQUET_SCHEMA:
            raise ValueError(f"persisted partition schema is invalid: {description!r}")
        rows = connection.execute(
            "SELECT symbol, epoch_us(timestamp), period, adjustment, status, "
            '"open", high, low, "close", volume '
            "FROM read_parquet(?, hive_partitioning = false) ORDER BY timestamp",
            [str(path)],
        ).fetchall()
    bars = tuple(_row_to_bar(tuple(row)) for row in rows)
    if bars != expected_bars:
        raise ValueError("persisted partition content is invalid")
    after = _secure_regular_stat(path)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("persisted partition changed during validation")


def _same_instant(stored: datetime, expected: datetime) -> bool:
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return stored.astimezone(timezone.utc) == expected.astimezone(timezone.utc)


def _catalog_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise _IntegrityValidationError("market lake catalog datetime is invalid")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dataset_lock_name(dataset_version: object) -> str:
    if not isinstance(dataset_version, str):
        raise _IntegrityValidationError(
            "market lake catalog dataset version is invalid"
        )
    prefix = "sha256:"
    digest = dataset_version.removeprefix(prefix)
    if (
        not dataset_version.startswith(prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise _IntegrityValidationError(
            "market lake catalog dataset version is invalid"
        )
    return f"{digest}.lock"


def _acquire_namespace_guard(locks_descriptor: int) -> None:
    import fcntl

    fcntl.flock(locks_descriptor, fcntl.LOCK_EX)
    metadata = os.fstat(locks_descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        fcntl.flock(locks_descriptor, fcntl.LOCK_UN)
        raise _IntegrityValidationError("lock namespace guard is invalid")


def _release_namespace_guard(locks_descriptor: int) -> None:
    import fcntl

    fcntl.flock(locks_descriptor, fcntl.LOCK_UN)


def _verify_dataset_lock_binding(
    locks_descriptor: int,
    dataset_lock: _DatasetLock,
) -> None:
    held = os.fstat(dataset_lock.descriptor)
    if (
        not stat.S_ISREG(held.st_mode)
        or stat.S_IMODE(held.st_mode) != 0o600
        or held.st_nlink != 1
        or (held.st_dev, held.st_ino) != (dataset_lock.device, dataset_lock.inode)
    ):
        raise _IntegrityValidationError("dataset lock object changed while held")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise _IntegrityValidationError(
            "market lake requires POSIX no-follow file access"
        )
    rebound = os.open(
        dataset_lock.name,
        os.O_RDWR | no_follow,
        dir_fd=locks_descriptor,
    )
    try:
        current = os.fstat(rebound)
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino)
            != (dataset_lock.device, dataset_lock.inode)
        ):
            raise _IntegrityValidationError("dataset lock name changed while held")
    finally:
        os.close(rebound)


def _acquire_dataset_lock(
    locks_descriptor: int,
    dataset_version: object,
) -> _DatasetLock:
    import fcntl

    name = _dataset_lock_name(dataset_version)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise _IntegrityValidationError(
            "market lake requires POSIX no-follow file access"
        )
    flags = os.O_RDWR | no_follow
    created = False
    try:
        descriptor = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=locks_descriptor,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(name, flags, dir_fd=locks_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _IntegrityValidationError("dataset lock object is invalid")
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(locks_descriptor)
        elif stat.S_IMODE(metadata.st_mode) != 0o600:
            raise _IntegrityValidationError("dataset lock object must have mode 0600")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(locked_metadata.st_mode)
            or locked_metadata.st_nlink != 1
            or stat.S_IMODE(locked_metadata.st_mode) != 0o600
        ):
            raise _IntegrityValidationError("dataset lock object changed while locking")
        dataset_lock = _DatasetLock(
            descriptor=descriptor,
            name=name,
            device=locked_metadata.st_dev,
            inode=locked_metadata.st_ino,
        )
        _verify_dataset_lock_binding(locks_descriptor, dataset_lock)
    except BaseException:
        os.close(descriptor)
        raise
    return dataset_lock


def _release_dataset_lock(dataset_lock: _DatasetLock) -> None:
    import fcntl

    try:
        fcntl.flock(dataset_lock.descriptor, fcntl.LOCK_UN)
    finally:
        os.close(dataset_lock.descriptor)


def _raise_collected_errors(
    message: str,
    errors: Sequence[BaseException],
) -> None:
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup(message, list(errors))


def _catalog_relative_components(relative_path: object) -> tuple[Path, ...]:
    if not isinstance(relative_path, str) or not relative_path:
        raise _IntegrityValidationError("market lake catalog path must be nonempty")
    if "\x00" in relative_path or "\\" in relative_path or ":" in relative_path:
        raise _IntegrityValidationError(
            "market lake catalog path is not canonical POSIX"
        )
    canonical = PurePosixPath(relative_path)
    if (
        canonical.is_absolute()
        or canonical.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in canonical.parts)
    ):
        raise _IntegrityValidationError(
            "market lake catalog path is not canonical POSIX"
        )
    return tuple(Path(part) for part in canonical.parts)


def _open_directory(
    path: Path,
    *,
    dir_fd: int | None = None,
    private: bool,
) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if no_follow == 0 or directory_only == 0:
        raise _IntegrityValidationError(
            "market lake requires POSIX no-follow directory access"
        )
    entry_metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    if stat.S_ISLNK(entry_metadata.st_mode):
        raise _IntegrityValidationError(
            "market lake catalog ancestor cannot be a symlink"
        )
    if not stat.S_ISDIR(entry_metadata.st_mode):
        raise _IntegrityValidationError(
            "market lake catalog ancestor must be a directory"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | directory_only,
            dir_fd=dir_fd,
        )
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _IntegrityValidationError(
                "market lake catalog ancestor cannot be a symlink"
            ) from error
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise _IntegrityValidationError(
                "market lake catalog ancestor must be a directory"
            )
        if private and stat.S_IMODE(metadata.st_mode) != 0o700:
            raise _IntegrityValidationError(
                "market lake catalog ancestor must be private"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_private_directory(path: Path, *, dir_fd: int | None = None) -> int:
    return _open_directory(path, dir_fd=dir_fd, private=True)


def _open_absolute_root(path: Path) -> int:
    if not path.is_absolute() or path == Path(path.anchor):
        raise _IntegrityValidationError("market lake root path is invalid")
    descriptor = _open_directory(Path(path.anchor), private=False)
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            child = _open_directory(
                Path(component),
                dir_fd=descriptor,
                private=index == len(components) - 1,
            )
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_catalog_leaf(root_descriptor: int, relative_path: object) -> int:
    components = _catalog_relative_components(relative_path)
    if not components:
        raise _IntegrityValidationError("market lake catalog path must name an object")
    directory_descriptor = os.dup(root_descriptor)
    root_metadata = os.fstat(directory_descriptor)
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        os.close(directory_descriptor)
        raise _IntegrityValidationError("market lake root descriptor is invalid")
    try:
        for component in components[:-1]:
            child_descriptor = _open_private_directory(
                component,
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if no_follow == 0:
            raise _IntegrityValidationError(
                "market lake requires POSIX no-follow file access"
            )
        leaf_metadata = os.stat(
            components[-1],
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(leaf_metadata.st_mode):
            raise _IntegrityValidationError(
                "market lake catalog object cannot be a symlink"
            )
        try:
            leaf_descriptor = os.open(
                components[-1],
                os.O_RDONLY | no_follow,
                dir_fd=directory_descriptor,
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise _IntegrityValidationError(
                    "market lake catalog object cannot be a symlink"
                ) from error
            raise
        try:
            metadata = os.fstat(leaf_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise _IntegrityValidationError(
                    "market lake catalog object must be a regular file"
                )
            if metadata.st_nlink != 1:
                raise _IntegrityValidationError(
                    "market lake catalog object cannot be a hard link"
                )
        except BaseException:
            os.close(leaf_descriptor)
            raise
    finally:
        os.close(directory_descriptor)
    return leaf_descriptor


def _catalog_object_exists(root_descriptor: int, relative_path: str) -> bool:
    components = _catalog_relative_components(relative_path)
    parent = PurePosixPath(*(component.name for component in components[:-1]))
    try:
        directory_descriptor = _open_private_chain(
            root_descriptor,
            parent,
            create=False,
        )
    except FileNotFoundError:
        return False
    try:
        try:
            os.stat(
                components[-1],
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(directory_descriptor)


def _descriptor_path(descriptor: int, expected: os.stat_result) -> Path:
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        candidate = directory / str(descriptor)
        try:
            metadata = os.stat(candidate)
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_ino == expected.st_ino
            and metadata.st_size == expected.st_size
        ):
            return candidate
    raise _IntegrityValidationError(
        "market lake cannot expose a held descriptor to DuckDB"
    )


def _open_held_catalog_object(
    root_descriptor: int,
    relative_path: object,
) -> _HeldCatalogObject:
    descriptor = _open_catalog_leaf(root_descriptor, relative_path)
    try:
        initial_stat = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return _HeldCatalogObject(
        descriptor=descriptor,
        initial_stat=initial_stat,
    )


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return f"sha256:{digest.hexdigest()}"


def _copy_and_hash_descriptor(source: int, destination: int) -> str:
    digest = hashlib.sha256()
    os.lseek(source, 0, os.SEEK_SET)
    os.lseek(destination, 0, os.SEEK_SET)
    os.ftruncate(destination, 0)
    while chunk := os.read(source, 1024 * 1024):
        digest.update(chunk)
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(destination, remaining)
            if written <= 0:
                raise OSError("failed to snapshot market partition")
            remaining = remaining[written:]
    os.fsync(destination)
    os.lseek(source, 0, os.SEEK_SET)
    os.lseek(destination, 0, os.SEEK_SET)
    return f"sha256:{digest.hexdigest()}"


def _open_read_only_snapshot(source: int) -> tuple[int, str]:
    import fcntl

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if no_follow == 0 or directory_only == 0:
        raise _IntegrityValidationError(
            "market lake requires POSIX no-follow file access"
        )
    temporary_parent = Path(tempfile.gettempdir())
    parent_descriptor: int | None = None
    directory_name = f"stock-desk-read-snapshot-{uuid4().hex}"
    directory_metadata: os.stat_result | None = None
    directory_descriptor: int | None = None
    write_descriptor: int | None = None
    read_descriptor: int | None = None
    copied_hash: str | None = None
    written: os.stat_result | None = None
    snapshot_name = Path("snapshot.parquet")
    errors: list[BaseException] = []
    try:
        parent_descriptor = _open_directory(temporary_parent, private=False)
        os.mkdir(directory_name, 0o700, dir_fd=parent_descriptor)
        directory_metadata = os.stat(
            directory_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _fsync_directory_descriptor(parent_descriptor)
        directory_descriptor = _open_private_directory(
            Path(directory_name),
            dir_fd=parent_descriptor,
        )
        try:
            write_descriptor = os.open(
                snapshot_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | no_follow,
                0o600,
                dir_fd=directory_descriptor,
            )
            os.fchmod(write_descriptor, 0o600)
            copied_hash = _copy_and_hash_descriptor(source, write_descriptor)
            os.fchmod(write_descriptor, 0o400)
            os.fsync(write_descriptor)
            written = os.fstat(write_descriptor)
        except BaseException as error:
            errors.append(error)
        if write_descriptor is not None:
            try:
                os.close(write_descriptor)
            except BaseException as error:
                errors.append(error)
            write_descriptor = None
        if not errors and written is not None:
            read_descriptor = os.open(
                snapshot_name,
                os.O_RDONLY | no_follow,
                dir_fd=directory_descriptor,
            )
            reopened = os.fstat(read_descriptor)
            if (
                not stat.S_ISREG(reopened.st_mode)
                or stat.S_IMODE(reopened.st_mode) != 0o400
                or reopened.st_nlink != 1
                or (reopened.st_dev, reopened.st_ino)
                != (written.st_dev, written.st_ino)
            ):
                raise _IntegrityValidationError(
                    "market partition snapshot could not be reopened read-only"
                )
            descriptor_flags = fcntl.fcntl(read_descriptor, fcntl.F_GETFL)
            if descriptor_flags & os.O_ACCMODE != os.O_RDONLY:
                raise _IntegrityValidationError(
                    "market partition snapshot descriptor is writable"
                )
            os.unlink(snapshot_name, dir_fd=directory_descriptor)
            _fsync_directory_descriptor(directory_descriptor)
            unlinked = os.fstat(read_descriptor)
            if (
                unlinked.st_nlink != 0
                or stat.S_IMODE(unlinked.st_mode) != 0o400
                or (unlinked.st_dev, unlinked.st_ino)
                != (written.st_dev, written.st_ino)
            ):
                raise _IntegrityValidationError(
                    "market partition snapshot was not unlinked securely"
                )
    except BaseException as error:
        errors.append(error)

    if directory_descriptor is not None:
        for attempt in range(2):
            try:
                os.unlink(snapshot_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                break
            except BaseException as error:
                errors.append(error)
                if attempt == 0:
                    continue
                break
            else:
                try:
                    _fsync_directory_descriptor(directory_descriptor)
                except BaseException as error:
                    errors.append(error)
                break
        try:
            os.close(directory_descriptor)
        except BaseException as error:
            errors.append(error)
        directory_descriptor = None

    if parent_descriptor is not None:
        if directory_metadata is not None:
            try:
                current_directory = os.stat(
                    directory_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            except BaseException as error:
                errors.append(error)
            else:
                if stat.S_ISDIR(current_directory.st_mode) and (
                    current_directory.st_dev,
                    current_directory.st_ino,
                ) == (directory_metadata.st_dev, directory_metadata.st_ino):
                    try:
                        os.rmdir(directory_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
                    except BaseException as error:
                        errors.append(error)
                    else:
                        try:
                            _fsync_directory_descriptor(parent_descriptor)
                        except BaseException as error:
                            errors.append(error)
        try:
            os.close(parent_descriptor)
        except BaseException as error:
            errors.append(error)
        parent_descriptor = None

    if read_descriptor is None or copied_hash is None:
        if not errors:
            errors.append(
                _IntegrityValidationError("market partition snapshot was not opened")
            )
    if errors and read_descriptor is not None:
        try:
            os.close(read_descriptor)
        except BaseException as error:
            errors.append(error)
        read_descriptor = None

    _raise_collected_errors("market partition snapshot cleanup failed", errors)
    if read_descriptor is None or copied_hash is None:
        raise _IntegrityValidationError("market partition snapshot was not opened")
    return read_descriptor, copied_hash


def _open_regular_at(directory_descriptor: int, name: str) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise _IntegrityValidationError(
            "market lake requires POSIX no-follow file access"
        )
    entry_metadata = os.stat(
        name,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )
    if stat.S_ISLNK(entry_metadata.st_mode):
        raise _IntegrityValidationError("market lake object cannot be a symlink")
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | no_follow,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _IntegrityValidationError(
                "market lake object cannot be a symlink"
            ) from error
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _IntegrityValidationError(
                "market lake object must be a regular single-link file"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _recover_partition_publish(
    parent_descriptor: int,
    target_name: str,
    expected_bars: tuple[Bar, ...],
) -> None:
    prefix = f".{target_name}."
    suffix = ".tmp"
    candidates = tuple(
        name
        for name in os.listdir(parent_descriptor)
        if name.startswith(prefix) and name.endswith(suffix)
    )
    pattern = re.compile(rf"{re.escape(prefix)}[0-9a-f]{{32}}{re.escape(suffix)}")
    if any(pattern.fullmatch(name) is None for name in candidates):
        raise _IntegrityValidationError(
            "market lake partition has malformed recovery temp"
        )
    strict_temps = tuple(sorted(candidates))
    try:
        target_metadata = os.stat(
            target_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        for name in strict_temps:
            metadata = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise _IntegrityValidationError(
                    "market lake stale partition temp is invalid"
                )
        for name in strict_temps:
            os.unlink(name, dir_fd=parent_descriptor)
        if strict_temps:
            _fsync_directory_descriptor(parent_descriptor)
        return
    if stat.S_ISLNK(target_metadata.st_mode):
        raise _IntegrityValidationError(
            "market lake partition target cannot be a symlink"
        )
    if (
        not stat.S_ISREG(target_metadata.st_mode)
        or stat.S_IMODE(target_metadata.st_mode) != 0o600
    ):
        raise _IntegrityValidationError("market lake partition target is invalid")
    if target_metadata.st_nlink == 1:
        if strict_temps:
            raise _IntegrityValidationError(
                "market lake partition has unexpected recovery temp"
            )
        return
    if not strict_temps:
        raise _IntegrityValidationError(
            "market lake partition target cannot be a hard link"
        )
    for name in strict_temps:
        metadata = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino)
            != (target_metadata.st_dev, target_metadata.st_ino)
        ):
            raise _IntegrityValidationError(
                "market lake recovery temp does not bind to target"
            )
    expected_links = 1 + len(strict_temps)
    if target_metadata.st_nlink != expected_links:
        raise _IntegrityValidationError(
            "market lake partition target has unexpected hard links"
        )
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise _IntegrityValidationError(
            "market lake requires POSIX no-follow file access"
        )
    descriptor = os.open(
        target_name,
        os.O_RDONLY | no_follow,
        dir_fd=parent_descriptor,
    )
    try:
        held = os.fstat(descriptor)
        if (
            stat.S_IMODE(held.st_mode) != 0o600
            or held.st_nlink != expected_links
            or (held.st_dev, held.st_ino)
            != (target_metadata.st_dev, target_metadata.st_ino)
        ):
            raise _IntegrityValidationError(
                "market lake partition changed during recovery"
            )
        _validate_parquet_descriptor(descriptor, expected_bars)
        validated = os.fstat(descriptor)
        if validated.st_nlink != expected_links or (
            validated.st_dev,
            validated.st_ino,
        ) != (target_metadata.st_dev, target_metadata.st_ino):
            raise _IntegrityValidationError(
                "market lake partition changed during recovery"
            )
        for name in strict_temps:
            os.unlink(name, dir_fd=parent_descriptor)
        _fsync_directory_descriptor(parent_descriptor)
        recovered = os.fstat(descriptor)
        if recovered.st_nlink != 1:
            raise _IntegrityValidationError(
                "market lake partition recovery left hard links"
            )
    finally:
        os.close(descriptor)


def _verify_catalog_binding(
    root_descriptor: int,
    relative_path: object,
    expected: os.stat_result,
) -> None:
    descriptor = _open_catalog_leaf(root_descriptor, relative_path)
    try:
        current = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise _IntegrityValidationError(
            "market lake catalog path changed while it was read"
        )


def _file_signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _read_partition_bars(path: Path, *, max_rows: int) -> tuple[Bar, ...]:
    if (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= MAX_BAR_SERIES_ROWS
    ):
        raise _IntegrityValidationError("market partition row bound is invalid")
    try:
        with duckdb.connect(":memory:") as connection:
            description = tuple(
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning = false)",
                    [str(path)],
                ).fetchall()
            )
            if description != _PARQUET_SCHEMA:
                raise _IntegrityValidationError(
                    f"persisted partition schema is invalid: {description!r}"
                )
            row_count = connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?, hive_partitioning = false)",
                [str(path)],
            ).fetchone()
            if (
                row_count is None
                or type(row_count[0]) is not int
                or row_count[0] > max_rows
            ):
                raise _IntegrityValidationError(
                    "market partition exceeds its validated row bound"
                )
            rows = connection.execute(
                "SELECT symbol, epoch_us(timestamp), period, adjustment, status, "
                '"open", high, low, "close", volume '
                "FROM read_parquet(?, hive_partitioning = false)",
                [str(path)],
            ).fetchall()
    except duckdb.Error as error:
        raise _IntegrityValidationError(
            "market partition could not be parsed by DuckDB"
        ) from error
    try:
        return tuple(_row_to_bar(tuple(row)) for row in rows)
    except ValueError as error:
        raise _IntegrityValidationError(
            "market partition row is not canonical"
        ) from error


def _validate_parquet_descriptor(
    descriptor: int,
    expected_bars: tuple[Bar, ...],
) -> None:
    before = os.fstat(descriptor)
    descriptor_path = _descriptor_path(descriptor, before)
    if not 1 <= len(expected_bars) <= MAX_BAR_SERIES_ROWS:
        raise _IntegrityValidationError("market partition expected rows are invalid")
    bars = _read_partition_bars(descriptor_path, max_rows=len(expected_bars))
    after = os.fstat(descriptor)
    if bars != expected_bars:
        raise ValueError("persisted partition content is invalid")
    if _file_signature(before) != _file_signature(after):
        raise ValueError("persisted partition changed during validation")


class MarketLake:
    def __init__(self, *, engine: Engine, root: Path) -> None:
        if os.name != "posix":
            raise ValueError("market lake requires POSIX filesystem semantics")
        original_root = Path(root)
        if not original_root.is_absolute():
            raise ValueError("market lake root must be absolute")
        requested_root = Path(os.path.abspath(os.fspath(original_root)))
        if requested_root in _DANGEROUS_ROOTS:
            raise ValueError("market lake root must be a dedicated directory")
        _assert_no_symlink_ancestors(requested_root)
        if _lexists(requested_root):
            _assert_directory(requested_root, private=True)
        else:
            _create_dedicated_root(requested_root)
        self._root = requested_root
        _initialize_ownership_marker(self._root)
        self._locks = self._root / ".locks"
        _mkdir_private(self._root, self._locks)
        root_descriptor = _open_absolute_root(self._root)
        try:
            locks_descriptor = _open_private_directory(
                Path(".locks"),
                dir_fd=root_descriptor,
            )
            try:
                root_metadata = os.fstat(root_descriptor)
                locks_metadata = os.fstat(locks_descriptor)
                self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
                self._locks_identity = (
                    locks_metadata.st_dev,
                    locks_metadata.st_ino,
                )
            finally:
                os.close(locks_descriptor)
        finally:
            os.close(root_descriptor)
        self._engine = engine
        try:
            with engine.connect() as connection:
                self._database_identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise MarketLakeCorruptionError(
                "market lake database identity could not be determined"
            ) from error

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    def _validate_database_connection(self, connection: Connection) -> None:
        if connection.closed or connection.engine is not self._engine:
            raise MarketLakeCorruptionError(
                "market lake database connection is not lake-bound"
            )
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise MarketLakeCorruptionError(
                "market lake database identity could not be determined"
            ) from error
        if identity != self._database_identity:
            raise MarketLakeCorruptionError("market lake database identity changed")

    def _checked_connection(self) -> Connection:
        connection = self._engine.connect()
        try:
            self._validate_database_connection(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _checked_begin(self) -> Iterator[Connection]:
        with self._checked_connection() as connection:
            with connection.begin():
                yield connection

    def _open_operation_context(self) -> _OperationContext:
        root_descriptor: int | None = None
        locks_descriptor: int | None = None
        try:
            root_descriptor = _open_absolute_root(self._root)
            root_metadata = os.fstat(root_descriptor)
            if (root_metadata.st_dev, root_metadata.st_ino) != self._root_identity:
                raise _IntegrityValidationError("market lake root identity changed")
            locks_descriptor = _open_private_directory(
                Path(".locks"),
                dir_fd=root_descriptor,
            )
            locks_metadata = os.fstat(locks_descriptor)
            if (locks_metadata.st_dev, locks_metadata.st_ino) != self._locks_identity:
                raise _IntegrityValidationError(
                    "market lake lock directory identity changed"
                )
            return _OperationContext(
                root_descriptor=root_descriptor,
                locks_descriptor=locks_descriptor,
            )
        except (OSError, _IntegrityValidationError) as error:
            if locks_descriptor is not None:
                os.close(locks_descriptor)
            if root_descriptor is not None:
                os.close(root_descriptor)
            raise MarketLakeCorruptionError(
                "market lake root failed identity validation"
            ) from error

    def _verify_operation_context(self, context: _OperationContext) -> None:
        public_root: int | None = None
        public_locks: int | None = None
        try:
            held_root = os.fstat(context.root_descriptor)
            held_locks = os.fstat(context.locks_descriptor)
            if (held_root.st_dev, held_root.st_ino) != self._root_identity:
                raise _IntegrityValidationError("held market lake root changed")
            if (held_locks.st_dev, held_locks.st_ino) != self._locks_identity:
                raise _IntegrityValidationError("held lock directory changed")
            public_root = _open_absolute_root(self._root)
            public_root_metadata = os.fstat(public_root)
            if (
                public_root_metadata.st_dev,
                public_root_metadata.st_ino,
            ) != self._root_identity:
                raise _IntegrityValidationError("public market lake root changed")
            public_locks = _open_private_directory(
                Path(".locks"),
                dir_fd=public_root,
            )
            public_locks_metadata = os.fstat(public_locks)
            if (
                public_locks_metadata.st_dev,
                public_locks_metadata.st_ino,
            ) != self._locks_identity:
                raise _IntegrityValidationError("public lock directory changed")
        except (OSError, _IntegrityValidationError) as error:
            raise MarketLakeCorruptionError(
                "market lake root changed during operation"
            ) from error
        finally:
            if public_locks is not None:
                os.close(public_locks)
            if public_root is not None:
                os.close(public_root)

    @staticmethod
    def _close_operation_context(context: _OperationContext) -> None:
        errors: list[BaseException] = []
        for descriptor in (context.locks_descriptor, context.root_descriptor):
            try:
                os.close(descriptor)
            except BaseException as error:
                errors.append(error)
        _raise_collected_errors("market lake context close failed", errors)

    @staticmethod
    def _verify_dataset_lock(
        context: _OperationContext,
        dataset_lock: _DatasetLock,
    ) -> None:
        try:
            _verify_dataset_lock_binding(
                context.locks_descriptor,
                dataset_lock,
            )
        except (OSError, _IntegrityValidationError) as error:
            raise MarketLakeCorruptionError(
                "dataset lock binding changed during operation"
            ) from error

    def _release_operation_resources(
        self,
        context: _OperationContext,
        dataset_lock: _DatasetLock | None,
        *,
        namespace_guard_acquired: bool,
        verify_dataset_binding: bool,
    ) -> None:
        errors: list[BaseException] = []
        if dataset_lock is not None:
            if verify_dataset_binding:
                try:
                    self._verify_dataset_lock(context, dataset_lock)
                except BaseException as error:
                    errors.append(error)
            try:
                _release_dataset_lock(dataset_lock)
            except BaseException as error:
                errors.append(error)
        if namespace_guard_acquired:
            try:
                _release_namespace_guard(context.locks_descriptor)
            except BaseException as error:
                errors.append(error)
        try:
            self._close_operation_context(context)
        except BaseException as error:
            errors.append(error)
        _raise_collected_errors("market lake operation cleanup failed", errors)

    def write(self, routed: RoutedBarSuccess) -> StoredRoutingManifest:
        canonical = self._validate_routed(routed)
        dataset_version = canonical.result.provenance.dataset_version
        context = self._open_operation_context()
        namespace_guard_acquired = False
        dataset_lock: _DatasetLock | None = None
        try:
            try:
                _acquire_namespace_guard(context.locks_descriptor)
                namespace_guard_acquired = True
                dataset_lock = _acquire_dataset_lock(
                    context.locks_descriptor,
                    dataset_version,
                )
            except (OSError, _IntegrityValidationError) as error:
                raise MarketLakeCorruptionError(
                    "dataset lock object is invalid"
                ) from error
            self._verify_dataset_lock(context, dataset_lock)
            stored = self._write_locked(canonical, context, dataset_lock)
            self._verify_dataset_lock(context, dataset_lock)
            self._verify_operation_context(context)
        except BaseException as primary_error:
            try:
                self._release_operation_resources(
                    context,
                    dataset_lock,
                    namespace_guard_acquired=namespace_guard_acquired,
                    verify_dataset_binding=False,
                )
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "market lake write and cleanup failed",
                    [primary_error, cleanup_error],
                ) from None
            raise
        self._release_operation_resources(
            context,
            dataset_lock,
            namespace_guard_acquired=namespace_guard_acquired,
            verify_dataset_binding=True,
        )
        return stored

    def read(self, manifest_record_id: str) -> RoutedBarSuccess:
        routed, _stored = self._read_validated_record(manifest_record_id)
        return routed

    def latest_exact(self, query: BarQuery) -> StoredRoutingManifest | None:
        canonical = BarQuery.model_validate(query.model_dump(mode="python"))
        record_id = self._latest_record_id(
            symbol=canonical.symbol,
            period=canonical.period,
            adjustment=canonical.adjustment,
            start=canonical.start,
            end=canonical.end,
        )
        if record_id is None:
            return None
        _routed, stored = self._read_validated_record(record_id)
        return stored

    def read_latest_exact(self, query: BarQuery) -> RoutedBarSuccess | None:
        canonical = BarQuery.model_validate(query.model_dump(mode="python"))
        record_id = self._latest_record_id(
            symbol=canonical.symbol,
            period=canonical.period,
            adjustment=canonical.adjustment,
            start=canonical.start,
            end=canonical.end,
        )
        if record_id is None:
            return None
        routed, _stored = self._read_validated_record(record_id)
        return routed

    def read_latest_series(
        self,
        symbol: str,
        period: Period,
        adjustment: Adjustment,
    ) -> RoutedBarSuccess | None:
        canonical_symbol = _SYMBOL_ADAPTER.validate_python(symbol, strict=True)
        if not isinstance(period, Period):
            raise ValueError("market period is invalid")
        if not isinstance(adjustment, Adjustment):
            raise ValueError("market adjustment is invalid")
        record_id = self._latest_record_id(
            symbol=canonical_symbol,
            period=period,
            adjustment=adjustment,
        )
        if record_id is None:
            return None
        routed, _stored = self._read_validated_record(record_id)
        return routed

    def _latest_record_id(
        self,
        *,
        symbol: str,
        period: Period,
        adjustment: Adjustment,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> str | None:
        statement = (
            select(MarketRoutingManifest.manifest_record_id)
            .join(
                MarketDataset,
                and_(
                    MarketRoutingManifest.dataset_version
                    == MarketDataset.dataset_version,
                    MarketRoutingManifest.symbol == MarketDataset.symbol,
                ),
            )
            .where(
                MarketDataset.symbol == symbol,
                MarketDataset.period == period.value,
                MarketDataset.adjustment == adjustment.value,
            )
        )
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValueError("exact cache lookup requires both range bounds")
            statement = statement.where(
                MarketDataset.query_start == start,
                MarketDataset.query_end == end,
            )
        statement = statement.order_by(
            MarketDataset.data_cutoff.desc(),
            MarketRoutingManifest.fetched_at.desc(),
            MarketRoutingManifest.manifest_record_id.desc(),
        ).limit(1)
        with self._checked_connection() as connection:
            return connection.execute(statement).scalar_one_or_none()

    def _read_validated_record(
        self,
        manifest_record_id: str,
    ) -> tuple[RoutedBarSuccess, StoredRoutingManifest]:
        context = self._open_operation_context()
        namespace_guard_acquired = False
        dataset_lock: _DatasetLock | None = None
        try:
            dataset_version = self._resolve_dataset_version(manifest_record_id)
            try:
                _acquire_namespace_guard(context.locks_descriptor)
                namespace_guard_acquired = True
                dataset_lock = _acquire_dataset_lock(
                    context.locks_descriptor,
                    dataset_version,
                )
            except (OSError, _IntegrityValidationError) as error:
                raise MarketLakeCorruptionError(
                    "dataset lock object is invalid"
                ) from error
            try:
                self._verify_dataset_lock(context, dataset_lock)
                snapshot = self._load_catalog_snapshot(
                    manifest_record_id,
                    dataset_version,
                )
                self._verify_dataset_lock(context, dataset_lock)
                routed, stored = self._read_snapshot(
                    snapshot,
                    root_descriptor=context.root_descriptor,
                )
            except _IntegrityValidationError as error:
                raise MarketLakeCorruptionError(
                    "market lake data failed integrity validation"
                ) from error
            self._verify_dataset_lock(context, dataset_lock)
            self._verify_operation_context(context)
        except BaseException as primary_error:
            try:
                self._release_operation_resources(
                    context,
                    dataset_lock,
                    namespace_guard_acquired=namespace_guard_acquired,
                    verify_dataset_binding=False,
                )
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "market lake read and cleanup failed",
                    [primary_error, cleanup_error],
                ) from None
            raise
        self._release_operation_resources(
            context,
            dataset_lock,
            namespace_guard_acquired=namespace_guard_acquired,
            verify_dataset_binding=True,
        )
        return routed, stored

    def _resolve_dataset_version(self, record_id: str) -> object:
        with self._checked_connection() as connection:
            dataset_version = connection.execute(
                select(MarketRoutingManifest.dataset_version).where(
                    MarketRoutingManifest.manifest_record_id == record_id
                )
            ).scalar_one_or_none()
        if dataset_version is None:
            raise MarketLakeNotFoundError("routing manifest was not found")
        return dataset_version

    def _load_catalog_snapshot(
        self,
        record_id: str,
        expected_dataset_version: object,
    ) -> _CatalogSnapshot:
        with self._checked_connection() as connection:
            manifest_result = connection.execute(
                select(MarketRoutingManifest).where(
                    MarketRoutingManifest.manifest_record_id == record_id
                )
            )
            try:
                manifest = manifest_result.mappings().one_or_none()
            except (TypeError, ValueError) as error:
                raise _IntegrityValidationError(
                    "routing manifest catalog row could not be decoded"
                ) from error
            if manifest is None:
                raise MarketLakeNotFoundError("routing manifest was not found")
            if manifest["dataset_version"] != expected_dataset_version:
                raise MarketLakeCorruptionError(
                    "routing manifest changed datasets while acquiring its lock"
                )
            dataset_result = connection.execute(
                select(MarketDataset).where(
                    MarketDataset.dataset_version == expected_dataset_version
                )
            )
            try:
                dataset = dataset_result.mappings().one_or_none()
            except (TypeError, ValueError) as error:
                raise _IntegrityValidationError(
                    "market dataset catalog row could not be decoded"
                ) from error
            if dataset is None:
                raise MarketLakeCorruptionError("routing manifest dataset is missing")
            partition_result = connection.execute(
                select(MarketDatasetPartition)
                .where(
                    MarketDatasetPartition.dataset_version == expected_dataset_version
                )
                .order_by(
                    MarketDatasetPartition.partition_year,
                    MarketDatasetPartition.partition_manifest_id,
                )
            )
            try:
                partitions = tuple(partition_result.mappings().all())
            except (TypeError, ValueError) as error:
                raise _IntegrityValidationError(
                    "market partition catalog rows could not be decoded"
                ) from error
        if not partitions:
            raise MarketLakeCorruptionError("market dataset has no partitions")
        return _CatalogSnapshot(
            manifest=manifest,
            dataset=dataset,
            partitions=partitions,
        )

    def _read_snapshot(
        self,
        snapshot: _CatalogSnapshot,
        *,
        root_descriptor: int,
    ) -> tuple[RoutedBarSuccess, StoredRoutingManifest]:
        dataset = snapshot.dataset
        try:
            source = ProviderId(dataset["source"])
            query = BarQuery(
                symbol=dataset["symbol"],
                period=Period(dataset["period"]),
                adjustment=Adjustment(dataset["adjustment"]),
                start=_catalog_datetime(dataset["query_start"]),
                end=_catalog_datetime(dataset["query_end"]),
            )
        except (ValidationError, ValueError) as error:
            raise _IntegrityValidationError(
                "market dataset query metadata is invalid"
            ) from error
        dataset_version = dataset["dataset_version"]
        _dataset_lock_name(dataset_version)
        dataset_row_count = dataset["row_count"]
        if (
            type(dataset_row_count) is not int
            or not 1 <= dataset_row_count <= MAX_BAR_SERIES_ROWS
        ):
            raise _IntegrityValidationError(
                "market dataset row count exceeds the supported limit"
            )

        bars: list[Bar] = []
        stored_partitions: list[StoredPartition] = []
        partition_row_counts: list[int] = []
        cumulative_rows = 0
        for partition in snapshot.partitions:
            partition_row_count = partition["row_count"]
            if (
                type(partition_row_count) is not int
                or not 1 <= partition_row_count <= MAX_BAR_SERIES_ROWS
            ):
                raise _IntegrityValidationError(
                    "market partition row count exceeds the supported limit"
                )
            cumulative_rows += partition_row_count
            if cumulative_rows > dataset_row_count:
                raise _IntegrityValidationError(
                    "market partition row counts exceed the dataset bound"
                )
            partition_row_counts.append(partition_row_count)
        if cumulative_rows != dataset_row_count:
            raise _IntegrityValidationError(
                "market dataset row count does not match its partitions"
            )
        remaining_rows = dataset_row_count
        for partition, partition_row_count in zip(
            snapshot.partitions,
            partition_row_counts,
            strict=True,
        ):
            partition_bars, stored_partition = self._read_catalog_partition(
                partition,
                source=source,
                query=query,
                dataset_version=dataset_version,
                root_descriptor=root_descriptor,
                expected_row_count=partition_row_count,
                max_rows=remaining_rows,
            )
            bars.extend(partition_bars)
            stored_partitions.append(stored_partition)
            remaining_rows -= partition_row_count
        if remaining_rows != 0:
            raise _IntegrityValidationError(
                "market dataset row count does not match its partitions"
            )
        timestamps = tuple(bar.timestamp for bar in bars)
        if any(
            current <= previous
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        ):
            raise _IntegrityValidationError(
                "market dataset timestamps must be unique and ascending"
            )
        if dataset_row_count != len(bars):
            raise _IntegrityValidationError(
                "market dataset row count does not match its partitions"
            )

        try:
            manifest_json = json.dumps(
                snapshot.manifest["manifest_json"],
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            manifest = RoutingManifest.model_validate_json(manifest_json)
        except (TypeError, ValidationError, ValueError) as error:
            raise _IntegrityValidationError(
                "routing manifest JSON is invalid"
            ) from error
        if not isinstance(manifest.request, BarRoutingRequest):
            raise _IntegrityValidationError(
                "routing manifest does not contain a bar query"
            )
        catalog_fetched_at = _catalog_datetime(snapshot.manifest["fetched_at"])
        if (
            snapshot.manifest["dataset_version"] != dataset_version
            or snapshot.manifest["symbol"] != query.symbol
            or snapshot.manifest["route_version"] != manifest.route_version
            or catalog_fetched_at != manifest.upstream_fetched_at
            or manifest.request.query != query
        ):
            raise _IntegrityValidationError(
                "routing manifest does not match dataset metadata"
            )
        record_id = snapshot.manifest["manifest_record_id"]
        if record_id != manifest_record_id(manifest):
            raise _IntegrityValidationError(
                "routing manifest record identity is invalid"
            )

        try:
            result = BarResult(
                query=query,
                bars=tuple(bars),
                coverage_start=query.start,
                coverage_end=query.end,
                provenance=Provenance(
                    source=source,
                    fetched_at=manifest.upstream_fetched_at,
                    data_cutoff=_catalog_datetime(dataset["data_cutoff"]),
                    adjustment=query.adjustment,
                    dataset_version=dataset_version,
                ),
            )
            routed_envelope = RoutedBarSuccess(result=result, manifest=manifest)
        except (ValidationError, ValueError) as error:
            raise _IntegrityValidationError(
                "market dataset content is inconsistent"
            ) from error
        routed = self._validate_read_routed(routed_envelope)
        try:
            stored = StoredRoutingManifest(
                manifest_record_id=record_id,
                dataset_version=dataset_version,
                route_version=manifest.route_version,
                fetched_at=manifest.upstream_fetched_at,
                partitions=tuple(stored_partitions),
            )
        except ValidationError as error:
            raise _IntegrityValidationError(
                "stored routing manifest metadata is invalid"
            ) from error
        return routed, stored

    def _read_catalog_partition(
        self,
        stored: RowMapping,
        *,
        source: ProviderId,
        query: BarQuery,
        dataset_version: str,
        root_descriptor: int,
        expected_row_count: int,
        max_rows: int,
    ) -> tuple[tuple[Bar, ...], StoredPartition]:
        year = stored["partition_year"]
        if type(year) is not int:
            raise _IntegrityValidationError("market partition year is invalid")
        if (
            type(expected_row_count) is not int
            or type(max_rows) is not int
            or not 1 <= expected_row_count <= max_rows <= MAX_BAR_SERIES_ROWS
            or stored["row_count"] != expected_row_count
        ):
            raise _IntegrityValidationError("market partition row bound is invalid")
        try:
            key = PartitionKey(
                category="bars",
                source=source,
                symbol=query.symbol,
                period=query.period,
                adjustment=query.adjustment,
                year=year,
            )
        except ValidationError as error:
            raise _IntegrityValidationError(
                "market partition identity is invalid"
            ) from error
        expected_relative_path = (
            partition_path(key)
            / f"dataset={dataset_version.removeprefix('sha256:')}"
            / "part-00000.parquet"
        ).as_posix()
        if (
            stored["dataset_version"] != dataset_version
            or stored["partition_manifest_id"] != partition_manifest_id(key)
            or stored["relative_path"] != expected_relative_path
        ):
            raise _IntegrityValidationError("market partition identity is invalid")

        try:
            held = _open_held_catalog_object(
                root_descriptor,
                stored["relative_path"],
            )
        except OSError as error:
            raise _IntegrityValidationError(
                "market partition path could not be opened securely"
            ) from error
        try:
            before = held.initial_stat
            if (
                type(stored["byte_size"]) is not int
                or before.st_size != stored["byte_size"]
            ):
                raise _IntegrityValidationError("market partition byte size is invalid")
            snapshot_descriptor: int | None = None
            try:
                try:
                    snapshot_descriptor, copied_hash = _open_read_only_snapshot(
                        held.descriptor
                    )
                except OSError as error:
                    raise _IntegrityValidationError(
                        "market partition could not be snapshotted"
                    ) from error
                if copied_hash != stored["physical_sha256"]:
                    raise _IntegrityValidationError(
                        "market partition physical hash is invalid"
                    )
                snapshot_before = os.fstat(snapshot_descriptor)
                snapshot_path = _descriptor_path(
                    snapshot_descriptor,
                    snapshot_before,
                )
                bars = _read_partition_bars(
                    snapshot_path,
                    max_rows=min(expected_row_count, max_rows),
                )
                try:
                    snapshot_after_hash = _descriptor_sha256(snapshot_descriptor)
                    snapshot_after = os.fstat(snapshot_descriptor)
                except OSError as error:
                    raise _IntegrityValidationError(
                        "market partition snapshot changed while it was read"
                    ) from error
                if (
                    _file_signature(snapshot_before) != _file_signature(snapshot_after)
                    or snapshot_after_hash != copied_hash
                ):
                    raise _IntegrityValidationError(
                        "market partition snapshot changed while it was read"
                    )
            finally:
                if snapshot_descriptor is not None:
                    os.close(snapshot_descriptor)
            try:
                after_hash = _descriptor_sha256(held.descriptor)
                after = os.fstat(held.descriptor)
            except OSError as error:
                raise _IntegrityValidationError(
                    "market partition changed while it was read"
                ) from error
            if (
                _file_signature(before) != _file_signature(after)
                or after_hash != stored["physical_sha256"]
            ):
                raise _IntegrityValidationError(
                    "market partition changed while it was read"
                )
            try:
                _verify_catalog_binding(
                    root_descriptor,
                    stored["relative_path"],
                    before,
                )
            except OSError as error:
                raise _IntegrityValidationError(
                    "market partition path changed while it was read"
                ) from error
        finally:
            os.close(held.descriptor)
        if expected_row_count != len(bars):
            raise _IntegrityValidationError("market partition row count is invalid")
        for bar in bars:
            if (
                bar.symbol != query.symbol
                or bar.period is not query.period
                or bar.adjustment is not query.adjustment
                or bar.timestamp.astimezone(MARKET_TIMEZONE).year != year
            ):
                raise _IntegrityValidationError(
                    "market partition bar metadata is invalid"
                )
        try:
            partition = StoredPartition(
                partition_manifest_id=stored["partition_manifest_id"],
                dataset_version=dataset_version,
                year=year,
                relative_path=stored["relative_path"],
                row_count=stored["row_count"],
                byte_size=stored["byte_size"],
                physical_sha256=stored["physical_sha256"],
            )
        except ValidationError as error:
            raise _IntegrityValidationError(
                "stored partition metadata is invalid"
            ) from error
        return bars, partition

    def _validate_routed(self, routed: RoutedBarSuccess) -> RoutedBarSuccess:
        canonical = RoutedBarSuccess.model_validate(routed.model_dump(mode="python"))
        result = canonical.result
        expected_dataset_version = provider_dataset_version(
            source=result.provenance.source,
            operation="bars",
            request={"query": result.query},
            data_cutoff=result.provenance.data_cutoff,
            items=result.bars,
        )
        if result.provenance.dataset_version != expected_dataset_version:
            raise ValueError(
                "dataset_version does not match canonical provider dataset"
            )
        manifest = canonical.manifest
        expected_manifest = make_routing_manifest(
            category=manifest.category,
            request=manifest.request,
            priority=manifest.priority,
            attempts=manifest.attempts,
            selected_source=manifest.selected_source,
            upstream_dataset_version=manifest.upstream_dataset_version,
            upstream_fetched_at=manifest.upstream_fetched_at,
            upstream_data_cutoff=manifest.upstream_data_cutoff,
            upstream_adjustment=manifest.upstream_adjustment,
            transition=manifest.transition,
        )
        if manifest != expected_manifest:
            raise ValueError("route_version does not match canonical routing manifest")
        return canonical

    @staticmethod
    def _validate_read_routed(routed: RoutedBarSuccess) -> RoutedBarSuccess:
        try:
            canonical = RoutedBarSuccess.model_validate(
                routed.model_dump(mode="python")
            )
        except ValidationError as error:
            raise _IntegrityValidationError(
                "routed market dataset is not canonical"
            ) from error
        result = canonical.result
        expected_dataset_version = provider_dataset_version(
            source=result.provenance.source,
            operation="bars",
            request={"query": result.query},
            data_cutoff=result.provenance.data_cutoff,
            items=result.bars,
        )
        if result.provenance.dataset_version != expected_dataset_version:
            raise _IntegrityValidationError(
                "dataset_version does not match canonical provider dataset"
            )
        manifest = canonical.manifest
        expected_manifest = make_routing_manifest(
            category=manifest.category,
            request=manifest.request,
            priority=manifest.priority,
            attempts=manifest.attempts,
            selected_source=manifest.selected_source,
            upstream_dataset_version=manifest.upstream_dataset_version,
            upstream_fetched_at=manifest.upstream_fetched_at,
            upstream_data_cutoff=manifest.upstream_data_cutoff,
            upstream_adjustment=manifest.upstream_adjustment,
            transition=manifest.transition,
        )
        if manifest != expected_manifest:
            raise _IntegrityValidationError(
                "route_version does not match canonical routing manifest"
            )
        return canonical

    def _write_locked(
        self,
        routed: RoutedBarSuccess,
        context: _OperationContext,
        dataset_lock: _DatasetLock,
    ) -> StoredRoutingManifest:
        grouped: dict[int, list[Bar]] = {}
        for bar in routed.result.bars:
            year = bar.timestamp.astimezone(MARKET_TIMEZONE).year
            grouped.setdefault(year, []).append(bar)
        new_targets = {
            self._partition_relative_path(routed, year).as_posix()
            for year in grouped
            if not _catalog_object_exists(
                context.root_descriptor,
                self._partition_relative_path(routed, year).as_posix(),
            )
        }
        published: list[_PublishedPartition] = []
        try:
            for year in sorted(grouped):
                relative_path = self._partition_relative_path(routed, year).as_posix()
                published.append(
                    self._publish_partition(
                        routed,
                        year,
                        tuple(grouped[year]),
                        context=context,
                        created=relative_path in new_targets,
                    )
                )
            partitions = tuple(item.stored for item in published)
            record_id = manifest_record_id(routed.manifest)
            self._verify_precommit_bindings(context, dataset_lock, published)
            self._commit_catalog(
                routed,
                record_id,
                partitions,
                before_commit=lambda: self._verify_precommit_bindings(
                    context,
                    dataset_lock,
                    published,
                ),
            )
        except Exception:
            self._cleanup_published_partitions(published)
            bound_targets = {item.stored.relative_path for item in published}
            self._cleanup_unreferenced(
                new_targets - bound_targets,
                root_descriptor=context.root_descriptor,
            )
            raise
        finally:
            for item in published:
                os.close(item.parent_descriptor)
        return StoredRoutingManifest(
            manifest_record_id=record_id,
            dataset_version=routed.result.provenance.dataset_version,
            route_version=routed.manifest.route_version,
            fetched_at=routed.manifest.upstream_fetched_at,
            partitions=partitions,
        )

    def _publish_partition(
        self,
        routed: RoutedBarSuccess,
        year: int,
        bars: tuple[Bar, ...],
        *,
        context: _OperationContext,
        created: bool,
    ) -> _PublishedPartition:
        result = routed.result
        dataset_version = result.provenance.dataset_version
        key = self._partition_key(routed, year)
        relative_path = self._partition_relative_path(routed, year)
        parent_descriptor = _open_private_chain(
            context.root_descriptor,
            relative_path.parent,
            create=True,
        )
        try:
            _recover_partition_publish(
                parent_descriptor,
                relative_path.name,
                bars,
            )
            try:
                target_descriptor = _open_regular_at(
                    parent_descriptor,
                    relative_path.name,
                )
            except FileNotFoundError:
                self._write_new_partition_at(
                    parent_descriptor,
                    relative_path.name,
                    bars,
                )
                target_descriptor = _open_regular_at(
                    parent_descriptor,
                    relative_path.name,
                )
            try:
                _validate_parquet_descriptor(target_descriptor, bars)
                target_metadata = os.fstat(target_descriptor)
                physical_sha256 = _descriptor_sha256(target_descriptor)
            finally:
                os.close(target_descriptor)
        except BaseException:
            os.close(parent_descriptor)
            raise
        stored = StoredPartition(
            partition_manifest_id=partition_manifest_id(key),
            dataset_version=dataset_version,
            year=year,
            relative_path=relative_path.as_posix(),
            row_count=len(bars),
            byte_size=target_metadata.st_size,
            physical_sha256=physical_sha256,
        )
        return _PublishedPartition(
            stored=stored,
            expected_bars=bars,
            parent_descriptor=parent_descriptor,
            target_name=relative_path.name,
            device=target_metadata.st_dev,
            inode=target_metadata.st_ino,
            created=created,
        )

    @staticmethod
    def _verify_published_partition(
        context: _OperationContext,
        published: _PublishedPartition,
    ) -> None:
        descriptor: int | None = None
        try:
            descriptor = _open_catalog_leaf(
                context.root_descriptor,
                published.stored.relative_path,
            )
            before = os.fstat(descriptor)
            if (
                stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size != published.stored.byte_size
                or (before.st_dev, before.st_ino) != (published.device, published.inode)
            ):
                raise _IntegrityValidationError(
                    "published partition binding changed before commit"
                )
            before_hash = _descriptor_sha256(descriptor)
            if before_hash != published.stored.physical_sha256:
                raise _IntegrityValidationError(
                    "published partition hash changed before commit"
                )
            _validate_parquet_descriptor(descriptor, published.expected_bars)
            after_hash = _descriptor_sha256(descriptor)
            after = os.fstat(descriptor)
            if (
                _file_signature(before) != _file_signature(after)
                or after_hash != before_hash
            ):
                raise _IntegrityValidationError(
                    "published partition changed during final validation"
                )
        except (OSError, ValueError) as error:
            raise MarketLakeCorruptionError(
                "published partition failed final binding validation"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _verify_precommit_bindings(
        self,
        context: _OperationContext,
        dataset_lock: _DatasetLock,
        published: Sequence[_PublishedPartition],
    ) -> None:
        self._verify_dataset_lock(context, dataset_lock)
        self._verify_operation_context(context)
        for item in published:
            self._verify_published_partition(context, item)
        self._verify_dataset_lock(context, dataset_lock)

    @staticmethod
    def _cleanup_published_partitions(
        published: Sequence[_PublishedPartition],
    ) -> None:
        for item in published:
            if not item.created:
                continue
            try:
                metadata = os.stat(
                    item.target_name,
                    dir_fd=item.parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino) != (item.device, item.inode)
            ):
                raise MarketLakeCorruptionError(
                    "published partition changed before cleanup"
                )
            os.unlink(item.target_name, dir_fd=item.parent_descriptor)
            _fsync_directory_descriptor(item.parent_descriptor)

    @staticmethod
    def _partition_key(routed: RoutedBarSuccess, year: int) -> PartitionKey:
        result = routed.result
        return PartitionKey(
            category="bars",
            source=result.provenance.source,
            symbol=result.query.symbol,
            period=result.query.period,
            adjustment=result.query.adjustment,
            year=year,
        )

    @classmethod
    def _partition_relative_path(
        cls,
        routed: RoutedBarSuccess,
        year: int,
    ) -> PurePosixPath:
        dataset_version = routed.result.provenance.dataset_version
        return (
            partition_path(cls._partition_key(routed, year))
            / f"dataset={dataset_version.removeprefix('sha256:')}"
            / "part-00000.parquet"
        )

    def _cleanup_unreferenced(
        self,
        targets: set[str],
        *,
        root_descriptor: int,
    ) -> None:
        with self._checked_connection() as connection:
            referenced = {
                str(path)
                for path in connection.execute(
                    select(MarketDatasetPartition.relative_path).where(
                        MarketDatasetPartition.relative_path.in_(targets)
                    )
                ).scalars()
            }
        for relative in targets:
            if relative in referenced:
                continue
            relative_path = PurePosixPath(relative)
            try:
                parent_descriptor = _open_private_chain(
                    root_descriptor,
                    relative_path.parent,
                    create=False,
                )
            except FileNotFoundError:
                continue
            try:
                try:
                    os.unlink(relative_path.name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    continue
                _fsync_directory_descriptor(parent_descriptor)
            finally:
                os.close(parent_descriptor)

    def _write_new_partition_at(
        self,
        parent_descriptor: int,
        target_name: str,
        bars: tuple[Bar, ...],
    ) -> None:
        temporary_name = f".{target_name}.{uuid4().hex}.tmp"
        temporary_descriptor: int | None = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="stock-desk-parquet-",
            ) as temporary_directory:
                external_directory = Path(temporary_directory)
                external_directory.chmod(0o700)
                generated = external_directory / "partition.parquet"
                with duckdb.connect(":memory:") as connection:
                    connection.execute(_CREATE_BAR_TABLE)
                    connection.executemany(
                        _INSERT_BAR, [_bar_parameters(bar) for bar in bars]
                    )
                    connection.execute(
                        "COPY market_bars TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                        [str(generated)],
                    )
                generated.chmod(0o600)
                _fsync_file(generated)
                _validate_parquet(generated, bars)
                source_descriptor = _secure_read_descriptor(generated)
                try:
                    no_follow = getattr(os, "O_NOFOLLOW", 0)
                    if no_follow == 0:
                        raise ValueError(
                            "market lake requires POSIX no-follow file access"
                        )
                    temporary_descriptor = os.open(
                        temporary_name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | no_follow,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    while chunk := os.read(source_descriptor, 1024 * 1024):
                        remaining = memoryview(chunk)
                        while remaining:
                            written = os.write(temporary_descriptor, remaining)
                            if written <= 0:
                                raise OSError("failed to copy parquet partition")
                            remaining = remaining[written:]
                finally:
                    os.close(source_descriptor)
                os.fchmod(temporary_descriptor, 0o600)
                os.fsync(temporary_descriptor)
                _validate_parquet_descriptor(temporary_descriptor, bars)
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing = _open_regular_at(parent_descriptor, target_name)
                try:
                    _validate_parquet_descriptor(existing, bars)
                finally:
                    os.close(existing)
            else:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
                _fsync_directory_descriptor(parent_descriptor)
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass

    def _commit_catalog(
        self,
        routed: RoutedBarSuccess,
        record_id: str,
        partitions: Sequence[StoredPartition],
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        result = routed.result
        version = result.provenance.dataset_version
        with self._checked_begin() as connection:
            dataset_row = (
                connection.execute(
                    select(MarketDataset).where(
                        MarketDataset.dataset_version == version
                    )
                )
                .mappings()
                .one_or_none()
            )
            if dataset_row is None:
                connection.execute(
                    insert(MarketDataset).values(
                        dataset_version=version,
                        source=result.provenance.source.value,
                        symbol=result.query.symbol,
                        period=result.query.period.value,
                        adjustment=result.query.adjustment.value,
                        query_start=result.query.start,
                        query_end=result.query.end,
                        data_cutoff=result.provenance.data_cutoff,
                        row_count=len(result.bars),
                    )
                )
            else:
                if not self._dataset_matches(dataset_row, routed):
                    raise ValueError("dataset_version collides with catalog metadata")

            for partition in partitions:
                partition_row = (
                    connection.execute(
                        select(MarketDatasetPartition).where(
                            MarketDatasetPartition.dataset_version
                            == partition.dataset_version,
                            MarketDatasetPartition.partition_manifest_id
                            == partition.partition_manifest_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if partition_row is None:
                    connection.execute(
                        insert(MarketDatasetPartition).values(
                            partition_manifest_id=partition.partition_manifest_id,
                            dataset_version=partition.dataset_version,
                            partition_year=partition.year,
                            relative_path=partition.relative_path,
                            row_count=partition.row_count,
                            byte_size=partition.byte_size,
                            physical_sha256=partition.physical_sha256,
                        )
                    )
                else:
                    if not self._partition_matches(partition_row, partition):
                        raise ValueError(
                            "partition_manifest_id collides with catalog metadata"
                        )

            manifest_row = (
                connection.execute(
                    select(MarketRoutingManifest).where(
                        MarketRoutingManifest.manifest_record_id == record_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            manifest_json = routed.manifest.model_dump(mode="json")
            if manifest_row is None:
                connection.execute(
                    insert(MarketRoutingManifest).values(
                        manifest_record_id=record_id,
                        dataset_version=version,
                        symbol=result.query.symbol,
                        route_version=routed.manifest.route_version,
                        manifest_json=manifest_json,
                        fetched_at=routed.manifest.upstream_fetched_at,
                    )
                )
            else:
                if (
                    manifest_row["dataset_version"] != version
                    or manifest_row["symbol"] != result.query.symbol
                    or manifest_row["route_version"] != routed.manifest.route_version
                    or manifest_row["manifest_json"] != manifest_json
                    or not _same_instant(
                        manifest_row["fetched_at"],
                        routed.manifest.upstream_fetched_at,
                    )
                ):
                    raise ValueError(
                        "manifest_record_id collides with catalog metadata"
                    )
            if before_commit is not None:
                before_commit()

    @staticmethod
    def _dataset_matches(stored: RowMapping, routed: RoutedBarSuccess) -> bool:
        result = routed.result
        return bool(
            stored["source"] == result.provenance.source.value
            and stored["symbol"] == result.query.symbol
            and stored["period"] == result.query.period.value
            and stored["adjustment"] == result.query.adjustment.value
            and _same_instant(stored["query_start"], result.query.start)
            and _same_instant(stored["query_end"], result.query.end)
            and _same_instant(stored["data_cutoff"], result.provenance.data_cutoff)
            and stored["row_count"] == len(result.bars)
        )

    @staticmethod
    def _partition_matches(
        stored: RowMapping,
        expected: StoredPartition,
    ) -> bool:
        return bool(
            stored["dataset_version"] == expected.dataset_version
            and stored["partition_year"] == expected.year
            and stored["relative_path"] == expected.relative_path
            and stored["row_count"] == expected.row_count
            and stored["byte_size"] == expected.byte_size
            and stored["physical_sha256"] == expected.physical_sha256
        )
