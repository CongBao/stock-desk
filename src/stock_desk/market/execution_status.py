"""Immutable evidence used to decide whether an A-share open can execute."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_desk.market.types import (
    CanonicalSymbol,
    Exchange,
    Period,
    Price,
    ProviderId,
    UtcDatetime,
)


class SuspensionState(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    SUSPENDED = "suspended"
    NOT_APPLICABLE = "not_applicable"


class ExecutionStatusEvidenceLevel(StrEnum):
    AUTHORITATIVE = "authoritative"
    BASIC_NO_PRICE_LIMITS = "basic_no_price_limits"


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _FrozenExecutionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ExecutionStatusQuery(_FrozenExecutionModel):
    symbol: CanonicalSymbol
    exchange: Exchange
    start: date
    end: date
    period: Period = Period.DAY

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if self.start >= self.end:
            raise ValueError("execution-status range must be nonempty")
        if self.symbol.rsplit(".", maxsplit=1)[1] != self.exchange.value:
            raise ValueError("execution-status exchange must match symbol")
        if (self.end - self.start).days > 366 * 50:
            raise ValueError("execution-status range is too large")
        return self


class ExecutionStatusDay(_FrozenExecutionModel):
    day: date
    exchange: Exchange
    is_exchange_open: bool
    suspension_state: SuspensionState
    raw_upper_limit: Price | None
    raw_lower_limit: Price | None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.is_exchange_open:
            if self.suspension_state not in {
                SuspensionState.NORMAL,
                SuspensionState.SUSPENDED,
            }:
                raise ValueError("open day requires explicit suspension evidence")
            has_upper = self.raw_upper_limit is not None
            has_lower = self.raw_lower_limit is not None
            if has_upper != has_lower:
                raise ValueError("price-limit evidence must be complete or absent")
            if has_upper:
                assert self.raw_upper_limit is not None
                assert self.raw_lower_limit is not None
                if (
                    self.raw_upper_limit <= 0
                    or self.raw_lower_limit <= 0
                    or self.raw_lower_limit > self.raw_upper_limit
                ):
                    raise ValueError("price-limit evidence is invalid")
        elif (
            self.suspension_state is not SuspensionState.NOT_APPLICABLE
            or self.raw_upper_limit is not None
            or self.raw_lower_limit is not None
        ):
            raise ValueError("closed day must use not-applicable empty evidence")
        return self


class RawExecutionOpen(_FrozenExecutionModel):
    timestamp: UtcDatetime
    trading_day: date
    raw_open: Price = Field(gt=0)


class ExecutionEligibility(_FrozenExecutionModel):
    timestamp: UtcDatetime
    trading_day: date
    is_exchange_open: bool
    suspension_state: SuspensionState
    buy_blocked_at_open: bool
    sell_blocked_at_open: bool
    evidence_complete: bool

    @model_validator(mode="after")
    def validate_complete_evidence(self) -> Self:
        if self.evidence_complete and (
            not self.is_exchange_open
            or self.suspension_state is SuspensionState.UNKNOWN
            or self.suspension_state is SuspensionState.NOT_APPLICABLE
        ):
            raise ValueError("complete eligibility must identify an open trading day")
        return self


class ExecutionStatusSnapshot(_FrozenExecutionModel):
    query: ExecutionStatusQuery
    days: tuple[ExecutionStatusDay, ...]
    eligibility: tuple[ExecutionEligibility, ...]
    source: ProviderId
    evidence_level: ExecutionStatusEvidenceLevel = (
        ExecutionStatusEvidenceLevel.AUTHORITATIVE
    )
    fetched_at: UtcDatetime
    data_cutoff: UtcDatetime
    dataset_version: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        expected_days = tuple(
            self.query.start + timedelta(days=offset)
            for offset in range((self.query.end - self.query.start).days)
        )
        if tuple(item.day for item in self.days) != expected_days:
            raise ValueError("execution status must cover every natural date")
        if any(item.exchange is not self.query.exchange for item in self.days):
            raise ValueError("execution status day exchange must match query")
        open_days = tuple(item for item in self.days if item.is_exchange_open)
        if self.evidence_level is ExecutionStatusEvidenceLevel.AUTHORITATIVE:
            if any(
                item.raw_upper_limit is None or item.raw_lower_limit is None
                for item in open_days
            ):
                raise ValueError("authoritative status requires price-limit evidence")
        elif any(
            item.raw_upper_limit is not None or item.raw_lower_limit is not None
            for item in open_days
        ):
            raise ValueError("basic status must not claim price-limit evidence")
        if (
            self.evidence_level is ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS
            and any(
                item.buy_blocked_at_open or item.sell_blocked_at_open
                for item in self.eligibility
            )
        ):
            raise ValueError("basic status must not claim price-limit blocking")
        if self.data_cutoff > self.fetched_at:
            raise ValueError("execution-status cutoff cannot follow fetch time")
        previous: datetime | None = None
        for item in self.eligibility:
            if not self.query.start <= item.trading_day < self.query.end:
                raise ValueError("eligibility timestamp falls outside query")
            if previous is not None and item.timestamp <= previous:
                raise ValueError("eligibility timestamps must be unique and ascending")
            previous = item.timestamp
        expected_version = _dataset_version(
            query=self.query,
            days=self.days,
            eligibility=self.eligibility,
            source=self.source,
            evidence_level=self.evidence_level,
            data_cutoff=self.data_cutoff,
        )
        if self.dataset_version != expected_version:
            raise ValueError(
                "execution-status dataset version does not match canonical evidence"
            )
        return self


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f").rstrip("0").rstrip(".")
    return text or "0"


def _dataset_version(
    *,
    query: ExecutionStatusQuery,
    days: tuple[ExecutionStatusDay, ...],
    eligibility: tuple[ExecutionEligibility, ...],
    source: ProviderId,
    evidence_level: ExecutionStatusEvidenceLevel,
    data_cutoff: datetime,
) -> str:
    payload = {
        "schema": "stock-desk-execution-status-v1",
        "query": query.model_dump(mode="json"),
        "source": source.value,
        "data_cutoff": data_cutoff.astimezone(timezone.utc).isoformat(),
        "days": [
            {
                "day": item.day.isoformat(),
                "exchange": item.exchange.value,
                "is_exchange_open": item.is_exchange_open,
                "suspension_state": item.suspension_state.value,
                "raw_upper_limit": _decimal_text(item.raw_upper_limit),
                "raw_lower_limit": _decimal_text(item.raw_lower_limit),
            }
            for item in days
        ],
        "eligibility": [item.model_dump(mode="json") for item in eligibility],
    }
    # Preserve validation of existing authoritative v1 snapshots while making
    # the weaker evidence grade part of every basic dataset identity.
    if evidence_level is not ExecutionStatusEvidenceLevel.AUTHORITATIVE:
        payload["evidence_level"] = evidence_level.value
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def materialize_execution_status(
    *,
    query: ExecutionStatusQuery,
    days: tuple[ExecutionStatusDay, ...],
    raw_opens: tuple[RawExecutionOpen, ...],
    source: ProviderId,
    fetched_at: datetime,
    data_cutoff: datetime,
    evidence_level: ExecutionStatusEvidenceLevel = (
        ExecutionStatusEvidenceLevel.AUTHORITATIVE
    ),
) -> ExecutionStatusSnapshot:
    """Join unadjusted opens to raw limits once, before adjusted fills are used."""
    validated_days = tuple(days)
    by_day = {item.day: item for item in validated_days}
    eligibility: list[ExecutionEligibility] = []
    raw_days: set[date] = set()
    for raw_open in sorted(raw_opens, key=lambda item: item.timestamp):
        status = by_day.get(raw_open.trading_day)
        if status is None:
            raise ValueError("raw open lacks calendar-bearing status evidence")
        if not status.is_exchange_open:
            raise ValueError("raw open cannot be joined to a closed exchange day")
        raw_days.add(raw_open.trading_day)
        has_price_limits = (
            status.raw_upper_limit is not None and status.raw_lower_limit is not None
        )
        eligibility.append(
            ExecutionEligibility(
                timestamp=raw_open.timestamp,
                trading_day=raw_open.trading_day,
                is_exchange_open=True,
                suspension_state=status.suspension_state,
                buy_blocked_at_open=(
                    raw_open.raw_open >= status.raw_upper_limit
                    if has_price_limits and status.raw_upper_limit is not None
                    else False
                ),
                sell_blocked_at_open=(
                    raw_open.raw_open <= status.raw_lower_limit
                    if has_price_limits and status.raw_lower_limit is not None
                    else False
                ),
                evidence_complete=True,
            )
        )
    if query.period in {Period.DAY, Period.WEEK}:
        for status in validated_days:
            if not status.is_exchange_open or status.day in raw_days:
                continue
            eligibility.append(
                ExecutionEligibility(
                    timestamp=datetime.combine(
                        status.day,
                        datetime.min.time().replace(hour=9, minute=30),
                        tzinfo=_SHANGHAI,
                    ),
                    trading_day=status.day,
                    is_exchange_open=True,
                    suspension_state=status.suspension_state,
                    buy_blocked_at_open=False,
                    sell_blocked_at_open=False,
                    evidence_complete=(
                        status.suspension_state is SuspensionState.SUSPENDED
                    ),
                )
            )
    canonical_cutoff = data_cutoff.astimezone(timezone.utc)
    canonical_fetched = fetched_at.astimezone(timezone.utc)
    frozen_eligibility = tuple(sorted(eligibility, key=lambda item: item.timestamp))
    return ExecutionStatusSnapshot(
        query=query,
        days=validated_days,
        eligibility=frozen_eligibility,
        source=source,
        evidence_level=evidence_level,
        fetched_at=canonical_fetched,
        data_cutoff=canonical_cutoff,
        dataset_version=_dataset_version(
            query=query,
            days=validated_days,
            eligibility=frozen_eligibility,
            source=source,
            evidence_level=evidence_level,
            data_cutoff=canonical_cutoff,
        ),
    )


__all__ = [
    "ExecutionEligibility",
    "ExecutionStatusEvidenceLevel",
    "ExecutionStatusDay",
    "ExecutionStatusQuery",
    "ExecutionStatusSnapshot",
    "RawExecutionOpen",
    "SuspensionState",
    "materialize_execution_status",
]
