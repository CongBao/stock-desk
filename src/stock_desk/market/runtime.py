"""Settings-backed provider construction for production market updates."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Protocol

from stock_desk.api.settings import RuntimeSourceSettings
from stock_desk.market.providers import (
    AkShareProvider,
    BaoStockProvider,
    MarketDataProvider,
    ProviderClientError,
    ProviderMissingCoverage,
    ProviderPermissionDenied,
    ProviderUnavailable,
    TdxLocalProvider,
    TushareProvider,
)
from stock_desk.market.providers.base import (
    CalendarFetchOutcome,
    InstrumentFetchOutcome,
)
from stock_desk.market.routing import SourcePriorities, SourceRouter
from stock_desk.market.types import (
    BarFetchOutcome,
    BarQuery,
    CapabilityReport,
    Exchange,
    ProviderId,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeProviderFactory(Protocol):
    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider: ...


class DefaultRuntimeProviderFactory:
    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock

    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider:
        if source is ProviderId.TUSHARE:
            if token is None:
                raise ValueError("Tushare is not configured")
            return TushareProvider.from_sdk(token=token, clock=self._clock)
        if source is ProviderId.AKSHARE:
            return AkShareProvider.from_sdk(clock=self._clock)
        if source is ProviderId.BAOSTOCK:
            return BaoStockProvider.from_sdk(clock=self._clock)
        if source is ProviderId.TDX_LOCAL:
            if tdx_path is None:
                raise ValueError("TDX local source is not configured")
            return TdxLocalProvider(root=tdx_path, clock=self._clock)
        raise ValueError("Configured provider has no runtime implementation")


class _UnavailableProvider:
    def __init__(
        self, source: ProviderId, error_type: type[ProviderClientError]
    ) -> None:
        self.name = source
        self._error_type = error_type

    def capabilities(self) -> CapabilityReport:
        raise self._error_type()

    def fetch_bars(self, _query: BarQuery) -> BarFetchOutcome:
        raise self._error_type()

    def fetch_instruments(self) -> InstrumentFetchOutcome:
        raise self._error_type()

    def fetch_calendar(
        self,
        _exchange: Exchange,
        _start: date,
        _end: date,
    ) -> CalendarFetchOutcome:
        raise self._error_type()


def _placeholder_error(
    source: ProviderId,
    error: Exception | None,
) -> type[ProviderClientError]:
    if source is ProviderId.TUSHARE and error is None:
        return ProviderPermissionDenied
    if source is ProviderId.TDX_LOCAL and error is None:
        return ProviderMissingCoverage
    if isinstance(error, ProviderClientError):
        return type(error)
    return ProviderUnavailable


def _close_all(providers: tuple[MarketDataProvider, ...]) -> None:
    failures = 0
    for provider in reversed(providers):
        close = getattr(provider, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception:
            failures += 1
    if failures:
        raise RuntimeError("one or more market providers failed to close safely")


def _configured_sources(settings: RuntimeSourceSettings) -> tuple[ProviderId, ...]:
    priorities = settings.priorities
    ordered = (
        *priorities.daily_bars,
        *priorities.weekly_bars,
        *priorities.minute_bars,
        *priorities.instruments,
        *priorities.trading_calendar,
    )
    return tuple(dict.fromkeys(ordered))


class MarketProviderRuntime:
    """One closeable router snapshot; missing SDKs remain honest routing misses."""

    def __init__(
        self,
        *,
        router: SourceRouter,
        providers: tuple[MarketDataProvider, ...],
    ) -> None:
        self.router = router
        self._providers = providers
        self._close_lock = Lock()
        self._closed = False

    @classmethod
    def build(
        cls,
        settings: RuntimeSourceSettings,
        *,
        factory: RuntimeProviderFactory | None = None,
    ) -> MarketProviderRuntime:
        resolved_factory = factory or DefaultRuntimeProviderFactory()
        entries: list[tuple[ProviderId, MarketDataProvider]] = []
        providers: list[MarketDataProvider] = []
        for source in _configured_sources(settings):
            token, tdx_path = settings.credentials_for(source)
            if source is ProviderId.EASTMONEY:
                entries.append(
                    (source, _UnavailableProvider(source, ProviderUnavailable))
                )
                continue
            if source is ProviderId.TUSHARE and token is None:
                entries.append(
                    (source, _UnavailableProvider(source, ProviderPermissionDenied))
                )
                continue
            if source is ProviderId.TDX_LOCAL and tdx_path is None:
                entries.append(
                    (source, _UnavailableProvider(source, ProviderMissingCoverage))
                )
                continue
            try:
                provider = resolved_factory.create(
                    source,
                    token=token,
                    tdx_path=tdx_path,
                )
                entries.append((source, provider))
                providers.append(provider)
            except Exception as error:
                entries.append(
                    (
                        source,
                        _UnavailableProvider(source, _placeholder_error(source, error)),
                    )
                )
        persisted = settings.priorities
        priorities = SourcePriorities(
            bars=persisted.daily_bars,
            daily_bars=persisted.daily_bars,
            weekly_bars=persisted.weekly_bars,
            minute_bars=persisted.minute_bars,
            instruments=persisted.instruments,
            trading_calendar=persisted.trading_calendar,
        )
        try:
            router = SourceRouter(tuple(entries), priorities=priorities)
        except BaseException:
            _close_all(tuple(providers))
            raise
        return cls(router=router, providers=tuple(providers))

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            providers = self._providers
        _close_all(providers)

    def __enter__(self) -> MarketProviderRuntime:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "DefaultRuntimeProviderFactory",
    "MarketProviderRuntime",
    "RuntimeProviderFactory",
]
