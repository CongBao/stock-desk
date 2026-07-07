from __future__ import annotations

from pathlib import Path

import pytest

from stock_desk.formula.repository import FormulaRepository
from stock_desk.formula.service import FormulaPreviewValidationError, FormulaService
from stock_desk.market.lake import MarketLake
from stock_desk.storage.database import create_engine_for_url, migrate


MACD = (
    "DIF:EMA(C,12)-EMA(C,26);DEA:EMA(DIF,9);MACD:(DIF-DEA)*2;"
    "BUY:CROSS(DIF,DEA);SELL:CROSS(DEA,DIF);"
)


def _service(tmp_path: Path) -> tuple[FormulaService, FormulaRepository, object]:
    url = f"sqlite:///{tmp_path / 'formula-preflight.db'}"
    migrate(url)
    engine = create_engine_for_url(url)
    repository = FormulaRepository(engine)
    service = FormulaService(
        repository=repository,
        lake=MarketLake(engine=engine, root=(tmp_path / "market").resolve()),
    )
    return service, repository, engine


def test_macd_preflight_freezes_version_parameters_and_unbounded_dependency(
    tmp_path: Path,
) -> None:
    service, repository, engine = _service(tmp_path)
    try:
        version = repository.create(
            "MACD",
            "trading",
            MACD,
            {},
            placement="subchart",
        )

        frozen = service.preflight_backtest(version.id, {})

        assert frozen.formula_version_id == version.id
        assert frozen.formula_checksum == version.checksum
        assert frozen.normalized_parameters == ()
        assert frozen.lookback_bars is None
        assert frozen.unbounded_dependency is True
    finally:
        engine.dispose()  # type: ignore[union-attr]


def test_preflight_rejects_indicator_and_invalid_parameter_binding(
    tmp_path: Path,
) -> None:
    service, repository, engine = _service(tmp_path)
    try:
        indicator = repository.create(
            "均线",
            "indicator",
            "X:MA(C,N);",
            {"N": {"kind": "integer", "default": 5}},
            placement="subchart",
        )
        with pytest.raises(FormulaPreviewValidationError):
            service.preflight_backtest(indicator.id, {})

        trading = repository.create(
            "参数策略",
            "trading",
            "M:=MA(C,N);BUY:CROSS(C,M);SELL:CROSS(M,C);",
            {"N": {"kind": "integer", "default": 5}},
            placement="subchart",
        )
        with pytest.raises(FormulaPreviewValidationError):
            service.preflight_backtest(trading.id, {"N": 2.5})
    finally:
        engine.dispose()  # type: ignore[union-attr]
