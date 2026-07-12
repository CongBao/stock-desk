from collections.abc import Callable
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from stock_desk.guidance.models import (
    GuidancePage,
    GuidancePreferences,
    GuidanceStatus,
)
from stock_desk.guidance.store import (
    GuidancePreferencesConflict,
    GuidancePreferencesStorageError,
    GuidancePreferencesStore,
)


class GuidancePreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)

    expected_revision: int = Field(ge=0)
    page: GuidancePage
    content_version: int = Field(ge=1, le=2_147_483_647)
    status: GuidanceStatus


def get_guidance_store(request: Request) -> GuidancePreferencesStore:
    provider = cast(
        Callable[[], GuidancePreferencesStore],
        request.app.state.guidance_preferences_store_provider,
    )
    return provider()


GuidanceStoreDependency = Annotated[
    GuidancePreferencesStore, Depends(get_guidance_store)
]

router = APIRouter(prefix="/v1/guidance", tags=["guidance"])


def _storage_error() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"code": "guidance_preferences_unavailable"},
    )


@router.get("/preferences", response_model=GuidancePreferences)
def get_preferences(
    store: GuidanceStoreDependency,
) -> GuidancePreferences | JSONResponse:
    try:
        return store.load()
    except GuidancePreferencesStorageError:
        return _storage_error()


@router.put("/preferences", response_model=GuidancePreferences)
def update_preferences(
    body: GuidancePreferencesUpdate,
    store: GuidanceStoreDependency,
) -> GuidancePreferences | JSONResponse:
    try:
        return store.update(
            expected_revision=body.expected_revision,
            page=body.page,
            content_version=body.content_version,
            status=body.status,
        )
    except GuidancePreferencesConflict:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"code": "guidance_revision_conflict"},
        )
    except GuidancePreferencesStorageError:
        return _storage_error()
