param(
  [Parameter(Mandatory = $true)] [string]$Kit,
  [Parameter(Mandatory = $true)] [string]$EvidenceRoot,
  [Parameter(Mandatory = $true)] [string]$DiagnosticsRoot,
  [Parameter(Mandatory = $true)] [ValidateSet('push', 'pull_request')] [string]$SourceEvent,
  [Parameter(Mandatory = $true)] [ValidatePattern('^(refs/heads/main|refs/pull/[1-9][0-9]*/merge)$')] [string]$SourceRef,
  [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-f]{40}$')] [string]$SourceSha,
  [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-f]{40}$')] [string]$SourceTree,
  [Parameter(Mandatory = $true)] [ValidateRange(1, [long]::MaxValue)] [long]$SourceEpoch,
  [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-f]{40}$')] [string]$GitHubSha
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
if (-not $IsWindows) { throw 'the NSIS repack integration requires Windows x64' }

$repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repository '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'the locked Python environment is missing' }
if ((git rev-parse HEAD) -cne $SourceSha) { throw 'SourceSha is not checkout HEAD' }
if ((git rev-parse 'HEAD^{tree}') -cne $SourceTree) { throw 'SourceTree is not checkout tree' }
if ([long](git show -s --format=%ct HEAD) -ne $SourceEpoch) { throw 'SourceEpoch is not checkout epoch' }
if ($SourceEvent -ceq 'push') {
  if ($SourceRef -cne 'refs/heads/main' -or $GitHubSha -cne $SourceSha) {
    throw 'push requires the exact protected main source pairing'
  }
} elseif ($SourceRef -cnotmatch '^refs/pull/[1-9][0-9]*/merge$') {
  throw 'pull_request requires a canonical merge ref'
}
if ($env:GITHUB_ACTIONS -ceq 'true') {
  if ($env:GITHUB_EVENT_NAME -cne $SourceEvent) { throw 'SourceEvent is not GITHUB_EVENT_NAME' }
  if ($env:GITHUB_REF -cne $SourceRef) { throw 'SourceRef is not GITHUB_REF' }
  if ($env:GITHUB_SHA -cne $GitHubSha) { throw 'GitHubSha is not GITHUB_SHA' }
}
foreach ($output in @($Kit, $EvidenceRoot)) {
  if (Test-Path -LiteralPath $output) { throw "NSIS repack output must not already exist: $output" }
}
if (-not (Test-Path -LiteralPath $DiagnosticsRoot -PathType Container)) {
  throw 'NSIS repack diagnostics root must already exist'
}

function New-PrivateDirectory([string]$Path) {
  & $python scripts\secure_artifact_snapshot.py --prepare-private-directory ([IO.Path]::GetFullPath($Path)) | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "private directory creation failed: $Path" }
}

function Confirm-PrivateDirectory([string]$Path) {
  & $python scripts\secure_artifact_snapshot.py --verify-private-directory ([IO.Path]::GetFullPath($Path)) | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "private directory verification failed: $Path" }
}

function Get-Sha256([string]$Path) {
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$sourceArguments = @(
  '--event-name', $SourceEvent,
  '--source-ref', $SourceRef,
  '--source-sha', $SourceSha,
  '--source-tree', $SourceTree,
  '--source-epoch', "$SourceEpoch",
  '--github-sha', $GitHubSha
)

# Native NTFS ADS gate: both a selected file and selected directory must reject,
# leave no producer state, then the identical live inputs must succeed below.
$renderRoot = Join-Path $repository 'src-tauri\target\x86_64-pc-windows-msvc\release\nsis\x64'
$adsCases = @(
  [PSCustomObject]@{ Name = 'file'; Selected = (Join-Path $renderRoot 'installer.nsi') },
  [PSCustomObject]@{ Name = 'directory'; Selected = $renderRoot }
)
foreach ($case in $adsCases) {
  $adsName = "stock-desk-stage11a2a-$($case.Name)"
  $adsPath = [string]::Concat($case.Selected, ':', $adsName)
  $negativeRoot = Join-Path $env:RUNNER_TEMP "stock-desk-ads-$($case.Name)-$([Guid]::NewGuid().ToString('N'))"
  try {
    [IO.File]::WriteAllText($adsPath, 'native-ads-marker', [Text.UTF8Encoding]::new($false))
    $stream = Get-Item -LiteralPath $case.Selected -Stream $adsName
    if ($stream.Stream -cne $adsName -or [IO.File]::ReadAllText($adsPath) -cne 'native-ads-marker') {
      throw "native Windows $($case.Name) ADS fixture was not created"
    }
    $oldNativePreference = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
    try {
      $negativeOutput = @(& $python scripts\nsis_repack_producer.py prepare-stage --work-root $negativeRoot @sourceArguments 2>&1)
      $negativeExit = $LASTEXITCODE
    } finally {
      $PSNativeCommandUseErrorActionPreference = $oldNativePreference
    }
    if ($negativeExit -eq 0) { throw "producer accepted a real named $($case.Name) ADS" }
    if (($negativeOutput -join "`n") -notmatch 'named alternate data streams are forbidden') {
      throw "producer failed for an unrelated reason instead of the $($case.Name) ADS gate"
    }
    foreach ($forbidden in @(
      (Join-Path $negativeRoot 'descriptor.json'),
      (Join-Path $negativeRoot 'producer-receipt.json'),
      (Join-Path $negativeRoot 'stage')
    )) {
      if (Test-Path -LiteralPath $forbidden) { throw "producer emitted state after ADS rejection: $forbidden" }
    }
  } finally {
    [IO.File]::Delete($adsPath)
    if (Test-Path -LiteralPath $negativeRoot) { Remove-Item -LiteralPath $negativeRoot -Recurse -Force }
  }
}

New-PrivateDirectory $EvidenceRoot
$captureRoot = Join-Path $EvidenceRoot '.private-capture'
$prepareJson = @(& $python scripts\nsis_repack_producer.py prepare-stage --work-root $captureRoot @sourceArguments)
if ($LASTEXITCODE -ne 0) { throw 'strict NSIS producer stage creation failed' }
try { $prepared = ($prepareJson -join "`n") | ConvertFrom-Json }
catch { throw 'strict NSIS producer returned invalid JSON' }
if (
  [string]$prepared.schema -cne 'stock-desk-nsis-repack-producer-summary-v1' -or
  [string]$prepared.stage -cne 'stage' -or
  [string]$prepared.descriptor -cne 'descriptor.json' -or
  [string]$prepared.producer_receipt -cne 'producer-receipt.json'
) { throw 'strict NSIS producer returned an unexpected path summary' }
$stage = Join-Path $captureRoot ([string]$prepared.stage)
$descriptor = Join-Path $captureRoot ([string]$prepared.descriptor)
$original = Join-Path $captureRoot ([string]$prepared.original_candidate)
Confirm-PrivateDirectory $captureRoot
Confirm-PrivateDirectory $stage

$createKitJson = @(& $python scripts\nsis_repack_contract.py create-kit `
  --descriptor $descriptor --source-root $stage --output $Kit `
  --expected-source-ref $SourceRef --expected-source-sha $SourceSha `
  --expected-source-tree $SourceTree --expected-source-epoch $SourceEpoch)
if ($LASTEXITCODE -ne 0) { throw 'content-addressed NSIS repack kit creation failed' }
try { $createKitResult = ($createKitJson -join "`n") | ConvertFrom-Json }
catch { throw 'content-addressed NSIS repack kit returned invalid JSON' }
$expectedKitSha = [string]$createKitResult.kit_sha256
if ($expectedKitSha -cnotmatch '^[0-9a-f]{64}$') { throw 'content-addressed NSIS repack kit returned an invalid kit SHA-256' }
& $python scripts\nsis_repack_contract.py verify-kit --kit $Kit `
  --expected-source-ref $SourceRef --expected-source-sha $SourceSha `
  --expected-source-tree $SourceTree --expected-source-epoch $SourceEpoch `
  --expected-kit-sha256 $expectedKitSha
if ($LASTEXITCODE -ne 0) { throw 'NSIS repack kit verification failed' }

$privateRepack = Join-Path $captureRoot 'repack'
New-PrivateDirectory $privateRepack
$diagnosticRoot = Join-Path $privateRepack 'diagnostic'
New-PrivateDirectory $diagnosticRoot
$diagnosticInstaller = Join-Path $diagnosticRoot 'stock-desk-unsigned-nsis.exe'
$diagnosticJson = @(& $python scripts\nsis_repack_contract.py diagnose-repack-mismatch `
  --kit $Kit --output $diagnosticInstaller `
  --expected-source-ref $SourceRef --expected-source-sha $SourceSha `
  --expected-source-tree $SourceTree --expected-source-epoch $SourceEpoch `
  --expected-kit-sha256 $expectedKitSha)
if ($LASTEXITCODE -ne 0) { throw 'private NSIS diagnostic repack failed' }
try { $diagnostic = ($diagnosticJson -join "`n") | ConvertFrom-Json }
catch { throw 'private NSIS diagnostic repack returned invalid JSON' }
if ([string]$diagnostic.artifact -cne 'stock-desk-nsis-diagnostic-repack-v1') {
  throw 'private NSIS diagnostic repack returned an unexpected artifact'
}
if (-not [bool]$diagnostic.matches_expected) {
  $mismatchRoot = Join-Path $captureRoot 'mismatch-diagnostic'
  $expectedTree = Join-Path $mismatchRoot 'expected'
  $actualTree = Join-Path $mismatchRoot 'actual'
  New-PrivateDirectory $mismatchRoot
  New-PrivateDirectory $expectedTree
  New-PrivateDirectory $actualTree
  & 7z x -bd -y "-o$expectedTree" $original | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'original NSIS mismatch extraction failed' }
  & 7z x -bd -y "-o$actualTree" $diagnosticInstaller | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'repacked NSIS mismatch extraction failed' }
  $diagnosticReportPath = Join-Path $DiagnosticsRoot 'nsis-mismatch-diagnostic.json'
  & $python scripts\nsis_mismatch_diagnostics.py `
    --expected $original --actual $diagnosticInstaller `
    --expected-tree $expectedTree --actual-tree $actualTree `
    --output $diagnosticReportPath
  if ($LASTEXITCODE -ne 0) { throw 'bounded NSIS mismatch diagnostic failed' }
  Write-Host 'BEGIN_NSIS_MISMATCH_DIAGNOSTIC'
  Get-Content -LiteralPath $diagnosticReportPath -Raw | Write-Host
  Write-Host 'END_NSIS_MISMATCH_DIAGNOSTIC'
  throw 'fixed NSIS repack does not reproduce the original unsigned candidate'
}
Remove-Item -LiteralPath $diagnosticRoot -Recurse -Force
if (Test-Path -LiteralPath $diagnosticRoot) { throw 'private diagnostic repack was not removed' }
New-PrivateDirectory (Join-Path $privateRepack 'a')
New-PrivateDirectory (Join-Path $privateRepack 'b')
$first = Join-Path $privateRepack 'a\stock-desk-unsigned-nsis.exe'
$second = Join-Path $privateRepack 'b\stock-desk-unsigned-nsis.exe'
$firstReceipt = Join-Path $EvidenceRoot 'repack-a-receipt.json'
$secondReceipt = Join-Path $EvidenceRoot 'repack-b-receipt.json'
& $python scripts\nsis_repack_contract.py repack --kit $Kit --output $first --receipt $firstReceipt `
  --repack-slot a --expected-source-ref $SourceRef --expected-source-sha $SourceSha `
  --expected-source-tree $SourceTree --expected-source-epoch $SourceEpoch --expected-kit-sha256 $expectedKitSha
if ($LASTEXITCODE -ne 0) { throw 'first fixed NSIS repack failed' }
& $python scripts\nsis_repack_contract.py repack --kit $Kit --output $second --receipt $secondReceipt `
  --repack-slot b --expected-source-ref $SourceRef --expected-source-sha $SourceSha `
  --expected-source-tree $SourceTree --expected-source-epoch $SourceEpoch --expected-kit-sha256 $expectedKitSha
if ($LASTEXITCODE -ne 0) { throw 'second fixed NSIS repack failed' }

$receiptPairs = @(
  [PSCustomObject]@{ Receipt = $firstReceipt; Output = $first; Slot = 'a' },
  [PSCustomObject]@{ Receipt = $secondReceipt; Output = $second; Slot = 'b' }
)
foreach ($pair in $receiptPairs) {
  & $python scripts\nsis_repack_contract.py verify-receipt `
    --receipt $pair.Receipt --kit $Kit --output $pair.Output `
    --expected-repack-slot $pair.Slot --expected-source-ref $SourceRef `
    --expected-source-sha $SourceSha --expected-source-tree $SourceTree `
    --expected-source-epoch $SourceEpoch --expected-kit-sha256 $expectedKitSha
  if ($LASTEXITCODE -ne 0) { throw "fixed NSIS repack receipt verification failed: $($pair.Receipt)" }
}

$expectedHash = Get-Sha256 $original
$firstHash = Get-Sha256 $first
$secondHash = Get-Sha256 $second
if ($firstHash -cne $secondHash) { throw 'independent fixed NSIS repacks are not byte-identical' }
if ($firstHash -cne $expectedHash) { throw 'fixed NSIS repack changed after matching diagnostic compile' }
& $python scripts\nsis_repack_producer.py verify-live-inputs --work-root $captureRoot @sourceArguments | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'producer live-input recheck failed' }
Confirm-PrivateDirectory $privateRepack
Remove-Item -LiteralPath $captureRoot -Recurse -Force
if (Test-Path -LiteralPath $captureRoot) { throw 'private NSIS producer state was not removed' }
