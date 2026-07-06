from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import cast

from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.engine import Connection

from stock_desk.backtest.models import (
    BacktestAggregateMetricRow,
    BacktestFailureRow,
    BacktestGroupMetricRow,
    BacktestLogRow,
    BacktestOrderEventRow,
    BacktestRunRow,
    BacktestSymbolRow,
    BacktestTradeRow,
)
from stock_desk.backtest.types import (
    BacktestSnapshot,
    FrozenSymbolGap,
    PinnedMarketRef,
)
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
)
from stock_desk.storage.models import TaskRun
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository


class BacktestRepositoryError(RuntimeError):
    pass


class BacktestNotFound(BacktestRepositoryError):
    pass


class BacktestConflict(BacktestRepositoryError):
    pass


def _json_depth(value: object, *, depth: int = 0) -> int:
    if isinstance(value, Mapping):
        return max(
            (depth, *(_json_depth(item, depth=depth + 1) for item in value.values()))
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return max((depth, *(_json_depth(item, depth=depth + 1) for item in value)))
    return depth


def _validate_json_payload(
    payload: object,
    *,
    field_name: str,
    max_bytes: int,
    max_depth: int = 32,
) -> None:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError) as error:
        raise BacktestRepositoryError(f"{field_name} is not canonical JSON") from error
    if len(encoded) > max_bytes:
        raise BacktestRepositoryError(f"{field_name} exceeds the byte limit")
    if _json_depth(payload) > max_depth:
        raise BacktestRepositoryError(f"{field_name} exceeds the depth limit")


@dataclass(frozen=True, slots=True)
class BacktestSymbolSnapshot:
    ordinal: int
    symbol: str
    reference: PinnedMarketRef | FrozenSymbolGap
    status: str
    signal_series_id: str | None
    warmup_start: datetime | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class BacktestRunSnapshot:
    id: str
    task_id: str
    snapshot: BacktestSnapshot
    status: str
    stage: str
    total: int
    processed: int
    failed: int
    result_hash: str | None
    actual_warmup_start: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    symbols: tuple[BacktestSymbolSnapshot, ...]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BacktestRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        try:
            with engine.connect() as connection:
                self._database_identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise BacktestRepositoryError(
                "backtest database identity could not be determined"
            ) from error

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    def _validate_connection(self, connection: Connection) -> None:
        if connection.closed:
            raise BacktestRepositoryError("backtest transaction is closed")
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise BacktestRepositoryError(
                "backtest database identity changed"
            ) from error
        if identity != self._database_identity:
            raise BacktestRepositoryError("backtest database identity changed")

    @staticmethod
    def _append_log(
        connection: Connection,
        *,
        run_id: str,
        level: str,
        message: str,
        detail: dict[str, object],
    ) -> None:
        ordinal = (
            int(
                connection.execute(
                    select(func.coalesce(func.max(BacktestLogRow.ordinal), -1)).where(
                        BacktestLogRow.run_id == run_id
                    )
                ).scalar_one()
            )
            + 1
        )
        connection.execute(
            insert(BacktestLogRow),
            {
                "run_id": run_id,
                "ordinal": ordinal,
                "level": level,
                "message": message,
                "detail_json": detail,
            },
        )

    def create_in_transaction(
        self,
        connection: Connection,
        *,
        run_id: str,
        task_id: str,
        snapshot: BacktestSnapshot,
        now: datetime,
    ) -> None:
        self._validate_connection(connection)
        canonical = BacktestSnapshot.model_validate(snapshot.model_dump(mode="python"))
        snapshot_payload = canonical.model_dump(mode="json")
        _validate_json_payload(
            snapshot_payload,
            field_name="backtest snapshot",
            max_bytes=64 * 1024 * 1024,
        )
        connection.execute(
            insert(BacktestRunRow),
            {
                "id": run_id,
                "task_id": task_id,
                "snapshot_id": canonical.snapshot_id,
                "snapshot_json": snapshot_payload,
                "status": "queued",
                "stage": "queued",
                "total": len(canonical.symbols),
                "processed": 0,
                "failed_count": 0,
                "result_hash": None,
                "actual_warmup_start": None,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
            },
        )
        connection.execute(
            insert(BacktestSymbolRow),
            [
                {
                    "run_id": run_id,
                    "ordinal": ordinal,
                    "symbol": reference.symbol,
                    "input_kind": (
                        "gap" if isinstance(reference, FrozenSymbolGap) else "runnable"
                    ),
                    "reference_json": reference.model_dump(mode="json"),
                    "status": "pending",
                    "signal_series_id": None,
                    "warmup_start": None,
                    "failure_reason": None,
                    "created_at": now,
                    "updated_at": now,
                }
                for ordinal, reference in enumerate(canonical.symbol_inputs)
            ],
        )

    def get_run(self, run_id: str) -> BacktestRunSnapshot:
        with self._engine.connect() as connection:
            run = (
                connection.execute(
                    select(BacktestRunRow).where(BacktestRunRow.id == run_id)
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise BacktestNotFound("backtest run was not found")
            symbols = (
                connection.execute(
                    select(BacktestSymbolRow)
                    .where(BacktestSymbolRow.run_id == run_id)
                    .order_by(BacktestSymbolRow.ordinal)
                )
                .mappings()
                .all()
            )
        snapshot = BacktestSnapshot.model_validate_json(
            json.dumps(run["snapshot_json"], allow_nan=False)
        )
        references = snapshot.symbol_inputs
        if len(symbols) != len(references):
            raise BacktestRepositoryError("backtest symbol rows are inconsistent")
        created_at = _utc(cast(datetime, run["created_at"]))
        updated_at = _utc(cast(datetime, run["updated_at"]))
        assert created_at is not None and updated_at is not None
        return BacktestRunSnapshot(
            id=cast(str, run["id"]),
            task_id=cast(str, run["task_id"]),
            snapshot=snapshot,
            status=cast(str, run["status"]),
            stage=cast(str, run["stage"]),
            total=cast(int, run["total"]),
            processed=cast(int, run["processed"]),
            failed=cast(int, run["failed_count"]),
            result_hash=cast(str | None, run["result_hash"]),
            actual_warmup_start=_utc(cast(datetime | None, run["actual_warmup_start"])),
            created_at=created_at,
            updated_at=updated_at,
            started_at=_utc(cast(datetime | None, run["started_at"])),
            finished_at=_utc(cast(datetime | None, run["finished_at"])),
            symbols=tuple(
                BacktestSymbolSnapshot(
                    ordinal=cast(int, row["ordinal"]),
                    symbol=cast(str, row["symbol"]),
                    reference=references[index],
                    status=cast(str, row["status"]),
                    signal_series_id=cast(str | None, row["signal_series_id"]),
                    warmup_start=_utc(cast(datetime | None, row["warmup_start"])),
                    failure_reason=cast(str | None, row["failure_reason"]),
                )
                for index, row in enumerate(symbols)
            ),
        )

    def list_run_ids(self) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            return tuple(
                connection.execute(
                    select(BacktestRunRow.id).order_by(BacktestRunRow.created_at)
                ).scalars()
            )

    def get_run_by_task(self, task_id: str) -> BacktestRunSnapshot:
        with self._engine.connect() as connection:
            run_id = connection.execute(
                select(BacktestRunRow.id).where(BacktestRunRow.task_id == task_id)
            ).scalar_one_or_none()
        if run_id is None:
            raise BacktestNotFound("backtest task has no run")
        return self.get_run(run_id)

    def start_claim(
        self,
        claim: TaskClaim,
        *,
        tasks: TaskRepository,
        now: datetime,
    ) -> BacktestRunSnapshot:
        run = self.get_run_by_task(claim.snapshot.id)
        with self._engine.begin() as connection:
            tasks.guard_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                progress=claim.snapshot.progress,
                now=now,
            )
            changed = connection.execute(
                update(BacktestRunRow)
                .where(
                    BacktestRunRow.id == run.id,
                    BacktestRunRow.status.in_(("queued", "running")),
                )
                .values(
                    status="running",
                    stage="executing",
                    started_at=func.coalesce(BacktestRunRow.started_at, now),
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise BacktestConflict("backtest run cannot be started")
            self._append_log(
                connection,
                run_id=run.id,
                level="info",
                message="run_started",
                detail={"attempt": claim.attempt_count},
            )
        return self.get_run(run.id)

    def checkpoint_symbol(
        self,
        claim: TaskClaim,
        *,
        tasks: TaskRepository,
        run_id: str,
        symbol: str,
        signal_series_id: str | None,
        trade_payloads: tuple[tuple[bool, dict[str, object]], ...],
        event_payloads: tuple[tuple[str, dict[str, object]], ...],
        failure_reason: str | None,
        now: datetime,
        warmup_start: datetime | None = None,
    ) -> BacktestRunSnapshot:
        for _realized, payload in trade_payloads:
            _validate_json_payload(
                payload,
                field_name="backtest trade",
                max_bytes=256 * 1024,
            )
        for _event_type, payload in event_payloads:
            _validate_json_payload(
                payload,
                field_name="backtest order event",
                max_bytes=64 * 1024,
            )
        with self._engine.begin() as connection:
            row = connection.execute(
                select(BacktestSymbolRow.status).where(
                    BacktestSymbolRow.run_id == run_id,
                    BacktestSymbolRow.symbol == symbol,
                )
            ).scalar_one_or_none()
            if row is None:
                raise BacktestNotFound("backtest symbol was not found")
            if row in {"succeeded", "failed"}:
                return self.get_run(run_id)
            counts = connection.execute(
                select(BacktestRunRow.total, BacktestRunRow.processed).where(
                    BacktestRunRow.id == run_id
                )
            ).one()
            total, processed = int(counts[0]), int(counts[1])
            progress = (processed + 1) / total
            tasks.guard_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                progress=progress,
                now=now,
            )
            if trade_payloads:
                connection.execute(
                    insert(BacktestTradeRow),
                    [
                        {
                            "run_id": run_id,
                            "symbol": symbol,
                            "ordinal": ordinal,
                            "realized": realized,
                            "payload_json": payload,
                        }
                        for ordinal, (realized, payload) in enumerate(trade_payloads)
                    ],
                )
            if event_payloads:
                connection.execute(
                    insert(BacktestOrderEventRow),
                    [
                        {
                            "run_id": run_id,
                            "symbol": symbol,
                            "ordinal": ordinal,
                            "event_type": event_type,
                            "payload_json": payload,
                        }
                        for ordinal, (event_type, payload) in enumerate(event_payloads)
                    ],
                )
            if failure_reason is not None:
                connection.execute(
                    insert(BacktestFailureRow),
                    {
                        "run_id": run_id,
                        "symbol": symbol,
                        "ordinal": processed,
                        "reason": failure_reason,
                        "detail_json": {},
                    },
                )
            terminal = "failed" if failure_reason is not None else "succeeded"
            changed = connection.execute(
                update(BacktestSymbolRow)
                .where(
                    BacktestSymbolRow.run_id == run_id,
                    BacktestSymbolRow.symbol == symbol,
                    BacktestSymbolRow.status == "pending",
                )
                .values(
                    status=terminal,
                    signal_series_id=signal_series_id,
                    warmup_start=warmup_start,
                    failure_reason=failure_reason,
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise BacktestConflict("backtest symbol checkpoint conflicted")
            changed = connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id, BacktestRunRow.status == "running")
                .values(
                    processed=BacktestRunRow.processed + 1,
                    failed_count=BacktestRunRow.failed_count
                    + (1 if failure_reason is not None else 0),
                    updated_at=now,
                )
            )
            if changed.rowcount != 1:
                raise BacktestConflict("backtest run checkpoint conflicted")
            self._append_log(
                connection,
                run_id=run_id,
                level="warning" if failure_reason is not None else "info",
                message="symbol_checkpointed",
                detail={"symbol": symbol, "status": terminal},
            )
        return self.get_run(run_id)

    def list_trade_payloads(self, run_id: str) -> tuple[dict[str, object], ...]:
        with self._engine.connect() as connection:
            return tuple(
                dict(payload)
                for payload in connection.execute(
                    select(BacktestTradeRow.payload_json)
                    .where(BacktestTradeRow.run_id == run_id)
                    .order_by(BacktestTradeRow.symbol, BacktestTradeRow.ordinal)
                ).scalars()
            )

    def minimum_warmup_start(self, run_id: str) -> datetime | None:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(func.min(BacktestSymbolRow.warmup_start)).where(
                    BacktestSymbolRow.run_id == run_id,
                    BacktestSymbolRow.status == "succeeded",
                )
            ).scalar_one()
        return _utc(value)

    def finish_claim(
        self,
        claim: TaskClaim,
        *,
        tasks: TaskRepository,
        run_id: str,
        aggregate_payload: dict[str, object],
        group_payloads: tuple[tuple[str, str, dict[str, object]], ...],
        actual_warmup_start: datetime | None,
        now: datetime,
    ) -> BacktestRunSnapshot:
        _validate_json_payload(
            aggregate_payload,
            field_name="backtest aggregate metric",
            max_bytes=1024 * 1024,
        )
        for _dimension, _group_key, payload in group_payloads:
            _validate_json_payload(
                payload,
                field_name="backtest group metric",
                max_bytes=64 * 1024,
            )
        with self._engine.begin() as connection:
            tasks.guard_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                progress=1.0,
                now=now,
            )
            run = connection.execute(
                select(
                    BacktestRunRow.total,
                    BacktestRunRow.processed,
                    BacktestRunRow.failed_count,
                    BacktestRunRow.snapshot_id,
                ).where(BacktestRunRow.id == run_id)
            ).one()
            if int(run[0]) != int(run[1]):
                raise BacktestConflict("backtest run is incomplete")
            connection.execute(
                insert(BacktestAggregateMetricRow),
                {
                    "run_id": run_id,
                    "metric_key": "overview",
                    "payload_json": aggregate_payload,
                },
            )
            if group_payloads:
                connection.execute(
                    insert(BacktestGroupMetricRow),
                    [
                        {
                            "run_id": run_id,
                            "dimension": dimension,
                            "group_key": group_key,
                            "payload_json": payload,
                        }
                        for dimension, group_key, payload in group_payloads
                    ],
                )
            result_hash = self._result_hash(
                connection, run_id, actual_warmup_start=actual_warmup_start
            )
            task = tasks.complete_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                {
                    "run_id": run_id,
                    "snapshot_id": cast(str, run[3]),
                    "result_hash": result_hash,
                    "processed": int(run[1]),
                    "total": int(run[0]),
                    "failed": int(run[2]),
                },
                now=now,
            )
            status = (
                "cancelled"
                if task.status == "cancelled"
                else ("succeeded" if int(run[2]) == 0 else "partial_failed")
            )
            self._append_log(
                connection,
                run_id=run_id,
                level="info",
                message="run_cancelled" if status == "cancelled" else "run_completed",
                detail={"status": status},
            )
            changed = connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id, BacktestRunRow.status == "running")
                .values(
                    status=status,
                    stage="cancelled" if status == "cancelled" else "completed",
                    result_hash=None if status == "cancelled" else result_hash,
                    actual_warmup_start=(
                        None if status == "cancelled" else actual_warmup_start
                    ),
                    updated_at=now,
                    finished_at=now,
                )
            )
            if changed.rowcount != 1:
                raise BacktestConflict("backtest run cannot be finalized")
        return self.get_run(run_id)

    def cancel_claim(
        self,
        claim: TaskClaim,
        *,
        tasks: TaskRepository,
        run_id: str,
        now: datetime,
    ) -> BacktestRunSnapshot:
        run = self.get_run(run_id)
        with self._engine.begin() as connection:
            tasks.guard_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                progress=run.processed / run.total,
                now=now,
            )
            cancel_requested = connection.execute(
                select(TaskRun.cancel_requested)
                .where(TaskRun.id == claim.snapshot.id)
                .with_for_update()
            ).scalar_one()
            if not cancel_requested:
                raise BacktestConflict("backtest cancellation was not requested")
            self._append_log(
                connection,
                run_id=run_id,
                level="info",
                message="run_cancelled",
                detail={},
            )
            changed = connection.execute(
                update(BacktestRunRow)
                .where(
                    BacktestRunRow.id == run_id,
                    BacktestRunRow.status == "running",
                )
                .values(
                    status="cancelled",
                    stage="cancelled",
                    updated_at=now,
                    finished_at=now,
                )
            )
            if changed.rowcount != 1:
                raise BacktestConflict("backtest run cannot be cancelled")
            tasks.complete_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                {
                    "run_id": run_id,
                    "cancelled": True,
                    "processed": run.processed,
                    "total": run.total,
                },
                now=now,
            )
        return self.get_run(run_id)

    def fail_claim(
        self,
        claim: TaskClaim,
        *,
        tasks: TaskRepository,
        run_id: str,
        reason: str,
        now: datetime,
    ) -> BacktestRunSnapshot:
        with self._engine.begin() as connection:
            task = tasks.fail_claim_in_transaction(
                connection,
                claim.snapshot.id,
                claim.claim_token,
                {"code": reason},
                now=now,
            )
            self._append_log(
                connection,
                run_id=run_id,
                level="error" if task.status != "cancelled" else "info",
                message=(
                    "run_cancelled" if task.status == "cancelled" else "run_failed"
                ),
                detail={"reason": reason},
            )
            changed = connection.execute(
                update(BacktestRunRow)
                .where(
                    BacktestRunRow.id == run_id,
                    BacktestRunRow.status.in_(("queued", "running")),
                )
                .values(
                    status="cancelled" if task.status == "cancelled" else "failed",
                    stage="cancelled" if task.status == "cancelled" else "failed",
                    updated_at=now,
                    finished_at=now,
                )
            )
            if changed.rowcount != 1:
                raise BacktestConflict("backtest run cannot be failed")
        return self.get_run(run_id)

    @staticmethod
    def _result_hash(
        connection: Connection,
        run_id: str,
        *,
        actual_warmup_start: datetime | None,
    ) -> str:
        payload = {
            "actual_warmup_start": (
                None
                if actual_warmup_start is None
                else actual_warmup_start.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "symbols": [
                tuple(row)
                for row in connection.execute(
                    select(
                        BacktestSymbolRow.ordinal,
                        BacktestSymbolRow.symbol,
                        BacktestSymbolRow.status,
                        BacktestSymbolRow.signal_series_id,
                        BacktestSymbolRow.failure_reason,
                    )
                    .where(BacktestSymbolRow.run_id == run_id)
                    .order_by(BacktestSymbolRow.ordinal)
                )
            ],
            "trades": [
                tuple(row)
                for row in connection.execute(
                    select(
                        BacktestTradeRow.symbol,
                        BacktestTradeRow.ordinal,
                        BacktestTradeRow.realized,
                        BacktestTradeRow.payload_json,
                    )
                    .where(BacktestTradeRow.run_id == run_id)
                    .order_by(BacktestTradeRow.symbol, BacktestTradeRow.ordinal)
                )
            ],
            "failures": [
                tuple(row)
                for row in connection.execute(
                    select(
                        BacktestFailureRow.symbol,
                        BacktestFailureRow.ordinal,
                        BacktestFailureRow.reason,
                    )
                    .where(BacktestFailureRow.run_id == run_id)
                    .order_by(BacktestFailureRow.ordinal, BacktestFailureRow.symbol)
                )
            ],
            "order_events": [
                tuple(row)
                for row in connection.execute(
                    select(
                        BacktestOrderEventRow.symbol,
                        BacktestOrderEventRow.ordinal,
                        BacktestOrderEventRow.event_type,
                        BacktestOrderEventRow.payload_json,
                    )
                    .where(BacktestOrderEventRow.run_id == run_id)
                    .order_by(
                        BacktestOrderEventRow.symbol,
                        BacktestOrderEventRow.ordinal,
                    )
                )
            ],
            "aggregate_metrics": [
                tuple(row)
                for row in connection.execute(
                    select(
                        BacktestAggregateMetricRow.metric_key,
                        BacktestAggregateMetricRow.payload_json,
                    )
                    .where(BacktestAggregateMetricRow.run_id == run_id)
                    .order_by(BacktestAggregateMetricRow.metric_key)
                )
            ],
            "group_metrics": [
                tuple(row)
                for row in connection.execute(
                    select(
                        BacktestGroupMetricRow.dimension,
                        BacktestGroupMetricRow.group_key,
                        BacktestGroupMetricRow.payload_json,
                    )
                    .where(BacktestGroupMetricRow.run_id == run_id)
                    .order_by(
                        BacktestGroupMetricRow.dimension,
                        BacktestGroupMetricRow.group_key,
                    )
                )
            ],
        }
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def count_trades(self, run_id: str, *, realized: bool) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    select(func.count())
                    .select_from(BacktestTradeRow)
                    .where(
                        BacktestTradeRow.run_id == run_id,
                        BacktestTradeRow.realized.is_(realized),
                    )
                ).scalar_one()
            )
