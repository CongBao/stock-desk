from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time as stdlib_time
import threading
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from stock_desk.backtest.models import (
    BacktestAggregateMetricRow,
    BacktestGroupMetricRow,
)
from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.backtest.service import BacktestIntent, BacktestService
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusEvidenceLevel,
    ExecutionStatusQuery,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.provenance import (
    ExecutionStatusRoutingRequest,
    RoutedExecutionStatusSuccess,
    make_routing_manifest,
)
from stock_desk.market.types import (
    Adjustment,
    Exchange,
    MarketCapability,
    Period,
    ProviderId,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import TaskRun
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.models import TaskClaim
from tests.integration.backtest.test_single_run import _intent
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


SHANGHAI = ZoneInfo("Asia/Shanghai")
SIMPLE = "BUY:C>0;SELL:C<0;"


def _complete_status(
    start: date,
    end: date,
    *,
    symbol: str = "600000.SH",
    basic: bool = False,
) -> RoutedExecutionStatusSuccess:
    exchange = Exchange(symbol.rsplit(".", maxsplit=1)[1])
    query = ExecutionStatusQuery(
        symbol=symbol,
        exchange=exchange,
        start=start,
        end=end,
        period=Period.DAY,
    )
    days = tuple(
        ExecutionStatusDay(
            day=start + timedelta(days=offset),
            exchange=exchange,
            is_exchange_open=True,
            suspension_state=SuspensionState.NORMAL,
            raw_upper_limit=None if basic else Decimal("20"),
            raw_lower_limit=None if basic else Decimal("1"),
        )
        for offset in range((end - start).days)
    )
    raw = tuple(
        RawExecutionOpen(
            timestamp=datetime.combine(day.day, time(9, 30), tzinfo=SHANGHAI),
            trading_day=day.day,
            raw_open=Decimal("10"),
        )
        for day in days
    )
    fetched_at = datetime(2024, 1, 10, tzinfo=SHANGHAI)
    status_source = ProviderId.BAOSTOCK if basic else ProviderId.TUSHARE
    result = materialize_execution_status(
        query=query,
        days=days,
        raw_opens=raw,
        source=status_source,
        fetched_at=fetched_at,
        data_cutoff=fetched_at,
        evidence_level=(
            ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS
            if basic
            else ExecutionStatusEvidenceLevel.AUTHORITATIVE
        ),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=query),
        priority=(status_source,),
        attempts=(),
        selected_source=status_source,
        upstream_dataset_version=result.dataset_version,
        upstream_fetched_at=result.fetched_at,
        upstream_data_cutoff=result.data_cutoff,
        upstream_adjustment=None,
    )
    return RoutedExecutionStatusSuccess(result=result, manifest=manifest)


@pytest.mark.parametrize("basic", [False, True], ids=["authoritative", "basic"])
def test_worker_executes_pinned_single_run_and_persists_open_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    basic: bool,
) -> None:
    url = f"sqlite:///{tmp_path / 'worker.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
    status = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    formula_service = FormulaService(repository=formulas, lake=market)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=status,
        instruments=instruments,
        pools=pools,
        formulas=formula_service,
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(
            routed_daily_bars(
                tuple(date(2024, 1, day) for day in range(2, 7)),
                adjustment=Adjustment.NONE,
            )
        )
        status.write(_complete_status(date(2024, 1, 2), date(2024, 1, 7), basic=basic))
        version = formulas.create(
            "简单策略", "trading", SIMPLE, {}, placement="subchart"
        )
        submitted = service.submit(_intent(version.id))
        assert submitted.warnings == (("basic_execution_status",) if basic else ())
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=status,
            formulas=formula_service,
            heartbeat_interval_seconds=0.01,
            # Keep the lease comfortably above shared-runner scheduling jitter.
            # The 10 ms heartbeat interval still proves that slow finalization is
            # renewed, while avoiding a wall-clock race under a loaded CI shard.
            heartbeat_lease_duration=timedelta(seconds=2),
        )
        finish_active = threading.Event()
        finish_heartbeat_seen = threading.Event()
        finish_heartbeats = 0
        original_heartbeat = tasks.heartbeat
        original_finish = repository.finish_claim

        def count_finish_heartbeat(*args, **kwargs):
            nonlocal finish_heartbeats
            if finish_active.is_set():
                finish_heartbeats += 1
                finish_heartbeat_seen.set()
            return original_heartbeat(*args, **kwargs)

        def slow_finish(*args, **kwargs):
            finish_active.set()
            try:
                assert finish_heartbeat_seen.wait(timeout=5)
                stdlib_time.sleep(0.06)
                return original_finish(*args, **kwargs)
            finally:
                finish_active.clear()

        monkeypatch.setattr(tasks, "heartbeat", count_finish_heartbeat)
        monkeypatch.setattr(repository, "finish_claim", slow_finish)
        original_preview = formula_service.preview_routed

        def slow_preview(*args, **kwargs):
            stdlib_time.sleep(0.18)
            return original_preview(*args, **kwargs)

        monkeypatch.setattr(formula_service, "preview_routed", slow_preview)
        claim = tasks.claim_next("backtest-worker", lease_duration=timedelta(seconds=1))
        assert isinstance(claim, TaskClaim)
        result = runner(claim)
        terminal = tasks.get(claim.snapshot.id)

        assert terminal is not None
        assert terminal.status == "succeeded"
        assert terminal.result == result
        run = repository.get_run(submitted.run_id)
        assert run.status == "succeeded"
        assert run.processed == 1
        assert run.failed == 0
        assert run.result_hash is not None
        assert run.snapshot.execution_rules_version == (
            "a-share-v2" if basic else "a-share-v1"
        )
        assert repository.count_trades(run.id, realized=False) == 1
        assert run.symbols[0].signal_series_id is not None
        assert finish_heartbeats > 0
        assert not any(
            thread.name == f"backtest-heartbeat-{submitted.task_id}"
            for thread in threading.enumerate()
        )
        with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="immutable"):
                connection.execute(
                    text(
                        "UPDATE backtest_trade SET realized = realized "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": run.id},
                )
        with engine.begin() as connection:
            with pytest.raises(DBAPIError, match="immutable"):
                connection.execute(
                    text("DELETE FROM backtest_trade WHERE run_id = :run_id"),
                    {"run_id": run.id},
                )
        for insert_prefix in ("INSERT", "INSERT OR REPLACE"):
            with engine.begin() as connection:
                with pytest.raises(DBAPIError, match="immutable"):
                    connection.execute(
                        text(
                            f"{insert_prefix} INTO backtest_log "
                            "(run_id, ordinal, level, message, detail_json) "
                            "VALUES (:run_id, 999, 'info', 'late', '{}')"
                        ),
                        {"run_id": run.id},
                    )
        other_task = tasks.create("test.other", {})
        with engine.connect() as connection:
            run_bytes_before = connection.execute(
                text(
                    "SELECT quote(id), quote(task_id), quote(snapshot_id), "
                    "quote(snapshot_json), quote(status), quote(stage), quote(total), "
                    "quote(processed), quote(failed_count), quote(result_hash), "
                    "quote(actual_warmup_start), quote(created_at), quote(updated_at), "
                    "quote(started_at), quote(finished_at) "
                    "FROM backtest_run WHERE id = :run_id"
                ),
                {"run_id": run.id},
            ).one()
            child_bytes_before = tuple(
                connection.execute(
                    text(
                        "SELECT quote(symbol), quote(ordinal), quote(payload_json) "
                        "FROM backtest_trade WHERE run_id = :run_id "
                        "ORDER BY symbol, ordinal"
                    ),
                    {"run_id": run.id},
                )
            )
        replace_select = text(
            "INSERT OR REPLACE INTO backtest_run "
            "(id, task_id, snapshot_id, snapshot_json, status, stage, total, "
            "processed, failed_count, result_hash, actual_warmup_start, created_at, "
            "updated_at, started_at, finished_at) "
            "SELECT :new_id, :new_task_id, snapshot_id, snapshot_json, 'queued', "
            "'queued', total, 0, 0, NULL, NULL, created_at, updated_at, NULL, NULL "
            "FROM backtest_run WHERE id = :run_id"
        )
        conflicts = (
            {"new_id": run.id, "new_task_id": other_task.id, "run_id": run.id},
            {
                "new_id": "00000000-0000-4000-8000-000000000001",
                "new_task_id": run.task_id,
                "run_id": run.id,
            },
        )
        for parameters in conflicts:
            with engine.begin() as connection:
                with pytest.raises(DBAPIError, match="immutable"):
                    connection.execute(replace_select, parameters)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT quote(id), quote(task_id), quote(snapshot_id), "
                        "quote(snapshot_json), quote(status), quote(stage), quote(total), "
                        "quote(processed), quote(failed_count), quote(result_hash), "
                        "quote(actual_warmup_start), quote(created_at), quote(updated_at), "
                        "quote(started_at), quote(finished_at) "
                        "FROM backtest_run WHERE id = :run_id"
                    ),
                    {"run_id": run.id},
                ).one()
                == run_bytes_before
            )
            assert (
                tuple(
                    connection.execute(
                        text(
                            "SELECT quote(symbol), quote(ordinal), quote(payload_json) "
                            "FROM backtest_trade WHERE run_id = :run_id "
                            "ORDER BY symbol, ordinal"
                        ),
                        {"run_id": run.id},
                    )
                )
                == child_bytes_before
            )

        new_submission = service.submit(_intent(version.id))
        assert repository.get_run(new_submission.run_id).status == "queued"
    finally:
        engine.dispose()


def test_crash_after_cancel_terminalizes_task_and_run_together(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'cancel-crash.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market-cancel").resolve())
    status = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    formula_service = FormulaService(repository=formulas, lake=market)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=status,
        instruments=instruments,
        pools=pools,
        formulas=formula_service,
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(
            routed_daily_bars(
                tuple(date(2024, 1, day) for day in range(2, 7)),
                adjustment=Adjustment.NONE,
            )
        )
        status.write(_complete_status(date(2024, 1, 2), date(2024, 1, 7)))
        version = formulas.create(
            "简单策略", "trading", SIMPLE, {}, placement="subchart"
        )
        submitted = service.submit(_intent(version.id))
        claim = tasks.claim_next("crashing-worker")
        assert isinstance(claim, TaskClaim)
        repository.start_claim(claim, tasks=tasks, now=claim.snapshot.updated_at)
        tasks.request_cancel(submitted.task_id)

        assert tasks.claim_next("reaper", now=claim.lease_expires_at) is None

        assert tasks.get(submitted.task_id).status == "cancelled"
        cancelled_run = repository.get_run(submitted.run_id)
        assert cancelled_run.status == "cancelled"
        assert cancelled_run.finished_at == claim.lease_expires_at
        assert cancelled_run.processed == 0
    finally:
        engine.dispose()


def test_unhandled_current_claim_failure_atomically_fails_task_and_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'fatal-runner.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market-fatal").resolve())
    status = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    formula_service = FormulaService(repository=formulas, lake=market)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=status,
        instruments=instruments,
        pools=pools,
        formulas=formula_service,
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(
            routed_daily_bars(
                tuple(date(2024, 1, day) for day in range(2, 7)),
                adjustment=Adjustment.NONE,
            )
        )
        status.write(_complete_status(date(2024, 1, 2), date(2024, 1, 7)))
        version = formulas.create(
            "简单策略", "trading", SIMPLE, {}, placement="subchart"
        )
        submitted = service.submit(_intent(version.id))
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=status,
            formulas=formula_service,
        )
        claim = tasks.claim_next("fatal-worker")
        assert isinstance(claim, TaskClaim)

        def fail_preflight(*_args, **_kwargs):
            raise RuntimeError("fatal preflight")

        monkeypatch.setattr(formula_service, "preflight_backtest", fail_preflight)
        with pytest.raises(RuntimeError, match="fatal preflight"):
            runner(claim)

        assert tasks.get(submitted.task_id).status == "failed"
        failed_run = repository.get_run(submitted.run_id)
        assert failed_run.status == "failed"
        assert failed_run.stage == "failed"
        assert failed_run.finished_at is not None
    finally:
        engine.dispose()


def test_recovery_with_all_symbols_terminal_keeps_lease_during_slow_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'slow-recovery-finalization.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market-slow-final").resolve())
    status = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    formula_service = FormulaService(repository=formulas, lake=market)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=status,
        instruments=instruments,
        pools=pools,
        formulas=formula_service,
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(
            routed_daily_bars(
                tuple(date(2024, 1, day) for day in range(2, 7)),
                adjustment=Adjustment.NONE,
            )
        )
        status.write(_complete_status(date(2024, 1, 2), date(2024, 1, 7)))
        version = formulas.create(
            "简单策略", "trading", SIMPLE, {}, placement="subchart"
        )
        submitted = service.submit(_intent(version.id))
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=status,
            formulas=formula_service,
            heartbeat_interval_seconds=0.05,
            heartbeat_lease_duration=timedelta(seconds=1),
        )
        original_load = repository.list_trade_payloads

        class SimulatedProcessCrash(BaseException):
            pass

        monkeypatch.setattr(
            repository,
            "list_trade_payloads",
            lambda _run_id: (_ for _ in ()).throw(SimulatedProcessCrash()),
        )
        first = tasks.claim_next(
            "worker-before-final-crash",
            lease_duration=timedelta(seconds=1),
        )
        assert isinstance(first, TaskClaim)
        with pytest.raises(SimulatedProcessCrash):
            runner(first)
        checkpointed = repository.get_run(submitted.run_id)
        assert checkpointed.processed == checkpointed.total == 1
        assert checkpointed.symbols[0].status == "succeeded"

        with engine.connect() as connection:
            renewed_expiry = connection.execute(
                select(TaskRun.lease_expires_at).where(TaskRun.id == submitted.task_id)
            ).scalar_one()
        assert renewed_expiry is not None
        reclaimed = tasks.claim_next(
            "worker-slow-finalization",
            now=renewed_expiry.replace(tzinfo=UTC) + timedelta(microseconds=1),
            lease_duration=timedelta(seconds=1),
        )
        assert isinstance(reclaimed, TaskClaim)

        finalization_active = threading.Event()
        finalization_heartbeat_seen = threading.Event()
        original_heartbeat = tasks.heartbeat

        def observe_finalization_heartbeat(*args, **kwargs):
            result = original_heartbeat(*args, **kwargs)
            if finalization_active.is_set():
                finalization_heartbeat_seen.set()
            return result

        def slow_load(run_id: str):
            finalization_active.set()
            try:
                assert finalization_heartbeat_seen.wait(timeout=5)
                stdlib_time.sleep(1.1)
                return original_load(run_id)
            finally:
                finalization_active.clear()

        monkeypatch.setattr(tasks, "heartbeat", observe_finalization_heartbeat)
        monkeypatch.setattr(repository, "list_trade_payloads", slow_load)
        result = runner(reclaimed)

        assert result["run_id"] == submitted.run_id
        assert finalization_heartbeat_seen.is_set()
        assert tasks.get(submitted.task_id).status == "succeeded"
        assert repository.get_run(submitted.run_id).status == "succeeded"
    finally:
        engine.dispose()


def test_cancel_racing_finalization_keeps_task_and_run_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'cancel-finish-race.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market-race").resolve())
    status = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    formula_service = FormulaService(repository=formulas, lake=market)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=status,
        instruments=instruments,
        pools=pools,
        formulas=formula_service,
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(
            routed_daily_bars(
                tuple(date(2024, 1, day) for day in range(2, 7)),
                adjustment=Adjustment.NONE,
            )
        )
        status.write(_complete_status(date(2024, 1, 2), date(2024, 1, 7)))
        version = formulas.create(
            "简单策略", "trading", SIMPLE, {}, placement="subchart"
        )
        submitted = service.submit(_intent(version.id))
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=status,
            formulas=formula_service,
        )
        claim = tasks.claim_next("racing-worker")
        assert isinstance(claim, TaskClaim)
        finalization = threading.Barrier(2)
        original_finish = repository.finish_claim

        def pause_before_finalization(*args, **kwargs):
            finalization.wait(timeout=10)
            finalization.wait(timeout=10)
            return original_finish(*args, **kwargs)

        monkeypatch.setattr(repository, "finish_claim", pause_before_finalization)
        with ThreadPoolExecutor(max_workers=1) as executor:
            result_future = executor.submit(runner, claim)
            finalization.wait(timeout=10)
            tasks.request_cancel(submitted.task_id)
            finalization.wait(timeout=10)
            result = result_future.result(timeout=10)

        assert result["cancelled"] is True
        assert tasks.get(submitted.task_id).status == "cancelled"
        cancelled = repository.get_run(submitted.run_id)
        assert cancelled.status == "cancelled"
        assert cancelled.stage == "cancelled"
        assert cancelled.result_hash is None
    finally:
        engine.dispose()


def test_reclaim_aggregates_persisted_and_new_trade_samples_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'aggregate-recovery.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market-recovery").resolve())
    status = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    formula_service = FormulaService(repository=formulas, lake=market)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=status,
        instruments=instruments,
        pools=pools,
        formulas=formula_service,
    )
    try:
        symbols = ("000001.SZ", "600000.SH")
        instruments.ingest(
            routed_instruments(
                tuple(instrument(symbol, f"name-{symbol}") for symbol in symbols)
            )
        )
        pool = pools.publish_full_a()
        days = tuple(date(2024, 1, day) for day in range(2, 7))
        for symbol in symbols:
            market.write(
                routed_daily_bars(
                    days,
                    symbol=symbol,
                    adjustment=Adjustment.NONE,
                )
            )
            status.write(
                _complete_status(days[0], days[-1] + timedelta(days=1), symbol=symbol)
            )
        version = formulas.create(
            "简单策略", "trading", SIMPLE, {}, placement="subchart"
        )
        base_intent = _intent(version.id)
        intent = BacktestIntent(
            scope_kind="preset",
            symbol=None,
            scope_id=pool.pool_id,
            scope_revision_or_snapshot_id=pool.snapshot_id,
            formula_version_id=base_intent.formula_version_id,
            formula_parameters=base_intent.formula_parameters,
            period=base_intent.period,
            adjustment=base_intent.adjustment,
            scoring_start=base_intent.scoring_start,
            scoring_end=base_intent.scoring_end,
            quantity_shares=base_intent.quantity_shares,
            commission_bps=base_intent.commission_bps,
            minimum_commission=base_intent.minimum_commission,
            sell_tax_bps=base_intent.sell_tax_bps,
            slippage_bps=base_intent.slippage_bps,
        )
        submitted = service.submit(intent)
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=status,
            formulas=formula_service,
        )
        original_checkpoint = repository.checkpoint_symbol
        checkpoint_count = 0

        class SimulatedProcessCrash(BaseException):
            pass

        def checkpoint_then_crash(*args, **kwargs):
            nonlocal checkpoint_count
            result = original_checkpoint(*args, **kwargs)
            checkpoint_count += 1
            if checkpoint_count == 1:
                raise SimulatedProcessCrash
            return result

        monkeypatch.setattr(repository, "checkpoint_symbol", checkpoint_then_crash)
        first = tasks.claim_next("worker-before-crash")
        assert isinstance(first, TaskClaim)
        with pytest.raises(SimulatedProcessCrash):
            runner(first)
        assert repository.get_run(submitted.run_id).processed == 1
        assert repository.count_trades(submitted.run_id, realized=False) == 1

        monkeypatch.setattr(repository, "checkpoint_symbol", original_checkpoint)
        with engine.connect() as connection:
            renewed_lease_expires_at = connection.execute(
                select(TaskRun.lease_expires_at).where(TaskRun.id == submitted.task_id)
            ).scalar_one()
        assert renewed_lease_expires_at is not None
        renewed_lease_expires_at = renewed_lease_expires_at.replace(tzinfo=UTC)
        reclaimed = tasks.claim_next(
            "worker-after-crash",
            now=renewed_lease_expires_at + timedelta(microseconds=1),
        )
        assert isinstance(reclaimed, TaskClaim)
        runner(reclaimed)

        completed = repository.get_run(submitted.run_id)
        assert completed.status == "succeeded"
        assert completed.processed == 2
        assert repository.count_trades(completed.id, realized=False) == 2
        with engine.connect() as connection:
            overview = connection.execute(
                select(BacktestAggregateMetricRow.payload_json).where(
                    BacktestAggregateMetricRow.run_id == completed.id,
                    BacktestAggregateMetricRow.metric_key == "overview",
                )
            ).scalar_one()
            symbol_groups = tuple(
                connection.execute(
                    select(BacktestGroupMetricRow.group_key).where(
                        BacktestGroupMetricRow.run_id == completed.id,
                        BacktestGroupMetricRow.dimension == "symbol",
                    )
                ).scalars()
            )
        assert overview["open_trades"]["count"] == 2
        assert (
            symbol_groups == ()
        )  # Groups intentionally contain realized samples only.
        assert completed.actual_warmup_start is not None
    finally:
        engine.dispose()
