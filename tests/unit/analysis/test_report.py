from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import cast

from pydantic import JsonValue, ValidationError
import pytest

from stock_desk.analysis.evidence import (
    Claim,
    EvidenceGraph,
    EvidenceItem,
    EvidenceStance,
)
from stock_desk.analysis.providers.base import ModelUsage
from stock_desk.analysis.rating import Rating, RatingProposal
from stock_desk.analysis.report import (
    MAX_RESEARCH_REPORT_BYTES,
    REPORT_DISCLAIMER,
    ReportInputValidationError,
    ReportStatus,
    ReportValidationError,
    ResearchReport,
    ResearchReportBuilder,
    parse_research_report_json,
)
from stock_desk.analysis.roles import ROLE_ORDER, RoleName, RoleOutput
from stock_desk.analysis.snapshot import (
    MissingResearchSection,
    ResearchMissingReason,
    ResearchQualityFlag,
    ResearchRouteMetadata,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.analysis.workflow import (
    WorkflowResult,
    WorkflowStageStatus,
    WorkflowStageTrace,
)


UTC = timezone.utc
FROZEN_AT = datetime(2025, 7, 6, 9, tzinfo=UTC)
FETCHED_AT = FROZEN_AT - timedelta(minutes=5)
DATA_CUTOFF = FETCHED_AT - timedelta(hours=1)
VERSION = "sha256:" + "a" * 64
SYMBOL = "600000.SH"


def section(
    kind: ResearchSectionKind,
    *,
    flags: tuple[ResearchQualityFlag, ...] = (),
    value: str | None = None,
) -> ResearchSection:
    route: ResearchRouteMetadata | None = None
    if ResearchQualityFlag.DEGRADED_SOURCE in flags:
        route = ResearchRouteMetadata(
            selected_source="fixture",
            attempted_sources=("primary",),
            failure_reasons=(ResearchMissingReason.TIMEOUT,),
            primary_failure_reason=ResearchMissingReason.TIMEOUT,
            degraded_from="primary",
        )
    return ResearchSection(  # type: ignore[call-arg]
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
        quality_flags=flags,
        route=route,
        content={"kind": kind.value, "value": value or f"{kind.value}-fixture"},
    )


def snapshot(
    *,
    flags: dict[ResearchSectionKind, tuple[ResearchQualityFlag, ...]] | None = None,
    missing: frozenset[ResearchSectionKind] = frozenset(),
    market_value: str | None = None,
) -> ResearchSnapshot:
    configured_flags = flags or {}
    sections: list[ResearchSection] = []
    missing_sections: list[MissingResearchSection] = []
    for kind in ResearchSectionKind:
        if kind in missing:
            missing_sections.append(
                MissingResearchSection(
                    kind=kind,
                    reason=ResearchMissingReason.NO_DATA,
                    checked_at=FETCHED_AT,
                    attempted_sources=("fixture",),
                    recovery_code=f"refresh_{kind.value}",
                )
            )
        else:
            sections.append(
                section(
                    kind,
                    flags=configured_flags.get(kind, ()),
                    value=(
                        market_value if kind is ResearchSectionKind.MARKET else None
                    ),
                )
            )
    return ResearchSnapshot.create(
        symbol=SYMBOL,
        frozen_at=FROZEN_AT,
        sections=tuple(sections),
        missing_sections=tuple(missing_sections),
    )


def graph(value: ResearchSnapshot) -> EvidenceGraph:
    return EvidenceGraph(
        snapshot=value,
        evidence_items=tuple(
            EvidenceItem.create(
                snapshot=value,
                section_kind=item.kind,
                excerpt=json.dumps(
                    item.content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            for item in value.sections
        ),
        claims=(),
    )


def evidence_for(
    evidence_graph: EvidenceGraph,
    kind: ResearchSectionKind,
) -> EvidenceItem:
    return next(
        item for item in evidence_graph.evidence_items if item.section_kind is kind
    )


def role_claim(
    role: RoleName,
    evidence_id: str,
    stance: EvidenceStance,
) -> Claim:
    return Claim(
        text=f"{role.value} evidence-backed claim",
        evidence_ids=(evidence_id,),
        stance=stance,
    )


def workflow_result(
    frozen: ResearchSnapshot,
    evidence_graph: EvidenceGraph,
    *,
    rating: Rating = Rating.BULLISH,
    confidence: float = 0.9,
    confidence_explanation: str = "Current registered evidence supports this confidence.",
    risk_model: str = "stub-model-v1",
    risk_template: str = "risk_decision-v2",
) -> WorkflowResult:
    market_id = evidence_for(evidence_graph, ResearchSectionKind.MARKET).evidence_id
    fundamental_item = next(
        (
            item
            for item in evidence_graph.evidence_items
            if item.section_kind is ResearchSectionKind.FUNDAMENTALS
        ),
        None,
    )
    if fundamental_item is None:
        fundamental_item = evidence_for(evidence_graph, ResearchSectionKind.NEWS)
    fundamental_id = fundamental_item.evidence_id
    outputs = (
        RoleOutput(
            role=RoleName.TECHNICAL,
            snapshot_id=frozen.snapshot_id,
            summary="Technical summary",
            claims=(
                role_claim(
                    RoleName.TECHNICAL,
                    market_id,
                    EvidenceStance.SUPPORT,
                ),
            ),
        ),
        RoleOutput(
            role=RoleName.FUNDAMENTAL_NEWS,
            snapshot_id=frozen.snapshot_id,
            summary="Fundamental and news summary",
            claims=(
                role_claim(
                    RoleName.FUNDAMENTAL_NEWS,
                    fundamental_id,
                    EvidenceStance.UNCERTAIN,
                ),
            ),
        ),
        RoleOutput(
            role=RoleName.BULL,
            snapshot_id=frozen.snapshot_id,
            summary="Bull summary",
            claims=(role_claim(RoleName.BULL, market_id, EvidenceStance.SUPPORT),),
        ),
        RoleOutput(
            role=RoleName.BEAR,
            snapshot_id=frozen.snapshot_id,
            summary="Bear summary",
            claims=(role_claim(RoleName.BEAR, fundamental_id, EvidenceStance.OPPOSE),),
        ),
        RoleOutput(
            role=RoleName.RISK_DECISION,
            snapshot_id=frozen.snapshot_id,
            summary="Risk decision draft",
            claims=(
                role_claim(
                    RoleName.RISK_DECISION,
                    fundamental_id,
                    EvidenceStance.UNCERTAIN,
                ),
            ),
            proposal=RatingProposal(
                rating=rating,
                confidence=confidence,
                confidence_explanation=confidence_explanation,
            ),
        ),
    )
    trace = tuple(
        WorkflowStageTrace(
            role=role,
            status=WorkflowStageStatus.SUCCEEDED,
            started_at=FROZEN_AT + timedelta(seconds=index),
            ended_at=FROZEN_AT + timedelta(seconds=index + 1),
            duration_seconds=1.0,
            provider="stub-provider",
            model=(risk_model if role is RoleName.RISK_DECISION else "stub-model-v1"),
            template_version=(
                risk_template if role is RoleName.RISK_DECISION else f"{role.value}-v1"
            ),
            template_hash="sha256:" + f"{index + 1:x}".zfill(64),
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        for index, role in enumerate(ROLE_ORDER)
    )
    return WorkflowResult(
        snapshot_id=frozen.snapshot_id,
        outputs=outputs,
        trace=trace,
        evidence_ids=tuple(item.evidence_id for item in evidence_graph.evidence_items),
    )


def build_report(
    *,
    frozen: ResearchSnapshot | None = None,
    evidence_graph: EvidenceGraph | None = None,
    workflow: WorkflowResult | None = None,
    **workflow_options: object,
) -> ResearchReport:
    selected_snapshot = frozen or snapshot()
    selected_graph = evidence_graph or graph(selected_snapshot)
    selected_workflow = workflow or workflow_result(
        selected_snapshot,
        selected_graph,
        **workflow_options,
    )
    return ResearchReportBuilder().build(
        snapshot=selected_snapshot,
        evidence_graph=selected_graph,
        workflow=selected_workflow,
    )


def recreate_report(report: ResearchReport, **updates: object) -> ResearchReport:
    fields: dict[str, object] = {
        "snapshot_id": report.snapshot_id,
        "status": report.status,
        "rating": report.rating,
        "confidence": report.confidence,
        "confidence_explanation": report.confidence_explanation,
        "core_judgments": report.core_judgments,
        "bull_claims": report.bull_claims,
        "bear_claims": report.bear_claims,
        "risks": report.risks,
        "evidence_items": report.evidence_items,
        "role_outputs": report.role_outputs,
        "model_metadata": report.model_metadata,
        "quality_flags": report.quality_flags,
        "quality_notes": report.quality_notes,
        "missing_modules": report.missing_modules,
        "missing_sections": report.missing_sections,
        "recovery_actions": report.recovery_actions,
        "generated_at": report.generated_at,
        "disclaimer": report.disclaimer,
    }
    fields.update(updates)
    return ResearchReport.create(**fields)  # type: ignore[arg-type]


def test_complete_report_contains_one_rating_balanced_claims_and_run_metadata() -> None:
    report = build_report()

    assert report.status is ReportStatus.COMPLETE
    assert report.rating is Rating.BULLISH
    assert report.rating.label_zh == "看多"
    assert report.confidence == 0.9
    assert report.confidence_explanation
    assert report.core_judgments
    assert report.bull_claims
    assert report.bear_claims
    assert report.risks
    assert {claim.stance for claim in report.all_claims} == {
        EvidenceStance.SUPPORT,
        EvidenceStance.OPPOSE,
        EvidenceStance.UNCERTAIN,
    }
    assert report.missing_modules == ()
    assert report.missing_sections == ()
    assert report.disclaimer == REPORT_DISCLAIMER
    assert tuple(item.role for item in report.role_outputs) == ROLE_ORDER
    assert tuple(item.role for item in report.model_metadata) == ROLE_ORDER
    assert report.model_metadata[-1].model == "stub-model-v1"
    assert report.model_metadata[-1].template_version == "risk_decision-v2"
    registered = {item.evidence_id for item in report.evidence_items}
    assert all(
        evidence_id in registered
        for claim in report.all_claims
        for evidence_id in claim.evidence_ids
    )


def test_clean_evidence_still_caps_model_reported_confidence() -> None:
    report = build_report(confidence=1.0)

    assert report.confidence == 0.95
    assert "capped" in report.confidence_explanation.lower()


@pytest.mark.parametrize(
    "flag",
    [
        ResearchQualityFlag.STALE,
        ResearchQualityFlag.UNVERIFIED,
        ResearchQualityFlag.EXPIRED,
    ],
)
def test_only_unusable_technical_evidence_suppresses_rating(
    flag: ResearchQualityFlag,
) -> None:
    frozen = snapshot(flags={ResearchSectionKind.MARKET: (flag,)})
    report = build_report(frozen=frozen)

    assert report.status is ReportStatus.INSUFFICIENT_EVIDENCE
    assert report.rating is None
    assert report.confidence == 0.0
    assert report.missing_modules == (RoleName.TECHNICAL,)
    assert report.missing_sections == (ResearchSectionKind.MARKET,)
    assert report.recovery_actions
    assert flag.value in report.confidence_explanation


def test_missing_fundamentals_cannot_be_hidden_by_news_evidence() -> None:
    frozen = snapshot(missing=frozenset({ResearchSectionKind.FUNDAMENTALS}))
    report = build_report(frozen=frozen)

    assert report.status is ReportStatus.INSUFFICIENT_EVIDENCE
    assert report.rating is None
    assert report.missing_modules == (RoleName.FUNDAMENTAL_NEWS,)
    assert report.missing_sections == (ResearchSectionKind.FUNDAMENTALS,)
    assert "refresh_fundamentals_evidence" in report.recovery_actions


def test_noncritical_snapshot_gaps_are_preserved_without_suppressing_rating() -> None:
    frozen = snapshot(missing=frozenset({ResearchSectionKind.NEWS}))
    report = build_report(frozen=frozen)

    assert report.status is ReportStatus.COMPLETE
    assert report.rating is Rating.BULLISH
    assert report.missing_modules == ()
    assert report.missing_sections == (ResearchSectionKind.NEWS,)
    assert report.recovery_actions == ("refresh_news_evidence",)
    assert "news" in " ".join(report.quality_notes).lower()


@pytest.mark.parametrize(
    "missing,expected_modules,expected_sections",
    [
        (
            frozenset({ResearchSectionKind.MARKET}),
            (RoleName.TECHNICAL,),
            (ResearchSectionKind.MARKET,),
        ),
        (
            frozenset(
                {
                    ResearchSectionKind.FUNDAMENTALS,
                    ResearchSectionKind.ANNOUNCEMENTS,
                    ResearchSectionKind.NEWS,
                }
            ),
            (RoleName.FUNDAMENTAL_NEWS,),
            (
                ResearchSectionKind.FUNDAMENTALS,
                ResearchSectionKind.ANNOUNCEMENTS,
                ResearchSectionKind.NEWS,
            ),
        ),
    ],
)
def test_preflight_builds_deterministic_insufficient_report_without_impossible_workflow(
    missing: frozenset[ResearchSectionKind],
    expected_modules: tuple[RoleName, ...],
    expected_sections: tuple[ResearchSectionKind, ...],
) -> None:
    frozen = snapshot(missing=missing)
    registered = graph(frozen)
    builder = ResearchReportBuilder()

    first = builder.build_insufficient(
        snapshot=frozen,
        evidence_graph=registered,
    )
    second = builder.build_insufficient(
        snapshot=frozen,
        evidence_graph=registered,
    )

    assert first == second
    assert first.status is ReportStatus.INSUFFICIENT_EVIDENCE
    assert first.rating is None
    assert first.confidence == 0.0
    assert first.missing_modules == expected_modules
    assert first.missing_sections == expected_sections
    assert first.recovery_actions
    assert first.role_outputs == ()
    assert first.model_metadata == ()
    assert first.all_claims == ()


def test_preflight_insufficient_entry_rejects_sufficient_critical_evidence() -> None:
    frozen = snapshot()

    with pytest.raises(ReportInputValidationError):
        ResearchReportBuilder().build_insufficient(
            snapshot=frozen,
            evidence_graph=graph(frozen),
        )


def test_conflicting_degraded_and_partial_evidence_deterministically_reduce_confidence() -> (
    None
):
    frozen = snapshot(
        flags={
            ResearchSectionKind.MARKET: (ResearchQualityFlag.CONFLICTING,),
            ResearchSectionKind.FUNDAMENTALS: (
                ResearchQualityFlag.DEGRADED_SOURCE,
                ResearchQualityFlag.PARTIAL,
            ),
        }
    )
    report = build_report(frozen=frozen, confidence=0.99)

    assert report.status is ReportStatus.COMPLETE
    assert report.rating is Rating.BULLISH
    assert report.confidence <= 0.6
    assert set(report.quality_flags) == {
        ResearchQualityFlag.CONFLICTING,
        ResearchQualityFlag.DEGRADED_SOURCE,
        ResearchQualityFlag.PARTIAL,
    }
    assert report.quality_notes
    assert "conflict" in " ".join(report.quality_notes).lower()


def test_empty_evidence_and_unknown_claim_references_fail_closed() -> None:
    frozen = snapshot()
    registered = graph(frozen)
    workflow = workflow_result(frozen, registered)
    empty_graph = EvidenceGraph(snapshot=frozen, evidence_items=(), claims=())

    with pytest.raises(ReportInputValidationError):
        build_report(
            frozen=frozen,
            evidence_graph=empty_graph,
            workflow=workflow.model_copy(update={"evidence_ids": ()}),
        )

    unknown = "sha256:" + "f" * 64
    technical = workflow.outputs[0].model_copy(
        update={
            "claims": (
                Claim(
                    text="Unknown claim",
                    evidence_ids=(unknown,),
                    stance=EvidenceStance.SUPPORT,
                ),
            )
        }
    )
    malformed = workflow.model_copy(
        update={"outputs": (technical, *workflow.outputs[1:])}
    )
    with pytest.raises(ReportInputValidationError):
        build_report(frozen=frozen, evidence_graph=registered, workflow=malformed)


@pytest.mark.parametrize(
    "malformation", ["duplicate-role", "trace-order", "evidence-list"]
)
def test_workflow_roles_trace_and_evidence_identity_must_be_exact(
    malformation: str,
) -> None:
    frozen = snapshot()
    registered = graph(frozen)
    workflow = workflow_result(frozen, registered)
    if malformation == "duplicate-role":
        altered = workflow.model_copy(
            update={
                "outputs": (
                    workflow.outputs[0],
                    workflow.outputs[0],
                    *workflow.outputs[2:],
                )
            }
        )
    elif malformation == "trace-order":
        altered = workflow.model_copy(
            update={
                "trace": (workflow.trace[1], workflow.trace[0], *workflow.trace[2:])
            }
        )
    else:
        altered = workflow.model_copy(
            update={"evidence_ids": workflow.evidence_ids[:-1]}
        )

    with pytest.raises(ReportInputValidationError):
        build_report(frozen=frozen, evidence_graph=registered, workflow=altered)


def test_cross_snapshot_and_forged_section_evidence_fail_closed() -> None:
    frozen = snapshot()
    registered = graph(frozen)
    other = snapshot(market_value="other-market")

    with pytest.raises(ReportInputValidationError):
        build_report(
            frozen=frozen,
            evidence_graph=graph(other),
            workflow=workflow_result(frozen, registered),
        )

    forged_item = registered.evidence_items[0].model_copy(
        update={"section_kind": ResearchSectionKind.FUNDAMENTALS}
    )
    forged_graph = EvidenceGraph.model_construct(
        snapshot=frozen,
        evidence_items=(forged_item, *registered.evidence_items[1:]),
        claims=(),
    )
    with pytest.raises(ReportInputValidationError):
        build_report(
            frozen=frozen,
            evidence_graph=forged_graph,
            workflow=workflow_result(frozen, registered),
        )


def test_report_model_rejects_cross_snapshot_evidence_even_with_recomputed_identity() -> (
    None
):
    report = build_report()
    other_snapshot = snapshot(market_value="other-market")
    other_item = graph(other_snapshot).evidence_items[0]
    rebound_claim = Claim(
        text="Rebound claim",
        evidence_ids=(other_item.evidence_id,),
        stance=EvidenceStance.SUPPORT,
    )

    with pytest.raises(ValidationError):
        recreate_report(
            report,
            core_judgments=(rebound_claim,),
            bull_claims=(rebound_claim,),
            bear_claims=(rebound_claim,),
            risks=(rebound_claim,),
            evidence_items=(other_item,),
        )


def test_report_model_rejects_role_output_refs_outside_report_evidence() -> None:
    report = build_report()
    unknown = "sha256:" + "f" * 64
    malformed_claim = Claim(
        text="Malformed role claim",
        evidence_ids=(unknown,),
        stance=EvidenceStance.SUPPORT,
    )
    technical = report.role_outputs[0].model_copy(update={"claims": (malformed_claim,)})

    with pytest.raises(ValidationError):
        recreate_report(
            report,
            role_outputs=(technical, *report.role_outputs[1:]),
        )


def test_report_model_requires_evidence_set_to_equal_role_reference_union() -> None:
    frozen = snapshot()
    registered = graph(frozen)
    report = build_report(frozen=frozen, evidence_graph=registered)
    news = evidence_for(registered, ResearchSectionKind.NEWS)

    with pytest.raises(ValidationError):
        recreate_report(
            report,
            evidence_items=(*report.evidence_items, news),
        )


def test_report_model_replays_review_evidence_allowlist() -> None:
    frozen = snapshot()
    registered = graph(frozen)
    report = build_report(frozen=frozen, evidence_graph=registered)
    news = evidence_for(registered, ResearchSectionKind.NEWS)
    bypass_claim = Claim(
        text="Review allowlist bypass",
        evidence_ids=(news.evidence_id,),
        stance=EvidenceStance.SUPPORT,
    )
    bull = report.role_outputs[2].model_copy(update={"claims": (bypass_claim,)})

    with pytest.raises(ValidationError):
        recreate_report(
            report,
            bull_claims=(bypass_claim,),
            evidence_items=(*report.evidence_items, news),
            role_outputs=(
                report.role_outputs[0],
                report.role_outputs[1],
                bull,
                report.role_outputs[3],
                report.role_outputs[4],
            ),
        )


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "目标价 20 元",
        "建议仓位 50%",
        "这是个性化投资建议",
        "立即下单买入",
        "Buy 100 shares now.",
        "Sell the entire position now.",
        "立即买入100股",
        "立即卖出全部持仓",
        "We recommend buy now.",
        "We recommend sell now.",
        "建议买入",
        "建议卖出",
        "Buy this stock.",
        "Sell.",
        "维持买入评级",
    ],
)
def test_report_rejects_financial_action_text_even_from_validated_role_output(
    unsafe_text: str,
) -> None:
    frozen = snapshot()
    registered = graph(frozen)
    workflow = workflow_result(frozen, registered)
    risk = workflow.outputs[-1].model_copy(
        update={
            "claims": (
                workflow.outputs[-1].claims[0].model_copy(update={"text": unsafe_text}),
            )
        }
    )
    malformed = workflow.model_copy(update={"outputs": (*workflow.outputs[:-1], risk)})

    with pytest.raises(ReportInputValidationError):
        build_report(frozen=frozen, evidence_graph=registered, workflow=malformed)


def test_report_identity_is_deterministic_and_sensitive_to_all_material_inputs() -> (
    None
):
    baseline = build_report()
    identical = build_report()
    changed_rating = build_report(rating=Rating.BEARISH)
    changed_model = build_report(risk_model="stub-model-v2")
    changed_template = build_report(risk_template="risk_decision-v3")
    changed_evidence_snapshot = snapshot(market_value="changed-market")
    changed_evidence = build_report(frozen=changed_evidence_snapshot)
    changed_quality_snapshot = snapshot(
        flags={ResearchSectionKind.MARKET: (ResearchQualityFlag.CONFLICTING,)}
    )
    changed_quality = build_report(frozen=changed_quality_snapshot)

    assert baseline.report_id == identical.report_id
    assert (
        len(
            {
                baseline.report_id,
                changed_rating.report_id,
                changed_model.report_id,
                changed_template.report_id,
                changed_evidence.report_id,
                changed_quality.report_id,
            }
        )
        == 6
    )


def test_report_model_recomputes_critical_gate_before_accepting_rehashed_complete() -> (
    None
):
    frozen = snapshot(flags={ResearchSectionKind.MARKET: (ResearchQualityFlag.STALE,)})
    report = build_report(frozen=frozen)
    assert report.status is ReportStatus.INSUFFICIENT_EVIDENCE

    with pytest.raises(ValidationError):
        recreate_report(
            report,
            status=ReportStatus.COMPLETE,
            rating=Rating.STRONG_BULLISH,
            confidence=1.0,
            missing_modules=(),
            missing_sections=(),
            recovery_actions=(),
        )


def test_report_model_recomputes_confidence_cap_and_risk_proposal_rating() -> None:
    frozen = snapshot(
        flags={ResearchSectionKind.MARKET: (ResearchQualityFlag.CONFLICTING,)}
    )
    report = build_report(frozen=frozen, confidence=1.0)
    assert report.confidence == 0.6

    with pytest.raises(ValidationError):
        recreate_report(report, confidence=1.0)
    with pytest.raises(ValidationError):
        recreate_report(report, rating=Rating.STRONG_BEARISH)


def test_report_model_rejects_empty_insufficient_shell_with_recomputed_identity() -> (
    None
):
    frozen = snapshot(missing=frozenset({ResearchSectionKind.MARKET}))
    report = ResearchReportBuilder().build_insufficient(
        snapshot=frozen,
        evidence_graph=graph(frozen),
    )

    with pytest.raises(ValidationError):
        recreate_report(
            report,
            evidence_items=(),
            missing_modules=(),
            missing_sections=(),
            recovery_actions=(),
            quality_notes=(),
        )


def test_report_model_binds_recovery_actions_to_missing_sections() -> None:
    complete = build_report()
    with pytest.raises(ValidationError):
        recreate_report(complete, recovery_actions=("buy_now",))

    frozen = snapshot(missing=frozenset({ResearchSectionKind.NEWS}))
    missing = build_report(frozen=frozen)
    with pytest.raises(ValidationError):
        recreate_report(missing, recovery_actions=("arbitrary_action",))
    with pytest.raises(ValidationError):
        recreate_report(missing, recovery_actions=("立即买入100股",))


def test_complete_report_generated_at_is_bound_to_latest_trace_end() -> None:
    report = build_report()

    with pytest.raises(ValidationError):
        recreate_report(
            report,
            generated_at=report.generated_at + timedelta(days=1),
        )


def test_report_json_round_trip_is_deeply_immutable_and_rejects_identity_tampering() -> (
    None
):
    report = build_report()
    restored = parse_research_report_json(report.canonical_json_bytes())

    assert restored == report
    with pytest.raises(ValidationError, match="frozen"):
        restored.confidence = 0.1
    with pytest.raises(ValidationError, match="frozen"):
        restored.role_outputs[0].summary = "mutated"

    payload = cast(dict[str, JsonValue], report.model_dump(mode="json"))
    payload["report_id"] = "sha256:" + "f" * 64
    with pytest.raises(ReportValidationError):
        parse_research_report_json(json.dumps(payload))


def test_report_parser_rejects_extra_nonfinite_and_complexity_attacks() -> None:
    report = build_report()
    payload = cast(dict[str, JsonValue], report.model_dump(mode="json"))
    payload["target_price"] = 20
    with pytest.raises(ReportValidationError):
        parse_research_report_json(json.dumps(payload))

    nonfinite = report.model_dump_json().replace('"confidence":0.9', '"confidence":NaN')
    with pytest.raises(ReportValidationError):
        parse_research_report_json(nonfinite)

    oversized = report.model_dump_json().replace(
        REPORT_DISCLAIMER,
        "x" * MAX_RESEARCH_REPORT_BYTES,
    )
    with pytest.raises(ReportValidationError):
        parse_research_report_json(oversized)

    too_many = payload.copy()
    too_many["core_judgments"] = [
        report.core_judgments[0].model_dump(mode="json") for _ in range(65)
    ]
    with pytest.raises(ReportValidationError):
        parse_research_report_json(json.dumps(too_many))
