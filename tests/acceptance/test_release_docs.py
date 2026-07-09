from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from scripts.verify_docs import (
    README_COMMAND_EVIDENCE,
    _FENCED_SHELL,
    _logical_shell_commands,
    _raster_failure,
    verify_repository,
)
from scripts.verify_release import PRE_PUBLISH_EVIDENCE_GATE, _candidate_gates


PROJECT_ROOT = Path(__file__).resolve().parents[2]
README_EN = PROJECT_ROOT / "README.en.md"
README_ZH = PROJECT_ROOT / "README.md"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
FINAL_WIKI_URL = "https://github.com/CongBao/stock-desk/wiki"
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]+\]\((?P<target>[^)]+)\)")


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _native_artifact_patterns() -> tuple[str, ...]:
    matrix = _workflow()["jobs"]["build-installers"]["strategy"]["matrix"]["include"]
    patterns: list[str] = []
    for target in matrix:
        suffix = "exe" if target["os_name"] == "windows" else "dmg"
        patterns.append(
            "stock-desk-<version>-"
            f"{target['os_name']}-{target['architecture']}.{suffix}"
        )
    return tuple(sorted(patterns))


def _readmes() -> tuple[str, str]:
    return (
        README_EN.read_text(encoding="utf-8"),
        README_ZH.read_text(encoding="utf-8"),
    )


def _readme_commands() -> frozenset[tuple[str, ...]]:
    return frozenset(
        tuple(shlex.split(command, posix=True))
        for document in _readmes()
        for block in _FENCED_SHELL.findall(document)
        for command in _logical_shell_commands(block)
    )


def test_bilingual_readme_baseline_contains_verified_installation_and_use() -> None:
    english, chinese = _readmes()
    assert verify_repository(PROJECT_ROOT) == []
    assert english.splitlines()[0] == "[简体中文](README.md)"
    assert chinese.splitlines()[0] == "[English](README.en.md)"
    assert not (PROJECT_ROOT / ("README." + "zh-CN.md")).exists()
    assert _readme_commands() == frozenset()

    for pattern in _native_artifact_patterns():
        assert f"`{pattern}`" in english
        assert f"`{pattern}`" in chinese
    for document in (english, chinese):
        assert "https://github.com/CongBao/stock-desk/releases/latest" in document
        assert "docker compose" not in document
        assert "gh attestation" not in document
        assert "localhost:" not in document
        assert "scripts/verify_docs.py" not in document
        assert "openspec/" not in document.casefold()
        assert "docs/superpowers/" not in document.casefold()
        assert "screenshot_placeholder" not in document.casefold()

    workflow = _workflow()
    jobs = workflow["jobs"]
    assert jobs["verify-windows-installer"]["needs"] == "build-installers"
    assert jobs["verify-macos-installer"]["needs"] == "build-installers"
    assert "verify-windows-installer" in jobs["attest"]["needs"]
    assert "verify-macos-installer" in jobs["attest"]["needs"]
    candidate_gates = _candidate_gates(target_performance=False)
    assert any(gate.command == ("make", "test") for gate in candidate_gates)
    assert PRE_PUBLISH_EVIDENCE_GATE in candidate_gates
    assert (PROJECT_ROOT / "tests/acceptance/test_clean_install.py").is_file()
    assert (PROJECT_ROOT / "tests/acceptance/test_installed_distribution.py").is_file()


def test_readme_commands_map_to_executed_release_evidence() -> None:
    commands = _readme_commands()
    assert commands <= README_COMMAND_EVIDENCE.keys()

    candidate_commands = {
        gate.command for gate in _candidate_gates(target_performance=False)
    }
    workflow = _workflow()
    jobs = workflow["jobs"]
    container_steps = str(jobs["container"]["steps"])
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")

    evidence = {README_COMMAND_EVIDENCE[command] for command in commands}
    for binding in evidence:
        family, _, detail = binding.gate.partition(":")
        assert family in {"candidate", "clean-install", "smoke"}
        if family == "candidate":
            if detail == "verify-docs":
                assert (
                    "uv",
                    "run",
                    "--frozen",
                    "python",
                    "scripts/verify_docs.py",
                ) in candidate_commands
            else:
                assert detail.startswith("make-")
                assert ("make", detail.removeprefix("make-")) in candidate_commands
        elif family == "clean-install":
            assert {"verify-windows-installer", "verify-macos-installer"} <= set(
                jobs["attest"]["needs"]
            )
        else:
            assert "docker compose up --wait --no-build" in container_steps
            assert "make smoke" in container_steps

        for selector in binding.test_selectors:
            relative_path = selector.partition("::")[0]
            assert (PROJECT_ROOT / relative_path).exists(), selector

    assert ("make", "test") in candidate_commands
    test_recipe = makefile.split("test:\n", maxsplit=1)[1].split(
        "\nacceptance:", maxsplit=1
    )[0]
    for required in (
        "tests/acceptance/test_release_docs.py",
        "tests/acceptance/test_release_artifacts.py",
        "tests/acceptance/test_installed_distribution.py",
    ):
        assert f"--ignore={required}" not in test_recipe


def test_readmes_are_concise_reciprocal_and_install_verified() -> None:
    english, chinese = _readmes()
    expected_images = {
        "docs/images/market-data-and-charts.png",
        "docs/images/formula-studio.png",
        "docs/images/backtesting.png",
        "docs/images/multi-agent-research.png",
    }
    for document in (english, chinese):
        assert len(document.splitlines()) <= 100
        assert FINAL_WIKI_URL in document
        image_targets = MARKDOWN_IMAGE.findall(document)
        assert set(image_targets) == expected_images
        for target in image_targets:
            assert "placeholder" not in target.casefold()
            parsed = urlsplit(target)
            assert not parsed.scheme and not parsed.netloc
            screenshot = (PROJECT_ROOT / unquote(parsed.path)).resolve()
            screenshot.relative_to(PROJECT_ROOT.resolve())
            assert screenshot.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
            assert screenshot.is_file()
            assert _raster_failure(screenshot) is None

    assert "[简体中文](README.md)" in english
    assert "[English](README.en.md)" in chinese
    english_sections = (
        "## Product positioning",
        "## Core features",
        "## Download and install",
        "## Documentation",
        "## Safety and scope",
    )
    chinese_sections = (
        "## 产品定位",
        "## 核心功能",
        "## 下载安装",
        "## 使用文档",
        "## 安全与范围",
    )
    for document, sections in (
        (english, english_sections),
        (chinese, chinese_sections),
    ):
        assert [document.index(section) for section in sections] == sorted(
            document.index(section) for section in sections
        )
        core = document.split(sections[1], maxsplit=1)[1].split(
            sections[2], maxsplit=1
        )[0]
        assert len([line for line in core.splitlines() if line.startswith("- ")]) == 4
        installation = document.split(sections[2], maxsplit=1)[1].split(
            sections[3], maxsplit=1
        )[0]
        assert all(f"{step}. " in installation for step in range(1, 4))
    for pattern in _native_artifact_patterns():
        assert pattern in english
        assert pattern in chinese
