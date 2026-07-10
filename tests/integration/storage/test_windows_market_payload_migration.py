from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from stock_desk.storage.database import create_engine_for_url, downgrade, migrate


PREVIOUS_REVISION = "0011_worker_heartbeat"
HEAD_REVISION = "0012_windows_market_payload"


def _insert_dataset(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO market_dataset "
            "(dataset_version, source, symbol, period, adjustment, query_start, "
            "query_end, data_cutoff, row_count) VALUES "
            "('dataset-1', 'tdx_local', '600000.SH', '1d', 'qfq', "
            "'2024-01-01 00:00:00', '2024-01-31 00:00:00', "
            "'2024-02-01 00:00:00', 2)"
        )
    )


def test_upgrade_preserves_legacy_rows_and_enforces_complete_payload(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'windows-payload.db'}"
    migrate(url, PREVIOUS_REVISION)
    engine = create_engine_for_url(url)
    try:
        with engine.begin() as connection:
            _insert_dataset(connection)
            connection.execute(
                text(
                    "INSERT INTO market_dataset_timestamp "
                    "(dataset_version, ordinal, timestamp) VALUES "
                    "('dataset-1', 0, '2024-01-02 00:00:00')"
                )
            )
    finally:
        engine.dispose()

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == HEAD_REVISION
            )
            legacy = connection.execute(
                text(
                    "SELECT status, open, high, low, close, volume "
                    "FROM market_dataset_timestamp WHERE ordinal = 0"
                )
            ).one()
            assert legacy == (None, None, None, None, None, None)

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO market_dataset_timestamp "
                    "(dataset_version, ordinal, timestamp, status, open, high, "
                    "low, close, volume) VALUES "
                    "('dataset-1', 1, '2024-01-03 00:00:00', 'normal', "
                    "10.12500000, 11.25000000, 9.50000000, 10.75000000, 12345)"
                )
            )

        invalid_values = (
            "('dataset-1', 2, '2024-01-04 00:00:00', 'normal', 10, NULL, 9, 10, 1)",
            "('dataset-1', 2, '2024-01-04 00:00:00', 'bad', 10, 11, 9, 10, 1)",
            "('dataset-1', 2, '2024-01-04 00:00:00', 'normal', 10, 11, 9, 10, -1)",
        )
        for values in invalid_values:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO market_dataset_timestamp "
                            "(dataset_version, ordinal, timestamp, status, open, "
                            "high, low, close, volume) VALUES " + values
                        )
                    )

        with pytest.raises(IntegrityError, match="immutable"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE market_dataset_timestamp SET close = 12 "
                        "WHERE dataset_version = 'dataset-1' AND ordinal = 1"
                    )
                )
    finally:
        engine.dispose()


def test_downgrade_removes_payload_without_losing_legacy_rows(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'windows-payload-downgrade.db'}"
    migrate(url, PREVIOUS_REVISION)
    engine = create_engine_for_url(url)
    try:
        with engine.begin() as connection:
            _insert_dataset(connection)
            connection.execute(
                text(
                    "INSERT INTO market_dataset_timestamp "
                    "(dataset_version, ordinal, timestamp) VALUES "
                    "('dataset-1', 0, '2024-01-02 00:00:00')"
                )
            )
    finally:
        engine.dispose()

    migrate(url)
    downgrade(url, PREVIOUS_REVISION)
    engine = create_engine_for_url(url)
    try:
        inspector = inspect(engine)
        assert {
            column["name"]
            for column in inspector.get_columns("market_dataset_timestamp")
        } == {"dataset_version", "ordinal", "timestamp"}
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT dataset_version, ordinal, timestamp "
                    "FROM market_dataset_timestamp"
                )
            ).one()[:2] == ("dataset-1", 0)
            trigger_names = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND "
                        "name LIKE 'trg_market_dataset_timestamp_immutable_%'"
                    )
                )
            }
            assert trigger_names == {
                "trg_market_dataset_timestamp_immutable_insert",
                "trg_market_dataset_timestamp_immutable_update",
                "trg_market_dataset_timestamp_immutable_delete",
            }
    finally:
        engine.dispose()


def test_downgrade_refuses_to_discard_windows_market_payload(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'windows-payload-protected.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        with engine.begin() as connection:
            _insert_dataset(connection)
            connection.execute(
                text(
                    "INSERT INTO market_dataset_timestamp "
                    "(dataset_version, ordinal, timestamp, status, open, high, "
                    "low, close, volume) VALUES "
                    "('dataset-1', 0, '2024-01-02 00:00:00', 'normal', "
                    "10.12500000, 11.25000000, 9.50000000, 10.75000000, 12345)"
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="pre-upgrade database backup"):
        downgrade(url, PREVIOUS_REVISION)

    engine = create_engine_for_url(url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == HEAD_REVISION
            )
            assert connection.execute(
                text("SELECT status, close, volume FROM market_dataset_timestamp")
            ).one() == ("normal", 10.75, 12345)
    finally:
        engine.dispose()
