from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict, replace
from decimal import Decimal
import json
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import event, update

from stock_desk.backtest.repository import BacktestRepository
from stock_desk.backtest.export import stream_export
from stock_desk.backtest.models import BacktestRunRow
from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.service import (
    BacktestIntent,
    BacktestService,
    BacktestSubmissionError,
)
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.market.execution_status_lake import (
    CatalogExecutionStatusPin,
    ExecutionStatusLake,
)
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import CatalogBarPin, MarketLake
from stock_desk.market.pools import PoolRepository, PoolRevisionConflict
from stock_desk.market.types import Adjustment, Period, ProviderId
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.models import TaskClaim
from tests.integration.backtest.test_single_run import MACD, _status
from tests.integration.backtest.test_worker_recovery import _complete_status
from tests.integration.market.lake_test_helpers import local_time, routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


def _pool_intent(version_id: str, pool_id: str, snapshot_id: str) -> BacktestIntent:
    return BacktestIntent(
        scope_kind="preset",
        symbol=None,
        scope_id=pool_id,
        scope_revision_or_snapshot_id=snapshot_id,
        formula_version_id=version_id,
        formula_parameters={},
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        scoring_start=local_time(date(2024, 1, 3)),
        scoring_end=local_time(date(2024, 1, 6)),
        quantity_shares=1_000,
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        sell_tax_bps=Decimal("5"),
        slippage_bps=Decimal("3"),
    )


def _services(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'pool.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
    status = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    pools = PoolRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=status,
        instruments=instruments,
        pools=pools,
        formulas=FormulaService(repository=formulas, lake=market),
    )
    return (
        engine,
        market,
        status,
        instruments,
        pools,
        tasks,
        formulas,
        repository,
        service,
    )


def test_partial_preset_freezes_runnable_and_gap_in_pool_order(tmp_path: Path) -> None:
    (
        engine,
        market,
        statuses,
        instruments,
        pools,
        tasks,
        formulas,
        repository,
        service,
    ) = _services(tmp_path)
    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument("600000.SH", "浦发银行"),
                )
            )
        )
        pool = pools.publish_full_a()
        market.write(
            routed_daily_bars(
                tuple(date(2024, 1, day) for day in range(2, 7)),
                symbol="600000.SH",
                adjustment=Adjustment.NONE,
            )
        )
        statuses.write(_complete_status(date(2024, 1, 2), date(2024, 1, 7)))
        version = formulas.create(
            "简单策略", "trading", "BUY:C>0;SELL:C<0;", {}, placement="subchart"
        )

        submitted = service.submit(
            _pool_intent(version.id, pool.pool_id, pool.snapshot_id)
        )

        run = repository.get_run(submitted.run_id)
        assert tuple(item.symbol for item in run.symbols) == tuple(
            member.instrument.symbol for member in pool.members
        )
        assert [item.reference.__class__.__name__ for item in run.symbols] == [
            "FrozenSymbolGap",
            "PinnedMarketRef",
        ]
        assert submitted.warnings == ("partial_pool_gaps",)
        assert tasks.get(submitted.task_id).payload == {
            "run_id": submitted.run_id,
            "snapshot_id": submitted.snapshot_id,
        }
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=statuses,
            formulas=FormulaService(repository=formulas, lake=market),
        )
        first_claim = tasks.claim_next("crashing-pool-worker")
        assert isinstance(first_claim, TaskClaim)
        started = repository.start_claim(
            first_claim, tasks=tasks, now=first_claim.snapshot.updated_at
        )
        first_symbol = started.symbols[0]
        assert first_symbol.reference.__class__.__name__ == "FrozenSymbolGap"
        repository.checkpoint_symbol(
            first_claim,
            tasks=tasks,
            run_id=started.id,
            symbol=first_symbol.symbol,
            signal_series_id=None,
            trade_payloads=(),
            event_payloads=(),
            failure_reason="missing_signal_data",
            now=first_claim.snapshot.updated_at,
        )
        reclaimed = tasks.claim_next(
            "recovery-pool-worker", now=first_claim.lease_expires_at
        )
        assert isinstance(reclaimed, TaskClaim)
        result = runner(reclaimed)
        terminal = tasks.complete(
            reclaimed.snapshot.id,
            result,
            claim_token=reclaimed.claim_token,
        )
        assert terminal is not None and terminal.status == "succeeded"
        completed = repository.get_run(submitted.run_id)
        assert completed.status == "partial_failed"
        assert completed.processed == 2
        assert completed.failed == 1
        assert repository.count_trades(completed.id, realized=False) == 1
    finally:
        engine.dispose()


def test_zero_runnable_pool_blocks_without_persisting_any_rows(tmp_path: Path) -> None:
    (
        engine,
        _market,
        _statuses,
        instruments,
        pools,
        tasks,
        formulas,
        repository,
        service,
    ) = _services(tmp_path)
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        pool = pools.publish_full_a()
        version = formulas.create("MACD", "trading", MACD, {}, placement="subchart")

        with pytest.raises(BacktestSubmissionError, match="runnable"):
            service.submit(_pool_intent(version.id, pool.pool_id, pool.snapshot_id))
        assert repository.list_run_ids() == ()
        assert tasks.list_recent() == []
    finally:
        engine.dispose()


def test_custom_submission_rejects_stale_revision_without_partial_rows(
    tmp_path: Path,
) -> None:
    (
        engine,
        _market,
        _statuses,
        instruments,
        pools,
        tasks,
        formulas,
        repository,
        service,
    ) = _services(tmp_path)
    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument("600000.SH", "浦发银行"),
                )
            )
        )
        original = pools.create_custom(name="自选", symbols=("600000.SH",))
        pools.update_custom(
            original.pool_id,
            expected_revision=1,
            name="自选二版",
            symbols=("000001.SZ",),
        )
        version = formulas.create("MACD", "trading", MACD, {}, placement="subchart")
        intent = _pool_intent(version.id, original.pool_id, "1")
        intent = replace(intent, scope_kind="custom")

        with pytest.raises(PoolRevisionConflict):
            service.submit(intent)
        assert repository.list_run_ids() == ()
        assert tasks.list_recent() == []
    finally:
        engine.dispose()


def test_copy_latest_custom_resolves_current_revision_while_exact_reuses_pin(
    tmp_path: Path,
) -> None:
    engine, market, statuses, instruments, pools, _, formulas, repository, service = (
        _services(tmp_path)
    )
    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("600000.SH", "浦发银行"),
                    instrument("600001.SH", "邯郸钢铁"),
                ),
                cutoff=datetime(2026, 7, 7, 8, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 7, 9, tzinfo=timezone.utc),
            )
        )
        for symbol in ("600000.SH", "600001.SH"):
            market.write(
                routed_daily_bars(
                    tuple(date(2024, 1, day) for day in range(2, 7)),
                    symbol=symbol,
                    adjustment=Adjustment.NONE,
                )
            )
            statuses.write(_status(symbol, date(2024, 1, 2), date(2024, 1, 7)))
        pool = pools.create_custom(name="自选", symbols=("600000.SH",))
        version = formulas.create(
            "简单策略", "trading", "BUY:C>0;SELL:C<0;", {}, placement="subchart"
        )
        original = service.submit(
            replace(_pool_intent(version.id, pool.pool_id, "1"), scope_kind="custom")
        )
        pools.update_custom(
            pool.pool_id,
            expected_revision=1,
            name="自选二版",
            symbols=("600001.SH",),
        )

        exact = service.copy(original.run_id, mode="exact")
        latest = service.copy(original.run_id, mode="latest")

        assert exact.snapshot_id == original.snapshot_id
        assert repository.get_run(exact.run_id).snapshot.symbols == ("600000.SH",)
        latest_snapshot = repository.get_run(latest.run_id).snapshot
        assert latest.snapshot_id != original.snapshot_id
        assert latest_snapshot.scope_revision_or_snapshot_id == "2"
        assert latest_snapshot.symbols == ("600001.SH",)
    finally:
        engine.dispose()


def test_copy_latest_preset_resolves_current_logical_composition(
    tmp_path: Path,
) -> None:
    engine, market, statuses, instruments, pools, _, formulas, repository, service = (
        _services(tmp_path)
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        first = pools.publish_full_a()
        instruments.ingest(
            routed_instruments(
                (
                    instrument("600000.SH", "浦发银行"),
                    instrument("600001.SH", "邯郸钢铁"),
                ),
                cutoff=datetime(2026, 7, 7, 8, tzinfo=timezone.utc),
                fetched_at=datetime(2026, 7, 7, 9, tzinfo=timezone.utc),
            )
        )
        second = pools.publish_full_a()
        assert second.snapshot_id != first.snapshot_id
        for symbol in ("600000.SH", "600001.SH"):
            market.write(
                routed_daily_bars(
                    tuple(date(2024, 1, day) for day in range(2, 7)),
                    symbol=symbol,
                    adjustment=Adjustment.NONE,
                )
            )
            statuses.write(_status(symbol, date(2024, 1, 2), date(2024, 1, 7)))
        version = formulas.create(
            "简单策略", "trading", "BUY:C>0;SELL:C<0;", {}, placement="subchart"
        )
        original = service.submit(
            _pool_intent(version.id, first.pool_id, first.snapshot_id)
        )

        exact = service.copy(original.run_id, mode="exact")
        latest = service.copy(original.run_id, mode="latest")

        assert exact.snapshot_id == original.snapshot_id
        assert repository.get_run(exact.run_id).snapshot.symbols == ("600000.SH",)
        latest_snapshot = repository.get_run(latest.run_id).snapshot
        assert latest.snapshot_id != original.snapshot_id
        assert latest_snapshot.scope_revision_or_snapshot_id == second.snapshot_id
        assert latest_snapshot.symbols == ("600000.SH", "600001.SH")
    finally:
        engine.dispose()


def test_all_a_submit_uses_bounded_catalog_queries_and_never_reads_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        engine,
        market,
        statuses,
        instruments,
        pools,
        tasks,
        formulas,
        repository,
        service,
    ) = _services(tmp_path)
    try:
        items = tuple(
            instrument(f"{code:06d}.SZ", f"股票{code}") for code in range(1, 10_001)
        )
        instruments.ingest(routed_instruments(items))
        pool = pools.publish_full_a()
        version = formulas.create(
            "简单策略", "trading", "BUY:C>0;SELL:C<0;", {}, placement="subchart"
        )
        digest = "sha256:" + "a" * 64
        observed_at = datetime(2024, 1, 10, tzinfo=timezone.utc)

        def market_pins(_connection, queries, **_kwargs):
            return {
                query.symbol: CatalogBarPin(
                    manifest_record_id=digest,
                    dataset_version=digest,
                    route_version=digest,
                    source=ProviderId.TUSHARE,
                    data_cutoff=observed_at,
                    fetched_at=observed_at,
                    query=query,
                    row_count=1,
                    prefix_row_count=0,
                )
                for query in queries
            }

        def status_pins(_connection, queries):
            return {
                query.symbol: CatalogExecutionStatusPin(
                    manifest_record_id=digest,
                    dataset_version=digest,
                    route_version=digest,
                    source=ProviderId.TUSHARE,
                    data_cutoff=observed_at,
                    query=query,
                )
                for query in queries
            }

        monkeypatch.setattr(market, "catalog_latest_covering_many", market_pins)
        monkeypatch.setattr(statuses, "catalog_latest_covering_many", status_pins)
        monkeypatch.setattr(
            market,
            "read",
            lambda _manifest_id: (_ for _ in ()).throw(
                AssertionError("submit opened market data")
            ),
        )
        monkeypatch.setattr(
            statuses,
            "read",
            lambda _manifest_id: (_ for _ in ()).throw(
                AssertionError("submit opened status content")
            ),
        )
        statements = 0

        def count_statement(*_args, **_kwargs) -> None:
            nonlocal statements
            statements += 1

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            submitted = service.submit(
                _pool_intent(version.id, pool.pool_id, pool.snapshot_id)
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)

        assert statements <= 16
        run = repository.get_run(submitted.run_id)
        assert run.total == 10_000
        assert len(run.symbols) == 10_000
        task = tasks.get(submitted.task_id)
        assert task.status == "queued"
        assert len(json.dumps(dict(task.payload), separators=(",", ":"))) < 512
        finished = datetime(2024, 1, 10, tzinfo=timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == submitted.run_id)
                .values(
                    status="succeeded",
                    stage="completed",
                    processed=10_000,
                    finished_at=finished,
                    updated_at=finished,
                )
            )
        report = repository.report(submitted.run_id)
        encoded_report = json.dumps(asdict(report), default=str, sort_keys=True)
        exported = b"".join(
            stream_export(repository, submitted.run_id, section="trades", format="json")
        )
        assert len(encoded_report.encode()) < 8_192
        assert len(exported) < 8_192
        assert "signal_series_ids" not in encoded_report
        assert b"signal_dataset_versions" not in exported
        assert report.symbol_count == report.runnable_count == 10_000
    finally:
        engine.dispose()


def test_cancel_between_symbols_keeps_checkpoint_and_stops_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        engine,
        market,
        statuses,
        instruments,
        pools,
        tasks,
        formulas,
        repository,
        service,
    ) = _services(tmp_path)
    try:
        instruments.ingest(
            routed_instruments(
                (
                    instrument("000001.SZ", "平安银行"),
                    instrument("600000.SH", "浦发银行"),
                )
            )
        )
        pool = pools.publish_full_a()
        days = tuple(date(2024, 1, day) for day in range(2, 7))
        for symbol in ("000001.SZ", "600000.SH"):
            market.write(
                routed_daily_bars(
                    days,
                    symbol=symbol,
                    adjustment=Adjustment.NONE,
                )
            )
            statuses.write(
                _complete_status(
                    date(2024, 1, 2),
                    date(2024, 1, 7),
                    symbol=symbol,
                )
            )
        version = formulas.create(
            "简单策略", "trading", "BUY:C>0;SELL:C<0;", {}, placement="subchart"
        )
        submitted = service.submit(
            _pool_intent(version.id, pool.pool_id, pool.snapshot_id)
        )
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=statuses,
            formulas=FormulaService(repository=formulas, lake=market),
            heartbeat_interval_seconds=0.01,
            heartbeat_lease_duration=timedelta(milliseconds=250),
        )
        original_checkpoint = repository.checkpoint_symbol
        original_cancel = repository.cancel_claim
        original_heartbeat = tasks.heartbeat
        requested = False
        cancel_active = threading.Event()
        cancel_heartbeat_seen = threading.Event()
        cancel_heartbeats = 0

        def checkpoint_then_cancel(*args, **kwargs):
            nonlocal requested
            result = original_checkpoint(*args, **kwargs)
            if not requested:
                requested = True
                tasks.request_cancel(submitted.task_id)
            return result

        monkeypatch.setattr(repository, "checkpoint_symbol", checkpoint_then_cancel)

        def count_cancel_heartbeat(*args, **kwargs):
            nonlocal cancel_heartbeats
            if cancel_active.is_set():
                cancel_heartbeats += 1
                cancel_heartbeat_seen.set()
            return original_heartbeat(*args, **kwargs)

        def slow_cancel(*args, **kwargs):
            cancel_active.set()
            try:
                assert cancel_heartbeat_seen.wait(timeout=5)
                time.sleep(0.06)
                return original_cancel(*args, **kwargs)
            finally:
                cancel_active.clear()

        monkeypatch.setattr(tasks, "heartbeat", count_cancel_heartbeat)
        monkeypatch.setattr(repository, "cancel_claim", slow_cancel)
        claim = tasks.claim_next(
            "cancelling-worker", lease_duration=timedelta(seconds=1)
        )
        assert isinstance(claim, TaskClaim)
        runner(claim)
        terminal = tasks.get(claim.snapshot.id)

        assert terminal is not None and terminal.status == "cancelled"
        run = repository.get_run(submitted.run_id)
        assert run.status == "cancelled"
        assert run.processed == 1
        assert tuple(item.status for item in run.symbols).count("pending") == 1
        assert repository.count_trades(run.id, realized=False) == 1
        assert cancel_heartbeats > 0
        assert not any(
            thread.name == f"backtest-heartbeat-{submitted.task_id}"
            for thread in threading.enumerate()
        )
    finally:
        engine.dispose()
