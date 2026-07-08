from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Any

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
_YAML_BOOLEAN_TAG = "tag:yaml.org,2002:bool"


class _GitHubActionsLoader(yaml.SafeLoader):
    """Apply GitHub's YAML 1.2-style boolean rules without global mutation."""


_GitHubActionsLoader.yaml_implicit_resolvers = {
    initial: [resolver for resolver in resolvers if resolver[0] != _YAML_BOOLEAN_TAG]
    for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_GitHubActionsLoader.add_implicit_resolver(
    _YAML_BOOLEAN_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

REQUIRED_FILES = {
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CHANGELOG.md",
    "ROADMAP.md",
    "docs/architecture.md",
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md",
    ".github/dependabot.yml",
    ".github/codeql/codeql-config.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/release.yml",
    ".github/workflows/security.yml",
}

VERIFIED_ACTION_PINS = {
    "actions/attest": (
        "59d89421af93a897026c735860bf21b6eb4f7b26",
        "v4.1.0",
    ),
    "actions/checkout": (
        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
        "v7.0.0",
    ),
    "actions/download-artifact": (
        "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        "v8.0.1",
    ),
    "actions/setup-node": (
        "48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
        "v6.4.0",
    ),
    "actions/setup-python": (
        "ece7cb06caefa5fff74198d8649806c4678c61a1",
        "v6.3.0",
    ),
    "actions/upload-artifact": (
        "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "v7.0.1",
    ),
    "astral-sh/setup-uv": (
        "d31148d669074a8d0a63714ba94f3201e7020bc3",
        "v8.3.0",
    ),
    "anchore/sbom-action": (
        "e22c389904149dbc22b58101806040fa8d37a610",
        "v0.24.0",
    ),
    "github/codeql-action/analyze": (
        "54f647b7e1bb85c95cddabcd46b0c578ec92bc1a",
        "v4.36.3",
    ),
    "github/codeql-action/init": (
        "54f647b7e1bb85c95cddabcd46b0c578ec92bc1a",
        "v4.36.3",
    ),
    "pnpm/action-setup": (
        "0ebf47130e4866e96fce0953f49152a61190b271",
        "v6.0.9",
    ),
    "actions/dependency-review-action": (
        "2031cfc080254a8a887f58cffee85186f0e49e48",
        "v4.9.0",
    ),
    "aquasecurity/trivy-action": (
        "57a97c7e7821a5776cebc9bb87c984fa69cba8f1",
        "0.35.0",
    ),
}


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _load_yaml(relative_path: str) -> Any:
    return yaml.safe_load(_read(relative_path))


def _load_github_actions_yaml(content: str) -> Any:
    loader = _GitHubActionsLoader(content)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _workflow_triggers(workflow: Any) -> dict[str, Any]:
    assert isinstance(workflow, dict)
    assert "on" in workflow, "workflow must define a string 'on' key"
    triggers = workflow["on"]
    assert isinstance(triggers, dict), "workflow 'on' value must be a mapping"
    return triggers


def _workflow_paths() -> list[Path]:
    return sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))


def test_market_lake_runtime_dependency_is_exact_and_locked() -> None:
    project = tomllib.loads(_read("pyproject.toml"))
    runtime_dependencies = project["project"]["dependencies"]

    assert "duckdb>=1.4.5,<1.5" in runtime_dependencies
    assert not any(
        dependency.partition("[")[0].partition("=")[0].casefold()
        in {"pandas", "pyarrow"}
        for dependency in runtime_dependencies
    )

    locked = tomllib.loads(_read("uv.lock"))
    stock_desk = next(
        package for package in locked["package"] if package["name"] == "stock-desk"
    )
    direct_names = {dependency["name"] for dependency in stock_desk["dependencies"]}
    assert "duckdb" in direct_names
    assert {"pandas", "pyarrow"}.isdisjoint(direct_names)
    duckdb_metadata = next(
        dependency
        for dependency in stock_desk["metadata"]["requires-dist"]
        if dependency["name"] == "duckdb"
    )
    assert duckdb_metadata["specifier"] == ">=1.4.5,<1.5"


def test_model_transport_runtime_dependency_is_exact_and_locked() -> None:
    project = tomllib.loads(_read("pyproject.toml"))
    runtime_dependencies = project["project"]["dependencies"]
    development_dependencies = project["dependency-groups"]["dev"]

    assert "httpx2>=2,<3" in runtime_dependencies
    assert "httpx2>=2,<3" not in development_dependencies

    locked = tomllib.loads(_read("uv.lock"))
    stock_desk = next(
        package for package in locked["package"] if package["name"] == "stock-desk"
    )
    direct_names = {dependency["name"] for dependency in stock_desk["dependencies"]}
    assert "httpx2" in direct_names
    httpx2_metadata = next(
        dependency
        for dependency in stock_desk["metadata"]["requires-dist"]
        if dependency["name"] == "httpx2"
    )
    assert httpx2_metadata["specifier"] == ">=2,<3"


def test_required_open_source_files_exist() -> None:
    missing = sorted(
        path for path in REQUIRED_FILES if not (REPO_ROOT / path).is_file()
    )
    assert missing == []


def test_readme_language_switches_are_the_exact_first_lines() -> None:
    assert _read("README.md").splitlines()[0] == "[简体中文](README.zh-CN.md)"
    assert _read("README.zh-CN.md").splitlines()[0] == "[English](README.md)"


def test_local_markdown_links_resolve() -> None:
    markdown_paths = [
        REPO_ROOT / path for path in REQUIRED_FILES if path.endswith(".md")
    ]
    markdown_paths.extend(sorted((REPO_ROOT / "docs").rglob("*.md")))
    unresolved: list[str] = []
    link_pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

    for markdown_path in sorted(set(markdown_paths)):
        for raw_target in link_pattern.findall(markdown_path.read_text("utf-8")):
            target = raw_target.strip().strip("<>").split()[0]
            if target.startswith(("#", "https://", "http://", "mailto:")):
                continue
            relative_target = target.split("#", maxsplit=1)[0]
            if (
                relative_target
                and not (markdown_path.parent / relative_target).exists()
            ):
                unresolved.append(
                    f"{markdown_path.relative_to(REPO_ROOT)} -> {relative_target}"
                )

    assert unresolved == []


def test_public_documentation_does_not_expose_private_working_paths() -> None:
    forbidden_tokens = (
        "." + "agents",
        "." + "codex",
        "." + "superpowers",
        "docs/" + "superpowers",
        "open" + "spec",
        "outputs/",
        "work/",
    )
    public_docs = [
        path
        for path in REPO_ROOT.glob("*.md")
        if path.name not in {"CODE_OF_CONDUCT.md"}
    ]
    public_docs.extend((REPO_ROOT / "docs").rglob("*.md"))

    leaks = {
        str(path.relative_to(REPO_ROOT)): token
        for path in public_docs
        for token in forbidden_tokens
        if token.casefold() in path.read_text("utf-8").casefold()
    }
    assert leaks == {}


def test_license_is_the_official_apache_2_text() -> None:
    license_bytes = (REPO_ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    )


def test_code_of_conduct_is_contributor_covenant_2_1_with_private_contact() -> None:
    content = _read("CODE_OF_CONDUCT.md")
    assert "Contributor Covenant Code of Conduct" in content
    assert "version 2.1" in content
    assert "Community Impact Guidelines" in content
    assert "bao_cong@outlook.com" in content
    assert "INSERT CONTACT METHOD" not in content


def test_github_yaml_is_valid_and_issue_forms_have_safety_checks() -> None:
    yaml_paths = sorted((REPO_ROOT / ".github").rglob("*.yml"))
    assert yaml_paths
    for path in yaml_paths:
        assert yaml.safe_load(path.read_text("utf-8")) is not None, path

    for template in ("bug_report.yml", "feature_request.yml"):
        form = _load_yaml(f".github/ISSUE_TEMPLATE/{template}")
        assert form["name"]
        assert form["description"]
        assert form["body"]
        serialized = _read(f".github/ISSUE_TEMPLATE/{template}").casefold()
        assert "code of conduct" in serialized
        assert "secret" in serialized or "sensitive" in serialized

    config = _load_yaml(".github/ISSUE_TEMPLATE/config.yml")
    assert config["blank_issues_enabled"] is False


def test_github_actions_loader_preserves_ambiguous_keys_and_real_booleans() -> None:
    content = """\
on:
  push:
On: mixed-case-key
OFF: uppercase-key
enabled: true
disabled: false
"""

    workflow = _load_github_actions_yaml(content)

    assert list(workflow) == ["on", "On", "OFF", "enabled", "disabled"]
    assert workflow["enabled"] is True
    assert workflow["disabled"] is False
    assert yaml.safe_load("on: value\n") == {True: "value"}


def test_workflow_trigger_validation_rejects_missing_or_misspelled_on() -> None:
    invalid_workflows = (
        "name: Missing\njobs: {}\n",
        "name: Misspelled\nonn:\n  push:\njobs: {}\n",
    )

    for content in invalid_workflows:
        with pytest.raises(AssertionError, match="string 'on' key"):
            _workflow_triggers(_load_github_actions_yaml(content))


def test_workflows_declare_the_expected_github_triggers() -> None:
    expected_triggers = {
        "ci.yml": {"push", "pull_request"},
        "codeql.yml": {"push", "pull_request", "schedule"},
        "release.yml": {"push"},
        "security.yml": {"push", "pull_request"},
    }
    loaded_triggers: dict[str, dict[str, Any]] = {}

    for workflow_path in _workflow_paths():
        triggers = _workflow_triggers(
            _load_github_actions_yaml(workflow_path.read_text("utf-8"))
        )
        loaded_triggers[workflow_path.name] = triggers
        assert set(triggers) == expected_triggers[workflow_path.name]

    assert loaded_triggers["release.yml"] == {"push": {"tags": ["v*"]}}


def test_security_workflow_fails_closed_on_dependencies_sbom_and_image_cves() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/security.yml"))
    serialized = _read(".github/workflows/security.yml")
    assert "pull_request_target" not in serialized
    assert "secrets." not in serialized
    assert workflow["permissions"] == {"contents": "read"}

    dependency_review = workflow["jobs"]["dependency-review"]
    assert dependency_review["if"] == "github.event_name == 'pull_request'"
    assert dependency_review["permissions"] == {"contents": "read"}
    review_step = next(
        step
        for step in dependency_review["steps"]
        if str(step.get("uses", "")).startswith("actions/dependency-review-action@")
    )
    assert review_step["with"]["fail-on-severity"] == "high"

    audit = workflow["jobs"]["locked-audit"]
    assert audit["permissions"] == {"contents": "read"}
    audit_commands = "\n".join(str(step.get("run", "")) for step in audit["steps"])
    assert "make security" in audit_commands

    image = workflow["jobs"]["container-security"]
    assert image["permissions"] == {"contents": "read"}
    image_steps = image["steps"]
    assert any(
        str(step.get("uses", "")).startswith("anchore/sbom-action@")
        and step["with"]["format"] == "spdx-json"
        and step["with"]["output-file"] == "stock-desk-image.spdx.json"
        and step["with"]["upload-artifact"] is False
        and step["with"]["upload-release-assets"] is False
        for step in image_steps
    )
    trivy_steps = [
        step
        for step in image_steps
        if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
    ]
    assert [step["name"] for step in trivy_steps] == [
        "Report all high and critical image CVEs",
        "Reject fixable high and critical image CVEs",
    ]
    report, gate = trivy_steps
    assert report["with"] == {
        "image-ref": "stock-desk:security",
        "format": "json",
        "output": "stock-desk-image-vulnerabilities.json",
        "severity": "CRITICAL,HIGH",
        "ignore-unfixed": False,
        "exit-code": 0,
    }
    assert gate["with"] == {
        "image-ref": "stock-desk:security",
        "format": "table",
        "severity": "CRITICAL,HIGH",
        "ignore-unfixed": True,
        "exit-code": 1,
    }
    vulnerability_upload = next(
        step
        for step in image_steps
        if step.get("name") == "Upload image vulnerability report"
    )
    assert vulnerability_upload["with"] == {
        "name": "stock-desk-image-vulnerabilities",
        "path": "stock-desk-image-vulnerabilities.json",
        "if-no-files-found": "error",
        "retention-days": 30,
    }
    assert image_steps.index(report) < image_steps.index(vulnerability_upload)
    assert image_steps.index(vulnerability_upload) < image_steps.index(gate)


def test_tag_release_generates_and_attests_sbom_and_artifacts() -> None:
    release = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    verify = release["jobs"]["verify"]
    assert verify["permissions"] == {"contents": "read"}
    steps = verify["steps"]
    names = [step.get("name") for step in steps]
    archive_index = names.index("Prepare release archives")
    sbom_index = names.index("Generate release SBOM")
    checksums_index = names.index("Prepare checksummed assets")
    upload_index = names.index("Upload verified release assets for attestation")
    assert archive_index < sbom_index < checksums_index < upload_index
    assert not any("Attest" in str(name) for name in names)
    sbom = steps[sbom_index]
    assert str(sbom["uses"]).startswith("anchore/sbom-action@")
    assert sbom["with"]["path"] == "dist"
    assert sbom["with"]["format"] == "spdx-json"
    assert sbom["with"]["output-file"] == "dist/stock-desk.spdx.json"
    assert sbom["with"]["upload-artifact"] is False
    assert sbom["with"]["upload-release-assets"] is False

    attest = release["jobs"]["attest"]
    assert set(attest["needs"]) == {
        "verify",
        "verify-windows-installer",
        "verify-macos-installer",
    }
    assert attest["permissions"] == {
        "attestations": "write",
        "contents": "read",
        "id-token": "write",
    }
    for job_name, job in release["jobs"].items():
        if job_name == "attest":
            continue
        permissions = job.get("permissions", {})
        assert "attestations" not in permissions
        assert "id-token" not in permissions
    attest_steps = attest["steps"]
    attest_names = [step.get("name") for step in attest_steps]
    assert attest_names == [
        "Download verified release assets",
        "Download verified native installer assets",
        "Verify release asset checksums",
        "Verify native assets and prepare complete checksums",
        "Attest release provenance",
        "Attest release SBOM",
        "Attest Windows installer SBOM",
        "Attest macOS x86_64 installer SBOM",
        "Attest macOS arm64 installer SBOM",
        "Upload attested release assets",
    ]
    provenance = attest_steps[attest_names.index("Attest release provenance")]
    assert str(provenance["uses"]).startswith("actions/attest@")
    provenance_subjects = provenance["with"]["subject-path"].splitlines()
    assert provenance_subjects == [
        "release-assets/*.whl",
        "release-assets/*.tar.gz",
        "release-assets/*.exe",
        "release-assets/*.dmg",
    ]
    sbom_attestation = attest_steps[attest_names.index("Attest release SBOM")]
    assert str(sbom_attestation["uses"]).startswith("actions/attest@")
    assert sbom_attestation["with"] == {
        "subject-path": "release-assets/*.{whl,tar.gz}",
        "sbom-path": "release-assets/stock-desk.spdx.json",
    }
    for platform, suffix in (
        ("Windows", "windows-x86_64.exe"),
        ("macOS x86_64", "macos-x86_64.dmg"),
        ("macOS arm64", "macos-arm64.dmg"),
    ):
        installer_attestation = attest_steps[
            attest_names.index(f"Attest {platform} installer SBOM")
        ]
        assert str(installer_attestation["uses"]).startswith("actions/attest@")
        assert installer_attestation["with"] == {
            "subject-path": f"release-assets/*-{suffix}",
            "sbom-path": f"release-assets/stock-desk-{suffix.rsplit('.', 1)[0]}.sbom.spdx.json",
        }

    native_verification = attest_steps[
        attest_names.index("Verify native assets and prepare complete checksums")
    ]["run"]
    assert 'test "${#installers[@]}" -eq 3' in native_verification
    assert 'sha256sum -c "$sidecar"' in native_verification
    assert "SHA256SUMS.complete" in native_verification
    assert "! -name '*.sha256'" not in native_verification
    assert "! -name 'SHA256SUMS'" not in native_verification
    assert "wc -l < SHA256SUMS.complete" in native_verification

    codeql = release["jobs"]["codeql"]
    assert codeql["permissions"] == {
        "contents": "read",
        "security-events": "write",
    }
    assert codeql["strategy"]["matrix"]["language"] == [
        "python",
        "javascript-typescript",
    ]
    assert [step.get("name") for step in codeql["steps"]] == [
        "Check out source",
        "Initialize CodeQL",
        "Analyze",
    ]
    assert any(
        str(step.get("uses", "")).startswith("github/codeql-action/analyze@")
        for step in codeql["steps"]
    )

    container = release["jobs"]["container"]
    trivy_steps = [
        step
        for step in container["steps"]
        if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
    ]
    assert [step["name"] for step in trivy_steps] == [
        "Report all high and critical release image CVEs",
        "Reject fixable high and critical release image CVEs",
    ]
    report, gate = trivy_steps
    assert report["with"] == {
        "image-ref": "stock-desk-runtime:local",
        "format": "json",
        "output": "stock-desk-release-image-vulnerabilities.json",
        "severity": "CRITICAL,HIGH",
        "ignore-unfixed": False,
        "exit-code": 0,
    }
    assert gate["with"] == {
        "image-ref": "stock-desk-runtime:local",
        "format": "table",
        "severity": "CRITICAL,HIGH",
        "ignore-unfixed": True,
        "exit-code": 1,
    }
    vulnerability_upload = next(
        step
        for step in container["steps"]
        if step.get("name") == "Upload release image vulnerability report"
    )
    assert vulnerability_upload["with"] == {
        "name": "stock-desk-release-image-vulnerabilities",
        "path": "stock-desk-release-image-vulnerabilities.json",
        "if-no-files-found": "error",
        "retention-days": 30,
    }
    assert container["steps"].index(report) < container["steps"].index(
        vulnerability_upload
    )
    assert container["steps"].index(vulnerability_upload) < container["steps"].index(
        gate
    )

    publish = release["jobs"]["release"]
    assert set(publish["needs"]) == {
        "verify",
        "attest",
        "codeql",
        "container",
        "verify-windows-installer",
        "verify-macos-installer",
    }
    download = next(
        step
        for step in publish["steps"]
        if step.get("name") == "Download attested release assets"
    )
    assert download["with"]["name"] == "release-assets-attested"


def test_release_job_dependency_graph_is_acyclic_and_preserves_trust_order() -> None:
    release = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    jobs = release["jobs"]

    dependencies: dict[str, set[str]] = {}
    for job_name, job in jobs.items():
        raw_needs = job.get("needs", [])
        if isinstance(raw_needs, str):
            raw_needs = [raw_needs]
        dependencies[job_name] = set(raw_needs)
        assert dependencies[job_name] <= set(jobs), (
            f"{job_name} references unknown dependencies: "
            f"{dependencies[job_name] - set(jobs)}"
        )

    assert dependencies["build-installers"] == {"verify"}
    assert dependencies["verify-windows-installer"] == {"build-installers"}
    assert dependencies["verify-macos-installer"] == {"build-installers"}
    assert dependencies["attest"] == {
        "verify",
        "verify-windows-installer",
        "verify-macos-installer",
    }
    assert "attest" in dependencies["release"]

    visited: set[str] = set()
    active: set[str] = set()

    def visit(job_name: str) -> None:
        assert job_name not in active, f"release dependency cycle includes {job_name}"
        if job_name in visited:
            return
        active.add(job_name)
        for dependency in dependencies[job_name]:
            visit(dependency)
        active.remove(job_name)
        visited.add(job_name)

    for job_name in jobs:
        visit(job_name)

    assert visited == set(jobs)


def test_release_publishes_only_the_attested_immutable_asset_directory() -> None:
    release = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    publish = release["jobs"]["release"]
    steps = publish["steps"]

    assert [step.get("name") for step in steps] == [
        "Download attested release assets",
        "Verify release asset checksums",
        "Verify complete release asset checksums",
        "Create GitHub release",
    ]
    download = steps[0]
    assert download["with"] == {
        "name": "release-assets-attested",
        "path": "release-assets",
    }
    assert steps[1]["working-directory"] == "release-assets"
    assert steps[1]["run"] == "sha256sum -c SHA256SUMS"
    assert steps[2]["working-directory"] == "release-assets"
    assert "sha256sum -c SHA256SUMS.complete" in steps[2]["run"]
    assert (
        steps[3]["run"] == 'gh release create "$GITHUB_REF_NAME" release-assets/* '
        '--verify-tag --generate-notes --title "Stock Desk $GITHUB_REF_NAME"'
    )


def test_compose_services_use_read_only_least_privilege_runtime() -> None:
    compose = _load_yaml("compose.yaml")
    shared = compose["x-stock-desk-service"]
    assert shared["read_only"] is True
    assert shared["cap_drop"] == ["ALL"]
    assert shared["cap_add"] == ["CHOWN", "SETGID", "SETUID"]
    assert shared["security_opt"] == ["no-new-privileges:true"]
    assert shared["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=64m"]
    mounts = {mount["target"]: mount for mount in shared["volumes"]}
    assert set(mounts) == {"/app/data", "/app/tdx"}
    assert "/app/logs" not in mounts
    assert mounts["/app/data"] == {
        "type": "bind",
        "source": "./data",
        "target": "/app/data",
        "read_only": False,
    }
    assert mounts["/app/tdx"]["read_only"] is True
    assert compose["services"]["api"]["healthcheck"]["test"][0] == "CMD"
    worker_health = compose["services"]["worker"]["healthcheck"]["test"]
    assert worker_health[:3] == ["CMD", "python", "-c"]
    assert "stock_desk.tasks.worker" in worker_health[3]

    dockerfile = _read("Dockerfile")
    runtime = dockerfile.split(" AS runtime", maxsplit=1)[1]
    assert "COPY --from=uv-bin" not in runtime
    assert "COPY --from=python-builder /app/.venv" in runtime
    assert "USER 10001:10001" in runtime
    assert "dpkg --purge --force-remove-essential perl-base" in runtime


def test_all_workflow_actions_use_verified_immutable_release_pins() -> None:
    uses_pattern = re.compile(
        r"^\s*-?\s*uses:\s*([^\s@]+)@([0-9a-f]{40})\s+#\s+(\S+)\s*$",
        re.MULTILINE,
    )
    seen_actions: set[str] = set()

    for workflow_path in _workflow_paths():
        content = workflow_path.read_text("utf-8")
        uses_lines = re.findall(r"^\s*-?\s*uses:.*$", content, re.MULTILINE)
        matches = uses_pattern.findall(content)
        assert len(matches) == len(uses_lines), workflow_path
        for action, sha, release_tag in matches:
            seen_actions.add(action)
            assert VERIFIED_ACTION_PINS[action] == (sha, release_tag)

    assert seen_actions == set(VERIFIED_ACTION_PINS)


def test_workflows_have_least_permissions_timeouts_and_bounded_concurrency() -> None:
    for workflow_path in _workflow_paths():
        workflow = yaml.safe_load(workflow_path.read_text("utf-8"))
        assert workflow["permissions"] == {"contents": "read"}, workflow_path
        assert "concurrency" in workflow, workflow_path
        jobs = workflow["jobs"]
        assert jobs
        for job in jobs.values():
            assert 1 <= job["timeout-minutes"] <= 45, workflow_path

    codeql = _read(".github/workflows/codeql.yml")
    assert "security-events: write" in codeql
    assert "javascript-typescript" in codeql
    assert "python" in codeql


def test_python_ci_timeout_covers_the_measured_suite_and_followup_gates() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))

    assert 30 <= workflow["jobs"]["python"]["timeout-minutes"] <= 35


def test_codeql_excludes_only_nonproduction_adversarial_tests() -> None:
    config = _load_yaml(".github/codeql/codeql-config.yml")
    assert config == {
        "name": "Stock Desk CodeQL configuration",
        "paths-ignore": ["tests/**"],
    }

    workflow = _load_github_actions_yaml(_read(".github/workflows/codeql.yml"))
    initialize_steps = [
        step
        for step in workflow["jobs"]["analyze"]["steps"]
        if step.get("name") == "Initialize CodeQL"
    ]
    assert len(initialize_steps) == 1
    assert initialize_steps[0]["with"]["config-file"] == (
        "./.github/codeql/codeql-config.yml"
    )


def test_ci_runs_native_and_container_gates_with_cleanup_and_artifacts() -> None:
    ci = _read(".github/workflows/ci.yml")
    for required in (
        "make public-tree",
        "ruff format --check",
        "ruff check",
        "mypy --strict",
        "bandit",
        "pytest",
        "uv build",
        "pnpm install --frozen-lockfile",
        "pnpm format:check",
        "pnpm lint",
        "pnpm typecheck",
        "pnpm test",
        "pnpm build",
        "docker compose build",
        "docker compose up",
        "make smoke",
        "trap cleanup EXIT",
        "docker compose down --volumes --remove-orphans",
        "actions/upload-artifact",
    ):
        assert required in ci


def test_coverage_tooling_and_numeric_thresholds_are_locked() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    dev_dependencies = pyproject["dependency-groups"]["dev"]
    assert any(dependency.startswith("pytest-cov") for dependency in dev_dependencies)
    assert pyproject["tool"]["coverage"]["run"] == {
        "branch": True,
        "source": ["src/stock_desk", "scripts", "migrations"],
        "omit": ["scripts/e2e_dev.py"],
    }
    python_threshold = pyproject["tool"]["coverage"]["report"]["fail_under"]
    assert isinstance(python_threshold, int)
    assert python_threshold >= 85
    assert pyproject["tool"]["coverage"]["xml"]["output"] == "coverage.xml"

    web_package = json.loads(_read("web/package.json"))
    assert web_package["devDependencies"]["@vitest/coverage-v8"].endswith("<5")
    assert web_package["scripts"]["test:coverage"] == "vitest run --coverage"
    root_package = json.loads(_read("package.json"))
    assert root_package["scripts"]["test"] == "pnpm --dir web test:coverage"

    vite_config = _read("web/vite.config.ts")
    assert "provider: 'v8'" in vite_config
    assert "reporter: ['text', 'lcov']" in vite_config
    assert "reportsDirectory: 'coverage'" in vite_config
    assert "include: ['src/**/*.{ts,tsx}']" in vite_config
    assert "exclude: ['src/**/*.d.ts', 'src/test/setup.ts']" in vite_config
    coverage_config = vite_config.split("coverage:", maxsplit=1)[1]
    for metric, minimum in {
        "lines": 80,
        "statements": 80,
        "functions": 80,
        "branches": 75,
    }.items():
        match = re.search(rf"\b{metric}:\s*(\d+)", coverage_config)
        assert match is not None
        assert int(match.group(1)) >= minimum


def test_public_runtime_version_surfaces_match() -> None:
    python_version = tomllib.loads(_read("pyproject.toml"))["project"]["version"]
    web_version = json.loads(_read("web/package.json"))["version"]
    api_source = _read("src/stock_desk/main.py")
    api_version = re.search(r'\bversion="([0-9]+\.[0-9]+\.[0-9]+)"', api_source)
    assert api_version is not None
    assert python_version == web_version == api_version.group(1) == "0.5.0"


def test_make_test_enforces_coverage_and_writes_reports() -> None:
    makefile = _read("Makefile")
    test_recipe = makefile.split("\ntest:\n", maxsplit=1)[1].split("\n\n", maxsplit=1)[
        0
    ]
    for required in (
        "--cov=src/stock_desk",
        "--cov=scripts",
        "--cov=migrations",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=xml:coverage.xml",
        "--cov-fail-under=85",
        "--ignore=tests/acceptance/test_formula_consistency.py",
        "--ignore=tests/acceptance/test_backtest_semantics.py",
        "--ignore=tests/performance/test_single_backtest.py",
        "--ignore=tests/performance/test_v1_budgets.py",
        "pnpm test",
    ):
        assert required in test_recipe


def test_ci_uploads_coverage_reports_and_release_uses_canonical_test() -> None:
    ci = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    python_steps = ci["jobs"]["python"]["steps"]
    python_test = next(
        step for step in python_steps if step.get("name") == "Test Python"
    )
    for required in (
        "--ignore=tests/acceptance/test_market_flow.py",
        "--ignore=tests/acceptance/test_formula_consistency.py",
        "--ignore=tests/acceptance/test_macd_formula_flow.py",
        "--ignore=tests/acceptance/test_backtest_semantics.py",
        "--ignore=tests/performance/test_formula_preview.py",
        "--ignore=tests/performance/test_chart_query.py",
        "--ignore=tests/performance/test_single_backtest.py",
        "--cov=src/stock_desk",
        "--cov=scripts",
        "--cov=migrations",
        "--cov-branch",
        "--cov-report=xml:coverage.xml",
        "--cov-fail-under=85",
    ):
        assert required in python_test["run"]
    assert (
        next(
            step
            for step in python_steps
            if step.get("name") == "Test Stage 1 market acceptance"
        )["run"]
        == "make acceptance"
    )
    assert (
        next(
            step
            for step in python_steps
            if step.get("name") == "Verify legacy cached chart correctness"
        )["run"]
        == "make benchmark"
    )
    assert (
        next(
            step
            for step in python_steps
            if step.get("name") == "Test Stage 2 formula acceptance"
        )["run"]
        == "make acceptance-formula"
    )
    assert (
        next(
            step
            for step in python_steps
            if step.get("name") == "Verify legacy formula preview correctness"
        )["run"]
        == "make benchmark-formula"
    )
    assert (
        next(
            step
            for step in python_steps
            if step.get("name") == "Test Stage 3 backtest acceptance"
        )["run"]
        == "make acceptance-backtest"
    )
    assert (
        next(
            step
            for step in python_steps
            if step.get("name") == "Verify legacy single-backtest correctness"
        )["run"]
        == "make benchmark-backtest"
    )

    web_steps = ci["jobs"]["web"]["steps"]
    web_test = next(step for step in web_steps if step.get("name") == "Test web")
    assert web_test["run"] == "pnpm test"

    artifact_paths = [
        str(step.get("with", {}).get("path", ""))
        for job in ci["jobs"].values()
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert any("coverage.xml" in path for path in artifact_paths)
    assert any("web/coverage/lcov.info" in path for path in artifact_paths)

    release = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    release_steps = release["jobs"]["verify"]["steps"]
    release_gates = next(
        step for step in release_steps if step.get("name") == "Run release gates"
    )
    command = str(release_gates["run"])
    assert "scripts/verify_release.py" in command
    assert "--candidate" in command
    assert "--target-performance" in command


def test_security_target_audits_only_locked_production_dependencies() -> None:
    makefile = _read("Makefile")
    security_recipe = makefile.split("\nsecurity:\n", maxsplit=1)[1].split(
        "\n\n", maxsplit=1
    )[0]
    assert security_recipe.splitlines() == [
        "\tuv run --frozen pytest -W error tests/security -q",
        "\tuv run --frozen bandit -q -ll -r src scripts",
        "\tuv audit --locked --no-dev",
        "\tpnpm install --lockfile-only --frozen-lockfile --ignore-scripts",
        "\tpnpm audit --prod --audit-level high",
    ]
    audit_commands = [
        command for command in security_recipe.splitlines() if " audit " in command
    ]
    assert all("--ignore" not in command for command in audit_commands)

    release_check = re.search(r"^release-check:\s*(.+)$", makefile, re.MULTILINE)
    assert release_check is not None
    assert "security" in release_check.group(1).split()


def test_ci_and_release_run_the_canonical_dependency_audit_gate() -> None:
    ci = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    audit_job = ci["jobs"]["dependency-audit"]
    assert audit_job["name"] == "Locked production dependency audit"
    assert 1 <= audit_job["timeout-minutes"] <= 10
    audit_steps = audit_job["steps"]
    setup_actions = {
        str(step.get("uses", "")).split("@", maxsplit=1)[0] for step in audit_steps
    }
    assert {
        "actions/checkout",
        "astral-sh/setup-uv",
        "pnpm/action-setup",
        "actions/setup-node",
    } <= setup_actions
    audit_step = next(
        step
        for step in audit_steps
        if step.get("name") == "Audit locked production dependencies"
    )
    assert audit_step["run"] == "make security"

    release = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    release_steps = release["jobs"]["verify"]["steps"]
    release_gates = next(
        step for step in release_steps if step.get("name") == "Run release gates"
    )
    command = str(release_gates["run"])
    assert command.count("scripts/verify_release.py") == 1
    assert '"security"' in _read("scripts/verify_release.py")


def test_contributing_guide_documents_networked_dependency_audits() -> None:
    contributing = _read("CONTRIBUTING.md")
    assert "make security" in contributing
    assert "OSV" in contributing
    assert "npm registry" in contributing
    assert "network access" in contributing.casefold()
    assert "manifests match their lockfiles" in contributing


def test_ci_and_release_gate_the_chromium_end_to_end_slice() -> None:
    ci_workflow = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    e2e = ci_workflow["jobs"]["e2e"]
    assert e2e["permissions"] == {"contents": "read"}
    assert 30 <= e2e["timeout-minutes"] <= 35
    ci_e2e = "\n".join(
        str(step.get("run", "")) for step in e2e["steps"] if isinstance(step, dict)
    )
    for required in (
        "uv sync --frozen --all-groups --extra providers",
        "pnpm install --frozen-lockfile",
        "pnpm exec playwright install --with-deps chromium",
        "make e2e-foundation",
        "make e2e-market",
        "make e2e-formula",
        "make e2e-backtest",
        "make e2e-analysis",
        "make e2e-task-center",
    ):
        assert required in ci_e2e

    release = _read(".github/workflows/release.yml")
    assert "uv sync --frozen --all-groups --extra providers" in release
    assert "git fetch --no-tags origin main:refs/remotes/origin/main" in release
    assert (
        'git merge-base --is-ancestor "$GITHUB_SHA" refs/remotes/origin/main' in release
    )
    assert "pnpm exec playwright install --with-deps chromium" in release
    assert "scripts/verify_release.py" in release
    assert "--candidate --target-performance" in release
    assert "contents: write" in release
    assert 'tags:\n      - "v*"' in release


def test_release_builds_final_artifacts_only_after_all_source_gates() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    steps = workflow["jobs"]["verify"]["steps"]
    step_names = [step.get("name") for step in steps]

    gate_index = step_names.index("Run release gates")
    build_index = step_names.index("Build final release assets")
    verify_index = step_names.index("Verify built release artifacts")
    assert gate_index < build_index < verify_index

    gate_commands = str(steps[gate_index]["run"])
    assert "scripts/verify_release.py" in gate_commands
    assert "--candidate" in gate_commands
    assert "make build" not in gate_commands
    assert steps[build_index]["run"] == "make build"


def test_release_workflow_uses_candidate_gate_and_always_uploads_its_report() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    steps = workflow["jobs"]["verify"]["steps"]
    gate = next(step for step in steps if step.get("name") == "Run release gates")
    command = str(gate["run"])

    assert 'release_version="${GITHUB_REF_NAME#v}"' in command
    assert (
        'uv run --frozen python scripts/verify_release.py "$release_version" '
        "--candidate --target-performance "
        "--report test-results/release/candidate.json"
    ) in command.replace("\n", " ")

    evidence = next(
        step
        for step in steps
        if step.get("name") == "Upload release performance evidence"
    )
    assert evidence["if"] == "always()"
    assert "test-results/release/candidate.json" in evidence["with"]["path"]

    installer_steps = workflow["jobs"]["build-installers"]["steps"]
    native_builds = [
        step
        for step in installer_steps
        if step.get("name") in {"Build Windows installer", "Build macOS installer"}
    ]
    assert len(native_builds) == 2
    assert all(
        step["env"]["STOCK_DESK_SOURCE_REVISION"] == "${{ github.sha }}"
        for step in native_builds
    )
    build_script = _read("scripts/build_installer.py")
    assert '"source_revision"' in build_script
    assert '"source_fingerprint"' in build_script


def test_e2e_is_a_root_script_without_changing_the_make_contract() -> None:
    package = json.loads(_read("package.json"))
    assert package["scripts"]["e2e"] == "playwright test"
    assert package["devDependencies"]["@playwright/test"].endswith("<2")
    gitignore = _read(".gitignore").splitlines()
    assert "test-results/" in gitignore
    assert "playwright-report/" in gitignore
    playwright = _read("playwright.config.ts")
    assert "gracefulShutdown" in playwright
    assert 'signal: "SIGTERM"' in playwright
    assert "scripts/e2e_dev.py" in playwright
    assert 'process.env.STOCK_DESK_PERFORMANCE_MODE === "1"' in playwright
    assert "performanceMode ? 300_000 : 120_000" in playwright
    foundation_e2e = _read("web/e2e/foundation.spec.ts")
    health_probe = "request.get('/api/health')"
    task_creation = "request.post('/api/tasks'"
    assert health_probe in foundation_e2e
    assert foundation_e2e.index(health_probe) < foundation_e2e.index(task_creation)
    status_hook = _read("web/src/shared/api/useSystemStatus.ts")
    assert "refetchInterval: 5_000" in status_hook

    makefile = _read("Makefile")
    targets = {
        match.group(1)
        for line in makefile.splitlines()
        if (match := re.match(r"^([a-z][a-z0-9-]*):", line))
    }
    assert targets == {
        "acceptance",
        "acceptance-formula",
        "acceptance-backtest",
        "acceptance-analysis",
        "acceptance-domain-contracts",
        "acceptance-full-journey",
        "benchmark",
        "benchmark-formula",
        "benchmark-backtest",
        "performance",
        "performance-reference",
        "performance-target",
        "performance-regressions",
        "bootstrap",
        "check-public-tree",
        "dev",
        "e2e",
        "e2e-foundation",
        "e2e-market",
        "e2e-formula",
        "e2e-backtest",
        "e2e-analysis",
        "e2e-task-center",
        "e2e-accessibility",
        "test",
        "lint",
        "typecheck",
        "build",
        "smoke",
        "container-smoke",
        "public-tree",
        "security",
        "release-check",
    }
    assert "scripts/clean_build_artifacts.py" in makefile
    assert makefile.index("scripts/clean_build_artifacts.py") < makefile.index(
        "uv build --no-build-isolation"
    )


def test_stage_two_formula_gates_extend_every_release_surface() -> None:
    makefile = _read("Makefile")
    assert re.search(
        r"^acceptance-formula:\n\tuv run --frozen pytest -W error "
        r"tests/acceptance/test_formula_consistency\.py "
        r"tests/acceptance/test_macd_formula_flow\.py "
        r"tests/acceptance/test_formula_editing_assistance\.py$",
        makefile,
        re.MULTILINE,
    )
    assert re.search(
        r"^benchmark-formula:\n\tuv run --frozen pytest -W error "
        r"tests/performance/test_formula_preview\.py -q$",
        makefile,
        re.MULTILINE,
    )
    assert re.search(
        r"^e2e-formula:\n\tpnpm exec playwright test "
        r"web/e2e/formula-studio\.spec\.ts --project=chromium$",
        makefile,
        re.MULTILINE,
    )
    release_check = re.search(r"^release-check:\s*(.+)$", makefile, re.MULTILINE)
    assert release_check is not None
    dependencies = release_check.group(1).split()
    for target in ("acceptance-formula", "performance-regressions", "e2e-formula"):
        assert target in dependencies

    ci = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    ci_commands = "\n".join(
        str(step.get("run", ""))
        for job in ci["jobs"].values()
        for step in job["steps"]
        if isinstance(step, dict)
    )
    for command in (
        "make acceptance-formula",
        "make benchmark-formula",
        "make e2e-formula",
    ):
        assert command in ci_commands

    release = _read(".github/workflows/release.yml")
    candidate = _read("scripts/verify_release.py")
    for command in (
        "make acceptance-formula",
        "make performance-regressions",
        "make e2e-formula",
    ):
        target = command.removeprefix("make ")
        assert f'"{target}"' in candidate
    assert "scripts/verify_release.py" in release


def test_stage_three_backtest_gates_extend_every_release_surface() -> None:
    makefile = _read("Makefile")
    assert re.search(
        r"^acceptance-backtest:\n\tuv run --frozen pytest -W error "
        r"tests/acceptance/test_backtest_semantics\.py$",
        makefile,
        re.MULTILINE,
    )
    assert re.search(
        r"^benchmark-backtest:\n\tuv run --frozen pytest -W error "
        r"tests/performance/test_single_backtest\.py -q$",
        makefile,
        re.MULTILINE,
    )
    assert re.search(
        r"^e2e-backtest:\n\tpnpm exec playwright test "
        r"web/e2e/backtest\.spec\.ts --project=chromium$",
        makefile,
        re.MULTILINE,
    )
    release_check = re.search(r"^release-check:\s*(.+)$", makefile, re.MULTILINE)
    assert release_check is not None
    dependencies = release_check.group(1).split()
    for target in ("acceptance-backtest", "performance-regressions", "e2e-backtest"):
        assert target in dependencies

    ci = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    ci_commands = "\n".join(
        str(step.get("run", ""))
        for job in ci["jobs"].values()
        for step in job["steps"]
        if isinstance(step, dict)
    )
    release = _read(".github/workflows/release.yml")
    candidate = _read("scripts/verify_release.py")
    for command in (
        "make acceptance-backtest",
        "make benchmark-backtest",
        "make e2e-backtest",
    ):
        assert command in ci_commands
    for command in (
        "make acceptance-backtest",
        "make performance-regressions",
        "make e2e-backtest",
    ):
        target = command.removeprefix("make ")
        assert f'"{target}"' in candidate
    assert "scripts/verify_release.py" in release


def test_stage_four_analysis_gates_extend_every_release_surface() -> None:
    makefile = _read("Makefile")
    assert re.search(
        r"^acceptance-analysis:\n\tuv run --frozen pytest -W error "
        r"tests/acceptance/test_analysis_flow\.py "
        r"tests/security/test_analysis_boundaries\.py$",
        makefile,
        re.MULTILINE,
    )
    assert re.search(
        r"^e2e-analysis:\n\tpnpm exec playwright test "
        r"web/e2e/analysis\.spec\.ts "
        r"web/e2e/model-provider-matrix\.spec\.ts --project=chromium$",
        makefile,
        re.MULTILINE,
    )
    release_check = re.search(r"^release-check:\s*(.+)$", makefile, re.MULTILINE)
    assert release_check is not None
    dependencies = release_check.group(1).split()
    for target in ("acceptance-analysis", "e2e-analysis"):
        assert target in dependencies

    ci = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    ci_commands = "\n".join(
        str(step.get("run", ""))
        for job in ci["jobs"].values()
        for step in job["steps"]
        if isinstance(step, dict)
    )
    release = _read(".github/workflows/release.yml")
    candidate = _read("scripts/verify_release.py")
    for command in ("make acceptance-analysis", "make e2e-analysis"):
        assert command in ci_commands
        target = command.removeprefix("make ")
        assert f'"{target}"' in candidate
    assert "scripts/verify_release.py" in release


def test_task_center_e2e_gate_extends_every_release_surface() -> None:
    makefile = _read("Makefile")
    assert re.search(
        r"^e2e-task-center:\n\tpnpm exec playwright test "
        r"web/e2e/task-center\.spec\.ts --project=chromium$",
        makefile,
        re.MULTILINE,
    )
    e2e = re.search(r"^e2e:\s*(.+)$", makefile, re.MULTILINE)
    assert e2e is not None
    assert "e2e-task-center" in e2e.group(1).split()
    release_check = re.search(r"^release-check:\s*(.+)$", makefile, re.MULTILINE)
    assert release_check is not None
    assert "e2e-task-center" in release_check.group(1).split()

    ci = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    ci_commands = [
        str(step.get("run", ""))
        for step in ci["jobs"]["e2e"]["steps"]
        if isinstance(step, dict)
    ]
    assert ci_commands.count("make e2e-task-center") == 1

    release = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    release_steps = release["jobs"]["verify"]["steps"]
    release_gates = next(
        step for step in release_steps if step.get("name") == "Run release gates"
    )
    assert "scripts/verify_release.py" in str(release_gates["run"])
    assert '"e2e-task-center"' in _read("scripts/verify_release.py")


def test_dependabot_covers_all_package_ecosystems_weekly() -> None:
    dependabot = _load_yaml(".github/dependabot.yml")
    updates = dependabot["updates"]
    assert {update["package-ecosystem"] for update in updates} == {
        "pip",
        "npm",
        "docker",
        "github-actions",
    }
    for update in updates:
        assert update["schedule"]["interval"] == "weekly"
        assert 1 <= update["open-pull-requests-limit"] <= 10
        assert update["groups"]


def test_project_metadata_is_complete_and_points_to_the_public_repository() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    web_package = json.loads(_read("web/package.json"))
    assert project["version"] == web_package["version"]
    assert project["description"]
    assert project["readme"] == "README.md"
    assert project["license"] == "Apache-2.0"
    assert project["requires-python"] == ">=3.12,<3.13"
    assert project["authors"] == [{"name": "CongBao", "email": "bao_cong@outlook.com"}]
    assert {"a-share", "fastapi", "react", "stock-analysis"} <= set(project["keywords"])
    assert (
        "License :: OSI Approved :: Apache Software License" in project["classifiers"]
    )
    repository_url = "https://github.com/CongBao/stock-desk"
    assert project["urls"] == {
        "Homepage": repository_url,
        "Documentation": f"{repository_url}/blob/main/docs/architecture.md",
        "Repository": repository_url,
        "Issues": f"{repository_url}/issues",
        "Security": f"{repository_url}/security/advisories/new",
    }


def test_sdist_uses_an_explicit_source_only_allowlist() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))

    assert pyproject["tool"]["hatch"]["build"]["targets"]["sdist"] == {
        "include": [
            "/alembic.ini",
            "/migrations",
            "/src/stock_desk",
        ]
    }


def test_readmes_are_concise_product_entries_with_detailed_guide_links() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    for content in (english, chinese):
        assert len(content.splitlines()) <= 120
        for shared_fact in (
            "https://github.com/CongBao/stock-desk/wiki",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "SUPPORT.md",
            "LICENSE",
            "docker compose up --build --wait",
            "STOCK_DESK_MASTER_KEY",
        ):
            assert shared_fact in content
        assert (
            "not investment advice" in content.casefold() or "不构成投资建议" in content
        )
        assert "cache" in content.casefold() or "缓存" in content

    assert english.splitlines()[0] == "[简体中文](README.zh-CN.md)"
    assert chinese.splitlines()[0] == "[English](README.md)"

    contributing = _read("CONTRIBUTING.md")
    for detailed_fact in (
        ">=3.12,<3.13",
        "pnpm 11",
        "make bootstrap",
        "make dev",
        "make release-check",
    ):
        assert detailed_fact in contributing


def test_security_and_support_use_the_right_reporting_channels() -> None:
    security = _read("SECURITY.md")
    private_report_url = "https://github.com/CongBao/stock-desk/security/advisories/new"
    assert private_report_url in security
    assert "do not open a public issue" in security.casefold()
    assert "SLA" not in security

    support = _read("SUPPORT.md")
    assert "https://github.com/CongBao/stock-desk/issues/new/choose" in support
    assert private_report_url in support


def test_changelog_roadmap_and_architecture_match_current_release_scope() -> None:
    changelog = _read("CHANGELOG.md")
    unreleased = re.search(
        r"## \[Unreleased\](?P<body>.*?)## \[1\.0\.0\] - 2026-07-08",
        changelog,
        re.DOTALL,
    )
    assert unreleased is not None
    unreleased_body = unreleased.group("body")
    assert "Nothing yet" in unreleased_body
    final_release_section = re.search(
        r"## \[1\.0\.0\] - 2026-07-08(?P<body>.*?)## \[0\.5\.0\]",
        changelog,
        re.DOTALL,
    )
    assert final_release_section is not None
    final_release_body = final_release_section.group("body")
    for fact in (
        "stage 5",
        "source-free windows",
        "2/3/5-second",
        "responsive",
        "github wiki",
    ):
        assert fact in final_release_body.casefold()
    release_section = re.search(
        r"## \[0\.5\.0\] - 2026-07-08(?P<body>.*?)## \[0\.4\.0\]",
        changelog,
        re.DOTALL,
    )
    assert release_section is not None
    release_body = release_section.group("body")
    for fact in (
        "Stage 4",
        "nine-stage",
        "insufficient-evidence",
        "Model API keys",
    ):
        assert fact in release_body
    previous_release_section = re.search(
        r"## \[0\.4\.0\] - 2026-07-07(?P<body>.*?)## \[0\.3\.0\]",
        changelog,
        re.DOTALL,
    )
    assert previous_release_section is not None
    assert "### Added" in previous_release_section.group("body")
    assert "Stage 3" in previous_release_section.group("body")

    roadmap = _read("ROADMAP.md")
    for stage in range(6):
        assert f"| {stage} —" in roadmap
    assert len(re.findall(r"\|\s+Complete\s+\|", roadmap)) == 6
    assert len(re.findall(r"\|\s+In verification\s+\|", roadmap)) == 0
    assert len(re.findall(r"\|\s+Current\s+\|", roadmap)) == 0
    assert len(re.findall(r"\|\s+Planned\s+\|", roadmap)) == 0

    release_notes = _read("docs/releases/v1.0.0.md")
    for fact in (
        "Windows x86_64",
        "macOS x86_64",
        "macOS arm64",
        "checksums",
        "SBOM",
        "rollback",
        "not investment advice",
    ):
        assert fact.casefold() in release_notes.casefold()

    architecture = _read("docs/architecture.md")
    for boundary in ("FastAPI", "worker", "SQLite", "security", "trust boundary"):
        assert boundary.casefold() in architecture.casefold()
    assert "formula engine" in architecture.casefold()
    assert "formula studio" in architecture.casefold()
    assert "backtest" in architecture.casefold()
    for boundary in ("analysis", "model", "evidence", "prompt injection"):
        assert boundary in architecture.casefold()


def test_release_workflow_is_tag_only_and_scopes_write_permission() -> None:
    release = _read(".github/workflows/release.yml")
    assert re.search(r"^\s+tags:\s*$", release, re.MULTILINE)
    assert re.search(r'^\s+- ["\']v\*["\']\s*$', release, re.MULTILINE)
    assert "pull_request:" not in release
    assert "branches:" not in release
    assert "schedule:" not in release
    assert release.count("contents: write") == 1
    assert re.search(
        r"release:\n(?:.|\n)*?permissions:\n\s+contents: write",
        release,
    )
    assert "gh release create" in release
    assert "GH_REPO: ${{ github.repository }}" in release
    assert "sha256sum -- *.whl *.tar.gz *.json > SHA256SUMS" in release
    assert "${GITHUB_REF_NAME#v}" in release
    assert (
        "from scripts.verify_release import check_changelog, check_versions" in release
    )
    assert 'os.environ["RELEASE_VERSION"]' in release
    assert "publish" not in release.casefold()


def test_tag_release_verifies_built_artifacts_before_packaging_and_upload() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    verify_steps = workflow["jobs"]["verify"]["steps"]
    build_step = next(
        step
        for step in verify_steps
        if step.get("name") == "Build final release assets"
    )
    artifact_step = next(
        step
        for step in verify_steps
        if step.get("name") == "Verify built release artifacts"
    )
    checksum_step = next(
        step
        for step in verify_steps
        if step.get("name") == "Prepare checksummed assets"
    )
    upload_step = next(
        step
        for step in verify_steps
        if step.get("name") == "Upload verified release assets for attestation"
    )

    assert build_step["run"] == "make build"
    assert (
        verify_steps.index(build_step)
        < verify_steps.index(artifact_step)
        < verify_steps.index(checksum_step)
        < verify_steps.index(upload_step)
    )

    artifact_command = artifact_step["run"]
    for required in (
        'release_version="${GITHUB_REF_NAME#v}"',
        'RELEASE_VERSION="$release_version"',
        "uv run --frozen python -c",
        "from scripts.verify_release import check_build_artifacts",
        'version = os.environ["RELEASE_VERSION"]',
        "check_build_artifacts(Path.cwd(), version)",
    ):
        assert required in artifact_command
    assert "0.1.0" not in artifact_command


def test_release_checksum_manifest_is_flat_and_verified_before_publish() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/release.yml"))
    verify_steps = workflow["jobs"]["verify"]["steps"]
    prepare_step = next(
        step
        for step in verify_steps
        if step.get("name") == "Prepare checksummed assets"
    )
    prepare_commands = [
        line.strip() for line in prepare_step["run"].splitlines() if line.strip()
    ]
    checksum_commands = [
        command for command in prepare_commands if command.startswith("sha256sum")
    ]

    assert checksum_commands == ["sha256sum -- *.whl *.tar.gz *.json > SHA256SUMS"]
    assert all("dist/" not in command for command in checksum_commands)
    assert prepare_commands.index("cd dist") < prepare_commands.index(
        checksum_commands[0]
    )

    release_steps = workflow["jobs"]["release"]["steps"]
    checksum_step = next(
        step
        for step in release_steps
        if step.get("name") == "Verify release asset checksums"
    )
    create_step_index = next(
        index
        for index, step in enumerate(release_steps)
        if step.get("name") == "Create GitHub release"
    )

    assert checksum_step["working-directory"] == "release-assets"
    assert checksum_step["run"] == "sha256sum -c SHA256SUMS"
    assert release_steps.index(checksum_step) < create_step_index

    native_checksum_step = next(
        step
        for step in release_steps
        if step.get("name") == "Verify complete release asset checksums"
    )
    native_commands = native_checksum_step["run"]
    assert "-eq 3" in native_commands
    assert "-eq 4" in native_commands
    assert "SHA256SUMS.complete" in native_commands
    assert "wc -l < SHA256SUMS.complete" in native_commands
    for pattern in ("*.exe", "*.dmg", "*.sbom.spdx.json"):
        assert pattern in native_commands
    assert release_steps.index(native_checksum_step) < create_step_index


def test_codeowners_covers_source_web_docs_tests_and_automation() -> None:
    codeowners = _read(".github/CODEOWNERS")
    for pattern in ("/src/", "/web/", "/docs/", "/tests/", "/.github/"):
        assert f"{pattern} @CongBao" in codeowners


def test_performance_chart_timer_includes_the_bounded_interaction_handshake() -> None:
    source = _read("web/e2e/performance.spec.ts")

    assert "async function proveChartInteractionHandshake" in source
    for action in ("chartAction", "warmChartAction"):
        start = source.index(f"async function {action}")
        end = source.index("\nasync function ", start + 1)
        body = source[start:end]
        assert body.index("await proveChartInteractionHandshake") < body.index(
            "const wall ="
        )
        assert body.index("await proveChartInteractionHandshake") < body.index(
            "await sampler.finish"
        )


def test_performance_pid_identity_is_scoped_to_each_timed_sample() -> None:
    source = _read("web/e2e/performance.spec.ts")

    assert "const processIdentities = new ProcessIdentityTracker()" not in source
    sampler_start = source.index("class RssSampler")
    sampler_end = source.index("\nasync function forbidExternalNetwork", sampler_start)
    sampler = source[sampler_start:sampler_end]
    assert "const identities = new ProcessIdentityTracker(rootRoles)" in sampler
    assert "processTreeSnapshot(roots, identities)" in sampler
    assert "processTreeSnapshot(this.roots, this.identities)" in sampler


def test_pool_navigation_interactivity_uses_rendered_spa_and_long_task_evidence() -> (
    None
):
    source = _read("web/e2e/performance.spec.ts")
    start = source.index(
        "await beginLongTaskWindow(page);\n  await page.getByRole('link', { name: '任务' })"
    )
    end = source.index("\n  await beginLongTaskWindow(page);", start + 1)
    navigation = source[start:end]

    assert "navigationStarted" not in navigation
    assert "taskCenterVisible" in navigation
    assert "runPageVisible" in navigation
    assert "progressVisible" in navigation
    assert (
        "interactive: taskCenterVisible && runPageVisible && progressVisible"
        in navigation
    )
    assert "long_task_count: await endLongTaskWindow(page)" in navigation


def test_performance_target_ci_is_explicit_and_requirements_remain_mapped() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    e2e = workflow["jobs"]["e2e"]
    assert e2e["runs-on"] == "ubuntu-24.04"
    target_step = next(
        step
        for step in e2e["steps"]
        if step.get("name") == "Measure Ubuntu x64 4-core/16GB target baseline"
    )
    assert target_step["run"] == "make performance-target"

    makefile = _read("Makefile")
    target_recipe = makefile.split("\nperformance-target:\n", maxsplit=1)[1].split(
        "\n\n", maxsplit=1
    )[0]
    assert "--evidence-kind target_baseline" in target_recipe

    requirements = yaml.safe_load(_read("tests/acceptance/requirements.yml"))
    records = {item["id"]: item for item in requirements["requirements"]}
    for requirement_id in ("R-053",):
        requirement = records[requirement_id]
        assert requirement["status"] == "mapped"
        assert any(
            evidence["state"] == "planned" and evidence["runner"] == "github-actions"
            for evidence in requirement["evidence"]
        )


def test_accessibility_and_responsive_suite_is_a_release_gate() -> None:
    workflow = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    e2e_steps = workflow["jobs"]["e2e"]["steps"]
    responsive_step = next(
        step
        for step in e2e_steps
        if step.get("name") == "Test accessible responsive workspaces"
    )
    assert responsive_step["run"] == "make e2e-accessibility"

    makefile = _read("Makefile")
    recipe = makefile.split("\ne2e-accessibility:\n", maxsplit=1)[1].split(
        "\n\n", maxsplit=1
    )[0]
    assert "web/e2e/accessibility.spec.ts" in recipe
    assert "web/e2e/responsive.spec.ts" in recipe
    assert "e2e-accessibility" in makefile.split("\nrelease-check:", maxsplit=1)[1]
