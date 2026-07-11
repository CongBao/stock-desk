from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_windows_bundle_verifier_contract_is_fail_closed_and_sbom_honest() -> None:
    verifier = (ROOT / "scripts" / "verify_windows_desktop_bundle.py").read_text(
        encoding="utf-8"
    )
    comparer = (ROOT / "scripts" / "compare_windows_payloads.py").read_text(
        encoding="utf-8"
    )

    assert "O_NOFOLLOW" in verifier
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in verifier
    assert "changed while hashing" in verifier
    assert '"not-produced"' in verifier
    assert "cyclonedx-reserved" in verifier
    assert "pe-timestamp" in comparer
    assert "pe-checksum" in comparer
    assert "allowed_differences" in comparer
