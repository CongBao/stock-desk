"""Persist task Worker process heartbeats.

Revision ID: 0011_worker_heartbeat
Revises: 0010_parent_active_retry
Create Date: 2026-07-09
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0011_worker_heartbeat"
down_revision: str | None = "0010_parent_active_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_worker_heartbeat",
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_task_worker_heartbeat_at",
        "task_worker_heartbeat",
        ["heartbeat_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_worker_heartbeat_at",
        table_name="task_worker_heartbeat",
    )
    op.drop_table("task_worker_heartbeat")
