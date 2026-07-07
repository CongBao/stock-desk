"""Add immutable analysis runs, stages, attempts, and reports.

Revision ID: 0008_analysis_runs
Revises: 0007_backtest_runs
Create Date: 2026-07-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_analysis_runs"
down_revision: str | None = "0007_backtest_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERMINAL_RUN = "('succeeded','partial','insufficient_evidence','failed','cancelled')"
_TERMINAL_STAGE = "('succeeded','failed','blocked','reused','cancelled')"
_TERMINAL_ATTEMPT = "('succeeded','failed','interrupted','cancelled')"


def _digest(column: str) -> str:
    return f"length({column}) = 71 AND substr({column}, 1, 7) = 'sha256:'"


def _json_object(column: str, maximum: int) -> str:
    return (
        f"json_valid({column}) = 1 AND json_type({column}) = 'object' "
        f"AND length(CAST({column} AS BLOB)) <= {maximum}"
    )


def upgrade() -> None:
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("parent_run_id", sa.String(36), nullable=True),
        sa.Column("requested_stage", sa.String(32), nullable=True),
        sa.Column("symbol", sa.String(9), nullable=False),
        sa.Column("model_config_id", sa.String(71), nullable=False),
        sa.Column("model_provider", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("model_config_json", sa.Text(), nullable=False),
        sa.Column("model_config_hash", sa.String(71), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_stage", sa.String(32), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("config_fingerprint", sa.String(71), nullable=False),
        sa.Column("snapshot_id", sa.String(71), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("snapshot_hash", sa.String(71), nullable=True),
        sa.Column("evidence_graph_json", sa.Text(), nullable=True),
        sa.Column("evidence_graph_hash", sa.String(71), nullable=True),
        sa.Column("retry_policy_json", sa.Text(), nullable=False),
        sa.Column("retry_policy_hash", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].SH' OR "
            "symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].SZ' OR "
            "symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9].BJ'",
            name="ck_analysis_run_symbol",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','partial',"
            "'insufficient_evidence','failed','cancelled')",
            name="ck_analysis_run_status",
        ),
        sa.CheckConstraint(
            "requested_stage IS NULL OR requested_stage IN "
            "('technical','fundamental_news','bull','bear','risk_decision')",
            name="ck_analysis_run_requested_stage",
        ),
        sa.CheckConstraint(
            "current_stage IS NULL OR current_stage IN "
            "('market','fundamentals','announcements','news','technical',"
            "'fundamental_news','bull','bear','risk_decision')",
            name="ck_analysis_run_current_stage",
        ),
        sa.CheckConstraint(
            "(snapshot_id IS NULL AND snapshot_json IS NULL AND snapshot_hash IS NULL "
            "AND evidence_graph_json IS NULL AND evidence_graph_hash IS NULL) OR "
            "(snapshot_id IS NOT NULL AND snapshot_json IS NOT NULL "
            "AND snapshot_hash IS NOT NULL AND evidence_graph_json IS NOT NULL "
            "AND evidence_graph_hash IS NOT NULL)",
            name="ck_analysis_run_input_binding",
        ),
        sa.CheckConstraint(
            f"snapshot_id IS NULL OR ({_digest('snapshot_id')})",
            name="ck_analysis_run_snapshot_id",
        ),
        sa.CheckConstraint(
            f"snapshot_hash IS NULL OR ({_digest('snapshot_hash')})",
            name="ck_analysis_run_snapshot_hash",
        ),
        sa.CheckConstraint(
            f"evidence_graph_hash IS NULL OR ({_digest('evidence_graph_hash')})",
            name="ck_analysis_run_evidence_hash",
        ),
        sa.CheckConstraint(
            _digest("retry_policy_hash"), name="ck_analysis_run_retry_hash"
        ),
        sa.CheckConstraint(
            f"snapshot_json IS NULL OR ({_json_object('snapshot_json', 2_097_152)})",
            name="ck_analysis_run_snapshot_json",
        ),
        sa.CheckConstraint(
            "evidence_graph_json IS NULL OR "
            f"({_json_object('evidence_graph_json', 4_194_304)})",
            name="ck_analysis_run_evidence_json",
        ),
        sa.CheckConstraint(
            _json_object("retry_policy_json", 16_384),
            name="ck_analysis_run_retry_json",
        ),
        sa.CheckConstraint(
            f"error_json IS NULL OR ({_json_object('error_json', 16_384)})",
            name="ck_analysis_run_error_json",
        ),
        sa.CheckConstraint(
            _digest("config_fingerprint"), name="ck_analysis_run_config_fingerprint"
        ),
        sa.CheckConstraint(
            _digest("model_config_id"), name="ck_analysis_run_model_config_id"
        ),
        sa.CheckConstraint(
            "length(model_provider) BETWEEN 1 AND 64",
            name="ck_analysis_run_model_provider",
        ),
        sa.CheckConstraint(
            "length(model_name) BETWEEN 1 AND 256",
            name="ck_analysis_run_model_name",
        ),
        sa.CheckConstraint(
            _json_object("model_config_json", 16_384),
            name="ck_analysis_run_model_config_json",
        ),
        sa.CheckConstraint(
            _digest("model_config_hash"), name="ck_analysis_run_model_config_hash"
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL "
            "AND error_json IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND error_json IS NULL) OR "
            "(status IN ('succeeded','partial','insufficient_evidence') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND current_stage IS NULL AND error_json IS NULL) OR "
            "(status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND current_stage IS NULL AND error_json IS NOT NULL) OR "
            "(status = 'cancelled' AND finished_at IS NOT NULL AND current_stage IS NULL)",
            name="ck_analysis_run_state_shape",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["task_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["analysis_run.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_analysis_run_task"),
    )
    op.create_index("ix_analysis_run_created", "analysis_run", ["created_at", "id"])
    op.create_index(
        "ix_analysis_run_status", "analysis_run", ["status", "updated_at", "id"]
    )
    op.create_index(
        "ix_analysis_run_symbol", "analysis_run", ["symbol", "created_at", "id"]
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_analysis_run_active_retry ON analysis_run "
            "(parent_run_id, requested_stage) WHERE parent_run_id IS NOT NULL "
            "AND status IN ('queued','running')"
        )
    )

    op.create_table(
        "analysis_stage",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_run_id", sa.String(36), nullable=True),
        sa.Column("source_role", sa.String(32), nullable=True),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("output_hash", sa.String(71), nullable=True),
        sa.Column("trace_json", sa.Text(), nullable=True),
        sa.Column("trace_hash", sa.String(71), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(role = 'market' AND ordinal = -4) OR "
            "(role = 'fundamentals' AND ordinal = -3) OR "
            "(role = 'announcements' AND ordinal = -2) OR "
            "(role = 'news' AND ordinal = -1) OR "
            "(role = 'technical' AND ordinal = 0) OR "
            "(role = 'fundamental_news' AND ordinal = 1) OR "
            "(role = 'bull' AND ordinal = 2) OR "
            "(role = 'bear' AND ordinal = 3) OR "
            "(role = 'risk_decision' AND ordinal = 4)",
            name="ck_analysis_stage_role_ordinal",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','blocked',"
            "'reused','cancelled')",
            name="ck_analysis_stage_status",
        ),
        sa.CheckConstraint(
            "(source_run_id IS NULL AND source_role IS NULL) OR "
            "(source_run_id IS NOT NULL AND source_role = role AND source_run_id <> run_id)",
            name="ck_analysis_stage_source",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_analysis_stage_attempt_count"
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR (length(failure_code) BETWEEN 1 AND 64 "
            "AND failure_code NOT GLOB '*[^a-z0-9_]*')",
            name="ck_analysis_stage_failure_code",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND attempt_count >= 1) OR "
            "(status = 'succeeded' AND source_run_id IS NULL AND output_json IS NOT NULL "
            "AND output_hash IS NOT NULL AND trace_json IS NOT NULL AND trace_hash IS NOT NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'reused' AND source_run_id IS NOT NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND attempt_count = 0 "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND source_run_id IS NULL AND trace_json IS NULL "
            "AND trace_hash IS NULL "
            "AND failure_code IS NOT NULL AND retryable IS NOT NULL AND attempt_count >= 1 "
            "AND (((role IN ('market','fundamentals','announcements','news')) "
            "AND output_json IS NOT NULL AND output_hash IS NOT NULL) OR "
            "((role NOT IN ('market','fundamentals','announcements','news')) "
            "AND output_json IS NULL AND output_hash IS NULL)) "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'blocked' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NOT NULL AND retryable = 0 AND finished_at IS NOT NULL) OR "
            "(status = 'cancelled' AND source_run_id IS NULL AND output_json IS NULL "
            "AND output_hash IS NULL AND trace_json IS NULL AND trace_hash IS NULL "
            "AND failure_code IS NULL AND retryable IS NULL AND finished_at IS NOT NULL)",
            name="ck_analysis_stage_state_shape",
        ),
        sa.CheckConstraint(
            "output_json IS NULL OR " + _json_object("output_json", 65_536),
            name="ck_analysis_stage_output_json",
        ),
        sa.CheckConstraint(
            "trace_json IS NULL OR " + _json_object("trace_json", 32_768),
            name="ck_analysis_stage_trace_json",
        ),
        sa.CheckConstraint(
            "output_hash IS NULL OR (" + _digest("output_hash") + ")",
            name="ck_analysis_stage_output_hash",
        ),
        sa.CheckConstraint(
            "trace_hash IS NULL OR (" + _digest("trace_hash") + ")",
            name="ck_analysis_stage_trace_hash",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["analysis_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_run_id", "source_role"],
            ["analysis_stage.run_id", "analysis_stage.role"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "role"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_analysis_stage_ordinal"),
    )
    op.create_index(
        "ix_analysis_stage_status", "analysis_stage", ["run_id", "status", "ordinal"]
    )
    op.create_index(
        "ix_analysis_stage_source", "analysis_stage", ["source_run_id", "source_role"]
    )

    op.create_table(
        "analysis_attempt",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("model", sa.String(256), nullable=True),
        sa.Column("request_hash", sa.String(71), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("backoff_seconds", sa.Float(), nullable=True),
        sa.Column("template_version", sa.String(64), nullable=True),
        sa.Column("template_hash", sa.String(71), nullable=True),
        sa.Column("usage_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_no BETWEEN 1 AND 6", name="ck_analysis_attempt_no"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','interrupted','cancelled')",
            name="ck_analysis_attempt_status",
        ),
        sa.CheckConstraint(
            "request_hash IS NULL OR (" + _digest("request_hash") + ")",
            name="ck_analysis_attempt_request_hash",
        ),
        sa.CheckConstraint(
            "error_json IS NULL OR " + _json_object("error_json", 16_384),
            name="ck_analysis_attempt_error_json",
        ),
        sa.CheckConstraint(
            "backoff_seconds IS NULL OR backoff_seconds >= 0",
            name="ck_analysis_attempt_backoff",
        ),
        sa.CheckConstraint(
            "template_hash IS NULL OR (" + _digest("template_hash") + ")",
            name="ck_analysis_attempt_template_hash",
        ),
        sa.CheckConstraint(
            "usage_json IS NULL OR " + _json_object("usage_json", 16_384),
            name="ck_analysis_attempt_usage_json",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND error_json IS NULL AND retryable IS NULL "
            "AND backoff_seconds IS NULL AND usage_json IS NULL AND finished_at IS NULL) OR "
            "(status = 'succeeded' AND error_json IS NULL AND retryable IS NULL "
            "AND backoff_seconds IS NULL AND finished_at IS NOT NULL) OR "
            "(status = 'failed' AND error_json IS NOT NULL AND retryable IS NOT NULL "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'interrupted' AND error_json IS NOT NULL AND retryable = 1 "
            "AND finished_at IS NOT NULL) OR "
            "(status = 'cancelled' AND error_json IS NULL AND retryable IS NULL "
            "AND backoff_seconds IS NULL AND finished_at IS NOT NULL)",
            name="ck_analysis_attempt_state_shape",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "role"],
            ["analysis_stage.run_id", "analysis_stage.role"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "role", "attempt_no"),
    )
    op.create_index(
        "ix_analysis_attempt_status", "analysis_attempt", ["run_id", "status", "role"]
    )

    op.create_table(
        "analysis_report",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("report_id", sa.String(71), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("report_hash", sa.String(71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_digest("report_id"), name="ck_analysis_report_id"),
        sa.CheckConstraint(_digest("report_hash"), name="ck_analysis_report_hash"),
        sa.CheckConstraint(
            _json_object("report_json", 1_048_576),
            name="ck_analysis_report_json",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["analysis_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_analysis_report_id", "analysis_report", ["report_id", "created_at"]
    )
    _create_immutability_triggers()


def downgrade() -> None:
    for table in (
        "analysis_report",
        "analysis_attempt",
        "analysis_stage",
        "analysis_run",
    ):
        for operation in ("insert", "update", "delete"):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{operation}")
            )
    for table in ("analysis_stage", "analysis_attempt", "analysis_report"):
        for operation in ("insert", "update", "delete"):
            op.execute(
                sa.text(
                    f"DROP TRIGGER IF EXISTS trg_{table}_owner_terminal_{operation}"
                )
            )
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_analysis_run_bind_once"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_analysis_run_config_immutable"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_analysis_run_terminal_guard"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_analysis_report_identity_guard"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_analysis_stage_reuse_identity"))
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_analysis_stage_reuse_identity_insert")
    )
    for table in ("analysis_stage", "analysis_attempt", "analysis_report"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_identity_immutable"))
    op.drop_index("ix_analysis_report_id", table_name="analysis_report")
    op.drop_table("analysis_report")
    op.drop_index("ix_analysis_attempt_status", table_name="analysis_attempt")
    op.drop_table("analysis_attempt")
    op.drop_index("ix_analysis_stage_source", table_name="analysis_stage")
    op.drop_index("ix_analysis_stage_status", table_name="analysis_stage")
    op.drop_table("analysis_stage")
    op.drop_index("uq_analysis_run_active_retry", table_name="analysis_run")
    op.drop_index("ix_analysis_run_symbol", table_name="analysis_run")
    op.drop_index("ix_analysis_run_status", table_name="analysis_run")
    op.drop_index("ix_analysis_run_created", table_name="analysis_run")
    op.drop_table("analysis_run")


def _create_immutability_triggers() -> None:
    tables = {
        "analysis_run": f"OLD.status IN {_TERMINAL_RUN}",
        "analysis_stage": f"OLD.status IN {_TERMINAL_STAGE}",
        "analysis_attempt": f"OLD.status IN {_TERMINAL_ATTEMPT}",
        "analysis_report": "1",
    }
    keys = {
        "analysis_run": "id = NEW.id OR task_id = NEW.task_id",
        "analysis_stage": "run_id = NEW.run_id AND (role = NEW.role OR ordinal = NEW.ordinal)",
        "analysis_attempt": (
            "run_id = NEW.run_id AND role = NEW.role AND attempt_no = NEW.attempt_no"
        ),
        "analysis_report": "run_id = NEW.run_id",
    }
    for table, terminal in tables.items():
        op.execute(
            sa.text(f"""
            CREATE TRIGGER trg_{table}_immutable_insert
            BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {keys[table]})
            BEGIN
                SELECT RAISE(ABORT, 'analysis artifact identity is immutable');
            END
            """)
        )
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(f"""
                CREATE TRIGGER trg_{table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN {terminal}
                BEGIN
                    SELECT RAISE(ABORT, 'terminal analysis artifact is immutable');
                END
                """)
            )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_analysis_run_config_immutable
        BEFORE UPDATE ON analysis_run
        WHEN NEW.id IS NOT OLD.id
          OR NEW.task_id IS NOT OLD.task_id
          OR NEW.symbol IS NOT OLD.symbol
          OR NEW.model_config_id IS NOT OLD.model_config_id
          OR NEW.model_provider IS NOT OLD.model_provider
          OR NEW.model_name IS NOT OLD.model_name
          OR NEW.model_config_json IS NOT OLD.model_config_json
          OR NEW.model_config_hash IS NOT OLD.model_config_hash
          OR NEW.retry_policy_json IS NOT OLD.retry_policy_json
          OR NEW.retry_policy_hash IS NOT OLD.retry_policy_hash
          OR NEW.config_fingerprint IS NOT OLD.config_fingerprint
          OR NEW.parent_run_id IS NOT OLD.parent_run_id
          OR NEW.requested_stage IS NOT OLD.requested_stage
        BEGIN
            SELECT RAISE(ABORT, 'analysis run configuration is immutable');
        END
        """)
    )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_analysis_stage_reuse_identity_insert
        BEFORE INSERT ON analysis_stage
        WHEN NEW.status = 'reused' AND NOT EXISTS (
            SELECT 1
            FROM analysis_run child
            JOIN analysis_run source ON source.id = NEW.source_run_id
            JOIN analysis_stage source_stage
              ON source_stage.run_id = NEW.source_run_id
             AND source_stage.role = NEW.source_role
            WHERE child.id = NEW.run_id
              AND source.status IN ('succeeded','partial','insufficient_evidence','failed','cancelled')
              AND child.snapshot_hash = source.snapshot_hash
              AND child.evidence_graph_hash = source.evidence_graph_hash
              AND child.model_config_hash = source.model_config_hash
              AND child.retry_policy_hash = source.retry_policy_hash
              AND child.config_fingerprint = source.config_fingerprint
              AND (
                (source_stage.status = 'succeeded'
                 AND source_stage.output_json IS NOT NULL
                 AND source_stage.output_hash IS NOT NULL
                 AND source_stage.trace_json IS NOT NULL
                 AND source_stage.trace_hash IS NOT NULL)
                OR
                (source_stage.status = 'failed'
                 AND source_stage.role IN ('market','fundamentals','announcements','news')
                 AND source_stage.output_json IS NOT NULL
                 AND source_stage.output_hash IS NOT NULL
                 AND source_stage.trace_json IS NULL
                 AND source_stage.trace_hash IS NULL)
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'analysis reuse inputs are inconsistent');
        END
        """)
    )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_analysis_run_bind_once
        BEFORE UPDATE ON analysis_run
        WHEN OLD.snapshot_id IS NOT NULL AND (
            NEW.snapshot_id IS NOT OLD.snapshot_id OR
            NEW.snapshot_json IS NOT OLD.snapshot_json OR
            NEW.snapshot_hash IS NOT OLD.snapshot_hash OR
            NEW.evidence_graph_json IS NOT OLD.evidence_graph_json OR
            NEW.evidence_graph_hash IS NOT OLD.evidence_graph_hash OR
            NEW.config_fingerprint IS NOT OLD.config_fingerprint
        )
        BEGIN
            SELECT RAISE(ABORT, 'analysis inputs are already bound');
        END
        """)
    )
    identity_columns = {
        "analysis_stage": "NEW.run_id IS NOT OLD.run_id OR NEW.role IS NOT OLD.role OR NEW.ordinal IS NOT OLD.ordinal",
        "analysis_attempt": "NEW.run_id IS NOT OLD.run_id OR NEW.role IS NOT OLD.role OR NEW.attempt_no IS NOT OLD.attempt_no",
        "analysis_report": "NEW.run_id IS NOT OLD.run_id",
    }
    for table, condition in identity_columns.items():
        op.execute(
            sa.text(f"""
            CREATE TRIGGER trg_{table}_identity_immutable
            BEFORE UPDATE ON {table}
            WHEN {condition}
            BEGIN
                SELECT RAISE(ABORT, 'analysis artifact identity is immutable');
            END
            """)
        )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_analysis_stage_reuse_identity
        BEFORE UPDATE ON analysis_stage
        WHEN NEW.status = 'reused' AND NOT EXISTS (
            SELECT 1
            FROM analysis_run child
            JOIN analysis_run source ON source.id = NEW.source_run_id
            JOIN analysis_stage source_stage
              ON source_stage.run_id = NEW.source_run_id
             AND source_stage.role = NEW.source_role
            WHERE child.id = NEW.run_id
              AND source.status IN ('succeeded','partial','insufficient_evidence','failed','cancelled')
              AND child.snapshot_hash = source.snapshot_hash
              AND child.evidence_graph_hash = source.evidence_graph_hash
              AND child.model_config_hash = source.model_config_hash
              AND child.retry_policy_hash = source.retry_policy_hash
              AND child.config_fingerprint = source.config_fingerprint
              AND (
                (source_stage.status = 'succeeded'
                 AND source_stage.output_json IS NOT NULL
                 AND source_stage.output_hash IS NOT NULL
                 AND source_stage.trace_json IS NOT NULL
                 AND source_stage.trace_hash IS NOT NULL)
                OR
                (source_stage.status = 'failed'
                 AND source_stage.role IN ('market','fundamentals','announcements','news')
                 AND source_stage.output_json IS NOT NULL
                 AND source_stage.output_hash IS NOT NULL
                 AND source_stage.trace_json IS NULL
                 AND source_stage.trace_hash IS NULL)
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'analysis reuse inputs are inconsistent');
        END
        """)
    )
    op.execute(
        sa.text(f"""
        CREATE TRIGGER trg_analysis_run_terminal_guard
        BEFORE UPDATE OF status ON analysis_run
        WHEN NEW.status IN {_TERMINAL_RUN} AND (
            EXISTS (
                SELECT 1 FROM analysis_stage
                WHERE run_id = NEW.id AND status IN ('pending','running')
            ) OR (
                NEW.status IN ('succeeded','partial','insufficient_evidence') AND (
                    NEW.snapshot_id IS NULL OR
                    NOT EXISTS (
                        SELECT 1 FROM analysis_report
                        WHERE run_id = NEW.id
                          AND (
                            (NEW.status = 'succeeded'
                             AND json_extract(report_json, '$.status') = 'complete') OR
                            (NEW.status = 'partial'
                             AND json_extract(report_json, '$.status') = 'partial') OR
                            (NEW.status = 'insufficient_evidence'
                             AND json_extract(report_json, '$.status') = 'insufficient_evidence')
                          )
                    )
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'analysis run cannot finish before its artifacts');
        END
        """)
    )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_analysis_report_identity_guard
        BEFORE INSERT ON analysis_report
        WHEN NOT EXISTS (
            SELECT 1 FROM analysis_run
            WHERE id = NEW.run_id
              AND status = 'running'
              AND snapshot_id IS NOT NULL
              AND json_extract(NEW.report_json, '$.report_id') = NEW.report_id
              AND json_extract(NEW.report_json, '$.snapshot_id') = snapshot_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'analysis report identity is inconsistent');
        END
        """)
    )
    for table in ("analysis_stage", "analysis_attempt", "analysis_report"):
        owner = "NEW.run_id" if table != "analysis_attempt" else "NEW.run_id"
        op.execute(
            sa.text(f"""
            CREATE TRIGGER trg_{table}_owner_terminal_insert
            BEFORE INSERT ON {table}
            WHEN EXISTS (
                SELECT 1 FROM analysis_run
                WHERE id = {owner} AND status IN {_TERMINAL_RUN}
            )
            BEGIN
                SELECT RAISE(ABORT, 'terminal analysis run is immutable');
            END
            """)
        )
        for operation in ("UPDATE", "DELETE"):
            owner_ids = (
                "(OLD.run_id, NEW.run_id)" if operation == "UPDATE" else "(OLD.run_id)"
            )
            op.execute(
                sa.text(f"""
                CREATE TRIGGER trg_{table}_owner_terminal_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN EXISTS (
                    SELECT 1 FROM analysis_run
                    WHERE id IN {owner_ids} AND status IN {_TERMINAL_RUN}
                )
                BEGIN
                    SELECT RAISE(ABORT, 'terminal analysis run is immutable');
                END
                """)
            )
