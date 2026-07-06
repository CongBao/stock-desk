"""Pure A-share execution constraints over precomputed eligibility evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from stock_desk.market.execution_status import (
    ExecutionEligibility,
    SuspensionState,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
OrderSide = Literal["buy", "sell"]


class ConstraintDecision(StrEnum):
    EXECUTABLE = "executable"
    BLOCKED = "blocked"
    DATA_INSUFFICIENT = "data_insufficient"


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    decision: ConstraintDecision
    reason: str | None = None


def assess_execution(
    *,
    side: OrderSide,
    at: datetime,
    eligibility: ExecutionEligibility | None,
    position_entry_at: datetime | None,
) -> ConstraintResult:
    if (
        eligibility is None
        or eligibility.timestamp != at
        or eligibility.trading_day != at.astimezone(SHANGHAI).date()
        or not eligibility.evidence_complete
        or eligibility.suspension_state is SuspensionState.UNKNOWN
    ):
        return ConstraintResult(
            ConstraintDecision.DATA_INSUFFICIENT,
            "data_insufficient_execution_status",
        )
    if not eligibility.is_exchange_open:
        return ConstraintResult(ConstraintDecision.BLOCKED, "exchange_closed")
    if eligibility.suspension_state is SuspensionState.SUSPENDED:
        return ConstraintResult(ConstraintDecision.BLOCKED, "suspended")
    if side == "buy" and eligibility.buy_blocked_at_open:
        return ConstraintResult(ConstraintDecision.BLOCKED, "limit_up")
    if side == "sell" and eligibility.sell_blocked_at_open:
        return ConstraintResult(ConstraintDecision.BLOCKED, "limit_down")
    if side == "sell":
        if position_entry_at is None:
            return ConstraintResult(
                ConstraintDecision.DATA_INSUFFICIENT,
                "data_insufficient_position",
            )
        if (
            position_entry_at.astimezone(SHANGHAI).date()
            >= at.astimezone(SHANGHAI).date()
        ):
            return ConstraintResult(ConstraintDecision.BLOCKED, "t_plus_one")
    return ConstraintResult(ConstraintDecision.EXECUTABLE)


__all__ = [
    "ConstraintDecision",
    "ConstraintResult",
    "SHANGHAI",
    "assess_execution",
]
