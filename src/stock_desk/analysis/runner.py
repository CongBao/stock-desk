from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
import threading
import time
from typing import cast

from stock_desk.analysis.evidence import EvidenceGraph, critical_evidence_eligible
from stock_desk.analysis.data_service import (
    ResearchDataService,
    ResearchDataUnavailable,
)
from stock_desk.analysis.providers.base import ModelProvider
from stock_desk.analysis.report import (
    ReportStatus,
    ResearchReport,
    ResearchReportBuilder,
    StageFailure,
)
from stock_desk.analysis.repository import (
    AnalysisRepository,
    AnalysisRunSnapshot,
    AnalysisRunStatus,
    AnalysisStageStatus,
)
from stock_desk.analysis.retry import RetryDecision, RetryPolicy, classify_retry
from stock_desk.analysis.roles import ROLE_ORDER, RoleName, RoleOutput
from stock_desk.analysis.snapshot import (
    MissingResearchSection,
    RESEARCH_SECTION_ORDER,
    ResearchMissingReason,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.analysis.workflow import (
    AnalysisWorkflow,
    WorkflowResult,
    WorkflowStageTrace,
)
from stock_desk.tasks.models import TaskClaim


@dataclass(frozen=True, slots=True)
class AnalysisRunnerResult:
    run: AnalysisRunSnapshot
    report: ResearchReport


class AnalysisCancelled(asyncio.CancelledError):
    pass


class AnalysisWorkerRestartRequired(BaseException):
    """The process data-worker budget is poisoned and must be recreated."""

    pass


_DEPENDENCIES = {
    RoleName.TECHNICAL: (),
    RoleName.FUNDAMENTAL_NEWS: (),
    RoleName.BULL: (RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS),
    RoleName.BEAR: (RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS),
    RoleName.RISK_DECISION: (RoleName.BULL, RoleName.BEAR),
}
_ROLE_WAVES = (
    (RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS),
    (RoleName.BULL, RoleName.BEAR),
    (RoleName.RISK_DECISION,),
)
_MAX_DATA_WORKER_THREADS = 4
_DATA_WORKER_SEQUENCE = count(1)


class _DataWorkerCapacityExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class _DataWorkerLease:
    timed_out: bool = False
    released: bool = False


class _DataWorkerCapacity:
    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._slots = threading.BoundedSemaphore(maximum)
        self._lock = threading.Lock()
        self._poisoned = 0

    @property
    def restart_required(self) -> bool:
        with self._lock:
            return self._poisoned >= self._maximum

    def acquire(self, *, restart_on_poisoned: bool) -> _DataWorkerLease:
        if self._slots.acquire(blocking=False):
            return _DataWorkerLease()
        with self._lock:
            poisoned = self._poisoned
        if restart_on_poisoned and poisoned >= self._maximum:
            raise AnalysisWorkerRestartRequired()
        raise _DataWorkerCapacityExceeded("analysis data worker capacity is exhausted")

    def mark_timeout(self, lease: _DataWorkerLease) -> None:
        with self._lock:
            if not lease.released and not lease.timed_out:
                lease.timed_out = True
                self._poisoned += 1

    def release(self, lease: _DataWorkerLease) -> None:
        with self._lock:
            if lease.released:
                return
            lease.released = True
            if lease.timed_out:
                self._poisoned -= 1
        self._slots.release()


_DATA_WORKER_CAPACITY = _DataWorkerCapacity(_MAX_DATA_WORKER_THREADS)


class AnalysisRunner:
    def __init__(
        self,
        *,
        repository: AnalysisRepository,
        provider: ModelProvider,
        retry_policy: RetryPolicy,
        sleeper: Callable[[float], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        lease_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_interval_seconds: float = 10.0,
        lease_duration: timedelta = timedelta(seconds=30),
        data_timeout_seconds: float = 90.0,
        temperature: float = 0.1,
        model_timeout_seconds: float = 90.0,
        max_output_tokens: int = 4_096,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._retry_policy = retry_policy
        self._sleeper = sleeper
        self._clock = clock
        self._lease_sleeper = lease_sleeper
        self._lease_interval_seconds = lease_interval_seconds
        self._lease_duration = lease_duration
        self._data_timeout_seconds = data_timeout_seconds
        self._restart_on_data_capacity = _DATA_WORKER_CAPACITY.restart_required
        self._workflow = AnalysisWorkflow(
            provider=provider,
            clock=clock,
            monotonic=monotonic,
            temperature=temperature,
            timeout_seconds=model_timeout_seconds,
            max_output_tokens=max_output_tokens,
        )
        self._reports = ResearchReportBuilder()

    async def run(
        self,
        *,
        claim: TaskClaim,
        run_id: str,
        snapshot: ResearchSnapshot,
        evidence_graph: EvidenceGraph,
        _resume_reclaimed: bool = True,
    ) -> AnalysisRunnerResult:
        self._validate_retry_policy(run_id)
        current = self._repository.get_run(run_id)
        if current.status is AnalysisRunStatus.QUEUED:
            current = self._repository.start_run(claim, run_id, now=self._clock())
        elif (
            current.status is AnalysisRunStatus.RUNNING
            and claim.attempt_count > 1
            and _resume_reclaimed
        ):
            current = self._repository.resume_run(claim, run_id, now=self._clock())
        if current.status is not AnalysisRunStatus.RUNNING:
            raise ValueError("analysis run is not executable")
        self._cancel_if_requested(claim, run_id)
        if current.snapshot_id is None:
            self._repository.bind_inputs(
                claim,
                run_id,
                snapshot,
                evidence_graph,
                now=self._clock(),
            )

        if not critical_evidence_eligible(snapshot, evidence_graph):
            for role in ROLE_ORDER:
                stage = self._repository.get_stage(run_id, role.value)
                if stage.status is AnalysisStageStatus.PENDING:
                    self._repository.block_stage(
                        claim,
                        run_id,
                        role.value,
                        failure_code="insufficient_evidence",
                        now=self._clock(),
                    )
            report = self._reports.build_insufficient(
                snapshot=snapshot,
                evidence_graph=evidence_graph,
            )
            finished = self._repository.finalize_run(
                claim,
                run_id,
                AnalysisRunStatus.INSUFFICIENT_EVIDENCE,
                report,
                now=self._clock(),
            )
            return AnalysisRunnerResult(run=finished, report=report)

        completed: dict[RoleName, tuple[RoleOutput, WorkflowStageTrace]] = {}
        for wave in _ROLE_WAVES:
            self._cancel_if_requested(claim, run_id)
            operations: list[
                tuple[
                    RoleName,
                    Awaitable[tuple[RoleOutput, WorkflowStageTrace] | None],
                ]
            ] = []
            for role in wave:
                stage = self._repository.get_stage(run_id, role.value)
                if stage.status in {
                    AnalysisStageStatus.SUCCEEDED,
                    AnalysisStageStatus.REUSED,
                }:
                    artifact = self._repository.get_stage_artifact(run_id, role.value)
                    completed[role] = (artifact.output, artifact.trace)
                    continue
                dependency_roles = _DEPENDENCIES[role]
                if any(dependency not in completed for dependency in dependency_roles):
                    self._repository.block_stage(
                        claim,
                        run_id,
                        role.value,
                        failure_code="dependency_failed",
                        now=self._clock(),
                    )
                    continue
                dependencies = tuple(completed[item][0] for item in dependency_roles)
                operations.append(
                    (
                        role,
                        self._execute_role(
                            claim=claim,
                            run_id=run_id,
                            role=role,
                            snapshot=snapshot,
                            evidence_graph=evidence_graph,
                            dependencies=dependencies,
                        ),
                    )
                )
            results = await self._await_with_lease(
                asyncio.gather(
                    *(operation for _, operation in operations),
                    return_exceptions=True,
                ),
                claim=claim,
                run_id=run_id,
            )
            first_system_error: BaseException | None = None
            for (role, _), result in zip(operations, results, strict=True):
                if isinstance(result, BaseException):
                    first_system_error = first_system_error or result
                elif result is not None:
                    completed[role] = result
            if first_system_error is not None:
                user_cancelled = isinstance(
                    first_system_error, asyncio.CancelledError
                ) and self._repository.cancellation_requested(claim)
                if isinstance(first_system_error, AnalysisCancelled) or user_cancelled:
                    if (
                        self._repository.get_run(run_id).status
                        is AnalysisRunStatus.RUNNING
                    ):
                        self._repository.cancel_run(
                            claim,
                            run_id,
                            now=self._clock(),
                        )
                    raise AnalysisCancelled()
                raise first_system_error

        ordered_roles = tuple(role for role in ROLE_ORDER if role in completed)
        outputs = tuple(completed[role][0] for role in ordered_roles)
        trace = tuple(completed[role][1] for role in ordered_roles)
        failed_stages = tuple(
            item
            for item in self._repository.list_stages(run_id)
            if item.status is AnalysisStageStatus.FAILED
            and item.role in {role.value for role in ROLE_ORDER}
        )
        if failed_stages:
            report = self._reports.build_partial(
                snapshot=snapshot,
                evidence_graph=evidence_graph,
                outputs=outputs,
                trace=trace,
                failures=tuple(
                    StageFailure(
                        stage=RoleName(item.role),
                        code=item.failure_code or "validation_failure",
                        attempt_count=item.attempt_count,
                    )
                    for item in failed_stages
                ),
            )
            run_status = AnalysisRunStatus.PARTIAL
        else:
            workflow = WorkflowResult(
                snapshot_id=snapshot.snapshot_id,
                outputs=outputs,
                trace=trace,
                evidence_ids=tuple(
                    item.evidence_id for item in evidence_graph.evidence_items
                ),
            )
            report = self._reports.build(
                snapshot=snapshot,
                evidence_graph=evidence_graph,
                workflow=workflow,
            )
            run_status = (
                AnalysisRunStatus.SUCCEEDED
                if report.status is ReportStatus.COMPLETE
                else AnalysisRunStatus.INSUFFICIENT_EVIDENCE
            )
        finished = self._repository.finalize_run(
            claim,
            run_id,
            run_status,
            report,
            now=self._clock(),
        )
        if finished.status is AnalysisRunStatus.CANCELLED:
            raise AnalysisCancelled()
        return AnalysisRunnerResult(run=finished, report=report)

    async def run_from_data(
        self,
        *,
        claim: TaskClaim,
        run_id: str,
        symbol: str,
        data_service: ResearchDataService,
        evidence_factory: Callable[[ResearchSnapshot], EvidenceGraph],
    ) -> AnalysisRunnerResult:
        self._validate_retry_policy(run_id)
        current = self._repository.get_run(run_id)
        if current.status is AnalysisRunStatus.QUEUED:
            current = self._repository.start_run(claim, run_id, now=self._clock())
        elif current.status is AnalysisRunStatus.RUNNING and claim.attempt_count > 1:
            current = self._repository.resume_run(claim, run_id, now=self._clock())
        if current.status is not AnalysisRunStatus.RUNNING or current.snapshot_id:
            raise ValueError("analysis data acquisition run is not executable")
        sections, missing = await self._acquire_data_sections(
            claim=claim,
            run_id=run_id,
            symbol=symbol,
            data_service=data_service,
        )
        snapshot = ResearchSnapshot.create(
            symbol=symbol,
            frozen_at=self._clock(),
            sections=tuple(sections),
            missing_sections=tuple(missing),
        )
        graph = evidence_factory(snapshot)
        return await self.run(
            claim=claim,
            run_id=run_id,
            snapshot=snapshot,
            evidence_graph=graph,
            _resume_reclaimed=False,
        )

    async def _acquire_data_sections(
        self,
        *,
        claim: TaskClaim,
        run_id: str,
        symbol: str,
        data_service: ResearchDataService,
    ) -> tuple[list[ResearchSection], list[MissingResearchSection]]:
        sections: list[ResearchSection] = []
        missing: list[MissingResearchSection] = []
        stages = {item.role: item for item in self._repository.list_stages(run_id)}
        for kind in RESEARCH_SECTION_ORDER:
            self._cancel_if_requested(claim, run_id)
            stage = stages[kind.value]
            if stage.status is AnalysisStageStatus.SUCCEEDED:
                sections.append(self._repository.get_data_section(run_id, kind.value))
                continue
            if stage.status is AnalysisStageStatus.FAILED:
                missing.append(self._repository.get_missing_section(run_id, kind.value))
                continue
            if stage.status is not AnalysisStageStatus.PENDING:
                raise ValueError("analysis data stage is not executable")
            first_attempt = stage.attempt_count + 1
            if first_attempt > self._retry_policy.max_attempts:
                exhausted_checkpoint = data_service.missing_from_error(
                    ResearchDataUnavailable(
                        kind=kind,
                        reason=ResearchMissingReason.INVALID_RESPONSE,
                        attempted_sources=(),
                    )
                )
                self._repository.exhaust_stage(
                    claim,
                    run_id,
                    kind.value,
                    failure_code="retry_budget_exhausted",
                    missing_section=exhausted_checkpoint,
                    now=self._clock(),
                )
                missing.append(exhausted_checkpoint)
                continue
            for attempt_no in range(first_attempt, self._retry_policy.max_attempts + 1):
                self._cancel_if_requested(claim, run_id)
                if attempt_no > 1:
                    await self._await_with_lease(
                        self._sleeper(
                            self._retry_policy.delay_before_attempt(attempt_no)
                        ),
                        claim=claim,
                        run_id=run_id,
                    )
                    self._cancel_if_requested(claim, run_id)
                attempt = self._repository.start_attempt(
                    claim,
                    run_id,
                    kind.value,
                    provider=None,
                    model=None,
                    request_hash=None,
                    now=self._clock(),
                )
                failure: ResearchDataUnavailable | None = None
                worker_failure_code: str | None = None
                try:
                    section = await self._await_with_lease(
                        self._load_data_kind(
                            data_service=data_service,
                            symbol=symbol,
                            kind=kind,
                        ),
                        claim=claim,
                        run_id=run_id,
                    )
                    if section.kind is not kind:
                        raise ValueError("research data kind is inconsistent")
                except ResearchDataUnavailable as error:
                    failure = error
                except _DataWorkerCapacityExceeded:
                    worker_failure_code = "worker_data_capacity"
                    failure = ResearchDataUnavailable(
                        kind=kind,
                        reason=ResearchMissingReason.PROVIDER_UNAVAILABLE,
                        attempted_sources=(),
                    )
                except TimeoutError:
                    worker_failure_code = "worker_data_deadline"
                    failure = ResearchDataUnavailable(
                        kind=kind,
                        reason=ResearchMissingReason.TIMEOUT,
                        attempted_sources=(),
                    )
                except Exception:
                    failure = ResearchDataUnavailable(
                        kind=kind,
                        reason=ResearchMissingReason.INVALID_RESPONSE,
                        attempted_sources=(),
                    )
                if failure is None:
                    self._repository.finish_data_attempt_success(
                        claim,
                        run_id,
                        kind.value,
                        attempt.attempt_no,
                        section,
                        now=self._clock(),
                    )
                    sections.append(section)
                    break
                if failure.kind is not kind:
                    failure = ResearchDataUnavailable(
                        kind=kind,
                        reason=ResearchMissingReason.INVALID_RESPONSE,
                        attempted_sources=(),
                    )
                decision = (
                    RetryDecision(
                        retryable=False,
                        code=worker_failure_code,
                        safe_message=(
                            "research data worker capacity is exhausted"
                            if worker_failure_code == "worker_data_capacity"
                            else "research data worker exceeded its deadline"
                        ),
                    )
                    if worker_failure_code is not None
                    else classify_retry(failure)
                )
                exhausted = (
                    not decision.retryable
                    or attempt_no >= self._retry_policy.max_attempts
                )
                backoff = (
                    self._retry_policy.delay_before_attempt(attempt_no + 1)
                    if not exhausted
                    else None
                )
                missing_outcome = (
                    data_service.missing_from_error(failure) if exhausted else None
                )
                self._repository.finish_attempt_failure(
                    claim,
                    run_id,
                    kind.value,
                    attempt.attempt_no,
                    decision,
                    exhausted=exhausted,
                    backoff_seconds=backoff,
                    missing_section=missing_outcome,
                    now=self._clock(),
                )
                if exhausted:
                    assert missing_outcome is not None
                    missing.append(missing_outcome)
                    break
        return sections, missing

    async def _load_data_kind(
        self,
        *,
        data_service: ResearchDataService,
        symbol: str,
        kind: ResearchSectionKind,
    ) -> ResearchSection:
        capacity = _DATA_WORKER_CAPACITY
        lease = capacity.acquire(restart_on_poisoned=self._restart_on_data_capacity)
        future: Future[ResearchSection] = Future()

        def invoke() -> None:
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(data_service.load_kind(symbol, kind))
                    except BaseException as error:
                        future.set_exception(error)
            finally:
                capacity.release(lease)

        worker = threading.Thread(
            target=invoke,
            name=f"analysis-data-{kind.value}-{next(_DATA_WORKER_SEQUENCE)}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            capacity.release(lease)
            raise
        deadline = asyncio.get_running_loop().time() + self._data_timeout_seconds
        while not future.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                capacity.mark_timeout(lease)
                raise TimeoutError
            await asyncio.sleep(min(0.005, remaining))
        return future.result()

    async def _await_with_lease[T](
        self,
        operation: Awaitable[T],
        *,
        claim: TaskClaim,
        run_id: str,
    ) -> T:
        operation_task = asyncio.ensure_future(operation)
        try:
            while not operation_task.done():
                tick: asyncio.Future[None] = asyncio.ensure_future(
                    self._lease_sleeper(self._lease_interval_seconds)
                )
                done, _pending = await asyncio.wait(
                    cast(
                        set[asyncio.Future[object]],
                        {operation_task, tick},
                    ),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if operation_task in done:
                    tick.cancel()
                    await asyncio.gather(tick, return_exceptions=True)
                    break
                if self._repository.cancellation_requested(claim):
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                    self._repository.cancel_run(
                        claim,
                        run_id,
                        now=self._clock(),
                    )
                    raise AnalysisCancelled()
                self._repository.heartbeat(
                    claim,
                    now=self._clock(),
                    lease_duration=self._lease_duration,
                )
            return await operation_task
        except BaseException:
            if not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
            raise

    async def _execute_role(
        self,
        *,
        claim: TaskClaim,
        run_id: str,
        role: RoleName,
        snapshot: ResearchSnapshot,
        evidence_graph: EvidenceGraph,
        dependencies: tuple[RoleOutput, ...],
    ) -> tuple[RoleOutput, WorkflowStageTrace] | None:
        self._raise_if_cancel_requested(claim)
        try:
            prepared = self._workflow.prepare_stage(
                role=role,
                snapshot=snapshot,
                graph=evidence_graph,
                dependencies=dependencies,
            )
        except Exception as error:
            attempt = self._repository.start_attempt(
                claim,
                run_id,
                role.value,
                provider=self._provider.provider,
                model=self._provider.model,
                request_hash=None,
                now=self._clock(),
            )
            self._repository.finish_attempt_failure(
                claim,
                run_id,
                role.value,
                attempt.attempt_no,
                classify_retry(error),
                exhausted=True,
                now=self._clock(),
            )
            return None

        stage = self._repository.get_stage(run_id, role.value)
        first_attempt = stage.attempt_count + 1
        if first_attempt > self._retry_policy.max_attempts:
            self._repository.exhaust_stage(
                claim,
                run_id,
                role.value,
                failure_code="retry_budget_exhausted",
                now=self._clock(),
            )
            return None
        for attempt_no in range(first_attempt, self._retry_policy.max_attempts + 1):
            self._raise_if_cancel_requested(claim)
            if attempt_no > 1:
                await self._await_with_lease(
                    self._sleeper(self._retry_policy.delay_before_attempt(attempt_no)),
                    claim=claim,
                    run_id=run_id,
                )
                self._raise_if_cancel_requested(claim)
            attempt = self._repository.start_attempt(
                claim,
                run_id,
                role.value,
                provider=self._provider.provider,
                model=self._provider.model,
                request_hash=prepared.request_hash,
                template_version=prepared.template_version,
                template_hash=prepared.template_hash,
                now=self._clock(),
            )
            try:
                result = await self._workflow.execute_stage(prepared)
            except Exception as error:
                decision = classify_retry(error)
                exhausted = (
                    not decision.retryable
                    or attempt_no >= self._retry_policy.max_attempts
                )
                backoff = (
                    self._retry_policy.delay_before_attempt(attempt_no + 1)
                    if not exhausted
                    else None
                )
                self._repository.finish_attempt_failure(
                    claim,
                    run_id,
                    role.value,
                    attempt.attempt_no,
                    decision,
                    exhausted=exhausted,
                    backoff_seconds=backoff,
                    now=self._clock(),
                )
                if exhausted:
                    return None
                continue
            canonical_output = self._repository.finish_attempt_success(
                claim,
                run_id,
                role.value,
                attempt.attempt_no,
                result.output,
                result.trace,
                now=self._clock(),
            )
            return canonical_output, result.trace
        return None

    def _raise_if_cancel_requested(self, claim: TaskClaim) -> None:
        if self._repository.cancellation_requested(claim):
            raise AnalysisCancelled()

    def _validate_retry_policy(self, run_id: str) -> None:
        frozen_policy = self._repository.load_execution_config(run_id).retry_policy
        if self._retry_policy != frozen_policy:
            raise ValueError(
                "analysis retry policy does not match frozen run configuration"
            )

    def _cancel_if_requested(self, claim: TaskClaim, run_id: str) -> None:
        if self._repository.cancellation_requested(claim):
            if self._repository.get_run(run_id).status is AnalysisRunStatus.RUNNING:
                self._repository.cancel_run(claim, run_id, now=self._clock())
            raise AnalysisCancelled()
