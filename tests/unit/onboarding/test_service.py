from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from stock_desk.api.market import MarketServices
from stock_desk.market.execution_status import ExecutionStatusQuery
from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    InstrumentFetchOutcome,
    MarketDataProvider,
    ProviderClientError,
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
from stock_desk.onboarding.models import (
    OnboardingStatus,
    OnboardingStep,
    SynchronizationStatus,
)
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


class _AkShareStockFailureProvider(_Provider):
    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        if self.name is ProviderId.AKSHARE:
            raise ProviderUnavailable()
        return super().fetch_bars(query)


class _AkShareStockFallbackFactory:
    def __init__(self) -> None:
        self.bar_attempts: list[tuple[ProviderId, str]] = []

    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        assert token is None
        assert tdx_path is None
        provider = _AkShareStockFailureProvider(source)
        original_fetch_bars = provider.fetch_bars

        def fetch_bars(query: BarQuery) -> BarFetchOutcome:
            self.bar_attempts.append((source, query.symbol))
            return original_fetch_bars(query)

        provider.fetch_bars = fetch_bars  # type: ignore[method-assign]
        return provider


class _BaoStockCatalogMissingSelectedProvider(_AkShareStockFailureProvider):
    def fetch_instruments(self) -> InstrumentFetchOutcome:
        original = super().fetch_instruments()
        if self.name is not ProviderId.BAOSTOCK:
            return original
        return cast(
            InstrumentFetchOutcome,
            make_batch(
                source=self.name,
                operation=ProviderOperation.INSTRUMENTS,
                request={},
                items=tuple(
                    item for item in original.items if item.symbol != "600000.SH"
                ),
                data_cutoff=NOW,
                observed_at=NOW,
            ),
        )


class _BaoStockCatalogMissingSelectedFactory(_AkShareStockFallbackFactory):
    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        assert token is None
        assert tdx_path is None
        provider = _BaoStockCatalogMissingSelectedProvider(source)
        original_fetch_bars = provider.fetch_bars

        def fetch_bars(query: BarQuery) -> BarFetchOutcome:
            self.bar_attempts.append((source, query.symbol))
            return original_fetch_bars(query)

        provider.fetch_bars = fetch_bars  # type: ignore[method-assign]
        return provider


class _FutureListingProvider(_Provider):
    def fetch_instruments(self) -> InstrumentFetchOutcome:
        original = super().fetch_instruments()
        return cast(
            InstrumentFetchOutcome,
            make_batch(
                source=self.name,
                operation=ProviderOperation.INSTRUMENTS,
                request={},
                items=tuple(
                    item
                    if item.symbol != "600000.SH"
                    else item.model_copy(update={"listed_on": date(2026, 7, 14)})
                    for item in original.items
                ),
                data_cutoff=NOW,
                observed_at=NOW,
            ),
        )

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        raise AssertionError(f"future listing must not be requested: {query.symbol}")


class _FutureListingFactory:
    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        assert token is None
        assert tdx_path is None
        return _FutureListingProvider(source)


class _TimeoutProvider(_Provider):
    def __init__(
        self,
        source: ProviderId,
        *,
        fail_catalog: bool,
        fail_bars: bool,
    ) -> None:
        super().__init__(source)
        self.fail_catalog = fail_catalog
        self.fail_bars = fail_bars
        self.close_count = 0

    def fetch_instruments(self) -> InstrumentFetchOutcome:
        if self.fail_catalog:
            raise TimeoutError
        return super().fetch_instruments()

    def fetch_bars(self, query: BarQuery) -> BarFetchOutcome:
        if self.fail_bars:
            raise TimeoutError
        return super().fetch_bars(query)

    def close(self) -> None:
        self.close_count += 1
        raise RuntimeError("simulated SDK close failure")


class _TimeoutFactory:
    def __init__(self, *, fail_catalog: bool, fail_bars: bool) -> None:
        self.fail_catalog = fail_catalog
        self.fail_bars = fail_bars
        self.providers: list[_TimeoutProvider] = []

    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        assert token is None
        assert tdx_path is None
        provider = _TimeoutProvider(
            source,
            fail_catalog=self.fail_catalog,
            fail_bars=self.fail_bars,
        )
        self.providers.append(provider)
        return provider


class _WrongSourceProvider(_Provider):
    def fetch_instruments(self) -> InstrumentFetchOutcome:
        other = (
            ProviderId.BAOSTOCK
            if self.name is ProviderId.AKSHARE
            else ProviderId.AKSHARE
        )
        return _Provider(other).fetch_instruments()


class _InvalidDefaultProvider(_Provider):
    def fetch_instruments(self) -> InstrumentFetchOutcome:
        original = super().fetch_instruments()
        assert not isinstance(original, tuple)
        return cast(
            InstrumentFetchOutcome,
            make_batch(
                source=self.name,
                operation=ProviderOperation.INSTRUMENTS,
                request={},
                items=tuple(
                    item for item in original.items if item.symbol != "000001.SS"
                ),
                data_cutoff=NOW,
                observed_at=NOW,
            ),
        )


class _CatalogVariantFactory:
    def __init__(self, provider_type: type[_Provider]) -> None:
        self.provider_type = provider_type

    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        assert token is None
        assert tdx_path is None
        return self.provider_type(source)


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
        prepared = service.prepare(ProviderId.AKSHARE)
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


@pytest.mark.parametrize(
    ("listed_on", "expected_start"),
    [
        (None, date(2025, 7, 12)),
        (date(2026, 3, 2), date(2026, 3, 2)),
    ],
)
def test_initial_daily_query_is_one_year_and_never_precedes_listing(
    tmp_path: Path,
    listed_on: date | None,
    expected_start: date,
) -> None:
    service, market = _service(tmp_path)
    instrument = Instrument(
        symbol="600000.SH",
        exchange=Exchange.SH,
        name="浦发银行",
        instrument_kind=InstrumentKind.STOCK,
        listing_status=ListingStatus.LISTED,
        listed_on=listed_on,
    )
    try:
        query = service._daily_query(instrument)

        assert (
            query.start.astimezone(timezone(timedelta(hours=8))).date()
            == expected_start
        )
        assert query.end.astimezone(timezone(timedelta(hours=8))).date() == date(
            2026, 7, 13
        )
    finally:
        market.close()


def test_completed_onboarding_is_idempotent_and_cannot_change_selection(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    try:
        prepared = service.prepare(ProviderId.AKSHARE)
        service.select(prepared.instrument.symbol)
        service.synchronize(
            source_id=ProviderId.AKSHARE,
            symbol=prepared.instrument.symbol,
        )
        completed = service.complete(prepared.instrument.symbol)

        assert service.prepare() == completed
        assert service.begin_preparation() == completed
        assert service.enter_data_preparation() == completed
        assert service.demo() == completed
        assert service.exit_demo() == completed
        assert service.complete(prepared.instrument.symbol) == completed
        with pytest.raises(OnboardingConflict) as caught:
            service.complete("000001.SZ")
        assert caught.value.code == "onboarding_selection_changed"
    finally:
        market.close()


@pytest.mark.parametrize("limit", [0, 101])
def test_search_rejects_out_of_range_limits(tmp_path: Path, limit: int) -> None:
    service, market = _service(tmp_path)
    try:
        with pytest.raises(OnboardingConflict) as caught:
            service.search("000001", limit=limit)
        assert caught.value.code == "invalid_request"
    finally:
        market.close()


@pytest.mark.parametrize("query", ["   ", "x" * 65])
def test_search_rejects_invalid_queries(tmp_path: Path, query: str) -> None:
    service, market = _service(tmp_path)
    try:
        with pytest.raises(OnboardingConflict) as caught:
            service.search(query, limit=20)
        assert caught.value.code == "invalid_request"
    finally:
        market.close()


def test_selection_and_sync_reject_stale_or_unknown_user_choices(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    try:
        with pytest.raises(OnboardingConflict) as unsupported:
            service.prepare(ProviderId.TDX_LOCAL)
        assert unsupported.value.code == "unsupported_onboarding_source"

        with pytest.raises(OnboardingConflict) as catalog_missing:
            service.select("000001.SS")
        assert catalog_missing.value.code == "catalog_not_ready"

        prepared = service.begin_preparation()
        with pytest.raises(OnboardingConflict) as instrument_missing:
            service.select("999999.SH")
        assert instrument_missing.value.code == "instrument_not_found"

        with pytest.raises(OnboardingConflict) as wrong_provider:
            service.synchronize(
                source_id=ProviderId.BAOSTOCK,
                symbol=prepared.instrument.symbol,
            )
        assert wrong_provider.value.code == "onboarding_selection_changed"

        with pytest.raises(OnboardingConflict) as wrong_symbol:
            service.synchronize(
                source_id=ProviderId.AKSHARE,
                symbol="000001.SZ",
            )
        assert wrong_symbol.value.code == "onboarding_selection_changed"
    finally:
        market.close()


def test_retry_and_provider_switch_follow_the_persisted_step(tmp_path: Path) -> None:
    service, market = _service(tmp_path)
    try:
        prepared = service.begin_preparation()
        assert service.retry() == prepared

        selected = service.select(prepared.instrument.symbol)
        retried = service.retry()
        assert retried.current_step is OnboardingStep.SYNCHRONIZATION
        assert retried.sync is not None
        assert retried.sync.status.value == "verified"

        switched_to_bao = service.switch_provider()
        assert switched_to_bao.source is not None
        assert switched_to_bao.source.id is ProviderId.BAOSTOCK
        switched_back = service.switch_provider()
        assert switched_back.source is not None
        assert switched_back.source.id is ProviderId.AKSHARE
        assert selected.instrument == switched_back.instrument
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
        prepared = service.prepare(ProviderId.AKSHARE)

        assert factory.attempted == [ProviderId.AKSHARE, ProviderId.BAOSTOCK]
        assert prepared.current_step is OnboardingStep.INSTRUMENT_SELECTION
        assert prepared.source is not None
        assert prepared.source.id is ProviderId.BAOSTOCK
        assert prepared.instrument.symbol == "000001.SS"
    finally:
        market.close()


def test_sources_are_unknown_until_a_real_provider_attempt_succeeds(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    try:
        assert [item["status"] for item in service.sources()] == [
            "unknown",
            "unknown",
        ]

        prepared = service.prepare(ProviderId.AKSHARE)

        assert prepared.source is not None
        assert [item["status"] for item in service.sources()] == [
            "ready",
            "unknown",
        ]
    finally:
        market.close()


def test_akshare_stock_bar_failure_retries_the_complete_flow_with_baostock(
    tmp_path: Path,
) -> None:
    _unused_service, market = _service(tmp_path)
    factory = _AkShareStockFallbackFactory()
    service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "stock-fallback-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=factory,
        clock=lambda: NOW,
    )
    try:
        prepared = service.prepare(ProviderId.AKSHARE)
        assert prepared.source is not None
        assert prepared.source.id is ProviderId.AKSHARE

        service.select("600000.SH")
        synchronized = service.synchronize(
            source_id=ProviderId.AKSHARE,
            symbol="600000.SH",
        )

        assert factory.bar_attempts == [
            (ProviderId.AKSHARE, "600000.SH"),
            (ProviderId.BAOSTOCK, "600000.SH"),
        ]
        assert synchronized.source is not None
        assert synchronized.source.id is ProviderId.BAOSTOCK
        assert synchronized.instrument.symbol == "600000.SH"
        assert synchronized.sync is not None
        assert synchronized.sync.status is SynchronizationStatus.VERIFIED
        assert synchronized.sync.provider_id is ProviderId.BAOSTOCK
        assert synchronized.error is None

        completed = service.complete("600000.SH")
        assert completed.status is OnboardingStatus.COMPLETED
        assert completed.instrument.symbol == "600000.SH"
    finally:
        market.close()


def test_akshare_default_index_failure_retries_with_baostock(
    tmp_path: Path,
) -> None:
    _unused_service, market = _service(tmp_path)
    factory = _AkShareStockFallbackFactory()
    service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "index-fallback-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=factory,
        clock=lambda: NOW,
    )
    try:
        prepared = service.prepare(ProviderId.AKSHARE)
        service.select(prepared.instrument.symbol)

        synchronized = service.synchronize(
            source_id=ProviderId.AKSHARE,
            symbol="000001.SS",
        )

        assert factory.bar_attempts == [
            (ProviderId.AKSHARE, "000001.SS"),
            (ProviderId.BAOSTOCK, "000001.SS"),
        ]
        assert synchronized.source is not None
        assert synchronized.source.id is ProviderId.BAOSTOCK
        assert synchronized.sync is not None
        assert synchronized.sync.status is SynchronizationStatus.VERIFIED
        assert synchronized.sync.provider_id is ProviderId.BAOSTOCK
        assert synchronized.error is None
    finally:
        market.close()


def test_baostock_fallback_catalog_miss_preserves_the_selected_stock(
    tmp_path: Path,
) -> None:
    _unused_service, market = _service(tmp_path)
    factory = _BaoStockCatalogMissingSelectedFactory()
    service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "fallback-catalog-miss-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=factory,
        clock=lambda: NOW,
    )
    try:
        prepared = service.prepare(ProviderId.AKSHARE)
        service.select("600000.SH")

        failed = service.synchronize(
            source_id=ProviderId.AKSHARE,
            symbol="600000.SH",
        )

        assert failed.source is not None
        assert failed.source.id is ProviderId.AKSHARE
        assert failed.instrument.symbol == "600000.SH"
        assert failed.sync is not None
        assert failed.sync.status is SynchronizationStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "provider_unavailable"

        retried = service.retry()
        assert retried.source is not None
        assert retried.source.id is ProviderId.AKSHARE
        assert retried.instrument.symbol == "600000.SH"
        assert factory.bar_attempts == [
            (ProviderId.AKSHARE, "600000.SH"),
            (ProviderId.AKSHARE, "600000.SH"),
        ]
        assert prepared.instrument.symbol == "000001.SS"
    finally:
        market.close()


def test_future_listing_becomes_recoverable_no_data_instead_of_api_error(
    tmp_path: Path,
) -> None:
    _unused_service, market = _service(tmp_path)
    service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "future-listing-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=_FutureListingFactory(),
        clock=lambda: NOW,
    )
    try:
        service.prepare(ProviderId.AKSHARE)
        service.select("600000.SH")

        failed = service.synchronize(
            source_id=ProviderId.AKSHARE,
            symbol="600000.SH",
        )

        assert failed.instrument.symbol == "600000.SH"
        assert failed.sync is not None
        assert failed.sync.status is SynchronizationStatus.FAILED
        assert failed.error is not None
        assert failed.error.code == "provider_no_data"
        assert failed.error.actions == (
            "retry",
            "switch_provider",
            "advanced",
            "demo",
        )
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


def test_provider_timeouts_remain_primary_when_sdk_cleanup_also_fails(
    tmp_path: Path,
) -> None:
    _unused_service, market = _service(tmp_path)
    catalog_factory = _TimeoutFactory(fail_catalog=True, fail_bars=False)
    catalog_service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "catalog-timeout-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=catalog_factory,
        clock=lambda: NOW,
    )
    bars_factory = _TimeoutFactory(fail_catalog=False, fail_bars=True)
    bars_service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / "bars-timeout-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=bars_factory,
        clock=lambda: NOW,
    )
    try:
        catalog_failed = catalog_service.begin_preparation()
        assert catalog_failed.error is not None
        assert catalog_failed.error.code == "provider_timeout"
        assert len(catalog_factory.providers) == 2
        assert all(item.close_count == 1 for item in catalog_factory.providers)

        prepared = bars_service.begin_preparation()
        bars_service.select(prepared.instrument.symbol)
        bars_failed = bars_service.synchronize(
            source_id=ProviderId.AKSHARE,
            symbol=prepared.instrument.symbol,
        )
        assert bars_failed.error is not None
        assert bars_failed.error.code == "provider_timeout"
        assert bars_failed.sync is not None
        assert bars_failed.sync.status is SynchronizationStatus.FAILED
        # Preparation opens both catalog providers; synchronization then opens
        # AKShare and its BaoStock fallback for the default index.
        assert len(bars_factory.providers) == 4
        assert all(item.close_count == 1 for item in bars_factory.providers)
    finally:
        market.close()


@pytest.mark.parametrize(
    ("provider_type", "expected_code"),
    [
        (_WrongSourceProvider, "provider_invalid_response"),
        (_InvalidDefaultProvider, "catalog_verification_failed"),
    ],
)
def test_catalog_identity_failures_never_commit_partial_provider_data(
    tmp_path: Path,
    provider_type: type[_Provider],
    expected_code: str,
) -> None:
    _unused_service, market = _service(tmp_path)
    service = OnboardingService(
        store=OnboardingStateStore(
            tmp_path / f"{provider_type.__name__}-state-v1.json", clock=lambda: NOW
        ),
        market=market,
        provider_factory=_CatalogVariantFactory(provider_type),
        clock=lambda: NOW,
    )
    try:
        failed = service.begin_preparation()
        assert failed.source is None
        assert failed.error is not None
        assert failed.error.code == expected_code
    finally:
        market.close()


def test_internal_provider_error_classification_is_stable_and_exhaustive() -> None:
    assert (
        OnboardingService._exception_code(ProviderClientError("unsafe detail"))
        == "provider_invalid_response"
    )

    class _UnknownProviderError(ProviderClientError):
        reason = "unexpected"

    assert (
        OnboardingService._exception_code(_UnknownProviderError())
        == "provider_unavailable"
    )


def test_persisted_catalog_references_are_revalidated_before_search(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    store = OnboardingStateStore(tmp_path / "state-v1.json", clock=lambda: NOW)
    try:
        prepared = service.begin_preparation()
        assert prepared.source is not None

        missing_reference = prepared.source.model_copy(
            update={"catalog_manifest_record_id": "sha256:" + "0" * 64}
        )
        store.save(prepared.model_copy(update={"source": missing_reference}))
        with pytest.raises(OnboardingConflict) as missing:
            service.search("000001", limit=20)
        assert missing.value.code == "catalog_verification_failed"

        mismatched_version = prepared.source.model_copy(
            update={"catalog_dataset_version": "sha256:" + "f" * 64}
        )
        store.save(prepared.model_copy(update={"source": mismatched_version}))
        with pytest.raises(OnboardingConflict) as mismatched:
            service.search("000001", limit=20)
        assert mismatched.value.code == "catalog_verification_failed"
    finally:
        market.close()
    no_attempts = SimpleNamespace(audit=SimpleNamespace(attempts=()))
    assert (
        OnboardingService._failure_code(no_attempts)  # type: ignore[arg-type]
        == "provider_unavailable"
    )


def test_bar_evidence_must_be_complete_sorted_and_identity_matched(
    tmp_path: Path,
) -> None:
    service, market = _service(tmp_path)
    try:
        prepared = service.begin_preparation()
        instrument = market.instruments.get(prepared.instrument.symbol).instrument
        query = service._daily_query(instrument)
        routed, _provider = service._fetch_bars(ProviderId.AKSHARE, query)

        incomplete = routed.model_copy(
            update={
                "result": routed.result.model_copy(
                    update={"coverage_start": query.start + timedelta(days=1)}
                )
            }
        )
        with pytest.raises(ValueError, match="bar evidence is incomplete"):
            service._validate_bar_result(incomplete, ProviderId.AKSHARE, query)

        unsorted = routed.model_copy(
            update={
                "result": routed.result.model_copy(
                    update={"bars": tuple(reversed(routed.result.bars))}
                )
            }
        )
        with pytest.raises(ValueError, match="bars are not strictly sorted"):
            service._validate_bar_result(unsorted, ProviderId.AKSHARE, query)

        mismatched = routed.model_copy(
            update={
                "result": routed.result.model_copy(
                    update={
                        "bars": (
                            routed.result.bars[0].model_copy(
                                update={"symbol": "000001.SZ"}
                            ),
                            *routed.result.bars[1:],
                        )
                    }
                )
            }
        )
        with pytest.raises(ValueError, match="bar identity mismatch"):
            service._validate_bar_result(mismatched, ProviderId.AKSHARE, query)
    finally:
        market.close()
