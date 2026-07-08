"""Verify an installed Stock Desk application without importing project code."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Final
from urllib.error import URLError
from urllib.request import urlopen


STARTUP_TIMEOUT_SECONDS: Final = 90.0


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def _wait_for_health(runtime_record: Path) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if runtime_record.is_file():
            record = _read_json(runtime_record)
            if record.get("host") != "127.0.0.1" or type(record.get("port")) is not int:
                raise RuntimeError("installed runtime record is not private loopback")
            try:
                with urlopen(  # nosec B310
                    f"http://127.0.0.1:{record['port']}/api/health",
                    timeout=1,
                ) as response:
                    health = json.load(response)
                if isinstance(health, dict) and health.get("status") == "ok":
                    return record
            except (OSError, URLError, ValueError):
                pass
        time.sleep(0.1)
    raise RuntimeError("installed Stock Desk did not become healthy")


def _assert_browser_document(record: dict[str, Any]) -> None:
    with urlopen(  # nosec B310
        f"http://127.0.0.1:{record['port']}/",
        timeout=3,
    ) as response:
        document = response.read().decode("utf-8")
    if "<title>stock-desk</title>" not in document:
        raise RuntimeError("installed browser application title is missing")


def _request_clean_shutdown(command: Path, environment: dict[str, str]) -> None:
    completed = subprocess.run(  # noqa: S603
        [os.fspath(command), "--shutdown"],
        cwd=command.parent,
        env=environment,
        check=False,
        timeout=45,
    )
    if completed.returncode != 0:
        raise RuntimeError("installed application did not drain cleanly")


def _verify_frozen_internal_dispatch(
    command: Path,
    environment: dict[str, str],
    work_dir: Path,
) -> None:
    result_path = (work_dir / "akshare-invalid-result.json").resolve()
    akshare = subprocess.run(  # noqa: S603
        [
            os.fspath(command),
            "--internal-akshare-worker",
            "not_allowed",
            "{}",
            os.fspath(result_path),
        ],
        cwd=work_dir,
        env=environment,
        check=False,
        timeout=30,
    )
    if akshare.returncode != 2 or _read_json(result_path) != {
        "status": "invalid_response"
    }:
        raise RuntimeError("frozen AkShare dispatch did not validate internal mode")
    formula = subprocess.run(  # noqa: S603
        [os.fspath(command), "--internal-formula-smoke"],
        cwd=work_dir,
        env=environment,
        check=False,
        timeout=30,
    )
    if formula.returncode != 0:
        raise RuntimeError("frozen Formula multiprocessing smoke failed")


def _start(
    command: Path,
    *,
    environment: dict[str, str],
    unrelated_cwd: Path,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [os.fspath(command), "--no-browser"],
        cwd=unrelated_cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_and_wait(
    command: Path,
    process: subprocess.Popen[bytes],
    environment: dict[str, str],
) -> None:
    _request_clean_shutdown(command, environment)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        raise RuntimeError(
            "installed application required forced termination"
        ) from None


def verify_installed_app(
    command: Path,
    runtime_record: Path,
    *,
    sanitized_path: str,
    fixture_sql: Path | None = None,
) -> None:
    if not command.is_absolute() or not command.is_file():
        raise RuntimeError("installed application command is missing")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "NODE_PATH", "VIRTUAL_ENV"}
    }
    environment["PATH"] = sanitized_path
    runtime_record.parent.mkdir(parents=True, exist_ok=True)
    if fixture_sql is not None:
        data_dir = runtime_record.parent.parent
        database = data_dir / "stock-desk.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.executescript(fixture_sql.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="stock-desk-installed-") as directory:
        unrelated_cwd = Path(directory)
        _verify_frozen_internal_dispatch(command, environment, unrelated_cwd)
        first = _start(command, environment=environment, unrelated_cwd=unrelated_cwd)
        first_record = _wait_for_health(runtime_record)
        _assert_browser_document(first_record)
        data_dir = Path(str(first_record["data_dir"]))
        if fixture_sql is not None:
            with sqlite3.connect(data_dir / "stock-desk.db") as connection:
                fixture_marker = connection.execute(
                    "SELECT release_version FROM distribution_fixture"
                ).fetchone()
                migrated_revision = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
            if fixture_marker != ("0.5.0",) or migrated_revision is None:
                raise RuntimeError("v0.5.0 fixture was not preserved and migrated")
        sentinel = data_dir / "installer-persistence.txt"
        sentinel.write_text("persistent\n", encoding="utf-8")
        _stop_and_wait(command, first, environment)
        if runtime_record.exists():
            raise RuntimeError("clean shutdown left a stale runtime record")

        second = _start(command, environment=environment, unrelated_cwd=unrelated_cwd)
        second_record = _wait_for_health(runtime_record)
        try:
            if (
                Path(str(second_record["data_dir"])) != data_dir
                or not sentinel.is_file()
            ):
                raise RuntimeError("same-version restart did not preserve user data")
            _assert_browser_document(second_record)
        finally:
            _stop_and_wait(command, second, environment)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--runtime-record", type=Path, required=True)
    parser.add_argument("--sanitized-path", required=True)
    parser.add_argument("--fixture-sql", type=Path)
    arguments = parser.parse_args(argv)
    verify_installed_app(
        arguments.command,
        arguments.runtime_record,
        sanitized_path=arguments.sanitized_path,
        fixture_sql=arguments.fixture_sql,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
