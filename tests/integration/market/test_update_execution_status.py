from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusQuery,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.provenance import (
    ExecutionStatusRoutingRequest,
    RoutedExecutionStatusFailure,
    RoutedExecutionStatusSuccess,
    make_failure_audit,
    make_routing_manifest,
)
from stock_desk.market.types import (
    Exchange,
    FailureReason,
    MarketCapability,
    ProviderId,
)
from stock_desk.market.update import MARKET_UPDATE_TASK_KIND, UpdateService
from stock_desk.tasks.worker import TaskWorker
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.integration.market.update_test_helpers import (
    SpyRouter,
    open_update_harness,
    update_payload,
)


def _status(query: ExecutionStatusQuery) -> RoutedExecutionStatusSuccess:
    result = materialize_execution_status(
        query=query,
        days=(
            ExecutionStatusDay(
                day=query.start,
                exchange=query.exchange,
                is_exchange_open=True,
                suspension_state=SuspensionState.NORMAL,
                raw_upper_limit=Decimal("11"),
                raw_lower_limit=Decimal("9"),
            ),
        ),
        raw_opens=(
            RawExecutionOpen(
                timestamp=datetime(2024, 1, 2, 1, 30, tzinfo=timezone.utc),
                trading_day=query.start,
                raw_open=Decimal("10"),
            ),
        ),
        source=ProviderId.TUSHARE,
        fetched_at=datetime(2024, 1, 3, tzinfo=timezone.utc),
        data_cutoff=datetime(2024, 1, 2, 7, tzinfo=timezone.utc),
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


class StatusSpyRouter(SpyRouter):
    def __init__(self, bars, *, status_available: bool) -> None:
        super().__init__(bars)
        self.status_available = status_available
        self.status_calls: list[ExecutionStatusQuery] = []

    def fetch_execution_status(self, query, *, previous_manifest=None):
        self.status_calls.append(query)
        if self.status_available:
            return _status(query)
        request = ExecutionStatusRoutingRequest(query=query)
        return RoutedExecutionStatusFailure(
            query=query,
            reason=FailureReason.NO_PROVIDER,
            detail="no configured provider can satisfy this request",
            audit=make_failure_audit(
                category=MarketCapability.EXECUTION_STATUS,
                request=request,
                priority=(),
                attempts=(),
            ),
        )


def test_update_caches_status_independently_and_never_infers_missing_as_tradable(
    tmp_path: Path,
) -> None:
    symbol = "600000.SH"
    with open_update_harness(tmp_path) as harness:
        bars = routed_daily_bars(
            (date(2024, 1, 2),),
            symbol=symbol,
            source=ProviderId.BAOSTOCK,
        )
        status_lake = ExecutionStatusLake(harness.engine)
        router = StatusSpyRouter({symbol: bars}, status_available=True)
        worker = TaskWorker(harness.tasks, worker_id="status-cache")
        worker.register(
            MARKET_UPDATE_TASK_KIND,
            UpdateService(
                router=router,  # type: ignore[arg-type]
                lake=harness.lake,
                tasks=harness.tasks,
                engine=harness.engine,
                execution_status_lake=status_lake,
            ).handle,
        )
        harness.tasks.create(MARKET_UPDATE_TASK_KIND, update_payload(symbol))

        completed = worker.run_once()

        assert completed is not None and completed.status == "succeeded"
        status_query = router.status_calls[0]
        stored_status = status_lake.latest_exact(status_query)
        assert stored_status is not None
        assert (
            status_lake.read(stored_status.manifest_record_id).result.source
            is ProviderId.TUSHARE
        )
        assert harness.lake.latest_exact(bars.result.query) is not None

        missing_symbol = "000001.SZ"
        missing_bars = routed_daily_bars(
            (date(2024, 1, 2),),
            symbol=missing_symbol,
            source=ProviderId.BAOSTOCK,
        )
        unavailable_router = StatusSpyRouter(
            {missing_symbol: missing_bars}, status_available=False
        )
        missing_lake = ExecutionStatusLake(harness.engine)
        missing_query = ExecutionStatusQuery(
            symbol=missing_symbol,
            exchange=Exchange.SZ,
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
        )
        second_worker = TaskWorker(harness.tasks, worker_id="status-missing")
        second_worker.register(
            MARKET_UPDATE_TASK_KIND,
            UpdateService(
                router=unavailable_router,  # type: ignore[arg-type]
                lake=harness.lake,
                tasks=harness.tasks,
                engine=harness.engine,
                execution_status_lake=missing_lake,
            ).handle,
        )
        harness.tasks.create(MARKET_UPDATE_TASK_KIND, update_payload(missing_symbol))
        missing_completed = second_worker.run_once()

        assert missing_completed is not None and missing_completed.status == "succeeded"
        assert missing_lake.latest_exact(missing_query) is None
