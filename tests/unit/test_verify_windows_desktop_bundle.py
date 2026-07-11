from __future__ import annotations

import os
from pathlib import Path
import struct

import pytest

from scripts import verify_windows_desktop_bundle as verifier


SOURCE_SHA = "a" * 40
LOCK_SHA = "b" * 64


def _pe_x64(*, timestamp: int = 0, checksum: int = 0, signed: bool = False) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 128)
    payload[128:132] = b"PE\0\0"
    struct.pack_into("<H", payload, 132, 0x8664)
    struct.pack_into("<H", payload, 148, 0xF0)
    struct.pack_into("<I", payload, 136, timestamp)
    struct.pack_into("<H", payload, 152, 0x20B)
    struct.pack_into("<I", payload, 216, checksum)
    if signed:
        struct.pack_into("<II", payload, 296, 400, 16)
        struct.pack_into("<IHH", payload, 400, 16, 0x0200, 0x0002)
    return bytes(payload)


def _payload(root: Path) -> None:
    (root / verifier.HOST_EXE).write_bytes(_pe_x64())
    (root / verifier.SIDECAR_EXE).write_bytes(_pe_x64())
    (root / verifier.UNINSTALL_EXE).write_bytes(_pe_x64())
    (root / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").write_bytes(
        _pe_x64(signed=True)
    )
    (root / "resources.pak").write_bytes(b"resource")


def _verify(root: Path) -> dict[str, object]:
    return verifier.verify_bundle(
        root,
        version="1.1.0-alpha.2",
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
        "version": "1.1.0-alpha.2",
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
        "version": "1.1.0-alpha.2",
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
            version="1.1.0-alpha.2",
            source_sha=SOURCE_SHA,
            toolchain={"rust": "1.88.0"},
            locks={"Cargo.lock": LOCK_SHA},
            installer=outside,
            signature_verifier=lambda _path: verifier.SignatureIdentity(
                valid=True, subject="CN=Microsoft Corporation"
            ),
        )

    (tmp_path / verifier.HOST_EXE).write_bytes(b"not-pe")
    with pytest.raises(verifier.BundleVerificationError, match="PE x64"):
        _verify(tmp_path)


def test_limits_are_fail_closed(tmp_path: Path) -> None:
    _payload(tmp_path)
    with pytest.raises(verifier.BundleVerificationError, match="file-count limit"):
        verifier.verify_bundle(
            tmp_path,
            version="1.1.0-alpha.2",
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
            version="1.1.0-alpha.2",
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
