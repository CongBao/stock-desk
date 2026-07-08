from __future__ import annotations

from pathlib import Path

import pytest

from stock_desk.formula.repository import FormulaNotFound, FormulaRepository
from stock_desk.formula.service import FormulaService
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
    with market_api(tmp_path) as context:
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
        preview = context.client.post(
            f"/api/formulas/{formula_id}/preview",
            json={
                "symbol": "600000.SH",
                "period": "1d",
                "adjustment": "none",
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-02-01T00:00:00Z",
                "parameters": {},
            },
        )
        persisted = context.client.get(f"/api/formulas/{formula_id}")
        versions = context.client.get(f"/api/formulas/{formula_id}/versions")
        formulas = FormulaService(
            repository=FormulaRepository(context.services.engine),
            lake=context.services.lake,
        )
        with pytest.raises(FormulaNotFound):
            formulas.preflight_backtest(formula_id, {})

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
    assert preview.status_code == 404
    assert preview.json() == {"code": "not_found"}
    assert persisted.json()["draft"]["source"] == source
    assert (
        persisted.json()["draft"]["parameter_schema"]
        == draft.json()["parameter_schema"]
    )
    assert persisted.json()["draft"]["executable_version_id"] is None
    assert [item["version"] for item in versions.json()["items"]] == [1]
