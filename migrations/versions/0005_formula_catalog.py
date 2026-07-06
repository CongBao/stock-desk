"""Create formula drafts and immutable published versions.

Revision ID: 0005_formula_catalog
Revises: 0004_instruments_and_pools
Create Date: 2026-07-06
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_formula_catalog"
down_revision: str | None = "0004_instruments_and_pools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "formula",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("formula_type", sa.String(16), nullable=False),
        sa.Column("placement", sa.String(16), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "formula_type IN ('indicator','trading')", name="ck_formula_type"
        ),
        sa.CheckConstraint(
            "placement IN ('main','subchart')", name="ck_formula_placement"
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 64 AND trim(name) = name",
            name="ck_formula_name",
        ),
        sa.CheckConstraint("latest_version >= 0", name="ck_formula_latest_version"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "formula_version",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("formula_id", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("formula_type", sa.String(16), nullable=False),
        sa.Column("placement", sa.String(16), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("parameter_schema_json", sa.JSON(), nullable=False),
        sa.Column("compatibility_version", sa.String(32), nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("checksum", sa.String(71), nullable=False),
        sa.Column("validation_result_json", sa.JSON(), nullable=False),
        sa.Column("copied_from_version_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_formula_version_positive"),
        sa.CheckConstraint(
            "formula_type IN ('indicator','trading')", name="ck_formula_version_type"
        ),
        sa.CheckConstraint(
            "placement IN ('main','subchart')", name="ck_formula_version_placement"
        ),
        sa.CheckConstraint(
            "length(CAST(source AS BLOB)) BETWEEN 1 AND 64000",
            name="ck_formula_version_source",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 64 AND trim(name) = name",
            name="ck_formula_version_name",
        ),
        sa.CheckConstraint(
            "length(checksum) = 71 AND checksum LIKE 'sha256:%'",
            name="ck_formula_version_checksum",
        ),
        sa.ForeignKeyConstraint(
            ["copied_from_version_id"],
            ["formula_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["formula_id"], ["formula.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("formula_id", "version", name="uq_formula_version_number"),
        sa.UniqueConstraint(
            "formula_id", "id", name="uq_formula_version_owner_identity"
        ),
    )
    op.create_index(
        "ix_formula_version_formula",
        "formula_version",
        ["formula_id", "version"],
        unique=False,
    )
    op.create_table(
        "formula_draft",
        sa.Column("formula_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_checksum", sa.String(71), nullable=False),
        sa.Column("parameter_schema_json", sa.JSON(), nullable=False),
        sa.Column("validation_result_json", sa.JSON(), nullable=False),
        sa.Column("executable_version_id", sa.String(36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(CAST(source AS BLOB)) BETWEEN 1 AND 64000",
            name="ck_formula_draft_source",
        ),
        sa.CheckConstraint(
            "length(source_checksum) = 71 AND source_checksum LIKE 'sha256:%'",
            name="ck_formula_draft_checksum",
        ),
        sa.CheckConstraint("revision > 0", name="ck_formula_draft_revision"),
        sa.ForeignKeyConstraint(["formula_id"], ["formula.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["formula_id", "executable_version_id"],
            ["formula_version.formula_id", "formula_version.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("formula_id"),
    )
    op.execute(
        sa.text("""
    CREATE TRIGGER trg_formula_version_immutable_insert
    BEFORE INSERT ON formula_version
    WHEN EXISTS (
        SELECT 1 FROM formula_version
        WHERE id = NEW.id
           OR (formula_id = NEW.formula_id AND version = NEW.version)
    )
    BEGIN
        SELECT RAISE(ABORT, 'formula_version rows are immutable');
    END
    """)
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            sa.text(f"""
        CREATE TRIGGER trg_formula_version_immutable_{operation.lower()}
        BEFORE {operation} ON formula_version
        BEGIN
            SELECT RAISE(ABORT, 'formula_version rows are immutable');
        END
        """)
        )
    op.execute(
        sa.text("""
    CREATE TRIGGER trg_formula_version_owner
    BEFORE INSERT ON formula_version
    WHEN NOT EXISTS (
        SELECT 1 FROM formula
        WHERE id = NEW.formula_id
          AND formula_type = NEW.formula_type
          AND placement = NEW.placement
          AND name = NEW.name
          AND latest_version = NEW.version
    )
    BEGIN
        SELECT RAISE(ABORT, 'formula version owner state is invalid');
    END
    """)
    )
    for operation in ("INSERT", "UPDATE"):
        op.execute(
            sa.text(f"""
        CREATE TRIGGER trg_formula_draft_executable_{operation.lower()}
        BEFORE {operation} ON formula_draft
        WHEN NEW.executable_version_id IS NOT NULL
         AND NOT EXISTS (
            SELECT 1 FROM formula_version
            WHERE id = NEW.executable_version_id
              AND formula_id = NEW.formula_id
         )
        BEGIN
            SELECT RAISE(ABORT, 'draft executable version owner is invalid');
        END
        """)
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_formula_draft_executable_update")
    op.execute("DROP TRIGGER IF EXISTS trg_formula_draft_executable_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_formula_version_owner")
    op.execute("DROP TRIGGER IF EXISTS trg_formula_version_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_formula_version_immutable_update")
    op.execute("DROP TRIGGER IF EXISTS trg_formula_version_immutable_insert")
    op.drop_table("formula_draft")
    op.drop_index("ix_formula_version_formula", table_name="formula_version")
    op.drop_table("formula_version")
    op.drop_table("formula")
