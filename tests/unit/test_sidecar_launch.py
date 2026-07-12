from pathlib import Path
import io
import secrets
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from stock_desk.desktop_session import DesktopLifecycleController
from stock_desk.sidecar import (
    SidecarLaunchConfig,
    await_bootstrap_gate,
    build_sidecar_settings,
    run_sidecar,
)


SOURCE_REVISION = "b" * 40


def test_sidecar_bootstrap_gate_accepts_only_the_host_release_byte() -> None:
    await_bootstrap_gate(io.BytesIO(b"\x01"))


@pytest.mark.parametrize("payload", [b"", b"\x00", b"x"])
def test_sidecar_bootstrap_gate_fails_closed_with_a_stable_redacted_error(
    payload: bytes,
) -> None:
    with pytest.raises(RuntimeError) as captured:
        await_bootstrap_gate(io.BytesIO(payload))

    assert str(captured.value) == "desktop sidecar bootstrap gate rejected"
    assert repr(payload) not in str(captured.value)


def test_sidecar_main_waits_at_gate_before_parsing_authority_or_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stock_desk.sidecar as sidecar_module

    events: list[str] = []

    config = object()

    def consume(_environment: object) -> object:
        events.append("consume")
        return config

    def run(actual: object) -> int:
        events.append("runtime")
        return 0 if actual is config else 1

    monkeypatch.setattr(
        sidecar_module.multiprocessing,
        "freeze_support",
        lambda: events.append("freeze_support"),
    )
    monkeypatch.setattr(
        sidecar_module.SidecarLaunchConfig,
        "consume",
        consume,
    )
    monkeypatch.setattr(
        sidecar_module,
        "await_bootstrap_gate",
        lambda _stream: events.append("gate"),
    )
    monkeypatch.setattr(
        sidecar_module,
        "run_sidecar",
        run,
    )

    assert sidecar_module.main() == 0
    assert events == ["freeze_support", "gate", "consume", "runtime"]


def test_sidecar_main_eof_cannot_parse_authority_or_start_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stock_desk.sidecar as sidecar_module

    events: list[str] = []

    def unexpected_consume(_environment: object) -> None:
        events.append("consume")

    def unexpected_run(_config: object) -> int:
        events.append("runtime")
        return 0

    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(b"")),
    )
    monkeypatch.setattr(
        sidecar_module.SidecarLaunchConfig,
        "consume",
        unexpected_consume,
    )
    monkeypatch.setattr(
        sidecar_module,
        "run_sidecar",
        unexpected_run,
    )

    with pytest.raises(RuntimeError, match="bootstrap gate rejected"):
        sidecar_module.main()

    assert events == []


def _environment(root: Path) -> dict[str, str]:
    return {
        "STOCK_DESK_DESKTOP_PORT": "49152",
        "STOCK_DESK_DESKTOP_ORIGIN": "http://tauri.localhost",
        "STOCK_DESK_DESKTOP_SESSION_SECRET": secrets.token_urlsafe(32),
        "STOCK_DESK_DESKTOP_DATA_ROOT": str(root),
        "STOCK_DESK_DESKTOP_HOST_VERSION": "1.1.0",
        "STOCK_DESK_DESKTOP_FRONTEND_VERSION": "1.1.0",
        "STOCK_DESK_DESKTOP_SIDECAR_VERSION": "1.1.0",
        "STOCK_DESK_DESKTOP_SOURCE_REVISION": SOURCE_REVISION,
    }


def test_sidecar_consumes_secret_from_inherited_environment_without_argv(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "Stock Desk" / "v1.1")
    secret = environment["STOCK_DESK_DESKTOP_SESSION_SECRET"]

    config = SidecarLaunchConfig.consume(environment)

    assert config.host == "127.0.0.1"
    assert config.port == 49152
    assert config.data_root == tmp_path / "Stock Desk" / "v1.1"
    assert config.session.authorizes(f"Bearer {secret}")
    assert "STOCK_DESK_DESKTOP_SESSION_SECRET" not in environment
    assert secret not in repr(config)


def test_sidecar_settings_keep_every_mutable_path_in_the_v11_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Local App Data" / "Stock Desk" / "v1.1"
    config = SidecarLaunchConfig.consume(_environment(root))

    settings = build_sidecar_settings(config, master_key="test-master-key")

    assert settings.data_dir == root
    assert settings.web_dist_dir is None
    assert settings.master_key is not None
    assert settings.master_key.get_secret_value() == "test-master-key"
    assert str(root / "stock-desk.db") in settings.database_url
    assert "stock-desk.db" in settings.database_url


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("STOCK_DESK_DESKTOP_PORT", "0"),
        ("STOCK_DESK_DESKTOP_PORT", "not-a-port"),
        ("STOCK_DESK_DESKTOP_ORIGIN", "http://evil.invalid"),
        ("STOCK_DESK_DESKTOP_DATA_ROOT", "relative/data"),
        ("STOCK_DESK_DESKTOP_DATA_ROOT", "/tmp/stock-desk"),
        ("STOCK_DESK_DESKTOP_HOST_VERSION", "1.1.1"),
        ("STOCK_DESK_DESKTOP_SOURCE_REVISION", "unknown"),
    ],
)
def test_sidecar_rejects_invalid_or_mismatched_host_authority(
    tmp_path: Path, key: str, value: str
) -> None:
    environment = _environment(tmp_path / "Stock Desk" / "v1.1")
    environment[key] = value

    with pytest.raises((RuntimeError, ValueError), match="desktop sidecar"):
        SidecarLaunchConfig.consume(environment)


def test_sidecar_rejects_missing_authority_without_echoing_variable_value(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path / "Stock Desk" / "v1.1")
    secret = environment.pop("STOCK_DESK_DESKTOP_SESSION_SECRET")

    with pytest.raises(RuntimeError) as captured:
        SidecarLaunchConfig.consume(environment)

    assert secret not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_desktop_lifecycle_cooperatively_stops_worker_and_uvicorn() -> None:
    lifecycle = DesktopLifecycleController()
    server = SimpleNamespace(should_exit=False)

    lifecycle.bind_server(server)
    lifecycle.request_shutdown()

    assert lifecycle.shutdown_requested is True
    assert lifecycle.stop_event.is_set()
    assert server.should_exit is True


def test_desktop_lifecycle_honors_shutdown_requested_before_server_binding() -> None:
    lifecycle = DesktopLifecycleController()
    server = SimpleNamespace(should_exit=False)

    lifecycle.request_shutdown()
    lifecycle.bind_server(server)

    assert server.should_exit is True


def test_desktop_lifecycle_stages_worker_stop_before_server_exit() -> None:
    lifecycle = DesktopLifecycleController()
    server = SimpleNamespace(should_exit=False)
    lifecycle.bind_server(server)

    lifecycle.begin_shutdown()

    assert lifecycle.shutdown_prepared is True
    assert lifecycle.shutdown_requested is True
    assert lifecycle.stop_event.is_set()
    assert server.should_exit is False

    lifecycle.complete_shutdown()

    assert server.should_exit is True


def test_desktop_lifecycle_prepare_does_not_stop_worker_before_response() -> None:
    lifecycle = DesktopLifecycleController()
    server = SimpleNamespace(should_exit=False)
    lifecycle.bind_server(server)

    lifecycle.prepare_shutdown()

    assert lifecycle.shutdown_prepared is True
    assert lifecycle.claim_stop_event.is_set() is True
    assert lifecycle.shutdown_requested is False
    assert lifecycle.stop_event.is_set() is False
    assert server.should_exit is False

    lifecycle.complete_shutdown()

    assert lifecycle.shutdown_requested is True
    assert server.should_exit is True


def test_sidecar_startup_failure_stops_worker_and_removes_runtime_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import stock_desk.desktop_runtime as desktop_runtime_module
    import stock_desk.main as main_module
    import stock_desk.market.worker_runtime as worker_module
    import stock_desk.storage.database as database_module

    root = tmp_path / "Stock Desk" / "v1.1"
    root.mkdir(parents=True)
    config = SidecarLaunchConfig.consume(_environment(root))
    record_path = root / "runtime.json"
    stop_observed: list[bool] = []
    worker_closed: list[bool] = []
    worker_sinks: list[Any] = []
    application_sinks: list[Any] = []

    class FakePaths:
        log_file = root / "sidecar.log"
        runtime_record = record_path

        @staticmethod
        def load_or_create_master_key() -> str:
            return "test-master-key"

        @staticmethod
        def write_runtime_record(_record: object) -> None:
            record_path.write_text("runtime", encoding="utf-8")

    class FakeWorker:
        def run_forever(
            self,
            stop_event: Any,
            *,
            ready_event: Any,
            claim_stop_event: Any,
        ) -> None:
            assert not claim_stop_event.is_set()
            ready_event.set()
            stop_event.wait(0.5)
            stop_observed.append(stop_event.is_set())

        def close(self) -> None:
            worker_closed.append(True)

    def open_worker(
        _settings: object,
        *,
        worker_id: str,
        diagnostic_event_sink: Any,
    ) -> FakeWorker:
        worker_sinks.append(diagnostic_event_sink)
        return FakeWorker()

    def fail_application(*_args: object, **kwargs: object) -> object:
        application_sinks.append(kwargs["diagnostic_event_sink"])
        raise RuntimeError("injected application startup failure")

    monkeypatch.setattr(
        desktop_runtime_module.RuntimePaths,
        "create",
        lambda _root: FakePaths(),
    )
    monkeypatch.setattr(database_module, "migrate", lambda _url: None)
    monkeypatch.setattr(
        worker_module.ProductionMarketWorker,
        "open",
        open_worker,
    )
    monkeypatch.setattr(
        main_module,
        "create_app",
        fail_application,
    )

    with pytest.raises(RuntimeError, match="application startup failure"):
        run_sidecar(config)

    assert stop_observed == [True]
    assert worker_closed == [True]
    assert worker_sinks == application_sinks
    assert len(worker_sinks) == 1
    assert [event.event_code for event in worker_sinks[0].event_buffer.snapshot()] == [
        "sidecar.starting",
        "storage.ready",
        "worker.starting",
        "worker.ready",
        "sidecar.runtime_failed",
        "sidecar.stopping",
        "worker.stopped",
    ]
    assert not record_path.exists()
