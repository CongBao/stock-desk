from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace

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


def test_tree_comparison_classifies_missing_size_and_digest_differences(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    expected.mkdir()
    actual.mkdir()
    (expected / "only-expected").write_bytes(b"expected")
    (actual / "only-actual").write_bytes(b"actual")
    (expected / "shared").write_bytes(b"longer")
    (actual / "shared").write_bytes(b"short")

    comparison = diagnostics._tree_comparison(expected, actual)

    assert comparison["equivalent"] is False
    assert comparison["difference_count"] == 3
    assert comparison["differences"] == [
        {"path": "only-actual", "fields": ["missing-expected"]},
        {"path": "only-expected", "fields": ["missing-actual"]},
        {"path": "shared", "fields": ["size", "sha256"]},
    ]


def test_hash_file_rejects_size_limit_and_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")

    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match="size limit"):
        diagnostics._hash_file(payload, "payload", limit=1)

    initial = payload.stat()
    observations = [
        initial,
        SimpleNamespace(
            st_size=initial.st_size,
            st_mtime_ns=initial.st_mtime_ns + 1,
            st_ctime_ns=initial.st_ctime_ns,
            st_ino=initial.st_ino,
        ),
    ]
    monkeypatch.setattr(
        diagnostics, "_regular_metadata", lambda _path, _field: observations.pop(0)
    )
    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match="changed"):
        diagnostics._hash_file(payload, "payload", limit=1024)


def test_chunked_prefix_and_suffix_scans_close_short_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diagnostics, "CHUNK_SIZE", 2)

    assert diagnostics._common_prefix(BytesIO(b"abcd"), BytesIO(b"abcd"), 4) == 4
    assert diagnostics._common_prefix(BytesIO(b"abcX"), BytesIO(b"abcY"), 4) == 3
    assert diagnostics._common_suffix(BytesIO(b"abcd"), BytesIO(b"abcd"), 4, 4, 4) == 4
    assert diagnostics._common_suffix(BytesIO(b"Xbcd"), BytesIO(b"Ybcd"), 4, 4, 4) == 3

    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match="prefix scan"):
        diagnostics._common_prefix(BytesIO(b""), BytesIO(b""), 1)
    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match="suffix scan"):
        diagnostics._common_suffix(BytesIO(b""), BytesIO(b""), 1, 1, 1)


@pytest.mark.parametrize(
    ("limit_name", "limit", "message"),
    [
        ("MAX_EXTRACTED_FILES", 1, "too many files"),
        ("MAX_EXTRACTED_TOTAL_BYTES", 1, "too large"),
    ],
)
def test_tree_inventory_enforces_count_and_total_size_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    message: str,
) -> None:
    root = tmp_path / limit_name
    root.mkdir()
    (root / "a").write_bytes(b"aa")
    (root / "b").write_bytes(b"bb")
    monkeypatch.setattr(diagnostics, limit_name, limit)

    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match=message):
        diagnostics._tree_inventory(root, "tree")


def test_low_level_inputs_reject_missing_nonfile_and_invalid_pe(tmp_path: Path) -> None:
    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match="cannot inspect"):
        diagnostics._regular_metadata(tmp_path / "missing", "missing")
    with pytest.raises(diagnostics.NsisMismatchDiagnosticError, match="regular"):
        diagnostics._regular_metadata(tmp_path, "directory")

    invalid = tmp_path / "invalid.exe"
    invalid.write_bytes(b"not-a-pe")
    with pytest.raises(diagnostics.NsisMismatchDiagnosticError):
        diagnostics._read_pe_header(invalid, "invalid")


def test_main_writes_new_canonical_report_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = tmp_path / "expected.exe"
    actual = tmp_path / "actual.exe"
    expected_tree = tmp_path / "expected-tree"
    actual_tree = tmp_path / "actual-tree"
    output = tmp_path / "diagnostic.json"
    report = {"schema_version": 1, "artifact": "diagnostic"}
    monkeypatch.setattr(diagnostics, "build_diagnostic", lambda **_kwargs: report)

    arguments = [
        "--expected",
        str(expected),
        "--actual",
        str(actual),
        "--expected-tree",
        str(expected_tree),
        "--actual-tree",
        str(actual_tree),
        "--output",
        str(output),
    ]
    assert diagnostics.main(arguments) == 0
    assert json.loads(output.read_bytes()) == report

    assert diagnostics.main(arguments) == 1
    assert "cannot write diagnostic output" in capsys.readouterr().err


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
    diagnostic_repack = "diagnose-repack-mismatch"
    mismatch = (
        "throw 'fixed NSIS repack does not reproduce the original unsigned candidate'"
    )
    assert "[string]$DiagnosticsRoot" in integration
    assert diagnostic_repack in integration
    assert diagnostic in integration
    assert integration.index(diagnostic_repack) < integration.index(diagnostic)
    assert integration.index(diagnostic) < integration.index(mismatch)
    assert (
        "$diagnosticReportPath = Join-Path $DiagnosticsRoot "
        "'nsis-mismatch-diagnostic.json'"
    ) in integration
    assert "Write-Host 'BEGIN_NSIS_MISMATCH_DIAGNOSTIC'" in integration
    assert (
        "Get-Content -LiteralPath $diagnosticReportPath -Raw | Write-Host"
        in integration
    )
    assert "Write-Host 'END_NSIS_MISMATCH_DIAGNOSTIC'" in integration
    assert "-DiagnosticsRoot (Join-Path $root 'diagnostics')" in workflow


def test_direct_script_entrypoint_bootstraps_repository_imports(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/nsis_mismatch_diagnostics.py"),
            "--help",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Create bounded diagnostics" in completed.stdout
