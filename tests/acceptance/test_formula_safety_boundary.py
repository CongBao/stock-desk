from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

import pytest

import stock_desk.formula.repository as repository_module
from stock_desk.formula.functions import V1_REGISTRY
from stock_desk.formula.functions.base import FutureBehavior
from stock_desk.formula.functions.registry import CompatibilityRegistry
from stock_desk.formula.repository import FormulaRepository, FormulaValidationError
from stock_desk.formula.service import FormulaService
from stock_desk.formula.validator import FormulaValidator
from tests.unit.api.test_market_api import market_api
from tests.integration.market.lake_test_helpers import routed_daily_bars


def _repainting_registry() -> CompatibilityRegistry:
    return CompatibilityRegistry(
        version="tdx-repainting-contract-v1",
        functions=tuple(
            replace(
                specification,
                future_behavior=cast(FutureBehavior, "repainting"),
            )
            if specification.name == "ABS"
            else specification
            for specification in V1_REGISTRY.functions()
        ),
        fields=V1_REGISTRY.fields(),
    )


def test_future_or_repainting_formula_cannot_be_saved_or_backtested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        ("future_data", "BUY:REF(C,-1)>0;SELL:C<0;", None),
        ("repainting", "BUY:ABS(C)>0;SELL:C<0;", _repainting_registry()),
    )
    routed = routed_daily_bars((date(2024, 1, 2), date(2024, 1, 3)))
    with market_api(tmp_path) as context:
        context.services.lake.write(routed)
        for expected_code, source, registry in cases:
            if registry is None:
                monkeypatch.setattr(
                    repository_module, "FormulaValidator", FormulaValidator
                )
            else:
                monkeypatch.setattr(
                    repository_module,
                    "FormulaValidator",
                    lambda: FormulaValidator(registry),
                )
            repository = FormulaRepository(context.services.engine)
            published = repository.create(
                f"安全边界-{expected_code}",
                "trading",
                "BUY:C>0;SELL:C<0;",
                {},
                placement="subchart",
            )
            service = FormulaService(
                repository=repository,
                lake=context.services.lake,
            )
            control_preview = service.preview_routed(published.id, routed, {})
            control_backtest = service.preflight_backtest(published.id, {})
            assert control_preview.formula_version_id == published.id
            assert control_backtest.formula_version_id == published.id
            original = repository.get_draft(published.formula_id)
            draft = repository.update_draft(
                published.formula_id,
                source,
                {},
                expected_revision=original.revision,
            )

            assert draft.source == source
            assert draft.executable_version_id is None
            assert draft.validation_result[0]["code"] == expected_code
            assert all(
                draft.validation_result[0][field]
                for field in ("blocks_save", "blocks_preview", "blocks_backtest")
            )
            with pytest.raises(FormulaValidationError):
                repository.save(
                    published.formula_id,
                    source,
                    {},
                    expected_revision=draft.revision,
                )
            persisted = repository.get_draft(published.formula_id)
            assert persisted.source == source
            assert persisted.executable_version_id is None
            assert [
                item.version for item in repository.list_versions(published.formula_id)
            ] == [1]
            # Preview and backtest accept immutable version IDs. The invalid draft
            # has deliberately lost that capability, so no draft request can be
            # formed; the prior valid version remains an explicit legal control.
            assert persisted.executable_version_id is None
