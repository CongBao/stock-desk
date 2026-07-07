from __future__ import annotations

import asyncio
import math
from typing import Any, ClassVar, cast

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stock_desk.analysis.model_config import (
    HostResolver,
    MODEL_API_KEY_SECRET_NAME,
    ModelHostResolutionError,
    ModelProviderKind,
    ModelResolvedEndpointError,
    system_host_resolver,
    validate_resolved_remote_url,
    validate_provider_url,
)
from stock_desk.analysis.providers.base import (
    borrowed_transport,
    connection_failure,
    decode_provider_response_json,
    ModelAuthenticationError,
    ModelConnectionResult,
    ModelDNSResolutionError,
    ModelInvalidResponseError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelSecretReader,
    ModelServerError,
    ModelTimeoutError,
    ModelTransportError,
    ModelUnsafeEndpointError,
    ModelUsage,
    raise_for_status,
    validate_model_name,
)
from stock_desk.security.redaction import scoped_log_redaction


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class _WireMessage(_WireModel):
    content: str


class _WireChoice(_WireModel):
    message: _WireMessage


class _WireUsage(_WireModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class _WireCompletion(_WireModel):
    model: str = Field(min_length=1, max_length=256)
    choices: list[_WireChoice] = Field(min_length=1, max_length=16)
    usage: _WireUsage


class _WireModelEntry(_WireModel):
    id: str = Field(min_length=1, max_length=256)


class _WireModels(_WireModel):
    data: list[_WireModelEntry]


class OpenAICompatibleProvider:
    provider: ClassVar[str] = ModelProviderKind.OPENAI_COMPATIBLE.value

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        secret_store: ModelSecretReader,
        secret_name: str = MODEL_API_KEY_SECRET_NAME,
        transport: httpx2.AsyncBaseTransport | None = None,
        resolver: HostResolver = system_host_resolver,
    ) -> None:
        self._base_url = validate_provider_url(
            ModelProviderKind.OPENAI_COMPATIBLE,
            base_url,
        ).rstrip("/")
        self.model = validate_model_name(model)
        self._secret_store = secret_store
        self._secret_name = secret_name
        self._transport = borrowed_transport(transport)
        self._resolver = resolver

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, "
            f"base_url={self._base_url!r}, credentials=configured)"
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        snapshot = request.stable_snapshot()
        data_blocks, output_schema = snapshot.structured_parts()
        body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": snapshot.system},
                {
                    "role": "user",
                    "content": _compact_json({"data_blocks": data_blocks}),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_response",
                    "strict": True,
                    "schema": output_schema,
                },
            },
            "temperature": snapshot.temperature,
            "max_tokens": snapshot.max_output_tokens,
        }
        payload = await self._request_json(
            "POST",
            f"{self._base_url}/chat/completions",
            body=body,
            timeout_seconds=snapshot.timeout_seconds,
        )
        try:
            wire = _WireCompletion.model_validate(payload)
            content = _decode_content(wire.choices[0].message.content)
            return ModelResponse(
                provider=self.provider,
                model=wire.model,
                content=content,
                usage=ModelUsage(
                    input_tokens=wire.usage.prompt_tokens,
                    output_tokens=wire.usage.completion_tokens,
                    total_tokens=wire.usage.total_tokens,
                ),
            )
        except (IndexError, ValidationError, ValueError, TypeError):
            raise ModelInvalidResponseError() from None

    async def test_connection(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> ModelConnectionResult:
        try:
            payload = await self._request_json(
                "GET",
                f"{self._base_url}/models",
                body=None,
                timeout_seconds=_validate_timeout(timeout_seconds),
            )
            models = _WireModels.model_validate(payload)
            if not any(entry.id == self.model for entry in models.data):
                raise ValueError
        except (ModelProviderError, ValidationError, ValueError, TypeError) as error:
            safe_error = (
                error
                if isinstance(error, ModelProviderError)
                else ModelInvalidResponseError()
            )
            return connection_failure(
                provider=self.provider,
                model=self.model,
                error=safe_error,
            )
        return ModelConnectionResult(
            connected=True,
            provider=self.provider,
            model=self.model,
            error_code=None,
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, object] | None,
        timeout_seconds: float,
    ) -> object:
        try:
            api_key = self._secret_store.read_secret_for_server_call(self._secret_name)
            if (
                not isinstance(api_key, str)
                or len(api_key) < 4
                or len(api_key) > 4_096
                or api_key != api_key.strip()
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in api_key
                )
            ):
                raise ValueError
        except Exception:
            raise ModelAuthenticationError() from None
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        response: httpx2.Response | None = None
        request_error: ModelProviderError | None = None
        try:
            async with asyncio.timeout(timeout_seconds):
                (
                    pinned_url,
                    original_authority,
                    sni_hostname,
                ) = await _validate_remote_endpoint(url, self._resolver)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                with scoped_log_redaction(api_key):
                    async with httpx2.AsyncClient(
                        transport=self._transport,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        response = await client.request(
                            method,
                            pinned_url,
                            headers={**headers, "Host": original_authority},
                            json=body,
                            timeout=remaining,
                            extensions={"sni_hostname": sni_hostname},
                        )
        except (TimeoutError, httpx2.TimeoutException):
            request_error = ModelTimeoutError()
        except httpx2.RequestError:
            request_error = ModelTransportError()
        if request_error is not None:
            raise request_error from None
        if response is None:
            raise ModelServerError()
        raise_for_status(response.status_code)
        return decode_provider_response_json(response.content)


def _compact_json(value: object) -> str:
    import json

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_content(value: str) -> dict[str, Any]:
    import json

    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError
    return cast(dict[str, Any], decoded)


def _validate_timeout(value: float) -> float:
    if (
        not isinstance(value, float)
        or not math.isfinite(value)
        or value < 0.01
        or value > 300.0
    ):
        raise ValueError("connection timeout is invalid")
    return value


async def _validate_remote_endpoint(
    url: str, resolver: HostResolver
) -> tuple[httpx2.URL, str, str]:
    resolution_error: ModelProviderError | None = None
    addresses: tuple[str, ...] = ()
    try:
        addresses = await validate_resolved_remote_url(url, resolver)
    except ModelHostResolutionError:
        resolution_error = ModelDNSResolutionError()
    except ModelResolvedEndpointError:
        resolution_error = ModelUnsafeEndpointError()
    if resolution_error is not None:
        raise resolution_error from None
    original = httpx2.URL(url)
    if not addresses or not original.host or not original.netloc:
        raise ModelDNSResolutionError()
    return (
        original.copy_with(host=addresses[0]),
        original.netloc.decode("ascii"),
        original.host,
    )
