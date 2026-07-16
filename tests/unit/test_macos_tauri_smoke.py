from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import macos_tauri_smoke


ROOT = Path(__file__).resolve().parents[2]


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
        '"driver": "codex-computer-use"',
        'evidence.get("source_sha") != source_sha',
        '"titlebar-close-open-dialog"',
        '"cancel-exit-dialog"',
        '"titlebar-close-reopen-dialog"',
        '"confirm-exit-dialog"',
        '"native_click_sequence_confirmed": True',
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
