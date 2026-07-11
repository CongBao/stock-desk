from __future__ import annotations

import pytest

from scripts.signpath_contract import SignPathContractError, evaluate_signing_contract


SHA = "a" * 40
DIGEST = "b" * 64


def test_pending_application_keeps_integration_disabled() -> None:
    decision = evaluate_signing_contract(
        status="application-submitted",
        enabled=False,
        source_sha=SHA,
        payload_digest=DIGEST,
        proof_digest=DIGEST,
        secrets={},
    )

    assert decision == {
        "enabled": False,
        "reason": "signpath-application-not-integrated",
        "status": "application-submitted",
    }


@pytest.mark.parametrize(
    "status",
    ["application-submitted", "pending-review", "approved", "SmartScreen-verified"],
)
def test_non_integrated_state_cannot_enable_signing(status: str) -> None:
    with pytest.raises(SignPathContractError, match="integrated"):
        evaluate_signing_contract(
            status=status,
            enabled=True,
            source_sha=SHA,
            payload_digest=DIGEST,
            proof_digest=DIGEST,
            secrets={"SIGNPATH_API_TOKEN": "secret"},
        )


def test_integrated_signing_fails_closed_without_required_secrets() -> None:
    with pytest.raises(SignPathContractError, match="SIGNPATH_API_TOKEN"):
        evaluate_signing_contract(
            status="integrated",
            enabled=True,
            source_sha=SHA,
            payload_digest=DIGEST,
            proof_digest=DIGEST,
            secrets={},
        )


def test_integrated_signing_requires_exact_immutable_identities() -> None:
    with pytest.raises(SignPathContractError, match="source_sha"):
        evaluate_signing_contract(
            status="integrated",
            enabled=True,
            source_sha="main",
            payload_digest=DIGEST,
            proof_digest=DIGEST,
            secrets={
                "SIGNPATH_API_TOKEN": "secret",
                "SIGNPATH_ORGANIZATION_ID": "org",
                "SIGNPATH_PROJECT_SLUG": "stock-desk",
                "SIGNPATH_SIGNING_POLICY_SLUG": "release-signing",
            },
        )
