from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from stock_desk.backtest.execution import (
    ExecutionEngine,
    ExecutionRequest,
    ReferenceOpen,
    SignalBar,
    candidates_from_status,
)

from stock_desk.market.execution_status import (
    ExecutionStatusEvidenceLevel,
    ExecutionStatusQuery,
    ExecutionStatusSnapshot,
    SuspensionState,
)
from stock_desk.market.providers.tushare import TushareProvider
from stock_desk.market.providers.execution_status import ExecutionStatusFailure
from stock_desk.market.types import (
    CapabilityState,
    Exchange,
    MarketCapability,
    Period,
    ProviderId,
)
from tests.contract.providers.conftest import ProviderCase


class FakeExecutionStatusClient:
    def __init__(self) -> None:
        self.suspend_calls: list[dict[str, object]] = []

    def trade_cal(self, **_kwargs: object) -> object:
        return [
            {"exchange": "SSE", "cal_date": "20260105", "is_open": "1"},
            {"exchange": "SSE", "cal_date": "20260106", "is_open": "1"},
            {"exchange": "SSE", "cal_date": "20260107", "is_open": "0"},
        ]

    def stock_basic(self, **_kwargs: object) -> object:
        return []

    def pro_bar(self, **_kwargs: object) -> object:
        return [
            {
                "ts_code": "600000.SH",
                "trade_date": "20260105",
                "open": "11.00",
                "high": "11.00",
                "low": "10.50",
                "close": "10.80",
                "vol": "100",
            },
            {
                "ts_code": "600000.SH",
                "trade_date": "20260106",
                "open": "9.20",
                "high": "9.50",
                "low": "9.20",
                "close": "9.40",
                "vol": "100",
            },
        ]

    def suspend_d(self, **_kwargs: object) -> object:
        self.suspend_calls.append(_kwargs)
        return [{"ts_code": "600000.SH", "trade_date": "20260106"}]

    def stk_limit(self, **_kwargs: object) -> object:
        return [
            {
                "ts_code": "600000.SH",
                "trade_date": "20260105",
                "up_limit": "11.00",
                "down_limit": "9.00",
            },
            {
                "ts_code": "600000.SH",
                "trade_date": "20260106",
                "up_limit": "11.20",
                "down_limit": "9.20",
            },
        ]


def test_tushare_materializes_authoritative_execution_status() -> None:
    client = FakeExecutionStatusClient()
    provider = TushareProvider(
        client=client,
        clock=lambda: datetime(2026, 1, 8, tzinfo=timezone.utc),
    )

    result = provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2026, 1, 5),
            end=date(2026, 1, 8),
        )
    )

    assert isinstance(result, ExecutionStatusSnapshot)
    assert result.evidence_level is ExecutionStatusEvidenceLevel.AUTHORITATIVE
    assert tuple(item.suspension_state for item in result.days) == (
        SuspensionState.NORMAL,
        SuspensionState.SUSPENDED,
        SuspensionState.NOT_APPLICABLE,
    )
    assert result.eligibility[0].buy_blocked_at_open is True
    assert result.eligibility[0].sell_blocked_at_open is False
    assert result.eligibility[1].buy_blocked_at_open is False
    assert result.eligibility[1].sell_blocked_at_open is True
    assert client.suspend_calls == [
        {
            "ts_code": "600000.SH",
            "start_date": "20260105",
            "end_date": "20260107",
            "suspend_type": "S",
            "fields": "ts_code,trade_date",
        }
    ]


def test_only_providers_with_explicit_execution_evidence_advertise_status(
    provider_case: ProviderCase,
) -> None:
    provider, _client = provider_case.build()
    report = provider.capabilities()

    if provider_case.source in {ProviderId.TUSHARE, ProviderId.BAOSTOCK}:
        assert MarketCapability.EXECUTION_STATUS in report.capabilities
    else:
        assert MarketCapability.EXECUTION_STATUS not in report.capabilities
        gap = next(
            item
            for item in report.gaps
            if item.capability is MarketCapability.EXECUTION_STATUS
        )
        assert gap.state is CapabilityState.UNSUPPORTED


def test_baostock_materializes_basic_status_without_price_limit_claims(
    provider_case: ProviderCase,
) -> None:
    if provider_case.source is not ProviderId.BAOSTOCK:
        return
    provider, client = provider_case.build()
    assert isinstance(client.fixture["calendar"], list)
    client.fixture["calendar"] = client.fixture["calendar"][:2]

    result = provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2024, 7, 1),
            end=date(2024, 7, 3),
        )
    )

    assert isinstance(result, ExecutionStatusSnapshot)
    assert result.evidence_level is ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS
    assert tuple(item.suspension_state for item in result.days) == (
        SuspensionState.NORMAL,
        SuspensionState.SUSPENDED,
    )
    assert all(item.raw_upper_limit is None for item in result.days)
    assert all(item.raw_lower_limit is None for item in result.days)
    assert all(not item.buy_blocked_at_open for item in result.eligibility)
    assert all(not item.sell_blocked_at_open for item in result.eligibility)


def test_baostock_never_infers_suspension_from_missing_bar(
    provider_case: ProviderCase,
) -> None:
    if provider_case.source is not ProviderId.BAOSTOCK:
        return
    provider, client = provider_case.build()
    assert isinstance(client.fixture["calendar"], list)
    client.fixture["calendar"] = client.fixture["calendar"][:2]
    assert isinstance(client.fixture["bars"], dict)
    client.fixture["bars"]["1d"] = client.fixture["bars"]["1d"][1:]

    result = provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2024, 7, 1),
            end=date(2024, 7, 3),
        )
    )

    assert isinstance(result, ExecutionStatusFailure)


def test_baostock_basic_status_supports_weekly_and_60m_backtests(
    provider_case: ProviderCase,
) -> None:
    if provider_case.source is not ProviderId.BAOSTOCK:
        return
    weekly_provider, weekly_client = provider_case.build()
    assert isinstance(weekly_client.fixture["calendar"], list)
    weekly_client.fixture["calendar"] = weekly_client.fixture["calendar"][:2]
    minute_provider, minute_client = provider_case.build()
    assert isinstance(minute_client.fixture["calendar"], list)
    minute_client.fixture["calendar"] = minute_client.fixture["calendar"][:1]

    weekly = weekly_provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2024, 7, 1),
            end=date(2024, 7, 3),
            period=Period.WEEK,
        )
    )
    minute = minute_provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2024, 7, 1),
            end=date(2024, 7, 2),
            period=Period.MIN60,
        )
    )

    assert isinstance(weekly, ExecutionStatusSnapshot)
    assert isinstance(minute, ExecutionStatusSnapshot)
    assert weekly.evidence_level is ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS
    assert minute.evidence_level is ExecutionStatusEvidenceLevel.BASIC_NO_PRICE_LIMITS
    assert len(weekly.eligibility) == 2
    assert len(minute.eligibility) == 4


def test_tushare_materializes_60m_eligibility_at_exact_fill_timestamps() -> None:
    class MinuteClient(FakeExecutionStatusClient):
        def trade_cal(self, **_kwargs: object) -> object:
            return [{"exchange": "SSE", "cal_date": "20260105", "is_open": "1"}]

        def suspend_d(self, **_kwargs: object) -> object:
            return []

        def stk_limit(self, **_kwargs: object) -> object:
            return [
                {
                    "ts_code": "600000.SH",
                    "trade_date": "20260105",
                    "up_limit": "11.00",
                    "down_limit": "9.00",
                }
            ]

        def pro_bar(self, **_kwargs: object) -> object:
            return [
                {
                    "ts_code": "600000.SH",
                    "trade_time": "2026-01-05 11:30:00",
                    "open": "10.20",
                    "high": "10.30",
                    "low": "10.10",
                    "close": "10.25",
                    "vol": "100",
                },
                {
                    "ts_code": "600000.SH",
                    "trade_time": "2026-01-05 10:30:00",
                    "open": "10.00",
                    "high": "10.20",
                    "low": "9.90",
                    "close": "10.10",
                    "vol": "100",
                },
            ]

    provider = TushareProvider(
        client=MinuteClient(),
        clock=lambda: datetime(2026, 1, 6, tzinfo=timezone.utc),
    )

    result = provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2026, 1, 5),
            end=date(2026, 1, 6),
            period=Period.MIN60,
        )
    )

    assert isinstance(result, ExecutionStatusSnapshot)
    assert tuple(
        item.timestamp.astimezone(timezone.utc).hour for item in result.eligibility
    ) == (1, 2)


def test_weekly_provider_overlay_blocks_suspended_no_bar_then_fills_tuesday() -> None:
    class WeeklySuspensionClient(FakeExecutionStatusClient):
        def trade_cal(self, **_kwargs: object) -> object:
            return [
                {"exchange": "SSE", "cal_date": "20260112", "is_open": "1"},
                {"exchange": "SSE", "cal_date": "20260113", "is_open": "1"},
            ]

        def pro_bar(self, **_kwargs: object) -> object:
            return [
                {
                    "ts_code": "600000.SH",
                    "trade_date": "20260113",
                    "open": "10.00",
                    "high": "10.20",
                    "low": "9.90",
                    "close": "10.10",
                    "vol": "100",
                }
            ]

        def suspend_d(self, **_kwargs: object) -> object:
            return [{"ts_code": "600000.SH", "trade_date": "20260112"}]

        def stk_limit(self, **_kwargs: object) -> object:
            return [
                {
                    "ts_code": "600000.SH",
                    "trade_date": day,
                    "up_limit": "11.00",
                    "down_limit": "9.00",
                }
                for day in ("20260112", "20260113")
            ]

    provider = TushareProvider(
        client=WeeklySuspensionClient(),
        clock=lambda: datetime(2026, 1, 14, tzinfo=timezone.utc),
    )
    status = provider.fetch_execution_status(
        ExecutionStatusQuery(
            symbol="600000.SH",
            exchange=Exchange.SH,
            start=date(2026, 1, 12),
            end=date(2026, 1, 14),
            period=Period.WEEK,
        )
    )
    assert isinstance(status, ExecutionStatusSnapshot)
    candidates = candidates_from_status(
        status,
        reference_opens=(
            ReferenceOpen(
                timestamp=datetime(2026, 1, 13, 1, 30, tzinfo=timezone.utc),
                price=Decimal("10"),
            ),
        ),
    )

    result = ExecutionEngine().run(
        ExecutionRequest(
            period=Period.WEEK,
            signals=(
                SignalBar(
                    timestamp=datetime(2026, 1, 9, 7, tzinfo=timezone.utc),
                    buy=True,
                ),
            ),
            candidates=candidates,
        )
    )

    assert result.failure is None
    assert result.blocked_events[0].reason == "suspended"
    assert result.trades[0].entry.timestamp == datetime(
        2026, 1, 13, 1, 30, tzinfo=timezone.utc
    )
