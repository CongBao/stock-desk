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


def test_distribution_contract_covers_all_native_release_artifacts() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    build_matrix = jobs["build-installers"]["strategy"]["matrix"]["include"]

    targets = {
        (entry["os_name"], entry["architecture"], entry["runner"])
        for entry in build_matrix
    }
    assert targets == {
        ("windows", "x86_64", "windows-2025"),
        ("macos", "x86_64", "macos-26-intel"),
        ("macos", "arm64", "macos-26"),
    }
    assert all(entry["native"] is True for entry in build_matrix)

    for relative in (
        "packaging/stock-desk.spec",
        "packaging/windows/stock-desk.iss",
        "packaging/macos/entitlements.plist",
        "scripts/build_installer.py",
        "scripts/verify_installed_app.py",
        "tests/fixtures/distribution/v0.5.0.sql",
    ):
        assert (ROOT / relative).is_file(), relative


def test_install_verification_jobs_do_not_checkout_or_expose_development_path() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]

    for job_name in ("verify-windows-installer", "verify-macos-installer"):
        steps = jobs[job_name]["steps"]
        assert not any(
            str(step.get("uses", "")).startswith("actions/checkout@") for step in steps
        )
        combined = json.dumps(steps, sort_keys=True)
        assert "verify_installed_app.py" in combined
        assert "STOCK_DESK_INSTALL_TEST_PATH" in combined
        assert "download-artifact" in combined
        assert "installer-logs" in combined
        assert "playwright" in combined.lower()


def test_windows_install_and_uninstall_prove_exit_codes_and_postconditions() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["verify-windows-installer"]["steps"]
    install = next(
        step
        for step in steps
        if step.get("name") == "Install and verify without development PATH"
    )["run"]
    uninstall = next(
        step
        for step in steps
        if step.get("name") == "Uninstall without deleting user data"
    )["run"]

    assert "$installProcess = Start-Process" in install
    assert "-Wait -PassThru" in install
    assert "$installProcess.ExitCode -ne 0" in install

    assert "$uninstallProcess = Start-Process" in uninstall
    assert "-Wait -PassThru" in uninstall
    assert "$uninstallProcess.ExitCode -ne 0" in uninstall
    assert "$applicationDir = Join-Path" in uninstall
    assert "$command = Join-Path $applicationDir 'stock-desk.exe'" in uninstall
    assert "$uninstaller = Join-Path $applicationDir 'unins000.exe'" in uninstall
    for path, message in (
        ("$command", "application executable remains after uninstall"),
        ("$uninstaller", "uninstaller remains after uninstall"),
        ("$applicationDir", "application directory remains after uninstall"),
    ):
        assert (
            f"if (Test-Path -LiteralPath {path}) {{ throw '{message}' }}" in uninstall
        )
    assert (
        "if (-not (Test-Path -LiteralPath $persistence)) "
        "{ throw 'user data was deleted' }"
    ) in uninstall


def test_release_workflow_generates_checksums_sbom_and_provenance() -> None:
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8").lower()

    assert "sha256" in workflow_text
    assert "sbom" in workflow_text
    assert "actions/attest@" in workflow_text
    assert "actions/attest-build-provenance@" not in workflow_text
    assert "actions/attest-sbom@" not in workflow_text
    assert "signing" in workflow_text or "notar" in workflow_text
    assert "pull_request" not in workflow_text


def test_windows_compiler_is_immutable_verified_and_recorded_in_provenance() -> None:
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    build_script = (ROOT / "scripts" / "build_installer.py").read_text(encoding="utf-8")

    assert "choco install innosetup" not in workflow_text
    assert "is-6_7_3/innosetup-6.7.3.exe" in workflow_text
    assert (
        "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"
        in workflow_text
    )
    assert "Get-FileHash" in workflow_text
    assert "STOCK_DESK_INNO_SETUP_PACKAGE_SHA256" in workflow_text
    assert '"build_provenance"' in build_script
    assert '"compiler_sha256"' in build_script


def test_release_verifies_native_sidecars_and_builds_complete_manifest() -> None:
    workflow = _workflow()
    attest_steps = workflow["jobs"]["attest"]["steps"]
    release_steps = workflow["jobs"]["release"]["steps"]
    native_step = next(
        step
        for step in attest_steps
        if step.get("name") == "Verify native assets and prepare complete checksums"
    )
    upload_index = next(
        index
        for index, step in enumerate(attest_steps)
        if step.get("name") == "Upload attested release assets"
    )
    create_index = next(
        index
        for index, step in enumerate(release_steps)
        if step.get("name") == "Create GitHub release"
    )
    commands = native_step["run"]

    assert "*.exe.sha256" in commands
    assert "*.dmg.sha256" in commands
    assert 'test "$listed" = "$artifact"' in commands
    assert 'sha256sum -c "$sidecar"' in commands
    assert "*.exe" in commands and "*.dmg" in commands
    assert "*.json" in commands and "*.sbom.spdx.json" in commands
    assert "SHA256SUMS.complete" in commands
    assert "! -name '*.sha256'" not in commands
    assert "! -name 'SHA256SUMS'" not in commands
    assert "wc -l < SHA256SUMS.complete" in commands
    assert "sha256sum -c SHA256SUMS.complete" in commands
    assert attest_steps.index(native_step) < upload_index

    complete_step = next(
        step
        for step in release_steps
        if step.get("name") == "Verify complete release asset checksums"
    )
    assert "wc -l < SHA256SUMS.complete" in complete_step["run"]
    assert "sha256sum -c SHA256SUMS.complete" in complete_step["run"]
    assert release_steps.index(complete_step) < create_index


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
