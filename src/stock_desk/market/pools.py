from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Annotated, Self, cast
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import Engine, delete, func, insert, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from stock_desk.market.instruments import (
    InstrumentCatalog,
    InstrumentRepository,
    InstrumentRepositoryError,
)
from stock_desk.market.provenance import Sha256Digest
from stock_desk.market.types import (
    CanonicalSymbol,
    Instrument,
    InstrumentKind,
    ListingStatus,
    ProviderId,
    UtcDatetime,
)
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
    create_engine_for_url,
    migrate,
)
from stock_desk.storage.models import (
    CustomPool as CustomPoolRecord,
    CustomPoolMember,
    PresetPoolMember,
    PresetPoolSnapshot,
)


MAX_PRESET_MEMBERS = 10_000
MAX_CUSTOM_MEMBERS = 5_000

PresetKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$",
    ),
]
PoolName = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^\S(?:.{0,62}\S)?$",
    ),
]


class PoolCategory(StrEnum):
    ALL_A = "all_a"
    INDEX = "index"
    INDUSTRY = "industry"


class PoolComposition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    preset_key: PresetKey
    category: PoolCategory
    display_name: PoolName
    symbols: tuple[CanonicalSymbol, ...] = Field(
        min_length=1,
        max_length=MAX_PRESET_MEMBERS,
    )
    source: ProviderId
    dataset_version: Sha256Digest
    route_version: Sha256Digest
    fetched_at: UtcDatetime
    data_cutoff: UtcDatetime
    complete: StrictBool

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("pool display name contains a control character")
        return value

    @model_validator(mode="after")
    def validate_composition(self) -> Self:
        if not self.complete:
            raise ValueError("pool composition must be complete")
        if self.data_cutoff > self.fetched_at:
            raise ValueError("pool data cutoff cannot be later than fetch time")
        if len(self.symbols) != len(frozenset(self.symbols)):
            raise ValueError("pool composition contains a duplicate symbol")
        return self


class PoolRepositoryError(RuntimeError):
    """Base class for pool persistence failures."""


class PoolValidationError(PoolRepositoryError, ValueError):
    """Pool input does not satisfy the public contract."""


class PoolNotFound(PoolRepositoryError):
    """The requested pool does not exist."""


class PoolConflict(PoolRepositoryError):
    """A content-addressed pool row collides with different content."""


class PoolCorruption(PoolRepositoryError):
    """Stored pool content fails canonical validation."""


class PoolRevisionConflict(PoolRepositoryError):
    """The custom pool revision changed before the requested mutation."""


class PoolItemIssueCode(StrEnum):
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    NOT_STOCK = "not_stock"
    DELISTED = "delisted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class PoolItemIssue:
    ordinal: int
    code: PoolItemIssueCode


class PoolItemValidationError(PoolValidationError):
    def __init__(self, issues: tuple[PoolItemIssue, ...]) -> None:
        if not issues:
            raise ValueError("pool item validation errors require at least one issue")
        self.issues = issues
        super().__init__("Custom pool members are invalid")


@dataclass(frozen=True, slots=True)
class PoolMember:
    ordinal: int
    instrument: Instrument


@dataclass(frozen=True, slots=True)
class PresetPool:
    snapshot_id: str
    pool_id: str
    composition: PoolComposition
    instrument_manifest_record_id: str
    instrument_dataset_version: str
    members: tuple[PoolMember, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(member.instrument.symbol for member in self.members)


@dataclass(frozen=True, slots=True)
class CustomPoolState:
    pool_id: str
    name: str
    revision: int
    instrument_manifest_record_id: str
    instrument_dataset_version: str
    members: tuple[PoolMember, ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(member.instrument.symbol for member in self.members)


@dataclass(frozen=True, slots=True)
class PresetPoolSummary:
    pool_id: str
    snapshot_id: str
    name: str
    category: PoolCategory
    member_count: int
    instrument_manifest_record_id: str
    instrument_dataset_version: str


@dataclass(frozen=True, slots=True)
class CustomPoolSummary:
    pool_id: str
    name: str
    revision: int
    member_count: int
    instrument_manifest_record_id: str
    instrument_dataset_version: str


_PRESET_KEY_ADAPTER = TypeAdapter(PresetKey)
_POOL_NAME_ADAPTER = TypeAdapter(PoolName)
_SYMBOL_ADAPTER = TypeAdapter(CanonicalSymbol)
_DIGEST_ADAPTER = TypeAdapter(Sha256Digest)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _snapshot_id(
    composition: PoolComposition,
    *,
    instrument_manifest_record_id: str,
    instrument_dataset_version: str,
) -> str:
    payload = {
        "composition": composition.model_dump(mode="json"),
        "instrument_dataset_version": instrument_dataset_version,
        "instrument_manifest_record_id": instrument_manifest_record_id,
        "schema_version": "stock-desk-preset-pool-v1",
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _custom_member_digest(
    *,
    pool_id: str,
    revision: int,
    instrument_dataset_version: str,
    symbols: tuple[str, ...],
) -> str:
    payload = {
        "instrument_dataset_version": instrument_dataset_version,
        "pool_id": pool_id,
        "revision": revision,
        "schema_version": "stock-desk-custom-pool-members-v1",
        "symbols": symbols,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _custom_state_digest(
    *,
    pool_id: str,
    name: str,
    revision: int,
    member_count: int,
    instrument_manifest_record_id: str,
    instrument_dataset_version: str,
    member_digest: str,
) -> str:
    payload = {
        "instrument_dataset_version": instrument_dataset_version,
        "instrument_manifest_record_id": instrument_manifest_record_id,
        "member_count": member_count,
        "member_digest": member_digest,
        "name": name,
        "pool_id": pool_id,
        "revision": revision,
        "schema_version": "stock-desk-custom-pool-state-v1",
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _validated_preset_key(value: object) -> str:
    try:
        return _PRESET_KEY_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise PoolValidationError("Preset key is invalid") from error


def _validated_composition(value: object) -> PoolComposition:
    try:
        if not isinstance(value, PoolComposition):
            raise TypeError
        return PoolComposition.model_validate(value.model_dump(mode="python"))
    except (ValidationError, TypeError, ValueError) as error:
        raise PoolValidationError("Pool composition is invalid") from error


def _validated_pool_name(value: object) -> str:
    try:
        name = _POOL_NAME_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise PoolValidationError("Custom pool name is invalid") from error
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise PoolValidationError("Custom pool name is invalid")
    return name


def _validated_pool_id(value: object) -> str:
    if type(value) is not str:
        raise PoolValidationError("Custom pool ID is invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise PoolValidationError("Custom pool ID is invalid") from error
    if str(parsed) != value:
        raise PoolValidationError("Custom pool ID is invalid")
    return value


def _validated_revision(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise PoolValidationError("Custom pool revision is invalid")
    return value


def _validated_summary_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise PoolValidationError("Pool summary limit is invalid")
    return value


def _validated_pool_cursor(value: object | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise PoolValidationError("Pool cursor is invalid")
    if value.startswith("preset:"):
        key = _validated_preset_key(value.removeprefix("preset:"))
        if value != f"preset:{key}":
            raise PoolValidationError("Pool cursor is invalid")
        return value
    try:
        return _validated_pool_id(value)
    except PoolValidationError as error:
        raise PoolValidationError("Pool cursor is invalid") from error


def _materialized_symbols(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PoolValidationError("Custom pool symbols are invalid")
    symbols = tuple(value)
    if not 1 <= len(symbols) <= 5_000:
        raise PoolValidationError("Custom pool symbols are invalid")
    return symbols


class PoolRepository:
    def __init__(self, engine: Engine, *, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine
        try:
            with engine.connect() as connection:
                self._database_identity = connection_database_identity(connection)
            self._instruments = InstrumentRepository(engine)
        except (DatabaseIdentityError, InstrumentRepositoryError) as error:
            raise PoolCorruption(
                "Pool database identity could not be determined"
            ) from error
        if self._instruments.database_identity != self._database_identity:
            raise PoolCorruption("Pool database identity is inconsistent")

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    @classmethod
    def open(cls, url: str) -> PoolRepository:
        migrate(url)
        engine = create_engine_for_url(url)
        try:
            return cls(engine, owns_engine=True)
        except BaseException:
            engine.dispose()
            raise

    def _validate_connection(self, connection: Connection) -> None:
        if connection.closed or connection.engine is not self._engine:
            raise PoolCorruption("Pool database connection is not repository-bound")
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise PoolCorruption(
                "Pool database identity could not be determined"
            ) from error
        if identity != self._database_identity:
            raise PoolCorruption("Pool database identity changed")

    def _checked_connection(self) -> Connection:
        connection = self._engine.connect()
        try:
            self._validate_connection(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _checked_read_connection(self) -> Iterator[Connection]:
        connection = self._checked_connection()
        try:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN")
                driver_connection = connection.connection.driver_connection
                if driver_connection is None or not bool(
                    driver_connection.in_transaction
                ):
                    raise PoolCorruption("Pool SQLite read snapshot did not begin")
            else:
                connection.begin()
            yield connection
        finally:
            try:
                if connection.in_transaction():
                    connection.rollback()
            finally:
                connection.close()

    def publish_full_a(
        self,
        *,
        preset_key: str = "all-a",
        display_name: str = "全部A股",
    ) -> PresetPool:
        connection = self._checked_connection()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            catalog = self._instruments.current_catalog(connection=connection)
            symbols = tuple(
                item.symbol
                for item in catalog.instruments
                if item.instrument_kind is InstrumentKind.STOCK
                and item.listing_status is not ListingStatus.DELISTED
            )
            try:
                composition = PoolComposition(
                    preset_key=preset_key,
                    category=PoolCategory.ALL_A,
                    display_name=display_name,
                    symbols=symbols,
                    source=catalog.manifest.source,
                    dataset_version=catalog.manifest.dataset_version,
                    route_version=catalog.manifest.route_version,
                    fetched_at=catalog.manifest.fetched_at,
                    data_cutoff=catalog.manifest.data_cutoff,
                    complete=True,
                )
            except ValidationError as error:
                raise PoolValidationError("Full-A composition is invalid") from error
            result = self._publish_composition(connection, composition, catalog)
            connection.commit()
            return result
        except PoolRepositoryError:
            connection.rollback()
            raise
        except IntegrityError as error:
            connection.rollback()
            raise PoolConflict("Preset pool content-address collision") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def publish_preset(self, composition: PoolComposition) -> PresetPool:
        validated = _validated_composition(composition)
        if validated.category is PoolCategory.ALL_A:
            raise PoolValidationError("All-A presets require the full-A builder")
        connection = self._checked_connection()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            catalog = self._instruments.current_catalog(connection=connection)
            result = self._publish_composition(connection, validated, catalog)
            connection.commit()
            return result
        except PoolRepositoryError:
            connection.rollback()
            raise
        except IntegrityError as error:
            connection.rollback()
            raise PoolConflict("Preset pool content-address collision") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _publish_composition(
        self,
        connection: Connection,
        composition: PoolComposition,
        catalog: InstrumentCatalog,
    ) -> PresetPool:
        self._validate_connection(connection)
        by_symbol = {item.symbol: item for item in catalog.instruments}
        for symbol in composition.symbols:
            item = by_symbol.get(symbol)
            if item is None:
                raise PoolValidationError(
                    "Preset member is not in the pinned instrument catalog"
                )
            if (
                item.instrument_kind is not InstrumentKind.STOCK
                or item.listing_status is ListingStatus.DELISTED
            ):
                raise PoolValidationError("Preset member is not eligible")

        snapshot_id = _snapshot_id(
            composition,
            instrument_manifest_record_id=catalog.manifest_record_id,
            instrument_dataset_version=catalog.dataset_version,
        )
        existing = connection.execute(
            select(PresetPoolSnapshot.snapshot_id).where(
                PresetPoolSnapshot.snapshot_id == snapshot_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            loaded = self._load_preset(connection, snapshot_id)
            if (
                loaded.composition != composition
                or loaded.instrument_manifest_record_id != catalog.manifest_record_id
                or loaded.instrument_dataset_version != catalog.dataset_version
            ):
                raise PoolConflict("Preset pool snapshot hash collision")
            return loaded

        connection.execute(
            insert(PresetPoolSnapshot).values(
                snapshot_id=snapshot_id,
                pool_id=f"preset:{composition.preset_key}",
                preset_key=composition.preset_key,
                category=composition.category.value,
                display_name=composition.display_name,
                source=composition.source.value,
                composition_dataset_version=composition.dataset_version,
                composition_route_version=composition.route_version,
                fetched_at=composition.fetched_at,
                data_cutoff=composition.data_cutoff,
                complete=composition.complete,
                instrument_manifest_record_id=catalog.manifest_record_id,
                instrument_dataset_version=catalog.dataset_version,
                member_count=len(composition.symbols),
            )
        )
        connection.execute(
            insert(PresetPoolMember),
            [
                {
                    "snapshot_id": snapshot_id,
                    "ordinal": ordinal,
                    "instrument_dataset_version": catalog.dataset_version,
                    "symbol": symbol,
                }
                for ordinal, symbol in enumerate(composition.symbols)
            ],
        )
        return self._load_preset(connection, snapshot_id)

    def get_preset(self, preset_key: str) -> PresetPool:
        validated_key = _validated_preset_key(preset_key)
        with self._checked_read_connection() as connection:
            snapshot_id = connection.execute(
                select(PresetPoolSnapshot.snapshot_id)
                .where(PresetPoolSnapshot.preset_key == validated_key)
                .order_by(
                    PresetPoolSnapshot.data_cutoff.desc(),
                    PresetPoolSnapshot.fetched_at.desc(),
                    PresetPoolSnapshot.snapshot_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            if snapshot_id is None:
                raise PoolNotFound("Preset pool was not found")
            return self._load_preset(connection, snapshot_id)

    def get_preset_snapshot(
        self,
        snapshot_id: str,
        *,
        connection: Connection,
    ) -> PresetPool:
        """Resolve one immutable preset inside a caller-owned transaction."""

        self._validate_connection(connection)
        if type(snapshot_id) is not str or not snapshot_id:
            raise PoolValidationError("Preset snapshot ID is invalid")
        return self._load_preset(connection, snapshot_id)

    def get_current_preset(self, pool_id: str, *, connection: Connection) -> PresetPool:
        """Resolve the latest immutable composition for a logical preset pool."""

        self._validate_connection(connection)
        if type(pool_id) is not str or not pool_id.startswith("preset:"):
            raise PoolValidationError("Preset pool ID is invalid")
        preset_key = _validated_preset_key(pool_id.removeprefix("preset:"))
        snapshot_id = connection.execute(
            select(PresetPoolSnapshot.snapshot_id)
            .where(PresetPoolSnapshot.preset_key == preset_key)
            .order_by(
                PresetPoolSnapshot.data_cutoff.desc(),
                PresetPoolSnapshot.fetched_at.desc(),
                PresetPoolSnapshot.snapshot_id.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if snapshot_id is None:
            raise PoolNotFound("Preset pool was not found")
        return self._load_preset(connection, snapshot_id)

    def list_presets(self) -> tuple[PresetPool, ...]:
        with self._checked_read_connection() as connection:
            rows = connection.execute(
                select(
                    PresetPoolSnapshot.preset_key,
                    PresetPoolSnapshot.snapshot_id,
                ).order_by(
                    PresetPoolSnapshot.preset_key,
                    PresetPoolSnapshot.data_cutoff.desc(),
                    PresetPoolSnapshot.fetched_at.desc(),
                    PresetPoolSnapshot.snapshot_id.desc(),
                )
            ).all()
            latest_ids: list[str] = []
            previous_key: str | None = None
            for preset_key, snapshot_id in rows:
                if preset_key != previous_key:
                    latest_ids.append(snapshot_id)
                    previous_key = preset_key
            return tuple(
                self._load_preset(connection, snapshot_id) for snapshot_id in latest_ids
            )

    def list_preset_summaries(
        self,
        *,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[PresetPoolSummary, ...]:
        validated_limit = _validated_summary_limit(limit)
        validated_after = _validated_pool_cursor(after)
        rank = (
            func.row_number()
            .over(
                partition_by=PresetPoolSnapshot.preset_key,
                order_by=(
                    PresetPoolSnapshot.data_cutoff.desc(),
                    PresetPoolSnapshot.fetched_at.desc(),
                    PresetPoolSnapshot.snapshot_id.desc(),
                ),
            )
            .label("snapshot_rank")
        )
        ranked = select(
            PresetPoolSnapshot.pool_id,
            PresetPoolSnapshot.snapshot_id,
            PresetPoolSnapshot.preset_key,
            PresetPoolSnapshot.display_name,
            PresetPoolSnapshot.category,
            PresetPoolSnapshot.source,
            PresetPoolSnapshot.composition_dataset_version,
            PresetPoolSnapshot.composition_route_version,
            PresetPoolSnapshot.fetched_at,
            PresetPoolSnapshot.data_cutoff,
            PresetPoolSnapshot.complete,
            PresetPoolSnapshot.member_count,
            PresetPoolSnapshot.instrument_manifest_record_id,
            PresetPoolSnapshot.instrument_dataset_version,
            rank,
        ).subquery()
        statement = select(ranked).where(ranked.c.snapshot_rank == 1)
        if validated_after is not None:
            statement = statement.where(ranked.c.pool_id > validated_after)
        statement = statement.order_by(ranked.c.pool_id).limit(validated_limit)
        with self._checked_read_connection() as connection:
            rows = connection.execute(statement).mappings().all()
            if not rows:
                return ()
            snapshot_ids = tuple(cast(str, row["snapshot_id"]) for row in rows)
            member_limit = len(rows) * MAX_PRESET_MEMBERS + 1
            member_rows = (
                connection.execute(
                    select(
                        PresetPoolMember.snapshot_id,
                        PresetPoolMember.ordinal,
                        PresetPoolMember.instrument_dataset_version,
                        PresetPoolMember.symbol,
                    )
                    .where(PresetPoolMember.snapshot_id.in_(snapshot_ids))
                    .order_by(
                        PresetPoolMember.snapshot_id,
                        PresetPoolMember.ordinal,
                    )
                    .limit(member_limit)
                )
                .mappings()
                .all()
            )
            if len(member_rows) >= member_limit:
                raise PoolCorruption("Stored preset pool summary members exceed bounds")
            members_by_snapshot: dict[str, list[RowMapping]] = {
                snapshot_id: [] for snapshot_id in snapshot_ids
            }
            for member in member_rows:
                member_snapshot_id = cast(str, member["snapshot_id"])
                try:
                    members_by_snapshot[member_snapshot_id].append(member)
                except KeyError as error:
                    raise PoolCorruption(
                        "Stored preset pool summary member is unbound"
                    ) from error
            summaries: list[PresetPoolSummary] = []
            for row in rows:
                try:
                    pool_id = cast(str, row["pool_id"])
                    preset_key = _validated_preset_key(row["preset_key"])
                    if pool_id != f"preset:{preset_key}":
                        raise ValueError("preset pool ID is invalid")
                    snapshot_id = _DIGEST_ADAPTER.validate_python(
                        row["snapshot_id"], strict=True
                    )
                    member_count = row["member_count"]
                    if type(member_count) is not int or not 1 <= member_count <= 10_000:
                        raise ValueError("preset member count is invalid")
                    manifest_id = _DIGEST_ADAPTER.validate_python(
                        row["instrument_manifest_record_id"], strict=True
                    )
                    dataset_version = _DIGEST_ADAPTER.validate_python(
                        row["instrument_dataset_version"], strict=True
                    )
                    actual_members = members_by_snapshot[snapshot_id]
                    if (
                        len(actual_members) != member_count
                        or tuple(member["ordinal"] for member in actual_members)
                        != tuple(range(member_count))
                        or any(
                            member["instrument_dataset_version"] != dataset_version
                            for member in actual_members
                        )
                    ):
                        raise ValueError("preset summary members are inconsistent")
                    symbols = tuple(
                        _SYMBOL_ADAPTER.validate_python(
                            member["symbol"],
                            strict=True,
                        )
                        for member in actual_members
                    )
                    composition = PoolComposition(
                        preset_key=preset_key,
                        category=PoolCategory(row["category"]),
                        display_name=_validated_pool_name(row["display_name"]),
                        symbols=symbols,
                        source=ProviderId(row["source"]),
                        dataset_version=_DIGEST_ADAPTER.validate_python(
                            row["composition_dataset_version"],
                            strict=True,
                        ),
                        route_version=_DIGEST_ADAPTER.validate_python(
                            row["composition_route_version"],
                            strict=True,
                        ),
                        fetched_at=_aware_utc(cast(datetime, row["fetched_at"])),
                        data_cutoff=_aware_utc(cast(datetime, row["data_cutoff"])),
                        complete=row["complete"],
                    )
                    if snapshot_id != _snapshot_id(
                        composition,
                        instrument_manifest_record_id=manifest_id,
                        instrument_dataset_version=dataset_version,
                    ):
                        raise ValueError("preset snapshot digest is invalid")
                    manifest = self._instruments.pinned_manifest(
                        manifest_id,
                        connection=connection,
                    )
                    if manifest.dataset_version != dataset_version:
                        raise ValueError("preset instrument pin is invalid")
                except (
                    InstrumentRepositoryError,
                    ValidationError,
                    ValueError,
                    PoolValidationError,
                ) as error:
                    raise PoolCorruption(
                        "Stored preset pool summary is corrupt"
                    ) from error
                summaries.append(
                    PresetPoolSummary(
                        pool_id=pool_id,
                        snapshot_id=snapshot_id,
                        name=composition.display_name,
                        category=composition.category,
                        member_count=member_count,
                        instrument_manifest_record_id=manifest_id,
                        instrument_dataset_version=dataset_version,
                    )
                )
            return tuple(summaries)

    def _load_preset(
        self,
        connection: Connection,
        snapshot_id: str,
    ) -> PresetPool:
        self._validate_connection(connection)
        row = (
            connection.execute(
                select(PresetPoolSnapshot).where(
                    PresetPoolSnapshot.snapshot_id == snapshot_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PoolNotFound("Preset pool was not found")
        member_rows = (
            connection.execute(
                select(PresetPoolMember)
                .where(PresetPoolMember.snapshot_id == snapshot_id)
                .order_by(PresetPoolMember.ordinal)
            )
            .mappings()
            .all()
        )
        if (
            len(member_rows) != row["member_count"]
            or tuple(member["ordinal"] for member in member_rows)
            != tuple(range(len(member_rows)))
            or any(
                member["instrument_dataset_version"]
                != row["instrument_dataset_version"]
                for member in member_rows
            )
        ):
            raise PoolCorruption("Stored preset pool members are corrupt")
        symbols = tuple(cast(str, member["symbol"]) for member in member_rows)
        try:
            composition = PoolComposition.model_validate_json(
                json.dumps(
                    {
                        "preset_key": row["preset_key"],
                        "category": row["category"],
                        "display_name": row["display_name"],
                        "symbols": symbols,
                        "source": row["source"],
                        "dataset_version": row["composition_dataset_version"],
                        "route_version": row["composition_route_version"],
                        "fetched_at": _aware_utc(
                            cast(datetime, row["fetched_at"])
                        ).isoformat(),
                        "data_cutoff": _aware_utc(
                            cast(datetime, row["data_cutoff"])
                        ).isoformat(),
                        "complete": row["complete"],
                    },
                    allow_nan=False,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise PoolCorruption("Stored preset pool header is corrupt") from error
        instrument_manifest_record_id = cast(str, row["instrument_manifest_record_id"])
        instrument_dataset_version = cast(str, row["instrument_dataset_version"])
        catalog = self._instruments.pinned_catalog(
            instrument_manifest_record_id,
            connection=connection,
        )
        if catalog.dataset_version != instrument_dataset_version:
            raise PoolCorruption("Stored preset pool instrument pin is corrupt")
        instruments = {item.symbol: item for item in catalog.instruments}
        try:
            members = tuple(
                PoolMember(ordinal, instruments[symbol])
                for ordinal, symbol in enumerate(symbols)
            )
        except KeyError as error:
            raise PoolCorruption("Stored preset pool member is missing") from error
        if any(
            member.instrument.instrument_kind is not InstrumentKind.STOCK
            or member.instrument.listing_status is ListingStatus.DELISTED
            for member in members
        ):
            raise PoolCorruption("Stored preset pool member is ineligible")
        expected_id = _snapshot_id(
            composition,
            instrument_manifest_record_id=instrument_manifest_record_id,
            instrument_dataset_version=instrument_dataset_version,
        )
        if (
            expected_id != row["snapshot_id"]
            or row["pool_id"] != f"preset:{composition.preset_key}"
        ):
            raise PoolCorruption("Stored preset pool snapshot hash is corrupt")
        return PresetPool(
            snapshot_id=expected_id,
            pool_id=cast(str, row["pool_id"]),
            composition=composition,
            instrument_manifest_record_id=instrument_manifest_record_id,
            instrument_dataset_version=instrument_dataset_version,
            members=members,
        )

    @staticmethod
    def _validate_custom_members(
        raw_symbols: tuple[object, ...],
        catalog: InstrumentCatalog,
    ) -> tuple[str, ...]:
        instruments = {item.symbol: item for item in catalog.instruments}
        issues: list[PoolItemIssue] = []
        symbols: list[str] = []
        seen: set[str] = set()
        for ordinal, raw_symbol in enumerate(raw_symbols):
            try:
                symbol = _SYMBOL_ADAPTER.validate_python(raw_symbol, strict=True)
            except ValidationError:
                issues.append(PoolItemIssue(ordinal, PoolItemIssueCode.INVALID))
                continue
            symbols.append(symbol)
            if symbol in seen:
                issues.append(PoolItemIssue(ordinal, PoolItemIssueCode.DUPLICATE))
            else:
                seen.add(symbol)
            item = instruments.get(symbol)
            if item is None:
                issues.append(PoolItemIssue(ordinal, PoolItemIssueCode.NOT_FOUND))
                continue
            if item.instrument_kind is not InstrumentKind.STOCK:
                issues.append(PoolItemIssue(ordinal, PoolItemIssueCode.NOT_STOCK))
            if item.listing_status is ListingStatus.DELISTED:
                issues.append(PoolItemIssue(ordinal, PoolItemIssueCode.DELISTED))
        if issues:
            raise PoolItemValidationError(tuple(issues))
        return tuple(symbols)

    def create_custom(
        self,
        *,
        name: str,
        symbols: Sequence[object],
    ) -> CustomPoolState:
        validated_name = _validated_pool_name(name)
        raw_symbols = _materialized_symbols(symbols)
        connection = self._checked_connection()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            catalog = self._instruments.current_catalog(connection=connection)
            validated_symbols = self._validate_custom_members(raw_symbols, catalog)
            pool_id = str(uuid4())
            member_digest = _custom_member_digest(
                pool_id=pool_id,
                revision=1,
                instrument_dataset_version=catalog.dataset_version,
                symbols=validated_symbols,
            )
            state_digest = _custom_state_digest(
                pool_id=pool_id,
                name=validated_name,
                revision=1,
                member_count=len(validated_symbols),
                instrument_manifest_record_id=catalog.manifest_record_id,
                instrument_dataset_version=catalog.dataset_version,
                member_digest=member_digest,
            )
            connection.execute(
                insert(CustomPoolRecord).values(
                    pool_id=pool_id,
                    name=validated_name,
                    revision=1,
                    instrument_manifest_record_id=catalog.manifest_record_id,
                    instrument_dataset_version=catalog.dataset_version,
                    member_count=len(validated_symbols),
                    member_digest=member_digest,
                    state_digest=state_digest,
                )
            )
            self._insert_custom_members(
                connection,
                pool_id=pool_id,
                member_revision=1,
                instrument_dataset_version=catalog.dataset_version,
                symbols=validated_symbols,
            )
            result = self._load_custom(connection, pool_id)
            connection.commit()
            return result
        except PoolRepositoryError:
            connection.rollback()
            raise
        except IntegrityError as error:
            connection.rollback()
            raise PoolConflict("Custom pool persistence conflict") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_custom(
        self,
        pool_id: str,
        *,
        expected_revision: int,
        name: str,
        symbols: Sequence[object],
    ) -> CustomPoolState:
        validated_id = _validated_pool_id(pool_id)
        validated_revision = _validated_revision(expected_revision)
        validated_name = _validated_pool_name(name)
        raw_symbols = _materialized_symbols(symbols)
        connection = self._checked_connection()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            current_revision = connection.execute(
                select(CustomPoolRecord.revision).where(
                    CustomPoolRecord.pool_id == validated_id
                )
            ).scalar_one_or_none()
            if current_revision is None:
                raise PoolNotFound("Custom pool was not found")
            if current_revision != validated_revision:
                raise PoolRevisionConflict("Custom pool revision is stale")
            self._load_custom(connection, validated_id)
            catalog = self._instruments.current_catalog(connection=connection)
            validated_symbols = self._validate_custom_members(raw_symbols, catalog)
            next_revision = validated_revision + 1
            member_digest = _custom_member_digest(
                pool_id=validated_id,
                revision=next_revision,
                instrument_dataset_version=catalog.dataset_version,
                symbols=validated_symbols,
            )
            state_digest = _custom_state_digest(
                pool_id=validated_id,
                name=validated_name,
                revision=next_revision,
                member_count=len(validated_symbols),
                instrument_manifest_record_id=catalog.manifest_record_id,
                instrument_dataset_version=catalog.dataset_version,
                member_digest=member_digest,
            )
            connection.execute(
                delete(CustomPoolMember).where(CustomPoolMember.pool_id == validated_id)
            )
            result = connection.execute(
                update(CustomPoolRecord)
                .where(
                    CustomPoolRecord.pool_id == validated_id,
                    CustomPoolRecord.revision == validated_revision,
                )
                .values(
                    name=validated_name,
                    revision=next_revision,
                    instrument_manifest_record_id=catalog.manifest_record_id,
                    instrument_dataset_version=catalog.dataset_version,
                    member_count=len(validated_symbols),
                    member_digest=member_digest,
                    state_digest=state_digest,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if result.rowcount != 1:
                raise PoolRevisionConflict("Custom pool revision is stale")
            self._insert_custom_members(
                connection,
                pool_id=validated_id,
                member_revision=next_revision,
                instrument_dataset_version=catalog.dataset_version,
                symbols=validated_symbols,
            )
            loaded = self._load_custom(connection, validated_id)
            connection.commit()
            return loaded
        except PoolRepositoryError:
            connection.rollback()
            raise
        except IntegrityError as error:
            connection.rollback()
            raise PoolConflict("Custom pool persistence conflict") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete_custom(self, pool_id: str, *, expected_revision: int) -> None:
        validated_id = _validated_pool_id(pool_id)
        validated_revision = _validated_revision(expected_revision)
        connection = self._checked_connection()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            result = connection.execute(
                delete(CustomPoolRecord).where(
                    CustomPoolRecord.pool_id == validated_id,
                    CustomPoolRecord.revision == validated_revision,
                )
            )
            if result.rowcount != 1:
                exists = connection.execute(
                    select(CustomPoolRecord.pool_id).where(
                        CustomPoolRecord.pool_id == validated_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    raise PoolNotFound("Custom pool was not found")
                raise PoolRevisionConflict("Custom pool revision is stale")
            connection.commit()
        except PoolRepositoryError:
            connection.rollback()
            raise
        except IntegrityError as error:
            connection.rollback()
            raise PoolConflict("Custom pool delete conflict") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_custom(self, pool_id: str) -> CustomPoolState:
        validated_id = _validated_pool_id(pool_id)
        with self._checked_read_connection() as connection:
            return self._load_custom(connection, validated_id)

    def get_custom_revision(
        self,
        pool_id: str,
        revision: int,
        *,
        connection: Connection,
    ) -> CustomPoolState:
        """Resolve the requested mutable-pool revision in the submit transaction."""

        self._validate_connection(connection)
        validated_id = _validated_pool_id(pool_id)
        validated_revision = _validated_revision(revision)
        state = self._load_custom(connection, validated_id)
        if state.revision != validated_revision:
            raise PoolRevisionConflict("Custom pool revision is stale")
        return state

    def get_current_custom(
        self, pool_id: str, *, connection: Connection
    ) -> CustomPoolState:
        """Resolve the current mutable-pool revision in the submit transaction."""

        self._validate_connection(connection)
        return self._load_custom(connection, _validated_pool_id(pool_id))

    def list_customs(self) -> tuple[CustomPoolState, ...]:
        with self._checked_read_connection() as connection:
            pool_ids = (
                connection.execute(
                    select(CustomPoolRecord.pool_id).order_by(CustomPoolRecord.pool_id)
                )
                .scalars()
                .all()
            )
            return tuple(self._load_custom(connection, pool_id) for pool_id in pool_ids)

    def list_custom_summaries(
        self,
        *,
        limit: int = 100,
        after: str | None = None,
    ) -> tuple[CustomPoolSummary, ...]:
        validated_limit = _validated_summary_limit(limit)
        validated_after = _validated_pool_cursor(after)
        statement = select(CustomPoolRecord)
        if validated_after is not None:
            statement = statement.where(CustomPoolRecord.pool_id > validated_after)
        statement = statement.order_by(CustomPoolRecord.pool_id).limit(validated_limit)
        with self._checked_read_connection() as connection:
            rows = connection.execute(statement).mappings().all()
            if not rows:
                return ()
            pool_ids = tuple(cast(str, row["pool_id"]) for row in rows)
            member_limit = len(rows) * MAX_CUSTOM_MEMBERS + 1
            member_rows = (
                connection.execute(
                    select(
                        CustomPoolMember.pool_id,
                        CustomPoolMember.ordinal,
                        CustomPoolMember.member_revision,
                        CustomPoolMember.instrument_dataset_version,
                        CustomPoolMember.symbol,
                    )
                    .where(CustomPoolMember.pool_id.in_(pool_ids))
                    .order_by(CustomPoolMember.pool_id, CustomPoolMember.ordinal)
                    .limit(member_limit)
                )
                .mappings()
                .all()
            )
            if len(member_rows) >= member_limit:
                raise PoolCorruption("Stored custom pool summary members exceed bounds")
            members_by_pool: dict[str, list[RowMapping]] = {
                pool_id: [] for pool_id in pool_ids
            }
            for member in member_rows:
                member_pool_id = cast(str, member["pool_id"])
                try:
                    members_by_pool[member_pool_id].append(member)
                except KeyError as error:
                    raise PoolCorruption(
                        "Stored custom pool summary member is unbound"
                    ) from error
            summaries: list[CustomPoolSummary] = []
            for row in rows:
                try:
                    pool_id = _validated_pool_id(row["pool_id"])
                    name = _validated_pool_name(row["name"])
                    revision = _validated_revision(row["revision"])
                    member_count = row["member_count"]
                    if type(member_count) is not int or not 1 <= member_count <= 5_000:
                        raise ValueError("custom member count is invalid")
                    member_digest = _DIGEST_ADAPTER.validate_python(
                        row["member_digest"], strict=True
                    )
                    state_digest = _DIGEST_ADAPTER.validate_python(
                        row["state_digest"], strict=True
                    )
                    manifest_id = _DIGEST_ADAPTER.validate_python(
                        row["instrument_manifest_record_id"], strict=True
                    )
                    dataset_version = _DIGEST_ADAPTER.validate_python(
                        row["instrument_dataset_version"], strict=True
                    )
                    manifest = self._instruments.pinned_manifest(
                        manifest_id,
                        connection=connection,
                    )
                    if manifest.dataset_version != dataset_version:
                        raise ValueError("custom instrument pin is invalid")
                    actual_members = members_by_pool[pool_id]
                    if (
                        len(actual_members) != member_count
                        or tuple(member["ordinal"] for member in actual_members)
                        != tuple(range(member_count))
                        or any(
                            member["member_revision"] != revision
                            for member in actual_members
                        )
                        or any(
                            member["instrument_dataset_version"] != dataset_version
                            for member in actual_members
                        )
                    ):
                        raise ValueError("custom summary members are inconsistent")
                    symbols = tuple(
                        _SYMBOL_ADAPTER.validate_python(
                            member["symbol"],
                            strict=True,
                        )
                        for member in actual_members
                    )
                    expected_member_digest = _custom_member_digest(
                        pool_id=pool_id,
                        revision=revision,
                        instrument_dataset_version=dataset_version,
                        symbols=symbols,
                    )
                    if member_digest != expected_member_digest:
                        raise ValueError("custom member digest is invalid")
                    expected_state_digest = _custom_state_digest(
                        pool_id=pool_id,
                        name=name,
                        revision=revision,
                        member_count=member_count,
                        instrument_manifest_record_id=manifest_id,
                        instrument_dataset_version=dataset_version,
                        member_digest=member_digest,
                    )
                    if state_digest != expected_state_digest:
                        raise ValueError("custom state digest is invalid")
                except (
                    InstrumentRepositoryError,
                    ValidationError,
                    ValueError,
                    PoolValidationError,
                ) as error:
                    raise PoolCorruption(
                        "Stored custom pool summary is corrupt"
                    ) from error
                summaries.append(
                    CustomPoolSummary(
                        pool_id=pool_id,
                        name=name,
                        revision=revision,
                        member_count=member_count,
                        instrument_manifest_record_id=manifest_id,
                        instrument_dataset_version=dataset_version,
                    )
                )
            return tuple(summaries)

    def _insert_custom_members(
        self,
        connection: Connection,
        *,
        pool_id: str,
        member_revision: int,
        instrument_dataset_version: str,
        symbols: tuple[str, ...],
    ) -> None:
        self._validate_connection(connection)
        connection.execute(
            insert(CustomPoolMember),
            [
                {
                    "pool_id": pool_id,
                    "ordinal": ordinal,
                    "member_revision": member_revision,
                    "instrument_dataset_version": instrument_dataset_version,
                    "symbol": symbol,
                }
                for ordinal, symbol in enumerate(symbols)
            ],
        )

    def _load_custom(
        self,
        connection: Connection,
        pool_id: str,
    ) -> CustomPoolState:
        self._validate_connection(connection)
        row = (
            connection.execute(
                select(CustomPoolRecord).where(CustomPoolRecord.pool_id == pool_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PoolNotFound("Custom pool was not found")
        member_rows = (
            connection.execute(
                select(CustomPoolMember)
                .where(CustomPoolMember.pool_id == pool_id)
                .order_by(CustomPoolMember.ordinal)
            )
            .mappings()
            .all()
        )
        if (
            len(member_rows) != row["member_count"]
            or tuple(member["ordinal"] for member in member_rows)
            != tuple(range(len(member_rows)))
            or any(
                member["instrument_dataset_version"]
                != row["instrument_dataset_version"]
                for member in member_rows
            )
            or any(
                member["member_revision"] != row["revision"] for member in member_rows
            )
        ):
            raise PoolCorruption("Stored custom pool members are corrupt")
        try:
            validated_id = _validated_pool_id(row["pool_id"])
            validated_name = _validated_pool_name(row["name"])
            revision = _validated_revision(row["revision"])
        except PoolValidationError as error:
            raise PoolCorruption("Stored custom pool header is corrupt") from error
        manifest_record_id = cast(str, row["instrument_manifest_record_id"])
        dataset_version = cast(str, row["instrument_dataset_version"])
        catalog = self._instruments.pinned_catalog(
            manifest_record_id,
            connection=connection,
        )
        if catalog.dataset_version != dataset_version:
            raise PoolCorruption("Stored custom pool instrument pin is corrupt")
        instruments = {item.symbol: item for item in catalog.instruments}
        members: list[PoolMember] = []
        for ordinal, member in enumerate(member_rows):
            symbol = cast(str, member["symbol"])
            item = instruments.get(symbol)
            if item is None:
                raise PoolCorruption("Stored custom pool member is missing")
            if (
                item.instrument_kind is not InstrumentKind.STOCK
                or item.listing_status is ListingStatus.DELISTED
            ):
                raise PoolCorruption("Stored custom pool member is ineligible")
            members.append(PoolMember(ordinal, item))
        if len({member.instrument.symbol for member in members}) != len(members):
            raise PoolCorruption("Stored custom pool members contain a duplicate")
        expected_member_digest = _custom_member_digest(
            pool_id=validated_id,
            revision=revision,
            instrument_dataset_version=dataset_version,
            symbols=tuple(member.instrument.symbol for member in members),
        )
        if row["member_digest"] != expected_member_digest:
            raise PoolCorruption("Stored custom pool member digest is corrupt")
        expected_state_digest = _custom_state_digest(
            pool_id=validated_id,
            name=validated_name,
            revision=revision,
            member_count=len(members),
            instrument_manifest_record_id=manifest_record_id,
            instrument_dataset_version=dataset_version,
            member_digest=expected_member_digest,
        )
        if row["state_digest"] != expected_state_digest:
            raise PoolCorruption("Stored custom pool state digest is corrupt")
        return CustomPoolState(
            pool_id=validated_id,
            name=validated_name,
            revision=revision,
            instrument_manifest_record_id=manifest_record_id,
            instrument_dataset_version=dataset_version,
            members=tuple(members),
        )

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()
