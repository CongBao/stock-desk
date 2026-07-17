from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import macos_tauri_smoke, macos_tauri_support


ROOT = Path(__file__).resolve().parents[2]


def test_shared_macos_support_tracks_and_stops_only_verified_process_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = tmp_path / "Stock Desk.app" / "Contents" / "MacOS" / "stock-desk-desktop"
    sidecar = host.parent / "stock-desk-sidecar"
    rows = {
        42: macos_tauri_support.ProcessInfo(42, 1, "host-start", os.fspath(host)),
        43: macos_tauri_support.ProcessInfo(
            43, 42, "sidecar-start", os.fspath(sidecar)
        ),
        44: macos_tauri_support.ProcessInfo(
            44, 43, "worker-start", f"{sidecar} --multiprocessing-fork"
        ),
        99: macos_tauri_support.ProcessInfo(
            99, 1, "unrelated-start", "/usr/bin/unrelated"
        ),
    }
    monkeypatch.setattr(macos_tauri_support, "process_table", lambda: rows.copy())
    signals: list[tuple[int, int]] = []

    def kill(pid: int, sent_signal: int) -> None:
        signals.append((pid, sent_signal))
        rows.pop(pid, None)

    monkeypatch.setattr(macos_tauri_support.os, "kill", kill)
    tree = macos_tauri_support.VerifiedProcessTree(
        root_pid=42,
        host_path=host,
        allowed_root=tmp_path,
    )

    tree.observe()
    assert tree.sidecar_pid() == 43
    tree.terminate(timeout_seconds=1)

    assert [pid for pid, _sent_signal in signals] == [44, 43, 42]
    assert 99 in rows


def test_shared_macos_support_ignores_same_command_pid_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = tmp_path / "Stock Desk.app" / "Contents" / "MacOS" / "stock-desk-desktop"
    sidecar = host.parent / "stock-desk-sidecar"
    rows = {
        42: macos_tauri_support.ProcessInfo(42, 1, "host-start", os.fspath(host)),
        43: macos_tauri_support.ProcessInfo(
            43, 42, "old-sidecar-start", os.fspath(sidecar)
        ),
    }
    monkeypatch.setattr(macos_tauri_support, "process_table", lambda: rows.copy())
    signals: list[int] = []

    def kill(pid: int, _sent_signal: int) -> None:
        signals.append(pid)
        rows.pop(pid, None)

    monkeypatch.setattr(macos_tauri_support.os, "kill", kill)
    tree = macos_tauri_support.VerifiedProcessTree(42, host, tmp_path)

    tree.observe()
    assert tree.sidecar_pid() == 43
    rows[43] = macos_tauri_support.ProcessInfo(
        43, 42, "reused-sidecar-start", os.fspath(sidecar)
    )

    tree.terminate(timeout_seconds=1)

    assert signals == [42]
    assert 43 in rows


def test_shared_macos_support_discovers_late_descendants_before_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = tmp_path / "Stock Desk.app" / "Contents" / "MacOS" / "stock-desk-desktop"
    sidecar = host.parent / "stock-desk-sidecar"
    rows = {
        42: macos_tauri_support.ProcessInfo(42, 1, "host-start", os.fspath(host)),
        43: macos_tauri_support.ProcessInfo(
            43, 42, "sidecar-start", os.fspath(sidecar)
        ),
    }
    monkeypatch.setattr(macos_tauri_support, "process_table", lambda: rows.copy())
    signals: list[int] = []

    def kill(pid: int, _sent_signal: int) -> None:
        signals.append(pid)
        rows.pop(pid, None)

    monkeypatch.setattr(macos_tauri_support.os, "kill", kill)
    tree = macos_tauri_support.VerifiedProcessTree(42, host, tmp_path)

    tree.observe()
    rows[44] = macos_tauri_support.ProcessInfo(
        44, 43, "late-worker-start", f"{sidecar} --multiprocessing-fork"
    )
    tree.observe()
    tree.terminate(timeout_seconds=1)

    assert signals == [44, 43, 42]


def test_shared_macos_support_parses_stable_process_start_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = (
        " 42 1 Fri Jul 18 10:11:12 2026 /tmp/stock-desk-desktop\n"
        " 43 42 Fri Jul 18 10:11:13 2026 /tmp/stock-desk-sidecar --child\n"
    )
    monkeypatch.setattr(
        macos_tauri_support.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=stdout),
    )

    rows = macos_tauri_support.process_table()

    assert rows[42].start_time == "Fri Jul 18 10:11:12 2026"
    assert rows[43].command == "/tmp/stock-desk-sidecar --child"


def _operator_action_records() -> list[dict[str, object]]:
    return [
        {
            "action": action,
            "observed": True,
            "input_method": "sky.click",
            "physical_mouse_click": True,
            "surface": surface,
            "role": role,
            "label": label,
            "element_index": element_index,
        }
        for action, surface, role, label, element_index in (
            (
                "titlebar-close-open-dialog",
                "native-titlebar",
                "close button",
                "close",
                15,
            ),
            (
                "cancel-exit-dialog",
                "embedded-webview",
                "button",
                "取消",
                31,
            ),
            (
                "titlebar-close-reopen-dialog",
                "native-titlebar",
                "close button",
                "close",
                15,
            ),
            (
                "confirm-exit-dialog",
                "embedded-webview",
                "button",
                "退出应用",
                32,
            ),
        )
    ]


def _bound_operator_evidence() -> dict[str, object]:
    return {
        "schema_version": "stock-desk-macos-computer-use-v1",
        "driver": "codex-computer-use",
        "input_method": "codex-computer-use-sky-click",
        "physical_mouse_click": True,
        "source_sha": "a" * 40,
        "source_tree": "b" * 40,
        "session_nonce": "nonce",
        "app_identifier": macos_tauri_smoke.APP_IDENTIFIER,
        "host_pid": 42,
        "actions": _operator_action_records(),
    }


def test_macos_tauri_smoke_runs_a_real_native_window_and_cleans_up() -> None:
    script_path = ROOT / "scripts" / "macos_tauri_smoke.py"
    capability_path = ROOT / "src-tauri" / "capabilities" / "macos-smoke.json"
    assert script_path.is_file()
    source = script_path.read_text(encoding="utf-8")
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    tauri_config = json.loads(
        (ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    assert capability_path.is_file()
    capability = json.loads(capability_path.read_text(encoding="utf-8"))

    assert package["scripts"]["desktop:smoke:macos"] == (
        "uv run --frozen python scripts/macos_tauri_smoke.py"
    )
    assert capability == {
        "$schema": "../gen/schemas/desktop-schema.json",
        "identifier": "macos-smoke",
        "description": "Local macOS Tauri interaction smoke capability; not a macOS release target",
        "windows": ["main"],
        "platforms": ["macOS"],
        "permissions": ["core:event:default"],
    }
    assert tauri_config["app"]["security"]["capabilities"] == [
        "default",
        "macos-smoke",
    ]
    for contract in (
        'platform.system() != "Darwin"',
        "CARGO_TARGET_DIR",
        "STOCK_DESK_SOURCE_REVISION",
        '"--debug"',
        '"--bundles"',
        '"app"',
        '"--no-sign"',
        '"--ci"',
        '"open"',
        '"-n"',
        '"Contents" / "MacOS" / HOST_NAME',
        "CGWindowListCopyWindowInfo",
        "AXUIElementCreateApplication",
        "independent-state-observer",
        'window["title"] != "Stock Desk"',
        'window["on_screen"] is not True',
        'window["layer"] != 0',
        'window["width"] < 640',
        'window["height"] < 360',
        '"screencapture"',
        '"-x"',
        '"-l"',
        '"macos-tauri-host-recovery-smoke"',
        '"external_browser_opened": False',
        '"operator-evidence.json"',
        'evidence.get("driver") != "codex-computer-use"',
        'evidence.get("source_sha") != source_sha',
        'evidence.get("source_tree") != source_tree',
        'evidence.get("host_pid") != host_pid',
        'evidence.get("physical_mouse_click") is not True',
        "_require_clean_source_identity(source_identity)",
        '"titlebar-close-open-dialog"',
        '"cancel-exit-dialog"',
        '"titlebar-close-reopen-dialog"',
        '"confirm-exit-dialog"',
        '"native_click_sequence_confirmed": native_click_sequence_confirmed',
        '"independent_state_sequence_confirmed": True',
        '"process_exit_observed": True',
        '"LaunchServices.framework/Support/lsregister"',
        '"-u"',
        "output.relative_to(allowed_root)",
        '"macOS smoke output cannot replace the entire test-results directory"',
        "shutil.rmtree",
    ):
        assert contract in source
    for command in ("pnpm", "exec", "tauri", "build"):
        assert f'"{command}"' in source
    assert "webbrowser.open" not in source
    assert "macos_tauri_real_click.swift" not in source
    assert '"tauri", "dev"' not in source


def test_macos_tauri_smoke_keeps_abnormal_cleanup_owned_by_the_harness() -> None:
    smoke = (ROOT / "scripts" / "macos_tauri_smoke.py").read_text(encoding="utf-8")
    exit_source = (ROOT / "src-tauri" / "src" / "exit.rs").read_text(encoding="utf-8")

    graceful_wait = smoke.index("_wait_for_host_exit(host_path, 10)")
    cleanup_boundary = smoke.index("finally:", graceful_wait)
    abnormal_cleanup = smoke.index("_cleanup_resources(", cleanup_boundary)
    assert graceful_wait < cleanup_boundary < abnormal_cleanup
    assert "_stop_host(host_path)" in smoke
    assert "WindowsJob" not in smoke
    assert "fn accepted_shutdown_commits_when_the_sidecar_terminates" in exit_source
    assert "machine.terminated(9, Some(0)), ExitEffect::Exit" in exit_source


def test_macos_tauri_smoke_rejects_destructive_output_paths() -> None:
    accepted = ROOT / "test-results" / "macos-tauri-smoke-test"
    assert macos_tauri_smoke._validated_output(accepted) == accepted.resolve()

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="must stay under",
    ):
        macos_tauri_smoke._validated_output(ROOT)
    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="cannot replace the entire",
    ):
        macos_tauri_smoke._validated_output(ROOT / "test-results")


def test_macos_tauri_smoke_rejects_a_locked_console_before_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(macos_tauri_smoke.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(macos_tauri_smoke.shutil, "which", lambda _command: "/bin/tool")
    monkeypatch.setattr(macos_tauri_smoke, "_screen_is_locked", lambda: True)

    def unexpected_git(*_arguments: str) -> str:
        raise AssertionError(
            "locked-session preflight must run before source/build work"
        )

    monkeypatch.setattr(macos_tauri_smoke, "_git", unexpected_git)

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="unlock the Mac",
    ):
        macos_tauri_smoke.run_smoke(
            output=tmp_path / "test-results" / "macos-tauri-smoke",
            timeout_seconds=300,
        )


def test_macos_tauri_smoke_requires_a_clean_bound_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): "a" * 40,
        ("rev-parse", "HEAD^{tree}"): "b" * 40,
        ("status", "--porcelain=v1"): "",
    }
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_git",
        lambda *arguments: values[arguments],
    )

    identity = macos_tauri_smoke._require_clean_source_identity()

    assert identity == ("a" * 40, "b" * 40)
    assert macos_tauri_smoke._require_clean_source_identity(identity) == identity


def test_macos_tauri_smoke_rejects_dirty_or_changed_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): "a" * 40,
        ("rev-parse", "HEAD^{tree}"): "b" * 40,
        ("status", "--porcelain=v1"): " M web/src/app/App.tsx",
    }
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_git",
        lambda *arguments: values[arguments],
    )

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="clean source tree",
    ):
        macos_tauri_smoke._require_clean_source_identity()

    values[("status", "--porcelain=v1")] = ""
    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="changed during",
    ):
        macos_tauri_smoke._require_clean_source_identity(("c" * 40, "b" * 40))


def test_macos_tauri_smoke_cleanup_removes_target_after_other_cleanup_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    temporary_root = tmp_path / "cargo-target-root"
    temporary_root.mkdir()
    unregister_calls: list[Path] = []

    def fail_stop(_host_path: Path) -> None:
        raise RuntimeError("stop failed")

    def record_unregister(app_path: Path) -> None:
        unregister_calls.append(app_path)
        raise RuntimeError("unregister failed")

    monkeypatch.setattr(macos_tauri_smoke, "_stop_host", fail_stop)
    monkeypatch.setattr(macos_tauri_smoke, "_unregister_bundle", record_unregister)

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="cleanup failed",
    ):
        macos_tauri_smoke._cleanup_resources(
            host_path=tmp_path / "host",
            app_path=tmp_path / "Stock Desk.app",
            temporary_root=temporary_root,
        )

    assert unregister_calls == [tmp_path / "Stock Desk.app"]
    assert not temporary_root.exists()


def test_macos_tauri_smoke_process_helpers_observe_the_exact_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = tmp_path / "Stock Desk.app" / "Contents" / "MacOS" / "stock-desk-desktop"
    result = SimpleNamespace(stdout=f"17 /usr/bin/other\n42 {os.path.realpath(host)}\n")
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: result,
    )

    assert macos_tauri_smoke._host_pid(host) == 42
    assert macos_tauri_smoke._wait_for_host(host, 1) == 42

    result.stdout = "17 /usr/bin/other\n"
    assert macos_tauri_smoke._host_pid(host) is None
    macos_tauri_smoke._wait_for_host_exit(host, 1)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ('"CGSSessionScreenIsLocked"=Yes', True),
        ('"CGSSessionScreenIsLocked"=No', False),
    ],
)
def test_macos_tauri_smoke_detects_console_lock_state(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: bool
) -> None:
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=stdout),
    )

    assert macos_tauri_smoke._screen_is_locked() is expected


def test_macos_tauri_smoke_observes_a_valid_native_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = {
        "title": "Stock Desk",
        "on_screen": True,
        "layer": 0,
        "width": 900,
        "height": 600,
        "window_number": 81,
    }
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(expected),
            stderr="",
        ),
    )

    assert (
        macos_tauri_smoke._observe_window(tmp_path / "probe.swift", 42, 60) == expected
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "invalid object"),
        ({"title": "Other"}, "title is not"),
        (
            {
                "title": "Stock Desk",
                "on_screen": False,
                "layer": 0,
                "width": 900,
                "height": 600,
            },
            "not on screen",
        ),
        (
            {
                "title": "Stock Desk",
                "on_screen": True,
                "layer": 1,
                "width": 900,
                "height": 600,
            },
            "normal application window",
        ),
        (
            {
                "title": "Stock Desk",
                "on_screen": True,
                "layer": 0,
                "width": 639,
                "height": 600,
            },
            "narrower",
        ),
        (
            {
                "title": "Stock Desk",
                "on_screen": True,
                "layer": 0,
                "width": 900,
                "height": 359,
            },
            "shorter",
        ),
    ],
)
def test_macos_tauri_smoke_rejects_invalid_native_window_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    with pytest.raises(macos_tauri_smoke.MacOSTauriSmokeError, match=message):
        macos_tauri_smoke._observe_window(tmp_path / "probe.swift", 42, 60)


def test_macos_tauri_smoke_starts_and_finishes_independent_observer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ready = tmp_path / "ready.json"
    ready.write_text("{}", encoding="utf-8")
    expected_actions = [
        {"action": action, "observed": True}
        for action in macos_tauri_smoke.EXPECTED_ACTIONS
    ]
    observer_evidence = {
        "driver": "independent-state-observer",
        "actions": expected_actions,
    }
    process = SimpleNamespace(
        poll=lambda: None,
        communicate=lambda timeout: (json.dumps(observer_evidence), ""),
        returncode=0,
    )
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    started = macos_tauri_smoke._start_interaction_observer(
        tmp_path / "observer.swift",
        tmp_path / "observer",
        ready,
        pid=42,
        timeout_seconds=300,
    )

    assert started is process
    assert macos_tauri_smoke._finish_interaction_observer(started, 300) == (
        observer_evidence
    )


def test_macos_tauri_smoke_rejects_observer_compile_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stderr="compile failed",
        ),
    )

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="did not compile",
    ):
        macos_tauri_smoke._start_interaction_observer(
            tmp_path / "observer.swift",
            tmp_path / "observer",
            tmp_path / "ready.json",
            pid=42,
            timeout_seconds=300,
        )


@pytest.mark.parametrize(
    ("stdout", "returncode", "message"),
    [
        ("", 1, "observer failed"),
        ("{", 0, "invalid JSON"),
        ("[]", 0, "invalid object"),
        (json.dumps({"driver": "other", "actions": []}), 0, "incomplete"),
    ],
)
def test_macos_tauri_smoke_rejects_invalid_observer_results(
    stdout: str,
    returncode: int,
    message: str,
) -> None:
    process = SimpleNamespace(
        communicate=lambda timeout: (stdout, "observer failed"),
        returncode=returncode,
    )

    with pytest.raises(macos_tauri_smoke.MacOSTauriSmokeError, match=message):
        macos_tauri_smoke._finish_interaction_observer(process, 300)


def test_macos_tauri_smoke_rejects_observer_that_exits_before_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = SimpleNamespace(
        poll=lambda: 1,
        communicate=lambda: ("", "early exit"),
    )
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="exited before readiness",
    ):
        macos_tauri_smoke._start_interaction_observer(
            tmp_path / "observer.swift",
            tmp_path / "observer",
            tmp_path / "ready.json",
            pid=42,
            timeout_seconds=300,
        )


def test_macos_tauri_smoke_rejects_observer_readiness_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: events.append("terminate"),
        wait=lambda timeout: events.append(f"wait:{timeout}"),
    )
    ticks = iter((0.0, 16.0))
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )
    monkeypatch.setattr(
        macos_tauri_smoke.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(macos_tauri_smoke.time, "monotonic", lambda: next(ticks))

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="did not become ready",
    ):
        macos_tauri_smoke._start_interaction_observer(
            tmp_path / "observer.swift",
            tmp_path / "observer",
            tmp_path / "ready.json",
            pid=42,
            timeout_seconds=300,
        )

    assert events == ["terminate", "wait:5"]


def test_macos_tauri_smoke_rejects_observer_finish_timeout() -> None:
    def timeout(*, timeout: int) -> tuple[str, str]:
        raise macos_tauri_smoke.subprocess.TimeoutExpired("observer", timeout)

    process = SimpleNamespace(communicate=timeout, returncode=None)

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="observer timed out",
    ):
        macos_tauri_smoke._finish_interaction_observer(process, 300)


def test_macos_tauri_smoke_loads_bound_operator_evidence(tmp_path: Path) -> None:
    path = tmp_path / "operator-evidence.json"
    expected = _bound_operator_evidence()
    path.write_text(json.dumps(expected), encoding="utf-8")

    assert (
        macos_tauri_smoke._load_operator_evidence(
            path,
            source_sha="a" * 40,
            source_tree="b" * 40,
            session_nonce="nonce",
            host_pid=42,
        )
        == expected
    )


def test_macos_tauri_smoke_allows_operator_evidence_after_fifteen_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "operator-evidence.json"
    expected = _bound_operator_evidence()
    ticks = iter((0.0, 15.0, 16.0))
    monkeypatch.setattr(macos_tauri_smoke.time, "monotonic", lambda: next(ticks))

    def write_delayed_evidence(_seconds: float) -> None:
        path.write_text(json.dumps(expected), encoding="utf-8")

    monkeypatch.setattr(macos_tauri_smoke.time, "sleep", write_delayed_evidence)

    assert (
        macos_tauri_smoke._load_operator_evidence(
            path,
            source_sha="a" * 40,
            source_tree="b" * 40,
            session_nonce="nonce",
            host_pid=42,
        )
        == expected
    )


def test_macos_tauri_smoke_rejects_missing_operator_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ticks = iter((0.0, 61.0))
    monkeypatch.setattr(macos_tauri_smoke.time, "monotonic", lambda: next(ticks))

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="was not written",
    ):
        macos_tauri_smoke._load_operator_evidence(
            tmp_path / "missing.json",
            source_sha="a" * 40,
            source_tree="b" * 40,
            session_nonce="nonce",
            host_pid=42,
        )


def test_macos_tauri_smoke_rejects_invalid_operator_json(tmp_path: Path) -> None:
    path = tmp_path / "operator-evidence.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(
        macos_tauri_smoke.MacOSTauriSmokeError,
        match="not valid JSON",
    ):
        macos_tauri_smoke._load_operator_evidence(
            path,
            source_sha="a" * 40,
            source_tree="b" * 40,
            session_nonce="nonce",
            host_pid=42,
        )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda evidence: [], "not an object"),
        (
            lambda evidence: {**evidence, "driver": "other"},
            "did not use Codex",
        ),
        (
            lambda evidence: {**evidence, "source_sha": "b" * 40},
            "source SHA",
        ),
        (
            lambda evidence: {**evidence, "source_tree": "c" * 40},
            "source tree",
        ),
        (
            lambda evidence: {**evidence, "session_nonce": "other"},
            "session nonce",
        ),
        (
            lambda evidence: {**evidence, "app_identifier": "other"},
            "app identifier",
        ),
        (
            lambda evidence: {**evidence, "host_pid": 41},
            "host PID",
        ),
        (
            lambda evidence: {**evidence, "physical_mouse_click": False},
            "physical mouse clicks",
        ),
        (
            lambda evidence: {**evidence, "input_method": "other"},
            "input method",
        ),
        (
            lambda evidence: {**evidence, "actions": []},
            "action sequence",
        ),
        (
            lambda evidence: {
                **evidence,
                "actions": [
                    *evidence["actions"][:-1],
                    {**evidence["actions"][-1], "observed": False},
                ],
            },
            "observe every action",
        ),
        (
            lambda evidence: {
                **evidence,
                "actions": [
                    {**evidence["actions"][0], "input_method": "keyboard"},
                    *evidence["actions"][1:],
                ],
            },
            "click record",
        ),
    ],
)
def test_macos_tauri_smoke_rejects_unbound_operator_evidence(
    tmp_path: Path,
    mutator: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    base = _bound_operator_evidence()
    invalid = mutator(base)
    path = tmp_path / "operator-evidence.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(macos_tauri_smoke.MacOSTauriSmokeError, match=message):
        macos_tauri_smoke._load_operator_evidence(
            path,
            source_sha="a" * 40,
            source_tree="b" * 40,
            session_nonce="nonce",
            host_pid=42,
        )


def test_macos_tauri_smoke_stops_host_after_term(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_pids = iter((42, None))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_host_pid",
        lambda _path: next(observed_pids),
    )
    monkeypatch.setattr(
        macos_tauri_smoke.os,
        "kill",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    macos_tauri_smoke._stop_host(tmp_path / "host")

    assert signals == [(42, macos_tauri_smoke.signal.SIGTERM)]


def test_macos_tauri_smoke_accepts_host_disappearing_during_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(macos_tauri_smoke, "_host_pid", lambda _path: 42)

    def disappeared(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(macos_tauri_smoke.os, "kill", disappeared)

    macos_tauri_smoke._stop_host(tmp_path / "host")


def test_macos_tauri_smoke_runs_disposable_app_orchestration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = ROOT / "test-results" / "macos-tauri-smoke-orchestration-test"
    temporary_root = tmp_path / "temporary-root"
    app_path = (
        temporary_root
        / "cargo-target"
        / "debug"
        / "bundle"
        / "macos"
        / macos_tauri_smoke.APP_NAME
    )
    host_path = app_path / "Contents" / "MacOS" / macos_tauri_smoke.HOST_NAME
    host_path.parent.mkdir(parents=True)
    host_path.write_bytes(b"host")
    observer_actions = [
        {"action": action, "observed": True}
        for action in macos_tauri_smoke.EXPECTED_ACTIONS
    ]
    observer_evidence = {
        "driver": "independent-state-observer",
        "actions": observer_actions,
    }
    operator_evidence = _bound_operator_evidence()
    window = {
        "title": "Stock Desk",
        "on_screen": True,
        "layer": 0,
        "width": 900,
        "height": 600,
        "window_number": 81,
    }
    observer = SimpleNamespace(poll=lambda: 0)
    git_calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(macos_tauri_smoke.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        macos_tauri_smoke.platform,
        "platform",
        lambda: "macOS-test",
    )
    monkeypatch.setattr(macos_tauri_smoke.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(macos_tauri_smoke.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(macos_tauri_smoke, "_screen_is_locked", lambda: False)

    def fake_git(*arguments: str) -> str:
        git_calls.append(arguments)
        return {
            ("rev-parse", "HEAD"): "a" * 40,
            ("rev-parse", "HEAD^{tree}"): "b" * 40,
            ("status", "--porcelain=v1"): "",
        }[arguments]

    monkeypatch.setattr(macos_tauri_smoke, "_git", fake_git)
    monkeypatch.setattr(
        macos_tauri_smoke.tempfile,
        "mkdtemp",
        lambda **_kwargs: os.fspath(temporary_root),
    )
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_swift_window_probe",
        lambda path: path.write_text("probe", encoding="utf-8"),
    )
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_swift_interaction_observer",
        lambda path: path.write_text("observer", encoding="utf-8"),
    )

    def fake_run(arguments: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        if arguments[0] == "screencapture":
            Path(arguments[-1]).write_bytes(b"image" * 300)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(macos_tauri_smoke.subprocess, "run", fake_run)
    monkeypatch.setattr(macos_tauri_smoke, "_wait_for_host", lambda *_args: 4242)
    monkeypatch.setattr(macos_tauri_smoke, "_observe_window", lambda *_args: window)
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_start_interaction_observer",
        lambda *_args, **_kwargs: observer,
    )
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_finish_interaction_observer",
        lambda *_args: observer_evidence,
    )
    monkeypatch.setattr(macos_tauri_smoke, "_wait_for_host_exit", lambda *_args: None)
    monkeypatch.setattr(
        macos_tauri_smoke,
        "_load_operator_evidence",
        lambda *_args, **_kwargs: operator_evidence,
    )
    monkeypatch.setattr(macos_tauri_smoke, "_host_pid", lambda *_args: None)
    monkeypatch.setattr(macos_tauri_smoke, "_unregister_bundle", lambda *_args: None)

    try:
        evidence = macos_tauri_smoke.run_smoke(
            output=output,
            timeout_seconds=300,
        )
        assert evidence["source_sha"] == "a" * 40
        assert evidence["source_tree"] == "b" * 40
        assert evidence["host_pid"] == 4242
        assert evidence["native_click_sequence_confirmed"] is True
        assert evidence["process_cleanup_confirmed"] is True
        assert git_calls.count(("status", "--porcelain=v1")) == 3
        assert (output / "macos-tauri-smoke.json").is_file()
        assert not temporary_root.exists()
    finally:
        macos_tauri_smoke.shutil.rmtree(output, ignore_errors=True)


def test_macos_tauri_smoke_main_runs_valid_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        macos_tauri_smoke,
        "run_smoke",
        lambda *, output, timeout_seconds: (
            captured.append((output, timeout_seconds)) or {"status": "passed"}
        ),
    )

    assert (
        macos_tauri_smoke.main(
            ["--output", os.fspath(tmp_path), "--timeout-seconds", "60"]
        )
        == 0
    )
    assert captured == [(tmp_path, 60)]


def test_macos_tauri_smoke_main_rejects_invalid_timeout() -> None:
    with pytest.raises(SystemExit):
        macos_tauri_smoke.main(["--timeout-seconds", "59"])
