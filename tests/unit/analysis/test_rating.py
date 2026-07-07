from __future__ import annotations

import json
import math

from pydantic import ValidationError
import pytest

from stock_desk.analysis.rating import (
    MAX_RATING_PROPOSAL_BYTES,
    Rating,
    RatingProposal,
    RatingProposalValidationError,
    parse_rating_proposal,
    rating_proposal_schema,
)


VALID_PROPOSAL = {
    "rating": "bullish",
    "confidence": 0.82,
    "confidence_explanation": "The registered evidence is current and sufficiently diverse.",
}


def test_rating_has_exactly_five_stable_values_and_chinese_labels() -> None:
    assert tuple(Rating) == (
        Rating.STRONG_BULLISH,
        Rating.BULLISH,
        Rating.NEUTRAL,
        Rating.BEARISH,
        Rating.STRONG_BEARISH,
    )
    assert tuple(item.value for item in Rating) == (
        "strong_bullish",
        "bullish",
        "neutral",
        "bearish",
        "strong_bearish",
    )
    assert tuple(item.label_zh for item in Rating) == (
        "强烈看多",
        "看多",
        "中性",
        "看空",
        "强烈看空",
    )


def test_rating_proposal_schema_exposes_only_stable_bounded_fields() -> None:
    schema = rating_proposal_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "rating",
        "confidence",
        "confidence_explanation",
    }
    rating_ref = schema["properties"]["rating"]["$ref"]
    definition = schema["$defs"][rating_ref.rsplit("/", 1)[-1]]
    assert definition["enum"] == [item.value for item in Rating]


def test_rating_proposal_is_strict_frozen_and_json_round_trips() -> None:
    proposal = parse_rating_proposal(VALID_PROPOSAL)
    restored = RatingProposal.model_validate_json(proposal.model_dump_json())

    assert proposal == restored
    assert proposal.rating is Rating.BULLISH
    assert proposal.confidence == 0.82
    with pytest.raises(ValidationError, match="frozen"):
        proposal.confidence = 0.1


@pytest.mark.parametrize(
    "field,value",
    [
        ("rating", "very_bullish"),
        ("confidence", -0.01),
        ("confidence", 1.01),
        ("confidence", "0.82"),
        ("confidence_explanation", ""),
        ("confidence_explanation", " padded "),
        ("confidence_explanation", "Buy 100 shares now."),
        ("confidence_explanation", "Sell the entire position now."),
        ("confidence_explanation", "立即买入100股"),
        ("confidence_explanation", "立即卖出全部持仓"),
        ("confidence_explanation", "We recommend buy now."),
        ("confidence_explanation", "We recommend sell now."),
        ("confidence_explanation", "建议买入"),
        ("confidence_explanation", "建议卖出"),
        ("confidence_explanation", "Buy this stock."),
        ("confidence_explanation", "Sell."),
        ("confidence_explanation", "维持买入评级"),
    ],
)
def test_rating_proposal_rejects_malformed_values(field: str, value: object) -> None:
    payload = {**VALID_PROPOSAL, field: value}

    with pytest.raises(RatingProposalValidationError):
        parse_rating_proposal(payload)


@pytest.mark.parametrize(
    "forbidden_field",
    ["target_price", "position_size", "personalized_advice", "order"],
)
def test_rating_proposal_rejects_financial_action_and_extra_fields(
    forbidden_field: str,
) -> None:
    payload = {**VALID_PROPOSAL, forbidden_field: "unsafe"}

    with pytest.raises(RatingProposalValidationError):
        parse_rating_proposal(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_rating_proposal_rejects_non_finite_confidence(value: float) -> None:
    assert not math.isfinite(value)

    with pytest.raises(ValidationError):
        RatingProposal(
            rating=Rating.NEUTRAL,
            confidence=value,
            confidence_explanation="Registered evidence was evaluated.",
        )


def test_rating_proposal_parser_enforces_byte_depth_and_node_budgets() -> None:
    oversized = {
        **VALID_PROPOSAL,
        "confidence_explanation": "x" * MAX_RATING_PROPOSAL_BYTES,
    }
    nested: object = "leaf"
    for _ in range(16):
        nested = {"nested": nested}
    too_deep = {**VALID_PROPOSAL, "unexpected": nested}
    too_many_nodes = {**VALID_PROPOSAL, "unexpected": list(range(256))}

    for payload in (oversized, too_deep, too_many_nodes):
        with pytest.raises(RatingProposalValidationError):
            parse_rating_proposal(payload)

    encoded = json.dumps(VALID_PROPOSAL, separators=(",", ":"), sort_keys=True)
    assert parse_rating_proposal(json.loads(encoded)).rating is Rating.BULLISH
