from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.verify_ci_cache_policy import (
    CachePolicyError,
    compare_clean_and_warm_runs,
    main,
    validate_cache_policy,
    verify_workflow_cache_policy,
)


def _entry(ecosystem: str) -> dict[str, object]:
    definitions: dict[str, tuple[str, str, str, list[str]]] = {
        "uv": (
            "~/.cache/uv",
            "${{ matrix.python-version }}",
            "${{ hashFiles('uv.lock') }}",
            ["dependency-downloads"],
        ),
        "pnpm": (
            "~/.local/share/pnpm/store",
            "${{ env.NODE_VERSION }}-pnpm-${{ env.PNPM_VERSION }}",
            "${{ hashFiles('pnpm-lock.yaml') }}",
            ["dependency-downloads"],
        ),
        "playwright": (
            "~/.cache/ms-playwright",
            "${{ env.NODE_VERSION }}-playwright-${{ env.PLAYWRIGHT_VERSION }}",
            "${{ hashFiles('pnpm-lock.yaml') }}",
            ["browser-binaries"],
        ),
        "cargo": (
            "~/.cargo/registry",
            "${{ env.RUST_VERSION }}",
            "${{ hashFiles('src-tauri/Cargo.lock') }}",
            ["dependency-downloads"],
        ),
    }
    path, toolchain, lockfile, content_classes = definitions[ecosystem]
    dimensions = {
        "os": "${{ runner.os }}",
        "architecture": "${{ runner.arch }}",
        "toolchain": toolchain,
        "lockfile": lockfile,
    }
    return {
        "name": f"{ecosystem}-cache",
        "ecosystem": ecosystem,
        "paths": [path],
        "key": "-".join(dimensions.values()),
        "dimensions": dimensions,
        "content_classes": content_classes,
    }


def _policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "entries": [_entry(name) for name in ("uv", "pnpm", "playwright", "cargo")],
    }


def test_uv_pnpm_playwright_and_cargo_have_environment_complete_keys() -> None:
    policy = validate_cache_policy(_policy())

    assert [entry["ecosystem"] for entry in policy["entries"]] == [
        "cargo",
        "playwright",
        "pnpm",
        "uv",
    ]
    for entry in policy["entries"]:
        assert set(entry["dimensions"]) == {
            "os",
            "architecture",
            "toolchain",
            "lockfile",
        }
        assert all(value in entry["key"] for value in entry["dimensions"].values())


@pytest.mark.parametrize("missing", ["os", "architecture", "toolchain", "lockfile"])
def test_cache_key_missing_any_required_dimension_is_rejected(missing: str) -> None:
    policy = _policy()
    entry = policy["entries"][0]
    assert isinstance(entry, dict)
    dimensions = entry["dimensions"]
    assert isinstance(dimensions, dict)
    entry["key"] = "-".join(
        value for name, value in dimensions.items() if name != missing
    )

    with pytest.raises(CachePolicyError, match=f"does not include {missing}"):
        validate_cache_policy(policy)


@pytest.mark.parametrize(
    "path",
    [
        "test-results/junit.xml",
        ".coverage",
        "coverage/report.xml",
        ".pytest_cache",
        "state/stock-desk.db",
        "state/cache.sqlite3",
        "artifacts/web.tar",
        "evidence/release-proof.json",
        "evidence/attestation.json",
        "signing/signature.p7s",
        "dist/stock-desk.exe",
        "release/stock-desk.exe",
        "sbom/cyclonedx.json",
        "provenance/statement.json",
    ],
)
def test_test_results_databases_identity_signing_and_final_artifacts_are_rejected(
    path: str,
) -> None:
    policy = _policy()
    entry = policy["entries"][0]
    assert isinstance(entry, dict)
    entry["paths"] = [path]

    with pytest.raises(CachePolicyError, match="prohibited cache content path"):
        validate_cache_policy(policy)


@pytest.mark.parametrize(
    "content_class",
    [
        "test-conclusions",
        "database",
        "junit",
        "coverage",
        "requirement-evidence",
        "signature",
        "release-proof",
        "artifact-identity",
    ],
)
def test_only_explicit_non_conclusion_cache_classes_are_allowed(
    content_class: str,
) -> None:
    policy = _policy()
    entry = policy["entries"][0]
    assert isinstance(entry, dict)
    entry["content_classes"] = [content_class]

    with pytest.raises(CachePolicyError, match="prohibited content classes"):
        validate_cache_policy(policy)


def test_rustsec_cache_may_hold_only_the_pinned_tool_and_advisory_database() -> None:
    entry = _entry("cargo")
    entry["paths"] = ["~/.cargo/bin/cargo-audit", "~/.cargo/advisory-db"]
    entry["content_classes"] = ["audit-tool", "vulnerability-database"]
    policy = validate_cache_policy({"schema_version": 1, "entries": [entry]})

    assert policy["entries"][0]["content_classes"] == [
        "audit-tool",
        "vulnerability-database",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "~/arbitrary/.cargo/bin/cargo-audit-backup",
        "~/.cargo/advisory-db-export",
        "~/.cache/uv/../release-proof",
    ],
)
def test_near_match_or_non_normalized_cache_path_is_rejected(path: str) -> None:
    entry = _entry("cargo" if ".cargo" in path else "uv")
    entry["paths"] = [path]

    with pytest.raises(
        CachePolicyError, match="not an allowed|must be normalized|cannot contain"
    ):
        validate_cache_policy({"schema_version": 1, "entries": [entry]})


def test_mixed_rustsec_and_dependency_cache_requires_all_content_classes() -> None:
    entry = _entry("cargo")
    entry["paths"] = ["~/.cargo/bin/cargo-audit", "~/.cargo/registry"]
    entry["content_classes"] = ["audit-tool"]

    with pytest.raises(CachePolicyError, match="exactly match cache paths"):
        validate_cache_policy({"schema_version": 1, "entries": [entry]})


def test_workflow_inventory_rejects_near_match_rustsec_path(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        """      - uses: actions/cache@0000000000000000000000000000000000000000
        with:
          path: ~/.cargo/bin/cargo-audit-backup
          key: ${{ runner.os }}-${{ runner.arch }}-${{ env.RUST_VERSION }}-${{ hashFiles('src-tauri/Cargo.lock') }}
""",
    )

    with pytest.raises(CachePolicyError, match="exactly one supported ecosystem"):
        verify_workflow_cache_policy([path])


def test_lockfile_dimension_must_be_a_content_digest() -> None:
    policy = _policy()
    entry = policy["entries"][0]
    assert isinstance(entry, dict)
    dimensions = entry["dimensions"]
    assert isinstance(dimensions, dict)
    old = dimensions["lockfile"]
    dimensions["lockfile"] = "uv.lock"
    assert isinstance(entry["key"], str)
    entry["key"] = entry["key"].replace(str(old), "uv.lock")

    with pytest.raises(CachePolicyError, match="lockfile dimension"):
        validate_cache_policy(policy)


def test_ecosystem_cannot_cache_an_arbitrary_directory() -> None:
    policy = _policy()
    entry = policy["entries"][0]
    assert isinstance(entry, dict)
    entry["paths"] = ["~/.cache/everything"]

    with pytest.raises(CachePolicyError, match="not an allowed uv intermediate"):
        validate_cache_policy(policy)


def _run_evidence(state: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "cache_state": state,
        "source_sha": "1" * 40,
        "source_tree": "2" * 40,
        "install_completed": True,
        "required_gates": {
            "python-unit": "success",
            "python-security": "success",
            "artifact-proof": "success",
        },
        "artifact_manifests": {"python": "3" * 64, "web": "4" * 64},
    }


def test_clean_miss_and_warm_cache_must_run_the_same_successful_gates() -> None:
    compare_clean_and_warm_runs(_run_evidence("clean-miss"), _run_evidence("warm"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "required_gates",
            {"python-unit": "success"},
            "differ in required_gates",
        ),
        ("source_sha", "9" * 40, "differ in source_sha"),
        ("source_tree", "9" * 40, "differ in source_tree"),
        ("artifact_manifests", {"python": "3" * 64}, "different artifact sets"),
    ],
)
def test_warm_cache_cannot_change_gates_source_or_artifact_set(
    field: str, value: object, message: str
) -> None:
    clean = _run_evidence("clean-miss")
    warm = deepcopy(_run_evidence("warm"))
    warm[field] = value

    with pytest.raises(CachePolicyError, match=message):
        compare_clean_and_warm_runs(clean, warm)


def test_clean_miss_must_complete_install() -> None:
    clean = _run_evidence("clean-miss")
    clean["install_completed"] = False

    with pytest.raises(CachePolicyError, match="installation must complete"):
        compare_clean_and_warm_runs(clean, _run_evidence("warm"))


def test_failed_gate_cannot_be_hidden_by_cache() -> None:
    warm = _run_evidence("warm")
    gates = warm["required_gates"]
    assert isinstance(gates, dict)
    gates["python-security"] = "failure"

    with pytest.raises(CachePolicyError, match="every required gate"):
        compare_clean_and_warm_runs(_run_evidence("clean-miss"), warm)


def test_cli_verifies_policy_and_run_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_path = tmp_path / "policy.json"
    clean_path = tmp_path / "clean.json"
    warm_path = tmp_path / "warm.json"
    policy_path.write_text(json.dumps(_policy()), encoding="utf-8")
    clean_path.write_text(json.dumps(_run_evidence("clean-miss")), encoding="utf-8")
    warm_path.write_text(json.dumps(_run_evidence("warm")), encoding="utf-8")

    assert main(["policy", str(policy_path)]) == 0
    assert capsys.readouterr().out == "valid cache entries: 4\n"
    assert (
        main(["compare-runs", "--clean", str(clean_path), "--warm", str(warm_path)])
        == 0
    )
    assert capsys.readouterr().out == "clean-cache and warm-cache gates match\n"


def _workflow(tmp_path: Path, cache_step: str) -> Path:
    path = tmp_path / "ci.yml"
    path.write_text(
        """name: CI
on: push
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000
"""
        + cache_step
        + "\n",
        encoding="utf-8",
    )
    return path


def test_workflow_inventory_validates_explicit_cache_step(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        """      - name: Cache uv downloads
        uses: actions/cache@0000000000000000000000000000000000000000
        with:
          path: ~/.cache/uv
          key: ${{ runner.os }}-${{ runner.arch }}-${{ matrix.python-version }}-${{ hashFiles('uv.lock') }}
""",
    )

    assert verify_workflow_cache_policy([path]) == 1


@pytest.mark.parametrize(
    ("cache_step", "message"),
    [
        (
            """      - uses: actions/setup-node@0000000000000000000000000000000000000000
        with:
          cache: pnpm
""",
            "implicit action cache",
        ),
        (
            """      - uses: astral-sh/setup-uv@0000000000000000000000000000000000000000
        with:
          enable-cache: true
""",
            "implicit action cache",
        ),
        (
            """      - uses: actions/cache@0000000000000000000000000000000000000000
        with:
          path: ~/.cache/uv
          key: ${{ runner.os }}-${{ runner.arch }}-${{ matrix.python-version }}-${{ hashFiles('uv.lock') }}
          restore-keys: uv-
""",
            "restore keys are forbidden",
        ),
        (
            """      - uses: actions/cache@0000000000000000000000000000000000000000
        with:
          path: ~/.cache/uv
          key: ${{ runner.os }}-${{ matrix.python-version }}-${{ hashFiles('uv.lock') }}
""",
            "does not include architecture",
        ),
    ],
)
def test_workflow_inventory_rejects_implicit_inexact_or_fallback_caches(
    tmp_path: Path, cache_step: str, message: str
) -> None:
    path = _workflow(tmp_path, cache_step)

    with pytest.raises(CachePolicyError, match=message):
        verify_workflow_cache_policy([path])


def test_workflow_cli_reports_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _workflow(
        tmp_path,
        """      - uses: actions/cache@0000000000000000000000000000000000000000
        with:
          path: ~/.cache/ms-playwright
          key: ${{ runner.os }}-${{ runner.arch }}-${{ env.PLAYWRIGHT_VERSION }}-${{ hashFiles('pnpm-lock.yaml') }}
""",
    )

    assert main(["workflows", str(path)]) == 0
    assert capsys.readouterr().out == "valid workflow cache entries: 1\n"
