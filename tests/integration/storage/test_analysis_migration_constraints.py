from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository


NOW = datetime(2025, 7, 6, 9, tzinfo=timezone.utc)
DB_NOW = NOW.isoformat()
DIGEST = "sha256:" + "a" * 64


def insert_run(engine, *, status: str = "queued", bound: bool = False) -> str:
    task = TaskRepository(engine).create("analysis.run", {"symbol": "600000.SH"})
    run_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO analysis_run (
                    id, task_id, parent_run_id, requested_stage, symbol,
                    model_config_id, model_provider, model_name,
                    model_config_json, model_config_hash, status,
                    current_stage, error_json, config_fingerprint,
                    snapshot_id, snapshot_json, snapshot_hash,
                    evidence_graph_json, evidence_graph_hash,
                    retry_policy_json, retry_policy_hash,
                    created_at, updated_at, started_at, finished_at
                ) VALUES (
                    :id, :task_id, NULL, NULL, '600000.SH',
                    :digest, 'stub-provider', 'stub-model',
                    :model_config_json, :digest, :status,
                    NULL, NULL, :digest,
                    :snapshot_id, :snapshot_json, :snapshot_hash,
                    :evidence_json, :evidence_hash,
                    :retry_policy_json, :digest,
                    :now, :now, :started_at, NULL
                )
                """
            ),
            {
                "id": run_id,
                "task_id": task.id,
                "status": status,
                "snapshot_id": DIGEST if bound else None,
                "snapshot_json": '{"snapshot_id":"' + DIGEST + '"}' if bound else None,
                "snapshot_hash": DIGEST if bound else None,
                "evidence_json": '{"evidence_items":[]}' if bound else None,
                "evidence_hash": DIGEST if bound else None,
                "retry_policy_json": '{"max_retries":0}',
                "model_config_json": (
                    '{"api_key_configured":false,"base_url":"http://127.0.0.1",'
                    '"max_output_tokens":4096,"model":"stub-model",'
                    '"provider":"stub-provider",'
                    '"schema_version":"analysis-model-public-v1",'
                    '"secret_reference_id":null,"temperature":0.1,'
                    '"timeout_seconds":90.0}'
                ),
                "digest": DIGEST,
                "now": DB_NOW,
                "started_at": DB_NOW if status == "running" else None,
            },
        )
    return run_id


def insert_stage(engine, run_id: str, *, status: str, **overrides: object) -> None:
    values: dict[str, object] = {
        "run_id": run_id,
        "role": "technical",
        "ordinal": 0,
        "status": status,
        "source_run_id": None,
        "source_role": None,
        "output_json": None,
        "output_hash": None,
        "trace_json": None,
        "trace_hash": None,
        "failure_code": None,
        "retryable": None,
        "attempt_count": 0,
        "created_at": DB_NOW,
        "updated_at": DB_NOW,
        "started_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO analysis_stage (
                    run_id, role, ordinal, status, source_run_id, source_role,
                    output_json, output_hash, trace_json, trace_hash,
                    failure_code, retryable, attempt_count,
                    created_at, updated_at, started_at, finished_at
                ) VALUES (
                    :run_id, :role, :ordinal, :status, :source_run_id, :source_role,
                    :output_json, :output_hash, :trace_json, :trace_hash,
                    :failure_code, :retryable, :attempt_count,
                    :created_at, :updated_at, :started_at, :finished_at
                )
                """
            ),
            values,
        )


def test_stage_state_shape_and_data_kind_are_database_enforced(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'stage-shape.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    run_id = insert_run(engine)

    with pytest.raises(IntegrityError):
        insert_stage(engine, run_id, status="succeeded")
    with pytest.raises(IntegrityError):
        insert_stage(
            engine,
            run_id,
            status="failed",
            failure_code=None,
            retryable=None,
            attempt_count=1,
            finished_at=DB_NOW,
        )
    with pytest.raises(IntegrityError):
        insert_stage(engine, run_id, status="pending", output_json="{}")
    with pytest.raises(IntegrityError):
        insert_stage(engine, run_id, status="pending", role="data", ordinal=-1)

    insert_stage(engine, run_id, status="pending", role="market", ordinal=-4)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE analysis_run SET symbol='000001.SZ' WHERE id=:id"),
            {"id": run_id},
        )
    engine.dispose()


def test_queued_analysis_run_identity_is_database_immutable(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'run-identity.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    first = insert_run(engine)
    second = insert_run(engine)
    replacement_task = TaskRepository(engine).create(
        "analysis.run", {"symbol": "600000.SH"}
    )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE analysis_run SET id=:new_id WHERE id=:id"),
            {"new_id": str(uuid4()), "id": first},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE analysis_run SET task_id=:task_id WHERE id=:id"),
            {"task_id": replacement_task.id, "id": second},
        )
    engine.dispose()


def test_terminal_owner_rejects_new_children_and_active_stage_terminalization(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'owner-terminal.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    run_id = insert_run(engine)
    insert_stage(engine, run_id, status="pending")

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_run SET status='cancelled', finished_at=:now "
                "WHERE id=:id"
            ),
            {"id": run_id, "now": DB_NOW},
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage SET status='cancelled', finished_at=:now "
                "WHERE run_id=:id"
            ),
            {"id": run_id, "now": DB_NOW},
        )
        connection.execute(
            text(
                "UPDATE analysis_run SET status='cancelled', finished_at=:now "
                "WHERE id=:id"
            ),
            {"id": run_id, "now": DB_NOW},
        )
    with pytest.raises(IntegrityError):
        insert_stage(
            engine,
            run_id,
            status="pending",
            role="fundamental_news",
            ordinal=1,
        )
    active = insert_run(engine)
    insert_stage(engine, active, status="pending", role="fundamentals", ordinal=-3)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage SET run_id=:terminal "
                "WHERE run_id=:active AND role='fundamentals'"
            ),
            {"terminal": run_id, "active": active},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR REPLACE INTO analysis_stage ("
                "run_id, role, ordinal, status, attempt_count, created_at, updated_at"
                ") VALUES (:run_id, 'fundamentals', -3, 'pending', 0, :now, :now)"
            ),
            {"run_id": active, "now": DB_NOW},
        )
    engine.dispose()


def test_terminal_owner_update_checks_old_and_new_run_independently(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'owner-update-both-sides.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    terminal = insert_run(engine)
    insert_stage(engine, terminal, status="pending")
    active = insert_run(engine)
    insert_stage(engine, active, status="pending", role="fundamentals", ordinal=-3)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage SET status='cancelled', finished_at=:now "
                "WHERE run_id=:id"
            ),
            {"id": terminal, "now": DB_NOW},
        )
        connection.execute(
            text(
                "UPDATE analysis_run SET status='cancelled', finished_at=:now "
                "WHERE id=:id"
            ),
            {"id": terminal, "now": DB_NOW},
        )
        # Isolate the owner fence from the row identity/terminal artifact fences.
        connection.execute(text("DROP TRIGGER trg_analysis_stage_immutable_update"))
        connection.execute(text("DROP TRIGGER trg_analysis_stage_identity_immutable"))

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage SET run_id=:active "
                "WHERE run_id=:terminal AND role='technical'"
            ),
            {"active": active, "terminal": terminal},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage SET run_id=:terminal "
                "WHERE run_id=:active AND role='fundamentals'"
            ),
            {"active": active, "terminal": terminal},
        )
    engine.dispose()


def test_reuse_requires_terminal_source_artifact_with_supported_semantics(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'reuse-source-artifact.db'}"
    migrate(url)
    engine = create_engine_for_url(url)

    def finalize_partial(run_id: str) -> None:
        report_id = "sha256:" + "b" * 64
        report = json.dumps(
            {"report_id": report_id, "snapshot_id": DIGEST, "status": "partial"},
            separators=(",", ":"),
            sort_keys=True,
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO analysis_report "
                    "(run_id, report_id, report_json, report_hash, created_at) "
                    "VALUES (:run_id, :report_id, :report, :digest, :now)"
                ),
                {
                    "run_id": run_id,
                    "report_id": report_id,
                    "report": report,
                    "digest": DIGEST,
                    "now": DB_NOW,
                },
            )
            connection.execute(
                text(
                    "UPDATE analysis_run SET status='partial', finished_at=:now "
                    "WHERE id=:run_id"
                ),
                {"run_id": run_id, "now": DB_NOW},
            )

    failed_model_source = insert_run(engine, status="running", bound=True)
    insert_stage(
        engine,
        failed_model_source,
        status="failed",
        failure_code="model_authentication",
        retryable=False,
        attempt_count=1,
        finished_at=DB_NOW,
    )
    finalize_partial(failed_model_source)
    invalid_child = insert_run(engine, bound=True)
    with pytest.raises(IntegrityError):
        insert_stage(
            engine,
            invalid_child,
            status="reused",
            source_run_id=failed_model_source,
            source_role="technical",
            finished_at=DB_NOW,
        )

    missing_source = insert_run(engine, status="running", bound=True)
    missing_json = json.dumps(
        {
            "attempted_sources": ["fixture"],
            "checked_at": DB_NOW,
            "kind": "news",
            "reason": "timeout",
            "recovery_code": "retry_source_connection",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    insert_stage(
        engine,
        missing_source,
        status="failed",
        role="news",
        ordinal=-1,
        output_json=missing_json,
        output_hash=DIGEST,
        failure_code="data_timeout",
        retryable=False,
        attempt_count=1,
        finished_at=DB_NOW,
    )
    finalize_partial(missing_source)
    valid_child = insert_run(engine, bound=True)
    insert_stage(
        engine,
        valid_child,
        status="reused",
        role="news",
        ordinal=-1,
        source_run_id=missing_source,
        source_role="news",
        finished_at=DB_NOW,
    )
    mismatched_child = insert_run(engine, bound=True)
    mismatched_digest = "sha256:" + "c" * 64
    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_analysis_run_config_immutable"))
        connection.execute(text("DROP TRIGGER trg_analysis_run_bind_once"))
        connection.execute(
            text(
                "UPDATE analysis_run SET model_config_id=:digest, "
                "model_config_hash=:digest, config_fingerprint=:digest "
                "WHERE id=:run_id"
            ),
            {"digest": mismatched_digest, "run_id": mismatched_child},
        )
    with pytest.raises(IntegrityError):
        insert_stage(
            engine,
            mismatched_child,
            status="reused",
            role="news",
            ordinal=-1,
            source_run_id=missing_source,
            source_role="news",
            finished_at=DB_NOW,
        )
    engine.dispose()


def test_attempt_state_shape_and_report_identity_are_database_enforced(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'attempt-report.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    first = insert_run(engine, status="running", bound=True)
    second = insert_run(engine, status="running", bound=True)
    insert_stage(engine, first, status="running", started_at=DB_NOW, attempt_count=1)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO analysis_attempt (
                    run_id, role, attempt_no, status, provider, model,
                    request_hash, error_json, started_at, finished_at
                ) VALUES (
                    :run_id, 'technical', 1, 'running', 'stub', 'model',
                    :digest, '{"code":"bad"}', :now, :now
                )
                """
            ),
            {"run_id": first, "digest": DIGEST, "now": DB_NOW},
        )

    report_id = "sha256:" + "b" * 64
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO analysis_report (
                    run_id, report_id, report_json, report_hash, created_at
                ) VALUES (:run_id, :report_id, :report, :digest, :now)
                """
            ),
            {
                "run_id": first,
                "report_id": report_id,
                "report": json.dumps(
                    {
                        "report_id": report_id,
                        "snapshot_id": "sha256:" + "c" * 64,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "digest": DIGEST,
                "now": DB_NOW,
            },
        )
    for run_id in (first, second):
        report = json.dumps(
            {"report_id": report_id, "snapshot_id": DIGEST},
            separators=(",", ":"),
            sort_keys=True,
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO analysis_report (
                        run_id, report_id, report_json, report_hash, created_at
                    ) VALUES (:run_id, :report_id, :report, :digest, :now)
                    """
                ),
                {
                    "run_id": run_id,
                    "report_id": report_id,
                    "report": report,
                    "digest": DIGEST,
                    "now": DB_NOW,
                },
            )
    engine.dispose()


def test_terminal_run_status_must_match_persisted_report_status(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'report-status-identity.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    run_id = insert_run(engine, status="running", bound=True)
    report_id = "sha256:" + "b" * 64
    report = json.dumps(
        {
            "report_id": report_id,
            "snapshot_id": DIGEST,
            "status": "complete",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO analysis_report "
                "(run_id, report_id, report_json, report_hash, created_at) "
                "VALUES (:run_id, :report_id, :report, :digest, :now)"
            ),
            {
                "run_id": run_id,
                "report_id": report_id,
                "report": report,
                "digest": DIGEST,
                "now": DB_NOW,
            },
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_run SET status='partial', finished_at=:now "
                "WHERE id=:run_id"
            ),
            {"run_id": run_id, "now": DB_NOW},
        )
    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM analysis_run WHERE id=:run_id"),
            {"run_id": run_id},
        ).scalar_one()
    assert status == "running"
    engine.dispose()
