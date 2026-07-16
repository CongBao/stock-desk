"""Shared private runtime paths for desktop hosts and frozen sidecars."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from stock_desk.windows_acl import (
    apply_windows_private_dacl as _apply_windows_private_dacl,
)


def _release_version() -> str:
    try:
        return package_version("stock-desk")
    except PackageNotFoundError:
        return "0+unknown"


def _restrict_owner_access(path: Path, *, directory: bool) -> None:
    os.chmod(path, 0o700 if directory else 0o600)
    if os.name == "nt":
        _apply_windows_private_dacl(path, directory=directory)


def _create_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"private runtime directory is invalid: {path}")
    _restrict_owner_access(path, directory=True)


def _create_private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(descriptor)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"private runtime file is invalid: {path}")
    _restrict_owner_access(path, directory=False)


def _create_inherited_private_file(path: Path) -> None:
    """Create a marker inside an already-protected runtime directory."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"private runtime file is invalid: {path}") from None
        return
    try:
        os.chmod(path, 0o600)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    pid: int
    host: str
    port: int
    data_dir: Path
    log_file: Path
    version: str = field(default_factory=_release_version)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_json_object(self) -> dict[str, object]:
        return {
            "data_dir": os.fspath(self.data_dir),
            "host": self.host,
            "log_file": os.fspath(self.log_file),
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_dir: Path
    runtime_dir: Path
    logs_dir: Path
    config_dir: Path
    lock_file: Path
    runtime_record: Path
    shutdown_request: Path
    log_file: Path
    master_key_file: Path

    @classmethod
    def resolve(cls, data_dir: Path) -> RuntimePaths:
        resolved_data_dir = data_dir.expanduser().resolve()
        runtime_dir = resolved_data_dir / "runtime"
        logs_dir = resolved_data_dir / "logs"
        config_dir = resolved_data_dir / "config"
        return cls(
            data_dir=resolved_data_dir,
            runtime_dir=runtime_dir,
            logs_dir=logs_dir,
            config_dir=config_dir,
            lock_file=runtime_dir / "stock-desk.lock",
            runtime_record=runtime_dir / "runtime.json",
            shutdown_request=runtime_dir / "shutdown.request",
            log_file=logs_dir / "stock-desk.log",
            master_key_file=config_dir / "master.key",
        )

    @classmethod
    def create(cls, data_dir: Path) -> RuntimePaths:
        paths = cls.resolve(data_dir)
        for private_directory in (
            paths.data_dir,
            paths.runtime_dir,
            paths.logs_dir,
            paths.config_dir,
        ):
            _create_private_directory(private_directory)
        _create_private_file(paths.lock_file)
        _create_private_file(paths.log_file)
        return paths

    def load_or_create_master_key(self) -> str:
        if not self.master_key_file.exists():
            descriptor = os.open(
                self.master_key_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                encoded = Fernet.generate_key()
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _restrict_owner_access(self.master_key_file, directory=False)
        try:
            return self.master_key_file.read_text(encoding="ascii")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(
                "the private desktop master key is unreadable"
            ) from error

    def write_runtime_record(self, record: RuntimeRecord) -> None:
        temporary = self.runtime_dir / f"runtime-{os.getpid()}.tmp"
        payload = json.dumps(
            record.as_json_object(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, self.runtime_record)
            _restrict_owner_access(self.runtime_record, directory=False)
        finally:
            temporary.unlink(missing_ok=True)
