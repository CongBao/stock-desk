from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo

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


SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_time(day: date, hour: int = 0) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=SHANGHAI)


def routed_daily_bars(
    days: tuple[date, ...],
    *,
    symbol: str = "600000.SH",
    source: ProviderId = ProviderId.TUSHARE,
    fetched_at: datetime | None = None,
    adjustment: Adjustment = Adjustment.QFQ,
    volume_delta: int = 0,
) -> RoutedBarSuccess:
    if not days:
        raise ValueError("test dataset requires at least one day")
    query = BarQuery(
        symbol=symbol,
        period=Period.DAY,
        adjustment=adjustment,
        start=local_time(days[0]),
        end=local_time(days[-1] + timedelta(days=1)),
    )
    if adjustment is Adjustment.NONE:
        prices = (
            Decimal("10.12500000"),
            Decimal("12.25000000"),
            Decimal("9.50000000"),
            Decimal("11.75000000"),
        )
    else:
        prices = (
            Decimal("-2.12500000"),
            Decimal("-1.25000000"),
            Decimal("-3.50000000"),
            Decimal("-2.75000000"),
        )
    bars = tuple(
        Bar(
            symbol=query.symbol,
            timestamp=local_time(day),
            period=query.period,
            adjustment=query.adjustment,
            open=prices[0],
            high=prices[1],
            low=prices[2],
            close=prices[3],
            volume=(
                2**63 - 1 + volume_delta
                if index == len(days) - 1
                else 1_000 + index + volume_delta
            ),
            status=(
                TradingStatus.SUSPENDED
                if index == len(days) - 1
                else TradingStatus.NORMAL
            ),
        )
        for index, day in enumerate(days)
    )
    data_cutoff = local_time(days[-1], 15)
    observed_at = fetched_at or local_time(days[-1], 16)
    version = dataset_version(
        source=source,
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
            source=source,
            fetched_at=observed_at,
            data_cutoff=data_cutoff,
            adjustment=query.adjustment,
            dataset_version=version,
        ),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(source,),
        attempts=(),
        selected_source=source,
        upstream_dataset_version=version,
        upstream_fetched_at=observed_at,
        upstream_data_cutoff=data_cutoff,
        upstream_adjustment=query.adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


def expected_manifest_record_id(routed: RoutedBarSuccess) -> str:
    payload = routed.manifest.model_dump(mode="json")
    request = payload["request"]
    assert isinstance(request, dict)
    query = request["query"]
    assert isinstance(query, dict)
    if query.get("instrument_kind") == "stock":
        # routing-manifest-v1 predates typed instruments, so the published
        # identity omits only the default stock discriminator.
        query.pop("instrument_kind")
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
