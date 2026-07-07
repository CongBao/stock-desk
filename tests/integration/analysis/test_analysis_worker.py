from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from stock_desk.analysis.data_service import ResearchDataService
from pydantic import ValidationError
import pytest
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.analysis.repository import (
    AnalysisAttemptStatus,
    AnalysisRepositoryError,
    AnalysisRepository,
    AnalysisRunStatus,
    AnalysisStageStatus,
)
from stock_desk.analysis.retry import RetryPolicy
from stock_desk.analysis.roles import RoleName
from stock_desk.analysis.runner import AnalysisRunner
from stock_desk.analysis.providers.base import ModelAuthenticationError
from stock_desk.analysis.snapshot import ResearchSection
from stock_desk.analysis.worker import AnalysisWorkerHandler
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.config import Settings
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker
from tests.integration.analysis.test_partial_report import (
    FROZEN_AT,
    evidence_graph,
    frozen_snapshot,
)
from tests.integration.analysis.test_runner import ScriptedProvider


CLAIMED_AT = datetime.now(timezone.utc) + timedelta(seconds=1)


def test_public_run_model_config_rejects_secret_bearing_fields() -> None:
    base = {
        "provider": ModelProviderKind.OPENAI_COMPATIBLE,
        "base_url": "https://example.com",
        "model": "vendor-chat",
        "temperature": 0.1,
        "timeout_seconds": 30.0,
        "max_output_tokens": 1024,
        "api_key_configured": False,
    }
    for secret_field in ("api_key", "Authorization", "headers"):
        with pytest.raises(ValidationError):
            AnalysisModelPublicConfig.model_validate(
                {**base, secret_field: {"secret": "TOP-SECRET"}}
            )


class CountingLoader:
    def __init__(self, section: ResearchSection) -> None:
        self.kind = section.kind
        self._section = section
        self.calls = 0

    def load(self, _symbol: str) -> ResearchSection:
        self.calls += 1
        return self._section


class CapturingProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return await super().complete(request)


class CorruptExecutionConfigRepository(AnalysisRepository):
    def load_execution_config(self, _run_id):
        raise AnalysisRepositoryError("TOP-SECRET corrupt config detail")


class MismatchedProvider(ScriptedProvider):
    provider = "wrong-provider"
    model = "wrong-model"


def _handler(
    repository: AnalysisRepository,
    *,
    data_service: ResearchDataService,
    evidence_factory: Callable,
    provider_factory: Callable | None = None,
    clock: Callable[[], datetime] = lambda: CLAIMED_AT,
) -> AnalysisWorkerHandler:
    async def no_wait(_delay: float) -> None:
        await asyncio.sleep(0)

    return AnalysisWorkerHandler(
        repository=repository,
        provider_factory=provider_factory or (lambda _config: ScriptedProvider()),
        data_service=data_service,
        evidence_factory=evidence_factory,
        sleeper=no_wait,
        clock=clock,
    )


def test_claimed_worker_runs_queued_analysis_with_injected_dependencies(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-worker.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    snapshot = frozen_snapshot()
    loaders = tuple(CountingLoader(section) for section in snapshot.sections)
    data_service = ResearchDataService(
        loaders=loaders,
        clock=lambda: FROZEN_AT,
    )
    graph_calls = []

    def build_evidence(value):
        graph_calls.append(value.snapshot_id)
        return evidence_graph(value)

    task = tasks.create(
        "analysis.run",
        {"symbol": snapshot.symbol},
    )
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol=snapshot.symbol,
        retry_policy=RetryPolicy(max_retries=0),
        model_provider="openai_compatible",
        model_name="vendor-chat",
        model_public_config=AnalysisModelPublicConfig(
            provider=ModelProviderKind.OPENAI_COMPATIBLE,
            base_url="https://example.com",
            model="vendor-chat",
            temperature=0.7,
            timeout_seconds=12.0,
            max_output_tokens=1234,
            api_key_configured=False,
        ),
        now=FROZEN_AT,
    )
    worker = TaskWorker(tasks, worker_id="analysis-worker")
    configs = []
    captured_provider = CapturingProvider()

    def provider_factory(config):
        configs.append(config)
        return captured_provider

    worker.register_claimed(
        "analysis.run",
        _handler(
            repository,
            data_service=data_service,
            evidence_factory=build_evidence,
            provider_factory=provider_factory,
        ),
    )

    completed = worker.run_once()

    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.result == {
        "analysis_run_id": pending.id,
        "report_id": repository.get_report(pending.id).report_id,
        "status": "succeeded",
    }
    assert repository.get_run(pending.id).status is AnalysisRunStatus.SUCCEEDED
    assert graph_calls == [repository.get_run(pending.id).snapshot_id]
    assert [loader.calls for loader in loaders] == [1, 1, 1, 1]
    assert configs[0].retry_policy == RetryPolicy(max_retries=0)
    assert configs[0].provider == "openai_compatible"
    assert all(request.temperature == 0.7 for request in captured_provider.requests)
    assert all(
        request.timeout_seconds == 12.0 for request in captured_provider.requests
    )
    assert all(
        request.max_output_tokens == 1234 for request in captured_provider.requests
    )
    engine.dispose()


def test_expired_claim_resumes_data_without_reloading_successful_stage(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-worker-resume.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    snapshot = frozen_snapshot()
    loaders = tuple(CountingLoader(section) for section in snapshot.sections)
    data_service = ResearchDataService(
        loaders=loaders,
        clock=lambda: FROZEN_AT,
    )
    task = tasks.create(
        "analysis.run",
        {"symbol": snapshot.symbol},
    )
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol=snapshot.symbol,
        retry_policy=RetryPolicy(max_retries=1),
        model_provider="openai_compatible",
        model_name="vendor-chat",
        now=FROZEN_AT,
    )
    stale = tasks.claim_next(
        "stale-worker",
        now=CLAIMED_AT,
        lease_duration=timedelta(seconds=1),
    )
    assert isinstance(stale, TaskClaim)
    repository.start_run(stale, pending.id, now=CLAIMED_AT)
    market_attempt = repository.start_attempt(
        stale,
        pending.id,
        "market",
        provider=None,
        model=None,
        request_hash=None,
        now=CLAIMED_AT,
    )
    market = snapshot.section(loaders[0].kind)
    assert market is not None
    repository.finish_data_attempt_success(
        stale,
        pending.id,
        "market",
        market_attempt.attempt_no,
        market,
        now=CLAIMED_AT,
    )
    assert tasks.get(task.id).progress == 1 / 9
    repository.start_attempt(
        stale,
        pending.id,
        "fundamentals",
        provider=None,
        model=None,
        request_hash=None,
        now=CLAIMED_AT,
    )

    replacement = tasks.claim_next(
        "replacement-worker",
        now=CLAIMED_AT + timedelta(seconds=2),
        lease_duration=timedelta(minutes=5),
    )
    assert isinstance(replacement, TaskClaim)
    assert replacement.attempt_count == 2
    handler = _handler(
        repository,
        data_service=data_service,
        evidence_factory=evidence_graph,
        clock=lambda: CLAIMED_AT + timedelta(seconds=2),
    )

    result = handler(replacement)

    assert result == {
        "analysis_run_id": pending.id,
        "report_id": repository.get_report(pending.id).report_id,
        "status": "succeeded",
    }
    assert repository.get_run(pending.id).status is AnalysisRunStatus.SUCCEEDED
    assert [loader.calls for loader in loaders] == [0, 1, 1, 1]
    attempts = repository.list_attempts(pending.id, "fundamentals")
    assert [(item.attempt_no, item.status) for item in attempts] == [
        (1, AnalysisAttemptStatus.INTERRUPTED),
        (2, AnalysisAttemptStatus.SUCCEEDED),
    ]
    engine.dispose()


def test_production_runtime_can_register_injected_analysis_claimed_handler(
    tmp_path,
) -> None:
    def injected(_claim: TaskClaim):
        return {"status": "injected"}

    runtime = ProductionMarketWorker.open(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'analysis-runtime.db'}",
            data_dir=tmp_path,
        ),
        worker_id="runtime-analysis",
        analysis_handler=injected,
    )
    try:
        assert "analysis.run" in runtime.worker.registered_claimed_kinds
    finally:
        runtime.close()


def test_worker_failure_terminalizes_task_and_analysis_without_partial_report(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-worker-failure.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    snapshot = frozen_snapshot()
    loaders = tuple(CountingLoader(section) for section in snapshot.sections)
    data_service = ResearchDataService(
        loaders=loaders,
        clock=lambda: FROZEN_AT,
    )
    task = tasks.create("analysis.run", {"symbol": snapshot.symbol})
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol=snapshot.symbol,
        retry_policy=RetryPolicy(max_retries=0),
        model_provider="openai_compatible",
        model_name="vendor-chat",
        now=FROZEN_AT,
    )

    def fail_evidence(_snapshot):
        raise RuntimeError("private failure detail")

    worker = TaskWorker(tasks, worker_id="analysis-worker-failure")
    worker.register_claimed(
        "analysis.run",
        _handler(
            repository,
            data_service=data_service,
            evidence_factory=fail_evidence,
        ),
    )

    failed = worker.run_once()

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == {
        "code": "analysis_worker_failed",
        "message": "analysis worker failed",
    }
    assert repository.get_run(pending.id).status is AnalysisRunStatus.FAILED


@pytest.mark.parametrize(
    "preflight_failure",
    ("config_corruption", "provider_factory", "provider_identity"),
)
def test_worker_preflight_failure_atomically_terminalizes_task_and_run(
    tmp_path,
    preflight_failure: str,
) -> None:
    url = f"sqlite:///{tmp_path / f'analysis-preflight-{preflight_failure}.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository: AnalysisRepository = (
        CorruptExecutionConfigRepository(engine)
        if preflight_failure == "config_corruption"
        else AnalysisRepository(engine)
    )
    snapshot = frozen_snapshot()
    data_service = ResearchDataService(
        loaders=tuple(CountingLoader(section) for section in snapshot.sections),
        clock=lambda: FROZEN_AT,
    )
    task = tasks.create("analysis.run", {"symbol": snapshot.symbol})
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol=snapshot.symbol,
        retry_policy=RetryPolicy(max_retries=0),
        model_provider="openai_compatible",
        model_name="vendor-chat",
        now=FROZEN_AT,
    )

    def provider_factory(_config):
        if preflight_failure == "provider_factory":
            raise RuntimeError("TOP-SECRET provider factory detail")
        if preflight_failure == "provider_identity":
            return MismatchedProvider()
        return ScriptedProvider()

    worker = TaskWorker(tasks, worker_id=f"preflight-{preflight_failure}")
    worker.register_claimed(
        "analysis.run",
        _handler(
            repository,
            data_service=data_service,
            evidence_factory=evidence_graph,
            provider_factory=provider_factory,
        ),
    )

    failed = worker.run_once()

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == {
        "code": "analysis_worker_failed",
        "message": "analysis worker failed",
    }
    terminal = repository.get_run(pending.id)
    assert terminal.status is AnalysisRunStatus.FAILED
    assert terminal.started_at is not None
    assert all(
        stage.status is AnalysisStageStatus.CANCELLED
        for stage in repository.list_stages(pending.id)
    )
    with pytest.raises(AnalysisRepositoryError):
        repository.get_report(pending.id)
    engine.dispose()


def test_worker_consumes_bound_queued_retry_child_without_reloading_data(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'analysis-worker-bound-child.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    snapshot = frozen_snapshot()
    loaders = tuple(CountingLoader(section) for section in snapshot.sections)
    data_service = ResearchDataService(
        loaders=loaders,
        clock=lambda: FROZEN_AT,
    )
    parent_task = tasks.create("analysis.run", {"symbol": snapshot.symbol})
    parent = repository._create_run_for_existing_task(
        task_id=parent_task.id,
        symbol=snapshot.symbol,
        retry_policy=RetryPolicy(max_retries=0),
        model_provider="openai_compatible",
        model_name="vendor-chat",
        now=FROZEN_AT,
    )
    parent_claim = tasks.claim_next("parent-worker", now=CLAIMED_AT)
    assert isinstance(parent_claim, TaskClaim)
    parent_runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(
            {RoleName.BULL: [ModelAuthenticationError("secret")]}
        ),
        retry_policy=RetryPolicy(max_retries=0),
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: CLAIMED_AT,
        monotonic=lambda: 1.0,
    )
    asyncio.run(
        parent_runner.run(
            claim=parent_claim,
            run_id=parent.id,
            snapshot=snapshot,
            evidence_graph=evidence_graph(snapshot),
        )
    )
    child = repository.enqueue_retry(parent.id, RoleName.BULL.value, now=CLAIMED_AT)
    worker = TaskWorker(tasks, worker_id="bound-child-worker")
    worker.register_claimed(
        "analysis.run",
        _handler(
            repository,
            data_service=data_service,
            evidence_factory=evidence_graph,
        ),
    )

    completed = worker.run_once()

    assert completed is not None and completed.status == "succeeded"
    assert repository.get_run(child.run.id).status is AnalysisRunStatus.SUCCEEDED
    assert all(loader.calls == 0 for loader in loaders)
