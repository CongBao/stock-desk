"""Regenerate upgrade fixtures by running the exact tagged release software."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any


TAGS = ("v0.1.0", "v0.2.0", "v0.3.0", "v0.4.0", "v0.5.0")
INVENTORY_TABLES = (
    "task_run",
    "formula",
    "formula_version",
    "backtest_run",
    "backtest_symbol",
    "backtest_trade",
    "backtest_aggregate_metric",
    "backtest_group_metric",
    "analysis_run",
    "analysis_stage",
    "analysis_attempt",
    "analysis_report",
)
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[ T][0-9]{2}:[0-9]{2}:[0-9]{2})"
)


def _run(*arguments: str, cwd: Path) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _normalized_export_value(column: str, value: object) -> object:
    if value is None or type(value) in {bool, int, float}:
        return value
    if type(value) is bytes:
        return {"bytes_sha256": f"sha256:{hashlib.sha256(value).hexdigest()}"}
    if type(value) is not str:
        raise TypeError("fixture export contains an unsupported SQLite value")
    if column.endswith("_json"):
        return _normalized_export_json(json.loads(value))
    if UUID_PATTERN.fullmatch(value):
        return "<uuid>"
    if SHA256_PATTERN.fullmatch(value):
        return "<sha256>"
    if TIMESTAMP_PATTERN.match(value):
        return "<timestamp>"
    return value


def _normalized_export_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _normalized_export_json(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalized_export_json(item) for item in value]
    if isinstance(value, str):
        if UUID_PATTERN.fullmatch(value):
            return "<uuid>"
        if SHA256_PATTERN.fullmatch(value):
            return "<sha256>"
        if TIMESTAMP_PATTERN.match(value):
            return "<timestamp>"
    return value


def canonical_export_sha256(database: Path) -> str:
    """Digest stable logical content while normalizing generated identities."""
    export: dict[str, object] = {}
    uri = f"file:{database.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in INVENTORY_TABLES:
            if table not in existing:
                continue
            columns = tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            quoted = ", ".join(f'"{column}"' for column in columns)
            query = f'SELECT {quoted} FROM "{table}"'
            rows = [
                [
                    _normalized_export_value(column, value)
                    for column, value in zip(columns, row, strict=True)
                ]
                for row in connection.execute(query)
            ]
            rows.sort(
                key=lambda row: json.dumps(
                    row,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            export[table] = {"columns": columns, "rows": rows}
    encoded = json.dumps(
        export,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _complete_fixture_task(tasks: Any, tag: str) -> None:
    task = tasks.create("fixture.release", {"tag": tag})
    claimed = tasks.claim_next("fixture-generator")
    if claimed is None or claimed.id != task.id:
        raise RuntimeError("fixture task was not claimed")
    tasks.complete(task.id, {"tag": tag, "stable": True})


def _seed_pre_backtest(tag: str, destination: Path) -> Path:
    from stock_desk.storage.database import create_engine_for_url, migrate
    from stock_desk.tasks.repository import TaskRepository

    database = destination / "stock-desk.db"
    url = f"sqlite:///{database}"
    migrate(url)
    engine = create_engine_for_url(url)
    try:
        tasks = TaskRepository(engine)
        _complete_fixture_task(tasks, tag)
        if tag >= "v0.2.0":
            from stock_desk.market.lake import MarketLake
            from tests.integration.market.lake_test_helpers import routed_daily_bars

            lake = MarketLake(engine=engine, root=(destination / "market").resolve())
            lake.write(
                routed_daily_bars(
                    (
                        date(2024, 1, 2),
                        date(2024, 1, 3),
                        date(2024, 1, 4),
                        date(2024, 1, 5),
                    )
                )
            )
        if tag >= "v0.3.0":
            from stock_desk.formula.repository import FormulaRepository

            FormulaRepository(engine).create(
                "Tagged fixture formula",
                "trading",
                "BUY:CROSS(C,REF(C,1));SELL:CROSS(REF(C,1),C);",
                {},
                placement="subchart",
            )
    finally:
        engine.dispose()
    return database


def _seed_backtest(tag: str, destination: Path) -> Path:
    from stock_desk.market.types import Period
    from tests.backtest_test_helpers import (
        BacktestHarness,
        WAVE_FORMULA,
        local_time,
        weekday_range,
    )

    days = weekday_range(date(2024, 1, 1), date(2024, 5, 1))
    with BacktestHarness.create(destination) as harness:
        harness.seed_instruments("600000.SH")
        harness.seed_symbol("600000.SH", Period.DAY, days)
        version = harness.create_formula("Tagged fixture wave", WAVE_FORMULA)
        completed = harness.run_single(
            version.id,
            symbol="600000.SH",
            period=Period.DAY,
            scoring_start=local_time(days[5]),
            scoring_end=local_time(days[-1]) + timedelta(days=1),
        )
        if completed.run.status != "succeeded":
            raise RuntimeError("tagged fixture backtest did not succeed")
        _complete_fixture_task(harness.tasks, tag)
    source = destination / "backtest-harness.db"
    database = destination / "stock-desk.db"
    source.replace(database)
    return database


def _seed_analysis(database: Path) -> None:
    from stock_desk.analysis.repository import AnalysisRepository
    from stock_desk.analysis.retry import RetryPolicy
    from stock_desk.storage.database import create_engine_for_url

    now = datetime(2025, 7, 6, 9, tzinfo=timezone.utc)
    engine = create_engine_for_url(f"sqlite:///{database}")
    try:
        repository = AnalysisRepository(engine)
        repository.enqueue_run(
            symbol="600000.SH",
            retry_policy=RetryPolicy(max_retries=0),
            now=now,
        )
    finally:
        engine.dispose()


def _inventory(database: Path) -> dict[str, list[list[str]]]:
    inventory: dict[str, list[list[str]]] = {}
    with sqlite3.connect(database) as connection:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in INVENTORY_TABLES:
            if table not in existing:
                continue
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if int(row[5]) > 0
            ]
            order = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(
                f'SELECT {order} FROM "{table}" ORDER BY {order}'
            ).fetchall()
            inventory[table] = [[str(value) for value in row] for row in rows]
    return inventory


def _seed_one(tag: str, destination: Path, commit: str) -> None:
    destination.mkdir(mode=0o700, parents=True)
    database = (
        _seed_backtest(tag, destination)
        if tag >= "v0.4.0"
        else _seed_pre_backtest(tag, destination)
    )
    if tag == "v0.5.0":
        _seed_analysis(database)
    locks = destination / "market" / ".locks"
    if locks.is_dir():
        shutil.rmtree(locks)
    for lock in destination.glob("*.migrate.lock"):
        lock.unlink()
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    market_root = destination / "market"
    market_files = (
        {
            path.relative_to(destination).as_posix(): _sha256(path)
            for path in sorted(market_root.rglob("*"))
            if path.is_file()
        }
        if market_root.is_dir()
        else {}
    )
    manifest = {
        "schema_version": "stock-desk-tagged-release-fixture-v1",
        "tag": tag,
        "tag_commit": commit,
        "generated_by": "checked-out-tag-software",
        "generator_sha256": _sha256(Path(__file__).resolve()),
        "canonical_export_sha256": canonical_export_sha256(database),
        "schema_revision": revision,
        "database_sha256": _sha256(database),
        "logical_inventory": _inventory(database),
        "market_files": market_files,
    }
    (destination / "manifest.json").write_text(
        json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def generate(repo: Path, output: Path) -> None:
    script = Path(__file__).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for tag in TAGS:
        commit = subprocess.run(
            ("git", "rev-parse", f"{tag}^{{commit}}"),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        target = output / tag
        if target.exists():
            shutil.rmtree(target)
        with tempfile.TemporaryDirectory(prefix=f"stock-desk-{tag}-") as raw:
            checkout = Path(raw) / "checkout"
            _run("git", "worktree", "add", "--detach", str(checkout), tag, cwd=repo)
            try:
                _run("uv", "sync", "--frozen", cwd=checkout)
                _run(
                    "uv",
                    "run",
                    "python",
                    str(script),
                    "--seed-one",
                    tag,
                    "--commit",
                    commit,
                    "--destination",
                    str(target),
                    cwd=checkout,
                )
            finally:
                _run("git", "worktree", "remove", "--force", str(checkout), cwd=repo)


def refresh_provenance(output: Path) -> None:
    generator_digest = _sha256(Path(__file__).resolve())
    for tag in TAGS:
        target = output / tag
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["generator_sha256"] = generator_digest
        manifest["canonical_export_sha256"] = canonical_export_sha256(
            target / "stock-desk.db"
        )
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed-one", choices=TAGS)
    parser.add_argument("--commit")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--refresh-provenance", action="store_true")
    args = parser.parse_args()
    if args.seed_one is not None:
        if args.commit is None or args.destination is None:
            parser.error("--seed-one requires --commit and --destination")
        sys.path.insert(0, str(Path.cwd()))
        _seed_one(args.seed_one, args.destination, args.commit)
        return 0
    if args.refresh_provenance:
        if args.output is None:
            parser.error("--refresh-provenance requires --output")
        refresh_provenance(args.output.resolve())
        return 0
    if args.repo is None or args.output is None:
        parser.error("generation requires --repo and --output")
    generate(args.repo.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
