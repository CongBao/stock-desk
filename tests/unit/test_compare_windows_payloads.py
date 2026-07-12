from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import struct

import pytest

from scripts import compare_windows_payloads as comparer
from scripts import verify_windows_desktop_bundle as verifier


def _pe_x64(*, timestamp: int, checksum: int, content: int = 0) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 128)
    payload[128:132] = b"PE\0\0"
    struct.pack_into("<H", payload, 132, 0x8664)
    struct.pack_into("<I", payload, 136, timestamp)
    struct.pack_into("<H", payload, 148, 0xF0)
    struct.pack_into("<H", payload, 152, 0x20B)
    struct.pack_into("<I", payload, 216, checksum)
    payload[-1] = content
    return bytes(payload)


def _pe_x86(*, timestamp: int, checksum: int) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 128)
    payload[128:132] = b"PE\0\0"
    struct.pack_into("<H", payload, 132, verifier.PE_X86_MACHINE)
    struct.pack_into("<I", payload, 136, timestamp)
    struct.pack_into("<H", payload, 148, 0xE0)
    struct.pack_into("<H", payload, 152, 0x10B)
    struct.pack_into("<I", payload, 216, checksum)
    return bytes(payload)


def _manifest() -> dict[str, object]:
    raw: dict[str, object] = {
        "schema_version": 1,
        "artifact": "windows-desktop-bundle",
        "release": {
            "version": "1.1.0-alpha.2",
            "channel": "prerelease",
            "signature": "unsigned",
        },
        "source_sha": "a" * 40,
        "toolchain": {"rust": "1.88.0"},
        "locks": {"Cargo.lock": "b" * 64},
        "files": [
            {
                "path": "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
                "size": 512,
                "sha256": "1" * 64,
                "role": "webview2-offline-installer",
            },
            {
                "path": verifier.HOST_EXE,
                "size": 512,
                "sha256": "c" * 64,
                "role": "desktop-host",
            },
            {
                "path": verifier.SIDECAR_EXE,
                "size": 512,
                "sha256": "2" * 64,
                "role": "sidecar",
            },
            {
                "path": verifier.UNINSTALL_EXE,
                "size": 512,
                "sha256": "3" * 64,
                "role": "nsis-uninstaller",
            },
        ],
        "installer": None,
        "sbom": {"status": "not-produced", "hook": "cyclonedx-reserved"},
    }
    raw["manifest_sha256"] = verifier.manifest_digest(raw)
    return raw


def test_comparison_requires_exact_source_locks_toolchain_and_payload() -> None:
    left = _manifest()
    assert comparer.compare_manifests(left, copy.deepcopy(left))["reproducible"] is True

    for field in ("source_sha", "locks", "toolchain", "files"):
        right = copy.deepcopy(left)
        if field == "source_sha":
            right[field] = "d" * 40
        elif field == "files":
            right[field][0]["sha256"] = "d" * 64
        else:
            right[field] = {"changed": "d" * 64}
        right["manifest_sha256"] = verifier.manifest_digest(right)
        with pytest.raises(comparer.PayloadComparisonError, match=field):
            comparer.compare_manifests(left, right)


def test_file_mismatch_reports_only_bounded_public_identity_fields() -> None:
    left = _manifest()
    right = copy.deepcopy(left)
    right["files"][1]["sha256"] = "d" * 64
    right["manifest_sha256"] = verifier.manifest_digest(right)

    with pytest.raises(
        comparer.PayloadComparisonError,
        match=r"desktop manifests differ in files: desktop-host\.sha256",
    ):
        comparer.compare_manifests(left, right)


def test_nsis_only_allows_named_pe_timestamp_and_checksum_differences(
    tmp_path: Path,
) -> None:
    left_path = tmp_path / "a.exe"
    right_path = tmp_path / "b.exe"
    left_path.write_bytes(_pe_x64(timestamp=1, checksum=2))
    right_path.write_bytes(_pe_x64(timestamp=3, checksum=4))

    result = comparer.compare_nsis_installers(left_path, right_path)

    assert result["equivalent"] is True
    assert result["allowed_differences"] == ["pe-checksum", "pe-timestamp"]


def test_nsis_comparison_accepts_the_verified_x86_launcher_architecture(
    tmp_path: Path,
) -> None:
    left_path = tmp_path / "a.exe"
    right_path = tmp_path / "b.exe"
    left_path.write_bytes(_pe_x86(timestamp=1, checksum=2))
    right_path.write_bytes(_pe_x86(timestamp=3, checksum=4))

    result = comparer.compare_nsis_installers(left_path, right_path)

    assert result["equivalent"] is True
    assert result["allowed_differences"] == ["pe-checksum", "pe-timestamp"]


def test_nsis_rejects_any_unnamed_difference(tmp_path: Path) -> None:
    left_path = tmp_path / "a.exe"
    right_path = tmp_path / "b.exe"
    left_path.write_bytes(_pe_x64(timestamp=1, checksum=2, content=0))
    right_path.write_bytes(_pe_x64(timestamp=3, checksum=4, content=1))

    with pytest.raises(comparer.PayloadComparisonError, match="beyond"):
        comparer.compare_nsis_installers(left_path, right_path)


def test_manifest_comparison_canonicalizes_only_nsis_record(tmp_path: Path) -> None:
    left_path = tmp_path / "a.exe"
    right_path = tmp_path / "b.exe"
    left_path.write_bytes(_pe_x64(timestamp=1, checksum=2))
    right_path.write_bytes(_pe_x64(timestamp=3, checksum=4))
    left = _manifest()
    right = copy.deepcopy(left)
    left_record = {
        "path": "zz-stock-desk-installer.exe",
        "size": 512,
        "sha256": hashlib.sha256(left_path.read_bytes()).hexdigest(),
        "role": "nsis-installer",
    }
    right_record = {
        **left_record,
        "sha256": hashlib.sha256(right_path.read_bytes()).hexdigest(),
    }
    left["files"].append(left_record)
    right["files"].append(right_record)
    left["installer"] = left_record
    right["installer"] = right_record
    left["manifest_sha256"] = verifier.manifest_digest(left)
    right["manifest_sha256"] = verifier.manifest_digest(right)

    result = comparer.compare_manifests(
        left,
        right,
        left_installer=left_path,
        right_installer=right_path,
    )

    assert result["left_manifest_sha256"] != result["right_manifest_sha256"]
    assert result["left_installer_sha256"] != result["right_installer_sha256"]
    assert result["nsis"]["equivalent"] is True


def test_different_nsis_digest_requires_installer_bytes() -> None:
    left = _manifest()
    right = copy.deepcopy(left)
    left_record = {
        "path": "zz-stock-desk-installer.exe",
        "size": 512,
        "sha256": "d" * 64,
        "role": "nsis-installer",
    }
    right_record = {**left_record, "sha256": "e" * 64}
    left["files"].append(left_record)
    right["files"].append(right_record)
    left["installer"] = left_record
    right["installer"] = right_record
    left["manifest_sha256"] = verifier.manifest_digest(left)
    right["manifest_sha256"] = verifier.manifest_digest(right)

    with pytest.raises(comparer.PayloadComparisonError, match="require both"):
        comparer.compare_manifests(left, right)


def test_nsis_bytes_must_match_each_manifest_record(tmp_path: Path) -> None:
    left_path = tmp_path / "left.exe"
    right_path = tmp_path / "right.exe"
    unrelated_path = tmp_path / "unrelated.exe"
    left_path.write_bytes(_pe_x64(timestamp=1, checksum=2))
    right_path.write_bytes(_pe_x64(timestamp=3, checksum=4))
    unrelated_path.write_bytes(_pe_x64(timestamp=5, checksum=6))
    left = _manifest()
    right = copy.deepcopy(left)
    left_record = {
        "path": "zz-stock-desk-installer.exe",
        "size": 512,
        "sha256": hashlib.sha256(left_path.read_bytes()).hexdigest(),
        "role": "nsis-installer",
    }
    right_record = {
        **left_record,
        "sha256": hashlib.sha256(right_path.read_bytes()).hexdigest(),
    }
    left["files"].append(left_record)
    right["files"].append(right_record)
    left["installer"] = left_record
    right["installer"] = right_record
    left["manifest_sha256"] = verifier.manifest_digest(left)
    right["manifest_sha256"] = verifier.manifest_digest(right)

    with pytest.raises(comparer.PayloadComparisonError, match="left NSIS bytes"):
        comparer.compare_manifests(
            left,
            right,
            left_installer=unrelated_path,
            right_installer=right_path,
        )
