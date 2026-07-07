from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stock_desk.analysis.evidence import (
    Claim,
    EvidenceGraph,
    EvidenceItem,
    EvidenceStance,
)
from stock_desk.analysis.providers.base import ModelUsage
from stock_desk.analysis.report import (
    ReportInputValidationError,
    ReportStatus,
    ResearchReport,
    ResearchReportBuilder,
    StageFailure,
)
from stock_desk.analysis.roles import RoleName, RoleOutput
from stock_desk.analysis.snapshot import (
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.analysis.workflow import (
    WorkflowStageStatus,
    WorkflowStageTrace,
)


UTC = timezone.utc
FROZEN_AT = datetime(2025, 7, 6, 9, tzinfo=UTC)
FETCHED_AT = FROZEN_AT - timedelta(minutes=5)
DATA_CUTOFF = FETCHED_AT - timedelta(hours=1)
VERSION = "sha256:" + "a" * 64


def frozen_snapshot() -> ResearchSnapshot:
    sections = tuple(
        ResearchSection(  # type: ignore[call-arg]
            kind=kind,
            canonical_source="fixture",
            source_record=f"{kind.value}:record-1",
            source_url=f"https://example.com/{kind.value}/record-1",
            published_at=(
                DATA_CUTOFF
                if kind in {ResearchSectionKind.ANNOUNCEMENTS, ResearchSectionKind.NEWS}
                else None
            ),
            data_cutoff=DATA_CUTOFF,
            fetched_at=FETCHED_AT,
            dataset_version=VERSION,
            quality_flags=(),
            content={"kind": kind.value, "value": "fixture"},
        )
        for kind in (
            ResearchSectionKind.MARKET,
            ResearchSectionKind.FUNDAMENTALS,
            ResearchSectionKind.ANNOUNCEMENTS,
            ResearchSectionKind.NEWS,
        )
    )
    return ResearchSnapshot.create(
        symbol="600000.SH",
        frozen_at=FROZEN_AT,
        sections=sections,
        missing_sections=(),
    )


def evidence_graph(snapshot: ResearchSnapshot) -> EvidenceGraph:
    return EvidenceGraph(
        snapshot=snapshot,
        evidence_items=tuple(
            EvidenceItem.create(
                snapshot=snapshot,
                section_kind=section.kind,
                excerpt=f"registered {section.kind.value} evidence",
            )
            for section in snapshot.sections
        ),
        claims=(),
    )


def role_output(
    role: RoleName,
    snapshot: ResearchSnapshot,
    evidence: EvidenceItem,
) -> RoleOutput:
    return RoleOutput(
        role=role,
        snapshot_id=snapshot.snapshot_id,
        summary=f"{role.value} summary",
        claims=(
            Claim(
                text=f"{role.value} claim",
                evidence_ids=(evidence.evidence_id,),
                stance=EvidenceStance.SUPPORT,
            ),
        ),
    )


def trace(role: RoleName, ordinal: int) -> WorkflowStageTrace:
    return WorkflowStageTrace(
        role=role,
        status=WorkflowStageStatus.SUCCEEDED,
        started_at=FROZEN_AT + timedelta(seconds=ordinal),
        ended_at=FROZEN_AT + timedelta(seconds=ordinal + 1),
        duration_seconds=1.0,
        provider="stub-provider",
        model="stub-model",
        template_version=f"{role.value}-v1",
        template_hash="sha256:" + f"{ordinal + 1:x}".zfill(64),
        request_hash="sha256:" + f"{ordinal + 11:x}".zfill(64),
        usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )


def test_noncritical_failure_builds_content_addressed_partial_without_rating() -> None:
    snapshot = frozen_snapshot()
    graph = evidence_graph(snapshot)
    market, fundamentals = graph.evidence_items[:2]
    completed_roles = (
        RoleName.TECHNICAL,
        RoleName.FUNDAMENTAL_NEWS,
        RoleName.BEAR,
    )
    outputs = (
        role_output(RoleName.TECHNICAL, snapshot, market),
        role_output(RoleName.FUNDAMENTAL_NEWS, snapshot, fundamentals),
        role_output(RoleName.BEAR, snapshot, market),
    )
    traces = tuple(trace(role, ordinal) for ordinal, role in enumerate(completed_roles))

    report = ResearchReportBuilder().build_partial(
        snapshot=snapshot,
        evidence_graph=graph,
        outputs=outputs,
        trace=traces,
        failures=(
            StageFailure(
                stage=RoleName.BULL,
                code="model_authentication",
                attempt_count=1,
            ),
        ),
    )

    assert report.status is ReportStatus.PARTIAL
    assert report.rating is None
    assert report.confidence == 0.0
    assert tuple(item.role for item in report.role_outputs) == completed_roles
    assert tuple(item.role for item in report.model_metadata) == completed_roles
    assert report.missing_modules == (RoleName.BULL, RoleName.RISK_DECISION)
    assert report.failed_modules == (RoleName.BULL,)
    assert report.blocked_modules == (RoleName.RISK_DECISION,)
    assert report.stage_failures[0].code == "model_authentication"
    assert report.stage_failures[0].attempt_count == 1
    assert report.risks == ()
    assert tuple(item.stage for item in report.retry_actions) == (RoleName.BULL,)
    assert ResearchReport.model_validate_json(report.model_dump_json()) == report
    assert report.report_id.startswith("sha256:")


def test_first_wave_total_failure_still_builds_partial_without_fake_evidence() -> None:
    snapshot = frozen_snapshot()
    graph = evidence_graph(snapshot)

    report = ResearchReportBuilder().build_partial(
        snapshot=snapshot,
        evidence_graph=graph,
        outputs=(),
        trace=(),
        failures=(
            StageFailure(
                stage=RoleName.TECHNICAL,
                code="model_timeout",
                attempt_count=3,
            ),
            StageFailure(
                stage=RoleName.FUNDAMENTAL_NEWS,
                code="model_authentication",
                attempt_count=1,
            ),
        ),
    )

    assert report.status is ReportStatus.PARTIAL
    assert report.rating is None
    assert report.role_outputs == ()
    assert report.model_metadata == ()
    assert report.evidence_items == ()
    assert report.risks == ()
    assert report.failed_modules == (
        RoleName.TECHNICAL,
        RoleName.FUNDAMENTAL_NEWS,
    )
    assert report.blocked_modules == (
        RoleName.BULL,
        RoleName.BEAR,
        RoleName.RISK_DECISION,
    )
    assert report.generated_at == snapshot.frozen_at


def test_partial_report_rejects_noncanonical_or_unpaired_completed_stages() -> None:
    snapshot = frozen_snapshot()
    graph = evidence_graph(snapshot)
    market, fundamentals = graph.evidence_items[:2]
    technical = role_output(RoleName.TECHNICAL, snapshot, market)
    fundamental = role_output(RoleName.FUNDAMENTAL_NEWS, snapshot, fundamentals)

    with pytest.raises(ReportInputValidationError):
        ResearchReportBuilder().build_partial(
            snapshot=snapshot,
            evidence_graph=graph,
            outputs=(fundamental, technical),
            trace=(
                trace(RoleName.FUNDAMENTAL_NEWS, 0),
                trace(RoleName.TECHNICAL, 1),
            ),
            failures=(
                StageFailure(
                    stage=RoleName.BULL,
                    code="model_invalid_response",
                    attempt_count=1,
                ),
            ),
        )
    with pytest.raises(ReportInputValidationError):
        ResearchReportBuilder().build_partial(
            snapshot=snapshot,
            evidence_graph=graph,
            outputs=(technical,),
            trace=(trace(RoleName.FUNDAMENTAL_NEWS, 0),),
            failures=(
                StageFailure(
                    stage=RoleName.BULL,
                    code="model_invalid_response",
                    attempt_count=1,
                ),
            ),
        )
