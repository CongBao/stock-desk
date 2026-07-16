from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_windows_hosted_automation import (
    HostedAutomationEvidenceError,
    verify_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/windows-hosted-automation/valid.json"


def _verify(path: Path) -> dict[str, object]:
    return verify_evidence(
        path,
        source_sha="1" * 40,
        source_tree="2" * 40,
        candidate_sha256="3" * 64,
    )


def _write_mutation(tmp_path: Path, field: str, value: object) -> Path:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload[field] = value
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_hosted_automation_evidence_passes() -> None:
    evidence = _verify(FIXTURE)

    assert evidence["schema_version"] == "stock-desk-windows-hosted-automation-v1"
    assert evidence["physical_mouse_click"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_method", "win32-sendinput-physical-mouse"),
        ("physical_mouse_click", True),
        ("source_sha", "4" * 40),
        ("candidate_sha256", "5" * 64),
    ],
)
def test_identity_and_input_claims_fail_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    path = _write_mutation(tmp_path, field, value)

    with pytest.raises(HostedAutomationEvidenceError):
        _verify(path)


def test_action_order_is_exact(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["actions"][1], payload["actions"][2] = (
        payload["actions"][2],
        payload["actions"][1],
    )
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HostedAutomationEvidenceError, match="action sequence"):
        _verify(path)


def test_hosted_limitations_are_mandatory(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["hosted_runner_limitations"] = payload["hosted_runner_limitations"][:2]
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HostedAutomationEvidenceError, match="limitations"):
        _verify(path)


def test_unknown_fields_fail_closed(tmp_path: Path) -> None:
    path = _write_mutation(tmp_path, "unexpected", True)

    with pytest.raises(HostedAutomationEvidenceError, match="fields"):
        _verify(path)


def test_action_target_must_match_the_authoritative_host(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["actions"][0]["target"]["process_id"] = 999
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HostedAutomationEvidenceError, match="native target"):
        _verify(path)
