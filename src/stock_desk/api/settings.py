"""Strict, secret-safe API and persistence for market source configuration."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, cast

from alembic.util.exc import CommandError as AlembicCommandError
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from filelock import Timeout as FileLockTimeout
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from sqlalchemy import Engine, case, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from stock_desk.config import Settings
from stock_desk.market.diagnostics import (
    DiagnosticProviderFactory,
    SourceDiagnostic,
    default_diagnostic_provider_factory,
    diagnose_source,
    unavailable_diagnostic,
)
from stock_desk.market.types import (
    CapabilityState,
    CONFIGURABLE_SOURCE_PROVIDER_IDS,
    FailureReason,
    ProviderId,
)
from stock_desk.security.secrets import (
    mask_secret,
    SecretConfigurationError,
    SecretDecryptionError,
    SecretNotFoundError,
    SecretStore,
    SecretStorageError,
)
from stock_desk.security.persistence import scrub_persisted_secrets_in_transaction
from stock_desk.security.redaction import LogSecretLease, scoped_log_redaction
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
    create_engine_for_url,
    migrate,
)
from stock_desk.storage.models import AppSetting, MarketDataset


PUBLIC_SOURCE_SETTINGS_KEY = "public.market.source_settings.v1"
_PUBLIC_SETTINGS_MAX_BYTES = 16_384
_TUSHARE_SECRET_NAME = "tushare_token"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourcePriorities(_SettingsModel):
    daily_bars: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
        ProviderId.TDX_LOCAL,
        ProviderId.EASTMONEY,
    )
    weekly_bars: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
        ProviderId.EASTMONEY,
    )
    minute_bars: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.BAOSTOCK,
        ProviderId.EASTMONEY,
    )
    instruments: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
        ProviderId.BAOSTOCK,
        ProviderId.EASTMONEY,
    )
    trading_calendar: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.BAOSTOCK,
        ProviderId.EASTMONEY,
    )
    execution_status: tuple[ProviderId, ...] = (ProviderId.TUSHARE,)
    fundamentals: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
    )
    announcements: tuple[ProviderId, ...] = (
        ProviderId.TUSHARE,
        ProviderId.AKSHARE,
    )
    news: tuple[ProviderId, ...] = (ProviderId.AKSHARE,)

    @field_validator("*", mode="before")
    @classmethod
    def decode_json_order(cls, value: object) -> tuple[ProviderId, ...]:
        if type(value) not in {list, tuple}:
            raise ValueError("source priority must be a JSON array")
        decoded: list[ProviderId] = []
        for item in cast(list[object] | tuple[object, ...], value):
            if type(item) is not str:
                raise ValueError("source priority contains an invalid provider")
            try:
                decoded.append(ProviderId(item))
            except ValueError:
                raise ValueError(
                    "source priority contains an invalid provider"
                ) from None
        return tuple(decoded)

    @field_validator("*")
    @classmethod
    def validate_order(cls, value: tuple[ProviderId, ...]) -> tuple[ProviderId, ...]:
        if not value or len(value) > len(ProviderId):
            raise ValueError("source priority must be nonempty and bounded")
        if len(value) != len(frozenset(value)):
            raise ValueError("source priority cannot contain duplicates")
        if not frozenset(value).issubset(CONFIGURABLE_SOURCE_PROVIDER_IDS):
            raise ValueError(
                "source priority contains a provider that is not configurable"
            )
        return value

    @model_validator(mode="after")
    def validate_usable_sources(self) -> SourcePriorities:
        from stock_desk.analysis.snapshot import ResearchSectionKind
        from stock_desk.analysis.sources.routing import supported_research_sources

        usable = {
            "daily_bars": frozenset(
                {
                    ProviderId.TUSHARE,
                    ProviderId.AKSHARE,
                    ProviderId.BAOSTOCK,
                    ProviderId.TDX_LOCAL,
                }
            ),
            "weekly_bars": frozenset(
                {ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK}
            ),
            "minute_bars": frozenset({ProviderId.TUSHARE, ProviderId.BAOSTOCK}),
            "instruments": frozenset(
                {ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK}
            ),
            "trading_calendar": frozenset({ProviderId.TUSHARE, ProviderId.BAOSTOCK}),
            "execution_status": frozenset({ProviderId.TUSHARE}),
            "fundamentals": supported_research_sources(
                ResearchSectionKind.FUNDAMENTALS
            ),
            "announcements": supported_research_sources(
                ResearchSectionKind.ANNOUNCEMENTS
            ),
            "news": supported_research_sources(ResearchSectionKind.NEWS),
        }
        for field_name, usable_sources in usable.items():
            configured = cast(tuple[ProviderId, ...], getattr(self, field_name))
            if not usable_sources.intersection(configured):
                raise ValueError(f"{field_name} priority has no usable source")
        return self


class _LegacyV1SourcePriorities(_SettingsModel):
    """Exact six-category shape persisted before research source routing."""

    daily_bars: tuple[ProviderId, ...]
    weekly_bars: tuple[ProviderId, ...]
    minute_bars: tuple[ProviderId, ...]
    instruments: tuple[ProviderId, ...]
    trading_calendar: tuple[ProviderId, ...]
    execution_status: tuple[ProviderId, ...]

    @field_validator("*", mode="before")
    @classmethod
    def decode_json_order(cls, value: object) -> tuple[ProviderId, ...]:
        return SourcePriorities.decode_json_order(value)

    @field_validator("*")
    @classmethod
    def validate_order(cls, value: tuple[ProviderId, ...]) -> tuple[ProviderId, ...]:
        return SourcePriorities.validate_order(value)

    @model_validator(mode="after")
    def validate_usable_sources(self) -> _LegacyV1SourcePriorities:
        usable = {
            "daily_bars": frozenset(
                {
                    ProviderId.TUSHARE,
                    ProviderId.AKSHARE,
                    ProviderId.BAOSTOCK,
                    ProviderId.TDX_LOCAL,
                }
            ),
            "weekly_bars": frozenset(
                {ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK}
            ),
            "minute_bars": frozenset({ProviderId.TUSHARE, ProviderId.BAOSTOCK}),
            "instruments": frozenset(
                {ProviderId.TUSHARE, ProviderId.AKSHARE, ProviderId.BAOSTOCK}
            ),
            "trading_calendar": frozenset({ProviderId.TUSHARE, ProviderId.BAOSTOCK}),
            "execution_status": frozenset({ProviderId.TUSHARE}),
        }
        for field_name, usable_sources in usable.items():
            configured = cast(tuple[ProviderId, ...], getattr(self, field_name))
            if not usable_sources.intersection(configured):
                raise ValueError(f"{field_name} priority has no usable source")
        return self


class _LegacyV1PublicSourceSettings(_SettingsModel):
    priorities: _LegacyV1SourcePriorities
    tdx_path: str | None

    @field_validator("tdx_path")
    @classmethod
    def validate_tdx_path(cls, value: str | None) -> str | None:
        return PublicSourceSettings.validate_tdx_path(value)


class PublicSourceSettings(_SettingsModel):
    priorities: SourcePriorities = Field(default_factory=SourcePriorities)
    tdx_path: str | None = None

    @field_validator("tdx_path")
    @classmethod
    def validate_tdx_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            len(value) < 4
            or len(value) > 2_048
            or value != value.strip()
            or not Path(value).is_absolute()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("TDX path must be a bounded absolute path")
        return value


class TushareSourceStatus(_SettingsModel):
    source: ProviderId
    configured: bool
    secure_storage_available: bool
    masked_hint: str | None


class SourceSettingsResponse(_SettingsModel):
    priorities: SourcePriorities
    tdx_path: str | None
    tushare: TushareSourceStatus


class TushareSourceUpdateRequest(_SettingsModel):
    token: SecretStr | None = Field(
        default=None,
        min_length=4,
        max_length=4_096,
        json_schema_extra={"writeOnly": True},
    )


class SettingsErrorResponse(_SettingsModel):
    code: str


class PublicSettingsCorrupt(RuntimeError):
    """Stored public source settings do not match the canonical contract."""


class SecureStorageUnavailable(RuntimeError):
    """The configured local secret store cannot safely serve this request."""


class SourceSettingsStorageError(RuntimeError):
    """The settings database no longer matches its frozen identity."""


class SourceSettingsPreflightError(ValueError):
    """A candidate source configuration failed its provider preflight."""


@dataclass(frozen=True, slots=True)
class RuntimeSourceSettings:
    """Secret-safe immutable configuration used by one update invocation."""

    priorities: SourcePriorities
    configuration_fingerprint: str
    _tushare_token: str | None = field(repr=False)
    _tdx_path: Path | None = field(repr=False)

    def credentials_for(self, source: ProviderId) -> tuple[str | None, Path | None]:
        if source is ProviderId.TUSHARE:
            return self._tushare_token, None
        if source is ProviderId.TDX_LOCAL:
            return None, self._tdx_path
        return None, None

    def redaction_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self._tushare_token,
                str(self._tdx_path) if self._tdx_path is not None else None,
            )
            if value is not None
        )


def _canonical_public(settings: PublicSourceSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _PUBLIC_SETTINGS_MAX_BYTES:
        raise ValueError("public source settings exceed the storage limit")
    return encoded


def _canonical_legacy_public(settings: _LegacyV1PublicSourceSettings) -> str:
    encoded = json.dumps(
        settings.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > _PUBLIC_SETTINGS_MAX_BYTES:
        raise ValueError("public source settings exceed the storage limit")
    return encoded


def _normalize_stored_provenance_only_sources(stored: str) -> str | None:
    """Remove the one historically accepted provenance-only source from canonical JSON."""
    try:
        decoded = json.loads(stored)
        if (
            json.dumps(
                decoded,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            != stored
        ):
            return None
    except (TypeError, ValueError):
        return None
    if type(decoded) is not dict:
        return stored
    priorities = decoded.get("priorities")
    if type(priorities) is not dict:
        return stored
    for field_name, order in priorities.items():
        if type(order) is list:
            priorities[field_name] = [
                source for source in order if source != ProviderId.STOCK_DESK_DEMO.value
            ]
    return json.dumps(
        decoded,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


class SourceSettingsServices:
    """One database-bound boundary for public settings, secrets, and diagnostics."""

    def __init__(
        self,
        *,
        engine: Engine,
        settings: Settings,
        diagnostic_factory: DiagnosticProviderFactory | None = None,
        clock: Callable[[], datetime] = _utc_now,
        _owns_engine: bool = False,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._diagnostic_factory = (
            diagnostic_factory
            if diagnostic_factory is not None
            else default_diagnostic_provider_factory
        )
        self._clock = clock
        self._owns_engine = _owns_engine
        self._state_lock = RLock()
        self._compromised = False
        self._closed = False
        self._configuration_revision = 0
        self._leased_token: str | None = None
        self._previous_leased_token: str | None = None
        self._leased_tdx_path: str | None = None
        self._previous_leased_tdx_path: str | None = None
        self._secret_lease = LogSecretLease()
        try:
            with engine.connect() as connection:
                self._database_identity: DatabaseIdentity = (
                    connection_database_identity(connection)
                )
        except (DatabaseIdentityError, OSError, SQLAlchemyError):
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None
        self._hydrate_configured_redaction()

    def __repr__(self) -> str:
        return "SourceSettingsServices(configured=True)"

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    @classmethod
    def open(
        cls,
        *,
        database_url: str,
        settings: Settings,
        diagnostic_factory: DiagnosticProviderFactory | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> SourceSettingsServices:
        try:
            migrate(database_url)
        except (
            AlembicCommandError,
            FileLockTimeout,
            OSError,
            RuntimeError,
            SQLAlchemyError,
            ValueError,
        ):
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None
        try:
            engine = create_engine_for_url(database_url)
        except (OSError, RuntimeError, SQLAlchemyError, ValueError):
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None
        try:
            return cls(
                engine=engine,
                settings=settings,
                diagnostic_factory=diagnostic_factory,
                clock=clock,
                _owns_engine=True,
            )
        except SourceSettingsStorageError:
            engine.dispose()
            raise
        except BaseException:
            engine.dispose()
            raise

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._leased_token = None
            self._previous_leased_token = None
            self._leased_tdx_path = None
            self._previous_leased_tdx_path = None
            self._secret_lease.close()
            owns_engine = self._owns_engine
        if owns_engine:
            self._engine.dispose()

    def read_public(self) -> PublicSourceSettings:
        with self._state_lock:
            self._ensure_available()
            return self._read_public_locked()

    def _read_public_locked(self) -> PublicSourceSettings:
        with self._checked_connection() as connection:
            stored = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == PUBLIC_SOURCE_SETTINGS_KEY
                )
            ).scalar_one_or_none()
        resolved = self._decode_public_stored(stored)
        self._set_leased_tdx_path(resolved.tdx_path)
        return resolved

    def _decode_public_stored(self, stored: object) -> PublicSourceSettings:
        if stored is None:
            return PublicSourceSettings()
        if (
            not isinstance(stored, str)
            or len(stored.encode("utf-8")) > _PUBLIC_SETTINGS_MAX_BYTES
        ):
            raise PublicSettingsCorrupt("Stored source settings are invalid")
        normalized = _normalize_stored_provenance_only_sources(stored)
        if normalized is None:
            raise PublicSettingsCorrupt("Stored source settings are invalid")
        try:
            decoded = PublicSourceSettings.model_validate_json(normalized)
        except Exception:
            decoded = None
        if decoded is not None and _canonical_public(decoded) == normalized:
            return decoded

        try:
            legacy = _LegacyV1PublicSourceSettings.model_validate_json(normalized)
        except Exception:
            raise PublicSettingsCorrupt("Stored source settings are invalid") from None
        if _canonical_legacy_public(legacy) != normalized:
            raise PublicSettingsCorrupt("Stored source settings are invalid")
        try:
            priorities = SourcePriorities.model_validate(
                legacy.priorities.model_dump(mode="json")
            )
            return PublicSourceSettings(
                priorities=priorities,
                tdx_path=legacy.tdx_path,
            )
        except Exception:
            raise PublicSettingsCorrupt("Stored source settings are invalid") from None

    def save_public(
        self, value: PublicSourceSettings | Mapping[str, Any]
    ) -> PublicSourceSettings:
        with self._state_lock:
            self._ensure_available()
            return self._save_public_locked(value)

    def _save_public_locked(
        self, value: PublicSourceSettings | Mapping[str, Any]
    ) -> PublicSourceSettings:
        serialized: object = (
            value.model_dump(mode="json")
            if isinstance(value, PublicSourceSettings)
            else value
        )
        try:
            validated = PublicSourceSettings.model_validate_json(
                json.dumps(serialized, allow_nan=False)
            )
        except Exception as error:
            raise ValueError("Public source settings are invalid") from error
        if validated.tdx_path is not None:
            with scoped_log_redaction(validated.tdx_path):
                diagnostic = diagnose_source(
                    ProviderId.TDX_LOCAL,
                    token=None,
                    tdx_path=Path(validated.tdx_path),
                    factory=default_diagnostic_provider_factory,
                    clock=self._clock,
                )
            if diagnostic.status is not CapabilityState.AVAILABLE:
                raise SourceSettingsPreflightError(
                    "TDX source configuration failed preflight"
                )
        encoded = _canonical_public(validated)
        now = self._clock()
        statement = sqlite_insert(AppSetting).values(
            key=PUBLIC_SOURCE_SETTINGS_KEY,
            encrypted_value=encoded,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={
                "encrypted_value": encoded,
                "updated_at": case(
                    (AppSetting.updated_at > now, AppSetting.updated_at),
                    else_=now,
                ),
            },
        )
        with self._checked_begin() as connection:
            connection.execute(statement)
        self._configuration_revision += 1
        self._set_leased_tdx_path(validated.tdx_path)
        return validated

    def _secret_store(self) -> SecretStore:
        self._ensure_available()
        try:
            return SecretStore(
                self._engine,
                self._settings,
                expected_database_identity=self._database_identity,
            )
        except SecretConfigurationError:
            raise SecureStorageUnavailable("Secure storage is unavailable") from None
        except SecretStorageError:
            self._poison()
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None

    def _has_tushare_secret(self) -> bool:
        with self._checked_connection() as connection:
            return (
                connection.execute(
                    select(AppSetting.key).where(
                        AppSetting.key == f"secret.{_TUSHARE_SECRET_NAME}"
                    )
                ).scalar_one_or_none()
                is not None
            )

    def tushare_status(self) -> TushareSourceStatus:
        with self._state_lock:
            self._ensure_available()
            return self._tushare_status_locked()

    def _tushare_status_locked(self) -> TushareSourceStatus:
        configured = self._has_tushare_secret()
        try:
            store = self._secret_store()
        except SecureStorageUnavailable:
            self._set_leased_token(None)
            return TushareSourceStatus(
                source=ProviderId.TUSHARE,
                configured=configured,
                secure_storage_available=False,
                masked_hint=None,
            )
        masked_hint: str | None = None
        storage_available = True
        if configured:
            try:
                plaintext = store.read_secret_for_server_call(_TUSHARE_SECRET_NAME)
                self._set_leased_token(plaintext)
                masked_hint = mask_secret(plaintext)
            except (SecretDecryptionError, SecretNotFoundError):
                self._set_leased_token(None)
                storage_available = False
            except SecretStorageError:
                self._poison()
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                ) from None
        else:
            self._set_leased_token(None)
        return TushareSourceStatus(
            source=ProviderId.TUSHARE,
            configured=configured,
            secure_storage_available=storage_available,
            masked_hint=masked_hint,
        )

    def update_tushare(
        self, request: TushareSourceUpdateRequest
    ) -> TushareSourceStatus:
        with self._state_lock:
            self._ensure_available()
            return self._update_tushare_locked(request)

    def _update_tushare_locked(
        self, request: TushareSourceUpdateRequest
    ) -> TushareSourceStatus:
        if request.token is not None:
            plaintext = request.token.get_secret_value()
            try:
                store = self._secret_store()
                with self._checked_begin() as connection:
                    previous = (
                        store.read_secret_for_server_call_in_transaction(
                            _TUSHARE_SECRET_NAME,
                            connection,
                        )
                        if store.has_secret_in_transaction(
                            _TUSHARE_SECRET_NAME, connection
                        )
                        else None
                    )
                    self._set_leased_token(previous)
                    self._set_leased_token(plaintext)
                    scrub_persisted_secrets_in_transaction(
                        connection,
                        tuple(
                            value
                            for value in (previous, plaintext)
                            if value is not None
                        ),
                    )
                    store.save_secret_in_transaction(
                        _TUSHARE_SECRET_NAME,
                        plaintext,
                        connection,
                    )
            except SecretStorageError:
                self._poison()
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                ) from None
            except SecureStorageUnavailable:
                raise
            except SourceSettingsStorageError:
                raise
            except Exception:
                self._poison()
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                ) from None
            self._configuration_revision += 1
            self._set_leased_token(plaintext)
        return self.tushare_status()

    def _hydrate_configured_redaction(self) -> None:
        try:
            store = SecretStore(
                self._engine,
                self._settings,
                expected_database_identity=self._database_identity,
            )
        except SecretConfigurationError:
            return
        try:
            with self._engine.begin() as connection:
                if not store.has_secret_in_transaction(
                    _TUSHARE_SECRET_NAME, connection
                ):
                    return
                plaintext = store.read_secret_for_server_call_in_transaction(
                    _TUSHARE_SECRET_NAME,
                    connection,
                )
                self._set_leased_token(plaintext)
                scrub_persisted_secrets_in_transaction(connection, (plaintext,))
        except (SecretDecryptionError, SecretNotFoundError, SecretStorageError):
            self._poison()
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None
        except SQLAlchemyError:
            self._poison()
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None
        except Exception:
            self._poison()
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None

    def response(self) -> SourceSettingsResponse:
        with self._state_lock:
            self._ensure_available()
            return self._response_locked()

    def _response_locked(self) -> SourceSettingsResponse:
        public = self.read_public()
        return SourceSettingsResponse(
            priorities=public.priorities,
            tdx_path=public.tdx_path,
            tushare=self.tushare_status(),
        )

    def runtime_snapshot(self) -> RuntimeSourceSettings:
        """Read a fresh public/secret snapshot without exposing it through an API DTO."""
        with self._state_lock:
            self._ensure_available()
            store: SecretStore | None
            try:
                store = self._secret_store()
            except SecureStorageUnavailable:
                store = None
            token: str | None = None
            with self._checked_connection() as connection:
                connection.exec_driver_sql("BEGIN")
                stored_rows: dict[str, str] = {
                    key: value
                    for key, value in connection.execute(
                        select(AppSetting.key, AppSetting.encrypted_value).where(
                            AppSetting.key.in_(
                                (
                                    PUBLIC_SOURCE_SETTINGS_KEY,
                                    f"secret.{_TUSHARE_SECRET_NAME}",
                                )
                            )
                        )
                    ).tuples()
                }
                public = self._decode_public_stored(
                    stored_rows.get(PUBLIC_SOURCE_SETTINGS_KEY)
                )
                if store is not None:
                    try:
                        token = store.read_secret_for_server_call_in_transaction(
                            _TUSHARE_SECRET_NAME,
                            connection,
                        )
                    except (SecretDecryptionError, SecretNotFoundError):
                        token = None
                    except SecretStorageError:
                        self._poison()
                        raise SourceSettingsStorageError(
                            "Source settings storage is unavailable"
                        ) from None
                connection.rollback()
            self._set_leased_token(token)
            tdx_path = Path(public.tdx_path) if public.tdx_path is not None else None
            self._set_leased_tdx_path(public.tdx_path)
            encoded_fingerprint = json.dumps(
                {
                    "public": public.model_dump(mode="json"),
                    "tushare_secret": hashlib.sha256(
                        str(
                            stored_rows.get(f"secret.{_TUSHARE_SECRET_NAME}", "")
                        ).encode("utf-8")
                    ).hexdigest(),
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return RuntimeSourceSettings(
                priorities=public.priorities,
                configuration_fingerprint=(
                    f"sha256:{hashlib.sha256(encoded_fingerprint).hexdigest()}"
                ),
                _tushare_token=token,
                _tdx_path=tdx_path,
            )

    def diagnose(self, source: ProviderId) -> SourceDiagnostic:
        with self._state_lock:
            self._ensure_available()
            prepared = self._prepare_diagnostic_locked(source)
        if isinstance(prepared, SourceDiagnostic):
            return prepared
        revision, token, tdx_path, active_secrets = prepared
        with scoped_log_redaction(*active_secrets):
            diagnostic = diagnose_source(
                source,
                token=token,
                tdx_path=tdx_path,
                factory=self._diagnostic_factory,
                clock=self._clock,
            )
        with self._state_lock:
            self._ensure_available()
            if revision != self._configuration_revision:
                return unavailable_diagnostic(
                    source,
                    reason=FailureReason.TRANSIENT_FAILURE,
                    detail="Source configuration changed during diagnostic",
                    checked_at=self._clock(),
                )
            return self._merge_diagnostic_evidence(diagnostic)

    def _prepare_diagnostic_locked(
        self, source: ProviderId
    ) -> tuple[int, str | None, Path | None, tuple[str, ...]] | SourceDiagnostic:
        token: str | None = None
        public = self._read_public_locked()
        if source is ProviderId.TUSHARE:
            try:
                token = self._secret_store().read_secret_for_server_call(
                    _TUSHARE_SECRET_NAME
                )
                self._set_leased_token(token)
            except SecretNotFoundError:
                self._set_leased_token(None)
                return self._merge_diagnostic_evidence(
                    unavailable_diagnostic(
                        source,
                        reason=FailureReason.PERMISSION_DENIED,
                        detail="Tushare token is not configured",
                        checked_at=self._clock(),
                    )
                )
            except (SecureStorageUnavailable, SecretDecryptionError):
                self._set_leased_token(None)
                return self._merge_diagnostic_evidence(
                    unavailable_diagnostic(
                        source,
                        reason=FailureReason.PROVIDER_UNAVAILABLE,
                        detail="Secure token storage is unavailable",
                        checked_at=self._clock(),
                    )
                )
            except SecretStorageError:
                self._poison()
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                ) from None
        tdx_path = Path(public.tdx_path) if public.tdx_path is not None else None
        active_secrets = tuple(
            value for value in (token, public.tdx_path) if value is not None
        )
        return self._configuration_revision, token, tdx_path, active_secrets

    def _set_leased_token(self, value: str | None) -> None:
        with self._state_lock:
            if self._closed:
                return
            if value == self._leased_token:
                return
            if self._leased_token is not None:
                self._previous_leased_token = self._leased_token
            self._leased_token = value
            self._refresh_secret_lease_locked()

    def _set_leased_tdx_path(self, value: str | None) -> None:
        with self._state_lock:
            if self._closed:
                return
            if value == self._leased_tdx_path:
                return
            if self._leased_tdx_path is not None:
                self._previous_leased_tdx_path = self._leased_tdx_path
            self._leased_tdx_path = value
            self._refresh_secret_lease_locked()

    def _refresh_secret_lease_locked(self) -> None:
        self._secret_lease.replace(
            *(
                value
                for value in (
                    self._leased_token,
                    self._previous_leased_token,
                    self._leased_tdx_path,
                    self._previous_leased_tdx_path,
                )
                if value is not None
            )
        )

    def _merge_diagnostic_evidence(
        self, diagnostic: SourceDiagnostic
    ) -> SourceDiagnostic:
        try:
            with self._checked_connection() as connection:
                row = connection.execute(
                    select(
                        func.max(MarketDataset.created_at),
                        func.max(MarketDataset.data_cutoff),
                    ).where(MarketDataset.source == diagnostic.source.value)
                ).one()
        except SourceSettingsStorageError:
            raise
        except (OverflowError, TypeError, ValueError):
            raise SourceSettingsStorageError(
                "Cached diagnostic evidence is invalid"
            ) from None
        completed_at = self._validated_completion_time(
            self._clock(),
            not_before=diagnostic.last_checked.astimezone(timezone.utc),
        )
        provider_cutoff = (
            None
            if diagnostic.data_cutoff is None
            else self._validated_provider_cutoff(
                diagnostic.data_cutoff,
                checked_at=completed_at,
            )
        )
        raw_last_update, raw_data_cutoff = row
        if raw_last_update is None and raw_data_cutoff is None:
            return SourceDiagnostic.model_validate(
                {
                    **diagnostic.model_dump(),
                    "last_checked": completed_at,
                    "data_cutoff": provider_cutoff,
                }
            )
        if raw_last_update is None or raw_data_cutoff is None:
            raise SourceSettingsStorageError("Cached diagnostic evidence is incomplete")
        last_update = self._validated_evidence_time(
            raw_last_update,
            checked_at=completed_at,
        )
        data_cutoff = self._validated_evidence_time(
            raw_data_cutoff,
            checked_at=completed_at,
        )
        if data_cutoff > last_update:
            raise SourceSettingsStorageError(
                "Cached diagnostic evidence is inconsistent"
            )
        return SourceDiagnostic.model_validate(
            {
                **diagnostic.model_dump(),
                "last_checked": completed_at,
                "last_update": last_update,
                "data_cutoff": data_cutoff,
            }
        )

    @staticmethod
    def _validated_evidence_time(
        value: object,
        *,
        checked_at: datetime,
    ) -> datetime:
        if type(value) is not datetime:
            raise SourceSettingsStorageError("Cached diagnostic evidence is invalid")
        if value.tzinfo is None or value.utcoffset() is None:
            normalized = value.replace(tzinfo=timezone.utc)
        else:
            normalized = value.astimezone(timezone.utc)
        if normalized.year < 1990 or normalized > checked_at:
            raise SourceSettingsStorageError(
                "Cached diagnostic evidence is out of bounds"
            )
        return normalized

    @staticmethod
    def _validated_provider_cutoff(
        value: object,
        *,
        checked_at: datetime,
    ) -> datetime:
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise SourceSettingsStorageError("Provider cutoff is invalid")
        normalized = value.astimezone(timezone.utc)
        if normalized.year < 1990 or normalized > checked_at:
            raise SourceSettingsStorageError("Provider cutoff is out of bounds")
        return normalized

    @staticmethod
    def _validated_completion_time(
        value: object,
        *,
        not_before: datetime,
    ) -> datetime:
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise SourceSettingsStorageError("Diagnostic clock is invalid")
        normalized = value.astimezone(timezone.utc)
        if normalized.year < 1990 or normalized < not_before:
            raise SourceSettingsStorageError("Diagnostic clock regressed")
        return normalized

    def _ensure_available(self) -> None:
        with self._state_lock:
            if self._closed or self._compromised:
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                )

    def _poison(self) -> None:
        with self._state_lock:
            self._compromised = True

    def _validate_connection(self, connection: Connection) -> None:
        self._ensure_available()
        if connection.closed or connection.engine is not self._engine:
            self._poison()
            raise SourceSettingsStorageError(
                "Source settings database connection is invalid"
            )
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError:
            self._poison()
            raise SourceSettingsStorageError(
                "Source settings storage is unavailable"
            ) from None
        with self._state_lock:
            if self._closed or self._compromised:
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                )
            if identity != self._database_identity:
                self._compromised = True
                raise SourceSettingsStorageError(
                    "Source settings database identity changed"
                )

    @contextmanager
    def _checked_connection(self) -> Iterator[Connection]:
        with self._state_lock:
            self._ensure_available()
            try:
                with self._engine.connect() as connection:
                    self._validate_connection(connection)
                    yield connection
            except SourceSettingsStorageError:
                raise
            except SQLAlchemyError:
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                ) from None

    @contextmanager
    def _checked_begin(self) -> Iterator[Connection]:
        with self._checked_connection() as connection:
            try:
                with connection.begin():
                    yield connection
            except SourceSettingsStorageError:
                raise
            except SQLAlchemyError:
                raise SourceSettingsStorageError(
                    "Source settings storage is unavailable"
                ) from None


def get_source_settings_services(request: Request) -> SourceSettingsServices:
    provider = cast(
        Callable[[], SourceSettingsServices],
        request.app.state.source_settings_services_provider,
    )
    return provider()


SourceSettingsDependency = Annotated[
    SourceSettingsServices, Depends(get_source_settings_services)
]


def _error(code: str, status_code: int) -> JSONResponse:
    response = SettingsErrorResponse(code=code)
    return JSONResponse(
        status_code=status_code, content=response.model_dump(mode="json")
    )


async def source_settings_storage_exception_handler(
    _request: Request, _error_value: Exception
) -> JSONResponse:
    return _error(
        "settings_storage_unavailable",
        status.HTTP_503_SERVICE_UNAVAILABLE,
    )


router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/sources", response_model=SourceSettingsResponse)
def get_sources(
    services: SourceSettingsDependency,
) -> SourceSettingsResponse | JSONResponse:
    try:
        return services.response()
    except PublicSettingsCorrupt:
        return _error("settings_corrupt", status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.put("/sources", response_model=SourceSettingsResponse)
def put_sources(
    request: PublicSourceSettings,
    services: SourceSettingsDependency,
) -> SourceSettingsResponse | JSONResponse:
    try:
        services.save_public(request)
    except SourceSettingsPreflightError:
        return _error("tdx_preflight_failed", status.HTTP_422_UNPROCESSABLE_CONTENT)
    return services.response()


@router.get("/sources/{source}", response_model=TushareSourceStatus)
def get_source(
    source: ProviderId,
    services: SourceSettingsDependency,
) -> TushareSourceStatus | JSONResponse:
    if source is not ProviderId.TUSHARE:
        return _error("source_settings_not_supported", status.HTTP_404_NOT_FOUND)
    return services.tushare_status()


@router.put("/sources/{source}", response_model=TushareSourceStatus)
def put_source(
    source: ProviderId,
    request: TushareSourceUpdateRequest,
    services: SourceSettingsDependency,
) -> TushareSourceStatus | JSONResponse:
    if source is not ProviderId.TUSHARE:
        return _error("source_settings_not_supported", status.HTTP_404_NOT_FOUND)
    try:
        return services.update_tushare(request)
    except SecureStorageUnavailable:
        return _error(
            "secure_storage_unavailable",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )


@router.post("/sources/{source}/test", response_model=SourceDiagnostic)
def test_source(
    source: ProviderId,
    services: SourceSettingsDependency,
) -> SourceDiagnostic | JSONResponse:
    try:
        return services.diagnose(source)
    except PublicSettingsCorrupt:
        return _error("settings_corrupt", status.HTTP_500_INTERNAL_SERVER_ERROR)


__all__ = [
    "PUBLIC_SOURCE_SETTINGS_KEY",
    "PublicSourceSettings",
    "SourcePriorities",
    "SourceSettingsServices",
    "SourceSettingsStorageError",
    "TushareSourceUpdateRequest",
    "router",
    "source_settings_storage_exception_handler",
]
