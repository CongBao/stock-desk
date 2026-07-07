from __future__ import annotations

from pydantic import ValidationError
import pytest

from stock_desk.analysis.content_policy import ContentPolicyError
from stock_desk.analysis.data_service import ResearchDataUnavailable
from stock_desk.analysis.providers.base import (
    ModelAuthenticationError,
    ModelDNSResolutionError,
    ModelInvalidResponseError,
    ModelRateLimitError,
    ModelServerError,
    ModelTimeoutError,
    ModelUnsafeEndpointError,
)
from stock_desk.analysis.retry import RetryPolicy, classify_retry
from stock_desk.analysis.roles import RoleOutputValidationError
from stock_desk.analysis.snapshot import ResearchMissingReason, ResearchSectionKind


def data_error(reason: ResearchMissingReason) -> ResearchDataUnavailable:
    return ResearchDataUnavailable(
        kind=ResearchSectionKind.MARKET,
        reason=reason,
        attempted_sources=("fixture",),
        unsafe_context="secret-token-must-never-persist",
    )


def test_retry_policy_uses_user_limit_and_capped_exponential_backoff() -> None:
    policy = RetryPolicy(
        max_retries=4,
        base_delay_seconds=0.5,
        max_delay_seconds=1.5,
    )

    assert policy.max_attempts == 5
    assert tuple(policy.delay_before_attempt(attempt) for attempt in range(2, 6)) == (
        0.5,
        1.0,
        1.5,
        1.5,
    )
    with pytest.raises(ValueError):
        policy.delay_before_attempt(1)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=6)


@pytest.mark.parametrize(
    "error,code",
    [
        (ModelTimeoutError("secret"), "model_timeout"),
        (ModelRateLimitError("secret"), "model_rate_limit"),
        (ModelServerError("secret"), "model_server"),
        (data_error(ResearchMissingReason.TIMEOUT), "data_timeout"),
        (
            data_error(ResearchMissingReason.PROVIDER_UNAVAILABLE),
            "data_provider_unavailable",
        ),
    ],
)
def test_only_explicit_transient_failures_are_retryable(
    error: Exception,
    code: str,
) -> None:
    decision = classify_retry(error)

    assert decision.retryable is True
    assert decision.code == code
    assert "secret" not in decision.safe_message
    assert "token" not in decision.safe_message


@pytest.mark.parametrize(
    "error",
    [
        ModelAuthenticationError("secret"),
        ModelDNSResolutionError("secret"),
        ModelUnsafeEndpointError("secret"),
        ModelInvalidResponseError("secret"),
        RoleOutputValidationError("secret"),
        ContentPolicyError("secret"),
        data_error(ResearchMissingReason.PERMISSION_DENIED),
        data_error(ResearchMissingReason.NO_DATA),
        data_error(ResearchMissingReason.INVALID_RESPONSE),
    ],
)
def test_auth_validation_policy_unsafe_and_terminal_data_errors_never_retry(
    error: Exception,
) -> None:
    decision = classify_retry(error)

    assert decision.retryable is False
    assert decision.code
    assert "secret" not in decision.safe_message
    assert "token" not in decision.safe_message
