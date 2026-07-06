from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
from threading import Lock

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

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
    application.add_exception_handler(
        RequestValidationError,
        market_request_validation_handler,
    )
    application.add_exception_handler(
        FormulaServiceDatabaseMismatch,
        formula_service_database_mismatch_handler,
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
    if resolved_settings.web_dist_dir is not None:
        install_web_routes(application, resolved_settings.web_dist_dir)
    return application


app = create_app()
