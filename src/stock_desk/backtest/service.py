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
from stock_desk.backtest.snapshot import freeze_request
from stock_desk.backtest.types import FrozenSymbolGap, GapReason, PinnedMarketRef
from stock_desk.formula.service import FormulaService
from stock_desk.market.calendar import MARKET_TIMEZONE
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import CatalogBarPin, MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.types import Adjustment, BarQuery, Exchange, Period
from stock_desk.storage.database import connection_database_identity
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

    def submit(self, intent: BacktestIntent) -> SubmittedBacktest:
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
                if (
                    intent.symbol is not None
                    or intent.scope_id is None
                    or intent.scope_revision_or_snapshot_id is None
                ):
                    raise BacktestSubmissionError("preset scope is invalid")
                pool = self._pools.get_preset_snapshot(
                    intent.scope_revision_or_snapshot_id,
                    connection=connection,
                )
                if pool.pool_id != intent.scope_id:
                    raise BacktestSubmissionError("preset snapshot owner is invalid")
                symbols = tuple(member.instrument.symbol for member in pool.members)
                instrument_version = pool.instrument_dataset_version
                scope_id = pool.pool_id
                scope_revision = pool.snapshot_id
            else:
                if (
                    intent.symbol is not None
                    or intent.scope_id is None
                    or intent.scope_revision_or_snapshot_id is None
                ):
                    raise BacktestSubmissionError("custom scope is invalid")
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
                        execution_query=execution.query,
                        execution_status_manifest_record_id=status.manifest_record_id,
                        execution_status_dataset_version=status.dataset_version,
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
        return SubmittedBacktest(
            run_id=run_id,
            task_id=task_id,
            snapshot_id=snapshot.snapshot_id,
            warnings=("partial_pool_gaps",)
            if any(isinstance(item, FrozenSymbolGap) for item in snapshot.symbol_inputs)
            else (),
        )
