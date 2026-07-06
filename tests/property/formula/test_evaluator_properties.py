from __future__ import annotations

from hypothesis import given, strategies as st

from stock_desk.formula.runtime.dispatch import execute_kernel
from stock_desk.formula.runtime.elementwise import binary_number
from stock_desk.formula.values import BooleanSeries, IntegerScalar, NumberSeries


@given(
    st.lists(
        st.one_of(
            st.none(), st.floats(allow_nan=False, allow_infinity=False, width=64)
        ),
        min_size=1,
        max_size=100,
    ),
    st.integers(min_value=0, max_value=20),
)
def test_ref_preserves_length_and_matches_offset(
    values: list[float | None], offset: int
) -> None:
    source = NumberSeries.from_optional(
        tuple(None if item is None else float(item) for item in values)
    )
    result = execute_kernel(
        "series.ref", (source, IntegerScalar(offset)), len(values)
    ).value
    assert len(result) == len(values)
    expected = (None,) * min(offset, len(values)) + source.to_optional_tuple()[
        : max(0, len(values) - offset)
    ]
    assert result.to_optional_tuple() == expected


@given(
    st.lists(st.booleans(), min_size=1, max_size=100),
    st.integers(min_value=1, max_value=10),
)
def test_filter_true_hits_are_spaced_by_more_than_window(
    values: list[bool], window: int
) -> None:
    source = BooleanSeries.from_optional(tuple(values))
    result = execute_kernel(
        "signal.filter", (source, IntegerScalar(window)), len(values)
    ).value
    indexes = [index for index, value in enumerate(result.to_optional_tuple()) if value]
    assert all(right - left > window for left, right in zip(indexes, indexes[1:]))


@given(st.lists(st.booleans(), min_size=1, max_size=100))
def test_barslast_is_null_until_first_hit_then_counts_distance(
    values: list[bool],
) -> None:
    source = BooleanSeries.from_optional(tuple(values))
    result = execute_kernel(
        "signal.barslast", (source,), len(values)
    ).value.to_optional_tuple()
    last = None
    for index, hit in enumerate(values):
        if hit:
            last = index
        assert result[index] == (None if last is None else float(index - last))


@given(
    st.lists(
        st.floats(allow_nan=False, allow_infinity=False, width=64),
        min_size=1,
        max_size=100,
    )
)
def test_numeric_runtime_never_returns_nonfinite(values: list[float]) -> None:
    source = NumberSeries.from_optional(tuple(float(value) for value in values))
    result = binary_number("*", source, source).value.to_optional_tuple()
    assert len(result) == len(values)
    assert all(
        value is None or value == value and abs(value) != float("inf")
        for value in result
    )


@given(
    st.lists(
        st.one_of(
            st.none(), st.floats(allow_nan=False, allow_infinity=False, width=64)
        ),
        min_size=2,
        max_size=40,
    ),
    st.integers(min_value=1, max_value=10),
)
def test_rolling_prefix_is_stable_when_rows_append(
    values: list[float | None], window: int
) -> None:
    optional = tuple(None if value is None else float(value) for value in values)
    for key in ("series.ma", "series.sum", "statistics.std"):
        n = max(2, window) if key == "statistics.std" else window
        before = execute_kernel(
            key,
            (NumberSeries.from_optional(optional[:-1]), IntegerScalar(n)),
            len(values) - 1,
        ).value
        after = execute_kernel(
            key,
            (NumberSeries.from_optional(optional), IntegerScalar(n)),
            len(values),
        ).value
        assert after.to_optional_tuple()[:-1] == before.to_optional_tuple()


@given(
    st.lists(
        st.one_of(
            st.none(), st.floats(allow_nan=False, allow_infinity=False, width=64)
        ),
        min_size=2,
        max_size=40,
    )
)
def test_recursive_and_cross_prefix_is_stable_when_rows_append(
    values: list[float | None],
) -> None:
    optional = tuple(None if value is None else float(value) for value in values)
    short = NumberSeries.from_optional(optional[:-1])
    full = NumberSeries.from_optional(optional)
    for key, tail in (
        ("series.ema", (IntegerScalar(3),)),
        ("series.sma", (IntegerScalar(3), IntegerScalar(2))),
        ("signal.cross", (IntegerScalar(0),)),
    ):
        before = execute_kernel(key, (short, *tail), len(short)).value
        after = execute_kernel(key, (full, *tail), len(full)).value
        assert after.to_optional_tuple()[:-1] == before.to_optional_tuple()
