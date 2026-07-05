from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable


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
    output = subprocess.check_output(["git", "ls-files"], text=True)
    blocked = forbidden_paths(output.splitlines())
    if blocked:
        print("Internal paths are tracked:\n" + "\n".join(blocked), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
