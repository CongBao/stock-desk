from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from stock_desk.formula.functions.registry import (
    COMPATIBILITY_VERSION,
    V1_REGISTRY,
)
from stock_desk.formula.functions.base import MAX_IDENTIFIER_CHARS, VALUE_KIND_HIERARCHY
from stock_desk.formula.parser import (
    MAX_ABSOLUTE_EXPONENT,
    MAX_AST_NODES,
    MAX_NESTING_DEPTH,
    MAX_NUMERIC_LITERAL_CHARS,
    MAX_SOURCE_BYTES,
    MAX_STATEMENTS,
)


__all__ = [
    "COMPATIBILITY_VERSION",
    "compatibility_data",
    "compatibility_json",
    "main",
    "render_compatibility_markdown",
]


def compatibility_data() -> dict[str, object]:
    """Return the canonical API/docs/editor compatibility payload."""

    return {
        "compatibility_version": COMPATIBILITY_VERSION,
        "official_reference": "https://help.tdx.com.cn/gspt/docs/markdown/redword/functionlist.html",
        "fields": [field.to_data() for field in V1_REGISTRY.fields()],
        "functions": [function.to_data() for function in V1_REGISTRY.functions()],
        "parser_limits": {
            "absolute_exponent": MAX_ABSOLUTE_EXPONENT,
            "ast_nodes": MAX_AST_NODES,
            "identifier_chars": MAX_IDENTIFIER_CHARS,
            "nesting_depth": MAX_NESTING_DEPTH,
            "numeric_literal_chars": MAX_NUMERIC_LITERAL_CHARS,
            "source_bytes": MAX_SOURCE_BYTES,
            "statements": MAX_STATEMENTS,
        },
        "runtime_semantics": {
            "division_by_zero": "除零、溢出及非有限结果统一转换为 null。",
            "json_numbers": "持久化 JSON 只允许有限数字和 null，禁止 NaN/Infinity。",
            "null_propagation": "除函数条目明确说明外，任一必要输入为 null 时结果为 null。",
            "numeric_storage": "float64",
            "provenance": "初始化、预热、null 与浮点规范为 stock-desk tdx-v1 固化语义。",
        },
        "value_kind_hierarchy": {
            kind: list(parents) for kind, parents in VALUE_KIND_HIERARCHY.items()
        },
    }


def compatibility_json() -> str:
    return (
        json.dumps(
            compatibility_data(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_compatibility_markdown() -> str:
    data = compatibility_data()
    fields = V1_REGISTRY.fields()
    functions = V1_REGISTRY.functions()
    limits = data["parser_limits"]
    assert isinstance(limits, dict)
    lines = [
        "# 通达信公式兼容性",
        "",
        "<!-- 此文件由 stock_desk.formula.compatibility 生成，请勿手工修改表格。 -->",
        "",
        f"当前兼容版本：`{COMPATIBILITY_VERSION}`。此清单由 API、Monaco 编辑器和公开文档共同使用。",
        "官方名称与基础公式参考：[通达信公式系统函数列表](https://help.tdx.com.cn/gspt/docs/markdown/redword/functionlist.html)。未由官方明确规定的细节会标注为 stock-desk 固化语义。",
        "",
        "## 支持的行情字段",
        "",
        "| 名称 | 来源/缩放 | 单位 | 类型 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| `{field.name}` | `{field.source_name}` × {field.scale_numerator}/{field.scale_denominator} | `{field.unit}` | `{field.value_type}` | {field.summary_zh} |"
        for field in fields
    )
    lines.extend(
        [
            "",
            "## 支持的函数",
            "",
            "| 函数 | 参数约束 | 结果/派发 | 时间行为 | 精确语义 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for function in functions:
        parameter_text = "; ".join(
            f"{parameter.name}: {'/'.join(parameter.accepted_kinds)}"
            + ("，常量" if parameter.constant else "")
            + (f"，{parameter.constraints_zh}" if parameter.constraints_zh else "")
            for parameter in function.parameters
        )
        lines.append(
            f"| `{function.signature}` | {parameter_text} | `{function.result_kind}` / "
            f"`{function.dispatch_key}` | `{function.future_behavior}` | {function.semantics_zh} |"
        )
        if function.relations:
            lines.append(
                "| 关系约束 | "
                + "; ".join(
                    f"`{relation.left} {relation.operator} {relation.right}`"
                    for relation in function.relations
                )
                + " | 结构化约束 | - | Task 3 必须通用执行此关系。 |"
            )
    lines.extend(
        [
            "",
            "## 语法和限制",
            "",
            "- `:=` 声明隐藏中间量，`:` 声明公开输出；标识符不区分大小写。",
            "- 仅支持受控表达式、静态函数调用和 `//` 注释；不执行 Python 代码，也不提供文件或网络访问。",
            "- 未列出的函数会返回 `unsupported_function`；参数数量不符会返回 `invalid_argument_count`。",
            "- 本版本只登记稳定的当前值或历史依赖语义。未来数据和信号漂移分析由保存/回测校验阶段处理。",
            "- 首版不支持条件选股、五彩 K 线或 AI 生成、解释、修复公式。",
            "- `VOLUME` 是 stock-desk 扩展，单位为股；A股 `VOL`/`V` 按 100 股/手从 `VOLUME` 缩放为手。`AMOUNT` 尚未支持。",
            "- 所有数值按 float64 计算；除零、溢出、NaN 和 Infinity 转为 null，JSON 只输出有限数字或 null。",
            "- 除函数条目明确说明外，必要输入为 null 时结果为 null。初始化、预热及浮点规范均为 stock-desk tdx-v1 固化语义。",
            "",
            "## 解析上限",
            "",
            f"- 源码：{limits['source_bytes']} UTF-8 字节",
            f"- 语句：{limits['statements']} 条",
            f"- AST 节点：{limits['ast_nodes']} 个",
            f"- 标识符：{limits['identifier_chars']} 个字符",
            f"- 括号嵌套：{limits['nesting_depth']} 层",
            f"- 数字字面量：{limits['numeric_literal_chars']} 个字符",
            f"- 指数绝对值：{limits['absolute_exponent']}",
            "",
            "## 校验与重新生成",
            "",
            "```bash",
            "uv run --frozen python -m stock_desk.formula.compatibility --check docs/formula-compatibility.md",
            "uv run --frozen python -m stock_desk.formula.compatibility --write docs/formula-compatibility.md",
            "uv run --frozen python -m stock_desk.formula.compatibility --json",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export or verify the stock-desk formula compatibility catalog."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", type=Path, metavar="PATH")
    actions.add_argument("--write", type=Path, metavar="PATH")
    actions.add_argument("--json", action="store_true")
    return parser


def main(arguments: list[str] | None = None) -> int:
    options = _argument_parser().parse_args(arguments)
    if options.check is not None:
        try:
            current = options.check.read_text(encoding="utf-8")
        except OSError:
            print(
                f"Compatibility document is missing: {options.check}", file=sys.stderr
            )
            return 1
        if current != render_compatibility_markdown():
            print(
                f"Compatibility document is out of date: {options.check}",
                file=sys.stderr,
            )
            return 1
        return 0
    if options.write is not None:
        options.write.parent.mkdir(parents=True, exist_ok=True)
        options.write.write_text(render_compatibility_markdown(), encoding="utf-8")
        return 0
    print(compatibility_json(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
