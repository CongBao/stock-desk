from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

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
from stock_desk.api.market import (
    MarketServices,
    market_request_validation_handler,
    router as market_router,
)
from stock_desk.api.settings import (
    SourceSettingsServices,
    SourceSettingsStorageError,
    router as settings_router,
    source_settings_storage_exception_handler,
)
from stock_desk.api.tasks import router as tasks_router
from stock_desk.config import Settings, get_settings
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService, FormulaServiceDatabaseMismatch
from stock_desk.tasks.repository import TaskRepository
from stock_desk.web import install_web_routes


def create_app(
    settings: Settings | None = None,
    task_repository: TaskRepository | None = None,
    market_services: MarketServices | None = None,
    source_settings_services: SourceSettingsServices | None = None,
    formula_service: FormulaService | None = None,
    backtest_services: BacktestServices | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
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

    def provide_task_repository() -> TaskRepository:
        nonlocal owned_repository
        if task_repository is not None:
            return task_repository
        with repository_lock:
            if owned_repository is None:
                owned_repository = TaskRepository.open(resolved_settings.database_url)
            return owned_repository

    def provide_market_services() -> MarketServices:
        nonlocal owned_market_services
        if market_services is not None:
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
            return owned_market_services

    def provide_source_settings_services() -> SourceSettingsServices:
        nonlocal owned_source_settings_services
        if source_settings_services is not None:
            return source_settings_services
        with source_settings_services_lock:
            if owned_source_settings_services is None:
                owned_source_settings_services = SourceSettingsServices.open(
                    database_url=resolved_settings.database_url,
                    settings=resolved_settings,
                )
            return owned_source_settings_services

    def provide_formula_service() -> FormulaService:
        nonlocal owned_formula_service
        if formula_service is not None:
            services = provide_market_services()
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
            return owned_formula_service

    def provide_backtest_services() -> BacktestServices:
        nonlocal owned_backtest_services
        if backtest_services is not None:
            if isinstance(backtest_services, BacktestServices):
                identities = (
                    provide_market_services().database_identity,
                    provide_formula_service().database_identity,
                    provide_task_repository().database_identity,
                )
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
                shared_market = provide_market_services()
                shared_formula = provide_formula_service()
                shared_tasks = provide_task_repository()
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
            return owned_backtest_services

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_repository is not None:
                owned_repository.close()
            if owned_market_services is not None:
                owned_market_services.close()
            if owned_source_settings_services is not None:
                owned_source_settings_services.close()

    application = FastAPI(
        title=resolved_settings.app_name,
        version="0.3.0",
        lifespan=lifespan,
    )
    application.state.task_repository_provider = provide_task_repository
    application.state.market_services_provider = provide_market_services
    application.state.source_settings_services_provider = (
        provide_source_settings_services
    )
    application.state.formula_service_provider = provide_formula_service
    application.state.backtest_services_provider = provide_backtest_services

    async def request_validation_handler(
        request: Request, error: Exception
    ) -> Response:
        if request.url.path == "/api/backtests" or request.url.path.startswith(
            "/api/backtests/"
        ):
            return await backtest_request_validation_handler(request, error)
        return await market_request_validation_handler(request, error)

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
    application.include_router(health_router, prefix="/api")
    application.include_router(tasks_router, prefix="/api")
    application.include_router(market_router, prefix="/api")
    application.include_router(settings_router, prefix="/api")
    application.include_router(formulas_router, prefix="/api")
    application.include_router(backtests_router, prefix="/api")
    if resolved_settings.web_dist_dir is not None:
        install_web_routes(application, resolved_settings.web_dist_dir)
    return application


app = create_app()
