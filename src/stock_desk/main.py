from fastapi import FastAPI

from stock_desk.api.health import router as health_router


def create_app() -> FastAPI:
    application = FastAPI(title="stock-desk", version="0.1.0")
    application.include_router(health_router, prefix="/api")
    return application


app = create_app()
