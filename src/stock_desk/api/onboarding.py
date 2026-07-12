from __future__ import annotations

from datetime import datetime
from typing import Annotated, Callable, Literal, cast

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from stock_desk.market.types import (
    CanonicalSymbol,
    Exchange,
    InstrumentKind,
    ProviderId,
)
from stock_desk.onboarding.models import OnboardingState, OnboardingStep
from stock_desk.onboarding.service import OnboardingConflict, OnboardingService
from stock_desk.onboarding.store import OnboardingStateStorageError
from stock_desk.workspace.models import WorkspaceInstrument
from stock_desk.workspace.service import WorkspaceService
from stock_desk.workspace.store import WorkspaceStateStorageError


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)


class ProgressRequest(_RequestModel):
    current_step: OnboardingStep
    source_id: ProviderId | None = None
    symbol: CanonicalSymbol | None = None


class SynchronizeRequest(_RequestModel):
    source_id: ProviderId
    symbol: CanonicalSymbol


class CompleteRequest(_RequestModel):
    symbol: CanonicalSymbol


class OnboardingErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str


class SourceOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: ProviderId
    label: str
    description: str
    requires_token: Literal[False]
    recommended: bool
    status: Literal["ready", "unavailable"]
    data_cutoff: datetime | None


class SourcesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: tuple[SourceOptionResponse, ...]


class InstrumentOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: CanonicalSymbol
    name: str
    exchange: Exchange
    instrument_kind: InstrumentKind


class InstrumentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: tuple[InstrumentOptionResponse, ...]


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code})


def get_onboarding_service(request: Request) -> OnboardingService:
    provider = cast(
        Callable[[], OnboardingService],
        request.app.state.onboarding_service_provider,
    )
    return provider()


OnboardingServiceDependency = Annotated[
    OnboardingService, Depends(get_onboarding_service)
]


def get_workspace_service(request: Request) -> WorkspaceService:
    provider = cast(
        Callable[[], WorkspaceService], request.app.state.workspace_service_provider
    )
    return provider()


WorkspaceServiceDependency = Annotated[WorkspaceService, Depends(get_workspace_service)]


router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])


@router.get("/state", response_model=OnboardingState)
def get_state(service: OnboardingServiceDependency) -> OnboardingState | JSONResponse:
    try:
        return service.state()
    except OnboardingStateStorageError:
        return _error(
            "onboarding_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.get("/sources", response_model=SourcesResponse)
def get_sources(
    service: OnboardingServiceDependency,
) -> dict[str, object] | JSONResponse:
    try:
        return {"items": service.sources()}
    except OnboardingStateStorageError:
        return _error(
            "onboarding_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.get("/instruments", response_model=InstrumentsResponse)
def search_instruments(
    service: OnboardingServiceDependency,
    q: Annotated[str, Query(max_length=64)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object] | JSONResponse:
    try:
        query = q if q.strip() else "000001.SS"
        return {"items": service.search(query, limit=limit)}
    except OnboardingConflict as error:
        status_code = (
            status.HTTP_409_CONFLICT
            if error.code.endswith("not_ready")
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return _error(error.code, status_code)
    except OnboardingStateStorageError:
        return _error(
            "onboarding_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.put("/progress", response_model=OnboardingState)
def update_progress(
    body: ProgressRequest,
    service: OnboardingServiceDependency,
) -> OnboardingState | JSONResponse:
    try:
        if body.current_step is OnboardingStep.WELCOME:
            return service.state()
        if body.current_step is OnboardingStep.DATA_PREPARATION:
            if body.source_id is None:
                return service.enter_data_preparation()
            return service.prepare(body.source_id)
        if body.current_step is OnboardingStep.INSTRUMENT_SELECTION:
            state = service.state()
            if body.source_id is not None and (
                state.source is None or state.source.id is not body.source_id
            ):
                return service.prepare(body.source_id)
            if state.source is None:
                return _error("onboarding_source_required", status.HTTP_409_CONFLICT)
            return state
        if body.current_step is OnboardingStep.SYNCHRONIZATION:
            if body.symbol is None:
                return _error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)
            return service.select(body.symbol)
        return _error("invalid_onboarding_transition", status.HTTP_409_CONFLICT)
    except OnboardingConflict as error:
        return _error(error.code, status.HTTP_409_CONFLICT)
    except OnboardingStateStorageError:
        return _error(
            "onboarding_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.post("/sync", response_model=OnboardingState)
def synchronize(
    body: SynchronizeRequest,
    service: OnboardingServiceDependency,
) -> OnboardingState | JSONResponse:
    try:
        state = service.state()
        if (
            state.source is not None
            and state.source.id is body.source_id
            and (
                state.current_step is not OnboardingStep.SYNCHRONIZATION
                or state.instrument.symbol != body.symbol
            )
        ):
            service.select(body.symbol)
        return service.synchronize(source_id=body.source_id, symbol=body.symbol)
    except OnboardingConflict as error:
        return _error(error.code, status.HTTP_409_CONFLICT)
    except OnboardingStateStorageError:
        return _error(
            "onboarding_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.post("/complete", response_model=OnboardingState)
def complete(
    body: CompleteRequest,
    service: OnboardingServiceDependency,
    workspace: WorkspaceServiceDependency,
) -> OnboardingState | JSONResponse:
    try:
        state = service.complete(body.symbol)
        workspace.initialize(
            WorkspaceInstrument(
                symbol=state.instrument.symbol,
                name=state.instrument.name,
                exchange=state.instrument.exchange,
                kind=state.instrument.instrument_kind,
            )
        )
        return state
    except OnboardingConflict as error:
        return _error(error.code, status.HTTP_409_CONFLICT)
    except OnboardingStateStorageError:
        return _error(
            "onboarding_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )
    except WorkspaceStateStorageError:
        return _error(
            "workspace_storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )


@router.post("/actions/{action}", response_model=OnboardingState)
def action(
    action: Annotated[Literal["retry", "switch_provider", "advanced", "demo"], Path()],
    service: OnboardingServiceDependency,
) -> OnboardingState | JSONResponse:
    try:
        handlers = {
            "retry": service.retry,
            "switch_provider": service.switch_provider,
            "advanced": service.advanced,
            "demo": service.demo,
        }
        return handlers[action]()
    except OnboardingConflict as error:
        return _error(error.code, status.HTTP_409_CONFLICT)
    except OnboardingStateStorageError:
        return _error(
            "onboarding_state_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
        )
