from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from stock_desk.api.settings import (
    PublicSourceSettings,
    SourcePriorities as PersistedPriorities,
    SourceSettingsServices,
)
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.compositions import PresetCompositionResult
from stock_desk.market.pools import PoolCategory, PoolComposition
from stock_desk.market.providers.normalization import dataset_version, make_batch
from stock_desk.market.providers.base import ProviderOperation
from stock_desk.market.types import (
    Bar,
    BarQuery,
    BarResult,
    CapabilityReport,
    Exchange,
    Instrument,
    InstrumentKind,
    ListingStatus,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)
from stock_desk.market.worker_runtime import ProductionMarketWorker
from stock_desk.storage.database import create_engine_for_url, migrate


NOW = datetime(2026, 7, 6, 8, tzinfo=timezone.utc)
DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"


class FixtureProvider:
    name = ProviderId.BAOSTOCK

    def capabilities(self) -> CapabilityReport:
        from tests.unit.market.routing_test_helpers import full_report

        return full_report(self.name)

    def fetch_instruments(self) -> object:
        items = tuple(
            Instrument(
                symbol=f"{index:06d}.SZ",
                exchange=Exchange.SZ,
                name=f"测试证券{index}",
                instrument_kind=InstrumentKind.STOCK,
                listing_status=ListingStatus.LISTED,
                listed_on=date(2000, 1, 1),
            )
            for index in range(1, 801)
        )
        return make_batch(
            source=self.name,
            operation=ProviderOperation.INSTRUMENTS,
            request={},
            items=items,
            data_cutoff=NOW,
            observed_at=NOW,
        )

    def fetch_bars(self, query: BarQuery) -> BarResult:
        timestamp = (
            query.start + timedelta(days=3, hours=16)
            if query.period is Period.WEEK
            else query.start
        )
        bar = Bar(
            symbol=query.symbol,
            timestamp=timestamp,
            period=query.period,
            adjustment=query.adjustment,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=1_000,
            status=TradingStatus.NORMAL,
        )
        version = dataset_version(
            source=self.name,
            operation="bars",
            request={"query": query},
            data_cutoff=NOW,
            items=(bar,),
        )
        return BarResult(
            query=query,
            bars=(bar,),
            coverage_start=query.start,
            coverage_end=query.end,
            provenance=Provenance(
                source=self.name,
                fetched_at=NOW,
                data_cutoff=NOW,
                adjustment=query.adjustment,
                dataset_version=version,
            ),
        )

    def fetch_calendar(self, *_args: object) -> object:
        raise AssertionError("calendar is not part of this acceptance flow")


class FixtureProviderFactory:
    def create(self, source: ProviderId, **_kwargs: object) -> FixtureProvider:
        if source is not ProviderId.BAOSTOCK:
            raise RuntimeError("fixture source unavailable")
        return FixtureProvider()


class FixtureCompositionProvider:
    def fetch_presets(self, known_symbols: frozenset[str]) -> PresetCompositionResult:
        assert len(known_symbols) == 800
        values = (
            (
                "index-fixture",
                PoolCategory.INDEX,
                "测试指数",
                tuple(sorted(known_symbols)[:50]),
                DIGEST_A,
            ),
            (
                "industry-fixture",
                PoolCategory.INDUSTRY,
                "测试行业",
                tuple(sorted(known_symbols)[50:80]),
                DIGEST_B,
            ),
        )
        return PresetCompositionResult(
            compositions=tuple(
                PoolComposition(
                    preset_key=key,
                    category=category,
                    display_name=name,
                    symbols=symbols,
                    source=ProviderId.AKSHARE,
                    dataset_version=digest,
                    route_version=digest,
                    fetched_at=NOW,
                    data_cutoff=NOW,
                    complete=True,
                )
                for key, category, name, symbols, digest in values
            ),
            failures=(),
        )


def test_settings_route_worker_cache_api_and_schedule_flow_without_network(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'acceptance.db'}"
    settings = Settings(database_url=database_url, data_dir=tmp_path)
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    source_settings = SourceSettingsServices(engine=engine, settings=settings)
    source_settings.save_public(
        PublicSourceSettings(
            priorities=PersistedPriorities(
                daily_bars=("baostock",),
                weekly_bars=("baostock",),
                minute_bars=("baostock",),
                instruments=("baostock",),
                trading_calendar=("baostock",),
            )
        )
    )
    source_settings.close()
    engine.dispose()

    runtime = ProductionMarketWorker.open(
        settings,
        worker_id="acceptance-worker",
        provider_factory=FixtureProviderFactory(),
        composition_factory=FixtureCompositionProvider,
    )
    payload = {
        "symbols": ["000001.SZ"],
        "period": "1w",
        "adjustment": "qfq",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-02-01T00:00:00Z",
    }
    try:
        with TestClient(create_app(settings)) as client:
            catalog = client.post("/api/market/catalog/updates")
            assert catalog.status_code == 201
            assert runtime.run_once() is not None
            assert (
                client.get(
                    "/api/market/instruments", params={"q": "000001", "limit": 5}
                ).json()[0]["symbol"]
                == "000001.SZ"
            )
            categories = {
                item["category"]
                for item in client.get("/api/market/pools").json()["items"]
            }
            assert categories == {"all_a", "index", "industry"}

            update = client.post("/api/market/updates", json=payload)
            assert update.status_code == 201
            completed = runtime.run_once()
            assert completed is not None and completed.status == "succeeded"
            assert completed.result is not None
            assert str(completed.result["configuration_fingerprint"]).startswith(
                "sha256:"
            )
            items = client.get(
                f"/api/market/updates/{update.json()['id']}/items"
            ).json()
            assert items[0]["status"] == "succeeded"
            bars = client.get(
                "/api/market/bars",
                params={
                    "symbol": "000001.SZ",
                    "period": "1w",
                    "adjustment": "qfq",
                },
            )
            assert bars.status_code == 200
            assert bars.json()["routing_manifest"]["priority"] == ["baostock"]
            assert bars.json()["provenance"]["source"] == "baostock"

            schedule = client.put(
                "/api/market/schedules/daily",
                json={"enabled": True, "local_time": "00:00", "payload": payload},
            )
            assert schedule.status_code == 200
            assert schedule.json()["symbols_frozen"] is True
            scheduled = runtime.run_once()
            assert scheduled is not None
            assert scheduled.kind == "market.update"
    finally:
        runtime.close()
