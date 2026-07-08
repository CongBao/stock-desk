"""Regenerate upgrade fixtures by running the exact tagged release software."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


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
    "analysis_report",
)


def _run(*arguments: str, cwd: Path) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _complete_fixture_task(tasks: object, tag: str) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed-one", choices=TAGS)
    parser.add_argument("--commit")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.seed_one is not None:
        if args.commit is None or args.destination is None:
            parser.error("--seed-one requires --commit and --destination")
        sys.path.insert(0, str(Path.cwd()))
        _seed_one(args.seed_one, args.destination, args.commit)
        return 0
    if args.repo is None or args.output is None:
        parser.error("generation requires --repo and --output")
    generate(args.repo.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
