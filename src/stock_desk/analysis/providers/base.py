from __future__ import annotations

from enum import StrEnum
import json
import math
from typing import Final, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    field_validator,
    model_validator,
)


MAX_MODEL_JSON_BYTES: Final = 262_144
MAX_MODEL_JSON_DEPTH: Final = 32
MAX_MODEL_JSON_NODES: Final = 10_000


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class ModelErrorCode(StrEnum):
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    DNS = "dns"
    UNSAFE_ENDPOINT = "unsafe_endpoint"
    INVALID_RESPONSE = "invalid_response"


class ModelUsage(_FrozenModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> ModelUsage:
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total token count is inconsistent")
        return self


class ModelRequest(_FrozenModel):
    system: str = Field(min_length=1, max_length=32_768)
    data_blocks: tuple[dict[str, JsonValue], ...] = Field(
        min_length=1,
        max_length=128,
    )
    output_schema: dict[str, JsonValue]
    temperature: StrictFloat = Field(default=0.1, ge=0.0, le=2.0)
    timeout_seconds: StrictFloat = Field(default=90.0, ge=1.0, le=300.0)
    max_output_tokens: int = Field(default=4_096, ge=1, le=65_536)

    @field_validator("system")
    @classmethod
    def validate_system(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) == 0 for character in value):
            raise ValueError("system instruction is invalid")
        return value

    @field_validator("temperature", "timeout_seconds", mode="before")
    @classmethod
    def validate_finite_number(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("model runtime value must be a float")
        assert isinstance(value, float)
        if not math.isfinite(value):
            raise ValueError("model runtime value must be finite")
        return value

    @field_validator("output_schema")
    @classmethod
    def validate_output_schema(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        if not value or value.get("type") != "object":
            raise ValueError("output schema must describe a JSON object")
        return value

    @model_validator(mode="after")
    def validate_json_budget(self) -> ModelRequest:
        _validate_json_shape((self.data_blocks, self.output_schema))
        encoded = json.dumps(
            {
                "data_blocks": self.data_blocks,
                "output_schema": self.output_schema,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_MODEL_JSON_BYTES:
            raise ValueError("model request JSON exceeds the byte limit")
        return self


class ModelResponse(_FrozenModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    content: dict[str, JsonValue]
    usage: ModelUsage


class ModelConnectionResult(_FrozenModel):
    connected: StrictBool
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    error_code: ModelErrorCode | None = None

    @model_validator(mode="after")
    def validate_error_state(self) -> ModelConnectionResult:
        if self.connected == (self.error_code is not None):
            raise ValueError("connection result state is inconsistent")
        return self


class ModelProviderError(RuntimeError):
    code = ModelErrorCode.INVALID_RESPONSE
    safe_message = "model provider response is invalid"

    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__(self.safe_message)


class ModelTimeoutError(ModelProviderError):
    code = ModelErrorCode.TIMEOUT
    safe_message = "model provider request timed out"


class ModelAuthenticationError(ModelProviderError):
    code = ModelErrorCode.AUTHENTICATION
    safe_message = "model provider authentication failed"


class ModelRateLimitError(ModelProviderError):
    code = ModelErrorCode.RATE_LIMIT
    safe_message = "model provider rate limit was reached"


class ModelServerError(ModelProviderError):
    code = ModelErrorCode.SERVER
    safe_message = "model provider is unavailable"


class ModelDNSResolutionError(ModelProviderError):
    code = ModelErrorCode.DNS
    safe_message = "model provider hostname could not be resolved"


class ModelUnsafeEndpointError(ModelProviderError):
    code = ModelErrorCode.UNSAFE_ENDPOINT
    safe_message = "model provider resolved to an unsafe endpoint"


class ModelInvalidResponseError(ModelProviderError):
    code = ModelErrorCode.INVALID_RESPONSE
    safe_message = "model provider response is invalid"


@runtime_checkable
class ModelSecretReader(Protocol):
    def read_secret_for_server_call(self, name: str) -> str: ...


@runtime_checkable
class ModelProvider(Protocol):
    provider: str
    model: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    async def test_connection(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> ModelConnectionResult: ...


def connection_failure(
    *, provider: str, model: str, error: ModelProviderError
) -> ModelConnectionResult:
    return ModelConnectionResult(
        connected=False,
        provider=provider,
        model=model,
        error_code=error.code,
    )


def raise_for_status(status_code: int) -> None:
    if status_code in {401, 403}:
        raise ModelAuthenticationError()
    if status_code == 429:
        raise ModelRateLimitError()
    if 500 <= status_code <= 599:
        raise ModelServerError()
    if status_code < 200 or status_code >= 300:
        raise ModelInvalidResponseError()


def validate_model_name(model: str) -> str:
    if (
        not isinstance(model, str)
        or not model
        or len(model) > 256
        or model != model.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in model)
    ):
        raise ValueError("model name is invalid")
    return model


def _validate_json_shape(roots: tuple[object, ...]) -> None:
    stack = [(root, 1) for root in roots]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        if depth > MAX_MODEL_JSON_DEPTH:
            raise ValueError("model request JSON exceeds the depth limit")
        node_count += 1
        if node_count > MAX_MODEL_JSON_NODES:
            raise ValueError("model request JSON exceeds the node limit")
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend((child, depth + 1) for child in value)
