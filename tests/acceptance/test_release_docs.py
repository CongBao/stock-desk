from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import yaml

import scripts.verify_docs as verify_docs_module
from scripts.verify_docs import (
    README_COMMAND_EVIDENCE,
    _FENCED_SHELL,
    _logical_shell_commands,
    _raster_failure,
    verify_repository,
)
from scripts.verify_release import _candidate_gates


PROJECT_ROOT = Path(__file__).resolve().parents[2]
README_EN = PROJECT_ROOT / "README.en.md"
README_ZH = PROJECT_ROOT / "README.md"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
FINAL_WIKI_URL = "https://github.com/CongBao/stock-desk/wiki"
MARKDOWN_IMAGE = re.compile(r"!\[[^\]]+\]\((?P<target>[^)]+)\)")
README_SCREENSHOT_MANIFEST = PROJECT_ROOT / "docs/images/manifest.yml"
FINAL_AUDIT = PROJECT_ROOT / "docs/releases/v1.0.0-final-audit.md"
REQUIREMENTS_MATRIX = PROJECT_ROOT / "tests/acceptance/requirements.yml"


def _copy_repository_for_docs_acceptance(tmp_path: Path) -> Path:
    destination = tmp_path / "stock-desk"
    destination.mkdir()
    tracked = {
        value.decode()
        for value in subprocess.check_output(
            ("git", "ls-files", "-z"), cwd=PROJECT_ROOT
        ).split(b"\0")
        if value
    }
    for relative_path in sorted(tracked | {"docs/images/manifest.yml"}):
        source = PROJECT_ROOT / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)

    subprocess.run(("git", "init", "-q", str(destination)), check=True)
    object_directory = subprocess.check_output(
        ("git", "rev-parse", "--git-path", "objects"),
        cwd=PROJECT_ROOT,
        text=True,
    ).strip()
    alternates = destination / ".git/objects/info/alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(
        f"{Path(object_directory).resolve()}\n",
        encoding="utf-8",
    )
    commit = subprocess.check_output(
        ("git", "rev-parse", "HEAD"), cwd=PROJECT_ROOT, text=True
    ).strip()
    subprocess.run(
        ("git", "-C", str(destination), "update-ref", "refs/heads/main", commit),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(destination),
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
        ),
        check=True,
    )
    return destination


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _native_artifact_patterns() -> tuple[str, ...]:
    return ("stock-desk-1.1.0-beta.2-unsigned-x64-setup.exe",)


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
    verify_steps = jobs["verify"]["steps"]
    assert any(
        step.get("name") == "Verify main validation proof identity and inputs"
        for step in verify_steps
    )
    ci = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    assert "validation-proof" in ci["jobs"]
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
    ci = yaml.safe_load(
        (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    container_steps = str(ci["jobs"]["container-compose"]["steps"])
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


def test_readme_screenshot_manifest_is_valid_and_binds_each_image_once() -> None:
    assert verify_repository(PROJECT_ROOT) == []
    loaded = yaml.safe_load(README_SCREENSHOT_MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert loaded["schema_version"] == "stock-desk-documentation-screenshots-v1"
    entries = loaded["screenshots"]
    assert isinstance(entries, list)
    manifest_paths = [entry["path"] for entry in entries]

    for document in _readmes():
        readme_paths = MARKDOWN_IMAGE.findall(document)
        assert Counter(readme_paths) == Counter(manifest_paths)
        assert all(count == 1 for count in Counter(readme_paths).values())

    by_path = {entry["path"]: entry for entry in entries}
    assert by_path["docs/images/market-data-and-charts.png"]["state"] == "real_chart"
    assert by_path["docs/images/formula-studio.png"]["state"] == "real_formula_preview"
    assert (
        by_path["docs/images/backtesting.png"]["state"]
        == "blocked_real_backtest_preflight"
    )
    analysis = by_path["docs/images/multi-agent-research.png"]
    assert analysis["state"] == "analysis_readiness"
    assert analysis["market_data"] is None


def test_acceptance_repository_copy_contains_only_controlled_paths(
    tmp_path: Path,
) -> None:
    repository = _copy_repository_for_docs_acceptance(tmp_path)
    tracked_output = subprocess.check_output(
        ("git", "ls-files", "-z"), cwd=PROJECT_ROOT
    )
    expected = {value.decode() for value in tracked_output.split(b"\0") if value} | {
        "docs/images/manifest.yml"
    }
    actual = {
        path.relative_to(repository).as_posix()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repository).parts
    }

    assert actual == expected
    assert not (repository / ".hypothesis").exists()
    assert not (repository / ".env").exists()


def test_copied_repository_cli_rejects_invalid_manifest(tmp_path: Path) -> None:
    repository = _copy_repository_for_docs_acceptance(tmp_path)
    manifest_path = repository / "docs/images/manifest.yml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["screenshots"][0]["market_data"]["is_real"] = False
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    result = subprocess.run(
        (
            sys.executable,
            str(repository / "scripts/verify_docs.py"),
            "--repo-root",
            str(repository),
        ),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "market_data keys must match exactly" in result.stderr


def test_english_readme_places_natural_and_chinese_disclaimers_together() -> None:
    english = README_EN.read_text(encoding="utf-8")
    for image_path in (
        "docs/images/market-data-and-charts.png",
        "docs/images/formula-studio.png",
        "docs/images/backtesting.png",
    ):
        context = verify_docs_module._readme_local_image_context(english, image_path)
        assert "For feature demonstration only; not investment advice." in context
        assert "（仅作功能演示，不构成投资建议。）" in context


@pytest.mark.parametrize(
    "tamper",
    (
        "appended_successful_backtest",
        "appended_synonym_en",
        "appended_synonym_zh",
        "wrong_market_identity",
        "demo",
        "cc0",
    ),
)
def test_copied_repository_rejects_truthfulness_and_provenance_tampering(
    tmp_path: Path, tamper: str
) -> None:
    repository = _copy_repository_for_docs_acceptance(tmp_path)
    if tamper.startswith("appended_"):
        readme_name = "README.md" if tamper == "appended_synonym_zh" else "README.en.md"
        readme = repository / readme_name
        truthful = (
            "不代表回测成功、结果或胜率"
            if tamper == "appended_synonym_zh"
            else "this is not a successful backtest, result, or win rate"
        )
        appended = {
            "appended_successful_backtest": (
                "this is not a successful backtest, result, or win rate; "
                "however, this is a successful backtest result with a 99% win rate"
            ),
            "appended_synonym_en": (
                "this is not a successful backtest, result, or win rate; "
                "the backtest succeeded and achieved a 99% win rate"
            ),
            "appended_synonym_zh": (
                "不代表回测成功、结果或胜率；该回测已经成功，胜率为 99%"
            ),
        }[tamper]
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                truthful,
                appended,
                1,
            ),
            encoding="utf-8",
        )
    else:
        manifest_path = repository / "docs/images/manifest.yml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        market_data = manifest["screenshots"][0]["market_data"]
        if tamper == "wrong_market_identity":
            market_data["symbol"] = "600000.SH"
            market_data["name"] = "浦发银行"
        else:
            marker = (
                "independent DEMO dataset"
                if tamper == "demo"
                else "independent cC0 data"
            )
            market_data["name"] = marker
        manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    failures = verify_repository(repository)

    assert failures
    if tamper.startswith("appended_"):
        assert any(
            "contradictory claim" in item or "exact local caption contract" in item
            for item in failures
        )
    elif tamper == "wrong_market_identity":
        assert any("stable market-data identity" in item for item in failures)
    else:
        assert any("requires real market provenance" in item for item in failures)


def test_wiki_publication_contract_uses_chinese_default_paths() -> None:
    stems = verify_docs_module.REQUIRED_WIKI_PAGE_STEMS

    assert stems[:5] == (
        "Home",
        "Feature-Index",
        "Windows-Installation",
        "macOS-Installation",
        "First-Launch-and-Health",
    )
    assert "Market-Charts" in stems
    assert "Formula-Studio-Quickstart" in stems
    assert "MACD-Backtest-Tutorial" in stems
    assert "Responsive-Navigation-and-Accessibility" in stems
    assert not hasattr(verify_docs_module, "REQUIRED_WIKI_PAGES")


def test_v1_final_audit_binds_every_completed_manual_procedure() -> None:
    matrix = yaml.safe_load(REQUIREMENTS_MATRIX.read_text(encoding="utf-8"))
    audit = FINAL_AUDIT.read_text(encoding="utf-8")
    manual = [
        evidence
        for requirement in matrix["requirements"]
        for evidence in requirement["evidence"]
        if evidence["state"] == "manual"
        and evidence["required_by_gate"] == "final-release-audit"
    ]

    assert len(manual) == 15
    assert all(evidence["completed"] is True for evidence in manual)
    assert all(evidence["procedure_id"] in audit for evidence in manual)
    assert "Main CI #126" in audit
    assert "Release #27" in audit
    assert "f980697bc876a57d1d1fe91483ecbc89e0c656d4" in audit
    assert "signed: false" in audit
