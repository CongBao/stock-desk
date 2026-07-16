[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][long]$WindowHandle,
  [Parameter(Mandatory = $true)][int]$ExpectedProcessId,
  [Parameter(Mandatory = $true)][int]$EvidenceProcessId,
  [Parameter(Mandatory = $true)][string]$ExpectedExecutableSha256,
  [Parameter(Mandatory = $true)][string]$SourceSha,
  [Parameter(Mandatory = $true)][string]$SourceTree,
  [Parameter(Mandatory = $true)][string]$CandidateSha256,
  [Parameter(Mandatory = $true)][string]$CaptureSyncRoot,
  [Parameter(Mandatory = $true)][string]$CaptureNonce,
  [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $IsWindows) { throw 'real desktop click evidence requires Windows' }
foreach ($identity in @($SourceSha, $SourceTree)) {
  if ($identity -cnotmatch '^[0-9a-f]{40}$') { throw 'real click source identity is invalid' }
}
foreach ($digest in @($ExpectedExecutableSha256, $CandidateSha256)) {
  if ($digest -cnotmatch '^[0-9a-f]{64}$') { throw 'real click binary identity is invalid' }
}
if ($CaptureNonce -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') {
  throw 'real click capture nonce is invalid'
}
$CaptureSyncRoot = [IO.Path]::GetFullPath($CaptureSyncRoot)
if (-not (Test-Path -LiteralPath $CaptureSyncRoot -PathType Container)) {
  throw 'real click capture root is unavailable'
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class StockDeskRealMouseInput {
  [StructLayout(LayoutKind.Sequential)]
  public struct POINT {
    public int X;
    public int Y;
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct RECT {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct MOUSEINPUT {
    public int dx;
    public int dy;
    public uint mouseData;
    public uint dwFlags;
    public uint time;
    public UIntPtr dwExtraInfo;
  }

  [StructLayout(LayoutKind.Explicit)]
  public struct INPUTUNION {
    [FieldOffset(0)] public MOUSEINPUT mouse;
  }

  [StructLayout(LayoutKind.Sequential)]
  public struct INPUT {
    public uint type;
    public INPUTUNION data;
  }

  [DllImport("user32.dll", SetLastError=true)]
  public static extern uint SendInput(uint count, INPUT[] inputs, int size);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern int GetSystemMetrics(int index);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool SetForegroundWindow(IntPtr hwnd);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool ShowWindow(IntPtr hwnd, int command);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool GetClientRect(IntPtr hwnd, out RECT rect);
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool ClientToScreen(IntPtr hwnd, ref POINT point);
  [DllImport("user32.dll")]
  public static extern IntPtr WindowFromPoint(POINT point);
  [DllImport("user32.dll")]
  public static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);

  public const int SM_XVIRTUALSCREEN = 76;
  public const int SM_YVIRTUALSCREEN = 77;
  public const int SM_CXVIRTUALSCREEN = 78;
  public const int SM_CYVIRTUALSCREEN = 79;
  public const uint INPUT_MOUSE = 0;
  public const uint MOUSEEVENTF_MOVE = 0x0001;
  public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
  public const uint MOUSEEVENTF_LEFTUP = 0x0004;
  public const uint MOUSEEVENTF_VIRTUALDESK = 0x4000;
  public const uint MOUSEEVENTF_ABSOLUTE = 0x8000;
  public const uint GA_ROOT = 2;
}
'@

$hwnd = [IntPtr]$WindowHandle
$process = [Diagnostics.Process]::GetProcessById($ExpectedProcessId)
$evidenceProcess = [Diagnostics.Process]::GetProcessById($EvidenceProcessId)
$actions = [Collections.Generic.List[object]]::new()

function Wait-Until([scriptblock]$Condition, [int]$Seconds, [string]$Failure) {
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($Seconds)
  do {
    $value = & $Condition
    if ($null -ne $value -and $value -ne $false) { return $value }
    Start-Sleep -Milliseconds 100
  } while ([DateTimeOffset]::UtcNow -lt $deadline)
  throw $Failure
}

function Wait-CaptureMarker([string]$Name, [int]$Seconds, [string]$Failure) {
  $path = Join-Path $CaptureSyncRoot "$Name.json"
  return Wait-Until {
    $evidenceProcess.Refresh()
    if ($evidenceProcess.HasExited) {
      throw "packaged WebView evidence exited before real click marker: $Name"
    }
    if (Test-Path -LiteralPath $path -PathType Leaf) {
      try {
        $candidate = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($candidate.capture_nonce -ceq $CaptureNonce) { return $candidate }
      } catch {
        # The producer publishes by atomic rename. Antivirus and indexing may
        # still transiently deny the first read, so retry within the deadline.
      }
    }
    return $false
  } $Seconds $Failure
}

function Write-CaptureAck([string]$Name) {
  $path = Join-Path $CaptureSyncRoot "$Name.ack"
  $temporary = Join-Path $CaptureSyncRoot ".$Name.$CaptureNonce.tmp"
  for ($attempt = 1; $attempt -le 10; $attempt++) {
    try {
      $CaptureNonce | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
      Move-Item -LiteralPath $temporary -Destination $path -Force
      return
    } catch {
      Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
      if ($attempt -eq 10) { throw "real click acknowledgment could not be published: $Name" }
      Start-Sleep -Milliseconds 100
    }
  }
}

function Get-RuntimeId([System.Windows.Automation.AutomationElement]$Element) {
  return @($Element.GetRuntimeId()) -join '.'
}

function Get-RootElement {
  $condition = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
    [int]$WindowHandle
  )
  $element = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
    [System.Windows.Automation.TreeScope]::Children,
    $condition
  )
  if ($null -eq $element) { throw 'installed Stock Desk HWND is not exposed to UI Automation' }
  if ($element.Current.ProcessId -ne $ExpectedProcessId) {
    throw 'real click UI Automation root belongs to another process'
  }
  return $element
}

function Test-HasTitleBarAncestor(
  [System.Windows.Automation.AutomationElement]$Element
) {
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  $ancestor = $walker.GetParent($Element)
  for ($depth = 0; $depth -lt 16 -and $null -ne $ancestor; $depth++) {
    if ($ancestor.Current.ProcessId -ne $ExpectedProcessId) { return $false }
    if ($ancestor.Current.ControlType -eq [System.Windows.Automation.ControlType]::TitleBar) {
      return $true
    }
    $ancestor = $walker.GetParent($ancestor)
  }
  return $false
}

function Find-Button {
  param(
    [string[]]$Names = @(),
    [string[]]$AutomationIds = @(),
    [int]$TimeoutSeconds = 15,
    [switch]$RequireTitleBarAncestor,
    [switch]$Optional
  )
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
  do {
    if ($process.HasExited) {
      if ($Optional) { return $null }
      throw 'installed Stock Desk exited before the click target appeared'
    }
    $matches = [Collections.Generic.List[System.Windows.Automation.AutomationElement]]::new()
    $all = (Get-RootElement).FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($candidate in $all) {
      if ($candidate.Current.ControlType -ne [System.Windows.Automation.ControlType]::Button) { continue }
      $name = ([string]$candidate.Current.Name).Trim()
      $automationId = ([string]$candidate.Current.AutomationId).Trim()
      if (
        ($Names.Count -gt 0 -and $Names -contains $name) -or
        ($AutomationIds.Count -gt 0 -and $AutomationIds -contains $automationId)
      ) {
        if (
          $candidate.Current.IsEnabled -and
          -not $candidate.Current.IsOffscreen -and
          (-not $RequireTitleBarAncestor -or (Test-HasTitleBarAncestor $candidate))
        ) {
          $matches.Add($candidate)
        }
      }
    }
    if ($matches.Count -eq 1) { return $matches[0] }
    if ($matches.Count -gt 1) { throw 'real click target is not unique' }
    Start-Sleep -Milliseconds 100
  } while ([DateTimeOffset]::UtcNow -lt $deadline)
  if ($Optional) { return $null }
  throw "real click target was not found: $($Names + $AutomationIds -join ' / ')"
}

function Get-ButtonAtPoint([Windows.Point]$Point) {
  $candidate = [System.Windows.Automation.AutomationElement]::FromPoint($Point)
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  for ($depth = 0; $depth -lt 8 -and $null -ne $candidate; $depth++) {
    if ($candidate.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {
      return $candidate
    }
    $candidate = $walker.GetParent($candidate)
  }
  return $null
}

function Focus-Window {
  [StockDeskRealMouseInput]::ShowWindow($hwnd, 5) | Out-Null
  for ($attempt = 1; $attempt -le 10; $attempt++) {
    if ([StockDeskRealMouseInput]::GetForegroundWindow() -eq $hwnd) {
      return $true
    }
    [StockDeskRealMouseInput]::SetForegroundWindow($hwnd) | Out-Null
    Start-Sleep -Milliseconds 100
  }
  return $false
}

function Invoke-PhysicalPointClick {
  param(
    [string]$Action,
    [int]$CenterX,
    [int]$CenterY,
    [Collections.IDictionary]$TargetEvidence
  )
  $foregroundBeforeClick = [long][StockDeskRealMouseInput]::GetForegroundWindow()
  $focusPreparationSucceeded = Focus-Window
  $virtualX = [StockDeskRealMouseInput]::GetSystemMetrics([StockDeskRealMouseInput]::SM_XVIRTUALSCREEN)
  $virtualY = [StockDeskRealMouseInput]::GetSystemMetrics([StockDeskRealMouseInput]::SM_YVIRTUALSCREEN)
  $virtualWidth = [StockDeskRealMouseInput]::GetSystemMetrics([StockDeskRealMouseInput]::SM_CXVIRTUALSCREEN)
  $virtualHeight = [StockDeskRealMouseInput]::GetSystemMetrics([StockDeskRealMouseInput]::SM_CYVIRTUALSCREEN)
  if ($virtualWidth -lt 2 -or $virtualHeight -lt 2) { throw 'Windows virtual screen metrics are invalid' }
  if (
    $centerX -lt $virtualX -or $centerX -ge $virtualX + $virtualWidth -or
    $centerY -lt $virtualY -or $centerY -ge $virtualY + $virtualHeight
  ) { throw 'real click target is outside the Windows virtual screen' }

  $absoluteX = [int][Math]::Round((($centerX - $virtualX) * 65535.0) / ($virtualWidth - 1))
  $absoluteY = [int][Math]::Round((($centerY - $virtualY) * 65535.0) / ($virtualHeight - 1))
  $inputs = [StockDeskRealMouseInput+INPUT[]]::new(3)
  $inputs[0].type = [StockDeskRealMouseInput]::INPUT_MOUSE
  $inputs[0].data.mouse.dx = $absoluteX
  $inputs[0].data.mouse.dy = $absoluteY
  $inputs[0].data.mouse.dwFlags = (
    [StockDeskRealMouseInput]::MOUSEEVENTF_MOVE -bor
    [StockDeskRealMouseInput]::MOUSEEVENTF_ABSOLUTE -bor
    [StockDeskRealMouseInput]::MOUSEEVENTF_VIRTUALDESK
  )
  $inputs[1].type = [StockDeskRealMouseInput]::INPUT_MOUSE
  $inputs[1].data.mouse.dwFlags = [StockDeskRealMouseInput]::MOUSEEVENTF_LEFTDOWN
  $inputs[2].type = [StockDeskRealMouseInput]::INPUT_MOUSE
  $inputs[2].data.mouse.dwFlags = [StockDeskRealMouseInput]::MOUSEEVENTF_LEFTUP
  $sent = [StockDeskRealMouseInput]::SendInput(
    [uint32]$inputs.Count,
    $inputs,
    [Runtime.InteropServices.Marshal]::SizeOf([type][StockDeskRealMouseInput+INPUT])
  )
  if ($sent -ne 3) {
    $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
    throw "Windows SendInput did not publish the exact mouse sequence: sent=$sent win32=$errorCode"
  }
  $foregroundAfterClick = [long][StockDeskRealMouseInput]::GetForegroundWindow()
  $record = [ordered]@{
      action = $Action
      center = [ordered]@{ x=$centerX; y=$centerY }
      focus_preparation_succeeded = $focusPreparationSucceeded
      foreground_hwnd_before_click = $foregroundBeforeClick
      foreground_hwnd_after_click = $foregroundAfterClick
      foreground_hwnd = $foregroundAfterClick
      send_input_returned = [int]$sent
      captured_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
  }
  foreach ($key in $TargetEvidence.Keys) {
    if ($record.Contains($key)) { throw "real click evidence field is duplicated: $key" }
    $record[$key] = $TargetEvidence[$key]
  }
  $actions.Add($record)
}

function Invoke-PhysicalClick {
  param(
    [string]$Action,
    [System.Windows.Automation.AutomationElement]$Element
  )
  $automationId = [string]$Element.Current.AutomationId
  $name = [string]$Element.Current.Name
  $controlType = [string]$Element.Current.ControlType.ProgrammaticName
  $runtimeId = Get-RuntimeId $Element
  $rect = $Element.Current.BoundingRectangle
  if ($rect.Width -lt 2 -or $rect.Height -lt 2) { throw 'real click target bounds are unusable' }
  $centerX = [int][Math]::Round($rect.X + ($rect.Width / 2.0))
  $centerY = [int][Math]::Round($rect.Y + ($rect.Height / 2.0))
  $point = [Windows.Point]::new($centerX, $centerY)
  $hit = Get-ButtonAtPoint $point
  $hitRuntimeId = if ($null -eq $hit) { '' } else { Get-RuntimeId $hit }
  if ($null -eq $hit -or $hitRuntimeId -cne $runtimeId) {
    throw 'UI Automation FromPoint did not resolve the exact physical click target'
  }
  Invoke-PhysicalPointClick -Action $Action -CenterX $centerX -CenterY $centerY -TargetEvidence ([ordered]@{
      target_source = 'windows-uia-exact-from-point'
      automation_id = $automationId
      name = $name
      control_type = $controlType
      runtime_id = $runtimeId
      bounding_rectangle = [ordered]@{ x=$rect.X; y=$rect.Y; width=$rect.Width; height=$rect.Height }
      from_point_runtime_id = $hitRuntimeId
    })
}

function Convert-ToFiniteDouble([object]$Value, [string]$Field) {
  try { $number = [double]$Value } catch { throw "real click DOM target field is not numeric: $Field" }
  if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) {
    throw "real click DOM target field is not finite: $Field"
  }
  return $number
}

function Get-DomTargetPoint([object]$Target, [string]$ExpectedName) {
  if (
    $null -eq $Target -or
    [string]$Target.name -cne $ExpectedName -or
    [string]$Target.role -cne 'button' -or
    $Target.enabled -ne $true -or
    $Target.visible -ne $true -or
    $Target.dom_hit_test -ne $true
  ) { throw "real click DOM target identity is invalid: $ExpectedName" }

  $x = Convert-ToFiniteDouble $Target.bounding_rectangle_css.x 'bounding_rectangle_css.x'
  $y = Convert-ToFiniteDouble $Target.bounding_rectangle_css.y 'bounding_rectangle_css.y'
  $width = Convert-ToFiniteDouble $Target.bounding_rectangle_css.width 'bounding_rectangle_css.width'
  $height = Convert-ToFiniteDouble $Target.bounding_rectangle_css.height 'bounding_rectangle_css.height'
  $viewportWidth = Convert-ToFiniteDouble $Target.viewport_css.width 'viewport_css.width'
  $viewportHeight = Convert-ToFiniteDouble $Target.viewport_css.height 'viewport_css.height'
  $devicePixelRatio = Convert-ToFiniteDouble $Target.device_pixel_ratio 'device_pixel_ratio'
  if (
    $width -lt 2 -or $height -lt 2 -or
    $viewportWidth -lt 2 -or $viewportHeight -lt 2 -or
    $devicePixelRatio -le 0
  ) {
    throw 'real click DOM target geometry is unusable'
  }
  $centerCssX = $x + ($width / 2.0)
  $centerCssY = $y + ($height / 2.0)
  if (
    $centerCssX -lt 0 -or $centerCssX -ge $viewportWidth -or
    $centerCssY -lt 0 -or $centerCssY -ge $viewportHeight
  ) { throw 'real click DOM target center is outside the WebView viewport' }

  $clientRect = [StockDeskRealMouseInput+RECT]::new()
  if (-not [StockDeskRealMouseInput]::GetClientRect($hwnd, [ref]$clientRect)) {
    throw 'real click host client rectangle is unavailable'
  }
  $clientWidth = $clientRect.Right - $clientRect.Left
  $clientHeight = $clientRect.Bottom - $clientRect.Top
  if ($clientWidth -lt 2 -or $clientHeight -lt 2) { throw 'real click host client rectangle is unusable' }
  $scaleX = $clientWidth / $viewportWidth
  $scaleY = $clientHeight / $viewportHeight
  if (
    $scaleX -le 0 -or $scaleY -le 0 -or
    [Math]::Abs($scaleX - $scaleY) -gt 0.1 -or
    [Math]::Abs($scaleX - $devicePixelRatio) -gt 0.1
  ) {
    throw 'real click WebView-to-client scale is inconsistent'
  }

  $clientOrigin = [StockDeskRealMouseInput+POINT]::new()
  if (-not [StockDeskRealMouseInput]::ClientToScreen($hwnd, [ref]$clientOrigin)) {
    throw 'real click host client origin is unavailable'
  }
  $centerX = [int][Math]::Round($clientOrigin.X + ($centerCssX * $scaleX))
  $centerY = [int][Math]::Round($clientOrigin.Y + ($centerCssY * $scaleY))
  $nativePoint = [StockDeskRealMouseInput+POINT]::new()
  $nativePoint.X = $centerX
  $nativePoint.Y = $centerY
  $pointWindow = [StockDeskRealMouseInput]::WindowFromPoint($nativePoint)
  $pointRoot = if ($pointWindow -eq [IntPtr]::Zero) {
    [IntPtr]::Zero
  } else {
    [StockDeskRealMouseInput]::GetAncestor($pointWindow, [StockDeskRealMouseInput]::GA_ROOT)
  }
  if ($pointRoot -ne $hwnd) { throw 'real click DOM target point is outside the installed Stock Desk window' }

  return [pscustomobject]@{
    CenterX = $centerX
    CenterY = $centerY
    Evidence = [ordered]@{
      target_source = 'cdp-dom-bounds-host-client-transform'
      name = [string]$Target.name
      control_type = 'dom-button'
      bounding_rectangle_css = $Target.bounding_rectangle_css
      viewport_css = $Target.viewport_css
      device_pixel_ratio = $devicePixelRatio
      dom_hit_test = $Target.dom_hit_test
      host_client_rectangle = [ordered]@{ x=$clientOrigin.X; y=$clientOrigin.Y; width=$clientWidth; height=$clientHeight }
      host_client_scale = [ordered]@{ x=$scaleX; y=$scaleY }
      point_window_hwnd = [long]$pointWindow
      point_host_root_hwnd = [long]$pointRoot
    }
  }
}

function Invoke-DomPhysicalClick([string]$Action, [object]$Marker, [string]$ExpectedName) {
  $point = Get-DomTargetPoint $Marker.target $ExpectedName
  Invoke-PhysicalPointClick -Action $Action -CenterX $point.CenterX -CenterY $point.CenterY -TargetEvidence $point.Evidence
}

$process.Refresh()
if ($process.HasExited -or [long]$process.MainWindowHandle -ne $WindowHandle) {
  throw 'real click target process does not own the expected main HWND'
}
$actualPath = [IO.Path]::GetFullPath($process.MainModule.FileName)
$actualHash = (Get-FileHash -LiteralPath $actualPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -cne $ExpectedExecutableSha256) {
  throw 'real click target executable does not match the installed candidate'
}

$close = Find-Button -Names @('Close', '关闭') -AutomationIds @('Close') -RequireTitleBarAncestor
Invoke-PhysicalClick -Action 'titlebar-close-open-dialog' -Element $close
$cancelMarker = Wait-CaptureMarker 'os-real-click-cancel-target' 20 'packaged WebView did not publish the cancel click target'
if ($cancelMarker.phase -cne 'first-exit-dialog-visible') { throw 'cancel click target phase is invalid' }
$nativeCloseOpenedDialog = $cancelMarker.target.visible -eq $true
Invoke-DomPhysicalClick -Action 'cancel-exit-dialog' -Marker $cancelMarker -ExpectedName '取消'
Write-CaptureAck 'os-real-click-cancel-target'
$cancelObserved = Wait-CaptureMarker 'os-real-click-cancel-observed' 20 'packaged WebView did not observe the cancel click'
if (
  $cancelObserved.phase -cne 'cancel-click-observed' -or
  $cancelObserved.dialog_visible -ne $false
) { throw 'cancel click observation is invalid' }
Write-CaptureAck 'os-real-click-cancel-observed'
$process.Refresh()
if ($process.HasExited) { throw 'cancel click unexpectedly exited Stock Desk' }
$cancelKeptProcessAlive = -not $process.HasExited

$close = Find-Button -Names @('Close', '关闭') -AutomationIds @('Close') -RequireTitleBarAncestor
Invoke-PhysicalClick -Action 'titlebar-close-reopen-dialog' -Element $close
$confirmMarker = Wait-CaptureMarker 'os-real-click-confirm-target' 20 'packaged WebView did not publish the confirm click target'
if ($confirmMarker.phase -cne 'second-exit-dialog-visible') { throw 'confirm click target phase is invalid' }
$secondCloseReopenedDialog = $confirmMarker.target.visible -eq $true
Invoke-DomPhysicalClick -Action 'confirm-exit-dialog' -Marker $confirmMarker -ExpectedName '退出应用'
Write-CaptureAck 'os-real-click-confirm-target'
if (-not $process.WaitForExit(25000)) { throw 'physical exit click did not terminate Stock Desk' }
$process.Refresh()
$hostExitCode = [int]$process.ExitCode
if ($hostExitCode -ne 0) { throw "physical exit click returned non-zero host status: $hostExitCode" }

$parent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force $parent | Out-Null
$evidence = [ordered]@{
  schema_version = 'stock-desk-windows-real-click-evidence-v1'
  source_sha = $SourceSha
  source_tree = $SourceTree
  candidate_sha256 = $CandidateSha256
  installed_executable_sha256 = $actualHash
  process_id = $ExpectedProcessId
  main_window_handle = $WindowHandle
  input_method = 'win32-sendinput-physical-mouse'
  real_os_mouse_click = $true
  native_close_click_opened_dialog = $nativeCloseOpenedDialog
  cancel_click_kept_process_alive = $cancelKeptProcessAlive
  second_close_reopened_dialog = $secondCloseReopenedDialog
  exit_click_host_exit_code = $hostExitCode
  actions = $actions
  environment = [ordered]@{
    os_version = [Environment]::OSVersion.VersionString
    runner_image = [string]$env:ImageOS
    current_user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  }
  limitations = @(
    'GitHub-hosted execution currently uses Windows Server 2025 and is not a Windows 10 or Windows 11 desktop SKU.',
    'The hosted runner account is runneradmin; this is not_equivalent_to_standard_user_windows_10_or_11.',
    'This evidence proves real OS mouse input on the hosted interactive desktop, not UAC secure-desktop behavior.'
  )
}
$temporary = "$OutputPath.$([Guid]::NewGuid().ToString('N')).tmp"
[IO.File]::WriteAllText(
  $temporary,
  (($evidence | ConvertTo-Json -Depth 12) + [Environment]::NewLine),
  [Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $temporary -Destination $OutputPath -Force

exit 0
