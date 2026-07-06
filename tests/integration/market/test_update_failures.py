from __future__ import annotations

from datetime import date
from pathlib import Path

from stock_desk.market.routing import SourceRouter
from stock_desk.market.update import (
    MARKET_UPDATE_TASK_KIND,
    MarketUpdateItemRepository,
    UpdateService,
    register_market_update,
)
from stock_desk.tasks.worker import TaskWorker
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.integration.market.update_test_helpers import (
    SpyRouter,
    open_update_harness,
    update_payload,
)


def test_partial_routing_failure_is_safe_and_continues_next_symbol(
    tmp_path: Path,
) -> None:
    failed_symbol = "600000.SH"
    succeeded_symbol = "000001.SZ"
    failed_query = routed_daily_bars(
        (date(2024, 1, 2),), symbol=failed_symbol
    ).result.query
    failure = SourceRouter([]).fetch_bars(failed_query)
    success = routed_daily_bars((date(2024, 1, 2),), symbol=succeeded_symbol)
    with open_update_harness(tmp_path) as harness:
        router = SpyRouter({failed_symbol: failure, succeeded_symbol: success})
        service = UpdateService(
            router=router,
            lake=harness.lake,
            tasks=harness.tasks,
            engine=harness.engine,
        )
        worker = TaskWorker(harness.tasks, worker_id="worker-partial")
        register_market_update(worker, service)
        created = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(failed_symbol, succeeded_symbol),
        )

        completed = worker.run_once()

        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.result == {
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "cancelled": 0,
        }
        assert [query.symbol for query, _previous in router.calls] == [
            failed_symbol,
            succeeded_symbol,
        ]
        items = MarketUpdateItemRepository(harness.engine).list_for_task(created.id)
        assert [(item.symbol, item.status, item.reason) for item in items] == [
            (failed_symbol, "failed", "routing:no_provider"),
            (succeeded_symbol, "succeeded", None),
        ]
        serialized = repr((completed, items, harness.tasks.list_events(created.id)))
        assert "no configured provider can satisfy this query" not in serialized
        assert "detail" not in repr(items)


def test_all_routing_failures_complete_batch_normally_and_finalize(
    tmp_path: Path,
) -> None:
    symbols = ("600000.SH", "000001.SZ")
    failures = {
        symbol: SourceRouter([]).fetch_bars(
            routed_daily_bars((date(2024, 1, 2),), symbol=symbol).result.query
        )
        for symbol in symbols
    }
    with open_update_harness(tmp_path) as harness:
        router = SpyRouter(failures)
        service = UpdateService(
            router=router,
            lake=harness.lake,
            tasks=harness.tasks,
            engine=harness.engine,
        )
        worker = TaskWorker(harness.tasks, worker_id="worker-all-fail")
        register_market_update(worker, service)
        created = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(*symbols),
        )

        completed = worker.run_once()

        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.result == {
            "total": 2,
            "succeeded": 0,
            "failed": 2,
            "cancelled": 0,
        }
        items = MarketUpdateItemRepository(harness.engine).list_for_task(created.id)
        assert [(item.ordinal, item.status, item.reason) for item in items] == [
            (0, "failed", "routing:no_provider"),
            (1, "failed", "routing:no_provider"),
        ]
        progress_events = [
            event
            for event in harness.tasks.list_events(created.id)
            if event.event_name == "task.progressed"
        ]
        assert [event.detail["stage"] for event in progress_events] == [
            "routing",
            "routing",
            "finalizing",
        ]
        assert progress_events[-1].detail == {
            "stage": "finalizing",
            "processed": 2,
            "total": 2,
            "current_symbol": None,
            "succeeded": 0,
            "failed": 2,
            "cancelled": 0,
        }
