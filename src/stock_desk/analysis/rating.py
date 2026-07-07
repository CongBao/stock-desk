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
_STRONG_SENTENCE_BOUNDARY: Final = re.compile(r"[.!?:。！？：；;\r\n]+")
_CLAUSE_BOUNDARY: Final = re.compile(
    r"[.!?:。！？：；;,，\r\n]+|\b(?:so|therefore|hence|thus|then|and|but)\b|"
    r"因此|所以|请|然后|并|但",
    re.IGNORECASE,
)
_QUANTITY_PATTERN: Final = (
    r"(?:\d+(?:\.\d+)?|\b(?:half|quarter|all|one[- ]third|two[- ]thirds?|"
    r"one[- ]quarter|three[- ]quarters?)\b|\b(?:zero|one|two|three|four|five|"
    r"six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|"
    r"seventy|eighty|ninety|hundred)(?:[- ](?:one|two|three|four|five|six|"
    r"seven|eight|nine))?\s+percent\b|一半|半数|全部|"
    r"[零一二三四五六七八九十百]+成|"
    r"百分之(?:\d+|[零一二三四五六七八九十百]+)|"
    r"[一二三四五六七八九十百]+分之(?:\d+|[一二三四五六七八九十百]+))"
)
_QUANTITY: Final = re.compile(_QUANTITY_PATTERN, re.IGNORECASE)
_TARGET_PRICE: Final = re.compile(
    r"\b(?:target[\s,，\-–—]+(?:(?:stock|share)[\s,，\-–—]+)?price|"
    r"(?:(?:stock|share)[\s,，\-–—]+)?price[\s,，\-–—]+target)\b|"
    r"目标[\s,，\-–—]*(?:价(?:格)?|股价)|"
    r"(?:股价|价格)[\s,，\-–—]*目标|估值目标",
    re.IGNORECASE,
)
_POSITION_ASSIGNMENT: Final = re.compile(
    rf"\b(?:position|holdings?|portfolio[\s,，\-–—]+exposure)\b"
    rf"(?:[\s,，\-–—]+(?:size|sizing|ratio))?\s*"
    rf"(?:at|to|is|should\s+be|[:=])?\s*{_QUANTITY_PATTERN}\s*(?:%|％)?|"
    rf"(?:仓位|持仓)(?:比例|占比)?(?:应)?(?:为|是|[:：])?\s*(?:约|大约)?\s*"
    rf"{_QUANTITY_PATTERN}\s*(?:%|％)?",
    re.IGNORECASE,
)
_ALLOCATION_ASSIGNMENT: Final = re.compile(
    rf"\ballocation\b\s*[:=]\s*{_QUANTITY_PATTERN}\s*(?:%|％)?|"
    rf"(?:资金)?(?:配置|分配)比例\s*[:：=]\s*{_QUANTITY_PATTERN}\s*(?:%|％)?",
    re.IGNORECASE,
)
_CONFIGURATION_ACTION: Final = re.compile(
    r"\b(?:use|using|invest|invests|invested|investing|allocate|allocates|"
    r"allocated|allocating|allocation|set|keep|commit|committed|deploy|deployed)"
    r"\b|\b(?:limit|cap|maintain|reduce|weight(?:ed|ing|s)?|hold(?:s|ing)?|"
    r"held|make|makes|made)\b|使用|投入|配置|分配|设定|控制|降低|降至|保持|"
    r"维持|减少|配比|权重|占组合",
    re.IGNORECASE,
)
_STRONG_INVESTMENT_OBJECT: Final = re.compile(
    r"\b(?:stocks?|shares?|portfolio|position|holdings?|exposure)\b|"
    r"股票|个股|标的|组合|仓位|持仓",
    re.IGNORECASE,
)
_GENERIC_INVESTMENT_RESOURCE: Final = re.compile(
    r"\b(?:funds?|capital|allocation)\b|资金|资本",
    re.IGNORECASE,
)
_IMPERATIVE_OR_FIRST_PERSON: Final = re.compile(
    r"^\s*(?:(?:only|now|please)\s+)*(?:(?:we|i)\s+)?(?:use|invest|allocate|set|keep|commit|"
    r"deploy|limit|cap|maintain|reduce|weight|hold|make|recommend|suggest|"
    r"advise|consider)\b|"
    r"^\s*(?:仅|现在|请)*(?:使用|投入|配置|分配|设定|建议|推荐|考虑)",
    re.IGNORECASE,
)
_EXPLICIT_ADVICE: Final = re.compile(
    r"\b(?:recommend(?:ed|s|ing)?|suggest(?:ed|s|ing)?|advis(?:e|ed|es|ing)|"
    r"consider(?:ed|s|ing)?|should|could|can|would|appropriate|advisable|"
    r"prefer(?:red|s|ring)?|preferable|prudent|wise|ideal|suitable|ought|need|"
    r"(?:may|might)\s+want|(?:you|it|we|i)\s+(?:may|might))\b|"
    r"建议|推荐|应该|应当|应|可以|可|不妨|为宜|宜|考虑|最好|适合",
    re.IGNORECASE,
)
_CORPORATE_ACTOR: Final = re.compile(
    r"\b(?:board|management|company|corporation|fund|ETF|asset\s+manager|"
    r"institution)\b|董事会|管理层|公司公告|公司|基金|资管机构|资产管理人",
    re.IGNORECASE,
)
_BOARD_OR_MANAGEMENT: Final = re.compile(
    r"\b(?:board|management)\b|董事会|管理层",
    re.IGNORECASE,
)
_HISTORICAL_ALLOCATION: Final = re.compile(
    r"\b(?:allocated|invested|deployed|committed|used|maintained|kept|reduced|"
    r"capped|held|weighted)\b|"
    r"已[^。；;\r\n]{0,40}(?:配置|投入|分配)|"
    r"(?:配置|投入|分配)[^。；;\r\n]{0,40}(?:到|于|至)",
    re.IGNORECASE,
)
_CORPORATE_DESTINATION: Final = re.compile(
    r"\b(?:to|in|into|at|for|toward|across)\s+"
    r"(?:(?:an?|the)\s+)?[A-Za-z][\w-]*|"
    r"(?:用于|配置到|配置于|配置至|投入到|投入于|投入至|分配到|分配于|"
    r"分配至)[^。；;\r\n]{1,32}",
    re.IGNORECASE,
)
_SECOND_PERSON: Final = re.compile(r"\b(?:you|your)\b|你|您的?|用户", re.IGNORECASE)
_HISTORICAL_POSITION_FACT: Final = re.compile(
    rf"\b(?:fund|ETF|company|corporation|asset\s+manager|institution)'?s?\b"
    rf"(?:[^.!?。！？；;\r\n]{{0,32}}\b(?:equity\s+)?position\b\s*"
    rf"(?:is|was|stood\s+at)\s*{_QUANTITY_PATTERN}\s*(?:%|％)?|"
    rf"[^.!?。！？；;\r\n]{{0,24}}\b(?:maintained|kept|reduced|capped)\b"
    rf"[^.!?。！？；;\r\n]{{0,48}}\b(?:portfolio|position|exposure)\b"
    rf"[^.!?。！？；;\r\n]{{0,24}}{_QUANTITY_PATTERN}\s*(?:%|％)?)",
    re.IGNORECASE,
)
_FORBIDDEN_FINANCIAL_ACTION_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bposition[\s,，\-–—]+(?:size|sizing)\b",
        r"\bpersonalized\s+investment\s+advice\b",
        r"\bplace\s+(?:an?\s+)?orders?\b",
        r"\b(?:buy|sell)\s+(?:\d+(?:\.\d+)?\s+shares?|the\s+(?:entire|whole)\s+position|all\s+(?:shares?|holdings?))(?:\s+now)?\b",
        r"\b(?:recommend|suggest)(?:ed|s|ing)?\s+(?:to\s+)?(?:buy|sell)\b",
        r"\b(?:buy|sell)\b",
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
    if any(
        pattern.search(value) is not None
        for pattern in _FORBIDDEN_FINANCIAL_ACTION_PATTERNS
    ):
        return True
    if _TARGET_PRICE.search(value) is not None:
        return True
    if (
        _POSITION_ASSIGNMENT.search(value) is not None
        and _HISTORICAL_POSITION_FACT.search(value) is None
    ):
        return True
    if _ALLOCATION_ASSIGNMENT.search(value) is not None:
        return True
    sentences = _segments(value, _STRONG_SENTENCE_BOUNDARY)
    if any(_contains_target_or_position_advice(sentence) for sentence in sentences):
        return True
    return any(
        _contains_target_or_position_advice(clause)
        for sentence in sentences
        for clause in _segments(sentence, _CLAUSE_BOUNDARY)
    )


def _segments(value: str, boundary: re.Pattern[str]) -> tuple[str, ...]:
    return tuple(
        sentence.strip() for sentence in boundary.split(value) if sentence.strip()
    )


def _contains_target_or_position_advice(sentence: str) -> bool:
    if _TARGET_PRICE.search(sentence) is not None:
        return True
    if (
        _POSITION_ASSIGNMENT.search(sentence) is not None
        and _HISTORICAL_POSITION_FACT.search(sentence) is None
    ):
        return True
    if _ALLOCATION_ASSIGNMENT.search(sentence) is not None:
        return True
    has_soft_advice = _EXPLICIT_ADVICE.search(sentence) is not None
    configuration_actions = tuple(_CONFIGURATION_ACTION.finditer(sentence))
    has_configuration_action = bool(configuration_actions) or has_soft_advice
    if not has_configuration_action or _QUANTITY.search(sentence) is None:
        return False
    has_strong_object = _STRONG_INVESTMENT_OBJECT.search(sentence) is not None
    has_generic_resource = _GENERIC_INVESTMENT_RESOURCE.search(sentence) is not None
    if not has_strong_object and not has_generic_resource:
        return False
    corporate_actors = tuple(_CORPORATE_ACTOR.finditer(sentence))
    has_corporate_actor = bool(corporate_actors)
    has_corporate_destination = _CORPORATE_DESTINATION.search(sentence) is not None
    if has_corporate_actor and _SECOND_PERSON.search(sentence) is None:
        if (
            _BOARD_OR_MANAGEMENT.search(sentence) is not None
            and has_corporate_destination
            and not has_strong_object
        ):
            return False
        if (
            _HISTORICAL_ALLOCATION.search(sentence) is not None
            and (
                len(configuration_actions) == 1
                or len(corporate_actors) >= len(configuration_actions)
            )
            and not has_soft_advice
        ):
            return False
    if has_strong_object:
        return True
    return _IMPERATIVE_OR_FIRST_PERSON.search(sentence) is not None or has_soft_advice


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
