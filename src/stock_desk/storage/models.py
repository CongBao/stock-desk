from datetime import date, datetime, time, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_task_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )


class TaskRun(Base):
    __tablename__ = "task_run"
    __table_args__ = (Index("ix_task_run_status_created_at", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_task_id)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class TaskEvent(Base):
    __tablename__ = "task_event"
    __table_args__ = (
        Index("ix_task_event_task_id_occurred_at", "task_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_task_id)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_run.id", ondelete="CASCADE"), nullable=False
    )
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class MarketDataset(Base):
    __tablename__ = "market_dataset"
    __table_args__ = (
        CheckConstraint(
            "row_count > 0",
            name="ck_market_dataset_row_count_positive",
        ),
        Index(
            "ix_market_dataset_exact_query",
            "symbol",
            "period",
            "adjustment",
            "query_start",
            "query_end",
        ),
        UniqueConstraint(
            "dataset_version",
            "symbol",
            name="uq_market_dataset_version_symbol",
        ),
        {"sqlite_with_rowid": False},
    )

    dataset_version: Mapped[str] = mapped_column(String(71), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    period: Mapped[str] = mapped_column(String(8), nullable=False)
    adjustment: Mapped[str] = mapped_column(String(8), nullable=False)
    query_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    query_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class MarketDatasetPartition(Base):
    __tablename__ = "market_dataset_partition"
    __table_args__ = (
        CheckConstraint(
            "partition_year BETWEEN 1900 AND 9999",
            name="ck_market_dataset_partition_year",
        ),
        CheckConstraint(
            "row_count > 0",
            name="ck_market_dataset_partition_row_count_positive",
        ),
        CheckConstraint(
            "byte_size > 0",
            name="ck_market_dataset_partition_byte_size_positive",
        ),
        UniqueConstraint(
            "dataset_version",
            "partition_year",
            name="uq_market_dataset_partition_dataset_year",
        ),
        UniqueConstraint(
            "relative_path",
            name="uq_market_dataset_partition_relative_path",
        ),
        {"sqlite_with_rowid": False},
    )

    dataset_version: Mapped[str] = mapped_column(
        String(71),
        ForeignKey("market_dataset.dataset_version", ondelete="RESTRICT"),
        primary_key=True,
    )
    partition_manifest_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    partition_year: Mapped[int] = mapped_column(Integer, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    physical_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class MarketRoutingManifest(Base):
    __tablename__ = "market_routing_manifest"
    __table_args__ = (
        Index(
            "ix_market_routing_manifest_dataset_fetched_at",
            "dataset_version",
            "fetched_at",
        ),
        Index("ix_market_routing_manifest_route_version", "route_version"),
        UniqueConstraint(
            "manifest_record_id",
            "dataset_version",
            "symbol",
            name="uq_market_routing_manifest_provenance",
        ),
        ForeignKeyConstraint(
            ["dataset_version", "symbol"],
            ["market_dataset.dataset_version", "market_dataset.symbol"],
            ondelete="RESTRICT",
        ),
        {"sqlite_with_rowid": False},
    )

    manifest_record_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    dataset_version: Mapped[str] = mapped_column(
        String(71),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    route_version: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class MarketUpdateItem(Base):
    __tablename__ = "market_update_item"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_market_update_item_ordinal"),
        CheckConstraint(
            "status IN ('succeeded', 'failed', 'cancelled')",
            name="ck_market_update_item_status",
        ),
        CheckConstraint(
            "(status = 'succeeded' "
            "AND manifest_record_id IS NOT NULL "
            "AND dataset_version IS NOT NULL "
            "AND reason IS NULL) "
            "OR (status IN ('failed', 'cancelled') "
            "AND manifest_record_id IS NULL "
            "AND dataset_version IS NULL "
            "AND reason IS NOT NULL)",
            name="ck_market_update_item_outcome",
        ),
        UniqueConstraint(
            "task_id",
            "symbol",
            name="uq_market_update_item_task_symbol",
        ),
        ForeignKeyConstraint(
            ["manifest_record_id", "dataset_version", "symbol"],
            [
                "market_routing_manifest.manifest_record_id",
                "market_routing_manifest.dataset_version",
                "market_routing_manifest.symbol",
            ],
            ondelete="RESTRICT",
        ),
        {"sqlite_with_rowid": False},
    )

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_run.id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest_record_id: Mapped[str | None] = mapped_column(
        String(71),
        nullable=True,
    )
    dataset_version: Mapped[str | None] = mapped_column(
        String(71),
        ForeignKey("market_dataset.dataset_version", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class MarketUpdateSchedule(Base):
    __tablename__ = "market_update_schedule"
    __table_args__ = (
        CheckConstraint(
            "timezone = 'Asia/Shanghai'",
            name="ck_market_update_schedule_timezone",
        ),
        Index(
            "ix_market_update_schedule_due",
            "enabled",
            "local_time",
            "last_enqueued_local_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_task_id)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Shanghai", nullable=False
    )
    local_time: Mapped[time] = mapped_column(Time, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    last_enqueued_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )


class MarketUpdateOccurrence(Base):
    __tablename__ = "market_update_occurrence"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            name="uq_market_update_occurrence_task_id",
        ),
        {"sqlite_with_rowid": False},
    )

    schedule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("market_update_schedule.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "task_run.id",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
