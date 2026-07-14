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
    value = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


def _commands(job: dict[str, Any]) -> str:
    return "\n".join(
        str(step.get("run", ""))
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


def test_workflow_is_manual_exact_main_and_github_hosted_only() -> None:
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
    assert set(workflow["jobs"]) == {
        "dispatch-guard",
        "preflight",
        "windows-installed",
        "aggregate",
        "attest",
    }
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "pull_request:" not in source
    assert "schedule:" not in source
    assert "workflow_run:" not in source
    assert "contents: write" not in source
    assert "runs-on: self-hosted" not in source
    assert "[self-hosted" not in source
    assert "runs-on: windows" not in source
    for job in workflow["jobs"].values():
        assert job["runs-on"] in ("ubuntu-24.04",)
    guard = _commands(workflow["jobs"]["dispatch-guard"])
    for text in (
        '[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]',
        'test "$GITHUB_REF" = "refs/heads/main"',
        'test "$GITHUB_REF_TYPE" = "branch"',
        'test "$GITHUB_REF_NAME" = "main"',
        'test "$GITHUB_REF_PROTECTED" = "true"',
        'test "$GITHUB_SHA" = "$SOURCE_SHA"',
        'test "$GITHUB_WORKFLOW_REF" = "$EXPECTED_WORKFLOW_REF"',
        'test "$GITHUB_WORKFLOW_SHA" = "$SOURCE_SHA"',
        'test "$GITHUB_RUN_ATTEMPT" = "1"',
        "+refs/heads/main:refs/remotes/origin/protected-main",
        'test "$(git rev-parse refs/remotes/origin/protected-main)" = "$SOURCE_SHA"',
        '.name == "main" and .protected == true and .commit.sha == $sha',
        "environments/windows-installed-acceptance",
        "deployment-branch-policies",
        "actions/runners",
        "scripts/windows_installed_environment_policy.py verify",
    ):
        assert text in guard

    guard_job = workflow["jobs"]["dispatch-guard"]
    assert guard_job["environment"] == "windows-installed-acceptance"
    assert guard_job["env"]["SOURCE_SHA"] == "${{ inputs.source_sha }}"
    assert (
        "windows-installed.yml@refs/heads/main"
        in guard_job["env"]["EXPECTED_WORKFLOW_REF"]
    )
    checkout = next(
        step
        for step in guard_job["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ inputs.source_sha }}"
    assert workflow["jobs"]["preflight"]["needs"] == "dispatch-guard"
    assert workflow["jobs"]["windows-installed"]["needs"] == "preflight"
    assert workflow["jobs"]["aggregate"]["needs"] == [
        "preflight",
        "windows-installed",
    ]
    assert workflow["jobs"]["attest"]["needs"] == "aggregate"


def test_oidc_is_fixed_to_protected_broker_job_and_tokens_are_not_artifacts() -> None:
    workflow = _workflow()
    broker = workflow["jobs"]["windows-installed"]
    assert broker["environment"] == "windows-installed-acceptance"
    assert broker["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    for name in ("dispatch-guard", "preflight", "aggregate"):
        assert "id-token" not in workflow["jobs"][name]["permissions"]
    attest = workflow["jobs"]["attest"]
    assert attest["permissions"]["id-token"] == "write"
    assert "environment" not in attest
    assert "WINDOWS_VM_BROKER_ENDPOINT" not in str(attest)
    commands = _commands(broker)
    assert "scripts/windows_vm_broker_client.py" in commands
    assert "WINDOWS_VM_BROKER_ENDPOINT" in str(broker["env"])
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in WORKFLOW_PATH.read_text(
        encoding="utf-8"
    )
    assert "stock-desk-windows-installed-acceptance" in (
        ROOT / "scripts" / "windows_vm_broker_client.py"
    ).read_text(encoding="utf-8")

    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    guard = workflow["jobs"]["dispatch-guard"]
    assert source.count("WINDOWS_INSTALLED_POLICY_TOKEN") == 1
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
    assert _commands(guard).count('GH_TOKEN="$POLICY_TOKEN" gh api') == 3
    for name in ("preflight", "windows-installed", "aggregate", "attest"):
        assert "WINDOWS_INSTALLED_POLICY_TOKEN" not in str(workflow["jobs"][name])
    for upload in (
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ):
        assert "TOKEN" not in str(upload).upper()


def test_real_matrix_has_exact_eleven_first_attempt_cases() -> None:
    matrix = _workflow()["jobs"]["windows-installed"]["strategy"]["matrix"]["include"]
    assert len(matrix) == 11
    expected = {
        *(f"win10-22h2-dpi-{dpi}" for dpi in (100, 125, 150, 175, 200)),
        *(f"win11-dpi-{dpi}" for dpi in (100, 125, 150, 175, 200)),
        "win10-22h2-dpi-100-webview-offline",
    }
    assert {item["case_id"] for item in matrix} == expected
    assert all(item["scenario"] == "installed-first-use" for item in matrix[:-1])
    assert matrix[-1] == {
        "case_id": "win10-22h2-dpi-100-webview-offline",
        "guest_profile": "win10-22h2",
        "scenario": "webview-install-failure",
        "dpi": 100,
    }
    commands = _commands(_workflow()["jobs"]["windows-installed"])
    assert 'test "$GITHUB_RUN_ATTEMPT" = 1' in commands
    assert '--job-id "windows-installed-$CASE_ID"' in commands


def test_aggregate_consumes_raw_bytes_and_isolated_job_attests_receipt() -> None:
    workflow = _workflow()
    aggregate = workflow["jobs"]["aggregate"]
    assert aggregate["permissions"] == {
        "actions": "read",
        "contents": "read",
    }
    commands = _commands(aggregate)
    for text in (
        'test "${#packages[@]}" -eq 11',
        "-printf '%h\\0' | sort -zu",
        "scripts/verify_windows_desktop_raw_evidence.py",
        "--broker-public-key config/windows-vm-broker-public-key.pem",
        "--snapshot-policy-sha256",
        "--adapter-sha256",
        "--controller-request-sha256",
        "--guest-harness-sha256",
        "--uia-driver-sha256",
        "acceptance-receipt.json",
    ):
        assert text in commands
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "verify_windows_installed_evidence.py" not in source
    assert "wiring_only" not in source
    assert "exit 86" not in source
    assert 'source "$RUNNER_TEMP/windows-installed-identity.env"' not in source
    assert aggregate["environment"] == "windows-installed-acceptance"
    assert aggregate["env"] == {
        "SOURCE_SHA": "${{ inputs.source_sha }}",
        "APPROVED_SNAPSHOT_POLICY_SHA256": "${{ secrets.WINDOWS_VM_SNAPSHOT_POLICY_SHA256 }}",
        "APPROVED_ADAPTER_SHA256": "${{ secrets.WINDOWS_VM_ADAPTER_SHA256 }}",
    }
    assert all(step.get("id") != "attest" for step in aggregate["steps"])
    attest_job = workflow["jobs"]["attest"]
    attest = next(step for step in attest_job["steps"] if step.get("id") == "attest")
    assert str(attest["uses"]).startswith("actions/attest@")
    assert attest_job["permissions"] == {
        "actions": "read",
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
    }
    assert "environment" not in attest_job
    assert aggregate["if"] == (
        "${{ needs.preflight.result == 'success' && "
        "needs.windows-installed.result == 'success' }}"
    )
    downloads = [
        step
        for step in aggregate["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert len(downloads) == 2
    assert downloads[0]["with"]["name"] == (
        "windows-installed-controller-${{ inputs.source_sha }}"
    )
    assert downloads[1]["with"] == {
        "pattern": "windows-installed-raw-*-${{ inputs.source_sha }}",
        "path": "${{ runner.temp }}/windows-installed-aggregate/raw",
        "merge-multiple": False,
    }
    upload = next(
        step
        for step in aggregate["steps"]
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["name"] == (
        "windows-installed-acceptance-${{ inputs.source_sha }}"
    )


def test_preflight_controller_request_is_raw_only_and_content_bound() -> None:
    preflight = _workflow()["jobs"]["preflight"]
    commands = _commands(preflight)
    for text in (
        "^[0-9a-f]{40}$",
        "repos/$GITHUB_REPOSITORY/branches/main",
        ".protected == true",
        'head_branch == "main"',
        'event == "push"',
        'conclusion == "success"',
        "main-validation-proof-$SOURCE_SHA",
        "windows-desktop-alpha-candidate-$SOURCE_SHA",
        "proof-attestation-bundle.jsonl",
        "evidence-attestation-bundle.jsonl",
        '"schema": "stock-desk-windows-installed-controller-request-v2"',
        '"status": "authorized"',
        '"raw_only": True',
        '"case_ids"',
        '"candidate_manifest_sha256"',
        '"main_proof_sha256"',
        '"candidate_sha256"',
        '"webview_installer_sha256"',
        '"guest_harness_sha256"',
        '"uia_driver_sha256"',
        '"broker_public_key_sha256"',
        "windows-desktop-alpha-candidate-manifest.json",
        "manifest-binding.json",
        "gh attestation verify",
        "--source-ref refs/heads/main",
        '--source-digest "$SOURCE_SHA"',
        '--signer-digest "$SOURCE_SHA"',
        "--deny-self-hosted-runners",
        "scripts/main_validation_proof.py verify",
        "verify_post_gh_attestation_binding",
        "scripts/artifact_manifest.py verify",
        "controller-digests.sha256",
    ):
        assert text in commands
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
    assert upload["with"]["name"] == (
        "windows-installed-controller-${{ inputs.source_sha }}"
    )
    assert upload["with"]["if-no-files-found"] == "error"


def test_workflow_cannot_publish_sign_or_substitute_fake_vm_evidence() -> None:
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    lowered = source.casefold()
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
        "|| true",
    ):
        assert forbidden not in lowered
    assert "derived-verifier-receipt" not in lowered
    assert "verification-receipt" not in lowered
    assert "actions/attest@" in lowered
    assert "subject-path:" in lowered
    assert "acceptance-receipt.json" in lowered
    assert "raw-manifest.json" not in next(
        str(step)
        for step in _workflow()["jobs"]["attest"]["steps"]
        if isinstance(step, dict) and step.get("id") == "attest"
    )
    broker_commands = _commands(_workflow()["jobs"]["windows-installed"])
    assert "raw-manifest.json" in broker_commands
    assert "passed" in broker_commands
    assert "RunInstalledAcceptance" not in _commands(_workflow()["jobs"]["preflight"])


def test_v1_and_v2_evidence_authorities_remain_exact_main_inputs() -> None:
    required = {
        "schemas/windows-installed-evidence-v1.schema.json",
        "schemas/windows-installed-raw-evidence-v1.schema.json",
        "schemas/windows-vm-snapshot-policy-v1.schema.json",
        "schemas/windows-installed-evidence-v2.schema.json",
        "schemas/windows-installed-raw-evidence-v2.schema.json",
        "schemas/windows-vm-lifecycle-receipt-v2.schema.json",
        "schemas/windows-vm-snapshot-policy-v2.schema.json",
        "scripts/verify_windows_raw_evidence.py",
        "scripts/verify_windows_desktop_raw_evidence.py",
        "scripts/windows_desktop_uia_driver.ps1",
        "scripts/windows_vm_broker_client.py",
        "config/windows-vm-broker-public-key.pem",
    }
    assert required <= set(CRITICAL_INPUTS)


def _valid_environment() -> dict[str, object]:
    return {
        "id": 1,
        "node_id": "EN_x",
        "name": "windows-installed-acceptance",
        "url": "https://api.github.com/repos/CongBao/stock-desk/environments/windows-installed-acceptance",
        "html_url": "https://github.com/CongBao/stock-desk/deployments/activity_log?environments_filter=windows-installed-acceptance",
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z",
        "can_admins_bypass": False,
        "protection_rules": [
            {"id": 1, "node_id": "BP_x", "type": "branch_policy"},
            {
                "id": 2,
                "node_id": "RR_x",
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "reviewer": {"id": 42}}],
            },
        ],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }


def test_environment_policy_remains_exact_main_and_zero_runner() -> None:
    valid = _valid_environment()
    policies = {
        "total_count": 1,
        "branch_policies": [{"name": "main", "type": "branch"}],
    }
    runners = {"total_count": 0, "runners": []}
    verify_environment_policy(
        valid,
        branch_policies=policies,
        runners=runners,
        repository="CongBao/stock-desk",
    )
    with pytest.raises(EnvironmentPolicyError):
        verify_environment_policy(
            {**valid, "can_admins_bypass": True},
            branch_policies=policies,
            runners=runners,
            repository="CongBao/stock-desk",
        )
    with pytest.raises(EnvironmentPolicyError):
        verify_environment_policy(
            valid,
            branch_policies={
                "total_count": 1,
                "branch_policies": [{"name": "*", "type": "branch"}],
            },
            runners=runners,
            repository="CongBao/stock-desk",
        )
    with pytest.raises(EnvironmentPolicyError):
        verify_environment_policy(
            valid,
            branch_policies=policies,
            runners={"total_count": 1, "runners": [{"id": 1}]},
            repository="CongBao/stock-desk",
        )


def test_environment_policy_rejects_every_identity_policy_and_runner_expansion() -> (
    None
):
    valid = _valid_environment()
    policies = {
        "total_count": 1,
        "branch_policies": [{"name": "main", "type": "branch"}],
    }
    runners = {"total_count": 0, "runners": []}
    for mutation in (
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
    ):
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


@pytest.mark.parametrize(
    "reviewer_rules",
    [
        [],
        [
            {
                "type": "required_reviewers",
                "prevent_self_review": False,
                "reviewers": [{"type": "User", "reviewer": {"id": 42}}],
            }
        ],
        [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [],
            }
        ],
        [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "App", "reviewer": {"id": 42}}],
            }
        ],
        [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "Team", "reviewer": {"id": True}}],
            }
        ],
        [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "reviewer": {"id": 42}}],
            },
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "Team", "reviewer": {"id": 84}}],
            },
        ],
    ],
    ids=(
        "missing",
        "self-review-enabled",
        "empty",
        "invalid-type",
        "invalid-id",
        "duplicate-rule",
    ),
)
def test_environment_policy_requires_one_non_self_reviewer_rule(
    reviewer_rules: list[dict[str, object]],
) -> None:
    valid = _valid_environment()
    valid["protection_rules"] = [
        {"id": 1, "node_id": "BP_x", "type": "branch_policy"},
        *reviewer_rules,
    ]
    with pytest.raises(EnvironmentPolicyError, match="reviewer"):
        verify_environment_policy(
            valid,
            branch_policies={
                "total_count": 1,
                "branch_policies": [{"name": "main", "type": "branch"}],
            },
            runners={"total_count": 0, "runners": []},
            repository="CongBao/stock-desk",
        )


def test_environment_bootstrap_preserves_reviewers_and_disables_bypass() -> None:
    payload = bootstrap_payload(_valid_environment())
    assert payload["can_admins_bypass"] is False
    assert payload["prevent_self_review"] is True
    assert payload["reviewers"] == [{"type": "User", "id": 42}]
    assert payload["deployment_branch_policy"] == {
        "protected_branches": False,
        "custom_branch_policies": True,
    }
    malformed = _valid_environment()
    malformed["protection_rules"] = [{"type": "required_reviewers", "reviewers": []}]
    with pytest.raises(EnvironmentPolicyError):
        bootstrap_payload(malformed)


def test_environment_bootstrap_preserves_team_wait_timer_and_self_review_policy() -> (
    None
):
    existing = _valid_environment()
    existing["protection_rules"] = [
        {"id": 1, "node_id": "BP_x", "type": "branch_policy"},
        {"id": 2, "node_id": "WT_x", "type": "wait_timer", "wait_timer": 7},
        {
            "id": 3,
            "node_id": "RR_x",
            "type": "required_reviewers",
            "prevent_self_review": True,
            "reviewers": [{"type": "Team", "reviewer": {"id": 84}}],
        },
    ]
    assert bootstrap_payload(existing) == {
        "wait_timer": 7,
        "prevent_self_review": True,
        "can_admins_bypass": False,
        "reviewers": [{"type": "Team", "id": 84}],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }
    for malformed_rule in (
        {"type": "wait_timer", "wait_timer": True},
        {"type": "wait_timer", "wait_timer": "7"},
        {"type": "required_reviewers", "reviewers": []},
        {
            "type": "required_reviewers",
            "reviewers": [{"type": "App", "reviewer": {"id": 84}}],
        },
        {
            "type": "required_reviewers",
            "reviewers": [{"type": "Team", "reviewer": {"id": True}}],
        },
        {
            "type": "required_reviewers",
            "reviewers": [{"type": "Team", "reviewer": {}}],
        },
    ):
        malformed = _valid_environment()
        malformed["protection_rules"] = [
            {"type": "branch_policy"},
            malformed_rule,
        ]
        with pytest.raises(EnvironmentPolicyError):
            bootstrap_payload(malformed)


def test_v1_raw_evidence_and_controller_contracts_remain_fail_closed() -> None:
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


def test_windows_browser_observer_fixture_stays_on_supported_compiler_path() -> None:
    integration = (
        ROOT / "tests" / "windows" / "windows_browser_observer_integration.ps1"
    ).read_text(encoding="utf-8")
    assert "Framework64\\v4.0.30319\\csc.exe" in integration
    assert "'/target:winexe'" in integration
    assert "System.Diagnostics.Process.GetCurrentProcess().Id" in integration
    assert "Add-Type -TypeDefinition $fixtureSource -OutputAssembly" not in integration
