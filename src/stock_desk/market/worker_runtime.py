"""Production worker composition for market catalog, updates, and schedules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timezone
import os
from pathlib import Path
from threading import Event, Lock
from time import monotonic as _monotonic
from typing import Any

from sqlalchemy import Engine

from stock_desk.analysis.model_catalog import AnalysisModelCatalog
from stock_desk.analysis.data_service import ResearchDataService
from stock_desk.analysis.model_settings import (
    ModelProviderFactory,
    ModelSettingsService,
)
from stock_desk.analysis.providers.base import ModelProvider
from stock_desk.analysis.repository import AnalysisExecutionConfig, AnalysisRepository
from stock_desk.analysis.runtime import (
    ResearchDataServiceFactory,
    production_evidence_factory,
)
from stock_desk.analysis.worker import AnalysisWorkerHandler
from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.repository import BacktestRepository
from stock_desk.api.settings import SourceSettingsServices
from stock_desk.config import Settings
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService, IsolatedFormulaExecutor
from stock_desk.diagnostics.models import DiagnosticEventSink
from stock_desk.runtime_identity import new_worker_id
from stock_desk.market.compositions import (
    AkShareCompositionProvider,
    CompositionProvider,
)
from stock_desk.market.instruments import InstrumentNotFound, InstrumentRepository
from stock_desk.market.lake import MarketLake, create_market_lake
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
from stock_desk.security.secrets import SecretConfigurationError, SecretStore
from stock_desk.storage.backup import recover_interrupted_restore
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.lifecycle import (
    SERVICE_STARTUP_LOCK_TIMEOUT_SECONDS,
    service_lifecycle,
)
from stock_desk.tasks.models import TaskSnapshot
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import ClaimedTaskHandler, TaskWorker, demo_double


_IDLE_TASK_POLL_SECONDS = 0.1
_SCHEDULE_POLL_SECONDS = 1.0
_BACKTEST_FORMULA_TIMEOUT_SECONDS = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _best_effort_cleanup(
    actions: tuple[Callable[[], None], ...],
    *,
    raise_first: bool,
) -> None:
    first_error: BaseException | None = None
    for action in actions:
        try:
            action()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if raise_first and first_error is not None:
        raise first_error


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
        tasks: TaskRepository,
        provider_factory: RuntimeProviderFactory,
        composition_factory: Callable[[], CompositionProvider] | None = None,
    ) -> None:
        identities = (
            source_settings.database_identity,
            instruments.database_identity,
            pools.database_identity,
            tasks.database_identity,
        )
        if identities[1:] != identities[:-1]:
            raise ValueError("market catalog database identities do not match")
        self._source_settings = source_settings
        self._instruments = instruments
        self._pools = pools
        self._tasks = tasks
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
                self._tasks.pause_at_desktop_checkpoint(task.id)
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
                self._tasks.pause_at_desktop_checkpoint(task.id)
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
                self._tasks.pause_at_desktop_checkpoint(task.id)
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
                        self._tasks.pause_at_desktop_checkpoint(task.id)
                self._tasks.pause_at_desktop_checkpoint(task.id)
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
        analysis_repository: AnalysisRepository,
        model_catalog: AnalysisModelCatalog,
        lifecycle_guard: AbstractContextManager[None],
        model_provider_factory: ModelProviderFactory | None = None,
        model_settings_service: ModelSettingsService | None = None,
    ) -> None:
        self._engine = engine
        self.tasks = tasks
        self.source_settings = source_settings
        self.worker = worker
        self.scheduler = scheduler
        self.analysis_repository = analysis_repository
        self.model_catalog = model_catalog
        self._lifecycle_guard = lifecycle_guard
        self._model_provider_factory = model_provider_factory
        self._model_settings_service = model_settings_service
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
        analysis_provider_factory: Callable[[AnalysisExecutionConfig], ModelProvider]
        | None = None,
        analysis_data_service_factory: Callable[[], ResearchDataService] | None = None,
        diagnostic_event_sink: DiagnosticEventSink | None = None,
    ) -> ProductionMarketWorker:
        data_dir = Path(os.path.abspath(os.fspath(settings.data_dir.expanduser())))
        lifecycle_guard = service_lifecycle(
            data_dir,
            role="worker",
            timeout_seconds=SERVICE_STARTUP_LOCK_TIMEOUT_SECONDS,
            preflight=lambda: recover_interrupted_restore(
                data_dir=data_dir,
                _lifecycle_held=True,
            ),
        )
        lifecycle_guard.__enter__()
        try:
            migrate(settings.database_url)
            engine = create_engine_for_url(settings.database_url)
        except BaseException as error:
            lifecycle_guard.__exit__(type(error), error, error.__traceback__)
            raise
        source_settings: SourceSettingsServices | None = None
        model_catalog: AnalysisModelCatalog | None = None
        model_provider_factory: ModelProviderFactory | None = None
        model_settings_service: ModelSettingsService | None = None
        try:
            tasks = TaskRepository(engine)
            source_settings = SourceSettingsServices(engine=engine, settings=settings)
            lake: MarketLake = create_market_lake(
                engine=engine,
                root=data_dir / "market",
            )
            execution_status_lake = ExecutionStatusLake(engine)
            instruments = InstrumentRepository(engine)
            pools = PoolRepository(engine)
            schedules = MarketUpdateScheduleRepository(engine)
            task_worker = TaskWorker(
                tasks,
                worker_id=worker_id or new_worker_id("market"),
                diagnostic_event_sink=diagnostic_event_sink,
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
                    tasks=tasks,
                    provider_factory=resolved_factory,
                    composition_factory=composition_factory,
                ),
            )
            formula_service = FormulaService(
                repository=FormulaRepository(engine),
                lake=lake,
                executor=IsolatedFormulaExecutor(
                    timeout_seconds=_BACKTEST_FORMULA_TIMEOUT_SECONDS
                ),
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
            analysis_repository = AnalysisRepository(engine, tasks=tasks)
            model_catalog = AnalysisModelCatalog(
                engine,
                expected_database_identity=tasks.database_identity,
                owns_engine=False,
            )
            identities = (
                tasks.database_identity,
                source_settings.database_identity,
                lake.database_identity,
                analysis_repository.database_identity,
                model_catalog.database_identity,
            )
            if any(identity != identities[0] for identity in identities[1:]):
                raise ValueError("analysis worker database identities do not match")
            try:
                model_secrets: SecretStore | None = SecretStore(
                    engine,
                    settings,
                    expected_database_identity=tasks.database_identity,
                )
            except SecretConfigurationError:
                model_secrets = None
            model_providers = ModelProviderFactory(secret_store=model_secrets)
            model_provider_factory = model_providers
            model_settings_service = ModelSettingsService(
                catalog=model_catalog,
                secret_store=model_secrets,
                provider_factory=model_providers,
            )
            resolved_analysis_handler = analysis_handler
            if resolved_analysis_handler is None:
                resolved_analysis_provider_factory = (
                    analysis_provider_factory
                    if analysis_provider_factory is not None
                    else lambda execution: model_providers.create(
                        execution.public_config
                    )
                )
                resolved_analysis_handler = AnalysisWorkerHandler(
                    repository=analysis_repository,
                    provider_factory=resolved_analysis_provider_factory,
                    data_service_factory=(
                        analysis_data_service_factory
                        if analysis_data_service_factory is not None
                        else ResearchDataServiceFactory(
                            source_settings=source_settings,
                            market_lake=lake,
                            clock=_utc_now,
                        )
                    ),
                    evidence_factory=production_evidence_factory,
                )
            task_worker.register_claimed("analysis.run", resolved_analysis_handler)
            scheduler = MarketUpdateScheduler(schedules, tasks, clock=_utc_now)
            return cls(
                engine=engine,
                tasks=tasks,
                source_settings=source_settings,
                worker=task_worker,
                scheduler=scheduler,
                analysis_repository=analysis_repository,
                model_catalog=model_catalog,
                lifecycle_guard=lifecycle_guard,
                model_provider_factory=model_provider_factory,
                model_settings_service=model_settings_service,
            )
        except BaseException as error:
            _best_effort_cleanup(
                tuple(
                    action
                    for action in (
                        source_settings.close if source_settings is not None else None,
                        model_catalog.close if model_catalog is not None else None,
                        model_settings_service.close
                        if model_settings_service is not None
                        else model_provider_factory.close
                        if model_provider_factory is not None
                        else None,
                        engine.dispose,
                    )
                    if action is not None
                ),
                raise_first=False,
            )
            lifecycle_guard.__exit__(type(error), error, error.__traceback__)
            raise

    def run_once(self) -> TaskSnapshot | None:
        self.scheduler.tick()
        return self.worker.run_once()

    def run_forever(
        self,
        stop_event: Event,
        *,
        ready_event: Any | None = None,
        claim_stop_event: Any | None = None,
    ) -> None:
        next_schedule_poll = 0.0
        claims_stopped = (
            claim_stop_event if claim_stop_event is not None else stop_event
        )
        with self.worker.heartbeat_lifecycle(stop_event) as heartbeat:
            if ready_event is not None:
                ready_event.set()
            while not stop_event.is_set():
                heartbeat.raise_if_failed()
                if claims_stopped.is_set():
                    stop_event.wait(_IDLE_TASK_POLL_SECONDS)
                    continue
                now = _monotonic()
                if now >= next_schedule_poll:
                    self.scheduler.tick()
                    next_schedule_poll = now + _SCHEDULE_POLL_SECONDS
                completed = self.worker.run_once(stop_event=claims_stopped)
                if completed is None:
                    stop_event.wait(_IDLE_TASK_POLL_SECONDS)
            heartbeat.raise_if_failed()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        def close_lifecycle() -> None:
            self._lifecycle_guard.__exit__(None, None, None)

        _best_effort_cleanup(
            tuple(
                action
                for action in (
                    self.source_settings.close,
                    self.model_catalog.close,
                    self._model_settings_service.close
                    if self._model_settings_service is not None
                    else self._model_provider_factory.close
                    if self._model_provider_factory is not None
                    else None,
                    self._engine.dispose,
                    close_lifecycle,
                )
                if action is not None
            ),
            raise_first=True,
        )


__all__ = [
    "MARKET_CATALOG_UPDATE_TASK_KIND",
    "ProductionMarketWorker",
    "SettingsBackedCatalogUpdateHandler",
    "SettingsBackedMarketUpdateHandler",
]
