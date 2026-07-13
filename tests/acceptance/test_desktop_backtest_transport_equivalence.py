from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from stock_desk.api.backtests import BacktestReportResponse, BacktestServices
from stock_desk.api.market import MarketServices
from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.repository import BacktestTradeSnapshot
from stock_desk.config import Settings
from stock_desk.desktop_session import DesktopSession, TAURI_WINDOWS_ORIGIN
from stock_desk.formula.service import MACD_TEMPLATE_SOURCE
from stock_desk.main import create_app
from stock_desk.market.types import Period
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import DesktopCheckpointPause, TaskRepository
from tests.backtest_test_helpers import (
    WAVE_FORMULA,
    BacktestHarness,
    CompletedBacktest,
    intraday_timestamps,
    local_time,
    weekday_range,
    weekly_timestamps,
)


SYMBOLS = ("600000.SH", "000001.SZ")
SESSION_SECRET = "desktop-backtest-equivalence-secret-never-exposed"
SOURCE_REVISION = "e" * 40


def _session() -> DesktopSession:
    return DesktopSession(
        origin=TAURI_WINDOWS_ORIGIN,
        secret=SESSION_SECRET,
        host_version="1.1.0",
        frontend_version="1.1.0",
        sidecar_version="1.1.0",
        source_revision=SOURCE_REVISION,
    )


def _timeline(
    period: Period,
) -> tuple[Sequence[date | datetime], datetime, datetime]:
    start = date(2024, 1, 1)
    if period is Period.DAY:
        values = weekday_range(start, date(2024, 6, 1))
        return (
            values,
            local_time(values[45]),
            local_time(values[-1]) + timedelta(days=1),
        )
    if period is Period.WEEK:
        values = weekly_timestamps(start, 64)
        return values, values[45], values[-1] + timedelta(days=7)
    values = intraday_timestamps(start, trading_days=45)
    return values, values[45], values[-1] + timedelta(hours=1)


def _request(
    completed: CompletedBacktest,
    *,
    scope: str,
) -> dict[str, object]:
    snapshot = completed.run.snapshot
    if scope == "single":
        request_scope: dict[str, object] = {
            "kind": "single",
            "symbol": snapshot.symbols[0],
        }
    else:
        assert snapshot.scope_id is not None
        assert snapshot.scope_revision_or_snapshot_id is not None
        request_scope = {
            "kind": "preset",
            "pool_id": snapshot.scope_id,
            "snapshot_id": snapshot.scope_revision_or_snapshot_id,
        }
    return {
        "scope": request_scope,
        "formula_version_id": snapshot.formula_version_id,
        "formula_parameters": {},
        "period": snapshot.period.value,
        "adjustment": snapshot.adjustment.value,
        "scoring_start": snapshot.scoring_start.isoformat(),
        "scoring_end": snapshot.scoring_end.isoformat(),
        "quantity_shares": snapshot.quantity_shares,
        "commission_bps": str(snapshot.commission_bps),
        "minimum_commission": str(snapshot.minimum_commission),
        "sell_tax_bps": str(snapshot.sell_tax_bps),
        "slippage_bps": str(snapshot.slippage_bps),
    }


def _runner(
    harness: BacktestHarness,
    completed: CompletedBacktest,
    *,
    tasks: TaskRepository | None = None,
) -> PoolBacktestRunner:
    return PoolBacktestRunner(
        engine=harness.engine,
        tasks=tasks or harness.tasks,
        repository=harness.repository,
        market_lake=harness.market,
        status_lake=harness.statuses,
        formulas=completed.formulas,
        heartbeat_interval_seconds=1,
        heartbeat_lease_duration=timedelta(seconds=30),
    )


def _claim(tasks: TaskRepository, worker_id: str) -> TaskClaim:
    claim = tasks.claim_next(worker_id, lease_duration=timedelta(seconds=30))
    assert isinstance(claim, TaskClaim)
    return claim


def _canonical_report(payload: Mapping[str, object]) -> dict[str, object]:
    report = dict(payload)
    overview = cast(Mapping[str, object], report["overview"])
    report["overview"] = {
        key: overview[key]
        for key in (
            "snapshot_id",
            "status",
            "stage",
            "total",
            "processed",
            "failed",
            "progress",
            "result_hash",
        )
    }
    return report


def _baseline_report(completed: CompletedBacktest) -> dict[str, object]:
    return cast(
        dict[str, object],
        BacktestReportResponse.from_snapshot(completed.report).model_dump(mode="json"),
    )


def _baseline_page(completed: CompletedBacktest, collection: str) -> list[object]:
    page = completed.service.page(
        completed.run.id,
        collection=collection,
        limit=100,
        cursor=None,
    )
    items = cast(tuple[BacktestTradeSnapshot, ...], page.items)
    return [
        {
            "symbol": item.symbol,
            "ordinal": item.ordinal,
            "payload": dict(item.payload),
        }
        for item in items
    ]


def _desktop_app(
    harness: BacktestHarness,
    completed: CompletedBacktest,
    tmp_path: Path,
) -> tuple[TestClient, DesktopSession]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'backtest-harness.db'}",
        data_dir=tmp_path,
    )
    session = _session()
    market_services = MarketServices(
        engine=harness.engine,
        lake_root=(tmp_path / "market").resolve(),
    )
    services = BacktestServices(
        service=completed.service,
        repository=harness.repository,
        tasks=harness.tasks,
    )
    client = TestClient(
        create_app(
            settings,
            task_repository=harness.tasks,
            market_services=market_services,
            formula_service=completed.formulas,
            backtest_services=services,
            desktop_session=session,
        )
    )
    client.headers.update(
        {
            "Origin": session.origin,
            "Authorization": f"Bearer {session.secret_for_host()}",
        }
    )
    return client, session


def _run_current_service_baseline(
    harness: BacktestHarness,
    *,
    formula_name: str,
    source: str,
    scope: str,
    period: Period,
) -> CompletedBacktest:
    timeline, scoring_start, scoring_end = _timeline(period)
    symbols = SYMBOLS[:1] if scope == "single" else SYMBOLS
    harness.seed_instruments(*symbols)
    for offset, symbol in enumerate(symbols):
        harness.seed_symbol(symbol, period, timeline, phase_offset=offset * 3)
    version = harness.create_formula(formula_name, source)
    if scope == "single":
        return harness.run_single(
            version.id,
            symbol=symbols[0],
            period=period,
            scoring_start=scoring_start,
            scoring_end=scoring_end,
        )
    return harness.run_pool(
        version.id,
        symbols=symbols,
        period=period,
        scoring_start=scoring_start,
        scoring_end=scoring_end,
    )


@pytest.mark.parametrize(
    ("formula_name", "source"),
    (("MACD 金叉死叉", MACD_TEMPLATE_SOURCE), ("自定义波段", WAVE_FORMULA)),
    ids=("macd", "custom"),
)
@pytest.mark.parametrize("scope", ("single", "pool"))
@pytest.mark.parametrize("period", (Period.DAY, Period.WEEK, Period.MIN60))
def test_authenticated_desktop_transport_matches_current_service_for_12_normal_market_cells(
    tmp_path: Path,
    formula_name: str,
    source: str,
    scope: str,
    period: Period,
) -> None:
    with BacktestHarness.create(tmp_path) as harness:
        baseline = _run_current_service_baseline(
            harness,
            formula_name=formula_name,
            source=source,
            scope=scope,
            period=period,
        )
        client, session = _desktop_app(harness, baseline, tmp_path)
        with client:
            request = _request(baseline, scope=scope)
            preflight = client.post("/api/backtests/preflight", json=request)
            submitted = client.post("/api/backtests", json=request)
            assert preflight.status_code == 200
            assert submitted.status_code == 202
            claim = _claim(
                harness.tasks, f"desktop-{formula_name}-{scope}-{period.value}"
            )
            _runner(harness, baseline)(claim)
            run_id = cast(str, submitted.json()["run_id"])
            report = client.get(f"/api/backtests/{run_id}/report")
            trades = client.get(f"/api/backtests/{run_id}/trades?limit=100")
            open_trades = client.get(f"/api/backtests/{run_id}/open?limit=100")

        assert report.status_code == 200
        assert trades.status_code == 200
        assert open_trades.status_code == 200
        assert _canonical_report(report.json()) == _canonical_report(
            _baseline_report(baseline)
        )
        assert trades.json()["items"] == _baseline_page(baseline, "trades")
        assert open_trades.json()["items"] == _baseline_page(baseline, "open")
        desktop_run = harness.repository.get_run(run_id)
        assert desktop_run.snapshot.snapshot_id == baseline.run.snapshot.snapshot_id
        assert desktop_run.result_hash == baseline.run.result_hash
        assert [item.signal_series_id for item in desktop_run.symbols] == [
            item.signal_series_id for item in baseline.run.symbols
        ]
        serialized = report.text + trades.text + open_trades.text
        assert session.secret_for_host() not in serialized
        assert str(tmp_path) not in serialized


def test_pool_checkpoint_resumes_on_new_worker_with_uninterrupted_result(
    tmp_path: Path,
) -> None:
    with BacktestHarness.create(tmp_path) as harness:
        baseline = _run_current_service_baseline(
            harness,
            formula_name="恢复基线",
            source=WAVE_FORMULA,
            scope="pool",
            period=Period.DAY,
        )
        client, _session_value = _desktop_app(harness, baseline, tmp_path)
        with client:
            submitted = client.post(
                "/api/backtests", json=_request(baseline, scope="pool")
            )
            assert submitted.status_code == 202
            run_id = cast(str, submitted.json()["run_id"])

            harness.tasks.request_desktop_checkpoint()
            original_claim = _claim(harness.tasks, "desktop-worker-before-restart")
            with pytest.raises(DesktopCheckpointPause):
                _runner(harness, baseline)(original_claim)

            paused = harness.repository.get_run(run_id)
            assert paused.status == "running"
            assert 0 < paused.processed < paused.total
            assert harness.tasks.wait_for_desktop_checkpoint(0)
            restarted_tasks = TaskRepository(harness.engine)
            assert restarted_tasks.resume_desktop_recovery() == 1

            replacement_claim = _claim(restarted_tasks, "desktop-worker-after-restart")
            assert replacement_claim.snapshot.id == original_claim.snapshot.id
            _runner(harness, baseline, tasks=restarted_tasks)(replacement_claim)
            report = client.get(f"/api/backtests/{run_id}/report")

        resumed = harness.repository.get_run(run_id)
        assert resumed.status == "succeeded"
        assert _canonical_report(report.json()) == _canonical_report(
            _baseline_report(baseline)
        )
        assert resumed.snapshot.snapshot_id == baseline.run.snapshot.snapshot_id
        assert resumed.result_hash == baseline.run.result_hash
        assert [item.signal_series_id for item in resumed.symbols] == [
            item.signal_series_id for item in baseline.run.symbols
        ]
