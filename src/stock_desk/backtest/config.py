from __future__ import annotations

from stock_desk.backtest.types import _BacktestInputs


class BacktestRequest(_BacktestInputs):
    """Validated user-selected inputs that will be frozen into a snapshot."""
