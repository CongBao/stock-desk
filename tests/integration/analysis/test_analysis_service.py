from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event, Lock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, text

from stock_desk.analysis.evidence import EvidenceGraph, EvidenceItem
from stock_desk.analysis.model_catalog import (
    AnalysisModelCatalog,
    ModelConfigStatus,
    ModelNotVerified,
)
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.analysis.model_settings import ModelSettingsSecureStorageError
from stock_desk.analysis.providers.base import ModelAuthenticationError
from stock_desk.analysis.repository import (
    AnalysisRepository,
    AnalysisRepositoryError,
    AnalysisRunStatus,
    AnalysisStageStatus,
)
from stock_desk.analysis.roles import RoleName
from stock_desk.analysis.retry import RetryPolicy
from stock_desk.analysis.runner import AnalysisRunner
from stock_desk.analysis.snapshot import ResearchSection, ResearchSnapshot
from stock_desk.analysis.service import (
    AnalysisEvidenceNotFound,
    AnalysisReportNotReady,
    AnalysisReportUnavailable,
    AnalysisService,
    AnalysisServiceStorageError,
    AnalysisStateConflict,
)
from stock_desk.api.analysis import router as analysis_router
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository, TaskRepositoryError
from tests.integration.analysis.test_partial_report import (
    evidence_graph,
    frozen_snapshot,
)
from tests.integration.analysis.test_runner import ScriptedProvider


NOW = datetime(2026, 7, 7, 9, tzinfo=timezone.utc)


def _config() -> AnalysisModelPublicConfig:
    return AnalysisModelPublicConfig(
        provider=ModelProviderKind.OLLAMA,
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        temperature=0.1,
        timeout_seconds=90.0,
        max_output_tokens=4096,
    )


def _service(
    tmp_path: Path,
) -> tuple[
    AnalysisService,
    AnalysisRepository,
    TaskRepository,
    AnalysisModelCatalog,
    str,
]:
    url = f"sqlite:///{tmp_path / 'analysis-service.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = AnalysisRepository(engine)
    tasks = TaskRepository(engine)
    catalog = AnalysisModelCatalog(engine, clock=lambda: NOW)
    saved = catalog.create(display_name="Local", public_config=_config())
    service = AnalysisService(
        repository=repository,
        tasks=tasks,
        model_catalog=catalog,
        clock=lambda: NOW,
    )
    return service, repository, tasks, catalog, saved.id


def _verify(catalog: AnalysisModelCatalog, config_id: str) -> None:
    catalog.mark_test_result(
        config_id,
        expected_status=ModelConfigStatus.UNVERIFIED,
        expected_revision=0,
        succeeded=True,
    )


def test_verified_submit_atomically_creates_task_run_and_nine_stages(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)

    with pytest.raises(ModelNotVerified):
        service.submit(symbol="600000.SH", model_config_id=config_id, max_retries=2)
    assert tasks.list_recent() == []

    _verify(catalog, config_id)
    submission = service.submit(
        symbol="600000.SH", model_config_id=config_id, max_retries=2
    )

    assert submission.status == "queued"
    assert submission.snapshot_id is None
    assert repository.get_run(submission.run_id).model_config_id == config_id
    assert len(repository.list_stages(submission.run_id)) == 9
    assert tasks.get(submission.task_id).status == "queued"


def test_submission_resolver_failure_happens_before_any_persistence(
    tmp_path: Path,
) -> None:
    _service_value, repository, tasks, catalog, config_id = _service(tmp_path)
    calls: list[str] = []

    def reject(_connection: object, configured_id: str) -> object:
        calls.append(configured_id)
        raise ModelSettingsSecureStorageError()

    guarded = AnalysisService(
        repository=repository,
        tasks=tasks,
        model_catalog=catalog,
        execution_resolver=reject,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    with pytest.raises(ModelSettingsSecureStorageError):
        guarded.submit(
            symbol="600000.SH",
            model_config_id=config_id,
            max_retries=0,
        )

    assert calls == [config_id]
    with tasks.engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM task_run), "
                "(SELECT count(*) FROM analysis_run), "
                "(SELECT count(*) FROM analysis_stage)"
            )
        ).one()
    assert tuple(counts) == (0, 0, 0)


def test_submit_and_disable_are_serialized_by_one_database_transaction(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    concurrent_catalog = AnalysisModelCatalog(
        tasks.engine,
        clock=lambda: NOW,
        owns_engine=False,
    )
    start = Barrier(2)
    disable_finished = Event()

    def disable_first() -> object:
        start.wait(timeout=5)
        disabled = concurrent_catalog.disable(config_id, expected_revision=1)
        disable_finished.set()
        return disabled

    def submit_second() -> object:
        start.wait(timeout=5)
        assert disable_finished.wait(timeout=5)
        with pytest.raises(ModelNotVerified):
            service.submit(
                symbol="600000.SH",
                model_config_id=config_id,
                max_retries=0,
            )
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        disabled_future = executor.submit(disable_first)
        submitted_future = executor.submit(submit_second)
        disabled = disabled_future.result(timeout=5)
        assert submitted_future.result(timeout=5) is None

    assert disabled.status is ModelConfigStatus.DISABLED

    with tasks.engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM task_run), "
                "(SELECT count(*) FROM analysis_run), "
                "(SELECT count(*) FROM analysis_stage)"
            )
        ).one()
    assert tuple(counts) == (0, 0, 0)

    second = catalog.create(
        display_name="Second",
        public_config=_config().model_copy(update={"model": "qwen3:14b"}),
    )
    _verify(catalog, second.id)
    start = Barrier(2)
    submission_finished = Event()
    resolver_connections: list[object] = []
    enqueue_connections: list[object] = []
    real_resolver = catalog.require_verified_in_transaction
    real_enqueue = repository.enqueue_run_in_transaction

    def record_resolver(connection: object, configured_id: str) -> object:
        resolver_connections.append(connection)
        return real_resolver(connection, configured_id)  # type: ignore[arg-type]

    def record_enqueue(connection: object, **kwargs: object) -> object:
        enqueue_connections.append(connection)
        return real_enqueue(connection, **kwargs)  # type: ignore[arg-type]

    guarded = AnalysisService(
        repository=repository,
        tasks=tasks,
        model_catalog=catalog,
        execution_resolver=record_resolver,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    repository.enqueue_run_in_transaction = record_enqueue  # type: ignore[method-assign]

    def submit_first() -> object:
        start.wait(timeout=5)
        submission = guarded.submit(
            symbol="000001.SZ",
            model_config_id=second.id,
            max_retries=0,
        )
        submission_finished.set()
        return submission

    def disable_second() -> object:
        start.wait(timeout=5)
        assert submission_finished.wait(timeout=5)
        return concurrent_catalog.disable(second.id, expected_revision=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        submitted_future = executor.submit(submit_first)
        disabled_future = executor.submit(disable_second)
        submission = submitted_future.result(timeout=5)
        disabled = disabled_future.result(timeout=5)

    assert submission.status == "queued"
    assert repository.get_run(submission.run_id).model_config_id == second.id
    assert disabled.status is ModelConfigStatus.DISABLED
    assert resolver_connections == enqueue_connections


def test_cancel_uses_task_repository_for_queued_running_and_terminal_states(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)

    queued = service.submit(
        symbol="600000.SH", model_config_id=config_id, max_retries=0
    )
    cancelled = service.cancel(queued.run_id)
    assert cancelled.run.status is AnalysisRunStatus.CANCELLED
    assert cancelled.task.status == "cancelled"
    assert service.cancel(queued.run_id).run.status is AnalysisRunStatus.CANCELLED
    with pytest.raises(AnalysisReportUnavailable):
        service.report(queued.run_id)

    running = service.submit(
        symbol="000001.SZ", model_config_id=config_id, max_retries=0
    )
    claim = tasks.claim_next(
        "service-worker", now=NOW, lease_duration=timedelta(minutes=1)
    )
    assert isinstance(claim, TaskClaim)
    repository.start_run(claim, running.run_id, now=NOW)
    requested = service.cancel(running.run_id)
    assert requested.run.status is AnalysisRunStatus.RUNNING
    assert requested.task.status == "running"
    assert requested.task.cancel_requested is True

    repository.cancel_run(claim, running.run_id, now=NOW)
    assert service.cancel(running.run_id).run.status is AnalysisRunStatus.CANCELLED

    failed = service.submit(
        symbol="600000.SH", model_config_id=config_id, max_retries=0
    )
    failed_claim = tasks.claim_next(
        "failed-worker", now=NOW, lease_duration=timedelta(minutes=1)
    )
    assert isinstance(failed_claim, TaskClaim)
    repository.start_run(failed_claim, failed.run_id, now=NOW)
    repository.fail_run(
        failed_claim,
        failed.run_id,
        code="model_authentication",
        safe_message="authentication failed",
        now=NOW,
    )
    with pytest.raises(AnalysisStateConflict):
        service.cancel(failed.run_id)
    assert service.detail(failed.run_id).run.failure_code == "model_authentication"


def _run_partial(
    service: AnalysisService,
    repository: AnalysisRepository,
    tasks: TaskRepository,
    config_id: str,
    *,
    symbol: str = "600000.SH",
) -> str:
    submission = service.submit(symbol=symbol, model_config_id=config_id, max_retries=0)
    claim = tasks.claim_next(
        f"runner-{symbol}", now=NOW, lease_duration=timedelta(minutes=1)
    )
    assert isinstance(claim, TaskClaim)
    provider = ScriptedProvider(
        {RoleName.BULL: [ModelAuthenticationError("provider-secret")]}
    )
    provider.provider = "ollama"
    provider.model = "qwen3:8b"
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=repository.load_execution_config(submission.run_id).retry_policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    snapshot = frozen_snapshot()
    if symbol != snapshot.symbol:
        snapshot = type(snapshot).create(
            symbol=symbol,
            frozen_at=snapshot.frozen_at,
            sections=snapshot.sections,
            missing_sections=snapshot.missing_sections,
        )
    result = asyncio.run(
        runner.run(
            claim=claim,
            run_id=submission.run_id,
            snapshot=snapshot,
            evidence_graph=evidence_graph(snapshot),
        )
    )
    assert result.run.status is AnalysisRunStatus.PARTIAL
    return submission.run_id


def _large_inputs() -> tuple[ResearchSnapshot, EvidenceGraph]:
    base = frozen_snapshot()
    sections = tuple(
        ResearchSection(  # type: ignore[call-arg]
            kind=section.kind,
            canonical_source=section.canonical_source,
            source_record=section.source_record,
            source_url=section.source_url,
            published_at=section.published_at,
            data_cutoff=section.data_cutoff,
            fetched_at=section.fetched_at,
            dataset_version=section.dataset_version,
            quality_flags=section.quality_flags,
            route=section.route,
            content={"payload": section.kind.value + ":" + "x" * 55_000},
        )
        for section in base.sections
    )
    snapshot = ResearchSnapshot.create(
        symbol=base.symbol,
        frozen_at=base.frozen_at,
        sections=sections,
        missing_sections=(),
    )
    graph = EvidenceGraph(
        snapshot=snapshot,
        evidence_items=tuple(
            EvidenceItem.create(
                snapshot=snapshot,
                section_kind=sections[index % len(sections)].kind,
                excerpt=f"{index:03d}:" + "e" * 3_000,
            )
            for index in range(200)
        ),
        claims=(),
    )
    return snapshot, graph


def test_report_readiness_persisted_identity_and_corruption_detection(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    queued = service.submit(
        symbol="600000.SH", model_config_id=config_id, max_retries=0
    )
    with pytest.raises(AnalysisReportNotReady):
        service.report(queued.run_id)
    with pytest.raises(AnalysisEvidenceNotFound):
        service.evidence(queued.run_id, "sha256:" + "0" * 64)
    service.cancel(queued.run_id)

    run_id = _run_partial(service, repository, tasks, config_id)
    report = service.report(run_id)
    detail = service.detail(run_id)
    assert report.snapshot_id == detail.run.snapshot_id
    assert detail.run.report_id == report.report_id
    assert report.rating is None
    assert detail.task.status == "succeeded"

    with repository._engine.begin() as connection:
        connection.execute(
            text("DROP TRIGGER trg_analysis_report_owner_terminal_update")
        )
        connection.execute(text("DROP TRIGGER trg_analysis_report_immutable_update"))
        connection.execute(
            text("UPDATE analysis_report SET report_hash=:hash WHERE run_id=:run_id"),
            {"hash": "sha256:" + "0" * 64, "run_id": run_id},
        )
    with pytest.raises(AnalysisRepositoryError):
        service.report(run_id)


def test_evidence_is_loaded_only_from_the_requested_runs_saved_report(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    first_id = _run_partial(service, repository, tasks, config_id)
    second_id = _run_partial(service, repository, tasks, config_id, symbol="000001.SZ")
    first_report = service.report(first_id)
    second_report = service.report(second_id)
    assert first_report.evidence_items
    evidence_id = first_report.evidence_items[0].evidence_id

    assert service.evidence(first_id, evidence_id).evidence_id == evidence_id
    assert evidence_id not in {
        item.evidence_id for item in second_report.evidence_items
    }
    with pytest.raises(AnalysisEvidenceNotFound):
        service.evidence(second_id, evidence_id)
    with pytest.raises(AnalysisEvidenceNotFound):
        service.evidence(first_id, "sha256:" + "0" * 64)


def test_retry_requires_report_action_is_concurrency_unique_and_preserves_parent(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    run_id = _run_partial(service, repository, tasks, config_id)
    with repository._engine.connect() as connection:
        before_run = connection.execute(
            text(
                "SELECT status,error_json,snapshot_json,evidence_graph_json,updated_at "
                "FROM analysis_run WHERE id=:id"
            ),
            {"id": run_id},
        ).one()
        before_report = connection.execute(
            text(
                "SELECT report_id,report_json,report_hash,created_at "
                "FROM analysis_report WHERE run_id=:id"
            ),
            {"id": run_id},
        ).one()

    with pytest.raises(AnalysisStateConflict):
        service.retry(run_id, RoleName.TECHNICAL.value)

    def attempt() -> object:
        try:
            return service.retry(run_id, RoleName.BULL.value)
        except AnalysisStateConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _index: attempt(), range(2)))
    children = tuple(item for item in outcomes if not isinstance(item, Exception))
    conflicts = tuple(
        item for item in outcomes if isinstance(item, AnalysisStateConflict)
    )
    assert len(children) == 1
    assert len(conflicts) == 1
    child = children[0]
    assert child.parent_run_id == run_id
    assert child.requested_stage == RoleName.BULL.value
    assert child.snapshot_id == service.detail(run_id).run.snapshot_id

    with repository._engine.connect() as connection:
        after_run = connection.execute(
            text(
                "SELECT status,error_json,snapshot_json,evidence_graph_json,updated_at "
                "FROM analysis_run WHERE id=:id"
            ),
            {"id": run_id},
        ).one()
        after_report = connection.execute(
            text(
                "SELECT report_id,report_json,report_hash,created_at "
                "FROM analysis_report WHERE run_id=:id"
            ),
            {"id": run_id},
        ).one()
    assert after_run == before_run
    assert after_report == before_report


def _parallel_failure_parent(
    tmp_path: Path,
) -> tuple[AnalysisService, AnalysisRepository, TaskRepository, str, RetryPolicy]:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    parent = service.submit(
        symbol="600000.SH",
        model_config_id=config_id,
        max_retries=0,
    )
    parent_claim = tasks.claim_next(
        "parallel-failure-parent",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert isinstance(parent_claim, TaskClaim)
    failing_provider = ScriptedProvider(
        {
            RoleName.BULL: [ModelAuthenticationError("bull-secret")],
            RoleName.BEAR: [ModelAuthenticationError("bear-secret")],
        }
    )
    failing_provider.provider = "ollama"
    failing_provider.model = "qwen3:8b"
    policy = repository.load_execution_config(parent.run_id).retry_policy
    failing_runner = AnalysisRunner(
        repository=repository,
        provider=failing_provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    snapshot = frozen_snapshot()
    parent_result = asyncio.run(
        failing_runner.run(
            claim=parent_claim,
            run_id=parent.run_id,
            snapshot=snapshot,
            evidence_graph=evidence_graph(snapshot),
        )
    )

    assert parent_result.run.status is AnalysisRunStatus.PARTIAL
    assert tuple(item.stage for item in parent_result.report.retry_actions) == (
        RoleName.BULL,
        RoleName.BEAR,
    )
    return service, repository, tasks, parent.run_id, policy


@pytest.mark.parametrize("requested_stage", [RoleName.BULL, RoleName.BEAR])
def test_each_advertised_parallel_failure_retry_builds_a_runnable_immutable_child(
    tmp_path: Path,
    requested_stage: RoleName,
) -> None:
    service, repository, tasks, parent_id, policy = _parallel_failure_parent(tmp_path)
    other_stage = RoleName.BEAR if requested_stage is RoleName.BULL else RoleName.BULL

    def parent_bytes() -> tuple[object, ...]:
        with tasks.engine.connect() as connection:
            return (
                connection.execute(
                    text("SELECT * FROM analysis_run WHERE id=:id"),
                    {"id": parent_id},
                ).one(),
                connection.execute(
                    text("SELECT * FROM analysis_report WHERE run_id=:id"),
                    {"id": parent_id},
                ).one(),
                tuple(
                    connection.execute(
                        text(
                            "SELECT * FROM analysis_stage WHERE run_id=:id "
                            "ORDER BY ordinal"
                        ),
                        {"id": parent_id},
                    )
                ),
                tuple(
                    connection.execute(
                        text(
                            "SELECT * FROM analysis_attempt WHERE run_id=:id "
                            "ORDER BY role, attempt_no"
                        ),
                        {"id": parent_id},
                    )
                ),
            )

    immutable_parent = parent_bytes()
    application = FastAPI()
    application.include_router(analysis_router)
    application.state.analysis_services_provider = lambda: service
    application.state.database_identity = service.database_identity
    application.state.analysis_cursor_key = b"k" * 32
    client = TestClient(application, raise_server_exceptions=False)
    response = client.post(
        f"/analysis/{parent_id}/stages/{requested_stage.value}/retry"
    )

    assert response.status_code == 202
    assert response.json()["requested_stage"] == requested_stage.value
    duplicate = client.post(f"/analysis/{parent_id}/stages/{other_stage.value}/retry")
    assert duplicate.status_code == 409
    assert duplicate.json() == {"code": "state_conflict"}
    with tasks.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM analysis_run WHERE parent_run_id=:id"),
                {"id": parent_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT count(*) FROM task_run")).scalar_one() == 2
        )
    child_id = response.json()["run_id"]
    child_stages = {stage.role: stage for stage in repository.list_stages(child_id)}
    assert {
        role
        for role, stage in child_stages.items()
        if stage.status is AnalysisStageStatus.PENDING
    } == {RoleName.BULL.value, RoleName.BEAR.value, RoleName.RISK_DECISION.value}
    for role in (
        RoleName.TECHNICAL,
        RoleName.FUNDAMENTAL_NEWS,
    ):
        assert child_stages[role.value].status is AnalysisStageStatus.REUSED
        assert child_stages[role.value].source_run_id == parent_id
    for role in ("market", "fundamentals", "announcements", "news"):
        assert child_stages[role].status is AnalysisStageStatus.REUSED
        assert child_stages[role].source_run_id == parent_id

    child_claim = tasks.claim_next(
        "parallel-failure-child",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert isinstance(child_claim, TaskClaim)
    child_snapshot, child_graph = repository.load_inputs(child_id)
    successful_provider = ScriptedProvider()
    successful_provider.provider = "ollama"
    successful_provider.model = "qwen3:8b"
    child_runner = AnalysisRunner(
        repository=repository,
        provider=successful_provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: NOW,
        monotonic=lambda: 1.0,
    )
    child_result = asyncio.run(
        child_runner.run(
            claim=child_claim,
            run_id=child_id,
            snapshot=child_snapshot,
            evidence_graph=child_graph,
        )
    )

    assert child_result.run.status is AnalysisRunStatus.SUCCEEDED
    assert successful_provider.calls == {
        RoleName.BULL: 1,
        RoleName.BEAR: 1,
        RoleName.RISK_DECISION: 1,
    }
    released = client.post(f"/analysis/{parent_id}/stages/{other_stage.value}/retry")
    assert released.status_code == 202
    assert released.json()["requested_stage"] == other_stage.value
    assert parent_bytes() == immutable_parent


def test_parallel_failure_retry_requests_are_parent_globally_unique(
    tmp_path: Path,
) -> None:
    service, _repository, tasks, parent_id, _policy = _parallel_failure_parent(tmp_path)
    application = FastAPI()
    application.include_router(analysis_router)
    application.state.analysis_services_provider = lambda: service
    application.state.database_identity = service.database_identity
    application.state.analysis_cursor_key = b"k" * 32
    start = Barrier(2)

    def retry(stage: RoleName) -> object:
        start.wait(timeout=5)
        return TestClient(application, raise_server_exceptions=False).post(
            f"/analysis/{parent_id}/stages/{stage.value}/retry"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = tuple(executor.map(retry, (RoleName.BULL, RoleName.BEAR)))

    assert sorted(response.status_code for response in responses) == [202, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json() == {"code": "state_conflict"}
    with tasks.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM analysis_run WHERE parent_run_id=:id"),
                {"id": parent_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT count(*) FROM task_run")).scalar_one() == 2
        )


def test_service_rejects_repository_identity_mismatch(tmp_path: Path) -> None:
    _service_value, repository, tasks, _catalog, _config_id = _service(tmp_path)
    other_url = f"sqlite:///{tmp_path / 'other.db'}"
    migrate(other_url)
    other_catalog = AnalysisModelCatalog(create_engine_for_url(other_url))

    with pytest.raises(AnalysisServiceStorageError):
        AnalysisService(
            repository=repository,
            tasks=tasks,
            model_catalog=other_catalog,
        )


def _clone_partial_state(
    repository: AnalysisRepository,
    submissions: tuple[object, ...],
    template_run_id: str,
) -> None:
    run_ids = tuple(item.run_id for item in submissions)
    task_ids = tuple(item.task_id for item in submissions)
    with repository._engine.begin() as connection:
        trigger_names = connection.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name IN ('analysis_run','analysis_report','task_run')"
            )
        ).scalars()
        for name in tuple(trigger_names):
            connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        template = (
            connection.execute(
                text(
                    "SELECT snapshot_id,snapshot_json,snapshot_hash,evidence_graph_json,"
                    "evidence_graph_hash,started_at,finished_at,updated_at "
                    "FROM analysis_run WHERE id=:id"
                ),
                {"id": template_run_id},
            )
            .mappings()
            .one()
        )
        report = (
            connection.execute(
                text(
                    "SELECT report_id,report_json,report_hash,created_at "
                    "FROM analysis_report WHERE run_id=:id"
                ),
                {"id": template_run_id},
            )
            .mappings()
            .one()
        )
        for run_id in run_ids:
            connection.execute(
                text(
                    "UPDATE analysis_run SET status='partial',current_stage=NULL,"
                    "snapshot_id=:snapshot_id,snapshot_json=:snapshot_json,"
                    "snapshot_hash=:snapshot_hash,"
                    "evidence_graph_json=:evidence_graph_json,"
                    "evidence_graph_hash=:evidence_graph_hash,started_at=:started_at,"
                    "finished_at=:finished_at,updated_at=:updated_at WHERE id=:id"
                ),
                {**template, "id": run_id},
            )
            connection.execute(
                text(
                    "INSERT INTO analysis_report "
                    "(run_id,report_id,report_json,report_hash,created_at) VALUES "
                    "(:run_id,:report_id,:report_json,:report_hash,:created_at)"
                ),
                {**report, "run_id": run_id},
            )
        connection.execute(
            text(
                "UPDATE task_run SET status='succeeded',progress=1.0,"
                "result_json='{}',started_at=:now,finished_at=:now,updated_at=:now "
                f"WHERE id IN ({','.join(f':task_{index}' for index in range(len(task_ids)))})"
            ),
            {
                "now": template["updated_at"],
                **{f"task_{index}": value for index, value in enumerate(task_ids)},
            },
        )
        connection.execute(
            text("UPDATE analysis_report SET report_hash=:hash"),
            {"hash": "sha256:" + "0" * 64},
        )


@pytest.mark.parametrize("partial", [False, True], ids=["queued", "partial"])
def test_history_uses_constant_queries_without_loading_stages_or_reports(
    tmp_path: Path,
    partial: bool,
) -> None:
    service, repository, _tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    template_run_id = (
        _run_partial(service, repository, _tasks, config_id) if partial else None
    )
    submissions = tuple(
        service.submit(symbol="600000.SH", model_config_id=config_id, max_retries=0)
        for _index in range(100)
    )
    if partial:
        assert template_run_id is not None
        _clone_partial_state(repository, submissions, template_run_id)

    select_count = 0

    def count_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(repository._engine, "before_cursor_execute", count_selects)
    try:
        page = service.history(limit=100, after=None, symbol="600000.SH")
    finally:
        event.remove(repository._engine, "before_cursor_execute", count_selects)

    assert len(page.items) == 100
    assert select_count == 1
    assert all(not item.stages and not item.retry_stages for item in page.items)


def test_detail_reads_run_task_and_stages_from_one_consistent_snapshot(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    submission = service.submit(
        symbol="600000.SH", model_config_id=config_id, max_retries=0
    )
    select_finished = Event()
    release_reader = Event()
    pause_lock = Lock()
    paused = False

    def pause_after_first_analysis_select(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal paused
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        if "FROM analysis_run" not in statement:
            return
        with pause_lock:
            if paused:
                return
            paused = True
        select_finished.set()
        assert release_reader.wait(timeout=5)

    event.listen(
        repository._engine, "after_cursor_execute", pause_after_first_analysis_select
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(service.detail, submission.run_id)
            assert select_finished.wait(timeout=5)
            cancelled = tasks.request_cancel(submission.task_id)
            assert cancelled.status == "cancelled"
            release_reader.set()
            detail = future.result(timeout=5)
    finally:
        release_reader.set()
        event.remove(
            repository._engine,
            "after_cursor_execute",
            pause_after_first_analysis_select,
        )

    observed = (
        detail.run.status.value,
        detail.task.status,
        frozenset(stage.status.value for stage in detail.stages),
    )
    assert observed in {
        ("queued", "queued", frozenset({"pending"})),
        ("cancelled", "cancelled", frozenset({"cancelled"})),
    }


@pytest.mark.parametrize(
    ("field", "value", "secret"),
    [
        ("status", "provider-secret-status", "provider-secret-status"),
        ("progress", 1.5, "1.5"),
    ],
)
def test_corrupt_task_state_fails_closed_in_repository_and_analysis_http(
    tmp_path: Path,
    field: str,
    value: object,
    secret: str,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    submission = service.submit(
        symbol="600000.SH", model_config_id=config_id, max_retries=0
    )
    with repository._engine.begin() as connection:
        connection.execute(
            text(f"UPDATE task_run SET {field}=:value WHERE id=:id"),
            {"value": value, "id": submission.task_id},
        )

    with pytest.raises(TaskRepositoryError):
        tasks.get(submission.task_id)

    application = FastAPI()
    application.include_router(analysis_router)
    application.state.analysis_services_provider = lambda: service
    application.state.database_identity = service.database_identity
    application.state.analysis_cursor_key = b"k" * 32
    response = TestClient(application, raise_server_exceptions=False).get(
        f"/analysis/{submission.run_id}"
    )

    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    assert secret not in response.text


def test_history_and_detail_exclude_large_run_payload_columns(
    tmp_path: Path,
) -> None:
    service, repository, tasks, catalog, config_id = _service(tmp_path)
    _verify(catalog, config_id)
    submission = service.submit(
        symbol="600000.SH", model_config_id=config_id, max_retries=0
    )
    claim = tasks.claim_next(
        "large-input-worker", now=NOW, lease_duration=timedelta(minutes=1)
    )
    assert isinstance(claim, TaskClaim)
    repository.start_run(claim, submission.run_id, now=NOW)
    snapshot, graph = _large_inputs()
    repository.bind_inputs(claim, submission.run_id, snapshot, graph, now=NOW)
    run_id = submission.run_id
    with repository._engine.connect() as connection:
        stored_input_bytes = connection.execute(
            text(
                "SELECT length(snapshot_json)+length(evidence_graph_json) "
                "FROM analysis_run WHERE id=:id"
            ),
            {"id": run_id},
        ).scalar_one()
    assert stored_input_bytes > 1_000_000
    result_columns: list[frozenset[str]] = []

    def capture_result_columns(
        _connection: object,
        cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        if "FROM analysis_run" not in statement:
            return
        description = cursor.description  # type: ignore[attr-defined]
        result_columns.append(frozenset(item[0] for item in description))

    event.listen(repository._engine, "after_cursor_execute", capture_result_columns)
    try:
        history = service.history(limit=1, after=None, symbol="600000.SH")
        detail = service.detail(run_id)
    finally:
        event.remove(
            repository._engine,
            "after_cursor_execute",
            capture_result_columns,
        )

    assert len(history.items) == 1
    assert len(detail.stages) == 9
    forbidden = {
        "snapshot_json",
        "snapshot_hash",
        "evidence_graph_json",
        "evidence_graph_hash",
        "model_config_json",
        "model_config_hash",
        "retry_policy_json",
        "retry_policy_hash",
    }
    assert result_columns
    assert all(columns.isdisjoint(forbidden) for columns in result_columns)
    history_columns = next(
        columns
        for columns in result_columns
        if "_task_id" in columns and "_stage_role" not in columns
    )
    detail_columns = next(
        columns for columns in result_columns if "_stage_role" in columns
    )
    assert len(history_columns) == 31
    assert len(detail_columns) == 41
