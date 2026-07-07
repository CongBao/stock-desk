"""Enforce one active analysis retry child per parent.

Revision ID: 0010_parent_active_retry
Revises: 0009_analysis_model_configs
Create Date: 2026-07-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0010_parent_active_retry"
down_revision: str | None = "0009_analysis_model_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_RETRY = "parent_run_id IS NOT NULL AND status IN ('queued','running')"


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    duplicate = connection.execute(
        sa.text(
            "SELECT parent_run_id FROM analysis_run "
            f"WHERE {_ACTIVE_RETRY} GROUP BY parent_run_id "
            "HAVING count(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError("multiple active analysis retries exist for one parent")
    op.drop_index("uq_analysis_run_active_retry", table_name="analysis_run")
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_analysis_run_active_retry ON analysis_run "
            f"(parent_run_id) WHERE {_ACTIVE_RETRY}"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    op.drop_index("uq_analysis_run_active_retry", table_name="analysis_run")
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_analysis_run_active_retry ON analysis_run "
            "(parent_run_id, requested_stage) WHERE "
            f"{_ACTIVE_RETRY}"
        )
    )
