from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


FORBIDDEN_PREFIXES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    "docs/superpowers/",
    "openspec/",
    "outputs/",
    "work/",
)


def forbidden_paths(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if path.startswith(FORBIDDEN_PREFIXES))


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    output = subprocess.check_output(
        ["git", "-C", os.fspath(repo_root), "ls-files", "-z"]
    )
    tracked = (os.fsdecode(path) for path in output.split(b"\0") if path)
    blocked = forbidden_paths(tracked)
    if blocked:
        print("Internal paths are tracked:\n" + "\n".join(blocked), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
