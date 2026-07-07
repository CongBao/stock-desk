from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, cast

import httpx2
import pytest
from pydantic import ValidationError

from stock_desk.analysis.providers.base import (
    MAX_MODEL_JSON_BYTES,
    MAX_MODEL_JSON_DEPTH,
    MAX_MODEL_JSON_NODES,
    ModelAuthenticationError,
    ModelDNSResolutionError,
    ModelInvalidResponseError,
    ModelProvider,
    ModelRateLimitError,
    ModelRequest,
    ModelServerError,
    ModelTimeoutError,
    ModelUnsafeEndpointError,
)
from stock_desk.analysis.providers.deepseek import DeepSeekProvider
from stock_desk.analysis.providers.ollama import OllamaProvider
from stock_desk.analysis.providers.openai_compatible import (
    OpenAICompatibleProvider,
)


API_KEY = "sk-contract-secret-never-leak"


class StubSecretStore:
    def read_secret_for_server_call(self, name: str) -> str:
        assert name == "analysis_model_api_key"
        return API_KEY


async def resolve_public(_hostname: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


def request() -> ModelRequest:
    return ModelRequest(
        system="Return only the requested JSON object.",
        data_blocks=({"symbol": "600000.SH", "close": 10.25},),
        output_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        temperature=0.2,
        timeout_seconds=17.0,
        max_output_tokens=321,
    )


def openai_response(model: str) -> dict[str, object]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": '{"summary":"ok"}'}}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
    }


def ollama_response(model: str) -> dict[str, object]:
    return {
        "model": model,
        "message": {"role": "assistant", "content": '{"summary":"ok"}'},
        "prompt_eval_count": 13,
        "eval_count": 5,
        "done": True,
    }


def run[T](awaitable: Any) -> T:
    return cast(T, asyncio.run(awaitable))


@pytest.mark.parametrize("target", ["data_blocks", "output_schema"])
def test_model_request_rejects_aggregate_json_larger_than_fixed_limit(
    target: str,
) -> None:
    marker = "oversized-structured-input-marker"
    oversized = marker + ("x" * MAX_MODEL_JSON_BYTES)
    data_blocks = ({"payload": oversized},) if target == "data_blocks" else ({},)
    output_schema = (
        {"type": "object", "description": oversized}
        if target == "output_schema"
        else {"type": "object"}
    )

    with pytest.raises(ValidationError) as captured:
        ModelRequest(
            system="Return JSON.",
            data_blocks=data_blocks,
            output_schema=output_schema,
        )

    assert marker not in str(captured.value)
    assert marker not in repr(captured.value)


@pytest.mark.parametrize("target", ["data_blocks", "output_schema"])
def test_model_request_rejects_json_deeper_than_fixed_limit(target: str) -> None:
    nested: dict[str, Any] = {"value": "leaf"}
    for _ in range(MAX_MODEL_JSON_DEPTH + 1):
        nested = {"nested": nested}
    data_blocks = (nested,) if target == "data_blocks" else ({},)
    output_schema = (
        {"type": "object", "metadata": nested}
        if target == "output_schema"
        else {"type": "object"}
    )

    with pytest.raises(ValidationError):
        ModelRequest(
            system="Return JSON.",
            data_blocks=data_blocks,
            output_schema=output_schema,
        )


@pytest.mark.parametrize("target", ["data_blocks", "output_schema"])
def test_model_request_rejects_json_with_too_many_nodes(target: str) -> None:
    many_nodes = list(range(MAX_MODEL_JSON_NODES + 1))
    data_blocks = ({"values": many_nodes},) if target == "data_blocks" else ({},)
    output_schema = (
        {"type": "object", "metadata": many_nodes}
        if target == "output_schema"
        else {"type": "object"}
    )
    with pytest.raises(ValidationError):
        ModelRequest(
            system="Return JSON.",
            data_blocks=data_blocks,
            output_schema=output_schema,
        )


@pytest.mark.parametrize("kind", ["deepseek", "openai", "ollama"])
def test_all_adapters_share_async_contract_and_parse_structured_responses(
    kind: str,
) -> None:
    captured: list[httpx2.Request] = []

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        captured.append(http_request)
        if kind == "ollama":
            return httpx2.Response(200, json=ollama_response("qwen3:8b"))
        model = "deepseek-v4" if kind == "deepseek" else "vendor-chat"
        return httpx2.Response(200, json=openai_response(model))

    transport = httpx2.MockTransport(handler)
    if kind == "deepseek":
        provider: ModelProvider = DeepSeekProvider(
            model="deepseek-v4",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
        expected_endpoint = "https://api.deepseek.com/chat/completions"
        expected_model = "deepseek-v4"
    elif kind == "openai":
        provider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
        expected_endpoint = "https://models.example.com/v1/chat/completions"
        expected_model = "vendor-chat"
    else:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model="qwen3:8b",
            transport=transport,
        )
        expected_endpoint = "http://127.0.0.1:11434/api/chat"
        expected_model = "qwen3:8b"

    assert isinstance(provider, ModelProvider)
    assert inspect.iscoroutinefunction(provider.complete)
    assert inspect.iscoroutinefunction(provider.test_connection)

    response = run(provider.complete(request()))

    assert response.model == expected_model
    assert response.content == {"summary": "ok"}
    assert response.usage.input_tokens in {11, 13}
    assert response.usage.output_tokens in {7, 5}
    assert str(captured[0].url) == expected_endpoint
    assert captured[0].extensions["timeout"] == {
        "connect": 17.0,
        "read": 17.0,
        "write": 17.0,
        "pool": 17.0,
    }
    body = json.loads(captured[0].content)
    assert body["model"] == expected_model
    assert "tools" not in body
    if kind == "ollama":
        assert body["format"] == request().output_schema
        assert body["options"] == {"temperature": 0.2, "num_predict": 321}
        assert "authorization" not in captured[0].headers
    else:
        assert body["temperature"] == 0.2
        assert body["max_tokens"] == 321
        assert body["response_format"]["json_schema"]["schema"] == (
            request().output_schema
        )
        assert captured[0].headers["authorization"] == f"Bearer {API_KEY}"


@pytest.mark.parametrize("kind", ["deepseek", "openai", "ollama"])
def test_connection_uses_provider_endpoint_and_returns_typed_result(kind: str) -> None:
    captured: list[httpx2.Request] = []

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        captured.append(http_request)
        if kind == "ollama":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3:8b", "model": "qwen3:8b"}]},
            )
        return httpx2.Response(200, json={"data": [{"id": "vendor-chat"}]})

    transport = httpx2.MockTransport(handler)
    if kind == "deepseek":
        provider: ModelProvider = DeepSeekProvider(
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
        endpoint = "https://api.deepseek.com/models"
    elif kind == "openai":
        provider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
        endpoint = "https://models.example.com/v1/models"
    else:
        provider = OllamaProvider(
            base_url="http://localhost:11434",
            model="qwen3:8b",
            transport=transport,
        )
        endpoint = "http://localhost:11434/api/tags"

    result = run(provider.test_connection(timeout_seconds=4.0))

    assert result.connected is True
    assert result.model == provider.model
    assert result.error_code is None
    assert captured[0].method == "GET"
    assert str(captured[0].url) == endpoint
    assert captured[0].extensions["timeout"]["read"] == 4.0


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, ModelAuthenticationError),
        (403, ModelAuthenticationError),
        (429, ModelRateLimitError),
        (500, ModelServerError),
        (503, ModelServerError),
        (400, ModelInvalidResponseError),
    ],
)
def test_http_failures_are_typed_and_do_not_leak_response_or_key(
    status_code: int,
    expected_error: type[Exception],
) -> None:
    response_secret = "provider-body-secret-never-leak"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, text=response_secret)

    provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(handler),
        resolver=resolve_public,
    )

    with pytest.raises(expected_error) as captured:
        run(provider.complete(request()))

    rendered = f"{captured.value!r} {captured.value}"
    assert API_KEY not in rendered
    assert response_secret not in rendered


def test_timeout_and_malformed_json_are_typed_and_secret_safe() -> None:
    def timeout_handler(http_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout(f"timeout {API_KEY}", request=http_request)

    timeout_provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(timeout_handler),
        resolver=resolve_public,
    )
    with pytest.raises(ModelTimeoutError) as timeout_error:
        run(timeout_provider.complete(request()))
    assert API_KEY not in repr(timeout_error.value)
    assert API_KEY not in str(timeout_error.value)

    invalid_provider = OllamaProvider(
        base_url="http://localhost:11434",
        model="qwen3:8b",
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, content=b"not-json")
        ),
    )
    with pytest.raises(ModelInvalidResponseError):
        run(invalid_provider.complete(request()))


def test_connection_failure_is_a_safe_result_instead_of_an_exception() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text=f"bad {API_KEY}")

    provider = DeepSeekProvider(
        model="deepseek-v4",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(handler),
        resolver=resolve_public,
    )

    result = run(provider.test_connection())

    assert result.connected is False
    assert result.error_code == "authentication"
    assert API_KEY not in result.model_dump_json()
    assert API_KEY not in repr(result)


def test_unavailable_or_header_unsafe_api_key_is_typed_as_authentication() -> None:
    class UnsafeSecretStore:
        def read_secret_for_server_call(self, _name: str) -> str:
            return "unsafe\nsecret"

    provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=UnsafeSecretStore(),
        transport=httpx2.MockTransport(
            lambda _request: pytest.fail("unsafe key reached HTTP transport")
        ),
        resolver=resolve_public,
    )

    with pytest.raises(ModelAuthenticationError):
        run(provider.complete(request()))


@pytest.mark.parametrize("kind", ["openai", "deepseek"])
@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.2",),
        ("169.254.169.254",),
        ("93.184.216.34", "192.168.1.2"),
    ],
)
def test_remote_request_rejects_any_resolved_non_global_address(
    kind: str,
    addresses: tuple[str, ...],
) -> None:
    async def resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        return addresses

    transport = httpx2.MockTransport(
        lambda _request: pytest.fail("unsafe resolved address reached HTTP transport")
    )
    provider: ModelProvider
    if kind == "deepseek":
        provider = DeepSeekProvider(
            model="deepseek-v4",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolver,
        )
    else:
        provider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolver,
        )

    with pytest.raises(ModelUnsafeEndpointError):
        run(provider.complete(request()))


def test_dns_failure_is_typed_safe_and_happens_before_transport() -> None:
    dns_secret = "dns-diagnostic-secret-never-leak"

    async def failing_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        raise OSError(dns_secret)

    provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(
            lambda _request: pytest.fail("DNS failure reached HTTP transport")
        ),
        resolver=failing_resolver,
    )

    with pytest.raises(ModelDNSResolutionError) as captured:
        run(provider.complete(request()))

    rendered = f"{captured.value!r} {captured.value}"
    assert dns_secret not in rendered
    assert API_KEY not in rendered
    assert captured.value.__context__ is None
