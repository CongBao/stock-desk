from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final


INVENTORY_SCHEMA: Final = "stock-desk-pytest-inventory-v1"
SHARDS: Final = (
    "unit",
    "integration",
    "acceptance-performance",
    "security",
)
SHARD_ROOTS: Final[dict[str, tuple[str, ...]]] = {
    "unit": ("tests/unit", "tests/contract", "tests/property"),
    "integration": ("tests/integration",),
    "acceptance-performance": ("tests/acceptance", "tests/performance"),
    "security": ("tests/security",),
}
MAX_COLLECTION_BYTES: Final = 16_000_000


class InventoryError(ValueError):
    pass


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Embed exact nodeids in xUnit reports when loaded with ``-p``.

    The evidence aggregator deliberately refuses to infer nodeids from lossy
    classname/name transformations.  CI loads this module as a pytest plugin so
    every authoritative first-run testcase carries its exact collection id.
    """

    identity = {
        "stock_desk_source_sha": os.environ.get("STOCK_DESK_SOURCE_SHA"),
        "stock_desk_source_tree": os.environ.get("STOCK_DESK_SOURCE_TREE"),
        "stock_desk_shard": os.environ.get("STOCK_DESK_PYTHON_SHARD"),
    }
    present = [value is not None for value in identity.values()]
    if any(present) and not all(present):
        raise InventoryError(
            "pytest evidence identity requires source SHA, tree, and shard together"
        )
    for item in items:
        item.user_properties.append(("stock_desk_nodeid", item.nodeid))
        for key, value in identity.items():
            if value is not None:
                item.user_properties.append((key, value))


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _expect_git_oid(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise InventoryError(f"{label} must be a lowercase 40-character git oid")
    return value


def normalize_nodeid(raw: str) -> str:
    nodeid = raw.strip().replace("\\", "/")
    if not nodeid or "\x00" in nodeid or nodeid.startswith(("/", "-")):
        raise InventoryError(f"unsafe pytest nodeid: {raw!r}")
    path_text = nodeid.partition("::")[0]
    path = PurePosixPath(path_text)
    if path.is_absolute() or ".." in path.parts or not path_text.startswith("tests/"):
        raise InventoryError(f"pytest nodeid escapes the test tree: {raw!r}")
    terminal = nodeid.partition("[")[0].rsplit("::", maxsplit=1)[-1]
    if "::" not in nodeid or not terminal.startswith("test"):
        raise InventoryError(f"pytest nodeid is not function-level: {raw!r}")
    return nodeid


def shard_for_nodeid(nodeid: str) -> str:
    normalized = normalize_nodeid(nodeid)
    path = normalized.partition("::")[0]
    matches = [
        shard
        for shard, roots in SHARD_ROOTS.items()
        if any(path == root or path.startswith(f"{root}/") for root in roots)
    ]
    if len(matches) != 1:
        detail = "unowned" if not matches else f"owned by {', '.join(matches)}"
        raise InventoryError(f"{normalized} is {detail}")
    return matches[0]


def parse_collection_output(output: str) -> tuple[str, ...]:
    if len(output.encode("utf-8")) > MAX_COLLECTION_BYTES:
        raise InventoryError("pytest collection output exceeds the safety limit")
    collected: list[str] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("tests/") or "::" not in line:
            continue
        nodeid = normalize_nodeid(line)
        if nodeid in seen:
            raise InventoryError(f"pytest collected duplicate nodeid: {nodeid}")
        seen.add(nodeid)
        collected.append(nodeid)
    if not collected:
        raise InventoryError("pytest collection produced no function-level nodeids")
    return tuple(sorted(collected))


def partition_nodeids(nodeids: Iterable[str]) -> dict[str, tuple[str, ...]]:
    partitions: dict[str, list[str]] = {shard: [] for shard in SHARDS}
    seen: set[str] = set()
    for raw in nodeids:
        nodeid = normalize_nodeid(raw)
        if nodeid in seen:
            raise InventoryError(f"duplicate nodeid in inventory: {nodeid}")
        seen.add(nodeid)
        partitions[shard_for_nodeid(nodeid)].append(nodeid)
    if not seen:
        raise InventoryError("inventory cannot be empty")
    return {shard: tuple(sorted(partitions[shard])) for shard in SHARDS}


def build_inventory(
    nodeids: Iterable[str], *, source_sha: str, source_tree: str
) -> dict[str, Any]:
    sha = _expect_git_oid(source_sha, "source_sha")
    tree = _expect_git_oid(source_tree, "source_tree")
    partitions = partition_nodeids(nodeids)
    flattened = tuple(nodeid for shard in SHARDS for nodeid in partitions[shard])
    inventory = {
        "schema": INVENTORY_SCHEMA,
        "source_sha": sha,
        "source_tree": tree,
        "shards": {
            shard: {
                "count": len(partitions[shard]),
                "nodeids": list(partitions[shard]),
                "nodeids_sha256": sha256_json(list(partitions[shard])),
            }
            for shard in SHARDS
        },
        "total_count": len(flattened),
        "all_nodeids_sha256": sha256_json(sorted(flattened)),
    }
    inventory["inventory_sha256"] = sha256_json(inventory)
    return inventory


def validate_inventory(
    raw: object, *, source_sha: str | None = None, source_tree: str | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InventoryError("inventory must be a JSON object")
    expected_fields = {
        "schema",
        "source_sha",
        "source_tree",
        "shards",
        "total_count",
        "all_nodeids_sha256",
        "inventory_sha256",
    }
    if set(raw) != expected_fields:
        raise InventoryError("inventory fields do not match the v1 schema")
    if raw["schema"] != INVENTORY_SCHEMA:
        raise InventoryError("unsupported pytest inventory schema")
    sha = _expect_git_oid(raw["source_sha"], "source_sha")
    tree = _expect_git_oid(raw["source_tree"], "source_tree")
    if source_sha is not None and sha != _expect_git_oid(
        source_sha, "expected source_sha"
    ):
        raise InventoryError("inventory source_sha does not match")
    if source_tree is not None and tree != _expect_git_oid(
        source_tree, "expected source_tree"
    ):
        raise InventoryError("inventory source_tree does not match")
    shards = raw["shards"]
    if not isinstance(shards, dict) or set(shards) != set(SHARDS):
        raise InventoryError("inventory must contain the four ordered Python shards")
    nodeids: list[str] = []
    for shard in SHARDS:
        payload = shards[shard]
        if not isinstance(payload, dict) or set(payload) != {
            "count",
            "nodeids",
            "nodeids_sha256",
        }:
            raise InventoryError(f"{shard} inventory fields are invalid")
        listed = payload["nodeids"]
        if not isinstance(listed, list) or not all(
            isinstance(item, str) for item in listed
        ):
            raise InventoryError(f"{shard} nodeids must be strings")
        normalized = tuple(normalize_nodeid(item) for item in listed)
        if normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(
            normalized
        ):
            raise InventoryError(f"{shard} nodeids must be sorted and unique")
        if any(shard_for_nodeid(item) != shard for item in normalized):
            raise InventoryError(f"{shard} contains a nodeid owned by another shard")
        if type(payload["count"]) is not int or payload["count"] != len(normalized):
            raise InventoryError(f"{shard} nodeid count does not match")
        if payload["nodeids_sha256"] != sha256_json(list(normalized)):
            raise InventoryError(f"{shard} nodeid digest does not match")
        nodeids.extend(normalized)
    if len(set(nodeids)) != len(nodeids):
        raise InventoryError("a nodeid is owned by more than one shard")
    if type(raw["total_count"]) is not int or raw["total_count"] != len(nodeids):
        raise InventoryError("total nodeid count does not match")
    if raw["all_nodeids_sha256"] != sha256_json(sorted(nodeids)):
        raise InventoryError("complete nodeid digest does not match")
    unsigned = dict(raw)
    digest = unsigned.pop("inventory_sha256")
    if digest != sha256_json(unsigned):
        raise InventoryError("inventory document digest does not match")
    return raw


def collect_nodeids(repo_root: Path, *, shard: str | None = None) -> tuple[str, ...]:
    roots = ["tests"] if shard is None else list(SHARD_ROOTS[shard])
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", *roots]
    result = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        raise InventoryError(
            "pytest collection failed:\n" + result.stdout + result.stderr
        )
    nodeids = parse_collection_output(result.stdout)
    if shard is not None and any(shard_for_nodeid(item) != shard for item in nodeids):
        raise InventoryError(f"pytest collection escaped the {shard} shard")
    return nodeids


def _git_oid(repo_root: Path, expression: str) -> str:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", expression], cwd=repo_root, text=True
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise InventoryError(f"cannot resolve {expression}") from exc
    return _expect_git_oid(value, expression)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot read inventory {path}: {exc}") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect and verify the exact Python test shard inventory"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--repo-root", type=Path, default=Path.cwd())
    collect.add_argument("--source-sha")
    collect.add_argument("--source-tree")
    collect.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--inventory", type=Path, required=True)
    verify.add_argument("--source-sha")
    verify.add_argument("--source-tree")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "collect":
            repo_root = args.repo_root.resolve()
            source_sha = args.source_sha or _git_oid(repo_root, "HEAD")
            source_tree = args.source_tree or _git_oid(repo_root, "HEAD^{tree}")
            inventory = build_inventory(
                collect_nodeids(repo_root),
                source_sha=source_sha,
                source_tree=source_tree,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(canonical_json(inventory))
            print(
                f"collected {inventory['total_count']} uniquely-owned pytest nodeids "
                f"for {source_sha}"
            )
        else:
            inventory = validate_inventory(
                _load_json(args.inventory),
                source_sha=args.source_sha,
                source_tree=args.source_tree,
            )
            print(f"verified {inventory['total_count']} uniquely-owned pytest nodeids")
    except (InventoryError, OSError) as exc:
        print(f"pytest inventory error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
