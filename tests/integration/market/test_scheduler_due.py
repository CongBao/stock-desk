from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, func, select, update
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


SHANGHAI = ZoneInfo("Asia/Shanghai")


class _NullOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


class _RaisingOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise RuntimeError("invalid offset")

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


class _CountingClock:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self.value


def _counts(engine: Any) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return (
            int(connection.execute(select(func.count(TaskRun.id))).scalar_one()),
            int(connection.execute(select(func.count(TaskEvent.id))).scalar_one()),
            int(
                connection.execute(
                    select(func.count()).select_from(MarketUpdateOccurrence)
                ).scalar_one()
            ),
        )


@pytest.mark.parametrize(
    "clock_value",
    [
        "2026-07-06T18:00:00+08:00",
        datetime(2026, 7, 6, 18, 0),
        datetime(2026, 7, 6, 18, 0, tzinfo=_NullOffset()),
        datetime(2026, 7, 6, 18, 0, tzinfo=_RaisingOffset()),
    ],
    ids=("non-datetime", "naive", "null-offset", "raising-offset"),
)
def test_tick_rejects_invalid_clock_once_without_database_changes(
    tmp_path: Path,
    clock_value: object,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedules.create(local_time=time(18, 0), payload=valid_update_payload())
    clock = _CountingClock(clock_value)
    scheduler = MarketUpdateScheduler(schedules, tasks, clock=clock)
    before = _counts(engine)
    statements: list[str] = []

    def record_statement(
        _connection: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    try:
        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            with pytest.raises(ValueError, match="aware datetime"):
                scheduler.tick()
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)
        assert clock.calls == 1
        assert statements == []
        assert _counts(engine) == before
        assert schedules.list()[0].last_enqueued_local_date is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("enabled", "local_now", "expected_count"),
    [
        (False, time(18, 0), 0),
        (True, time(17, 59), 0),
        (True, time(18, 0), 1),
        (True, time(20, 15), 1),
    ],
    ids=("disabled", "before", "exact", "missed-current-date"),
)
def test_tick_due_matrix_and_canonical_task_payload(
    tmp_path: Path,
    enabled: bool,
    local_now: time,
    expected_count: int,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(
        local_time=time(18, 0),
        payload=valid_update_payload(),
        enabled=enabled,
    )
    sampled = datetime.combine(date(2026, 7, 6), local_now, SHANGHAI)
    clock = _CountingClock(sampled)
    scheduler = MarketUpdateScheduler(schedules, tasks, clock=clock)
    try:
        enqueued = scheduler.tick()

        assert clock.calls == 1
        assert len(enqueued) == expected_count
        assert len(schedules.list_occurrences(schedule.id)) == expected_count
        refreshed = schedules.get(schedule.id)
        assert refreshed.last_enqueued_local_date == (
            date(2026, 7, 6) if expected_count else None
        )
        if expected_count:
            task = enqueued[0]
            assert task.kind == "market.update"
            assert task.status == "queued"
            assert task.payload == schedule.payload
            assert task.created_at == sampled.astimezone(timezone.utc)
            assert schedules.list_occurrences(schedule.id)[0].task_id == task.id
    finally:
        engine.dispose()


def test_tick_converts_utc_to_shanghai_and_calls_clock_once(tmp_path: Path) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18, 0), payload=valid_update_payload())
    clock = _CountingClock(datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc))
    try:
        enqueued = MarketUpdateScheduler(schedules, tasks, clock=clock).tick()
        assert len(enqueued) == 1
        assert clock.calls == 1
        assert schedules.list_occurrences(schedule.id)[0].local_date == date(2026, 7, 6)
    finally:
        engine.dispose()


def test_tick_restart_new_day_and_clock_rollback_never_duplicate(
    tmp_path: Path,
) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18, 0), payload=valid_update_payload())
    try:
        first = MarketUpdateScheduler(
            schedules,
            tasks,
            clock=lambda: datetime(2026, 7, 6, 18, 0, tzinfo=SHANGHAI),
        ).tick()
        same_day_restart = MarketUpdateScheduler(
            schedules,
            tasks,
            clock=lambda: datetime(2026, 7, 6, 23, 59, tzinfo=SHANGHAI),
        ).tick()
        next_day_before = MarketUpdateScheduler(
            schedules,
            tasks,
            clock=lambda: datetime(2026, 7, 7, 17, 59, tzinfo=SHANGHAI),
        ).tick()
        next_day_due = MarketUpdateScheduler(
            schedules,
            tasks,
            clock=lambda: datetime(2026, 7, 7, 18, 0, tzinfo=SHANGHAI),
        ).tick()
        rolled_back_clock = MarketUpdateScheduler(
            schedules,
            tasks,
            clock=lambda: datetime(2026, 7, 6, 20, 0, tzinfo=SHANGHAI),
        ).tick()

        assert len(first) == 1
        assert same_day_restart == ()
        assert next_day_before == ()
        assert len(next_day_due) == 1
        assert rolled_back_clock == ()
        occurrences = schedules.list_occurrences(schedule.id)
        assert [occurrence.local_date for occurrence in occurrences] == [
            date(2026, 7, 6),
            date(2026, 7, 7),
        ]
        assert len({occurrence.task_id for occurrence in occurrences}) == 2
    finally:
        engine.dispose()


def test_future_last_date_is_clock_rollback_noop(tmp_path: Path) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18, 0), payload=valid_update_payload())
    with engine.begin() as connection:
        connection.execute(
            update(MarketUpdateSchedule)
            .where(MarketUpdateSchedule.id == schedule.id)
            .values(last_enqueued_local_date=date(2026, 7, 8))
        )
    try:
        enqueued = MarketUpdateScheduler(
            schedules,
            tasks,
            clock=lambda: datetime(2026, 7, 7, 20, 0, tzinfo=SHANGHAI),
        ).tick()
        assert enqueued == ()
        assert _counts(engine) == (0, 0, 0)
        assert schedules.get(schedule.id).last_enqueued_local_date == date(2026, 7, 8)
    finally:
        engine.dispose()


def test_tick_orders_due_schedules_and_never_backfills_history(tmp_path: Path) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedules.create(
        local_time=time(18, 0),
        payload=valid_update_payload(symbols=["600000.SH"]),
        schedule_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
    )
    schedules.create(
        local_time=time(9, 30),
        payload=valid_update_payload(symbols=["000001.SZ"]),
        schedule_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    )
    schedules.create(
        local_time=time(18, 0),
        payload=valid_update_payload(symbols=["300750.SZ"]),
        schedule_id="00000000-0000-4000-8000-000000000001",
    )
    try:
        enqueued = MarketUpdateScheduler(
            schedules,
            tasks,
            clock=lambda: datetime(2026, 7, 6, 20, 0, tzinfo=SHANGHAI),
        ).tick()

        assert [
            cast(tuple[str, ...], task.payload["symbols"])[0] for task in enqueued
        ] == [
            "000001.SZ",
            "600000.SH",
            "300750.SZ",
        ]
        with engine.connect() as connection:
            dates = (
                connection.execute(
                    select(MarketUpdateOccurrence.local_date).order_by(
                        MarketUpdateOccurrence.local_date
                    )
                )
                .scalars()
                .all()
            )
        assert dates == [date(2026, 7, 6)] * 3
    finally:
        engine.dispose()
