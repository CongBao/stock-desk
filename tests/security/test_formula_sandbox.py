from __future__ import annotations

import builtins
from datetime import date
from pathlib import Path
import socket
import subprocess

import pytest

from stock_desk.api.market import MarketServices
from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaService
from stock_desk.formula.validator import FormulaValidator
from stock_desk.storage.database import create_engine_for_url, migrate
from tests.integration.market.lake_test_helpers import routed_daily_bars


ATTACK_SOURCES = (
    "X:__IMPORT__('os');",
    "X:OPEN('/etc/passwd');",
    "X:CLOSE.__CLASS__;",
    "X:$(whoami);",
    "X:EVAL('1+1');",
    "X:EXEC('pass');",
    "X:SOCKET('attacker.invalid',80);",
)
VALID_SOURCE = (
    "DIF:EMA(CLOSE,12)-EMA(CLOSE,26);"
    "DEA:EMA(DIF,9);"
    "BUY:CROSS(DIF,DEA);"
    "SELL:CROSS(DEA,DIF);"
)


@pytest.mark.parametrize("source", ATTACK_SOURCES)
def test_formula_host_access_is_rejected_without_file_process_or_network_side_effects(
    source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = FormulaValidator()
    assert validator.validate(VALID_SOURCE) == ()
    side_effects: list[str] = []

    def blocked_file(*_args: object, **_kwargs: object) -> object:
        side_effects.append("file")
        raise AssertionError("formula validation attempted file access")

    def blocked_process(*_args: object, **_kwargs: object) -> object:
        side_effects.append("process")
        raise AssertionError("formula validation attempted process execution")

    def blocked_network(*_args: object, **_kwargs: object) -> object:
        side_effects.append("network")
        raise AssertionError("formula validation attempted network access")

    monkeypatch.setattr(builtins, "open", blocked_file)
    monkeypatch.setattr(subprocess, "Popen", blocked_process)
    monkeypatch.setattr(socket, "socket", blocked_network)

    diagnostics = validator.validate(source)

    assert diagnostics
    assert all(item.blocks_save for item in diagnostics)
    assert all(item.blocks_preview for item in diagnostics)
    assert all(item.blocks_backtest for item in diagnostics)
    assert side_effects == []
    assert validator.validate(VALID_SOURCE) == ()


def test_real_formula_preview_executes_before_and_after_hostile_validation_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'formula-security-preview.db'}"
    migrate(database_url)
    services = MarketServices(
        engine=create_engine_for_url(database_url),
        lake_root=(tmp_path / "market").resolve(),
    )
    routed = routed_daily_bars(
        (
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
        )
    )
    services.lake.write(routed)
    repository = FormulaRepository(services.engine)
    version = repository.create(
        "Security MACD",
        "trading",
        VALID_SOURCE,
        {},
        placement="subchart",
    )
    before = FormulaService(repository=repository, lake=services.lake).preview(
        version.id,
        routed.result.query,
        {},
    )
    side_effects: list[str] = []

    def blocked_file(*_args: object, **_kwargs: object) -> object:
        side_effects.append("file")
        raise AssertionError("hostile formula attempted file access")

    def blocked_process(*_args: object, **_kwargs: object) -> object:
        side_effects.append("process")
        raise AssertionError("hostile formula attempted process execution")

    def blocked_network(*_args: object, **_kwargs: object) -> object:
        side_effects.append("network")
        raise AssertionError("hostile formula attempted network access")

    monkeypatch.setattr(builtins, "open", blocked_file)
    monkeypatch.setattr(subprocess, "Popen", blocked_process)
    monkeypatch.setattr(socket, "socket", blocked_network)
    service = FormulaService(repository=repository, lake=services.lake)
    diagnostics = tuple(
        service.validate(source=source, parameter_schema={}, formula_type="trading")
        for source in ATTACK_SOURCES
    )
    assert all(items for items in diagnostics)
    assert side_effects == []
    monkeypatch.undo()

    try:
        after = FormulaService(repository=repository, lake=services.lake).preview(
            version.id,
            routed.result.query,
            {},
        )
    finally:
        services.close()

    assert before.signal_series_id == after.signal_series_id
    assert before.numeric_outputs == after.numeric_outputs
    assert before.signals == after.signals
