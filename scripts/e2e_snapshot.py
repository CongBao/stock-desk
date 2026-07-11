"""Create and verify content-bound deterministic E2E snapshot manifests."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TypedDict, cast


_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_NAME = "snapshot-manifest.json"


class SnapshotError(ValueError):
    """Raised when an E2E snapshot cannot be trusted."""


class SnapshotManifest(TypedDict):
    schema_version: int
    source_commit: str
    source_tree: str
    files: dict[str, str]
    snapshot_digest: str


def _validate_identity(name: str, value: str) -> None:
    if _HEX_40.fullmatch(value) is None:
        raise SnapshotError(f"{name} must be a lowercase 40-character Git object id")


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != _MANIFEST_NAME
    }


def _digest(files: dict[str, str]) -> str:
    payload = json.dumps(
        files, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_snapshot_manifest(
    root: Path,
    *,
    source_commit: str,
    source_tree: str,
) -> SnapshotManifest:
    """Return a manifest binding every seeded file to an exact source tree."""
    _validate_identity("source_commit", source_commit)
    _validate_identity("source_tree", source_tree)
    files = _files(root)
    if not files:
        raise SnapshotError("snapshot file inventory is empty")
    return SnapshotManifest(
        schema_version=1,
        source_commit=source_commit,
        source_tree=source_tree,
        files=files,
        snapshot_digest=_digest(files),
    )


def write_snapshot_manifest(root: Path, manifest: SnapshotManifest) -> Path:
    destination = root / _MANIFEST_NAME
    temporary = root / f".{_MANIFEST_NAME}.tmp"
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def verify_snapshot(root: Path, raw_manifest: object) -> str:
    """Fail closed unless identity, inventory, hashes, and aggregate digest match."""
    if not isinstance(raw_manifest, dict):
        raise SnapshotError("snapshot manifest must be an object")
    manifest = cast(dict[str, object], raw_manifest)
    if manifest.get("schema_version") != 1:
        raise SnapshotError("unsupported snapshot manifest schema")
    for key in ("source_commit", "source_tree"):
        value = manifest.get(key)
        if not isinstance(value, str):
            raise SnapshotError(f"{key} is required")
        _validate_identity(key, value)
    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in expected_files.items()
    ):
        raise SnapshotError("snapshot files must be a string digest map")
    actual_files = _files(root)
    if set(actual_files) != set(expected_files):
        raise SnapshotError("snapshot file inventory mismatch")
    if actual_files != expected_files:
        raise SnapshotError("snapshot content mismatch")
    actual_digest = _digest(actual_files)
    if manifest.get("snapshot_digest") != actual_digest:
        raise SnapshotError("snapshot aggregate digest mismatch")
    return actual_digest
