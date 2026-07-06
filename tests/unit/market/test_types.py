from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
import json
from typing import get_args
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFailure,
    BarFetchOutcome,
    BarQuery,
    BarResult,
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
    Provenance,
    ProviderId,
    TradingDay,
    TradingStatus,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADING_DATE = date(2026, 7, 6)


def market_time(hour: int, minute: int = 0, *, day: date = TRADING_DATE) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SHANGHAI)


def provenance(**updates: object) -> Provenance:
    values: dict[str, object] = {
        "source": ProviderId.AKSHARE,
        "fetched_at": market_time(16),
        "data_cutoff": market_time(15),
        "adjustment": Adjustment.NONE,
        "dataset_version": "bars-v1",
    }
    values.update(updates)
    return Provenance.model_validate(values)


def query(**updates: object) -> BarQuery:
    values: dict[str, object] = {
        "symbol": "600000.SH",
        "period": Period.MIN60,
        "adjustment": Adjustment.NONE,
        "start": market_time(9, 30),
        "end": market_time(11, 30),
    }
    values.update(updates)
    return BarQuery.model_validate(values)


def bar(
    *,
    symbol: str = "600000.SH",
    timestamp: datetime | None = None,
    period: Period = Period.MIN60,
    adjustment: Adjustment = Adjustment.NONE,
    open_price: Decimal = Decimal("10.00"),
    high: Decimal = Decimal("12.00"),
    low: Decimal = Decimal("9.00"),
    close: Decimal = Decimal("11.00"),
) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=timestamp or market_time(9, 30),
        period=period,
        adjustment=adjustment,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


def result(**updates: object) -> BarResult:
    bar_query = query()
    values: dict[str, object] = {
        "query": bar_query,
        "bars": (
            bar(timestamp=market_time(9, 30)),
            bar(timestamp=market_time(10, 30)),
        ),
        "coverage_start": bar_query.start,
        "coverage_end": bar_query.end,
        "provenance": provenance(),
    }
    values.update(updates)
    return BarResult.model_validate(values)


def json_bar_payload(**updates: object) -> str:
    values: dict[str, object] = {
        "symbol": "600000.SH",
        "timestamp": "2026-07-06T01:30:00Z",
        "period": "60m",
        "adjustment": "none",
        "open": "10.00",
        "high": "12.00",
        "low": "9.00",
        "close": "11.00",
        "volume": 1000,
        "status": "unknown",
    }
    values.update(updates)
    return json.dumps(values, separators=(",", ":"))


def test_period_has_exact_public_members_and_values() -> None:
    assert Period.__members__ == {
        "DAY": Period.DAY,
        "WEEK": Period.WEEK,
        "MIN60": Period.MIN60,
    }
    assert tuple(period.value for period in Period) == ("1d", "1w", "60m")
    assert tuple(adjustment.value for adjustment in Adjustment) == (
        "none",
        "qfq",
        "hfq",
    )


def test_shared_provider_exchange_and_failure_enums_are_closed() -> None:
    assert ProviderId.AKSHARE.value == "akshare"
    assert ProviderId.TDX_LOCAL.value == "tdx_local"
    assert tuple(exchange.value for exchange in Exchange) == ("SH", "SZ", "BJ")
    assert {
        "permission_denied",
        "unsupported",
        "missing",
        "no_data",
        "provider_unavailable",
        "transient_failure",
        "timeout",
        "corrupt",
        "invalid_response",
        "no_provider",
    } <= {reason.value for reason in FailureReason}
    assert tuple(state.value for state in CapabilityState) == (
        "available",
        "unavailable",
        "permission_denied",
        "unsupported",
        "transient_failure",
    )


@pytest.mark.parametrize(
    ("symbol", "exchange", "kind"),
    [
        ("600000.SH", Exchange.SH, InstrumentKind.STOCK),
        ("000001.SZ", Exchange.SZ, InstrumentKind.STOCK),
        ("920000.BJ", Exchange.BJ, InstrumentKind.STOCK),
        ("000001.SH", Exchange.SH, InstrumentKind.INDEX),
        ("399001.SZ", Exchange.SZ, InstrumentKind.INDEX),
    ],
)
def test_instrument_accepts_canonical_equities_and_indices(
    symbol: str,
    exchange: Exchange,
    kind: InstrumentKind,
) -> None:
    instrument = Instrument(
        symbol=symbol,
        exchange=exchange,
        name="Index or equity",
        instrument_kind=kind,
        listing_status=ListingStatus.LISTED,
        listed_on=date(1990, 12, 19),
        delisted_on=None,
    )

    assert instrument.exchange is exchange
    assert instrument.instrument_kind is kind


@pytest.mark.parametrize(
    "symbol",
    [
        "600000.sh",
        " 600000.SH",
        "600000.SH ",
        "60000.SH",
        "6000000.SH",
        "600000.HK",
        "../600000.SH",
        "600000/SH",
        "600000.SH/../../secret",
        "",
    ],
)
def test_instrument_rejects_noncanonical_symbols(symbol: str) -> None:
    with pytest.raises(ValidationError):
        Instrument(
            symbol=symbol,
            exchange=Exchange.SH,
            name="Invalid",
            instrument_kind=InstrumentKind.STOCK,
            listing_status=ListingStatus.UNKNOWN,
        )


def test_instrument_validates_exchange_suffix_and_listing_dates() -> None:
    values: dict[str, object] = {
        "symbol": "600000.SH",
        "exchange": Exchange.SZ,
        "name": "Mismatch",
        "instrument_kind": InstrumentKind.STOCK,
        "listing_status": ListingStatus.LISTED,
    }
    with pytest.raises(ValidationError, match="exchange"):
        Instrument.model_validate(values)

    values.update(
        exchange=Exchange.SH,
        listing_status=ListingStatus.DELISTED,
        listed_on=date(2020, 1, 2),
        delisted_on=date(2020, 1, 1),
    )
    with pytest.raises(ValidationError, match="delisted"):
        Instrument.model_validate(values)

    values.update(delisted_on=None)
    with pytest.raises(ValidationError, match="delisted"):
        Instrument.model_validate(values)


def test_trading_day_includes_exchange_and_models_are_frozen() -> None:
    trading_day = TradingDay(day=TRADING_DATE, exchange=Exchange.SH, is_open=True)

    assert trading_day.exchange is Exchange.SH
    with pytest.raises(ValidationError, match="frozen"):
        trading_day.is_open = False
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TradingDay.model_validate(
            {
                "day": TRADING_DATE,
                "exchange": Exchange.SH,
                "is_open": True,
                "unknown": True,
            }
        )


def test_trading_status_defaults_to_unknown() -> None:
    assert tuple(status.value for status in TradingStatus) == (
        "unknown",
        "normal",
        "suspended",
        "limit_up",
        "limit_down",
    )
    assert bar().status is TradingStatus.UNKNOWN


@pytest.mark.parametrize(
    ("period", "timestamp"),
    [
        (Period.DAY, market_time(0)),
        (Period.WEEK, market_time(0)),
        (Period.MIN60, market_time(9, 30)),
        (Period.MIN60, market_time(10, 30)),
        (Period.MIN60, market_time(13)),
        (Period.MIN60, market_time(14)),
    ],
)
def test_bar_accepts_only_canonical_bucket_starts(
    period: Period,
    timestamp: datetime,
) -> None:
    value = bar(period=period, timestamp=timestamp)

    assert value.timestamp.tzinfo is timezone.utc


@pytest.mark.parametrize(
    ("period", "timestamp"),
    [
        (Period.DAY, market_time(0, 1)),
        (Period.WEEK, market_time(0, day=date(2026, 7, 7))),
        (Period.MIN60, market_time(10)),
        (Period.MIN60, market_time(12)),
        (Period.MIN60, market_time(15)),
    ],
)
def test_bar_rejects_noncanonical_bucket_starts(
    period: Period,
    timestamp: datetime,
) -> None:
    with pytest.raises(ValidationError, match="bucket"):
        bar(period=period, timestamp=timestamp)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", Decimal("0")),
        ("high", Decimal("10.50")),
        ("low", Decimal("10.50")),
        ("close", Decimal("12.50")),
        ("open", Decimal("NaN")),
        ("open", Decimal("Infinity")),
        ("open", Decimal("-Infinity")),
    ],
)
def test_bar_rejects_invalid_prices(field: str, value: Decimal) -> None:
    payload = bar().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        Bar.model_validate(payload)


@pytest.mark.parametrize("adjustment", [Adjustment.QFQ, Adjustment.HFQ])
def test_adjusted_bar_preserves_finite_zero_and_negative_prices(
    adjustment: Adjustment,
) -> None:
    value = bar(
        adjustment=adjustment,
        open_price=Decimal("-0.20"),
        high=Decimal("0.10"),
        low=Decimal("-0.30"),
        close=Decimal("0"),
    )

    assert value.open == Decimal("-0.2")
    assert value.high == Decimal("0.1")
    assert value.low == Decimal("-0.3")
    assert value.close == Decimal("0")
    assert '"open":"-0.2"' in value.model_dump_json()


@pytest.mark.parametrize("adjustment", [Adjustment.QFQ, Adjustment.HFQ])
@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_adjusted_bar_still_rejects_nonfinite_prices(
    adjustment: Adjustment,
    value: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        bar(
            adjustment=adjustment,
            open_price=value,
            high=value,
            low=value,
            close=value,
        )


@pytest.mark.parametrize("value", [10, 10.0, True, False, "10.00"])
def test_python_price_inputs_require_decimal(value: object) -> None:
    payload = bar().model_dump()
    payload["open"] = value

    with pytest.raises(ValidationError):
        Bar.model_validate(payload)


def test_json_price_inputs_require_strings_and_normalize_equivalent_decimals() -> None:
    canonical = Bar.model_validate_json(json_bar_payload())
    equivalent = Bar.model_validate_json(
        json_bar_payload(
            open="10.000",
            high="12.000",
            low="9.0",
            close="11.0000",
        )
    )

    assert canonical.open == equivalent.open == Decimal("10")
    assert canonical.model_dump_json() == equivalent.model_dump_json()
    assert canonical.model_dump_json() == (
        '{"symbol":"600000.SH","timestamp":"2026-07-06T01:30:00Z",'
        '"period":"60m","adjustment":"none","open":"10","high":"12",'
        '"low":"9","close":"11","volume":1000,"status":"unknown"}'
    )


def test_adjusted_json_prices_accept_canonical_negative_syntax() -> None:
    value = Bar.model_validate_json(
        json_bar_payload(
            adjustment="qfq",
            open="-0.20",
            high="0.10",
            low="-0.30",
            close="0",
        )
    )

    assert value.open == Decimal("-0.2")
    assert value.model_dump_json() == (
        '{"symbol":"600000.SH","timestamp":"2026-07-06T01:30:00Z",'
        '"period":"60m","adjustment":"qfq","open":"-0.2","high":"0.1",'
        '"low":"-0.3","close":"0","volume":1000,"status":"unknown"}'
    )


def test_adjusted_negative_zero_normalizes_to_canonical_zero() -> None:
    negative_zero = Bar.model_validate_json(
        json_bar_payload(
            adjustment="qfq",
            open="-0.00",
            high="-0.0",
            low="-0",
            close="0.000",
        )
    )
    positive_zero = Bar.model_validate_json(
        json_bar_payload(
            adjustment="qfq",
            open="0",
            high="0",
            low="0",
            close="0",
        )
    )

    assert negative_zero.model_dump_json() == positive_zero.model_dump_json()
    assert '"open":"0"' in negative_zero.model_dump_json()


def test_decimal_normalization_preserves_bounded_significant_digits() -> None:
    precise = Decimal("10.12345678")
    value = bar(open_price=precise, high=Decimal("20"))

    assert value.open == precise
    assert '"open":"10.12345678"' in value.model_dump_json()

    with pytest.raises(ValidationError, match="price precision"):
        bar(open_price=Decimal("10.123456789"), high=Decimal("20"))


@pytest.mark.parametrize("value", [10, 10.0, True, False])
def test_json_price_inputs_reject_numbers_and_booleans(value: object) -> None:
    with pytest.raises(ValidationError):
        Bar.model_validate_json(json_bar_payload(open=value))


@pytest.mark.parametrize("value", [" 10", "10 ", "1_0", "+10", "- 10"])
def test_json_price_inputs_reject_noncanonical_string_syntax(value: str) -> None:
    with pytest.raises(ValidationError):
        Bar.model_validate_json(json_bar_payload(open=value))


@pytest.mark.parametrize(
    "value",
    ["1e10000", "1e-10000", "1E+20", "9" * 26],
)
def test_json_price_inputs_reject_exponent_amplification(value: str) -> None:
    with pytest.raises(ValidationError):
        Bar.model_validate_json(
            json_bar_payload(open=value, high=value, low=value, close=value)
        )


@pytest.mark.parametrize("value", [Decimal("1e10000"), Decimal("1e-10000")])
def test_python_price_inputs_reject_extreme_exponents_before_formatting(
    value: Decimal,
) -> None:
    payload = bar().model_dump()
    payload.update(open=value, high=value, low=value, close=value)

    with pytest.raises(ValidationError, match="price precision"):
        Bar.model_validate(payload)


@pytest.mark.parametrize("value", [-1, True, False, 1.5, Decimal("1")])
def test_bar_volume_is_a_strict_non_negative_integer(value: object) -> None:
    payload = bar().model_dump()
    payload["volume"] = value

    with pytest.raises(ValidationError):
        Bar.model_validate(payload)


def test_bar_volume_fits_the_signed_bigint_storage_domain() -> None:
    payload = bar().model_dump()
    payload["volume"] = 2**63 - 1

    assert Bar.model_validate(payload).volume == 2**63 - 1

    payload["volume"] = 2**63
    with pytest.raises(ValidationError, match="less than or equal"):
        Bar.model_validate(payload)


def test_provenance_uses_provider_id_and_valid_utc_instants() -> None:
    value = provenance()

    assert value.source is ProviderId.AKSHARE
    assert value.fetched_at.tzinfo is timezone.utc
    assert value.data_cutoff.tzinfo is timezone.utc
    assert value.data_cutoff <= value.fetched_at

    with pytest.raises(ValidationError):
        provenance(source="akshare")
    with pytest.raises(ValidationError):
        provenance(data_cutoff=market_time(16, 1))


def test_bar_query_uses_half_open_aware_range() -> None:
    value = query()

    assert value.start < value.end
    with pytest.raises(ValidationError):
        query(start=market_time(11, 30), end=market_time(11, 30))
    with pytest.raises(ValidationError, match="timezone"):
        query(start=datetime(2026, 7, 6, 9, 30))


def test_bar_result_represents_only_complete_nonempty_success() -> None:
    value = result()

    assert isinstance(value.bars, tuple)
    assert value.bars
    assert value.coverage_start == value.query.start
    assert value.coverage_end == value.query.end
    assert value.provenance.data_cutoff >= value.bars[-1].timestamp

    with pytest.raises(ValidationError, match="nonempty"):
        result(bars=())
    with pytest.raises(ValidationError, match="coverage"):
        result(coverage_start=market_time(10, 30))
    with pytest.raises(ValidationError, match="coverage"):
        result(coverage_end=market_time(10, 30))
    with pytest.raises(ValidationError, match="cutoff"):
        result(provenance=provenance(data_cutoff=market_time(10)))


def test_bar_result_enforces_half_open_ordered_query_consistency() -> None:
    first = bar(timestamp=market_time(9, 30))
    second = bar(timestamp=market_time(10, 30))

    with pytest.raises(ValidationError):
        result(bars=(second, first))
    with pytest.raises(ValidationError):
        result(bars=(first, first))
    with pytest.raises(ValidationError):
        result(bars=(bar(symbol="000001.SZ"),))
    with pytest.raises(ValidationError):
        result(bars=(bar(period=Period.DAY, timestamp=market_time(0)),))
    end_exclusive_query = query(end=market_time(13))
    with pytest.raises(ValidationError, match="range"):
        result(
            query=end_exclusive_query,
            coverage_end=end_exclusive_query.end,
            bars=(bar(timestamp=market_time(13)),),
        )


def test_bar_result_cannot_mix_success_and_failure_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BarResult.model_validate(
            {
                **result().model_dump(),
                "failure": BarFailure(
                    query=query(),
                    source=ProviderId.AKSHARE,
                    reason=FailureReason.NO_DATA,
                    failed_start=query().start,
                    failed_end=query().end,
                    detail="no rows",
                ),
            }
        )


def test_bar_failure_has_full_context_and_is_a_separate_outcome() -> None:
    bar_query = query()
    failure = BarFailure(
        query=bar_query,
        source=ProviderId.AKSHARE,
        reason=FailureReason.NO_DATA,
        failed_start=bar_query.start,
        failed_end=bar_query.end,
        detail="provider returned no rows",
    )

    assert failure.failed_start == bar_query.start
    assert failure.failed_end == bar_query.end
    assert set(get_args(BarFetchOutcome)) == {BarResult, BarFailure}

    with pytest.raises(ValidationError, match="failed"):
        BarFailure(
            query=bar_query,
            source=ProviderId.AKSHARE,
            reason=FailureReason.NO_DATA,
            failed_start=bar_query.end,
            failed_end=bar_query.start,
            detail="invalid range",
        )
    with pytest.raises(ValidationError):
        BarFailure(
            query=bar_query,
            source=ProviderId.AKSHARE,
            reason=FailureReason.NO_DATA,
            failed_start=bar_query.start,
            failed_end=bar_query.end,
            detail="x" * 513,
        )
    with pytest.raises(ValidationError):
        BarFailure(
            query=bar_query,
            source=ProviderId.AKSHARE,
            reason=FailureReason.NO_DATA,
            failed_start=bar_query.start,
            failed_end=bar_query.end,
        )


def test_no_provider_failure_has_no_false_source_attribution() -> None:
    bar_query = query()
    failure = BarFailure(
        query=bar_query,
        source=None,
        reason=FailureReason.NO_PROVIDER,
        failed_start=bar_query.start,
        failed_end=bar_query.end,
        detail="no configured provider can satisfy this query",
    )

    assert failure.source is None
    with pytest.raises(ValidationError, match="source must be absent"):
        BarFailure(
            query=bar_query,
            source=ProviderId.TDX_LOCAL,
            reason=FailureReason.NO_PROVIDER,
            failed_start=bar_query.start,
            failed_end=bar_query.end,
            detail="no provider",
        )
    with pytest.raises(ValidationError, match="source is required"):
        BarFailure(
            query=bar_query,
            source=None,
            reason=FailureReason.TIMEOUT,
            failed_start=bar_query.start,
            failed_end=bar_query.end,
            detail="provider timed out",
        )


def test_capability_report_expresses_unavailable_and_partial_capabilities() -> None:
    unavailable = CapabilityReport(
        source=ProviderId.AKSHARE,
        state=CapabilityState.UNAVAILABLE,
        capabilities=frozenset(),
        available_periods=frozenset(),
        available_adjustments=frozenset(),
        markets=frozenset(),
        data_cutoff=None,
        gaps=(
            CapabilityGap(
                capability=MarketCapability.BARS,
                state=CapabilityState.PERMISSION_DENIED,
                reason=FailureReason.PERMISSION_DENIED,
                detail="credential required",
            ),
        ),
    )
    partial = CapabilityReport(
        source=ProviderId.BAOSTOCK,
        state=CapabilityState.TRANSIENT_FAILURE,
        capabilities=frozenset(
            {MarketCapability.TRADING_CALENDAR, MarketCapability.BARS}
        ),
        available_periods=frozenset({Period.MIN60, Period.DAY}),
        available_adjustments=frozenset({Adjustment.QFQ, Adjustment.NONE}),
        markets=frozenset({Exchange.SZ, Exchange.SH}),
        data_cutoff=market_time(15),
        gaps=(
            CapabilityGap(
                capability=MarketCapability.INSTRUMENTS,
                state=CapabilityState.TRANSIENT_FAILURE,
                reason=FailureReason.TIMEOUT,
                detail="instrument endpoint timed out",
            ),
        ),
    )

    assert unavailable.capabilities == frozenset()
    assert isinstance(unavailable.gaps, tuple)
    assert partial.data_cutoff is not None
    assert partial.data_cutoff.tzinfo is timezone.utc
    assert json.loads(partial.model_dump_json()) == {
        "source": "baostock",
        "state": "transient_failure",
        "capabilities": ["bars", "trading_calendar"],
        "available_periods": ["1d", "60m"],
        "available_adjustments": ["none", "qfq"],
        "markets": ["SH", "SZ"],
        "data_cutoff": "2026-07-06T07:00:00Z",
        "gaps": [
            {
                "capability": "instruments",
                "state": "transient_failure",
                "reason": "timeout",
                "detail": "instrument endpoint timed out",
            }
        ],
    }


def test_capability_gap_rejects_available_or_mismatched_failure_state() -> None:
    with pytest.raises(ValidationError, match="cannot be available"):
        CapabilityGap(
            capability=MarketCapability.BARS,
            state=CapabilityState.AVAILABLE,
            reason=FailureReason.UNSUPPORTED,
        )
    with pytest.raises(ValidationError, match="permission_denied"):
        CapabilityGap(
            capability=MarketCapability.BARS,
            state=CapabilityState.PERMISSION_DENIED,
            reason=FailureReason.TIMEOUT,
        )


def test_capability_gap_rejects_router_only_no_provider_reason() -> None:
    with pytest.raises(ValidationError, match="router-only"):
        CapabilityGap(
            capability=MarketCapability.BARS,
            state=CapabilityState.UNAVAILABLE,
            reason=FailureReason.NO_PROVIDER,
        )


def test_capability_report_rejects_constructed_router_only_gap() -> None:
    router_gap = CapabilityGap.model_construct(
        capability=MarketCapability.BARS,
        state=CapabilityState.UNAVAILABLE,
        reason=FailureReason.NO_PROVIDER,
        detail=None,
    )

    with pytest.raises(ValidationError, match="router-only"):
        CapabilityReport(
            source=ProviderId.AKSHARE,
            state=CapabilityState.UNAVAILABLE,
            gaps=(router_gap,),
        )


def test_available_capability_report_can_explain_an_unsupported_capability() -> None:
    report = CapabilityReport(
        source=ProviderId.AKSHARE,
        state=CapabilityState.AVAILABLE,
        capabilities=frozenset({MarketCapability.BARS, MarketCapability.INSTRUMENTS}),
        available_periods=frozenset({Period.DAY}),
        available_adjustments=frozenset({Adjustment.NONE}),
        markets=frozenset({Exchange.SH}),
        data_cutoff=None,
        gaps=(
            CapabilityGap(
                capability=MarketCapability.TRADING_CALENDAR,
                state=CapabilityState.UNSUPPORTED,
                reason=FailureReason.UNSUPPORTED,
                detail="open dates do not prove closed-day completeness",
            ),
        ),
    )

    assert report.state is CapabilityState.AVAILABLE
    assert report.gaps[0].capability is MarketCapability.TRADING_CALENDAR


def test_available_capability_report_rejects_non_unsupported_gaps() -> None:
    with pytest.raises(ValidationError, match="only unsupported gaps"):
        CapabilityReport(
            source=ProviderId.AKSHARE,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset({MarketCapability.BARS}),
            available_periods=frozenset({Period.DAY}),
            available_adjustments=frozenset({Adjustment.NONE}),
            markets=frozenset({Exchange.SH}),
            gaps=(
                CapabilityGap(
                    capability=MarketCapability.INSTRUMENTS,
                    state=CapabilityState.PERMISSION_DENIED,
                    reason=FailureReason.PERMISSION_DENIED,
                ),
            ),
        )


def test_capability_report_rejects_overlapping_or_duplicate_global_gaps() -> None:
    overlapping_gap = CapabilityGap(
        capability=MarketCapability.BARS,
        state=CapabilityState.UNSUPPORTED,
        reason=FailureReason.UNSUPPORTED,
    )
    with pytest.raises(ValidationError, match="both available and unavailable"):
        CapabilityReport(
            source=ProviderId.AKSHARE,
            state=CapabilityState.UNSUPPORTED,
            capabilities=frozenset({MarketCapability.BARS}),
            available_periods=frozenset({Period.DAY}),
            available_adjustments=frozenset({Adjustment.NONE}),
            markets=frozenset({Exchange.SH}),
            data_cutoff=market_time(15),
            gaps=(overlapping_gap,),
        )

    duplicate_gaps = (
        CapabilityGap(
            capability=MarketCapability.INSTRUMENTS,
            state=CapabilityState.PERMISSION_DENIED,
            reason=FailureReason.PERMISSION_DENIED,
        ),
        CapabilityGap(
            capability=MarketCapability.INSTRUMENTS,
            state=CapabilityState.UNSUPPORTED,
            reason=FailureReason.UNSUPPORTED,
        ),
    )
    with pytest.raises(ValidationError, match="unique"):
        CapabilityReport(
            source=ProviderId.AKSHARE,
            state=CapabilityState.UNAVAILABLE,
            capabilities=frozenset(),
            gaps=duplicate_gaps,
        )


def test_capability_collections_and_gaps_are_deeply_immutable() -> None:
    report = CapabilityReport(
        source=ProviderId.AKSHARE,
        state=CapabilityState.AVAILABLE,
        capabilities=frozenset({MarketCapability.BARS}),
        available_periods=frozenset({Period.DAY}),
        available_adjustments=frozenset({Adjustment.NONE}),
        markets=frozenset({Exchange.SH}),
        data_cutoff=market_time(15),
        gaps=(),
    )

    assert isinstance(report.capabilities, frozenset)
    assert isinstance(report.available_periods, frozenset)
    assert isinstance(report.available_adjustments, frozenset)
    assert isinstance(report.markets, frozenset)
    assert isinstance(report.gaps, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        report.state = CapabilityState.UNAVAILABLE


def test_bar_capability_can_be_static_without_observed_cutoff() -> None:
    report = CapabilityReport(
        source=ProviderId.TUSHARE,
        state=CapabilityState.AVAILABLE,
        capabilities=frozenset({MarketCapability.BARS}),
        available_periods=frozenset({Period.DAY}),
        available_adjustments=frozenset({Adjustment.NONE}),
        markets=frozenset({Exchange.SH}),
        data_cutoff=None,
        gaps=(),
    )

    assert report.data_cutoff is None


@pytest.mark.parametrize(
    "updates",
    [
        {"available_periods": frozenset({Period.DAY})},
        {"available_adjustments": frozenset({Adjustment.NONE})},
        {"markets": frozenset({Exchange.SH})},
        {"data_cutoff": market_time(15)},
    ],
)
def test_non_bar_capability_report_rejects_bar_metadata(
    updates: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "source": ProviderId.AKSHARE,
        "state": CapabilityState.AVAILABLE,
        "capabilities": frozenset({MarketCapability.INSTRUMENTS}),
        "available_periods": frozenset(),
        "available_adjustments": frozenset(),
        "markets": frozenset(),
        "data_cutoff": None,
        "gaps": (),
    }
    values.update(updates)

    with pytest.raises(ValidationError, match="bar metadata"):
        CapabilityReport.model_validate(values)
