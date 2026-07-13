"""Representative authenticated sidecar vertical slice for desktop workflows.

The day/week/60-minute, single/pool, and MACD/custom semantic matrix remains
covered by the existing domain acceptance tests. This module does not claim
installed Windows/WebView evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any, Iterator, cast

from fastapi.testclient import TestClient

from scripts.seed_demo_data import load_demo_fixture, seed_demo_data
from stock_desk.analysis.providers.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from stock_desk.analysis.repository import AnalysisExecutionConfig
from stock_desk.analysis.roles import RoleName
from stock_desk.config import Settings
from stock_desk.desktop_session import DesktopSession, TAURI_WINDOWS_ORIGIN
from stock_desk.main import create_app
from stock_desk.market.worker_runtime import ProductionMarketWorker


SOURCE_REVISION = "d" * 40
SESSION_SECRET = "desktop-core-workflow-session-secret-never-exposed"
MACD_SOURCE = (
    "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"
    "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
)
CUSTOM_SOURCE = (
    "FAST:EMA(C,3);SLOW:EMA(C,7);BUY:CROSS(FAST,SLOW);SELL:CROSS(SLOW,FAST);"
)
SYMBOL = "600000.SH"


class _DeterministicModel:
    def __init__(self, execution: AnalysisExecutionConfig) -> None:
        self.provider = execution.provider
        self.model = execution.model

    async def complete(self, request: ModelRequest) -> ModelResponse:
        context = request.data_blocks[0]
        role = RoleName(cast(str, context["role"]))
        evidence_ids = cast(list[str], context["allowed_evidence_ids"])
        content: dict[str, object] = {
            "role": role.value,
            "snapshot_id": context["snapshot_id"],
            "summary": f"{role.value} deterministic desktop summary",
            "claims": [
                {
                    "text": f"{role.value} deterministic evidence observation",
                    "evidence_ids": [evidence_ids[0]],
                    "stance": "support",
                }
            ],
        }
        if role is RoleName.RISK_DECISION:
            content["proposal"] = {
                "rating": "neutral",
                "confidence": 0.5,
                "confidence_explanation": "Deterministic evidence is balanced.",
            }
        return ModelResponse(
            provider=self.provider,
            model=self.model,
            content=cast(Any, content),
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )


def _session() -> DesktopSession:
    return DesktopSession(
        origin=TAURI_WINDOWS_ORIGIN,
        secret=SESSION_SECRET,
        host_version="1.1.0",
        frontend_version="1.1.0",
        sidecar_version="1.1.0",
        source_revision=SOURCE_REVISION,
    )


def _headers(session: DesktopSession) -> dict[str, str]:
    return {
        "Origin": session.origin,
        "Authorization": f"Bearer {session.secret_for_host()}",
    }


@contextmanager
def _desktop_journey(
    tmp_path: Path,
    *,
    authorized_by_default: bool = True,
) -> Iterator[tuple[TestClient, ProductionMarketWorker, DesktopSession]]:
    destination = tmp_path / "desktop-core-profile"
    seed_demo_data(destination)
    settings = Settings(
        database_url=f"sqlite:///{destination / 'stock-desk.db'}",
        data_dir=destination,
    )
    fixture = load_demo_fixture()
    session = _session()
    worker = ProductionMarketWorker.open(
        settings,
        worker_id="desktop-core-workflows",
        analysis_provider_factory=lambda execution: cast(
            ModelProvider, _DeterministicModel(execution)
        ),
        analysis_data_service_factory=fixture.research_data_service,
    )
    try:
        with TestClient(create_app(settings, desktop_session=session)) as client:
            if authorized_by_default:
                client.headers.update(_headers(session))
            yield client, worker, session
    finally:
        worker.close()


def _formula(client: TestClient, *, name: str, source: str) -> dict[str, object]:
    response = client.post(
        "/api/formulas",
        json={
            "name": name,
            "formula_type": "trading",
            "placement": "subchart",
            "source": source,
            "parameter_schema": {},
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def _backtest_request(
    version_id: str, *, scope: dict[str, object]
) -> dict[str, object]:
    fixture = load_demo_fixture()
    return {
        "scope": scope,
        "formula_version_id": version_id,
        "formula_parameters": {},
        "period": "1d",
        "adjustment": "none",
        "scoring_start": fixture.scoring_start,
        "scoring_end": fixture.scoring_end,
        "quantity_shares": 1000,
        "commission_bps": "2.5",
        "minimum_commission": "5",
        "sell_tax_bps": "5",
        "slippage_bps": "1",
    }


def _run_backtest(
    client: TestClient,
    worker: ProductionMarketWorker,
    version_id: str,
    *,
    scope: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    request = _backtest_request(version_id, scope=scope)
    preflight = client.post("/api/backtests/preflight", json=request)
    submitted = client.post("/api/backtests", json=request)
    assert preflight.status_code == 200
    assert submitted.status_code == 202
    completed = worker.run_once()
    assert completed is not None and completed.status == "succeeded"
    report = client.get(f"/api/backtests/{submitted.json()['run_id']}/report")
    assert report.status_code == 200
    return cast(dict[str, object], preflight.json()), cast(
        dict[str, object], report.json()
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested_key for nested in value.values() for nested_key in _all_keys(nested)
        }
    if isinstance(value, list):
        return {nested_key for nested in value for nested_key in _all_keys(nested)}
    return set()


def test_desktop_authority_protects_each_core_workflow_before_dependencies_run(
    tmp_path: Path,
) -> None:
    with _desktop_journey(tmp_path, authorized_by_default=False) as (
        client,
        _worker,
        session,
    ):
        assert "origin" not in client.headers
        assert "authorization" not in client.headers
        endpoints = (
            ("POST", "/api/backtests/preflight"),
            ("POST", "/api/analysis"),
            ("GET", "/api/tasks?view=safe&limit=100"),
        )
        authority_cases = (
            (
                "missing-origin",
                {"Authorization": f"Bearer {session.secret_for_host()}"},
                403,
                "desktop_origin_forbidden",
            ),
            (
                "wrong-origin",
                {
                    "Origin": "http://evil.invalid",
                    "Authorization": f"Bearer {session.secret_for_host()}",
                },
                403,
                "desktop_origin_forbidden",
            ),
            (
                "missing-bearer",
                {"Origin": session.origin},
                401,
                "desktop_auth_required",
            ),
            (
                "wrong-bearer",
                {"Origin": session.origin, "Authorization": "Bearer wrong"},
                401,
                "desktop_auth_required",
            ),
        )
        attempts = []
        for method, path in endpoints:
            for case_name, headers, expected_status, expected_code in authority_cases:
                response = client.request(
                    method,
                    path,
                    headers=headers,
                    json={} if method == "POST" else None,
                )
                attempts.append(
                    (path, case_name, response, expected_status, expected_code)
                )

    for path, case_name, response, expected_status, expected_code in attempts:
        assert response.status_code == expected_status, (path, case_name, response.text)
        assert response.json() == {"code": expected_code}, (path, case_name)
    serialized = "\n".join(response.text for _, _, response, _, _ in attempts)
    assert SESSION_SECRET not in serialized
    assert "evil.invalid" not in serialized
    assert str(tmp_path) not in serialized
    assert "traceback" not in serialized.casefold()


def test_authenticated_desktop_runs_backtest_analysis_and_safe_task_center(
    tmp_path: Path,
) -> None:
    with _desktop_journey(tmp_path) as (client, worker, session):
        macd = _formula(client, name="Desktop MACD", source=MACD_SOURCE)
        custom = _formula(client, name="Desktop custom wave", source=CUSTOM_SOURCE)
        macd_version = cast(
            str, cast(dict[str, object], macd["draft"])["executable_version_id"]
        )
        custom_version = cast(
            str, cast(dict[str, object], custom["draft"])["executable_version_id"]
        )

        macd_preflight, single_report = _run_backtest(
            client,
            worker,
            macd_version,
            scope={"kind": "single", "symbol": SYMBOL},
        )
        pool = next(
            item
            for item in client.get("/api/market/pools").json()["items"]
            if item["category"] == "index"
        )
        custom_preflight, pool_report = _run_backtest(
            client,
            worker,
            custom_version,
            scope={
                "kind": "preset",
                "pool_id": pool["pool_id"],
                "snapshot_id": pool["snapshot_id"],
            },
        )

        assert macd_preflight["formula"]["formula_version_id"] == macd_version
        assert custom_preflight["formula"]["formula_version_id"] == custom_version
        for report, version_id in (
            (single_report, macd_version),
            (pool_report, custom_version),
        ):
            assert report["overview"]["status"] in {"succeeded", "partial_failed"}
            assert report["overview"]["snapshot_id"]
            assert report["formula_version_id"] == version_id
            assert cast(str, report["formula_checksum"]).startswith("sha256:")
            assert report["formula_parameters"] == []

        model = client.get("/api/settings/models").json()["items"][0]
        submitted = client.post(
            "/api/analysis",
            json={
                "symbol": SYMBOL,
                "model_config_id": model["id"],
                "retry": {"max_retries": 0},
            },
        )
        assert submitted.status_code == 202
        analysis_task_id = submitted.json()["task_id"]
        analysis_run_id = submitted.json()["run_id"]
        completed = worker.run_once()
        assert completed is not None
        assert completed.id == analysis_task_id
        assert completed.status == "succeeded"

        detail = client.get(f"/api/analysis/{analysis_run_id}")
        report = client.get(f"/api/analysis/{analysis_run_id}/report")
        assert detail.status_code == report.status_code == 200
        assert detail.json()["status"] == "succeeded"
        assert report.json()["status"] == "complete"
        assert report.json()["snapshot_id"] == detail.json()["snapshot_id"]
        evidence_id = report.json()["core_judgments"][0]["evidence_ids"][0]
        evidence = client.get(f"/api/analysis/{analysis_run_id}/evidence/{evidence_id}")
        assert evidence.status_code == 200
        assert evidence.json()["snapshot_id"] == report.json()["snapshot_id"]

        task_list = client.get("/api/tasks?view=safe&limit=100")
        task_detail = client.get(f"/api/tasks/{analysis_task_id}?view=safe")
        task_events = client.get(
            f"/api/tasks/{analysis_task_id}/events?view=safe&limit=100"
        )
        task_metrics = client.get("/api/tasks/metrics")

    assert task_list.status_code == 200
    assert task_detail.status_code == 200
    assert task_events.status_code == 200
    assert task_metrics.status_code == 200
    tasks = task_list.json()
    assert {item["kind"] for item in tasks} >= {"backtest.run", "analysis.run"}
    assert task_detail.json()["status"] == "succeeded"
    assert task_detail.json()["presentation"]["label"] == "智能分析"
    assert task_detail.json()["presentation"]["stage"] is None
    assert task_events.json()
    assert task_events.json()[-1]["presentation"]["label"] == "任务已完成"
    forbidden_task_fields = {
        "payload",
        "payload_json",
        "result",
        "result_json",
        "error",
        "error_json",
        "worker_id",
        "claim_token",
    }
    for payload in (tasks, task_detail.json(), task_events.json()):
        assert forbidden_task_fields.isdisjoint(_all_keys(payload))
    serialized = json.dumps(
        {
            "tasks": tasks,
            "detail": task_detail.json(),
            "events": task_events.json(),
            "metrics": task_metrics.json(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert session.secret_for_host() not in serialized
    assert str(tmp_path) not in serialized
    assert "traceback" not in serialized.casefold()
