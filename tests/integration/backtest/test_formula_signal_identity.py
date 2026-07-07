from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.backtest.service import BacktestIntent, BacktestService
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.types import Adjustment, Period
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker
from tests.integration.backtest.test_single_run import MACD
from tests.integration.backtest.test_worker_recovery import _complete_status
from tests.integration.market.lake_test_helpers import local_time, routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


def test_macd_worker_uses_entire_pinned_prefix_and_persists_signal_identity(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'macd-prefix.db'}"
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
    start = date(2024, 1, 1)
    days = tuple(start + timedelta(days=offset) for offset in range(50))
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        original = routed_daily_bars(days, adjustment=Adjustment.NONE)
        market.write(original)
        status.write(_complete_status(days[0], days[-1] + timedelta(days=1)))
        version = formulas.create("MACD", "trading", MACD, {}, placement="subchart")
        intent = BacktestIntent(
            scope_kind="single",
            symbol="600000.SH",
            scope_id=None,
            scope_revision_or_snapshot_id=None,
            formula_version_id=version.id,
            formula_parameters={},
            period=Period.DAY,
            adjustment=Adjustment.NONE,
            scoring_start=local_time(days[35]),
            scoring_end=local_time(days[-1] + timedelta(days=1)),
            quantity_shares=1_000,
            commission_bps=Decimal("2.5"),
            minimum_commission=Decimal("5"),
            sell_tax_bps=Decimal("5"),
            slippage_bps=Decimal("3"),
        )
        submitted = service.submit(intent)
        market.write(
            routed_daily_bars(
                days,
                adjustment=Adjustment.NONE,
                fetched_at=original.result.provenance.fetched_at + timedelta(days=1),
            )
        )
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=status,
            formulas=formula_service,
        )
        worker = TaskWorker(tasks, worker_id="macd-worker")
        worker.register_claimed("backtest.run", runner)

        terminal = worker.run_once()

        assert terminal is not None and terminal.status == "succeeded"
        run = repository.get_run(submitted.run_id)
        assert run.actual_warmup_start == original.result.bars[0].timestamp
        assert run.symbols[0].signal_series_id is not None
        assert run.snapshot.symbol_inputs[0].signal_dataset_version == (
            original.result.provenance.dataset_version
        )
    finally:
        engine.dispose()
