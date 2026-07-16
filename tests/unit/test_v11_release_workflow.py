from __future__ import annotations

from pathlib import Path
import re

import yaml  # type: ignore[import-untyped]

from scripts.main_validation_proof import EVIDENCE_POLICIES


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


def _job_commands(job: dict[str, object]) -> str:
    return "\n".join(
        str(step.get("run", ""))
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


def test_v11_release_preserves_exact_proof_reuse_and_unsigned_publish_jobs() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)

    assert {"tag-policy", "prerelease-verify", "prerelease"} <= set(jobs)
    assert jobs["prerelease-verify"]["needs"] == "tag-policy"
    assert jobs["prerelease"]["needs"] == "prerelease-verify"

    commands = _commands(workflow)
    for required in (
        "main-validation-proof-$GITHUB_SHA",
        "windows-payload-comparison-manifest",
        "windows-desktop-alpha-candidate-$GITHUB_SHA",
        "gh attestation verify",
        "scripts/verify_release.py",
        "UNSIGNED-WINDOWS",
        "--prerelease",
        "--latest=false",
    ):
        assert required in commands


def test_v11_release_consumes_the_complete_main_proof_evidence_set() -> None:
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    commands = _commands(_workflow())
    expected = {policy.artifact_name for policy in EVIDENCE_POLICIES.values()}
    blocks = re.findall(r"evidence_names=\(\n(?P<body>.*?)\n\s*\)", source, re.DOTALL)
    consumed = [
        {line.strip() for line in block.splitlines() if line.strip()}
        for block in blocks
    ]

    # The observer manifest is downloaded, attested, and passed to the offline
    # release verifier.  Keep the explicit cardinality checks synchronized with
    # the twelve-artifact exact-main proof contract.
    assert len(consumed) == 3
    candidate = "windows-desktop-alpha-candidate-manifest"
    assert consumed[0] == expected - {candidate}
    assert consumed[1:] == [expected, expected]
    assert "windows-desktop-alpha-candidate-$GITHUB_SHA" in commands
    assert len(expected) == 12
    assert commands.count("windows-browser-observer-evidence") == 4
    assert commands.count("-eq 12") == 2
    assert (
        'find "$EVIDENCE_ROOT" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 11'
        not in commands
    )
    assert source.count("windows-browser-observer-evidence") == 4
    assert (
        "windows-browser-observer-evidence"
        in commands.split("UNSIGNED-WINDOWS-proved-artifacts-$GITHUB_REF_NAME.tar", 1)[
            1
        ]
    )


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


def test_v11_tag_policy_splits_unsigned_tag_push_from_formal_main_dispatch() -> None:
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
    assert '      - "v1.1.0-alpha.*"' in source
    assert '      - "v1.1.0-beta.*"' in source
    assert "workflow_dispatch:" in source
    assert "release_tag:" in source


def test_v11_exact_stable_tag_publishes_the_proved_candidate_as_unsigned() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    push_tags = triggers["push"]["tags"]

    assert "v1.1.0" in push_tags
    verify_if = " ".join(str(jobs["prerelease-verify"]["if"]).split())
    publish_if = " ".join(str(jobs["prerelease"]["if"]).split())
    assert verify_if == (
        "${{ github.ref_name == 'v1.1.0' || "
        "startsWith(github.ref_name, 'v1.1.0-alpha.') || "
        "startsWith(github.ref_name, 'v1.1.0-beta.') }}"
    )
    assert publish_if == verify_if

    policy = str(jobs["tag-policy"]["steps"][0]["run"])
    verify = _job_commands(jobs["prerelease-verify"])
    publish = _job_commands(jobs["prerelease"])
    assert '"$GITHUB_REF_NAME" = v1.1.0' in policy
    assert 'git cat-file -t "refs/tags/${GITHUB_REF_NAME}"' in verify
    assert 'expected_version="${GITHUB_REF_NAME#v}"' in verify
    assert "windows-desktop-alpha-candidate-$GITHUB_SHA" in verify
    assert "scripts/verify_release.py" in verify
    assert "UNSIGNED-WINDOWS-INSTALLER.txt" in verify
    assert "UNSIGNED-WINDOWS-SHA256SUMS" in publish
    assert 'if test "$GITHUB_REF_NAME" = v1.1.0; then' in publish
    assert 'release_flags=(--latest --title "Stock Desk $GITHUB_REF_NAME' in publish
    assert "--prerelease" in publish
    assert "--latest=false" in publish
    assert publish.count("mapfile -t installers") == 2
    assert "UNSIGNED-TEST-ONLY" not in verify
    assert "UNSIGNED-TEST-ONLY" not in publish
    for forbidden in (
        "make test",
        "pytest",
        "pnpm test",
        "cargo test",
        "scripts.build_windows_desktop",
    ):
        assert forbidden not in verify
        assert forbidden not in publish


def test_v11_unsigned_release_includes_exact_candidate_sbom_and_provenance() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    verify = jobs["prerelease-verify"]
    assert isinstance(verify, dict)
    steps = verify["steps"]
    sbom = next(
        step
        for step in steps
        if step.get("name") == "Generate SPDX SBOM from exact unsigned candidate"
    )
    assert sbom["uses"] == (
        "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610"
    )
    assert sbom["with"] == {
        "path": "${{ env.EVIDENCE_ROOT }}/windows-desktop-alpha-candidate-manifest",
        "format": "spdx-json",
        "output-file": "${{ runner.temp }}/unsigned-windows-candidate.spdx.json",
        "upload-artifact": False,
        "upload-release-assets": False,
    }
    prepare = next(
        step
        for step in steps
        if step.get("name") == "Prepare explicitly unsigned Windows evidence assets"
    )
    commands = str(prepare["run"])
    assert "UNSIGNED-WINDOWS-sbom-$GITHUB_REF_NAME.spdx.json" in commands
    assert "UNSIGNED-WINDOWS-builder-provenance-$GITHUB_REF_NAME.json" in commands
    publish = _job_commands(jobs["prerelease"])
    assert "UNSIGNED-WINDOWS-SHA256SUMS" in publish
    assert "-eq 11" in publish


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


def test_formal_release_is_a_protected_main_dispatch_with_a_closed_signed_dag() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"push", "workflow_dispatch"}
    dispatch = triggers["workflow_dispatch"]
    assert dispatch["inputs"]["release_tag"]["required"] is True

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {
        "tag-policy",
        "prerelease-verify",
        "prerelease",
        "formal-inputs",
        "signpath",
        "windows-installed",
        "trusted-updater-release",
        "stable-readiness",
        "stable-attest",
        "stable-release",
    }
    assert jobs["signpath"]["uses"] == "./.github/workflows/signpath.yml"
    assert jobs["signpath"]["needs"] == "formal-inputs"
    assert jobs["signpath"]["with"]["release_tag"] == "${{ inputs.release_tag }}"
    assert jobs["windows-installed"]["uses"] == (
        "./.github/workflows/windows-installed.yml"
    )
    assert jobs["windows-installed"]["needs"] == ["formal-inputs", "signpath"]
    assert jobs["trusted-updater-release"]["needs"] == [
        "formal-inputs",
        "signpath",
        "windows-installed",
    ]
    assert jobs["trusted-updater-release"]["runs-on"] == "windows-2025"
    assert jobs["trusted-updater-release"]["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && "
        "inputs.release_tag == 'v1.1.0' }}"
    )
    assert workflow["permissions"] == {"contents": "read"}
    for reusable in ("signpath", "windows-installed"):
        assert jobs[reusable]["permissions"] == {
            "actions": "read",
            "attestations": "write",
            "contents": "read",
            "id-token": "write",
        }
        assert jobs[reusable]["secrets"] != "inherit"
    assert set(jobs["signpath"]["secrets"]) == {
        "SIGNPATH_API_TOKEN",
        "SIGNPATH_ORGANIZATION_ID",
        "SIGNPATH_PROJECT_SLUG",
        "SIGNPATH_SIGNING_POLICY_SLUG",
        "SIGNPATH_ARTIFACT_CONFIGURATION_SLUG",
        "SIGNPATH_POLICY_TOKEN",
    }
    assert set(jobs["windows-installed"]["secrets"]) == {
        "WINDOWS_INSTALLED_POLICY_TOKEN",
        "WINDOWS_VM_BROKER_ENDPOINT",
        "WINDOWS_VM_SNAPSHOT_POLICY_SHA256",
        "WINDOWS_VM_ADAPTER_SHA256",
    }
    assert jobs["stable-readiness"]["if"] == "${{ false }}"
    assert jobs["stable-readiness"]["needs"] == [
        "formal-inputs",
        "signpath",
        "windows-installed",
        "trusted-updater-release",
    ]
    assert jobs["stable-attest"]["needs"] == [
        "trusted-updater-release",
        "stable-readiness",
    ]
    assert "needs.stable-readiness.result == 'success'" in jobs["stable-attest"]["if"]
    assert jobs["stable-release"]["needs"] == [
        "formal-inputs",
        "signpath",
        "windows-installed",
        "trusted-updater-release",
        "stable-readiness",
        "stable-attest",
    ]


def test_formal_release_reuses_exact_main_bytes_and_fails_closed() -> None:
    workflow = _workflow()
    commands = _commands(workflow)
    source = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    for required in (
        'test "$GITHUB_REF" = refs/heads/main',
        'test "$GITHUB_REF_PROTECTED" = true',
        'git cat-file -t "refs/tags/$RELEASE_TAG"',
        "refs/remotes/origin/protected-main",
        "main-validation-proof-$SOURCE_SHA",
        "windows-desktop-alpha-candidate-$SOURCE_SHA",
        "scripts/main_validation_proof.py verify",
        "scripts/artifact_manifest.py verify",
        "TAURI_SIGNING_PRIVATE_KEY",
        "scripts/trusted_updater_release.py",
        "latest.json",
        "SHA256SUMS",
        "sbom.spdx.json",
        "provenance.json",
        "gh release create",
        "--verify-tag",
    ):
        assert required in commands
    assert "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610" in source

    for forbidden in (
        "pytest",
        "pnpm test",
        "pnpm e2e",
        "cargo test",
        "cargo build",
        "scripts.build_installer",
        "scripts.build_windows_desktop",
        "continue-on-error",
        "|| true",
    ):
        assert forbidden not in source
    assert "UNSIGNED-TEST-ONLY" not in _job_commands(workflow["jobs"]["stable-release"])


def test_stable_publish_cannot_run_without_every_real_signed_receipt() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    signpath_workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "signpath.yml").read_text(encoding="utf-8")
    )
    assert signpath_workflow["jobs"]["sign"]["if"] == "${{ false }}"
    assert isinstance(jobs, dict)
    verify = _job_commands(jobs["trusted-updater-release"])
    verify_job = str(jobs["trusted-updater-release"])
    assert jobs["trusted-updater-release"]["env"]["NODE_VERSION"] == "24.14.0"
    assert jobs["trusted-updater-release"]["env"]["PNPM_VERSION"] == "11.7.0"
    trusted_steps = jobs["trusted-updater-release"]["steps"]
    setup_node = next(
        step for step in trusted_steps if step.get("name") == "Set up Node.js"
    )
    assert setup_node["with"] == {"node-version": "${{ env.NODE_VERSION }}"}
    pnpm_cache = next(
        step
        for step in trusted_steps
        if step.get("name") == "Restore exact-lock pnpm downloads"
    )
    assert pnpm_cache["uses"] == (
        "actions/cache@27d5ce7f107fe9357f9df03efb73ab90386fccae"
    )
    assert pnpm_cache["with"] == {
        "path": "~/.pnpm-store",
        "key": (
            "trusted-updater-pnpm-${{ runner.os }}-${{ runner.arch }}-"
            "node-${{ env.NODE_VERSION }}-pnpm-${{ env.PNPM_VERSION }}-"
            "${{ hashFiles('pnpm-lock.yaml') }}"
        ),
    }
    assert 'pnpm config set store-dir "$HOME/.pnpm-store"' in verify
    assert "needs.signpath.outputs.signed_artifact_name" in verify_job
    assert "needs.windows-installed.outputs.acceptance_artifact_name" in verify_job
    for required in (
        "signpath-receipt.json",
        "signpath-attestation-bundle.jsonl",
        "windows-10-trust-receipt.json",
        "windows-11-trust-receipt.json",
        "windows-trust-attestation.jsonl",
        "--installer-attestation",
        "--signpath-attestation",
        "--windows-10-attestation",
        "--windows-11-attestation",
    ):
        assert required in verify
    assert "TAURI_SIGNING_PRIVATE_KEY" not in jobs["trusted-updater-release"]["env"]
    signing_step = next(
        step
        for step in jobs["trusted-updater-release"]["steps"]
        if step.get("name")
        == "Create detached Tauri signature and strict stable metadata"
    )
    assert set(signing_step["env"]) == {
        "TAURI_SIGNING_PRIVATE_KEY",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
    }
    assert ".verified.exe" not in verify
    assert (
        'verified="$RUNNER_TEMP/trusted-release/verified/stock-desk-$version-windows-x64-setup.exe"'
        in verify
    )
    assert jobs["stable-release"]["if"] == (
        "${{ inputs.release_tag == 'v1.1.0' && "
        "needs.trusted-updater-release.result == 'success' && "
        "needs.stable-readiness.result == 'success' && "
        "needs.stable-attest.result == 'success' }}"
    )
    publish = _job_commands(jobs["stable-release"])
    assert jobs["stable-release"]["permissions"] == {
        "actions": "read",
        "attestations": "read",
        "contents": "write",
    }
    assert "gh attestation verify" in publish
    assert "stable-assets-attestation.jsonl" in publish
    assert "--signer-workflow" in publish
    assert "--source-ref refs/heads/main" in publish
