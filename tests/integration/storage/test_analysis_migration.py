from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from stock_desk.storage.database import create_engine_for_url, downgrade, migrate


ANALYSIS_COLUMNS = {
    "analysis_run": {
        "id",
        "task_id",
        "parent_run_id",
        "requested_stage",
        "symbol",
        "model_config_id",
        "model_provider",
        "model_name",
        "model_config_json",
        "model_config_hash",
        "status",
        "current_stage",
        "error_json",
        "config_fingerprint",
        "snapshot_id",
        "snapshot_json",
        "snapshot_hash",
        "evidence_graph_json",
        "evidence_graph_hash",
        "retry_policy_json",
        "retry_policy_hash",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    },
    "analysis_stage": {
        "run_id",
        "role",
        "ordinal",
        "status",
        "source_run_id",
        "source_role",
        "output_json",
        "output_hash",
        "trace_json",
        "trace_hash",
        "failure_code",
        "retryable",
        "attempt_count",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    },
    "analysis_attempt": {
        "run_id",
        "role",
        "attempt_no",
        "status",
        "provider",
        "model",
        "request_hash",
        "error_json",
        "retryable",
        "backoff_seconds",
        "template_version",
        "template_hash",
        "usage_json",
        "started_at",
        "finished_at",
    },
    "analysis_report": {
        "run_id",
        "report_id",
        "report_json",
        "report_hash",
        "created_at",
    },
}


def test_analysis_revision_upgrades_downgrades_and_reupgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-migration.db'}"

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        inspector = inspect(engine)
        assert ANALYSIS_COLUMNS.keys() <= set(inspector.get_table_names())
        for table, columns in ANALYSIS_COLUMNS.items():
            assert columns == {
                column["name"] for column in inspector.get_columns(table)
            }
        report_indexes = {
            (item["name"], bool(item["unique"]))
            for item in inspector.get_indexes("analysis_report")
        }
        assert ("ix_analysis_report_id", False) in report_indexes
    finally:
        engine.dispose()

    downgrade(url, "0007_backtest_runs")
    engine = create_engine_for_url(url)
    try:
        assert ANALYSIS_COLUMNS.keys().isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    migrate(url)
    engine = create_engine_for_url(url)
    try:
        assert ANALYSIS_COLUMNS.keys() <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
