from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from itertools import count
import json
import math
import threading
from typing import cast, Final, Protocol, runtime_checkable

import httpx2

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    field_serializer,
    field_validator,
    model_validator,
)


MAX_MODEL_JSON_BYTES: Final = 262_144
MAX_MODEL_JSON_DEPTH: Final = 32
MAX_MODEL_JSON_NODES: Final = 10_000
MAX_PROVIDER_RESPONSE_BYTES: Final = 1_048_576
MAX_PROVIDER_RESPONSE_DEPTH: Final = 32
MAX_PROVIDER_RESPONSE_NODES: Final = 20_000
MAX_SECRET_READER_THREADS: Final = 4
_SECRET_READER_SLOTS = threading.BoundedSemaphore(MAX_SECRET_READER_THREADS)
_SECRET_READER_SEQUENCE = count(1)


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
    TRANSPORT = "transport"
    DNS = "dns"
    UNSAFE_ENDPOINT = "unsafe_endpoint"
    INVALID_RESPONSE = "invalid_response"
    STORAGE = "storage"


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
        _canonical_request_json(self.data_blocks, self.output_schema)
        return self

    def stable_snapshot(self) -> ModelRequestSnapshot:
        return ModelRequestSnapshot(
            system=self.system,
            structured_json=_canonical_request_json(
                self.data_blocks,
                self.output_schema,
            ),
            temperature=self.temperature,
            timeout_seconds=self.timeout_seconds,
            max_output_tokens=self.max_output_tokens,
        )

    def stable_hash(self) -> str:
        snapshot = self.stable_snapshot()
        data_blocks, output_schema = snapshot.structured_parts()
        encoded = json.dumps(
            {
                "system": snapshot.system,
                "data_blocks": data_blocks,
                "output_schema": output_schema,
                "temperature": snapshot.temperature,
                "timeout_seconds": snapshot.timeout_seconds,
                "max_output_tokens": snapshot.max_output_tokens,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class ModelResponse(_FrozenModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        serialize_by_alias=True,
        strict=True,
    )

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    usage: ModelUsage
    content_json: bytes = Field(
        validation_alias=AliasChoices("content", "content_json"),
        serialization_alias="content",
        repr=False,
        json_schema_extra=lambda schema: _content_object_schema(schema),
    )

    @field_validator("content_json", mode="before")
    @classmethod
    def freeze_content(cls, value: object) -> bytes:
        if isinstance(value, (bytes, bytearray)):
            try:
                value = json.loads(bytes(value))
            except (UnicodeDecodeError, ValueError, TypeError):
                raise ValueError("model response content is invalid") from None
        if not isinstance(value, dict):
            raise ValueError("model response content must be an object")
        return _canonical_response_content(value)

    @field_serializer("content_json")
    def serialize_content(self, value: bytes) -> dict[str, JsonValue]:
        return _decoded_response_object(value)

    @property
    def content(self) -> dict[str, JsonValue]:
        return _decoded_response_object(self.content_json)


@dataclass(frozen=True, slots=True)
class ModelRequestSnapshot:
    system: str
    structured_json: bytes
    temperature: float
    timeout_seconds: float
    max_output_tokens: int

    def structured_parts(
        self,
    ) -> tuple[list[dict[str, JsonValue]], dict[str, JsonValue]]:
        decoded = cast(dict[str, object], json.loads(self.structured_json))
        return (
            cast(list[dict[str, JsonValue]], decoded["data_blocks"]),
            cast(dict[str, JsonValue], decoded["output_schema"]),
        )


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


class ModelCredentialUnavailableError(ModelProviderError):
    code = ModelErrorCode.STORAGE
    safe_message = "model provider credentials are unavailable"


class ModelRateLimitError(ModelProviderError):
    code = ModelErrorCode.RATE_LIMIT
    safe_message = "model provider rate limit was reached"


class ModelServerError(ModelProviderError):
    code = ModelErrorCode.SERVER
    safe_message = "model provider is unavailable"


class ModelTransportError(ModelProviderError):
    code = ModelErrorCode.TRANSPORT
    safe_message = "model provider transport failed"


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


async def read_model_secret_with_deadline(
    reader: ModelSecretReader,
    name: str,
    *,
    timeout_seconds: float,
) -> str:
    """Run a synchronous credential read in one bounded daemon worker."""
    if not _SECRET_READER_SLOTS.acquire(blocking=False):
        raise ModelCredentialUnavailableError()
    future: Future[str] = Future()

    def invoke() -> None:
        try:
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(reader.read_secret_for_server_call(name))
                except BaseException as error:
                    future.set_exception(error)
        finally:
            _SECRET_READER_SLOTS.release()

    worker = threading.Thread(
        target=invoke,
        name=f"model-secret-reader-{next(_SECRET_READER_SEQUENCE)}",
        daemon=True,
    )
    try:
        worker.start()
    except BaseException:
        _SECRET_READER_SLOTS.release()
        raise ModelCredentialUnavailableError() from None
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not future.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ModelTimeoutError()
        await asyncio.sleep(min(0.005, remaining))
    try:
        return future.result()
    except BaseException:
        raise ModelCredentialUnavailableError() from None


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


class BorrowedAsyncTransport(httpx2.AsyncBaseTransport):
    """Delegate requests without closing the caller-owned transport."""

    def __init__(self, transport: httpx2.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(
        self,
        request: httpx2.Request,
    ) -> httpx2.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        return None


def borrowed_transport(
    transport: httpx2.AsyncBaseTransport | None,
) -> httpx2.AsyncBaseTransport | None:
    return BorrowedAsyncTransport(transport) if transport is not None else None


def decode_provider_response_json(content: bytes) -> object:
    if len(content) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ModelInvalidResponseError()
    decoded: object | None = None
    try:
        decoded = json.loads(content)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        pass
    if decoded is None:
        raise ModelInvalidResponseError() from None
    try:
        _validate_json_shape(
            (decoded,),
            max_depth=MAX_PROVIDER_RESPONSE_DEPTH,
            max_nodes=MAX_PROVIDER_RESPONSE_NODES,
            label="model provider response",
        )
    except ValueError:
        raise ModelInvalidResponseError() from None
    return decoded


async def read_bounded_provider_response(response: httpx2.Response) -> bytes:
    """Read an identity-encoded provider response without unbounded buffering."""
    content_encoding = response.headers.get("content-encoding")
    if content_encoding is not None and content_encoding.strip().lower() not in {
        "",
        "identity",
    }:
        raise ModelInvalidResponseError()
    content_length = response.headers.get("content-length")
    if content_length is not None:
        if (
            not content_length
            or not content_length.isascii()
            or not content_length.isdecimal()
            or len(content_length) > 20
        ):
            raise ModelInvalidResponseError()
        try:
            declared_length = int(content_length)
        except ValueError:
            raise ModelInvalidResponseError() from None
        if declared_length > MAX_PROVIDER_RESPONSE_BYTES:
            raise ModelInvalidResponseError()
    if response.is_stream_consumed:
        content = response.content
        if len(content) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ModelInvalidResponseError()
        return content
    payload = bytearray()
    async for chunk in response.aiter_raw():
        remaining = MAX_PROVIDER_RESPONSE_BYTES + 1 - len(payload)
        payload.extend(chunk[:remaining])
        if len(payload) > MAX_PROVIDER_RESPONSE_BYTES or len(chunk) > remaining:
            raise ModelInvalidResponseError()
    return bytes(payload)


def _canonical_request_json(
    data_blocks: object,
    output_schema: object,
) -> bytes:
    _validate_json_shape(
        (data_blocks, output_schema),
        max_depth=MAX_MODEL_JSON_DEPTH,
        max_nodes=MAX_MODEL_JSON_NODES,
        label="model request JSON",
    )
    encoded: bytes | None = None
    try:
        encoded = json.dumps(
            {
                "data_blocks": data_blocks,
                "output_schema": output_schema,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        pass
    if encoded is None:
        raise ValueError("model request JSON is invalid") from None
    if len(encoded) > MAX_MODEL_JSON_BYTES:
        raise ValueError("model request JSON exceeds the byte limit")
    return encoded


def _canonical_response_content(content: object) -> bytes:
    _validate_json_shape(
        (content,),
        max_depth=MAX_PROVIDER_RESPONSE_DEPTH,
        max_nodes=MAX_PROVIDER_RESPONSE_NODES,
        label="model response content",
    )
    encoded: bytes | None = None
    try:
        encoded = json.dumps(
            content,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        pass
    if encoded is None or len(encoded) > MAX_PROVIDER_RESPONSE_BYTES:
        raise ValueError("model response content exceeds its budget") from None
    return encoded


def _decoded_response_object(value: bytes) -> dict[str, JsonValue]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("model response content is invalid")
    return cast(dict[str, JsonValue], decoded)


def _content_object_schema(schema: dict[str, JsonValue]) -> None:
    schema.pop("format", None)
    schema["type"] = "object"
    schema["additionalProperties"] = True


def _validate_json_shape(
    roots: tuple[object, ...],
    *,
    max_depth: int,
    max_nodes: int,
    label: str,
) -> None:
    stack = [(root, 1) for root in roots]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        if depth > max_depth:
            raise ValueError(f"{label} exceeds the depth limit")
        node_count += 1
        if node_count > max_nodes:
            raise ValueError(f"{label} exceeds the node limit")
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend((child, depth + 1) for child in value)
