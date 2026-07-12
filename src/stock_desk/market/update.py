from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from typing import Annotated, Any, Final, Literal, Self, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import Engine, insert, literal, select
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
)

from stock_desk.market.lake import MarketLake, StoredRoutingManifest
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.providers.normalization import MARKET_TIMEZONE
from stock_desk.market.provenance import RoutedBarFailure, RoutedBarSuccess
from stock_desk.market.provenance import RoutedExecutionStatusSuccess
from stock_desk.market.routing import SourceRouter
from stock_desk.market.types import (
    Adjustment,
    BarQuery,
    CanonicalSymbol,
    Exchange,
    FailureReason,
    InstrumentKind,
    MAX_MARKET_UPDATE_PERIOD_BUCKETS,
    Period,
    UtcDatetime,
    estimated_period_buckets,
    instrument_kind_for_symbol,
)
from stock_desk.storage.models import MarketUpdateItem, TaskRun
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker


MARKET_UPDATE_TASK_KIND: Final[str] = "market.update"
MARKET_CATALOG_UPDATE_TASK_KIND: Final[str] = "market.catalog.update"
_CANCELLED_REASON: Final[str] = "cancel_requested"
_FINALIZING_PROGRESS: Final[float] = 0.99
_SYMBOL_ADAPTER = TypeAdapter(CanonicalSymbol)
_ROUTING_REASONS = frozenset(f"routing:{reason.value}" for reason in FailureReason)

MarketUpdateItemStatus: TypeAlias = Literal["succeeded", "failed", "cancelled"]


def execution_status_date_range(
    start: datetime,
    end: datetime,
) -> tuple[date, date]:
    """Return a natural-date half-open range containing every requested instant."""
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
        or start >= end
    ):
        raise ValueError("execution-status source range must be aware and nonempty")
    local_start = start.astimezone(MARKET_TIMEZONE)
    local_end = end.astimezone(MARKET_TIMEZONE)
    exclusive_end = local_end.date()
    if any(
        (
            local_end.hour,
            local_end.minute,
            local_end.second,
            local_end.microsecond,
        )
    ):
        exclusive_end += timedelta(days=1)
    if exclusive_end <= local_start.date():
        exclusive_end = local_start.date() + timedelta(days=1)
    return local_start.date(), exclusive_end


class MarketUpdateItemError(RuntimeError):
    """Base class for typed immutable update-item persistence failures."""


class MarketUpdateItemNotFound(MarketUpdateItemError):
    """The owning task does not exist."""


class MarketUpdateItemConflict(MarketUpdateItemError):
    """The task state or immutable item identity conflicts."""


class MarketUpdateItemValidationError(MarketUpdateItemError, ValueError):
    """An update item does not satisfy its strict public shape."""


class MarketUpdateItemStorageError(MarketUpdateItemError):
    """The update-item database identity changed or became unavailable."""


@dataclass(frozen=True, slots=True)
class MarketUpdateItemSnapshot:
    task_id: str
    ordinal: int
    symbol: str
    status: MarketUpdateItemStatus
    manifest_record_id: str | None
    dataset_version: str | None
    reason: str | None
    created_at: datetime


class MarketUpdateRequest(BaseModel):
    """Strict durable payload for one ordered multi-symbol bars update."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    symbols: Annotated[
        tuple[CanonicalSymbol, ...],
        Field(min_length=1, max_length=10_000),
    ]
    period: Period
    adjustment: Adjustment
    start: UtcDatetime
    end: UtcDatetime

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        encoded = json.dumps(
            dict(payload),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return cls.model_validate_json(encoded)

    @model_validator(mode="after")
    def validate_queries(self) -> Self:
        if len(self.symbols) != len(frozenset(self.symbols)):
            raise ValueError("market update symbols must be unique")
        for symbol in self.symbols:
            BarQuery(
                symbol=symbol,
                instrument_kind=instrument_kind_for_symbol(symbol),
                period=self.period,
                adjustment=self.adjustment,
                start=self.start,
                end=self.end,
            )
        estimated_work = len(self.symbols) * estimated_period_buckets(
            self.period,
            self.start,
            self.end,
        )
        if estimated_work > MAX_MARKET_UPDATE_PERIOD_BUCKETS:
            raise ValueError("market update estimated work exceeds the limit")
        return self


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _item_snapshot(row: RowMapping) -> MarketUpdateItemSnapshot:
    return MarketUpdateItemSnapshot(
        task_id=cast(str, row["task_id"]),
        ordinal=cast(int, row["ordinal"]),
        symbol=cast(str, row["symbol"]),
        status=cast(MarketUpdateItemStatus, row["status"]),
        manifest_record_id=cast(str | None, row["manifest_record_id"]),
        dataset_version=cast(str | None, row["dataset_version"]),
        reason=cast(str | None, row["reason"]),
        created_at=_aware_utc(cast(datetime, row["created_at"])),
    )


def _validated_item_identity(ordinal: int, symbol: str) -> tuple[int, str]:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise MarketUpdateItemValidationError(
            "Market update item ordinal must be a nonnegative integer"
        )
    try:
        canonical_symbol = _SYMBOL_ADAPTER.validate_python(symbol, strict=True)
    except ValueError as error:
        raise MarketUpdateItemValidationError(
            "Market update item symbol must be canonical"
        ) from error
    return ordinal, canonical_symbol


class MarketUpdateItemRepository:
    """Append-only persistence for per-symbol market update truth."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        try:
            with engine.connect() as connection:
                self._database_identity = connection_database_identity(connection)
        except DatabaseIdentityError:
            raise MarketUpdateItemStorageError(
                "Market update item storage is unavailable"
            ) from None

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    def _validate_connection(self, connection: Connection) -> None:
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError:
            raise MarketUpdateItemStorageError(
                "Market update item storage is unavailable"
            ) from None
        if identity != self._database_identity:
            raise MarketUpdateItemStorageError(
                "Market update item database identity changed"
            )

    def _insert(
        self,
        *,
        task_id: str,
        ordinal: int,
        symbol: str,
        status: MarketUpdateItemStatus,
        manifest_record_id: str | None,
        dataset_version: str | None,
        reason: str | None,
    ) -> MarketUpdateItemSnapshot:
        ordinal, symbol = _validated_item_identity(ordinal, symbol)
        try:
            with self._engine.begin() as connection:
                self._validate_connection(connection)
                eligible_task = select(
                    TaskRun.id,
                    literal(ordinal),
                    literal(symbol),
                    literal(status),
                    literal(manifest_record_id),
                    literal(dataset_version),
                    literal(reason),
                ).where(
                    TaskRun.id == task_id,
                    TaskRun.kind == MARKET_UPDATE_TASK_KIND,
                    TaskRun.status == "running",
                )
                row = (
                    connection.execute(
                        insert(MarketUpdateItem)
                        .from_select(
                            (
                                "task_id",
                                "ordinal",
                                "symbol",
                                "status",
                                "manifest_record_id",
                                "dataset_version",
                                "reason",
                            ),
                            eligible_task,
                        )
                        .returning(MarketUpdateItem)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    task = (
                        connection.execute(
                            select(TaskRun.kind, TaskRun.status).where(
                                TaskRun.id == task_id
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if task is None:
                        raise MarketUpdateItemNotFound(f"Task {task_id} was not found")
                    raise MarketUpdateItemConflict(
                        "Market update items require a running market update task"
                    )
        except IntegrityError as error:
            raise MarketUpdateItemConflict(
                "Market update item conflicts with immutable persisted state"
            ) from error
        return _item_snapshot(row)

    def record_success(
        self,
        *,
        task_id: str,
        ordinal: int,
        symbol: str,
        stored: StoredRoutingManifest,
    ) -> MarketUpdateItemSnapshot:
        return self._insert(
            task_id=task_id,
            ordinal=ordinal,
            symbol=symbol,
            status="succeeded",
            manifest_record_id=stored.manifest_record_id,
            dataset_version=stored.dataset_version,
            reason=None,
        )

    def record_failure(
        self,
        *,
        task_id: str,
        ordinal: int,
        symbol: str,
        reason: str,
    ) -> MarketUpdateItemSnapshot:
        if reason not in _ROUTING_REASONS:
            raise MarketUpdateItemValidationError(
                "Market update failure reason must be a fixed routing code"
            )
        return self._insert(
            task_id=task_id,
            ordinal=ordinal,
            symbol=symbol,
            status="failed",
            manifest_record_id=None,
            dataset_version=None,
            reason=reason,
        )

    def record_cancelled(
        self,
        *,
        task_id: str,
        ordinal: int,
        symbol: str,
    ) -> MarketUpdateItemSnapshot:
        return self._insert(
            task_id=task_id,
            ordinal=ordinal,
            symbol=symbol,
            status="cancelled",
            manifest_record_id=None,
            dataset_version=None,
            reason=_CANCELLED_REASON,
        )

    def list_for_task(self, task_id: str) -> list[MarketUpdateItemSnapshot]:
        statement = (
            select(MarketUpdateItem)
            .where(MarketUpdateItem.task_id == task_id)
            .order_by(MarketUpdateItem.ordinal)
        )
        with self._engine.connect() as connection:
            self._validate_connection(connection)
            task = connection.execute(
                select(TaskRun.kind).where(TaskRun.id == task_id)
            ).scalar_one_or_none()
            if task is None:
                raise MarketUpdateItemNotFound(f"Task {task_id} was not found")
            if task != MARKET_UPDATE_TASK_KIND:
                raise MarketUpdateItemConflict(
                    "Market update items require a market update task"
                )
            rows = connection.execute(statement).mappings().all()
        return [_item_snapshot(row) for row in rows]


class UpdateService:
    """Run one durable ordered multi-symbol market update task."""

    def __init__(
        self,
        *,
        router: SourceRouter,
        lake: MarketLake,
        tasks: TaskRepository,
        engine: Engine,
        execution_status_lake: ExecutionStatusLake | None = None,
    ) -> None:
        self._router = router
        self._lake = lake
        self._tasks = tasks
        self._items = MarketUpdateItemRepository(engine)
        self._execution_status_lake = execution_status_lake
        identities = (
            lake.database_identity,
            tasks.database_identity,
            self._items.database_identity,
        )
        if identities[1:] != identities[:-1]:
            raise ValueError("market update database identities do not match")

    @staticmethod
    def _progress(processed: int, total: int, *, persisting: bool) -> float:
        event_index = processed * 2 + (2 if persisting else 1)
        return 0.98 * event_index / (total * 2 + 1)

    @staticmethod
    def _detail(
        *,
        stage: Literal["routing", "persisting", "finalizing"],
        processed: int,
        total: int,
        current_symbol: str | None,
        succeeded: int,
        failed: int,
        cancelled: int,
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "processed": processed,
            "total": total,
            "current_symbol": current_symbol,
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": cancelled,
        }

    def handle(self, task: TaskSnapshot) -> Mapping[str, Any]:
        if task.kind != MARKET_UPDATE_TASK_KIND:
            raise ValueError("market update handler received the wrong task kind")
        request = MarketUpdateRequest.from_payload(task.payload)
        total = len(request.symbols)
        processed = 0
        succeeded = 0
        failed = 0
        cancelled = 0

        for ordinal, symbol in enumerate(request.symbols):
            current = self._tasks.get(task.id)
            if current.cancel_requested:
                for remaining_ordinal, remaining_symbol in enumerate(
                    request.symbols[ordinal:],
                    start=ordinal,
                ):
                    self._items.record_cancelled(
                        task_id=task.id,
                        ordinal=remaining_ordinal,
                        symbol=remaining_symbol,
                    )
                    processed += 1
                    cancelled += 1
                break

            query = BarQuery(
                symbol=symbol,
                instrument_kind=instrument_kind_for_symbol(symbol),
                period=request.period,
                adjustment=request.adjustment,
                start=request.start,
                end=request.end,
            )
            if (
                self._execution_status_lake is not None
                and query.instrument_kind is InstrumentKind.STOCK
            ):
                local_start, local_end = execution_status_date_range(
                    request.start, request.end
                )
                status_query = ExecutionStatusQuery(
                    symbol=symbol,
                    exchange=Exchange(symbol.rsplit(".", maxsplit=1)[1]),
                    start=local_start,
                    end=local_end,
                    period=request.period,
                )
                latest_status = self._execution_status_lake.latest_exact(status_query)
                previous_status_manifest = (
                    self._execution_status_lake.read(
                        latest_status.manifest_record_id
                    ).manifest
                    if latest_status is not None
                    else None
                )
                routed_status = self._router.fetch_execution_status(
                    status_query,
                    previous_manifest=previous_status_manifest,
                )
                if isinstance(routed_status, RoutedExecutionStatusSuccess):
                    self._execution_status_lake.write(routed_status)
            self._tasks.set_progress(
                task.id,
                self._progress(processed, total, persisting=False),
                detail=self._detail(
                    stage="routing",
                    processed=processed,
                    total=total,
                    current_symbol=symbol,
                    succeeded=succeeded,
                    failed=failed,
                    cancelled=cancelled,
                ),
            )
            latest = self._lake.latest_exact(query)
            previous_manifest = (
                self._lake.read(latest.manifest_record_id).manifest
                if latest is not None
                else None
            )
            routed = self._router.fetch_bars(
                query,
                previous_manifest=previous_manifest,
            )
            if isinstance(routed, RoutedBarFailure):
                self._items.record_failure(
                    task_id=task.id,
                    ordinal=ordinal,
                    symbol=symbol,
                    reason=f"routing:{routed.failure.reason.value}",
                )
                processed += 1
                failed += 1
                continue
            if not isinstance(routed, RoutedBarSuccess):
                raise TypeError("market router returned an invalid bars outcome")

            self._tasks.set_progress(
                task.id,
                self._progress(processed, total, persisting=True),
                detail=self._detail(
                    stage="persisting",
                    processed=processed,
                    total=total,
                    current_symbol=symbol,
                    succeeded=succeeded,
                    failed=failed,
                    cancelled=cancelled,
                ),
            )
            stored = self._lake.write(routed)
            self._items.record_success(
                task_id=task.id,
                ordinal=ordinal,
                symbol=symbol,
                stored=stored,
            )
            processed += 1
            succeeded += 1

        self._tasks.set_progress(
            task.id,
            _FINALIZING_PROGRESS,
            detail=self._detail(
                stage="finalizing",
                processed=processed,
                total=total,
                current_symbol=None,
                succeeded=succeeded,
                failed=failed,
                cancelled=cancelled,
            ),
        )
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": cancelled,
        }


def register_market_update(worker: TaskWorker, service: UpdateService) -> None:
    worker.register(MARKET_UPDATE_TASK_KIND, service.handle)
