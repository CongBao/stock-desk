from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from stock_desk.backtest.repository import (
    BacktestFailureSnapshot,
    BacktestGroupSnapshot,
    BacktestLogSnapshot,
    BacktestOverviewSnapshot,
    BacktestPage,
    BacktestReportSnapshot,
    BacktestTradeSnapshot,
)
from stock_desk.backtest.service import (
    BacktestIntent,
    BacktestPreflight,
    SubmittedBacktest,
)
from stock_desk.main import create_app
from stock_desk.formula.repository import FormulaNotFound
from stock_desk.market.pools import PoolNotFound, PoolRevisionConflict


FORMULA_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _request() -> dict[str, object]:
    return {
        "scope": {"kind": "single", "symbol": "600000.SH"},
        "formula_version_id": FORMULA_ID,
        "formula_parameters": {},
        "period": "1d",
        "adjustment": "none",
        "scoring_start": "2024-01-03T00:00:00Z",
        "scoring_end": "2024-02-01T00:00:00Z",
        "quantity_shares": 1000,
        "commission_bps": "2.5",
        "minimum_commission": "5",
        "sell_tax_bps": "5",
        "slippage_bps": "3",
    }


class _CreateServices:
    database_identity = SimpleNamespace(kind="test", value="same")

    def __init__(self) -> None:
        self.intent: BacktestIntent | None = None

    def submit(self, intent: BacktestIntent) -> SubmittedBacktest:
        self.intent = intent
        return SubmittedBacktest(
            run_id="11111111-1111-1111-1111-111111111111",
            task_id="22222222-2222-2222-2222-222222222222",
            snapshot_id="sha256:" + "a" * 64,
            warnings=(),
        )


RUN_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"
SNAPSHOT_ID = "sha256:" + "a" * 64
NOW = datetime(2024, 2, 2, tzinfo=timezone.utc)


def _overview() -> BacktestOverviewSnapshot:
    return BacktestOverviewSnapshot(
        run_id=RUN_ID,
        task_id=TASK_ID,
        snapshot_id=SNAPSHOT_ID,
        status="succeeded",
        stage="completed",
        total=3,
        processed=3,
        failed=1,
        result_hash="sha256:" + "b" * 64,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        finished_at=NOW,
    )


class _ReadServices(_CreateServices):
    def preflight(self, intent: BacktestIntent) -> BacktestPreflight:
        self.intent = intent
        return BacktestPreflight(
            preview_snapshot_id=SNAPSHOT_ID,
            reservation=False,
            formula_id="formula-1",
            formula_version_id=FORMULA_ID,
            formula_checksum="sha256:" + "d" * 64,
            engine_version="formula-engine-v1",
            compatibility_version="tdx-v1",
            normalized_parameters=(),
            scope_kind="single",
            symbol="600000.SH",
            scope_id=None,
            scope_revision_or_snapshot_id=None,
            total=1,
            runnable=1,
            gap_count=0,
            gap_sample=(),
            warnings=(),
            period="1d",
            adjustment="none",
            scoring_start=datetime(2024, 1, 3, tzinfo=timezone.utc),
            scoring_end=datetime(2024, 2, 1, tzinfo=timezone.utc),
            warmup_policy_version="formula-warmup-v1",
            lookback_bars=None,
            unbounded_dependency=True,
            pinned_signal_count=1,
            pinned_execution_count=1,
            pinned_status_count=1,
            estimated_formula_rows=30,
            execution_rules_version="a-share-v1",
            cost_model_version="a-share-cost-v1",
            sizing_version="fixed-lot-v1",
            quantity_shares=1000,
            commission_bps=Decimal("2.5"),
            minimum_commission=Decimal("5"),
            sell_tax_bps=Decimal("5"),
            slippage_bps=Decimal("3"),
            disclaimer="independent trade samples, not portfolio return",
        )

    def list_runs(self, *, limit: int, cursor: str | None) -> BacktestPage:
        assert (limit, cursor) == (1, None)
        return BacktestPage(items=(_overview(),), next_cursor="next-run")

    def get_overview(self, run_id: str) -> BacktestOverviewSnapshot:
        assert run_id == RUN_ID
        return _overview()

    def cancel(self, run_id: str) -> SubmittedBacktest:
        assert run_id == RUN_ID
        return SubmittedBacktest(RUN_ID, TASK_ID, SNAPSHOT_ID, ())

    def copy(self, run_id: str, *, mode: str) -> SubmittedBacktest:
        assert run_id == RUN_ID
        assert mode in {"exact", "latest"}
        return SubmittedBacktest(
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
            SNAPSHOT_ID if mode == "exact" else "sha256:" + "c" * 64,
            (),
        )

    def report(self, run_id: str) -> BacktestReportSnapshot:
        assert run_id == RUN_ID
        return BacktestReportSnapshot(
            overview=_overview(),
            formula_version_id=FORMULA_ID,
            formula_checksum="sha256:" + "d" * 64,
            formula_engine_version="formula-engine-v1",
            compatibility_version="tdx-v1",
            backtest_engine_version="backtest-engine-v1",
            formula_parameters=(),
            instrument_dataset_version="sha256:" + "e" * 64,
            symbol_count=3,
            runnable_count=2,
            gap_count=1,
            signal_source_ids=("tushare",),
            execution_source_ids=("akshare",),
            status_source_ids=("tdx_local",),
            provenance_digest="sha256:" + "f" * 64,
            period="1d",
            adjustment="none",
            quantity_shares=1000,
            commission_bps="2.5",
            minimum_commission="5",
            sell_tax_bps="5",
            slippage_bps="3",
            execution_rules_version="a-share-v1",
            cost_model_version="a-share-cost-v1",
            sizing_version="fixed-lot-v1",
            warmup_policy_version="formula-warmup-v1",
            metrics={"realized_count": 2, "win_rate": "0.5"},
            disclaimer="independent trade samples, not portfolio return",
        )

    def page(
        self,
        run_id: str,
        *,
        collection: str,
        limit: int,
        cursor: str | None,
    ) -> BacktestPage:
        assert run_id == RUN_ID
        assert (limit, cursor) == (1, None)
        items: dict[str, tuple[object, ...]] = {
            "groups": (
                BacktestGroupSnapshot("symbol", "600000.SH", {"win_rate": "0.5"}),
            ),
            "trades": (BacktestTradeSnapshot("600000.SH", 0, {"net_pnl": "10"}),),
            "open": (BacktestTradeSnapshot("000001.SZ", 1, {"floating_pnl": "2"}),),
            "failures": (
                BacktestFailureSnapshot("300001.SZ", 2, "missing_signal_data", {}),
            ),
            "logs": (BacktestLogSnapshot(0, "info", "run_completed", {}),),
        }
        return BacktestPage(items=items[collection], next_cursor=f"next-{collection}")

    def export(self, run_id: str, *, section: str, format: str):
        assert run_id == RUN_ID
        assert section == "trades"
        if format == "json":
            return iter((b'{"metadata":{},"rows":[]}',))
        return iter((b"record_type\nmetadata\n",))


def test_anonymous_create_returns_persisted_snapshot_run_and_task() -> None:
    services = _CreateServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        response = client.post("/api/backtests", json=_request())

    assert response.status_code == 202
    assert response.json() == {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "task_id": "22222222-2222-2222-2222-222222222222",
        "snapshot_id": "sha256:" + "a" * 64,
        "warnings": [],
    }
    assert services.intent is not None
    assert services.intent.formula_version_id == FORMULA_ID
    assert services.intent.scoring_start == datetime(2024, 1, 3, tzinfo=timezone.utc)


def test_anonymous_run_routes_are_bounded_and_copy_mode_is_explicit() -> None:
    services = _ReadServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        listed = client.get("/api/backtests", params={"limit": 1})
        overview = client.get(f"/api/backtests/{RUN_ID}")
        cancelled = client.post(f"/api/backtests/{RUN_ID}/cancel")
        exact = client.post(f"/api/backtests/{RUN_ID}/copy", json={"mode": "exact"})
        latest = client.post(f"/api/backtests/{RUN_ID}/copy", json={"mode": "latest"})
        implicit = client.post(f"/api/backtests/{RUN_ID}/copy", json={})

    assert listed.status_code == 200
    assert listed.json()["next_cursor"] == "next-run"
    assert len(listed.json()["items"]) == 1
    assert overview.status_code == 200
    assert overview.json()["progress"] == 1.0
    assert cancelled.status_code == 202
    assert exact.status_code == latest.status_code == 202
    assert exact.json()["snapshot_id"] == SNAPSHOT_ID
    assert latest.json()["snapshot_id"] != SNAPSHOT_ID
    assert implicit.status_code == 422
    assert implicit.json() == {"code": "invalid_request"}


def test_report_and_each_result_collection_use_separate_cursor_pages() -> None:
    services = _ReadServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        report = client.get(f"/api/backtests/{RUN_ID}/report")
        pages = {
            name: client.get(f"/api/backtests/{RUN_ID}/{name}", params={"limit": 1})
            for name in ("groups", "trades", "open", "failures", "logs")
        }

    assert report.status_code == 200
    assert report.json()["formula_version_id"] == FORMULA_ID
    assert report.json()["provenance"] == {
        "instrument_dataset_version": "sha256:" + "e" * 64,
        "symbol_count": 3,
        "runnable_count": 2,
        "gap_count": 1,
        "source_ids": {
            "signal": ["tushare"],
            "execution": ["akshare"],
            "status": ["tdx_local"],
        },
        "digest": "sha256:" + "f" * 64,
    }
    assert report.json()["execution_rules_version"] == "a-share-v1"
    for name, response in pages.items():
        assert response.status_code == 200
        assert response.json()["next_cursor"] == f"next-{name}"
        assert len(response.json()["items"]) == 1


def test_export_route_streams_with_safe_fixed_headers() -> None:
    services = _ReadServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        json_response = client.get(f"/api/backtests/{RUN_ID}/export/trades.json")
        csv_response = client.get(f"/api/backtests/{RUN_ID}/export/trades.csv")

    assert json_response.status_code == csv_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert json_response.headers["x-content-type-options"] == "nosniff"
    assert json_response.headers["cache-control"] == "no-store"
    assert RUN_ID in json_response.headers["content-disposition"]
    assert "attachment" in json_response.headers["content-disposition"]


def test_preflight_is_explicitly_non_reserving_and_review_complete() -> None:
    services = _ReadServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        response = client.post("/api/backtests/preflight", json=_request())

    assert response.status_code == 200
    body = response.json()
    assert body["preview_snapshot_id"] == SNAPSHOT_ID
    assert body["reservation"] is False
    assert body["formula"]["compatibility_version"] == "tdx-v1"
    assert body["scope"] == {
        "kind": "single",
        "symbol": "600000.SH",
        "pool_id": None,
        "revision_or_snapshot_id": None,
        "total": 1,
        "runnable": 1,
        "gap_count": 0,
        "gap_sample": [],
        "gaps_truncated": False,
        "warnings": [],
    }
    assert body["quantity_shares"] == 1000
    assert body["costs"] == {
        "commission_bps": "2.5",
        "minimum_commission": "5",
        "sell_tax_bps": "5",
        "slippage_bps": "3",
    }
    assert body["warmup"]["unbounded_dependency"] is True
    assert body["coverage"] == {"signal": 1, "execution": 1, "status": 1}
    assert body["estimated_workload"]["formula_rows"] == 30
    assert "run_id" not in body and "task_id" not in body


def test_intent_rejects_inexact_costs_naive_time_unknown_fields_and_bools() -> None:
    services = _ReadServices()
    invalid_requests: list[dict[str, object]] = []
    for field, value in (
        ("commission_bps", 2.5),
        ("minimum_commission", True),
        ("scoring_start", "2024-01-03T00:00:00"),
        ("quantity_shares", True),
    ):
        request = _request()
        request[field] = value
        invalid_requests.append(request)
    extra = _request()
    extra["broker_account"] = "must-never-exist"
    invalid_requests.append(extra)

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        responses = [
            client.post("/api/backtests/preflight", json=item)
            for item in invalid_requests
        ]

    assert all(response.status_code == 422 for response in responses)
    assert all(response.json() == {"code": "invalid_request"} for response in responses)
    assert services.intent is None


def test_huge_integer_parameter_is_uniform_422_without_service_call() -> None:
    services = _ReadServices()
    request = _request()
    request["formula_parameters"] = {"N": 10**400}

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        response = client.post("/api/backtests/preflight", json=request)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}
    assert services.intent is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("formula_version_id", " " + FORMULA_ID),
        ("formula_version_id", "formula version"),
        ("commission_bps", " 2.5"),
        ("commission_bps", "+2.5"),
        ("commission_bps", "02.500"),
        ("commission_bps", "2.50"),
        ("commission_bps", "0.0"),
        ("commission_bps", "2e0"),
        ("commission_bps", "-0"),
        ("scoring_start", 1_700_000_000),
        ("scoring_start", True),
        ("scoring_start", "2024-01-03T00:00:00"),
        ("scoring_end", "2024-01-03T00:00:00Z"),
        ("quantity_shares", 99),
        ("quantity_shares", 100_000_100),
    ],
)
def test_strict_intent_rejects_noncanonical_values_before_service(
    field: str, value: object
) -> None:
    services = _ReadServices()
    request = _request()
    request[field] = value

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        responses = (
            client.post("/api/backtests/preflight", json=request),
            client.post("/api/backtests", json=request),
        )

    assert all(response.status_code == 422 for response in responses)
    assert all(response.json() == {"code": "invalid_request"} for response in responses)
    assert services.intent is None


@pytest.mark.parametrize(
    "scope",
    [
        {
            "kind": "preset",
            "pool_id": " preset:all-a",
            "snapshot_id": "sha256:" + "a" * 64,
        },
        {
            "kind": "preset",
            "pool_id": "preset:all-a",
            "snapshot_id": "sha256:" + "A" * 64,
        },
        {"kind": "preset", "pool_id": "preset:all-a", "snapshot_id": "x" * 71},
        {"kind": "custom", "pool_id": "custom pool", "revision": 1},
        {"kind": "custom", "pool_id": "custom-1", "revision": 0},
    ],
)
def test_scope_identity_is_canonical_before_service(scope: object) -> None:
    services = _ReadServices()
    request = _request()
    request["scope"] = scope

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        response = client.post("/api/backtests/preflight", json=request)

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}
    assert services.intent is None


def test_valid_fixed_point_and_offset_timestamp_are_normalized() -> None:
    services = _ReadServices()
    request = _request()
    request["commission_bps"] = "2.05"
    request["scoring_start"] = "2024-01-03T08:00:00+08:00"

    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        response = client.post("/api/backtests/preflight", json=request)

    assert response.status_code == 200
    assert response.json()["costs"]["commission_bps"] == "2.5"
    assert services.intent is not None
    assert services.intent.commission_bps == Decimal("2.05")
    assert services.intent.scoring_start == datetime(2024, 1, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (FormulaNotFound("private"), 404, "not_found"),
        (PoolNotFound("private"), 404, "not_found"),
        (PoolRevisionConflict("private"), 409, "state_conflict"),
        (RuntimeError("/private/storage"), 503, "service_unavailable"),
    ],
)
def test_preflight_domain_errors_have_stable_public_mapping(
    error: Exception, status_code: int, code: str
) -> None:
    class RaisingServices(_ReadServices):
        def preflight(self, intent: BacktestIntent) -> BacktestPreflight:
            self.intent = intent
            raise error

    services = RaisingServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        response = client.post("/api/backtests/preflight", json=_request())

    assert response.status_code == status_code
    assert response.json() == {"code": code}
    assert "private" not in response.text


def test_large_pool_preflight_gap_sample_is_bounded_and_explicitly_truncated() -> None:
    class LargePoolServices(_ReadServices):
        def preflight(self, intent: BacktestIntent) -> BacktestPreflight:
            base = super().preflight(intent)
            return replace(
                base,
                scope_kind="preset",
                symbol=None,
                scope_id="preset:all-a",
                scope_revision_or_snapshot_id="sha256:" + "a" * 64,
                total=10_000,
                runnable=1,
                gap_count=9_999,
                gap_sample=tuple(
                    (f"{ordinal:06d}.SZ", "missing_signal_data")
                    for ordinal in range(1, 101)
                ),
            )

    services = LargePoolServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        response = client.post("/api/backtests/preflight", json=_request())

    assert response.status_code == 200
    scope = response.json()["scope"]
    assert scope["gap_count"] == 9_999
    assert len(scope["gap_sample"]) == 100
    assert scope["gaps_truncated"] is True


@pytest.mark.parametrize("run_id", ["☃", "A" * 36, "not-a-uuid"])
def test_run_routes_reject_noncanonical_identity_before_service(run_id: str) -> None:
    class NeverReadServices(_ReadServices):
        def get_overview(self, run_id: str) -> BacktestOverviewSnapshot:
            raise AssertionError("service must not be called")

    with TestClient(create_app(backtest_services=NeverReadServices())) as client:  # type: ignore[arg-type]
        response = client.get(f"/api/backtests/{run_id}")

    assert response.status_code == 422
    assert response.json() == {"code": "invalid_request"}


def test_backtest_openapi_is_bounded_anonymous_and_broker_free() -> None:
    services = _ReadServices()
    with TestClient(create_app(backtest_services=services)) as client:  # type: ignore[arg-type]
        document = client.get("/openapi.json").json()

    assert "securitySchemes" not in document.get("components", {})
    backtest_paths = {
        path: operations
        for path, operations in document["paths"].items()
        if path.startswith("/api/backtests")
    }
    assert backtest_paths
    assert "broker" not in json.dumps(backtest_paths).lower()
    assert "claim_token" not in json.dumps(backtest_paths)
    for operations in backtest_paths.values():
        for operation in operations.values():
            assert "security" not in operation
            if "422" in operation["responses"]:
                schema = operation["responses"]["422"]["content"]["application/json"][
                    "schema"
                ]
                assert schema["$ref"].endswith("/BacktestErrorResponse")
    components = document["components"]["schemas"]
    assert components["BacktestListResponse"]["properties"]["items"]["maxItems"] == 100
    assert (
        components["BacktestCreateRequest"]["properties"]["formula_parameters"][
            "maxProperties"
        ]
        == 64
    )
    assert (
        components["BacktestPreflightScopeResponse"]["properties"]["gap_sample"][
            "maxItems"
        ]
        == 100
    )
    for category in ("signal", "execution", "status"):
        assert (
            components["BacktestSourceSummaryResponse"]["properties"][category][
                "maxItems"
            ]
            == 5
        )
    assert (
        components["BacktestSymbolPageResponse"]["properties"]["items"]["maxItems"]
        == 100
    )
