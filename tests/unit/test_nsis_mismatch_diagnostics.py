from __future__ import annotations

from pathlib import Path
import struct

import pytest

from scripts import nsis_mismatch_diagnostics as diagnostics


ROOT = Path(__file__).resolve().parents[2]


def _pe(*, timestamp: int, checksum: int, tail: bytes) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 128)
    payload[128:132] = b"PE\0\0"
    struct.pack_into("<H", payload, 132, 0x14C)
    struct.pack_into("<H", payload, 148, 0xE0)
    struct.pack_into("<I", payload, 136, timestamp)
    struct.pack_into("<H", payload, 152, 0x10B)
    struct.pack_into("<I", payload, 216, checksum)
    payload.extend(tail)
    return bytes(payload)


def test_build_diagnostic_identifies_wrapper_only_difference(tmp_path: Path) -> None:
    expected = tmp_path / "expected.exe"
    actual = tmp_path / "actual.exe"
    expected.write_bytes(_pe(timestamp=1, checksum=2, tail=b"same-wrapper-data"))
    actual.write_bytes(_pe(timestamp=3, checksum=4, tail=b"same-wrapper-data-extra"))
    expected_tree = tmp_path / "expected-tree"
    actual_tree = tmp_path / "actual-tree"
    expected_tree.mkdir()
    actual_tree.mkdir()
    (expected_tree / "stock-desk.exe").write_bytes(b"application")
    (actual_tree / "stock-desk.exe").write_bytes(b"application")

    report = diagnostics.build_diagnostic(
        expected=expected,
        actual=actual,
        expected_tree=expected_tree,
        actual_tree=actual_tree,
    )

    assert report["artifact"] == "stock-desk-nsis-mismatch-diagnostic-v1"
    assert report["classification"] == "wrapper-only"
    assert report["byte_difference"]["size_delta"] == 6
    assert report["byte_difference"]["common_prefix_bytes"] == 136
    assert report["expected"]["pe"]["timestamp"] == 1
    assert report["actual"]["pe"]["checksum"] == 4
    assert report["extracted"]["equivalent"] is True
    assert report["extracted"]["differences"] == []


def test_build_diagnostic_reports_bounded_extracted_payload_difference(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.exe"
    actual = tmp_path / "actual.exe"
    expected.write_bytes(_pe(timestamp=1, checksum=2, tail=b"expected"))
    actual.write_bytes(_pe(timestamp=1, checksum=2, tail=b"actual"))
    expected_tree = tmp_path / "expected-tree"
    actual_tree = tmp_path / "actual-tree"
    expected_tree.mkdir()
    actual_tree.mkdir()
    (expected_tree / "stock-desk.exe").write_bytes(b"one")
    (actual_tree / "stock-desk.exe").write_bytes(b"two")

    report = diagnostics.build_diagnostic(
        expected=expected,
        actual=actual,
        expected_tree=expected_tree,
        actual_tree=actual_tree,
    )

    assert report["classification"] == "payload-difference"
    assert report["extracted"]["equivalent"] is False
    assert report["extracted"]["differences"] == [
        {
            "fields": ["sha256"],
            "path": "stock-desk.exe",
        }
    ]


def test_build_diagnostic_rejects_symlinked_extracted_content(tmp_path: Path) -> None:
    expected = tmp_path / "expected.exe"
    actual = tmp_path / "actual.exe"
    expected.write_bytes(_pe(timestamp=1, checksum=2, tail=b"expected"))
    actual.write_bytes(_pe(timestamp=1, checksum=2, tail=b"actual"))
    expected_tree = tmp_path / "expected-tree"
    actual_tree = tmp_path / "actual-tree"
    expected_tree.mkdir()
    actual_tree.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (expected_tree / "unsafe").symlink_to(outside)
    (actual_tree / "safe").write_bytes(b"safe")

    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match="link"):
        diagnostics.build_diagnostic(
            expected=expected,
            actual=actual,
            expected_tree=expected_tree,
            actual_tree=actual_tree,
        )


def test_windows_repack_integration_captures_mismatch_before_failing() -> None:
    integration = (
        ROOT / "tests/windows/nsis_repack_contract_integration.ps1"
    ).read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    diagnostic = "scripts\\nsis_mismatch_diagnostics.py"
    mismatch = (
        "throw 'fixed NSIS repack does not reproduce the original unsigned candidate'"
    )
    assert "[string]$DiagnosticsRoot" in integration
    assert diagnostic in integration
    assert integration.index(diagnostic) < integration.index(mismatch)
    assert "-DiagnosticsRoot (Join-Path $root 'diagnostics')" in workflow
