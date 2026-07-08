from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from stock_desk.storage.backup import BackupBusyError, create_backup
from stock_desk.storage.database import create_engine_for_url, migrate, migration_lock
from stock_desk.tasks.repository import TaskRepository


def _repository(tmp_path: Path) -> tuple[str, TaskRepository]:
    database_url = f"sqlite:///{tmp_path / 'stock-desk.db'}"
    migrate(database_url)
    return database_url, TaskRepository(create_engine_for_url(database_url))


def test_claim_gate_blocks_claims_but_allows_scheduler_enqueue(tmp_path: Path) -> None:
    _url, repository = _repository(tmp_path)
    repository.create("fixture.first", {})
    started = threading.Event()

    def claim() -> object:
        started.set()
        return repository.claim_next("blocked-worker")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with repository.hold_claim_gate(timeout_seconds=1.0):
            future = pool.submit(claim)
            assert started.wait(timeout=1)
            time.sleep(0.05)
            assert not future.done()
            scheduled = repository.create("market.update", {"scheduled": True})
        assert scheduled.status == "queued"
        claimed = future.result(timeout=1)
    assert claimed is not None
    repository.close()


def test_backup_drain_timeout_releases_claim_gate(tmp_path: Path) -> None:
    database_url, repository = _repository(tmp_path)
    repository.create("fixture.running", {})
    running = repository.claim_next("running-worker")
    assert running is not None
    repository.create("fixture.queued", {})

    with pytest.raises(BackupBusyError, match="running tasks"):
        create_backup(
            database_url=database_url,
            data_dir=tmp_path,
            destination=tmp_path / "busy.stockdesk-backup",
            drain_timeout_seconds=0.05,
            drain_poll_seconds=0.01,
        )

    repository.complete(running.id, {"done": True})
    assert repository.claim_next("after-timeout") is not None
    assert not (tmp_path / "busy.stockdesk-backup").exists()
    repository.close()


def test_claim_gate_blocks_claims_in_another_process(tmp_path: Path) -> None:
    database_url, repository = _repository(tmp_path)
    repository.create("fixture.cross-process", {})
    started = tmp_path / "claim-started"
    child_code = """
from pathlib import Path
import sys

from stock_desk.storage.database import create_engine_for_url
from stock_desk.tasks.repository import TaskRepository

repository = TaskRepository(create_engine_for_url(sys.argv[1]))
Path(sys.argv[2]).touch()
claimed = repository.claim_next("child-worker")
print(claimed.id if claimed is not None else "none", flush=True)
repository.close()
"""
    with repository.hold_claim_gate(timeout_seconds=1.0):
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, database_url, str(started)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        time.sleep(0.05)
        assert process.poll() is None
    stdout, stderr = process.communicate(timeout=2)

    assert process.returncode == 0, stderr
    assert stdout.strip() != "none"
    repository.close()


def test_backup_times_out_on_migration_lock_without_publishing(tmp_path: Path) -> None:
    database_url, repository = _repository(tmp_path)
    destination = tmp_path / "migration-busy.stockdesk-backup"

    with migration_lock(database_url, timeout_seconds=1.0):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                create_backup,
                database_url=database_url,
                data_dir=tmp_path,
                destination=destination,
                drain_timeout_seconds=0.05,
            )
            with pytest.raises(BackupBusyError, match="consistency locks"):
                future.result(timeout=1)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".migration-busy.stockdesk-backup.*.tmp"))
    repository.close()
