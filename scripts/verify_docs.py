from __future__ import annotations

import argparse
from dataclasses import dataclass
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal
from urllib.parse import unquote, urlsplit
import warnings

from markdown_it import MarkdownIt
from markdown_it.token import Token
from PIL import Image, UnidentifiedImageError


REQUIRED_PUBLIC_DOCUMENTS = (
    "README.md",
    "README.zh-CN.md",
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "docs/architecture.md",
    "docs/backup-and-restore.md",
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
    "docs/backup-and-restore.md": (
        "Deployment support",
        "Upgrade and rollback procedure",
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
        "user-writable install location",
    ),
    "docs/backup-and-restore.md": (
        "Compose image digest",
        "immutable source commit",
        "exact macOS installer artifact",
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

APPROVED_RASTER_SUFFIXES = frozenset({".jpeg", ".jpg", ".png", ".webp"})
PUBLISHABLE_SUFFIXES = frozenset({".md", *APPROVED_RASTER_SUFFIXES})
ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
MIN_SCREENSHOT_WIDTH = 320
MIN_SCREENSHOT_HEIGHT = 180

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_FENCED_SHELL = re.compile(
    r"^```(?:bash|sh|shell)\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL
)

_MARKDOWN = MarkdownIt("gfm-like", {"html": True})


@dataclass(frozen=True, slots=True)
class RenderedTarget:
    kind: Literal["link", "image"]
    target: str


class _RenderedHTMLTargets(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[RenderedTarget] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.casefold(): value for name, value in attrs}
        normalized_tag = tag.casefold()
        if normalized_tag == "a" and attributes.get("href"):
            self.targets.append(RenderedTarget("link", attributes["href"] or ""))
        elif normalized_tag == "img" and attributes.get("src"):
            self.targets.append(RenderedTarget("image", attributes["src"] or ""))


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


def _rendered_targets(document: str) -> tuple[RenderedTarget, ...]:
    rendered: list[RenderedTarget] = []

    def visit(tokens: list[Token]) -> None:
        for token in tokens:
            if token.type == "link_open":
                target = token.attrGet("href")
                if isinstance(target, str) and target:
                    rendered.append(RenderedTarget("link", target))
            elif token.type == "image":
                target = token.attrGet("src")
                if isinstance(target, str) and target:
                    rendered.append(RenderedTarget("image", target))
            elif token.type in {"html_block", "html_inline"}:
                parser = _RenderedHTMLTargets()
                parser.feed(token.content)
                parser.close()
                rendered.extend(parser.targets)
            if token.children:
                visit(token.children)

    visit(_MARKDOWN.parse(document))
    return tuple(rendered)


def _local_destination(root: Path, source: Path, target: str) -> Path | None:
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or target.startswith("#"):
        return None
    decoded_path = unquote(parts.path)
    if not decoded_path:
        return None
    return (source.parent / decoded_path).resolve()


def _rendered_target_failures(
    root: Path,
    relative_path: str,
    targets: tuple[RenderedTarget, ...],
    *,
    allowed_files: frozenset[Path] | None = None,
) -> list[str]:
    failures: list[str] = []
    source = root / relative_path
    resolved_root = root.resolve()
    for rendered in targets:
        target = rendered.target
        parts = urlsplit(target)
        if parts.scheme or parts.netloc:
            if rendered.kind == "image":
                failures.append(
                    f"{relative_path}: external image cannot be verified: {target}"
                )
            elif parts.scheme.casefold() not in ALLOWED_LINK_SCHEMES:
                failures.append(
                    f"{relative_path}: unsupported rendered link scheme: {target}"
                )
            continue
        if target.startswith("#"):
            continue
        destination = _local_destination(root, source, target)
        if destination is None:
            continue
        try:
            destination.relative_to(resolved_root)
        except ValueError:
            failures.append(
                f"{relative_path}: rendered {rendered.kind} escapes the publication root: {target}"
            )
            continue
        if allowed_files is not None and destination not in allowed_files:
            failures.append(
                f"{relative_path}: rendered {rendered.kind} target is not a scanned publication file: {target}"
            )
            continue
        if rendered.kind == "image":
            if not destination.is_file():
                failures.append(
                    f"{relative_path}: image is not a regular image file: {target}"
                )
            elif destination.suffix.casefold() not in APPROVED_RASTER_SUFFIXES:
                failures.append(
                    f"{relative_path}: unsupported rendered image type: {target}"
                )
        elif not destination.exists():
            failures.append(f"{relative_path}: broken rendered link: {target}")
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


def _raster_failure(path: Path) -> str | None:
    expected_formats = {
        ".jpeg": "JPEG",
        ".jpg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
    }
    expected_format = expected_formats.get(path.suffix.casefold())
    if expected_format is None:
        return "unsupported raster type"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as candidate:
                if candidate.format != expected_format:
                    return "decoded format does not match the filename"
                candidate.verify()
            with Image.open(path) as decoded:
                decoded.load()
                width, height = decoded.size
                if width < MIN_SCREENSHOT_WIDTH or height < MIN_SCREENSHOT_HEIGHT:
                    return (
                        "screenshot dimensions are too small "
                        f"({width}x{height}; minimum "
                        f"{MIN_SCREENSHOT_WIDTH}x{MIN_SCREENSHOT_HEIGHT})"
                    )
                sample = decoded.convert("RGB").resize((64, 36))
                colors = sample.getcolors(maxcolors=(64 * 36) + 1)
                if colors is not None and len(colors) < 4:
                    return "screenshot content is visually trivial"
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ) as error:
        return f"image decode failed: {type(error).__name__}"
    return None


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
        relative_casefolded = relative_text.casefold()
        for blocked in WIKI_FORBIDDEN_REFERENCES:
            if blocked.casefold() in relative_casefolded:
                failures.append(
                    f"{relative_text}: forbidden public-boundary path: {blocked}"
                )
        if final and any(
            placeholder in relative_casefolded
            for placeholder in WIKI_PLACEHOLDER_PATTERNS
        ):
            failures.append(
                f"{relative_text}: placeholder path blocks final Wiki publication"
            )
        if path.is_symlink():
            failures.append(f"{relative_text}: symlink is not publishable")
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        try:
            payload = path.read_bytes()
        except OSError:
            failures.append(f"{relative_text}: publication file is unreadable")
            continue
        payload_casefolded = payload.lower()
        for blocked in WIKI_FORBIDDEN_REFERENCES:
            if blocked.casefold().encode("utf-8") in payload_casefolded:
                failures.append(
                    f"{relative_text}: forbidden public-boundary content: {blocked}"
                )
        if final:
            for placeholder in WIKI_PLACEHOLDER_PATTERNS:
                if placeholder.encode("utf-8") in payload_casefolded:
                    failures.append(
                        f"{relative_text}: placeholder content blocks final Wiki publication: {placeholder}"
                    )
            if suffix not in PUBLISHABLE_SUFFIXES:
                failures.append(
                    f"{relative_text}: unsupported Wiki publication file type"
                )
                continue
        if suffix == ".md":
            markdown.append(path)
        elif suffix in APPROVED_RASTER_SUFFIXES:
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
        failures.extend(
            _rendered_target_failures(root, relative_path, _rendered_targets(document))
        )
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
    publication_files = frozenset(
        path.resolve() for path in (*markdown_paths, *image_paths)
    )
    images_root = (root / "images").resolve()
    documents: dict[str, str] = {}
    rendered_targets: dict[str, tuple[RenderedTarget, ...]] = {}
    for path in markdown_paths:
        relative_path = path.relative_to(root).as_posix()
        try:
            document = _read(path)
        except (OSError, UnicodeError):
            failures.append(f"{relative_path}: Markdown is unreadable")
            continue
        documents[relative_path] = document
        targets = _rendered_targets(document)
        rendered_targets[relative_path] = targets
        failures.extend(
            _rendered_target_failures(
                root,
                relative_path,
                targets,
                allowed_files=publication_files,
            )
        )
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
        if final:
            image_failure = _raster_failure(path)
            if image_failure is not None:
                failures.append(f"{relative_path}: {image_failure}")

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
            if final:
                has_real_screenshot = False
                for rendered in rendered_targets.get(path.name, ()):
                    if rendered.kind != "image":
                        continue
                    destination = _local_destination(root, path, rendered.target)
                    if destination is None:
                        continue
                    try:
                        destination.relative_to(images_root)
                    except ValueError:
                        continue
                    if destination not in publication_files:
                        continue
                    if destination.suffix.casefold() not in APPROVED_RASTER_SUFFIXES:
                        continue
                    if destination.is_file() and _raster_failure(destination) is None:
                        has_real_screenshot = True
                        break
                if not has_real_screenshot:
                    failures.append(
                        f"{path.name}: final page is missing a real screenshot"
                    )

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
