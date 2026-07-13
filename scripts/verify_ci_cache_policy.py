from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml  # type: ignore[import-untyped]


POLICY_SCHEMA_VERSION = 1
ALLOWED_CONTENT_CLASSES = frozenset(
    {
        "dependency-downloads",
        "browser-binaries",
        "compiled-intermediates",
        "audit-tool",
        "vulnerability-database",
    }
)
_ECOSYSTEM_CONTENT = {
    "uv": frozenset({"dependency-downloads"}),
    "pnpm": frozenset({"dependency-downloads"}),
    "playwright": frozenset({"browser-binaries"}),
    "cargo": frozenset(
        {
            "dependency-downloads",
            "compiled-intermediates",
            "audit-tool",
            "vulnerability-database",
        }
    ),
}
_PATH_CONTENT_CLASSES = {
    "uv": {"~/.cache/uv": frozenset({"dependency-downloads"})},
    "pnpm": {
        "~/.pnpm-store": frozenset({"dependency-downloads"}),
        "~/.local/share/pnpm/store": frozenset({"dependency-downloads"}),
    },
    "playwright": {
        "~/.cache/ms-playwright": frozenset({"browser-binaries"}),
    },
    "cargo": {
        "~/.cargo/registry": frozenset({"dependency-downloads"}),
        "~/.cargo/git": frozenset({"dependency-downloads"}),
        "~/.cargo/bin/cargo-audit": frozenset({"audit-tool"}),
        "~/.cargo/advisory-db": frozenset({"vulnerability-database"}),
        "target/debug/build": frozenset({"compiled-intermediates"}),
        "target/debug/deps": frozenset({"compiled-intermediates"}),
        "target/debug/incremental": frozenset({"compiled-intermediates"}),
        "target/release/build": frozenset({"compiled-intermediates"}),
        "target/release/deps": frozenset({"compiled-intermediates"}),
        "target/release/incremental": frozenset({"compiled-intermediates"}),
    },
}
_PROHIBITED_PATH_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(^|[/_.-])junit([/_.-]|$)",
        r"(^|[/_.-])coverage([/_.-]|$)",
        r"(^|[/_.-])test-results?([/_.-]|$)",
        r"(^|[/_.-])pytest([/_.-]|$)",
        r"(^|[/_.-])(database|sqlite|duckdb)([/_.-]|$)",
        r"\.(db|sqlite|sqlite3|duckdb)$",
        r"(^|[/_.-])(artifacts?|manifest|proof|attestation)([/_.-]|$)",
        r"(^|[/_.-])(signature|signed|signing)([/_.-]|$)",
        r"(^|[/_.-])(sbom|provenance)([/_.-]|$)",
        r"(^|/)dist(/|$)",
        r"(^|/)release(s)?(/|$)",
    )
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_EXPRESSION = re.compile(r"\$\{\{[^}]+\}\}")
_LOCKFILE_EXPRESSION = re.compile(r"\$\{\{\s*hashFiles\([^}]+\)\s*\}\}", re.I)


class CachePolicyError(ValueError):
    """A cache could influence conclusions, identity, or cross-environment output."""


def _cache_path(raw: object, *, entry: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\\" in raw or "\x00" in raw:
        raise CachePolicyError(f"{entry}: cache paths must be non-empty POSIX paths")
    value = raw.strip()
    normalized_for_check = value.replace("~/", "home/", 1)
    pure = PurePosixPath(normalized_for_check)
    if ".." in pure.parts:
        raise CachePolicyError(f"{entry}: cache paths cannot contain '..'")
    if "." in pure.parts or pure.as_posix() != normalized_for_check:
        raise CachePolicyError(f"{entry}: cache paths must be normalized")
    for pattern in _PROHIBITED_PATH_PATTERNS:
        if pattern.search(value):
            raise CachePolicyError(f"{entry}: prohibited cache content path: {value}")
    return value


def validate_cache_policy(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "entries"}:
        raise CachePolicyError("policy must contain exactly schema_version and entries")
    if raw["schema_version"] != POLICY_SCHEMA_VERSION:
        raise CachePolicyError(f"schema_version must be {POLICY_SCHEMA_VERSION}")
    entries = raw["entries"]
    if not isinstance(entries, list) or not entries:
        raise CachePolicyError("entries must be a non-empty array")

    normalized_entries: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw_entry in enumerate(entries):
        label = f"entries[{index}]"
        expected_fields = {
            "name",
            "ecosystem",
            "paths",
            "key",
            "dimensions",
            "content_classes",
        }
        if not isinstance(raw_entry, dict) or set(raw_entry) != expected_fields:
            raise CachePolicyError(
                f"{label} must contain exactly {', '.join(sorted(expected_fields))}"
            )
        name = raw_entry["name"]
        if not isinstance(name, str) or not name.strip():
            raise CachePolicyError(f"{label}.name must be a non-empty string")
        if name in names:
            raise CachePolicyError(f"duplicate cache entry name: {name}")
        names.add(name)
        ecosystem = raw_entry["ecosystem"]
        if ecosystem not in _ECOSYSTEM_CONTENT:
            raise CachePolicyError(f"{name}: unsupported ecosystem: {ecosystem}")

        paths = raw_entry["paths"]
        if not isinstance(paths, list) or not paths:
            raise CachePolicyError(f"{name}: paths must be a non-empty array")
        normalized_paths = [_cache_path(path, entry=name) for path in paths]
        for path in normalized_paths:
            if path not in _PATH_CONTENT_CLASSES[ecosystem]:
                raise CachePolicyError(
                    f"{name}: path is not an allowed {ecosystem} intermediate: {path}"
                )
        if len(set(normalized_paths)) != len(normalized_paths):
            raise CachePolicyError(f"{name}: cache paths must be unique")

        content_classes = raw_entry["content_classes"]
        if not isinstance(content_classes, list) or not content_classes:
            raise CachePolicyError(f"{name}: content_classes must be non-empty")
        if any(not isinstance(item, str) for item in content_classes):
            raise CachePolicyError(f"{name}: content_classes must contain strings")
        class_set = frozenset(content_classes)
        if not class_set <= ALLOWED_CONTENT_CLASSES:
            rejected = sorted(class_set - ALLOWED_CONTENT_CLASSES)
            raise CachePolicyError(f"{name}: prohibited content classes: {rejected}")
        if not class_set <= _ECOSYSTEM_CONTENT[ecosystem]:
            raise CachePolicyError(
                f"{name}: content classes are not valid for {ecosystem}"
            )
        expected_classes = frozenset().union(
            *(_PATH_CONTENT_CLASSES[ecosystem][path] for path in normalized_paths)
        )
        if class_set != expected_classes:
            raise CachePolicyError(
                f"{name}: content classes do not exactly match cache paths"
            )

        key = raw_entry["key"]
        if not isinstance(key, str) or not key.strip():
            raise CachePolicyError(f"{name}: key must be a non-empty string")
        dimensions = raw_entry["dimensions"]
        dimension_names = {"os", "architecture", "toolchain", "lockfile"}
        if not isinstance(dimensions, dict) or set(dimensions) != dimension_names:
            raise CachePolicyError(
                f"{name}: dimensions must contain exactly OS, architecture, toolchain, lockfile"
            )
        normalized_dimensions: dict[str, str] = {}
        for dimension in sorted(dimension_names):
            value = dimensions[dimension]
            if not isinstance(value, str) or not value.strip():
                raise CachePolicyError(f"{name}: dimension {dimension} is empty")
            if value not in key:
                raise CachePolicyError(
                    f"{name}: cache key does not include {dimension} dimension"
                )
            normalized_dimensions[dimension] = value
        lock_dimension = normalized_dimensions["lockfile"].lower()
        if "hashfiles(" not in lock_dimension and not _SHA256.fullmatch(
            normalized_dimensions["lockfile"]
        ):
            raise CachePolicyError(
                f"{name}: lockfile dimension must be hashFiles(...) or an exact SHA-256"
            )
        if "runner.os" not in normalized_dimensions["os"].lower():
            raise CachePolicyError(f"{name}: OS dimension must bind runner.os")
        if "runner.arch" not in normalized_dimensions["architecture"].lower():
            raise CachePolicyError(
                f"{name}: architecture dimension must bind runner.arch"
            )
        normalized_entries.append(
            {
                "name": name,
                "ecosystem": ecosystem,
                "paths": normalized_paths,
                "key": key,
                "dimensions": normalized_dimensions,
                "content_classes": sorted(class_set),
            }
        )
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "entries": sorted(normalized_entries, key=lambda entry: entry["name"]),
    }


def _infer_ecosystem(paths: list[str]) -> str:
    matches = {
        ecosystem
        for ecosystem, allowed_paths in _PATH_CONTENT_CLASSES.items()
        if any(path in allowed_paths for path in paths)
    }
    if len(matches) != 1:
        raise CachePolicyError(
            "an actions/cache step must contain paths for exactly one supported ecosystem"
        )
    return next(iter(matches))


def _workflow_cache_entry(
    *, workflow: Path, job_name: str, step_index: int, step: Mapping[str, object]
) -> dict[str, object]:
    location = f"{workflow.as_posix()}:{job_name}:step-{step_index}"
    settings = step.get("with")
    if not isinstance(settings, dict):
        raise CachePolicyError(f"{location}: actions/cache requires a with object")
    if "restore-keys" in settings or "restore_keys" in settings:
        raise CachePolicyError(
            f"{location}: restore keys are forbidden because they bypass exact lock identity"
        )
    raw_paths = settings.get("path")
    if not isinstance(raw_paths, str):
        raise CachePolicyError(f"{location}: actions/cache path must be a string")
    paths = [line.strip() for line in raw_paths.splitlines() if line.strip()]
    if not paths:
        raise CachePolicyError(f"{location}: actions/cache path is empty")
    ecosystem = _infer_ecosystem(paths)
    key = settings.get("key")
    if not isinstance(key, str):
        raise CachePolicyError(f"{location}: actions/cache key must be a string")
    expressions = _GITHUB_EXPRESSION.findall(key)
    toolchain_candidates = [
        expression
        for expression in expressions
        if "version" in expression.lower() and "hashfiles(" not in expression.lower()
    ]
    lockfile = _LOCKFILE_EXPRESSION.search(key)
    if not toolchain_candidates:
        raise CachePolicyError(
            f"{location}: cache key must bind an explicit toolchain version expression"
        )
    if lockfile is None:
        raise CachePolicyError(
            f"{location}: cache key must bind an exact lockfile hashFiles expression"
        )
    content_classes = sorted(
        frozenset().union(*(_PATH_CONTENT_CLASSES[ecosystem][path] for path in paths))
    )
    return {
        "name": location,
        "ecosystem": ecosystem,
        "paths": paths,
        "key": key,
        "dimensions": {
            "os": "${{ runner.os }}",
            "architecture": "${{ runner.arch }}",
            "toolchain": toolchain_candidates[0],
            "lockfile": lockfile.group(0),
        },
        "content_classes": content_classes,
    }


def verify_workflow_cache_policy(workflow_paths: Sequence[Path]) -> int:
    """Inventory every workflow cache and reject implicit or unkeyed caches."""
    entries: list[dict[str, object]] = []
    for workflow_path in workflow_paths:
        try:
            raw = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            raise CachePolicyError(
                f"cannot read workflow {workflow_path}: {error}"
            ) from error
        if not isinstance(raw, dict) or not isinstance(raw.get("jobs"), dict):
            raise CachePolicyError(f"workflow has no jobs object: {workflow_path}")
        for raw_job_name, raw_job in raw["jobs"].items():
            if not isinstance(raw_job_name, str) or not isinstance(raw_job, dict):
                raise CachePolicyError(f"invalid workflow job in {workflow_path}")
            steps = raw_job.get("steps", [])
            if not isinstance(steps, list):
                raise CachePolicyError(
                    f"{workflow_path}:{raw_job_name}: steps must be an array"
                )
            for index, raw_step in enumerate(steps):
                if not isinstance(raw_step, dict):
                    raise CachePolicyError(
                        f"{workflow_path}:{raw_job_name}: step {index} must be an object"
                    )
                uses = raw_step.get("uses")
                if not isinstance(uses, str):
                    continue
                settings = raw_step.get("with")
                if isinstance(settings, dict) and (
                    settings.get("enable-cache") is True
                    or settings.get("enable-cache") == "true"
                    or (
                        "cache" in settings
                        and settings.get("cache") not in (None, False, "")
                    )
                ):
                    raise CachePolicyError(
                        f"{workflow_path}:{raw_job_name}: step {index} uses an implicit "
                        "action cache; use explicit actions/cache with all four dimensions"
                    )
                if uses.startswith("actions/cache@") or uses.startswith(
                    "actions/cache/"
                ):
                    entries.append(
                        _workflow_cache_entry(
                            workflow=workflow_path,
                            job_name=raw_job_name,
                            step_index=index,
                            step=raw_step,
                        )
                    )
    if not entries:
        raise CachePolicyError(
            "no explicit cache entries found in the supplied workflows"
        )
    validate_cache_policy({"schema_version": 1, "entries": entries})
    return len(entries)


def validate_cache_run_evidence(raw: object, *, expected_state: str) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "cache_state",
        "source_sha",
        "source_tree",
        "install_completed",
        "required_gates",
        "artifact_manifests",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise CachePolicyError(
            "cache run evidence must contain exactly "
            + ", ".join(sorted(expected_fields))
        )
    if raw["schema_version"] != 1:
        raise CachePolicyError("cache run evidence schema_version must be 1")
    if raw["cache_state"] != expected_state:
        raise CachePolicyError(f"expected cache_state {expected_state}")
    for field in ("source_sha", "source_tree"):
        value = raw[field]
        if not isinstance(value, str) or _GIT_OBJECT.fullmatch(value) is None:
            raise CachePolicyError(f"{field} must be an exact lowercase git object id")
    if raw["install_completed"] is not True:
        raise CachePolicyError(
            "dependency installation must complete even on cache miss"
        )
    gates = raw["required_gates"]
    if not isinstance(gates, dict) or not gates:
        raise CachePolicyError("required_gates must be a non-empty object")
    for name, conclusion in gates.items():
        if not isinstance(name, str) or not name or conclusion != "success":
            raise CachePolicyError("every required gate must have conclusion success")
    manifests = raw["artifact_manifests"]
    if not isinstance(manifests, dict) or not manifests:
        raise CachePolicyError("artifact_manifests must be a non-empty object")
    for name, digest in manifests.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise CachePolicyError(
                "artifact_manifests must map names to lowercase SHA-256 identities"
            )
    return dict(raw)


def compare_clean_and_warm_runs(clean: object, warm: object) -> None:
    clean_run = validate_cache_run_evidence(clean, expected_state="clean-miss")
    warm_run = validate_cache_run_evidence(warm, expected_state="warm")
    for field in ("source_sha", "source_tree", "required_gates"):
        if clean_run[field] != warm_run[field]:
            raise CachePolicyError(f"clean and warm runs differ in {field}")
    if set(clean_run["artifact_manifests"]) != set(warm_run["artifact_manifests"]):
        raise CachePolicyError("clean and warm runs produced different artifact sets")


def _load_json(path: Path, *, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CachePolicyError(f"cannot read {label}: {error}") from error


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify fail-closed CI cache policy.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    policy = subparsers.add_parser("policy")
    policy.add_argument("path", type=Path)
    compare = subparsers.add_parser("compare-runs")
    compare.add_argument("--clean", type=Path, required=True)
    compare.add_argument("--warm", type=Path, required=True)
    workflows = subparsers.add_parser("workflows")
    workflows.add_argument("paths", type=Path, nargs="+")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "policy":
            policy = validate_cache_policy(_load_json(args.path, label="cache policy"))
            print(f"valid cache entries: {len(policy['entries'])}")
        elif args.command == "compare-runs":
            compare_clean_and_warm_runs(
                _load_json(args.clean, label="clean-cache evidence"),
                _load_json(args.warm, label="warm-cache evidence"),
            )
            print("clean-cache and warm-cache gates match")
        else:
            count = verify_workflow_cache_policy(args.paths)
            print(f"valid workflow cache entries: {count}")
    except CachePolicyError as error:
        print(f"cache policy error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
