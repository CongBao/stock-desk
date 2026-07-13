"""Ephemeral identities for local worker sessions."""

from __future__ import annotations

import re
import secrets


_SAFE_PREFIX = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def new_worker_id(prefix: str) -> str:
    """Return a non-persistent, host-independent identifier for one worker session."""
    if _SAFE_PREFIX.fullmatch(prefix) is None:
        raise ValueError("worker id prefix must be a short lowercase slug")
    return f"{prefix}-{secrets.token_hex(16)}"
