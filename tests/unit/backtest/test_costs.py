from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from stock_desk.backtest.costs import (
    COST_MODEL_VERSION,
    MONEY_QUANTUM,
    PRICE_QUANTUM,
    CostModel,
    divide_decimal,
    price_order,
    round_money,
)


def test_buy_and_sell_slippage_are_both_adverse() -> None:
    model = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("3"),
    )

    buy = price_order(
        side="buy", reference_open=Decimal("10"), quantity=1_000, model=model
    )
    sell = price_order(
        side="sell", reference_open=Decimal("11"), quantity=1_000, model=model
    )

    assert model.version == COST_MODEL_VERSION == "a-share-cost-v1"
    assert PRICE_QUANTUM == Decimal("0.0001")
    assert MONEY_QUANTUM == Decimal("0.01")
    assert buy.fill_price == Decimal("10.0030")
    assert sell.fill_price == Decimal("10.9967")
    assert buy.slippage_cost == Decimal("3.00")
    assert sell.slippage_cost == Decimal("3.30")


def test_commission_applies_to_both_sides_with_independent_minimum() -> None:
    model = CostModel(
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    buy = price_order(
        side="buy", reference_open=Decimal("10"), quantity=100, model=model
    )
    sell = price_order(
        side="sell", reference_open=Decimal("11"), quantity=100, model=model
    )

    assert buy.commission == Decimal("5.00")
    assert sell.commission == Decimal("5.00")


def test_fractional_commission_rounds_half_up_to_fen() -> None:
    model = CostModel(
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("3"),
    )

    priced = price_order(
        side="buy", reference_open=Decimal("10"), quantity=10_000, model=model
    )

    assert priced.notional == Decimal("100030.00")
    assert priced.commission == Decimal("25.01")


def test_sell_tax_applies_only_to_sell_fill_notional() -> None:
    model = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("5"),
        slippage_bps=Decimal("3"),
    )

    buy = price_order(
        side="buy", reference_open=Decimal("10"), quantity=10_000, model=model
    )
    sell = price_order(
        side="sell", reference_open=Decimal("11"), quantity=10_000, model=model
    )

    assert buy.sell_tax == Decimal("0.00")
    assert sell.notional == Decimal("109967.00")
    assert sell.sell_tax == Decimal("54.98")


def test_zero_cost_model_preserves_reference_open() -> None:
    model = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    priced = price_order(
        side="sell",
        reference_open=Decimal("10.12345"),
        quantity=1_000,
        model=model,
    )

    assert priced.reference_open == Decimal("10.1235")
    assert priced.fill_price == Decimal("10.1235")
    assert priced.slippage_cost == Decimal("0.00")
    assert priced.commission == Decimal("0.00")
    assert priced.sell_tax == Decimal("0.00")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"commission_bps": Decimal("-1")}, "commission_bps"),
        ({"commission_bps": 1}, "commission_bps"),
        ({"minimum_commission": Decimal("-1")}, "minimum_commission"),
        ({"sell_tax_bps": Decimal("10001")}, "sell_tax_bps"),
        ({"slippage_bps": Decimal("NaN")}, "slippage_bps"),
        ({"version": "future"}, "version"),
    ],
)
def test_cost_model_rejects_invalid_values(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "commission_bps": Decimal("2.5"),
        "minimum_commission": Decimal("5"),
        "sell_tax_bps": Decimal("5"),
        "slippage_bps": Decimal("3"),
    }
    values.update(changes)

    with pytest.raises((TypeError, ValueError), match=message):
        CostModel(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("side", "reference_open", "quantity", "message"),
    [
        ("hold", Decimal("10"), 1_000, "side"),
        ("buy", Decimal("0"), 1_000, "reference_open"),
        ("buy", Decimal("Infinity"), 1_000, "reference_open"),
        ("buy", Decimal("10"), 0, "quantity"),
        ("buy", Decimal("10"), 150, "100-share"),
        ("buy", Decimal("10"), True, "quantity"),
    ],
)
def test_price_order_rejects_invalid_inputs(
    side: object, reference_open: Decimal, quantity: object, message: str
) -> None:
    model = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    with pytest.raises((TypeError, ValueError), match=message):
        price_order(
            side=side,  # type: ignore[arg-type]
            reference_open=reference_open,
            quantity=quantity,  # type: ignore[arg-type]
            model=model,
        )


def test_sell_slippage_cannot_create_a_nonpositive_fill() -> None:
    model = CostModel(
        commission_bps=Decimal("0"),
        minimum_commission=Decimal("0"),
        sell_tax_bps=Decimal("0"),
        slippage_bps=Decimal("10000"),
    )

    with pytest.raises(ValueError, match="positive sell fill"):
        price_order(
            side="sell", reference_open=Decimal("10"), quantity=1_000, model=model
        )


def test_cost_values_are_frozen_value_objects() -> None:
    left = CostModel(
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        sell_tax_bps=Decimal("5"),
        slippage_bps=Decimal("3"),
    )
    right = CostModel(
        commission_bps=Decimal("2.50"),
        minimum_commission=Decimal("5.00"),
        sell_tax_bps=Decimal("5.0"),
        slippage_bps=Decimal("3.0"),
    )

    assert left == right
    assert hash(left) == hash(right)
    with pytest.raises((AttributeError, TypeError)):
        left.slippage_bps = Decimal("4")  # type: ignore[misc]


def test_order_cost_rejects_an_inconsistent_value_copy() -> None:
    model = CostModel(
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        sell_tax_bps=Decimal("5"),
        slippage_bps=Decimal("3"),
    )
    priced = price_order(
        side="sell", reference_open=Decimal("11"), quantity=1_000, model=model
    )

    with pytest.raises(ValueError, match="notional"):
        replace(priced, notional=Decimal("999.00"))


@pytest.mark.parametrize(
    ("side", "changes", "message"),
    [
        ("buy", {"commission": Decimal("1.001")}, "commission"),
        ("buy", {"reference_open": Decimal("10.00001")}, "reference_open"),
        ("buy", {"fill_price": Decimal("9")}, "buy fill"),
        ("sell", {"fill_price": Decimal("12")}, "sell fill"),
        ("sell", {"slippage_cost": Decimal("0")}, "slippage_cost"),
        ("buy", {"sell_tax": Decimal("1")}, "sell_tax"),
    ],
)
def test_order_cost_value_object_enforces_accounting_invariants(
    side: str, changes: dict[str, object], message: str
) -> None:
    model = CostModel(
        commission_bps=Decimal("2.5"),
        minimum_commission=Decimal("5"),
        sell_tax_bps=Decimal("5"),
        slippage_bps=Decimal("3"),
    )
    priced = price_order(
        side=side,  # type: ignore[arg-type]
        reference_open=Decimal("10"),
        quantity=1_000,
        model=model,
    )

    with pytest.raises(ValueError, match=message):
        replace(priced, **changes)  # type: ignore[arg-type]


def test_price_order_requires_a_cost_model() -> None:
    with pytest.raises(TypeError, match="CostModel"):
        price_order(
            side="buy",
            reference_open=Decimal("10"),
            quantity=1_000,
            model=object(),  # type: ignore[arg-type]
        )


def test_decimal_ratio_rejects_zero_denominator() -> None:
    with pytest.raises(ZeroDivisionError, match="denominator"):
        divide_decimal(Decimal("1"), Decimal("0"))


def test_decimal_ratio_rejects_a_nonfinite_result() -> None:
    with pytest.raises(ValueError, match="result"):
        divide_decimal(Decimal("1E+999999"), Decimal("1E-999999"))


@pytest.mark.parametrize(
    ("numerator", "denominator", "message"),
    [
        (1, Decimal("1"), "numerator"),
        (True, Decimal("1"), "numerator"),
        (Decimal("1"), 1.0, "denominator"),
        (Decimal("NaN"), Decimal("1"), "numerator"),
        (Decimal("1"), Decimal("Infinity"), "denominator"),
    ],
)
def test_decimal_ratio_rejects_non_decimal_or_nonfinite_operands(
    numerator: object, denominator: object, message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        divide_decimal(
            numerator,  # type: ignore[arg-type]
            denominator,  # type: ignore[arg-type]
        )


def test_rounded_zero_is_canonical_positive_zero() -> None:
    rounded = round_money(Decimal("-0.001"))
    model = CostModel(
        commission_bps=Decimal("-0"),
        minimum_commission=Decimal("-0.000"),
        sell_tax_bps=Decimal("-0"),
        slippage_bps=Decimal("-0"),
    )
    priced = price_order(
        side="buy", reference_open=Decimal("10"), quantity=100, model=model
    )

    assert rounded == Decimal("0.00")
    assert rounded.as_tuple().sign == 0
    assert all(
        value.as_tuple().sign == 0
        for value in (
            model.commission_bps,
            model.minimum_commission,
            model.sell_tax_bps,
            model.slippage_bps,
            priced.commission,
            priced.sell_tax,
            priced.slippage_cost,
        )
    )
    assert json.dumps({"amount": str(rounded)}) == '{"amount": "0.00"}'
