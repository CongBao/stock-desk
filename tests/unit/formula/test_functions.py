from __future__ import annotations

import math

import pytest

from stock_desk.formula.runtime.dispatch import KernelInputError, execute_kernel
from stock_desk.formula.values import BooleanSeries, IntegerScalar, NumberSeries


def ns(*values: float | None) -> NumberSeries:
    return NumberSeries.from_optional(values)


def bs(*values: bool | None) -> BooleanSeries:
    return BooleanSeries.from_optional(values)


def out(name: str, *args: object) -> tuple[object, tuple[object, ...]]:
    result = execute_kernel(name, args, 5)
    value = result.value
    return value, result.issues


def test_pointwise_functions_and_if_only_propagate_selected_branch() -> None:
    absolute, _ = out("math.abs", ns(-1.0, None, -0.0, 3.0, -4.0))
    maximum, _ = out(
        "math.max", ns(1.0, None, 4.0, 1.0, 7.0), ns(2.0, 3.0, 2.0, 1.0, 6.0)
    )
    minimum, _ = out(
        "math.min", ns(1.0, None, 4.0, 1.0, 7.0), ns(2.0, 3.0, 2.0, 1.0, 6.0)
    )
    selected, _ = out(
        "logic.if",
        bs(True, False, None, True, False),
        ns(1.0, None, 3.0, 4.0, None),
        ns(None, 2.0, 8.0, None, 5.0),
    )

    assert absolute.to_optional_tuple() == (1.0, None, 0.0, 3.0, 4.0)
    assert maximum.to_optional_tuple() == (2.0, None, 4.0, 1.0, 7.0)
    assert minimum.to_optional_tuple() == (1.0, None, 2.0, 1.0, 6.0)
    assert selected.to_optional_tuple() == (1.0, 2.0, None, 4.0, 5.0)


def test_ref_rolling_and_zero_window_semantics() -> None:
    ref, _ = out("series.ref", ns(1.0, None, 3.0, 4.0, 5.0), IntegerScalar(2))
    ma, _ = out("series.ma", ns(1.0, 2.0, None, 4.0, 5.0), IntegerScalar(2))
    total, _ = out("series.sum", ns(1.0, None, 3.0, 4.0, None), IntegerScalar(2))
    cumulative, _ = out("series.sum", ns(None, 2.0, None, 4.0, 1.0), IntegerScalar(0))
    count, _ = out("series.count", ns(0.0, None, 2.0, -1.0, 0.0), IntegerScalar(2))
    hhv, _ = out("series.hhv", ns(None, 2.0, 1.0, None, 4.0), IntegerScalar(2))
    llv, _ = out("series.llv", ns(None, 2.0, 1.0, None, 4.0), IntegerScalar(2))

    assert ref.to_optional_tuple() == (None, None, 1.0, None, 3.0)
    assert ma.to_optional_tuple() == (None, 1.5, None, None, 4.5)
    assert total.to_optional_tuple() == (1.0, 1.0, 3.0, 7.0, 4.0)
    assert cumulative.to_optional_tuple() == (None, 2.0, 2.0, 6.0, 7.0)
    assert count.to_optional_tuple() == (0.0, 0.0, 1.0, 2.0, 1.0)
    assert hhv.to_optional_tuple() == (None, 2.0, 2.0, 1.0, 4.0)
    assert llv.to_optional_tuple() == (None, 2.0, 1.0, 1.0, 4.0)


def test_std_ema_and_sma_frozen_semantics() -> None:
    source = ns(1.0, 2.0, None, 4.0, 5.0)
    std, _ = out("statistics.std", source, IntegerScalar(2))
    ema, _ = out("series.ema", source, IntegerScalar(3))
    sma, _ = out("series.sma", source, IntegerScalar(2), IntegerScalar(1))

    assert std.to_optional_tuple()[0] is None
    assert std.to_optional_tuple()[1] == pytest.approx(math.sqrt(0.5))
    assert std.to_optional_tuple()[2:4] == (None, None)
    assert ema.to_optional_tuple() == sma.to_optional_tuple()
    assert ema.to_optional_tuple() == (1.0, 1.5, None, 2.75, 3.875)


def test_signal_boundaries_longcross_barslast_and_filter() -> None:
    cross, _ = out(
        "signal.cross", ns(1.0, 2.0, 2.0, 3.0, 2.0), ns(2.0, 2.0, 2.0, 2.0, 2.0)
    )
    longcross, _ = out(
        "signal.longcross",
        ns(1.0, 1.0, 1.0, 3.0, 4.0),
        ns(2.0, 2.0, 2.0, 2.0, 3.0),
        IntegerScalar(3),
    )
    barslast, _ = out("signal.barslast", bs(False, None, True, False, False))
    filtered, _ = out(
        "signal.filter", bs(True, True, False, True, True), IntegerScalar(2)
    )

    assert cross.to_optional_tuple() == (False, False, False, True, False)
    assert longcross.to_optional_tuple() == (False, False, False, True, False)
    assert barslast.to_optional_tuple() == (None, None, 0.0, 1.0, 2.0)
    assert filtered.to_optional_tuple() == (True, False, False, True, False)


def test_invalid_math_is_null_canonical_and_aggregated() -> None:
    from stock_desk.formula.runtime.elementwise import binary_number

    result = binary_number(
        "/", ns(1.0, 0.0, 1e308, -0.0, 2.0), ns(0.0, -0.0, 1e-308, 2.0, 1.0)
    )
    assert result.value.to_optional_tuple() == (None, None, None, 0.0, 2.0)
    assert [
        (issue.code, issue.count, issue.first_index) for issue in result.issues
    ] == [
        ("division_by_zero", 2, 0),
        ("numeric_overflow", 1, 2),
    ]
    assert all(
        value is None or math.isfinite(value)
        for value in result.value.to_optional_tuple()
    )

    modulo = binary_number(
        "%", ns(1.0, 2.0, 3.0, 4.0, 5.0), ns(1.0, 0.0, -0.0, 3.0, 2.0)
    )
    assert modulo.value.to_optional_tuple() == (0.0, None, None, 1.0, 1.0)
    assert [(issue.code, issue.count) for issue in modulo.issues] == [
        ("modulo_by_zero", 2)
    ]

    rolling = execute_kernel(
        "series.sum",
        (ns(1e308, 1e308, 1.0, 1.0, 1.0), IntegerScalar(2)),
        5,
    )
    assert rolling.value.to_optional_tuple()[0] == 1e308
    assert rolling.value.to_optional_tuple()[1:] == (None, 1e308, 2.0, 2.0)
    assert rolling.issues[0].code == "numeric_overflow"


def test_std_is_exact_for_large_nearby_values_and_constant_extremes() -> None:
    constant = execute_kernel(
        "statistics.std", (ns(1e308, 1e308), IntegerScalar(2)), 2
    ).value
    nearby = execute_kernel(
        "statistics.std", (ns(1e16, 1e16 + 2.0), IntegerScalar(2)), 2
    ).value
    assert constant.to_optional_tuple() == (None, 0.0)
    assert nearby.to_optional_tuple()[1] == pytest.approx(math.sqrt(2.0))


def test_recursive_smoothing_avoids_intermediate_overflow() -> None:
    source = ns(1e308, 1e308, -1e308, -1e308, 1e308)
    ema = execute_kernel("series.ema", (source, IntegerScalar(3)), 5)
    sma = execute_kernel("series.sma", (source, IntegerScalar(2), IntegerScalar(1)), 5)
    assert ema.value.to_optional_tuple() == sma.value.to_optional_tuple()
    assert all(
        value is not None and math.isfinite(value)
        for value in ema.value.to_optional_tuple()
    )
    assert ema.issues == sma.issues == ()


@pytest.mark.parametrize(
    ("key", "args", "rows"),
    [
        ("series.ma", (ns(1.0),), 1),
        ("series.ma", (ns(1.0), IntegerScalar(0)), 1),
        ("series.ma", (ns(1.0, 2.0), IntegerScalar(1)), 1),
        ("signal.filter", (bs(True), IntegerScalar(100_001)), 1),
        ("series.ma", (ns(1.0), IntegerScalar(1)), -1),
    ],
)
def test_dispatch_preflight_rejects_invalid_direct_calls(
    key: str, args: tuple[object, ...], rows: int
) -> None:
    with pytest.raises(KernelInputError):
        execute_kernel(key, args, rows)
