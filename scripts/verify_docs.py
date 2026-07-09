from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Literal
from urllib.parse import unquote, urlsplit
import warnings

from markdown_it import MarkdownIt
from markdown_it.token import Token
from PIL import Image, UnidentifiedImageError
import yaml  # type: ignore[import-untyped]

from stock_desk.market.types import BAR_SOURCE_PROVIDER_IDS


REQUIRED_PUBLIC_DOCUMENTS = (
    "README.md",
    "README.en.md",
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
        "产品定位",
        "核心功能",
        "下载安装",
        "使用文档",
        "安全与范围",
    ),
    "README.en.md": (
        "Product positioning",
        "Core features",
        "Download and install",
        "Documentation",
        "Safety and scope",
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

REQUIRED_WIKI_PAGE_STEMS = (
    "Home",
    "Feature-Index",
    "Windows-Installation",
    "macOS-Installation",
    "First-Launch-and-Health",
    "Project-Governance-and-Release-Evidence",
    "Data-Sources-and-Tushare",
    "Local-TDX-Data",
    "Data-Updates-and-Provenance",
    "Stock-Pools",
    "Market-Charts",
    "Formula-Studio-Quickstart",
    "Formula-Compatibility-and-Errors",
    "Formula-Versions-and-Safety",
    "MACD-Backtest-Tutorial",
    "A-Share-Execution-and-Costs",
    "Backtest-Metrics-and-Reliability",
    "Backtest-Replay-Export-and-Failures",
    "Model-Provider-Setup",
    "Research-Reports-and-Evidence",
    "Research-Failures-Retries-and-Safety",
    "Task-Center",
    "Responsive-Navigation-and-Accessibility",
    "Credentials-Logs-and-Local-Security",
    "Backup-Restore-Upgrade-and-Uninstall",
    "Troubleshooting",
)

REQUIRED_WIKI_ENTRY_FILES = (
    "Home.md",
    "Home-en.md",
    "_Sidebar.md",
    "_Sidebar-en.md",
    "Feature-Index.md",
    "Feature-Index-en.md",
    "SCREENSHOT-MANIFEST.yml",
)

REPLACED_WIKI_PAGE_FILENAMES = frozenset(
    {
        "Installation.md",
        "Market-Data-and-Charts.md",
        "Formula-Studio.md",
        "Backtesting.md",
        "Multi-Agent-Research.md",
        "Backup-and-Restore.md",
        "Configuration-and-Security.md",
    }
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
    "README.md": ("https://github.com/CongBao/stock-desk/releases/latest",),
    "README.en.md": ("https://github.com/CongBao/stock-desk/releases/latest",),
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
    "~/.ssh/",
    "id_ed25519",
    "BEGIN OPENSSH PRIVATE KEY",
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
SCREENSHOT_MANIFEST_SCHEMA = "stock-desk-documentation-screenshots-v1"
SCREENSHOT_DISCLAIMER = "\u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae"
ACTIVE_REQUIREMENT_IDS = frozenset(f"R-{number:03d}" for number in range(1, 80))
MARKET_SCREENSHOT_PAGE_PREFIXES = (
    "Market-",
    "Data-",
    "Local-TDX-",
    "Stock-Pools",
    "Formula-",
    "MACD-",
    "A-Share-",
    "Backtest-",
)
EVIDENCE_SURFACE_TYPES = frozenset(
    {
        "app-route",
        "wiki-page",
        "windows-installer",
        "macos-installer",
        "github-release",
        "repository-audit",
    }
)
REPOSITORY_AUDIT_LOCATORS = frozenset(
    {
        "requirements-boundary",
        "repository-name",
        "remote",
        "git-identity",
        "local-layout",
        "branch-policy",
        "public-boundary",
        "stage-delivery",
        "open-source-governance",
        "release-verification",
        "documentation-entry",
        "private-spec-boundary",
        "ssh-identity-policy",
    }
)

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_FENCED_SHELL = re.compile(
    r"^```(?:bash|sh|shell)\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL
)

_MARKDOWN = MarkdownIt("gfm-like", {"html": True})

_FEATURE_INDEX_ROW = re.compile(
    r"^\|\s*(R-\d{3}(?:\s*[\u2013\u2014-]\s*R?-?\d{3})?)\s*\|"
    r"\s*\[[^]]+\]\(([^)]+)\)\s*\|"
    r"\s*\[[^]]+\]\(([^)]+)\)\s*\|"
    r"\s*([^|]+?)\s*\|\s*`?([a-z0-9][a-z0-9-]*)`?\s*\|"
    r"\s*`?([^|`]+)`?\s*\|\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class RenderedTarget:
    kind: Literal["link", "image"]
    target: str


@dataclass(frozen=True, slots=True)
class ReadmeCommandEvidence:
    gate: str
    test_selectors: tuple[str, ...]


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

_ATTESTATION_BASE = (
    "gh",
    "attestation",
    "verify",
    "INSTALLER_PATH",
    "--repo",
    "CongBao/stock-desk",
    "--signer-workflow",
    "CongBao/stock-desk/.github/workflows/release.yml",
)
_NATIVE_ATTESTATION_TESTS = (
    "tests/acceptance/test_release_artifacts.py::"
    "test_native_manifest_checksum_sbom_and_attestation_chain_is_revision_bound",
    "tests/acceptance/test_installed_distribution.py::"
    "test_release_workflow_generates_checksums_sbom_and_provenance",
)
_CONTAINER_SMOKE_TESTS = (
    "tests/acceptance/test_container_smoke.py::"
    "test_compose_worker_completes_demo_task_through_shared_sqlite",
)

README_COMMAND_EVIDENCE: dict[tuple[str, ...], ReadmeCommandEvidence] = {
    _ATTESTATION_BASE: ReadmeCommandEvidence(
        gate="clean-install:native-attestation",
        test_selectors=_NATIVE_ATTESTATION_TESTS,
    ),
    (*_ATTESTATION_BASE, "--predicate-type", "https://spdx.dev/Document/v2.3"): (
        ReadmeCommandEvidence(
            gate="clean-install:native-sbom-attestation",
            test_selectors=_NATIVE_ATTESTATION_TESTS,
        )
    ),
    ("docker", "compose", "up", "--build", "--wait"): ReadmeCommandEvidence(
        gate="smoke:release-container",
        test_selectors=_CONTAINER_SMOKE_TESTS,
    ),
    (
        "docker",
        "compose",
        "down",
        "--volumes",
        "--remove-orphans",
    ): ReadmeCommandEvidence(
        gate="smoke:release-container",
        test_selectors=_CONTAINER_SMOKE_TESTS,
    ),
    (
        "uv",
        "run",
        "--frozen",
        "python",
        "scripts/verify_docs.py",
    ): ReadmeCommandEvidence(
        gate="candidate:verify-docs",
        test_selectors=(
            "tests/acceptance/test_release_docs.py::"
            "test_bilingual_readme_baseline_contains_verified_installation_and_use",
        ),
    ),
}

for _target, _selector in {
    "acceptance": "tests/acceptance/test_market_flow.py",
    "acceptance-formula": "tests/acceptance/test_formula_consistency.py",
    "acceptance-backtest": "tests/acceptance/test_backtest_semantics.py",
    "e2e-market": "web/e2e/market.spec.ts",
    "e2e-formula": "web/e2e/formula-studio.spec.ts",
    "e2e-backtest": "web/e2e/backtest.spec.ts",
    "e2e-analysis": "web/e2e/analysis.spec.ts",
    "e2e-task-center": "web/e2e/task-center.spec.ts",
    "security": "tests/security",
}.items():
    README_COMMAND_EVIDENCE[("make", _target)] = ReadmeCommandEvidence(
        gate=f"candidate:make-{_target}",
        test_selectors=(_selector,),
    )

for _target, _selector in {
    "benchmark": "tests/performance/test_chart_query.py",
    "benchmark-formula": "tests/performance/test_formula_preview.py",
    "benchmark-backtest": "tests/performance/test_single_backtest.py",
}.items():
    README_COMMAND_EVIDENCE[("make", _target)] = ReadmeCommandEvidence(
        gate="candidate:make-performance-regressions",
        test_selectors=(_selector,),
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
    allow_extensionless_markdown: bool = False,
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
        if (
            allow_extensionless_markdown
            and rendered.kind == "link"
            and destination.with_name(f"{destination.name}.md") in (allowed_files or ())
        ):
            destination = destination.with_name(f"{destination.name}.md")
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
        if relative_path in {"README.md", "README.en.md"}:
            failures.extend(_readme_command_failures(repo_root, relative_path, block))
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


def _logical_shell_commands(block: str) -> tuple[str, ...]:
    commands: list[str] = []
    pending = ""
    for raw_line in block.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        commands.append(pending)
        pending = ""
    if pending:
        commands.append(pending)
    return tuple(commands)


def _readme_command_failures(
    repo_root: Path, relative_path: str, block: str
) -> list[str]:
    del repo_root
    failures: list[str] = []
    for command in _logical_shell_commands(block):
        if any(token in command for token in ("|", ";", "`", "$(", ">", "<")):
            failures.append(f"{relative_path}: README command is not allowlisted")
            continue
        try:
            arguments = shlex.split(command, posix=True)
        except ValueError:
            failures.append(f"{relative_path}: README command is not allowlisted")
            continue
        if tuple(arguments) not in README_COMMAND_EVIDENCE:
            failures.append(f"{relative_path}: README command is not allowlisted")
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
            if (
                suffix not in PUBLISHABLE_SUFFIXES
                and relative_text != "SCREENSHOT-MANIFEST.yml"
            ):
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
        if relative_path in {"README.md", "README.en.md"}:
            section_positions = [
                document.find(f"## {heading}")
                for heading in REQUIRED_SECTIONS[relative_path]
            ]
            if all(position >= 0 for position in section_positions) and (
                section_positions != sorted(section_positions)
            ):
                failures.append(f"{relative_path}: required sections are out of order")
            if len(document.splitlines()) > 100:
                failures.append(f"{relative_path}: must not exceed 100 lines")
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

    chinese = documents.get("README.md", "")
    if not chinese.splitlines() or chinese.splitlines()[0] != "[English](README.en.md)":
        failures.append("README.md must start with a link to README.en.md")
    english = documents.get("README.en.md", "")
    if not english.splitlines() or english.splitlines()[0] != "[简体中文](README.md)":
        failures.append("README.en.md must start with a link to README.md")

    for relative_path, document in (
        ("README.md", chinese),
        ("README.en.md", english),
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


def _manifest_timestamp_is_utc(value: object) -> bool:
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    else:
        return False
    return (
        candidate.tzinfo is not None
        and candidate.utcoffset() == timezone.utc.utcoffset(candidate)
    )


def _manifest_market_page(page_pairs: object) -> bool:
    if not isinstance(page_pairs, list):
        return False
    return any(
        isinstance(page, str)
        and page.removesuffix("-en.md")
        .removesuffix(".md")
        .startswith(MARKET_SCREENSHOT_PAGE_PREFIXES)
        for page in page_pairs
    )


def _canonical_app_routes() -> frozenset[str]:
    routes_path = (
        Path(__file__).resolve().parent.parent / "web/src/app/route-paths.json"
    )
    try:
        loaded = json.loads(routes_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(loaded, dict) or not all(
        isinstance(key, str)
        and key
        and isinstance(value, str)
        and re.fullmatch(r"/[a-z][a-z0-9-]*", value)
        for key, value in loaded.items()
    ):
        return frozenset()
    routes = frozenset(loaded.values())
    return routes if len(routes) == len(loaded) else frozenset()


def _real_market_source_ids() -> frozenset[str]:
    return frozenset(provider.value for provider in BAR_SOURCE_PROVIDER_IDS)


@lru_cache(maxsize=128)
def _repository_commit_is_reachable(commit: str) -> bool:
    repo = Path(__file__).resolve().parent.parent
    try:
        subprocess.run(
            ("git", "cat-file", "-e", f"{commit}^{{commit}}"),
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        subprocess.run(
            ("git", "merge-base", "--is-ancestor", commit, "HEAD"),
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _surface_tuple(value: object) -> tuple[str, str] | None:
    if isinstance(value, str):
        surface_type, separator, locator = value.partition(":")
        if separator and surface_type and locator:
            return surface_type, locator
        return None
    if not isinstance(value, dict):
        return None
    mapped_type = value.get("type")
    mapped_locator = value.get("locator")
    if not isinstance(mapped_type, str) or not isinstance(mapped_locator, str):
        return None
    return mapped_type, mapped_locator


def _surface_failure(
    surface: tuple[str, str] | None,
    canonical_routes: frozenset[str],
) -> str | None:
    if surface is None:
        return "requires a typed evidence surface"
    surface_type, locator = surface
    if surface_type not in EVIDENCE_SURFACE_TYPES:
        return f"has an unsupported evidence surface type: {surface_type}"
    if surface_type == "app-route":
        if locator not in canonical_routes:
            return f"is not a canonical application route: {locator}"
    elif surface_type == "wiki-page":
        if locator not in REQUIRED_WIKI_PAGE_STEMS:
            return f"has an unknown Wiki page surface: {locator}"
    elif surface_type == "windows-installer":
        if not re.fullmatch(r"stock-desk-<version>-windows-x86_64\.exe", locator):
            return f"has an invalid Windows installer surface: {locator}"
    elif surface_type == "macos-installer":
        if not re.fullmatch(
            r"stock-desk-<version>-macos-(?:x86_64|arm64)\.dmg", locator
        ):
            return f"has an invalid macOS installer surface: {locator}"
    elif surface_type == "github-release":
        if locator != "latest":
            return f"has an invalid GitHub Release surface: {locator}"
    elif locator not in REPOSITORY_AUDIT_LOCATORS:
        return f"has an invalid repository audit surface: {locator}"
    return None


def _screenshot_manifest(
    root: Path,
    *,
    final: bool,
    publication_files: frozenset[Path],
    documents: dict[str, str],
    rendered_targets: dict[str, tuple[RenderedTarget, ...]],
    canonical_routes: frozenset[str],
) -> tuple[dict[str, dict[str, object]], dict[Path, dict[str, object]], list[str]]:
    path = root / "SCREENSHOT-MANIFEST.yml"
    if not path.is_file():
        return {}, {}, ["Screenshot manifest is missing: SCREENSHOT-MANIFEST.yml"]
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        return {}, {}, [f"Screenshot manifest is unreadable: {type(error).__name__}"]
    if not isinstance(loaded, dict):
        return {}, {}, ["Screenshot manifest root must be a mapping"]
    failures: list[str] = []
    if loaded.get("schema_version") != SCREENSHOT_MANIFEST_SCHEMA:
        failures.append(
            "Screenshot manifest has an unsupported schema_version: "
            f"{loaded.get('schema_version')!r}"
        )
    screenshots = loaded.get("screenshots")
    if not isinstance(screenshots, list):
        return {}, {}, [*failures, "Screenshot manifest screenshots must be a list"]

    by_id: dict[str, dict[str, object]] = {}
    valid_captured_images: dict[Path, dict[str, object]] = {}
    paths: set[str] = set()
    captured_digests: dict[str, str] = {}
    images_root = (root / "images").resolve()
    for position, raw_entry in enumerate(screenshots, start=1):
        entry_failure_start = len(failures)
        label = f"Screenshot manifest entry {position}"
        if not isinstance(raw_entry, dict):
            failures.append(f"{label} must be a mapping")
            continue
        entry = {str(key): value for key, value in raw_entry.items()}
        screenshot_id = entry.get("screenshot_id")
        if not isinstance(screenshot_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]*", screenshot_id
        ):
            failures.append(f"{label} has an invalid screenshot_id")
            continue
        label = f"Screenshot manifest {screenshot_id}"
        if screenshot_id in by_id:
            failures.append(f"{label} duplicates screenshot_id")
            continue
        by_id[screenshot_id] = entry

        relative_path = entry.get("path")
        resolved_image: Path | None = None
        if isinstance(relative_path, str):
            candidate = root / relative_path
            resolved_image = candidate.resolve()
            if ".." in Path(relative_path).parts:
                failures.append(f"{label} path escapes Wiki images: {relative_path}")
            else:
                try:
                    resolved_image.relative_to(images_root)
                except ValueError:
                    failures.append(
                        f"{label} path escapes Wiki images: {relative_path}"
                    )
            if candidate.is_symlink():
                failures.append(f"{label} image path must not be a symlink")
        if not isinstance(relative_path, str) or not re.fullmatch(
            r"images/[A-Za-z0-9][A-Za-z0-9._/-]*\.(?:png|jpe?g|webp)",
            relative_path,
            re.IGNORECASE,
        ):
            failures.append(f"{label} has an invalid Wiki-relative image path")
        elif relative_path in paths:
            failures.append(f"{label} duplicates image path: {relative_path}")
        else:
            paths.add(relative_path)

        page_pairs = entry.get("page_pairs")
        if (
            not isinstance(page_pairs, list)
            or len(page_pairs) != 2
            or not all(
                isinstance(page, str) and page.endswith(".md") for page in page_pairs
            )
        ):
            failures.append(f"{label} page_pairs must contain two Markdown pages")
        elif not (page_pairs[1] == page_pairs[0].removesuffix(".md") + "-en.md"):
            failures.append(f"{label} page_pairs must be a Chinese/English pair")

        captions = entry.get("caption_locales")
        if not isinstance(captions, dict) or not all(
            isinstance(captions.get(locale), str) and captions[locale].strip()
            for locale in ("zh-CN", "en")
        ):
            failures.append(f"{label} requires zh-CN and en caption_locales")
        features = entry.get("features")
        if (
            not isinstance(features, list)
            or not features
            or not all(
                isinstance(feature, str) and feature in ACTIVE_REQUIREMENT_IDS
                for feature in features
            )
        ):
            failures.append(f"{label} has invalid features")
        surface = _surface_tuple(entry.get("surface"))
        surface_failure = _surface_failure(surface, canonical_routes)
        if surface_failure is not None:
            failures.append(f"{label} {surface_failure}")
        contains_market_data = entry.get("contains_market_data")
        if type(contains_market_data) is not bool:
            failures.append(f"{label} requires boolean contains_market_data")
        market_surface = (
            surface is not None
            and surface[0] == "app-route"
            and (surface[1] in {"/market", "/formulas", "/backtests"})
        )
        market_page = _manifest_market_page(page_pairs)
        if (market_surface or market_page) and contains_market_data is not True:
            failures.append(
                f"{label} contains_market_data must be true for this surface or page"
            )
        if contains_market_data is False and entry.get("market_data") is not None:
            failures.append(
                f"{label} market_data must be null when contains_market_data is false"
            )
        if entry.get("disclaimer") != SCREENSHOT_DISCLAIMER:
            failures.append(f"{label} has an invalid disclaimer")

        state = entry.get("state")
        if final or state == "captured":
            if isinstance(page_pairs, list):
                for page_name in page_pairs:
                    if not isinstance(page_name, str):
                        continue
                    page_path = root / page_name
                    if (
                        not page_path.is_file()
                        or page_path.resolve() not in publication_files
                    ):
                        failures.append(
                            f"{label} page_pairs page does not exist in the Wiki publication: "
                            f"{page_name}"
                        )
        if state == "pending":
            for field in (
                "viewport",
                "product",
                "captured_at",
                "sha256",
                "market_data",
                "capture",
                "editing",
            ):
                if entry.get(field) is not None:
                    failures.append(f"{label} pending entry must leave {field} null")
            if entry.get("redaction") != "pending":
                failures.append(f"{label} pending entry requires redaction: pending")
            if final:
                failures.append(f"{label} is pending and blocks final publication")
            continue
        if state != "captured":
            failures.append(f"{label} state must be pending or captured")
            continue

        if not isinstance(relative_path, str):
            continue
        image_path = root / relative_path
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append(f"{label} requires a lowercase SHA-256")
        else:
            digest_owner = captured_digests.get(digest)
            if digest_owner is not None:
                failures.append(
                    f"{label} captured screenshot SHA-256 is reused by {digest_owner}"
                )
            else:
                captured_digests[digest] = screenshot_id
            if not image_path.is_file():
                failures.append(f"{label} image does not exist: {relative_path}")
            elif hashlib.sha256(image_path.read_bytes()).hexdigest() != digest:
                failures.append(f"{label} SHA-256 does not match: {relative_path}")
        if resolved_image not in publication_files:
            failures.append(
                f"{label} image is not a scanned Wiki publication file: {relative_path}"
            )
        elif image_path.is_file():
            raster_failure = _raster_failure(image_path)
            if raster_failure is not None:
                failures.append(f"{label} {raster_failure}")

        viewport = entry.get("viewport")
        if not isinstance(viewport, dict) or not all(
            isinstance(viewport.get(key), int) and viewport[key] > 0
            for key in ("width", "height", "device_scale_factor")
        ):
            failures.append(f"{label} requires a positive viewport")
        product = entry.get("product")
        if not isinstance(product, dict):
            failures.append(f"{label} requires product provenance")
        else:
            version = product.get("version")
            commit = product.get("git_commit")
            if not isinstance(version, str) or not re.fullmatch(
                r"(?:[1-9]\d*|0)\.(?:[0-9]+)\.(?:[0-9]+)", version
            ):
                failures.append(f"{label} has an invalid product version")
            elif int(version.split(".")[0]) < 1:
                failures.append(f"{label} requires product version 1.0.0 or later")
            if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
                failures.append(f"{label} requires a 40-character git commit")
            elif not _repository_commit_is_reachable(commit):
                failures.append(
                    f"{label} git_commit is not a reachable repository commit"
                )
        if not _manifest_timestamp_is_utc(entry.get("captured_at")):
            failures.append(f"{label} requires an aware UTC captured_at")
        if entry.get("capture") not in {"playwright", "in-app-browser"}:
            failures.append(f"{label} has an unsupported capture method")
        if entry.get("editing") not in {"none", "crop-only"}:
            failures.append(f"{label} has unsupported editing metadata")
        if entry.get("redaction") != "passed":
            failures.append(f"{label} requires redaction: passed")

        market_data = entry.get("market_data")
        if contains_market_data is True:
            if not isinstance(market_data, dict):
                failures.append(f"{label} requires real market provenance")
            else:
                serialized_market = str(market_data).casefold()
                if any(
                    forbidden in serialized_market
                    for forbidden in ("synthetic", "cc0 demo", "fixture")
                ):
                    failures.append(f"{label} requires real market provenance")
                if not re.fullmatch(
                    r"(?:[036]\d{5})\.(?:SH|SZ)", str(market_data.get("symbol", ""))
                ):
                    failures.append(f"{label} has an invalid A-share symbol")
                if market_data.get("period") not in {"1d", "1w", "60m"}:
                    failures.append(f"{label} has an invalid market period")
                if market_data.get("adjustment") not in {"none", "qfq", "hfq"}:
                    failures.append(f"{label} has an invalid adjustment")
                source = market_data.get("source")
                if not isinstance(source, str) or not source.strip():
                    failures.append(f"{label} requires a market source")
                elif source not in _real_market_source_ids():
                    failures.append(
                        f"{label} market source is not a product ProviderId: {source}"
                    )
                name = market_data.get("name")
                if not isinstance(name, str) or not name.strip():
                    failures.append(f"{label} requires a market instrument name")
                start = str(market_data.get("start", ""))
                end = str(market_data.get("end", ""))
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", end
                ):
                    failures.append(f"{label} requires market start and end dates")
                elif start > end:
                    failures.append(f"{label} market date range is reversed")
                if not _manifest_timestamp_is_utc(market_data.get("cutoff")):
                    failures.append(f"{label} requires an aware UTC market cutoff")
                if not re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(market_data.get("dataset_version", "")),
                ):
                    failures.append(f"{label} requires a dataset version")
                elif market_data.get("dataset_version") == f"sha256:{digest}":
                    failures.append(
                        f"{label} dataset_version must be distinct from screenshot "
                        "SHA-256"
                    )
        if isinstance(page_pairs, list):
            for page_name in page_pairs:
                if not isinstance(page_name, str) or page_name not in documents:
                    continue
                page_path = root / page_name
                expected_image = image_path.resolve()
                referenced = any(
                    rendered.kind == "image"
                    and _local_destination(root, page_path, rendered.target)
                    == expected_image
                    for rendered in rendered_targets.get(page_name, ())
                )
                if not referenced:
                    failures.append(
                        f"{label} article {page_name} must reference {relative_path}"
                    )
        if (
            state == "captured"
            and resolved_image is not None
            and len(failures) == entry_failure_start
        ):
            valid_captured_images[resolved_image] = entry
    return by_id, valid_captured_images, failures


def _github_heading_anchor(heading: str) -> str:
    anchor = heading.casefold().strip()
    anchor = re.sub(r"[^\w\s-]", "", anchor, flags=re.UNICODE)
    anchor = re.sub(r"\s+", "-", anchor)
    return re.sub(r"-+", "-", anchor).strip("-")


def _feature_requirement_ids(value: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", value).replace("\u2013", "-").replace("\u2014", "-")
    match = re.fullmatch(r"R-(\d{3})(?:-R?-?(\d{3}))?", normalized)
    if match is None:
        return ()
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if end < start:
        return ()
    return tuple(f"R-{number:03d}" for number in range(start, end + 1))


def _feature_index_rows(
    document: str,
) -> tuple[list[tuple[tuple[str, ...], str, str, str, str, str]], list[str]]:
    rows: list[tuple[tuple[str, ...], str, str, str, str, str]] = []
    failures: list[str] = []
    lines = document.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("|")
            and ("Screenshot ID" in line or "\u622a\u56fe ID" in line)
            and ("Feature/requirement" in line or "\u529f\u80fd/\u9700\u6c42" in line)
        ),
        None,
    )
    if header_index is None or header_index + 1 >= len(lines):
        return [], ["missing feature-index table header"]
    separator = [cell.strip() for cell in lines[header_index + 1].strip("|").split("|")]
    if len(separator) != 6 or not all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in separator
    ):
        return [], ["invalid feature-index table separator"]
    table_closed = False
    for line_number, line in enumerate(
        lines[header_index + 2 :], start=header_index + 3
    ):
        if not line.startswith("|"):
            table_closed = True
            if re.search(r"\bR-\d{3}\b", line):
                failures.append(f"unparseable table row at line {line_number}: {line}")
            continue
        if table_closed:
            failures.append(f"unparseable table row at line {line_number}: {line}")
            continue
        match = _FEATURE_INDEX_ROW.fullmatch(line)
        if match is None:
            failures.append(f"unparseable table row at line {line_number}: {line}")
            continue
        identifiers = _feature_requirement_ids(match.group(1))
        rows.append(
            (
                identifiers,
                match.group(2).strip(),
                match.group(3).strip(),
                match.group(4).strip(),
                match.group(5).strip(),
                match.group(6).strip(),
            )
        )
    return rows, failures


def _feature_index_failures(
    root: Path,
    documents: dict[str, str],
    screenshot_entries: dict[str, dict[str, object]],
    canonical_routes: frozenset[str],
) -> list[str]:
    failures: list[str] = []
    parsed: dict[str, list[tuple[tuple[str, ...], str, str, str, str, str]]] = {}
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        rows, row_failures = _feature_index_rows(documents.get(filename, ""))
        parsed[filename] = rows
        failures.extend(
            f"Feature index {filename}: {failure}" for failure in row_failures
        )
        if not rows:
            failures.append(f"Feature index {filename}: no machine-readable rows")
            continue
        seen: list[str] = [identifier for row in rows for identifier in row[0]]
        for identifier in sorted(ACTIVE_REQUIREMENT_IDS - set(seen)):
            failures.append(
                f"Feature index {filename}: missing requirement ID: {identifier}"
            )
        for identifier in sorted(set(seen) - ACTIVE_REQUIREMENT_IDS):
            failures.append(
                f"Feature index {filename}: unknown requirement ID: {identifier}"
            )
        for identifier in sorted({item for item in seen if seen.count(item) > 1}):
            failures.append(
                f"Feature index {filename}: duplicate requirement ID: {identifier}"
            )
        for (
            identifiers,
            chinese_target,
            english_target,
            section_text,
            screenshot_id,
            surface_text,
        ) in rows:
            row_label = identifiers[0] if identifiers else "invalid row"
            chinese_section, separator, english_section = section_text.partition(" / ")
            if not separator or not chinese_section or not english_section:
                failures.append(
                    f"Feature index {filename} {row_label}: section must be bilingual: "
                    f"{section_text}"
                )
            else:
                for target, section in (
                    (chinese_target, chinese_section),
                    (english_target, english_section),
                ):
                    target_anchor = unquote(urlsplit(target).fragment).casefold()
                    expected_anchor = _github_heading_anchor(section)
                    if target_anchor != expected_anchor:
                        failures.append(
                            f"Feature index {filename} {row_label}: section {section} "
                            f"does not match target anchor: {target}"
                        )
            surface = _surface_tuple(surface_text)
            surface_failure = _surface_failure(surface, canonical_routes)
            if surface_failure is not None:
                failures.append(
                    f"Feature index {filename} {row_label}: {surface_failure}"
                )
            if screenshot_id not in screenshot_entries:
                failures.append(
                    f"Feature index {filename} {row_label}: missing screenshot reference: "
                    f"{screenshot_id}"
                )
            else:
                screenshot_entry = screenshot_entries[screenshot_id]
                manifest_features = screenshot_entry.get("features")
                if not isinstance(manifest_features, list) or not set(
                    identifiers
                ).issubset(manifest_features):
                    failures.append(
                        f"Feature index {filename} {row_label}: screenshot "
                        f"{screenshot_id} does not cover mapped requirement"
                    )
                manifest_surface = _surface_tuple(screenshot_entry.get("surface"))
                if manifest_surface != surface:
                    failures.append(
                        f"Feature index {filename} {row_label}: screenshot "
                        f"{screenshot_id} surface does not match manifest: "
                        f"{surface} != {manifest_surface}"
                    )
                page_pairs = screenshot_entry.get("page_pairs")
                expected_page_pairs = [
                    (
                        unquote(urlsplit(target).path)
                        if unquote(urlsplit(target).path).endswith(".md")
                        else f"{unquote(urlsplit(target).path)}.md"
                    )
                    for target in (chinese_target, english_target)
                ]
                if page_pairs != expected_page_pairs:
                    failures.append(
                        f"Feature index {filename} {row_label}: screenshot "
                        f"{screenshot_id} page_pairs do not match feature targets"
                    )
            for target in (chinese_target, english_target):
                split = urlsplit(target)
                target_path = unquote(split.path)
                page_name = (
                    target_path if target_path.endswith(".md") else f"{target_path}.md"
                )
                page = root / page_name
                if not page.is_file():
                    failures.append(
                        f"Feature index {filename} {row_label}: referenced page does not exist: "
                        f"{page_name}"
                    )
                    continue
                if not split.fragment:
                    failures.append(
                        f"Feature index {filename} {row_label}: referenced page lacks a section anchor: "
                        f"{target}"
                    )
                    continue
                anchors = {
                    _github_heading_anchor(heading)
                    for heading in _headings(documents.get(page_name, _read(page)))
                }
                if unquote(split.fragment).casefold() not in anchors:
                    failures.append(
                        f"Feature index {filename} {row_label}: referenced section does not exist: "
                        f"{target}"
                    )

    chinese_rows = parsed.get("Feature-Index.md", [])
    english_rows = parsed.get("Feature-Index-en.md", [])
    if chinese_rows and english_rows and chinese_rows != english_rows:
        failures.append(
            "Feature index language pages must contain the same requirement mappings"
        )
    indexed_features: dict[str, set[str]] = {}
    for (
        identifiers,
        _chinese,
        _english,
        _section,
        screenshot_id,
        _surface,
    ) in chinese_rows:
        indexed_features.setdefault(screenshot_id, set()).update(identifiers)
    for screenshot_id, entry in screenshot_entries.items():
        manifest_features = entry.get("features")
        if isinstance(manifest_features, list) and set(manifest_features) != (
            indexed_features.get(screenshot_id, set())
        ):
            failures.append(
                f"Screenshot manifest {screenshot_id} features do not exactly match "
                "Feature index mappings"
            )
    referenced_ids = {
        row[4]
        for rows in parsed.values()
        for row in rows
        if row[4] in screenshot_entries
    }
    for screenshot_id in sorted(set(screenshot_entries) - referenced_ids):
        failures.append(
            f"Feature index has an unreferenced screenshot manifest entry: {screenshot_id}"
        )
    return failures


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
        path.resolve()
        for path in (
            *markdown_paths,
            *image_paths,
            *(
                (root / "SCREENSHOT-MANIFEST.yml",)
                if (root / "SCREENSHOT-MANIFEST.yml").is_file()
                else ()
            ),
        )
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
        if final and path.name.endswith(".zh-CN.md"):
            failures.append(
                f"{relative_path}: legacy .zh-CN Wiki alias is not publishable"
            )
        if final and relative_path in REPLACED_WIKI_PAGE_FILENAMES:
            failures.append(
                f"{relative_path}: replaced Wiki page name is not publishable"
            )
        targets = _rendered_targets(document)
        rendered_targets[relative_path] = targets
        failures.extend(
            _rendered_target_failures(
                root,
                relative_path,
                targets,
                allowed_files=publication_files,
                allow_extensionless_markdown=True,
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

    canonical_routes = _canonical_app_routes()
    if not canonical_routes:
        failures.append("Unable to load canonical application routes")
    screenshot_entries, valid_captured_images, manifest_failures = _screenshot_manifest(
        root,
        final=final,
        publication_files=publication_files,
        documents=documents,
        rendered_targets=rendered_targets,
        canonical_routes=canonical_routes,
    )
    failures.extend(manifest_failures)
    failures.extend(
        _feature_index_failures(root, documents, screenshot_entries, canonical_routes)
    )
    if final:
        for image_path in image_paths:
            relative_path = image_path.relative_to(root).as_posix()
            if not relative_path.startswith("images/"):
                failures.append(
                    f"{relative_path}: publication raster is outside Wiki images/"
                )
            if image_path.resolve() not in valid_captured_images:
                failures.append(
                    f"{relative_path}: must have exactly one valid captured manifest entry"
                )
        for relative_path, targets in rendered_targets.items():
            source = root / relative_path
            for rendered in targets:
                if rendered.kind != "image":
                    continue
                destination = _local_destination(root, source, rendered.target)
                if (
                    destination is not None
                    and destination.suffix.casefold() in APPROVED_RASTER_SUFFIXES
                ):
                    manifest_entry = valid_captured_images.get(destination)
                    if manifest_entry is None:
                        failures.append(
                            f"{relative_path}: local raster {rendered.target} is not "
                            "backed by a valid captured manifest entry"
                        )
                    else:
                        page_pairs = manifest_entry.get("page_pairs")
                        if (
                            not isinstance(page_pairs, list)
                            or relative_path not in page_pairs
                        ):
                            failures.append(
                                f"{relative_path}: local raster {rendered.target} is "
                                "not listed in manifest page_pairs"
                            )

    checklist = documents.get("PUBLISHING-CHECKLIST.md")
    if final and checklist is not None:
        if "Status: final" not in checklist or re.search(
            r"^- \[ \]", checklist, re.MULTILINE
        ):
            failures.append(
                "PUBLISHING-CHECKLIST.md must be deleted or finalized before publication"
            )

    for filename in REQUIRED_WIKI_ENTRY_FILES:
        path = root / filename
        if not path.is_file():
            failures.append(f"Missing required Wiki entry file: {filename}")

    for filename, required_link in (
        ("_Sidebar.md", "[English](Home-en)"),
        ("_Sidebar-en.md", "[简体中文](Home)"),
    ):
        sidebar = documents.get(filename, "")
        if sidebar and required_link not in sidebar:
            failures.append(f"{filename}: missing language entry link: {required_link}")

    if final:
        sidebar_targets: dict[str, set[str]] = {}
        for filename in ("_Sidebar.md", "_Sidebar-en.md"):
            sidebar_targets[filename] = {
                unquote(urlsplit(rendered.target).path)
                for rendered in rendered_targets.get(filename, ())
                if rendered.kind == "link"
                and not urlsplit(rendered.target).scheme
                and not urlsplit(rendered.target).netloc
            }
        chinese_targets = sidebar_targets["_Sidebar.md"]
        english_targets = sidebar_targets["_Sidebar-en.md"]
        for stem in REQUIRED_WIKI_PAGE_STEMS:
            if stem not in chinese_targets:
                failures.append(
                    f"_Sidebar.md: missing authoritative Chinese target: {stem}"
                )
            english_target = f"{stem}-en"
            if english_target not in english_targets:
                failures.append(
                    "_Sidebar-en.md: missing authoritative English target: "
                    f"{english_target}"
                )
        for wrong_target in sorted(
            {f"{stem}-en" for stem in REQUIRED_WIKI_PAGE_STEMS if stem != "Home"}
            & chinese_targets
        ):
            failures.append(
                f"_Sidebar.md: cross-language navigation target: {wrong_target}"
            )
        for wrong_target in sorted(
            (set(REQUIRED_WIKI_PAGE_STEMS) - {"Home"}) & english_targets
        ):
            failures.append(
                f"_Sidebar-en.md: cross-language navigation target: {wrong_target}"
            )

    for stem in REQUIRED_WIKI_PAGE_STEMS:
        chinese_path = root / f"{stem}.md"
        english_path = root / f"{stem}-en.md"
        for path in (chinese_path, english_path):
            if not path.is_file():
                failures.append(f"Missing required Wiki page: {path.name}")
        if not english_path.is_file() or not chinese_path.is_file():
            continue
        english = documents.get(english_path.name, "")
        chinese = documents.get(chinese_path.name, "")
        if f"[简体中文]({stem})" not in english:
            failures.append(f"{english_path.name}: missing counterpart link to {stem}")
        if f"[English]({stem}-en)" not in chinese:
            failures.append(
                f"{chinese_path.name}: missing counterpart link to {stem}-en"
            )
        if stem in {"Home", "Feature-Index"}:
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
                    manifest_entry = valid_captured_images.get(destination)
                    page_pairs = (
                        manifest_entry.get("page_pairs")
                        if manifest_entry is not None
                        else None
                    )
                    if isinstance(page_pairs, list) and path.name in page_pairs:
                        has_real_screenshot = True
                        break
                if not has_real_screenshot:
                    failures.append(
                        f"{path.name}: final page is missing a real screenshot backed by "
                        "captured manifest evidence"
                    )

    for relative_path, document in documents.items():
        if (
            "uv run python scripts/backup.py" in document
            or "uv run python scripts/restore.py" in document
        ):
            required_scope = (
                "仅适用于源码或容器 POSIX"
                if not relative_path.endswith("-en.md")
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
