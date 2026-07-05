import sys
import time
from collections.abc import Sequence
import signal
import subprocess

import pytest
from unittest.mock import Mock

from scripts import dev


def test_windows_graceful_stop_signals_the_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=1234)
    process.poll.return_value = None
    monkeypatch.setattr(dev.os, "name", "nt")

    dev._signal_process(process, signal.SIGTERM)

    process.send_signal.assert_called_once_with(1)


def test_windows_hard_stop_terminates_the_descendant_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=1234)
    process.poll.return_value = None
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(dev.os, "name", "nt")
    monkeypatch.setattr(dev.subprocess, "run", run)

    dev._hard_stop(process, timeout=0.5)

    run.assert_called_once_with(
        ["taskkill", "/PID", "1234", "/T", "/F"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=0.5,
    )


def test_cleanup_race_does_not_escape_and_mask_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = Mock(pid=1234)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(["child"], 0.01)
    monkeypatch.setattr(
        dev,
        "_signal_process",
        Mock(side_effect=ProcessLookupError("already exited")),
    )

    dev._stop_children((process,), shutdown_timeout=0.01)


def test_cleanup_poll_race_does_not_escape_and_mask_the_result() -> None:
    process = Mock(pid=1234)
    process.poll.side_effect = OSError("process disappeared")

    dev._stop_children((process,), shutdown_timeout=0.01)


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
