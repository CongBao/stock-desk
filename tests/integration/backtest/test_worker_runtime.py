from pathlib import Path

from stock_desk.config import Settings
from stock_desk.market.worker_runtime import ProductionMarketWorker


def test_production_worker_registers_recoverable_backtest_handler(
    tmp_path: Path,
) -> None:
    runtime = ProductionMarketWorker.open(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
            data_dir=tmp_path,
        ),
        worker_id="runtime-backtest",
    )
    try:
        assert "backtest.run" in runtime.worker.registered_claimed_kinds
    finally:
        runtime.close()
