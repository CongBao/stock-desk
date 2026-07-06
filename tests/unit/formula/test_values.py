from __future__ import annotations

from dataclasses import FrozenInstanceError
import math

import numpy as np
import pytest

from stock_desk.formula.values import (
    MAX_BAR_SERIES_ROWS,
    BooleanSeries,
    IntegerScalar,
    NumberScalar,
    NumberSeries,
)


def test_number_series_uses_an_independent_read_only_validity_mask() -> None:
    values = np.array([1.5, -0.0, 99.0], dtype=np.float64)
    valid = np.array([True, True, False], dtype=np.bool_)

    series = NumberSeries(values, valid)
    values[:] = 7.0
    valid[:] = True

    assert series.to_optional_tuple() == (1.5, 0.0, None)
    assert not series.values.flags.writeable
    assert not series.valid.flags.writeable
    with pytest.raises(ValueError):
        series.values[0] = 2.0
    with pytest.raises(ValueError):
        series.values.flags.writeable = True
    with pytest.raises(FrozenInstanceError):
        series._values = np.array([], dtype=np.float64)  # type: ignore[misc]
    assert not hasattr(series, "__dict__")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_number_contracts_reject_non_finite_values(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        NumberScalar(value)
    with pytest.raises(ValueError, match="finite"):
        NumberSeries(np.array([value]), np.array([True]))


def test_scalar_and_series_broadcasts_are_typed_and_canonical() -> None:
    number = NumberScalar(-0.0)
    integer = IntegerScalar(3)

    assert number.value == 0.0
    assert math.copysign(1.0, number.value) == 1.0
    assert number.broadcast(2).to_optional_tuple() == (0.0, 0.0)
    assert integer.broadcast(2).to_optional_tuple() == (3.0, 3.0)
    assert BooleanSeries.from_optional((True, None, False)).to_optional_tuple() == (
        True,
        None,
        False,
    )


def test_series_reject_invalid_shapes_dtypes_and_limits_before_copy() -> None:
    with pytest.raises(ValueError, match="one-dimensional"):
        NumberSeries(np.array(1.0), np.array(True))
    with pytest.raises(ValueError, match="one-dimensional"):
        BooleanSeries(np.array(True), np.array(True))
    with pytest.raises(ValueError, match="same length"):
        NumberSeries(np.array([1.0]), np.array([True, False]))
    with pytest.raises(TypeError, match="float64"):
        NumberSeries(np.array([1], dtype=np.int64), np.array([True]))
    with pytest.raises(TypeError, match="bool"):
        BooleanSeries(np.array([1], dtype=np.int64), np.array([True]))
    with pytest.raises(ValueError, match="row limit"):
        NumberSeries.from_optional((None,) * (MAX_BAR_SERIES_ROWS + 1))


def test_integer_scalar_rejects_bool_and_series_normalizes_invalid_slots() -> None:
    with pytest.raises(TypeError, match="integer"):
        IntegerScalar(True)  # type: ignore[arg-type]

    series = NumberSeries(
        np.array([1.0, -0.0, 88.0], dtype=np.float64),
        np.array([True, True, False], dtype=np.bool_),
    )
    assert series.values.tolist() == [1.0, 0.0, 0.0]


@pytest.mark.parametrize("value", [True, 1, "1", math.nan, math.inf, -math.inf])
def test_number_from_optional_requires_exact_finite_floats(value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="finite float"):
        NumberSeries.from_optional((value,))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1, 0, "true", 1.0])
def test_boolean_from_optional_requires_exact_booleans(value: object) -> None:
    with pytest.raises(TypeError, match="boolean"):
        BooleanSeries.from_optional((value,))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [2**53 + 1, -(2**53 + 1)])
def test_integer_scalar_rejects_values_not_exactly_representable_by_float64(
    value: int,
) -> None:
    with pytest.raises(ValueError, match="float64"):
        IntegerScalar(value)


def test_integer_scalar_boundary_broadcast_is_exact() -> None:
    assert IntegerScalar(2**53).broadcast(1).to_optional_tuple() == (float(2**53),)
