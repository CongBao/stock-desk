"""Persist canonical bar payloads in the timestamp index.

Revision ID: 0012_windows_market_payload
Revises: 0011_worker_heartbeat
Create Date: 2026-07-10
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0012_windows_market_payload"
down_revision: str | None = "0011_worker_heartbeat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "market_dataset_timestamp"
_TRIGGER_PREFIX = "trg_market_dataset_timestamp_immutable_"
_STATUSES = "('unknown','normal','suspended','limit_up','limit_down')"


def _drop_immutability() -> None:
    for operation in ("insert", "update", "delete"):
        op.execute(sa.text(f"DROP TRIGGER {_TRIGGER_PREFIX}{operation}"))


def _create_immutability() -> None:
    op.execute(
        sa.text(f"""
        CREATE TRIGGER {_TRIGGER_PREFIX}insert
        BEFORE INSERT ON {_TABLE}
        WHEN EXISTS (
            SELECT 1 FROM {_TABLE}
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
            CREATE TRIGGER {_TRIGGER_PREFIX}{operation.lower()}
            BEFORE {operation} ON {_TABLE}
            BEGIN
                SELECT RAISE(ABORT, 'market dataset timestamps are immutable');
            END
            """)
        )


def upgrade() -> None:
    _drop_immutability()
    with op.batch_alter_table(
        _TABLE,
        recreate="always",
        table_kwargs={"sqlite_with_rowid": False},
    ) as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("open", sa.Numeric(24, 8), nullable=True))
        batch_op.add_column(sa.Column("high", sa.Numeric(24, 8), nullable=True))
        batch_op.add_column(sa.Column("low", sa.Numeric(24, 8), nullable=True))
        batch_op.add_column(sa.Column("close", sa.Numeric(24, 8), nullable=True))
        batch_op.add_column(sa.Column("volume", sa.BigInteger(), nullable=True))
        batch_op.create_check_constraint(
            "ck_market_dataset_timestamp_payload_shape",
            "(status IS NULL AND open IS NULL AND high IS NULL AND low IS NULL "
            "AND close IS NULL AND volume IS NULL) OR "
            "(status IS NOT NULL AND open IS NOT NULL AND high IS NOT NULL "
            "AND low IS NOT NULL AND close IS NOT NULL AND volume IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_market_dataset_timestamp_status",
            f"status IS NULL OR status IN {_STATUSES}",
        )
        batch_op.create_check_constraint(
            "ck_market_dataset_timestamp_volume",
            "volume IS NULL OR volume >= 0",
        )
    _create_immutability()


def downgrade() -> None:
    payload_rows = (
        op.get_bind()
        .execute(sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE status IS NOT NULL"))
        .scalar_one()
    )
    if payload_rows:
        raise RuntimeError(
            "cannot downgrade while Windows market payload rows exist; "
            "restore the pre-upgrade database backup instead"
        )
    _drop_immutability()
    with op.batch_alter_table(
        _TABLE,
        recreate="always",
        table_kwargs={"sqlite_with_rowid": False},
    ) as batch_op:
        batch_op.drop_constraint("ck_market_dataset_timestamp_volume", type_="check")
        batch_op.drop_constraint("ck_market_dataset_timestamp_status", type_="check")
        batch_op.drop_constraint(
            "ck_market_dataset_timestamp_payload_shape", type_="check"
        )
        for column in ("volume", "close", "low", "high", "open", "status"):
            batch_op.drop_column(column)
    _create_immutability()
