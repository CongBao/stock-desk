from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


FULL_PROFILE = "full"
DOCS_PROFILE = "docs-only"
RELEASE_DESKTOP_PROFILE = "release-infra-desktop"

_DOC_FILES = frozenset(
    {
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "README.en.md",
        "README.md",
        "ROADMAP.md",
        "SECURITY.md",
        "SUPPORT.md",
    }
)
_DOC_PREFIXES = ("docs/",)
_RELEASE_DESKTOP_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "packaging/NOTICE.txt",
        "packaging/macos/entitlements.plist",
        "packaging/stock-desk.spec",
        "packaging/windows/stock-desk.iss",
        "scripts/build_installer.py",
        "scripts/ci_impact.py",
        "scripts/check_requirement_coverage.py",
        "scripts/main_validation_proof.py",
        "scripts/verify_installed_app.py",
        "src/stock_desk/desktop.py",
        "src/stock_desk/storage/backup.py",
        "tests/acceptance/test_installed_distribution.py",
        "tests/acceptance/test_release_acceptance_scope.py",
        "tests/acceptance/test_release_artifacts.py",
        "tests/acceptance/test_release_docs.py",
        "tests/integration/test_windows_runtime_acl.py",
        "tests/integration/storage/test_restore_recovery.py",
        "tests/unit/test_ci_impact.py",
        "tests/unit/test_desktop_launcher.py",
        "tests/unit/test_installer_scripts.py",
        "tests/unit/test_main_validation_proof.py",
        "tests/unit/test_repository_health.py",
        "tests/unit/test_requirement_coverage.py",
        "tests/unit/storage/test_backup.py",
    }
)


@dataclass(frozen=True)
class Impact:
    profile: str
    full: bool
    reason: str
    changed_files: tuple[str, ...]


def _normalise_path(raw_path: str) -> str | None:
    path = raw_path.strip()
    if not path or "\\" in path:
        return None
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        return None
    normalised = pure_path.as_posix()
    if normalised == "." or normalised.startswith("./"):
        return None
    return normalised


def _is_docs_path(path: str) -> bool:
    return path in _DOC_FILES or path.startswith(_DOC_PREFIXES)


def classify_impact(event_name: str, changed_files: Sequence[str]) -> Impact:
    normalised_paths: list[str] = []
    for raw_path in changed_files:
        path = _normalise_path(raw_path)
        if path is None:
            return Impact(FULL_PROFILE, True, "invalid-or-empty-path", tuple())
        normalised_paths.append(path)

    paths = tuple(sorted(set(normalised_paths)))
    if event_name == "push":
        return Impact(FULL_PROFILE, True, "push-events-require-full", paths)
    if event_name != "pull_request":
        return Impact(FULL_PROFILE, True, "unsupported-event", paths)
    if not paths:
        return Impact(FULL_PROFILE, True, "empty-change-set", paths)

    unknown = tuple(
        path
        for path in paths
        if not _is_docs_path(path) and path not in _RELEASE_DESKTOP_FILES
    )
    if unknown:
        return Impact(FULL_PROFILE, True, f"unclassified-path:{unknown[0]}", paths)
    if all(_is_docs_path(path) for path in paths):
        return Impact(DOCS_PROFILE, False, "explicit-docs-only", paths)
    return Impact(
        RELEASE_DESKTOP_PROFILE,
        False,
        "explicit-release-infra-desktop-only",
        paths,
    )


def changed_files_between(repo_root: Path, base: str, head: str) -> tuple[str, ...]:
    output = subprocess.check_output(
        [
            "git",
            "-C",
            os.fspath(repo_root),
            "diff",
            "--name-only",
            "-z",
            f"{base}...{head}",
        ]
    )
    return tuple(os.fsdecode(path) for path in output.split(b"\0") if path)


def _write_github_output(output_path: Path, impact: Impact) -> None:
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"profile={impact.profile}\n")
        output.write(f"full={str(impact.full).lower()}\n")
        output.write(f"reason={impact.reason}\n")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify CI impact conservatively.")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    changed_files = tuple(args.changed_file)
    if not changed_files:
        if bool(args.base) != bool(args.head):
            impact = Impact(FULL_PROFILE, True, "incomplete-diff-range", tuple())
        elif args.base and args.head:
            try:
                repo_root = Path(__file__).resolve().parent.parent
                changed_files = changed_files_between(repo_root, args.base, args.head)
            except (OSError, subprocess.CalledProcessError):
                impact = Impact(FULL_PROFILE, True, "diff-failed", tuple())
            else:
                impact = classify_impact(args.event_name, changed_files)
        else:
            impact = Impact(FULL_PROFILE, True, "missing-change-source", tuple())
    else:
        impact = classify_impact(args.event_name, changed_files)

    print(f"profile={impact.profile}")
    print(f"full={str(impact.full).lower()}")
    print(f"reason={impact.reason}")
    if args.github_output is not None:
        _write_github_output(args.github_output, impact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
