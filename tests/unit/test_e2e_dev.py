from pathlib import Path
import json
import signal
import sys

import pytest

from scripts import e2e_dev
from stock_desk.formula.repository import FormulaRepository
from stock_desk.market.lake import MarketLake
from stock_desk.market.pools import PoolRepository
from stock_desk.market.types import Adjustment, Period
from stock_desk.storage.database import create_engine_for_url


def test_main_removes_temporary_profile_when_seed_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "seed-failure-profile"
    profile.mkdir()

    def fail_seed(data_dir: Path) -> None:
        assert data_dir == profile.resolve()
        (data_dir / "partial.db").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected seed failure")

    monkeypatch.setattr(sys, "argv", ["e2e_dev.py"])
    monkeypatch.setattr(e2e_dev.tempfile, "mkdtemp", lambda **_kwargs: str(profile))
    monkeypatch.setattr(e2e_dev, "_seed", fail_seed)
    monkeypatch.setattr(
        e2e_dev,
        "supervise",
        lambda *_args, **_kwargs: pytest.fail("services must not start"),
    )

    with pytest.raises(RuntimeError, match="injected seed failure"):
        e2e_dev.main()

    assert not profile.exists()


def test_main_honors_sigterm_received_during_seed_and_removes_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "sigterm-profile"
    profile.mkdir()

    def interrupt_seed(data_dir: Path) -> None:
        assert data_dir == profile.resolve()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    def stopped_supervisor(
        _commands: object,
        *,
        requested_signal: object,
    ) -> int:
        assert callable(requested_signal)
        assert requested_signal() == signal.SIGTERM
        return 128 + signal.SIGTERM

    monkeypatch.setattr(sys, "argv", ["e2e_dev.py"])
    monkeypatch.setattr(e2e_dev.tempfile, "mkdtemp", lambda **_kwargs: str(profile))
    monkeypatch.setattr(e2e_dev, "_seed", interrupt_seed)
    monkeypatch.setattr(e2e_dev, "supervise", stopped_supervisor)

    assert e2e_dev.main() == 128 + signal.SIGTERM
    assert not profile.exists()


def test_performance_harness_records_supervisor_and_service_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "performance-profile"
    profile.mkdir()
    pid_file = tmp_path / "processes.json"

    def fake_supervisor(
        _commands: object,
        *,
        requested_signal: object,
        on_started: object,
    ) -> int:
        assert callable(requested_signal)
        assert callable(on_started)
        processes = [
            type("Process", (), {"pid": 101})(),
            type("Process", (), {"pid": 202})(),
            type("Process", (), {"pid": 303})(),
        ]
        on_started(processes)
        return 0

    monkeypatch.setattr(sys, "argv", ["e2e_dev.py"])
    monkeypatch.setattr(e2e_dev.tempfile, "mkdtemp", lambda **_kwargs: str(profile))
    monkeypatch.setattr(e2e_dev, "_seed", lambda _data_dir: None)
    monkeypatch.setattr(e2e_dev, "supervise", fake_supervisor)
    monkeypatch.setenv("STOCK_DESK_PERFORMANCE_PROCESS_FILE", str(pid_file))

    assert e2e_dev.main() == 0
    payload = json.loads(pid_file.read_text(encoding="utf-8"))
    assert payload["supervisor_pid"] == e2e_dev.os.getpid()
    assert payload["service_pids"] == [101, 202, 303]
    assert payload["service_processes"][0]["pid"] == 101
    assert "uvicorn" in " ".join(payload["service_processes"][0]["command"])


def test_performance_seed_is_opt_in_and_default_demo_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_dir = tmp_path / "default"
    monkeypatch.delenv("STOCK_DESK_PERFORMANCE_MODE", raising=False)
    e2e_dev._seed(default_dir)
    default_engine = create_engine_for_url(f"sqlite:///{default_dir / 'stock-desk.db'}")
    try:
        default_lake = MarketLake(engine=default_engine, root=default_dir / "market")
        default_bars = default_lake.read_latest_series(
            "600000.SH", Period.DAY, Adjustment.QFQ
        )
        assert default_bars is not None
        assert len(default_bars.result.bars) == 475
        assert not any(
            formula.name.startswith("Performance MACD")
            for formula in FormulaRepository(default_engine).list_formulas()
        )
    finally:
        default_engine.dispose()

    performance_dir = tmp_path / "performance"
    monkeypatch.setenv("STOCK_DESK_PERFORMANCE_MODE", "1")
    e2e_dev._seed(performance_dir)
    performance_engine = create_engine_for_url(
        f"sqlite:///{performance_dir / 'stock-desk.db'}"
    )
    try:
        performance_lake = MarketLake(
            engine=performance_engine, root=performance_dir / "market"
        )
        performance_bars = performance_lake.read_latest_series(
            "600000.SH", Period.DAY, Adjustment.QFQ
        )
        assert performance_bars is not None
        assert len(performance_bars.result.bars) == 2_632
        assert (
            len(
                PoolRepository(performance_engine)
                .get_preset("performance-all-a")
                .members
            )
            == 12
        )
        assert (
            sum(
                formula.name.startswith("Performance MACD")
                for formula in FormulaRepository(performance_engine).list_formulas()
            )
            == 20
        )
    finally:
        performance_engine.dispose()
