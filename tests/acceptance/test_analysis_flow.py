from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import cast

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import JsonValue, SecretStr

from stock_desk.analysis.data_service import (
    ResearchDataService,
    ResearchLoadDiagnostic,
    ResearchSourceCandidate,
)
from stock_desk.analysis.evidence import EvidenceGraph, EvidenceItem
from stock_desk.analysis.model_catalog import AnalysisModelCatalog
from stock_desk.analysis.model_config import AnalysisModelPublicConfig
from stock_desk.analysis.model_settings import (
    ModelProviderFactory,
    ModelSettingsService,
)
from stock_desk.analysis.providers.base import (
    ModelAuthenticationError,
    ModelConnectionResult,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from stock_desk.analysis.repository import AnalysisExecutionConfig, AnalysisRepository
from stock_desk.analysis.roles import RoleName
from stock_desk.analysis.runtime import AnalysisPreflightService
from stock_desk.analysis.service import AnalysisService
from stock_desk.analysis.snapshot import (
    ResearchMissingReason,
    ResearchSection,
    ResearchSectionKind,
)
from stock_desk.analysis.sources.market_cache import MarketCacheLoader
from stock_desk.analysis.sources.routing import ResearchSourceRouter
from stock_desk.analysis.sources.tushare import TushareResearchSource
from stock_desk.analysis.worker import AnalysisWorkerHandler
from stock_desk.config import Settings
from stock_desk.desktop_session import DesktopSession
from stock_desk.main import create_app
from stock_desk.market.providers.base import ProviderPermissionDenied
from stock_desk.market.types import Adjustment, Period, ProviderId
from stock_desk.security.secrets import SecretStore
from stock_desk.storage.database import create_engine_for_url, migrate
from stock_desk.tasks.repository import TaskRepository
from stock_desk.tasks.worker import TaskWorker
from tests.contract.analysis.test_research_source_contract import StubSource, section
from tests.integration.analysis.test_partial_report import frozen_snapshot
from tests.integration.analysis.test_research_data_service import FakeMarketLake
from tests.integration.market.lake_test_helpers import routed_daily_bars


SYMBOL = "600000.SH"
PLAINTEXT_KEY = "sk-acceptance-plaintext-must-never-escape"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _request_role(request: ModelRequest) -> RoleName:
    return RoleName(cast(str, request.data_blocks[0]["role"]))


def _valid_content(request: ModelRequest) -> dict[str, JsonValue]:
    role = _request_role(request)
    context = request.data_blocks[0]
    evidence_ids = cast(list[str], context["allowed_evidence_ids"])
    content: dict[str, JsonValue] = {
        "role": role.value,
        "snapshot_id": cast(str, context["snapshot_id"]),
        "summary": f"{role.value} deterministic summary",
        "claims": [
            {
                "text": f"{role.value} traceable claim",
                "evidence_ids": [evidence_ids[0]],
                "stance": "support",
            }
        ],
    }
    if role is RoleName.RISK_DECISION:
        content["proposal"] = {
            "rating": "neutral",
            "confidence": 0.66,
            "confidence_explanation": "Deterministic evidence covers both sides.",
        }
    return content


class DeterministicProvider:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        failures: dict[RoleName, list[Exception]] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.failures = failures or {}
        self.calls: Counter[RoleName] = Counter()

    async def test_connection(
        self, *, timeout_seconds: float = 10.0
    ) -> ModelConnectionResult:
        del timeout_seconds
        return ModelConnectionResult(
            connected=True,
            provider=self.provider,
            model=self.model,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        role = _request_role(request)
        self.calls[role] += 1
        scripted = self.failures.get(role, [])
        if scripted:
            raise scripted.pop(0)
        return ModelResponse(
            provider=self.provider,
            model=self.model,
            content=_valid_content(request),
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )


class ConnectionProviderFactory:
    def create(self, config: AnalysisModelPublicConfig) -> ModelProvider:
        return cast(
            ModelProvider,
            DeterministicProvider(
                provider=config.provider.value,
                model=config.model,
            ),
        )


class RoutedLoader:
    def __init__(self, section: ResearchSection) -> None:
        self.kind = section.kind
        self._section = section

    def load(self, _symbol: str) -> ResearchSection:
        return self._section

    def load_with_diagnostics(
        self, _symbol: str
    ) -> tuple[ResearchSection, ResearchLoadDiagnostic]:
        if self.kind is ResearchSectionKind.MARKET:
            route = "market_cache"
            actual = self._section.canonical_source
            candidates = (
                ResearchSourceCandidate(
                    source=route,
                    position=0,
                    supported=True,
                    configured=True,
                    outcome="selected",
                ),
            )
        else:
            route = "tushare"
            actual = "akshare"
            candidates = (
                ResearchSourceCandidate(
                    source="tushare",
                    position=0,
                    supported=True,
                    configured=True,
                    outcome="failed",
                    failure_reason=ResearchMissingReason.PERMISSION_DENIED,
                ),
                ResearchSourceCandidate(
                    source="akshare",
                    position=1,
                    supported=True,
                    configured=True,
                    outcome="selected",
                ),
            )
        return self._section, ResearchLoadDiagnostic(
            kind=self.kind,
            route_source=route,
            actual_source=actual,
            attempted_sources=tuple(
                item.source
                for item in candidates
                if item.outcome in {"failed", "selected"}
            ),
            ordered_candidates=candidates,
        )


class BoundDataFactory:
    def __init__(self, service: ResearchDataService, identity: object) -> None:
        self._service = service
        self.database_identity = identity

    def __call__(self) -> ResearchDataService:
        return self._service


@dataclass
class AnalysisHarness:
    client: TestClient
    worker: TaskWorker
    repository: AnalysisRepository
    tasks: TaskRepository
    engine: object
    provider_factory: Callable[[AnalysisExecutionConfig], ModelProvider]

    def run_worker(self) -> object:
        completed = self.worker.run_once()
        assert completed is not None
        return completed


def _data_service(*, omit: ResearchSectionKind | None = None) -> ResearchDataService:
    snapshot = frozen_snapshot()
    return ResearchDataService(
        loaders=tuple(
            RoutedLoader(section)
            for section in snapshot.sections
            if section.kind is not omit
        ),
        clock=_utc_now,
    )


def _empty_fundamentals_service() -> ResearchDataService:
    class EmptyTushareClient:
        def income(self, **_kwargs: object) -> object:
            return []

        def anns_d(self, **_kwargs: object) -> object:
            raise AssertionError("fundamentals route must not request announcements")

    snapshot = frozen_snapshot()
    successful_loaders = tuple(
        RoutedLoader(section)
        for section in snapshot.sections
        if section.kind is not ResearchSectionKind.FUNDAMENTALS
    )
    fundamentals = ResearchSourceRouter(
        kind=ResearchSectionKind.FUNDAMENTALS,
        priority=(ProviderId.TUSHARE,),
        sources=(TushareResearchSource(client=EmptyTushareClient(), clock=_utc_now),),
    )
    return ResearchDataService(
        loaders=(*successful_loaders, fundamentals),
        clock=_utc_now,
    )


@contextmanager
def _harness(
    tmp_path: Path,
    *,
    data_service: ResearchDataService | None = None,
    provider_builder: Callable[[AnalysisExecutionConfig], ModelProvider] | None = None,
    desktop_session: DesktopSession | None = None,
    provider_builder_factory: Callable[
        [SecretStore],
        tuple[
            Callable[[AnalysisExecutionConfig], ModelProvider],
            Callable[[], None],
        ],
    ]
    | None = None,
) -> Iterator[AnalysisHarness]:
    if provider_builder is not None and provider_builder_factory is not None:
        raise ValueError("only one analysis provider builder may be configured")
    database_url = f"sqlite:///{tmp_path / 'analysis-acceptance.db'}"
    migrate(database_url)
    engine = create_engine_for_url(database_url)
    tasks = TaskRepository(engine)
    repository = AnalysisRepository(engine)
    catalog = AnalysisModelCatalog(engine, owns_engine=False)
    settings = Settings(
        database_url=database_url,
        data_dir=tmp_path,
        master_key=SecretStr(Fernet.generate_key().decode("ascii")),
    )
    secret_store = SecretStore(
        engine,
        settings,
        expected_database_identity=tasks.database_identity,
    )
    model_settings = ModelSettingsService(
        catalog=catalog,
        secret_store=secret_store,
        provider_factory=cast(ModelProviderFactory, ConnectionProviderFactory()),
    )
    analysis = AnalysisService(
        repository=repository,
        tasks=tasks,
        model_catalog=catalog,
        execution_resolver=model_settings.require_verified_execution_in_transaction,
        clock=_utc_now,
    )
    research = data_service or _data_service()
    bound_data = BoundDataFactory(research, tasks.database_identity)
    preflight = AnalysisPreflightService(
        data_service_factory=bound_data,
        clock=_utc_now,
    )

    def default_provider_builder(execution: AnalysisExecutionConfig) -> ModelProvider:
        return cast(
            ModelProvider,
            DeterministicProvider(
                provider=execution.provider,
                model=execution.model,
            ),
        )

    owned_provider_close: Callable[[], None] | None = None
    if provider_builder_factory is not None:
        resolved_builder, owned_provider_close = provider_builder_factory(secret_store)
    else:
        resolved_builder = provider_builder or default_provider_builder
    worker = TaskWorker(tasks, worker_id="analysis-acceptance-worker")

    async def no_wait(_delay: float) -> None:
        await asyncio.sleep(0)

    worker.register_claimed(
        "analysis.run",
        AnalysisWorkerHandler(
            repository=repository,
            provider_factory=resolved_builder,
            data_service_factory=bound_data,
            evidence_factory=lambda snapshot: EvidenceGraph(
                snapshot=snapshot,
                evidence_items=tuple(
                    EvidenceItem.create(
                        snapshot=snapshot,
                        section_kind=section.kind,
                        excerpt=f"deterministic {section.kind.value} evidence",
                    )
                    for section in snapshot.sections
                ),
                claims=(),
            ),
            sleeper=no_wait,
            clock=_utc_now,
        ),
    )
    app = create_app(
        settings,
        task_repository=tasks,
        model_settings_service=model_settings,
        analysis_service=analysis,
        analysis_preflight_service=preflight,
        desktop_session=desktop_session,
    )
    try:
        with TestClient(app) as client:
            if desktop_session is not None:
                client.headers.update(
                    {
                        "Origin": desktop_session.origin,
                        "Authorization": (
                            f"Bearer {desktop_session.secret_for_host()}"
                        ),
                    }
                )
            yield AnalysisHarness(
                client=client,
                worker=worker,
                repository=repository,
                tasks=tasks,
                engine=engine,
                provider_factory=resolved_builder,
            )
    finally:
        if owned_provider_close is not None:
            owned_provider_close()
        model_settings.close()
        catalog.close()
        engine.dispose()


def _configure_verified_model(
    client: TestClient,
    *,
    provider: str = "deepseek",
    base_url: str = "https://api.deepseek.com",
    model_name: str = "deepseek-chat",
    api_key: str = PLAINTEXT_KEY,
) -> dict[str, object]:
    created = client.post(
        "/api/settings/models",
        json={
            "display_name": "Acceptance DeepSeek",
            "provider": provider,
            "base_url": base_url,
            "model": model_name,
            "api_key": api_key,
            "temperature": 0.1,
            "timeout": 30.0,
            "max_output": 2048,
        },
    )
    assert created.status_code == 201
    model = created.json()
    assert model["api_key_configured"] is True
    assert model["masked_api_key"] != api_key
    assert api_key not in created.text
    tested = client.post(
        f"/api/settings/models/{model['id']}/test",
        json={"expected_revision": model["revision"]},
    )
    assert tested.status_code == 200
    assert tested.json()["status"] == "verified"
    model["revision"] = tested.json()["revision"]
    return model


def _submit(
    client: TestClient, model_id: str, *, retries: int = 1
) -> dict[str, object]:
    response = client.post(
        "/api/analysis",
        json={
            "symbol": SYMBOL,
            "model_config_id": model_id,
            "retry": {"max_retries": retries},
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["snapshot_id"] is None
    return body


def test_full_stubbed_analysis_is_traceable_and_history_is_immutable(
    tmp_path: Path,
) -> None:
    with _harness(tmp_path) as harness:
        model = _configure_verified_model(harness.client)
        preflight = harness.client.post(
            "/api/analysis/preflight", json={"symbol": SYMBOL}
        )
        assert preflight.status_code == 200
        categories = preflight.json()["categories"]
        assert [item["kind"] for item in categories] == [
            "market",
            "fundamentals",
            "announcements",
            "news",
        ]
        assert categories[0]["route_source"] == "market_cache"
        assert all(item["actual_source"] is not None for item in categories)
        assert all(item["connection_state"] == "degraded" for item in categories[1:])

        submitted = _submit(harness.client, cast(str, model["id"]))
        run_id = cast(str, submitted["run_id"])
        completed = harness.run_worker()
        assert getattr(completed, "status") == "succeeded"

        detail_response = harness.client.get(f"/api/analysis/{run_id}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["status"] == "succeeded"
        assert detail["snapshot_id"].startswith("sha256:")
        assert [item["ordinal"] for item in detail["stages"]] == list(range(-4, 5))
        assert [item["stage"] for item in detail["stages"]] == [
            "market",
            "fundamentals",
            "announcements",
            "news",
            "technical",
            "fundamental_news",
            "bull",
            "bear",
            "risk_decision",
        ]
        assert all(item["status"] == "succeeded" for item in detail["stages"])

        report_response = harness.client.get(f"/api/analysis/{run_id}/report")
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["status"] == "complete"
        assert report["rating"] in {
            "strong_bullish",
            "bullish",
            "neutral",
            "bearish",
            "strong_bearish",
        }
        claims = [
            *report["core_judgments"],
            *report["bull_claims"],
            *report["bear_claims"],
            *report["risks"],
        ]
        assert claims and all(claim["evidence_ids"] for claim in claims)
        assert report["snapshot_id"] == detail["snapshot_id"]
        assert all(
            item["model"] == "deepseek-chat" for item in report["model_metadata"]
        )
        assert all(item["template_version"] for item in report["model_metadata"])
        assert "不构成投资建议" in report["disclaimer"]

        evidence_id = report["core_judgments"][0]["evidence_ids"][0]
        evidence_response = harness.client.get(
            f"/api/analysis/{run_id}/evidence/{evidence_id}"
        )
        assert evidence_response.status_code == 200
        evidence = evidence_response.json()
        assert evidence["evidence_id"] == evidence_id
        assert evidence["snapshot_id"] == report["snapshot_id"]
        assert evidence["source_url"].startswith("https://")
        assert evidence["data_cutoff"] and evidence["fetched_at"]

        persisted_before = json.dumps(report, ensure_ascii=False, sort_keys=True)
        history = harness.client.get("/api/analysis", params={"symbol": SYMBOL})
        assert history.status_code == 200
        assert history.json()["items"][0]["report_id"] == report["report_id"]
        persisted_after = harness.client.get(f"/api/analysis/{run_id}/report")
        assert (
            json.dumps(persisted_after.json(), ensure_ascii=False, sort_keys=True)
            == persisted_before
        )

        with harness.engine.connect() as connection:
            database_text = " ".join(
                str(value)
                for row in connection.exec_driver_sql(
                    "SELECT key, encrypted_value FROM app_setting"
                )
                for value in row
            )
        assert PLAINTEXT_KEY not in database_text


def test_partial_retry_insufficient_evidence_and_cancel_are_durable(
    tmp_path: Path,
) -> None:
    def partial_provider(execution: AnalysisExecutionConfig) -> ModelProvider:
        return cast(
            ModelProvider,
            DeterministicProvider(
                provider=execution.provider,
                model=execution.model,
                failures={
                    RoleName.BULL: [ModelAuthenticationError("unsafe-provider-detail")]
                },
            ),
        )

    with _harness(tmp_path / "partial", provider_builder=partial_provider) as harness:
        model = _configure_verified_model(harness.client)
        submitted = _submit(harness.client, cast(str, model["id"]), retries=0)
        parent_id = cast(str, submitted["run_id"])
        harness.run_worker()
        parent_report_response = harness.client.get(f"/api/analysis/{parent_id}/report")
        assert parent_report_response.status_code == 200
        parent_report = parent_report_response.json()
        assert parent_report["status"] == "partial"
        assert parent_report["rating"] is None
        assert parent_report["failed_modules"] == ["bull"]
        assert parent_report["blocked_modules"] == ["risk_decision"]
        parent_bytes = parent_report_response.content

        retry = harness.client.post(f"/api/analysis/{parent_id}/stages/bull/retry")
        assert retry.status_code == 202
        child = retry.json()
        assert child["parent_run_id"] == parent_id
        assert child["requested_stage"] == "bull"
        assert child["snapshot_id"] == parent_report["snapshot_id"]

        harness.worker.register_claimed(
            "analysis.run",
            AnalysisWorkerHandler(
                repository=harness.repository,
                provider_factory=lambda execution: cast(
                    ModelProvider,
                    DeterministicProvider(
                        provider=execution.provider,
                        model=execution.model,
                    ),
                ),
                data_service_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("retry must reuse frozen inputs")
                ),
                evidence_factory=lambda _snapshot: (_ for _ in ()).throw(
                    AssertionError("retry must reuse frozen evidence")
                ),
                sleeper=lambda _delay: asyncio.sleep(0),
                clock=_utc_now,
            ),
        )
        harness.run_worker()
        child_detail = harness.client.get(f"/api/analysis/{child['run_id']}").json()
        assert child_detail["status"] == "succeeded"
        assert any(item["status"] == "reused" for item in child_detail["stages"])
        assert (
            harness.client.get(f"/api/analysis/{parent_id}/report").content
            == parent_bytes
        )

        queued = _submit(harness.client, cast(str, model["id"]), retries=0)
        cancelled = harness.client.post(f"/api/analysis/{queued['run_id']}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "cancelled"
        assert harness.client.get(
            f"/api/analysis/{queued['run_id']}/report"
        ).json() == {"code": "report_unavailable"}


def test_empty_tushare_fundamentals_becomes_insufficient_without_rating(
    tmp_path: Path,
) -> None:
    with _harness(
        tmp_path / "insufficient",
        data_service=_empty_fundamentals_service(),
    ) as harness:
        model = _configure_verified_model(harness.client)
        preflight = harness.client.post(
            "/api/analysis/preflight", json={"symbol": SYMBOL}
        )
        assert preflight.status_code == 200
        fundamentals = next(
            item
            for item in preflight.json()["categories"]
            if item["kind"] == "fundamentals"
        )
        assert fundamentals["connection_state"] == "missing"
        assert fundamentals["missing_reason"] == "no_data"
        assert preflight.json()["rating_eligible"] is False
        submitted = _submit(harness.client, cast(str, model["id"]), retries=0)
        harness.run_worker()
        report = harness.client.get(
            f"/api/analysis/{submitted['run_id']}/report"
        ).json()
        assert report["status"] == "insufficient_evidence"
        assert report["rating"] is None
        assert report["missing_sections"] == ["fundamentals"]
        assert report["recovery_actions"]


def test_research_routing_is_cache_only_category_safe_and_never_merges() -> None:
    routed = routed_daily_bars(
        (date(2025, 7, 3), date(2025, 7, 4)),
        symbol=SYMBOL,
        adjustment=Adjustment.QFQ,
    )
    lake = FakeMarketLake(routed)

    cached = MarketCacheLoader(lake=lake).load(SYMBOL)

    assert lake.calls == [(SYMBOL, Period.DAY, Adjustment.QFQ)]
    assert cached.canonical_source == routed.result.provenance.source.value

    tushare = StubSource(
        ProviderId.TUSHARE,
        {
            ResearchSectionKind.FUNDAMENTALS: ProviderPermissionDenied(
                "unsafe-token-detail"
            )
        },
    )
    akshare = StubSource(
        ProviderId.AKSHARE,
        {
            ResearchSectionKind.FUNDAMENTALS: section(
                ProviderId.AKSHARE,
                ResearchSectionKind.FUNDAMENTALS,
                marker="akshare-only",
            )
        },
    )
    router = ResearchSourceRouter(
        kind=ResearchSectionKind.FUNDAMENTALS,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        sources=(tushare, akshare),
    )

    fallback, diagnostic = router.load_with_diagnostics(SYMBOL)

    assert fallback.canonical_source == "akshare"
    assert fallback.content == {"items": [{"marker": "akshare-only"}]}
    assert fallback.route is not None
    assert fallback.route.attempted_sources == ("tushare",)
    assert diagnostic.attempted_sources == ("tushare", "akshare")
    assert [candidate.outcome for candidate in diagnostic.ordered_candidates] == [
        "failed",
        "selected",
    ]

    unsupported = StubSource(
        ProviderId.TUSHARE,
        {ResearchSectionKind.NEWS: AssertionError("must not be called")},
    )
    news = StubSource(
        ProviderId.AKSHARE,
        {
            ResearchSectionKind.NEWS: section(
                ProviderId.AKSHARE,
                ResearchSectionKind.NEWS,
            )
        },
    )
    loaded_news = ResearchSourceRouter(
        kind=ResearchSectionKind.NEWS,
        priority=(ProviderId.TUSHARE, ProviderId.AKSHARE),
        sources=(unsupported, news),
    ).load(SYMBOL)
    assert loaded_news.canonical_source == "akshare"
    assert unsupported.calls == []
