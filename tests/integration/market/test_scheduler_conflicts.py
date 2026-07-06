from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import event, func, insert, select
from sqlalchemy.engine import Connection

from stock_desk.market.scheduler import MarketUpdateScheduler
from stock_desk.storage.models import (
    MarketUpdateOccurrence,
    MarketUpdateSchedule,
    TaskEvent,
    TaskRun,
)
from tests.integration.market.scheduler_test_helpers import (
    scheduler_database,
    valid_update_payload,
)


DUE_NOW = datetime(2026, 7, 6, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_enqueue_writes_occurrence_before_task_event_and_schedule(
    tmp_path: Path,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedules.create(local_time=time(18), payload=valid_update_payload())
    writes: list[str] = []

    def capture_write(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower()
        for table in (
            "market_update_occurrence",
            "task_run",
            "task_event",
            "market_update_schedule",
        ):
            if normalized.startswith((f"insert into {table}", f"update {table}")):
                writes.append(table)

    event.listen(engine, "before_cursor_execute", capture_write)
    try:
        MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW).tick()
    finally:
        event.remove(engine, "before_cursor_execute", capture_write)
        engine.dispose()
    assert writes == [
        "market_update_occurrence",
        "task_run",
        "task_event",
        "market_update_schedule",
    ]


def test_only_confirmed_same_occurrence_duplicate_is_a_noop(tmp_path: Path) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    existing = tasks.create("test.kind", {"value": 1})
    with engine.begin() as connection:
        connection.execute(
            insert(MarketUpdateOccurrence).values(
                schedule_id=schedule.id,
                local_date=date(2026, 7, 6),
                task_id=existing.id,
                created_at=DUE_NOW,
            )
        )
    try:
        assert (
            MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW).tick() == ()
        )
        with engine.connect() as connection:
            assert connection.execute(select(func.count(TaskRun.id))).scalar_one() == 1
            assert (
                connection.execute(select(func.count(TaskEvent.id))).scalar_one() == 1
            )
            assert (
                connection.execute(
                    select(func.count()).select_from(MarketUpdateOccurrence)
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    select(MarketUpdateSchedule.last_enqueued_local_date).where(
                        MarketUpdateSchedule.id == schedule.id
                    )
                ).scalar_one()
                is None
            )
    finally:
        engine.dispose()
