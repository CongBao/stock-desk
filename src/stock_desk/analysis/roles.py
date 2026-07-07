from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from typing import cast, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from stock_desk.analysis.evidence import Claim, EvidenceItem
from stock_desk.analysis.snapshot import ResearchSectionKind, Sha256Digest


MAX_ROLE_OUTPUT_BYTES: Final = 65_536
MAX_ROLE_OUTPUT_DEPTH: Final = 8
MAX_ROLE_OUTPUT_NODES: Final = 512
MAX_ROLE_CLAIMS: Final = 16
MAX_ROLE_EVIDENCE_REFERENCES: Final = 64
MAX_PROMPT_BYTES: Final = 32_768
_PROMPT_VERSION_PATTERN = re.compile(r"^[a-z_]+-v[1-9][0-9]*$")


class RoleName(StrEnum):
    TECHNICAL = "technical"
    FUNDAMENTAL_NEWS = "fundamental_news"
    BULL = "bull"
    BEAR = "bear"
    RISK_DECISION = "risk_decision"


ROLE_ORDER: Final = (
    RoleName.TECHNICAL,
    RoleName.FUNDAMENTAL_NEWS,
    RoleName.BULL,
    RoleName.BEAR,
    RoleName.RISK_DECISION,
)
ANALYST_ROLES: Final = (RoleName.TECHNICAL, RoleName.FUNDAMENTAL_NEWS)
REVIEW_ROLES: Final = (RoleName.BULL, RoleName.BEAR)
ROLE_SECTION_KINDS: Final = {
    RoleName.TECHNICAL: frozenset({ResearchSectionKind.MARKET}),
    RoleName.FUNDAMENTAL_NEWS: frozenset(
        {
            ResearchSectionKind.FUNDAMENTALS,
            ResearchSectionKind.ANNOUNCEMENTS,
            ResearchSectionKind.NEWS,
        }
    ),
}


class _FrozenRoleModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class RoleOutput(_FrozenRoleModel):
    role: RoleName
    snapshot_id: Sha256Digest
    summary: str = Field(min_length=1, max_length=8_192)
    claims: tuple[Claim, ...] = Field(min_length=1, max_length=MAX_ROLE_CLAIMS)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) == 0 or ord(character) == 127 for character in value
        ):
            raise ValueError("role summary is invalid")
        return value

    @field_validator("claims", mode="before")
    @classmethod
    def decode_claims(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "json" and type(value) is list:
            return tuple(
                Claim.model_validate_json(json.dumps(item, ensure_ascii=False))
                for item in cast(list[object], value)
            )
        return value

    @model_validator(mode="after")
    def validate_reference_count(self) -> Self:
        reference_count = sum(len(claim.evidence_ids) for claim in self.claims)
        if reference_count > MAX_ROLE_EVIDENCE_REFERENCES:
            raise ValueError("role output exceeds the evidence reference limit")
        return self

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        known: set[str] = set()
        ordered: list[str] = []
        for claim in self.claims:
            for evidence_id in claim.evidence_ids:
                if evidence_id not in known:
                    known.add(evidence_id)
                    ordered.append(evidence_id)
        return tuple(ordered)


class RoleOutputValidationError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("model role output is invalid")


@dataclass(frozen=True, slots=True)
class RolePrompt:
    role: RoleName
    version: str
    content: str
    content_hash: str


def load_role_prompt(role: RoleName) -> RolePrompt:
    path = Path(__file__).with_name("prompts") / f"{role.value}.md"
    try:
        raw = path.read_bytes()
    except OSError:
        raise RuntimeError("role prompt is unavailable") from None
    if not raw or len(raw) > MAX_PROMPT_BYTES or b"\r" in raw or b"\x00" in raw:
        raise RuntimeError("role prompt is invalid")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("role prompt is invalid") from None
    first_line, separator, content = decoded.partition("\n\n")
    if not separator or not first_line.startswith("version: "):
        raise RuntimeError("role prompt is invalid")
    version = first_line.removeprefix("version: ")
    if (
        _PROMPT_VERSION_PATTERN.fullmatch(version) is None
        or version != f"{role.value}-v1"
        or not content
        or content != content.strip() + "\n"
    ):
        raise RuntimeError("role prompt is invalid")
    return RolePrompt(
        role=role,
        version=version,
        content=content.strip(),
        content_hash=(
            f"sha256:{hashlib.sha256(content.strip().encode('utf-8')).hexdigest()}"
        ),
    )


def role_output_schema() -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], RoleOutput.model_json_schema(mode="validation"))


def validate_role_output(
    content: object,
    *,
    expected_role: RoleName,
    snapshot_id: str,
    allowed_evidence: tuple[EvidenceItem, ...],
) -> RoleOutput:
    try:
        encoded = _canonical_role_output(content)
        output = RoleOutput.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise RoleOutputValidationError() from None
    if output.role is not expected_role or output.snapshot_id != snapshot_id:
        raise RoleOutputValidationError()
    allowed_ids = frozenset(item.evidence_id for item in allowed_evidence)
    if any(
        evidence_id not in allowed_ids
        for claim in output.claims
        for evidence_id in claim.evidence_ids
    ):
        raise RoleOutputValidationError()
    return output


def _canonical_role_output(content: object) -> bytes:
    _validate_role_output_shape(content)
    encoded = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_ROLE_OUTPUT_BYTES:
        raise ValueError("role output exceeds the byte limit")
    return encoded


def _validate_role_output_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        if depth > MAX_ROLE_OUTPUT_DEPTH:
            raise ValueError("role output exceeds the depth limit")
        nodes += 1
        if nodes > MAX_ROLE_OUTPUT_NODES:
            raise ValueError("role output exceeds the node limit")
        if isinstance(current, dict):
            if any(type(key) is not str for key in current):
                raise ValueError("role output keys must be strings")
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
        elif current is not None and type(current) not in {str, int, float, bool}:
            raise ValueError("role output contains a non-JSON value")
