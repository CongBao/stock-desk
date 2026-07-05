import sys
import time
from collections.abc import Sequence
import signal
import subprocess

import pytest

from scripts import dev


def test_supervisor_returns_child_failure_and_stops_siblings_quickly() -> None:
    started_at = time.monotonic()

    return_code = dev.supervise(
        (
            (sys.executable, "-c", "import time; time.sleep(30)"),
            (sys.executable, "-c", "raise SystemExit(7)"),
        ),
        requested_signal=lambda: None,
        poll_interval=0.01,
        shutdown_timeout=0.5,
    )

    assert return_code == 7
    assert time.monotonic() - started_at < 3


def test_supervisor_stops_started_child_when_later_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_start = dev._start

    def recording_start(command: Sequence[str]) -> subprocess.Popen[bytes]:
        process = real_start(command)
        started.append(process)
        return process

    monkeypatch.setattr(dev, "_start", recording_start)
    try:
        return_code = dev.supervise(
            (
                (sys.executable, "-c", "import time; time.sleep(30)"),
                ("stock-desk-command-that-does-not-exist",),
            ),
            requested_signal=lambda: None,
            poll_interval=0.01,
            shutdown_timeout=0.5,
        )

        assert return_code == 1
        assert len(started) == 1
        started[0].wait(timeout=1)
    finally:
        for process in started:
            if process.poll() is None:
                dev._signal_process(process, signal.SIGKILL)
                process.wait(timeout=1)
