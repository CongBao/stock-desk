from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import verify_windows_installed_evidence as verifier


FIXTURES = Path("tests/fixtures/windows-installed-evidence")
SCHEMA = Path("schemas/windows-installed-evidence-v1.schema.json")
SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40
MAIN_PROOF_SHA256 = "c" * 64
CANDIDATE_SHA256 = "d" * 64
WEBVIEW_SHA256 = "e" * 64
WORKFLOW = "Installed Windows validation"
RUN_ID = 424242
RUN_ATTEMPT = 1
JOB_ID_PREFIX = "installed"
VALID_NAMES = ("valid-preinstalled.json", "valid-absent.json", "valid-failure.json")


def _load(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _verify(raw: object) -> dict[str, Any]:
    return verifier.validate_evidence(
        raw,
        expected_source_sha=SOURCE_SHA,
        expected_source_tree=SOURCE_TREE,
        expected_main_proof_sha256=MAIN_PROOF_SHA256,
        expected_candidate_sha256=CANDIDATE_SHA256,
        expected_webview_installer_sha256=WEBVIEW_SHA256,
        expected_workflow=WORKFLOW,
        expected_run_id=RUN_ID,
        expected_run_attempt=RUN_ATTEMPT,
        expected_job_id_prefix=JOB_ID_PREFIX,
    )


def _matrix(raw: list[object]) -> tuple[dict[str, Any], ...]:
    return verifier.validate_matrix(
        raw,
        expected_source_sha=SOURCE_SHA,
        expected_source_tree=SOURCE_TREE,
        expected_main_proof_sha256=MAIN_PROOF_SHA256,
        expected_candidate_sha256=CANDIDATE_SHA256,
        expected_webview_installer_sha256=WEBVIEW_SHA256,
        expected_workflow=WORKFLOW,
        expected_run_id=RUN_ID,
        expected_run_attempt=RUN_ATTEMPT,
        expected_job_id_prefix=JOB_ID_PREFIX,
    )


def _assert_closed_objects(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
        for child in node.values():
            _assert_closed_objects(child)
    elif isinstance(node, list):
        for child in node:
            _assert_closed_objects(child)


def test_public_schema_closes_every_object_and_has_no_caller_pass_field() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    _assert_closed_objects(schema)
    assert '"passed"' not in SCHEMA.read_text(encoding="utf-8")
    assert set(schema["properties"]["scenario"]["enum"]) == verifier.SCENARIOS


def test_three_valid_fixtures_cover_complete_first_attempt_matrix() -> None:
    documents = _matrix([_load(name) for name in VALID_NAMES])

    assert {item["scenario"] for item in documents} == verifier.SCENARIOS
    assert {
        item["system"]["family"]
        for item in documents  # type: ignore[index]
    } == {"windows-10", "windows-11"}
    assert all(item["execution"]["run_attempt"] == 1 for item in documents)  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_sha", "9" * 40),
        ("source_tree", "9" * 40),
        ("main_proof_sha256", "9" * 64),
        ("candidate_sha256", "9" * 64),
        ("webview_installer_sha256", "9" * 64),
    ],
)
def test_exact_source_proof_candidate_and_webview_identity_is_required(
    field: str, replacement: str
) -> None:
    evidence = _load("valid-preinstalled.json")
    evidence["identity"][field] = replacement

    with pytest.raises(verifier.InstalledEvidenceError, match="identity mismatch"):
        _verify(evidence)


@pytest.mark.parametrize(
    "location",
    ["top", "identity", "runtime", "record"],
)
def test_unknown_fields_and_caller_supplied_pass_are_rejected(location: str) -> None:
    evidence = _load("valid-preinstalled.json")
    if location == "top":
        evidence["passed"] = True
    elif location == "identity":
        evidence["identity"]["passed"] = True
    elif location == "runtime":
        evidence["webview"]["before"]["passed"] = True
    else:
        evidence["diagnostic_summary"]["records"][0]["passed"] = True

    with pytest.raises(verifier.InstalledEvidenceError, match="unknown fields"):
        _verify(evidence)


@pytest.mark.parametrize(
    "mutation",
    ["admin", "installer-elevated", "sidecar-elevated", "uac", "elevation-request"],
)
def test_admin_elevation_and_uac_evidence_fail_closed(mutation: str) -> None:
    evidence = _load("valid-absent.json")
    if mutation == "admin":
        evidence["account"]["is_admin"] = True
    elif mutation == "installer-elevated":
        evidence["processes"]["installer"]["elevated"] = True
    elif mutation == "sidecar-elevated":
        evidence["processes"]["sidecar"]["elevated"] = True
    elif mutation == "uac":
        evidence["security"]["uac_prompt_count"] = 1
    else:
        evidence["security"]["elevation_requested"] = True

    with pytest.raises(verifier.InstalledEvidenceError):
        _verify(evidence)


@pytest.mark.parametrize("field", ["run_attempt", "scenario_attempt"])
def test_retry_only_evidence_cannot_replace_first_attempt(field: str) -> None:
    evidence = _load("valid-preinstalled.json")
    evidence["execution"][field] = 2

    with pytest.raises(verifier.InstalledEvidenceError, match="retry-only"):
        _verify(evidence)


@pytest.mark.parametrize("field", ["workflow", "run_id", "job_id"])
def test_actions_execution_identity_cannot_be_self_reported(field: str) -> None:
    evidence = _load("valid-preinstalled.json")
    evidence["execution"][field] = "forged" if field != "run_id" else RUN_ID + 1

    with pytest.raises(verifier.InstalledEvidenceError, match="execution identity"):
        _verify(evidence)


@pytest.mark.parametrize(
    "mutation",
    [
        "preinstalled-reinstall",
        "absent-not-installed",
        "failure-runtime-present",
        "failure-zero-exit",
    ],
)
def test_webview_scenario_state_contradictions_are_rejected(mutation: str) -> None:
    if mutation == "preinstalled-reinstall":
        evidence = _load("valid-preinstalled.json")
        evidence["webview"]["installation"].update(attempted=True, exit_code=0)
    elif mutation == "absent-not-installed":
        evidence = _load("valid-absent.json")
        evidence["webview"]["installation"].update(attempted=False, exit_code=None)
    else:
        evidence = _load("valid-failure.json")
        if mutation == "failure-runtime-present":
            evidence["webview"]["after"] = copy.deepcopy(
                _load("valid-absent.json")["webview"]["after"]
            )
        else:
            evidence["webview"]["installation"]["exit_code"] = 0

    with pytest.raises(
        verifier.InstalledEvidenceError, match="(contradictory|installed|failure)"
    ):
        _verify(evidence)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("product_guid", "{26A24AE4-039D-4CA4-87B4-2F64180138F0}"),
        ("version", "0.0.0.0"),
        ("version", "119.0.9999.9999"),
        ("version", "120.0.bad.91"),
    ],
)
def test_only_supported_production_webview_runtime_is_accepted(
    field: str, replacement: str
) -> None:
    evidence = _load("valid-preinstalled.json")
    evidence["webview"]["before"][field] = replacement

    with pytest.raises(verifier.InstalledEvidenceError, match="WebView2|webview"):
        _verify(evidence)


def test_webview_failure_cannot_leave_launchable_or_partial_application() -> None:
    evidence = _load("valid-failure.json")
    evidence["install"].update(
        application_files_present=True, shortcut_present=True, launchable=True
    )

    with pytest.raises(verifier.InstalledEvidenceError, match="launchable"):
        _verify(evidence)


def test_v1_canary_must_remain_byte_identical() -> None:
    evidence = _load("valid-absent.json")
    evidence["v1_canary"]["after"]["content_sha256"] = "0" * 64

    with pytest.raises(verifier.InstalledEvidenceError, match="v1 canary changed"):
        _verify(evidence)


@pytest.mark.parametrize(
    "mutation",
    ["missing-record", "secret", "username", "path", "entry-count"],
)
def test_redacted_diagnostic_evidence_is_complete_and_consistent(mutation: str) -> None:
    evidence = _load("valid-preinstalled.json")
    summary = evidence["diagnostic_summary"]
    if mutation == "missing-record":
        summary["records"] = [
            item for item in summary["records"] if item["kind"] != "window-capture"
        ]
        summary["entry_count"] -= 1
    elif mutation == "entry-count":
        summary["entry_count"] += 1
    else:
        summary["redaction_scan"][
            f"{mutation}_match_count"
            if mutation != "path"
            else "absolute_path_match_count"
        ] = 1

    with pytest.raises(
        verifier.InstalledEvidenceError, match="(missing|redacted|inconsistent)"
    ):
        _verify(evidence)


@pytest.mark.parametrize(
    ("family", "version", "build", "architecture"),
    [
        ("windows-10", "21H2", 19044, "x86_64"),
        ("windows-10", "22H2", 19045, "arm64"),
        ("windows-11", "24H2", 19045, "x86_64"),
    ],
)
def test_unsupported_systems_and_architecture_are_rejected(
    family: str, version: str, build: int, architecture: str
) -> None:
    evidence = _load("valid-preinstalled.json")
    evidence["system"].update(
        family=family,
        display_version=version,
        build_number=build,
        architecture=architecture,
    )

    with pytest.raises(verifier.InstalledEvidenceError, match="(unsupported|x86_64)"):
        _verify(evidence)


def test_matrix_rejects_missing_duplicate_or_cross_run_scenarios() -> None:
    valid = [_load(name) for name in VALID_NAMES]
    with pytest.raises(verifier.InstalledEvidenceError, match="exactly one"):
        _matrix(valid[:2])

    duplicate = [valid[0], copy.deepcopy(valid[0]), valid[2]]
    duplicate[1]["execution"]["attempt_id"] = "different-attempt"
    with pytest.raises(verifier.InstalledEvidenceError, match="exactly one"):
        _matrix(duplicate)

    cross_run = copy.deepcopy(valid)
    cross_run[1]["execution"]["run_id"] = 999
    with pytest.raises(verifier.InstalledEvidenceError, match="execution identity"):
        _matrix(cross_run)


def test_matrix_requires_windows_and_profile_edge_coverage() -> None:
    missing_windows = [_load(name) for name in VALID_NAMES]
    missing_windows[0]["system"] = copy.deepcopy(missing_windows[1]["system"])
    missing_windows[0]["system"]["image_sha256"] = "0" * 64
    with pytest.raises(
        verifier.InstalledEvidenceError, match="Windows 10 and Windows 11"
    ):
        _matrix(missing_windows)

    missing_profile = [_load(name) for name in VALID_NAMES]
    for item in missing_profile:
        item["account"]["username_contains_non_ascii"] = False
        item["account"]["profile_path_contains_space"] = False
    with pytest.raises(verifier.InstalledEvidenceError, match="non-ASCII"):
        _matrix(missing_profile)


def test_webview_digest_and_microsoft_signer_are_bound() -> None:
    digest = _load("valid-absent.json")
    digest["webview"]["installation"]["installer_sha256"] = "0" * 64
    with pytest.raises(verifier.InstalledEvidenceError, match="digest"):
        _verify(digest)

    signer = _load("valid-preinstalled.json")
    signer["webview"]["before"]["signer"]["subject"] = "CN=Untrusted"
    with pytest.raises(verifier.InstalledEvidenceError, match="Microsoft signer"):
        _verify(signer)


def test_missing_symlink_duplicate_and_invalid_fixture_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(verifier.InstalledEvidenceError, match="missing"):
        verifier.read_evidence(tmp_path / "missing.json")

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(verifier.InstalledEvidenceError, match="missing"):
        verifier.read_evidence(link)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(verifier.InstalledEvidenceError, match="duplicate JSON"):
        verifier.read_evidence(duplicate)

    for path in sorted(FIXTURES.glob("invalid-*.json")):
        with pytest.raises(verifier.InstalledEvidenceError):
            _verify(verifier.read_evidence(path))


def test_cli_verifies_only_complete_exact_identity_matrix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = [str(FIXTURES / name) for name in VALID_NAMES]
    common = [
        "--source-sha",
        SOURCE_SHA,
        "--source-tree",
        SOURCE_TREE,
        "--main-proof-sha256",
        MAIN_PROOF_SHA256,
        "--candidate-sha256",
        CANDIDATE_SHA256,
        "--webview-installer-sha256",
        WEBVIEW_SHA256,
        "--workflow",
        WORKFLOW,
        "--run-id",
        str(RUN_ID),
        "--run-attempt",
        str(RUN_ATTEMPT),
        "--job-id-prefix",
        JOB_ID_PREFIX,
    ]

    assert verifier.main([*paths, *common]) == 0
    assert "3 first-attempt scenarios" in capsys.readouterr().out
    assert verifier.main([*paths[:2], *common]) == 1
    assert "exactly one" in capsys.readouterr().err
