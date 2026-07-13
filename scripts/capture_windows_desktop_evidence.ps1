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
$desktopProcess = $null
$gracefulExit = $false
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
  # from any reused WebView2 browser process. Port zero lets WebView2 select an
  # unused loopback port; DevToolsActivePort is then the authoritative endpoint.
  Remove-Item -Recurse -Force $webviewUserData -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $webviewUserData | Out-Null
  $env:WEBVIEW2_USER_DATA_FOLDER = $webviewUserData
  $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = '--remote-debugging-port=0 --remote-debugging-address=127.0.0.1'
  New-Item -Path $webviewEdgePolicy -Force | Out-Null
  New-Item -Path $webviewPolicyRoot -Force | Out-Null
  foreach ($policy in @($webviewArgsPolicy, $webviewDataPolicy)) {
    if ($null -ne (Get-ItemProperty -LiteralPath $policy -Name $webviewAppName -ErrorAction SilentlyContinue)) {
      throw 'packaged WebView2 evidence refuses to replace an existing app policy'
    }
    New-Item -Path $policy -Force | Out-Null
  }
  New-ItemProperty -LiteralPath $webviewArgsPolicy -Name $webviewAppName -PropertyType String -Value $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS | Out-Null
  $webviewArgsPolicySet = $true
  New-ItemProperty -LiteralPath $webviewDataPolicy -Name $webviewAppName -PropertyType String -Value $webviewUserData | Out-Null
  $webviewDataPolicySet = $true
  # This candidate proof owns an isolated first-run state. Removing stale state
  # makes onboarding evidence deterministic even on a reused Windows host.
  Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA 'Stock Desk\v1.1') -ErrorAction SilentlyContinue
  $desktopStart = [Diagnostics.ProcessStartInfo]::new()
  $desktopStart.FileName = $hostPath
  $desktopStart.WorkingDirectory = $installRoot
  $desktopStart.UseShellExecute = $false
  $desktopStart.Environment['WEBVIEW2_USER_DATA_FOLDER'] = $webviewUserData
  $desktopStart.Environment['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS
  $desktopProcess = [Diagnostics.Process]::Start($desktopStart)
  if ($null -eq $desktopProcess) { throw 'packaged Tauri host could not be started' }
  Wait-Until { $desktopProcess.Refresh(); if ($desktopProcess.HasExited) { throw 'packaged Tauri host exited during startup' }; if ($desktopProcess.MainWindowHandle -ne [IntPtr]::Zero) { $desktopProcess.MainWindowHandle } } 90 'packaged Tauri main window did not appear' | Out-Null
  $devToolsPortFile = Wait-Until {
    @(Get-ChildItem -LiteralPath $webviewUserData -Recurse -File -Filter 'DevToolsActivePort' -ErrorAction SilentlyContinue) | Select-Object -First 1
  } 90 'packaged WebView2 did not publish its isolated DevTools port'
  $devToolsPortLines = @(Get-Content -LiteralPath $devToolsPortFile.FullName -ErrorAction Stop)
  [int]$devToolsPort = 0
  $devToolsPath = if ($devToolsPortLines.Count -ge 2) { [string]$devToolsPortLines[1] } else { '' }
  if ($devToolsPortLines.Count -ne 2 -or -not [int]::TryParse([string]$devToolsPortLines[0], [ref]$devToolsPort) -or $devToolsPort -lt 1 -or $devToolsPort -gt 65535 -or $devToolsPath -notmatch '^/devtools/browser/[A-Za-z0-9-]+$') {
    throw 'packaged WebView2 published an invalid isolated DevTools port'
  }
  $desktopCdp = "http://127.0.0.1:$devToolsPort"
  $devToolsVersion = Wait-Until { try { Invoke-RestMethod -Uri "$desktopCdp/json/version" -TimeoutSec 2 } catch { $false } } 90 'packaged WebView2 CDP endpoint did not appear'
  try { $devToolsWebSocket = [Uri]$devToolsVersion.webSocketDebuggerUrl }
  catch { throw 'packaged WebView2 published an invalid CDP browser endpoint' }
  if ($devToolsWebSocket.Scheme -ne 'ws' -or $devToolsWebSocket.Host -ne '127.0.0.1' -or $devToolsWebSocket.Port -ne $devToolsPort -or $devToolsWebSocket.AbsolutePath -ne $devToolsPath) {
    throw 'packaged WebView2 CDP endpoint does not match the isolated browser identity'
  }
  $sidecar = Wait-Until { @(Get-Process -Name 'stock-desk-sidecar' -ErrorAction SilentlyContinue) | Select-Object -First 1 } 60 'packaged Python sidecar did not remain running'

  $nativeEvidence = Save-WindowScreenshot $desktopProcess (Join-Path $Output 'tauri-native-window.png')
  $virtual = [Windows.Forms.SystemInformation]::VirtualScreen
  $nativeEvidence['screen'] = [ordered]@{ x=$virtual.X; y=$virtual.Y; width=$virtual.Width; height=$virtual.Height }
  $nativeEvidence['host_pid'] = $desktopProcess.Id
  $nativeEvidence['sidecar_pid'] = $sidecar.Id

  $env:SOURCE_SHA = $SourceSha
  $env:SOURCE_TREE = $SourceTree
  $env:STOCK_DESK_DESKTOP_EVIDENCE_DIR = $Output
  $env:STOCK_DESK_DESKTOP_CDP = $desktopCdp
  & node scripts/windows_desktop_webview_evidence.mjs
  if ($LASTEXITCODE -ne 0) { throw 'packaged Tauri WebView evidence failed' }
  try {
    Wait-Until { $desktopProcess.Refresh(); if ($desktopProcess.HasExited) { $true } else { $false } } 25 'packaged app did not complete the tested graceful exit' | Out-Null
  } catch {
    $desktopProcess.Refresh()
    $sidecarAlive = @(Get-Process -Name 'stock-desk-sidecar' -ErrorAction SilentlyContinue).Count -gt 0
    throw "packaged app did not complete the tested graceful exit; host_alive=$(-not $desktopProcess.HasExited); sidecar_alive=$sidecarAlive"
  }
  $gracefulExit = $true

  $webviewManifest = Join-Path $Output 'tauri-webview-evidence.json'
  if (-not (Test-Path -LiteralPath $webviewManifest -PathType Leaf)) {
    throw 'packaged WebView evidence manifest is missing'
  }
  $manifest = [ordered]@{
    schema_version = 'stock-desk-windows-desktop-evidence-v1'
    source_sha = $SourceSha
    source_tree = $SourceTree
    actual_packaged_tauri = $true
    native = $nativeEvidence
    icons = $iconEvidence
    webview = [ordered]@{
      manifest = 'tauri-webview-evidence.json'
      sha256 = (Get-FileHash -LiteralPath $webviewManifest -Algorithm SHA256).Hash.ToLowerInvariant()
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
  if (-not $gracefulExit -and (Test-Path -LiteralPath $Output -PathType Container)) {
    $diagnosticsRoot = Join-Path (Split-Path (Split-Path $Output -Parent) -Parent) 'diagnostics'
    New-Item -ItemType Directory -Force $diagnosticsRoot | Out-Null
    Get-ChildItem -LiteralPath $Output -File -Filter 'packaged-*' -ErrorAction SilentlyContinue |
      Copy-Item -Destination $diagnosticsRoot -Force
    $webviewProcesses = @(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" -ErrorAction SilentlyContinue)
    [ordered]@{
      schema_version = 1
      isolated_user_data_created = (Test-Path -LiteralPath $webviewUserData -PathType Container)
      devtools_port_file_count = @(Get-ChildItem -LiteralPath $webviewUserData -Recurse -File -Filter 'DevToolsActivePort' -ErrorAction SilentlyContinue).Count
      webview_process_count = $webviewProcesses.Count
      webview_remote_debug_argument_observed = @($webviewProcesses | Where-Object { $_.CommandLine -like '*--remote-debugging-port=*' }).Count -gt 0
      host_has_main_window = if ($null -ne $desktopProcess) { $desktopProcess.MainWindowHandle -ne [IntPtr]::Zero } else { $false }
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $diagnosticsRoot 'webview-startup-summary.json') -Encoding utf8NoBOM
  }
  Remove-Item Env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS -ErrorAction SilentlyContinue
  Remove-Item Env:WEBVIEW2_USER_DATA_FOLDER -ErrorAction SilentlyContinue
  Remove-Item Env:STOCK_DESK_DESKTOP_CDP -ErrorAction SilentlyContinue
  if ($webviewArgsPolicySet) { Remove-ItemProperty -LiteralPath $webviewArgsPolicy -Name $webviewAppName -ErrorAction SilentlyContinue }
  if ($webviewDataPolicySet) { Remove-ItemProperty -LiteralPath $webviewDataPolicy -Name $webviewAppName -ErrorAction SilentlyContinue }
  if ($webviewArgsPolicyCreated) { Remove-Item -LiteralPath $webviewArgsPolicy -Force -ErrorAction SilentlyContinue }
  if ($webviewDataPolicyCreated) { Remove-Item -LiteralPath $webviewDataPolicy -Force -ErrorAction SilentlyContinue }
  if ($webviewPolicyRootCreated) { Remove-Item -LiteralPath $webviewPolicyRoot -Force -ErrorAction SilentlyContinue }
  if ($webviewEdgePolicyCreated) { Remove-Item -LiteralPath $webviewEdgePolicy -Force -ErrorAction SilentlyContinue }
  if ($null -ne $desktopProcess) {
    $desktopProcess.Refresh()
    if (-not $desktopProcess.HasExited) {
      Stop-Process -Id $desktopProcess.Id -Force -ErrorAction SilentlyContinue
      Get-Process -Name 'stock-desk-sidecar' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
      if ($gracefulExit) { throw 'packaged process unexpectedly remained after graceful exit' }
    }
  }
  if (Test-Path -LiteralPath $uninstallerPath -PathType Leaf) {
    $cleanup = Start-Process -FilePath $uninstallerPath -ArgumentList '/S' -Wait -PassThru
    if ($cleanup.ExitCode -ne 0 -and $gracefulExit) { throw 'test uninstall failed after desktop evidence' }
  }
  Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA 'Stock Desk\v1.1') -ErrorAction SilentlyContinue
  for ($cleanupAttempt = 1; $cleanupAttempt -le 10; $cleanupAttempt++) {
    Remove-Item -Recurse -Force $webviewUserData -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $webviewUserData)) { break }
    Start-Sleep -Milliseconds 250
  }
  if ($gracefulExit -and (Test-Path -LiteralPath $webviewUserData)) {
    throw 'isolated WebView2 evidence state could not be cleaned'
  }
}
