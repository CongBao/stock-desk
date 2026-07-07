from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from typing import Any, cast

import httpx2
import pytest
from pydantic import ValidationError

from stock_desk.analysis.providers.base import (
    MAX_MODEL_JSON_BYTES,
    MAX_MODEL_JSON_DEPTH,
    MAX_MODEL_JSON_NODES,
    MAX_PROVIDER_RESPONSE_BYTES,
    MAX_PROVIDER_RESPONSE_DEPTH,
    MAX_PROVIDER_RESPONSE_NODES,
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
    expected_usage = {
        "deepseek": (11, 7, 18),
        "openai": (11, 7, 18),
        "ollama": (13, 5, 18),
    }[kind]
    assert response.usage.input_tokens == expected_usage[0]
    assert response.usage.output_tokens == expected_usage[1]
    assert response.usage.total_tokens == expected_usage[2]
    assert str(captured[0].url) == expected_endpoint
    observed_timeouts = captured[0].extensions["timeout"]
    if kind == "ollama":
        assert observed_timeouts == {
            "connect": 17.0,
            "read": 17.0,
            "write": 17.0,
            "pool": 17.0,
        }
    else:
        assert all(0 < value <= 17.0 for value in observed_timeouts.values())
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
    observed_read_timeout = captured[0].extensions["timeout"]["read"]
    if kind == "ollama":
        assert observed_read_timeout == 4.0
    else:
        assert 0 < observed_read_timeout <= 4.0


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


def test_mutated_request_is_revalidated_before_transport() -> None:
    mutable_request = request()
    mutable_request.data_blocks[0]["payload"] = "x" * (MAX_MODEL_JSON_BYTES + 1)
    calls: list[httpx2.Request] = []
    provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(
            lambda http_request: (
                calls.append(http_request)
                or httpx2.Response(200, json=openai_response("vendor-chat"))
            )
        ),
        resolver=resolve_public,
    )

    with pytest.raises(ValueError, match="byte limit"):
        run(provider.complete(mutable_request))

    assert calls == []


def test_concurrent_mutation_cannot_change_one_validated_request_snapshot() -> None:
    mutable_request = request()
    captured: list[httpx2.Request] = []

    async def scenario() -> None:
        resolver_started = asyncio.Event()
        release_resolver = asyncio.Event()

        async def blocking_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
            resolver_started.set()
            await release_resolver.wait()
            return ("93.184.216.34",)

        provider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=httpx2.MockTransport(
                lambda http_request: (
                    captured.append(http_request)
                    or httpx2.Response(200, json=openai_response("vendor-chat"))
                )
            ),
            resolver=blocking_resolver,
        )
        completion = asyncio.create_task(provider.complete(mutable_request))
        await resolver_started.wait()
        mutable_request.output_schema["properties"] = {"mutated": {"type": "boolean"}}
        mutable_request.data_blocks[0]["symbol"] = "MUTATED"
        release_resolver.set()
        await completion

    asyncio.run(scenario())

    body = json.loads(captured[0].content)
    user_content = json.loads(body["messages"][1]["content"])
    assert user_content["data_blocks"][0]["symbol"] == "600000.SH"
    assert "mutated" not in body["response_format"]["json_schema"]["schema"].get(
        "properties", {}
    )


def test_model_response_content_access_returns_defensive_deep_copies() -> None:
    nested_response = openai_response("vendor-chat")
    nested_response["choices"] = [
        {
            "message": {
                "role": "assistant",
                "content": '{"summary":"ok","items":[{"value":1}]}',
            }
        }
    ]
    provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, json=nested_response)
        ),
        resolver=resolve_public,
    )

    response = run(provider.complete(request()))
    first = response.content
    first["items"][0]["value"] = 99  # type: ignore[index]

    assert response.content["items"][0]["value"] == 1  # type: ignore[index]


class TrackingBorrowedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0
        self.close_calls = 0

    async def handle_async_request(
        self, http_request: httpx2.Request
    ) -> httpx2.Response:
        if self.close_calls:
            raise RuntimeError("borrowed transport was closed")
        self.calls += 1
        if self.kind == "ollama":
            return httpx2.Response(200, json=ollama_response("qwen3:8b"))
        model = "deepseek-v4" if self.kind == "deepseek" else "vendor-chat"
        return httpx2.Response(200, json=openai_response(model))

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize("kind", ["deepseek", "openai", "ollama"])
def test_injected_transport_is_borrowed_and_reusable_across_calls(kind: str) -> None:
    transport = TrackingBorrowedTransport(kind)
    if kind == "deepseek":
        provider: ModelProvider = DeepSeekProvider(
            model="deepseek-v4",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
    elif kind == "openai":
        provider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
    else:
        provider = OllamaProvider(model="qwen3:8b", transport=transport)

    run(provider.complete(request()))
    run(provider.complete(request()))

    assert transport.calls == 2
    assert transport.close_calls == 0
    run(transport.aclose())


@pytest.mark.parametrize("kind", ["openai", "deepseek"])
@pytest.mark.parametrize("models", [[], [{"id": "different-model"}]])
def test_remote_connection_requires_configured_model_in_model_list(
    kind: str,
    models: list[dict[str, str]],
) -> None:
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(200, json={"data": models})
    )
    if kind == "deepseek":
        provider: ModelProvider = DeepSeekProvider(
            model="deepseek-v4",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
    else:
        provider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )

    result = run(provider.test_connection(timeout_seconds=1.0))

    assert result.connected is False
    assert result.error_code == "invalid_response"


@pytest.mark.parametrize("kind", ["openai", "deepseek"])
def test_dns_resolution_is_bounded_by_total_connection_deadline(kind: str) -> None:
    async def stalled_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    transport = httpx2.MockTransport(
        lambda _request: pytest.fail("stalled DNS reached HTTP transport")
    )
    if kind == "deepseek":
        provider: ModelProvider = DeepSeekProvider(
            model="deepseek-v4",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=stalled_resolver,
        )
    else:
        provider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=stalled_resolver,
        )

    started = time.monotonic()
    result = run(provider.test_connection(timeout_seconds=0.02))
    elapsed = time.monotonic() - started

    assert result.connected is False
    assert result.error_code == "timeout"
    assert elapsed < 0.5


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_connection_timeout_rejects_non_finite_values(value: float) -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(
            lambda _request: pytest.fail("invalid timeout reached transport")
        ),
        resolver=resolve_public,
    )

    result = run(provider.test_connection(timeout_seconds=value))

    assert result.connected is False
    assert result.error_code == "invalid_response"


@pytest.mark.parametrize("kind", ["openai", "ollama"])
def test_provider_rejects_response_larger_than_fixed_byte_limit(kind: str) -> None:
    oversized = b'{"ignored":"' + (b"x" * MAX_PROVIDER_RESPONSE_BYTES) + b'"}'
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(200, content=oversized)
    )
    if kind == "openai":
        provider: ModelProvider = OpenAICompatibleProvider(
            base_url="https://models.example.com/v1",
            model="vendor-chat",
            secret_store=StubSecretStore(),
            transport=transport,
            resolver=resolve_public,
        )
    else:
        provider = OllamaProvider(model="qwen3:8b", transport=transport)

    with pytest.raises(ModelInvalidResponseError):
        run(provider.complete(request()))


@pytest.mark.parametrize("limit_kind", ["depth", "nodes"])
def test_provider_rejects_response_exceeding_structural_budget(
    limit_kind: str,
) -> None:
    payload = openai_response("vendor-chat")
    if limit_kind == "depth":
        nested: dict[str, Any] = {"leaf": True}
        for _ in range(MAX_PROVIDER_RESPONSE_DEPTH + 1):
            nested = {"nested": nested}
        payload["ignored"] = nested
    else:
        payload["ignored"] = list(range(MAX_PROVIDER_RESPONSE_NODES + 1))
    provider = OpenAICompatibleProvider(
        base_url="https://models.example.com/v1",
        model="vendor-chat",
        secret_store=StubSecretStore(),
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, json=payload)
        ),
        resolver=resolve_public,
    )

    with pytest.raises(ModelInvalidResponseError):
        run(provider.complete(request()))
