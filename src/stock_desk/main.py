from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
from threading import Lock

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from stock_desk.api.health import router as health_router
from stock_desk.api.market import (
    MarketServices,
    market_request_validation_handler,
    router as market_router,
)
from stock_desk.api.tasks import router as tasks_router
from stock_desk.config import Settings, get_settings
from stock_desk.tasks.repository import TaskRepository
from stock_desk.web import install_web_routes


def create_app(
    settings: Settings | None = None,
    task_repository: TaskRepository | None = None,
    market_services: MarketServices | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    owned_repository: TaskRepository | None = None
    repository_lock = Lock()
    owned_market_services: MarketServices | None = None
    market_services_lock = Lock()

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

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_repository is not None:
                owned_repository.close()
            if owned_market_services is not None:
                owned_market_services.close()

    application = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.task_repository_provider = provide_task_repository
    application.state.market_services_provider = provide_market_services
    application.add_exception_handler(
        RequestValidationError,
        market_request_validation_handler,
    )
    application.include_router(health_router, prefix="/api")
    application.include_router(tasks_router, prefix="/api")
    application.include_router(market_router, prefix="/api")
    if resolved_settings.web_dist_dir is not None:
        install_web_routes(application, resolved_settings.web_dist_dir)
    return application


app = create_app()
