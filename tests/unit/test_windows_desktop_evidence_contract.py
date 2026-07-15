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
    assert "windows_packaged_backtest_evidence.mjs" in workflow
    assert "verify_packaged_backtest_evidence.py" in workflow
    assert "--bundle-manifest" in workflow
    assert "--comparison" in workflow
    assert "--output-promotion" in workflow
    assert "packaged-backtest-evidence.json:provenance" in workflow
    for promoted in (
        "packaged-backtest/windows-desktop-evidence.json:provenance",
        "packaged-backtest/tauri-webview-evidence.json:provenance",
        "packaged-backtest/packaged-backtest-evidence.json:provenance",
        "packaged-backtest/packaged-backtest-seed.json:provenance",
        "packaged-backtest/packaged-backtest-host-observation.json:provenance",
        "packaged-backtest/windows-packaged-backtest-promotion.json:provenance",
    ):
        assert promoted in workflow
    assert "schemas/windows-packaged-backtest-promotion-v1.schema.json" in workflow
    assert "tauri-native-window.png:provenance" in workflow
    assert "tauri-webview-effective-200.png:provenance" in workflow
    assert "windows-icon-light-dark-contact-sheet.png:provenance" in workflow
    assert "Verify candidate A desktop privacy boundary" in workflow
    assert "scripts/verify_zero_telemetry.py --root ." in workflow
    assert "$workflowHash = (Get-FileHash .github\\workflows\\ci.yml" in workflow
    assert (
        '".github/workflows/ci.yml=$workflowHash"' in workflow
        or "'.github/workflows/ci.yml=$workflowHash'" in workflow
    )
    assert "scripts/verify_zero_telemetry.py=$privacyVerifierHash" in workflow
    assert "config/desktop-network-privacy.json=$privacyPolicyHash" in workflow


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
        "WEBVIEW2_USER_DATA_FOLDER",
        "Get-AvailableLoopbackPort",
        "Get-NetTCPConnection",
        "OwningProcess",
        "--remote-debugging-port=$devToolsPort",
        "--remote-debugging-address=127.0.0.1",
        "packaged WebView2 CDP endpoint did not appear",
        "CDP endpoint does not match the isolated browser identity",
        "shortcuts_share_host_identity = $true",
        "packaged_entries_match_reviewed_identity = $true",
        "graceful_exit = $true",
    ):
        assert contract in source
    assert "hosted_runner_limitations" in source
    assert "not an OS DPI change" in source
    assert source.count("'16' = [ordered]@{") == 2
    assert source.count("'32' = [ordered]@{") == 2
    first_run_cleanup = source.index("Remove-EvidenceDirectory $packagedDataRoot")
    fixture_prepare = source.index("prepare_windows_packaged_backtest_evidence.py")
    launch = source.index(
        "$desktopProcess = [Diagnostics.Process]::Start($desktopStart)"
    )
    assert first_run_cleanup < fixture_prepare < launch
    isolation = source.index("$env:WEBVIEW2_USER_DATA_FOLDER = $webviewUserData")
    cdp_wait = source.index("$devToolsVersion = Wait-Until")
    listener_ownership = source.index("$devToolsListeners = @(")
    cdp_export = source.index("$env:STOCK_DESK_DESKTOP_CDP = $desktopCdp")
    assert isolation < launch < cdp_wait < listener_ownership < cdp_export
    assert source.count("Remove-Item -Recurse -Force $webviewUserData") == 2
    assert "Remove-Item Env:WEBVIEW2_USER_DATA_FOLDER" in source
    assert "--remote-allow-origins=*" not in source
    assert "http://127.0.0.1:9222" not in source
    assert "DevToolsActivePort" not in source
    assert "[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)" in source
    assert "$listener.Stop()" in source
    port_reservation = source.index("$devToolsPort = Get-AvailableLoopbackPort")
    browser_arguments = source.index(
        '$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=$devToolsPort'
    )
    assert "[int]$devToolsPort = 0" in source
    assert fixture_prepare < port_reservation < browser_arguments < launch
    assert "selected loopback DevTools port is invalid" in source
    assert "Test-RemoteDebuggingPortCommandLine" not in source
    assert "LocalAddress -eq '127.0.0.1'" in source
    assert "isolated WebView2 process does not own the selected CDP listener" in source
    process_cleanup = source.index("Stop-Process -Id $desktopProcess.Id")
    udf_cleanup = source.rindex("Remove-Item -Recurse -Force $webviewUserData")
    assert process_cleanup < udf_cleanup
    assert "for ($cleanupAttempt = 1; $cleanupAttempt -le 10" in source
    assert "HKCU:\\Software\\Policies\\Microsoft\\Edge\\WebView2" in source
    assert "refuses to replace an existing app policy" in source
    assert source.count("Remove-ItemProperty -LiteralPath") == 2
    assert "$desktopStart.UseShellExecute = $false" in source
    assert "webview-startup-summary.json" in source
    assert "$nodeStdout = $null" in source
    assert "$nodeStderr = $null" in source
    assert "foreach ($runtimeLog in @($nodeStdout, $nodeStderr))" in source
    assert "function Copy-DiagnosticRuntimeLog" in source
    assert "Copy-DiagnosticRuntimeLog -Path $runtimeLog" in source
    node_stop = source.index("Stop-Process -Id $nodeProcess.Id")
    runtime_log_copy = source.rindex("Copy-DiagnosticRuntimeLog -Path $runtimeLog")
    assert node_stop < runtime_log_copy
    assert "$nodeProcess.WaitForExit(5000)" in source
    assert "diagnostic runtime log copy failed after bounded retries" not in source
    assert "devtools_listener_owned_by_isolated_webview" in source
    assert "webview_process_scope = 'isolated-user-data-folder'" in source
    assert "CommandLine =" not in source
    assert "Get-IsolatedWebViewProcesses $webviewUserData" in source
    assert "Join-Path $UserDataFolder 'EBWebView'" in source
    assert "--user-data-dir=$candidateUserDataFolder" in source
    assert "[StringComparison]::OrdinalIgnoreCase" in source
    assert "[char]::IsWhiteSpace($CommandLine[$argumentEnd])" in source
    assert "StartsWith(" not in source
    assert "Get-EvidenceSidecarProcesses $baselineSidecarProcessIds" in source
    assert "Get-Process -Name 'stock-desk-sidecar'" in source
    assert "function Get-InstalledHostSidecarProcesses" in source
    assert 'Win32_Process -Filter "ParentProcessId=$HostProcessId"' in source
    assert "[string]$_.ExecutablePath" in source
    assert "[IO.Path]::GetFullPath([string]$_.ExecutablePath)" in source
    assert "[StringComparison]::OrdinalIgnoreCase" in source
    assert (
        source.count(
            "Get-InstalledHostSidecarProcesses $desktopProcess.Id $installedSidecarPath"
        )
        == 4
    )
    assert "$beforeSidecars[0].Id -ne $sidecar.Id" in source
    assert "$afterSidecars[0].Id -eq $sidecarBeforePid" in source
    assert "sidecar binary identity changed before restart" in source
    assert "old packaged sidecar OS process survived" in source
    assert "com.congbao.stockdesk" in source
    assert "$tauriDefaultWebViewDataExisted" in source
    diagnostics = source.index(
        "$webviewProcesses = @(Get-IsolatedWebViewProcesses $webviewUserData)"
    )
    process_cleanup = source.index("Stop-Process -Id $desktopProcess.Id")
    assert diagnostics < process_cleanup
    sidecar_cleanup = source.index("$evidenceSidecars = @(Get-EvidenceSidecarProcesses")
    isolated_webview_cleanup = source.index(
        "$isolatedWebViewProcesses = @(Get-IsolatedWebViewProcesses"
    )
    uninstall_cleanup = source.index(
        "if (Test-Path -LiteralPath $uninstallerPath -PathType Leaf)",
        process_cleanup,
    )
    assert process_cleanup < sidecar_cleanup < isolated_webview_cleanup
    assert isolated_webview_cleanup < uninstall_cleanup < udf_cleanup
    child_exit_wait = source.index(
        "packaged child processes did not complete the tested graceful exit"
    )
    host_exit_code = source.index("$desktopProcess.ExitCode -ne 0")
    retained_sidecar_handle = source.index(
        "$sidecarAfterHandle = [StockDeskEvidenceNative]::OpenProcess"
    )
    sidecar_exit_code = source.rindex("GetExitCodeProcess($sidecarAfterHandle")
    graceful_exit_proof = source.index("$gracefulExit = $true")
    assert (
        retained_sidecar_handle
        < host_exit_code
        < child_exit_wait
        < sidecar_exit_code
        < graceful_exit_proof
        < process_cleanup
    )
    assert (
        "} 30 'packaged child processes did not complete the tested graceful exit'"
        in source
    )
    assert "$desktopProcess.ExitCode -ne 0" in source
    assert "$sidecarAfterProcess = $afterSidecars[0]" in source
    assert "OpenProcess(0x1000, $false, [uint32]$sidecarAfterPid)" in source
    assert "restarted sidecar process handle is unavailable" in source
    assert "$observedSidecarExitCode -ne 259" in source
    assert "GetExitCodeProcess($sidecarAfterHandle" in source
    assert "$sidecarAfterExitCode -eq 259" in source
    assert "$sidecarAfterExitCode -ne 0" in source
    assert "sidecar exit code is unavailable" in source
    assert "CloseHandle($sidecarAfterHandle)" in source
    assert "bool GetExitCodeProcess(IntPtr process, out uint exitCode)" in source
    assert "IntPtr OpenProcess(uint processAccess, bool inheritHandle" in source
    assert "bool CloseHandle(IntPtr handle)" in source
    final_exit_read = source.rindex("GetExitCodeProcess($sidecarAfterHandle")
    unavailable_exit = source.index("sidecar exit code is unavailable", final_exit_read)
    active_exit = source.index("$sidecarAfterExitCode -eq 259", unavailable_exit)
    nonzero_exit = source.index("$sidecarAfterExitCode -ne 0", active_exit)
    assert final_exit_read < unavailable_exit < active_exit < nonzero_exit
    close_handle = source.index("CloseHandle($sidecarAfterHandle)", nonzero_exit)
    assert nonzero_exit < graceful_exit_proof < close_handle
    assert "Get-IsolatedWebViewProcesses $webviewUserData -Strict" in source
    assert "$queryErrorAction = if ($Strict) { 'Stop' }" in source
    assert "sidecar_ids=$($remainingSidecarIds -join ',')" in source
    assert "webview_ids=$($remainingWebViewIds -join ',')" in source
    assert "packaged processes unexpectedly remained after graceful exit" in source
    assert (
        "Tauri default WebView2 state created by evidence could not be cleaned"
        in source
    )
    assert "$webviewPolicyRootCreated" in source
    assert "$webviewEdgePolicyCreated" in source


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
    for route in (
        "/market",
        "/formulas",
        "/backtests",
        "/analysis",
        "/tasks",
        "/settings",
    ):
        assert f'path: "{route}"' in source
    guidance_helper = source[
        source.index("async function dismissAutomaticGuidance") : source.index(
            "async function focusEvidence"
        )
    ]
    assert '.waitFor({ state: "visible", timeout: 15_000 })' in source
    assert 'dialog.getByRole("button", { name: "跳过引导" })' in source
    assert "await skip.click()" in source
    assert 'dialog.waitFor({ state: "hidden", timeout: 15_000 })' in source
    assert ".catch(() => false)" not in guidance_helper
    assert 'return "fresh-page-dismissed"' in guidance_helper
    assert "guidanceExpected: false" in source
    assert 'return "not-applicable-no-tour"' in guidance_helper
    assert "automaticGuidanceDisposition" in source
    assert (
        len(
            re.findall(
                r"await dismissAutomaticGuidance\(\s*page,\s*route,\s*\)", source
            )
        )
        == 1
    )
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
    assert "packaged backtest evidence refuses read-only demo mode" in source
    assert 'return "hash-bound-public-fixture-real-mode"' in source
    assert "throw new Error(`packaged navigation link is missing:" in source
    assert "history.pushState" not in source
    assert "readonly-demo-router" not in source
    assert 'return internals.invoke("desktop_runtime_state")' in source
    assert 'latestState.state === "ready"' in source
    assert "packaged desktop entered recovery before onboarding" in source
    assert "packaged desktop did not become ready before onboarding" in source
    assert "await navigateToCoreRoute(page, coreRoutes[0])" in source
    restore_helper = source[
        source.index(
            "async function reloadWorkspaceAfterPackagedBacktests"
        ) : source.index("const browser = await connect()")
    ]
    assert 'page.reload({ waitUntil: "domcontentloaded" })' in restore_helper
    assert "await waitForDesktopReady(page)" in restore_helper
    assert "await ensureWorkspaceReady(page)" in restore_helper
    assert (
        source.index("await runPackagedBacktestEvidence(page, outputDir)")
        < source.index("await reloadWorkspaceAfterPackagedBacktests(page)")
        < source.index("await navigateToCoreRoute(page, coreRoutes[0])")
    )
    assert "post_backtest_workspace_restore: postBacktestWorkspaceRestore" in source
    assert "desktop_runtime: desktopRuntime" in source
    assert "workspace_entry_mode: workspaceEntryMode" in source
    assert "routeTransition" in source


def test_packaged_backtest_matrix_uses_webview_host_ipc_and_new_worker_resume() -> None:
    source = (ROOT / "scripts" / "windows_packaged_backtest_evidence.mjs").read_text(
        encoding="utf-8"
    )

    assert 'internals.invoke("desktop_api_request"' in source
    assert 'globalThis.__TAURI_INTERNALS__.invoke("desktop_runtime_state"' in source
    assert 'globalThis.__TAURI_INTERNALS__.invoke("desktop_restart_service")' in source
    assert '"/api/desktop/shutdown"' in source
    assert '"/api/desktop/shutdown/commit"' in source
    assert '"/api/desktop/recovery/resume"' in source
    assert '"/api/tasks/worker-status"' in source
    assert "packaged Worker disappeared while task remained queued" in source
    assert "waitForRuntimeRecovery" in source
    assert 'captureHandshake("restart-before"' in source
    assert 'captureHandshake("restart-after"' in source
    assert 'for (const formulaId of ["macd", "custom"])' in source
    assert 'for (const scope of ["single", "pool"])' in source
    assert 'for (const period of ["1d", "1w", "60m"])' in source
    assert 'submission_surface: "installed-tauri-webview-host-ipc"' in source
    assert "read_only_demo: false" in source
    assert "running.worker_id === finished.worker_id" in source
    powershell = (ROOT / "scripts" / "capture_windows_desktop_evidence.ps1").read_text(
        encoding="utf-8"
    )
    assert "sidecarBeforePid" in powershell
    assert "sidecarAfterPid" in powershell
    assert "old packaged sidecar OS process survived" in powershell
    assert "packaged-backtest-host-observation.json" in powershell
    assert "host_observation_sha256" in powershell
    runtime_ready = source.index(
        "const runtimeBefore = await waitForRuntimeReady(page)"
    )
    backlog = source.index("const submissions = []", runtime_ready)
    submission = source.index(
        'await invoke(page, "POST", "/api/backtests", request)', backlog
    )
    shutdown = source.index("await requestCheckpoint(page, taskIds)", backlog)
    paused = source.index("const pausedTasks", shutdown)
    running = source.index("const running = pausedTasks[0]", paused)
    assert runtime_ready < backlog < submission < shutdown < paused < running
    assert "for (let index = 0; index < 64; index += 1)" in source[backlog:shutdown]
    assert "Promise.all" not in source[backlog:shutdown]
    assert "100-row bound" in source[backlog:shutdown]
    assert "taskIds.size !== submissions.length" in source[backlog:shutdown]
    backlog_helper = source.index("async function waitForCheckpointBacklog")
    backlog_helper_end = source.index("async function waitForCheckpointBacklogSuccess")
    assert 'item.status === "running"' in source[backlog_helper:backlog_helper_end]
    assert 'item.status === "queued"' in source[backlog_helper:backlog_helper_end]
    assert (
        "running.length === 1 && queued.length >= 8"
        in source[backlog_helper:backlog_helper_end]
    )
    checkpoint_helper = source.index("async function requestCheckpoint")
    checkpoint_helper_end = source.index("async function pageAll")
    checkpoint_retry = source[checkpoint_helper:checkpoint_helper_end]
    assert "maxAttempts = 24" in checkpoint_retry
    assert "await waitForCheckpointBacklog(page, taskIds)" in checkpoint_retry
    assert '"/api/desktop/shutdown"' in checkpoint_retry
    assert "require_running_checkpoint: true" in checkpoint_retry
    assert 'payload?.code !== "desktop_checkpoint_timeout"' in checkpoint_retry
    assert 'payload?.code !== "desktop_checkpoint_not_active"' in checkpoint_retry
    assert "payload?.retryable !== true" in checkpoint_retry
    assert "error?.status !== 409" in checkpoint_retry
    assert "selected.length !== taskIds.size" in checkpoint_retry
    assert "queued.length < 8" in checkpoint_retry
    assert "running.length > 1" in checkpoint_retry
    assert "rejected.length > 0" in checkpoint_retry
    assert "STOCK_DESK_CHECKPOINT_RETRY" in checkpoint_retry
    assert "exhausted bounded retryable timeouts" in checkpoint_retry
    assert "checkpoint.running !== 1" in source[shutdown:paused]
    assert 'item.status === "running"' in source[paused:running]
    assert "pausedTasks.length !== 1" in source[paused:running]
    assert "waitForRuntimeReady(page)" not in checkpoint_retry
    resumed = source.index("const finished = await waitForTask", running)
    drained = source.index("await waitForCheckpointBacklogSuccess", resumed)
    report = source.index('await invoke(\n    page,\n    "GET",', drained)
    assert running < resumed < drained < report


def test_packaged_backtest_marker_protocol_is_atomic_and_retries_partial_or_stale_data() -> (
    None
):
    webview = (ROOT / "scripts" / "windows_packaged_backtest_evidence.mjs").read_text(
        encoding="utf-8"
    )
    powershell = (ROOT / "scripts" / "capture_windows_desktop_evidence.ps1").read_text(
        encoding="utf-8"
    )

    marker_write = webview.index("await writeFile(")
    marker_rename = webview.index("await rename(temporaryMarker, marker)")
    assert marker_write < marker_rename
    assert "path.join(syncDir, `.${name}.${nonce}.tmp`)" in webview
    assert "while (Date.now() < deadline)" in webview
    assert '(await readFile(acknowledgment, "utf8")).trim() === nonce' in webview
    assert "setTimeout(resolve, 100)" in webview
    for error_code in ("ENOENT", "EACCES", "EPERM", "EBUSY"):
        assert f'"{error_code}"' in webview
    assert "RETRYABLE_FILE_ERROR_CODES.has(error?.code)" in webview

    assert "function Wait-CaptureMarker" in powershell
    assert "ConvertFrom-Json -ErrorAction Stop" in powershell
    assert "if ($candidate.capture_nonce -eq $Nonce)" in powershell
    assert "return $false" in powershell
    ack_write = powershell.index("$Nonce | Set-Content -LiteralPath $temporary")
    ack_rename = powershell.index(
        "Move-Item -LiteralPath $temporary -Destination $Path -Force"
    )
    assert ack_write < ack_rename
    assert "for ($attempt = 1; $attempt -le 10; $attempt++)" in powershell
    assert (
        "capture acknowledgment could not be published after bounded retries"
        in powershell
    )
    assert "Remove-Item -LiteralPath $temporary" in powershell
    assert "transiently deny a read. Retry until the bounded deadline" in powershell


def test_packaged_evidence_cleanup_retries_and_rejects_residual_state() -> None:
    powershell = (ROOT / "scripts" / "capture_windows_desktop_evidence.ps1").read_text(
        encoding="utf-8"
    )

    assert "function Remove-EvidenceDirectory" in powershell
    assert "Remove-Item -LiteralPath $Path -Recurse -Force" in powershell
    assert "stale packaged backtest data state could not be cleaned" in powershell
    assert (
        "stale packaged backtest synchronization state could not be cleaned"
        in powershell
    )
    assert (
        "$packagedDataCleaned = Remove-EvidenceDirectory $packagedDataRoot"
        in powershell
    )
    assert (
        "$restartSyncCleaned = Remove-EvidenceDirectory $restartSyncRoot" in powershell
    )
    assert "packaged backtest data state could not be cleaned" in powershell
    assert "packaged backtest synchronization state could not be cleaned" in powershell


def test_fixture_marker_producer_and_consumer_use_the_same_deadlock_free_order() -> (
    None
):
    webview = (ROOT / "scripts" / "windows_packaged_backtest_evidence.mjs").read_text(
        encoding="utf-8"
    )
    powershell = (ROOT / "scripts" / "capture_windows_desktop_evidence.ps1").read_text(
        encoding="utf-8"
    )
    before_restart = (
        "a_share_constraints_60m",
        "open_position_costs_1d",
        "partial_pool_gap_1d",
        "matrix_1d",
        "checkpoint-matrix-1d",
    )
    after_restart = (
        "matrix_1w",
        "matrix_60m",
    )
    before_positions = [
        powershell.index(
            "@('checkpoint-matrix-1d','matrix_1d')"
            if fixture_id == "checkpoint-matrix-1d"
            else f"@('{fixture_id}','{fixture_id}')"
        )
        for fixture_id in before_restart
    ]
    after_positions = [
        powershell.index(f"@('{fixture_id}','{fixture_id}')")
        for fixture_id in after_restart
    ]
    restart_ack = powershell.index(
        "Write-CaptureAck (Join-Path $restartSyncRoot 'restart-after.ack')"
    )
    restart_before_wait = powershell.index(
        "$beforeMarkerPath = Join-Path $restartSyncRoot 'restart-before.json'"
    )
    assert before_positions == sorted(before_positions)
    assert max(before_positions) < restart_before_wait < restart_ack
    assert restart_ack < min(after_positions)
    special_start = webview.index("const specialCases = []")
    cells_start = webview.index("const cells = []")
    checkpoint_start = webview.index(
        'await selectFixture("matrix_1d", "checkpoint-matrix-1d")'
    )
    period_loop = webview.index('for (const period of ["1d", "1w", "60m"])')
    checkpoint_branch = webview.index('if (period === "1d")', period_loop)
    checkpoint_guard = webview.index("if (checkpoint === undefined)")
    assert special_start < cells_start < checkpoint_start
    assert period_loop < checkpoint_branch < checkpoint_start < checkpoint_guard
    assert (
        webview.index(
            "checkpoint = await checkpointEvidence(page, seed, baseline);",
            checkpoint_start,
        )
        < checkpoint_guard
    )
    special_positions = [
        webview.index(f'"{fixture_id}"', special_start, cells_start)
        for fixture_id in before_restart[:3]
    ]
    assert special_positions == sorted(special_positions)
    assert 'for (const period of ["1d", "1w", "60m"])' in webview[cells_start:]
    assert 'await selectFixture("matrix_1d", "checkpoint-matrix-1d")' in webview
    assert "@('checkpoint-matrix-1d','matrix_1d')" in powershell
    assert powershell.index("@('checkpoint-matrix-1d','matrix_1d')") < powershell.index(
        "@('matrix_1w','matrix_1w')"
    )


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
