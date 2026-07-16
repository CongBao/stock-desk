[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][long]$WindowHandle,
  [Parameter(Mandatory = $true)][int]$ExpectedProcessId,
  [Parameter(Mandatory = $true)][string]$ExpectedExecutableSha256,
  [Parameter(Mandatory = $true)][string]$SourceSha,
  [Parameter(Mandatory = $true)][string]$SourceTree,
  [Parameter(Mandatory = $true)][string]$CandidateSha256,
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

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName WindowsBase
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class StockDeskRealMouseInput {
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
}
'@

$hwnd = [IntPtr]$WindowHandle
$process = [Diagnostics.Process]::GetProcessById($ExpectedProcessId)
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
  [StockDeskRealMouseInput]::SetForegroundWindow($hwnd) | Out-Null
  Wait-Until {
    if ([StockDeskRealMouseInput]::GetForegroundWindow() -eq $hwnd) { return $true }
    [StockDeskRealMouseInput]::SetForegroundWindow($hwnd) | Out-Null
    return $false
  } 5 'installed Stock Desk could not become the foreground window' | Out-Null
}

function Invoke-PhysicalClick {
  param(
    [string]$Action,
    [System.Windows.Automation.AutomationElement]$Element
  )
  Focus-Window
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
  $actions.Add([ordered]@{
      action = $Action
      automation_id = $automationId
      name = $name
      control_type = $controlType
      runtime_id = $runtimeId
      bounding_rectangle = [ordered]@{ x=$rect.X; y=$rect.Y; width=$rect.Width; height=$rect.Height }
      center = [ordered]@{ x=$centerX; y=$centerY }
      from_point_runtime_id = $hitRuntimeId
      foreground_hwnd = [long][StockDeskRealMouseInput]::GetForegroundWindow()
      send_input_returned = [int]$sent
      captured_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    })
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
$cancel = Find-Button -Names @('取消')
$exit = Find-Button -Names @('退出应用')
$nativeCloseOpenedDialog = $null -ne $cancel -and $null -ne $exit

Invoke-PhysicalClick -Action 'cancel-exit-dialog' -Element $cancel
Wait-Until {
  $process.Refresh()
  if ($process.HasExited) { throw 'cancel click unexpectedly exited Stock Desk' }
  if ($null -eq (Find-Button -Names @('取消') -TimeoutSeconds 0 -Optional)) { return $true }
  return $false
} 10 'cancel click did not close the exit confirmation dialog' | Out-Null
$cancelKeptProcessAlive = -not $process.HasExited

$close = Find-Button -Names @('Close', '关闭') -AutomationIds @('Close') -RequireTitleBarAncestor
Invoke-PhysicalClick -Action 'titlebar-close-reopen-dialog' -Element $close
$exit = Find-Button -Names @('退出应用')
$secondCloseReopenedDialog = $null -ne $exit
Invoke-PhysicalClick -Action 'confirm-exit-dialog' -Element $exit
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
