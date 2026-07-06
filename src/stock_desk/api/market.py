from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import math
from pathlib import Path
import re
from threading import Lock
from typing import Annotated, Any, Callable, Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    Path as ApiPath,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    WithJsonSchema,
)
from sqlalchemy import Engine

from stock_desk.api.formulas import (
    FormulaErrorResponse,
    formula_exception,
    get_formula_service,
)
from stock_desk.formula.service import FormulaService
from stock_desk.api.tasks import RepositoryDependency, TaskResponse
from stock_desk.formula.signal_series import SignalSeries
from stock_desk.market.instruments import (
    InstrumentCorruption,
    InstrumentManifestSnapshot,
    InstrumentNotFound,
    InstrumentRepository,
    InstrumentSearchResult,
    InstrumentValidationError,
)
from stock_desk.market.lake import (
    MarketLake,
    MarketLakeCorruptionError,
    manifest_record_id,
)
from stock_desk.market.provenance import RoutingManifest, Sha256Digest
from stock_desk.market.scheduler import (
    MARKET_UPDATE_TIMEZONE,
    MarketUpdateScheduleNotFound,
    MarketUpdateScheduleRepository,
    MarketUpdateScheduleSnapshot,
    MarketUpdateScheduleStorageError,
    MarketUpdateScheduleValidationError,
    next_due_at,
)
from stock_desk.market.pools import (
    CustomPoolSummary,
    CustomPoolState,
    PoolConflict,
    PoolComposition,
    PoolCategory,
    PoolCorruption,
    PoolItemValidationError,
    PoolName,
    PoolNotFound,
    PoolRepository,
    PoolRepositoryError,
    PoolRevisionConflict,
    PoolValidationError,
    PresetPool,
    PresetPoolSummary,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    CanonicalSymbol,
    Exchange,
    InstrumentKind,
    ListingStatus,
    MAX_BAR_SERIES_ROWS,
    Period,
    Provenance,
    ProviderId,
    UtcDatetime,
)
from stock_desk.market.update import (
    MARKET_UPDATE_TASK_KIND,
    MARKET_CATALOG_UPDATE_TASK_KIND,
    MarketUpdateItemConflict,
    MarketUpdateItemNotFound,
    MarketUpdateItemRepository,
    MarketUpdateItemSnapshot,
    MarketUpdateItemStorageError,
    MarketUpdateRequest,
)
from stock_desk.storage.database import DatabaseIdentity, create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskValidationError


class MarketServices:
    """One-engine service bundle for cached market reads and pool persistence."""

    def __init__(self, *, engine: Engine, lake_root: Path) -> None:
        root = Path(lake_root)
        if not root.is_absolute():
            raise ValueError("market lake root must be absolute")
        self.engine = engine
        self.lake_root = root
        self.instruments = InstrumentRepository(engine)
        self.pools = PoolRepository(engine)
        self.lake = MarketLake(engine=engine, root=root)
        self.update_items = MarketUpdateItemRepository(engine)
        self.schedules = MarketUpdateScheduleRepository(engine)
        identities = (
            self.instruments.database_identity,
            self.pools.database_identity,
            self.lake.database_identity,
            self.update_items.database_identity,
            self.schedules.database_identity,
        )
        if identities[1:] != identities[:-1]:
            raise ValueError("market services database identities do not match")
        self._database_identity = identities[0]
        self._close_lock = Lock()
        self._closed = False

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    @classmethod
    def open(cls, *, database_url: str, lake_root: Path) -> MarketServices:
        migrate(database_url)
        engine = create_engine_for_url(database_url)
        try:
            return cls(engine=engine, lake_root=lake_root)
        except BaseException:
            engine.dispose()
            raise

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.engine.dispose()


RawPoolSymbol = Annotated[
    str,
    Field(strict=True, max_length=64),
]

_RFC3339_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


def _validated_rfc3339(value: str) -> str:
    if _RFC3339_PATTERN.fullmatch(value) is None:
        raise ValueError("timestamp must be timezone-qualified RFC3339")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as error:
        raise ValueError("timestamp must be timezone-qualified RFC3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-qualified RFC3339")
    return value


Rfc3339Timestamp = Annotated[
    str,
    Field(strict=True, min_length=20, max_length=40),
    AfterValidator(_validated_rfc3339),
    WithJsonSchema(
        {
            "type": "string",
            "format": "date-time",
            "pattern": _RFC3339_PATTERN.pattern,
            "minLength": 20,
            "maxLength": 40,
        }
    ),
]


def _parse_rfc3339(value: Rfc3339Timestamp) -> datetime:
    parsed = datetime.fromisoformat(
        value[:-1] + "+00:00" if value.endswith("Z") else value
    )
    return parsed.astimezone(timezone.utc)


class CustomPoolCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: PoolName
    symbols: list[RawPoolSymbol] = Field(min_length=1, max_length=5_000)


class CustomPoolUpdateRequest(CustomPoolCreateRequest):
    expected_revision: Annotated[StrictInt, Field(gt=0)]


class _MarketDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MarketIssueResponse(_MarketDTO):
    ordinal: Annotated[int, Field(ge=0)]
    code: Annotated[str, Field(min_length=1, max_length=64)]


class MarketErrorResponse(_MarketDTO):
    code: Annotated[str, Field(min_length=1, max_length=64)]
    issues: tuple[MarketIssueResponse, ...] | None = None


class CatalogProvenanceResponse(_MarketDTO):
    manifest_record_id: Sha256Digest
    dataset_version: Sha256Digest
    route_version: Sha256Digest
    source: ProviderId
    fetched_at: UtcDatetime
    data_cutoff: UtcDatetime
    routing_manifest: RoutingManifest


class InstrumentResponse(_MarketDTO):
    symbol: CanonicalSymbol
    name: str
    exchange: Exchange
    instrument_kind: InstrumentKind
    listing_status: ListingStatus
    listed_on: date | None
    delisted_on: date | None
    provenance: CatalogProvenanceResponse


class PoolSummaryProvenanceResponse(CatalogProvenanceResponse):
    instrument_dataset_version: Sha256Digest


class PresetPoolProvenanceResponse(PoolSummaryProvenanceResponse):
    composition: PoolComposition


class PoolMemberResponse(_MarketDTO):
    ordinal: Annotated[int, Field(ge=0)]
    symbol: CanonicalSymbol
    name: str
    instrument_kind: InstrumentKind
    listing_status: ListingStatus


class PresetPoolSummaryResponse(_MarketDTO):
    pool_id: str
    kind: Literal["preset"]
    name: str
    category: PoolCategory
    revision: None
    member_count: Annotated[int, Field(ge=1, le=10_000)]
    snapshot_id: Sha256Digest
    provenance: PoolSummaryProvenanceResponse


class CustomPoolSummaryResponse(_MarketDTO):
    pool_id: str
    kind: Literal["custom"]
    name: str
    category: None
    revision: Annotated[int, Field(gt=0)]
    member_count: Annotated[int, Field(ge=1, le=5_000)]
    snapshot_id: None
    provenance: PoolSummaryProvenanceResponse


PoolSummaryResponse = Annotated[
    PresetPoolSummaryResponse | CustomPoolSummaryResponse,
    Field(discriminator="kind"),
]


class PresetPoolDetailResponse(PresetPoolSummaryResponse):
    provenance: PresetPoolProvenanceResponse
    members: tuple[PoolMemberResponse, ...]


class CustomPoolDetailResponse(CustomPoolSummaryResponse):
    members: tuple[PoolMemberResponse, ...]


PoolDetailResponse = Annotated[
    PresetPoolDetailResponse | CustomPoolDetailResponse,
    Field(discriminator="kind"),
]


class PoolListPageResponse(_MarketDTO):
    items: tuple[PoolSummaryResponse, ...]
    next_cursor: str | None


class CoverageResponse(_MarketDTO):
    start: UtcDatetime
    end: UtcDatetime


class CachedBarsResponse(_MarketDTO):
    query: BarQuery
    bars: Annotated[tuple[Bar, ...], Field(max_length=MAX_BAR_SERIES_ROWS)]
    coverage: CoverageResponse
    manifest_record_id: Sha256Digest
    dataset_version: Sha256Digest
    route_version: Sha256Digest
    routing_manifest: RoutingManifest
    provenance: Provenance


class CachedBarsFormulaResponse(CachedBarsResponse):
    formula: SignalSeries


class MarketUpdateRequestDTO(_MarketDTO):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    symbols: Annotated[
        list[CanonicalSymbol],
        Field(min_length=1, max_length=10_000),
    ]
    period: Period
    adjustment: Adjustment
    start: Rfc3339Timestamp
    end: Rfc3339Timestamp

    def to_domain(self) -> MarketUpdateRequest:
        return MarketUpdateRequest(
            symbols=tuple(self.symbols),
            period=self.period,
            adjustment=self.adjustment,
            start=_parse_rfc3339(self.start),
            end=_parse_rfc3339(self.end),
        )


class MarketUpdateItemResponse(_MarketDTO):
    task_id: str
    ordinal: Annotated[int, Field(ge=0)]
    symbol: CanonicalSymbol
    status: Literal["succeeded", "failed", "cancelled"]
    manifest_record_id: str | None
    dataset_version: str | None
    reason: str | None
    created_at: UtcDatetime


class DailyScheduleRequest(_MarketDTO):
    enabled: bool
    local_time: Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    payload: MarketUpdateRequestDTO

    def parsed_time(self) -> time:
        return time.fromisoformat(self.local_time)


class DailyScheduleResponse(_MarketDTO):
    id: str
    enabled: bool
    timezone: Literal["Asia/Shanghai"]
    local_time: str
    payload: MarketUpdateRequestDTO
    symbols_frozen: Literal[True]
    last_enqueued_local_date: date | None
    next_due_at: UtcDatetime | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


_MARKET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": MarketErrorResponse},
    status.HTTP_409_CONFLICT: {"model": MarketErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": MarketErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": MarketErrorResponse},
}
_BAR_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_MARKET_ERROR_RESPONSES,
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": FormulaErrorResponse},
    status.HTTP_504_GATEWAY_TIMEOUT: {"model": FormulaErrorResponse},
}
_DAILY_SCHEDULE_ID = "00000000-0000-0000-0000-000000000001"


def _error(
    code: str,
    status_code: int,
    *,
    issues: list[dict[str, object]] | None = None,
) -> JSONResponse:
    validated = MarketErrorResponse(
        code=code,
        issues=(
            tuple(MarketIssueResponse.model_validate(issue) for issue in issues)
            if issues is not None
            else None
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content=validated.model_dump(mode="json", exclude_none=True),
    )


def _invalid() -> JSONResponse:
    return _error(
        "invalid_request",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        issues=[],
    )


def _update_item_response(item: MarketUpdateItemSnapshot) -> dict[str, object]:
    return {
        "task_id": item.task_id,
        "ordinal": item.ordinal,
        "symbol": item.symbol,
        "status": item.status,
        "manifest_record_id": item.manifest_record_id,
        "dataset_version": item.dataset_version,
        "reason": item.reason,
        "created_at": item.created_at,
    }


def _schedule_response(
    schedule: MarketUpdateScheduleSnapshot,
) -> dict[str, object]:
    return {
        "id": schedule.id,
        "enabled": schedule.enabled,
        "timezone": schedule.timezone,
        "local_time": schedule.local_time.strftime("%H:%M"),
        "payload": dict(schedule.payload),
        "symbols_frozen": True,
        "last_enqueued_local_date": schedule.last_enqueued_local_date,
        "next_due_at": next_due_at(schedule),
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


async def market_request_validation_handler(
    request: Request,
    error: Exception,
) -> Response:
    if not isinstance(error, RequestValidationError):
        raise error
    if (
        request.url.path == "/api/market"
        or request.url.path.startswith("/api/market/")
        or request.url.path == "/api/settings"
        or request.url.path.startswith("/api/settings/")
    ):
        return _invalid()
    if request.url.path == "/api/formulas" or request.url.path.startswith(
        "/api/formulas/"
    ):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"code": "invalid_request"},
        )
    return await request_validation_exception_handler(request, error)


def get_market_services(request: Request) -> MarketServices:
    provider = cast(
        Callable[[], MarketServices],
        request.app.state.market_services_provider,
    )
    return provider()


MarketServicesDependency = Annotated[MarketServices, Depends(get_market_services)]


def _catalog_provenance(snapshot: InstrumentManifestSnapshot) -> dict[str, object]:
    return {
        "manifest_record_id": snapshot.manifest_record_id,
        "dataset_version": snapshot.dataset_version,
        "route_version": snapshot.route_version,
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at,
        "data_cutoff": snapshot.data_cutoff,
        "routing_manifest": snapshot.manifest,
    }


def _instrument_response(result: InstrumentSearchResult) -> dict[str, object]:
    item = result.instrument
    return {
        "symbol": item.symbol,
        "name": item.name,
        "exchange": item.exchange,
        "instrument_kind": item.instrument_kind,
        "listing_status": item.listing_status,
        "listed_on": item.listed_on,
        "delisted_on": item.delisted_on,
        "provenance": _catalog_provenance(result.manifest),
    }


def _pool_provenance(
    services: MarketServices,
    manifest_record_id_value: str,
) -> dict[str, object]:
    catalog = services.instruments.pinned_catalog(manifest_record_id_value)
    return _catalog_provenance(catalog.manifest)


def _pool_summary_provenance(
    services: MarketServices,
    manifest_record_id_value: str,
) -> dict[str, object]:
    return _catalog_provenance(
        services.instruments.pinned_manifest(manifest_record_id_value)
    )


def _preset_summary_response(
    services: MarketServices,
    pool: PresetPoolSummary,
) -> dict[str, object]:
    return {
        "pool_id": pool.pool_id,
        "kind": "preset",
        "name": pool.name,
        "category": pool.category,
        "revision": None,
        "member_count": pool.member_count,
        "snapshot_id": pool.snapshot_id,
        "provenance": {
            **_pool_summary_provenance(
                services,
                pool.instrument_manifest_record_id,
            ),
            "instrument_dataset_version": pool.instrument_dataset_version,
        },
    }


def _custom_summary_response(
    services: MarketServices,
    pool: CustomPoolSummary,
) -> dict[str, object]:
    return {
        "pool_id": pool.pool_id,
        "kind": "custom",
        "name": pool.name,
        "category": None,
        "revision": pool.revision,
        "member_count": pool.member_count,
        "snapshot_id": None,
        "provenance": {
            **_pool_summary_provenance(
                services,
                pool.instrument_manifest_record_id,
            ),
            "instrument_dataset_version": pool.instrument_dataset_version,
        },
    }


def _preset_response(
    services: MarketServices,
    pool: PresetPool,
    *,
    include_members: bool,
) -> dict[str, object]:
    response: dict[str, object] = {
        "pool_id": pool.pool_id,
        "kind": "preset",
        "name": pool.composition.display_name,
        "category": pool.composition.category,
        "revision": None,
        "member_count": len(pool.members),
        "snapshot_id": pool.snapshot_id,
        "provenance": {
            **_pool_provenance(
                services,
                pool.instrument_manifest_record_id,
            ),
            "composition": pool.composition,
            "instrument_dataset_version": pool.instrument_dataset_version,
        },
    }
    if include_members:
        response["members"] = tuple(
            {
                "ordinal": member.ordinal,
                "symbol": member.instrument.symbol,
                "name": member.instrument.name,
                "instrument_kind": member.instrument.instrument_kind,
                "listing_status": member.instrument.listing_status,
            }
            for member in pool.members
        )
    return response


def _custom_response(
    services: MarketServices,
    pool: CustomPoolState,
    *,
    include_members: bool,
) -> dict[str, object]:
    response: dict[str, object] = {
        "pool_id": pool.pool_id,
        "kind": "custom",
        "name": pool.name,
        "category": None,
        "revision": pool.revision,
        "member_count": len(pool.members),
        "snapshot_id": None,
        "provenance": {
            **_pool_provenance(
                services,
                pool.instrument_manifest_record_id,
            ),
            "instrument_dataset_version": pool.instrument_dataset_version,
        },
    }
    if include_members:
        response["members"] = tuple(
            {
                "ordinal": member.ordinal,
                "symbol": member.instrument.symbol,
                "name": member.instrument.name,
                "instrument_kind": member.instrument.instrument_kind,
                "listing_status": member.instrument.listing_status,
            }
            for member in pool.members
        )
    return response


def _pool_exception(error: PoolRepositoryError) -> JSONResponse:
    if isinstance(error, PoolItemValidationError):
        return _error(
            "invalid_pool_members",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            issues=[
                {"ordinal": issue.ordinal, "code": issue.code.value}
                for issue in error.issues
            ],
        )
    if isinstance(error, PoolRevisionConflict):
        return _error("revision_conflict", status.HTTP_409_CONFLICT)
    if isinstance(error, PoolNotFound):
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    if isinstance(error, PoolValidationError):
        return _invalid()
    if isinstance(error, PoolConflict):
        return _error("conflict", status.HTTP_409_CONFLICT)
    return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


router = APIRouter(prefix="/market", tags=["market"])


@router.get(
    "/instruments",
    response_model=list[InstrumentResponse],
    responses=_MARKET_ERROR_RESPONSES,
)
def search_instruments(
    services: MarketServicesDependency,
    q: Annotated[str, Query(min_length=1, max_length=64)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, object]] | JSONResponse:
    try:
        results = services.instruments.search(q, limit=limit)
        return [_instrument_response(result) for result in results]
    except InstrumentValidationError:
        return _invalid()
    except InstrumentNotFound:
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    except InstrumentCorruption:
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get(
    "/instruments/{symbol}",
    response_model=InstrumentResponse,
    responses=_MARKET_ERROR_RESPONSES,
)
def get_instrument(
    symbol: Annotated[CanonicalSymbol, ApiPath()],
    services: MarketServicesDependency,
) -> dict[str, object] | JSONResponse:
    try:
        return _instrument_response(services.instruments.get(symbol))
    except InstrumentValidationError:
        return _invalid()
    except InstrumentNotFound:
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    except InstrumentCorruption:
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get(
    "/pools",
    response_model=PoolListPageResponse,
    responses=_MARKET_ERROR_RESPONSES,
)
def list_pools(
    services: MarketServicesDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=71)] = None,
) -> dict[str, object] | JSONResponse:
    try:
        presets = [
            _preset_summary_response(services, pool)
            for pool in services.pools.list_preset_summaries(
                limit=limit,
                after=cursor,
            )
        ]
        customs = [
            _custom_summary_response(services, pool)
            for pool in services.pools.list_custom_summaries(
                limit=limit,
                after=cursor,
            )
        ]
        candidates = sorted(
            [*presets, *customs],
            key=lambda item: cast(str, item["pool_id"]),
        )
        items = candidates[:limit]
        next_cursor = None
        if items:
            last_pool_id = cast(str, items[-1]["pool_id"])
            has_more = len(candidates) > limit
            if not has_more and len(candidates) == limit:
                has_more = bool(
                    services.pools.list_preset_summaries(
                        limit=1,
                        after=last_pool_id,
                    )
                    or services.pools.list_custom_summaries(
                        limit=1,
                        after=last_pool_id,
                    )
                )
            if has_more:
                next_cursor = last_pool_id
        return {"items": tuple(items), "next_cursor": next_cursor}
    except PoolValidationError:
        return _invalid()
    except (PoolCorruption, InstrumentCorruption):
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get(
    "/pools/{pool_id}",
    response_model=PoolDetailResponse,
    responses=_MARKET_ERROR_RESPONSES,
)
def get_pool(
    pool_id: Annotated[str, ApiPath(min_length=1, max_length=71)],
    services: MarketServicesDependency,
) -> dict[str, object] | JSONResponse:
    try:
        if pool_id.startswith("preset:"):
            preset_pool = services.pools.get_preset(pool_id.removeprefix("preset:"))
            return _preset_response(services, preset_pool, include_members=True)
        custom_pool = services.pools.get_custom(pool_id)
        return _custom_response(services, custom_pool, include_members=True)
    except PoolRepositoryError as error:
        return _pool_exception(error)
    except InstrumentCorruption:
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post(
    "/pools",
    status_code=status.HTTP_201_CREATED,
    response_model=CustomPoolDetailResponse,
    responses=_MARKET_ERROR_RESPONSES,
)
def create_pool(
    body: CustomPoolCreateRequest,
    services: MarketServicesDependency,
) -> dict[str, object] | JSONResponse:
    try:
        pool = services.pools.create_custom(name=body.name, symbols=body.symbols)
        return _custom_response(services, pool, include_members=True)
    except PoolRepositoryError as error:
        return _pool_exception(error)
    except InstrumentNotFound:
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    except (PoolCorruption, InstrumentCorruption):
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.put(
    "/pools/{pool_id}",
    response_model=CustomPoolDetailResponse,
    responses=_MARKET_ERROR_RESPONSES,
)
def update_pool(
    pool_id: Annotated[str, ApiPath(min_length=1, max_length=71)],
    body: CustomPoolUpdateRequest,
    services: MarketServicesDependency,
) -> dict[str, object] | JSONResponse:
    if pool_id.startswith("preset:"):
        return _error(
            "preset_read_only",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            issues=[],
        )
    try:
        pool = services.pools.update_custom(
            pool_id,
            expected_revision=body.expected_revision,
            name=body.name,
            symbols=body.symbols,
        )
        return _custom_response(services, pool, include_members=True)
    except PoolRepositoryError as error:
        return _pool_exception(error)
    except InstrumentNotFound:
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    except (PoolCorruption, InstrumentCorruption):
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.delete(
    "/pools/{pool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_MARKET_ERROR_RESPONSES,
)
def delete_pool(
    pool_id: Annotated[str, ApiPath(min_length=1, max_length=71)],
    services: MarketServicesDependency,
    expected_revision: Annotated[int, Query(gt=0)],
) -> Response:
    if pool_id.startswith("preset:"):
        return _error(
            "preset_read_only",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            issues=[],
        )
    try:
        services.pools.delete_custom(pool_id, expected_revision=expected_revision)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except PoolRepositoryError as error:
        return _pool_exception(error)


def _bar_lookup(
    services: MarketServices,
    formula_service: FormulaService | None,
    *,
    symbol: CanonicalSymbol,
    period: Period,
    adjustment: Adjustment,
    start: Rfc3339Timestamp | None,
    end: Rfc3339Timestamp | None,
    formula_version_id: str | None,
    formula_parameters: str | None,
) -> dict[str, object] | JSONResponse:
    try:
        if (start is None) != (end is None):
            return _invalid()
        if start is None:
            routed = services.lake.read_latest_series(
                symbol,
                period,
                adjustment,
            )
        else:
            assert end is not None
            query = BarQuery(
                symbol=symbol,
                period=period,
                adjustment=adjustment,
                start=_parse_rfc3339(start),
                end=_parse_rfc3339(end),
            )
            routed = services.lake.read_latest_exact(query)
    except (ValidationError, ValueError, TypeError):
        return _invalid()
    except MarketLakeCorruptionError:
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)
    if routed is None:
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    result = routed.result
    response: dict[str, object] = {
        "query": result.query,
        "bars": result.bars,
        "coverage": {
            "start": result.coverage_start,
            "end": result.coverage_end,
        },
        "manifest_record_id": manifest_record_id(routed.manifest),
        "dataset_version": result.provenance.dataset_version,
        "route_version": routed.manifest.route_version,
        "routing_manifest": routed.manifest,
        "provenance": result.provenance,
    }
    if formula_version_id is not None:
        if formula_service is None:
            return formula_exception(RuntimeError("formula service is unavailable"))
        parameter_values: dict[str, int | float] = {}
        try:
            if formula_parameters is not None:
                raw_parameters = json.loads(formula_parameters)
                if (
                    not isinstance(raw_parameters, dict)
                    or len(raw_parameters) > 64
                    or any(type(name) is not str for name in raw_parameters)
                    or any(
                        type(value) not in {int, float}
                        or (type(value) is int and abs(value) > 2**53)
                        or (type(value) is float and not math.isfinite(value))
                        for value in raw_parameters.values()
                    )
                ):
                    return _invalid()
                parameter_values = cast(dict[str, int | float], raw_parameters)
        except (UnicodeError, ValueError):
            return _invalid()
        try:
            response["formula"] = formula_service.preview_routed(
                formula_version_id, routed, parameter_values
            )
        except Exception as error:
            return formula_exception(error)
    return response


@router.get(
    "/bars",
    response_model=CachedBarsFormulaResponse | CachedBarsResponse,
    responses=_BAR_ERROR_RESPONSES,
)
def get_bars(
    request: Request,
    services: MarketServicesDependency,
    symbol: Annotated[CanonicalSymbol, Query()],
    period: Annotated[Period, Query()],
    adjustment: Annotated[Adjustment, Query()],
    start: Annotated[Rfc3339Timestamp | None, Query()] = None,
    end: Annotated[Rfc3339Timestamp | None, Query()] = None,
    formula_version_id: Annotated[
        str | None, Query(min_length=1, max_length=128)
    ] = None,
    formula_parameters: Annotated[str | None, Query(max_length=8_192)] = None,
) -> dict[str, object] | JSONResponse:
    if formula_parameters is not None and formula_version_id is None:
        return _invalid()
    formula_service = None
    if formula_version_id is not None:
        try:
            formula_service = get_formula_service(request)
        except Exception as error:
            return formula_exception(error)
    return _bar_lookup(
        services,
        formula_service,
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        start=start,
        end=end,
        formula_version_id=formula_version_id,
        formula_parameters=formula_parameters,
    )


@router.post(
    "/updates",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_MARKET_ERROR_RESPONSES,
)
def create_market_update(
    repository: RepositoryDependency,
    services: MarketServicesDependency,
    payload: MarketUpdateRequestDTO,
) -> TaskResponse | JSONResponse:
    try:
        if repository.database_identity != services.database_identity:
            return _error("storage_mismatch", status.HTTP_500_INTERNAL_SERVER_ERROR)
        request = payload.to_domain()
        task = repository.create(
            MARKET_UPDATE_TASK_KIND,
            request.model_dump(mode="json"),
        )
        return TaskResponse.from_snapshot(task)
    except (ValidationError, TypeError, ValueError, TaskValidationError):
        return _invalid()


@router.post(
    "/catalog/updates",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_MARKET_ERROR_RESPONSES,
)
def create_market_catalog_update(
    repository: RepositoryDependency,
    services: MarketServicesDependency,
) -> TaskResponse | JSONResponse:
    try:
        if repository.database_identity != services.database_identity:
            return _error("storage_mismatch", status.HTTP_500_INTERNAL_SERVER_ERROR)
        return TaskResponse.from_snapshot(
            repository.create(MARKET_CATALOG_UPDATE_TASK_KIND, {})
        )
    except TaskValidationError:
        return _invalid()


@router.get(
    "/updates/{task_id}/items",
    response_model=list[MarketUpdateItemResponse],
    responses=_MARKET_ERROR_RESPONSES,
)
def list_market_update_items(
    task_id: Annotated[str, ApiPath(min_length=1, max_length=64)],
    services: MarketServicesDependency,
) -> list[dict[str, object]] | JSONResponse:
    try:
        return [
            _update_item_response(item)
            for item in services.update_items.list_for_task(task_id)
        ]
    except MarketUpdateItemNotFound:
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    except MarketUpdateItemConflict:
        return _error("conflict", status.HTTP_409_CONFLICT)
    except MarketUpdateItemStorageError:
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get(
    "/schedules/daily",
    response_model=DailyScheduleResponse,
    responses=_MARKET_ERROR_RESPONSES,
)
def get_daily_market_schedule(
    services: MarketServicesDependency,
) -> dict[str, object] | JSONResponse:
    try:
        return _schedule_response(services.schedules.get(_DAILY_SCHEDULE_ID))
    except MarketUpdateScheduleNotFound:
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    except MarketUpdateScheduleStorageError:
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.put(
    "/schedules/daily",
    response_model=DailyScheduleResponse,
    responses=_MARKET_ERROR_RESPONSES,
)
def put_daily_market_schedule(
    body: DailyScheduleRequest,
    services: MarketServicesDependency,
) -> dict[str, object] | JSONResponse:
    try:
        schedule = services.schedules.replace(
            schedule_id=_DAILY_SCHEDULE_ID,
            local_time=body.parsed_time(),
            payload=body.payload.to_domain().model_dump(mode="json"),
            timezone=MARKET_UPDATE_TIMEZONE,
            enabled=body.enabled,
        )
        return _schedule_response(schedule)
    except (MarketUpdateScheduleValidationError, ValidationError, ValueError):
        return _invalid()
    except MarketUpdateScheduleStorageError:
        return _error("internal_error", status.HTTP_500_INTERNAL_SERVER_ERROR)
