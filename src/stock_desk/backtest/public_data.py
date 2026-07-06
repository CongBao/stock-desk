from __future__ import annotations

from collections.abc import Mapping
import re


_DANGEROUS_KEY = re.compile(
    r"(?:claim|secret|token|password|path|exception|traceback|raw_error|formula_source|provider_diagnostic)",
    re.I,
)
_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s(='\"])(?:"
    r"file:///[^\x00\s,;)\]\}]+|"
    r"/[^\x00\s,;)\]\}]+|"
    r"[A-Za-z]:[\\/][^\x00\s,;)\]\}]+|"
    r"\\\\(?:\?\\)?[^\x00\s,;)\]\}]+"
    r")"
)


def is_dangerous_key(key: str) -> bool:
    return _DANGEROUS_KEY.search(key) is not None


def public_text(value: str) -> str:
    if _ABSOLUTE_PATH.search(value) is not None or "token=" in value.lower():
        return "[REDACTED]"
    return value


def public_payload(value: object, *, depth: int = 0) -> object:
    if depth > 32:
        return "[REDACTED]"
    if isinstance(value, str):
        return public_text(value)
    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, Mapping):
        return {
            key: public_payload(item, depth=depth + 1)
            for key, item in sorted(value.items())
            if isinstance(key, str) and not is_dangerous_key(key)
        }
    if isinstance(value, (tuple, list)):
        return [public_payload(item, depth=depth + 1) for item in value]
    return "[REDACTED]"


__all__ = ["is_dangerous_key", "public_payload", "public_text"]
