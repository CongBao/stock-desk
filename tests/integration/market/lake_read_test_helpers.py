from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3

from sqlalchemy.engine import Engine

from stock_desk.storage.database import create_engine_for_url, migrate


@contextmanager
def open_catalog_engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'read-catalog.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        yield engine
    finally:
        engine.dispose()


def physical_sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def corrupt_catalog(
    engine: Engine,
    *,
    table: str,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> None:
    database = engine.url.database
    if database is None:
        raise AssertionError("corruption helper requires a file-backed SQLite database")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_update")
        connection.execute(sql, parameters)


def refresh_partition_file_metadata(engine: Engine, path: Path) -> None:
    corrupt_catalog(
        engine,
        table="market_dataset_partition",
        sql=("UPDATE market_dataset_partition SET byte_size = ?, physical_sha256 = ?"),
        parameters=(path.stat().st_size, physical_sha256(path)),
    )
