from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import struct
from typing import Mapping, cast

import pytest
import yaml

from scripts.signpath_contract import (
    REQUIRED_ROLES,
    SignPathContractError,
    _hash_regular_file,
    _pe_authenticode_identity,
    build_identity_closure,
    build_signing_receipt,
    build_signing_request,
    evaluate_signing_contract,
    main,
    verify_signing_equivalence,
    verify_manual_approval_environment,
)


SHA = "a" * 40
TREE = "b" * 40
PROOF_DIGEST = "c" * 64
CANDIDATE_DIGEST = "d" * 64
SECRETS = {
    "SIGNPATH_API_TOKEN": "secret",
    "SIGNPATH_ORGANIZATION_ID": "org",
    "SIGNPATH_PROJECT_SLUG": "stock-desk",
    "SIGNPATH_SIGNING_POLICY_SLUG": "release-signing",
    "SIGNPATH_ARTIFACT_CONFIGURATION_SLUG": "windows-nested-authenticode",
    "SIGNPATH_POLICY_TOKEN": "policy-token",
}
UNSIGNED = {
    "desktop-host": {
        "path": "app/stock-desk-desktop.exe",
        "sha256": "1" * 64,
    },
    "sidecar": {
        "path": "app/stock-desk-sidecar.exe",
        "sha256": "2" * 64,
    },
    "nsis-installer": {
        "path": "stock-desk-unsigned-nsis.exe",
        "sha256": "3" * 64,
    },
}
SIGNED = {
    "desktop-host": {
        "path": "stock-desk-desktop.exe",
        "sha256": "4" * 64,
    },
    "sidecar": {
        "path": "stock-desk-sidecar.exe",
        "sha256": "5" * 64,
    },
    "nsis-installer": {
        "path": "stock-desk-signed-nsis.exe",
        "sha256": "6" * 64,
    },
}
EQUIVALENCE = {
    "desktop-host": {
        "algorithm": "pe-authenticode-normalized-sha256-v1",
        "content_sha256": "7" * 64,
    },
    "sidecar": {
        "algorithm": "pe-authenticode-normalized-sha256-v1",
        "content_sha256": "8" * 64,
    },
    "nsis-installer": {
        "algorithm": "nsis-pe-stub-and-extracted-payload-sha256-v1",
        "content_sha256": "9" * 64,
    },
}


def _pe(payload: bytes, *, signed: bool = False) -> bytes:
    """Build the smallest PE shape needed to exercise audited normalization."""
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x80 + 6, 1)
    struct.pack_into("<H", data, 0x80 + 20, 224)
    optional = 0x80 + 24
    struct.pack_into("<H", data, optional, 0x10B)
    struct.pack_into("<I", data, optional + 92, 16)
    section = optional + 224
    struct.pack_into("<II", data, section + 16, 64, 448)
    if len(payload) > 64:
        raise ValueError("synthetic PE payload is too large")
    data[448 : 448 + len(payload)] = payload
    if signed:
        certificate_offset = len(data)
        certificate = struct.pack("<IHH", 16, 0x0200, 0x0002) + b"12345678"
        data.extend(certificate)
        struct.pack_into("<I", data, optional + 64, 0xAABBCCDD)
        struct.pack_into("<II", data, optional + 96 + (4 * 8), certificate_offset, 16)
    return bytes(data)


def _sign_pe(unsigned: bytes) -> bytes:
    data = bytearray(unsigned)
    while len(data) % 8:
        data.append(0)
    optional = 0x80 + 24
    certificate_offset = len(data)
    data.extend(struct.pack("<IHH", 16, 0x0200, 0x0002) + b"12345678")
    struct.pack_into("<I", data, optional + 64, 0xAABBCCDD)
    struct.pack_into("<II", data, optional + 96 + (4 * 8), certificate_offset, 16)
    return bytes(data)


def _environment() -> dict[str, object]:
    return {
        "name": "release-signing",
        "url": (
            "https://api.github.com/repos/CongBao/stock-desk/"
            "environments/release-signing"
        ),
        "can_admins_bypass": False,
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
        "protection_rules": [
            {"type": "branch_policy"},
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": 17, "login": "reviewer"}}
                ],
            },
        ],
    }


def _branches() -> dict[str, object]:
    return {
        "total_count": 1,
        "branch_policies": [{"name": "main", "type": "branch"}],
    }


def test_disabled_application_state_is_explicit_and_cannot_sign() -> None:
    decision = evaluate_signing_contract(
        status="application-submitted",
        enabled=False,
        source_sha=SHA,
        source_tree=TREE,
        proof_digest=PROOF_DIGEST,
        candidate_digest=CANDIDATE_DIGEST,
        secrets={},
    )

    assert decision == {
        "enabled": False,
        "reason": "signpath-application-not-integrated",
        "status": "application-submitted",
    }


@pytest.mark.parametrize("status", ["", "pending-review", "approved", "signed"])
def test_missing_or_non_integrated_status_fails_closed(status: str) -> None:
    with pytest.raises(SignPathContractError, match="integrated"):
        evaluate_signing_contract(
            status=status,
            enabled=True,
            source_sha=SHA,
            source_tree=TREE,
            proof_digest=PROOF_DIGEST,
            candidate_digest=CANDIDATE_DIGEST,
            secrets=SECRETS,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha", "main"),
        ("source_tree", "HEAD^{tree}"),
        ("proof_digest", ""),
        ("candidate_digest", "not-a-digest"),
    ],
)
def test_integrated_signing_requires_every_immutable_identity(
    field: str, value: str
) -> None:
    values = {
        "source_sha": SHA,
        "source_tree": TREE,
        "proof_digest": PROOF_DIGEST,
        "candidate_digest": CANDIDATE_DIGEST,
    }
    values[field] = value

    with pytest.raises(SignPathContractError, match=field):
        evaluate_signing_contract(
            status="integrated", enabled=True, secrets=SECRETS, **values
        )


@pytest.mark.parametrize("missing", sorted(SECRETS))
def test_integrated_signing_requires_every_named_secret(missing: str) -> None:
    secrets = dict(SECRETS)
    secrets.pop(missing)

    with pytest.raises(SignPathContractError, match=missing):
        evaluate_signing_contract(
            status="integrated",
            enabled=True,
            source_sha=SHA,
            source_tree=TREE,
            proof_digest=PROOF_DIGEST,
            candidate_digest=CANDIDATE_DIGEST,
            secrets=secrets,
        )


def test_manual_approval_environment_requires_exact_main_and_second_reviewer() -> None:
    verify_manual_approval_environment(
        _environment(),
        branch_policies=_branches(),
        repository="CongBao/stock-desk",
    )

    environment = _environment()
    environment["can_admins_bypass"] = True
    with pytest.raises(SignPathContractError, match="bypass"):
        verify_manual_approval_environment(
            environment,
            branch_policies=_branches(),
            repository="CongBao/stock-desk",
        )

    environment = _environment()
    reviewer = environment["protection_rules"][1]  # type: ignore[index]
    reviewer["prevent_self_review"] = False
    with pytest.raises(SignPathContractError, match="self review"):
        verify_manual_approval_environment(
            environment,
            branch_policies=_branches(),
            repository="CongBao/stock-desk",
        )

    with pytest.raises(SignPathContractError, match="exact main"):
        verify_manual_approval_environment(
            _environment(),
            branch_policies={
                "total_count": 1,
                "branch_policies": [{"name": "release/*", "type": "branch"}],
            },
            repository="CongBao/stock-desk",
        )


def test_request_closes_exactly_host_sidecar_and_nsis_identities() -> None:
    request = build_signing_request(
        source_sha=SHA,
        source_tree=TREE,
        proof_digest=PROOF_DIGEST,
        candidate_digest=CANDIDATE_DIGEST,
        unsigned=UNSIGNED,
    )

    assert request["schema"] == "stock-desk-signpath-request-v1"
    assert request["status"] == "awaiting-manual-approval"
    assert request["source"] == {
        "ref": "refs/heads/main",
        "sha": SHA,
        "tree": TREE,
    }
    assert set(cast(Mapping[str, object], request["unsigned"])) == REQUIRED_ROLES
    assert request["approval"] == {
        "github_environment": "release-signing",
        "prevent_self_review": True,
        "signpath_policy": "manual",
    }

    missing = dict(UNSIGNED)
    missing.pop("sidecar")
    with pytest.raises(SignPathContractError, match="exact roles"):
        build_signing_request(
            source_sha=SHA,
            source_tree=TREE,
            proof_digest=PROOF_DIGEST,
            candidate_digest=CANDIDATE_DIGEST,
            unsigned=missing,
        )


def test_receipt_and_identity_closure_reject_missing_or_replayed_identity() -> None:
    request = build_signing_request(
        source_sha=SHA,
        source_tree=TREE,
        proof_digest=PROOF_DIGEST,
        candidate_digest=CANDIDATE_DIGEST,
        unsigned=UNSIGNED,
    )
    closure = build_identity_closure(
        request=request,
        request_id="2e905217-2a1d-4f12-a1ef-936d0a0c44b0",
        signed=SIGNED,
        equivalence=EQUIVALENCE,
    )

    assert closure["status"] == "signed"
    artifacts = cast(Mapping[str, Mapping[str, str]], closure["artifacts"])
    assert set(artifacts) == REQUIRED_ROLES
    assert closure["request_sha256"]
    for role, record in artifacts.items():
        assert record["unsigned_sha256"] == UNSIGNED[role]["sha256"]
        assert record["signed_sha256"] == SIGNED[role]["sha256"]
        assert record["equivalence_algorithm"] == EQUIVALENCE[role]["algorithm"]
        assert (
            record["equivalent_content_sha256"] == EQUIVALENCE[role]["content_sha256"]
        )

    missing = dict(SIGNED)
    missing.pop("desktop-host")
    with pytest.raises(SignPathContractError, match="exact roles"):
        build_identity_closure(
            request=request,
            request_id="2e905217-2a1d-4f12-a1ef-936d0a0c44b0",
            signed=missing,
            equivalence=EQUIVALENCE,
        )

    incomplete_equivalence = dict(EQUIVALENCE)
    incomplete_equivalence.pop("sidecar")
    with pytest.raises(SignPathContractError, match="equivalence.*exact roles"):
        build_identity_closure(
            request=request,
            request_id="2e905217-2a1d-4f12-a1ef-936d0a0c44b0",
            signed=SIGNED,
            equivalence=incomplete_equivalence,
        )

    replayed = {role: dict(value) for role, value in SIGNED.items()}
    replayed["sidecar"]["sha256"] = UNSIGNED["sidecar"]["sha256"]
    with pytest.raises(SignPathContractError, match="unsigned digest"):
        build_identity_closure(
            request=request,
            request_id="2e905217-2a1d-4f12-a1ef-936d0a0c44b0",
            signed=replayed,
            equivalence=EQUIVALENCE,
        )

    tampered = dict(request)
    tampered["candidate_sha256"] = "e" * 64
    tampered["approval"] = {"github_environment": "unprotected"}
    with pytest.raises(SignPathContractError, match="approval"):
        build_identity_closure(
            request=tampered,
            request_id="2e905217-2a1d-4f12-a1ef-936d0a0c44b0",
            signed=SIGNED,
            equivalence=EQUIVALENCE,
        )


def _equivalence_fixture(
    tmp_path: Path,
) -> tuple[dict[str, dict[str, str]], dict[str, Path], dict[str, Path], Path, Path]:
    unsigned_root = tmp_path / "unsigned-extract"
    signed_root = tmp_path / "signed-extract"
    unsigned_app = unsigned_root / "app"
    signed_app = signed_root / "app"
    unsigned_app.mkdir(parents=True)
    signed_app.mkdir(parents=True)

    unsigned_host = _pe(b"host-content")
    unsigned_sidecar = _pe(b"sidecar-content")
    (unsigned_app / "stock-desk-desktop.exe").write_bytes(unsigned_host)
    (unsigned_app / "stock-desk-sidecar.exe").write_bytes(unsigned_sidecar)
    (signed_app / "stock-desk-desktop.exe").write_bytes(_sign_pe(unsigned_host))
    (signed_app / "stock-desk-sidecar.exe").write_bytes(_sign_pe(unsigned_sidecar))
    (unsigned_root / "config.json").write_text('{"channel":"stable"}', encoding="utf-8")
    (signed_root / "config.json").write_text('{"channel":"stable"}', encoding="utf-8")

    unsigned_installer = tmp_path / "stock-desk-unsigned-nsis.exe"
    signed_installer = tmp_path / "stock-desk-signed-nsis.exe"
    unsigned_installer.write_bytes(_pe(b"nsis-stub"))
    signed_installer.write_bytes(_sign_pe(unsigned_installer.read_bytes()))
    unsigned_paths = {
        "desktop-host": unsigned_app / "stock-desk-desktop.exe",
        "sidecar": unsigned_app / "stock-desk-sidecar.exe",
        "nsis-installer": unsigned_installer,
    }
    signed_paths = {
        "desktop-host": signed_app / "stock-desk-desktop.exe",
        "sidecar": signed_app / "stock-desk-sidecar.exe",
        "nsis-installer": signed_installer,
    }
    expected = {
        role: {
            "path": (f"app/{path.name}" if role != "nsis-installer" else path.name),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for role, path in unsigned_paths.items()
    }
    return expected, unsigned_paths, signed_paths, unsigned_root, signed_root


def test_authenticode_normalization_accepts_only_certificate_regions(
    tmp_path: Path,
) -> None:
    unsigned = tmp_path / "unsigned.exe"
    signed = tmp_path / "signed.exe"
    unsigned.write_bytes(_pe(b"same-image"))
    signed.write_bytes(_sign_pe(unsigned.read_bytes()))

    unsigned_raw, unsigned_normalized, unsigned_image = _pe_authenticode_identity(
        unsigned, "unsigned", require_signature=False
    )
    signed_raw, signed_normalized, signed_image = _pe_authenticode_identity(
        signed, "signed", require_signature=True
    )

    assert unsigned_raw != signed_raw
    assert unsigned_normalized == signed_normalized
    assert unsigned_image == signed_image


def test_equivalence_closes_pe_stub_and_complete_nsis_payload_tree(
    tmp_path: Path,
) -> None:
    expected, unsigned_paths, signed_paths, unsigned_root, signed_root = (
        _equivalence_fixture(tmp_path)
    )

    signed, equivalence = verify_signing_equivalence(
        expected_unsigned=expected,
        unsigned_paths=unsigned_paths,
        signed_paths=signed_paths,
        unsigned_extract_root=unsigned_root,
        signed_extract_root=signed_root,
    )

    assert set(signed) == REQUIRED_ROLES
    assert equivalence["desktop-host"]["algorithm"] == (
        "pe-authenticode-normalized-sha256-v1"
    )
    assert equivalence["nsis-installer"]["algorithm"] == (
        "nsis-pe-stub-and-extracted-payload-sha256-v1"
    )


def test_equivalence_rejects_a_substituted_but_signed_nested_payload(
    tmp_path: Path,
) -> None:
    expected, unsigned_paths, signed_paths, unsigned_root, signed_root = (
        _equivalence_fixture(tmp_path)
    )
    substituted = _pe(b"different-host")
    signed_paths["desktop-host"].write_bytes(_sign_pe(substituted))

    with pytest.raises(SignPathContractError, match="outside Authenticode"):
        verify_signing_equivalence(
            expected_unsigned=expected,
            unsigned_paths=unsigned_paths,
            signed_paths=signed_paths,
            unsigned_extract_root=unsigned_root,
            signed_extract_root=signed_root,
        )


def test_equivalence_rejects_changed_nsis_stub_or_extracted_file(
    tmp_path: Path,
) -> None:
    expected, unsigned_paths, signed_paths, unsigned_root, signed_root = (
        _equivalence_fixture(tmp_path)
    )
    signed_paths["nsis-installer"].write_bytes(_pe(b"evil-stub", signed=True))
    with pytest.raises(SignPathContractError, match="PE stub"):
        verify_signing_equivalence(
            expected_unsigned=expected,
            unsigned_paths=unsigned_paths,
            signed_paths=signed_paths,
            unsigned_extract_root=unsigned_root,
            signed_extract_root=signed_root,
        )

    signed_paths["nsis-installer"].write_bytes(
        _sign_pe(unsigned_paths["nsis-installer"].read_bytes())
    )
    (signed_root / "config.json").write_text('{"channel":"evil"}', encoding="utf-8")
    with pytest.raises(SignPathContractError, match="extracted bytes"):
        verify_signing_equivalence(
            expected_unsigned=expected,
            unsigned_paths=unsigned_paths,
            signed_paths=signed_paths,
            unsigned_extract_root=unsigned_root,
            signed_extract_root=signed_root,
        )


def test_secure_hash_rejects_symlink_and_hashes_open_descriptor_during_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "artifact.exe"
    replacement = tmp_path / "replacement.exe"
    original.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    link = tmp_path / "link.exe"
    link.symlink_to(original)
    with pytest.raises(SignPathContractError, match="unsafe"):
        _hash_regular_file(link, "linked artifact")

    real_open = os.open
    swapped = False

    def swap_after_open(
        path: object, flags: int, *args: object, **kwargs: object
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path) == original and not swapped:  # type: ignore[arg-type]
            swapped = True
            original.unlink()
            replacement.rename(original)
        return descriptor

    monkeypatch.setattr(os, "open", swap_after_open)
    with pytest.raises(SignPathContractError, match="path changed"):
        _hash_regular_file(original, "raced artifact")


def test_legacy_release_receipt_keeps_exact_trusted_updater_schema() -> None:
    receipt = build_signing_receipt(
        source_sha=SHA,
        payload_digest=SIGNED["nsis-installer"]["sha256"],
        request_id="2e905217-2a1d-4f12-a1ef-936d0a0c44b0",
        signer_subject="CN=Stock Desk",
        certificate_thumbprint="A" * 40,
        timestamp_subject="CN=Trusted Timestamp",
    )

    assert receipt == {
        "schema": "stock-desk-signpath-receipt-v1",
        "status": "signed",
        "source_sha": SHA,
        "payload_sha256": "6" * 64,
        "request_id": "2e905217-2a1d-4f12-a1ef-936d0a0c44b0",
        "signer_subject": "CN=Stock Desk",
        "certificate_thumbprint": "A" * 40,
        "timestamp_subject": "CN=Trusted Timestamp",
    }

    with pytest.raises(SignPathContractError, match="timestamp_subject"):
        build_signing_receipt(
            source_sha=SHA,
            payload_digest="6" * 64,
            request_id="request",
            signer_subject="CN=Stock Desk",
            certificate_thumbprint="A" * 40,
            timestamp_subject="",
        )


def test_workflow_is_reusable_protected_and_pinned() -> None:
    workflow = Path(".github/workflows/signpath.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)

    assert "workflow_call:" in workflow
    assert parsed["jobs"]["sign"]["if"] == "${{ false }}"
    assert "workflow_dispatch:" not in workflow
    assert "environment: release-signing" in workflow
    assert "refs/heads/main" in workflow
    assert "GITHUB_REF_PROTECTED" in workflow
    assert "release_tag:" in workflow
    assert "GITHUB_REF_TYPE -ne 'branch'" in workflow
    assert "GITHUB_REF_NAME -ne 'main'" in workflow
    assert "git cat-file -t" in workflow
    assert "refs/tags/$env:RELEASE_TAG^{commit}" in workflow
    assert "SIGNPATH_INTEGRATION_STATUS" in workflow
    assert "wait-for-completion: true" in workflow
    assert (
        "signpath/github-action-submit-signing-request@"
        "b9d91eadd323de506c0c81cf0c7fe7438f3360fd" in workflow
    )
    assert "signpath-attestation-bundle.jsonl" in workflow
    assert "stock-desk-signed-nsis.exe" in workflow
    assert "--unsigned-extract-root $env:UNSIGNED_EXTRACT_ROOT" in workflow
    assert "--signed-extract-root $env:SIGNED_EXTRACT_ROOT" in workflow
    assert '--unsigned "desktop-host=$($unsignedHosts[0].FullName)"' in workflow
    steps = parsed["jobs"]["sign"]["steps"]
    preflight = next(
        step
        for step in steps
        if step.get("name")
        == "Fail closed unless the integration state and identities are exact"
    )
    assert "env" not in preflight
    policy = next(
        step
        for step in steps
        if step.get("name") == "Verify release-signing manual approval policy"
    )
    assert set(policy["env"]) == {"GH_TOKEN"}
    submit = next(
        step
        for step in steps
        if step.get("name")
        == "Submit nested Authenticode request and wait for manual approval"
    )
    assert "env" not in submit
    assert (
        "stock-desk-unsigned-nsis.exe"
        not in workflow.split("name: Upload formal signed candidate")[-1]
    )
    for line in workflow.splitlines():
        if "uses:" in line:
            ref = line.split("@", 1)[-1].split()[0]
            assert len(ref) == 40 and all(char in "0123456789abcdef" for char in ref)


def test_request_is_canonical_json_serializable() -> None:
    request = build_signing_request(
        source_sha=SHA,
        source_tree=TREE,
        proof_digest=PROOF_DIGEST,
        candidate_digest=CANDIDATE_DIGEST,
        unsigned=UNSIGNED,
    )
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
    assert "secret" not in encoded.casefold()


def test_verify_environment_cli_smoke(tmp_path: Path) -> None:
    environment = tmp_path / "environment.json"
    branches = tmp_path / "branches.json"
    environment.write_text(json.dumps(_environment()), encoding="utf-8")
    branches.write_text(json.dumps(_branches()), encoding="utf-8")

    assert (
        main(
            [
                "verify-environment",
                "--environment",
                str(environment),
                "--branch-policies",
                str(branches),
                "--repository",
                "CongBao/stock-desk",
            ]
        )
        == 0
    )
