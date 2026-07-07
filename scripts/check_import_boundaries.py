from __future__ import annotations

import ast
from pathlib import Path
import sys
from typing import Final


ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_ROOT = ROOT / "src/stock_desk/analysis"
ANALYSIS_API_PATH = ROOT / "src/stock_desk/api/analysis.py"
FORBIDDEN_ANALYSIS_DEPENDENCIES: Final = (
    "stock_desk.formula",
    "stock_desk.backtest",
    "stock_desk.broker",
)
FORBIDDEN_WORKFLOW_RUNTIME_DEPENDENCIES: Final = (
    "httpx",
    "httpx2",
    "requests",
    "urllib",
    "socket",
    "akshare",
    "tushare",
    "baostock",
)


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


def _imports(
    tree: ast.AST,
    *,
    package: tuple[str, ...],
) -> tuple[tuple[int, str], ...]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module is not None:
                found.append((node.lineno, node.module))
            elif node.level > 0:
                parent_count = node.level - 1
                base = package[: len(package) - parent_count]
                if node.module is not None:
                    found.append((node.lineno, ".".join((*base, node.module))))
                else:
                    found.extend(
                        (node.lineno, ".".join((*base, alias.name)))
                        for alias in node.names
                    )
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            is_dynamic_import = (
                isinstance(function, ast.Name) and function.id == "__import__"
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr == "import_module"
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
            )
            module = node.args[0]
            if (
                is_dynamic_import
                and isinstance(module, ast.Constant)
                and type(module.value) is str
            ):
                found.append((node.lineno, module.value))
    return tuple(sorted(found))


def find_import_boundary_violations(
    analysis_root: Path = ANALYSIS_ROOT,
) -> tuple[str, ...]:
    violations: list[str] = []
    for path in sorted(analysis_root.rglob("*.py")):
        relative = path.relative_to(analysis_root).as_posix()
        package = (
            "stock_desk",
            "analysis",
            *path.relative_to(analysis_root).parent.parts,
        )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            violations.append(f"{relative}: unable to inspect Python imports")
            continue
        for line, module in _imports(tree, package=package):
            if _matches(module, FORBIDDEN_ANALYSIS_DEPENDENCIES):
                violations.append(
                    f"{relative}:{line}: forbidden analysis dependency {module}"
                )
            if path.name == "workflow.py" and _matches(
                module, FORBIDDEN_WORKFLOW_RUNTIME_DEPENDENCIES
            ):
                violations.append(
                    f"{relative}:{line}: forbidden workflow runtime dependency {module}"
                )
    return tuple(violations)


def find_analysis_api_boundary_violations(
    analysis_api_path: Path = ANALYSIS_API_PATH,
) -> tuple[str, ...]:
    try:
        tree = ast.parse(
            analysis_api_path.read_text(encoding="utf-8"),
            filename=str(analysis_api_path),
        )
    except (OSError, UnicodeError, SyntaxError):
        return (f"{analysis_api_path.name}: unable to inspect Python imports",)
    return tuple(
        f"{analysis_api_path.name}:{line}: forbidden analysis API dependency {module}"
        for line, module in _imports(tree, package=("stock_desk", "api"))
        if _matches(module, FORBIDDEN_ANALYSIS_DEPENDENCIES)
    )


def main() -> int:
    violations = (
        *find_import_boundary_violations(),
        *find_analysis_api_boundary_violations(),
    )
    if violations:
        sys.stderr.write("\n".join(violations) + "\n")
        return 1
    print("analysis import boundaries: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
