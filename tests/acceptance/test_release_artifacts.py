from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tomllib
import tracemalloc

import pytest
import yaml

from scripts import build_installer
from scripts.check_public_tree import forbidden_paths
from scripts.source_fingerprint import compute_source_fingerprint
from scripts.verify_release import (
    ReleaseLeakScanner,
    check_build_artifacts,
    check_public_history,
)
from tests.acceptance.clean_install_harness import (
    CleanInstallResult,
    build_clean_install,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
GENERATED_OR_PRIVATE_PREFIXES = (
    ".agents/",
    ".codex/",
    ".superpowers/",
    ".venv/",
    "coverage/",
    "dist/",
    "docs/superpowers/",
    "htmlcov/",
    "node_modules/",
    "openspec/",
    "outputs/",
    "test-results/",
    "web/dist/",
    "work/",
)


@pytest.fixture(scope="module")
def release_build(tmp_path_factory: pytest.TempPathFactory) -> CleanInstallResult:
    return build_clean_install(
        PROJECT_ROOT,
        tmp_path_factory.mktemp("release-artifact-evidence"),
    )


def _git_bytes(*arguments: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(  # noqa: S603 -- fixed Git binary and local repository
        ("git", "-C", os.fspath(PROJECT_ROOT), *arguments),
        input=input_bytes,
        check=True,
        capture_output=True,
        timeout=120,
    )
    return completed.stdout


def _history_paths() -> tuple[str, ...]:
    output = _git_bytes("log", "--format=", "--name-only", "-z", "HEAD")
    return tuple(os.fsdecode(item) for item in output.split(b"\0") if item)


def _workflow() -> dict[str, object]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _project_version() -> str:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        version = tomllib.load(project_file)["project"]["version"]
    assert isinstance(version, str)
    return version


def test_release_history_contains_only_public_artifacts() -> None:
    tracemalloc.start()
    try:
        check_public_history(PROJECT_ROOT)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak_bytes < 8 * 1024 * 1024

    historical_paths = _history_paths()
    assert forbidden_paths(historical_paths) == []
    leaked_generated_paths = sorted(
        path
        for path in historical_paths
        if path.startswith(GENERATED_OR_PRIVATE_PREFIXES)
        or path in {"coverage.xml", ".coverage"}
    )
    assert leaked_generated_paths == []


def test_source_wheel_and_web_artifacts_match_the_bound_public_revision(
    release_build: CleanInstallResult,
) -> None:
    version = _project_version()
    check_build_artifacts(release_build.source_checkout, version)

    assert (
        release_build.source_revision
        == _git_bytes("rev-parse", "HEAD").decode().strip()
    )
    assert release_build.source_fingerprint == compute_source_fingerprint(PROJECT_ROOT)


@pytest.mark.parametrize(
    ("os_name", "architecture", "suffix"),
    (
        ("windows", "x86_64", ".exe"),
        ("macos", "x86_64", ".dmg"),
        ("macos", "arm64", ".dmg"),
    ),
)
def test_legacy_native_manifest_remains_revision_bound_but_not_v11_reachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    os_name: str,
    architecture: str,
    suffix: str,
) -> None:
    monkeypatch.delenv("STOCK_DESK_WINDOWS_CERTIFICATE_BASE64", raising=False)
    monkeypatch.delenv("STOCK_DESK_MACOS_SIGNING_IDENTITY", raising=False)
    version = _project_version()
    artifact = tmp_path / f"stock-desk-{version}-{os_name}-{architecture}{suffix}"
    artifact.write_bytes(f"native-contract:{os_name}:{architecture}\n".encode())
    source_identity = {
        "source_revision": _git_bytes("rev-parse", "HEAD").decode().strip(),
        "source_fingerprint": compute_source_fingerprint(PROJECT_ROOT),
    }
    provenance: dict[str, object] = {}
    if os_name == "windows":
        provenance["inno_setup"] = {
            "compiler_sha256": "a" * 64,
            "package_sha256": build_installer.INNO_SETUP_PACKAGE_SHA256,
            "version": build_installer.INNO_SETUP_VERSION,
        }
    checksum = build_installer._write_checksum(artifact)
    manifest = tmp_path / f"stock-desk-{version}-{os_name}-{architecture}.json"
    build_installer._write_installer_manifest(
        manifest,
        version=version,
        os_name=os_name,
        architecture=architecture,
        artifact=artifact,
        build_provenance=provenance,
        source_identity=source_identity,
    )

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="ascii") == f"{digest}  {artifact.name}\n"
    assert json.loads(manifest.read_text(encoding="utf-8")) == {
        "architecture": architecture,
        "artifact": artifact.name,
        "build_provenance": provenance,
        "os": os_name,
        "sha256": digest,
        "signed": False,
        **source_identity,
        "version": version,
    }
    for path in (artifact, checksum, manifest):
        scanner = ReleaseLeakScanner(label=path.name)
        with path.open("rb") as payload:
            for chunk in iter(lambda: payload.read(8192), b""):
                scanner.feed(chunk)
        scanner.finish()

    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"tag-policy", "prerelease-verify", "prerelease"}
    rendered_workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "build-installers" not in rendered_workflow
    assert "build_installer.py" not in rendered_workflow
    assert "release-assets/*.dmg" not in rendered_workflow
    assert "stock-desk-*-unsigned-x64-setup.exe" in rendered_workflow
