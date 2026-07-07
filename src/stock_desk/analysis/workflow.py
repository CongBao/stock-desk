from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import time
from typing import Any, cast, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    ValidationError,
)

from stock_desk.analysis.evidence import EvidenceGraph, EvidenceItem
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
    load_role_prompt,
    role_output_schema,
    validate_role_output,
)
from stock_desk.analysis.snapshot import (
    ResearchSection,
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


class AnalysisWorkflow:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._monotonic = monotonic

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
        prompt = load_role_prompt(role)
        request = _build_request(
            role=role,
            snapshot=snapshot,
            evidence=allowed_evidence,
            dependencies=dependencies,
            system=prompt.content,
        )
        return _PreparedRole(
            role=role,
            snapshot_id=snapshot.snapshot_id,
            allowed_evidence=allowed_evidence,
            request=request,
            template_version=prompt.version,
            template_hash=prompt.content_hash,
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


def _build_request(
    *,
    role: RoleName,
    snapshot: ResearchSnapshot,
    evidence: tuple[EvidenceItem, ...],
    dependencies: tuple[RoleOutput, ...],
    system: str,
) -> ModelRequest:
    blocks: list[dict[str, JsonValue]] = [
        {
            "block_type": "workflow_context",
            "role": role.value,
            "snapshot_id": snapshot.snapshot_id,
            "symbol": snapshot.symbol,
            "allowed_evidence_ids": [item.evidence_id for item in evidence],
        }
    ]
    if role in ROLE_SECTION_KINDS:
        allowed_kinds = ROLE_SECTION_KINDS[role]
        blocks.extend(
            _snapshot_section_block(section, evidence)
            for section in snapshot.sections
            if section.kind in allowed_kinds
        )
    else:
        blocks.extend(
            {
                "block_type": "role_output",
                **cast(dict[str, JsonValue], dependency.model_dump(mode="json")),
            }
            for dependency in dependencies
        )
        blocks.extend(_evidence_reference_block(item) for item in evidence)
        if role is RoleName.RISK_DECISION:
            blocks.append(
                {
                    "block_type": "quality_flags",
                    "sections": [
                        {
                            "section_kind": section.kind.value,
                            "flags": [flag.value for flag in section.quality_flags],
                        }
                        for section in snapshot.sections
                    ],
                }
            )
    try:
        request = ModelRequest(
            system=system,
            data_blocks=tuple(blocks),
            output_schema=role_output_schema(),
            temperature=DEFAULT_TEMPERATURE,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        )
        return ModelRequest.model_validate_json(request.model_dump_json())
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise WorkflowRequestValidationError() from None


def _snapshot_section_block(
    section: ResearchSection,
    evidence: tuple[EvidenceItem, ...],
) -> dict[str, JsonValue]:
    return {
        "block_type": "snapshot_section",
        "section_kind": section.kind.value,
        "section_id": section.section_id,
        "content": section.content,
        "evidence_ids": [
            item.evidence_id for item in evidence if item.section_kind is section.kind
        ],
        "provenance": {
            "canonical_source": section.canonical_source,
            "source_record": section.source_record,
            "source_url": section.source_url,
            "published_at": (
                section.published_at.isoformat()
                if section.published_at is not None
                else None
            ),
            "data_cutoff": section.data_cutoff.isoformat(),
            "fetched_at": section.fetched_at.isoformat(),
            "dataset_version": section.dataset_version,
            "quality_flags": [flag.value for flag in section.quality_flags],
        },
    }


def _evidence_reference_block(item: EvidenceItem) -> dict[str, JsonValue]:
    return {
        "block_type": "evidence_reference",
        "evidence_id": item.evidence_id,
        "section_kind": item.section_kind.value,
        "excerpt": item.excerpt,
        "canonical_source": item.canonical_source,
        "source_record": item.source_record,
        "source_url": item.source_url,
        "published_at": (
            item.published_at.isoformat() if item.published_at is not None else None
        ),
        "data_cutoff": item.data_cutoff.isoformat(),
        "fetched_at": item.fetched_at.isoformat(),
        "quality_flags": [flag.value for flag in item.quality_flags],
    }
