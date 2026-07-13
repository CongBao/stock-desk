from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.v1_backtest_oracle import (
    ORACLE_PATH,
    case_specs,
    load_inputs,
    load_oracle,
    prepare_matrix_case,
    project_completed,
    run_case,
)
from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.service import BacktestService
from stock_desk.formula.service import FormulaService
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import DesktopCheckpointPause, TaskRepository
from tests.backtest_test_helpers import BacktestHarness, CompletedBacktest


ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "tests/fixtures/backtest/v1_0_oracle_inputs.json"
_INPUTS = load_inputs(INPUTS)
_ORACLE = load_oracle(ORACLE_PATH, inputs_path=INPUTS)
_CASES = case_specs(_INPUTS)


def _checkpoint_business_logs(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    recovery = [
        item
        for item in logs
        if item
        == {
            "detail": {"attempt": 2},
            "level": "info",
            "message": "run_started",
            "ordinal": 2,
        }
    ]
    assert len(recovery) == 1
    retained = [item for item in logs if item is not recovery[0]]
    return [dict(item, ordinal=ordinal) for ordinal, item in enumerate(retained)]


@pytest.mark.parametrize("spec", _CASES, ids=[str(item["id"]) for item in _CASES])
def test_current_backtest_business_semantics_match_frozen_v1_oracle(
    spec: dict[str, object],
    tmp_path: Path,
) -> None:
    actual = run_case(spec, tmp_path)
    expected = _ORACLE["cases"][str(spec["id"])]["semantic"]

    assert actual == expected


def test_resumed_current_pool_run_matches_uninterrupted_v1_oracle(
    tmp_path: Path,
) -> None:
    spec = next(item for item in _CASES if item["id"] == "custom_pool_1d")
    with BacktestHarness.create(tmp_path) as harness:
        intent = prepare_matrix_case(harness, spec)
        formulas = FormulaService(
            repository=harness.formula_repository,
            lake=harness.market,
        )
        service = BacktestService(
            engine=harness.engine,
            tasks=harness.tasks,
            repository=harness.repository,
            market_lake=harness.market,
            status_lake=harness.statuses,
            instruments=harness.instruments,
            pools=harness.pools,
            formulas=formulas,
        )
        submitted = service.submit(intent)
        harness.tasks.request_desktop_checkpoint()
        claim = harness.tasks.claim_next(
            "v1-oracle-worker-before-restart",
            lease_duration=timedelta(seconds=30),
        )
        assert isinstance(claim, TaskClaim)
        runner = PoolBacktestRunner(
            engine=harness.engine,
            tasks=harness.tasks,
            repository=harness.repository,
            market_lake=harness.market,
            status_lake=harness.statuses,
            formulas=formulas,
            heartbeat_interval_seconds=1,
            heartbeat_lease_duration=timedelta(seconds=30),
        )
        with pytest.raises(DesktopCheckpointPause):
            runner(claim)

        paused = harness.repository.get_run(submitted.run_id)
        assert 0 < paused.processed < paused.total
        assert harness.tasks.wait_for_desktop_checkpoint(0)
        restarted_tasks = TaskRepository(harness.engine)
        assert restarted_tasks.resume_desktop_recovery() == 1
        resumed_claim = restarted_tasks.claim_next(
            "v1-oracle-worker-after-restart",
            lease_duration=timedelta(seconds=30),
        )
        assert isinstance(resumed_claim, TaskClaim)
        resumed_runner = PoolBacktestRunner(
            engine=harness.engine,
            tasks=restarted_tasks,
            repository=harness.repository,
            market_lake=harness.market,
            status_lake=harness.statuses,
            formulas=formulas,
            heartbeat_interval_seconds=1,
            heartbeat_lease_duration=timedelta(seconds=30),
        )
        resumed_runner(resumed_claim)
        run = harness.repository.get_run(submitted.run_id)
        completed = CompletedBacktest(
            submitted=submitted,
            run=run,
            report=service.report(submitted.run_id),
            service=service,
            formulas=formulas,
        )
        actual = project_completed(completed, harness)

    expected = deepcopy(_ORACLE["cases"]["custom_pool_1d"]["semantic"])
    actual_logs = actual["collections"].pop("logs")
    expected_logs = expected["collections"].pop("logs")

    assert actual == expected
    assert _checkpoint_business_logs(actual_logs) == expected_logs
