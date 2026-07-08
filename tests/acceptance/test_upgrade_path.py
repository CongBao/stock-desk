from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from stock_desk.storage.database import migrate


ROOT = Path(__file__).resolve().parents[2]
RELEASE_FIXTURES = ROOT / "tests" / "fixtures" / "releases"
RELEASE_TAGS = ("v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0", "v0.5.0")
HEAD_REVISION = "0010_parent_active_retry"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _manifest(tag: str) -> dict[str, object]:
    path = RELEASE_FIXTURES / tag / "manifest.json"
    assert path.is_file(), f"missing exact tagged release fixture: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _database_inventory(
    database: Path,
    expected: dict[str, object],
) -> dict[str, list[list[str]]]:
    inventory: dict[str, list[list[str]]] = {}
    with sqlite3.connect(database) as connection:
        for table, raw in expected.items():
            assert isinstance(table, str)
            assert isinstance(raw, list)
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if int(row[5]) > 0
            ]
            assert columns, f"fixture inventory table has no primary key: {table}"
            order = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(
                f'SELECT {order} FROM "{table}" ORDER BY {order}'
            ).fetchall()
            inventory[table] = [[str(value) for value in row] for row in rows]
    return inventory


@pytest.mark.parametrize("tag", RELEASE_TAGS)
def test_upgrade_from_exact_tagged_release_fixture_is_idempotent(
    tag: str,
    tmp_path: Path,
) -> None:
    source = RELEASE_FIXTURES / tag
    manifest = _manifest(tag)
    expected_commit = subprocess.run(
        ["git", "rev-parse", f"{tag}^{{commit}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert manifest["schema_version"] == "stock-desk-tagged-release-fixture-v1"
    assert manifest["tag"] == tag
    assert manifest["tag_commit"] == expected_commit
    assert manifest["generated_by"] == "checked-out-tag-software"

    destination = tmp_path / tag
    shutil.copytree(source, destination)
    database = destination / "stock-desk.db"
    assert _sha256(database) == manifest["database_sha256"]
    expected_inventory = manifest["logical_inventory"]
    assert isinstance(expected_inventory, dict)
    assert _database_inventory(database, expected_inventory) == expected_inventory
    market_hashes = manifest["market_files"]
    assert isinstance(market_hashes, dict)
    assert {
        relative: _sha256(destination / relative) for relative in market_hashes
    } == market_hashes

    database_url = f"sqlite:///{database}"
    migrate(database_url)
    first_inventory = _database_inventory(database, expected_inventory)
    first_market_hashes = {
        relative: _sha256(destination / relative) for relative in market_hashes
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD_REVISION,)

    migrate(database_url)
    assert _database_inventory(database, expected_inventory) == first_inventory
    assert {
        relative: _sha256(destination / relative) for relative in market_hashes
    } == first_market_hashes
