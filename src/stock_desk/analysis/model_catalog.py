from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import re
from threading import RLock
from typing import NoReturn, cast

from pydantic import ValidationError
from sqlalchemy import Engine, insert, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql import Select

from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
)
from stock_desk.storage.models import AnalysisModelConfig


_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_CONFIG_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        aware = (
            value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        )
        return aware.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        aware = (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
        return aware.astimezone(timezone.utc)
    raise ModelCatalogCorruption("Model configuration timestamp is invalid")


def _optional_utc(value: object) -> datetime | None:
    return None if value is None else _utc(value)


def _canonical_config(config: AnalysisModelPublicConfig) -> str:
    return json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_hash(payload: str) -> str:
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _display_name(value: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Model configuration display name is invalid")
    return value


class ModelConfigStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"
    DISABLED = "disabled"


class ModelCatalogError(RuntimeError):
    """Base class for stable model catalog failures."""


class ModelCatalogConflict(ModelCatalogError):
    """A catalog mutation lost a race or conflicts with immutable content."""


class ModelCatalogCorruption(ModelCatalogError):
    """Stored model configuration content does not match its identity."""


class ModelCatalogClosed(ModelCatalogError):
    """The catalog has been closed or its database identity changed."""


class ModelNotFound(ModelCatalogError):
    """The requested model configuration does not exist."""


class ModelNotVerified(ModelCatalogError):
    """The requested model configuration is not enabled and verified."""


@dataclass(frozen=True, slots=True)
class AnalysisModelConfigSnapshot:
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
    supersedes_id: str | None
    status: ModelConfigStatus
    revision: int
    verified_at: datetime | None
    last_tested_at: datetime | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ModelConfigListKey:
    id: str


@dataclass(frozen=True, slots=True)
class ModelConfigPage:
    items: tuple[AnalysisModelConfigSnapshot, ...]
    next_key: ModelConfigListKey | None


@dataclass(frozen=True, slots=True, repr=False)
class ModelConfigInternalEntry:
    snapshot: AnalysisModelConfigSnapshot
    public_config: AnalysisModelPublicConfig


@dataclass(frozen=True, slots=True, repr=False)
class ModelConfigInternalPage:
    items: tuple[ModelConfigInternalEntry, ...]
    next_key: ModelConfigListKey | None


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedModelExecution:
    model_config_id: str
    public_config: AnalysisModelPublicConfig

    def __repr__(self) -> str:
        return f"VerifiedModelExecution(model_config_id={self.model_config_id!r})"


@dataclass(frozen=True, slots=True, repr=False)
class _StoredConfig:
    snapshot: AnalysisModelConfigSnapshot
    public_config: AnalysisModelPublicConfig


class AnalysisModelCatalog:
    """Database-bound immutable model execution configuration catalog."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = _utc_now,
        expected_database_identity: DatabaseIdentity | None = None,
        owns_engine: bool = True,
    ) -> None:
        self._engine = engine
        self._owns_engine = owns_engine
        self._clock = clock
        self._lock = RLock()
        self._closed = False
        try:
            with engine.connect() as connection:
                identity = connection_database_identity(connection)
        except (DatabaseIdentityError, SQLAlchemyError):
            raise ModelCatalogClosed("Model catalog database is unavailable") from None
        if (
            expected_database_identity is not None
            and identity != expected_database_identity
        ):
            raise ModelCatalogClosed("Model catalog database identity changed")
        self._database_identity = identity

    def __repr__(self) -> str:
        return "AnalysisModelCatalog(configured=True)"

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    def _ensure_open(self) -> None:
        if self._closed:
            raise ModelCatalogClosed("Model catalog is closed")

    def _validate_connection(self, connection: Connection) -> None:
        self._ensure_open()
        if connection.closed or connection.engine is not self._engine:
            self._closed = True
            raise ModelCatalogClosed("Model catalog connection is invalid")
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError:
            self._closed = True
            raise ModelCatalogClosed("Model catalog database is unavailable") from None
        if identity != self._database_identity:
            self._closed = True
            raise ModelCatalogClosed("Model catalog database identity changed")

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self._lock:
            self._ensure_open()
            try:
                with self._engine.connect() as connection:
                    self._validate_connection(connection)
                    yield connection
            except IntegrityError:
                raise
            except (OverflowError, ValueError):
                raise ModelCatalogCorruption(
                    "Stored model configuration is corrupted"
                ) from None
            except ModelCatalogError:
                raise
            except SQLAlchemyError:
                raise ModelCatalogError(
                    "Model catalog database operation failed"
                ) from None

    @contextmanager
    def _begin(self) -> Iterator[Connection]:
        with self._connection() as connection:
            try:
                with connection.begin():
                    yield connection
            except IntegrityError:
                raise
            except ModelCatalogError:
                raise
            except SQLAlchemyError:
                raise ModelCatalogError(
                    "Model catalog database operation failed"
                ) from None

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Yield a validated transaction for coordinated same-database writes."""
        with self._begin() as connection:
            yield connection

    def create(
        self,
        *,
        display_name: str,
        public_config: AnalysisModelPublicConfig,
    ) -> AnalysisModelConfigSnapshot:
        return self._create(
            display_name=display_name,
            public_config=public_config,
            supersedes_id=None,
        )

    def _create(
        self,
        *,
        display_name: str,
        public_config: AnalysisModelPublicConfig,
        supersedes_id: str | None,
    ) -> AnalysisModelConfigSnapshot:
        name = _display_name(display_name)
        try:
            with self._begin() as connection:
                snapshot = self.create_in_transaction(
                    connection,
                    display_name=name,
                    public_config=public_config,
                    supersedes_id=supersedes_id,
                )
        except IntegrityError:
            raise ModelCatalogConflict(
                "Model configuration conflicts with the catalog"
            ) from None
        return snapshot

    def create_in_transaction(
        self,
        connection: Connection,
        *,
        display_name: str,
        public_config: AnalysisModelPublicConfig,
        supersedes_id: str | None = None,
    ) -> AnalysisModelConfigSnapshot:
        """Insert immutable catalog content in a validated caller transaction."""
        self._validate_transaction_connection(connection)
        name = _display_name(display_name)
        if not isinstance(public_config, AnalysisModelPublicConfig):
            raise TypeError("public_config must be AnalysisModelPublicConfig")
        payload = _canonical_config(public_config)
        config_id = _content_hash(payload)
        now = _utc(self._clock())
        if supersedes_id is not None and self._row(connection, supersedes_id) is None:
            raise ModelNotFound("Model configuration was not found")
        returned = (
            connection.execute(
                insert(AnalysisModelConfig)
                .values(
                    id=config_id,
                    display_name=name,
                    provider=public_config.provider.value,
                    model=public_config.model,
                    public_config_json=payload,
                    public_config_hash=config_id,
                    secret_reference_id=public_config.secret_reference_id,
                    supersedes_id=supersedes_id,
                    status=ModelConfigStatus.UNVERIFIED.value,
                    revision=0,
                    verified_at=None,
                    last_tested_at=None,
                    error_code=None,
                    created_at=now,
                    updated_at=now,
                )
                .returning(*AnalysisModelConfig.__table__.columns)
            )
            .mappings()
            .one()
        )
        return _stored(returned).snapshot

    def get_public_config_in_transaction(
        self,
        connection: Connection,
        config_id: str,
    ) -> AnalysisModelPublicConfig:
        """Read execution content without exposing it through public snapshots."""
        self._validate_transaction_connection(connection)
        row = self._row(connection, config_id)
        if row is None:
            raise ModelNotFound("Model configuration was not found")
        return _stored(row).public_config

    def get_snapshot_and_public_config_in_transaction(
        self,
        connection: Connection,
        config_id: str,
    ) -> tuple[AnalysisModelConfigSnapshot, AnalysisModelPublicConfig]:
        """Read current catalog state and execution content from one snapshot."""
        self._validate_transaction_connection(connection)
        row = self._row(connection, config_id)
        if row is None:
            raise ModelNotFound("Model configuration was not found")
        stored = _stored(row)
        return stored.snapshot, stored.public_config

    def _validate_transaction_connection(self, connection: Connection) -> None:
        if (
            connection.closed
            or connection.engine is not self._engine
            or not connection.in_transaction()
        ):
            raise ModelCatalogClosed("Model catalog connection is invalid")
        self._validate_connection(connection)

    def create_successor(
        self,
        config_id: str,
        *,
        display_name: str,
        public_config: AnalysisModelPublicConfig,
    ) -> AnalysisModelConfigSnapshot:
        return self._create(
            display_name=display_name,
            public_config=public_config,
            supersedes_id=config_id,
        )

    def get(self, config_id: str) -> AnalysisModelConfigSnapshot:
        with self._connection() as connection:
            row = self._row(connection, config_id)
        if row is None:
            raise ModelNotFound("Model configuration was not found")
        return _stored(row).snapshot

    def list_page(
        self,
        *,
        limit: int,
        after: ModelConfigListKey | None = None,
        include_disabled: bool = False,
    ) -> ModelConfigPage:
        statement = _page_statement(
            limit=limit,
            after=after,
            include_disabled=include_disabled,
        )
        with self._connection() as connection:
            rows = tuple(connection.execute(statement).mappings())
        items = tuple(_stored(row).snapshot for row in rows[:limit])
        next_key = None
        if len(rows) > limit:
            last = items[-1]
            next_key = ModelConfigListKey(id=last.id)
        return ModelConfigPage(items=items, next_key=next_key)

    def list_page_with_public_configs_in_transaction(
        self,
        connection: Connection,
        *,
        limit: int,
        after: ModelConfigListKey | None = None,
        include_disabled: bool = False,
    ) -> ModelConfigInternalPage:
        """Read page metadata and immutable execution content with one SELECT."""
        self._validate_transaction_connection(connection)
        statement = _page_statement(
            limit=limit,
            after=after,
            include_disabled=include_disabled,
        )
        rows = tuple(connection.execute(statement).mappings())
        stored = tuple(_stored(row) for row in rows[:limit])
        items = tuple(
            ModelConfigInternalEntry(
                snapshot=item.snapshot,
                public_config=item.public_config,
            )
            for item in stored
        )
        next_key = None
        if len(rows) > limit:
            next_key = ModelConfigListKey(id=items[-1].snapshot.id)
        return ModelConfigInternalPage(items=items, next_key=next_key)

    def update_display_name(
        self, config_id: str, display_name: str, *, expected_revision: int
    ) -> AnalysisModelConfigSnapshot:
        name = _display_name(display_name)
        _revision(expected_revision)
        try:
            with self._begin() as connection:
                current = self._row(connection, config_id)
                if current is None:
                    raise ModelNotFound("Model configuration was not found")
                now = _effective_now(_stored(current).snapshot, self._clock())
                returned = (
                    connection.execute(
                        update(AnalysisModelConfig)
                        .where(
                            AnalysisModelConfig.id == config_id,
                            AnalysisModelConfig.revision == expected_revision,
                            AnalysisModelConfig.status
                            != ModelConfigStatus.DISABLED.value,
                        )
                        .values(
                            display_name=name,
                            revision=AnalysisModelConfig.revision + 1,
                            updated_at=now,
                        )
                        .returning(*AnalysisModelConfig.__table__.columns)
                    )
                    .mappings()
                    .one_or_none()
                )
                if returned is None:
                    self._raise_mutation_miss(connection, config_id)
                snapshot = _stored(returned).snapshot
        except IntegrityError:
            raise ModelCatalogConflict(
                "Model configuration mutation was rejected"
            ) from None
        return snapshot

    def mark_test_result(
        self,
        config_id: str,
        *,
        expected_status: ModelConfigStatus,
        expected_revision: int,
        succeeded: bool,
        error_code: str | None = None,
    ) -> AnalysisModelConfigSnapshot:
        if not isinstance(expected_status, ModelConfigStatus):
            raise TypeError("expected_status must be ModelConfigStatus")
        if expected_status is ModelConfigStatus.DISABLED:
            raise ModelCatalogConflict("Disabled model configuration cannot be tested")
        _revision(expected_revision)
        if succeeded:
            if error_code is not None:
                raise ValueError("Successful model test cannot have an error code")
            new_status = ModelConfigStatus.VERIFIED
        else:
            if type(error_code) is not str or _ERROR_CODE.fullmatch(error_code) is None:
                raise ValueError("Failed model test requires a valid error code")
            new_status = ModelConfigStatus.FAILED
        try:
            with self._begin() as connection:
                current = self._row(connection, config_id)
                if current is None:
                    raise ModelNotFound("Model configuration was not found")
                now = _effective_now(_stored(current).snapshot, self._clock())
                returned = (
                    connection.execute(
                        update(AnalysisModelConfig)
                        .where(
                            AnalysisModelConfig.id == config_id,
                            AnalysisModelConfig.status == expected_status.value,
                            AnalysisModelConfig.revision == expected_revision,
                            AnalysisModelConfig.status
                            != ModelConfigStatus.DISABLED.value,
                        )
                        .values(
                            status=new_status.value,
                            verified_at=now if succeeded else None,
                            last_tested_at=now,
                            error_code=None if succeeded else error_code,
                            revision=AnalysisModelConfig.revision + 1,
                            updated_at=now,
                        )
                        .returning(*AnalysisModelConfig.__table__.columns)
                    )
                    .mappings()
                    .one_or_none()
                )
                if returned is None:
                    self._raise_mutation_miss(connection, config_id)
                snapshot = _stored(returned).snapshot
        except IntegrityError:
            raise ModelCatalogConflict(
                "Model configuration mutation was rejected"
            ) from None
        return snapshot

    def disable(
        self, config_id: str, *, expected_revision: int
    ) -> AnalysisModelConfigSnapshot:
        _revision(expected_revision)
        try:
            with self._begin() as connection:
                current = self._row(connection, config_id)
                if current is None:
                    raise ModelNotFound("Model configuration was not found")
                now = _effective_now(_stored(current).snapshot, self._clock())
                returned = (
                    connection.execute(
                        update(AnalysisModelConfig)
                        .where(
                            AnalysisModelConfig.id == config_id,
                            AnalysisModelConfig.revision == expected_revision,
                            AnalysisModelConfig.status
                            != ModelConfigStatus.DISABLED.value,
                        )
                        .values(
                            status=ModelConfigStatus.DISABLED.value,
                            error_code=None,
                            revision=AnalysisModelConfig.revision + 1,
                            updated_at=now,
                        )
                        .returning(*AnalysisModelConfig.__table__.columns)
                    )
                    .mappings()
                    .one_or_none()
                )
                if returned is None:
                    self._raise_mutation_miss(connection, config_id)
                snapshot = _stored(returned).snapshot
        except IntegrityError:
            raise ModelCatalogConflict(
                "Model configuration mutation was rejected"
            ) from None
        return snapshot

    def require_verified(self, config_id: str) -> VerifiedModelExecution:
        with self._connection() as connection:
            return self._require_verified(connection, config_id)

    def require_verified_in_transaction(
        self,
        connection: Connection,
        config_id: str,
    ) -> VerifiedModelExecution:
        """Resolve enabled execution content in a validated caller transaction."""
        self._validate_transaction_connection(connection)
        locked = connection.execute(
            update(AnalysisModelConfig)
            .where(
                AnalysisModelConfig.id == config_id,
                AnalysisModelConfig.status == ModelConfigStatus.VERIFIED.value,
            )
            .values(revision=AnalysisModelConfig.revision)
        )
        if locked.rowcount not in {0, 1}:
            raise ModelCatalogCorruption("Model configuration lock is invalid")
        row = self._row(connection, config_id, for_update=True)
        return self._verified_from_row(row)

    def _require_verified(
        self,
        connection: Connection,
        config_id: str,
    ) -> VerifiedModelExecution:
        row = self._row(connection, config_id)
        return self._verified_from_row(row)

    @staticmethod
    def _verified_from_row(row: RowMapping | None) -> VerifiedModelExecution:
        if row is None:
            raise ModelNotFound("Model configuration was not found")
        stored = _stored(row)
        if stored.snapshot.status is not ModelConfigStatus.VERIFIED:
            raise ModelNotVerified("Model configuration is not verified and enabled")
        return VerifiedModelExecution(
            model_config_id=stored.snapshot.id,
            public_config=stored.public_config,
        )

    @staticmethod
    def _row(
        connection: Connection,
        config_id: str,
        *,
        for_update: bool = False,
    ) -> RowMapping | None:
        statement = select(AnalysisModelConfig).where(
            AnalysisModelConfig.id == config_id
        )
        if for_update:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    def _raise_mutation_miss(self, connection: Connection, config_id: str) -> NoReturn:
        if self._row(connection, config_id) is None:
            raise ModelNotFound("Model configuration was not found")
        raise ModelCatalogConflict("Model configuration state changed")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._owns_engine:
                self._engine.dispose()


def _stored(row: RowMapping) -> _StoredConfig:
    try:
        payload = cast(str, row["public_config_json"])
        public_config = AnalysisModelPublicConfig.model_validate_json(payload)
        canonical = _canonical_config(public_config)
        config_id = cast(str, row["id"])
        if (
            payload != canonical
            or _content_hash(canonical) != config_id
            or row["public_config_hash"] != config_id
            or row["provider"] != public_config.provider.value
            or row["model"] != public_config.model
            or row["secret_reference_id"] != public_config.secret_reference_id
        ):
            raise ValueError
        status = ModelConfigStatus(cast(str, row["status"]))
        revision = _revision(row["revision"])
        display_name = _display_name(cast(str, row["display_name"]))
        verified_at = _optional_utc(row["verified_at"])
        last_tested_at = _optional_utc(row["last_tested_at"])
        error_code = cast(str | None, row["error_code"])
        created_at = _utc(row["created_at"])
        updated_at = _utc(row["updated_at"])
        if updated_at < created_at or not _valid_state_shape(
            status,
            verified_at=verified_at,
            last_tested_at=last_tested_at,
            error_code=error_code,
        ):
            raise ValueError
        snapshot = AnalysisModelConfigSnapshot(
            id=config_id,
            public_config_hash=config_id,
            display_name=display_name,
            provider=public_config.provider,
            model=public_config.model,
            base_url=public_config.base_url,
            temperature=public_config.temperature,
            timeout_seconds=public_config.timeout_seconds,
            max_output_tokens=public_config.max_output_tokens,
            api_key_configured=public_config.api_key_configured,
            supersedes_id=cast(str | None, row["supersedes_id"]),
            status=status,
            revision=revision,
            verified_at=verified_at,
            last_tested_at=last_tested_at,
            error_code=error_code,
            created_at=created_at,
            updated_at=updated_at,
        )
    except (KeyError, OverflowError, TypeError, ValueError, ValidationError):
        raise ModelCatalogCorruption(
            "Stored model configuration is corrupted"
        ) from None
    return _StoredConfig(snapshot=snapshot, public_config=public_config)


def _revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Model configuration revision is invalid")
    return value


def _page_statement(
    *,
    limit: int,
    after: ModelConfigListKey | None,
    include_disabled: bool,
) -> Select[tuple[AnalysisModelConfig]]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Model configuration page limit is invalid")
    if type(include_disabled) is not bool:
        raise ValueError("Model configuration disabled filter is invalid")
    if after is not None:
        if not isinstance(after, ModelConfigListKey):
            raise ValueError("Model configuration page key is invalid")
        if _CONFIG_ID.fullmatch(after.id) is None:
            raise ValueError("Model configuration page key is invalid")
    statement = select(AnalysisModelConfig)
    if not include_disabled:
        statement = statement.where(
            AnalysisModelConfig.status != ModelConfigStatus.DISABLED.value
        )
    if after is not None:
        statement = statement.where(AnalysisModelConfig.id > after.id)
    return statement.order_by(AnalysisModelConfig.id).limit(limit + 1)


def _valid_state_shape(
    status: ModelConfigStatus,
    *,
    verified_at: datetime | None,
    last_tested_at: datetime | None,
    error_code: str | None,
) -> bool:
    valid_error = error_code is None or _ERROR_CODE.fullmatch(error_code) is not None
    if not valid_error:
        return False
    if status is ModelConfigStatus.UNVERIFIED:
        return verified_at is None and last_tested_at is None and error_code is None
    if status is ModelConfigStatus.VERIFIED:
        return (
            verified_at is not None
            and last_tested_at == verified_at
            and error_code is None
        )
    if status is ModelConfigStatus.FAILED:
        return (
            verified_at is None
            and last_tested_at is not None
            and error_code is not None
        )
    return error_code is None and (
        verified_at is None
        or (last_tested_at is not None and last_tested_at == verified_at)
    )


def _effective_now(current: AnalysisModelConfigSnapshot, sampled: datetime) -> datetime:
    lower_bound = current.updated_at + timedelta(microseconds=1)
    if current.last_tested_at is not None:
        lower_bound = max(
            lower_bound,
            current.last_tested_at + timedelta(microseconds=1),
        )
    return max(_utc(sampled), lower_bound)
