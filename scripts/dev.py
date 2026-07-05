from __future__ import annotations

from collections.abc import Callable, Sequence
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parent.parent
POLL_INTERVAL_SECONDS = 0.2
SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _commands() -> tuple[tuple[str, ...], ...]:
    return (
        (
            sys.executable,
            "-m",
            "uvicorn",
            "stock_desk.main:app",
            "--reload",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ),
        (sys.executable, "-m", "stock_desk.tasks.worker"),
        ("pnpm", "--dir", "web", "dev"),
    )


def _start(command: Sequence[str]) -> subprocess.Popen[bytes]:
    if os.name == "posix":
        return subprocess.Popen(  # noqa: S603
            command,
            cwd=REPO_ROOT,
            start_new_session=True,
        )
    if os.name == "nt":
        return subprocess.Popen(  # noqa: S603
            command,
            cwd=REPO_ROOT,
            creationflags=0x00000200,
        )
    return subprocess.Popen(command, cwd=REPO_ROOT)  # noqa: S603


def _signal_process(process: subprocess.Popen[bytes], signum: int) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signum)
    else:
        process.send_signal(signum)


def _stop_children(
    processes: Sequence[subprocess.Popen[bytes]],
    *,
    shutdown_timeout: float,
) -> None:
    for process in processes:
        _signal_process(process, signal.SIGTERM)

    deadline = time.monotonic() + shutdown_timeout
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            hard_stop = signal.SIGKILL if os.name == "posix" else signal.SIGTERM
            _signal_process(process, hard_stop)

    for process in processes:
        if process.poll() is None:
            process.wait()


def supervise(
    commands: Sequence[Sequence[str]],
    *,
    requested_signal: Callable[[], int | None],
    poll_interval: float = POLL_INTERVAL_SECONDS,
    shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
) -> int:
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for command in commands:
            processes.append(_start(command))
        while (signum := requested_signal()) is None:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    return return_code if return_code != 0 else 1
            time.sleep(poll_interval)
        return 128 + signum
    except OSError as error:
        print(f"Unable to start development services: {error}", file=sys.stderr)
        return 1
    finally:
        _stop_children(processes, shutdown_timeout=shutdown_timeout)


def main() -> int:
    received_signal: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        return supervise(_commands(), requested_signal=lambda: received_signal)
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
