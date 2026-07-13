from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from stock_desk.diagnostics.models import DiagnosticSnapshot, DiagnosticSnapshotService


def get_diagnostic_snapshot_service(request: Request) -> DiagnosticSnapshotService:
    provider = getattr(request.app.state, "diagnostic_snapshot_service_provider", None)
    if not callable(provider):
        raise RuntimeError("desktop diagnostics are unavailable")
    return cast(Callable[[], DiagnosticSnapshotService], provider)()


DiagnosticServiceDependency = Annotated[
    DiagnosticSnapshotService, Depends(get_diagnostic_snapshot_service)
]


class _SafeDiagnosticRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Any]:
        route_handler = super().get_route_handler()

        async def safe_route_handler(request: Request) -> Any:
            try:
                return await route_handler(request)
            except Exception:
                return JSONResponse(
                    status_code=503,
                    content={"code": "diagnostic_snapshot_unavailable"},
                )

        return safe_route_handler


router = APIRouter(
    prefix="/v1/diagnostics",
    tags=["desktop-diagnostics"],
    route_class=_SafeDiagnosticRoute,
)


@router.post("/snapshot", response_model=DiagnosticSnapshot)
def create_diagnostic_snapshot(
    service: DiagnosticServiceDependency,
) -> DiagnosticSnapshot:
    """Create a local snapshot only after an explicit desktop action."""

    return service.snapshot()


__all__ = ["router"]
