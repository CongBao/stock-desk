from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import stat
import sys
from typing import NoReturn


_FALLBACK_UID = 10001
_FALLBACK_GID = 10001
_MAX_ID = 2**31 - 1
_RUNTIME_DATA_DIR = Path("/app/data")


def _explicit_id(environment: Mapping[str, str], name: str) -> int | None:
    raw_value = environment.get(name)
    if raw_value is None or not raw_value:
        return None
    if not raw_value.isdecimal():
        raise RuntimeError(f"{name} must be a positive nonzero integer")
    value = int(raw_value)
    if not 0 < value <= _MAX_ID:
        raise RuntimeError(f"{name} must be a positive nonzero integer")
    return value


def _select_identity(
    environment: Mapping[str, str],
    *,
    owner_uid: int,
    owner_gid: int,
) -> tuple[int, int]:
    configured_uid = _explicit_id(environment, "STOCK_DESK_UID")
    configured_gid = _explicit_id(environment, "STOCK_DESK_GID")
    uid = configured_uid or (owner_uid if owner_uid > 0 else _FALLBACK_UID)
    gid = configured_gid or (owner_gid if owner_gid > 0 else _FALLBACK_GID)
    if uid <= 0 or gid <= 0:
        raise RuntimeError("Stock Desk refuses to run with a root UID or GID")
    return uid, gid


def _chown_data_path(path: Path, uid: int, gid: int) -> None:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return
        if metadata.st_uid != uid or metadata.st_gid != gid:
            os.chown(os.fspath(path), uid, gid, follow_symlinks=False)
        current_mode = stat.S_IMODE(metadata.st_mode)
        required_mode = 0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600
        updated_mode = current_mode | required_mode
        if updated_mode != current_mode:
            path.chmod(updated_mode, follow_symlinks=False)
    except FileNotFoundError:
        return


def _prepare_data_tree(data_dir: Path, uid: int, gid: int) -> None:
    if uid <= 0 or gid <= 0:
        raise RuntimeError("Stock Desk refuses to prepare data for a root UID or GID")
    data_dir.mkdir(parents=True, exist_ok=True)
    _chown_data_path(data_dir, uid, gid)
    for root, directory_names, file_names in os.walk(data_dir, followlinks=False):
        root_path = Path(root)
        for name in directory_names:
            _chown_data_path(root_path / name, uid, gid)
        for name in file_names:
            _chown_data_path(root_path / name, uid, gid)


def _drop_privileges(uid: int, gid: int) -> None:
    if uid <= 0 or gid <= 0:
        raise RuntimeError("Stock Desk refuses to drop privileges to root")
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def _verify_nonroot_runtime(
    data_dir: Path,
    *,
    current_uid: int,
    current_gid: int,
    environment: Mapping[str, str],
) -> None:
    if current_uid <= 0 or current_gid <= 0:
        raise RuntimeError("Stock Desk runtime process must be non-root")
    configured_uid = _explicit_id(environment, "STOCK_DESK_UID")
    configured_gid = _explicit_id(environment, "STOCK_DESK_GID")
    if configured_uid is not None and configured_uid != current_uid:
        raise RuntimeError("STOCK_DESK_UID does not match the runtime process UID")
    if configured_gid is not None and configured_gid != current_gid:
        raise RuntimeError("STOCK_DESK_GID does not match the runtime process GID")
    required_access = os.R_OK | os.W_OK | os.X_OK
    if not data_dir.is_dir() or not os.access(data_dir, required_access):
        raise RuntimeError(f"Stock Desk data directory must be writable: {data_dir}")


def _validate_command(arguments: Sequence[str]) -> tuple[str, ...]:
    command = tuple(arguments)
    if not command or not command[0]:
        raise RuntimeError("Stock Desk runtime entrypoint requires a command")
    return command


def main() -> NoReturn:
    command = _validate_command(sys.argv[1:])
    configured_data_dir = Path(
        os.environ.get("STOCK_DESK_DATA_DIR", os.fspath(_RUNTIME_DATA_DIR))
    ).resolve()
    if configured_data_dir != _RUNTIME_DATA_DIR:
        raise RuntimeError(
            f"STOCK_DESK_DATA_DIR must be {_RUNTIME_DATA_DIR} in the packaged runtime"
        )

    if os.geteuid() == 0:
        configured_data_dir.mkdir(parents=True, exist_ok=True)
        metadata = configured_data_dir.stat()
        uid, gid = _select_identity(
            os.environ,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
        )
        _prepare_data_tree(configured_data_dir, uid, gid)
        _drop_privileges(uid, gid)

    _verify_nonroot_runtime(
        configured_data_dir,
        current_uid=os.geteuid(),
        current_gid=os.getegid(),
        environment=os.environ,
    )
    os.execvp(command[0], command)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Stock Desk runtime startup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
