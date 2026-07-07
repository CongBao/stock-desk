from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Decimal,
    DecimalException,
    InvalidOperation,
    ROUND_HALF_UP,
    localcontext,
)
from typing import Literal, TypeAlias


COST_MODEL_VERSION = "a-share-cost-v1"
PRICE_QUANTUM = Decimal("0.0001")
MONEY_QUANTUM = Decimal("0.01")
BASIS_POINTS = Decimal("10000")

OrderSide: TypeAlias = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class CostModel:
    """Frozen parameters for the explicit ``a-share-cost-v1`` contract.

    The contract rounds slipped fill prices to four decimal places and each
    independently assessed fee to fen, both with ``ROUND_HALF_UP``. Fees and
    fills use the internally consistent price basis selected by the backtest
    (none/qfq/hfq); they are not representations of a raw-price cash account.
    """

    commission_bps: Decimal
    minimum_commission: Decimal
    sell_tax_bps: Decimal
    slippage_bps: Decimal
    version: str = COST_MODEL_VERSION

    def __post_init__(self) -> None:
        _validate_rate(self.commission_bps, field_name="commission_bps")
        _validate_nonnegative_decimal(
            self.minimum_commission, field_name="minimum_commission"
        )
        _validate_rate(self.sell_tax_bps, field_name="sell_tax_bps")
        _validate_rate(self.slippage_bps, field_name="slippage_bps")
        if self.version != COST_MODEL_VERSION:
            raise ValueError(f"version must be {COST_MODEL_VERSION}")
        for field_name in ("commission_bps", "sell_tax_bps", "slippage_bps"):
            value = getattr(self, field_name)
            if value.is_zero():
                object.__setattr__(self, field_name, value.copy_abs())
        object.__setattr__(
            self,
            "minimum_commission",
            _quantize(self.minimum_commission, MONEY_QUANTUM),
        )


@dataclass(frozen=True, slots=True)
class OrderCost:
    """One side's reference price, adverse fill, and independently rounded fees."""

    side: OrderSide
    reference_open: Decimal
    fill_price: Decimal
    quantity: int
    notional: Decimal
    slippage_cost: Decimal
    commission: Decimal
    sell_tax: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "reference_open",
            "fill_price",
            "notional",
            "slippage_cost",
            "commission",
            "sell_tax",
        ):
            value = getattr(self, field_name)
            if isinstance(value, Decimal) and value.is_zero():
                object.__setattr__(self, field_name, value.copy_abs())
        _validate_side(self.side)
        _validate_positive_decimal(self.reference_open, field_name="reference_open")
        _validate_positive_decimal(self.fill_price, field_name="fill_price")
        _validate_quantity(self.quantity)
        for field_name, value in (
            ("notional", self.notional),
            ("slippage_cost", self.slippage_cost),
            ("commission", self.commission),
            ("sell_tax", self.sell_tax),
        ):
            _validate_nonnegative_decimal(value, field_name=field_name)
            if value != round_money(value):
                raise ValueError(f"{field_name} must be rounded to fen")
        if self.reference_open != _quantize(self.reference_open, PRICE_QUANTUM):
            raise ValueError("reference_open must use the contract price precision")
        if self.fill_price != _quantize(self.fill_price, PRICE_QUANTUM):
            raise ValueError("fill_price must use the contract price precision")
        if self.side == "buy" and self.fill_price < self.reference_open:
            raise ValueError("buy fill cannot improve on reference_open")
        if self.side == "sell" and self.fill_price > self.reference_open:
            raise ValueError("sell fill cannot improve on reference_open")
        if self.notional != round_money(self.fill_price * self.quantity):
            raise ValueError("notional must equal fill price times quantity")
        expected_slippage = round_money(
            (
                self.fill_price - self.reference_open
                if self.side == "buy"
                else self.reference_open - self.fill_price
            )
            * self.quantity
        )
        if self.slippage_cost != expected_slippage:
            raise ValueError("slippage_cost does not bridge reference and fill prices")
        if self.side == "buy" and self.sell_tax != 0:
            raise ValueError("buy order cannot contain sell_tax")


def price_order(
    *,
    side: OrderSide,
    reference_open: Decimal,
    quantity: int,
    model: CostModel,
) -> OrderCost:
    """Apply adverse slippage and side-specific A-share costs to one order."""

    _validate_side(side)
    _validate_positive_decimal(reference_open, field_name="reference_open")
    _validate_quantity(quantity)
    if not isinstance(model, CostModel):
        raise TypeError("model must be a CostModel")

    reference = _quantize(reference_open, PRICE_QUANTUM)
    direction = Decimal("1") if side == "buy" else Decimal("-1")
    with localcontext() as context:
        context.prec = _calculation_precision(reference, model.slippage_bps)
        raw_fill = reference * (
            Decimal("1") + direction * model.slippage_bps / BASIS_POINTS
        )
    fill_price = _quantize(raw_fill, PRICE_QUANTUM)
    if fill_price <= 0:
        raise ValueError("slippage must leave a positive sell fill price")

    with localcontext() as context:
        context.prec = _calculation_precision(fill_price, Decimal(quantity))
        raw_notional = fill_price * quantity
        raw_slippage = (
            fill_price - reference if side == "buy" else reference - fill_price
        ) * quantity
    notional = _quantize(raw_notional, MONEY_QUANTUM)
    slippage_cost = _quantize(raw_slippage, MONEY_QUANTUM)

    with localcontext() as context:
        context.prec = _calculation_precision(notional, model.commission_bps)
        raw_commission = notional * model.commission_bps / BASIS_POINTS
    commission = max(
        model.minimum_commission,
        _quantize(raw_commission, MONEY_QUANTUM),
    )
    sell_tax = Decimal("0.00")
    if side == "sell":
        with localcontext() as context:
            context.prec = _calculation_precision(notional, model.sell_tax_bps)
            raw_tax = notional * model.sell_tax_bps / BASIS_POINTS
        sell_tax = _quantize(raw_tax, MONEY_QUANTUM)

    return OrderCost(
        side=side,
        reference_open=reference,
        fill_price=fill_price,
        quantity=quantity,
        notional=notional,
        slippage_cost=slippage_cost,
        commission=commission,
        sell_tax=sell_tax,
    )


def round_money(value: Decimal) -> Decimal:
    """Round an accounting amount to fen under the versioned contract."""

    return _quantize(value, MONEY_QUANTUM)


def divide_decimal(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Divide deterministically without rounding the stored ratio to display precision."""

    _validate_finite_decimal(numerator, field_name="numerator")
    _validate_finite_decimal(denominator, field_name="denominator")
    if denominator == 0:
        raise ZeroDivisionError("ratio denominator cannot be zero")
    try:
        with localcontext() as context:
            context.prec = 28
            context.rounding = ROUND_HALF_UP
            result = numerator / denominator
    except DecimalException as error:
        raise ValueError("ratio result must be a finite Decimal") from error
    if not result.is_finite():
        raise ValueError("ratio result must be a finite Decimal")
    return result.copy_abs() if result.is_zero() else result


def _validate_side(value: object) -> None:
    if type(value) is not str or value not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")


def _validate_rate(value: object, *, field_name: str) -> None:
    _validate_nonnegative_decimal(value, field_name=field_name)
    assert isinstance(value, Decimal)
    if value > BASIS_POINTS:
        raise ValueError(f"{field_name} cannot exceed 10000 basis points")


def _validate_nonnegative_decimal(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative finite Decimal")


def _validate_finite_decimal(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")


def _validate_positive_decimal(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be a Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a positive finite Decimal")


def _validate_quantity(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("quantity must be a positive integer")
    if value % 100 != 0:
        raise ValueError("quantity must use a 100-share lot")


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    precision = _calculation_precision(value, quantum)
    try:
        with localcontext() as context:
            context.prec = precision
            result = value.quantize(quantum, rounding=ROUND_HALF_UP)
            return result.copy_abs() if result.is_zero() else result
    except InvalidOperation as error:
        raise ValueError(
            "Decimal value exceeds the supported accounting range"
        ) from error


def _calculation_precision(*values: Decimal) -> int:
    integer_digits = max(
        (max(value.adjusted() + 1, 1) for value in values if value.is_finite()),
        default=1,
    )
    fractional_digits = max(
        (_fractional_digits(value) for value in values if value.is_finite()),
        default=0,
    )
    return max(50, integer_digits + fractional_digits + 16)


def _fractional_digits(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("accounting Decimal must be finite")
    return max(-exponent, 0)
