from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, select, update

from stock_desk.backtest.models import (
    BacktestRunRow,
    BacktestSymbolRow,
    BacktestTradeRow,
)
from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.repository import (
    BacktestConflict,
    BacktestRepository,
    BacktestRepositoryError,
    _encode_cursor,
)
from stock_desk.backtest.service import BacktestIntent, BacktestService
from stock_desk.backtest.types import PinnedMarketRef
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
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
    BarRoutingRequest,
    ExecutionStatusRoutingRequest,
    RoutedBarSuccess,
    RoutedExecutionStatusSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    Exchange,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker
from tests.integration.backtest.test_worker_recovery import _complete_status
from tests.integration.market.lake_test_helpers import local_time, routed_daily_bars
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


REPLAY_FORMULA = "X:C;BUY:VOLUME=1000;SELL:VOLUME=1002;"


def _weekly_bars(mondays: tuple[date, ...]) -> RoutedBarSuccess:
    query = BarQuery(
        symbol="600000.SH",
        period=Period.WEEK,
        adjustment=Adjustment.NONE,
        start=local_time(mondays[0]),
        end=local_time(mondays[-1] + timedelta(days=7)),
    )
    bars = tuple(
        Bar(
            symbol=query.symbol,
            timestamp=local_time(day),
            period=Period.WEEK,
            adjustment=Adjustment.NONE,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=1000 + ordinal,
            status=TradingStatus.NORMAL,
        )
        for ordinal, day in enumerate(mondays)
    )
    data_cutoff = local_time(mondays[-1] + timedelta(days=4), 15)
    fetched_at = local_time(mondays[-1] + timedelta(days=4), 16)
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": query},
        data_cutoff=data_cutoff,
        items=bars,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=ProviderId.TUSHARE,
            fetched_at=fetched_at,
            data_cutoff=data_cutoff,
            adjustment=Adjustment.NONE,
            dataset_version=version,
        ),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=version,
        upstream_fetched_at=fetched_at,
        upstream_data_cutoff=data_cutoff,
        upstream_adjustment=Adjustment.NONE,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


def _weekly_status(start: date, end: date) -> RoutedExecutionStatusSuccess:
    query = ExecutionStatusQuery(
        symbol="600000.SH",
        exchange=Exchange.SH,
        start=start,
        end=end,
        period=Period.WEEK,
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
    raw_opens = tuple(
        RawExecutionOpen(
            timestamp=datetime.combine(
                day.day, time(9, 30), tzinfo=local_time(day.day).tzinfo
            ),
            trading_day=day.day,
            raw_open=Decimal("10"),
        )
        for day in days
    )
    fetched_at = local_time(end - timedelta(days=1), 16)
    result = materialize_execution_status(
        query=query,
        days=days,
        raw_opens=raw_opens,
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


def _completed_run(
    tmp_path: Path,
    *,
    day_count: int = 6,
    formula_source: str = REPLAY_FORMULA,
) -> tuple[
    BacktestService,
    BacktestRepository,
    MarketLake,
    FormulaRepository,
    FormulaService,
    Engine,
    tuple[date, ...],
    str,
]:
    url = f"sqlite:///{tmp_path / 'replay.db'}"
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
    days = tuple(
        date(2024, 1, 1) + timedelta(days=offset) for offset in range(day_count)
    )
    instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
    original = routed_daily_bars(days, adjustment=Adjustment.NONE)
    market.write(original)
    statuses.write(_complete_status(days[0], days[-1] + timedelta(days=1)))
    version = formulas.create(
        "Replay", "trading", formula_source, {}, placement="subchart"
    )
    submitted = service.submit(
        BacktestIntent(
            scope_kind="single",
            symbol="600000.SH",
            scope_id=None,
            scope_revision_or_snapshot_id=None,
            formula_version_id=version.id,
            formula_parameters={},
            period=Period.DAY,
            adjustment=Adjustment.NONE,
            scoring_start=local_time(days[0]),
            scoring_end=local_time(days[-1] + timedelta(days=1)),
            quantity_shares=1_000,
            commission_bps=Decimal("2.5"),
            minimum_commission=Decimal("5"),
            sell_tax_bps=Decimal("5"),
            slippage_bps=Decimal("3"),
        )
    )
    runner = PoolBacktestRunner(
        engine=engine,
        tasks=tasks,
        repository=repository,
        market_lake=market,
        status_lake=statuses,
        formulas=formula_service,
    )
    worker = TaskWorker(tasks, worker_id="replay-worker")
    worker.register_claimed("backtest.run", runner)
    terminal = worker.run_once()
    assert terminal is not None and terminal.status == "succeeded"
    assert repository.count_trades(submitted.run_id, realized=True) == 1
    return (
        service,
        repository,
        market,
        formulas,
        formula_service,
        engine,
        days,
        submitted.run_id,
    )


def test_replay_pages_pinned_bars_and_formula_after_newer_latest_write(
    tmp_path: Path,
) -> None:
    (
        _service,
        repository,
        market,
        formulas,
        _formula_service,
        engine,
        days,
        run_id,
    ) = _completed_run(tmp_path)
    try:
        market.write(
            routed_daily_bars(
                days,
                adjustment=Adjustment.NONE,
                volume_delta=-100,
                fetched_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
            )
        )
        replay_service = BacktestService(
            engine=engine,
            tasks=TaskRepository(engine),
            repository=repository,
            market_lake=market,
            status_lake=ExecutionStatusLake(engine),
            instruments=InstrumentRepository(engine),
            pools=PoolRepository(engine),
            formulas=FormulaService(repository=formulas, lake=market),
        )

        first = cast(
            dict[str, Any],
            replay_service.replay(run_id, "600000.SH", 0, limit=2, cursor=None),
        )
        second = cast(
            dict[str, Any],
            replay_service.replay(
                run_id,
                "600000.SH",
                0,
                limit=2,
                cursor=cast(str | None, first["next_cursor"]),
            ),
        )

        assert [bar["volume"] for bar in first["bars"]] == [1000, 1001]
        assert [bar["volume"] for bar in second["bars"]] == [1002, 1003]
        assert first["formula"]["signals"][0] == {
            "name": "BUY",
            "values": [True, False],
        }
        assert first["next_cursor"] is not None
        assert first["run_id"] == run_id
        assert first["symbol"] == "600000.SH"
        assert first["trade_ordinal"] == 0
        assert len(first["fill_markers"]) == 2
        assert len(first["execution_evidence"]) == 2
        reference = repository.get_run(run_id).snapshot.symbol_inputs[0]
        assert isinstance(reference, PinnedMarketRef)
        assert first["provenance"]["signal"]["manifest_record_id"] == (
            reference.signal_manifest_record_id
        )
    finally:
        engine.dispose()


def test_initial_replay_page_is_anchored_to_a_late_trade_entry(tmp_path: Path) -> None:
    (
        service,
        repository,
        _market,
        _formulas,
        _formula_service,
        engine,
        _days,
        run_id,
    ) = _completed_run(
        tmp_path,
        day_count=60,
        formula_source="X:C;BUY:VOLUME=1050;SELL:VOLUME=1052;",
    )
    try:
        page = service.replay(
            run_id,
            "600000.SH",
            0,
            limit=5,
            cursor=None,
        )

        trade = page["trade"]
        bars = page["bars"]
        assert isinstance(trade, dict)
        assert isinstance(bars, list)
        entry_signal_at = trade["entry_signal_at"]
        assert entry_signal_at in {
            bar["timestamp"] for bar in bars if isinstance(bar, dict)
        }
    finally:
        engine.dispose()


def test_replay_lookup_does_not_materialize_every_run_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        service,
        repository,
        _market,
        _formulas,
        _formula_service,
        engine,
        _days,
        run_id,
    ) = _completed_run(tmp_path)
    try:
        monkeypatch.setattr(
            repository,
            "get_run",
            lambda _run_id: (_ for _ in ()).throw(
                AssertionError("replay must query only the requested symbol")
            ),
        )

        page = service.replay(
            run_id,
            "600000.SH",
            0,
            limit=5,
            cursor=None,
        )

        assert page["symbol"] == "600000.SH"
    finally:
        engine.dispose()


def test_replay_rejects_foreign_cursor_limit_and_signal_identity_corruption(
    tmp_path: Path,
) -> None:
    (
        service,
        repository,
        _market,
        _formulas,
        _formula_service,
        engine,
        _days,
        run_id,
    ) = _completed_run(tmp_path)
    try:
        foreign_cursor = _encode_cursor("replay:foreign", run_id, [0])
        with pytest.raises(BacktestConflict, match="cursor"):
            service.replay(
                run_id,
                "600000.SH",
                0,
                limit=2,
                cursor=foreign_cursor,
            )
        with pytest.raises(BacktestConflict, match="limit"):
            service.replay(
                run_id,
                "600000.SH",
                0,
                limit=501,
                cursor=None,
            )
        with pytest.raises(BacktestConflict, match="ordinal"):
            service.replay(
                run_id,
                "600000.SH",
                2**63,
                limit=2,
                cursor=None,
            )
        with pytest.raises(BacktestConflict, match="ordinal"):
            repository.get_replay_record(run_id, "600000.SH", 2**63)

        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_backtest_symbol_terminal_update"
            )
            connection.execute(
                update(BacktestSymbolRow)
                .where(
                    BacktestSymbolRow.run_id == run_id,
                    BacktestSymbolRow.symbol == "600000.SH",
                )
                .values(signal_series_id="sha256:" + "0" * 64)
            )

        with pytest.raises(BacktestRepositoryError, match="identity"):
            service.replay(
                run_id,
                "600000.SH",
                0,
                limit=2,
                cursor=None,
            )
    finally:
        engine.dispose()


def test_replay_rejects_corrupt_run_snapshot_and_result_identity(
    tmp_path: Path,
) -> None:
    (
        service,
        repository,
        _market,
        _formulas,
        _formula_service,
        engine,
        _days,
        run_id,
    ) = _completed_run(tmp_path)
    run = repository.get_run(run_id)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER trg_backtest_run_terminal_update")
            connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id)
                .values(snapshot_id="sha256:" + "0" * 64)
            )
        with pytest.raises(BacktestRepositoryError, match="snapshot identity"):
            service.replay(
                run_id,
                "600000.SH",
                0,
                limit=5,
                cursor=None,
            )

        with engine.begin() as connection:
            connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id)
                .values(snapshot_id=run.snapshot.snapshot_id, result_hash=None)
            )
        with pytest.raises(BacktestRepositoryError, match="result identity"):
            service.replay(
                run_id,
                "600000.SH",
                0,
                limit=5,
                cursor=None,
            )

        with engine.begin() as connection:
            connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id)
                .values(result_hash="sha256:" + "Z" * 64)
            )
        with pytest.raises(BacktestRepositoryError, match="result identity"):
            service.replay(
                run_id,
                "600000.SH",
                0,
                limit=5,
                cursor=None,
            )

        with engine.begin() as connection:
            connection.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id)
                .values(status="cancelled", result_hash=run.result_hash)
            )
        with pytest.raises(BacktestRepositoryError, match="result identity"):
            service.replay(
                run_id,
                "600000.SH",
                0,
                limit=5,
                cursor=None,
            )
    finally:
        engine.dispose()


def test_replay_canonicalizes_nested_order_events_before_public_response(
    tmp_path: Path,
) -> None:
    (
        service,
        _repository,
        _market,
        _formulas,
        _formula_service,
        engine,
        _days,
        run_id,
    ) = _completed_run(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_backtest_trade_terminal_update"
            )
            payload = dict(
                connection.execute(
                    select(BacktestTradeRow.payload_json).where(
                        BacktestTradeRow.run_id == run_id,
                        BacktestTradeRow.symbol == "600000.SH",
                        BacktestTradeRow.ordinal == 0,
                    )
                ).scalar_one()
            )
            events = list(payload["order_events"])
            event = dict(events[1])
            nested = dict(event["payload"])
            nested["note"] = "TOP-SECRET"
            event["payload"] = nested
            events[1] = event
            payload["order_events"] = events
            connection.execute(
                update(BacktestTradeRow)
                .where(
                    BacktestTradeRow.run_id == run_id,
                    BacktestTradeRow.symbol == "600000.SH",
                    BacktestTradeRow.ordinal == 0,
                )
                .values(payload_json=payload)
            )

        page = service.replay(
            run_id,
            "600000.SH",
            0,
            limit=5,
            cursor=None,
        )

        assert "TOP-SECRET" not in json.dumps(page)
        assert "note" not in json.dumps(page)
    finally:
        engine.dispose()


def test_weekly_replay_keeps_weekly_signal_bars_and_daily_execution_evidence(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'weekly-replay.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    market = MarketLake(engine=engine, root=(tmp_path / "weekly-market").resolve())
    statuses = ExecutionStatusLake(engine)
    instruments = InstrumentRepository(engine)
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
        pools=PoolRepository(engine),
        formulas=formula_service,
    )
    mondays = tuple(date(2024, 1, 1) + timedelta(days=7 * index) for index in range(4))
    daily_days = tuple(
        mondays[0] + timedelta(days=offset)
        for offset in range((mondays[-1] + timedelta(days=7) - mondays[0]).days)
    )
    try:
        instruments.ingest(routed_instruments((instrument("600000.SH", "浦发银行"),)))
        market.write(routed_daily_bars(daily_days, adjustment=Adjustment.NONE))
        market.write(_weekly_bars(mondays))
        statuses.write(
            _weekly_status(daily_days[0], daily_days[-1] + timedelta(days=1))
        )
        version = formulas.create(
            "Weekly Replay",
            "trading",
            REPLAY_FORMULA,
            {},
            placement="subchart",
        )
        submitted = service.submit(
            BacktestIntent(
                scope_kind="single",
                symbol="600000.SH",
                scope_id=None,
                scope_revision_or_snapshot_id=None,
                formula_version_id=version.id,
                formula_parameters={},
                period=Period.WEEK,
                adjustment=Adjustment.NONE,
                scoring_start=local_time(mondays[0]),
                scoring_end=local_time(mondays[-1] + timedelta(days=7)),
                quantity_shares=1_000,
                commission_bps=Decimal("2.5"),
                minimum_commission=Decimal("5"),
                sell_tax_bps=Decimal("5"),
                slippage_bps=Decimal("3"),
            )
        )
        runner = PoolBacktestRunner(
            engine=engine,
            tasks=tasks,
            repository=repository,
            market_lake=market,
            status_lake=statuses,
            formulas=formula_service,
        )
        worker = TaskWorker(tasks, worker_id="weekly-replay-worker")
        worker.register_claimed("backtest.run", runner)
        terminal = worker.run_once()
        assert terminal is not None and terminal.status == "succeeded"

        page = cast(
            dict[str, Any],
            service.replay(
                submitted.run_id,
                "600000.SH",
                0,
                limit=4,
                cursor=None,
            ),
        )

        assert page["period"] == "1w"
        assert {bar["period"] for bar in page["bars"]} == {"1w"}
        assert page["formula"]["signals"] == [
            {"name": "BUY", "values": [True, False, False, False]},
            {"name": "SELL", "values": [False, False, True, False]},
        ]
        assert {item["bar"]["period"] for item in page["execution_evidence"]} == {"1d"}
        assert len(page["fill_markers"]) == 2
    finally:
        engine.dispose()
