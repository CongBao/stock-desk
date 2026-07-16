from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from pypinyin import Style, lazy_pinyin

from stock_desk.market.instruments import (
    InstrumentCatalog,
    InstrumentCorruption,
    InstrumentNotFound,
    InstrumentRepository,
    InstrumentRepositoryError,
)
from stock_desk.market.lake import MarketLake, MarketLakeError
from stock_desk.market.provenance import (
    RoutedBarFailure,
    RoutedBarSuccess,
    RoutedInstrumentFailure,
    RoutedInstrumentSuccess,
)
from stock_desk.market.providers.base import (
    MarketDataProvider,
    ProviderClientError,
    ProviderNoData,
)
from stock_desk.market.routing import SourcePriorities, SourceRouter
from stock_desk.market.runtime import DefaultRuntimeProviderFactory
from stock_desk.market.types import (
    Adjustment,
    BarQuery,
    CanonicalSymbol,
    Exchange,
    FailureReason,
    Instrument,
    InstrumentKind,
    Period,
    ProviderId,
)
from stock_desk.onboarding.models import (
    DEFAULT_SYMBOL,
    FREE_PROVIDER_IDS,
    OnboardingAction,
    OnboardingError,
    OnboardingInstrument,
    OnboardingSource,
    OnboardingState,
    OnboardingStatus,
    OnboardingStep,
    OnboardingSynchronization,
    SynchronizationStatus,
)
from stock_desk.onboarding.store import OnboardingStateStore


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SOURCE_LABELS = {
    ProviderId.AKSHARE: "AKShare",
    ProviderId.BAOSTOCK: "BaoStock",
}
_RECOVERY_ACTIONS: tuple[OnboardingAction, ...] = (
    "retry",
    "switch_provider",
    "advanced",
    "demo",
)
_ERROR_BY_REASON = {
    FailureReason.PERMISSION_DENIED: "provider_permission_denied",
    FailureReason.UNSUPPORTED: "provider_unsupported",
    FailureReason.MISSING: "provider_incomplete",
    FailureReason.NO_DATA: "provider_no_data",
    FailureReason.PROVIDER_UNAVAILABLE: "provider_unavailable",
    FailureReason.TRANSIENT_FAILURE: "provider_transient_failure",
    FailureReason.TIMEOUT: "provider_timeout",
    FailureReason.CORRUPT: "provider_corrupt_data",
    FailureReason.INVALID_RESPONSE: "provider_invalid_response",
    FailureReason.NO_PROVIDER: "provider_unavailable",
}


class OnboardingConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__()


class OnboardingProviderFactory(Protocol):
    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> MarketDataProvider: ...


class OnboardingMarketServices(Protocol):
    instruments: InstrumentRepository
    lake: MarketLake


class OnboardingService:
    """Four-step first-run orchestration with fail-closed completion."""

    def __init__(
        self,
        *,
        store: OnboardingStateStore,
        market: OnboardingMarketServices | Callable[[], OnboardingMarketServices],
        provider_factory: OnboardingProviderFactory | None = None,
        demo_initializer: Callable[[], OnboardingInstrument] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store = store
        self._market_provider = market if callable(market) else lambda: market
        self._resolved_market: OnboardingMarketServices | None = None
        self._provider_factory = provider_factory or DefaultRuntimeProviderFactory(
            clock=clock
        )
        self._demo_initializer = demo_initializer
        self._clock = clock
        self._lock = RLock()

    @classmethod
    def open(
        cls,
        *,
        data_dir: Path,
        market: OnboardingMarketServices | Callable[[], OnboardingMarketServices],
        provider_factory: OnboardingProviderFactory | None = None,
        demo_initializer: Callable[[], OnboardingInstrument] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> OnboardingService:
        state_path = data_dir.resolve() / "onboarding" / "state-v1.json"
        return cls(
            store=OnboardingStateStore(state_path, clock=clock),
            market=market,
            provider_factory=provider_factory,
            demo_initializer=demo_initializer,
            clock=clock,
        )

    def state(self) -> OnboardingState:
        return self._store.load()

    def sources(self) -> tuple[dict[str, object], ...]:
        state = self.state()
        selected = state.source
        return tuple(
            {
                "id": source,
                "label": _SOURCE_LABELS[source],
                "description": "A 股公开行情",
                "requires_token": False,
                "recommended": source is ProviderId.AKSHARE,
                "status": (
                    "unavailable"
                    if selected is not None
                    and selected.id is source
                    and state.error is not None
                    else "ready"
                    if selected is not None and selected.id is source
                    else "unknown"
                ),
                "data_cutoff": (
                    selected.data_cutoff
                    if selected is not None and selected.id is source
                    else None
                ),
            }
            for source in FREE_PROVIDER_IDS
        )

    def prepare(self, requested_source: ProviderId | None = None) -> OnboardingState:
        with self._lock:
            state = self._store.load()
            if state.status is OnboardingStatus.COMPLETED:
                return state
            candidates = (
                FREE_PROVIDER_IDS
                if requested_source is None or requested_source is ProviderId.AKSHARE
                else (requested_source,)
            )
            if any(source not in FREE_PROVIDER_IDS for source in candidates):
                raise OnboardingConflict("unsupported_onboarding_source")
            last_code = "provider_unavailable"
            for source in cast(Sequence[ProviderId], candidates):
                try:
                    routed, provider = self._fetch_catalog(source)
                except Exception as error:
                    last_code = self._exception_code(error)
                    continue
                try:
                    if isinstance(routed, RoutedInstrumentFailure):
                        last_code = self._failure_code(routed)
                        continue
                    catalog_source = routed.batch.provenance.source
                    if catalog_source is not source:
                        last_code = "provider_invalid_response"
                        continue
                    default = self._validated_default(routed)
                    manifest = self._market().instruments.ingest(routed)
                    if (
                        manifest.source is not source
                        or manifest.dataset_version
                        != routed.batch.provenance.dataset_version
                        or manifest.data_cutoff != routed.batch.provenance.data_cutoff
                    ):
                        last_code = "catalog_verification_failed"
                        continue
                    next_state = state.evolved(
                        now=self._clock(),
                        status=OnboardingStatus.IN_PROGRESS,
                        current_step=OnboardingStep.INSTRUMENT_SELECTION,
                        source=OnboardingSource(
                            id=source,
                            label=_SOURCE_LABELS[source],
                            catalog_manifest_record_id=manifest.manifest_record_id,
                            catalog_dataset_version=manifest.dataset_version,
                            data_cutoff=manifest.data_cutoff,
                        ),
                        instrument=self._instrument_state(default),
                        sync=None,
                        error=None,
                        demo_mode=False,
                    )
                    return self._store.save(next_state)
                except (InstrumentRepositoryError, ValueError):
                    last_code = "catalog_verification_failed"
                finally:
                    self._close_provider(provider)
            return self._save_failure(
                state,
                step=OnboardingStep.DATA_PREPARATION,
                code=last_code,
            )

    def begin_preparation(self) -> OnboardingState:
        state = self.enter_data_preparation()
        if state.status is OnboardingStatus.COMPLETED:
            return state
        return self.prepare()

    def enter_data_preparation(self) -> OnboardingState:
        with self._lock:
            state = self._store.load()
            if state.status is OnboardingStatus.COMPLETED:
                return state
            return self._store.save(
                state.evolved(
                    now=self._clock(),
                    status=OnboardingStatus.IN_PROGRESS,
                    current_step=OnboardingStep.DATA_PREPARATION,
                    error=None,
                    demo_mode=False,
                )
            )

    def search(self, query: str, *, limit: int) -> tuple[OnboardingInstrument, ...]:
        with self._lock:
            if not 1 <= limit <= 100:
                raise OnboardingConflict("invalid_request")
            normalized = query.strip().casefold()
            if not normalized or len(normalized) > 64:
                raise OnboardingConflict("invalid_request")
            state = self.state()
            catalog = self._catalog_for(state)
            ranked: list[tuple[int, str, Instrument]] = []
            for item in catalog.instruments:
                code = item.symbol.casefold()
                name = item.name.casefold()
                full_pinyin = "".join(
                    lazy_pinyin(item.name, style=Style.NORMAL)
                ).casefold()
                initials = "".join(
                    lazy_pinyin(item.name, style=Style.FIRST_LETTER)
                ).casefold()
                fields = (code, code[:6], name, full_pinyin, initials)
                if normalized not in fields and not any(
                    field.startswith(normalized) for field in fields
                ):
                    continue
                rank = 0 if normalized in fields else 1
                ranked.append((rank, item.symbol, item))
            return tuple(
                self._instrument_state(item)
                for _rank, _symbol, item in sorted(ranked)[:limit]
            )

    def select(self, symbol: CanonicalSymbol) -> OnboardingState:
        with self._lock:
            state = self._store.load()
            if state.source is None or state.demo_mode:
                raise OnboardingConflict("catalog_not_ready")
            catalog = self._catalog_for(state)
            selected = next(
                (item for item in catalog.instruments if item.symbol == symbol), None
            )
            if selected is None:
                raise OnboardingConflict("instrument_not_found")
            return self._store.save(
                state.evolved(
                    now=self._clock(),
                    status=OnboardingStatus.IN_PROGRESS,
                    current_step=OnboardingStep.SYNCHRONIZATION,
                    instrument=self._instrument_state(selected),
                    sync=OnboardingSynchronization(status=SynchronizationStatus.IDLE),
                    error=None,
                )
            )

    def synchronize(
        self, *, source_id: ProviderId, symbol: CanonicalSymbol
    ) -> OnboardingState:
        with self._lock:
            state = self._store.load()
            if (
                state.source is None
                or state.source.id is not source_id
                or state.instrument.symbol != symbol
                or state.demo_mode
            ):
                raise OnboardingConflict("onboarding_selection_changed")
            catalog = self._catalog_for(state)
            instrument = next(
                (item for item in catalog.instruments if item.symbol == symbol), None
            )
            if instrument is None:
                raise OnboardingConflict("instrument_not_found")
            try:
                query = self._daily_query(instrument)
                routed, provider = self._fetch_bars(source_id, query)
            except Exception as error:
                return self._recover_stock_with_baostock_or_save_failure(
                    state,
                    instrument=instrument,
                    source_id=source_id,
                    symbol=symbol,
                    code=self._exception_code(error),
                )
            try:
                if isinstance(routed, RoutedBarFailure):
                    return self._recover_stock_with_baostock_or_save_failure(
                        state,
                        instrument=instrument,
                        source_id=source_id,
                        symbol=symbol,
                        code=self._failure_code(routed),
                    )
                self._validate_bar_result(routed, source_id, query)
                stored = self._market().lake.write(routed)
                verified = self._market().lake.read(stored.manifest_record_id)
                self._validate_bar_result(verified, source_id, query)
                if (
                    stored.dataset_version != verified.result.provenance.dataset_version
                    or stored.fetched_at != verified.result.provenance.fetched_at
                ):
                    raise ValueError("stored evidence mismatch")
                sync = OnboardingSynchronization(
                    status=SynchronizationStatus.VERIFIED,
                    provider_id=source_id,
                    manifest_record_id=stored.manifest_record_id,
                    dataset_version=stored.dataset_version,
                    data_cutoff=verified.result.provenance.data_cutoff,
                    row_count=len(verified.result.bars),
                )
                return self._store.save(
                    state.evolved(
                        now=self._clock(),
                        status=OnboardingStatus.IN_PROGRESS,
                        current_step=OnboardingStep.SYNCHRONIZATION,
                        sync=sync,
                        error=None,
                    )
                )
            except (ValueError, InstrumentCorruption, MarketLakeError):
                return self._recover_stock_with_baostock_or_save_failure(
                    state,
                    instrument=instrument,
                    source_id=source_id,
                    symbol=symbol,
                    code="bar_verification_failed",
                )
            finally:
                self._close_provider(provider)

    def complete(self, symbol: CanonicalSymbol) -> OnboardingState:
        with self._lock:
            state = self._store.load()
            if state.status is OnboardingStatus.COMPLETED:
                if state.instrument.symbol != symbol:
                    raise OnboardingConflict("onboarding_selection_changed")
                return state
            if (
                state.demo_mode
                or state.source is None
                or state.instrument.symbol != symbol
                or state.sync is None
                or state.sync.status is not SynchronizationStatus.VERIFIED
                or state.sync.provider_id is not state.source.id
                or state.sync.manifest_record_id is None
            ):
                raise OnboardingConflict("synchronization_not_verified")
            catalog = self._catalog_for(state)
            instrument = next(
                (item for item in catalog.instruments if item.symbol == symbol), None
            )
            if (
                instrument is None
                or self._instrument_state(instrument) != state.instrument
            ):
                raise OnboardingConflict("catalog_verification_failed")
            try:
                routed = self._market().lake.read(state.sync.manifest_record_id)
            except MarketLakeError as error:
                raise OnboardingConflict("bar_verification_failed") from error
            if (
                routed.result.query.symbol != symbol
                or routed.result.provenance.source is not state.source.id
                or routed.result.provenance.dataset_version
                != state.sync.dataset_version
                or routed.result.provenance.data_cutoff != state.sync.data_cutoff
                or len(routed.result.bars) != state.sync.row_count
            ):
                raise OnboardingConflict("bar_verification_failed")
            return self._store.save(
                state.evolved(
                    now=self._clock(),
                    status=OnboardingStatus.COMPLETED,
                    current_step=OnboardingStep.COMPLETED,
                    error=None,
                )
            )

    def retry(self) -> OnboardingState:
        state = self.state()
        if state.current_step is OnboardingStep.DATA_PREPARATION:
            return self.prepare(state.source.id if state.source is not None else None)
        if state.current_step is OnboardingStep.SYNCHRONIZATION:
            if state.source is None:
                raise OnboardingConflict("catalog_not_ready")
            return self.synchronize(
                source_id=state.source.id, symbol=state.instrument.symbol
            )
        return state

    def switch_provider(self) -> OnboardingState:
        state = self.state()
        current = state.source.id if state.source is not None else ProviderId.AKSHARE
        alternative = (
            ProviderId.BAOSTOCK if current is ProviderId.AKSHARE else ProviderId.AKSHARE
        )
        return self.prepare(alternative)

    def advanced(self) -> OnboardingState:
        with self._lock:
            state = self._store.load()
            return self._save_failure(
                state,
                step=OnboardingStep.DATA_PREPARATION,
                code="advanced_configuration_required",
            )

    def demo(self) -> OnboardingState:
        with self._lock:
            state = self._store.load()
            if state.status is OnboardingStatus.COMPLETED:
                return state
            instrument = (
                self._demo_initializer()
                if self._demo_initializer is not None
                else state.instrument
            )
            return self._store.save(
                state.evolved(
                    now=self._clock(),
                    status=OnboardingStatus.IN_PROGRESS,
                    current_step=OnboardingStep.INSTRUMENT_SELECTION,
                    source=None,
                    instrument=instrument,
                    sync=None,
                    demo_mode=True,
                    error=OnboardingError(
                        code="demo_read_only",
                        actions=("retry", "switch_provider", "advanced"),
                    ),
                )
            )

    def exit_demo(self) -> OnboardingState:
        """Leave the persisted read-only demo without retaining a dead source."""
        with self._lock:
            state = self._store.load()
            if state.status is OnboardingStatus.COMPLETED:
                return state
            return self._store.save(
                state.evolved(
                    now=self._clock(),
                    status=OnboardingStatus.IN_PROGRESS,
                    current_step=OnboardingStep.DATA_PREPARATION,
                    source=None,
                    instrument=OnboardingInstrument(
                        symbol=DEFAULT_SYMBOL,
                        name="上证指数",
                        exchange=Exchange.SH,
                        instrument_kind=InstrumentKind.INDEX,
                    ),
                    sync=None,
                    demo_mode=False,
                    error=None,
                )
            )

    def _fetch_catalog(
        self, source: ProviderId
    ) -> tuple[RoutedInstrumentSuccess | RoutedInstrumentFailure, MarketDataProvider]:
        provider = self._provider_factory.create(source, token=None, tdx_path=None)
        try:
            router = SourceRouter(
                ((source, provider),),
                priorities=SourcePriorities(instruments=(source,)),
            )
            return router.fetch_instruments(), provider
        except BaseException:
            self._close_provider(provider)
            raise

    def _fetch_bars(
        self, source: ProviderId, query: BarQuery
    ) -> tuple[RoutedBarSuccess | RoutedBarFailure, MarketDataProvider]:
        provider = self._provider_factory.create(source, token=None, tdx_path=None)
        try:
            router = SourceRouter(
                ((source, provider),),
                priorities=SourcePriorities(bars=(source,), daily_bars=(source,)),
            )
            return router.fetch_bars(query), provider
        except BaseException:
            self._close_provider(provider)
            raise

    def _catalog_for(self, state: OnboardingState) -> InstrumentCatalog:
        if state.source is None:
            raise OnboardingConflict("catalog_not_ready")
        try:
            catalog = self._market().instruments.pinned_catalog(
                state.source.catalog_manifest_record_id
            )
        except (InstrumentCorruption, InstrumentNotFound) as error:
            raise OnboardingConflict("catalog_verification_failed") from error
        if (
            catalog.manifest.source is not state.source.id
            or catalog.manifest.dataset_version != state.source.catalog_dataset_version
            or catalog.manifest.data_cutoff != state.source.data_cutoff
        ):
            raise OnboardingConflict("catalog_verification_failed")
        return catalog

    @staticmethod
    def _validated_default(routed: RoutedInstrumentSuccess) -> Instrument:
        matching = tuple(
            item for item in routed.batch.items if item.symbol == DEFAULT_SYMBOL
        )
        stock = tuple(item for item in routed.batch.items if item.symbol == "000001.SZ")
        if (
            len(matching) != 1
            or matching[0].instrument_kind.value != "index"
            or matching[0].exchange.value != "SH"
            or any(item.instrument_kind.value == "index" for item in stock)
        ):
            raise ValueError("default index identity is invalid")
        return matching[0]

    @staticmethod
    def _instrument_state(instrument: Instrument) -> OnboardingInstrument:
        return OnboardingInstrument(
            symbol=instrument.symbol,
            name=(
                "上证指数" if instrument.symbol == DEFAULT_SYMBOL else instrument.name
            ),
            exchange=instrument.exchange,
            instrument_kind=instrument.instrument_kind,
        )

    def _daily_query(self, instrument: Instrument) -> BarQuery:
        now_local = self._clock().astimezone(_SHANGHAI)
        window_start = now_local.date() - timedelta(days=365)
        end_day = now_local.date() + timedelta(days=1)
        start_day = max(window_start, instrument.listed_on or window_start)
        if start_day >= end_day:
            raise ProviderNoData()
        start_local = datetime.combine(
            start_day,
            datetime.min.time(),
            tzinfo=_SHANGHAI,
        )
        end_local = datetime.combine(
            end_day,
            datetime.min.time(),
            tzinfo=_SHANGHAI,
        )
        return BarQuery(
            symbol=instrument.symbol,
            instrument_kind=instrument.instrument_kind,
            period=Period.DAY,
            adjustment=Adjustment.NONE,
            start=start_local.astimezone(timezone.utc),
            end=end_local.astimezone(timezone.utc),
        )

    @staticmethod
    def _validate_bar_result(
        routed: RoutedBarSuccess, source: ProviderId, query: BarQuery
    ) -> None:
        result = routed.result
        if (
            result.query != query
            or result.provenance.source is not source
            or routed.manifest.selected_source is not source
            or routed.manifest.upstream_dataset_version
            != result.provenance.dataset_version
            or routed.manifest.upstream_data_cutoff != result.provenance.data_cutoff
            or result.coverage_start != query.start
            or result.coverage_end != query.end
            or not result.bars
        ):
            raise ValueError("bar evidence is incomplete")
        timestamps = tuple(bar.timestamp for bar in result.bars)
        if timestamps != tuple(sorted(timestamps)) or len(timestamps) != len(
            frozenset(timestamps)
        ):
            raise ValueError("bars are not strictly sorted")
        if any(
            bar.symbol != query.symbol
            or bar.period is not Period.DAY
            or bar.adjustment is not Adjustment.NONE
            for bar in result.bars
        ):
            raise ValueError("bar identity mismatch")

    @staticmethod
    def _failure_code(
        routed: RoutedInstrumentFailure | RoutedBarFailure,
    ) -> str:
        attempts = routed.audit.attempts
        reason = attempts[-1].reason if attempts else FailureReason.NO_PROVIDER
        return _ERROR_BY_REASON.get(reason, "provider_invalid_response")

    @staticmethod
    def _exception_code(error: Exception) -> str:
        if isinstance(error, ProviderClientError):
            reason = getattr(error, "reason", FailureReason.INVALID_RESPONSE)
            if isinstance(reason, FailureReason):
                return _ERROR_BY_REASON.get(reason, "provider_invalid_response")
        if isinstance(error, TimeoutError):
            return "provider_timeout"
        return "provider_unavailable"

    def _save_failure(
        self,
        state: OnboardingState,
        *,
        step: OnboardingStep,
        code: str,
        failed_sync: bool = False,
    ) -> OnboardingState:
        return self._store.save(
            state.evolved(
                now=self._clock(),
                status=OnboardingStatus.IN_PROGRESS,
                current_step=step,
                sync=(
                    OnboardingSynchronization(status=SynchronizationStatus.FAILED)
                    if failed_sync
                    else state.sync
                ),
                error=OnboardingError(code=code, actions=_RECOVERY_ACTIONS),
            )
        )

    def _recover_stock_with_baostock_or_save_failure(
        self,
        state: OnboardingState,
        *,
        instrument: Instrument,
        source_id: ProviderId,
        symbol: CanonicalSymbol,
        code: str,
    ) -> OnboardingState:
        supports_baostock_fallback = (
            instrument.instrument_kind is InstrumentKind.STOCK
            or (
                instrument.instrument_kind is InstrumentKind.INDEX
                and instrument.symbol == DEFAULT_SYMBOL
            )
        )
        if source_id is not ProviderId.AKSHARE or not supports_baostock_fallback:
            return self._save_failure(
                state,
                step=OnboardingStep.SYNCHRONIZATION,
                code=code,
                failed_sync=True,
            )

        prepared = self.prepare(ProviderId.BAOSTOCK)
        if (
            prepared.source is None
            or prepared.source.id is not ProviderId.BAOSTOCK
            or prepared.error is not None
        ):
            return prepared
        try:
            self.select(symbol)
        except OnboardingConflict:
            return self._save_failure(
                state,
                step=OnboardingStep.SYNCHRONIZATION,
                code=code,
                failed_sync=True,
            )
        return self.synchronize(source_id=ProviderId.BAOSTOCK, symbol=symbol)

    @staticmethod
    def _close_provider(provider: MarketDataProvider) -> None:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _market(self) -> OnboardingMarketServices:
        if self._resolved_market is None:
            self._resolved_market = self._market_provider()
        return self._resolved_market
