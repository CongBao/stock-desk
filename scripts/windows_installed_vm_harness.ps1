# Public, fail-closed boundary between GitHub Actions and a protected Windows VM controller.
# The hypervisor adapter and snapshot policy are machine-owned files outside the checkout.
# This wrapper is the only component allowed to create the upload directory.

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('RestoreCleanSnapshot', 'RunInstalledAcceptance', 'CleanupAndRestoreSnapshot')]
  [string]$Action,
  [Parameter(Mandatory = $true)][ValidateSet('win10-22h2', 'win11')][string]$GuestProfile,
  [Parameter(Mandatory = $true)][ValidateSet('webview-preinstalled', 'webview-absent', 'webview-install-failure')][string]$Scenario,
  [Parameter(Mandatory = $true)][string]$ControllerLabel,
  [Parameter(Mandatory = $true)][string]$ApprovedSnapshotPolicySha256,
  [Parameter(Mandatory = $true)][string]$ApprovedAdapterSha256,
  [ValidateRange(1, 1)][int]$ScenarioAttempt = 1,
  [long]$ActionsRunId,
  [ValidateRange(1, 1)][int]$ActionsRunAttempt = 1,
  [string]$ActionsRepository,
  [string]$ActionsWorkflow,
  [string]$ActionsWorkflowRef,
  [string]$ActionsWorkflowSha,
  [string]$ActionsWorkflowPath,
  [string]$ActionsWorkflowSha256,
  [string]$ActionsJobId,
  [string]$ActionsJobName,
  [Parameter(Mandatory = $true)][string]$ControllerRequestPath,
  [ValidateSet('observed-windows-vm')][string]$EvidenceKind = 'observed-windows-vm',
  [Parameter(Mandatory = $true)][string]$EvidenceRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$uploadRoot = Join-Path $EvidenceRoot 'upload'
$stateRoot = Join-Path $EvidenceRoot 'state'
$privateRoot = Join-Path $EvidenceRoot 'private'
$maximumJsonBytes = 1MB
$maximumRecordBytes = 8MB
$maximumPackageBytes = 16MB
$maximumPublicTextBytes = 2MB
$leaseTtlSeconds = 3600

function Write-NonPassingDiagnostic {
  param([string]$Status, [string]$Reason)
  if (Test-Path -LiteralPath $uploadRoot) { Remove-Item -LiteralPath $uploadRoot -Recurse -Force }
  $root = Join-Path $uploadRoot 'diagnostic'
  New-Item -ItemType Directory -Path $root -Force | Out-Null
  $value = [ordered]@{
    schema = 'stock-desk-windows-vm-controller-diagnostic-v1'
    evidence_kind = 'controller-unavailable-diagnostic'
    action = $Action
    guest_profile = $GuestProfile
    scenario = $Scenario
    status = $Status
    reason = $Reason
  }
  [IO.File]::WriteAllText(
    (Join-Path $root 'controller-diagnostic.json'),
    (($value | ConvertTo-Json -Depth 5) + [Environment]::NewLine),
    [Text.UTF8Encoding]::new($false)
  )
}

function Assert-RegularFile {
  param([string]$Path, [string]$Label)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label is missing" }
  if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label cannot be a reparse point" }
}

function Assert-HexDigest {
  param([object]$Value, [string]$Label)
  if ($Value -isnot [string] -or $Value -cnotmatch '^[0-9a-f]{64}$') { throw "$Label is invalid" }
}

function Get-StringDigest {
  param([string]$Value)
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    return [BitConverter]::ToString(
      $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))
    ).Replace('-', '').ToLowerInvariant()
  } finally { $sha.Dispose() }
}

function Assert-ExactProperties {
  param([object]$Value, [string[]]$Names, [string]$Label)
  if ($null -eq $Value) { throw "$Label is missing" }
  $actual = @($Value.PSObject.Properties.Name | Sort-Object)
  $expected = @($Names | Sort-Object)
  if (($actual -join "`n") -cne ($expected -join "`n")) { throw "$Label fields are not closed" }
}

function Assert-OutsideWorkspace {
  param([string]$Path, [string]$Label)
  $full = [IO.Path]::GetFullPath($Path)
  Assert-RegularFile -Path $full -Label $Label
  $workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\') + '\'
  if ($full.StartsWith($workspace, [StringComparison]::OrdinalIgnoreCase)) { throw "$Label must be controller-owned" }
  return $full
}

function Read-JsonFile {
  param([string]$Path, [string]$Label, [long]$MaximumBytes = $maximumJsonBytes)
  Assert-RegularFile -Path $Path -Label $Label
  $length = (Get-Item -LiteralPath $Path -Force).Length
  if ($length -lt 1 -or $length -gt $MaximumBytes) { throw "$Label exceeds its closed size limit" }
  return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Assert-NoReparsePath {
  param([string]$Root, [string]$Path, [string]$Label)
  $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
  $pathFull = [IO.Path]::GetFullPath($Path)
  $prefix = $rootFull + '\'
  if (-not $pathFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "$Label escapes its root" }
  $current = $rootFull
  $rootItem = Get-Item -LiteralPath $current -Force -ErrorAction Stop
  if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label root cannot be a reparse point" }
  foreach ($component in $pathFull.Substring($prefix.Length).Split('\')) {
    $current = Join-Path $current $component
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "$Label path cannot contain a reparse point" }
  }
}

function Invoke-ProtectedAdapter {
  param([string]$AdapterPath, [object]$Assignment, [string]$PolicySha256, [string]$RequestSha256, [string]$GuestHarnessPath, [string]$GuestHarnessSha256, [string]$ControllerBindingSha256, [string]$LeaseDigest)
  $actionRoot = Join-Path $privateRoot $Action
  if (Test-Path -LiteralPath $actionRoot) { Remove-Item -LiteralPath $actionRoot -Recurse -Force }
  New-Item -ItemType Directory -Path $actionRoot -Force | Out-Null
  $resultPath = Join-Path $actionRoot 'adapter-result.json'
  $arguments = @{
    Action = $Action; GuestProfile = $GuestProfile; Scenario = $Scenario
    ControllerLabel = $ControllerLabel; ControllerRequestPath = $ControllerRequestPath
    ControllerRequestSha256 = $RequestSha256; GuestHarnessPath = $GuestHarnessPath
    GuestHarnessSha256 = $GuestHarnessSha256; SnapshotPolicySha256 = $PolicySha256
    SnapshotAssignmentJson = ($Assignment | ConvertTo-Json -Depth 8 -Compress)
    PrivateEvidenceRoot = $actionRoot; AdapterResultPath = $resultPath
    ScenarioAttempt = $ScenarioAttempt; ActionsRepository = $ActionsRepository
    ActionsWorkflow = $ActionsWorkflow; ActionsWorkflowRef = $ActionsWorkflowRef
    ActionsWorkflowSha = $ActionsWorkflowSha; ActionsWorkflowPath = $ActionsWorkflowPath
    ActionsWorkflowSha256 = $ActionsWorkflowSha256; ActionsRunId = $ActionsRunId
    ActionsRunAttempt = $ActionsRunAttempt; ActionsJobId = $ActionsJobId
    ActionsJobName = $ActionsJobName; EvidenceKind = $EvidenceKind
    ControllerBindingSha256 = $ControllerBindingSha256; LeaseDigest = $LeaseDigest
    LeaseTtlSeconds = $leaseTtlSeconds
  }
  $privateLogPath = Join-Path $actionRoot 'adapter-private.log'
  try {
    $LASTEXITCODE = 0
    & $AdapterPath @arguments *> $privateLogPath
    $adapterInvocationSucceeded = $?
    $adapterExitCode = $LASTEXITCODE
  } catch {
    throw 'Protected VM adapter failed; details remain in the controller-private log'
  }
  if (-not $adapterInvocationSucceeded -or $adapterExitCode -ne 0) { throw 'Protected VM adapter returned a nonzero exit code; details remain private' }
  $result = Read-JsonFile -Path $resultPath -Label 'Protected VM adapter result'
  Assert-ExactProperties -Value $result -Label 'Protected VM adapter result' -Names @(
    'schema', 'action', 'guest_profile', 'controller_label', 'scenario', 'status',
    'snapshot_id', 'snapshot_sha256', 'image_sha256', 'system',
    'webview_initial_state', 'failure_injection', 'guest_executed_harness_sha256',
    'controller_binding_sha256', 'lease_digest', 'lease_state',
    'lease_expires_at_utc', 'watchdog_armed'
  )
  if (
    $result.schema -ne 'stock-desk-windows-vm-adapter-result-v1' -or
    $result.action -ne $Action -or $result.guest_profile -ne $GuestProfile -or
    $result.controller_label -ne $ControllerLabel -or $result.scenario -ne $Scenario -or
    $result.status -ne 'completed'
  ) { throw 'Protected VM adapter result identity is inconsistent' }
  if ($result.controller_binding_sha256 -cne $ControllerBindingSha256 -or $result.lease_digest -cne $LeaseDigest) { throw 'Protected adapter lease binding is inconsistent' }
  Assert-HexDigest -Value $result.controller_binding_sha256 -Label 'Controller binding digest'
  Assert-HexDigest -Value $result.lease_digest -Label 'Controller lease digest'
  if ($Action -eq 'CleanupAndRestoreSnapshot') {
    if ($result.lease_state -ne 'released-after-restore' -or $result.watchdog_armed -ne $false -or $null -ne $result.lease_expires_at_utc) { throw 'Protected adapter did not release its lease after restore' }
  } else {
    if ($result.lease_state -ne 'armed' -or $result.watchdog_armed -ne $true -or [string]$result.lease_expires_at_utc -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') { throw 'Protected adapter did not arm its cancellation watchdog lease' }
    $leaseExpiry = [DateTimeOffset]::ParseExact([string]$result.lease_expires_at_utc, 'yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture, ([Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal))
    $leaseNow = [DateTimeOffset]::UtcNow
    if ($leaseExpiry -le $leaseNow.AddMinutes(45) -or $leaseExpiry -gt $leaseNow.AddSeconds($leaseTtlSeconds + 300)) { throw 'Protected adapter watchdog lease is outside its bounded TTL' }
  }
  foreach ($name in @('snapshot_id', 'snapshot_sha256', 'image_sha256', 'webview_initial_state', 'failure_injection')) {
    if (($result.$name | ConvertTo-Json -Depth 8 -Compress) -cne ($Assignment.$name | ConvertTo-Json -Depth 8 -Compress)) { throw "Adapter result does not match protected assignment: $name" }
  }
  if (($result.system | ConvertTo-Json -Depth 8 -Compress) -cne ($Assignment.system | ConvertTo-Json -Depth 8 -Compress)) { throw 'Adapter OS identity does not match protected assignment' }
  if ($Action -in @('RestoreCleanSnapshot', 'CleanupAndRestoreSnapshot')) {
    if ($null -ne $result.guest_executed_harness_sha256) { throw 'Restore cannot claim that the guest harness already executed' }
  } else {
    Assert-HexDigest -Value $result.guest_executed_harness_sha256 -Label 'Guest-executed harness digest'
    if ($result.guest_executed_harness_sha256 -cne $GuestHarnessSha256) { throw 'Protected adapter measured a different guest-executed harness' }
  }
  return $result
}

function Copy-ClosedPublicPackage {
  param([string]$SourceRoot)
  $manifestPath = Join-Path $SourceRoot 'public\raw-manifest.json'
  Assert-NoReparsePath -Root $SourceRoot -Path $manifestPath -Label 'Raw guest manifest'
  $manifest = Read-JsonFile -Path $manifestPath -Label 'Raw guest manifest'
  $packageBytes = (Get-Item -LiteralPath $manifestPath -Force).Length
  if ($manifest.artifact -ne 'windows-installed-raw-evidence' -or $manifest.schema_version -ne 1 -or $manifest.scenario -ne $Scenario) { throw 'Raw guest manifest identity is inconsistent' }
  $text = Get-Content -LiteralPath $manifestPath -Raw
  if ($text -match '"passed"\s*:') { throw 'Raw guest evidence cannot declare passed' }
  $expectedCount = if ($Scenario -eq 'webview-install-failure') { 3 } else { 4 }
  if (@($manifest.records).Count -ne $expectedCount) { throw 'Raw guest manifest has the wrong closed public record count' }
  $destination = Join-Path $uploadRoot 'public'
  if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
  New-Item -ItemType Directory -Path (Join-Path $destination 'raw') -Force | Out-Null
  Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $destination 'raw-manifest.json')
  $sourcePrefix = [IO.Path]::GetFullPath((Join-Path $SourceRoot 'public')).TrimEnd('\') + '\'
  foreach ($record in $manifest.records) {
    if ($record.path -cnotmatch '^raw/[a-z0-9][a-z0-9._-]{0,63}$') { throw 'Raw record path is unsafe' }
    Assert-HexDigest -Value $record.sha256 -Label 'Raw record digest'
    $source = [IO.Path]::GetFullPath((Join-Path (Join-Path $SourceRoot 'public') ($record.path -replace '/', '\')))
    if (-not $source.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Raw record path escapes public source' }
    Assert-RegularFile -Path $source -Label 'Raw record'
    Assert-NoReparsePath -Root $SourceRoot -Path $source -Label 'Raw record'
    $recordLength = (Get-Item -LiteralPath $source -Force).Length
    if ($recordLength -lt 1 -or $recordLength -gt $maximumRecordBytes) { throw 'Raw record exceeds its closed size limit' }
    $packageBytes += $recordLength
    if ($packageBytes -gt $maximumPackageBytes) { throw 'Raw public package exceeds its closed size limit' }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256) { throw 'Raw record digest mismatch' }
    if ($recordLength -ne $record.size_bytes) { throw 'Raw record size mismatch' }
    Copy-Item -LiteralPath $source -Destination (Join-Path $destination ($record.path -replace '/', '\'))
  }
  $captureRecords = @($manifest.records | Where-Object { $_.kind -eq 'window-capture' })
  $textRecords = @($manifest.records | Where-Object { $_.kind -eq 'ui-automation-text' })
  $failureRecords = @($manifest.records | Where-Object { $_.kind -eq 'failure-diagnostic' })
  $streamRecords = @($manifest.records | Where-Object { $_.kind -eq 'observation-stream' })
  $logRecords = @($manifest.records | Where-Object { $_.kind -eq 'install-log' })
  if ($streamRecords.Count -ne 1 -or $logRecords.Count -ne 1) { throw 'Raw public record roles are incomplete' }
  $streamPath = Join-Path $destination ($streamRecords[0].path -replace '/', '\')
  $logPath = Join-Path $destination ($logRecords[0].path -replace '/', '\')
  $detailRecord = if ($Scenario -eq 'webview-install-failure') { $failureRecords[0] } else { $textRecords[0] }
  $detailPath = Join-Path $destination ($detailRecord.path -replace '/', '\')
  $publicTextBytes = (Get-Item -LiteralPath $detailPath).Length + (Get-Item -LiteralPath $streamPath).Length + (Get-Item -LiteralPath $logPath).Length
  if ($publicTextBytes -gt $maximumPublicTextBytes) { throw 'Raw public text exceeds its closed size limit' }
  $publicText = (Get-Content -LiteralPath $detailPath -Raw) + "`n" + (Get-Content -LiteralPath $streamPath -Raw) + "`n" + (Get-Content -LiteralPath $logPath -Raw)
  $publicUserPathPattern = '(?i)(?:[a-z]:\\' + 'users\\|/' + 'home/|/' + 'users/)[^\s"'']+'
  if ($publicText -match '(?i)(authorization\s*:|bearer\s+[a-z0-9._-]+|api[_-]?key\s*[=:]|password\s*[=:]|github[_-]?token)' -or $publicText -match $publicUserPathPattern) { throw 'Raw public text contains a secret or user-profile path' }
  $events = @(Get-Content -LiteralPath $streamPath | ForEach-Object { $_ | ConvertFrom-Json })
  $windowEvents = @($events | Where-Object { $_.kind -eq 'window-observation' })
  if ($windowEvents.Count -ne 1) { throw 'Raw package lacks a unique window observation' }
  $manifestBrowserObserver = $manifest.capture.browser_window_observer | ConvertTo-Json -Depth 8 -Compress
  $eventBrowserObserver = $windowEvents[0].value.external_browser_observer | ConvertTo-Json -Depth 8 -Compress
  if (
    $manifest.capture.browser_window_observer.schema -ne 'stock-desk-browser-window-observer-v1' -or
    $manifest.capture.browser_window_observer.api -ne 'Win32 EnumWindows + SetWinEventHook' -or
    $manifestBrowserObserver -cne $eventBrowserObserver -or
    @($windowEvents[0].value.external_browser_window_events).Count -ne $manifest.capture.browser_window_observer.lifecycle_event_count
  ) { throw 'Browser EnumWindows/SetWinEventHook evidence is not manifest-bound' }
  if ($Scenario -eq 'webview-install-failure') {
    if ($failureRecords.Count -ne 1 -or $captureRecords.Count -ne 0 -or $textRecords.Count -ne 0 -or $windowEvents[0].value.capture_scope -ne 'none') { throw 'Failure evidence must use parent/child exit diagnostics without a UI shim' }
    if ($publicText -notmatch 'webview_child_exit_code=-?[1-9][0-9]*' -or $publicText -notmatch 'nsis_parent_exit_code=-?[1-9][0-9]*') { throw 'Failure diagnostics lack real child and NSIS parent abort exits' }
  } else {
    if ($captureRecords.Count -ne 1 -or $textRecords.Count -ne 1 -or $failureRecords.Count -ne 0 -or $windowEvents[0].value.capture_scope -ne 'target-window-only') { throw 'Success screenshot is not bound to a target-window observation' }
    if ($windowEvents[0].value.rendered_content_sha256 -ne $captureRecords[0].sha256) { throw 'Screenshot digest is not bound to the window observation' }
    if ($windowEvents[0].value.uia_text_sha256 -ne $textRecords[0].sha256) { throw 'Screenshot UI Automation text is not digest-bound' }
  }
}

try {
  Assert-RegularFile -Path $ControllerRequestPath -Label 'Controller request'
  $request = Read-JsonFile -Path $ControllerRequestPath -Label 'Controller request'
  if ($request.schema -ne 'stock-desk-windows-installed-controller-request-v1' -or $request.evidence_kind -ne 'observed-windows-vm' -or $request.status -ne 'awaiting-controller' -or $request.wiring_only -ne $true) { throw 'Controller request contract is invalid' }
  foreach ($name in @('candidate_manifest_sha256', 'main_proof_sha256', 'candidate_sha256', 'webview_installer_sha256')) { Assert-HexDigest -Value $request.$name -Label "Controller request $name" }

  $adapterPath = $env:STOCK_DESK_WINDOWS_VM_ADAPTER
  $policyPath = $env:STOCK_DESK_WINDOWS_VM_SNAPSHOT_POLICY
  if ([string]::IsNullOrWhiteSpace($adapterPath) -or [string]::IsNullOrWhiteSpace($policyPath)) {
    Write-NonPassingDiagnostic -Status 'protected-controller-not-configured' -Reason 'Protected VM adapter or snapshot policy is unavailable.'
    exit 86
  }
  $adapterPath = Assert-OutsideWorkspace -Path $adapterPath -Label 'Protected VM adapter'
  $policyPath = Assert-OutsideWorkspace -Path $policyPath -Label 'Protected snapshot policy'
  $policySha256 = (Get-FileHash -LiteralPath $policyPath -Algorithm SHA256).Hash.ToLowerInvariant()
  Assert-HexDigest -Value $ApprovedSnapshotPolicySha256 -Label 'Approved snapshot policy digest'
  if ($policySha256 -cne $ApprovedSnapshotPolicySha256) { throw 'Protected snapshot policy is not externally approved' }
  $policy = Read-JsonFile -Path $policyPath -Label 'Protected snapshot policy'
  Assert-ExactProperties -Value $policy -Names @('schema', 'assignments') -Label 'Protected snapshot policy'
  if ($policy.schema -ne 'stock-desk-windows-vm-snapshot-policy-v1') { throw 'Protected snapshot policy schema is unsupported' }
  if (@($policy.assignments).Count -ne 3) { throw 'Protected snapshot policy must contain the exact three-scenario matrix' }
  $matches = @($policy.assignments | Where-Object { $_.guest_profile -eq $GuestProfile -and $_.controller_label -eq $ControllerLabel -and $_.scenario -eq $Scenario })
  if ($matches.Count -ne 1) { throw 'Protected snapshot policy has no unique matrix assignment' }
  $assignment = $matches[0]
  Assert-ExactProperties -Value $assignment -Label 'Protected snapshot assignment' -Names @('guest_profile', 'controller_label', 'scenario', 'snapshot_id', 'snapshot_sha256', 'image_sha256', 'system', 'webview_initial_state', 'failure_injection')
  Assert-ExactProperties -Value $assignment.system -Label 'Protected snapshot OS assignment' -Names @('family', 'display_version', 'build_number', 'update_build_revision', 'architecture')
  foreach ($name in @('snapshot_sha256', 'image_sha256')) { Assert-HexDigest -Value $assignment.$name -Label "Assignment $name" }
  if ($GuestProfile -eq 'win10-22h2' -and ($assignment.system.family -ne 'windows-10' -or $assignment.system.display_version -ne '22H2' -or $assignment.system.build_number -ne 19045)) { throw 'Windows 10 profile is not pinned to 22H2 build 19045' }
  if ($GuestProfile -eq 'win11' -and ($assignment.system.family -ne 'windows-11' -or $assignment.system.build_number -lt 22000)) { throw 'Windows 11 profile assignment is invalid' }
  if ($Scenario -eq 'webview-preinstalled' -and $assignment.webview_initial_state -ne 'present') { throw 'Preinstalled scenario policy is contradictory' }
  if ($Scenario -ne 'webview-preinstalled' -and $assignment.webview_initial_state -ne 'absent') { throw 'WebView-absent scenario policy is contradictory' }
  if ($Scenario -eq 'webview-install-failure') {
    Assert-ExactProperties -Value $assignment.failure_injection -Label 'Protected failure injection' -Names @('identity', 'sha256')
    if ($assignment.failure_injection.identity -ne 'stock-desk-webview2-offline-install-failure-v1') { throw 'Failure injection policy is not fixed' }
    Assert-HexDigest -Value $assignment.failure_injection.sha256 -Label 'Failure injection digest'
  } elseif ($null -ne $assignment.failure_injection) { throw 'Unexpected failure injection policy' }
  $controllerBindingSha256 = Get-StringDigest -Value ("stock-desk-controller-binding-v1`0$ControllerLabel`0$($assignment.snapshot_id)`0$($assignment.snapshot_sha256)")
  $leaseDigest = Get-StringDigest -Value ("stock-desk-controller-lease-v1`0$ActionsRepository`0$ActionsWorkflowSha`0$ActionsRunId`0$ActionsJobId`0$controllerBindingSha256")

  $guestHarnessPath = Join-Path $PSScriptRoot 'windows_installed_guest_harness.ps1'
  Assert-RegularFile -Path $guestHarnessPath -Label 'Reviewed guest harness'
  $guestHarnessSha256 = (Get-FileHash -LiteralPath $guestHarnessPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $requestSha256 = (Get-FileHash -LiteralPath $ControllerRequestPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $adapterSha256 = (Get-FileHash -LiteralPath $adapterPath -Algorithm SHA256).Hash.ToLowerInvariant()
  Assert-HexDigest -Value $ApprovedAdapterSha256 -Label 'Approved adapter digest'
  if ($adapterSha256 -cne $ApprovedAdapterSha256) { throw 'Protected VM adapter is not externally approved' }
  if ($Action -eq 'RestoreCleanSnapshot' -and (Test-Path -LiteralPath $stateRoot)) { Remove-Item -LiteralPath $stateRoot -Recurse -Force }
  New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
  $statePath = Join-Path $stateRoot 'lifecycle-state.json'

  if ($Action -eq 'RestoreCleanSnapshot') {
    if (Test-Path -LiteralPath $uploadRoot) { Remove-Item -LiteralPath $uploadRoot -Recurse -Force }
    if (Test-Path -LiteralPath $privateRoot) { Remove-Item -LiteralPath $privateRoot -Recurse -Force }
    $adapterResult = Invoke-ProtectedAdapter -AdapterPath $adapterPath -Assignment $assignment -PolicySha256 $policySha256 -RequestSha256 $requestSha256 -GuestHarnessPath $guestHarnessPath -GuestHarnessSha256 $guestHarnessSha256 -ControllerBindingSha256 $controllerBindingSha256 -LeaseDigest $leaseDigest
    $state = [ordered]@{
      schema = 'stock-desk-windows-vm-lifecycle-receipt-v1'; guest_profile = $GuestProfile
      controller_label = $ControllerLabel; scenario = $Scenario; snapshot_policy_sha256 = $policySha256
      snapshot_sha256 = $assignment.snapshot_sha256
      image_sha256 = $assignment.image_sha256; system = $assignment.system
      webview_initial_state = $assignment.webview_initial_state; failure_injection = $assignment.failure_injection
      controller_request_sha256 = $requestSha256; guest_harness_sha256 = $guestHarnessSha256
      guest_executed_harness_sha256 = $null
      workflow_sha256 = $ActionsWorkflowSha256; raw_manifest_sha256 = $null
      restored_before_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
      acceptance_completed_at_utc = $null; cleanup_restored_at_utc = $null; adapter_sha256 = $adapterSha256
      controller_binding_sha256 = $controllerBindingSha256; lease_digest = $leaseDigest
      lease_expires_at_utc = $adapterResult.lease_expires_at_utc; watchdog_armed = $true
      lease_state = 'armed'; lease_released_at_utc = $null
    }
    [IO.File]::WriteAllText($statePath, (($state | ConvertTo-Json -Depth 10) + "`n"), [Text.UTF8Encoding]::new($false))
  } elseif ($Action -eq 'RunInstalledAcceptance') {
    $state = Read-JsonFile -Path $statePath -Label 'Controller restore state'
    if ($state.cleanup_restored_at_utc -ne $null -or $state.raw_manifest_sha256 -ne $null) { throw 'Controller lifecycle state is not fresh' }
    $adapterResult = Invoke-ProtectedAdapter -AdapterPath $adapterPath -Assignment $assignment -PolicySha256 $policySha256 -RequestSha256 $requestSha256 -GuestHarnessPath $guestHarnessPath -GuestHarnessSha256 $guestHarnessSha256 -ControllerBindingSha256 $controllerBindingSha256 -LeaseDigest $leaseDigest
    if ($state.controller_binding_sha256 -cne $controllerBindingSha256 -or $state.lease_digest -cne $leaseDigest) { throw 'Controller lifecycle lease binding changed before acceptance' }
    $state.guest_executed_harness_sha256 = $adapterResult.guest_executed_harness_sha256
    $state.lease_expires_at_utc = $adapterResult.lease_expires_at_utc
    Copy-ClosedPublicPackage -SourceRoot (Join-Path $privateRoot $Action)
    $state.raw_manifest_sha256 = (Get-FileHash -LiteralPath (Join-Path $uploadRoot 'public\raw-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    $state.acceptance_completed_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    [IO.File]::WriteAllText($statePath, (($state | ConvertTo-Json -Depth 10) + "`n"), [Text.UTF8Encoding]::new($false))
  } else {
    # Restore the protected snapshot before consulting evidence state. Cleanup is
    # an always() safety boundary and must remain independently idempotent when
    # restore, guest execution, or evidence publication failed part-way through.
    $adapterResult = Invoke-ProtectedAdapter -AdapterPath $adapterPath -Assignment $assignment -PolicySha256 $policySha256 -RequestSha256 $requestSha256 -GuestHarnessPath $guestHarnessPath -GuestHarnessSha256 $guestHarnessSha256 -ControllerBindingSha256 $controllerBindingSha256 -LeaseDigest $leaseDigest
    $state = Read-JsonFile -Path $statePath -Label 'Controller acceptance state'
    if ($state.controller_binding_sha256 -cne $controllerBindingSha256 -or $state.lease_digest -cne $leaseDigest) { throw 'Controller lifecycle lease binding changed before cleanup' }
    if ($null -eq $state.acceptance_completed_at_utc -or $null -eq $state.raw_manifest_sha256 -or $null -eq $state.guest_executed_harness_sha256) { throw 'Cleanup restored the snapshot but cannot publish an incomplete acceptance lifecycle' }
    $state.cleanup_restored_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $state.watchdog_armed = $false
    $state.lease_state = $adapterResult.lease_state
    $state.lease_released_at_utc = $state.cleanup_restored_at_utc
    $controllerUpload = Join-Path $uploadRoot 'controller'
    New-Item -ItemType Directory -Path $controllerUpload -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $controllerUpload 'lifecycle-receipt.json'), (($state | ConvertTo-Json -Depth 10) + "`n"), [Text.UTF8Encoding]::new($false))
  }
  if (Test-Path -LiteralPath $privateRoot) { Remove-Item -LiteralPath $privateRoot -Recurse -Force }
  if ($Action -eq 'CleanupAndRestoreSnapshot' -and (Test-Path -LiteralPath $stateRoot)) { Remove-Item -LiteralPath $stateRoot -Recurse -Force }
} catch {
  Write-NonPassingDiagnostic -Status 'controller-failed' -Reason 'Protected controller, snapshot, guest, cleanup, or public-package contract failed.'
  if (Test-Path -LiteralPath $privateRoot) { Remove-Item -LiteralPath $privateRoot -Recurse -Force -ErrorAction SilentlyContinue }
  if ($Action -eq 'CleanupAndRestoreSnapshot' -and (Test-Path -LiteralPath $stateRoot)) { Remove-Item -LiteralPath $stateRoot -Recurse -Force -ErrorAction SilentlyContinue }
  Write-Error 'Windows VM acceptance failed closed; controller details remain only in its private evidence root.'
  exit 86
}

exit 0
