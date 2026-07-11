from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import threading

import pytest

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


def _successes(*symbols: str) -> dict[str, object]:
    return {
        symbol: routed_daily_bars((date(2024, 1, 2),), symbol=symbol)
        for symbol in symbols
    }


def test_cancel_before_first_boundary_records_all_remaining_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("600000.SH", "000001.SZ")
    with open_update_harness(tmp_path) as harness:
        router = SpyRouter(_successes(*symbols))
        service = UpdateService(
            router=router,
            lake=harness.lake,
            tasks=harness.tasks,
            engine=harness.engine,
        )
        worker = TaskWorker(harness.tasks, worker_id="worker-cancel-first")
        register_market_update(worker, service)
        created = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(*symbols),
        )
        original_claim = harness.tasks.claim_next

        def claim_then_cancel(
            worker_id: str, *, stop_event: threading.Event | None = None
        ) -> object:
            claimed = original_claim(worker_id, stop_event=stop_event)
            assert claimed is not None
            harness.tasks.request_cancel(claimed.id)
            return claimed

        monkeypatch.setattr(harness.tasks, "claim_next", claim_then_cancel)

        completed = worker.run_once()

        assert completed is not None
        assert completed.status == "cancelled"
        assert completed.result is None
        assert completed.error is None
        assert router.calls == []
        items = MarketUpdateItemRepository(harness.engine).list_for_task(created.id)
        assert [(item.ordinal, item.status, item.reason) for item in items] == [
            (0, "cancelled", "cancel_requested"),
            (1, "cancelled", "cancel_requested"),
        ]
        progress_events = [
            event
            for event in harness.tasks.list_events(created.id)
            if event.event_name == "task.progressed"
        ]
        assert len(progress_events) == 1
        assert progress_events[0].detail == {
            "stage": "finalizing",
            "processed": 2,
            "total": 2,
            "current_symbol": None,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 2,
        }


def test_cancel_between_symbols_keeps_current_success_and_skips_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("600000.SH", "000001.SZ", "000002.SZ")
    with open_update_harness(tmp_path) as harness:
        router = SpyRouter(_successes(*symbols))
        service = UpdateService(
            router=router,
            lake=harness.lake,
            tasks=harness.tasks,
            engine=harness.engine,
        )
        worker = TaskWorker(harness.tasks, worker_id="worker-cancel-between")
        register_market_update(worker, service)
        created = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(*symbols),
        )
        original_write = harness.lake.write
        cancelled = False

        def write_then_cancel(routed: object) -> object:
            nonlocal cancelled
            stored = original_write(routed)  # type: ignore[arg-type]
            if not cancelled:
                cancelled = True
                harness.tasks.request_cancel(created.id)
            return stored

        monkeypatch.setattr(harness.lake, "write", write_then_cancel)

        completed = worker.run_once()

        assert completed is not None
        assert completed.status == "cancelled"
        assert completed.result is None
        assert [query.symbol for query, _previous in router.calls] == [symbols[0]]
        items = MarketUpdateItemRepository(harness.engine).list_for_task(created.id)
        assert [(item.symbol, item.status, item.reason) for item in items] == [
            (symbols[0], "succeeded", None),
            (symbols[1], "cancelled", "cancel_requested"),
            (symbols[2], "cancelled", "cancel_requested"),
        ]


def test_cancel_during_lake_write_finishes_current_symbol_then_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("600000.SH", "000001.SZ")
    with open_update_harness(tmp_path) as harness:
        router = SpyRouter(_successes(*symbols))
        service = UpdateService(
            router=router,
            lake=harness.lake,
            tasks=harness.tasks,
            engine=harness.engine,
        )
        worker = TaskWorker(harness.tasks, worker_id="worker-cancel-write")
        register_market_update(worker, service)
        created = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(*symbols),
        )
        original_write = harness.lake.write
        entered = threading.Event()
        release = threading.Event()

        def blocked_write(routed: object) -> object:
            entered.set()
            assert release.wait(timeout=5)
            return original_write(routed)  # type: ignore[arg-type]

        monkeypatch.setattr(harness.lake, "write", blocked_write)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker.run_once)
            assert entered.wait(timeout=5)
            cancelling = harness.tasks.request_cancel(created.id)
            assert cancelling.cancel_requested is True
            release.set()
            completed = future.result(timeout=10)

        assert completed is not None
        assert completed.status == "cancelled"
        assert completed.result is None
        assert [query.symbol for query, _previous in router.calls] == [symbols[0]]
        items = MarketUpdateItemRepository(harness.engine).list_for_task(created.id)
        assert [(item.symbol, item.status, item.reason) for item in items] == [
            (symbols[0], "succeeded", None),
            (symbols[1], "cancelled", "cancel_requested"),
        ]
