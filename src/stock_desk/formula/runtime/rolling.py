from __future__ import annotations

from collections import deque
from fractions import Fraction
import math

import numpy as np

from stock_desk.formula.runtime.base import KernelResult, RuntimeIssue
from stock_desk.formula.runtime.elementwise import condition_series, number_series
from stock_desk.formula.values import IntegerScalar, NumberSeries


_FLOAT_SCALE = 1 << 1074


def _window(value: object) -> int:
    if not isinstance(value, IntegerScalar):
        raise TypeError("window must be an integer scalar")
    return value.value


def _scaled_integers(source: NumberSeries) -> list[int]:
    scaled: list[int] = []
    for value, valid in zip(source.values, source.valid, strict=True):
        if not valid:
            scaled.append(0)
            continue
        numerator, denominator = float(value).as_integer_ratio()
        scaled.append(numerator * (_FLOAT_SCALE // denominator))
    return scaled


def _finite_float(numerator: int, denominator: int) -> float | None:
    try:
        value = float(Fraction(numerator, denominator))
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _exact_window(source: NumberSeries, n: int, operation: str) -> KernelResult:
    rows = len(source)
    scaled = _scaled_integers(source)
    values = np.zeros(rows, dtype=np.float64)
    valid = np.zeros(rows, dtype=np.bool_)
    overflow: list[int] = []
    running_sum = 0
    running_square_sum = 0
    valid_count = 0
    for index in range(rows):
        if source.valid[index]:
            current = scaled[index]
            running_sum += current
            running_square_sum += current * current
            valid_count += 1
        if n > 0 and index >= n and source.valid[index - n]:
            expired = scaled[index - n]
            running_sum -= expired
            running_square_sum -= expired * expired
            valid_count -= 1

        if operation == "sum":
            eligible = valid_count > 0
            numerator = running_sum
            denominator = _FLOAT_SCALE
        elif operation == "ma":
            eligible = index >= n - 1 and valid_count == n
            numerator = running_sum
            denominator = _FLOAT_SCALE * n
        else:
            eligible = index >= n - 1 and valid_count == n
            numerator = max(n * running_square_sum - running_sum * running_sum, 0)
            denominator = n * (n - 1) * _FLOAT_SCALE * _FLOAT_SCALE
        if not eligible:
            continue
        converted = _finite_float(numerator, denominator)
        if converted is None:
            overflow.append(index)
            continue
        values[index] = math.sqrt(converted) if operation == "std" else converted
        valid[index] = True
    issues = (
        (RuntimeIssue("numeric_overflow", len(overflow), overflow[0]),)
        if overflow
        else ()
    )
    return KernelResult(NumberSeries(values, valid), issues)


def ref(args: tuple[object, ...], rows: int) -> KernelResult:
    source = number_series(args[0], rows)
    offset = _window(args[1])
    values = np.zeros(rows, dtype=np.float64)
    valid = np.zeros(rows, dtype=np.bool_)
    if offset < rows:
        values[offset:] = source.values[: rows - offset]
        valid[offset:] = source.valid[: rows - offset]
    return KernelResult(NumberSeries(values, valid))


def ma(args: tuple[object, ...], rows: int) -> KernelResult:
    return _exact_window(number_series(args[0], rows), _window(args[1]), "ma")


def _count(args: tuple[object, ...], rows: int) -> KernelResult:
    source = number_series(args[0], rows)
    n = _window(args[1])
    data = (source.values != 0.0).astype(np.int64)
    prefix = np.concatenate(([0], np.cumsum(np.where(source.valid, data, 0))))
    valid_prefix = np.concatenate(([0], np.cumsum(source.valid.astype(np.int64))))
    values = np.zeros(rows, dtype=np.float64)
    valid = np.zeros(rows, dtype=np.bool_)
    for index in range(rows):
        start = 0 if n == 0 else max(0, index + 1 - n)
        if valid_prefix[index + 1] - valid_prefix[start]:
            values[index] = prefix[index + 1] - prefix[start]
            valid[index] = True
    return KernelResult(NumberSeries(values, valid))


def _extreme(args: tuple[object, ...], rows: int, operation: str) -> KernelResult:
    source = number_series(args[0], rows)
    n = _window(args[1])
    values = np.zeros(rows, dtype=np.float64)
    valid = np.zeros(rows, dtype=np.bool_)
    queue: deque[int] = deque()
    for index in range(rows):
        if n and queue and queue[0] < index + 1 - n:
            queue.popleft()
        if source.valid[index]:
            while queue and (
                source.values[queue[-1]] <= source.values[index]
                if operation == "max"
                else source.values[queue[-1]] >= source.values[index]
            ):
                queue.pop()
            queue.append(index)
        if queue:
            values[index] = source.values[queue[0]]
            valid[index] = True
    return KernelResult(NumberSeries(values, valid))


def hhv(args: tuple[object, ...], rows: int) -> KernelResult:
    return _extreme(args, rows, "max")


def llv(args: tuple[object, ...], rows: int) -> KernelResult:
    return _extreme(args, rows, "min")


def total(args: tuple[object, ...], rows: int) -> KernelResult:
    return _exact_window(number_series(args[0], rows), _window(args[1]), "sum")


def count(args: tuple[object, ...], rows: int) -> KernelResult:
    condition = condition_series(args[0], rows)
    numeric = NumberSeries(condition.values.astype(np.float64), condition.valid)
    return _count((numeric, args[1]), rows)


def std(args: tuple[object, ...], rows: int) -> KernelResult:
    return _exact_window(number_series(args[0], rows), _window(args[1]), "std")
