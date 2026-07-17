from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from stock_desk.market.execution_status import (
    ExecutionEligibility,
    ExecutionStatusDay,
    ExecutionStatusQuery,
    ExecutionStatusSnapshot,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.provenance import (
    ExecutionStatusRoutingRequest,
    RoutedExecutionStatusSuccess,
    make_routing_manifest,
)
from stock_desk.market.types import Exchange, Period, ProviderId
from stock_desk.market.types import MarketCapability
from stock_desk.storage.database import create_engine_for_url, migrate


UTC = timezone.utc


def _query() -> ExecutionStatusQuery:
    return ExecutionStatusQuery(
        symbol="600000.SH",
        exchange=Exchange.SH,
        start=date(2026, 1, 5),
        end=date(2026, 1, 8),
    )


def _days() -> tuple[ExecutionStatusDay, ...]:
    return (
        ExecutionStatusDay(
            day=date(2026, 1, 5),
            exchange=Exchange.SH,
            is_exchange_open=True,
            suspension_state=SuspensionState.NORMAL,
            raw_upper_limit=Decimal("11"),
            raw_lower_limit=Decimal("9"),
        ),
        ExecutionStatusDay(
            day=date(2026, 1, 6),
            exchange=Exchange.SH,
            is_exchange_open=True,
            suspension_state=SuspensionState.SUSPENDED,
            raw_upper_limit=Decimal("11.2"),
            raw_lower_limit=Decimal("9.2"),
        ),
        ExecutionStatusDay(
            day=date(2026, 1, 7),
            exchange=Exchange.SH,
            is_exchange_open=False,
            suspension_state=SuspensionState.NOT_APPLICABLE,
            raw_upper_limit=None,
            raw_lower_limit=None,
        ),
    )


def test_snapshot_requires_complete_natural_date_evidence() -> None:
    with pytest.raises(ValidationError, match="every natural date"):
        ExecutionStatusSnapshot(
            query=_query(),
            days=_days()[:-1],
            eligibility=(),
            source=ProviderId.TUSHARE,
            fetched_at=datetime(2026, 1, 8, tzinfo=UTC),
            data_cutoff=datetime(2026, 1, 7, 8, tzinfo=UTC),
            dataset_version="sha256:" + "0" * 64,
        )


def test_materialization_uses_raw_open_once_and_freezes_side_specific_blocks() -> None:
    snapshot = materialize_execution_status(
        query=_query(),
        days=_days(),
        raw_opens=(
            RawExecutionOpen(
                timestamp=datetime(2026, 1, 5, 1, 30, tzinfo=UTC),
                trading_day=date(2026, 1, 5),
                raw_open=Decimal("11"),
            ),
            RawExecutionOpen(
                timestamp=datetime(2026, 1, 6, 1, 30, tzinfo=UTC),
                trading_day=date(2026, 1, 6),
                raw_open=Decimal("9.2"),
            ),
        ),
        source=ProviderId.TUSHARE,
        fetched_at=datetime(2026, 1, 8, tzinfo=UTC),
        data_cutoff=datetime(2026, 1, 7, 8, tzinfo=UTC),
    )

    assert snapshot.eligibility == (
        ExecutionEligibility(
            timestamp=datetime(2026, 1, 5, 1, 30, tzinfo=UTC),
            trading_day=date(2026, 1, 5),
            is_exchange_open=True,
            suspension_state=SuspensionState.NORMAL,
            buy_blocked_at_open=True,
            sell_blocked_at_open=False,
            evidence_complete=True,
        ),
        ExecutionEligibility(
            timestamp=datetime(2026, 1, 6, 1, 30, tzinfo=UTC),
            trading_day=date(2026, 1, 6),
            is_exchange_open=True,
            suspension_state=SuspensionState.SUSPENDED,
            buy_blocked_at_open=False,
            sell_blocked_at_open=True,
            evidence_complete=True,
        ),
    )
    assert snapshot.dataset_version.startswith("sha256:")


def test_open_day_with_unknown_suspension_is_rejected() -> None:
    with pytest.raises(ValidationError, match="explicit suspension evidence"):
        ExecutionStatusDay(
            day=date(2026, 1, 5),
            exchange=Exchange.SH,
            is_exchange_open=True,
            suspension_state=SuspensionState.UNKNOWN,
            raw_upper_limit=None,
            raw_lower_limit=None,
        )


@pytest.fixture
def catalog_engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'catalog.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_execution_status_lake_round_trips_immutable_snapshot(
    catalog_engine: Engine,
) -> None:
    snapshot = materialize_execution_status(
        query=_query(),
        days=_days(),
        raw_opens=(
            RawExecutionOpen(
                timestamp=datetime(2026, 1, 5, 1, 30, tzinfo=UTC),
                trading_day=date(2026, 1, 5),
                raw_open=Decimal("10"),
            ),
        ),
        source=ProviderId.TUSHARE,
        fetched_at=datetime(2026, 1, 8, tzinfo=UTC),
        data_cutoff=datetime(2026, 1, 7, 8, tzinfo=UTC),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=snapshot.query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=snapshot.dataset_version,
        upstream_fetched_at=snapshot.fetched_at,
        upstream_data_cutoff=snapshot.data_cutoff,
        upstream_adjustment=None,
    )
    routed = RoutedExecutionStatusSuccess(result=snapshot, manifest=manifest)
    lake = ExecutionStatusLake(catalog_engine)

    stored = lake.write(routed)
    loaded = lake.read(stored.manifest_record_id)

    assert loaded == routed
    assert lake.latest_exact(snapshot.query) == stored
    assert (
        lake.latest_exact(
            ExecutionStatusQuery(
                symbol=snapshot.query.symbol,
                exchange=snapshot.query.exchange,
                start=snapshot.query.start,
                end=snapshot.query.end,
                period=Period.MIN60,
            )
        )
        is None
    )
    with pytest.raises(IntegrityError, match="immutable"):
        with catalog_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE execution_status_dataset "
                    "SET source = 'akshare' WHERE dataset_version = :version"
                ),
                {"version": snapshot.dataset_version},
            )


def test_execution_status_lake_rejects_catalog_identity_drift(
    catalog_engine: Engine,
) -> None:
    snapshot = materialize_execution_status(
        query=_query(),
        days=_days(),
        raw_opens=(
            RawExecutionOpen(
                timestamp=datetime(2026, 1, 5, 1, 30, tzinfo=UTC),
                trading_day=date(2026, 1, 5),
                raw_open=Decimal("10"),
            ),
        ),
        source=ProviderId.TUSHARE,
        fetched_at=datetime(2026, 1, 8, tzinfo=UTC),
        data_cutoff=datetime(2026, 1, 7, 8, tzinfo=UTC),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=snapshot.query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=snapshot.dataset_version,
        upstream_fetched_at=snapshot.fetched_at,
        upstream_data_cutoff=snapshot.data_cutoff,
        upstream_adjustment=None,
    )
    lake = ExecutionStatusLake(catalog_engine)
    stored = lake.write(
        RoutedExecutionStatusSuccess(result=snapshot, manifest=manifest)
    )
    with catalog_engine.begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER trg_execution_status_routing_manifest_immutable_update"
        )
        connection.execute(
            text(
                "UPDATE execution_status_routing_manifest "
                "SET route_version = :route WHERE manifest_record_id = :record"
            ),
            {
                "route": "sha256:" + "f" * 64,
                "record": stored.manifest_record_id,
            },
        )

    with pytest.raises(ValueError, match="catalog identity"):
        lake.read(stored.manifest_record_id)


def test_suspended_open_day_without_raw_bar_materializes_daily_opportunity() -> None:
    query = ExecutionStatusQuery(
        symbol="600000.SH",
        exchange=Exchange.SH,
        start=date(2026, 1, 12),
        end=date(2026, 1, 14),
        period=Period.WEEK,
    )
    snapshot = materialize_execution_status(
        query=query,
        days=(
            ExecutionStatusDay(
                day=date(2026, 1, 12),
                exchange=Exchange.SH,
                is_exchange_open=True,
                suspension_state=SuspensionState.SUSPENDED,
                raw_upper_limit=Decimal("11"),
                raw_lower_limit=Decimal("9"),
            ),
            ExecutionStatusDay(
                day=date(2026, 1, 13),
                exchange=Exchange.SH,
                is_exchange_open=True,
                suspension_state=SuspensionState.NORMAL,
                raw_upper_limit=Decimal("11"),
                raw_lower_limit=Decimal("9"),
            ),
        ),
        raw_opens=(
            RawExecutionOpen(
                timestamp=datetime(2026, 1, 13, 1, 30, tzinfo=UTC),
                trading_day=date(2026, 1, 13),
                raw_open=Decimal("10"),
            ),
        ),
        source=ProviderId.TUSHARE,
        fetched_at=datetime(2026, 1, 14, tzinfo=UTC),
        data_cutoff=datetime(2026, 1, 13, 7, tzinfo=UTC),
    )

    assert tuple(item.trading_day for item in snapshot.eligibility) == (
        date(2026, 1, 12),
        date(2026, 1, 13),
    )
    assert snapshot.eligibility[0].suspension_state is SuspensionState.SUSPENDED
    assert snapshot.eligibility[0].evidence_complete is True


def test_60m_status_never_invents_intraday_timestamps_without_raw_evidence() -> None:
    snapshot = materialize_execution_status(
        query=ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2026, 1, 12),
            end=date(2026, 1, 13),
            period=Period.MIN60,
        ),
        days=(
            ExecutionStatusDay(
                day=date(2026, 1, 12),
                exchange=Exchange.SH,
                is_exchange_open=True,
                suspension_state=SuspensionState.SUSPENDED,
                raw_upper_limit=Decimal("11"),
                raw_lower_limit=Decimal("9"),
            ),
        ),
        raw_opens=(),
        source=ProviderId.TUSHARE,
        fetched_at=datetime(2026, 1, 13, tzinfo=UTC),
        data_cutoff=datetime(2026, 1, 12, 7, tzinfo=UTC),
    )

    assert snapshot.eligibility == ()
