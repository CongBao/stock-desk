from __future__ import annotations

import builtins
import socket
import subprocess

import pytest

from stock_desk.formula.validator import FormulaValidator


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
