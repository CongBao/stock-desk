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

TARGET_AND_ALLOCATION_ADVICE = (
    "Price target is CNY 20.",
    "Target-price: CNY 20.",
    "Target stock price is CNY 20.",
    "Stock price target is CNY 20.",
    "价格目标为20元。",
    "Target price: ¥20.",
    "目标价格为20元。",
    "估值目标为20元。",
    "Allocate 50% of the portfolio.",
    "Allocation should be 30% of capital.",
    "Exposure should be 20%.",
    "50% portfolio allocation is recommended.",
    "Recommended position: 50%.",
    "Set the position at 50% of capital.",
    "Allocating 50% of the portfolio is recommended.",
    "We recommend allocating 50% of the portfolio.",
    "Set the portfolio allocation to 50%.",
    "Keep portfolio exposure at 20%.",
    "Use 50% of available funds.",
    "Invest 50% of capital.",
    "Set allocation at 50%.",
    "Allocate 50% of funds.",
    "Consider allocating 50% of available funds.",
    "You can use 50% of available funds.",
    "I would allocate 50% of available funds.",
    "Using 50% of available funds is appropriate.",
    "It may be appropriate to invest 50% of capital.",
    "The appropriate capital allocation is 50%.",
    "You should invest half of available funds.",
    "You may want to allocate a quarter of available capital.",
    "It would be prudent to deploy all available capital.",
    "Limit portfolio exposure to 20%.",
    "Cap portfolio exposure at 20%.",
    "Maintain portfolio exposure at 20%.",
    "Reduce portfolio exposure to 20%.",
    "You should allocate one-third of available funds.",
    "You should allocate two thirds of available funds.",
    "You should allocate fifty percent of available funds.",
    "It may be wise to allocate half of available funds.",
    "I prefer allocating half of available funds.",
    "You may allocate half of available funds.",
    "The ideal allocation is 50%.",
    "建议配置50%的资金。",
    "建议配置30%的组合。",
    "建议将50%的资金配置于该股票。",
    "推荐配置50%资金到该股票。",
    "将50%的资金配置于该股票。",
    "配置50%资金到该股票。",
    "资金配置比例可为50%。",
    "资金分配以50%为宜。",
    "可以配置一半资金到该股票。",
    "最好投入全部资金到该标的。",
    "仓位控制在20%。",
    "控制仓位在20%。",
    "将仓位降至20%。",
    "保持20%仓位。",
    "建议配置五成资金。",
    "建议配置百分之五十的资金。",
    "建议配置四分之一资金。",
    "The fund allocated 20% of its portfolio to bonds, so allocate 50% of the portfolio.",
    "基金已将20%的组合资金配置于债券，请配置50%的资金。",
    "The fund allocated 20% of its portfolio to bonds; allocate 50% of the portfolio.",
    "The fund allocated 20% of its portfolio to bonds: allocate 50% of the portfolio.",
    "The fund allocated 20% of its portfolio to bonds therefore you should allocate 50% of available funds.",
    "The fund allocated 20% of its portfolio to bonds then allocate 50% of the portfolio.",
    "基金已将20%的组合资金配置于债券，然后配置50%的资金。",
    "Allocate, at most, 50% of the portfolio.",
    "Limit, if possible, portfolio exposure to 20%.",
    "建议配置，最多50%的资金。",
    "The fund allocated 20% of its portfolio to bonds and allocate 50% of the portfolio.",
    "基金已将20%的组合资金配置于债券，但配置50%的资金。",
    "Target, price is CNY 20.",
    "Price, target is CNY 20.",
    "Position, size: 50%.",
    "仓位五成。",
    "仓位约三成。",
    "Position: half.",
    "Portfolio exposure: 20%.",
    "Only allocate 50% of available funds.",
    "Now allocate 50% of available funds.",
    "The fund allocated 20% of its portfolio to bonds while allocate 50% of available funds.",
    "The fund allocated 20% of its portfolio to bonds, yet allocate 50% of available funds.",
    "The fund allocated 20% of its portfolio to bonds — allocate 50% of available funds.",
    "基金已将20%的组合资金配置于债券，同时配置50%的资金。",
    "基金已将20%的组合资金配置于债券——配置50%的资金。",
    "Target，price is CNY 20.",
    "Price，target is CNY 20.",
    "Position，size: 50%.",
    "目标，价格为20元。",
    "Allocation: 50%.",
    "持仓50%。",
    "持仓比例50%。",
    "建议持仓占比50%。",
    "建议持仓应为50%。",
    "仓位30%。",
    "目标股价为20元。",
    "股价目标为20元。",
)


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


@pytest.mark.parametrize("advice", TARGET_AND_ALLOCATION_ADVICE)
def test_rating_proposal_rejects_target_and_allocation_advice(advice: str) -> None:
    with pytest.raises(RatingProposalValidationError):
        parse_rating_proposal({**VALID_PROPOSAL, "confidence_explanation": advice})


@pytest.mark.parametrize(
    "fact",
    [
        "Capital expenditure increased by 20% year over year.",
        "Operating funds flow improved during the quarter.",
        "Capital allocation for capital expenditure increased by 20% year over year.",
        "资本开支同比增长20%。",
        "经营资金流较上季度改善。",
        "公司配置20亿元资金用于资本开支。",
        "经营资金配置效率同比提升20%。",
        "The board recommended a capital allocation of 20% to research equipment.",
        "公司公告称董事会建议配置20亿元资金用于资本开支。",
        "Exposure to operating funds fell by 20% during the quarter.",
        "Overseas revenue exposure fell to 20% during the quarter.",
        "The product portfolio generated 20% revenue growth.",
        "产品组合收入增长20%。",
        "组合包含20只股票。",
        "The company allocated 20% of its portfolio to research equipment.",
        "The fund allocated 20% of its portfolio to bonds.",
        "公司将20%的组合资金配置于研发设备。",
        "Management allocated half of the portfolio to bonds.",
        "管理层已将一半组合资金配置于债券。",
        "The fund allocated 20% of its portfolio to cash.",
        "The ETF allocated 20% of its portfolio to bonds.",
        "The fund invested 20% of its portfolio in bonds.",
        "The asset manager allocated 20% of its portfolio to bonds.",
        "基金已将20%的组合资金配置于现金。",
        "基金将20%的组合资金配置到债券。",
        "The fund allocated 20% of its portfolio toward bonds.",
        "The fund allocated 20% of its portfolio across bonds and cash.",
        "基金配置20%的组合资金于债券。",
        "The fund's equity position is 20%.",
    ],
)
def test_rating_proposal_allows_non_advisory_capital_and_cash_flow_facts(
    fact: str,
) -> None:
    proposal = parse_rating_proposal({**VALID_PROPOSAL, "confidence_explanation": fact})

    assert proposal.confidence_explanation == fact


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
