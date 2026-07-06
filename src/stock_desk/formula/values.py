from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import math

import numpy as np
import numpy.typing as npt

from stock_desk.market.types import MAX_BAR_SERIES_ROWS


FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]


def _check_row_count(row_count: int) -> None:
    if type(row_count) is not int or row_count < 0:
        raise ValueError("series row count must be a non-negative integer")
    if row_count > MAX_BAR_SERIES_ROWS:
        raise ValueError("formula series exceeds the public row limit")


def _immutable_float_array(values: FloatArray, valid: BoolArray) -> FloatArray:
    if values.dtype != np.dtype(np.float64):
        raise TypeError("number series values must use float64")
    if values.ndim != 1:
        raise ValueError("series arrays must be one-dimensional")
    if not bool(np.isfinite(values).all()):
        raise ValueError("number series values must be finite")
    canonical = values.copy()
    canonical[canonical == 0.0] = 0.0
    canonical[~valid] = 0.0
    return np.frombuffer(canonical.tobytes(), dtype=np.float64)


def _immutable_bool_array(values: BoolArray) -> BoolArray:
    if values.dtype != np.dtype(np.bool_):
        raise TypeError("boolean series values must use bool")
    if values.ndim != 1:
        raise ValueError("series arrays must be one-dimensional")
    return np.frombuffer(values.tobytes(), dtype=np.bool_)


def _immutable_validity(valid: BoolArray) -> BoolArray:
    if valid.dtype != np.dtype(np.bool_):
        raise TypeError("series validity mask must use bool")
    if valid.ndim != 1:
        raise ValueError("series arrays must be one-dimensional")
    return np.frombuffer(valid.tobytes(), dtype=np.bool_)


@dataclass(frozen=True, slots=True)
class NumberScalar:
    value: float

    def __post_init__(self) -> None:
        if type(self.value) is not float:
            raise TypeError("number scalar must be a float")
        if not math.isfinite(self.value):
            raise ValueError("number scalar must be finite")
        if self.value == 0.0:
            object.__setattr__(self, "value", 0.0)

    def broadcast(self, row_count: int) -> NumberSeries:
        _check_row_count(row_count)
        return NumberSeries(
            np.full(row_count, self.value, dtype=np.float64),
            np.ones(row_count, dtype=np.bool_),
        )


@dataclass(frozen=True, slots=True)
class IntegerScalar:
    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise TypeError("integer scalar must be an integer")
        if abs(self.value) > 2**53:
            raise ValueError("integer scalar must be exactly representable by float64")

    def broadcast(self, row_count: int) -> NumberSeries:
        _check_row_count(row_count)
        return NumberSeries(
            np.full(row_count, self.value, dtype=np.float64),
            np.ones(row_count, dtype=np.bool_),
        )


@dataclass(frozen=True, slots=True, init=False)
class NumberSeries:
    _values: FloatArray = field(repr=False)
    _valid: BoolArray = field(repr=False)

    def __init__(self, values: FloatArray, valid: BoolArray) -> None:
        if values.ndim != 1 or valid.ndim != 1:
            raise ValueError("series arrays must be one-dimensional")
        _check_row_count(len(values))
        if len(values) != len(valid):
            raise ValueError(
                "series values and validity mask must have the same length"
            )
        immutable_valid = _immutable_validity(valid)
        object.__setattr__(self, "_valid", immutable_valid)
        object.__setattr__(
            self,
            "_values",
            _immutable_float_array(values, immutable_valid),
        )

    @property
    def values(self) -> FloatArray:
        return self._values

    @property
    def valid(self) -> BoolArray:
        return self._valid

    @classmethod
    def from_optional(cls, values: Sequence[float | None]) -> NumberSeries:
        _check_row_count(len(values))
        if any(
            value is not None and (type(value) is not float or not math.isfinite(value))
            for value in values
        ):
            raise TypeError(
                "number series optional values must be finite floats or null"
            )
        valid = np.fromiter(
            (value is not None for value in values),
            dtype=np.bool_,
            count=len(values),
        )
        data = np.fromiter(
            (0.0 if value is None else value for value in values),
            dtype=np.float64,
            count=len(values),
        )
        return cls(data, valid)

    def to_optional_tuple(self) -> tuple[float | None, ...]:
        return tuple(
            float(value) if is_valid else None
            for value, is_valid in zip(self._values, self._valid, strict=True)
        )

    def __len__(self) -> int:
        return len(self._values)


@dataclass(frozen=True, slots=True, init=False)
class BooleanSeries:
    _values: BoolArray = field(repr=False)
    _valid: BoolArray = field(repr=False)

    def __init__(self, values: BoolArray, valid: BoolArray) -> None:
        if values.ndim != 1 or valid.ndim != 1:
            raise ValueError("series arrays must be one-dimensional")
        _check_row_count(len(values))
        if len(values) != len(valid):
            raise ValueError(
                "series values and validity mask must have the same length"
            )
        immutable_valid = _immutable_validity(valid)
        canonical = values.copy() if values.dtype == np.dtype(np.bool_) else values
        if canonical.dtype == np.dtype(np.bool_) and canonical.ndim == 1:
            canonical[~valid] = False
        object.__setattr__(self, "_valid", immutable_valid)
        object.__setattr__(self, "_values", _immutable_bool_array(canonical))

    @property
    def values(self) -> BoolArray:
        return self._values

    @property
    def valid(self) -> BoolArray:
        return self._valid

    @classmethod
    def from_optional(cls, values: Sequence[bool | None]) -> BooleanSeries:
        _check_row_count(len(values))
        if any(value is not None and type(value) is not bool for value in values):
            raise TypeError("boolean series optional values must be booleans or null")
        valid = np.fromiter(
            (value is not None for value in values),
            dtype=np.bool_,
            count=len(values),
        )
        data = np.fromiter(
            (False if value is None else value for value in values),
            dtype=np.bool_,
            count=len(values),
        )
        return cls(data, valid)

    def to_optional_tuple(self) -> tuple[bool | None, ...]:
        return tuple(
            bool(value) if is_valid else None
            for value, is_valid in zip(self._values, self._valid, strict=True)
        )

    def __len__(self) -> int:
        return len(self._values)


type ScalarValue = NumberScalar | IntegerScalar
type SeriesValue = NumberSeries | BooleanSeries
