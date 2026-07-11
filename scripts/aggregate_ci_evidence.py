from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final

from coverage import CoverageData
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import check_requirement_coverage
from scripts.ci_test_inventory import (
    SHARDS,
    InventoryError,
    canonical_json,
    sha256_json,
    validate_inventory,
)


SHARD_SCHEMA: Final = "stock-desk-python-shard-evidence-v1"
AGGREGATE_SCHEMA: Final = "stock-desk-python-evidence-aggregate-v1"
TEST_REPORT_SCHEMA: Final = "stock-desk-test-report-v1"
REQUIREMENT_SCHEMA: Final = "stock-desk-requirement-evidence-v2"
MAX_REPORT_BYTES: Final = 64_000_000
MINIMUM_COVERAGE: Final = 85.0
MINIMUM_PRECISION: Final = 2
_TEST_STATUSES: Final = {"passed", "failed", "error", "skipped", "xfail"}
REQUIREMENT_RUNNERS: Final = ("pytest", "vitest", "playwright")


class EvidenceError(ValueError):
    pass


def _expect_git_oid(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EvidenceError(f"{label} must be a lowercase 40-character git oid")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceError(f"cannot read evidence file {path}: {exc}") from exc
    return digest.hexdigest()


def _load_json(path: Path) -> object:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise EvidenceError(f"evidence JSON exceeds the size limit: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read evidence JSON {path}: {exc}") from exc


def _safe_basename(path: Path) -> str:
    if path.name != str(path) and not path.is_absolute():
        # The manifest stores only a basename; callers may pass any local source
        # path while producing it.
        return path.name
    return path.name


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(payload))


def _junit_root(path: Path) -> ET.Element:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"cannot read JUnit report {path}: {exc}") from exc
    if len(raw) > MAX_REPORT_BYTES:
        raise EvidenceError("JUnit report exceeds the size limit")
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise EvidenceError("JUnit report cannot contain DTD or entity declarations")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EvidenceError(f"JUnit report is malformed: {exc}") from exc


def parse_pytest_junit(
    path: Path, *, source_sha: str, source_tree: str, shard: str
) -> list[dict[str, str]]:
    root = _junit_root(path)
    if root.tag not in {"testsuite", "testsuites"}:
        raise EvidenceError("JUnit report has an unsupported root element")
    records: list[dict[str, str]] = []
    for testcase in root.findall(".//testcase"):
        properties = {
            prop.get("name", ""): prop.get("value", "")
            for prop in testcase.findall("./properties/property")
        }
        nodeid = properties.get("stock_desk_nodeid", "")
        if not nodeid:
            raise EvidenceError(
                "JUnit testcase lacks stock_desk_nodeid; run pytest with "
                "-p scripts.ci_test_inventory"
            )
        expected_properties = {
            "stock_desk_source_sha": source_sha,
            "stock_desk_source_tree": source_tree,
            "stock_desk_shard": shard,
        }
        if any(
            properties.get(key) != value for key, value in expected_properties.items()
        ):
            raise EvidenceError(
                "JUnit testcase source SHA/tree/shard identity does not match"
            )
        try:
            from scripts.ci_test_inventory import normalize_nodeid

            selector = normalize_nodeid(nodeid)
        except InventoryError as exc:
            raise EvidenceError(str(exc)) from exc
        status = "passed"
        if testcase.find("failure") is not None:
            status = "failed"
        elif testcase.find("error") is not None:
            status = "error"
        else:
            skipped = testcase.find("skipped")
            if skipped is not None:
                status = (
                    "xfail"
                    if skipped.get("type", "").lower() == "pytest.xfail"
                    else "skipped"
                )
        records.append(
            {
                "path": selector.partition("::")[0],
                "selector": selector,
                "status": status,
            }
        )
    if not records:
        raise EvidenceError("JUnit report contains no testcase records")
    selectors = [record["selector"] for record in records]
    duplicate = sorted(
        selector for selector, count in Counter(selectors).items() if count != 1
    )
    if duplicate:
        raise EvidenceError(f"JUnit report contains duplicate nodeids: {duplicate[0]}")
    return sorted(records, key=lambda item: item["selector"])


def _status_counts(records: Iterable[Mapping[str, str]]) -> dict[str, int]:
    counts = Counter(record["status"] for record in records)
    return {status: counts.get(status, 0) for status in sorted(_TEST_STATUSES)}


def build_shard_evidence(
    *,
    inventory: Mapping[str, Any],
    shard: str,
    junit_path: Path,
    coverage_path: Path,
    source_sha: str,
    source_tree: str,
    attempt: int = 1,
) -> dict[str, Any]:
    try:
        validated_inventory = validate_inventory(
            dict(inventory), source_sha=source_sha, source_tree=source_tree
        )
    except InventoryError as exc:
        raise EvidenceError(str(exc)) from exc
    if shard not in SHARDS:
        raise EvidenceError(f"unknown Python shard: {shard}")
    if type(attempt) is not int or attempt != 1:
        raise EvidenceError(
            "only authoritative first-run evidence (attempt 1) is accepted"
        )
    records = parse_pytest_junit(
        junit_path, source_sha=source_sha, source_tree=source_tree, shard=shard
    )
    expected = validated_inventory["shards"][shard]["nodeids"]
    actual = [record["selector"] for record in records]
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise EvidenceError(
            "JUnit nodeids differ from the canonical shard inventory "
            f"(missing={missing[:3]}, unexpected={unexpected[:3]})"
        )
    counts = _status_counts(records)
    if counts["failed"] or counts["error"] or counts["xfail"]:
        raise EvidenceError(
            "authoritative first-run JUnit contains failed, error, or xfail records"
        )
    if not coverage_path.is_file():
        raise EvidenceError("parallel coverage data file is missing")
    if not coverage_path.name.startswith(".coverage"):
        raise EvidenceError("shard coverage must be an individual .coverage data file")
    expected_context = f"stock-desk:{source_sha}:{source_tree}:{shard}"
    try:
        coverage_data = CoverageData(basename=str(coverage_path))
        coverage_data.read()
        contexts = coverage_data.measured_contexts()
    except Exception as exc:
        raise EvidenceError(f"parallel coverage data is unreadable: {exc}") from exc
    if not coverage_data.has_arcs():
        raise EvidenceError("shard coverage data was not recorded in branch mode")
    if contexts != {expected_context}:
        raise EvidenceError(
            "parallel coverage context does not match the exact source SHA/tree/shard"
        )
    payload: dict[str, Any] = {
        "schema": SHARD_SCHEMA,
        "source_sha": _expect_git_oid(source_sha, "source_sha"),
        "source_tree": _expect_git_oid(source_tree, "source_tree"),
        "shard": shard,
        "attempt": 1,
        "authoritative": True,
        "inventory_sha256": validated_inventory["inventory_sha256"],
        "nodeids": actual,
        "nodeids_sha256": sha256_json(actual),
        "junit": {
            "file": _safe_basename(junit_path),
            "sha256": _sha256_file(junit_path),
            "counts": counts,
            "records": records,
        },
        "coverage": {
            "file": _safe_basename(coverage_path),
            "sha256": _sha256_file(coverage_path),
            "parallel": True,
        },
        "toolchain": {
            "python": sys.version.split()[0],
            "pytest": importlib.metadata.version("pytest"),
            "coverage": importlib.metadata.version("coverage"),
        },
        "status": "passed",
    }
    payload["evidence_sha256"] = sha256_json(payload)
    return payload


def validate_shard_evidence(
    raw: object,
    *,
    source_sha: str,
    source_tree: str,
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EvidenceError("shard evidence must be a JSON object")
    expected_fields = {
        "schema",
        "source_sha",
        "source_tree",
        "shard",
        "attempt",
        "authoritative",
        "inventory_sha256",
        "nodeids",
        "nodeids_sha256",
        "junit",
        "coverage",
        "toolchain",
        "status",
        "evidence_sha256",
    }
    if set(raw) != expected_fields or raw.get("schema") != SHARD_SCHEMA:
        raise EvidenceError("shard evidence fields do not match the v1 schema")
    if raw["source_sha"] != source_sha or raw["source_tree"] != source_tree:
        raise EvidenceError("shard evidence source identity does not match")
    if raw["shard"] not in SHARDS:
        raise EvidenceError("shard evidence names an unknown shard")
    if raw["attempt"] != 1 or raw["authoritative"] is not True:
        raise EvidenceError("retry evidence cannot replace the authoritative first run")
    if raw["status"] != "passed":
        raise EvidenceError("shard evidence is not successful")
    shard = raw["shard"]
    if raw["inventory_sha256"] != inventory["inventory_sha256"]:
        raise EvidenceError("shard evidence uses a stale inventory")
    expected = inventory["shards"][shard]["nodeids"]
    if raw["nodeids"] != expected or raw["nodeids_sha256"] != sha256_json(expected):
        raise EvidenceError("shard nodeids differ from the canonical inventory")
    junit = raw["junit"]
    if not isinstance(junit, dict) or set(junit) != {
        "file",
        "sha256",
        "counts",
        "records",
    }:
        raise EvidenceError("shard JUnit metadata is invalid")
    records = junit["records"]
    if (
        not isinstance(records, list)
        or [item.get("selector") for item in records] != expected
    ):
        raise EvidenceError("shard JUnit records differ from the canonical inventory")
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "selector", "status"}
            or record["status"] not in _TEST_STATUSES
        ):
            raise EvidenceError("shard JUnit record is invalid")
    counts = _status_counts(records)
    if junit["counts"] != counts or any(
        counts[key] for key in ("failed", "error", "xfail")
    ):
        raise EvidenceError(
            "shard first-run JUnit is not successful "
            f"(failed={counts['failed']}, error={counts['error']}, xfail={counts['xfail']})"
        )
    coverage = raw["coverage"]
    if (
        not isinstance(coverage, dict)
        or set(coverage) != {"file", "sha256", "parallel"}
        or coverage["parallel"] is not True
    ):
        raise EvidenceError("shard parallel coverage metadata is invalid")
    unsigned = dict(raw)
    digest = unsigned.pop("evidence_sha256")
    if digest != sha256_json(unsigned):
        raise EvidenceError("shard evidence document digest does not match")
    return raw


def coverage_totals(
    raw: object,
    *,
    threshold: float = MINIMUM_COVERAGE,
    precision: int = MINIMUM_PRECISION,
) -> dict[str, Any]:
    if type(precision) is not int or precision < MINIMUM_PRECISION:
        raise EvidenceError("coverage precision must be at least two decimal places")
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or threshold < MINIMUM_COVERAGE
    ):
        raise EvidenceError("coverage threshold cannot be lower than 85.00%")
    if not isinstance(raw, dict):
        raise EvidenceError("coverage JSON has an unsupported schema")
    metadata = raw.get("meta")
    if (
        not isinstance(metadata, dict)
        or metadata.get("format") not in {2, 3}
        or metadata.get("branch_coverage") is not True
    ):
        raise EvidenceError(
            "coverage JSON has an unsupported schema or is not branch mode"
        )
    totals = raw.get("totals")
    if not isinstance(totals, dict):
        raise EvidenceError("coverage JSON is missing totals")
    percent = totals.get("percent_covered")
    branches = totals.get("num_branches")
    if (
        not isinstance(percent, (int, float))
        or isinstance(percent, bool)
        or not isinstance(branches, int)
        or isinstance(branches, bool)
        or branches <= 0
    ):
        raise EvidenceError("coverage JSON does not contain branch-mode totals")
    actual = float(percent)
    if actual < float(threshold):
        raise EvidenceError(
            f"actual combined branch-mode coverage {actual:.{precision}f}% is below "
            f"{float(threshold):.{precision}f}%"
        )
    return {
        "branch": True,
        "actual_percent": actual,
        "display_percent": f"{actual:.{precision}f}",
        "threshold_percent": float(threshold),
        "precision": precision,
        "num_branches": branches,
        "covered_branches": totals.get("covered_branches"),
        "actual_branches_percent": totals.get("percent_branches_covered"),
        "status": "passed",
    }


def build_python_aggregate(
    *,
    inventory: Mapping[str, Any],
    shard_evidence: Iterable[object],
    coverage_json: object,
    source_sha: str,
    source_tree: str,
    coverage_report_sha256: str,
    precision: int = MINIMUM_PRECISION,
    threshold: float = MINIMUM_COVERAGE,
    requirement_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        validated_inventory = validate_inventory(
            dict(inventory), source_sha=source_sha, source_tree=source_tree
        )
    except InventoryError as exc:
        raise EvidenceError(str(exc)) from exc
    by_shard: dict[str, dict[str, Any]] = {}
    for raw in shard_evidence:
        evidence = validate_shard_evidence(
            raw,
            source_sha=source_sha,
            source_tree=source_tree,
            inventory=validated_inventory,
        )
        shard = evidence["shard"]
        if shard in by_shard:
            raise EvidenceError(f"duplicate shard evidence: {shard}")
        by_shard[shard] = evidence
    if tuple(sorted(by_shard, key=SHARDS.index)) != SHARDS:
        missing = sorted(set(SHARDS) - set(by_shard))
        raise EvidenceError(f"missing Python shard evidence: {', '.join(missing)}")
    all_nodeids = [nodeid for shard in SHARDS for nodeid in by_shard[shard]["nodeids"]]
    if len(all_nodeids) != len(set(all_nodeids)):
        raise EvidenceError("a pytest nodeid appears in more than one shard report")
    if sha256_json(sorted(all_nodeids)) != validated_inventory["all_nodeids_sha256"]:
        raise EvidenceError("Python shard reports omit canonical pytest nodeids")
    coverage = coverage_totals(coverage_json, threshold=threshold, precision=precision)
    payload: dict[str, Any] = {
        "schema": AGGREGATE_SCHEMA,
        "source_sha": _expect_git_oid(source_sha, "source_sha"),
        "source_tree": _expect_git_oid(source_tree, "source_tree"),
        "inventory": {
            "count": validated_inventory["total_count"],
            "sha256": validated_inventory["inventory_sha256"],
            "unique_ownership": True,
        },
        "shards": {
            shard: {
                "evidence_sha256": by_shard[shard]["evidence_sha256"],
                "nodeid_count": len(by_shard[shard]["nodeids"]),
                "junit_sha256": by_shard[shard]["junit"]["sha256"],
                "coverage_sha256": by_shard[shard]["coverage"]["sha256"],
                "status": "passed",
            }
            for shard in SHARDS
        },
        "coverage": {**coverage, "report_sha256": coverage_report_sha256},
        "first_run_authoritative": True,
        "requirement_evidence_sha256": requirement_evidence_sha256,
        "status": "passed",
    }
    payload["aggregate_sha256"] = sha256_json(payload)
    return payload


def build_test_report(
    *,
    runner: str,
    source_sha: str,
    source_tree: str,
    tests: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    if runner not in {"vitest", "playwright"}:
        raise EvidenceError(
            "normalized frontend report runner must be vitest or playwright"
        )
    records: list[dict[str, str]] = []
    for raw in tests:
        if set(raw) != {"path", "selector", "status"}:
            raise EvidenceError("normalized test record fields are invalid")
        path = raw["path"]
        selector = raw["selector"]
        status = raw["status"]
        if (
            not isinstance(path, str)
            or not isinstance(selector, str)
            or not path
            or not selector
            or status not in _TEST_STATUSES
        ):
            raise EvidenceError("normalized test record values are invalid")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or not path.startswith("web/"):
            raise EvidenceError("normalized frontend path escapes web/")
        records.append({"path": path, "selector": selector, "status": status})
    keys = [(record["path"], record["selector"]) for record in records]
    if not keys or len(keys) != len(set(keys)):
        raise EvidenceError("normalized frontend tests must be non-empty and unique")
    records.sort(key=lambda item: (item["path"], item["selector"]))
    payload: dict[str, Any] = {
        "schema": TEST_REPORT_SCHEMA,
        "source_sha": _expect_git_oid(source_sha, "source_sha"),
        "source_tree": _expect_git_oid(source_tree, "source_tree"),
        "runner": runner,
        "tests": records,
        "status": "passed"
        if all(record["status"] in {"passed", "skipped"} for record in records)
        else "failed",
    }
    payload["report_sha256"] = sha256_json(payload)
    return payload


def _frontend_path(runner: str, classname: str, repo_root: Path) -> str:
    separator = " > " if runner == "vitest" else " › "
    candidates = [part.strip() for part in classname.split(separator)]
    extensions = (".ts", ".tsx", ".js", ".jsx")
    paths = [
        candidate.replace("\\", "/")
        for candidate in candidates
        if candidate.replace("\\", "/").endswith(extensions)
    ]
    if len(paths) != 1:
        raise EvidenceError(
            "frontend JUnit classname must contain exactly one test file path"
        )
    path = paths[0]
    if path.startswith("web/"):
        normalized = path
    elif runner == "vitest" and path.startswith("src/"):
        normalized = f"web/{path}"
    elif runner == "playwright" and path.startswith("e2e/"):
        normalized = f"web/{path}"
    elif runner == "playwright" and "/" not in path:
        matches = sorted(
            candidate
            for candidate in (repo_root / "web" / "e2e").rglob(path)
            if candidate.is_file() and not candidate.is_symlink()
        )
        if len(matches) != 1:
            raise EvidenceError(
                "Playwright JUnit basename must resolve to exactly one web/e2e file"
            )
        normalized = matches[0].relative_to(repo_root).as_posix()
    else:
        raise EvidenceError(f"frontend JUnit test path must be rooted at web/: {path}")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts:
        raise EvidenceError("frontend JUnit test path escapes web/")
    return normalized


def normalize_frontend_junit(
    path: Path,
    *,
    runner: str,
    source_sha: str,
    source_tree: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    if runner not in {"vitest", "playwright"}:
        raise EvidenceError("frontend JUnit runner must be vitest or playwright")
    root = _junit_root(path)
    resolved_root = (repo_root or Path.cwd()).resolve()
    separator = " > " if runner == "vitest" else " › "
    records_by_selector: dict[tuple[str, str], dict[str, str]] = {}
    status_priority = {
        "skipped": 0,
        "passed": 1,
        "xfail": 2,
        "failed": 3,
        "error": 4,
    }
    for testcase in root.findall(".//testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "").strip()
        if not classname or not name:
            raise EvidenceError("frontend JUnit testcase lacks classname or name")
        selector = name.rsplit(separator, maxsplit=1)[-1].strip()
        if not selector:
            raise EvidenceError("frontend JUnit testcase has an empty exact title")
        status = "passed"
        if testcase.find("failure") is not None:
            status = "failed"
        elif testcase.find("error") is not None:
            status = "error"
        else:
            skipped = testcase.find("skipped")
            if skipped is not None:
                skip_type = skipped.get("type", "").lower()
                status = "xfail" if "xfail" in skip_type else "skipped"
        frontend_path = _frontend_path(runner, classname, resolved_root)
        key = (frontend_path, selector)
        existing = records_by_selector.get(key)
        if (
            existing is None
            or status_priority[status] > status_priority[existing["status"]]
        ):
            records_by_selector[key] = {
                "path": frontend_path,
                "selector": selector,
                "status": status,
            }
    return build_test_report(
        runner=runner,
        source_sha=source_sha,
        source_tree=source_tree,
        tests=records_by_selector.values(),
    )


def validate_test_report(
    raw: object, *, source_sha: str, source_tree: str
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "source_sha",
        "source_tree",
        "runner",
        "tests",
        "status",
        "report_sha256",
    }:
        raise EvidenceError("normalized test report fields do not match the v1 schema")
    if raw["schema"] != TEST_REPORT_SCHEMA:
        raise EvidenceError("unsupported normalized test report schema")
    if raw["source_sha"] != source_sha or raw["source_tree"] != source_tree:
        raise EvidenceError("normalized test report source identity does not match")
    rebuilt = build_test_report(
        runner=raw["runner"],
        source_sha=source_sha,
        source_tree=source_tree,
        tests=raw["tests"],
    )
    if rebuilt != raw:
        raise EvidenceError("normalized test report digest or status does not match")
    return raw


def build_requirement_evidence(
    *,
    manifest: Mapping[str, Any] | None = None,
    manifests: Sequence[Mapping[str, Any]] | None = None,
    reports: Iterable[object],
    source_sha: str,
    source_tree: str,
    manifest_sha256: str | None = None,
    manifest_sha256s: Mapping[str, str] | None = None,
    inventory: Mapping[str, Any] | None = None,
    required_runners: Iterable[str] | None = None,
) -> dict[str, Any]:
    sha = _expect_git_oid(source_sha, "source_sha")
    tree = _expect_git_oid(source_tree, "source_tree")
    if (manifest is None) == (manifests is None):
        raise EvidenceError("exactly one manifest or manifests collection is required")
    authorities = [manifest] if manifest is not None else list(manifests or ())
    if not authorities:
        raise EvidenceError("requirement authority collection cannot be empty")
    if manifest_sha256s is None:
        if manifest_sha256 is None:
            raise EvidenceError("requirement manifest digest is required")
        manifest_digests = {"requirements.yml": manifest_sha256}
    else:
        if manifest_sha256 is not None:
            raise EvidenceError("single and multi-manifest digests cannot be combined")
        manifest_digests = dict(manifest_sha256s)
    if (
        len(manifest_digests) != len(authorities)
        or any(Path(path).name != path for path in manifest_digests)
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in manifest_digests.values()
        )
    ):
        raise EvidenceError("requirement manifest digests are incomplete or invalid")
    items: list[Mapping[str, Any]] = []
    ids: set[str] = set()
    behaviors: set[str] = set()
    for authority in authorities:
        for item in [*authority["requirements"], *authority["non_goals"]]:
            item_id = item["id"]
            behavior = item.get("behavior_key")
            if item_id in ids:
                raise EvidenceError(
                    f"duplicate requirement id across authorities: {item_id}"
                )
            if isinstance(behavior, str) and behavior in behaviors:
                raise EvidenceError(
                    f"duplicate behavior_key across authorities: {behavior}"
                )
            ids.add(item_id)
            if isinstance(behavior, str):
                behaviors.add(behavior)
            items.append(item)
    requested = list(
        REQUIREMENT_RUNNERS if required_runners is None else required_runners
    )
    if (
        not requested
        or len(requested) != len(set(requested))
        or any(runner not in REQUIREMENT_RUNNERS for runner in requested)
    ):
        raise EvidenceError(
            "required_runners must uniquely select pytest, vitest, and/or playwright"
        )
    scope = tuple(runner for runner in REQUIREMENT_RUNNERS if runner in requested)
    report_records: dict[tuple[str, str, str], tuple[str, str]] = {}
    report_digests: list[str] = []
    python_shards: set[str] = set()
    seen_runners: set[str] = set()
    for raw in reports:
        if isinstance(raw, dict) and raw.get("schema") == SHARD_SCHEMA:
            if "pytest" not in scope:
                raise EvidenceError(
                    "pytest report is outside the required runner scope"
                )
            if inventory is None:
                raise EvidenceError(
                    "canonical inventory is required for pytest requirement evidence"
                )
            validated_shard = validate_shard_evidence(
                raw,
                source_sha=sha,
                source_tree=tree,
                inventory=inventory,
            )
            shard = validated_shard["shard"]
            if shard in python_shards:
                raise EvidenceError(f"duplicate pytest shard report: {shard}")
            python_shards.add(shard)
            runner = "pytest"
            records = validated_shard["junit"]["records"]
            digest = validated_shard["evidence_sha256"]
        else:
            report = validate_test_report(raw, source_sha=sha, source_tree=tree)
            if report["status"] != "passed":
                raise EvidenceError(
                    f"{report['runner']} report contains a failed/error/xfail test"
                )
            runner = report["runner"]
            if runner not in scope:
                raise EvidenceError(
                    f"{runner} report is outside the required runner scope"
                )
            records = report["tests"]
            digest = report["report_sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise EvidenceError("test report digest is invalid")
        report_digests.append(digest)
        seen_runners.add(runner)
        for record in records:
            key = (runner, record["path"], record["selector"])
            if key in report_records:
                raise EvidenceError(
                    "selector appears in more than one exact-SHA report: "
                    f"{runner}:{record['selector']}"
                )
            report_records[key] = (record["status"], digest)
    if "pytest" in scope and python_shards != set(SHARDS):
        missing = sorted(set(SHARDS) - python_shards)
        raise EvidenceError(
            "requirement evidence requires all four pytest shards"
            + (f"; missing {', '.join(missing)}" if missing else "")
        )
    missing_runners = [runner for runner in scope if runner not in seen_runners]
    if missing_runners:
        raise EvidenceError(
            f"required runner reports are missing: {', '.join(missing_runners)}"
        )
    bindings: list[dict[str, str]] = []
    for item in items:
        for evidence in item["evidence"]:
            if evidence.get("state") != "existing":
                continue
            runner = evidence.get("runner")
            if runner not in {"pytest", "vitest", "playwright"}:
                continue
            if runner not in scope:
                continue
            key = (runner, evidence["path"], evidence["selector"])
            match = report_records.get(key)
            matches = [match] if match is not None else []
            terminal_selector = evidence["selector"].rsplit("::", maxsplit=1)[-1]
            if not matches and runner == "pytest" and "[" not in terminal_selector:
                parameter_prefix = f"{evidence['selector']}["
                matches = [
                    record
                    for (record_runner, record_path, record_selector), record in sorted(
                        report_records.items()
                    )
                    if record_runner == runner
                    and record_path == evidence["path"]
                    and record_selector.startswith(parameter_prefix)
                ]
            if not matches:
                raise EvidenceError(
                    f"{item['id']} selector has no exact-SHA report evidence: "
                    f"{runner}:{evidence['selector']}"
                )
            failed_statuses = sorted(
                {status for status, _digest in matches if status != "passed"}
            )
            if failed_statuses:
                raise EvidenceError(
                    f"{item['id']} selector is {', '.join(failed_statuses)}, "
                    "not a successful non-xfail test: "
                    f"{evidence['selector']}"
                )
            digests = {digest for _status, digest in matches}
            if len(digests) != 1:
                raise EvidenceError(
                    f"{item['id']} parameterized selector spans multiple reports: "
                    f"{evidence['selector']}"
                )
            digest = next(iter(digests))
            bindings.append(
                {
                    "requirement_id": item["id"],
                    "runner": runner,
                    "path": evidence["path"],
                    "selector": evidence["selector"],
                    "report_sha256": digest,
                }
            )
    if not bindings:
        raise EvidenceError("requirement manifest contains no executable selectors")
    payload: dict[str, Any] = {
        "schema": REQUIREMENT_SCHEMA,
        "source_sha": sha,
        "source_tree": tree,
        "requirements_manifests": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(manifest_digests.items())
        ],
        "required_runners": list(scope),
        "report_sha256": sorted(report_digests),
        "binding_count": len(bindings),
        "bindings": bindings,
        "schema_authority_collect": "passed",
        "status": "passed",
    }
    payload["evidence_sha256"] = sha256_json(payload)
    return payload


def _copy_coverage_inputs(
    manifests: Sequence[tuple[Path, Mapping[str, Any]]], workdir: Path
) -> list[Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    resolved_workdir = workdir.resolve()
    copied: list[Path] = []
    for manifest_path, evidence in manifests:
        filename = evidence["coverage"]["file"]
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise EvidenceError("coverage manifest path must be a basename")
        source = manifest_path.parent / filename
        if _sha256_file(source) != evidence["coverage"]["sha256"]:
            raise EvidenceError(f"coverage digest mismatch for {evidence['shard']}")
        target = resolved_workdir / f".coverage.{evidence['shard']}"
        shutil.copyfile(source, target)
        copied.append(target)
    return copied


def combine_coverage(
    manifests: Sequence[tuple[Path, Mapping[str, Any]]], workdir: Path
) -> tuple[Path, object]:
    resolved_workdir = workdir.resolve()
    inputs = _copy_coverage_inputs(manifests, workdir)
    result = subprocess.run(
        [sys.executable, "-m", "coverage", "combine", "--keep", *map(str, inputs)],
        cwd=resolved_workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EvidenceError(
            "coverage combine failed:\n" + result.stdout + result.stderr
        )
    output = resolved_workdir / "coverage.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "--fail-under=0",
            "-o",
            str(output),
        ],
        cwd=resolved_workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EvidenceError(
            "coverage JSON generation failed:\n" + result.stdout + result.stderr
        )
    return output, _load_json(output)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build exact-SHA Python and requirement test evidence"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    shard = subparsers.add_parser("shard")
    shard.add_argument("--inventory", type=Path, required=True)
    shard.add_argument("--shard", choices=SHARDS, required=True)
    shard.add_argument("--junit", type=Path, required=True)
    shard.add_argument("--coverage-data", type=Path, required=True)
    shard.add_argument("--source-sha", required=True)
    shard.add_argument("--source-tree", required=True)
    shard.add_argument("--output", type=Path, required=True)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--inventory", type=Path, required=True)
    aggregate.add_argument(
        "--shard-evidence", type=Path, action="append", required=True
    )
    aggregate.add_argument("--source-sha", required=True)
    aggregate.add_argument("--source-tree", required=True)
    aggregate.add_argument("--workdir", type=Path, required=True)
    aggregate.add_argument("--coverage-precision", type=int, default=2)
    aggregate.add_argument("--coverage-threshold", type=float, default=85.0)
    aggregate.add_argument("--requirement-evidence", type=Path)
    aggregate.add_argument("--output", type=Path, required=True)

    normalize = subparsers.add_parser("normalize-frontend-junit")
    normalize.add_argument("--runner", choices=("vitest", "playwright"), required=True)
    normalize.add_argument("--junit", type=Path, required=True)
    normalize.add_argument("--source-sha", required=True)
    normalize.add_argument("--source-tree", required=True)
    normalize.add_argument("--repo-root", type=Path, default=Path.cwd())
    normalize.add_argument("--output", type=Path, required=True)

    requirements = subparsers.add_parser("requirements")
    requirements.add_argument("--manifest", type=Path, action="append", required=True)
    requirements.add_argument("--inventory", type=Path)
    requirements.add_argument("--repo-root", type=Path, default=Path.cwd())
    requirements.add_argument("--report", type=Path, action="append", required=True)
    requirements.add_argument("--source-sha", required=True)
    requirements.add_argument("--source-tree", required=True)
    requirements.add_argument(
        "--required-runner",
        action="append",
        choices=REQUIREMENT_RUNNERS,
        dest="required_runners",
    )
    requirements.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "shard":
            inventory = _load_json(args.inventory)
            if not isinstance(inventory, dict):
                raise EvidenceError("inventory must be a JSON object")
            payload = build_shard_evidence(
                inventory=inventory,
                shard=args.shard,
                junit_path=args.junit,
                coverage_path=args.coverage_data,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
            )
        elif args.command == "aggregate":
            inventory = _load_json(args.inventory)
            if not isinstance(inventory, dict):
                raise EvidenceError("inventory must be a JSON object")
            manifests: list[tuple[Path, Mapping[str, Any]]] = []
            for path in args.shard_evidence:
                raw = _load_json(path)
                if not isinstance(raw, dict):
                    raise EvidenceError("shard evidence must be a JSON object")
                validated = validate_shard_evidence(
                    raw,
                    source_sha=args.source_sha,
                    source_tree=args.source_tree,
                    inventory=inventory,
                )
                manifests.append((path, validated))
            coverage_path, coverage_json = combine_coverage(manifests, args.workdir)
            requirement_digest = (
                _sha256_file(args.requirement_evidence)
                if args.requirement_evidence is not None
                else None
            )
            payload = build_python_aggregate(
                inventory=inventory,
                shard_evidence=[item for _, item in manifests],
                coverage_json=coverage_json,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
                coverage_report_sha256=_sha256_file(coverage_path),
                precision=args.coverage_precision,
                threshold=args.coverage_threshold,
                requirement_evidence_sha256=requirement_digest,
            )
        elif args.command == "normalize-frontend-junit":
            payload = normalize_frontend_junit(
                args.junit,
                runner=args.runner,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
                repo_root=args.repo_root,
            )
        else:
            repo_root = args.repo_root.resolve()
            manifest_paths = list(args.manifest)
            if len({path.name for path in manifest_paths}) != len(manifest_paths):
                raise EvidenceError("requirement manifest paths must be unique")
            manifests = [
                check_requirement_coverage.load_manifest(path)
                for path in manifest_paths
            ]
            # This performs schema, frozen authority, public-boundary and collect
            # validation. It never executes the selected tests.
            for manifest_path, manifest in zip(manifest_paths, manifests, strict=True):
                check_requirement_coverage.validate_authority_manifest(
                    manifest,
                    manifest_path=manifest_path,
                    repo_root=repo_root,
                    mode="pre-publish",
                    verify_selectors=True,
                    selector_runners=(
                        frozenset(args.required_runners)
                        if args.required_runners is not None
                        else None
                    ),
                )
            if len(manifests) > 1:
                check_requirement_coverage._validate_cross_authority_uniqueness(
                    {
                        manifest_path.name: manifest
                        for manifest_path, manifest in zip(
                            manifest_paths, manifests, strict=True
                        )
                    }
                )
            reports = [_load_json(path) for path in args.report]
            inventory = (
                _load_json(args.inventory) if args.inventory is not None else None
            )
            if inventory is not None and not isinstance(inventory, dict):
                raise EvidenceError("inventory must be a JSON object")
            payload = build_requirement_evidence(
                manifests=manifests,
                reports=reports,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
                manifest_sha256s={
                    path.name: _sha256_file(path) for path in manifest_paths
                },
                inventory=inventory,
                required_runners=args.required_runners,
            )
        _write_json(args.output, payload)
        print(f"{payload['schema']} passed for {payload['source_sha']}")
    except (
        EvidenceError,
        InventoryError,
        check_requirement_coverage.ValidationError,
        OSError,
    ) as exc:
        print(f"CI evidence error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
