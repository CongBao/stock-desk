from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
import hashlib
import hmac
import json
import re
from typing import Annotated, Any, Literal, Self, cast

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from stock_desk.analysis.model_catalog import (
    ModelConfigListKey,
    ModelConfigStatus,
    ModelNotFound,
    ModelNotVerified,
)
from stock_desk.analysis.model_config import ModelConfigUpdate, ModelProviderKind
from stock_desk.analysis.model_settings import (
    ConnectionTestResult,
    ModelSettingsConflict,
    ModelSettingsPage,
    ModelSettingsSecureStorageError,
    ModelSettingsService,
    ModelSettingsSnapshot,
    ModelSettingsStorageError,
    ModelSettingsValidationError,
)
from stock_desk.analysis.providers.base import ModelErrorCode


_CONFIG_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"
_CONFIG_ID = re.compile(_CONFIG_ID_PATTERN)
_CURSOR_VERSION = 1
_CURSOR_COLLECTION = "models"
_CURSOR_MAX_CHARS = 1_024
_CURSOR_CHECKSUM_CONTEXT = b"stock-desk:model-settings-cursor:v1\x00"
_MODEL_ERROR_CODES = frozenset(value.value for value in ModelErrorCode)


class _ModelSettingsDTO(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class ModelSettingsErrorResponse(_ModelSettingsDTO):
    code: Annotated[str, Field(min_length=1, max_length=64)]


class _ModelSettingsWriteRequest(_ModelSettingsDTO):
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    provider: Literal["deepseek", "openai_compatible", "ollama"]
    base_url: Annotated[str | None, Field(max_length=2_048)] = None
    model: Annotated[str, Field(min_length=1, max_length=256)]
    api_key: SecretStr | None = Field(
        default_factory=lambda: None,
        min_length=4,
        max_length=4_096,
        exclude=True,
        repr=False,
        json_schema_extra={"writeOnly": True},
    )
    temperature: StrictFloat = Field(default=0.1, ge=0.0, le=2.0)
    timeout: StrictFloat = Field(default=90.0, ge=1.0, le=300.0)
    max_output: StrictInt = Field(default=4_096, ge=1, le=65_536)

    @field_validator("temperature", "timeout", mode="before")
    @classmethod
    def require_json_float(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("model runtime value must be a float")
        return value

    @field_validator("max_output", mode="before")
    @classmethod
    def require_json_int(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("model output limit must be an integer")
        return value

    @field_validator("display_name", "model")
    @classmethod
    def validate_bounded_text(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("model settings text is invalid")
        return value

    def to_update(self) -> ModelConfigUpdate:
        return ModelConfigUpdate(
            provider=ModelProviderKind(self.provider),
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            temperature=self.temperature,
            timeout_seconds=self.timeout,
            max_output_tokens=self.max_output,
        )


class ModelSettingsCreateRequest(_ModelSettingsWriteRequest):
    @model_validator(mode="after")
    def require_remote_key(self) -> ModelSettingsCreateRequest:
        if self.provider != ModelProviderKind.OLLAMA.value and self.api_key is None:
            raise ValueError("remote model settings require an API key")
        return self


class ModelSettingsUpdateRequest(_ModelSettingsWriteRequest):
    pass


class ModelSettingsRevisionRequest(_ModelSettingsDTO):
    expected_revision: StrictInt = Field(ge=0, le=2**63 - 1)


class ModelSettingsResponse(_ModelSettingsDTO):
    id: Annotated[str, Field(pattern=_CONFIG_ID_PATTERN)]
    public_config_hash: Annotated[str, Field(pattern=_CONFIG_ID_PATTERN)]
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    provider: ModelProviderKind
    base_url: Annotated[str, Field(min_length=1, max_length=2_048)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    temperature: StrictFloat
    timeout: StrictFloat
    max_output: StrictInt
    api_key_configured: bool
    masked_api_key: Annotated[str | None, Field(max_length=64)]
    status: ModelConfigStatus
    revision: StrictInt = Field(ge=0)
    verified_at: AwareDatetime | None
    last_tested_at: AwareDatetime | None
    error_code: Annotated[
        str | None, Field(max_length=64, pattern=r"^[a-z0-9_]{1,64}$")
    ]
    supersedes_id: Annotated[str | None, Field(pattern=_CONFIG_ID_PATTERN)]
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_snapshot_invariants(self) -> Self:
        valid_identity = (
            self.id == self.public_config_hash and self.supersedes_id != self.id
        )
        valid_mask = self.api_key_configured == (
            self.masked_api_key is not None
        ) and _valid_mask(self.masked_api_key)
        valid_provider_credentials = (
            self.provider is ModelProviderKind.OLLAMA and not self.api_key_configured
        ) or (self.provider is not ModelProviderKind.OLLAMA and self.api_key_configured)
        valid_state = _valid_response_state_shape(
            self.status,
            verified_at=self.verified_at,
            last_tested_at=self.last_tested_at,
            error_code=self.error_code,
        )
        event_times = tuple(
            value
            for value in (self.verified_at, self.last_tested_at)
            if value is not None
        )
        valid_times = self.created_at <= self.updated_at and all(
            self.created_at <= value <= self.updated_at for value in event_times
        )
        if not (
            valid_identity
            and valid_mask
            and valid_provider_credentials
            and valid_state
            and valid_times
        ):
            raise ValueError("model settings response is invalid")
        return self

    @classmethod
    def from_snapshot(cls, snapshot: ModelSettingsSnapshot) -> ModelSettingsResponse:
        try:
            return cls(
                id=snapshot.id,
                public_config_hash=snapshot.public_config_hash,
                display_name=snapshot.display_name,
                provider=snapshot.provider,
                base_url=snapshot.base_url,
                model=snapshot.model,
                temperature=snapshot.temperature,
                timeout=snapshot.timeout_seconds,
                max_output=snapshot.max_output_tokens,
                api_key_configured=snapshot.api_key_configured,
                masked_api_key=snapshot.masked_api_key,
                status=snapshot.status,
                revision=snapshot.revision,
                verified_at=_optional_aware(snapshot.verified_at),
                last_tested_at=_optional_aware(snapshot.last_tested_at),
                error_code=snapshot.error_code,
                supersedes_id=snapshot.supersedes_id,
                created_at=_required_aware(snapshot.created_at),
                updated_at=_required_aware(snapshot.updated_at),
            )
        except ValidationError:
            raise ModelSettingsStorageError() from None


class ModelSettingsListResponse(_ModelSettingsDTO):
    items: Annotated[tuple[ModelSettingsResponse, ...], Field(max_length=100)]
    next_cursor: Annotated[str | None, Field(max_length=_CURSOR_MAX_CHARS)]


class ModelConnectionTestResponse(_ModelSettingsDTO):
    config_id: Annotated[str, Field(pattern=_CONFIG_ID_PATTERN)]
    connected: bool
    provider: ModelProviderKind
    model: Annotated[str, Field(min_length=1, max_length=256)]
    error_code: Annotated[
        str | None, Field(max_length=64, pattern=r"^[a-z0-9_]{1,64}$")
    ]
    status: ModelConfigStatus
    revision: StrictInt = Field(ge=0)
    tested_at: AwareDatetime
    last_tested_at: AwareDatetime

    @model_validator(mode="after")
    def validate_connection_invariants(self) -> Self:
        if (
            (self.error_code is not None and self.error_code not in _MODEL_ERROR_CODES)
            or self.connected != (self.error_code is None)
            or self.connected != (self.status is ModelConfigStatus.VERIFIED)
            or (not self.connected) != (self.status is ModelConfigStatus.FAILED)
            or self.tested_at != self.last_tested_at
        ):
            raise ValueError("model connection response is invalid")
        return self

    @classmethod
    def from_result(cls, result: ConnectionTestResult) -> ModelConnectionTestResponse:
        try:
            return cls(
                config_id=result.config_id,
                connected=result.connected,
                provider=result.provider,
                model=result.model,
                error_code=(
                    None if result.error_code is None else result.error_code.value
                ),
                status=result.status,
                revision=result.revision,
                tested_at=_required_aware(result.tested_at),
                last_tested_at=_required_aware(result.last_tested_at),
            )
        except (AttributeError, ValidationError):
            raise ModelSettingsStorageError() from None


class ModelSettingsCursorError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("Model settings cursor is invalid")


class ModelSettingsDatabaseMismatch(ModelSettingsStorageError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("Model settings storage does not match the application")


def _required_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ModelSettingsStorageError()
    return value


def _optional_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _required_aware(value)


def _valid_mask(value: str | None) -> bool:
    if value is None:
        return True
    if value in {"•••••••", "[MASKED]"}:
        return True
    return len(value) == 15 and value[4:11] == "•••••••"


def _valid_response_state_shape(
    status_value: ModelConfigStatus,
    *,
    verified_at: datetime | None,
    last_tested_at: datetime | None,
    error_code: str | None,
) -> bool:
    if status_value is ModelConfigStatus.UNVERIFIED:
        return verified_at is None and last_tested_at is None and error_code is None
    if status_value is ModelConfigStatus.VERIFIED:
        return (
            verified_at is not None
            and last_tested_at == verified_at
            and error_code is None
        )
    if status_value is ModelConfigStatus.FAILED:
        return (
            verified_at is None
            and last_tested_at is not None
            and error_code is not None
        )
    return error_code is None and (
        verified_at is None
        or (last_tested_at is not None and last_tested_at == verified_at)
    )


def _error(code: str, status_code: int) -> JSONResponse:
    response = ModelSettingsErrorResponse(code=code)
    return JSONResponse(
        status_code=status_code, content=response.model_dump(mode="json")
    )


def model_settings_exception(error: Exception) -> JSONResponse:
    if isinstance(error, ModelNotFound):
        return _error("not_found", status.HTTP_404_NOT_FOUND)
    if isinstance(error, ModelNotVerified):
        return _error("model_not_verified", status.HTTP_409_CONFLICT)
    if isinstance(error, ModelSettingsConflict):
        return _error("state_conflict", status.HTTP_409_CONFLICT)
    if isinstance(error, ModelSettingsCursorError):
        return _error("invalid_cursor", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if isinstance(error, (ModelSettingsValidationError, ValidationError)):
        return _error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)
    if isinstance(error, ModelSettingsSecureStorageError):
        return _error("secure_storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    if isinstance(error, ModelSettingsStorageError):
        return _error("storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)
    return _error("storage_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE)


class _SafeModelSettingsRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Any]:
        route_handler = super().get_route_handler()

        async def safe_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except RequestValidationError:
                return _error("invalid_request", status.HTTP_422_UNPROCESSABLE_CONTENT)
            except Exception as error:
                return model_settings_exception(error)

        return safe_route_handler


def get_model_settings_service(request: Request) -> ModelSettingsService:
    provider = getattr(request.app.state, "model_settings_services_provider", None)
    if not callable(provider):
        raise ModelSettingsDatabaseMismatch()
    provider = cast(Callable[[], ModelSettingsService], provider)
    service = provider()
    identity_provider = getattr(request.app.state, "database_identity_provider", None)
    expected_identity = (
        identity_provider()
        if callable(identity_provider)
        else getattr(request.app.state, "database_identity", None)
    )
    service_identity = getattr(service, "database_identity", None)
    if (
        expected_identity is None
        or service_identity is None
        or service_identity != expected_identity
    ):
        raise ModelSettingsDatabaseMismatch()
    return service


ModelSettingsServiceDependency = Annotated[
    ModelSettingsService, Depends(get_model_settings_service)
]


def get_model_settings_cursor_key(request: Request) -> bytes:
    key = getattr(request.app.state, "model_settings_cursor_key", None)
    if type(key) is not bytes or len(key) != 32:
        raise ModelSettingsStorageError()
    return key


ModelSettingsCursorKeyDependency = Annotated[
    bytes, Depends(get_model_settings_cursor_key)
]
ConfigIdPath = Annotated[
    str,
    Path(
        min_length=71,
        max_length=71,
        pattern=_CONFIG_ID_PATTERN,
    ),
]


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ModelSettingsErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ModelSettingsErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ModelSettingsErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ModelSettingsErrorResponse},
}


router = APIRouter(
    prefix="/settings/models",
    tags=["model-settings"],
    responses=_ERROR_RESPONSES,
    route_class=_SafeModelSettingsRoute,
)


@router.post(
    "",
    response_model=ModelSettingsResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_model_settings(
    body: ModelSettingsCreateRequest,
    service: ModelSettingsServiceDependency,
) -> ModelSettingsResponse:
    return ModelSettingsResponse.from_snapshot(
        service.create(body.display_name, body.to_update())
    )


@router.get("", response_model=ModelSettingsListResponse)
def list_model_settings(
    service: ModelSettingsServiceDependency,
    cursor_key: ModelSettingsCursorKeyDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    include_disabled: Annotated[bool, Query()] = False,
) -> ModelSettingsListResponse:
    after = _decode_cursor(
        cursor,
        include_disabled=include_disabled,
        cursor_key=cursor_key,
    )
    page: ModelSettingsPage = service.list_page(
        limit=limit,
        after=after,
        include_disabled=include_disabled,
    )
    return ModelSettingsListResponse(
        items=tuple(ModelSettingsResponse.from_snapshot(item) for item in page.items),
        next_cursor=(
            None
            if page.next_key is None
            else _encode_cursor(
                page.next_key,
                include_disabled=include_disabled,
                cursor_key=cursor_key,
            )
        ),
    )


@router.get("/{config_id}", response_model=ModelSettingsResponse)
def get_model_settings(
    config_id: ConfigIdPath,
    service: ModelSettingsServiceDependency,
) -> ModelSettingsResponse:
    return ModelSettingsResponse.from_snapshot(service.get(config_id))


@router.put("/{config_id}", response_model=ModelSettingsResponse)
def update_model_settings(
    config_id: ConfigIdPath,
    body: ModelSettingsUpdateRequest,
    service: ModelSettingsServiceDependency,
) -> ModelSettingsResponse:
    return ModelSettingsResponse.from_snapshot(
        service.create_successor(
            config_id,
            body.display_name,
            body.to_update(),
        )
    )


@router.post("/{config_id}/test", response_model=ModelConnectionTestResponse)
async def test_model_settings(
    config_id: ConfigIdPath,
    body: ModelSettingsRevisionRequest,
    service: ModelSettingsServiceDependency,
) -> ModelConnectionTestResponse:
    result = await service.test_connection(
        config_id, expected_revision=body.expected_revision
    )
    return ModelConnectionTestResponse.from_result(result)


@router.post("/{config_id}/disable", response_model=ModelSettingsResponse)
def disable_model_settings(
    config_id: ConfigIdPath,
    body: ModelSettingsRevisionRequest,
    service: ModelSettingsServiceDependency,
) -> ModelSettingsResponse:
    return ModelSettingsResponse.from_snapshot(
        service.disable(config_id, expected_revision=body.expected_revision)
    )


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _cursor_checksum(body: dict[str, object], cursor_key: bytes) -> str:
    _validate_cursor_key(cursor_key)
    return hmac.new(
        cursor_key,
        _CURSOR_CHECKSUM_CONTEXT + _canonical_json(body),
        hashlib.sha256,
    ).hexdigest()


def _encode_cursor(
    key: ModelConfigListKey,
    *,
    include_disabled: bool,
    cursor_key: bytes,
) -> str:
    _validate_cursor_key(cursor_key)
    if (
        not isinstance(key, ModelConfigListKey)
        or _CONFIG_ID.fullmatch(key.id) is None
        or type(include_disabled) is not bool
    ):
        raise ModelSettingsCursorError()
    body: dict[str, object] = {
        "collection": _CURSOR_COLLECTION,
        "id": key.id,
        "include_disabled": include_disabled,
        "version": _CURSOR_VERSION,
    }
    payload = {**body, "checksum": _cursor_checksum(body, cursor_key)}
    token = base64.urlsafe_b64encode(_canonical_json(payload)).decode("ascii")
    token = token.rstrip("=")
    if len(token) > _CURSOR_MAX_CHARS:
        raise ModelSettingsCursorError()
    return token


def _decode_cursor(
    token: str | None,
    *,
    include_disabled: bool,
    cursor_key: bytes,
) -> ModelConfigListKey | None:
    _validate_cursor_key(cursor_key)
    if token is None:
        return None
    if (
        type(token) is not str
        or not token
        or len(token) > _CURSOR_MAX_CHARS
        or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None
        or type(include_disabled) is not bool
    ):
        raise ModelSettingsCursorError()
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.b64decode(
            token + padding,
            altchars=b"-_",
            validate=True,
        )
        if len(decoded) > _CURSOR_MAX_CHARS:
            raise ValueError
        pairs = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ModelSettingsCursorError() from None
    if type(pairs) is not dict:
        raise ModelSettingsCursorError()
    payload = cast(dict[str, object], pairs)
    if set(payload) != {
        "checksum",
        "collection",
        "id",
        "include_disabled",
        "version",
    }:
        raise ModelSettingsCursorError()
    checksum = payload.pop("checksum")
    config_id = payload.get("id")
    if (
        type(checksum) is not str
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        or payload.get("collection") != _CURSOR_COLLECTION
        or type(payload.get("version")) is not int
        or payload.get("version") != _CURSOR_VERSION
        or type(payload.get("include_disabled")) is not bool
        or payload.get("include_disabled") is not include_disabled
        or type(config_id) is not str
        or _CONFIG_ID.fullmatch(config_id) is None
        or not hmac.compare_digest(checksum, _cursor_checksum(payload, cursor_key))
    ):
        raise ModelSettingsCursorError()
    canonical_payload = {**payload, "checksum": checksum}
    canonical_token = (
        base64.urlsafe_b64encode(_canonical_json(canonical_payload))
        .decode("ascii")
        .rstrip("=")
    )
    if not hmac.compare_digest(token, canonical_token):
        raise ModelSettingsCursorError()
    return ModelConfigListKey(id=config_id)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate cursor field")
        value[key] = item
    return value


def _validate_cursor_key(cursor_key: bytes) -> None:
    if type(cursor_key) is not bytes or len(cursor_key) != 32:
        raise ModelSettingsStorageError()


__all__ = [
    "ModelConnectionTestResponse",
    "ModelSettingsCreateRequest",
    "ModelSettingsErrorResponse",
    "ModelSettingsListResponse",
    "ModelSettingsResponse",
    "ModelSettingsRevisionRequest",
    "ModelSettingsUpdateRequest",
    "get_model_settings_service",
    "get_model_settings_cursor_key",
    "model_settings_exception",
    "router",
]
