from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from stat import S_IMODE
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake, manifest_record_id
from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
    make_routing_manifest,
)
from stock_desk.market.types import MarketCapability
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


@pytest.fixture
def catalog_engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'fault-catalog.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _catalog_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return tuple(
            int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            for table in (
                "market_dataset",
                "market_dataset_partition",
                "market_routing_manifest",
            )
        )


def _temporary_objects(root: Path) -> tuple[Path, ...]:
    return tuple(path for path in root.rglob("*") if path.name.endswith(".tmp"))


def _open_descriptor_count() -> int:
    for directory in (Path("/dev/fd"), Path("/proc/self/fd")):
        if directory.is_dir():
            return len(tuple(directory.iterdir()))
    pytest.skip("descriptor filesystem is unavailable")


def _replace_routed_result(
    routed: RoutedBarSuccess,
    *,
    result: object,
) -> RoutedBarSuccess:
    replaced_result = routed.result.__class__.model_validate(result)
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=replaced_result.query),
        priority=routed.manifest.priority,
        attempts=routed.manifest.attempts,
        selected_source=replaced_result.provenance.source,
        upstream_dataset_version=replaced_result.provenance.dataset_version,
        upstream_fetched_at=replaced_result.provenance.fetched_at,
        upstream_data_cutoff=replaced_result.provenance.data_cutoff,
        upstream_adjustment=replaced_result.provenance.adjustment,
    )
    return RoutedBarSuccess(result=replaced_result, manifest=manifest)


@pytest.mark.parametrize(
    "failure_point",
    ["_fsync_file", "_fsync_directory_descriptor"],
)
def test_ordinary_publish_failure_removes_new_objects_and_catalog_rows(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)

    def fail_fsync(_target: object) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(lake_module, failure_point, fail_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        lake.write(routed_daily_bars((date(2024, 1, 2),)))

    assert _catalog_counts(catalog_engine) == (0, 0, 0)
    assert not tuple(root.rglob("*.parquet"))
    assert not _temporary_objects(root)


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EACCES])
def test_sibling_temp_open_failure_does_not_leak_source_descriptor(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)
    routed = routed_daily_bars((date(2024, 1, 2),))
    original_open = os.open
    attempts = 0

    def fail_sibling_temp_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal attempts
        name = os.fsdecode(path)
        if (
            name.startswith(".part-00000.parquet.")
            and name.endswith(".tmp")
            and flags & os.O_EXCL
            and dir_fd is not None
        ):
            attempts += 1
            raise OSError(error_number, "injected sibling temp open failure")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(lake_module.os, "open", fail_sibling_temp_open)
    with pytest.raises(OSError, match="sibling temp open failure"):
        lake.write(routed)
    baseline = _open_descriptor_count()

    for _ in range(4):
        with pytest.raises(OSError, match="sibling temp open failure"):
            lake.write(routed)

    assert attempts == 5
    assert _open_descriptor_count() == baseline
    assert _catalog_counts(catalog_engine) == (0, 0, 0)
    assert not tuple(root.rglob("*.parquet"))
    assert not _temporary_objects(root)


def test_catalog_commit_failure_rolls_back_rows_and_removes_published_objects(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)
    with catalog_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER inject_manifest_failure "
                "BEFORE INSERT ON market_routing_manifest "
                "BEGIN "
                "SELECT RAISE(ABORT, 'injected manifest failure'); "
                "END"
            )
        )

    with pytest.raises(DBAPIError, match="injected manifest failure"):
        lake.write(routed_daily_bars((date(2024, 1, 2),)))

    assert _catalog_counts(catalog_engine) == (0, 0, 0)
    assert not tuple(root.rglob("*.parquet"))
    assert not _temporary_objects(root)


def test_interrupted_catalog_commit_leaves_reusable_valid_orphan(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessExit(BaseException):
        pass

    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)
    routed = routed_daily_bars((date(2024, 1, 2),))
    commit_catalog = lake._commit_catalog

    def interrupt_commit(*_args: object, **_kwargs: object) -> None:
        raise SimulatedProcessExit

    monkeypatch.setattr(lake, "_commit_catalog", interrupt_commit)
    with pytest.raises(SimulatedProcessExit):
        lake.write(routed)

    orphan_paths = tuple(root.rglob("*.parquet"))
    assert len(orphan_paths) == 1
    orphan_bytes = orphan_paths[0].read_bytes()
    assert _catalog_counts(catalog_engine) == (0, 0, 0)
    assert not _temporary_objects(root)

    monkeypatch.setattr(lake, "_commit_catalog", commit_catalog)
    stored = lake.write(routed)

    assert len(stored.partitions) == 1
    assert (root / stored.partitions[0].relative_path).read_bytes() == orphan_bytes
    assert _catalog_counts(catalog_engine) == (1, 1, 1)


def test_same_dataset_concurrent_writes_converge_under_file_lock(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    lakes = (
        MarketLake(engine=catalog_engine, root=root),
        MarketLake(engine=catalog_engine, root=root),
    )
    barrier = threading.Barrier(2)

    def write_once(index: int) -> object:
        barrier.wait(timeout=5)
        return lakes[index].write(routed)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(write_once, range(2)))

    assert results[0] == results[1]
    assert _catalog_counts(catalog_engine) == (1, 1, 1)
    assert len(tuple(root.rglob("*.parquet"))) == 1
    assert not _temporary_objects(root)


def test_same_dataset_subprocess_writes_converge_under_file_lock(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    database_path = tmp_path / "fault-catalog.db"
    start_signal = tmp_path / "start"
    child_code = """
from datetime import date
from pathlib import Path
import sys
import time

from stock_desk.market.lake import MarketLake
from stock_desk.storage.database import create_engine_for_url
from tests.integration.market.lake_test_helpers import routed_daily_bars

while not Path(sys.argv[3]).exists():
    time.sleep(0.01)
engine = create_engine_for_url(f"sqlite:///{sys.argv[1]}")
try:
    MarketLake(engine=engine, root=Path(sys.argv[2])).write(
        routed_daily_bars((date(2024, 1, 2),))
    )
finally:
    engine.dispose()
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(database_path),
                str(root),
                str(start_signal),
            ],
            cwd=Path(__file__).parents[3],
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _worker in range(3)
    ]
    start_signal.touch()
    outcomes = [process.communicate(timeout=20) for process in processes]

    assert [
        (process.returncode, stdout, stderr)
        for process, (stdout, stderr) in zip(processes, outcomes, strict=True)
        if process.returncode != 0
    ] == []
    assert _catalog_counts(catalog_engine) == (1, 1, 1)
    assert len(tuple(root.rglob("*.parquet"))) == 1
    assert not _temporary_objects(root)


def test_write_uses_private_modes_for_objects_locks_and_all_directories(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    stored = MarketLake(engine=catalog_engine, root=root).write(routed)
    object_path = root / stored.partitions[0].relative_path
    lock_path = (
        root / ".locks" / f"{stored.dataset_version.removeprefix('sha256:')}.lock"
    )
    private_directories = {root, root / ".locks"}
    private_directories.update(
        parent
        for parent in object_path.parents
        if parent == root or root in parent.parents
    )

    assert {S_IMODE(path.stat().st_mode) for path in private_directories} == {0o700}
    assert S_IMODE(object_path.stat().st_mode) == 0o600
    assert S_IMODE(lock_path.stat().st_mode) == 0o600


def test_constructor_fsyncs_parent_for_new_root_and_lock_directory(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "durable-market"
    fsynced: list[Path] = []
    original_fsync_directory = lake_module._fsync_directory

    def record_fsync(path: Path) -> None:
        fsynced.append(path)
        original_fsync_directory(path)

    monkeypatch.setattr(lake_module, "_fsync_directory", record_fsync)

    MarketLake(engine=catalog_engine, root=root)

    assert root.parent in fsynced
    assert root in fsynced


def test_parent_directory_fsync_failure_prevents_catalog_visibility(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    lake = MarketLake(engine=catalog_engine, root=root)
    root_metadata = os.lstat(root)
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    fsynced: list[tuple[int, int]] = []
    original_fsync_directory = lake_module._fsync_directory_descriptor

    def fail_first_partition_parent(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        fsynced.append(identity)
        if identity == root_identity:
            raise OSError("injected parent directory fsync failure")
        original_fsync_directory(descriptor)

    monkeypatch.setattr(
        lake_module,
        "_fsync_directory_descriptor",
        fail_first_partition_parent,
    )

    with pytest.raises(OSError, match="parent directory fsync failure"):
        lake.write(routed_daily_bars((date(2024, 1, 2),)))

    assert root_identity in fsynced
    assert _catalog_counts(catalog_engine) == (0, 0, 0)
    assert not tuple(root.rglob("*.parquet"))
    assert not _temporary_objects(root)


def test_dataset_digest_collision_rejects_different_query_metadata(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)
    first = routed_daily_bars((date(2024, 1, 2),))
    version = first.result.provenance.dataset_version
    lake.write(first)
    original_path = next(root.rglob("*.parquet"))
    original_bytes = original_path.read_bytes()

    expanded_query = first.result.query.model_copy(
        update={"start": first.result.query.start - timedelta(days=1)}
    )
    collision_result = first.result.model_copy(
        update={
            "query": expanded_query,
            "coverage_start": expanded_query.start,
        }
    )
    collision = _replace_routed_result(
        first,
        result=collision_result.model_dump(mode="python"),
    )
    monkeypatch.setattr(
        lake_module,
        "provider_dataset_version",
        lambda **_kwargs: version,
    )

    with pytest.raises(ValueError, match="dataset_version collides.*metadata"):
        lake.write(collision)

    assert _catalog_counts(catalog_engine) == (1, 1, 1)
    assert tuple(root.rglob("*.parquet")) == (original_path,)
    assert original_path.read_bytes() == original_bytes


def test_dataset_digest_collision_rejects_different_content_without_overwrite(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    lake = MarketLake(engine=catalog_engine, root=root)
    first = routed_daily_bars((date(2024, 1, 2),))
    version = first.result.provenance.dataset_version
    lake.write(first)
    original_path = next(root.rglob("*.parquet"))
    original_bytes = original_path.read_bytes()
    changed_bar = first.result.bars[0].model_copy(
        update={"volume": first.result.bars[0].volume - 1}
    )
    collision_result = first.result.model_copy(update={"bars": (changed_bar,)})
    collision = _replace_routed_result(
        first,
        result=collision_result.model_dump(mode="python"),
    )
    monkeypatch.setattr(
        lake_module,
        "provider_dataset_version",
        lambda **_kwargs: version,
    )

    with pytest.raises(ValueError, match="partition content"):
        lake.write(collision)

    assert _catalog_counts(catalog_engine) == (1, 1, 1)
    assert tuple(root.rglob("*.parquet")) == (original_path,)
    assert original_path.read_bytes() == original_bytes


def test_existing_manifest_collision_comparison_includes_symbol(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    result = routed.result
    record_id = manifest_record_id(routed.manifest)
    database_path = tmp_path / "fault-catalog.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO market_dataset "
            "(dataset_version, source, symbol, period, adjustment, query_start, "
            "query_end, data_cutoff, row_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.provenance.dataset_version,
                result.provenance.source.value,
                result.query.symbol,
                result.query.period.value,
                result.query.adjustment.value,
                result.query.start.isoformat(),
                result.query.end.isoformat(),
                result.provenance.data_cutoff.isoformat(),
                len(result.bars),
            ),
        )
        connection.execute(
            "INSERT INTO market_routing_manifest "
            "(manifest_record_id, dataset_version, symbol, route_version, "
            "manifest_json, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record_id,
                result.provenance.dataset_version,
                "000001.SZ",
                routed.manifest.route_version,
                json.dumps(routed.manifest.model_dump(mode="json")),
                routed.manifest.upstream_fetched_at.isoformat(),
            ),
        )

    root = tmp_path / "market"
    with pytest.raises(ValueError, match="manifest_record_id collides"):
        MarketLake(engine=catalog_engine, root=root).write(routed)

    assert _catalog_counts(catalog_engine) == (1, 0, 1)
    assert not tuple(root.rglob("*.parquet"))
