from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_formula_grammar_is_available_from_wheel_and_sdist(tmp_path: Path) -> None:
    subprocess.run(
        [
            "uv",
            "build",
            "--no-build-isolation",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    sdist = next(tmp_path.glob("*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        assert "stock_desk/formula/grammar.lark" in archive.namelist()

    with tarfile.open(sdist, "r:gz") as archive:
        assert any(
            name.endswith("/src/stock_desk/formula/grammar.lark")
            for name in archive.getnames()
        )

    installed = tmp_path / "installed"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    probe = (
        "import importlib.resources, sys; "
        f"sys.path.insert(0, {str(installed)!r}); "
        "resource = importlib.resources.files('stock_desk.formula') / 'grammar.lark'; "
        "assert 'statement' in resource.read_text(encoding='utf-8')"
    )
    subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_formula_runtime_and_property_test_dependencies_are_direct_and_locked() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = project["project"]["dependencies"]
    dev = project["dependency-groups"]["dev"]

    assert "lark>=1.2,<2" in runtime
    assert "numpy>=2.3,<3" in runtime
    assert "polars>=1.37,<2" in runtime
    assert "hypothesis>=6.140,<7" in dev

    locked = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(item for item in locked["package"] if item["name"] == "stock-desk")
    direct = {dependency["name"] for dependency in package["dependencies"]}
    assert {"lark", "numpy", "polars"} <= direct
    dev_dependencies = {
        dependency["name"] for dependency in package["dev-dependencies"]["dev"]
    }
    assert "hypothesis" in dev_dependencies
