from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusQuery,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.providers.execution_status import ExecutionStatusFailure
from stock_desk.market.provenance import RoutedExecutionStatusSuccess, RoutingDecision
from stock_desk.market.routing import SourcePriorities, SourceRouter
from stock_desk.market.types import (
    Adjustment,
    CapabilityGap,
    CapabilityReport,
    CapabilityState,
    Exchange,
    FailureReason,
    MarketCapability,
    Period,
    ProviderId,
)


class StatusProvider:
    def __init__(self, source: ProviderId, *, supported: bool) -> None:
        self.name = source
        self.supported = supported

    def capabilities(self) -> CapabilityReport:
        capabilities = {
            MarketCapability.BARS,
            MarketCapability.INSTRUMENTS,
            MarketCapability.TRADING_CALENDAR,
        }
        gaps = ()
        if self.supported:
            capabilities.add(MarketCapability.EXECUTION_STATUS)
        else:
            gaps = (
                CapabilityGap(
                    capability=MarketCapability.EXECUTION_STATUS,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                    detail="authoritative execution status is unavailable",
                ),
            )
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(capabilities),
            available_periods=frozenset(Period),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset(Exchange),
            gaps=gaps,
        )

    def fetch_execution_status(self, query: ExecutionStatusQuery):
        if not self.supported:
            return ExecutionStatusFailure(
                query=query,
                source=self.name,
                reason=FailureReason.UNSUPPORTED,
                detail="provider does not support authoritative execution status",
            )
        return materialize_execution_status(
            query=query,
            days=(
                ExecutionStatusDay(
                    day=query.start,
                    exchange=query.exchange,
                    is_exchange_open=True,
                    suspension_state=SuspensionState.NORMAL,
                    raw_upper_limit=Decimal("11"),
                    raw_lower_limit=Decimal("9"),
                ),
            ),
            raw_opens=(
                RawExecutionOpen(
                    timestamp=datetime(2026, 1, 5, 1, 30, tzinfo=timezone.utc),
                    trading_day=query.start,
                    raw_open=Decimal("10"),
                ),
            ),
            source=self.name,
            fetched_at=datetime(2026, 1, 6, tzinfo=timezone.utc),
            data_cutoff=datetime(2026, 1, 5, 7, tzinfo=timezone.utc),
        )

    def fetch_bars(self, query):
        raise AssertionError(query)

    def fetch_instruments(self):
        raise AssertionError

    def fetch_calendar(self, exchange, start, end):
        raise AssertionError((exchange, start, end))


def test_execution_status_routes_independently_from_bar_priority() -> None:
    query = ExecutionStatusQuery(
        symbol="600000.SH",
        exchange=Exchange.SH,
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
    )
    router = SourceRouter(
        (
            (ProviderId.AKSHARE, StatusProvider(ProviderId.AKSHARE, supported=False)),
            (ProviderId.TUSHARE, StatusProvider(ProviderId.TUSHARE, supported=True)),
        ),
        priorities=SourcePriorities(
            daily_bars=(ProviderId.AKSHARE,),
            execution_status=(ProviderId.AKSHARE, ProviderId.TUSHARE),
        ),
    )

    outcome = router.fetch_execution_status(query)

    assert isinstance(outcome, RoutedExecutionStatusSuccess)
    assert outcome.manifest.selected_source is ProviderId.TUSHARE
    assert outcome.manifest.attempts[0].decision is RoutingDecision.CAPABILITY_SKIP
    assert router.priorities().daily_bars == (ProviderId.AKSHARE,)
    assert outcome.result.source is ProviderId.TUSHARE


def test_default_execution_status_routes_authoritative_then_basic_fallback() -> None:
    assert SourcePriorities().execution_status == (
        ProviderId.TUSHARE,
        ProviderId.BAOSTOCK,
    )
