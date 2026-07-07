from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import cast, Final, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    ValidationInfo,
)

from stock_desk.analysis.snapshot import (
    ResearchQualityFlag,
    ResearchRouteMetadata,
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
    Sha256Digest,
)
from stock_desk.market.types import UtcDatetime


_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_EVIDENCE_ITEMS: Final = 256
MAX_EVIDENCE_CLAIMS: Final = 256
MAX_EVIDENCE_GRAPH_BYTES: Final = 1_048_576
MAX_EVIDENCE_GRAPH_DEPTH: Final = 8
MAX_EVIDENCE_GRAPH_NODES: Final = 4_000


class _FrozenEvidenceModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class EvidenceStance(StrEnum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    UNCERTAIN = "uncertain"


class EvidenceItem(_FrozenEvidenceModel):
    evidence_id: Sha256Digest
    snapshot_id: Sha256Digest
    section_id: Sha256Digest
    section_kind: ResearchSectionKind
    canonical_source: str = Field(min_length=1, max_length=64)
    source_record: str = Field(min_length=1, max_length=1_024)
    source_url: str | None = Field(
        default=None,
        max_length=2_048,
        description="Display-only provenance URL; never fetched by this domain model.",
    )
    published_at: UtcDatetime | None = None
    data_cutoff: UtcDatetime
    fetched_at: UtcDatetime
    dataset_version: str = Field(min_length=1, max_length=256)
    excerpt: str = Field(min_length=1, max_length=4_096)
    quality_flags: tuple[ResearchQualityFlag, ...] = ()
    route: ResearchRouteMetadata | None = None

    @field_validator("canonical_source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if _SOURCE_PATTERN.fullmatch(value) is None:
            raise ValueError("canonical source is invalid")
        return value

    @field_validator("source_record")
    @classmethod
    def validate_source_record(cls, value: str) -> str:
        return _validate_safe_text(value, label="source record", maximum=1_024)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            len(value) > 2_048
            or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("source URL is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("source URL is invalid") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
            or port == 0
        ):
            raise ValueError("source URL is unsafe")
        return value

    @field_validator("dataset_version")
    @classmethod
    def validate_dataset_version(cls, value: str) -> str:
        return _validate_safe_text(value, label="dataset version", maximum=256)

    @field_validator("quality_flags", mode="before")
    @classmethod
    def canonicalize_quality_flags(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        if info.mode == "json" and type(value) is list:
            value = tuple(cast(list[object], value))
        if type(value) is not tuple:
            raise ValueError("quality flags must be a tuple")
        flags = cast(tuple[object, ...], value)
        if len(flags) != len(frozenset(flags)):
            raise ValueError("quality flags cannot contain duplicate values")
        return tuple(sorted(flags, key=lambda item: str(item)))

    @classmethod
    def create(
        cls,
        *,
        snapshot: ResearchSnapshot,
        section_kind: ResearchSectionKind,
        excerpt: str,
    ) -> EvidenceItem:
        canonical_snapshot = ResearchSnapshot.model_validate_json(
            snapshot.model_dump_json(by_alias=True)
        )
        section = canonical_snapshot.section(section_kind)
        if section is None:
            raise ValueError("evidence requires a registered snapshot section")
        fields: dict[str, object] = {
            "snapshot_id": canonical_snapshot.snapshot_id,
            "section_id": section.section_id,
            "section_kind": section.kind,
            "canonical_source": section.canonical_source,
            "source_record": section.source_record,
            "source_url": section.source_url,
            "published_at": section.published_at,
            "data_cutoff": section.data_cutoff,
            "fetched_at": section.fetched_at,
            "dataset_version": section.dataset_version,
            "excerpt": excerpt,
            "quality_flags": section.quality_flags,
        }
        if section.route is not None:
            fields["route"] = section.route.canonical_payload()
        return cls(
            evidence_id=_evidence_id(fields),
            snapshot_id=canonical_snapshot.snapshot_id,
            section_id=section.section_id,
            section_kind=section.kind,
            canonical_source=section.canonical_source,
            source_record=section.source_record,
            source_url=section.source_url,
            published_at=section.published_at,
            data_cutoff=section.data_cutoff,
            fetched_at=section.fetched_at,
            dataset_version=section.dataset_version,
            excerpt=excerpt,
            quality_flags=section.quality_flags,
            route=section.route,
        )

    @field_validator("excerpt")
    @classmethod
    def validate_excerpt(cls, value: str) -> str:
        if (
            value != value.strip()
            or any(
                ord(character) < 32 and character not in {"\n", "\t"}
                for character in value
            )
            or any(ord(character) == 127 for character in value)
        ):
            raise ValueError("evidence excerpt is invalid")
        return value

    @model_validator(mode="after")
    def validate_content_address(self) -> Self:
        if self.data_cutoff > self.fetched_at:
            raise ValueError("data cutoff cannot be later than fetch time")
        if self.published_at is not None and self.published_at > self.fetched_at:
            raise ValueError("publication time cannot be later than fetch time")
        if (
            self.section_kind
            in {
                ResearchSectionKind.ANNOUNCEMENTS,
                ResearchSectionKind.NEWS,
            }
            and self.published_at is None
        ):
            raise ValueError("published evidence requires publication time")
        if (
            self.route is not None
            and self.route.selected_source != self.canonical_source
        ):
            raise ValueError("evidence route must match canonical source")
        if self.evidence_id != _evidence_id(_evidence_fields(self)):
            raise ValueError("evidence_id does not match canonical evidence content")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {"evidence_id": self.evidence_id, **_evidence_fields(self)}
        )


class Claim(_FrozenEvidenceModel):
    text: str = Field(min_length=1, max_length=4_096)
    evidence_ids: tuple[Sha256Digest, ...] = Field(min_length=1, max_length=64)
    stance: EvidenceStance

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) == 0 or ord(character) == 127 for character in value
        ):
            raise ValueError("claim text is invalid")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(frozenset(value)):
            raise ValueError("claim cannot contain duplicate evidence references")
        return value


class EvidenceGraph(_FrozenEvidenceModel):
    snapshot: ResearchSnapshot
    evidence_items: tuple[EvidenceItem, ...] = Field(max_length=MAX_EVIDENCE_ITEMS)
    claims: tuple[Claim, ...] = Field(max_length=MAX_EVIDENCE_CLAIMS)

    @field_validator("evidence_items", mode="before")
    @classmethod
    def validate_evidence_item_count(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        if isinstance(value, Sequence) and len(value) > MAX_EVIDENCE_ITEMS:
            raise ValueError("evidence graph exceeds the evidence item limit")
        if info.mode == "json" and type(value) is list:
            _validate_raw_graph_field("evidence_items", value)
            return tuple(
                EvidenceItem.model_validate_json(json.dumps(item, ensure_ascii=False))
                for item in cast(list[object], value)
            )
        return value

    @field_validator("claims", mode="before")
    @classmethod
    def validate_claim_count(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        if isinstance(value, Sequence) and len(value) > MAX_EVIDENCE_CLAIMS:
            raise ValueError("evidence graph exceeds the claim limit")
        if info.mode == "json" and type(value) is list:
            _validate_raw_graph_field("claims", value)
            return tuple(
                Claim.model_validate_json(json.dumps(item, ensure_ascii=False))
                for item in cast(list[object], value)
            )
        return value

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        _validate_graph_budget(self.evidence_items, self.claims)
        canonical_snapshot = ResearchSnapshot.model_validate_json(
            self.snapshot.model_dump_json(by_alias=True)
        )
        evidence_ids = tuple(item.evidence_id for item in self.evidence_items)
        if len(evidence_ids) != len(frozenset(evidence_ids)):
            raise ValueError("evidence graph cannot contain duplicate evidence")
        for item in self.evidence_items:
            section = canonical_snapshot.section(item.section_kind)
            if section is None or not _matches_snapshot_section(
                item,
                snapshot=canonical_snapshot,
                section=section,
            ):
                raise ValueError("evidence must match its registered snapshot section")
            EvidenceItem.model_validate_json(item.model_dump_json(by_alias=True))
        known = frozenset(evidence_ids)
        if any(
            evidence_id not in known
            for claim in self.claims
            for evidence_id in claim.evidence_ids
        ):
            raise ValueError("claim must reference existing evidence")
        return self

    def evidence_for(self, claim: Claim) -> tuple[EvidenceItem, ...]:
        by_id = {item.evidence_id: item for item in self.evidence_items}
        try:
            return tuple(by_id[evidence_id] for evidence_id in claim.evidence_ids)
        except KeyError:
            raise ValueError("claim must reference existing evidence") from None


_CRITICAL_SECTION_KINDS = frozenset(
    {ResearchSectionKind.MARKET, ResearchSectionKind.FUNDAMENTALS}
)
_INELIGIBLE_EVIDENCE_FLAGS = frozenset(
    {
        ResearchQualityFlag.STALE,
        ResearchQualityFlag.EXPIRED,
        ResearchQualityFlag.UNVERIFIED,
    }
)


def critical_evidence_eligible(
    snapshot: ResearchSnapshot,
    evidence_graph: EvidenceGraph,
) -> bool:
    """Single critical-evidence gate shared by preflight and execution."""
    return not (
        bool(
            _CRITICAL_SECTION_KINDS.intersection(
                item.kind for item in snapshot.missing_sections
            )
        )
        or any(
            section.kind in _CRITICAL_SECTION_KINDS
            and bool(_INELIGIBLE_EVIDENCE_FLAGS.intersection(section.quality_flags))
            for section in snapshot.sections
        )
        or any(
            not any(
                item.section_kind is kind
                and not _INELIGIBLE_EVIDENCE_FLAGS.intersection(item.quality_flags)
                for item in evidence_graph.evidence_items
            )
            for kind in _CRITICAL_SECTION_KINDS
        )
    )


def _evidence_fields(item: EvidenceItem) -> dict[str, object]:
    fields: dict[str, object] = {
        "snapshot_id": item.snapshot_id,
        "section_id": item.section_id,
        "section_kind": item.section_kind.value,
        "canonical_source": item.canonical_source,
        "source_record": item.source_record,
        "source_url": item.source_url,
        "published_at": _canonical_datetime(item.published_at),
        "data_cutoff": _canonical_datetime(item.data_cutoff),
        "fetched_at": _canonical_datetime(item.fetched_at),
        "dataset_version": item.dataset_version,
        "excerpt": item.excerpt,
        "quality_flags": tuple(flag.value for flag in item.quality_flags),
    }
    if item.route is not None:
        fields["route"] = item.route.canonical_payload()
    return fields


def _matches_snapshot_section(
    item: EvidenceItem,
    *,
    snapshot: ResearchSnapshot,
    section: ResearchSection,
) -> bool:
    return (
        item.snapshot_id == snapshot.snapshot_id
        and item.section_id == section.section_id
        and item.canonical_source == section.canonical_source
        and item.source_record == section.source_record
        and item.source_url == section.source_url
        and item.published_at == section.published_at
        and item.data_cutoff == section.data_cutoff
        and item.fetched_at == section.fetched_at
        and item.dataset_version == section.dataset_version
        and item.quality_flags == section.quality_flags
        and item.route == section.route
    )


def _evidence_id(fields: Mapping[str, object]) -> str:
    normalized = {
        key: (
            value.value
            if isinstance(value, StrEnum)
            else _canonical_datetime(value)
            if hasattr(value, "tzinfo")
            else tuple(
                item.value if isinstance(item, StrEnum) else item for item in value
            )
            if key == "quality_flags" and isinstance(value, tuple)
            else value
        )
        for key, value in fields.items()
    }
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(normalized)).hexdigest()}"


def _canonical_datetime(value: object) -> str | None:
    if value is None:
        return None
    if not hasattr(value, "astimezone"):
        raise TypeError("evidence time is invalid")
    assert isinstance(value, datetime)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_graph_budget(
    evidence_items: tuple[EvidenceItem, ...],
    claims: tuple[Claim, ...],
) -> None:
    payload = {
        "evidence_items": tuple(
            item.model_dump(mode="json") for item in evidence_items
        ),
        "claims": tuple(claim.model_dump(mode="json") for claim in claims),
    }
    _validate_graph_shape(payload)
    if len(_canonical_json_bytes(payload)) > MAX_EVIDENCE_GRAPH_BYTES:
        raise ValueError("evidence graph exceeds the aggregate byte limit")


def _validate_raw_graph_field(field_name: str, value: object) -> None:
    payload = {field_name: value}
    _validate_graph_shape(payload)
    if len(_canonical_json_bytes(payload)) > MAX_EVIDENCE_GRAPH_BYTES:
        raise ValueError("evidence graph exceeds the aggregate byte limit")


def _validate_graph_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    node_count = 0
    while stack:
        current, depth = stack.pop()
        if depth > MAX_EVIDENCE_GRAPH_DEPTH:
            raise ValueError("evidence graph exceeds the aggregate depth limit")
        node_count += 1
        if node_count > MAX_EVIDENCE_GRAPH_NODES:
            raise ValueError("evidence graph exceeds the aggregate node limit")
        if isinstance(current, Mapping):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            stack.extend((child, depth + 1) for child in current)


def _validate_safe_text(
    value: str,
    *,
    label: str,
    maximum: int,
) -> str:
    if (
        not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return value
