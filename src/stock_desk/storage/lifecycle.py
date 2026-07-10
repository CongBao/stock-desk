"""Cross-process exclusion between restore/recovery and live services."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from filelock import FileLock, Timeout as FileLockTimeout


_RESTORE_LOCK = ".stock-desk-restore.lock"
_SERVICE_DIRECTORY = ".stock-desk-services"
_REPARSE_POINT = 0x400
_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PLATFORM = os.name


class LifecycleBusyError(RuntimeError):
    """Restore or service startup conflicts with another live process."""


class LifecycleCorruptionError(RuntimeError):
    """Lifecycle lock state is not safe to trust."""


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _safe_lock_file(path: Path) -> None:
    metadata = os.lstat(path)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise LifecycleCorruptionError(
            "lifecycle lock is not a regular single-link file"
        )
    path.chmod(0o600)


def _service_directory(data_dir: Path) -> Path:
    directory = data_dir / _SERVICE_DIRECTORY
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = os.lstat(directory)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or (_PLATFORM == "posix" and stat.S_IMODE(metadata.st_mode) != 0o700)
    ):
        raise LifecycleCorruptionError("service lifecycle directory is unsafe")
    return directory


def _restore_lock(data_dir: Path, timeout_seconds: float) -> FileLock:
    lock = FileLock(data_dir / _RESTORE_LOCK, timeout=timeout_seconds)
    try:
        lock.acquire()
    except FileLockTimeout as error:
        raise LifecycleBusyError("restore lifecycle lock is busy") from error
    try:
        _safe_lock_file(data_dir / _RESTORE_LOCK)
    except BaseException:
        lock.release()
        raise
    return lock


def _reject_active_services(data_dir: Path) -> None:
    directory = _service_directory(data_dir)
    for marker in tuple(directory.iterdir()):
        if marker.suffix != ".lock":
            raise LifecycleCorruptionError("service lifecycle directory is ambiguous")
        try:
            probe = FileLock(marker, timeout=0)
            probe.acquire()
        except FileLockTimeout as error:
            raise LifecycleBusyError("a Stock Desk service is still running") from error
        try:
            _safe_lock_file(marker)
        finally:
            probe.release()
        marker.unlink()


def has_application_or_operator_content(data_dir: Path) -> bool:
    """Return whether a restore target contains more than lifecycle artifacts."""
    return any(
        entry.name not in {_RESTORE_LOCK, _SERVICE_DIRECTORY}
        for entry in data_dir.iterdir()
    )


@contextmanager
def restore_lifecycle(
    data_dir: Path,
    *,
    timeout_seconds: float = 0,
) -> Iterator[None]:
    """Exclude service startup and reject already-running service processes."""
    if timeout_seconds < 0:
        raise ValueError("restore lifecycle timeout must be nonnegative")
    root = Path(data_dir).resolve(strict=True)
    lock = _restore_lock(root, timeout_seconds)
    try:
        _reject_active_services(root)
        yield
    finally:
        lock.release()


@contextmanager
def service_lifecycle(
    data_dir: Path,
    *,
    role: str,
    timeout_seconds: float = 0,
    preflight: Callable[[], object] | None = None,
) -> Iterator[None]:
    """Register a live API/worker process without excluding peer services."""
    if _ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("service lifecycle role is invalid")
    root = Path(data_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    gate = _restore_lock(root, timeout_seconds)
    marker_lock: FileLock | None = None
    marker: Path | None = None
    try:
        if preflight is not None:
            preflight()
        directory = _service_directory(root)
        marker = directory / f"{role}-{os.getpid()}-{uuid4().hex}.lock"
        marker_lock = FileLock(marker, timeout=0)
        marker_lock.acquire()
        _safe_lock_file(marker)
    finally:
        gate.release()
    try:
        yield
    finally:
        if marker_lock is not None:
            marker_lock.release()
        if marker is not None:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
