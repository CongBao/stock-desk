from __future__ import annotations

from typing import Any, ClassVar, cast

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stock_desk.analysis.model_config import (
    ModelProviderKind,
    OLLAMA_BASE_URL,
    validate_provider_url,
)
from stock_desk.analysis.providers.base import (
    connection_failure,
    ModelConnectionResult,
    ModelInvalidResponseError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelServerError,
    ModelTimeoutError,
    ModelUsage,
    raise_for_status,
    validate_model_name,
)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class _WireMessage(_WireModel):
    content: str


class _WireCompletion(_WireModel):
    model: str = Field(min_length=1, max_length=256)
    message: _WireMessage
    prompt_eval_count: int = Field(ge=0)
    eval_count: int = Field(ge=0)
    done: bool


class _WireTag(_WireModel):
    name: str | None = None
    model: str | None = None


class _WireTags(_WireModel):
    models: list[_WireTag]


class OllamaProvider:
    provider: ClassVar[str] = ModelProviderKind.OLLAMA.value

    def __init__(
        self,
        *,
        model: str,
        base_url: str = OLLAMA_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = validate_provider_url(
            ModelProviderKind.OLLAMA,
            base_url,
        ).rstrip("/")
        self.model = validate_model_name(model)
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, base_url={self._base_url!r})"
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {
                    "role": "user",
                    "content": _compact_json({"data_blocks": request.data_blocks}),
                },
            ],
            "stream": False,
            "format": request.output_schema,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens,
            },
        }
        payload = await self._request_json(
            "POST",
            f"{self._base_url}/api/chat",
            body=body,
            timeout_seconds=request.timeout_seconds,
        )
        try:
            wire = _WireCompletion.model_validate(payload)
            if not wire.done:
                raise ValueError
            content = _decode_content(wire.message.content)
            return ModelResponse(
                provider=self.provider,
                model=wire.model,
                content=content,
                usage=ModelUsage(
                    input_tokens=wire.prompt_eval_count,
                    output_tokens=wire.eval_count,
                    total_tokens=wire.prompt_eval_count + wire.eval_count,
                ),
            )
        except (ValidationError, ValueError, TypeError):
            raise ModelInvalidResponseError() from None

    async def test_connection(
        self,
        *,
        timeout_seconds: float = 10.0,
    ) -> ModelConnectionResult:
        try:
            payload = await self._request_json(
                "GET",
                f"{self._base_url}/api/tags",
                body=None,
                timeout_seconds=_validate_timeout(timeout_seconds),
            )
            tags = _WireTags.model_validate(payload)
            if not any(
                self.model in {entry.name, entry.model} for entry in tags.models
            ):
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
            async with httpx2.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"Content-Type": "application/json"},
                    json=body,
                    timeout=timeout_seconds,
                )
        except httpx2.TimeoutException:
            raise ModelTimeoutError() from None
        except httpx2.RequestError:
            raise ModelServerError() from None
        raise_for_status(response.status_code)
        try:
            return cast(object, response.json())
        except (ValueError, TypeError):
            raise ModelInvalidResponseError() from None


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
    if not isinstance(value, float) or value < 1.0 or value > 300.0:
        raise ValueError("connection timeout is invalid")
    return value
