[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][long]$WindowHandle,
  [Parameter(Mandatory = $true)][int]$ExpectedProcessId,
  [Parameter(Mandatory = $true)][int]$EvidenceProcessId,
  [Parameter(Mandatory = $true)][string]$ExpectedExecutableSha256,
  [Parameter(Mandatory = $true)][string]$SourceSha,
  [Parameter(Mandatory = $true)][string]$SourceTree,
  [Parameter(Mandatory = $true)][string]$CandidateSha256,
  [Parameter(Mandatory = $true)][string]$WebViewUserDataDir,
  [Parameter(Mandatory = $true)][int]$CdpPort,
  [Parameter(Mandatory = $true)][int]$WebViewBrowserProcessId,
  [Parameter(Mandatory = $true)][string]$CaptureSyncRoot,
  [Parameter(Mandatory = $true)][string]$CaptureNonce,
  [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $IsWindows) { throw 'Windows Hosted automation requires Windows' }
foreach ($identity in @($SourceSha, $SourceTree)) {
  if ($identity -cnotmatch '^[0-9a-f]{40}$') { throw 'Hosted automation source identity is invalid' }
}
foreach ($digest in @($ExpectedExecutableSha256, $CandidateSha256)) {
  if ($digest -cnotmatch '^[0-9a-f]{64}$') { throw 'Hosted automation binary identity is invalid' }
}
if ($CaptureNonce -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') {
  throw 'Hosted automation capture nonce is invalid'
}
if ($WindowHandle -lt 1 -or $ExpectedProcessId -lt 1 -or $EvidenceProcessId -lt 1) {
  throw 'Hosted automation process identity is invalid'
}
if ($WebViewBrowserProcessId -lt 1 -or $CdpPort -lt 1 -or $CdpPort -gt 65535) {
  throw 'Hosted automation WebView2 identity is invalid'
}

$CaptureSyncRoot = [IO.Path]::GetFullPath($CaptureSyncRoot)
$WebViewUserDataDir = [IO.Path]::GetFullPath($WebViewUserDataDir)
if (-not (Test-Path -LiteralPath $CaptureSyncRoot -PathType Container)) {
  throw 'Hosted automation capture root is unavailable'
}
if (-not (Test-Path -LiteralPath $WebViewUserDataDir -PathType Container)) {
  throw 'Hosted automation WebView2 user data directory is unavailable'
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$process = [Diagnostics.Process]::GetProcessById($ExpectedProcessId)
$evidenceProcess = [Diagnostics.Process]::GetProcessById($EvidenceProcessId)
$webviewProcess = [Diagnostics.Process]::GetProcessById($WebViewBrowserProcessId)
if ($process.HasExited -or $evidenceProcess.HasExited -or $webviewProcess.HasExited) {
  throw 'Hosted automation process exited before interaction began'
}
$actualHash = (Get-FileHash -LiteralPath $process.MainModule.FileName -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -cne $ExpectedExecutableSha256) {
  throw 'Hosted automation executable identity does not match the installed candidate'
}

$actions = [Collections.Generic.List[object]]::new()
$progressPath = Join-Path (Split-Path -Parent $OutputPath) 'packaged-hosted-automation-progress.json'

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
      throw "packaged WebView evidence exited before Hosted marker: $Name"
    }
    if (Test-Path -LiteralPath $path -PathType Leaf) {
      try {
        $candidate = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($candidate.capture_nonce -ceq $CaptureNonce) { return $candidate }
      } catch {
        # The producer publishes by atomic rename. Retry transient sharing failures.
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
      if ($attempt -eq 10) { throw "Hosted acknowledgment could not be published: $Name" }
      Start-Sleep -Milliseconds 100
    }
  }
}

function Write-ProgressEvidence {
  $progress = [ordered]@{
    schema_version = 'stock-desk-windows-hosted-automation-progress-v1'
    source_sha = $SourceSha
    source_tree = $SourceTree
    candidate_sha256 = $CandidateSha256
    capture_nonce = $CaptureNonce
    process_id = $ExpectedProcessId
    main_window_handle = $WindowHandle
    actions = $actions
  }
  $temporary = "$progressPath.$([Guid]::NewGuid().ToString('N')).tmp"
  [IO.File]::WriteAllText(
    $temporary,
    (($progress | ConvertTo-Json -Depth 12) + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
  )
  Move-Item -LiteralPath $temporary -Destination $progressPath -Force
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
    throw 'Hosted UIA root belongs to another process'
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

function Find-NativeCloseButton([int]$TimeoutSeconds = 15) {
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
  do {
    $matches = [Collections.Generic.List[System.Windows.Automation.AutomationElement]]::new()
    $all = (Get-RootElement).FindAll(
      [System.Windows.Automation.TreeScope]::Descendants,
      [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($candidate in $all) {
      if ($candidate.Current.ProcessId -ne $ExpectedProcessId) { continue }
      if ($candidate.Current.ControlType -ne [System.Windows.Automation.ControlType]::Button) { continue }
      if (-not $candidate.Current.IsEnabled -or $candidate.Current.IsOffscreen) { continue }
      if (-not (Test-HasTitleBarAncestor $candidate)) { continue }
      $name = ([string]$candidate.Current.Name).Trim()
      $automationId = ([string]$candidate.Current.AutomationId).Trim()
      if ($name -in @('Close', '关闭') -or $automationId -in @('Close', 'CloseButton')) {
        $matches.Add($candidate)
      }
    }
    if ($matches.Count -eq 1) { return $matches[0] }
    if ($matches.Count -gt 1) { throw 'native close UIA target is ambiguous' }
    Start-Sleep -Milliseconds 100
  } while ([DateTimeOffset]::UtcNow -lt $deadline)
  throw 'native close UIA target was not found'
}

function Invoke-NativeClose([int]$Sequence, [string]$Action) {
  $button = Find-NativeCloseButton
  $patternObject = $null
  if (-not $button.TryGetCurrentPattern(
    [System.Windows.Automation.InvokePattern]::Pattern,
    [ref]$patternObject
  )) { throw 'native close button does not expose InvokePattern' }
  $runtimeId = @($button.GetRuntimeId()) -join '.'
  $record = [ordered]@{
    sequence = $Sequence
    action = $Action
    target = [ordered]@{
      process_id = [int]$button.Current.ProcessId
      window_handle = $WindowHandle
      automation_id = [string]$button.Current.AutomationId
      name = [string]$button.Current.Name
      runtime_id = $runtimeId
      titlebar_ancestor = $true
      enabled = $true
      offscreen = $false
    }
    invocation = 'uia-invoke-pattern'
    physical_mouse_click = $false
    observed_state = 'pending-webview-observation'
  }
  $pattern = [System.Windows.Automation.InvokePattern]$patternObject
  $pattern.Invoke()
  return $record
}

function Get-WebViewAction([object]$Marker, [int]$Sequence, [string]$Action, [string]$Name) {
  $record = $Marker.action
  if (
    $null -eq $record -or
    [int]$record.sequence -ne $Sequence -or
    [string]$record.action -cne $Action -or
    [string]$record.invocation -cne 'playwright-cdp-click' -or
    $record.physical_mouse_click -ne $false -or
    [string]$record.target.role -cne 'button' -or
    [string]$record.target.name -cne $Name -or
    $record.target.exact -ne $true
  ) { throw "Hosted WebView action is invalid: $Action" }
  return [ordered]@{
    sequence = $Sequence
    action = $Action
    target = [ordered]@{ role='button'; name=$Name; exact=$true }
    invocation = 'playwright-cdp-click'
    physical_mouse_click = $false
    observed_state = [string]$record.observed_state
  }
}

$close1 = Invoke-NativeClose 1 'native-close-open-dialog'
$dialog1 = Wait-CaptureMarker 'hosted-dialog-visible-1' 20 'WebView did not observe the first exit dialog'
if ([string]$dialog1.observed_state -cne 'exit-dialog-visible') {
  throw 'first Hosted exit dialog observation is invalid'
}
$close1.observed_state = 'exit-dialog-visible'
$actions.Add($close1)
Write-ProgressEvidence
Write-CaptureAck 'hosted-dialog-visible-1'
Write-CaptureAck 'hosted-cancel-authorized'

$cancelMarker = Wait-CaptureMarker 'hosted-cancel-complete' 20 'WebView did not complete cancel automation'
$cancel = Get-WebViewAction $cancelMarker 2 'webview-cancel-dialog' '取消'
if ($cancel.observed_state -cne 'dialog-hidden-host-alive') {
  throw 'Hosted cancel observation is invalid'
}
$process.Refresh()
if ($process.HasExited) { throw 'host exited after cancel automation' }
$actions.Add($cancel)
Write-ProgressEvidence
Write-CaptureAck 'hosted-cancel-complete'

$close2 = Invoke-NativeClose 3 'native-close-reopen-dialog'
$dialog2 = Wait-CaptureMarker 'hosted-dialog-visible-2' 20 'WebView did not observe the reopened exit dialog'
if ([string]$dialog2.observed_state -cne 'exit-dialog-visible') {
  throw 'second Hosted exit dialog observation is invalid'
}
$close2.observed_state = 'exit-dialog-visible'
$actions.Add($close2)
Write-ProgressEvidence
Write-CaptureAck 'hosted-dialog-visible-2'
Write-CaptureAck 'hosted-confirm-authorized'

$confirmMarker = Wait-CaptureMarker 'hosted-confirm-complete' 30 'WebView did not complete exit confirmation automation'
$confirm = Get-WebViewAction $confirmMarker 4 'webview-confirm-exit' '退出应用'
Write-CaptureAck 'hosted-confirm-complete'
if (-not $process.WaitForExit(30000)) {
  throw 'host did not exit successfully after confirmation automation'
}
$process.Refresh()
$hostExitCode = [int]$process.ExitCode
if ($hostExitCode -ne 0) {
  throw "host did not exit successfully after confirmation automation: $hostExitCode"
}
$confirm.observed_state = 'host-exited-zero'
$actions.Add($confirm)
Write-ProgressEvidence

$parent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force $parent | Out-Null
$evidence = [ordered]@{
  schema_version = 'stock-desk-windows-hosted-automation-v1'
  input_method = 'windows-uia-and-cdp-automation'
  physical_mouse_click = $false
  source_sha = $SourceSha
  source_tree = $SourceTree
  candidate_sha256 = $CandidateSha256
  installed_executable_sha256 = $actualHash
  capture_nonce = $CaptureNonce
  process_id = $ExpectedProcessId
  main_window_handle = $WindowHandle
  webview2 = [ordered]@{
    user_data_dir = $WebViewUserDataDir
    cdp_port = $CdpPort
    browser_process_id = $WebViewBrowserProcessId
  }
  actions = $actions
  host_exit_code = $hostExitCode
  hosted_runner_limitations = @(
    'github-hosted-windows-server-is-not-win10-or-win11-desktop',
    'runner-account-is-not-real-standard-user-acceptance',
    'automation-does-not-prove-uac-secure-desktop-or-physical-input'
  )
}
$temporary = "$OutputPath.$([Guid]::NewGuid().ToString('N')).tmp"
[IO.File]::WriteAllText(
  $temporary,
  (($evidence | ConvertTo-Json -Depth 12) + [Environment]::NewLine),
  [Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $temporary -Destination $OutputPath -Force
Remove-Item -LiteralPath $progressPath -Force -ErrorAction SilentlyContinue

exit 0
