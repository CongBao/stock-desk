from __future__ import annotations

import ast
from pathlib import Path
from types import FunctionType
from typing import Any, cast


ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "packaging" / "stock-desk-sidecar.spec"
LEGACY_BROWSER_MODULES = ("stock_desk.desktop", "stock_desk.web")


def _spec_tree() -> ast.Module:
    return ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))


def _literal_assignment(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing literal assignment: {name}")


def _module_filter(tree: ast.Module) -> FunctionType:
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "include_sidecar_module"
        ),
        None,
    )
    assert function is not None
    namespace: dict[str, object] = {
        "SIDECAR_EXCLUDES": _literal_assignment(tree, "SIDECAR_EXCLUDES"),
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(SPEC), "exec"),
        namespace,
    )
    return cast(FunctionType, namespace["include_sidecar_module"])


def test_sidecar_collects_stock_desk_through_an_injectable_module_filter() -> None:
    tree = _spec_tree()
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    collect_call = next(
        call
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "collect_submodules"
    )

    assert ast.literal_eval(collect_call.args[0]) == "stock_desk"
    assert any(
        keyword.arg == "filter"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "include_sidecar_module"
        for keyword in collect_call.keywords
    )


def test_sidecar_module_filter_keeps_runtime_and_provider_modules() -> None:
    include = _module_filter(_spec_tree())

    assert include("stock_desk.desktop_session")
    assert include("stock_desk.desktop_runtime")
    assert include("stock_desk.market.providers.akshare")
    assert include("stock_desk.analysis.providers.deepseek")


def test_sidecar_module_filter_rejects_legacy_browser_modules_and_descendants() -> None:
    include = _module_filter(_spec_tree())

    for module in LEGACY_BROWSER_MODULES:
        assert not include(module)
        assert not include(f"{module}.child")
        assert not include(f"{module}.child.grandchild")


def test_sidecar_analysis_explicitly_excludes_legacy_browser_modules() -> None:
    tree = _spec_tree()

    assert set(_literal_assignment(tree, "SIDECAR_EXCLUDES")) == set(
        LEGACY_BROWSER_MODULES
    )
    analysis_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    )
    excludes = next(
        keyword.value for keyword in analysis_call.keywords if keyword.arg == "excludes"
    )
    assert isinstance(excludes, ast.Name)
    assert excludes.id == "SIDECAR_EXCLUDES"


def test_sidecar_datas_contain_no_browser_test_or_development_assets() -> None:
    source = SPEC.read_text(encoding="utf-8").replace("\\", "/").casefold()

    for forbidden in ("web/dist", "browser", "/src/tests", "/tests", "/dev"):
        assert forbidden not in source


def test_sidecar_packages_only_sorted_migration_sources_not_ignored_caches() -> None:
    source = SPEC.read_text(encoding="utf-8").replace("\\", "/")

    assert 'rglob("*.py")' in source
    assert 'str(ROOT / "migrations"), "stock_desk/migrations"' not in source
    assert "sorted(" in source


def test_frozen_sidecar_runtime_path_never_imports_excluded_modules() -> None:
    sidecar = (ROOT / "src" / "stock_desk" / "sidecar.py").read_text(encoding="utf-8")
    main = (ROOT / "src" / "stock_desk" / "main.py").read_text(encoding="utf-8")
    lake = (ROOT / "src" / "stock_desk" / "market" / "lake.py").read_text(
        encoding="utf-8"
    )

    assert "from stock_desk.desktop_runtime import RuntimePaths" in sidecar
    assert "from stock_desk.desktop import RuntimePaths" not in sidecar
    assert "from stock_desk.desktop_runtime import _restrict_owner_access" in lake
    assert "from stock_desk.desktop import _restrict_owner_access" not in lake
    assert main.index("if resolved_settings.web_dist_dir is not None:") < main.index(
        "from stock_desk.web import install_web_routes"
    )
    assert 'app = None if "STOCK_DESK_DESKTOP_PORT" in os.environ' in main


def test_frozen_multiprocessing_dispatch_precedes_the_host_bootstrap_gate() -> None:
    source = (ROOT / "src" / "stock_desk" / "sidecar.py").read_text(encoding="utf-8")

    main_start = source.index("def main() -> int:")
    freeze_support = source.index("multiprocessing.freeze_support()", main_start)
    bootstrap_gate = source.index("await_bootstrap_gate(sys.stdin.buffer)", main_start)
    assert freeze_support < bootstrap_gate
