from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, model_validator

from stock_desk.analysis.data_service import ResearchDataUnavailable
from stock_desk.analysis.providers.base import (
    ModelAuthenticationError,
    ModelDNSResolutionError,
    ModelInvalidResponseError,
    ModelRateLimitError,
    ModelServerError,
    ModelTimeoutError,
    ModelTransportError,
    ModelUnsafeEndpointError,
)
from stock_desk.analysis.snapshot import ResearchMissingReason


MAX_USER_RETRIES: Final = 5


class RetryPolicy(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )

    max_retries: int = Field(default=2, ge=0, le=MAX_USER_RETRIES)
    base_delay_seconds: StrictFloat = Field(default=0.5, gt=0.0, le=60.0)
    max_delay_seconds: StrictFloat = Field(default=8.0, gt=0.0, le=60.0)

    @model_validator(mode="after")
    def validate_delay_cap(self) -> Self:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry delay cap cannot be below its base")
        return self

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def delay_before_attempt(self, attempt: int) -> float:
        if type(attempt) is not int or attempt < 2 or attempt > self.max_attempts:
            raise ValueError("retry attempt is outside the configured policy")
        return float(
            min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2 ** (attempt - 2)),
            )
        )


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    code: str
    safe_message: str


_MODEL_FAILURES: Final = (
    (
        ModelTimeoutError,
        RetryDecision(True, "model_timeout", "model request timed out"),
    ),
    (
        ModelRateLimitError,
        RetryDecision(True, "model_rate_limit", "model request was rate limited"),
    ),
    (
        ModelServerError,
        RetryDecision(
            True, "model_server", "model provider is temporarily unavailable"
        ),
    ),
    (
        ModelAuthenticationError,
        RetryDecision(False, "model_authentication", "model authentication failed"),
    ),
    (
        ModelTransportError,
        RetryDecision(False, "model_transport", "model provider transport failed"),
    ),
    (
        ModelDNSResolutionError,
        RetryDecision(False, "model_dns", "model hostname could not be resolved"),
    ),
    (
        ModelUnsafeEndpointError,
        RetryDecision(False, "model_unsafe_endpoint", "model endpoint is unsafe"),
    ),
    (
        ModelInvalidResponseError,
        RetryDecision(False, "model_invalid_response", "model response is invalid"),
    ),
)


def classify_retry(error: BaseException) -> RetryDecision:
    for error_type, decision in _MODEL_FAILURES:
        if isinstance(error, error_type):
            return decision
    if isinstance(error, ResearchDataUnavailable):
        if error.reason is ResearchMissingReason.TIMEOUT:
            return RetryDecision(
                True, "data_timeout", "research data request timed out"
            )
        if error.reason is ResearchMissingReason.PROVIDER_UNAVAILABLE:
            return RetryDecision(
                True,
                "data_provider_unavailable",
                "research data provider is temporarily unavailable",
            )
        return RetryDecision(
            False,
            f"data_{error.reason.value}",
            "research data is unavailable",
        )
    return RetryDecision(False, "validation_failure", "analysis input is invalid")
