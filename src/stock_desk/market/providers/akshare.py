from __future__ import annotations

from collections.abc import Callable, Hashable
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol, Self, cast

from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    Clock,
    InstrumentFetchOutcome,
    ProviderClientError,
    ProviderInvalidResponse,
    ProviderNoData,
    ProviderOperation,
    ProviderUnsupported,
    ProviderUnavailable,
)
from stock_desk.market.providers.normalization import (
    MARKET_TIMEZONE,
    aware_now,
    bar_failure,
    batch_failure,
    decimal_price,
    make_bar_result,
    make_batch,
    parse_date,
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
    complete_bar_table,
    import_optional_sdk,
    inclusive_date_chunks,
    materialize_sdk_rows,
    required_sdk_callable,
    validate_sdk_chunk_rows,
)
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusEvidenceLevel,
    ExecutionStatusQuery,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.providers.execution_status import (
    ExecutionStatusFailure,
    ExecutionStatusFetchOutcome,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFetchOutcome,
    BarQuery,
    CapabilityGap,
    CapabilityReport,
    CapabilityState,
    Exchange,
    FailureReason,
    Instrument,
    InstrumentKind,
    ListingStatus,
    MarketCapability,
    Period,
    ProviderId,
    TradingStatus,
)


class AkShareClient(Protocol):
    def stock_zh_a_hist(self, **kwargs: object) -> object: ...

    def stock_zh_a_daily(self, **kwargs: object) -> object: ...

    def stock_zh_a_hist_min_em(self, **kwargs: object) -> object: ...

    def stock_info_a_code_name(self) -> object: ...

    def stock_zh_index_spot_sina(self) -> object: ...

    def stock_zh_index_daily(self, **kwargs: object) -> object: ...

    def tool_trade_date_hist_sina(self) -> object: ...


class AkShareSdkFacade:
    def __init__(self, *, module: object) -> None:
        self._module = module

    def stock_zh_a_hist(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start", None)
        coverage_end = kwargs.pop("_coverage_end", None)
        if not isinstance(coverage_start, datetime) or not isinstance(
            coverage_end, datetime
        ):
            raise ProviderUnavailable()
        raw_period = kwargs.get("period")
        if not isinstance(raw_period, str):
            raise ProviderUnavailable()
        period = _SDK_PERIODS.get(raw_period)
        if period is None:
            raise ProviderUnavailable()
        try:
            return self._complete_stock_rows(
                fetch=required_sdk_callable(self._module, "stock_zh_a_hist"),
                kwargs=kwargs,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                period=period,
                temporal_identity=_sdk_temporal_identity,
            )
        except Exception:
            if period is not Period.DAY:
                raise
            raw_symbol = kwargs.get("symbol")
            if not isinstance(raw_symbol, str):
                raise ProviderUnavailable()
            exchange = _stock_exchange(raw_symbol)
            if exchange not in {Exchange.SH, Exchange.SZ}:
                raise ProviderUnsupported()
            return self.stock_zh_a_daily(
                symbol=f"{exchange.value.lower()}{raw_symbol}",
                adjust=kwargs.get("adjust", ""),
                _coverage_start=coverage_start,
                _coverage_end=coverage_end,
            )

    def stock_zh_a_daily(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start", None)
        coverage_end = kwargs.pop("_coverage_end", None)
        if not isinstance(coverage_start, datetime) or not isinstance(
            coverage_end, datetime
        ):
            raise ProviderUnavailable()
        return self._complete_stock_rows(
            fetch=required_sdk_callable(self._module, "stock_zh_a_daily"),
            kwargs=kwargs,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            period=Period.DAY,
            temporal_identity=_sdk_sina_temporal_identity,
        )

    @staticmethod
    def _complete_stock_rows(
        *,
        fetch: Callable[..., object],
        kwargs: dict[str, object],
        coverage_start: datetime,
        coverage_end: datetime,
        period: Period,
        temporal_identity: Callable[[dict[str, object]], tuple[date, Hashable]],
    ) -> object:
        chunks: list[tuple[dict[str, object], ...]] = []
        seen_identities: set[Hashable] = set()
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
                call_sdk(fetch, **call_kwargs),
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                period=period,
                provider_row_limit=_SDK_BAR_ROW_LIMIT,
            )
            validate_sdk_chunk_rows(
                chunk_rows,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                temporal_identity=temporal_identity,
                seen_identities=seen_identities,
            )
            chunks.append(chunk_rows)
        return combine_complete_chunks(
            tuple(chunks),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def stock_zh_a_hist_min_em(self, **kwargs: object) -> object:
        raise ProviderUnsupported()

    def stock_info_a_code_name(self) -> object:
        try:
            return call_sdk(
                required_sdk_callable(self._module, "stock_info_a_code_name")
            )
        except Exception:
            response = call_sdk(required_sdk_callable(self._module, "stock_zh_a_spot"))
            rows = records_from_table(
                response,
                required=frozenset({"代码", "名称"}),
            )
            projected: list[dict[str, object]] = []
            exchanges = {"sh": Exchange.SH, "sz": Exchange.SZ, "bj": Exchange.BJ}
            for row in rows:
                provider_code = required_text(row["代码"])
                prefix, code = provider_code[:2], provider_code[2:]
                if (
                    prefix not in exchanges
                    or len(provider_code) != 8
                    or len(code) != 6
                    or not code.isascii()
                    or not code.isdigit()
                    or _stock_exchange(code) is not exchanges[prefix]
                ):
                    raise ProviderInvalidResponse()
                projected.append({"code": code, "name": required_text(row["名称"])})
            if not projected:
                raise ProviderNoData()
            return tuple(projected)

    def stock_zh_index_spot_sina(self) -> object:
        return call_sdk(required_sdk_callable(self._module, "stock_zh_index_spot_sina"))

    def stock_zh_index_daily(self, **kwargs: object) -> object:
        coverage_start = kwargs.pop("_coverage_start", None)
        coverage_end = kwargs.pop("_coverage_end", None)
        symbol = kwargs.get("symbol")
        if (
            not isinstance(coverage_start, datetime)
            or not isinstance(coverage_end, datetime)
            or symbol != "sh000001"
        ):
            raise ProviderUnavailable()
        response = call_sdk(
            required_sdk_callable(self._module, "stock_zh_index_daily"),
            symbol=symbol,
        )
        start_day = coverage_start.astimezone(MARKET_TIMEZONE).date()
        end_day = coverage_end.astimezone(MARKET_TIMEZONE).date()
        rows = tuple(
            row
            for row in records_from_table(response, required=frozenset())
            if start_day <= parse_date(row["date"]) < end_day
        )
        materialized = materialize_sdk_rows(
            rows,
            chunk_start=start_day,
            chunk_end=end_day,
            period=Period.DAY,
            provider_row_limit=_SDK_BAR_ROW_LIMIT,
        )
        validate_sdk_chunk_rows(
            materialized,
            chunk_start=start_day,
            chunk_end=end_day,
            temporal_identity=_sdk_index_temporal_identity,
            seen_identities=set(),
        )
        return complete_bar_table(
            materialized,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

    def tool_trade_date_hist_sina(self) -> object:
        return call_sdk(
            required_sdk_callable(self._module, "tool_trade_date_hist_sina")
        )


_PERIODS = {Period.DAY: "daily", Period.WEEK: "weekly"}
_SDK_PERIODS = {value: key for key, value in _PERIODS.items()}
_SDK_BAR_ROW_LIMIT = 1_000_000
_BAR_COLUMNS = frozenset({"日期", "股票代码", "开盘", "最高", "最低", "收盘", "成交量"})
_SINA_BAR_COLUMNS = frozenset({"date", "open", "high", "low", "close", "volume"})
_INDEX_BAR_COLUMNS = frozenset({"date", "open", "high", "low", "close", "volume"})
_B_SHARE_PREFIXES = ("200", "900")


def _sdk_temporal_identity(row: dict[str, object]) -> tuple[date, Hashable]:
    raw_day = parse_date(row["日期"])
    if raw_day.weekday() > 4:
        raise ProviderInvalidResponse()
    return raw_day, raw_day


def _sdk_index_temporal_identity(row: dict[str, object]) -> tuple[date, Hashable]:
    raw_day = parse_date(row["date"])
    if raw_day.weekday() > 4:
        raise ProviderInvalidResponse()
    return raw_day, raw_day


def _sdk_sina_temporal_identity(row: dict[str, object]) -> tuple[date, Hashable]:
    raw_day = parse_date(row["date"])
    if raw_day.weekday() > 4:
        raise ProviderInvalidResponse()
    return raw_day, raw_day


def _stock_exchange(code: str) -> Exchange | None:
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return Exchange.SH
    if code.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return Exchange.SZ
    if code.startswith(("43", "83", "87", "88", "92")):
        return Exchange.BJ
    return None


class AkShareProvider:
    name = ProviderId.AKSHARE

    def __init__(self, *, client: AkShareClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    @classmethod
    def from_sdk(cls, *, clock: Clock) -> Self:
        module = import_optional_sdk("akshare")
        required_sdk_callable(module, "stock_zh_a_hist")
        required_sdk_callable(module, "stock_zh_a_daily")
        required_sdk_callable(module, "stock_zh_a_spot")
        required_sdk_callable(module, "stock_zh_index_daily")
        required_sdk_callable(module, "stock_zh_index_spot_sina")
        required_sdk_callable(module, "tool_trade_date_hist_sina")
        return cls(client=AkShareSdkFacade(module=module), clock=clock)

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(
                {
                    MarketCapability.BARS,
                    MarketCapability.EXECUTION_STATUS,
                    MarketCapability.INSTRUMENTS,
                }
            ),
            available_periods=frozenset({Period.DAY, Period.WEEK}),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset(Exchange),
            data_cutoff=None,
            gaps=(
                CapabilityGap(
                    capability=MarketCapability.TRADING_CALENDAR,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                    detail="AKShare open dates lack explicit closed-day coverage",
                ),
            ),
        )

    def fetch_execution_status(
        self, query: ExecutionStatusQuery
    ) -> ExecutionStatusFetchOutcome:
        if query.period is Period.MIN60 or query.exchange is Exchange.BJ:
            return ExecutionStatusFailure(
                query=query,
                source=self.name,
                reason=FailureReason.UNSUPPORTED,
                detail="provider does not support this execution-status request",
            )
        try:
            calendar_rows = records_from_table(
                self._client.tool_trade_date_hist_sina(),
                required=frozenset({"trade_date"}),
            )
            all_trade_days = tuple(
                parse_date(row["trade_date"]) for row in calendar_rows
            )
            require_unique(all_trade_days)
            if (
                not all_trade_days
                or min(all_trade_days) > query.start
                or max(all_trade_days) < query.end - timedelta(days=1)
            ):
                raise ProviderInvalidResponse()
            open_days = frozenset(
                day for day in all_trade_days if query.start <= day < query.end
            )
            if not open_days:
                raise ProviderNoData()

            coverage_start = datetime.combine(
                query.start, time.min, tzinfo=MARKET_TIMEZONE
            ).astimezone(timezone.utc)
            coverage_end = datetime.combine(
                query.end, time.min, tzinfo=MARKET_TIMEZONE
            ).astimezone(timezone.utc)
            response = self._client.stock_zh_a_daily(
                symbol=f"{query.exchange.value.lower()}{query.symbol[:6]}",
                adjust="",
                _coverage_start=coverage_start,
                _coverage_end=coverage_end,
            )
            table = validated_bar_table(
                response,
                BarQuery(
                    symbol=query.symbol,
                    instrument_kind=InstrumentKind.STOCK,
                    period=Period.DAY,
                    adjustment=Adjustment.NONE,
                    start=coverage_start,
                    end=coverage_end,
                ),
            )
            rows = records_from_table(table, required=_SINA_BAR_COLUMNS)
            raw_opens_by_day: dict[date, object] = {}
            for row in rows:
                day = parse_date(row["date"])
                if (
                    day in raw_opens_by_day
                    or share_volume(row["volume"], lot_size=1) <= 0
                ):
                    raise ProviderInvalidResponse()
                raw_opens_by_day[day] = row["open"]
            if frozenset(raw_opens_by_day) != open_days:
                raise ProviderInvalidResponse()

            days = tuple(
                ExecutionStatusDay(
                    day=day,
                    exchange=query.exchange,
                    is_exchange_open=day in open_days,
                    suspension_state=(
                        SuspensionState.NORMAL
                        if day in open_days
                        else SuspensionState.NOT_APPLICABLE
                    ),
                    raw_upper_limit=None,
                    raw_lower_limit=None,
                )
                for offset in range((query.end - query.start).days)
                for day in (query.start + timedelta(days=offset),)
            )
            raw_opens = tuple(
                RawExecutionOpen(
                    timestamp=datetime.combine(
                        day, time(9, 30), tzinfo=MARKET_TIMEZONE
                    ),
                    trading_day=day,
                    raw_open=decimal_price(raw_opens_by_day[day], Adjustment.NONE),
                )
                for day in sorted(open_days)
            )
            observed_at = aware_now(self._clock)
            return materialize_execution_status(
                query=query,
                days=days,
                raw_opens=raw_opens,
                source=self.name,
                fetched_at=observed_at,
                data_cutoff=min(
                    period_bounds(max(open_days), Period.DAY)[1], observed_at
                ),
                evidence_level=ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS,
            )
        except Exception as error:
            reason = (
                error.reason
                if isinstance(error, ProviderClientError)
                else FailureReason.INVALID_RESPONSE
            )
            detail = (
                error.safe_detail
                if isinstance(error, ProviderClientError)
                else "provider response is invalid"
            )
            return ExecutionStatusFailure(
                query=query,
                source=self.name,
                reason=reason,
                detail=detail,
            )

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        if query.instrument_kind is InstrumentKind.INDEX:
            return self._fetch_index_bars(query)
        expected_exchange = _stock_exchange(query.symbol[:6])
        requested_exchange = Exchange(query.symbol[-2:])
        if expected_exchange is None or expected_exchange is not requested_exchange:
            return bar_failure(
                source=self.name,
                query=query,
                error=ProviderUnsupported(),
            )
        if query.period is Period.MIN60:
            return bar_failure(
                source=self.name,
                query=query,
                error=ProviderUnsupported(),
                detail="AKShare minute history is recent-only and unsupported",
            )
        local_start = query.start.astimezone(MARKET_TIMEZONE)
        local_end = query.end.astimezone(MARKET_TIMEZONE)
        try:
            response = self._client.stock_zh_a_hist(
                symbol=query.symbol[:6],
                period=_PERIODS[query.period],
                start_date=local_start.strftime("%Y%m%d"),
                end_date=local_end.strftime("%Y%m%d"),
                adjust=""
                if query.adjustment is Adjustment.NONE
                else query.adjustment.value,
                _coverage_start=query.start,
                _coverage_end=query.end,
            )
            table = validated_bar_table(response, query)
            rows = records_from_table(table, required=frozenset())
            if not rows:
                raise ProviderNoData()
            uses_primary_schema = all(_BAR_COLUMNS.issubset(row) for row in rows)
            uses_sina_schema = all(_SINA_BAR_COLUMNS.issubset(row) for row in rows)
            if uses_primary_schema == uses_sina_schema:
                raise ProviderInvalidResponse()
            normalized: list[tuple[Bar, datetime]] = []
            for row in rows:
                if uses_primary_schema and row["股票代码"] != query.symbol[:6]:
                    raise ValueError
                date_field = "日期" if uses_primary_schema else "date"
                timestamp, endpoint = period_bounds(row[date_field], query.period)
                open_field = "开盘" if uses_primary_schema else "open"
                high_field = "最高" if uses_primary_schema else "high"
                low_field = "最低" if uses_primary_schema else "low"
                close_field = "收盘" if uses_primary_schema else "close"
                volume_field = "成交量" if uses_primary_schema else "volume"
                normalized.append(
                    (
                        Bar(
                            symbol=query.symbol,
                            timestamp=timestamp,
                            period=query.period,
                            adjustment=query.adjustment,
                            open=decimal_price(row[open_field], query.adjustment),
                            high=decimal_price(row[high_field], query.adjustment),
                            low=decimal_price(row[low_field], query.adjustment),
                            close=decimal_price(row[close_field], query.adjustment),
                            volume=share_volume(
                                row[volume_field],
                                lot_size=100 if uses_primary_schema else 1,
                            ),
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

    def _fetch_index_bars(self, query: BarQuery) -> BarFetchOutcome:
        if (
            query.symbol != "000001.SS"
            or query.period is not Period.DAY
            or query.adjustment is not Adjustment.NONE
        ):
            return bar_failure(
                source=self.name,
                query=query,
                error=ProviderUnsupported(),
            )
        try:
            response = self._client.stock_zh_index_daily(
                symbol="sh000001",
                start_date=query.start.astimezone(MARKET_TIMEZONE).strftime("%Y%m%d"),
                end_date=query.end.astimezone(MARKET_TIMEZONE).strftime("%Y%m%d"),
                _coverage_start=query.start,
                _coverage_end=query.end,
            )
            table = validated_bar_table(response, query)
            rows = records_from_table(table, required=_INDEX_BAR_COLUMNS)
            normalized: list[tuple[Bar, datetime]] = []
            for row in rows:
                timestamp, endpoint = period_bounds(row["date"], query.period)
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
            table = self._client.stock_info_a_code_name()
            rows = records_from_table(table, required=frozenset())
            code_field, name_field = self._instrument_schema(rows)
            selected: list[Instrument] = []
            for row in rows:
                raw_code = self._instrument_code(row[code_field])
                if raw_code.startswith(_B_SHARE_PREFIXES):
                    continue
                selected.append(
                    self._instrument(
                        row,
                        code_field=code_field,
                        name_field=name_field,
                    )
                )
            index_rows = records_from_table(
                self._client.stock_zh_index_spot_sina(),
                required=frozenset({"代码", "名称"}),
            )
            for row in index_rows:
                if row["代码"] != "sh000001":
                    continue
                selected.append(
                    Instrument(
                        symbol="000001.SS",
                        exchange=Exchange.SH,
                        name=required_text(row["名称"]),
                        instrument_kind=InstrumentKind.INDEX,
                        listing_status=ListingStatus.UNKNOWN,
                        listed_on=None,
                        delisted_on=None,
                    )
                )
            instruments = tuple(sorted(selected, key=lambda item: item.symbol))
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
        return batch_failure(
            source=self.name,
            operation=ProviderOperation.CALENDAR,
            error=ProviderUnsupported(),
            exchange=exchange,
            start=start,
            end=end,
        )

    @staticmethod
    def _instrument_schema(
        rows: tuple[dict[str, object], ...],
    ) -> tuple[str, str]:
        if not rows:
            return "code", "name"
        keys = frozenset(rows[0])
        if keys == frozenset({"code", "name"}):
            fields = ("code", "name")
        elif keys == frozenset({"代码", "名称"}):
            fields = ("代码", "名称")
        else:
            raise ValueError
        if any(frozenset(row) != keys for row in rows[1:]):
            raise ValueError
        return fields

    @staticmethod
    def _instrument(
        row: dict[str, object],
        *,
        code_field: str,
        name_field: str,
    ) -> Instrument:
        raw_code = AkShareProvider._instrument_code(row[code_field])
        exchange = _stock_exchange(raw_code)
        if exchange is None:
            raise ProviderUnsupported()
        return Instrument(
            symbol=f"{raw_code}.{exchange.value}",
            exchange=exchange,
            name=required_text(row[name_field]),
            instrument_kind=InstrumentKind.STOCK,
            listing_status=ListingStatus.UNKNOWN,
            listed_on=None,
            delisted_on=None,
        )

    @staticmethod
    def _instrument_code(raw_code: object) -> str:
        if (
            not isinstance(raw_code, str)
            or len(raw_code) != 6
            or not raw_code.isascii()
            or not raw_code.isdigit()
        ):
            raise ValueError
        return raw_code
