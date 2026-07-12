from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse

from stock_desk.workspace.models import WorkspacePut, WorkspaceView
from stock_desk.workspace.service import WorkspaceConflict, WorkspaceService
from stock_desk.workspace.store import WorkspaceStateStorageError


def get_workspace_service(request: Request) -> WorkspaceService:
    provider = cast(
        Callable[[], WorkspaceService], request.app.state.workspace_service_provider
    )
    return provider()


WorkspaceServiceDependency = Annotated[WorkspaceService, Depends(get_workspace_service)]


router = APIRouter(prefix="/v1/workspace", tags=["workspace"])


@router.get("", response_model=WorkspaceView)
def get_workspace(service: WorkspaceServiceDependency) -> WorkspaceView:
    return service.restore()


@router.put("", response_model=WorkspaceView)
def put_workspace(
    body: WorkspacePut, service: WorkspaceServiceDependency
) -> WorkspaceView | JSONResponse:
    try:
        return service.update(body)
    except WorkspaceConflict as error:
        status_code = (
            status.HTTP_409_CONFLICT
            if error.code == "workspace_revision_conflict"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return JSONResponse(status_code=status_code, content={"code": error.code})
    except WorkspaceStateStorageError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"code": "workspace_storage_unavailable"},
        )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(service: WorkspaceServiceDependency) -> Response:
    try:
        service.delete()
    except WorkspaceStateStorageError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"code": "workspace_storage_unavailable"},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
