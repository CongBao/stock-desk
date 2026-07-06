from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.engine import Connection

from stock_desk.backtest.config import BacktestRequest
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.backtest.repository import (
    BacktestOverviewSnapshot,
    BacktestPage,
    BacktestReportSnapshot,
)
from stock_desk.backtest.snapshot import freeze_request
from stock_desk.backtest.types import (
    BacktestSnapshot,
    FrozenSymbolGap,
    GapReason,
    PinnedMarketRef,
)
from stock_desk.formula.service import FormulaBacktestPreflight, FormulaService
from stock_desk.market.calendar import MARKET_TIMEZONE
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import CatalogBarPin, MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.types import Adjustment, BarQuery, Exchange, Period
from stock_desk.storage.database import DatabaseIdentity, connection_database_identity
from stock_desk.storage.models import (
    ExecutionStatusRoutingManifest,
    InstrumentRoutingManifest,
    MarketRoutingManifest,
)
from stock_desk.tasks.repository import TaskRepository


BACKTEST_ENGINE_VERSION = "backtest-engine-v1"


class BacktestSubmissionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BacktestIntent:
    scope_kind: Literal["single", "preset", "custom"]
    symbol: str | None
    scope_id: str | None
    scope_revision_or_snapshot_id: str | None
    formula_version_id: str
    formula_parameters: Mapping[str, int | float]
    period: Period
    adjustment: Adjustment
    scoring_start: datetime
    scoring_end: datetime
    quantity_shares: int
    commission_bps: Decimal
    minimum_commission: Decimal
    sell_tax_bps: Decimal
    slippage_bps: Decimal


@dataclass(frozen=True, slots=True)
class SubmittedBacktest:
    run_id: str
    task_id: str
    snapshot_id: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestPreflight:
    preview_snapshot_id: str
    reservation: Literal[False]
    formula_id: str
    formula_version_id: str
    formula_checksum: str
    engine_version: str
    compatibility_version: str
    normalized_parameters: tuple[Mapping[str, object], ...]
    scope_kind: str
    symbol: str | None
    scope_id: str | None
    scope_revision_or_snapshot_id: str | None
    total: int
    runnable: int
    gap_count: int
    gap_sample: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    period: Period | str
    adjustment: Adjustment | str
    scoring_start: datetime
    scoring_end: datetime
    warmup_policy_version: str
    lookback_bars: int | None
    unbounded_dependency: bool
    pinned_signal_count: int
    pinned_execution_count: int
    pinned_status_count: int
    estimated_formula_rows: int
    execution_rules_version: str
    cost_model_version: str
    sizing_version: str
    quantity_shares: int
    commission_bps: Decimal
    minimum_commission: Decimal
    sell_tax_bps: Decimal
    slippage_bps: Decimal
    disclaimer: str


@dataclass(frozen=True, slots=True)
class _PreparedBacktest:
    run_id: str
    task_id: str
    snapshot: BacktestSnapshot
    formula: FormulaBacktestPreflight
    runnable: int
    gaps: tuple[tuple[str, str], ...]
    pinned_signal_count: int
    pinned_execution_count: int
    pinned_status_count: int
    estimated_formula_rows: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _catalog_watermark(connection: Connection, model: type[Any]) -> str:
    row = connection.execute(
        select(
            func.count().label("row_count"),
            func.max(model.created_at).label("created_at"),
            func.max(model.manifest_record_id).label("record_id"),
        )
    ).one()
    payload = json.dumps(
        [row[0], str(row[1]) if row[1] is not None else None, row[2]],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _has_obvious_warmup_capacity(
    pin: CatalogBarPin,
    *,
    scoring_start: datetime,
    lookback_bars: int | None,
) -> bool:
    if lookback_bars == 0:
        return True
    if pin.query.start >= scoring_start:
        return False
    if lookback_bars is None:
        return pin.prefix_row_count > 0
    return pin.prefix_row_count >= lookback_bars


class BacktestService:
    def __init__(
        self,
        *,
        engine: Engine,
        tasks: TaskRepository,
        repository: BacktestRepository,
        market_lake: MarketLake,
        status_lake: ExecutionStatusLake,
        instruments: InstrumentRepository,
        pools: PoolRepository,
        formulas: FormulaService,
    ) -> None:
        with engine.connect() as connection:
            identity = connection_database_identity(connection)
        identities = (
            tasks.database_identity,
            repository.database_identity,
            market_lake.database_identity,
            status_lake.database_identity,
            instruments.database_identity,
            pools.database_identity,
            formulas.database_identity,
        )
        if any(item != identity for item in identities):
            raise ValueError("backtest service database identities do not match")
        self._engine = engine
        self._tasks = tasks
        self._repository = repository
        self._market_lake = market_lake
        self._status_lake = status_lake
        self._instruments = instruments
        self._pools = pools
        self._formulas = formulas

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._repository.database_identity

    def list_runs(
        self, *, limit: int, cursor: str | None
    ) -> BacktestPage[BacktestOverviewSnapshot]:
        return self._repository.list_runs_page(limit=limit, cursor=cursor)

    def get_overview(self, run_id: str) -> BacktestOverviewSnapshot:
        return self._repository.get_overview(run_id)

    def report(self, run_id: str) -> BacktestReportSnapshot:
        return self._repository.report(run_id)

    def page(
        self,
        run_id: str,
        *,
        collection: str,
        limit: int,
        cursor: str | None,
    ) -> BacktestPage[object]:
        return self._repository.page(
            run_id, collection=collection, limit=limit, cursor=cursor
        )

    def cancel(self, run_id: str) -> SubmittedBacktest:
        run = self._repository.get_run(run_id)
        self._tasks.request_cancel(run.task_id)
        return SubmittedBacktest(
            run_id=run.id,
            task_id=run.task_id,
            snapshot_id=run.snapshot.snapshot_id,
            warnings=self._snapshot_warnings(run.snapshot.symbol_inputs),
        )

    def copy(
        self, run_id: str, *, mode: Literal["exact", "latest"]
    ) -> SubmittedBacktest:
        run = self._repository.get_run(run_id)
        if mode == "latest":
            snapshot = run.snapshot
            parameters: dict[str, int | float] = {}
            for item in snapshot.formula_parameters:
                parameters[item.name] = (
                    int(item.value) if item.kind == "integer" else float(item.value)
                )
            return self.submit(
                BacktestIntent(
                    scope_kind=snapshot.scope_kind,
                    symbol=(
                        snapshot.symbols[0] if snapshot.scope_kind == "single" else None
                    ),
                    scope_id=snapshot.scope_id,
                    scope_revision_or_snapshot_id=(
                        None
                        if snapshot.scope_kind in {"preset", "custom"}
                        else snapshot.scope_revision_or_snapshot_id
                    ),
                    formula_version_id=snapshot.formula_version_id,
                    formula_parameters=parameters,
                    period=snapshot.period,
                    adjustment=snapshot.adjustment,
                    scoring_start=snapshot.scoring_start,
                    scoring_end=snapshot.scoring_end,
                    quantity_shares=snapshot.quantity_shares,
                    commission_bps=snapshot.commission_bps,
                    minimum_commission=snapshot.minimum_commission,
                    sell_tax_bps=snapshot.sell_tax_bps,
                    slippage_bps=snapshot.slippage_bps,
                )
            )
        if mode != "exact":
            raise BacktestSubmissionError("backtest copy mode is invalid")
        new_run_id = str(uuid4())
        new_task_id = str(uuid4())
        now = _utc_now()
        with self._engine.begin() as connection:
            self._tasks.enqueue_in_transaction(
                connection,
                "backtest.run",
                {"run_id": new_run_id, "snapshot_id": run.snapshot.snapshot_id},
                task_id=new_task_id,
                now=now,
            )
            self._repository.create_in_transaction(
                connection,
                run_id=new_run_id,
                task_id=new_task_id,
                snapshot=run.snapshot,
                now=now,
            )
        return SubmittedBacktest(
            run_id=new_run_id,
            task_id=new_task_id,
            snapshot_id=run.snapshot.snapshot_id,
            warnings=self._snapshot_warnings(run.snapshot.symbol_inputs),
        )

    @staticmethod
    def _snapshot_warnings(
        inputs: tuple[PinnedMarketRef | FrozenSymbolGap, ...],
    ) -> tuple[str, ...]:
        return (
            ("partial_pool_gaps",)
            if any(isinstance(item, FrozenSymbolGap) for item in inputs)
            else ()
        )

    def _prepare(self, intent: BacktestIntent, *, persist: bool) -> _PreparedBacktest:
        if not isinstance(intent, BacktestIntent):
            raise BacktestSubmissionError("backtest intent is invalid")
        preflight = self._formulas.preflight_backtest(
            intent.formula_version_id, intent.formula_parameters
        )
        if intent.scoring_start >= intent.scoring_end:
            raise BacktestSubmissionError("backtest range is invalid")

        run_id = str(uuid4())
        task_id = str(uuid4())
        now = _utc_now()
        with self._engine.begin() as connection:
            symbols: tuple[str, ...]
            scope_id: str | None
            scope_revision: str | None
            if intent.scope_kind == "single":
                if (
                    intent.symbol is None
                    or intent.scope_id is not None
                    or intent.scope_revision_or_snapshot_id is not None
                ):
                    raise BacktestSubmissionError("single scope is invalid")
                catalog = self._instruments.current_catalog(connection=connection)
                if intent.symbol not in {item.symbol for item in catalog.instruments}:
                    raise BacktestSubmissionError("single symbol is not in the catalog")
                symbols = (intent.symbol,)
                instrument_version = catalog.dataset_version
                scope_id = None
                scope_revision = None
            elif intent.scope_kind == "preset":
                if intent.symbol is not None or intent.scope_id is None:
                    raise BacktestSubmissionError("preset scope is invalid")
                pool = (
                    self._pools.get_current_preset(
                        intent.scope_id, connection=connection
                    )
                    if intent.scope_revision_or_snapshot_id is None
                    else self._pools.get_preset_snapshot(
                        intent.scope_revision_or_snapshot_id,
                        connection=connection,
                    )
                )
                if pool.pool_id != intent.scope_id:
                    raise BacktestSubmissionError("preset snapshot owner is invalid")
                symbols = tuple(member.instrument.symbol for member in pool.members)
                instrument_version = pool.instrument_dataset_version
                scope_id = pool.pool_id
                scope_revision = pool.snapshot_id
            else:
                if intent.symbol is not None or intent.scope_id is None:
                    raise BacktestSubmissionError("custom scope is invalid")
                if intent.scope_revision_or_snapshot_id is None:
                    custom = self._pools.get_current_custom(
                        intent.scope_id, connection=connection
                    )
                else:
                    try:
                        requested_revision = int(intent.scope_revision_or_snapshot_id)
                    except ValueError as error:
                        raise BacktestSubmissionError(
                            "custom pool revision is invalid"
                        ) from error
                    custom = self._pools.get_custom_revision(
                        intent.scope_id,
                        requested_revision,
                        connection=connection,
                    )
                symbols = tuple(member.instrument.symbol for member in custom.members)
                instrument_version = custom.instrument_dataset_version
                scope_id = custom.pool_id
                scope_revision = str(custom.revision)
            desired_signal = tuple(
                BarQuery(
                    symbol=symbol,
                    period=intent.period,
                    adjustment=intent.adjustment,
                    start=intent.scoring_start,
                    end=intent.scoring_end,
                )
                for symbol in symbols
            )
            execution_period = (
                Period.DAY if intent.period is Period.WEEK else intent.period
            )
            desired_execution = tuple(
                BarQuery(
                    symbol=symbol,
                    period=execution_period,
                    adjustment=intent.adjustment,
                    start=intent.scoring_start,
                    end=intent.scoring_end,
                )
                for symbol in symbols
            )
            signal_pins = self._market_lake.catalog_latest_covering_many(
                connection,
                desired_signal,
                prefer_earliest_prefix=preflight.lookback_bars != 0,
            )
            estimated_formula_rows = sum(
                pin.row_count for pin in signal_pins.values() if pin is not None
            )
            execution_pins = self._market_lake.catalog_latest_covering_many(
                connection, desired_execution
            )
            local_start = intent.scoring_start.astimezone(MARKET_TIMEZONE)
            local_end = intent.scoring_end.astimezone(MARKET_TIMEZONE)
            status_end = local_end.date()
            if local_end.timetz().replace(tzinfo=None) != datetime.min.time():
                status_end += timedelta(days=1)
            desired_status = tuple(
                ExecutionStatusQuery(
                    symbol=symbol,
                    exchange=Exchange(symbol.rsplit(".", maxsplit=1)[1]),
                    start=local_start.date(),
                    end=status_end,
                    period=intent.period,
                )
                for symbol in symbols
            )
            status_pins = self._status_lake.catalog_latest_covering_many(
                connection, desired_status
            )
            instrument_watermark = instrument_version
            signal_watermark = _catalog_watermark(connection, MarketRoutingManifest)
            status_watermark = _catalog_watermark(
                connection, ExecutionStatusRoutingManifest
            )
            _catalog_watermark(connection, InstrumentRoutingManifest)
            inputs: list[PinnedMarketRef | FrozenSymbolGap] = []
            for signal_query, execution_query, symbol in zip(
                desired_signal, desired_execution, symbols, strict=True
            ):
                signal = signal_pins.get(symbol)
                execution = execution_pins.get(symbol)
                status = status_pins.get(symbol)
                if signal is not None and not _has_obvious_warmup_capacity(
                    signal,
                    scoring_start=intent.scoring_start,
                    lookback_bars=preflight.lookback_bars,
                ):
                    signal = None
                if signal is None or execution is None or status is None:
                    reason: GapReason = (
                        "missing_signal_data"
                        if signal is None
                        else "missing_execution_data"
                        if execution is None
                        else "missing_execution_status"
                    )
                    inputs.append(
                        FrozenSymbolGap(
                            symbol=symbol,
                            reason=reason,
                            signal_query=signal_query,
                            execution_query=execution_query,
                            checked_instrument_dataset_version=instrument_watermark,
                            checked_signal_catalog_version=signal_watermark,
                            checked_execution_catalog_version=signal_watermark,
                            checked_status_catalog_version=status_watermark,
                        )
                    )
                    continue
                inputs.append(
                    PinnedMarketRef(
                        symbol=symbol,
                        signal_manifest_record_id=signal.manifest_record_id,
                        signal_dataset_version=signal.dataset_version,
                        signal_route_version=signal.route_version,
                        signal_source=signal.source,
                        signal_data_cutoff=signal.data_cutoff,
                        signal_query=signal.query,
                        execution_manifest_record_id=execution.manifest_record_id,
                        execution_dataset_version=execution.dataset_version,
                        execution_route_version=execution.route_version,
                        execution_source=execution.source,
                        execution_data_cutoff=execution.data_cutoff,
                        execution_query=execution.query,
                        execution_status_manifest_record_id=status.manifest_record_id,
                        execution_status_dataset_version=status.dataset_version,
                        execution_status_route_version=status.route_version,
                        execution_status_source=status.source,
                        execution_status_data_cutoff=status.data_cutoff,
                        execution_status_query=status.query,
                    )
                )
            runnable_count = sum(isinstance(item, PinnedMarketRef) for item in inputs)
            if intent.scope_kind == "single" and runnable_count == 0:
                raise BacktestSubmissionError("single symbol data is incomplete")
            if runnable_count == 0:
                raise BacktestSubmissionError("pool has no runnable symbols")
            snapshot = freeze_request(
                BacktestRequest(
                    scope_kind=intent.scope_kind,
                    scope_id=scope_id,
                    scope_revision_or_snapshot_id=scope_revision,
                    instrument_dataset_version=instrument_version,
                    symbols=symbols,
                    formula_version_id=preflight.formula_version_id,
                    formula_checksum=preflight.formula_checksum,
                    formula_engine_version=preflight.engine_version,
                    compatibility_version=preflight.compatibility_version,
                    formula_parameters=preflight.normalized_parameters,
                    symbol_inputs=tuple(inputs),
                    period=intent.period,
                    adjustment=intent.adjustment,
                    scoring_start=intent.scoring_start,
                    scoring_end=intent.scoring_end,
                    quantity_shares=intent.quantity_shares,
                    commission_bps=intent.commission_bps,
                    minimum_commission=intent.minimum_commission,
                    sell_tax_bps=intent.sell_tax_bps,
                    slippage_bps=intent.slippage_bps,
                    backtest_engine_version=BACKTEST_ENGINE_VERSION,
                )
            )
            if persist:
                self._tasks.enqueue_in_transaction(
                    connection,
                    "backtest.run",
                    {"run_id": run_id, "snapshot_id": snapshot.snapshot_id},
                    task_id=task_id,
                    now=now,
                )
                self._repository.create_in_transaction(
                    connection,
                    run_id=run_id,
                    task_id=task_id,
                    snapshot=snapshot,
                    now=now,
                )
        return _PreparedBacktest(
            run_id=run_id,
            task_id=task_id,
            snapshot=snapshot,
            formula=preflight,
            runnable=runnable_count,
            gaps=tuple(
                (item.symbol, item.reason)
                for item in snapshot.symbol_inputs
                if isinstance(item, FrozenSymbolGap)
            ),
            pinned_signal_count=sum(pin is not None for pin in signal_pins.values()),
            pinned_execution_count=sum(
                pin is not None for pin in execution_pins.values()
            ),
            pinned_status_count=sum(pin is not None for pin in status_pins.values()),
            estimated_formula_rows=estimated_formula_rows,
        )

    def preflight(self, intent: BacktestIntent) -> BacktestPreflight:
        prepared = self._prepare(intent, persist=False)
        snapshot = prepared.snapshot
        warnings = self._snapshot_warnings(snapshot.symbol_inputs)
        return BacktestPreflight(
            preview_snapshot_id=snapshot.snapshot_id,
            reservation=False,
            formula_id=prepared.formula.formula_id,
            formula_version_id=prepared.formula.formula_version_id,
            formula_checksum=prepared.formula.formula_checksum,
            engine_version=prepared.formula.engine_version,
            compatibility_version=prepared.formula.compatibility_version,
            normalized_parameters=tuple(
                item.model_dump(mode="json")
                for item in prepared.formula.normalized_parameters
            ),
            scope_kind=snapshot.scope_kind,
            symbol=snapshot.symbols[0] if snapshot.scope_kind == "single" else None,
            scope_id=snapshot.scope_id,
            scope_revision_or_snapshot_id=snapshot.scope_revision_or_snapshot_id,
            total=len(snapshot.symbols),
            runnable=prepared.runnable,
            gap_count=len(prepared.gaps),
            gap_sample=prepared.gaps[:100],
            warnings=warnings,
            period=snapshot.period,
            adjustment=snapshot.adjustment,
            scoring_start=snapshot.scoring_start,
            scoring_end=snapshot.scoring_end,
            warmup_policy_version=snapshot.warmup_policy_version,
            lookback_bars=prepared.formula.lookback_bars,
            unbounded_dependency=prepared.formula.unbounded_dependency,
            pinned_signal_count=prepared.pinned_signal_count,
            pinned_execution_count=prepared.pinned_execution_count,
            pinned_status_count=prepared.pinned_status_count,
            estimated_formula_rows=prepared.estimated_formula_rows,
            execution_rules_version=snapshot.execution_rules_version,
            cost_model_version=snapshot.cost_model_version,
            sizing_version="fixed-lot-v1",
            quantity_shares=snapshot.quantity_shares,
            commission_bps=snapshot.commission_bps,
            minimum_commission=snapshot.minimum_commission,
            sell_tax_bps=snapshot.sell_tax_bps,
            slippage_bps=snapshot.slippage_bps,
            disclaimer="independent trade samples, not portfolio return",
        )

    def submit(self, intent: BacktestIntent) -> SubmittedBacktest:
        prepared = self._prepare(intent, persist=True)
        return SubmittedBacktest(
            run_id=prepared.run_id,
            task_id=prepared.task_id,
            snapshot_id=prepared.snapshot.snapshot_id,
            warnings=self._snapshot_warnings(prepared.snapshot.symbol_inputs),
        )
