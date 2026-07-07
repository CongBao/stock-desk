from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from stock_desk.storage.base import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BacktestRunRow(Base):
    __tablename__ = "backtest_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','partial_failed','failed','cancelled')",
            name="ck_backtest_run_status",
        ),
        CheckConstraint(
            "stage IN ('queued','executing','completed','failed','cancelled')",
            name="ck_backtest_run_stage",
        ),
        CheckConstraint(
            "total BETWEEN 1 AND 10000",
            name="ck_backtest_run_total",
        ),
        CheckConstraint(
            "failed_count >= 0 AND failed_count <= processed AND processed <= total",
            name="ck_backtest_run_counts",
        ),
        UniqueConstraint("task_id", name="uq_backtest_run_task"),
        Index("ix_backtest_run_created", "created_at", "id"),
        Index("ix_backtest_run_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("task_run.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[str] = mapped_column(String(71), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    processed: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    actual_warmup_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BacktestSymbolRow(Base):
    __tablename__ = "backtest_symbol"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 9999", name="ck_backtest_symbol_ordinal"
        ),
        CheckConstraint(
            "input_kind IN ('runnable','gap')", name="ck_backtest_symbol_input_kind"
        ),
        CheckConstraint(
            "status IN ('pending','succeeded','failed')",
            name="ck_backtest_symbol_status",
        ),
        UniqueConstraint("run_id", "symbol", name="uq_backtest_symbol_owner"),
        Index("ix_backtest_symbol_status", "run_id", "status", "ordinal"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_run.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(9), nullable=False)
    input_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reference_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_series_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    warmup_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class BacktestTradeRow(Base):
    __tablename__ = "backtest_trade"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_backtest_trade_ordinal"),
        ForeignKeyConstraint(
            ["run_id", "symbol"],
            ["backtest_symbol.run_id", "backtest_symbol.symbol"],
            ondelete="CASCADE",
        ),
        Index("ix_backtest_trade_page", "run_id", "realized", "ordinal"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(9), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    realized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BacktestOrderEventRow(Base):
    __tablename__ = "backtest_order_event"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_backtest_order_event_ordinal"),
        ForeignKeyConstraint(
            ["run_id", "symbol"],
            ["backtest_symbol.run_id", "backtest_symbol.symbol"],
            ondelete="CASCADE",
        ),
        Index("ix_backtest_order_event_page", "run_id", "symbol", "ordinal"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(9), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BacktestFailureRow(Base):
    __tablename__ = "backtest_failure"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_backtest_failure_ordinal"),
        ForeignKeyConstraint(
            ["run_id", "symbol"],
            ["backtest_symbol.run_id", "backtest_symbol.symbol"],
            ondelete="CASCADE",
        ),
        Index("ix_backtest_failure_page", "run_id", "ordinal"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(9), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BacktestLogRow(Base):
    __tablename__ = "backtest_log"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_backtest_log_ordinal"),
        CheckConstraint(
            "level IN ('info','warning','error')", name="ck_backtest_log_level"
        ),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_run.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(String(128), nullable=False)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BacktestAggregateMetricRow(Base):
    __tablename__ = "backtest_aggregate_metric"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_run.id", ondelete="CASCADE"), primary_key=True
    )
    metric_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class BacktestGroupMetricRow(Base):
    __tablename__ = "backtest_group_metric"
    __table_args__ = (
        CheckConstraint(
            "dimension IN ('symbol','entry_month','entry_year')",
            name="ck_backtest_group_dimension",
        ),
        Index("ix_backtest_group_page", "run_id", "dimension", "group_key"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("backtest_run.id", ondelete="CASCADE"), primary_key=True
    )
    dimension: Mapped[str] = mapped_column(String(32), primary_key=True)
    group_key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
