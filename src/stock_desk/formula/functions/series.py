from __future__ import annotations

from stock_desk.formula.functions.base import (
    FunctionSpec,
    ParameterSpec,
    RelationSpec,
    ValueKind,
)


_SERIES: tuple[ValueKind, ...] = ("scalar", "number_series")


def _x() -> ParameterSpec:
    return ParameterSpec("X", _SERIES)


def _window(*, zero_means_all: bool = False, minimum: int = 1) -> ParameterSpec:
    detail = (
        "非负整数；N=0 表示从首个有效值累计。"
        if zero_means_all
        else f"整数且 N>={minimum}。"
    )
    return ParameterSpec(
        "N",
        ("integer_scalar",),
        minimum=0 if zero_means_all else minimum,
        constraints_zh=detail,
    )


SERIES_FUNCTIONS = (
    FunctionSpec(
        "REF",
        "series",
        "引用 N 个周期前的值。",
        "past_only",
        (
            _x(),
            ParameterSpec(
                "N",
                ("integer_scalar",),
                minimum=0,
                constraints_zh="非负整数，可为参数。",
            ),
        ),
        "number_series",
        "series.ref",
        "返回 X[t-N]；N 为非负整数，历史不足返回 null。",
    ),
    FunctionSpec(
        "MA",
        "series",
        "简单移动平均。",
        "past_only",
        (_x(), _window()),
        "number_series",
        "series.ma",
        "仅当最近 N 个 bar 位置全部有效时返回算术平均，否则为 null；预热期为 null。这是 stock-desk tdx-v1 固化语义。",
    ),
    FunctionSpec(
        "EMA",
        "series",
        "指数移动平均。",
        "past_only",
        (_x(), _window()),
        "number_series",
        "series.ema",
        "递推 Y=2*X/(N+1)+(N-1)*Y_PREV/(N+1)；首个有效值以 X 初始化，输入 null 时输出 null 且不更新状态。初始化/null 规则为 stock-desk tdx-v1 固化语义。",
    ),
    FunctionSpec(
        "SMA",
        "series",
        "通达信三参数平滑移动平均。",
        "past_only",
        (
            _x(),
            _window(),
            ParameterSpec(
                "M",
                ("integer_scalar",),
                minimum=1,
                constraints_zh="整数且 1<=M<=N。",
            ),
        ),
        "number_series",
        "series.sma",
        "递推 Y=(M*X+(N-M)*Y_PREV)/N，且 1<=M<=N；首个有效值以 X 初始化，输入 null 时输出 null 且不更新状态。初始化/null 规则为 stock-desk tdx-v1 固化语义。",
        (RelationSpec("M", "<=", "N"),),
    ),
    FunctionSpec(
        "HHV",
        "series",
        "窗口最高值。",
        "past_only",
        (_x(), _window(zero_means_all=True)),
        "number_series",
        "series.hhv",
        "返回最近 N 个 bar 内忽略 null 后的最大值，至少一个有效值才输出；N=0 从首个有效值累计。",
    ),
    FunctionSpec(
        "LLV",
        "series",
        "窗口最低值。",
        "past_only",
        (_x(), _window(zero_means_all=True)),
        "number_series",
        "series.llv",
        "返回最近 N 个 bar 内忽略 null 后的最小值，至少一个有效值才输出；N=0 从首个有效值累计。",
    ),
    FunctionSpec(
        "SUM",
        "series",
        "窗口求和。",
        "past_only",
        (_x(), _window(zero_means_all=True)),
        "number_series",
        "series.sum",
        "返回最近 N 个 bar 内忽略 null 后的和，至少一个有效值才输出；N=0 从首个有效值累计。",
    ),
    FunctionSpec(
        "COUNT",
        "series",
        "统计条件成立次数。",
        "past_only",
        (
            ParameterSpec("X", ("scalar", "boolean_series", "number_series")),
            _window(zero_means_all=True),
        ),
        "number_series",
        "series.count",
        "统计最近 N 个 bar 内忽略 null 后非零/true 的次数，至少一个有效值才输出；N=0 从首个有效值累计。",
    ),
)
