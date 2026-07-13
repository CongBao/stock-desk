from pathlib import Path

from stock_desk.config import Settings
from stock_desk.diagnostics.models import DiagnosticEventSink
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
        assert "analysis.run" in runtime.worker.registered_claimed_kinds
    finally:
        runtime.close()


def test_production_worker_writes_safe_task_failures_to_shared_diagnostics(
    tmp_path: Path,
) -> None:
    sink = DiagnosticEventSink()
    runtime = ProductionMarketWorker.open(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'runtime-failure.db'}",
            data_dir=tmp_path,
        ),
        worker_id="runtime-diagnostic",
        diagnostic_event_sink=sink,
    )
    try:
        task = runtime.tasks.create("demo.double", {"value": "not-a-number"})

        completed = runtime.worker.run_once()

        assert completed is not None
        assert completed.id == task.id
        assert completed.status == "failed"
        assert [event.event_code for event in sink.event_buffer.snapshot()] == [
            "worker.task_failed"
        ]
        assert [event.failure_id for event in sink.event_buffer.snapshot()] == [
            "task_handler_failed"
        ]
    finally:
        runtime.close()
