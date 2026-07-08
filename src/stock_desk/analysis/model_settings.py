from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import secrets
from threading import RLock
from typing import cast

import httpx2
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from stock_desk.analysis.model_catalog import (
    AnalysisModelCatalog,
    AnalysisModelConfigSnapshot,
    ModelCatalogConflict,
    ModelConfigListKey,
    ModelConfigStatus,
    ModelNotFound,
    VerifiedModelExecution,
)
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    HostResolver,
    MODEL_API_KEY_SECRET_NAME,
    ModelConfigUpdate,
    ModelProviderKind,
    _resolved_base_url,
    system_host_resolver,
)
from stock_desk.analysis.providers.base import (
    ModelConnectionResult,
    ModelCredentialUnavailableError,
    ModelErrorCode,
    ModelProvider,
    ModelSecretReader,
)
from stock_desk.analysis.providers.deepseek import DeepSeekProvider
from stock_desk.analysis.providers.ollama import OllamaProvider
from stock_desk.analysis.providers.openai_compatible import OpenAICompatibleProvider
from stock_desk.security.secrets import SecretStore, SecretStoreError, mask_secret
from stock_desk.security.redaction import LogSecretLease
from stock_desk.storage.database import DatabaseIdentity


class ModelSettingsError(RuntimeError):
    """Base class for safe model-settings failures."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ModelSettingsValidationError(ModelSettingsError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("Model settings are invalid")


class ModelSettingsStorageError(ModelSettingsError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("Model settings could not be saved")


class ModelSettingsSecureStorageError(ModelSettingsStorageError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("Secure model settings storage is unavailable")


class ModelSettingsConflict(ModelSettingsError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("Model settings state changed")


@dataclass(frozen=True, slots=True)
class ModelSettingsSnapshot:
    id: str
    public_config_hash: str
    display_name: str
    provider: ModelProviderKind
    model: str
    base_url: str
    temperature: float
    timeout_seconds: float
    max_output_tokens: int
    api_key_configured: bool
    masked_api_key: str | None
    supersedes_id: str | None
    status: ModelConfigStatus
    revision: int
    verified_at: datetime | None
    last_tested_at: datetime | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ModelSettingsPage:
    items: tuple[ModelSettingsSnapshot, ...]
    next_key: ModelConfigListKey | None


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    config_id: str
    connected: bool
    provider: ModelProviderKind
    model: str
    error_code: ModelErrorCode | None
    status: ModelConfigStatus
    revision: int
    tested_at: datetime
    last_tested_at: datetime

    def __post_init__(self) -> None:
        valid_revision = type(self.revision) is int and self.revision >= 0
        valid_connection_shape = (
            type(self.connected) is bool
            and (self.error_code is None or isinstance(self.error_code, ModelErrorCode))
            and self.connected == (self.error_code is None)
            and (
                (self.connected and self.status is ModelConfigStatus.VERIFIED)
                or (not self.connected and self.status is ModelConfigStatus.FAILED)
            )
        )
        valid_times = (
            type(self.tested_at) is datetime
            and self.tested_at.tzinfo is not None
            and self.tested_at.utcoffset() is not None
            and type(self.last_tested_at) is datetime
            and self.last_tested_at.tzinfo is not None
            and self.last_tested_at.utcoffset() is not None
            and self.tested_at == self.last_tested_at
        )
        if not (valid_revision and valid_connection_shape and valid_times):
            raise ValueError("Connection test result is invalid")


class _RedactingModelSecretReader:
    def __init__(
        self,
        delegate: ModelSecretReader,
        register: Callable[[str], None],
    ) -> None:
        self._delegate = delegate
        self._register = register

    def read_secret_for_server_call(self, name: str) -> str:
        value = self._delegate.read_secret_for_server_call(name)
        self._register(value)
        return value


class ModelProviderFactory:
    """Build the production provider adapter from immutable public config."""

    def __init__(
        self,
        *,
        secret_store: ModelSecretReader | None,
        transport: httpx2.AsyncBaseTransport | None = None,
        resolver: HostResolver = system_host_resolver,
    ) -> None:
        self._secret_store = secret_store
        self._transport = transport
        self._resolver = resolver
        self._redaction_lock = RLock()
        self._redaction_values: tuple[str, ...] = ()
        self._redaction_lease = LogSecretLease()
        self._closed = False
        self._provider_secret_store = (
            _RedactingModelSecretReader(secret_store, self._register_redaction_value)
            if secret_store is not None
            else None
        )

    def close(self) -> None:
        with self._redaction_lock:
            if self._closed:
                return
            self._closed = True
            self._redaction_values = ()
            self._redaction_lease.close()

    def _register_redaction_value(self, value: str) -> None:
        with self._redaction_lock:
            if self._closed:
                raise ModelSettingsStorageError()
            combined = tuple(dict.fromkeys((*self._redaction_values, value)))
            self._redaction_lease.replace(*combined)
            self._redaction_values = combined

    def create(self, config: AnalysisModelPublicConfig) -> ModelProvider:
        if not isinstance(config, AnalysisModelPublicConfig):
            raise ModelSettingsValidationError()
        try:
            config = AnalysisModelPublicConfig.model_validate(
                config.model_dump(mode="python")
            )
        except Exception:
            raise ModelSettingsValidationError() from None
        if config.provider is ModelProviderKind.OLLAMA:
            if config.secret_reference_id is not None:
                raise ModelSettingsValidationError()
            return cast(
                ModelProvider,
                OllamaProvider(
                    model=config.model,
                    base_url=config.base_url,
                    transport=self._transport,
                ),
            )
        if self._secret_store is None:
            raise ModelSettingsSecureStorageError()
        secret_reference = config.secret_reference_id
        if secret_reference is None:
            raise ModelSettingsValidationError()
        provider_secret_store = self._provider_secret_store
        if provider_secret_store is None:
            raise ModelSettingsSecureStorageError()
        if config.provider is ModelProviderKind.DEEPSEEK:
            return cast(
                ModelProvider,
                DeepSeekProvider(
                    model=config.model,
                    base_url=config.base_url,
                    secret_store=provider_secret_store,
                    secret_name=secret_reference,
                    transport=self._transport,
                    resolver=self._resolver,
                ),
            )
        return cast(
            ModelProvider,
            OpenAICompatibleProvider(
                base_url=config.base_url,
                model=config.model,
                secret_store=provider_secret_store,
                secret_name=secret_reference,
                transport=self._transport,
                resolver=self._resolver,
            ),
        )


class ModelSettingsService:
    def __init__(
        self,
        *,
        catalog: AnalysisModelCatalog,
        secret_store: SecretStore | None,
        provider_factory: ModelProviderFactory | None = None,
    ) -> None:
        self._catalog = catalog
        self._secret_store = secret_store
        self._provider_factory = provider_factory or ModelProviderFactory(
            secret_store=secret_store
        )
        self._redaction_lock = RLock()
        self._redaction_values: tuple[str, ...] = ()
        self._redaction_lease = LogSecretLease()
        self._closed = False

    def __repr__(self) -> str:
        return "ModelSettingsService(configured=True)"

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._catalog.database_identity

    def close(self) -> None:
        with self._redaction_lock:
            if self._closed:
                return
            self._closed = True
            self._redaction_values = ()
            self._redaction_lease.close()
        if isinstance(self._provider_factory, ModelProviderFactory):
            self._provider_factory.close()

    def _register_redaction_values(self, *values: str) -> None:
        normalized = tuple(value for value in values if value)
        if not normalized:
            return
        with self._redaction_lock:
            if self._closed:
                return
            combined = tuple(dict.fromkeys((*self._redaction_values, *normalized)))
            self._redaction_lease.replace(*combined)
            self._redaction_values = combined

    def require_verified_execution(self, config_id: str) -> VerifiedModelExecution:
        try:
            with self._catalog.transaction() as connection:
                return self.require_verified_execution_in_transaction(
                    connection, config_id
                )
        except ModelSettingsError:
            raise
        except SecretStoreError:
            raise ModelSettingsSecureStorageError() from None

    def require_verified_execution_in_transaction(
        self,
        connection: Connection,
        config_id: str,
    ) -> VerifiedModelExecution:
        execution = self._catalog.require_verified_in_transaction(connection, config_id)
        public_config = execution.public_config
        if public_config.provider is ModelProviderKind.OLLAMA:
            return execution
        secret_store = self._secret_store
        secret_reference = public_config.secret_reference_id
        if secret_store is None or secret_reference is None:
            raise ModelSettingsSecureStorageError()
        try:
            plaintext = secret_store.read_secret_for_server_call_in_transaction(
                secret_reference, connection
            )
        except SecretStoreError:
            raise ModelSettingsSecureStorageError() from None
        self._register_redaction_values(plaintext)
        return execution

    def create(
        self,
        display_name: str,
        update: ModelConfigUpdate,
    ) -> ModelSettingsSnapshot:
        if not isinstance(update, ModelConfigUpdate):
            raise ModelSettingsValidationError()
        if (
            update.provider is not ModelProviderKind.OLLAMA
            and self._secret_store is None
        ):
            raise ModelSettingsSecureStorageError()
        if update.provider is not ModelProviderKind.OLLAMA and update.api_key is None:
            raise ModelSettingsValidationError()
        registered: tuple[str, ...] = ()
        try:
            with self._catalog.transaction() as connection:
                secret_reference: str | None = None
                masked: str | None = None
                if update.provider is not ModelProviderKind.OLLAMA:
                    assert update.api_key is not None
                    plaintext = update.api_key.get_secret_value()
                    secret_reference = self._save_fresh_secret(plaintext, connection)
                    masked = mask_secret(plaintext)
                    registered = (plaintext,)
                public_config = _public_config(update, secret_reference)
                snapshot = self._catalog.create_in_transaction(
                    connection,
                    display_name=display_name,
                    public_config=public_config,
                )
        except ModelSettingsError:
            raise
        except SecretStoreError:
            raise ModelSettingsSecureStorageError() from None
        except (ValueError, TypeError):
            raise ModelSettingsValidationError() from None
        except (IntegrityError, ModelCatalogConflict):
            raise ModelSettingsConflict() from None
        except Exception:
            raise ModelSettingsStorageError() from None
        self._register_redaction_values(*registered)
        return _safe_snapshot(snapshot, masked)

    def create_successor(
        self,
        parent_id: str,
        display_name: str,
        update: ModelConfigUpdate,
    ) -> ModelSettingsSnapshot:
        if not isinstance(update, ModelConfigUpdate):
            raise ModelSettingsValidationError()
        secret_store = self._secret_store
        if update.provider is not ModelProviderKind.OLLAMA and secret_store is None:
            raise ModelSettingsSecureStorageError()
        registered: tuple[str, ...] = ()
        try:
            with self._catalog.transaction() as connection:
                parent_public = self._catalog.get_public_config_in_transaction(
                    connection, parent_id
                )
                secret_reference: str | None = None
                masked: str | None = None
                if update.provider is not ModelProviderKind.OLLAMA:
                    if update.api_key is not None:
                        plaintext = update.api_key.get_secret_value()
                        secret_reference = self._save_fresh_secret(
                            plaintext, connection
                        )
                        masked = mask_secret(plaintext)
                    else:
                        if (
                            parent_public.provider is not update.provider
                            or parent_public.base_url != _resolved_base_url(update)
                        ):
                            raise ModelSettingsValidationError()
                        secret_reference = parent_public.secret_reference_id
                        assert secret_store is not None
                        if secret_reference is None:
                            raise ModelSettingsValidationError()
                        if not secret_store.has_secret_in_transaction(
                            secret_reference, connection
                        ):
                            raise ModelSettingsSecureStorageError()
                        plaintext = (
                            secret_store.read_secret_for_server_call_in_transaction(
                                secret_reference, connection
                            )
                        )
                        masked = mask_secret(plaintext)
                    registered = (plaintext,)
                public_config = _public_config(update, secret_reference)
                snapshot = self._catalog.create_in_transaction(
                    connection,
                    display_name=display_name,
                    public_config=public_config,
                    supersedes_id=parent_id,
                )
        except ModelSettingsError:
            raise
        except SecretStoreError:
            raise ModelSettingsSecureStorageError() from None
        except ModelNotFound:
            raise
        except (ValueError, TypeError):
            raise ModelSettingsValidationError() from None
        except (IntegrityError, ModelCatalogConflict):
            raise ModelSettingsConflict() from None
        except Exception:
            raise ModelSettingsStorageError() from None
        self._register_redaction_values(*registered)
        return _safe_snapshot(snapshot, masked)

    def get(self, config_id: str) -> ModelSettingsSnapshot:
        try:
            with self._catalog.transaction() as connection:
                snapshot, public_config = (
                    self._catalog.get_snapshot_and_public_config_in_transaction(
                        connection, config_id
                    )
                )
                masks = self._masks_for_public_configs((public_config,), connection)
        except ModelSettingsError:
            raise
        except SecretStoreError:
            raise ModelSettingsSecureStorageError() from None
        except ModelNotFound:
            raise
        except Exception:
            raise ModelSettingsStorageError() from None
        return _safe_snapshot(snapshot, _mask_for(public_config, masks))

    def list_page(
        self,
        *,
        limit: int,
        after: ModelConfigListKey | None = None,
        include_disabled: bool = False,
    ) -> ModelSettingsPage:
        try:
            with self._catalog.transaction() as connection:
                page = self._catalog.list_page_with_public_configs_in_transaction(
                    connection,
                    limit=limit,
                    after=after,
                    include_disabled=include_disabled,
                )
                public_configs = tuple(item.public_config for item in page.items)
                masks = self._masks_for_public_configs(public_configs, connection)
        except ModelSettingsError:
            raise
        except SecretStoreError:
            raise ModelSettingsSecureStorageError() from None
        except ValueError:
            raise
        except Exception:
            raise ModelSettingsStorageError() from None
        return ModelSettingsPage(
            items=tuple(
                _safe_snapshot(
                    item.snapshot,
                    _mask_for(item.public_config, masks),
                )
                for item in page.items
            ),
            next_key=page.next_key,
        )

    async def test_connection(
        self,
        config_id: str,
        *,
        expected_revision: int,
    ) -> ConnectionTestResult:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ModelSettingsValidationError()
        try:
            with self._catalog.transaction() as connection:
                snapshot, public_config = (
                    self._catalog.get_snapshot_and_public_config_in_transaction(
                        connection, config_id
                    )
                )
            if (
                snapshot.revision != expected_revision
                or snapshot.status is ModelConfigStatus.DISABLED
            ):
                raise ModelSettingsConflict()
            if (
                public_config.provider is not ModelProviderKind.OLLAMA
                and self._secret_store is None
            ):
                raise ModelSettingsSecureStorageError()
            provider = self._provider_factory.create(public_config)
            try:
                deadline = min(10.0, public_config.timeout_seconds)
                async with asyncio.timeout(deadline):
                    provider_result = await provider.test_connection(
                        timeout_seconds=deadline
                    )
            except ModelCredentialUnavailableError:
                raise ModelSettingsSecureStorageError() from None
            except TimeoutError:
                provider_result = ModelConnectionResult(
                    connected=False,
                    provider=public_config.provider.value,
                    model=public_config.model,
                    error_code=ModelErrorCode.TIMEOUT,
                )
            except Exception:
                provider_result = ModelConnectionResult(
                    connected=False,
                    provider=public_config.provider.value,
                    model=public_config.model,
                    error_code=ModelErrorCode.INVALID_RESPONSE,
                )
            if (
                provider_result.provider != public_config.provider.value
                or provider_result.model != public_config.model
            ):
                provider_result = ModelConnectionResult(
                    connected=False,
                    provider=public_config.provider.value,
                    model=public_config.model,
                    error_code=ModelErrorCode.INVALID_RESPONSE,
                )
            if provider_result.error_code is ModelErrorCode.STORAGE:
                raise ModelSettingsSecureStorageError()
            updated = self._catalog.mark_test_result(
                config_id,
                expected_status=snapshot.status,
                expected_revision=snapshot.revision,
                succeeded=provider_result.connected,
                error_code=(
                    None
                    if provider_result.error_code is None
                    else provider_result.error_code.value
                ),
            )
            last_tested_at = updated.last_tested_at
            if last_tested_at is None:
                raise ModelSettingsStorageError()
        except ModelSettingsError:
            raise
        except SecretStoreError:
            raise ModelSettingsSecureStorageError() from None
        except ModelCatalogConflict:
            raise ModelSettingsConflict() from None
        except ModelNotFound:
            raise
        except Exception:
            raise ModelSettingsStorageError() from None
        return ConnectionTestResult(
            config_id=config_id,
            connected=provider_result.connected,
            provider=public_config.provider,
            model=public_config.model,
            error_code=provider_result.error_code,
            status=updated.status,
            revision=updated.revision,
            tested_at=last_tested_at,
            last_tested_at=last_tested_at,
        )

    def disable(
        self,
        config_id: str,
        *,
        expected_revision: int,
    ) -> ModelSettingsSnapshot:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ModelSettingsValidationError()
        current = self.get(config_id)
        try:
            updated = self._catalog.disable(
                config_id, expected_revision=expected_revision
            )
        except ModelCatalogConflict:
            raise ModelSettingsConflict() from None
        except ModelNotFound:
            raise
        except (ValueError, TypeError):
            raise ModelSettingsValidationError() from None
        except Exception:
            raise ModelSettingsStorageError() from None
        return _safe_snapshot(updated, current.masked_api_key)

    def _masks_for_public_configs(
        self,
        public_configs: tuple[AnalysisModelPublicConfig, ...],
        connection: Connection,
    ) -> dict[str, str]:
        references = tuple(
            dict.fromkeys(
                config.secret_reference_id
                for config in public_configs
                if config.secret_reference_id is not None
            )
        )
        if not references:
            return {}
        secret_store = self._secret_store
        if secret_store is None:
            raise ModelSettingsSecureStorageError()
        plaintext = secret_store.redaction_values_in_transaction(references, connection)
        self._register_redaction_values(*plaintext.values())
        return {name: mask_secret(value) for name, value in plaintext.items()}

    def _save_fresh_secret(
        self,
        plaintext: str,
        connection: Connection,
    ) -> str:
        if self._secret_store is None:
            raise ModelSettingsSecureStorageError()
        secret_reference = _new_secret_reference()
        if self._secret_store.has_secret_in_transaction(secret_reference, connection):
            raise ModelSettingsConflict()
        self._secret_store.create_secret_in_transaction(
            secret_reference, plaintext, connection
        )
        return secret_reference


def _new_secret_reference() -> str:
    return f"{MODEL_API_KEY_SECRET_NAME}_{secrets.token_hex(16)}"


def _public_config(
    update: ModelConfigUpdate,
    secret_reference: str | None,
) -> AnalysisModelPublicConfig:
    return AnalysisModelPublicConfig(
        provider=update.provider,
        base_url=_resolved_base_url(update),
        model=update.model,
        temperature=update.temperature,
        timeout_seconds=update.timeout_seconds,
        max_output_tokens=update.max_output_tokens,
        secret_reference_id=secret_reference,
        api_key_configured=secret_reference is not None,
    )


def _safe_snapshot(
    snapshot: AnalysisModelConfigSnapshot,
    masked: str | None,
) -> ModelSettingsSnapshot:
    return ModelSettingsSnapshot(
        id=snapshot.id,
        public_config_hash=snapshot.public_config_hash,
        display_name=snapshot.display_name,
        provider=snapshot.provider,
        model=snapshot.model,
        base_url=snapshot.base_url,
        temperature=snapshot.temperature,
        timeout_seconds=snapshot.timeout_seconds,
        max_output_tokens=snapshot.max_output_tokens,
        api_key_configured=snapshot.api_key_configured,
        masked_api_key=masked,
        supersedes_id=snapshot.supersedes_id,
        status=snapshot.status,
        revision=snapshot.revision,
        verified_at=snapshot.verified_at,
        last_tested_at=snapshot.last_tested_at,
        error_code=snapshot.error_code,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _mask_for(
    public_config: AnalysisModelPublicConfig,
    masks: dict[str, str],
) -> str | None:
    reference = public_config.secret_reference_id
    if reference is None:
        return None
    try:
        return masks[reference]
    except KeyError:
        raise ModelSettingsStorageError() from None
