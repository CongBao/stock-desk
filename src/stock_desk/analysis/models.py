from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from stock_desk.storage.base import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_HASH_CHECK = "length({name}) = 71 AND substr({name}, 1, 7) = 'sha256:'"


def _json_object(name: str, maximum: int) -> str:
    return (
        f"json_valid({name}) = 1 AND json_type({name}) = 'object' "
        f"AND length(CAST({name} AS BLOB)) <= {maximum}"
    )


class AnalysisRunRow(Base):
    __tablename__ = "analysis_run"
    __table_args__ = (
        CheckConstraint(
            "symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].SH' OR "
            "symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].SZ' OR "
            "symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].BJ'",
            name="ck_analysis_run_symbol",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','partial',"
            "'insufficient_evidence','failed','cancelled')",
            name="ck_analysis_run_status",
        ),
        CheckConstraint(
            "requested_stage IS NULL OR requested_stage IN "
            "('technical','fundamental_news','bull','bear','risk_decision')",
            name="ck_analysis_run_requested_stage",
        ),
        CheckConstraint(
            "current_stage IS NULL OR current_stage IN "
            "('market','fundamentals','announcements','news','technical',"
            "'fundamental_news','bull','bear','risk_decision')",
            name="ck_analysis_run_current_stage",
        ),
        CheckConstraint(
            "(snapshot_id IS NULL AND snapshot_json IS NULL AND snapshot_hash IS NULL "
            "AND evidence_graph_json IS NULL AND evidence_graph_hash IS NULL) OR "
            "(snapshot_id IS NOT NULL AND snapshot_json IS NOT NULL "
            "AND snapshot_hash IS NOT NULL AND evidence_graph_json IS NOT NULL "
            "AND evidence_graph_hash IS NOT NULL)",
            name="ck_analysis_run_input_binding",
        ),
        CheckConstraint(
            "snapshot_id IS NULL OR " + _HASH_CHECK.format(name="snapshot_id"),
            name="ck_analysis_run_snapshot_id",
        ),
        CheckConstraint(
            "snapshot_hash IS NULL OR " + _HASH_CHECK.format(name="snapshot_hash"),
            name="ck_analysis_run_snapshot_hash",
        ),
        CheckConstraint(
            "evidence_graph_hash IS NULL OR "
            + _HASH_CHECK.format(name="evidence_graph_hash"),
            name="ck_analysis_run_evidence_hash",
        ),
        CheckConstraint(
            _HASH_CHECK.format(name="retry_policy_hash"),
            name="ck_analysis_run_retry_hash",
        ),
        CheckConstraint(
            f"snapshot_json IS NULL OR ({_json_object('snapshot_json', 2_097_152)})",
            name="ck_analysis_run_snapshot_json",
        ),
        CheckConstraint(
            "evidence_graph_json IS NULL OR "
            f"({_json_object('evidence_graph_json', 4_194_304)})",
            name="ck_analysis_run_evidence_json",
        ),
        CheckConstraint(
            _json_object("retry_policy_json", 16_384),
            name="ck_analysis_run_retry_json",
        ),
        CheckConstraint(
            f"error_json IS NULL OR ({_json_object('error_json', 16_384)})",
            name="ck_analysis_run_error_json",
        ),
        CheckConstraint(
            _HASH_CHECK.format(name="config_fingerprint"),
            name="ck_analysis_run_config_fingerprint",
        ),
        CheckConstraint(
            _HASH_CHECK.format(name="model_config_id"),
            name="ck_analysis_run_model_config_id",
        ),
        CheckConstraint(
            "length(model_provider) BETWEEN 1 AND 64",
            name="ck_analysis_run_model_provider",
        ),
        CheckConstraint(
            "length(model_name) BETWEEN 1 AND 256",
            name="ck_analysis_run_model_name",
        ),
        CheckConstraint(
            _json_object("model_config_json", 16_384),
            name="ck_analysis_run_model_config_json",
        ),
        CheckConstraint(
            _HASH_CHECK.format(name="model_config_hash"),
            name="ck_analysis_run_model_config_hash",
        ),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL "
            "AND error_json IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND error_json IS NULL) OR "
            "(status IN ('succeeded','partial','insufficient_evidence') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND current_stage IS NULL AND error_json IS NULL) OR "
            "(status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND current_stage IS NULL AND error_json IS NOT NULL) OR "
            "(status = 'cancelled' AND finished_at IS NOT NULL AND current_stage IS NULL)",
            name="ck_analysis_run_state_shape",
        ),
        UniqueConstraint("task_id", name="uq_analysis_run_task"),
        Index("ix_analysis_run_created", "created_at", "id"),
        Index("ix_analysis_run_status", "status", "updated_at", "id"),
        Index("ix_analysis_run_symbol", "symbol", "created_at", "id"),
        Index(
            "uq_analysis_run_active_retry",
            "parent_run_id",
            "requested_stage",
            unique=True,
            sqlite_where=text(
                "parent_run_id IS NOT NULL AND status IN ('queued','running')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_run.id", ondelete="RESTRICT"), nullable=False
    )
    parent_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("analysis_run.id", ondelete="RESTRICT"), nullable=True
    )
    requested_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    model_config_id: Mapped[str] = mapped_column(String(71), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    model_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_config_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    evidence_graph_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_graph_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    retry_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    retry_policy_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisStageRow(Base):
    __tablename__ = "analysis_stage"
    __table_args__ = (
        CheckConstraint(
            "(role = 'market' AND ordinal = -4) OR "
            "(role = 'fundamentals' AND ordinal = -3) OR "
            "(role = 'announcements' AND ordinal = -2) OR "
            "(role = 'news' AND ordinal = -1) OR "
            "(role = 'technical' AND ordinal = 0) OR "
            "(role = 'fundamental_news' AND ordinal = 1) OR "
            "(role = 'bull' AND ordinal = 2) OR "
            "(role = 'bear' AND ordinal = 3) OR "
            "(role = 'risk_decision' AND ordinal = 4)",
            name="ck_analysis_stage_role_ordinal",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','blocked',"
            "'reused','cancelled')",
            name="ck_analysis_stage_status",
        ),
        CheckConstraint(
            "(source_run_id IS NULL AND source_role IS NULL) OR "
            "(source_run_id IS NOT NULL AND source_role = role AND source_run_id <> run_id)",
            name="ck_analysis_stage_source",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_analysis_stage_attempt_count"),
        CheckConstraint(
            f"output_json IS NULL OR ({_json_object('output_json', 65_536)})",
            name="ck_analysis_stage_output_json",
        ),
        CheckConstraint(
            f"trace_json IS NULL OR ({_json_object('trace_json', 32_768)})",
            name="ck_analysis_stage_trace_json",
        ),
        CheckConstraint(
            "output_hash IS NULL OR " + _HASH_CHECK.format(name="output_hash"),
            name="ck_analysis_stage_output_hash",
        ),
        CheckConstraint(
            "trace_hash IS NULL OR " + _HASH_CHECK.format(name="trace_hash"),
            name="ck_analysis_stage_trace_hash",
        ),
        CheckConstraint(
            "failure_code IS NULL OR (length(failure_code) BETWEEN 1 AND 64 "
            "AND failure_code NOT GLOB '*[^a-z0-9_]*')",
            name="ck_analysis_stage_failure_code",
        ),
        CheckConstraint(
            "(status = 'pending' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND attempt_count >= 1) OR "
            "(status = 'succeeded' AND source_run_id IS NULL AND output_json IS NOT NULL "
            "AND output_hash IS NOT NULL AND trace_json IS NOT NULL AND trace_hash IS NOT NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'reused' AND source_run_id IS NOT NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND attempt_count = 0 "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND source_run_id IS NULL AND trace_json IS NULL "
            "AND trace_hash IS NULL "
            "AND failure_code IS NOT NULL AND retryable IS NOT NULL AND attempt_count >= 1 "
            "AND (((role IN ('market','fundamentals','announcements','news')) "
            "AND output_json IS NOT NULL AND output_hash IS NOT NULL) OR "
            "((role NOT IN ('market','fundamentals','announcements','news')) "
            "AND output_json IS NULL AND output_hash IS NULL)) "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'blocked' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NOT NULL AND retryable = 0 AND finished_at IS NOT NULL) OR "
            "(status = 'cancelled' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND finished_at IS NOT NULL)",
            name="ck_analysis_stage_state_shape",
        ),
        UniqueConstraint("run_id", "ordinal", name="uq_analysis_stage_ordinal"),
        ForeignKeyConstraint(
            ["source_run_id", "source_role"],
            ["analysis_stage.run_id", "analysis_stage.role"],
            ondelete="RESTRICT",
        ),
        Index("ix_analysis_stage_status", "run_id", "status", "ordinal"),
        Index("ix_analysis_stage_source", "source_run_id", "source_role"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_run.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(String(36))
    source_role: Mapped[str | None] = mapped_column(String(32))
    output_json: Mapped[str | None] = mapped_column(Text)
    output_hash: Mapped[str | None] = mapped_column(String(71))
    trace_json: Mapped[str | None] = mapped_column(Text)
    trace_hash: Mapped[str | None] = mapped_column(String(71))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisAttemptRow(Base):
    __tablename__ = "analysis_attempt"
    __table_args__ = (
        CheckConstraint("attempt_no BETWEEN 1 AND 6", name="ck_analysis_attempt_no"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','interrupted','cancelled')",
            name="ck_analysis_attempt_status",
        ),
        CheckConstraint(
            "backoff_seconds IS NULL OR backoff_seconds >= 0",
            name="ck_analysis_attempt_backoff",
        ),
        CheckConstraint(
            "request_hash IS NULL OR " + _HASH_CHECK.format(name="request_hash"),
            name="ck_analysis_attempt_request_hash",
        ),
        CheckConstraint(
            f"error_json IS NULL OR ({_json_object('error_json', 16_384)})",
            name="ck_analysis_attempt_error_json",
        ),
        CheckConstraint(
            "template_hash IS NULL OR " + _HASH_CHECK.format(name="template_hash"),
            name="ck_analysis_attempt_template_hash",
        ),
        CheckConstraint(
            f"usage_json IS NULL OR ({_json_object('usage_json', 16_384)})",
            name="ck_analysis_attempt_usage_json",
        ),
        CheckConstraint(
            "(status = 'running' AND error_json IS NULL AND retryable IS NULL "
            "AND backoff_seconds IS NULL AND usage_json IS NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND error_json IS NULL AND retryable IS NULL "
            "AND backoff_seconds IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND error_json IS NOT NULL AND retryable IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'interrupted' AND error_json IS NOT NULL AND retryable = 1 "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'cancelled' AND error_json IS NULL AND retryable IS NULL "
            "AND backoff_seconds IS NULL AND finished_at IS NOT NULL)",
            name="ck_analysis_attempt_state_shape",
        ),
        ForeignKeyConstraint(
            ["run_id", "role"],
            ["analysis_stage.run_id", "analysis_stage.role"],
            ondelete="CASCADE",
        ),
        Index("ix_analysis_attempt_status", "run_id", "status", "role"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(256))
    request_hash: Mapped[str | None] = mapped_column(String(71))
    error_json: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    backoff_seconds: Mapped[float | None] = mapped_column(Float)
    template_version: Mapped[str | None] = mapped_column(String(64))
    template_hash: Mapped[str | None] = mapped_column(String(71))
    usage_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalysisReportRow(Base):
    __tablename__ = "analysis_report"
    __table_args__ = (
        CheckConstraint(
            _HASH_CHECK.format(name="report_id"), name="ck_analysis_report_id"
        ),
        CheckConstraint(
            _HASH_CHECK.format(name="report_hash"), name="ck_analysis_report_hash"
        ),
        CheckConstraint(
            _json_object("report_json", 1_048_576),
            name="ck_analysis_report_json",
        ),
        Index("ix_analysis_report_id", "report_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("analysis_run.id", ondelete="CASCADE"), primary_key=True
    )
    report_id: Mapped[str] = mapped_column(String(71), nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
