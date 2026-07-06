from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, strategies as st
import pytest

from stock_desk.formula.compiler import formula_source_checksum
from stock_desk.formula.context import EvaluationContext
from stock_desk.formula.evaluator import FormulaEvaluator
from stock_desk.formula.functions import V1_REGISTRY
from stock_desk.formula.signal_series import FormulaReference
from stock_desk.formula.values import NumberSeries
from stock_desk.market.types import Adjustment, Period, ProviderId


UTC = timezone.utc
DIGEST = "sha256:" + "d" * 64
SAFE_FORMULA = (
    "FAST:=EMA(C,3);SLOW:=EMA(C,5);"
    "BUY:FILTER(CROSS(FAST,SLOW),2);SELL:FILTER(CROSS(SLOW,FAST),2);"
)
SAFE_INDICATOR = (
    "R:REF(C,1);M:MA(C,3);E:EMA(C,3);S:SMA(C,3,2);"
    "HV:HHV(C,3);LV:LLV(C,3);T:SUM(C,2);N:COUNT(C>0,2);"
    "D:STD(C,2);LC:=LONGCROSS(C,R,2);B:BARSLAST(LC);A:ABS(T);"
    "CR:=CROSS(E,S);F:=FILTER(CR,2);MX:MAX(A,HV);MN:MIN(LV,B);"
    "X:IF(F,MX,MN);"
)


def _context(prices: tuple[float, ...]) -> EvaluationContext:
    start = datetime(2023, 12, 31, 16, tzinfo=UTC)
    timestamps = tuple(start + timedelta(days=index) for index in range(len(prices)))
    source = {
        "OPEN": prices,
        "HIGH": tuple(value + 1.0 for value in prices),
        "LOW": tuple(value - 1.0 for value in prices),
        "CLOSE": prices,
        "VOLUME": tuple(10_000.0 for _ in prices),
    }
    fields = {
        spec.name: NumberSeries.from_optional(
            tuple(
                float(value * spec.scale_numerator / spec.scale_denominator)
                for value in source[spec.source_name]
            )
        )
        for spec in V1_REGISTRY.fields()
    }
    return EvaluationContext(
        symbol="600000.SH",
        period=Period.DAY,
        adjustment=Adjustment.QFQ,
        source=ProviderId.TUSHARE,
        dataset_version=DIGEST,
        route_version=DIGEST,
        manifest_record_id=DIGEST,
        data_cutoff=timestamps[-1],
        query_start=timestamps[0] - timedelta(days=1),
        query_end=timestamps[-1] + timedelta(days=2),
        timestamps=timestamps,
        fields=fields,
        parameters={},
    )


@pytest.mark.parametrize("source", [SAFE_FORMULA, SAFE_INDICATOR])
@given(
    data=st.data(),
    prices=st.lists(
        st.one_of(
            st.sampled_from([1e308, -1e308, 0.0]),
            st.floats(
                min_value=-1e100,
                max_value=1e100,
                allow_nan=False,
                allow_infinity=False,
            ),
        ),
        min_size=2,
        max_size=40,
    ),
)
def test_appending_rows_never_changes_historical_values_or_signals(
    source: str, data: st.DataObject, prices: list[float]
) -> None:
    values = tuple(float(value) for value in prices)
    split = data.draw(st.integers(min_value=1, max_value=len(values) - 1))
    formula = FormulaReference(
        formula_id="safe",
        formula_version_id="safe-v1",
        version=1,
        checksum=formula_source_checksum(source),
    )
    evaluator = FormulaEvaluator()

    before = evaluator.evaluate(source, _context(values[:split]), formula)
    after = evaluator.evaluate(source, _context(values), formula)

    for old, new in zip(before.signals, after.signals, strict=True):
        assert new.values[:split] == old.values
    for old, new in zip(before.numeric_outputs, after.numeric_outputs, strict=True):
        assert new.values[:split] == old.values
