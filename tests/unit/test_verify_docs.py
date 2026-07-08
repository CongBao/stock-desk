from __future__ import annotations

from pathlib import Path
import struct
import zlib

import pytest

from scripts.verify_docs import (
    REQUIRED_WIKI_PAGES,
    main,
    verify_repository,
    verify_wiki,
)


REPOSITORY_DOCUMENTS = {
    "README.md": """# Stock Desk

[简体中文](README.zh-CN.md)

## Quick start

Prefer the source-free `stock-desk-<version>-windows-x86_64.exe`,
`stock-desk-<version>-macos-x86_64.dmg`, or
`stock-desk-<version>-macos-arm64.dmg` installer.

```bash
gh attestation verify INSTALLER --repo CongBao/stock-desk --signer-workflow CongBao/stock-desk/.github/workflows/release.yml
```

```bash
make bootstrap
make dev
```

## Core workflows

Use the task center, market charts, Formula Studio, backtesting, and research.

## Documentation

See [configuration](docs/configuration.md) and the [disclaimer](docs/disclaimer.md).

## Safety and scope

Research only; no live trading.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
""",
    "README.zh-CN.md": """# Stock Desk

[English](README.md)

## 快速启动

优先使用无需源码的 `stock-desk-<version>-windows-x86_64.exe`、
`stock-desk-<version>-macos-x86_64.dmg` 或
`stock-desk-<version>-macos-arm64.dmg` 安装包。

```bash
gh attestation verify INSTALLER --repo CongBao/stock-desk --signer-workflow CongBao/stock-desk/.github/workflows/release.yml
```

```bash
make bootstrap
make dev
```

## 核心工作流

使用任务中心、行情图表、公式工作室、回测和研究功能。

## 文档

参阅[配置](docs/configuration.md)和[免责声明](docs/disclaimer.md)。

## 安全与范围

仅供研究，不连接实盘交易。

## 参与贡献

参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。
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
        "bootstrap:\n\t@true\ndev:\n\t@true\ntest:\n\t@true\n",
        encoding="utf-8",
    )


def _write_wiki(root: Path) -> None:
    for stem in REQUIRED_WIKI_PAGES:
        if stem == "Home":
            english = """# Stock Desk Wiki

[简体中文](Home.zh-CN.md)

## Released features

See the feature guides.
"""
            chinese = """# Stock Desk Wiki

[English](Home.md)

## 已发布功能

参阅功能指南。
"""
        else:
            english = f"""# {stem.replace("-", " ")}

[简体中文]({stem}.zh-CN.md)

<!-- SCREENSHOT_PLACEHOLDER: replace after integrated release-candidate capture -->

## Steps

1. Open Stock Desk.
2. Complete the workflow.

## Expected result

The result is visible.

## Recovery

Return to the task center and retry.
"""
            chinese = f"""# {stem.replace("-", " ")}

[English]({stem}.md)

<!-- SCREENSHOT_PLACEHOLDER: replace after integrated release-candidate capture -->

## 操作步骤

1. 打开 Stock Desk。
2. 完成工作流。

## 预期结果

结果可见。

## 恢复方法

返回任务中心后重试。
"""
        (root / f"{stem}.md").write_text(english, encoding="utf-8")
        (root / f"{stem}.zh-CN.md").write_text(chinese, encoding="utf-8")


def _png_bytes(width: int, height: int, *, varied: bool) -> bytes:
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
            value = (x * 7 + y * 13) % 256 if varied else 128
            rows.extend((value, (value * 3) % 256, (value * 5) % 256))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + chunk(b"IEND", b"")
    )


def _finalize_wiki(root: Path) -> None:
    image_dir = root / "images"
    image_dir.mkdir()
    png = _png_bytes(640, 360, varied=True)
    for stem in REQUIRED_WIKI_PAGES:
        if stem == "Home":
            continue
        for suffix in ("", ".zh-CN"):
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


def test_repository_documentation_contract_passes_for_complete_tree(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)

    assert verify_repository(tmp_path) == []


def test_repository_contract_reports_missing_files_and_readme_switch(
    tmp_path: Path,
) -> None:
    _write_repository(tmp_path)
    (tmp_path / "docs/disclaimer.md").unlink()
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            "[简体中文](README.zh-CN.md)", "简体中文"
        ),
        encoding="utf-8",
    )

    failures = verify_repository(tmp_path)

    assert any("docs/disclaimer.md" in failure for failure in failures)
    assert any(
        "README.md" in failure and "README.zh-CN.md" in failure for failure in failures
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
        "README.md": ("gh attestation verify", "--signer-workflow"),
        "README.zh-CN.md": ("gh attestation verify", "--signer-workflow"),
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
        "gh attestation verify",
        "--signer-workflow",
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

    (tmp_path / "Backtesting.zh-CN.md").unlink()
    formula = tmp_path / "Formula-Studio.md"
    formula.write_text(
        formula.read_text(encoding="utf-8").replace("## Recovery", "## Notes"),
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any("Backtesting.zh-CN.md" in failure for failure in failures)
    assert any(
        "Formula-Studio.md" in failure and "Recovery" in failure for failure in failures
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
        "images/directory.png" in failure and "not a regular image" in failure
        for failure in failures
    )


def test_final_wiki_rejects_placeholder_and_internal_publishable_path_names(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    _finalize_wiki(tmp_path)
    png = (tmp_path / "images" / "Backtesting.png").read_bytes()
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
    page = tmp_path / "Backtesting.md"
    document = page.read_text(encoding="utf-8")
    document = document.replace("images/Backtesting.png", "images/fake.png")
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
        "Backtesting.md" in failure and "real screenshot" in failure
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


def test_wiki_backup_commands_require_posix_source_or_container_scope(
    tmp_path: Path,
) -> None:
    _write_wiki(tmp_path)
    backup = tmp_path / "Backup-and-Restore.md"
    backup.write_text(
        backup.read_text(encoding="utf-8")
        + "\n`uv run python scripts/backup.py backup.stockdesk-backup`\n",
        encoding="utf-8",
    )

    failures = verify_wiki(tmp_path, final=False)

    assert any(
        "Backup-and-Restore.md" in failure and "source/container POSIX" in failure
        for failure in failures
    )
