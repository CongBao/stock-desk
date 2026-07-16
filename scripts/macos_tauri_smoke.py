"""Build a disposable macOS Tauri app and verify operator-driven native clicks.

Stock Desk v1.1 is released only for Windows.  This local development gate
still builds the real macOS Tauri/WKWebView application bundle so Codex
Computer Use can click the native title bar and the in-app exit dialog.  The
script waits for that short interaction, validates the driver's evidence, and
always removes the disposable Cargo target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "test-results" / "macos-tauri-smoke"
HOST_NAME = "stock-desk-desktop"
APP_NAME = "Stock Desk.app"
APP_IDENTIFIER = "com.baozijuan.stockdesk"
WINDOW_TITLE = "Stock Desk"
EXPECTED_ACTIONS = (
    "titlebar-close-open-dialog",
    "cancel-exit-dialog",
    "titlebar-close-reopen-dialog",
    "confirm-exit-dialog",
)
EXPECTED_OPERATOR_TARGETS = (
    ("native-titlebar", "close button", "close"),
    ("embedded-webview", "button", "取消"),
    ("native-titlebar", "close button", "close"),
    ("embedded-webview", "button", "退出应用"),
)
OPERATOR_EVIDENCE_WAIT_SECONDS = 60


class MacOSTauriSmokeError(RuntimeError):
    """The disposable native-host smoke did not produce trustworthy evidence."""


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _require_clean_source_identity(
    expected: tuple[str, str] | None = None,
) -> tuple[str, str]:
    source_sha = _git("rev-parse", "HEAD")
    source_tree = _git("rev-parse", "HEAD^{tree}")
    if any(
        len(value) != 40
        or not all(character in "0123456789abcdefABCDEF" for character in value)
        for value in (source_sha, source_tree)
    ):
        raise MacOSTauriSmokeError("macOS smoke source identity is invalid")
    if _git("status", "--porcelain=v1"):
        raise MacOSTauriSmokeError("macOS smoke requires a clean source tree")
    identity = (source_sha.lower(), source_tree.lower())
    if expected is not None and identity != expected:
        raise MacOSTauriSmokeError(
            "macOS smoke source identity changed during the gate"
        )
    return identity


def _screen_is_locked() -> bool:
    result = subprocess.run(
        ("ioreg", "-n", "Root", "-d1"),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return '"CGSSessionScreenIsLocked"=Yes' in result.stdout


def _host_pid(host_path: Path) -> int | None:
    result = subprocess.run(
        ("ps", "-axo", "pid=,command="),
        check=True,
        capture_output=True,
        text=True,
    )
    # macOS reports temporary executables through /private/var even when
    # tempfile returned the equivalent /var path.
    expected = os.path.realpath(host_path)
    for raw in result.stdout.splitlines():
        fields = raw.strip().split(maxsplit=1)
        if len(fields) == 2 and os.path.realpath(fields[1]) == expected:
            return int(fields[0])
    return None


def _wait_for_host(host_path: Path, timeout_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pid = _host_pid(host_path)
        if pid is not None:
            return pid
        time.sleep(0.25)
    raise MacOSTauriSmokeError("timed out waiting for the macOS Tauri host")


def _wait_for_host_exit(host_path: Path, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _host_pid(host_path) is None:
            return
        time.sleep(0.2)
    raise MacOSTauriSmokeError(
        "timed out waiting for Codex Computer Use to confirm the native exit"
    )


def _swift_window_probe(path: Path) -> None:
    path.write_text(
        r"""import CoreGraphics
import Foundation

guard CommandLine.arguments.count == 3,
      let pid = Int32(CommandLine.arguments[1]),
      let timeout = Double(CommandLine.arguments[2]) else {
  fputs("usage: window-probe PID TIMEOUT\n", stderr)
  exit(2)
}

let deadline = Date().addingTimeInterval(timeout)
repeat {
  let rows = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID)
    as? [[String: Any]] ?? []
  for row in rows {
    guard let owner = row[kCGWindowOwnerPID as String] as? NSNumber,
          owner.int32Value == pid else { continue }
    let title = row[kCGWindowName as String] as? String ?? ""
    let layer = (row[kCGWindowLayer as String] as? NSNumber)?.intValue ?? -1
    let onScreen = (row[kCGWindowIsOnscreen as String] as? NSNumber)?.boolValue ?? false
    guard title == "Stock Desk", layer == 0, onScreen,
          let bounds = row[kCGWindowBounds as String] as? [String: Any],
          let width = bounds["Width"] as? NSNumber,
          let height = bounds["Height"] as? NSNumber,
          let x = bounds["X"] as? NSNumber,
          let y = bounds["Y"] as? NSNumber,
          let number = row[kCGWindowNumber as String] as? NSNumber else { continue }
    let evidence: [String: Any] = [
      "title": title,
      "layer": layer,
      "on_screen": onScreen,
      "window_number": number.intValue,
      "x": x.doubleValue,
      "y": y.doubleValue,
      "width": width.doubleValue,
      "height": height.doubleValue,
    ]
    let encoded = try! JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys])
    FileHandle.standardOutput.write(encoded)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
  }
  Thread.sleep(forTimeInterval: 0.2)
} while Date() < deadline

fputs("on-screen Stock Desk native window was not observed\n", stderr)
exit(1)
""",
        encoding="utf-8",
    )


def _swift_interaction_observer(path: Path) -> None:
    path.write_text(
        r"""import ApplicationServices
import Darwin
import Foundation

func attribute(_ element: AXUIElement, _ name: CFString) -> CFTypeRef? {
  var value: CFTypeRef?
  guard AXUIElementCopyAttributeValue(element, name, &value) == .success else { return nil }
  return value
}

func stringAttribute(_ element: AXUIElement, _ name: CFString) -> String? {
  return attribute(element, name) as? String
}

func elementsAttribute(_ element: AXUIElement, _ name: CFString) -> [AXUIElement] {
  return attribute(element, name) as? [AXUIElement] ?? []
}

func processExists(_ pid: pid_t) -> Bool {
  if kill(pid, 0) == 0 { return true }
  return errno == EPERM
}

func mainWindow(_ application: AXUIElement) -> AXUIElement? {
  for window in elementsAttribute(application, kAXWindowsAttribute as CFString) {
    if stringAttribute(window, kAXTitleAttribute as CFString) == "Stock Desk" { return window }
  }
  return nil
}

func hasButton(_ expected: String, below root: AXUIElement) -> Bool {
  var stack: [(AXUIElement, Int)] = [(root, 0)]
  var visited = Set<CFHashCode>()
  while let (element, depth) = stack.popLast() {
    let identity = CFHash(element)
    if visited.contains(identity) || depth > 40 { continue }
    visited.insert(identity)
    if stringAttribute(element, kAXRoleAttribute as CFString) == (kAXButtonRole as String) {
      for name in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute] {
        if stringAttribute(element, name as CFString) == expected { return true }
      }
    }
    for child in elementsAttribute(element, kAXChildrenAttribute as CFString) {
      stack.append((child, depth + 1))
    }
  }
  return false
}

func emit(_ value: [String: Any]) {
  let data = try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
  FileHandle.standardOutput.write(data)
  FileHandle.standardOutput.write(Data("\n".utf8))
}

func fail(_ message: String) -> Never {
  fputs("macOS interaction observer failed: \(message)\n", stderr)
  exit(1)
}

guard CommandLine.arguments.count == 4,
      let pid = pid_t(CommandLine.arguments[1]),
      let timeout = Double(CommandLine.arguments[2]),
      timeout >= 30, timeout <= 900 else {
  fail("usage: observer PID TIMEOUT READY_PATH")
}
guard AXIsProcessTrusted() else { fail("accessibility permission is unavailable") }

let readyPath = URL(fileURLWithPath: CommandLine.arguments[3])
let application = AXUIElementCreateApplication(pid)
let deadline = Date().addingTimeInterval(timeout)
let formatter = ISO8601DateFormatter()
var stage = 0
var actions: [[String: Any]] = []

while Date() < deadline {
  if !processExists(pid) {
    if stage == 4 {
      actions.append([
        "action": "confirm-exit-dialog",
        "observed": true,
        "observed_at": formatter.string(from: Date()),
        "state": "host-process-exited",
      ])
      emit([
        "driver": "independent-state-observer",
        "process_id": Int(pid),
        "window_title": "Stock Desk",
        "actions": actions,
      ])
      exit(0)
    }
    fail("Stock Desk exited before the required state sequence completed")
  }
  guard let window = mainWindow(application) else {
    Thread.sleep(forTimeInterval: 0.05)
    continue
  }
  let safeExit = hasButton("安全退出", below: window)
  let cancel = hasButton("取消", below: window)
  let confirm = hasButton("退出应用", below: window)
  let now = formatter.string(from: Date())
  switch stage {
  case 0 where safeExit && !cancel && !confirm:
    let ready: [String: Any] = ["process_id": Int(pid), "state": "initial-recovery"]
    let data = try! JSONSerialization.data(withJSONObject: ready, options: [.sortedKeys])
    try! data.write(to: readyPath, options: [.atomic])
    stage = 1
  case 1 where cancel && confirm:
    actions.append(["action": "titlebar-close-open-dialog", "observed": true, "observed_at": now, "state": "exit-dialog-open"])
    stage = 2
  case 2 where safeExit && !cancel && !confirm:
    actions.append(["action": "cancel-exit-dialog", "observed": true, "observed_at": now, "state": "recovery-restored"])
    stage = 3
  case 3 where cancel && confirm:
    actions.append(["action": "titlebar-close-reopen-dialog", "observed": true, "observed_at": now, "state": "exit-dialog-reopened"])
    stage = 4
  default:
    break
  }
  Thread.sleep(forTimeInterval: 0.05)
}

fail("timed out waiting for the native click state sequence")
""",
        encoding="utf-8",
    )


def _observe_window(probe: Path, pid: int, timeout_seconds: int) -> dict[str, Any]:
    result = subprocess.run(
        ("swift", os.fspath(probe), str(pid), str(timeout_seconds)),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 60,
    )
    if result.returncode != 0:
        raise MacOSTauriSmokeError(
            f"native macOS window probe failed: {result.stderr.strip()}"
        )
    try:
        window = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MacOSTauriSmokeError(
            "native window probe returned invalid JSON"
        ) from error
    if not isinstance(window, dict):
        raise MacOSTauriSmokeError("native window probe returned an invalid object")
    if window["title"] != "Stock Desk":
        raise MacOSTauriSmokeError("native window title is not Stock Desk")
    if window["on_screen"] is not True:
        raise MacOSTauriSmokeError("native window is not on screen")
    if window["layer"] != 0:
        raise MacOSTauriSmokeError("native window is not a normal application window")
    if window["width"] < 640:
        raise MacOSTauriSmokeError(
            "native window is narrower than the supported minimum"
        )
    if window["height"] < 360:
        raise MacOSTauriSmokeError(
            "native window is shorter than the supported minimum"
        )
    return window


def _start_interaction_observer(
    source: Path,
    binary: Path,
    ready_path: Path,
    *,
    pid: int,
    timeout_seconds: int,
) -> subprocess.Popen[str]:
    compile_result = subprocess.run(
        (
            "swiftc",
            "-warnings-as-errors",
            os.fspath(source),
            "-o",
            os.fspath(binary),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if compile_result.returncode != 0:
        raise MacOSTauriSmokeError(
            f"macOS interaction observer did not compile: {compile_result.stderr.strip()}"
        )
    process = subprocess.Popen(
        (
            os.fspath(binary),
            str(pid),
            str(timeout_seconds),
            os.fspath(ready_path),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return process
        return_code = process.poll()
        if return_code is not None:
            _stdout, stderr = process.communicate()
            raise MacOSTauriSmokeError(
                f"macOS interaction observer exited before readiness: {stderr.strip()}"
            )
        time.sleep(0.05)
    process.terminate()
    process.wait(timeout=5)
    raise MacOSTauriSmokeError("macOS interaction observer did not become ready")


def _finish_interaction_observer(
    process: subprocess.Popen[str], timeout_seconds: int
) -> dict[str, Any]:
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MacOSTauriSmokeError("macOS interaction observer timed out") from error
    if process.returncode != 0:
        raise MacOSTauriSmokeError(
            f"macOS interaction observer failed: {stderr.strip()}"
        )
    try:
        evidence = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise MacOSTauriSmokeError(
            "macOS interaction observer returned invalid JSON"
        ) from error
    if not isinstance(evidence, dict):
        raise MacOSTauriSmokeError(
            "macOS interaction observer returned an invalid object"
        )
    actions = evidence.get("actions")
    if (
        evidence.get("driver") != "independent-state-observer"
        or not isinstance(actions, list)
        or len(actions) != len(EXPECTED_ACTIONS)
        or not all(isinstance(action, dict) for action in actions)
        or tuple(action.get("action") for action in actions) != EXPECTED_ACTIONS
        or any(action.get("observed") is not True for action in actions)
    ):
        raise MacOSTauriSmokeError(
            "macOS interaction observer state sequence is incomplete"
        )
    return evidence


def _load_operator_evidence(
    path: Path,
    *,
    source_sha: str,
    source_tree: str,
    session_nonce: str,
    host_pid: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + OPERATOR_EVIDENCE_WAIT_SECONDS
    while time.monotonic() < deadline and not path.is_file():
        time.sleep(0.1)
    if not path.is_file():
        raise MacOSTauriSmokeError("Codex Computer Use evidence was not written")
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MacOSTauriSmokeError("operator evidence is not valid JSON") from error
    if not isinstance(evidence, dict):
        raise MacOSTauriSmokeError("operator evidence is not an object")
    if set(evidence) != {
        "schema_version",
        "driver",
        "input_method",
        "physical_mouse_click",
        "source_sha",
        "source_tree",
        "session_nonce",
        "app_identifier",
        "host_pid",
        "actions",
    }:
        raise MacOSTauriSmokeError("operator evidence shape is invalid")
    if evidence.get("schema_version") != "stock-desk-macos-computer-use-v1":
        raise MacOSTauriSmokeError("operator evidence schema version is invalid")
    if evidence.get("driver") != "codex-computer-use":
        raise MacOSTauriSmokeError("operator evidence did not use Codex Computer Use")
    if evidence.get("input_method") != "codex-computer-use-sky-click":
        raise MacOSTauriSmokeError("operator evidence input method is invalid")
    if evidence.get("physical_mouse_click") is not True:
        raise MacOSTauriSmokeError(
            "operator evidence did not record physical mouse clicks"
        )
    if evidence.get("source_sha") != source_sha:
        raise MacOSTauriSmokeError("operator evidence source SHA does not match")
    if evidence.get("source_tree") != source_tree:
        raise MacOSTauriSmokeError("operator evidence source tree does not match")
    if evidence.get("session_nonce") != session_nonce:
        raise MacOSTauriSmokeError("operator evidence session nonce does not match")
    if evidence.get("app_identifier") != APP_IDENTIFIER:
        raise MacOSTauriSmokeError("operator evidence app identifier does not match")
    if evidence.get("host_pid") != host_pid:
        raise MacOSTauriSmokeError("operator evidence host PID does not match")
    actions = evidence.get("actions")
    if (
        not isinstance(actions, list)
        or len(actions) != len(EXPECTED_ACTIONS)
        or not all(isinstance(action, dict) for action in actions)
        or tuple(action.get("action") for action in actions) != EXPECTED_ACTIONS
    ):
        raise MacOSTauriSmokeError("operator evidence action sequence is invalid")
    if any(action.get("observed") is not True for action in actions):
        raise MacOSTauriSmokeError("operator evidence did not observe every action")
    for action, (surface, role, label) in zip(
        actions, EXPECTED_OPERATOR_TARGETS, strict=True
    ):
        if set(action) != {
            "action",
            "observed",
            "input_method",
            "physical_mouse_click",
            "surface",
            "role",
            "label",
            "element_index",
        }:
            raise MacOSTauriSmokeError("operator evidence action shape is invalid")
        element_index = action.get("element_index")
        if (
            action.get("input_method") != "sky.click"
            or action.get("physical_mouse_click") is not True
            or action.get("surface") != surface
            or action.get("role") != role
            or action.get("label") != label
            or isinstance(element_index, bool)
            or not isinstance(element_index, int)
            or element_index < 0
        ):
            raise MacOSTauriSmokeError("operator evidence click record is invalid")
    return evidence


def _stop_host(host_path: Path) -> None:
    pid = _host_pid(host_path)
    if pid is None:
        return
    for sent_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sent_signal)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _host_pid(host_path) is None:
                return
            time.sleep(0.1)
    raise MacOSTauriSmokeError("could not stop the disposable Tauri host")


def _unregister_bundle(app_path: Path) -> None:
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if lsregister.is_file() and app_path.is_dir():
        subprocess.run(
            (os.fspath(lsregister), "-u", os.fspath(app_path)),
            check=False,
            capture_output=True,
            timeout=30,
        )


def _validated_output(path: Path) -> Path:
    output = path.resolve()
    allowed_root = (ROOT / "test-results").resolve()
    try:
        relative = output.relative_to(allowed_root)
    except ValueError as error:
        raise MacOSTauriSmokeError(
            "macOS smoke output must stay under the ignored test-results directory"
        ) from error
    if not relative.parts:
        raise MacOSTauriSmokeError(
            "macOS smoke output cannot replace the entire test-results directory"
        )
    return output


def _cleanup_resources(
    *,
    host_path: Path,
    app_path: Path,
    temporary_root: Path,
    observer: subprocess.Popen[str] | None = None,
) -> None:
    errors: list[Exception] = []
    if observer is not None and observer.poll() is None:
        try:
            observer.terminate()
            observer.wait(timeout=5)
        except Exception as error:  # pragma: no cover - destructive fallback
            errors.append(error)
            try:
                observer.kill()
                observer.wait(timeout=5)
            except Exception as kill_error:  # pragma: no cover
                errors.append(kill_error)
    try:
        _stop_host(host_path)
    except Exception as error:  # pragma: no cover - destructive fallback
        errors.append(error)
    try:
        _unregister_bundle(app_path)
    except Exception as error:  # pragma: no cover - platform failure
        errors.append(error)
    try:
        shutil.rmtree(temporary_root, ignore_errors=False)
    except FileNotFoundError:
        pass
    except Exception as error:  # pragma: no cover - filesystem failure
        errors.append(error)
    if temporary_root.exists():
        errors.append(RuntimeError("temporary Cargo target still exists"))
    if errors:
        details = "; ".join(str(error) for error in errors)
        raise MacOSTauriSmokeError(
            f"macOS smoke cleanup failed: {details}"
        ) from errors[0]


def run_smoke(*, output: Path, timeout_seconds: int) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise MacOSTauriSmokeError("macOS Tauri smoke requires Darwin")
    for required_command in (
        "git",
        "ioreg",
        "pnpm",
        "rustc",
        "swift",
        "swiftc",
        "screencapture",
        "open",
    ):
        if shutil.which(required_command) is None:
            raise MacOSTauriSmokeError(
                f"required macOS smoke command is missing: {required_command}"
            )
    if _screen_is_locked():
        raise MacOSTauriSmokeError(
            "unlock the Mac before running the native Tauri click smoke"
        )

    output = _validated_output(output)
    source_sha, source_tree = _require_clean_source_identity()
    source_identity = (source_sha, source_tree)
    session_nonce = uuid.uuid4().hex
    temporary_root = Path(tempfile.mkdtemp(prefix="stock-desk-tauri-macos-smoke-"))
    cargo_target = temporary_root / "cargo-target"
    probe = temporary_root / "window_probe.swift"
    observer_source = temporary_root / "interaction_observer.swift"
    observer_binary = temporary_root / "interaction-observer"
    observer_ready_path = temporary_root / "interaction-observer-ready.json"
    app_path = cargo_target / "debug" / "bundle" / "macos" / APP_NAME
    host_path = app_path / "Contents" / "MacOS" / HOST_NAME
    try:
        _swift_window_probe(probe)
        _swift_interaction_observer(observer_source)
        shutil.rmtree(output, ignore_errors=True)
        output.mkdir(parents=True, exist_ok=True)
    except BaseException:
        active_error = sys.exception()
        try:
            _cleanup_resources(
                host_path=host_path,
                app_path=app_path,
                temporary_root=temporary_root,
            )
        except Exception as cleanup_error:
            if active_error is not None:
                active_error.add_note(f"cleanup also failed: {cleanup_error}")
        raise
    build_log_path = output / "tauri-build.log"
    screenshot_path = output / "stock-desk-native-window.png"
    operator_evidence_path = output / "operator-evidence.json"
    ready_path = output / "interaction-ready.json"
    environment = os.environ.copy()
    environment.update(
        {
            "CARGO_TARGET_DIR": os.fspath(cargo_target),
            "STOCK_DESK_SOURCE_REVISION": source_sha,
        }
    )
    build_command = (
        "pnpm",
        "exec",
        "tauri",
        "build",
        "--config",
        "src-tauri/tauri.conf.json",
        "--debug",
        "--bundles",
        "app",
        "--no-sign",
        "--ci",
    )
    host_pid: int | None = None
    window: dict[str, Any] | None = None
    operator_evidence: dict[str, Any] | None = None
    observer_evidence: dict[str, Any] | None = None
    observer: subprocess.Popen[str] | None = None
    try:
        with build_log_path.open("wb") as build_log:
            build = subprocess.run(
                build_command,
                cwd=ROOT,
                env=environment,
                stdout=build_log,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                check=False,
            )
        if build.returncode != 0 or not app_path.is_dir():
            raise MacOSTauriSmokeError(
                f"macOS debug app build failed; inspect {build_log_path}"
            )
        _require_clean_source_identity(source_identity)
        subprocess.run(
            ("open", "-n", os.fspath(app_path)),
            check=True,
            timeout=30,
        )
        host_pid = _wait_for_host(host_path, min(timeout_seconds, 60))
        window = _observe_window(probe, host_pid, min(timeout_seconds, 60))
        subprocess.run(
            (
                "screencapture",
                "-x",
                "-l",
                str(window["window_number"]),
                os.fspath(screenshot_path),
            ),
            check=True,
            timeout=30,
        )
        if screenshot_path.stat().st_size < 1_024:
            raise MacOSTauriSmokeError("native-window screenshot is unexpectedly small")
        observer = _start_interaction_observer(
            observer_source,
            observer_binary,
            observer_ready_path,
            pid=host_pid,
            timeout_seconds=timeout_seconds,
        )
        ready = {
            "schema_version": "stock-desk-macos-click-ready-v1",
            "source_sha": source_sha,
            "source_tree": source_tree,
            "session_nonce": session_nonce,
            "app_identifier": APP_IDENTIFIER,
            "host_pid": host_pid,
            "input_method": "codex-computer-use-sky-click",
            "physical_mouse_click": True,
            "expected_actions": list(EXPECTED_ACTIONS),
            "operator_evidence_path": os.fspath(operator_evidence_path),
            "independent_observer": "ready",
        }
        ready_path.write_text(
            json.dumps(ready, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            "Stock Desk is ready for Codex Computer Use: close, cancel, close, exit.",
            flush=True,
        )
        observer_evidence = _finish_interaction_observer(observer, timeout_seconds)
        _wait_for_host_exit(host_path, 10)
        operator_evidence = _load_operator_evidence(
            operator_evidence_path,
            source_sha=source_sha,
            source_tree=source_tree,
            session_nonce=session_nonce,
            host_pid=host_pid,
        )
    finally:
        active_error = sys.exception()
        try:
            _cleanup_resources(
                host_path=host_path,
                app_path=app_path,
                temporary_root=temporary_root,
                observer=observer,
            )
        except Exception as cleanup_error:
            if active_error is None:
                raise
            active_error.add_note(f"cleanup also failed: {cleanup_error}")

    if (
        window is None
        or host_pid is None
        or operator_evidence is None
        or observer_evidence is None
    ):
        raise MacOSTauriSmokeError("macOS native interaction evidence is incomplete")
    if temporary_root.exists() or _host_pid(host_path) is not None:
        raise MacOSTauriSmokeError("macOS smoke left a process or Cargo target behind")
    _require_clean_source_identity(source_identity)

    screenshot_sha256 = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
    native_click_sequence_confirmed = bool(
        operator_evidence["physical_mouse_click"] is True
        and all(
            action["input_method"] == "sky.click"
            and action["physical_mouse_click"] is True
            for action in operator_evidence["actions"]
        )
    )
    evidence: dict[str, Any] = {
        "schema_version": "stock-desk-macos-tauri-smoke-v3",
        "scope": "macos-tauri-host-recovery-smoke",
        "source_sha": source_sha,
        "source_tree": source_tree,
        "source_dirty": False,
        "host_pid": host_pid,
        "host_binary": os.fspath(host_path),
        "native_window": window,
        "embedded_webview": "WKWebView",
        "external_browser_opened": False,
        "driver": operator_evidence["driver"],
        "input_method": operator_evidence["input_method"],
        "physical_mouse_click": operator_evidence["physical_mouse_click"],
        "actions": observer_evidence["actions"],
        "operator_actions": operator_evidence["actions"],
        "interaction_observer": observer_evidence,
        "native_click_sequence_confirmed": native_click_sequence_confirmed,
        "independent_state_sequence_confirmed": True,
        "process_exit_observed": True,
        "screenshot": {
            "path": screenshot_path.name,
            "sha256": screenshot_sha256,
            "size": screenshot_path.stat().st_size,
        },
        "process_cleanup_confirmed": True,
        "temporary_cargo_target_removed": True,
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "limitations": [
            "Stock Desk v1.1 remains a Windows-only release.",
            "The macOS development host intentionally enters sidecar recovery because no macOS sidecar is shipped.",
            "Windows sidecar, NSIS, and Windows OS behavior remain authoritative on the GitHub Windows runner and a real standard-user Win10 machine.",
        ],
    }
    evidence_path = output / "macos-tauri-smoke.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parsed = parser.parse_args(arguments)
    if parsed.timeout_seconds < 60 or parsed.timeout_seconds > 900:
        parser.error("--timeout-seconds must be between 60 and 900")
    evidence = run_smoke(output=parsed.output, timeout_seconds=parsed.timeout_seconds)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
