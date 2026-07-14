# ruff: noqa: E402

"""Fail-closed verifier for installed Tauri backtest evidence."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.v1_backtest_oracle import load_oracle
from scripts.capture_packaged_backtest_semantics import normalize_resumed_semantics
from scripts.verify_windows_desktop_bundle import (
    BundleVerificationError,
    canonical_json,
    validate_manifest as validate_bundle_manifest,
)


ORACLE = ROOT / "tests/fixtures/backtest/v1_0_oracle.json"
INPUTS = ROOT / "tests/fixtures/backtest/v1_0_oracle_inputs.json"
GENERATOR = ROOT / "scripts/v1_backtest_oracle.py"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_WORKER = re.compile(r"^tauri-sidecar-[0-9a-f]{32}$")
PROMOTION_SCHEMA = "stock-desk-windows-packaged-backtest-promotion-v1"
_PACKAGED_RECORDS = {
    "desktop-manifest": "packaged-backtest/windows-desktop-evidence.json",
    "evidence": "packaged-backtest/packaged-backtest-evidence.json",
    "host-observation": "packaged-backtest/packaged-backtest-host-observation.json",
    "seed": "packaged-backtest/packaged-backtest-seed.json",
    "webview-manifest": "packaged-backtest/tauri-webview-evidence.json",
}


class EvidenceError(ValueError):
    """Packaged evidence does not prove the required immutable workflow."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, *, maximum: int = 8 * 1024 * 1024) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > maximum:
            raise EvidenceError(f"invalid evidence size: {path.name}")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"unreadable evidence JSON: {path.name}") from error
    if not isinstance(value, dict):
        raise EvidenceError(f"evidence root must be an object: {path.name}")
    return cast(dict[str, Any], value)


def _expected_report(oracle: Mapping[str, Any], case_id: str) -> dict[str, object]:
    return cast(dict[str, object], oracle["cases"][case_id]["semantic"]["report"])


def _record(path: Path, *, relative_path: str, role: str) -> dict[str, object]:
    try:
        size = path.stat().st_size
        digest = _sha256(path)
    except OSError as error:
        raise EvidenceError(
            f"promoted payload is unreadable: {relative_path}"
        ) from error
    return {
        "path": relative_path,
        "size": size,
        "sha256": digest,
        "role": role,
    }


def _promotion_digest(value: Mapping[str, object]) -> str:
    unsigned = dict(value)
    unsigned.pop("binding_sha256", None)
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def _validate_comparison(
    comparison: Mapping[str, Any],
    *,
    source_sha: str,
    bundle: Mapping[str, object],
    installer_sha256: str,
) -> None:
    if set(comparison) != {
        "schema_version",
        "artifact",
        "reproducible",
        "source_sha",
        "left_manifest_sha256",
        "right_manifest_sha256",
        "left_installer_sha256",
        "right_installer_sha256",
        "nsis",
    }:
        raise EvidenceError("Windows comparison fields are not canonical")
    if (
        comparison.get("schema_version") != 1
        or comparison.get("artifact") != "windows-desktop-reproducibility-comparison"
        or comparison.get("reproducible") is not True
        or comparison.get("source_sha") != source_sha
        or comparison.get("left_manifest_sha256") != bundle.get("manifest_sha256")
        or comparison.get("left_installer_sha256") != installer_sha256
    ):
        raise EvidenceError("Windows comparison does not bind candidate A")
    for key in ("right_manifest_sha256", "right_installer_sha256"):
        if _HEX64.fullmatch(str(comparison.get(key, ""))) is None:
            raise EvidenceError(f"Windows comparison has invalid {key}")
    nsis = comparison.get("nsis")
    if not isinstance(nsis, dict) or set(nsis) != {
        "equivalent",
        "allowed_differences",
        "left_raw_sha256",
        "right_raw_sha256",
        "canonical_sha256",
    }:
        raise EvidenceError("Windows NSIS comparison is missing")
    allowed = nsis.get("allowed_differences")
    if (
        nsis.get("equivalent") is not True
        or nsis.get("left_raw_sha256") != installer_sha256
        or nsis.get("right_raw_sha256") != comparison.get("right_installer_sha256")
        or _HEX64.fullmatch(str(nsis.get("canonical_sha256", ""))) is None
        or not isinstance(allowed, list)
        or allowed != sorted(set(allowed))
        or not set(allowed).issubset({"pe-checksum", "pe-timestamp"})
    ):
        raise EvidenceError("Windows NSIS comparison is invalid")


def create_promotion(
    *,
    root: Path,
    installer_path: Path,
    bundle_manifest_path: Path,
    comparison_path: Path,
    evidence_path: Path,
    seed_path: Path,
    host_observation_path: Path,
    desktop_manifest_path: Path,
    source_sha: str,
    source_tree: str,
) -> dict[str, object]:
    try:
        bundle = validate_bundle_manifest(_load(bundle_manifest_path))
    except BundleVerificationError as error:
        raise EvidenceError("promoted Windows bundle manifest is invalid") from error
    comparison = _load(comparison_path)
    installer_sha256 = _sha256(installer_path)
    _validate_comparison(
        comparison,
        source_sha=source_sha,
        bundle=bundle,
        installer_sha256=installer_sha256,
    )
    verify(
        evidence_path,
        seed_path,
        host_observation_path,
        desktop_manifest_path,
        installer_path,
        bundle_manifest_path,
        source_sha=source_sha,
        source_tree=source_tree,
        candidate_sha256=installer_sha256,
    )
    try:
        installer_relative = (
            installer_path.resolve().relative_to(root.resolve()).as_posix()
        )
        bundle_relative = (
            bundle_manifest_path.resolve().relative_to(root.resolve()).as_posix()
        )
        comparison_relative = (
            comparison_path.resolve().relative_to(root.resolve()).as_posix()
        )
    except ValueError as error:
        raise EvidenceError("promotion payload escaped the candidate root") from error
    packaged_paths = {
        "evidence": evidence_path,
        "seed": seed_path,
        "host-observation": host_observation_path,
        "desktop-manifest": desktop_manifest_path,
        "webview-manifest": desktop_manifest_path.with_name(
            "tauri-webview-evidence.json"
        ),
    }
    promotion: dict[str, object] = {
        "schema_version": PROMOTION_SCHEMA,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "installer": _record(
            installer_path, relative_path=installer_relative, role="tauri-unsigned"
        ),
        "bundle_manifest": {
            **_record(
                bundle_manifest_path,
                relative_path=bundle_relative,
                role="bundle-manifest",
            ),
            "manifest_sha256": bundle["manifest_sha256"],
        },
        "comparison": {
            **_record(
                comparison_path,
                relative_path=comparison_relative,
                role="reproducibility-comparison",
            ),
            "left_manifest_sha256": comparison["left_manifest_sha256"],
            "left_installer_sha256": comparison["left_installer_sha256"],
        },
        "packaged_backtest": [
            _record(packaged_paths[role], relative_path=path, role=role)
            for role, path in sorted(
                _PACKAGED_RECORDS.items(), key=lambda item: item[1]
            )
        ],
    }
    promotion["binding_sha256"] = _promotion_digest(promotion)
    return promotion


def verify_promotion(
    promotion_path: Path,
    *,
    root: Path,
    source_sha: str,
    source_tree: str,
) -> None:
    promotion = _load(promotion_path, maximum=256 * 1024)
    if (
        set(promotion)
        != {
            "schema_version",
            "source_sha",
            "source_tree",
            "installer",
            "bundle_manifest",
            "comparison",
            "packaged_backtest",
            "binding_sha256",
        }
        or promotion.get("schema_version") != PROMOTION_SCHEMA
    ):
        raise EvidenceError("packaged backtest promotion fields are not canonical")
    if (
        promotion.get("source_sha") != source_sha
        or promotion.get("source_tree") != source_tree
        or promotion.get("binding_sha256") != _promotion_digest(promotion)
    ):
        raise EvidenceError("packaged backtest promotion identity is invalid")
    records: dict[str, dict[str, Any]] = {}
    for field, role in (
        ("installer", "tauri-unsigned"),
        ("bundle_manifest", "bundle-manifest"),
        ("comparison", "reproducibility-comparison"),
    ):
        value = promotion.get(field)
        expected_extra = (
            {"manifest_sha256"}
            if field == "bundle_manifest"
            else (
                {"left_manifest_sha256", "left_installer_sha256"}
                if field == "comparison"
                else set()
            )
        )
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "path",
                "size",
                "sha256",
                "role",
                *expected_extra,
            }
            or value.get("role") != role
        ):
            raise EvidenceError(f"promotion has invalid {field} record")
        records[field] = value
    if (
        records["bundle_manifest"].get("path") != "windows-desktop-bundle.json"
        or records["comparison"].get("path") != "windows-payload-comparison.json"
        or not str(records["installer"].get("path", "")).casefold().endswith(".exe")
    ):
        raise EvidenceError("promotion top-level payload paths are not canonical")
    packaged = promotion.get("packaged_backtest")
    if not isinstance(packaged, list) or len(packaged) != len(_PACKAGED_RECORDS):
        raise EvidenceError("promotion packaged backtest records are incomplete")
    by_role: dict[str, dict[str, Any]] = {}
    for item in packaged:
        if isinstance(item, dict) and isinstance(item.get("role"), str):
            by_role[item["role"]] = item
    if set(by_role) != set(_PACKAGED_RECORDS) or any(
        set(record) != {"path", "size", "sha256", "role"}
        or record.get("path") != _PACKAGED_RECORDS[role]
        for role, record in by_role.items()
    ):
        raise EvidenceError("promotion packaged backtest records are not canonical")

    def bound_path(record: Mapping[str, Any]) -> Path:
        raw = record.get("path")
        pure = PurePosixPath(raw) if isinstance(raw, str) else None
        if (
            not isinstance(raw, str)
            or not raw
            or "\\" in raw
            or ":" in raw
            or pure is None
            or pure.is_absolute()
            or ".." in pure.parts
            or pure.as_posix() != raw
        ):
            raise EvidenceError("promotion contains an invalid relative path")
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as error:
            raise EvidenceError(
                "promotion payload escaped the candidate root"
            ) from error
        actual = _record(candidate, relative_path=raw, role=str(record.get("role")))
        if any(actual[key] != record.get(key) for key in actual):
            raise EvidenceError(f"promoted payload digest mismatch: {raw}")
        return candidate

    installer_path = bound_path(records["installer"])
    bundle_path = bound_path(records["bundle_manifest"])
    comparison_path = bound_path(records["comparison"])
    packaged_paths = {role: bound_path(record) for role, record in by_role.items()}
    actual = create_promotion(
        root=root,
        installer_path=installer_path,
        bundle_manifest_path=bundle_path,
        comparison_path=comparison_path,
        evidence_path=packaged_paths["evidence"],
        seed_path=packaged_paths["seed"],
        host_observation_path=packaged_paths["host-observation"],
        desktop_manifest_path=packaged_paths["desktop-manifest"],
        source_sha=source_sha,
        source_tree=source_tree,
    )
    if actual != promotion:
        raise EvidenceError("packaged backtest promotion binding is not reproducible")


def verify(
    evidence_path: Path,
    seed_path: Path,
    host_observation_path: Path,
    desktop_manifest_path: Path,
    installer_path: Path,
    bundle_manifest_path: Path,
    *,
    source_sha: str,
    source_tree: str,
    candidate_sha256: str,
) -> None:
    if _HEX40.fullmatch(source_sha) is None or _HEX40.fullmatch(source_tree) is None:
        raise EvidenceError("expected source identity is invalid")
    if _HEX64.fullmatch(candidate_sha256) is None:
        raise EvidenceError("expected candidate digest is invalid")
    evidence = _load(evidence_path)
    seed = _load(seed_path)
    host = _load(host_observation_path, maximum=64 * 1024)
    desktop = _load(desktop_manifest_path, maximum=512 * 1024)
    webview_path = desktop_manifest_path.with_name("tauri-webview-evidence.json")
    webview = _load(webview_path, maximum=8 * 1024 * 1024)
    try:
        bundle = validate_bundle_manifest(
            _load(bundle_manifest_path, maximum=2 * 1024 * 1024)
        )
    except BundleVerificationError as error:
        raise EvidenceError("Windows bundle manifest is invalid") from error
    try:
        installer_size = installer_path.stat().st_size
        installer_sha256 = _sha256(installer_path)
    except OSError as error:
        raise EvidenceError("installed candidate bytes are unreadable") from error
    if installer_sha256 != candidate_sha256:
        raise EvidenceError("actual candidate bytes do not match expected digest")
    if bundle.get("source_sha") != source_sha:
        raise EvidenceError("Windows bundle belongs to another source revision")
    installer_record = bundle.get("installer")
    if not isinstance(installer_record, dict) or installer_record != {
        "path": installer_record.get("path"),
        "size": installer_size,
        "sha256": installer_sha256,
        "role": "nsis-installer",
    }:
        raise EvidenceError("actual installer bytes do not match the bundle manifest")
    expected_root_keys = {
        "schema_version",
        "source_sha",
        "source_tree",
        "candidate_sha256",
        "capture_nonce",
        "actual_packaged_tauri",
        "actual_tauri_webview",
        "authenticated_host_ipc",
        "packaged_sidecar_worker",
        "read_only_demo",
        "submission_surface",
        "seed",
        "oracle",
        "cells",
        "special_cases",
        "checkpoint",
    }
    if set(evidence) != expected_root_keys:
        raise EvidenceError("packaged evidence root fields are not canonical")
    flags = {
        "schema_version": "stock-desk-packaged-backtest-evidence-v1",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "candidate_sha256": candidate_sha256,
        "actual_packaged_tauri": True,
        "actual_tauri_webview": True,
        "authenticated_host_ipc": True,
        "packaged_sidecar_worker": True,
        "read_only_demo": False,
        "submission_surface": "installed-tauri-webview-host-ipc",
    }
    for key, expected in flags.items():
        if evidence.get(key) != expected:
            raise EvidenceError(f"packaged evidence identity mismatch: {key}")
    capture_nonce = evidence.get("capture_nonce")
    if _UUID4.fullmatch(str(capture_nonce or "")) is None:
        raise EvidenceError("packaged evidence capture nonce is invalid")

    expected_host_keys = {
        "schema_version",
        "source_sha",
        "source_tree",
        "candidate_sha256",
        "capture_nonce",
        "capture_scope",
        "host_ipc_command",
        "host_pid",
        "main_window_handle",
        "installed_host_sha256",
        "isolated_webview_process_ids",
        "sidecar_before",
        "sidecar_after",
        "checkpoint",
        "evidence_sha256",
    }
    if set(host) != expected_host_keys:
        raise EvidenceError("host observation fields are not canonical")
    expected_host_identity = {
        "schema_version": "stock-desk-packaged-backtest-host-observation-v1",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "candidate_sha256": candidate_sha256,
        "capture_nonce": capture_nonce,
        "capture_scope": "installed-current-user-tauri-webview",
        "host_ipc_command": "desktop_api_request",
        "evidence_sha256": _sha256(evidence_path),
    }
    for key, expected in expected_host_identity.items():
        if host.get(key) != expected:
            raise EvidenceError(f"host observation identity mismatch: {key}")
    for key in ("host_pid", "main_window_handle"):
        if (
            isinstance(host.get(key), bool)
            or not isinstance(host.get(key), int)
            or host[key] < 1
        ):
            raise EvidenceError(f"host observation has invalid {key}")
    if _HEX64.fullmatch(str(host.get("installed_host_sha256", ""))) is None:
        raise EvidenceError("installed Tauri host digest is invalid")
    webview_pids = host.get("isolated_webview_process_ids")
    if (
        not isinstance(webview_pids, list)
        or not webview_pids
        or len(set(webview_pids)) != len(webview_pids)
        or any(
            isinstance(pid, bool) or not isinstance(pid, int) or pid < 1
            for pid in webview_pids
        )
    ):
        raise EvidenceError("isolated WebView process identities are invalid")
    sidecars: list[dict[str, Any]] = []
    for key in ("sidecar_before", "sidecar_after"):
        value = host.get(key)
        if not isinstance(value, dict) or set(value) != {"pid", "executable_sha256"}:
            raise EvidenceError(f"host observation has invalid {key}")
        if (
            isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or value["pid"] < 1
            or _HEX64.fullmatch(str(value.get("executable_sha256", ""))) is None
        ):
            raise EvidenceError(f"host observation has invalid {key}")
        sidecars.append(value)
    if (
        sidecars[0]["pid"] == sidecars[1]["pid"]
        or sidecars[0]["executable_sha256"] != sidecars[1]["executable_sha256"]
    ):
        raise EvidenceError(
            "host observation did not prove a new identical sidecar binary"
        )
    bundle_files = bundle.get("files")
    assert isinstance(bundle_files, list)
    host_records = [
        record
        for record in bundle_files
        if isinstance(record, dict) and record.get("role") == "desktop-host"
    ]
    sidecar_records = [
        record
        for record in bundle_files
        if isinstance(record, dict) and record.get("role") == "sidecar"
    ]
    if (
        len(host_records) != 1
        or host_records[0].get("sha256") != host["installed_host_sha256"]
        or len(sidecar_records) != 1
        or sidecar_records[0].get("sha256") != sidecars[0]["executable_sha256"]
    ):
        raise EvidenceError(
            "installed host or sidecar digest does not match the bundle manifest"
        )
    if (
        desktop.get("schema_version") != "stock-desk-windows-desktop-evidence-v1"
        or desktop.get("source_sha") != source_sha
        or desktop.get("source_tree") != source_tree
        or desktop.get("candidate_sha256") != candidate_sha256
        or desktop.get("actual_packaged_tauri") is not True
    ):
        raise EvidenceError("outer Windows desktop identity mismatch")
    expected_packaged_binding = {
        "manifest": evidence_path.name,
        "sha256": _sha256(evidence_path),
        "seed": seed_path.name,
        "seed_sha256": _sha256(seed_path),
        "host_observation": host_observation_path.name,
        "host_observation_sha256": _sha256(host_observation_path),
    }
    if desktop.get("packaged_backtests") != expected_packaged_binding:
        raise EvidenceError("outer Windows manifest packaged backtest binding mismatch")
    if desktop.get("webview") != {
        "manifest": webview_path.name,
        "sha256": _sha256(webview_path),
    }:
        raise EvidenceError("outer Windows manifest WebView binding mismatch")
    if (
        webview.get("schema_version") != "stock-desk-packaged-webview-evidence-v1"
        or webview.get("source_sha") != source_sha
        or webview.get("source_tree") != source_tree
        or webview.get("actual_tauri_webview") is not True
        or webview.get("packaged_backtests")
        != {
            "manifest": evidence_path.name,
            "schema_version": evidence.get("schema_version"),
            "cell_count": 12,
            "checkpoint_run_id": cast(dict[str, Any], evidence["checkpoint"])["run_id"],
        }
    ):
        raise EvidenceError("packaged WebView backtest binding mismatch")
    if seed.get("source_sha") != source_sha or seed.get("source_tree") != source_tree:
        raise EvidenceError("seed source identity mismatch")
    if (
        seed.get("read_only_demo") is not False
        or seed.get("public_fixture") is not True
    ):
        raise EvidenceError("seed does not prove a writable public fixture")
    if evidence.get("seed") != {
        "file": seed_path.name,
        "sha256": _sha256(seed_path),
    }:
        raise EvidenceError("seed content digest mismatch")

    oracle = load_oracle(ORACLE, inputs_path=INPUTS)
    expected_oracle = {
        "source": oracle["source"],
        "oracle_sha256": _sha256(ORACLE),
        "inputs_sha256": _sha256(INPUTS),
        "generator_sha256": _sha256(GENERATOR),
        "payload_digest": oracle["payload_digest"],
    }
    if (
        evidence.get("oracle") != expected_oracle
        or seed.get("oracle") != expected_oracle
    ):
        raise EvidenceError("v1 oracle binding mismatch")

    expected_ids = {
        f"{formula}_{scope}_{period}"
        for formula in ("macd", "custom")
        for scope in ("single", "pool")
        for period in ("1d", "1w", "60m")
    }
    cells = evidence.get("cells")
    if not isinstance(cells, list) or len(cells) != 12:
        raise EvidenceError("packaged evidence must contain exactly 12 cells")
    by_id = {item.get("case_id"): item for item in cells if isinstance(item, dict)}
    if set(by_id) != expected_ids or len(by_id) != len(cells):
        raise EvidenceError(
            "packaged evidence matrix is missing, duplicated, or unknown"
        )
    run_ids: set[str] = set()
    task_ids: set[str] = set()
    matrix_workers: set[str] = set()
    for case_id in sorted(expected_ids):
        cell = by_id[case_id]
        if set(cell) != {
            "case_id",
            "formula",
            "scope",
            "period",
            "run_id",
            "task_id",
            "snapshot_id",
            "result_hash",
            "worker_id",
            "oracle_semantic_digest",
            "preflight_sha256",
            "overview_sha256",
            "report_sha256",
            "collections_sha256",
            "report_semantics",
            "semantic_projection",
        }:
            raise EvidenceError(f"cell fields are not canonical: {case_id}")
        formula, scope, period = case_id.split("_", maxsplit=2)
        if cell.get("scope") != scope or cell.get("period") != period:
            raise EvidenceError(f"matrix axes mismatch: {case_id}")
        formula_value = cell.get("formula")
        seed_formula = seed["formulas"][formula]
        if not isinstance(formula_value, dict) or formula_value != {
            "kind": formula,
            "version_id": seed_formula["version_id"],
            "checksum": seed_formula["checksum"],
            "parameters": seed_formula["parameters"],
        }:
            raise EvidenceError(f"frozen formula mismatch: {case_id}")
        if (
            cell.get("oracle_semantic_digest")
            != oracle["cases"][case_id]["semantic_digest"]
        ):
            raise EvidenceError(f"oracle case binding mismatch: {case_id}")
        if cell.get("report_semantics") != _expected_report(oracle, case_id):
            raise EvidenceError(f"v1 report semantics mismatch: {case_id}")
        if cell.get("semantic_projection") != oracle["cases"][case_id]["semantic"]:
            raise EvidenceError(f"complete v1 semantics mismatch: {case_id}")
        for key in (
            "preflight_sha256",
            "overview_sha256",
            "report_sha256",
            "collections_sha256",
        ):
            if _HEX64.fullmatch(str(cell.get(key, ""))) is None:
                raise EvidenceError(f"invalid {key}: {case_id}")
        for key in ("snapshot_id", "result_hash"):
            if _DIGEST.fullmatch(str(cell.get(key, ""))) is None:
                raise EvidenceError(f"invalid {key}: {case_id}")
        for key in ("run_id", "task_id"):
            if _UUID4.fullmatch(str(cell.get(key, ""))) is None:
                raise EvidenceError(f"invalid {key}: {case_id}")
        run_ids.add(cast(str, cell["run_id"]))
        task_ids.add(cast(str, cell["task_id"]))
        if _WORKER.fullmatch(str(cell.get("worker_id", ""))) is None:
            raise EvidenceError(f"missing packaged Worker identity: {case_id}")
        matrix_workers.add(cast(str, cell["worker_id"]))
    if len(run_ids) != 12 or len(task_ids) != 12:
        raise EvidenceError("packaged matrix run/task identities are not unique")

    expected_special_ids = {
        "a_share_constraints_60m",
        "open_position_costs_1d",
        "partial_pool_gap_1d",
    }
    special_cases = evidence.get("special_cases")
    if not isinstance(special_cases, list) or len(special_cases) != 3:
        raise EvidenceError("packaged special cases are incomplete")
    special_by_id = {
        item.get("case_id"): item for item in special_cases if isinstance(item, dict)
    }
    if set(special_by_id) != expected_special_ids or len(special_by_id) != 3:
        raise EvidenceError("packaged special case ids are not canonical")
    special_workers: set[str] = set()
    for case_id in sorted(expected_special_ids):
        item = special_by_id[case_id]
        if set(item) != {
            "case_id",
            "run_id",
            "task_id",
            "snapshot_id",
            "result_hash",
            "worker_id",
            "oracle_semantic_digest",
            "preflight_sha256",
            "overview_sha256",
            "report_sha256",
            "collections_sha256",
            "semantic_projection",
        }:
            raise EvidenceError(f"special case fields are not canonical: {case_id}")
        if (
            item.get("oracle_semantic_digest")
            != oracle["cases"][case_id]["semantic_digest"]
            or item.get("semantic_projection") != oracle["cases"][case_id]["semantic"]
        ):
            raise EvidenceError(f"special case semantics mismatch: {case_id}")
        for key in ("run_id", "task_id"):
            if _UUID4.fullmatch(str(item.get(key, ""))) is None:
                raise EvidenceError(f"invalid special case {key}: {case_id}")
        for key in ("snapshot_id", "result_hash"):
            if _DIGEST.fullmatch(str(item.get(key, ""))) is None:
                raise EvidenceError(f"invalid special case {key}: {case_id}")
        for key in (
            "preflight_sha256",
            "overview_sha256",
            "report_sha256",
            "collections_sha256",
        ):
            if _HEX64.fullmatch(str(item.get(key, ""))) is None:
                raise EvidenceError(f"invalid special case {key}: {case_id}")
        if _WORKER.fullmatch(str(item.get("worker_id", ""))) is None:
            raise EvidenceError(f"invalid special case Worker: {case_id}")
        special_workers.add(cast(str, item["worker_id"]))
        special_run_id = cast(str, item["run_id"])
        special_task_id = cast(str, item["task_id"])
        if special_run_id in run_ids or special_task_id in task_ids:
            raise EvidenceError("packaged run/task identities are not globally unique")
        run_ids.add(special_run_id)
        task_ids.add(special_task_id)

    checkpoint = evidence.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise EvidenceError("checkpoint evidence is missing")
    if set(checkpoint) != {
        "case_id",
        "run_id",
        "task_id",
        "snapshot_id",
        "result_hash",
        "worker_before",
        "worker_after",
        "runtime_state_before",
        "runtime_state_recovery",
        "runtime_state_after",
        "runtime_restart_observed",
        "recovery_required",
        "report_sha256",
        "baseline_run_id",
        "baseline_task_id",
        "baseline_snapshot_id",
        "baseline_result_hash",
        "baseline_worker_id",
        "uninterrupted_semantic_projection",
        "resumed_semantic_projection",
        "resumed_normalized_projection",
        "normalization",
    }:
        raise EvidenceError("checkpoint fields are not canonical")
    if (
        checkpoint.get("case_id") != "custom_pool_1d_checkpoint_resume"
        or checkpoint.get("recovery_required") is not True
        or checkpoint.get("worker_before") == checkpoint.get("worker_after")
        or checkpoint.get("runtime_state_before") != "ready"
        or checkpoint.get("runtime_state_recovery") != "recovery"
        or checkpoint.get("runtime_state_after") != "ready"
        or checkpoint.get("runtime_restart_observed") is not True
    ):
        raise EvidenceError("checkpoint did not resume on a new packaged Worker")
    for key in ("run_id", "task_id"):
        if _UUID4.fullmatch(str(checkpoint.get(key, ""))) is None:
            raise EvidenceError(f"invalid checkpoint {key}")
    for key in ("baseline_run_id", "baseline_task_id"):
        if _UUID4.fullmatch(str(checkpoint.get(key, ""))) is None:
            raise EvidenceError(f"invalid checkpoint {key}")
    checkpoint_run_ids = {
        cast(str, checkpoint["run_id"]),
        cast(str, checkpoint["baseline_run_id"]),
    }
    checkpoint_task_ids = {
        cast(str, checkpoint["task_id"]),
        cast(str, checkpoint["baseline_task_id"]),
    }
    if (
        len(checkpoint_run_ids) != 2
        or len(checkpoint_task_ids) != 2
        or not checkpoint_run_ids.isdisjoint(run_ids)
        or not checkpoint_task_ids.isdisjoint(task_ids)
    ):
        raise EvidenceError("packaged run/task identities are not globally unique")
    run_ids.update(checkpoint_run_ids)
    task_ids.update(checkpoint_task_ids)
    if len(run_ids) != 17 or len(task_ids) != 17:
        raise EvidenceError("packaged execution identity set is incomplete")
    for key in ("worker_before", "worker_after"):
        if _WORKER.fullmatch(str(checkpoint.get(key, ""))) is None:
            raise EvidenceError(f"invalid checkpoint {key}")
    if (
        matrix_workers != {checkpoint["worker_before"]}
        or special_workers != matrix_workers
        or checkpoint.get("baseline_worker_id") != checkpoint.get("worker_before")
    ):
        raise EvidenceError(
            "packaged cases were not executed by the pre-restart Worker"
        )
    for key in ("baseline_snapshot_id", "baseline_result_hash"):
        if _DIGEST.fullmatch(str(checkpoint.get(key, ""))) is None:
            raise EvidenceError(f"invalid checkpoint {key}")
    if checkpoint.get("baseline_snapshot_id") != checkpoint.get(
        "snapshot_id"
    ) or checkpoint.get("baseline_result_hash") != checkpoint.get("result_hash"):
        raise EvidenceError("checkpoint baseline and resumed identities differ")
    expected_checkpoint = oracle["cases"]["custom_pool_1d"]["semantic"]
    raw_resumed = checkpoint.get("resumed_semantic_projection")
    if not isinstance(raw_resumed, dict):
        raise EvidenceError("checkpoint resumed semantics are missing")
    try:
        normalized, normalization = normalize_resumed_semantics(raw_resumed)
    except ValueError as error:
        raise EvidenceError("checkpoint normalization is invalid") from error
    if (
        checkpoint.get("uninterrupted_semantic_projection") != expected_checkpoint
        or checkpoint.get("resumed_normalized_projection") != expected_checkpoint
        or normalized != expected_checkpoint
        or checkpoint.get("normalization") != normalization
    ):
        raise EvidenceError("checkpoint semantics differ from uninterrupted v1 oracle")
    host_checkpoint = host.get("checkpoint")
    if (
        not isinstance(host_checkpoint, dict)
        or set(host_checkpoint) != {"run_id", "task_id"}
        or host_checkpoint
        != {"run_id": checkpoint.get("run_id"), "task_id": checkpoint.get("task_id")}
    ):
        raise EvidenceError("checkpoint is not bound to the OS process observation")
    for key in ("snapshot_id", "result_hash"):
        if _DIGEST.fullmatch(str(checkpoint.get(key, ""))) is None:
            raise EvidenceError(f"invalid checkpoint {key}")
    if _HEX64.fullmatch(str(checkpoint.get("report_sha256", ""))) is None:
        raise EvidenceError("invalid checkpoint report digest")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--host-observation", type=Path, required=True)
    parser.add_argument("--desktop-manifest", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--bundle-manifest", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--promotion-root", type=Path)
    parser.add_argument("--output-promotion", type=Path)
    args = parser.parse_args(argv)
    verify(
        args.evidence.resolve(),
        args.seed.resolve(),
        args.host_observation.resolve(),
        args.desktop_manifest.resolve(),
        args.installer.resolve(),
        args.bundle_manifest.resolve(),
        source_sha=args.source_sha,
        source_tree=args.source_tree,
        candidate_sha256=args.candidate_sha256,
    )
    promotion_args = (args.comparison, args.promotion_root, args.output_promotion)
    if any(value is not None for value in promotion_args):
        if any(value is None for value in promotion_args):
            raise EvidenceError(
                "comparison, promotion root, and promotion output are required together"
            )
        assert args.comparison is not None
        assert args.promotion_root is not None
        assert args.output_promotion is not None
        promotion = create_promotion(
            root=args.promotion_root.resolve(),
            installer_path=args.installer.resolve(),
            bundle_manifest_path=args.bundle_manifest.resolve(),
            comparison_path=args.comparison.resolve(),
            evidence_path=args.evidence.resolve(),
            seed_path=args.seed.resolve(),
            host_observation_path=args.host_observation.resolve(),
            desktop_manifest_path=args.desktop_manifest.resolve(),
            source_sha=args.source_sha,
            source_tree=args.source_tree,
        )
        output = args.output_promotion.resolve()
        temporary = output.with_name(f".{output.name}.tmp")
        temporary.write_bytes(canonical_json(promotion))
        temporary.replace(output)
        verify_promotion(
            output,
            root=args.promotion_root.resolve(),
            source_sha=args.source_sha,
            source_tree=args.source_tree,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
