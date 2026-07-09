from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading

from sqlalchemy import inspect, text

from stock_desk.storage.database import (
    create_engine_for_url,
    downgrade,
    migrate,
)


HEAD_REVISION = "0011_worker_heartbeat"
CORE_TABLES = {
    "app_setting",
    "formula",
    "formula_draft",
    "formula_version",
    "task_event",
    "task_run",
    "task_worker_heartbeat",
    "analysis_model_config",
}


def _assert_database_at_head(url: str) -> None:
    engine = create_engine_for_url(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == [HEAD_REVISION]
        assert CORE_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_concurrent_thread_upgrades_serialize_to_one_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'thread-upgrade.db'}"
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def migrate_once(_worker: int) -> None:
        barrier.wait(timeout=10)
        migrate(url)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(migrate_once, range(worker_count)))

    _assert_database_at_head(url)
    assert (tmp_path / "thread-upgrade.db.migrate.lock").is_file()


def test_concurrent_process_upgrades_serialize_to_one_head(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'process-upgrade.db'}"
    start_signal = tmp_path / "start"
    child_code = """
from pathlib import Path
import sys
import time

from stock_desk.storage.database import migrate

while not Path(sys.argv[2]).exists():
    time.sleep(0.01)
migrate(sys.argv[1])
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child_code, url, str(start_signal)],
            cwd=tmp_path,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _worker in range(6)
    ]
    start_signal.touch()

    outcomes: list[tuple[int, str, str]] = []
    for process in processes:
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            stderr = f"timed out after 20 seconds\n{stderr}"
        outcomes.append((process.returncode, stdout, stderr))

    failures = [
        f"process {index}: exit={returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        for index, (returncode, stdout, stderr) in enumerate(outcomes)
        if returncode != 0
    ]
    assert failures == []
    _assert_database_at_head(url)


def test_concurrent_thread_downgrades_serialize_to_base(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'thread-downgrade.db'}"
    migrate(url)
    worker_count = 6
    barrier = threading.Barrier(worker_count)

    def downgrade_once(_worker: int) -> None:
        barrier.wait(timeout=10)
        downgrade(url)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        list(executor.map(downgrade_once, range(worker_count)))

    engine = create_engine_for_url(url)
    try:
        assert CORE_TABLES.isdisjoint(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM alembic_version")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()
