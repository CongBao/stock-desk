from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.e2e_snapshot import (
    SnapshotError,
    build_snapshot_manifest,
    verify_snapshot,
    write_snapshot_manifest,
)


SOURCE_SHA = "1" * 40
SOURCE_TREE = "2" * 40


def test_snapshot_manifest_binds_source_and_content(tmp_path: Path) -> None:
    (tmp_path / "stock-desk.db").write_bytes(b"sqlite")
    market = tmp_path / "market"
    market.mkdir()
    (market / "part.parquet").write_bytes(b"parquet")

    manifest = build_snapshot_manifest(
        tmp_path,
        source_commit=SOURCE_SHA,
        source_tree=SOURCE_TREE,
    )

    assert manifest["schema_version"] == 1
    assert manifest["source_commit"] == SOURCE_SHA
    assert manifest["source_tree"] == SOURCE_TREE
    assert manifest["files"] == {
        "market/part.parquet": hashlib.sha256(b"parquet").hexdigest(),
        "stock-desk.db": hashlib.sha256(b"sqlite").hexdigest(),
    }
    assert verify_snapshot(tmp_path, manifest) == manifest["snapshot_digest"]


def test_snapshot_verification_rejects_mutation_and_extra_files(tmp_path: Path) -> None:
    database = tmp_path / "stock-desk.db"
    database.write_bytes(b"before")
    manifest = build_snapshot_manifest(
        tmp_path,
        source_commit=SOURCE_SHA,
        source_tree=SOURCE_TREE,
    )

    database.write_bytes(b"after")
    with pytest.raises(SnapshotError, match="content mismatch"):
        verify_snapshot(tmp_path, manifest)

    database.write_bytes(b"before")
    (tmp_path / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(SnapshotError, match="file inventory mismatch"):
        verify_snapshot(tmp_path, manifest)


def test_snapshot_verification_rejects_invalid_identity(tmp_path: Path) -> None:
    (tmp_path / "stock-desk.db").write_bytes(b"data")
    manifest = build_snapshot_manifest(
        tmp_path,
        source_commit=SOURCE_SHA,
        source_tree=SOURCE_TREE,
    )
    invalid = json.loads(json.dumps(manifest))
    invalid["source_commit"] = "main"

    with pytest.raises(SnapshotError, match="source_commit"):
        verify_snapshot(tmp_path, invalid)


def test_snapshot_manifest_write_round_trip_and_empty_snapshot_rejection(
    tmp_path: Path,
) -> None:
    with pytest.raises(SnapshotError, match="inventory is empty"):
        build_snapshot_manifest(
            tmp_path,
            source_commit=SOURCE_SHA,
            source_tree=SOURCE_TREE,
        )

    (tmp_path / "stock-desk.db").write_bytes(b"database")
    manifest = build_snapshot_manifest(
        tmp_path,
        source_commit=SOURCE_SHA,
        source_tree=SOURCE_TREE,
    )
    destination = write_snapshot_manifest(tmp_path, manifest)

    assert destination.name == "snapshot-manifest.json"
    assert json.loads(destination.read_text(encoding="utf-8")) == manifest
    assert verify_snapshot(tmp_path, json.loads(destination.read_text()))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda _manifest: [], "must be an object"),
        (lambda manifest: {**manifest, "schema_version": 2}, "unsupported"),
        (lambda manifest: {**manifest, "source_tree": None}, "source_tree is required"),
        (lambda manifest: {**manifest, "files": []}, "files must be"),
        (
            lambda manifest: {**manifest, "files": {"stock-desk.db": 7}},
            "files must be",
        ),
        (
            lambda manifest: {**manifest, "snapshot_digest": "0" * 64},
            "aggregate digest mismatch",
        ),
    ],
)
def test_snapshot_verification_rejects_malformed_manifest_shapes(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    (tmp_path / "stock-desk.db").write_bytes(b"database")
    manifest = build_snapshot_manifest(
        tmp_path,
        source_commit=SOURCE_SHA,
        source_tree=SOURCE_TREE,
    )

    with pytest.raises(SnapshotError, match=message):
        verify_snapshot(tmp_path, mutation(manifest))
