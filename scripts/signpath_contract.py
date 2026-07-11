"""Fail-closed activation contract for the future SignPath integration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TypedDict


_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_STATES = {
    "application-submitted",
    "pending-review",
    "approved",
    "integrated",
    "SmartScreen-verified",
}
_REQUIRED_SECRETS = (
    "SIGNPATH_API_TOKEN",
    "SIGNPATH_ORGANIZATION_ID",
    "SIGNPATH_PROJECT_SLUG",
    "SIGNPATH_SIGNING_POLICY_SLUG",
)


class SignPathContractError(ValueError):
    """Raised when a signing request is not eligible to reach SignPath."""


class SigningDecision(TypedDict):
    enabled: bool
    reason: str
    status: str


def evaluate_signing_contract(
    *,
    status: str,
    enabled: bool,
    source_sha: str,
    payload_digest: str,
    proof_digest: str,
    secrets: Mapping[str, str],
) -> SigningDecision:
    """Validate state and identity without exposing secret values."""
    if status not in _KNOWN_STATES:
        raise SignPathContractError("unknown SignPath application state")
    if not enabled:
        return SigningDecision(
            enabled=False,
            reason="signpath-application-not-integrated",
            status=status,
        )
    if status != "integrated":
        raise SignPathContractError(
            "SignPath signing requires the explicit integrated state"
        )
    if _SHA.fullmatch(source_sha) is None:
        raise SignPathContractError("source_sha must be an exact Git commit")
    for name, value in (
        ("payload_digest", payload_digest),
        ("proof_digest", proof_digest),
    ):
        if _DIGEST.fullmatch(value) is None:
            raise SignPathContractError(f"{name} must be a SHA-256 digest")
    missing = [name for name in _REQUIRED_SECRETS if not secrets.get(name)]
    if missing:
        raise SignPathContractError(
            "missing required SignPath secret names: " + ", ".join(missing)
        )
    return SigningDecision(
        enabled=True,
        reason="exact-proof-and-payload-eligible-for-manual-approval",
        status=status,
    )
