from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from stock_desk.analysis.content_policy import (
    make_quality_flags_block,
    make_untrusted_data_block,
    make_workflow_context_block,
    validate_prompt_blocks,
)
from stock_desk.analysis.evidence import EvidenceGraph, EvidenceItem
from stock_desk.analysis.providers.base import ModelRequest
from stock_desk.analysis.roles import (
    ROLE_SECTION_KINDS,
    RoleName,
    RoleOutput,
    load_role_prompt,
    role_output_schema,
)
from stock_desk.analysis.snapshot import ResearchSection, ResearchSnapshot


@dataclass(frozen=True, slots=True)
class BuiltRoleRequest:
    request: ModelRequest
    template_version: str
    template_hash: str
    request_hash: str


class PromptBuildError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("model prompt input is invalid")


def build_role_request(
    *,
    role: RoleName,
    snapshot: ResearchSnapshot,
    evidence: tuple[EvidenceItem, ...],
    dependencies: tuple[RoleOutput, ...],
    temperature: float = 0.1,
    timeout_seconds: float = 90.0,
    max_output_tokens: int = 4_096,
) -> BuiltRoleRequest:
    _validate_role_inputs(role, snapshot, evidence, dependencies)
    prompt = load_role_prompt(role)
    blocks: list[dict[str, JsonValue]] = [
        make_workflow_context_block(
            role=role.value,
            snapshot_id=snapshot.snapshot_id,
            symbol=snapshot.symbol,
            allowed_evidence_ids=tuple(item.evidence_id for item in evidence),
        )
    ]
    if role in ROLE_SECTION_KINDS:
        allowed_kinds = ROLE_SECTION_KINDS[role]
        blocks.extend(
            _snapshot_section_block(section, evidence)
            for section in snapshot.sections
            if section.kind in allowed_kinds
        )
    else:
        blocks.extend(_role_output_block(dependency) for dependency in dependencies)
        blocks.extend(_evidence_reference_block(item) for item in evidence)
        if role is RoleName.RISK_DECISION:
            blocks.append(
                make_quality_flags_block(
                    [
                        {
                            "section_kind": section.kind.value,
                            "flags": [flag.value for flag in section.quality_flags],
                        }
                        for section in snapshot.sections
                    ]
                )
            )
    canonical_blocks = validate_prompt_blocks(
        tuple(blocks),
        expected_role=role.value,
        expected_snapshot_id=snapshot.snapshot_id,
        expected_symbol=snapshot.symbol,
        expected_evidence_ids=tuple(item.evidence_id for item in evidence),
    )
    request = ModelRequest(
        system=prompt.content,
        data_blocks=canonical_blocks,
        output_schema=role_output_schema(),
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )
    canonical_request = ModelRequest.model_validate_json(request.model_dump_json())
    return BuiltRoleRequest(
        request=canonical_request,
        template_version=prompt.version,
        template_hash=prompt.content_hash,
        request_hash=canonical_request.stable_hash(),
    )


def _validate_role_inputs(
    role: RoleName,
    snapshot: ResearchSnapshot,
    evidence: tuple[EvidenceItem, ...],
    dependencies: tuple[RoleOutput, ...],
) -> None:
    try:
        EvidenceGraph(snapshot=snapshot, evidence_items=evidence, claims=())
        for dependency in dependencies:
            RoleOutput.model_validate_json(dependency.model_dump_json())
        evidence_ids = tuple(item.evidence_id for item in evidence)
        if not evidence_ids or len(evidence_ids) != len(frozenset(evidence_ids)):
            raise ValueError
        if role in ROLE_SECTION_KINDS:
            if dependencies or any(
                item.section_kind not in ROLE_SECTION_KINDS[role] for item in evidence
            ):
                raise ValueError
            return
        expected_roles = {
            RoleName.BULL: (RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS),
            RoleName.BEAR: (RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS),
            RoleName.RISK_DECISION: (RoleName.BULL, RoleName.BEAR),
        }[role]
        if tuple(item.role for item in dependencies) != expected_roles:
            raise ValueError
        if any(item.snapshot_id != snapshot.snapshot_id for item in dependencies):
            raise ValueError
        evidence_by_id = {item.evidence_id: item for item in evidence}
        dependency_evidence = frozenset(
            evidence_id
            for dependency in dependencies
            for evidence_id in dependency.evidence_ids
        )
        if frozenset(evidence_ids) != dependency_evidence:
            raise ValueError
        if role in {RoleName.BULL, RoleName.BEAR}:
            for dependency in dependencies:
                allowed_kinds = ROLE_SECTION_KINDS[dependency.role]
                if any(
                    evidence_by_id[evidence_id].section_kind not in allowed_kinds
                    for evidence_id in dependency.evidence_ids
                ):
                    raise ValueError
    except (KeyError, TypeError, ValueError):
        raise PromptBuildError() from None


def _snapshot_section_block(
    section: ResearchSection,
    evidence: tuple[EvidenceItem, ...],
) -> dict[str, JsonValue]:
    evidence_ids = tuple(
        item.evidence_id for item in evidence if item.section_kind is section.kind
    )
    return make_untrusted_data_block(
        origin=f"snapshot_section:{section.section_id}",
        source_identity=section.section_id,
        evidence_ids=evidence_ids,
        payload={
            "data_kind": "snapshot_section",
            "section_kind": section.kind.value,
            "section_id": section.section_id,
            "content": section.content,
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
                "route": (
                    section.route.model_dump(mode="json")
                    if section.route is not None
                    else None
                ),
            },
        },
    )


def _role_output_block(dependency: RoleOutput) -> dict[str, JsonValue]:
    return make_untrusted_data_block(
        origin=f"role_output:{dependency.role.value}",
        source_identity=f"{dependency.snapshot_id}:{dependency.role.value}",
        evidence_ids=dependency.evidence_ids,
        payload={
            "data_kind": "role_output",
            **dependency.model_dump(mode="json"),
        },
    )


def _evidence_reference_block(item: EvidenceItem) -> dict[str, JsonValue]:
    return make_untrusted_data_block(
        origin=f"evidence:{item.evidence_id}",
        source_identity=item.evidence_id,
        evidence_ids=(item.evidence_id,),
        payload={
            "data_kind": "evidence_reference",
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
            "dataset_version": item.dataset_version,
            "quality_flags": [flag.value for flag in item.quality_flags],
            "route": item.route.model_dump(mode="json")
            if item.route is not None
            else None,
        },
    )
