from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
    derive_source_transition,
    make_routing_manifest,
)
from stock_desk.market.types import MarketCapability, ProviderId
from stock_desk.market.update import (
    MARKET_UPDATE_TASK_KIND,
    MarketUpdateItemConflict,
    MarketUpdateItemRepository,
    UpdateService,
    register_market_update,
)
from stock_desk.tasks.worker import TaskWorker
from stock_desk.tasks.repository import TaskRepository
from tests.integration.market.lake_test_helpers import local_time, routed_daily_bars
from tests.integration.market.update_test_helpers import (
    SpyRouter,
    open_update_harness,
    update_payload,
)


def test_success_item_repository_is_typed_ordered_and_immutable(
    tmp_path: Path,
) -> None:
    with open_update_harness(tmp_path) as harness:
        routed = routed_daily_bars((date(2024, 1, 2),))
        stored = harness.lake.write(routed)
        task = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload("600000.SH"),
        )
        assert harness.tasks.claim_next("worker-item") is not None
        items = MarketUpdateItemRepository(harness.engine)

        inserted = items.record_success(
            task_id=task.id,
            ordinal=0,
            symbol="600000.SH",
            stored=stored,
        )

        assert inserted.task_id == task.id
        assert inserted.ordinal == 0
        assert inserted.symbol == "600000.SH"
        assert inserted.status == "succeeded"
        assert inserted.manifest_record_id == stored.manifest_record_id
        assert inserted.dataset_version == stored.dataset_version
        assert inserted.reason is None
        assert items.list_for_task(task.id) == [inserted]
        with pytest.raises(MarketUpdateItemConflict):
            items.record_success(
                task_id=task.id,
                ordinal=0,
                symbol="600000.SH",
                stored=stored,
            )
        assert items.list_for_task(task.id) == [inserted]


@pytest.mark.parametrize("wrong_state", ["wrong_kind", "queued"])
def test_item_repository_rejects_wrong_task_kind_or_state(
    tmp_path: Path,
    wrong_state: str,
) -> None:
    with open_update_harness(tmp_path) as harness:
        kind = "demo.double" if wrong_state == "wrong_kind" else MARKET_UPDATE_TASK_KIND
        task = harness.tasks.create(kind, {})
        if wrong_state == "wrong_kind":
            assert harness.tasks.claim_next("worker-wrong") is not None
        items = MarketUpdateItemRepository(harness.engine)

        with pytest.raises(MarketUpdateItemConflict):
            items.record_failure(
                task_id=task.id,
                ordinal=0,
                symbol="600000.SH",
                reason="routing:no_provider",
            )

        if wrong_state == "wrong_kind":
            with pytest.raises(MarketUpdateItemConflict):
                items.list_for_task(task.id)
        else:
            assert items.list_for_task(task.id) == []


def test_worker_runs_happy_multi_symbol_update_with_previous_manifest(
    tmp_path: Path,
) -> None:
    first_symbol = "600000.SH"
    second_symbol = "000001.SZ"
    day = date(2024, 1, 2)
    with open_update_harness(tmp_path) as harness:
        previous_routed = routed_daily_bars((day,), symbol=first_symbol)
        previous_stored = harness.lake.write(previous_routed)
        first_base = routed_daily_bars(
            (day,),
            symbol=first_symbol,
            source=ProviderId.BAOSTOCK,
            fetched_at=local_time(day, 17),
        )
        transition = derive_source_transition(
            previous=previous_routed.manifest,
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=first_base.result.query),
            priority=(ProviderId.BAOSTOCK,),
            selected_source=ProviderId.BAOSTOCK,
            upstream_dataset_version=first_base.result.provenance.dataset_version,
            observed_at=None,
        )
        assert transition is not None
        first_routed = RoutedBarSuccess(
            result=first_base.result,
            manifest=make_routing_manifest(
                category=MarketCapability.BARS,
                request=BarRoutingRequest(query=first_base.result.query),
                priority=(ProviderId.BAOSTOCK,),
                attempts=(),
                selected_source=ProviderId.BAOSTOCK,
                upstream_dataset_version=first_base.result.provenance.dataset_version,
                upstream_fetched_at=first_base.result.provenance.fetched_at,
                upstream_data_cutoff=first_base.result.provenance.data_cutoff,
                upstream_adjustment=first_base.result.provenance.adjustment,
                transition=transition,
            ),
        )
        second_routed = routed_daily_bars(
            (day,),
            symbol=second_symbol,
            fetched_at=local_time(day, 17),
        )
        router = SpyRouter({first_symbol: first_routed, second_symbol: second_routed})
        service = UpdateService(
            router=router,
            lake=harness.lake,
            tasks=harness.tasks,
            engine=harness.engine,
        )
        worker = TaskWorker(harness.tasks, worker_id="worker-update")
        register_market_update(worker, service)
        created = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(first_symbol, second_symbol),
        )

        completed = worker.run_once()

        assert completed is not None
        assert completed.id == created.id
        assert completed.status == "succeeded"
        assert completed.result == {
            "total": 2,
            "succeeded": 2,
            "failed": 0,
            "cancelled": 0,
        }
        assert [query.symbol for query, _previous in router.calls] == [
            first_symbol,
            second_symbol,
        ]
        assert (
            router.calls[0][1]
            == harness.lake.read(previous_stored.manifest_record_id).manifest
        )
        assert router.calls[1][1] is None

        items = MarketUpdateItemRepository(harness.engine).list_for_task(created.id)
        assert [(item.ordinal, item.symbol, item.status) for item in items] == [
            (0, first_symbol, "succeeded"),
            (1, second_symbol, "succeeded"),
        ]
        assert all(item.manifest_record_id is not None for item in items)
        assert all(item.dataset_version is not None for item in items)
        assert items[0].manifest_record_id is not None
        assert harness.lake.read(items[0].manifest_record_id).manifest.transition == (
            transition
        )

        progress_events = [
            event
            for event in harness.tasks.list_events(created.id)
            if event.event_name == "task.progressed"
        ]
        assert [event.detail["stage"] for event in progress_events] == [
            "routing",
            "persisting",
            "routing",
            "persisting",
            "finalizing",
        ]
        assert [event.detail["processed"] for event in progress_events] == [
            0,
            0,
            1,
            1,
            2,
        ]
        assert [event.detail["current_symbol"] for event in progress_events] == [
            first_symbol,
            first_symbol,
            second_symbol,
            second_symbol,
            None,
        ]
        assert all(event.progress is not None for event in progress_events)
        progress = [float(event.progress) for event in progress_events]
        assert progress == sorted(progress)
        assert all(value < 1 for value in progress)


def test_market_update_resumes_after_a_durable_desktop_checkpoint(
    tmp_path: Path,
) -> None:
    first_symbol = "600000.SH"
    second_symbol = "000001.SZ"
    day = date(2024, 1, 2)
    with open_update_harness(tmp_path) as harness:
        routed = {
            first_symbol: routed_daily_bars((day,), symbol=first_symbol),
            second_symbol: routed_daily_bars((day,), symbol=second_symbol),
        }
        first_router = SpyRouter(routed)
        first_worker = TaskWorker(harness.tasks, worker_id="worker-before-exit")
        register_market_update(
            first_worker,
            UpdateService(
                router=first_router,
                lake=harness.lake,
                tasks=harness.tasks,
                engine=harness.engine,
            ),
        )
        task = harness.tasks.create(
            MARKET_UPDATE_TASK_KIND,
            update_payload(first_symbol, second_symbol),
        )
        harness.tasks.request_desktop_checkpoint()

        paused = first_worker.run_once()

        assert paused is not None and paused.status == "running"
        assert [query.symbol for query, _ in first_router.calls] == [first_symbol]
        assert [
            (item.ordinal, item.symbol)
            for item in MarketUpdateItemRepository(harness.engine).list_for_task(
                task.id
            )
        ] == [(0, first_symbol)]
        assert any(
            event.event_name == "task.desktop_checkpointed"
            for event in harness.tasks.list_events(task.id)
        )

        resumed_tasks = TaskRepository(harness.engine)
        assert resumed_tasks.resume_desktop_recovery() == 1
        second_router = SpyRouter(routed)
        second_worker = TaskWorker(resumed_tasks, worker_id="worker-after-restart")
        register_market_update(
            second_worker,
            UpdateService(
                router=second_router,
                lake=harness.lake,
                tasks=resumed_tasks,
                engine=harness.engine,
            ),
        )

        completed = second_worker.run_once()

        assert completed is not None and completed.status == "succeeded"
        assert [query.symbol for query, _ in second_router.calls] == [second_symbol]
        assert [
            (item.ordinal, item.symbol)
            for item in MarketUpdateItemRepository(harness.engine).list_for_task(
                task.id
            )
        ] == [(0, first_symbol), (1, second_symbol)]
