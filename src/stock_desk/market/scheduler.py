from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone as datetime_timezone
from typing import Any, Final, Literal, TypeAlias, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, case, insert, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from stock_desk.market.update import MARKET_UPDATE_TASK_KIND, MarketUpdateRequest
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import (
    MarketUpdateOccurrence,
    MarketUpdateSchedule,
)
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import (
    TaskRepository,
    _freeze_json_object,
    _validated_json_object,
)


MARKET_UPDATE_TIMEZONE: Final[str] = "Asia/Shanghai"
MarketUpdateTimezone: TypeAlias = Literal["Asia/Shanghai"]


class MarketUpdateScheduleError(RuntimeError):
    """Base class for schedule persistence failures."""


class MarketUpdateScheduleNotFound(MarketUpdateScheduleError):
    """A requested schedule does not exist."""


class MarketUpdateScheduleConflict(MarketUpdateScheduleError):
    """A schedule identity conflicts with persisted state."""


class MarketUpdateScheduleValidationError(MarketUpdateScheduleError, ValueError):
    """A schedule does not satisfy the strict public contract."""


@dataclass(frozen=True, slots=True)
class MarketUpdateScheduleSnapshot:
    id: str
    enabled: bool
    timezone: MarketUpdateTimezone
    local_time: time
    payload: Mapping[str, Any]
    last_enqueued_local_date: date | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MarketUpdateOccurrenceSnapshot:
    schedule_id: str
    local_date: date
    task_id: str
    created_at: datetime


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime_timezone.utc)
    return value.astimezone(datetime_timezone.utc)


def _validated_schedule_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule id must be a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule id must be a canonical UUID"
        ) from error
    if str(parsed) != value:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule id must be a canonical UUID"
        )
    return value


def _validated_occurrence_task_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise MarketUpdateScheduleValidationError(
            "Market update occurrence task id must be a canonical UUID"
        )
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise MarketUpdateScheduleValidationError(
            "Market update occurrence task id must be a canonical UUID"
        ) from error
    if str(parsed) != value:
        raise MarketUpdateScheduleValidationError(
            "Market update occurrence task id must be a canonical UUID"
        )
    return value


def _validated_timezone(value: object) -> MarketUpdateTimezone:
    if value != MARKET_UPDATE_TIMEZONE or type(value) is not str:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule timezone must be Asia/Shanghai"
        )
    return cast(MarketUpdateTimezone, value)


def _validated_local_time(value: object) -> time:
    if type(value) is not time:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule local time must be a time"
        )
    local_time = value
    if local_time.tzinfo is not None:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule local time must be naive"
        )
    if local_time.second != 0 or local_time.microsecond != 0:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule local time must use minute precision"
        )
    return local_time


def _validated_enabled(value: object) -> bool:
    if type(value) is not bool:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule enabled must be a boolean"
        )
    return value


def _validated_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        request = MarketUpdateRequest.from_payload(value)
        canonical = cast(Mapping[str, Any], request.model_dump(mode="json"))
        return _validated_json_object(canonical, field_name="schedule payload")
    except Exception as error:
        raise MarketUpdateScheduleValidationError(
            "Market update schedule payload is invalid"
        ) from error


def _validated_last_date(value: object) -> date | None:
    if value is None or type(value) is date:
        return value
    raise MarketUpdateScheduleValidationError(
        "Market update schedule last date is invalid"
    )


def _schedule_snapshot(row: RowMapping) -> MarketUpdateScheduleSnapshot:
    canonical_payload = _validated_payload(cast(Mapping[str, Any], row["payload_json"]))
    return MarketUpdateScheduleSnapshot(
        id=_validated_schedule_id(row["id"]),
        enabled=_validated_enabled(row["enabled"]),
        timezone=_validated_timezone(row["timezone"]),
        local_time=_validated_local_time(row["local_time"]),
        payload=_freeze_json_object(canonical_payload),
        last_enqueued_local_date=_validated_last_date(row["last_enqueued_local_date"]),
        created_at=_aware_utc(cast(datetime, row["created_at"])),
        updated_at=_aware_utc(cast(datetime, row["updated_at"])),
    )


def _occurrence_snapshot(row: RowMapping) -> MarketUpdateOccurrenceSnapshot:
    local_date = row["local_date"]
    if type(local_date) is not date:
        raise MarketUpdateScheduleValidationError(
            "Market update occurrence date is invalid"
        )
    return MarketUpdateOccurrenceSnapshot(
        schedule_id=_validated_schedule_id(row["schedule_id"]),
        local_date=local_date,
        task_id=_validated_occurrence_task_id(row["task_id"]),
        created_at=_aware_utc(cast(datetime, row["created_at"])),
    )


class MarketUpdateScheduleRepository:
    """Strict persistence boundary for daily market update schedules."""

    def __init__(self, engine: Engine, *, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine

    @classmethod
    def open(cls, url: str) -> MarketUpdateScheduleRepository:
        migrate(url)
        return cls(create_engine_for_url(url), owns_engine=True)

    def create(
        self,
        *,
        local_time: time,
        payload: Mapping[str, Any],
        timezone: str = MARKET_UPDATE_TIMEZONE,
        enabled: bool = True,
        schedule_id: str | None = None,
    ) -> MarketUpdateScheduleSnapshot:
        validated_id = _validated_schedule_id(
            str(uuid4()) if schedule_id is None else schedule_id
        )
        validated_timezone = _validated_timezone(timezone)
        validated_time = _validated_local_time(local_time)
        validated_payload = _validated_payload(payload)
        validated_enabled = _validated_enabled(enabled)
        now = datetime.now(datetime_timezone.utc)
        statement = (
            insert(MarketUpdateSchedule)
            .values(
                id=validated_id,
                enabled=validated_enabled,
                timezone=validated_timezone,
                local_time=validated_time,
                payload_json=validated_payload,
                last_enqueued_local_date=None,
                created_at=now,
                updated_at=now,
            )
            .returning(MarketUpdateSchedule)
        )
        try:
            with self._engine.begin() as connection:
                row = connection.execute(statement).mappings().one()
        except IntegrityError as error:
            raise MarketUpdateScheduleConflict(
                "Market update schedule conflicts with persisted state"
            ) from error
        return _schedule_snapshot(row)

    def get(self, schedule_id: str) -> MarketUpdateScheduleSnapshot:
        validated_id = _validated_schedule_id(schedule_id)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(MarketUpdateSchedule).where(
                        MarketUpdateSchedule.id == validated_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise MarketUpdateScheduleNotFound("Market update schedule was not found")
        return _schedule_snapshot(row)

    def list(self) -> tuple[MarketUpdateScheduleSnapshot, ...]:
        statement = select(MarketUpdateSchedule).order_by(
            MarketUpdateSchedule.local_time,
            MarketUpdateSchedule.created_at,
            MarketUpdateSchedule.id,
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_schedule_snapshot(row) for row in rows)

    def _candidate_ids(self) -> tuple[str, ...]:
        statement = select(MarketUpdateSchedule.id).order_by(
            MarketUpdateSchedule.local_time,
            MarketUpdateSchedule.created_at,
            MarketUpdateSchedule.id,
        )
        with self._engine.connect() as connection:
            return tuple(connection.execute(statement).scalars())

    def set_enabled(
        self,
        schedule_id: str,
        enabled: bool,
    ) -> MarketUpdateScheduleSnapshot:
        validated_id = _validated_schedule_id(schedule_id)
        validated_enabled = _validated_enabled(enabled)
        now = datetime.now(datetime_timezone.utc)
        transition_time = case(
            (MarketUpdateSchedule.updated_at > now, MarketUpdateSchedule.updated_at),
            else_=now,
        )
        statement = (
            update(MarketUpdateSchedule)
            .where(MarketUpdateSchedule.id == validated_id)
            .values(enabled=validated_enabled, updated_at=transition_time)
            .returning(MarketUpdateSchedule)
        )
        with self._engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise MarketUpdateScheduleNotFound("Market update schedule was not found")
        return _schedule_snapshot(row)

    def list_occurrences(
        self,
        schedule_id: str,
    ) -> tuple[MarketUpdateOccurrenceSnapshot, ...]:
        validated_id = _validated_schedule_id(schedule_id)
        schedule_statement = select(MarketUpdateSchedule.id).where(
            MarketUpdateSchedule.id == validated_id
        )
        occurrence_statement = (
            select(MarketUpdateOccurrence)
            .where(MarketUpdateOccurrence.schedule_id == validated_id)
            .order_by(
                MarketUpdateOccurrence.local_date,
                MarketUpdateOccurrence.task_id,
            )
        )
        with self._engine.connect() as connection:
            if connection.execute(schedule_statement).scalar_one_or_none() is None:
                raise MarketUpdateScheduleNotFound(
                    "Market update schedule was not found"
                )
            rows = connection.execute(occurrence_statement).mappings().all()
        return tuple(_occurrence_snapshot(row) for row in rows)

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()


def _validated_clock_sample(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Scheduler clock must return an aware datetime")
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(datetime_timezone.utc)
    except Exception as error:
        raise ValueError("Scheduler clock must return an aware datetime") from error
    if offset is None:
        raise ValueError("Scheduler clock must return an aware datetime")
    return normalized


class MarketUpdateScheduler:
    """Perform one finite pass over durable daily update schedules."""

    def __init__(
        self,
        schedules: MarketUpdateScheduleRepository,
        tasks: TaskRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._schedules = schedules
        self._tasks = tasks
        self._clock = clock
        self._shanghai = ZoneInfo(MARKET_UPDATE_TIMEZONE)

    def tick(self) -> tuple[TaskSnapshot, ...]:
        sampled_at = _validated_clock_sample(self._clock())
        local_now = sampled_at.astimezone(self._shanghai)
        local_date = local_now.date()
        local_wall_time = local_now.timetz().replace(tzinfo=None)
        enqueued: list[TaskSnapshot] = []
        for schedule_id in self._schedules._candidate_ids():
            task = self._enqueue_if_due(
                schedule_id,
                local_date=local_date,
                local_wall_time=local_wall_time,
                sampled_at=sampled_at,
            )
            if task is not None:
                enqueued.append(task)
        return tuple(enqueued)

    def _enqueue_if_due(
        self,
        schedule_id: str,
        *,
        local_date: date,
        local_wall_time: time,
        sampled_at: datetime,
    ) -> TaskSnapshot | None:
        connection = self._schedules._engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            row = self._schedule_row(connection, schedule_id)
            if row is None:
                connection.commit()
                return None
            schedule = _schedule_snapshot(row)
            previous_date = schedule.last_enqueued_local_date
            if (
                not schedule.enabled
                or local_wall_time < schedule.local_time
                or (previous_date is not None and previous_date >= local_date)
            ):
                connection.commit()
                return None

            task_id = str(uuid4())
            connection.execute(
                insert(MarketUpdateOccurrence).values(
                    schedule_id=schedule.id,
                    local_date=local_date,
                    task_id=task_id,
                    created_at=sampled_at,
                )
            )
            task = self._tasks.enqueue_in_transaction(
                connection,
                MARKET_UPDATE_TASK_KIND,
                schedule.payload,
                task_id=task_id,
                now=sampled_at,
            )
            previous_date_condition = (
                MarketUpdateSchedule.last_enqueued_local_date.is_(None)
                if previous_date is None
                else MarketUpdateSchedule.last_enqueued_local_date == previous_date
            )
            transition_time = case(
                (
                    MarketUpdateSchedule.updated_at > sampled_at,
                    MarketUpdateSchedule.updated_at,
                ),
                else_=sampled_at,
            )
            updated_id = connection.execute(
                update(MarketUpdateSchedule)
                .where(
                    MarketUpdateSchedule.id == schedule.id,
                    MarketUpdateSchedule.enabled.is_(True),
                    previous_date_condition,
                )
                .values(
                    last_enqueued_local_date=local_date,
                    updated_at=transition_time,
                )
                .returning(MarketUpdateSchedule.id)
            ).scalar_one_or_none()
            if updated_id is None:
                raise MarketUpdateScheduleConflict(
                    "Market update schedule changed during enqueue"
                )
            connection.commit()
            return task
        except IntegrityError:
            connection.rollback()
            if self._occurrence_exists(schedule_id, local_date):
                return None
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _schedule_row(
        connection: Connection,
        schedule_id: str,
    ) -> RowMapping | None:
        return (
            connection.execute(
                select(MarketUpdateSchedule).where(
                    MarketUpdateSchedule.id == schedule_id
                )
            )
            .mappings()
            .one_or_none()
        )

    def _occurrence_exists(self, schedule_id: str, local_date: date) -> bool:
        statement = select(MarketUpdateOccurrence.schedule_id).where(
            MarketUpdateOccurrence.schedule_id == schedule_id,
            MarketUpdateOccurrence.local_date == local_date,
        )
        with self._schedules._engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None
