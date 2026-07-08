from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from stock_desk.storage.backup import (
    BackupError,
    recover_interrupted_restore,
    restore_backup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Restore verified Stock Desk owned data components.",
    )
    parser.add_argument("archive", type=Path, nargs="?")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Confirm that every Stock Desk process using the destination is stopped.",
    )
    parser.add_argument(
        "--recover-only",
        action="store_true",
        help="Recover an interrupted journal without starting another restore.",
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
    parser = _parser()
    arguments = parser.parse_args(argv)
    data_dir, database_url = _locations(arguments)
    try:
        if arguments.recover_only:
            if arguments.archive is not None:
                parser.error("archive cannot be combined with --recover-only")
            recovered = recover_interrupted_restore(data_dir=data_dir)
            print(json.dumps({"recovered": recovered}, separators=(",", ":")))
            return 0
        if arguments.archive is None:
            parser.error("archive is required unless --recover-only is used")
        result = restore_backup(
            archive=arguments.archive,
            database_url=database_url,
            data_dir=data_dir,
            offline=arguments.offline,
        )
    except (BackupError, OSError, ValueError) as error:
        print(f"Stock Desk restore failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "database": os.fspath(result.database),
                "market": os.fspath(result.market) if result.market else None,
                "recovery_archive": (
                    os.fspath(result.recovery_archive)
                    if result.recovery_archive
                    else None
                ),
                "schema_revision": result.manifest.schema_revision,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
