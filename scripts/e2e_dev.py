"""Seed an isolated local market cache, then run the real E2E service trio."""

# ruff: noqa: E402

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import importlib
import asyncio
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from zoneinfo import ZoneInfo
from typing import cast
from pydantic import JsonValue

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_desk.api.settings import (
    PublicSourceSettings,
    SourcePriorities,
    SourceSettingsServices,
)
from stock_desk.analysis.model_catalog import AnalysisModelCatalog, ModelConfigStatus
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
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
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusQuery,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolCategory, PoolComposition, PoolRepository
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
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
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.provenance import (
    ExecutionStatusRoutingRequest,
    RoutedExecutionStatusSuccess,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository
from scripts.dev import supervise
from scripts.seed_demo_data import DemoResearchDataFactory, load_demo_fixture


SHANGHAI = ZoneInfo("Asia/Shanghai")
MACD_SOURCE = (
    "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"
    "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
)
CUSTOM_SOURCE = (
    "FAST:EMA(C,3);SLOW:EMA(C,7);BUY:CROSS(FAST,SLOW);SELL:CROSS(SLOW,FAST);"
)


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


def _local(day: date, clock: time = time()) -> datetime:
    return datetime.combine(day, clock, tzinfo=SHANGHAI)


def _routed(
    period: Period,
    adjustment: Adjustment,
    *,
    symbol: str = "600000.SH",
) -> RoutedBarSuccess:
    start_day = date(2024, 1, 1)
    if period is Period.DAY:
        timestamps = tuple(
            _local(start_day + timedelta(days=index)) for index in range(650)
        )
    elif period is Period.WEEK:
        timestamps = tuple(
            _local(start_day + timedelta(days=7 * index)) for index in range(100)
        )
    else:
        timestamps = tuple(
            _local(start_day + timedelta(days=day_index), clock)
            for day_index in range(160)
            for clock in (time(9, 30), time(10, 30), time(13), time(14))
        )
    query = BarQuery(
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        start=timestamps[0],
        end=timestamps[-1]
        + (timedelta(hours=1) if period is Period.MIN60 else timedelta(days=1)),
    )
    bars = []
    previous = Decimal("10")
    for index, timestamp in enumerate(timestamps):
        phase = index % 20
        wave = phase if phase <= 10 else 20 - phase
        close = Decimal("9.5") + Decimal(wave) / Decimal("10")
        bars.append(
            Bar(
                symbol=query.symbol,
                timestamp=timestamp,
                period=period,
                adjustment=adjustment,
                open=previous,
                high=max(previous, close) + Decimal("0.2"),
                low=min(previous, close) - Decimal("0.2"),
                close=close,
                volume=1_000 + index,
                status=TradingStatus.NORMAL,
            )
        )
        previous = close
    bar_series = tuple(bars)
    observed = _local(timestamps[-1].astimezone(SHANGHAI).date(), time(16))
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": query},
        data_cutoff=observed,
        items=bar_series,
    )
    result = BarResult(
        query=query,
        bars=bar_series,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=ProviderId.TUSHARE,
            fetched_at=observed,
            data_cutoff=observed,
            adjustment=adjustment,
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
        upstream_fetched_at=observed,
        upstream_data_cutoff=observed,
        upstream_adjustment=adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


def _execution_status(
    *,
    symbol: str,
    period: Period,
    start: date,
    end: date,
    timestamps: tuple[datetime, ...],
) -> RoutedExecutionStatusSuccess:
    exchange = Exchange(symbol.rsplit(".", maxsplit=1)[1])
    query = ExecutionStatusQuery(
        symbol=symbol,
        exchange=exchange,
        start=start,
        end=end,
        period=period,
    )
    days = tuple(
        ExecutionStatusDay(
            day=start + timedelta(days=index),
            exchange=exchange,
            is_exchange_open=True,
            suspension_state=SuspensionState.NORMAL,
            raw_upper_limit=Decimal("20"),
            raw_lower_limit=Decimal("1"),
        )
        for index in range((end - start).days)
    )
    if period is Period.MIN60:
        raw_timestamps = timestamps
    else:
        raw_timestamps = tuple(_local(item.day, time(9, 30)) for item in days)
    raw_opens = tuple(
        RawExecutionOpen(
            timestamp=timestamp,
            trading_day=timestamp.astimezone(SHANGHAI).date(),
            raw_open=Decimal("10"),
        )
        for timestamp in raw_timestamps
    )
    observed = _local(end, time(16))
    result = materialize_execution_status(
        query=query,
        days=days,
        raw_opens=raw_opens,
        source=ProviderId.TUSHARE,
        fetched_at=observed,
        data_cutoff=observed,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=result.dataset_version,
        upstream_fetched_at=observed,
        upstream_data_cutoff=observed,
        upstream_adjustment=None,
    )
    return RoutedExecutionStatusSuccess(result=result, manifest=manifest)


def _seed(data_dir: Path) -> None:
    database_url = f"sqlite:///{data_dir / 'stock-desk.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    settings = Settings(database_url=database_url, data_dir=data_dir)
    tdx_root = data_dir / "tdx"
    target = tdx_root / "vipdoc" / "sh" / "lday" / "sh600000.day"
    target.parent.mkdir(parents=True, exist_ok=True)
    fixture = ROOT / "tests" / "fixtures" / "tdx" / "sh600000.day.hex"
    target.write_bytes(bytes.fromhex(fixture.read_text(encoding="ascii").strip()))
    try:
        helpers = importlib.import_module("tests.integration.market.task6_test_helpers")
        instruments = InstrumentRepository(engine)
        instruments.ingest(
            helpers.routed_instruments(
                (
                    helpers.instrument("000001.SZ", "平安银行"),
                    helpers.instrument("600000.SH", "浦发银行"),
                    helpers.instrument("600036.SH", "招商银行"),
                )
            )
        )
        pools = PoolRepository(engine)
        pools.publish_full_a()
        catalog = instruments.current_manifest()
        partial_pool = None
        for key, category, name, symbols, digest_character in (
            (
                "index-e2e",
                PoolCategory.INDEX,
                "E2E 指数",
                ("000001.SZ", "600000.SH"),
                "a",
            ),
            (
                "industry-e2e",
                PoolCategory.INDUSTRY,
                "E2E 行业",
                ("600000.SH", "600036.SH"),
                "b",
            ),
            (
                "partial-e2e",
                PoolCategory.INDEX,
                "E2E 部分池",
                ("600000.SH", "000001.SZ"),
                "c",
            ),
        ):
            digest = f"sha256:{digest_character * 64}"
            published = pools.publish_preset(
                PoolComposition(
                    preset_key=key,
                    category=category,
                    display_name=name,
                    symbols=symbols,
                    source=ProviderId.AKSHARE,
                    dataset_version=digest,
                    route_version=digest,
                    fetched_at=catalog.fetched_at,
                    data_cutoff=catalog.data_cutoff,
                    complete=True,
                )
            )
            if key == "partial-e2e":
                partial_pool = published
        lake = MarketLake(engine=engine, root=data_dir / "market")
        status_lake = ExecutionStatusLake(engine)
        status_inputs: dict[Period, RoutedBarSuccess] = {}
        for period in Period:
            for adjustment in Adjustment:
                routed = _routed(period, adjustment)
                lake.write(routed)
                if adjustment is Adjustment.QFQ:
                    status_inputs[period] = routed
        for period, routed in status_inputs.items():
            query = routed.result.query
            local_end = query.end.astimezone(SHANGHAI)
            status_end = local_end.date()
            if local_end.timetz().replace(tzinfo=None) != time():
                status_end += timedelta(days=1)
            status_lake.write(
                _execution_status(
                    symbol=query.symbol,
                    period=period,
                    start=query.start.astimezone(SHANGHAI).date(),
                    end=status_end,
                    timestamps=tuple(item.timestamp for item in routed.result.bars),
                )
            )
        formulas = FormulaRepository(engine)
        macd = formulas.create(
            "E2E MACD 金叉死叉",
            "trading",
            MACD_SOURCE,
            {},
            placement="subchart",
        )
        formulas.create(
            "E2E 自定义波段",
            "trading",
            CUSTOM_SOURCE,
            {},
            placement="subchart",
        )
        if partial_pool is None:
            raise RuntimeError("E2E partial pool was not published")
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
                scoring_start=_local(date(2024, 2, 10)),
                scoring_end=_local(date(2024, 3, 15)),
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
        source_settings = SourceSettingsServices(engine=engine, settings=settings)
        try:
            source_settings.save_public(
                PublicSourceSettings(
                    priorities=SourcePriorities.model_validate(
                        {
                            "daily_bars": ["tdx_local"],
                            "weekly_bars": ["baostock"],
                            "minute_bars": ["baostock"],
                            "instruments": ["akshare"],
                            "trading_calendar": ["baostock"],
                        }
                    ),
                    tdx_path=str(tdx_root),
                )
            )
        finally:
            source_settings.close()
        analysis_fixture = load_demo_fixture()
        model_catalog = AnalysisModelCatalog(
            engine,
            expected_database_identity=tasks.database_identity,
            owns_engine=False,
            clock=analysis_fixture.clock,
        )
        model = model_catalog.create(
            display_name="Deterministic demo model",
            public_config=AnalysisModelPublicConfig(
                provider=ModelProviderKind.OLLAMA,
                base_url="http://127.0.0.1:11434",
                model="stock-desk-demo-stub",
                temperature=0.0,
                timeout_seconds=30.0,
                max_output_tokens=2048,
                secret_reference_id=None,
                api_key_configured=False,
            ),
        )
        model_catalog.mark_test_result(
            model.id,
            expected_status=ModelConfigStatus.UNVERIFIED,
            expected_revision=model.revision,
            succeeded=True,
        )
        model_catalog.close()
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
    _seed(data_dir)
    os.environ["STOCK_DESK_DATA_DIR"] = str(data_dir)
    os.environ["STOCK_DESK_DATABASE_URL"] = f"sqlite:///{data_dir / 'stock-desk.db'}"
    received_signal: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
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
    try:
        return supervise(commands, requested_signal=lambda: received_signal)
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
