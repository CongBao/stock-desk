from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
import hashlib
import os
from pathlib import Path
import sys


ROOT_FILES = (
    ".dockerignore",
    "Dockerfile",
    "alembic.ini",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "pyproject.toml",
    "scripts/source_fingerprint.py",
    "uv.lock",
)
TREE_ROOTS = ("migrations", "src", "web")
_IGNORED_COMPONENTS = frozenset(
    {
        ".agents",
        ".cache",
        ".codex",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".superpowers",
        ".venv",
        "__pycache__",
        "coverage",
        "data",
        "dist",
        "logs",
        "node_modules",
        "openspec",
        "outputs",
        "reports",
        "venv",
        "work",
    }
)
_FINGERPRINT_DOMAIN = b"stock-desk-public-build-inputs-v1\0"


def _is_ignored(relative_path: Path) -> bool:
    if any(part in _IGNORED_COMPONENTS for part in relative_path.parts):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in relative_path.parts):
        return True
    return relative_path.suffix in {".pyc", ".pyo"} or relative_path.name.endswith(
        ".tsbuildinfo"
    )


def public_build_paths(repo_root: Path) -> tuple[Path, ...]:
    root = repo_root.resolve(strict=True)
    relative_paths: set[Path] = set()
    for relative_name in ROOT_FILES:
        relative_path = Path(relative_name)
        candidate = root / relative_path
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeError(
                f"required public build input is missing: {relative_path}"
            )
        relative_paths.add(relative_path)

    for tree_name in TREE_ROOTS:
        tree_root = root / tree_name
        if tree_root.is_symlink() or not tree_root.is_dir():
            raise RuntimeError(f"required public build tree is missing: {tree_name}")
        for candidate in tree_root.rglob("*"):
            relative_path = candidate.relative_to(root)
            if _is_ignored(relative_path):
                continue
            if candidate.is_symlink():
                raise RuntimeError(
                    f"public build inputs must not contain symlinks: {relative_path}"
                )
            if candidate.is_file():
                relative_paths.add(relative_path)

    return tuple(sorted(relative_paths, key=lambda path: path.as_posix()))


def _hash_files(repo_root: Path, relative_paths: Iterable[Path]) -> str:
    root = repo_root.resolve(strict=True)
    digest = hashlib.sha256()
    digest.update(_FINGERPRINT_DOMAIN)
    normalized_paths = sorted(set(relative_paths), key=lambda path: path.as_posix())
    for relative_path in normalized_paths:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(
                f"fingerprint path must stay inside repository: {relative_path}"
            )
        source = root / relative_path
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(
                f"fingerprint input is not a regular file: {relative_path}"
            )
        path_bytes = relative_path.as_posix().encode("utf-8")
        content = source.read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def compute_source_fingerprint(repo_root: Path) -> str:
    return _hash_files(repo_root, public_build_paths(repo_root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute the deterministic Stock Desk public build fingerprint."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    fingerprint = compute_source_fingerprint(options.root)
    if options.output is None:
        print(fingerprint)
    else:
        options.output.write_text(f"{fingerprint}\n", encoding="ascii")
        os.chmod(options.output, 0o444)
    return 0


if __name__ == "__main__":
    sys.exit(main())
