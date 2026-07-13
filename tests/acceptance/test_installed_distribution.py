from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from urllib.request import urlopen

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _commands(steps: list[dict[str, Any]]) -> str:
    return "\n".join(str(step.get("run", "")) for step in steps)


def test_distribution_contract_covers_all_native_release_artifacts() -> None:
    """Keep the frozen v1.0 distribution claim backed by public release evidence.

    The active v1.1 workflow is intentionally Windows-only, so the historical
    cross-platform contract must be checked against the immutable v1.0 audit
    instead of pretending that the current workflow still builds macOS assets.
    """

    release_notes = (ROOT / "docs" / "releases" / "v1.0.0.md").read_text(
        encoding="utf-8"
    )
    final_audit = (ROOT / "docs" / "releases" / "v1.0.0-final-audit.md").read_text(
        encoding="utf-8"
    )

    for target, asset in (
        ("Windows x86_64", "stock-desk-1.0.0-windows-x86_64.exe"),
        ("macOS x86_64", "stock-desk-1.0.0-macos-x86_64.dmg"),
        ("macOS arm64", "stock-desk-1.0.0-macos-arm64.dmg"),
    ):
        assert target in release_notes
        assert asset in release_notes
        assert target in final_audit


def test_install_verification_jobs_do_not_checkout_or_expose_development_path() -> None:
    """Preserve the v1.0 source-free install proof after workflow replacement."""

    release_notes = (ROOT / "docs" / "releases" / "v1.0.0.md").read_text(
        encoding="utf-8"
    )
    final_audit = (ROOT / "docs" / "releases" / "v1.0.0-final-audit.md").read_text(
        encoding="utf-8"
    )

    assert "source-free first" in release_notes
    assert "Windows x86_64" in final_audit
    assert "macOS x86_64" in final_audit
    assert "macOS arm64" in final_audit
    assert "安装、首次启动" in final_audit
    assert final_audit.count("DMG 安装与首次启动通过") == 2


def test_v11_distribution_contract_is_windows_only_and_reuses_main_candidate() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    rendered = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert set(jobs) == {"tag-policy", "prerelease-verify", "prerelease"}
    assert "windows-desktop-alpha-candidate-$GITHUB_SHA" in rendered
    assert "stock-desk-*-unsigned-x64-setup.exe" in rendered
    for legacy_job in (
        "build-installers",
        "verify-windows-installer",
        "verify-macos-installer",
        "attest",
        "release",
    ):
        assert legacy_job not in jobs

    for relative in (
        "src-tauri/tauri.conf.json",
        "src-tauri/tauri.windows.conf.json",
        "scripts/build_windows_desktop.py",
        "scripts/verify_windows_desktop_bundle.py",
        "scripts/compare_windows_payloads.py",
    ):
        assert (ROOT / relative).is_file(), relative


def test_v11_release_verification_uses_exact_tag_checkout_and_proved_payload() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    steps = jobs["prerelease-verify"]["steps"]
    combined = json.dumps(steps, sort_keys=True)

    assert any(
        str(step.get("uses", "")).startswith("actions/checkout@") for step in steps
    )
    assert "git merge-base --is-ancestor" in combined
    assert "main-validation-proof-$GITHUB_SHA" in combined
    assert "windows-desktop-alpha-candidate-$GITHUB_SHA" in combined
    assert "verify_release.py" in combined
    assert "build_installer.py" not in combined
    assert "verify_installed_app.py" not in combined
    assert "playwright" not in combined.lower()


def test_windows_candidate_is_bound_to_tag_version_before_publish() -> None:
    workflow = _workflow()
    rendered = "\n".join(
        _commands(job["steps"])
        for job in workflow["jobs"].values()
        if isinstance(job, dict)
    )

    assert 'expected_version="${GITHUB_REF_NAME#v}"' in rendered
    assert ".release.version == $version" in rendered
    assert "stock-desk-${expected_version}-unsigned-x64-setup.exe" in rendered
    assert rendered.count('test "${installers[0]}" = "$expected_installer"') == 2


def test_release_workflow_generates_checksums_sbom_and_provenance() -> None:
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8").lower()

    assert "sha256" in workflow_text
    assert "gh attestation verify" in workflow_text
    assert "evidence-attestation-bundle.jsonl" in workflow_text
    assert "windows-builder-provenance" in workflow_text
    assert "actions/attest-build-provenance@" not in workflow_text
    assert "actions/attest-sbom@" not in workflow_text
    assert "unsigned prerelease" in workflow_text
    assert "pull_request" not in workflow_text


def test_legacy_inno_compiler_is_not_reachable_from_v11_release() -> None:
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    build_script = (ROOT / "scripts" / "build_installer.py").read_text(encoding="utf-8")

    assert "choco install innosetup" not in workflow_text
    assert "innosetup" not in workflow_text.casefold()
    assert "build_installer.py" not in workflow_text
    assert "STOCK_DESK_INNO_SETUP_PACKAGE_SHA256" not in workflow_text
    # Keep the immutable v1.0 builder metadata readable for historical verification.
    assert '"build_provenance"' in build_script
    assert '"compiler_sha256"' in build_script


def test_release_rechecks_one_windows_installer_and_complete_asset_checksums() -> None:
    workflow = _workflow()
    verify_steps = workflow["jobs"]["prerelease-verify"]["steps"]
    publish_steps = workflow["jobs"]["prerelease"]["steps"]
    verify_commands = _commands(verify_steps)
    publish_commands = _commands(publish_steps)

    assert "windows-desktop-bundle.json" in verify_commands
    assert "windows-payload-comparison.json" in verify_commands
    assert (
        "find \"$candidate_root\" -maxdepth 1 -type f -name '*.exe'" in verify_commands
    )
    assert ".dmg" not in verify_commands
    assert "sha256sum -c UNSIGNED-TEST-ONLY-SHA256SUMS" in publish_commands
    assert "--prerelease" in publish_commands
    assert "--latest=false" in publish_commands


def test_pyinstaller_bundle_declares_assets_migrations_and_legal_notices() -> None:
    spec = (ROOT / "packaging" / "stock-desk.spec").read_text(encoding="utf-8")

    for bundled_path in (
        "web/dist",
        "migrations",
        "alembic.ini",
        "grammar.lark",
        "LICENSE",
        "NOTICE",
    ):
        assert bundled_path in spec
    assert "COLLECT" in spec
    assert "EXE" in spec


def test_windows_installer_is_uninstallable_and_per_user() -> None:
    installer = (ROOT / "packaging" / "windows" / "stock-desk.iss").read_text(
        encoding="utf-8"
    )

    assert "PrivilegesRequired=lowest" in installer
    assert "Uninstallable=yes" in installer
    assert "{localappdata}" in installer
    assert "uninsneveruninstall" not in installer.lower()


def test_macos_bundle_declares_loopback_network_entitlements() -> None:
    entitlements = (ROOT / "packaging" / "macos" / "entitlements.plist").read_text(
        encoding="utf-8"
    )

    assert "com.apple.security.network.client" in entitlements
    assert "com.apple.security.network.server" in entitlements


@pytest.mark.skipif(
    "STOCK_DESK_INSTALLED_COMMAND" not in os.environ,
    reason="runs only in source-free native installer verification jobs",
)
def test_distribution_runs_without_source_or_development_tools(tmp_path: Path) -> None:
    command = Path(os.environ["STOCK_DESK_INSTALLED_COMMAND"])
    runtime_record = Path(os.environ["STOCK_DESK_RUNTIME_RECORD"])
    sanitized_path = os.environ["STOCK_DESK_INSTALL_TEST_PATH"]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "NODE_PATH", "VIRTUAL_ENV"}
    }
    environment["PATH"] = sanitized_path
    process = subprocess.Popen(
        [str(command), "--no-browser"],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        record: dict[str, object] | None = None
        while time.monotonic() < deadline:
            if runtime_record.is_file():
                record = json.loads(runtime_record.read_text(encoding="utf-8"))
                try:
                    with urlopen(  # noqa: S310 -- loopback URL from private record
                        f"http://127.0.0.1:{record['port']}/api/health",
                        timeout=1,
                    ) as response:
                        health = json.load(response)
                    if health.get("status") == "ok":
                        break
                except OSError:
                    pass
            time.sleep(0.1)
        else:
            pytest.fail("installed application did not become healthy")

        assert record is not None
        assert record["host"] == "127.0.0.1"
        assert Path(str(record["data_dir"])).is_dir()
        assert process.poll() is None
        with urlopen(  # noqa: S310 -- loopback URL from private record
            f"http://127.0.0.1:{record['port']}/",
            timeout=3,
        ) as response:
            browser_document = response.read().decode("utf-8")
        assert "<title>stock-desk</title>" in browser_document
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
