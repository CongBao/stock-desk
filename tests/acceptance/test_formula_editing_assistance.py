from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_desk.api.market import MarketServices
from stock_desk.config import Settings
from stock_desk.main import create_app
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.acceptance.test_macd_formula_flow import (
    _oscillating_daily_bars,
    _preview_request,
)


def test_highlight_hints_templates_preview_save_and_copy(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-assistance.db'}"
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
            catalog_response = client.get("/api/formulas/functions")
            assert catalog_response.status_code == 200
            catalog = catalog_response.json()
            ema = next(item for item in catalog["functions"] if item["name"] == "EMA")
            assert ema["signature"] == "EMA(X, N)"
            assert ema["summary_zh"]
            assert ema["semantics_zh"]
            assert catalog["compatibility_version"] == "tdx-v1"

            template_response = client.get("/api/formulas/templates")
            assert template_response.status_code == 200
            template = next(
                item
                for item in template_response.json()["items"]
                if "MACD" in item["name"]
            )
            assert template["formula_type"] == "trading"
            assert template["placement"] == "subchart"

            invalid = client.post(
                "/api/formulas/validate",
                json={
                    "source": "X:UNKNOWN(C);",
                    "parameter_schema": {},
                    "formula_type": "indicator",
                },
            )
            assert invalid.status_code == 200
            diagnostic = invalid.json()["diagnostics"][0]
            assert diagnostic["code"] == "unsupported_function"
            assert diagnostic["span"]["line"] == 1
            assert diagnostic["span"]["column"] == 3

            created_response = client.post(
                "/api/formulas",
                json={
                    "name": "Formula assistance acceptance",
                    "formula_type": template["formula_type"],
                    "placement": template["placement"],
                    "source": template["source"],
                    "parameter_schema": template["parameter_schema"],
                },
            )
            assert created_response.status_code == 201
            created = created_response.json()
            formula_id = created["id"]
            version_id = created["draft"]["executable_version_id"]
            assert created["latest_version"] == 1

            request = _preview_request(routed)
            preview = client.post(f"/api/formulas/{version_id}/preview", json=request)
            assert preview.status_code == 200
            preview_body = preview.json()
            assert preview_body["symbol"] == routed.result.query.symbol
            assert [item["name"] for item in preview_body["numeric_outputs"]] == [
                "DIF",
                "DEA",
                "MACD",
            ]
            assert {item["name"] for item in preview_body["signals"]} == {
                "BUY",
                "SELL",
            }

            copied_response = client.post(
                f"/api/formulas/{formula_id}/copy",
                json={
                    "name": "Formula assistance independent copy",
                    "source_version_id": version_id,
                },
            )
            assert copied_response.status_code == 201
            copied = copied_response.json()
            assert copied["formula_id"] != formula_id
            assert copied["version"] == 1
            assert copied["source"] == template["source"]
            original_versions = client.get(
                f"/api/formulas/{formula_id}/versions"
            ).json()["items"]
            copied_versions = client.get(
                f"/api/formulas/{copied['formula_id']}/versions"
            ).json()["items"]
            assert [item["id"] for item in original_versions] == [version_id]
            assert [item["id"] for item in copied_versions] == [copied["id"]]

            formula_page = client.get("/api/formulas").json()["items"]
            assert {item["name"] for item in formula_page} >= {
                "Formula assistance acceptance",
                "Formula assistance independent copy",
            }
    finally:
        services.close()
