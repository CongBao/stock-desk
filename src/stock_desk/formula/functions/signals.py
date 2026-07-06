from __future__ import annotations

from stock_desk.formula.functions.base import FunctionSpec, ParameterSpec, ValueKind


_SERIES: tuple[ValueKind, ...] = ("scalar", "number_series")
_CONDITION: tuple[ValueKind, ...] = (
    "scalar",
    "boolean_series",
    "number_series",
)

SIGNAL_FUNCTIONS = (
    FunctionSpec(
        "CROSS",
        "signal",
        "由下向上穿越。",
        "past_only",
        (ParameterSpec("X", _SERIES), ParameterSpec("Y", _SERIES)),
        "boolean_series",
        "signal.cross",
        "仅当 X[t]>Y[t] 且 X[t-1]<=Y[t-1] 时为 true；首周期或任一比较值为 null 时为 false。边界/null 规则为 stock-desk tdx-v1 固化语义。",
    ),
    FunctionSpec(
        "LONGCROSS",
        "signal",
        "持续低于后上穿。",
        "past_only",
        (
            ParameterSpec("X", _SERIES),
            ParameterSpec("Y", _SERIES),
            ParameterSpec(
                "N",
                ("integer_scalar",),
                constant=True,
                minimum=1,
                constraints_zh="常量整数且 N>=1。",
            ),
        ),
        "boolean_series",
        "signal.longcross",
        "当此前连续 N 个完整周期 X<Y，且当前周期 X>Y 时为 true；窗口不足或含 null 时为 false。N 固定为常量是 stock-desk tdx-v1 约束。",
    ),
    FunctionSpec(
        "BARSLAST",
        "signal",
        "距上次条件成立的周期数。",
        "past_only",
        (ParameterSpec("X", _CONDITION),),
        "number_series",
        "signal.barslast",
        "当前周期条件成立返回 0，之后逐周期递增；从未成立则为 null。条件 null 视为未命中，已有状态时距离仍按 bar 递增。未命中语义为 stock-desk tdx-v1 固化语义。",
    ),
    FunctionSpec(
        "FILTER",
        "signal",
        "抑制连续信号。",
        "past_only",
        (
            ParameterSpec("X", _CONDITION),
            ParameterSpec(
                "N",
                ("integer_scalar",),
                constant=True,
                minimum=1,
                constraints_zh="常量整数且 N>=1。",
            ),
        ),
        "boolean_series",
        "signal.filter",
        "当前命中保留为 true，并将后续 N 个周期内再次出现的命中抑制为 false；条件 null 视为未命中且抑制期仍按 bar 推进；N 为常量正整数。",
    ),
)
