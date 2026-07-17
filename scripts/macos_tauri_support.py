"""Shared safety primitives for disposable macOS Tauri test harnesses."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Any


class MacOSTauriSupportError(RuntimeError):
    """A disposable macOS Tauri safety boundary failed closed."""


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    start_time: str
    command: str


def process_table() -> dict[int, ProcessInfo]:
    result = subprocess.run(  # noqa: S603
        ("ps", "-axo", "pid=,ppid=,lstart=,command="),
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    processes: dict[int, ProcessInfo] = {}
    for raw in result.stdout.splitlines():
        fields = raw.strip().split(maxsplit=7)
        if len(fields) != 8:
            continue
        try:
            pid = int(fields[0])
            parent_pid = int(fields[1])
        except ValueError:
            continue
        processes[pid] = ProcessInfo(
            pid,
            parent_pid,
            " ".join(fields[2:7]),
            fields[7],
        )
    return processes


class VerifiedProcessTree:
    """Track exact process identities reached through one verified host ancestry."""

    def __init__(self, root_pid: int, host_path: Path, allowed_root: Path) -> None:
        if root_pid <= 1:
            raise MacOSTauriSupportError("Stock Desk host PID is invalid")
        self._root_pid = root_pid
        self._host_commands = frozenset(
            {os.fspath(host_path), os.path.realpath(host_path)}
        )
        self._allowed_roots = frozenset(
            {os.fspath(allowed_root), os.path.realpath(allowed_root)}
        )
        self._known: dict[int, tuple[str, str, int]] = {}

    def _inside_allowed_root(self, command: str) -> bool:
        return "stock-desk" in command.lower() and any(
            root in command for root in self._allowed_roots
        )

    def observe(self) -> tuple[ProcessInfo, ...]:
        rows = process_table()
        root = rows.get(self._root_pid)
        if root is None:
            return ()
        if root.command not in self._host_commands:
            raise MacOSTauriSupportError("Stock Desk host process identity changed")
        known_root = self._known.get(self._root_pid)
        if known_root is not None and (
            known_root[0] != root.start_time or known_root[1] != root.command
        ):
            raise MacOSTauriSupportError("Stock Desk host process identity changed")
        observed: dict[int, tuple[str, str, int]] = {
            self._root_pid: (root.start_time, root.command, 0)
        }
        pending = {self._root_pid}
        while pending:
            parent_pid = pending.pop()
            parent_depth = observed[parent_pid][2]
            for row in rows.values():
                if row.parent_pid != parent_pid or row.pid in observed:
                    continue
                if not self._inside_allowed_root(row.command):
                    continue
                known = self._known.get(row.pid)
                if known is not None and (
                    known[0] != row.start_time or known[1] != row.command
                ):
                    continue
                observed[row.pid] = (
                    row.start_time,
                    row.command,
                    parent_depth + 1,
                )
                pending.add(row.pid)
        self._known.update(observed)
        return tuple(rows[pid] for pid in sorted(observed) if pid != self._root_pid)

    def sidecar_pid(self) -> int | None:
        candidates = [
            (depth, pid)
            for pid, (_start_time, command, depth) in self._known.items()
            if pid != self._root_pid and "stock-desk-sidecar" in command
        ]
        if not candidates:
            return None
        return min(candidates)[1]

    def terminate(self, timeout_seconds: int = 10) -> None:
        try:
            self.observe()
        except MacOSTauriSupportError:
            # A reused root PID is not authority to discover or signal processes.
            pass
        for pid, (start_time, command, _depth) in sorted(
            self._known.items(), key=lambda item: item[1][2], reverse=True
        ):
            current = process_table().get(pid)
            if (
                current is None
                or current.start_time != start_time
                or current.command != command
            ):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                current = process_table().get(pid)
                if (
                    current is None
                    or current.start_time != start_time
                    or current.command != command
                ):
                    break
                time.sleep(0.05)
            else:
                current = process_table().get(pid)
                if (
                    current is not None
                    and current.start_time == start_time
                    and current.command == command
                ):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.verify_absent()

    def verify_absent(self) -> None:
        rows = process_table()
        remaining = [
            pid
            for pid, (start_time, command, _depth) in self._known.items()
            if pid in rows
            and rows[pid].start_time == start_time
            and rows[pid].command == command
        ]
        if remaining:
            raise MacOSTauriSupportError(
                "verified Stock Desk process remains after cleanup"
            )


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def require_clean_source_identity(
    root: Path, expected: tuple[str, str] | None = None
) -> tuple[str, str]:
    source_sha = git(root, "rev-parse", "HEAD").lower()
    source_tree = git(root, "rev-parse", "HEAD^{tree}").lower()
    if any(
        len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
        for value in (source_sha, source_tree)
    ):
        raise MacOSTauriSupportError("macOS test source identity is invalid")
    if git(root, "status", "--porcelain=v1"):
        raise MacOSTauriSupportError("macOS test requires a clean source tree")
    identity = (source_sha, source_tree)
    if expected is not None and identity != expected:
        raise MacOSTauriSupportError("macOS test source identity changed")
    return identity


def screen_is_locked() -> bool:
    result = subprocess.run(  # noqa: S603
        ("ioreg", "-n", "Root", "-d1"),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return '"CGSSessionScreenIsLocked"=Yes' in result.stdout


def validated_output(root: Path, requested: Path, leaf_name: str) -> Path:
    output = requested.resolve()
    allowed = (root / "test-results").resolve()
    try:
        relative = output.relative_to(allowed)
    except ValueError as error:
        raise MacOSTauriSupportError(
            f"{leaf_name} output must stay under test-results"
        ) from error
    if not relative.parts:
        raise MacOSTauriSupportError(
            f"{leaf_name} output cannot replace the test-results directory"
        )
    return output


def copy_exclusive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    try:
        with (
            source.open("rb") as source_file,
            os.fdopen(descriptor, "wb", closefd=False) as destination_file,
        ):
            shutil.copyfileobj(source_file, destination_file)
            destination_file.flush()
            os.fsync(destination_file.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unregister_bundle(app_path: Path) -> None:
    command = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if command.is_file() and app_path.is_dir():
        subprocess.run(  # noqa: S603
            (os.fspath(command), "-u", os.fspath(app_path)),
            check=False,
            capture_output=True,
            timeout=30,
        )


def observe_native_window(
    temporary_root: Path, host_pid: int, timeout_seconds: int
) -> dict[str, Any]:
    # Keep the established smoke observer authoritative while the full journey
    # remains a separate orchestrator.
    from scripts import macos_tauri_smoke

    probe = temporary_root / "full-product-window-probe.swift"
    macos_tauri_smoke._swift_window_probe(probe)
    return macos_tauri_smoke._observe_window(probe, host_pid, timeout_seconds)
