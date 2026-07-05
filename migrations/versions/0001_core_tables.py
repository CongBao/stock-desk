"""Create core application metadata tables.

Revision ID: 0001_core_tables
Revises:
Create Date: 2026-07-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0001_core_tables"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "task_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("progress", sa.Float(), server_default="0", nullable=False),
        sa.Column("payload_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_run_status_created_at",
        "task_run",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_run_status_created_at", table_name="task_run")
    op.drop_table("task_run")
    op.drop_table("app_setting")
