from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from base64 import urlsafe_b64decode, urlsafe_b64encode
import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Generic, TypeVar, cast

from sqlalchemy import (
    Engine,
    and_,
    case,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql import Select
from pydantic import ValidationError

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
from stock_desk.backtest.public_data import public_payload, public_text
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


@dataclass(frozen=True, slots=True)
class BacktestOverviewSnapshot:
    run_id: str
    task_id: str
    snapshot_id: str
    status: str
    stage: str
    total: int
    processed: int
    failed: int
    result_hash: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class BacktestGroupSnapshot:
    dimension: str
    key: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BacktestTradeSnapshot:
    symbol: str
    ordinal: int
    payload: Mapping[str, object]
    symbol_ordinal: int = 0


@dataclass(frozen=True, slots=True)
class BacktestReplayRecord:
    run_id: str
    snapshot: BacktestSnapshot
    result_hash: str | None
    status: str
    symbol: BacktestSymbolSnapshot
    trade: BacktestTradeSnapshot
    realized: bool


@dataclass(frozen=True, slots=True)
class BacktestFailureSnapshot:
    symbol: str
    ordinal: int
    reason: str
    detail: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BacktestLogSnapshot:
    ordinal: int
    level: str
    message: str
    detail: Mapping[str, object]


TPageItem = TypeVar("TPageItem")


@dataclass(frozen=True, slots=True)
class BacktestPage(Generic[TPageItem]):
    items: tuple[TPageItem, ...]
    next_cursor: str | None
    after_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestReportSnapshot:
    overview: BacktestOverviewSnapshot
    formula_version_id: str
    formula_checksum: str
    formula_parameters: tuple[Mapping[str, object], ...]
    formula_engine_version: str
    compatibility_version: str
    backtest_engine_version: str
    instrument_dataset_version: str
    symbol_count: int
    runnable_count: int
    gap_count: int
    signal_source_ids: tuple[str, ...]
    execution_source_ids: tuple[str, ...]
    status_source_ids: tuple[str, ...]
    provenance_digest: str
    period: str
    adjustment: str
    quantity_shares: int
    commission_bps: str
    minimum_commission: str
    sell_tax_bps: str
    slippage_bps: str
    execution_rules_version: str
    cost_model_version: str
    sizing_version: str
    warmup_policy_version: str
    metrics: Mapping[str, object]
    disclaimer: str
    outcomes: BacktestOutcomeSnapshot


@dataclass(frozen=True, slots=True)
class BacktestOutcomeSnapshot:
    total: int
    succeeded: int
    failed: int
    data_insufficient: int
    unprocessed: int


@dataclass(frozen=True, slots=True)
class BacktestExportMetadata:
    run_id: str
    snapshot_id: str
    generated_at: datetime
    section: str
    disclaimer: str
    formula_version_id: str
    formula_checksum: str
    formula_engine_version: str
    compatibility_version: str
    backtest_engine_version: str
    instrument_dataset_version: str
    symbol_count: int
    runnable_count: int
    gap_count: int
    signal_source_ids: tuple[str, ...]
    execution_source_ids: tuple[str, ...]
    status_source_ids: tuple[str, ...]
    provenance_digest: str
    period: str
    adjustment: str
    quantity_shares: int
    commission_bps: str
    minimum_commission: str
    sell_tax_bps: str
    slippage_bps: str
    execution_rules_version: str
    cost_model_version: str
    sizing_version: str
    warmup_policy_version: str


@dataclass(frozen=True, slots=True)
class BacktestExportRecord:
    section: str
    data: Mapping[str, object]


_TERMINAL_STATUSES = frozenset({"succeeded", "partial_failed", "failed", "cancelled"})
_ACTIVE_STATUSES = frozenset({"queued", "running"})
_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 512


def _decimal_text(value: object) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _is_sha256_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _provenance_summary(
    snapshot: BacktestSnapshot,
) -> tuple[
    int,
    int,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str,
]:
    runnable = tuple(
        item for item in snapshot.symbol_inputs if isinstance(item, PinnedMarketRef)
    )
    signal_sources = tuple(sorted({item.signal_source.value for item in runnable}))
    execution_sources = tuple(
        sorted({item.execution_source.value for item in runnable})
    )
    status_sources = tuple(
        sorted({item.execution_status_source.value for item in runnable})
    )
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in snapshot.symbol_inputs],
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return (
        len(runnable),
        len(snapshot.symbol_inputs) - len(runnable),
        signal_sources,
        execution_sources,
        status_sources,
        f"sha256:{hashlib.sha256(canonical).hexdigest()}",
    )


def _public_detail(
    detail: Mapping[str, object], *, collection: str
) -> dict[str, object]:
    allowed = (
        frozenset({"attempt", "reason", "status", "symbol"})
        if collection == "logs"
        else frozenset(
            {
                "code",
                "reason",
                "status",
                "symbol",
                "ordinal",
                "manifest_record_id",
                "dataset_version",
                "signal_series_id",
            }
        )
    )
    result: dict[str, object] = {}
    for key in sorted(detail):
        if key not in allowed:
            continue
        value = detail[key]
        if key in {"attempt", "ordinal"} and type(value) is int and value >= 0:
            result[key] = value
        elif key == "symbol" and isinstance(value, str) and len(value) <= 9:
            result[key] = value
        elif key in {"manifest_record_id", "dataset_version", "signal_series_id"}:
            if (
                isinstance(value, str)
                and len(value) == 71
                and value.startswith("sha256:")
            ):
                result[key] = value
        elif key in {"code", "reason", "status"} and isinstance(value, str):
            if (
                value
                and len(value) <= 64
                and all(
                    char.islower() or char.isdigit() or char == "_" for char in value
                )
            ):
                result[key] = public_text(value)
    return result


def _persisted_reference(
    payload: object, *, input_kind: object
) -> PinnedMarketRef | FrozenSymbolGap:
    try:
        encoded = json.dumps(payload, allow_nan=False)
        if input_kind == "runnable":
            return PinnedMarketRef.model_validate_json(encoded)
        if input_kind == "gap":
            return FrozenSymbolGap.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError) as error:
        raise BacktestRepositoryError(
            "backtest symbol provenance is invalid"
        ) from error
    raise BacktestRepositoryError("backtest symbol provenance is invalid")


def _encode_cursor(collection: str, run_id: str | None, key: list[object]) -> str:
    body = json.dumps(
        {"collection": collection, "key": key, "run_id": run_id, "version": 1},
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    checksum = hashlib.sha256(body).hexdigest()[:16].encode("ascii")
    return urlsafe_b64encode(body + b"." + checksum).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str | None, *, collection: str, run_id: str | None
) -> list[object] | None:
    if cursor is None:
        return None
    if type(cursor) is not str or not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise BacktestConflict("backtest cursor is invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        encoded, checksum = urlsafe_b64decode(padded.encode("ascii")).rsplit(b".", 1)
        if hashlib.sha256(encoded).hexdigest()[:16].encode("ascii") != checksum:
            raise ValueError
        value = json.loads(encoded)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise BacktestConflict("backtest cursor is invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("version") != _CURSOR_VERSION
        or value.get("collection") != collection
        or value.get("run_id") != run_id
        or not isinstance(value.get("key"), list)
    ):
        raise BacktestConflict("backtest cursor is invalid")
    key = cast(list[object], value["key"])
    if any(type(item) is int and not 0 <= item <= 2**63 - 1 for item in key):
        raise BacktestConflict("backtest cursor is invalid")
    return key


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

    @staticmethod
    def _overview_from_row(run: RowMapping) -> BacktestOverviewSnapshot:
        created_at = _utc(cast(datetime, run["created_at"]))
        updated_at = _utc(cast(datetime, run["updated_at"]))
        if created_at is None or updated_at is None:
            raise BacktestRepositoryError("backtest timestamps are invalid")
        return BacktestOverviewSnapshot(
            run_id=cast(str, run["id"]),
            task_id=cast(str, run["task_id"]),
            snapshot_id=cast(str, run["snapshot_id"]),
            status=cast(str, run["status"]),
            stage=cast(str, run["stage"]),
            total=cast(int, run["total"]),
            processed=cast(int, run["processed"]),
            failed=cast(int, run["failed_count"]),
            result_hash=cast(str | None, run["result_hash"]),
            created_at=created_at,
            updated_at=updated_at,
            started_at=_utc(cast(datetime | None, run["started_at"])),
            finished_at=_utc(cast(datetime | None, run["finished_at"])),
        )

    def get_overview(self, run_id: str) -> BacktestOverviewSnapshot:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(BacktestRunRow).where(BacktestRunRow.id == run_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise BacktestNotFound("backtest run was not found")
        return self._overview_from_row(row)

    def list_runs_page(
        self, *, limit: int, cursor: str | None
    ) -> BacktestPage[BacktestOverviewSnapshot]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BacktestConflict("backtest page limit is invalid")
        key = _decode_cursor(cursor, collection="runs", run_id=None)
        statement = select(BacktestRunRow)
        if key is not None:
            if len(key) != 2 or not all(isinstance(item, str) for item in key):
                raise BacktestConflict("backtest cursor is invalid")
            try:
                created_at = datetime.fromisoformat(
                    cast(str, key[0]).replace("Z", "+00:00")
                )
            except ValueError:
                raise BacktestConflict("backtest cursor is invalid") from None
            statement = statement.where(
                or_(
                    BacktestRunRow.created_at < created_at,
                    and_(
                        BacktestRunRow.created_at == created_at,
                        BacktestRunRow.id < cast(str, key[1]),
                    ),
                )
            )
        statement = statement.order_by(
            BacktestRunRow.created_at.desc(), BacktestRunRow.id.desc()
        ).limit(limit + 1)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        selected = rows[:limit]
        items = tuple(self._overview_from_row(row) for row in selected)
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = _encode_cursor(
                "runs",
                None,
                [
                    last.created_at.isoformat().replace("+00:00", "Z"),
                    last.run_id,
                ],
            )
        return BacktestPage(items=items, next_cursor=next_cursor)

    def report(self, run_id: str) -> BacktestReportSnapshot:
        overview = self.get_overview(run_id)
        if overview.status not in _TERMINAL_STATUSES:
            raise BacktestConflict("backtest report is not ready")
        with self._engine.connect() as connection:
            raw_snapshot = connection.execute(
                select(BacktestRunRow.snapshot_json).where(BacktestRunRow.id == run_id)
            ).scalar_one()
            metrics = connection.execute(
                select(BacktestAggregateMetricRow.payload_json).where(
                    BacktestAggregateMetricRow.run_id == run_id,
                    BacktestAggregateMetricRow.metric_key == "overview",
                )
            ).scalar_one_or_none()
            outcome_row = connection.execute(
                select(
                    func.sum(
                        case(
                            (
                                and_(
                                    BacktestSymbolRow.input_kind == "runnable",
                                    BacktestSymbolRow.status == "succeeded",
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            (
                                and_(
                                    BacktestSymbolRow.input_kind == "runnable",
                                    BacktestSymbolRow.status == "failed",
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            (
                                and_(
                                    BacktestSymbolRow.input_kind == "gap",
                                    BacktestSymbolRow.status == "failed",
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    func.sum(case((BacktestSymbolRow.status == "pending", 1), else_=0)),
                ).where(BacktestSymbolRow.run_id == run_id)
            ).one()
        snapshot = BacktestSnapshot.model_validate_json(
            json.dumps(raw_snapshot, allow_nan=False)
        )
        (
            runnable,
            gaps,
            signal_sources,
            execution_sources,
            status_sources,
            provenance_digest,
        ) = _provenance_summary(snapshot)
        outcomes = BacktestOutcomeSnapshot(
            total=overview.total,
            succeeded=int(outcome_row[0] or 0),
            failed=int(outcome_row[1] or 0),
            data_insufficient=int(outcome_row[2] or 0),
            unprocessed=int(outcome_row[3] or 0),
        )
        if (
            outcomes.succeeded
            + outcomes.failed
            + outcomes.data_insufficient
            + outcomes.unprocessed
            != outcomes.total
            or outcomes.succeeded + outcomes.failed + outcomes.data_insufficient
            != overview.processed
            or outcomes.failed + outcomes.data_insufficient != overview.failed
        ):
            raise BacktestRepositoryError("backtest outcome counts are inconsistent")
        return BacktestReportSnapshot(
            overview=overview,
            formula_version_id=snapshot.formula_version_id,
            formula_checksum=snapshot.formula_checksum,
            formula_parameters=tuple(
                cast(Mapping[str, object], item.model_dump(mode="json"))
                for item in snapshot.formula_parameters
            ),
            formula_engine_version=snapshot.formula_engine_version,
            compatibility_version=snapshot.compatibility_version,
            backtest_engine_version=snapshot.backtest_engine_version,
            instrument_dataset_version=snapshot.instrument_dataset_version,
            symbol_count=len(snapshot.symbols),
            runnable_count=runnable,
            gap_count=gaps,
            signal_source_ids=signal_sources,
            execution_source_ids=execution_sources,
            status_source_ids=status_sources,
            provenance_digest=provenance_digest,
            period=snapshot.period.value,
            adjustment=snapshot.adjustment.value,
            quantity_shares=snapshot.quantity_shares,
            commission_bps=_decimal_text(snapshot.commission_bps),
            minimum_commission=_decimal_text(snapshot.minimum_commission),
            sell_tax_bps=_decimal_text(snapshot.sell_tax_bps),
            slippage_bps=_decimal_text(snapshot.slippage_bps),
            execution_rules_version=snapshot.execution_rules_version,
            cost_model_version=snapshot.cost_model_version,
            sizing_version="fixed-lot-v1",
            warmup_policy_version=snapshot.warmup_policy_version,
            metrics=(
                {}
                if metrics is None
                else cast(
                    Mapping[str, object],
                    public_payload(dict(cast(Mapping[str, object], metrics))),
                )
            ),
            disclaimer="independent trade samples, not portfolio return",
            outcomes=outcomes,
        )

    def get_replay_record(
        self, run_id: str, symbol: str, trade_ordinal: int
    ) -> BacktestReplayRecord:
        if type(trade_ordinal) is not int or not 0 <= trade_ordinal <= 2**63 - 1:
            raise BacktestConflict("backtest trade ordinal is invalid")
        with self._engine.connect() as connection:
            run_row = (
                connection.execute(
                    select(
                        BacktestRunRow.id,
                        BacktestRunRow.snapshot_id,
                        BacktestRunRow.snapshot_json,
                        BacktestRunRow.result_hash,
                        BacktestRunRow.status,
                    ).where(BacktestRunRow.id == run_id)
                )
                .mappings()
                .one_or_none()
            )
            if run_row is None:
                raise BacktestNotFound("backtest run was not found")
            raw_symbol = (
                connection.execute(
                    select(BacktestSymbolRow).where(
                        BacktestSymbolRow.run_id == run_id,
                        BacktestSymbolRow.symbol == symbol,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if raw_symbol is None:
                raise BacktestNotFound("backtest symbol was not found")
            row = (
                connection.execute(
                    select(BacktestTradeRow).where(
                        BacktestTradeRow.run_id == run_id,
                        BacktestTradeRow.symbol == symbol,
                        BacktestTradeRow.ordinal == trade_ordinal,
                    )
                )
                .mappings()
                .one_or_none()
            )
        stored_run_id = run_row["id"]
        if stored_run_id != run_id:
            raise BacktestRepositoryError("backtest replay run identity is invalid")
        status_value = run_row["status"]
        if not isinstance(status_value, str) or status_value not in (
            _ACTIVE_STATUSES | _TERMINAL_STATUSES
        ):
            raise BacktestRepositoryError("backtest replay run state is invalid")
        if status_value not in _TERMINAL_STATUSES:
            raise BacktestConflict("backtest replay is not ready")
        if row is None:
            raise BacktestNotFound("backtest trade was not found")
        try:
            snapshot = BacktestSnapshot.model_validate_json(
                json.dumps(run_row["snapshot_json"], allow_nan=False)
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise BacktestRepositoryError("backtest snapshot is invalid") from error
        if run_row["snapshot_id"] != snapshot.snapshot_id:
            raise BacktestRepositoryError(
                "backtest replay snapshot identity is invalid"
            )
        result_hash = run_row["result_hash"]
        if status_value in {"succeeded", "partial_failed"}:
            if not _is_sha256_digest(result_hash):
                raise BacktestRepositoryError(
                    "backtest replay result identity is invalid"
                )
        elif result_hash is not None:
            raise BacktestRepositoryError("backtest replay result identity is invalid")
        symbol_ordinal = cast(int, raw_symbol["ordinal"])
        if not 0 <= symbol_ordinal < len(snapshot.symbol_inputs):
            raise BacktestRepositoryError("backtest symbol ordinal is invalid")
        reference = _persisted_reference(
            raw_symbol["reference_json"], input_kind=raw_symbol["input_kind"]
        )
        if snapshot.symbol_inputs[symbol_ordinal] != reference:
            raise BacktestRepositoryError("backtest symbol provenance is inconsistent")
        symbol_row = BacktestSymbolSnapshot(
            ordinal=symbol_ordinal,
            symbol=cast(str, raw_symbol["symbol"]),
            reference=reference,
            status=cast(str, raw_symbol["status"]),
            signal_series_id=cast(str | None, raw_symbol["signal_series_id"]),
            warmup_start=_utc(cast(datetime | None, raw_symbol["warmup_start"])),
            failure_reason=cast(str | None, raw_symbol["failure_reason"]),
        )
        payload = row["payload_json"]
        if not isinstance(payload, Mapping):
            raise BacktestRepositoryError("backtest trade payload is invalid")
        _validate_json_payload(
            payload,
            field_name="backtest trade",
            max_bytes=256 * 1024,
        )
        realized = row["realized"]
        if type(realized) is not bool:
            raise BacktestRepositoryError("backtest trade state is invalid")
        return BacktestReplayRecord(
            run_id=run_id,
            snapshot=snapshot,
            result_hash=cast(str | None, result_hash),
            status=status_value,
            symbol=symbol_row,
            trade=BacktestTradeSnapshot(
                symbol=cast(str, row["symbol"]),
                ordinal=cast(int, row["ordinal"]),
                payload=dict(cast(Mapping[str, object], payload)),
                symbol_ordinal=symbol_row.ordinal,
            ),
            realized=realized,
        )

    def page(
        self,
        run_id: str,
        *,
        collection: str,
        limit: int,
        cursor: str | None,
        dimension: str | None = None,
    ) -> BacktestPage[object]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BacktestConflict("backtest page limit is invalid")
        if collection not in {
            "groups",
            "trades",
            "open",
            "failures",
            "logs",
            "symbols",
        }:
            raise BacktestConflict("backtest collection is invalid")
        if dimension is not None and (
            collection != "groups"
            or dimension not in {"symbol", "entry_month", "entry_year"}
        ):
            raise BacktestConflict("backtest group dimension is invalid")
        self.get_overview(run_id)
        cursor_collection = (
            f"groups:{dimension}"
            if collection == "groups" and dimension is not None
            else collection
        )
        key = _decode_cursor(cursor, collection=cursor_collection, run_id=run_id)
        with self._engine.connect() as connection:
            items, has_more = self._page_rows(
                connection,
                run_id=run_id,
                collection=collection,
                limit=limit,
                key=key,
                dimension=dimension,
            )
        next_cursor = None
        after_cursor = cursor
        encoded_last = None
        if items:
            encoded_last = _encode_cursor(
                cursor_collection, run_id, self._page_key(collection, items[-1])
            )
            after_cursor = encoded_last
        if has_more and items:
            next_cursor = encoded_last
        return BacktestPage(
            items=items, next_cursor=next_cursor, after_cursor=after_cursor
        )

    def iter_export_records(
        self,
        run_id: str,
        *,
        section: str,
        batch_size: int = 100,
    ) -> Iterator[BacktestExportMetadata | BacktestExportRecord]:
        if section not in {"groups", "trades", "open", "failures", "logs"}:
            raise BacktestConflict("backtest export section is invalid")
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise BacktestConflict("backtest export batch is invalid")
        with self._engine.connect() as connection, connection.begin():
            run = (
                connection.execute(
                    select(BacktestRunRow).where(BacktestRunRow.id == run_id)
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise BacktestNotFound("backtest run was not found")
            status_value = cast(str, run["status"])
            finished_at = _utc(cast(datetime | None, run["finished_at"]))
            if status_value not in _TERMINAL_STATUSES or finished_at is None:
                raise BacktestConflict("backtest export is not ready")
            snapshot = BacktestSnapshot.model_validate_json(
                json.dumps(run["snapshot_json"], allow_nan=False)
            )
            (
                runnable,
                gaps,
                signal_sources,
                execution_sources,
                status_sources,
                provenance_digest,
            ) = _provenance_summary(snapshot)
            yield BacktestExportMetadata(
                run_id=cast(str, run["id"]),
                snapshot_id=cast(str, run["snapshot_id"]),
                generated_at=finished_at,
                section=section,
                disclaimer="independent trade samples, not portfolio return",
                formula_version_id=snapshot.formula_version_id,
                formula_checksum=snapshot.formula_checksum,
                formula_engine_version=snapshot.formula_engine_version,
                compatibility_version=snapshot.compatibility_version,
                backtest_engine_version=snapshot.backtest_engine_version,
                instrument_dataset_version=snapshot.instrument_dataset_version,
                symbol_count=len(snapshot.symbols),
                runnable_count=runnable,
                gap_count=gaps,
                signal_source_ids=signal_sources,
                execution_source_ids=execution_sources,
                status_source_ids=status_sources,
                provenance_digest=provenance_digest,
                period=snapshot.period.value,
                adjustment=snapshot.adjustment.value,
                quantity_shares=snapshot.quantity_shares,
                commission_bps=_decimal_text(snapshot.commission_bps),
                minimum_commission=_decimal_text(snapshot.minimum_commission),
                sell_tax_bps=_decimal_text(snapshot.sell_tax_bps),
                slippage_bps=_decimal_text(snapshot.slippage_bps),
                execution_rules_version=snapshot.execution_rules_version,
                cost_model_version=snapshot.cost_model_version,
                sizing_version="fixed-lot-v1",
                warmup_policy_version=snapshot.warmup_policy_version,
            )
            result = connection.execute(self._export_statement(run_id, section))
            while True:
                rows = result.mappings().fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    yield BacktestExportRecord(
                        section=section,
                        data=self._export_record(section, row),
                    )

    @staticmethod
    def _export_statement(run_id: str, section: str) -> Select[Any]:
        if section == "groups":
            return (
                select(BacktestGroupMetricRow)
                .where(BacktestGroupMetricRow.run_id == run_id)
                .order_by(
                    BacktestGroupMetricRow.dimension,
                    BacktestGroupMetricRow.group_key,
                )
            )
        if section in {"trades", "open"}:
            return (
                select(
                    BacktestTradeRow,
                    BacktestSymbolRow.ordinal.label("symbol_ordinal"),
                )
                .join(
                    BacktestSymbolRow,
                    and_(
                        BacktestSymbolRow.run_id == BacktestTradeRow.run_id,
                        BacktestSymbolRow.symbol == BacktestTradeRow.symbol,
                    ),
                )
                .where(
                    BacktestTradeRow.run_id == run_id,
                    BacktestTradeRow.realized.is_(section == "trades"),
                )
                .order_by(
                    BacktestSymbolRow.ordinal,
                    BacktestTradeRow.ordinal,
                    BacktestTradeRow.symbol,
                )
            )
        if section == "failures":
            return (
                select(BacktestFailureRow)
                .where(BacktestFailureRow.run_id == run_id)
                .order_by(BacktestFailureRow.ordinal, BacktestFailureRow.symbol)
            )
        return (
            select(BacktestLogRow)
            .where(BacktestLogRow.run_id == run_id)
            .order_by(BacktestLogRow.ordinal)
        )

    @staticmethod
    def _export_record(section: str, row: RowMapping) -> Mapping[str, object]:
        if section == "groups":
            return {
                "dimension": row["dimension"],
                "key": row["group_key"],
                "payload": public_payload(
                    dict(cast(Mapping[str, object], row["payload_json"]))
                ),
            }
        if section in {"trades", "open"}:
            return {
                "ordinal": row["ordinal"],
                "payload": public_payload(
                    dict(cast(Mapping[str, object], row["payload_json"]))
                ),
                "symbol": row["symbol"],
            }
        if section == "failures":
            return {
                "detail": _public_detail(
                    cast(Mapping[str, object], row["detail_json"]),
                    collection="failures",
                ),
                "ordinal": row["ordinal"],
                "reason": row["reason"],
                "symbol": row["symbol"],
            }
        return {
            "detail": _public_detail(
                cast(Mapping[str, object], row["detail_json"]), collection="logs"
            ),
            "level": row["level"],
            "message": row["message"],
            "ordinal": row["ordinal"],
        }

    @staticmethod
    def _page_key(collection: str, item: object) -> list[object]:
        if collection == "groups":
            group = cast(BacktestGroupSnapshot, item)
            return [group.dimension, group.key]
        if collection in {"trades", "open"}:
            trade = cast(BacktestTradeSnapshot, item)
            return [trade.symbol_ordinal, trade.ordinal, trade.symbol]
        if collection == "failures":
            failure = cast(BacktestFailureSnapshot, item)
            return [failure.ordinal, failure.symbol]
        if collection == "symbols":
            symbol = cast(BacktestSymbolSnapshot, item)
            return [symbol.ordinal]
        log = cast(BacktestLogSnapshot, item)
        return [log.ordinal]

    @staticmethod
    def _page_rows(
        connection: Connection,
        *,
        run_id: str,
        collection: str,
        limit: int,
        key: list[object] | None,
        dimension: str | None = None,
    ) -> tuple[tuple[object, ...], bool]:
        if collection == "symbols":
            symbol_statement = select(BacktestSymbolRow).where(
                BacktestSymbolRow.run_id == run_id
            )
            if key is not None:
                if len(key) != 1 or type(key[0]) is not int:
                    raise BacktestConflict("backtest cursor is invalid")
                symbol_statement = symbol_statement.where(
                    BacktestSymbolRow.ordinal > key[0]
                )
            symbol_rows = (
                connection.execute(
                    symbol_statement.order_by(BacktestSymbolRow.ordinal).limit(
                        limit + 1
                    )
                )
                .mappings()
                .all()
            )
            symbol_items: tuple[object, ...] = tuple(
                BacktestSymbolSnapshot(
                    ordinal=cast(int, row["ordinal"]),
                    symbol=cast(str, row["symbol"]),
                    reference=_persisted_reference(
                        row["reference_json"], input_kind=row["input_kind"]
                    ),
                    status=cast(str, row["status"]),
                    signal_series_id=cast(str | None, row["signal_series_id"]),
                    warmup_start=_utc(cast(datetime | None, row["warmup_start"])),
                    failure_reason=cast(str | None, row["failure_reason"]),
                )
                for row in symbol_rows[:limit]
            )
            return symbol_items, len(symbol_rows) > limit
        if collection == "groups":
            group_statement = select(BacktestGroupMetricRow).where(
                BacktestGroupMetricRow.run_id == run_id
            )
            if dimension is not None:
                group_statement = group_statement.where(
                    BacktestGroupMetricRow.dimension == dimension
                )
            if key is not None:
                if len(key) != 2 or not all(isinstance(item, str) for item in key):
                    raise BacktestConflict("backtest cursor is invalid")
                if dimension is not None and key[0] != dimension:
                    raise BacktestConflict("backtest cursor is invalid")
                group_statement = group_statement.where(
                    or_(
                        BacktestGroupMetricRow.dimension > cast(str, key[0]),
                        and_(
                            BacktestGroupMetricRow.dimension == cast(str, key[0]),
                            BacktestGroupMetricRow.group_key > cast(str, key[1]),
                        ),
                    )
                )
            group_rows = (
                connection.execute(
                    group_statement.order_by(
                        BacktestGroupMetricRow.dimension,
                        BacktestGroupMetricRow.group_key,
                    ).limit(limit + 1)
                )
                .mappings()
                .all()
            )
            group_items: tuple[object, ...] = tuple(
                BacktestGroupSnapshot(
                    cast(str, row["dimension"]),
                    cast(str, row["group_key"]),
                    cast(
                        Mapping[str, object],
                        public_payload(
                            dict(cast(Mapping[str, object], row["payload_json"]))
                        ),
                    ),
                )
                for row in group_rows[:limit]
            )
            return group_items, len(group_rows) > limit
        if collection in {"trades", "open"}:
            realized = collection == "trades"
            trade_statement = (
                select(
                    BacktestTradeRow,
                    BacktestSymbolRow.ordinal.label("symbol_ordinal"),
                )
                .join(
                    BacktestSymbolRow,
                    and_(
                        BacktestSymbolRow.run_id == BacktestTradeRow.run_id,
                        BacktestSymbolRow.symbol == BacktestTradeRow.symbol,
                    ),
                )
                .where(
                    BacktestTradeRow.run_id == run_id,
                    BacktestTradeRow.realized.is_(realized),
                )
            )
            if key is not None:
                if (
                    len(key) != 3
                    or type(key[0]) is not int
                    or type(key[1]) is not int
                    or not isinstance(key[2], str)
                ):
                    raise BacktestConflict("backtest cursor is invalid")
                trade_statement = trade_statement.where(
                    tuple_(
                        BacktestSymbolRow.ordinal,
                        BacktestTradeRow.ordinal,
                        BacktestTradeRow.symbol,
                    )
                    > tuple_(literal(key[0]), literal(key[1]), literal(key[2]))
                )
            trade_rows = (
                connection.execute(
                    trade_statement.order_by(
                        BacktestSymbolRow.ordinal,
                        BacktestTradeRow.ordinal,
                        BacktestTradeRow.symbol,
                    ).limit(limit + 1)
                )
                .mappings()
                .all()
            )
            trade_items: tuple[object, ...] = tuple(
                BacktestTradeSnapshot(
                    cast(str, row["symbol"]),
                    cast(int, row["ordinal"]),
                    cast(
                        Mapping[str, object],
                        public_payload(
                            dict(cast(Mapping[str, object], row["payload_json"]))
                        ),
                    ),
                    cast(int, row["symbol_ordinal"]),
                )
                for row in trade_rows[:limit]
            )
            return trade_items, len(trade_rows) > limit
        if collection == "failures":
            failure_statement = select(BacktestFailureRow).where(
                BacktestFailureRow.run_id == run_id
            )
            if key is not None:
                if (
                    len(key) != 2
                    or type(key[0]) is not int
                    or not isinstance(key[1], str)
                ):
                    raise BacktestConflict("backtest cursor is invalid")
                failure_statement = failure_statement.where(
                    tuple_(BacktestFailureRow.ordinal, BacktestFailureRow.symbol)
                    > tuple_(literal(key[0]), literal(key[1]))
                )
            failure_rows = (
                connection.execute(
                    failure_statement.order_by(
                        BacktestFailureRow.ordinal, BacktestFailureRow.symbol
                    ).limit(limit + 1)
                )
                .mappings()
                .all()
            )
            failure_items: tuple[object, ...] = tuple(
                BacktestFailureSnapshot(
                    cast(str, row["symbol"]),
                    cast(int, row["ordinal"]),
                    cast(str, row["reason"]),
                    _public_detail(
                        cast(Mapping[str, object], row["detail_json"]),
                        collection="failures",
                    ),
                )
                for row in failure_rows[:limit]
            )
            return failure_items, len(failure_rows) > limit
        log_statement = select(BacktestLogRow).where(BacktestLogRow.run_id == run_id)
        if key is not None:
            if len(key) != 1 or type(key[0]) is not int:
                raise BacktestConflict("backtest cursor is invalid")
            log_statement = log_statement.where(BacktestLogRow.ordinal > key[0])
        log_rows = (
            connection.execute(
                log_statement.order_by(BacktestLogRow.ordinal).limit(limit + 1)
            )
            .mappings()
            .all()
        )
        log_items: tuple[object, ...] = tuple(
            BacktestLogSnapshot(
                cast(int, row["ordinal"]),
                cast(str, row["level"]),
                cast(str, row["message"]),
                _public_detail(
                    cast(Mapping[str, object], row["detail_json"]), collection="logs"
                ),
            )
            for row in log_rows[:limit]
        )
        return log_items, len(log_rows) > limit

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
                select(
                    BacktestRunRow.total,
                    BacktestRunRow.processed,
                    BacktestRunRow.failed_count,
                ).where(BacktestRunRow.id == run_id)
            ).one()
            total, processed, failed = int(counts[0]), int(counts[1]), int(counts[2])
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
            tasks.append_progress_event_in_transaction(
                connection,
                claim.snapshot.id,
                progress=progress,
                stage="executing",
                processed=processed + 1,
                total=total,
                failed=failed + (1 if failure_reason is not None else 0),
                now=now,
            )
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
