from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import verify_windows_desktop_raw_evidence as verifier


SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40
MAIN_PROOF_SHA256 = "c" * 64
CANDIDATE_SHA256 = "d" * 64
WEBVIEW_INSTALLER_SHA256 = "e" * 64
ADAPTER_SHA256 = "f" * 64
CONTROLLER_REQUEST_SHA256 = "1" * 64
GUEST_HARNESS_SHA256 = "2" * 64
UIA_DRIVER_SHA256 = "3" * 64
BROKER_PUBLIC_KEY_SHA256 = "4" * 64
WORKFLOW_SHA256 = "5" * 64
REPOSITORY = "CongBao/stock-desk"
WORKFLOW = "Installed Windows acceptance"
WORKFLOW_REF = (
    "CongBao/stock-desk/.github/workflows/windows-installed.yml@refs/heads/main"
)
RUN_ID = 4242
RUN_ATTEMPT = 1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_policy(tmp_path: Path) -> tuple[Path, str]:
    policy_path = tmp_path / "snapshot-policy.json"
    policy_bytes = b'{"schema":"test-policy"}\n'
    policy_path.write_bytes(policy_bytes)
    return policy_path, _sha256(policy_bytes)


def _write_packages(tmp_path: Path, case_ids: Sequence[str]) -> list[Path]:
    packages: list[Path] = []
    for index, case_id in enumerate(case_ids):
        package = tmp_path / "packages" / f"package-{index:02d}"
        package.mkdir(parents=True)
        (package / "raw-manifest.json").write_text(
            json.dumps({"case_id": case_id}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        packages.append(package)
    return packages


def _assignments(case_ids: Sequence[str]) -> dict[str, dict[str, object]]:
    return {
        case_id: {"case_id": case_id, "assignment_marker": f"policy:{case_id}"}
        for case_id in case_ids
    }


def _matrix_arguments(
    *, policy_path: Path, policy_sha256: str, output_root: Path
) -> dict[str, object]:
    return {
        "policy_path": policy_path,
        "output_root": output_root,
        "expected_source_sha": SOURCE_SHA,
        "expected_source_tree": SOURCE_TREE,
        "expected_main_proof_sha256": MAIN_PROOF_SHA256,
        "expected_candidate_sha256": CANDIDATE_SHA256,
        "expected_webview_installer_sha256": WEBVIEW_INSTALLER_SHA256,
        "expected_policy_sha256": policy_sha256,
        "expected_adapter_sha256": ADAPTER_SHA256,
        "expected_controller_request_sha256": CONTROLLER_REQUEST_SHA256,
        "expected_guest_harness_sha256": GUEST_HARNESS_SHA256,
        "expected_uia_driver_sha256": UIA_DRIVER_SHA256,
        "broker_public_key": policy_path.parent / "broker-public-key.pem",
        "expected_broker_public_key_sha256": BROKER_PUBLIC_KEY_SHA256,
        "expected_repository": REPOSITORY,
        "expected_workflow": WORKFLOW,
        "expected_workflow_ref": WORKFLOW_REF,
        "expected_workflow_sha256": WORKFLOW_SHA256,
        "expected_run_id": RUN_ID,
        "expected_run_attempt": RUN_ATTEMPT,
    }


def _install_aggregate_fakes(
    monkeypatch: pytest.MonkeyPatch,
    assignments: dict[str, dict[str, object]],
    *,
    fail_case: str | None = None,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def validate_snapshot_policy(value: object) -> dict[str, dict[str, object]]:
        assert value == {"schema": "test-policy"}
        return assignments

    def verify_package(
        package: Path,
        *,
        assignment: dict[str, object],
        **identity: object,
    ) -> dict[str, object]:
        case_id = str(assignment["case_id"])
        calls.append(
            {
                "package": package,
                "assignment": assignment,
                "identity": identity,
            }
        )
        if case_id == fail_case:
            raise verifier.DesktopEvidenceError(f"injected rejection for {case_id}")
        return {
            "schema_version": 2,
            "artifact": "windows-installed-evidence",
            "case_id": case_id,
            "assignment_marker": assignment["assignment_marker"],
            "raw_package_sha256": _sha256(f"raw:{case_id}".encode()),
        }

    monkeypatch.setattr(verifier, "validate_snapshot_policy", validate_snapshot_policy)
    monkeypatch.setattr(verifier, "verify_package", verify_package)
    return calls


def test_verify_matrix_derives_ordered_exact_case_outputs_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_ids = verifier.expected_case_ids()
    policy_path, policy_sha256 = _write_policy(tmp_path)
    packages = _write_packages(tmp_path, tuple(reversed(case_ids)))
    assignments = _assignments(case_ids)
    calls = _install_aggregate_fakes(monkeypatch, assignments)
    output_root = tmp_path / "derived"

    receipt = verifier.verify_matrix(
        packages,
        **_matrix_arguments(
            policy_path=policy_path,
            policy_sha256=policy_sha256,
            output_root=output_root,
        ),
    )

    assert [call["assignment"] for call in calls] == [
        assignments[case_id] for case_id in reversed(case_ids)
    ]
    expected_identity = {
        "expected_source_sha": SOURCE_SHA,
        "expected_source_tree": SOURCE_TREE,
        "expected_main_proof_sha256": MAIN_PROOF_SHA256,
        "expected_candidate_sha256": CANDIDATE_SHA256,
        "expected_webview_installer_sha256": WEBVIEW_INSTALLER_SHA256,
        "expected_policy_sha256": policy_sha256,
        "expected_adapter_sha256": ADAPTER_SHA256,
        "expected_controller_request_sha256": CONTROLLER_REQUEST_SHA256,
        "expected_guest_harness_sha256": GUEST_HARNESS_SHA256,
        "expected_uia_driver_sha256": UIA_DRIVER_SHA256,
        "broker_public_key": policy_path.parent / "broker-public-key.pem",
        "expected_broker_public_key_sha256": BROKER_PUBLIC_KEY_SHA256,
        "expected_repository": REPOSITORY,
        "expected_workflow": WORKFLOW,
        "expected_workflow_ref": WORKFLOW_REF,
        "expected_workflow_sha256": WORKFLOW_SHA256,
        "expected_run_id": RUN_ID,
        "expected_run_attempt": RUN_ATTEMPT,
    }
    assert all(call["identity"] == expected_identity for call in calls)

    assert {path.name for path in output_root.iterdir()} == {
        *(f"{case_id}.json" for case_id in case_ids),
        "acceptance-receipt.json",
    }
    parsed_receipt = json.loads(
        (output_root / "acceptance-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt == parsed_receipt
    assert receipt == {
        "schema": "stock-desk-windows-installed-acceptance-receipt-v2",
        "artifact": "windows-installed-acceptance-receipt",
        "evidence_kind": "observed-windows-vm",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "main_proof_sha256": MAIN_PROOF_SHA256,
        "candidate_sha256": CANDIDATE_SHA256,
        "webview_installer_sha256": WEBVIEW_INSTALLER_SHA256,
        "snapshot_policy_sha256": policy_sha256,
        "adapter_sha256": ADAPTER_SHA256,
        "broker_public_key_sha256": BROKER_PUBLIC_KEY_SHA256,
        "repository": REPOSITORY,
        "workflow": WORKFLOW,
        "workflow_ref": WORKFLOW_REF,
        "workflow_sha256": WORKFLOW_SHA256,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "case_receipts": receipt["case_receipts"],
        "status": "accepted",
    }
    assert [item["case_id"] for item in receipt["case_receipts"]] == list(case_ids)

    for item in receipt["case_receipts"]:
        case_id = item["case_id"]
        case_bytes = (output_root / f"{case_id}.json").read_bytes()
        case_value = json.loads(case_bytes)
        assert item == {
            "case_id": case_id,
            "derived_sha256": _sha256(case_bytes),
            "raw_package_sha256": case_value["raw_package_sha256"],
        }
        assert case_value["assignment_marker"] == f"policy:{case_id}"


@pytest.mark.parametrize("package_count", (10, 12), ids=("missing", "extra"))
def test_verify_matrix_rejects_non_eleven_package_count_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_count: int,
) -> None:
    case_ids = verifier.expected_case_ids()
    policy_path, policy_sha256 = _write_policy(tmp_path)
    observed_case_ids = (
        case_ids[:10] if package_count == 10 else (*case_ids, "extra-policy-case")
    )
    packages = _write_packages(tmp_path, observed_case_ids)
    calls = _install_aggregate_fakes(monkeypatch, _assignments(case_ids))
    output_root = tmp_path / "derived"

    with pytest.raises(verifier.DesktopEvidenceError, match="exactly 11"):
        verifier.verify_matrix(
            packages,
            **_matrix_arguments(
                policy_path=policy_path,
                policy_sha256=policy_sha256,
                output_root=output_root,
            ),
        )

    assert calls == []
    assert not output_root.exists()


def test_verify_matrix_rejects_duplicate_case_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_ids = verifier.expected_case_ids()
    duplicated = (*case_ids[:-1], case_ids[0])
    policy_path, policy_sha256 = _write_policy(tmp_path)
    packages = _write_packages(tmp_path, duplicated)
    calls = _install_aggregate_fakes(monkeypatch, _assignments(case_ids))
    output_root = tmp_path / "derived"

    with pytest.raises(
        verifier.DesktopEvidenceError, match="duplicated or unauthorized"
    ):
        verifier.verify_matrix(
            packages,
            **_matrix_arguments(
                policy_path=policy_path,
                policy_sha256=policy_sha256,
                output_root=output_root,
            ),
        )

    assert len(calls) == 10
    assert not output_root.exists()


def test_verify_matrix_rejects_final_case_set_mismatch_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = verifier.expected_case_ids()
    observed = (*expected[:-1], "foreign-policy-case")
    policy_path, policy_sha256 = _write_policy(tmp_path)
    packages = _write_packages(tmp_path, observed)
    calls = _install_aggregate_fakes(monkeypatch, _assignments(observed))
    output_root = tmp_path / "derived"

    with pytest.raises(verifier.DesktopEvidenceError, match="exact 11-case matrix"):
        verifier.verify_matrix(
            packages,
            **_matrix_arguments(
                policy_path=policy_path,
                policy_sha256=policy_sha256,
                output_root=output_root,
            ),
        )

    assert len(calls) == 11
    assert not output_root.exists()


def test_verify_matrix_verifies_every_case_before_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_ids = verifier.expected_case_ids()
    policy_path, policy_sha256 = _write_policy(tmp_path)
    packages = _write_packages(tmp_path, case_ids)
    rejected_case = case_ids[5]
    calls = _install_aggregate_fakes(
        monkeypatch, _assignments(case_ids), fail_case=rejected_case
    )
    output_root = tmp_path / "derived"

    with pytest.raises(verifier.DesktopEvidenceError, match=rejected_case):
        verifier.verify_matrix(
            packages,
            **_matrix_arguments(
                policy_path=policy_path,
                policy_sha256=policy_sha256,
                output_root=output_root,
            ),
        )

    assert len(calls) == 6
    assert not output_root.exists()


def test_verify_matrix_refuses_existing_output_root_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_ids = verifier.expected_case_ids()
    policy_path, policy_sha256 = _write_policy(tmp_path)
    packages = _write_packages(tmp_path, case_ids)
    _install_aggregate_fakes(monkeypatch, _assignments(case_ids))
    output_root = tmp_path / "derived"
    output_root.mkdir()
    sentinel = output_root / "keep.txt"
    sentinel.write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        verifier.verify_matrix(
            packages,
            **_matrix_arguments(
                policy_path=policy_path,
                policy_sha256=policy_sha256,
                output_root=output_root,
            ),
        )

    assert sentinel.read_text(encoding="utf-8") == "do not overwrite\n"
    assert sorted(path.name for path in output_root.iterdir()) == ["keep.txt"]


def _main_arguments(tmp_path: Path) -> list[str]:
    packages = [tmp_path / f"raw-{index:02d}" for index in range(11)]
    return [
        "--policy",
        str(tmp_path / "policy.json"),
        "--output-root",
        str(tmp_path / "derived"),
        "--source-sha",
        SOURCE_SHA,
        "--source-tree",
        SOURCE_TREE,
        "--main-proof-sha256",
        MAIN_PROOF_SHA256,
        "--candidate-sha256",
        CANDIDATE_SHA256,
        "--webview-installer-sha256",
        WEBVIEW_INSTALLER_SHA256,
        "--snapshot-policy-sha256",
        "6" * 64,
        "--adapter-sha256",
        ADAPTER_SHA256,
        "--controller-request-sha256",
        CONTROLLER_REQUEST_SHA256,
        "--guest-harness-sha256",
        GUEST_HARNESS_SHA256,
        "--uia-driver-sha256",
        UIA_DRIVER_SHA256,
        "--broker-public-key",
        str(tmp_path / "broker-public-key.pem"),
        "--broker-public-key-sha256",
        BROKER_PUBLIC_KEY_SHA256,
        "--repository",
        REPOSITORY,
        "--workflow",
        WORKFLOW,
        "--workflow-ref",
        WORKFLOW_REF,
        "--workflow-sha256",
        WORKFLOW_SHA256,
        "--run-id",
        str(RUN_ID),
        "--run-attempt",
        str(RUN_ATTEMPT),
        *(str(package) for package in packages),
    ]


def test_main_validates_cli_identity_and_delegates_exact_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def verify_matrix(packages: Sequence[Path], **arguments: object) -> dict[str, Any]:
        captured["packages"] = packages
        captured.update(arguments)
        return {"status": "accepted"}

    monkeypatch.setattr(verifier, "verify_matrix", verify_matrix)

    assert verifier.main(_main_arguments(tmp_path)) == 0
    assert captured["packages"] == [
        tmp_path / f"raw-{index:02d}" for index in range(11)
    ]
    assert captured["policy_path"] == tmp_path / "policy.json"
    assert captured["output_root"] == tmp_path / "derived"
    assert captured["expected_source_sha"] == SOURCE_SHA
    assert captured["expected_source_tree"] == SOURCE_TREE
    assert captured["expected_run_id"] == RUN_ID
    assert captured["expected_run_attempt"] == RUN_ATTEMPT


@pytest.mark.parametrize(
    "failure",
    (
        verifier.DesktopEvidenceError("duplicate raw case"),
        OSError("read-only output"),
    ),
    ids=("evidence-rejection", "output-error"),
)
def test_main_returns_failure_and_reports_safe_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    def reject(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise failure

    monkeypatch.setattr(verifier, "verify_matrix", reject)

    assert verifier.main(_main_arguments(tmp_path)) == 1
    assert capsys.readouterr().out == (
        f"windows desktop raw evidence rejected: {failure}\n"
    )
    assert not (tmp_path / "derived").exists()
