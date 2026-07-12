from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
from threading import RLock

from pydantic import ValidationError

from stock_desk.workspace.models import WorkspaceNotice, WorkspaceState


_MAX_STATE_BYTES = 64 * 1024
_VALID_ROUTES = frozenset(
    {"/market", "/formulas", "/backtests", "/analysis", "/tasks", "/settings"}
)


class WorkspaceStateStorageError(RuntimeError):
    """The persisted workspace cannot be trusted or updated."""

    def __init__(self, code: WorkspaceNotice = "workspace_corrupt") -> None:
        self.code = code
        super().__init__(code)


class WorkspaceStateStore:
    """Crash-consistent workspace state below the versioned user data root."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("workspace state path must be absolute")
        self._path = path
        self._lock = RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> WorkspaceState | None:
        with self._lock:
            if not self._path.exists():
                return None
            try:
                raw = self._path.read_bytes()
                if len(raw) > _MAX_STATE_BYTES:
                    raise WorkspaceStateStorageError()
                decoded = json.loads(raw)
                if not isinstance(decoded, dict):
                    raise WorkspaceStateStorageError()
                if decoded.get("schema_version") != 1:
                    raise WorkspaceStateStorageError("workspace_schema_unsupported")
                preferences = decoded.get("preferences")
                if (
                    isinstance(preferences, dict)
                    and preferences.get("current_page") not in _VALID_ROUTES
                ):
                    raise WorkspaceStateStorageError("workspace_route_invalid")
                return WorkspaceState.model_validate_json(raw, strict=True)
            except WorkspaceStateStorageError:
                raise
            except (OSError, UnicodeError, ValueError, ValidationError) as error:
                raise WorkspaceStateStorageError() from error

    def save(self, state: WorkspaceState) -> WorkspaceState:
        try:
            validated = WorkspaceState.model_validate(
                state.model_dump(mode="python"), strict=True
            )
            encoded = json.dumps(
                validated.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, UnicodeError, ValueError, ValidationError) as error:
            raise WorkspaceStateStorageError() from error
        if len(encoded) > _MAX_STATE_BYTES:
            raise WorkspaceStateStorageError()

        with self._lock:
            parent = self._path.parent
            try:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                temporary = parent / (
                    f".{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
                )
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except OSError as error:
                raise WorkspaceStateStorageError() from error
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._path)
                self._sync_parent(parent)
            except OSError as error:
                raise WorkspaceStateStorageError() from error
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return validated

    def delete(self) -> None:
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
                if self._path.parent.exists():
                    self._sync_parent(self._path.parent)
            except OSError as error:
                raise WorkspaceStateStorageError() from error

    @staticmethod
    def _sync_parent(parent: Path) -> None:
        if os.name != "posix":
            return
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
