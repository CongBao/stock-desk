"""Run the disposable native macOS Stock Desk full-product journey gate."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from scripts import macos_sidecar, macos_tauri_support
from scripts.macos_product_journey import (
    APP_IDENTIFIER,
    EMBEDDED_WEBVIEW,
    EXPECTED_ACTIONS,
    JourneyEvidence,
    JourneyIdentity,
    MacOSJourneyError,
    validate_isolated_product_state,
    validate_operator_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "test-results" / "macos-full-product"
APP_NAME = "Stock Desk.app"
HOST_NAME = "stock-desk-desktop"


class MacOSFullProductError(RuntimeError):
    """The disposable macOS full-product gate failed closed."""


@dataclass(frozen=True, slots=True)
class HarnessPaths:
    temporary_root: Path
    pyinstaller: Path
    cargo: Path
    app_root: Path
    app_path: Path
    host_path: Path
    data_root: Path
    build_log: Path
    host_log: Path

    @classmethod
    def create(cls, temporary_root: Path) -> HarnessPaths:
        pyinstaller = temporary_root / "pyinstaller"
        cargo = temporary_root / "cargo"
        app_root = temporary_root / "app"
        app_path = app_root / APP_NAME
        data_root = temporary_root / "data" / "Stock Desk" / "v1.1"
        for directory in (pyinstaller, cargo, app_root, data_root):
            directory.mkdir(parents=True, exist_ok=False)
        return cls(
            temporary_root=temporary_root,
            pyinstaller=pyinstaller,
            cargo=cargo,
            app_root=app_root,
            app_path=app_path,
            host_path=app_path / "Contents" / "MacOS" / HOST_NAME,
            data_root=data_root,
            build_log=temporary_root / "tauri-build.log",
            host_log=temporary_root / "desktop-host.log",
        )


@dataclass(slots=True)
class HarnessContext:
    paths: HarnessPaths
    output: Path
    sidecar_copy: Path | None = None
    host_process: subprocess.Popen[bytes] | Any | None = None
    process_tree: macos_tauri_support.VerifiedProcessTree | None = None


def _preflight() -> None:
    if platform.system() != "Darwin":
        raise MacOSFullProductError("macOS full-product test requires Darwin")
    for command in ("git", "ioreg", "pnpm", "rustc", "swift", "swiftc"):
        if shutil.which(command) is None:
            raise MacOSFullProductError(
                f"required macOS test command is missing: {command}"
            )
    if macos_tauri_support.screen_is_locked():
        raise MacOSFullProductError("unlock the Mac before running the product journey")


def _source_identity(
    expected: tuple[str, str] | None = None,
) -> tuple[str, str]:
    return macos_tauri_support.require_clean_source_identity(ROOT, expected)


def _build_application(
    context: HarnessContext, timeout_seconds: int, source_sha: str
) -> None:
    paths = context.paths
    target = macos_sidecar.host_target_triple()
    artifact = macos_sidecar.build_native_sidecar(ROOT, paths.pyinstaller, target)
    sidecar_copy = (
        ROOT / "src-tauri" / "binaries" / macos_sidecar.sidecar_filename(target)
    )
    macos_tauri_support.copy_exclusive(artifact, sidecar_copy)
    context.sidecar_copy = sidecar_copy
    environment = os.environ.copy()
    environment.update(
        {
            "CARGO_TARGET_DIR": os.fspath(paths.cargo),
            "STOCK_DESK_SOURCE_REVISION": source_sha,
        }
    )
    command = (
        "pnpm",
        "exec",
        "tauri",
        "build",
        "--config",
        "src-tauri/tauri.conf.json",
        "--config",
        "src-tauri/tauri.macos-test.conf.json",
        "--debug",
        "--bundles",
        "app",
        "--no-sign",
        "--ci",
    )
    with paths.build_log.open("wb") as build_log:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=ROOT,
            env=environment,
            stdout=build_log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout_seconds,
        )
    built_app = paths.cargo / "debug" / "bundle" / "macos" / APP_NAME
    if result.returncode != 0 or not built_app.is_dir():
        raise MacOSFullProductError("macOS debug app build failed")
    shutil.copytree(built_app, paths.app_path, symlinks=True)
    if not paths.host_path.is_file():
        raise MacOSFullProductError("macOS app host executable is missing")


def _launch_application(
    context: HarnessContext, source_sha: str, source_tree: str, nonce: str
) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "STOCK_DESK_MACOS_TEST_DATA_ROOT": os.fspath(context.paths.data_root),
            "STOCK_DESK_SOURCE_REVISION": source_sha,
            "STOCK_DESK_SOURCE_TREE": source_tree,
            "STOCK_DESK_MACOS_TEST_SESSION_NONCE": nonce,
        }
    )
    with context.paths.host_log.open("wb") as host_log:
        process = subprocess.Popen(  # noqa: S603
            (os.fspath(context.paths.host_path),),
            cwd=context.paths.host_path.parent,
            env=environment,
            stdout=host_log,
            stderr=subprocess.STDOUT,
        )
    context.host_process = process
    context.process_tree = macos_tauri_support.VerifiedProcessTree(
        process.pid,
        context.paths.host_path,
        context.paths.temporary_root,
    )
    return process


def _wait_for_sidecar_child(context: HarnessContext, timeout_seconds: int) -> int:
    if context.process_tree is None:
        raise MacOSFullProductError("macOS host process tree was not initialized")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        context.process_tree.observe()
        sidecar_pid = context.process_tree.sidecar_pid()
        if sidecar_pid is not None:
            return sidecar_pid
        if context.host_process is not None and context.host_process.poll() is not None:
            raise MacOSFullProductError("macOS host exited before sidecar startup")
        time.sleep(0.1)
    raise MacOSFullProductError("timed out waiting for the native sidecar child")


def _wait_for_ready_state(
    context: HarnessContext, sidecar_pid: int, timeout_seconds: int
) -> dict[str, Any]:
    runtime_record = context.paths.data_root / "runtime" / "runtime.json"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if runtime_record.is_file() and not runtime_record.is_symlink():
            try:
                payload = json.loads(runtime_record.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get("pid") == sidecar_pid
                and payload.get("host") == "127.0.0.1"
                and type(payload.get("port")) is int
            ):
                break
        time.sleep(0.1)
    else:
        raise MacOSFullProductError("timed out waiting for ready sidecar state")
    if context.host_process is None:
        raise MacOSFullProductError("macOS host process is unavailable")
    return macos_tauri_support.observe_native_window(
        context.paths.temporary_root,
        context.host_process.pid,
        min(timeout_seconds, 60),
    )


def _write_interaction_ready(
    output: Path, identity: JourneyIdentity, window: dict[str, Any]
) -> Path:
    evidence_path = output / "operator-evidence.json"
    ready = {
        "schema_version": "stock-desk-macos-full-product-ready-v1",
        "source_sha": identity.source_sha,
        "source_tree": identity.source_tree,
        "session_nonce": identity.session_nonce,
        "app_identifier": APP_IDENTIFIER,
        "embedded_webview": EMBEDDED_WEBVIEW,
        "host_pid": identity.host_pid,
        "sidecar_pid": identity.sidecar_pid,
        "input_method": "codex-computer-use-sky-click",
        "physical_mouse_click": True,
        "expected_actions": list(EXPECTED_ACTIONS),
        "operator_evidence_path": os.fspath(evidence_path),
        "screenshot_directory": os.fspath(output),
        "native_window": {
            key: window[key]
            for key in ("title", "layer", "on_screen", "width", "height")
        },
    }
    macos_tauri_support.atomic_write_json(output / "interaction-ready.json", ready)
    return evidence_path


def _await_operator_evidence(
    path: Path, identity: JourneyIdentity, timeout_seconds: int
) -> JourneyEvidence:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.1)
    if not path.is_file() or path.is_symlink():
        raise MacOSJourneyError("Codex Computer Use operator evidence timed out")
    if path.stat().st_size > 1_048_576:
        raise MacOSJourneyError("operator evidence is unexpectedly large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MacOSJourneyError("operator evidence is not valid JSON") from error
    evidence = validate_operator_evidence(payload, identity)
    for screenshot in evidence.screenshots:
        screenshot_path = path.parent / screenshot.name
        try:
            screenshot_path.resolve().relative_to(path.parent.resolve())
        except ValueError as error:
            raise MacOSJourneyError("operator screenshot escaped output") from error
        if screenshot_path.is_symlink() or not screenshot_path.is_file():
            raise MacOSJourneyError("operator screenshot is missing or unsafe")
        if (
            screenshot_path.stat().st_size != screenshot.size
            or macos_tauri_support.sha256_file(screenshot_path) != screenshot.sha256
        ):
            raise MacOSJourneyError("operator screenshot identity does not match")
    return evidence


def _wait_for_graceful_exit(context: HarnessContext, timeout_seconds: int) -> None:
    if context.host_process is None or context.process_tree is None:
        raise MacOSFullProductError("macOS process identity is incomplete")
    try:
        return_code = context.host_process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MacOSFullProductError(
            "timed out waiting for graceful host exit"
        ) from error
    if return_code != 0:
        raise MacOSFullProductError("macOS host did not exit gracefully")
    context.process_tree.verify_absent()


def _remove_operator_intermediates(output: Path) -> None:
    for path in output.iterdir() if output.is_dir() else ():
        if (
            path.name in {"interaction-ready.json", "operator-evidence.json"}
            or path.suffix.lower() == ".png"
        ):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)


def _cleanup(context: HarnessContext) -> None:
    errors: list[BaseException] = []
    if context.process_tree is not None:
        try:
            context.process_tree.terminate()
        except BaseException as error:
            errors.append(error)
    try:
        macos_tauri_support.unregister_bundle(context.paths.app_path)
    except BaseException as error:
        errors.append(error)
    if context.sidecar_copy is not None:
        try:
            context.sidecar_copy.unlink(missing_ok=True)
        except BaseException as error:
            errors.append(error)
    try:
        shutil.rmtree(context.paths.temporary_root, ignore_errors=False)
    except FileNotFoundError:
        pass
    except BaseException as error:
        errors.append(error)
    try:
        _remove_operator_intermediates(context.output)
    except BaseException as error:
        errors.append(error)
    if context.sidecar_copy is not None and context.sidecar_copy.exists():
        errors.append(RuntimeError("temporary target-triple sidecar remains"))
    if context.paths.temporary_root.exists():
        errors.append(RuntimeError("temporary full-product root remains"))
    if errors:
        details = "; ".join(str(error) for error in errors)
        raise MacOSFullProductError(f"macOS full-product cleanup failed: {details}")


def run_full_product_test(output: Path, timeout_seconds: int) -> dict[str, Any]:
    """Build, wait for real operator evidence, validate, and clean unconditionally."""

    if timeout_seconds < 60 or timeout_seconds > 1800:
        raise MacOSFullProductError("timeout must be between 60 and 1800 seconds")
    _preflight()
    output = macos_tauri_support.validated_output(ROOT, output, "macOS full-product")
    source_sha, source_tree = _source_identity()
    source_identity = (source_sha, source_tree)
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=False)
    temporary_root = Path(tempfile.mkdtemp(prefix="stock-desk-macos-full-product-"))
    context = HarnessContext(
        paths=HarnessPaths.create(temporary_root),
        output=output,
    )
    evidence: JourneyEvidence | None = None
    product_state: dict[str, Any] | None = None
    window: dict[str, Any] | None = None
    identity: JourneyIdentity | None = None
    try:
        _build_application(context, timeout_seconds, source_sha)
        _source_identity(source_identity)
        nonce = str(uuid.uuid4())
        host = _launch_application(context, source_sha, source_tree, nonce)
        sidecar_pid = _wait_for_sidecar_child(context, min(timeout_seconds, 60))
        window = _wait_for_ready_state(context, sidecar_pid, min(timeout_seconds, 60))
        identity = JourneyIdentity(
            source_sha=source_sha,
            source_tree=source_tree,
            session_nonce=nonce,
            host_pid=host.pid,
            sidecar_pid=sidecar_pid,
        )
        operator_path = _write_interaction_ready(output, identity, window)
        print(
            f"Stock Desk full-product journey ready: {output / 'interaction-ready.json'}",
            flush=True,
        )
        evidence = _await_operator_evidence(operator_path, identity, timeout_seconds)
        product_state = validate_isolated_product_state(
            context.paths.data_root, evidence
        )
        _wait_for_graceful_exit(context, 20)
    finally:
        active_error = sys.exception()
        try:
            _cleanup(context)
        except BaseException as cleanup_error:
            if active_error is None:
                raise
            active_error.add_note(f"cleanup also failed: {cleanup_error}")

    if evidence is None or product_state is None or window is None or identity is None:
        raise MacOSFullProductError("macOS full-product evidence is incomplete")
    if temporary_root.exists():
        raise MacOSFullProductError("temporary full-product root remains")
    _source_identity(source_identity)
    report: dict[str, Any] = {
        "schema_version": "stock-desk-macos-full-product-v1",
        "scope": "local-macos-development-test-only",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "source_dirty": False,
        "app_identifier": APP_IDENTIFIER,
        "embedded_webview": EMBEDDED_WEBVIEW,
        "host_pid": identity.host_pid,
        "sidecar_pid": identity.sidecar_pid,
        "native_window": {
            key: window[key]
            for key in ("title", "layer", "on_screen", "width", "height")
        },
        "operator_evidence": asdict(evidence),
        "isolated_product_state": product_state,
        "graceful_exit_confirmed": True,
        "process_cleanup_confirmed": True,
        "temporary_root_removed": True,
        "limitations": [
            "This is a local macOS development gate, not a macOS release asset.",
            "Windows NSIS and standard-user Windows acceptance remain authoritative.",
        ],
    }
    macos_tauri_support.atomic_write_json(output / "macos-full-product.json", report)
    return report


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    raw_arguments = sys.argv[1:] if arguments is None else arguments
    if raw_arguments[:1] == ["--"]:
        raw_arguments = raw_arguments[1:]
    parsed = parser.parse_args(raw_arguments)
    if parsed.timeout_seconds < 60 or parsed.timeout_seconds > 1800:
        parser.error("--timeout-seconds must be between 60 and 1800")
    report = run_full_product_test(parsed.output, parsed.timeout_seconds)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
