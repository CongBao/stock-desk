from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeAlias, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, StrictBool, model_validator

from stock_desk.market.types import (
    BarFetchOutcome,
    BarQuery,
    CapabilityReport,
    Exchange,
    FailureDetail,
    FailureReason,
    Instrument,
    NonEmptyText,
    ProviderId,
    TradingDay,
    UtcDatetime,
)


Clock: TypeAlias = Callable[[], datetime]
BatchItem = TypeVar("BatchItem", Instrument, TradingDay)


class ProviderOperation(StrEnum):
    INSTRUMENTS = "instruments"
    CALENDAR = "calendar"


class _FrozenProviderModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DatasetProvenance(_FrozenProviderModel):
    source: ProviderId
    fetched_at: UtcDatetime
    data_cutoff: UtcDatetime
    dataset_version: NonEmptyText

    @model_validator(mode="after")
    def validate_temporal_order(self) -> DatasetProvenance:
        if self.data_cutoff > self.fetched_at:
            raise ValueError("data cutoff cannot be later than fetch time")
        return self


class ProviderBarTable(_FrozenProviderModel):
    table: object
    coverage_start: UtcDatetime
    coverage_end: UtcDatetime
    complete: StrictBool
    limit_reached: StrictBool = False

    @model_validator(mode="after")
    def validate_coverage(self) -> ProviderBarTable:
        if self.coverage_start >= self.coverage_end:
            raise ValueError("provider bar coverage must be nonempty")
        return self


class ProviderBatch(_FrozenProviderModel, Generic[BatchItem]):
    items: tuple[BatchItem, ...]
    provenance: DatasetProvenance

    @model_validator(mode="after")
    def validate_nonempty(self) -> ProviderBatch[BatchItem]:
        if not self.items:
            raise ValueError("successful provider batch must be nonempty")
        return self


class ProviderBatchFailure(_FrozenProviderModel):
    source: ProviderId
    operation: ProviderOperation
    exchange: Exchange | None = None
    start: date | None = None
    end: date | None = None
    reason: FailureReason
    detail: FailureDetail

    @model_validator(mode="after")
    def validate_request_context(self) -> ProviderBatchFailure:
        if self.reason is FailureReason.NO_PROVIDER:
            raise ValueError("provider failures cannot use the router-only reason")
        if self.operation is ProviderOperation.INSTRUMENTS:
            if (
                self.exchange is not None
                or self.start is not None
                or self.end is not None
            ):
                raise ValueError("instrument failure cannot contain calendar context")
            return self
        if self.exchange is None or self.start is None or self.end is None:
            raise ValueError("calendar failure requires exchange and date range")
        if self.start >= self.end:
            raise ValueError("calendar failure range must be nonempty")
        return self


InstrumentFetchOutcome: TypeAlias = ProviderBatch[Instrument] | ProviderBatchFailure
CalendarFetchOutcome: TypeAlias = ProviderBatch[TradingDay] | ProviderBatchFailure


class ProviderClientError(Exception):
    reason: FailureReason = FailureReason.INVALID_RESPONSE
    safe_detail: str = "provider response is invalid"

    def __init__(self, *_unsafe_context: object) -> None:
        super().__init__()


class ProviderPermissionDenied(ProviderClientError):
    reason = FailureReason.PERMISSION_DENIED
    safe_detail = "provider permission was denied"


class ProviderUnsupported(ProviderClientError):
    reason = FailureReason.UNSUPPORTED
    safe_detail = "provider does not support this request"


class ProviderTransientFailure(ProviderClientError):
    reason = FailureReason.TRANSIENT_FAILURE
    safe_detail = "provider failed transiently"


class ProviderTimeout(ProviderClientError):
    reason = FailureReason.TIMEOUT
    safe_detail = "provider request timed out"


class ProviderUnavailable(ProviderClientError):
    reason = FailureReason.PROVIDER_UNAVAILABLE
    safe_detail = "provider is unavailable"


class ProviderInvalidResponse(ProviderClientError):
    reason = FailureReason.INVALID_RESPONSE
    safe_detail = "provider response is invalid"


class ProviderNoData(ProviderClientError):
    reason = FailureReason.NO_DATA
    safe_detail = "provider returned no data"


class ProviderMissingCoverage(ProviderClientError):
    reason = FailureReason.MISSING
    safe_detail = "provider response does not cover the full request"


@runtime_checkable
class MarketDataProvider(Protocol):
    name: ProviderId

    def capabilities(self) -> CapabilityReport: ...

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome: ...

    def fetch_instruments(self) -> InstrumentFetchOutcome: ...

    def fetch_calendar(
        self,
        exchange: Exchange,
        start: date,
        end: date,
    ) -> CalendarFetchOutcome: ...
