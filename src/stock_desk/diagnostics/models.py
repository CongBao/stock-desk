from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import StrEnum
import re
from threading import RLock
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_SAFE_ID: Final = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_GIT_REVISION: Final = re.compile(r"[0-9a-f]{40}\Z")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DiagnosticEventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticEventCode(StrEnum):
    """Closed production event vocabulary safe for local diagnostic export."""

    SIDECAR_STARTING = "sidecar.starting"
    SIDECAR_API_CONFIGURED = "sidecar.api_configured"
    SIDECAR_READY = "sidecar.ready"
    SIDECAR_RUNTIME_FAILED = "sidecar.runtime_failed"
    SIDECAR_STOPPING = "sidecar.stopping"
    SIDECAR_STOPPED = "sidecar.stopped"
    DIAGNOSTIC_CONFIGURATION_UNAVAILABLE = "diagnostic.configuration_unavailable"
    DIAGNOSTIC_HEALTH_UNAVAILABLE = "diagnostic.health_unavailable"
    STORAGE_READY = "storage.ready"
    STORAGE_UNAVAILABLE = "storage.unavailable"
    WORKER_STARTING = "worker.starting"
    WORKER_READY = "worker.ready"
    WORKER_UNAVAILABLE = "worker.unavailable"
    WORKER_STARTUP_FAILED = "worker.startup_failed"
    WORKER_RUNTIME_FAILED = "worker.runtime_failed"
    WORKER_TASK_FAILED = "worker.task_failed"
    WORKER_SHUTDOWN_TIMEOUT = "worker.shutdown_timeout"
    WORKER_STOPPED = "worker.stopped"


class DiagnosticModelProvider(StrEnum):
    DEEPSEEK = "deepseek"
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"


class DiagnosticEvent(_FrozenModel):
    timestamp: datetime
    level: DiagnosticEventLevel
    component: str
    event_code: str
    failure_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("component", "event_code", "failure_id")
    @classmethod
    def validate_safe_id(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_ID.fullmatch(value) is None:
            raise ValueError("diagnostic identifier is invalid")
        return value


class DiagnosticApplication(_FrozenModel):
    version: str = Field(min_length=1, max_length=64, pattern=r"[0-9A-Za-z.+-]+")
    source_revision: str | None = None

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", value) is None:
            raise ValueError("diagnostic application version is invalid")
        return value

    @field_validator("source_revision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        if value is not None and _GIT_REVISION.fullmatch(value) is None:
            raise ValueError("diagnostic source revision is invalid")
        return value


class DiagnosticPlatform(_FrozenModel):
    system: Literal["windows", "other"]
    architecture: Literal["x86_64", "other"]


class DiagnosticServiceHealth(_FrozenModel):
    sidecar: Literal["ready"] = "ready"
    storage: Literal["ready", "unavailable"]
    market_worker: Literal["ready", "unavailable"]


class DiagnosticConfiguration(_FrozenModel):
    available: bool
    daily_sources: tuple[str, ...] = ()
    weekly_sources: tuple[str, ...] = ()
    minute_sources: tuple[str, ...] = ()
    instrument_sources: tuple[str, ...] = ()
    tushare_token_configured: bool = False
    local_tdx_configured: bool = False
    model_providers: tuple[DiagnosticModelProvider, ...] = ()

    @field_validator(
        "daily_sources",
        "weekly_sources",
        "minute_sources",
        "instrument_sources",
    )
    @classmethod
    def validate_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 8 or any(_SAFE_ID.fullmatch(item) is None for item in value):
            raise ValueError("diagnostic source list is invalid")
        return value


class DiagnosticPrivacy(_FrozenModel):
    telemetry_enabled: Literal[False] = False
    automatic_crash_upload: Literal[False] = False
    automatic_diagnostic_upload: Literal[False] = False
    stable_device_identifier: Literal[False] = False


class DiagnosticSnapshot(_FrozenModel):
    schema_version: Literal["stock-desk-diagnostic-snapshot-v1"] = (
        "stock-desk-diagnostic-snapshot-v1"
    )
    created_at: datetime
    application: DiagnosticApplication
    platform: DiagnosticPlatform
    service_health: DiagnosticServiceHealth
    configuration: DiagnosticConfiguration
    events: tuple[DiagnosticEvent, ...]
    failure_ids: tuple[str, ...]
    privacy: DiagnosticPrivacy = Field(default_factory=DiagnosticPrivacy)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @field_validator("events")
    @classmethod
    def validate_event_limit(
        cls, value: tuple[DiagnosticEvent, ...]
    ) -> tuple[DiagnosticEvent, ...]:
        if len(value) > 200:
            raise ValueError("diagnostic event limit exceeded")
        return value

    @field_validator("failure_ids")
    @classmethod
    def validate_failures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 32 or any(_SAFE_ID.fullmatch(item) is None for item in value):
            raise ValueError("diagnostic failure identifiers are invalid")
        if value != tuple(dict.fromkeys(value)):
            raise ValueError("diagnostic failure identifiers must be unique")
        return value


class DiagnosticEventBuffer:
    """Bounded memory-only event stream containing no free-form message fields."""

    def __init__(self, *, maximum: int = 200) -> None:
        if not 1 <= maximum <= 200:
            raise ValueError("diagnostic event capacity is invalid")
        self._events: deque[DiagnosticEvent] = deque(maxlen=maximum)
        self._lock = RLock()

    def append(self, event: DiagnosticEvent) -> None:
        with self._lock:
            self._events.append(event)

    def snapshot(self) -> tuple[DiagnosticEvent, ...]:
        with self._lock:
            return tuple(self._events)


_EventDefinition = tuple[DiagnosticEventLevel, str, str | None]
_EVENT_DEFINITIONS: Final[Mapping[DiagnosticEventCode, _EventDefinition]] = {
    DiagnosticEventCode.SIDECAR_STARTING: (
        DiagnosticEventLevel.INFO,
        "sidecar",
        None,
    ),
    DiagnosticEventCode.SIDECAR_API_CONFIGURED: (
        DiagnosticEventLevel.INFO,
        "sidecar",
        None,
    ),
    DiagnosticEventCode.SIDECAR_READY: (
        DiagnosticEventLevel.INFO,
        "sidecar",
        None,
    ),
    DiagnosticEventCode.SIDECAR_RUNTIME_FAILED: (
        DiagnosticEventLevel.ERROR,
        "sidecar",
        "sidecar_runtime_failed",
    ),
    DiagnosticEventCode.SIDECAR_STOPPING: (
        DiagnosticEventLevel.INFO,
        "sidecar",
        None,
    ),
    DiagnosticEventCode.SIDECAR_STOPPED: (
        DiagnosticEventLevel.INFO,
        "sidecar",
        None,
    ),
    DiagnosticEventCode.DIAGNOSTIC_CONFIGURATION_UNAVAILABLE: (
        DiagnosticEventLevel.WARNING,
        "diagnostic",
        "diagnostic_configuration_unavailable",
    ),
    DiagnosticEventCode.DIAGNOSTIC_HEALTH_UNAVAILABLE: (
        DiagnosticEventLevel.WARNING,
        "diagnostic",
        "diagnostic_health_unavailable",
    ),
    DiagnosticEventCode.STORAGE_READY: (
        DiagnosticEventLevel.INFO,
        "storage",
        None,
    ),
    DiagnosticEventCode.STORAGE_UNAVAILABLE: (
        DiagnosticEventLevel.ERROR,
        "storage",
        "storage_unavailable",
    ),
    DiagnosticEventCode.WORKER_STARTING: (
        DiagnosticEventLevel.INFO,
        "worker",
        None,
    ),
    DiagnosticEventCode.WORKER_READY: (
        DiagnosticEventLevel.INFO,
        "worker",
        None,
    ),
    DiagnosticEventCode.WORKER_UNAVAILABLE: (
        DiagnosticEventLevel.WARNING,
        "worker",
        "market_worker_unavailable",
    ),
    DiagnosticEventCode.WORKER_STARTUP_FAILED: (
        DiagnosticEventLevel.ERROR,
        "worker",
        "market_worker_unavailable",
    ),
    DiagnosticEventCode.WORKER_RUNTIME_FAILED: (
        DiagnosticEventLevel.ERROR,
        "worker",
        "market_worker_unavailable",
    ),
    DiagnosticEventCode.WORKER_TASK_FAILED: (
        DiagnosticEventLevel.WARNING,
        "worker",
        "task_handler_failed",
    ),
    DiagnosticEventCode.WORKER_SHUTDOWN_TIMEOUT: (
        DiagnosticEventLevel.WARNING,
        "worker",
        "market_worker_shutdown_timeout",
    ),
    DiagnosticEventCode.WORKER_STOPPED: (
        DiagnosticEventLevel.INFO,
        "worker",
        None,
    ),
}


class DiagnosticEventSink:
    """Thread-safe producer that accepts only the closed production vocabulary."""

    def __init__(
        self,
        *,
        event_buffer: DiagnosticEventBuffer | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._buffer = event_buffer or DiagnosticEventBuffer()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def event_buffer(self) -> DiagnosticEventBuffer:
        return self._buffer

    def emit(self, code: DiagnosticEventCode) -> None:
        if not isinstance(code, DiagnosticEventCode):
            raise TypeError("diagnostic event code must use the closed vocabulary")
        level, component, failure_id = _EVENT_DEFINITIONS[code]
        self._buffer.append(
            DiagnosticEvent(
                timestamp=self._clock(),
                level=level,
                component=component,
                event_code=code.value,
                failure_id=failure_id,
            )
        )


ConfigurationProvider = Callable[[], DiagnosticConfiguration]
HealthProvider = Callable[[], tuple[bool, bool]]
Clock = Callable[[], datetime]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("diagnostic timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class DiagnosticSnapshotService:
    def __init__(
        self,
        *,
        version: str,
        source_revision: str | None,
        configuration_provider: ConfigurationProvider,
        health_provider: HealthProvider,
        event_buffer: DiagnosticEventBuffer | None = None,
        event_sink: DiagnosticEventSink | None = None,
        clock: Clock | None = None,
        platform_system: str,
        platform_machine: str,
    ) -> None:
        self._application = DiagnosticApplication(
            version=version, source_revision=source_revision
        )
        self._platform = DiagnosticPlatform(
            system="windows" if platform_system.casefold() == "windows" else "other",
            architecture=(
                "x86_64"
                if platform_machine.casefold() in {"amd64", "x86_64"}
                else "other"
            ),
        )
        self._configuration_provider = configuration_provider
        self._health_provider = health_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if event_buffer is not None and event_sink is not None:
            raise ValueError("diagnostic event source is ambiguous")
        self._event_sink = event_sink or DiagnosticEventSink(
            event_buffer=event_buffer,
            clock=self._clock,
        )
        self._events = self._event_sink.event_buffer

    def snapshot(self) -> DiagnosticSnapshot:
        failures: list[str] = []
        try:
            configuration = self._configuration_provider()
        except Exception:
            configuration = DiagnosticConfiguration(available=False)
            failures.append("diagnostic_configuration_unavailable")
            self._event_sink.emit(
                DiagnosticEventCode.DIAGNOSTIC_CONFIGURATION_UNAVAILABLE
            )
        try:
            storage_ready, worker_ready = self._health_provider()
        except Exception:
            storage_ready, worker_ready = False, False
            failures.append("diagnostic_health_unavailable")
            self._event_sink.emit(DiagnosticEventCode.DIAGNOSTIC_HEALTH_UNAVAILABLE)
        events = self._events.snapshot()
        failures.extend(
            event.failure_id
            for event in events
            if event.failure_id is not None and event.failure_id not in failures
        )
        return DiagnosticSnapshot(
            created_at=self._clock(),
            application=self._application,
            platform=self._platform,
            service_health=DiagnosticServiceHealth(
                storage="ready" if storage_ready else "unavailable",
                market_worker="ready" if worker_ready else "unavailable",
            ),
            configuration=configuration,
            events=events,
            failure_ids=tuple(failures),
        )


__all__ = [
    "DiagnosticConfiguration",
    "DiagnosticEvent",
    "DiagnosticEventBuffer",
    "DiagnosticEventCode",
    "DiagnosticEventLevel",
    "DiagnosticEventSink",
    "DiagnosticModelProvider",
    "DiagnosticSnapshot",
    "DiagnosticSnapshotService",
]
