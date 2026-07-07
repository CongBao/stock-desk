from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from pydantic import ValidationError
import pytest

from stock_desk.analysis.evidence import (
    Claim,
    EvidenceGraph,
    EvidenceItem,
    EvidenceStance,
    MAX_EVIDENCE_CLAIMS,
    MAX_EVIDENCE_GRAPH_BYTES,
    MAX_EVIDENCE_GRAPH_DEPTH,
    MAX_EVIDENCE_GRAPH_NODES,
    MAX_EVIDENCE_ITEMS,
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
        snapshot=selected_snapshot,
        section_kind=selected_section.kind,
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


def test_evidence_and_graph_json_round_trip() -> None:
    section = _section(quality_flags=(ResearchQualityFlag.STALE,))
    snapshot = _snapshot(section)
    evidence = _evidence(snapshot=snapshot, section=section)
    claim = _claim(evidence, EvidenceStance.SUPPORT)
    graph = EvidenceGraph(
        snapshot=snapshot,
        evidence_items=(evidence,),
        claims=(claim,),
    )

    restored_evidence = EvidenceItem.model_validate_json(
        evidence.model_dump_json(by_alias=True)
    )
    restored_graph = EvidenceGraph.model_validate_json(
        graph.model_dump_json(by_alias=True)
    )

    assert restored_evidence == evidence
    assert restored_graph == graph


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
    snapshot = _snapshot()
    evidence = _evidence(snapshot=snapshot)
    unknown = Claim(
        text="盈利改善",
        evidence_ids=(UNKNOWN_DIGEST,),
        stance=EvidenceStance.SUPPORT,
    )

    with pytest.raises(ValidationError, match="existing evidence"):
        EvidenceGraph(
            snapshot=snapshot,
            evidence_items=(evidence,),
            claims=(unknown,),
        )


def test_graph_rejects_cross_snapshot_relabel_and_duplicate_evidence() -> None:
    snapshot = _snapshot()
    evidence = _evidence(snapshot=snapshot)
    other_section = _section(quality_flags=(ResearchQualityFlag.STALE,))
    other_snapshot = _snapshot(other_section)
    cross_snapshot = _evidence(snapshot=other_snapshot, section=other_section)
    relabelled_payload = cross_snapshot.model_dump(mode="json")
    relabelled_payload["snapshot_id"] = snapshot.snapshot_id
    identity = {
        key: value for key, value in relabelled_payload.items() if key != "evidence_id"
    }
    relabelled_payload["evidence_id"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                identity,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    relabelled = EvidenceItem.model_validate_json(
        json.dumps(relabelled_payload, ensure_ascii=False)
    )

    with pytest.raises(ValidationError, match="registered snapshot section"):
        EvidenceGraph(
            snapshot=snapshot,
            evidence_items=(relabelled,),
            claims=(_claim(relabelled, EvidenceStance.SUPPORT),),
        )
    with pytest.raises(ValidationError, match="duplicate"):
        EvidenceGraph(
            snapshot=snapshot,
            evidence_items=(evidence, evidence),
            claims=(_claim(evidence, EvidenceStance.SUPPORT),),
        )


def test_evidence_cannot_be_created_from_detached_section_and_snapshot_id() -> None:
    snapshot = _snapshot()
    detached = _section(quality_flags=(ResearchQualityFlag.STALE,))

    with pytest.raises(TypeError):
        EvidenceItem.create(  # type: ignore[call-arg]
            snapshot_id=snapshot.snapshot_id,
            section=detached,
            excerpt="不可重标的证据",
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
        snapshot=snapshot,
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
    snapshot = _snapshot()
    evidence = _evidence(snapshot=snapshot)
    claim = _claim(evidence, EvidenceStance.SUPPORT)
    graph = EvidenceGraph(
        snapshot=snapshot,
        evidence_items=(evidence,),
        claims=(claim,),
    )

    with pytest.raises(ValidationError, match="frozen"):
        evidence.excerpt = "changed"
    with pytest.raises(ValidationError, match="frozen"):
        claim.text = "changed"
    with pytest.raises(ValidationError, match="frozen"):
        graph.snapshot = _snapshot()


def test_graph_rejects_evidence_item_and_claim_count_overflow() -> None:
    snapshot = _snapshot()
    evidence = _evidence(snapshot=snapshot)
    claim = _claim(evidence, EvidenceStance.SUPPORT)

    with pytest.raises(ValidationError, match="evidence item limit"):
        EvidenceGraph(
            snapshot=snapshot,
            evidence_items=(evidence,) * (MAX_EVIDENCE_ITEMS + 1),
            claims=(),
        )
    with pytest.raises(ValidationError, match="claim limit"):
        EvidenceGraph(
            snapshot=snapshot,
            evidence_items=(evidence,),
            claims=(claim,) * (MAX_EVIDENCE_CLAIMS + 1),
        )


def test_graph_rejects_aggregate_byte_budget_overflow() -> None:
    snapshot = _snapshot()
    evidence = _evidence(snapshot=snapshot)
    claims = tuple(
        Claim(
            text=f"{index:03d}:" + "大" * 4_000,
            evidence_ids=(evidence.evidence_id,),
            stance=EvidenceStance.SUPPORT,
        )
        for index in range(MAX_EVIDENCE_CLAIMS)
    )

    assert sum(len(claim.text.encode("utf-8")) for claim in claims) > (
        MAX_EVIDENCE_GRAPH_BYTES
    )
    with pytest.raises(ValidationError, match="byte limit"):
        EvidenceGraph(
            snapshot=snapshot,
            evidence_items=(evidence,),
            claims=claims,
        )


def test_graph_rejects_aggregate_node_budget_overflow() -> None:
    snapshot = _snapshot()
    section = snapshot.section(ResearchSectionKind.FUNDAMENTALS)
    assert section is not None
    evidence_items = tuple(
        _evidence(
            snapshot=snapshot,
            section=section,
            excerpt=f"证据 {index}",
        )
        for index in range(64)
    )
    evidence_ids = tuple(item.evidence_id for item in evidence_items)
    claims = tuple(
        Claim(
            text=f"判断 {index}",
            evidence_ids=evidence_ids,
            stance=EvidenceStance.UNCERTAIN,
        )
        for index in range(64)
    )

    assert len(evidence_items) * len(claims) > MAX_EVIDENCE_GRAPH_NODES
    with pytest.raises(ValidationError, match="node limit"):
        EvidenceGraph(
            snapshot=snapshot,
            evidence_items=evidence_items,
            claims=claims,
        )


def test_graph_rejects_raw_json_depth_before_nested_model_validation() -> None:
    snapshot = _snapshot()
    evidence = _evidence(snapshot=snapshot)
    graph = EvidenceGraph(
        snapshot=snapshot,
        evidence_items=(evidence,),
        claims=(_claim(evidence, EvidenceStance.SUPPORT),),
    )
    payload = graph.model_dump(mode="json")
    nested: dict[str, object] = {"leaf": True}
    for _ in range(MAX_EVIDENCE_GRAPH_DEPTH + 1):
        nested = {"nested": nested}
    payload["evidence_items"][0]["untrusted_extra"] = nested  # type: ignore[index]

    with pytest.raises(ValidationError, match="depth limit"):
        EvidenceGraph.model_validate_json(json.dumps(payload, ensure_ascii=False))
