from __future__ import annotations

import os
from pathlib import Path
import stat
import struct
import subprocess
from types import SimpleNamespace

import pytest

from scripts import verify_windows_desktop_bundle as verifier


def test_cli_requires_an_explicit_manifest_output() -> None:
    with pytest.raises(SystemExit) as error:
        verifier.main(
            [
                "payload",
                "--version",
                "1.1.0-beta.2",
                "--source-sha",
                "a" * 40,
                "--toolchain",
                "python=3.12",
                "--lock",
                f"uv.lock={'b' * 64}",
            ]
        )

    assert error.value.code == 2


SOURCE_SHA = "a" * 40
LOCK_SHA = "b" * 64


def _pe(
    *,
    machine: int = verifier.PE_X64_MACHINE,
    timestamp: int = 0,
    checksum: int = 0,
    signed: bool = False,
) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 128)
    payload[128:132] = b"PE\0\0"
    struct.pack_into("<H", payload, 132, machine)
    optional_size = 0xF0 if machine == verifier.PE_X64_MACHINE else 0xE0
    struct.pack_into("<H", payload, 148, optional_size)
    struct.pack_into("<I", payload, 136, timestamp)
    magic = 0x20B if machine == verifier.PE_X64_MACHINE else 0x10B
    struct.pack_into("<H", payload, 152, magic)
    struct.pack_into("<I", payload, 216, checksum)
    if signed:
        security_directory = 296 if machine == verifier.PE_X64_MACHINE else 280
        struct.pack_into("<II", payload, security_directory, 400, 16)
        struct.pack_into("<IHH", payload, 400, 16, 0x0200, 0x0002)
    return bytes(payload)


def _pe_x64(*, timestamp: int = 0, checksum: int = 0, signed: bool = False) -> bytes:
    return _pe(timestamp=timestamp, checksum=checksum, signed=signed)


def _payload(root: Path) -> None:
    (root / verifier.HOST_EXE).write_bytes(_pe_x64())
    (root / verifier.SIDECAR_EXE).write_bytes(_pe_x64())
    (root / verifier.UNINSTALL_EXE).write_bytes(_pe_x64())
    (root / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").write_bytes(
        _pe(machine=verifier.PE_X86_MACHINE, signed=True)
    )
    (root / "resources.pak").write_bytes(b"resource")


def _verify(root: Path) -> dict[str, object]:
    return verifier.verify_bundle(
        root,
        version="1.1.0-beta.2",
        source_sha=SOURCE_SHA,
        toolchain={"rust": "1.88.0", "python": "3.12.11"},
        locks={"Cargo.lock": LOCK_SHA, "uv.lock": LOCK_SHA},
        signature_verifier=lambda _path: verifier.SignatureIdentity(
            valid=True, subject="CN=Microsoft Corporation"
        ),
    )


def test_verifier_emits_closed_public_safe_manifest(tmp_path: Path) -> None:
    _payload(tmp_path)

    manifest = _verify(tmp_path)

    assert set(manifest) == {
        "schema_version",
        "artifact",
        "release",
        "source_sha",
        "toolchain",
        "locks",
        "files",
        "installer",
        "sbom",
        "manifest_sha256",
    }
    assert manifest["release"] == {
        "version": "1.1.0-beta.2",
        "channel": "prerelease",
        "signature": "unsigned",
    }
    files = manifest["files"]
    assert isinstance(files, list)
    assert {entry["role"] for entry in files} == {
        "desktop-host",
        "sidecar",
        "webview2-offline-installer",
        "nsis-uninstaller",
        "runtime-resource",
    }
    serialized = verifier.canonical_json(manifest).decode()
    assert os.fspath(tmp_path) not in serialized
    assert verifier.validate_manifest(manifest) == manifest
    assert manifest["sbom"] == {
        "status": "not-produced",
        "hook": "cyclonedx-reserved",
    }


@pytest.mark.parametrize(
    "relative",
    [
        "src/main.py",
        "web/dist/index.html",
        "browser/chromium.dll",
        "tests/test_app.py",
        "package.json",
        "source.map",
    ],
)
def test_verifier_rejects_source_dev_and_browser_files(
    tmp_path: Path, relative: str
) -> None:
    _payload(tmp_path)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"forbidden")

    with pytest.raises(verifier.BundleVerificationError, match="forbidden"):
        _verify(tmp_path)


def test_verifier_rejects_unexpected_executable_and_missing_offline_webview(
    tmp_path: Path,
) -> None:
    _payload(tmp_path)
    (tmp_path / "unexpected.exe").write_bytes(_pe_x64())
    with pytest.raises(verifier.BundleVerificationError, match="unexpected executable"):
        _verify(tmp_path)

    (tmp_path / "unexpected.exe").unlink()
    (tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").unlink()
    with pytest.raises(verifier.BundleVerificationError, match="WebView2 offline"):
        _verify(tmp_path)


def test_webview_requires_valid_microsoft_authenticode(tmp_path: Path) -> None:
    _payload(tmp_path)
    common = {
        "version": "1.1.0-beta.2",
        "source_sha": SOURCE_SHA,
        "toolchain": {"rust": "1.88.0"},
        "locks": {"Cargo.lock": LOCK_SHA},
    }
    with pytest.raises(
        verifier.BundleVerificationError, match="signer is not Microsoft"
    ):
        verifier.verify_bundle(
            tmp_path,
            **common,
            signature_verifier=lambda _path: verifier.SignatureIdentity(
                valid=True, subject="CN=Other Vendor"
            ),
        )

    with pytest.raises(
        verifier.BundleVerificationError, match="signer is not Microsoft"
    ):
        verifier.verify_bundle(
            tmp_path,
            **common,
            signature_verifier=lambda _path: verifier.SignatureIdentity(
                valid=True, subject="CN=Not Microsoft Support, O=Other Vendor"
            ),
        )

    webview = tmp_path / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    webview.write_bytes(_pe_x64(signed=False))
    with pytest.raises(verifier.BundleVerificationError, match="must carry"):
        verifier.verify_bundle(
            tmp_path,
            **common,
            signature_verifier=lambda _path: verifier.SignatureIdentity(
                valid=True, subject="CN=Microsoft Corporation"
            ),
        )


def test_windows_authenticode_passes_an_opaque_path_outside_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "signed payload.exe"
    target.write_bytes(_pe(machine=verifier.PE_X86_MACHINE, signed=True))
    captured: dict[str, object] = {}

    monkeypatch.setattr(verifier.os, "name", "nt")
    monkeypatch.setattr(verifier.shutil, "which", lambda _name: "pwsh.exe")

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"status_code":1,"status":"Valid",'
                '"subject":"CN=Microsoft Corporation"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(verifier.subprocess, "run", run)

    identity = verifier.verify_windows_authenticode(target)

    assert identity == verifier.SignatureIdentity(
        valid=True, subject="CN=Microsoft Corporation"
    )
    assert os.fspath(target) not in captured["command"]
    command = captured["command"]
    assert isinstance(command, list)
    assert command[-2] == "-EncodedCommand"
    decoded_script = verifier.base64.b64decode(command[-1]).decode("utf-16-le")
    assert os.fspath(target) not in decoded_script
    path_token = verifier.base64.b64encode(os.fspath(target).encode()).decode()
    assert path_token in decoded_script


def test_windows_authenticode_reports_only_the_safe_trust_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "signed payload.exe"
    target.write_bytes(_pe(machine=verifier.PE_X86_MACHINE, signed=True))
    monkeypatch.setattr(verifier.os, "name", "nt")
    monkeypatch.setattr(verifier.shutil, "which", lambda _name: "pwsh.exe")
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_options: subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                '{"status_code":4,"status":"Not Trusted",'
                '"subject":"CN=Microsoft Corporation"}'
            ),
            stderr="",
        ),
    )

    with pytest.raises(
        verifier.BundleVerificationError,
        match="trust status is not valid: NotTrusted; signer=microsoft",
    ):
        verifier.verify_windows_authenticode(target)


def test_windows_authenticode_reports_only_the_safe_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "signed payload.exe"
    target.write_bytes(_pe(machine=verifier.PE_X86_MACHINE, signed=True))
    monkeypatch.setattr(verifier.os, "name", "nt")
    monkeypatch.setattr(verifier.shutil, "which", lambda _name: "pwsh.exe")
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_options: subprocess.CompletedProcess(
            [],
            0,
            stdout='{"error_type":"FileNotFoundException"}',
            stderr="",
        ),
    )

    with pytest.raises(
        verifier.BundleVerificationError,
        match="Authenticode command error: FileNotFoundException",
    ):
        verifier.verify_windows_authenticode(target)


def test_unknown_powershell_status_requires_independent_signtool_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "signed payload.exe"
    target.write_bytes(_pe(machine=verifier.PE_X86_MACHINE, signed=True))
    calls: list[list[str]] = []
    monkeypatch.setattr(verifier.os, "name", "nt")
    monkeypatch.setattr(
        verifier.shutil,
        "which",
        lambda name: name if name in {"pwsh.exe", "signtool.exe"} else None,
    )

    def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "pwsh.exe":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"status_code":0,"status":"UnknownError",'
                    '"subject":"CN=Microsoft Corporation"}'
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", run)

    assert verifier.verify_windows_authenticode(target).valid is True
    assert calls[1] == [
        "signtool.exe",
        "verify",
        "/pa",
        os.fspath(target),
    ]


def test_signtool_failure_is_reduced_to_a_safe_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "signed payload.exe"
    target.write_bytes(_pe(machine=verifier.PE_X86_MACHINE, signed=True))
    monkeypatch.setattr(verifier, "_find_signtool", lambda: Path("signtool.exe"))
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_options: subprocess.CompletedProcess(
            [],
            1,
            stdout="SignTool Error: WinVerifyTrust returned error: 0x80096010",
            stderr=f"private path that must not escape: {target}",
        ),
    )

    with pytest.raises(
        verifier.BundleVerificationError,
        match="trust verification failed: winverifytrust-0x80096010",
    ) as error:
        verifier.verify_windows_signtool(target)
    assert os.fspath(target) not in str(error.value)


def test_malformed_certificate_table_is_rejected() -> None:
    payload = bytearray(_pe_x64())
    struct.pack_into("<II", payload, 296, 504, 64)

    with pytest.raises(verifier.BundleVerificationError, match="out-of-bounds"):
        verifier.parse_pe_x64(bytes(payload), label="fixture")


def test_verifier_rejects_symlinks_and_reparse_points(tmp_path: Path) -> None:
    _payload(tmp_path)
    (tmp_path / "link.pak").symlink_to(tmp_path / "resources.pak")
    with pytest.raises(verifier.BundleVerificationError, match="symlink or reparse"):
        _verify(tmp_path)

    fake = type("FakeStat", (), {"st_file_attributes": 0x400})()
    assert verifier.is_reparse_point(fake)


def test_verifier_rejects_unsafe_external_path_and_non_x64_pe(tmp_path: Path) -> None:
    _payload(tmp_path)
    outside = tmp_path.parent / "outside.exe"
    outside.write_bytes(_pe_x64())
    with pytest.raises(
        verifier.BundleVerificationError, match="must be inside payload"
    ):
        verifier.verify_bundle(
            tmp_path,
            version="1.1.0-beta.2",
            source_sha=SOURCE_SHA,
            toolchain={"rust": "1.88.0"},
            locks={"Cargo.lock": LOCK_SHA},
            installer=outside,
            signature_verifier=lambda _path: verifier.SignatureIdentity(
                valid=True, subject="CN=Microsoft Corporation"
            ),
        )

    (tmp_path / verifier.HOST_EXE).write_bytes(_pe(machine=verifier.PE_X86_MACHINE))
    with pytest.raises(verifier.BundleVerificationError, match="PE x64"):
        _verify(tmp_path)


def test_x86_nsis_launcher_is_allowed_while_installed_binaries_remain_x64(
    tmp_path: Path,
) -> None:
    _payload(tmp_path)
    installer = tmp_path / "stock-desk-unsigned-nsis.exe"
    installer.write_bytes(_pe(machine=verifier.PE_X86_MACHINE))

    manifest = verifier.verify_bundle(
        tmp_path,
        version="1.1.0-beta.2",
        source_sha=SOURCE_SHA,
        toolchain={"rust": "1.88.0"},
        locks={"Cargo.lock": LOCK_SHA},
        installer=installer,
        signature_verifier=lambda _path: verifier.SignatureIdentity(
            valid=True, subject="CN=Microsoft Corporation"
        ),
    )

    assert any(record["role"] == "nsis-installer" for record in manifest["files"])


def test_limits_are_fail_closed(tmp_path: Path) -> None:
    _payload(tmp_path)
    with pytest.raises(verifier.BundleVerificationError, match="file-count limit"):
        verifier.verify_bundle(
            tmp_path,
            version="1.1.0-beta.2",
            source_sha=SOURCE_SHA,
            toolchain={"rust": "1.88.0"},
            locks={"Cargo.lock": LOCK_SHA},
            limits=verifier.Limits(
                max_files=3, max_file_size=1024, max_total_size=4096
            ),
            signature_verifier=lambda _path: verifier.SignatureIdentity(
                valid=True, subject="CN=Microsoft Corporation"
            ),
        )
    with pytest.raises(verifier.BundleVerificationError, match="single-file limit"):
        verifier.verify_bundle(
            tmp_path,
            version="1.1.0-beta.2",
            source_sha=SOURCE_SHA,
            toolchain={"rust": "1.88.0"},
            locks={"Cargo.lock": LOCK_SHA},
            limits=verifier.Limits(max_files=10, max_file_size=7, max_total_size=4096),
            signature_verifier=lambda _path: verifier.SignatureIdentity(
                valid=True, subject="CN=Microsoft Corporation"
            ),
        )


def test_hash_detects_metadata_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"payload")
    real_fstat = os.fstat
    calls = 0

    def changing(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_fstat(fd)
        if calls == 2:
            values = list(result)
            values[8] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(verifier.os, "fstat", changing)
    with pytest.raises(verifier.BundleVerificationError, match="changed while hashing"):
        verifier.hash_regular_file(target, expected_lstat=os.lstat(target))


def test_hash_identity_ignores_windows_extension_derived_permission_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "payload.exe"
    target.write_bytes(b"payload")
    expected = os.lstat(target)
    real_fstat = os.fstat

    def descriptor_without_extension_permissions(fd: int) -> object:
        result = real_fstat(fd)
        return SimpleNamespace(
            st_mode=result.st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH),
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns,
            st_file_attributes=getattr(result, "st_file_attributes", 0),
        )

    monkeypatch.setattr(verifier.os, "fstat", descriptor_without_extension_permissions)

    assert verifier.hash_regular_file(target, expected_lstat=expected) == (
        verifier.hashlib.sha256(b"payload").hexdigest(),
        len(b"payload"),
    )


def test_verifier_refreshes_discovery_metadata_before_descriptor_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _payload(tmp_path)
    discovered = verifier._walk_files(tmp_path)
    webview = next(
        path for path, _metadata in discovered if path.name.startswith("MicrosoftEdge")
    )
    actual = webview.lstat()

    class StaleMetadata:
        st_mode = actual.st_mode
        st_dev = actual.st_dev
        st_ino = actual.st_ino + 1
        st_size = actual.st_size
        st_mtime_ns = actual.st_mtime_ns
        st_file_attributes = getattr(actual, "st_file_attributes", 0)

    monkeypatch.setattr(
        verifier,
        "_walk_files",
        lambda _root: [
            (path, StaleMetadata() if path == webview else metadata)
            for path, metadata in discovered
        ],
    )
    real_hash = verifier.hash_regular_file

    def record_fresh_identity(
        path: Path, *, expected_lstat: os.stat_result
    ) -> tuple[str, int]:
        assert verifier._stat_identity(expected_lstat) == verifier._stat_identity(
            path.lstat()
        )
        return real_hash(path, expected_lstat=expected_lstat)

    monkeypatch.setattr(verifier, "hash_regular_file", record_fresh_identity)

    assert _verify(tmp_path)["artifact"] == verifier.ARTIFACT_KIND
