from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from stock_desk.guidance.models import (
    GuidancePage,
    GuidancePagePreference,
    GuidancePreferences,
    GuidanceStatus,
)


class GuidancePreferencesStorageError(RuntimeError):
    """The preference file cannot be safely read or updated."""


class GuidancePreferencesConflict(RuntimeError):
    """The caller attempted to overwrite a newer preference revision."""


class GuidancePreferencesStore:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("guidance preferences path must be absolute")
        self._path = path
        self._lock = RLock()

    def _load_unlocked(self) -> GuidancePreferences:
        if not self._path.exists():
            return GuidancePreferences()
        try:
            raw = self._path.read_bytes()
            if len(raw) > 64 * 1024:
                raise ValueError
            return GuidancePreferences.model_validate_json(raw, strict=True)
        except (OSError, ValueError, ValidationError) as error:
            raise GuidancePreferencesStorageError() from error

    def load(self) -> GuidancePreferences:
        with self._lock:
            return self._load_unlocked()

    def update(
        self,
        *,
        expected_revision: int,
        page: GuidancePage,
        content_version: int,
        status: GuidanceStatus,
    ) -> GuidancePreferences:
        with self._lock:
            current = self._load_unlocked()
            if current.revision != expected_revision:
                raise GuidancePreferencesConflict()
            pages = dict(current.pages)
            pages[page] = GuidancePagePreference(
                content_version=content_version,
                status=status,
            )
            updated = GuidancePreferences(
                revision=current.revision + 1,
                pages=pages,
            )
            encoded = json.dumps(
                updated.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            parent = self._path.parent
            temporary = parent / f".{self._path.name}.{os.getpid()}.tmp"
            descriptor: int | None = None
            try:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    descriptor = None
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._path)
                if os.name == "posix":
                    directory = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            except OSError as error:
                raise GuidancePreferencesStorageError() from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return updated
