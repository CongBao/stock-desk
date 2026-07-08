from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import yaml

from scripts.verify_docs import _raster_failure, verify_repository
from scripts.verify_release import PRE_PUBLISH_EVIDENCE_GATE, _candidate_gates


PROJECT_ROOT = Path(__file__).resolve().parents[2]
README_EN = PROJECT_ROOT / "README.md"
README_ZH = PROJECT_ROOT / "README.zh-CN.md"
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


def test_bilingual_readme_baseline_contains_verified_installation_and_use() -> None:
    english, chinese = _readmes()
    assert verify_repository(PROJECT_ROOT) == []
    assert english.splitlines()[0] == "[简体中文](README.zh-CN.md)"
    assert chinese.splitlines()[0] == "[English](README.md)"

    for pattern in _native_artifact_patterns():
        assert f"`{pattern}`" in english
        assert f"`{pattern}`" in chinese
    for document in (english, chinese):
        assert "docker compose up --build --wait" in document
        assert "docker compose down --volumes --remove-orphans" in document
        assert "gh attestation verify INSTALLER_PATH" in document
        assert (
            "--signer-workflow CongBao/stock-desk/.github/workflows/release.yml"
            in document
        )
        assert "http://localhost:8000/market" in document
        assert "http://localhost:5173/market" in document
        assert "scripts/verify_docs.py" in document
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "planned final-RC evidence: remove this marker only after the public Wiki "
        "and real release-candidate screenshots exist"
    ),
)
def test_readmes_are_concise_reciprocal_and_install_verified() -> None:
    english, chinese = _readmes()
    for document in (english, chinese):
        assert len(document.splitlines()) <= 120
        assert FINAL_WIKI_URL in document
        image_targets = MARKDOWN_IMAGE.findall(document)
        assert image_targets
        for target in image_targets:
            assert "placeholder" not in target.casefold()
            parsed = urlsplit(target)
            assert not parsed.scheme and not parsed.netloc
            screenshot = (PROJECT_ROOT / unquote(parsed.path)).resolve()
            screenshot.relative_to(PROJECT_ROOT.resolve())
            assert screenshot.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
            assert screenshot.is_file()
            assert _raster_failure(screenshot) is None

    assert "[简体中文](README.zh-CN.md)" in english
    assert "[English](README.md)" in chinese
    for pattern in _native_artifact_patterns():
        assert pattern in english
        assert pattern in chinese
