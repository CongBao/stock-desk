from __future__ import annotations

from datetime import date
import os
from pathlib import Path
from stat import S_IMODE

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

import stock_desk.market.lake as lake_module
from stock_desk.market.lake import MarketLake
from stock_desk.market.partitions import PartitionKey, partition_path
from stock_desk.market.provenance import RoutedBarSuccess
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


@pytest.fixture
def catalog_engine(tmp_path: Path) -> Engine:
    url = f"sqlite:///{tmp_path / 'security-catalog.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _relative_path(routed: RoutedBarSuccess, year: int) -> Path:
    key = PartitionKey(
        category="bars",
        source=routed.result.provenance.source,
        symbol=routed.result.query.symbol,
        period=routed.result.query.period,
        adjustment=routed.result.query.adjustment,
        year=year,
    )
    dataset_hex = routed.result.provenance.dataset_version.removeprefix("sha256:")
    return Path(partition_path(key) / f"dataset={dataset_hex}" / "part-00000.parquet")


def _catalog_is_empty(engine: Engine) -> bool:
    with engine.connect() as connection:
        return all(
            connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
            for table in (
                "market_dataset",
                "market_dataset_partition",
                "market_routing_manifest",
            )
        )


def _source_object(tmp_path: Path, routed: RoutedBarSuccess) -> tuple[Path, bytes]:
    url = f"sqlite:///{tmp_path / 'source-catalog.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        root = tmp_path / "source-market"
        stored = MarketLake(engine=engine, root=root).write(routed)
        path = root / stored.partitions[0].relative_path
        return path, path.read_bytes()
    finally:
        engine.dispose()


@pytest.mark.parametrize("relative_root", [Path("market"), Path("../market")])
def test_constructor_rejects_original_relative_root_before_cwd_resolution(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    relative_root: Path,
) -> None:
    working_directory = tmp_path / "working-directory"
    working_directory.mkdir(mode=0o700)
    monkeypatch.chdir(working_directory)

    with pytest.raises(ValueError, match="absolute"):
        MarketLake(engine=catalog_engine, root=relative_root)

    assert not (working_directory / "market").exists()
    assert not (tmp_path / "market").exists()


@pytest.mark.parametrize("dangerous_root", [Path("/"), Path("/tmp")])
def test_constructor_rejects_dangerous_roots_without_chmod(
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    dangerous_root: Path,
) -> None:
    chmod_calls: list[Path] = []

    def reject_chmod(path: os.PathLike[str] | str, _mode: int) -> None:
        chmod_calls.append(Path(path))
        raise AssertionError("dangerous root chmod attempted")

    monkeypatch.setattr(lake_module.os, "chmod", reject_chmod)

    with pytest.raises(ValueError, match="dedicated"):
        MarketLake(engine=catalog_engine, root=dangerous_root)

    assert chmod_calls == []


def test_constructor_rejects_existing_shared_root_without_changing_mode(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "shared-root"
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    with pytest.raises(ValueError, match="private"):
        MarketLake(engine=catalog_engine, root=root)

    assert S_IMODE(root.stat().st_mode) == 0o755


def test_constructor_does_not_chmod_existing_private_root(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    original_chmod = lake_module.os.chmod

    def preserve_root(path: os.PathLike[str] | str, mode: int) -> None:
        if Path(path) == root:
            raise AssertionError("existing root chmod attempted")
        original_chmod(path, mode)

    monkeypatch.setattr(lake_module.os, "chmod", preserve_root)

    MarketLake(engine=catalog_engine, root=root)

    assert S_IMODE(root.stat().st_mode) == 0o700


def test_constructor_does_not_chmod_existing_root_ancestors(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    existing_parent = tmp_path / "existing-parent"
    existing_parent.mkdir(mode=0o755)
    existing_parent.chmod(0o755)
    root = existing_parent / "new-private-root"

    MarketLake(engine=catalog_engine, root=root)

    assert S_IMODE(existing_parent.stat().st_mode) == 0o755
    assert S_IMODE(root.stat().st_mode) == 0o700


def test_constructor_rejects_symlink_ancestor(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        MarketLake(engine=catalog_engine, root=alias / "market")

    assert not (real_parent / "market").exists()


def test_constructor_rejects_symlink_lock_directory(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    root.mkdir(mode=0o700)
    MarketLake(engine=catalog_engine, root=root)
    (root / ".locks").rmdir()
    external = tmp_path / "external-locks"
    external.mkdir(mode=0o700)
    (root / ".locks").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        MarketLake(engine=catalog_engine, root=root)


def test_write_rejects_symlink_partition_directory(
    tmp_path: Path,
    catalog_engine: Engine,
) -> None:
    root = tmp_path / "market"
    root.mkdir(mode=0o700)
    lake = MarketLake(engine=catalog_engine, root=root)
    external = tmp_path / "external-partitions"
    external.mkdir(mode=0o700)
    (root / "layout=v1").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        lake.write(routed_daily_bars((date(2024, 1, 2),)))

    assert _catalog_is_empty(catalog_engine)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_write_rejects_linked_leaf_without_touching_external_object(
    tmp_path: Path,
    catalog_engine: Engine,
    link_kind: str,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2),))
    source, source_bytes = _source_object(tmp_path, routed)
    root = tmp_path / f"target-{link_kind}"
    root.mkdir(mode=0o700)
    lake = MarketLake(engine=catalog_engine, root=root)
    target = root / _relative_path(routed, 2024)
    current = root
    for segment in target.parent.relative_to(root).parts:
        current /= segment
        current.mkdir(mode=0o700)
        current.chmod(0o700)
    if link_kind == "symlink":
        target.symlink_to(source)
    else:
        os.link(source, target)

    with pytest.raises(ValueError, match="link"):
        lake.write(routed)

    assert source.read_bytes() == source_bytes
    assert target.exists()
    assert _catalog_is_empty(catalog_engine)


def test_constructor_fails_closed_off_posix(
    tmp_path: Path,
    catalog_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lake_module.os, "name", "nt")

    with pytest.raises(ValueError, match="POSIX"):
        MarketLake(engine=catalog_engine, root=tmp_path / "market")
