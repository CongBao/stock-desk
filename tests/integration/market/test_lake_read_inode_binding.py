from __future__ import annotations

from datetime import date
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

import duckdb
import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _physical_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_descriptor_count() -> int:
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        if directory.is_dir():
            return len(tuple(directory.iterdir()))
    pytest.skip("descriptor filesystem is unavailable")


def _reencode_uncompressed(source: Path, destination: Path) -> None:
    with duckdb.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE copied AS "
            "SELECT * FROM read_parquet(?, hive_partitioning = false)",
            [str(source)],
        )
        connection.execute(
            "COPY copied TO ? (FORMAT PARQUET, COMPRESSION UNCOMPRESSED)",
            [str(destination)],
        )
    destination.chmod(0o600)


def test_read_binds_duckdb_to_original_leaf_during_swap_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        target = root / stored.partitions[0].relative_path
        replacement = target.with_name("replacement.parquet")
        displaced = target.with_name("displaced-original.parquet")
        _reencode_uncompressed(target, replacement)
        assert _physical_sha256(target) != _physical_sha256(replacement)
        original_sha = _physical_sha256(target)
        parsed_hashes: list[str] = []
        original_read = lake_module._read_partition_bars

        def swap_read_restore(
            path: Path, *, max_rows: int
        ) -> tuple[lake_module.Bar, ...]:
            os.replace(target, displaced)
            os.replace(replacement, target)
            try:
                parsed_hashes.append(_physical_sha256(path))
                return original_read(path, max_rows=max_rows)
            finally:
                os.replace(target, replacement)
                os.replace(displaced, target)

        monkeypatch.setattr(
            lake_module,
            "_read_partition_bars",
            swap_read_restore,
        )

        assert lake.read(stored.manifest_record_id) == routed
        assert parsed_hashes == [original_sha]


def test_read_parses_immutable_bytes_during_same_inode_mutate_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        target = root / stored.partitions[0].relative_path
        replacement = target.with_name("replacement-bytes.parquet")
        _reencode_uncompressed(target, replacement)
        original_bytes = target.read_bytes()
        replacement_bytes = replacement.read_bytes()
        original_metadata = os.lstat(target)
        original_sha = hashlib.sha256(original_bytes).hexdigest()
        replacement_sha = hashlib.sha256(replacement_bytes).hexdigest()
        assert replacement_sha != original_sha
        parsed_hashes: list[str] = []
        original_read = lake_module._read_partition_bars

        def mutate_read_restore(
            path: Path, *, max_rows: int
        ) -> tuple[lake_module.Bar, ...]:
            target.write_bytes(replacement_bytes)
            try:
                parsed_hashes.append(_physical_sha256(path))
                return original_read(path, max_rows=max_rows)
            finally:
                target.write_bytes(original_bytes)
                target.chmod(0o600)
                os.utime(
                    target,
                    ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
                )

        monkeypatch.setattr(
            lake_module,
            "_read_partition_bars",
            mutate_read_restore,
        )

        assert lake.read(stored.manifest_record_id) == routed
        assert parsed_hashes == [original_sha]


def test_read_snapshot_is_read_only_before_duckdb_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        target = root / stored.partitions[0].relative_path
        replacement = target.with_name("replacement-snapshot-bytes.parquet")
        _reencode_uncompressed(target, replacement)
        replacement_bytes = replacement.read_bytes()
        original_sha = _physical_sha256(target)
        replacement_sha = hashlib.sha256(replacement_bytes).hexdigest()
        assert replacement_sha != original_sha
        writable_reopens: list[int] = []
        mutation_errors: list[OSError] = []
        parsed_hashes: list[str] = []
        original_read = lake_module._read_partition_bars

        def replace_descriptor_bytes(descriptor: int, payload: bytes) -> None:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                assert written > 0
                remaining = remaining[written:]
            os.fsync(descriptor)

        def mutate_snapshot_read_restore(
            path: Path,
            *,
            max_rows: int,
        ) -> tuple[lake_module.Bar, ...]:
            snapshot_metadata = os.stat(path)
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            except OSError as error:
                mutation_errors.append(error)
                parsed_hashes.append(_physical_sha256(path))
                return original_read(path, max_rows=max_rows)
            writable_reopens.append(descriptor)
            snapshot_bytes = os.pread(
                descriptor,
                snapshot_metadata.st_size,
                0,
            )
            try:
                replace_descriptor_bytes(descriptor, replacement_bytes)
                parsed_hashes.append(_physical_sha256(path))
                return original_read(path, max_rows=max_rows)
            finally:
                replace_descriptor_bytes(descriptor, snapshot_bytes)
                os.utime(
                    path,
                    ns=(snapshot_metadata.st_atime_ns, snapshot_metadata.st_mtime_ns),
                )
                os.close(descriptor)

        monkeypatch.setattr(
            lake_module,
            "_read_partition_bars",
            mutate_snapshot_read_restore,
        )
        baseline = _open_descriptor_count()

        assert lake.read(stored.manifest_record_id) == routed
        assert writable_reopens == []
        assert len(mutation_errors) == 1
        assert parsed_hashes == [original_sha]
        assert _open_descriptor_count() == baseline


def test_snapshot_unlink_is_bound_to_original_temporary_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = os.open
    replacement_bytes = b"replacement snapshot must remain untouched"
    displaced_directory = tmp_path / "displaced-snapshot-directory"
    replacement_path: list[Path] = []
    original_lingering_path: list[Path] = []
    swapped = False

    def swap_directory_after_read_only_reopen(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        candidate = Path(path)
        candidate_parent = candidate.parent
        if dir_fd is not None and candidate.name == "snapshot.parquet":
            directory_metadata = os.fstat(dir_fd)
            candidate_parent = next(
                temporary_directory
                for temporary_directory in Path(tempfile.gettempdir()).glob(
                    "stock-desk-read-snapshot-*"
                )
                if (
                    (metadata := os.lstat(temporary_directory)).st_dev,
                    metadata.st_ino,
                )
                == (directory_metadata.st_dev, directory_metadata.st_ino)
            )
        if (
            not swapped
            and candidate.name == "snapshot.parquet"
            and flags & os.O_ACCMODE == os.O_RDONLY
            and candidate_parent.name.startswith("stock-desk-read-snapshot-")
        ):
            replacement = candidate_parent / candidate.name
            os.replace(candidate_parent, displaced_directory)
            candidate_parent.mkdir(mode=0o700)
            replacement.write_bytes(replacement_bytes)
            replacement.chmod(0o400)
            replacement_path.append(replacement)
            original_lingering_path.append(displaced_directory / candidate.name)
            swapped = True
        return descriptor

    monkeypatch.setattr(
        lake_module.os,
        "open",
        swap_directory_after_read_only_reopen,
    )
    with tempfile.TemporaryFile(mode="w+b") as source:
        source.write(b"original immutable snapshot")
        source.flush()
        baseline = _open_descriptor_count()

        descriptor, _copied_hash = lake_module._open_read_only_snapshot(source.fileno())
        try:
            metadata = os.fstat(descriptor)
            descriptor_flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            assert descriptor_flags & os.O_ACCMODE == os.O_RDONLY
            assert metadata.st_mode & 0o777 == 0o400
            assert metadata.st_nlink == 0
            assert replacement_path[0].read_bytes() == replacement_bytes
            assert not original_lingering_path[0].exists()
        finally:
            os.close(descriptor)

        assert swapped
        assert _open_descriptor_count() == baseline
        replacement_path[0].unlink()
        replacement_path[0].parent.rmdir()
        displaced_directory.rmdir()


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_read_binds_duckdb_to_original_ancestor_chain_during_swap_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        target = root / stored.partitions[0].relative_path
        layout = root / "layout=v1"
        replacement_layout = tmp_path / f"replacement-layout-{replacement_kind}"
        displaced_layout = tmp_path / f"displaced-layout-{replacement_kind}"
        shutil.copytree(layout, replacement_layout)
        replacement_target = replacement_layout / target.relative_to(layout)
        reencoded = replacement_target.with_name("reencoded.parquet")
        _reencode_uncompressed(replacement_target, reencoded)
        os.replace(reencoded, replacement_target)
        assert _physical_sha256(target) != _physical_sha256(replacement_target)
        original_sha = _physical_sha256(target)
        parsed_hashes: list[str] = []
        original_read = lake_module._read_partition_bars

        def swap_read_restore(
            path: Path, *, max_rows: int
        ) -> tuple[lake_module.Bar, ...]:
            os.replace(layout, displaced_layout)
            if replacement_kind == "directory":
                os.replace(replacement_layout, layout)
            else:
                layout.symlink_to(replacement_layout, target_is_directory=True)
            try:
                parsed_hashes.append(_physical_sha256(path))
                return original_read(path, max_rows=max_rows)
            finally:
                if replacement_kind == "directory":
                    os.replace(layout, replacement_layout)
                else:
                    layout.unlink()
                os.replace(displaced_layout, layout)

        monkeypatch.setattr(
            lake_module,
            "_read_partition_bars",
            swap_read_restore,
        )

        assert lake.read(stored.manifest_record_id) == routed
        assert parsed_hashes == [original_sha]
