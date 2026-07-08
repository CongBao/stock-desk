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
from sqlalchemy.orm import Mapped, mapped_column

from stock_desk.storage.base import Base


_MODEL_CONFIG_DISPLAY_NAME_CHECK = " AND ".join(
    f"instr(display_name, char({codepoint})) = 0" for codepoint in (*range(32), 127)
)
_TIMESTAMP_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] "
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
    "[0-9][0-9][0-9][0-9][0-9][0-9]"
)


def _model_config_timestamp_check(column: str) -> str:
    year = f"CAST(substr({column}, 1, 4) AS INTEGER)"
    month = f"CAST(substr({column}, 6, 2) AS INTEGER)"
    day = f"CAST(substr({column}, 9, 2) AS INTEGER)"
    maximum_day = (
        f"CASE {month} WHEN 2 THEN 28 + CASE WHEN "
        f"(({year} % 4 = 0 AND {year} % 100 <> 0) OR {year} % 400 = 0) "
        f"THEN 1 ELSE 0 END WHEN 4 THEN 30 WHEN 6 THEN 30 "
        f"WHEN 9 THEN 30 WHEN 11 THEN 30 ELSE 31 END"
    )
    return (
        f"length({column}) = 26 AND {column} GLOB '{_TIMESTAMP_GLOB}' "
        f"AND {year} BETWEEN 1 AND 9999 "
        f"AND {month} BETWEEN 1 AND 12 "
        f"AND {day} BETWEEN 1 AND ({maximum_day}) "
        f"AND CAST(substr({column}, 12, 2) AS INTEGER) BETWEEN 0 AND 23 "
        f"AND CAST(substr({column}, 15, 2) AS INTEGER) BETWEEN 0 AND 59 "
        f"AND CAST(substr({column}, 18, 2) AS INTEGER) BETWEEN 0 AND 59"
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_task_id() -> str:
    return str(uuid4())


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )


class AnalysisModelConfig(Base):
    __tablename__ = "analysis_model_config"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 71 AND substr(id, 1, 7) = 'sha256:' "
            "AND substr(id, 8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_analysis_model_config_id",
        ),
        CheckConstraint(
            "id = public_config_hash",
            name="ck_analysis_model_config_hash_binding",
        ),
        CheckConstraint(
            "length(public_config_hash) = 71 "
            "AND substr(public_config_hash, 1, 7) = 'sha256:' "
            "AND substr(public_config_hash, 8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_analysis_model_config_hash",
        ),
        CheckConstraint(
            "json_valid(public_config_json) = 1 "
            "AND json_type(public_config_json) = 'object' "
            "AND length(CAST(public_config_json AS BLOB)) <= 16384",
            name="ck_analysis_model_config_json",
        ),
        CheckConstraint(
            "coalesce(json_type(public_config_json, '$.schema_version'), '') "
            "= 'text' AND json_extract(public_config_json, '$.schema_version') = "
            "'analysis-model-public-v1' "
            "AND coalesce(json_type(public_config_json, '$.provider'), '') = 'text' "
            "AND coalesce(json_type(public_config_json, '$.model'), '') = 'text' "
            "AND coalesce(json_type(public_config_json, '$.base_url'), '') = 'text' "
            "AND length(json_extract(public_config_json, '$.base_url')) "
            "BETWEEN 1 AND 2048 "
            "AND coalesce(json_type(public_config_json, '$.temperature'), '') "
            "= 'real' AND json_extract(public_config_json, '$.temperature') "
            "BETWEEN 0.0 AND 2.0 "
            "AND coalesce(json_type(public_config_json, '$.timeout_seconds'), '') "
            "= 'real' AND json_extract(public_config_json, '$.timeout_seconds') "
            "BETWEEN 1.0 AND 300.0 "
            "AND coalesce(json_type(public_config_json, '$.max_output_tokens'), '') "
            "= 'integer' "
            "AND json_extract(public_config_json, '$.max_output_tokens') "
            "BETWEEN 1 AND 65536 "
            "AND coalesce(json_type(public_config_json, '$.api_key_configured'), '') "
            "IN ('true','false') "
            "AND coalesce(json_type(public_config_json, '$.secret_reference_id'), '') "
            "IN ('null','text') "
            "AND json_type(public_config_json, '$.api_key') IS NULL",
            name="ck_analysis_model_config_public_shape",
        ),
        CheckConstraint(
            "provider IN ('deepseek','openai_compatible','ollama') "
            "AND provider = json_extract(public_config_json, '$.provider')",
            name="ck_analysis_model_config_provider",
        ),
        CheckConstraint(
            "length(model) BETWEEN 1 AND 256 "
            "AND model = trim(model) "
            "AND model = json_extract(public_config_json, '$.model')",
            name="ck_analysis_model_config_model",
        ),
        CheckConstraint(
            "(secret_reference_id IS NULL "
            "AND json_type(public_config_json, '$.secret_reference_id') = 'null' "
            "AND json_extract(public_config_json, '$.api_key_configured') = 0) "
            "OR (secret_reference_id IS NOT NULL "
            "AND secret_reference_id = "
            "json_extract(public_config_json, '$.secret_reference_id') "
            "AND json_extract(public_config_json, '$.api_key_configured') = 1)",
            name="ck_analysis_model_config_secret_binding",
        ),
        CheckConstraint(
            "secret_reference_id IS NULL "
            "OR secret_reference_id = 'analysis_model_api_key' "
            "OR (length(secret_reference_id) = 55 "
            "AND substr(secret_reference_id, 1, 23) = 'analysis_model_api_key_' "
            "AND substr(secret_reference_id, 24) NOT GLOB '*[^0-9a-f]*')",
            name="ck_analysis_model_config_secret_reference",
        ),
        CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name="ck_analysis_model_config_supersedes",
        ),
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 128 "
            "AND display_name = trim(display_name) "
            f"AND {_MODEL_CONFIG_DISPLAY_NAME_CHECK}",
            name="ck_analysis_model_config_display_name",
        ),
        CheckConstraint(
            "status IN ('unverified','verified','failed','disabled')",
            name="ck_analysis_model_config_status",
        ),
        CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 "
            "AND error_code NOT GLOB '*[^a-z0-9_]*')",
            name="ck_analysis_model_config_error_code",
        ),
        CheckConstraint(
            "typeof(revision) = 'integer' AND revision >= 0",
            name="ck_analysis_model_config_revision",
        ),
        CheckConstraint(
            f"({_model_config_timestamp_check('created_at')}) AND "
            f"({_model_config_timestamp_check('updated_at')}) AND "
            "(verified_at IS NULL OR "
            f"({_model_config_timestamp_check('verified_at')})) AND "
            "(last_tested_at IS NULL OR "
            f"({_model_config_timestamp_check('last_tested_at')}))",
            name="ck_analysis_model_config_timestamp_format",
        ),
        CheckConstraint(
            "updated_at >= created_at "
            "AND (verified_at IS NULL OR "
            "(verified_at >= created_at AND verified_at <= updated_at)) "
            "AND (last_tested_at IS NULL OR "
            "(last_tested_at >= created_at AND last_tested_at <= updated_at))",
            name="ck_analysis_model_config_timestamp_order",
        ),
        CheckConstraint(
            "(status = 'unverified' AND verified_at IS NULL "
            "AND last_tested_at IS NULL AND error_code IS NULL) OR "
            "(status = 'verified' AND verified_at IS NOT NULL "
            "AND last_tested_at IS NOT NULL AND verified_at = last_tested_at "
            "AND error_code IS NULL) OR "
            "(status = 'failed' AND verified_at IS NULL "
            "AND last_tested_at IS NOT NULL AND error_code IS NOT NULL) OR "
            "(status = 'disabled' AND error_code IS NULL "
            "AND (verified_at IS NULL OR (last_tested_at IS NOT NULL "
            "AND verified_at = last_tested_at)))",
            name="ck_analysis_model_config_state_shape",
        ),
        ForeignKeyConstraint(
            ["supersedes_id"],
            ["analysis_model_config.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("supersedes_id", name="uq_analysis_model_config_supersedes"),
        Index(
            "ix_analysis_model_config_list",
            "display_name",
            "id",
        ),
        Index(
            "ix_analysis_model_config_status",
            "status",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(71), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    public_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    public_config_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    secret_reference_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class TaskRun(Base):
    __tablename__ = "task_run"
    __table_args__ = (
        Index("ix_task_run_status_created_at", "status", "created_at"),
        Index(
            "ix_task_run_backtest_lease",
            "kind",
            "status",
            "lease_expires_at",
            "created_at",
        ),
    )

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
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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


class MarketDatasetTimestamp(Base):
    __tablename__ = "market_dataset_timestamp"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_market_dataset_timestamp_ordinal"),
        UniqueConstraint(
            "dataset_version",
            "timestamp",
            name="uq_market_dataset_timestamp_value",
        ),
        Index(
            "ix_market_dataset_timestamp_lookup",
            "dataset_version",
            "timestamp",
        ),
        {"sqlite_with_rowid": False},
    )

    dataset_version: Mapped[str] = mapped_column(
        String(71),
        ForeignKey("market_dataset.dataset_version", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketDatasetTimestampSeal(Base):
    __tablename__ = "market_dataset_timestamp_seal"
    __table_args__ = (
        CheckConstraint(
            "row_count > 0",
            name="ck_market_dataset_timestamp_seal_row_count",
        ),
    )

    dataset_version: Mapped[str] = mapped_column(
        String(71),
        ForeignKey("market_dataset.dataset_version", ondelete="CASCADE"),
        primary_key=True,
    )
    index_version: Mapped[str] = mapped_column(String(32), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp_digest: Mapped[str] = mapped_column(String(71), nullable=False)


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


class ExecutionStatusDataset(Base):
    __tablename__ = "execution_status_dataset"
    __table_args__ = (
        CheckConstraint(
            "row_count > 0",
            name="ck_execution_status_dataset_row_count_positive",
        ),
        CheckConstraint(
            "period IN ('1d', '1w', '60m')",
            name="ck_execution_status_dataset_period",
        ),
        Index(
            "ix_execution_status_dataset_exact_query",
            "symbol",
            "exchange",
            "period",
            "query_start",
            "query_end",
        ),
        {"sqlite_with_rowid": False},
    )

    dataset_version: Mapped[str] = mapped_column(String(71), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    exchange: Mapped[str] = mapped_column(String(2), nullable=False)
    period: Mapped[str] = mapped_column(String(8), nullable=False)
    query_start: Mapped[date] = mapped_column(Date, nullable=False)
    query_end: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    data_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class ExecutionStatusRoutingManifest(Base):
    __tablename__ = "execution_status_routing_manifest"
    __table_args__ = (
        Index(
            "ix_execution_status_manifest_latest",
            "dataset_version",
            "fetched_at",
        ),
        {"sqlite_with_rowid": False},
    )

    manifest_record_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    dataset_version: Mapped[str] = mapped_column(
        String(71),
        ForeignKey("execution_status_dataset.dataset_version", ondelete="RESTRICT"),
        nullable=False,
    )
    route_version: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
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


class InstrumentDataset(Base):
    __tablename__ = "instrument_dataset"
    __table_args__ = (
        CheckConstraint(
            "row_count BETWEEN 1 AND 50000",
            name="ck_instrument_dataset_row_count_bounded",
        ),
        {"sqlite_with_rowid": False},
    )

    dataset_version: Mapped[str] = mapped_column(String(71), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    data_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class InstrumentDatasetItem(Base):
    __tablename__ = "instrument_dataset_item"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 49999",
            name="ck_instrument_dataset_item_ordinal",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 255",
            name="ck_instrument_dataset_item_name_length",
        ),
        UniqueConstraint(
            "dataset_version",
            "ordinal",
            name="uq_instrument_dataset_item_ordinal",
        ),
        {"sqlite_with_rowid": False},
    )

    dataset_version: Mapped[str] = mapped_column(
        String(71),
        ForeignKey("instrument_dataset.dataset_version", ondelete="RESTRICT"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(String(9), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange: Mapped[str] = mapped_column(String(2), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    instrument_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    listing_status: Mapped[str] = mapped_column(String(16), nullable=False)
    listed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class InstrumentRoutingManifest(Base):
    __tablename__ = "instrument_routing_manifest"
    __table_args__ = (
        UniqueConstraint(
            "manifest_record_id",
            "dataset_version",
            name="uq_instrument_routing_manifest_dataset",
        ),
        Index(
            "ix_instrument_routing_manifest_current",
            "data_cutoff",
            "fetched_at",
            "manifest_record_id",
        ),
        {"sqlite_with_rowid": False},
    )

    manifest_record_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    dataset_version: Mapped[str] = mapped_column(
        String(71),
        ForeignKey("instrument_dataset.dataset_version", ondelete="RESTRICT"),
        nullable=False,
    )
    route_version: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    data_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class PresetPoolSnapshot(Base):
    __tablename__ = "preset_pool_snapshot"
    __table_args__ = (
        CheckConstraint(
            "pool_id = 'preset:' || preset_key",
            name="ck_preset_pool_snapshot_logical_id",
        ),
        CheckConstraint(
            "category IN ('all_a', 'index', 'industry')",
            name="ck_preset_pool_snapshot_category",
        ),
        CheckConstraint(
            "complete = 1",
            name="ck_preset_pool_snapshot_complete",
        ),
        CheckConstraint(
            "member_count BETWEEN 1 AND 10000",
            name="ck_preset_pool_snapshot_member_count",
        ),
        UniqueConstraint(
            "snapshot_id",
            "instrument_dataset_version",
            name="uq_preset_pool_snapshot_dataset",
        ),
        ForeignKeyConstraint(
            ["instrument_manifest_record_id", "instrument_dataset_version"],
            [
                "instrument_routing_manifest.manifest_record_id",
                "instrument_routing_manifest.dataset_version",
            ],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_preset_pool_snapshot_latest",
            "preset_key",
            "data_cutoff",
            "fetched_at",
            "snapshot_id",
        ),
        {"sqlite_with_rowid": False},
    )

    snapshot_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    pool_id: Mapped[str] = mapped_column(String(71), nullable=False)
    preset_key: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    composition_dataset_version: Mapped[str] = mapped_column(String(71), nullable=False)
    composition_route_version: Mapped[str] = mapped_column(String(71), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    data_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    instrument_manifest_record_id: Mapped[str] = mapped_column(
        String(71), nullable=False
    )
    instrument_dataset_version: Mapped[str] = mapped_column(String(71), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class PresetPoolMember(Base):
    __tablename__ = "preset_pool_member"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 9999",
            name="ck_preset_pool_member_ordinal",
        ),
        UniqueConstraint(
            "snapshot_id",
            "symbol",
            name="uq_preset_pool_member_symbol",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "instrument_dataset_version"],
            [
                "preset_pool_snapshot.snapshot_id",
                "preset_pool_snapshot.instrument_dataset_version",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["instrument_dataset_version", "symbol"],
            [
                "instrument_dataset_item.dataset_version",
                "instrument_dataset_item.symbol",
            ],
            ondelete="RESTRICT",
        ),
        {"sqlite_with_rowid": False},
    )

    snapshot_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_dataset_version: Mapped[str] = mapped_column(String(71), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class CustomPool(Base):
    __tablename__ = "custom_pool"
    __table_args__ = (
        CheckConstraint(
            "length(name) BETWEEN 1 AND 64 AND trim(name) = name",
            name="ck_custom_pool_name",
        ),
        CheckConstraint("revision > 0", name="ck_custom_pool_revision"),
        CheckConstraint(
            "member_count BETWEEN 1 AND 5000",
            name="ck_custom_pool_member_count",
        ),
        CheckConstraint(
            "length(member_digest) = 71 AND substr(member_digest, 1, 7) = 'sha256:'",
            name="ck_custom_pool_member_digest",
        ),
        CheckConstraint(
            "length(state_digest) = 71 AND substr(state_digest, 1, 7) = 'sha256:'",
            name="ck_custom_pool_state_digest",
        ),
        UniqueConstraint(
            "pool_id",
            "revision",
            "instrument_dataset_version",
            name="uq_custom_pool_revision_dataset",
        ),
        ForeignKeyConstraint(
            ["instrument_manifest_record_id", "instrument_dataset_version"],
            [
                "instrument_routing_manifest.manifest_record_id",
                "instrument_routing_manifest.dataset_version",
            ],
            ondelete="RESTRICT",
        ),
    )

    pool_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_manifest_record_id: Mapped[str] = mapped_column(
        String(71), nullable=False
    )
    instrument_dataset_version: Mapped[str] = mapped_column(String(71), nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    state_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, nullable=False
    )


class CustomPoolMember(Base):
    __tablename__ = "custom_pool_member"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 4999",
            name="ck_custom_pool_member_ordinal",
        ),
        CheckConstraint(
            "member_revision > 0",
            name="ck_custom_pool_member_revision",
        ),
        UniqueConstraint(
            "pool_id",
            "symbol",
            name="uq_custom_pool_member_symbol",
        ),
        ForeignKeyConstraint(
            ["pool_id", "member_revision", "instrument_dataset_version"],
            [
                "custom_pool.pool_id",
                "custom_pool.revision",
                "custom_pool.instrument_dataset_version",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["instrument_dataset_version", "symbol"],
            [
                "instrument_dataset_item.dataset_version",
                "instrument_dataset_item.symbol",
            ],
            ondelete="RESTRICT",
        ),
    )

    pool_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_dataset_version: Mapped[str] = mapped_column(String(71), nullable=False)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
