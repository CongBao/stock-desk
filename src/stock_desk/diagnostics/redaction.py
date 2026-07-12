"""Schema-bound diagnostic sanitizing and final-byte leak detection."""

from __future__ import annotations

import re
from typing import Final, cast


REDACTED: Final = "<redacted>"
_MAX_DEPTH: Final = 16
_MAX_NODES: Final = 4_096
_MAX_TEXT: Final = 8_192
_SAFE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PRIVATE_PATHS = (
    re.compile(r"(?i)(?:[a-z]:)?[\\/]users[\\/][^\\/\s\"']+"),
    re.compile(r"/(?:home|Users)/[^/\s\"']+"),
)
_AUTHORIZATION = re.compile(r"(?i)\bauthorization\s*[:=]\s*[^\s,;}]+")
_DIRECT_CREDENTIALS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9._-]{24,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{24,}\b"),
)
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "device_id",
        "hostname",
        "machine_id",
        "master_key",
        "password",
        "path",
        "private_prompt",
        "prompt",
        "secret",
        "session",
        "token",
        "username",
    }
)


class DiagnosticSafetyError(ValueError):
    """Diagnostic material could not be proven safe for export."""


def sanitize_tree(
    value: object,
    *,
    allowed_keys: frozenset[str],
    secrets: tuple[str, ...] = (),
    private_identities: tuple[str, ...] = (),
) -> object:
    """Return JSON-compatible allowlisted data or abort without partial output."""

    if any(not item for item in (*secrets, *private_identities)):
        raise DiagnosticSafetyError("empty diagnostic redaction identity")
    state = [0]

    def clean(item: object, depth: int) -> object:
        state[0] += 1
        if depth > _MAX_DEPTH or state[0] > _MAX_NODES:
            raise DiagnosticSafetyError("diagnostic structure exceeds safety limits")
        if item is None or type(item) in {bool, int, float}:
            return item
        if type(item) is str:
            return _sanitize_text(
                item,
                secrets=secrets,
                private_identities=private_identities,
            )
        if type(item) is dict:
            output: dict[str, object] = {}
            for key, child in item.items():
                if type(key) is not str or _SAFE_KEY.fullmatch(key) is None:
                    raise DiagnosticSafetyError("diagnostic key is invalid")
                if key not in allowed_keys:
                    raise DiagnosticSafetyError("diagnostic key is not allowlisted")
                if _is_sensitive_key(key):
                    output[key] = REDACTED
                else:
                    output[key] = clean(child, depth + 1)
            return output
        if type(item) in {list, tuple}:
            return [
                clean(child, depth + 1)
                for child in cast(list[object] | tuple[object, ...], item)
            ]
        raise DiagnosticSafetyError("diagnostic value is not strict JSON data")

    cleaned = clean(value, 0)
    assert_safe_bytes(
        _canonical_probe_bytes(cleaned),
        secrets=secrets,
        private_identities=private_identities,
    )
    return cleaned


def assert_safe_bytes(
    payload: bytes,
    *,
    secrets: tuple[str, ...] = (),
    private_identities: tuple[str, ...] = (),
) -> None:
    """Reject final serialized material that still has high-confidence secrets."""

    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise DiagnosticSafetyError("diagnostic output is not UTF-8") from error
    if any(secret and secret in text for secret in secrets):
        raise DiagnosticSafetyError("diagnostic output retained a configured secret")
    if any(identity and identity in text for identity in private_identities):
        raise DiagnosticSafetyError("diagnostic output retained a private identity")
    if _AUTHORIZATION.search(text) is not None:
        raise DiagnosticSafetyError("diagnostic output retained authorization data")
    if any(pattern.search(text) is not None for pattern in _PRIVATE_PATHS):
        raise DiagnosticSafetyError("diagnostic output retained a private path")
    if any(pattern.search(text) is not None for pattern in _DIRECT_CREDENTIALS):
        raise DiagnosticSafetyError("diagnostic output retained credential material")


def _is_sensitive_key(key: str) -> bool:
    folded = key.casefold()
    return any(part in folded for part in _SENSITIVE_KEY_PARTS)


def _sanitize_text(
    value: str,
    *,
    secrets: tuple[str, ...],
    private_identities: tuple[str, ...],
) -> str:
    if len(value) > _MAX_TEXT or any(ord(character) < 32 for character in value):
        raise DiagnosticSafetyError("diagnostic text exceeds safety limits")
    output = value
    for private in sorted((*secrets, *private_identities), key=len, reverse=True):
        output = output.replace(private, REDACTED)
    output = _AUTHORIZATION.sub(REDACTED, output)
    for pattern in _PRIVATE_PATHS:
        output = pattern.sub(REDACTED, output)
    for pattern in _DIRECT_CREDENTIALS:
        output = pattern.sub(REDACTED, output)
    return output


def _canonical_probe_bytes(value: object) -> bytes:
    import json

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise DiagnosticSafetyError("diagnostic data is not canonical JSON") from error


__all__ = [
    "DiagnosticSafetyError",
    "REDACTED",
    "assert_safe_bytes",
    "sanitize_tree",
]
