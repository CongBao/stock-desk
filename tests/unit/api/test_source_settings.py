from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import Engine, event, func, insert, select

import stock_desk.api.settings as settings_module
from stock_desk.api.settings import (
    PUBLIC_SOURCE_SETTINGS_KEY,
    SourceSettingsServices,
    SourceSettingsStorageError,
)
from stock_desk.config import Settings
from stock_desk.analysis.snapshot import ResearchSectionKind
from stock_desk.analysis.sources.routing import ResearchSourceRouter
from stock_desk.main import create_app
from stock_desk.market.providers.base import ProviderPermissionDenied
from stock_desk.market.providers.base import (
    DatasetProvenance,
    ProviderBatch,
    ProviderBatchFailure,
    ProviderOperation,
)
from stock_desk.market.execution_status import (
    ExecutionStatusDay,
    ExecutionStatusQuery,
    RawExecutionOpen,
    SuspensionState,
    materialize_execution_status,
)
from stock_desk.market.types import (
    Adjustment,
    Bar,
    BarFailure,
    BarQuery,
    BarResult,
    CapabilityGap,
    CapabilityReport,
    CapabilityState,
    Exchange,
    FailureReason,
    Instrument,
    InstrumentKind,
    ListingStatus,
    MarketCapability,
    Period,
    Provenance,
    ProviderId,
    TradingDay,
    TradingStatus,
)
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.storage.models import AppSetting, MarketDataset
from tests.unit.market.providers.tdx_test_helpers import raw_record


TOKEN = "ts-private-token-123456"
FIXED_NOW = datetime(2026, 7, 6, 9, 30, tzinfo=timezone.utc)
BLOCKED_DIAGNOSTIC_WATCHDOG_SECONDS = 30
DEFAULT_PRIORITIES = {
    "daily_bars": [
        "tushare",
        "akshare",
        "baostock",
        "tdx_local",
        "eastmoney",
    ],
    "weekly_bars": ["tushare", "akshare", "baostock", "eastmoney"],
    "minute_bars": ["tushare", "baostock", "eastmoney"],
    "instruments": ["tushare", "akshare", "baostock", "eastmoney"],
    "trading_calendar": ["tushare", "baostock", "eastmoney"],
    "execution_status": ["tushare", "baostock", "akshare"],
    "fundamentals": ["tushare", "akshare"],
    "announcements": ["tushare", "akshare"],
    "news": ["akshare"],
}


def result_while_diagnostic_remains_blocked[T](
    future: Future[T],
    release: threading.Event,
    block_exited: threading.Event,
) -> T:
    """Wait for an independent operation without releasing the blocked probe."""
    completed = threading.Event()
    future.add_done_callback(lambda _future: completed.set())
    assert completed.wait(timeout=5), (
        "the independent settings operation waited for the blocked diagnostic"
    )
    assert not release.is_set()
    assert not block_exited.is_set(), "the blocked diagnostic exited before release"
    return future.result()


LEGACY_V1_PRIORITIES = {
    key: value
    for key, value in DEFAULT_PRIORITIES.items()
    if key not in {"fundamentals", "announcements", "news"}
}


def make_valid_tdx_root(root: Path, *, raw_date: int = 20240701) -> Path:
    for market in ("sh", "sz"):
        (root / market / "lday").mkdir(parents=True)
    (root / "sh" / "lday" / "sh600000.day").write_bytes(raw_record(raw_date=raw_date))
    return root


class AvailableProvider:
    def __init__(self, source: ProviderId) -> None:
        self.name = source
        self.closed = False

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            source=self.name,
            state=CapabilityState.AVAILABLE,
            capabilities=frozenset(MarketCapability),
            available_periods=frozenset(Period),
            available_adjustments=frozenset(Adjustment),
            markets=frozenset(Exchange),
            data_cutoff=None,
            gaps=(),
        )

    def close(self) -> None:
        self.closed = True


class BlockingDiagnosticProvider(AvailableProvider):
    def __init__(
        self,
        source: ProviderId,
        *,
        phase: str,
        started: threading.Event,
        release: threading.Event,
        block_exited: threading.Event,
    ) -> None:
        super().__init__(source)
        self._phase = phase
        self._started = started
        self._release = release
        self._block_exited = block_exited

    def _block(self, phase: str) -> None:
        if self._phase == phase:
            self._started.set()
            try:
                assert self._release.wait(timeout=BLOCKED_DIAGNOSTIC_WATCHDOG_SECONDS)
            finally:
                self._block_exited.set()

    def capabilities(self) -> CapabilityReport:
        self._block("probe")
        return super().capabilities()

    def close(self) -> None:
        self._block("close")
        super().close()


def successful_bar_probe(query: BarQuery) -> BarResult:
    bar = Bar(
        symbol=query.symbol,
        timestamp=query.start,
        period=query.period,
        adjustment=query.adjustment,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=100,
        status=TradingStatus.UNKNOWN,
    )
    return BarResult(
        query=query,
        bars=(bar,),
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=ProviderId.TUSHARE,
            fetched_at=FIXED_NOW,
            data_cutoff=query.start,
            adjustment=query.adjustment,
            dataset_version=f"probe-{query.period.value}",
        ),
    )


class PartialTushareProvider(AvailableProvider):
    def __init__(self) -> None:
        super().__init__(ProviderId.TUSHARE)
        self.bar_calls: list[BarQuery] = []
        self.instrument_calls = 0
        self.calendar_calls: list[tuple[Exchange, date, date]] = []

    def fetch_bars(self, query: BarQuery) -> BarResult | BarFailure:
        self.bar_calls.append(query)
        if query.period is Period.MIN60:
            return BarFailure(
                query=query,
                source=self.name,
                reason=FailureReason.PERMISSION_DENIED,
                failed_start=query.start,
                failed_end=query.end,
                detail="provider permission was denied",
            )
        return successful_bar_probe(query)

    def fetch_instruments(self) -> ProviderBatch[Instrument]:
        self.instrument_calls += 1
        return ProviderBatch[Instrument](
            items=(
                Instrument(
                    symbol="600000.SH",
                    exchange=Exchange.SH,
                    name="浦发银行",
                    instrument_kind=InstrumentKind.STOCK,
                    listing_status=ListingStatus.LISTED,
                    listed_on=date(1999, 11, 10),
                ),
            ),
            provenance=DatasetProvenance(
                source=self.name,
                fetched_at=FIXED_NOW,
                data_cutoff=FIXED_NOW,
                dataset_version="probe-instruments",
            ),
        )

    def fetch_calendar(
        self, exchange: Exchange, start: date, end: date
    ) -> ProviderBatch[TradingDay]:
        self.calendar_calls.append((exchange, start, end))
        return ProviderBatch[TradingDay](
            items=(TradingDay(day=start, exchange=exchange, is_open=True),),
            provenance=DatasetProvenance(
                source=self.name,
                fetched_at=FIXED_NOW,
                data_cutoff=FIXED_NOW,
                dataset_version="probe-calendar",
            ),
        )

    def fetch_execution_status(self, query: ExecutionStatusQuery):
        return materialize_execution_status(
            query=query,
            days=(
                ExecutionStatusDay(
                    day=query.start,
                    exchange=query.exchange,
                    is_exchange_open=True,
                    suspension_state=SuspensionState.NORMAL,
                    raw_upper_limit=Decimal("11"),
                    raw_lower_limit=Decimal("9"),
                ),
            ),
            raw_opens=(
                RawExecutionOpen(
                    timestamp=datetime(2024, 1, 2, 1, 30, tzinfo=timezone.utc),
                    trading_day=query.start,
                    raw_open=Decimal("10"),
                ),
            ),
            source=ProviderId.TUSHARE,
            fetched_at=FIXED_NOW,
            data_cutoff=datetime(2024, 1, 2, 7, tzinfo=timezone.utc),
        )


class ProbedTushareProvider(PartialTushareProvider):
    def fetch_bars(self, query: BarQuery) -> BarResult:
        self.bar_calls.append(query)
        return successful_bar_probe(query)


class DeniedProvider:
    name = ProviderId.TUSHARE

    def fetch_bars(self, _query: BarQuery) -> object:
        raise ProviderPermissionDenied(TOKEN)

    def fetch_instruments(self) -> object:
        raise ProviderPermissionDenied(TOKEN)

    def fetch_calendar(self, _exchange: Exchange, _start: date, _end: date) -> object:
        raise ProviderPermissionDenied(TOKEN)

    def fetch_execution_status(self, _query: ExecutionStatusQuery) -> object:
        raise ProviderPermissionDenied(TOKEN)

    def capabilities(self) -> CapabilityReport:
        raise ProviderPermissionDenied(TOKEN)


class MaliciousProvider:
    name = ProviderId.TUSHARE

    def __init__(self, unsafe_path: str) -> None:
        self._unsafe_path = unsafe_path

    def fetch_calendar(self, _exchange: Exchange, _start: date, _end: date) -> object:
        raise RuntimeError(f"{TOKEN} {self._unsafe_path}")

    def capabilities(self) -> CapabilityReport:
        raise RuntimeError(f"{TOKEN} {self._unsafe_path}")


DiagnosticFactory = Callable[..., object]


@dataclass(frozen=True)
class ApiContext:
    client: TestClient
    engine: Engine
    services: SourceSettingsServices


@contextmanager
def settings_api(
    tmp_path: Path,
    *,
    master_key: str | None,
    diagnostic_factory: DiagnosticFactory | None = None,
    clock: Callable[[], datetime] = lambda: FIXED_NOW,
) -> Iterator[ApiContext]:
    database_url = f"sqlite:///{tmp_path / 'source-settings.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    settings = Settings(
        database_url=database_url,
        data_dir=tmp_path,
        master_key=SecretStr(master_key) if master_key is not None else None,
    )
    services = SourceSettingsServices(
        engine=engine,
        settings=settings,
        diagnostic_factory=diagnostic_factory,
        clock=clock,
    )
    try:
        with TestClient(
            create_app(settings, source_settings_services=services)
        ) as client:
            yield ApiContext(client=client, engine=engine, services=services)
    finally:
        services.close()
        engine.dispose()


@pytest.mark.parametrize("master_key", [None, "not-a-valid-fernet-key"])
def test_missing_or_invalid_master_key_is_safe_for_get_and_token_write(
    tmp_path: Path, master_key: str | None
) -> None:
    with settings_api(tmp_path, master_key=master_key) as context:
        fetched = context.client.get("/api/settings/sources/tushare")
        written = context.client.put(
            "/api/settings/sources/tushare", json={"token": TOKEN}
        )

    assert fetched.status_code == 200
    assert fetched.json() == {
        "source": "tushare",
        "configured": False,
        "secure_storage_available": False,
        "masked_hint": None,
    }
    assert written.status_code == 503
    assert written.json() == {"code": "secure_storage_unavailable"}
    assert TOKEN not in fetched.text
    assert TOKEN not in written.text


def test_tushare_token_is_write_only_masked_and_omission_keeps_ciphertext(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    with settings_api(tmp_path, master_key=key) as context:
        saved = context.client.put(
            "/api/settings/sources/tushare", json={"token": TOKEN}
        )
        with context.engine.connect() as connection:
            first_ciphertext = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == "secret.tushare_token"
                )
            ).scalar_one()
        omitted = context.client.put("/api/settings/sources/tushare", json={})
        fetched = context.client.get("/api/settings/sources/tushare")
        with context.engine.connect() as connection:
            second_ciphertext = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == "secret.tushare_token"
                )
            ).scalar_one()

    for response in (saved, omitted, fetched):
        assert response.status_code == 200
        assert TOKEN not in response.text
        assert key not in response.text
    assert fetched.json() == {
        "source": "tushare",
        "configured": True,
        "secure_storage_available": True,
        "masked_hint": "ts-p•••••••3456",
    }
    assert TOKEN not in first_ciphertext
    assert first_ciphertext == second_ciphertext


def test_invalid_secret_request_is_fixed_and_never_echoes_input(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    with settings_api(tmp_path, master_key=key) as context:
        extra = context.client.put(
            "/api/settings/sources/tushare",
            json={"token": TOKEN, "unsafe": TOKEN},
        )
        wrong_content_type = context.client.put(
            "/api/settings/sources/tushare",
            content=json.dumps({"token": TOKEN}),
            headers={"Content-Type": "text/plain"},
        )

    for response in (extra, wrong_content_type):
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_request", "issues": []}
        assert TOKEN not in response.text


def test_public_priorities_and_tdx_path_use_bounded_canonical_json(
    tmp_path: Path,
) -> None:
    tdx_path = str(make_valid_tdx_root((tmp_path / "vipdoc").resolve()))
    priorities = {
        **DEFAULT_PRIORITIES,
        "daily_bars": ["tdx_local", "tushare", "akshare"],
    }
    with settings_api(tmp_path, master_key=None) as context:
        defaults = context.client.get("/api/settings/sources")
        saved = context.client.put(
            "/api/settings/sources",
            json={"priorities": priorities, "tdx_path": tdx_path},
        )
        fetched = context.client.get("/api/settings/sources")
        with context.engine.connect() as connection:
            stored = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == PUBLIC_SOURCE_SETTINGS_KEY
                )
            ).scalar_one()

    assert defaults.status_code == 200
    assert defaults.json()["priorities"] == DEFAULT_PRIORITIES
    assert defaults.json()["tdx_path"] is None
    assert saved.status_code == 200
    assert fetched.json()["priorities"] == priorities
    assert fetched.json()["tdx_path"] == tdx_path
    assert stored == json.dumps(
        {"priorities": priorities, "tdx_path": tdx_path},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert len(stored.encode("utf-8")) < 16_384


def test_legacy_v1_public_settings_are_read_with_research_defaults(
    tmp_path: Path,
) -> None:
    legacy = json.dumps(
        {"priorities": LEGACY_V1_PRIORITIES, "tdx_path": None},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with settings_api(tmp_path, master_key=None) as context:
        with context.engine.begin() as connection:
            connection.execute(
                insert(AppSetting).values(
                    key=PUBLIC_SOURCE_SETTINGS_KEY,
                    encrypted_value=legacy,
                    updated_at=FIXED_NOW,
                )
            )

        response = context.client.get("/api/settings/sources")
        with context.engine.connect() as connection:
            stored = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == PUBLIC_SOURCE_SETTINGS_KEY
                )
            ).scalar_one()

    assert response.status_code == 200
    assert response.json()["priorities"] == DEFAULT_PRIORITIES
    assert stored == legacy


@pytest.mark.parametrize(
    "legacy",
    [
        json.dumps(
            {
                "priorities": {
                    key: value
                    for key, value in LEGACY_V1_PRIORITIES.items()
                    if key != "execution_status"
                },
                "tdx_path": None,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        json.dumps(
            {
                "priorities": {
                    **LEGACY_V1_PRIORITIES,
                    "unexpected": ["tushare"],
                },
                "tdx_path": None,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        json.dumps(
            {"priorities": LEGACY_V1_PRIORITIES, "tdx_path": None},
            ensure_ascii=True,
            indent=2,
            sort_keys=False,
        ),
    ],
)
def test_legacy_v1_public_settings_remain_strict_and_canonical(
    tmp_path: Path,
    legacy: str,
) -> None:
    with settings_api(tmp_path, master_key=None) as context:
        with context.engine.begin() as connection:
            connection.execute(
                insert(AppSetting).values(
                    key=PUBLIC_SOURCE_SETTINGS_KEY,
                    encrypted_value=legacy,
                    updated_at=FIXED_NOW,
                )
            )

        response = context.client.get("/api/settings/sources")

    assert response.status_code == 500
    assert response.json() == {"code": "settings_corrupt"}


@pytest.mark.parametrize(
    ("category", "priority"),
    [
        ("fundamentals", ["baostock"]),
        ("announcements", ["tdx_local"]),
        ("news", ["tushare"]),
    ],
)
def test_research_priorities_reject_sources_without_declared_capability(
    tmp_path: Path,
    category: str,
    priority: list[str],
) -> None:
    priorities = json.loads(json.dumps(DEFAULT_PRIORITIES))
    priorities[category] = priority

    with settings_api(tmp_path, master_key=None) as context:
        response = context.client.put(
            "/api/settings/sources",
            json={"priorities": priorities, "tdx_path": None},
        )

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "issues": []}


@pytest.mark.parametrize("category", tuple(DEFAULT_PRIORITIES))
def test_public_source_priorities_reject_demo_provenance_in_every_category(
    category: str,
) -> None:
    priorities = json.loads(json.dumps(DEFAULT_PRIORITIES))
    priorities[category].append("stock_desk_demo")

    with pytest.raises(ValidationError, match="not configurable"):
        settings_module.SourcePriorities.model_validate(priorities)


def test_public_settings_api_rejects_demo_provenance_without_persisting(
    tmp_path: Path,
) -> None:
    priorities = json.loads(json.dumps(DEFAULT_PRIORITIES))
    priorities["daily_bars"].append("stock_desk_demo")

    with settings_api(tmp_path, master_key=None) as context:
        response = context.client.put(
            "/api/settings/sources",
            json={"priorities": priorities, "tdx_path": None},
        )
        with context.engine.connect() as connection:
            stored = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == PUBLIC_SOURCE_SETTINGS_KEY
                )
            ).scalar_one_or_none()

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "issues": []}
    assert stored is None


def test_public_settings_service_revalidates_constructed_models_before_write(
    tmp_path: Path,
) -> None:
    priorities = settings_module.SourcePriorities()
    unsafe_priorities = priorities.model_copy(
        update={
            "daily_bars": (
                *priorities.daily_bars,
                ProviderId.STOCK_DESK_DEMO,
            )
        }
    )
    unsafe_settings = settings_module.PublicSourceSettings().model_copy(
        update={"priorities": unsafe_priorities}
    )

    with settings_api(tmp_path, master_key=None) as context:
        with pytest.raises(ValueError, match="invalid"):
            context.services.save_public(unsafe_settings)
        with context.engine.connect() as connection:
            stored = connection.execute(
                select(AppSetting.encrypted_value).where(
                    AppSetting.key == PUBLIC_SOURCE_SETTINGS_KEY
                )
            ).scalar_one_or_none()

    assert stored is None


def test_persisted_demo_provenance_is_normalized_before_status_and_routing(
    tmp_path: Path,
) -> None:
    priorities = {
        category: [*values, "stock_desk_demo"]
        for category, values in DEFAULT_PRIORITIES.items()
    }
    stored = json.dumps(
        {"priorities": priorities, "tdx_path": None},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )

    with settings_api(tmp_path, master_key=None) as context:
        with context.engine.begin() as connection:
            connection.execute(
                insert(AppSetting).values(
                    key=PUBLIC_SOURCE_SETTINGS_KEY,
                    encrypted_value=stored,
                    updated_at=FIXED_NOW,
                )
            )

        response = context.client.get("/api/settings/sources")
        snapshot = context.services.runtime_snapshot()
        diagnostic = ResearchSourceRouter(
            kind=ResearchSectionKind.FUNDAMENTALS,
            priority=snapshot.priorities.fundamentals,
            sources=(),
        ).diagnostic_template()

    assert response.status_code == 200
    assert response.json()["priorities"] == DEFAULT_PRIORITIES
    assert all(
        ProviderId.STOCK_DESK_DEMO not in priority
        for priority in snapshot.priorities.model_dump().values()
    )
    assert [candidate.source for candidate in diagnostic.ordered_candidates] == [
        "tushare",
        "akshare",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body["priorities"].__setitem__(
            "daily_bars", ["tushare", "tushare"]
        ),
        lambda body: body["priorities"].__setitem__("minute_bars", ["akshare"]),
        lambda body: body["priorities"].__setitem__("instruments", ["unknown"]),
        lambda body: body.__setitem__("tdx_path", "relative/vipdoc"),
    ],
)
def test_public_settings_reject_duplicate_unknown_or_unusable_ordering(
    tmp_path: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    body: dict[str, Any] = {
        "priorities": json.loads(json.dumps(DEFAULT_PRIORITIES)),
        "tdx_path": None,
    }
    mutation(body)
    with settings_api(tmp_path, master_key=None) as context:
        response = context.client.put("/api/settings/sources", json=body)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request", "issues": []}


@pytest.mark.parametrize("tdx_path", ["/", "/x"])
def test_public_settings_reject_implausibly_short_absolute_tdx_path(
    tmp_path: Path, tdx_path: str
) -> None:
    with settings_api(tmp_path, master_key=None) as context:
        rejected = context.client.put(
            "/api/settings/sources",
            json={"priorities": DEFAULT_PRIORITIES, "tdx_path": tdx_path},
        )
        diagnostic = context.client.post("/api/settings/sources/tdx_local/test")

    assert rejected.status_code == 422
    assert rejected.json() == {"code": "invalid_request", "issues": []}
    assert diagnostic.status_code == 200
    assert diagnostic.json()["status"] == "unavailable"


def test_malformed_public_json_maps_to_fixed_corruption_error(tmp_path: Path) -> None:
    with settings_api(tmp_path, master_key=None) as context:
        with context.engine.begin() as connection:
            connection.execute(
                insert(AppSetting).values(
                    key=PUBLIC_SOURCE_SETTINGS_KEY,
                    encrypted_value=f'{{"unsafe":"{TOKEN}"',
                    updated_at=FIXED_NOW,
                )
            )
        response = context.client.get("/api/settings/sources")

    assert response.status_code == 500
    assert response.json() == {"code": "settings_corrupt"}
    assert TOKEN not in response.text


def test_all_settings_routes_fail_closed_after_atomic_database_replacement(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    database = tmp_path / "source-settings.db"
    replacement = tmp_path / "replacement.db"
    original_inode = tmp_path / "original-inode.db"
    migrate(f"sqlite:///{replacement}")

    with settings_api(tmp_path, master_key=key) as context:
        context.client.put("/api/settings/sources/tushare", json={"token": TOKEN})
        context.engine.dispose()
        os.replace(database, original_inode)
        os.replace(replacement, database)

        responses = (
            context.client.get("/api/settings/sources"),
            context.client.put(
                "/api/settings/sources",
                json={"priorities": DEFAULT_PRIORITIES, "tdx_path": None},
            ),
            context.client.get("/api/settings/sources/tushare"),
            context.client.put(
                "/api/settings/sources/tushare", json={"token": "replacement-token"}
            ),
            context.client.post("/api/settings/sources/tushare/test"),
        )

        replacement_engine = create_engine_for_url(f"sqlite:///{database}")
        try:
            with replacement_engine.connect() as connection:
                row_count = connection.execute(
                    select(func.count()).select_from(AppSetting)
                ).scalar_one()
        finally:
            replacement_engine.dispose()

    assert row_count == 0
    for response in responses:
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"code": "settings_storage_unavailable"}
        assert TOKEN not in response.text


def test_settings_identity_mismatch_permanently_poisons_old_inode_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "source-settings.db"
    replacement = tmp_path / "replacement.db"
    original_inode = tmp_path / "original-inode.db"
    migrate(f"sqlite:///{replacement}")

    with settings_api(tmp_path, master_key=None) as context:
        old_connection = context.engine.connect()
        try:
            context.engine.dispose()
            os.replace(database, original_inode)
            os.replace(replacement, database)

            first = context.client.get("/api/settings/sources")

            @contextmanager
            def borrow_old_connection() -> Iterator[object]:
                yield old_connection

            monkeypatch.setattr(
                context.engine, "connect", lambda: borrow_old_connection()
            )
            second = context.client.get("/api/settings/sources")
        finally:
            old_connection.close()

    for response in (first, second):
        assert response.status_code == 503
        assert response.json() == {"code": "settings_storage_unavailable"}


def test_settings_mismatch_waits_for_validated_old_inode_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "source-settings.db"
    replacement = tmp_path / "replacement-linearized.db"
    original_inode = tmp_path / "original-linearized.db"
    migrate(f"sqlite:///{replacement}")
    paused = threading.Event()
    release = threading.Event()
    thread_state = threading.local()

    with settings_api(tmp_path, master_key=None) as context:
        old_connection = context.engine.connect()
        context.engine.dispose()
        os.replace(database, original_inode)
        os.replace(replacement, database)
        real_connect = context.engine.connect

        def pause_old_statement(
            _connection: object,
            _cursor: object,
            _statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if getattr(thread_state, "old_reader", False):
                paused.set()
                assert release.wait(timeout=5)

        @contextmanager
        def borrow_old_connection() -> Iterator[object]:
            yield old_connection

        def connect_for_thread() -> object:
            if getattr(thread_state, "old_reader", False):
                return borrow_old_connection()
            return real_connect()

        event.listen(context.engine, "before_cursor_execute", pause_old_statement)
        monkeypatch.setattr(context.engine, "connect", connect_for_thread)

        def old_read() -> object:
            thread_state.old_reader = True
            return context.services.read_public()

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                old_future = executor.submit(old_read)
                assert paused.wait(timeout=5)
                mismatch_future = executor.submit(context.services.read_public)
                with pytest.raises(FutureTimeoutError):
                    mismatch_future.result(timeout=0.2)
                release.set()
                assert old_future.result(timeout=5).tdx_path is None
                with pytest.raises(SourceSettingsStorageError):
                    mismatch_future.result(timeout=5)
        finally:
            release.set()
            event.remove(context.engine, "before_cursor_execute", pause_old_statement)
            old_connection.close()


@pytest.mark.parametrize(
    "stored",
    [b"blob-ciphertext", "", "é", "x" * 20_000],
    ids=["blob", "empty", "non-ascii", "oversized"],
)
def test_corrupt_secret_scalar_is_masked_and_diagnostic_is_safe(
    tmp_path: Path, stored: object
) -> None:
    key = Fernet.generate_key().decode("ascii")
    with settings_api(tmp_path, master_key=key) as context:
        with context.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO app_setting (key, encrypted_value, updated_at) VALUES (?, ?, ?)",
                ("secret.tushare_token", stored, FIXED_NOW.isoformat()),
            )
        fetched = context.client.get("/api/settings/sources/tushare")
        diagnostic = context.client.post("/api/settings/sources/tushare/test")

    assert fetched.status_code == 200
    assert fetched.json() == {
        "source": "tushare",
        "configured": True,
        "secure_storage_available": False,
        "masked_hint": None,
    }
    assert diagnostic.status_code == 200
    assert diagnostic.json()["status"] == "unavailable"
    assert diagnostic.json()["fallback_reason"] == {
        "reason": "provider_unavailable",
        "detail": "Secure token storage is unavailable",
    }
    for rendered in (fetched.text, diagnostic.text):
        assert repr(stored) not in rendered
        assert TOKEN not in rendered


def test_concurrent_public_updates_remain_whole_and_readable(tmp_path: Path) -> None:
    with settings_api(tmp_path, master_key=None) as context:
        candidates = []
        for index in range(8):
            priorities = json.loads(json.dumps(DEFAULT_PRIORITIES))
            priorities["daily_bars"] = (
                ["tushare", "akshare", "baostock"]
                if index % 2 == 0
                else ["tdx_local", "tushare"]
            )
            tdx_path = make_valid_tdx_root(tmp_path / f"vipdoc-{index}")
            candidates.append({"priorities": priorities, "tdx_path": str(tdx_path)})
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(context.services.save_public, candidates))
        resolved = context.services.read_public().model_dump(mode="json")

    assert resolved in candidates


@pytest.mark.parametrize("blocking_phase", ["factory", "probe", "close"])
def test_blocking_diagnostic_never_blocks_concurrent_get(
    tmp_path: Path,
    blocking_phase: str,
) -> None:
    started = threading.Event()
    release = threading.Event()
    block_exited = threading.Event()
    provider = BlockingDiagnosticProvider(
        ProviderId.BAOSTOCK,
        phase=blocking_phase,
        started=started,
        release=release,
        block_exited=block_exited,
    )

    def factory(_source: ProviderId, **_context: object) -> object:
        if blocking_phase == "factory":
            started.set()
            try:
                assert release.wait(timeout=BLOCKED_DIAGNOSTIC_WATCHDOG_SECONDS)
            finally:
                block_exited.set()
        return provider

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=factory,
    ) as context:
        with ThreadPoolExecutor(max_workers=2) as executor:
            diagnostic_future = executor.submit(
                context.client.post,
                "/api/settings/sources/baostock/test",
            )
            assert started.wait(timeout=5)
            get_future = executor.submit(
                context.client.get,
                "/api/settings/sources",
            )
            try:
                fetched = result_while_diagnostic_remains_blocked(
                    get_future,
                    release,
                    block_exited,
                )
            finally:
                release.set()
            diagnostic = diagnostic_future.result(timeout=5)

    assert fetched.status_code == 200
    assert diagnostic.status_code == 200


def test_service_close_completes_while_probe_blocks_and_late_result_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    block_exited = threading.Event()
    provider = BlockingDiagnosticProvider(
        ProviderId.BAOSTOCK,
        phase="probe",
        started=started,
        release=release,
        block_exited=block_exited,
    )
    merge_calls = 0

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: provider,
    ) as context:
        original_merge = context.services._merge_diagnostic_evidence

        def track_merge(diagnostic: object) -> object:
            nonlocal merge_calls
            merge_calls += 1
            return original_merge(diagnostic)  # type: ignore[arg-type]

        monkeypatch.setattr(
            context.services,
            "_merge_diagnostic_evidence",
            track_merge,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            diagnostic_future = executor.submit(
                context.services.diagnose,
                ProviderId.BAOSTOCK,
            )
            assert started.wait(timeout=5)
            close_future = executor.submit(context.services.close)
            try:
                result_while_diagnostic_remains_blocked(
                    close_future,
                    release,
                    block_exited,
                )
            finally:
                release.set()
            with pytest.raises(SourceSettingsStorageError):
                diagnostic_future.result(timeout=5)

    assert provider.closed is True
    assert merge_calls == 0


def test_public_update_invalidates_blocked_diagnostic_without_merging_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    block_exited = threading.Event()
    provider = BlockingDiagnosticProvider(
        ProviderId.BAOSTOCK,
        phase="probe",
        started=started,
        release=release,
        block_exited=block_exited,
    )
    merge_calls = 0

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: provider,
    ) as context:
        original_merge = context.services._merge_diagnostic_evidence

        def track_merge(diagnostic: object) -> object:
            nonlocal merge_calls
            merge_calls += 1
            return original_merge(diagnostic)  # type: ignore[arg-type]

        monkeypatch.setattr(
            context.services,
            "_merge_diagnostic_evidence",
            track_merge,
        )
        replacement_root = make_valid_tdx_root(tmp_path / "replacement-vipdoc")
        replacement = {
            "priorities": DEFAULT_PRIORITIES,
            "tdx_path": str(replacement_root.resolve()),
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            diagnostic_future = executor.submit(
                context.services.diagnose,
                ProviderId.BAOSTOCK,
            )
            assert started.wait(timeout=5)
            update_future = executor.submit(context.services.save_public, replacement)
            try:
                assert (
                    result_while_diagnostic_remains_blocked(
                        update_future,
                        release,
                        block_exited,
                    ).tdx_path
                    == replacement["tdx_path"]
                )
            finally:
                release.set()
            diagnostic = diagnostic_future.result(timeout=5)

    assert diagnostic.status is CapabilityState.TRANSIENT_FAILURE
    assert diagnostic.fallback_reason is not None
    assert diagnostic.fallback_reason.reason is FailureReason.TRANSIENT_FAILURE
    assert provider.closed is True
    assert merge_calls == 0


def test_token_update_invalidates_blocked_diagnostic_without_merging_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    started = threading.Event()
    release = threading.Event()
    block_exited = threading.Event()
    replacement_token = "replacement-private-token"
    merge_calls = 0

    class BlockingTushareProvider(PartialTushareProvider):
        def fetch_bars(self, query: BarQuery) -> BarResult | BarFailure:
            if not started.is_set():
                started.set()
                try:
                    assert release.wait(timeout=BLOCKED_DIAGNOSTIC_WATCHDOG_SECONDS)
                finally:
                    block_exited.set()
            return super().fetch_bars(query)

    provider = BlockingTushareProvider()
    with settings_api(
        tmp_path,
        master_key=key,
        diagnostic_factory=lambda _source, **_context: provider,
    ) as context:
        context.services.update_tushare(
            settings_module.TushareSourceUpdateRequest(token=SecretStr(TOKEN))
        )
        original_merge = context.services._merge_diagnostic_evidence

        def track_merge(diagnostic: object) -> object:
            nonlocal merge_calls
            merge_calls += 1
            return original_merge(diagnostic)  # type: ignore[arg-type]

        monkeypatch.setattr(
            context.services,
            "_merge_diagnostic_evidence",
            track_merge,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            diagnostic_future = executor.submit(
                context.services.diagnose,
                ProviderId.TUSHARE,
            )
            assert started.wait(timeout=5)
            update_future = executor.submit(
                context.services.update_tushare,
                settings_module.TushareSourceUpdateRequest(
                    token=SecretStr(replacement_token)
                ),
            )
            try:
                assert (
                    result_while_diagnostic_remains_blocked(
                        update_future,
                        release,
                        block_exited,
                    ).configured
                    is True
                )
            finally:
                release.set()
            diagnostic = diagnostic_future.result(timeout=5)

    assert diagnostic.status is CapabilityState.TRANSIENT_FAILURE
    assert diagnostic.fallback_reason is not None
    assert diagnostic.fallback_reason.reason is FailureReason.TRANSIENT_FAILURE
    assert TOKEN not in repr(diagnostic)
    assert replacement_token not in repr(diagnostic)
    assert provider.closed is True
    assert merge_calls == 0


def test_permission_denial_starts_with_minute_gap_and_closes_owned_provider(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    available = AvailableProvider(ProviderId.BAOSTOCK)

    def factory(source: ProviderId, **_context: object) -> object:
        return DeniedProvider() if source is ProviderId.TUSHARE else available

    with settings_api(tmp_path, master_key=key, diagnostic_factory=factory) as context:
        context.client.put("/api/settings/sources/tushare", json={"token": TOKEN})
        denied = context.client.post("/api/settings/sources/tushare/test")
        successful = context.client.post("/api/settings/sources/baostock/test")

    assert denied.status_code == 200
    denied_body = denied.json()
    assert denied_body["status"] == "permission_denied"
    assert denied_body["gaps"][0] == {
        "category": "minute_bars",
        "state": "permission_denied",
        "reason": "permission_denied",
        "detail": "provider permission was denied",
    }
    assert denied_body["fallback_reason"] == {
        "reason": "permission_denied",
        "detail": "provider permission was denied",
    }
    assert denied_body["last_checked"] == "2026-07-06T09:30:00Z"
    assert TOKEN not in denied.text
    assert successful.status_code == 200
    assert successful.json()["status"] == "available"
    assert available.closed is True


def insert_market_dataset(
    engine: Engine,
    *,
    suffix: str,
    source: ProviderId,
    created_at: datetime,
    data_cutoff: datetime,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(MarketDataset).values(
                dataset_version=f"sha256:{suffix * 64}",
                source=source.value,
                symbol="600000.SH",
                period=Period.DAY.value,
                adjustment=Adjustment.NONE.value,
                query_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                query_end=datetime(2024, 1, 3, tzinfo=timezone.utc),
                data_cutoff=data_cutoff,
                row_count=2,
                created_at=created_at,
            )
        )


def test_diagnostic_merges_latest_source_specific_cached_dataset_evidence(
    tmp_path: Path,
) -> None:
    available = AvailableProvider(ProviderId.BAOSTOCK)
    created_old = datetime(2026, 7, 5, 8, tzinfo=timezone.utc)
    created_new = datetime(2026, 7, 5, 9, tzinfo=timezone.utc)
    cutoff_old = datetime(2026, 7, 4, 7, tzinfo=timezone.utc)
    cutoff_new = datetime(2026, 7, 5, 7, tzinfo=timezone.utc)

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: available,
    ) as context:
        insert_market_dataset(
            context.engine,
            suffix="a",
            source=ProviderId.BAOSTOCK,
            created_at=created_old,
            data_cutoff=cutoff_old,
        )
        insert_market_dataset(
            context.engine,
            suffix="b",
            source=ProviderId.BAOSTOCK,
            created_at=created_new,
            data_cutoff=cutoff_new,
        )
        insert_market_dataset(
            context.engine,
            suffix="c",
            source=ProviderId.TUSHARE,
            created_at=datetime(2026, 7, 5, 10, tzinfo=timezone.utc),
            data_cutoff=datetime(2026, 7, 5, 8, tzinfo=timezone.utc),
        )
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 200
    assert response.json()["last_update"] == "2026-07-05T09:00:00Z"
    assert response.json()["data_cutoff"] == "2026-07-05T07:00:00Z"


def test_diagnostic_accepts_dataset_committed_during_probe_and_finishes_after_read(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 7, 6, 9, 30, tzinfo=timezone.utc)
    evidence_time = datetime(2026, 7, 6, 9, 45, tzinfo=timezone.utc)
    probe_completed = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
    read_completed = datetime(2026, 7, 6, 10, 1, tzinfo=timezone.utc)

    class ProbeAwareClock:
        probe_is_complete = False
        completion_reads = 0

        def __call__(self) -> datetime:
            if not self.probe_is_complete:
                return started
            self.completion_reads += 1
            return probe_completed if self.completion_reads == 1 else read_completed

    clock = ProbeAwareClock()
    provider = AvailableProvider(ProviderId.BAOSTOCK)

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: provider,
        clock=clock,
    ) as context:
        original_capabilities = provider.capabilities

        def write_during_probe() -> CapabilityReport:
            insert_market_dataset(
                context.engine,
                suffix="f",
                source=ProviderId.BAOSTOCK,
                created_at=evidence_time,
                data_cutoff=evidence_time,
            )
            clock.probe_is_complete = True
            return original_capabilities()

        provider.capabilities = write_during_probe  # type: ignore[method-assign]
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 200
    assert response.json()["last_update"] == "2026-07-06T09:45:00Z"
    assert response.json()["data_cutoff"] == "2026-07-06T09:45:00Z"
    assert response.json()["last_checked"] == "2026-07-06T10:01:00Z"


def test_diagnostic_rejects_completion_clock_regression(tmp_path: Path) -> None:
    times = iter(
        (
            datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 6, 9, 59, tzinfo=timezone.utc),
        )
    )
    available = AvailableProvider(ProviderId.BAOSTOCK)

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: available,
        clock=lambda: next(times),
    ) as context:
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 503
    assert response.json() == {"code": "settings_storage_unavailable"}


def test_diagnostic_rejects_future_cached_dataset_evidence_as_storage_failure(
    tmp_path: Path,
) -> None:
    available = AvailableProvider(ProviderId.BAOSTOCK)
    future = datetime(2026, 7, 7, tzinfo=timezone.utc)

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: available,
    ) as context:
        insert_market_dataset(
            context.engine,
            suffix="d",
            source=ProviderId.BAOSTOCK,
            created_at=future,
            data_cutoff=future,
        )
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"code": "settings_storage_unavailable"}


def test_diagnostic_rejects_malformed_cached_datetime_as_storage_failure(
    tmp_path: Path,
) -> None:
    available = AvailableProvider(ProviderId.BAOSTOCK)

    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: available,
    ) as context:
        with context.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO market_dataset "
                "(dataset_version, source, symbol, period, adjustment, "
                "query_start, query_end, data_cutoff, row_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"sha256:{'e' * 64}",
                    ProviderId.BAOSTOCK.value,
                    "600000.SH",
                    Period.DAY.value,
                    Adjustment.NONE.value,
                    "2024-01-01T00:00:00+00:00",
                    "2024-01-03T00:00:00+00:00",
                    "not-a-datetime",
                    2,
                    "also-not-a-datetime",
                ),
            )
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"code": "settings_storage_unavailable"}


def test_tushare_runs_bounded_remote_probe_before_reporting_available(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    provider = ProbedTushareProvider()

    def factory(_source: ProviderId, **_context: object) -> object:
        return provider

    with settings_api(tmp_path, master_key=key, diagnostic_factory=factory) as context:
        context.client.put("/api/settings/sources/tushare", json={"token": TOKEN})
        response = context.client.post("/api/settings/sources/tushare/test")

    assert response.status_code == 200
    assert response.json()["status"] == "available"
    assert provider.calendar_calls == [
        (Exchange.SH, date(2024, 1, 2), date(2024, 1, 3))
    ]
    assert provider.closed is True


def test_tushare_probes_every_category_and_preserves_partial_permission_evidence(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    provider = PartialTushareProvider()

    def factory(_source: ProviderId, **_context: object) -> object:
        return provider

    with settings_api(tmp_path, master_key=key, diagnostic_factory=factory) as context:
        context.client.put("/api/settings/sources/tushare", json={"token": TOKEN})
        response = context.client.post("/api/settings/sources/tushare/test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "permission_denied"
    assert body["capabilities"] == [
        "bars",
        "execution_status",
        "instruments",
        "trading_calendar",
    ]
    assert body["available_periods"] == ["1d", "1w"]
    assert body["permissions"] == [
        {"category": "minute_bars", "state": "permission_denied"},
        {"category": "daily_bars", "state": "available"},
        {"category": "weekly_bars", "state": "available"},
        {"category": "instruments", "state": "available"},
        {"category": "trading_calendar", "state": "available"},
        {"category": "execution_status", "state": "available"},
    ]
    assert body["gaps"] == [
        {
            "category": "minute_bars",
            "state": "permission_denied",
            "reason": "permission_denied",
            "detail": "provider permission was denied",
        }
    ]
    assert body["fallback_reason"] == {
        "reason": "permission_denied",
        "detail": "provider permission was denied",
    }
    assert [query.period for query in provider.bar_calls] == [
        Period.MIN60,
        Period.DAY,
        Period.WEEK,
    ]
    assert provider.instrument_calls == 1
    assert provider.calendar_calls == [
        (Exchange.SH, date(2024, 1, 2), date(2024, 1, 3))
    ]
    assert provider.closed is True


@pytest.mark.parametrize(
    ("attack", "category"),
    [
        ("bar_failure_source", "minute_bars"),
        ("bar_failure_query", "minute_bars"),
        ("bar_result_query", "daily_bars"),
        ("instrument_failure_source", "instruments"),
        ("instrument_failure_operation", "instruments"),
        ("calendar_failure_operation", "trading_calendar"),
        ("calendar_failure_context", "trading_calendar"),
        ("instrument_success_item_type", "instruments"),
        ("calendar_success_item_type", "trading_calendar"),
        ("calendar_success_sz", "trading_calendar"),
        ("calendar_success_out_of_range", "trading_calendar"),
        ("calendar_success_missing", "trading_calendar"),
        ("calendar_success_duplicate", "trading_calendar"),
    ],
)
def test_tushare_probe_rejects_misattributed_typed_outcomes(
    tmp_path: Path, attack: str, category: str
) -> None:
    class AdversarialProvider(ProbedTushareProvider):
        def fetch_bars(self, query: BarQuery) -> BarResult | BarFailure:
            self.bar_calls.append(query)
            if query.period is Period.MIN60 and attack.startswith("bar_failure"):
                wrong_query = query.model_copy(update={"symbol": "000001.SH"})
                return BarFailure(
                    query=wrong_query if attack == "bar_failure_query" else query,
                    source=(
                        ProviderId.BAOSTOCK
                        if attack == "bar_failure_source"
                        else ProviderId.TUSHARE
                    ),
                    reason=FailureReason.PERMISSION_DENIED,
                    failed_start=query.start,
                    failed_end=query.end,
                    detail="provider permission was denied",
                )
            if query.period is Period.DAY and attack == "bar_result_query":
                wrong_query = query.model_copy(update={"symbol": "000001.SH"})
                return successful_bar_probe(wrong_query)
            return successful_bar_probe(query)

        def fetch_instruments(
            self,
        ) -> ProviderBatch[Instrument] | ProviderBatchFailure:
            self.instrument_calls += 1
            if attack == "instrument_failure_source":
                return ProviderBatchFailure(
                    source=ProviderId.BAOSTOCK,
                    operation=ProviderOperation.INSTRUMENTS,
                    reason=FailureReason.PERMISSION_DENIED,
                    detail="provider permission was denied",
                )
            if attack == "instrument_failure_operation":
                return ProviderBatchFailure(
                    source=ProviderId.TUSHARE,
                    operation=ProviderOperation.CALENDAR,
                    exchange=Exchange.SH,
                    start=date(2024, 1, 2),
                    end=date(2024, 1, 3),
                    reason=FailureReason.PERMISSION_DENIED,
                    detail="provider permission was denied",
                )
            if attack == "instrument_success_item_type":
                return ProviderBatch[TradingDay](
                    items=(
                        TradingDay(
                            day=date(2024, 1, 2),
                            exchange=Exchange.SH,
                            is_open=True,
                        ),
                    ),
                    provenance=DatasetProvenance(
                        source=ProviderId.TUSHARE,
                        fetched_at=FIXED_NOW,
                        data_cutoff=FIXED_NOW,
                        dataset_version="wrong-instrument-items",
                    ),
                )  # type: ignore[return-value]
            return super().fetch_instruments()

        def fetch_calendar(
            self, exchange: Exchange, start: date, end: date
        ) -> ProviderBatch[TradingDay] | ProviderBatchFailure:
            self.calendar_calls.append((exchange, start, end))
            if attack == "calendar_failure_operation":
                return ProviderBatchFailure(
                    source=ProviderId.TUSHARE,
                    operation=ProviderOperation.INSTRUMENTS,
                    reason=FailureReason.PERMISSION_DENIED,
                    detail="provider permission was denied",
                )
            if attack == "calendar_failure_context":
                return ProviderBatchFailure(
                    source=ProviderId.TUSHARE,
                    operation=ProviderOperation.CALENDAR,
                    exchange=Exchange.SZ,
                    start=start,
                    end=end,
                    reason=FailureReason.PERMISSION_DENIED,
                    detail="provider permission was denied",
                )
            if attack == "calendar_success_item_type":
                return ProviderBatch[Instrument](
                    items=(
                        Instrument(
                            symbol="600000.SH",
                            exchange=Exchange.SH,
                            name="浦发银行",
                            instrument_kind=InstrumentKind.STOCK,
                            listing_status=ListingStatus.LISTED,
                            listed_on=date(1999, 11, 10),
                        ),
                    ),
                    provenance=DatasetProvenance(
                        source=ProviderId.TUSHARE,
                        fetched_at=FIXED_NOW,
                        data_cutoff=FIXED_NOW,
                        dataset_version="wrong-calendar-items",
                    ),
                )  # type: ignore[return-value]
            if attack.startswith("calendar_success_"):
                provenance = DatasetProvenance(
                    source=ProviderId.TUSHARE,
                    fetched_at=FIXED_NOW,
                    data_cutoff=FIXED_NOW,
                    dataset_version=f"{attack}-calendar",
                )
                if attack == "calendar_success_sz":
                    items = (TradingDay(day=start, exchange=Exchange.SZ, is_open=True),)
                elif attack == "calendar_success_out_of_range":
                    items = (TradingDay(day=end, exchange=exchange, is_open=True),)
                elif attack == "calendar_success_duplicate":
                    items = (
                        TradingDay(day=start, exchange=exchange, is_open=True),
                        TradingDay(day=start, exchange=exchange, is_open=False),
                    )
                else:
                    return ProviderBatch[TradingDay].model_construct(
                        items=(), provenance=provenance
                    )
                return ProviderBatch[TradingDay](
                    items=items,
                    provenance=provenance,
                )
            return ProviderBatch[TradingDay](
                items=(TradingDay(day=start, exchange=exchange, is_open=True),),
                provenance=DatasetProvenance(
                    source=ProviderId.TUSHARE,
                    fetched_at=FIXED_NOW,
                    data_cutoff=FIXED_NOW,
                    dataset_version="probe-calendar",
                ),
            )

    provider = AdversarialProvider()
    key = Fernet.generate_key().decode("ascii")
    with settings_api(
        tmp_path,
        master_key=key,
        diagnostic_factory=lambda _source, **_context: provider,
    ) as context:
        context.client.put("/api/settings/sources/tushare", json={"token": TOKEN})
        response = context.client.post("/api/settings/sources/tushare/test")

    assert response.status_code == 200
    body = response.json()
    gap = next(item for item in body["gaps"] if item["category"] == category)
    assert gap == {
        "category": category,
        "state": "unavailable",
        "reason": "invalid_response",
        "detail": "provider response is invalid",
    }
    assert (
        next(item for item in body["permissions"] if item["category"] == category)[
            "state"
        ]
        == "unavailable"
    )
    assert all(
        item["state"] == "available"
        for item in body["permissions"]
        if item["category"] != category
    )


@pytest.mark.parametrize(
    ("provider_source", "report_source"),
    [
        (ProviderId.AKSHARE, ProviderId.BAOSTOCK),
        (ProviderId.BAOSTOCK, ProviderId.AKSHARE),
    ],
)
def test_generic_diagnostic_rejects_provider_or_report_source_mismatch(
    tmp_path: Path,
    provider_source: ProviderId,
    report_source: ProviderId,
) -> None:
    class MisboundProvider(AvailableProvider):
        def __init__(self) -> None:
            super().__init__(provider_source)

        def capabilities(self) -> CapabilityReport:
            return CapabilityReport(
                source=report_source,
                state=CapabilityState.AVAILABLE,
                capabilities=frozenset(),
                gaps=tuple(
                    CapabilityGap(
                        capability=capability,
                        state=CapabilityState.UNSUPPORTED,
                        reason=FailureReason.UNSUPPORTED,
                    )
                    for capability in MarketCapability
                ),
            )

    provider = MisboundProvider()
    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: provider,
    ) as context:
        insert_market_dataset(
            context.engine,
            suffix="9",
            source=ProviderId.AKSHARE,
            created_at=datetime(2026, 7, 6, 8, tzinfo=timezone.utc),
            data_cutoff=datetime(2026, 7, 6, 7, tzinfo=timezone.utc),
        )
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "baostock"
    assert body["status"] == "unavailable"
    assert body["fallback_reason"] == {
        "reason": "invalid_response",
        "detail": "provider response is invalid",
    }
    assert body["last_update"] is None


@pytest.mark.parametrize(
    ("state", "reason", "expected_state"),
    [
        (
            CapabilityState.PERMISSION_DENIED,
            FailureReason.PERMISSION_DENIED,
            "permission_denied",
        ),
        (CapabilityState.UNAVAILABLE, FailureReason.MISSING, "unavailable"),
    ],
)
def test_generic_nonavailable_report_maps_to_coherent_failure(
    tmp_path: Path,
    state: CapabilityState,
    reason: FailureReason,
    expected_state: str,
) -> None:
    class NonavailableProvider(AvailableProvider):
        def capabilities(self) -> CapabilityReport:
            return CapabilityReport(
                source=ProviderId.BAOSTOCK,
                state=state,
                gaps=(
                    CapabilityGap(
                        capability=MarketCapability.BARS,
                        state=state,
                        reason=reason,
                        detail="unsafe provider supplied detail",
                    ),
                ),
            )

    provider = NonavailableProvider(ProviderId.BAOSTOCK)
    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: provider,
    ) as context:
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "baostock"
    assert body["status"] == expected_state
    assert body["capabilities"] == []
    assert all(item["state"] == expected_state for item in body["permissions"])
    assert body["fallback_reason"]["reason"] == reason.value
    assert "unsafe" not in response.text


def test_diagnostic_rejects_future_provider_cutoff_without_cache_evidence(
    tmp_path: Path,
) -> None:
    future = datetime(2026, 7, 7, tzinfo=timezone.utc)

    class FutureCutoffProvider(AvailableProvider):
        def capabilities(self) -> CapabilityReport:
            return super().capabilities().model_copy(update={"data_cutoff": future})

    provider = FutureCutoffProvider(ProviderId.BAOSTOCK)
    with settings_api(
        tmp_path,
        master_key=None,
        diagnostic_factory=lambda _source, **_context: provider,
    ) as context:
        response = context.client.post("/api/settings/sources/baostock/test")

    assert response.status_code == 503
    assert response.json() == {"code": "settings_storage_unavailable"}


def test_provider_factory_dynamic_nonpropagating_handler_and_close_are_redacted(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    unsafe_path = str((tmp_path / f"vipdoc-{TOKEN}").resolve())
    third_party = logging.getLogger("third_party.provider.settings_test")
    stream = io.StringIO()
    handlers: list[logging.Handler] = []
    original_factory = logging.getLogRecordFactory()

    class LoggingProvider(PartialTushareProvider):
        def close(self) -> None:
            third_party.warning("close token=%s path=%s", TOKEN, unsafe_path)
            super().close()

    provider = LoggingProvider()

    def factory(_source: ProviderId, **context: object) -> object:
        logging.setLogRecordFactory(
            lambda *args, **kwargs: original_factory(*args, **kwargs)
        )
        handler = logging.StreamHandler(stream)
        handlers.append(handler)
        third_party.addHandler(handler)
        third_party.propagate = False
        third_party.setLevel(logging.WARNING)
        third_party.warning(
            "factory token=%s path=%s", context.get("token"), context.get("tdx_path")
        )
        return provider

    try:
        with settings_api(
            tmp_path, master_key=key, diagnostic_factory=factory
        ) as context:
            context.client.put(
                "/api/settings/sources",
                json={"priorities": DEFAULT_PRIORITIES, "tdx_path": unsafe_path},
            )
            context.client.put("/api/settings/sources/tushare", json={"token": TOKEN})
            response = context.client.post("/api/settings/sources/tushare/test")

        assert response.status_code == 200
        assert provider.closed is True
        assert handlers
        assert all(handler.filters == [] for handler in handlers)
        assert TOKEN not in stream.getvalue()
        assert unsafe_path not in stream.getvalue()
    finally:
        logging.setLogRecordFactory(original_factory)
        for handler in handlers:
            third_party.removeHandler(handler)
            handler.close()


def test_service_secret_lease_retains_only_current_and_previous_values(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    first_token = "first-current-token"
    second_token = "second-current-token"
    third_token = "third-current-token"
    first_path = str((tmp_path / "first-current-vipdoc").resolve())
    second_path = str((tmp_path / "second-current-vipdoc").resolve())
    third_path = str((tmp_path / "third-current-vipdoc").resolve())
    for path in (first_path, second_path, third_path):
        make_valid_tdx_root(Path(path))
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    background = logging.getLogger("third_party.provider.background")
    background.handlers.clear()
    background.propagate = False
    background.setLevel(logging.WARNING)
    background.addHandler(handler)

    try:
        with settings_api(
            tmp_path,
            master_key=key,
            diagnostic_factory=lambda _source, **_context: PartialTushareProvider(),
        ) as context:
            context.client.put(
                "/api/settings/sources",
                json={"priorities": DEFAULT_PRIORITIES, "tdx_path": first_path},
            )
            context.client.put(
                "/api/settings/sources/tushare", json={"token": first_token}
            )
            context.client.post("/api/settings/sources/tushare/test")
            background.warning("after scope token=%s path=%s", first_token, first_path)
            assert first_token not in output.getvalue()
            assert first_path not in output.getvalue()

            context.client.put(
                "/api/settings/sources",
                json={"priorities": DEFAULT_PRIORITIES, "tdx_path": second_path},
            )
            context.client.put(
                "/api/settings/sources/tushare", json={"token": second_token}
            )
            output.seek(0)
            output.truncate(0)
            background.warning(
                "replaced old=%s %s new=%s %s",
                first_token,
                first_path,
                second_token,
                second_path,
            )
            assert first_token not in output.getvalue()
            assert first_path not in output.getvalue()
            assert second_token not in output.getvalue()
            assert second_path not in output.getvalue()

            context.client.put(
                "/api/settings/sources",
                json={"priorities": DEFAULT_PRIORITIES, "tdx_path": third_path},
            )
            context.client.put(
                "/api/settings/sources/tushare", json={"token": third_token}
            )
            output.seek(0)
            output.truncate(0)
            background.warning(
                "rotated first=%s %s second=%s %s third=%s %s",
                first_token,
                first_path,
                second_token,
                second_path,
                third_token,
                third_path,
            )
            assert first_token in output.getvalue()
            assert first_path in output.getvalue()
            assert second_token not in output.getvalue()
            assert second_path not in output.getvalue()
            assert third_token not in output.getvalue()
            assert third_path not in output.getvalue()
        output.seek(0)
        output.truncate(0)
        background.warning(
            "closed second=%s %s third=%s %s",
            second_token,
            second_path,
            third_token,
            third_path,
        )
        assert second_token in output.getvalue()
        assert second_path in output.getvalue()
        assert third_token in output.getvalue()
        assert third_path in output.getvalue()
    finally:
        background.removeHandler(handler)
        handler.close()


def test_malicious_diagnostic_exception_and_path_never_escape(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = Fernet.generate_key().decode("ascii")
    unsafe_path = str(tmp_path / f"vipdoc-{TOKEN}")

    def factory(_source: ProviderId, **_context: object) -> object:
        return MaliciousProvider(unsafe_path)

    with settings_api(tmp_path, master_key=key, diagnostic_factory=factory) as context:
        context.client.put("/api/settings/sources/tushare", json={"token": TOKEN})
        with caplog.at_level(logging.WARNING, logger="stock_desk.market.diagnostics"):
            response = context.client.post("/api/settings/sources/tushare/test")

    assert response.status_code == 200
    assert response.json()["fallback_reason"] == {
        "reason": "provider_unavailable",
        "detail": "provider diagnostic failed safely",
    }
    rendered_logs = caplog.text
    for rendered in (response.text, rendered_logs):
        assert TOKEN not in rendered
        assert unsafe_path not in rendered


def test_tdx_save_uses_secure_preflight_without_exposing_missing_path(
    tmp_path: Path,
) -> None:
    missing_vipdoc = (tmp_path / f"missing-{TOKEN}").resolve()
    body = {
        "priorities": DEFAULT_PRIORITIES,
        "tdx_path": str(missing_vipdoc),
    }
    with settings_api(tmp_path, master_key=None) as context:
        response = context.client.put("/api/settings/sources", json=body)
        persisted = context.client.get("/api/settings/sources")

    assert response.status_code == 422
    assert response.json() == {"code": "tdx_preflight_failed"}
    assert persisted.json()["tdx_path"] is None
    assert str(missing_vipdoc) not in response.text
    assert TOKEN not in response.text


@pytest.mark.skipif(os.name != "posix", reason="POSIX ancestor symlink security")
def test_tdx_save_rejects_symlinked_ancestor_without_exposing_path(
    tmp_path: Path,
) -> None:
    actual_parent = tmp_path / "actual-parent"
    root = actual_parent / "vipdoc"
    for market in ("sh", "sz"):
        (root / market / "lday").mkdir(parents=True)
    (root / "sh" / "lday" / "sh600000.day").write_bytes(b"x" * 32)
    alias = tmp_path / f"alias-{TOKEN}"
    alias.symlink_to(actual_parent, target_is_directory=True)
    configured = alias / "vipdoc"
    body = {"priorities": DEFAULT_PRIORITIES, "tdx_path": str(configured)}

    with settings_api(tmp_path, master_key=None) as context:
        response = context.client.put("/api/settings/sources", json=body)
        persisted = context.client.get("/api/settings/sources")

    assert response.status_code == 422
    assert response.json() == {"code": "tdx_preflight_failed"}
    assert persisted.json()["tdx_path"] is None
    assert str(configured) not in response.text
    assert TOKEN not in response.text


def test_openapi_marks_token_write_only_without_secret_examples(tmp_path: Path) -> None:
    with settings_api(tmp_path, master_key=None) as context:
        document = context.client.get("/openapi.json").json()

    rendered = json.dumps(document, ensure_ascii=False)
    assert TOKEN not in rendered
    token_schema = document["components"]["schemas"]["TushareSourceUpdateRequest"][
        "properties"
    ]["token"]
    assert token_schema["writeOnly"] is True
    assert token_schema["anyOf"][0]["format"] == "password"
    assert token_schema["anyOf"][0]["writeOnly"] is True


def test_owned_settings_services_initialize_once_lazily_and_close_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'owned-settings.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    settings = Settings(database_url=database_url, data_dir=tmp_path)
    owned = SourceSettingsServices(engine=engine, settings=settings)
    real_close = owned.close
    opens: list[str] = []
    closes: list[bool] = []
    barrier = threading.Barrier(2)

    def counted_open(
        cls: type[SourceSettingsServices], **_kwargs: object
    ) -> SourceSettingsServices:
        opens.append(cls.__name__)
        return owned

    monkeypatch.setattr(SourceSettingsServices, "open", classmethod(counted_open))
    monkeypatch.setattr(owned, "close", lambda: closes.append(True))
    application = create_app(settings)
    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200
        assert opens == []

        def request_settings() -> int:
            barrier.wait(timeout=5)
            return client.get("/api/settings/sources").status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = tuple(executor.map(lambda _index: request_settings(), range(2)))

    assert statuses == (200, 200)
    assert opens == ["SourceSettingsServices"]
    assert closes == [True]
    real_close()
    engine.dispose()


@pytest.mark.parametrize("failure_mode", ["missing_read_only", "directory"])
def test_lazy_settings_open_storage_failures_are_fixed_json_503(
    tmp_path: Path, failure_mode: str
) -> None:
    if failure_mode == "missing_read_only":
        database_url = f"sqlite:///file:{tmp_path / 'missing.db'}?mode=ro&uri=true"
    else:
        database_directory = tmp_path / "database-directory"
        database_directory.mkdir()
        database_url = f"sqlite:///{database_directory}"
    settings = Settings(database_url=database_url, data_dir=tmp_path)

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        responses = (
            client.get("/api/settings/sources"),
            client.put(
                "/api/settings/sources",
                json={"priorities": DEFAULT_PRIORITIES, "tdx_path": None},
            ),
            client.post("/api/settings/sources/baostock/test"),
        )

    for response in responses:
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"code": "settings_storage_unavailable"}


def test_open_maps_identity_file_error_and_disposes_partial_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenEngine:
        disposed = False

        def connect(self) -> object:
            raise OSError("unsafe filesystem detail")

        def dispose(self) -> None:
            self.disposed = True

    engine = BrokenEngine()
    monkeypatch.setattr(settings_module, "migrate", lambda _url: None)
    monkeypatch.setattr(settings_module, "create_engine_for_url", lambda _url: engine)

    with pytest.raises(SourceSettingsStorageError):
        SourceSettingsServices.open(
            database_url=f"sqlite:///{tmp_path / 'unreachable.db'}",
            settings=Settings(data_dir=tmp_path),
        )

    assert engine.disposed is True


def test_injected_settings_services_are_not_closed_by_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'injected-settings.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    settings = Settings(database_url=database_url, data_dir=tmp_path)
    injected = SourceSettingsServices(engine=engine, settings=settings)
    real_close = injected.close
    closes: list[bool] = []
    monkeypatch.setattr(injected, "close", lambda: closes.append(True))
    try:
        with TestClient(
            create_app(settings, source_settings_services=injected)
        ) as client:
            assert client.get("/api/settings/sources").status_code == 200
        assert closes == []
        injected.close()
        assert closes == [True]
    finally:
        real_close()
        engine.dispose()
