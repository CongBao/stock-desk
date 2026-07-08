from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import cast, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    ValidationError,
    model_validator,
)

from stock_desk.analysis.evidence import (
    Claim,
    EvidenceGraph,
    EvidenceItem,
    EvidenceStance,
)
from stock_desk.analysis.rating import (
    contains_forbidden_financial_action,
    Rating,
)
from stock_desk.analysis.roles import (
    ANALYST_ROLES,
    REVIEW_ROLES,
    ROLE_ORDER,
    ROLE_SECTION_KINDS,
    RoleName,
    RoleOutput,
    clean_role_output_active_secrets,
)
from stock_desk.analysis.snapshot import (
    RESEARCH_SECTION_ORDER,
    ResearchQualityFlag,
    ResearchSectionKind,
    ResearchSnapshot,
    Sha256Digest,
)
from stock_desk.analysis.workflow import (
    WorkflowResult,
    WorkflowStageStatus,
    WorkflowStageTrace,
)
from stock_desk.market.types import UtcDatetime
from stock_desk.security.redaction import clean_active_secrets


REPORT_SCHEMA_VERSION: Final = "analysis-report-v1"
REPORT_DISCLAIMER: Final = (
    "本报告仅为研究辅助信息，不构成投资建议、个性化建议或交易指令。"
)
MAX_RESEARCH_REPORT_BYTES: Final = 1_048_576
MAX_RESEARCH_REPORT_DEPTH: Final = 24
MAX_RESEARCH_REPORT_NODES: Final = 10_000
_INELIGIBLE_CRITICAL_FLAGS: Final = frozenset(
    {
        ResearchQualityFlag.STALE,
        ResearchQualityFlag.EXPIRED,
        ResearchQualityFlag.UNVERIFIED,
    }
)


class ReportStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class _FrozenReportModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class StageRetryAction(_FrozenReportModel):
    stage: RoleName
    action: Literal["retry_stage"] = "retry_stage"


class StageFailure(_FrozenReportModel):
    stage: RoleName
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    attempt_count: int = Field(ge=1, le=6)


class ResearchReport(_FrozenReportModel):
    schema_version: Literal["analysis-report-v1"] = REPORT_SCHEMA_VERSION
    report_id: Sha256Digest
    snapshot_id: Sha256Digest
    status: ReportStatus
    rating: Rating | None
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    confidence_explanation: str = Field(min_length=1, max_length=8_192)
    core_judgments: tuple[Claim, ...] = Field(max_length=64)
    bull_claims: tuple[Claim, ...] = Field(max_length=64)
    bear_claims: tuple[Claim, ...] = Field(max_length=64)
    risks: tuple[Claim, ...] = Field(max_length=64)
    evidence_items: tuple[EvidenceItem, ...] = Field(max_length=256)
    role_outputs: tuple[RoleOutput, ...] = Field(max_length=5)
    model_metadata: tuple[WorkflowStageTrace, ...] = Field(max_length=5)
    quality_flags: tuple[ResearchQualityFlag, ...] = Field(max_length=16)
    quality_notes: tuple[str, ...] = Field(max_length=16)
    missing_modules: tuple[RoleName, ...] = Field(max_length=5)
    missing_sections: tuple[ResearchSectionKind, ...] = Field(max_length=4)
    recovery_actions: tuple[str, ...] = Field(max_length=8)
    generated_at: UtcDatetime
    disclaimer: str = Field(min_length=1, max_length=512)
    retry_actions: tuple[StageRetryAction, ...] = ()
    failed_modules: tuple[RoleName, ...] = ()
    blocked_modules: tuple[RoleName, ...] = ()
    stage_failures: tuple[StageFailure, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        status: ReportStatus,
        rating: Rating | None,
        confidence: float,
        confidence_explanation: str,
        core_judgments: tuple[Claim, ...],
        bull_claims: tuple[Claim, ...],
        bear_claims: tuple[Claim, ...],
        risks: tuple[Claim, ...],
        evidence_items: tuple[EvidenceItem, ...],
        role_outputs: tuple[RoleOutput, ...],
        model_metadata: tuple[WorkflowStageTrace, ...],
        quality_flags: tuple[ResearchQualityFlag, ...],
        quality_notes: tuple[str, ...],
        missing_modules: tuple[RoleName, ...],
        missing_sections: tuple[ResearchSectionKind, ...],
        recovery_actions: tuple[str, ...],
        generated_at: datetime,
        disclaimer: str,
        retry_actions: tuple[StageRetryAction, ...] = (),
        failed_modules: tuple[RoleName, ...] = (),
        blocked_modules: tuple[RoleName, ...] = (),
        stage_failures: tuple[StageFailure, ...] = (),
    ) -> ResearchReport:
        draft = cls.model_construct(
            report_id="sha256:" + "0" * 64,
            schema_version=REPORT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            status=status,
            rating=rating,
            confidence=confidence,
            confidence_explanation=confidence_explanation,
            core_judgments=core_judgments,
            bull_claims=bull_claims,
            bear_claims=bear_claims,
            risks=risks,
            evidence_items=evidence_items,
            role_outputs=role_outputs,
            model_metadata=model_metadata,
            quality_flags=quality_flags,
            quality_notes=quality_notes,
            missing_modules=missing_modules,
            missing_sections=missing_sections,
            recovery_actions=recovery_actions,
            generated_at=generated_at,
            disclaimer=disclaimer,
            retry_actions=retry_actions,
            failed_modules=failed_modules,
            blocked_modules=blocked_modules,
            stage_failures=stage_failures,
        )
        report_id = _content_id(draft._identity_payload())
        return cls(
            report_id=report_id,
            schema_version=REPORT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            status=status,
            rating=rating,
            confidence=confidence,
            confidence_explanation=confidence_explanation,
            core_judgments=core_judgments,
            bull_claims=bull_claims,
            bear_claims=bear_claims,
            risks=risks,
            evidence_items=evidence_items,
            role_outputs=role_outputs,
            model_metadata=model_metadata,
            quality_flags=quality_flags,
            quality_notes=quality_notes,
            missing_modules=missing_modules,
            missing_sections=missing_sections,
            recovery_actions=recovery_actions,
            generated_at=generated_at,
            disclaimer=disclaimer,
            retry_actions=retry_actions,
            failed_modules=failed_modules,
            blocked_modules=blocked_modules,
            stage_failures=stage_failures,
        )

    @property
    def all_claims(self) -> tuple[Claim, ...]:
        return (
            *self.core_judgments,
            *self.bull_claims,
            *self.bear_claims,
            *self.risks,
        )

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.status is ReportStatus.COMPLETE:
            if (
                self.rating is None
                or self.missing_modules
                or not self.core_judgments
                or not self.bull_claims
                or not self.bear_claims
                or not self.risks
                or not self.evidence_items
                or not self.role_outputs
                or not self.model_metadata
            ):
                raise ValueError("complete report state is inconsistent")
            if self.retry_actions:
                raise ValueError("complete report cannot contain retry actions")
            if self.failed_modules or self.blocked_modules or self.stage_failures:
                raise ValueError("complete report cannot contain stage failures")
        elif self.status is ReportStatus.PARTIAL:
            if (
                self.rating is not None
                or self.confidence != 0.0
                or not self.missing_modules
                or not self.retry_actions
            ):
                raise ValueError("partial report state is inconsistent")
        elif self.rating is not None or self.confidence != 0.0:
            raise ValueError("insufficient report cannot contain a rating")
        elif self.retry_actions:
            raise ValueError("insufficient report cannot contain stage retries")
        elif self.failed_modules or self.blocked_modules or self.stage_failures:
            raise ValueError("insufficient report cannot contain stage failures")
        if self.disclaimer != REPORT_DISCLAIMER:
            raise ValueError("research report disclaimer is invalid")
        output_roles = tuple(item.role for item in self.role_outputs)
        canonical_output_roles = tuple(
            role for role in ROLE_ORDER if role in frozenset(output_roles)
        )
        if output_roles != canonical_output_roles:
            raise ValueError("report role outputs must use canonical order")
        trace_roles = tuple(item.role for item in self.model_metadata)
        if trace_roles != output_roles:
            raise ValueError("report model metadata must align with role outputs")
        if bool(self.role_outputs) != bool(self.model_metadata):
            raise ValueError("report role outputs and model metadata must align")
        for item in self.evidence_items:
            EvidenceItem.model_validate_json(item.model_dump_json(by_alias=True))
        for output in self.role_outputs:
            RoleOutput.model_validate_json(output.model_dump_json())
        for trace in self.model_metadata:
            WorkflowStageTrace.model_validate_json(trace.model_dump_json())
        if any(item.snapshot_id != self.snapshot_id for item in self.role_outputs):
            raise ValueError("report role output snapshot is inconsistent")
        if any(item.snapshot_id != self.snapshot_id for item in self.evidence_items):
            raise ValueError("report evidence snapshot is inconsistent")
        evidence_ids = tuple(item.evidence_id for item in self.evidence_items)
        if len(evidence_ids) != len(frozenset(evidence_ids)):
            raise ValueError("report evidence cannot contain duplicates")
        known = frozenset(evidence_ids)
        if any(
            evidence_id not in known
            for claim in self.all_claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError("report claim must reference registered evidence")
        if any(
            evidence_id not in known
            for output in self.role_outputs
            for claim in output.claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError("report role output must reference registered evidence")
        if self.role_outputs and self.status is not ReportStatus.PARTIAL:
            _validate_role_evidence_closure(
                self.evidence_items,
                self.role_outputs,
                require_exact=True,
            )
            outputs = {item.role: item for item in self.role_outputs}
            expected_risks = (
                tuple(
                    claim
                    for claim in outputs[RoleName.RISK_DECISION].claims
                    if claim.stance in {EvidenceStance.OPPOSE, EvidenceStance.UNCERTAIN}
                )
                or outputs[RoleName.BEAR].claims
            )
            if (
                self.core_judgments != outputs[RoleName.RISK_DECISION].claims
                or self.bull_claims != outputs[RoleName.BULL].claims
                or self.bear_claims != outputs[RoleName.BEAR].claims
                or self.risks != expected_risks
            ):
                raise ValueError(
                    "report claims must derive from canonical role outputs"
                )
        elif self.status is ReportStatus.PARTIAL:
            _validate_partial_role_evidence(self.evidence_items, self.role_outputs)
            outputs = {item.role: item for item in self.role_outputs}
            if (
                self.core_judgments
                or self.bull_claims
                != (outputs[RoleName.BULL].claims if RoleName.BULL in outputs else ())
                or self.bear_claims
                != (outputs[RoleName.BEAR].claims if RoleName.BEAR in outputs else ())
                or self.risks
            ):
                raise ValueError(
                    "partial report claims must derive from completed roles"
                )
        elif self.all_claims:
            raise ValueError("preflight report cannot contain role claims")
        canonical_missing = tuple(
            kind
            for kind in RESEARCH_SECTION_ORDER
            if kind in frozenset(self.missing_sections)
        )
        if self.missing_sections != canonical_missing:
            raise ValueError("report missing sections must use canonical order")
        if self.missing_sections and not self.recovery_actions:
            raise ValueError("report missing sections require recovery actions")
        if self.recovery_actions != _canonical_recovery_actions(self.missing_sections):
            raise ValueError("report recovery actions are inconsistent")
        if self.model_metadata and self.generated_at != max(
            item.ended_at for item in self.model_metadata
        ):
            raise ValueError("report generation time must match completed workflow")
        if self.status is ReportStatus.PARTIAL:
            expected_missing = tuple(
                role for role in ROLE_ORDER if role not in frozenset(output_roles)
            )
            retry_stages = tuple(item.stage for item in self.retry_actions)
            failure_stages = tuple(item.stage for item in self.stage_failures)
            if (
                self.missing_modules != expected_missing
                or self.failed_modules != failure_stages
                or tuple(
                    role
                    for role in ROLE_ORDER
                    if role in frozenset(self.failed_modules)
                )
                != self.failed_modules
                or tuple(
                    role
                    for role in ROLE_ORDER
                    if role in frozenset(self.blocked_modules)
                )
                != self.blocked_modules
                or frozenset((*self.failed_modules, *self.blocked_modules))
                != frozenset(expected_missing)
                or frozenset(self.failed_modules).intersection(self.blocked_modules)
                or retry_stages
                != tuple(role for role in ROLE_ORDER if role in frozenset(retry_stages))
                or len(retry_stages) != len(frozenset(retry_stages))
                or retry_stages != self.failed_modules
                or self.confidence_explanation
                != "Partial analysis: one or more model stages did not complete."
            ):
                raise ValueError("partial report does not match deterministic policy")
        else:
            policy = _evaluate_report_policy(
                evidence=self.evidence_items,
                outputs=self.role_outputs,
                missing_sections=self.missing_sections,
            )
            if (
                self.status is not policy.status
                or self.rating is not policy.rating
                or self.confidence != policy.confidence
                or self.confidence_explanation != policy.confidence_explanation
                or self.quality_flags != policy.quality_flags
                or self.quality_notes != policy.quality_notes
                or self.missing_modules != policy.missing_modules
                or any(gap[1] not in self.missing_sections for gap in policy.gaps)
            ):
                raise ValueError("research report does not match deterministic policy")
        _validate_safe_report_text(self)
        _validate_report_budget(self._identity_payload())
        if self.report_id != _content_id(self._identity_payload()):
            raise ValueError("report_id does not match canonical report content")
        return self

    def _identity_payload(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            self.model_dump(mode="json", exclude={"report_id"}),
        )

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


class ReportInputValidationError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("research report input is invalid")


class ReportValidationError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("research report is invalid")


@dataclass(frozen=True, slots=True)
class _PolicyEvaluation:
    status: ReportStatus
    rating: Rating | None
    confidence: float
    confidence_explanation: str
    quality_flags: tuple[ResearchQualityFlag, ...]
    quality_notes: tuple[str, ...]
    missing_modules: tuple[RoleName, ...]
    gaps: tuple[tuple[RoleName, ResearchSectionKind, str, str], ...]


class ResearchReportBuilder:
    def build(
        self,
        *,
        snapshot: ResearchSnapshot,
        evidence_graph: EvidenceGraph,
        workflow: WorkflowResult,
    ) -> ResearchReport:
        try:
            frozen_snapshot = ResearchSnapshot.model_validate_json(
                snapshot.model_dump_json(by_alias=True)
            )
            frozen_graph = EvidenceGraph.model_validate_json(
                evidence_graph.model_dump_json(by_alias=True)
            )
            frozen_workflow = WorkflowResult.model_validate_json(
                workflow.model_dump_json()
            )
            self._validate_inputs(frozen_snapshot, frozen_graph, frozen_workflow)
            return self._build_validated(
                frozen_snapshot,
                frozen_graph,
                frozen_workflow,
            )
        except ReportInputValidationError:
            raise
        except (TypeError, ValueError, ValidationError, RecursionError):
            raise ReportInputValidationError() from None

    def build_insufficient(
        self,
        *,
        snapshot: ResearchSnapshot,
        evidence_graph: EvidenceGraph,
    ) -> ResearchReport:
        try:
            frozen_snapshot = ResearchSnapshot.model_validate_json(
                snapshot.model_dump_json(by_alias=True)
            )
            frozen_graph = EvidenceGraph.model_validate_json(
                evidence_graph.model_dump_json(by_alias=True)
            )
            if (
                frozen_graph.snapshot.canonical_json_bytes()
                != frozen_snapshot.canonical_json_bytes()
            ):
                raise ReportInputValidationError()
            snapshot_missing = tuple(
                item.kind for item in frozen_snapshot.missing_sections
            )
            initial_policy = _evaluate_report_policy(
                evidence=frozen_graph.evidence_items,
                outputs=(),
                missing_sections=snapshot_missing,
            )
            if not initial_policy.gaps:
                raise ReportInputValidationError()
            missing_sections = _all_missing_sections(
                frozen_snapshot,
                initial_policy.gaps,
            )
            policy = _evaluate_report_policy(
                evidence=frozen_graph.evidence_items,
                outputs=(),
                missing_sections=missing_sections,
            )
            recovery_actions = _canonical_recovery_actions(missing_sections)
            return ResearchReport.create(
                snapshot_id=frozen_snapshot.snapshot_id,
                status=policy.status,
                rating=policy.rating,
                confidence=policy.confidence,
                confidence_explanation=policy.confidence_explanation,
                core_judgments=(),
                bull_claims=(),
                bear_claims=(),
                risks=(),
                evidence_items=frozen_graph.evidence_items,
                role_outputs=(),
                model_metadata=(),
                quality_flags=policy.quality_flags,
                quality_notes=policy.quality_notes,
                missing_modules=policy.missing_modules,
                missing_sections=missing_sections,
                recovery_actions=recovery_actions,
                generated_at=frozen_snapshot.frozen_at,
                disclaimer=REPORT_DISCLAIMER,
            )
        except ReportInputValidationError:
            raise
        except (TypeError, ValueError, ValidationError, RecursionError):
            raise ReportInputValidationError() from None

    def build_partial(
        self,
        *,
        snapshot: ResearchSnapshot,
        evidence_graph: EvidenceGraph,
        outputs: tuple[RoleOutput, ...],
        trace: tuple[WorkflowStageTrace, ...],
        failures: tuple[StageFailure, ...],
    ) -> ResearchReport:
        try:
            frozen_snapshot = ResearchSnapshot.model_validate_json(
                snapshot.model_dump_json(by_alias=True)
            )
            frozen_graph = EvidenceGraph.model_validate_json(
                evidence_graph.model_dump_json(by_alias=True)
            )
            frozen_outputs = tuple(
                RoleOutput.model_validate_json(item.model_dump_json())
                for item in outputs
            )
            frozen_trace = tuple(
                WorkflowStageTrace.model_validate_json(item.model_dump_json())
                for item in trace
            )
            frozen_failures = tuple(
                StageFailure.model_validate_json(item.model_dump_json())
                for item in failures
            )
            if (
                frozen_graph.snapshot.canonical_json_bytes()
                != frozen_snapshot.canonical_json_bytes()
            ):
                raise ValueError
            output_roles = tuple(item.role for item in frozen_outputs)
            if (
                output_roles
                != tuple(role for role in ROLE_ORDER if role in frozenset(output_roles))
                or tuple(item.role for item in frozen_trace) != output_roles
                or any(
                    item.status is not WorkflowStageStatus.SUCCEEDED
                    for item in frozen_trace
                )
                or any(
                    item.snapshot_id != frozen_snapshot.snapshot_id
                    for item in frozen_outputs
                )
            ):
                raise ValueError
            failure_roles = tuple(item.stage for item in frozen_failures)
            if (
                not failure_roles
                or failure_roles
                != tuple(
                    role for role in ROLE_ORDER if role in frozenset(failure_roles)
                )
                or len(failure_roles) != len(frozenset(failure_roles))
                or frozenset(failure_roles).intersection(output_roles)
            ):
                raise ValueError
            _validate_partial_role_evidence(
                frozen_graph.evidence_items,
                frozen_outputs,
            )
            referenced = frozenset(
                evidence_id
                for output in frozen_outputs
                for claim in output.claims
                for evidence_id in claim.evidence_ids
            )
            evidence_items = tuple(
                item
                for item in frozen_graph.evidence_items
                if item.evidence_id in referenced
            )
            if frozenset(item.evidence_id for item in evidence_items) != referenced:
                raise ValueError
            missing_modules = tuple(
                role for role in ROLE_ORDER if role not in frozenset(output_roles)
            )
            if any(role not in missing_modules for role in failure_roles):
                raise ValueError
            blocked_modules = tuple(
                role for role in missing_modules if role not in frozenset(failure_roles)
            )
            by_role = {item.role: item for item in frozen_outputs}
            bull_claims = (
                by_role[RoleName.BULL].claims if RoleName.BULL in by_role else ()
            )
            bear_claims = (
                by_role[RoleName.BEAR].claims if RoleName.BEAR in by_role else ()
            )
            missing_sections = tuple(
                item.kind for item in frozen_snapshot.missing_sections
            )
            quality_flags = tuple(
                sorted(
                    {flag for item in evidence_items for flag in item.quality_flags},
                    key=lambda flag: flag.value,
                )
            )
            quality_notes = tuple(
                (
                    *(f"{role.value} stage failed" for role in failure_roles),
                    *(f"{role.value} stage blocked" for role in blocked_modules),
                    *_missing_section_notes(missing_sections),
                )
            )
            return ResearchReport.create(
                snapshot_id=frozen_snapshot.snapshot_id,
                status=ReportStatus.PARTIAL,
                rating=None,
                confidence=0.0,
                confidence_explanation=(
                    "Partial analysis: one or more model stages did not complete."
                ),
                core_judgments=(),
                bull_claims=bull_claims,
                bear_claims=bear_claims,
                risks=(),
                evidence_items=evidence_items,
                role_outputs=frozen_outputs,
                model_metadata=frozen_trace,
                quality_flags=quality_flags,
                quality_notes=quality_notes,
                missing_modules=missing_modules,
                missing_sections=missing_sections,
                recovery_actions=_canonical_recovery_actions(missing_sections),
                generated_at=(
                    max(item.ended_at for item in frozen_trace)
                    if frozen_trace
                    else frozen_snapshot.frozen_at
                ),
                disclaimer=REPORT_DISCLAIMER,
                retry_actions=tuple(
                    StageRetryAction(stage=role) for role in failure_roles
                ),
                failed_modules=failure_roles,
                blocked_modules=blocked_modules,
                stage_failures=frozen_failures,
            )
        except ReportInputValidationError:
            raise
        except (TypeError, ValueError, ValidationError, RecursionError):
            raise ReportInputValidationError() from None

    def _validate_inputs(
        self,
        snapshot: ResearchSnapshot,
        graph: EvidenceGraph,
        workflow: WorkflowResult,
    ) -> None:
        if graph.snapshot.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise ReportInputValidationError()
        if workflow.snapshot_id != snapshot.snapshot_id:
            raise ReportInputValidationError()
        graph_ids = tuple(item.evidence_id for item in graph.evidence_items)
        if workflow.evidence_ids != graph_ids:
            raise ReportInputValidationError()
        if tuple(item.role for item in workflow.outputs) != ROLE_ORDER:
            raise ReportInputValidationError()
        if tuple(item.role for item in workflow.trace) != ROLE_ORDER:
            raise ReportInputValidationError()
        if any(
            item.status is not WorkflowStageStatus.SUCCEEDED for item in workflow.trace
        ):
            raise ReportInputValidationError()
        if any(item.snapshot_id != snapshot.snapshot_id for item in workflow.outputs):
            raise ReportInputValidationError()
        self._validate_role_evidence(graph, workflow.outputs)
        for output in workflow.outputs:
            if contains_forbidden_financial_action(output.summary) or any(
                contains_forbidden_financial_action(claim.text)
                for claim in output.claims
            ):
                raise ReportInputValidationError()

    def _validate_role_evidence(
        self,
        graph: EvidenceGraph,
        outputs: tuple[RoleOutput, ...],
    ) -> None:
        try:
            _validate_role_evidence_closure(
                graph.evidence_items,
                outputs,
                require_exact=False,
            )
        except ValueError:
            raise ReportInputValidationError()

    def _build_validated(
        self,
        snapshot: ResearchSnapshot,
        graph: EvidenceGraph,
        workflow: WorkflowResult,
    ) -> ResearchReport:
        output_by_role = {item.role: item for item in workflow.outputs}
        risk_output = output_by_role[RoleName.RISK_DECISION]
        proposal = risk_output.proposal
        if proposal is None:
            raise ReportInputValidationError()
        referenced_ids = frozenset(
            evidence_id
            for output in workflow.outputs
            for claim in output.claims
            for evidence_id in claim.evidence_ids
        )
        evidence_items = tuple(
            item for item in graph.evidence_items if item.evidence_id in referenced_ids
        )
        snapshot_missing = tuple(item.kind for item in snapshot.missing_sections)
        initial_policy = _evaluate_report_policy(
            evidence=evidence_items,
            outputs=workflow.outputs,
            missing_sections=snapshot_missing,
        )
        missing_sections = _all_missing_sections(snapshot, initial_policy.gaps)
        policy = _evaluate_report_policy(
            evidence=evidence_items,
            outputs=workflow.outputs,
            missing_sections=missing_sections,
        )
        recovery_actions = _canonical_recovery_actions(missing_sections)
        risk_claims = (
            tuple(
                claim
                for claim in risk_output.claims
                if claim.stance in {EvidenceStance.OPPOSE, EvidenceStance.UNCERTAIN}
            )
            or output_by_role[RoleName.BEAR].claims
        )
        generated_at = max(item.ended_at for item in workflow.trace)
        return ResearchReport.create(
            snapshot_id=snapshot.snapshot_id,
            status=policy.status,
            rating=policy.rating,
            confidence=policy.confidence,
            confidence_explanation=policy.confidence_explanation,
            core_judgments=risk_output.claims,
            bull_claims=output_by_role[RoleName.BULL].claims,
            bear_claims=output_by_role[RoleName.BEAR].claims,
            risks=risk_claims,
            evidence_items=evidence_items,
            role_outputs=workflow.outputs,
            model_metadata=workflow.trace,
            quality_flags=policy.quality_flags,
            quality_notes=policy.quality_notes,
            missing_modules=policy.missing_modules,
            missing_sections=missing_sections,
            recovery_actions=recovery_actions,
            generated_at=generated_at,
            disclaimer=REPORT_DISCLAIMER,
        )


def parse_research_report_json(value: str | bytes | bytearray) -> ResearchReport:
    try:
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if len(raw) > MAX_RESEARCH_REPORT_BYTES:
            raise ValueError("research report exceeds the byte limit")
        decoded = json.loads(raw)
        _validate_report_shape(decoded)
        return ResearchReport.model_validate_json(raw)
    except (TypeError, ValueError, ValidationError, RecursionError, UnicodeError):
        raise ReportValidationError() from None


def clean_research_report_active_secrets(report: ResearchReport) -> ResearchReport:
    """Return a valid report with credentials removed from model-controlled text."""
    payload = report.model_dump(mode="json", exclude={"report_id"})
    payload["role_outputs"] = [
        output.model_dump(mode="json")
        for output in (
            clean_role_output_active_secrets(item) for item in report.role_outputs
        )
    ]
    for field_name in (
        "core_judgments",
        "bull_claims",
        "bear_claims",
        "risks",
    ):
        claims = payload[field_name]
        if type(claims) is not list:
            raise ReportValidationError()
        for claim in claims:
            if type(claim) is not dict or type(claim.get("text")) is not str:
                raise ReportValidationError()
            cleaned = clean_active_secrets(claim["text"])
            if type(cleaned) is not str:
                raise ReportValidationError()
            claim["text"] = cleaned
    explanation = clean_active_secrets(payload["confidence_explanation"])
    if type(explanation) is not str:
        raise ReportValidationError()
    payload["confidence_explanation"] = explanation
    payload["report_id"] = _content_id(payload)
    try:
        return ResearchReport.model_validate_json(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError, ValidationError):
        raise ReportValidationError() from None


def _validate_role_evidence_closure(
    evidence: tuple[EvidenceItem, ...],
    outputs: tuple[RoleOutput, ...],
    *,
    require_exact: bool,
) -> None:
    by_id = {item.evidence_id: item for item in evidence}
    output_by_role = {item.role: item for item in outputs}
    for role in ANALYST_ROLES:
        allowed_kinds = ROLE_SECTION_KINDS[role]
        if any(
            evidence_id not in by_id
            or by_id[evidence_id].section_kind not in allowed_kinds
            for claim in output_by_role[role].claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError("analyst evidence is outside its allowlist")
    analyst_ids = frozenset(
        evidence_id
        for role in ANALYST_ROLES
        for claim in output_by_role[role].claims
        for evidence_id in claim.evidence_ids
    )
    for role in REVIEW_ROLES:
        if any(
            evidence_id not in analyst_ids
            for claim in output_by_role[role].claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError("review evidence is outside its dependency closure")
    review_ids = frozenset(
        evidence_id
        for role in REVIEW_ROLES
        for claim in output_by_role[role].claims
        for evidence_id in claim.evidence_ids
    )
    if any(
        evidence_id not in review_ids
        for claim in output_by_role[RoleName.RISK_DECISION].claims
        for evidence_id in claim.evidence_ids
    ):
        raise ValueError("risk evidence is outside its dependency closure")
    if require_exact:
        referenced = frozenset(
            evidence_id
            for output in outputs
            for claim in output.claims
            for evidence_id in claim.evidence_ids
        )
        if referenced != frozenset(by_id):
            raise ValueError("report evidence must equal the role reference union")


def _validate_partial_role_evidence(
    evidence: tuple[EvidenceItem, ...],
    outputs: tuple[RoleOutput, ...],
) -> None:
    by_id = {item.evidence_id: item for item in evidence}
    by_role = {item.role: item for item in outputs}
    for role in ANALYST_ROLES:
        output = by_role.get(role)
        if output is None:
            continue
        allowed_kinds = ROLE_SECTION_KINDS[role]
        if any(
            evidence_id not in by_id
            or by_id[evidence_id].section_kind not in allowed_kinds
            for claim in output.claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError("partial analyst evidence is outside its allowlist")
    analyst_ids = frozenset(
        evidence_id
        for role in ANALYST_ROLES
        if role in by_role
        for claim in by_role[role].claims
        for evidence_id in claim.evidence_ids
    )
    for role in REVIEW_ROLES:
        output = by_role.get(role)
        if output is None:
            continue
        if not all(analyst in by_role for analyst in ANALYST_ROLES) or any(
            evidence_id not in analyst_ids
            for claim in output.claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError(
                "partial review evidence is outside its dependency closure"
            )
    risk = by_role.get(RoleName.RISK_DECISION)
    if risk is not None:
        if not all(review in by_role for review in REVIEW_ROLES):
            raise ValueError("partial risk output is missing review dependencies")
        review_ids = frozenset(
            evidence_id
            for role in REVIEW_ROLES
            for claim in by_role[role].claims
            for evidence_id in claim.evidence_ids
        )
        if any(
            evidence_id not in review_ids
            for claim in risk.claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError("partial risk evidence is outside its dependency closure")


def _evaluate_report_policy(
    *,
    evidence: tuple[EvidenceItem, ...],
    outputs: tuple[RoleOutput, ...],
    missing_sections: tuple[ResearchSectionKind, ...],
) -> _PolicyEvaluation:
    by_id = {item.evidence_id: item for item in evidence}
    outputs_by_role = {item.role: item for item in outputs}
    requirements = (
        (RoleName.TECHNICAL, ResearchSectionKind.MARKET),
        (RoleName.FUNDAMENTAL_NEWS, ResearchSectionKind.FUNDAMENTALS),
    )
    gaps: list[tuple[RoleName, ResearchSectionKind, str, str]] = []
    missing = frozenset(missing_sections)
    for role, kind in requirements:
        if outputs:
            role_evidence = tuple(
                by_id[evidence_id]
                for claim in outputs_by_role[role].claims
                for evidence_id in claim.evidence_ids
                if evidence_id in by_id and by_id[evidence_id].section_kind is kind
            )
        else:
            role_evidence = tuple(
                item for item in evidence if item.section_kind is kind
            )
        eligible = tuple(
            item
            for item in role_evidence
            if not _INELIGIBLE_CRITICAL_FLAGS.intersection(item.quality_flags)
        )
        if eligible:
            continue
        reason = (
            _unusable_reason(role_evidence)
            if role_evidence
            else "missing"
            if kind in missing
            else "no_evidence"
        )
        gaps.append((role, kind, reason, f"refresh_{kind.value}"))
    quality_flags = tuple(
        sorted(
            {flag for item in evidence for flag in item.quality_flags},
            key=lambda item: item.value,
        )
    )
    missing_notes = _missing_section_notes(missing_sections)
    if gaps:
        reasons = "; ".join(
            f"{section.value}: {reason}" for _, section, reason, _ in gaps
        )
        gap_notes = tuple(
            f"{section.value} evidence is {reason}" for _, section, reason, _ in gaps
        )
        return _PolicyEvaluation(
            status=ReportStatus.INSUFFICIENT_EVIDENCE,
            rating=None,
            confidence=0.0,
            confidence_explanation=f"Insufficient evidence: {reasons}.",
            quality_flags=quality_flags,
            quality_notes=tuple(dict.fromkeys((*missing_notes, *gap_notes))),
            missing_modules=tuple(gap[0] for gap in gaps),
            gaps=tuple(gaps),
        )
    if not outputs:
        raise ValueError("sufficient evidence requires completed role outputs")
    risk_output = outputs_by_role[RoleName.RISK_DECISION]
    proposal = risk_output.proposal
    if proposal is None:
        raise ValueError("risk decision output requires a rating proposal")
    cap, quality_notes = _confidence_cap(evidence, outputs)
    if missing_sections:
        cap = min(cap, 0.80)
        quality_notes += missing_notes
    confidence = float(min(proposal.confidence, cap))
    explanation = proposal.confidence_explanation
    if proposal.confidence > cap:
        explanation = (
            f"{explanation} Confidence capped at {cap:.2f} by deterministic "
            "evidence-quality policy."
        )
    return _PolicyEvaluation(
        status=ReportStatus.COMPLETE,
        rating=proposal.rating,
        confidence=confidence,
        confidence_explanation=explanation,
        quality_flags=quality_flags,
        quality_notes=quality_notes,
        missing_modules=(),
        gaps=(),
    )


def _all_missing_sections(
    snapshot: ResearchSnapshot,
    gaps: tuple[tuple[RoleName, ResearchSectionKind, str, str], ...],
) -> tuple[ResearchSectionKind, ...]:
    missing = {item.kind for item in snapshot.missing_sections}
    missing.update(gap[1] for gap in gaps)
    return tuple(kind for kind in RESEARCH_SECTION_ORDER if kind in missing)


def _canonical_recovery_actions(
    missing_sections: tuple[ResearchSectionKind, ...],
) -> tuple[str, ...]:
    return tuple(f"refresh_{kind.value}_evidence" for kind in missing_sections)


def _missing_section_notes(
    missing_sections: tuple[ResearchSectionKind, ...],
) -> tuple[str, ...]:
    return tuple(f"{kind.value} section is missing" for kind in missing_sections)


def _unusable_reason(evidence: tuple[EvidenceItem, ...]) -> str:
    if not evidence:
        return "no_evidence"
    for flag in (
        ResearchQualityFlag.EXPIRED,
        ResearchQualityFlag.STALE,
        ResearchQualityFlag.UNVERIFIED,
    ):
        if all(flag in item.quality_flags for item in evidence):
            return flag.value
    return "no_eligible_evidence"


def _confidence_cap(
    evidence: tuple[EvidenceItem, ...],
    outputs: tuple[RoleOutput, ...],
) -> tuple[float, tuple[str, ...]]:
    cap = 0.95
    notes: list[str] = []
    flags = {flag for item in evidence for flag in item.quality_flags}
    policies = (
        (ResearchQualityFlag.CONFLICTING, 0.60, "conflicting evidence"),
        (ResearchQualityFlag.EXPIRED, 0.50, "expired evidence included"),
        (ResearchQualityFlag.STALE, 0.50, "stale evidence included"),
        (ResearchQualityFlag.UNVERIFIED, 0.50, "unverified evidence included"),
        (ResearchQualityFlag.PARTIAL, 0.70, "partial evidence"),
        (ResearchQualityFlag.DEGRADED_SOURCE, 0.75, "degraded source evidence"),
    )
    for flag, limit, note in policies:
        if flag in flags:
            cap = min(cap, limit)
            notes.append(note)
    stances_by_evidence: dict[str, set[EvidenceStance]] = {}
    for output in outputs:
        for claim in output.claims:
            for evidence_id in claim.evidence_ids:
                stances_by_evidence.setdefault(evidence_id, set()).add(claim.stance)
    if any(
        EvidenceStance.SUPPORT in stances and EvidenceStance.OPPOSE in stances
        for stances in stances_by_evidence.values()
    ):
        cap = min(cap, 0.60)
        if "conflicting evidence" not in notes:
            notes.append("conflicting evidence stances")
    if cap == 0.95:
        notes.append("clean evidence confidence ceiling")
    return cap, tuple(notes)


def _validate_safe_report_text(report: ResearchReport) -> None:
    values = (
        report.confidence_explanation,
        report.disclaimer,
        *(claim.text for output in report.role_outputs for claim in output.claims),
        *(output.summary for output in report.role_outputs),
    )
    if any(contains_forbidden_financial_action(value) for value in values):
        raise ValueError("research report contains forbidden financial action text")


def _validate_report_budget(value: object) -> None:
    _validate_report_shape(value)
    if len(_canonical_json_bytes(value)) > MAX_RESEARCH_REPORT_BYTES:
        raise ValueError("research report exceeds the byte limit")


def _validate_report_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        if depth > MAX_RESEARCH_REPORT_DEPTH:
            raise ValueError("research report exceeds the depth limit")
        nodes += 1
        if nodes > MAX_RESEARCH_REPORT_NODES:
            raise ValueError("research report exceeds the node limit")
        if isinstance(current, Mapping):
            if any(type(key) is not str for key in current):
                raise ValueError("research report keys must be strings")
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            stack.extend((child, depth + 1) for child in current)
        elif current is not None and type(current) not in {str, int, float, bool}:
            raise ValueError("research report contains a non-JSON value")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _content_id(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(value)).hexdigest()}"
