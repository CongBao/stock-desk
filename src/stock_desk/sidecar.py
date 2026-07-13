from collections.abc import MutableMapping
from dataclasses import dataclass
import logging
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import threading
import time
from typing import BinaryIO

from pydantic import SecretStr
from sqlalchemy.engine import URL

from stock_desk.config import Settings, V11_DATA_VERSION, V11_PRODUCT_DIRECTORY
from stock_desk.desktop_session import DesktopLifecycleController, DesktopSession
from stock_desk.desktop_runtime import RuntimePaths, RuntimeRecord
from stock_desk.diagnostics.models import DiagnosticEventCode, DiagnosticEventSink


_PREFIX = "STOCK_DESK_DESKTOP_"
_SESSION_SECRET_KEY = f"{_PREFIX}SESSION_SECRET"
_STARTUP_TIMEOUT_SECONDS = 45.0
_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_LOGGER = logging.getLogger(__name__)
_BOOTSTRAP_RELEASE_BYTE = b"\x01"


@dataclass(frozen=True, slots=True)
class SidecarLaunchConfig:
    host: str
    port: int
    data_root: Path
    session: DesktopSession

    @classmethod
    def consume(cls, environment: MutableMapping[str, str]) -> "SidecarLaunchConfig":
        secret = environment.pop(_SESSION_SECRET_KEY, None)
        try:
            if secret is None:
                raise ValueError
            port = int(environment[f"{_PREFIX}PORT"])
            if not 1024 <= port <= 65535:
                raise ValueError
            data_root = Path(environment[f"{_PREFIX}DATA_ROOT"])
            if (
                not data_root.is_absolute()
                or data_root.name != V11_DATA_VERSION
                or data_root.parent.name != V11_PRODUCT_DIRECTORY
            ):
                raise ValueError
            session = DesktopSession(
                origin=environment[f"{_PREFIX}ORIGIN"],
                secret=secret,
                host_version=environment[f"{_PREFIX}HOST_VERSION"],
                frontend_version=environment[f"{_PREFIX}FRONTEND_VERSION"],
                sidecar_version=environment[f"{_PREFIX}SIDECAR_VERSION"],
                source_revision=environment[f"{_PREFIX}SOURCE_REVISION"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("desktop sidecar configuration is invalid") from error
        return cls(
            host="127.0.0.1",
            port=port,
            data_root=data_root,
            session=session,
        )


def build_sidecar_settings(config: SidecarLaunchConfig, *, master_key: str) -> Settings:
    database_url = URL.create(
        "sqlite",
        database=os.fspath(config.data_root / "stock-desk.db"),
    ).render_as_string(hide_password=False)
    return Settings(
        app_name="stock-desk",
        data_dir=config.data_root,
        database_url=database_url,
        master_key=SecretStr(master_key),
        web_dist_dir=None,
    )


def await_bootstrap_gate(stream: BinaryIO) -> None:
    """Wait until the desktop host confirms that process containment is active."""
    try:
        release = stream.read(1)
    except (OSError, ValueError) as error:
        raise RuntimeError("desktop sidecar bootstrap gate rejected") from error
    if release != _BOOTSTRAP_RELEASE_BYTE:
        raise RuntimeError("desktop sidecar bootstrap gate rejected")


def run_sidecar(config: SidecarLaunchConfig) -> int:
    """Run the authenticated API and market worker inside one controlled process."""
    import uvicorn

    from stock_desk.main import create_app
    from stock_desk.market.worker_runtime import ProductionMarketWorker
    from stock_desk.storage.database import migrate

    paths = RuntimePaths.create(config.data_root)
    logging.basicConfig(
        filename=paths.log_file,
        level=logging.INFO,
        format="%(asctime)s %(process)d %(levelname)s %(name)s %(message)s",
        force=True,
    )
    diagnostic_events = DiagnosticEventSink()
    diagnostic_events.emit(DiagnosticEventCode.SIDECAR_STARTING)
    settings = build_sidecar_settings(
        config,
        master_key=paths.load_or_create_master_key(),
    )
    try:
        migrate(settings.database_url)
    except BaseException:
        diagnostic_events.emit(DiagnosticEventCode.STORAGE_UNAVAILABLE)
        raise
    diagnostic_events.emit(DiagnosticEventCode.STORAGE_READY)
    lifecycle = DesktopLifecycleController()
    stop_event = lifecycle.stop_event
    ready_event = threading.Event()
    diagnostic_events.emit(DiagnosticEventCode.WORKER_STARTING)
    try:
        worker = ProductionMarketWorker.open(
            settings,
            worker_id=f"tauri-sidecar-{socket.gethostname()}-{os.getpid()}",
            diagnostic_event_sink=diagnostic_events,
        )
    except BaseException:
        diagnostic_events.emit(DiagnosticEventCode.WORKER_STARTUP_FAILED)
        raise
    if hasattr(worker, "tasks"):
        startup_metrics = worker.tasks.metrics()
        lifecycle.initialize_startup_recovery(
            queued=startup_metrics.by_status["queued"],
            running=startup_metrics.by_status["running"],
        )

    def run_worker() -> None:
        try:
            worker.run_forever(
                stop_event,
                ready_event=ready_event,
                claim_stop_event=lifecycle.claim_stop_event,
            )
        except BaseException:
            diagnostic_events.emit(DiagnosticEventCode.WORKER_RUNTIME_FAILED)
            raise
        finally:
            try:
                worker.close()
            except BaseException:
                diagnostic_events.emit(DiagnosticEventCode.WORKER_RUNTIME_FAILED)
                raise
            else:
                diagnostic_events.emit(DiagnosticEventCode.WORKER_STOPPED)

    worker_thread = threading.Thread(
        name="stock-desk-market-worker",
        target=run_worker,
        daemon=False,
    )
    worker_thread.start()
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        while not ready_event.wait(timeout=0.05):
            if not worker_thread.is_alive() or time.monotonic() >= deadline:
                diagnostic_events.emit(DiagnosticEventCode.WORKER_STARTUP_FAILED)
                raise RuntimeError("desktop sidecar did not become ready")
        diagnostic_events.emit(DiagnosticEventCode.WORKER_READY)

        application = create_app(
            settings,
            task_repository=getattr(worker, "tasks", None),
            desktop_session=config.session,
            desktop_lifecycle=lifecycle,
            diagnostic_event_sink=diagnostic_events,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=config.host,
                port=config.port,
                access_log=False,
                log_config=None,
            )
        )
        lifecycle.bind_server(server)
        diagnostic_events.emit(DiagnosticEventCode.SIDECAR_READY)
        paths.write_runtime_record(
            RuntimeRecord(
                pid=os.getpid(),
                host=config.host,
                port=config.port,
                data_dir=config.data_root,
                log_file=paths.log_file,
                version=config.session.sidecar_version,
            )
        )
        server.run()
    except BaseException:
        diagnostic_events.emit(DiagnosticEventCode.SIDECAR_RUNTIME_FAILED)
        raise
    finally:
        diagnostic_events.emit(DiagnosticEventCode.SIDECAR_STOPPING)
        stop_event.set()
        worker_thread.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        paths.runtime_record.unlink(missing_ok=True)
    if worker_thread.is_alive():
        diagnostic_events.emit(DiagnosticEventCode.WORKER_SHUTDOWN_TIMEOUT)
        _LOGGER.error("desktop worker did not stop before shutdown deadline")
        return 1
    diagnostic_events.emit(DiagnosticEventCode.SIDECAR_STOPPED)
    return 0


def main() -> int:
    multiprocessing.freeze_support()
    await_bootstrap_gate(sys.stdin.buffer)
    config = SidecarLaunchConfig.consume(os.environ)
    return run_sidecar(config)


if __name__ == "__main__":
    raise SystemExit(main())
