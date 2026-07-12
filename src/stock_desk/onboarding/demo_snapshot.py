from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
from typing import Literal, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_desk.api.market import MarketServices
from stock_desk.market.providers.base import DatasetProvenance, ProviderBatch
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.provenance import (
    BarRoutingRequest,
    InstrumentRoutingRequest,
    RoutedBarSuccess,
    RoutedInstrumentSuccess,
    make_routing_manifest,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarQuery,
    BarResult,
    Exchange,
    Instrument,
    InstrumentKind,
    ListingStatus,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingStatus,
)
from stock_desk.onboarding.models import OnboardingInstrument


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RESOURCE_PACKAGE = "stock_desk.demo"
_RESOURCE_NAME = "market_snapshot.json"
_MARKER = ".stock-desk-bundled-demo-v1.json"


class _SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _SnapshotInstrument(_SnapshotModel):
    symbol: Literal["600000.SH"]
    name: str = Field(min_length=1, max_length=255)
    exchange: Literal["SH"]
    instrument_kind: Literal["stock"]


class _SnapshotSeries(_SnapshotModel):
    start: date
    daily_rows: int = Field(ge=60, le=500)
    weekly_rows: int = Field(ge=60, le=200)
    minute_60_days: int = Field(ge=60, le=200)
    base_price: Decimal = Field(gt=0)
    wave_step: Decimal = Field(gt=0)
    wave_length: int = Field(ge=4, le=100)


class BundledDemoManifest(_SnapshotModel):
    schema_version: Literal["stock-desk-bundled-demo-v1"]
    fixture_id: str = Field(min_length=1, max_length=128)
    label: Literal["公开合成演示数据 · 非真实行情"]
    english_label: Literal["PUBLIC SYNTHETIC DEMO DATA - NOT REAL MARKET DATA"]
    license: Literal["CC0-1.0"]
    source: Literal["stock_desk_demo"]
    synthetic: Literal[True]
    real_market_data: Literal[False]
    investment_recommendation: Literal[False]
    network_policy: Literal["forbidden"]
    generated_at: datetime
    instrument: _SnapshotInstrument
    series: _SnapshotSeries

    @model_validator(mode="after")
    def validate_generated_at(self) -> Self:
        if self.generated_at.tzinfo is None:
            raise ValueError("demo generated_at must be timezone-aware")
        return self


def load_bundled_demo_manifest() -> BundledDemoManifest:
    resource = files(_RESOURCE_PACKAGE).joinpath(_RESOURCE_NAME)
    return BundledDemoManifest.model_validate_json(resource.read_bytes())


def _weekdays(start: date, count: int) -> tuple[date, ...]:
    result: list[date] = []
    current = start
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


def _timestamps(manifest: BundledDemoManifest, period: Period) -> tuple[datetime, ...]:
    if period is Period.DAY:
        return tuple(
            datetime.combine(day, time(), tzinfo=_SHANGHAI)
            for day in _weekdays(manifest.series.start, manifest.series.daily_rows)
        )
    if period is Period.WEEK:
        first = manifest.series.start + timedelta(
            days=(-manifest.series.start.weekday()) % 7
        )
        return tuple(
            datetime.combine(first + timedelta(weeks=index), time(), tzinfo=_SHANGHAI)
            for index in range(manifest.series.weekly_rows)
        )
    return tuple(
        datetime.combine(day, clock, tzinfo=_SHANGHAI)
        for day in _weekdays(manifest.series.start, manifest.series.minute_60_days)
        for clock in (time(9, 30), time(10, 30), time(13), time(14))
    )


def _instrument_route(manifest: BundledDemoManifest) -> RoutedInstrumentSuccess:
    item = Instrument(
        symbol=manifest.instrument.symbol,
        exchange=Exchange.SH,
        name=manifest.instrument.name,
        instrument_kind=InstrumentKind.STOCK,
        listing_status=ListingStatus.LISTED,
        listed_on=manifest.series.start,
    )
    items = (item,)
    generated_at = manifest.generated_at
    version = dataset_version(
        source=ProviderId.STOCK_DESK_DEMO,
        operation="instruments",
        request={},
        data_cutoff=generated_at,
        items=items,
    )
    provenance = DatasetProvenance(
        source=ProviderId.STOCK_DESK_DEMO,
        fetched_at=generated_at,
        data_cutoff=generated_at,
        dataset_version=version,
    )
    return RoutedInstrumentSuccess(
        batch=ProviderBatch(items=items, provenance=provenance),
        manifest=make_routing_manifest(
            category=MarketCapability.INSTRUMENTS,
            request=InstrumentRoutingRequest(),
            priority=(ProviderId.STOCK_DESK_DEMO,),
            attempts=(),
            selected_source=ProviderId.STOCK_DESK_DEMO,
            upstream_dataset_version=version,
            upstream_fetched_at=generated_at,
            upstream_data_cutoff=generated_at,
            upstream_adjustment=None,
        ),
    )


def _bar_route(
    manifest: BundledDemoManifest, period: Period, adjustment: Adjustment
) -> RoutedBarSuccess:
    timestamps = _timestamps(manifest, period)
    factor = {
        Adjustment.NONE: Decimal("1"),
        Adjustment.QFQ: Decimal("0.9"),
        Adjustment.HFQ: Decimal("1.1"),
    }[adjustment]
    closes: list[Decimal] = []
    for index in range(len(timestamps)):
        phase = index % manifest.series.wave_length
        wave = min(phase, manifest.series.wave_length - phase)
        closes.append(
            (
                (manifest.series.base_price + manifest.series.wave_step * wave) * factor
            ).quantize(Decimal("0.001"))
        )
    bars = tuple(
        Bar(
            symbol=manifest.instrument.symbol,
            timestamp=timestamp,
            period=period,
            adjustment=adjustment,
            open=closes[index - 1] if index else close,
            high=max(closes[index - 1] if index else close, close) + Decimal("0.2"),
            low=min(closes[index - 1] if index else close, close) - Decimal("0.2"),
            close=close,
            volume=10_000 + index,
            status=TradingStatus.NORMAL,
        )
        for index, (timestamp, close) in enumerate(zip(timestamps, closes, strict=True))
    )
    interval = (
        timedelta(hours=1)
        if period is Period.MIN60
        else timedelta(days=7)
        if period is Period.WEEK
        else timedelta(days=1)
    )
    query = BarQuery(
        symbol=manifest.instrument.symbol,
        period=period,
        adjustment=adjustment,
        start=bars[0].timestamp,
        end=bars[-1].timestamp + interval,
    )
    cutoff = bars[-1].timestamp + min(interval, timedelta(hours=8))
    version = dataset_version(
        source=ProviderId.STOCK_DESK_DEMO,
        operation="bars",
        request={"query": query},
        data_cutoff=cutoff,
        items=bars,
    )
    provenance = Provenance(
        source=ProviderId.STOCK_DESK_DEMO,
        fetched_at=manifest.generated_at,
        data_cutoff=cutoff,
        adjustment=adjustment,
        dataset_version=version,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=provenance,
    )
    return RoutedBarSuccess(
        result=result,
        manifest=make_routing_manifest(
            category=MarketCapability.BARS,
            request=BarRoutingRequest(query=query),
            priority=(ProviderId.STOCK_DESK_DEMO,),
            attempts=(),
            selected_source=ProviderId.STOCK_DESK_DEMO,
            upstream_dataset_version=version,
            upstream_fetched_at=provenance.fetched_at,
            upstream_data_cutoff=cutoff,
            upstream_adjustment=adjustment,
        ),
    )


def _seed(root: Path, manifest: BundledDemoManifest) -> None:
    services = MarketServices.open(
        database_url=f"sqlite:///{root / 'stock-desk-demo.db'}",
        lake_root=(root / "market").resolve(),
    )
    try:
        services.instruments.ingest(_instrument_route(manifest))
        for period in Period:
            for adjustment in Adjustment:
                services.lake.write(_bar_route(manifest, period, adjustment))
        (root / _MARKER).write_text(
            json.dumps(
                {
                    "schema_version": manifest.schema_version,
                    "fixture_id": manifest.fixture_id,
                    "label": manifest.label,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        services.close()


class BundledDemoMarket:
    def __init__(
        self, *, services: MarketServices, manifest: BundledDemoManifest
    ) -> None:
        self.services = services
        self.manifest = manifest

    @property
    def label(self) -> str:
        return self.manifest.label

    @property
    def instrument(self) -> OnboardingInstrument:
        return OnboardingInstrument(
            symbol=self.manifest.instrument.symbol,
            name=self.manifest.instrument.name,
            exchange=Exchange.SH,
            instrument_kind=InstrumentKind.STOCK,
        )

    @classmethod
    def open(cls, data_dir: Path) -> BundledDemoMarket:
        manifest = load_bundled_demo_manifest()
        data_root = Path(os.path.abspath(os.fspath(data_dir.expanduser())))
        root = data_root / "demo-market"
        marker = root / _MARKER
        if not marker.is_file():
            if root.exists():
                raise ValueError("bundled demo storage is incomplete")
            data_root.mkdir(parents=True, exist_ok=True)
            staging = data_root / f".demo-market-staging-{uuid4().hex}"
            staging.mkdir(mode=0o700)
            try:
                _seed(staging, manifest)
                os.replace(staging, root)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        if (
            marker_payload.get("schema_version") != manifest.schema_version
            or marker_payload.get("fixture_id") != manifest.fixture_id
        ):
            raise ValueError("bundled demo storage identity mismatch")
        services = MarketServices.open(
            database_url=f"sqlite:///{root / 'stock-desk-demo.db'}",
            lake_root=(root / "market").resolve(),
        )
        return cls(services=services, manifest=manifest)

    def close(self) -> None:
        self.services.close()
