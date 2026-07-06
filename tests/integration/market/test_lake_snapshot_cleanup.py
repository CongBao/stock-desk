from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

import pytest

import stock_desk.market.lake as lake_module


def _open_descriptor_count() -> int:
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        if directory.is_dir():
            return len(tuple(directory.iterdir()))
    pytest.skip("descriptor filesystem is unavailable")


def _exception_tree(error: BaseException) -> tuple[BaseException, ...]:
    collected = [error]
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            collected.extend(_exception_tree(nested))
    chained = error.__cause__ or error.__context__
    if chained is not None:
        collected.extend(_exception_tree(chained))
    return tuple(collected)


@pytest.mark.parametrize(
    "failure_point",
    [
        "main_unlink",
        "main_fsync",
        "cleanup_unlink",
        "cleanup_fsync",
        "rmdir",
    ],
)
def test_snapshot_failures_exhaust_every_cleanup_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    snapshot_parent = tmp_path / "snapshot-parent"
    snapshot_parent.mkdir(mode=0o700)
    source_path = tmp_path / "source.parquet"
    source_path.write_bytes(b"immutable source bytes")
    original_open = os.open
    original_close = os.close
    original_unlink = os.unlink
    original_fsync_directory = lake_module._fsync_directory_descriptor
    original_rmdir = os.rmdir
    current_iteration = -1
    roles_by_descriptor: dict[int, tuple[int, str]] = {}
    opened_by_iteration: dict[int, dict[str, int]] = {}
    close_attempts: set[tuple[int, str]] = set()
    unlink_calls: dict[int, int] = {}
    child_fsync_calls: dict[int, int] = {}
    rmdir_calls: dict[int, int] = {}
    injected: dict[int, list[OSError]] = {}

    def tracked_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        name = os.fsdecode(path)
        role: str | None = None
        if dir_fd is None and Path(name) == snapshot_parent:
            role = "parent"
        elif (
            name.startswith("stock-desk-read-snapshot-")
            and dir_fd is not None
            and roles_by_descriptor.get(dir_fd) == (current_iteration, "parent")
        ):
            role = "child"
        elif name == "snapshot.parquet" and dir_fd is not None:
            access_mode = flags & os.O_ACCMODE
            role = "read" if access_mode == os.O_RDONLY else "write"
        if role is not None:
            roles_by_descriptor[descriptor] = (current_iteration, role)
            opened_by_iteration.setdefault(current_iteration, {})[role] = descriptor
        return descriptor

    def tracked_close(descriptor: int) -> None:
        role = roles_by_descriptor.get(descriptor)
        if role is not None:
            close_attempts.add(role)
        original_close(descriptor)

    def fail_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        name = os.fsdecode(path)
        if name == "snapshot.parquet" and dir_fd is not None:
            calls = unlink_calls.get(current_iteration, 0) + 1
            unlink_calls[current_iteration] = calls
            should_fail = (
                (failure_point == "main_unlink" and calls == 1)
                or (failure_point == "cleanup_unlink" and calls <= 2)
                or (failure_point == "cleanup_fsync" and calls == 1)
            )
            if should_fail:
                error = OSError(
                    errno.EIO,
                    f"injected {failure_point} unlink {calls}",
                )
                injected.setdefault(current_iteration, []).append(error)
                raise error
        original_unlink(path, dir_fd=dir_fd)

    def fail_directory_fsync(descriptor: int) -> None:
        role = roles_by_descriptor.get(descriptor)
        if role == (current_iteration, "child"):
            calls = child_fsync_calls.get(current_iteration, 0) + 1
            child_fsync_calls[current_iteration] = calls
            should_fail = (failure_point == "main_fsync" and calls == 1) or (
                failure_point == "cleanup_fsync" and calls == 1
            )
            if should_fail:
                error = OSError(errno.EIO, f"injected {failure_point}")
                injected.setdefault(current_iteration, []).append(error)
                raise error
        original_fsync_directory(descriptor)

    def fail_rmdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        name = os.fsdecode(path)
        if name.startswith("stock-desk-read-snapshot-") and dir_fd is not None:
            calls = rmdir_calls.get(current_iteration, 0) + 1
            rmdir_calls[current_iteration] = calls
            if failure_point == "rmdir" and calls == 1:
                error = OSError(errno.EIO, "injected rmdir")
                injected.setdefault(current_iteration, []).append(error)
                raise error
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        lake_module.tempfile, "gettempdir", lambda: str(snapshot_parent)
    )
    monkeypatch.setattr(lake_module.os, "open", tracked_open)
    monkeypatch.setattr(lake_module.os, "close", tracked_close)
    monkeypatch.setattr(lake_module.os, "unlink", fail_unlink)
    monkeypatch.setattr(
        lake_module,
        "_fsync_directory_descriptor",
        fail_directory_fsync,
    )
    monkeypatch.setattr(lake_module.os, "rmdir", fail_rmdir)

    raised_errors: list[BaseException] = []
    with source_path.open("rb") as source:
        baseline = _open_descriptor_count()
        try:
            for repetition in range(3):
                current_iteration = repetition
                with pytest.raises(BaseException) as raised:
                    lake_module._open_read_only_snapshot(source.fileno())
                raised_errors.append(raised.value)

            for repetition, error in enumerate(raised_errors):
                errors = _exception_tree(error)
                assert all(expected in errors for expected in injected[repetition])
                if failure_point in {"cleanup_unlink", "cleanup_fsync"}:
                    assert isinstance(error, BaseExceptionGroup)
                else:
                    assert error is injected[repetition][0]
                assert {
                    role
                    for iteration, role in close_attempts
                    if iteration == repetition
                } == {"write", "read", "child", "parent"}

            for directory in snapshot_parent.iterdir():
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700
                assert not tuple(directory.iterdir())
            assert _open_descriptor_count() == baseline
        finally:
            for descriptors in opened_by_iteration.values():
                for descriptor in descriptors.values():
                    try:
                        os.fstat(descriptor)
                    except OSError:
                        continue
                    original_close(descriptor)
            for directory in tuple(snapshot_parent.iterdir()):
                snapshot = directory / "snapshot.parquet"
                if snapshot.exists():
                    original_unlink(snapshot)
                original_rmdir(directory)

    assert not tuple(snapshot_parent.iterdir())


def test_snapshot_cleanup_never_touches_replacement_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_parent = tmp_path / "snapshot-parent"
    snapshot_parent.mkdir(mode=0o700)
    source_path = tmp_path / "source.parquet"
    source_path.write_bytes(b"immutable source bytes")
    displaced = tmp_path / "displaced-original"
    replacement_bytes = b"replacement must remain untouched"
    original_open = os.open
    original_unlink = os.unlink
    original_fsync_directory = lake_module._fsync_directory_descriptor
    swapped = False
    snapshot_unlinks = 0
    child_descriptor: int | None = None

    def swap_after_read_reopen(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal child_descriptor, swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        name = os.fsdecode(path)
        if name.startswith("stock-desk-read-snapshot-") and dir_fd is not None:
            child_descriptor = descriptor
        if (
            not swapped
            and name == "snapshot.parquet"
            and flags & os.O_ACCMODE == os.O_RDONLY
        ):
            original_directory = next(snapshot_parent.iterdir())
            original_directory.replace(displaced)
            original_directory.mkdir(mode=0o700)
            replacement = original_directory / "snapshot.parquet"
            replacement.write_bytes(replacement_bytes)
            replacement.chmod(0o400)
            swapped = True
        return descriptor

    def fail_main_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal snapshot_unlinks
        if os.fsdecode(path) == "snapshot.parquet" and dir_fd == child_descriptor:
            snapshot_unlinks += 1
            if snapshot_unlinks == 1:
                raise OSError(errno.EIO, "injected main unlink")
        original_unlink(path, dir_fd=dir_fd)

    def fail_cleanup_fsync(descriptor: int) -> None:
        if descriptor == child_descriptor and snapshot_unlinks >= 2:
            raise OSError(errno.EIO, "injected cleanup fsync")
        original_fsync_directory(descriptor)

    monkeypatch.setattr(
        lake_module.tempfile, "gettempdir", lambda: str(snapshot_parent)
    )
    monkeypatch.setattr(lake_module.os, "open", swap_after_read_reopen)
    monkeypatch.setattr(lake_module.os, "unlink", fail_main_unlink)
    monkeypatch.setattr(
        lake_module,
        "_fsync_directory_descriptor",
        fail_cleanup_fsync,
    )

    with source_path.open("rb") as source:
        baseline = _open_descriptor_count()
        with pytest.raises(BaseExceptionGroup) as raised:
            lake_module._open_read_only_snapshot(source.fileno())

        assert {
            str(error)
            for error in _exception_tree(raised.value)
            if isinstance(error, OSError)
        } >= {"[Errno 5] injected main unlink", "[Errno 5] injected cleanup fsync"}
        replacement_directory = next(snapshot_parent.iterdir())
        replacement = replacement_directory / "snapshot.parquet"
        assert replacement.read_bytes() == replacement_bytes
        assert not (displaced / "snapshot.parquet").exists()
        assert _open_descriptor_count() == baseline

    original_unlink(replacement)
    os.rmdir(replacement_directory)
    os.rmdir(displaced)
    assert not tuple(snapshot_parent.iterdir())
