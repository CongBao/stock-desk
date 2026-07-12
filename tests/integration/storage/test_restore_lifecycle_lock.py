from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import zipfile

from fastapi.testclient import TestClient
from filelock import Timeout as FileLockTimeout
import pytest

from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.storage.backup import BackupBusyError, create_backup, restore_backup
from stock_desk.storage.database import (
    create_engine_for_url,
    migrate,
    migration_lock,
)
from stock_desk.tasks.repository import TaskRepository


def _instance(root: Path) -> str:
    url = f"sqlite:///{root / 'stock-desk.db'}"
    migrate(url)
    return url


def _archive(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source_url = _instance(source)
    archive = tmp_path / "source.stockdesk-backup"
    create_backup(database_url=source_url, data_dir=source, destination=archive)
    return archive


def test_restore_holds_migration_and_claim_gates_through_component_swap(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    destination = tmp_path / "destination"
    destination_url = _instance(destination)
    repository = TaskRepository(create_engine_for_url(destination_url))
    repository.create("fixture.queued", {})
    prepared = threading.Event()
    release = threading.Event()

    def pause(phase: str) -> None:
        if phase == "offline_locked":
            prepared.set()
            assert release.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        restoring = pool.submit(
            restore_backup,
            archive=archive,
            database_url=destination_url,
            data_dir=destination,
            offline=True,
            _phase_hook=pause,
        )
        assert prepared.wait(timeout=3)
        with pytest.raises(FileLockTimeout):
            with migration_lock(destination_url, timeout_seconds=0.05):
                pass
        claiming = pool.submit(repository.claim_next, "late-worker")
        assert not claiming.done()
        release.set()
        restoring.result(timeout=5)
        assert claiming.result(timeout=5) is not None
    repository.close()


def test_restore_refuses_before_backup_when_api_or_worker_is_registered(
    tmp_path: Path,
) -> None:
    from stock_desk.storage.lifecycle import service_lifecycle

    archive = _archive(tmp_path)
    destination = tmp_path / "destination"
    destination_url = _instance(destination)
    with ExitStack() as stack:
        stack.enter_context(service_lifecycle(destination, role="api"))
        stack.enter_context(service_lifecycle(destination, role="worker"))
        with pytest.raises(BackupBusyError, match="service"):
            restore_backup(
                archive=archive,
                database_url=destination_url,
                data_dir=destination,
                offline=True,
            )

    assert not (destination / ".stock-desk-recovery").exists()


def test_expired_leased_task_is_requeued_in_local_recovery_backup(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    destination = tmp_path / "destination"
    destination_url = _instance(destination)
    repository = TaskRepository(create_engine_for_url(destination_url))
    queued = repository.create("backtest.run", {})
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    claimed = repository.claim_next(
        "expired-worker",
        now=old,
        lease_duration=timedelta(seconds=1),
    )
    assert claimed is not None
    repository.close()

    result = restore_backup(
        archive=archive,
        database_url=destination_url,
        data_dir=destination,
        offline=True,
    )

    assert result.recovery_archive is not None
    with zipfile.ZipFile(result.recovery_archive) as bundle:
        recovery_database = tmp_path / "recovery.db"
        recovery_database.write_bytes(bundle.read("database/stock-desk.db"))
    with sqlite3.connect(recovery_database) as connection:
        assert connection.execute(
            "SELECT status, worker_id, claim_token, lease_expires_at "
            "FROM task_run WHERE id = ?",
            (queued.id,),
        ).fetchone() == ("queued", None, None, None)


def test_expired_cancel_requested_task_is_terminal_in_local_recovery_backup(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path)
    destination = tmp_path / "destination"
    destination_url = _instance(destination)
    repository = TaskRepository(create_engine_for_url(destination_url))
    task = repository.create("backtest.run", {})
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    claimed = repository.claim_next(
        "expired-worker",
        now=old,
        lease_duration=timedelta(seconds=1),
    )
    assert claimed is not None
    repository.request_cancel(task.id)
    repository.close()

    result = restore_backup(
        archive=archive,
        database_url=destination_url,
        data_dir=destination,
        offline=True,
    )

    assert result.recovery_archive is not None
    with zipfile.ZipFile(result.recovery_archive) as bundle:
        recovery_database = tmp_path / "cancelled-recovery.db"
        recovery_database.write_bytes(bundle.read("database/stock-desk.db"))
    with sqlite3.connect(recovery_database) as connection:
        restored = connection.execute(
            "SELECT status, finished_at, claim_token, lease_expires_at "
            "FROM task_run WHERE id = ?",
            (task.id,),
        ).fetchone()
    assert restored is not None
    assert restored[0] == "cancelled"
    assert restored[1] is not None
    assert restored[2:] == (None, None)


@pytest.mark.parametrize("operation", ("api", "worker", "recovery"))
def test_restore_lock_rejects_concurrent_service_or_recovery_process(
    tmp_path: Path,
    operation: str,
) -> None:
    from stock_desk.storage.lifecycle import restore_lifecycle

    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    child_code = """
from pathlib import Path
import sys

from stock_desk.storage.backup import RestoreRecoveryRequired, recover_interrupted_restore
from stock_desk.storage.lifecycle import LifecycleBusyError, service_lifecycle

try:
    if sys.argv[2] == "recovery":
        recover_interrupted_restore(data_dir=Path(sys.argv[1]))
    else:
        with service_lifecycle(Path(sys.argv[1]), role=sys.argv[2]):
            pass
except (LifecycleBusyError, RestoreRecoveryRequired):
    raise SystemExit(23)
raise SystemExit(0)
"""
    with restore_lifecycle(data_dir):
        result = subprocess.run(
            [sys.executable, "-c", child_code, str(data_dir), operation],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )

    assert result.returncode == 23, result.stderr


def test_peer_service_startup_waits_for_the_bounded_registration_gate(
    tmp_path: Path,
) -> None:
    from stock_desk.storage.lifecycle import (
        SERVICE_STARTUP_LOCK_TIMEOUT_SECONDS,
        service_lifecycle,
    )

    data_dir = tmp_path / "data"
    first_preflight_started = threading.Event()
    release_first_preflight = threading.Event()

    def hold_first_registration() -> None:
        def preflight() -> None:
            first_preflight_started.set()
            assert release_first_preflight.wait(timeout=2)

        with service_lifecycle(
            data_dir,
            role="api",
            timeout_seconds=SERVICE_STARTUP_LOCK_TIMEOUT_SECONDS,
            preflight=preflight,
        ):
            pass

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(hold_first_registration)
        assert first_preflight_started.wait(timeout=2)
        release = threading.Timer(0.05, release_first_preflight.set)
        release.start()
        try:
            with service_lifecycle(
                data_dir,
                role="worker",
                timeout_seconds=SERVICE_STARTUP_LOCK_TIMEOUT_SECONDS,
            ):
                pass
            first.result(timeout=2)
        finally:
            release.cancel()
            release_first_preflight.set()


def test_live_api_lifespan_blocks_offline_restore(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    destination = tmp_path / "destination"
    destination_url = _instance(destination)
    app = create_app(Settings(data_dir=destination, database_url=destination_url))

    with TestClient(app):
        with pytest.raises(BackupBusyError, match="service"):
            restore_backup(
                archive=archive,
                database_url=destination_url,
                data_dir=destination,
                offline=True,
            )


def test_live_worker_blocks_offline_restore(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    destination = tmp_path / "destination"
    destination_url = _instance(destination)
    runtime = ProductionMarketWorker.open(
        Settings(data_dir=destination, database_url=destination_url),
        worker_id="restore-lock-test-worker",
    )
    try:
        with pytest.raises(BackupBusyError, match="service"):
            restore_backup(
                archive=archive,
                database_url=destination_url,
                data_dir=destination,
                offline=True,
            )
    finally:
        runtime.close()
