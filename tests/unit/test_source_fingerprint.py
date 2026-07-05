from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest


def _fingerprint() -> ModuleType:
    try:
        return importlib.import_module("scripts.source_fingerprint")
    except ModuleNotFoundError:
        pytest.fail("scripts.source_fingerprint is missing")


def _minimal_public_tree(root: Path, fingerprint: ModuleType) -> None:
    for relative in fingerprint.ROOT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"public:{relative}\n", encoding="utf-8")
    for tree_root in fingerprint.TREE_ROOTS:
        (root / tree_root).mkdir(parents=True, exist_ok=True)
    (root / "src" / "stock_desk" / "main.py").parent.mkdir(parents=True)
    (root / "src" / "stock_desk" / "main.py").write_text("APP = 1\n")
    (root / "migrations" / "env.py").write_text("MIGRATION = 1\n")
    (root / "web" / "src").mkdir(parents=True)
    (root / "web" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "web" / "src" / "main.tsx").write_text("export {};\n")


def test_hash_is_sorted_and_changes_with_path_or_bytes(tmp_path: Path) -> None:
    fingerprint = _fingerprint()
    (tmp_path / "a.txt").write_text("same", encoding="utf-8")
    (tmp_path / "b.txt").write_text("same", encoding="utf-8")

    first = fingerprint._hash_files(
        tmp_path,
        (Path("b.txt"), Path("a.txt")),
    )
    reordered = fingerprint._hash_files(
        tmp_path,
        (Path("a.txt"), Path("b.txt")),
    )
    (tmp_path / "b.txt").write_text("changed", encoding="utf-8")
    changed_bytes = fingerprint._hash_files(
        tmp_path,
        (Path("a.txt"), Path("b.txt")),
    )
    (tmp_path / "b.txt").rename(tmp_path / "c.txt")
    changed_path = fingerprint._hash_files(
        tmp_path,
        (Path("a.txt"), Path("c.txt")),
    )

    assert first == reordered
    assert changed_bytes != first
    assert changed_path != changed_bytes


def test_public_inputs_include_every_build_surface(tmp_path: Path) -> None:
    fingerprint = _fingerprint()
    _minimal_public_tree(tmp_path, fingerprint)

    relative_paths = {
        path.as_posix() for path in fingerprint.public_build_paths(tmp_path)
    }

    assert {
        ".dockerignore",
        "Dockerfile",
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "scripts/source_fingerprint.py",
        "src/stock_desk/main.py",
        "migrations/env.py",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "web/package.json",
        "web/src/main.tsx",
    } <= relative_paths


def test_fingerprint_ignores_internal_secret_data_and_generated_paths(
    tmp_path: Path,
) -> None:
    fingerprint = _fingerprint()
    _minimal_public_tree(tmp_path, fingerprint)
    baseline = fingerprint.compute_source_fingerprint(tmp_path)
    ignored = (
        ".agents/private.md",
        ".codex/session.json",
        ".env",
        "data/stock-desk.db",
        "outputs/report.md",
        "work/scratch.txt",
        "src/stock_desk/__pycache__/main.pyc",
        "web/.env.local",
        "web/dist/index.html",
        "web/node_modules/package/index.js",
        "web/tsconfig.app.tsbuildinfo",
    )
    for relative in ignored:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not affect fingerprint\n", encoding="utf-8")

    assert fingerprint.compute_source_fingerprint(tmp_path) == baseline


def test_fingerprint_changes_for_relevant_public_input(tmp_path: Path) -> None:
    fingerprint = _fingerprint()
    _minimal_public_tree(tmp_path, fingerprint)
    baseline = fingerprint.compute_source_fingerprint(tmp_path)

    (tmp_path / "src" / "stock_desk" / "main.py").write_text(
        "APP = 2\n",
        encoding="utf-8",
    )

    assert fingerprint.compute_source_fingerprint(tmp_path) != baseline


def test_fingerprint_changes_when_package_readme_changes(tmp_path: Path) -> None:
    fingerprint = _fingerprint()
    _minimal_public_tree(tmp_path, fingerprint)
    readme = tmp_path / "README.md"
    readme.write_text("first package description\n", encoding="utf-8")
    baseline = fingerprint.compute_source_fingerprint(tmp_path)

    readme.write_text("changed package description\n", encoding="utf-8")

    assert fingerprint.compute_source_fingerprint(tmp_path) != baseline
