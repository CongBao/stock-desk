from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from scripts import check_public_tree
from scripts.check_public_tree import forbidden_paths


FORBIDDEN_TRACKED_PATHS = [
    ".agents/notes.txt",
    ".codex/session.txt",
    ".superpowers/plan.txt",
    "docs/superpowers/design.md",
    "openspec/内部.yaml",
    "outputs/review.md",
    "work/scratch.txt",
]


def create_git_repo(tmp_path: Path, tracked_paths: list[str]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)

    script = repo / "scripts" / "check_public_tree.py"
    script.parent.mkdir()
    shutil.copyfile(Path(check_public_tree.__file__), script)

    for path in tracked_paths:
        tracked = repo / path
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("fixture\n", encoding="utf-8")

    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "--", *tracked_paths],
        check=True,
    )
    return repo, script


def run_checker(script: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_internal_paths_are_rejected() -> None:
    tracked = ["src/stock_desk/main.py", "openspec/config.yaml", "outputs/review.md"]
    assert forbidden_paths(tracked) == ["openspec/config.yaml", "outputs/review.md"]


def test_public_paths_are_allowed() -> None:
    tracked = ["README.md", "src/stock_desk/main.py", "docs/architecture.md"]
    assert forbidden_paths(tracked) == []


def test_cli_rejects_every_forbidden_prefix_with_exact_output(tmp_path: Path) -> None:
    repo, script = create_git_repo(tmp_path, FORBIDDEN_TRACKED_PATHS)

    result = run_checker(script, repo)

    expected = (
        "Internal paths are tracked:\n"
        + "\n".join(sorted(FORBIDDEN_TRACKED_PATHS))
        + "\n"
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == expected


def test_cli_scans_repo_root_when_invoked_from_subdirectory(tmp_path: Path) -> None:
    forbidden = "openspec/config.yaml"
    repo, script = create_git_repo(tmp_path, [forbidden])
    nested = repo / "src" / "stock_desk"
    nested.mkdir(parents=True)

    result = run_checker(script, nested)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"Internal paths are tracked:\n{forbidden}\n"
