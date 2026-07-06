from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.formula.service import MACD_TEMPLATE_SOURCE
from stock_desk.main import create_app
from stock_desk.market.provenance import (
    BarRoutingRequest,
    RoutedBarSuccess,
    make_routing_manifest,
)
from stock_desk.market.providers.normalization import dataset_version
from stock_desk.market.types import Adjustment, MarketCapability, TradingStatus
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _oscillating_daily_bars() -> Any:
    start = date(2024, 1, 2)
    days = tuple(start + timedelta(days=index) for index in range(120))
    routed = routed_daily_bars(days, adjustment=Adjustment.NONE)
    bars = []
    previous = Decimal("10")
    for index, bar in enumerate(routed.result.bars):
        phase = index % 20
        wave = phase if phase <= 10 else 20 - phase
        close = Decimal("9.5") + Decimal(wave) / Decimal("10")
        bars.append(
            bar.model_copy(
                update={
                    "open": previous,
                    "high": max(previous, close) + Decimal("0.2"),
                    "low": min(previous, close) - Decimal("0.2"),
                    "close": close,
                    "status": TradingStatus.NORMAL,
                    "volume": 10_000 + index,
                }
            )
        )
        previous = close
    result = routed.result.model_copy(update={"bars": tuple(bars)})
    version = dataset_version(
        source=result.provenance.source,
        operation="bars",
        request={"query": result.query},
        data_cutoff=result.provenance.data_cutoff,
        items=result.bars,
    )
    provenance = result.provenance.model_copy(update={"dataset_version": version})
    result = result.model_copy(update={"provenance": provenance})
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=result.query),
        priority=(provenance.source,),
        attempts=(),
        selected_source=provenance.source,
        upstream_dataset_version=version,
        upstream_fetched_at=provenance.fetched_at,
        upstream_data_cutoff=provenance.data_cutoff,
        upstream_adjustment=provenance.adjustment,
    )
    return RoutedBarSuccess(result=result, manifest=manifest)


def _preview_request(routed: Any) -> dict[str, object]:
    query = routed.result.query
    return {
        "symbol": query.symbol,
        "period": query.period.value,
        "adjustment": query.adjustment.value,
        "start": query.start.isoformat(),
        "end": query.end.isoformat(),
        "parameters": {},
    }


def test_macd_template_preview_versioning_and_safety_are_one_flow(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'macd-acceptance.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    routed = _oscillating_daily_bars()
    services.lake.write(routed)
    try:
        with TestClient(
            create_app(
                Settings(database_url=database_url, data_dir=tmp_path),
                market_services=services,
            )
        ) as client:
            templates = client.get("/api/formulas/templates")
            assert templates.status_code == 200
            template = templates.json()["items"][0]
            assert template["source"] == MACD_TEMPLATE_SOURCE
            assert template["formula_type"] == "trading"
            assert template["placement"] == "subchart"

            validated = client.post(
                "/api/formulas/validate",
                json={
                    "source": template["source"],
                    "parameter_schema": template["parameter_schema"],
                    "formula_type": template["formula_type"],
                },
            )
            assert validated.status_code == 200
            assert validated.json() == {"valid": True, "diagnostics": []}

            created = client.post(
                "/api/formulas",
                json={
                    "name": "MACD Stage 2 acceptance",
                    "formula_type": template["formula_type"],
                    "placement": template["placement"],
                    "source": template["source"],
                    "parameter_schema": template["parameter_schema"],
                },
            )
            assert created.status_code == 201
            detail = created.json()
            formula_id = detail["id"]
            version_one_id = detail["draft"]["executable_version_id"]
            assert detail["latest_version"] == 1

            request = _preview_request(routed)
            preview = client.post(
                f"/api/formulas/{version_one_id}/preview", json=request
            )
            chart = client.get(
                "/api/market/bars",
                params={
                    key: value for key, value in request.items() if key != "parameters"
                }
                | {"formula_version_id": version_one_id},
            )
            assert preview.status_code == 200
            assert chart.status_code == 200
            payload = preview.json()
            assert chart.json()["formula"] == payload
            assert [item["name"] for item in payload["numeric_outputs"]] == [
                "DIF",
                "DEA",
                "MACD",
            ]
            signals = {item["name"]: item["values"] for item in payload["signals"]}
            assert any(value is True for value in signals["BUY"])
            assert any(value is True for value in signals["SELL"])

            version_two_source = f"{MACD_TEMPLATE_SOURCE}REFERENCE:C;"
            saved = client.post(
                f"/api/formulas/{formula_id}/save",
                json={
                    "source": version_two_source,
                    "parameter_schema": {},
                    "expected_revision": detail["draft"]["revision"],
                },
            )
            assert saved.status_code == 201
            assert saved.json()["version"] == 2
            versions = client.get(f"/api/formulas/{formula_id}/versions")
            assert versions.status_code == 200
            version_items = versions.json()["items"]
            assert [(item["version"], item["source"]) for item in version_items] == [
                (1, MACD_TEMPLATE_SOURCE),
                (2, version_two_source),
            ]

            for source, expected_code in (
                ("X:UNKNOWN(C);", "unsupported_function"),
                ("X:REF(C,-1);", "future_data"),
            ):
                rejected = client.post(
                    "/api/formulas/validate",
                    json={
                        "source": source,
                        "parameter_schema": {},
                        "formula_type": "indicator",
                    },
                )
                assert rejected.status_code == 200
                body = rejected.json()
                assert body["valid"] is False
                assert body["diagnostics"][0]["code"] == expected_code
                assert body["diagnostics"][0]["blocks_save"] is True
                assert body["diagnostics"][0]["blocks_preview"] is True

            for excluded_type in ("selection", "color_k"):
                excluded = client.post(
                    "/api/formulas",
                    json={
                        "name": excluded_type,
                        "formula_type": excluded_type,
                        "placement": "subchart",
                        "source": "X:C;",
                        "parameter_schema": {},
                    },
                )
                assert excluded.status_code == 422
            openapi_paths = client.get("/openapi.json").json()["paths"]
            assert not any(
                "formula" in path and "ai" in path.casefold() for path in openapi_paths
            )
    finally:
        services.close()
