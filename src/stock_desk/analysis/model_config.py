from __future__ import annotations

from enum import StrEnum
import ipaddress
import math
from threading import RLock
from typing import Protocol, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictFloat,
    field_validator,
    model_validator,
)


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
MODEL_API_KEY_SECRET_NAME = "analysis_model_api_key"

_DANGEROUS_PORTS = frozenset(
    {
        21,
        22,
        23,
        25,
        53,
        69,
        110,
        111,
        135,
        137,
        138,
        139,
        143,
        389,
        445,
        465,
        587,
        636,
        993,
        995,
        1433,
        1521,
        2049,
        2375,
        2379,
        3306,
        5432,
        5672,
        6379,
        9200,
        11211,
        27017,
    }
)


class ModelProviderKind(StrEnum):
    DEEPSEEK = "deepseek"
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class ModelIdentity(_FrozenModel):
    provider: ModelProviderKind
    base_url: str
    model: str


class ModelConfig(_FrozenModel):
    provider: ModelProviderKind
    base_url: str
    model: str = Field(min_length=1, max_length=256)
    temperature: StrictFloat = Field(ge=0.0, le=2.0)
    timeout_seconds: StrictFloat = Field(ge=1.0, le=300.0)
    max_output_tokens: int = Field(ge=1, le=65_536)
    api_key_configured: bool
    masked_api_key: str | None = Field(default=None, max_length=64)

    @property
    def identity(self) -> ModelIdentity:
        return ModelIdentity(
            provider=self.provider,
            base_url=self.base_url,
            model=self.model,
        )

    @field_validator("temperature", "timeout_seconds", mode="before")
    @classmethod
    def validate_float_fields(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("model runtime value must be a float")
        return value

    @field_validator("masked_api_key")
    @classmethod
    def validate_masked_api_key(cls, value: str | None) -> str | None:
        if value is not None and "•••••••" not in value and value != "[MASKED]":
            raise ValueError("API key hint is not masked")
        return value

    @model_validator(mode="after")
    def validate_public_state(self) -> Self:
        validate_provider_url(self.provider, self.base_url)
        _validate_model(self.model)
        _validate_finite(self.temperature)
        _validate_finite(self.timeout_seconds)
        if self.api_key_configured != (self.masked_api_key is not None):
            raise ValueError("API key status is inconsistent")
        if self.provider is ModelProviderKind.OLLAMA and self.api_key_configured:
            raise ValueError("Ollama cannot have an API key")
        return self


class ModelConfigUpdate(_FrozenModel):
    provider: ModelProviderKind
    base_url: str | None = None
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr | None = Field(
        default=None,
        min_length=4,
        max_length=4_096,
        exclude=True,
        repr=False,
        json_schema_extra={"writeOnly": True},
    )
    temperature: StrictFloat = Field(default=0.1, ge=0.0, le=2.0)
    timeout_seconds: StrictFloat = Field(default=90.0, ge=1.0, le=300.0)
    max_output_tokens: int = Field(default=4_096, ge=1, le=65_536)

    @field_validator("temperature", "timeout_seconds", mode="before")
    @classmethod
    def validate_float_fields(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("model runtime value must be a float")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        plaintext = value.get_secret_value()
        if plaintext != plaintext.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in plaintext
        ):
            raise ValueError("API key is invalid")
        return value

    @model_validator(mode="after")
    def validate_update(self) -> Self:
        if self.base_url is None:
            if self.provider is ModelProviderKind.OPENAI_COMPATIBLE:
                raise ValueError("OpenAI-compatible provider requires a base URL")
        else:
            validate_provider_url(self.provider, self.base_url)
        _validate_model(self.model)
        _validate_finite(self.temperature)
        _validate_finite(self.timeout_seconds)
        if self.provider is ModelProviderKind.OLLAMA and self.api_key is not None:
            raise ValueError("Ollama does not accept an API key")
        return self


class ModelConfigRepository(Protocol):
    def save(self, config: ModelConfig) -> None: ...

    def load(self) -> ModelConfig | None: ...


class ModelConfigSecretStore(Protocol):
    def save_secret(self, name: str, value: str) -> None: ...

    def has_secret(self, name: str) -> bool: ...

    def masked_secret(self, name: str) -> str: ...


class InMemoryModelConfigRepository:
    """Minimal Task-1 repository; persistent storage is supplied in a later task."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._config: ModelConfig | None = None

    def __repr__(self) -> str:
        return "InMemoryModelConfigRepository(configured=%s)" % (
            self._config is not None
        )

    def save(self, config: ModelConfig) -> None:
        with self._lock:
            self._config = config

    def load(self) -> ModelConfig | None:
        with self._lock:
            return self._config


class ModelConfigService:
    def __init__(
        self,
        *,
        repository: ModelConfigRepository,
        secret_store: ModelConfigSecretStore,
    ) -> None:
        self._repository = repository
        self._secret_store = secret_store
        self._lock = RLock()

    def __repr__(self) -> str:
        return "ModelConfigService(configured=True)"

    def save(self, update: ModelConfigUpdate) -> ModelConfig:
        with self._lock:
            if update.api_key is not None:
                self._secret_store.save_secret(
                    MODEL_API_KEY_SECRET_NAME,
                    update.api_key.get_secret_value(),
                )
            configured = (
                False
                if update.provider is ModelProviderKind.OLLAMA
                else self._secret_store.has_secret(MODEL_API_KEY_SECRET_NAME)
            )
            masked = (
                self._secret_store.masked_secret(MODEL_API_KEY_SECRET_NAME)
                if configured
                else None
            )
            config = ModelConfig(
                provider=update.provider,
                base_url=_resolved_base_url(update),
                model=update.model,
                temperature=update.temperature,
                timeout_seconds=update.timeout_seconds,
                max_output_tokens=update.max_output_tokens,
                api_key_configured=configured,
                masked_api_key=masked,
            )
            self._repository.save(config)
            return config


def _resolved_base_url(update: ModelConfigUpdate) -> str:
    if update.base_url is not None:
        return update.base_url
    if update.provider is ModelProviderKind.DEEPSEEK:
        return DEEPSEEK_BASE_URL
    if update.provider is ModelProviderKind.OLLAMA:
        return OLLAMA_BASE_URL
    raise ValueError("OpenAI-compatible provider requires a base URL")


def _validate_model(model: str) -> None:
    if (
        not model
        or model != model.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in model)
    ):
        raise ValueError("model name is invalid")


def _validate_finite(value: float) -> None:
    if not math.isfinite(value):
        raise ValueError("model runtime value must be finite")


def validate_provider_url(provider: ModelProviderKind, value: str) -> str:
    if (
        not value
        or len(value) > 2_048
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("model provider URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("model provider URL is invalid") from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or _is_dangerous_port(port)
    ):
        raise ValueError("model provider URL is unsafe")
    hostname = parsed.hostname.lower().rstrip(".")
    if provider is ModelProviderKind.OLLAMA:
        if parsed.scheme not in {"http", "https"} or not _is_loopback(hostname):
            raise ValueError("Ollama URL must use a local HTTP endpoint")
        return value
    if parsed.scheme != "https" or _is_non_public_host(hostname):
        raise ValueError("remote model URL must use a public HTTPS endpoint")
    return value


def _is_loopback(hostname: str) -> bool:
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_dangerous_port(port: int | None) -> bool:
    if port is None:
        return False
    return port == 0 or port in _DANGEROUS_PORTS or (port < 1_024 and port != 443)


def _is_non_public_host(hostname: str) -> bool:
    if (
        hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal", ".home.arpa"))
        or "." not in hostname
    ):
        return True
    try:
        return not ipaddress.ip_address(hostname).is_global
    except ValueError:
        return not _is_valid_dns_hostname(hostname)


def _is_valid_dns_hostname(hostname: str) -> bool:
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253:
        return False
    for label in ascii_hostname.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
        ):
            return False
    return True
