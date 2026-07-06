from __future__ import annotations

from datetime import date
import errno
import os
from pathlib import Path
import shutil
import tempfile

import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


def test_directory_open_normalizes_nofollow_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o700)
    original_open = os.open

    def race_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path) == directory:
            raise OSError(errno.ELOOP, "injected symlink race")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(lake_module.os, "open", race_open)

    with pytest.raises(ValueError, match="symlink"):
        lake_module._open_private_directory(directory)


def test_catalog_leaf_normalizes_nofollow_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    root.mkdir(mode=0o700)
    target = root / "object"
    target.write_bytes(b"data")
    target.chmod(0o600)
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open

    def race_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path) == Path("object"):
            raise OSError(errno.ELOOP, "injected symlink race")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(lake_module.os, "open", race_open)
    try:
        with pytest.raises(ValueError, match="symlink"):
            lake_module._open_catalog_leaf(root_descriptor, "object")
    finally:
        os.close(root_descriptor)


def test_descriptor_helpers_reject_invalid_targets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="root path"):
        lake_module._open_absolute_root(Path("/"))

    root = tmp_path / "market"
    root.mkdir(mode=0o700)
    directory_leaf = root / "directory-leaf"
    directory_leaf.mkdir(mode=0o700)
    regular_root = tmp_path / "regular-root"
    regular_root.write_bytes(b"not a directory")
    regular_root.chmod(0o600)
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    regular_descriptor = os.open(regular_root, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="must name an object"):
            lake_module._open_catalog_leaf(root_descriptor, ".")
        with pytest.raises(ValueError, match="root descriptor"):
            lake_module._open_catalog_leaf(regular_descriptor, "object")
        with pytest.raises(ValueError, match="regular file"):
            lake_module._open_catalog_leaf(root_descriptor, "directory-leaf")
        with pytest.raises(ValueError, match="regular single-link"):
            lake_module._open_regular_at(root_descriptor, "directory-leaf")
    finally:
        os.close(regular_descriptor)
        os.close(root_descriptor)


def test_snapshot_copy_rejects_zero_progress_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryFile(mode="w+b") as source:
        with tempfile.TemporaryFile(mode="w+b") as destination:
            source.write(b"data")
            source.flush()
            monkeypatch.setattr(lake_module.os, "write", lambda *_args: 0)

            with pytest.raises(OSError, match="failed to snapshot"):
                lake_module._copy_and_hash_descriptor(
                    source.fileno(),
                    destination.fileno(),
                )


def test_read_rejects_lock_directory_replaced_before_operation(tmp_path: Path) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        locks = root / ".locks"
        displaced = tmp_path / "displaced-locks"
        os.replace(locks, displaced)
        shutil.copytree(displaced, locks)

        with pytest.raises(MarketLakeCorruptionError, match="root"):
            lake.read(stored.manifest_record_id)


def test_read_rejects_lock_directory_replaced_during_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        locks = root / ".locks"
        replacement = tmp_path / "replacement-locks"
        displaced = tmp_path / "displaced-locks"
        shutil.copytree(locks, replacement)
        original_load = lake._load_catalog_snapshot

        def swap_locks(record_id: str, dataset_version: object) -> object:
            snapshot = original_load(record_id, dataset_version)
            os.replace(locks, displaced)
            os.replace(replacement, locks)
            return snapshot

        monkeypatch.setattr(lake, "_load_catalog_snapshot", swap_locks)

        with pytest.raises(MarketLakeCorruptionError, match="root changed"):
            lake.read(stored.manifest_record_id)


@pytest.mark.parametrize(
    "failure_point",
    ["copy", "snapshot_hash", "source_hash", "binding"],
)
def test_read_normalizes_snapshot_descriptor_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed)
        if failure_point == "copy":

            def fail_copy(_source: int, _destination: int) -> str:
                raise OSError("injected snapshot copy failure")

            monkeypatch.setattr(
                lake_module,
                "_copy_and_hash_descriptor",
                fail_copy,
            )
        elif failure_point in {"snapshot_hash", "source_hash"}:
            original_hash = lake_module._descriptor_sha256
            calls = 0

            def fail_hash(descriptor: int) -> str:
                nonlocal calls
                calls += 1
                if failure_point == "snapshot_hash" or calls == 2:
                    raise OSError("injected descriptor hash failure")
                return original_hash(descriptor)

            monkeypatch.setattr(lake_module, "_descriptor_sha256", fail_hash)
        else:

            def fail_binding(*_args: object, **_kwargs: object) -> None:
                raise OSError("injected catalog binding failure")

            monkeypatch.setattr(
                lake_module,
                "_verify_catalog_binding",
                fail_binding,
            )

        with pytest.raises(MarketLakeCorruptionError, match="integrity"):
            lake.read(stored.manifest_record_id)
