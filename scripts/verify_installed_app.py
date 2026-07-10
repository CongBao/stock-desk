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
from typing import Any, Final, IO
from urllib.error import URLError
from urllib.request import urlopen


STARTUP_TIMEOUT_SECONDS: Final = 90.0
V050_SCHEMA_REVISION: Final = "0009_analysis_model_configs"
CURRENT_SCHEMA_REVISION: Final = "0012_windows_market_payload"
DISTRIBUTION_TASK_ID: Final = "00000000-0000-4000-8000-000000000500"
_EXPECTED_DISTRIBUTION_TASK: Final = (
    "distribution.fixture",
    "succeeded",
    '{"input":21}',
    '{"output":42}',
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def _wait_for_health(
    runtime_record: Path,
    process: subprocess.Popen[bytes],
) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                "installed Stock Desk exited before becoming healthy "
                f"with code {return_code}"
            )
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


def _terminate_windows_tree(process: subprocess.Popen[bytes]) -> None:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    taskkill = system_root / "System32" / "taskkill.exe"
    subprocess.run(  # noqa: S603 -- fixed system executable and numeric PID
        [os.fspath(taskkill), "/PID", str(process.pid), "/T", "/F"],
        check=False,
        capture_output=True,
        timeout=15,
    )


def _terminate_failed_start(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            _terminate_windows_tree(process)
        else:
            process.terminate()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


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
        raise RuntimeError("frozen market storage and Formula smoke failed")


def _start(
    command: Path,
    *,
    environment: dict[str, str],
    unrelated_cwd: Path,
    diagnostic_log: Path | None = None,
) -> subprocess.Popen[bytes]:
    output_handle: IO[bytes] | None = None
    output: int | IO[bytes] = subprocess.DEVNULL
    if diagnostic_log is not None:
        diagnostic_log.parent.mkdir(parents=True, exist_ok=True)
        output_handle = diagnostic_log.open("ab")  # noqa: SIM115
        output = output_handle
    try:
        return subprocess.Popen(  # noqa: S603
            [os.fspath(command), "--no-browser"],
            cwd=unrelated_cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    finally:
        if output_handle is not None:
            output_handle.close()


def _read_distribution_fixture(database: Path) -> tuple[str, tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        revisions = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
        task = connection.execute(
            "SELECT kind, status, payload_json, result_json FROM task_run WHERE id = ?",
            (DISTRIBUTION_TASK_ID,),
        ).fetchone()
    if len(revisions) != 1:
        raise RuntimeError("distribution fixture has an ambiguous schema revision")
    if task is None:
        raise RuntimeError("representative v0.5.0 data was not preserved")
    return str(revisions[0][0]), task


def _assert_historical_fixture(database: Path) -> None:
    revision, task = _read_distribution_fixture(database)
    if revision != V050_SCHEMA_REVISION:
        raise RuntimeError("v0.5.0 fixture schema revision is not authentic")
    if task != _EXPECTED_DISTRIBUTION_TASK:
        raise RuntimeError("representative v0.5.0 fixture data is invalid")


def _assert_migrated_fixture(database: Path) -> None:
    revision, task = _read_distribution_fixture(database)
    if revision != CURRENT_SCHEMA_REVISION:
        raise RuntimeError("installed database schema revision is not current")
    if task != _EXPECTED_DISTRIBUTION_TASK:
        raise RuntimeError("representative v0.5.0 data was not preserved")


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
    diagnostic_dir: Path | None = None,
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
        _assert_historical_fixture(database)
    with tempfile.TemporaryDirectory(prefix="stock-desk-installed-") as directory:
        unrelated_cwd = Path(directory)
        _verify_frozen_internal_dispatch(command, environment, unrelated_cwd)
        first = _start(
            command,
            environment=environment,
            unrelated_cwd=unrelated_cwd,
            diagnostic_log=(
                diagnostic_dir / "first-start.log" if diagnostic_dir else None
            ),
        )
        try:
            first_record = _wait_for_health(runtime_record, first)
            _assert_browser_document(first_record)
            data_dir = Path(str(first_record["data_dir"]))
            if fixture_sql is not None:
                _assert_migrated_fixture(data_dir / "stock-desk.db")
            sentinel = data_dir / "installer-persistence.txt"
            sentinel.write_text("persistent\n", encoding="utf-8")
            _stop_and_wait(command, first, environment)
        except BaseException:
            _terminate_failed_start(first)
            raise
        if runtime_record.exists():
            raise RuntimeError("clean shutdown left a stale runtime record")

        second = _start(
            command,
            environment=environment,
            unrelated_cwd=unrelated_cwd,
            diagnostic_log=(
                diagnostic_dir / "second-start.log" if diagnostic_dir else None
            ),
        )
        try:
            second_record = _wait_for_health(runtime_record, second)
            if (
                Path(str(second_record["data_dir"])) != data_dir
                or not sentinel.is_file()
            ):
                raise RuntimeError("same-version restart did not preserve user data")
            if fixture_sql is not None:
                _assert_migrated_fixture(data_dir / "stock-desk.db")
            _assert_browser_document(second_record)
            _stop_and_wait(command, second, environment)
        except BaseException:
            _terminate_failed_start(second)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--runtime-record", type=Path, required=True)
    parser.add_argument("--sanitized-path", required=True)
    parser.add_argument("--fixture-sql", type=Path)
    parser.add_argument("--diagnostic-dir", type=Path)
    arguments = parser.parse_args(argv)
    verify_installed_app(
        arguments.command,
        arguments.runtime_record,
        sanitized_path=arguments.sanitized_path,
        fixture_sql=arguments.fixture_sql,
        diagnostic_dir=arguments.diagnostic_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
