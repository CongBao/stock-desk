from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from stock_desk.analysis.evidence import EvidenceGraph, EvidenceItem
from stock_desk.analysis.model_catalog import ModelNotFound, ModelNotVerified
from stock_desk.analysis.report import ResearchReport, ResearchReportBuilder
from stock_desk.analysis.repository import AnalysisStageStatus
from stock_desk.analysis.snapshot import (
    ResearchSection,
    ResearchSectionKind,
    ResearchSnapshot,
)
from stock_desk.analysis.service import (
    AnalysisEvidenceNotFound,
    AnalysisReportNotReady,
    AnalysisReportUnavailable,
    AnalysisStateConflict,
)
from stock_desk.api.analysis import router


RUN_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"
MODEL_CONFIG_ID = "sha256:" + "a" * 64
IDENTITY = SimpleNamespace(kind="test", value="same")
NOW = datetime(2026, 7, 7, 9, tzinfo=timezone.utc)


class _Services:
    database_identity = IDENTITY
    analysis_repository_identity = IDENTITY
    task_repository_identity = IDENTITY
    model_catalog_identity = IDENTITY

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: Exception | None = None

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    def submit(self, *, symbol: str, model_config_id: str, max_retries: int) -> object:
        self._raise()
        self.calls.append(("submit", symbol, model_config_id, max_retries))
        return SimpleNamespace(
            run_id=RUN_ID,
            task_id=TASK_ID,
            parent_run_id=None,
            requested_stage=None,
            status="queued",
            snapshot_id=None,
        )

    def check(self, symbol: str) -> object:
        self._raise()
        self.calls.append(("preflight", symbol))
        categories = tuple(
            SimpleNamespace(
                kind=ResearchSectionKind(kind),
                critical=kind in {"market", "fundamentals"},
                connection_state="available",
                route_source=("market_cache" if kind == "market" else "akshare"),
                actual_source="akshare",
                ordered_candidates=(
                    SimpleNamespace(
                        source=("market_cache" if kind == "market" else "akshare"),
                        position=0,
                        supported=True,
                        configured=True,
                        outcome="selected",
                        failure_reason=None,
                    ),
                ),
                attempted_sources=("market_cache" if kind == "market" else "akshare",),
                missing_reason=None,
                recovery_code=None,
                permission_gap=False,
                data_cutoff=NOW,
                fetched_at=NOW,
                dataset_version="sha256:" + "d" * 64,
                quality_flags=(),
            )
            for kind in ("market", "fundamentals", "announcements", "news")
        )
        return SimpleNamespace(
            symbol=symbol,
            preview_snapshot_id="sha256:" + "e" * 64,
            reservation=False,
            rating_eligible=True,
            checked_at=NOW,
            categories=categories,
        )

    def history(
        self, *, limit: int, after: object | None, symbol: str | None
    ) -> object:
        self._raise()
        self.calls.append(("history", limit, after, symbol))
        item = _detail()
        return SimpleNamespace(
            items=(item,),
            next_key=SimpleNamespace(created_at=item.run.created_at, id=item.run.id),
        )

    def detail(self, run_id: str) -> object:
        self._raise()
        self.calls.append(("detail", run_id))
        return _detail()

    def cancel(self, run_id: str) -> object:
        self._raise()
        self.calls.append(("cancel", run_id))
        detail = _detail()
        detail.run.status = "cancelled"
        detail.task.status = "cancelled"
        detail.task.cancel_requested = True
        return detail

    def report(self, run_id: str) -> ResearchReport:
        self._raise()
        self.calls.append(("report", run_id))
        return _report()

    def evidence(self, run_id: str, evidence_id: str) -> EvidenceItem:
        self._raise()
        self.calls.append(("evidence", run_id, evidence_id))
        return _evidence()

    def retry(self, run_id: str, stage: str) -> object:
        self._raise()
        self.calls.append(("retry", run_id, stage))
        return SimpleNamespace(
            run_id="33333333-3333-3333-3333-333333333333",
            task_id="44444444-4444-4444-4444-444444444444",
            parent_run_id=run_id,
            requested_stage=stage,
            status="queued",
            snapshot_id="sha256:" + "b" * 64,
        )


def _detail() -> object:
    roles = (
        "market",
        "fundamentals",
        "announcements",
        "news",
        "technical",
        "fundamental_news",
        "bull",
        "bear",
        "risk_decision",
    )
    stages = tuple(
        SimpleNamespace(
            role=role,
            ordinal=ordinal - 4,
            status=(
                AnalysisStageStatus.FAILED
                if role == "bull"
                else AnalysisStageStatus.SUCCEEDED
            ),
            attempt_count=(2 if role == "bull" else 1),
            source_run_id=None,
            failure_code=("model_timeout" if role == "bull" else None),
            retryable=(True if role == "bull" else None),
            started_at=NOW,
            finished_at=NOW + timedelta(milliseconds=250),
            duration_ms=250.0,
        )
        for ordinal, role in enumerate(roles)
    )
    return SimpleNamespace(
        run=SimpleNamespace(
            id=RUN_ID,
            task_id=TASK_ID,
            symbol="600000.SH",
            parent_run_id=None,
            requested_stage=None,
            status="partial",
            current_stage=None,
            snapshot_id="sha256:" + "b" * 64,
            report_id="sha256:" + "c" * 64,
            failure_code=None,
            model_config_id=MODEL_CONFIG_ID,
            model_provider="ollama",
            model_name="qwen3:8b",
            created_at=NOW,
            updated_at=NOW,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            duration_ms=1000.0,
        ),
        task=SimpleNamespace(status="succeeded", progress=1.0, cancel_requested=False),
        stages=stages,
        retry_stages=frozenset({"bull"}),
    )


def _snapshot() -> ResearchSnapshot:
    sections = tuple(
        ResearchSection(  # type: ignore[call-arg]
            kind=kind,
            canonical_source="fixture",
            source_record=f"{kind.value}:1",
            source_url=f"https://example.com/{kind.value}/1",
            published_at=(
                NOW
                if kind in {ResearchSectionKind.ANNOUNCEMENTS, ResearchSectionKind.NEWS}
                else None
            ),
            data_cutoff=NOW,
            fetched_at=NOW,
            dataset_version="fixture-v1",
            quality_flags=(),
            content={"kind": kind.value},
        )
        for kind in ResearchSectionKind
    )
    return ResearchSnapshot.create(
        symbol="600000.SH", frozen_at=NOW, sections=sections, missing_sections=()
    )


def _evidence() -> EvidenceItem:
    snapshot = _snapshot()
    return EvidenceItem.create(
        snapshot=snapshot,
        section_kind=ResearchSectionKind.NEWS,
        excerpt="persisted evidence",
    )


def _report() -> ResearchReport:
    snapshot = _snapshot()
    return ResearchReportBuilder().build_insufficient(
        snapshot=snapshot,
        evidence_graph=EvidenceGraph(snapshot=snapshot, evidence_items=(), claims=()),
    )


def _client(services: object, *, identity: object = IDENTITY) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.state.analysis_services_provider = lambda: services
    application.state.analysis_preflight_provider = lambda: services
    application.state.database_identity = identity
    application.state.analysis_cursor_key = b"k" * 32
    return TestClient(application, raise_server_exceptions=False)


def _request(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "symbol": "600000.SH",
        "model_config_id": MODEL_CONFIG_ID,
        "retry": {"max_retries": 2},
    }
    body.update(changes)
    return body


def test_submit_accepts_only_analysis_inputs_and_returns_async_identity() -> None:
    services = _Services()
    response = _client(services).post("/analysis", json=_request())

    assert response.status_code == 202
    assert response.json() == {
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "parent_run_id": None,
        "requested_stage": None,
        "status": "queued",
        "snapshot_id": None,
    }
    assert services.calls == [("submit", "600000.SH", MODEL_CONFIG_ID, 2)]


def test_preflight_accepts_only_symbol_and_returns_four_ordered_categories() -> None:
    services = _Services()

    response = _client(services).post(
        "/analysis/preflight", json={"symbol": "600000.SH"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "600000.SH"
    assert body["reservation"] is False
    assert body["rating_eligible"] is True
    assert [item["kind"] for item in body["categories"]] == [
        "market",
        "fundamentals",
        "announcements",
        "news",
    ]
    assert body["categories"][0]["route_source"] == "market_cache"
    assert body["categories"][0]["ordered_candidates"][0]["outcome"] == "selected"
    assert services.calls == [("preflight", "600000.SH")]

    invalid = _client(services).post(
        "/analysis/preflight",
        json={"symbol": "600000.SH", "prompt": "TOP-SECRET"},
    )
    assert invalid.status_code == 422
    assert invalid.json() == {"code": "invalid_request"}
    assert "TOP-SECRET" not in invalid.text


def test_preflight_openapi_contains_no_model_formula_or_backtest_inputs() -> None:
    schema = _client(_Services()).get("/openapi.json").json()
    request = schema["components"]["schemas"]["AnalysisPreflightRequest"]

    assert set(request["properties"]) == {"symbol"}
    serialized = str(schema["paths"]["/analysis/preflight"]["post"])
    for forbidden in (
        "model_config_id",
        "formula_id",
        "backtest_id",
        "prompt",
        "snapshot_id",
    ):
        assert forbidden not in serialized


def test_submit_rejects_frozen_and_unknown_inputs_without_validation_details() -> None:
    services = _Services()
    forbidden = (
        "formula_id",
        "formula_version_id",
        "formula_parameters",
        "backtest_id",
        "strategy",
        "prompt",
        "snapshot_id",
        "target",
        "position",
        "unknown",
    )

    for field in forbidden:
        response = _client(services).post(
            "/analysis", json=_request(**{field: "provider-secret"})
        )
        assert response.status_code == 422
        assert response.json() == {"code": "invalid_request"}
        assert "provider-secret" not in response.text

    assert services.calls == []


def test_analysis_openapi_request_excludes_formula_backtest_and_prompt_fields() -> None:
    schema = _client(_Services()).get("/openapi.json").json()
    request_schema = schema["components"]["schemas"]["AnalysisCreateRequest"]
    stage_schema = schema["components"]["schemas"]["AnalysisStageResponse"]

    assert set(request_schema["properties"]) == {
        "symbol",
        "model_config_id",
        "retry",
    }
    serialized = str(schema["paths"]["/analysis"])
    for forbidden in (
        "formula_id",
        "formula_version_id",
        "backtest_id",
        "prompt",
        "target",
        "position",
    ):
        assert forbidden not in serialized
    assert stage_schema["properties"]["ordinal"]["type"] == "integer"
    assert "ordinal" in stage_schema["required"]
    overview_schema = schema["components"]["schemas"]["AnalysisOverviewResponse"]
    assert overview_schema["properties"]["task_status"]["enum"] == [
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert overview_schema["properties"]["progress"]["minimum"] == 0.0
    assert overview_schema["properties"]["progress"]["maximum"] == 1.0


def test_history_cursor_is_stable_filter_bound_and_tamper_evident() -> None:
    services = _Services()
    client = _client(services)

    first = client.get("/analysis", params={"symbol": "600000.SH", "limit": 1})
    assert first.status_code == 200
    assert len(first.json()["items"]) == 1
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str)

    second = client.get(
        "/analysis",
        params={"symbol": "600000.SH", "limit": 1, "cursor": cursor},
    )
    assert second.status_code == 200
    after = services.calls[-1][2]
    assert after.id == RUN_ID
    assert after.created_at == NOW

    for invalid_params in (
        {"symbol": "000001.SZ", "cursor": cursor},
        {"symbol": "600000.SH", "cursor": cursor[:-1] + "A"},
        {"symbol": "600000.SH", "cursor": "x" * 5000},
    ):
        invalid = client.get("/analysis", params=invalid_params)
        assert invalid.status_code == 422
        assert invalid.json() == {"code": "invalid_cursor"}


def test_history_cursor_rejects_signed_non_integer_version() -> None:
    client = _client(_Services())
    cursor = client.get("/analysis").json()["next_cursor"]
    padding = "=" * (-len(cursor) % 4)
    envelope = json.loads(base64.urlsafe_b64decode(cursor + padding))
    envelope["body"]["v"] = True
    body = json.dumps(
        envelope["body"],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    envelope["signature"] = hmac.new(b"k" * 32, body, hashlib.sha256).hexdigest()
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )

    response = client.get("/analysis", params={"cursor": forged})
    assert response.status_code == 422
    assert response.json() == {"code": "invalid_cursor"}


def test_detail_distinguishes_run_and_task_status_and_projects_nine_stages() -> None:
    response = _client(_Services()).get(f"/analysis/{RUN_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "partial"
    assert body["task_status"] == "succeeded"
    assert body["progress"] == 1.0
    assert body["report_id"] == "sha256:" + "c" * 64
    assert [stage["stage"] for stage in body["stages"]] == [
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
    assert [stage["ordinal"] for stage in body["stages"]] == list(range(-4, 5))
    assert [stage["kind"] for stage in body["stages"][:4]] == ["data"] * 4
    assert [stage["kind"] for stage in body["stages"][4:]] == ["role"] * 5
    bull = body["stages"][6]
    assert bull["retry_allowed"] is True
    assert bull["failure_code"] == "model_timeout"
    assert bull["duration_ms"] == 250.0


def test_submit_maps_model_gate_and_unknown_failures_to_stable_safe_errors() -> None:
    services = _Services()
    client = _client(services)
    cases = (
        (ModelNotFound("secret provider detail"), 404, "not_found"),
        (ModelNotVerified("secret provider detail"), 409, "model_not_verified"),
        (RuntimeError("secret provider token"), 503, "service_unavailable"),
    )
    for error, status_code, code in cases:
        services.error = error
        response = client.post("/analysis", json=_request())
        assert response.status_code == status_code
        assert response.json() == {"code": code}
        assert "secret" not in response.text


def test_cancel_report_evidence_and_retry_routes_use_stable_contracts() -> None:
    services = _Services()
    client = _client(services)

    cancelled = client.post(f"/analysis/{RUN_ID}/cancel")
    report = client.get(f"/analysis/{RUN_ID}/report")
    evidence = client.get(f"/analysis/{RUN_ID}/evidence/{'sha256:' + 'd' * 64}")
    retried = client.post(f"/analysis/{RUN_ID}/stages/bull/retry")

    assert cancelled.status_code == 202
    assert cancelled.json()["cancel_requested"] is True
    assert cancelled.json()["status"] == "cancelled"
    assert report.status_code == 200
    assert report.json()["rating"] is None
    assert report.json()["report_id"].startswith("sha256:")
    assert evidence.status_code == 200
    assert evidence.json()["excerpt"] == "persisted evidence"
    assert retried.status_code == 202
    assert retried.json()["parent_run_id"] == RUN_ID
    assert retried.json()["requested_stage"] == "bull"
    assert retried.json()["snapshot_id"] == "sha256:" + "b" * 64


def test_analysis_state_and_report_failures_have_endpoint_specific_codes() -> None:
    services = _Services()
    client = _client(services)
    cases = (
        (
            "post",
            f"/analysis/{RUN_ID}/cancel",
            AnalysisStateConflict(),
            409,
            "state_conflict",
        ),
        (
            "get",
            f"/analysis/{RUN_ID}/report",
            AnalysisReportNotReady(),
            409,
            "report_not_ready",
        ),
        (
            "get",
            f"/analysis/{RUN_ID}/report",
            AnalysisReportUnavailable(),
            409,
            "report_unavailable",
        ),
        (
            "get",
            f"/analysis/{RUN_ID}/evidence/{'sha256:' + 'd' * 64}",
            AnalysisEvidenceNotFound(),
            404,
            "evidence_not_found",
        ),
    )
    for method, path, error, status_code, code in cases:
        services.error = error
        response = client.request(method, path)
        assert response.status_code == status_code
        assert response.json() == {"code": code}


def test_database_identity_missing_or_mismatch_fails_closed() -> None:
    services = _Services()
    mismatched = _client(services, identity=SimpleNamespace(kind="test", value="other"))
    assert mismatched.get(f"/analysis/{RUN_ID}").json() == {
        "code": "storage_unavailable"
    }

    services.analysis_repository_identity = None
    missing = _client(services)
    response = missing.get(f"/analysis/{RUN_ID}")
    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}


def test_history_requires_exactly_32_byte_cursor_key() -> None:
    for key in (None, b"short", b"x" * 33, "k" * 32):
        application = FastAPI()
        application.include_router(router)
        services = _Services()
        application.state.analysis_services_provider = lambda: services
        application.state.database_identity = IDENTITY
        if key is not None:
            application.state.analysis_cursor_key = key
        response = TestClient(application, raise_server_exceptions=False).get(
            "/analysis"
        )
        assert response.status_code == 503
        assert response.json() == {"code": "storage_unavailable"}


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "provider-secret-status"), ("progress", 1.5)],
)
def test_analysis_response_projection_rejects_corrupt_task_values(
    field: str, value: object
) -> None:
    class _CorruptServices(_Services):
        def detail(self, run_id: str) -> object:
            item = _detail()
            setattr(item.task, field, value)
            return item

    response = _client(_CorruptServices()).get(f"/analysis/{RUN_ID}")
    assert response.status_code == 503
    assert response.json() == {"code": "storage_unavailable"}
    assert str(value) not in response.text
