param(
  [Parameter(Mandatory = $true)][string]$Installer,
  [Parameter(Mandatory = $true)][string]$Output,
  [Parameter(Mandatory = $true)][string]$SourceSha,
  [Parameter(Mandatory = $true)][string]$SourceTree
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

if ($env:OS -ne 'Windows_NT') { throw 'packaged desktop evidence requires Windows' }
if ($SourceSha -notmatch '^[0-9a-f]{40}$' -or $SourceTree -notmatch '^[0-9a-f]{40}$') {
  throw 'desktop evidence requires exact lowercase source SHA and tree ids'
}
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$candidateSha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
$Output = [IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Force $Output | Out-Null

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class StockDeskEvidenceNative {
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")]
  public static extern uint GetDpiForWindow(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int command);
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern IntPtr OpenProcess(uint processAccess, bool inheritHandle, uint processId);
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool CloseHandle(IntPtr handle);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)]
  public static extern uint PrivateExtractIcons(string file, int index, int width, int height, IntPtr[] icons, uint[] ids, uint count, uint flags);
  [DllImport("user32.dll")]
  public static extern bool DestroyIcon(IntPtr icon);
}
'@

function Wait-Until([scriptblock]$Condition, [int]$Seconds, [string]$Failure) {
  $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
  do {
    $value = & $Condition
    if ($null -ne $value -and $value -ne $false) { return $value }
    Start-Sleep -Milliseconds 500
  } while ([DateTime]::UtcNow -lt $deadline)
  throw $Failure
}

function Get-AvailableLoopbackPort() {
  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
  try {
    $listener.Start()
    $endpoint = [Net.IPEndPoint]$listener.LocalEndpoint
    [int]$selectedPort = $endpoint.Port
    if ($selectedPort -lt 1 -or $selectedPort -gt 65535) {
      throw 'selected loopback DevTools port is invalid'
    }
    return $selectedPort
  } finally {
    $listener.Stop()
  }
}

function Wait-CaptureMarker([string]$Path, [Diagnostics.Process]$NodeProcess, [string]$Nonce, [int]$Seconds, [string]$Failure) {
  return Wait-Until {
    $NodeProcess.Refresh()
    if ($NodeProcess.HasExited) {
      throw "packaged WebView evidence exited before capture marker: $(Get-Content -LiteralPath $nodeStderr -Raw -ErrorAction SilentlyContinue)"
    }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
      try {
        $candidate = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($candidate.capture_nonce -eq $Nonce) { return $candidate }
      } catch {
        # A writer may only publish with atomic rename, but antivirus/indexing can
        # still transiently deny a read. Retry until the bounded deadline.
      }
    }
    return $false
  } $Seconds $Failure
}

function Write-CaptureAck([string]$Path, [string]$Nonce) {
  $temporary = Join-Path (Split-Path $Path -Parent) ".$(Split-Path $Path -Leaf).$Nonce.tmp"
  for ($attempt = 1; $attempt -le 10; $attempt++) {
    try {
      $Nonce | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
      Move-Item -LiteralPath $temporary -Destination $Path -Force
      return
    } catch {
      Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
      if ($attempt -eq 10) { throw "capture acknowledgment could not be published after bounded retries: $Path" }
      Start-Sleep -Milliseconds 100
    }
  }
}

function Copy-DiagnosticRuntimeLog([string]$Path, [string]$Destination) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
      if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
      Copy-Item -LiteralPath $Path -Destination $Destination -Force -ErrorAction Stop
      return $true
    } catch {
      Start-Sleep -Milliseconds 100
    }
  }
  return $false
}

function Remove-EvidenceDirectory([string]$Path) {
  for ($attempt = 1; $attempt -le 10; $attempt++) {
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    Start-Sleep -Milliseconds 250
  }
  return -not (Test-Path -LiteralPath $Path)
}

function Test-IsolatedWebViewCommandLine([string]$CommandLine, [string]$UserDataFolder) {
  if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
  # WebView2 appends EBWebView to an explicitly configured UDF. Accept only
  # the configured root or that exact runtime-owned suffix; sibling and prefix
  # paths must remain outside this evidence process scope.
  foreach ($candidateUserDataFolder in @(
    $UserDataFolder,
    (Join-Path $UserDataFolder 'EBWebView')
  )) {
    foreach ($argument in @(
      "--user-data-dir=$candidateUserDataFolder",
      "--user-data-dir=`"$candidateUserDataFolder`""
    )) {
      $offset = $CommandLine.IndexOf($argument, [StringComparison]::OrdinalIgnoreCase)
      while ($offset -ge 0) {
        $argumentEnd = $offset + $argument.Length
        if ($argumentEnd -eq $CommandLine.Length -or [char]::IsWhiteSpace($CommandLine[$argumentEnd])) {
          return $true
        }
        $offset = $CommandLine.IndexOf(
          $argument,
          $argumentEnd,
          [StringComparison]::OrdinalIgnoreCase
        )
      }
    }
  }
  return $false
}

function Get-IsolatedWebViewProcesses([string]$UserDataFolder, [switch]$Strict) {
  $queryErrorAction = if ($Strict) { 'Stop' } else { 'SilentlyContinue' }
  return @(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" -ErrorAction $queryErrorAction |
    Where-Object { Test-IsolatedWebViewCommandLine ([string]$_.CommandLine) $UserDataFolder })
}

function Test-ProcessExitedStrict([int]$ProcessId) {
  try {
    $candidate = [Diagnostics.Process]::GetProcessById($ProcessId)
    try {
      $candidate.Refresh()
      return $candidate.HasExited
    } finally {
      $candidate.Dispose()
    }
  } catch [ArgumentException] {
    return $true
  }
}

function Get-EvidenceSidecarProcesses([int[]]$BaselineProcessIds = @()) {
  return @(Get-Process -Name 'stock-desk-sidecar' -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -notin $BaselineProcessIds })
}

function Get-InstalledHostSidecarProcesses([int]$HostProcessId, [string]$InstalledSidecarPath) {
  $expectedPath = [IO.Path]::GetFullPath($InstalledSidecarPath)
  $hostChildren = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$HostProcessId" -ErrorAction SilentlyContinue |
    Where-Object {
      -not [string]::IsNullOrWhiteSpace([string]$_.ExecutablePath) -and
      [string]::Equals(
        [IO.Path]::GetFullPath([string]$_.ExecutablePath),
        $expectedPath,
        [StringComparison]::OrdinalIgnoreCase
      )
    })
  return @($hostChildren | ForEach-Object {
    Get-Process -Id ([int]$_.ProcessId) -ErrorAction SilentlyContinue
  })
}

function Save-EmbeddedIcon([string]$Source, [string]$Destination, [int]$Size, [int]$Index) {
  $handles = [IntPtr[]]::new(1)
  $ids = [uint32[]]::new(1)
  $count = [StockDeskEvidenceNative]::PrivateExtractIcons($Source, $Index, $Size, $Size, $handles, $ids, 1, 0)
  if ($count -ne 1 -or $handles[0] -eq [IntPtr]::Zero) {
    throw "Windows could not extract the ${Size}px icon from $([IO.Path]::GetFileName($Source))"
  }
  $icon = [Drawing.Icon]::FromHandle($handles[0])
  try {
    $bitmap = $icon.ToBitmap()
    try { $bitmap.Save($Destination, [Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
  } finally {
    $icon.Dispose()
    [StockDeskEvidenceNative]::DestroyIcon($handles[0]) | Out-Null
  }
  return (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Resolve-Shortcut([string]$Path) {
  $shell = New-Object -ComObject WScript.Shell
  try { return $shell.CreateShortcut($Path).TargetPath }
  finally { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null }
}

function Save-WindowScreenshot([Diagnostics.Process]$Process, [string]$Destination) {
  $Process.Refresh()
  $handle = $Process.MainWindowHandle
  if ($handle -eq [IntPtr]::Zero) { throw 'packaged Tauri process has no native main window' }
  [StockDeskEvidenceNative]::ShowWindow($handle, 3) | Out-Null
  [StockDeskEvidenceNative]::SetForegroundWindow($handle) | Out-Null
  $rect = New-Object StockDeskEvidenceNative+RECT
  if (-not [StockDeskEvidenceNative]::GetWindowRect($handle, [ref]$rect)) {
    throw 'Windows could not read the packaged Tauri window bounds'
  }
  $width = $rect.Right - $rect.Left
  $height = $rect.Bottom - $rect.Top
  if ($width -lt 320 -or $height -lt 240) { throw 'packaged Tauri window bounds are unusable' }
  $sampledColors = $null
  $captured = $false
  for ($attempt = 1; $attempt -le 12; $attempt++) {
    Start-Sleep -Milliseconds 750
    $bitmap = New-Object Drawing.Bitmap $width, $height
    try {
      $graphics = [Drawing.Graphics]::FromImage($bitmap)
      try {
        $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
      } finally { $graphics.Dispose() }
      $sampledColors = [Collections.Generic.HashSet[int]]::new()
      for ($x = 0; $x -lt $width; $x += [Math]::Max(1, [int]($width / 80))) {
        for ($y = 0; $y -lt $height; $y += [Math]::Max(1, [int]($height / 50))) {
          $sampledColors.Add($bitmap.GetPixel($x, $y).ToArgb()) | Out-Null
        }
      }
      if ($sampledColors.Count -ge 16) {
        $bitmap.Save($Destination, [Drawing.Imaging.ImageFormat]::Png)
        $captured = $true
        break
      }
    } finally { $bitmap.Dispose() }
  }
  if (-not $captured) { throw 'native Tauri screenshot is blank or visually unusable after bounded retries' }
  $dpi = [StockDeskEvidenceNative]::GetDpiForWindow($handle)
  return [ordered]@{
    dpi = [int]$dpi
    scale_percent = [math]::Round(($dpi / 96.0) * 100)
    window = [ordered]@{ x=$rect.Left; y=$rect.Top; width=$width; height=$height }
    screenshot = [ordered]@{
      file = [IO.Path]::GetFileName($Destination)
      sha256 = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
      sampled_color_count = $sampledColors.Count
    }
  }
}

$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\Stock Desk'
$hostPath = Join-Path $installRoot 'stock-desk-desktop.exe'
$uninstallerPath = Join-Path $installRoot 'uninstall.exe'
$evidenceRoot = Split-Path (Split-Path $Output -Parent) -Parent
$webviewUserData = Join-Path $evidenceRoot "webview2-user-data-$SourceSha"
$restartSyncRoot = Join-Path $evidenceRoot "packaged-backtest-sync-$SourceSha"
$captureNonce = [Guid]::NewGuid().ToString('D')
$webviewEdgePolicy = 'HKCU:\Software\Policies\Microsoft\Edge'
$webviewPolicyRoot = 'HKCU:\Software\Policies\Microsoft\Edge\WebView2'
$webviewArgsPolicy = Join-Path $webviewPolicyRoot 'AdditionalBrowserArguments'
$webviewDataPolicy = Join-Path $webviewPolicyRoot 'UserDataFolder'
$webviewAppName = [IO.Path]::GetFileName($hostPath)
$webviewEdgePolicyCreated = -not (Test-Path -LiteralPath $webviewEdgePolicy)
$webviewPolicyRootCreated = -not (Test-Path -LiteralPath $webviewPolicyRoot)
$webviewArgsPolicyCreated = -not (Test-Path -LiteralPath $webviewArgsPolicy)
$webviewDataPolicyCreated = -not (Test-Path -LiteralPath $webviewDataPolicy)
$webviewArgsPolicySet = $false
$webviewDataPolicySet = $false
$tauriDefaultWebViewData = Join-Path $env:LOCALAPPDATA 'com.congbao.stockdesk'
$tauriDefaultWebViewDataExisted = Test-Path -LiteralPath $tauriDefaultWebViewData
$packagedDataRoot = Join-Path $env:LOCALAPPDATA 'Stock Desk\v1.1'
$baselineSidecarProcessIds = @(
  Get-Process -Name 'stock-desk-sidecar' -ErrorAction SilentlyContinue |
    ForEach-Object { $_.Id }
)
$desktopProcess = $null
$nodeProcess = $null
$nodeStdout = $null
$nodeStderr = $null
$diagnosticsRoot = $null
$sidecarAfterHandle = [IntPtr]::Zero
$gracefulExit = $false
[int]$devToolsPort = 0
try {
  if (Test-Path -LiteralPath $uninstallerPath -PathType Leaf) {
    $oldUninstall = Start-Process -FilePath $uninstallerPath -ArgumentList '/S' -Wait -PassThru
    if ($oldUninstall.ExitCode -ne 0) { throw 'existing Stock Desk test install could not be removed' }
  }
  $install = Start-Process -FilePath $Installer -ArgumentList '/S' -Wait -PassThru
  if ($install.ExitCode -ne 0) { throw "silent current-user installer failed with $($install.ExitCode)" }
  if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {
    throw 'silent installer did not create the packaged desktop host'
  }

  $desktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Stock Desk.lnk'
  $startMenuRoot = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
  $startMenuShortcut = @(Get-ChildItem $startMenuRoot -Recurse -File -Filter 'Stock Desk.lnk') | Select-Object -First 1
  if (-not (Test-Path -LiteralPath $desktopShortcut -PathType Leaf) -or $null -eq $startMenuShortcut) {
    throw 'silent installer did not create both desktop and Start menu shortcuts'
  }
  $desktopTarget = [IO.Path]::GetFullPath((Resolve-Shortcut $desktopShortcut))
  $startMenuTarget = [IO.Path]::GetFullPath((Resolve-Shortcut $startMenuShortcut.FullName))
  if ($desktopTarget -ne $hostPath -or $startMenuTarget -ne $hostPath) {
    throw 'Windows shell shortcuts do not share the packaged desktop host identity'
  }

  $hostIcon16 = Join-Path $Output 'packaged-host-icon-16.png'
  $installerIcon16 = Join-Path $Output 'packaged-installer-icon-16.png'
  $hostIcon = Join-Path $Output 'packaged-host-icon.png'
  $installerIcon = Join-Path $Output 'packaged-installer-icon.png'
  # tauri-build embeds the reviewed window icon at RT_GROUP_ICON resource 32512.
  $hostIcon16Hash = Save-EmbeddedIcon $hostPath $hostIcon16 16 -32512
  $installerIcon16Hash = Save-EmbeddedIcon $Installer $installerIcon16 16 0
  $hostIconHash = Save-EmbeddedIcon $hostPath $hostIcon 32 -32512
  $installerIconHash = Save-EmbeddedIcon $Installer $installerIcon 32 0
  $iconMetrics = Join-Path $Output 'packaged-icon-evidence.json'
  $python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
  if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'locked Python environment is missing' }
  & $python (Join-Path $PSScriptRoot 'verify_windows_packaged_icons.py') `
    --canonical (Join-Path $PSScriptRoot '..\src-tauri\icons') `
    --host "16=$hostIcon16" --host "32=$hostIcon" `
    --installer "16=$installerIcon16" --installer "32=$installerIcon" `
    --output $iconMetrics
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $iconMetrics -PathType Leaf)) {
    throw 'packaged Windows entries do not match the reviewed icon identity'
  }
  $iconEvidence = [ordered]@{
    host = [ordered]@{
      '16' = [ordered]@{ file='packaged-host-icon-16.png'; sha256=$hostIcon16Hash }
      '32' = [ordered]@{ file='packaged-host-icon.png'; sha256=$hostIconHash }
    }
    installer = [ordered]@{
      '16' = [ordered]@{ file='packaged-installer-icon-16.png'; sha256=$installerIcon16Hash }
      '32' = [ordered]@{ file='packaged-installer-icon.png'; sha256=$installerIconHash }
    }
    reviewed_identity_metrics = [ordered]@{
      file = 'packaged-icon-evidence.json'
      sha256 = (Get-FileHash -LiteralPath $iconMetrics -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    desktop_shortcut_target = 'install-root/stock-desk-desktop.exe'
    start_menu_shortcut_target = 'install-root/stock-desk-desktop.exe'
    shortcuts_share_host_identity = $true
    packaged_entries_match_reviewed_identity = $true
  }

  # Keep CDP outside the shipped configuration and isolate this evidence run
  # from any reused WebView2 browser process.
  Remove-Item -Recurse -Force $webviewUserData -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $webviewUserData | Out-Null
  $env:WEBVIEW2_USER_DATA_FOLDER = $webviewUserData
  New-Item -Path $webviewEdgePolicy -Force | Out-Null
  New-Item -Path $webviewPolicyRoot -Force | Out-Null
  foreach ($policy in @($webviewArgsPolicy, $webviewDataPolicy)) {
    if ($null -ne (Get-ItemProperty -LiteralPath $policy -Name $webviewAppName -ErrorAction SilentlyContinue)) {
      throw 'packaged WebView2 evidence refuses to replace an existing app policy'
    }
    New-Item -Path $policy -Force | Out-Null
  }
  # This candidate proof owns an isolated public fixture. Removing stale state
  # prevents a reused Windows host from changing the packaged backtest matrix.
  if (-not (Remove-EvidenceDirectory $packagedDataRoot)) {
    throw 'stale packaged backtest data state could not be cleaned'
  }
  & $python (Join-Path $PSScriptRoot 'prepare_windows_packaged_backtest_evidence.py') `
    --destination $packagedDataRoot --source-sha $SourceSha --source-tree $SourceTree
  if ($LASTEXITCODE -ne 0) { throw 'packaged backtest evidence fixture preparation failed' }
  $packagedBacktestSeed = Join-Path $packagedDataRoot 'packaged-backtest-seed.json'
  if (-not (Test-Path -LiteralPath $packagedBacktestSeed -PathType Leaf)) {
    throw 'packaged backtest evidence seed manifest is missing'
  }
  # Select the loopback port immediately before launch so evidence does not
  # depend on a runtime-generated browser discovery file and minimizes the
  # interval between releasing the selection socket and WebView2 binding it.
  $devToolsPort = Get-AvailableLoopbackPort
  $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=$devToolsPort --remote-debugging-address=127.0.0.1"
  New-ItemProperty -LiteralPath $webviewArgsPolicy -Name $webviewAppName -PropertyType String -Value $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS | Out-Null
  $webviewArgsPolicySet = $true
  New-ItemProperty -LiteralPath $webviewDataPolicy -Name $webviewAppName -PropertyType String -Value $webviewUserData | Out-Null
  $webviewDataPolicySet = $true
  $desktopStart = [Diagnostics.ProcessStartInfo]::new()
  $desktopStart.FileName = $hostPath
  $desktopStart.WorkingDirectory = $installRoot
  $desktopStart.UseShellExecute = $false
  $desktopStart.Environment['WEBVIEW2_USER_DATA_FOLDER'] = $webviewUserData
  $desktopStart.Environment['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
  $desktopProcess = [Diagnostics.Process]::Start($desktopStart)
  if ($null -eq $desktopProcess) { throw 'packaged Tauri host could not be started' }
  Wait-Until { $desktopProcess.Refresh(); if ($desktopProcess.HasExited) { throw 'packaged Tauri host exited during startup' }; if ($desktopProcess.MainWindowHandle -ne [IntPtr]::Zero) { $desktopProcess.MainWindowHandle } } 90 'packaged Tauri main window did not appear' | Out-Null
  $desktopCdp = "http://127.0.0.1:$devToolsPort"
  $devToolsVersion = Wait-Until { try { Invoke-RestMethod -Uri "$desktopCdp/json/version" -TimeoutSec 2 } catch { $false } } 90 'packaged WebView2 CDP endpoint did not appear'
  try { $devToolsWebSocket = [Uri]$devToolsVersion.webSocketDebuggerUrl }
  catch { throw 'packaged WebView2 published an invalid CDP browser endpoint' }
  if ($devToolsWebSocket.Scheme -ne 'ws' -or $devToolsWebSocket.Host -ne '127.0.0.1' -or $devToolsWebSocket.Port -ne $devToolsPort -or $devToolsWebSocket.AbsolutePath -notmatch '^/devtools/browser/[A-Za-z0-9-]+$') {
    throw 'packaged WebView2 CDP endpoint does not match the isolated browser identity'
  }
  $isolatedWebViews = @(
    Wait-Until {
      $processes = @(Get-IsolatedWebViewProcesses $webviewUserData)
      if ($processes.Count -gt 0) { return $processes }
      return $false
    } 90 'isolated packaged WebView2 process is missing'
  )
  $isolatedWebViewProcessIds = @($isolatedWebViews | ForEach-Object { [int]$_.ProcessId })
  $devToolsListeners = @(
    Wait-Until {
      $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $devToolsPort -ErrorAction SilentlyContinue |
          Where-Object {
            $_.LocalAddress -eq '127.0.0.1' -and
            $isolatedWebViewProcessIds -contains [int]($_.OwningProcess)
          }
      )
      if ($listeners.Count -eq 1) { return $listeners[0] }
      return $false
    } 30 'isolated WebView2 process does not own the selected CDP listener'
  )
  if ($devToolsListeners.Count -ne 1) {
    throw 'isolated WebView2 process does not own exactly one selected CDP listener'
  }
  $installedSidecarPath = Join-Path $installRoot 'stock-desk-sidecar.exe'
  $sidecar = Wait-Until { @(Get-InstalledHostSidecarProcesses $desktopProcess.Id $installedSidecarPath) | Select-Object -First 1 } 60 'packaged Python sidecar did not remain running'
  $initialSidecars = @(Get-InstalledHostSidecarProcesses $desktopProcess.Id $installedSidecarPath)
  if ($initialSidecars.Count -ne 1 -or $initialSidecars[0].Id -ne $sidecar.Id) {
    throw 'packaged capture did not own exactly one initial sidecar process'
  }
  if ([IO.Path]::GetFullPath($sidecar.Path) -ne [IO.Path]::GetFullPath($installedSidecarPath)) {
    throw 'initial sidecar process does not belong to the installed candidate'
  }
  $sidecarBinaryHash = (Get-FileHash -LiteralPath $sidecar.Path -Algorithm SHA256).Hash.ToLowerInvariant()
  $nativeEvidence = Save-WindowScreenshot $desktopProcess (Join-Path $Output 'tauri-native-window.png')
  $virtual = [Windows.Forms.SystemInformation]::VirtualScreen
  $nativeEvidence['screen'] = [ordered]@{ x=$virtual.X; y=$virtual.Y; width=$virtual.Width; height=$virtual.Height }
  $nativeEvidence['host_pid'] = $desktopProcess.Id
  $nativeEvidence['sidecar_pid'] = $sidecar.Id
  $hostMainWindowHandle = [int64]$desktopProcess.MainWindowHandle

  $env:SOURCE_SHA = $SourceSha
  $env:SOURCE_TREE = $SourceTree
  $env:STOCK_DESK_DESKTOP_EVIDENCE_DIR = $Output
  $env:STOCK_DESK_DESKTOP_CDP = $desktopCdp
  $env:STOCK_DESK_PACKAGED_BACKTEST_SEED = $packagedBacktestSeed
  $env:STOCK_DESK_CANDIDATE_SHA256 = $candidateSha256
  if (-not (Remove-EvidenceDirectory $restartSyncRoot)) {
    throw 'stale packaged backtest synchronization state could not be cleaned'
  }
  New-Item -ItemType Directory -Force $restartSyncRoot | Out-Null
  $env:STOCK_DESK_RESTART_SYNC_DIR = $restartSyncRoot
  $env:STOCK_DESK_CAPTURE_NONCE = $captureNonce
  $nodeStdout = Join-Path $restartSyncRoot 'node-stdout.log'
  $nodeStderr = Join-Path $restartSyncRoot 'node-stderr.log'
  $nodeProcess = Start-Process -FilePath 'node' -ArgumentList @('scripts/windows_desktop_webview_evidence.mjs') -WorkingDirectory $PWD.Path -RedirectStandardOutput $nodeStdout -RedirectStandardError $nodeStderr -PassThru
  foreach ($fixtureRequest in @(
    @('a_share_constraints_60m','a_share_constraints_60m'),
    @('open_position_costs_1d','open_position_costs_1d'),
    @('partial_pool_gap_1d','partial_pool_gap_1d'),
    @('matrix_1d','matrix_1d'),
    @('checkpoint-matrix-1d','matrix_1d')
  )) {
    $markerName = $fixtureRequest[0]
    $fixtureId = $fixtureRequest[1]
    $fixtureMarkerPath = Join-Path $restartSyncRoot "fixture-$markerName.json"
    $fixtureMarker = Wait-CaptureMarker $fixtureMarkerPath $nodeProcess $captureNonce 300 "packaged fixture switch did not arrive: $fixtureId"
    if ($fixtureMarker.fixture_id -ne $fixtureId) { throw "packaged fixture switch identity is invalid: $fixtureId" }
    & $python (Join-Path $PSScriptRoot 'prepare_windows_packaged_backtest_evidence.py') `
      --destination $packagedDataRoot --source-sha $SourceSha --source-tree $SourceTree --switch-fixture $fixtureId
    if ($LASTEXITCODE -ne 0) { throw "packaged fixture switch failed: $fixtureId" }
    Write-CaptureAck (Join-Path $restartSyncRoot "fixture-$markerName.ack") $captureNonce
  }
  $beforeMarkerPath = Join-Path $restartSyncRoot 'restart-before.json'
  $beforeMarker = Wait-CaptureMarker $beforeMarkerPath $nodeProcess $captureNonce 300 'packaged checkpoint did not reach the sidecar restart boundary'
  if ($beforeMarker.runtime_state -ne 'ready') {
    throw 'pre-restart capture marker identity is invalid'
  }
  $beforeSidecars = @(Get-InstalledHostSidecarProcesses $desktopProcess.Id $installedSidecarPath)
  if ($beforeSidecars.Count -ne 1 -or $beforeSidecars[0].Id -ne $sidecar.Id) {
    throw 'pre-restart packaged sidecar process identity changed unexpectedly'
  }
  if ([IO.Path]::GetFullPath($beforeSidecars[0].Path) -ne [IO.Path]::GetFullPath($installedSidecarPath)) {
    throw 'pre-restart sidecar process does not belong to the installed candidate'
  }
  $sidecarBeforeHash = (Get-FileHash -LiteralPath $beforeSidecars[0].Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($sidecarBeforeHash -ne $sidecarBinaryHash) { throw 'sidecar binary identity changed before restart' }
  $sidecarBeforePid = [int]$beforeSidecars[0].Id
  Write-CaptureAck (Join-Path $restartSyncRoot 'restart-before.ack') $captureNonce
  $afterMarkerPath = Join-Path $restartSyncRoot 'restart-after.json'
  $afterMarker = Wait-CaptureMarker $afterMarkerPath $nodeProcess $captureNonce 120 'packaged sidecar restart did not reach the ready boundary'
  if ($afterMarker.runtime_state -ne 'ready' -or $afterMarker.run_id -ne $beforeMarker.run_id -or $afterMarker.task_id -ne $beforeMarker.task_id) {
    throw 'post-restart capture marker identity is invalid'
  }
  $afterSidecars = @(Get-InstalledHostSidecarProcesses $desktopProcess.Id $installedSidecarPath)
  if ($afterSidecars.Count -ne 1 -or $afterSidecars[0].Id -eq $sidecarBeforePid) {
    throw 'packaged sidecar did not restart as exactly one new OS process'
  }
  if (Get-Process -Id $sidecarBeforePid -ErrorAction SilentlyContinue) {
    throw 'old packaged sidecar OS process survived the runtime restart'
  }
  if ([IO.Path]::GetFullPath($afterSidecars[0].Path) -ne [IO.Path]::GetFullPath($installedSidecarPath)) {
    throw 'restarted sidecar process does not belong to the installed candidate'
  }
  $sidecarAfterHash = (Get-FileHash -LiteralPath $afterSidecars[0].Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($sidecarAfterHash -ne $sidecarBinaryHash) { throw 'sidecar binary identity changed across restart' }
  $sidecarAfterProcess = $afterSidecars[0]
  $sidecarAfterPid = [int]$afterSidecars[0].Id
  # Retain the least-privilege native process handle while the sidecar is alive.
  # Windows keeps the process object and its exit status available until this
  # handle is closed, even after the process has terminated.
  $sidecarAfterHandle = [StockDeskEvidenceNative]::OpenProcess(0x1000, $false, [uint32]$sidecarAfterPid)
  if ($sidecarAfterHandle -eq [IntPtr]::Zero) {
    throw 'restarted sidecar process handle is unavailable'
  }
  Write-CaptureAck (Join-Path $restartSyncRoot 'restart-after.ack') $captureNonce
  foreach ($fixtureRequest in @(
    @('matrix_1w','matrix_1w'),
    @('matrix_60m','matrix_60m')
  )) {
    $markerName = $fixtureRequest[0]
    $fixtureId = $fixtureRequest[1]
    $fixtureMarkerPath = Join-Path $restartSyncRoot "fixture-$markerName.json"
    $fixtureMarker = Wait-CaptureMarker $fixtureMarkerPath $nodeProcess $captureNonce 300 "packaged fixture switch did not arrive after restart: $fixtureId"
    if ($fixtureMarker.fixture_id -ne $fixtureId) { throw "packaged fixture switch identity is invalid after restart: $fixtureId" }
    & $python (Join-Path $PSScriptRoot 'prepare_windows_packaged_backtest_evidence.py') `
      --destination $packagedDataRoot --source-sha $SourceSha --source-tree $SourceTree --switch-fixture $fixtureId
    if ($LASTEXITCODE -ne 0) { throw "packaged fixture switch failed after restart: $fixtureId" }
    Write-CaptureAck (Join-Path $restartSyncRoot "fixture-$markerName.ack") $captureNonce
  }
  Wait-Until { $nodeProcess.Refresh(); if ($nodeProcess.HasExited) { $true } else { $false } } 300 'packaged Tauri WebView evidence did not finish after restart observation' | Out-Null
  if ($nodeProcess.ExitCode -ne 0) {
    throw "packaged Tauri WebView evidence failed: $(Get-Content -LiteralPath $nodeStderr -Raw -ErrorAction SilentlyContinue)"
  }
  try {
    Wait-Until { $desktopProcess.Refresh(); if ($desktopProcess.HasExited) { $true } else { $false } } 25 'packaged app did not complete the tested graceful exit' | Out-Null
  } catch {
    $desktopProcess.Refresh()
    $sidecarAlive = @(Get-Process -Name 'stock-desk-sidecar' -ErrorAction SilentlyContinue).Count -gt 0
    throw "packaged app did not complete the tested graceful exit; host_alive=$(-not $desktopProcess.HasExited); sidecar_alive=$sidecarAlive"
  }
  $desktopProcess.Refresh()
  if ($desktopProcess.ExitCode -ne 0) {
    throw "packaged app exited with a non-zero code during the tested graceful exit: $($desktopProcess.ExitCode)"
  }
  try {
    Wait-Until {
      [uint32]$observedSidecarExitCode = 259
      if (-not [StockDeskEvidenceNative]::GetExitCodeProcess($sidecarAfterHandle, [ref]$observedSidecarExitCode)) {
        throw 'packaged sidecar native exit status became unavailable'
      }
      $sidecarExited = $observedSidecarExitCode -ne 259
      $remainingWebViews = @(Get-IsolatedWebViewProcesses $webviewUserData -Strict)
      if ($sidecarExited -and $remainingWebViews.Count -eq 0) { $true } else { $false }
    } 30 'packaged child processes did not complete the tested graceful exit' | Out-Null
  } catch {
    $childExitFailure = $_.Exception.Message
    $remainingSidecarIds = if (Test-ProcessExitedStrict $sidecarAfterPid) { @() } else { @($sidecarAfterPid) }
    $remainingWebViewIds = @(
      Get-IsolatedWebViewProcesses $webviewUserData |
        ForEach-Object { [int]$_.ProcessId }
    )
    throw "packaged child processes did not complete the tested graceful exit; reason=$childExitFailure; sidecar_ids=$($remainingSidecarIds -join ','); webview_ids=$($remainingWebViewIds -join ',')"
  }
  [uint32]$sidecarAfterExitCode = 0
  if (-not [StockDeskEvidenceNative]::GetExitCodeProcess($sidecarAfterHandle, [ref]$sidecarAfterExitCode)) {
    $sidecarExitCodeError = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    throw "packaged sidecar exit code is unavailable after the tested graceful exit: win32=$sidecarExitCodeError"
  }
  if ($sidecarAfterExitCode -eq 259) {
    throw 'packaged sidecar native handle remained active after the tested graceful exit'
  }
  if ($sidecarAfterExitCode -ne 0) {
    throw "packaged sidecar exited with a non-zero code during the tested graceful exit: $sidecarAfterExitCode"
  }
  $gracefulExit = $true

  $webviewManifest = Join-Path $Output 'tauri-webview-evidence.json'
  if (-not (Test-Path -LiteralPath $webviewManifest -PathType Leaf)) {
    throw 'packaged WebView evidence manifest is missing'
  }
  $packagedBacktestManifest = Join-Path $Output 'packaged-backtest-evidence.json'
  if (-not (Test-Path -LiteralPath $packagedBacktestManifest -PathType Leaf)) {
    throw 'packaged backtest evidence manifest is missing'
  }
  & $python (Join-Path $PSScriptRoot 'capture_packaged_backtest_semantics.py') `
    --data-root $packagedDataRoot --evidence $packagedBacktestManifest
  if ($LASTEXITCODE -ne 0) { throw 'packaged canonical semantic capture failed' }
  $packagedBacktest = Get-Content -LiteralPath $packagedBacktestManifest -Raw | ConvertFrom-Json
  if ($packagedBacktest.capture_nonce -ne $captureNonce -or $packagedBacktest.checkpoint.run_id -ne $beforeMarker.run_id -or $packagedBacktest.checkpoint.task_id -ne $beforeMarker.task_id) {
    throw 'packaged backtest evidence is not bound to the Windows process observation'
  }
  $hostObservation = [ordered]@{
    schema_version = 'stock-desk-packaged-backtest-host-observation-v1'
    source_sha = $SourceSha
    source_tree = $SourceTree
    candidate_sha256 = $candidateSha256
    capture_nonce = $captureNonce
    capture_scope = 'installed-current-user-tauri-webview'
    host_ipc_command = 'desktop_api_request'
    host_pid = [int]$desktopProcess.Id
    main_window_handle = $hostMainWindowHandle
    installed_host_sha256 = (Get-FileHash -LiteralPath $hostPath -Algorithm SHA256).Hash.ToLowerInvariant()
    isolated_webview_process_ids = @($isolatedWebViews | ForEach-Object { [int]$_.ProcessId } | Sort-Object -Unique)
    sidecar_before = [ordered]@{ pid=$sidecarBeforePid; executable_sha256=$sidecarBinaryHash }
    sidecar_after = [ordered]@{ pid=$sidecarAfterPid; executable_sha256=$sidecarAfterHash }
    checkpoint = [ordered]@{ run_id=[string]$beforeMarker.run_id; task_id=[string]$beforeMarker.task_id }
    evidence_sha256 = (Get-FileHash -LiteralPath $packagedBacktestManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  }
  $hostObservation | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Output 'packaged-backtest-host-observation.json') -Encoding utf8NoBOM
  Copy-Item -LiteralPath $packagedBacktestSeed -Destination (Join-Path $Output 'packaged-backtest-seed.json') -Force
  $manifest = [ordered]@{
    schema_version = 'stock-desk-windows-desktop-evidence-v1'
    source_sha = $SourceSha
    source_tree = $SourceTree
    candidate_sha256 = $candidateSha256
    actual_packaged_tauri = $true
    native = $nativeEvidence
    icons = $iconEvidence
    webview = [ordered]@{
      manifest = 'tauri-webview-evidence.json'
      sha256 = (Get-FileHash -LiteralPath $webviewManifest -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    packaged_backtests = [ordered]@{
      manifest = 'packaged-backtest-evidence.json'
      sha256 = (Get-FileHash -LiteralPath $packagedBacktestManifest -Algorithm SHA256).Hash.ToLowerInvariant()
      seed = 'packaged-backtest-seed.json'
      seed_sha256 = (Get-FileHash -LiteralPath $packagedBacktestSeed -Algorithm SHA256).Hash.ToLowerInvariant()
      host_observation = 'packaged-backtest-host-observation.json'
      host_observation_sha256 = (Get-FileHash -LiteralPath (Join-Path $Output 'packaged-backtest-host-observation.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    graceful_exit = $true
    hosted_runner_limitations = @(
      'The native screenshot records the GitHub-hosted runner current Windows DPI only.',
      '125-200 percent coverage is explicitly CDP effective-viewport evidence inside the packaged Tauri WebView, not an OS DPI change.',
      'Windows 10/11 real 200 percent DPI and live taskbar/Start menu visual confirmation remain separate release-machine evidence.'
    )
  }
  $manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $Output 'windows-desktop-evidence.json') -Encoding utf8NoBOM
} finally {
  if ($sidecarAfterHandle -ne [IntPtr]::Zero) {
    [StockDeskEvidenceNative]::CloseHandle($sidecarAfterHandle) | Out-Null
    $sidecarAfterHandle = [IntPtr]::Zero
  }
  if (-not $gracefulExit -and (Test-Path -LiteralPath $Output -PathType Container)) {
    $diagnosticsRoot = Join-Path (Split-Path (Split-Path $Output -Parent) -Parent) 'diagnostics'
    New-Item -ItemType Directory -Force $diagnosticsRoot | Out-Null
    Get-ChildItem -LiteralPath $Output -File -Filter 'packaged-*' -ErrorAction SilentlyContinue |
      Copy-Item -Destination $diagnosticsRoot -Force
    $webviewProcesses = @(Get-IsolatedWebViewProcesses $webviewUserData)
    $diagnosticWebViewProcessIds = @($webviewProcesses | ForEach-Object { [int]$_.ProcessId })
    $diagnosticDevToolsListeners = @(
      if ($devToolsPort -gt 0) {
        Get-NetTCPConnection -State Listen -LocalPort $devToolsPort -ErrorAction SilentlyContinue |
          Where-Object {
            $_.LocalAddress -eq '127.0.0.1' -and
            $diagnosticWebViewProcessIds -contains [int]($_.OwningProcess)
          }
      }
    )
    [ordered]@{
      schema_version = 1
      isolated_user_data_created = (Test-Path -LiteralPath $webviewUserData -PathType Container)
      selected_loopback_devtools_port = $devToolsPort
      webview_process_scope = 'isolated-user-data-folder'
      webview_process_count = $webviewProcesses.Count
      devtools_listener_owned_by_isolated_webview = $diagnosticDevToolsListeners.Count -eq 1
      host_has_main_window = if ($null -ne $desktopProcess) { $desktopProcess.MainWindowHandle -ne [IntPtr]::Zero } else { $false }
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $diagnosticsRoot 'webview-startup-summary.json') -Encoding utf8NoBOM
  }
  Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
  Remove-Item Env:WEBVIEW2_USER_DATA_FOLDER -ErrorAction SilentlyContinue
  Remove-Item Env:STOCK_DESK_DESKTOP_CDP -ErrorAction SilentlyContinue
  Remove-Item Env:STOCK_DESK_PACKAGED_BACKTEST_SEED -ErrorAction SilentlyContinue
  Remove-Item Env:STOCK_DESK_CANDIDATE_SHA256 -ErrorAction SilentlyContinue
  Remove-Item Env:STOCK_DESK_RESTART_SYNC_DIR -ErrorAction SilentlyContinue
  Remove-Item Env:STOCK_DESK_CAPTURE_NONCE -ErrorAction SilentlyContinue
  if ($webviewArgsPolicySet) { Remove-ItemProperty -LiteralPath $webviewArgsPolicy -Name $webviewAppName -ErrorAction SilentlyContinue }
  if ($webviewDataPolicySet) { Remove-ItemProperty -LiteralPath $webviewDataPolicy -Name $webviewAppName -ErrorAction SilentlyContinue }
  if ($webviewArgsPolicyCreated) { Remove-Item -LiteralPath $webviewArgsPolicy -Force -ErrorAction SilentlyContinue }
  if ($webviewDataPolicyCreated) { Remove-Item -LiteralPath $webviewDataPolicy -Force -ErrorAction SilentlyContinue }
  if ($webviewPolicyRootCreated) { Remove-Item -LiteralPath $webviewPolicyRoot -Force -ErrorAction SilentlyContinue }
  if ($webviewEdgePolicyCreated) { Remove-Item -LiteralPath $webviewEdgePolicy -Force -ErrorAction SilentlyContinue }
  $unexpectedGracefulResidue = @()
  if ($null -ne $nodeProcess) {
    $nodeProcess.Refresh()
    if (-not $nodeProcess.HasExited) {
      if ($gracefulExit) { $unexpectedGracefulResidue += "node:$($nodeProcess.Id)" }
      Stop-Process -Id $nodeProcess.Id -Force -ErrorAction SilentlyContinue
    }
    try { $nodeProcess.WaitForExit(5000) | Out-Null } catch { }
  }
  if (-not $gracefulExit -and $null -ne $diagnosticsRoot) {
    foreach ($runtimeLog in @($nodeStdout, $nodeStderr)) {
      Copy-DiagnosticRuntimeLog -Path $runtimeLog -Destination $diagnosticsRoot | Out-Null
    }
  }
  if ($null -ne $desktopProcess) {
    $desktopProcess.Refresh()
    if (-not $desktopProcess.HasExited) {
      if ($gracefulExit) { $unexpectedGracefulResidue += "host:$($desktopProcess.Id)" }
      Stop-Process -Id $desktopProcess.Id -Force -ErrorAction SilentlyContinue
    }
  }
  for ($cleanupAttempt = 1; $cleanupAttempt -le 10; $cleanupAttempt++) {
    $evidenceSidecars = @(Get-EvidenceSidecarProcesses $baselineSidecarProcessIds)
    if ($evidenceSidecars.Count -eq 0) { break }
    $evidenceSidecars | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 250
  }
  $remainingEvidenceSidecars = @(Get-EvidenceSidecarProcesses $baselineSidecarProcessIds)
  if ($gracefulExit -and $remainingEvidenceSidecars.Count -gt 0) {
    $unexpectedGracefulResidue += @(
      $remainingEvidenceSidecars | ForEach-Object { "sidecar:$($_.Id)" }
    )
  }
  for ($cleanupAttempt = 1; $cleanupAttempt -le 10; $cleanupAttempt++) {
    $isolatedWebViewProcesses = @(Get-IsolatedWebViewProcesses $webviewUserData)
    if ($isolatedWebViewProcesses.Count -eq 0) { break }
    $isolatedWebViewProcesses | ForEach-Object {
      Stop-Process -Id ([int]$_.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 250
  }
  $remainingIsolatedWebViews = @(Get-IsolatedWebViewProcesses $webviewUserData)
  if ($gracefulExit -and $remainingIsolatedWebViews.Count -gt 0) {
    $unexpectedGracefulResidue += @(
      $remainingIsolatedWebViews | ForEach-Object { "webview:$($_.ProcessId)" }
    )
  }
  if (Test-Path -LiteralPath $uninstallerPath -PathType Leaf) {
    $cleanup = Start-Process -FilePath $uninstallerPath -ArgumentList '/S' -Wait -PassThru
    if ($cleanup.ExitCode -ne 0 -and $gracefulExit) { throw 'test uninstall failed after desktop evidence' }
  }
  $packagedDataCleaned = Remove-EvidenceDirectory $packagedDataRoot
  for ($cleanupAttempt = 1; $cleanupAttempt -le 10; $cleanupAttempt++) {
    Remove-Item -Recurse -Force $webviewUserData -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $webviewUserData)) { break }
    Start-Sleep -Milliseconds 250
  }
  $restartSyncCleaned = Remove-EvidenceDirectory $restartSyncRoot
  if (-not $tauriDefaultWebViewDataExisted) {
    for ($cleanupAttempt = 1; $cleanupAttempt -le 10; $cleanupAttempt++) {
      Remove-Item -Recurse -Force $tauriDefaultWebViewData -ErrorAction SilentlyContinue
      if (-not (Test-Path -LiteralPath $tauriDefaultWebViewData)) { break }
      Start-Sleep -Milliseconds 250
    }
  }
  if ($gracefulExit -and (Test-Path -LiteralPath $webviewUserData)) {
    throw 'isolated WebView2 evidence state could not be cleaned'
  }
  if ($gracefulExit -and -not $packagedDataCleaned) {
    throw 'packaged backtest data state could not be cleaned'
  }
  if ($gracefulExit -and -not $restartSyncCleaned) {
    throw 'packaged backtest synchronization state could not be cleaned'
  }
  if ($gracefulExit -and -not $tauriDefaultWebViewDataExisted -and (Test-Path -LiteralPath $tauriDefaultWebViewData)) {
    throw 'Tauri default WebView2 state created by evidence could not be cleaned'
  }
  if ($gracefulExit -and $unexpectedGracefulResidue.Count -gt 0) {
    throw "packaged processes unexpectedly remained after graceful exit: $($unexpectedGracefulResidue -join ',')"
  }
}
