from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
from types import ModuleType
from typing import Any
from urllib.request import urlopen

import pytest

from stock_desk.analysis.sources import akshare as akshare_source_module


def _desktop() -> ModuleType:
    try:
        return importlib.import_module("stock_desk.desktop")
    except ModuleNotFoundError:
        pytest.fail("stock_desk.desktop packaged entrypoint is missing")


@pytest.mark.parametrize(
    ("platform_name", "environment", "home", "expected"),
    [
        (
            "Windows",
            {"LOCALAPPDATA": r"C:\\Users\\owner\\AppData\\Local"},
            Path(r"C:\\Users\\owner"),
            Path(r"C:\\Users\\owner\\AppData\\Local") / "stock-desk",
        ),
        (
            "Darwin",
            {},
            Path("/Users/owner"),
            Path("/Users/owner/Library/Application Support/stock-desk"),
        ),
        (
            "Linux",
            {"XDG_DATA_HOME": "/home/owner/.xdg-data"},
            Path("/home/owner"),
            Path("/home/owner/.xdg-data/stock-desk"),
        ),
    ],
)
def test_expected_platform_data_dir_is_private_per_user(
    platform_name: str,
    environment: dict[str, str],
    home: Path,
    expected: Path,
) -> None:
    desktop = _desktop()

    actual = desktop.expected_platform_data_dir(
        "stock-desk",
        platform_name=platform_name,
        environment=environment,
        home=home,
    )

    assert actual == expected


def test_reserved_api_socket_is_local_dynamic_and_already_owned() -> None:
    desktop = _desktop()

    reserved = desktop.reserve_api_socket()
    try:
        assert reserved.host == "127.0.0.1"
        assert reserved.port > 0
        assert reserved.socket.getsockname() == (reserved.host, reserved.port)

        contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError):
                contender.bind((reserved.host, reserved.port))
        finally:
            contender.close()
    finally:
        reserved.socket.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable")
def test_runtime_directory_lock_and_record_are_owner_only(tmp_path: Path) -> None:
    desktop = _desktop()
    paths = desktop.RuntimePaths.create(tmp_path / "stock-desk")

    first = desktop.SingleInstanceLock(paths.lock_file)
    first.acquire()
    try:
        with pytest.raises(desktop.AlreadyRunningError):
            desktop.SingleInstanceLock(paths.lock_file).acquire()

        paths.write_runtime_record(
            desktop.RuntimeRecord(
                pid=1234,
                host="127.0.0.1",
                port=43210,
                data_dir=paths.data_dir,
                log_file=paths.log_file,
            )
        )
        record = json.loads(paths.runtime_record.read_text(encoding="utf-8"))
        assert record["pid"] == 1234
        assert record["host"] == "127.0.0.1"
        assert record["port"] == 43210
        assert record["data_dir"] == str(paths.data_dir)
        assert record["log_file"] == str(paths.log_file)
        assert stat.S_IMODE(paths.runtime_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(paths.lock_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(paths.runtime_record.stat().st_mode) == 0o600
    finally:
        first.release()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable")
def test_generated_master_key_is_stable_and_owner_only(tmp_path: Path) -> None:
    desktop = _desktop()
    paths = desktop.RuntimePaths.create(tmp_path / "stock-desk")

    generated = paths.load_or_create_master_key()

    assert len(generated) >= 32
    assert paths.load_or_create_master_key() == generated
    assert stat.S_IMODE(paths.master_key_file.stat().st_mode) == 0o600


def test_launcher_uses_private_os_data_dir_localhost_and_dynamic_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    desktop = _desktop()
    web_dist = tmp_path / "web-dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "index.html").write_text(
        '<!doctype html><title>stock-desk</title><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (web_dist / "assets" / "app.js").write_text("", encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        desktop,
        "expected_platform_data_dir",
        lambda _app_name: data_dir,
    )
    launcher = desktop.DesktopLauncher(
        web_dist_dir=web_dist,
        startup_timeout_seconds=30,
    )

    running = launcher.start(open_browser=False)
    try:
        assert running.host == "127.0.0.1"
        assert running.port > 0
        assert running.data_dir == data_dir.resolve()
        assert running.health().status == "ok"
        assert running.api_alive
        assert running.worker_alive
        with urlopen(  # nosec B310
            f"http://{running.host}:{running.port}/api/tasks/worker-status",
            timeout=2,
        ) as response:
            worker_status = json.load(response)
        assert worker_status["state"] == "running"
        assert worker_status["last_seen_at"] is not None
    finally:
        running.stop()

    assert not running.api_alive
    assert not running.worker_alive
    assert not running.runtime_record.exists()


def test_entrypoint_calls_freeze_support_before_desktop_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = _desktop()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        desktop.multiprocessing,
        "freeze_support",
        lambda: calls.append(("freeze_support", None)),
    )
    monkeypatch.setattr(
        desktop,
        "run_desktop",
        lambda: calls.append(("desktop", None)) or 0,
    )

    result = desktop.main([])

    assert result == 0
    assert calls == [("freeze_support", None), ("desktop", None)]


def test_entrypoint_calls_freeze_support_before_validated_internal_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    desktop = _desktop()
    result_path = tmp_path / "result.json"
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        desktop.multiprocessing,
        "freeze_support",
        lambda: calls.append(("freeze_support", None)),
    )
    monkeypatch.setattr(
        desktop,
        "run_akshare_worker",
        lambda arguments: calls.append(("akshare", tuple(arguments))) or 0,
    )

    result = desktop.main(
        [
            "--internal-akshare-worker",
            "stock_news_em",
            '{"symbol":"600000"}',
            str(result_path),
        ]
    )

    assert result == 0
    assert calls == [
        ("freeze_support", None),
        (
            "akshare",
            (
                "stock_news_em",
                '{"symbol":"600000"}',
                str(result_path),
            ),
        ),
    ]


def test_entrypoint_dispatches_no_browser_shutdown_and_usage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    desktop = _desktop()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(desktop.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(
        desktop,
        "run_desktop",
        lambda *, open_browser=True: calls.append(("desktop", open_browser)) or 0,
    )
    monkeypatch.setattr(
        desktop,
        "shutdown_desktop",
        lambda: calls.append(("shutdown", None)) or 0,
    )

    assert desktop.main(["--no-browser"]) == 0
    assert desktop.main(["--shutdown"]) == 0
    assert desktop.main(["--unknown"]) == 2

    assert calls == [("desktop", False), ("shutdown", None)]
    assert "usage: stock-desk" in capsys.readouterr().err


def test_child_settings_payload_is_absolute_and_secret_backed(tmp_path: Path) -> None:
    desktop = _desktop()

    settings = desktop._settings_from_payload(
        {
            "data_dir": str(tmp_path / "data"),
            "database_url": f"sqlite:///{tmp_path / 'stock-desk.db'}",
            "master_key": "private-key",
            "web_dist_dir": str(tmp_path / "web-dist"),
        }
    )

    assert settings.data_dir == tmp_path / "data"
    assert settings.master_key.get_secret_value() == "private-key"
    assert settings.web_dist_dir == tmp_path / "web-dist"


def test_worker_child_signals_ready_only_from_running_heartbeat_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = _desktop()
    ready_event = desktop.threading.Event()
    stop_event = desktop.threading.Event()
    calls: list[str] = []

    class Runtime:
        def run_forever(self, stop: object, *, ready_event: object) -> None:
            calls.append("run")
            assert stop is stop_event
            assert not getattr(ready_event, "is_set")()
            getattr(ready_event, "set")()

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(
        "stock_desk.market.worker_runtime.ProductionMarketWorker.open",
        lambda *_args, **_kwargs: Runtime(),
    )
    monkeypatch.setattr(desktop, "_configure_file_logging", lambda _path: None)

    desktop._worker_child(
        {
            "data_dir": "/tmp/data",
            "database_url": "sqlite:////tmp/data.db",
            "master_key": "private-key",
            "web_dist_dir": "/tmp/web",
        },
        stop_event,
        ready_event,
        "/tmp/worker.log",
    )

    assert ready_event.is_set()
    assert calls == ["run", "close"]


def test_worker_child_first_heartbeat_failure_never_signals_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = _desktop()
    ready_event = desktop.threading.Event()
    calls: list[str] = []

    class Runtime:
        def run_forever(self, _stop: object, *, ready_event: object) -> None:
            calls.append("run")
            assert not getattr(ready_event, "is_set")()
            raise RuntimeError("first heartbeat failed")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(
        "stock_desk.market.worker_runtime.ProductionMarketWorker.open",
        lambda *_args, **_kwargs: Runtime(),
    )
    monkeypatch.setattr(desktop, "_configure_file_logging", lambda _path: None)

    with pytest.raises(RuntimeError, match="first heartbeat failed"):
        desktop._worker_child(
            {
                "data_dir": "/tmp/data",
                "database_url": "sqlite:////tmp/data.db",
                "master_key": "private-key",
                "web_dist_dir": "/tmp/web",
            },
            desktop.threading.Event(),
            ready_event,
            "/tmp/worker.log",
        )

    assert not ready_event.is_set()
    assert calls == ["run", "close"]


def test_windows_acl_command_replaces_and_validates_the_complete_dacl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    desktop = _desktop()
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("USERDOMAIN", "DESKTOP")
    monkeypatch.setenv("USERNAME", "owner")

    target = tmp_path / "runtime user's 数据"
    command = desktop._windows_acl_command(target, directory=True)

    assert command[0].endswith("System32/WindowsPowerShell/v1.0/powershell.exe")
    assert command[-2] == "-Command"
    script = command[-1]
    assert str(target) not in script
    assert "STOCK_DESK_ACL_TARGET" in script
    assert "Import-Module $securityModule -ErrorAction Stop" in script
    assert "Microsoft.PowerShell.Security\\Set-Acl" in script
    assert "Microsoft.PowerShell.Security\\Get-Acl" in script
    assert "SetAccessRuleProtection($true, $false)" in script
    assert "S-1-5-18" in script
    assert "S-1-5-32-544" in script
    assert "WindowsIdentity]::GetCurrent().User" in script
    assert "GetAccessRules($true, $true" in script
    assert "Unexpected ACL principal" in script
    assert "Required ACL principal is missing" in script


def test_windows_acl_target_is_passed_only_in_the_child_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    desktop = _desktop()
    target = tmp_path / "runtime user's 数据"
    target.mkdir()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda *_args, **kwargs: (
            calls.append(kwargs) or type("Completed", (), {"returncode": 0})()
        ),
    )

    desktop._run_windows_acl(target, directory=True)

    assert calls[0]["env"]["STOCK_DESK_ACL_TARGET"] == str(target)
    assert calls[0]["timeout"] == 30


def test_internal_akshare_mode_rejects_an_unknown_operation(tmp_path: Path) -> None:
    desktop = _desktop()
    result_path = tmp_path / "worker-result.json"

    result = desktop.run_akshare_worker(
        ["not_allowed", "{}", str(result_path.resolve())]
    )

    assert result == 2
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "status": "invalid_response"
    }


def test_formula_smoke_uses_the_real_spawn_executor() -> None:
    desktop = _desktop()

    assert desktop.run_formula_smoke() == 0


@pytest.mark.parametrize("frozen", [False, True])
def test_akshare_worker_uses_module_in_development_and_internal_mode_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frozen: bool,
) -> None:
    result_path = tmp_path / "worker-result.json"
    launched: list[tuple[str, ...]] = []

    class Temporary:
        name = str(result_path)

        def close(self) -> None:
            result_path.touch()

    class Process:
        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    def popen(arguments: tuple[str, ...], **kwargs: Any) -> Process:
        del kwargs
        launched.append(arguments)
        return Process()

    monkeypatch.setattr(
        akshare_source_module.tempfile,
        "NamedTemporaryFile",
        lambda **_kwargs: Temporary(),
    )
    monkeypatch.setattr(akshare_source_module.subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)

    process = akshare_source_module._launch_worker(
        "stock_news_em",
        {"symbol": "600000"},
    )
    try:
        assert len(launched) == 1
        command = launched[0]
        assert command[0] == sys.executable
        if frozen:
            assert command[1] == "--internal-akshare-worker"
            assert "-m" not in command
        else:
            assert command[1:3] == (
                "-m",
                "stock_desk.analysis.sources._akshare_worker",
            )
        assert command[-3] == "stock_news_em"
        assert json.loads(command[-2]) == {"symbol": "600000"}
        assert command[-1] == str(result_path)
    finally:
        process.close_result()
