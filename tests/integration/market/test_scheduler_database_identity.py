from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, select

from stock_desk.market.scheduler import (
    MarketUpdateScheduleRepository,
    MarketUpdateScheduler,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import MarketUpdateOccurrence, TaskEvent, TaskRun
from stock_desk.tasks.repository import TaskRepository, TaskValidationError
from tests.integration.market.scheduler_test_helpers import valid_update_payload


def _task_counts(engine: Engine) -> tuple[int, int, int]:
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


def test_tick_rejects_task_repository_bound_to_same_relative_url_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_a = tmp_path / "a"
    directory_b = tmp_path / "b"
    directory_a.mkdir()
    directory_b.mkdir()
    relative_url = "sqlite:///same.db"

    monkeypatch.chdir(directory_a)
    migrate(relative_url)
    task_engine = create_engine_for_url(relative_url)
    tasks = TaskRepository(task_engine)

    monkeypatch.chdir(directory_b)
    migrate(relative_url)
    schedule_engine = create_engine_for_url(relative_url)
    schedules = MarketUpdateScheduleRepository(schedule_engine)
    schedule = schedules.create(
        local_time=time(18, 0),
        payload=valid_update_payload(),
    )
    scheduler = MarketUpdateScheduler(
        schedules,
        tasks,
        clock=lambda: datetime(
            2026,
            7,
            6,
            20,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )
    try:
        with pytest.raises(TaskValidationError, match="database"):
            scheduler.tick()

        assert _task_counts(task_engine) == (0, 0, 0)
        assert _task_counts(schedule_engine) == (0, 0, 0)
        assert schedules.get(schedule.id).last_enqueued_local_date is None
    finally:
        task_engine.dispose()
        schedule_engine.dispose()
