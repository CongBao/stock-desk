from fastapi import FastAPI

from stock_desk.api.health import router as health_router
from stock_desk.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings if settings is not None else get_settings()
    application = FastAPI(title=resolved_settings.app_name, version="0.1.0")
    application.include_router(health_router, prefix="/api")
    return application


app = create_app()
