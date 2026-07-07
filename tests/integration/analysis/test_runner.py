from __future__ import annotations

import asyncio
import stock_desk.analysis.runner as runner_module
import threading
import time
from collections import Counter
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, cast

from pydantic import JsonValue
import pytest
from sqlalchemy import text

from stock_desk.analysis.providers.base import (
    ModelAuthenticationError,
    ModelConnectionResult,
    ModelRateLimitError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from stock_desk.analysis.data_service import (
    ResearchDataService,
    ResearchDataUnavailable,
)
from stock_desk.analysis.report import ReportStatus
from stock_desk.analysis.repository import (
    AnalysisConflict,
    AnalysisRepositoryError,
    AnalysisRepository,
    AnalysisRunStatus,
    AnalysisStageStatus,
)
from stock_desk.analysis.retry import RetryPolicy
from stock_desk.analysis.roles import RoleName
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.analysis.runner import AnalysisRunner, _MAX_DATA_WORKER_THREADS
from stock_desk.analysis.runner import AnalysisCancelled
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository
from tests.integration.analysis.test_partial_report import (
    FROZEN_AT,
    evidence_graph,
    frozen_snapshot,
)


def run[T](awaitable: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(awaitable)


def request_role(request: ModelRequest) -> RoleName:
    return RoleName(cast(str, request.data_blocks[0]["role"]))


def valid_content(request: ModelRequest) -> dict[str, JsonValue]:
    role = request_role(request)
    context = request.data_blocks[0]
    evidence_ids = cast(list[str], context["allowed_evidence_ids"])
    content: dict[str, JsonValue] = {
        "role": role.value,
        "snapshot_id": cast(str, context["snapshot_id"]),
        "summary": f"{role.value} summary",
        "claims": [
            {
                "text": f"{role.value} claim",
                "evidence_ids": [evidence_ids[0]],
                "stance": "support",
            }
        ],
    }
    if role is RoleName.RISK_DECISION:
        content["proposal"] = {
            "rating": "neutral",
            "confidence": 0.5,
            "confidence_explanation": "Evidence is balanced and incomplete.",
        }
    return content


class ScriptedProvider:
    provider = "openai_compatible"
    model = "vendor-chat"

    def __init__(self, failures: dict[RoleName, list[Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.calls: Counter[RoleName] = Counter()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        role = request_role(request)
        self.calls[role] += 1
        scripted = self.failures.get(role, [])
        if scripted:
            raise scripted.pop(0)
        return ModelResponse(
            provider=self.provider,
            model=self.model,
            content=valid_content(request),
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )

    async def test_connection(
        self, *, timeout_seconds: float = 10.0
    ) -> ModelConnectionResult:
        del timeout_seconds
        raise AssertionError("runner must not test provider connections")


class ConcurrentProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.active_by_wave: Counter[str] = Counter()
        self.max_active_by_wave: Counter[str] = Counter()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        role = request_role(request)
        wave = (
            "analyst"
            if role
            in {
                RoleName.TECHNICAL,
                RoleName.FUNDAMENTAL_NEWS,
            }
            else "review"
            if role in {RoleName.BULL, RoleName.BEAR}
            else "decision"
        )
        self.active_by_wave[wave] += 1
        self.max_active_by_wave[wave] = max(
            self.max_active_by_wave[wave], self.active_by_wave[wave]
        )
        try:
            await asyncio.sleep(0.01)
            return await super().complete(request)
        finally:
            self.active_by_wave[wave] -= 1


class HangingTechnicalProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request_role(request) is RoleName.TECHNICAL:
            await asyncio.Event().wait()
        return await super().complete(request)


class YieldingTechnicalProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request_role(request) is RoleName.TECHNICAL:
            await asyncio.sleep(0)
        return await super().complete(request)


class LongTechnicalProvider(ScriptedProvider):
    def __init__(self, release: asyncio.Event) -> None:
        super().__init__()
        self.release = release

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request_role(request) is RoleName.TECHNICAL:
            await self.release.wait()
        return await super().complete(request)


class CancelBeforeFinalizeRepository(AnalysisRepository):
    def __init__(self, engine, tasks: TaskRepository) -> None:
        super().__init__(engine)
        self._test_tasks = tasks

    def finalize_run(self, claim, run_id, status, report, *, now):
        self._test_tasks.request_cancel(claim.snapshot.id)
        return super().finalize_run(claim, run_id, status, report, now=now)


class CrashBeforeFirstCheckpointRepository(AnalysisRepository):
    crashed = False

    def finish_attempt_success(
        self, claim, run_id, role, attempt_no, output, trace, *, now
    ):
        if role == RoleName.TECHNICAL.value and not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated worker crash before checkpoint")
        return super().finish_attempt_success(
            claim,
            run_id,
            role,
            attempt_no,
            output,
            trace,
            now=now,
        )


class ScriptedSectionLoader:
    def __init__(self, section, failures=()) -> None:
        self.kind = section.kind
        self.section = section
        self.failures = list(failures)
        self.calls = 0

    def load(self, _symbol):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.section


class BlockingSectionLoader(ScriptedSectionLoader):
    def __init__(self, section, started: threading.Event, release: threading.Event):
        super().__init__(section)
        self.started = started
        self.release = release

    def load(self, _symbol):
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5.0)
        return self.section


class TimeoutThenSuccessSectionLoader(ScriptedSectionLoader):
    def load(self, _symbol):
        self.calls += 1
        if self.calls == 1:
            raise ResearchDataUnavailable(
                kind=self.kind,
                reason=ResearchMissingReason.TIMEOUT,
                attempted_sources=("fixture",),
            )
        return self.section


def claimed_task(tasks: TaskRepository, worker: str) -> TaskClaim:
    tasks.create("analysis.run", {"symbol": "600000.SH"})
    claim = tasks.claim_next(
        worker,
        now=FROZEN_AT,
        lease_duration=timedelta(minutes=5),
    )
    assert isinstance(claim, TaskClaim)
    return claim


def test_runner_persists_every_attempt_and_uses_injected_backoff(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'runner-retry.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(
        max_retries=2,
        base_delay_seconds=0.25,
        max_delay_seconds=1.0,
    )
    claim = claimed_task(tasks, "worker-retry")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    provider = ScriptedProvider(
        {
            RoleName.TECHNICAL: [
                ModelRateLimitError("secret-1"),
                ModelRateLimitError("secret-2"),
            ]
        }
    )
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=sleeper,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run(
            claim=claim,
            run_id=pending.id,
            snapshot=frozen_snapshot(),
            evidence_graph=evidence_graph(frozen_snapshot()),
        )
    )

    assert result.run.status is AnalysisRunStatus.SUCCEEDED
    assert result.report.status is ReportStatus.COMPLETE
    assert provider.calls[RoleName.TECHNICAL] == 3
    assert delays == [0.25, 0.5]
    attempts = repository.list_attempts(pending.id, "technical")
    assert [item.status.value for item in attempts] == ["failed", "failed", "succeeded"]
    assert [item.safe_error["code"] for item in attempts[:2] if item.safe_error] == [
        "model_rate_limit",
        "model_rate_limit",
    ]


def test_runner_rejects_retry_policy_different_from_frozen_run_before_start(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-policy-mismatch.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    claim = claimed_task(tasks, "worker-policy-mismatch")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=RetryPolicy(max_retries=0),
        now=FROZEN_AT,
    )
    provider = ScriptedProvider()
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=RetryPolicy(max_retries=1),
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(ValueError, match="frozen run configuration"):
        run(
            runner.run(
                claim=claim,
                run_id=pending.id,
                snapshot=frozen_snapshot(),
                evidence_graph=evidence_graph(frozen_snapshot()),
            )
        )

    assert repository.get_run(pending.id).status is AnalysisRunStatus.QUEUED
    assert not provider.calls
    assert all(
        not repository.list_attempts(pending.id, stage.role)
        for stage in repository.list_stages(pending.id)
    )


def test_data_runner_rejects_retry_policy_mismatch_before_loading_data(
    tmp_path,
) -> None:
    snapshot = frozen_snapshot()
    loaders = [ScriptedSectionLoader(section) for section in snapshot.sections]
    service = ResearchDataService(loaders=loaders, clock=lambda: FROZEN_AT)
    url = f"sqlite:///{tmp_path / 'data-runner-policy-mismatch.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    claim = claimed_task(tasks, "data-worker-policy-mismatch")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol=snapshot.symbol,
        retry_policy=RetryPolicy(max_retries=0),
        now=FROZEN_AT,
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=RetryPolicy(max_retries=1),
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(ValueError, match="frozen run configuration"):
        run(
            runner.run_from_data(
                claim=claim,
                run_id=pending.id,
                symbol=snapshot.symbol,
                data_service=service,
                evidence_factory=evidence_graph,
            )
        )

    assert repository.get_run(pending.id).status is AnalysisRunStatus.QUEUED
    assert [loader.calls for loader in loaders] == [0, 0, 0, 0]


def test_partial_then_stage_retry_creates_child_and_reuses_valid_successes(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-child.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    parent_claim = claimed_task(tasks, "worker-parent")
    parent = repository._create_run_for_existing_task(
        task_id=parent_claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    provider = ScriptedProvider(
        {RoleName.BULL: [ModelAuthenticationError("api-key-secret")]}
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )
    snapshot = frozen_snapshot()
    graph = evidence_graph(snapshot)

    parent_result = run(
        runner.run(
            claim=parent_claim,
            run_id=parent.id,
            snapshot=snapshot,
            evidence_graph=graph,
        )
    )
    assert parent_result.run.status is AnalysisRunStatus.PARTIAL
    assert parent_result.report.failed_modules == (RoleName.BULL,)
    assert parent_result.report.blocked_modules == (RoleName.RISK_DECISION,)
    assert (
        repository.get_stage(parent.id, "bear").status is AnalysisStageStatus.SUCCEEDED
    )

    enqueued_child = repository.enqueue_retry(
        parent.id,
        RoleName.BULL.value,
        now=FROZEN_AT,
    )
    assert enqueued_child.task.progress == 7 / 9
    assert enqueued_child.run.current_stage == RoleName.BULL.value
    child_claim = tasks.claim_next(
        "worker-child",
        now=FROZEN_AT,
        lease_duration=timedelta(minutes=5),
    )
    assert isinstance(child_claim, TaskClaim)
    child_snapshot, child_graph = repository.load_inputs(enqueued_child.run.id)
    child_result = run(
        runner.run(
            claim=child_claim,
            run_id=enqueued_child.run.id,
            snapshot=child_snapshot,
            evidence_graph=child_graph,
        )
    )

    assert child_result.run.parent_run_id == parent.id
    assert child_result.run.status is AnalysisRunStatus.SUCCEEDED
    assert repository.get_stage(child_result.run.id, "technical").status is (
        AnalysisStageStatus.REUSED
    )
    assert repository.get_stage(child_result.run.id, "bear").source_run_id == parent.id
    assert provider.calls[RoleName.TECHNICAL] == 1
    assert provider.calls[RoleName.FUNDAMENTAL_NEWS] == 1
    assert provider.calls[RoleName.BEAR] == 1
    assert provider.calls[RoleName.BULL] == 2
    assert provider.calls[RoleName.RISK_DECISION] == 1
    assert repository.get_stage(parent.id, "bull").status is AnalysisStageStatus.FAILED


@pytest.mark.parametrize(
    ("failed_role", "expected_blocked"),
    [
        (
            RoleName.TECHNICAL,
            (RoleName.BULL, RoleName.BEAR, RoleName.RISK_DECISION),
        ),
        (
            RoleName.FUNDAMENTAL_NEWS,
            (RoleName.BULL, RoleName.BEAR, RoleName.RISK_DECISION),
        ),
        (RoleName.BULL, (RoleName.RISK_DECISION,)),
        (RoleName.BEAR, (RoleName.RISK_DECISION,)),
        (RoleName.RISK_DECISION, ()),
    ],
)
def test_each_role_failure_preserves_independent_successes_and_blocks_dependents(
    tmp_path,
    failed_role: RoleName,
    expected_blocked: tuple[RoleName, ...],
) -> None:
    url = f"sqlite:///{tmp_path / f'runner-failure-{failed_role.value}.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, f"worker-{failed_role.value}")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider({failed_role: [ModelAuthenticationError("secret")]}),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run(
            claim=claim,
            run_id=pending.id,
            snapshot=frozen_snapshot(),
            evidence_graph=evidence_graph(frozen_snapshot()),
        )
    )

    assert result.run.status is AnalysisRunStatus.PARTIAL
    assert result.report.failed_modules == (failed_role,)
    assert result.report.blocked_modules == expected_blocked
    assert result.report.rating is None


def test_runner_executes_independent_role_waves_concurrently(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'runner-concurrency.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, "worker-concurrency")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    provider = ConcurrentProvider()
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run(
            claim=claim,
            run_id=pending.id,
            snapshot=frozen_snapshot(),
            evidence_graph=evidence_graph(frozen_snapshot()),
        )
    )

    assert result.run.status is AnalysisRunStatus.SUCCEEDED
    assert provider.max_active_by_wave["analyst"] == 2
    assert provider.max_active_by_wave["review"] == 2


def test_critical_untrusted_quality_stops_before_any_model_call(tmp_path) -> None:
    base = frozen_snapshot()
    sections = tuple(
        section.model_copy(update={"quality_flags": (ResearchQualityFlag.STALE,)})
        if section.kind is ResearchSectionKind.MARKET
        else section
        for section in base.sections
    )
    snapshot = ResearchSnapshot.create(
        symbol=base.symbol,
        frozen_at=base.frozen_at,
        sections=sections,
        missing_sections=base.missing_sections,
    )
    url = f"sqlite:///{tmp_path / 'runner-quality.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, "worker-quality")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol=snapshot.symbol,
        retry_policy=policy,
        now=FROZEN_AT,
    )
    provider = ScriptedProvider()
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run(
            claim=claim,
            run_id=pending.id,
            snapshot=snapshot,
            evidence_graph=evidence_graph(snapshot),
        )
    )

    assert result.run.status is AnalysisRunStatus.INSUFFICIENT_EVIDENCE
    assert result.report.status is ReportStatus.INSUFFICIENT_EVIDENCE
    assert provider.calls == Counter()


def test_missing_critical_evidence_graph_item_stops_before_model_call(tmp_path) -> None:
    snapshot = frozen_snapshot()
    full_graph = evidence_graph(snapshot)
    graph = full_graph.model_copy(
        update={
            "evidence_items": tuple(
                item
                for item in full_graph.evidence_items
                if item.section_kind is not ResearchSectionKind.MARKET
            )
        }
    )
    url = f"sqlite:///{tmp_path / 'runner-critical-graph.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, "worker-critical-graph")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol=snapshot.symbol,
        retry_policy=policy,
        now=FROZEN_AT,
    )
    provider = ScriptedProvider()
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run(
            claim=claim,
            run_id=pending.id,
            snapshot=snapshot,
            evidence_graph=graph,
        )
    )

    assert result.run.status is AnalysisRunStatus.INSUFFICIENT_EVIDENCE
    assert provider.calls == Counter()


def test_repository_rejects_corrupted_content_hashes(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'runner-corruption.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, "worker-corruption")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )
    run(
        runner.run(
            claim=claim,
            run_id=pending.id,
            snapshot=frozen_snapshot(),
            evidence_graph=evidence_graph(frozen_snapshot()),
        )
    )
    corrupted = "sha256:" + "f" * 64
    with engine.begin() as connection:
        for trigger in (
            "trg_analysis_run_immutable_update",
            "trg_analysis_run_bind_once",
            "trg_analysis_stage_immutable_update",
            "trg_analysis_stage_owner_terminal_update",
            "trg_analysis_report_immutable_update",
            "trg_analysis_report_owner_terminal_update",
        ):
            connection.execute(text(f"DROP TRIGGER {trigger}"))
        connection.execute(
            text("UPDATE analysis_run SET snapshot_hash=:hash WHERE id=:run_id"),
            {"hash": corrupted, "run_id": pending.id},
        )
        connection.execute(
            text(
                "UPDATE analysis_stage SET output_hash=:hash "
                "WHERE run_id=:run_id AND role='technical'"
            ),
            {"hash": corrupted, "run_id": pending.id},
        )
        connection.execute(
            text("UPDATE analysis_report SET report_hash=:hash WHERE run_id=:run_id"),
            {"hash": corrupted, "run_id": pending.id},
        )

    with pytest.raises(AnalysisRepositoryError):
        repository.load_inputs(pending.id)
    with pytest.raises(AnalysisRepositoryError):
        repository.get_stage_artifact(pending.id, "technical")
    with pytest.raises(AnalysisRepositoryError):
        repository.get_report(pending.id)


def test_inflight_user_cancellation_preserves_checkpoint_without_partial_report(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, "worker-cancel")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    cancellation_sent = False

    async def lease_tick(_delay: float) -> None:
        nonlocal cancellation_sent
        if not cancellation_sent:
            cancellation_sent = True
            tasks.request_cancel(claim.snapshot.id)
        await asyncio.sleep(0)

    runner = AnalysisRunner(
        repository=repository,
        provider=HangingTechnicalProvider(),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        lease_sleeper=lease_tick,
        lease_interval_seconds=0.01,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(AnalysisCancelled):
        run(
            runner.run(
                claim=claim,
                run_id=pending.id,
                snapshot=frozen_snapshot(),
                evidence_graph=evidence_graph(frozen_snapshot()),
            )
        )

    assert repository.get_run(pending.id).status is AnalysisRunStatus.CANCELLED
    assert tasks.get(claim.snapshot.id).status == "cancelled"
    assert repository.get_stage(pending.id, "fundamental_news").status is (
        AnalysisStageStatus.SUCCEEDED
    )
    assert repository.list_attempts(pending.id, "technical")[0].status.value == (
        "cancelled"
    )
    with pytest.raises(AnalysisRepositoryError):
        repository.get_report(pending.id)


def test_data_kind_retry_is_persisted_before_model_waves(tmp_path) -> None:
    snapshot = frozen_snapshot()

    def market_error() -> ResearchDataUnavailable:
        return ResearchDataUnavailable(
            kind=ResearchSectionKind.MARKET,
            reason=ResearchMissingReason.TIMEOUT,
            attempted_sources=("market_cache",),
        )

    loaders = [
        ScriptedSectionLoader(
            section,
            failures=(market_error(), market_error())
            if section.kind is ResearchSectionKind.MARKET
            else (),
        )
        for section in snapshot.sections
    ]
    data_service = ResearchDataService(loaders=loaders, clock=lambda: FROZEN_AT)
    url = f"sqlite:///{tmp_path / 'runner-data-retry.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(
        max_retries=2,
        base_delay_seconds=0.25,
        max_delay_seconds=1.0,
    )
    claim = claimed_task(tasks, "worker-data")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol=snapshot.symbol,
        retry_policy=policy,
        now=FROZEN_AT,
    )
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=policy,
        sleeper=sleeper,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run_from_data(
            claim=claim,
            run_id=pending.id,
            symbol=snapshot.symbol,
            data_service=data_service,
            evidence_factory=evidence_graph,
        )
    )

    assert result.run.status is AnalysisRunStatus.SUCCEEDED
    assert loaders[0].calls == 3
    assert delays == [0.25, 0.5]
    assert [
        item.status.value for item in repository.list_attempts(pending.id, "market")
    ] == ["failed", "failed", "succeeded"]


@pytest.mark.parametrize(
    ("kind", "reason", "max_retries", "expected_status", "model_calls"),
    [
        (
            ResearchSectionKind.FUNDAMENTALS,
            ResearchMissingReason.PERMISSION_DENIED,
            2,
            AnalysisRunStatus.INSUFFICIENT_EVIDENCE,
            0,
        ),
        (
            ResearchSectionKind.MARKET,
            ResearchMissingReason.TIMEOUT,
            2,
            AnalysisRunStatus.INSUFFICIENT_EVIDENCE,
            0,
        ),
        (
            ResearchSectionKind.NEWS,
            ResearchMissingReason.TIMEOUT,
            1,
            AnalysisRunStatus.SUCCEEDED,
            5,
        ),
    ],
)
def test_data_failure_retry_policy_and_evidence_gate(
    tmp_path,
    kind: ResearchSectionKind,
    reason: ResearchMissingReason,
    max_retries: int,
    expected_status: AnalysisRunStatus,
    model_calls: int,
) -> None:
    snapshot = frozen_snapshot()
    loaders = []
    for section in snapshot.sections:
        failures = ()
        if section.kind is kind:
            failure_count = (
                max_retries + 1 if reason is ResearchMissingReason.TIMEOUT else 1
            )
            failures = tuple(
                ResearchDataUnavailable(
                    kind=kind,
                    reason=reason,
                    attempted_sources=("fixture",),
                )
                for _ in range(failure_count)
            )
        loaders.append(ScriptedSectionLoader(section, failures=failures))
    data_service = ResearchDataService(loaders=loaders, clock=lambda: FROZEN_AT)
    url = f"sqlite:///{tmp_path / f'runner-data-{kind.value}-{reason.value}.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(
        max_retries=max_retries,
        base_delay_seconds=0.01,
        max_delay_seconds=0.02,
    )
    claim = claimed_task(tasks, f"worker-data-{kind.value}")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol=snapshot.symbol,
        retry_policy=policy,
        now=FROZEN_AT,
    )
    provider = ScriptedProvider()
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run_from_data(
            claim=claim,
            run_id=pending.id,
            symbol=snapshot.symbol,
            data_service=data_service,
            evidence_factory=evidence_graph,
        )
    )

    assert result.run.status is expected_status
    assert sum(provider.calls.values()) == model_calls
    attempts = repository.list_attempts(pending.id, kind.value)
    assert len(attempts) == (
        max_retries + 1 if reason is ResearchMissingReason.TIMEOUT else 1
    )
    if kind is ResearchSectionKind.NEWS:
        assert result.report.status is ReportStatus.COMPLETE
        assert result.report.missing_sections == (ResearchSectionKind.NEWS,)
        persisted, _graph = repository.load_inputs(pending.id)
        assert persisted.missing_sections[0].attempted_sources == ("fixture",)
        assert persisted.missing_sections[0].checked_at == FROZEN_AT


def test_cancel_wins_atomically_when_requested_before_report_finalization(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-finalize-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = CancelBeforeFinalizeRepository(engine, tasks)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, "worker-finalize-cancel")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(AnalysisCancelled):
        run(
            runner.run(
                claim=claim,
                run_id=pending.id,
                snapshot=frozen_snapshot(),
                evidence_graph=evidence_graph(frozen_snapshot()),
            )
        )

    assert repository.get_run(pending.id).status is AnalysisRunStatus.CANCELLED
    assert tasks.get(claim.snapshot.id).status == "cancelled"
    assert all(
        repository.get_stage(pending.id, role.value).status
        is AnalysisStageStatus.SUCCEEDED
        for role in RoleName
    )
    with pytest.raises(AnalysisRepositoryError):
        repository.get_report(pending.id)


def test_pre_attempt_cancel_stops_without_waiting_for_heartbeat_tick(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'runner-immediate-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    claim = claimed_task(tasks, "worker-immediate-cancel")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    tasks.request_cancel(claim.snapshot.id)
    provider = ScriptedProvider()

    async def forbidden_tick(_delay: float) -> None:
        raise AssertionError("immediate cancellation must not wait for a lease tick")

    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        lease_sleeper=forbidden_tick,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(AnalysisCancelled):
        run(
            runner.run(
                claim=claim,
                run_id=pending.id,
                snapshot=frozen_snapshot(),
                evidence_graph=evidence_graph(frozen_snapshot()),
            )
        )

    assert provider.calls == Counter()
    assert repository.get_run(pending.id).status is AnalysisRunStatus.CANCELLED
    assert repository.list_attempts(pending.id, "technical") == ()


def test_expired_reclaim_interrupts_uncheckpointed_attempt_and_skips_success(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-reclaim.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = CrashBeforeFirstCheckpointRepository(engine)
    policy = RetryPolicy(max_retries=1)
    task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    first_claim = tasks.claim_next(
        "worker-crash",
        now=FROZEN_AT,
        lease_duration=timedelta(seconds=1),
    )
    assert isinstance(first_claim, TaskClaim)
    provider = ScriptedProvider()
    first_runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        run(
            first_runner.run(
                claim=first_claim,
                run_id=pending.id,
                snapshot=frozen_snapshot(),
                evidence_graph=evidence_graph(frozen_snapshot()),
            )
        )

    reclaimed_at = FROZEN_AT + timedelta(seconds=2)
    second_claim = tasks.claim_next(
        "worker-reclaim",
        now=reclaimed_at,
        lease_duration=timedelta(minutes=5),
    )
    assert isinstance(second_claim, TaskClaim)
    second_runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: reclaimed_at,
        monotonic=lambda: 2.0,
    )

    result = run(
        second_runner.run(
            claim=second_claim,
            run_id=pending.id,
            snapshot=frozen_snapshot(),
            evidence_graph=evidence_graph(frozen_snapshot()),
        )
    )

    assert result.run.status is AnalysisRunStatus.SUCCEEDED
    assert provider.calls[RoleName.TECHNICAL] == 2
    assert provider.calls[RoleName.FUNDAMENTAL_NEWS] == 1
    assert [
        item.status.value for item in repository.list_attempts(pending.id, "technical")
    ] == ["interrupted", "succeeded"]
    assert repository.get_stage(pending.id, "technical").attempt_count == 2


def test_reclaim_uses_persisted_attempt_count_as_lifetime_retry_budget(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-retry-budget.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(
        max_retries=1,
        base_delay_seconds=0.01,
        max_delay_seconds=0.01,
    )
    task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    first = tasks.claim_next(
        "budget-first", now=FROZEN_AT, lease_duration=timedelta(seconds=1)
    )
    assert isinstance(first, TaskClaim)
    repository.start_run(first, pending.id, now=FROZEN_AT)
    snapshot = frozen_snapshot()
    repository.bind_inputs(
        first,
        pending.id,
        snapshot,
        evidence_graph(snapshot),
        now=FROZEN_AT,
    )
    repository.start_attempt(
        first,
        pending.id,
        RoleName.TECHNICAL.value,
        provider="openai_compatible",
        model="vendor-chat",
        request_hash="sha256:" + "a" * 64,
        now=FROZEN_AT,
    )
    reclaimed_at = FROZEN_AT + timedelta(seconds=2)
    second = tasks.claim_next("budget-second", now=reclaimed_at)
    assert isinstance(second, TaskClaim)
    provider = ScriptedProvider(
        {
            RoleName.TECHNICAL: [
                ModelRateLimitError("secret-1"),
                ModelRateLimitError("secret-2"),
            ]
        }
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: reclaimed_at,
        monotonic=lambda: 2.0,
    )

    result = run(
        runner.run(
            claim=second,
            run_id=pending.id,
            snapshot=snapshot,
            evidence_graph=evidence_graph(snapshot),
        )
    )

    assert result.run.status is AnalysisRunStatus.PARTIAL
    assert provider.calls[RoleName.TECHNICAL] == 1
    assert [
        item.status.value for item in repository.list_attempts(pending.id, "technical")
    ] == ["interrupted", "failed"]


def test_reclaim_exhausts_data_stage_without_mutating_interrupted_attempt(
    tmp_path,
) -> None:
    snapshot = frozen_snapshot()
    url = f"sqlite:///{tmp_path / 'runner-data-reclaim-budget.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    enqueued = repository.enqueue_run(
        symbol=snapshot.symbol,
        retry_policy=policy,
        now=FROZEN_AT,
    )
    first = tasks.claim_next(
        "data-budget-first",
        now=FROZEN_AT,
        lease_duration=timedelta(seconds=1),
    )
    assert isinstance(first, TaskClaim)
    repository.start_run(first, enqueued.run.id, now=FROZEN_AT)
    repository.start_attempt(
        first,
        enqueued.run.id,
        ResearchSectionKind.MARKET.value,
        provider=None,
        model=None,
        request_hash=None,
        now=FROZEN_AT,
    )
    reclaimed_at = FROZEN_AT + timedelta(seconds=2)
    second = tasks.claim_next("data-budget-second", now=reclaimed_at)
    assert isinstance(second, TaskClaim)
    market_loader = ScriptedSectionLoader(snapshot.sections[0])
    service = ResearchDataService(
        loaders=(
            market_loader,
            *(ScriptedSectionLoader(section) for section in snapshot.sections[1:]),
        ),
        clock=lambda: reclaimed_at,
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: reclaimed_at,
        monotonic=lambda: 2.0,
    )

    result = run(
        runner.run_from_data(
            claim=second,
            run_id=enqueued.run.id,
            symbol=snapshot.symbol,
            data_service=service,
            evidence_factory=evidence_graph,
        )
    )

    assert result.run.status is AnalysisRunStatus.INSUFFICIENT_EVIDENCE
    assert market_loader.calls == 0
    attempts = repository.list_attempts(enqueued.run.id, "market")
    assert [attempt.status.value for attempt in attempts] == ["interrupted"]
    assert repository.get_stage(enqueued.run.id, "market").failure_code == (
        "retry_budget_exhausted"
    )
    missing = repository.get_missing_section(enqueued.run.id, "market")
    assert missing.kind is ResearchSectionKind.MARKET
    assert missing.checked_at == reclaimed_at


def test_only_one_child_retry_can_be_active_and_parent_remains_byte_stable(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-child-concurrency.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    parent_claim = claimed_task(tasks, "worker-parent-concurrent")
    parent = repository._create_run_for_existing_task(
        task_id=parent_claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(
            {RoleName.BULL: [ModelAuthenticationError("secret")]}
        ),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )
    run(
        runner.run(
            claim=parent_claim,
            run_id=parent.id,
            snapshot=frozen_snapshot(),
            evidence_graph=evidence_graph(frozen_snapshot()),
        )
    )
    before = (
        repository.get_run(parent.id),
        repository.list_stages(parent.id),
        tuple(repository.list_attempts(parent.id, role.value) for role in RoleName),
        repository.get_report(parent.id).canonical_json_bytes(),
    )
    first_child = repository.enqueue_retry(
        parent.id,
        RoleName.BULL.value,
        now=FROZEN_AT,
    )

    with pytest.raises(AnalysisConflict, match="already active"):
        repository.enqueue_retry(
            parent.id,
            RoleName.BULL.value,
            now=FROZEN_AT,
        )

    assert first_child.run.status is AnalysisRunStatus.QUEUED
    assert len(tasks.list_recent()) == 2
    assert (
        repository.get_run(parent.id),
        repository.list_stages(parent.id),
        tuple(repository.list_attempts(parent.id, role.value) for role in RoleName),
        repository.get_report(parent.id).canonical_json_bytes(),
    ) == before


def test_cancel_during_backoff_preserves_sibling_checkpoint_and_starts_no_retry(
    tmp_path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runner-backoff-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(
        max_retries=2,
        base_delay_seconds=0.25,
        max_delay_seconds=1.0,
    )
    claim = claimed_task(tasks, "worker-backoff-cancel")
    pending = repository._create_run_for_existing_task(
        task_id=claim.snapshot.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    provider = YieldingTechnicalProvider(
        {RoleName.TECHNICAL: [ModelRateLimitError("secret")]}
    )

    async def cancelling_backoff(_delay: float) -> None:
        tasks.request_cancel(claim.snapshot.id)
        assert tasks.get(claim.snapshot.id).cancel_requested is True
        await asyncio.sleep(0)

    runner = AnalysisRunner(
        repository=repository,
        provider=provider,
        retry_policy=policy,
        sleeper=cancelling_backoff,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )

    with pytest.raises(AnalysisCancelled):
        run(
            runner.run(
                claim=claim,
                run_id=pending.id,
                snapshot=frozen_snapshot(),
                evidence_graph=evidence_graph(frozen_snapshot()),
            )
        )

    assert provider.calls[RoleName.TECHNICAL] == 1
    assert provider.calls[RoleName.FUNDAMENTAL_NEWS] == 1
    assert repository.get_stage(pending.id, "fundamental_news").status is (
        AnalysisStageStatus.SUCCEEDED
    )
    assert [
        item.status.value for item in repository.list_attempts(pending.id, "technical")
    ] == ["failed"]
    assert repository.get_run(pending.id).status is AnalysisRunStatus.CANCELLED


def test_long_provider_call_heartbeats_before_lease_can_be_reclaimed(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'runner-heartbeat.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=0)
    task = tasks.create("analysis.run", {"symbol": "600000.SH"})
    pending = repository._create_run_for_existing_task(
        task_id=task.id,
        symbol="600000.SH",
        retry_policy=policy,
        now=FROZEN_AT,
    )
    claim = tasks.claim_next(
        "worker-heartbeat",
        now=FROZEN_AT,
        lease_duration=timedelta(seconds=2),
    )
    assert isinstance(claim, TaskClaim)
    sampled_at = [FROZEN_AT]
    release = asyncio.Event()
    reclaim_results = []
    ticks = 0

    async def lease_tick(_delay: float) -> None:
        nonlocal ticks
        await asyncio.sleep(0)
        ticks += 1
        sampled_at[0] += timedelta(seconds=1)
        reclaim_results.append(tasks.claim_next("thief", now=sampled_at[0]))
        if ticks >= 2:
            release.set()

    runner = AnalysisRunner(
        repository=repository,
        provider=LongTechnicalProvider(release),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        lease_sleeper=lease_tick,
        lease_interval_seconds=0.01,
        lease_duration=timedelta(seconds=10),
        clock=lambda: sampled_at[0],
        monotonic=lambda: 1.0,
    )

    result = run(
        runner.run(
            claim=claim,
            run_id=pending.id,
            snapshot=frozen_snapshot(),
            evidence_graph=evidence_graph(frozen_snapshot()),
        )
    )

    assert result.run.status is AnalysisRunStatus.SUCCEEDED
    assert ticks >= 2
    assert reclaim_results[:2] == [None, None]


def test_cancelled_sync_data_loader_is_abandoned_without_blocking_worker(
    tmp_path,
) -> None:
    snapshot = frozen_snapshot()
    started = threading.Event()
    release = threading.Event()
    loaders = [
        BlockingSectionLoader(section, started, release)
        if section.kind is ResearchSectionKind.MARKET
        else ScriptedSectionLoader(section)
        for section in snapshot.sections
    ]
    service = ResearchDataService(loaders=loaders, clock=lambda: FROZEN_AT)
    url = f"sqlite:///{tmp_path / 'runner-data-cancel.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    enqueued = repository.enqueue_run(
        symbol=snapshot.symbol,
        retry_policy=RetryPolicy(max_retries=0),
        now=FROZEN_AT,
    )
    claim = tasks.claim_next("data-cancel-worker", now=FROZEN_AT)
    assert isinstance(claim, TaskClaim)

    async def cancel_tick(_delay: float) -> None:
        while not started.is_set():
            await asyncio.sleep(0)
        tasks.request_cancel(claim.snapshot.id)

    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=RetryPolicy(max_retries=0),
        sleeper=lambda _delay: asyncio.sleep(0),
        lease_sleeper=cancel_tick,
        lease_interval_seconds=0.01,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )
    began = time.monotonic()
    try:
        with pytest.raises(AnalysisCancelled):
            run(
                runner.run_from_data(
                    claim=claim,
                    run_id=enqueued.run.id,
                    symbol=snapshot.symbol,
                    data_service=service,
                    evidence_factory=evidence_graph,
                )
            )
    finally:
        release.set()

    assert time.monotonic() - began < 1.0
    assert tasks.get(enqueued.task.id).status == "cancelled"
    assert repository.get_run(enqueued.run.id).status is AnalysisRunStatus.CANCELLED


def test_provider_reported_data_timeout_is_retried_by_policy(
    tmp_path,
) -> None:
    snapshot = frozen_snapshot()
    market_loader = TimeoutThenSuccessSectionLoader(snapshot.sections[0])
    loaders = [
        market_loader
        if section.kind is ResearchSectionKind.MARKET
        else ScriptedSectionLoader(section)
        for section in snapshot.sections
    ]
    service = ResearchDataService(loaders=loaders, clock=lambda: FROZEN_AT)
    url = f"sqlite:///{tmp_path / 'runner-data-timeout-retry.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=1)
    enqueued = repository.enqueue_run(
        symbol=snapshot.symbol,
        retry_policy=policy,
        now=FROZEN_AT,
    )
    claim = tasks.claim_next("data-timeout-worker", now=FROZEN_AT)
    assert isinstance(claim, TaskClaim)
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        data_timeout_seconds=0.05,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )
    result = run(
        runner.run_from_data(
            claim=claim,
            run_id=enqueued.run.id,
            symbol=snapshot.symbol,
            data_service=service,
            evidence_factory=evidence_graph,
        )
    )

    assert result.run.status is AnalysisRunStatus.SUCCEEDED
    assert market_loader.calls == 2
    attempts = repository.list_attempts(enqueued.run.id, "market")
    assert [attempt.status.value for attempt in attempts] == ["failed", "succeeded"]
    assert attempts[0].safe_error is not None
    assert attempts[0].safe_error["code"] == "data_timeout"


def test_worker_data_deadline_does_not_fake_retry_or_starve_other_loaders(
    tmp_path,
) -> None:
    snapshot = frozen_snapshot()
    started = threading.Event()
    release = threading.Event()
    market_loader = BlockingSectionLoader(snapshot.sections[0], started, release)
    other_loaders = [
        ScriptedSectionLoader(section)
        for section in snapshot.sections
        if section.kind is not ResearchSectionKind.MARKET
    ]
    service = ResearchDataService(
        loaders=(market_loader, *other_loaders),
        clock=lambda: FROZEN_AT,
    )
    url = f"sqlite:///{tmp_path / 'runner-hard-data-deadline.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    policy = RetryPolicy(max_retries=2)
    enqueued = repository.enqueue_run(
        symbol=snapshot.symbol,
        retry_policy=policy,
        now=FROZEN_AT,
    )
    claim = tasks.claim_next("hard-deadline-worker", now=FROZEN_AT)
    assert isinstance(claim, TaskClaim)
    runner = AnalysisRunner(
        repository=repository,
        provider=ScriptedProvider(),
        retry_policy=policy,
        sleeper=lambda _delay: asyncio.sleep(0),
        data_timeout_seconds=0.02,
        clock=lambda: FROZEN_AT,
        monotonic=lambda: 1.0,
    )
    abandoned: list[threading.Thread] = []
    try:
        result = run(
            runner.run_from_data(
                claim=claim,
                run_id=enqueued.run.id,
                symbol=snapshot.symbol,
                data_service=service,
                evidence_factory=evidence_graph,
            )
        )
        abandoned = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("analysis-data-market")
        ]
        assert abandoned
        assert all(thread.daemon for thread in abandoned)
    finally:
        release.set()
        for thread in abandoned:
            thread.join(timeout=1.0)

    assert started.is_set()
    assert result.run.status is AnalysisRunStatus.INSUFFICIENT_EVIDENCE
    assert market_loader.calls == 1
    assert [loader.calls for loader in other_loaders] == [1, 1, 1]
    attempts = repository.list_attempts(enqueued.run.id, "market")
    assert len(attempts) == 1
    assert attempts[0].safe_error is not None
    assert attempts[0].safe_error["code"] == "worker_data_deadline"
    missing = repository.get_missing_section(enqueued.run.id, "market")
    assert missing.reason is ResearchMissingReason.TIMEOUT
    assert missing.checked_at == FROZEN_AT


def test_abandoned_data_workers_are_process_bounded(tmp_path, monkeypatch) -> None:
    snapshot = frozen_snapshot()
    releases: list[threading.Event] = []
    started: list[threading.Event] = []
    engines = []
    try:
        for index in range(_MAX_DATA_WORKER_THREADS + 2):
            market_started = threading.Event()
            market_release = threading.Event()
            started.append(market_started)
            releases.append(market_release)
            service = ResearchDataService(
                loaders=(
                    BlockingSectionLoader(
                        snapshot.sections[0], market_started, market_release
                    ),
                    *(ScriptedSectionLoader(item) for item in snapshot.sections[1:]),
                ),
                clock=lambda: FROZEN_AT,
            )
            url = f"sqlite:///{tmp_path / f'bounded-data-worker-{index}.db'}"
            migrate(url)
            engine = create_engine_for_url(url)
            engines.append(engine)
            tasks = TaskRepository(engine)
            repository = AnalysisRepository(engine)
            policy = RetryPolicy(max_retries=0)
            enqueued = repository.enqueue_run(
                symbol=snapshot.symbol,
                retry_policy=policy,
                now=FROZEN_AT,
            )
            claim = tasks.claim_next(f"bounded-worker-{index}", now=FROZEN_AT)
            assert isinstance(claim, TaskClaim)
            runner = AnalysisRunner(
                repository=repository,
                provider=ScriptedProvider(),
                retry_policy=policy,
                sleeper=lambda _delay: asyncio.sleep(0),
                data_timeout_seconds=0.01,
                clock=lambda: FROZEN_AT,
                monotonic=lambda: 1.0,
            )
            operation = runner.run_from_data(
                claim=claim,
                run_id=enqueued.run.id,
                symbol=snapshot.symbol,
                data_service=service,
                evidence_factory=evidence_graph,
            )
            if index < _MAX_DATA_WORKER_THREADS:
                run(operation)
            else:
                with pytest.raises(runner_module.AnalysisWorkerRestartRequired):
                    run(operation)
                assert tasks.get(claim.snapshot.id).status == "running"
                assert repository.get_run(enqueued.run.id).status is (
                    AnalysisRunStatus.RUNNING
                )

        active = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("analysis-data-")
        ]
        assert len(active) <= _MAX_DATA_WORKER_THREADS
        assert all(thread.daemon for thread in active)
        assert sum(event.is_set() for event in started) == _MAX_DATA_WORKER_THREADS

        monkeypatch.setattr(
            runner_module,
            "_DATA_WORKER_CAPACITY",
            runner_module._DataWorkerCapacity(_MAX_DATA_WORKER_THREADS),
        )
        reset_url = f"sqlite:///{tmp_path / 'reset-data-worker.db'}"
        migrate(reset_url)
        reset_engine = create_engine_for_url(reset_url)
        engines.append(reset_engine)
        reset_tasks = TaskRepository(reset_engine)
        reset_repository = AnalysisRepository(reset_engine)
        reset_enqueued = reset_repository.enqueue_run(
            symbol=snapshot.symbol,
            retry_policy=RetryPolicy(max_retries=0),
            now=FROZEN_AT,
        )
        reset_claim = reset_tasks.claim_next("reset-worker", now=FROZEN_AT)
        assert isinstance(reset_claim, TaskClaim)
        reset_result = run(
            AnalysisRunner(
                repository=reset_repository,
                provider=ScriptedProvider(),
                retry_policy=RetryPolicy(max_retries=0),
                sleeper=lambda _delay: asyncio.sleep(0),
                data_timeout_seconds=0.01,
                clock=lambda: FROZEN_AT,
                monotonic=lambda: 1.0,
            ).run_from_data(
                claim=reset_claim,
                run_id=reset_enqueued.run.id,
                symbol=snapshot.symbol,
                data_service=ResearchDataService(
                    loaders=tuple(
                        ScriptedSectionLoader(item) for item in snapshot.sections
                    ),
                    clock=lambda: FROZEN_AT,
                ),
                evidence_factory=evidence_graph,
            )
        )
        assert reset_result.run.status is AnalysisRunStatus.SUCCEEDED
    finally:
        for release in releases:
            release.set()
        for engine in engines:
            engine.dispose()


def test_restart_required_is_a_process_boundary_not_an_exception() -> None:
    restart = getattr(runner_module, "AnalysisWorkerRestartRequired", None)

    assert isinstance(restart, type)
    assert issubclass(restart, BaseException)
    assert not issubclass(restart, Exception)
