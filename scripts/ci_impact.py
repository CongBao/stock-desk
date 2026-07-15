from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


FULL_PROFILE = "full"
DOCS_PROFILE = "docs-only"
BACKEND_PROFILE = "backend"
WEB_PROFILE = "web"
RELEASE_DESKTOP_PROFILE = "release-infra-desktop"

# These are semantic gates, not workflow implementation details.  Workflows may fan a
# gate out into multiple jobs, but may not silently omit a gate selected here.
ALL_GATES = (
    "change-policy",
    "public-tree",
    "docs",
    "python-unit",
    "python-integration",
    "python-acceptance-performance",
    "python-security",
    "web",
    "e2e",
    "container",
    "dependency-audit",
    "codeql",
    "requirement-evidence",
    "artifact-proof",
    "rust",
)

_PROFILE_GATES: dict[str, tuple[str, ...]] = {
    DOCS_PROFILE: ("change-policy", "public-tree", "docs"),
    BACKEND_PROFILE: (
        "change-policy",
        "public-tree",
        "python-unit",
        "python-integration",
        "python-acceptance-performance",
        "python-security",
        "requirement-evidence",
    ),
    WEB_PROFILE: (
        "change-policy",
        "public-tree",
        "web",
        "e2e",
        "requirement-evidence",
    ),
    RELEASE_DESKTOP_PROFILE: (
        "change-policy",
        "public-tree",
        "python-unit",
        "python-integration",
        "python-acceptance-performance",
        "python-security",
        "web",
        "e2e",
        "container",
        "dependency-audit",
        "requirement-evidence",
        "artifact-proof",
        "rust",
    ),
    FULL_PROFILE: ALL_GATES,
}

_DOC_FILES = frozenset(
    {
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "README.en.md",
        "README.md",
        "ROADMAP.md",
        "SECURITY.md",
        "SUPPORT.md",
        "LICENSE",
    }
)

_DEPENDENCY_FILES = frozenset(
    {
        ".python-version",
        "Dockerfile",
        "Makefile",
        "alembic.ini",
        "compose.yaml",
        "package.json",
        "playwright.config.ts",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "pyproject.toml",
        "rust-toolchain.toml",
        "src-tauri/Cargo.lock",
        "src-tauri/Cargo.toml",
        "uv.lock",
    }
)
_DELIVERY_FILES = frozenset(
    {
        ".dockerignore",
        ".editorconfig",
        ".env.example",
        ".gitattributes",
        ".gitignore",
        "config/desktop-network-privacy.json",
        "config/tauri-updater-runtime.json",
    }
)
_SIGNING_TRUST_FILES = frozenset(
    {
        "config/release-auditor-public-key.pem",
        "config/release-tag-allowed-signers",
    }
)
_HIGH_RISK_DOMAINS = frozenset({"delivery", "dependency", "permissions", "signing"})
_HIGH_RISK_SCRIPT_NAMES = frozenset(
    {
        "aggregate_ci_evidence.py",
        "artifact_manifest.py",
        "check_public_tree.py",
        "check_requirement_coverage.py",
        "ci_impact.py",
        "ci_test_inventory.py",
        "deployment_latency.py",
        "main_validation_proof.py",
        "verify_ci_cache_policy.py",
        "verify_installed_app.py",
        "verify_release.py",
    }
)
_INSTALLER_SCRIPT_NAMES = frozenset(
    {
        "build_installer.py",
        "build_windows_desktop.py",
        "compare_windows_payloads.py",
        "verify_windows_desktop_bundle.py",
    }
)
_INSTALLER_TEST_NAMES = frozenset(
    {
        "test_build_windows_desktop.py",
        "test_compare_windows_payloads.py",
        "test_sidecar_spec_contract.py",
        "test_verify_windows_desktop_bundle.py",
        "test_windows_bundle_verifier.py",
        "test_windows_desktop_packaging.py",
        "test_windows_desktop_raw_evidence_v2.py",
        "test_windows_installed_workflow.py",
    }
)
_HIGH_RISK_INSTALLER_FILES = frozenset(
    {
        ".github/workflows/windows-installed.yml",
        "config/windows-vm-broker-public-key.pem",
        "schemas/windows-installed-evidence-v2.schema.json",
        "schemas/windows-installed-raw-evidence-v2.schema.json",
        "schemas/windows-vm-lifecycle-receipt-v2.schema.json",
        "schemas/windows-vm-snapshot-policy-v2.schema.json",
        "scripts/verify_windows_desktop_raw_evidence.py",
        "scripts/verify_windows_installed_evidence.py",
        "scripts/windows_desktop_uia_driver.ps1",
        "scripts/windows_installed_environment_policy.py",
        "scripts/windows_installed_guest_harness.ps1",
        "scripts/windows_installed_vm_harness.ps1",
        "scripts/windows_vm_broker_client.py",
        "tests/windows/windows_desktop_uia_driver_integration.ps1",
    }
)
_PACKAGED_BACKTEST_PROOF_FILES = frozenset(
    {
        "schemas/packaged-backtest-evidence-v1.schema.json",
        "schemas/packaged-backtest-host-observation-v1.schema.json",
        "schemas/windows-packaged-backtest-promotion-v1.schema.json",
        "scripts/capture_packaged_backtest_semantics.py",
        "scripts/capture_windows_desktop_evidence.ps1",
        "scripts/main_validation_proof.py",
        "scripts/prepare_windows_packaged_backtest_evidence.py",
        "scripts/v1_backtest_oracle.py",
        "scripts/verify_packaged_backtest_evidence.py",
        "scripts/windows_desktop_webview_evidence.mjs",
        "scripts/windows_packaged_backtest_evidence.mjs",
        "tests/fixtures/backtest/v1_0_oracle.json",
        "tests/fixtures/backtest/v1_0_oracle_inputs.json",
    }
)
_NSIS_REPACK_PROOF_FILES = frozenset(
    {
        "config/nsis-toolchain-lock.json",
        "schemas/nsis-repack-kit-v1.schema.json",
        "schemas/nsis-repack-receipt-v1.schema.json",
        "scripts/nsis_repack_contract.py",
        "scripts/secure_artifact_snapshot.py",
        "tests/unit/test_nsis_repack_contract.py",
        "tests/unit/test_secure_artifact_snapshot.py",
        "tests/windows/nsis_repack_contract_integration.ps1",
    }
)


@dataclass(frozen=True)
class Impact:
    profile: str
    full: bool
    reason: str
    changed_files: tuple[str, ...]
    domains: tuple[str, ...] = ()
    required_jobs: tuple[str, ...] = ()
    skipped_jobs: tuple[str, ...] = ()


def _impact(
    profile: str,
    *,
    full: bool,
    reason: str,
    paths: tuple[str, ...],
    domains: Iterable[str] = (),
) -> Impact:
    required = _PROFILE_GATES[profile]
    skipped = tuple(gate for gate in ALL_GATES if gate not in required)
    return Impact(
        profile=profile,
        full=full,
        reason=reason,
        changed_files=paths,
        domains=tuple(sorted(set(domains))),
        required_jobs=required,
        skipped_jobs=skipped,
    )


def _normalise_path(raw_path: str) -> str | None:
    path = raw_path.strip()
    if not path or "\\" in path or "\x00" in path:
        return None
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        return None
    normalised = pure_path.as_posix()
    if normalised == "." or normalised.startswith("./"):
        return None
    return normalised


def _path_domain(path: str) -> str | None:
    """Return the owned CI domain, or None for a genuinely unknown path.

    Order is security-sensitive: delivery/proof inputs are recognized before the
    broad scripts/tests prefixes so they always fail closed.
    """
    if path in _PACKAGED_BACKTEST_PROOF_FILES or path in _NSIS_REPACK_PROOF_FILES:
        return "delivery"
    if path in _HIGH_RISK_INSTALLER_FILES:
        return "installer"
    if path in _DEPENDENCY_FILES:
        return "dependency"
    if path in _SIGNING_TRUST_FILES:
        return "signing"
    if path in _DELIVERY_FILES or path.startswith("schemas/"):
        return "delivery"
    if path == ".github/CODEOWNERS":
        return "permissions"
    if path.startswith(".github/"):
        return "delivery"
    if path.startswith("scripts/"):
        name = PurePosixPath(path).name
        if "sign" in name:
            return "signing"
        if name in _HIGH_RISK_SCRIPT_NAMES or any(
            token in name for token in ("security", "proof", "release")
        ):
            return "delivery"
        if name in _INSTALLER_SCRIPT_NAMES:
            return "installer"
        return "backend"
    if path in _DOC_FILES or path.startswith("docs/"):
        return "documentation"
    if path.startswith("packaging/"):
        return "installer"
    if path.startswith(("src-tauri/", "desktop/")):
        return "tauri"
    if path.startswith("web/"):
        return "web"
    if path == "src/stock_desk/market/lake.py":
        return "windows-storage"
    if path.startswith(("src/", "migrations/")):
        return "backend"
    if path.startswith("tests/"):
        name = PurePosixPath(path).name
        if "sign" in name:
            return "signing"
        if name in {
            "test_ci_impact.py",
            "test_ci_cache_policy.py",
            "test_artifact_manifest.py",
            "test_main_validation_proof.py",
            "test_requirement_coverage.py",
            "test_verify_release.py",
        }:
            return "delivery"
        if path.startswith(("tests/e2e/", "tests/web/")):
            return "web"
        if path in {
            "tests/integration/market/test_sqlite_market_lake.py",
            "tests/integration/test_windows_runtime_acl.py",
        }:
            return "windows-storage"
        if name in _INSTALLER_TEST_NAMES or any(
            token in path for token in ("installer", "installed_distribution")
        ):
            return "installer"
        return "backend"
    return None


def unclassified_tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Inventory tracked public paths and return any absent from the risk graph."""
    output = subprocess.check_output(
        ["git", "-C", os.fspath(repo_root), "ls-files", "-z"]
    )
    paths = tuple(os.fsdecode(item) for item in output.split(b"\0") if item)
    return tuple(sorted(path for path in paths if _path_domain(path) is None))


def classify_impact(
    event_name: str,
    changed_files: Sequence[str],
    *,
    fork_pull_request: bool = False,
    base_sha: str | None = None,
    expected_base_sha: str | None = None,
    base_reachable: bool = True,
) -> Impact:
    normalised_paths: list[str] = []
    for raw_path in changed_files:
        path = _normalise_path(raw_path)
        if path is None:
            return _impact(
                FULL_PROFILE,
                full=True,
                reason="invalid-or-empty-path",
                paths=(),
                domains=("high-risk",),
            )
        normalised_paths.append(path)

    paths = tuple(sorted(set(normalised_paths)))
    if event_name == "push":
        return _impact(
            FULL_PROFILE,
            full=True,
            reason="push-events-require-full",
            paths=paths,
            domains=("high-risk",),
        )
    if event_name != "pull_request":
        return _impact(
            FULL_PROFILE,
            full=True,
            reason="unsupported-event",
            paths=paths,
            domains=("high-risk",),
        )
    if fork_pull_request:
        return _impact(
            FULL_PROFILE,
            full=True,
            reason="fork-pull-request-requires-full",
            paths=paths,
            domains=("high-risk",),
        )
    if not base_reachable:
        return _impact(
            FULL_PROFILE,
            full=True,
            reason="unreachable-base-sha",
            paths=paths,
            domains=("high-risk",),
        )
    if expected_base_sha is not None and base_sha != expected_base_sha:
        return _impact(
            FULL_PROFILE,
            full=True,
            reason="stale-base-sha",
            paths=paths,
            domains=("high-risk",),
        )
    if not paths:
        return _impact(
            FULL_PROFILE,
            full=True,
            reason="empty-change-set",
            paths=paths,
            domains=("high-risk",),
        )

    domains = tuple(sorted({_path_domain(path) or "unknown" for path in paths}))
    if "unknown" in domains:
        unknown_path = next(path for path in paths if _path_domain(path) is None)
        return _impact(
            FULL_PROFILE,
            full=True,
            reason=f"unclassified-path:{unknown_path}",
            paths=paths,
            domains=domains,
        )
    high_risk_domains = _HIGH_RISK_DOMAINS.intersection(domains)
    high_risk_installer_paths = set(paths).intersection(_HIGH_RISK_INSTALLER_FILES)
    if high_risk_domains or high_risk_installer_paths:
        high_risk_path = next(
            path
            for path in paths
            if _path_domain(path) in _HIGH_RISK_DOMAINS
            or path in _HIGH_RISK_INSTALLER_FILES
        )
        return _impact(
            FULL_PROFILE,
            full=True,
            reason=f"high-risk-path:{high_risk_path}",
            paths=paths,
            domains=domains,
        )

    functional_domains = set(domains) - {"documentation"}
    if not functional_domains:
        return _impact(
            DOCS_PROFILE,
            full=False,
            reason="explicit-docs-only",
            paths=paths,
            domains=domains,
        )
    if len(functional_domains) > 1:
        return _impact(
            FULL_PROFILE,
            full=True,
            reason="cross-domain-change",
            paths=paths,
            domains=domains,
        )
    domain = next(iter(functional_domains))
    if domain == "backend":
        profile = BACKEND_PROFILE
    elif domain == "web":
        profile = WEB_PROFILE
    else:
        profile = RELEASE_DESKTOP_PROFILE
    return _impact(
        profile,
        full=False,
        reason=f"explicit-{domain}-only",
        paths=paths,
        domains=domains,
    )


def changed_files_between(repo_root: Path, base: str, head: str) -> tuple[str, ...]:
    output = subprocess.check_output(
        [
            "git",
            "-C",
            os.fspath(repo_root),
            "diff",
            "--name-only",
            "-z",
            f"{base}...{head}",
        ]
    )
    return tuple(os.fsdecode(path) for path in output.split(b"\0") if path)


def _output_lines(impact: Impact) -> tuple[str, ...]:
    return (
        f"profile={impact.profile}",
        f"full={str(impact.full).lower()}",
        f"reason={impact.reason}",
        f"domains={json.dumps(impact.domains, separators=(',', ':'))}",
        f"required_jobs={json.dumps(impact.required_jobs, separators=(',', ':'))}",
        f"skipped_jobs={json.dumps(impact.skipped_jobs, separators=(',', ':'))}",
    )


def _write_github_output(output_path: Path, impact: Impact) -> None:
    with output_path.open("a", encoding="utf-8") as output:
        output.write("\n".join(_output_lines(impact)) + "\n")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify CI impact conservatively.")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--expected-base")
    parser.add_argument("--fork-pull-request", action="store_true")
    parser.add_argument("--base-unreachable", action="store_true")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--check-inventory", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    if args.check_inventory:
        try:
            unclassified = unclassified_tracked_paths(repo_root)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"risk inventory failed: {error}")
            return 2
        if unclassified:
            print("unclassified tracked paths:")
            print("\n".join(unclassified))
            return 1

    changed_files = tuple(args.changed_file)
    if not changed_files:
        if bool(args.base) != bool(args.head):
            impact = _impact(
                FULL_PROFILE,
                full=True,
                reason="incomplete-diff-range",
                paths=(),
                domains=("high-risk",),
            )
        elif args.base and args.head:
            try:
                changed_files = changed_files_between(repo_root, args.base, args.head)
            except (OSError, subprocess.CalledProcessError):
                impact = _impact(
                    FULL_PROFILE,
                    full=True,
                    reason="diff-failed",
                    paths=(),
                    domains=("high-risk",),
                )
            else:
                impact = classify_impact(
                    args.event_name,
                    changed_files,
                    fork_pull_request=args.fork_pull_request,
                    base_sha=args.base,
                    expected_base_sha=args.expected_base,
                    base_reachable=not args.base_unreachable,
                )
        else:
            impact = _impact(
                FULL_PROFILE,
                full=True,
                reason="missing-change-source",
                paths=(),
                domains=("high-risk",),
            )
    else:
        impact = classify_impact(
            args.event_name,
            changed_files,
            fork_pull_request=args.fork_pull_request,
            base_sha=args.base,
            expected_base_sha=args.expected_base,
            base_reachable=not args.base_unreachable,
        )

    print("\n".join(_output_lines(impact)))
    if args.github_output is not None:
        _write_github_output(args.github_output, impact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
