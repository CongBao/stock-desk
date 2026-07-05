from __future__ import annotations

from collections.abc import Hashable
from datetime import date, datetime, timedelta
from typing import Protocol, Self, cast

from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    Clock,
    InstrumentFetchOutcome,
    ProviderInvalidResponse,
    ProviderNoData,
    ProviderOperation,
    ProviderPermissionDenied,
    ProviderTimeout,
    ProviderUnsupported,
    ProviderUnavailable,
)
from stock_desk.market.providers.normalization import (
    MARKET_TIMEZONE,
    aware_now,
    bar_failure,
    batch_failure,
    complete_explicit_calendar,
    dated_cutoff,
    decimal_price,
    make_bar_result,
    make_batch,
    parse_date,
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


class BaoStockClient(Protocol):
    def query_history_k_data_plus(self, **kwargs: object) -> object: ...

    def query_stock_basic(self, **kwargs: object) -> object: ...

    def query_trade_dates(self, **kwargs: object) -> object: ...


class BaoStockSdkSession:
    def __init__(self, *, module: object) -> None:
        self._module = module
        self._closed = False

    def query_history_k_data_plus(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start", None)
        coverage_end = kwargs.pop("_coverage_end", None)
        if not isinstance(coverage_start, datetime) or not isinstance(
            coverage_end, datetime
        ):
            raise ProviderUnavailable()
        raw_frequency = kwargs.get("frequency")
        if not isinstance(raw_frequency, str):
            raise ProviderUnavailable()
        period = _SDK_PERIODS.get(raw_frequency)
        if period is None:
            raise ProviderUnavailable()
        chunks: list[tuple[dict[str, object], ...]] = []
        seen_identities: set[Hashable] = set()
        fetch = required_sdk_callable(self._module, "query_history_k_data_plus")
        for chunk_start, chunk_end in inclusive_date_chunks(
            coverage_start,
            coverage_end,
        ):
            call_kwargs = dict(kwargs)
            call_kwargs.update(
                start_date=chunk_start.strftime("%Y-%m-%d"),
                end_date=chunk_end.strftime("%Y-%m-%d"),
            )
            chunk_rows = call_sdk(
                materialize_sdk_rows,
                call_sdk(fetch, **call_kwargs),
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                period=period,
                provider_row_limit=None,
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

    def query_stock_basic(self, **kwargs: object) -> object:
        response = call_sdk(
            required_sdk_callable(self._module, "query_stock_basic"), **kwargs
        )
        return call_sdk(records_from_table, response, required=frozenset())

    def query_trade_dates(self, **kwargs: object) -> object:
        raw_end = kwargs.get("end_date")
        if not isinstance(raw_end, str):
            raise ProviderUnavailable()
        try:
            inclusive_end = datetime.strptime(raw_end, "%Y-%m-%d").date() - timedelta(
                days=1
            )
        except ValueError:
            raise ProviderUnavailable() from None
        call_kwargs = dict(kwargs)
        call_kwargs["end_date"] = inclusive_end.isoformat()
        response = call_sdk(
            required_sdk_callable(self._module, "query_trade_dates"), **call_kwargs
        )
        return call_sdk(records_from_table, response, required=frozenset())

    def close(self) -> None:
        if self._closed:
            return
        logout = required_sdk_callable(self._module, "logout")
        try:
            result = call_sdk(logout)
        except ProviderTimeout:
            raise
        except Exception:
            raise ProviderUnavailable() from None
        if getattr(result, "error_code", None) not in {"0", 0}:
            raise ProviderUnavailable()
        self._closed = True


_FREQUENCIES = {Period.DAY: "d", Period.WEEK: "w", Period.MIN60: "60"}
_SDK_PERIODS = {value: key for key, value in _FREQUENCIES.items()}
_ADJUSTMENTS = {
    Adjustment.NONE: "3",
    Adjustment.QFQ: "2",
    Adjustment.HFQ: "1",
}
_BAR_COLUMNS = frozenset({"date", "code", "open", "high", "low", "close", "volume"})
_INSTRUMENT_COLUMNS = frozenset(
    {"code", "code_name", "ipoDate", "outDate", "type", "status"}
)
_CALENDAR_COLUMNS = frozenset({"calendar_date", "is_trading_day"})


def _sdk_temporal_identity(
    row: dict[str, object],
    period: Period,
) -> tuple[date, Hashable]:
    raw_day = parse_date(row["date"])
    if raw_day.weekday() > 4:
        raise ProviderInvalidResponse()
    if period is Period.MIN60:
        _timestamp, endpoint = period_bounds(row["time"], period, compact_minute=True)
        if endpoint.astimezone(MARKET_TIMEZONE).date() != raw_day:
            raise ProviderInvalidResponse()
        return raw_day, endpoint
    return raw_day, raw_day


class BaoStockProvider:
    name = ProviderId.BAOSTOCK

    def __init__(
        self,
        *,
        client: BaoStockClient,
        clock: Clock,
        _owned_session: BaoStockSdkSession | None = None,
    ) -> None:
        self._client = client
        self._clock = clock
        self._owned_session = _owned_session

    @classmethod
    def from_sdk(cls, *, clock: Clock) -> Self:
        module = import_optional_sdk("baostock")
        login = required_sdk_callable(module, "login")
        try:
            result = call_sdk(login)
        except ProviderTimeout:
            raise
        except Exception:
            raise ProviderUnavailable() from None
        if getattr(result, "error_code", None) not in {"0", 0}:
            raise ProviderPermissionDenied()
        session = BaoStockSdkSession(module=module)
        return cls(client=session, clock=clock, _owned_session=session)

    def close(self) -> None:
        if self._owned_session is not None:
            self._owned_session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        self.close()

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(MarketCapability),
            available_periods=frozenset(Period),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset({Exchange.SH, Exchange.SZ}),
            data_cutoff=None,
            gaps=(),
        )

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        exchange = Exchange(query.symbol[-2:])
        if exchange is Exchange.BJ:
            return bar_failure(
                source=self.name,
                query=query,
                error=ProviderUnsupported(),
            )
        local_start = query.start.astimezone(MARKET_TIMEZONE)
        local_end = query.end.astimezone(MARKET_TIMEZONE)
        provider_code = f"{exchange.value.lower()}.{query.symbol[:6]}"
        try:
            fields = ["date"]
            if query.period is Period.MIN60:
                fields.append("time")
            fields.extend(["code", "open", "high", "low", "close", "volume"])
            if query.period is Period.DAY:
                fields.append("tradestatus")
            response = self._client.query_history_k_data_plus(
                code=provider_code,
                fields=",".join(fields),
                start_date=local_start.strftime("%Y-%m-%d"),
                end_date=local_end.strftime("%Y-%m-%d"),
                frequency=_FREQUENCIES[query.period],
                adjustflag=_ADJUSTMENTS[query.adjustment],
                _coverage_start=query.start,
                _coverage_end=query.end,
            )
            table = validated_bar_table(response, query)
            time_column = "time" if query.period is Period.MIN60 else "date"
            required = _BAR_COLUMNS | {time_column}
            if query.period is Period.DAY:
                required |= {"tradestatus"}
            rows = records_from_table(table, required=required)
            normalized: list[tuple[Bar, datetime]] = []
            for row in rows:
                if row["code"] != provider_code:
                    raise ValueError
                timestamp, endpoint = period_bounds(
                    row[time_column],
                    query.period,
                    compact_minute=query.period is Period.MIN60,
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
                            volume=share_volume(row["volume"], lot_size=1),
                            status=(
                                self._trading_status(row["tradestatus"])
                                if query.period is Period.DAY
                                else TradingStatus.UNKNOWN
                            ),
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
            table = self._client.query_stock_basic(code="")
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
            table = self._client.query_trade_dates(
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
            rows = records_from_table(table, required=_CALENDAR_COLUMNS)
            days: list[TradingDay] = []
            for row in rows:
                if row["is_trading_day"] not in {"0", "1"}:
                    raise ValueError
                day = parse_date(row["calendar_date"])
                days.append(
                    TradingDay(
                        day=day,
                        exchange=exchange,
                        is_open=row["is_trading_day"] == "1",
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
    def _trading_status(raw: object) -> TradingStatus:
        if type(raw) not in (str, int):
            raise ValueError
        value = str(raw)
        if value == "0":
            return TradingStatus.SUSPENDED
        if value == "1":
            return TradingStatus.NORMAL
        return TradingStatus.UNKNOWN

    @staticmethod
    def _instrument(row: dict[str, object]) -> Instrument:
        raw_code = row["code"]
        if not isinstance(raw_code, str) or len(raw_code) != 9 or raw_code[2] != ".":
            raise ValueError
        exchanges = {"sh": Exchange.SH, "sz": Exchange.SZ, "bj": Exchange.BJ}
        exchange = exchanges.get(raw_code[:2])
        digits = raw_code[3:]
        if (
            exchange is None
            or len(digits) != 6
            or not digits.isascii()
            or not digits.isdigit()
        ):
            raise ValueError
        if row["type"] != "1":
            raise ProviderUnsupported()
        listed_on = parse_date(row["ipoDate"])
        delisted_on = parse_optional_date(row["outDate"])
        if delisted_on is not None:
            status = ListingStatus.DELISTED
        elif row["status"] == "1":
            status = ListingStatus.LISTED
        else:
            status = ListingStatus.UNKNOWN
        return Instrument(
            symbol=f"{digits}.{exchange.value}",
            exchange=exchange,
            name=required_text(row["code_name"]),
            instrument_kind=InstrumentKind.STOCK,
            listing_status=status,
            listed_on=listed_on,
            delisted_on=delisted_on,
        )
