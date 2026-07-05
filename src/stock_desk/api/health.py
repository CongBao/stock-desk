from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel


class HealthResponse(BaseModel):
    name: Literal["stock-desk"] = "stock-desk"
    status: Literal["ok"] = "ok"
    api_version: Literal["v1"] = "v1"


router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()
