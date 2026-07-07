"""Production worker composition for market catalog, updates, and schedules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import os
from pathlib import Path
import socket
from threading import Event, Lock
from typing import Any

from sqlalchemy import Engine

from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.api.settings import SourceSettingsServices
from stock_desk.config import Settings
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.market.compositions import (
    AkShareCompositionProvider,
    CompositionProvider,
)
from stock_desk.market.instruments import InstrumentNotFound, InstrumentRepository
from stock_desk.market.lake import MarketLake
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.pools import PoolCategory, PoolRepository, PoolRepositoryError
from stock_desk.market.provenance import (
    RoutedInstrumentFailure,
    RoutedInstrumentSuccess,
)
from stock_desk.market.runtime import (
    DefaultRuntimeProviderFactory,
    MarketProviderRuntime,
    RuntimeProviderFactory,
)
from stock_desk.market.scheduler import (
    MarketUpdateScheduleRepository,
    MarketUpdateScheduler,
)
from stock_desk.market.update import (
    MARKET_CATALOG_UPDATE_TASK_KIND,
    MARKET_UPDATE_TASK_KIND,
    UpdateService,
)
from stock_desk.security.redaction import scoped_log_redaction
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import ClaimedTaskHandler, TaskWorker, demo_double


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SettingsBackedMarketUpdateHandler:
    """Freeze one source-policy snapshot for the complete multi-symbol task."""

    def __init__(
        self,
        *,
        source_settings: SourceSettingsServices,
        lake: MarketLake,
        tasks: TaskRepository,
        engine: Engine,
        provider_factory: RuntimeProviderFactory,
        execution_status_lake: ExecutionStatusLake | None = None,
    ) -> None:
        identities = (
            source_settings.database_identity,
            lake.database_identity,
            tasks.database_identity,
        )
        if identities[1:] != identities[:-1]:
            raise ValueError("market worker database identities do not match")
        self._source_settings = source_settings
        self._lake = lake
        self._tasks = tasks
        self._engine = engine
        self._provider_factory = provider_factory
        self._execution_status_lake = execution_status_lake

    def __call__(self, task: TaskSnapshot) -> Mapping[str, Any]:
        snapshot = self._source_settings.runtime_snapshot()
        with scoped_log_redaction(*snapshot.redaction_values()):
            runtime = MarketProviderRuntime.build(
                snapshot,
                factory=self._provider_factory,
            )
            try:
                result = dict(
                    UpdateService(
                        router=runtime.router,
                        lake=self._lake,
                        tasks=self._tasks,
                        engine=self._engine,
                        execution_status_lake=self._execution_status_lake,
                    ).handle(task)
                )
                result["configuration_fingerprint"] = snapshot.configuration_fingerprint
                return result
            finally:
                runtime.close()


class SettingsBackedCatalogUpdateHandler:
    """Refresh instruments, then publish independently recoverable preset snapshots."""

    def __init__(
        self,
        *,
        source_settings: SourceSettingsServices,
        instruments: InstrumentRepository,
        pools: PoolRepository,
        provider_factory: RuntimeProviderFactory,
        composition_factory: Callable[[], CompositionProvider] | None = None,
    ) -> None:
        identities = (
            source_settings.database_identity,
            instruments.database_identity,
            pools.database_identity,
        )
        if identities[1:] != identities[:-1]:
            raise ValueError("market catalog database identities do not match")
        self._source_settings = source_settings
        self._instruments = instruments
        self._pools = pools
        self._provider_factory = provider_factory
        self._composition_factory = composition_factory or (
            lambda: AkShareCompositionProvider.from_sdk(clock=_utc_now)
        )

    def __call__(self, task: TaskSnapshot) -> Mapping[str, Any]:
        if task.kind != MARKET_CATALOG_UPDATE_TASK_KIND or task.payload:
            raise ValueError("market catalog handler received an invalid task")
        snapshot = self._source_settings.runtime_snapshot()
        with scoped_log_redaction(*snapshot.redaction_values()):
            runtime = MarketProviderRuntime.build(
                snapshot,
                factory=self._provider_factory,
            )
            try:
                try:
                    previous = self._instruments.current_manifest().manifest
                except InstrumentNotFound:
                    previous = None
                routed = runtime.router.fetch_instruments(previous_manifest=previous)
                if isinstance(routed, RoutedInstrumentFailure):
                    raise RuntimeError("catalog routing failed")
                if not isinstance(routed, RoutedInstrumentSuccess):
                    raise TypeError("market router returned an invalid catalog outcome")
                manifest = self._instruments.ingest(routed)
                preset_successes: list[dict[str, str]] = []
                preset_failures: list[dict[str, str]] = []
                full_a_pool_id: str | None = None
                try:
                    full_a = self._pools.publish_full_a()
                    full_a_pool_id = full_a.pool_id
                    preset_successes.append(
                        {"preset_key": "all-a", "category": "all_a"}
                    )
                except PoolRepositoryError:
                    preset_failures.append(
                        {
                            "preset_key": "all-a",
                            "category": "all_a",
                            "reason": "persistence_failure",
                        }
                    )
                try:
                    catalog = self._instruments.pinned_catalog(
                        manifest.manifest_record_id
                    )
                    composition_result = self._composition_factory().fetch_presets(
                        frozenset(item.symbol for item in catalog.instruments)
                    )
                except Exception:
                    composition_result = None
                    preset_failures.extend(
                        {
                            "preset_key": preset_key,
                            "category": category.value,
                            "reason": "provider_unavailable",
                        }
                        for preset_key, category in (
                            ("index-catalog", PoolCategory.INDEX),
                            ("industry-catalog", PoolCategory.INDUSTRY),
                        )
                    )
                if composition_result is not None:
                    preset_failures.extend(
                        {
                            "preset_key": failure.preset_key,
                            "category": failure.category.value,
                            "reason": failure.reason.value,
                        }
                        for failure in composition_result.failures
                    )
                    for composition in composition_result.compositions:
                        try:
                            self._pools.publish_preset(composition)
                            preset_successes.append(
                                {
                                    "preset_key": composition.preset_key,
                                    "category": composition.category.value,
                                }
                            )
                        except PoolRepositoryError:
                            preset_failures.append(
                                {
                                    "preset_key": composition.preset_key,
                                    "category": composition.category.value,
                                    "reason": "persistence_failure",
                                }
                            )
                return {
                    "source": manifest.source.value,
                    "row_count": manifest.row_count,
                    "manifest_record_id": manifest.manifest_record_id,
                    "full_a_pool_id": full_a_pool_id,
                    "preset_successes": preset_successes,
                    "preset_failures": preset_failures,
                    "configuration_fingerprint": snapshot.configuration_fingerprint,
                }
            finally:
                runtime.close()


class ProductionMarketWorker:
    def __init__(
        self,
        *,
        engine: Engine,
        tasks: TaskRepository,
        source_settings: SourceSettingsServices,
        worker: TaskWorker,
        scheduler: MarketUpdateScheduler,
    ) -> None:
        self._engine = engine
        self.tasks = tasks
        self.source_settings = source_settings
        self.worker = worker
        self.scheduler = scheduler
        self._close_lock = Lock()
        self._closed = False

    @classmethod
    def open(
        cls,
        settings: Settings,
        *,
        worker_id: str | None = None,
        provider_factory: RuntimeProviderFactory | None = None,
        composition_factory: Callable[[], CompositionProvider] | None = None,
        analysis_handler: ClaimedTaskHandler | None = None,
    ) -> ProductionMarketWorker:
        migrate(settings.database_url)
        engine = create_engine_for_url(settings.database_url)
        source_settings: SourceSettingsServices | None = None
        try:
            tasks = TaskRepository(engine)
            source_settings = SourceSettingsServices(engine=engine, settings=settings)
            data_dir = Path(os.path.abspath(os.fspath(settings.data_dir.expanduser())))
            lake = MarketLake(engine=engine, root=data_dir / "market")
            execution_status_lake = ExecutionStatusLake(engine)
            instruments = InstrumentRepository(engine)
            pools = PoolRepository(engine)
            schedules = MarketUpdateScheduleRepository(engine)
            task_worker = TaskWorker(
                tasks,
                worker_id=worker_id or f"{socket.gethostname()}-{os.getpid()}",
            )
            resolved_factory = provider_factory or DefaultRuntimeProviderFactory()
            task_worker.register("demo.double", demo_double)
            task_worker.register(
                MARKET_UPDATE_TASK_KIND,
                SettingsBackedMarketUpdateHandler(
                    source_settings=source_settings,
                    lake=lake,
                    tasks=tasks,
                    engine=engine,
                    provider_factory=resolved_factory,
                    execution_status_lake=execution_status_lake,
                ),
            )
            task_worker.register(
                MARKET_CATALOG_UPDATE_TASK_KIND,
                SettingsBackedCatalogUpdateHandler(
                    source_settings=source_settings,
                    instruments=instruments,
                    pools=pools,
                    provider_factory=resolved_factory,
                    composition_factory=composition_factory,
                ),
            )
            formula_service = FormulaService(
                repository=FormulaRepository(engine),
                lake=lake,
            )
            backtests = BacktestRepository(engine)
            task_worker.register_claimed(
                "backtest.run",
                PoolBacktestRunner(
                    engine=engine,
                    tasks=tasks,
                    repository=backtests,
                    market_lake=lake,
                    status_lake=execution_status_lake,
                    formulas=formula_service,
                ),
            )
            if analysis_handler is not None:
                task_worker.register_claimed("analysis.run", analysis_handler)
            scheduler = MarketUpdateScheduler(schedules, tasks, clock=_utc_now)
            return cls(
                engine=engine,
                tasks=tasks,
                source_settings=source_settings,
                worker=task_worker,
                scheduler=scheduler,
            )
        except BaseException:
            if source_settings is not None:
                source_settings.close()
            engine.dispose()
            raise

    def run_once(self) -> TaskSnapshot | None:
        self.scheduler.tick()
        return self.worker.run_once()

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            completed = self.run_once()
            if completed is None:
                stop_event.wait(1.0)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.source_settings.close()
        self._engine.dispose()


__all__ = [
    "MARKET_CATALOG_UPDATE_TASK_KIND",
    "ProductionMarketWorker",
    "SettingsBackedCatalogUpdateHandler",
    "SettingsBackedMarketUpdateHandler",
]
