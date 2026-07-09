from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stock_desk.market.providers.base import (
    MarketDataProvider,
    ProviderBatch,
    ProviderBatchFailure,
    ProviderInvalidResponse,
    ProviderOperation,
    ProviderBarTable,
    ProviderPermissionDenied,
    ProviderTimeout,
    ProviderTransientFailure,
    ProviderUnavailable,
    ProviderUnsupported,
)
from stock_desk.market.types import (
    Adjustment,
    BarFailure,
    BarQuery,
    BarResult,
    CapabilityState,
    Exchange,
    FailureReason,
    MarketCapability,
    Period,
    ProviderId,
    TradingStatus,
)
from tests.contract.providers.conftest import (
    FETCHED_AT,
    SECRET_SENTINEL,
    ProviderCase,
    load_fixture,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[3]


def market_time(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2024, 7, day, hour, minute, tzinfo=SHANGHAI)


def query(
    period: Period,
    adjustment: Adjustment = Adjustment.NONE,
    *,
    symbol: str = "600000.SH",
) -> BarQuery:
    if period is Period.DAY:
        start, end = market_time(1), market_time(3)
    elif period is Period.WEEK:
        start, end = market_time(1), market_time(8)
    else:
        start, end = market_time(1, 9, 30), market_time(1, 15)
    return BarQuery(
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        start=start,
        end=end,
    )


def provider_and_client(
    case: ProviderCase,
    **kwargs: object,
) -> tuple[MarketDataProvider, object]:
    provider, client = case.build(**kwargs)
    return cast(MarketDataProvider, provider), client


def test_provider_package_imports_without_sdks_or_network() -> None:
    script = """
import socket
import sys
socket.socket = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('network'))
for name in ('tushare', 'akshare', 'baostock', 'pandas'):
    sys.modules[name] = None
import stock_desk.market.providers
"""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_provider_implements_protocol_and_truthful_static_capabilities(
    provider_case: ProviderCase,
) -> None:
    provider, client = provider_and_client(provider_case)

    assert isinstance(provider, MarketDataProvider)
    assert provider.name is provider_case.source
    report = provider.capabilities()
    assert report.source is provider_case.source
    assert report.state is CapabilityState.AVAILABLE
    expected_periods = (
        frozenset({Period.DAY, Period.WEEK})
        if provider_case.source is ProviderId.AKSHARE
        else frozenset(Period)
    )
    expected_markets = (
        frozenset({Exchange.SH, Exchange.SZ})
        if provider_case.source is ProviderId.BAOSTOCK
        else frozenset(Exchange)
    )
    assert report.available_periods == expected_periods
    assert report.available_adjustments == frozenset(Adjustment)
    assert report.markets == expected_markets
    assert report.data_cutoff is None
    if provider_case.source is ProviderId.AKSHARE:
        assert report.capabilities == frozenset(
            {MarketCapability.BARS, MarketCapability.INSTRUMENTS}
        )
        assert len(report.gaps) == 2
        gap = next(
            item
            for item in report.gaps
            if item.capability is MarketCapability.TRADING_CALENDAR
        )
        assert gap.capability is MarketCapability.TRADING_CALENDAR
        assert gap.state is CapabilityState.UNSUPPORTED
        assert gap.reason is FailureReason.UNSUPPORTED
        assert gap.detail is not None and "closed-day" in gap.detail
    elif provider_case.source is ProviderId.TUSHARE:
        assert report.capabilities == frozenset(MarketCapability)
        assert report.gaps == ()
    else:
        assert report.capabilities == frozenset(
            {
                MarketCapability.BARS,
                MarketCapability.INSTRUMENTS,
                MarketCapability.TRADING_CALENDAR,
            }
        )
        assert {gap.capability for gap in report.gaps} == {
            MarketCapability.EXECUTION_STATUS
        }
    assert client.calls == []


@pytest.mark.parametrize("period", list(Period))
@pytest.mark.parametrize("adjustment", list(Adjustment))
def test_bar_contract_normalizes_schema_units_order_and_provenance(
    provider_case: ProviderCase,
    period: Period,
    adjustment: Adjustment,
) -> None:
    provider, _client = provider_and_client(provider_case)
    bar_query = query(period, adjustment)

    outcome = provider.fetch_bars(bar_query)

    if provider_case.source is ProviderId.AKSHARE and period is Period.MIN60:
        assert isinstance(outcome, BarFailure)
        assert outcome.reason is FailureReason.UNSUPPORTED
        assert "recent" in outcome.detail.lower()
        return

    assert isinstance(outcome, BarResult)
    assert outcome.query == bar_query
    assert outcome.coverage_start == bar_query.start
    assert outcome.coverage_end == bar_query.end
    assert outcome.provenance.source is provider_case.source
    assert outcome.provenance.adjustment is adjustment
    assert outcome.provenance.fetched_at == FETCHED_AT
    assert outcome.provenance.dataset_version.startswith("sha256:")
    assert tuple(bar.timestamp for bar in outcome.bars) == tuple(
        sorted(bar.timestamp for bar in outcome.bars)
    )
    assert all(bar.timestamp.tzinfo is timezone.utc for bar in outcome.bars)
    assert all(bar.symbol == "600000.SH" for bar in outcome.bars)
    assert all(bar.period is period for bar in outcome.bars)
    assert all(bar.adjustment is adjustment for bar in outcome.bars)
    expected_volumes = {
        Period.DAY: (1000, 0),
        Period.WEEK: (3000,),
        Period.MIN60: (100, 200, 300, 400),
    }
    assert tuple(bar.volume for bar in outcome.bars) == expected_volumes[period]
    if period is Period.MIN60:
        assert tuple(
            bar.timestamp.astimezone(SHANGHAI).time() for bar in outcome.bars
        ) == (time(9, 30), time(10, 30), time(13), time(14))
    elif period is Period.WEEK:
        assert outcome.bars[0].timestamp.astimezone(SHANGHAI) == market_time(1)
    else:
        assert all(
            bar.timestamp.astimezone(SHANGHAI).time() == time(0) for bar in outcome.bars
        )
    expected_cutoff = {
        Period.DAY: market_time(2, 15),
        Period.WEEK: market_time(5, 15),
        Period.MIN60: market_time(1, 15),
    }
    assert outcome.provenance.data_cutoff == expected_cutoff[period]


def test_zero_volume_never_guesses_suspension(provider_case: ProviderCase) -> None:
    provider, _client = provider_and_client(provider_case)
    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarResult)
    zero_volume = next(bar for bar in outcome.bars if bar.volume == 0)
    if provider_case.source is ProviderId.BAOSTOCK:
        assert zero_volume.status is TradingStatus.SUSPENDED
    else:
        assert zero_volume.status is not TradingStatus.SUSPENDED


def test_tushare_and_akshare_never_infer_trading_status() -> None:
    for case in _provider_cases():
        if case.source is ProviderId.BAOSTOCK:
            continue
        provider, _client = provider_and_client(case)
        outcome = provider.fetch_bars(query(Period.DAY))
        assert isinstance(outcome, BarResult)
        assert {bar.status for bar in outcome.bars} == {TradingStatus.UNKNOWN}


def test_akshare_preserves_negative_adjusted_prices_without_clamping() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.AKSHARE)
    fixture = load_fixture("akshare")
    fixture["bars"]["1d"] = [fixture["adjusted_negative_bar"]]
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    adjusted = provider.fetch_bars(query(Period.DAY, Adjustment.QFQ))
    unadjusted = provider.fetch_bars(query(Period.DAY, Adjustment.NONE))

    assert isinstance(adjusted, BarResult)
    assert adjusted.bars[0].open == Decimal("-0.2")
    assert adjusted.bars[0].low == Decimal("-0.3")
    assert adjusted.bars[0].close == Decimal("0")
    assert isinstance(unadjusted, BarFailure)
    assert unadjusted.reason is FailureReason.INVALID_RESPONSE


def test_baostock_trade_status_is_explicitly_mapped() -> None:
    case = next(
        case for case in _provider_cases() if case.source is ProviderId.BAOSTOCK
    )
    provider, _client = provider_and_client(case)
    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarResult)
    assert tuple(bar.status for bar in outcome.bars) == (
        TradingStatus.NORMAL,
        TradingStatus.SUSPENDED,
    )


@pytest.mark.parametrize("period", [Period.WEEK, Period.MIN60])
def test_baostock_non_daily_bars_have_unknown_status(period: Period) -> None:
    case = next(
        case for case in _provider_cases() if case.source is ProviderId.BAOSTOCK
    )
    provider, _client = provider_and_client(case)

    outcome = provider.fetch_bars(query(period))

    assert isinstance(outcome, BarResult)
    assert {bar.status for bar in outcome.bars} == {TradingStatus.UNKNOWN}


def _provider_cases() -> tuple[ProviderCase, ...]:
    from tests.contract.providers.conftest import PROVIDER_CASES

    return PROVIDER_CASES


@pytest.mark.parametrize("table_style", ["list", "frame"])
def test_record_lists_and_dataframe_like_tables_are_supported(
    provider_case: ProviderCase,
    table_style: str,
) -> None:
    provider, _client = provider_and_client(provider_case, table_style=table_style)

    assert isinstance(provider.fetch_bars(query(Period.DAY)), BarResult)


@pytest.mark.parametrize(
    "coverage_mutation",
    ["partial", "limit-reached", "start-mismatch", "end-mismatch"],
)
def test_bar_success_requires_exact_complete_coverage_witness(
    provider_case: ProviderCase,
    coverage_mutation: str,
) -> None:
    provider, client = provider_and_client(provider_case)
    client.bar_coverage_mutation = coverage_mutation

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.MISSING


def test_adapter_rejects_raw_bar_table_without_coverage_witness(
    provider_case: ProviderCase,
) -> None:
    provider, client = provider_and_client(provider_case)
    client.return_raw_bar_table = True

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_bar_coverage_witness_is_frozen_strict_and_nonempty() -> None:
    bar_query = query(Period.DAY)
    witness = ProviderBarTable(
        table=({"row": 1},),
        coverage_start=bar_query.start,
        coverage_end=bar_query.end,
        complete=True,
        limit_reached=False,
    )

    assert witness.complete is True
    with pytest.raises(ValidationError, match="frozen"):
        witness.complete = False
    with pytest.raises(ValidationError):
        ProviderBarTable(
            table=(),
            coverage_start=bar_query.start,
            coverage_end=bar_query.start,
            complete=True,
            limit_reached=False,
        )
    with pytest.raises(ValidationError):
        ProviderBarTable(
            table=(),
            coverage_start=bar_query.start,
            coverage_end=bar_query.end,
            complete=1,
            limit_reached=False,
        )


@pytest.mark.parametrize(
    ("source", "symbols"),
    [
        (ProviderId.TUSHARE, ("600000.SH", "000001.SZ", "920000.BJ")),
        (ProviderId.AKSHARE, ("600000.SH", "000001.SZ", "920000.BJ")),
        (ProviderId.BAOSTOCK, ("600000.SH", "000001.SZ")),
    ],
)
def test_declared_market_symbol_mappings_are_fixture_proven(
    source: ProviderId,
    symbols: tuple[str, ...],
) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    provider, _client = provider_and_client(case)

    for symbol in symbols:
        outcome = provider.fetch_bars(query(Period.DAY, symbol=symbol))
        assert isinstance(outcome, BarResult)
        assert {bar.symbol for bar in outcome.bars} == {symbol}


def test_baostock_rejects_beijing_bars_without_calling_client() -> None:
    case = next(
        case for case in _provider_cases() if case.source is ProviderId.BAOSTOCK
    )
    provider, client = provider_and_client(case)

    outcome = provider.fetch_bars(query(Period.DAY, symbol="920000.BJ"))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.UNSUPPORTED
    assert client.calls == []


def test_index_shaped_symbol_is_not_rejected_by_board_number() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.TUSHARE)
    provider, _client = provider_and_client(case)

    outcome = provider.fetch_bars(query(Period.DAY, symbol="000001.SH"))

    assert isinstance(outcome, BarResult)
    assert outcome.bars[0].symbol == "000001.SH"


@pytest.mark.parametrize(
    "symbol",
    ["000001.SH", "399001.SZ", "600000.SZ", "920000.SH"],
)
def test_akshare_rejects_index_or_suffix_ambiguous_stock_symbols_without_calling(
    symbol: str,
) -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.AKSHARE)
    provider, client = provider_and_client(case)

    outcome = provider.fetch_bars(query(Period.DAY, symbol=symbol))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.UNSUPPORTED
    assert client.calls == []


def test_exact_provider_parameter_mappings(provider_case: ProviderCase) -> None:
    provider, client = provider_and_client(provider_case)
    for period in Period:
        for adjustment in Adjustment:
            provider.fetch_bars(query(period, adjustment))

    calls = client.calls
    assert len(calls) == (6 if provider_case.source is ProviderId.AKSHARE else 9)
    if provider_case.source is ProviderId.TUSHARE:
        expected_freq = {Period.DAY: "D", Period.WEEK: "W", Period.MIN60: "60min"}
        for (_name, kwargs), period, adjustment in zip(
            calls,
            (period for period in Period for _ in Adjustment),
            (adjustment for _ in Period for adjustment in Adjustment),
            strict=True,
        ):
            assert kwargs == {
                "ts_code": "600000.SH",
                "start_date": "20240701",
                "end_date": "20240703"
                if period is Period.DAY
                else ("20240708" if period is Period.WEEK else "20240701"),
                "freq": expected_freq[period],
                "adj": None if adjustment is Adjustment.NONE else adjustment.value,
            }
    elif provider_case.source is ProviderId.AKSHARE:
        assert calls[0] == (
            "stock_zh_a_hist",
            {
                "symbol": "600000",
                "period": "daily",
                "start_date": "20240701",
                "end_date": "20240703",
                "adjust": "",
            },
        )
        assert {name for name, _kwargs in calls} == {"stock_zh_a_hist"}
    else:
        assert calls[0][1]["code"] == "sh.600000"
        assert calls[0][1]["frequency"] == "d"
        assert calls[0][1]["adjustflag"] == "3"
        assert "tradestatus" in calls[0][1]["fields"]
        assert "time" not in calls[0][1]["fields"]
        assert calls[4][1]["frequency"] == "w"
        assert calls[4][1]["adjustflag"] == "2"
        assert "tradestatus" not in calls[3][1]["fields"]
        assert "time" not in calls[3][1]["fields"]
        assert calls[-1][1]["frequency"] == "60"
        assert calls[-1][1]["adjustflag"] == "1"
        assert "tradestatus" not in calls[6][1]["fields"]
        assert "time" in calls[6][1]["fields"]


def test_tushare_calendar_exchange_mapping_is_explicit() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.TUSHARE)
    provider, client = provider_and_client(case)

    for exchange in Exchange:
        outcome = provider.fetch_calendar(
            exchange,
            date(2024, 7, 1),
            date(2024, 7, 7),
        )
        assert isinstance(outcome, ProviderBatch)
        assert {day.exchange for day in outcome.items} == {exchange}

    assert [kwargs["exchange"] for _name, kwargs in client.calls] == [
        "SSE",
        "SZSE",
        "BSE",
    ]


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ProviderPermissionDenied(SECRET_SENTINEL), FailureReason.PERMISSION_DENIED),
        (ProviderUnsupported(SECRET_SENTINEL), FailureReason.UNSUPPORTED),
        (ProviderTransientFailure(SECRET_SENTINEL), FailureReason.TRANSIENT_FAILURE),
        (ProviderUnavailable(SECRET_SENTINEL), FailureReason.PROVIDER_UNAVAILABLE),
        (ProviderInvalidResponse(SECRET_SENTINEL), FailureReason.INVALID_RESPONSE),
        (ProviderTimeout(SECRET_SENTINEL), FailureReason.TIMEOUT),
        (TimeoutError(SECRET_SENTINEL), FailureReason.TIMEOUT),
        (RuntimeError(SECRET_SENTINEL), FailureReason.INVALID_RESPONSE),
    ],
)
def test_client_errors_become_safe_contextual_failures(
    provider_case: ProviderCase,
    error: Exception,
    reason: FailureReason,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, client = provider_and_client(provider_case)
    client.bar_exception = error
    bar_query = query(Period.DAY)

    outcome = provider.fetch_bars(bar_query)

    assert isinstance(outcome, BarFailure)
    assert outcome.query == bar_query
    assert outcome.source is provider_case.source
    assert outcome.reason is reason
    assert outcome.failed_start == bar_query.start
    assert outcome.failed_end == bar_query.end
    assert outcome.reason is not FailureReason.NO_PROVIDER
    assert SECRET_SENTINEL not in outcome.model_dump_json()
    assert SECRET_SENTINEL not in caplog.text


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("empty", FailureReason.NO_DATA),
        ("duplicate", FailureReason.INVALID_RESPONSE),
        ("corrupt", FailureReason.INVALID_RESPONSE),
        ("mismatch", FailureReason.INVALID_RESPONSE),
        ("long-cell", FailureReason.INVALID_RESPONSE),
        ("bool-volume", FailureReason.INVALID_RESPONSE),
        ("negative-volume", FailureReason.INVALID_RESPONSE),
        ("overflow-volume", FailureReason.INVALID_RESPONSE),
        ("nan-price", FailureReason.INVALID_RESPONSE),
    ],
)
def test_invalid_tables_fail_without_partial_success(
    provider_case: ProviderCase,
    mutation: str,
    reason: FailureReason,
) -> None:
    provider, client = provider_and_client(provider_case)
    client.bar_mutation = mutation

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is reason


def test_dataframe_duplicate_columns_fail_closed(provider_case: ProviderCase) -> None:
    provider, client = provider_and_client(provider_case, table_style="frame")
    rows = load_fixture(provider_case.fixture_name)["bars"]["1d"]
    client.frame_columns = [*rows[0], next(iter(rows[0]))]

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_dataframe_missing_columns_fail_closed(provider_case: ProviderCase) -> None:
    provider, client = provider_and_client(provider_case, table_style="frame")
    rows = load_fixture(provider_case.fixture_name)["bars"]["1d"]
    client.frame_columns = [
        column for column in rows[0] if column not in {"open", "开盘"}
    ]

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_baostock_cursor_validation_fails_closed() -> None:
    case = next(
        case for case in _provider_cases() if case.source is ProviderId.BAOSTOCK
    )
    provider, client = provider_and_client(case, table_style="cursor")
    client.cursor_error_code = "999"
    outcome = provider.fetch_bars(query(Period.DAY))
    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE
    assert SECRET_SENTINEL not in outcome.detail

    provider, client = provider_and_client(case, table_style="cursor")
    client.cursor_fields = ["date", "date"]
    outcome = provider.fetch_bars(query(Period.DAY))
    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE

    provider, client = provider_and_client(case, table_style="cursor")
    client.cursor_fail_after = 1
    outcome = provider.fetch_bars(query(Period.DAY))
    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE

    provider, client = provider_and_client(case, table_style="cursor")
    client.cursor_row_width_delta = -1
    outcome = provider.fetch_bars(query(Period.DAY))
    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_excessive_rows_fail_closed(provider_case: ProviderCase) -> None:
    provider, client = provider_and_client(provider_case)
    client.bar_mutation = "too-many"

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize("source", [ProviderId.TUSHARE, ProviderId.AKSHARE])
def test_fractional_hand_volume_must_convert_to_whole_shares(
    source: ProviderId,
) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    provider, client = provider_and_client(case)
    client.bar_mutation = "fractional-lot"

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize("source", [ProviderId.TUSHARE, ProviderId.AKSHARE])
def test_fractional_hand_volume_is_accepted_when_shares_are_integral(
    source: ProviderId,
) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    provider, client = provider_and_client(case)
    client.bar_mutation = "tiny-lot"

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarResult)
    assert 1 in {bar.volume for bar in outcome.bars}


def test_baostock_suspended_row_requires_official_prices() -> None:
    case = next(
        case for case in _provider_cases() if case.source is ProviderId.BAOSTOCK
    )
    provider, client = provider_and_client(case)
    client.bar_mutation = "missing-suspended-prices"

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


def test_dataset_version_is_canonical_and_excludes_fetched_at(
    provider_case: ProviderCase,
) -> None:
    provider_a, _ = provider_and_client(provider_case, clock=lambda: FETCHED_AT)
    provider_b, _ = provider_and_client(
        provider_case,
        clock=lambda: FETCHED_AT.replace(day=5),
    )
    result_a = provider_a.fetch_bars(query(Period.DAY))
    result_b = provider_b.fetch_bars(query(Period.DAY))
    adjusted = provider_a.fetch_bars(query(Period.DAY, Adjustment.QFQ))

    assert isinstance(result_a, BarResult)
    assert isinstance(result_b, BarResult)
    assert isinstance(adjusted, BarResult)
    assert result_a.provenance.dataset_version == result_b.provenance.dataset_version
    assert result_a.provenance.dataset_version != adjusted.provenance.dataset_version


def test_same_day_daily_bar_is_valid_after_the_source_session_close(
    provider_case: ProviderCase,
) -> None:
    fetched_at = market_time(2, 16)
    provider, _ = provider_and_client(provider_case, clock=lambda: fetched_at)
    same_day_query = BarQuery(
        symbol="600000.SH",
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=market_time(2),
        end=market_time(3),
    )

    result = provider.fetch_bars(same_day_query)

    assert isinstance(result, BarResult)
    assert result.provenance.data_cutoff == market_time(2, 15)
    assert result.provenance.fetched_at == fetched_at.astimezone(timezone.utc)


def test_dataset_version_is_stable_for_equivalent_query_timezones() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.TUSHARE)
    provider, _client = provider_and_client(case)
    local_query = query(Period.DAY)
    utc_query = BarQuery(
        symbol=local_query.symbol,
        period=local_query.period,
        adjustment=local_query.adjustment,
        start=local_query.start.astimezone(timezone.utc),
        end=local_query.end.astimezone(timezone.utc),
    )

    local_result = provider.fetch_bars(local_query)
    utc_result = provider.fetch_bars(utc_query)

    assert isinstance(local_result, BarResult)
    assert isinstance(utc_result, BarResult)
    assert (
        local_result.provenance.dataset_version == utc_result.provenance.dataset_version
    )


def test_dataset_version_is_stable_across_python_hash_seeds() -> None:
    script = """
from datetime import datetime, timezone
from decimal import Decimal
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import Adjustment, Bar, Period, ProviderId
bar = Bar(
    symbol='600000.SH',
    timestamp=datetime(2024, 6, 30, 16, tzinfo=timezone.utc),
    period=Period.DAY,
    adjustment=Adjustment.QFQ,
    open=Decimal('-0.2'), high=Decimal('0.1'), low=Decimal('-0.3'),
    close=Decimal('0'), volume=1,
)
print(dataset_version(
    source=ProviderId.TUSHARE,
    operation='bars',
    request={'z': 2, 'a': 1},
    data_cutoff=datetime(2024, 7, 1, 16, tzinfo=timezone.utc),
    items=(bar,),
))
"""
    versions = []
    for seed in ("1", "987654"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
        )
        versions.append(completed.stdout.strip())

    assert len(set(versions)) == 1


def test_dataset_version_is_stable_across_row_column_and_decimal_order() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.TUSHARE)
    baseline, _ = provider_and_client(case)
    baseline_result = baseline.fetch_bars(query(Period.DAY))
    assert isinstance(baseline_result, BarResult)

    fixture = load_fixture("tushare")
    fixture["bars"]["1d"].reverse()
    for row in fixture["bars"]["1d"]:
        row["open"] = f"{Decimal(str(row['open'])):.3f}"
    client = case.client_type(fixture, table_style="frame")
    first_row = fixture["bars"]["1d"][0]
    client.frame_columns = list(reversed(first_row))
    reordered = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    ).fetch_bars(query(Period.DAY))

    assert isinstance(reordered, BarResult)
    assert (
        reordered.provenance.dataset_version
        == baseline_result.provenance.dataset_version
    )


def test_dataset_version_changes_when_normalized_data_changes() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.TUSHARE)
    baseline, _ = provider_and_client(case)
    baseline_result = baseline.fetch_bars(query(Period.DAY))
    assert isinstance(baseline_result, BarResult)

    fixture = load_fixture("tushare")
    fixture["bars"]["1d"][0]["close"] = "10.61"
    client = case.client_type(fixture)
    changed = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    ).fetch_bars(query(Period.DAY))

    assert isinstance(changed, BarResult)
    assert (
        changed.provenance.dataset_version != baseline_result.provenance.dataset_version
    )


def test_source_changes_dataset_version_for_equivalent_normalized_data() -> None:
    results = []
    normalized_bars = []
    for case in _provider_cases():
        provider, _client = provider_and_client(case)
        outcome = provider.fetch_bars(query(Period.DAY))
        assert isinstance(outcome, BarResult)
        results.append(outcome.provenance.dataset_version)
        normalized_bars.append(
            tuple(bar.model_dump(mode="json") for bar in outcome.bars)
        )

    assert normalized_bars[0] == normalized_bars[1]
    assert len(set(results)) == len(results)


def test_instrument_and_calendar_contracts(provider_case: ProviderCase) -> None:
    provider, _client = provider_and_client(provider_case)

    instruments = provider.fetch_instruments()
    calendar = provider.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 1),
        date(2024, 7, 7),
    )

    assert isinstance(instruments, ProviderBatch)
    assert instruments.provenance.source is provider_case.source
    assert instruments.provenance.fetched_at == FETCHED_AT
    assert instruments.provenance.data_cutoff == FETCHED_AT
    assert instruments.provenance.dataset_version.startswith("sha256:")
    assert not hasattr(instruments.provenance, "adjustment")
    assert tuple(item.symbol for item in instruments.items) == (
        "000001.SZ",
        "600000.SH",
        "920000.BJ",
    )
    assert {item.exchange for item in instruments.items} == set(Exchange)
    if provider_case.source is ProviderId.AKSHARE:
        assert all(item.listing_status.value == "unknown" for item in instruments.items)
        assert all(item.listed_on is None for item in instruments.items)
    else:
        assert all(item.listing_status.value == "listed" for item in instruments.items)
        assert all(item.listed_on is not None for item in instruments.items)
    if provider_case.source is ProviderId.AKSHARE:
        assert isinstance(calendar, ProviderBatchFailure)
        assert calendar.reason is FailureReason.UNSUPPORTED
        return
    assert isinstance(calendar, ProviderBatch)
    assert calendar.provenance.source is provider_case.source
    assert calendar.provenance.fetched_at == FETCHED_AT
    assert calendar.provenance.dataset_version.startswith("sha256:")
    assert not hasattr(calendar.provenance, "adjustment")
    assert all(day.exchange is Exchange.SH for day in calendar.items)
    assert tuple(day.day for day in calendar.items) == tuple(
        date(2024, 7, day) for day in range(1, 7)
    )
    assert tuple(day.is_open for day in calendar.items) == (
        True,
        True,
        True,
        True,
        True,
        False,
    )
    assert tuple(day.day for day in calendar.items) == tuple(
        sorted(day.day for day in calendar.items)
    )
    with pytest.raises(ValidationError, match="frozen"):
        instruments.items = ()


class OneShotClock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("provider sampled the clock more than once")
        return self.value


@pytest.mark.parametrize("operation", ["instruments", "calendar"])
def test_batch_operations_sample_the_clock_exactly_once(
    provider_case: ProviderCase,
    operation: str,
) -> None:
    clock = OneShotClock(FETCHED_AT)
    provider, _ = provider_and_client(provider_case, clock=clock)

    if operation == "instruments":
        outcome = provider.fetch_instruments()
    else:
        outcome = provider.fetch_calendar(
            Exchange.SH,
            date(2024, 7, 1),
            date(2024, 7, 7),
        )

    if provider_case.source is ProviderId.AKSHARE and operation == "calendar":
        assert isinstance(outcome, ProviderBatchFailure)
        assert outcome.reason is FailureReason.UNSUPPORTED
        assert clock.calls == 0
    else:
        assert isinstance(outcome, ProviderBatch)
        assert clock.calls == 1


@pytest.mark.parametrize("source", [ProviderId.TUSHARE, ProviderId.BAOSTOCK])
def test_current_day_calendar_cutoff_is_capped_at_single_observation(
    source: ProviderId,
) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    fixture = load_fixture(case.fixture_name)
    if source is ProviderId.TUSHARE:
        fixture["calendar"]["SSE"] = [
            {"exchange": "SSE", "cal_date": "20240708", "is_open": "1"}
        ]
    else:
        fixture["calendar"] = [{"calendar_date": "2024-07-08", "is_trading_day": "1"}]
    client = case.client_type(fixture)
    observed_at = market_time(8, 16)
    clock = OneShotClock(observed_at)
    provider = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=clock),
    )

    outcome = provider.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 8),
        date(2024, 7, 9),
    )

    assert isinstance(outcome, ProviderBatch)
    assert outcome.provenance.data_cutoff == observed_at.astimezone(timezone.utc)
    assert outcome.provenance.data_cutoff <= outcome.provenance.fetched_at
    assert clock.calls == 1


def test_akshare_instrument_snapshot_version_includes_observation_cutoff() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.AKSHARE)
    provider_a, _ = provider_and_client(case, clock=lambda: FETCHED_AT)
    provider_b, _ = provider_and_client(
        case,
        clock=lambda: FETCHED_AT.replace(minute=1),
    )

    result_a = provider_a.fetch_instruments()
    result_b = provider_b.fetch_instruments()

    assert isinstance(result_a, ProviderBatch)
    assert isinstance(result_b, ProviderBatch)
    assert result_a.items == result_b.items
    assert result_a.provenance.dataset_version != result_b.provenance.dataset_version


@pytest.mark.parametrize("source", [ProviderId.TUSHARE, ProviderId.BAOSTOCK])
@pytest.mark.parametrize("mutation", ["duplicate", "missing", "out-of-range"])
def test_explicit_calendars_require_exact_unique_natural_day_coverage(
    source: ProviderId,
    mutation: str,
) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    fixture = load_fixture(case.fixture_name)
    if source is ProviderId.TUSHARE:
        rows = fixture["calendar"]["SSE"]
    else:
        rows = fixture["calendar"]
    if mutation == "duplicate":
        rows.append(rows[0].copy())
    elif mutation == "missing":
        rows.pop()
    elif source is ProviderId.TUSHARE:
        rows.append({"exchange": "SSE", "cal_date": "20240630", "is_open": "0"})
    else:
        rows.append({"calendar_date": "2024-06-30", "is_trading_day": "0"})
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    outcome = provider.fetch_calendar(
        Exchange.SH,
        date(2024, 7, 1),
        date(2024, 7, 7),
    )

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2024, 7, 1), date(2024, 7, 7)),
        (date(2024, 7, 8), date(2024, 7, 10)),
    ],
)
def test_akshare_calendar_is_unsupported_without_calling_open_date_endpoint(
    start: date,
    end: date,
) -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.AKSHARE)
    fixture = load_fixture("akshare")
    fixture["calendar"].pop(2)
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    outcome = provider.fetch_calendar(
        Exchange.SH,
        start,
        end,
    )

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.UNSUPPORTED
    assert client.calls == []


def test_duplicate_instrument_identity_fails_closed(
    provider_case: ProviderCase,
) -> None:
    fixture = load_fixture(provider_case.fixture_name)
    fixture["instruments"].append(fixture["instruments"][0].copy())
    client = provider_case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        provider_case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("operation", "attribute"),
    [
        (ProviderOperation.INSTRUMENTS, "instrument_exception"),
        (ProviderOperation.CALENDAR, "calendar_exception"),
    ],
)
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ProviderTimeout(SECRET_SENTINEL), FailureReason.TIMEOUT),
        (TimeoutError(SECRET_SENTINEL), FailureReason.TIMEOUT),
        (RuntimeError(SECRET_SENTINEL), FailureReason.INVALID_RESPONSE),
    ],
)
def test_batch_failures_are_typed_contextual_and_safe(
    provider_case: ProviderCase,
    operation: ProviderOperation,
    attribute: str,
    error: Exception,
    reason: FailureReason,
) -> None:
    provider, client = provider_and_client(provider_case)
    setattr(client, attribute, error)

    outcome = (
        provider.fetch_instruments()
        if operation is ProviderOperation.INSTRUMENTS
        else provider.fetch_calendar(Exchange.SH, date(2024, 7, 1), date(2024, 7, 7))
    )

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.source is provider_case.source
    assert outcome.operation is operation
    expected_reason = (
        FailureReason.UNSUPPORTED
        if provider_case.source is ProviderId.AKSHARE
        and operation is ProviderOperation.CALENDAR
        else reason
    )
    assert outcome.reason is expected_reason
    assert SECRET_SENTINEL not in outcome.model_dump_json()
    if operation is ProviderOperation.CALENDAR:
        assert outcome.exchange is Exchange.SH
        assert outcome.start == date(2024, 7, 1)
        assert outcome.end == date(2024, 7, 7)
    else:
        assert outcome.exchange is None
        assert outcome.start is None
        assert outcome.end is None


def test_empty_instrument_batches_return_no_data(
    provider_case: ProviderCase,
) -> None:
    fixture = load_fixture(provider_case.fixture_name)
    fixture["instruments"] = []
    client = provider_case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        provider_case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    instruments = provider.fetch_instruments()

    assert isinstance(instruments, ProviderBatchFailure)
    assert instruments.reason is FailureReason.NO_DATA


def test_baostock_catalog_filters_non_stock_and_b_share_rows() -> None:
    case = next(
        case for case in _provider_cases() if case.source is ProviderId.BAOSTOCK
    )
    fixture = load_fixture(case.fixture_name)
    fixture["instruments"].extend(
        (
            {
                "code": "sh.000001",
                "code_name": "上证指数",
                "ipoDate": "1990-12-19",
                "outDate": "",
                "type": "2",
                "status": "1",
            },
            {
                "code": "sh.900901",
                "code_name": "B股样本",
                "ipoDate": "1992-02-21",
                "outDate": "",
                "type": "1",
                "status": "1",
            },
        )
    )
    provider = cast(
        MarketDataProvider,
        case.provider_type(
            client=case.client_type(fixture),
            clock=lambda: FETCHED_AT,
        ),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatch)
    assert tuple(item.symbol for item in outcome.items) == (
        "000001.SZ",
        "600000.SH",
        "920000.BJ",
    )


@pytest.mark.parametrize("source", [ProviderId.BAOSTOCK, ProviderId.AKSHARE])
def test_current_shenzhen_a_share_prefix_is_preserved(source: ProviderId) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    fixture = load_fixture(case.fixture_name)
    if source is ProviderId.AKSHARE:
        fixture["instruments"] = [
            {"code": row["代码"], "name": row["名称"]} for row in fixture["instruments"]
        ]
    fixture["instruments"].append(
        {
            "code": "sz.302132",
            "code_name": "中航成飞",
            "ipoDate": "2010-08-27",
            "outDate": "",
            "type": "1",
            "status": "1",
        }
        if source is ProviderId.BAOSTOCK
        else {"code": "302132", "name": "中航成飞"}
    )
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider, case.provider_type(client=client, clock=lambda: FETCHED_AT)
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatch)
    instrument = next(item for item in outcome.items if item.symbol == "302132.SZ")
    assert instrument.name == "中航成飞"
    assert instrument.exchange is Exchange.SZ


def test_akshare_catalog_filters_explicit_b_share_rows() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.AKSHARE)
    fixture = load_fixture("akshare")
    fixture["instruments"] = [
        {"code": row["代码"], "name": row["名称"]} for row in fixture["instruments"]
    ]
    fixture["instruments"].extend(
        (
            {"code": "900901", "name": "沪市B股样本"},
            {"code": "200002", "name": "深市B股样本"},
        )
    )
    provider = cast(
        MarketDataProvider,
        case.provider_type(
            client=case.client_type(fixture),
            clock=lambda: FETCHED_AT,
        ),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatch)
    assert tuple(item.symbol for item in outcome.items) == (
        "000001.SZ",
        "600000.SH",
        "920000.BJ",
    )


@pytest.mark.parametrize("source", [ProviderId.BAOSTOCK, ProviderId.AKSHARE])
def test_catalog_with_only_explicitly_out_of_scope_rows_is_no_data(
    source: ProviderId,
) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    fixture = load_fixture(case.fixture_name)
    fixture["instruments"] = [
        {
            "code": "sh.900901",
            "code_name": "沪市B股样本",
            "ipoDate": "1992-02-21",
            "outDate": "",
            "type": "1",
            "status": "1",
        }
        if source is ProviderId.BAOSTOCK
        else {"代码": "900901", "名称": "沪市B股样本"}
    ]
    provider = cast(
        MarketDataProvider,
        case.provider_type(
            client=case.client_type(fixture),
            clock=lambda: FETCHED_AT,
        ),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.NO_DATA


@pytest.mark.parametrize("source", [ProviderId.BAOSTOCK, ProviderId.AKSHARE])
def test_malformed_instrument_codes_still_fail_closed(source: ProviderId) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    fixture = load_fixture(case.fixture_name)
    fixture["instruments"].append(
        {
            "code": "sh.90090",
            "code_name": "畸形记录",
            "ipoDate": "1992-02-21",
            "outDate": "",
            "type": "1",
            "status": "1",
        }
        if source is ProviderId.BAOSTOCK
        else {"代码": "90090", "名称": "畸形记录"}
    )
    provider = cast(
        MarketDataProvider,
        case.provider_type(
            client=case.client_type(fixture),
            clock=lambda: FETCHED_AT,
        ),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize("source", [ProviderId.BAOSTOCK, ProviderId.AKSHARE])
def test_unknown_board_is_rejected_without_guessing(source: ProviderId) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    fixture = load_fixture(case.fixture_name)
    fixture["instruments"].append(
        {
            "code": "sh.777777",
            "code_name": "未知板块",
            "ipoDate": "2024-01-02",
            "outDate": "",
            "type": "1",
            "status": "1",
        }
        if source is ProviderId.BAOSTOCK
        else {"代码": "777777", "名称": "未知板块"}
    )
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider, case.provider_type(client=client, clock=lambda: FETCHED_AT)
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.UNSUPPORTED


def test_akshare_instruments_accept_current_official_code_name_schema() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.AKSHARE)
    fixture = load_fixture("akshare")
    fixture["instruments"] = [{"code": "600000", "name": "浦发银行"}]
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatch)
    assert outcome.items[0].symbol == "600000.SH"


def test_akshare_instruments_reject_mixed_code_name_schema() -> None:
    case = next(case for case in _provider_cases() if case.source is ProviderId.AKSHARE)
    fixture = load_fixture("akshare")
    fixture["instruments"] = [{"code": "600000", "名称": "浦发银行"}]
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatchFailure)
    assert outcome.reason is FailureReason.INVALID_RESPONSE


@pytest.mark.parametrize("missing", [None, float("nan")])
@pytest.mark.parametrize("source", [ProviderId.TUSHARE, ProviderId.BAOSTOCK])
def test_missing_delist_date_sentinels_are_safe(
    source: ProviderId,
    missing: object,
) -> None:
    case = next(case for case in _provider_cases() if case.source is source)
    fixture = load_fixture(case.fixture_name)
    field = "delist_date" if source is ProviderId.TUSHARE else "outDate"
    fixture["instruments"][0][field] = missing
    client = case.client_type(fixture)
    provider = cast(
        MarketDataProvider,
        case.provider_type(client=client, clock=lambda: FETCHED_AT),
    )

    outcome = provider.fetch_instruments()

    assert isinstance(outcome, ProviderBatch)
    assert outcome.items[1].delisted_on is None


def test_empty_zero_column_dataframe_is_no_data(provider_case: ProviderCase) -> None:
    provider, client = provider_and_client(provider_case, table_style="frame")
    client.bar_mutation = "empty"

    outcome = provider.fetch_bars(query(Period.DAY))

    assert isinstance(outcome, BarFailure)
    assert outcome.reason is FailureReason.NO_DATA
