from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Engine, insert, select
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from stock_desk.market.lake import manifest_record_id
from stock_desk.market.provenance import RoutedInstrumentSuccess, RoutingManifest
from stock_desk.market.providers.normalization import (
    dataset_version as make_dataset_version,
)
from stock_desk.market.types import CanonicalSymbol, Instrument, ProviderId
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
)
from stock_desk.storage.models import (
    InstrumentDataset,
    InstrumentDatasetItem,
    InstrumentRoutingManifest,
)


class InstrumentRepositoryError(RuntimeError):
    """Base class for instrument catalog failures."""


class InstrumentValidationError(InstrumentRepositoryError, ValueError):
    """Instrument input does not satisfy the public contract."""


class InstrumentNotFound(InstrumentRepositoryError):
    """No current instrument manifest or requested instrument exists."""


class InstrumentConflict(InstrumentRepositoryError):
    """A content-addressed instrument row collides with different content."""


class InstrumentCorruption(InstrumentRepositoryError):
    """Persisted instrument catalog content fails canonical validation."""


MAX_INSTRUMENT_CATALOG_ITEMS = 50_000
_SYMBOL_ADAPTER = TypeAdapter(CanonicalSymbol)


def _validated_catalog_item_count(value: object) -> int:
    if type(value) is not int or value < 1:
        raise InstrumentValidationError("Instrument catalog size is invalid")
    if value > MAX_INSTRUMENT_CATALOG_ITEMS:
        raise InstrumentValidationError("Instrument catalog has too many items")
    return value


@dataclass(frozen=True, slots=True)
class InstrumentManifestSnapshot:
    manifest_record_id: str
    dataset_version: str
    route_version: str
    source: ProviderId
    fetched_at: datetime
    data_cutoff: datetime
    row_count: int
    manifest: RoutingManifest


@dataclass(frozen=True, slots=True)
class InstrumentSearchResult:
    instrument: Instrument
    manifest: InstrumentManifestSnapshot


@dataclass(frozen=True, slots=True)
class InstrumentCatalog:
    manifest: InstrumentManifestSnapshot
    instruments: tuple[Instrument, ...]

    @property
    def manifest_record_id(self) -> str:
        return self.manifest.manifest_record_id

    @property
    def dataset_version(self) -> str:
        return self.manifest.dataset_version


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _instrument_values(
    dataset_version: str,
    ordinal: int,
    item: Instrument,
) -> dict[str, Any]:
    return {
        "dataset_version": dataset_version,
        "symbol": item.symbol,
        "ordinal": ordinal,
        "exchange": item.exchange.value,
        "name": item.name,
        "instrument_kind": item.instrument_kind.value,
        "listing_status": item.listing_status.value,
        "listed_on": item.listed_on,
        "delisted_on": item.delisted_on,
    }


def _validated_symbol(value: object) -> str:
    try:
        return _SYMBOL_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise InstrumentValidationError("Instrument symbol is invalid") from error


def _validated_query(value: object) -> str:
    if type(value) is not str:
        raise InstrumentValidationError("Instrument search query is invalid")
    query = value.strip()
    if not query or len(query) > 64:
        raise InstrumentValidationError("Instrument search query is invalid")
    try:
        query.encode("utf-8", errors="strict")
    except UnicodeError as error:
        raise InstrumentValidationError("Instrument search query is invalid") from error
    return query


def _item_from_row(row: RowMapping) -> Instrument:
    try:
        return Instrument.model_validate_json(
            json.dumps(
                {
                    "symbol": row["symbol"],
                    "exchange": row["exchange"],
                    "name": row["name"],
                    "instrument_kind": row["instrument_kind"],
                    "listing_status": row["listing_status"],
                    "listed_on": (
                        row["listed_on"].isoformat()
                        if row["listed_on"] is not None
                        else None
                    ),
                    "delisted_on": (
                        row["delisted_on"].isoformat()
                        if row["delisted_on"] is not None
                        else None
                    ),
                },
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (ValidationError, ValueError, TypeError) as error:
        raise InstrumentCorruption("Stored instrument item is corrupt") from error


class InstrumentRepository:
    """Atomic, content-addressed instrument snapshot repository."""

    def __init__(self, engine: Engine, *, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine
        try:
            with engine.connect() as connection:
                self._database_identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise InstrumentCorruption(
                "Instrument database identity could not be determined"
            ) from error

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    @classmethod
    def open(cls, url: str) -> InstrumentRepository:
        migrate(url)
        engine = create_engine_for_url(url)
        try:
            return cls(engine, owns_engine=True)
        except BaseException:
            engine.dispose()
            raise

    def _checked_connection(self) -> Connection:
        connection = self._engine.connect()
        try:
            self._validate_connection(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def ingest(self, routed: RoutedInstrumentSuccess) -> InstrumentManifestSnapshot:
        try:
            _validated_catalog_item_count(len(routed.batch.items))
        except (AttributeError, TypeError) as error:
            raise InstrumentValidationError("Routed instruments are invalid") from error
        try:
            validated = RoutedInstrumentSuccess.model_validate(
                routed.model_dump(mode="python")
            )
        except (ValidationError, AttributeError, TypeError, ValueError) as error:
            raise InstrumentValidationError("Routed instruments are invalid") from error
        batch = validated.batch
        expected_dataset_version = make_dataset_version(
            source=batch.provenance.source,
            operation="instruments",
            request={},
            data_cutoff=batch.provenance.data_cutoff,
            items=batch.items,
        )
        if expected_dataset_version != batch.provenance.dataset_version:
            raise InstrumentValidationError("Instrument dataset version is invalid")
        record_id = manifest_record_id(validated.manifest)
        manifest_json = validated.manifest.model_dump(mode="json")
        connection = self._checked_connection()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            self._insert_or_verify_dataset(connection, validated)
            self._insert_or_verify_manifest(
                connection,
                validated,
                record_id=record_id,
                manifest_json=manifest_json,
            )
            connection.commit()
            self._validate_connection(connection)
            return self._load_catalog_by_id(connection, record_id).manifest
        except InstrumentRepositoryError:
            connection.rollback()
            raise
        except IntegrityError as error:
            connection.rollback()
            raise InstrumentConflict("Instrument content-address collision") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _insert_or_verify_dataset(
        self,
        connection: Connection,
        routed: RoutedInstrumentSuccess,
    ) -> None:
        self._validate_connection(connection)
        batch = routed.batch
        version = batch.provenance.dataset_version
        dataset = (
            connection.execute(
                select(InstrumentDataset).where(
                    InstrumentDataset.dataset_version == version
                )
            )
            .mappings()
            .one_or_none()
        )
        expected_dataset = {
            "source": batch.provenance.source.value,
            "data_cutoff": batch.provenance.data_cutoff,
            "row_count": len(batch.items),
        }
        if dataset is None:
            connection.execute(
                insert(InstrumentDataset).values(
                    dataset_version=version,
                    **expected_dataset,
                )
            )
            connection.execute(
                insert(InstrumentDatasetItem),
                [
                    _instrument_values(version, ordinal, item)
                    for ordinal, item in enumerate(batch.items)
                ],
            )
            return
        if (
            dataset["source"] != expected_dataset["source"]
            or _aware_utc(cast(datetime, dataset["data_cutoff"]))
            != batch.provenance.data_cutoff
            or dataset["row_count"] != expected_dataset["row_count"]
        ):
            raise InstrumentConflict("Instrument dataset hash collision")
        rows = (
            connection.execute(
                select(InstrumentDatasetItem)
                .where(InstrumentDatasetItem.dataset_version == version)
                .order_by(InstrumentDatasetItem.ordinal)
            )
            .mappings()
            .all()
        )
        expected = tuple(
            _instrument_values(version, ordinal, item)
            for ordinal, item in enumerate(batch.items)
        )
        actual = tuple(
            {key: row[key] for key in values}
            for row, values in zip(rows, expected, strict=False)
        )
        if len(rows) != len(expected) or actual != expected:
            raise InstrumentConflict("Instrument dataset hash collision")

    def _insert_or_verify_manifest(
        self,
        connection: Connection,
        routed: RoutedInstrumentSuccess,
        *,
        record_id: str,
        manifest_json: dict[str, Any],
    ) -> None:
        self._validate_connection(connection)
        row = (
            connection.execute(
                select(InstrumentRoutingManifest).where(
                    InstrumentRoutingManifest.manifest_record_id == record_id
                )
            )
            .mappings()
            .one_or_none()
        )
        expected = {
            "dataset_version": routed.batch.provenance.dataset_version,
            "route_version": routed.manifest.route_version,
            "manifest_json": manifest_json,
            "fetched_at": routed.batch.provenance.fetched_at,
            "data_cutoff": routed.batch.provenance.data_cutoff,
        }
        if row is None:
            connection.execute(
                insert(InstrumentRoutingManifest).values(
                    manifest_record_id=record_id,
                    **expected,
                )
            )
            return
        comparable = {
            "dataset_version": row["dataset_version"],
            "route_version": row["route_version"],
            "manifest_json": row["manifest_json"],
            "fetched_at": _aware_utc(cast(datetime, row["fetched_at"])),
            "data_cutoff": _aware_utc(cast(datetime, row["data_cutoff"])),
        }
        if comparable != expected:
            raise InstrumentConflict("Instrument manifest hash collision")

    def current_manifest(self) -> InstrumentManifestSnapshot:
        return self.current_catalog().manifest

    def current_catalog(
        self,
        *,
        connection: Connection | None = None,
    ) -> InstrumentCatalog:
        if connection is not None:
            self._validate_connection(connection)
            snapshot, items = self._load_current(connection)
            return InstrumentCatalog(snapshot, items)
        with self._checked_connection() as owned_connection:
            snapshot, items = self._load_current(owned_connection)
        return InstrumentCatalog(snapshot, items)

    def pinned_catalog(
        self,
        manifest_record_id: str,
        *,
        connection: Connection | None = None,
    ) -> InstrumentCatalog:
        if type(manifest_record_id) is not str or not manifest_record_id:
            raise InstrumentValidationError("Instrument manifest ID is invalid")
        if connection is not None:
            self._validate_connection(connection)
            return self._load_catalog_by_id(connection, manifest_record_id)
        with self._checked_connection() as owned_connection:
            return self._load_catalog_by_id(owned_connection, manifest_record_id)

    def pinned_manifest(
        self,
        manifest_record_id: str,
        *,
        connection: Connection | None = None,
    ) -> InstrumentManifestSnapshot:
        """Validate pinned manifest/dataset metadata without loading catalog items."""
        if type(manifest_record_id) is not str or not manifest_record_id:
            raise InstrumentValidationError("Instrument manifest ID is invalid")
        if connection is not None:
            self._validate_connection(connection)
            return self._load_manifest_snapshot(connection, manifest_record_id)
        with self._checked_connection() as owned_connection:
            return self._load_manifest_snapshot(owned_connection, manifest_record_id)

    def _validate_connection(self, connection: Connection) -> None:
        if connection.closed or connection.engine is not self._engine:
            raise InstrumentCorruption(
                "Instrument database connection is not repository-bound"
            )
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise InstrumentCorruption(
                "Instrument database identity could not be determined"
            ) from error
        if identity != self._database_identity:
            raise InstrumentCorruption("Instrument database identity changed")

    def _load_catalog_by_id(
        self,
        connection: Connection,
        manifest_record_id: str,
    ) -> InstrumentCatalog:
        self._validate_connection(connection)
        row = self._manifest_row(connection, record_id=manifest_record_id)
        if row is None:
            raise InstrumentNotFound("Instrument manifest was not found")
        snapshot, items = self._validate_catalog(connection, row)
        return InstrumentCatalog(snapshot, items)

    def _load_manifest_snapshot(
        self,
        connection: Connection,
        manifest_record_id: str,
    ) -> InstrumentManifestSnapshot:
        row = self._manifest_row(connection, record_id=manifest_record_id)
        if row is None:
            raise InstrumentNotFound("Instrument manifest was not found")
        return self._validate_manifest_row(row)

    def get(self, symbol: str) -> InstrumentSearchResult:
        validated_symbol = _validated_symbol(symbol)
        with self._checked_connection() as connection:
            snapshot, items = self._load_current(connection)
        for item in items:
            if item.symbol == validated_symbol:
                return InstrumentSearchResult(item, snapshot)
        raise InstrumentNotFound("Instrument was not found")

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> tuple[InstrumentSearchResult, ...]:
        normalized = _validated_query(query)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InstrumentValidationError("Instrument search limit is invalid")
        folded = normalized.casefold()
        symbol_query = normalized.upper()
        with self._checked_connection() as connection:
            snapshot, items = self._load_current(connection)

        def rank(item: Instrument) -> tuple[int, str] | None:
            symbol = item.symbol.upper()
            code = symbol[:6]
            name = item.name.casefold()
            if symbol == symbol_query:
                value = 0
            elif code == symbol_query:
                value = 1
            elif name == folded:
                value = 2
            elif symbol.startswith(symbol_query) or code.startswith(symbol_query):
                value = 3
            elif name.startswith(folded):
                value = 4
            elif symbol_query in symbol or symbol_query in code:
                value = 5
            elif folded in name:
                value = 6
            else:
                return None
            return value, item.symbol

        ranked = tuple(
            sorted(
                ((key, item) for item in items if (key := rank(item)) is not None),
                key=lambda pair: pair[0],
            )
        )
        return tuple(
            InstrumentSearchResult(item, snapshot) for _key, item in ranked[:limit]
        )

    def _load_manifest_by_id(self, record_id: str) -> InstrumentManifestSnapshot:
        with self._checked_connection() as connection:
            return self._load_catalog_by_id(connection, record_id).manifest

    def _load_current(
        self,
        connection: Connection,
    ) -> tuple[InstrumentManifestSnapshot, tuple[Instrument, ...]]:
        self._validate_connection(connection)
        row = self._manifest_row(connection)
        if row is None:
            raise InstrumentNotFound("Instrument catalog is empty")
        return self._validate_catalog(connection, row)

    @staticmethod
    def _manifest_row(
        connection: Connection,
        *,
        record_id: str | None = None,
    ) -> RowMapping | None:
        statement = select(
            InstrumentRoutingManifest,
            InstrumentDataset.source,
            InstrumentDataset.row_count,
            InstrumentDataset.data_cutoff.label("dataset_data_cutoff"),
        ).join(
            InstrumentDataset,
            InstrumentDataset.dataset_version
            == InstrumentRoutingManifest.dataset_version,
        )
        if record_id is None:
            statement = statement.order_by(
                InstrumentRoutingManifest.data_cutoff.desc(),
                InstrumentRoutingManifest.fetched_at.desc(),
                InstrumentRoutingManifest.manifest_record_id.desc(),
            )
        else:
            statement = statement.where(
                InstrumentRoutingManifest.manifest_record_id == record_id
            )
        return connection.execute(statement.limit(1)).mappings().one_or_none()

    @staticmethod
    def _validate_manifest_row(row: RowMapping) -> InstrumentManifestSnapshot:
        try:
            manifest = RoutingManifest.model_validate_json(
                json.dumps(
                    cast(Mapping[str, Any], row["manifest_json"]),
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            record_id = manifest_record_id(manifest)
            source = ProviderId(cast(str, row["source"]))
        except (ValidationError, ValueError, TypeError) as error:
            raise InstrumentCorruption(
                "Stored instrument manifest is corrupt"
            ) from error
        if (
            record_id != row["manifest_record_id"]
            or manifest.route_version != row["route_version"]
            or manifest.upstream_dataset_version != row["dataset_version"]
            or manifest.selected_source != source
            or manifest.upstream_fetched_at
            != _aware_utc(cast(datetime, row["fetched_at"]))
            or manifest.upstream_data_cutoff
            != _aware_utc(cast(datetime, row["data_cutoff"]))
        ):
            raise InstrumentCorruption("Stored instrument manifest is corrupt")
        if manifest.upstream_data_cutoff != _aware_utc(
            cast(datetime, row["dataset_data_cutoff"])
        ):
            raise InstrumentCorruption("Stored instrument dataset is corrupt")
        try:
            row_count = _validated_catalog_item_count(row["row_count"])
        except InstrumentValidationError as error:
            raise InstrumentCorruption(
                "Stored instrument dataset count is corrupt"
            ) from error
        return InstrumentManifestSnapshot(
            manifest_record_id=record_id,
            dataset_version=cast(str, row["dataset_version"]),
            route_version=manifest.route_version,
            source=source,
            fetched_at=manifest.upstream_fetched_at,
            data_cutoff=manifest.upstream_data_cutoff,
            row_count=row_count,
            manifest=manifest,
        )

    @classmethod
    def _validate_catalog(
        cls,
        connection: Connection,
        row: RowMapping,
    ) -> tuple[InstrumentManifestSnapshot, tuple[Instrument, ...]]:
        snapshot = cls._validate_manifest_row(row)
        item_rows = (
            connection.execute(
                select(InstrumentDatasetItem)
                .where(InstrumentDatasetItem.dataset_version == row["dataset_version"])
                .order_by(InstrumentDatasetItem.ordinal)
                .limit(snapshot.row_count + 1)
            )
            .mappings()
            .all()
        )
        if tuple(item["ordinal"] for item in item_rows) != tuple(range(len(item_rows))):
            raise InstrumentCorruption("Stored instrument item order is corrupt")
        items = tuple(_item_from_row(item) for item in item_rows)
        if len(items) != snapshot.row_count:
            raise InstrumentCorruption("Stored instrument dataset count is corrupt")
        expected_version = make_dataset_version(
            source=snapshot.source,
            operation="instruments",
            request={},
            data_cutoff=snapshot.data_cutoff,
            items=items,
        )
        if expected_version != row["dataset_version"]:
            raise InstrumentCorruption("Stored instrument dataset hash is corrupt")
        return snapshot, items

    def close(self) -> None:
        if self._owns_engine:
            self._engine.dispose()
