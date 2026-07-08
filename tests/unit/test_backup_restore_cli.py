from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import backup as backup_cli
from scripts import restore as restore_cli
from stock_desk.storage.backup import inspect_backup
from stock_desk.storage.database import migrate


def test_backup_and_restore_cli_round_trip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source_url = f"sqlite:///{source / 'stock-desk.db'}"
    migrate(source_url)
    archive = tmp_path / "cli.stockdesk-backup"

    assert (
        backup_cli.main(
            [
                str(archive),
                "--data-dir",
                str(source),
                "--database-url",
                source_url,
            ]
        )
        == 0
    )
    backup_output = json.loads(capsys.readouterr().out)
    assert backup_output["secret_policy"] == "omitted"

    destination = tmp_path / "destination"
    destination_url = f"sqlite:///{destination / 'stock-desk.db'}"
    assert (
        restore_cli.main(
            [
                str(archive),
                "--data-dir",
                str(destination),
                "--database-url",
                destination_url,
            ]
        )
        == 0
    )
    restore_output = json.loads(capsys.readouterr().out)
    assert restore_output["database"] == str(destination / "stock-desk.db")


def test_restore_cli_recover_only_is_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert restore_cli.main(["--data-dir", str(tmp_path), "--recover-only"]) == 0
    assert json.loads(capsys.readouterr().out) == {"recovered": False}


def test_cli_encrypted_secret_mode_is_explicit(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    url = f"sqlite:///{data_dir / 'stock-desk.db'}"
    migrate(url)
    archive = tmp_path / "recovery.stockdesk-backup"

    assert (
        backup_cli.main(
            [
                str(archive),
                "--data-dir",
                str(data_dir),
                "--database-url",
                url,
                "--include-encrypted-secrets",
            ]
        )
        == 0
    )
    assert inspect_backup(archive).secret_policy == "encrypted_included"
