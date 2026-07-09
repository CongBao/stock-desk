from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import zlib

import pytest

import scripts.verify_docs as verify_docs_module
from scripts.verify_docs import (
    main,
    verify_repository,
    verify_wiki,
)


EXPECTED_WIKI_PAGE_STEMS = (
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

EXPECTED_REPLACED_WIKI_PAGES = (
    "Installation.md",
    "Market-Data-and-Charts.md",
    "Formula-Studio.md",
    "Backtesting.md",
    "Multi-Agent-Research.md",
    "Backup-and-Restore.md",
    "Configuration-and-Security.md",
)


REPOSITORY_DOCUMENTS = {
    "README.md": """[English](README.en.md)

# Stock Desk

## 产品定位

本地优先的个人 A 股研究工作台。

## 核心功能

使用任务中心、行情图表、公式工作室、回测和研究功能。

## 下载安装

从 https://github.com/CongBao/stock-desk/releases/latest 选择无需源码的
`stock-desk-<version>-windows-x86_64.exe`、
`stock-desk-<version>-macos-x86_64.dmg` 或
`stock-desk-<version>-macos-arm64.dmg` 安装包。

## 使用文档

参阅[配置](docs/configuration.md)和[免责声明](docs/disclaimer.md)。

## 安全与范围

仅供研究，不连接实盘交易。
""",
    "README.en.md": """[简体中文](README.md)

# Stock Desk

## Product positioning

A local-first personal A-share research desk.

## Core features

Use the task center, market charts, Formula Studio, backtesting, and research.

## Download and install

Choose a source-free installer from
https://github.com/CongBao/stock-desk/releases/latest:
`stock-desk-<version>-windows-x86_64.exe`,
`stock-desk-<version>-macos-x86_64.dmg`, or
`stock-desk-<version>-macos-arm64.dmg`.

## Documentation

See [configuration](docs/configuration.md) and the [disclaimer](docs/disclaimer.md).

## Safety and scope

Research only; no live trading.
""",
    "CONTRIBUTING.md": """# Contributing

## Development setup

```bash
make bootstrap
```

## Quality gates

```bash
make test
```

## Pull requests

Keep changes focused.
""",
    "SUPPORT.md": """# Support

## Questions

Open a discussion.

## Bug reports

Include diagnostics.

## Security

Follow SECURITY.md.
""",
    "CHANGELOG.md": """# Changelog

## Unreleased

Documentation improvements.

## 0.5.0

Multi-agent research.
""",
    "ROADMAP.md": """# Roadmap

## Released

Stages 0 through 4.

## Planned

Release readiness.
""",
    "docs/architecture.md": """# Architecture

## Deployment model

Local API and worker.

### Native installer topology

The parent launcher creates API and worker children on a random 127.0.0.1 port.

### Source development topology

Supervised source processes.

### Container topology

Separate API and worker containers.

The native packaged runtime does not self-mutate, but its user-writable install location can be changed by the user.

## Modules and boundaries

Ports and adapters.

## Data and storage

Local data directory.

## Trust and security

Localhost by default.
""",
    "docs/backup-and-restore.md": """# Backup, restore, upgrade, and rollback

## Deployment support

Record the Compose image digest, immutable source commit, or exact macOS installer artifact.

## Upgrade and rollback procedure

Restore each deployment with its recorded identity.
""",
    "docs/configuration.md": """# Configuration

## Native installers

Windows uses `%LOCALAPPDATA%\\stock-desk`; macOS uses
`~/Library/Application Support/stock-desk`; the key is `config/master.key`.

## Source development

Use a local `.env` and explicit master key.

## Native development

Use the sample environment file.

## Container deployment

Use Docker Compose.

## Application settings

`STOCK_DESK_APP_NAME`, `STOCK_DESK_DATA_DIR`, `STOCK_DESK_DATABASE_URL`,
`STOCK_DESK_MASTER_KEY`, and `STOCK_DESK_WEB_DIST_DIR`.

## Container settings

`STOCK_DESK_UID`, `STOCK_DESK_GID`, `STOCK_DESK_IMAGE`, and
`STOCK_DESK_TDX_HOST_PATH`.

## Provider credentials

Store provider keys locally.
""",
    "docs/troubleshooting.md": """# Troubleshooting

## Startup and health

Inspect the health endpoint.

## Data and charts

Check the configured data source.

## Tasks and workers

Inspect task diagnostics.

## Model providers

Test credentials locally.

## Backup and restore

Restore into an empty data directory.
""",
    "docs/disclaimer.md": """# Disclaimer

## Research use only

This software is for research, not investment advice or live trading.

## Data limitations

Data may be delayed or incomplete.

## Model limitations

Generated output may be inaccurate.

## User responsibility

Verify all results independently.
""",
}


def _write_repository(root: Path) -> None:
    for relative_path, content in REPOSITORY_DOCUMENTS.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / ".env.example").write_text(
        "\n".join(
            (
                "STOCK_DESK_APP_NAME=Stock Desk",
                "STOCK_DESK_DATA_DIR=./.data",
                "STOCK_DESK_DATABASE_URL=sqlite:///stock-desk.db",
                "STOCK_DESK_MASTER_KEY=",
                "STOCK_DESK_UID=1000",
                "STOCK_DESK_GID=1000",
                "STOCK_DESK_IMAGE=stock-desk:local",
                "STOCK_DESK_TDX_HOST_PATH=./.data/tdx",
            )
        ),
        encoding="utf-8",
    )
    (root / "Makefile").write_text(
        ("bootstrap:\n\t@true\ndev:\n\t@true\ntest:\n\t@true\nacceptance:\n\t@true\n"),
        encoding="utf-8",
    )


def _write_wiki(root: Path) -> None:
    for stem in EXPECTED_WIKI_PAGE_STEMS:
        if stem == "Home":
            chinese = """# Stock Desk 使用手册

[English](Home-en)

## 从这里开始

参阅功能指南。
"""
            english = """# Stock Desk User Guide

[简体中文](Home)

## Start here

See the feature guides.
"""
        elif stem == "Feature-Index":
            rows = "\n".join(
                f"| R-{number:03d} | [\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb) | "
                "[English home](Home-en#start-here) | \u4ece\u8fd9\u91cc\u5f00\u59cb / Start here | "
                "`planned-home` | `app-route:/market` |"
                for number in range(1, 80)
            )
            chinese = f"""# \u529f\u80fd\u7d22\u5f15

[English](Feature-Index-en)

## \u9700\u6c42\u5230\u9875\u9762

| \u529f\u80fd/\u9700\u6c42 | \u4e2d\u6587\u9875\u9762 | English page | \u7ae0\u8282 | \u622a\u56fe ID | \u8def\u7531/\u754c\u9762 |
| --- | --- | --- | --- | --- | --- |
{rows}
"""
            english = f"""# Feature index

[\u7b80\u4f53\u4e2d\u6587](Feature-Index)

## Requirements to pages

| Feature/requirement | Chinese page | English page | Section | Screenshot ID | Route/surface |
| --- | --- | --- | --- | --- | --- |
{rows}
"""
        else:
            chinese = f"""# {stem.replace("-", " ")}

[English]({stem}-en)

<!-- SCREENSHOT_PLACEHOLDER: replace after integrated release-candidate capture -->

## 操作步骤

1. 打开 Stock Desk。
2. 完成工作流。

## 预期结果

结果可见。

## 恢复方法

返回任务中心后重试。
"""
            english = f"""# {stem.replace("-", " ")}

[简体中文]({stem})

<!-- SCREENSHOT_PLACEHOLDER: replace after integrated release-candidate capture -->

## Steps

1. Open Stock Desk.
2. Complete the workflow.

## Expected result

The result is visible.

## Recovery

Return to the task center and retry.
"""
        (root / f"{stem}.md").write_text(chinese, encoding="utf-8")
        (root / f"{stem}-en.md").write_text(english, encoding="utf-8")
    chinese_navigation = "\n".join(
        f"- [{stem}]({stem})" for stem in EXPECTED_WIKI_PAGE_STEMS
    )
    english_navigation = "\n".join(
        f"- [{stem}]({stem}-en)" for stem in EXPECTED_WIKI_PAGE_STEMS
    )
    (root / "_Sidebar.md").write_text(
        f"[English](Home-en)\n\n{chinese_navigation}\n", encoding="utf-8"
    )
    (root / "_Sidebar-en.md").write_text(
        f"[简体中文](Home)\n\n{english_navigation}\n", encoding="utf-8"
    )
    manifest_features = ", ".join(f"R-{number:03d}" for number in range(1, 80))
    (root / "SCREENSHOT-MANIFEST.yml").write_text(
        f"""schema_version: stock-desk-documentation-screenshots-v1
screenshots:
  - screenshot_id: planned-home
    path: images/planned-home.png
    page_pairs: [Home.md, Home-en.md]
    caption_locales:
      zh-CN: \u4e2d\u6587\u9ed8\u8ba4\u5165\u53e3
      en: Chinese-default entry
    features: [{manifest_features}]
    surface:
      type: app-route
      locator: /market
    contains_market_data: true
    state: pending
    viewport: null
    product: null
    captured_at: null
    sha256: null
    market_data: null
    capture: null
    editing: null
    redaction: pending
    disclaimer: \u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae
""",
        encoding="utf-8",
    )


def _png_bytes(width: int, height: int, *, varied: bool, seed: int = 0) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", checksum)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            value = (x * 7 + y * 13 + seed * 17) % 256 if varied else 128
            rows.extend((value, (value * 3) % 256, (value * 5) % 256))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + chunk(b"IEND", b"")
    )


def _mark_planned_home_captured(root: Path, payload: bytes) -> None:
    image = root / "images" / "planned-home.png"
    image.parent.mkdir(exist_ok=True)
    image.write_bytes(payload)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest = root / "SCREENSHOT-MANIFEST.yml"
    document = manifest.read_text(encoding="utf-8")
    replacements = {
        "    state: pending": "    state: captured",
        "    viewport: null": (
            "    viewport: {width: 1440, height: 1000, device_scale_factor: 1}"
        ),
        "    product: null": (f"    product: {{version: 1.0.0, git_commit: {commit}}}"),
        "    captured_at: null": "    captured_at: 2026-07-09T00:00:00Z",
        "    sha256: null": f"    sha256: {hashlib.sha256(payload).hexdigest()}",
        "    market_data: null": (
            "    market_data:\n"
            "      symbol: 600519.SH\n"
            "      name: \u8d35\u5dde\u8305\u53f0\n"
            "      period: 1d\n"
            "      adjustment: qfq\n"
            "      start: 2021-01-01\n"
            "      end: 2026-07-08\n"
            "      source: tushare\n"
            "      cutoff: 2026-07-08T07:00:00Z\n"
            "      dataset_version: sha256:"
            f"{hashlib.sha256(b'planned-home-dataset').hexdigest()}"
        ),
        "    capture: null": "    capture: playwright",
        "    editing: null": "    editing: none",
        "    redaction: pending": "    redaction: passed",
    }
    for old, new in replacements.items():
        document = document.replace(old, new)
    manifest.write_text(document, encoding="utf-8")


def _finalize_wiki(root: Path) -> None:
    image_dir = root / "images"
    image_dir.mkdir()
    png = _png_bytes(640, 360, varied=True)
    for stem in EXPECTED_WIKI_PAGE_STEMS:
        if stem == "Home":
            continue
        for suffix in ("", "-en"):
            page = root / f"{stem}{suffix}.md"
            image_name = f"{stem}{suffix}.png"
            page.write_text(
                page.read_text(encoding="utf-8").replace(
                    "<!-- SCREENSHOT_PLACEHOLDER: replace after integrated release-candidate capture -->",
                    f"![Verified release-candidate screenshot](images/{image_name})",
                ),
                encoding="utf-8",
            )
            (image_dir / image_name).write_bytes(png)


def _write_complete_final_wiki(root: Path) -> None:
    _write_wiki(root)
    image_dir = root / "images"
    image_dir.mkdir()
    repo = Path(__file__).resolve().parents[2]
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assignments: dict[str, list[str]] = {stem: [] for stem in EXPECTED_WIKI_PAGE_STEMS}
    for number in range(1, 80):
        stem = EXPECTED_WIKI_PAGE_STEMS[(number - 1) % len(EXPECTED_WIKI_PAGE_STEMS)]
        assignments[stem].append(f"R-{number:03d}")

    rows: list[str] = []
    entries: list[str] = []
    for ordinal, (stem, features) in enumerate(assignments.items(), start=1):
        payload = _png_bytes(640, 360, varied=True, seed=ordinal)
        if stem == "Home":
            chinese_section, english_section = (
                "\u4ece\u8fd9\u91cc\u5f00\u59cb",
                "Start here",
            )
            chinese_anchor, english_anchor = (
                "\u4ece\u8fd9\u91cc\u5f00\u59cb",
                "start-here",
            )
        elif stem == "Feature-Index":
            chinese_section, english_section = (
                "\u9700\u6c42\u5230\u9875\u9762",
                "Requirements to pages",
            )
            chinese_anchor, english_anchor = (
                "\u9700\u6c42\u5230\u9875\u9762",
                "requirements-to-pages",
            )
        else:
            chinese_section, english_section = "\u64cd\u4f5c\u6b65\u9aa4", "Steps"
            chinese_anchor, english_anchor = "\u64cd\u4f5c\u6b65\u9aa4", "steps"
        if stem in {
            "Market-Charts",
            "Stock-Pools",
            "Responsive-Navigation-and-Accessibility",
            "First-Launch-and-Health",
        }:
            surface_type, locator = "app-route", "/market"
        elif stem.startswith("Formula-"):
            surface_type, locator = "app-route", "/formulas"
        elif stem.startswith(("MACD-", "A-Share-", "Backtest-")):
            surface_type, locator = "app-route", "/backtests"
        else:
            surface_type, locator = "wiki-page", stem
        screenshot_id = f"final-{stem.casefold()}"
        image_relative = f"images/{screenshot_id}.png"
        image = root / image_relative
        image.write_bytes(payload)
        for suffix in ("", "-en"):
            page = root / f"{stem}{suffix}.md"
            document = page.read_text(encoding="utf-8")
            document = document.replace(
                "<!-- SCREENSHOT_PLACEHOLDER: replace after integrated release-candidate capture -->",
                f"![Captured evidence]({image_relative})",
            )
            if image_relative not in document:
                document += f"\n![Captured evidence]({image_relative})\n"
            page.write_text(document, encoding="utf-8")
        for requirement_id in features:
            rows.append(
                f"| {requirement_id} | [\u4e2d\u6587\u9875\u9762]({stem}#{chinese_anchor}) | "
                f"[English page]({stem}-en#{english_anchor}) | "
                f"{chinese_section} / {english_section} | `{screenshot_id}` | "
                f"`{surface_type}:{locator}` |"
            )
        contains_market_data = (
            surface_type == "app-route"
            and locator
            in {
                "/market",
                "/formulas",
                "/backtests",
            }
            or verify_docs_module._manifest_market_page([f"{stem}.md", f"{stem}-en.md"])
        )
        market_data = (
            """    market_data:
      symbol: 600519.SH
      name: \u8d35\u5dde\u8305\u53f0
      period: 1d
      adjustment: qfq
      start: 2021-01-01
      end: 2026-07-08
      source: tushare
      cutoff: 2026-07-08T07:00:00Z
      dataset_version: sha256:"""
            + hashlib.sha256(f"dataset:{stem}".encode()).hexdigest()
            if contains_market_data
            else "    market_data: null"
        )
        entries.append(
            f"""  - screenshot_id: {screenshot_id}
    path: {image_relative}
    page_pairs: [{stem}.md, {stem}-en.md]
    caption_locales: {{zh-CN: \u5b8c\u6574\u8bc1\u636e, en: Complete evidence}}
    features: [{", ".join(features)}]
    surface: {{type: {surface_type}, locator: {locator}}}
    contains_market_data: {str(contains_market_data).lower()}
    state: captured
    viewport: {{width: 1440, height: 1000, device_scale_factor: 1}}
    product: {{version: 1.0.0, git_commit: {commit}}}
    captured_at: 2026-07-09T00:00:00Z
    sha256: {hashlib.sha256(payload).hexdigest()}
{market_data}
    capture: playwright
    editing: none
    redaction: passed
    disclaimer: \u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae"""
        )
    table = "\n".join(rows)
    (root / "Feature-Index.md").write_text(
        f"""# \u529f\u80fd\u7d22\u5f15

[English](Feature-Index-en)

## \u9700\u6c42\u5230\u9875\u9762

| \u529f\u80fd/\u9700\u6c42 | \u4e2d\u6587\u9875\u9762 | English page | \u7ae0\u8282 | \u622a\u56fe ID | \u8bc1\u636e\u8868\u9762 |
| --- | --- | --- | --- | --- | --- |
{table}

![Captured evidence](images/final-feature-index.png)
""",
        encoding="utf-8",
    )
    (root / "Feature-Index-en.md").write_text(
        f"""# Feature index

[\u7b80\u4f53\u4e2d\u6587](Feature-Index)

## Requirements to pages

| Feature/requirement | Chinese page | English page | Section | Screenshot ID | Evidence surface |
| --- | --- | --- | --- | --- | --- |
{table}

![Captured evidence](images/final-feature-index.png)
""",
        encoding="utf-8",
    )
    (root / "SCREENSHOT-MANIFEST.yml").write_text(
        "schema_version: stock-desk-documentation-screenshots-v1\nscreenshots:\n"
        + "\n".join(entries)
        + "\n",
        encoding="utf-8",
    )


def test_repository_documentation_contract_passes_for_complete_tree(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)

    assert verify_repository(tmp_path) == []


def test_complete_final_wiki_fixture_passes_every_publication_gate(
    tmp_path: Path,
) -> None:
    _write_complete_final_wiki(tmp_path)

    assert verify_wiki(tmp_path, final=True) == []


def test_wiki_real_market_sources_match_product_bar_providers() -> None:
    from stock_desk.market.routing import SourcePriorities
    from stock_desk.market.types import BAR_SOURCE_PROVIDER_IDS

    assert verify_docs_module._real_market_source_ids() == frozenset(
        {"tushare", "akshare", "baostock", "tdx_local"}
    )
    assert SourcePriorities().bars == BAR_SOURCE_PROVIDER_IDS


def test_final_wiki_rejects_copied_image_under_another_name(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    first = tmp_path / "images/final-home.png"
    second = tmp_path / "images/final-feature-index.png"
    first_digest = hashlib.sha256(first.read_bytes()).hexdigest()
    second_digest = hashlib.sha256(second.read_bytes()).hexdigest()
    second.write_bytes(first.read_bytes())
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f"    sha256: {second_digest}", f"    sha256: {first_digest}", 1
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any("captured screenshot SHA-256 is reused" in item for item in failures)


def test_final_wiki_separates_dataset_digest_from_screenshot_digest(
    tmp_path: Path,
) -> None:
    _write_complete_final_wiki(tmp_path)
    image = tmp_path / "images/final-market-charts.png"
    image_digest = hashlib.sha256(image.read_bytes()).hexdigest()
    dataset_digest = hashlib.sha256(b"dataset:Market-Charts").hexdigest()
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f"      dataset_version: sha256:{dataset_digest}",
            f"      dataset_version: sha256:{image_digest}",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "dataset_version must be distinct from screenshot SHA-256" in item
        for item in failures
    )


def test_final_wiki_rejects_fictional_market_source(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "      source: tushare", "      source: fictional_provider", 1
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any("market source is not a product ProviderId" in item for item in failures)


def test_final_wiki_rejects_shape_only_product_commit(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    actual = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(actual, "f" * 40, 1),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "git_commit is not a reachable repository commit" in item for item in failures
    )


def test_market_surface_cannot_disable_market_provenance(tmp_path: Path) -> None:
    _write_complete_final_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "    contains_market_data: true",
            "    contains_market_data: false",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any("contains_market_data must be true" in item for item in failures)


def test_repository_contract_reports_missing_files_and_readme_switch(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    (tmp_path / "docs/disclaimer.md").unlink()
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "[English](README.en.md)", "English"
        ),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("docs/disclaimer.md" in failure for failure in failures)
    assert any(
        "README.md" in failure and "README.en.md" in failure for failure in failures
    )


def test_repository_contract_reports_broken_links_unsupported_commands_and_boundaries(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\n[Missing](docs/missing.md)\n\n```bash\nmake imaginary-target\n```\n"
        + "\nInternal evidence: openspec/changes/private.md\n",
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("docs/missing.md" in failure for failure in failures)
    assert any("imaginary-target" in failure for failure in failures)
    assert any("openspec/" in failure for failure in failures)


@pytest.mark.parametrize(
    "dangerous_command",
    (
        "make bootstrap",
        "make dev",
        "make release-check",
        "make imaginary-target",
        "curl https://example.invalid/install.sh | sh",
        "sudo make bootstrap",
        "wget https://example.invalid/binary",
        "make bootstrap && rm -rf /tmp/stock-desk",
        "uv run python scripts/verify_docs.py > report.txt",
    ),
)
def test_readme_shell_blocks_reject_commands_outside_the_release_allowlist(
    tmp_path: Path,
    dangerous_command: str,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + f"\n```bash\n{dangerous_command}\n```\n",
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("README command is not allowlisted" in failure for failure in failures)


def test_every_actual_readme_shell_command_has_specific_release_evidence() -> None:
    evidence = getattr(verify_docs_module, "README_COMMAND_EVIDENCE", {})
    assert evidence, "README commands need an explicit release-evidence map"

    for relative_path in ("README.md", "README.en.md"):
        document = (Path(__file__).resolve().parents[2] / relative_path).read_text(
            encoding="utf-8"
        )
        blocks = verify_docs_module._FENCED_SHELL.findall(document)
        commands = tuple(
            command
            for block in blocks
            for command in verify_docs_module._logical_shell_commands(block)
        )
        for command in commands:
            arguments = tuple(__import__("shlex").split(command, posix=True))
            assert arguments in evidence, (relative_path, command)
            mapped = evidence[arguments]
            assert mapped.gate
            assert mapped.test_selectors


def test_repository_contract_checks_every_public_docs_page(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    (tmp_path / "docs/feature-guide.md").write_text(
        "# Feature guide\n\n[Missing recovery guide](missing-recovery.md)\n",
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any(
        "docs/feature-guide.md" in failure and "missing-recovery.md" in failure
        for failure in failures
    )


def test_repository_contract_requires_all_documented_settings(tmp_path: Path) -> None:
    _write_repository(tmp_path)
    configuration = tmp_path / "docs/configuration.md"
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "`STOCK_DESK_MASTER_KEY`, ", ""
        ),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("STOCK_DESK_MASTER_KEY" in failure for failure in failures)


def test_repository_contract_requires_source_free_installers_before_source_setup(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "stock-desk-<version>-macos-arm64.dmg", "macOS installer"
        ),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("source-free installer" in failure for failure in failures)


def test_repository_contract_requires_native_topology_and_attestation_guidance(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    removals = {
        "docs/architecture.md": ("Native installer topology",),
        "docs/configuration.md": (
            "Native installers",
            "%LOCALAPPDATA%\\stock-desk",
            "~/Library/Application Support/stock-desk",
            "config/master.key",
        ),
    }
    for relative_path, snippets in removals.items():
        path = tmp_path / relative_path
        document = path.read_text(encoding="utf-8")
        for snippet in snippets:
            document = document.replace(snippet, "removed")
        path.write_text(document, encoding="utf-8")

    failures = verify_repository(tmp_path)

    for expected in (
        "Native installers",
        "%LOCALAPPDATA%\\stock-desk",
        "~/Library/Application Support/stock-desk",
        "config/master.key",
    ):
        assert any(expected in failure for failure in failures), expected


def test_repository_contract_requires_mode_specific_rollback_and_native_writability(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    removals = {
        "docs/backup-and-restore.md": (
            "Compose image digest",
            "immutable source commit",
            "exact macOS installer artifact",
        ),
        "docs/architecture.md": ("user-writable install location",),
    }
    for relative_path, snippets in removals.items():
        path = tmp_path / relative_path
        document = path.read_text(encoding="utf-8")
        for snippet in snippets:
            document = document.replace(snippet, "removed")
        path.write_text(document, encoding="utf-8")

    failures = verify_repository(tmp_path)

    for expected in (
        "Compose image digest",
        "immutable source commit",
        "exact macOS installer artifact",
        "user-writable install location",
    ):
        assert any(expected in failure for failure in failures), expected


def test_wiki_staging_requires_complete_pairs_and_procedural_sections(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)

    assert verify_wiki(tmp_path, final=False) == []

    (tmp_path / "MACD-Backtest-Tutorial-en.md").unlink()
    formula = tmp_path / "Formula-Studio-Quickstart-en.md"
    formula.write_text(
        formula.read_text(encoding="utf-8").replace("## Recovery", "## Notes"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any("MACD-Backtest-Tutorial-en.md" in failure for failure in failures)
    assert any(
        "Formula-Studio-Quickstart-en.md" in failure and "Recovery" in failure
        for failure in failures
    )


def test_wiki_requires_chinese_default_and_english_suffix(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    assert verify_docs_module.REQUIRED_WIKI_PAGE_STEMS == EXPECTED_WIKI_PAGE_STEMS
    assert verify_wiki(tmp_path, final=False) == []

    home = tmp_path / "Home.md"
    home.write_text(
        home.read_text(encoding="utf-8").replace(
            "[English](Home-en)", "[English](Home)"
        ),
        encoding="utf-8",
    )
    english = tmp_path / "Market-Charts-en.md"
    english.write_text(
        english.read_text(encoding="utf-8").replace(
            "[简体中文](Market-Charts)", "[简体中文](Market-Charts-en)"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any("Home.md" in failure and "Home-en" in failure for failure in failures)
    assert any(
        "Market-Charts-en.md" in failure and "Market-Charts" in failure
        for failure in failures
    )


def test_wiki_inventory_includes_public_governance_and_release_evidence() -> None:
    assert "Project-Governance-and-Release-Evidence" in (
        verify_docs_module.REQUIRED_WIKI_PAGE_STEMS
    )


def test_wiki_requires_shared_navigation_and_entry_files(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    (tmp_path / "_Sidebar-en.md").unlink()
    (tmp_path / "SCREENSHOT-MANIFEST.yml").unlink()

    failures = verify_wiki(tmp_path, final=False)

    assert any("_Sidebar-en.md" in failure for failure in failures)
    assert any("SCREENSHOT-MANIFEST.yml" in failure for failure in failures)


def test_final_wiki_feature_index_covers_active_requirements(tmp_path: Path) -> None:
    _write_wiki(tmp_path)

    failures = verify_wiki(tmp_path, final=True)

    assert not [item for item in failures if "feature index" in item.casefold()]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        (
            lambda document: document.replace(
                "| R-079 | [\u4e2d\u6587\u9996\u9875]",
                "| R-080 | [\u4e2d\u6587\u9996\u9875]",
            ),
            "missing requirement ID: R-079",
        ),
        (
            lambda document: document.replace(
                "[\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb)",
                "[\u4e2d\u6587\u9996\u9875](Missing#\u4ece\u8fd9\u91cc\u5f00\u59cb)",
                1,
            ),
            "referenced page does not exist: Missing.md",
        ),
        (
            lambda document: document.replace(
                "Home#\u4ece\u8fd9\u91cc\u5f00\u59cb", "Home#\u4e0d\u5b58\u5728", 1
            ),
            "referenced section does not exist",
        ),
        (
            lambda document: document.replace("`planned-home`", "`missing-shot`", 1),
            "missing screenshot reference: missing-shot",
        ),
    ),
)
def test_wiki_feature_index_rejects_incomplete_or_dangling_rows(
    tmp_path: Path,
    mutation: object,
    expected: str,
) -> None:
    _write_wiki(tmp_path)
    index = tmp_path / "Feature-Index.md"
    mutate = mutation
    assert callable(mutate)
    index.write_text(mutate(index.read_text(encoding="utf-8")), encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "feature index" in item.casefold() and expected in item for item in failures
    )


def test_screenshot_manifest_allows_honest_staging_but_blocks_final(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)

    staging_failures = verify_wiki(tmp_path, final=False)
    final_failures = verify_wiki(tmp_path, final=True)

    assert not [
        item for item in staging_failures if "screenshot manifest" in item.casefold()
    ]
    assert any(
        "screenshot manifest" in item.casefold() and "pending" in item.casefold()
        for item in final_failures
    )


def test_wiki_feature_index_requires_screenshot_to_cover_mapped_requirement(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(", R-079]", "]"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "R-079" in item
        and "planned-home" in item
        for item in failures
    )


def test_wiki_manifest_features_exactly_match_feature_index(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        index = tmp_path / filename
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "| R-079 | [\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb) | "
                "[English home](Home-en#start-here) | \u4ece\u8fd9\u91cc\u5f00\u59cb / Start here | "
                "`planned-home` | `app-route:/market` |",
                "| R-079 | [\u4e2d\u6587\u9996\u9875](Home#\u4ece\u8fd9\u91cc\u5f00\u59cb) | "
                "[English home](Home-en#start-here) | \u4ece\u8fd9\u91cc\u5f00\u59cb / Start here | "
                "`second-shot` | `app-route:/market` |",
            ),
            encoding="utf-8",
        )
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + """  - screenshot_id: second-shot
    path: images/second-shot.png
    page_pairs: [Home.md, Home-en.md]
    caption_locales: {zh-CN: \u7b2c\u4e8c\u5f20\u622a\u56fe, en: Second screenshot}
    features: [R-079]
    surface: {type: app-route, locator: /market}
    state: pending
    viewport: null
    product: null
    captured_at: null
    sha256: null
    market_data: null
    capture: null
    editing: null
    redaction: pending
    disclaimer: \u4ec5\u4f5c\u529f\u80fd\u6f14\u793a\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae
""",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "features do not exactly match Feature index" in item
        for item in failures
    )


def test_wiki_typed_surface_supports_app_and_non_app_evidence(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    for filename in ("Feature-Index.md", "Feature-Index-en.md"):
        index = tmp_path / filename
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "`app-route:/market`", "`wiki-page:Home`"
            ),
            encoding="utf-8",
        )
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "    surface:\n      type: app-route\n      locator: /market",
            "    surface:\n      type: wiki-page\n      locator: Home",
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert not [item for item in failures if "surface" in item.casefold()]


def test_application_routes_use_shared_json_as_the_single_source_of_truth() -> None:
    repo = Path(__file__).resolve().parents[2]
    contract_path = repo / "web/src/app/route-paths.json"
    assert contract_path.is_file()
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert isinstance(contract, dict)
    source = (repo / "web/src/app/routes.ts").read_text(encoding="utf-8")
    assert "./route-paths.json" in source
    assert verify_docs_module._canonical_app_routes() == frozenset(contract.values())
    for key in contract:
        assert source.count(f"routePaths.{key}") == 1
    assert "/comment-only-route" not in verify_docs_module._canonical_app_routes()


def test_wiki_feature_index_rejects_every_unparsed_table_body_row(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    index = tmp_path / "Feature-Index.md"
    index.write_text(
        index.read_text(encoding="utf-8") + "\n| R-080 | malformed | row |\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index feature-index.md" in item.casefold()
        and "unparseable table row" in item
        and "R-080" in item
        for item in failures
    )


def test_final_wiki_requires_every_publication_raster_in_captured_manifest(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, _png_bytes(640, 360, varied=True))
    for filename in ("Home.md", "Home-en.md"):
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n![Home evidence](images/planned-home.png)\n",
            encoding="utf-8",
        )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "images/MACD-Backtest-Tutorial.png" in item
        and "exactly one valid captured manifest entry" in item
        for item in failures
    )


def test_final_wiki_rejects_rogue_article_raster_reference(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, _png_bytes(640, 360, varied=True))
    rogue = tmp_path / "images" / "rogue.png"
    rogue.write_bytes(_png_bytes(640, 360, varied=True))
    page = tmp_path / "Market-Charts.md"
    page.write_text(
        page.read_text(encoding="utf-8") + "\n![Rogue evidence](images/rogue.png)\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.md" in item
        and "images/rogue.png" in item
        and "valid captured manifest entry" in item
        for item in failures
    )


def test_final_wiki_rejects_unreferenced_raster_outside_images(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    (tmp_path / "root-rogue.png").write_bytes(_png_bytes(640, 360, varied=True))

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "root-rogue.png" in item and "outside Wiki images" in item for item in failures
    )


def test_final_wiki_rejects_cross_page_pair_image_reference(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    payload = _png_bytes(640, 360, varied=True)
    _mark_planned_home_captured(tmp_path, payload)
    for filename in ("Home.md", "Home-en.md"):
        page = tmp_path / filename
        page.write_text(
            page.read_text(encoding="utf-8")
            + "\n![Home evidence](images/planned-home.png)\n",
            encoding="utf-8",
        )
    market = tmp_path / "Market-Charts.md"
    market.write_text(
        market.read_text(encoding="utf-8")
        + "\n![Wrong page evidence](images/planned-home.png)\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.md" in item
        and "planned-home.png" in item
        and "not listed in manifest page_pairs" in item
        for item in failures
    )


def test_final_page_screenshot_gate_requires_valid_manifest_entry(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.md" in item and "captured manifest evidence" in item
        for item in failures
    )


def test_wiki_feature_routes_come_from_the_application_route_contract(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    index = tmp_path / "Feature-Index.md"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "`app-route:/market`", "`app-route:/health`", 1
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "/health" in item
        and "canonical application route" in item
        for item in failures
    )


def test_repository_audit_supports_distinct_ssh_identity_policy_surface() -> None:
    assert (
        verify_docs_module._surface_failure(
            ("repository-audit", "ssh-identity-policy"),
            verify_docs_module._canonical_app_routes(),
        )
        is None
    )


def test_wiki_rejects_private_ssh_material_and_machine_paths(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    page = tmp_path / "Project-Governance-and-Release-Evidence.md"
    ssh_directory = "~/.ssh/"
    key_family = "id_" + "ed25519"
    private_key_path = ssh_directory + key_family + "_github"
    private_key_marker = "BEGIN " + "OPENSSH PRIVATE KEY"
    private_key_header = "-----" + private_key_marker + "-----"
    page.write_text(
        page.read_text(encoding="utf-8")
        + f"\n{private_key_path}\n{private_key_header}\n",
        encoding="utf-8",
    )
    written = page.read_text(encoding="utf-8")
    assert private_key_path in written
    assert private_key_header in written

    failures = verify_wiki(tmp_path, final=False)

    for blocked in (ssh_directory, key_family, private_key_marker):
        assert any(blocked in item for item in failures)


def test_wiki_feature_index_section_column_is_bilingual_and_anchor_bound(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    english = tmp_path / "Feature-Index-en.md"
    english.write_text(
        english.read_text(encoding="utf-8").replace(
            "\u4ece\u8fd9\u91cc\u5f00\u59cb / Start here",
            "\u4ece\u8fd9\u91cc\u5f00\u59cb / Wrong section",
            1,
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Feature index" in item
        and "section" in item.casefold()
        and "Wrong section" in item
        for item in failures
    )


def test_wiki_feature_route_must_match_screenshot_manifest(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "      locator: /market", "      locator: /settings", 1
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "planned-home" in item
        and "surface does not match" in item
        for item in failures
    )


def test_wiki_manifest_rejects_image_path_escape_and_symlink(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "images/planned-home.png", "images/../escape.png"
        ),
        encoding="utf-8",
    )

    traversal_failures = verify_wiki(tmp_path, final=False)

    assert any(
        "screenshot manifest" in item.casefold() and "escapes Wiki images" in item
        for item in traversal_failures
    )

    _write_wiki(tmp_path)
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(_png_bytes(640, 360, varied=True))
    images = tmp_path / "images"
    images.mkdir()
    (images / "planned-home.png").symlink_to(outside)

    symlink_failures = verify_wiki(tmp_path, final=False)

    assert any(
        "screenshot manifest" in item.casefold() and "symlink" in item.casefold()
        for item in symlink_failures
    )


def test_captured_wiki_manifest_rejects_arbitrary_image_bytes(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, b"not a raster image")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "decode" in item.casefold()
        for item in failures
    )


def test_final_wiki_manifest_rejects_fictional_page_pair(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[Home.md, Home-en.md]", "[Missing.md, Missing-en.md]"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "page_pairs page does not exist" in item
        for item in failures
    )


def test_wiki_manifest_page_pair_matches_feature_targets(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    manifest = tmp_path / "SCREENSHOT-MANIFEST.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[Home.md, Home-en.md]", "[Market-Charts.md, Market-Charts-en.md]"
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "feature index" in item.casefold()
        and "planned-home" in item
        and "page_pairs do not match" in item
        for item in failures
    )


def test_captured_wiki_manifest_requires_article_image_references(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _mark_planned_home_captured(tmp_path, _png_bytes(640, 360, varied=True))

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "screenshot manifest planned-home" in item.casefold()
        and "Home.md" in item
        and "must reference images/planned-home.png" in item
        for item in failures
    )


def test_wiki_sidebars_link_to_the_other_language_home(tmp_path: Path) -> None:
    _write_wiki(tmp_path)
    sidebar = tmp_path / "_Sidebar.md"
    sidebar.write_text("[English](Home)\n\n[首页](Home)\n", encoding="utf-8")
    english_sidebar = tmp_path / "_Sidebar-en.md"
    english_sidebar.write_text("[中文](Home-en)\n\n[Home](Home-en)\n", encoding="utf-8")

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "_Sidebar.md" in failure and "Home-en" in failure for failure in failures
    )
    assert any(
        "_Sidebar-en.md" in failure and "简体中文" in failure for failure in failures
    )


def test_final_wiki_sidebars_require_complete_same_language_navigation(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    sidebar = tmp_path / "_Sidebar.md"
    sidebar.write_text(
        sidebar.read_text(encoding="utf-8")
        .replace("- [Data-Sources-and-Tushare](Data-Sources-and-Tushare)\n", "")
        .replace("(Market-Charts)", "(Market-Charts-en)"),
        encoding="utf-8",
    )
    english_sidebar = tmp_path / "_Sidebar-en.md"
    english_sidebar.write_text(
        english_sidebar.read_text(encoding="utf-8")
        .replace("- [Local-TDX-Data](Local-TDX-Data-en)\n", "")
        .replace("(Formula-Studio-Quickstart-en)", "(Formula-Studio-Quickstart)"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    for filename, target in (
        ("_Sidebar.md", "Data-Sources-and-Tushare"),
        ("_Sidebar.md", "Market-Charts-en"),
        ("_Sidebar-en.md", "Local-TDX-Data-en"),
        ("_Sidebar-en.md", "Formula-Studio-Quickstart"),
    ):
        assert any(filename in failure and target in failure for failure in failures)


def test_final_wiki_rejects_legacy_language_aliases_and_replaced_pages(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    (tmp_path / "Market-Charts.zh-CN.md").write_text("# 旧中文别名\n", encoding="utf-8")
    for filename in EXPECTED_REPLACED_WIKI_PAGES:
        (tmp_path / filename).write_text("# Replaced page\n", encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "Market-Charts.zh-CN.md" in failure and "legacy" in failure.casefold()
        for failure in failures
    )
    for filename in EXPECTED_REPLACED_WIKI_PAGES:
        assert any(
            filename in failure and "replaced" in failure.casefold()
            for failure in failures
        )


def test_wiki_cannot_be_marked_final_with_screenshot_placeholders(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)

    failures = verify_wiki(tmp_path, final=True)

    assert any("SCREENSHOT_PLACEHOLDER" in failure for failure in failures)


def test_final_wiki_cli_requires_an_explicit_wiki_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_repository(tmp_path)

    with pytest.raises(SystemExit, match="2"):
        main(["--repo-root", str(tmp_path), "--final-wiki"])

    assert "--final-wiki requires --wiki-root" in capsys.readouterr().err


def test_final_wiki_recursively_scans_checklist_and_nested_markdown(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    (tmp_path / "PUBLISHING-CHECKLIST.md").write_text(
        "# Publishing checklist\n\nStatus: staging\n\nSCREENSHOT_PLACEHOLDER\n",
        encoding="utf-8",
    )
    nested = tmp_path / "guides" / "advanced.md"
    nested.parent.mkdir()
    nested.write_text(
        "# Advanced\n\n[Missing](missing.md)\n\nopenspec/private.md\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "PUBLISHING-CHECKLIST.md" in failure and "placeholder" in failure.lower()
        for failure in failures
    )
    assert any(
        "PUBLISHING-CHECKLIST.md" in failure and "finalized" in failure
        for failure in failures
    )
    assert any(
        "guides/advanced.md" in failure and "missing.md" in failure
        for failure in failures
    )
    assert any(
        "guides/advanced.md" in failure and "openspec/" in failure
        for failure in failures
    )


def test_final_wiki_rejects_symlinks_path_escapes_and_invalid_images(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_wiki(wiki)
    _finalize_wiki(wiki)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not an image")
    (wiki / "images" / "linked.png").symlink_to(outside)
    nested = wiki / "guides.md"
    nested.write_text(
        "# Unsafe\n\n![Escape](../outside.png)\n\n![Symlink](images/linked.png)\n\n![Directory](images/directory.png)\n",
        encoding="utf-8",
    )
    (wiki / "images" / "directory.png").mkdir()
    (wiki / "images" / "invalid.png").write_bytes(b"not a real screenshot")

    failures = verify_wiki(wiki, final=True)

    assert any("guides.md" in failure and "escapes" in failure for failure in failures)
    assert any(
        "images/linked.png" in failure and "symlink" in failure for failure in failures
    )
    assert any(
        "images/invalid.png" in failure and "decode" in failure for failure in failures
    )
    assert any(
        "images/directory.png" in failure and "scanned publication file" in failure
        for failure in failures
    )


def test_final_wiki_rejects_placeholder_and_internal_publishable_path_names(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    png = (tmp_path / "images" / "MACD-Backtest-Tutorial.png").read_bytes()
    (tmp_path / "images" / "SCREENSHOT_PLACEHOLDER.png").write_bytes(png)
    internal = tmp_path / "openspec" / "private.png"
    internal.parent.mkdir()
    internal.write_bytes(png)

    failures = verify_wiki(tmp_path, final=True)

    assert any("images/SCREENSHOT_PLACEHOLDER.png" in failure for failure in failures)
    assert any("openspec/private.png" in failure for failure in failures)


def test_final_wiki_rejects_every_unsupported_regular_path_before_filtering(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    (tmp_path / "attachment.pdf").write_bytes(b"%PDF harmless")
    (tmp_path / "notes.txt").write_text(
        "SCREENSHOT_PLACEHOLDER openspec/private.md",
        encoding="utf-8",
    )
    (tmp_path / "unexpected.yml").write_text("private: true\n", encoding="utf-8")
    git_metadata = tmp_path / ".git" / "ignored.txt"
    git_metadata.parent.mkdir()
    git_metadata.write_text("SCREENSHOT_PLACEHOLDER", encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "attachment.pdf" in failure and "unsupported" in failure for failure in failures
    )
    assert any(
        "notes.txt" in failure and "unsupported" in failure for failure in failures
    )
    assert any(
        "unexpected.yml" in failure and "unsupported" in failure for failure in failures
    )
    assert any(
        "notes.txt" in failure and "placeholder" in failure.lower()
        for failure in failures
    )
    assert any(
        "notes.txt" in failure and "openspec/" in failure for failure in failures
    )
    assert not any(".git/ignored.txt" in failure for failure in failures)


def test_final_wiki_requires_fully_decoded_useful_raster_screenshots(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    image_dir = tmp_path / "images"
    fake = image_dir / "fake.png"
    fake.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-decoded-image")
    tiny = image_dir / "tiny.png"
    tiny.write_bytes(_png_bytes(1, 1, varied=False))
    uniform = image_dir / "uniform.png"
    uniform.write_bytes(_png_bytes(640, 360, varied=False))
    svg = image_dir / "fake.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    page = tmp_path / "MACD-Backtest-Tutorial-en.md"
    document = page.read_text(encoding="utf-8")
    document = document.replace(
        "images/MACD-Backtest-Tutorial-en.png", "images/fake.png"
    )
    document += (
        "\n![Tiny](images/tiny.png)\n"
        "![Uniform](images/uniform.png)\n"
        "![Vector](images/fake.svg)\n"
    )
    page.write_text(document, encoding="utf-8")

    failures = verify_wiki(tmp_path, final=True)

    assert any(
        "images/fake.png" in failure and "decode" in failure for failure in failures
    )
    assert any(
        "images/tiny.png" in failure and "dimensions" in failure for failure in failures
    )
    assert any(
        "images/uniform.png" in failure and "content" in failure for failure in failures
    )
    assert any(
        "images/fake.svg" in failure and "unsupported" in failure
        for failure in failures
    )
    assert any(
        "MACD-Backtest-Tutorial-en.md" in failure and "real screenshot" in failure
        for failure in failures
    )


def test_ast_link_policy_covers_reference_html_autolink_and_nested_parentheses(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    page = tmp_path / "guides" / "rendered-links.md"
    page.parent.mkdir()
    page.write_text(
        """# Rendered links

[Reference][missing-reference]

![Reference image][missing-image]

<a href="../escaped.html">escaped HTML</a>

<img src="images/missing-html.png" alt="missing HTML image">

<ftp://example.com/private>

[Nested](missing_(guide).md)

[missing-reference]: missing_(reference).md
[missing-image]: images/missing_(reference).png
""",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    for target in (
        "missing_(reference).md",
        "images/missing_(reference).png",
        "../escaped.html",
        "images/missing-html.png",
        "ftp://example.com/private",
        "missing_(guide).md",
    ):
        assert any(
            "guides/rendered-links.md" in failure and target in failure
            for failure in failures
        ), target


def test_wiki_targets_must_be_scanned_and_screenshots_must_resolve_under_images(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    png = _png_bytes(640, 360, varied=True)
    (tmp_path / "root.png").write_bytes(png)
    (tmp_path / "notes.txt").write_text("not publishable", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "private.md").write_text("# Private", encoding="utf-8")
    (git_dir / "private.png").write_bytes(png)

    english = tmp_path / "MACD-Backtest-Tutorial-en.md"
    english.write_text(
        english.read_text(encoding="utf-8")
        .replace("images/MACD-Backtest-Tutorial-en.png", "images/../root.png")
        .replace(
            "## Recovery",
            """[Ignored](notes.txt)
[Literal traversal](images/../notes.txt)
[Git metadata](.git/private.md)
![Git image](.git/private.png)

## Recovery""",
        ),
        encoding="utf-8",
    )
    chinese = tmp_path / "MACD-Backtest-Tutorial.md"
    chinese.write_text(
        chinese.read_text(encoding="utf-8")
        .replace("images/MACD-Backtest-Tutorial.png", "images/%2e%2e/root.png")
        .replace(
            "## 恢复方法",
            """[编码穿越](images/%2e%2e/notes.txt)

## 恢复方法""",
        ),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=True)

    for target in (
        "notes.txt",
        "images/../notes.txt",
        "images/%2e%2e/notes.txt",
        ".git/private.md",
        ".git/private.png",
    ):
        assert any(
            target in failure and "scanned publication file" in failure
            for failure in failures
        ), target
    assert any(
        "MACD-Backtest-Tutorial-en.md" in failure and "real screenshot" in failure
        for failure in failures
    )
    assert any(
        "MACD-Backtest-Tutorial.md" in failure and "real screenshot" in failure
        for failure in failures
    )


def test_wiki_backup_commands_require_posix_source_or_container_scope(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    backup = tmp_path / "Backup-Restore-Upgrade-and-Uninstall-en.md"
    backup.write_text(
        backup.read_text(encoding="utf-8")
        + "\n`uv run python scripts/backup.py backup.stockdesk-backup`\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Backup-Restore-Upgrade-and-Uninstall-en.md" in failure
        and "source/container POSIX" in failure
        for failure in failures
    )
