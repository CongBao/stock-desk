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
from stock_desk.backtest.repository import (
    BacktestConflict,
    BacktestRepository,
    BacktestRepositoryError,
    _decode_cursor,
    _encode_cursor,
)
from stock_desk.backtest.repository import (
    BacktestOverviewSnapshot,
    BacktestPage,
    BacktestReportSnapshot,
)
from stock_desk.backtest.pool_runner import _trade_from_payload, _trade_payload
from stock_desk.backtest.public_data import public_payload
from stock_desk.backtest.snapshot import freeze_request, reopen_symbol_input
from stock_desk.backtest.events import OrderFilled
from stock_desk.backtest.types import (
    BASIC_EXECUTION_RULES_VERSION,
    BacktestSnapshot,
    FrozenSymbolGap,
    GapReason,
    PinnedMarketRef,
    execution_status_evidence_summary,
)
from stock_desk.formula.service import FormulaBacktestPreflight, FormulaService
from stock_desk.market.calendar import MARKET_TIMEZONE
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import CatalogBarPin, MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.types import (
    Adjustment,
    BarQuery,
    Exchange,
    Period,
    instrument_kind_for_symbol,
)
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
    execution_status_evidence_level: Literal[
        "authoritative", "basic_no_price_limits", "mixed"
    ] = "authoritative"


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


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _pin_payload(reference: PinnedMarketRef, kind: str) -> dict[str, object]:
    if kind == "signal":
        return {
            "manifest_record_id": reference.signal_manifest_record_id,
            "dataset_version": reference.signal_dataset_version,
            "route_version": reference.signal_route_version,
            "source": reference.signal_source.value,
            "data_cutoff": _timestamp_text(reference.signal_data_cutoff),
        }
    if kind == "execution":
        return {
            "manifest_record_id": reference.execution_manifest_record_id,
            "dataset_version": reference.execution_dataset_version,
            "route_version": reference.execution_route_version,
            "source": reference.execution_source.value,
            "data_cutoff": _timestamp_text(reference.execution_data_cutoff),
        }
    if kind != "execution_status":
        raise ValueError("backtest replay pin kind is invalid")
    return {
        "manifest_record_id": reference.execution_status_manifest_record_id,
        "dataset_version": reference.execution_status_dataset_version,
        "route_version": reference.execution_status_route_version,
        "source": reference.execution_status_source.value,
        "data_cutoff": _timestamp_text(reference.execution_status_data_cutoff),
    }


def _replay_cursor_collection(
    snapshot_id: str,
    result_hash: str | None,
    symbol: str,
    trade_ordinal: int,
    signal_series_id: str,
    reference: PinnedMarketRef,
) -> str:
    encoded = json.dumps(
        {
            "snapshot_id": snapshot_id,
            "result_hash": result_hash,
            "symbol": symbol,
            "trade_ordinal": trade_ordinal,
            "signal_series_id": signal_series_id,
            "signal_manifest_record_id": reference.signal_manifest_record_id,
            "execution_manifest_record_id": reference.execution_manifest_record_id,
            "status_manifest_record_id": (
                reference.execution_status_manifest_record_id
            ),
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"replay:{hashlib.sha256(encoded).hexdigest()}"


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
        dimension: str | None = None,
    ) -> BacktestPage[object]:
        return self._repository.page(
            run_id,
            collection=collection,
            limit=limit,
            cursor=cursor,
            dimension=dimension,
        )

    def replay(
        self,
        run_id: str,
        symbol: str,
        trade_ordinal: int,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise BacktestConflict("backtest replay page limit is invalid")
        if type(trade_ordinal) is not int or not 0 <= trade_ordinal <= 2**63 - 1:
            raise BacktestConflict("backtest trade ordinal is invalid")
        record = self._repository.get_replay_record(run_id, symbol, trade_ordinal)
        reference = record.symbol.reference
        if (
            not isinstance(reference, PinnedMarketRef)
            or record.symbol.status != "succeeded"
            or record.symbol.signal_series_id is None
        ):
            raise BacktestConflict("backtest replay is unavailable")
        try:
            trade = _trade_from_payload(record.trade.payload)
        except (KeyError, TypeError, ValueError) as error:
            raise BacktestRepositoryError(
                "backtest trade payload is invalid"
            ) from error
        if (
            trade.symbol != symbol
            or trade.realized is not record.realized
            or trade.formula_version_id != record.snapshot.formula_version_id
            or trade.signal_series_id != record.symbol.signal_series_id
            or trade.market_manifest_ids
            != (
                reference.signal_manifest_record_id,
                reference.execution_manifest_record_id,
            )
            or trade.status_manifest_ids
            != (reference.execution_status_manifest_record_id,)
        ):
            raise BacktestRepositoryError("backtest replay identity is invalid")
        try:
            reopened = reopen_symbol_input(
                reference,
                market_lake=self._market_lake,
                status_lake=self._status_lake,
            )
            assert reopened.signal is not None and reopened.execution is not None
            series = self._formulas.preview_routed(
                record.snapshot.formula_version_id,
                reopened.signal,
                {
                    item.name: (
                        int(item.value) if item.kind == "integer" else float(item.value)
                    )
                    for item in record.snapshot.formula_parameters
                },
            )
        except BacktestRepositoryError:
            raise
        except Exception as error:
            raise BacktestRepositoryError(
                "backtest replay data is unavailable"
            ) from error
        if (
            series.signal_series_id != record.symbol.signal_series_id
            or series.formula_version_id != record.snapshot.formula_version_id
            or series.formula_checksum != record.snapshot.formula_checksum
            or series.manifest_record_id != reference.signal_manifest_record_id
            or series.dataset_version != reference.signal_dataset_version
            or series.route_version != reference.signal_route_version
            or series.symbol != symbol
            or series.period is not record.snapshot.period
            or series.adjustment is not record.snapshot.adjustment
        ):
            raise BacktestRepositoryError("backtest replay formula identity is invalid")
        signal_bars = reopened.signal.result.bars
        if tuple(bar.timestamp for bar in signal_bars) != series.timestamps:
            raise BacktestRepositoryError("backtest replay series is not aligned")
        cursor_collection = _replay_cursor_collection(
            record.snapshot.snapshot_id,
            record.result_hash,
            symbol,
            trade_ordinal,
            series.signal_series_id,
            reference,
        )
        timestamp_ordinals = {
            timestamp: ordinal for ordinal, timestamp in enumerate(series.timestamps)
        }
        entry_ordinal = timestamp_ordinals.get(trade.entry_signal_at)
        if entry_ordinal is None:
            raise BacktestRepositoryError("backtest replay entry signal is missing")
        key = _decode_cursor(cursor, collection=cursor_collection, run_id=run_id)
        if key is None:
            context_bars = min(40, limit - 1)
            start = max(0, entry_ordinal - context_bars)
        else:
            if len(key) != 1 or type(key[0]) is not int or key[0] < 0:
                raise BacktestConflict("backtest cursor is invalid")
            start = key[0] + 1
        end = min(start + limit, len(signal_bars))
        selected = signal_bars[start:end]
        next_cursor = None
        if end < len(signal_bars) and selected:
            next_cursor = _encode_cursor(
                cursor_collection,
                run_id,
                [end - 1],
            )
        execution_bars = {bar.timestamp: bar for bar in reopened.execution.result.bars}
        fill_markers: list[dict[str, object]] = []
        execution_evidence: list[dict[str, object]] = []
        for event in trade.order_events:
            if not isinstance(event, OrderFilled):
                continue
            anchor = timestamp_ordinals.get(event.signal_at)
            evidence = execution_bars.get(event.filled_at)
            if (
                evidence is None
                and reopened.execution.result.query.period is Period.DAY
            ):
                fill_day = event.filled_at.astimezone(MARKET_TIMEZONE).date()
                evidence = next(
                    (
                        bar
                        for bar in reopened.execution.result.bars
                        if bar.timestamp.astimezone(MARKET_TIMEZONE).date() == fill_day
                    ),
                    None,
                )
            if anchor is None or evidence is None:
                raise BacktestRepositoryError(
                    "backtest replay fill evidence is missing"
                )
            reference_open: Decimal | None
            fill_price: Decimal | None
            if event.side == "buy":
                reference_open = trade.entry_reference_open
                fill_price = trade.buy_fill_price
            else:
                reference_open = trade.exit_reference_open
                fill_price = trade.sell_fill_price
            if reference_open is None or fill_price is None:
                raise BacktestRepositoryError(
                    "backtest replay fill identity is invalid"
                )
            fill_markers.append(
                {
                    "side": event.side,
                    "signal_at": _timestamp_text(event.signal_at),
                    "filled_at": _timestamp_text(event.filled_at),
                    "anchor_ordinal": anchor,
                    "reference_open": _decimal_text(reference_open),
                    "fill_price": _decimal_text(fill_price),
                    "quantity": event.quantity,
                }
            )
            execution_evidence.append(
                {
                    "side": event.side,
                    "filled_at": _timestamp_text(event.filled_at),
                    "bar": evidence.model_dump(mode="json"),
                }
            )
        expected_fills = 2 if trade.realized else 1
        if len(fill_markers) != expected_fills:
            raise BacktestRepositoryError("backtest replay fill identity is invalid")
        public_trade = public_payload(_trade_payload(trade))
        if not isinstance(public_trade, dict):
            raise BacktestRepositoryError("backtest trade payload is invalid")
        evidence_level, warnings = execution_status_evidence_summary((reference,))
        return {
            "run_id": run_id,
            "snapshot_id": record.snapshot.snapshot_id,
            "result_hash": record.result_hash,
            "symbol": symbol,
            "trade_ordinal": trade_ordinal,
            "period": record.snapshot.period.value,
            "adjustment": record.snapshot.adjustment.value,
            "execution_status_evidence_level": evidence_level,
            "warnings": list(warnings),
            "bars": [bar.model_dump(mode="json") for bar in selected],
            "formula": {
                "signal_series_id": series.signal_series_id,
                "formula_version_id": series.formula_version_id,
                "formula_checksum": series.formula_checksum,
                "engine_version": series.engine_version,
                "compatibility_version": series.compatibility_version,
                "numeric_outputs": [
                    {"name": output.name, "values": list(output.values[start:end])}
                    for output in series.numeric_outputs
                ],
                "signals": [
                    {"name": signal.name, "values": list(signal.values[start:end])}
                    for signal in series.signals
                ],
            },
            "trade": public_trade,
            "fill_markers": fill_markers,
            "execution_evidence": execution_evidence,
            "provenance": {
                "signal": _pin_payload(reference, "signal"),
                "execution": _pin_payload(reference, "execution"),
                "status": _pin_payload(reference, "execution_status"),
            },
            "next_cursor": next_cursor,
        }

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
        return execution_status_evidence_summary(inputs)[1]

    @staticmethod
    def _execution_status_evidence_level(
        inputs: tuple[PinnedMarketRef | FrozenSymbolGap, ...],
    ) -> Literal["authoritative", "basic_no_price_limits", "mixed"]:
        return execution_status_evidence_summary(inputs)[0]

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
                    instrument_kind=instrument_kind_for_symbol(symbol),
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
                    instrument_kind=instrument_kind_for_symbol(symbol),
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
                        execution_status_evidence_level=status.evidence_level,
                    )
                )
            runnable_count = sum(isinstance(item, PinnedMarketRef) for item in inputs)
            if intent.scope_kind == "single" and runnable_count == 0:
                raise BacktestSubmissionError("single symbol data is incomplete")
            if runnable_count == 0:
                raise BacktestSubmissionError("pool has no runnable symbols")
            evidence_level, _warnings = execution_status_evidence_summary(tuple(inputs))
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
                    execution_rules_version=(
                        "a-share-v1"
                        if evidence_level == "authoritative"
                        else BASIC_EXECUTION_RULES_VERSION
                    ),
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
            execution_status_evidence_level=(
                self._execution_status_evidence_level(snapshot.symbol_inputs)
            ),
        )

    def submit(self, intent: BacktestIntent) -> SubmittedBacktest:
        prepared = self._prepare(intent, persist=True)
        return SubmittedBacktest(
            run_id=prepared.run_id,
            task_id=prepared.task_id,
            snapshot_id=prepared.snapshot.snapshot_id,
            warnings=self._snapshot_warnings(prepared.snapshot.symbol_inputs),
        )
