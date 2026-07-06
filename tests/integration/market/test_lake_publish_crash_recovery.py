from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from stock_desk.market.lake import MarketLake
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


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


def _crash_after_publish_link(
    engine: Engine,
    root: Path,
) -> tuple[Path, Path]:
    database_path = engine.url.database
    assert database_path is not None
    MarketLake(engine=engine, root=root)
    child_code = """
from datetime import date
import os
from pathlib import Path
import sys

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake
from stock_desk.storage.database import create_engine_for_url
from tests.integration.market.lake_test_helpers import routed_daily_bars

engine = create_engine_for_url(f"sqlite:///{sys.argv[1]}")
try:
    lake = MarketLake(engine=engine, root=Path(sys.argv[2]))
    original_link = os.link
    def crash_link(*args, **kwargs):
        original_link(*args, **kwargs)
        os._exit(73)
    lake_module.os.link = crash_link
    lake.write(routed_daily_bars((date(2024, 1, 2),)))
finally:
    engine.dispose()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, database_path, str(root)],
        cwd=Path(__file__).parents[3],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 73, (stdout, stderr)
    targets = tuple(root.rglob("part-00000.parquet"))
    temporary_objects = tuple(root.rglob(".*.tmp"))
    assert len(targets) == 1
    assert len(temporary_objects) == 1
    target = targets[0]
    temporary = temporary_objects[0]
    target_metadata = os.lstat(target)
    temporary_metadata = os.lstat(temporary)
    assert (target_metadata.st_dev, target_metadata.st_ino) == (
        temporary_metadata.st_dev,
        temporary_metadata.st_ino,
    )
    assert target_metadata.st_nlink == 2
    return target, temporary


def test_retry_recovers_crash_between_link_and_temp_unlink(tmp_path: Path) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        target, temporary = _crash_after_publish_link(engine, root)
        assert _catalog_counts(engine) == (0, 0, 0)

        stored = MarketLake(engine=engine, root=root).write(routed)

        assert root / stored.partitions[0].relative_path == target
        assert not temporary.exists()
        assert os.lstat(target).st_nlink == 1
        assert _catalog_counts(engine) == (1, 1, 1)


def test_retry_removes_strict_stale_temp_without_target(tmp_path: Path) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        source_root = tmp_path / "source-market"
        source_stored = MarketLake(engine=engine, root=source_root).write(routed)
        source = source_root / source_stored.partitions[0].relative_path
        lake = MarketLake(engine=engine, root=root)
        relative = lake._partition_relative_path(routed, 2024)
        parent = root / relative.parent
        parent.mkdir(mode=0o700, parents=True)
        for ancestor in (parent, *parent.parents):
            if ancestor == root.parent:
                break
            ancestor.chmod(0o700)
        stale = parent / f".{relative.name}.{'0' * 32}.tmp"
        shutil.copyfile(source, stale)
        stale.chmod(0o600)

        stored = lake.write(routed)

        assert not stale.exists()
        assert os.lstat(root / stored.partitions[0].relative_path).st_nlink == 1


def test_retry_rejects_crash_shape_with_extra_hardlink(tmp_path: Path) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        target, temporary = _crash_after_publish_link(engine, root)
        extra = target.with_name("ordinary-extra-hardlink")
        os.link(target, extra)

        with pytest.raises(ValueError, match="link"):
            MarketLake(engine=engine, root=root).write(routed)

        assert target.exists()
        assert temporary.exists()
        assert extra.exists()
        assert os.lstat(target).st_nlink == 3
        assert _catalog_counts(engine) == (0, 0, 0)
