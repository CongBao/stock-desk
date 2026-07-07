from __future__ import annotations

import asyncio

import httpx2
import pytest

from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.analysis.model_settings import (
    ModelProviderFactory,
    ModelSettingsStorageError,
    ModelSettingsValidationError,
)
from stock_desk.analysis.providers.deepseek import DeepSeekProvider
from stock_desk.analysis.providers.ollama import OllamaProvider
from stock_desk.analysis.providers.openai_compatible import OpenAICompatibleProvider


REF = "analysis_model_api_key_0123456789abcdef0123456789abcdef"
KEY = "sk-factory-private-key"


class _SecretReader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_secret_for_server_call(self, name: str) -> str:
        self.calls.append(name)
        return KEY


async def _public_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


def _config(kind: ModelProviderKind) -> AnalysisModelPublicConfig:
    if kind is ModelProviderKind.DEEPSEEK:
        base_url = "https://api.deepseek.com"
    elif kind is ModelProviderKind.OLLAMA:
        base_url = "http://127.0.0.1:11434"
    else:
        base_url = "https://models.example.com/v1"
    return AnalysisModelPublicConfig(
        provider=kind,
        base_url=base_url,
        model="factory-model",
        temperature=0.1,
        timeout_seconds=90.0,
        max_output_tokens=4096,
        secret_reference_id=None if kind is ModelProviderKind.OLLAMA else REF,
        api_key_configured=kind is not ModelProviderKind.OLLAMA,
    )


@pytest.mark.parametrize(
    ("kind", "provider_type"),
    [
        (ModelProviderKind.DEEPSEEK, DeepSeekProvider),
        (ModelProviderKind.OPENAI_COMPATIBLE, OpenAICompatibleProvider),
        (ModelProviderKind.OLLAMA, OllamaProvider),
    ],
)
def test_factory_preserves_provider_model_endpoint_and_secret_reference(
    kind: ModelProviderKind,
    provider_type: type[object],
) -> None:
    secret_reader = _SecretReader()
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if kind is ModelProviderKind.OLLAMA:
            return httpx2.Response(200, json={"models": [{"name": "factory-model"}]})
        return httpx2.Response(200, json={"data": [{"id": "factory-model"}]})

    factory = ModelProviderFactory(
        secret_store=secret_reader,  # type: ignore[arg-type]
        transport=httpx2.MockTransport(respond),
        resolver=_public_resolver,
    )

    provider = factory.create(_config(kind))
    result = asyncio.run(provider.test_connection(timeout_seconds=1.0))

    assert isinstance(provider, provider_type)
    assert provider.provider == kind.value
    assert provider.model == "factory-model"
    assert result.connected is True
    assert len(requests) == 1
    if kind is ModelProviderKind.OLLAMA:
        assert secret_reader.calls == []
        assert requests[0].url.host == "127.0.0.1"
        assert "authorization" not in requests[0].headers
    else:
        assert secret_reader.calls == [REF]
        assert requests[0].headers["authorization"] == f"Bearer {KEY}"
        assert requests[0].url.host == "93.184.216.34"


def test_factory_revalidates_strict_secret_reference_at_its_boundary() -> None:
    unsafe_ref = "analysis_model_api_key_not-a-version"
    bypassed = AnalysisModelPublicConfig.model_construct(
        schema_version="analysis-model-public-v1",
        provider=ModelProviderKind.OPENAI_COMPATIBLE,
        base_url="https://models.example.com/v1",
        model="factory-model",
        temperature=0.1,
        timeout_seconds=90.0,
        max_output_tokens=4096,
        secret_reference_id=unsafe_ref,
        api_key_configured=True,
    )
    factory = ModelProviderFactory(secret_store=_SecretReader())  # type: ignore[arg-type]

    with pytest.raises(ModelSettingsValidationError) as captured:
        factory.create(bypassed)

    assert unsafe_ref not in str(captured.value)
    assert unsafe_ref not in repr(captured.value)


def test_factory_allows_ollama_without_secret_reader_but_rejects_remote() -> None:
    factory = ModelProviderFactory(secret_store=None)

    provider = factory.create(_config(ModelProviderKind.OLLAMA))
    assert isinstance(provider, OllamaProvider)

    with pytest.raises(ModelSettingsStorageError):
        factory.create(_config(ModelProviderKind.OPENAI_COMPATIBLE))

    remote_without_ref = _config(ModelProviderKind.OPENAI_COMPATIBLE).model_copy(
        update={"secret_reference_id": None, "api_key_configured": False}
    )
    with pytest.raises(ModelSettingsStorageError):
        factory.create(remote_without_ref)
