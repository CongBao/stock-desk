from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from stock_desk.analysis.data_service import ResearchDataService
from pydantic import ValidationError
import pytest
from sqlalchemy import event
import stock_desk.market.worker_runtime as worker_runtime_module
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.analysis.model_catalog import AnalysisModelCatalog, ModelCatalogClosed
from stock_desk.analysis.model_settings import ModelSettingsService
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
from stock_desk.analysis.providers.base import (
    ModelAuthenticationError,
    ModelConnectionResult,
)
from stock_desk.analysis.snapshot import ResearchSection
from stock_desk.analysis.worker import AnalysisWorkerHandler
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.config import Settings
from stock_desk.main import create_app
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


class LocalScriptedProvider(ScriptedProvider):
    provider = "ollama"
    model = "qwen3:8b"

    async def test_connection(
        self, *, timeout_seconds: float = 10.0
    ) -> ModelConnectionResult:
        del timeout_seconds
        return ModelConnectionResult(
            connected=True,
            provider=self.provider,
            model=self.model,
        )


class LocalProviderFactory:
    def create(self, _config: AnalysisModelPublicConfig) -> LocalScriptedProvider:
        return LocalScriptedProvider()


def _handler(
    repository: AnalysisRepository,
    *,
    data_service: ResearchDataService,
    data_service_factory: Callable[[], ResearchDataService] | None = None,
    evidence_factory: Callable,
    provider_factory: Callable | None = None,
    clock: Callable[[], datetime] = lambda: CLAIMED_AT,
) -> AnalysisWorkerHandler:
    async def no_wait(_delay: float) -> None:
        await asyncio.sleep(0)

    return AnalysisWorkerHandler(
        repository=repository,
        provider_factory=provider_factory or (lambda _config: ScriptedProvider()),
        data_service_factory=data_service_factory or (lambda: data_service),
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
    factory_calls = []

    def build_evidence(value):
        graph_calls.append(value.snapshot_id)
        return evidence_graph(value)

    def build_data_service():
        factory_calls.append("called")
        return data_service

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
            data_service_factory=build_data_service,
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
    assert factory_calls == ["called"]
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


def test_production_runtime_registers_default_analysis_handler_without_master_key(
    tmp_path,
) -> None:
    runtime = ProductionMarketWorker.open(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'analysis-runtime-default.db'}",
            data_dir=tmp_path,
            master_key=None,
        ),
        worker_id="runtime-analysis-default",
    )
    try:
        assert "analysis.run" in runtime.worker.registered_claimed_kinds
        assert (
            runtime.analysis_repository.database_identity
            == runtime.tasks.database_identity
        )
        assert (
            runtime.model_catalog.database_identity == runtime.tasks.database_identity
        )
    finally:
        runtime.close()


def test_production_runtime_default_handler_completes_real_analysis_task_with_stubs(
    tmp_path,
) -> None:
    snapshot = frozen_snapshot()
    data_service = ResearchDataService(
        loaders=tuple(CountingLoader(section) for section in snapshot.sections),
        clock=lambda: FROZEN_AT,
    )
    provider_calls = []

    def provider_factory(config):
        provider_calls.append(config)
        return ScriptedProvider()

    runtime = ProductionMarketWorker.open(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'analysis-runtime-execute.db'}",
            data_dir=tmp_path,
        ),
        worker_id="runtime-analysis-execute",
        analysis_provider_factory=provider_factory,
        analysis_data_service_factory=lambda: data_service,
    )
    try:
        task = runtime.tasks.create("analysis.run", {"symbol": snapshot.symbol})
        run = runtime.analysis_repository._create_run_for_existing_task(
            task_id=task.id,
            symbol=snapshot.symbol,
            retry_policy=RetryPolicy(max_retries=0),
            model_provider="openai_compatible",
            model_name="vendor-chat",
            model_public_config=AnalysisModelPublicConfig(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                base_url="https://example.com",
                model="vendor-chat",
                temperature=0.1,
                timeout_seconds=30.0,
                max_output_tokens=1024,
                api_key_configured=False,
            ),
            now=FROZEN_AT,
        )

        completed = runtime.run_once()

        assert completed is not None
        assert completed.id == task.id
        assert completed.status == "succeeded"
        assert (
            runtime.analysis_repository.get_run(run.id).status
            is AnalysisRunStatus.SUCCEEDED
        )
        assert len(provider_calls) == 1
    finally:
        runtime.close()


def test_main_http_to_production_worker_flow_uses_one_database_with_stubs(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'analysis-main-worker.db'}"
    settings = Settings(
        database_url=database_url,
        data_dir=tmp_path,
        master_key=None,
    )
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    tasks = TaskRepository(engine)
    catalog = AnalysisModelCatalog(
        engine,
        expected_database_identity=tasks.database_identity,
        owns_engine=False,
    )
    model_settings = ModelSettingsService(
        catalog=catalog,
        secret_store=None,
        provider_factory=LocalProviderFactory(),  # type: ignore[arg-type]
    )
    snapshot = frozen_snapshot()
    data_service = ResearchDataService(
        loaders=tuple(CountingLoader(section) for section in snapshot.sections),
        clock=lambda: FROZEN_AT,
    )
    application = create_app(
        settings,
        task_repository=tasks,
        model_settings_service=model_settings,
    )
    runtime = None
    try:
        with TestClient(application) as client:
            created = client.post(
                "/api/settings/models",
                json={
                    "display_name": "Local Qwen",
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "temperature": 0.1,
                    "timeout": 90.0,
                    "max_output": 4096,
                },
            )
            assert created.status_code == 201
            config_id = created.json()["id"]
            verified = client.post(
                f"/api/settings/models/{config_id}/test",
                json={"expected_revision": 0},
            )
            assert verified.status_code == 200
            assert verified.json()["status"] == "verified"

            submitted = client.post(
                "/api/analysis",
                json={
                    "symbol": snapshot.symbol,
                    "model_config_id": config_id,
                    "retry": {"max_retries": 0},
                },
            )
            assert submitted.status_code == 202
            assert submitted.json()["snapshot_id"] is None
            run_id = submitted.json()["run_id"]

            runtime = ProductionMarketWorker.open(
                settings,
                worker_id="main-http-analysis",
                analysis_provider_factory=lambda _config: LocalScriptedProvider(),
                analysis_data_service_factory=lambda: data_service,
            )
            completed = runtime.run_once()
            assert completed is not None
            assert completed.status == "succeeded"

            detail = client.get(f"/api/analysis/{run_id}")
            report = client.get(f"/api/analysis/{run_id}/report")
            history = client.get("/api/analysis")

        assert detail.status_code == 200
        assert detail.json()["status"] == "succeeded"
        assert detail.json()["snapshot_id"] is not None
        assert report.status_code == 200
        assert report.json()["snapshot_id"] == detail.json()["snapshot_id"]
        assert history.status_code == 200
        assert history.json()["items"][0]["run_id"] == run_id
        assert runtime.tasks.database_identity == tasks.database_identity
    finally:
        if runtime is not None:
            runtime.close()
        catalog.close()
        engine.dispose()


def test_production_runtime_remote_model_without_secret_fails_safely(
    tmp_path,
) -> None:
    runtime = ProductionMarketWorker.open(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'analysis-runtime-remote-secret.db'}",
            data_dir=tmp_path,
            master_key=None,
        ),
        worker_id="runtime-analysis-remote-secret",
    )
    try:
        task = runtime.tasks.create("analysis.run", {"symbol": "600000.SH"})
        run = runtime.analysis_repository._create_run_for_existing_task(
            task_id=task.id,
            symbol="600000.SH",
            retry_policy=RetryPolicy(max_retries=0),
            model_provider="openai_compatible",
            model_name="vendor-chat",
            model_public_config=AnalysisModelPublicConfig(
                provider=ModelProviderKind.OPENAI_COMPATIBLE,
                base_url="https://example.com",
                model="vendor-chat",
                temperature=0.1,
                timeout_seconds=30.0,
                max_output_tokens=1024,
                api_key_configured=False,
            ),
            now=FROZEN_AT,
        )

        failed = runtime.run_once()

        assert failed is not None
        assert failed.status == "failed"
        assert failed.error == {
            "code": "analysis_worker_failed",
            "message": "analysis worker failed",
        }
        terminal = runtime.analysis_repository.get_run(run.id)
        assert terminal.status is AnalysisRunStatus.FAILED
        assert "secret" not in repr(failed.error).casefold()
    finally:
        runtime.close()


def test_production_worker_close_is_best_effort_and_idempotent() -> None:
    calls: list[str] = []

    class Engine:
        def dispose(self) -> None:
            calls.append("engine")

    class SourceSettings:
        def close(self) -> None:
            calls.append("source")
            raise RuntimeError("source close failed")

    class Catalog:
        def close(self) -> None:
            calls.append("catalog")

    runtime = ProductionMarketWorker(
        engine=Engine(),  # type: ignore[arg-type]
        tasks=object(),  # type: ignore[arg-type]
        source_settings=SourceSettings(),  # type: ignore[arg-type]
        worker=object(),  # type: ignore[arg-type]
        scheduler=object(),  # type: ignore[arg-type]
        analysis_repository=object(),  # type: ignore[arg-type]
        model_catalog=Catalog(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="source close failed"):
        runtime.close()
    runtime.close()

    assert calls == ["source", "catalog", "engine"]


def test_production_worker_shared_catalog_disposes_engine_once_on_close(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'worker-close-once.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    disposals: list[bool] = []
    event.listen(engine, "engine_disposed", lambda _engine: disposals.append(True))
    monkeypatch.setattr(
        worker_runtime_module,
        "create_engine_for_url",
        lambda _url: engine,
    )

    runtime = ProductionMarketWorker.open(
        Settings(database_url=database_url, data_dir=tmp_path),
        worker_id="single-dispose",
        analysis_handler=lambda _claim: {"status": "unused"},
    )
    runtime.close()
    runtime.close()

    assert disposals == [True]
    with pytest.raises(ModelCatalogClosed):
        runtime.model_catalog.require_verified("sha256:" + "a" * 64)


def test_production_worker_partial_open_disposes_shared_engine_once_and_closes_catalog(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'worker-open-failure.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    disposals: list[bool] = []
    captured: list[AnalysisModelCatalog] = []
    real_catalog = AnalysisModelCatalog
    event.listen(engine, "engine_disposed", lambda _engine: disposals.append(True))
    monkeypatch.setattr(
        worker_runtime_module,
        "create_engine_for_url",
        lambda _url: engine,
    )

    def capture_catalog(*args: object, **kwargs: object) -> AnalysisModelCatalog:
        catalog = real_catalog(*args, **kwargs)  # type: ignore[arg-type]
        captured.append(catalog)
        return catalog

    monkeypatch.setattr(worker_runtime_module, "AnalysisModelCatalog", capture_catalog)
    monkeypatch.setattr(
        worker_runtime_module,
        "MarketUpdateScheduler",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("scheduler construction failed")
        ),
    )

    with pytest.raises(RuntimeError, match="scheduler construction failed"):
        ProductionMarketWorker.open(
            Settings(database_url=database_url, data_dir=tmp_path),
            worker_id="partial-single-dispose",
            analysis_handler=lambda _claim: {"status": "unused"},
        )

    assert len(captured) == 1
    assert disposals == [True]
    with pytest.raises(ModelCatalogClosed):
        captured[0].require_verified("sha256:" + "a" * 64)


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

    def forbidden_data_service_factory() -> ResearchDataService:
        raise AssertionError("bound retry must not read current source settings")

    worker = TaskWorker(tasks, worker_id="bound-child-worker")
    worker.register_claimed(
        "analysis.run",
        _handler(
            repository,
            data_service=data_service,
            data_service_factory=forbidden_data_service_factory,
            evidence_factory=evidence_graph,
        ),
    )

    completed = worker.run_once()

    assert completed is not None and completed.status == "succeeded"
    assert repository.get_run(child.run.id).status is AnalysisRunStatus.SUCCEEDED
    assert all(loader.calls == 0 for loader in loaders)
