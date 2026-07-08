from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import SecretStr

from stock_desk.api.settings import (
    PublicSourceSettings,
    SourcePriorities as PersistedPriorities,
    SourceSettingsServices,
    TushareSourceUpdateRequest,
)
from stock_desk.config import Settings
from stock_desk.market.runtime import MarketProviderRuntime, RuntimeProviderFactory
from stock_desk.market.update import MARKET_CATALOG_UPDATE_TASK_KIND
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.market.types import Period, ProviderId
from stock_desk.market.provenance import RoutedBarFailure
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.market.pools import PoolRepository
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.unit.market.routing_test_helpers import (
    BatchProvider,
    full_report,
    instrument_batch,
)
from tests.unit.market.providers.tdx_test_helpers import (
    make_vipdoc_root,
    raw_record,
    write_tdx_file,
)


class _OneIdleWait:
    def __init__(self) -> None:
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return bool(self.waits)

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return True


def test_production_worker_checks_new_tasks_within_interactive_latency() -> None:
    runtime = object.__new__(ProductionMarketWorker)
    runtime.scheduler = type("Scheduler", (), {"tick": lambda _self: None})()
    runtime.worker = type("Worker", (), {"run_once": lambda _self: None})()
    stop = _OneIdleWait()

    runtime.run_forever(stop)  # type: ignore[arg-type]

    assert stop.waits == [0.1]


class _Provider:
    def __init__(self, name: ProviderId, closed: list[ProviderId]) -> None:
        self.name = name
        self._closed = closed

    def close(self) -> None:
        self._closed.append(self.name)

    def capabilities(self) -> object:
        raise AssertionError("not called")

    def fetch_bars(self, _query: object) -> object:
        raise AssertionError("not called")

    def fetch_instruments(self) -> object:
        raise AssertionError("not called")

    def fetch_calendar(self, *_args: object) -> object:
        raise AssertionError("not called")


class _Factory(RuntimeProviderFactory):
    def __init__(self) -> None:
        self.calls: list[tuple[ProviderId, str | None, Path | None]] = []
        self.closed: list[ProviderId] = []

    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> _Provider:
        self.calls.append((source, token, tdx_path))
        if source is ProviderId.AKSHARE:
            raise RuntimeError("optional SDK unavailable")
        return _Provider(source, self.closed)


def test_runtime_snapshot_routes_each_period_and_closes_partial_builds(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'runtime.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        database_url=url,
        data_dir=tmp_path,
        master_key=SecretStr(key),
    )
    services = SourceSettingsServices(engine=engine, settings=settings)
    token = "runtime-token-must-stay-private"
    tdx_path = make_vipdoc_root(tmp_path).resolve()
    write_tdx_file(tdx_path, "600000.SH", raw_record())
    services.update_tushare(TushareSourceUpdateRequest(token=SecretStr(token)))
    services.save_public(
        PublicSourceSettings(
            priorities=PersistedPriorities(
                daily_bars=("tdx_local", "tushare"),
                weekly_bars=("akshare", "tushare"),
                minute_bars=("baostock", "tushare"),
                instruments=("akshare", "tushare"),
                trading_calendar=("baostock", "tushare"),
            ),
            tdx_path=str(tdx_path),
        )
    )
    factory = _Factory()
    try:
        snapshot = services.runtime_snapshot()
        assert token not in repr(snapshot)
        assert str(tdx_path) not in repr(snapshot)

        runtime = MarketProviderRuntime.build(snapshot, factory=factory)
        assert runtime.router.priorities().for_period(Period.DAY) == (
            ProviderId.TDX_LOCAL,
            ProviderId.TUSHARE,
        )
        assert runtime.router.priorities().for_period(Period.WEEK) == (
            ProviderId.AKSHARE,
            ProviderId.TUSHARE,
        )
        assert runtime.router.priorities().for_period(Period.MIN60) == (
            ProviderId.BAOSTOCK,
            ProviderId.TUSHARE,
        )
        assert ProviderId.EASTMONEY not in [call[0] for call in factory.calls]
        runtime.close()
        assert set(factory.closed) == {
            ProviderId.TUSHARE,
            ProviderId.BAOSTOCK,
            ProviderId.TDX_LOCAL,
        }
    finally:
        services.close()
        engine.dispose()


def test_runtime_snapshot_observes_settings_changes_without_restart(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'refresh.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    services = SourceSettingsServices(
        engine=engine,
        settings=Settings(database_url=url, data_dir=tmp_path),
        clock=lambda: datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    try:
        before = services.runtime_snapshot()
        services.save_public(
            PublicSourceSettings(
                priorities=PersistedPriorities(
                    daily_bars=("baostock",),
                )
            )
        )
        after = services.runtime_snapshot()
        assert before.priorities.daily_bars != after.priorities.daily_bars
        assert after.priorities.daily_bars == (ProviderId.BAOSTOCK,)
    finally:
        services.close()
        engine.dispose()


class _UnavailableFactory(RuntimeProviderFactory):
    def create(self, *_args: object, **_kwargs: object) -> _Provider:
        raise RuntimeError("optional provider SDK unavailable")


def test_runtime_keeps_typed_placeholders_for_missing_configuration(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'placeholders.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    services = SourceSettingsServices(
        engine=engine,
        settings=Settings(database_url=url, data_dir=tmp_path),
    )
    try:
        runtime = MarketProviderRuntime.build(
            services.runtime_snapshot(),
            factory=_UnavailableFactory(),
        )
        from tests.unit.market.routing_test_helpers import BAR_QUERY

        outcome = runtime.router.fetch_bars(BAR_QUERY)
        assert isinstance(outcome, RoutedBarFailure)
        assert [attempt.reason.value for attempt in outcome.audit.attempts] == [
            "permission_denied",
            "provider_unavailable",
            "provider_unavailable",
            "missing",
            "provider_unavailable",
        ]
        runtime.close()
    finally:
        services.close()
        engine.dispose()


class _CatalogFactory(RuntimeProviderFactory):
    def create(
        self,
        source: ProviderId,
        *,
        token: str | None,
        tdx_path: Path | None,
    ) -> BatchProvider:
        del token, tdx_path
        if source is not ProviderId.AKSHARE:
            raise RuntimeError("provider unavailable")
        return BatchProvider(
            ProviderId.AKSHARE,
            full_report(ProviderId.AKSHARE),
            instruments=instrument_batch(ProviderId.AKSHARE),
            calendar=AssertionError("unused"),
        )


def test_production_worker_registers_catalog_refresh_and_persists_full_a(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'production.db'}"
    settings = Settings(database_url=url, data_dir=tmp_path)
    runtime = ProductionMarketWorker.open(
        settings,
        worker_id="production-test",
        provider_factory=_CatalogFactory(),
        composition_factory=lambda: (_ for _ in ()).throw(
            RuntimeError("composition provider unavailable")
        ),
    )
    try:
        created = runtime.tasks.create(MARKET_CATALOG_UPDATE_TASK_KIND, {})
        completed = runtime.run_once()
        assert completed is not None
        assert completed.id == created.id
        assert completed.status == "succeeded"
        assert completed.result is not None
        assert completed.result["source"] == "akshare"
        assert completed.result["row_count"] == 2
    finally:
        runtime.close()

    engine = create_engine_for_url(url)
    try:
        assert InstrumentRepository(engine).get("600000.SH").instrument.name == (
            "name-600000.SH"
        )
        full_a = PoolRepository(engine).get_preset("all-a")
        assert [member.instrument.symbol for member in full_a.members] == [
            "000001.SZ",
            "600000.SH",
        ]
    finally:
        engine.dispose()
