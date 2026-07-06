from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import re
from threading import RLock
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, case, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from stock_desk.config import Settings
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
)
from stock_desk.storage.models import AppSetting


_SECRET_KEY_PREFIX: Final = "secret."
_SECRET_NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MASK_SEPARATOR: Final = "•••••••"
_MAX_ENCRYPTED_SECRET_LENGTH: Final = 8_192


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SecretStoreError(Exception):
    """Base class for intentionally generic secret-store failures."""


class SecretConfigurationError(SecretStoreError):
    """The local encryption key is unavailable or unusable."""


class SecretValidationError(SecretStoreError):
    """A caller supplied an invalid public secret identifier or value."""


class SecretNotFoundError(SecretStoreError):
    """A requested local secret has not been configured."""


class SecretDecryptionError(SecretStoreError):
    """A stored token cannot be authenticated and decrypted."""


class SecretStorageError(SecretStoreError):
    """The secret database connection no longer matches its frozen identity."""


def mask_secret(value: str) -> str:
    """Return a recognizable but never plaintext representation of a secret."""
    if not isinstance(value, str) or not value:
        raise SecretValidationError("Secret value is invalid")
    if len(value) <= 8:
        candidate = _MASK_SEPARATOR
    else:
        candidate = f"{value[:4]}{_MASK_SEPARATOR}{value[-4:]}"
    if value in candidate:
        return "[MASKED]"
    return candidate


def _validated_name(name: str) -> str:
    if not isinstance(name, str) or _SECRET_NAME_PATTERN.fullmatch(name) is None:
        raise SecretValidationError("Secret name is invalid")
    return name


def _setting_key(name: str) -> str:
    return f"{_SECRET_KEY_PREFIX}{_validated_name(name)}"


class SecretStore:
    """Encrypt small local credentials in the existing application settings table."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        expected_database_identity: DatabaseIdentity | None = None,
    ) -> None:
        configured = settings.master_key
        try:
            if configured is None or not configured.get_secret_value():
                raise ValueError
            self._fernet = Fernet(configured.get_secret_value().encode("ascii"))
        except (TypeError, ValueError, UnicodeEncodeError):
            raise SecretConfigurationError(
                "STOCK_DESK_MASTER_KEY is missing or invalid"
            ) from None
        self._engine = engine
        self._state_lock = RLock()
        self._compromised = False
        try:
            with engine.connect() as connection:
                database_identity = connection_database_identity(connection)
        except (DatabaseIdentityError, SQLAlchemyError):
            raise SecretStorageError("Secret storage is unavailable") from None
        if (
            expected_database_identity is not None
            and database_identity != expected_database_identity
        ):
            raise SecretStorageError("Secret storage database identity changed")
        self._database_identity = database_identity

    def __repr__(self) -> str:
        return "SecretStore(configured=True)"

    def save_secret(self, name: str, value: str) -> None:
        with self._state_lock:
            self._ensure_available()
            self._save_secret_locked(name, value)

    def _save_secret_locked(self, name: str, value: str) -> None:
        key = _setting_key(name)
        if not isinstance(value, str) or not value:
            raise SecretValidationError("Secret value is invalid")
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        now = _utc_now()
        statement = sqlite_insert(AppSetting).values(
            key=key,
            encrypted_value=token,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={
                "encrypted_value": token,
                "updated_at": case(
                    (AppSetting.updated_at > now, AppSetting.updated_at),
                    else_=now,
                ),
            },
        )
        with self._checked_begin() as connection:
            connection.execute(statement)

    def has_secret(self, name: str) -> bool:
        with self._state_lock:
            self._ensure_available()
            return self._has_secret_locked(name)

    def _has_secret_locked(self, name: str) -> bool:
        key = _setting_key(name)
        with self._checked_connection() as connection:
            return (
                connection.execute(
                    select(AppSetting.key).where(AppSetting.key == key)
                ).scalar_one_or_none()
                is not None
            )

    def read_secret_for_server_call(self, name: str) -> str:
        with self._state_lock:
            self._ensure_available()
            return self._read_secret_locked(name)

    def _read_secret_locked(self, name: str) -> str:
        token = self._read_token(name)
        return self._decrypt_token(token)

    def _decrypt_token(self, token: str) -> str:
        try:
            plaintext = self._fernet.decrypt(token.encode("ascii"))
            return plaintext.decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError):
            raise SecretDecryptionError(
                "Stored secret could not be decrypted"
            ) from None

    def read_secret_for_server_call_in_transaction(
        self,
        name: str,
        connection: Connection,
    ) -> str:
        """Decrypt a secret from the caller's already-open database snapshot."""
        with self._state_lock:
            self._ensure_available()
            if (
                connection.closed
                or connection.engine is not self._engine
                or not connection.in_transaction()
            ):
                raise SecretStorageError("Secret storage connection is invalid")
            self._validate_connection(connection)
            token = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == _setting_key(name)
                )
            ).scalar_one_or_none()
            if token is None:
                raise SecretNotFoundError("Secret is not configured")
            if (
                type(token) is not str
                or not token
                or len(token) > _MAX_ENCRYPTED_SECRET_LENGTH
                or not token.isascii()
            ):
                raise SecretDecryptionError("Stored secret could not be decrypted")
            return self._decrypt_token(token)

    def masked_secret(self, name: str) -> str:
        with self._state_lock:
            self._ensure_available()
            return mask_secret(self._read_secret_locked(name))

    def _read_token(self, name: str) -> str:
        key = _setting_key(name)
        with self._checked_connection() as connection:
            token = connection.execute(
                select(AppSetting.encrypted_value).where(AppSetting.key == key)
            ).scalar_one_or_none()
        if token is None:
            raise SecretNotFoundError("Secret is not configured")
        if (
            type(token) is not str
            or not token
            or len(token) > _MAX_ENCRYPTED_SECRET_LENGTH
            or not token.isascii()
        ):
            raise SecretDecryptionError("Stored secret could not be decrypted")
        return token

    def _validate_connection(self, connection: Connection) -> None:
        self._ensure_available()
        if connection.closed or connection.engine is not self._engine:
            self._poison()
            raise SecretStorageError("Secret storage connection is invalid")
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError:
            self._poison()
            raise SecretStorageError("Secret storage is unavailable") from None
        with self._state_lock:
            if self._compromised:
                raise SecretStorageError("Secret storage is unavailable")
            if identity != self._database_identity:
                self._compromised = True
                raise SecretStorageError("Secret storage database identity changed")

    def _ensure_available(self) -> None:
        with self._state_lock:
            if self._compromised:
                raise SecretStorageError("Secret storage is unavailable")

    def _poison(self) -> None:
        with self._state_lock:
            self._compromised = True

    @contextmanager
    def _checked_connection(self) -> Iterator[Connection]:
        with self._state_lock:
            self._ensure_available()
            try:
                with self._engine.connect() as connection:
                    self._validate_connection(connection)
                    yield connection
            except SecretStorageError:
                raise
            except SQLAlchemyError:
                raise SecretStorageError("Secret storage is unavailable") from None

    @contextmanager
    def _checked_begin(self) -> Iterator[Connection]:
        with self._checked_connection() as connection:
            try:
                with connection.begin():
                    yield connection
            except SecretStorageError:
                raise
            except SQLAlchemyError:
                raise SecretStorageError("Secret storage is unavailable") from None
