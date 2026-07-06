"""Seed an isolated local market cache, then run the real E2E service trio."""

# ruff: noqa: E402

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import importlib
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stock_desk.api.settings import (
    PublicSourceSettings,
    SourcePriorities,
    SourceSettingsServices,
)
from stock_desk.config import Settings
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolCategory, PoolComposition, PoolRepository
from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)
from stock_desk.market.instruments import InstrumentRepository
from stock_desk.storage.database import create_engine_for_url, migrate
from scripts.dev import supervise


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _local(day: date, clock: time = time()) -> datetime:
    return datetime.combine(day, clock, tzinfo=SHANGHAI)


def _routed(period: Period, adjustment: Adjustment) -> RoutedBarSuccess:
    start_day = date(2024, 1, 1)
    if period is Period.DAY:
        timestamps = tuple(
            _local(start_day + timedelta(days=index)) for index in range(30)
        )
    elif period is Period.WEEK:
        timestamps = tuple(
            _local(start_day + timedelta(days=7 * index)) for index in range(30)
        )
    else:
        timestamps = tuple(
            _local(start_day + timedelta(days=index), time(9, 30))
            for index in range(30)
        )
    query = BarQuery(
        symbol="600000.SH",
        period=period,
        adjustment=adjustment,
        start=timestamps[0],
        end=timestamps[-1]
        + (timedelta(hours=1) if period is Period.MIN60 else timedelta(days=1)),
    )
    bars = tuple(
        Bar(
            symbol=query.symbol,
            timestamp=timestamp,
            period=period,
            adjustment=adjustment,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5") + Decimal(index) / Decimal("100"),
            volume=1_000 + index,
            status=TradingStatus.NORMAL,
        )
        for index, timestamp in enumerate(timestamps)
    )
    observed = _local(date(2024, 12, 31), time(16))
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": query},
        data_cutoff=observed,
        items=bars,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=ProviderId.TUSHARE,
            fetched_at=observed,
            data_cutoff=observed,
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
        upstream_fetched_at=observed,
        upstream_data_cutoff=observed,
        upstream_adjustment=adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


def _seed(data_dir: Path) -> None:
    database_url = f"sqlite:///{data_dir / 'stock-desk.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    settings = Settings(database_url=database_url, data_dir=data_dir)
    tdx_root = data_dir / "tdx"
    target = tdx_root / "vipdoc" / "sh" / "lday" / "sh600000.day"
    target.parent.mkdir(parents=True, exist_ok=True)
    fixture = ROOT / "tests" / "fixtures" / "tdx" / "sh600000.day.hex"
    target.write_bytes(bytes.fromhex(fixture.read_text(encoding="ascii").strip()))
    try:
        helpers = importlib.import_module("tests.integration.market.task6_test_helpers")
        instruments = InstrumentRepository(engine)
        instruments.ingest(
            helpers.routed_instruments(
                (
                    helpers.instrument("000001.SZ", "平安银行"),
                    helpers.instrument("600000.SH", "浦发银行"),
                    helpers.instrument("600036.SH", "招商银行"),
                )
            )
        )
        pools = PoolRepository(engine)
        pools.publish_full_a()
        catalog = instruments.current_manifest()
        for key, category, name, symbols, digest_character in (
            (
                "index-e2e",
                PoolCategory.INDEX,
                "E2E 指数",
                ("000001.SZ", "600000.SH"),
                "a",
            ),
            (
                "industry-e2e",
                PoolCategory.INDUSTRY,
                "E2E 行业",
                ("600000.SH", "600036.SH"),
                "b",
            ),
        ):
            digest = f"sha256:{digest_character * 64}"
            pools.publish_preset(
                PoolComposition(
                    preset_key=key,
                    category=category,
                    display_name=name,
                    symbols=symbols,
                    source=ProviderId.AKSHARE,
                    dataset_version=digest,
                    route_version=digest,
                    fetched_at=catalog.fetched_at,
                    data_cutoff=catalog.data_cutoff,
                    complete=True,
                )
            )
        lake = MarketLake(engine=engine, root=data_dir / "market")
        for period in Period:
            for adjustment in Adjustment:
                lake.write(_routed(period, adjustment))
        source_settings = SourceSettingsServices(engine=engine, settings=settings)
        try:
            source_settings.save_public(
                PublicSourceSettings(
                    priorities=SourcePriorities.model_validate(
                        {
                            "daily_bars": ["tdx_local"],
                            "weekly_bars": ["baostock"],
                            "minute_bars": ["baostock"],
                            "instruments": ["akshare"],
                            "trading_calendar": ["baostock"],
                        }
                    ),
                    tdx_path=str(tdx_root),
                )
            )
        finally:
            source_settings.close()
    finally:
        engine.dispose()


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="stock-desk-e2e-")).resolve()
    _seed(data_dir)
    os.environ["STOCK_DESK_DATA_DIR"] = str(data_dir)
    os.environ["STOCK_DESK_DATABASE_URL"] = f"sqlite:///{data_dir / 'stock-desk.db'}"
    received_signal: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal received_signal
        received_signal = signum

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    commands = (
        (
            sys.executable,
            "-m",
            "uvicorn",
            "stock_desk.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ),
        (sys.executable, "-m", "stock_desk.tasks.worker"),
        ("pnpm", "--dir", "web", "dev"),
    )
    try:
        return supervise(commands, requested_signal=lambda: received_signal)
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
