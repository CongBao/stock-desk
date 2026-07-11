from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import math
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictFloat,
    StrictInt,
    ValidationInfo,
    field_validator,
)

from stock_desk.formula.compatibility import compatibility_data
from stock_desk.formula.models import Formula, FormulaDraft, FormulaVersion
from stock_desk.formula.repository import (
    FormulaConflict,
    FormulaCursorError,
    FormulaNotFound,
    FormulaRepositoryError,
    FormulaValidationError,
)
from stock_desk.formula.service import (
    FormulaPreviewNotFound,
    FormulaPreviewResourceError,
    FormulaPreviewTimeout,
    FormulaPreviewUnsupportedVersion,
    FormulaPreviewValidationError,
    FormulaPreviewWorkerError,
    FormulaService,
    FormulaServiceDatabaseMismatch,
)
from stock_desk.formula.signal_series import SignalSeries
from stock_desk.market.types import (
    Adjustment,
    BarQuery,
    CanonicalSymbol,
    Period,
    UtcDatetime,
    instrument_kind_for_symbol,
)


class FormulaErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: Annotated[str, Field(min_length=1, max_length=64)]


ParameterName = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]{0,63}$"),
]
MAX_SAFE_INTEGER = 2**53 - 1
ParameterDefault = (
    Annotated[StrictInt, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]
    | Annotated[StrictFloat, Field(allow_inf_nan=False)]
)
PreviewParameterValue = StrictInt | Annotated[StrictFloat, Field(allow_inf_nan=False)]


class FormulaIntegerParameterDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["integer"]
    default: Annotated[StrictInt, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]
    label: Annotated[str | None, Field(max_length=256)] = None
    description: Annotated[str | None, Field(max_length=1_024)] = None


class FormulaNumberParameterDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["number"]
    default: Annotated[StrictFloat, Field(allow_inf_nan=False)]
    label: Annotated[str | None, Field(max_length=256)] = None
    description: Annotated[str | None, Field(max_length=1_024)] = None

    @field_validator("default", mode="before")
    @classmethod
    def normalize_json_number(cls, value: object) -> object:
        if type(value) not in {int, float}:
            raise ValueError("number parameter default must be numeric")
        try:
            return float(cast(int | float, value))
        except OverflowError as error:
            raise ValueError("number parameter default must be finite") from error


class FormulaParameterDeclaration(
    RootModel[
        Annotated[
            FormulaIntegerParameterDeclaration | FormulaNumberParameterDeclaration,
            Field(discriminator="kind"),
        ]
    ]
):
    model_config = ConfigDict(frozen=True, strict=True)


class FormulaValidateParameterDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["integer", "number"]
    default: ParameterDefault
    label: Annotated[str | None, Field(max_length=256)] = None
    description: Annotated[str | None, Field(max_length=1_024)] = None

    @field_validator("default", mode="before")
    @classmethod
    def normalize_json_number(cls, value: object, info: ValidationInfo) -> object:
        if info.data.get("kind") == "integer":
            if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
                raise ValueError("integer parameter default is out of range")
            return value
        if info.data.get("kind") != "number":
            return value
        if type(value) not in {int, float}:
            raise ValueError("number parameter default must be numeric")
        try:
            return float(cast(int | float, value))
        except OverflowError as error:
            raise ValueError("number parameter default must be finite") from error


ParameterSchema = Annotated[
    dict[ParameterName, FormulaParameterDeclaration], Field(max_length=64)
]
ValidateParameterSchema = Annotated[
    dict[ParameterName, FormulaValidateParameterDeclaration], Field(max_length=64)
]


class FormulaPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    symbol: CanonicalSymbol
    period: Period
    adjustment: Adjustment
    start: UtcDatetime
    end: UtcDatetime
    parameters: dict[ParameterName, PreviewParameterValue] = Field(
        default_factory=dict, max_length=64
    )

    @field_validator("parameters", mode="before")
    @classmethod
    def reject_nonfinite_numeric_values(cls, value: object) -> object:
        if isinstance(value, Mapping):
            for item in value.values():
                if type(item) is int:
                    try:
                        if not math.isfinite(float(item)):
                            raise ValueError
                    except OverflowError as error:
                        raise ValueError(
                            "parameter override is out of range"
                        ) from error
        return value

    def query(self) -> BarQuery:
        return BarQuery(
            symbol=self.symbol,
            instrument_kind=instrument_kind_for_symbol(self.symbol),
            period=self.period,
            adjustment=self.adjustment,
            start=self.start,
            end=self.end,
        )


class FormulaMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: Annotated[str, Field(min_length=1, max_length=64_000)]
    parameter_schema: ParameterSchema = Field(default_factory=dict)


class FormulaValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: Annotated[str, Field(min_length=1, max_length=64_000)]
    parameter_schema: ValidateParameterSchema = Field(default_factory=dict)
    formula_type: Literal["indicator", "trading"]


class FormulaCreateRequest(FormulaMutationRequest):
    formula_type: Literal["indicator", "trading"]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    placement: Literal["main", "subchart"]


class FormulaDraftUpdateRequest(FormulaMutationRequest):
    expected_revision: Annotated[StrictInt, Field(gt=0)]


class FormulaSaveRequest(FormulaDraftUpdateRequest):
    pass


class FormulaCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Annotated[str, Field(min_length=1, max_length=64)]
    source_version_id: Annotated[str | None, Field(max_length=128)] = None


Checksum = Annotated[
    str,
    Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    ),
]


class FormulaDiagnosticSpanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    line: Annotated[int, Field(ge=1, le=64_001)]
    column: Annotated[int, Field(ge=1, le=64_001)]
    end_line: Annotated[int, Field(ge=1, le=64_001)]
    end_column: Annotated[int, Field(ge=1, le=64_001)]


class FormulaPublishedValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: Literal["validated"]
    compatibility_version: Annotated[str, Field(min_length=1, max_length=32)]
    engine_version: Annotated[str, Field(min_length=1, max_length=32)]
    parameter_schema_checksum: Checksum
    source_checksum: Checksum


class FormulaDiagnosticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: Annotated[str, Field(min_length=1, max_length=64)]
    function: Annotated[str | None, Field(max_length=64)]
    explanation: Annotated[str, Field(min_length=1, max_length=1_024)]
    span: FormulaDiagnosticSpanResponse
    blocks_preview: bool
    blocks_save: bool
    blocks_backtest: bool


class FormulaValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    valid: bool
    diagnostics: Annotated[tuple[FormulaDiagnosticResponse, ...], Field(max_length=64)]


class FormulaTemplateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    template_id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    formula_type: Literal["indicator", "trading"]
    placement: Literal["main", "subchart"]
    source: Annotated[str, Field(min_length=1, max_length=64_000)]
    parameter_schema: ParameterSchema


class FormulaTemplateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    items: Annotated[tuple[FormulaTemplateResponse, ...], Field(max_length=64)]


class FormulaDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    formula_id: Annotated[str, Field(min_length=1, max_length=128)]
    revision: Annotated[int, Field(ge=1)]
    source: Annotated[str, Field(min_length=1, max_length=64_000)]
    source_checksum: Checksum
    parameter_schema: ParameterSchema
    diagnostics: Annotated[
        tuple[FormulaDiagnosticResponse | FormulaPublishedValidationResponse, ...],
        Field(max_length=64),
    ]
    executable_version_id: Annotated[str | None, Field(max_length=128)]
    updated_at: datetime


class FormulaSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    formula_type: Literal["indicator", "trading"]
    placement: Literal["main", "subchart"]
    latest_version: Annotated[int, Field(ge=0)]
    created_at: datetime
    updated_at: datetime


class FormulaDetailResponse(FormulaSummaryResponse):
    draft: FormulaDraftResponse


class FormulaListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    items: Annotated[tuple[FormulaSummaryResponse, ...], Field(max_length=100)]
    next_cursor: Annotated[str | None, Field(max_length=128)]


class FormulaVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Annotated[str, Field(min_length=1, max_length=128)]
    formula_id: Annotated[str, Field(min_length=1, max_length=128)]
    version: Annotated[int, Field(ge=1)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    formula_type: Literal["indicator", "trading"]
    placement: Literal["main", "subchart"]
    source: Annotated[str, Field(min_length=1, max_length=64_000)]
    parameter_schema: ParameterSchema
    compatibility_version: Annotated[str, Field(min_length=1, max_length=32)]
    engine_version: Annotated[str, Field(min_length=1, max_length=32)]
    checksum: Checksum
    validation_result: Annotated[
        tuple[FormulaPublishedValidationResponse, ...], Field(max_length=64)
    ]
    copied_from_version_id: Annotated[str | None, Field(max_length=128)]
    created_at: datetime


class FormulaVersionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    items: Annotated[tuple[FormulaVersionResponse, ...], Field(max_length=100)]
    next_cursor: Annotated[str | None, Field(max_length=128)]


ValueKind = Literal["scalar", "integer_scalar", "number_series", "boolean_series"]


class FormulaFunctionParameterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: ParameterName
    accepted_kinds: Annotated[list[ValueKind], Field(min_length=1, max_length=4)]
    required: bool
    constant: bool
    minimum: int | None
    maximum: int | None
    constraints_zh: Annotated[str, Field(max_length=1_024)]


class FormulaFunctionRelationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    left: ParameterName
    operator: Literal["<=", "<", ">=", ">", "=="]
    right: ParameterName


class FormulaFunctionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: Literal["math", "logic", "series", "statistics", "signal"]
    dispatch_key: Annotated[str, Field(min_length=1, max_length=128)]
    future_behavior: Literal["current_only", "past_only", "future", "repainting"]
    max_args: Annotated[int, Field(ge=0, le=16)]
    min_args: Annotated[int, Field(ge=0, le=16)]
    name: ParameterName
    parameters: Annotated[list[FormulaFunctionParameterResponse], Field(max_length=16)]
    result_kind: Literal["number_series", "boolean_series"]
    relations: Annotated[list[FormulaFunctionRelationResponse], Field(max_length=16)]
    semantics_zh: Annotated[str, Field(min_length=1, max_length=2_048)]
    signature: Annotated[str, Field(min_length=1, max_length=256)]
    summary_zh: Annotated[str, Field(min_length=1, max_length=1_024)]


class FormulaFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canonical_name: ParameterName
    name: ParameterName
    scale_denominator: Annotated[int, Field(gt=0)]
    scale_numerator: Annotated[int, Field(gt=0)]
    source_name: ParameterName
    summary_zh: Annotated[str, Field(min_length=1, max_length=1_024)]
    unit: Literal["price", "shares", "hands"]
    value_type: Literal["number_series", "boolean_series"]


class FormulaParserLimitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    absolute_exponent: Annotated[int, Field(ge=0, le=1_000_000)]
    ast_nodes: Annotated[int, Field(ge=1, le=1_000_000)]
    identifier_chars: Annotated[int, Field(ge=1, le=1_024)]
    nesting_depth: Annotated[int, Field(ge=1, le=1_024)]
    numeric_literal_chars: Annotated[int, Field(ge=1, le=4_096)]
    source_bytes: Annotated[int, Field(ge=1, le=64_000)]
    statements: Annotated[int, Field(ge=1, le=4_096)]


class FormulaRuntimeSemanticsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    division_by_zero: Annotated[str, Field(min_length=1, max_length=1_024)]
    json_numbers: Annotated[str, Field(min_length=1, max_length=1_024)]
    null_propagation: Annotated[str, Field(min_length=1, max_length=1_024)]
    numeric_storage: Annotated[str, Field(min_length=1, max_length=64)]
    provenance: Annotated[str, Field(min_length=1, max_length=1_024)]


class FormulaValueKindHierarchyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    integer_scalar: Annotated[list[Literal["scalar"]], Field(max_length=4)]


class FormulaCompatibilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    compatibility_version: Annotated[str, Field(min_length=1, max_length=32)]
    official_reference: Annotated[str, Field(min_length=1, max_length=2_048)]
    fields: Annotated[list[FormulaFieldResponse], Field(max_length=64)]
    functions: Annotated[list[FormulaFunctionResponse], Field(max_length=128)]
    parser_limits: FormulaParserLimitsResponse
    runtime_semantics: FormulaRuntimeSemanticsResponse
    value_kind_hierarchy: FormulaValueKindHierarchyResponse


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": FormulaErrorResponse},
    status.HTTP_409_CONFLICT: {"model": FormulaErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": FormulaErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": FormulaErrorResponse},
    status.HTTP_504_GATEWAY_TIMEOUT: {"model": FormulaErrorResponse},
}


router = APIRouter(prefix="/formulas", tags=["formulas"])


def get_formula_service(request: Request) -> FormulaService:
    provider = cast(
        Callable[[], FormulaService],
        request.app.state.formula_service_provider,
    )
    return provider()


FormulaServiceDependency = Annotated[FormulaService, Depends(get_formula_service)]


def _parameter_schema_data(
    schema: Mapping[
        str, FormulaParameterDeclaration | FormulaValidateParameterDeclaration
    ],
) -> dict[str, object]:
    return {
        name: declaration.model_dump(exclude_unset=True)
        for name, declaration in schema.items()
    }


def _mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_mutable_json(item) for item in value)
    return value


def formula_error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code})


def formula_exception(error: Exception) -> JSONResponse:
    if isinstance(error, FormulaServiceDatabaseMismatch):
        return formula_error("storage_mismatch", status.HTTP_503_SERVICE_UNAVAILABLE)
    if isinstance(error, (FormulaNotFound, FormulaPreviewNotFound)):
        return formula_error("not_found", status.HTTP_404_NOT_FOUND)
    if isinstance(error, FormulaConflict):
        return formula_error("revision_conflict", status.HTTP_409_CONFLICT)
    if isinstance(error, FormulaCursorError):
        return formula_error("invalid_cursor", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if isinstance(error, FormulaValidationError):
        return formula_error("formula_invalid", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if isinstance(error, FormulaPreviewValidationError):
        return formula_error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if isinstance(error, FormulaPreviewUnsupportedVersion):
        return formula_error(
            "unsupported_formula_version", status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    if isinstance(error, FormulaPreviewResourceError):
        return formula_error(
            "resource_limit_exceeded", status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    if isinstance(error, FormulaPreviewTimeout):
        return formula_error("preview_timeout", status.HTTP_504_GATEWAY_TIMEOUT)
    if isinstance(error, FormulaPreviewWorkerError):
        return formula_error(
            "preview_worker_failed", status.HTTP_503_SERVICE_UNAVAILABLE
        )
    if isinstance(error, FormulaRepositoryError):
        return formula_error(
            "formula_storage_error", status.HTTP_503_SERVICE_UNAVAILABLE
        )
    return formula_error("internal_error", status.HTTP_503_SERVICE_UNAVAILABLE)


async def formula_service_database_mismatch_handler(
    _request: Request, error: Exception
) -> JSONResponse:
    return formula_exception(error)


def _draft_response(draft: FormulaDraft) -> dict[str, object]:
    return {
        "formula_id": draft.formula_id,
        "revision": draft.revision,
        "source": draft.source,
        "source_checksum": draft.source_checksum,
        "parameter_schema": _mutable_json(draft.parameter_schema),
        "diagnostics": _mutable_json(draft.validation_result),
        "executable_version_id": draft.executable_version_id,
        "updated_at": draft.updated_at,
    }


def _summary_response(formula: Formula) -> dict[str, object]:
    return {
        "id": formula.id,
        "name": formula.name,
        "formula_type": formula.formula_type,
        "placement": formula.placement,
        "latest_version": formula.latest_version,
        "created_at": formula.created_at,
        "updated_at": formula.updated_at,
    }


def _detail_response(formula: Formula, draft: FormulaDraft) -> dict[str, object]:
    return {**_summary_response(formula), "draft": _draft_response(draft)}


def _version_response(version: FormulaVersion) -> dict[str, object]:
    return {
        "id": version.id,
        "formula_id": version.formula_id,
        "version": version.version,
        "name": version.name,
        "formula_type": version.formula_type,
        "placement": version.placement,
        "source": version.source,
        "parameter_schema": _mutable_json(version.parameter_schema),
        "compatibility_version": version.compatibility_version,
        "engine_version": version.engine_version,
        "checksum": version.checksum,
        "validation_result": _mutable_json(version.validation_result),
        "copied_from_version_id": version.copied_from_version_id,
        "created_at": version.created_at,
    }


@router.get("/functions", response_model=FormulaCompatibilityResponse)
def list_functions() -> dict[str, object]:
    return compatibility_data()


@router.get(
    "/templates",
    response_model=FormulaTemplateListResponse,
    responses=_ERROR_RESPONSES,
)
def list_templates(service: FormulaServiceDependency) -> dict[str, object]:
    return {"items": service.templates()}


@router.post(
    "/validate",
    response_model=FormulaValidationResponse,
    responses=_ERROR_RESPONSES,
)
def validate_formula(
    payload: FormulaValidateRequest,
    service: FormulaServiceDependency,
) -> dict[str, object]:
    diagnostics = service.validate(
        source=payload.source,
        parameter_schema=_parameter_schema_data(payload.parameter_schema),
        formula_type=payload.formula_type,
    )
    return {"valid": not diagnostics, "diagnostics": diagnostics}


@router.get("", response_model=FormulaListResponse, responses=_ERROR_RESPONSES)
def list_formulas(
    service: FormulaServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> dict[str, object] | JSONResponse:
    try:
        formulas, next_cursor = service.list_formula_page(limit=limit, cursor=cursor)
        return {
            "items": tuple(_summary_response(formula) for formula in formulas),
            "next_cursor": next_cursor,
        }
    except Exception as error:
        return formula_exception(error)


@router.post(
    "",
    response_model=FormulaDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
def create_formula(
    payload: FormulaCreateRequest,
    service: FormulaServiceDependency,
) -> dict[str, object] | JSONResponse:
    try:
        version = service.create(
            name=payload.name,
            formula_type=payload.formula_type,
            placement=payload.placement,
            source=payload.source,
            parameter_schema=_parameter_schema_data(payload.parameter_schema),
        )
        formula, draft = service.get_formula(version.formula_id)
        return _detail_response(formula, draft)
    except Exception as error:
        return formula_exception(error)


@router.get(
    "/{formula_id}",
    response_model=FormulaDetailResponse,
    responses=_ERROR_RESPONSES,
)
def get_formula(
    formula_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: FormulaServiceDependency,
) -> dict[str, object] | JSONResponse:
    try:
        formula, draft = service.get_formula(formula_id)
        return _detail_response(formula, draft)
    except Exception as error:
        return formula_exception(error)


@router.put(
    "/{formula_id}/draft",
    response_model=FormulaDraftResponse,
    responses=_ERROR_RESPONSES,
)
def update_formula_draft(
    formula_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: FormulaDraftUpdateRequest,
    service: FormulaServiceDependency,
) -> dict[str, object] | JSONResponse:
    try:
        draft = service.update_draft(
            formula_id,
            source=payload.source,
            parameter_schema=_parameter_schema_data(payload.parameter_schema),
            expected_revision=payload.expected_revision,
        )
        return _draft_response(draft)
    except Exception as error:
        return formula_exception(error)


@router.post(
    "/{formula_id}/save",
    response_model=FormulaVersionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
def save_formula(
    formula_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: FormulaSaveRequest,
    service: FormulaServiceDependency,
) -> dict[str, object] | JSONResponse:
    try:
        version = service.save(
            formula_id,
            source=payload.source,
            parameter_schema=_parameter_schema_data(payload.parameter_schema),
            expected_revision=payload.expected_revision,
        )
        return _version_response(version)
    except Exception as error:
        return formula_exception(error)


@router.post(
    "/{formula_id}/copy",
    response_model=FormulaVersionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
def copy_formula(
    formula_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: FormulaCopyRequest,
    service: FormulaServiceDependency,
) -> dict[str, object] | JSONResponse:
    try:
        version = service.copy(
            formula_id,
            name=payload.name,
            source_version_id=payload.source_version_id,
        )
        return _version_response(version)
    except Exception as error:
        return formula_exception(error)


@router.get(
    "/{formula_id}/versions",
    response_model=FormulaVersionListResponse,
    responses=_ERROR_RESPONSES,
)
def list_formula_versions(
    formula_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: FormulaServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cursor: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> dict[str, object] | JSONResponse:
    try:
        versions, next_cursor = service.list_version_page(
            formula_id, limit=limit, cursor=cursor
        )
        return {
            "items": tuple(_version_response(version) for version in versions),
            "next_cursor": next_cursor,
        }
    except Exception as error:
        return formula_exception(error)


@router.post(
    "/{version_id}/preview",
    response_model=SignalSeries,
    responses=_ERROR_RESPONSES,
)
def preview_formula(
    version_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: FormulaPreviewRequest,
    service: FormulaServiceDependency,
) -> SignalSeries | JSONResponse:
    try:
        return service.preview(version_id, payload.query(), payload.parameters)
    except Exception as error:
        return formula_exception(error)
