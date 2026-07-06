from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
from threading import Barrier

import pytest
from sqlalchemy.engine import Engine

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake
from stock_desk.storage.database import create_engine_for_url, migrate


_MARKER_NAME = ".stock-desk-market-lake"
_MARKER_CONTENT = b"stock-desk-market-lake-v1\n"
_MARKER_TEMP_PREFIX = f"{_MARKER_NAME}.init-"


class SimulatedCrash(BaseException):
    pass


@pytest.fixture
def catalog_engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'ownership-catalog.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _assert_valid_marker(root: Path) -> None:
    marker = root / _MARKER_NAME
    metadata = os.lstat(marker)
    assert stat.S_ISREG(metadata.st_mode)
    assert not stat.S_ISLNK(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert marker.read_bytes() == _MARKER_CONTENT


def test_constructor_rejects_unowned_nonempty_private_root_without_changes(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "unowned"
    root.mkdir(mode=0o700)
    unrelated = root / "authorized_keys"
    unrelated.write_bytes(b"do not touch\n")
    unrelated.chmod(0o600)
    before = os.lstat(unrelated)

    with pytest.raises(ValueError, match="ownership marker"):
        MarketLake(engine=catalog_engine, root=root)

    after = os.lstat(unrelated)
    assert unrelated.read_bytes() == b"do not touch\n"
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
    )
    assert {entry.name for entry in root.iterdir()} == {"authorized_keys"}


def test_constructor_initializes_empty_existing_root_and_reopens_it(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    root.mkdir(mode=0o700)

    first = MarketLake(engine=catalog_engine, root=root)
    second = MarketLake(engine=catalog_engine, root=root)

    assert first is not second
    _assert_valid_marker(root)
    assert (root / ".locks").is_dir()


@pytest.mark.parametrize("invalid_kind", ["symlink", "hardlink", "content", "mode"])
def test_constructor_rejects_invalid_marker_without_modifying_root(
    tmp_path: Path,
    catalog_engine: Engine,
    invalid_kind: str,
) -> None:
    root = tmp_path / f"invalid-{invalid_kind}"
    root.mkdir(mode=0o700)
    marker = root / _MARKER_NAME
    external = tmp_path / f"external-{invalid_kind}"
    external.write_bytes(_MARKER_CONTENT)
    external.chmod(0o600)
    if invalid_kind == "symlink":
        marker.symlink_to(external)
    elif invalid_kind == "hardlink":
        os.link(external, marker)
    else:
        marker.write_bytes(
            b"not-the-market-lake-marker\n"
            if invalid_kind == "content"
            else _MARKER_CONTENT
        )
        marker.chmod(0o600 if invalid_kind == "content" else 0o644)
    before_marker = os.lstat(marker)
    before_external = external.read_bytes()

    with pytest.raises(ValueError, match="ownership marker"):
        MarketLake(engine=catalog_engine, root=root)

    after_marker = os.lstat(marker)
    assert (after_marker.st_dev, after_marker.st_ino, after_marker.st_mode) == (
        before_marker.st_dev,
        before_marker.st_ino,
        before_marker.st_mode,
    )
    assert external.read_bytes() == before_external
    assert not (root / ".locks").exists()


def test_marker_is_fsynced_and_valid_before_lock_directory_creation(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    root.mkdir(mode=0o700)
    original_fsync_directory = lake_module._fsync_directory
    original_mkdir_private = lake_module._mkdir_private
    root_fsynced = False

    def record_fsync(path: Path) -> None:
        nonlocal root_fsynced
        original_fsync_directory(path)
        if path == root:
            root_fsynced = True

    def check_marker_before_mkdir(root_arg: Path, target: Path) -> None:
        if target == root / ".locks":
            _assert_valid_marker(root)
            assert root_fsynced
        original_mkdir_private(root_arg, target)

    monkeypatch.setattr(lake_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(lake_module, "_mkdir_private", check_marker_before_mkdir)

    MarketLake(engine=catalog_engine, root=root)

    _assert_valid_marker(root)


def test_concurrent_constructors_converge_on_one_marker(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    workers = 8
    barrier = Barrier(workers)

    def construct() -> None:
        barrier.wait()
        MarketLake(engine=catalog_engine, root=root)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(construct) for _ in range(workers)]
        for future in futures:
            future.result()

    _assert_valid_marker(root)
    assert (root / ".locks").is_dir()
    assert not tuple(root.glob(f"{_MARKER_TEMP_PREFIX}*.tmp"))


def test_constructor_recovers_crash_between_marker_link_and_temp_unlink(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    root.mkdir(mode=0o700)
    marker = root / _MARKER_NAME
    original_unlink = lake_module.os.unlink
    crashed = False

    def crash_once(
        path: os.PathLike[str] | str, *args: object, **kwargs: object
    ) -> None:
        nonlocal crashed
        candidate = Path(path)
        if (
            not crashed
            and candidate.name.startswith(_MARKER_TEMP_PREFIX)
            and candidate.name.endswith(".tmp")
            and marker.exists()
            and os.path.samefile(candidate, marker)
        ):
            crashed = True
            raise SimulatedCrash
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(lake_module.os, "unlink", crash_once)

    with pytest.raises(SimulatedCrash):
        MarketLake(engine=catalog_engine, root=root)

    temporary_markers = tuple(root.glob(f"{_MARKER_TEMP_PREFIX}*.tmp"))
    assert crashed
    assert len(temporary_markers) == 1
    assert os.path.samefile(marker, temporary_markers[0])
    assert os.lstat(marker).st_nlink == 2
    assert not (root / ".locks").exists()

    monkeypatch.setattr(lake_module.os, "unlink", original_unlink)
    MarketLake(engine=catalog_engine, root=root)

    _assert_valid_marker(root)
    assert not tuple(root.glob(f"{_MARKER_TEMP_PREFIX}*.tmp"))
    assert (root / ".locks").is_dir()
