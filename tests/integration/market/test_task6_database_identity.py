from datetime import date
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

from stock_desk.market.instruments import InstrumentCorruption, InstrumentRepository
from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from stock_desk.market.pools import PoolCorruption, PoolRepository
from stock_desk.market.types import Adjustment, Period
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import (
    CustomPool,
    InstrumentDataset,
    InstrumentRoutingManifest,
)
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


def _counts(database: Path) -> tuple[int, int, int]:
    engine = create_engine_for_url(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return (
                int(
                    connection.execute(
                        select(func.count()).select_from(InstrumentDataset)
                    ).scalar_one()
                ),
                int(
                    connection.execute(
                        select(func.count()).select_from(InstrumentRoutingManifest)
                    ).scalar_one()
                ),
                int(
                    connection.execute(
                        select(func.count()).select_from(CustomPool)
                    ).scalar_one()
                ),
            )
    finally:
        engine.dispose()


def test_instrument_repository_rejects_read_and_write_after_atomic_db_replace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.db"
    replacement = tmp_path / "replacement.db"
    original_inode = tmp_path / "original-inode.db"
    migrate(f"sqlite:///{database}")
    migrate(f"sqlite:///{replacement}")
    engine = create_engine_for_url(f"sqlite:///{database}")
    repository = InstrumentRepository(engine)
    routed = routed_instruments((instrument("600000.SH", "浦发银行"),))
    repository.ingest(routed)
    engine.dispose()
    os.replace(database, original_inode)
    os.replace(replacement, database)
    try:
        with pytest.raises(InstrumentCorruption, match="database"):
            repository.current_catalog()
        with pytest.raises(InstrumentCorruption, match="database"):
            repository.ingest(routed)
        assert _counts(database) == (0, 0, 0)
    finally:
        engine.dispose()


def test_pool_delete_rejects_symlink_rebind_without_touching_new_target(
    tmp_path: Path,
) -> None:
    database_a = tmp_path / "a.db"
    database_b = tmp_path / "b.db"
    alias = tmp_path / "alias.db"
    migrate(f"sqlite:///{database_a}")
    migrate(f"sqlite:///{database_b}")
    alias.symlink_to(database_a)
    engine = create_engine_for_url(f"sqlite:///{alias}")
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
    created = pools.create_custom(name="original", symbols=("600000.SH",))
    engine.dispose()
    alias.unlink()
    alias.symlink_to(database_b)
    try:
        with pytest.raises(PoolCorruption, match="database"):
            pools.delete_custom(created.pool_id, expected_revision=1)
        assert _counts(database_b) == (0, 0, 0)
    finally:
        engine.dispose()


def test_market_lake_rejects_catalog_read_after_atomic_db_replace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lake.db"
    replacement = tmp_path / "replacement.db"
    original_inode = tmp_path / "lake-original.db"
    migrate(f"sqlite:///{database}")
    migrate(f"sqlite:///{replacement}")
    engine = create_engine_for_url(f"sqlite:///{database}")
    lake = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
    lake.write(routed_daily_bars((date(2024, 1, 2),)))
    engine.dispose()
    os.replace(database, original_inode)
    os.replace(replacement, database)
    try:
        with pytest.raises(MarketLakeCorruptionError, match="database"):
            lake.read_latest_series("600000.SH", Period.DAY, Adjustment.QFQ)
        assert _counts(database) == (0, 0, 0)
    finally:
        engine.dispose()
