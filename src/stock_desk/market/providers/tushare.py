from __future__ import annotations

from collections.abc import Callable, Hashable
from datetime import date, datetime, timedelta
from typing import Protocol, Self, cast

from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    Clock,
    InstrumentFetchOutcome,
    ProviderNoData,
    ProviderInvalidResponse,
    ProviderOperation,
    ProviderPermissionDenied,
    ProviderTimeout,
    ProviderUnavailable,
)
from stock_desk.market.providers.normalization import (
    MARKET_TIMEZONE,
    aware_now,
    bar_failure,
    batch_failure,
    binary_flag,
    complete_explicit_calendar,
    dated_cutoff,
    decimal_price,
    make_bar_result,
    make_batch,
    parse_date,
    parse_datetime,
    parse_optional_date,
    period_bounds,
    records_from_table,
    require_unique,
    required_text,
    share_volume,
    validated_bar_table,
)
from stock_desk.market.providers.sdk import (
    call_sdk,
    combine_complete_chunks,
    import_optional_sdk,
    inclusive_date_chunks,
    is_sdk_timeout,
    materialize_sdk_rows,
    required_sdk_callable,
    validate_sdk_chunk_rows,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFetchOutcome,
    BarQuery,
    CapabilityReport,
    CapabilityState,
    Exchange,
    Instrument,
    InstrumentKind,
    ListingStatus,
    MarketCapability,
    Period,
    ProviderId,
    TradingDay,
    TradingStatus,
)


class TushareClient(Protocol):
    def pro_bar(self, **kwargs: object) -> object: ...

    def stock_basic(self, **kwargs: object) -> object: ...

    def trade_cal(self, **kwargs: object) -> object: ...


class _TrackedSdkFailure(Exception):
    pass


class _FailureTrackingProxy:
    def __init__(self, target: object) -> None:
        self._target = target
        self.failures: list[Exception] = []

    def __getattr__(self, name: str) -> object:
        value = getattr(self._target, name)
        if not callable(value):
            return value

        def tracked(*args: object, **kwargs: object) -> object:
            try:
                return value(*args, **kwargs)
            except Exception as error:
                self.failures.append(error)
            raise _TrackedSdkFailure() from None

        return tracked


def _call_pro_bar_with_failure_tracking(
    pro_bar: Callable[..., object],
    *,
    pro: object,
    call_kwargs: dict[str, object],
) -> object:
    tracked_pro = _FailureTrackingProxy(pro)
    try:
        return pro_bar(**call_kwargs, api=tracked_pro, retry_count=1)
    except Exception:
        if len(tracked_pro.failures) == 1 and is_sdk_timeout(tracked_pro.failures[0]):
            raise ProviderTimeout() from None
        raise ProviderInvalidResponse() from None


class TushareSdkFacade:
    def __init__(self, *, module: object, pro: object) -> None:
        self._module = module
        self._pro = pro

    def pro_bar(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start", None)
        coverage_end = kwargs.pop("_coverage_end", None)
        if not isinstance(coverage_start, datetime) or not isinstance(
            coverage_end, datetime
        ):
            raise ProviderUnavailable()
        frequency = kwargs.get("freq")
        if not isinstance(frequency, str):
            raise ProviderUnavailable()
        period = _SDK_PERIODS.get(frequency)
        if period is None:
            raise ProviderUnavailable()
        chunks: list[tuple[dict[str, object], ...]] = []
        seen_identities: set[Hashable] = set()
        pro_bar = required_sdk_callable(self._module, "pro_bar")
        for chunk_start, chunk_end in inclusive_date_chunks(
            coverage_start,
            coverage_end,
        ):
            call_kwargs = dict(kwargs)
            call_kwargs.update(
                start_date=chunk_start.strftime("%Y%m%d"),
                end_date=chunk_end.strftime("%Y%m%d"),
            )
            chunk_rows = materialize_sdk_rows(
                _call_pro_bar_with_failure_tracking(
                    pro_bar,
                    pro=self._pro,
                    call_kwargs=call_kwargs,
                ),
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                period=period,
                provider_row_limit=_SDK_BAR_ROW_LIMITS[period],
            )
            validate_sdk_chunk_rows(
                chunk_rows,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                temporal_identity=lambda row: _sdk_temporal_identity(row, period),
                seen_identities=seen_identities,
            )
            chunks.append(chunk_rows)
        return combine_complete_chunks(
            tuple(chunks),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def stock_basic(self, **kwargs: object) -> object:
        stock_basic = required_sdk_callable(self._pro, "stock_basic")
        rows: list[dict[str, object]] = []
        for status in ("L", "D", "P"):
            call_kwargs = dict(kwargs)
            call_kwargs["list_status"] = status
            rows.extend(
                records_from_table(
                    call_sdk(stock_basic, **call_kwargs),
                    required=frozenset(),
                )
            )
        return tuple(rows)

    def trade_cal(self, **kwargs: object) -> object:
        raw_end = kwargs.get("end_date")
        if not isinstance(raw_end, str):
            raise ProviderUnavailable()
        try:
            inclusive_end = datetime.strptime(raw_end, "%Y%m%d").date() - timedelta(
                days=1
            )
        except ValueError:
            raise ProviderUnavailable() from None
        call_kwargs = dict(kwargs)
        call_kwargs["end_date"] = inclusive_end.strftime("%Y%m%d")
        return call_sdk(required_sdk_callable(self._pro, "trade_cal"), **call_kwargs)


_FREQUENCIES = {
    Period.DAY: "D",
    Period.WEEK: "W",
    Period.MIN60: "60min",
}
_SDK_PERIODS = {value: key for key, value in _FREQUENCIES.items()}
_SDK_BAR_ROW_LIMITS = {
    Period.DAY: 6_000,
    Period.WEEK: 6_000,
    Period.MIN60: 8_000,
}


def _sdk_temporal_identity(
    row: dict[str, object],
    period: Period,
) -> tuple[date, Hashable]:
    if period is Period.MIN60:
        raw_day = parse_datetime(row["trade_time"]).date()
    else:
        raw_day = parse_date(row["trade_date"], compact=True)
    if raw_day.weekday() > 4:
        raise ProviderInvalidResponse()
    if period is Period.MIN60:
        _timestamp, endpoint = period_bounds(row["trade_time"], period)
        return raw_day, endpoint
    return raw_day, raw_day


_CALENDAR_EXCHANGES = {
    Exchange.SH: "SSE",
    Exchange.SZ: "SZSE",
    Exchange.BJ: "BSE",
}
_INSTRUMENT_EXCHANGES = {value: key for key, value in _CALENDAR_EXCHANGES.items()}
_BAR_COLUMNS = frozenset({"ts_code", "open", "high", "low", "close", "vol"})
_INSTRUMENT_COLUMNS = frozenset(
    {
        "ts_code",
        "name",
        "exchange",
        "list_status",
        "list_date",
        "delist_date",
    }
)
_CALENDAR_COLUMNS = frozenset({"exchange", "cal_date", "is_open"})


class TushareProvider:
    name = ProviderId.TUSHARE

    def __init__(self, *, client: TushareClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    @classmethod
    def from_sdk(cls, *, token: str, clock: Clock) -> Self:
        if not isinstance(token, str) or not token:
            raise ProviderPermissionDenied()
        module = import_optional_sdk("tushare")
        pro_api = required_sdk_callable(module, "pro_api")
        try:
            pro = call_sdk(pro_api, token)
        except ProviderTimeout:
            raise
        except Exception:
            raise ProviderPermissionDenied() from None
        if pro is None:
            raise ProviderPermissionDenied()
        return cls(client=TushareSdkFacade(module=module, pro=pro), clock=clock)

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(MarketCapability),
            available_periods=frozenset(Period),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset(Exchange),
            data_cutoff=None,
            gaps=(),
        )

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        local_start = query.start.astimezone(MARKET_TIMEZONE)
        local_end = query.end.astimezone(MARKET_TIMEZONE)
        try:
            response = self._client.pro_bar(
                ts_code=query.symbol,
                start_date=local_start.strftime("%Y%m%d"),
                end_date=local_end.strftime("%Y%m%d"),
                freq=_FREQUENCIES[query.period],
                adj=None
                if query.adjustment is Adjustment.NONE
                else query.adjustment.value,
                _coverage_start=query.start,
                _coverage_end=query.end,
            )
            table = validated_bar_table(response, query)
            time_column = "trade_time" if query.period is Period.MIN60 else "trade_date"
            rows = records_from_table(table, required=_BAR_COLUMNS | {time_column})
            normalized: list[tuple[Bar, datetime]] = []
            for row in rows:
                if row["ts_code"] != query.symbol:
                    raise ValueError
                timestamp, endpoint = period_bounds(
                    row[time_column],
                    query.period,
                    compact_date=query.period is not Period.MIN60,
                )
                normalized.append(
                    (
                        Bar(
                            symbol=query.symbol,
                            timestamp=timestamp,
                            period=query.period,
                            adjustment=query.adjustment,
                            open=decimal_price(row["open"], query.adjustment),
                            high=decimal_price(row["high"], query.adjustment),
                            low=decimal_price(row["low"], query.adjustment),
                            close=decimal_price(row["close"], query.adjustment),
                            volume=share_volume(row["vol"], lot_size=100),
                            status=TradingStatus.UNKNOWN,
                        ),
                        endpoint,
                    )
                )
            return make_bar_result(
                source=self.name,
                query=query,
                normalized=normalized,
                clock=self._clock,
            )
        except Exception as error:
            return bar_failure(source=self.name, query=query, error=error)

    def fetch_instruments(self) -> InstrumentFetchOutcome:
        try:
            table = self._client.stock_basic(
                exchange="",
                list_status="",
                fields="ts_code,name,exchange,market,list_status,list_date,delist_date",
            )
            rows = records_from_table(table, required=_INSTRUMENT_COLUMNS)
            instruments = tuple(
                sorted(
                    (self._instrument(row) for row in rows),
                    key=lambda item: item.symbol,
                )
            )
            if not instruments:
                raise ProviderNoData()
            require_unique(tuple(item.symbol for item in instruments))
            observed_at = aware_now(self._clock)
            return cast(
                InstrumentFetchOutcome,
                make_batch(
                    source=self.name,
                    operation=ProviderOperation.INSTRUMENTS,
                    request={},
                    items=instruments,
                    data_cutoff=observed_at,
                    observed_at=observed_at,
                ),
            )
        except Exception as error:
            return batch_failure(
                source=self.name,
                operation=ProviderOperation.INSTRUMENTS,
                error=error,
            )

    def fetch_calendar(
        self,
        exchange: Exchange,
        start: date,
        end: date,
    ) -> CalendarFetchOutcome:
        if start >= end:
            raise ValueError("calendar range must be nonempty")
        try:
            provider_exchange = _CALENDAR_EXCHANGES[exchange]
            table = self._client.trade_cal(
                exchange=provider_exchange,
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                fields="exchange,cal_date,is_open",
            )
            rows = records_from_table(table, required=_CALENDAR_COLUMNS)
            days: list[TradingDay] = []
            for row in rows:
                if row["exchange"] != provider_exchange:
                    raise ValueError
                day = parse_date(row["cal_date"], compact=True)
                days.append(
                    TradingDay(
                        day=day,
                        exchange=exchange,
                        is_open=binary_flag(row["is_open"]),
                    )
                )
            items = complete_explicit_calendar(
                days,
                exchange=exchange,
                start=start,
                end=end,
            )
            if not items:
                raise ProviderNoData()
            observed_at = aware_now(self._clock)
            return cast(
                CalendarFetchOutcome,
                make_batch(
                    source=self.name,
                    operation=ProviderOperation.CALENDAR,
                    request={"exchange": exchange, "start": start, "end": end},
                    items=items,
                    data_cutoff=min(dated_cutoff(items[-1].day), observed_at),
                    observed_at=observed_at,
                ),
            )
        except Exception as error:
            return batch_failure(
                source=self.name,
                operation=ProviderOperation.CALENDAR,
                error=error,
                exchange=exchange,
                start=start,
                end=end,
            )

    @staticmethod
    def _instrument(row: dict[str, object]) -> Instrument:
        raw_exchange = required_text(row["exchange"])
        exchange = _INSTRUMENT_EXCHANGES.get(raw_exchange)
        if exchange is None or not isinstance(row["ts_code"], str):
            raise ValueError
        listed_on = parse_date(row["list_date"], compact=True)
        delisted_on = parse_optional_date(row["delist_date"], compact=True)
        statuses = {
            "L": ListingStatus.LISTED,
            "D": ListingStatus.DELISTED,
            "P": ListingStatus.UNKNOWN,
        }
        raw_status = required_text(row["list_status"])
        status = statuses.get(raw_status)
        if status is None:
            raise ValueError
        return Instrument(
            symbol=row["ts_code"],
            exchange=exchange,
            name=required_text(row["name"]),
            instrument_kind=InstrumentKind.STOCK,
            listing_status=status,
            listed_on=listed_on,
            delisted_on=delisted_on,
        )
