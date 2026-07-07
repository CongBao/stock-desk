from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import cast, Final
import unicodedata

from pydantic import JsonValue


UNTRUSTED_DATA_LABEL: Final = "untrusted-data"
TRUSTED_CONTROL_LABEL: Final = "trusted-control"
PROMPT_DATA_POLICY_VERSION: Final = "prompt-data-v1"
MAX_UNTRUSTED_PAYLOAD_BYTES: Final = 65_536
MAX_UNTRUSTED_TOTAL_BYTES: Final = 196_608
MAX_UNTRUSTED_BLOCK_DEPTH: Final = 16
MAX_UNTRUSTED_BLOCK_NODES: Final = 4_096
MAX_UNTRUSTED_ENVELOPE_BYTES: Final = 4_096
MAX_TRUSTED_CONTROL_BYTES: Final = 32_768
MAX_TRUSTED_TOTAL_BYTES: Final = 65_536
_EVIDENCE_ID_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_HASH_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_SYMBOL_PATTERN: Final = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_ENVELOPE_KEYS: Final = frozenset(
    {
        "block_type",
        "trust_label",
        "policy_version",
        "origin",
        "source_identity",
        "evidence_ids",
        "payload_hash",
        "block_hash",
        "payload",
    }
)
_WORKFLOW_CONTEXT_KEYS: Final = frozenset(
    {
        "trust_label",
        "block_type",
        "role",
        "snapshot_id",
        "symbol",
        "allowed_evidence_ids",
    }
)
_QUALITY_FLAGS_KEYS: Final = frozenset({"trust_label", "block_type", "sections"})
_ROLES: Final = frozenset(
    {"technical", "fundamental_news", "bull", "bear", "risk_decision"}
)
_SECTION_ORDER: Final = ("market", "fundamentals", "announcements", "news")
_SECTION_KINDS: Final = frozenset(_SECTION_ORDER)
_QUALITY_FLAGS: Final = frozenset(
    {
        "partial",
        "stale",
        "expired",
        "degraded_source",
        "unverified",
        "conflicting",
    }
)
_SNAPSHOT_PAYLOAD_KEYS: Final = frozenset(
    {"data_kind", "section_kind", "section_id", "content", "provenance"}
)
_PROVENANCE_KEYS: Final = frozenset(
    {
        "canonical_source",
        "source_record",
        "source_url",
        "published_at",
        "data_cutoff",
        "fetched_at",
        "dataset_version",
        "quality_flags",
        "route",
    }
)
_ROUTE_KEYS: Final = frozenset(
    {
        "selected_source",
        "attempted_sources",
        "failure_reasons",
        "primary_failure_reason",
        "degraded_from",
    }
)
_EVIDENCE_PAYLOAD_KEYS: Final = frozenset(
    {
        "data_kind",
        "evidence_id",
        "section_kind",
        "excerpt",
        *_PROVENANCE_KEYS,
    }
)
_ROLE_OUTPUT_PAYLOAD_KEYS: Final = frozenset(
    {"data_kind", "role", "snapshot_id", "summary", "claims", "proposal"}
)
_CLAIM_KEYS: Final = frozenset({"text", "evidence_ids", "stance"})
_STANCES: Final = frozenset({"support", "oppose", "uncertain"})


class ContentPolicyError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("prompt data violates the content policy")


def make_untrusted_data_block(
    *,
    origin: str,
    evidence_ids: tuple[str, ...],
    payload: dict[str, JsonValue],
    source_identity: str | None = None,
) -> dict[str, JsonValue]:
    try:
        _validate_identifier(origin, maximum=512)
        _validate_evidence_ids(evidence_ids)
        identity = evidence_ids[0] if source_identity is None else source_identity
        _validate_identifier(identity, maximum=512)
        if type(payload) is not dict or not payload:
            raise ValueError
        sanitized_payload = cast(dict[str, JsonValue], _sanitize_json_payload(payload))
        payload_json = _canonical_json(sanitized_payload)
        if len(payload_json) > MAX_UNTRUSTED_PAYLOAD_BYTES:
            raise ValueError
        block: dict[str, JsonValue] = {
            "block_type": "data_block",
            "trust_label": UNTRUSTED_DATA_LABEL,
            "policy_version": PROMPT_DATA_POLICY_VERSION,
            "origin": origin,
            "source_identity": identity,
            "evidence_ids": list(evidence_ids),
            "payload_hash": _sha256(payload_json),
            "payload": sanitized_payload,
        }
        block["block_hash"] = _block_hash(block)
        _validate_untrusted_block(block)
        _validate_envelope_overhead(block, len(payload_json))
        return cast(dict[str, JsonValue], json.loads(_canonical_json(block)))
    except (TypeError, ValueError, RecursionError):
        raise ContentPolicyError() from None


def make_workflow_context_block(
    *,
    role: str,
    snapshot_id: str,
    symbol: str,
    allowed_evidence_ids: tuple[str, ...],
) -> dict[str, JsonValue]:
    block: dict[str, JsonValue] = {
        "trust_label": TRUSTED_CONTROL_LABEL,
        "block_type": "workflow_context",
        "role": role,
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "allowed_evidence_ids": list(allowed_evidence_ids),
    }
    _validate_trusted_control_block(block)
    return cast(dict[str, JsonValue], json.loads(_canonical_json(block)))


def make_quality_flags_block(
    sections: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    block: dict[str, JsonValue] = {
        "trust_label": TRUSTED_CONTROL_LABEL,
        "block_type": "quality_flags",
        "sections": cast(JsonValue, sections),
    }
    _validate_trusted_control_block(block)
    return cast(dict[str, JsonValue], json.loads(_canonical_json(block)))


def validate_prompt_blocks(
    blocks: tuple[dict[str, JsonValue], ...],
    *,
    expected_role: str,
    expected_snapshot_id: str,
    expected_symbol: str,
    expected_evidence_ids: tuple[str, ...],
) -> tuple[dict[str, JsonValue], ...]:
    try:
        untrusted_total = 0
        trusted_total = 0
        encoded_blocks: list[bytes] = []
        for block in blocks:
            if type(block) is not dict:
                raise ValueError
            label = block.get("trust_label")
            if label == UNTRUSTED_DATA_LABEL:
                payload_json = _validate_untrusted_block(block)
                untrusted_total += len(payload_json)
            elif label == TRUSTED_CONTROL_LABEL:
                _validate_trusted_control_block(block)
                trusted_total += len(_canonical_json(block))
            else:
                raise ValueError
            encoded_blocks.append(_canonical_json(block))
        if untrusted_total > MAX_UNTRUSTED_TOTAL_BYTES:
            raise ValueError
        if trusted_total > MAX_TRUSTED_TOTAL_BYTES:
            raise ValueError
        _validate_block_collection(
            blocks,
            expected_role=expected_role,
            expected_snapshot_id=expected_snapshot_id,
            expected_symbol=expected_symbol,
            expected_evidence_ids=expected_evidence_ids,
        )
        return tuple(
            cast(dict[str, JsonValue], json.loads(encoded))
            for encoded in encoded_blocks
        )
    except (TypeError, ValueError, RecursionError):
        raise ContentPolicyError() from None


def validate_untrusted_data_block(
    block: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    try:
        _validate_untrusted_block(block)
        return cast(dict[str, JsonValue], json.loads(_canonical_json(block)))
    except (TypeError, ValueError, RecursionError):
        raise ContentPolicyError() from None


def _validate_untrusted_block(block: dict[str, JsonValue]) -> bytes:
    if frozenset(block) != _ENVELOPE_KEYS:
        raise ValueError
    if block.get("block_type") != "data_block":
        raise ValueError
    if block.get("policy_version") != PROMPT_DATA_POLICY_VERSION:
        raise ValueError
    origin = block.get("origin")
    source_identity = block.get("source_identity")
    evidence_ids = block.get("evidence_ids")
    payload_hash = block.get("payload_hash")
    block_hash = block.get("block_hash")
    payload = block.get("payload")
    if (
        type(origin) is not str
        or type(source_identity) is not str
        or type(evidence_ids) is not list
        or type(payload_hash) is not str
        or type(block_hash) is not str
        or type(payload) is not dict
        or not payload
    ):
        raise ValueError
    _validate_identifier(origin, maximum=512)
    _validate_identifier(source_identity, maximum=512)
    _validate_evidence_ids(tuple(cast(list[str], evidence_ids)))
    _validate_json_payload(payload)
    payload_json = _canonical_json(payload)
    if len(payload_json) > MAX_UNTRUSTED_PAYLOAD_BYTES:
        raise ValueError
    if _HASH_PATTERN.fullmatch(payload_hash) is None or payload_hash != _sha256(
        payload_json
    ):
        raise ValueError
    if _HASH_PATTERN.fullmatch(block_hash) is None or block_hash != _block_hash(block):
        raise ValueError
    _validate_data_payload(payload)
    _validate_envelope_overhead(block, len(payload_json))
    return payload_json


def _block_hash(block: dict[str, JsonValue]) -> str:
    return _sha256(
        _canonical_json(
            {
                "policy_version": block.get("policy_version"),
                "origin": block.get("origin"),
                "source_identity": block.get("source_identity"),
                "evidence_ids": block.get("evidence_ids"),
                "payload": block.get("payload"),
            }
        )
    )


def _validate_data_payload(payload: dict[str, JsonValue]) -> None:
    data_kind = payload.get("data_kind")
    if data_kind == "snapshot_section":
        if frozenset(payload) != _SNAPSHOT_PAYLOAD_KEYS:
            raise ValueError
        section_kind = payload.get("section_kind")
        section_id = payload.get("section_id")
        content = payload.get("content")
        provenance = payload.get("provenance")
        if (
            type(section_kind) is not str
            or section_kind not in _SECTION_KINDS
            or type(section_id) is not str
            or _EVIDENCE_ID_PATTERN.fullmatch(section_id) is None
            or type(content) is not dict
            or type(provenance) is not dict
        ):
            raise ValueError
        _validate_provenance(provenance)
    elif data_kind == "evidence_reference":
        if frozenset(payload) != _EVIDENCE_PAYLOAD_KEYS:
            raise ValueError
        evidence_id = payload.get("evidence_id")
        section_kind = payload.get("section_kind")
        excerpt = payload.get("excerpt")
        if (
            type(evidence_id) is not str
            or _EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is None
            or type(section_kind) is not str
            or section_kind not in _SECTION_KINDS
            or type(excerpt) is not str
            or not excerpt
        ):
            raise ValueError
        _validate_provenance({key: payload[key] for key in _PROVENANCE_KEYS})
    elif data_kind == "role_output":
        _validate_role_output_payload(payload)
    else:
        raise ValueError


def _validate_provenance(provenance: dict[str, JsonValue]) -> None:
    if frozenset(provenance) != _PROVENANCE_KEYS:
        raise ValueError
    canonical_source = provenance.get("canonical_source")
    source_record = provenance.get("source_record")
    source_url = provenance.get("source_url")
    published_at = provenance.get("published_at")
    data_cutoff = provenance.get("data_cutoff")
    fetched_at = provenance.get("fetched_at")
    dataset_version = provenance.get("dataset_version")
    quality_flags = provenance.get("quality_flags")
    route = provenance.get("route")
    if (
        type(canonical_source) is not str
        or not canonical_source
        or len(canonical_source) > 64
        or type(source_record) is not str
        or not source_record
        or len(source_record) > 1_024
        or (source_url is not None and type(source_url) is not str)
        or (type(source_url) is str and (not source_url or len(source_url) > 2_048))
        or (published_at is not None and type(published_at) is not str)
        or type(data_cutoff) is not str
        or not data_cutoff
        or type(fetched_at) is not str
        or not fetched_at
        or type(dataset_version) is not str
        or not dataset_version
        or len(dataset_version) > 256
        or type(quality_flags) is not list
    ):
        raise ValueError
    _validate_quality_flag_values(quality_flags)
    _validate_route(route)


def _validate_quality_flag_values(flags: list[JsonValue]) -> None:
    if (
        any(type(flag) is not str or flag not in _QUALITY_FLAGS for flag in flags)
        or len(flags) != len(frozenset(cast(list[str], flags)))
        or flags != sorted(cast(list[str], flags))
    ):
        raise ValueError


def _validate_route(route: JsonValue | None) -> None:
    if route is None:
        return
    if type(route) is not dict or frozenset(route) != _ROUTE_KEYS:
        raise ValueError
    selected_source = route.get("selected_source")
    attempted_sources = route.get("attempted_sources")
    failure_reasons = route.get("failure_reasons")
    primary_failure_reason = route.get("primary_failure_reason")
    degraded_from = route.get("degraded_from")
    if (
        type(selected_source) is not str
        or not selected_source
        or type(attempted_sources) is not list
        or any(type(item) is not str or not item for item in attempted_sources)
        or type(failure_reasons) is not list
        or any(type(item) is not str or not item for item in failure_reasons)
        or (
            primary_failure_reason is not None
            and type(primary_failure_reason) is not str
        )
        or (degraded_from is not None and type(degraded_from) is not str)
    ):
        raise ValueError


def _validate_role_output_payload(payload: dict[str, JsonValue]) -> None:
    if frozenset(payload) != _ROLE_OUTPUT_PAYLOAD_KEYS:
        raise ValueError
    role = payload.get("role")
    snapshot_id = payload.get("snapshot_id")
    summary = payload.get("summary")
    claims = payload.get("claims")
    proposal = payload.get("proposal")
    if (
        type(role) is not str
        or role not in _ROLES
        or type(snapshot_id) is not str
        or _EVIDENCE_ID_PATTERN.fullmatch(snapshot_id) is None
        or type(summary) is not str
        or not summary
        or type(claims) is not list
        or not claims
    ):
        raise ValueError
    for claim in claims:
        if type(claim) is not dict or frozenset(claim) != _CLAIM_KEYS:
            raise ValueError
        text = claim.get("text")
        evidence_ids = claim.get("evidence_ids")
        stance = claim.get("stance")
        if (
            type(text) is not str
            or not text
            or type(evidence_ids) is not list
            or type(stance) is not str
            or stance not in _STANCES
        ):
            raise ValueError
        _validate_evidence_ids(tuple(cast(list[str], evidence_ids)))
    if proposal is not None:
        raise ValueError


def _validate_block_collection(
    blocks: tuple[dict[str, JsonValue], ...],
    *,
    expected_role: str,
    expected_snapshot_id: str,
    expected_symbol: str,
    expected_evidence_ids: tuple[str, ...],
) -> None:
    if not blocks:
        raise ValueError
    contexts = [
        (index, block)
        for index, block in enumerate(blocks)
        if block.get("trust_label") == TRUSTED_CONTROL_LABEL
        and block.get("block_type") == "workflow_context"
    ]
    if len(contexts) != 1 or contexts[0][0] != 0:
        raise ValueError
    context = contexts[0][1]
    if (
        context.get("role") != expected_role
        or context.get("snapshot_id") != expected_snapshot_id
        or context.get("symbol") != expected_symbol
        or context.get("allowed_evidence_ids") != list(expected_evidence_ids)
    ):
        raise ValueError
    role = cast(str, context["role"])
    allowed_ids = cast(list[str], context["allowed_evidence_ids"])
    untrusted = [
        block for block in blocks if block.get("trust_label") == UNTRUSTED_DATA_LABEL
    ]
    if not untrusted:
        raise ValueError
    identities = [cast(str, block["source_identity"]) for block in untrusted]
    if len(identities) != len(frozenset(identities)):
        raise ValueError
    evidence_union = frozenset(
        evidence_id
        for block in untrusted
        for evidence_id in cast(list[str], block["evidence_ids"])
    )
    if frozenset(allowed_ids) != evidence_union:
        raise ValueError
    for block in untrusted:
        _validate_block_relationship(block)

    quality = [
        (index, block)
        for index, block in enumerate(blocks)
        if block.get("trust_label") == TRUSTED_CONTROL_LABEL
        and block.get("block_type") == "quality_flags"
    ]
    if role in {"technical", "fundamental_news"}:
        _validate_analyst_collection(role, blocks, untrusted, quality)
    else:
        _validate_dependency_collection(
            role,
            cast(str, context["snapshot_id"]),
            blocks,
            untrusted,
            quality,
            allowed_ids,
        )


def _validate_block_relationship(block: dict[str, JsonValue]) -> None:
    origin = cast(str, block["origin"])
    source_identity = cast(str, block["source_identity"])
    evidence_ids = cast(list[str], block["evidence_ids"])
    payload = cast(dict[str, JsonValue], block["payload"])
    data_kind = payload["data_kind"]
    if data_kind == "snapshot_section":
        section_id = cast(str, payload["section_id"])
        if origin != f"snapshot_section:{section_id}" or source_identity != section_id:
            raise ValueError
    elif data_kind == "evidence_reference":
        evidence_id = cast(str, payload["evidence_id"])
        if (
            origin != f"evidence:{evidence_id}"
            or source_identity != evidence_id
            or evidence_ids != [evidence_id]
        ):
            raise ValueError
    elif data_kind == "role_output":
        role = cast(str, payload["role"])
        snapshot_id = cast(str, payload["snapshot_id"])
        claims = cast(list[dict[str, JsonValue]], payload["claims"])
        closure: list[str] = []
        known: set[str] = set()
        for claim in claims:
            for evidence_id in cast(list[str], claim["evidence_ids"]):
                if evidence_id not in known:
                    known.add(evidence_id)
                    closure.append(evidence_id)
        if (
            origin != f"role_output:{role}"
            or source_identity != f"{snapshot_id}:{role}"
            or evidence_ids != closure
        ):
            raise ValueError
    else:
        raise ValueError


def _validate_analyst_collection(
    role: str,
    blocks: tuple[dict[str, JsonValue], ...],
    untrusted: list[dict[str, JsonValue]],
    quality: list[tuple[int, dict[str, JsonValue]]],
) -> None:
    if quality or len(blocks) != 1 + len(untrusted):
        raise ValueError
    if any(
        cast(dict[str, JsonValue], block["payload"])["data_kind"] != "snapshot_section"
        for block in untrusted
    ):
        raise ValueError
    allowed = (
        {"market"} if role == "technical" else {"fundamentals", "announcements", "news"}
    )
    section_kinds = [
        cast(str, cast(dict[str, JsonValue], block["payload"])["section_kind"])
        for block in untrusted
    ]
    if (
        any(kind not in allowed for kind in section_kinds)
        or len(section_kinds) != len(frozenset(section_kinds))
        or section_kinds
        != sorted(section_kinds, key=lambda kind: _SECTION_ORDER.index(kind))
    ):
        raise ValueError


def _validate_dependency_collection(
    role: str,
    snapshot_id: str,
    blocks: tuple[dict[str, JsonValue], ...],
    untrusted: list[dict[str, JsonValue]],
    quality: list[tuple[int, dict[str, JsonValue]]],
    allowed_ids: list[str],
) -> None:
    expected_roles = (
        ["technical", "fundamental_news"]
        if role in {"bull", "bear"}
        else ["bull", "bear"]
    )
    if len(untrusted) < len(expected_roles):
        raise ValueError
    role_blocks = untrusted[: len(expected_roles)]
    evidence_blocks = untrusted[len(expected_roles) :]
    dependency_roles = [
        cast(str, cast(dict[str, JsonValue], block["payload"]).get("role"))
        for block in role_blocks
        if cast(dict[str, JsonValue], block["payload"]).get("data_kind")
        == "role_output"
    ]
    if dependency_roles != expected_roles:
        raise ValueError
    if any(
        cast(dict[str, JsonValue], block["payload"])["snapshot_id"] != snapshot_id
        for block in role_blocks
    ):
        raise ValueError
    if any(
        cast(dict[str, JsonValue], block["payload"])["data_kind"]
        != "evidence_reference"
        for block in evidence_blocks
    ):
        raise ValueError
    reference_ids = [
        cast(str, cast(dict[str, JsonValue], block["payload"])["evidence_id"])
        for block in evidence_blocks
    ]
    if reference_ids != allowed_ids:
        raise ValueError
    evidence_kinds = {
        cast(str, cast(dict[str, JsonValue], block["payload"])["evidence_id"]): cast(
            str, cast(dict[str, JsonValue], block["payload"])["section_kind"]
        )
        for block in evidence_blocks
    }
    role_kind_allowlist = {
        "technical": {"market"},
        "fundamental_news": {"fundamentals", "announcements", "news"},
    }
    for block in role_blocks:
        payload = cast(dict[str, JsonValue], block["payload"])
        dependency_role = cast(str, payload["role"])
        allowed_kinds = role_kind_allowlist.get(dependency_role)
        if allowed_kinds is not None and any(
            evidence_kinds[evidence_id] not in allowed_kinds
            for evidence_id in cast(list[str], block["evidence_ids"])
        ):
            raise ValueError
    if role == "risk_decision":
        if len(quality) != 1 or quality[0][0] != len(blocks) - 1:
            raise ValueError
        if len(blocks) != 2 + len(untrusted):
            raise ValueError
    elif quality or len(blocks) != 1 + len(untrusted):
        raise ValueError


def _validate_envelope_overhead(
    block: dict[str, JsonValue], payload_bytes: int
) -> None:
    overhead = len(_canonical_json(block)) - payload_bytes
    if overhead < 0 or overhead > MAX_UNTRUSTED_ENVELOPE_BYTES:
        raise ValueError


def _validate_trusted_control_block(block: dict[str, JsonValue]) -> None:
    block_type = block.get("block_type")
    if block.get("trust_label") != TRUSTED_CONTROL_LABEL:
        raise ValueError
    if block_type == "workflow_context":
        if frozenset(block) != _WORKFLOW_CONTEXT_KEYS:
            raise ValueError
        role = block.get("role")
        snapshot_id = block.get("snapshot_id")
        symbol = block.get("symbol")
        evidence_ids = block.get("allowed_evidence_ids")
        if (
            type(role) is not str
            or role not in _ROLES
            or type(snapshot_id) is not str
            or _EVIDENCE_ID_PATTERN.fullmatch(snapshot_id) is None
            or type(symbol) is not str
            or _SYMBOL_PATTERN.fullmatch(symbol) is None
            or type(evidence_ids) is not list
        ):
            raise ValueError
        _validate_identifier(symbol, maximum=32)
        _validate_evidence_ids(tuple(cast(list[str], evidence_ids)))
    elif block_type == "quality_flags":
        if frozenset(block) != _QUALITY_FLAGS_KEYS:
            raise ValueError
        sections = block.get("sections")
        if type(sections) is not list or len(sections) > len(_SECTION_KINDS):
            raise ValueError
        seen: set[str] = set()
        section_positions: list[int] = []
        for section in sections:
            if type(section) is not dict or frozenset(section) != {
                "section_kind",
                "flags",
            }:
                raise ValueError
            kind = section.get("section_kind")
            flags = section.get("flags")
            if (
                type(kind) is not str
                or kind not in _SECTION_KINDS
                or kind in seen
                or type(flags) is not list
                or any(
                    type(flag) is not str or flag not in _QUALITY_FLAGS
                    for flag in flags
                )
                or len(flags) != len(frozenset(cast(list[str], flags)))
                or flags != sorted(cast(list[str], flags))
            ):
                raise ValueError
            seen.add(kind)
            section_positions.append(_SECTION_ORDER.index(kind))
        if section_positions != sorted(section_positions):
            raise ValueError
    else:
        raise ValueError
    _validate_json_payload(block)
    if len(_canonical_json(block)) > MAX_TRUSTED_CONTROL_BYTES:
        raise ValueError


def _validate_identifier(value: str, *, maximum: int) -> None:
    if (
        not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _has_forbidden_character(value)
    ):
        raise ValueError


def _validate_evidence_ids(value: tuple[str, ...]) -> None:
    if (
        not value
        or len(value) > 64
        or len(value) != len(frozenset(value))
        or any(
            type(item) is not str or _EVIDENCE_ID_PATTERN.fullmatch(item) is None
            for item in value
        )
    ):
        raise ValueError


def _validate_json_payload(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        if depth > MAX_UNTRUSTED_BLOCK_DEPTH:
            raise ValueError
        nodes += 1
        if nodes > MAX_UNTRUSTED_BLOCK_NODES:
            raise ValueError
        if isinstance(current, Mapping):
            if any(type(key) is not str for key in current):
                raise ValueError
            for key, child in current.items():
                _validate_key(key)
                stack.append((child, depth + 1))
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            stack.extend((child, depth + 1) for child in current)
        elif type(current) is str:
            if _has_forbidden_character(current):
                raise ValueError
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError
        elif current is not None and type(current) not in {int, bool}:
            raise ValueError


def _sanitize_json_payload(value: object) -> JsonValue:
    nodes = [0]

    def sanitize(current: object, depth: int) -> JsonValue:
        if depth > MAX_UNTRUSTED_BLOCK_DEPTH:
            raise ValueError
        nodes[0] += 1
        if nodes[0] > MAX_UNTRUSTED_BLOCK_NODES:
            raise ValueError
        if isinstance(current, Mapping):
            result: dict[str, JsonValue] = {}
            for key, child in current.items():
                if type(key) is not str:
                    raise ValueError
                _validate_key(key)
                result[key] = sanitize(child, depth + 1)
            return result
        if isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            return [sanitize(child, depth + 1) for child in current]
        if type(current) is str:
            normalized = current.replace("\r\n", "\n").replace("\r", "\n")
            cleaned = "".join(
                character
                for character in normalized
                if not _has_forbidden_character(character)
            )
            if current and not cleaned.strip():
                raise ValueError
            return cleaned
        if type(current) is float:
            if not math.isfinite(current):
                raise ValueError
            return current
        if current is None or type(current) in {int, bool}:
            return cast(JsonValue, current)
        raise ValueError

    return sanitize(value, 1)


def _validate_key(value: str) -> None:
    if _has_forbidden_character(value) or any(
        character in {"\n", "\t", "\r"} for character in value
    ):
        raise ValueError


def _has_forbidden_character(value: str) -> bool:
    return any(
        (ord(character) < 32 and character not in {"\n", "\t"})
        or 127 <= ord(character) <= 159
        or ord(character) in {0x2028, 0x2029}
        or unicodedata.category(character) == "Cf"
        for character in value
    )


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
