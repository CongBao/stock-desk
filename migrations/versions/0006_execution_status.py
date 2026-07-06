"""Create immutable execution-status evidence catalog.

Revision ID: 0006_execution_status
Revises: 0005_formula_catalog
Create Date: 2026-07-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006_execution_status"
down_revision: str | None = "0005_formula_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = (
    "execution_status_dataset",
    "execution_status_routing_manifest",
)


def _immutable(table: str, identity: str) -> None:
    op.execute(
        sa.text(f"""
        CREATE TRIGGER trg_{table}_immutable_insert
        BEFORE INSERT ON {table}
        WHEN EXISTS (SELECT 1 FROM {table} WHERE {identity})
        BEGIN
            SELECT RAISE(ABORT, '{table} rows are immutable');
        END
        """)
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(f"""
            CREATE TRIGGER trg_{table}_immutable_{operation.lower()}
            BEFORE {operation} ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """)
        )


def upgrade() -> None:
    op.create_table(
        "execution_status_dataset",
        sa.Column("dataset_version", sa.String(71), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("exchange", sa.String(2), nullable=False),
        sa.Column("period", sa.String(8), nullable=False),
        sa.Column("query_start", sa.Date(), nullable=False),
        sa.Column("query_end", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "row_count > 0",
            name="ck_execution_status_dataset_row_count_positive",
        ),
        sa.CheckConstraint(
            "period IN ('1d', '1w', '60m')",
            name="ck_execution_status_dataset_period",
        ),
        sa.PrimaryKeyConstraint("dataset_version"),
        sqlite_with_rowid=False,
    )
    op.create_index(
        "ix_execution_status_dataset_exact_query",
        "execution_status_dataset",
        ["symbol", "exchange", "period", "query_start", "query_end"],
    )
    op.create_table(
        "execution_status_routing_manifest",
        sa.Column("manifest_record_id", sa.String(71), nullable=False),
        sa.Column("dataset_version", sa.String(71), nullable=False),
        sa.Column("route_version", sa.String(71), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version"],
            ["execution_status_dataset.dataset_version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("manifest_record_id"),
        sqlite_with_rowid=False,
    )
    op.create_index(
        "ix_execution_status_manifest_latest",
        "execution_status_routing_manifest",
        ["dataset_version", "fetched_at"],
    )
    _immutable("execution_status_dataset", "dataset_version = NEW.dataset_version")
    _immutable(
        "execution_status_routing_manifest",
        "manifest_record_id = NEW.manifest_record_id",
    )


def downgrade() -> None:
    for table in reversed(_TABLES):
        for operation in ("delete", "update", "insert"):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{operation}")
            )
    op.drop_index(
        "ix_execution_status_manifest_latest",
        table_name="execution_status_routing_manifest",
    )
    op.drop_table("execution_status_routing_manifest")
    op.drop_index(
        "ix_execution_status_dataset_exact_query",
        table_name="execution_status_dataset",
    )
    op.drop_table("execution_status_dataset")
