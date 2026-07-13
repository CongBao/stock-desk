from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import math
import threading
from typing import Any

from sqlalchemy import Engine

from stock_desk.backtest.costs import CostModel, normalize_reference_price
from stock_desk.backtest.events import (
    CancellationReason,
    IgnoredSignal,
    IgnoredSignalReason,
    OpenTradeMarked,
    OrderBlocked,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderPending,
    OrderUnfilled,
    SignalCode,
)
from stock_desk.backtest.execution import (
    ExecutionEngine,
    ExecutionRequest,
    ExecutionResult,
    ReferenceOpen,
    SignalBar,
    candidates_from_status,
)
from stock_desk.backtest.grouping import (
    group_by_entry_month,
    group_by_entry_year,
    group_by_symbol,
)
from stock_desk.backtest.metrics import summarize
from stock_desk.backtest.repository import BacktestRepository, BacktestRunSnapshot
from stock_desk.backtest.snapshot import reopen_symbol_input
from stock_desk.backtest.trades import TradeSample, close_trade, mark_open_trade
from stock_desk.backtest.types import BacktestSnapshot, FrozenSymbolGap, PinnedMarketRef
from stock_desk.formula.service import FormulaService, FormulaServiceError
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.lake import MarketLake
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import (
    DesktopCheckpointPause,
    TaskConflict,
    TaskRepository,
    validate_lease_duration,
)


@dataclass(frozen=True, slots=True)
class SymbolRunFailure:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class PoolBacktestResult:
    trades: tuple[TradeSample, ...]
    failed: tuple[SymbolRunFailure, ...]
    succeeded_count: int
    result_hash: str


@dataclass(slots=True)
class _KeepaliveState:
    terminal_succeeded: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parameter_values(reference: Sequence[object]) -> dict[str, int | float]:
    values: dict[str, int | float] = {}
    for item in reference:
        name = getattr(item, "name")
        kind = getattr(item, "kind")
        value = getattr(item, "value")
        values[name] = int(value) if kind == "integer" else float(value)
    return values


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("backtest result floats must be finite")
        return value
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(
            normalized.copy_abs() if normalized.is_zero() else normalized, "f"
        )
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported backtest result value: {type(value).__name__}")


def _payload(value: object) -> dict[str, object]:
    converted = _json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("backtest payload must be an object")
    return converted


def _canonical_order_events(events: tuple[OrderEvent, ...]) -> tuple[OrderEvent, ...]:
    canonical: list[OrderEvent] = []
    for event in events:
        if isinstance(event, OrderFilled):
            canonical.append(
                replace(event, price=normalize_reference_price(event.price))
            )
        elif isinstance(event, OpenTradeMarked):
            entry = normalize_reference_price(event.entry_price)
            mark = normalize_reference_price(event.mark_price)
            canonical.append(
                replace(
                    event,
                    entry_price=entry,
                    mark_price=mark,
                    floating_pnl=(mark - entry) * event.quantity,
                )
            )
        else:
            canonical.append(event)
    return tuple(canonical)


def _trade_payload(sample: TradeSample) -> dict[str, object]:
    payload = _payload(sample)
    payload["order_events"] = [
        {"event_type": type(event).__name__, "payload": _payload(event)}
        for event in sample.order_events
    ]
    return payload


def _datetime_value(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("persisted backtest timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted backtest timestamp must be timezone-aware")
    return parsed


def _decimal_value(value: object) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("persisted backtest Decimal must be finite")
    return parsed


def _event_from_payload(value: object) -> OrderEvent:
    if not isinstance(value, Mapping):
        raise ValueError("persisted order event must be an object")
    event_type = value.get("event_type")
    raw = value.get("payload")
    if not isinstance(event_type, str) or not isinstance(raw, Mapping):
        raise ValueError("persisted order event identity is invalid")
    payload = dict(raw)
    if event_type == "OrderPending":
        return OrderPending(
            side=payload["side"],
            signal_at=_datetime_value(payload["signal_at"]),
            eligible_at=_datetime_value(payload["eligible_at"]),
        )
    if event_type == "IgnoredSignal":
        signal = payload.get("signal")
        return IgnoredSignal(
            reason=IgnoredSignalReason(str(payload["reason"])),
            signal=None if signal is None else SignalCode(str(signal)),
            at=_datetime_value(payload["at"]),
        )
    if event_type == "OrderCancelled":
        return OrderCancelled(
            side=payload["side"],
            reason=CancellationReason(str(payload["reason"])),
            at=_datetime_value(payload["at"]),
        )
    if event_type == "OrderBlocked":
        return OrderBlocked(
            side=payload["side"],
            at=_datetime_value(payload["at"]),
            reason=str(payload["reason"]),
        )
    if event_type == "OrderFilled":
        return OrderFilled(
            side=payload["side"],
            signal_at=_datetime_value(payload["signal_at"]),
            filled_at=_datetime_value(payload["filled_at"]),
            price=_decimal_value(payload["price"]),
            quantity=int(payload["quantity"]),
        )
    if event_type == "OrderUnfilled":
        return OrderUnfilled(
            side=payload["side"],
            signal_at=_datetime_value(payload["signal_at"]),
            eligible_at=_datetime_value(payload["eligible_at"]),
            ended_at=_datetime_value(payload["ended_at"]),
            reason=payload["reason"],
        )
    if event_type == "OpenTradeMarked":
        return OpenTradeMarked(
            entry_at=_datetime_value(payload["entry_at"]),
            entry_price=_decimal_value(payload["entry_price"]),
            quantity=int(payload["quantity"]),
            mark_at=_datetime_value(payload["mark_at"]),
            mark_price=_decimal_value(payload["mark_price"]),
            floating_pnl=_decimal_value(payload["floating_pnl"]),
        )
    raise ValueError(f"unsupported persisted order event: {event_type}")


def _trade_from_payload(raw: Mapping[str, object]) -> TradeSample:
    payload = dict(raw)
    for field_name in (
        "entry_signal_at",
        "entry_fill_at",
        "exit_signal_at",
        "exit_fill_at",
        "mark_at",
    ):
        value = payload[field_name]
        payload[field_name] = None if value is None else _datetime_value(value)
    for field_name in (
        "entry_reference_open",
        "exit_reference_open",
        "mark_price",
        "buy_fill_price",
        "sell_fill_price",
        "buy_commission",
        "sell_commission",
        "sell_tax",
        "slippage_cost",
        "reference_gross_pnl",
        "fill_gross_pnl",
        "invested_cost",
        "net_pnl",
        "net_return",
        "floating_pnl",
        "floating_return",
    ):
        value = payload[field_name]
        payload[field_name] = None if value is None else _decimal_value(value)
    payload["market_manifest_ids"] = tuple(payload["market_manifest_ids"])  # type: ignore[arg-type]
    payload["status_manifest_ids"] = tuple(payload["status_manifest_ids"])  # type: ignore[arg-type]
    raw_events = payload["order_events"]
    if not isinstance(raw_events, Sequence) or isinstance(
        raw_events, (str, bytes, bytearray)
    ):
        raise ValueError("persisted trade events must be an array")
    payload["order_events"] = tuple(_event_from_payload(value) for value in raw_events)
    return TradeSample(**payload)  # type: ignore[arg-type]


def _is_fill(event: OrderEvent, side: str, at: datetime) -> bool:
    return (
        isinstance(event, OrderFilled) and event.side == side and event.filled_at == at
    )


def _is_pending(event: OrderEvent, side: str, signal_at: datetime) -> bool:
    return (
        isinstance(event, OrderPending)
        and event.side == side
        and event.signal_at == signal_at
    )


class PoolBacktestRunner:
    def __init__(
        self,
        *,
        engine: Engine,
        tasks: TaskRepository,
        repository: BacktestRepository,
        market_lake: MarketLake,
        status_lake: ExecutionStatusLake,
        formulas: FormulaService,
        heartbeat_interval_seconds: float = 30.0,
        heartbeat_lease_duration: timedelta = timedelta(minutes=2),
    ) -> None:
        identities = (
            tasks.database_identity,
            repository.database_identity,
            market_lake.database_identity,
            status_lake.database_identity,
            formulas.database_identity,
        )
        if identities[1:] != identities[:-1]:
            raise ValueError("backtest runner database identities do not match")
        self._engine = engine
        self._tasks = tasks
        self._repository = repository
        self._market_lake = market_lake
        self._status_lake = status_lake
        self._formulas = formulas
        if (
            isinstance(heartbeat_interval_seconds, bool)
            or not isinstance(heartbeat_interval_seconds, (int, float))
            or not math.isfinite(heartbeat_interval_seconds)
            or heartbeat_interval_seconds <= 0
        ):
            raise ValueError("heartbeat interval must be a positive finite number")
        self._heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._heartbeat_lease_duration = validate_lease_duration(
            heartbeat_lease_duration
        )

    @contextmanager
    def _keep_claim_alive(self, claim: TaskClaim) -> Iterator[_KeepaliveState]:
        stop = threading.Event()
        errors: list[BaseException] = []
        state = _KeepaliveState()

        def heartbeat_loop() -> None:
            try:
                while not stop.wait(self._heartbeat_interval_seconds):
                    self._tasks.heartbeat(
                        claim.snapshot.id,
                        claim.claim_token,
                        lease_duration=self._heartbeat_lease_duration,
                    )
            except BaseException as error:
                errors.append(error)
                stop.set()

        thread = threading.Thread(
            target=heartbeat_loop,
            name=f"backtest-heartbeat-{claim.snapshot.id}",
            daemon=True,
        )
        thread.start()
        try:
            yield state
        finally:
            stop.set()
            thread.join(timeout=max(1.0, self._heartbeat_interval_seconds * 2))
        if thread.is_alive():
            raise RuntimeError("backtest heartbeat thread did not stop")
        unexpected = tuple(
            error
            for error in errors
            if not (state.terminal_succeeded and isinstance(error, TaskConflict))
        )
        if unexpected:
            raise unexpected[0]

    def __call__(self, claim: TaskClaim) -> Mapping[str, Any]:
        try:
            return self._execute_claim(claim)
        except DesktopCheckpointPause:
            raise
        except Exception:
            try:
                run = self._repository.get_run_by_task(claim.snapshot.id)
                self._repository.fail_claim(
                    claim,
                    tasks=self._tasks,
                    run_id=run.id,
                    reason="backtest_runner_failed",
                    now=_utc_now(),
                )
            except Exception:
                # A stale/expired owner must never alter the replacement owner.
                pass
            raise

    def _execute_claim(self, claim: TaskClaim) -> Mapping[str, Any]:
        run = self._repository.start_claim(claim, tasks=self._tasks, now=_utc_now())
        snapshot = run.snapshot
        with self._keep_claim_alive(claim) as keepalive:
            cancelled, aggregate_payload, group_payloads, actual_warmup_start = (
                self._prepare_claim(claim, run)
            )
            if cancelled:
                cancelled_run = self._repository.cancel_claim(
                    claim,
                    tasks=self._tasks,
                    run_id=run.id,
                    now=_utc_now(),
                )
                result = {
                    "run_id": cancelled_run.id,
                    "cancelled": True,
                    "processed": cancelled_run.processed,
                    "total": cancelled_run.total,
                }
            else:
                assert aggregate_payload is not None
                finished = self._repository.finish_claim(
                    claim,
                    tasks=self._tasks,
                    run_id=run.id,
                    aggregate_payload=aggregate_payload,
                    group_payloads=group_payloads,
                    actual_warmup_start=actual_warmup_start,
                    now=_utc_now(),
                )
                if finished.status == "cancelled":
                    result = {
                        "run_id": finished.id,
                        "cancelled": True,
                        "processed": finished.processed,
                        "total": finished.total,
                    }
                else:
                    assert finished.result_hash is not None
                    result = {
                        "run_id": finished.id,
                        "snapshot_id": snapshot.snapshot_id,
                        "result_hash": finished.result_hash,
                        "processed": finished.processed,
                        "total": finished.total,
                        "failed": finished.failed,
                    }
            keepalive.terminal_succeeded = True
        return result

    def _prepare_claim(
        self,
        claim: TaskClaim,
        run: BacktestRunSnapshot,
    ) -> tuple[
        bool,
        dict[str, object] | None,
        tuple[tuple[str, str, dict[str, object]], ...],
        datetime | None,
    ]:
        snapshot = run.snapshot
        parameters = _parameter_values(snapshot.formula_parameters)
        preflight = self._formulas.preflight_backtest(
            snapshot.formula_version_id, parameters
        )

        for symbol_row in run.symbols:
            if symbol_row.status in {"succeeded", "failed"}:
                continue
            current_task = self._tasks.get(claim.snapshot.id)
            if current_task.cancel_requested:
                break
            self._tasks.heartbeat(
                claim.snapshot.id,
                claim.claim_token,
                lease_duration=self._heartbeat_lease_duration,
            )
            reference = symbol_row.reference
            if isinstance(reference, FrozenSymbolGap):
                self._repository.checkpoint_symbol(
                    claim,
                    tasks=self._tasks,
                    run_id=run.id,
                    symbol=reference.symbol,
                    signal_series_id=None,
                    trade_payloads=(),
                    event_payloads=(),
                    failure_reason=reference.reason,
                    now=_utc_now(),
                )
                self._tasks.pause_at_desktop_checkpoint(claim.snapshot.id)
                continue
            try:
                samples, events, signal_series_id, warmup_start = self._run_symbol(
                    snapshot=snapshot,
                    reference=reference,
                    parameters=parameters,
                    lookback_bars=preflight.lookback_bars,
                )
            except FormulaServiceError:
                # A formula worker failure is run infrastructure failure, not a
                # symbol-level data gap. Publishing an empty partial report here
                # would make identical backtests produce different conclusions.
                raise
            except Exception:
                reason = "symbol_execution_failed"
                self._repository.checkpoint_symbol(
                    claim,
                    tasks=self._tasks,
                    run_id=run.id,
                    symbol=reference.symbol,
                    signal_series_id=None,
                    trade_payloads=(),
                    event_payloads=(),
                    failure_reason=reason,
                    now=_utc_now(),
                )
                self._tasks.pause_at_desktop_checkpoint(claim.snapshot.id)
                continue
            self._repository.checkpoint_symbol(
                claim,
                tasks=self._tasks,
                run_id=run.id,
                symbol=reference.symbol,
                signal_series_id=signal_series_id,
                trade_payloads=tuple(
                    (sample.realized, _trade_payload(sample)) for sample in samples
                ),
                event_payloads=tuple(
                    (type(event).__name__, _payload(event)) for event in events
                ),
                failure_reason=None,
                warmup_start=warmup_start,
                now=_utc_now(),
            )
            self._tasks.pause_at_desktop_checkpoint(claim.snapshot.id)

        current_task = self._tasks.get(claim.snapshot.id)
        if current_task.cancel_requested:
            return True, None, (), None
        all_samples = tuple(
            _trade_from_payload(payload)
            for payload in self._repository.list_trade_payloads(run.id)
        )
        metrics = summarize(all_samples)
        groups = (
            group_by_symbol(all_samples),
            group_by_entry_month(all_samples),
            group_by_entry_year(all_samples),
        )
        return (
            False,
            metrics.to_json_dict(),
            tuple(
                (group.dimension, item.key, item.to_json_dict())
                for group in groups
                for item in group.groups
            ),
            self._repository.minimum_warmup_start(run.id),
        )

    def _run_symbol(
        self,
        *,
        snapshot: BacktestSnapshot,
        reference: PinnedMarketRef,
        parameters: Mapping[str, int | float],
        lookback_bars: int | None,
    ) -> tuple[tuple[TradeSample, ...], tuple[object, ...], str, datetime]:
        reopened = reopen_symbol_input(
            reference,
            market_lake=self._market_lake,
            status_lake=self._status_lake,
        )
        assert reopened.signal is not None
        assert reopened.execution is not None
        assert reopened.execution_status is not None
        signal_routed = reopened.signal
        execution_routed = reopened.execution
        status_routed = reopened.execution_status
        series = self._formulas.preview_routed(
            snapshot.formula_version_id, signal_routed, parameters
        )
        if (
            series.signal_series_id == ""
            or series.formula_checksum != snapshot.formula_checksum
            or series.manifest_record_id != reference.signal_manifest_record_id
            or series.dataset_version != reference.signal_dataset_version
            or series.period is not snapshot.period
            or series.adjustment is not snapshot.adjustment
            or series.parameters != snapshot.formula_parameters
        ):
            raise ValueError("formula signal identity changed")
        scoring_indexes = tuple(
            index
            for index, timestamp in enumerate(series.timestamps)
            if snapshot.scoring_start <= timestamp < snapshot.scoring_end
        )
        if not scoring_indexes:
            raise ValueError("signal series does not cover scoring range")
        first_index = scoring_indexes[0]
        if lookback_bars is None:
            if first_index == 0:
                raise ValueError("unbounded formula lacks pinned warm-up history")
            warmup_start = series.timestamps[0]
        else:
            if first_index < lookback_bars:
                raise ValueError("bounded formula lacks required warm-up history")
            warmup_start = series.timestamps[first_index - lookback_bars]
        signals_by_name = {item.name: item.values for item in series.signals}
        signals = tuple(
            SignalBar(
                timestamp=series.timestamps[index],
                buy=signals_by_name["BUY"][index],
                sell=signals_by_name["SELL"][index],
            )
            for index in scoring_indexes
        )
        reference_opens = tuple(
            ReferenceOpen(timestamp=bar.timestamp, price=bar.open)
            for bar in execution_routed.result.bars
            if snapshot.scoring_start <= bar.timestamp < snapshot.scoring_end
        )
        candidates = tuple(
            item
            for item in candidates_from_status(
                status_routed.result, reference_opens=reference_opens
            )
            if snapshot.scoring_start <= item.timestamp < snapshot.scoring_end
        )
        mark_price = next(
            (
                bar.close
                for bar in reversed(execution_routed.result.bars)
                if bar.timestamp < snapshot.scoring_end
            ),
            None,
        )
        execution = ExecutionEngine().run(
            ExecutionRequest(
                period=snapshot.period,
                signals=signals,
                candidates=candidates,
                quantity=snapshot.quantity_shares,
                ended_at=snapshot.scoring_end,
                mark_price=mark_price,
            )
        )
        if execution.failure is not None:
            raise ValueError(execution.failure.reason)
        samples = self._trade_samples(
            snapshot=snapshot,
            reference=reference,
            signal_series_id=series.signal_series_id,
            execution=execution,
            signal_timestamps=tuple(item.timestamp for item in signals),
        )
        return samples, execution.order_events, series.signal_series_id, warmup_start

    @staticmethod
    def _trade_samples(
        *,
        snapshot: BacktestSnapshot,
        reference: PinnedMarketRef,
        signal_series_id: str,
        execution: ExecutionResult,
        signal_timestamps: tuple[datetime, ...],
    ) -> tuple[TradeSample, ...]:
        model = CostModel(
            commission_bps=snapshot.commission_bps,
            minimum_commission=snapshot.minimum_commission,
            sell_tax_bps=snapshot.sell_tax_bps,
            slippage_bps=snapshot.slippage_bps,
        )
        events = _canonical_order_events(execution.order_events)
        samples: list[TradeSample] = []
        search_start = 0
        for trade in execution.trades:
            entry_fill_index = next(
                index
                for index in range(search_start, len(events))
                if _is_fill(events[index], "buy", trade.entry.timestamp)
            )
            entry_fill = events[entry_fill_index]
            assert isinstance(entry_fill, OrderFilled)
            lifecycle_start = next(
                index
                for index in range(entry_fill_index, search_start - 1, -1)
                if _is_pending(events[index], "buy", entry_fill.signal_at)
            )
            if trade.exit is not None:
                exit_index = next(
                    index
                    for index in range(entry_fill_index + 1, len(events))
                    if _is_fill(events[index], "sell", trade.exit.timestamp)
                )
                exit_fill = events[exit_index]
                assert isinstance(exit_fill, OrderFilled)
                sample = close_trade(
                    entry=trade.entry.price,
                    exit=trade.exit.price,
                    quantity=snapshot.quantity_shares,
                    cost_model=model,
                    symbol=reference.symbol,
                    entry_signal_at=entry_fill.signal_at,
                    entry_fill_at=entry_fill.filled_at,
                    exit_signal_at=exit_fill.signal_at,
                    exit_fill_at=exit_fill.filled_at,
                    holding_bars=sum(
                        entry_fill.filled_at <= timestamp <= exit_fill.signal_at
                        for timestamp in signal_timestamps
                    ),
                    formula_version_id=snapshot.formula_version_id,
                    signal_series_id=signal_series_id,
                    market_manifest_ids=(
                        reference.signal_manifest_record_id,
                        reference.execution_manifest_record_id,
                    ),
                    status_manifest_ids=(
                        reference.execution_status_manifest_record_id,
                    ),
                    order_events=tuple(events[lifecycle_start : exit_index + 1]),
                )
                search_start = exit_index + 1
            else:
                mark = events[-1]
                if not isinstance(mark, OpenTradeMarked):
                    raise ValueError("open trade lacks terminal mark")
                sample = mark_open_trade(
                    entry=trade.entry.price,
                    mark=mark.mark_price,
                    mark_at=mark.mark_at,
                    quantity=snapshot.quantity_shares,
                    cost_model=model,
                    symbol=reference.symbol,
                    entry_signal_at=entry_fill.signal_at,
                    entry_fill_at=entry_fill.filled_at,
                    holding_bars=sum(
                        entry_fill.filled_at <= timestamp < mark.mark_at
                        for timestamp in signal_timestamps
                    ),
                    formula_version_id=snapshot.formula_version_id,
                    signal_series_id=signal_series_id,
                    market_manifest_ids=(
                        reference.signal_manifest_record_id,
                        reference.execution_manifest_record_id,
                    ),
                    status_manifest_ids=(
                        reference.execution_status_manifest_record_id,
                    ),
                    order_events=tuple(events[lifecycle_start:]),
                )
                search_start = len(events)
            samples.append(sample)
        return tuple(samples)
