"""Integration coverage for durable analysis repository behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from stock_desk.analysis.repository import (
    AnalysisConflict,
    AnalysisHistoryKey,
    AnalysisRepository,
    AnalysisRunStatus,
    AnalysisStageStatus,
    AnalysisAttemptStatus,
)
from stock_desk.analysis.report import ReportStatus, ResearchReport
from stock_desk.analysis.retry import RetryDecision, RetryPolicy
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository


UTC = timezone.utc
NOW = datetime(2025, 7, 6, 9, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _insufficient_report(status: ReportStatus) -> ResearchReport:
    return ResearchReport.model_construct(
        schema_version="analysis-report-v1",
        report_id=DIGEST,
        snapshot_id=DIGEST,
        status=status,
        rating=None,
        confidence=0.0,
        confidence_explanation="Insufficient evidence.",
        core_judgments=(),
        bull_claims=(),
        bear_claims=(),
        risks=(),
        evidence_items=(),
        role_outputs=(),
        model_metadata=(),
        quality_flags=(),
        quality_notes=(),
        missing_modules=(),
        missing_sections=(),
        recovery_actions=(),
        generated_at=NOW,
        disclaimer="仅供研究参考，不构成任何投资建议。投资有风险，决策需谨慎。",
        retry_actions=(),
        failed_modules=(),
        blocked_modules=(),
        stage_failures=(),
    )


@pytest.mark.parametrize(
    ("report_status", "run_status"),
    [
        (ReportStatus.COMPLETE, AnalysisRunStatus.PARTIAL),
        (ReportStatus.PARTIAL, AnalysisRunStatus.INSUFFICIENT_EVIDENCE),
        (ReportStatus.INSUFFICIENT_EVIDENCE, AnalysisRunStatus.SUCCEEDED),
    ],
)
def test_finalize_run_rejects_report_status_mismatch_before_any_write(
    tmp_path: Path,
    report_status: ReportStatus,
    run_status: AnalysisRunStatus,
) -> None:
    url = f"sqlite:///{tmp_path / f'analysis-finalize-{report_status}.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    enqueued = repository.enqueue_run(
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=NOW,
    )
    claim = tasks.claim_next(
        "finalize-worker",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert isinstance(claim, TaskClaim)
    repository.start_run(claim, enqueued.run.id, now=NOW)

    with pytest.raises(AnalysisConflict, match="status"):
        repository.finalize_run(
            claim,
            enqueued.run.id,
            run_status,
            _insufficient_report(report_status),
            now=NOW,
        )

    assert repository.get_run(enqueued.run.id).status is AnalysisRunStatus.RUNNING
    assert tasks.get(enqueued.task.id).status == "running"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM analysis_report")) == 0


def test_analysis_claim_recovery_interrupts_attempt_and_fences_stale_writer(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-recovery.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    run = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=2),
        now=NOW,
    )
    first = tasks.claim_next(
        "worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=1),
    )
    assert isinstance(first, TaskClaim)
    repository.start_run(first, run.id, now=NOW)
    attempt = repository.start_attempt(
        first,
        run.id,
        "technical",
        provider="openai_compatible",
        model="vendor-chat",
        request_hash="sha256:" + "a" * 64,
        now=NOW,
    )
    assert attempt.attempt_no == 1

    second = tasks.claim_next(
        "worker-2",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    assert isinstance(second, TaskClaim)
    repository.resume_run(second, run.id, now=NOW + timedelta(seconds=2))

    attempts = repository.list_attempts(run.id, "technical")
    assert attempts[0].status is AnalysisAttemptStatus.INTERRUPTED
    assert (
        repository.get_stage(run.id, "technical").status is AnalysisStageStatus.PENDING
    )
    with pytest.raises(AnalysisConflict):
        repository.start_attempt(
            first,
            run.id,
            "technical",
            provider="openai_compatible",
            model="vendor-chat",
            request_hash="sha256:" + "b" * 64,
            now=NOW + timedelta(seconds=2),
        )

    retried = repository.start_attempt(
        second,
        run.id,
        "technical",
        provider="openai_compatible",
        model="vendor-chat",
        request_hash="sha256:" + "b" * 64,
        now=NOW + timedelta(seconds=2),
    )
    assert retried.attempt_no == 2
    repository.finish_attempt_failure(
        second,
        run.id,
        "technical",
        retried.attempt_no,
        RetryDecision(False, "model_authentication", "model authentication failed"),
        exhausted=True,
        now=NOW + timedelta(seconds=3),
    )
    stage = repository.get_stage(run.id, "technical")
    assert stage.status is AnalysisStageStatus.FAILED
    assert stage.failure_code == "model_authentication"
    assert repository.list_attempts(run.id, "technical")[-1].safe_error == {
        "code": "model_authentication",
        "message": "model authentication failed",
        "retryable": False,
    }

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage SET failure_code='overwritten' "
                "WHERE run_id=:run_id AND role='technical'"
            ),
            {"run_id": run.id},
        )

    repository.close()


def test_queued_task_cancellation_terminalizes_analysis_domain_rows(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-queued-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    run = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=NOW,
    )

    cancelled = tasks.request_cancel(task.id)

    assert cancelled.status == "cancelled"
    assert repository.get_run(run.id).status.value == "cancelled"
    assert {stage.status for stage in repository.list_stages(run.id)} == {
        AnalysisStageStatus.CANCELLED
    }


def test_enqueue_run_atomically_creates_matching_task_run_and_stages(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-enqueue.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = AnalysisRepository(engine)

    enqueued = repository.enqueue_run(
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=2),
        model_provider="openai_compatible",
        model_name="vendor-chat",
        now=NOW,
    )

    assert enqueued.task.kind == "analysis.run"
    assert enqueued.task.status == "queued"
    assert enqueued.task.payload == {"symbol": "600000.SH"}
    assert enqueued.run.task_id == enqueued.task.id
    assert enqueued.run.model_provider == "openai_compatible"
    assert enqueued.run.current_stage == "market"
    assert len(repository.list_stages(enqueued.run.id)) == 9


def test_enqueue_run_rolls_back_task_and_run_when_stage_insert_fails(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-enqueue-rollback.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = AnalysisRepository(engine)

    def fail_stage_insert(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().startswith("INSERT INTO analysis_stage"):
            raise RuntimeError("injected stage insert failure")

    event.listen(engine, "before_cursor_execute", fail_stage_insert)
    try:
        with pytest.raises(RuntimeError, match="injected stage insert failure"):
            repository.enqueue_run(
                symbol="600000.SH",
                retry_policy=RetryPolicy(max_retries=0),
                now=NOW,
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_stage_insert)

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM task_run")) == 0
        assert connection.scalar(text("SELECT count(*) FROM analysis_run")) == 0
        assert connection.scalar(text("SELECT count(*) FROM analysis_stage")) == 0


def test_history_page_uses_stable_descending_keyset_and_symbol_filter(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-history.db'}"
    migrate(url)
    repository = AnalysisRepository(create_engine_for_url(url))
    first = repository.enqueue_run(
        symbol="600000.SH", retry_policy=RetryPolicy(max_retries=0), now=NOW
    )
    second = repository.enqueue_run(
        symbol="000001.SZ", retry_policy=RetryPolicy(max_retries=0), now=NOW
    )
    third = repository.enqueue_run(
        symbol="600000.SH", retry_policy=RetryPolicy(max_retries=0), now=NOW
    )

    page_one = repository.list_history_page(limit=2)
    assert tuple(item.id for item in page_one.items) == tuple(
        sorted((first.run.id, second.run.id, third.run.id), reverse=True)[:2]
    )
    assert page_one.next_key is not None
    page_two = repository.list_history_page(limit=2, after=page_one.next_key)
    assert (
        len(
            {
                *(item.id for item in page_one.items),
                *(item.id for item in page_two.items),
            }
        )
        == 3
    )
    assert set(item.id for item in page_one.items).isdisjoint(
        item.id for item in page_two.items
    )

    filtered = repository.list_history_page(limit=10, symbol="600000.SH")
    assert tuple(item.symbol for item in filtered.items) == ("600000.SH",) * 2
    assert tuple(item.id for item in filtered.items) == tuple(
        sorted((first.run.id, third.run.id), reverse=True)
    )

    exact_after = AnalysisHistoryKey(
        created_at=page_one.items[-1].created_at,
        id=page_one.items[-1].id,
    )
    assert repository.list_history_page(limit=2, after=exact_after) == page_two


def test_stage_projection_exposes_safe_timing_and_duration(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-stage-timing.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    enqueued = repository.enqueue_run(
        symbol="600000.SH", retry_policy=RetryPolicy(max_retries=0), now=NOW
    )
    claim = tasks.claim_next(
        "timing-worker", now=NOW, lease_duration=timedelta(minutes=1)
    )
    assert isinstance(claim, TaskClaim)
    repository.start_run(claim, enqueued.run.id, now=NOW)
    attempt = repository.start_attempt(
        claim,
        enqueued.run.id,
        "technical",
        provider="openai_compatible",
        model="vendor-chat",
        request_hash=DIGEST,
        now=NOW,
    )
    repository.finish_attempt_failure(
        claim,
        enqueued.run.id,
        "technical",
        attempt.attempt_no,
        RetryDecision(False, "model_authentication", "safe failure"),
        exhausted=True,
        now=NOW + timedelta(milliseconds=250),
    )

    stage = repository.get_stage(enqueued.run.id, "technical")
    assert stage.started_at == NOW
    assert stage.finished_at == NOW + timedelta(milliseconds=250)
    assert stage.duration_ms == 250.0


def test_checkpoint_progress_derives_current_stage_from_durable_stage_order(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-progress.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    enqueued = repository.enqueue_run(
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=NOW,
    )
    claim = tasks.claim_next(
        "progress-worker",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert isinstance(claim, TaskClaim)
    repository.start_run(claim, enqueued.run.id, now=NOW)

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage "
                "SET status='cancelled', finished_at=:now, updated_at=:now "
                "WHERE run_id=:run_id AND ordinal < 0"
            ),
            {"now": NOW.isoformat(), "run_id": enqueued.run.id},
        )
        connection.execute(
            text(
                "UPDATE analysis_stage "
                "SET status='running', started_at=:now, updated_at=:now, "
                "attempt_count=1 "
                "WHERE run_id=:run_id AND role IN ('technical','fundamental_news')"
            ),
            {"now": NOW.isoformat(), "run_id": enqueued.run.id},
        )
        repository._checkpoint_progress(connection, claim, enqueued.run.id, NOW)

    assert tasks.get(enqueued.task.id).progress == 4 / 9
    assert repository.get_run(enqueued.run.id).current_stage == "technical"

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE analysis_stage "
                "SET status='cancelled', finished_at=:now, updated_at=:now "
                "WHERE run_id=:run_id AND role='technical'"
            ),
            {"now": NOW.isoformat(), "run_id": enqueued.run.id},
        )
        repository._checkpoint_progress(connection, claim, enqueued.run.id, NOW)

    assert tasks.get(enqueued.task.id).progress == 5 / 9
    assert repository.get_run(enqueued.run.id).current_stage == "fundamental_news"


def test_expired_cancelled_claim_terminalizes_running_analysis_domain_rows(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-expired-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=NOW,
    )
    claim = tasks.claim_next(
        "worker-expired",
        now=NOW,
        lease_duration=timedelta(seconds=1),
    )
    assert isinstance(claim, TaskClaim)
    repository.start_run(claim, pending.id, now=NOW)
    repository.start_attempt(
        claim,
        pending.id,
        "technical",
        provider="openai_compatible",
        model="vendor-chat",
        request_hash="sha256:" + "a" * 64,
        now=NOW,
    )
    tasks.request_cancel(task.id)

    assert tasks.claim_next("worker-next", now=NOW + timedelta(seconds=2)) is None

    assert tasks.get(task.id).status == "cancelled"
    assert repository.get_run(pending.id).status.value == "cancelled"
    assert repository.list_attempts(pending.id, "technical")[0].status is (
        AnalysisAttemptStatus.CANCELLED
    )
    assert repository.get_stage(pending.id, "technical").status is (
        AnalysisStageStatus.CANCELLED
    )


def test_expired_cancel_after_claim_before_run_start_cancels_queued_domain(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-claimed-queued-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    enqueued = repository.enqueue_run(
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=NOW,
    )
    claim = tasks.claim_next(
        "worker-before-start",
        now=NOW,
        lease_duration=timedelta(seconds=1),
    )
    assert isinstance(claim, TaskClaim)
    tasks.request_cancel(enqueued.task.id)

    assert tasks.claim_next("worker-next", now=NOW + timedelta(seconds=2)) is None
    assert repository.get_run(enqueued.run.id).status.value == "cancelled"
    assert {item.status for item in repository.list_stages(enqueued.run.id)} == {
        AnalysisStageStatus.CANCELLED
    }
