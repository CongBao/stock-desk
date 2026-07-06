from __future__ import annotations

from collections.abc import Callable

import numpy as np

from stock_desk.formula.runtime.base import KernelResult, RuntimeIssue
from stock_desk.formula.values import (
    BooleanSeries,
    IntegerScalar,
    NumberScalar,
    NumberSeries,
    ScalarValue,
)


Numeric = NumberSeries | ScalarValue
Condition = NumberSeries | BooleanSeries | ScalarValue


def number_series(value: object, row_count: int) -> NumberSeries:
    if isinstance(value, NumberSeries):
        return value
    if isinstance(value, (NumberScalar, IntegerScalar)):
        return value.broadcast(row_count)
    raise TypeError("numeric kernel received a non-numeric value")


def condition_series(value: object, row_count: int) -> BooleanSeries:
    if isinstance(value, BooleanSeries):
        return value
    numeric = number_series(value, row_count)
    return BooleanSeries(numeric.values != 0.0, numeric.valid)


def sanitize_number(
    values: np.ndarray, valid: np.ndarray
) -> tuple[NumberSeries, tuple[RuntimeIssue, ...]]:
    finite = np.isfinite(values)
    overflow = valid & ~finite
    canonical_valid = valid & finite
    canonical = np.where(canonical_valid, values, 0.0).astype(np.float64)
    issues: tuple[RuntimeIssue, ...] = ()
    indexes = np.flatnonzero(overflow)
    if len(indexes):
        issues = (RuntimeIssue("numeric_overflow", len(indexes), int(indexes[0])),)
    return NumberSeries(canonical, canonical_valid), issues


def unary_number(operator: str, value: object, row_count: int) -> KernelResult:
    source = number_series(value, row_count)
    with np.errstate(all="ignore"):
        if operator == "+":
            result = source.values.copy()
        elif operator == "-":
            result = -source.values
        elif operator == "ABS":
            result = np.abs(source.values)
        else:
            raise ValueError("unsupported numeric unary operator")
    series, issues = sanitize_number(result, source.valid.copy())
    return KernelResult(series, issues)


def binary_number(
    operator: str, left: object, right: object, row_count: int | None = None
) -> KernelResult:
    if row_count is None:
        row_count = len(left) if isinstance(left, NumberSeries) else len(right)  # type: ignore[arg-type]
    lhs = number_series(left, row_count)
    rhs = number_series(right, row_count)
    valid = lhs.valid & rhs.valid
    zero = rhs.values == 0.0
    invalid_zero = (
        valid & zero if operator in {"/", "%"} else np.zeros(row_count, dtype=np.bool_)
    )
    valid = valid & ~invalid_zero
    with np.errstate(all="ignore"):
        operations: dict[str, Callable[[], np.ndarray]] = {
            "+": lambda: lhs.values + rhs.values,
            "-": lambda: lhs.values - rhs.values,
            "*": lambda: lhs.values * rhs.values,
            "/": lambda: lhs.values / rhs.values,
            "%": lambda: np.remainder(lhs.values, rhs.values),
            "MAX": lambda: np.maximum(lhs.values, rhs.values),
            "MIN": lambda: np.minimum(lhs.values, rhs.values),
        }
        try:
            values = operations[operator]()
        except KeyError as error:
            raise ValueError("unsupported numeric binary operator") from error
    series, overflow = sanitize_number(values, valid)
    issues: list[RuntimeIssue] = []
    indexes = np.flatnonzero(invalid_zero)
    if len(indexes):
        code = "division_by_zero" if operator == "/" else "modulo_by_zero"
        issues.append(RuntimeIssue(code, len(indexes), int(indexes[0])))
    issues.extend(overflow)
    return KernelResult(series, tuple(sorted(issues, key=lambda item: item.code)))


def compare(operator: str, left: object, right: object, row_count: int) -> KernelResult:
    lhs = number_series(left, row_count)
    rhs = number_series(right, row_count)
    operations = {
        "=": np.equal,
        "==": np.equal,
        "!=": np.not_equal,
        "<>": np.not_equal,
        ">": np.greater,
        ">=": np.greater_equal,
        "<": np.less,
        "<=": np.less_equal,
    }
    try:
        values = operations[operator](lhs.values, rhs.values)
    except KeyError as error:
        raise ValueError("unsupported comparison operator") from error
    return KernelResult(BooleanSeries(values, lhs.valid & rhs.valid))


def boolean(
    operator: str, left: object, right: object | None, row_count: int
) -> KernelResult:
    lhs = condition_series(left, row_count)
    if operator == "NOT":
        return KernelResult(BooleanSeries(~lhs.values, lhs.valid))
    assert right is not None
    rhs = condition_series(right, row_count)
    values = lhs.values & rhs.values if operator == "AND" else lhs.values | rhs.values
    return KernelResult(BooleanSeries(values, lhs.valid & rhs.valid))


def choose(condition: object, yes: object, no: object, row_count: int) -> KernelResult:
    test = condition_series(condition, row_count)
    left = number_series(yes, row_count)
    right = number_series(no, row_count)
    selected_valid = np.where(test.values, left.valid, right.valid)
    valid = test.valid & selected_valid
    values = np.where(test.values, left.values, right.values)
    series, issues = sanitize_number(values, valid)
    return KernelResult(series, issues)
