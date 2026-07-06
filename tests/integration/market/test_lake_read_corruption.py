from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil

import duckdb
import pytest

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from tests.integration.market.lake_read_test_helpers import (
    open_catalog_engine,
    refresh_partition_file_metadata,
)
from tests.integration.market.lake_test_helpers import routed_daily_bars


@pytest.mark.parametrize("mutation", ["missing", "truncated", "bit-tampered"])
def test_read_rejects_missing_or_physically_damaged_partition(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        target = root / stored.partitions[0].relative_path
        if mutation == "missing":
            target.unlink()
        elif mutation == "truncated":
            target.write_bytes(target.read_bytes()[:32])
        else:
            content = bytearray(target.read_bytes())
            content[len(content) // 2] ^= 0x01
            target.write_bytes(content)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_rejects_valid_partition_swapped_from_another_dataset(
    tmp_path: Path,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        first = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        second = lake.write(routed_daily_bars((date(2024, 2, 2),)))
        first_path = root / first.partitions[0].relative_path
        second_path = root / second.partitions[0].relative_path
        shutil.copyfile(second_path, first_path)
        refresh_partition_file_metadata(engine, first_path)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(first.manifest_record_id)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_read_rejects_linked_partition_leaf(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        target = root / stored.partitions[0].relative_path
        external = tmp_path / f"external-{link_kind}.parquet"
        shutil.copyfile(target, external)
        target.unlink()
        if link_kind == "symlink":
            target.symlink_to(external)
        else:
            os.link(external, target)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_rejects_symlinked_partition_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        layout = root / "layout=v1"
        external = tmp_path / "external-layout"
        layout.rename(external)
        layout.symlink_to(external, target_is_directory=True)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_rejects_nonprivate_partition_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        target = root / stored.partitions[0].relative_path
        target.parent.chmod(0o755)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_rejects_wrong_schema_even_when_catalog_hash_matches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        target = root / stored.partitions[0].relative_path
        replacement = target.with_name("wrong-schema.parquet")
        with duckdb.connect(":memory:") as connection:
            connection.execute("CREATE TABLE wrong_schema (symbol VARCHAR)")
            connection.execute("INSERT INTO wrong_schema VALUES ('600000.SH')")
            connection.execute(
                "COPY wrong_schema TO ? (FORMAT PARQUET)",
                [str(replacement)],
            )
        replacement.chmod(0o600)
        os.replace(replacement, target)
        refresh_partition_file_metadata(engine, target)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)


def test_read_uses_catalog_paths_without_glob_or_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        routed = routed_daily_bars((date(2024, 1, 2),))
        stored = lake.write(routed)

        def reject_discovery(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("read attempted directory discovery")

        monkeypatch.setattr(Path, "glob", reject_discovery)
        monkeypatch.setattr(Path, "rglob", reject_discovery)
        monkeypatch.setattr(Path, "iterdir", reject_discovery)

        assert lake.read(stored.manifest_record_id) == routed


def test_read_detects_same_signature_tamper_after_duckdb_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "market"
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed_daily_bars((date(2024, 1, 2),)))
        target = root / stored.partitions[0].relative_path
        before = os.lstat(target)
        original_read = lake_module._read_partition_bars

        def tamper_after_read(path: Path) -> tuple[lake_module.Bar, ...]:
            bars = original_read(path)
            content = bytearray(target.read_bytes())
            content[len(content) // 2] ^= 0x01
            target.write_bytes(content)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            return bars

        monkeypatch.setattr(lake_module, "_read_partition_bars", tamper_after_read)

        with pytest.raises(MarketLakeCorruptionError):
            lake.read(stored.manifest_record_id)
