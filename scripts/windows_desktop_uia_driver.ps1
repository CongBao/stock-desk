# Reviewed Windows UI Automation + Win32 driver for installed Stock Desk evidence.
#
# This script is copied into an external, ephemeral Windows VM by the protected
# broker.  It drives only the installed candidate HWND and writes observations;
# it never declares acceptance.  The Python aggregate verifier independently
# derives geometry, overlap, focus, timing, provider and DPI conclusions.

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][long]$WindowHandle,
  [Parameter(Mandatory = $true)][int]$ExpectedProcessId,
  [Parameter(Mandatory = $true)][string]$ExpectedExecutableSha256,
  [Parameter(Mandatory = $true)][ValidateSet(100, 125, 150, 175, 200)][int]$ExpectedDpiPercent,
  [Parameter(Mandatory = $true)][ValidateSet('primary', 'primary-blocked-fallback')][string]$DataPath,
  [Parameter(Mandatory = $true)][ValidateSet('akshare', 'baostock')][string]$ExpectedProvider,
  [Parameter(Mandatory = $true)][string]$NetworkObservationPath,
  [Parameter(Mandatory = $true)][string]$OutputRoot,
  [switch]$RuntimeProbe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName WindowsBase
Add-Type -AssemblyName System.Drawing

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class StockDeskDesktopEvidenceNative {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  [DllImport("user32.dll", SetLastError=true)] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  [DllImport("user32.dll", SetLastError=true)] public static extern uint GetDpiForWindow(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern uint GetDpiForSystem();
  [DllImport("user32.dll")] public static extern IntPtr GetWindowDpiAwarenessContext(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool AreDpiAwarenessContextsEqual(IntPtr left, IntPtr right);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool GetClientRect(IntPtr hwnd, out RECT rect);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool ClientToScreen(IntPtr hwnd, ref POINT point);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool SetForegroundWindow(IntPtr hwnd);
  [DllImport("user32.dll", SetLastError=true)] public static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
  [DllImport("user32.dll", SetLastError=true, EntryPoint="PrintWindow")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);
  [DllImport("shcore.dll")] public static extern int GetDpiForMonitor(IntPtr monitor, int type, out uint x, out uint y);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool LogicalToPhysicalPointForPerMonitorDPI(IntPtr hwnd, ref POINT point);
  [DllImport("user32.dll", SetLastError=true)] public static extern bool PhysicalToLogicalPointForPerMonitorDPI(IntPtr hwnd, ref POINT point);
  public static readonly IntPtr DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = new IntPtr(-4);
  public const uint SWP_NOZORDER = 0x0004;
  public const uint SWP_NOACTIVATE = 0x0010;
  public const uint MONITOR_DEFAULTTONEAREST = 2;
  public const uint GA_ROOT = 2;
}
'@

$root = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
New-Item -ItemType Directory -Path $root -Force | Out-Null
$actionPath = Join-Path $root 'uia-actions.json'
$treePath = Join-Path $root 'uia-tree.json'
$resultPath = Join-Path $root 'driver-result.json'
$focusRegionRoot = Join-Path $root 'focus-region-parts'
New-Item -ItemType Directory -Path $focusRegionRoot -Force | Out-Null
$script:Actions = [Collections.Generic.List[object]]::new()
$script:Trees = [Collections.Generic.List[object]]::new()
$script:FocusRegionCaptures = [Collections.Generic.List[object]]::new()
$script:FocusRegionSequence = 0
$script:ActionSequence = 0
$script:PrimaryClicks = 0
$script:KeyboardActivationCount = 0
$script:KeyboardMatrixCheckCount = 0
$script:EscapeBehaviorCheckCount = 0
$script:FocusObservationCount = 0
$script:OnboardingTabPaths = [Collections.Generic.List[object]]::new()
$script:AuxiliaryTabPaths = [Collections.Generic.List[object]]::new()
$started = [DateTimeOffset]::UtcNow
$hwnd = [IntPtr]$WindowHandle

function Write-Json {
  param([string]$Path, [object]$Value)
  [IO.File]::WriteAllText(
    $Path,
    (($Value | ConvertTo-Json -Depth 30) + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
  )
}

function Get-Sha256 {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Digest input is missing' }
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ElementId {
  param([System.Windows.Automation.AutomationElement]$Element)
  $id = [string]$Element.Current.AutomationId
  if ([string]::IsNullOrWhiteSpace($id)) {
    $name = ([string]$Element.Current.Name).Trim()
    $kind = [string]$Element.Current.ControlType.ProgrammaticName
    $id = "$kind`:$name"
  }
  $runtime = @($Element.GetRuntimeId()) -join '.'
  if ($id.Length -gt 88) { $id = $id.Substring(0, 88) }
  return "$id#$runtime"
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
  if ($null -eq $element) { throw 'Installed candidate HWND is not exposed to UI Automation' }
  if ($element.Current.ProcessId -ne $ExpectedProcessId) { throw 'UI Automation root belongs to another process' }
  return $element
}

function Find-Element {
  param(
    [string[]]$Names,
    [System.Windows.Automation.ControlType]$ControlType,
    [int]$TimeoutSeconds = 30,
    [switch]$Optional
  )
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
  do {
    $rootElement = Get-RootElement
    $all = $rootElement.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($candidate in $all) {
      if ($candidate.Current.ControlType -ne $ControlType) { continue }
      $name = ([string]$candidate.Current.Name).Trim()
      if ($Names -contains $name) { return $candidate }
    }
    Start-Sleep -Milliseconds 250
  } while ([DateTimeOffset]::UtcNow -lt $deadline)
  if ($Optional) { return $null }
  throw "UI Automation target was not found: $($Names -join ' / ')"
}

function Find-TopLevelWindow {
  param([string]$Name, [int]$TimeoutSeconds = 10, [switch]$Optional)
  $condition = [System.Windows.Automation.AndCondition]::new(
    [System.Windows.Automation.Condition[]]@(
      [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ExpectedProcessId
      ),
      [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Window
      ),
      [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $Name
      )
    )
  )
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
  do {
    $candidate = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
      [System.Windows.Automation.TreeScope]::Descendants,
      $condition
    )
    if ($null -ne $candidate) {
      $candidateHwnd = [IntPtr][long]$candidate.Current.NativeWindowHandle
      if (
        $candidateHwnd -ne [IntPtr]::Zero -and
        [StockDeskDesktopEvidenceNative]::GetAncestor(
          $candidateHwnd,
          [StockDeskDesktopEvidenceNative]::GA_ROOT
        ) -eq $candidateHwnd
      ) { return $candidate }
    }
    Start-Sleep -Milliseconds 100
  } while ([DateTimeOffset]::UtcNow -lt $deadline)
  if ($Optional) { return $null }
  throw "Top-level UIA window was not found: $Name"
}

function Add-Action {
  param(
    [string]$Kind,
    [System.Windows.Automation.AutomationElement]$Element,
    [bool]$MajorClick,
    [string]$Outcome
  )
  $script:ActionSequence += 1
  if ($MajorClick) { $script:PrimaryClicks += 1 }
  $script:Actions.Add([ordered]@{
      sequence = $script:ActionSequence
      captured_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
      action = $Kind
      target_id = Get-ElementId -Element $Element
      target_name = [string]$Element.Current.Name
      target_control_type = [string]$Element.Current.ControlType.ProgrammaticName
      major_click = $MajorClick
      outcome = $Outcome
    })
}

function Invoke-Element {
  param(
    [System.Windows.Automation.AutomationElement]$Element,
    [bool]$MajorClick = $true
  )
  if (-not $Element.Current.IsEnabled -or $Element.Current.IsOffscreen) {
    throw 'UI Automation target is disabled or offscreen'
  }
  $pattern = $null
  if (-not $Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
    throw 'UI Automation target has no InvokePattern'
  }
  ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
  Add-Action -Kind 'invoke' -Element $Element -MajorClick $MajorClick -Outcome 'invoked'
}

function Save-ElementFocusRegion {
  param([System.Windows.Automation.AutomationElement]$Element)
  $windowRect = [StockDeskDesktopEvidenceNative+RECT]::new()
  if (-not [StockDeskDesktopEvidenceNative]::GetWindowRect($hwnd, [ref]$windowRect)) {
    throw 'GetWindowRect failed for focus-region observation'
  }
  $elementRect = $Element.Current.BoundingRectangle
  $windowWidth = $windowRect.Right - $windowRect.Left
  $windowHeight = $windowRect.Bottom - $windowRect.Top
  $left = [Math]::Max(0, [int][Math]::Floor($elementRect.X - $windowRect.Left) - 4)
  $top = [Math]::Max(0, [int][Math]::Floor($elementRect.Y - $windowRect.Top) - 4)
  $right = [Math]::Min($windowWidth, [int][Math]::Ceiling($elementRect.Right - $windowRect.Left) + 4)
  $bottom = [Math]::Min($windowHeight, [int][Math]::Ceiling($elementRect.Bottom - $windowRect.Top) + 4)
  if ($right - $left -lt 2 -or $bottom - $top -lt 2) {
    throw 'Focused UIA target has no capturable pixel region'
  }
  $bitmap = [Drawing.Bitmap]::new($windowWidth, $windowHeight, [Drawing.Imaging.PixelFormat]::Format24bppRgb)
  $graphics = [Drawing.Graphics]::FromImage($bitmap)
  $dc = $graphics.GetHdc()
  try {
    if (-not [StockDeskDesktopEvidenceNative]::PrintWindow($hwnd, $dc, 2)) {
      throw 'PrintWindow failed for focus-region observation'
    }
  } finally {
    $graphics.ReleaseHdc($dc)
    $graphics.Dispose()
  }
  $region = $null
  try {
    $region = $bitmap.Clone(
      [Drawing.Rectangle]::new($left, $top, $right - $left, $bottom - $top),
      [Drawing.Imaging.PixelFormat]::Format24bppRgb
    )
    $script:FocusRegionSequence += 1
    $captureId = "focus-region-$($script:FocusRegionSequence.ToString('D3'))"
    $capturePath = Join-Path $focusRegionRoot "$captureId.png"
    $region.Save($capturePath, [Drawing.Imaging.ImageFormat]::Png)
    $capture = [ordered]@{
      id = $captureId
      path = $capturePath
      width = $region.Width
      height = $region.Height
    }
    $script:FocusRegionCaptures.Add($capture)
    return $capture
  } finally {
    if ($null -ne $region) { $region.Dispose() }
    $bitmap.Dispose()
  }
}

function Write-FocusRegionContactSheet {
  param([string]$Path)
  if ($script:FocusRegionCaptures.Count -lt 2) { throw 'Focus-region evidence is incomplete' }
  $sheetWidth = 0
  $sheetHeight = [long]0
  foreach ($capture in $script:FocusRegionCaptures) {
    $captureWidth = [int]$capture.width
    $captureHeight = [int]$capture.height
    if (
      $captureWidth -lt 2 -or $captureWidth -gt 2048 -or
      $captureHeight -lt 2 -or $captureHeight -gt 2048
    ) { throw 'Focus-region capture exceeds its closed bounds' }
    $sheetWidth = [Math]::Max($sheetWidth, $captureWidth)
    $sheetHeight += [long]$captureHeight
    if ($sheetHeight -gt 32768) { throw 'Focus-region contact sheet exceeds its closed bounds' }
  }
  if ($sheetWidth -lt 2 -or $sheetHeight -lt 4) { throw 'Focus-region evidence is incomplete' }
  $sheet = [Drawing.Bitmap]::new($sheetWidth, [int]$sheetHeight, [Drawing.Imaging.PixelFormat]::Format24bppRgb)
  $graphics = [Drawing.Graphics]::FromImage($sheet)
  $graphics.Clear([Drawing.Color]::Black)
  $entries = [Collections.Generic.List[object]]::new()
  $offsetY = 0
  try {
    foreach ($capture in $script:FocusRegionCaptures) {
      $part = [Drawing.Image]::FromFile([string]$capture.path)
      try { $graphics.DrawImageUnscaled($part, 0, $offsetY) } finally { $part.Dispose() }
      $entries.Add([ordered]@{
          id = [string]$capture.id
          x = 0
          y = $offsetY
          width = [int]$capture.width
          height = [int]$capture.height
        })
      $offsetY += [int]$capture.height
    }
  } finally { $graphics.Dispose() }
  try { $sheet.Save($Path, [Drawing.Imaging.ImageFormat]::Png) } finally { $sheet.Dispose() }
  Remove-Item -LiteralPath $focusRegionRoot -Recurse -Force -ErrorAction Stop
  if (Test-Path -LiteralPath $focusRegionRoot) {
    throw 'focus-region scratch captures were not removed'
  }
  return [ordered]@{
    schema = 'stock-desk-focus-region-contact-sheet-v1'
    media_kind = 'focus-region-contact-sheet'
    width = $sheetWidth
    height = $sheetHeight
    captures = @($entries)
  }
}

function Move-FocusToElementByTab {
  param([System.Windows.Automation.AutomationElement]$Element)
  if (-not $Element.Current.IsEnabled -or $Element.Current.IsOffscreen -or -not $Element.Current.IsKeyboardFocusable) {
    throw 'Keyboard target is disabled, offscreen, or not focusable'
  }
  if (-not [StockDeskDesktopEvidenceNative]::SetForegroundWindow($hwnd)) {
    throw 'Candidate window could not become the foreground keyboard target'
  }
  Start-Sleep -Milliseconds 100
  $targetId = Get-ElementId -Element $Element
  $initial = [System.Windows.Automation.AutomationElement]::FocusedElement
  $initialId = if ($null -ne $initial -and $initial.Current.ProcessId -eq $ExpectedProcessId) {
    Get-ElementId -Element $initial
  } else { 'none-or-external' }
  $tabSequence = [Collections.Generic.List[string]]::new()
  if ($initialId -eq $targetId) {
    Send-Key -Keys '{TAB}'
    $moved = [System.Windows.Automation.AutomationElement]::FocusedElement
    if (
      $null -eq $moved -or $moved.Current.ProcessId -ne $ExpectedProcessId -or
      (Get-ElementId -Element $moved) -eq $targetId
    ) {
      throw 'Real Tab input did not move focus away before focus-indicator capture'
    }
    $tabSequence.Add((Get-ElementId -Element $moved))
  }
  $unfocusedRegion = Save-ElementFocusRegion -Element $Element
  for ($tabIndex = 1; $tabIndex -le 128; $tabIndex += 1) {
    Send-Key -Keys '{TAB}'
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $focused -or $focused.Current.ProcessId -ne $ExpectedProcessId) {
      throw 'Real Tab input moved focus outside the installed candidate'
    }
    $focusedId = Get-ElementId -Element $focused
    $tabSequence.Add($focusedId)
    if ($focusedId -eq $targetId) {
      if (-not $focused.Current.HasKeyboardFocus) {
        throw 'UI Automation did not observe keyboard focus after real Tab input'
      }
      $focusedRegion = Save-ElementFocusRegion -Element $Element
      $focusRegionChanged = (Get-Sha256 -Path $focusedRegion.path) -cne (Get-Sha256 -Path $unfocusedRegion.path)
      if (-not $focusRegionChanged) {
        throw 'Real Tab focus did not produce a visible target-region pixel change'
      }
      $script:FocusObservationCount += 1
      return [ordered]@{
        target_id = $targetId
        target_name = [string]$Element.Current.Name
        initial_focus_id = $initialId
        tab_sequence = @($tabSequence)
        tab_input_count = $tabSequence.Count
        focus_observation_method = 'uia-focused-element-after-real-tab'
        target_has_keyboard_focus = $true
        unfocused_region_id = [string]$unfocusedRegion.id
        focused_region_id = [string]$focusedRegion.id
        focus_region_changed = [bool]$focusRegionChanged
      }
    }
  }
  throw 'Real Tab navigation did not reach the required keyboard target'
}

function Invoke-ElementByKeyboard {
  param(
    [System.Windows.Automation.AutomationElement]$Element,
    [bool]$MajorClick = $true
  )
  $focusEvidence = Move-FocusToElementByTab -Element $Element
  Send-Key -Keys '{ENTER}'
  $script:KeyboardActivationCount += 1
  Add-Action -Kind 'keyboard-enter' -Element $Element -MajorClick $MajorClick -Outcome 'activated'
  $focusEvidence['activated'] = $true
  if ($MajorClick) {
    $script:OnboardingTabPaths.Add($focusEvidence)
  } else { $script:AuxiliaryTabPaths.Add($focusEvidence) }
}

function Send-Key {
  param([string]$Keys)
  [System.Windows.Forms.SendKeys]::SendWait($Keys)
  Start-Sleep -Milliseconds 150
}

function Set-LogicalWindowSize {
  param([int]$Width, [int]$Height)
  $dpi = [StockDeskDesktopEvidenceNative]::GetDpiForWindow($hwnd)
  if ($dpi -eq 0) { throw 'GetDpiForWindow failed' }
  $physicalWidth = [int][Math]::Round($Width * $dpi / 96.0)
  $physicalHeight = [int][Math]::Round($Height * $dpi / 96.0)
  if (-not [StockDeskDesktopEvidenceNative]::SetWindowPos(
      $hwnd, [IntPtr]::Zero, 0, 0, $physicalWidth, $physicalHeight,
      [StockDeskDesktopEvidenceNative]::SWP_NOZORDER
    )) { throw 'SetWindowPos failed' }
  Start-Sleep -Milliseconds 500
}

function Save-TargetWindowCapture {
  param([string]$Path)
  $rect = [StockDeskDesktopEvidenceNative+RECT]::new()
  if (-not [StockDeskDesktopEvidenceNative]::GetWindowRect($hwnd, [ref]$rect)) { throw 'GetWindowRect failed for capture' }
  $width = $rect.Right - $rect.Left; $height = $rect.Bottom - $rect.Top
  if ($width -lt 320 -or $height -lt 180) { throw 'Target window is too small for capture' }
  $bitmap = [Drawing.Bitmap]::new($width, $height, [Drawing.Imaging.PixelFormat]::Format24bppRgb)
  $graphics = [Drawing.Graphics]::FromImage($bitmap)
  $dc = $graphics.GetHdc()
  try {
    if (-not [StockDeskDesktopEvidenceNative]::PrintWindow($hwnd, $dc, 2)) { throw 'PrintWindow failed for target-only capture' }
  } finally {
    $graphics.ReleaseHdc($dc); $graphics.Dispose()
  }
  try { $bitmap.Save($Path, [Drawing.Imaging.ImageFormat]::Png) } finally { $bitmap.Dispose() }
}

function Get-HitElementId {
  param(
    [System.Windows.Automation.AutomationElement]$Expected,
    [double]$X,
    [double]$Y
  )
  $point = [Windows.Point]::new($X, $Y)
  $hit = [System.Windows.Automation.AutomationElement]::FromPoint($point)
  while ($null -ne $hit) {
    if ($hit -eq $Expected) { return Get-ElementId -Element $Expected }
    $hit = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($hit)
  }
  return if ($null -eq $hit) { 'occluded-or-unrelated' } else { Get-ElementId -Element $hit }
}

function Get-InteractiveComponents {
  param([System.Windows.Automation.AutomationElement]$Scope)
  $allowed = @(
    [System.Windows.Automation.ControlType]::Button,
    [System.Windows.Automation.ControlType]::Edit,
    [System.Windows.Automation.ControlType]::ComboBox,
    [System.Windows.Automation.ControlType]::ListItem,
    [System.Windows.Automation.ControlType]::MenuItem,
    [System.Windows.Automation.ControlType]::Hyperlink,
    [System.Windows.Automation.ControlType]::RadioButton,
    [System.Windows.Automation.ControlType]::CheckBox
  )
  $elements = @($Scope.FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    ) | Where-Object {
      $allowed -contains $_.Current.ControlType -and
      -not $_.Current.IsOffscreen -and $_.Current.BoundingRectangle.Width -gt 1 -and
      $_.Current.BoundingRectangle.Height -gt 1
    })
  $results = [Collections.Generic.List[object]]::new()
  foreach ($element in $elements) {
    $rect = $element.Current.BoundingRectangle
    $parent = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($element)
    $id = Get-ElementId -Element $element
    $results.Add([ordered]@{
        id = $id
        parent_id = if ($null -eq $parent -or $parent -eq $Scope) { $null } else { Get-ElementId -Element $parent }
        x = [int][Math]::Round($rect.X)
        y = [int][Math]::Round($rect.Y)
        width = [int][Math]::Round($rect.Width)
        height = [int][Math]::Round($rect.Height)
        is_offscreen = [bool]$element.Current.IsOffscreen
        is_enabled = [bool]$element.Current.IsEnabled
        keyboard_focusable = [bool]$element.Current.IsKeyboardFocusable
        hit_test_id = Get-HitElementId -Expected $element -X ($rect.X + $rect.Width / 2) -Y ($rect.Y + $rect.Height / 2)
      })
  }
  return @($results)
}

function Get-LayoutCheck {
  param(
    [string]$Identity,
    [int]$Width,
    [int]$Height,
    [System.Windows.Automation.AutomationElement]$Scope,
    [string]$EscapeResult = 'closed-safe'
  )
  Set-LogicalWindowSize -Width $Width -Height $Height
  $windowRect = [StockDeskDesktopEvidenceNative+RECT]::new()
  if (-not [StockDeskDesktopEvidenceNative]::GetWindowRect($hwnd, [ref]$windowRect)) { throw 'GetWindowRect failed' }
  $components = @(Get-InteractiveComponents -Scope $Scope)
  if ($components.Count -lt 1) { throw "No visible interactive components for $Identity" }
  $focusable = @($components | Where-Object { $_.keyboard_focusable } | Sort-Object y, x, id)
  if ($focusable.Count -lt 1) { throw "No keyboard focusable components for $Identity" }
  $visualSequence = @($focusable | ForEach-Object { $_.id })
  $firstElement = $Scope.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
  ) | Where-Object { (Get-ElementId -Element $_) -eq $visualSequence[0] } | Select-Object -First 1
  if ($null -eq $firstElement) { throw "Cannot focus first component for $Identity" }
  $focusEvidence = Move-FocusToElementByTab -Element $firstElement
  $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
  if ($null -eq $focused) { throw "No focused element for $Identity" }
  $focusedElementId = Get-ElementId -Element $focused
  if (
    $focusedElementId -ne $visualSequence[0] -or
    $focusEvidence.focus_observation_method -ne 'uia-focused-element-after-real-tab' -or
    -not $focusEvidence.target_has_keyboard_focus
  ) {
    throw "The first visual control was not reached and observed through real Tab input for $Identity"
  }
  $focusVisible = (
    $focusEvidence.focus_region_changed -and
    $focusEvidence.focused_region_id -cne $focusEvidence.unfocused_region_id
  )
  $actualTabSequence = [Collections.Generic.List[string]]::new()
  $actualTabSequence.Add($focusedElementId)
  for ($tabIndex = 1; $tabIndex -lt $visualSequence.Count; $tabIndex += 1) {
    Send-Key -Keys '{TAB}'
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $focused) { throw "Tab navigation lost focus for $Identity" }
    $actualTabSequence.Add((Get-ElementId -Element $focused))
  }
  if ((@($actualTabSequence) | ConvertTo-Json -Compress) -cne ($visualSequence | ConvertTo-Json -Compress)) {
    throw "Observed Tab order differs from the visual control order for $Identity"
  }
  $script:KeyboardMatrixCheckCount += 1
  $clippedCount = 0
  foreach ($component in $components) {
    if (
      $component.x -lt $windowRect.Left -or $component.y -lt $windowRect.Top -or
      ($component.x + $component.width) -gt $windowRect.Right -or
      ($component.y + $component.height) -gt $windowRect.Bottom
    ) { $clippedCount += 1 }
  }
  $overlapCount = 0
  for ($leftIndex = 0; $leftIndex -lt $components.Count; $leftIndex += 1) {
    for ($rightIndex = $leftIndex + 1; $rightIndex -lt $components.Count; $rightIndex += 1) {
      $left = $components[$leftIndex]; $right = $components[$rightIndex]
      if ($left.parent_id -ne $right.parent_id) { continue }
      $horizontal = [Math]::Min($left.x + $left.width, $right.x + $right.width) - [Math]::Max($left.x, $right.x)
      $vertical = [Math]::Min($left.y + $left.height, $right.y + $right.height) - [Math]::Max($left.y, $right.y)
      if ($horizontal -gt 1 -and $vertical -gt 1) { $overlapCount += 1 }
    }
  }
  $check = [ordered]@{
    logical_size = [ordered]@{ width = $Width; height = $Height }
    window_bounds = [ordered]@{
      x = $windowRect.Left; y = $windowRect.Top
      width = $windowRect.Right - $windowRect.Left
      height = $windowRect.Bottom - $windowRect.Top
    }
    component_bounds = $components
    clipped_component_count = $clippedCount
    overlap_count = $overlapCount
    tab_sequence = @($actualTabSequence)
    focused_element_id = $focusedElementId
    focus_visible = [bool]$focusVisible
    focus_evidence = $focusEvidence
    escape_result = $EscapeResult
  }
  $script:Trees.Add([ordered]@{ identity = $Identity; check = $check })
  return $check
}

function Open-Route {
  param([string[]]$Names)
  $link = Find-Element -Names $Names -ControlType ([System.Windows.Automation.ControlType]::Hyperlink)
  Invoke-ElementByKeyboard -Element $link -MajorClick $false
  Start-Sleep -Milliseconds 500
  return Get-RootElement
}

function Capture-Route {
  param([string]$Id, [string[]]$Names)
  $scope = Open-Route -Names $Names
  $standard = Get-LayoutCheck -Identity "route:$Id`:standard" -Width 1366 -Height 768 -Scope $scope
  if ($Id -eq 'market') { Save-TargetWindowCapture -Path (Join-Path $root 'window-standard.png') }
  $narrow = Get-LayoutCheck -Identity "route:$Id`:narrow" -Width 640 -Height 360 -Scope $scope
  if ($Id -eq 'market') { Save-TargetWindowCapture -Path (Join-Path $root 'window-narrow.png') }
  return [ordered]@{ id = $Id; checks = @($standard, $narrow) }
}

if ($RuntimeProbe) {
  $ownerPid = [uint32]0
  if (
    [StockDeskDesktopEvidenceNative]::GetWindowThreadProcessId($hwnd, [ref]$ownerPid) -eq 0 -or
    [int]$ownerPid -ne $ExpectedProcessId
  ) { throw 'Runtime probe HWND is not owned by the expected process' }
  $probeProcess = Get-Process -Id $ExpectedProcessId -ErrorAction Stop
  if ((Get-Sha256 -Path $probeProcess.Path) -cne $ExpectedExecutableSha256) {
    throw 'Runtime probe executable digest mismatch'
  }
  $probeRoot = Get-RootElement
  $probeTarget = Find-Element -Names @('Runtime probe target') -ControlType ([System.Windows.Automation.ControlType]::Button) -TimeoutSeconds 10
  Invoke-ElementByKeyboard -Element $probeTarget
  $activated = Find-Element -Names @('Runtime probe activated') -ControlType ([System.Windows.Automation.ControlType]::Button) -TimeoutSeconds 10
  if ($null -eq $activated -or $script:OnboardingTabPaths.Count -ne 1) {
    throw 'Runtime probe did not activate the controlled UIA target through the keyboard'
  }
  $probeDialog = Find-TopLevelWindow -Name 'Runtime probe dialog' -TimeoutSeconds 10
  Send-Key -Keys '{ESC}'
  $remainingProbeDialog = Find-TopLevelWindow -Name 'Runtime probe dialog' -TimeoutSeconds 2 -Optional
  $escapeClosed = $null -eq $remainingProbeDialog -or $remainingProbeDialog.Current.IsOffscreen
  if (-not $escapeClosed) { throw 'Runtime probe real Esc input did not close the controlled dialog' }
  $probeCapture = Join-Path $root 'runtime-probe-window.png'
  Save-TargetWindowCapture -Path $probeCapture
  $focusContactSheet = Join-Path $root 'focus-region-contact-sheet.png'
  $focusRegionManifest = Write-FocusRegionContactSheet -Path $focusContactSheet
  $probeContext = [StockDeskDesktopEvidenceNative]::GetWindowDpiAwarenessContext($hwnd)
  if ($probeContext -eq [IntPtr]::Zero) { throw 'Runtime probe target HWND has no DPI awareness context' }
  Write-Json -Path $actionPath -Value @($script:Actions)
  Write-Json -Path $treePath -Value @()
  Write-Json -Path $resultPath -Value ([ordered]@{
      schema = 'stock-desk-windows-uia-driver-runtime-probe-v1'
      raw_only = $true
      real_vm_acceptance = $false
      driver_sha256 = Get-Sha256 -Path $PSCommandPath
      candidate = [ordered]@{ pid = $ExpectedProcessId; hwnd = $WindowHandle; executable_sha256 = $ExpectedExecutableSha256 }
      target_hwnd_dpi_context_observed = $probeContext -ne [IntPtr]::Zero
      actual_tab_activation_observed = $script:OnboardingTabPaths.Count -eq 1 -and $script:KeyboardActivationCount -eq 1
      actual_escape_close_observed = [bool]$escapeClosed
      focus_observation_method = 'uia-focused-element-after-real-tab'
      focus_path = $script:OnboardingTabPaths[0]
      focus_regions = $focusRegionManifest
      focus_region_contact_sheet_sha256 = Get-Sha256 -Path $focusContactSheet
      target_window_capture_sha256 = Get-Sha256 -Path $probeCapture
    })
  exit 0
}

function Capture-Dialog {
  param(
    [string]$Id,
    [scriptblock]$Open,
    [string[]]$DialogNames,
    [string[]]$CloseNames,
    [string]$EscapeResult = 'closed-safe'
  )
  $checks = [Collections.Generic.List[object]]::new()
  foreach ($size in @(@(1366, 768), @(640, 360))) {
    Set-LogicalWindowSize -Width $size[0] -Height $size[1]
    & $Open
    $dialog = Find-Element -Names $DialogNames -ControlType ([System.Windows.Automation.ControlType]::Window) -TimeoutSeconds 20 -Optional
    if ($null -eq $dialog) {
      $dialog = Find-Element -Names $DialogNames -ControlType ([System.Windows.Automation.ControlType]::Pane) -TimeoutSeconds 5
    }
    $check = Get-LayoutCheck -Identity "dialog:$Id`:$($size[0])" -Width $size[0] -Height $size[1] -Scope $dialog -EscapeResult $EscapeResult
    Send-Key -Keys '{ESC}'
    Start-Sleep -Milliseconds 250
    $remaining = Find-Element -Names $DialogNames -ControlType ([System.Windows.Automation.ControlType]::Window) -TimeoutSeconds 1 -Optional
    if ($null -eq $remaining) {
      $remaining = Find-Element -Names $DialogNames -ControlType ([System.Windows.Automation.ControlType]::Pane) -TimeoutSeconds 1 -Optional
    }
    if ($EscapeResult -eq 'closed-safe') {
      if ($null -ne $remaining -and -not $remaining.Current.IsOffscreen) { throw "Escape did not safely close $Id" }
    } else {
      if ($null -eq $remaining -or $remaining.Current.IsOffscreen) { throw "Escape bypassed the required confirmation for $Id" }
      $close = Find-Element -Names $CloseNames -ControlType ([System.Windows.Automation.ControlType]::Button) -TimeoutSeconds 5
      Invoke-ElementByKeyboard -Element $close -MajorClick $false
    }
    $script:EscapeBehaviorCheckCount += 1
    $checks.Add($check)
  }
  return [ordered]@{ id = $Id; checks = @($checks) }
}

# Bind the driver to the exact installed candidate process and binary.
$process = Get-Process -Id $ExpectedProcessId -ErrorAction Stop
if ([long]$process.MainWindowHandle -ne $WindowHandle) { throw 'Candidate process does not own the requested HWND' }
if ((Get-Sha256 -Path $process.Path) -cne $ExpectedExecutableSha256) { throw 'Candidate executable digest mismatch' }
$rootElement = Get-RootElement

# Record real DPI APIs and a logical/physical coordinate round trip.
$windowDpi = [StockDeskDesktopEvidenceNative]::GetDpiForWindow($hwnd)
$systemDpi = [StockDeskDesktopEvidenceNative]::GetDpiForSystem()
$monitor = [StockDeskDesktopEvidenceNative]::MonitorFromWindow($hwnd, [StockDeskDesktopEvidenceNative]::MONITOR_DEFAULTTONEAREST)
$monitorX = [uint32]0; $monitorY = [uint32]0
if ([StockDeskDesktopEvidenceNative]::GetDpiForMonitor($monitor, 0, [ref]$monitorX, [ref]$monitorY) -ne 0) { throw 'GetDpiForMonitor failed' }
$logical = [StockDeskDesktopEvidenceNative+POINT]::new(); $logical.X = 37; $logical.Y = 53
$physical = $logical
if (-not [StockDeskDesktopEvidenceNative]::LogicalToPhysicalPointForPerMonitorDPI($hwnd, [ref]$physical)) { throw 'LogicalToPhysicalPointForPerMonitorDPI failed' }
$roundtrip = $physical
if (-not [StockDeskDesktopEvidenceNative]::PhysicalToLogicalPointForPerMonitorDPI($hwnd, [ref]$roundtrip)) { throw 'PhysicalToLogicalPointForPerMonitorDPI failed' }
$roundtripError = [Math]::Max([Math]::Abs($roundtrip.X - $logical.X), [Math]::Abs($roundtrip.Y - $logical.Y))
$windowContext = [StockDeskDesktopEvidenceNative]::GetWindowDpiAwarenessContext($hwnd)
$isPerMonitorV2 = [StockDeskDesktopEvidenceNative]::AreDpiAwarenessContextsEqual(
  $windowContext, [StockDeskDesktopEvidenceNative]::DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
)
$expectedDpi = [int]($ExpectedDpiPercent * 96 / 100)
if ($windowDpi -ne $expectedDpi -or $systemDpi -ne $expectedDpi -or $monitorX -ne $expectedDpi -or $monitorY -ne $expectedDpi -or -not $isPerMonitorV2 -or $roundtripError -gt 1) {
  throw 'Assigned scale is not proven by non-virtualized Win32 DPI APIs'
}

# Four-step first-use journey.  Provider blocking is performed by the protected
# VM network policy before the catalog probe, so both normal and fallback paths
# remain within the same four primary invocations.
$startButton = Find-Element -Names @('开始设置') -ControlType ([System.Windows.Automation.ControlType]::Button) -TimeoutSeconds 45
Invoke-ElementByKeyboard -Element $startButton
$sourceButton = Find-Element -Names @('继续') -ControlType ([System.Windows.Automation.ControlType]::Button) -TimeoutSeconds 60
Invoke-ElementByKeyboard -Element $sourceButton
$syncButton = Find-Element -Names @('准备并继续') -ControlType ([System.Windows.Automation.ControlType]::Button) -TimeoutSeconds 30
Invoke-ElementByKeyboard -Element $syncButton
$enterButton = Find-Element -Names @('打开行情') -ControlType ([System.Windows.Automation.ControlType]::Button) -TimeoutSeconds 120
$synchronizationText = @((Get-RootElement).FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
  ) | ForEach-Object { ([string]$_.Current.Name).Trim() } | Where-Object { $_ })
$rowText = $synchronizationText | Where-Object { $_ -match '^[0-9,]+ 条$' } | Select-Object -First 1
$cutoffText = $synchronizationText | Where-Object { $_ -match '^20[0-9]{2}' } | Select-Object -First 1
if ($synchronizationText -notcontains '上证指数' -or $synchronizationText -notcontains '000001.SS' -or $null -eq $rowText -or $null -eq $cutoffText) {
  throw 'Synchronization summary does not prove canonical real Shanghai Composite data'
}
$rowCount = [int](($rowText -replace '[^0-9]', ''))
if ($rowCount -lt 1) { throw 'Synchronization summary has no real daily bars' }
Invoke-ElementByKeyboard -Element $enterButton
$marketReady = Find-Element -Names @('行情') -ControlType ([System.Windows.Automation.ControlType]::Hyperlink) -TimeoutSeconds 30
$marketText = @((Get-RootElement).FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
  ) | ForEach-Object { ([string]$_.Current.Name).Trim() } | Where-Object { $_ })
if ($marketText -notcontains '000001.SS' -or $marketText -contains '000001.SZ' -or $marketText -match '只读演示') {
  throw 'First workspace is not the real canonical 000001.SS market chart'
}
$completed = [DateTimeOffset]::UtcNow
$elapsed = ($completed - $started).TotalSeconds
if ($elapsed -gt 180 -or $script:PrimaryClicks -gt 5) { throw 'First real K-line exceeded its time or click budget' }
$networkDeadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
while (-not (Test-Path -LiteralPath $NetworkObservationPath -PathType Leaf) -and [DateTimeOffset]::UtcNow -lt $networkDeadline) {
  Start-Sleep -Milliseconds 250
}
if (-not (Test-Path -LiteralPath $NetworkObservationPath -PathType Leaf)) { throw 'Protected network observation is missing' }
$networkObservation = Get-Content -LiteralPath $NetworkObservationPath -Raw | ConvertFrom-Json
$providerRecords = @($networkObservation.records | Where-Object {
    $_.provider -eq $ExpectedProvider -and $_.outcome -eq 'success' -and $_.operation -in @('catalog', 'daily-bars')
  })
$catalogRecord = @($providerRecords | Where-Object { $_.operation -eq 'catalog' })
$barsRecord = @($providerRecords | Where-Object { $_.operation -eq 'daily-bars' })
if ($catalogRecord.Count -ne 1 -or $barsRecord.Count -ne 1) { throw 'Protected network observation lacks one provider catalog/bars segment' }
if ($catalogRecord[0].payload_sha256 -cnotmatch '^[0-9a-f]{64}$' -or $barsRecord[0].payload_sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Provider payload digests are invalid' }
if ([int]$barsRecord[0].row_count -ne $rowCount) { throw 'Visible row count differs from protected provider capture' }

$routes = @(
  (Capture-Route -Id 'market' -Names @('行情')),
  (Capture-Route -Id 'formula' -Names @('自定义公式')),
  (Capture-Route -Id 'backtest' -Names @('策略回测')),
  (Capture-Route -Id 'analysis' -Names @('智能分析')),
  (Capture-Route -Id 'tasks' -Names @('任务中心')),
  (Capture-Route -Id 'settings' -Names @('设置'))
)

# Capture every release-relevant dialog.  The protected VM prepares immutable
# update/recovery fixtures; an absent opener is a hard failure, never a skip.
$dialogs = @(
  (Capture-Dialog -Id 'about' -Open {
      Invoke-Element -Element (Find-Element -Names @('关于 stock-desk') -ControlType ([System.Windows.Automation.ControlType]::Button)) -MajorClick $false
    } -DialogNames @('关于 stock-desk') -CloseNames @('关闭关于信息')),
  (Capture-Dialog -Id 'exit-confirmation' -Open { Send-Key -Keys '%{F4}' } -DialogNames @('确认退出 Stock Desk？') -CloseNames @('取消')),
  (Capture-Dialog -Id 'update-confirmation' -Open {
      Invoke-Element -Element (Find-Element -Names @('安装更新', '查看更新') -ControlType ([System.Windows.Automation.ControlType]::Button)) -MajorClick $false
    } -DialogNames @('确认安装更新') -CloseNames @('暂不安装')),
  (Capture-Dialog -Id 'sidecar-recovery' -Open {
      Invoke-Element -Element (Find-Element -Names @('打开恢复验证', '模拟服务中断') -ControlType ([System.Windows.Automation.ControlType]::Button)) -MajorClick $false
    } -DialogNames @('本地服务需要恢复', '发现上次未完成的任务') -CloseNames @('安全退出', '取消未完成任务')),
  (Capture-Dialog -Id 'model-settings' -Open {
      Open-Route -Names @('智能分析') | Out-Null
      Invoke-Element -Element (Find-Element -Names @('模型设置') -ControlType ([System.Windows.Automation.ControlType]::Button)) -MajorClick $false
    } -DialogNames @('模型设置') -CloseNames @('关闭模型设置')),
  (Capture-Dialog -Id 'market-pool' -Open {
      Open-Route -Names @('行情') | Out-Null
      Invoke-Element -Element (Find-Element -Names @('选择或管理股票池') -ControlType ([System.Windows.Automation.ControlType]::Button)) -MajorClick $false
    } -DialogNames @('选择或管理股票池') -CloseNames @('关闭股票池')),
  (Capture-Dialog -Id 'contextual-guidance' -Open {
      Invoke-Element -Element (Find-Element -Names @('帮助') -ControlType ([System.Windows.Automation.ControlType]::Button)) -MajorClick $false
      Invoke-Element -Element (Find-Element -Names @('重新打开行情引导', '重新打开设置引导', '重新打开智能分析引导') -ControlType ([System.Windows.Automation.ControlType]::MenuItem)) -MajorClick $false
    } -DialogNames @('行情快速引导', '设置快速引导', '智能分析快速引导') -CloseNames @('关闭引导', '跳过引导'))
)

# Narrow sidebar proof: semantic icon, collapsed default, and chart reflow.
Open-Route -Names @('行情') | Out-Null
Set-LogicalWindowSize -Width 640 -Height 360
$chartBefore = Find-Element -Names @('行情图表工作区') -ControlType ([System.Windows.Automation.ControlType]::Pane)
$chartBeforeX = [int][Math]::Round($chartBefore.Current.BoundingRectangle.X)
$toggle = Find-Element -Names @('展开导航', '展开自选与最近访问') -ControlType ([System.Windows.Automation.ControlType]::Button)
Invoke-Element -Element $toggle -MajorClick $false
Start-Sleep -Milliseconds 300
$chartAfter = Find-Element -Names @('行情图表工作区') -ControlType ([System.Windows.Automation.ControlType]::Pane)
$rail = Find-Element -Names @('自选与最近访问') -ControlType ([System.Windows.Automation.ControlType]::Pane)
$chartAfterRect = $chartAfter.Current.BoundingRectangle
$railRect = $rail.Current.BoundingRectangle
$intersectionWidth = [Math]::Max(0, [Math]::Min($chartAfterRect.Right, $railRect.Right) - [Math]::Max($chartAfterRect.Left, $railRect.Left))
$intersectionHeight = [Math]::Max(0, [Math]::Min($chartAfterRect.Bottom, $railRect.Bottom) - [Math]::Max($chartAfterRect.Top, $railRect.Top))

$displaySizes = @()
foreach ($size in @(@(1366, 768), @(640, 360))) {
  Set-LogicalWindowSize -Width $size[0] -Height $size[1]
  $rect = [StockDeskDesktopEvidenceNative+RECT]::new()
  if (-not [StockDeskDesktopEvidenceNative]::GetWindowRect($hwnd, [ref]$rect)) { throw 'GetWindowRect failed' }
  $workArea = [System.Windows.Forms.Screen]::FromHandle($hwnd).WorkingArea
  $sizeChecks = @($script:Trees | Where-Object {
      $_.check.logical_size.width -eq $size[0] -and $_.check.logical_size.height -eq $size[1]
    })
  $displaySizes += [ordered]@{
    width = $size[0]; height = $size[1]
    physical_width = $rect.Right - $rect.Left; physical_height = $rect.Bottom - $rect.Top
    within_work_area = (
      $rect.Left -ge $workArea.Left -and $rect.Top -ge $workArea.Top -and
      $rect.Right -le $workArea.Right -and $rect.Bottom -le $workArea.Bottom
    )
    clipped_component_count = [int](($sizeChecks | ForEach-Object { $_.check.clipped_component_count } | Measure-Object -Sum).Sum)
    overlap_count = [int](($sizeChecks | ForEach-Object { $_.check.overlap_count } | Measure-Object -Sum).Sum)
  }
}

$providerLabel = if ($ExpectedProvider -eq 'akshare') { 'AKShare' } else { 'BaoStock' }
$focusContactSheet = Join-Path $root 'focus-region-contact-sheet.png'
$focusRegionManifest = Write-FocusRegionContactSheet -Path $focusContactSheet
$result = [ordered]@{
  schema = 'stock-desk-windows-uia-driver-result-v1'
  candidate = [ordered]@{
    pid = $ExpectedProcessId
    hwnd = $WindowHandle
    executable_sha256 = $ExpectedExecutableSha256
  }
  display = [ordered]@{
    requested_scale_percent = $ExpectedDpiPercent
    get_dpi_for_window = [int]$windowDpi
    get_dpi_for_system = [int]$systemDpi
    get_dpi_for_monitor_x = [int]$monitorX
    get_dpi_for_monitor_y = [int]$monitorY
    window_dpi_awareness_context = 'per-monitor-v2'
    logical_to_physical_roundtrip_max_error_px = [int]$roundtripError
    dpi_virtualized = $false
    logical_window_sizes = $displaySizes
  }
  journey = [ordered]@{
    elapsed_seconds = [Math]::Round($elapsed, 3)
    primary_click_count = $script:PrimaryClicks
    onboarding_steps = @('welcome', 'data_preparation', 'instrument_selection', 'synchronization')
    instrument = [ordered]@{ symbol = '000001.SS'; name = '上证指数'; exchange = 'SH'; instrument_kind = 'index'; period = '1d' }
    real_data = $true; demo = $false; kline_rendered = $true
    source = [ordered]@{
      provider = $ExpectedProvider
      provider_label = $providerLabel
      cutoff_utc = [string]$barsRecord[0].cutoff_utc
      row_count = $rowCount
      catalog_sha256 = [string]$catalogRecord[0].payload_sha256
      bars_sha256 = [string]$barsRecord[0].payload_sha256
    }
    fallback = [ordered]@{
      primary_blocked = $DataPath -eq 'primary-blocked-fallback'
      fallback_used = $DataPath -eq 'primary-blocked-fallback'
      whole_segment = $true
      primary_provider = 'akshare'
      fallback_provider = if ($DataPath -eq 'primary-blocked-fallback') { 'baostock' } else { $null }
    }
  }
  uia = [ordered]@{
    schema = 'stock-desk-windows-uia-matrix-v1'
    api = 'Windows UI Automation 3 + Win32'
    driver_sha256 = Get-Sha256 -Path $PSCommandPath
    routes = $routes
    dialogs = $dialogs
    keyboard = [ordered]@{
      pure_keyboard_journey = $script:OnboardingTabPaths.Count -eq 4 -and $script:KeyboardActivationCount -ge 4
      focus_visible = $script:KeyboardMatrixCheckCount -eq 26 -and $script:FocusObservationCount -ge 30
      tab_order_valid = $script:KeyboardMatrixCheckCount -eq 26
      safe_escape = $script:EscapeBehaviorCheckCount -eq 14
      focus_observation_count = $script:FocusObservationCount
      onboarding_tab_paths = @($script:OnboardingTabPaths)
      auxiliary_tab_paths = @($script:AuxiliaryTabPaths)
    }
    focus_regions = $focusRegionManifest
    narrow_sidebar = [ordered]@{
      logical_size = [ordered]@{ width = 640; height = 360 }
      collapsed_before = $true
      toggle_control_type = 'button'
      toggle_semantic_name = [string]$toggle.Current.Name
      expanded_after = -not $rail.Current.IsOffscreen
      expanded_reflow = [int][Math]::Round($chartAfterRect.X) -ne $chartBeforeX
      chart_x_shift = [Math]::Abs([int][Math]::Round($chartAfterRect.X) - $chartBeforeX)
      sidebar_chart_overlap_pixels = [int]($intersectionWidth * $intersectionHeight)
    }
  }
}

Write-Json -Path $actionPath -Value @($script:Actions)
Write-Json -Path $treePath -Value @($script:Trees)
Write-Json -Path $resultPath -Value $result
exit 0
