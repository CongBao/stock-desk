from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from scripts.main_validation_proof import CRITICAL_INPUTS
from scripts.windows_installed_environment_policy import (
    EnvironmentPolicyError,
    bootstrap_payload,
    verify_environment_policy,
)


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "windows-installed.yml"


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


def _commands(job: dict[str, Any]) -> str:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return "\n".join(
        str(step.get("run", "")) for step in steps if isinstance(step, dict)
    )


def test_installed_workflow_is_manual_read_only_and_exact_sha_only() -> None:
    workflow = _workflow()
    triggers = _triggers(workflow)

    assert set(triggers) == {"workflow_dispatch"}
    dispatch = triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    source_sha = dispatch["inputs"]["source_sha"]
    assert source_sha["required"] is True
    assert source_sha["type"] == "string"
    assert "40" in source_sha["description"] and "SHA" in source_sha["description"]
    assert workflow["permissions"] == {"contents": "read"}

    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "id-token: write" not in source
    assert "attestations: write" not in source
    assert "contents: write" not in source
    assert "pull_request:" not in source
    assert "schedule:" not in source
    assert "workflow_run:" not in source
    assert set(workflow["jobs"]) == {
        "dispatch-guard",
        "preflight",
        "windows-installed",
        "aggregate",
    }
    guard = workflow["jobs"]["dispatch-guard"]
    assert guard["permissions"] == {"actions": "read", "contents": "read"}
    for name in ("preflight", "windows-installed", "aggregate"):
        assert workflow["jobs"][name]["permissions"] == {
            "actions": "read",
            "contents": "read",
        }


def test_dispatch_guard_rejects_non_main_before_any_protected_job() -> None:
    workflow = _workflow()
    guard = workflow["jobs"]["dispatch-guard"]
    assert guard["runs-on"] == "ubuntu-24.04"
    assert guard["environment"] == "windows-installed-acceptance"
    assert guard["env"]["SOURCE_SHA"] == "${{ inputs.source_sha }}"
    assert (
        "windows-installed.yml@refs/heads/main" in guard["env"]["EXPECTED_WORKFLOW_REF"]
    )
    commands = _commands(guard)
    for required in (
        'test "$GITHUB_REF" = "refs/heads/main"',
        'test "$GITHUB_REF_TYPE" = "branch"',
        'test "$GITHUB_REF_NAME" = "main"',
        'test "$GITHUB_REF_PROTECTED" = "true"',
        'test "$GITHUB_SHA" = "$SOURCE_SHA"',
        'test "$GITHUB_WORKFLOW_REF" = "$EXPECTED_WORKFLOW_REF"',
        'test "$GITHUB_WORKFLOW_SHA" = "$SOURCE_SHA"',
        "+refs/heads/main:refs/remotes/origin/protected-main",
        'test "$(git rev-parse refs/remotes/origin/protected-main)" = "$SOURCE_SHA"',
        ".commit.sha == $sha",
        "environments/windows-installed-acceptance",
        "deployment-branch-policies",
        "actions/runners",
        "scripts/windows_installed_environment_policy.py verify",
        '--branch-policies "$branch_policies"',
        '--runners "$runners"',
    ):
        assert required in commands
    policy_step = next(
        step
        for step in guard["steps"]
        if isinstance(step, dict)
        and step.get("name")
        == "Prove exact protected main and environment branch policy"
    )
    assert policy_step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "POLICY_TOKEN": "${{ secrets.WINDOWS_INSTALLED_POLICY_TOKEN }}",
    }
    assert commands.count('GH_TOKEN="$POLICY_TOKEN" gh api') == 3
    checkout = next(
        step
        for step in guard["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ inputs.source_sha }}"

    preflight = workflow["jobs"]["preflight"]
    vm = workflow["jobs"]["windows-installed"]
    assert preflight["needs"] == "dispatch-guard"
    assert vm["needs"] == "preflight"
    assert "self-hosted" not in str(guard["runs-on"])
    assert "self-hosted" not in str(preflight["runs-on"])


def test_policy_token_is_confined_to_github_hosted_exact_main_guard() -> None:
    workflow = _workflow()
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    guard = workflow["jobs"]["dispatch-guard"]

    assert source.count("WINDOWS_INSTALLED_POLICY_TOKEN") == 1
    assert guard["runs-on"] == "ubuntu-24.04"
    assert guard["environment"] == "windows-installed-acceptance"
    assert "needs" not in guard
    for name in ("preflight", "windows-installed", "aggregate"):
        assert "WINDOWS_INSTALLED_POLICY_TOKEN" not in str(workflow["jobs"][name])


def test_github_hosted_preflight_proves_main_candidate_and_digest_identity() -> None:
    workflow = _workflow()
    preflight = workflow["jobs"]["preflight"]
    assert preflight["needs"] == "dispatch-guard"
    assert preflight["runs-on"] == "ubuntu-24.04"
    assert "environment" not in preflight
    commands = _commands(preflight)

    for required in (
        "^[0-9a-f]{40}$",
        "repos/$GITHUB_REPOSITORY/branches/main",
        ".protected == true",
        "+refs/heads/main:refs/remotes/origin/protected-main",
        'test "$(git rev-parse refs/remotes/origin/protected-main)" = "$SOURCE_SHA"',
        ".commit.sha == $sha",
        "head_sha == $sha",
        'head_branch == "main"',
        'event == "push"',
        'conclusion == "success"',
        "main-validation-proof-$SOURCE_SHA",
        "windows-desktop-alpha-candidate-$SOURCE_SHA",
        "proof-attestation-bundle.jsonl",
        "evidence-attestation-bundle.jsonl",
        "gh attestation verify",
        "--source-ref refs/heads/main",
        '--source-digest "$SOURCE_SHA"',
        '--signer-digest "$SOURCE_SHA"',
        "--deny-self-hosted-runners",
        "scripts/main_validation_proof.py verify",
        "verify_post_gh_attestation_binding",
        'proof["validation_evidence"]["windows-desktop-alpha-candidate-manifest"]',
        "scripts/artifact_manifest.py verify",
        "manifest-binding.json",
        "main-proof.json",
        "windows-candidate.json",
        "sha256",
        '"evidence_kind": "observed-windows-vm"',
        '"status": "awaiting-controller"',
        '"wiring_only": True',
        '"main_proof_sha256"',
        '"candidate_sha256"',
        '"webview_installer_sha256"',
        'item["role"] == "webview2-offline-installer"',
        'test "$GITHUB_RUN_ATTEMPT" = "1"',
    ):
        assert required in commands
    assert 'proof["validation_evidence"]["windows-alpha-candidate"]' not in commands

    checkout = next(
        step
        for step in preflight["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ inputs.source_sha }}"
    upload = next(
        step
        for step in preflight["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert (
        upload["with"]["name"]
        == "windows-installed-controller-${{ inputs.source_sha }}"
    )
    assert upload["with"]["if-no-files-found"] == "error"


def test_vm_matrix_is_wiring_only_on_github_hosted_windows() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["windows-installed"]

    assert job["needs"] == "preflight"
    assert job["environment"] == "windows-installed-acceptance"
    assert job["strategy"]["fail-fast"] is False
    matrix = job["strategy"]["matrix"]["include"]
    assert {entry["guest_profile"] for entry in matrix} == {"win10-22h2", "win11"}
    assert all("controller_label" not in entry for entry in matrix)
    assert {(entry["guest_profile"], entry["scenario"]) for entry in matrix} == {
        ("win10-22h2", "webview-preinstalled"),
        ("win10-22h2", "webview-install-failure"),
        ("win11", "webview-absent"),
    }
    assert job["runs-on"] == "windows-2025"

    commands = _commands(job)
    for required in (
        "RUNNER_ENVIRONMENT",
        "github-hosted",
        "Persistent repository runners are forbidden",
        "windows_installed_vm_harness.ps1",
        "RestoreCleanSnapshot",
        "RunInstalledAcceptance",
        "CleanupAndRestoreSnapshot",
        "observed-windows-vm",
        "Controller evidence cannot declare passed",
        "Retry evidence cannot replace first-attempt VM evidence",
        "Observed VM scenario assignment mismatch",
        "Observed VM Actions execution identity mismatch",
        "-Scenario $env:EXPECTED_SCENARIO -ScenarioAttempt 1",
        "-ActionsRunId $env:GITHUB_RUN_ID",
        "controller-digests.sha256",
        "Controller digest payload mismatch",
        "Test-Path -LiteralPath $harness -PathType Leaf",
        "Windows VM controller harness is not implemented on this runner",
    ):
        assert required in commands

    download = next(
        step
        for step in job["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert (
        download["with"]["name"]
        == "windows-installed-controller-${{ inputs.source_sha }}"
    )

    cleanup = next(
        step
        for step in job["steps"]
        if isinstance(step, dict)
        and step.get("name") == "Clean controller state and restore snapshot"
    )
    assert cleanup["if"] == "${{ always() }}"
    upload = next(
        step
        for step in job["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["if-no-files-found"] == "error"


def test_every_job_uses_only_github_hosted_runner_labels() -> None:
    workflow = _workflow()
    allowed = {"ubuntu-24.04", "windows-2025"}

    assert {job["runs-on"] for job in workflow["jobs"].values()} <= allowed
    for job in workflow["jobs"].values():
        assert isinstance(job["runs-on"], str)
        assert "controller_label" not in str(job)
        assert "stock-desk-vm-controller" not in str(job)


def test_aggregate_validates_schema_but_refuses_unverified_raw_observations() -> None:
    workflow = _workflow()
    aggregate = workflow["jobs"]["aggregate"]

    assert aggregate["needs"] == ["preflight", "windows-installed"]
    assert aggregate["runs-on"] == "ubuntu-24.04"
    assert aggregate["permissions"] == {"actions": "read", "contents": "read"}
    assert "needs.windows-installed.result == 'success'" in aggregate["if"]
    commands = _commands(aggregate)
    for required in (
        "scripts/verify_windows_installed_evidence.py",
        'test "${#evidence_files[@]}" -eq 3',
        "--main-proof-sha256",
        "--candidate-sha256",
        "--webview-installer-sha256",
        "--workflow",
        "--run-id",
        "--run-attempt",
        "--job-id-prefix windows-installed",
        '"evidence_kind": "wiring-only-diagnostic"',
        '"status": "raw-observation-verifier-required"',
        '"scenario_evidence"',
        "export {name}={value}",
        "Raw observation verification is not implemented",
        "exit 86",
    ):
        assert required in commands

    downloads = [
        step
        for step in aggregate["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert len(downloads) == 2
    assert downloads[1]["with"]["pattern"] == (
        "windows-installed-observed-*-${{ inputs.source_sha }}"
    )
    assert downloads[1]["with"]["merge-multiple"] is False
    upload = next(
        step
        for step in aggregate["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["name"] == (
        "windows-installed-wiring-diagnostic-${{ inputs.source_sha }}"
    )
    assert upload["with"]["if-no-files-found"] == "error"


def test_wiring_cannot_publish_sign_or_substitute_fixtures_for_vm_evidence() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    lowered = source.casefold()

    assert "wiring only" in lowered
    assert "does not prove" in lowered
    for forbidden in (
        "fixtures/",
        "synthetic evidence",
        "continue-on-error",
        "if-no-files-found: warn",
        "if-no-files-found: ignore",
        "gh release",
        "action-gh-release",
        "signpath",
        "cosign",
        "signtool",
        "exit 0",
        "|| true",
    ):
        assert forbidden not in lowered

    preflight_commands = _commands(_workflow()["jobs"]["preflight"])
    assert "RunInstalledAcceptance" not in preflight_commands
    assert "outcome" not in _commands(_workflow()["jobs"]["windows-installed"])
    assert "derived-verifier-receipt" not in source
    assert "verification-receipt" not in source


def _valid_environment() -> dict[str, object]:
    return {
        "name": "windows-installed-acceptance",
        "url": (
            "https://api.github.com/repos/CongBao/stock-desk/environments/"
            "windows-installed-acceptance"
        ),
        "can_admins_bypass": False,
        "protection_rules": [{"type": "branch_policy"}],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }


def _main_branch_policy() -> dict[str, object]:
    return {
        "total_count": 1,
        "branch_policies": [{"name": "main", "type": "branch"}],
    }


def _zero_runners() -> dict[str, object]:
    return {"total_count": 0, "runners": []}


def test_environment_policy_is_repository_bound_and_fail_closed() -> None:
    valid = _valid_environment()
    policies = _main_branch_policy()
    runners = _zero_runners()
    verify_environment_policy(
        valid,
        branch_policies=policies,
        runners=runners,
        repository="CongBao/stock-desk",
    )

    mutations = (
        {**valid, "name": "other"},
        {
            **valid,
            "url": "https://api.github.com/repos/attacker/fork/environments/windows-installed-acceptance",
        },
        {**valid, "can_admins_bypass": True},
        {**valid, "protection_rules": []},
        {
            **valid,
            "deployment_branch_policy": {
                "protected_branches": True,
                "custom_branch_policies": False,
            },
        },
    )
    for mutation in mutations:
        with pytest.raises(EnvironmentPolicyError):
            verify_environment_policy(
                mutation,
                branch_policies=policies,
                runners=runners,
                repository="CongBao/stock-desk",
            )
    for policy_mutation in (
        {"total_count": 0, "branch_policies": []},
        {
            "total_count": 2,
            "branch_policies": [
                {"name": "main", "type": "branch"},
                {"name": "release/*", "type": "branch"},
            ],
        },
        {"total_count": 1, "branch_policies": [{"name": "*", "type": "branch"}]},
        {"total_count": 1, "branch_policies": [{"name": "main", "type": "tag"}]},
    ):
        with pytest.raises(EnvironmentPolicyError):
            verify_environment_policy(
                valid,
                branch_policies=policy_mutation,
                runners=runners,
                repository="CongBao/stock-desk",
            )

    for runner_mutation in (
        {"total_count": 1, "runners": [{"id": 1, "name": "persistent"}]},
        {"total_count": 1, "runners": []},
        {"total_count": 0, "runners": [{"id": 1}]},
        {"total_count": 0},
        [],
    ):
        with pytest.raises(EnvironmentPolicyError):
            verify_environment_policy(
                valid,
                branch_policies=policies,
                runners=runner_mutation,
                repository="CongBao/stock-desk",
            )


def test_bootstrap_payload_preserves_reviewers_but_forces_protected_main_policy() -> (
    None
):
    existing = _valid_environment()
    existing["protection_rules"] = [
        {"type": "branch_policy"},
        {"type": "wait_timer", "wait_timer": 7},
        {
            "type": "required_reviewers",
            "prevent_self_review": True,
            "reviewers": [{"type": "Team", "reviewer": {"id": 42}}],
        },
    ]
    payload = bootstrap_payload(existing)
    assert payload == {
        "wait_timer": 7,
        "prevent_self_review": True,
        "can_admins_bypass": False,
        "reviewers": [{"type": "Team", "id": 42}],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }
    malformed = _valid_environment()
    malformed["protection_rules"] = [{"type": "required_reviewers", "reviewers": []}]
    with pytest.raises(EnvironmentPolicyError):
        bootstrap_payload(malformed)


def test_raw_evidence_contracts_are_signed_inputs_but_not_active_runner_wiring() -> (
    None
):
    required = {
        "schemas/windows-installed-raw-evidence-v1.schema.json",
        "schemas/windows-vm-snapshot-policy-v1.schema.json",
        "scripts/verify_windows_raw_evidence.py",
        "scripts/windows_installed_guest_harness.ps1",
        "scripts/windows_installed_vm_harness.ps1",
        "tests/windows/windows_browser_observer_integration.ps1",
    }
    assert required <= set(CRITICAL_INPUTS)

    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "runs-on: windows-2025" in workflow
    assert "Persistent repository runners are forbidden" in workflow
    assert "STOCK_DESK_WINDOWS_VM_ADAPTER" not in workflow
    assert "verify_windows_raw_evidence.py" not in workflow


def test_browser_observer_fixture_uses_supported_windows_executable_compiler() -> None:
    integration = (
        ROOT / "tests" / "windows" / "windows_browser_observer_integration.ps1"
    ).read_text(encoding="utf-8")

    assert "Framework64\\v4.0.30319\\csc.exe" in integration
    assert "'/target:winexe'" in integration
    assert "System.Diagnostics.Process.GetCurrentProcess().Id" in integration
    assert "Add-Type -TypeDefinition $fixtureSource -OutputAssembly" not in integration


def test_reference_controller_and_guest_contracts_fail_closed() -> None:
    controller = (ROOT / "scripts" / "windows_installed_vm_harness.ps1").read_text(
        encoding="utf-8"
    )
    guest = (ROOT / "scripts" / "windows_installed_guest_harness.ps1").read_text(
        encoding="utf-8"
    )

    for required in (
        "controller-unavailable-diagnostic",
        "Protected snapshot policy is not externally approved",
        "Protected VM adapter is not externally approved",
        "$maximumJsonBytes = 1MB",
        "$maximumRecordBytes = 8MB",
        "$maximumPackageBytes = 16MB",
        "$maximumPublicTextBytes = 2MB",
        "Assert-NoReparsePath",
        "@arguments *> $privateLogPath",
        "LeaseTtlSeconds = $leaseTtlSeconds",
        "released-after-restore",
        "cancellation watchdog lease",
        "Cleanup restored the snapshot but cannot publish an incomplete acceptance lifecycle",
    ):
        assert required in controller
    for required in (
        'EntryPoint = "PrintWindow"',
        "PW_RENDERFULLCONTENT",
        "PrintWindowContent($WindowHandle, $targetDc)",
        "EnumWindows",
        "SetWinEventHook",
        "TreeScope]::Descendants",
        "Executed guest harness differs from the controller-reviewed file",
        "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        "120.0.2210.91",
        "webview_product_guid = $WebView2ProductionGuid",
        "minimum_webview_version = $MinimumWebView2Version.ToString()",
    ):
        assert required in guest
    assert "Get-ChildItem -LiteralPath $root" not in guest
    assert "-like '*WebView2*'" not in guest
    assert "Sort-Object { [version]$_.Version }" not in guest
    assert (
        "HKLM:\\SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\$WebView2ProductionGuid"
        not in guest
    )
    assert "CopyFromScreen" not in guest
    assert "passed =" not in controller
    assert "passed =" not in guest
