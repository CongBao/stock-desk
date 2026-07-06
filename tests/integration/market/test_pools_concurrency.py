from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

import pytest
from sqlalchemy import event

from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.pools import PoolRepository
from tests.integration.market.task6_test_helpers import (
    instrument,
    routed_instruments,
    task6_database,
)


def test_pool_write_holds_one_catalog_pin_during_concurrent_publish(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    base = datetime(2026, 7, 6, 8, tzinfo=timezone.utc)
    old = routed_instruments(
        (instrument("600000.SH", "旧名称"),),
        cutoff=base,
        fetched_at=base + timedelta(hours=1),
    )
    new = routed_instruments(
        (
            instrument("000001.SZ", "平安银行"),
            instrument("600000.SH", "新名称"),
        ),
        cutoff=base + timedelta(days=1),
        fetched_at=base + timedelta(days=1, hours=1),
    )
    pinned = threading.Event()
    release = threading.Event()
    publish_started = threading.Event()
    publish_finished = threading.Event()
    original_current_catalog = InstrumentRepository.current_catalog

    def paused_current_catalog(self, *, connection=None):
        catalog = original_current_catalog(self, connection=connection)
        if connection is not None:
            pinned.set()
            assert release.wait(timeout=5)
        return catalog

    def publish_new_catalog():
        publish_started.set()
        try:
            return instruments.ingest(new)
        finally:
            publish_finished.set()

    try:
        old_manifest = instruments.ingest(old)
        monkeypatch.setattr(
            InstrumentRepository,
            "current_catalog",
            paused_current_catalog,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            pool_future = executor.submit(
                pools.create_custom,
                name="serialized",
                symbols=("600000.SH",),
            )
            assert pinned.wait(timeout=5)
            publish_future = executor.submit(publish_new_catalog)
            assert publish_started.wait(timeout=5)
            assert not publish_finished.wait(timeout=0.1)
            release.set()
            created = pool_future.result(timeout=5)
            new_manifest = publish_future.result(timeout=5)

        assert created.instrument_manifest_record_id == old_manifest.manifest_record_id
        assert created.instrument_dataset_version == old_manifest.dataset_version
        assert created.members[0].instrument.name == "旧名称"
        assert new_manifest.dataset_version != old_manifest.dataset_version
        assert instruments.current_manifest() == new_manifest
    finally:
        release.set()
        engine.dispose()


@pytest.mark.parametrize("reader_kind", ["summary", "detail"])
def test_pool_reads_hold_one_sqlite_snapshot_across_header_and_members(
    tmp_path: Path,
    reader_kind: str,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    header_read = threading.Event()
    release_reader = threading.Event()
    reader_thread_id: list[int] = []
    dbapi_transaction_states: list[bool] = []
    paused = False

    def pause_after_header(
        connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany: bool,
    ) -> None:
        nonlocal paused
        if (
            not paused
            and reader_thread_id
            and threading.get_ident() == reader_thread_id[0]
            and "FROM custom_pool" in statement
            and "custom_pool_member" not in statement
        ):
            paused = True
            dbapi_transaction_states.append(
                bool(connection.connection.driver_connection.in_transaction)
            )
            header_read.set()
            assert release_reader.wait(timeout=5)

    def read_pool():
        reader_thread_id.append(threading.get_ident())
        if reader_kind == "summary":
            return pools.list_custom_summaries()[0]
        return pools.get_custom(created.pool_id)

    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument("600000.SH", "浦发银行"),
                )
            )
        )
        created = pools.create_custom(name="revision-one", symbols=("600000.SH",))
        event.listen(engine, "after_cursor_execute", pause_after_header)
        with ThreadPoolExecutor(max_workers=2) as executor:
            reader_future = executor.submit(read_pool)
            assert header_read.wait(timeout=5)
            writer_future = executor.submit(
                pools.update_custom,
                created.pool_id,
                expected_revision=1,
                name="revision-two",
                symbols=("000001.SZ",),
            )
            updated = writer_future.result(timeout=5)
            release_reader.set()
            observed = reader_future.result(timeout=5)

        assert dbapi_transaction_states == [True]
        assert observed.revision == 1
        assert observed.name == "revision-one"
        assert updated.revision == 2
        assert (
            pools.update_custom(
                created.pool_id,
                expected_revision=2,
                name="revision-three",
                symbols=("600000.SH",),
            ).revision
            == 3
        )
    finally:
        release_reader.set()
        event.remove(engine, "after_cursor_execute", pause_after_header)
        engine.dispose()
