"""Add durable task observability events.

Revision ID: 0002_task_observability
Revises: 0001_core_tables
Create Date: 2026-07-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002_task_observability"
down_revision: str | None = "0001_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("event_name", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("detail_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_event_task_id_occurred_at",
        "task_event",
        ["task_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_event_task_id_occurred_at", table_name="task_event")
    op.drop_table("task_event")
