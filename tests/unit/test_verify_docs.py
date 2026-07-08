from __future__ import annotations

from pathlib import Path

from scripts.verify_docs import (
    REQUIRED_WIKI_PAGES,
    verify_repository,
    verify_wiki,
)


REPOSITORY_DOCUMENTS = {
    "README.md": """# Stock Desk

[简体中文](README.zh-CN.md)

## Quick start

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

## Modules and boundaries

Ports and adapters.

## Data and storage

Local data directory.

## Trust and security

Localhost by default.
""",
    "docs/configuration.md": """# Configuration

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
