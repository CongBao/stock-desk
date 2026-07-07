from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest

from scripts.seed_demo_data import (
    DEMO_FIXTURE_PATH,
    load_demo_fixture,
    seed_demo_data,
)
from stock_desk.analysis.providers.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from stock_desk.analysis.repository import AnalysisExecutionConfig
from stock_desk.analysis.roles import RoleName
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.market.worker_runtime import ProductionMarketWorker


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
            "summary": f"{role.value} synthetic demo summary",
            "claims": [
                {
                    "text": f"{role.value} synthetic evidence observation",
                    "evidence_ids": [evidence_ids[0]],
                    "stance": "support",
                }
            ],
        }
        if role is RoleName.RISK_DECISION:
            content["proposal"] = {
                "rating": "neutral",
                "confidence": 0.5,
                "confidence_explanation": "Synthetic evidence is balanced.",
            }
        return ModelResponse(
            provider=self.provider,
            model=self.model,
            content=cast(Any, content),
            usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
        )


@contextmanager
def _journey(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, ProductionMarketWorker, dict[str, object]]]:
    destination = tmp_path / "demo-profile"
    summary = seed_demo_data(destination)
    settings = Settings(
        database_url=f"sqlite:///{destination / 'stock-desk.db'}",
        data_dir=destination,
    )
    fixture = load_demo_fixture()
    worker = ProductionMarketWorker.open(
        settings,
        worker_id="full-user-journey",
        analysis_provider_factory=lambda execution: cast(
            ModelProvider, _DeterministicModel(execution)
        ),
        analysis_data_service_factory=fixture.research_data_service,
    )
    try:
        with TestClient(create_app(settings)) as client:
            yield client, worker, summary
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


def _preview(client: TestClient, version_id: str) -> dict[str, object]:
    fixture = load_demo_fixture()
    response = client.post(
        f"/api/formulas/{version_id}/preview",
        json={
            "symbol": SYMBOL,
            "period": "1d",
            "adjustment": "none",
            "start": fixture.window_start,
            "end": fixture.window_end,
            "parameters": {},
        },
    )
    assert response.status_code == 200
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
    assert preflight.status_code == 200
    submitted = client.post("/api/backtests", json=request)
    assert submitted.status_code == 202
    submission = cast(dict[str, object], submitted.json())
    completed = worker.run_once()
    assert completed is not None and completed.status == "succeeded"
    report = client.get(f"/api/backtests/{submission['run_id']}/report")
    assert report.status_code == 200
    return cast(dict[str, object], preflight.json()), cast(
        dict[str, object], report.json()
    )


def test_complete_no_network_application_journey(tmp_path: Path) -> None:
    with _journey(tmp_path) as (client, worker, summary):
        assert summary["fixture_schema"] == "stock-desk-public-demo-v1"
        assert client.get("/api/health").status_code == 200
        sources = client.get("/api/settings/sources")
        assert sources.status_code == 200

        search = client.get("/api/market/instruments", params={"q": "Synthetic Alpha"})
        assert search.status_code == 200
        assert search.json()[0]["symbol"] == SYMBOL
        fixture = load_demo_fixture()
        chart = client.get(
            "/api/market/bars",
            params={
                "symbol": SYMBOL,
                "period": "1d",
                "adjustment": "none",
                "start": fixture.window_start,
                "end": fixture.window_end,
            },
        )
        assert chart.status_code == 200
        assert chart.json()["provenance"]["dataset_version"].startswith("sha256:")
        assert chart.json()["provenance"]["source"] == "stock_desk_demo"

        macd = _formula(client, name="Demo MACD", source=MACD_SOURCE)
        custom = _formula(client, name="Demo custom wave", source=CUSTOM_SOURCE)
        macd_version = cast(
            str, cast(dict[str, object], macd["draft"])["executable_version_id"]
        )
        custom_version = cast(
            str, cast(dict[str, object], custom["draft"])["executable_version_id"]
        )
        assert any(
            value is True
            for signal in cast(
                list[dict[str, object]], _preview(client, macd_version)["signals"]
            )
            if signal["name"] == "BUY"
            for value in cast(list[bool | None], signal["values"])
        )
        assert _preview(client, custom_version)["signals"]

        _, single = _run_backtest(
            client,
            worker,
            macd_version,
            scope={"kind": "single", "symbol": SYMBOL},
        )
        pool = next(
            item
            for item in client.get("/api/market/pools").json()["items"]
            if item["category"] == "index"
            and item["name"] == "Stock Desk Synthetic Demo Index (CC0)"
        )
        _, pooled = _run_backtest(
            client,
            worker,
            custom_version,
            scope={
                "kind": "preset",
                "pool_id": pool["pool_id"],
                "snapshot_id": pool["snapshot_id"],
            },
        )
        assert single["overview"]["status"] == "succeeded"
        assert pooled["overview"]["status"] in {"succeeded", "partial_failed"}

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
        completed = worker.run_once()
        assert completed is not None and completed.status == "succeeded", (
            completed,
            json.dumps(
                client.get(f"/api/analysis/{submitted.json()['run_id']}").json(),
                sort_keys=True,
            ),
        )
        run_id = submitted.json()["run_id"]
        report = client.get(f"/api/analysis/{run_id}/report")
        assert report.status_code == 200
        evidence_id = report.json()["core_judgments"][0]["evidence_ids"][0]
        evidence = client.get(f"/api/analysis/{run_id}/evidence/{evidence_id}")
        assert evidence.status_code == 200
        assert evidence.json()["snapshot_id"] == report.json()["snapshot_id"]
        history = client.get("/api/tasks")
        assert history.status_code == 200
        assert {item["kind"] for item in history.json()} >= {
            "backtest.run",
            "analysis.run",
        }


def test_formula_signal_matches_backtest_entry(tmp_path: Path) -> None:
    with _journey(tmp_path) as (client, worker, _summary):
        formula = _formula(client, name="Signal identity MACD", source=MACD_SOURCE)
        version_id = cast(
            str,
            cast(dict[str, object], formula["draft"])["executable_version_id"],
        )
        preview = _preview(client, version_id)
        timestamps = cast(list[str], preview["timestamps"])
        buy = next(
            signal
            for signal in cast(list[dict[str, object]], preview["signals"])
            if signal["name"] == "BUY"
        )
        scoring_start = datetime.fromisoformat(
            load_demo_fixture().scoring_start.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        first_buy = next(
            timestamp
            for timestamp, active in zip(
                timestamps, cast(list[bool | None], buy["values"]), strict=True
            )
            if active is True
            and datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            >= scoring_start
        )

        preflight, report = _run_backtest(
            client,
            worker,
            version_id,
            scope={"kind": "single", "symbol": SYMBOL},
        )
        run_id = cast(str, cast(dict[str, object], report["overview"])["run_id"])
        trades = client.get(f"/api/backtests/{run_id}/trades").json()["items"]
        assert trades
        replay = client.get(
            f"/api/backtests/{run_id}/trades/{SYMBOL}/0/replay",
            params={"limit": 500},
        )
        assert replay.status_code == 200
        replay_body = replay.json()
        assert replay_body["fill_markers"][0]["signal_at"] == first_buy
        assert report["formula_version_id"] == version_id
        assert preflight["formula"]["formula_version_id"] == version_id
        assert replay_body["formula"]["formula_version_id"] == version_id
        assert report["formula_checksum"] == preview["formula_checksum"]
        assert replay_body["formula"]["formula_checksum"] == preview["formula_checksum"]
        assert replay_body["snapshot_id"] == report["overview"]["snapshot_id"]
        symbols = client.get(f"/api/backtests/{run_id}/symbols").json()["items"]
        assert (
            replay_body["formula"]["signal_series_id"] == symbols[0]["signal_series_id"]
        )
        assert replay_body["provenance"]["signal"]["dataset_version"] in {
            item
            for item in [load_demo_fixture().bar_dataset_version]
            if item is not None
        }


def test_analysis_remains_independent(tmp_path: Path) -> None:
    forbidden = {
        "formula_version_id",
        "backtest_run_id",
        "signal_series_id",
        "formula_parameters",
        "strategy",
    }
    with _journey(tmp_path) as (client, worker, _summary):
        formula = _formula(client, name="Isolation MACD", source=MACD_SOURCE)
        version_id = cast(
            str, cast(dict[str, object], formula["draft"])["executable_version_id"]
        )
        _, backtest_report = _run_backtest(
            client,
            worker,
            version_id,
            scope={"kind": "single", "symbol": SYMBOL},
        )
        backtest_before = json.dumps(backtest_report, sort_keys=True)
        run_ids_before = {
            item["run_id"] for item in client.get("/api/backtests").json()["items"]
        }
        model = client.get("/api/settings/models").json()["items"][0]
        request = {
            "symbol": SYMBOL,
            "model_config_id": model["id"],
            "retry": {"max_retries": 0},
        }
        assert forbidden.isdisjoint(request)
        submitted = client.post("/api/analysis", json=request)
        assert submitted.status_code == 202
        completed = worker.run_once()
        assert completed is not None and completed.status == "succeeded"
        run_id = submitted.json()["run_id"]
        detail = client.get(f"/api/analysis/{run_id}").json()
        report = client.get(f"/api/analysis/{run_id}/report").json()
        assert report["status"] == "complete"
        for payload in (submitted.json(), detail, report):
            assert forbidden.isdisjoint(payload)
            assert not any(key in json.dumps(payload) for key in forbidden)
        assert report["core_judgments"]
        run_ids_after = {
            item["run_id"] for item in client.get("/api/backtests").json()["items"]
        }
        assert run_ids_after == run_ids_before
        unchanged = client.get(
            f"/api/backtests/{backtest_report['overview']['run_id']}/report"
        )
        assert json.dumps(unchanged.json(), sort_keys=True) == backtest_before


def test_demo_data_categories_and_missing_category_are_visible() -> None:
    fixture = load_demo_fixture(DEMO_FIXTURE_PATH)
    assert fixture.schema_version == "stock-desk-public-demo-v1"
    assert fixture.license == "CC0-1.0"
    assert fixture.market_source == "stock_desk_demo"
    outcomes = fixture.category_outcomes
    assert set(outcomes) == {
        "bars_adjustment",
        "instrument_calendar_trading_status",
        "fundamentals",
        "announcements",
        "news",
        "index_membership",
        "industry",
    }
    assert all(outcome.status in {"actual", "missing"} for outcome in outcomes.values())
    assert all(outcome.source and outcome.data_cutoff for outcome in outcomes.values())
    assert {outcome.source for outcome in outcomes.values()} == {"stock_desk_demo"}
    assert outcomes["announcements"].status == "missing"
    assert outcomes["announcements"].missing_reason == "no_data"
    assert outcomes["announcements"].substitute is None
    assert {scenario.kind for scenario in fixture.scenarios} >= {
        "normal",
        "suspended",
        "limit_up",
        "limit_down",
        "missing_data",
        "formula_signal",
        "traceable_analysis_evidence",
    }
    assert fixture.network_policy == "forbidden"
    assert fixture.investment_recommendation_claims is False
    assert "/Users/" not in DEMO_FIXTURE_PATH.read_text(encoding="utf-8")


def test_demo_seed_is_idempotent_and_rejects_unsafe_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "controlled-demo"
    first = seed_demo_data(destination)
    second = seed_demo_data(destination)
    assert first["seed_state"] == "created"
    assert second == {**first, "seed_state": "already_seeded"}
    assert (
        first["primary_bar_dataset_version"] == load_demo_fixture().bar_dataset_version
    )
    assert all(
        token not in json.dumps(second, sort_keys=True).casefold()
        for token in ("api_key", "token", "secret", str(tmp_path).casefold())
    )

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("user-owned", encoding="utf-8")
    with pytest.raises(ValueError, match="unrelated"):
        seed_demo_data(unrelated)
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "user-owned"

    symlink = tmp_path / "linked"
    symlink.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        seed_demo_data(symlink)
    with pytest.raises(ValueError, match="unsafe"):
        seed_demo_data(Path.home())

    interrupted = tmp_path / "interrupted-demo"

    def fail_after_partial_write(destination: Path, _fixture: object) -> object:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial.db").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected seed failure")

    monkeypatch.setattr(
        "scripts.seed_demo_data._seed_fresh",
        fail_after_partial_write,
    )
    with pytest.raises(RuntimeError, match="injected seed failure"):
        seed_demo_data(interrupted)
    assert not interrupted.exists()
    assert not tuple(tmp_path.glob(".interrupted-demo.stock-desk-demo-staging-*"))
