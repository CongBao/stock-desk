from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import sys
from typing import BinaryIO, cast

import pytest

import scripts.trusted_updater_release as trusted_release
from scripts.trusted_updater_release import (
    EvidencePaths,
    TrustedUpdaterReleaseError,
    evaluate_trusted_updater_release,
)


SOURCE_SHA = "a" * 40
VERSION = "1.1.0"
PUBLIC_KEY = (
    "untrusted comment: minisign public key\n"
    "RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3\n"
)
SIGNATURE = """untrusted comment: signature from minisign secret key
RUQf6LRCGA9i559r3g7V1qNyJDApGip8MfqcadIgT9CuhV3EMhHoN1mGTkUidF/z7SrlQgXdy8ofjb7bNJJylDOocrCo8KLzZwo=
trusted comment: timestamp:1633700835\tfile:test\tprehashed
wLMDjy9FLAuxZ3q4NlEvkgtyhrr0gtTu6KC4KBJdITbbOeAi1zBIYo0v4iTgt8jJpIidRJnp94ABQkJAgAooBQ==
"""
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "trusted-updater-release-v1.schema.json"
)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, EvidencePaths]:
    installer = tmp_path / "stock-desk-1.1.0-windows-x64-setup.exe"
    installer.write_bytes(b"test")
    signature = tmp_path / "installer.exe.sig"
    signature.write_text(SIGNATURE, encoding="utf-8")
    metadata = tmp_path / "latest.json"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": "stock-desk-trusted-updater-v1",
                "channel": "stable",
                "version": VERSION,
                "target": "windows-x86_64-nsis",
                "arch": "x86_64",
                "source_sha": SOURCE_SHA,
                "url": (
                    "https://github.com/CongBao/stock-desk/releases/download/"
                    "v1.1.0/stock-desk-1.1.0-windows-x64-setup.exe"
                ),
                "sha256": hashlib.sha256(b"test").hexdigest(),
                "signature": SIGNATURE.rstrip("\n"),
            }
        ),
        encoding="utf-8",
    )
    evidence_values: dict[str, Path] = {}
    for name in EvidencePaths.__required_keys__:
        path = tmp_path / f"{name}.json"
        path.write_text("{}", encoding="utf-8")
        evidence_values[name] = path
    return metadata, installer, signature, EvidencePaths(**evidence_values)


def _verified_path(tmp_path: Path) -> Path:
    parent = tmp_path / "verified"
    parent.mkdir(exist_ok=True)
    return parent / "stock-desk-1.1.0-windows-x64-setup.exe"


def _evaluate(tmp_path: Path) -> None:
    metadata, installer, signature, evidence = _paths(tmp_path)
    evaluate_trusted_updater_release(
        metadata_path=metadata,
        installer_path=installer,
        verified_installer_path=_verified_path(tmp_path),
        signature_path=signature,
        evidence=evidence,
        expected_version=VERSION,
        source_sha=SOURCE_SHA,
    )


def test_verification_contract_accepts_paths_not_claimed_success_or_secrets() -> None:
    parameters = inspect.signature(evaluate_trusted_updater_release).parameters
    assert "metadata_path" in parameters
    assert "installer_path" in parameters
    assert "verified_installer_path" in parameters
    assert "signature_path" in parameters
    assert "evidence" in parameters
    assert "secrets" not in parameters
    assert "payload_digest" not in parameters
    assert "signpath_status" not in parameters
    assert "repository" not in parameters


def test_schema_required_consts_patterns_and_fixture_match_verifier(
    tmp_path: Path,
) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    metadata, _, _, _ = _paths(tmp_path)
    fixture = json.loads(metadata.read_text(encoding="utf-8"))

    expected = set(trusted_release.TRUSTED_UPDATER_METADATA_FIELDS)
    assert set(schema["required"]) == expected == set(fixture)
    assert schema["additionalProperties"] is False
    for field, value in (
        ("schema_version", trusted_release._SCHEMA),
        ("channel", "stable"),
        ("target", trusted_release._TARGET),
        ("arch", trusted_release._ARCH),
    ):
        assert schema["properties"][field] == {"const": value}
        assert fixture[field] == value

    version_pattern = re.compile(schema["properties"]["version"]["pattern"])
    source_pattern = re.compile(schema["$defs"]["source_sha"]["pattern"])
    digest_pattern = re.compile(schema["$defs"]["sha256"]["pattern"])
    assert version_pattern.fullmatch(str(fixture["version"]))
    assert source_pattern.fullmatch(str(fixture["source_sha"]))
    assert digest_pattern.fullmatch(str(fixture["sha256"]))
    assert not version_pattern.fullmatch("1.1.0-beta.1")
    assert not version_pattern.fullmatch("01.1.0")
    assert not source_pattern.fullmatch("A" * 40)
    assert not digest_pattern.fullmatch("a" * 63)


@pytest.mark.parametrize("mutation", ["missing", "unknown"])
def test_verifier_rejects_schema_key_drift(tmp_path: Path, mutation: str) -> None:
    metadata, installer, signature, evidence = _paths(tmp_path)
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    if mutation == "missing":
        del payload["arch"]
    else:
        payload["claimed_verified"] = True
    metadata.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TrustedUpdaterReleaseError, match="fields are incomplete"):
        evaluate_trusted_updater_release(
            metadata_path=metadata,
            installer_path=installer,
            verified_installer_path=_verified_path(tmp_path),
            signature_path=signature,
            evidence=evidence,
            expected_version=VERSION,
            source_sha=SOURCE_SHA,
        )


def test_missing_fixed_production_public_key_fails_closed(tmp_path: Path) -> None:
    assert not trusted_release.TRUSTED_TAURI_PUBLIC_KEY.exists()
    with pytest.raises(TrustedUpdaterReleaseError, match="public key"):
        _evaluate(tmp_path)


def test_real_minisign_verification_rejects_mutated_payload(tmp_path: Path) -> None:
    installer = tmp_path / "payload.exe"
    installer.write_bytes(b"tampered")
    public_key = tmp_path / "trusted.pub"
    public_key.write_text(PUBLIC_KEY, encoding="utf-8")
    with pytest.raises(TrustedUpdaterReleaseError, match="does not verify"):
        with installer.open("rb") as payload:
            trusted_release._verify_minisign(
                payload=payload,
                signature_text=SIGNATURE.rstrip("\n"),
                public_key_path=public_key,
            )


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows fail-closed check")
def test_valid_test_signature_still_cannot_claim_release_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_key = tmp_path / "trusted.pub"
    public_key.write_text(PUBLIC_KEY, encoding="utf-8")
    monkeypatch.setattr(trusted_release, "TRUSTED_TAURI_PUBLIC_KEY", public_key)
    with pytest.raises(
        TrustedUpdaterReleaseError, match="WinVerifyTrust is unavailable"
    ):
        _evaluate(tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("channel", "beta", "stable"),
        ("version", "1.1.0+build", "version"),
        ("arch", "aarch64", "architecture"),
        ("url", "https://example.com/update.exe", "URL"),
        ("sha256", "0" * 64, "digest"),
        ("signature", "claimed", "signature"),
    ),
)
def test_claimed_metadata_cannot_replace_real_artifacts(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    metadata, installer, signature, evidence = _paths(tmp_path)
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload[field] = value
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TrustedUpdaterReleaseError, match=message):
        evaluate_trusted_updater_release(
            metadata_path=metadata,
            installer_path=installer,
            verified_installer_path=_verified_path(tmp_path),
            signature_path=signature,
            evidence=evidence,
            expected_version=VERSION,
            source_sha=SOURCE_SHA,
        )


def test_actual_installer_bytes_are_hashed_by_the_verifier(tmp_path: Path) -> None:
    metadata, installer, signature, evidence = _paths(tmp_path)
    installer.write_bytes(b"changed after metadata")
    with pytest.raises(TrustedUpdaterReleaseError, match="installer bytes"):
        evaluate_trusted_updater_release(
            metadata_path=metadata,
            installer_path=installer,
            verified_installer_path=_verified_path(tmp_path),
            signature_path=signature,
            evidence=evidence,
            expected_version=VERSION,
            source_sha=SOURCE_SHA,
        )


def test_verifier_publishes_the_exact_staged_object_not_the_source_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"signed installer bytes")
    output = _verified_path(tmp_path)

    with trusted_release._stage_installer(source, output) as staged:
        assert staged.path != source
        assert not output.exists()
        assert staged.sha256 == hashlib.sha256(b"signed installer bytes").hexdigest()
        assert staged.stream.read() == b"signed installer bytes"

    assert output.read_bytes() == b"signed installer bytes"
    source.write_bytes(b"replacement after publication")
    assert output.read_bytes() == b"signed installer bytes"


def test_source_replacement_during_verification_aborts_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"original")
    output = _verified_path(tmp_path)

    with pytest.raises(TrustedUpdaterReleaseError, match="source changed"):
        with trusted_release._stage_installer(source, output):
            replacement = tmp_path / "replacement.exe"
            replacement.write_bytes(b"replacement")
            replacement.replace(source)

    assert not output.exists()


def test_post_link_failure_revokes_readonly_output_and_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(trusted_release, "_fsync_directory", fail_directory_sync)
    with pytest.raises(TrustedUpdaterReleaseError, match="staged or published"):
        with trusted_release._stage_installer(source, output):
            assert not output.exists()

    assert not output.exists()
    assert list(output.parent.glob(".stock-desk-verified-*")) == []


def test_mocked_windows_success_keeps_target_absent_until_locked_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)
    publications: list[tuple[Path, Path]] = []

    def publish(temporary: Path, target: Path) -> None:
        publications.append((temporary, target))
        temporary.rename(target)

    def create(parent: Path) -> tuple[Path, BinaryIO, bool]:
        descriptor, name = trusted_release.tempfile.mkstemp(
            prefix=".stock-desk-verified-", suffix=".exe", dir=parent
        )
        return Path(name), os.fdopen(descriptor, "w+b"), True

    def duplicate(stream: BinaryIO) -> BinaryIO:
        return os.fdopen(os.dup(stream.fileno()), "rb")

    monkeypatch.setattr(trusted_release.sys, "platform", "win32")
    monkeypatch.setattr(
        trusted_release,
        "_create_staged_installer_file",
        create,
    )
    monkeypatch.setattr(
        trusted_release,
        "_duplicate_windows_verifier_stream",
        duplicate,
    )
    monkeypatch.setattr(
        trusted_release,
        "_set_open_windows_file_attributes",
        lambda _stream, _attributes: None,
    )
    monkeypatch.setattr(
        trusted_release,
        "_publish_staged_installer",
        publish,
    )

    with trusted_release._stage_installer(source, output) as staged:
        assert not output.exists()
        assert staged.stream.read() == b"verified"

    assert output.read_bytes() == b"verified"
    assert len(publications) == 1
    assert publications[0][1] == output
    assert list(output.parent.glob(".stock-desk-verified-*")) == []


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 file APIs")
def test_windows_production_publish_returns_the_locked_readonly_object(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)

    with trusted_release._stage_installer(source, output) as staged:
        temporary = staged.path
        assert temporary.exists()
        assert not output.exists()

    assert output.read_bytes() == b"verified"
    assert not temporary.exists()
    with pytest.raises(PermissionError):
        output.write_bytes(b"attacker")
    trusted_release._unlink_readonly(output)
    assert not output.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 file APIs")
def test_windows_production_cleanup_removes_readonly_temporary_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)
    temporary: Path | None = None

    with pytest.raises(RuntimeError, match="injected verification failure"):
        with trusted_release._stage_installer(source, output) as staged:
            temporary = staged.path
            raise RuntimeError("injected verification failure")

    assert temporary is not None and not temporary.exists()
    assert not output.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 file APIs")
def test_windows_production_post_move_failure_revokes_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)
    real_same_file_object = trusted_release._same_file_object
    comparisons = 0

    def fail_first_identity_check(left: os.stat_result, right: os.stat_result) -> bool:
        nonlocal comparisons
        comparisons += 1
        if comparisons == 1:
            return False
        return real_same_file_object(left, right)

    monkeypatch.setattr(trusted_release, "_same_file_object", fail_first_identity_check)

    with pytest.raises(TrustedUpdaterReleaseError, match="not the verified"):
        with trusted_release._stage_installer(source, output):
            assert not output.exists()

    assert not output.exists()
    assert list(output.parent.glob(".stock-desk-verified-*")) == []


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 file APIs")
def test_windows_production_move_then_error_revokes_by_open_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)
    real_publish = trusted_release._publish_staged_installer

    def publish_then_report_failure(temporary: Path, target: Path) -> None:
        real_publish(temporary, target)
        assert target.exists() and not temporary.exists()
        raise OSError("simulated false WinAPI result after completed move")

    monkeypatch.setattr(
        trusted_release, "_publish_staged_installer", publish_then_report_failure
    )
    with pytest.raises(TrustedUpdaterReleaseError, match="staged or published"):
        with trusted_release._stage_installer(source, output):
            assert not output.exists()

    assert not output.exists()
    assert list(output.parent.glob(".stock-desk-verified-*")) == []


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 file APIs")
def test_windows_production_attacker_target_is_never_deleted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)
    attacker = b"attacker-owned destination"
    temporary: Path | None = None

    with pytest.raises(TrustedUpdaterReleaseError, match="appeared"):
        with trusted_release._stage_installer(source, output) as staged:
            temporary = staged.path
            assert not output.exists()
            output.write_bytes(attacker)

    assert output.read_bytes() == attacker
    assert temporary is not None and not temporary.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 file APIs")
def test_windows_production_renamed_staged_object_is_revoked_without_deleting_decoys(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)
    third_path = output.parent / "renamed-staged-object.exe"
    temporary: Path | None = None
    temporary_decoy = b"attacker temporary decoy"
    final_decoy = b"attacker final decoy"

    with pytest.raises(RuntimeError, match="injected failure after rename"):
        with trusted_release._stage_installer(source, output) as staged:
            temporary = staged.path
            staged.path.rename(third_path)
            staged.path.write_bytes(temporary_decoy)
            output.write_bytes(final_decoy)
            raise RuntimeError("injected failure after rename")

    assert not third_path.exists()
    assert temporary is not None and temporary.read_bytes() == temporary_decoy
    assert output.read_bytes() == final_decoy


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Win32 file APIs")
def test_windows_production_handle_duplication_cannot_adopt_a_path_decoy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"verified")
    output = _verified_path(tmp_path)
    third_path = output.parent / "owned-object-moved-before-duplication.exe"
    temporary_decoy = b"close-reopen temporary decoy"
    final_decoy = b"close-reopen final decoy"
    temporary: Path | None = None
    real_duplicate = trusted_release._duplicate_windows_verifier_stream

    def swap_path_then_duplicate(stream: BinaryIO) -> BinaryIO:
        candidates = list(output.parent.glob(".stock-desk-verified-*"))
        assert len(candidates) == 1
        candidates[0].rename(third_path)
        candidates[0].write_bytes(temporary_decoy)
        output.write_bytes(final_decoy)
        return real_duplicate(stream)

    monkeypatch.setattr(
        trusted_release,
        "_duplicate_windows_verifier_stream",
        swap_path_then_duplicate,
    )
    with pytest.raises(RuntimeError, match="injected post-duplication failure"):
        with trusted_release._stage_installer(source, output) as staged:
            temporary = staged.path
            assert temporary.read_bytes() == temporary_decoy
            raise RuntimeError("injected post-duplication failure")

    assert not third_path.exists()
    assert temporary is not None and temporary.read_bytes() == temporary_decoy
    assert output.read_bytes() == final_decoy


def test_mocked_full_release_pipeline_publishes_only_the_verified_staged_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata, installer, signature, evidence = _paths(tmp_path)
    output = _verified_path(tmp_path)
    staged_paths: list[Path] = []
    authenticode = trusted_release.AuthenticodeEvidence(
        signer_subject="CN=Stock Desk",
        certificate_thumbprint="A" * 40,
        timestamp_subject="CN=Trusted Timestamp",
    )

    def verify_minisign(**kwargs: object) -> None:
        assert not output.exists()
        payload = cast(BinaryIO, kwargs["payload"])
        staged_paths.append(Path(str(payload.name)))

    def verify_authenticode(path: Path) -> trusted_release.AuthenticodeEvidence:
        assert not output.exists()
        staged_paths.append(path)
        return authenticode

    def verify_attestation(subject: Path, *_args: object, **_kwargs: object) -> None:
        assert not output.exists()
        if subject.name.startswith(".stock-desk-verified-"):
            staged_paths.append(subject)

    def verify_receipt(*_args: object, **_kwargs: object) -> None:
        assert not output.exists()

    monkeypatch.setattr(trusted_release, "_verify_minisign", verify_minisign)
    monkeypatch.setattr(trusted_release, "_verify_authenticode", verify_authenticode)
    monkeypatch.setattr(trusted_release, "_verify_gh_attestation", verify_attestation)
    monkeypatch.setattr(trusted_release, "_verify_signpath_receipt", verify_receipt)
    monkeypatch.setattr(trusted_release, "_verify_windows_receipt", verify_receipt)

    decision = evaluate_trusted_updater_release(
        metadata_path=metadata,
        installer_path=installer,
        verified_installer_path=output,
        signature_path=signature,
        evidence=evidence,
        expected_version=VERSION,
        source_sha=SOURCE_SHA,
    )

    assert decision["eligible"] is True
    assert decision["verified_installer_path"] == str(output)
    assert output.read_bytes() == installer.read_bytes()
    assert len(staged_paths) == 3
    assert len({path.resolve() for path in staged_paths}) == 1


def test_staged_payload_mutation_cannot_mix_verifier_inputs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.exe"
    source.write_bytes(b"original")
    output = _verified_path(tmp_path)

    with pytest.raises(TrustedUpdaterReleaseError):
        with trusted_release._stage_installer(source, output) as staged:
            assert not output.exists()
            os.chmod(staged.path, 0o600)
            staged.path.write_bytes(b"attacker")

    assert not output.exists()


def test_attestation_verification_is_bound_to_main_and_exact_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = tmp_path / "receipt.json"
    bundle = tmp_path / "receipt.bundle.jsonl"
    subject.write_text("{}", encoding="utf-8")
    bundle.write_text("{}", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="[{}]", stderr="")

    monkeypatch.setattr(trusted_release.subprocess, "run", run)
    trusted_release._verify_gh_attestation(
        subject,
        bundle,
        "CongBao/stock-desk",
        SOURCE_SHA,
        ".github/workflows/windows-installed.yml",
    )

    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("--repo") + 1] == "CongBao/stock-desk"
    assert command[command.index("--source-ref") + 1] == "refs/heads/main"
    assert command[command.index("--source-digest") + 1] == SOURCE_SHA
    assert command[command.index("--signer-digest") + 1] == SOURCE_SHA
    assert command[command.index("--signer-workflow") + 1] == (
        "CongBao/stock-desk/.github/workflows/windows-installed.yml"
    )
    assert "--deny-self-hosted-runners" in command


def test_signpath_receipt_must_match_actual_authenticode_identity(
    tmp_path: Path,
) -> None:
    authenticode = trusted_release.AuthenticodeEvidence(
        signer_subject="CN=Stock Desk",
        certificate_thumbprint="A" * 40,
        timestamp_subject="CN=Trusted Timestamp",
    )
    receipt = tmp_path / "signpath-receipt.json"
    payload = {
        "schema": "stock-desk-signpath-receipt-v1",
        "status": "signed",
        "source_sha": SOURCE_SHA,
        "payload_sha256": "b" * 64,
        "request_id": "signpath-request-1",
        "signer_subject": authenticode["signer_subject"],
        "certificate_thumbprint": authenticode["certificate_thumbprint"],
        "timestamp_subject": authenticode["timestamp_subject"],
    }
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    trusted_release._verify_signpath_receipt(
        receipt, SOURCE_SHA, "b" * 64, authenticode
    )

    payload["certificate_thumbprint"] = "C" * 40
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TrustedUpdaterReleaseError, match="exact-SHA bound"):
        trusted_release._verify_signpath_receipt(
            receipt, SOURCE_SHA, "b" * 64, authenticode
        )


def test_cli_success_log_never_discloses_decision_path_or_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = str(tmp_path / "private-user" / "signed-installer.exe")
    secret_digest = "d" * 64

    def evaluate(**_kwargs: object) -> trusted_release.TrustedUpdaterDecision:
        return {
            "eligible": True,
            "channel": "stable",
            "version": VERSION,
            "target": trusted_release._TARGET,
            "payload_sha256": secret_digest,
            "verified_installer_path": secret_path,
        }

    monkeypatch.setattr(trusted_release, "evaluate_trusted_updater_release", evaluate)
    argv = [
        str(tmp_path / "latest.json"),
        str(tmp_path / "installer.exe"),
        str(tmp_path / "installer.exe.sig"),
        "--verified-installer",
        secret_path,
        "--expected-version",
        VERSION,
        "--source-sha",
        SOURCE_SHA,
    ]
    for name in trusted_release.EvidencePaths.__required_keys__:
        argv.extend([f"--{name.replace('_', '-')}", str(tmp_path / f"{name}.json")])

    assert trusted_release.main(argv) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "channel": "stable",
        "eligible": True,
        "target": "windows-x86_64-nsis",
    }
    assert secret_path not in output
    assert secret_digest not in output


@pytest.mark.parametrize("version", ("1.1.0-beta.1", "1.1.0+build", "01.1.0"))
def test_only_exact_xyz_version_is_accepted(tmp_path: Path, version: str) -> None:
    metadata, installer, signature, evidence = _paths(tmp_path)
    with pytest.raises(TrustedUpdaterReleaseError, match="X.Y.Z"):
        evaluate_trusted_updater_release(
            metadata_path=metadata,
            installer_path=installer,
            verified_installer_path=_verified_path(tmp_path),
            signature_path=signature,
            evidence=evidence,
            expected_version=version,
            source_sha=SOURCE_SHA,
        )
