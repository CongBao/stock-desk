from __future__ import annotations

import hashlib
import json
from pathlib import Path
from copy import deepcopy
import shutil
from datetime import datetime
from decimal import Decimal

import pytest

from scripts.v1_backtest_oracle import load_inputs, load_oracle
from scripts.v1_backtest_oracle import project_completed
from scripts.capture_packaged_backtest_semantics import normalize_resumed_semantics
from scripts.prepare_windows_packaged_backtest_evidence import prepare, switch_fixture
from stock_desk.backtest.service import BacktestIntent
from stock_desk.market.types import Adjustment, Period
from tests.backtest_test_helpers import BacktestHarness
from scripts.verify_packaged_backtest_evidence import (
    EvidenceError,
    create_promotion,
    verify,
    verify_promotion,
)
from scripts.verify_windows_desktop_bundle import manifest_digest
from scripts.artifact_manifest import (
    build_manifest,
    create_attestation_binding,
    verify_artifact_root_closure,
    write_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
ORACLE_PATH = ROOT / "tests/fixtures/backtest/v1_0_oracle.json"
INPUTS_PATH = ROOT / "tests/fixtures/backtest/v1_0_oracle_inputs.json"
GENERATOR_PATH = ROOT / "scripts/v1_backtest_oracle.py"
SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40
INSTALLER_BYTES = b"packaged backtest installer fixture\n"
CANDIDATE = hashlib.sha256(INSTALLER_BYTES).hexdigest()
CAPTURE_NONCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_semantics(report: dict[str, object]) -> dict[str, object]:
    return dict(report)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    inputs = load_inputs(INPUTS_PATH)
    oracle = load_oracle(ORACLE_PATH, inputs_path=INPUTS_PATH)
    oracle_binding = {
        "source": oracle["source"],
        "oracle_sha256": _sha(ORACLE_PATH),
        "inputs_sha256": _sha(INPUTS_PATH),
        "generator_sha256": _sha(GENERATOR_PATH),
        "payload_digest": oracle["payload_digest"],
    }
    formula_inputs = {item["id"]: item for item in inputs["matrix"]["formulas"]}
    formulas = {
        formula_id: {
            "name": item["name"],
            "formula_id": "11111111-1111-4111-8111-111111111111",
            "version_id": (
                "22222222-2222-4222-8222-222222222222"
                if formula_id == "macd"
                else "33333333-3333-4333-8333-333333333333"
            ),
            "checksum": oracle["cases"][f"{formula_id}_single_1d"]["semantic"][
                "report"
            ]["formula_checksum"],
            "parameters": item["parameters"],
        }
        for formula_id, item in formula_inputs.items()
    }
    expected_ids = [
        f"{formula}_{scope}_{period}"
        for formula in ("macd", "custom")
        for scope in ("single", "pool")
        for period in ("1d", "1w", "60m")
    ]
    seed = {
        "schema_version": "stock-desk-packaged-backtest-seed-v1",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "public_fixture": True,
        "read_only_demo": False,
        "oracle": oracle_binding,
        "matrix_case_ids": expected_ids,
        "formulas": formulas,
    }
    seed_path = tmp_path / "packaged-backtest-seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    cells = []
    for index, case_id in enumerate(expected_ids, start=1):
        formula, scope, period = case_id.split("_", maxsplit=2)
        report = oracle["cases"][case_id]["semantic"]["report"]
        cells.append(
            {
                "case_id": case_id,
                "formula": {
                    "kind": formula,
                    "version_id": formulas[formula]["version_id"],
                    "checksum": formulas[formula]["checksum"],
                    "parameters": formulas[formula]["parameters"],
                },
                "scope": scope,
                "period": period,
                "run_id": f"44444444-4444-4444-8444-{index:012d}",
                "task_id": f"55555555-5555-4555-8555-{index:012d}",
                "snapshot_id": "sha256:" + "d" * 64,
                "result_hash": "sha256:" + "e" * 64,
                "worker_id": "tauri-sidecar-" + "a" * 32,
                "oracle_semantic_digest": oracle["cases"][case_id]["semantic_digest"],
                "preflight_sha256": "1" * 64,
                "overview_sha256": "2" * 64,
                "report_sha256": "3" * 64,
                "collections_sha256": "4" * 64,
                "report_semantics": _report_semantics(report),
                "semantic_projection": oracle["cases"][case_id]["semantic"],
            }
        )
    special_cases = []
    for index, case_id in enumerate(
        (
            "a_share_constraints_60m",
            "open_position_costs_1d",
            "partial_pool_gap_1d",
        ),
        start=20,
    ):
        special_cases.append(
            {
                "case_id": case_id,
                "run_id": f"44444444-4444-4444-8444-{index:012d}",
                "task_id": f"55555555-5555-4555-8555-{index:012d}",
                "snapshot_id": "sha256:" + "d" * 64,
                "result_hash": "sha256:" + "e" * 64,
                "worker_id": "tauri-sidecar-" + "a" * 32,
                "oracle_semantic_digest": oracle["cases"][case_id]["semantic_digest"],
                "preflight_sha256": "1" * 64,
                "overview_sha256": "2" * 64,
                "report_sha256": "3" * 64,
                "collections_sha256": "4" * 64,
                "semantic_projection": oracle["cases"][case_id]["semantic"],
            }
        )
    checkpoint_expected = deepcopy(oracle["cases"]["custom_pool_1d"]["semantic"])
    resumed_raw = deepcopy(checkpoint_expected)
    resumed_logs = resumed_raw["collections"]["logs"]
    for item in resumed_logs[2:]:
        item["ordinal"] += 1
    resumed_logs.insert(
        2,
        {
            "detail": {"attempt": 2},
            "level": "info",
            "message": "run_started",
            "ordinal": 2,
        },
    )
    evidence = {
        "schema_version": "stock-desk-packaged-backtest-evidence-v1",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "candidate_sha256": CANDIDATE,
        "capture_nonce": CAPTURE_NONCE,
        "actual_packaged_tauri": True,
        "actual_tauri_webview": True,
        "authenticated_host_ipc": True,
        "packaged_sidecar_worker": True,
        "read_only_demo": False,
        "submission_surface": "installed-tauri-webview-host-ipc",
        "seed": {"file": seed_path.name, "sha256": _sha(seed_path)},
        "oracle": oracle_binding,
        "cells": cells,
        "special_cases": special_cases,
        "checkpoint": {
            "case_id": "custom_pool_1d_checkpoint_resume",
            "run_id": "66666666-6666-4666-8666-666666666666",
            "task_id": "77777777-7777-4777-8777-777777777777",
            "snapshot_id": "sha256:" + "8" * 64,
            "result_hash": "sha256:" + "9" * 64,
            "worker_before": "tauri-sidecar-" + "a" * 32,
            "worker_after": "tauri-sidecar-" + "b" * 32,
            "runtime_state_before": "ready",
            "runtime_state_recovery": "recovery",
            "runtime_state_after": "ready",
            "runtime_restart_observed": True,
            "recovery_required": True,
            "report_sha256": "0" * 64,
            "baseline_run_id": "88888888-8888-4888-8888-888888888888",
            "baseline_task_id": "99999999-9999-4999-8999-999999999999",
            "baseline_snapshot_id": "sha256:" + "8" * 64,
            "baseline_result_hash": "sha256:" + "9" * 64,
            "baseline_worker_id": "tauri-sidecar-" + "a" * 32,
            "uninterrupted_semantic_projection": checkpoint_expected,
            "resumed_semantic_projection": resumed_raw,
            "resumed_normalized_projection": checkpoint_expected,
            "normalization": {
                "allowed_difference_id": "desktop-checkpoint-extension-v1.1",
                "removed_log": {
                    "detail": {"attempt": 2},
                    "level": "info",
                    "message": "run_started",
                    "ordinal": 2,
                },
                "renumbered_field": "collections.logs[].ordinal",
            },
        },
    }
    evidence_path = tmp_path / "packaged-backtest-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    host = {
        "schema_version": "stock-desk-packaged-backtest-host-observation-v1",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "candidate_sha256": CANDIDATE,
        "capture_nonce": CAPTURE_NONCE,
        "capture_scope": "installed-current-user-tauri-webview",
        "host_ipc_command": "desktop_api_request",
        "host_pid": 1001,
        "main_window_handle": 2002,
        "installed_host_sha256": "6" * 64,
        "isolated_webview_process_ids": [3003, 3004],
        "sidecar_before": {"pid": 4004, "executable_sha256": "7" * 64},
        "sidecar_after": {"pid": 5005, "executable_sha256": "7" * 64},
        "checkpoint": {
            "run_id": evidence["checkpoint"]["run_id"],
            "task_id": evidence["checkpoint"]["task_id"],
        },
        "evidence_sha256": _sha(evidence_path),
    }
    host_path = tmp_path / "packaged-backtest-host-observation.json"
    host_path.write_text(json.dumps(host), encoding="utf-8")
    webview = {
        "schema_version": "stock-desk-packaged-webview-evidence-v1",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "actual_tauri_webview": True,
        "packaged_backtests": {
            "manifest": evidence_path.name,
            "schema_version": evidence["schema_version"],
            "cell_count": len(evidence["cells"]),
            "checkpoint_run_id": evidence["checkpoint"]["run_id"],
        },
    }
    webview_path = tmp_path / "tauri-webview-evidence.json"
    webview_path.write_text(json.dumps(webview), encoding="utf-8")
    desktop = {
        "schema_version": "stock-desk-windows-desktop-evidence-v1",
        "source_sha": SOURCE_SHA,
        "source_tree": SOURCE_TREE,
        "candidate_sha256": CANDIDATE,
        "actual_packaged_tauri": True,
        "webview": {"manifest": webview_path.name, "sha256": _sha(webview_path)},
        "packaged_backtests": {
            "manifest": evidence_path.name,
            "sha256": _sha(evidence_path),
            "seed": seed_path.name,
            "seed_sha256": _sha(seed_path),
            "host_observation": host_path.name,
            "host_observation_sha256": _sha(host_path),
        },
    }
    desktop_path = tmp_path / "windows-desktop-evidence.json"
    desktop_path.write_text(json.dumps(desktop), encoding="utf-8")
    installer_path = tmp_path / "stock-desk-unsigned-nsis.exe"
    installer_path.write_bytes(INSTALLER_BYTES)
    files = [
        {
            "path": "MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
            "size": 1,
            "sha256": "5" * 64,
            "role": "webview2-offline-installer",
        },
        {
            "path": "stock-desk-desktop.exe",
            "size": 1,
            "sha256": host["installed_host_sha256"],
            "role": "desktop-host",
        },
        {
            "path": "stock-desk-sidecar.exe",
            "size": 1,
            "sha256": host["sidecar_before"]["executable_sha256"],
            "role": "sidecar",
        },
        {
            "path": "stock-desk-unsigned-nsis.exe",
            "size": len(INSTALLER_BYTES),
            "sha256": CANDIDATE,
            "role": "nsis-installer",
        },
        {
            "path": "uninstall.exe",
            "size": 1,
            "sha256": "8" * 64,
            "role": "nsis-uninstaller",
        },
    ]
    installer_record = files[3]
    bundle = {
        "schema_version": 1,
        "artifact": "windows-desktop-bundle",
        "release": {
            "version": "1.1.0-alpha.1",
            "channel": "prerelease",
            "signature": "unsigned",
        },
        "source_sha": SOURCE_SHA,
        "toolchain": {"python": "3.12"},
        "locks": {"uv.lock": "f" * 64},
        "files": files,
        "installer": installer_record,
        "sbom": {"status": "not-produced", "hook": "cyclonedx-reserved"},
    }
    bundle["manifest_sha256"] = manifest_digest(bundle)
    bundle_path = tmp_path / "windows-desktop-bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    return (
        evidence_path,
        seed_path,
        host_path,
        desktop_path,
        installer_path,
        bundle_path,
    )


def test_packaged_backtest_evidence_verifier_accepts_complete_bound_matrix(
    tmp_path: Path,
) -> None:
    evidence, seed, host, desktop, installer, bundle = _fixture(tmp_path)

    verify(
        evidence,
        seed,
        host,
        desktop,
        installer,
        bundle,
        source_sha=SOURCE_SHA,
        source_tree=SOURCE_TREE,
        candidate_sha256=CANDIDATE,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "demo",
        "missing_cell",
        "invalid_run_uuid",
        "collections_replaced",
        "special_missing",
        "special_content",
        "special_identity_collision",
        "checkpoint_identity_collision",
        "checkpoint_duplicate_symbol",
        "spliced_matrix_worker",
        "same_worker",
        "same_sidecar_pid",
        "host_nonce",
        "host_evidence_hash",
        "outer_host_hash",
        "outer_webview_hash",
        "webview_checkpoint",
        "installer_bytes",
        "bundle_host_hash",
        "bundle_sidecar_hash",
        "bundle_installer_hash",
    ),
)
def test_packaged_backtest_evidence_verifier_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    evidence, seed, host, desktop, installer, bundle = _fixture(tmp_path)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    if mutation == "demo":
        payload["read_only_demo"] = True
    elif mutation == "missing_cell":
        payload["cells"].pop()
    elif mutation == "invalid_run_uuid":
        payload["cells"][0]["run_id"] = "not-a-uuid"
    elif mutation == "collections_replaced":
        payload["cells"][0]["semantic_projection"]["collections"] = {}
    elif mutation == "special_missing":
        payload["special_cases"].pop()
    elif mutation == "special_content":
        payload["special_cases"][0]["semantic_projection"]["order_events"] = []
    elif mutation == "special_identity_collision":
        payload["special_cases"][0]["run_id"] = payload["cells"][0]["run_id"]
        payload["special_cases"][0]["task_id"] = payload["cells"][0]["task_id"]
    elif mutation == "checkpoint_duplicate_symbol":
        symbols = payload["checkpoint"]["resumed_semantic_projection"]["symbols"]
        symbols.append(deepcopy(symbols[0]))
    elif mutation == "checkpoint_identity_collision":
        payload["checkpoint"]["baseline_run_id"] = payload["cells"][0]["run_id"]
        payload["checkpoint"]["baseline_task_id"] = payload["cells"][0]["task_id"]
    elif mutation == "spliced_matrix_worker":
        payload["cells"][0]["worker_id"] = "tauri-sidecar-" + "c" * 32
    elif mutation == "same_worker":
        payload["checkpoint"]["worker_after"] = payload["checkpoint"]["worker_before"]
    elif mutation == "same_sidecar_pid":
        host_payload = json.loads(host.read_text(encoding="utf-8"))
        host_payload["sidecar_after"]["pid"] = host_payload["sidecar_before"]["pid"]
        host.write_text(json.dumps(host_payload), encoding="utf-8")
    elif mutation == "host_nonce":
        host_payload = json.loads(host.read_text(encoding="utf-8"))
        host_payload["capture_nonce"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        host.write_text(json.dumps(host_payload), encoding="utf-8")
    elif mutation == "host_evidence_hash":
        host_payload = json.loads(host.read_text(encoding="utf-8"))
        host_payload["evidence_sha256"] = "8" * 64
        host.write_text(json.dumps(host_payload), encoding="utf-8")
    elif mutation == "outer_host_hash":
        desktop_payload = json.loads(desktop.read_text(encoding="utf-8"))
        desktop_payload["packaged_backtests"]["host_observation_sha256"] = "9" * 64
        desktop.write_text(json.dumps(desktop_payload), encoding="utf-8")
    elif mutation == "outer_webview_hash":
        desktop_payload = json.loads(desktop.read_text(encoding="utf-8"))
        desktop_payload["webview"]["sha256"] = "9" * 64
        desktop.write_text(json.dumps(desktop_payload), encoding="utf-8")
    elif mutation == "webview_checkpoint":
        webview = desktop.with_name("tauri-webview-evidence.json")
        webview_payload = json.loads(webview.read_text(encoding="utf-8"))
        webview_payload["packaged_backtests"]["checkpoint_run_id"] = (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        webview.write_text(json.dumps(webview_payload), encoding="utf-8")
    elif mutation == "installer_bytes":
        installer.write_bytes(INSTALLER_BYTES + b"tampered")
    elif mutation in {
        "bundle_host_hash",
        "bundle_sidecar_hash",
        "bundle_installer_hash",
    }:
        bundle_payload = json.loads(bundle.read_text(encoding="utf-8"))
        role = {
            "bundle_host_hash": "desktop-host",
            "bundle_sidecar_hash": "sidecar",
            "bundle_installer_hash": "nsis-installer",
        }[mutation]
        record = next(item for item in bundle_payload["files"] if item["role"] == role)
        record["sha256"] = "0" * 64
        if role == "nsis-installer":
            bundle_payload["installer"] = record
        bundle_payload["manifest_sha256"] = manifest_digest(bundle_payload)
        bundle.write_text(json.dumps(bundle_payload), encoding="utf-8")
    if mutation in {
        "demo",
        "missing_cell",
        "invalid_run_uuid",
        "collections_replaced",
        "special_missing",
        "special_content",
        "special_identity_collision",
        "checkpoint_identity_collision",
        "checkpoint_duplicate_symbol",
        "spliced_matrix_worker",
        "same_worker",
    }:
        evidence.write_text(json.dumps(payload), encoding="utf-8")
    if mutation in {"special_identity_collision", "checkpoint_identity_collision"}:
        host_payload = json.loads(host.read_text(encoding="utf-8"))
        host_payload["evidence_sha256"] = _sha(evidence)
        host.write_text(json.dumps(host_payload), encoding="utf-8")
        desktop_payload = json.loads(desktop.read_text(encoding="utf-8"))
        desktop_payload["packaged_backtests"]["sha256"] = _sha(evidence)
        desktop_payload["packaged_backtests"]["host_observation_sha256"] = _sha(host)
        desktop.write_text(json.dumps(desktop_payload), encoding="utf-8")

    with pytest.raises(EvidenceError):
        verify(
            evidence,
            seed,
            host,
            desktop,
            installer,
            bundle,
            source_sha=SOURCE_SHA,
            source_tree=SOURCE_TREE,
            candidate_sha256=CANDIDATE,
        )


def _promotion_fixture(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    evidence, seed, host, desktop, installer, bundle = _fixture(source)
    root = tmp_path / "candidate"
    packaged = root / "packaged-backtest"
    packaged.mkdir(parents=True)
    promoted_installer = root / "stock-desk-1.1.0-alpha.1-unsigned-x64-setup.exe"
    promoted_bundle = root / "windows-desktop-bundle.json"
    promoted_comparison = root / "windows-payload-comparison.json"
    paths = {
        evidence: packaged / "packaged-backtest-evidence.json",
        seed: packaged / "packaged-backtest-seed.json",
        host: packaged / "packaged-backtest-host-observation.json",
        desktop: packaged / "windows-desktop-evidence.json",
        desktop.with_name("tauri-webview-evidence.json"): packaged
        / "tauri-webview-evidence.json",
        installer: promoted_installer,
        bundle: promoted_bundle,
    }
    for origin, destination in paths.items():
        shutil.copyfile(origin, destination)
    bundle_payload = json.loads(promoted_bundle.read_text(encoding="utf-8"))
    comparison = {
        "schema_version": 1,
        "artifact": "windows-desktop-reproducibility-comparison",
        "reproducible": True,
        "source_sha": SOURCE_SHA,
        "left_manifest_sha256": bundle_payload["manifest_sha256"],
        "right_manifest_sha256": "a" * 64,
        "left_installer_sha256": CANDIDATE,
        "right_installer_sha256": CANDIDATE,
        "nsis": {
            "equivalent": True,
            "allowed_differences": [],
            "left_raw_sha256": CANDIDATE,
            "right_raw_sha256": CANDIDATE,
            "canonical_sha256": CANDIDATE,
        },
    }
    promoted_comparison.write_text(json.dumps(comparison), encoding="utf-8")
    promotion = create_promotion(
        root=root,
        installer_path=promoted_installer,
        bundle_manifest_path=promoted_bundle,
        comparison_path=promoted_comparison,
        evidence_path=paths[evidence],
        seed_path=paths[seed],
        host_observation_path=paths[host],
        desktop_manifest_path=paths[desktop],
        source_sha=SOURCE_SHA,
        source_tree=SOURCE_TREE,
    )
    promotion_path = packaged / "windows-packaged-backtest-promotion.json"
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    return root, promotion_path


def test_checkpoint_normalization_accepts_any_durable_symbol_boundary() -> None:
    oracle = load_oracle(ORACLE_PATH, inputs_path=INPUTS_PATH)
    expected = deepcopy(oracle["cases"]["custom_pool_1d"]["semantic"])
    resumed = deepcopy(expected)
    logs = resumed["collections"]["logs"]
    restart_index = 3
    for item in logs[restart_index:]:
        item["ordinal"] += 1
    logs.insert(
        restart_index,
        {
            "detail": {"attempt": 2},
            "level": "info",
            "message": "run_started",
            "ordinal": restart_index,
        },
    )

    normalized, difference = normalize_resumed_semantics(resumed)

    assert normalized == expected
    assert difference["removed_log"]["ordinal"] == restart_index


@pytest.mark.parametrize("restart_index", (0, 4))
def test_checkpoint_normalization_rejects_non_durable_boundaries(
    restart_index: int,
) -> None:
    oracle = load_oracle(ORACLE_PATH, inputs_path=INPUTS_PATH)
    resumed = deepcopy(oracle["cases"]["custom_pool_1d"]["semantic"])
    logs = resumed["collections"]["logs"]
    for item in logs[restart_index:]:
        item["ordinal"] += 1
    logs.insert(
        restart_index,
        {
            "detail": {"attempt": 2},
            "level": "info",
            "message": "run_started",
            "ordinal": restart_index,
        },
    )

    with pytest.raises(
        ValueError, match="checkpoint restart is not at a durable boundary"
    ):
        normalize_resumed_semantics(resumed)


def test_packaged_backtest_promotion_rechecks_actual_candidate_bytes(
    tmp_path: Path,
) -> None:
    root, promotion = _promotion_fixture(tmp_path)

    verify_promotion(
        promotion, root=root, source_sha=SOURCE_SHA, source_tree=SOURCE_TREE
    )


def test_promoted_candidate_root_is_manifest_closed_with_all_backtest_provenance(
    tmp_path: Path,
) -> None:
    root, promotion = _promotion_fixture(tmp_path)
    payloads = tuple(
        (
            path.relative_to(root).as_posix(),
            "tauri-unsigned" if path.suffix.casefold() == ".exe" else "provenance",
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )
    manifest = build_manifest(
        root=root,
        source_sha=SOURCE_SHA,
        source_tree=SOURCE_TREE,
        producer={
            "workflow": "CI",
            "run_id": 1,
            "run_attempt": 1,
            "job_id": "windows-desktop-compare",
            "job_name": "Compare independent Windows desktop candidates",
        },
        payloads=payloads,
        critical_inputs={"fixture": "1" * 64},
        toolchain={"python": "3.12"},
        lockfiles={"uv.lock": "2" * 64},
        cargo_lock_sha256="3" * 64,
    )
    manifest_path = root / "windows-desktop-alpha-candidate-manifest.json"
    write_manifest(manifest_path, manifest)
    (root / "manifest-binding.json").write_text(
        json.dumps(create_attestation_binding(manifest)), encoding="utf-8"
    )

    verify_promotion(
        promotion, root=root, source_sha=SOURCE_SHA, source_tree=SOURCE_TREE
    )
    verify_artifact_root_closure(
        manifest,
        root=root,
        artifact_name="windows-desktop-alpha-candidate-manifest",
    )


@pytest.mark.parametrize(
    "mutation",
    ("evidence", "webview", "bundle", "comparison", "promotion_binding"),
)
def test_packaged_backtest_promotion_rejects_tampering(
    tmp_path: Path, mutation: str
) -> None:
    root, promotion = _promotion_fixture(tmp_path)
    if mutation == "evidence":
        target = root / "packaged-backtest/packaged-backtest-evidence.json"
        target.write_bytes(target.read_bytes() + b" ")
    elif mutation == "webview":
        target = root / "packaged-backtest/tauri-webview-evidence.json"
        target.write_bytes(target.read_bytes() + b" ")
    elif mutation == "bundle":
        target = root / "windows-desktop-bundle.json"
        target.write_bytes(target.read_bytes() + b" ")
    elif mutation == "comparison":
        target = root / "windows-payload-comparison.json"
        target.write_bytes(target.read_bytes() + b" ")
    else:
        payload = json.loads(promotion.read_text(encoding="utf-8"))
        payload["binding_sha256"] = "0" * 64
        promotion.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceError):
        verify_promotion(
            promotion, root=root, source_sha=SOURCE_SHA, source_tree=SOURCE_TREE
        )


def test_packaged_fixture_switches_reproduce_all_fifteen_oracle_semantics(
    tmp_path: Path,
) -> None:
    seed = prepare(tmp_path, source_sha=SOURCE_SHA, source_tree=SOURCE_TREE)
    oracle = load_oracle(ORACLE_PATH, inputs_path=INPUTS_PATH)
    costs = seed["costs"]

    def run_case(
        case_id: str,
        formula: dict[str, object],
        scope: dict[str, object],
        period: str,
        scoring_start: str,
        scoring_end: str,
    ) -> None:
        packaged_database = tmp_path / "stock-desk.db"
        harness_database = tmp_path / "backtest-harness.db"
        packaged_database.replace(harness_database)
        try:
            with BacktestHarness.create(tmp_path) as harness:
                completed = harness._run(
                    BacktestIntent(
                        scope_kind=scope["kind"],  # type: ignore[arg-type]
                        symbol=scope.get("symbol"),  # type: ignore[arg-type]
                        scope_id=scope.get("pool_id"),  # type: ignore[arg-type]
                        scope_revision_or_snapshot_id=(
                            str(scope["revision"])
                            if "revision" in scope
                            else str(scope["snapshot_id"])
                            if "snapshot_id" in scope
                            else None
                        ),
                        formula_version_id=str(formula["version_id"]),
                        formula_parameters=formula["parameters"],  # type: ignore[arg-type]
                        period=Period(period),
                        adjustment=Adjustment.NONE,
                        scoring_start=datetime.fromisoformat(scoring_start),
                        scoring_end=datetime.fromisoformat(scoring_end),
                        quantity_shares=int(costs["quantity_shares"]),
                        commission_bps=Decimal(str(costs["commission_bps"])),
                        minimum_commission=Decimal(str(costs["minimum_commission"])),
                        sell_tax_bps=Decimal(str(costs["sell_tax_bps"])),
                        slippage_bps=Decimal(str(costs["slippage_bps"])),
                    )
                )
                actual = project_completed(completed, harness)
        finally:
            harness_database.replace(packaged_database)
        assert actual == oracle["cases"][case_id]["semantic"]

    for case_id in (
        "a_share_constraints_60m",
        "open_position_costs_1d",
        "partial_pool_gap_1d",
    ):
        switch_fixture(tmp_path, case_id)
        special = seed["special_cases"][case_id]
        run_case(
            case_id,
            special["formula"],
            special["scope"],
            special["period"],
            special["scoring_start"],
            special["scoring_end"],
        )
    for period in ("1d", "1w", "60m"):
        switch_fixture(tmp_path, f"matrix_{period}")
        period_seed = seed["periods"][period]
        pool = seed["pools"][period]
        for formula_id in ("macd", "custom"):
            for scope_id in ("single", "pool"):
                scope = (
                    {"kind": "single", "symbol": pool["symbols"][0]}
                    if scope_id == "single"
                    else {
                        "kind": "preset",
                        "pool_id": pool["pool_id"],
                        "snapshot_id": pool["snapshot_id"],
                    }
                )
                run_case(
                    f"{formula_id}_{scope_id}_{period}",
                    seed["formulas"][formula_id],
                    scope,
                    period,
                    period_seed["scoring_start"],
                    period_seed["scoring_end"],
                )
    # The packaged checkpoint baseline selects the daily matrix again. The
    # append-only market catalog must treat that deterministic switch as
    # idempotent while all prior run snapshots remain queryable.
    switch_fixture(tmp_path, "matrix_1d")
