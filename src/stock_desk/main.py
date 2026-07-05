from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI

from stock_desk.api.health import router as health_router
from stock_desk.api.tasks import router as tasks_router
from stock_desk.config import Settings, get_settings
from stock_desk.tasks.repository import TaskRepository
from stock_desk.web import install_web_routes


def create_app(
    settings: Settings | None = None,
    task_repository: TaskRepository | None = None,
) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    owned_repository: TaskRepository | None = None
    repository_lock = Lock()

    def provide_task_repository() -> TaskRepository:
        nonlocal owned_repository
        if task_repository is not None:
            return task_repository
        with repository_lock:
            if owned_repository is None:
                owned_repository = TaskRepository.open(resolved_settings.database_url)
            return owned_repository

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_repository is not None:
                owned_repository.close()

    application = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.task_repository_provider = provide_task_repository
    application.include_router(health_router, prefix="/api")
    application.include_router(tasks_router, prefix="/api")
    if resolved_settings.web_dist_dir is not None:
        install_web_routes(application, resolved_settings.web_dist_dir)
    return application


app = create_app()
