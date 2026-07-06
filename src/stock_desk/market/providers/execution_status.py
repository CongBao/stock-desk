"""Provider boundary for complete A-share execution-status evidence."""

from __future__ import annotations

from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, model_validator

from stock_desk.market.execution_status import (
    ExecutionStatusQuery,
    ExecutionStatusSnapshot,
)
from stock_desk.market.types import FailureDetail, FailureReason, ProviderId


class ExecutionStatusFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    query: ExecutionStatusQuery
    source: ProviderId
    reason: FailureReason
    detail: FailureDetail

    @model_validator(mode="after")
    def validate_reason(self) -> "ExecutionStatusFailure":
        if self.reason is FailureReason.NO_PROVIDER:
            raise ValueError("NO_PROVIDER is router-only")
        return self


ExecutionStatusFetchOutcome: TypeAlias = (
    ExecutionStatusSnapshot | ExecutionStatusFailure
)


__all__ = ["ExecutionStatusFailure", "ExecutionStatusFetchOutcome"]
