from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from stock_desk.analysis.model_config import (
    InMemoryModelConfigRepository,
    ModelConfig,
    ModelConfigStorageError,
    ModelConfigService,
    ModelConfigUpdate,
    ModelProviderKind,
)


API_KEY = "sk-config-plaintext-never-leak"


class StubSecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def save_secret(self, name: str, value: str) -> None:
        self.values[name] = value

    def has_secret(self, name: str) -> bool:
        return name in self.values

    def masked_secret(self, name: str) -> str:
        assert name in self.values
        return "sk-c•••••••leak"


def test_model_config_imports_without_loading_provider_adapters() -> None:
    subprocess.run(
        [sys.executable, "-c", "import stock_desk.analysis.model_config"],
        check=True,
        capture_output=True,
        text=True,
    )


def update(**overrides: Any) -> ModelConfigUpdate:
    values: dict[str, Any] = {
        "provider": ModelProviderKind.OPENAI_COMPATIBLE,
        "base_url": "https://models.example.com/v1",
        "model": "vendor-chat",
        "api_key": SecretStr(API_KEY),
    }
    values.update(overrides)
    return ModelConfigUpdate(**values)


def test_api_key_is_write_only_in_every_serialization_and_repr() -> None:
    value = update()

    rendered = " ".join(
        (
            repr(value),
            str(value),
            repr(value.model_dump()),
            value.model_dump_json(),
        )
    )

    assert API_KEY not in rendered
    assert "api_key" not in value.model_dump()
    assert "api_key" not in value.model_dump_json()
    schema = ModelConfigUpdate.model_json_schema()
    assert schema["properties"]["api_key"]["writeOnly"] is True


def test_public_config_rejects_unmasked_key_without_echoing_input() -> None:
    with pytest.raises(ValidationError) as captured:
        ModelConfig(
            provider=ModelProviderKind.DEEPSEEK,
            base_url="https://api.deepseek.com",
            model="deepseek-v4",
            temperature=0.1,
            timeout_seconds=90.0,
            max_output_tokens=4096,
            api_key_configured=True,
            masked_api_key=API_KEY,
        )

    assert API_KEY not in str(captured.value)
    assert API_KEY not in repr(captured.value)


@pytest.mark.parametrize(
    "invalid_mask",
    [
        f"{API_KEY}•••••••",
        "abc•••••••def",
        "aaaa•••••••bbbbb",
        "aaaa•••••••bbbb\n",
    ],
)
def test_public_config_accepts_only_exact_mask_secret_shapes(
    invalid_mask: str,
) -> None:
    with pytest.raises(ValidationError) as captured:
        ModelConfig(
            provider=ModelProviderKind.DEEPSEEK,
            base_url="https://api.deepseek.com",
            model="deepseek-v4",
            temperature=0.1,
            timeout_seconds=90.0,
            max_output_tokens=4096,
            api_key_configured=True,
            masked_api_key=invalid_mask,
        )

    assert invalid_mask not in str(captured.value)
    assert invalid_mask not in repr(captured.value)


def test_service_stores_key_only_in_secret_store_and_public_safe_config() -> None:
    secrets = StubSecretStore()
    repository = InMemoryModelConfigRepository()
    service = ModelConfigService(repository=repository, secret_store=secrets)

    saved = service.save(update())

    assert secrets.values == {"analysis_model_api_key": API_KEY}
    assert saved.api_key_configured is True
    assert saved.masked_api_key == "sk-c•••••••leak"
    assert saved.identity.provider is ModelProviderKind.OPENAI_COMPATIBLE
    assert saved.identity.model == "vendor-chat"
    assert repository.load() == saved
    public_rendered = f"{saved!r} {saved.model_dump()} {saved.model_dump_json()}"
    assert API_KEY not in public_rendered


@pytest.mark.parametrize("failure_point", ["save", "has", "masked"])
def test_every_secret_store_failure_is_typed_and_never_echoes_key(
    failure_point: str,
) -> None:
    class ExplodingSecretStore(StubSecretStore):
        def save_secret(self, name: str, value: str) -> None:
            if failure_point == "save":
                raise RuntimeError(f"failed to save {value}")
            super().save_secret(name, value)

        def has_secret(self, name: str) -> bool:
            if failure_point == "has":
                raise RuntimeError(f"failed to find {API_KEY}")
            return super().has_secret(name)

        def masked_secret(self, name: str) -> str:
            if failure_point == "masked":
                raise RuntimeError(f"failed to mask {API_KEY}")
            return super().masked_secret(name)

    repository = InMemoryModelConfigRepository()
    service = ModelConfigService(
        repository=repository,
        secret_store=ExplodingSecretStore(),
    )

    with pytest.raises(ModelConfigStorageError) as captured:
        service.save(update())

    rendered = f"{captured.value!r} {captured.value}"
    assert API_KEY not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert repository.load() is None


def test_deepseek_and_ollama_resolve_named_provider_defaults() -> None:
    secrets = StubSecretStore()
    repository = InMemoryModelConfigRepository()
    service = ModelConfigService(repository=repository, secret_store=secrets)

    deepseek = service.save(
        update(
            provider=ModelProviderKind.DEEPSEEK,
            base_url=None,
            model="deepseek-v4",
        )
    )
    assert deepseek.base_url == "https://api.deepseek.com"

    ollama = service.save(
        update(
            provider=ModelProviderKind.OLLAMA,
            base_url=None,
            model="qwen3:8b",
            api_key=None,
        )
    )
    assert ollama.base_url == "http://127.0.0.1:11434"
    assert ollama.api_key_configured is False
    assert ollama.masked_api_key is None


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        (ModelProviderKind.OPENAI_COMPATIBLE, "http://models.example.com/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://127.0.0.1/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://%31%32%37.0.0.1/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://10.0.0.2/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://host.local/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://user:pass@example.com/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://example.com/v1#fragment"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://example.com:0/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://example.com:22/v1"),
        (ModelProviderKind.OLLAMA, "https://ollama.example.com:11434"),
        (ModelProviderKind.OLLAMA, "http://192.168.1.2:11434"),
        (ModelProviderKind.OLLAMA, "ftp://localhost:11434"),
        (ModelProviderKind.OLLAMA, "http://localhost"),
        (ModelProviderKind.OLLAMA, "http://localhost:6379"),
    ],
)
def test_provider_urls_reject_ssrf_and_credential_hazards(
    provider: ModelProviderKind, url: str
) -> None:
    with pytest.raises(ValidationError):
        update(
            provider=provider,
            base_url=url,
            api_key=None
            if provider is ModelProviderKind.OLLAMA
            else SecretStr(API_KEY),
        )


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://models.example.com/v1"),
        (ModelProviderKind.OPENAI_COMPATIBLE, "https://models.example.com:8443/v1"),
        (ModelProviderKind.OLLAMA, "http://localhost:11434"),
        (ModelProviderKind.OLLAMA, "https://[::1]:11434"),
        (ModelProviderKind.OLLAMA, "https://localhost"),
    ],
)
def test_provider_urls_accept_only_expected_remote_or_local_endpoints(
    provider: ModelProviderKind, url: str
) -> None:
    value = update(
        provider=provider,
        base_url=url,
        api_key=None if provider is ModelProviderKind.OLLAMA else SecretStr(API_KEY),
    )
    assert value.base_url == url


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", -0.01),
        ("temperature", 2.01),
        ("timeout_seconds", 0.9),
        ("timeout_seconds", 301.0),
        ("max_output_tokens", 0),
        ("max_output_tokens", 65_537),
    ],
)
def test_runtime_parameters_are_bounded(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        update(**{field: value})


@pytest.mark.parametrize("api_key", [" key-value", "key-value ", "key\nvalue"])
def test_remote_api_key_rejects_header_unsafe_values(api_key: str) -> None:
    with pytest.raises(ValidationError):
        update(api_key=SecretStr(api_key))


def test_models_are_strict_frozen_and_forbid_unknown_fields() -> None:
    config = ModelConfig(
        provider=ModelProviderKind.DEEPSEEK,
        base_url="https://api.deepseek.com",
        model="deepseek-v4",
        temperature=0.1,
        timeout_seconds=90.0,
        max_output_tokens=4096,
        api_key_configured=False,
        masked_api_key=None,
    )
    with pytest.raises(ValidationError):
        ModelConfigUpdate.model_validate(
            {
                "provider": "deepseek",
                "model": "deepseek-v4",
                "unknown": True,
            }
        )
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(
            {
                **config.model_dump(),
                "temperature": 1,
            }
        )
    with pytest.raises(ValidationError):
        config.model = "changed"  # type: ignore[misc]
