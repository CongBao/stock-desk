from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from stock_desk.analysis.evidence import EvidenceItem
from stock_desk.analysis.model_catalog import (
    ModelCatalogError,
    ModelNotFound,
    ModelNotVerified,
)
from stock_desk.analysis.model_settings import (
    ModelSettingsSecureStorageError,
    ModelSettingsStorageError,
)
from stock_desk.analysis.report import (
    ResearchReport,
    clean_research_report_active_secrets,
)
from stock_desk.analysis.runtime import AnalysisPreflightService
from stock_desk.analysis.repository import (
    AnalysisConflict,
    AnalysisHistoryKey,
    AnalysisNotFound,
    AnalysisRepositoryError,
)
from stock_desk.analysis.service import (
    AnalysisDetail,
    AnalysisEvidenceNotFound,
    AnalysisReportNotReady,
    AnalysisReportUnavailable,
    AnalysisService,
    AnalysisServiceStorageError,
    AnalysisStateConflict,
)
from stock_desk.tasks.repository import (
    TaskConflict,
    TaskNotFound,
    TaskRepositoryError,
)
from stock_desk.tasks.models import TaskStatus


_SYMBOL_PATTERN = r"^[0-9]{6}\.(?:SH|SZ|BJ)$"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SOURCE_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"


class _AnalysisDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class AnalysisRetryRequest(_AnalysisDTO):
    max_retries: Annotated[StrictInt, Field(ge=0, le=5)]


class AnalysisCreateRequest(_AnalysisDTO):
    symbol: Annotated[str, Field(pattern=_SYMBOL_PATTERN)]
    model_config_id: Annotated[
        str, Field(min_length=71, max_length=71, pattern=_DIGEST_PATTERN)
    ]
    retry: AnalysisRetryRequest


class AnalysisPreflightRequest(_AnalysisDTO):
    symbol: Annotated[str, Field(pattern=_SYMBOL_PATTERN)]


class AnalysisPreflightCandidateResponse(_AnalysisDTO):
    source: Annotated[str, Field(pattern=_SOURCE_PATTERN, max_length=64)]
    position: Annotated[StrictInt, Field(ge=0)]
    supported: bool
    configured: bool
    outcome: Literal[
        "selected", "failed", "not_attempted", "unsupported", "unconfigured"
    ]
    failure_reason: str | None


class AnalysisPreflightCategoryResponse(_AnalysisDTO):
    kind: Literal["market", "fundamentals", "announcements", "news"]
    critical: bool
    connection_state: Literal["available", "degraded", "missing"]
    route_source: Annotated[str, Field(pattern=_SOURCE_PATTERN, max_length=64)]
    actual_source: Annotated[str | None, Field(pattern=_SOURCE_PATTERN, max_length=64)]
    ordered_candidates: tuple[AnalysisPreflightCandidateResponse, ...]
    attempted_sources: tuple[str, ...]
    missing_reason: str | None
    recovery_code: str | None
    permission_gap: bool
    data_cutoff: datetime | None
    fetched_at: datetime | None
    dataset_version: str | None
    quality_flags: tuple[str, ...]


class AnalysisPreflightResponse(_AnalysisDTO):
    symbol: str
    preview_snapshot_id: str
    reservation: Literal[False]
    rating_eligible: bool
    checked_at: datetime
    categories: tuple[AnalysisPreflightCategoryResponse, ...]


class AnalysisSubmissionResponse(_AnalysisDTO):
    run_id: str
    task_id: str
    parent_run_id: str | None
    requested_stage: str | None
    status: Literal["queued"]
    snapshot_id: str | None


class AnalysisErrorResponse(_AnalysisDTO):
    code: str


class AnalysisStageResponse(_AnalysisDTO):
    stage: str
    ordinal: StrictInt
    kind: Literal["data", "role"]
    status: str
    attempt_count: int
    source_run_id: str | None
    failure_code: str | None
    retryable: bool | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: float | None
    retry_allowed: bool


class AnalysisOverviewResponse(_AnalysisDTO):
    run_id: str
    task_id: str
    symbol: str
    parent_run_id: str | None
    requested_stage: str | None
    status: str
    task_status: TaskStatus
    progress: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    cancel_requested: bool
    current_stage: str | None
    snapshot_id: str | None
    report_id: str | None
    failure_code: str | None
    model_config_id: str
    model_provider: str
    model_name: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: float | None


class AnalysisDetailResponse(AnalysisOverviewResponse):
    stages: tuple[AnalysisStageResponse, ...]


class AnalysisHistoryResponse(_AnalysisDTO):
    items: tuple[AnalysisOverviewResponse, ...]
    next_cursor: str | None


class AnalysisDatabaseMismatch(RuntimeError):
    pass


class AnalysisCursorError(ValueError):
    pass


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code})


class _SafeAnalysisRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Any]:
        route_handler = super().get_route_handler()

        async def safe_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except RequestValidationError:
                return _error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)
            except ValidationError:
                return _error(
                    "storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
                )
            except AnalysisCursorError:
                return _error("invalid_cursor", status.HTTP_422_UNPROCESSABLE_CONTENT)
            except AnalysisEvidenceNotFound:
                return _error("evidence_not_found", status.HTTP_404_NOT_FOUND)
            except (AnalysisNotFound, ModelNotFound, TaskNotFound):
                return _error("not_found", status.HTTP_404_NOT_FOUND)
            except ModelNotVerified:
                return _error("model_not_verified", status.HTTP_409_CONFLICT)
            except ModelSettingsSecureStorageError:
                return _error(
                    "secure_storage_unavailable",
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            except ModelSettingsStorageError:
                return _error(
                    "storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
                )
            except AnalysisReportNotReady:
                return _error("report_not_ready", status.HTTP_409_CONFLICT)
            except AnalysisReportUnavailable:
                return _error("report_unavailable", status.HTTP_409_CONFLICT)
            except (AnalysisStateConflict, AnalysisConflict, TaskConflict):
                return _error("state_conflict", status.HTTP_409_CONFLICT)
            except (
                AnalysisRepositoryError,
                AnalysisServiceStorageError,
                ModelCatalogError,
                TaskRepositoryError,
            ):
                return _error(
                    "storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
                )
            except AnalysisDatabaseMismatch:
                return _error(
                    "storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
                )
            except Exception:
                return _error(
                    "service_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE
                )

        return safe_route_handler


def get_analysis_services(request: Request) -> AnalysisService:
    provider = getattr(request.app.state, "analysis_services_provider", None)
    if not callable(provider):
        raise AnalysisDatabaseMismatch()
    services = cast(Callable[[], AnalysisService], provider)()
    identity_provider = getattr(request.app.state, "database_identity_provider", None)
    expected = (
        identity_provider()
        if callable(identity_provider)
        else getattr(request.app.state, "database_identity", None)
    )
    if expected is None:
        raise AnalysisDatabaseMismatch()
    identities = tuple(
        getattr(services, attribute, None)
        for attribute in (
            "database_identity",
            "analysis_repository_identity",
            "task_repository_identity",
            "model_catalog_identity",
        )
    )
    if any(identity is None or identity != expected for identity in identities):
        raise AnalysisDatabaseMismatch()
    return services


AnalysisServicesDependency = Annotated[AnalysisService, Depends(get_analysis_services)]


def get_analysis_preflight_service(request: Request) -> AnalysisPreflightService:
    provider = getattr(request.app.state, "analysis_preflight_provider", None)
    if not callable(provider):
        raise AnalysisDatabaseMismatch()
    service = cast(Callable[[], AnalysisPreflightService], provider)()
    identity_provider = getattr(request.app.state, "database_identity_provider", None)
    expected = (
        identity_provider()
        if callable(identity_provider)
        else getattr(request.app.state, "database_identity", None)
    )
    if expected is None:
        raise AnalysisDatabaseMismatch()
    if getattr(service, "database_identity", None) != expected:
        raise AnalysisDatabaseMismatch()
    return service


AnalysisPreflightDependency = Annotated[
    AnalysisPreflightService, Depends(get_analysis_preflight_service)
]


def get_analysis_cursor_key(request: Request) -> bytes:
    key = getattr(request.app.state, "analysis_cursor_key", None)
    if type(key) is not bytes or len(key) != 32:
        raise AnalysisDatabaseMismatch()
    return key


AnalysisCursorKeyDependency = Annotated[bytes, Depends(get_analysis_cursor_key)]
RunIdPath = Annotated[
    str,
    Path(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ),
]
EvidenceIdPath = Annotated[
    str,
    Path(min_length=71, max_length=71, pattern=_DIGEST_PATTERN),
]
RetryStagePath = Annotated[
    Literal["technical", "fundamental_news", "bull", "bear", "risk_decision"],
    Path(),
]


router = APIRouter(
    prefix="/analysis",
    tags=["analysis"],
    route_class=_SafeAnalysisRoute,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": AnalysisErrorResponse},
        status.HTTP_409_CONFLICT: {"model": AnalysisErrorResponse},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": AnalysisErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": AnalysisErrorResponse},
    },
)


@router.post(
    "",
    response_model=AnalysisSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_analysis(
    body: AnalysisCreateRequest,
    services: AnalysisServicesDependency,
) -> AnalysisSubmissionResponse:
    submission = services.submit(
        symbol=body.symbol,
        model_config_id=body.model_config_id,
        max_retries=body.retry.max_retries,
    )
    return AnalysisSubmissionResponse(
        run_id=submission.run_id,
        task_id=submission.task_id,
        parent_run_id=submission.parent_run_id,
        requested_stage=submission.requested_stage,
        status=submission.status,
        snapshot_id=submission.snapshot_id,
    )


@router.post("/preflight", response_model=AnalysisPreflightResponse)
def preflight_analysis(
    body: AnalysisPreflightRequest,
    service: AnalysisPreflightDependency,
) -> AnalysisPreflightResponse:
    result = service.check(body.symbol)
    return AnalysisPreflightResponse(
        symbol=result.symbol,
        preview_snapshot_id=result.preview_snapshot_id,
        reservation=False,
        rating_eligible=result.rating_eligible,
        checked_at=result.checked_at,
        categories=tuple(
            AnalysisPreflightCategoryResponse(
                kind=cast(Any, category.kind.value),
                critical=category.critical,
                connection_state=category.connection_state,
                route_source=category.route_source,
                actual_source=category.actual_source,
                ordered_candidates=tuple(
                    AnalysisPreflightCandidateResponse(
                        source=candidate.source,
                        position=candidate.position,
                        supported=candidate.supported,
                        configured=candidate.configured,
                        outcome=candidate.outcome,
                        failure_reason=(
                            candidate.failure_reason.value
                            if candidate.failure_reason is not None
                            else None
                        ),
                    )
                    for candidate in category.ordered_candidates
                ),
                attempted_sources=category.attempted_sources,
                missing_reason=category.missing_reason,
                recovery_code=category.recovery_code,
                permission_gap=category.permission_gap,
                data_cutoff=category.data_cutoff,
                fetched_at=category.fetched_at,
                dataset_version=category.dataset_version,
                quality_flags=category.quality_flags,
            )
            for category in result.categories
        ),
    )


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _encode_cursor(
    key: AnalysisHistoryKey, *, symbol: str | None, cursor_key: bytes
) -> str:
    body: dict[str, object] = {
        "collection": "analysis",
        "created_at": key.created_at.astimezone(timezone.utc).isoformat(),
        "id": key.id,
        "symbol": symbol,
        "v": 1,
    }
    signature = hmac.new(cursor_key, _canonical_json(body), hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(
        _canonical_json({"body": body, "signature": signature})
    ).rstrip(b"=")
    return encoded.decode("ascii")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisCursorError()
        result[key] = value
    return result


def _decode_cursor(
    cursor: str | None, *, symbol: str | None, cursor_key: bytes
) -> AnalysisHistoryKey | None:
    if cursor is None:
        return None
    if not cursor or len(cursor) > 4096:
        raise AnalysisCursorError()
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            (cursor + padding).encode("ascii"), altchars=b"-_", validate=True
        )
        envelope = json.loads(raw, object_pairs_hook=_unique_object)
        if type(envelope) is not dict or set(envelope) != {"body", "signature"}:
            raise AnalysisCursorError()
        body = envelope["body"]
        signature = envelope["signature"]
        if type(body) is not dict or set(body) != {
            "collection",
            "created_at",
            "id",
            "symbol",
            "v",
        }:
            raise AnalysisCursorError()
        if (
            type(body["v"]) is not int
            or body["v"] != 1
            or body["collection"] != "analysis"
            or body["symbol"] != symbol
            or type(body["created_at"]) is not str
            or type(body["id"]) is not str
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                body["id"],
            )
            is None
            or type(signature) is not str
            or len(signature) != 64
        ):
            raise AnalysisCursorError()
        expected = hmac.new(
            cursor_key, _canonical_json(cast(dict[str, object], body)), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AnalysisCursorError()
        created_at = datetime.fromisoformat(body["created_at"])
        if created_at.tzinfo is None:
            raise AnalysisCursorError()
    except (AnalysisCursorError, UnicodeError):
        raise AnalysisCursorError() from None
    except Exception:
        raise AnalysisCursorError() from None
    return AnalysisHistoryKey(
        created_at=created_at.astimezone(timezone.utc), id=body["id"]
    )


def _overview(detail: AnalysisDetail) -> AnalysisOverviewResponse:
    run = detail.run
    task = detail.task
    return AnalysisOverviewResponse(
        run_id=run.id,
        task_id=run.task_id,
        symbol=run.symbol,
        parent_run_id=run.parent_run_id,
        requested_stage=run.requested_stage,
        status=run.status,
        task_status=task.status,
        progress=task.progress,
        cancel_requested=task.cancel_requested,
        current_stage=run.current_stage,
        snapshot_id=run.snapshot_id,
        report_id=run.report_id,
        failure_code=run.failure_code,
        model_config_id=run.model_config_id,
        model_provider=run.model_provider,
        model_name=run.model_name,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
    )


def _detail_response(detail: AnalysisDetail) -> AnalysisDetailResponse:
    overview = _overview(detail)
    retry_stages = detail.retry_stages
    stages = tuple(
        AnalysisStageResponse(
            stage=item.role,
            ordinal=item.ordinal,
            kind="data" if item.ordinal < 0 else "role",
            status=item.status,
            attempt_count=item.attempt_count,
            source_run_id=item.source_run_id,
            failure_code=item.failure_code,
            retryable=item.retryable,
            started_at=item.started_at,
            finished_at=item.finished_at,
            duration_ms=item.duration_ms,
            retry_allowed=item.status.value == "failed" and item.role in retry_stages,
        )
        for item in detail.stages
    )
    return AnalysisDetailResponse(**overview.model_dump(), stages=stages)


@router.get("", response_model=AnalysisHistoryResponse)
def list_analysis(
    services: AnalysisServicesDependency,
    cursor_key: AnalysisCursorKeyDependency,
    symbol: Annotated[str | None, Query(pattern=_SYMBOL_PATTERN)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> AnalysisHistoryResponse:
    after = _decode_cursor(cursor, symbol=symbol, cursor_key=cursor_key)
    page = services.history(limit=limit, after=after, symbol=symbol)
    next_cursor = (
        _encode_cursor(page.next_key, symbol=symbol, cursor_key=cursor_key)
        if page.next_key is not None
        else None
    )
    return AnalysisHistoryResponse(
        items=tuple(_overview(item) for item in page.items),
        next_cursor=next_cursor,
    )


@router.get("/{run_id}", response_model=AnalysisDetailResponse)
def get_analysis(
    run_id: RunIdPath, services: AnalysisServicesDependency
) -> AnalysisDetailResponse:
    return _detail_response(services.detail(run_id))


@router.post(
    "/{run_id}/cancel",
    response_model=AnalysisDetailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_analysis(
    run_id: RunIdPath, services: AnalysisServicesDependency
) -> AnalysisDetailResponse:
    return _detail_response(services.cancel(run_id))


@router.get("/{run_id}/report", response_model=ResearchReport)
def get_analysis_report(
    run_id: RunIdPath, services: AnalysisServicesDependency
) -> ResearchReport:
    return clean_research_report_active_secrets(services.report(run_id))


@router.get("/{run_id}/evidence/{evidence_id}", response_model=EvidenceItem)
def get_analysis_evidence(
    run_id: RunIdPath,
    evidence_id: EvidenceIdPath,
    services: AnalysisServicesDependency,
) -> EvidenceItem:
    return services.evidence(run_id, evidence_id)


@router.post(
    "/{run_id}/stages/{stage}/retry",
    response_model=AnalysisSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_analysis_stage(
    run_id: RunIdPath,
    stage: RetryStagePath,
    services: AnalysisServicesDependency,
) -> AnalysisSubmissionResponse:
    submission = services.retry(run_id, stage)
    return AnalysisSubmissionResponse(
        run_id=submission.run_id,
        task_id=submission.task_id,
        parent_run_id=submission.parent_run_id,
        requested_stage=submission.requested_stage,
        status=submission.status,
        snapshot_id=submission.snapshot_id,
    )
