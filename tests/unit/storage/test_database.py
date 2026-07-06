from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from stock_desk.storage.database import create_engine_for_url, timestamp_digest


def _aggregate_digest(connection) -> str:
    value = connection.execute(
        text(
            "SELECT stock_desk_timestamp_digest(ordinal, timestamp) "
            "FROM ("
            "SELECT 2 AS ordinal, '2024-01-04 00:00:00+08:00' AS timestamp "
            "UNION ALL SELECT 0, '2024-01-02 00:00:00+08:00' "
            "UNION ALL SELECT 1, '2024-01-02 16:00:00+00:00'"
            ")"
        )
    ).scalar_one()
    assert isinstance(value, str)
    return value


def test_sqlite_timestamp_digest_is_registered_on_pool_and_fresh_engines(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'timestamp-digest.db'}"
    expected = timestamp_digest(
        (
            datetime(2024, 1, 1, 16, tzinfo=timezone.utc),
            datetime(2024, 1, 2, 16, tzinfo=timezone.utc),
            datetime(2024, 1, 3, 16, tzinfo=timezone.utc),
        )
    )
    engine = create_engine_for_url(url)
    try:
        with engine.connect() as first, engine.connect() as second:
            assert _aggregate_digest(first) == expected
            assert _aggregate_digest(second) == expected
    finally:
        engine.dispose()

    fresh_engine = create_engine_for_url(url)
    try:
        with fresh_engine.connect() as connection:
            assert _aggregate_digest(connection) == expected
    finally:
        fresh_engine.dispose()


def test_sqlite_timestamp_digest_fails_closed_for_invalid_or_empty_evidence(
    tmp_path: Path,
) -> None:
    engine = create_engine_for_url(f"sqlite:///{tmp_path / 'invalid-digest.db'}")
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT stock_desk_timestamp_digest(ordinal, timestamp) "
                        "FROM (SELECT 1 AS ordinal, '2024-01-02 00:00:00' AS timestamp)"
                    )
                ).scalar_one()
                is None
            )
            assert (
                connection.execute(
                    text(
                        "SELECT stock_desk_timestamp_digest(ordinal, timestamp) "
                        "FROM (SELECT 0 AS ordinal, 'not-a-timestamp' AS timestamp)"
                    )
                ).scalar_one()
                is None
            )
            assert (
                connection.execute(
                    text(
                        "SELECT stock_desk_timestamp_digest(ordinal, timestamp) "
                        "FROM (SELECT 0 AS ordinal, '2024-01-02' AS timestamp WHERE 0)"
                    )
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()
