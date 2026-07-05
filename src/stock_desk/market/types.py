from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import re
from typing import Annotated, Self, TypeAlias, cast
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)


CanonicalSymbol = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[0-9]{6}\.(?:SH|SZ|BJ)$",
    ),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        pattern=r"^\S(?:.*\S)?$",
    ),
]
FailureDetail = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=512,
        pattern=r"^\S(?:.*\S)?$",
    ),
]
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Volume = Annotated[int, Field(ge=0)]
_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MIN60_BUCKET_STARTS = frozenset({(9, 30), (10, 30), (13, 0), (14, 0)})
_DECIMAL_STRING_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_MAX_PRICE_INTEGER_DIGITS = 16
_MAX_PRICE_DECIMAL_PLACES = 8


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class Period(StrEnum):
    DAY = "1d"
    WEEK = "1w"
    MIN60 = "60m"


class Adjustment(StrEnum):
    NONE = "none"
    QFQ = "qfq"
    HFQ = "hfq"


class ProviderId(StrEnum):
    AKSHARE = "akshare"
    BAOSTOCK = "baostock"
    EASTMONEY = "eastmoney"
    TDX_LOCAL = "tdx_local"
    TUSHARE = "tushare"


class Exchange(StrEnum):
    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


class InstrumentKind(StrEnum):
    STOCK = "stock"
    INDEX = "index"
    ETF = "etf"
    FUND = "fund"
    BOND = "bond"


class ListingStatus(StrEnum):
    UNKNOWN = "unknown"
    LISTED = "listed"
    DELISTED = "delisted"


class TradingStatus(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    SUSPENDED = "suspended"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"


class MarketCapability(StrEnum):
    BARS = "bars"
    INSTRUMENTS = "instruments"
    TRADING_CALENDAR = "trading_calendar"


class FailureReason(StrEnum):
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    NO_DATA = "no_data"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TRANSIENT_FAILURE = "transient_failure"
    TIMEOUT = "timeout"
    CORRUPT = "corrupt"
    INVALID_RESPONSE = "invalid_response"
    NO_PROVIDER = "no_provider"


class CapabilityState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED = "unsupported"
    TRANSIENT_FAILURE = "transient_failure"


class _FrozenMarketModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Instrument(_FrozenMarketModel):
    symbol: CanonicalSymbol
    exchange: Exchange
    name: NonEmptyText
    instrument_kind: InstrumentKind
    listing_status: ListingStatus
    listed_on: date | None = None
    delisted_on: date | None = None

    @model_validator(mode="after")
    def validate_identity_and_dates(self) -> Self:
        if self.symbol.rsplit(".", maxsplit=1)[1] != self.exchange.value:
            raise ValueError("instrument exchange must match its symbol suffix")
        if self.delisted_on is not None and self.listed_on is None:
            raise ValueError("delisted instrument must include its listing date")
        if (
            self.listed_on is not None
            and self.delisted_on is not None
            and self.delisted_on < self.listed_on
        ):
            raise ValueError("instrument delisted date cannot precede listed date")
        if self.listing_status is ListingStatus.DELISTED:
            if self.delisted_on is None:
                raise ValueError("delisted status requires a delisted date")
        elif self.delisted_on is not None:
            raise ValueError("only delisted instruments may include a delisted date")
        return self


class TradingDay(_FrozenMarketModel):
    day: date
    exchange: Exchange
    is_open: StrictBool


class TradingSession(_FrozenMarketModel):
    opens_at: UtcDatetime
    closes_at: UtcDatetime

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.opens_at >= self.closes_at:
            raise ValueError("trading session must open before it closes")
        return self


class Provenance(_FrozenMarketModel):
    source: ProviderId
    fetched_at: UtcDatetime
    data_cutoff: UtcDatetime
    adjustment: Adjustment
    dataset_version: NonEmptyText

    @model_validator(mode="after")
    def validate_temporal_order(self) -> Self:
        if self.data_cutoff > self.fetched_at:
            raise ValueError("data cutoff cannot be later than fetch time")
        return self


def _validate_price_precision(value: Decimal) -> None:
    if not value.is_finite():
        return
    _sign, raw_digits, raw_exponent = value.as_tuple()
    if len(raw_digits) > 64:
        raise ValueError("price precision exceeds canonical bounds")
    digits = list(raw_digits)
    exponent = cast(int, raw_exponent)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    integer_digits = max(len(digits) + exponent, 0)
    decimal_places = max(-exponent, 0)
    if (
        integer_digits > _MAX_PRICE_INTEGER_DIGITS
        or decimal_places > _MAX_PRICE_DECIMAL_PLACES
        or len(digits) > _MAX_PRICE_INTEGER_DIGITS + _MAX_PRICE_DECIMAL_PLACES
    ):
        raise ValueError("price precision exceeds canonical bounds")


def _canonical_decimal_text(value: Decimal) -> str:
    _validate_price_precision(value)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _normalized_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        return value
    return Decimal(_canonical_decimal_text(value))


def _is_canonical_bucket_start(timestamp: datetime, period: Period) -> bool:
    local_timestamp = timestamp.astimezone(_MARKET_TIMEZONE)
    if local_timestamp.second != 0 or local_timestamp.microsecond != 0:
        return False
    clock = (local_timestamp.hour, local_timestamp.minute)
    if period is Period.DAY:
        return clock == (0, 0)
    if period is Period.WEEK:
        return local_timestamp.weekday() == 0 and clock == (0, 0)
    return clock in _MIN60_BUCKET_STARTS


class Bar(_FrozenMarketModel):
    symbol: CanonicalSymbol
    timestamp: UtcDatetime
    period: Period
    adjustment: Adjustment
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Volume
    status: TradingStatus = TradingStatus.UNKNOWN

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def validate_price_input(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> Decimal:
        if info.mode == "python":
            if type(value) is not Decimal:
                raise ValueError("Python price inputs must be Decimal instances")
            return _normalized_decimal(value)
        if not isinstance(value, str):
            raise ValueError("JSON price inputs must be strings")
        if len(value) > 25 or _DECIMAL_STRING_PATTERN.fullmatch(value) is None:
            raise ValueError("JSON price input must use canonical decimal syntax")
        try:
            return _normalized_decimal(Decimal(value))
        except InvalidOperation as error:
            raise ValueError("JSON price input is not a decimal") from error

    @field_serializer("open", "high", "low", "close", when_used="json")
    def serialize_price(self, value: Decimal) -> str:
        return _canonical_decimal_text(value)

    @model_validator(mode="after")
    def validate_bar(self) -> Self:
        if self.high < max(self.open, self.close):
            raise ValueError("bar high must contain open and close")
        if self.low > min(self.open, self.close):
            raise ValueError("bar low must contain open and close")
        if not _is_canonical_bucket_start(self.timestamp, self.period):
            raise ValueError("bar timestamp must be a canonical bucket start")
        return self


class BarQuery(_FrozenMarketModel):
    symbol: CanonicalSymbol
    period: Period
    adjustment: Adjustment
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.start >= self.end:
            raise ValueError("bar query start must be before end")
        return self


class CapabilityGap(_FrozenMarketModel):
    capability: MarketCapability
    state: CapabilityState
    reason: FailureReason
    detail: FailureDetail | None = None

    @model_validator(mode="after")
    def validate_failure_state(self) -> Self:
        allowed_reasons = {
            CapabilityState.UNAVAILABLE: frozenset(
                {
                    FailureReason.MISSING,
                    FailureReason.NO_DATA,
                    FailureReason.PROVIDER_UNAVAILABLE,
                    FailureReason.CORRUPT,
                    FailureReason.INVALID_RESPONSE,
                    FailureReason.NO_PROVIDER,
                }
            ),
            CapabilityState.PERMISSION_DENIED: frozenset(
                {FailureReason.PERMISSION_DENIED}
            ),
            CapabilityState.UNSUPPORTED: frozenset({FailureReason.UNSUPPORTED}),
            CapabilityState.TRANSIENT_FAILURE: frozenset(
                {
                    FailureReason.TRANSIENT_FAILURE,
                    FailureReason.TIMEOUT,
                    FailureReason.PROVIDER_UNAVAILABLE,
                }
            ),
        }
        if self.state is CapabilityState.AVAILABLE:
            raise ValueError("capability gap cannot be available")
        if self.reason not in allowed_reasons[self.state]:
            raise ValueError(
                f"{self.state.value} capability gap has an incompatible reason"
            )
        return self


class CapabilityReport(_FrozenMarketModel):
    source: ProviderId
    state: CapabilityState
    capabilities: frozenset[MarketCapability] = frozenset()
    available_periods: frozenset[Period] = frozenset()
    available_adjustments: frozenset[Adjustment] = frozenset()
    markets: frozenset[Exchange] = frozenset()
    data_cutoff: UtcDatetime | None = None
    gaps: tuple[CapabilityGap, ...] = ()

    @model_validator(mode="after")
    def validate_bar_capabilities(self) -> Self:
        has_bars = MarketCapability.BARS in self.capabilities
        has_bar_metadata = bool(
            self.available_periods
            or self.available_adjustments
            or self.markets
            or self.data_cutoff is not None
        )
        if has_bars and (
            not self.available_periods
            or not self.available_adjustments
            or not self.markets
            or self.data_cutoff is None
        ):
            raise ValueError(
                "bar capability requires periods, adjustments, markets, and cutoff"
            )
        if not has_bars and has_bar_metadata:
            raise ValueError("bar metadata requires the bar capability")
        if self.state is CapabilityState.AVAILABLE and self.gaps:
            raise ValueError("available report cannot contain gaps")
        if self.state is not CapabilityState.AVAILABLE and not self.gaps:
            raise ValueError("non-available report must explain at least one gap")
        gap_capabilities = tuple(gap.capability for gap in self.gaps)
        if len(gap_capabilities) != len(frozenset(gap_capabilities)):
            raise ValueError("global capability gaps must be unique")
        if self.capabilities.intersection(gap_capabilities):
            raise ValueError("a capability cannot be both available and unavailable")
        return self

    @field_serializer("capabilities", when_used="json")
    def serialize_capabilities(
        self,
        value: frozenset[MarketCapability],
    ) -> tuple[str, ...]:
        return tuple(sorted(item.value for item in value))

    @field_serializer("available_periods", when_used="json")
    def serialize_periods(self, value: frozenset[Period]) -> tuple[str, ...]:
        return tuple(sorted(item.value for item in value))

    @field_serializer("available_adjustments", when_used="json")
    def serialize_adjustments(
        self,
        value: frozenset[Adjustment],
    ) -> tuple[str, ...]:
        return tuple(sorted(item.value for item in value))

    @field_serializer("markets", when_used="json")
    def serialize_markets(self, value: frozenset[Exchange]) -> tuple[str, ...]:
        return tuple(sorted(item.value for item in value))


class BarResult(_FrozenMarketModel):
    query: BarQuery
    bars: tuple[Bar, ...]
    coverage_start: UtcDatetime
    coverage_end: UtcDatetime
    provenance: Provenance

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if not self.bars:
            raise ValueError("successful bar result must be nonempty")
        if (
            self.coverage_start != self.query.start
            or self.coverage_end != self.query.end
        ):
            raise ValueError("result coverage must exactly match its query")
        if self.provenance.adjustment != self.query.adjustment:
            raise ValueError("result provenance adjustment must match its query")

        previous_timestamp = None
        for value in self.bars:
            if (
                value.symbol != self.query.symbol
                or value.period != self.query.period
                or value.adjustment != self.query.adjustment
            ):
                raise ValueError("result bars must match their query")
            if not self.query.start <= value.timestamp < self.query.end:
                raise ValueError(
                    "result bar timestamp must be inside [start, end) range"
                )
            if previous_timestamp is not None and value.timestamp <= previous_timestamp:
                raise ValueError("result bars must have unique ascending timestamps")
            previous_timestamp = value.timestamp

        if self.provenance.data_cutoff < self.bars[-1].timestamp:
            raise ValueError("result cutoff must include the final bar")
        return self


class BarFailure(_FrozenMarketModel):
    query: BarQuery
    source: ProviderId | None
    reason: FailureReason
    failed_start: UtcDatetime
    failed_end: UtcDatetime
    detail: FailureDetail

    @model_validator(mode="after")
    def validate_failed_range(self) -> Self:
        if not (
            self.query.start <= self.failed_start < self.failed_end <= self.query.end
        ):
            raise ValueError("failed range must be a nonempty subset of query range")
        if self.reason is FailureReason.NO_PROVIDER:
            if self.source is not None:
                raise ValueError("source must be absent when no provider exists")
        elif self.source is None:
            raise ValueError("source is required for a provider failure")
        return self


BarFetchOutcome: TypeAlias = BarResult | BarFailure
