"""Immutable SQLite catalog for execution-status evidence snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import cast

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Connection, Engine

from stock_desk.market.execution_status import (
    ExecutionStatusQuery,
    ExecutionStatusSnapshot,
)
from stock_desk.market.types import Exchange, Period
from stock_desk.market.provenance import (
    RoutedExecutionStatusSuccess,
    RoutingManifest,
)
from stock_desk.storage.models import (
    ExecutionStatusDataset,
    ExecutionStatusRoutingManifest,
)
from stock_desk.storage.database import (
    DatabaseIdentity,
    DatabaseIdentityError,
    connection_database_identity,
)


@dataclass(frozen=True, slots=True)
class StoredExecutionStatus:
    manifest_record_id: str
    dataset_version: str
    route_version: str


@dataclass(frozen=True, slots=True)
class CatalogExecutionStatusPin:
    manifest_record_id: str
    dataset_version: str
    route_version: str
    query: ExecutionStatusQuery


def execution_status_manifest_record_id(manifest: RoutingManifest) -> str:
    encoded = json.dumps(
        manifest.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class ExecutionStatusLake:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        try:
            with engine.connect() as connection:
                self._database_identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise ValueError(
                "execution-status database identity could not be determined"
            ) from error

    @property
    def database_identity(self) -> DatabaseIdentity:
        return self._database_identity

    def _validate_connection(self, connection: Connection) -> None:
        if connection.closed or connection.engine is not self._engine:
            raise ValueError("execution-status connection is not lake-bound")
        try:
            identity = connection_database_identity(connection)
        except DatabaseIdentityError as error:
            raise ValueError("execution-status database identity changed") from error
        if identity != self._database_identity:
            raise ValueError("execution-status database identity changed")

    def catalog_latest_covering_many(
        self,
        connection: Connection,
        queries: tuple[ExecutionStatusQuery, ...],
    ) -> dict[str, CatalogExecutionStatusPin]:
        self._validate_connection(connection)
        canonical = tuple(
            ExecutionStatusQuery.model_validate(query.model_dump(mode="python"))
            for query in queries
        )
        if len({query.symbol for query in canonical}) != len(canonical):
            raise ValueError("execution-status pin queries require unique symbols")
        result: dict[str, CatalogExecutionStatusPin] = {}
        groups: dict[tuple[object, ...], set[str]] = {}
        for query in canonical:
            groups.setdefault((query.period, query.start, query.end), set()).add(
                query.symbol
            )
        expected = {query.symbol: query for query in canonical}
        for (period, start, end), symbols in groups.items():
            canonical_period = cast(Period, period)
            rank = (
                func.row_number()
                .over(
                    partition_by=ExecutionStatusDataset.symbol,
                    order_by=(
                        ExecutionStatusDataset.data_cutoff.desc(),
                        ExecutionStatusRoutingManifest.fetched_at.desc(),
                        ExecutionStatusDataset.query_start.asc(),
                        ExecutionStatusRoutingManifest.manifest_record_id.desc(),
                    ),
                )
                .label("catalog_rank")
            )
            ranked = (
                select(
                    ExecutionStatusRoutingManifest.manifest_record_id,
                    ExecutionStatusRoutingManifest.dataset_version,
                    ExecutionStatusRoutingManifest.route_version,
                    ExecutionStatusRoutingManifest.manifest_json,
                    ExecutionStatusDataset.symbol,
                    ExecutionStatusDataset.exchange,
                    ExecutionStatusDataset.period,
                    ExecutionStatusDataset.query_start,
                    ExecutionStatusDataset.query_end,
                    rank,
                )
                .join(
                    ExecutionStatusDataset,
                    ExecutionStatusDataset.dataset_version
                    == ExecutionStatusRoutingManifest.dataset_version,
                )
                .where(
                    ExecutionStatusDataset.symbol.in_(tuple(sorted(symbols))),
                    ExecutionStatusDataset.period == canonical_period.value,
                    ExecutionStatusDataset.query_start <= start,
                    ExecutionStatusDataset.query_end >= end,
                )
                .subquery()
            )
            rows = connection.execute(
                select(ranked)
                .where(ranked.c.catalog_rank == 1)
                .order_by(ranked.c.symbol)
            ).mappings()
            for row in rows:
                symbol = row["symbol"]
                if symbol not in symbols:
                    continue
                query = ExecutionStatusQuery(
                    symbol=symbol,
                    exchange=Exchange(row["exchange"]),
                    period=Period(row["period"]),
                    start=row["query_start"],
                    end=row["query_end"],
                )
                manifest = RoutingManifest.model_validate_json(
                    json.dumps(row["manifest_json"], allow_nan=False)
                )
                wanted = expected[symbol]
                if (
                    query.exchange is not wanted.exchange
                    or execution_status_manifest_record_id(manifest)
                    != row["manifest_record_id"]
                    or manifest.upstream_dataset_version != row["dataset_version"]
                    or manifest.route_version != row["route_version"]
                ):
                    raise ValueError("execution-status catalog identity is invalid")
                result[symbol] = CatalogExecutionStatusPin(
                    manifest_record_id=row["manifest_record_id"],
                    dataset_version=row["dataset_version"],
                    route_version=row["route_version"],
                    query=query,
                )
        return result

    def write(self, routed: RoutedExecutionStatusSuccess) -> StoredExecutionStatus:
        validated = RoutedExecutionStatusSuccess.model_validate(
            routed.model_dump(mode="python")
        )
        result = validated.result
        manifest = validated.manifest
        record_id = execution_status_manifest_record_id(manifest)
        snapshot_json = result.model_dump(mode="json")
        manifest_json = manifest.model_dump(mode="json")
        with self._engine.begin() as connection:
            existing_dataset = connection.execute(
                select(ExecutionStatusDataset.snapshot_json).where(
                    ExecutionStatusDataset.dataset_version == result.dataset_version
                )
            ).scalar_one_or_none()
            if existing_dataset is None:
                connection.execute(
                    insert(ExecutionStatusDataset).values(
                        dataset_version=result.dataset_version,
                        source=result.source.value,
                        symbol=result.query.symbol,
                        exchange=result.query.exchange.value,
                        period=result.query.period.value,
                        query_start=result.query.start,
                        query_end=result.query.end,
                        fetched_at=result.fetched_at,
                        data_cutoff=result.data_cutoff,
                        row_count=len(result.days) + len(result.eligibility),
                        snapshot_json=snapshot_json,
                    )
                )
            elif existing_dataset != snapshot_json:
                raise ValueError("execution-status dataset identity collision")

            existing_manifest = connection.execute(
                select(ExecutionStatusRoutingManifest.manifest_json).where(
                    ExecutionStatusRoutingManifest.manifest_record_id == record_id
                )
            ).scalar_one_or_none()
            if existing_manifest is None:
                connection.execute(
                    insert(ExecutionStatusRoutingManifest).values(
                        manifest_record_id=record_id,
                        dataset_version=result.dataset_version,
                        route_version=manifest.route_version,
                        manifest_json=manifest_json,
                        fetched_at=result.fetched_at,
                    )
                )
            elif existing_manifest != manifest_json:
                raise ValueError("execution-status manifest identity collision")
        return StoredExecutionStatus(
            manifest_record_id=record_id,
            dataset_version=result.dataset_version,
            route_version=manifest.route_version,
        )

    def read(self, manifest_record_id: str) -> RoutedExecutionStatusSuccess:
        statement = (
            select(
                ExecutionStatusDataset.snapshot_json,
                ExecutionStatusRoutingManifest.manifest_json,
                ExecutionStatusDataset.dataset_version,
                ExecutionStatusDataset.source,
                ExecutionStatusDataset.symbol,
                ExecutionStatusDataset.exchange,
                ExecutionStatusDataset.period,
                ExecutionStatusDataset.query_start,
                ExecutionStatusDataset.query_end,
                ExecutionStatusDataset.fetched_at,
                ExecutionStatusDataset.data_cutoff,
                ExecutionStatusDataset.row_count,
                ExecutionStatusRoutingManifest.manifest_record_id,
                ExecutionStatusRoutingManifest.dataset_version,
                ExecutionStatusRoutingManifest.route_version,
                ExecutionStatusRoutingManifest.fetched_at,
            )
            .join(
                ExecutionStatusRoutingManifest,
                ExecutionStatusRoutingManifest.dataset_version
                == ExecutionStatusDataset.dataset_version,
            )
            .where(
                ExecutionStatusRoutingManifest.manifest_record_id == manifest_record_id
            )
        )
        with self._engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        if row is None:
            raise KeyError("execution-status manifest was not found")
        result = ExecutionStatusSnapshot.model_validate_json(
            json.dumps(row[0], allow_nan=False)
        )
        manifest = RoutingManifest.model_validate_json(
            json.dumps(row[1], allow_nan=False)
        )
        routed = RoutedExecutionStatusSuccess(
            result=result,
            manifest=manifest,
        )
        stored_fetched_at = row[9]
        stored_cutoff = row[10]
        manifest_fetched_at = row[15]
        if not all(
            isinstance(item, datetime)
            for item in (stored_fetched_at, stored_cutoff, manifest_fetched_at)
        ):
            raise ValueError("execution-status catalog identity is invalid")
        assert isinstance(stored_fetched_at, datetime)
        assert isinstance(stored_cutoff, datetime)
        assert isinstance(manifest_fetched_at, datetime)
        identity_valid = (
            row[2] == result.dataset_version
            and row[3] == result.source.value
            and row[4] == result.query.symbol
            and row[5] == result.query.exchange.value
            and row[6] == result.query.period.value
            and row[7] == result.query.start
            and row[8] == result.query.end
            and _same_instant(stored_fetched_at, result.fetched_at)
            and _same_instant(stored_cutoff, result.data_cutoff)
            and row[11] == len(result.days) + len(result.eligibility)
            and row[12] == manifest_record_id
            and row[12] == execution_status_manifest_record_id(manifest)
            and row[13] == result.dataset_version
            and row[14] == manifest.route_version
            and _same_instant(manifest_fetched_at, result.fetched_at)
        )
        if not identity_valid:
            raise ValueError("execution-status catalog identity is invalid")
        return routed

    def latest_exact(self, query: ExecutionStatusQuery) -> StoredExecutionStatus | None:
        statement = (
            select(
                ExecutionStatusRoutingManifest.manifest_record_id,
                ExecutionStatusRoutingManifest.dataset_version,
                ExecutionStatusRoutingManifest.route_version,
            )
            .join(
                ExecutionStatusDataset,
                ExecutionStatusDataset.dataset_version
                == ExecutionStatusRoutingManifest.dataset_version,
            )
            .where(
                ExecutionStatusDataset.symbol == query.symbol,
                ExecutionStatusDataset.exchange == query.exchange.value,
                ExecutionStatusDataset.period == query.period.value,
                ExecutionStatusDataset.query_start == query.start,
                ExecutionStatusDataset.query_end == query.end,
            )
            .order_by(
                ExecutionStatusRoutingManifest.fetched_at.desc(),
                ExecutionStatusRoutingManifest.manifest_record_id.desc(),
            )
            .limit(1)
        )
        with self._engine.connect() as connection:
            row = connection.execute(statement).one_or_none()
        return (
            None
            if row is None
            else StoredExecutionStatus(
                manifest_record_id=row[0],
                dataset_version=row[1],
                route_version=row[2],
            )
        )


__all__ = [
    "ExecutionStatusLake",
    "StoredExecutionStatus",
    "execution_status_manifest_record_id",
]


def _same_instant(stored: datetime, expected: datetime) -> bool:
    if stored.tzinfo is None or stored.utcoffset() is None:
        stored = stored.replace(tzinfo=timezone.utc)
    return stored.astimezone(timezone.utc) == expected.astimezone(timezone.utc)
