from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
import pytest
from sqlalchemy import event, text

from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.pools import (
    PoolCategory,
    PoolComposition,
    PoolCorruption,
    PoolRepository,
    PoolValidationError,
)
from stock_desk.market.types import InstrumentKind, ListingStatus, ProviderId
from stock_desk.storage.models import PresetPoolMember
from tests.integration.market.task6_test_helpers import (
    instrument,
    routed_instruments,
    task6_database,
)


DATASET_A = "sha256:" + "a" * 64
DATASET_B = "sha256:" + "b" * 64
ROUTE_A = "sha256:" + "c" * 64
ROUTE_B = "sha256:" + "d" * 64


def composition(
    *,
    key: str = "csi-300",
    category: PoolCategory = PoolCategory.INDEX,
    name: str = "沪深300",
    symbols: tuple[str, ...] = ("600000.SH", "000001.SZ"),
    dataset_version: str = DATASET_A,
    route_version: str = ROUTE_A,
    cutoff: datetime = datetime(2026, 7, 6, 8, tzinfo=timezone.utc),
    fetched_at: datetime = datetime(2026, 7, 6, 9, tzinfo=timezone.utc),
    complete: bool = True,
) -> PoolComposition:
    return PoolComposition(
        preset_key=key,
        category=category,
        display_name=name,
        symbols=symbols,
        source=ProviderId.TUSHARE,
        dataset_version=dataset_version,
        route_version=route_version,
        fetched_at=fetched_at,
        data_cutoff=cutoff,
        complete=complete,
    )


def test_full_a_derives_eligible_stocks_and_preserves_unknown_status(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        catalog = instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument(
                        "600000.SH",
                        "未知状态公司",
                        status=ListingStatus.UNKNOWN,
                    ),
                    instrument(
                        "600001.SH",
                        "退市公司",
                        status=ListingStatus.DELISTED,
                    ),
                    instrument(
                        "000300.SH",
                        "沪深300",
                        kind=InstrumentKind.INDEX,
                    ),
                )
            )
        )

        published = pools.publish_full_a(
            preset_key="all-a",
            display_name="全部A股",
        )
        reopened = PoolRepository(engine).get_preset("all-a")

        assert published == reopened
        assert published.pool_id == "preset:all-a"
        assert published.composition.category is PoolCategory.ALL_A
        assert published.symbols == ("000001.SZ", "600000.SH")
        assert [member.ordinal for member in published.members] == [0, 1]
        assert published.members[1].instrument.listing_status is ListingStatus.UNKNOWN
        assert published.composition.source is catalog.source
        assert published.composition.dataset_version == catalog.dataset_version
        assert published.composition.route_version == catalog.route_version
        assert published.instrument_manifest_record_id == catalog.manifest_record_id
        assert published.instrument_dataset_version == catalog.dataset_version
    finally:
        engine.dispose()


def test_index_and_industry_presets_are_latest_persistent_and_idempotent(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    base = datetime(2026, 7, 6, 8, tzinfo=timezone.utc)
    try:
        catalog = instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument("600000.SH", "浦发银行"),
                    instrument("600036.SH", "招商银行"),
                )
            )
        )
        older = composition(cutoff=base, fetched_at=base + timedelta(hours=1))
        newer = composition(
            symbols=("600036.SH", "600000.SH"),
            dataset_version=DATASET_B,
            route_version=ROUTE_B,
            cutoff=base + timedelta(days=1),
            fetched_at=base + timedelta(days=1, hours=1),
        )
        industry = composition(
            key="banking",
            category=PoolCategory.INDUSTRY,
            name="银行",
            symbols=("600036.SH", "000001.SZ"),
            dataset_version="sha256:" + "e" * 64,
            route_version="sha256:" + "f" * 64,
        )

        pools.publish_preset(newer)
        old_snapshot = pools.publish_preset(older)
        assert pools.publish_preset(older) == old_snapshot
        banking = pools.publish_preset(industry)

        reopened = PoolRepository(engine)
        latest = reopened.get_preset("csi-300")
        assert latest.composition == newer
        assert latest.symbols == newer.symbols
        assert latest.instrument_manifest_record_id == catalog.manifest_record_id
        assert latest.instrument_dataset_version == catalog.dataset_version
        assert banking.composition.category is PoolCategory.INDUSTRY
        assert banking.symbols == industry.symbols
        assert banking.snapshot_id.startswith("sha256:")
        assert banking.snapshot_id != latest.snapshot_id
        assert [pool.pool_id for pool in reopened.list_presets()] == [
            "preset:banking",
            "preset:csi-300",
        ]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"complete": False},
        {"symbols": ("600000.SH", "600000.SH")},
        {"symbols": ("not-a-symbol",)},
        {"symbols": ()},
        {"key": "Unsafe Key"},
        {"name": " padded "},
        {"fetched_at": datetime(2026, 7, 6, 7, tzinfo=timezone.utc)},
    ],
)
def test_pool_composition_rejects_incomplete_duplicate_or_invalid_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        composition(**overrides)  # type: ignore[arg-type]


def test_preset_publish_rejects_all_a_or_ineligible_provider_members(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("600000.SH", "浦发银行"),
                    instrument(
                        "000300.SH",
                        "沪深300",
                        kind=InstrumentKind.INDEX,
                    ),
                    instrument(
                        "600001.SH",
                        "退市公司",
                        status=ListingStatus.DELISTED,
                    ),
                )
            )
        )
        with pytest.raises(PoolValidationError, match="full-A builder"):
            pools.publish_preset(
                composition(category=PoolCategory.ALL_A, key="all-a", name="全部A股")
            )
        with pytest.raises(PoolValidationError, match="eligible"):
            pools.publish_preset(composition(symbols=("000300.SH",)))
        with pytest.raises(PoolValidationError, match="eligible"):
            pools.publish_preset(composition(symbols=("600001.SH",)))
        with pytest.raises(PoolValidationError, match="catalog"):
            pools.publish_preset(composition(symbols=("000001.SZ",)))
    finally:
        engine.dispose()


def test_preset_read_rejects_member_that_is_no_longer_an_eligible_stock(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("600000.SH", "浦发银行"),
                    instrument(
                        "000300.SH",
                        "沪深300",
                        kind=InstrumentKind.INDEX,
                    ),
                )
            )
        )
        published = pools.publish_preset(composition(symbols=("600000.SH",)))
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_preset_pool_member_immutable_update"
            )
            connection.exec_driver_sql(
                "DROP TRIGGER trg_preset_pool_member_immutable_delete"
            )
            connection.execute(
                PresetPoolMember.__table__.update()
                .where(PresetPoolMember.snapshot_id == published.snapshot_id)
                .values(symbol="000300.SH")
            )

        with pytest.raises(PoolCorruption, match="ineligible"):
            pools.get_preset("csi-300")
    finally:
        engine.dispose()


@pytest.mark.parametrize("tamper", ["delete", "replace", "reorder"])
def test_preset_summary_rejects_actual_member_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument("600000.SH", "浦发银行"),
                    instrument("600036.SH", "招商银行"),
                )
            )
        )
        published = pools.publish_preset(composition())
        connection = engine.connect()
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.exec_driver_sql(
                "DROP TRIGGER trg_preset_pool_member_immutable_update"
            )
            connection.exec_driver_sql(
                "DROP TRIGGER trg_preset_pool_member_immutable_delete"
            )
            connection.commit()
            connection.exec_driver_sql("BEGIN")
            if tamper == "delete":
                connection.execute(
                    text(
                        "DELETE FROM preset_pool_member "
                        "WHERE snapshot_id = :snapshot_id AND ordinal = 1"
                    ),
                    {"snapshot_id": published.snapshot_id},
                )
            elif tamper == "replace":
                connection.execute(
                    text(
                        "UPDATE preset_pool_member SET symbol = '600036.SH' "
                        "WHERE snapshot_id = :snapshot_id AND ordinal = 0"
                    ),
                    {"snapshot_id": published.snapshot_id},
                )
            else:
                connection.execute(
                    text(
                        "UPDATE preset_pool_member SET ordinal = 99 "
                        "WHERE snapshot_id = :snapshot_id AND ordinal = 0"
                    ),
                    {"snapshot_id": published.snapshot_id},
                )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(PoolCorruption):
            pools.list_preset_summaries()
    finally:
        engine.dispose()


def test_preset_summary_member_query_has_page_scaled_hard_limit(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    captured: list[tuple[str, object]] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "FROM preset_pool_member" in statement:
            captured.append((statement, parameters))

    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument("600000.SH", "浦发银行"),
                )
            )
        )
        pools.publish_preset(composition(key="first"))
        pools.publish_preset(composition(key="second"))
        event.listen(engine, "before_cursor_execute", capture)

        assert len(pools.list_preset_summaries(limit=1)) == 1

        assert captured
        statement, parameters = captured[-1]
        assert "LIMIT" in statement
        assert 10_001 in tuple(parameters)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        engine.dispose()
