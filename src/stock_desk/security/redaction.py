from collections.abc import Callable, Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from threading import RLock
from typing import Any, Final
import weakref


_MINIMUM_SECRET_LENGTH: Final = 4
_MARKER_COUNT: Final = 7
_PRIVATE_USE_RANGES: Final = (
    range(0xE000, 0xF900),
    range(0xF0000, 0xFFFFE),
    range(0x100000, 0x10FFFE),
)
REDACTED_MARKER: Final = "\ue000"
_LOG_REDACTION_LOCK = RLock()
_RECORD_REDACTOR_ATTRIBUTE: Final = "_stock_desk_redactor_v1"


@dataclass(frozen=True, slots=True)
class _Markers:
    redacted: str
    cycle: str
    depth: str
    unrenderable_log: str
    unrenderable_value: str
    unhashable_key: str
    collapse: str


class SecretRedactor:
    """Recursively replace registered plaintexts without retaining them in reprs."""

    def __init__(self, secrets: list[str] | tuple[str, ...], *, max_depth: int = 64):
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        self._lock = RLock()
        self._secrets: tuple[str, ...] = ()
        self._markers = _resolve_markers(())
        self._max_depth = max_depth
        for secret in secrets:
            self.register(secret)

    def __repr__(self) -> str:
        with self._lock:
            count = len(self._secrets)
        return f"SecretRedactor({count} secrets)"

    def register(self, secret: str) -> None:
        if not isinstance(secret, str):
            raise TypeError("Secret must be a string")
        secret = str.__str__(secret)
        if not secret:
            return
        if len(secret) < _MINIMUM_SECRET_LENGTH:
            raise ValueError("Secret must contain at least 4 characters")
        with self._lock:
            if secret in self._secrets:
                return
            secrets = tuple(sorted((*self._secrets, secret), key=len, reverse=True))
            markers = _resolve_markers(secrets)
            self._secrets = secrets
            self._markers = markers

    @property
    def redacted_marker(self) -> str:
        with self._lock:
            return self._markers.redacted

    @property
    def cycle_marker(self) -> str:
        with self._lock:
            return self._markers.cycle

    @property
    def depth_marker(self) -> str:
        with self._lock:
            return self._markers.depth

    @property
    def unrenderable_log_marker(self) -> str:
        with self._lock:
            return self._markers.unrenderable_log

    @property
    def unrenderable_value_marker(self) -> str:
        with self._lock:
            return self._markers.unrenderable_value

    def clean(self, value: Any) -> Any:
        with self._lock:
            secrets = self._secrets
            markers = self._markers
        try:
            cleaned = self._clean(
                value,
                secrets=secrets,
                markers=markers,
                depth=0,
                active=set(),
            )
        except Exception:
            cleaned = markers.unrenderable_value
        return _audit_result(cleaned, secrets, markers.collapse)

    def _secret_snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return self._secrets

    @classmethod
    def _combine(cls, redactors: tuple["SecretRedactor", ...]) -> "SecretRedactor":
        secrets = tuple(
            sorted(
                {
                    secret
                    for redactor in redactors
                    for secret in redactor._secret_snapshot()
                },
                key=len,
                reverse=True,
            )
        )
        return cls(secrets)

    def _clean(
        self,
        value: Any,
        *,
        secrets: tuple[str, ...],
        markers: _Markers,
        depth: int,
        active: set[int],
    ) -> Any:
        if depth > self._max_depth:
            return markers.depth
        if isinstance(value, str):
            normalized_text = str.__str__(value)
            return _replace_known_strings(normalized_text, secrets, markers.redacted)
        if isinstance(value, bytes):
            normalized_bytes = bytes.__bytes__(value)
            text = bytes.decode(normalized_bytes, "utf-8", errors="surrogateescape")
            return _replace_known_strings(text, secrets, markers.redacted).encode(
                "utf-8", errors="surrogateescape"
            )
        if isinstance(value, (Mapping, Sequence, Set, BaseException)):
            identity = id(value)
            if identity in active:
                return markers.cycle
            active.add(identity)
            try:
                return self._clean_container(
                    value,
                    secrets=secrets,
                    markers=markers,
                    depth=depth,
                    active=active,
                )
            finally:
                active.remove(identity)
        if value is None or type(value) is bool:
            return value
        if isinstance(value, int):
            return int.__int__(value)
        if isinstance(value, float):
            return float.__float__(value)
        if isinstance(value, complex):
            return complex.__complex__(value)
        return _render_unknown(value, secrets, markers)

    def _clean_container(
        self,
        value: Any,
        *,
        secrets: tuple[str, ...],
        markers: _Markers,
        depth: int,
        active: set[int],
    ) -> Any:
        def child(item: Any) -> Any:
            return self._clean(
                item,
                secrets=secrets,
                markers=markers,
                depth=depth + 1,
                active=active,
            )

        if isinstance(value, Mapping):
            cleaned_mapping: dict[Any, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                cleaned_key = child(key)
                cleaned_value = child(item)
                try:
                    hash(cleaned_key)
                    cleaned_mapping[cleaned_key] = cleaned_value
                except Exception:
                    cleaned_mapping[(markers.unhashable_key, index)] = cleaned_value
            return cleaned_mapping
        if isinstance(value, list):
            return [child(item) for item in value]
        if isinstance(value, tuple):
            return tuple(child(item) for item in value)
        if isinstance(value, Sequence):
            return [child(item) for item in value]
        if isinstance(value, set):
            return _safe_set([child(item) for item in value], frozen=False)
        if isinstance(value, frozenset):
            return _safe_set([child(item) for item in value], frozen=True)
        if isinstance(value, Set):
            return _safe_set([child(item) for item in value], frozen=False)
        if isinstance(value, BaseException):
            exception_name = _safe_exception_name(value, secrets, markers)
            exception_message = _render_unknown(value, secrets, markers)
            return RuntimeError(f"{exception_name}: {exception_message}")
        return markers.unrenderable_value


class _LogRedactionContext:
    """In-process union snapshot that serializes to a harmless builtin value."""

    __slots__ = ("_redactor", "__weakref__")

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor

    def __repr__(self) -> str:
        return "LogRedactionContext(active=True)"

    def __reduce__(self) -> tuple[Callable[[], None], tuple[()]]:
        return type(None), ()

    @classmethod
    def _union(
        cls,
        contexts: tuple["_LogRedactionContext", ...],
        active_redactor: SecretRedactor | None,
    ) -> "_LogRedactionContext":
        if len(contexts) == 1 and active_redactor is None:
            return contexts[0]
        redactors = tuple(context._redactor for context in contexts)
        if active_redactor is not None:
            redactors += (active_redactor,)
        return cls(SecretRedactor._combine(redactors))

    def _sanitize(self, record: logging.LogRecord) -> None:
        RedactingFilter(self._redactor).filter(record)

    def _clean_output(self, value: Any) -> str:
        cleaned = self._redactor.clean(value)
        if not isinstance(cleaned, str):
            return self._redactor.unrenderable_log_marker
        return str.__str__(cleaned)


class RedactingFilter(logging.Filter):
    """Sanitize structured fields before a final ``RedactingFormatter`` pass."""

    def __init__(self, redactor: SecretRedactor, name: str = "") -> None:
        super().__init__(name)
        self._redactor = redactor

    @property
    def redactor(self) -> SecretRedactor:
        return self._redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered_message = record.getMessage()
        except Exception:
            rendered_message = self._redactor.unrenderable_log_marker
        record.msg = str(self._redactor.clean(rendered_message))
        record.args = ()
        for key, value in tuple(record.__dict__.items()):
            if key not in {"msg", "args", "exc_info", "exc_text", "stack_info"}:
                setattr(record, key, self._redactor.clean(value))
        if hasattr(record, "message"):
            record.message = record.msg
        if record.stack_info is not None:
            cleaned_stack = self._redactor.clean(record.stack_info)
            record.stack_info = str(cleaned_stack)
        if isinstance(record.exc_info, tuple):
            _exception_type, exception, _traceback = record.exc_info
            cleaned_exception = self._redactor.clean(exception)
            if type(cleaned_exception) is RuntimeError:
                safe_exception = cleaned_exception
            else:
                safe_exception = RuntimeError(str(cleaned_exception))
            record.exc_info = (RuntimeError, safe_exception, None)
        record.exc_text = None
        return True


class RedactingFormatter(logging.Formatter):
    """Redact a delegate formatter's fully composed output as the final boundary."""

    def __init__(self, redactor: SecretRedactor, delegate: logging.Formatter) -> None:
        super().__init__()
        self._redactor = redactor
        self._delegate = delegate

    @property
    def redactor(self) -> SecretRedactor:
        return self._redactor

    @property
    def delegate(self) -> logging.Formatter:
        return self._delegate

    def format(self, record: logging.LogRecord) -> str:
        try:
            rendered = self._delegate.format(record)
        except Exception:
            return self._redactor.unrenderable_log_marker
        cleaned = self._redactor.clean(rendered)
        if not isinstance(cleaned, str):
            return self._redactor.unrenderable_log_marker
        return str.__str__(cleaned)


def configure_redacting_handler(
    handler: logging.Handler, redactor: SecretRedactor
) -> logging.Handler:
    """Install structured and final-output redaction while preserving formatting."""
    handler.acquire()
    try:
        current_formatter = handler.formatter or logging.Formatter()
        if not (
            isinstance(current_formatter, RedactingFormatter)
            and current_formatter.redactor is redactor
        ):
            handler.setFormatter(RedactingFormatter(redactor, current_formatter))
        if not any(
            isinstance(item, RedactingFilter) and item.redactor is redactor
            for item in handler.filters
        ):
            handler.addFilter(RedactingFilter(redactor))
    finally:
        handler.release()
    return handler


def _redacting_handler_handle(
    handler: logging.Handler, record: logging.LogRecord
) -> Any:
    with _LOG_REDACTION_LOCK:
        redacting_filter = _ACTIVE_UNION_FILTER
    incoming_context = record.__dict__.get(_RECORD_REDACTOR_ATTRIBUTE)
    if redacting_filter is None and not isinstance(
        incoming_context, _LogRedactionContext
    ):
        return _HANDLER_HANDLE_DELEGATE(handler, record)

    result = handler.filter(record)
    if isinstance(result, logging.LogRecord):
        record = result
    if result:
        attached = record.__dict__.pop(_RECORD_REDACTOR_ATTRIBUTE, None)
        contexts = tuple(
            context
            for context in (attached, incoming_context)
            if isinstance(context, _LogRedactionContext)
        )
        active_redactor = (
            None if redacting_filter is None else redacting_filter.redactor
        )
        record_context = _LogRedactionContext._union(contexts, active_redactor)
        record_context._sanitize(record)
        record.__dict__[_RECORD_REDACTOR_ATTRIBUTE] = record_context
        handler.acquire()
        try:
            handler.emit(record)
        finally:
            handler.release()
    return result


def _redacting_handler_format(
    handler: logging.Handler, record: logging.LogRecord
) -> Any:
    rendered = _HANDLER_FORMAT_DELEGATE(handler, record)
    record_context = record.__dict__.get(_RECORD_REDACTOR_ATTRIBUTE)
    if not isinstance(record_context, _LogRedactionContext):
        return rendered
    return record_context._clean_output(rendered)


def _normalized_secrets(secrets: tuple[str, ...]) -> tuple[str, ...]:
    validated = SecretRedactor(secrets)
    del validated
    return tuple(sorted(frozenset(secrets), key=len, reverse=True))


def _refresh_union_locked() -> None:
    global _ACTIVE_UNION_FILTER
    secrets = tuple(
        sorted(
            {
                secret
                for registered in _ACTIVE_SECRET_REGISTRATIONS.values()
                for secret in registered
            },
            key=len,
            reverse=True,
        )
    )
    _ACTIVE_UNION_FILTER = RedactingFilter(SecretRedactor(secrets)) if secrets else None


def _replace_registration(registration_id: int, secrets: tuple[str, ...]) -> None:
    normalized = _normalized_secrets(secrets)
    with _LOG_REDACTION_LOCK:
        if normalized:
            _ACTIVE_SECRET_REGISTRATIONS[registration_id] = normalized
        else:
            _ACTIVE_SECRET_REGISTRATIONS.pop(registration_id, None)
        _refresh_union_locked()


def _remove_registration(registration_id: int) -> None:
    with _LOG_REDACTION_LOCK:
        if _ACTIVE_SECRET_REGISTRATIONS.pop(registration_id, None) is not None:
            _refresh_union_locked()


class LogSecretLease:
    """A replaceable bounded contribution to standard logging redaction."""

    def __init__(self, *secrets: str) -> None:
        global _NEXT_SECRET_REGISTRATION_ID
        self._lock = RLock()
        self._closed = False
        with _LOG_REDACTION_LOCK:
            self._registration_id = _NEXT_SECRET_REGISTRATION_ID
            _NEXT_SECRET_REGISTRATION_ID += 1
        self._finalizer = weakref.finalize(
            self, _remove_registration, self._registration_id
        )
        self.replace(*secrets)

    def __repr__(self) -> str:
        return f"LogSecretLease(closed={self._closed})"

    def replace(self, *secrets: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Log secret lease is closed")
            _replace_registration(self._registration_id, tuple(secrets))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._finalizer()


@contextmanager
def scoped_log_redaction(*secrets: str) -> Iterator[LogSecretLease]:
    """Redact standard logging output while this bounded process scope is active.

    Concurrent scopes and lifetime leases contribute to one longest-first union. A
    process-stable ``Handler.handle`` hook applies the union after existing logger and
    handler filters, while custom record factories remain untouched.
    """
    lease = LogSecretLease(*secrets)
    try:
        yield lease
    finally:
        lease.close()


def _marker_candidates() -> Iterator[str]:
    for marker_range in _PRIVATE_USE_RANGES:
        for codepoint in marker_range:
            yield chr(codepoint)
    for codepoint in range(0x2500, 0x2C00):
        candidate = chr(codepoint)
        if candidate.isprintable() and len(repr(candidate)) < _MINIMUM_SECRET_LENGTH:
            yield candidate


def _resolve_markers(secrets: tuple[str, ...]) -> _Markers:
    resolved: list[str] = []
    for candidate in _marker_candidates():
        rendered_candidate = repr(candidate)
        representation_fragment = rendered_candidate[1:-1]
        representations = (candidate, rendered_candidate, representation_fragment)
        if any(
            secret in rendered or rendered in secret
            for secret in secrets
            for rendered in representations
        ):
            continue
        resolved.append(candidate)
        if len(resolved) == _MARKER_COUNT:
            return _Markers(*resolved)
    raise RuntimeError("Unable to resolve safe redaction markers")


def _audit_result(value: Any, secrets: tuple[str, ...], collapse_marker: str) -> Any:
    try:
        representations = (str(value), repr(value))
    except Exception:
        return collapse_marker
    if any(secret in rendered for secret in secrets for rendered in representations):
        return collapse_marker
    return value


def _safe_set(
    items: list[Any], *, frozen: bool
) -> set[Any] | frozenset[Any] | list[Any]:
    try:
        if frozen:
            return frozenset(items)
        return set(items)
    except Exception:
        return items


def _safe_exception_name(
    value: BaseException, secrets: tuple[str, ...], markers: _Markers
) -> str:
    try:
        name = type(value).__name__
        if not isinstance(name, str):
            return markers.unrenderable_value
        normalized_name = str.__str__(name)
    except Exception:
        return markers.unrenderable_value
    return _replace_known_strings(normalized_name, secrets, markers.redacted)


def _replace_known_strings(
    value: str, secrets: tuple[str, ...], redacted_marker: str
) -> str:
    """Replace secrets in one pass while treating only the safe marker as opaque."""
    value = str.__str__(value)
    if not secrets or not value:
        return value
    output: list[str] = []
    position = 0
    while position < len(value):
        marker_at = value.find(redacted_marker, position)
        matches = [
            (found, -len(secret), secret)
            for secret in secrets
            if (found := value.find(secret, position)) >= 0
        ]
        next_secret = min(matches, default=None)
        if marker_at >= 0 and (next_secret is None or marker_at < next_secret[0]):
            output.append(value[position:marker_at])
            output.append(redacted_marker)
            position = marker_at + len(redacted_marker)
            continue
        if next_secret is None:
            output.append(value[position:])
            break
        found, _negative_length, secret = next_secret
        output.append(value[position:found])
        output.append(redacted_marker)
        position = found + len(secret)
    return "".join(output)


def _render_unknown(value: Any, secrets: tuple[str, ...], markers: _Markers) -> str:
    try:
        rendered = str(value)
    except Exception:
        try:
            rendered = repr(value)
        except Exception:
            return markers.unrenderable_value
    return _replace_known_strings(rendered, secrets, markers.redacted)


_ACTIVE_SECRET_REGISTRATIONS: dict[int, tuple[str, ...]] = {}
_ACTIVE_UNION_FILTER: RedactingFilter | None = None
_HANDLER_HANDLE_DELEGATE: Callable[[logging.Handler, logging.LogRecord], Any] = (
    logging.Handler.handle
)
_HANDLER_FORMAT_DELEGATE: Callable[[logging.Handler, logging.LogRecord], Any] = (
    logging.Handler.format
)
_NEXT_SECRET_REGISTRATION_ID = 0


def _install_process_logging_hook() -> None:
    with _LOG_REDACTION_LOCK:
        if logging.Handler.handle is not _redacting_handler_handle:
            setattr(logging.Handler, "handle", _redacting_handler_handle)
        if logging.Handler.format is not _redacting_handler_format:
            setattr(logging.Handler, "format", _redacting_handler_format)


_install_process_logging_hook()
