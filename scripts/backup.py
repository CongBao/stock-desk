from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from stock_desk.storage.backup import BackupError, create_backup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a verified Stock Desk portable backup.",
    )
    parser.add_argument("destination", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--drain-timeout", type=float, default=30.0)
    parser.add_argument(
        "--include-encrypted-secrets",
        action="store_true",
        help="Include encrypted secret.* rows; the master key is never included.",
    )
    return parser


def _locations(arguments: argparse.Namespace) -> tuple[Path, str]:
    data_dir = Path(
        arguments.data_dir or os.environ.get("STOCK_DESK_DATA_DIR", "data")
    ).resolve()
    database_url = arguments.database_url or os.environ.get(
        "STOCK_DESK_DATABASE_URL",
        f"sqlite:///{data_dir / 'stock-desk.db'}",
    )
    return data_dir, database_url


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    data_dir, database_url = _locations(arguments)
    try:
        result = create_backup(
            database_url=database_url,
            data_dir=data_dir,
            destination=arguments.destination,
            include_encrypted_secrets=arguments.include_encrypted_secrets,
            drain_timeout_seconds=arguments.drain_timeout,
        )
    except (BackupError, OSError, ValueError) as error:
        print(f"Stock Desk backup failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "archive": os.fspath(result.archive),
                "files": len(result.manifest.files),
                "schema_revision": result.manifest.schema_revision,
                "secret_policy": result.manifest.secret_policy,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
