from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text

from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.pools import (
    PoolCorruption,
    PoolItemIssueCode,
    PoolItemValidationError,
    PoolNotFound,
    PoolRepository,
    PoolRevisionConflict,
    PoolValidationError,
)
from stock_desk.market.types import InstrumentKind, ListingStatus
from stock_desk.storage.models import CustomPool, CustomPoolMember
from tests.integration.market.task6_test_helpers import (
    instrument,
    routed_instruments,
    task6_database,
)


def _catalog_items():
    return (
        instrument("000001.SZ", "平安银行"),
        instrument("600000.SH", "浦发银行"),
        instrument("600036.SH", "招商银行"),
        instrument("000300.SH", "沪深300", kind=InstrumentKind.INDEX),
        instrument("600001.SH", "退市公司", status=ListingStatus.DELISTED),
    )


def test_custom_pool_reopens_with_order_and_instrument_pin(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        catalog = instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(
            name="核心银行",
            symbols=("600036.SH", "000001.SZ", "600000.SH"),
        )
        reopened = PoolRepository(engine).get_custom(created.pool_id)

        assert UUID(created.pool_id).version == 4
        assert created == reopened
        assert created.revision == 1
        assert created.name == "核心银行"
        assert created.symbols == ("600036.SH", "000001.SZ", "600000.SH")
        assert [member.ordinal for member in created.members] == [0, 1, 2]
        assert created.instrument_manifest_record_id == catalog.manifest_record_id
        assert created.instrument_dataset_version == catalog.dataset_version
        assert PoolRepository(engine).list_customs() == (created,)
    finally:
        engine.dispose()


def test_custom_pool_collects_all_item_issues_and_rolls_back(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        with pytest.raises(PoolItemValidationError) as raised:
            pools.create_custom(
                name="invalid members",
                symbols=(
                    "bad",
                    "999999.SH",
                    "000300.SH",
                    "600001.SH",
                    "600000.SH",
                    "600000.SH",
                ),
            )

        assert [(issue.ordinal, issue.code) for issue in raised.value.issues] == [
            (0, PoolItemIssueCode.INVALID),
            (1, PoolItemIssueCode.NOT_FOUND),
            (2, PoolItemIssueCode.NOT_STOCK),
            (3, PoolItemIssueCode.DELISTED),
            (5, PoolItemIssueCode.DUPLICATE),
        ]
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(func.count()).select_from(CustomPool)
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    select(func.count()).select_from(CustomPoolMember)
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("name", "symbols"),
    [
        ("", ("600000.SH",)),
        (" padded ", ("600000.SH",)),
        ("x" * 65, ("600000.SH",)),
        ("valid", ()),
        ("valid", ("600000.SH",) * 5_001),
    ],
)
def test_custom_pool_rejects_unsafe_name_or_member_count(
    tmp_path: Path,
    name: str,
    symbols: tuple[str, ...],
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        with pytest.raises(PoolValidationError):
            pools.create_custom(name=name, symbols=symbols)
    finally:
        engine.dispose()


def test_custom_update_and_delete_use_revision_cas(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(
            name="old",
            symbols=("600000.SH", "000001.SZ"),
        )
        updated = pools.update_custom(
            created.pool_id,
            expected_revision=1,
            name="new",
            symbols=("600036.SH", "600000.SH"),
        )
        assert updated.revision == 2
        assert updated.name == "new"
        assert updated.symbols == ("600036.SH", "600000.SH")

        with pytest.raises(PoolRevisionConflict):
            pools.update_custom(
                created.pool_id,
                expected_revision=1,
                name="stale",
                symbols=("000001.SZ",),
            )
        with pytest.raises(PoolNotFound):
            pools.update_custom(
                str(uuid4()),
                expected_revision=1,
                name="missing",
                symbols=("000001.SZ",),
            )
        with pytest.raises(PoolRevisionConflict):
            pools.delete_custom(created.pool_id, expected_revision=1)
        for invalid_revision in (0, -1, True):
            with pytest.raises(PoolValidationError):
                pools.delete_custom(
                    created.pool_id,
                    expected_revision=invalid_revision,
                )

        pools.delete_custom(created.pool_id, expected_revision=2)
        with pytest.raises(PoolNotFound):
            pools.get_custom(created.pool_id)
        with pytest.raises(PoolNotFound):
            pools.delete_custom(created.pool_id, expected_revision=2)
    finally:
        engine.dispose()


def test_invalid_custom_update_rolls_back_old_revision_and_members(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(
            name="preserved",
            symbols=("600000.SH", "000001.SZ"),
        )
        with pytest.raises(PoolItemValidationError):
            pools.update_custom(
                created.pool_id,
                expected_revision=1,
                name="must roll back",
                symbols=("600036.SH", "bad"),
            )

        assert pools.get_custom(created.pool_id) == created
    finally:
        engine.dispose()


def test_custom_read_rejects_ineligible_raw_member_update(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(name="tamper", symbols=("600000.SH",))
        with engine.begin() as connection:
            connection.execute(
                CustomPoolMember.__table__.update()
                .where(CustomPoolMember.pool_id == created.pool_id)
                .values(symbol="000300.SH")
            )

        with pytest.raises(PoolCorruption, match="ineligible"):
            pools.get_custom(created.pool_id)
    finally:
        engine.dispose()


def test_two_writers_using_same_revision_have_exactly_one_success(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(name="base", symbols=("600000.SH",))
        barrier = threading.Barrier(2)

        def update(name: str, symbol: str):
            repository = PoolRepository(engine)
            barrier.wait(timeout=5)
            try:
                return repository.update_custom(
                    created.pool_id,
                    expected_revision=1,
                    name=name,
                    symbols=(symbol,),
                )
            except PoolRevisionConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(
                    lambda values: update(*values),
                    (("writer-a", "000001.SZ"), ("writer-b", "600036.SH")),
                )
            )

        successes = [
            outcome for outcome in outcomes if not isinstance(outcome, Exception)
        ]
        conflicts = [
            outcome for outcome in outcomes if isinstance(outcome, PoolRevisionConflict)
        ]
        assert len(successes) == 1
        assert len(conflicts) == 1
        final = pools.get_custom(created.pool_id)
        assert final.revision == 2
        assert final.name in {"writer-a", "writer-b"}
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "tamper",
    [
        "eligible-symbol",
        "reorder",
        "delete-and-count",
        "member-revision",
        "header-revision-rewind",
        "header-name",
        "member-digest",
        "state-digest",
    ],
)
def test_custom_pool_content_binding_rejects_raw_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(
            name="bound",
            symbols=("600000.SH", "000001.SZ"),
        )
        if tamper == "header-revision-rewind":
            created = pools.update_custom(
                created.pool_id,
                expected_revision=1,
                name="revision-two",
                symbols=("600000.SH", "000001.SZ"),
            )
        connection = engine.connect()
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
            connection.exec_driver_sql("BEGIN")
            if tamper == "eligible-symbol":
                connection.execute(
                    text(
                        "UPDATE custom_pool_member SET symbol = '600036.SH' "
                        "WHERE pool_id = :pool_id AND ordinal = 0"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "reorder":
                connection.execute(
                    text(
                        "UPDATE custom_pool_member SET ordinal = 99 "
                        "WHERE pool_id = :pool_id AND ordinal = 0"
                    ),
                    {"pool_id": created.pool_id},
                )
                connection.execute(
                    text(
                        "UPDATE custom_pool_member SET ordinal = 0 "
                        "WHERE pool_id = :pool_id AND ordinal = 1"
                    ),
                    {"pool_id": created.pool_id},
                )
                connection.execute(
                    text(
                        "UPDATE custom_pool_member SET ordinal = 1 "
                        "WHERE pool_id = :pool_id AND ordinal = 99"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "delete-and-count":
                connection.execute(
                    text(
                        "DELETE FROM custom_pool_member "
                        "WHERE pool_id = :pool_id AND ordinal = 1"
                    ),
                    {"pool_id": created.pool_id},
                )
                connection.execute(
                    text(
                        "UPDATE custom_pool SET member_count = 1 "
                        "WHERE pool_id = :pool_id"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "member-revision":
                connection.execute(
                    text(
                        "UPDATE custom_pool_member "
                        "SET member_revision = member_revision + 1 "
                        "WHERE pool_id = :pool_id AND ordinal = 0"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "header-revision-rewind":
                connection.execute(
                    text(
                        "UPDATE custom_pool SET revision = 1 WHERE pool_id = :pool_id"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "header-name":
                connection.execute(
                    text(
                        "UPDATE custom_pool SET name = 'still-valid' "
                        "WHERE pool_id = :pool_id"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "member-digest":
                connection.execute(
                    text(
                        "UPDATE custom_pool SET member_digest = :digest "
                        "WHERE pool_id = :pool_id"
                    ),
                    {
                        "pool_id": created.pool_id,
                        "digest": "sha256:" + "0" * 64,
                    },
                )
            else:
                connection.execute(
                    text(
                        "UPDATE custom_pool SET state_digest = :digest "
                        "WHERE pool_id = :pool_id"
                    ),
                    {
                        "pool_id": created.pool_id,
                        "digest": "sha256:" + "0" * 64,
                    },
                )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(PoolCorruption):
            pools.get_custom(created.pool_id)
    finally:
        engine.dispose()


def test_invalid_and_stale_updates_preserve_custom_content_binding(
    tmp_path: Path,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(
            name="preserved",
            symbols=("600000.SH", "000001.SZ"),
        )

        def binding() -> tuple[object, ...]:
            with engine.connect() as connection:
                header = connection.execute(
                    text(
                        "SELECT revision, state_digest, member_count "
                        "FROM custom_pool WHERE pool_id = :pool_id"
                    ),
                    {"pool_id": created.pool_id},
                ).one()
                members = tuple(
                    connection.execute(
                        text(
                            "SELECT member_revision, ordinal, symbol "
                            "FROM custom_pool_member WHERE pool_id = :pool_id "
                            "ORDER BY ordinal"
                        ),
                        {"pool_id": created.pool_id},
                    ).all()
                )
            return (*header, members)

        before = binding()
        with pytest.raises(PoolItemValidationError):
            pools.update_custom(
                created.pool_id,
                expected_revision=1,
                name="invalid",
                symbols=("bad",),
            )
        with pytest.raises(PoolRevisionConflict):
            pools.update_custom(
                created.pool_id,
                expected_revision=2,
                name="stale",
                symbols=("600036.SH",),
            )
        assert binding() == before
    finally:
        engine.dispose()


def test_update_rejects_tampered_current_state_before_mutation(tmp_path: Path) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(name="bound", symbols=("600000.SH",))
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE custom_pool SET name = 'valid-tamper' "
                    "WHERE pool_id = :pool_id"
                ),
                {"pool_id": created.pool_id},
            )

        with pytest.raises(PoolCorruption):
            pools.update_custom(
                created.pool_id,
                expected_revision=1,
                name="replacement",
                symbols=("000001.SZ",),
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "assignment",
    [
        "name = 'valid-tamper'",
        "member_count = 2",
        "revision = 2",
    ],
)
def test_custom_summary_rejects_header_state_tampering(
    tmp_path: Path,
    assignment: str,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(name="bound", symbols=("600000.SH",))
        connection = engine.connect()
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
            connection.exec_driver_sql("BEGIN")
            connection.execute(
                text(f"UPDATE custom_pool SET {assignment} WHERE pool_id = :pool_id"),
                {"pool_id": created.pool_id},
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(PoolCorruption):
            pools.list_custom_summaries()
    finally:
        engine.dispose()


@pytest.mark.parametrize("tamper", ["delete", "replace", "reorder", "revision"])
def test_custom_summary_rejects_actual_member_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    _url, engine = task6_database(tmp_path)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        created = pools.create_custom(
            name="bound",
            symbols=("600000.SH", "000001.SZ"),
        )
        connection = engine.connect()
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
            connection.commit()
            connection.exec_driver_sql("BEGIN")
            if tamper == "delete":
                connection.execute(
                    text(
                        "DELETE FROM custom_pool_member "
                        "WHERE pool_id = :pool_id AND ordinal = 1"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "replace":
                connection.execute(
                    text(
                        "UPDATE custom_pool_member SET symbol = '600036.SH' "
                        "WHERE pool_id = :pool_id AND ordinal = 0"
                    ),
                    {"pool_id": created.pool_id},
                )
            elif tamper == "reorder":
                connection.execute(
                    text(
                        "UPDATE custom_pool_member SET ordinal = 99 "
                        "WHERE pool_id = :pool_id AND ordinal = 0"
                    ),
                    {"pool_id": created.pool_id},
                )
            else:
                connection.execute(
                    text(
                        "UPDATE custom_pool_member "
                        "SET member_revision = member_revision + 1 "
                        "WHERE pool_id = :pool_id AND ordinal = 0"
                    ),
                    {"pool_id": created.pool_id},
                )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(PoolCorruption):
            pools.list_custom_summaries()
    finally:
        engine.dispose()


def test_custom_summary_member_query_has_page_scaled_hard_limit(
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
        if "FROM custom_pool_member" in statement:
            captured.append((statement, parameters))

    try:
        instruments.ingest(routed_instruments(_catalog_items()))
        pools.create_custom(name="one", symbols=("600000.SH",))
        pools.create_custom(name="two", symbols=("000001.SZ",))
        event.listen(engine, "before_cursor_execute", capture)

        assert len(pools.list_custom_summaries(limit=1)) == 1

        assert captured
        statement, parameters = captured[-1]
        assert "LIMIT" in statement
        assert 5_001 in tuple(parameters)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        engine.dispose()
