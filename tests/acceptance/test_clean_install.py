from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.acceptance.clean_install_harness import (
    CleanInstallResult,
    assert_bound_source_identity,
    build_clean_install,
)
from tests.acceptance import test_full_user_journey as full_user_journey


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def clean_install(tmp_path_factory: pytest.TempPathFactory) -> CleanInstallResult:
    root = tmp_path_factory.mktemp("clean-install")
    return build_clean_install(PROJECT_ROOT, root)


def test_clean_checkout_builds_installable_python_and_web_artifacts(
    clean_install: CleanInstallResult,
) -> None:
    result = clean_install

    assert result.source_checkout.name == "public-checkout"
    assert result.wheel.name.startswith("stock_desk-")
    assert result.wheel.name.endswith("-py3-none-any.whl")
    assert result.source_archive.name.startswith("stock_desk-")
    assert result.source_archive.name.endswith(".tar.gz")
    assert result.web_entrypoint.name == "index.html"
    assert result.package_name == "stock-desk"
    assert result.import_succeeded is True
    assert result.installed_module_path.is_relative_to(
        result.source_checkout.parent / "installed-runtime"
    )
    assert result.installed_health_status == "ok"
    assert result.web_title == "stock-desk"
    assert len(result.source_revision) == 40
    assert len(result.source_fingerprint) == 64
    manifest = json.loads(result.installer_manifest.read_text(encoding="utf-8"))
    assert manifest["source_revision"] == result.source_revision
    assert manifest["source_fingerprint"] == result.source_fingerprint
    assert result.installer_manifest_bound is True


def test_clean_checkout_excludes_private_and_generated_state(
    clean_install: CleanInstallResult,
) -> None:
    result = clean_install

    excluded = {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "openspec",
        "docs/superpowers",
        "test-results",
    }
    assert excluded.isdisjoint(result.initial_paths)


def test_same_immutable_revision_independently_passes_complete_demo_journey(
    clean_install: CleanInstallResult,
    tmp_path: Path,
) -> None:
    # The fixture proves the wheel starts its own installed API. This separate
    # acceptance journey proves the exact source identity bound to that wheel.
    assert_bound_source_identity(PROJECT_ROOT, clean_install)

    full_user_journey.test_complete_no_network_application_journey(
        tmp_path / "bound-source-revision-journey"
    )
    assert_bound_source_identity(PROJECT_ROOT, clean_install)
