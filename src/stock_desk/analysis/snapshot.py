from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Annotated, Any, cast, Final, Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    AliasChoices,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    JsonValue,
    model_validator,
    StringConstraints,
    TypeAdapter,
    ValidationInfo,
)

from stock_desk.market.types import CanonicalSymbol, UtcDatetime


MAX_SECTION_CONTENT_BYTES: Final = 262_144
MAX_SECTION_CONTENT_DEPTH: Final = 32
MAX_SECTION_CONTENT_NODES: Final = 20_000
SNAPSHOT_SCHEMA_VERSION: Final = "analysis-snapshot-v1"

Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]
_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_RECOVERY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SYMBOL_ADAPTER = TypeAdapter(CanonicalSymbol)


class ResearchSectionKind(StrEnum):
    MARKET = "market"
    FUNDAMENTALS = "fundamentals"
    ANNOUNCEMENTS = "announcements"
    NEWS = "news"


RESEARCH_SECTION_ORDER: Final = (
    ResearchSectionKind.MARKET,
    ResearchSectionKind.FUNDAMENTALS,
    ResearchSectionKind.ANNOUNCEMENTS,
    ResearchSectionKind.NEWS,
)
_SECTION_ORDINAL = {
    kind: ordinal for ordinal, kind in enumerate(RESEARCH_SECTION_ORDER)
}


class ResearchQualityFlag(StrEnum):
    PARTIAL = "partial"
    STALE = "stale"
    EXPIRED = "expired"
    DEGRADED_SOURCE = "degraded_source"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting"


class ResearchMissingReason(StrEnum):
    NO_PROVIDER = "no_provider"
    MISSING = "missing"
    NO_DATA = "no_data"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED = "unsupported"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"


class _FrozenAnalysisModel(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        serialize_by_alias=True,
        strict=True,
    )


class ResearchRouteMetadata(_FrozenAnalysisModel):
    """Deterministic, public-safe routing identity for one successful section."""

    selected_source: str = Field(min_length=1, max_length=64)
    attempted_sources: tuple[str, ...] = Field(default=(), max_length=16)
    failure_reasons: tuple[ResearchMissingReason, ...] = Field(
        default=(), max_length=16
    )
    primary_failure_reason: ResearchMissingReason | None = None
    degraded_from: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("attempted_sources", "failure_reasons", mode="before")
    @classmethod
    def decode_json_tuples(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        if info.mode == "json" and type(value) is list:
            return tuple(cast(list[object], value))
        return value

    @field_validator("selected_source")
    @classmethod
    def validate_selected_source(cls, value: str) -> str:
        if _SOURCE_PATTERN.fullmatch(value) is None:
            raise ValueError("selected route source is invalid")
        return value

    @field_validator("attempted_sources")
    @classmethod
    def validate_attempted_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(frozenset(value)):
            raise ValueError("route attempts cannot contain duplicate sources")
        if any(_SOURCE_PATTERN.fullmatch(source) is None for source in value):
            raise ValueError("route attempt source is invalid")
        return value

    @field_validator("degraded_from")
    @classmethod
    def validate_degraded_from(cls, value: str | None) -> str | None:
        if value is not None and _SOURCE_PATTERN.fullmatch(value) is None:
            raise ValueError("degraded route source is invalid")
        return value

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if len(self.attempted_sources) != len(self.failure_reasons):
            raise ValueError("route sources and failure reasons must align")
        if self.selected_source in self.attempted_sources:
            raise ValueError("selected route source cannot be a failed attempt")
        if self.attempted_sources:
            if self.primary_failure_reason is not self.failure_reasons[0]:
                raise ValueError("primary route failure must be the first failure")
            if self.degraded_from != self.attempted_sources[0]:
                raise ValueError("degraded route boundary must start at primary source")
        elif self.primary_failure_reason is not None or self.degraded_from is not None:
            raise ValueError("direct route cannot contain a degraded boundary")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "selected_source": self.selected_source,
            "attempted_sources": self.attempted_sources,
            "failure_reasons": tuple(reason.value for reason in self.failure_reasons),
            "primary_failure_reason": (
                self.primary_failure_reason.value
                if self.primary_failure_reason is not None
                else None
            ),
            "degraded_from": self.degraded_from,
        }


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


def _validate_source_url(value: str | None) -> str | None:
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


def _validate_json_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    node_count = 0
    while stack:
        current, depth = stack.pop()
        if depth > MAX_SECTION_CONTENT_DEPTH:
            raise ValueError("section content exceeds the depth limit")
        node_count += 1
        if node_count > MAX_SECTION_CONTENT_NODES:
            raise ValueError("section content exceeds the node limit")
        if isinstance(current, Mapping):
            for key, child in current.items():
                if type(key) is not str:
                    raise ValueError("section content keys must be strings")
                stack.append((child, depth + 1))
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            stack.extend((child, depth + 1) for child in current)
        elif type(current) is float:
            if not math.isfinite(current):
                raise ValueError("section content must contain only finite numbers")
        elif current is not None and type(current) not in {str, int, bool}:
            raise ValueError("section content contains a non-JSON value")


def _canonical_content_json(value: object) -> bytes:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("section content must be a nonempty JSON object")
    _validate_json_shape(value)
    encoded: bytes | None = None
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        pass
    if encoded is None:
        raise ValueError("section content is invalid") from None
    if len(encoded) > MAX_SECTION_CONTENT_BYTES:
        raise ValueError("section content exceeds the byte limit")
    return encoded


def _decoded_json_object(value: bytes) -> dict[str, JsonValue]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("section content is invalid")
    return cast(dict[str, JsonValue], decoded)


def _content_object_schema(schema: dict[str, Any]) -> None:
    schema.pop("format", None)
    schema["type"] = "object"
    schema["additionalProperties"] = True


class ResearchSection(_FrozenAnalysisModel):
    kind: ResearchSectionKind
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
    quality_flags: tuple[ResearchQualityFlag, ...] = ()
    route: ResearchRouteMetadata | None = None
    content_json: bytes = Field(
        validation_alias=AliasChoices("content", "content_json"),
        serialization_alias="content",
        repr=False,
        json_schema_extra=_content_object_schema,
    )

    @field_validator("content_json", mode="before")
    @classmethod
    def freeze_content(cls, value: object) -> bytes:
        if isinstance(value, (bytes, bytearray)):
            if len(value) > MAX_SECTION_CONTENT_BYTES:
                raise ValueError("section content exceeds the byte limit")
            try:
                value = json.loads(bytes(value))
            except (UnicodeDecodeError, ValueError, TypeError):
                raise ValueError("section content is invalid") from None
        return _canonical_content_json(value)

    @field_serializer("content_json")
    def serialize_content(self, value: bytes) -> dict[str, JsonValue]:
        return _decoded_json_object(value)

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
        return _validate_source_url(value)

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

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        if self.data_cutoff > self.fetched_at:
            raise ValueError("data cutoff cannot be later than fetch time")
        if self.published_at is not None and self.published_at > self.fetched_at:
            raise ValueError("publication time cannot be later than fetch time")
        if (
            self.kind
            in {
                ResearchSectionKind.ANNOUNCEMENTS,
                ResearchSectionKind.NEWS,
            }
            and self.published_at is None
        ):
            raise ValueError("published research section requires publication time")
        if self.route is not None:
            if self.route.selected_source != self.canonical_source:
                raise ValueError("route source must match canonical source")
            is_degraded = bool(self.route.attempted_sources)
            has_degraded_flag = (
                ResearchQualityFlag.DEGRADED_SOURCE in self.quality_flags
            )
            if is_degraded != has_degraded_flag:
                raise ValueError("route boundary must match degraded quality flag")
        return self

    @property
    def content(self) -> dict[str, JsonValue]:
        return _decoded_json_object(self.content_json)

    @property
    def section_id(self) -> str:
        return _content_id(self.canonical_payload())

    def canonical_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind.value,
            "canonical_source": self.canonical_source,
            "source_record": self.source_record,
            "source_url": self.source_url,
            "published_at": _canonical_datetime(self.published_at),
            "data_cutoff": _canonical_datetime(self.data_cutoff),
            "fetched_at": _canonical_datetime(self.fetched_at),
            "dataset_version": self.dataset_version,
            "quality_flags": tuple(flag.value for flag in self.quality_flags),
            "content": self.content,
        }
        if self.route is not None:
            payload["route"] = self.route.canonical_payload()
        return payload


class MissingResearchSection(_FrozenAnalysisModel):
    kind: ResearchSectionKind
    reason: ResearchMissingReason
    checked_at: UtcDatetime
    attempted_sources: tuple[str, ...] = Field(max_length=16)
    recovery_code: str = Field(min_length=1, max_length=64)

    @field_validator("attempted_sources")
    @classmethod
    def validate_attempted_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(frozenset(value)):
            raise ValueError("attempted sources cannot contain duplicates")
        for source in value:
            if _SOURCE_PATTERN.fullmatch(source) is None:
                raise ValueError("attempted source is invalid")
        return value

    @field_validator("recovery_code")
    @classmethod
    def validate_recovery_code(cls, value: str) -> str:
        if _RECOVERY_PATTERN.fullmatch(value) is None:
            raise ValueError("recovery code is invalid")
        return value

    def canonical_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "reason": self.reason.value,
            "checked_at": _canonical_datetime(self.checked_at),
            "attempted_sources": self.attempted_sources,
            "recovery_code": self.recovery_code,
        }


class ResearchDataServiceProtocol(Protocol):
    def load_all(
        self,
        symbol: CanonicalSymbol,
    ) -> tuple[ResearchSection | MissingResearchSection, ...]: ...


class ResearchSnapshot(_FrozenAnalysisModel):
    schema_version: Literal["analysis-snapshot-v1"] = SNAPSHOT_SCHEMA_VERSION
    snapshot_id: Sha256Digest
    symbol: CanonicalSymbol
    frozen_at: UtcDatetime
    sections: tuple[ResearchSection, ...]
    missing_sections: tuple[MissingResearchSection, ...]

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        frozen_at: datetime,
        sections: tuple[ResearchSection, ...],
        missing_sections: tuple[MissingResearchSection, ...],
    ) -> ResearchSnapshot:
        ordered_sections = tuple(
            sorted(sections, key=lambda item: _SECTION_ORDINAL[item.kind])
        )
        ordered_missing = tuple(
            sorted(missing_sections, key=lambda item: _SECTION_ORDINAL[item.kind])
        )
        identity = _snapshot_identity_payload(
            symbol=symbol,
            frozen_at=frozen_at,
            sections=ordered_sections,
            missing_sections=ordered_missing,
        )
        return cls(
            snapshot_id=_content_id(identity),
            symbol=symbol,
            frozen_at=frozen_at,
            sections=ordered_sections,
            missing_sections=ordered_missing,
        )

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        kinds = tuple(section.kind for section in self.sections) + tuple(
            missing.kind for missing in self.missing_sections
        )
        if len(kinds) != len(RESEARCH_SECTION_ORDER) or set(kinds) != set(
            RESEARCH_SECTION_ORDER
        ):
            raise ValueError("snapshot requires exactly one outcome for every kind")
        if self.sections != tuple(
            sorted(self.sections, key=lambda item: _SECTION_ORDINAL[item.kind])
        ) or self.missing_sections != tuple(
            sorted(
                self.missing_sections,
                key=lambda item: _SECTION_ORDINAL[item.kind],
            )
        ):
            raise ValueError("snapshot outcomes must use canonical order")
        if any(section.fetched_at > self.frozen_at for section in self.sections):
            raise ValueError("section fetch time cannot be later than snapshot freeze")
        if any(
            missing.checked_at > self.frozen_at for missing in self.missing_sections
        ):
            raise ValueError("missing check cannot be later than snapshot freeze")
        expected = _content_id(
            _snapshot_identity_payload(
                symbol=self.symbol,
                frozen_at=self.frozen_at,
                sections=self.sections,
                missing_sections=self.missing_sections,
            )
        )
        if self.snapshot_id != expected:
            raise ValueError("snapshot_id does not match canonical snapshot content")
        return self

    def section(self, kind: ResearchSectionKind) -> ResearchSection | None:
        for section in self.sections:
            if section.kind is kind:
                return section
        return None

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "snapshot_id": self.snapshot_id,
                **_snapshot_identity_payload(
                    symbol=self.symbol,
                    frozen_at=self.frozen_at,
                    sections=self.sections,
                    missing_sections=self.missing_sections,
                ),
            }
        )


class ResearchSnapshotBuilder:
    def __init__(
        self,
        *,
        data_service: ResearchDataServiceProtocol,
        clock: Callable[[], datetime],
    ) -> None:
        self._data_service = data_service
        self._clock = clock

    def build(self, symbol: str) -> ResearchSnapshot:
        canonical_symbol = _SYMBOL_ADAPTER.validate_python(symbol, strict=True)
        outcomes = self._data_service.load_all(canonical_symbol)
        sections = tuple(
            outcome for outcome in outcomes if isinstance(outcome, ResearchSection)
        )
        missing = tuple(
            outcome
            for outcome in outcomes
            if isinstance(outcome, MissingResearchSection)
        )
        if len(sections) + len(missing) != len(outcomes):
            raise TypeError("research data service returned an invalid outcome")
        return ResearchSnapshot.create(
            symbol=canonical_symbol,
            frozen_at=self._clock(),
            sections=sections,
            missing_sections=missing,
        )


def _snapshot_identity_payload(
    *,
    symbol: str,
    frozen_at: datetime,
    sections: tuple[ResearchSection, ...],
    missing_sections: tuple[MissingResearchSection, ...],
) -> dict[str, object]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "symbol": symbol,
        "frozen_at": _canonical_datetime(frozen_at),
        "sections": tuple(section.canonical_payload() for section in sections),
        "missing_sections": tuple(
            missing.canonical_payload() for missing in missing_sections
        ),
    }


def _canonical_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("canonical datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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
