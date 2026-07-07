"""Add immutable content-addressed analysis model configurations.

Revision ID: 0009_analysis_model_configs
Revises: 0008_analysis_runs
Create Date: 2026-07-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_analysis_model_configs"
down_revision: str | None = "0008_analysis_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISPLAY_NAME_CHECK = " AND ".join(
    f"instr(display_name, char({codepoint})) = 0" for codepoint in (*range(32), 127)
)
_TIMESTAMP_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] "
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
    "[0-9][0-9][0-9][0-9][0-9][0-9]"
)


def _timestamp_check(column: str) -> str:
    year = f"CAST(substr({column}, 1, 4) AS INTEGER)"
    month = f"CAST(substr({column}, 6, 2) AS INTEGER)"
    day = f"CAST(substr({column}, 9, 2) AS INTEGER)"
    maximum_day = (
        f"CASE {month} WHEN 2 THEN 28 + CASE WHEN "
        f"(({year} % 4 = 0 AND {year} % 100 <> 0) OR {year} % 400 = 0) "
        f"THEN 1 ELSE 0 END WHEN 4 THEN 30 WHEN 6 THEN 30 "
        f"WHEN 9 THEN 30 WHEN 11 THEN 30 ELSE 31 END"
    )
    return (
        f"length({column}) = 26 AND {column} GLOB '{_TIMESTAMP_GLOB}' "
        f"AND {year} BETWEEN 1 AND 9999 "
        f"AND {month} BETWEEN 1 AND 12 "
        f"AND {day} BETWEEN 1 AND ({maximum_day}) "
        f"AND CAST(substr({column}, 12, 2) AS INTEGER) BETWEEN 0 AND 23 "
        f"AND CAST(substr({column}, 15, 2) AS INTEGER) BETWEEN 0 AND 59 "
        f"AND CAST(substr({column}, 18, 2) AS INTEGER) BETWEEN 0 AND 59"
    )


def upgrade() -> None:
    op.create_table(
        "analysis_model_config",
        sa.Column("id", sa.String(71), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("public_config_json", sa.Text(), nullable=False),
        sa.Column("public_config_hash", sa.String(71), nullable=False),
        sa.Column("secret_reference_id", sa.String(128), nullable=True),
        sa.Column("supersedes_id", sa.String(71), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(id) = 71 AND substr(id, 1, 7) = 'sha256:' "
            "AND substr(id, 8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_analysis_model_config_id",
        ),
        sa.CheckConstraint(
            "id = public_config_hash",
            name="ck_analysis_model_config_hash_binding",
        ),
        sa.CheckConstraint(
            "length(public_config_hash) = 71 "
            "AND substr(public_config_hash, 1, 7) = 'sha256:' "
            "AND substr(public_config_hash, 8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_analysis_model_config_hash",
        ),
        sa.CheckConstraint(
            "json_valid(public_config_json) = 1 "
            "AND json_type(public_config_json) = 'object' "
            "AND length(CAST(public_config_json AS BLOB)) <= 16384",
            name="ck_analysis_model_config_json",
        ),
        sa.CheckConstraint(
            "coalesce(json_type(public_config_json, '$.schema_version'), '') "
            "= 'text' AND json_extract(public_config_json, '$.schema_version') = "
            "'analysis-model-public-v1' "
            "AND coalesce(json_type(public_config_json, '$.provider'), '') = 'text' "
            "AND coalesce(json_type(public_config_json, '$.model'), '') = 'text' "
            "AND coalesce(json_type(public_config_json, '$.base_url'), '') = 'text' "
            "AND length(json_extract(public_config_json, '$.base_url')) "
            "BETWEEN 1 AND 2048 "
            "AND coalesce(json_type(public_config_json, '$.temperature'), '') "
            "= 'real' AND json_extract(public_config_json, '$.temperature') "
            "BETWEEN 0.0 AND 2.0 "
            "AND coalesce(json_type(public_config_json, '$.timeout_seconds'), '') "
            "= 'real' AND json_extract(public_config_json, '$.timeout_seconds') "
            "BETWEEN 1.0 AND 300.0 "
            "AND coalesce(json_type(public_config_json, '$.max_output_tokens'), '') "
            "= 'integer' "
            "AND json_extract(public_config_json, '$.max_output_tokens') "
            "BETWEEN 1 AND 65536 "
            "AND coalesce(json_type(public_config_json, '$.api_key_configured'), '') "
            "IN ('true','false') "
            "AND coalesce(json_type(public_config_json, '$.secret_reference_id'), '') "
            "IN ('null','text') "
            "AND json_type(public_config_json, '$.api_key') IS NULL",
            name="ck_analysis_model_config_public_shape",
        ),
        sa.CheckConstraint(
            "provider IN ('deepseek','openai_compatible','ollama') "
            "AND provider = json_extract(public_config_json, '$.provider')",
            name="ck_analysis_model_config_provider",
        ),
        sa.CheckConstraint(
            "length(model) BETWEEN 1 AND 256 "
            "AND model = trim(model) "
            "AND model = json_extract(public_config_json, '$.model')",
            name="ck_analysis_model_config_model",
        ),
        sa.CheckConstraint(
            "(secret_reference_id IS NULL "
            "AND json_type(public_config_json, '$.secret_reference_id') = 'null' "
            "AND json_extract(public_config_json, '$.api_key_configured') = 0) "
            "OR (secret_reference_id IS NOT NULL "
            "AND secret_reference_id = "
            "json_extract(public_config_json, '$.secret_reference_id') "
            "AND json_extract(public_config_json, '$.api_key_configured') = 1)",
            name="ck_analysis_model_config_secret_binding",
        ),
        sa.CheckConstraint(
            "secret_reference_id IS NULL "
            "OR secret_reference_id = 'analysis_model_api_key' "
            "OR (length(secret_reference_id) = 55 "
            "AND substr(secret_reference_id, 1, 23) = 'analysis_model_api_key_' "
            "AND substr(secret_reference_id, 24) NOT GLOB '*[^0-9a-f]*')",
            name="ck_analysis_model_config_secret_reference",
        ),
        sa.CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name="ck_analysis_model_config_supersedes",
        ),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 128 "
            "AND display_name = trim(display_name) "
            f"AND {_DISPLAY_NAME_CHECK}",
            name="ck_analysis_model_config_display_name",
        ),
        sa.CheckConstraint(
            "status IN ('unverified','verified','failed','disabled')",
            name="ck_analysis_model_config_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 "
            "AND error_code NOT GLOB '*[^a-z0-9_]*')",
            name="ck_analysis_model_config_error_code",
        ),
        sa.CheckConstraint(
            "typeof(revision) = 'integer' AND revision >= 0",
            name="ck_analysis_model_config_revision",
        ),
        sa.CheckConstraint(
            f"({_timestamp_check('created_at')}) AND "
            f"({_timestamp_check('updated_at')}) AND "
            f"(verified_at IS NULL OR ({_timestamp_check('verified_at')})) AND "
            "(last_tested_at IS NULL OR "
            f"({_timestamp_check('last_tested_at')}))",
            name="ck_analysis_model_config_timestamp_format",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at "
            "AND (verified_at IS NULL OR "
            "(verified_at >= created_at AND verified_at <= updated_at)) "
            "AND (last_tested_at IS NULL OR "
            "(last_tested_at >= created_at AND last_tested_at <= updated_at))",
            name="ck_analysis_model_config_timestamp_order",
        ),
        sa.CheckConstraint(
            "(status = 'unverified' AND verified_at IS NULL "
            "AND last_tested_at IS NULL AND error_code IS NULL) OR "
            "(status = 'verified' AND verified_at IS NOT NULL "
            "AND last_tested_at IS NOT NULL AND verified_at = last_tested_at "
            "AND error_code IS NULL) OR "
            "(status = 'failed' AND verified_at IS NULL "
            "AND last_tested_at IS NOT NULL AND error_code IS NOT NULL) OR "
            "(status = 'disabled' AND error_code IS NULL "
            "AND (verified_at IS NULL OR (last_tested_at IS NOT NULL "
            "AND verified_at = last_tested_at)))",
            name="ck_analysis_model_config_state_shape",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["analysis_model_config.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supersedes_id", name="uq_analysis_model_config_supersedes"
        ),
    )
    op.create_index(
        "ix_analysis_model_config_list",
        "analysis_model_config",
        ["display_name", "id"],
    )
    op.create_index(
        "ix_analysis_model_config_status",
        "analysis_model_config",
        ["status", "updated_at", "id"],
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_analysis_model_config_immutable_update "
            "BEFORE UPDATE OF id, provider, model, public_config_json, "
            "public_config_hash, secret_reference_id, supersedes_id, created_at "
            "ON analysis_model_config FOR EACH ROW WHEN "
            "NEW.id IS NOT OLD.id OR NEW.provider IS NOT OLD.provider OR "
            "NEW.model IS NOT OLD.model OR "
            "NEW.public_config_json IS NOT OLD.public_config_json OR "
            "NEW.public_config_hash IS NOT OLD.public_config_hash OR "
            "NEW.secret_reference_id IS NOT OLD.secret_reference_id OR "
            "NEW.supersedes_id IS NOT OLD.supersedes_id OR "
            "NEW.created_at IS NOT OLD.created_at "
            "BEGIN SELECT RAISE(ABORT, 'analysis model config is immutable'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_analysis_model_config_initial_revision "
            "BEFORE INSERT ON analysis_model_config FOR EACH ROW "
            "WHEN NEW.revision <> 0 "
            "BEGIN SELECT RAISE(ABORT, 'model config revision must start at zero'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_analysis_model_config_disabled_terminal "
            "BEFORE UPDATE ON analysis_model_config FOR EACH ROW "
            "WHEN OLD.status = 'disabled' "
            "BEGIN SELECT RAISE(ABORT, 'disabled model config is terminal'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_analysis_model_config_mutation_guard "
            "BEFORE UPDATE ON analysis_model_config FOR EACH ROW WHEN NOT ("
            "NEW.id IS OLD.id AND NEW.display_name IS OLD.display_name AND "
            "NEW.provider IS OLD.provider AND NEW.model IS OLD.model AND "
            "NEW.public_config_json IS OLD.public_config_json AND "
            "NEW.public_config_hash IS OLD.public_config_hash AND "
            "NEW.secret_reference_id IS OLD.secret_reference_id AND "
            "NEW.supersedes_id IS OLD.supersedes_id AND "
            "NEW.status IS OLD.status AND NEW.revision IS OLD.revision AND "
            "NEW.verified_at IS OLD.verified_at AND "
            "NEW.last_tested_at IS OLD.last_tested_at AND "
            "NEW.error_code IS OLD.error_code AND "
            "NEW.created_at IS OLD.created_at AND "
            "NEW.updated_at IS OLD.updated_at) AND ("
            "NEW.revision <> OLD.revision + 1 OR "
            "NEW.updated_at <= OLD.updated_at OR NEW.updated_at < NEW.created_at OR "
            "(NEW.status = 'unverified' AND OLD.status <> 'unverified') OR "
            "(NEW.display_name IS OLD.display_name AND NEW.status IS OLD.status AND "
            "NEW.verified_at IS OLD.verified_at AND "
            "NEW.last_tested_at IS OLD.last_tested_at AND "
            "NEW.error_code IS OLD.error_code)) "
            "BEGIN SELECT RAISE(ABORT, 'model config mutation audit is invalid'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_analysis_model_config_no_replace "
            "BEFORE INSERT ON analysis_model_config FOR EACH ROW WHEN EXISTS "
            "(SELECT 1 FROM analysis_model_config WHERE id = NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'analysis model config already exists'); END"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER trg_analysis_model_config_no_delete "
            "BEFORE DELETE ON analysis_model_config FOR EACH ROW "
            "BEGIN SELECT RAISE(ABORT, 'analysis model config cannot be deleted'); END"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_analysis_model_config_no_delete"))
    op.execute(sa.text("DROP TRIGGER trg_analysis_model_config_no_replace"))
    op.execute(sa.text("DROP TRIGGER trg_analysis_model_config_mutation_guard"))
    op.execute(sa.text("DROP TRIGGER trg_analysis_model_config_disabled_terminal"))
    op.execute(sa.text("DROP TRIGGER trg_analysis_model_config_initial_revision"))
    op.execute(sa.text("DROP TRIGGER trg_analysis_model_config_immutable_update"))
    op.drop_index("ix_analysis_model_config_status", table_name="analysis_model_config")
    op.drop_index("ix_analysis_model_config_list", table_name="analysis_model_config")
    op.drop_table("analysis_model_config")
