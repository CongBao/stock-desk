"""Source-free desktop launcher for the private Stock Desk web application."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import logging
import multiprocessing
from multiprocessing.process import BaseProcess
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Final
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser

from cryptography.fernet import Fernet
from filelock import FileLock, Timeout
from pydantic import SecretStr
from sqlalchemy.engine import URL

from stock_desk.config import Settings


APP_NAME: Final = "stock-desk"
_LOOPBACK_HOST: Final = "127.0.0.1"
_DEFAULT_STARTUP_TIMEOUT_SECONDS: Final = 45.0
_PROCESS_STOP_TIMEOUT_SECONDS: Final = 10.0
_LOGGER = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """The per-user desktop instance lock is already held."""


def _release_version() -> str:
    try:
        return package_version("stock-desk")
    except PackageNotFoundError:
        return "0+unknown"


def expected_platform_data_dir(
    app_name: str,
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the current user's mutable application-data directory."""
    if not app_name or app_name != app_name.strip():
        raise ValueError("application name must be non-empty and trimmed")
    resolved_platform = platform.system() if platform_name is None else platform_name
    resolved_environment = os.environ if environment is None else environment
    resolved_home = Path.home() if home is None else home
    if resolved_platform == "Windows":
        local_app_data = resolved_environment.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is unavailable for the current user")
        return Path(local_app_data) / app_name
    if resolved_platform == "Darwin":
        return resolved_home / "Library" / "Application Support" / app_name
    xdg_data_home = resolved_environment.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else resolved_home / ".local" / "share"
    return base / app_name


def _windows_acl_command(path: Path, *, directory: bool) -> tuple[str, ...]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = (
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    inheritance = (
        "[System.Security.AccessControl.InheritanceFlags]::ContainerInherit "
        "-bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit"
        if directory
        else "[System.Security.AccessControl.InheritanceFlags]::None"
    )
    security_type = "DirectorySecurity" if directory else "FileSecurity"
    script = f"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('STOCK_DESK_ACL_TARGET', 'Process')
if ([string]::IsNullOrWhiteSpace($target)) {{ throw 'ACL target is unavailable' }}
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$administrators = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$required = @($current, $system, $administrators)
$acl = [System.Security.AccessControl.{security_type}]::new()
$acl.SetOwner($current)
$acl.SetAccessRuleProtection($true, $false)
$inheritance = {inheritance}
$propagation = [System.Security.AccessControl.PropagationFlags]::None
foreach ($sid in $required) {{
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        $propagation,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
}}
Set-Acl -LiteralPath $target -AclObject $acl
$actual = Get-Acl -LiteralPath $target
if (-not $actual.AreAccessRulesProtected) {{ throw 'ACL inheritance remains enabled' }}
$allowed = @{{}}
foreach ($sid in $required) {{ $allowed[$sid.Value] = $false }}
$rules = @($actual.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
foreach ($rule in $rules) {{
    $sid = $rule.IdentityReference.Value
    if (-not $allowed.ContainsKey($sid)) {{ throw "Unexpected ACL principal: $sid" }}
    if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {{
        throw "Unexpected ACL deny rule: $sid"
    }}
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    if (($rule.FileSystemRights -band $full) -ne $full) {{
        throw "ACL principal lacks full control: $sid"
    }}
    $allowed[$sid] = $true
}}
foreach ($sid in $required) {{
    if (-not $allowed[$sid.Value]) {{ throw "Required ACL principal is missing: $($sid.Value)" }}
}}
""".strip()
    return (
        os.fspath(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    )


def _run_windows_acl(path: Path, *, directory: bool) -> None:
    environment = os.environ.copy()
    environment["STOCK_DESK_ACL_TARGET"] = os.fspath(path)
    completed = subprocess.run(  # noqa: S603 -- fixed system tool and validated args
        _windows_acl_command(path, directory=directory),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"could not restrict private runtime path: {path}")


def _restrict_owner_access(path: Path, *, directory: bool) -> None:
    os.chmod(path, 0o700 if directory else 0o600)
    if os.name != "nt":
        return
    _run_windows_acl(path, directory=directory)


def _create_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"private runtime directory is invalid: {path}")
    _restrict_owner_access(path, directory=True)


def _create_private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(descriptor)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"private runtime file is invalid: {path}")
    _restrict_owner_access(path, directory=False)


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    pid: int
    host: str
    port: int
    data_dir: Path
    log_file: Path
    version: str = field(default_factory=_release_version)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_json_object(self) -> dict[str, object]:
        return {
            "data_dir": os.fspath(self.data_dir),
            "host": self.host,
            "log_file": os.fspath(self.log_file),
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_dir: Path
    runtime_dir: Path
    logs_dir: Path
    config_dir: Path
    lock_file: Path
    runtime_record: Path
    shutdown_request: Path
    log_file: Path
    master_key_file: Path

    @classmethod
    def create(cls, data_dir: Path) -> RuntimePaths:
        resolved_data_dir = data_dir.expanduser().resolve()
        runtime_dir = resolved_data_dir / "runtime"
        logs_dir = resolved_data_dir / "logs"
        config_dir = resolved_data_dir / "config"
        for private_directory in (
            resolved_data_dir,
            runtime_dir,
            logs_dir,
            config_dir,
        ):
            _create_private_directory(private_directory)
        paths = cls(
            data_dir=resolved_data_dir,
            runtime_dir=runtime_dir,
            logs_dir=logs_dir,
            config_dir=config_dir,
            lock_file=runtime_dir / "stock-desk.lock",
            runtime_record=runtime_dir / "runtime.json",
            shutdown_request=runtime_dir / "shutdown.request",
            log_file=logs_dir / "stock-desk.log",
            master_key_file=config_dir / "master.key",
        )
        _create_private_file(paths.lock_file)
        _create_private_file(paths.log_file)
        return paths

    def load_or_create_master_key(self) -> str:
        if not self.master_key_file.exists():
            descriptor = os.open(
                self.master_key_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                encoded = Fernet.generate_key()
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _restrict_owner_access(self.master_key_file, directory=False)
        try:
            return self.master_key_file.read_text(encoding="ascii")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(
                "the private desktop master key is unreadable"
            ) from error

    def write_runtime_record(self, record: RuntimeRecord) -> None:
        temporary = self.runtime_dir / f"runtime-{os.getpid()}.tmp"
        payload = json.dumps(
            record.as_json_object(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, self.runtime_record)
            _restrict_owner_access(self.runtime_record, directory=False)
        finally:
            temporary.unlink(missing_ok=True)


class SingleInstanceLock:
    def __init__(self, lock_file: Path) -> None:
        self._lock_file = lock_file
        self._lock = FileLock(lock_file, timeout=0)
        self._acquired = False

    def acquire(self) -> None:
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            raise AlreadyRunningError(
                "Stock Desk is already running for this user"
            ) from None
        self._acquired = True
        _restrict_owner_access(self._lock_file, directory=False)

    def release(self) -> None:
        if self._acquired:
            self._lock.release()
            self._acquired = False


@dataclass(frozen=True, slots=True)
class ReservedSocket:
    socket: socket.socket
    host: str
    port: int


def reserve_api_socket() -> ReservedSocket:
    """Bind and listen before process creation so no port-selection race exists."""
    api_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            api_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        api_socket.bind((_LOOPBACK_HOST, 0))
        api_socket.listen(128)
        host, port = api_socket.getsockname()
        return ReservedSocket(socket=api_socket, host=str(host), port=int(port))
    except BaseException:
        api_socket.close()
        raise


@dataclass(frozen=True, slots=True)
class HealthStatus:
    name: str
    status: str
    api_version: str


def _read_health(host: str, port: int, *, timeout: float = 2.0) -> HealthStatus:
    url = f"http://{host}:{port}/api/health"
    with urlopen(url, timeout=timeout) as response:  # nosec B310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("desktop health response is invalid")
    if payload.get("status") != "ok":
        raise RuntimeError("desktop API is not healthy")
    return HealthStatus(
        name=str(payload.get("name", "")),
        status=str(payload["status"]),
        api_version=str(payload.get("api_version", "")),
    )


def _settings_from_payload(payload: Mapping[str, str]) -> Settings:
    return Settings(
        app_name=APP_NAME,
        data_dir=Path(payload["data_dir"]),
        database_url=payload["database_url"],
        master_key=SecretStr(payload["master_key"]),
        web_dist_dir=Path(payload["web_dist_dir"]),
    )


def _configure_file_logging(log_file: str) -> None:
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s %(process)d %(levelname)s %(name)s %(message)s",
        force=True,
    )


def _api_child(
    api_socket: socket.socket,
    settings_payload: Mapping[str, str],
    stop_event: Any,
    log_file: str,
) -> None:
    _configure_file_logging(log_file)
    from stock_desk.main import create_app
    import uvicorn

    application = create_app(_settings_from_payload(settings_payload))
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host=_LOOPBACK_HOST,
            port=0,
            access_log=False,
            log_config=None,
        )
    )

    def request_shutdown() -> None:
        stop_event.wait()
        server.should_exit = True

    watcher = threading.Thread(target=request_shutdown, daemon=True)
    watcher.start()
    try:
        server.run(sockets=[api_socket])
    finally:
        api_socket.close()


def _worker_child(
    settings_payload: Mapping[str, str],
    stop_event: Any,
    ready_event: Any,
    log_file: str,
) -> None:
    _configure_file_logging(log_file)
    from stock_desk.market.worker_runtime import ProductionMarketWorker

    runtime = ProductionMarketWorker.open(
        _settings_from_payload(settings_payload),
        worker_id=f"desktop-{socket.gethostname()}-{os.getpid()}",
    )
    try:
        runtime.run_forever(stop_event, ready_event=ready_event)
    finally:
        runtime.close()


def _stop_process(process: BaseProcess, stop_event: Any) -> None:
    if process.pid is None:
        return
    stop_event.set()
    process.join(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)


class RunningDesktop:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        paths: RuntimePaths,
        instance_lock: SingleInstanceLock,
        api_process: BaseProcess,
        worker_process: BaseProcess,
        api_stop_event: Any,
        worker_stop_event: Any,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = paths.data_dir
        self.runtime_record = paths.runtime_record
        self.shutdown_request = paths.shutdown_request
        self.log_file = paths.log_file
        self._paths = paths
        self._instance_lock = instance_lock
        self._api_process = api_process
        self._worker_process = worker_process
        self._api_stop_event = api_stop_event
        self._worker_stop_event = worker_stop_event
        self._stop_lock = threading.Lock()
        self._stopped = False

    @property
    def api_alive(self) -> bool:
        return self._api_process.is_alive()

    @property
    def worker_alive(self) -> bool:
        return self._worker_process.is_alive()

    def health(self) -> HealthStatus:
        return _read_health(self.host, self.port)

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            try:
                _stop_process(self._api_process, self._api_stop_event)
                _stop_process(self._worker_process, self._worker_stop_event)
            finally:
                self._paths.runtime_record.unlink(missing_ok=True)
                self._instance_lock.release()


class DesktopLauncher:
    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        web_dist_dir: Path | None = None,
        startup_timeout_seconds: float = _DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < startup_timeout_seconds <= 300:
            raise ValueError("desktop startup timeout is invalid")
        self._data_dir = data_dir
        self._web_dist_dir = web_dist_dir
        self._startup_timeout_seconds = startup_timeout_seconds

    @staticmethod
    def _resource_root() -> Path:
        frozen_root = getattr(sys, "_MEIPASS", None)
        if getattr(sys, "frozen", False) and isinstance(frozen_root, str):
            return Path(frozen_root).resolve()
        return Path(__file__).resolve().parents[2]

    def _resolved_web_dist(self) -> Path:
        if self._web_dist_dir is not None:
            return self._web_dist_dir.expanduser().resolve()
        resource_root = self._resource_root()
        relative = (
            Path("web-dist") if getattr(sys, "frozen", False) else Path("web/dist")
        )
        web_dist = resource_root / relative
        if not (web_dist / "index.html").is_file():
            raise RuntimeError(f"production web assets are missing: {web_dist}")
        return web_dist

    def start(self, *, open_browser: bool = True) -> RunningDesktop:
        data_dir = (
            expected_platform_data_dir(APP_NAME)
            if self._data_dir is None
            else self._data_dir
        )
        paths = RuntimePaths.create(data_dir)
        instance_lock = SingleInstanceLock(paths.lock_file)
        instance_lock.acquire()
        paths.shutdown_request.unlink(missing_ok=True)
        _configure_file_logging(os.fspath(paths.log_file))
        reserved: ReservedSocket | None = None
        api_process: BaseProcess | None = None
        worker_process: BaseProcess | None = None
        api_stop_event: Any = None
        worker_stop_event: Any = None
        try:
            web_dist = self._resolved_web_dist()
            master_key = paths.load_or_create_master_key()
            database_path = paths.data_dir / "stock-desk.db"
            database_url = URL.create(
                "sqlite",
                database=os.fspath(database_path),
            ).render_as_string(hide_password=False)
            settings_payload = {
                "data_dir": os.fspath(paths.data_dir),
                "database_url": database_url,
                "master_key": master_key,
                "web_dist_dir": os.fspath(web_dist),
            }
            from stock_desk.storage.database import migrate

            migrate(database_url)
            context = multiprocessing.get_context("spawn")
            api_stop_event = context.Event()
            worker_stop_event = context.Event()
            worker_ready_event = context.Event()
            reserved = reserve_api_socket()
            api_port = reserved.port
            worker_process = context.Process(
                name="stock-desk-worker",
                target=_worker_child,
                args=(
                    settings_payload,
                    worker_stop_event,
                    worker_ready_event,
                    os.fspath(paths.log_file),
                ),
            )
            api_process = context.Process(
                name="stock-desk-api",
                target=_api_child,
                args=(
                    reserved.socket,
                    settings_payload,
                    api_stop_event,
                    os.fspath(paths.log_file),
                ),
            )
            worker_process.start()
            api_process.start()
            reserved.socket.close()
            reserved = None
            deadline = time.monotonic() + self._startup_timeout_seconds
            while time.monotonic() < deadline:
                if not api_process.is_alive():
                    raise RuntimeError("desktop API exited during startup")
                if not worker_process.is_alive():
                    raise RuntimeError("desktop worker exited during startup")
                if worker_ready_event.is_set():
                    try:
                        health = _read_health(
                            _LOOPBACK_HOST,
                            api_port,
                            timeout=0.5,
                        )
                    except (OSError, RuntimeError, URLError):
                        pass
                    else:
                        if health.status == "ok":
                            break
                time.sleep(0.05)
            else:
                raise RuntimeError("desktop API and worker did not become healthy")
            running = RunningDesktop(
                host=_LOOPBACK_HOST,
                port=api_port,
                paths=paths,
                instance_lock=instance_lock,
                api_process=api_process,
                worker_process=worker_process,
                api_stop_event=api_stop_event,
                worker_stop_event=worker_stop_event,
            )
            paths.write_runtime_record(
                RuntimeRecord(
                    pid=os.getpid(),
                    host=running.host,
                    port=running.port,
                    data_dir=running.data_dir,
                    log_file=running.log_file,
                )
            )
            if open_browser:
                webbrowser.open(f"http://{running.host}:{running.port}/")
            return running
        except BaseException:
            if reserved is not None:
                reserved.socket.close()
            if api_process is not None and api_stop_event is not None:
                _stop_process(api_process, api_stop_event)
            if worker_process is not None and worker_stop_event is not None:
                _stop_process(worker_process, worker_stop_event)
            paths.runtime_record.unlink(missing_ok=True)
            instance_lock.release()
            _LOGGER.exception("Stock Desk desktop startup failed")
            raise


def run_akshare_worker(arguments: Sequence[str]) -> int:
    from stock_desk.analysis.sources._akshare_worker import main as worker_main

    return worker_main(list(arguments))


def run_formula_smoke() -> int:
    """Exercise the real isolated Formula worker in a frozen distribution."""
    from datetime import date, datetime, time as datetime_time, timedelta
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from stock_desk.formula.compiler import formula_source_checksum
    from stock_desk.formula.service import IsolatedFormulaExecutor
    from stock_desk.formula.signal_series import SignalSeries
    from stock_desk.market.provenance import (
        BarRoutingRequest,
        RoutedBarSuccess,
        make_routing_manifest,
    )
    from stock_desk.market.providers.normalization import dataset_version
    from stock_desk.market.types import (
        Adjustment,
        Bar,
        BarQuery,
        BarResult,
        MarketCapability,
        Period,
        Provenance,
        ProviderId,
        TradingStatus,
    )

    timezone = ZoneInfo("Asia/Shanghai")
    days = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(4))
    timestamps = tuple(
        datetime.combine(day, datetime_time(), tzinfo=timezone) for day in days
    )
    query = BarQuery(
        symbol="600000.SH",
        period=Period.DAY,
        adjustment=Adjustment.NONE,
        start=timestamps[0],
        end=timestamps[-1] + timedelta(days=1),
    )
    closes = tuple(Decimal(value) for value in ("10", "11", "10.5", "12"))
    bars = tuple(
        Bar(
            symbol=query.symbol,
            timestamp=timestamp,
            period=query.period,
            adjustment=query.adjustment,
            open=close,
            high=close + Decimal("0.2"),
            low=close - Decimal("0.2"),
            close=close,
            volume=1_000 + index,
            status=TradingStatus.NORMAL,
        )
        for index, (timestamp, close) in enumerate(zip(timestamps, closes, strict=True))
    )
    data_cutoff = timestamps[-1] + timedelta(hours=15)
    fetched_at = data_cutoff + timedelta(hours=1)
    version = dataset_version(
        source=ProviderId.TUSHARE,
        operation="bars",
        request={"query": query},
        data_cutoff=data_cutoff,
        items=bars,
    )
    result = BarResult(
        query=query,
        bars=bars,
        coverage_start=query.start,
        coverage_end=query.end,
        provenance=Provenance(
            source=ProviderId.TUSHARE,
            fetched_at=fetched_at,
            data_cutoff=data_cutoff,
            adjustment=query.adjustment,
            dataset_version=version,
        ),
    )
    manifest = make_routing_manifest(
        category=MarketCapability.BARS,
        request=BarRoutingRequest(query=query),
        priority=(ProviderId.TUSHARE,),
        attempts=(),
        selected_source=ProviderId.TUSHARE,
        upstream_dataset_version=version,
        upstream_fetched_at=fetched_at,
        upstream_data_cutoff=data_cutoff,
        upstream_adjustment=query.adjustment,
    )
    source = "X:C;"
    request = json.dumps(
        {
            "formula": {
                "formula_id": "formula-smoke",
                "formula_version_id": "formula-smoke-v1",
                "version": 1,
                "checksum": formula_source_checksum(source),
            },
            "parameters": [],
            "routed": RoutedBarSuccess(result=result, manifest=manifest).model_dump(
                mode="json"
            ),
            "source": source,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    payload = IsolatedFormulaExecutor().execute(request)
    series = SignalSeries.from_canonical_json_bytes(payload)
    return 0 if tuple(item.name for item in series.numeric_outputs) == ("X",) else 1


def run_desktop(*, open_browser: bool = True) -> int:
    running = DesktopLauncher().start(open_browser=open_browser)
    stop_requested = threading.Event()
    old_handlers: dict[signal.Signals, Any] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        stop_requested.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, request_stop)
    try:
        while not stop_requested.wait(0.25):
            if running.shutdown_request.exists():
                running.shutdown_request.unlink(missing_ok=True)
                stop_requested.set()
                continue
            if not running.api_alive or not running.worker_alive:
                raise RuntimeError(
                    f"a desktop child process exited; see log: {running.log_file}"
                )
    finally:
        running.stop()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    return 0


def shutdown_desktop(*, timeout_seconds: float = 30.0) -> int:
    paths = RuntimePaths.create(expected_platform_data_dir(APP_NAME))
    if not paths.runtime_record.is_file():
        return 0
    _create_private_file(paths.shutdown_request)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not paths.runtime_record.exists():
            return 0
        time.sleep(0.1)
    raise RuntimeError(f"Stock Desk did not stop; see log: {paths.log_file}")


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--internal-akshare-worker"]:
        return run_akshare_worker(arguments[1:])
    if arguments == ["--internal-formula-smoke"]:
        return run_formula_smoke()
    if not arguments:
        return run_desktop()
    if arguments == ["--no-browser"]:
        return run_desktop(open_browser=False)
    if arguments == ["--shutdown"]:
        return shutdown_desktop()
    print("usage: stock-desk [--no-browser | --shutdown]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
