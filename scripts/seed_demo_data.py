"""Safely seed the public synthetic Stock Desk demo through production APIs."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Literal, Self, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from stock_desk.analysis.data_service import (
    ResearchDataService,
    ResearchDataUnavailable,
    ResearchLoadDiagnostic,
    ResearchSourceCandidate,
)
from stock_desk.analysis.model_catalog import AnalysisModelCatalog, ModelConfigStatus
from stock_desk.analysis.model_config import (
    AnalysisModelPublicConfig,
    ModelProviderKind,
)
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchSection,
    ResearchSectionKind,
)
from stock_desk.formula.repository import FormulaRepository
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
from stock_desk.market.pools import PoolCategory, PoolComposition, PoolRepository
from stock_desk.market.provenance import (
    BarRoutingRequest,
    ExecutionStatusRoutingRequest,
    InstrumentRoutingRequest,
    RoutedBarSuccess,
    RoutedExecutionStatusSuccess,
    RoutedInstrumentSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.base import DatasetProvenance, ProviderBatch
from stock_desk.market.providers.normalization import dataset_version
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
from stock_desk.storage.database import create_engine_for_url, migrate


ROOT = Path(__file__).resolve().parent.parent
DEMO_FIXTURE_PATH = ROOT / "tests" / "fixtures" / "demo_market" / "manifest.json"
DEMO_SCHEMA_VERSION = "stock-desk-public-demo-v1"
DEMO_MARKER = ".stock-desk-public-demo.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")
MACD_SOURCE = (
    "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"
    "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
)
CUSTOM_SOURCE = (
    "FAST:EMA(C,3);SLOW:EMA(C,7);BUY:CROSS(FAST,SLOW);SELL:CROSS(SLOW,FAST);"
)


class _FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DemoSymbol(_FixtureModel):
    symbol: str
    name: str
    wave_phase: int


class DemoScenario(_FixtureModel):
    kind: str
    symbol: str
    detail: str


class DemoCategoryOutcome(_FixtureModel):
    status: Literal["actual", "missing"]
    source: str
    data_cutoff: str
    missing_reason: str | None
    substitute: None

    @model_validator(mode="after")
    def validate_missing(self) -> Self:
        if (self.status == "missing") != (self.missing_reason is not None):
            raise ValueError("demo category missing outcome is inconsistent")
        return self


class DemoResearchItem(_FixtureModel):
    source_record: str
    content: dict[str, JsonValue]


class _DemoResearchLoader:
    def __init__(
        self,
        *,
        fixture: "DemoFixture",
        kind: ResearchSectionKind,
        item: DemoResearchItem | None,
    ) -> None:
        self.kind = kind
        self._fixture = fixture
        self._item = item

    def load(self, symbol: str) -> ResearchSection:
        if self._item is None:
            raise ResearchDataUnavailable(
                kind=self.kind,
                reason=ResearchMissingReason.NO_DATA,
                attempted_sources=("synthetic_fixture",),
                ordered_candidates=(
                    ResearchSourceCandidate(
                        source="synthetic_fixture",
                        position=0,
                        supported=True,
                        configured=True,
                        outcome="failed",
                        failure_reason=ResearchMissingReason.NO_DATA,
                    ),
                ),
                route_source="synthetic_fixture",
            )
        cutoff = _parse_datetime(self._fixture.data_cutoff)
        published = (
            cutoff
            if self.kind
            in {
                ResearchSectionKind.ANNOUNCEMENTS,
                ResearchSectionKind.NEWS,
            }
            else None
        )
        return ResearchSection(  # type: ignore[call-arg]
            kind=self.kind,
            canonical_source="synthetic_fixture",
            source_record=self._item.source_record,
            source_url=f"https://example.invalid/stock-desk-demo/{self.kind.value}",
            published_at=published,
            data_cutoff=cutoff,
            fetched_at=_parse_datetime(self._fixture.generated_at),
            dataset_version=_digest(
                {
                    "schema": self._fixture.schema_version,
                    "symbol": symbol,
                    "kind": self.kind.value,
                    "content": self._item.content,
                }
            ),
            quality_flags=(),
            content=self._item.content,
        )

    def load_with_diagnostics(
        self, symbol: str
    ) -> tuple[ResearchSection, ResearchLoadDiagnostic]:
        section = self.load(symbol)
        return section, ResearchLoadDiagnostic(
            kind=self.kind,
            route_source="synthetic_fixture",
            actual_source="synthetic_fixture",
            attempted_sources=("synthetic_fixture",),
            ordered_candidates=(
                ResearchSourceCandidate(
                    source="synthetic_fixture",
                    position=0,
                    supported=True,
                    configured=True,
                    outcome="selected",
                ),
            ),
        )


class DemoFixture(_FixtureModel):
    schema_version: Literal["stock-desk-public-demo-v1"]
    fixture_id: str
    label: str
    license: Literal["CC0-1.0"]
    network_policy: Literal["forbidden"]
    investment_recommendation_claims: Literal[False]
    generated_at: str
    data_cutoff: str
    window_start: str
    window_end: str
    scoring_start: str
    scoring_end: str
    symbols: tuple[DemoSymbol, ...]
    scenarios: tuple[DemoScenario, ...]
    category_outcomes: dict[str, DemoCategoryOutcome]
    research: dict[str, DemoResearchItem]

    @property
    def bar_dataset_version(self) -> str:
        return _routed_bars(
            self, "600000.SH", Period.DAY, Adjustment.NONE
        ).result.provenance.dataset_version

    def research_data_service(self) -> ResearchDataService:
        loaders = []
        for kind in (
            ResearchSectionKind.MARKET,
            ResearchSectionKind.FUNDAMENTALS,
            ResearchSectionKind.ANNOUNCEMENTS,
            ResearchSectionKind.NEWS,
        ):
            loaders.append(
                _DemoResearchLoader(
                    fixture=self,
                    kind=kind,
                    item=self.research.get(kind.value),
                )
            )
        return ResearchDataService(loaders=tuple(loaders), clock=self.clock)

    def clock(self) -> datetime:
        return _parse_datetime(self.generated_at)


class DemoResearchDataFactory:
    def __init__(self, fixture: DemoFixture, database_identity: object) -> None:
        if database_identity is None:
            raise ValueError("demo research factory requires database identity")
        self._fixture = fixture
        self.database_identity = database_identity

    def __call__(self) -> ResearchDataService:
        return self._fixture.research_data_service()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("demo fixture timestamp must be timezone-aware")
    return parsed


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_demo_fixture(path: Path = DEMO_FIXTURE_PATH) -> DemoFixture:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("demo fixture path must be a regular non-symlink file")
    return DemoFixture.model_validate_json(candidate.read_bytes())


def _local(day: date, clock: time = time()) -> datetime:
    return datetime.combine(day, clock, tzinfo=SHANGHAI)


def _weekdays(start: date, count: int) -> tuple[date, ...]:
    values: list[date] = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return tuple(values)


def _timestamps(period: Period) -> tuple[datetime, ...]:
    start = date(2024, 1, 2)
    if period is Period.DAY:
        return tuple(_local(day) for day in _weekdays(start, 475))
    if period is Period.WEEK:
        first_monday = start + timedelta(days=(-start.weekday()) % 7)
        return tuple(
            _local(first_monday + timedelta(weeks=index)) for index in range(95)
        )
    return tuple(
        _local(day, clock)
        for day in _weekdays(start, 165)
        for clock in (time(9, 30), time(10, 30), time(13), time(14))
    )


def _routed_bars(
    fixture: DemoFixture,
    symbol: str,
    period: Period,
    adjustment: Adjustment,
) -> RoutedBarSuccess:
    spec = next(item for item in fixture.symbols if item.symbol == symbol)
    timestamps = _timestamps(period)
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
        end=timestamps[-1] + interval,
    )
    closes: list[Decimal] = []
    for index in range(len(timestamps)):
        phase = (index + spec.wave_phase) % 20
        wave = phase if phase <= 10 else 20 - phase
        base = Decimal("9.5") + Decimal(wave) / Decimal("10")
        factor = {
            Adjustment.NONE: Decimal("1"),
            Adjustment.QFQ: Decimal("0.9"),
            Adjustment.HFQ: Decimal("1.1"),
        }[adjustment]
        closes.append((base * factor).quantize(Decimal("0.001")))
    bars: list[Bar] = []
    for index, (timestamp, close) in enumerate(zip(timestamps, closes, strict=True)):
        previous = closes[index - 1] if index else close
        status = TradingStatus.NORMAL
        if symbol == "300001.SZ" and index == 45:
            status = TradingStatus.LIMIT_UP
        elif symbol == "300001.SZ" and index == 46:
            status = TradingStatus.LIMIT_DOWN
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=timestamp,
                period=period,
                adjustment=adjustment,
                open=previous,
                high=max(previous, close) + Decimal("0.2"),
                low=min(previous, close) - Decimal("0.2"),
                close=close,
                volume=10_000 + index,
                status=status,
            )
        )
    cutoff = bars[-1].timestamp + min(interval, timedelta(hours=8))
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": query},
        data_cutoff=cutoff,
        items=tuple(bars),
    )
    provenance = Provenance(
        source=ProviderId.TUSHARE,
        fetched_at=cutoff + timedelta(minutes=5),
        data_cutoff=cutoff,
        adjustment=adjustment,
        dataset_version=version,
    )
    result = BarResult(
        query=query,
        bars=tuple(bars),
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=provenance,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=version,
        upstream_fetched_at=provenance.fetched_at,
        upstream_data_cutoff=cutoff,
        upstream_adjustment=adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


def _routed_status(routed: RoutedBarSuccess) -> RoutedExecutionStatusSuccess:
    result = routed.result
    symbol = str(result.query.symbol)
    bars = result.bars
    start = bars[0].timestamp.astimezone(SHANGHAI).date()
    final = result.query.end.astimezone(SHANGHAI)
    end = final.date() + (
        timedelta(days=1)
        if final.timetz().replace(tzinfo=None) != time()
        else timedelta()
    )
    exchange = Exchange(symbol.rsplit(".", 1)[1])
    query = ExecutionStatusQuery(
        symbol=symbol,
        exchange=exchange,
        start=start,
        end=end,
        period=result.query.period,
    )
    trading = {item.timestamp.astimezone(SHANGHAI).date() for item in bars}
    suspended = sorted(trading)[45] if symbol == "000001.SZ" else None
    days = tuple(
        ExecutionStatusDay(
            day=day,
            exchange=exchange,
            is_exchange_open=day in trading,
            suspension_state=(
                SuspensionState.SUSPENDED
                if day == suspended
                else SuspensionState.NORMAL
                if day in trading
                else SuspensionState.NOT_APPLICABLE
            ),
            raw_upper_limit=Decimal("12") if day in trading else None,
            raw_lower_limit=Decimal("8") if day in trading else None,
        )
        for day in (
            start + timedelta(days=index) for index in range((end - start).days)
        )
    )
    raw_bars = (
        bars
        if result.query.period is Period.MIN60
        else tuple(
            next(
                item
                for item in bars
                if item.timestamp.astimezone(SHANGHAI).date() == day
            )
            for day in sorted(trading)
        )
    )
    raw_opens = tuple(
        RawExecutionOpen(
            timestamp=(
                item.timestamp
                if result.query.period is Period.MIN60
                else _local(item.timestamp.astimezone(SHANGHAI).date(), time(9, 30))
            ),
            trading_day=item.timestamp.astimezone(SHANGHAI).date(),
            raw_open=item.open,
        )
        for item in raw_bars
    )
    status_result = materialize_execution_status(
        query=query,
        days=days,
        raw_opens=raw_opens,
        source=ProviderId.TUSHARE,
        fetched_at=result.provenance.fetched_at,
        data_cutoff=result.provenance.data_cutoff,
    )
    manifest = make_routing_manifest(
        category=MarketCapability.EXECUTION_STATUS,
        request=ExecutionStatusRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=status_result.dataset_version,
        upstream_fetched_at=status_result.fetched_at,
        upstream_data_cutoff=status_result.data_cutoff,
        upstream_adjustment=None,
    )
    return RoutedExecutionStatusSuccess(result=status_result, manifest=manifest)


def _routed_instruments(fixture: DemoFixture) -> RoutedInstrumentSuccess:
    items = tuple(
        sorted(
            (
                Instrument(
                    symbol=item.symbol,
                    exchange=Exchange(item.symbol.rsplit(".", 1)[1]),
                    name=item.name,
                    instrument_kind=InstrumentKind.STOCK,
                    listing_status=ListingStatus.LISTED,
                    listed_on=date(2000, 1, 1),
                    delisted_on=None,
                )
                for item in fixture.symbols
            ),
            key=lambda item: item.symbol,
        )
    )
    cutoff = _parse_datetime(fixture.data_cutoff)
    fetched = _parse_datetime(fixture.generated_at)
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="instruments",
        request={},
        data_cutoff=cutoff,
        items=items,
    )
    batch = ProviderBatch[Instrument](
        items=items,
        provenance=DatasetProvenance(
            source=ProviderId.TUSHARE,
            fetched_at=fetched,
            data_cutoff=cutoff,
            dataset_version=version,
        ),
    )
    return RoutedInstrumentSuccess(
        batch=batch,
        manifest=make_routing_manifest(
            category=MarketCapability.INSTRUMENTS,
            request=InstrumentRoutingRequest(),
            priority=(ProviderId.TUSHARE,),
            attempts=(),
            selected_source=ProviderId.TUSHARE,
            upstream_dataset_version=version,
            upstream_fetched_at=fetched,
            upstream_data_cutoff=cutoff,
            upstream_adjustment=None,
        ),
    )


def _assert_safe_destination(destination: Path) -> Path:
    raw = Path(os.path.abspath(os.fspath(destination.expanduser())))
    home = Path.home().resolve()
    if raw in {Path("/"), home, ROOT.resolve()}:
        raise ValueError("unsafe demo destination")
    current = raw
    while True:
        if current.exists() and stat.S_ISLNK(current.lstat().st_mode):
            raise ValueError("demo destination cannot contain symlinks")
        if current.parent == current:
            break
        current = current.parent
    if raw.exists() and not raw.is_dir():
        raise ValueError("demo destination must be a directory")
    marker = raw / DEMO_MARKER
    if marker.is_symlink():
        raise ValueError("demo marker cannot be a symlink")
    if raw.exists() and any(raw.iterdir()) and not marker.is_file():
        raise ValueError("demo destination contains unrelated data")
    return raw


def _seed_fresh(destination: Path, fixture: DemoFixture) -> dict[str, object]:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_url = f"sqlite:///{destination / 'stock-desk.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    try:
        instruments = InstrumentRepository(engine)
        instrument_manifest = instruments.ingest(_routed_instruments(fixture))
        pools = PoolRepository(engine)
        full_a = pools.publish_full_a()
        cutoff = _parse_datetime(fixture.data_cutoff)
        index = pools.publish_preset(
            PoolComposition(
                preset_key="index-synthetic-demo",
                category=PoolCategory.INDEX,
                display_name="Synthetic Demo Index",
                symbols=("600000.SH", "000001.SZ", "600036.SH"),
                source=ProviderId.AKSHARE,
                dataset_version=_digest(
                    {"fixture": fixture.fixture_id, "pool": "index"}
                ),
                route_version=_digest(
                    {"fixture": fixture.fixture_id, "route": "index"}
                ),
                fetched_at=cutoff,
                data_cutoff=cutoff,
                complete=True,
            )
        )
        industry = pools.publish_preset(
            PoolComposition(
                preset_key="industry-synthetic-demo",
                category=PoolCategory.INDUSTRY,
                display_name="Synthetic Demo Industry",
                symbols=("600000.SH", "300001.SZ"),
                source=ProviderId.AKSHARE,
                dataset_version=_digest(
                    {"fixture": fixture.fixture_id, "pool": "industry"}
                ),
                route_version=_digest(
                    {"fixture": fixture.fixture_id, "route": "industry"}
                ),
                fetched_at=cutoff,
                data_cutoff=cutoff,
                complete=True,
            )
        )
        lake = MarketLake(engine=engine, root=(destination / "market").resolve())
        statuses = ExecutionStatusLake(engine)
        bar_versions: dict[str, str] = {}
        for symbol in ("600000.SH", "000001.SZ", "300001.SZ"):
            for period in Period:
                for adjustment in Adjustment:
                    routed = _routed_bars(fixture, symbol, period, adjustment)
                    lake.write(routed)
                    bar_versions[f"{symbol}:{period.value}:{adjustment.value}"] = (
                        routed.result.provenance.dataset_version
                    )
                    statuses.write(_routed_status(routed))
        formulas = FormulaRepository(engine)
        formulas.create(
            "Demo MACD (synthetic)",
            "trading",
            MACD_SOURCE,
            {},
            placement="subchart",
        )
        formulas.create(
            "Demo custom wave (synthetic)",
            "trading",
            CUSTOM_SOURCE,
            {},
            placement="subchart",
        )
        catalog = AnalysisModelCatalog(engine, owns_engine=False, clock=fixture.clock)
        model = catalog.create(
            display_name="Deterministic demo model",
            public_config=AnalysisModelPublicConfig(
                provider=ModelProviderKind.OLLAMA,
                base_url="http://127.0.0.1:11434",
                model="stock-desk-demo-stub",
                temperature=0.0,
                timeout_seconds=30.0,
                max_output_tokens=2048,
                secret_reference_id=None,
                api_key_configured=False,
            ),
        )
        verified = catalog.mark_test_result(
            model.id,
            expected_status=ModelConfigStatus.UNVERIFIED,
            expected_revision=model.revision,
            succeeded=True,
        )
        summary: dict[str, object] = {
            "fixture_schema": fixture.schema_version,
            "fixture_id": fixture.fixture_id,
            "license": fixture.license,
            "instrument_dataset_version": instrument_manifest.dataset_version,
            "full_a_snapshot_id": full_a.snapshot_id,
            "index_snapshot_id": index.snapshot_id,
            "industry_snapshot_id": industry.snapshot_id,
            "primary_bar_dataset_version": bar_versions["600000.SH:1d:none"],
            "model_config_id": verified.id,
            "symbols": [item.symbol for item in fixture.symbols],
            "seed_state": "created",
        }
        return summary
    finally:
        engine.dispose()


def seed_demo_data(destination: Path) -> dict[str, object]:
    target = _assert_safe_destination(Path(destination))
    marker = target / DEMO_MARKER
    if marker.is_file():
        payload = cast(
            dict[str, object], json.loads(marker.read_text(encoding="utf-8"))
        )
        if (
            payload.get("fixture_schema") != DEMO_SCHEMA_VERSION
            or not (target / "stock-desk.db").is_file()
        ):
            raise ValueError("existing demo destination is invalid")
        return {**payload, "seed_state": "already_seeded"}
    fixture = load_demo_fixture()
    summary = _seed_fresh(target, fixture)
    marker.write_text(
        json.dumps(summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    marker.chmod(0o600)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="new or previously seeded isolated demo data directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = seed_demo_data(args.destination)
    except (OSError, ValueError) as error:
        print(f"Demo seed rejected: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
