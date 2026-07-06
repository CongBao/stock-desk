from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from stock_desk.formula.runtime import recursive, rolling, signals
from stock_desk.formula.runtime.base import KernelResult
from stock_desk.formula.runtime.elementwise import binary_number, choose, unary_number
from stock_desk.formula.compiler import MAX_LOOKBACK
from stock_desk.formula.functions import V1_REGISTRY, accepts_value_kind
from stock_desk.formula.functions.base import ValueKind
from stock_desk.formula.values import (
    BooleanSeries,
    IntegerScalar,
    NumberScalar,
    NumberSeries,
)
from stock_desk.market.types import MAX_BAR_SERIES_ROWS


class KernelInputError(ValueError):
    """A direct kernel call failed stable metadata preflight."""


@dataclass(frozen=True, slots=True)
class Kernel:
    result_kind: Literal["number_series", "boolean_series"]
    execute: Callable[[tuple[object, ...], int], KernelResult]


def _abs(args: tuple[object, ...], rows: int) -> KernelResult:
    return unary_number("ABS", args[0], rows)


def _max(args: tuple[object, ...], rows: int) -> KernelResult:
    return binary_number("MAX", args[0], args[1], rows)


def _min(args: tuple[object, ...], rows: int) -> KernelResult:
    return binary_number("MIN", args[0], args[1], rows)


def _if(args: tuple[object, ...], rows: int) -> KernelResult:
    return choose(args[0], args[1], args[2], rows)


KERNELS = MappingProxyType(
    {
        "math.abs": Kernel("number_series", _abs),
        "math.max": Kernel("number_series", _max),
        "math.min": Kernel("number_series", _min),
        "logic.if": Kernel("number_series", _if),
        "series.ref": Kernel("number_series", rolling.ref),
        "series.ma": Kernel("number_series", rolling.ma),
        "series.ema": Kernel("number_series", recursive.ema),
        "series.sma": Kernel("number_series", recursive.sma),
        "series.hhv": Kernel("number_series", rolling.hhv),
        "series.llv": Kernel("number_series", rolling.llv),
        "series.sum": Kernel("number_series", rolling.total),
        "series.count": Kernel("number_series", rolling.count),
        "statistics.std": Kernel("number_series", rolling.std),
        "signal.cross": Kernel("boolean_series", signals.cross),
        "signal.longcross": Kernel("boolean_series", signals.longcross),
        "signal.barslast": Kernel("number_series", signals.barslast),
        "signal.filter": Kernel("boolean_series", signals.filter_hits),
    }
)

_SPECS = MappingProxyType({spec.dispatch_key: spec for spec in V1_REGISTRY.functions()})


def _kind(value: object) -> ValueKind:
    if type(value) is IntegerScalar:
        return "integer_scalar"
    if type(value) is NumberScalar:
        return "scalar"
    if type(value) is NumberSeries:
        return "number_series"
    if type(value) is BooleanSeries:
        return "boolean_series"
    raise KernelInputError("kernel argument has an unsupported runtime type")


def _preflight(key: str, args: tuple[object, ...], row_count: int) -> None:
    if type(key) is not str or type(args) is not tuple:
        raise KernelInputError("kernel key and arguments are not canonical")
    if type(row_count) is not int or not 0 <= row_count <= MAX_BAR_SERIES_ROWS:
        raise KernelInputError("kernel row count is outside the public limit")
    spec = _SPECS.get(key)
    if spec is None:
        raise KernelInputError("formula dispatch key is not registered")
    if not spec.min_args <= len(args) <= spec.max_args:
        raise KernelInputError("kernel argument count is invalid")
    scalar_values: dict[str, float] = {}
    for parameter, argument in zip(spec.parameters, args, strict=True):
        kind = _kind(argument)
        if not accepts_value_kind(parameter.accepted_kinds, kind):
            raise KernelInputError("kernel argument kind is invalid")
        if isinstance(argument, (NumberSeries, BooleanSeries)):
            if len(argument) != row_count:
                raise KernelInputError("kernel series length does not match row count")
            if parameter.constant:
                raise KernelInputError("kernel constant argument must be scalar")
        if isinstance(argument, (IntegerScalar, NumberScalar)):
            value = float(argument.value)
            scalar_values[parameter.name] = value
            if parameter.minimum is not None and value < parameter.minimum:
                raise KernelInputError("kernel scalar is below its minimum")
            if parameter.maximum is not None and value > parameter.maximum:
                raise KernelInputError("kernel scalar exceeds its maximum")
            if parameter.name == "N" and value > MAX_LOOKBACK:
                raise KernelInputError("kernel lookback exceeds its maximum")
    relations = {
        "<=": lambda left, right: left <= right,
        "<": lambda left, right: left < right,
        ">=": lambda left, right: left >= right,
        ">": lambda left, right: left > right,
        "==": lambda left, right: left == right,
    }
    for relation in spec.relations:
        if relation.left not in scalar_values or relation.right not in scalar_values:
            raise KernelInputError("kernel relation requires scalar arguments")
        if not relations[relation.operator](
            scalar_values[relation.left], scalar_values[relation.right]
        ):
            raise KernelInputError("kernel scalar relation is invalid")


def _postflight(key: str, result: KernelResult, row_count: int) -> None:
    expected = _SPECS[key].result_kind
    if expected == "number_series" and type(result.value) is not NumberSeries:
        raise KernelInputError("kernel result kind does not match metadata")
    if expected == "boolean_series" and type(result.value) is not BooleanSeries:
        raise KernelInputError("kernel result kind does not match metadata")
    if len(result.value) != row_count:
        raise KernelInputError("kernel result length does not match row count")
    for issue in result.issues:
        if (
            type(issue.count) is not int
            or type(issue.first_index) is not int
            or not 0 < issue.count <= row_count
            or not 0 <= issue.first_index < row_count
        ):
            raise KernelInputError("kernel issue is outside the input rows")


def execute_kernel(key: str, args: tuple[object, ...], row_count: int) -> KernelResult:
    _preflight(key, args, row_count)
    result = KERNELS[key].execute(args, row_count)
    _postflight(key, result, row_count)
    return result
