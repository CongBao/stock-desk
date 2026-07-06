from __future__ import annotations

from stock_desk.formula.functions.base import FunctionSpec, ParameterSpec, ValueKind


_VALUE: tuple[ValueKind, ...] = ("scalar", "number_series")

STATISTICS_FUNCTIONS = (
    FunctionSpec(
        "ABS",
        "math",
        "绝对值。",
        "current_only",
        (ParameterSpec("X", _VALUE),),
        "number_series",
        "math.abs",
        "逐项返回 X 的绝对值；null 传播。",
    ),
    FunctionSpec(
        "MAX",
        "math",
        "两值中的较大值。",
        "current_only",
        (ParameterSpec("X", _VALUE), ParameterSpec("Y", _VALUE)),
        "number_series",
        "math.max",
        "逐项返回 X、Y 较大值；任一输入为 null 时结果为 null。",
    ),
    FunctionSpec(
        "MIN",
        "math",
        "两值中的较小值。",
        "current_only",
        (ParameterSpec("X", _VALUE), ParameterSpec("Y", _VALUE)),
        "number_series",
        "math.min",
        "逐项返回 X、Y 较小值；任一输入为 null 时结果为 null。",
    ),
    FunctionSpec(
        "STD",
        "statistics",
        "估算标准差。",
        "past_only",
        (
            ParameterSpec("X", _VALUE),
            ParameterSpec(
                "N", ("integer_scalar",), minimum=2, constraints_zh="整数且 N>=2。"
            ),
        ),
        "number_series",
        "statistics.std",
        "仅当最近 N 个 bar 位置全部有效时返回样本标准差（分母 N-1），否则为 null；预热期为 null。样本及预热规则为 stock-desk tdx-v1 固化语义。",
    ),
    FunctionSpec(
        "IF",
        "logic",
        "条件选择。",
        "current_only",
        (
            ParameterSpec("CONDITION", ("scalar", "boolean_series", "number_series")),
            ParameterSpec("A", _VALUE),
            ParameterSpec("B", _VALUE),
        ),
        "number_series",
        "logic.if",
        "条件非零/true 时逐项返回 A，否则返回 B；条件为 null 时结果为 null。",
    ),
)
