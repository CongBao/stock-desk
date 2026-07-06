from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import shutil
from threading import Event

from filelock import FileLock
import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _open_descriptor_count() -> int:
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        if directory.is_dir():
            return len(tuple(directory.iterdir()))
    pytest.skip("descriptor filesystem is unavailable")


@pytest.mark.parametrize("replacement_kind", ["symlink", "directory"])
def test_read_rejects_root_parent_replaced_before_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    parent = tmp_path / "lake-parent"
    parent.mkdir(mode=0o700)
    root = parent / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        replacement_parent = tmp_path / f"replacement-parent-{replacement_kind}"
        displaced_parent = tmp_path / f"displaced-parent-{replacement_kind}"
        shutil.copytree(parent, replacement_parent)
        os.replace(parent, displaced_parent)
        if replacement_kind == "symlink":
            parent.symlink_to(replacement_parent, target_is_directory=True)
        else:
            shutil.copytree(replacement_parent, parent)
        parsed_paths: list[Path] = []
        original_read = lake_module._read_partition_bars

        def record_parse(path: Path) -> tuple[lake_module.Bar, ...]:
            parsed_paths.append(path)
            return original_read(path)

        monkeypatch.setattr(lake_module, "_read_partition_bars", record_parse)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)
        assert parsed_paths == []


@pytest.mark.parametrize("swap_level", ["parent", "root"])
def test_read_operation_is_not_redirected_by_root_chain_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_level: str,
) -> None:
    parent = tmp_path / "lake-parent"
    parent.mkdir(mode=0o700)
    root = parent / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        target = root / stored.partitions[0].relative_path
        original_inode = os.lstat(target).st_ino
        replacement = tmp_path / f"replacement-{swap_level}"
        displaced = tmp_path / f"displaced-{swap_level}"
        source = parent if swap_level == "parent" else root
        shutil.copytree(source, replacement)
        original_load = lake._load_catalog_snapshot
        opened_inodes: list[int] = []
        original_open = lake_module._open_held_catalog_object

        def swap_after_operation_start(
            record_id: str,
            dataset_version: object,
        ) -> object:
            snapshot = original_load(record_id, dataset_version)
            os.replace(source, displaced)
            os.replace(replacement, source)
            return snapshot

        def record_open(
            root_descriptor: int,
            relative_path: object,
        ) -> lake_module._HeldCatalogObject:
            held = original_open(root_descriptor, relative_path)
            opened_inodes.append(held.initial_stat.st_ino)
            return held

        monkeypatch.setattr(lake, "_load_catalog_snapshot", swap_after_operation_start)
        monkeypatch.setattr(lake_module, "_open_held_catalog_object", record_open)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)
        assert opened_inodes == [original_inode]


@pytest.mark.parametrize("swap_level", ["parent", "root"])
def test_write_operation_is_not_redirected_by_root_chain_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_level: str,
) -> None:
    parent = tmp_path / "lake-parent"
    parent.mkdir(mode=0o700)
    root = parent / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        lake.write(routed_daily_bars((date(2024, 1, 2),)))
        replacement = tmp_path / f"write-replacement-{swap_level}"
        displaced = tmp_path / f"write-displaced-{swap_level}"
        source = parent if swap_level == "parent" else root
        shutil.copytree(source, replacement)
        routed = routed_daily_bars((date(2024, 2, 2),))
        dataset_hex = routed.result.provenance.dataset_version.removeprefix("sha256:")
        original_write = lake._write_locked

        def swap_then_write(*args: object, **kwargs: object) -> object:
            os.replace(source, displaced)
            os.replace(replacement, source)
            return original_write(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(lake, "_write_locked", swap_then_write)

        with pytest.raises(MarketLakeCorruptionError):
            lake.write(routed)
        assert not any(
            f"dataset={dataset_hex}" in path.parts for path in root.rglob("*.parquet")
        )


@pytest.mark.parametrize("operation", ["read", "write"])
def test_dataset_lock_interoperates_with_unix_filelock_contender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        entered = Event()
        if operation == "read":
            original = lake._read_snapshot

            def observe(*args: object, **kwargs: object) -> object:
                entered.set()
                return original(*args, **kwargs)

            monkeypatch.setattr(lake, "_read_snapshot", observe)
        else:
            original = lake._write_locked

            def observe(*args: object, **kwargs: object) -> object:
                entered.set()
                return original(*args, **kwargs)

            monkeypatch.setattr(lake, "_write_locked", observe)

        def invoke() -> object:
            if operation == "read":
                return lake.read(stored.manifest_record_id)
            return lake.write(routed)

        lock_path = (
            root / ".locks" / (f"{stored.dataset_version.removeprefix('sha256:')}.lock")
        )
        contender = FileLock(lock_path)
        contender.acquire()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(invoke)
                assert not entered.wait(timeout=0.2)
                contender.release()
                future.result(timeout=5)
        finally:
            if contender.is_locked:
                contender.release()


@pytest.mark.parametrize("operation", ["read", "write"])
def test_repeated_successful_operations_do_not_leak_descriptors(
    tmp_path: Path,
    operation: str,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed)

        def invoke() -> object:
            if operation == "read":
                return lake.read(stored.manifest_record_id)
            return lake.write(routed)

        invoke()
        baseline = _open_descriptor_count()

        for _ in range(10):
            invoke()

        assert _open_descriptor_count() == baseline


@pytest.mark.parametrize(
    ("operation", "failure_point"),
    [("read", "_read_snapshot"), ("write", "_write_locked")],
)
def test_repeated_failed_operations_do_not_leak_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_point: str,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=tmp_path / "market")
        stored = lake.write(routed)

        def fail(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("injected operation failure")

        monkeypatch.setattr(lake, failure_point, fail)

        def invoke() -> object:
            if operation == "read":
                return lake.read(stored.manifest_record_id)
            return lake.write(routed)

        with pytest.raises(RuntimeError, match="injected operation failure"):
            invoke()
        baseline = _open_descriptor_count()

        for _ in range(10):
            with pytest.raises(RuntimeError, match="injected operation failure"):
                invoke()

        assert _open_descriptor_count() == baseline
