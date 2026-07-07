from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import event
from sqlalchemy import text
import pytest

from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from stock_desk.market.types import Adjustment, BarQuery, Period
from stock_desk.storage.database import create_engine_for_url, downgrade, migrate
from tests.integration.market.lake_test_helpers import local_time, routed_daily_bars
from tests.integration.backtest.test_worker_recovery import _complete_status


def test_bulk_catalog_pin_does_not_open_parquet_and_returns_containing_query(
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-pin.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
    routed = routed_daily_bars(
        (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
        adjustment=Adjustment.NONE,
    )
    stored = lake.write(routed)
    requested = BarQuery(
        symbol=routed.result.query.symbol,
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=local_time(date(2024, 1, 3)),
        end=local_time(date(2024, 1, 5)),
    )

    def forbidden_read(_manifest_record_id: str):
        raise AssertionError("submission must not open parquet")

    monkeypatch.setattr(lake, "_read_validated_record", forbidden_read)
    try:
        with engine.begin() as connection:
            pins = lake.catalog_latest_covering_many(
                connection,
                (requested,),
            )
        pin = pins[requested.symbol]
        assert pin.manifest_record_id == stored.manifest_record_id
        assert pin.dataset_version == stored.dataset_version
        assert pin.query == routed.result.query
        assert pin.query.start < requested.start
        assert pin.prefix_row_count == 1
    finally:
        engine.dispose()


def test_catalog_pin_rejects_incomplete_timestamp_evidence_without_parquet(
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-evidence-corrupt.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=(tmp_path / "market-corrupt").resolve())
    routed = routed_daily_bars(
        (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
        adjustment=Adjustment.NONE,
    )
    lake.write(routed)
    requested = BarQuery(
        symbol=routed.result.query.symbol,
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=local_time(date(2024, 1, 3)),
        end=local_time(date(2024, 1, 5)),
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER trg_market_dataset_timestamp_immutable_delete")
            )
            connection.execute(
                text(
                    "DELETE FROM market_dataset_timestamp "
                    "WHERE dataset_version = :dataset_version AND ordinal = 0"
                ),
                {"dataset_version": routed.result.provenance.dataset_version},
            )
        monkeypatch.setattr(
            lake,
            "_read_validated_record",
            lambda _manifest_id: (_ for _ in ()).throw(
                AssertionError("catalog proof must not open parquet")
            ),
        )

        with engine.begin() as connection:
            with pytest.raises(MarketLakeCorruptionError, match="integrity"):
                lake.catalog_latest_covering_many(connection, (requested,))
    finally:
        engine.dispose()


def test_catalog_pin_rejects_middle_timestamp_tamper_with_unchanged_seal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-middle-timestamp-tamper.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=(tmp_path / "market-tamper").resolve())
    routed = routed_daily_bars(
        (
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
        ),
        adjustment=Adjustment.NONE,
    )
    lake.write(routed)
    requested = BarQuery(
        symbol=routed.result.query.symbol,
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=local_time(date(2024, 1, 4)),
        end=local_time(date(2024, 1, 6)),
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER trg_market_dataset_timestamp_immutable_update")
            )
            connection.execute(
                text(
                    "UPDATE market_dataset_timestamp "
                    "SET timestamp = :timestamp "
                    "WHERE dataset_version = :dataset_version AND ordinal = 1"
                ),
                {
                    "dataset_version": routed.result.provenance.dataset_version,
                    "timestamp": "2024-01-03 04:00:00.000000",
                },
            )
        monkeypatch.setattr(
            lake,
            "_read_validated_record",
            lambda _manifest_id: (_ for _ in ()).throw(
                AssertionError("catalog proof must not open parquet")
            ),
        )

        with engine.begin() as connection:
            with pytest.raises(MarketLakeCorruptionError, match="integrity"):
                lake.catalog_latest_covering_many(connection, (requested,))
    finally:
        engine.dispose()


def test_catalog_pin_rejects_timestamp_seal_digest_mismatch(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-seal-digest-mismatch.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=(tmp_path / "market-seal-tamper").resolve())
    routed = routed_daily_bars(
        (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
        adjustment=Adjustment.NONE,
    )
    lake.write(routed)
    requested = BarQuery(
        symbol=routed.result.query.symbol,
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=local_time(date(2024, 1, 3)),
        end=local_time(date(2024, 1, 5)),
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER trg_market_dataset_timestamp_seal_immutable_update")
            )
            connection.execute(
                text(
                    "UPDATE market_dataset_timestamp_seal "
                    "SET timestamp_digest = :digest "
                    "WHERE dataset_version = :dataset_version"
                ),
                {
                    "dataset_version": routed.result.provenance.dataset_version,
                    "digest": "sha256:" + "0" * 64,
                },
            )

        with engine.begin() as connection:
            with pytest.raises(MarketLakeCorruptionError, match="integrity"):
                lake.catalog_latest_covering_many(connection, (requested,))
    finally:
        engine.dispose()


def test_bulk_pin_ranks_valid_digest_before_earlier_tampered_candidate(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-digest-rank.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=(tmp_path / "market-digest-rank").resolve())
    earlier = routed_daily_bars(
        (
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
        ),
        adjustment=Adjustment.NONE,
    )
    valid = routed_daily_bars(
        (date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5)),
        adjustment=Adjustment.NONE,
        volume_delta=-1,
    )
    lake.write(earlier)
    lake.write(valid)
    requested = BarQuery(
        symbol=valid.result.query.symbol,
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=local_time(date(2024, 1, 4)),
        end=local_time(date(2024, 1, 6)),
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER trg_market_dataset_timestamp_immutable_update")
            )
            connection.execute(
                text(
                    "UPDATE market_dataset_timestamp "
                    "SET timestamp = :timestamp "
                    "WHERE dataset_version = :dataset_version AND ordinal = 1"
                ),
                {
                    "dataset_version": earlier.result.provenance.dataset_version,
                    "timestamp": "2024-01-03 04:00:00.000000",
                },
            )
            pins = lake.catalog_latest_covering_many(
                connection,
                (requested,),
                prefer_earliest_prefix=True,
            )

        assert pins[requested.symbol].dataset_version == (
            valid.result.provenance.dataset_version
        )
    finally:
        engine.dispose()


def test_legacy_dataset_identical_rewrite_atomically_publishes_timestamp_seal(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'legacy-timestamp-seal.db'}"
    root = (tmp_path / "legacy-market").resolve()
    migrate(url)
    engine = create_engine_for_url(url)
    routed = routed_daily_bars(
        (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
        adjustment=Adjustment.NONE,
    )
    lake = MarketLake(engine=engine, root=root)
    stored = lake.write(routed)
    engine.dispose()

    downgrade(url, "0006_execution_status")
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=root)
    requested = BarQuery(
        symbol=routed.result.query.symbol,
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=local_time(date(2024, 1, 3)),
        end=local_time(date(2024, 1, 5)),
    )
    try:
        assert lake.read(stored.manifest_record_id) == routed
        with engine.begin() as connection:
            with pytest.raises(MarketLakeCorruptionError, match="integrity"):
                lake.catalog_latest_covering_many(connection, (requested,))

        lake.write(routed)

        with engine.begin() as connection:
            pins = lake.catalog_latest_covering_many(connection, (requested,))
            seal = connection.execute(
                text(
                    "SELECT index_version, row_count, timestamp_digest "
                    "FROM market_dataset_timestamp_seal "
                    "WHERE dataset_version = :dataset_version"
                ),
                {"dataset_version": routed.result.provenance.dataset_version},
            ).one()
        assert pins[requested.symbol].prefix_row_count == 1
        assert seal[0] == "market-timestamps-v1"
        assert seal[1] == len(routed.result.bars)
        assert str(seal[2]).startswith("sha256:")

        different = routed_daily_bars(
            (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
            adjustment=Adjustment.NONE,
            volume_delta=-1,
        )
        forged_result = different.result.model_copy(
            update={
                "provenance": different.result.provenance.model_copy(
                    update={"dataset_version": routed.result.provenance.dataset_version}
                )
            }
        )
        forged = different.model_copy(update={"result": forged_result})
        with pytest.raises(ValueError, match="version"):
            lake.write(forged)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "legacy_evidence_state",
    ["missing", "invalid_seal", "corrupt_evidence"],
)
def test_bulk_pin_prefers_valid_sealed_dataset_over_earlier_legacy_candidate(
    tmp_path: Path,
    legacy_evidence_state: str,
) -> None:
    url = f"sqlite:///{tmp_path / 'legacy-coexistence.db'}"
    root = (tmp_path / "coexistence-market").resolve()
    migrate(url)
    engine = create_engine_for_url(url)
    legacy = routed_daily_bars(
        (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)),
        adjustment=Adjustment.NONE,
    )
    MarketLake(engine=engine, root=root).write(legacy)
    engine.dispose()

    downgrade(url, "0006_execution_status")
    migrate(url)
    engine = create_engine_for_url(url)
    lake = MarketLake(engine=engine, root=root)
    sealed = routed_daily_bars(
        (date(2024, 1, 3), date(2024, 1, 4)),
        adjustment=Adjustment.NONE,
        volume_delta=-1,
    )
    requested = BarQuery(
        symbol=sealed.result.query.symbol,
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=local_time(date(2024, 1, 4)),
        end=local_time(date(2024, 1, 5)),
    )
    try:
        if legacy_evidence_state != "missing":
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO market_dataset_timestamp_seal "
                        "(dataset_version, index_version, row_count, timestamp_digest) "
                        "VALUES (:dataset_version, :index_version, 3, :digest)"
                    ),
                    {
                        "dataset_version": legacy.result.provenance.dataset_version,
                        "index_version": (
                            "invalid-v0"
                            if legacy_evidence_state == "invalid_seal"
                            else "market-timestamps-v1"
                        ),
                        "digest": "sha256:" + "0" * 64,
                    },
                )
        lake.write(sealed)

        with engine.begin() as connection:
            pins = lake.catalog_latest_covering_many(
                connection,
                (requested,),
                prefer_earliest_prefix=True,
            )

        pin = pins[requested.symbol]
        assert pin.dataset_version == sealed.result.provenance.dataset_version
        assert pin.query == sealed.result.query
        assert pin.prefix_row_count == 1
    finally:
        engine.dispose()


def test_execution_status_lake_exposes_bound_database_identity(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'status-identity.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        status_lake = ExecutionStatusLake(engine)
        market_lake = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
        assert status_lake.database_identity == market_lake.database_identity
    finally:
        engine.dispose()


def test_bulk_catalog_rank_queries_restrict_symbols_inside_sql(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'catalog-symbol-predicate.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market-many").resolve())
    statuses = ExecutionStatusLake(engine)
    days = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
    wanted = "600000.SH"
    symbols = (wanted, *(f"{index:06d}.SZ" for index in range(1, 31)))
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if "row_number() OVER" in statement:
            statements.append(statement)

    try:
        for symbol in symbols:
            market.write(
                routed_daily_bars(
                    days,
                    symbol=symbol,
                    adjustment=Adjustment.NONE,
                )
            )
            statuses.write(_complete_status(days[0], date(2024, 1, 5), symbol=symbol))
        event.listen(engine, "before_cursor_execute", capture)
        requested_market = BarQuery(
            symbol=wanted,
            period=Period.DAY,
            adjustment=Adjustment.NONE,
            start=local_time(days[1]),
            end=local_time(date(2024, 1, 5)),
        )
        requested_status = _complete_status(
            days[1], date(2024, 1, 5), symbol=wanted
        ).result.query
        with engine.begin() as connection:
            assert tuple(
                market.catalog_latest_covering_many(connection, (requested_market,))
            ) == (wanted,)
            assert tuple(
                statuses.catalog_latest_covering_many(connection, (requested_status,))
            ) == (wanted,)

        assert len(statements) == 2
        assert all("symbol IN" in statement for statement in statements)
        assert all(statement.count("?") < 32 for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        engine.dispose()
