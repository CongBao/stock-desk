from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


REQUIRED_PUBLIC_DOCUMENTS = (
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "docs/architecture.md",
    "docs/configuration.md",
    "docs/troubleshooting.md",
    "docs/disclaimer.md",
)

REQUIRED_SECTIONS = {
    "README.md": (
        "Quick start",
        "Core workflows",
        "Documentation",
        "Safety and scope",
        "Contributing",
    ),
    "README.zh-CN.md": (
        "快速启动",
        "核心工作流",
        "文档",
        "安全与范围",
        "参与贡献",
    ),
    "CONTRIBUTING.md": ("Development setup", "Quality gates", "Pull requests"),
    "SUPPORT.md": ("Questions", "Bug reports", "Security"),
    "CHANGELOG.md": ("Unreleased",),
    "ROADMAP.md": ("Released", "Planned"),
    "docs/architecture.md": (
        "Deployment model",
        "Modules and boundaries",
        "Data and storage",
        "Trust and security",
    ),
    "docs/configuration.md": (
        "Native installers",
        "Source development",
        "Container deployment",
        "Application settings",
        "Container settings",
        "Provider credentials",
    ),
    "docs/troubleshooting.md": (
        "Startup and health",
        "Data and charts",
        "Tasks and workers",
        "Model providers",
        "Backup and restore",
    ),
    "docs/disclaimer.md": (
        "Research use only",
        "Data limitations",
        "Model limitations",
        "User responsibility",
    ),
}

REQUIRED_WIKI_PAGES = (
    "Home",
    "Installation",
    "Task-Center",
    "Market-Data-and-Charts",
    "Formula-Studio",
    "Backtesting",
    "Multi-Agent-Research",
    "Backup-and-Restore",
    "Configuration-and-Security",
    "Troubleshooting",
)

FORBIDDEN_PUBLIC_REFERENCES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    "docs/superpowers/",
    "openspec/",
    "SCREENSHOT_PLACEHOLDER",
    "/Users/",
)

FORBIDDEN_TRACKED_PREFIXES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    "docs/superpowers/",
    "openspec/",
    "outputs/",
    "work/",
)

SOURCE_FREE_INSTALLER_PATTERNS = (
    "stock-desk-<version>-windows-x86_64.exe",
    "stock-desk-<version>-macos-x86_64.dmg",
    "stock-desk-<version>-macos-arm64.dmg",
)

REQUIRED_PUBLIC_SNIPPETS = {
    "README.md": (
        "gh attestation verify",
        "--repo CongBao/stock-desk",
        "--signer-workflow CongBao/stock-desk/.github/workflows/release.yml",
    ),
    "README.zh-CN.md": (
        "gh attestation verify",
        "--repo CongBao/stock-desk",
        "--signer-workflow CongBao/stock-desk/.github/workflows/release.yml",
    ),
    "docs/architecture.md": (
        "Native installer topology",
        "Source development topology",
        "Container topology",
        "parent launcher",
        "127.0.0.1",
        "random",
    ),
    "docs/configuration.md": (
        "Native installers",
        "Source development",
        "Container deployment",
        r"%LOCALAPPDATA%\stock-desk",
        "~/Library/Application Support/stock-desk",
        "config/master.key",
    ),
}

WIKI_FORBIDDEN_REFERENCES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    "docs/superpowers/",
    "openspec/",
    "/Users/",
    "C:\\Users\\",
    "file://",
)

WIKI_PLACEHOLDER_PATTERNS = (
    "screenshot_placeholder",
    "screenshot placeholder",
    "replace after integrated release-candidate capture",
)

IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_IMAGE_LINK = re.compile(r"!\[[^\]]+\]\(([^)]+)\)")
_FENCED_SHELL = re.compile(
    r"^```(?:bash|sh|shell)\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL
)
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s|$)", re.MULTILINE)
_MAKE_COMMAND = re.compile(r"(?:^|[;&|]\s*|\s)make\s+([A-Za-z0-9_.-]+)")
_SCRIPT_COMMAND = re.compile(
    r"uv\s+run(?:\s+--frozen)?\s+python\s+(scripts/[A-Za-z0-9_./-]+\.py)"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _headings(document: str) -> set[str]:
    headings: set[str] = set()
    for raw_heading in _HEADING.findall(document):
        heading = raw_heading.strip().rstrip("#").strip()
        if heading.startswith("[") and "]" in heading:
            heading = heading[1 : heading.index("]")]
        headings.add(heading)
    return headings


def _relative_link_failures(
    repo_root: Path, relative_path: str, document: str
) -> list[str]:
    failures: list[str] = []
    source = repo_root / relative_path
    for raw_target in _LINK.findall(document):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or target.startswith(("#", "mailto:", "tel:")):
            continue
        decoded_path = unquote(parts.path)
        if not decoded_path:
            continue
        destination = (source.parent / decoded_path).resolve()
        try:
            destination.relative_to(repo_root.resolve())
        except ValueError:
            failures.append(
                f"{relative_path}: relative link escapes the repository: {target}"
            )
            continue
        if not destination.exists():
            failures.append(f"{relative_path}: broken relative link: {target}")
    return failures


def _make_targets(repo_root: Path) -> set[str]:
    makefile = repo_root / "Makefile"
    if not makefile.is_file():
        return set()
    return set(_MAKE_TARGET.findall(_read(makefile)))


def _command_failures(repo_root: Path, relative_path: str, document: str) -> list[str]:
    failures: list[str] = []
    make_targets = _make_targets(repo_root)
    for block in _FENCED_SHELL.findall(document):
        for target in _MAKE_COMMAND.findall(block):
            if target not in make_targets:
                failures.append(
                    f"{relative_path}: unsupported Make target in command example: {target}"
                )
        for script in _SCRIPT_COMMAND.findall(block):
            if not (repo_root / script).is_file():
                failures.append(
                    f"{relative_path}: command references missing script: {script}"
                )
    return failures


def _tracked_boundary_failures(repo_root: Path) -> list[str]:
    if not (repo_root / ".git").exists():
        return []
    try:
        output = subprocess.check_output(
            ["git", "-C", os.fspath(repo_root), "ls-files", "-z"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return ["Unable to inspect tracked paths for the public-boundary contract"]
    tracked_paths = (os.fsdecode(value) for value in output.split(b"\0") if value)
    return [
        f"Internal path is tracked: {path}"
        for path in sorted(tracked_paths)
        if path.startswith(FORBIDDEN_TRACKED_PREFIXES)
    ]


def _required_settings(repo_root: Path) -> set[str]:
    settings = {"STOCK_DESK_WEB_DIST_DIR"}
    environment = repo_root / ".env.example"
    if not environment.is_file():
        return settings
    for line in _read(environment).splitlines():
        match = re.match(r"^(STOCK_DESK_[A-Z0-9_]+)=", line.strip())
        if match:
            settings.add(match.group(1))
    return settings


def _valid_image(path: Path) -> bool:
    try:
        payload = path.read_bytes()
    except OSError:
        return False
    suffix = path.suffix.casefold()
    if suffix == ".png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return payload.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return payload.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return (
            len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
        )
    if suffix == ".svg":
        return b"<svg" in payload[:4096].lower()
    return False


def _wiki_publishable_paths(
    root: Path, *, final: bool
) -> tuple[list[Path], list[Path], list[str]]:
    markdown: list[Path] = []
    images: list[Path] = []
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        relative_text = relative.as_posix()
        if path.is_symlink():
            failures.append(f"{relative_text}: symlink is not publishable")
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        is_publishable = suffix == ".md" or suffix in IMAGE_SUFFIXES
        if not is_publishable:
            continue
        for blocked in WIKI_FORBIDDEN_REFERENCES:
            if blocked.casefold() in relative_text.casefold():
                failures.append(
                    f"{relative_text}: forbidden public-boundary path: {blocked}"
                )
        if final and any(
            placeholder in relative_text.casefold()
            for placeholder in WIKI_PLACEHOLDER_PATTERNS
        ):
            failures.append(
                f"{relative_text}: placeholder path blocks final Wiki publication"
            )
        if suffix == ".md":
            markdown.append(path)
        elif suffix in IMAGE_SUFFIXES:
            images.append(path)
    return markdown, images, failures


def verify_repository(repo_root: Path) -> list[str]:
    """Return public-documentation contract failures without changing the tree."""

    root = repo_root.resolve()
    failures: list[str] = []
    documents: dict[str, str] = {}
    for relative_path in REQUIRED_PUBLIC_DOCUMENTS:
        path = root / relative_path
        if not path.is_file():
            failures.append(f"Missing required public document: {relative_path}")
            continue
        document = _read(path)
        documents[relative_path] = document
        headings = _headings(document)
        for required_heading in REQUIRED_SECTIONS[relative_path]:
            if required_heading not in headings:
                failures.append(
                    f"{relative_path}: missing required heading: {required_heading}"
                )
        for snippet in REQUIRED_PUBLIC_SNIPPETS.get(relative_path, ()):
            if snippet not in document:
                failures.append(
                    f"{relative_path}: missing required guidance: {snippet}"
                )

    public_paths = sorted(root.glob("*.md")) + sorted((root / "docs").rglob("*.md"))
    for path in public_paths:
        relative_path = path.relative_to(root).as_posix()
        document = documents.get(relative_path, _read(path))
        failures.extend(_relative_link_failures(root, relative_path, document))
        failures.extend(_command_failures(root, relative_path, document))
        for blocked in FORBIDDEN_PUBLIC_REFERENCES:
            if blocked in document:
                failures.append(
                    f"{relative_path}: forbidden public-boundary reference: {blocked}"
                )

    english = documents.get("README.md", "")
    if "[简体中文](README.zh-CN.md)" not in english:
        failures.append("README.md must link to README.zh-CN.md")
    chinese = documents.get("README.zh-CN.md", "")
    if "[English](README.md)" not in chinese:
        failures.append("README.zh-CN.md must link to README.md")

    for relative_path, document in (
        ("README.md", english),
        ("README.zh-CN.md", chinese),
    ):
        positions = [
            document.find(pattern) for pattern in SOURCE_FREE_INSTALLER_PATTERNS
        ]
        source_setup = document.find("make bootstrap")
        if any(position < 0 for position in positions):
            failures.append(
                f"{relative_path}: source-free installer artifact names are incomplete"
            )
        elif source_setup >= 0 and max(positions) > source_setup:
            failures.append(
                f"{relative_path}: source-free installers must precede source setup"
            )

    configuration = documents.get("docs/configuration.md", "")
    for setting in sorted(_required_settings(root)):
        if setting not in configuration:
            failures.append(f"docs/configuration.md: missing setting: {setting}")

    failures.extend(_tracked_boundary_failures(root))
    return sorted(set(failures))


def verify_wiki(wiki_root: Path, *, final: bool) -> list[str]:
    """Verify bilingual external Wiki staging or its final publication boundary."""

    if wiki_root.is_symlink():
        return ["Wiki root must not be a symlink"]
    root = wiki_root.absolute()
    if not root.is_dir():
        return [f"Wiki root is not a directory: {root}"]
    failures: list[str] = []
    markdown_paths, image_paths, path_failures = _wiki_publishable_paths(
        root, final=final
    )
    failures.extend(path_failures)
    documents: dict[str, str] = {}
    for path in markdown_paths:
        relative_path = path.relative_to(root).as_posix()
        try:
            document = _read(path)
        except (OSError, UnicodeError):
            failures.append(f"{relative_path}: Markdown is unreadable")
            continue
        documents[relative_path] = document
        failures.extend(_relative_link_failures(root, relative_path, document))
        for blocked in WIKI_FORBIDDEN_REFERENCES:
            if blocked in document:
                failures.append(
                    f"{relative_path}: forbidden public-boundary reference: {blocked}"
                )
        if final:
            casefolded = document.casefold()
            for placeholder in WIKI_PLACEHOLDER_PATTERNS:
                if placeholder in casefolded:
                    failures.append(
                        f"{relative_path}: placeholder blocks final Wiki publication: {placeholder}"
                    )

    for path in image_paths:
        relative_path = path.relative_to(root).as_posix()
        if final and not _valid_image(path):
            failures.append(f"{relative_path}: invalid image blocks final publication")

    checklist = documents.get("PUBLISHING-CHECKLIST.md")
    if final and checklist is not None:
        if "Status: final" not in checklist or re.search(
            r"^- \[ \]", checklist, re.MULTILINE
        ):
            failures.append(
                "PUBLISHING-CHECKLIST.md must be deleted or finalized before publication"
            )

    for stem in REQUIRED_WIKI_PAGES:
        english_path = root / f"{stem}.md"
        chinese_path = root / f"{stem}.zh-CN.md"
        for path in (english_path, chinese_path):
            if not path.is_file():
                failures.append(f"Missing required Wiki page: {path.name}")
        if not english_path.is_file() or not chinese_path.is_file():
            continue
        english = documents.get(english_path.name, "")
        chinese = documents.get(chinese_path.name, "")
        if f"[简体中文]({stem}.zh-CN.md)" not in english:
            failures.append(
                f"{english_path.name}: missing counterpart link to {stem}.zh-CN.md"
            )
        if f"[English]({stem}.md)" not in chinese:
            failures.append(
                f"{chinese_path.name}: missing counterpart link to {stem}.md"
            )
        if stem == "Home":
            continue
        for path, document, required_headings in (
            (english_path, english, ("Steps", "Expected result", "Recovery")),
            (chinese_path, chinese, ("操作步骤", "预期结果", "恢复方法")),
        ):
            headings = _headings(document)
            for heading in required_headings:
                if heading not in headings:
                    failures.append(f"{path.name}: missing required heading: {heading}")
            if not re.search(r"^1\.\s+\S", document, re.MULTILINE):
                failures.append(f"{path.name}: missing ordered workflow steps")
            marker_present = "screenshot_placeholder" in document.casefold()
            if final and marker_present:
                failures.append(
                    f"{path.name}: SCREENSHOT_PLACEHOLDER blocks final Wiki publication"
                )
            if not final and not marker_present:
                failures.append(
                    f"{path.name}: staging page must carry a SCREENSHOT_PLACEHOLDER marker"
                )
            if final and not any(
                urlsplit(target.strip().split(maxsplit=1)[0].strip("<>"))
                .path.casefold()
                .endswith(tuple(IMAGE_SUFFIXES))
                and urlsplit(
                    target.strip().split(maxsplit=1)[0].strip("<>")
                ).path.startswith("images/")
                for target in _IMAGE_LINK.findall(document)
            ):
                failures.append(f"{path.name}: final page is missing a real screenshot")

    for relative_path, document in documents.items():
        if (
            "uv run python scripts/backup.py" in document
            or "uv run python scripts/restore.py" in document
        ):
            required_scope = (
                "仅适用于源码或容器 POSIX"
                if relative_path.endswith(".zh-CN.md")
                else "source/container POSIX only"
            )
            if required_scope not in document:
                failures.append(
                    f"{relative_path}: backup commands require {required_scope} scope"
                )
    return sorted(set(failures))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Stock Desk public documentation"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="application repository root",
    )
    parser.add_argument(
        "--wiki-root",
        type=Path,
        help="optional external bilingual Wiki root",
    )
    parser.add_argument(
        "--final-wiki",
        action="store_true",
        help="reject placeholders and require real Wiki screenshots",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.final_wiki and arguments.wiki_root is None:
        parser.error("--final-wiki requires --wiki-root")
    failures = verify_repository(arguments.repo_root)
    if arguments.wiki_root is not None:
        failures.extend(verify_wiki(arguments.wiki_root, final=arguments.final_wiki))
    if failures:
        print("Documentation verification failed:", file=sys.stderr)
        for failure in sorted(set(failures)):
            print(f"- {failure}", file=sys.stderr)
        return 1
    mode = "final" if arguments.final_wiki else "staging"
    suffix = f" and {mode} Wiki" if arguments.wiki_root is not None else ""
    print(f"Public documentation{suffix} verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
