from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from sqlalchemy import text

import stock_desk.storage.backup as backup_module
from stock_desk.storage.backup import (
    BackupValidationError,
    create_backup,
    restore_backup,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from tests.fixtures.releases import generate_tagged_fixtures


ROOT = Path(__file__).resolve().parents[2]
RELEASE_FIXTURES = ROOT / "tests" / "fixtures" / "releases"
RELEASE_TAGS = ("v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0", "v0.5.0")
RELEASE_COMMITS = {
    "v0.1.0": "05b379b7984268e34859614a771082e49140632b",
    "v0.2.0": "f1727c67f9db4a068ed33f6751fa904f88a43f59",
    "v0.3.0": "7ba36181f77fe7a805e14ec82607f10d13daf3b0",
    "v0.4.0": "8a97dae9e59109b18f08f297d9d1a7d43bb72bc7",
    "v0.5.0": "525c1e50cccd87c534ac44ae8f6a29c743dbfc03",
}
RELEASE_EXPORT_DIGESTS = {
    "v0.1.0": "sha256:d28d99af6a0b1e249a15f143e4246d2ff56949bbda69ce94bac1860705739050",
    "v0.2.0": "sha256:d527482ae763a736dbb990681bfc37770721ba7558b7a4d16fc2f481c168b8cd",
    "v0.3.0": "sha256:25d0fb76a1976657e95a12ce95cf492d85a0e6850bb95a2ba9761c390fd6ae82",
    "v0.4.0": "sha256:3bd848d8d59cece6a16c0876e80c33a1d7ba692bdb301334076bd8707ae8fe47",
    "v0.5.0": "sha256:965249b81002b0fbab4e7fbd98a7f31130176579522cebf4d52e589f5eeb12ba",
}
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


def test_tagged_fixture_provenance_is_bound_to_generator_and_export() -> None:
    generator_digest = _sha256(Path(generate_tagged_fixtures.__file__))
    for tag in RELEASE_TAGS:
        manifest = _manifest(tag)
        database = RELEASE_FIXTURES / tag / "stock-desk.db"
        assert manifest["tag_commit"] == RELEASE_COMMITS[tag]
        assert manifest["generator_sha256"] == generator_digest
        assert manifest["canonical_export_sha256"] == RELEASE_EXPORT_DIGESTS[tag]
        assert (
            generate_tagged_fixtures.canonical_export_sha256(database)
            == (RELEASE_EXPORT_DIGESTS[tag])
        )


def test_canonical_export_normalizes_generated_identifiers_and_clocks(
    tmp_path: Path,
) -> None:
    digests: list[str] = []
    for ordinal in range(2):
        database = tmp_path / f"generated-{ordinal}.db"
        url = f"sqlite:///{database}"
        migrate(url)
        repository = TaskRepository(create_engine_for_url(url))
        task = repository.create("fixture.reproducible", {"stable": True})
        claimed = repository.claim_next("fixture-generator")
        assert claimed is not None and claimed.id == task.id
        repository.complete(task.id, {"stable": True})
        repository.close()
        digests.append(generate_tagged_fixtures.canonical_export_sha256(database))

    assert digests[0] == digests[1]


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
    assert manifest["schema_version"] == "stock-desk-tagged-release-fixture-v1"
    assert manifest["tag"] == tag
    assert manifest["tag_commit"] == RELEASE_COMMITS[tag]
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


@pytest.mark.parametrize("tag", RELEASE_TAGS)
def test_compatible_backup_from_tagged_fixture_restores_on_current_release(
    tag: str,
    tmp_path: Path,
) -> None:
    fixture = tmp_path / f"{tag}-source"
    shutil.copytree(RELEASE_FIXTURES / tag, fixture)
    source_url = f"sqlite:///{fixture / 'stock-desk.db'}"
    archive = tmp_path / f"{tag}.stockdesk-backup"

    backed_up = create_backup(
        database_url=source_url,
        data_dir=fixture,
        destination=archive,
    )
    restored = tmp_path / f"{tag}-restored"
    restored.mkdir(mode=0o700)
    result = restore_backup(
        archive=archive,
        database_url=f"sqlite:///{restored / 'stock-desk.db'}",
        data_dir=restored,
    )

    assert result.manifest == backed_up.manifest
    with sqlite3.connect(restored / "stock-desk.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (HEAD_REVISION,)
    fixture_manifest = _manifest(tag)
    market_hashes = fixture_manifest["market_files"]
    assert isinstance(market_hashes, dict)
    assert {
        relative: _sha256(restored / relative) for relative in market_hashes
    } == market_hashes


@pytest.mark.parametrize(
    "mutation",
    (
        "DROP TABLE formula_draft",
        "ALTER TABLE app_setting DROP COLUMN encrypted_value",
        "DROP INDEX ix_formula_version_formula",
    ),
)
def test_current_schema_validation_rejects_missing_required_shape(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / "incomplete-current.db"
    url = f"sqlite:///{database}"
    migrate(url)
    with sqlite3.connect(database) as connection:
        source_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        connection.execute(mutation)

    with pytest.raises(BackupValidationError, match="current schema"):
        backup_module._validate_current_schema(database, source_tables=source_tables)


def test_current_schema_validation_rejects_missing_check_constraint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-check.db"
    url = f"sqlite:///{database}"
    migrate(url)
    with sqlite3.connect(database) as connection:
        source_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            PRAGMA legacy_alter_table=ON;
            ALTER TABLE formula RENAME TO formula_with_checks;
            CREATE TABLE formula (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                name VARCHAR(64) NOT NULL,
                formula_type VARCHAR(16) NOT NULL,
                placement VARCHAR(16) NOT NULL,
                latest_version INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            DROP TABLE formula_with_checks;
            """
        )

    with pytest.raises(BackupValidationError, match="constraints are incomplete"):
        backup_module._validate_current_schema(database, source_tables=source_tables)


def test_current_schema_validation_requires_new_business_tables_to_be_empty(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nonempty-new-table.db"
    url = f"sqlite:///{database}"
    migrate(url, "0002_task_observability")
    with sqlite3.connect(database) as connection:
        source_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO formula "
                    "(id, name, formula_type, placement, latest_version, "
                    "created_at, updated_at) VALUES "
                    "('migration-created', 'unexpected', 'indicator', "
                    "'main', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(BackupValidationError, match="new business table"):
        backup_module._validate_current_schema(database, source_tables=source_tables)


def test_current_schema_validation_requires_migration_head(tmp_path: Path) -> None:
    database = tmp_path / "wrong-revision.db"
    url = f"sqlite:///{database}"
    migrate(url)
    with sqlite3.connect(database) as connection:
        source_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        connection.execute(
            "UPDATE alembic_version SET version_num = '0009_analysis_model_configs'"
        )

    with pytest.raises(BackupValidationError, match="head revision"):
        backup_module._validate_current_schema(database, source_tables=source_tables)
