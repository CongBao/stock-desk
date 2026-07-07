from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
import json
import re
from typing import cast, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    ValidationError,
    field_validator,
)


MAX_RATING_PROPOSAL_BYTES: Final = 16_384
MAX_RATING_PROPOSAL_DEPTH: Final = 8
MAX_RATING_PROPOSAL_NODES: Final = 128
_FORBIDDEN_FINANCIAL_ACTION_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btarget\s+price\b",
        r"\bposition\s+(?:size|sizing)\b",
        r"\bpersonalized\s+investment\s+advice\b",
        r"\bplace\s+(?:an?\s+)?orders?\b",
        r"\b(?:buy|sell)\s+(?:\d+(?:\.\d+)?\s+shares?|the\s+(?:entire|whole)\s+position|all\s+(?:shares?|holdings?))(?:\s+now)?\b",
        r"\b(?:recommend|suggest)(?:ed|s|ing)?\s+(?:to\s+)?(?:buy|sell)\b",
        r"\b(?:buy|sell)\b",
        r"目标价",
        r"仓位",
        r"个性化投资建议",
        r"下单",
        r"自动交易",
        r"(?:立即|马上|现在|建议)\s*(?:买入|卖出)(?:\d+(?:\.\d+)?(?:股|手|%)?|全部持仓|全部|所有持仓)?",
        r"买入|卖出",
    )
)


class Rating(StrEnum):
    STRONG_BULLISH = "strong_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    STRONG_BEARISH = "strong_bearish"

    @property
    def label_zh(self) -> str:
        return {
            Rating.STRONG_BULLISH: "强烈看多",
            Rating.BULLISH: "看多",
            Rating.NEUTRAL: "中性",
            Rating.BEARISH: "看空",
            Rating.STRONG_BEARISH: "强烈看空",
        }[self]


class RatingProposal(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    rating: Rating
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    confidence_explanation: str = Field(min_length=1, max_length=4_096)

    @field_validator("confidence_explanation")
    @classmethod
    def validate_confidence_explanation(cls, value: str) -> str:
        if (
            value != value.strip()
            or any(ord(character) == 0 or ord(character) == 127 for character in value)
            or contains_forbidden_financial_action(value)
        ):
            raise ValueError("confidence explanation is invalid")
        return value


class RatingProposalValidationError(ValueError):
    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__("rating proposal is invalid")


def rating_proposal_schema() -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        RatingProposal.model_json_schema(mode="validation"),
    )


def parse_rating_proposal(value: object) -> RatingProposal:
    try:
        encoded = _canonical_proposal_json(value)
        return RatingProposal.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise RatingProposalValidationError() from None


def contains_forbidden_financial_action(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in _FORBIDDEN_FINANCIAL_ACTION_PATTERNS
    )


def _canonical_proposal_json(value: object) -> bytes:
    _validate_json_shape(value)
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_RATING_PROPOSAL_BYTES:
        raise ValueError("rating proposal exceeds the byte limit")
    return encoded


def _validate_json_shape(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        if depth > MAX_RATING_PROPOSAL_DEPTH:
            raise ValueError("rating proposal exceeds the depth limit")
        nodes += 1
        if nodes > MAX_RATING_PROPOSAL_NODES:
            raise ValueError("rating proposal exceeds the node limit")
        if isinstance(current, Mapping):
            if any(type(key) is not str for key in current):
                raise ValueError("rating proposal keys must be strings")
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            stack.extend((child, depth + 1) for child in current)
        elif current is not None and type(current) not in {str, int, float, bool}:
            raise ValueError("rating proposal contains a non-JSON value")
