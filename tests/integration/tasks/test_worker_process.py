import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from stock_desk.tasks.repository import TaskRepository


def test_worker_process_completes_persisted_demo_task_and_stops_cleanly(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'worker-process.db'}"
    creator = TaskRepository.open(database_url)
    try:
        created = creator.create("demo.double", {"value": 21})
    finally:
        creator.close()

    observer = TaskRepository.open(database_url)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["STOCK_DESK_DATABASE_URL"] = database_url
    process = subprocess.Popen(
        [sys.executable, "-m", "stock_desk.tasks.worker"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout = ""
    stderr = ""
    try:
        deadline = time.monotonic() + 10
        completed = observer.get(created.id)
        while completed.status not in {"succeeded", "failed", "cancelled"}:
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Worker did not complete demo task in time")
            time.sleep(0.05)
            completed = observer.get(created.id)

        assert completed.status == "succeeded"
        assert completed.result == {"value": 42}

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, (stdout, stderr)
        assert "Stock Desk task worker ready" in stderr
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        observer.close()
