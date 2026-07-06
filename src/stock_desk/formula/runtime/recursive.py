from __future__ import annotations

import numpy as np

from stock_desk.formula.runtime.base import KernelResult, RuntimeIssue
from stock_desk.formula.runtime.elementwise import number_series
from stock_desk.formula.values import IntegerScalar, NumberSeries


def _smooth(args: tuple[object, ...], rows: int, *, ema: bool) -> KernelResult:
    source = number_series(args[0], rows)
    n = args[1]
    if not isinstance(n, IntegerScalar):
        raise TypeError("smoothing window must be an integer scalar")
    m = 2 if ema else args[2]
    if not isinstance(m, IntegerScalar):
        raise TypeError("smoothing numerator must be an integer scalar")
    values = np.zeros(rows, dtype=np.float64)
    valid = np.zeros(rows, dtype=np.bool_)
    state = 0.0
    initialized = False
    overflow: list[int] = []
    alpha = m.value / n.value
    for index in range(rows):
        if not source.valid[index]:
            continue
        current = source.values[index]
        if initialized:
            with np.errstate(all="ignore"):
                candidate = alpha * current + (1.0 - alpha) * state
            if not np.isfinite(candidate):
                overflow.append(index)
                continue
            state = float(candidate)
        else:
            state = current
            initialized = True
        values[index] = state
        valid[index] = True
    issues = (
        (RuntimeIssue("numeric_overflow", len(overflow), overflow[0]),)
        if overflow
        else ()
    )
    return KernelResult(NumberSeries(values, valid), issues)


def ema(args: tuple[object, ...], rows: int) -> KernelResult:
    n = args[1]
    if not isinstance(n, IntegerScalar):
        raise TypeError("EMA window must be an integer scalar")
    return _smooth(
        (args[0], IntegerScalar(n.value + 1), IntegerScalar(2)), rows, ema=False
    )


def sma(args: tuple[object, ...], rows: int) -> KernelResult:
    return _smooth(args, rows, ema=False)
