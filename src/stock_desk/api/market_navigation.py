from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from stock_desk.market.navigation import (
    MarketNavigationConflict,
    MarketNavigationInstrument,
    MarketNavigationService,
    MarketNavigationSnapshot,
    MarketNavigationStorageError,
)
from stock_desk.market.types import CanonicalSymbol, InstrumentKind, InstrumentName


class MarketNavigationInstrumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    symbol: CanonicalSymbol
    name: InstrumentName
    instrument_kind: InstrumentKind


class MarketNavigationReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    expected_revision: Annotated[StrictInt, Field(ge=0)]
    watchlist: Annotated[
        tuple[MarketNavigationInstrumentRequest, ...], Field(max_length=100)
    ]
    recent: Annotated[
        tuple[MarketNavigationInstrumentRequest, ...], Field(max_length=20)
    ]


def get_market_navigation_service(request: Request) -> MarketNavigationService:
    provider = cast(
        Callable[[], MarketNavigationService],
        request.app.state.market_navigation_service_provider,
    )
    return provider()


MarketNavigationServiceDependency = Annotated[
    MarketNavigationService, Depends(get_market_navigation_service)
]


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code})


router = APIRouter(prefix="/v1/market/navigation", tags=["market-navigation"])


@router.get("", response_model=MarketNavigationSnapshot)
def get_navigation(
    service: MarketNavigationServiceDependency,
) -> MarketNavigationSnapshot | JSONResponse:
    try:
        return service.state()
    except MarketNavigationStorageError:
        return _error(
            "market_navigation_storage_unavailable",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )


@router.put("", response_model=MarketNavigationSnapshot)
def replace_navigation(
    body: MarketNavigationReplaceRequest,
    service: MarketNavigationServiceDependency,
) -> MarketNavigationSnapshot | JSONResponse:
    try:
        return service.replace(
            expected_revision=body.expected_revision,
            watchlist=tuple(
                MarketNavigationInstrument.model_validate(
                    item.model_dump(mode="python"), strict=True
                )
                for item in body.watchlist
            ),
            recent=tuple(
                MarketNavigationInstrument.model_validate(
                    item.model_dump(mode="python"), strict=True
                )
                for item in body.recent
            ),
        )
    except MarketNavigationConflict as error:
        status_code = (
            status.HTTP_409_CONFLICT
            if error.code == "market_navigation_revision_conflict"
            else status.HTTP_503_SERVICE_UNAVAILABLE
            if error.code == "market_navigation_catalog_unavailable"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return _error(error.code, status_code)
    except MarketNavigationStorageError:
        return _error(
            "market_navigation_storage_unavailable",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
