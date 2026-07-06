"""Add recoverable backtest task leases.

Revision ID: 0007_backtest_runs
Revises: 0006_execution_status
Create Date: 2026-07-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0007_backtest_runs"
down_revision: str | None = "0006_execution_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKTEST_TABLES = (
    "backtest_run",
    "backtest_symbol",
    "backtest_trade",
    "backtest_order_event",
    "backtest_failure",
    "backtest_log",
    "backtest_aggregate_metric",
    "backtest_group_metric",
)


def _create_terminal_immutability() -> None:
    terminal = "('succeeded','partial_failed','failed','cancelled')"
    for table in _BACKTEST_TABLES:
        operations = ("INSERT", "UPDATE", "DELETE")
        for operation in operations:
            owner = (
                (
                    "EXISTS (SELECT 1 FROM backtest_run "
                    f"WHERE status IN {terminal} "
                    "AND (id = NEW.id OR task_id = NEW.task_id))"
                )
                if table == "backtest_run" and operation == "INSERT"
                else f"OLD.status IN {terminal}"
                if table == "backtest_run"
                else (
                    "EXISTS (SELECT 1 FROM backtest_run "
                    f"WHERE id = {'NEW' if operation == 'INSERT' else 'OLD'}.run_id "
                    f"AND status IN {terminal})"
                )
            )
            op.execute(
                sa.text(f"""
                CREATE TRIGGER trg_{table}_terminal_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN {owner}
                BEGIN
                    SELECT RAISE(ABORT, 'completed backtest results are immutable');
                END
                """)
            )


def upgrade() -> None:
    op.add_column(
        "task_run", sa.Column("claim_token", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "task_run",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_run",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_run",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_task_run_backtest_lease",
        "task_run",
        ["kind", "status", "lease_expires_at", "created_at"],
    )
    op.create_table(
        "market_dataset_timestamp",
        sa.Column("dataset_version", sa.String(71), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_market_dataset_timestamp_ordinal"),
        sa.ForeignKeyConstraint(
            ["dataset_version"],
            ["market_dataset.dataset_version"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("dataset_version", "ordinal"),
        sa.UniqueConstraint(
            "dataset_version",
            "timestamp",
            name="uq_market_dataset_timestamp_value",
        ),
        sqlite_with_rowid=False,
    )
    op.create_index(
        "ix_market_dataset_timestamp_lookup",
        "market_dataset_timestamp",
        ["dataset_version", "timestamp"],
    )
    op.create_table(
        "market_dataset_timestamp_seal",
        sa.Column("dataset_version", sa.String(71), nullable=False),
        sa.Column("index_version", sa.String(32), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("timestamp_digest", sa.String(71), nullable=False),
        sa.CheckConstraint(
            "row_count > 0", name="ck_market_dataset_timestamp_seal_row_count"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version"],
            ["market_dataset.dataset_version"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("dataset_version"),
        sqlite_with_rowid=False,
    )
    _create_market_timestamp_immutability()
    op.create_table(
        "backtest_run",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("snapshot_id", sa.String(71), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("result_hash", sa.String(71), nullable=True),
        sa.Column("actual_warmup_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','partial_failed','failed','cancelled')",
            name="ck_backtest_run_status",
        ),
        sa.CheckConstraint(
            "stage IN ('queued','executing','completed','failed','cancelled')",
            name="ck_backtest_run_stage",
        ),
        sa.CheckConstraint("total BETWEEN 1 AND 10000", name="ck_backtest_run_total"),
        sa.CheckConstraint(
            "failed_count >= 0 AND failed_count <= processed AND processed <= total",
            name="ck_backtest_run_counts",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task_run.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_backtest_run_task"),
    )
    op.create_index("ix_backtest_run_created", "backtest_run", ["created_at", "id"])
    op.create_index("ix_backtest_run_status", "backtest_run", ["status", "updated_at"])
    op.create_table(
        "backtest_symbol",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("input_kind", sa.String(16), nullable=False),
        sa.Column("reference_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("signal_series_id", sa.String(71), nullable=True),
        sa.Column("warmup_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 9999", name="ck_backtest_symbol_ordinal"
        ),
        sa.CheckConstraint(
            "input_kind IN ('runnable','gap')", name="ck_backtest_symbol_input_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending','succeeded','failed')",
            name="ck_backtest_symbol_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "ordinal"),
        sa.UniqueConstraint("run_id", "symbol", name="uq_backtest_symbol_owner"),
    )
    op.create_index(
        "ix_backtest_symbol_status",
        "backtest_symbol",
        ["run_id", "status", "ordinal"],
    )
    _create_result_tables()
    _create_terminal_immutability()


def downgrade() -> None:
    for table in reversed(_BACKTEST_TABLES):
        for operation in ("delete", "update", "insert"):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_terminal_{operation}")
            )
    for table, indexes in (
        ("backtest_group_metric", ("ix_backtest_group_page",)),
        ("backtest_aggregate_metric", ()),
        ("backtest_log", ()),
        ("backtest_failure", ("ix_backtest_failure_page",)),
        ("backtest_order_event", ("ix_backtest_order_event_page",)),
        ("backtest_trade", ("ix_backtest_trade_page",)),
        ("backtest_symbol", ("ix_backtest_symbol_status",)),
        ("backtest_run", ("ix_backtest_run_status", "ix_backtest_run_created")),
    ):
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
    for operation in ("delete", "update", "insert"):
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS "
                f"trg_market_dataset_timestamp_immutable_{operation}"
            )
        )
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS "
                f"trg_market_dataset_timestamp_seal_immutable_{operation}"
            )
        )
    op.drop_table("market_dataset_timestamp_seal")
    op.drop_index(
        "ix_market_dataset_timestamp_lookup",
        table_name="market_dataset_timestamp",
    )
    op.drop_table("market_dataset_timestamp")
    op.drop_index("ix_task_run_backtest_lease", table_name="task_run")
    op.drop_column("task_run", "attempt_count")
    op.drop_column("task_run", "heartbeat_at")
    op.drop_column("task_run", "lease_expires_at")
    op.drop_column("task_run", "claim_token")


def _create_result_tables() -> None:
    symbol_fk = sa.ForeignKeyConstraint(
        ["run_id", "symbol"],
        ["backtest_symbol.run_id", "backtest_symbol.symbol"],
        ondelete="CASCADE",
    )
    op.create_table(
        "backtest_trade",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("realized", sa.Boolean(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_backtest_trade_ordinal"),
        symbol_fk,
        sa.PrimaryKeyConstraint("run_id", "symbol", "ordinal"),
    )
    op.create_index(
        "ix_backtest_trade_page", "backtest_trade", ["run_id", "realized", "ordinal"]
    )
    op.create_table(
        "backtest_order_event",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_backtest_order_event_ordinal"),
        sa.ForeignKeyConstraint(
            ["run_id", "symbol"],
            ["backtest_symbol.run_id", "backtest_symbol.symbol"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "symbol", "ordinal"),
    )
    op.create_index(
        "ix_backtest_order_event_page",
        "backtest_order_event",
        ["run_id", "symbol", "ordinal"],
    )
    op.create_table(
        "backtest_failure",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_backtest_failure_ordinal"),
        sa.ForeignKeyConstraint(
            ["run_id", "symbol"],
            ["backtest_symbol.run_id", "backtest_symbol.symbol"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "symbol", "ordinal"),
    )
    op.create_index(
        "ix_backtest_failure_page", "backtest_failure", ["run_id", "ordinal"]
    )
    op.create_table(
        "backtest_log",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("message", sa.String(128), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_backtest_log_ordinal"),
        sa.CheckConstraint(
            "level IN ('info','warning','error')", name="ck_backtest_log_level"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "ordinal"),
    )
    op.create_table(
        "backtest_aggregate_metric",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("metric_key", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "metric_key"),
    )
    op.create_table(
        "backtest_group_metric",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("dimension", sa.String(32), nullable=False),
        sa.Column("group_key", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "dimension IN ('symbol','entry_month','entry_year')",
            name="ck_backtest_group_dimension",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "dimension", "group_key"),
    )
    op.create_index(
        "ix_backtest_group_page",
        "backtest_group_metric",
        ["run_id", "dimension", "group_key"],
    )


def _create_market_timestamp_immutability() -> None:
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_market_dataset_timestamp_immutable_insert
        BEFORE INSERT ON market_dataset_timestamp
        WHEN EXISTS (
            SELECT 1 FROM market_dataset_timestamp
            WHERE dataset_version = NEW.dataset_version
              AND (ordinal = NEW.ordinal OR timestamp = NEW.timestamp)
        )
        BEGIN
            SELECT RAISE(ABORT, 'market dataset timestamps are immutable');
        END
        """)
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(f"""
            CREATE TRIGGER trg_market_dataset_timestamp_immutable_{operation.lower()}
            BEFORE {operation} ON market_dataset_timestamp
            BEGIN
                SELECT RAISE(ABORT, 'market dataset timestamps are immutable');
            END
            """)
        )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_market_dataset_timestamp_seal_immutable_insert
        BEFORE INSERT ON market_dataset_timestamp_seal
        WHEN EXISTS (
            SELECT 1 FROM market_dataset_timestamp_seal
            WHERE dataset_version = NEW.dataset_version
        )
        BEGIN
            SELECT RAISE(ABORT, 'market dataset timestamp seals are immutable');
        END
        """)
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(f"""
            CREATE TRIGGER trg_market_dataset_timestamp_seal_immutable_{operation.lower()}
            BEFORE {operation} ON market_dataset_timestamp_seal
            BEGIN
                SELECT RAISE(ABORT, 'market dataset timestamp seals are immutable');
            END
            """)
        )
