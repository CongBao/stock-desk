from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
import pytest

from stock_desk.analysis.evidence import (
    Claim,
    EvidenceGraph,
    EvidenceItem,
    EvidenceStance,
)
from stock_desk.analysis.snapshot import (
    ResearchQualityFlag,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)


UTC = timezone.utc
FROZEN_AT = datetime(2025, 7, 6, 9, tzinfo=UTC)
FETCHED_AT = FROZEN_AT - timedelta(minutes=5)
DATA_CUTOFF = FETCHED_AT - timedelta(hours=1)
DIGEST = "sha256:" + "a" * 64
UNKNOWN_DIGEST = "sha256:" + "f" * 64


def _section(
    kind: ResearchSectionKind = ResearchSectionKind.FUNDAMENTALS,
    *,
    quality_flags: tuple[ResearchQualityFlag, ...] = (),
) -> ResearchSection:
    return ResearchSection(
        kind=kind,
        canonical_source="tushare",
        source_record=f"{kind.value}:2025Q1",
        source_url="https://example.com/reports/2025Q1",
        published_at=DATA_CUTOFF,
        data_cutoff=DATA_CUTOFF,
        fetched_at=FETCHED_AT,
        dataset_version=DIGEST,
        quality_flags=quality_flags,
        content={"metric": "净利润", "value": "123.45"},
    )


def _snapshot(section: ResearchSection | None = None) -> ResearchSnapshot:
    selected = section or _section()
    sections = tuple(
        selected if kind is selected.kind else _section(kind)
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


def _evidence(
    *,
    snapshot: ResearchSnapshot | None = None,
    section: ResearchSection | None = None,
    excerpt: str = "2025 年第一季度净利润同比改善。",
) -> EvidenceItem:
    selected_section = section or _section()
    selected_snapshot = snapshot or _snapshot(selected_section)
    return EvidenceItem.create(
        snapshot_id=selected_snapshot.snapshot_id,
        section=selected_section,
        excerpt=excerpt,
    )


def _claim(evidence: EvidenceItem, stance: EvidenceStance) -> Claim:
    return Claim(
        text="盈利质量改善",
        evidence_ids=(evidence.evidence_id,),
        stance=stance,
    )


def test_evidence_identity_is_content_addressed_stable_and_sensitive() -> None:
    snapshot = _snapshot()
    section = snapshot.section(ResearchSectionKind.FUNDAMENTALS)
    assert section is not None
    first = _evidence(snapshot=snapshot, section=section)
    repeated = _evidence(snapshot=snapshot, section=section)
    changed = _evidence(
        snapshot=snapshot,
        section=section,
        excerpt="同比改善，但现金流仍需观察。",
    )

    assert first.evidence_id == repeated.evidence_id
    assert first.canonical_json_bytes() == repeated.canonical_json_bytes()
    assert first.evidence_id.startswith("sha256:")
    assert changed.evidence_id != first.evidence_id


def test_evidence_rejects_forged_content_address() -> None:
    evidence = _evidence()
    forged = evidence.model_copy(update={"evidence_id": UNKNOWN_DIGEST})

    with pytest.raises(ValidationError, match="evidence_id"):
        EvidenceItem.model_validate(forged.model_dump(mode="python"))


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///tmp/source",
        "https://user:password@example.com/report",
    ],
)
def test_direct_evidence_validation_rejects_unsafe_source_url(url: str) -> None:
    evidence = _evidence()
    payload = evidence.model_dump(mode="python")
    payload["source_url"] = url

    with pytest.raises(ValidationError, match="URL"):
        EvidenceItem.model_validate(payload)


def test_direct_evidence_validation_rejects_invalid_time_order() -> None:
    evidence = _evidence()
    payload = evidence.model_dump(mode="python")
    payload["data_cutoff"] = evidence.fetched_at + timedelta(seconds=1)

    with pytest.raises(ValidationError, match="data cutoff"):
        EvidenceItem.model_validate(payload)


def test_direct_evidence_validation_rejects_duplicate_quality_flags() -> None:
    evidence = _evidence()
    payload = evidence.model_dump(mode="python")
    payload["quality_flags"] = (
        ResearchQualityFlag.STALE,
        ResearchQualityFlag.STALE,
    )

    with pytest.raises(ValidationError, match="duplicate"):
        EvidenceItem.model_validate(payload)


def test_graph_rejects_well_formed_unknown_evidence_reference() -> None:
    evidence = _evidence()
    unknown = Claim(
        text="盈利改善",
        evidence_ids=(UNKNOWN_DIGEST,),
        stance=EvidenceStance.SUPPORT,
    )

    with pytest.raises(ValidationError, match="existing evidence"):
        EvidenceGraph(
            snapshot_id=evidence.snapshot_id,
            evidence_items=(evidence,),
            claims=(unknown,),
        )


def test_graph_rejects_cross_snapshot_and_duplicate_evidence() -> None:
    evidence = _evidence()
    other_snapshot = _snapshot().model_copy(
        update={"snapshot_id": "sha256:" + "b" * 64}
    )
    cross_snapshot = _evidence(snapshot=other_snapshot)

    with pytest.raises(ValidationError, match="snapshot"):
        EvidenceGraph(
            snapshot_id=evidence.snapshot_id,
            evidence_items=(cross_snapshot,),
            claims=(_claim(cross_snapshot, EvidenceStance.SUPPORT),),
        )
    with pytest.raises(ValidationError, match="duplicate"):
        EvidenceGraph(
            snapshot_id=evidence.snapshot_id,
            evidence_items=(evidence, evidence),
            claims=(_claim(evidence, EvidenceStance.SUPPORT),),
        )


def test_claim_requires_unique_nonempty_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        Claim(text="无引用", evidence_ids=(), stance=EvidenceStance.UNCERTAIN)
    with pytest.raises(ValidationError, match="duplicate"):
        Claim(
            text="重复引用",
            evidence_ids=(DIGEST, DIGEST),
            stance=EvidenceStance.UNCERTAIN,
        )


def test_support_oppose_uncertain_and_expired_evidence_can_coexist() -> None:
    section = _section(quality_flags=(ResearchQualityFlag.EXPIRED,))
    snapshot = _snapshot(section)
    expired = _evidence(snapshot=snapshot, section=section)
    support = _claim(expired, EvidenceStance.SUPPORT)
    oppose = Claim(
        text="现金流质量承压",
        evidence_ids=(expired.evidence_id,),
        stance=EvidenceStance.OPPOSE,
    )
    uncertain = Claim(
        text="改善能否持续仍不确定",
        evidence_ids=(expired.evidence_id,),
        stance=EvidenceStance.UNCERTAIN,
    )

    graph = EvidenceGraph(
        snapshot_id=snapshot.snapshot_id,
        evidence_items=(expired,),
        claims=(support, oppose, uncertain),
    )

    assert ResearchQualityFlag.EXPIRED in expired.quality_flags
    assert graph.evidence_for(support) == (expired,)
    assert tuple(claim.stance for claim in graph.claims) == (
        EvidenceStance.SUPPORT,
        EvidenceStance.OPPOSE,
        EvidenceStance.UNCERTAIN,
    )


def test_evidence_graph_models_are_immutable() -> None:
    evidence = _evidence()
    claim = _claim(evidence, EvidenceStance.SUPPORT)
    graph = EvidenceGraph(
        snapshot_id=evidence.snapshot_id,
        evidence_items=(evidence,),
        claims=(claim,),
    )

    with pytest.raises(ValidationError, match="frozen"):
        evidence.excerpt = "changed"
    with pytest.raises(ValidationError, match="frozen"):
        claim.text = "changed"
    with pytest.raises(ValidationError, match="frozen"):
        graph.snapshot_id = UNKNOWN_DIGEST
