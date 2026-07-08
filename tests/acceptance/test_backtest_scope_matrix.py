from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.service import BacktestIntent, BacktestService
from stock_desk.formula.service import FormulaService
from stock_desk.market.pools import PoolCategory, PoolComposition
from stock_desk.market.types import Adjustment, Period, ProviderId
from stock_desk.tasks.models import TaskClaim
from tests.backtest_test_helpers import BacktestHarness, local_time, weekday_range


def _intent(
    formula_version_id: str,
    *,
    scope_kind: str,
    scope_id: str,
    revision: str,
    start: datetime,
    end: datetime,
) -> BacktestIntent:
    return BacktestIntent(
        scope_kind=scope_kind,  # type: ignore[arg-type]
        symbol=None,
        scope_id=scope_id,
        scope_revision_or_snapshot_id=revision,
        formula_version_id=formula_version_id,
        formula_parameters={},
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        scoring_start=start,
        scoring_end=end,
        quantity_shares=1_000,
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        sell_tax_bps=Decimal("5"),
        slippage_bps=Decimal("3"),
    )


def test_all_a_index_industry_custom_failure_and_insufficient_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols = ("600000.SH", "000001.SZ", "600001.SH")
    days = weekday_range(date(2024, 1, 1), date(2024, 5, 1))
    observed = datetime(2024, 5, 2, tzinfo=timezone.utc)
    digest = "sha256:" + "a" * 64

    with BacktestHarness.create(tmp_path) as harness:
        harness.seed_instruments(*symbols)
        harness.seed_symbol(symbols[0], Period.DAY, days)
        harness.seed_symbol(symbols[1], Period.DAY, days, phase_offset=3)
        formula = harness.create_formula(
            "范围矩阵",
            "BUY:CROSS(C,MA(C,3));SELL:CROSS(MA(C,3),C);",
        )
        full_a = harness.pools.publish_full_a()
        index = harness.pools.publish_preset(
            PoolComposition(
                preset_key="index-contract",
                category=PoolCategory.INDEX,
                display_name="验收指数",
                symbols=symbols[:2],
                source=ProviderId.AKSHARE,
                dataset_version=digest,
                route_version=digest,
                fetched_at=observed,
                data_cutoff=observed,
                complete=True,
            )
        )
        industry = harness.pools.publish_preset(
            PoolComposition(
                preset_key="industry-contract",
                category=PoolCategory.INDUSTRY,
                display_name="验收行业",
                symbols=(symbols[0],),
                source=ProviderId.AKSHARE,
                dataset_version=digest,
                route_version=digest,
                fetched_at=observed,
                data_cutoff=observed,
                complete=True,
            )
        )
        custom = harness.pools.create_custom(name="验收自定义池", symbols=(symbols[1],))
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
        scoring_start = local_time(days[5])
        scoring_end = local_time(days[-1]) + timedelta(days=1)
        scopes = (
            ("all_a", "preset", full_a.pool_id, full_a.snapshot_id, 3, 2, 1),
            ("index", "preset", index.pool_id, index.snapshot_id, 2, 2, 0),
            ("industry", "preset", industry.pool_id, industry.snapshot_id, 1, 1, 0),
            (
                "custom",
                "custom",
                custom.pool_id,
                str(custom.revision),
                1,
                1,
                0,
            ),
        )
        for label, kind, pool_id, revision, total, runnable, gaps in scopes:
            preflight = service.preflight(
                _intent(
                    formula.id,
                    scope_kind=kind,
                    scope_id=pool_id,
                    revision=revision,
                    start=scoring_start,
                    end=scoring_end,
                )
            )
            assert (
                preflight.scope_kind,
                preflight.total,
                preflight.runnable,
                preflight.gap_count,
            ) == (kind, total, runnable, gaps), label

        submitted = service.submit(
            _intent(
                formula.id,
                scope_kind="preset",
                scope_id=full_a.pool_id,
                revision=full_a.snapshot_id,
                start=scoring_start,
                end=scoring_end,
            )
        )
        claim = harness.tasks.claim_next(
            "scope-contract-worker", lease_duration=timedelta(seconds=30)
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
        original = runner._run_symbol

        def fail_one_symbol(*args: object, **kwargs: object):
            reference = kwargs["reference"]
            if reference.signal_query.symbol == symbols[1]:  # type: ignore[attr-defined]
                raise RuntimeError("bounded synthetic symbol failure")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(runner, "_run_symbol", fail_one_symbol)
        runner(claim)
        run = harness.repository.get_run(submitted.run_id)
        report = harness.repository.report(submitted.run_id)
        failures = service.page(
            submitted.run_id,
            collection="failures",
            limit=10,
            cursor=None,
        ).items

        assert run.status == "partial_failed"
        assert report.outcomes.succeeded == 1
        assert report.outcomes.failed == 1
        assert report.outcomes.data_insufficient == 1
        assert {item.reason for item in failures} == {
            "symbol_execution_failed",
            "missing_signal_data",
        }
