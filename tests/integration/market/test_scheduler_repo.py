from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update

from stock_desk.market.scheduler import (
    MarketUpdateScheduleConflict,
    MarketUpdateScheduleNotFound,
    MarketUpdateScheduleValidationError,
)
from stock_desk.storage.models import MarketUpdateOccurrence, MarketUpdateSchedule

from tests.integration.market.scheduler_test_helpers import (
    scheduler_database,
    valid_update_payload,
)


def test_schedule_repository_create_get_list_and_defensive_freeze(
    tmp_path: Path,
) -> None:
    _url, engine, repository, _tasks = scheduler_database(tmp_path)
    payload = valid_update_payload(symbols=["600000.SH", "000001.SZ"])
    original_symbols = payload["symbols"]
    schedule_id = str(uuid4())
    try:
        created = repository.create(
            local_time=time(18, 0),
            payload=payload,
            timezone="Asia/Shanghai",
            enabled=True,
            schedule_id=schedule_id,
        )
        original_symbols.append("300750.SZ")
        fetched = repository.get(schedule_id)

        assert created == fetched
        assert created.id == schedule_id
        assert created.enabled is True
        assert created.timezone == "Asia/Shanghai"
        assert created.local_time == time(18, 0)
        assert created.payload["symbols"] == ("600000.SH", "000001.SZ")
        assert isinstance(created.payload, MappingProxyType)
        assert created.last_enqueued_local_date is None
        assert created.created_at.tzinfo is not None
        assert created.updated_at.tzinfo is not None
        with pytest.raises(TypeError):
            created.payload["period"] = "1m"  # type: ignore[index]

        generated = repository.create(
            local_time=time(9, 30),
            payload=valid_update_payload(symbols=["300750.SZ"]),
        )
        assert str(UUID(generated.id)) == generated.id
        assert [schedule.id for schedule in repository.list()] == [
            generated.id,
            created.id,
        ]

        with engine.connect() as connection:
            stored_payload = connection.execute(
                select(MarketUpdateSchedule.payload_json).where(
                    MarketUpdateSchedule.id == schedule_id
                )
            ).scalar_one()
        assert stored_payload["symbols"] == ["600000.SH", "000001.SZ"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"schedule_id": "not-a-uuid"}, "id"),
        ({"schedule_id": str(uuid4()).upper()}, "id"),
        ({"timezone": "UTC"}, "timezone"),
        ({"local_time": time(18, 0, 1)}, "minute"),
        ({"local_time": time(18, 0, 0, 1)}, "minute"),
        ({"local_time": time(18, 0, tzinfo=timezone.utc)}, "naive"),
        ({"local_time": "18:00"}, "time"),
        ({"enabled": 1}, "enabled"),
        ({"payload": {**valid_update_payload(), "extra": True}}, "payload"),
        (
            {"payload": valid_update_payload(symbols=["600000.SH", "600000.SH"])},
            "payload",
        ),
    ],
    ids=(
        "id-shape",
        "id-canonical",
        "timezone",
        "seconds",
        "microseconds",
        "aware-time",
        "time-type",
        "enabled-type",
        "payload-extra",
        "payload-duplicate-symbol",
    ),
)
def test_schedule_repository_rejects_invalid_contract_without_rows(
    tmp_path: Path,
    overrides: dict[str, Any],
    match: str,
) -> None:
    _url, engine, repository, _tasks = scheduler_database(tmp_path)
    arguments: dict[str, Any] = {
        "local_time": time(18, 0),
        "payload": valid_update_payload(),
        "timezone": "Asia/Shanghai",
        "enabled": True,
        "schedule_id": str(uuid4()),
    }
    arguments.update(overrides)
    try:
        with pytest.raises(MarketUpdateScheduleValidationError, match=match):
            repository.create(**arguments)
        assert repository.list() == ()
    finally:
        engine.dispose()


def test_schedule_repository_duplicate_not_found_and_strict_enable(
    tmp_path: Path,
) -> None:
    _url, engine, repository, _tasks = scheduler_database(tmp_path)
    schedule_id = str(uuid4())
    try:
        repository.create(
            local_time=time(18, 0),
            payload=valid_update_payload(),
            schedule_id=schedule_id,
        )
        with pytest.raises(MarketUpdateScheduleConflict):
            repository.create(
                local_time=time(18, 0),
                payload=valid_update_payload(),
                schedule_id=schedule_id,
            )
        with pytest.raises(MarketUpdateScheduleNotFound):
            repository.get(str(uuid4()))
        with pytest.raises(MarketUpdateScheduleNotFound):
            repository.set_enabled(str(uuid4()), True)
        with pytest.raises(MarketUpdateScheduleValidationError, match="enabled"):
            repository.set_enabled(schedule_id, 1)  # type: ignore[arg-type]
    finally:
        engine.dispose()


def test_schedule_repository_disable_reenable_and_monotonic_updated_at(
    tmp_path: Path,
) -> None:
    _url, engine, repository, _tasks = scheduler_database(tmp_path)
    schedule_id = str(uuid4())
    future = datetime.now(timezone.utc) + timedelta(days=365)
    try:
        created = repository.create(
            local_time=time(18, 0),
            payload=valid_update_payload(),
            schedule_id=schedule_id,
        )
        with engine.begin() as connection:
            connection.execute(
                update(MarketUpdateSchedule)
                .where(MarketUpdateSchedule.id == schedule_id)
                .values(updated_at=future)
            )

        disabled = repository.set_enabled(schedule_id, False)
        reenabled = repository.set_enabled(schedule_id, True)

        assert created.enabled is True
        assert disabled.enabled is False
        assert reenabled.enabled is True
        assert disabled.updated_at == future
        assert reenabled.updated_at == future
    finally:
        engine.dispose()


def test_schedule_repository_lists_frozen_occurrences_without_mutation_api(
    tmp_path: Path,
) -> None:
    _url, engine, repository, tasks = scheduler_database(tmp_path)
    schedule_id = str(uuid4())
    try:
        repository.create(
            local_time=time(18, 0),
            payload=valid_update_payload(),
            schedule_id=schedule_id,
        )
        task = tasks.create("market.update", valid_update_payload())
        with engine.begin() as connection:
            connection.execute(
                insert(MarketUpdateOccurrence).values(
                    schedule_id=schedule_id,
                    local_date=date(2026, 7, 6),
                    task_id=task.id,
                )
            )

        occurrences = repository.list_occurrences(schedule_id)
        assert len(occurrences) == 1
        assert occurrences[0].schedule_id == schedule_id
        assert occurrences[0].local_date == date(2026, 7, 6)
        assert occurrences[0].task_id == task.id
        assert occurrences[0].created_at.tzinfo is not None
        assert not hasattr(repository, "update_occurrence")
        assert not hasattr(repository, "delete_occurrence")
    finally:
        engine.dispose()
