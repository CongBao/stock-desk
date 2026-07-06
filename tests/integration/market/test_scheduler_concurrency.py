from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

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


def _assert_exactly_once(engine: object, schedule_id: str) -> None:
    with engine.connect() as connection:  # type: ignore[attr-defined]
        task_ids = connection.execute(select(TaskRun.id)).scalars().all()
        event_task_ids = connection.execute(select(TaskEvent.task_id)).scalars().all()
        occurrence = connection.execute(
            select(
                MarketUpdateOccurrence.task_id,
                MarketUpdateOccurrence.local_date,
            )
        ).one()
        last_date = connection.execute(
            select(MarketUpdateSchedule.last_enqueued_local_date).where(
                MarketUpdateSchedule.id == schedule_id
            )
        ).scalar_one()
        assert connection.execute(select(func.count(TaskRun.id))).scalar_one() == 1
    assert task_ids == [occurrence.task_id]
    assert event_task_ids == [occurrence.task_id]
    assert occurrence.local_date == date(2026, 7, 6)
    assert last_date == date(2026, 7, 6)


def test_synchronized_threads_enqueue_exactly_once(tmp_path: Path) -> None:
    _url, engine, schedules, tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    participant_count = 4
    barrier = Barrier(participant_count)

    def run_tick() -> tuple[str, ...]:
        def synchronized_clock() -> datetime:
            barrier.wait(timeout=10)
            return DUE_NOW

        enqueued = MarketUpdateScheduler(
            schedules, tasks, clock=synchronized_clock
        ).tick()
        return tuple(task.id for task in enqueued)

    try:
        with ThreadPoolExecutor(max_workers=participant_count) as executor:
            results = list(
                executor.map(lambda _index: run_tick(), range(participant_count))
            )
        assert sum(len(result) for result in results) == 1
        _assert_exactly_once(engine, schedule.id)
    finally:
        engine.dispose()


SUBPROCESS_PROGRAM = """
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_desk.market.scheduler import MarketUpdateScheduleRepository, MarketUpdateScheduler
from stock_desk.storage.database import create_engine_for_url
from stock_desk.tasks.repository import TaskRepository

url, gate = sys.argv[1:]
while not os.path.exists(gate):
    pass
schedule_engine = create_engine_for_url(url)
task_engine = create_engine_for_url(url)
try:
    scheduler = MarketUpdateScheduler(
        MarketUpdateScheduleRepository(schedule_engine),
        TaskRepository(task_engine),
        clock=lambda: datetime(2026, 7, 6, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    print(json.dumps([task.id for task in scheduler.tick()]))
finally:
    task_engine.dispose()
    schedule_engine.dispose()
"""


def test_synchronized_subprocesses_enqueue_exactly_once(tmp_path: Path) -> None:
    url, engine, schedules, _tasks = scheduler_database(tmp_path)
    schedule = schedules.create(local_time=time(18), payload=valid_update_payload())
    gate = tmp_path / "start-gate"
    command = [sys.executable, "-c", SUBPROCESS_PROGRAM, url, str(gate)]
    processes = [
        subprocess.Popen(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _index in range(3)
    ]
    try:
        gate.touch()
        completed = [process.communicate(timeout=20) for process in processes]
        for process, (_stdout, stderr) in zip(processes, completed, strict=True):
            assert process.returncode == 0, stderr
        results = [
            json.loads(stdout.strip().splitlines()[-1]) for stdout, _ in completed
        ]
        assert sum(len(result) for result in results) == 1
        _assert_exactly_once(engine, schedule.id)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        engine.dispose()
