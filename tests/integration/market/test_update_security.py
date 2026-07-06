from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import logging
from pathlib import Path
import threading

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.exc import IntegrityError

from stock_desk.market.update import (
    MARKET_UPDATE_TASK_KIND,
    MarketUpdateItemConflict,
    MarketUpdateItemNotFound,
    MarketUpdateItemRepository,
    MarketUpdateItemValidationError,
    UpdateService,
    register_market_update,
)
from stock_desk.storage.models import MarketUpdateItem
from stock_desk.tasks.worker import TaskWorker
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.integration.market.update_test_helpers import (
    SpyRouter,
    open_update_harness,
    update_payload,
)


@pytest.mark.parametrize("failure_layer", ["router", "lake"])
def test_system_failure_is_generic_and_preserves_prior_success_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_layer: str,
) -> None:
    secret = "TOP-SECRET-provider-token"
    symbols = ("600000.SH", "000001.SZ")
    outcomes = {
        symbol: routed_daily_bars((date(2024, 1, 2),), symbol=symbol)
        for symbol in symbols
    }
    with open_update_harness(tmp_path) as harness:
        router = SpyRouter(outcomes)
        original_fetch = router.fetch_bars
        original_write = harness.lake.write
        fetch_calls = 0
        write_calls = 0

        def fail_second_fetch(*args: object, **kwargs: object) -> object:
            nonlocal fetch_calls
            fetch_calls += 1
            if failure_layer == "router" and fetch_calls == 2:
                raise RuntimeError(secret)
            return original_fetch(*args, **kwargs)  # type: ignore[arg-type]

        def fail_second_write(routed: object) -> object:
            nonlocal write_calls
            write_calls += 1
            if failure_layer == "lake" and write_calls == 2:
                raise RuntimeError(secret)
            return original_write(routed)  # type: ignore[arg-type]

        monkeypatch.setattr(router, "fetch_bars", fail_second_fetch)
        monkeypatch.setattr(harness.lake, "write", fail_second_write)
        service = UpdateService(
            router=router,
            lake=harness.lake,
            tasks=harness.tasks,
            engine=harness.engine,
        )
        worker = TaskWorker(harness.tasks, worker_id=f"worker-{failure_layer}")
        register_market_update(worker, service)
        created = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(*symbols),
        )

        with caplog.at_level(logging.WARNING, logger="stock_desk.tasks.worker"):
            completed = worker.run_once()

        assert completed is not None
        assert completed.status == "failed"
        assert completed.error == {"code": "task_handler_failed"}
        assert completed.result is None
        items = MarketUpdateItemRepository(harness.engine).list_for_task(created.id)
        assert [(item.symbol, item.status) for item in items] == [
            (symbols[0], "succeeded")
        ]
        persisted = repr((completed, items, harness.tasks.list_events(created.id)))
        logged = " ".join(record.getMessage() for record in caplog.records)
        assert secret not in persisted
        assert secret not in logged
        assert "RuntimeError" in logged


def test_item_repository_rejects_missing_wrong_kind_and_unsafe_reason(
    tmp_path: Path,
) -> None:
    with open_update_harness(tmp_path) as harness:
        items = MarketUpdateItemRepository(harness.engine)
        with pytest.raises(MarketUpdateItemNotFound):
            items.list_for_task("missing")

        wrong = harness.tasks.create("demo.double", {})
        with pytest.raises(MarketUpdateItemConflict):
            items.list_for_task(wrong.id)

        update_task = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload("600000.SH"),
        )
        assert harness.tasks.claim_next("worker-reason") is not None
        with pytest.raises(MarketUpdateItemValidationError):
            items.record_failure(
                task_id=update_task.id,
                ordinal=0,
                symbol="600000.SH",
                reason="routing:no_provider:TOP-SECRET",
            )
        assert items.list_for_task(update_task.id) == []


@pytest.mark.parametrize("outcome", ["failure", "success"])
def test_item_insert_cannot_commit_after_task_becomes_terminal(
    tmp_path: Path,
    outcome: str,
) -> None:
    with open_update_harness(tmp_path) as harness:
        stored = harness.lake.write(
            routed_daily_bars((date(2024, 1, 2),), symbol="600000.SH")
        )
        task = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload("600000.SH"),
        )
        assert harness.tasks.claim_next("worker-late-item") is not None
        items = MarketUpdateItemRepository(harness.engine)
        insert_waiting = threading.Event()
        release_insert = threading.Event()
        insert_thread: int | None = None

        def pause_item_insert(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if (
                threading.get_ident() == insert_thread
                and statement.lstrip()
                .upper()
                .startswith("INSERT INTO MARKET_UPDATE_ITEM")
            ):
                insert_waiting.set()
                assert release_insert.wait(timeout=5)

        def insert_item() -> object:
            nonlocal insert_thread
            insert_thread = threading.get_ident()
            if outcome == "success":
                return items.record_success(
                    task_id=task.id,
                    ordinal=0,
                    symbol="600000.SH",
                    stored=stored,
                )
            return items.record_failure(
                task_id=task.id,
                ordinal=0,
                symbol="600000.SH",
                reason="routing:no_provider",
            )

        event.listen(harness.engine, "before_cursor_execute", pause_item_insert)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(insert_item)
                assert insert_waiting.wait(timeout=5)
                terminal = harness.tasks.complete(task.id, {"done": True})
                assert terminal.status == "succeeded"
                release_insert.set()
                with pytest.raises(MarketUpdateItemConflict):
                    future.result(timeout=10)
        finally:
            release_insert.set()
            event.remove(
                harness.engine,
                "before_cursor_execute",
                pause_item_insert,
            )

        assert harness.tasks.get(task.id).status == "succeeded"
        assert items.list_for_task(task.id) == []


def test_success_item_fk_binding_and_database_immutability(
    tmp_path: Path,
) -> None:
    with open_update_harness(tmp_path) as harness:
        routed = routed_daily_bars((date(2024, 1, 2),), symbol="600000.SH")
        stored = harness.lake.write(routed)
        task = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload("600000.SH"),
        )
        assert harness.tasks.claim_next("worker-fk") is not None
        items = MarketUpdateItemRepository(harness.engine)

        with pytest.raises(MarketUpdateItemConflict):
            items.record_success(
                task_id=task.id,
                ordinal=0,
                symbol="000001.SZ",
                stored=stored,
            )
        inserted = items.record_success(
            task_id=task.id,
            ordinal=0,
            symbol="600000.SH",
            stored=stored,
        )

        for statement in (
            update(MarketUpdateItem)
            .where(MarketUpdateItem.task_id == task.id)
            .values(reason="forbidden"),
            delete(MarketUpdateItem).where(MarketUpdateItem.task_id == task.id),
        ):
            with pytest.raises(IntegrityError):
                with harness.engine.begin() as connection:
                    connection.execute(statement)

        assert items.list_for_task(task.id) == [inserted]
