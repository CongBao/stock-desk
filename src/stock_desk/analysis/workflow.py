from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import time
from typing import Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    ValidationError,
)

from stock_desk.analysis.evidence import EvidenceGraph, EvidenceItem
from stock_desk.analysis.content_policy import ContentPolicyError
from stock_desk.analysis.prompt_builder import build_role_request
from stock_desk.analysis.providers.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from stock_desk.analysis.roles import (
    ANALYST_ROLES,
    REVIEW_ROLES,
    ROLE_ORDER,
    ROLE_SECTION_KINDS,
    RoleName,
    RoleOutput,
    validate_role_output,
)
from stock_desk.analysis.snapshot import (
    ResearchSnapshot,
    Sha256Digest,
)
from stock_desk.market.types import UtcDatetime


DEFAULT_TEMPERATURE: Final = 0.1
DEFAULT_TIMEOUT_SECONDS: Final = 90.0
DEFAULT_MAX_OUTPUT_TOKENS: Final = 4_096


class _FrozenWorkflowModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class WorkflowStageStatus(StrEnum):
    SUCCEEDED = "succeeded"


class WorkflowStageTrace(_FrozenWorkflowModel):
    role: RoleName
    status: WorkflowStageStatus
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_seconds: StrictFloat = Field(ge=0.0)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=256)
    template_version: str = Field(min_length=1, max_length=64)
    template_hash: Sha256Digest
    request_hash: Sha256Digest
    usage: ModelUsage


class WorkflowResult(_FrozenWorkflowModel):
    snapshot_id: Sha256Digest
    outputs: tuple[RoleOutput, ...]
    trace: tuple[WorkflowStageTrace, ...]
    evidence_ids: tuple[Sha256Digest, ...]


class WorkflowRequestValidationError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("model role request is invalid")


@dataclass(frozen=True, slots=True)
class _CompletedRole:
    output: RoleOutput
    trace: WorkflowStageTrace


@dataclass(frozen=True, slots=True)
class _PreparedRole:
    role: RoleName
    snapshot_id: str
    allowed_evidence: tuple[EvidenceItem, ...]
    request: ModelRequest
    template_version: str
    template_hash: str
    request_hash: str


class AnalysisWorkflow:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._monotonic = monotonic
        self._temperature = temperature
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens

    def prepare_stage(
        self,
        *,
        role: RoleName,
        snapshot: ResearchSnapshot,
        graph: EvidenceGraph,
        dependencies: tuple[RoleOutput, ...],
    ) -> _PreparedRole:
        """Prepare one validated role request for resilient orchestration."""
        return self._prepare_role(
            role=role,
            snapshot=snapshot,
            graph=graph,
            dependencies=dependencies,
        )

    async def execute_stage(self, prepared: _PreparedRole) -> _CompletedRole:
        """Execute one prepared role through the same Task 4/5 boundary."""
        return await self._execute_role(prepared)

    async def run(
        self,
        snapshot: ResearchSnapshot,
        evidence_graph: EvidenceGraph,
    ) -> WorkflowResult:
        frozen_snapshot = _copy_snapshot(snapshot)
        frozen_graph = _copy_evidence_graph(evidence_graph)
        if (
            frozen_graph.snapshot.canonical_json_bytes()
            != frozen_snapshot.canonical_json_bytes()
        ):
            raise ValueError("evidence graph snapshot does not match workflow snapshot")

        analyst_plans = tuple(
            self._prepare_role(
                role=role,
                snapshot=frozen_snapshot,
                graph=frozen_graph,
                dependencies=(),
            )
            for role in ANALYST_ROLES
        )
        analysts = await self._run_parallel(
            tuple(self._execute_role(plan) for plan in analyst_plans),
            ANALYST_ROLES,
        )
        analyst_outputs = tuple(item.output for item in analysts)
        review_plans = tuple(
            self._prepare_role(
                role=role,
                snapshot=frozen_snapshot,
                graph=frozen_graph,
                dependencies=analyst_outputs,
            )
            for role in REVIEW_ROLES
        )
        reviews = await self._run_parallel(
            tuple(self._execute_role(plan) for plan in review_plans),
            REVIEW_ROLES,
        )
        review_outputs = tuple(item.output for item in reviews)
        decision_plan = self._prepare_role(
            role=RoleName.RISK_DECISION,
            snapshot=frozen_snapshot,
            graph=frozen_graph,
            dependencies=review_outputs,
        )
        decision = await self._execute_role(decision_plan)
        completed = (*analysts, *reviews, decision)
        by_role = {item.output.role: item for item in completed}
        return WorkflowResult(
            snapshot_id=frozen_snapshot.snapshot_id,
            outputs=tuple(by_role[role].output for role in ROLE_ORDER),
            trace=tuple(by_role[role].trace for role in ROLE_ORDER),
            evidence_ids=tuple(
                item.evidence_id for item in frozen_graph.evidence_items
            ),
        )

    def _prepare_role(
        self,
        *,
        role: RoleName,
        snapshot: ResearchSnapshot,
        graph: EvidenceGraph,
        dependencies: tuple[RoleOutput, ...],
    ) -> _PreparedRole:
        allowed_evidence = _allowed_evidence(role, graph, dependencies)
        try:
            built = build_role_request(
                role=role,
                snapshot=snapshot,
                evidence=allowed_evidence,
                dependencies=dependencies,
                temperature=self._temperature,
                timeout_seconds=self._timeout_seconds,
                max_output_tokens=self._max_output_tokens,
            )
        except (
            ContentPolicyError,
            TypeError,
            ValueError,
            ValidationError,
            RecursionError,
        ):
            raise WorkflowRequestValidationError() from None
        return _PreparedRole(
            role=role,
            snapshot_id=snapshot.snapshot_id,
            allowed_evidence=allowed_evidence,
            request=built.request,
            template_version=built.template_version,
            template_hash=built.template_hash,
            request_hash=built.request_hash,
        )

    async def _execute_role(self, prepared: _PreparedRole) -> _CompletedRole:
        started_at = self._clock()
        started_monotonic = self._monotonic()
        response = await self._provider.complete(prepared.request)
        ended_monotonic = self._monotonic()
        ended_at = self._clock()
        if not isinstance(response, ModelResponse):
            from stock_desk.analysis.roles import RoleOutputValidationError

            raise RoleOutputValidationError()
        output = validate_role_output(
            response.content,
            expected_role=prepared.role,
            snapshot_id=prepared.snapshot_id,
            allowed_evidence=prepared.allowed_evidence,
        )
        trace = WorkflowStageTrace(
            role=prepared.role,
            status=WorkflowStageStatus.SUCCEEDED,
            started_at=started_at,
            ended_at=ended_at,
            duration_seconds=float(max(0.0, ended_monotonic - started_monotonic)),
            provider=response.provider,
            model=response.model,
            template_version=prepared.template_version,
            template_hash=prepared.template_hash,
            request_hash=prepared.request_hash,
            usage=response.usage,
        )
        return _CompletedRole(output=output, trace=trace)

    async def _run_parallel(
        self,
        operations: tuple[Coroutine[Any, Any, _CompletedRole], ...],
        roles: tuple[RoleName, ...],
    ) -> tuple[_CompletedRole, ...]:
        tasks: tuple[asyncio.Task[_CompletedRole], ...] = tuple(
            asyncio.create_task(operation, name=f"analysis-role-{role.value}")
            for role, operation in zip(roles, operations, strict=True)
        )
        try:
            remaining: set[asyncio.Task[_CompletedRole]] = set(tasks)
            while remaining:
                done, pending = await asyncio.wait(
                    remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in tasks:
                    if task not in done:
                        continue
                    if task.cancelled():
                        for sibling in pending:
                            sibling.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        task.result()
                    exception = task.exception()
                    if exception is not None:
                        for sibling in pending:
                            sibling.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        raise exception
                remaining = set(pending)
            return tuple(task.result() for task in tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


def _copy_snapshot(snapshot: ResearchSnapshot) -> ResearchSnapshot:
    return ResearchSnapshot.model_validate_json(snapshot.model_dump_json(by_alias=True))


def _copy_evidence_graph(graph: EvidenceGraph) -> EvidenceGraph:
    return EvidenceGraph.model_validate_json(graph.model_dump_json(by_alias=True))


def _allowed_evidence(
    role: RoleName,
    graph: EvidenceGraph,
    dependencies: tuple[RoleOutput, ...],
) -> tuple[EvidenceItem, ...]:
    if role in ROLE_SECTION_KINDS:
        kinds = ROLE_SECTION_KINDS[role]
        return tuple(
            item for item in graph.evidence_items if item.section_kind in kinds
        )
    referenced = frozenset(
        evidence_id
        for dependency in dependencies
        for evidence_id in dependency.evidence_ids
    )
    return tuple(
        item for item in graph.evidence_items if item.evidence_id in referenced
    )
