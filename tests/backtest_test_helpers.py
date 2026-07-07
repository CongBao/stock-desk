from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from stock_desk.backtest.pool_runner import PoolBacktestRunner
from stock_desk.backtest.repository import (
    BacktestReportSnapshot,
    BacktestRepository,
    BacktestRunSnapshot,
)
from stock_desk.backtest.service import (
    BacktestIntent,
    BacktestService,
    SubmittedBacktest,
)
from stock_desk.formula.models import FormulaVersion
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusQuery,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.execution_status_lake import ExecutionStatusLake
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.provenance import (
    BarRoutingRequest,
    ExecutionStatusRoutingRequest,
    RoutedBarSuccess,
    RoutedExecutionStatusSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    Exchange,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.models import TaskClaim
from stock_desk.tasks.repository import TaskRepository
from tests.integration.market.task6_test_helpers import instrument, routed_instruments


SHANGHAI = ZoneInfo("Asia/Shanghai")
WAVE_FORMULA = "BUY:CROSS(C,MA(C,3));SELL:CROSS(MA(C,3),C);"
OPEN_ONLY_FORMULA = "BUY:C>0;SELL:C<0;"


def local_time(day: date, clock: time = time()) -> datetime:
    return datetime.combine(day, clock, tzinfo=SHANGHAI)


def weekday_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        day
        for offset in range((end - start).days)
        if (day := start + timedelta(days=offset)).weekday() < 5
    )


def weekly_timestamps(start: date, count: int) -> tuple[datetime, ...]:
    first_monday = start + timedelta(days=(-start.weekday()) % 7)
    return tuple(
        local_time(first_monday + timedelta(weeks=index)) for index in range(count)
    )


def intraday_timestamps(
    start: date,
    *,
    trading_days: int,
) -> tuple[datetime, ...]:
    days: list[date] = []
    current = start
    while len(days) < trading_days:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    clocks = (time(9, 30), time(10, 30), time(13), time(14))
    return tuple(local_time(day, clock) for day in days for clock in clocks)


def _timestamps(values: Sequence[date | datetime]) -> tuple[datetime, ...]:
    return tuple(
        local_time(value)
        if isinstance(value, date) and not isinstance(value, datetime)
        else value
        for value in values
    )


def _wave(index: int, *, period: int, phase_offset: int) -> Decimal:
    phase = (index + phase_offset) % period
    half = period // 2
    height = phase if phase <= half else period - phase
    return Decimal("9") + Decimal(height) / Decimal("5")


def routed_wave_bars(
    symbol: str,
    period: Period,
    values: Sequence[date | datetime],
    *,
    adjustment: Adjustment = Adjustment.NONE,
    phase_offset: int = 0,
    wave_period: int = 20,
    coverage_end: datetime | None = None,
) -> RoutedBarSuccess:
    timestamps = _timestamps(values)
    if not timestamps:
        raise ValueError("wave bars require timestamps")
    if wave_period < 4 or wave_period % 2:
        raise ValueError("wave period must be an even integer of at least four")
    closes = tuple(
        _wave(index, period=wave_period, phase_offset=phase_offset)
        for index in range(len(timestamps))
    )
    return routed_bars_from_closes(
        symbol,
        period,
        timestamps,
        closes,
        adjustment=adjustment,
        coverage_end=coverage_end,
    )


def routed_bars_from_closes(
    symbol: str,
    period: Period,
    values: Sequence[date | datetime],
    closes: Sequence[Decimal],
    *,
    adjustment: Adjustment = Adjustment.NONE,
    coverage_end: datetime | None = None,
) -> RoutedBarSuccess:
    timestamps = _timestamps(values)
    canonical_closes = tuple(closes)
    if not timestamps or len(timestamps) != len(canonical_closes):
        raise ValueError("bar timestamps and closes must be nonempty and aligned")
    interval = (
        timedelta(days=7)
        if period is Period.WEEK
        else timedelta(hours=1)
        if period is Period.MIN60
        else timedelta(days=1)
    )
    query = BarQuery(
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        start=timestamps[0],
        end=coverage_end or timestamps[-1] + interval,
    )
    bars = tuple(
        Bar(
            symbol=symbol,
            timestamp=timestamp,
            period=period,
            adjustment=adjustment,
            open=canonical_closes[index - 1] if index else canonical_closes[index],
            high=max(
                canonical_closes[index - 1] if index else canonical_closes[index],
                canonical_closes[index],
            )
            + Decimal("0.2"),
            low=min(
                canonical_closes[index - 1] if index else canonical_closes[index],
                canonical_closes[index],
            )
            - Decimal("0.2"),
            close=canonical_closes[index],
            volume=10_000 + index,
            status=TradingStatus.NORMAL,
        )
        for index, timestamp in enumerate(timestamps)
    )
    data_cutoff = timestamps[-1] + min(interval, timedelta(hours=1))
    fetched_at = data_cutoff + timedelta(hours=1)
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": query},
        data_cutoff=data_cutoff,
        items=bars,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=ProviderId.TUSHARE,
            fetched_at=fetched_at,
            data_cutoff=data_cutoff,
            adjustment=adjustment,
            dataset_version=version,
        ),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=version,
        upstream_fetched_at=fetched_at,
        upstream_data_cutoff=data_cutoff,
        upstream_adjustment=adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


def routed_status(
    symbol: str,
    period: Period,
    execution_bars: RoutedBarSuccess,
    *,
    suspended_days: frozenset[date] = frozenset(),
    raw_open_overrides: Mapping[datetime, Decimal] | None = None,
) -> RoutedExecutionStatusSuccess:
    bars = execution_bars.result.bars
    first_day = bars[0].timestamp.astimezone(SHANGHAI).date()
    last_day = bars[-1].timestamp.astimezone(SHANGHAI).date()
    local_query_end = execution_bars.result.query.end.astimezone(SHANGHAI)
    end = local_query_end.date()
    if local_query_end.timetz().replace(tzinfo=None) != time():
        end += timedelta(days=1)
    exchange = Exchange(symbol.rsplit(".", maxsplit=1)[1])
    query = ExecutionStatusQuery(
        symbol=symbol,
        exchange=exchange,
        start=first_day,
        end=end,
        period=period,
    )
    trading_days = {bar.timestamp.astimezone(SHANGHAI).date() for bar in bars}
    days = tuple(
        ExecutionStatusDay(
            day=day,
            exchange=exchange,
            is_exchange_open=day in trading_days,
            suspension_state=(
                SuspensionState.SUSPENDED
                if day in suspended_days
                else SuspensionState.NORMAL
                if day in trading_days
                else SuspensionState.NOT_APPLICABLE
            ),
            raw_upper_limit=Decimal("12") if day in trading_days else None,
            raw_lower_limit=Decimal("8") if day in trading_days else None,
        )
        for day in _natural_days(first_day, end)
    )
    overrides = raw_open_overrides or {}
    if period is Period.MIN60:
        raw_opens = tuple(
            RawExecutionOpen(
                timestamp=bar.timestamp,
                trading_day=bar.timestamp.astimezone(SHANGHAI).date(),
                raw_open=overrides.get(bar.timestamp, bar.open),
            )
            for bar in bars
        )
    else:
        first_by_day: dict[date, Bar] = {}
        for bar in bars:
            first_by_day.setdefault(bar.timestamp.astimezone(SHANGHAI).date(), bar)
        raw_opens = tuple(
            RawExecutionOpen(
                timestamp=local_time(day, time(9, 30)),
                trading_day=day,
                raw_open=overrides.get(local_time(day, time(9, 30)), bar.open),
            )
            for day, bar in sorted(first_by_day.items())
        )
    data_cutoff = local_time(last_day, time(16))
    fetched_at = data_cutoff + timedelta(hours=1)
    result = materialize_execution_status(
        query=query,
        days=days,
        raw_opens=raw_opens,
        source=ProviderId.TUSHARE,
        fetched_at=fetched_at,
        data_cutoff=data_cutoff,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=result.dataset_version,
        upstream_fetched_at=result.fetched_at,
        upstream_data_cutoff=result.data_cutoff,
        upstream_adjustment=None,
    )
    return RoutedExecutionStatusSuccess(result=result, manifest=manifest)


def _natural_days(start: date, end: date) -> Iterator[date]:
    for offset in range((end - start).days):
        yield start + timedelta(days=offset)


@dataclass(frozen=True, slots=True)
class CompletedBacktest:
    submitted: SubmittedBacktest
    run: BacktestRunSnapshot
    report: BacktestReportSnapshot
    service: BacktestService
    formulas: FormulaService


@dataclass(slots=True)
class BacktestHarness(AbstractContextManager["BacktestHarness"]):
    engine: Engine
    market: MarketLake
    statuses: ExecutionStatusLake
    instruments: InstrumentRepository
    pools: PoolRepository
    tasks: TaskRepository
    formula_repository: FormulaRepository
    repository: BacktestRepository

    @classmethod
    def create(cls, tmp_path: Path) -> "BacktestHarness":
        url = f"sqlite:///{tmp_path / 'backtest-harness.db'}"
        migrate(url)
        engine = create_engine_for_url(url)
        return cls(
            engine=engine,
            market=MarketLake(engine=engine, root=(tmp_path / "market").resolve()),
            statuses=ExecutionStatusLake(engine),
            instruments=InstrumentRepository(engine),
            pools=PoolRepository(engine),
            tasks=TaskRepository(engine),
            formula_repository=FormulaRepository(engine),
            repository=BacktestRepository(engine),
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.engine.dispose()

    def seed_instruments(self, *symbols: str) -> None:
        names = {"600000.SH": "浦发银行", "000001.SZ": "平安银行"}
        self.instruments.ingest(
            routed_instruments(
                tuple(
                    instrument(symbol, names.get(symbol, symbol)) for symbol in symbols
                )
            )
        )

    def seed_symbol(
        self,
        symbol: str,
        period: Period,
        values: Sequence[date | datetime],
        *,
        phase_offset: int = 0,
        wave_period: int = 20,
    ) -> None:
        signal = routed_wave_bars(
            symbol,
            period,
            values,
            phase_offset=phase_offset,
            wave_period=wave_period,
        )
        self.market.write(signal)
        if period is Period.WEEK:
            signal_times = _timestamps(values)
            companion_days = weekday_range(
                signal_times[0].astimezone(SHANGHAI).date(),
                signal_times[-1].astimezone(SHANGHAI).date() + timedelta(days=7),
            )
            execution = routed_wave_bars(
                symbol,
                Period.DAY,
                companion_days,
                phase_offset=phase_offset,
                wave_period=wave_period,
                coverage_end=signal_times[-1] + timedelta(days=7),
            )
            self.market.write(execution)
        else:
            execution = signal
        self.statuses.write(routed_status(symbol, period, execution))

    def create_formula(self, name: str, source: str) -> FormulaVersion:
        return self.formula_repository.create(
            name,
            "trading",
            source,
            {},
            placement="subchart",
        )

    def run_single(
        self,
        formula_version_id: str,
        *,
        symbol: str,
        period: Period,
        scoring_start: datetime,
        scoring_end: datetime,
        quantity_shares: int = 1_000,
        commission_bps: Decimal = Decimal("2.5"),
        minimum_commission: Decimal = Decimal("5"),
        sell_tax_bps: Decimal = Decimal("5"),
        slippage_bps: Decimal = Decimal("3"),
    ) -> CompletedBacktest:
        return self._run(
            BacktestIntent(
                scope_kind="single",
                symbol=symbol,
                scope_id=None,
                scope_revision_or_snapshot_id=None,
                formula_version_id=formula_version_id,
                formula_parameters={},
                period=period,
                adjustment=Adjustment.NONE,
                scoring_start=scoring_start,
                scoring_end=scoring_end,
                quantity_shares=quantity_shares,
                commission_bps=commission_bps,
                minimum_commission=minimum_commission,
                sell_tax_bps=sell_tax_bps,
                slippage_bps=slippage_bps,
            )
        )

    def run_pool(
        self,
        formula_version_id: str,
        *,
        symbols: tuple[str, ...],
        period: Period,
        scoring_start: datetime,
        scoring_end: datetime,
    ) -> CompletedBacktest:
        pool = self.pools.publish_full_a()
        if {member.instrument.symbol for member in pool.members} != set(symbols):
            raise ValueError("pool symbols do not match the seeded instrument catalog")
        return self._run(
            BacktestIntent(
                scope_kind="preset",
                symbol=None,
                scope_id=pool.pool_id,
                scope_revision_or_snapshot_id=pool.snapshot_id,
                formula_version_id=formula_version_id,
                formula_parameters={},
                period=period,
                adjustment=Adjustment.NONE,
                scoring_start=scoring_start,
                scoring_end=scoring_end,
                quantity_shares=1_000,
                commission_bps=Decimal("2.5"),
                minimum_commission=Decimal("5"),
                sell_tax_bps=Decimal("5"),
                slippage_bps=Decimal("3"),
            )
        )

    def _run(self, intent: BacktestIntent) -> CompletedBacktest:
        formulas = FormulaService(repository=self.formula_repository, lake=self.market)
        service = BacktestService(
            engine=self.engine,
            tasks=self.tasks,
            repository=self.repository,
            market_lake=self.market,
            status_lake=self.statuses,
            instruments=self.instruments,
            pools=self.pools,
            formulas=formulas,
        )
        submitted = service.submit(intent)
        claim = self.tasks.claim_next(
            f"backtest-test-{submitted.run_id}",
            lease_duration=timedelta(seconds=30),
        )
        if not isinstance(claim, TaskClaim) or claim.snapshot.id != submitted.task_id:
            raise AssertionError("submitted backtest task was not claimed")
        runner = PoolBacktestRunner(
            engine=self.engine,
            tasks=self.tasks,
            repository=self.repository,
            market_lake=self.market,
            status_lake=self.statuses,
            formulas=formulas,
            heartbeat_interval_seconds=1,
            heartbeat_lease_duration=timedelta(seconds=30),
        )
        runner(claim)
        run = self.repository.get_run(submitted.run_id)
        return CompletedBacktest(
            submitted=submitted,
            run=run,
            report=service.report(submitted.run_id),
            service=service,
            formulas=formulas,
        )
