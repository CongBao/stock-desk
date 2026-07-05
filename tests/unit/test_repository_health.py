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
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/release.yml",
}

VERIFIED_ACTION_PINS = {
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
    }
    loaded_triggers: dict[str, dict[str, Any]] = {}

    for workflow_path in _workflow_paths():
        triggers = _workflow_triggers(
            _load_github_actions_yaml(workflow_path.read_text("utf-8"))
        )
        loaded_triggers[workflow_path.name] = triggers
        assert set(triggers) == expected_triggers[workflow_path.name]

    assert loaded_triggers["release.yml"] == {"push": {"tags": ["v*"]}}


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


def test_ci_and_release_gate_the_chromium_end_to_end_slice() -> None:
    ci_workflow = _load_github_actions_yaml(_read(".github/workflows/ci.yml"))
    e2e = ci_workflow["jobs"]["e2e"]
    assert e2e["permissions"] == {"contents": "read"}
    assert 1 <= e2e["timeout-minutes"] <= 20
    ci_e2e = "\n".join(
        str(step.get("run", "")) for step in e2e["steps"] if isinstance(step, dict)
    )
    for required in (
        "uv sync --frozen --all-groups",
        "pnpm install --frozen-lockfile",
        "pnpm exec playwright install --with-deps chromium",
        "pnpm e2e",
    ):
        assert required in ci_e2e

    release = _read(".github/workflows/release.yml")
    assert "pnpm exec playwright install --with-deps chromium" in release
    assert "pnpm e2e" in release
    assert "contents: write" in release
    assert 'tags:\n      - "v*"' in release


def test_e2e_is_a_root_script_without_changing_the_make_contract() -> None:
    package = json.loads(_read("package.json"))
    assert package["scripts"]["e2e"] == "playwright test"
    assert package["devDependencies"]["@playwright/test"].endswith("<2")
    gitignore = _read(".gitignore").splitlines()
    assert "test-results/" in gitignore
    assert "playwright-report/" in gitignore
    playwright = _read("playwright.config.ts")
    assert "gracefulShutdown" in playwright
    assert "signal: 'SIGTERM'" in playwright
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
        if (match := re.match(r"^([a-z][a-z-]*):", line))
    }
    assert targets == {
        "bootstrap",
        "dev",
        "test",
        "lint",
        "typecheck",
        "build",
        "smoke",
        "public-tree",
        "release-check",
    }
    assert "scripts/clean_build_artifacts.py" in makefile
    assert makefile.index("scripts/clean_build_artifacts.py") < makefile.index(
        "uv build --no-build-isolation"
    )


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


def test_readmes_match_commands_and_describe_stage_zero_limits() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    shared_facts = (
        ">=3.12,<3.13",
        "pnpm 11",
        "make bootstrap",
        "make dev",
        "docker compose up --build --wait",
        "make release-check",
        "http://localhost:5173",
        "http://localhost:8000/api/health",
        "http://localhost:8000/docs",
        "/market",
        "/formulas",
        "/backtests",
        "/analysis",
        "/tasks",
        "/settings",
        "demo.double",
        "STOCK_DESK_MASTER_KEY",
    )
    for fact in shared_facts:
        assert fact in english
        assert fact in chinese

    for content in (english, chinese):
        assert "Stage 0" in content
        assert "Apache-2.0" in content
        assert "Docker" in content
        assert "uv" in content
        assert "Node.js" in content
        assert (
            "not investment advice" in content.casefold() or "不构成投资建议" in content
        )
        assert "running" in content.casefold() or "运行中" in content


def test_security_and_support_use_the_right_reporting_channels() -> None:
    security = _read("SECURITY.md")
    private_report_url = "https://github.com/CongBao/stock-desk/security/advisories/new"
    assert private_report_url in security
    assert "do not open a public issue" in security.casefold()
    assert "SLA" not in security

    support = _read("SUPPORT.md")
    assert "https://github.com/CongBao/stock-desk/issues/new/choose" in support
    assert private_report_url in support


def test_changelog_roadmap_and_architecture_match_foundation_scope() -> None:
    changelog = _read("CHANGELOG.md")
    unreleased = re.search(
        r"## \[Unreleased\](?P<body>.*?)## \[0\.1\.0\]",
        changelog,
        re.DOTALL,
    )
    assert unreleased is not None
    assert unreleased.group("body").strip() == ""
    release_section = re.search(
        r"## \[0\.1\.0\] - 2026-07-05(?P<body>.*)",
        changelog,
        re.DOTALL,
    )
    assert release_section is not None
    assert "### Added" in release_section.group("body")
    assert "Stage 0" in release_section.group("body")

    roadmap = _read("ROADMAP.md")
    for stage in range(6):
        assert f"| {stage} —" in roadmap
    assert roadmap.count("| Planned |") == 5

    architecture = _read("docs/architecture.md")
    for boundary in ("FastAPI", "worker", "SQLite", "security", "trust boundary"):
        assert boundary.casefold() in architecture.casefold()


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
    assert "sha256sum -- *.whl *.tar.gz > SHA256SUMS" in release
    assert "${GITHUB_REF_NAME#v}" in release
    assert (
        "from scripts.verify_release import check_changelog, check_versions" in release
    )
    assert 'os.environ["RELEASE_VERSION"]' in release
    assert "publish" not in release.casefold()


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

    assert checksum_commands == ["sha256sum -- *.whl *.tar.gz > SHA256SUMS"]
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


def test_codeowners_covers_source_web_docs_tests_and_automation() -> None:
    codeowners = _read(".github/CODEOWNERS")
    for pattern in ("/src/", "/web/", "/docs/", "/tests/", "/.github/"):
        assert f"{pattern} @CongBao" in codeowners
