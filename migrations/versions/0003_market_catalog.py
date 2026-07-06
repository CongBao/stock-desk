"""Create immutable market data catalog and update scheduling tables.

Revision ID: 0003_market_catalog
Revises: 0002_task_observability
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003_market_catalog"
down_revision: str | None = "0002_task_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_TABLES = (
    "market_dataset",
    "market_dataset_partition",
    "market_routing_manifest",
    "market_update_item",
    "market_update_occurrence",
)
_UPDATE_ITEM_OWNER_TRIGGER = "trg_market_update_item_owner_running"
_IMMUTABLE_INSERT_CONFLICTS = {
    "market_dataset": "dataset_version = NEW.dataset_version",
    "market_dataset_partition": """
        (dataset_version = NEW.dataset_version
         AND partition_manifest_id = NEW.partition_manifest_id)
        OR (dataset_version = NEW.dataset_version
            AND partition_year = NEW.partition_year)
        OR relative_path = NEW.relative_path
    """,
    "market_routing_manifest": ("manifest_record_id = NEW.manifest_record_id"),
    "market_update_item": """
        (task_id = NEW.task_id AND ordinal = NEW.ordinal)
        OR (task_id = NEW.task_id AND symbol = NEW.symbol)
    """,
    "market_update_occurrence": """
        (schedule_id = NEW.schedule_id AND local_date = NEW.local_date)
        OR task_id = NEW.task_id
    """,
}


def _create_immutable_triggers(table: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER trg_{table}_immutable_insert
            BEFORE INSERT ON {table}
            WHEN EXISTS (
                SELECT 1
                FROM {table}
                WHERE {_IMMUTABLE_INSERT_CONFLICTS[table]}
            )
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER trg_{table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} rows are immutable');
                END
                """
            )
        )


def upgrade() -> None:
    op.create_table(
        "market_dataset",
        sa.Column("dataset_version", sa.String(length=71), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=9), nullable=False),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("adjustment", sa.String(length=8), nullable=False),
        sa.Column("query_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("query_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "row_count > 0",
            name="ck_market_dataset_row_count_positive",
        ),
        sa.PrimaryKeyConstraint("dataset_version"),
        sa.UniqueConstraint(
            "dataset_version",
            "symbol",
            name="uq_market_dataset_version_symbol",
        ),
        sqlite_with_rowid=False,
    )
    op.create_index(
        "ix_market_dataset_exact_query",
        "market_dataset",
        ["symbol", "period", "adjustment", "query_start", "query_end"],
        unique=False,
    )

    op.create_table(
        "market_dataset_partition",
        sa.Column("dataset_version", sa.String(length=71), nullable=False),
        sa.Column("partition_manifest_id", sa.String(length=71), nullable=False),
        sa.Column("partition_year", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("physical_sha256", sa.String(length=71), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name="ck_market_dataset_partition_byte_size_positive",
        ),
        sa.CheckConstraint(
            "row_count > 0",
            name="ck_market_dataset_partition_row_count_positive",
        ),
        sa.CheckConstraint(
            "partition_year BETWEEN 1900 AND 9999",
            name="ck_market_dataset_partition_year",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version"],
            ["market_dataset.dataset_version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("dataset_version", "partition_manifest_id"),
        sa.UniqueConstraint(
            "dataset_version",
            "partition_year",
            name="uq_market_dataset_partition_dataset_year",
        ),
        sa.UniqueConstraint(
            "relative_path",
            name="uq_market_dataset_partition_relative_path",
        ),
        sqlite_with_rowid=False,
    )

    op.create_table(
        "market_routing_manifest",
        sa.Column("manifest_record_id", sa.String(length=71), nullable=False),
        sa.Column("dataset_version", sa.String(length=71), nullable=False),
        sa.Column("symbol", sa.String(length=9), nullable=False),
        sa.Column("route_version", sa.String(length=71), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version", "symbol"],
            ["market_dataset.dataset_version", "market_dataset.symbol"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("manifest_record_id"),
        sa.UniqueConstraint(
            "manifest_record_id",
            "dataset_version",
            "symbol",
            name="uq_market_routing_manifest_provenance",
        ),
        sqlite_with_rowid=False,
    )
    op.create_index(
        "ix_market_routing_manifest_dataset_fetched_at",
        "market_routing_manifest",
        ["dataset_version", "fetched_at"],
        unique=False,
    )
    op.create_index(
        "ix_market_routing_manifest_route_version",
        "market_routing_manifest",
        ["route_version"],
        unique=False,
    )

    op.create_table(
        "market_update_schedule",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default="Asia/Shanghai",
            nullable=False,
        ),
        sa.Column("local_time", sa.Time(), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("last_enqueued_local_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "timezone = 'Asia/Shanghai'",
            name="ck_market_update_schedule_timezone",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_market_update_schedule_due",
        "market_update_schedule",
        ["enabled", "local_time", "last_enqueued_local_date"],
        unique=False,
    )

    op.create_table(
        "market_update_item",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=9), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("manifest_record_id", sa.String(length=71), nullable=True),
        sa.Column("dataset_version", sa.String(length=71), nullable=True),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_market_update_item_ordinal",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'cancelled')",
            name="ck_market_update_item_status",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version"],
            ["market_dataset.dataset_version"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_record_id", "dataset_version", "symbol"],
            [
                "market_routing_manifest.manifest_record_id",
                "market_routing_manifest.dataset_version",
                "market_routing_manifest.symbol",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task_run.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id", "ordinal"),
        sa.UniqueConstraint(
            "task_id",
            "symbol",
            name="uq_market_update_item_task_symbol",
        ),
        sqlite_with_rowid=False,
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_UPDATE_ITEM_OWNER_TRIGGER}
            BEFORE INSERT ON market_update_item
            WHEN NOT EXISTS (
                SELECT 1
                FROM task_run
                WHERE id = NEW.task_id
                  AND kind = 'market.update'
                  AND status = 'running'
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'market_update_item requires running market update task'
                );
            END
            """
        )
    )
    op.create_table(
        "market_update_occurrence",
        sa.Column("schedule_id", sa.String(length=36), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["market_update_schedule.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task_run.id"],
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("schedule_id", "local_date"),
        sa.UniqueConstraint(
            "task_id",
            name="uq_market_update_occurrence_task_id",
        ),
        sqlite_with_rowid=False,
    )

    for table in _IMMUTABLE_TABLES:
        _create_immutable_triggers(table)


def downgrade() -> None:
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_UPDATE_ITEM_OWNER_TRIGGER}"))
    for table in reversed(_IMMUTABLE_TABLES):
        for operation in ("delete", "update", "insert"):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{operation}")
            )

    op.drop_table("market_update_occurrence")
    op.drop_table("market_update_item")
    op.drop_index(
        "ix_market_update_schedule_due",
        table_name="market_update_schedule",
    )
    op.drop_table("market_update_schedule")
    op.drop_index(
        "ix_market_routing_manifest_route_version",
        table_name="market_routing_manifest",
    )
    op.drop_index(
        "ix_market_routing_manifest_dataset_fetched_at",
        table_name="market_routing_manifest",
    )
    op.drop_table("market_routing_manifest")
    op.drop_table("market_dataset_partition")
    op.drop_index(
        "ix_market_dataset_exact_query",
        table_name="market_dataset",
    )
    op.drop_table("market_dataset")
