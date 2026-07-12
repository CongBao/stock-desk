from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from stock_desk.api.market import MarketServices
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    InstrumentFetchOutcome,
    MarketDataProvider,
    ProviderOperation,
    ProviderUnavailable,
)
from stock_desk.market.providers.execution_status import (
    ExecutionStatusFailure,
    ExecutionStatusFetchOutcome,
)
from stock_desk.market.providers.normalization import make_bar_result, make_batch
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
from stock_desk.onboarding.models import OnboardingStatus, OnboardingStep
from stock_desk.onboarding.service import OnboardingConflict, OnboardingService
from stock_desk.onboarding.store import OnboardingStateStore


NOW = datetime(2026, 7, 12, 4, tzinfo=timezone.utc)


class _Provider:
    def __init__(self, source: ProviderId) -> None:
        self.name = source

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(
                {MarketCapability.INSTRUMENTS, MarketCapability.BARS}
            ),
            available_periods=frozenset({Period.DAY}),
            available_adjustments=frozenset({Adjustment.NONE}),
            markets=frozenset({Exchange.SH, Exchange.SZ}),
            data_cutoff=NOW,
            gaps=(
                CapabilityGap(
                    capability=MarketCapability.TRADING_CALENDAR,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                ),
                CapabilityGap(
                    capability=MarketCapability.EXECUTION_STATUS,
                    state=CapabilityState.UNSUPPORTED,
                    reason=FailureReason.UNSUPPORTED,
                ),
            ),
        )

    def fetch_instruments(self) -> InstrumentFetchOutcome:
        items = (
            Instrument(
                symbol="000001.SS",
                exchange=Exchange.SH,
                name="上证指数",
                instrument_kind=InstrumentKind.INDEX,
                listing_status=ListingStatus.LISTED,
            ),
            Instrument(
                symbol="000001.SZ",
                exchange=Exchange.SZ,
                name="平安银行",
                instrument_kind=InstrumentKind.STOCK,
                listing_status=ListingStatus.LISTED,
            ),
            Instrument(
                symbol="600000.SH",
                exchange=Exchange.SH,
                name="浦发银行",
                instrument_kind=InstrumentKind.STOCK,
                listing_status=ListingStatus.LISTED,
            ),
        )
        return cast(
            InstrumentFetchOutcome,
            make_batch(
                source=self.name,
                operation=ProviderOperation.INSTRUMENTS,
                request={},
                items=items,
                data_cutoff=NOW,
                observed_at=NOW,
            ),
        )

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        days = (date(2026, 7, 9), date(2026, 7, 10))
        normalized = tuple(
            (
                Bar(
                    symbol=query.symbol,
                    timestamp=datetime(
                        day.year,
                        day.month,
                        day.day,
                        tzinfo=timezone(timedelta(hours=8)),
                    ),
                    period=Period.DAY,
                    adjustment=Adjustment.NONE,
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.5"),
                    volume=1000,
                    status=TradingStatus.UNKNOWN,
                ),
                datetime(
                    day.year,
                    day.month,
                    day.day,
                    15,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
            )
            for day in days
        )
        return make_bar_result(
            source=self.name,
            query=query,
            normalized=normalized,
            clock=lambda: NOW,
        )

    def fetch_calendar(
        self, exchange: Exchange, start: date, end: date
    ) -> CalendarFetchOutcome:
        del exchange, start, end
        raise AssertionError("not used")

    def fetch_execution_status(
        self, query: ExecutionStatusQuery
    ) -> ExecutionStatusFetchOutcome:
        return ExecutionStatusFailure(
            query=query,
            source=self.name,
            reason=FailureReason.UNSUPPORTED,
            detail="provider does not support authoritative execution status",
        )


class _Factory:
    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        assert token is None
        assert tdx_path is None
        return _Provider(source)


class _UnavailableFactory:
    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        del source, token, tdx_path
        raise ProviderUnavailable()


class _FallbackFactory:
    def __init__(self) -> None:
        self.attempted: list[ProviderId] = []

    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        assert token is None
        assert tdx_path is None
        self.attempted.append(source)
        if source is ProviderId.AKSHARE:
            raise ProviderUnavailable()
        return _Provider(source)


def _service(tmp_path: Path) -> tuple[OnboardingService, MarketServices]:
    database_url = f"sqlite:///{tmp_path / 'onboarding.db'}"
    market = MarketServices.open(
        database_url=database_url,
        lake_root=(tmp_path / "market").resolve(),
    )
    service = OnboardingService(
        store=OnboardingStateStore(tmp_path / "state-v1.json", clock=lambda: NOW),
        market=market,
        provider_factory=_Factory(),
        clock=lambda: NOW,
    )
    return service, market


def test_real_catalog_and_single_provider_daily_pin_are_required_for_completion(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    try:
        prepared = service.begin_preparation()
        assert prepared.current_step is OnboardingStep.INSTRUMENT_SELECTION
        assert prepared.source is not None
        assert prepared.source.id is ProviderId.AKSHARE
        assert prepared.instrument.symbol == "000001.SS"
        assert prepared.instrument.instrument_kind is InstrumentKind.INDEX

        assert [item.symbol for item in service.search("shangzheng", limit=10)] == [
            "000001.SS"
        ]
        assert [item.symbol for item in service.search("pfyh", limit=10)] == [
            "600000.SH"
        ]

        selected = service.select("000001.SS")
        synced = service.synchronize(
            source_id=ProviderId.AKSHARE,
            symbol=selected.instrument.symbol,
        )
        assert synced.sync is not None
        assert synced.sync.provider_id is ProviderId.AKSHARE
        assert synced.sync.row_count == 2
        assert synced.status is OnboardingStatus.IN_PROGRESS

        completed = service.complete("000001.SS")
        assert completed.status is OnboardingStatus.COMPLETED
        assert completed.current_step is OnboardingStep.COMPLETED
        assert completed.demo_mode is False
    finally:
        market.close()


def test_preparation_falls_back_only_after_the_whole_provider_attempt_fails(
    tmp_path: Path,
) -> None:
    _unused_service, market = _service(tmp_path)
    factory = _FallbackFactory()
    service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "fallback-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=factory,
        clock=lambda: NOW,
    )
    try:
        prepared = service.begin_preparation()

        assert factory.attempted == [ProviderId.AKSHARE, ProviderId.BAOSTOCK]
        assert prepared.current_step is OnboardingStep.INSTRUMENT_SELECTION
        assert prepared.source is not None
        assert prepared.source.id is ProviderId.BAOSTOCK
        assert prepared.instrument.symbol == "000001.SS"
    finally:
        market.close()


def test_demo_mode_never_marks_onboarding_completed(tmp_path: Path) -> None:
    service, market = _service(tmp_path)
    try:
        demo = service.demo()
        assert demo.demo_mode is True
        assert demo.status is OnboardingStatus.IN_PROGRESS
        assert demo.current_step is not OnboardingStep.COMPLETED
        with pytest.raises(OnboardingConflict) as caught:
            service.complete("000001.SS")
        assert caught.value.code == "synchronization_not_verified"
    finally:
        market.close()


def test_demo_mode_survives_restart_and_exit_can_prepare_real_data(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    state_path = tmp_path / "state-v1.json"
    try:
        service.demo()
        restarted = OnboardingService(
            store=OnboardingStateStore(state_path, clock=lambda: NOW),
            market=market,
            provider_factory=_Factory(),
            clock=lambda: NOW,
        )

        restored = restarted.state()
        assert restored.demo_mode is True
        assert restored.source is None

        exited = restarted.exit_demo()
        assert exited.demo_mode is False
        assert exited.current_step is OnboardingStep.DATA_PREPARATION
        assert exited.source is None
        assert exited.error is None

        prepared = restarted.prepare()
        assert prepared.demo_mode is False
        assert prepared.current_step is OnboardingStep.INSTRUMENT_SELECTION
        assert prepared.source is not None
        assert prepared.source.id is ProviderId.AKSHARE
    finally:
        market.close()


def test_provider_failures_expose_only_stable_recovery_codes(tmp_path: Path) -> None:
    _unused_service, market = _service(tmp_path)
    unavailable = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "failed-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=_UnavailableFactory(),
        clock=lambda: NOW,
    )
    try:
        failed = unavailable.begin_preparation()
        assert failed.current_step is OnboardingStep.DATA_PREPARATION
        assert failed.error is not None
        assert failed.error.code == "provider_unavailable"
        assert failed.error.actions == (
            "retry",
            "switch_provider",
            "advanced",
            "demo",
        )
        assert "ProviderUnavailable" not in failed.model_dump_json()
    finally:
        market.close()
