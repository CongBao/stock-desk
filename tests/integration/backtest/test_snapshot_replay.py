from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_desk.backtest.config import BacktestRequest
from stock_desk.backtest.snapshot import freeze_request, reopen_snapshot
from stock_desk.backtest.types import PinnedMarketRef
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusQuery,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.lake import MarketLake
from stock_desk.market.provenance import (
    BarRoutingRequest,
    ExecutionStatusRoutingRequest,
    RoutedBarSuccess,
    RoutedExecutionStatusSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    Exchange,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
)
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")
DIGEST_STATUS_V1 = "sha256:" + "7" * 64


def _routed_status(
    *,
    symbol: str,
    start: date,
    end: date,
    fetched_at: datetime,
    upper_limit: Decimal,
) -> RoutedExecutionStatusSuccess:
    exchange = Exchange(symbol.rsplit(".", maxsplit=1)[1])
    query = ExecutionStatusQuery(
        symbol=symbol,
        exchange=exchange,
        start=start,
        end=end,
    )
    days = tuple(
        ExecutionStatusDay(
            day=date.fromordinal(start.toordinal() + offset),
            exchange=exchange,
            is_exchange_open=True,
            suspension_state=SuspensionState.NORMAL,
            raw_upper_limit=upper_limit,
            raw_lower_limit=Decimal("1"),
        )
        for offset in range((end - start).days)
    )
    result = materialize_execution_status(
        query=query,
        days=days,
        raw_opens=(),
        source=ProviderId.TUSHARE,
        fetched_at=fetched_at,
        data_cutoff=fetched_at,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=result.dataset_version,
        upstream_fetched_at=result.fetched_at,
        upstream_data_cutoff=result.data_cutoff,
        upstream_adjustment=None,
    )
    return RoutedExecutionStatusSuccess(result=result, manifest=manifest)


def _routed_intraday(
    *,
    start: datetime,
    end: datetime,
    timestamps: tuple[datetime, ...],
) -> RoutedBarSuccess:
    query = BarQuery(
        symbol="600000.SH",
        period=Period.MIN60,
        adjustment=Adjustment.QFQ,
        start=start,
        end=end,
    )
    bars = tuple(
        Bar(
            symbol=query.symbol,
            timestamp=timestamp,
            period=query.period,
            adjustment=query.adjustment,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=10_000 + index,
        )
        for index, timestamp in enumerate(timestamps)
    )
    data_cutoff = bars[-1].timestamp
    fetched_at = query.end + timedelta(days=1)
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": query},
        data_cutoff=data_cutoff,
        items=bars,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=ProviderId.TUSHARE,
            fetched_at=fetched_at,
            data_cutoff=data_cutoff,
            adjustment=query.adjustment,
            dataset_version=version,
        ),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=version,
        upstream_fetched_at=fetched_at,
        upstream_data_cutoff=data_cutoff,
        upstream_adjustment=query.adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


@pytest.mark.parametrize(
    ("query_start", "query_end", "timestamps", "status_start", "status_end"),
    [
        (
            datetime(2024, 7, 1, 9, 30, tzinfo=SHANGHAI),
            datetime(2024, 7, 1, 15, tzinfo=SHANGHAI),
            (
                datetime(2024, 7, 1, 9, 30, tzinfo=SHANGHAI),
                datetime(2024, 7, 1, 14, tzinfo=SHANGHAI),
            ),
            date(2024, 7, 1),
            date(2024, 7, 2),
        ),
        (
            datetime(2024, 7, 1, 9, 30, tzinfo=SHANGHAI),
            datetime(2024, 7, 3, 14, 30, tzinfo=SHANGHAI),
            (
                datetime(2024, 7, 1, 9, 30, tzinfo=SHANGHAI),
                datetime(2024, 7, 2, 10, 30, tzinfo=SHANGHAI),
                datetime(2024, 7, 3, 13, tzinfo=SHANGHAI),
            ),
            date(2024, 6, 30),
            date(2024, 7, 5),
        ),
    ],
    ids=("same-day-60m", "multi-day-intraday-containing-status"),
)
def test_intraday_snapshot_accepts_containing_date_exclusive_status_coverage(
    tmp_path: Path,
    query_start: datetime,
    query_end: datetime,
    timestamps: tuple[datetime, ...],
    status_start: date,
    status_end: date,
) -> None:
    routed = _routed_intraday(
        start=query_start,
        end=query_end,
        timestamps=timestamps,
    )
    status = _routed_status(
        symbol=routed.result.query.symbol,
        start=status_start,
        end=status_end,
        fetched_at=routed.result.provenance.fetched_at + timedelta(days=1),
        upper_limit=Decimal("12"),
    )

    with open_catalog_engine(tmp_path) as engine:
        market_lake = MarketLake(engine=engine, root=tmp_path / "market")
        status_lake = ExecutionStatusLake(engine)
        stored = market_lake.write(routed)
        stored_status = status_lake.write(status)
        reference = PinnedMarketRef(
            symbol=routed.result.query.symbol,
            signal_manifest_record_id=stored.manifest_record_id,
            signal_dataset_version=stored.dataset_version,
            signal_route_version=routed.manifest.route_version,
            signal_source=routed.result.provenance.source,
            signal_data_cutoff=routed.result.provenance.data_cutoff,
            signal_query=routed.result.query,
            execution_manifest_record_id=stored.manifest_record_id,
            execution_dataset_version=stored.dataset_version,
            execution_route_version=routed.manifest.route_version,
            execution_source=routed.result.provenance.source,
            execution_data_cutoff=routed.result.provenance.data_cutoff,
            execution_query=routed.result.query,
            execution_status_manifest_record_id=stored_status.manifest_record_id,
            execution_status_dataset_version=stored_status.dataset_version,
            execution_status_route_version=status.manifest.route_version,
            execution_status_source=status.manifest.selected_source,
            execution_status_data_cutoff=status.manifest.upstream_data_cutoff,
            execution_status_query=status.result.query,
        )
        snapshot = freeze_request(
            BacktestRequest(
                scope_kind="single",
                instrument_dataset_version="sha256:" + "1" * 64,
                symbols=(routed.result.query.symbol,),
                formula_version_id="formula-v1",
                formula_checksum="sha256:" + "2" * 64,
                formula_engine_version="formula-engine-v1",
                compatibility_version="tdx-v1",
                formula_parameters=(),
                symbol_inputs=(reference,),
                period=Period.MIN60,
                adjustment=Adjustment.QFQ,
                scoring_start=routed.result.query.start,
                scoring_end=routed.result.query.end,
                commission_bps=Decimal("2.5"),
                minimum_commission=Decimal("5"),
                sell_tax_bps=Decimal("5"),
                slippage_bps=Decimal("3"),
                backtest_engine_version="backtest-engine-v1",
            )
        )

        reopened = reopen_snapshot(
            snapshot,
            market_lake=market_lake,
            status_lake=status_lake,
        )

    assert reopened.symbols[0].execution_status == status


def test_snapshot_reopens_exact_pinned_data_after_newer_versions_are_published(
    tmp_path: Path,
) -> None:
    first = routed_daily_bars(
        (date(2023, 1, 3), date(2023, 1, 4), date(2023, 1, 5)),
        fetched_at=datetime(2023, 1, 5, 8, tzinfo=UTC),
    )
    newer = routed_daily_bars(
        (date(2023, 1, 3), date(2023, 1, 4), date(2023, 1, 5)),
        fetched_at=datetime(2023, 1, 6, 8, tzinfo=UTC),
        volume_delta=-100,
    )
    status_start = first.result.query.start.astimezone(SHANGHAI).date()
    status_end = first.result.query.end.astimezone(SHANGHAI).date()
    first_status = _routed_status(
        symbol=first.result.query.symbol,
        start=status_start,
        end=status_end,
        fetched_at=datetime(2023, 1, 5, 9, tzinfo=UTC),
        upper_limit=Decimal("12"),
    )
    newer_status = _routed_status(
        symbol=first.result.query.symbol,
        start=status_start,
        end=status_end,
        fetched_at=datetime(2023, 1, 6, 9, tzinfo=UTC),
        upper_limit=Decimal("13"),
    )

    with open_catalog_engine(tmp_path) as engine:
        market_lake = MarketLake(engine=engine, root=tmp_path / "market")
        status_lake = ExecutionStatusLake(engine)
        pinned = market_lake.write(first)
        pinned_status = status_lake.write(first_status)
        reference = PinnedMarketRef(
            symbol=first.result.query.symbol,
            signal_manifest_record_id=pinned.manifest_record_id,
            signal_dataset_version=pinned.dataset_version,
            signal_route_version=first.manifest.route_version,
            signal_source=ProviderId.TUSHARE,
            signal_data_cutoff=first.result.provenance.data_cutoff,
            signal_query=first.result.query,
            execution_manifest_record_id=pinned.manifest_record_id,
            execution_dataset_version=pinned.dataset_version,
            execution_route_version=first.manifest.route_version,
            execution_source=first.result.provenance.source,
            execution_data_cutoff=first.result.provenance.data_cutoff,
            execution_query=first.result.query,
            execution_status_manifest_record_id=pinned_status.manifest_record_id,
            execution_status_dataset_version=pinned_status.dataset_version,
            execution_status_route_version=first_status.manifest.route_version,
            execution_status_source=first_status.manifest.selected_source,
            execution_status_data_cutoff=first_status.manifest.upstream_data_cutoff,
            execution_status_query=first_status.result.query,
        )
        snapshot = freeze_request(
            BacktestRequest(
                scope_kind="single",
                scope_id=None,
                scope_revision_or_snapshot_id=None,
                instrument_dataset_version="sha256:" + "1" * 64,
                symbols=(first.result.query.symbol,),
                formula_version_id="macd-v1",
                formula_checksum="sha256:" + "2" * 64,
                formula_engine_version="formula-engine-v1",
                compatibility_version="tdx-v1",
                formula_parameters=(),
                warmup_policy_version="formula-warmup-v1",
                symbol_inputs=(reference,),
                period=first.result.query.period,
                adjustment=first.result.query.adjustment,
                scoring_start=first.result.query.start,
                scoring_end=first.result.query.end,
                quantity_shares=1_000,
                commission_bps=Decimal("2.5"),
                minimum_commission=Decimal("5"),
                sell_tax_bps=Decimal("5"),
                slippage_bps=Decimal("3"),
                cost_model_version="a-share-cost-v1",
                backtest_engine_version="backtest-engine-v1",
                execution_rules_version="a-share-v1",
            )
        )
        expected = reopen_snapshot(
            snapshot,
            market_lake=market_lake,
            status_lake=status_lake,
        ).canonical_bytes()

        latest = market_lake.write(newer)
        latest_status = status_lake.write(newer_status)
        assert latest.manifest_record_id != pinned.manifest_record_id
        resolved_market = market_lake.latest_exact(first.result.query)
        assert resolved_market is not None
        assert resolved_market.manifest_record_id == latest.manifest_record_id
        assert latest_status.manifest_record_id != pinned_status.manifest_record_id
        resolved_status = status_lake.latest_exact(first_status.result.query)
        assert resolved_status is not None
        assert resolved_status.manifest_record_id == latest_status.manifest_record_id

        replayed = reopen_snapshot(
            snapshot,
            market_lake=market_lake,
            status_lake=status_lake,
        )

    assert replayed.canonical_bytes() == expected
    assert replayed.symbols[0].signal == first
    assert replayed.symbols[0].execution == first
    assert replayed.symbols[0].execution_status == first_status


def test_reopen_validates_pinned_identity_instead_of_trusting_reader(
    tmp_path: Path,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))

    with open_catalog_engine(tmp_path) as engine:
        market_lake = MarketLake(engine=engine, root=tmp_path / "market")
        status_lake = ExecutionStatusLake(engine)
        stored = market_lake.write(routed)
        ref = PinnedMarketRef(
            symbol=routed.result.query.symbol,
            signal_manifest_record_id=stored.manifest_record_id,
            signal_dataset_version="sha256:" + "9" * 64,
            signal_route_version=routed.manifest.route_version,
            signal_source=routed.result.provenance.source,
            signal_data_cutoff=routed.result.provenance.data_cutoff,
            signal_query=routed.result.query,
            execution_manifest_record_id=stored.manifest_record_id,
            execution_dataset_version=stored.dataset_version,
            execution_route_version=routed.manifest.route_version,
            execution_source=routed.result.provenance.source,
            execution_data_cutoff=routed.result.provenance.data_cutoff,
            execution_query=routed.result.query,
            execution_status_manifest_record_id=DIGEST_STATUS_V1,
            execution_status_dataset_version=DIGEST_STATUS_V1,
            execution_status_route_version=DIGEST_STATUS_V1,
            execution_status_source=ProviderId.TUSHARE,
            execution_status_data_cutoff=routed.result.provenance.data_cutoff,
            execution_status_query=ExecutionStatusQuery(
                symbol=routed.result.query.symbol,
                exchange=Exchange.SH,
                start=routed.result.query.start.date(),
                end=routed.result.query.end.date(),
                period=routed.result.query.period,
            ),
        )
        request = BacktestRequest(
            scope_kind="single",
            instrument_dataset_version="sha256:" + "1" * 64,
            symbols=(routed.result.query.symbol,),
            formula_version_id="macd-v1",
            formula_checksum="sha256:" + "2" * 64,
            formula_engine_version="formula-engine-v1",
            compatibility_version="tdx-v1",
            formula_parameters=(),
            symbol_inputs=(ref,),
            period=routed.result.query.period,
            adjustment=routed.result.query.adjustment,
            scoring_start=routed.result.query.start,
            scoring_end=routed.result.query.end,
            commission_bps=Decimal("2.5"),
            minimum_commission=Decimal("5"),
            sell_tax_bps=Decimal("5"),
            slippage_bps=Decimal("3"),
            backtest_engine_version="backtest-engine-v1",
        )
        snapshot = freeze_request(request)

        with pytest.raises(ValueError, match="signal dataset version"):
            reopen_snapshot(
                snapshot,
                market_lake=market_lake,
                status_lake=status_lake,
            )
