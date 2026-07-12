from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager
import os
from pathlib import Path
import secrets
from threading import Lock

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from stock_desk.analysis.model_catalog import AnalysisModelCatalog
from stock_desk.analysis.model_settings import (
    ModelProviderFactory,
    ModelSettingsSecureStorageError,
    ModelSettingsService,
    ModelSettingsStorageError,
)
from stock_desk.analysis.repository import AnalysisRepository
from stock_desk.analysis.runtime import (
    AnalysisPreflightService,
    ResearchDataServiceFactory,
)
from stock_desk.analysis.service import AnalysisService
from stock_desk.api.analysis import (
    AnalysisDatabaseMismatch,
    router as analysis_router,
)

from stock_desk.api.backtests import (
    BacktestServiceDatabaseMismatch,
    BacktestServices,
    backtest_service_database_mismatch_handler,
    backtest_request_validation_handler,
    router as backtests_router,
)

from stock_desk.api.formulas import (
    formula_service_database_mismatch_handler,
    router as formulas_router,
)
from stock_desk.api.health import router as health_router
from stock_desk.api.guidance import router as guidance_router
from stock_desk.api.market import (
    MarketServices,
    market_request_validation_handler,
    router as market_router,
)
from stock_desk.api.market_navigation import router as market_navigation_router
from stock_desk.api.models import (
    ModelSettingsDatabaseMismatch,
    router as models_router,
)
from stock_desk.api.onboarding import router as onboarding_router
from stock_desk.api.settings import (
    SourceSettingsServices,
    SourceSettingsStorageError,
    router as settings_router,
    source_settings_storage_exception_handler,
)
from stock_desk.api.tasks import router as tasks_router
from stock_desk.api.workspace import router as workspace_router
from stock_desk.config import Settings, get_settings
from stock_desk.desktop_session import (
    DesktopHandshake,
    DesktopLifecycleController,
    DesktopSession,
    DesktopSessionMiddleware,
)
from stock_desk.formula.repository import FormulaRepository
from stock_desk.guidance.store import GuidancePreferencesStore
from stock_desk.formula.service import FormulaService, FormulaServiceDatabaseMismatch
from stock_desk.security.secrets import (
    SecretConfigurationError,
    SecretStore,
    SecretStoreError,
)
from stock_desk.security.persistence import StartupSecretHydrator
from stock_desk.onboarding.service import OnboardingService
from stock_desk.market.navigation import MarketNavigationService
from stock_desk.storage.backup import recover_interrupted_restore
from stock_desk.storage.lifecycle import service_lifecycle
from stock_desk.tasks.repository import TaskRepository, TaskRepositoryError
from stock_desk.web import install_web_routes
from stock_desk.workspace.service import WorkspaceService


class _ApplicationDatabaseMismatch(RuntimeError):
    pass


class _ApplicationDatabaseIdentity:
    """Lazily bind one immutable database identity for this app instance."""

    def __init__(self, explicit_dependencies: tuple[object, ...]) -> None:
        self._lock = Lock()
        identities: list[object] = []
        compromised = False
        for dependency in explicit_dependencies:
            try:
                identity = getattr(dependency, "database_identity", None)
            except Exception:
                compromised = True
                continue
            if identity is None:
                compromised = True
                continue
            identities.append(identity)
        self._identity: object | None = identities[0] if identities else None
        try:
            compromised = compromised or any(
                identity != self._identity for identity in identities[1:]
            )
        except Exception:
            compromised = True
        self._compromised = compromised

    def bind(self, identity: object) -> object:
        if identity is None:
            raise _ApplicationDatabaseMismatch()
        with self._lock:
            if self._compromised:
                raise _ApplicationDatabaseMismatch()
            if self._identity is None:
                self._identity = identity
            elif identity != self._identity:
                self._compromised = True
                raise _ApplicationDatabaseMismatch()
            return self._identity

    def current(self) -> object | None:
        with self._lock:
            if self._compromised:
                raise _ApplicationDatabaseMismatch()
            return self._identity


def create_app(
    settings: Settings | None = None,
    task_repository: TaskRepository | None = None,
    market_services: MarketServices | None = None,
    source_settings_services: SourceSettingsServices | None = None,
    formula_service: FormulaService | None = None,
    backtest_services: BacktestServices | None = None,
    model_settings_service: ModelSettingsService | None = None,
    analysis_service: AnalysisService | None = None,
    analysis_preflight_service: AnalysisPreflightService | None = None,
    desktop_session: DesktopSession | None = None,
    desktop_lifecycle: DesktopLifecycleController | None = None,
    onboarding_service: OnboardingService | None = None,
    workspace_service: WorkspaceService | None = None,
    market_navigation_service: MarketNavigationService | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    database_identity = _ApplicationDatabaseIdentity(
        tuple(
            dependency
            for dependency in (
                task_repository,
                market_services,
                source_settings_services,
                formula_service,
                backtest_services,
                model_settings_service,
                analysis_service,
                analysis_preflight_service,
            )
            if dependency is not None
        )
    )
    owned_repository: TaskRepository | None = None
    repository_lock = Lock()
    owned_market_services: MarketServices | None = None
    market_services_lock = Lock()
    owned_source_settings_services: SourceSettingsServices | None = None
    source_settings_services_lock = Lock()
    owned_formula_service: FormulaService | None = None
    formula_service_lock = Lock()
    owned_backtest_services: BacktestServices | None = None
    backtest_services_lock = Lock()
    owned_model_catalog: AnalysisModelCatalog | None = None
    model_catalog_lock = Lock()
    owned_secret_store: SecretStore | None = None
    secret_store_lock = Lock()
    secret_store_initialized = False
    secret_store_error = False
    owned_model_settings_service: ModelSettingsService | None = None
    model_settings_service_lock = Lock()
    owned_analysis_service: AnalysisService | None = None
    analysis_service_lock = Lock()
    owned_analysis_preflight: AnalysisPreflightService | None = None
    analysis_preflight_lock = Lock()
    owned_startup_secret_hydrator: StartupSecretHydrator | None = None
    owned_onboarding_service: OnboardingService | None = None
    onboarding_service_lock = Lock()
    owned_workspace_service: WorkspaceService | None = None
    workspace_service_lock = Lock()
    owned_market_navigation_service: MarketNavigationService | None = None
    market_navigation_service_lock = Lock()
    owned_guidance_preferences_store: GuidancePreferencesStore | None = None
    guidance_preferences_store_lock = Lock()
    shutdown_lock = Lock()
    active_lifespans = 0
    service_guard: AbstractContextManager[None] | None = None

    def bind_dependency(dependency: object) -> None:
        database_identity.bind(getattr(dependency, "database_identity", None))

    def provide_task_repository() -> TaskRepository:
        nonlocal owned_repository
        if task_repository is not None:
            try:
                bind_dependency(task_repository)
            except _ApplicationDatabaseMismatch:
                raise TaskRepositoryError(
                    "Task storage does not match the application"
                ) from None
            return task_repository
        with repository_lock:
            if owned_repository is None:
                owned_repository = TaskRepository.open(resolved_settings.database_url)
            try:
                bind_dependency(owned_repository)
            except _ApplicationDatabaseMismatch:
                raise TaskRepositoryError(
                    "Task storage does not match the application"
                ) from None
            return owned_repository

    def provide_market_services() -> MarketServices:
        nonlocal owned_market_services
        if market_services is not None:
            bind_dependency(market_services)
            return market_services
        with market_services_lock:
            if owned_market_services is None:
                data_dir = Path(
                    os.path.abspath(os.fspath(resolved_settings.data_dir.expanduser()))
                )
                owned_market_services = MarketServices.open(
                    database_url=resolved_settings.database_url,
                    lake_root=data_dir / "market",
                )
            bind_dependency(owned_market_services)
            return owned_market_services

    def provide_source_settings_services() -> SourceSettingsServices:
        nonlocal owned_source_settings_services
        if source_settings_services is not None:
            bind_dependency(source_settings_services)
            return source_settings_services
        with source_settings_services_lock:
            if owned_source_settings_services is None:
                owned_source_settings_services = SourceSettingsServices.open(
                    database_url=resolved_settings.database_url,
                    settings=resolved_settings,
                )
            bind_dependency(owned_source_settings_services)
            return owned_source_settings_services

    def provide_formula_service() -> FormulaService:
        nonlocal owned_formula_service
        if formula_service is not None:
            try:
                services = provide_market_services()
            except _ApplicationDatabaseMismatch:
                raise FormulaServiceDatabaseMismatch(
                    "formula and market storage do not match"
                ) from None
            if formula_service.database_identity != services.database_identity:
                raise FormulaServiceDatabaseMismatch(
                    "formula and market storage do not match"
                )
            return formula_service
        with formula_service_lock:
            if owned_formula_service is None:
                services = provide_market_services()
                owned_formula_service = FormulaService(
                    repository=FormulaRepository(services.engine),
                    lake=services.lake,
                )
            bind_dependency(owned_formula_service)
            return owned_formula_service

    def provide_backtest_services() -> BacktestServices:
        nonlocal owned_backtest_services
        if backtest_services is not None:
            if isinstance(backtest_services, BacktestServices):
                try:
                    identities = (
                        provide_market_services().database_identity,
                        provide_formula_service().database_identity,
                        provide_task_repository().database_identity,
                    )
                except (
                    _ApplicationDatabaseMismatch,
                    FormulaServiceDatabaseMismatch,
                    TaskRepositoryError,
                ):
                    raise BacktestServiceDatabaseMismatch(
                        "backtest dependencies do not share storage"
                    ) from None
                if any(
                    identity != backtest_services.database_identity
                    for identity in identities
                ):
                    raise BacktestServiceDatabaseMismatch(
                        "backtest and application storage do not match"
                    )
            return backtest_services
        with backtest_services_lock:
            if owned_backtest_services is None:
                try:
                    shared_market = provide_market_services()
                    shared_formula = provide_formula_service()
                    shared_tasks = provide_task_repository()
                except (
                    _ApplicationDatabaseMismatch,
                    FormulaServiceDatabaseMismatch,
                    TaskRepositoryError,
                ):
                    raise BacktestServiceDatabaseMismatch(
                        "backtest dependencies do not share storage"
                    ) from None
                if not (
                    shared_market.database_identity
                    == shared_formula.database_identity
                    == shared_tasks.database_identity
                ):
                    raise BacktestServiceDatabaseMismatch(
                        "backtest dependencies do not share storage"
                    )
                owned_backtest_services = BacktestServices.from_shared(
                    market_services=shared_market,
                    formula_service=shared_formula,
                    tasks=shared_tasks,
                )
            bind_dependency(owned_backtest_services)
            return owned_backtest_services

    def provide_model_catalog() -> AnalysisModelCatalog:
        nonlocal owned_model_catalog
        with model_catalog_lock:
            if owned_model_catalog is None:
                tasks = provide_task_repository()
                owned_model_catalog = AnalysisModelCatalog(
                    tasks.engine,
                    expected_database_identity=tasks.database_identity,
                    owns_engine=False,
                )
            bind_dependency(owned_model_catalog)
            return owned_model_catalog

    def provide_secret_store() -> SecretStore | None:
        nonlocal owned_secret_store
        nonlocal secret_store_error
        nonlocal secret_store_initialized
        with secret_store_lock:
            if secret_store_error:
                raise ModelSettingsSecureStorageError()
            if secret_store_initialized:
                return owned_secret_store
            configured = resolved_settings.master_key
            if configured is None or not configured.get_secret_value():
                secret_store_initialized = True
                return None
            catalog = provide_model_catalog()
            try:
                candidate = SecretStore(
                    catalog.engine,
                    resolved_settings,
                    expected_database_identity=catalog.database_identity,
                )
            except SecretConfigurationError:
                secret_store_error = True
                raise ModelSettingsSecureStorageError() from None
            except SecretStoreError:
                raise ModelSettingsStorageError() from None
            bind_dependency(candidate)
            owned_secret_store = candidate
            secret_store_initialized = True
            return owned_secret_store

    def provide_model_settings_services() -> ModelSettingsService:
        nonlocal owned_model_settings_service
        if model_settings_service is not None:
            try:
                bind_dependency(model_settings_service)
            except _ApplicationDatabaseMismatch:
                raise ModelSettingsDatabaseMismatch() from None
            return model_settings_service
        with model_settings_service_lock:
            if owned_model_settings_service is None:
                catalog = provide_model_catalog()
                secret_store = provide_secret_store()
                owned_model_settings_service = ModelSettingsService(
                    catalog=catalog,
                    secret_store=secret_store,
                    provider_factory=ModelProviderFactory(secret_store=secret_store),
                )
            try:
                bind_dependency(owned_model_settings_service)
            except _ApplicationDatabaseMismatch:
                raise ModelSettingsDatabaseMismatch() from None
            return owned_model_settings_service

    def provide_analysis_services() -> AnalysisService:
        nonlocal owned_analysis_service
        if analysis_service is not None:
            try:
                bind_dependency(analysis_service)
            except _ApplicationDatabaseMismatch:
                raise AnalysisDatabaseMismatch() from None
            return analysis_service
        with analysis_service_lock:
            if owned_analysis_service is None:
                tasks = provide_task_repository()
                catalog = provide_model_catalog()
                model_settings = provide_model_settings_services()
                repository = AnalysisRepository(tasks.engine)
                owned_analysis_service = AnalysisService(
                    repository=repository,
                    tasks=tasks,
                    model_catalog=catalog,
                    execution_resolver=(
                        model_settings.require_verified_execution_in_transaction
                    ),
                )
            try:
                bind_dependency(owned_analysis_service)
            except _ApplicationDatabaseMismatch:
                raise AnalysisDatabaseMismatch() from None
            return owned_analysis_service

    def provide_analysis_preflight() -> AnalysisPreflightService:
        nonlocal owned_analysis_preflight
        if analysis_preflight_service is not None:
            try:
                bind_dependency(analysis_preflight_service)
            except _ApplicationDatabaseMismatch:
                raise AnalysisDatabaseMismatch() from None
            return analysis_preflight_service
        with analysis_preflight_lock:
            if owned_analysis_preflight is None:
                market = provide_market_services()
                source_settings = provide_source_settings_services()
                factory = ResearchDataServiceFactory(
                    source_settings=source_settings,
                    market_lake=market.lake,
                )
                owned_analysis_preflight = AnalysisPreflightService(
                    data_service_factory=factory
                )
            try:
                bind_dependency(owned_analysis_preflight)
            except _ApplicationDatabaseMismatch:
                raise AnalysisDatabaseMismatch() from None
            return owned_analysis_preflight

    def provide_onboarding_service() -> OnboardingService:
        nonlocal owned_onboarding_service
        if onboarding_service is not None:
            return onboarding_service
        with onboarding_service_lock:
            if owned_onboarding_service is None:
                data_dir = Path(
                    os.path.abspath(os.fspath(resolved_settings.data_dir.expanduser()))
                )
                owned_onboarding_service = OnboardingService.open(
                    data_dir=data_dir,
                    market=provide_market_services,
                )
            return owned_onboarding_service

    def provide_workspace_service() -> WorkspaceService:
        nonlocal owned_workspace_service
        if workspace_service is not None:
            return workspace_service
        with workspace_service_lock:
            if owned_workspace_service is None:
                data_dir = Path(
                    os.path.abspath(os.fspath(resolved_settings.data_dir.expanduser()))
                )
                owned_workspace_service = WorkspaceService.open(
                    data_dir=data_dir,
                    market=provide_market_services,
                    formula_repository=lambda: FormulaRepository(
                        provide_market_services().engine
                    ),
                )
            return owned_workspace_service

    def provide_market_navigation_service() -> MarketNavigationService:
        nonlocal owned_market_navigation_service
        if market_navigation_service is not None:
            return market_navigation_service
        with market_navigation_service_lock:
            if owned_market_navigation_service is None:
                data_dir = Path(
                    os.path.abspath(os.fspath(resolved_settings.data_dir.expanduser()))
                )
                owned_market_navigation_service = MarketNavigationService.open(
                    data_dir=data_dir,
                    instruments=provide_market_services().instruments,
                )
            return owned_market_navigation_service

    def provide_guidance_preferences_store() -> GuidancePreferencesStore:
        nonlocal owned_guidance_preferences_store
        with guidance_preferences_store_lock:
            if owned_guidance_preferences_store is None:
                data_dir = Path(
                    os.path.abspath(os.fspath(resolved_settings.data_dir.expanduser()))
                )
                owned_guidance_preferences_store = GuidancePreferencesStore(
                    data_dir / "guidance" / "preferences.json"
                )
            return owned_guidance_preferences_store

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        nonlocal active_lifespans
        nonlocal service_guard
        nonlocal owned_repository
        nonlocal owned_market_services
        nonlocal owned_source_settings_services
        nonlocal owned_formula_service
        nonlocal owned_backtest_services
        nonlocal owned_model_catalog
        nonlocal owned_secret_store
        nonlocal secret_store_initialized
        nonlocal secret_store_error
        nonlocal owned_model_settings_service
        nonlocal owned_analysis_service
        nonlocal owned_analysis_preflight
        nonlocal owned_startup_secret_hydrator
        nonlocal owned_onboarding_service
        nonlocal owned_workspace_service
        nonlocal owned_market_navigation_service
        nonlocal owned_guidance_preferences_store
        with shutdown_lock:
            if active_lifespans == 0:
                candidate = service_lifecycle(
                    resolved_settings.data_dir,
                    role="api",
                    preflight=lambda: recover_interrupted_restore(
                        data_dir=resolved_settings.data_dir,
                        _lifecycle_held=True,
                    ),
                )
                candidate.__enter__()
                service_guard = candidate
            active_lifespans += 1
        try:
            with shutdown_lock:
                if owned_startup_secret_hydrator is None:
                    owned_startup_secret_hydrator = StartupSecretHydrator.open(
                        resolved_settings
                    )
            yield
        finally:
            with shutdown_lock:
                active_lifespans -= 1
                if active_lifespans:
                    return
                resources = (
                    owned_model_settings_service,
                    owned_model_catalog,
                    owned_source_settings_services,
                    owned_market_services,
                    owned_repository,
                    owned_startup_secret_hydrator,
                )
                owned_repository = None
                owned_market_services = None
                owned_source_settings_services = None
                owned_formula_service = None
                owned_backtest_services = None
                owned_model_catalog = None
                owned_secret_store = None
                secret_store_initialized = False
                secret_store_error = False
                owned_model_settings_service = None
                owned_analysis_service = None
                owned_analysis_preflight = None
                owned_startup_secret_hydrator = None
                owned_onboarding_service = None
                owned_workspace_service = None
                owned_market_navigation_service = None
                owned_guidance_preferences_store = None
                closing_service_guard = service_guard
                service_guard = None
            for resource in resources:
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception:
                    continue
            if closing_service_guard is not None:
                closing_service_guard.__exit__(None, None, None)

    application = FastAPI(
        title=resolved_settings.app_name,
        version="1.1.0",
        lifespan=lifespan,
    )
    if desktop_session is not None:
        application.add_middleware(
            DesktopSessionMiddleware,
            session=desktop_session,
        )
    application.state.task_repository_provider = provide_task_repository
    application.state.market_services_provider = provide_market_services
    application.state.source_settings_services_provider = (
        provide_source_settings_services
    )
    application.state.formula_service_provider = provide_formula_service
    application.state.backtest_services_provider = provide_backtest_services
    application.state.model_settings_services_provider = provide_model_settings_services
    application.state.analysis_services_provider = provide_analysis_services
    application.state.analysis_preflight_provider = provide_analysis_preflight
    application.state.onboarding_service_provider = provide_onboarding_service
    application.state.workspace_service_provider = provide_workspace_service
    application.state.market_navigation_service_provider = (
        provide_market_navigation_service
    )
    application.state.guidance_preferences_store_provider = (
        provide_guidance_preferences_store
    )
    application.state.database_identity_provider = database_identity.current
    application.state.model_settings_cursor_key = secrets.token_bytes(32)
    application.state.analysis_cursor_key = secrets.token_bytes(32)

    async def request_validation_handler(
        request: Request, error: Exception
    ) -> Response:
        if request.url.path == "/api/backtests" or request.url.path.startswith(
            "/api/backtests/"
        ):
            return await backtest_request_validation_handler(request, error)
        if any(
            request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
            for prefix in (
                "/api/settings/models",
                "/api/analysis",
                "/api/tasks",
                "/api/v1/onboarding",
                "/api/v1/workspace",
                "/api/v1/market/navigation",
                "/api/v1/guidance",
            )
        ):
            return JSONResponse(status_code=422, content={"code": "invalid_request"})
        return await market_request_validation_handler(request, error)

    async def application_database_mismatch_handler(
        request: Request, _error: Exception
    ) -> JSONResponse:
        if (
            request.url.path == "/api/formulas"
            or request.url.path.startswith("/api/formulas/")
            or (
                request.url.path == "/api/market/bars"
                and "formula_version_id" in request.query_params
            )
        ):
            return await formula_service_database_mismatch_handler(
                request,
                FormulaServiceDatabaseMismatch(
                    "formula and application storage do not match"
                ),
            )
        return JSONResponse(status_code=503, content={"code": "storage_unavailable"})

    application.add_exception_handler(
        RequestValidationError,
        request_validation_handler,
    )
    application.add_exception_handler(
        FormulaServiceDatabaseMismatch,
        formula_service_database_mismatch_handler,
    )
    application.add_exception_handler(
        BacktestServiceDatabaseMismatch,
        backtest_service_database_mismatch_handler,
    )
    application.add_exception_handler(
        SourceSettingsStorageError,
        source_settings_storage_exception_handler,
    )
    application.add_exception_handler(
        _ApplicationDatabaseMismatch,
        application_database_mismatch_handler,
    )
    application.include_router(health_router, prefix="/api")
    application.include_router(onboarding_router, prefix="/api")
    application.include_router(guidance_router, prefix="/api")
    application.include_router(workspace_router, prefix="/api")
    application.include_router(market_navigation_router, prefix="/api")
    application.include_router(tasks_router, prefix="/api")
    application.include_router(market_router, prefix="/api")
    application.include_router(settings_router, prefix="/api")
    application.include_router(models_router, prefix="/api")
    application.include_router(analysis_router, prefix="/api")
    application.include_router(formulas_router, prefix="/api")
    application.include_router(backtests_router, prefix="/api")
    if desktop_session is not None:
        resolved_desktop_lifecycle = (
            desktop_lifecycle
            if desktop_lifecycle is not None
            else DesktopLifecycleController()
        )

        @application.get(
            "/api/desktop/handshake",
            response_model=DesktopHandshake,
            tags=["desktop"],
        )
        def desktop_handshake() -> DesktopHandshake:
            provide_task_repository()
            return desktop_session.handshake()

        @application.get("/api/desktop/activity", tags=["desktop"])
        def desktop_activity() -> Response:
            try:
                metrics = provide_task_repository().metrics()
            except Exception:
                return JSONResponse(
                    status_code=503,
                    content={"code": "storage_unavailable"},
                )
            return JSONResponse(
                content={
                    "queued": metrics.by_status["queued"],
                    "running": metrics.by_status["running"],
                }
            )

        @application.post("/api/desktop/shutdown", tags=["desktop"])
        def desktop_shutdown() -> Response:
            repository = provide_task_repository()
            try:
                with repository.hold_claim_gate():
                    metrics = repository.metrics()
                    queued = metrics.by_status["queued"]
                    running = metrics.by_status["running"]
                    if queued + running > 0:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "code": "desktop_tasks_active",
                                "queued": queued,
                                "running": running,
                            },
                        )
                    resolved_desktop_lifecycle.request_shutdown()
            except Exception:
                return JSONResponse(
                    status_code=503,
                    content={"code": "storage_unavailable"},
                )
            return JSONResponse(
                status_code=202,
                content={"status": "shutdown_requested"},
            )

    if resolved_settings.web_dist_dir is not None:
        install_web_routes(application, resolved_settings.web_dist_dir)
    return application


app = create_app()
