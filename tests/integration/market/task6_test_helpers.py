from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy.engine import Engine

from stock_desk.market.provenance import (
    InstrumentRoutingRequest,
    RoutedInstrumentSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.base import DatasetProvenance, ProviderBatch
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import (
    Exchange,
    Instrument,
    InstrumentKind,
    ListingStatus,
    MarketCapability,
    ProviderId,
)
from stock_desk.storage.database import create_engine_for_url, migrate


def instrument(
    symbol: str,
    name: str,
    *,
    kind: InstrumentKind = InstrumentKind.STOCK,
    status: ListingStatus = ListingStatus.LISTED,
) -> Instrument:
    exchange = Exchange(symbol.rsplit(".", maxsplit=1)[1])
    listed_on = date(2000, 1, 1)
    return Instrument(
        symbol=symbol,
        exchange=exchange,
        name=name,
        instrument_kind=kind,
        listing_status=status,
        listed_on=listed_on,
        delisted_on=date(2020, 1, 1) if status is ListingStatus.DELISTED else None,
    )


def routed_instruments(
    items: tuple[Instrument, ...],
    *,
    cutoff: datetime = datetime(2026, 7, 6, 8, tzinfo=timezone.utc),
    fetched_at: datetime = datetime(2026, 7, 6, 9, tzinfo=timezone.utc),
    source: ProviderId = ProviderId.TUSHARE,
) -> RoutedInstrumentSuccess:
    ordered = tuple(sorted(items, key=lambda item: item.symbol))
    version = dataset_version(
        source=source,
        operation="instruments",
        request={},
        data_cutoff=cutoff,
        items=ordered,
    )
    provenance = DatasetProvenance(
        source=source,
        fetched_at=fetched_at,
        data_cutoff=cutoff,
        dataset_version=version,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.INSTRUMENTS,
        request=InstrumentRoutingRequest(),
        priority=(source,),
        attempts=(),
        selected_source=source,
        upstream_dataset_version=version,
        upstream_fetched_at=fetched_at,
        upstream_data_cutoff=cutoff,
        upstream_adjustment=None,
    )
    return RoutedInstrumentSuccess(
        batch=ProviderBatch[Instrument](items=ordered, provenance=provenance),
        manifest=manifest,
    )


def task6_database(tmp_path: Path) -> tuple[str, Engine]:
    url = f"sqlite:///{tmp_path / 'task6.db'}"
    migrate(url)
    return url, create_engine_for_url(url)
