from __future__ import annotations

from pathlib import Path
from datetime import date

import pytest

from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from tests.integration.market.lake_test_helpers import routed_daily_bars
from tests.unit.api.test_market_api import market_api


@pytest.mark.parametrize(
    ("source", "parameter_schema", "expected_code"),
    [
        ("BUY:C>;SELL:C<0;", {}, "formula_syntax_error"),
        ("BUY:UNKNOWN(C)>0;SELL:C<0;", {}, "unsupported_function"),
        (
            "BUY:MA(C,N)>0;SELL:C<0;",
            {"N": {"kind": "integer", "default": 0}},
            "argument_out_of_range",
        ),
    ],
)
def test_all_validation_stages_block_invalid_save_preview_and_backtest_while_preserving_draft(
    tmp_path: Path,
    source: str,
    parameter_schema: dict[str, object],
    expected_code: str,
) -> None:
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    with market_api(tmp_path) as context:
        context.services.lake.write(routed)
        created = context.client.post(
            "/api/formulas",
            json={
                "name": f"校验边界-{expected_code}",
                "formula_type": "trading",
                "placement": "subchart",
                "source": "BUY:C>0;SELL:C<0;",
                "parameter_schema": {},
            },
        )
        assert created.status_code == 201
        formula_id = created.json()["id"]
        revision = created.json()["draft"]["revision"]
        control_version_id = created.json()["draft"]["executable_version_id"]
        control_preview = context.client.post(
            f"/api/formulas/{control_version_id}/preview",
            json={
                "symbol": routed.result.query.symbol,
                "period": routed.result.query.period.value,
                "adjustment": routed.result.query.adjustment.value,
                "start": routed.result.query.start.isoformat(),
                "end": routed.result.query.end.isoformat(),
                "parameters": {},
            },
        )
        formulas = FormulaService(
            repository=FormulaRepository(context.services.engine),
            lake=context.services.lake,
        )
        control_backtest = formulas.preflight_backtest(control_version_id, {})
        validated = context.client.post(
            "/api/formulas/validate",
            json={
                "source": source,
                "parameter_schema": parameter_schema,
                "formula_type": "trading",
            },
        )

        draft = context.client.put(
            f"/api/formulas/{formula_id}/draft",
            json={
                "expected_revision": revision,
                "source": source,
                "parameter_schema": parameter_schema,
            },
        )
        saved = context.client.post(
            f"/api/formulas/{formula_id}/save",
            json={
                "expected_revision": draft.json()["revision"],
                "source": source,
                "parameter_schema": parameter_schema,
            },
        )
        persisted = context.client.get(f"/api/formulas/{formula_id}")
        versions = context.client.get(f"/api/formulas/{formula_id}/versions")

    assert control_preview.status_code == 200
    assert control_preview.json()["formula_version_id"] == control_version_id
    assert control_backtest.formula_version_id == control_version_id
    assert validated.status_code == 200
    assert validated.json()["valid"] is False
    assert validated.json()["diagnostics"][0]["code"] == expected_code
    assert all(
        validated.json()["diagnostics"][0][field]
        for field in ("blocks_save", "blocks_preview", "blocks_backtest")
    )
    assert draft.status_code == 200
    assert draft.json()["source"] == source
    for name, declaration in parameter_schema.items():
        assert draft.json()["parameter_schema"][name]["kind"] == declaration["kind"]  # type: ignore[index]
        assert (
            draft.json()["parameter_schema"][name]["default"] == declaration["default"]
        )  # type: ignore[index]
    assert draft.json()["executable_version_id"] is None
    assert draft.json()["diagnostics"][0]["code"] == expected_code
    assert all(
        draft.json()["diagnostics"][0][field]
        for field in ("blocks_save", "blocks_preview", "blocks_backtest")
    )
    assert saved.status_code == 422
    assert saved.json() == {"code": "formula_invalid"}
    assert persisted.json()["draft"]["source"] == source
    assert (
        persisted.json()["draft"]["parameter_schema"]
        == draft.json()["parameter_schema"]
    )
    assert persisted.json()["draft"]["executable_version_id"] is None
    assert [item["version"] for item in versions.json()["items"]] == [1]
