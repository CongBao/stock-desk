from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, func, insert, select
from sqlalchemy.exc import IntegrityError

from stock_desk.market.instruments import (
    MAX_INSTRUMENT_CATALOG_ITEMS,
    InstrumentConflict,
    InstrumentCorruption,
    InstrumentNotFound,
    InstrumentRepository,
    InstrumentValidationError,
    _validated_catalog_item_count,
)
from stock_desk.market.provenance import RoutedInstrumentSuccess
from stock_desk.market.providers.base import ProviderBatch
from stock_desk.market.types import Instrument
from stock_desk.market.types import InstrumentKind, ListingStatus
from stock_desk.storage.models import (
    InstrumentDataset,
    InstrumentDatasetItem,
    InstrumentRoutingManifest,
)
from tests.integration.market.task6_test_helpers import (
    instrument,
    routed_instruments,
    task6_database,
)


def test_ingest_search_detail_are_manifest_pinned_with_provenance(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    routed = routed_instruments(
        (
            instrument("000001.SZ", "平安银行"),
            instrument("000300.SH", "沪深300", kind=InstrumentKind.INDEX),
            instrument("600000.SH", "浦发银行"),
            instrument("600001.SH", "百分%公司"),
            instrument("600002.SH", "下划_公司", status=ListingStatus.UNKNOWN),
        )
    )
    try:
        published = repository.ingest(routed)
        assert repository.current_manifest() == published

        exact_code = repository.search("600000")
        chinese = repository.search("浦发")
        literal_percent = repository.search("%")
        literal_underscore = repository.search("_")
        detail = repository.get("600000.SH")

        assert [item.instrument.symbol for item in exact_code] == ["600000.SH"]
        assert [item.instrument.symbol for item in chinese] == ["600000.SH"]
        assert [item.instrument.symbol for item in literal_percent] == ["600001.SH"]
        assert [item.instrument.symbol for item in literal_underscore] == ["600002.SH"]
        assert detail.instrument.name == "浦发银行"
        assert detail.manifest == published
        assert all(item.manifest == published for item in chinese)
        assert published.source.value == "tushare"
        assert published.data_cutoff == routed.manifest.upstream_data_cutoff
    finally:
        engine.dispose()


def test_current_manifest_orders_by_cutoff_then_fetch_then_record_id(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    base = datetime(2026, 7, 6, 8, tzinfo=timezone.utc)
    older = routed_instruments(
        (instrument("600000.SH", "旧名称"),),
        cutoff=base,
        fetched_at=base + timedelta(hours=5),
    )
    newer = routed_instruments(
        (instrument("600000.SH", "新名称"),),
        cutoff=base + timedelta(days=1),
        fetched_at=base + timedelta(days=1, hours=1),
    )
    try:
        repository.ingest(newer)
        repository.ingest(older)
        assert repository.current_manifest().dataset_version == (
            newer.batch.provenance.dataset_version
        )
        assert repository.get("600000.SH").instrument.name == "新名称"
    finally:
        engine.dispose()


def test_ingest_is_idempotent_atomic_and_rejects_hash_collision(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    routed = routed_instruments((instrument("600000.SH", "浦发银行"),))
    try:
        first = repository.ingest(routed)
        assert repository.ingest(routed) == first

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_instrument_dataset_immutable_update"
            )
            connection.execute(
                InstrumentDataset.__table__.update()
                .where(
                    InstrumentDataset.dataset_version
                    == routed.batch.provenance.dataset_version
                )
                .values(row_count=2)
            )
        with pytest.raises(InstrumentConflict, match="collision"):
            repository.ingest(routed)

        with engine.connect() as connection:
            counts = (
                connection.execute(
                    select(func.count()).select_from(InstrumentDataset)
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(InstrumentDatasetItem)
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(InstrumentRoutingManifest)
                ).scalar_one(),
            )
        assert counts == (1, 1, 1)
    finally:
        engine.dispose()


def test_invalid_ingest_and_missing_or_invalid_reads_fail_typed(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    try:
        with pytest.raises(InstrumentNotFound):
            repository.current_manifest()
        with pytest.raises(InstrumentValidationError):
            repository.search("   ")
        with pytest.raises(InstrumentValidationError):
            repository.search("x" * 65)
        with pytest.raises(InstrumentNotFound):
            repository.get("600000.SH")
    finally:
        engine.dispose()


def test_current_manifest_rejects_dataset_cutoff_tampering(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    routed = routed_instruments((instrument("600000.SH", "浦发银行"),))
    try:
        repository.ingest(routed)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_instrument_dataset_immutable_update"
            )
            connection.execute(
                InstrumentDataset.__table__.update()
                .where(
                    InstrumentDataset.dataset_version
                    == routed.batch.provenance.dataset_version
                )
                .values(
                    data_cutoff=routed.batch.provenance.data_cutoff + timedelta(days=1)
                )
            )

        with pytest.raises(InstrumentCorruption, match="dataset"):
            repository.current_manifest()
    finally:
        engine.dispose()


def test_ingest_rejects_oversized_name_and_catalog_before_any_rows(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    valid = routed_instruments((instrument("600000.SH", "浦发银行"),))
    invalid_name = Instrument.model_construct(
        **{
            **valid.batch.items[0].model_dump(mode="python"),
            "name": "x" * 256,
        }
    )

    def unchecked(items: tuple[Instrument, ...]) -> RoutedInstrumentSuccess:
        batch = ProviderBatch[Instrument].model_construct(
            items=items,
            provenance=valid.batch.provenance,
        )
        return RoutedInstrumentSuccess.model_construct(
            batch=batch,
            manifest=valid.manifest,
        )

    try:
        with pytest.raises(InstrumentValidationError):
            repository.ingest(unchecked((invalid_name,)))
        with pytest.raises(InstrumentValidationError, match="too many"):
            repository.ingest(
                unchecked((valid.batch.items[0],) * (MAX_INSTRUMENT_CATALOG_ITEMS + 1))
            )
        with engine.connect() as connection:
            counts = (
                connection.execute(
                    select(func.count()).select_from(InstrumentDataset)
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(InstrumentDatasetItem)
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(InstrumentRoutingManifest)
                ).scalar_one(),
            )
        assert counts == (0, 0, 0)
    finally:
        engine.dispose()


def test_instrument_catalog_item_count_accepts_exact_boundary() -> None:
    assert (
        _validated_catalog_item_count(MAX_INSTRUMENT_CATALOG_ITEMS)
        == MAX_INSTRUMENT_CATALOG_ITEMS
    )
    with pytest.raises(InstrumentValidationError):
        _validated_catalog_item_count(MAX_INSTRUMENT_CATALOG_ITEMS + 1)


def test_instrument_item_ordinal_50000_is_rejected_by_database(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    routed = routed_instruments((instrument("600000.SH", "浦发银行"),))
    try:
        repository.ingest(routed)
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(InstrumentDatasetItem).values(
                        dataset_version=routed.batch.provenance.dataset_version,
                        symbol="600001.SH",
                        ordinal=MAX_INSTRUMENT_CATALOG_ITEMS,
                        exchange="SH",
                        name="越界项目",
                        instrument_kind="stock",
                        listing_status="listed",
                        listed_on=None,
                        delisted_on=None,
                    )
                )
    finally:
        engine.dispose()


def test_catalog_read_limits_items_to_declared_count_plus_one(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    repository = InstrumentRepository(engine)
    routed = routed_instruments((instrument("600000.SH", "浦发银行"),))
    item_selects: list[tuple[str, object]] = []

    def capture_item_select(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "FROM instrument_dataset_item" in statement:
            item_selects.append((statement, parameters))

    try:
        repository.ingest(routed)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_instrument_dataset_immutable_update"
            )
            connection.execute(
                InstrumentDataset.__table__.update().values(
                    row_count=MAX_INSTRUMENT_CATALOG_ITEMS
                )
            )
        event.listen(engine, "before_cursor_execute", capture_item_select)

        with pytest.raises(InstrumentCorruption, match="count"):
            repository.current_catalog()

        assert item_selects
        statement, parameters = item_selects[-1]
        assert "LIMIT" in statement
        assert MAX_INSTRUMENT_CATALOG_ITEMS + 1 in tuple(parameters)
    finally:
        event.remove(engine, "before_cursor_execute", capture_item_select)
        engine.dispose()
