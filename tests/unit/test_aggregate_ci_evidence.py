from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from coverage import CoverageData

from scripts import aggregate_ci_evidence as aggregate
from scripts import ci_test_inventory as inventory


SHA = "a" * 40
TREE = "b" * 40
NODEIDS = (
    "tests/unit/test_sample.py::test_unit",
    "tests/integration/test_sample.py::test_integration",
    "tests/acceptance/test_sample.py::test_acceptance",
    "tests/security/test_sample.py::test_security",
)


def _inventory() -> dict[str, object]:
    return inventory.build_inventory(NODEIDS, source_sha=SHA, source_tree=TREE)


def _write_junit(
    path: Path,
    nodeid: str | tuple[str, ...],
    *,
    shard: str = "unit",
    status: str = "passed",
) -> None:
    nodeids = (nodeid,) if isinstance(nodeid, str) else nodeid
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite", tests=str(len(nodeids)))
    for item in nodeids:
        case = ET.SubElement(suite, "testcase", classname="ignored", name="ignored")
        properties = ET.SubElement(case, "properties")
        ET.SubElement(
            properties,
            "property",
            name="stock_desk_nodeid",
            value=item,
        )
        for name, value in (
            ("stock_desk_source_sha", SHA),
            ("stock_desk_source_tree", TREE),
            ("stock_desk_shard", shard),
        ):
            ET.SubElement(properties, "property", name=name, value=value)
        if status == "failed":
            ET.SubElement(case, "failure", message="boom")
        elif status == "error":
            ET.SubElement(case, "error", message="boom")
        elif status == "xfail":
            ET.SubElement(case, "skipped", type="pytest.xfail", message="expected")
        elif status == "skipped":
            ET.SubElement(case, "skipped", type="pytest.skip", message="optional")
    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)


def _shard_evidence(
    tmp_path: Path, canonical: dict[str, object] | None = None
) -> list[dict[str, object]]:
    canonical = _inventory() if canonical is None else canonical
    evidence: list[dict[str, object]] = []
    for shard in inventory.SHARDS:
        nodeids = tuple(canonical["shards"][shard]["nodeids"])
        directory = tmp_path / shard
        directory.mkdir(parents=True)
        junit = directory / "junit.xml"
        coverage = directory / f".coverage.{shard}"
        _write_junit(junit, nodeids, shard=shard)
        data = CoverageData(basename=str(coverage))
        data.set_context(f"stock-desk:{SHA}:{TREE}:{shard}")
        data.add_arcs({"example.py": {(1, 2)}})
        data.write()
        evidence.append(
            aggregate.build_shard_evidence(
                inventory=canonical,
                shard=shard,
                junit_path=junit,
                coverage_path=coverage,
                source_sha=SHA,
                source_tree=TREE,
            )
        )
    return evidence


def _coverage(percent: float = 85.0) -> dict[str, object]:
    return {
        "meta": {"format": 3, "branch_coverage": True},
        "totals": {
            "percent_covered": percent,
            "num_branches": 100,
            "covered_branches": 85,
        },
    }


def test_four_first_run_shards_aggregate_without_missing_or_duplicate_nodeids(
    tmp_path: Path,
) -> None:
    evidence = _shard_evidence(tmp_path)

    payload = aggregate.build_python_aggregate(
        inventory=_inventory(),
        shard_evidence=evidence,
        coverage_json=_coverage(85.001),
        source_sha=SHA,
        source_tree=TREE,
        coverage_report_sha256="c" * 64,
    )

    assert payload["schema"] == aggregate.AGGREGATE_SCHEMA
    assert payload["inventory"]["unique_ownership"] is True
    assert tuple(payload["shards"]) == inventory.SHARDS
    assert payload["coverage"]["display_percent"] == "85.00"
    assert payload["status"] == "passed"


def test_parallel_coverage_files_are_digest_checked_and_combined(
    tmp_path: Path,
) -> None:
    evidence = _shard_evidence(tmp_path / "inputs")
    manifests: list[tuple[Path, dict[str, object]]] = []
    for item in evidence:
        shard = item["shard"]
        manifest = tmp_path / "inputs" / shard / "shard-evidence.json"
        manifest.write_bytes(inventory.canonical_json(item))
        manifests.append((manifest, item))
    workdir = tmp_path / "combined"
    workdir.mkdir()
    (workdir / "example.py").write_text("value = 1\nvalue += 1\n", encoding="utf-8")

    report_path, report = aggregate.combine_coverage(manifests, workdir)

    assert report_path.is_file()
    assert report["totals"]["num_branches"] >= 0

    coverage_file = tmp_path / "inputs" / "unit" / ".coverage.unit"
    coverage_file.write_bytes(b"tampered")
    with pytest.raises(aggregate.EvidenceError, match="digest mismatch"):
        aggregate.combine_coverage(manifests, tmp_path / "tampered")


def test_missing_duplicate_retry_and_stale_shard_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    evidence = _shard_evidence(tmp_path)
    kwargs = {
        "inventory": _inventory(),
        "coverage_json": _coverage(),
        "source_sha": SHA,
        "source_tree": TREE,
        "coverage_report_sha256": "c" * 64,
    }

    with pytest.raises(aggregate.EvidenceError, match="missing Python shard"):
        aggregate.build_python_aggregate(shard_evidence=evidence[:-1], **kwargs)
    with pytest.raises(aggregate.EvidenceError, match="duplicate shard"):
        aggregate.build_python_aggregate(
            shard_evidence=[*evidence, evidence[0]], **kwargs
        )

    retry = copy.deepcopy(evidence)
    retry[0]["attempt"] = 2
    retry[0]["evidence_sha256"] = inventory.sha256_json(
        {key: value for key, value in retry[0].items() if key != "evidence_sha256"}
    )
    with pytest.raises(aggregate.EvidenceError, match="retry evidence"):
        aggregate.build_python_aggregate(shard_evidence=retry, **kwargs)

    stale = copy.deepcopy(evidence)
    stale[0]["source_sha"] = "d" * 40
    stale[0]["evidence_sha256"] = inventory.sha256_json(
        {key: value for key, value in stale[0].items() if key != "evidence_sha256"}
    )
    with pytest.raises(aggregate.EvidenceError, match="source identity"):
        aggregate.build_python_aggregate(shard_evidence=stale, **kwargs)


@pytest.mark.parametrize("status", ["failed", "error", "xfail"])
def test_failed_error_and_xfail_cannot_masquerade_as_a_green_shard(
    tmp_path: Path, status: str
) -> None:
    canonical = _inventory()
    junit = tmp_path / "junit.xml"
    coverage = tmp_path / ".coverage.unit"
    _write_junit(junit, NODEIDS[0], status=status)
    data = CoverageData(basename=str(coverage))
    data.set_context(f"stock-desk:{SHA}:{TREE}:unit")
    data.add_arcs({"example.py": {(1, 2)}})
    data.write()

    with pytest.raises(aggregate.EvidenceError, match="failed, error, or xfail"):
        aggregate.build_shard_evidence(
            inventory=canonical,
            shard="unit",
            junit_path=junit,
            coverage_path=coverage,
            source_sha=SHA,
            source_tree=TREE,
        )


def test_junit_corruption_and_dynamic_collection_drift_fail_closed(
    tmp_path: Path,
) -> None:
    canonical = _inventory()
    bad = tmp_path / "bad.xml"
    coverage = tmp_path / ".coverage.unit"
    bad.write_text("<!DOCTYPE x [<!ENTITY a 'x'>]><testsuites/>", encoding="utf-8")
    data = CoverageData(basename=str(coverage))
    data.set_context(f"stock-desk:{SHA}:{TREE}:unit")
    data.add_arcs({"example.py": {(1, 2)}})
    data.write()
    with pytest.raises(aggregate.EvidenceError, match="DTD or entity"):
        aggregate.build_shard_evidence(
            inventory=canonical,
            shard="unit",
            junit_path=bad,
            coverage_path=coverage,
            source_sha=SHA,
            source_tree=TREE,
        )


def test_junit_requires_supported_root_exact_identity_and_unique_cases(
    tmp_path: Path,
) -> None:
    unsupported = tmp_path / "unsupported.xml"
    unsupported.write_text("<report/>\n", encoding="utf-8")
    with pytest.raises(aggregate.EvidenceError, match="unsupported root"):
        aggregate.parse_pytest_junit(
            unsupported, source_sha=SHA, source_tree=TREE, shard="unit"
        )

    missing_identity = tmp_path / "missing-identity.xml"
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite")
    case = ET.SubElement(suite, "testcase")
    properties = ET.SubElement(case, "properties")
    ET.SubElement(
        properties,
        "property",
        name="stock_desk_nodeid",
        value=NODEIDS[0],
    )
    ET.ElementTree(suites).write(
        missing_identity, encoding="utf-8", xml_declaration=True
    )
    with pytest.raises(aggregate.EvidenceError, match="identity does not match"):
        aggregate.parse_pytest_junit(
            missing_identity, source_sha=SHA, source_tree=TREE, shard="unit"
        )

    duplicate = tmp_path / "duplicate.xml"
    _write_junit(duplicate, NODEIDS[0])
    root = ET.parse(duplicate).getroot()
    suite = root.find("testsuite")
    assert suite is not None
    original = suite.find("testcase")
    assert original is not None
    suite.append(copy.deepcopy(original))
    ET.ElementTree(root).write(duplicate, encoding="utf-8", xml_declaration=True)
    with pytest.raises(aggregate.EvidenceError, match="duplicate nodeids"):
        aggregate.parse_pytest_junit(
            duplicate, source_sha=SHA, source_tree=TREE, shard="unit"
        )


def test_coverage_data_requires_branch_mode_and_exact_static_context(
    tmp_path: Path,
) -> None:
    canonical = _inventory()
    junit = tmp_path / "junit.xml"
    _write_junit(junit, NODEIDS[0])
    coverage = tmp_path / ".coverage.unit"
    data = CoverageData(basename=str(coverage))
    data.set_context("stock-desk:stale")
    data.add_arcs({"example.py": {(1, 2)}})
    data.write()
    with pytest.raises(aggregate.EvidenceError, match="context does not match"):
        aggregate.build_shard_evidence(
            inventory=canonical,
            shard="unit",
            junit_path=junit,
            coverage_path=coverage,
            source_sha=SHA,
            source_tree=TREE,
        )

    coverage.unlink()
    lines = CoverageData(basename=str(coverage))
    lines.set_context(f"stock-desk:{SHA}:{TREE}:unit")
    lines.add_lines({"example.py": {1}})
    lines.write()
    with pytest.raises(aggregate.EvidenceError, match="branch mode"):
        aggregate.build_shard_evidence(
            inventory=canonical,
            shard="unit",
            junit_path=junit,
            coverage_path=coverage,
            source_sha=SHA,
            source_tree=TREE,
        )

    drifted = tmp_path / "drifted.xml"
    _write_junit(drifted, "tests/unit/test_sample.py::test_new_dynamic_case")
    with pytest.raises(aggregate.EvidenceError, match="differ from the canonical"):
        aggregate.build_shard_evidence(
            inventory=canonical,
            shard="unit",
            junit_path=drifted,
            coverage_path=coverage,
            source_sha=SHA,
            source_tree=TREE,
        )


def test_coverage_uses_actual_unrounded_value_and_at_least_two_decimals() -> None:
    with pytest.raises(aggregate.EvidenceError, match="84.99% is below 85.00%"):
        aggregate.coverage_totals(_coverage(84.99))
    with pytest.raises(aggregate.EvidenceError, match="at least two"):
        aggregate.coverage_totals(_coverage(85.0), precision=1)

    assert aggregate.coverage_totals(_coverage(85.0))["status"] == "passed"

    with pytest.raises(aggregate.EvidenceError, match="threshold"):
        aggregate.coverage_totals(_coverage(90), threshold=84.99)
    with pytest.raises(aggregate.EvidenceError, match="unsupported schema"):
        aggregate.coverage_totals(
            {"meta": {"format": 99, "branch_coverage": True}, "totals": {}}
        )
    with pytest.raises(aggregate.EvidenceError, match="missing totals"):
        aggregate.coverage_totals({"meta": {"format": 3, "branch_coverage": True}})
    with pytest.raises(aggregate.EvidenceError, match="branch-mode totals"):
        aggregate.coverage_totals(
            {
                "meta": {"format": 3, "branch_coverage": True},
                "totals": {"percent_covered": 90, "num_branches": 0},
            }
        )


def test_shard_evidence_schema_and_digest_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    canonical = _inventory()
    valid = _shard_evidence(tmp_path)[0]

    changed = copy.deepcopy(valid)
    changed["status"] = "failed"
    with pytest.raises(aggregate.EvidenceError, match="not successful"):
        aggregate.validate_shard_evidence(
            changed, source_sha=SHA, source_tree=TREE, inventory=canonical
        )

    changed = copy.deepcopy(valid)
    changed["inventory_sha256"] = "0" * 64
    with pytest.raises(aggregate.EvidenceError, match="stale inventory"):
        aggregate.validate_shard_evidence(
            changed, source_sha=SHA, source_tree=TREE, inventory=canonical
        )

    changed = copy.deepcopy(valid)
    changed["coverage"]["parallel"] = False
    with pytest.raises(aggregate.EvidenceError, match="coverage metadata"):
        aggregate.validate_shard_evidence(
            changed, source_sha=SHA, source_tree=TREE, inventory=canonical
        )

    changed = copy.deepcopy(valid)
    changed["evidence_sha256"] = "0" * 64
    with pytest.raises(aggregate.EvidenceError, match="document digest"):
        aggregate.validate_shard_evidence(
            changed, source_sha=SHA, source_tree=TREE, inventory=canonical
        )


def _requirement_manifest() -> dict[str, object]:
    return {
        "requirements": [
            {
                "id": "R-001",
                "evidence": [
                    {
                        "state": "existing",
                        "runner": "pytest",
                        "path": NODEIDS[0].partition("::")[0],
                        "selector": NODEIDS[0],
                    },
                    {
                        "state": "existing",
                        "runner": "playwright",
                        "path": "web/e2e/market.spec.ts",
                        "selector": "loads an exact real market chart",
                    },
                ],
            }
        ],
        "non_goals": [],
    }


def _pytest_reports(
    tmp_path: Path, *, status: str = "passed"
) -> list[dict[str, object]]:
    reports = _shard_evidence(tmp_path)
    if status != "passed":
        changed = copy.deepcopy(reports[0])
        changed["junit"]["records"][0]["status"] = status
        changed["junit"]["counts"] = aggregate._status_counts(
            changed["junit"]["records"]
        )
        changed["evidence_sha256"] = inventory.sha256_json(
            {key: value for key, value in changed.items() if key != "evidence_sha256"}
        )
        reports[0] = changed
    return reports


def _playwright_report(
    *,
    source_sha: str = SHA,
    selector: str = "loads an exact real market chart",
) -> dict[str, object]:
    return aggregate.build_test_report(
        runner="playwright",
        source_sha=source_sha,
        source_tree=TREE,
        tests=[
            {
                "path": "web/e2e/market.spec.ts",
                "selector": selector,
                "status": "passed",
            }
        ],
    )


def _vitest_report(
    *, selector: str = "renders an exact market chart"
) -> dict[str, object]:
    return aggregate.build_test_report(
        runner="vitest",
        source_sha=SHA,
        source_tree=TREE,
        tests=[
            {
                "path": "web/src/market.test.tsx",
                "selector": selector,
                "status": "passed",
            }
        ],
    )


def test_requirement_selectors_bind_to_unique_same_sha_success_reports(
    tmp_path: Path,
) -> None:
    payload = aggregate.build_requirement_evidence(
        manifest=_requirement_manifest(),
        reports=[*_pytest_reports(tmp_path), _vitest_report(), _playwright_report()],
        source_sha=SHA,
        source_tree=TREE,
        manifest_sha256="f" * 64,
        inventory=_inventory(),
    )

    assert payload["binding_count"] == 2
    assert payload["required_runners"] == ["pytest", "vitest", "playwright"]
    assert payload["schema_authority_collect"] == "passed"
    assert payload["status"] == "passed"


def test_pytest_parameterized_parent_binds_every_exact_report_case(
    tmp_path: Path,
) -> None:
    parent = "tests/acceptance/test_matrix.py::test_every_period_and_scope"
    parameterized = (
        f"{parent}[day-single]",
        f"{parent}[day-pool]",
        f"{parent}[week-single]",
        f"{parent}[week-pool]",
        f"{parent}[min60-single]",
        f"{parent}[min60-pool]",
    )
    canonical = inventory.build_inventory(
        (NODEIDS[0], NODEIDS[1], *parameterized, NODEIDS[3]),
        source_sha=SHA,
        source_tree=TREE,
    )
    manifest = {
        "requirements": [
            {
                "id": "R-014",
                "evidence": [
                    {
                        "state": "existing",
                        "runner": "pytest",
                        "path": "tests/acceptance/test_matrix.py",
                        "selector": parent,
                    }
                ],
            }
        ],
        "non_goals": [],
    }

    payload = aggregate.build_requirement_evidence(
        manifest=manifest,
        reports=_shard_evidence(tmp_path, canonical),
        source_sha=SHA,
        source_tree=TREE,
        manifest_sha256="f" * 64,
        inventory=canonical,
        required_runners=["pytest"],
    )

    assert payload["binding_count"] == 1
    assert payload["bindings"][0]["selector"] == parent


def test_pytest_parameterized_parent_does_not_bind_a_sibling_prefix(
    tmp_path: Path,
) -> None:
    parent = "tests/acceptance/test_matrix.py::test_every_period_and_scope"
    sibling = f"{parent}_extended[day-single]"
    canonical = inventory.build_inventory(
        (NODEIDS[0], NODEIDS[1], sibling, NODEIDS[3]),
        source_sha=SHA,
        source_tree=TREE,
    )
    manifest = {
        "requirements": [
            {
                "id": "R-014",
                "evidence": [
                    {
                        "state": "existing",
                        "runner": "pytest",
                        "path": "tests/acceptance/test_matrix.py",
                        "selector": parent,
                    }
                ],
            }
        ],
        "non_goals": [],
    }

    with pytest.raises(aggregate.EvidenceError, match="no exact-SHA"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=_shard_evidence(tmp_path, canonical),
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=canonical,
            required_runners=["pytest"],
        )


def test_pytest_parameterized_parent_rejects_a_skipped_child(
    tmp_path: Path,
) -> None:
    parent = "tests/acceptance/test_matrix.py::test_every_period_and_scope"
    parameterized = (f"{parent}[day-single]", f"{parent}[day-pool]")
    canonical = inventory.build_inventory(
        (NODEIDS[0], NODEIDS[1], *parameterized, NODEIDS[3]),
        source_sha=SHA,
        source_tree=TREE,
    )
    reports = _shard_evidence(tmp_path, canonical)
    acceptance = next(
        item for item in reports if item["shard"] == "acceptance-performance"
    )
    acceptance["junit"]["records"][0]["status"] = "skipped"
    acceptance["junit"]["counts"] = aggregate._status_counts(
        acceptance["junit"]["records"]
    )
    acceptance["evidence_sha256"] = inventory.sha256_json(
        {key: value for key, value in acceptance.items() if key != "evidence_sha256"}
    )
    manifest = {
        "requirements": [
            {
                "id": "R-014",
                "evidence": [
                    {
                        "state": "existing",
                        "runner": "pytest",
                        "path": "tests/acceptance/test_matrix.py",
                        "selector": parent,
                    }
                ],
            }
        ],
        "non_goals": [],
    }

    with pytest.raises(aggregate.EvidenceError, match="skipped"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=reports,
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=canonical,
            required_runners=["pytest"],
        )


def test_requirement_selector_missing_xfail_duplicate_and_stale_reports_fail(
    tmp_path: Path,
) -> None:
    manifest = _requirement_manifest()
    with pytest.raises(aggregate.EvidenceError, match="no exact-SHA"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=[
                *_pytest_reports(tmp_path),
                _vitest_report(),
                _playwright_report(selector="wrong title"),
            ],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=_inventory(),
        )

    with pytest.raises(aggregate.EvidenceError, match="xfail"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=[
                *_pytest_reports(tmp_path / "xfail", status="xfail"),
                _vitest_report(),
                _playwright_report(),
            ],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=_inventory(),
        )

    pytest_reports = _pytest_reports(tmp_path / "duplicate")
    with pytest.raises(aggregate.EvidenceError, match="duplicate pytest shard"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=[
                *pytest_reports,
                pytest_reports[0],
                _vitest_report(),
                _playwright_report(),
            ],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=_inventory(),
        )

    with pytest.raises(aggregate.EvidenceError, match="source identity"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=[
                *_pytest_reports(tmp_path / "stale"),
                _vitest_report(),
                _playwright_report(source_sha="d" * 40),
            ],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=_inventory(),
        )

    failed_frontend = aggregate.build_test_report(
        runner="playwright",
        source_sha=SHA,
        source_tree=TREE,
        tests=[
            {
                "path": "web/e2e/market.spec.ts",
                "selector": "loads an exact real market chart",
                "status": "failed",
            }
        ],
    )
    with pytest.raises(aggregate.EvidenceError, match="contains a failed"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=[
                *_pytest_reports(tmp_path / "failed-frontend"),
                _vitest_report(),
                failed_frontend,
            ],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=_inventory(),
        )


def test_backend_only_requirement_scope_requires_exact_pytest_evidence(
    tmp_path: Path,
) -> None:
    payload = aggregate.build_requirement_evidence(
        manifest=_requirement_manifest(),
        reports=_pytest_reports(tmp_path),
        source_sha=SHA,
        source_tree=TREE,
        manifest_sha256="f" * 64,
        inventory=_inventory(),
        required_runners=["pytest"],
    )

    assert payload["required_runners"] == ["pytest"]
    assert payload["binding_count"] == 1
    assert {item["runner"] for item in payload["bindings"]} == {"pytest"}

    with pytest.raises(aggregate.EvidenceError, match="outside the required runner"):
        aggregate.build_requirement_evidence(
            manifest=_requirement_manifest(),
            reports=[*_pytest_reports(tmp_path / "outside"), _playwright_report()],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            inventory=_inventory(),
            required_runners=["pytest"],
        )


def test_web_only_scope_requires_unique_vitest_and_playwright_evidence() -> None:
    manifest = _requirement_manifest()
    manifest["requirements"][0]["evidence"].append(
        {
            "state": "existing",
            "runner": "vitest",
            "path": "web/src/market.test.tsx",
            "selector": "renders an exact market chart",
        }
    )
    payload = aggregate.build_requirement_evidence(
        manifest=manifest,
        reports=[_vitest_report(), _playwright_report()],
        source_sha=SHA,
        source_tree=TREE,
        manifest_sha256="f" * 64,
        required_runners=["vitest", "playwright"],
    )

    assert payload["required_runners"] == ["vitest", "playwright"]
    assert payload["binding_count"] == 2

    with pytest.raises(
        aggregate.EvidenceError, match="reports are missing: playwright"
    ):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=[_vitest_report()],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            required_runners=["vitest", "playwright"],
        )
    with pytest.raises(aggregate.EvidenceError, match="no exact-SHA"):
        aggregate.build_requirement_evidence(
            manifest=manifest,
            reports=[_vitest_report(selector="wrong title"), _playwright_report()],
            source_sha=SHA,
            source_tree=TREE,
            manifest_sha256="f" * 64,
            required_runners=["vitest", "playwright"],
        )


@pytest.mark.parametrize(
    ("runner", "classname", "name", "expected_path", "expected_selector"),
    [
        (
            "vitest",
            "src/features/market/MarketPage.test.tsx",
            "MarketPage > loads an exact market chart",
            "web/src/features/market/MarketPage.test.tsx",
            "loads an exact market chart",
        ),
        (
            "playwright",
            "chromium › web/e2e/market.spec.ts",
            "market › loads an exact market chart",
            "web/e2e/market.spec.ts",
            "loads an exact market chart",
        ),
    ],
)
def test_frontend_junit_normalization_binds_exact_path_title_and_source(
    tmp_path: Path,
    runner: str,
    classname: str,
    name: str,
    expected_path: str,
    expected_selector: str,
) -> None:
    junit = tmp_path / f"{runner}.xml"
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite")
    ET.SubElement(suite, "testcase", classname=classname, name=name)
    ET.ElementTree(suites).write(junit, encoding="utf-8", xml_declaration=True)

    payload = aggregate.normalize_frontend_junit(
        junit, runner=runner, source_sha=SHA, source_tree=TREE
    )

    assert payload["source_sha"] == SHA
    assert payload["source_tree"] == TREE
    assert payload["tests"] == [
        {
            "path": expected_path,
            "selector": expected_selector,
            "status": "passed",
        }
    ]


def test_frontend_junit_rejects_ambiguous_or_unrooted_paths(tmp_path: Path) -> None:
    junit = tmp_path / "ambiguous.xml"
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite")
    ET.SubElement(
        suite,
        "testcase",
        classname="web/e2e/a.spec.ts › web/e2e/b.spec.ts",
        name="test",
    )
    ET.ElementTree(suites).write(junit, encoding="utf-8", xml_declaration=True)

    with pytest.raises(aggregate.EvidenceError, match="exactly one"):
        aggregate.normalize_frontend_junit(
            junit, runner="playwright", source_sha=SHA, source_tree=TREE
        )


def test_playwright_junit_basename_resolves_to_one_exact_repo_path(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "web" / "e2e" / "nested" / "market.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("test('exact', () => {})\n", encoding="utf-8")
    junit = tmp_path / "playwright.xml"
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite")
    ET.SubElement(
        suite,
        "testcase",
        classname="market.spec.ts",
        name="loads an exact market chart",
    )
    ET.ElementTree(suites).write(junit, encoding="utf-8", xml_declaration=True)

    payload = aggregate.normalize_frontend_junit(
        junit,
        runner="playwright",
        source_sha=SHA,
        source_tree=TREE,
        repo_root=tmp_path,
    )

    assert payload["tests"][0]["path"] == "web/e2e/nested/market.spec.ts"


def test_playwright_junit_collapses_parameterized_selector_family_fail_closed(
    tmp_path: Path,
) -> None:
    junit = tmp_path / "playwright.xml"
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite")
    ET.SubElement(
        suite,
        "testcase",
        classname="chromium › web/e2e/responsive.spec.ts › wide desktop",
        name="wide desktop › /market has bounded non-overlapping layout",
    )
    failed = ET.SubElement(
        suite,
        "testcase",
        classname="chromium › web/e2e/responsive.spec.ts › mobile portrait",
        name="mobile portrait › /market has bounded non-overlapping layout",
    )
    ET.SubElement(failed, "failure", message="overlap")
    ET.ElementTree(suites).write(junit, encoding="utf-8", xml_declaration=True)

    payload = aggregate.normalize_frontend_junit(
        junit,
        runner="playwright",
        source_sha=SHA,
        source_tree=TREE,
    )

    assert payload["status"] == "failed"
    assert payload["tests"] == [
        {
            "path": "web/e2e/responsive.spec.ts",
            "selector": "/market has bounded non-overlapping layout",
            "status": "failed",
        }
    ]


def test_shard_normalize_and_aggregate_cli_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canonical = _inventory()
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_bytes(inventory.canonical_json(canonical))
    evidence = _shard_evidence(tmp_path / "shards")
    shard_paths: list[Path] = []
    for item in evidence:
        path = tmp_path / "shards" / item["shard"] / "shard-evidence.json"
        path.write_bytes(inventory.canonical_json(item))
        shard_paths.append(path)

    unit = tmp_path / "shards" / "unit"
    rebuilt = unit / "rebuilt.json"
    assert (
        aggregate.main(
            [
                "shard",
                "--inventory",
                str(inventory_path),
                "--shard",
                "unit",
                "--junit",
                str(unit / "junit.xml"),
                "--coverage-data",
                str(unit / ".coverage.unit"),
                "--source-sha",
                SHA,
                "--source-tree",
                TREE,
                "--output",
                str(rebuilt),
            ]
        )
        == 0
    )

    frontend_junit = tmp_path / "vitest.xml"
    suites = ET.Element("testsuites")
    suite = ET.SubElement(suites, "testsuite")
    ET.SubElement(
        suite,
        "testcase",
        classname="src/market.test.tsx",
        name="market > exact title",
    )
    ET.ElementTree(suites).write(frontend_junit, encoding="utf-8", xml_declaration=True)
    frontend_output = tmp_path / "vitest-report.json"
    assert (
        aggregate.main(
            [
                "normalize-frontend-junit",
                "--runner",
                "vitest",
                "--junit",
                str(frontend_junit),
                "--source-sha",
                SHA,
                "--source-tree",
                TREE,
                "--repo-root",
                str(tmp_path),
                "--output",
                str(frontend_output),
            ]
        )
        == 0
    )

    coverage_report = tmp_path / "coverage.json"
    coverage_report.write_text(json.dumps(_coverage(85.1)), encoding="utf-8")
    monkeypatch.setattr(
        aggregate,
        "combine_coverage",
        lambda _manifests, _workdir: (coverage_report, _coverage(85.1)),
    )
    requirement = tmp_path / "requirement-evidence.json"
    requirement.write_text("{}\n", encoding="utf-8")
    aggregate_output = tmp_path / "python-evidence.json"
    arguments = [
        "aggregate",
        "--inventory",
        str(inventory_path),
        "--source-sha",
        SHA,
        "--source-tree",
        TREE,
        "--workdir",
        str(tmp_path / "work"),
        "--requirement-evidence",
        str(requirement),
        "--output",
        str(aggregate_output),
    ]
    for path in shard_paths:
        arguments.extend(["--shard-evidence", str(path)])
    assert aggregate.main(arguments) == 0
    assert json.loads(aggregate_output.read_text())["status"] == "passed"
    assert "passed for" in capsys.readouterr().out


def test_requirement_cli_runs_schema_collect_then_offline_report_cross_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _inventory()
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_bytes(inventory.canonical_json(canonical))
    reports = _shard_evidence(tmp_path / "shards")
    reports.append(_vitest_report())
    reports.append(_playwright_report())
    report_paths: list[Path] = []
    for index, report in enumerate(reports):
        path = tmp_path / f"report-{index}.json"
        path.write_bytes(inventory.canonical_json(report))
        report_paths.append(path)
    manifest_path = tmp_path / "requirements.yml"
    manifest_path.write_text("placeholder\n", encoding="utf-8")
    manifest = _requirement_manifest()
    validated: list[str] = []
    monkeypatch.setattr(
        aggregate.check_requirement_coverage,
        "load_manifest",
        lambda _path: manifest,
    )
    monkeypatch.setattr(
        aggregate.check_requirement_coverage,
        "validate_manifest",
        lambda *_args, **_kwargs: validated.append("schema-authority-collect"),
    )
    output = tmp_path / "requirement-output.json"
    arguments = [
        "requirements",
        "--manifest",
        str(manifest_path),
        "--inventory",
        str(inventory_path),
        "--repo-root",
        str(tmp_path),
        "--source-sha",
        SHA,
        "--source-tree",
        TREE,
        "--output",
        str(output),
    ]
    for path in report_paths:
        arguments.extend(["--report", str(path)])

    assert aggregate.main(arguments) == 0
    assert validated == ["schema-authority-collect"]
    assert json.loads(output.read_text())["schema_authority_collect"] == "passed"


def test_requirement_cli_accepts_web_only_runner_scope_without_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _requirement_manifest()
    manifest["requirements"][0]["evidence"].append(
        {
            "state": "existing",
            "runner": "vitest",
            "path": "web/src/market.test.tsx",
            "selector": "renders an exact market chart",
        }
    )
    manifest_path = tmp_path / "requirements.yml"
    manifest_path.write_text("placeholder\n", encoding="utf-8")
    reports = [_vitest_report(), _playwright_report()]
    report_paths: list[Path] = []
    for index, report in enumerate(reports):
        path = tmp_path / f"web-report-{index}.json"
        path.write_bytes(inventory.canonical_json(report))
        report_paths.append(path)
    collected_scopes: list[frozenset[str] | None] = []
    monkeypatch.setattr(
        aggregate.check_requirement_coverage,
        "load_manifest",
        lambda _path: manifest,
    )
    monkeypatch.setattr(
        aggregate.check_requirement_coverage,
        "validate_manifest",
        lambda *_args, **kwargs: collected_scopes.append(kwargs["selector_runners"]),
    )
    output = tmp_path / "web-requirements.json"
    arguments = [
        "requirements",
        "--manifest",
        str(manifest_path),
        "--repo-root",
        str(tmp_path),
        "--source-sha",
        SHA,
        "--source-tree",
        TREE,
        "--required-runner",
        "vitest",
        "--required-runner",
        "playwright",
        "--output",
        str(output),
    ]
    for path in report_paths:
        arguments.extend(["--report", str(path)])

    assert aggregate.main(arguments) == 0
    assert collected_scopes == [frozenset({"vitest", "playwright"})]
    assert json.loads(output.read_text())["required_runners"] == [
        "vitest",
        "playwright",
    ]


def test_evidence_cli_fails_closed_on_invalid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    assert (
        aggregate.main(
            [
                "shard",
                "--inventory",
                str(invalid),
                "--shard",
                "unit",
                "--junit",
                str(invalid),
                "--coverage-data",
                str(invalid),
                "--source-sha",
                SHA,
                "--source-tree",
                TREE,
                "--output",
                str(tmp_path / "out.json"),
            ]
        )
        == 1
    )
    assert "CI evidence error" in capsys.readouterr().err
