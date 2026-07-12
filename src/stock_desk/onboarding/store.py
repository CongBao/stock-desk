from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from stock_desk.onboarding.models import OnboardingState


class OnboardingStateStorageError(RuntimeError):
    """The persisted onboarding state cannot be trusted or updated."""


class OnboardingStateStore:
    """Crash-consistent, versioned state stored below the user's data directory."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not path.is_absolute():
            raise ValueError("onboarding state path must be absolute")
        self._path = path
        self._clock = clock
        self._lock = RLock()

    def load(self) -> OnboardingState:
        with self._lock:
            if not self._path.exists():
                return OnboardingState(updated_at=self._clock())
            try:
                raw = self._path.read_bytes()
                if len(raw) > 64 * 1024:
                    raise ValueError
                return OnboardingState.model_validate_json(raw, strict=True)
            except (OSError, ValueError, ValidationError) as error:
                raise OnboardingStateStorageError() from error

    def save(self, state: OnboardingState) -> OnboardingState:
        validated = OnboardingState.model_validate(
            state.model_dump(mode="python"), strict=True
        )
        encoded = json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with self._lock:
            parent = self._path.parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = parent / f".{self._path.name}.{os.getpid()}.tmp"
            descriptor: int | None = None
            try:
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
                raise OnboardingStateStorageError() from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return validated
