from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

from filelock import FileLock
import pytest

from stock_desk.market.lake import MarketLake, MarketLakeCorruptionError
from tests.integration.market.lake_read_test_helpers import open_catalog_engine
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _wait_for_path(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"child exited before signal: {process.returncode=} {stdout=} {stderr=}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(f"timed out waiting for child: {stdout=} {stderr=}")
        time.sleep(0.01)


@pytest.mark.parametrize("operation", ["read", "write"])
def test_operation_rejects_dataset_lock_leaf_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "market"
    routed = routed_daily_bars((date(2024, 1, 2),))
    with open_catalog_engine(tmp_path) as engine:
        lake = MarketLake(engine=engine, root=root)
        stored = lake.write(routed)
        entered = Event()
        proceed = Event()
        if operation == "read":
            original = lake._load_catalog_snapshot

            def pause(*args: object, **kwargs: object) -> object:
                entered.set()
                assert proceed.wait(timeout=5)
                return original(*args, **kwargs)

            monkeypatch.setattr(lake, "_load_catalog_snapshot", pause)
        else:
            original = lake._write_locked

            def pause(*args: object, **kwargs: object) -> object:
                entered.set()
                assert proceed.wait(timeout=5)
                return original(*args, **kwargs)

            monkeypatch.setattr(lake, "_write_locked", pause)

        def invoke() -> object:
            if operation == "read":
                return lake.read(stored.manifest_record_id)
            return lake.write(routed)

        digest = stored.dataset_version.removeprefix("sha256:")
        lock_path = root / ".locks" / f"{digest}.lock"
        displaced = root / ".locks" / f"{digest}.displaced"
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(invoke)
            assert entered.wait(timeout=5)
            os.replace(lock_path, displaced)
            replacement = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.close(replacement)
            contender = FileLock(lock_path)
            contender.acquire(timeout=0)
            try:
                assert contender.is_locked
            finally:
                contender.release()
                proceed.set()

            with pytest.raises(MarketLakeCorruptionError, match="lock"):
                future.result(timeout=5)


@pytest.mark.parametrize("second_operation", ["read", "write"])
def test_second_market_lake_waits_for_cross_process_namespace_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_operation: str,
) -> None:
    root = tmp_path / "market"
    database_path = tmp_path / "read-catalog.db"
    child_entered = tmp_path / "child-entered"
    release_child = tmp_path / "release-child"
    first = routed_daily_bars((date(2024, 1, 2),))
    second = routed_daily_bars((date(2024, 1, 2),), volume_delta=-1)
    child_code = """
from pathlib import Path
import sys
import time

from stock_desk.market.lake import MarketLake
from stock_desk.storage.database import create_engine_for_url

engine = create_engine_for_url(f"sqlite:///{sys.argv[1]}")
try:
    lake = MarketLake(engine=engine, root=Path(sys.argv[2]))
    original = lake._load_catalog_snapshot
    def pause(record_id, dataset_version):
        Path(sys.argv[4]).touch()
        while not Path(sys.argv[5]).exists():
            time.sleep(0.01)
        return original(record_id, dataset_version)
    lake._load_catalog_snapshot = pause
    lake.read(sys.argv[3])
finally:
    engine.dispose()
"""
    with open_catalog_engine(tmp_path) as engine:
        first_lake = MarketLake(engine=engine, root=root)
        first_stored = first_lake.write(first)
        second_lake = MarketLake(engine=engine, root=root)
        second_stored = second_lake.write(second)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(database_path),
                str(root),
                first_stored.manifest_record_id,
                str(child_entered),
                str(release_child),
            ],
            cwd=Path(__file__).parents[3],
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        _wait_for_path(child_entered, process)
        second_entered = Event()
        if second_operation == "read":
            original = second_lake._read_snapshot

            def observe(*args: object, **kwargs: object) -> object:
                second_entered.set()
                return original(*args, **kwargs)

            monkeypatch.setattr(second_lake, "_read_snapshot", observe)
        else:
            original = second_lake._write_locked

            def observe(*args: object, **kwargs: object) -> object:
                second_entered.set()
                return original(*args, **kwargs)

            monkeypatch.setattr(second_lake, "_write_locked", observe)

        def invoke_second() -> object:
            if second_operation == "read":
                return second_lake.read(second_stored.manifest_record_id)
            return second_lake.write(second)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(invoke_second)
            blocked = not second_entered.wait(timeout=0.3)
            release_child.touch()
            future.result(timeout=10)
        stdout, stderr = process.communicate(timeout=10)

        assert process.returncode == 0, (stdout, stderr)
        assert blocked
        assert second_entered.is_set()
