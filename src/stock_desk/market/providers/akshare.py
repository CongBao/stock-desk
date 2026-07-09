from __future__ import annotations

from collections.abc import Hashable
from datetime import date, datetime
from typing import Protocol, Self, cast

from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    Clock,
    InstrumentFetchOutcome,
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
    import_optional_sdk,
    inclusive_date_chunks,
    materialize_sdk_rows,
    required_sdk_callable,
    validate_sdk_chunk_rows,
)
from stock_desk.market.execution_status import ExecutionStatusQuery
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

    def stock_zh_a_hist_min_em(self, **kwargs: object) -> object: ...

    def stock_info_a_code_name(self) -> object: ...


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
        chunks: list[tuple[dict[str, object], ...]] = []
        seen_identities: set[Hashable] = set()
        fetch = required_sdk_callable(self._module, "stock_zh_a_hist")
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
                temporal_identity=_sdk_temporal_identity,
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
        return call_sdk(required_sdk_callable(self._module, "stock_info_a_code_name"))


_PERIODS = {Period.DAY: "daily", Period.WEEK: "weekly"}
_SDK_PERIODS = {value: key for key, value in _PERIODS.items()}
_SDK_BAR_ROW_LIMIT = 1_000_000
_BAR_COLUMNS = frozenset({"日期", "股票代码", "开盘", "最高", "最低", "收盘", "成交量"})
_B_SHARE_PREFIXES = ("200", "900")


def _sdk_temporal_identity(row: dict[str, object]) -> tuple[date, Hashable]:
    raw_day = parse_date(row["日期"])
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
        return cls(client=AkShareSdkFacade(module=module), clock=clock)

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(
                {MarketCapability.BARS, MarketCapability.INSTRUMENTS}
            ),
            available_periods=frozenset({Period.DAY, Period.WEEK}),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset(Exchange),
            data_cutoff=None,
            gaps=(
                CapabilityGap(
                    capability=MarketCapability.EXECUTION_STATUS,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                    detail="AKShare does not prove historical suspension and limits",
                ),
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
        return ExecutionStatusFailure(
            query=query,
            source=self.name,
            reason=FailureReason.UNSUPPORTED,
            detail="provider does not support authoritative execution status",
        )

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
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
            rows = records_from_table(table, required=_BAR_COLUMNS)
            normalized: list[tuple[Bar, datetime]] = []
            for row in rows:
                if row["股票代码"] != query.symbol[:6]:
                    raise ValueError
                timestamp, endpoint = period_bounds(row["日期"], query.period)
                normalized.append(
                    (
                        Bar(
                            symbol=query.symbol,
                            timestamp=timestamp,
                            period=query.period,
                            adjustment=query.adjustment,
                            open=decimal_price(row["开盘"], query.adjustment),
                            high=decimal_price(row["最高"], query.adjustment),
                            low=decimal_price(row["最低"], query.adjustment),
                            close=decimal_price(row["收盘"], query.adjustment),
                            volume=share_volume(row["成交量"], lot_size=100),
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
