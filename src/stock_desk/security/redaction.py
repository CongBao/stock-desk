from collections.abc import Mapping, Sequence, Set
import logging
from threading import RLock
from typing import Any, Final


REDACTED_MARKER: Final = "[REDACTED]"
_CYCLE_MARKER: Final = "[REDACTION_CYCLE]"
_DEPTH_MARKER: Final = "[REDACTION_DEPTH]"
_UNRENDERABLE_LOG_MESSAGE: Final = "[UNRENDERABLE_LOG_MESSAGE]"
_UNRENDERABLE_VALUE: Final = "[UNRENDERABLE_VALUE]"


class SecretRedactor:
    """Recursively replace registered plaintexts without retaining them in reprs."""

    def __init__(self, secrets: list[str] | tuple[str, ...], *, max_depth: int = 64):
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        self._lock = RLock()
        self._secrets: tuple[str, ...] = ()
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
        with self._lock:
            if secret in self._secrets:
                return
            self._secrets = tuple(
                sorted((*self._secrets, secret), key=len, reverse=True)
            )

    def clean(self, value: Any) -> Any:
        with self._lock:
            secrets = self._secrets
        try:
            return self._clean(value, secrets=secrets, depth=0, active=set())
        except Exception:
            return _UNRENDERABLE_VALUE

    def _clean(
        self,
        value: Any,
        *,
        secrets: tuple[str, ...],
        depth: int,
        active: set[int],
    ) -> Any:
        if depth > self._max_depth:
            return _DEPTH_MARKER
        if isinstance(value, str):
            normalized_text = str.__str__(value)
            return _replace_known_strings(normalized_text, secrets)
        if isinstance(value, bytes):
            normalized_bytes = bytes.__bytes__(value)
            text = bytes.decode(normalized_bytes, "utf-8", errors="surrogateescape")
            return _replace_known_strings(text, secrets).encode(
                "utf-8", errors="surrogateescape"
            )
        if isinstance(value, (Mapping, Sequence, Set, BaseException)):
            identity = id(value)
            if identity in active:
                return _CYCLE_MARKER
            active.add(identity)
            try:
                return self._clean_container(
                    value,
                    secrets=secrets,
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
        return _render_unknown(value, secrets)

    def _clean_container(
        self,
        value: Any,
        *,
        secrets: tuple[str, ...],
        depth: int,
        active: set[int],
    ) -> Any:
        def child(item: Any) -> Any:
            return self._clean(
                item,
                secrets=secrets,
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
                    cleaned_mapping[f"[REDACTED_UNHASHABLE_KEY:{index}]"] = (
                        cleaned_value
                    )
            return cleaned_mapping
        if isinstance(value, list):
            return [child(item) for item in value]
        if isinstance(value, tuple):
            return tuple(child(item) for item in value)
        if isinstance(value, Sequence):
            return [child(item) for item in value]
        if isinstance(value, set):
            cleaned_items = [child(item) for item in value]
            try:
                return set(cleaned_items)
            except Exception:
                return cleaned_items
        if isinstance(value, frozenset):
            cleaned_items = [child(item) for item in value]
            try:
                return frozenset(cleaned_items)
            except Exception:
                return cleaned_items
        if isinstance(value, Set):
            cleaned_items = [child(item) for item in value]
            try:
                return set(cleaned_items)
            except Exception:
                return cleaned_items
        if isinstance(value, BaseException):
            exception_name = _replace_known_strings(type(value).__name__, secrets)
            exception_message = _render_unknown(value, secrets)
            return RuntimeError(f"{exception_name}: {exception_message}")
        return value


class RedactingFilter(logging.Filter):
    """Sanitize all message-bearing fields before any formatter sees a record."""

    def __init__(self, redactor: SecretRedactor, name: str = "") -> None:
        super().__init__(name)
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered_message = record.getMessage()
        except Exception:
            rendered_message = _UNRENDERABLE_LOG_MESSAGE
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
            try:
                rendered_exception = str(exception)
            except Exception:
                rendered_exception = _UNRENDERABLE_VALUE
            exception_name = str(self._redactor.clean(type(exception).__name__))
            exception_message = str(self._redactor.clean(rendered_exception))
            safe_exception = RuntimeError(f"{exception_name}: {exception_message}")
            record.exc_info = (RuntimeError, safe_exception, None)
        record.exc_text = None
        return True


def _replace_known_strings(value: str, secrets: tuple[str, ...]) -> str:
    """Replace secrets in one pass while treating an existing marker as opaque."""
    value = str.__str__(value)
    if not secrets or not value:
        return value
    output: list[str] = []
    position = 0
    while position < len(value):
        marker_at = value.find(REDACTED_MARKER, position)
        matches = [
            (found, -len(secret), secret)
            for secret in secrets
            if (found := value.find(secret, position)) >= 0
        ]
        next_secret = min(matches, default=None)
        if marker_at >= 0 and (next_secret is None or marker_at <= next_secret[0]):
            output.append(value[position:marker_at])
            output.append(REDACTED_MARKER)
            position = marker_at + len(REDACTED_MARKER)
            continue
        if next_secret is None:
            output.append(value[position:])
            break
        found, _negative_length, secret = next_secret
        output.append(value[position:found])
        output.append(REDACTED_MARKER)
        position = found + len(secret)
    return "".join(output)


def _render_unknown(value: Any, secrets: tuple[str, ...]) -> str:
    try:
        rendered = str(value)
    except Exception:
        try:
            rendered = repr(value)
        except Exception:
            return _UNRENDERABLE_VALUE
    return _replace_known_strings(rendered, secrets)
