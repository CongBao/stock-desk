"""Seed an isolated local market cache, then run the real E2E service trio."""

# ruff: noqa: E402

from __future__ import annotations

from datetime import datetime, timedelta
from collections.abc import Sequence
from decimal import Decimal
import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo
from typing import cast
from pydantic import JsonValue

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_desk.analysis.providers.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from stock_desk.analysis.repository import AnalysisExecutionConfig
from stock_desk.analysis.roles import RoleName
from stock_desk.analysis.runtime import AnalysisPreflightService
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.backtest.service import BacktestIntent, BacktestService
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.market.types import (
    Adjustment,
    Period,
)
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.storage.database import create_engine_for_url
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository
from scripts.dev import supervise
from scripts.seed_demo_data import (
    DemoSymbol,
    DemoResearchDataFactory,
    _routed_instruments,
    _routed_status,
    load_demo_fixture,
    seed_demo_data,
)
from tests.performance.ten_year_a_share import (
    generate_fixture_bars,
    load_fixture_metadata,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


class _DeterministicDemoProvider:
    def __init__(self, execution: AnalysisExecutionConfig) -> None:
        self.provider = execution.provider
        self.model = execution.model

    async def complete(self, request: ModelRequest) -> ModelResponse:
        # Keep the async task observable without contacting a model endpoint.
        await asyncio.sleep(0.05)
        context = request.data_blocks[0]
        role = RoleName(cast(str, context["role"]))
        evidence_ids = cast(list[str], context["allowed_evidence_ids"])
        content: dict[str, object] = {
            "role": role.value,
            "snapshot_id": context["snapshot_id"],
            "summary": f"{role.value} synthetic demo summary",
            "claims": [
                {
                    "text": f"{role.value} synthetic evidence observation",
                    "evidence_ids": [evidence_ids[0]],
                    "stance": "support",
                }
            ],
        }
        if role is RoleName.RISK_DECISION:
            content["proposal"] = {
                "rating": "neutral",
                "confidence": 0.5,
                "confidence_explanation": "Synthetic evidence is balanced.",
            }
        return ModelResponse(  # type: ignore[call-arg]
            provider=self.provider,
            model=self.model,
            content=cast(dict[str, JsonValue], content),
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )


def _demo_provider(execution: AnalysisExecutionConfig) -> ModelProvider:
    return cast(ModelProvider, _DeterministicDemoProvider(execution))


def _seed(
    data_dir: Path,
    *,
    performance_runnable_symbol_limit: int | None = None,
) -> None:
    seed_demo_data(data_dir)
    database_url = f"sqlite:///{data_dir / 'stock-desk.db'}"
    engine = create_engine_for_url(database_url)
    try:
        instruments = InstrumentRepository(engine)
        pools = PoolRepository(engine)
        partial_pool = pools.get_preset("index-synthetic-demo")
        fixture = load_demo_fixture()
        lake = MarketLake(engine=engine, root=data_dir / "market")
        status_lake = ExecutionStatusLake(engine)
        if os.environ.get("STOCK_DESK_PERFORMANCE_MODE") == "1":
            performance_metadata = load_fixture_metadata()
            runnable_symbol_count: int = performance_metadata.runnable_symbol_count
            if performance_runnable_symbol_limit is not None:
                if (
                    type(performance_runnable_symbol_limit) is not int
                    or not 1
                    <= performance_runnable_symbol_limit
                    <= runnable_symbol_count
                ):
                    raise ValueError("performance runnable symbol limit is invalid")
                runnable_symbol_count = performance_runnable_symbol_limit
            performance_fixture = generate_fixture_bars(performance_metadata)
            lake.write(performance_fixture.routed)
            status_lake.write(_routed_status(performance_fixture.routed))
            scope_symbols = tuple(
                DemoSymbol(
                    symbol=f"{601_000 + index:06d}.SH",
                    name=f"Performance Full-A Scope {index + 1:04d} (CC0)",
                    wave_phase=index,
                )
                for index in range(
                    performance_metadata.scope_instrument_count - len(fixture.symbols)
                )
            )
            augmented_fixture = fixture.model_copy(
                update={"symbols": (*fixture.symbols, *scope_symbols)}
            )
            instruments.ingest(_routed_instruments(augmented_fixture))
            pools.publish_full_a(
                preset_key="performance-all-a",
                display_name="Perf Full-A Scope: 5000 metadata / 40 runnable (CC0)",
            )
            runnable_extras = scope_symbols[: runnable_symbol_count - 1]
            for item in runnable_extras:
                generated = generate_fixture_bars(
                    performance_metadata.model_copy(update={"symbol": item.symbol})
                )
                lake.write(generated.routed)
                status_lake.write(_routed_status(generated.routed))
        formulas = FormulaRepository(engine)
        macd_name = next(item.name for item in fixture.formulas if item.key == "macd")
        macd_owner = next(
            formula for formula in formulas.list_formulas() if formula.name == macd_name
        )
        macd = formulas.list_versions(macd_owner.id)[-1]
        if os.environ.get("STOCK_DESK_PERFORMANCE_MODE") == "1":
            for index in range(20):
                formulas.create(
                    f"Performance MACD {index + 1:02d} (CC0 synthetic)",
                    "trading",
                    macd.source,
                    {},
                    placement="subchart",
                )
        tasks = TaskRepository(engine)
        backtests = BacktestRepository(engine)
        service = BacktestService(
            engine=engine,
            tasks=tasks,
            repository=backtests,
            market_lake=lake,
            status_lake=status_lake,
            instruments=instruments,
            pools=pools,
            formulas=FormulaService(repository=formulas, lake=lake),
        )
        held = service.submit(
            BacktestIntent(
                scope_kind="preset",
                symbol=None,
                scope_id=partial_pool.pool_id,
                scope_revision_or_snapshot_id=partial_pool.snapshot_id,
                formula_version_id=macd.id,
                formula_parameters={},
                period=Period.DAY,
                adjustment=Adjustment.QFQ,
                scoring_start=datetime(2024, 2, 10, tzinfo=SHANGHAI),
                scoring_end=datetime(2024, 3, 15, tzinfo=SHANGHAI),
                quantity_shares=1_000,
                commission_bps=Decimal("2.5"),
                minimum_commission=Decimal("5"),
                sell_tax_bps=Decimal("5"),
                slippage_bps=Decimal("1"),
            )
        )
        claim = tasks.claim_next("e2e-held", lease_duration=timedelta(seconds=15))
        if not isinstance(claim, TaskClaim) or claim.snapshot.id != held.task_id:
            raise RuntimeError("E2E held cancellation run was not claimed")
        backtests.start_claim(claim, tasks=tasks, now=claim.snapshot.updated_at)
        # Persist the intent before starting the worker so recovery is independent
        # of browser startup timing. Repeating the request in the UI remains safe;
        # only the real worker's expired-lease sweep terminalizes this running run.
        tasks.request_cancel(held.task_id)
    finally:
        engine.dispose()


def create_e2e_app() -> object:
    """Build the real API with only research transport replaced by demo data."""
    settings = Settings()
    tasks = TaskRepository.open(settings.database_url)
    fixture = load_demo_fixture()
    preflight = AnalysisPreflightService(
        data_service_factory=DemoResearchDataFactory(
            fixture,
            tasks.database_identity,
        ),
        clock=fixture.clock,
    )
    return create_app(
        settings,
        task_repository=tasks,
        analysis_preflight_service=preflight,
    )


def _worker_main() -> int:
    import threading

    settings = Settings()
    fixture = load_demo_fixture()
    runtime = ProductionMarketWorker.open(
        settings,
        worker_id=f"e2e-{os.getpid()}",
        analysis_provider_factory=_demo_provider,
        analysis_data_service_factory=fixture.research_data_service,
    )
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        runtime.run_forever(stop_event)
        return 0
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
        runtime.close()


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        return _worker_main()
    if sys.argv[1:]:
        raise SystemExit("usage: e2e_dev.py [--worker]")
    data_dir = Path(tempfile.mkdtemp(prefix="stock-desk-e2e-")).resolve()
    received_signal: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        _seed(data_dir)
        os.environ["STOCK_DESK_DATA_DIR"] = str(data_dir)
        os.environ["STOCK_DESK_DATABASE_URL"] = (
            f"sqlite:///{data_dir / 'stock-desk.db'}"
        )
        commands = (
            (
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.e2e_dev:create_e2e_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ),
            (sys.executable, "-m", "scripts.e2e_dev", "--worker"),
            ("pnpm", "--dir", "web", "dev"),
        )
        process_file = os.environ.get("STOCK_DESK_PERFORMANCE_PROCESS_FILE")

        def record_processes(
            processes: Sequence[subprocess.Popen[bytes]],
        ) -> None:
            if process_file is None:
                return
            service_pids = [
                process.pid
                for process in processes
                if isinstance(getattr(process, "pid", None), int)
            ]
            destination = Path(process_file).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "supervisor_pid": os.getpid(),
                        "service_pids": service_pids,
                        "service_processes": [
                            {"pid": process.pid, "command": list(command)}
                            for process, command in zip(
                                processes, commands, strict=True
                            )
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, destination)

        if process_file is None:
            return supervise(commands, requested_signal=lambda: received_signal)
        return supervise(
            commands,
            requested_signal=lambda: received_signal,
            on_started=record_processes,
        )
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
