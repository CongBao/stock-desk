from __future__ import annotations

import numpy as np

from stock_desk.formula.runtime.base import KernelResult
from stock_desk.formula.runtime.elementwise import condition_series, number_series
from stock_desk.formula.values import BooleanSeries, IntegerScalar, NumberSeries


def cross(args: tuple[object, ...], rows: int) -> KernelResult:
    left = number_series(args[0], rows)
    right = number_series(args[1], rows)
    values = np.zeros(rows, dtype=np.bool_)
    valid = np.ones(rows, dtype=np.bool_)
    if rows > 1:
        comparable = (
            left.valid[1:] & right.valid[1:] & left.valid[:-1] & right.valid[:-1]
        )
        values[1:] = (
            comparable
            & (left.values[1:] > right.values[1:])
            & (left.values[:-1] <= right.values[:-1])
        )
    return KernelResult(BooleanSeries(values, valid))


def longcross(args: tuple[object, ...], rows: int) -> KernelResult:
    left = number_series(args[0], rows)
    right = number_series(args[1], rows)
    n = args[2]
    if not isinstance(n, IntegerScalar):
        raise TypeError("LONGCROSS window must be an integer scalar")
    prior = left.valid & right.valid & (left.values < right.values)
    prefix = np.concatenate(([0], np.cumsum(prior.astype(np.int64))))
    values = np.zeros(rows, dtype=np.bool_)
    for index in range(n.value, rows):
        complete = prefix[index] - prefix[index - n.value] == n.value
        values[index] = (
            complete
            and left.valid[index]
            and right.valid[index]
            and left.values[index] > right.values[index]
        )
    return KernelResult(BooleanSeries(values, np.ones(rows, dtype=np.bool_)))


def barslast(args: tuple[object, ...], rows: int) -> KernelResult:
    condition = condition_series(args[0], rows)
    values = np.zeros(rows, dtype=np.float64)
    valid = np.zeros(rows, dtype=np.bool_)
    last: int | None = None
    for index in range(rows):
        if condition.valid[index] and condition.values[index]:
            last = index
        if last is not None:
            values[index] = index - last
            valid[index] = True
    return KernelResult(NumberSeries(values, valid))


def filter_hits(args: tuple[object, ...], rows: int) -> KernelResult:
    condition = condition_series(args[0], rows)
    n = args[1]
    if not isinstance(n, IntegerScalar):
        raise TypeError("FILTER window must be an integer scalar")
    values = np.zeros(rows, dtype=np.bool_)
    blocked_through = -1
    for index in range(rows):
        if (
            index > blocked_through
            and condition.valid[index]
            and condition.values[index]
        ):
            values[index] = True
            blocked_through = index + n.value
    return KernelResult(BooleanSeries(values, np.ones(rows, dtype=np.bool_)))
