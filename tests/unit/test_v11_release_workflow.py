from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[2]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> dict[str, object]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _commands(workflow: dict[str, object]) -> str:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return "\n".join(
        str(step.get("run", ""))
        for job in jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


def test_v11_release_has_only_exact_proof_reuse_and_unsigned_publish_jobs() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)

    assert set(jobs) == {"tag-policy", "prerelease-verify", "prerelease"}
    assert jobs["prerelease-verify"]["needs"] == "tag-policy"
    assert jobs["prerelease"]["needs"] == "prerelease-verify"

    commands = _commands(workflow)
    for required in (
        "main-validation-proof-$GITHUB_SHA",
        "windows-payload-comparison-manifest",
        "windows-desktop-alpha-candidate-$GITHUB_SHA",
        "gh attestation verify",
        "scripts/verify_release.py",
        "UNSIGNED-TEST-ONLY",
        "--prerelease",
        "--latest=false",
    ):
        assert required in commands


def test_v11_release_cannot_rebuild_retest_or_publish_legacy_platforms() -> None:
    workflow = _workflow()
    commands = _commands(workflow)
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    for forbidden_job in (
        "verify",
        "build-installers",
        "verify-windows-installer",
        "verify-macos-installer",
        "attest",
        "release",
    ):
        assert forbidden_job not in workflow["jobs"]

    for forbidden in (
        "make build",
        "make test",
        "pytest",
        "pnpm build",
        "pnpm e2e",
        "cargo build",
        "scripts.build_installer",
        "scripts.build_windows_desktop",
        "Inno Setup",
        ".dmg",
        "macos-",
        "macOS",
        "android",
        "linux",
        "arm64",
    ):
        assert forbidden not in commands
        assert forbidden not in source


def test_v11_tag_policy_fails_closed_until_trusted_stable_chain_exists() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    policy = jobs["tag-policy"]
    assert isinstance(policy, dict)
    command = str(policy["steps"][0]["run"])
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert r"^v1\.1\.0-(alpha|beta)\.[1-9][0-9]*$" in command
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" not in command
    assert "v1.1.0-rc" not in command
    assert '      - "v1.1.0"' in source
    assert '      - "v1.1.0-*"' in source


def test_v11_prerelease_asset_selection_is_version_agnostic_and_windows_only() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    commands = _commands(workflow)

    assert "stock-desk-1.1.0-beta.2-unsigned-x64-setup.exe" not in commands
    assert "stock-desk-*-unsigned-x64-setup.exe" in commands
    assert 'expected_version="${GITHUB_REF_NAME#v}"' in commands
    assert ".release.version == $version" in commands
    assert (
        'expected_installer="stock-desk-${expected_version}-unsigned-x64-setup.exe"'
        in commands
    )
    assert 'test "${installers[0]}" = "$expected_installer"' in commands
    assert 'test "${#installers[@]}" -eq 1' in commands
    assert "docs/releases/$GITHUB_REF_NAME.md" in commands
    assert "*.exe" in commands
    assert "*.dmg" not in commands
