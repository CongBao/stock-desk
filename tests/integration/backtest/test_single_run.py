from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from stock_desk.backtest.repository import BacktestRepository
from stock_desk.backtest.service import (
    BacktestIntent,
    BacktestService,
    BacktestSubmissionError,
)
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaPreviewValidationError, FormulaService
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusQuery,
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
from stock_desk.tasks.repository import TaskRepository
from tests.integration.market.lake_test_helpers import local_time, routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


MACD = (
    "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"
    "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
)


def _status(symbol: str, start: date, end: date) -> RoutedExecutionStatusSuccess:
    query = ExecutionStatusQuery(
        symbol=symbol,
        exchange=Exchange.SH,
        start=start,
        end=end,
        period=Period.DAY,
    )
    days = tuple(
        ExecutionStatusDay(
            day=start + timedelta(days=offset),
            exchange=Exchange.SH,
            is_exchange_open=True,
            suspension_state=SuspensionState.NORMAL,
            raw_upper_limit=Decimal("20"),
            raw_lower_limit=Decimal("1"),
        )
        for offset in range((end - start).days)
    )
    fetched_at = datetime(2024, 1, 10, tzinfo=timezone.utc)
    result = materialize_execution_status(
        query=query,
        days=days,
        raw_opens=(),
        source=ProviderId.TUSHARE,
        fetched_at=fetched_at,
        data_cutoff=fetched_at,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=result.dataset_version,
        upstream_fetched_at=result.fetched_at,
        upstream_data_cutoff=result.data_cutoff,
        upstream_adjustment=None,
    )
    return RoutedExecutionStatusSuccess(result=result, manifest=manifest)


def _intent(version_id: str) -> BacktestIntent:
    return BacktestIntent(
        scope_kind="single",
        symbol="600000.SH",
        scope_id=None,
        scope_revision_or_snapshot_id=None,
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


def test_single_submit_freezes_catalog_refs_and_enqueues_bounded_task_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'single.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
    statuses = ExecutionStatusLake(engine)
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
        status_lake=statuses,
        instruments=instruments,
        pools=pools,
        formulas=formula_service,
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        bars = routed_daily_bars(
            tuple(date(2024, 1, day) for day in range(2, 7)),
            adjustment=Adjustment.NONE,
        )
        market.write(bars)
        statuses.write(_status("600000.SH", date(2024, 1, 2), date(2024, 1, 7)))
        version = formulas.create("MACD", "trading", MACD, {}, placement="subchart")

        monkeypatch.setattr(
            market,
            "read",
            lambda _manifest_id: (_ for _ in ()).throw(
                AssertionError("submit must not read parquet")
            ),
        )
        submitted = service.submit(_intent(version.id))

        task = tasks.get(submitted.task_id)
        assert task.payload == {
            "run_id": submitted.run_id,
            "snapshot_id": submitted.snapshot_id,
        }
        run = repository.get_run(submitted.run_id)
        assert run.snapshot.snapshot_id == submitted.snapshot_id
        assert run.total == 1
        assert run.processed == 0
        assert run.failed == 0
        assert run.symbols[0].reference.signal_manifest_record_id
    finally:
        engine.dispose()


def test_invalid_formula_rolls_back_run_symbols_task_and_event(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'rollback.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "market").resolve())
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=ExecutionStatusLake(engine),
        instruments=InstrumentRepository(engine),
        pools=PoolRepository(engine),
        formulas=FormulaService(repository=formulas, lake=market),
    )
    try:
        indicator = formulas.create(
            "均线", "indicator", "X:MA(C,5);", {}, placement="subchart"
        )
        with pytest.raises(FormulaPreviewValidationError):
            service.submit(_intent(indicator.id))
        assert repository.list_run_ids() == ()
        assert tasks.list_recent() == []
    finally:
        engine.dispose()


def test_submit_rejects_obviously_insufficient_bounded_warmup(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'warmup-submit.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "warmup-market").resolve())
    statuses = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=statuses,
        instruments=instruments,
        pools=PoolRepository(engine),
        formulas=FormulaService(repository=formulas, lake=market),
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(
            routed_daily_bars(
                tuple(date(2024, 1, day) for day in range(3, 7)),
                adjustment=Adjustment.NONE,
            )
        )
        statuses.write(_status("600000.SH", date(2024, 1, 3), date(2024, 1, 7)))
        version = formulas.create(
            "三日引用",
            "trading",
            "BUY:REF(C,3)>0;SELL:C<0;",
            {},
            placement="subchart",
        )

        with pytest.raises(BacktestSubmissionError, match="incomplete"):
            service.submit(_intent(version.id))

        assert repository.list_run_ids() == ()
        assert tasks.list_recent() == []
    finally:
        engine.dispose()


def test_submit_uses_exact_catalog_prefix_count_for_sparse_warmup(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'sparse-warmup-submit.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "sparse-market").resolve())
    statuses = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
    tasks = TaskRepository(engine)
    formulas = FormulaRepository(engine)
    repository = BacktestRepository(engine)
    service = BacktestService(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=statuses,
        instruments=instruments,
        pools=PoolRepository(engine),
        formulas=FormulaService(repository=formulas, lake=market),
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        sparse_days = (
            date(2024, 1, 1),
            date(2024, 1, 10),
            date(2024, 1, 11),
            date(2024, 1, 12),
        )
        market.write(routed_daily_bars(sparse_days, adjustment=Adjustment.NONE))
        statuses.write(_status("600000.SH", date(2024, 1, 1), date(2024, 1, 13)))
        version = formulas.create(
            "三日引用",
            "trading",
            "BUY:REF(C,3)>0;SELL:C<0;",
            {},
            placement="subchart",
        )
        intent = replace(
            _intent(version.id),
            scoring_start=local_time(date(2024, 1, 10)),
            scoring_end=local_time(date(2024, 1, 13)),
        )

        with pytest.raises(BacktestSubmissionError, match="incomplete"):
            service.submit(intent)

        assert repository.list_run_ids() == ()
        assert tasks.list_recent() == []
    finally:
        engine.dispose()
