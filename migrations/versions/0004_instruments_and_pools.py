"""Create immutable instrument catalog foundations for pools.

Revision ID: 0004_instruments_and_pools
Revises: 0003_market_catalog
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004_instruments_and_pools"
down_revision: str | None = "0003_market_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_CONFLICTS = {
    "instrument_dataset": "dataset_version = NEW.dataset_version",
    "instrument_dataset_item": """
        (dataset_version = NEW.dataset_version AND symbol = NEW.symbol)
        OR (dataset_version = NEW.dataset_version AND ordinal = NEW.ordinal)
    """,
    "instrument_routing_manifest": """
        manifest_record_id = NEW.manifest_record_id
        OR (manifest_record_id = NEW.manifest_record_id
            AND dataset_version = NEW.dataset_version)
    """,
    "preset_pool_snapshot": """
        snapshot_id = NEW.snapshot_id
        OR (snapshot_id = NEW.snapshot_id
            AND instrument_dataset_version = NEW.instrument_dataset_version)
    """,
    "preset_pool_member": """
        (snapshot_id = NEW.snapshot_id AND ordinal = NEW.ordinal)
        OR (snapshot_id = NEW.snapshot_id AND symbol = NEW.symbol)
    """,
}


def _create_immutable_triggers(table: str) -> None:
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER trg_{table}_immutable_insert
            BEFORE INSERT ON {table}
            WHEN EXISTS (
                SELECT 1 FROM {table}
                WHERE {_IMMUTABLE_CONFLICTS[table]}
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
        "instrument_dataset",
        sa.Column("dataset_version", sa.String(length=71), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "row_count BETWEEN 1 AND 50000",
            name="ck_instrument_dataset_row_count_bounded",
        ),
        sa.PrimaryKeyConstraint("dataset_version"),
        sqlite_with_rowid=False,
    )
    op.create_table(
        "instrument_dataset_item",
        sa.Column("dataset_version", sa.String(length=71), nullable=False),
        sa.Column("symbol", sa.String(length=9), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("exchange", sa.String(length=2), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("instrument_kind", sa.String(length=16), nullable=False),
        sa.Column("listing_status", sa.String(length=16), nullable=False),
        sa.Column("listed_on", sa.Date(), nullable=True),
        sa.Column("delisted_on", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 49999",
            name="ck_instrument_dataset_item_ordinal",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 255",
            name="ck_instrument_dataset_item_name_length",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version"],
            ["instrument_dataset.dataset_version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("dataset_version", "symbol"),
        sa.UniqueConstraint(
            "dataset_version",
            "ordinal",
            name="uq_instrument_dataset_item_ordinal",
        ),
        sqlite_with_rowid=False,
    )
    op.create_table(
        "instrument_routing_manifest",
        sa.Column("manifest_record_id", sa.String(length=71), nullable=False),
        sa.Column("dataset_version", sa.String(length=71), nullable=False),
        sa.Column("route_version", sa.String(length=71), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version"],
            ["instrument_dataset.dataset_version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("manifest_record_id"),
        sa.UniqueConstraint(
            "manifest_record_id",
            "dataset_version",
            name="uq_instrument_routing_manifest_dataset",
        ),
        sqlite_with_rowid=False,
    )
    op.create_index(
        "ix_instrument_routing_manifest_current",
        "instrument_routing_manifest",
        ["data_cutoff", "fetched_at", "manifest_record_id"],
        unique=False,
    )
    op.create_table(
        "preset_pool_snapshot",
        sa.Column("snapshot_id", sa.String(length=71), nullable=False),
        sa.Column("pool_id", sa.String(length=71), nullable=False),
        sa.Column("preset_key", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "composition_dataset_version",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column(
            "composition_route_version",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column(
            "instrument_manifest_record_id",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column(
            "instrument_dataset_version",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "pool_id = 'preset:' || preset_key",
            name="ck_preset_pool_snapshot_logical_id",
        ),
        sa.CheckConstraint(
            "category IN ('all_a', 'index', 'industry')",
            name="ck_preset_pool_snapshot_category",
        ),
        sa.CheckConstraint(
            "complete = 1",
            name="ck_preset_pool_snapshot_complete",
        ),
        sa.CheckConstraint(
            "member_count BETWEEN 1 AND 10000",
            name="ck_preset_pool_snapshot_member_count",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_manifest_record_id", "instrument_dataset_version"],
            [
                "instrument_routing_manifest.manifest_record_id",
                "instrument_routing_manifest.dataset_version",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "instrument_dataset_version",
            name="uq_preset_pool_snapshot_dataset",
        ),
        sqlite_with_rowid=False,
    )
    op.create_index(
        "ix_preset_pool_snapshot_latest",
        "preset_pool_snapshot",
        ["preset_key", "data_cutoff", "fetched_at", "snapshot_id"],
        unique=False,
    )
    op.create_table(
        "preset_pool_member",
        sa.Column("snapshot_id", sa.String(length=71), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "instrument_dataset_version",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=9), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 9999",
            name="ck_preset_pool_member_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "instrument_dataset_version"],
            [
                "preset_pool_snapshot.snapshot_id",
                "preset_pool_snapshot.instrument_dataset_version",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_dataset_version", "symbol"],
            [
                "instrument_dataset_item.dataset_version",
                "instrument_dataset_item.symbol",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "ordinal"),
        sa.UniqueConstraint(
            "snapshot_id",
            "symbol",
            name="uq_preset_pool_member_symbol",
        ),
        sqlite_with_rowid=False,
    )
    op.create_table(
        "custom_pool",
        sa.Column("pool_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "instrument_manifest_record_id",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column(
            "instrument_dataset_version",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("member_digest", sa.String(length=71), nullable=False),
        sa.Column("state_digest", sa.String(length=71), nullable=False),
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
            "length(name) BETWEEN 1 AND 64 AND trim(name) = name",
            name="ck_custom_pool_name",
        ),
        sa.CheckConstraint("revision > 0", name="ck_custom_pool_revision"),
        sa.CheckConstraint(
            "member_count BETWEEN 1 AND 5000",
            name="ck_custom_pool_member_count",
        ),
        sa.CheckConstraint(
            "length(member_digest) = 71 AND substr(member_digest, 1, 7) = 'sha256:'",
            name="ck_custom_pool_member_digest",
        ),
        sa.CheckConstraint(
            "length(state_digest) = 71 AND substr(state_digest, 1, 7) = 'sha256:'",
            name="ck_custom_pool_state_digest",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_manifest_record_id", "instrument_dataset_version"],
            [
                "instrument_routing_manifest.manifest_record_id",
                "instrument_routing_manifest.dataset_version",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("pool_id"),
        sa.UniqueConstraint(
            "pool_id",
            "revision",
            "instrument_dataset_version",
            name="uq_custom_pool_revision_dataset",
        ),
    )
    op.create_table(
        "custom_pool_member",
        sa.Column("pool_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("member_revision", sa.Integer(), nullable=False),
        sa.Column(
            "instrument_dataset_version",
            sa.String(length=71),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=9), nullable=False),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 4999",
            name="ck_custom_pool_member_ordinal",
        ),
        sa.CheckConstraint(
            "member_revision > 0",
            name="ck_custom_pool_member_revision",
        ),
        sa.ForeignKeyConstraint(
            ["pool_id", "member_revision", "instrument_dataset_version"],
            [
                "custom_pool.pool_id",
                "custom_pool.revision",
                "custom_pool.instrument_dataset_version",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_dataset_version", "symbol"],
            [
                "instrument_dataset_item.dataset_version",
                "instrument_dataset_item.symbol",
            ],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("pool_id", "ordinal"),
        sa.UniqueConstraint(
            "pool_id",
            "symbol",
            name="uq_custom_pool_member_symbol",
        ),
    )
    for table in _IMMUTABLE_CONFLICTS:
        _create_immutable_triggers(table)


def downgrade() -> None:
    for table in reversed(tuple(_IMMUTABLE_CONFLICTS)):
        for operation in ("delete", "update", "insert"):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{operation}")
            )
    op.drop_table("custom_pool_member")
    op.drop_table("custom_pool")
    op.drop_table("preset_pool_member")
    op.drop_index(
        "ix_preset_pool_snapshot_latest",
        table_name="preset_pool_snapshot",
    )
    op.drop_table("preset_pool_snapshot")
    op.drop_index(
        "ix_instrument_routing_manifest_current",
        table_name="instrument_routing_manifest",
    )
    op.drop_table("instrument_routing_manifest")
    op.drop_table("instrument_dataset_item")
    op.drop_table("instrument_dataset")
