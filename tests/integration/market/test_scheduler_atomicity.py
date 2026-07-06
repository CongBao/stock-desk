from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, event, func, insert, select, update
from sqlalchemy.engine import Connection

import stock_desk.market.scheduler as scheduler_module
from stock_desk.market.scheduler import (
    MarketUpdateScheduleConflict,
    MarketUpdateScheduleValidationError,
    MarketUpdateScheduler,
)
from stock_desk.storage.models import (
    MarketUpdateOccurrence,
    MarketUpdateSchedule,
    TaskEvent,
    TaskRun,
)
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import TaskNotFound, TaskRepository
from tests.integration.market.scheduler_test_helpers import (
    scheduler_database,
    valid_update_payload,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
LOCAL_DATE = date(2026, 7, 6)
DUE_NOW = datetime(2026, 7, 6, 20, 0, tzinfo=SHANGHAI)


def _state(engine: Engine, schedule_id: str) -> tuple[int, int, int, date | None]:
    with engine.connect() as connection:
        return (
            int(connection.execute(select(func.count(TaskRun.id))).scalar_one()),
            int(connection.execute(select(func.count(TaskEvent.id))).scalar_one()),
            int(
                connection.execute(
                    select(func.count()).select_from(MarketUpdateOccurrence)
                ).scalar_one()
            ),
            connection.execute(
                select(MarketUpdateSchedule.last_enqueued_local_date).where(
                    MarketUpdateSchedule.id == schedule_id
                )
            ).scalar_one(),
        )


@contextmanager
def _fail_statement(engine: Engine, fragment: str) -> Iterator[None]:
    def fail_matching_statement(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if fragment in statement.lower():
            raise RuntimeError(f"injected failure: {fragment}")

    event.listen(engine, "before_cursor_execute", fail_matching_statement)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", fail_matching_statement)


@pytest.mark.parametrize(
    "statement_fragment",
    (
        "insert into market_update_occurrence",
        "insert into task_run",
        "insert into task_event",
        "update market_update_schedule",
    ),
    ids=("occurrence", "task", "event", "schedule"),
)
def test_failure_at_each_write_boundary_rolls_back_everything(
    tmp_path: Path,
    statement_fragment: str,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    scheduler = MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW)
    try:
        with _fail_statement(engine, statement_fragment):
            with pytest.raises(RuntimeError, match="injected failure"):
                scheduler.tick()
        assert _state(engine, schedule.id) == (0, 0, 0, None)
    finally:
        engine.dispose()


def test_commit_failure_rolls_back_and_retry_uses_a_new_task_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    failed_id = "11111111-1111-4111-8111-111111111111"
    retry_id = "22222222-2222-4222-8222-222222222222"
    ids = iter((UUID(failed_id), UUID(retry_id)))
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: next(ids))

    def fail_commit(_connection: Connection) -> None:
        raise RuntimeError("injected commit failure")

    event.listen(engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failure"):
            MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW).tick()
    finally:
        event.remove(engine, "commit", fail_commit)

    try:
        assert _state(engine, schedule.id) == (0, 0, 0, None)
        with pytest.raises(TaskNotFound):
            tasks.get(failed_id)

        enqueued = MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW).tick()
        assert [task.id for task in enqueued] == [retry_id]
        assert failed_id not in {
            occurrence.task_id for occurrence in schedules.list_occurrences(schedule.id)
        }
        assert _state(engine, schedule.id) == (1, 1, 1, LOCAL_DATE)
    finally:
        engine.dispose()


class _DisablingTaskRepository(TaskRepository):
    def __init__(self, engine: Engine, schedule_id: str) -> None:
        super().__init__(engine)
        self._schedule_id = schedule_id

    def enqueue_in_transaction(
        self,
        connection: Connection,
        kind: str,
        payload: Mapping[str, Any],
        *,
        task_id: str,
        now: datetime,
    ) -> TaskSnapshot:
        task = super().enqueue_in_transaction(
            connection, kind, payload, task_id=task_id, now=now
        )
        connection.execute(
            update(MarketUpdateSchedule)
            .where(MarketUpdateSchedule.id == self._schedule_id)
            .values(enabled=False)
        )
        return task


def test_conditional_schedule_update_conflict_rolls_back_all_writes(
    tmp_path: Path,
) -> None:
    _url, engine, schedules, _tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    tasks = _DisablingTaskRepository(engine, schedule.id)
    try:
        with pytest.raises(MarketUpdateScheduleConflict, match="changed"):
            MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW).tick()
        assert _state(engine, schedule.id) == (0, 0, 0, None)
        assert schedules.get(schedule.id).enabled is True
    finally:
        engine.dispose()


def test_invalid_middle_schedule_is_fail_fast_with_per_schedule_commits(
    tmp_path: Path,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    first = schedules.create(local_time=time(9), payload=valid_update_payload())
    broken = schedules.create(local_time=time(10), payload=valid_update_payload())
    last = schedules.create(local_time=time(11), payload=valid_update_payload())
    with engine.begin() as connection:
        connection.execute(
            update(MarketUpdateSchedule)
            .where(MarketUpdateSchedule.id == broken.id)
            .values(payload_json={"symbols": [], "period": "invalid"})
        )
    try:
        with pytest.raises(MarketUpdateScheduleValidationError, match="payload"):
            MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW).tick()

        with engine.connect() as connection:
            occurrences = set(
                connection.execute(select(MarketUpdateOccurrence.schedule_id)).scalars()
            )
            last_dates = dict(
                connection.execute(
                    select(
                        MarketUpdateSchedule.id,
                        MarketUpdateSchedule.last_enqueued_local_date,
                    )
                ).all()
            )
        assert occurrences == {first.id}
        assert last_dates == {first.id: LOCAL_DATE, broken.id: None, last.id: None}
        assert _state(engine, first.id)[:3] == (1, 1, 1)
    finally:
        engine.dispose()


def test_unrelated_integrity_error_is_not_misclassified_as_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    existing = tasks.create("test.kind", {"value": 1})
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    monkeypatch.setattr(scheduler_module, "uuid4", lambda: UUID(existing.id))
    try:
        with pytest.raises(Exception) as captured:
            MarketUpdateScheduler(schedules, tasks, clock=lambda: DUE_NOW).tick()
        assert captured.type.__name__ == "IntegrityError"
        assert _state(engine, schedule.id) == (1, 1, 0, None)
    finally:
        engine.dispose()


def test_occurrence_snapshot_rejects_tampered_non_uuid_task_id(
    tmp_path: Path,
) -> None:
    _url, engine, schedules, _tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        connection.execute(
            insert(MarketUpdateOccurrence).values(
                schedule_id=schedule.id,
                local_date=LOCAL_DATE,
                task_id="tampered-task-id",
                created_at=DUE_NOW,
            )
        )
        connection.commit()
    try:
        with pytest.raises(
            MarketUpdateScheduleValidationError, match="occurrence task id"
        ):
            schedules.list_occurrences(schedule.id)
    finally:
        engine.dispose()
