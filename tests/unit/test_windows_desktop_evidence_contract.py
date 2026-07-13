from pathlib import Path
import json
import re

from stock_desk.sidecar import _SHUTDOWN_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]


def test_windows_candidate_runs_packaged_tauri_and_binds_visual_evidence() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Launch packaged Tauri and capture Windows desktop evidence" in workflow
    assert "capture_windows_desktop_evidence.ps1" in workflow
    assert "windows_desktop_webview_evidence.mjs" in workflow
    assert "tauri-native-window.png:provenance" in workflow
    assert "tauri-webview-effective-200.png:provenance" in workflow
    assert "windows-icon-light-dark-contact-sheet.png:provenance" in workflow


def test_native_harness_installs_candidate_checks_shell_icons_and_exits_cleanly() -> (
    None
):
    source = (ROOT / "scripts" / "capture_windows_desktop_evidence.ps1").read_text(
        encoding="utf-8"
    )

    for contract in (
        "Programs\\Stock Desk",
        "stock-desk-desktop.exe",
        "stock-desk-sidecar",
        "Stock Desk.lnk",
        "GetDpiForWindow",
        "Save-WindowScreenshot",
        "for ($attempt = 1; $attempt -le 12; $attempt++)",
        "blank or visually unusable after bounded retries",
        "PrivateExtractIcons",
        "16 -32512",
        "32 -32512",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "shortcuts_share_host_identity = $true",
        "packaged_entries_match_reviewed_identity = $true",
        "graceful_exit = $true",
    ):
        assert contract in source
    assert "hosted_runner_limitations" in source
    assert "not an OS DPI change" in source
    assert source.count("'16' = [ordered]@{") == 2
    assert source.count("'32' = [ordered]@{") == 2
    first_run_cleanup = source.index(
        "Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA 'Stock Desk\\v1.1')"
    )
    launch = source.index(
        "$desktopProcess = Start-Process -FilePath $hostPath -PassThru"
    )
    assert first_run_cleanup < launch


def test_packaged_webview_matrix_is_explicitly_equivalent_not_real_os_dpi() -> None:
    source = (ROOT / "scripts" / "windows_desktop_webview_evidence.mjs").read_text(
        encoding="utf-8"
    )

    for percent, width, height in (
        (100, 1366, 768),
        (125, 1093, 614),
        (150, 911, 512),
        (175, 781, 439),
        (200, 683, 384),
    ):
        assert f"{{ percent: {percent}, width: {width}, height: {height} }}" in source
    assert "actual_tauri_webview: true" in source
    assert "tauri-webview-cdp-effective-viewport-not-os-dpi" in source
    assert "tauri-webview-cdp-system-media-not-windows-theme" in source
    for route in ("/market", "/formulas", "/backtests", "/analysis", "/tasks"):
        assert f'path: "{route}"' in source
    for preference in ("light", "dark", "system"):
        assert f'preference: "{preference}"' in source
    assert "core_route_theme_scale_matrix" in source
    assert "nonColorStatus" in source
    assert "controlOverlap" in source
    assert "criticalControlClipped" in source
    assert "focusVisible" in source
    assert "packaged exit dialog did not focus the safe cancel action" in source
    assert "horizontal overflow" in source
    assert "not Windows OS DPI" in source
    assert 'name: "先看只读演示"' in source
    assert 'name: "欢迎使用 stock-desk"' in source
    assert 'getByText("默认打开上证指数 000001.SS"' in source
    assert 'return "first-run-readonly-demo"' in source
    assert 'globalThis.history.pushState({}, "", pathName)' in source
    assert 'new PopStateEvent("popstate")' in source
    assert 'transition = "readonly-demo-router"' in source
    assert 'return internals.invoke("desktop_runtime_state")' in source
    assert 'latestState.state === "ready"' in source
    assert "packaged desktop entered recovery before onboarding" in source
    assert "packaged desktop did not become ready before onboarding" in source
    assert "await navigateToCoreRoute(page, coreRoutes[0])" in source
    assert "desktop_runtime: desktopRuntime" in source
    assert "workspace_entry_mode: workspaceEntryMode" in source
    assert "routeTransition" in source


def test_nsis_installer_and_uninstaller_use_the_reviewed_windows_icon() -> None:
    config = json.loads(
        (ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )

    nsis = config["bundle"]["windows"]["nsis"]
    assert nsis["installerIcon"] == "icons/icon.ico"
    assert nsis["uninstallerIcon"] == "icons/icon.ico"


def test_exit_deadlines_are_ordered_and_native_pid_is_authoritative() -> None:
    rust = (ROOT / "src-tauri" / "src" / "exit.rs").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts" / "capture_windows_desktop_evidence.ps1").read_text(
        encoding="utf-8"
    )
    webview = (ROOT / "scripts" / "windows_desktop_webview_evidence.mjs").read_text(
        encoding="utf-8"
    )

    host_match = re.search(
        r"SIDECAR_EXIT_TIMEOUT: Duration = Duration::from_secs\((\d+)\)", rust
    )
    evidence_match = re.search(
        r"\}\s+(\d+)\s+'packaged app did not complete the tested graceful exit'",
        powershell,
    )
    assert host_match is not None
    assert evidence_match is not None
    host_timeout = int(host_match.group(1))
    evidence_timeout = int(evidence_match.group(1))
    assert _SHUTDOWN_TIMEOUT_SECONDS + 5 <= host_timeout < evidence_timeout

    confirm_click = webview.index(
        'page.getByRole("button", { name: "退出应用", exact: true }).click()'
    )
    finally_block = webview.index("} finally", confirm_click)
    assert "STOCK_DESK_EXIT_ACTIVITY" in webview
    assert "STOCK_DESK_EXIT_OBSERVATION" in webview
    assert "unavailable_while_not_ready" in webview
    activity_probe = webview.index('path: "/api/desktop/activity"')
    runtime_probe = webview.rindex("desktop_runtime_state", 0, activity_probe)
    assert runtime_probe < activity_probe < confirm_click
    assert "page.waitForTimeout(3_000)" in webview[confirm_click:finally_block]
    assert not re.search(
        r"page\.waitForEvent\((?:'|\")close(?:'|\")",
        webview[confirm_click:finally_block],
    )
    assert "正在安全退出" not in webview[confirm_click:finally_block]
    assert "host_alive=" in powershell
    assert "sidecar_alive=" in powershell


def test_packaged_evidence_waits_longer_than_the_bounded_cold_start_budget() -> None:
    rust = (ROOT / "src-tauri" / "src" / "app.rs").read_text(encoding="utf-8")
    webview = (ROOT / "scripts" / "windows_desktop_webview_evidence.mjs").read_text(
        encoding="utf-8"
    )

    host_match = re.search(
        r"STARTUP_TIMEOUT: Duration = Duration::from_secs\((\d+)\)", rust
    )
    evidence_match = re.search(
        r"async function waitForDesktopReady.*?Date\.now\(\) \+ ([\d_]+);",
        webview,
        re.DOTALL,
    )
    assert host_match is not None
    assert evidence_match is not None
    host_seconds = int(host_match.group(1))
    evidence_seconds = int(evidence_match.group(1).replace("_", "")) / 1000
    assert host_seconds == 45
    assert host_seconds < evidence_seconds <= host_seconds + 30
