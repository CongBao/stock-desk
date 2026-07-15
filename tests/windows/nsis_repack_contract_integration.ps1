param(
  [Parameter(Mandatory = $true)] [string]$RenderedRoot,
  [Parameter(Mandatory = $true)] [string]$NsisRoot,
  [Parameter(Mandatory = $true)] [string]$TauriConfig,
  [Parameter(Mandatory = $true)] [string]$NsisTemplate,
  [Parameter(Mandatory = $true)] [string]$ExpectedInstaller,
  [Parameter(Mandatory = $true)] [string]$Kit,
  [Parameter(Mandatory = $true)] [string]$EvidenceRoot,
  [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-f]{40}$')] [string]$SourceSha,
  [Parameter(Mandatory = $true)] [ValidatePattern('^[0-9a-f]{40}$')] [string]$SourceTree,
  [Parameter(Mandatory = $true)] [ValidateRange(1, [long]::MaxValue)] [long]$SourceEpoch
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
if (-not $IsWindows) { throw 'the NSIS repack integration requires Windows' }

$repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $repository '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'the locked Python environment is missing' }
$render = (Resolve-Path -LiteralPath $RenderedRoot).Path
$nsis = (Resolve-Path -LiteralPath $NsisRoot).Path
$config = (Resolve-Path -LiteralPath $TauriConfig).Path
$template = (Resolve-Path -LiteralPath $NsisTemplate).Path
$originalSource = (Resolve-Path -LiteralPath $ExpectedInstaller).Path
$renderedScript = Join-Path $render 'installer.nsi'
foreach ($required in @($renderedScript, $config, $template, $originalSource, (Join-Path $nsis 'makensis.exe'))) {
  if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "required NSIS repack input is missing: $required" }
}
foreach ($output in @($Kit, $EvidenceRoot)) {
  if (Test-Path -LiteralPath $output) { throw "NSIS repack output must not already exist: $output" }
}

function New-PrivateDirectory([string]$Path) {
  & $python scripts\secure_artifact_snapshot.py --prepare-private-directory ([IO.Path]::GetFullPath($Path)) | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "private directory creation failed: $Path" }
}

function Confirm-PrivateDirectory([string]$Path) {
  & $python scripts\secure_artifact_snapshot.py --verify-private-directory ([IO.Path]::GetFullPath($Path)) | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "private directory verification failed: $Path" }
}

New-PrivateDirectory $EvidenceRoot
$captureRoot = Join-Path $EvidenceRoot '.private-capture'
$stage = Join-Path $captureRoot 'stage'
$snapshots = Join-Path $captureRoot 'snapshots'
New-PrivateDirectory $captureRoot
New-PrivateDirectory $stage
New-PrivateDirectory $snapshots
$snapshotNumber = 0

function Get-Sha256([string]$Path) {
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Copy-PrivateSnapshot(
  [string]$SourceRoot,
  [string[]]$Entries,
  [string]$TargetPrefix
) {
  $script:snapshotNumber += 1
  $snapshot = Join-Path $snapshots ("snapshot-{0:D4}" -f $script:snapshotNumber)
  $arguments = @('scripts\secure_artifact_snapshot.py', '--source-root', $SourceRoot, '--destination', $snapshot)
  foreach ($entry in $Entries) { $arguments += @('--entry', $entry.Replace('\', '/')) }
  & $python @arguments | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "secure artifact snapshot failed for $SourceRoot" }
  foreach ($file in Get-ChildItem -LiteralPath $snapshot -Recurse -File) {
    $relative = [IO.Path]::GetRelativePath($snapshot, $file.FullName)
    $destination = if ($TargetPrefix) { Join-Path $stage (Join-Path $TargetPrefix $relative) } else { Join-Path $stage $relative }
    New-Item -ItemType Directory -Force (Split-Path $destination -Parent) | Out-Null
    if (Test-Path -LiteralPath $destination) {
      if ((Get-Sha256 $destination) -cne (Get-Sha256 $file.FullName)) { throw "snapshot target collision: $destination" }
    } else {
      Copy-Item -LiteralPath $file.FullName -Destination $destination
    }
  }
}

$hardlinkSource = Join-Path $captureRoot 'hardlink-source'
New-PrivateDirectory $hardlinkSource
$hardlinkOriginal = Join-Path $hardlinkSource 'original.bin'
$hardlinkAlias = Join-Path $hardlinkSource 'alias.bin'
[IO.File]::WriteAllBytes($hardlinkOriginal, [Text.Encoding]::UTF8.GetBytes('native-windows-hardlink'))
New-Item -ItemType HardLink -Path $hardlinkAlias -Target $hardlinkOriginal | Out-Null
$hardlinkSnapshot = Join-Path $snapshots 'hardlink-contract'
& $python scripts\secure_artifact_snapshot.py `
  --source-root $hardlinkSource `
  --destination $hardlinkSnapshot `
  --entry 'original.bin' `
  --entry 'alias.bin' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'native Windows hardlink snapshot contract failed' }
$expectedHardlinkHash = Get-Sha256 $hardlinkOriginal
foreach ($name in @('original.bin','alias.bin')) {
  if ((Get-Sha256 (Join-Path $hardlinkSnapshot $name)) -cne $expectedHardlinkHash) {
    throw "native Windows hardlink snapshot content mismatch: $name"
  }
}
[IO.File]::WriteAllBytes($hardlinkAlias, [Text.Encoding]::UTF8.GetBytes('mutated-source-alias'))
foreach ($name in @('original.bin','alias.bin')) {
  if ((Get-Sha256 (Join-Path $hardlinkSnapshot $name)) -cne $expectedHardlinkHash) {
    throw "native Windows hardlink snapshot retained a source link: $name"
  }
}

$renderEntries = @(Get-ChildItem -LiteralPath $render -Force | ForEach-Object Name)
$toolEntries = @(Get-ChildItem -LiteralPath $nsis -Force | ForEach-Object Name)
if (-not $renderEntries -or -not $toolEntries) { throw 'rendered NSIS or toolchain root is empty' }
Copy-PrivateSnapshot $render $renderEntries ''
Copy-PrivateSnapshot $nsis $toolEntries 'toolchain'
Copy-PrivateSnapshot (Split-Path $config -Parent) @((Split-Path $config -Leaf)) 'source'
Copy-PrivateSnapshot (Split-Path $template -Parent) @((Split-Path $template -Leaf)) 'template'
$snapshotNumber += 1
$originalSnapshot = Join-Path $snapshots ("snapshot-{0:D4}" -f $snapshotNumber)
& $python scripts\secure_artifact_snapshot.py `
  --source-root (Split-Path $originalSource -Parent) `
  --destination $originalSnapshot `
  --entry (Split-Path $originalSource -Leaf) | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'original unsigned installer snapshot failed' }
$original = Join-Path $originalSnapshot (Split-Path $originalSource -Leaf)

$scriptText = Get-Content -LiteralPath (Join-Path $stage 'installer.nsi') -Raw
$quotedAbsolute = [regex]'(?<quote>["''])(?<path>(?:[A-Za-z]:[\\/]|\\\\)[^"''\r\n]*?)\k<quote>'
$absoluteValues = @($quotedAbsolute.Matches($scriptText) | ForEach-Object { $_.Groups['path'].Value } | Sort-Object -Unique)
if (-not $absoluteValues) { throw 'rendered installer.nsi did not contain expected absolute Tauri inputs' }
$mappings = @()
$mappedRoles = @{}
$mappingIndex = 0
foreach ($source in $absoluteValues) {
  $mappingIndex += 1
  $occurrences = 0
  $position = 0
  while (($position = $scriptText.IndexOf($source, $position, [StringComparison]::Ordinal)) -ge 0) {
    $occurrences += 1
    $position += $source.Length
  }
  if ($occurrences -lt 1) { throw "absolute mapping was not observed: $source" }
  if ([IO.Path]::GetFullPath($source) -ieq [IO.Path]::GetFullPath($originalSource)) {
    throw 'rendered installer.nsi unexpectedly references the final bundle path'
  } elseif (Test-Path -LiteralPath $source -PathType Container) {
    $relativeToToolchain = [IO.Path]::GetRelativePath($nsis, $source)
    if ($relativeToToolchain -eq '..' -or $relativeToToolchain.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) {
      $externalPlugins = @(Get-ChildItem -LiteralPath $source -Recurse -File)
      if (-not ($externalPlugins | Where-Object Name -IEQ 'nsis_tauri_utils.dll')) {
        throw "unexpected absolute NSIS directory outside the pinned toolchain: $source"
      }
      Copy-PrivateSnapshot $source @((Get-ChildItem -LiteralPath $source -Force | ForEach-Object Name)) 'toolchain/Plugins/x86-unicode/additional'
      $target = 'toolchain/Plugins/x86-unicode/additional'
    } else {
      $target = ('toolchain/' + $relativeToToolchain.Replace('\', '/')).TrimEnd('/')
    }
  } elseif (Test-Path -LiteralPath $source -PathType Leaf) {
    $relativeToRender = [IO.Path]::GetRelativePath($render, $source)
    $relativeToToolchain = [IO.Path]::GetRelativePath($nsis, $source)
    if ($relativeToRender -ne '..' -and -not $relativeToRender.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) {
      $target = $relativeToRender.Replace('\', '/')
    } elseif ($relativeToToolchain -ne '..' -and -not $relativeToToolchain.StartsWith("..$([IO.Path]::DirectorySeparatorChar)")) {
      $target = 'toolchain/' + $relativeToToolchain.Replace('\', '/')
    } else {
      $prefix = "captured/{0:D3}" -f $mappingIndex
      Copy-PrivateSnapshot (Split-Path $source -Parent) @((Split-Path $source -Leaf)) $prefix
      $target = "$prefix/$(Split-Path $source -Leaf)"
    }
    $name = (Split-Path $target -Leaf).ToLowerInvariant()
    if ($name -match 'webview2.*(installer|setup).*\.exe$') { $mappedRoles[$target] = 'webview2' }
    elseif ([IO.Path]::GetExtension($name) -in @('.ico', '.bmp', '.png')) { $mappedRoles[$target] = 'icon' }
    elseif ($name -eq 'installer-hooks.nsh') { $mappedRoles[$target] = 'nsis-hook' }
    elseif ($name -match '^(english|simpchinese)\.nsh$') { $mappedRoles[$target] = 'nsis-language' }
    elseif ([IO.Path]::GetExtension($name) -eq '.nsh') { $mappedRoles[$target] = 'nsis-include' }
    else { $mappedRoles[$target] = 'payload' }
  } else {
    throw "absolute Tauri input does not exist: $source"
  }
  $mappings += [ordered]@{source_absolute=$source;target=$target;occurrences=$occurrences}
}

$pluginNames = @($scriptText -split "`r?`n" | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z][A-Za-z0-9_.-]*)::') { $Matches[1] }
} | Sort-Object -Unique)
if ('nsis_tauri_utils' -notin $pluginNames) { throw 'rendered NSIS script does not invoke nsis_tauri_utils' }
$pluginPaths = @{}
foreach ($plugin in $pluginNames) {
  $candidates = @(Get-ChildItem -LiteralPath (Join-Path $stage 'toolchain\Plugins\x86-unicode') -Recurse -File -Filter "$plugin.dll")
  if ($candidates.Count -ne 1) { throw "expected one pinned x86-unicode plugin for $plugin, found $($candidates.Count)" }
  $pluginPaths[$plugin] = [IO.Path]::GetRelativePath($stage, $candidates[0].FullName).Replace('\', '/')
}

$records = @()
foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File) {
  $relative = [IO.Path]::GetRelativePath($stage, $file.FullName).Replace('\', '/')
  $lower = $relative.ToLowerInvariant()
  if ($relative -eq 'installer.nsi') { $role = 'nsis-rendered-script' }
  elseif ($relative -eq 'source/tauri.conf.json') { $role = 'tauri-config' }
  elseif ($relative -eq 'template/installer.nsi') { $role = 'nsis-template' }
  elseif ($pluginPaths.Values -contains $relative) { $role = 'nsis-plugin' }
  elseif ($lower.StartsWith('toolchain/')) { $role = 'nsis-toolchain' }
  elseif ($mappedRoles.ContainsKey($relative)) { $role = $mappedRoles[$relative] }
  elseif ($lower.EndsWith('installer-hooks.nsh')) { $role = 'nsis-hook' }
  elseif ($lower -match '/(english|simpchinese)\.nsh$') { $role = 'nsis-language' }
  elseif ($lower.EndsWith('.nsh')) { $role = 'nsis-include' }
  elseif ([IO.Path]::GetExtension($lower) -in @('.ico', '.bmp', '.png')) { $role = 'icon' }
  elseif ($lower -match 'webview2.*(installer|setup).*\.exe$') { $role = 'webview2' }
  else { $role = 'payload' }
  $records += [ordered]@{
    path=$relative;role=$role;size=$file.Length;sha256=(Get-Sha256 $file.FullName)
    executable=($relative -eq 'toolchain/makensis.exe')
  }
}

$compiler = Join-Path $stage 'toolchain\makensis.exe'
$plugins = @($pluginNames | ForEach-Object {
  $pluginPath = $pluginPaths[$_]
  [ordered]@{name=$_;path=$pluginPath;sha256=(Get-Sha256 (Join-Path $stage $pluginPath))}
})
$descriptor = [ordered]@{
  schema_version=1;source_sha=$SourceSha;source_tree=$SourceTree;source_epoch=$SourceEpoch
  toolchain=[ordered]@{
    path='toolchain/makensis.exe';sha256=(Get-Sha256 $compiler)
    tauri_cli_version='2.11.4';nsis_version='3.11';nsis_tauri_utils_version='0.5.3';plugins=$plugins
  }
  argv=@('-INPUTCHARSET','UTF8','-OUTPUTCHARSET','UTF8','-V3','installer.nsi')
  environment=[ordered]@{SOURCE_DATE_EPOCH="$SourceEpoch"}
  cleared_environment=@('NSISCONFDIR','NSISDIR')
  files=@($records | Sort-Object { $_.path })
  expected_unsigned_installer=[ordered]@{path='nsis-output.exe';size=(Get-Item $original).Length;sha256=(Get-Sha256 $original)}
  path_mappings=@($mappings | Sort-Object { $_.target })
}
$descriptorPath = Join-Path $captureRoot 'descriptor.json'
$descriptor | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $descriptorPath -Encoding utf8NoBOM
Confirm-PrivateDirectory $EvidenceRoot
Confirm-PrivateDirectory $captureRoot
Confirm-PrivateDirectory $stage
Confirm-PrivateDirectory $snapshots

$createKitJson = @(& $python scripts\nsis_repack_contract.py create-kit --descriptor $descriptorPath --source-root $stage --output $Kit --expected-source-sha $SourceSha --expected-source-tree $SourceTree)
if ($LASTEXITCODE -ne 0) { throw 'content-addressed NSIS repack kit creation failed' }
try { $createKitResult = ($createKitJson -join "`n") | ConvertFrom-Json }
catch { throw 'content-addressed NSIS repack kit returned invalid JSON' }
$expectedKitSha = [string]$createKitResult.kit_sha256
if ($expectedKitSha -cnotmatch '^[0-9a-f]{64}$') { throw 'content-addressed NSIS repack kit returned an invalid kit SHA-256' }
& $python scripts\nsis_repack_contract.py verify-kit --kit $Kit --expected-source-sha $SourceSha --expected-source-tree $SourceTree --expected-kit-sha256 $expectedKitSha
if ($LASTEXITCODE -ne 0) { throw 'NSIS repack kit verification failed' }

$privateRepack = Join-Path $captureRoot 'repack'
New-PrivateDirectory $privateRepack
New-PrivateDirectory (Join-Path $privateRepack 'a')
New-PrivateDirectory (Join-Path $privateRepack 'b')
$first = Join-Path $privateRepack 'a\stock-desk-unsigned-nsis.exe'
$second = Join-Path $privateRepack 'b\stock-desk-unsigned-nsis.exe'
$firstReceipt = Join-Path $EvidenceRoot 'repack-a-receipt.json'
$secondReceipt = Join-Path $EvidenceRoot 'repack-b-receipt.json'
& $python scripts\nsis_repack_contract.py repack --kit $Kit --output $first --receipt $firstReceipt --expected-source-sha $SourceSha --expected-source-tree $SourceTree --expected-kit-sha256 $expectedKitSha
if ($LASTEXITCODE -ne 0) { throw 'first fixed NSIS repack failed' }
& $python scripts\nsis_repack_contract.py repack --kit $Kit --output $second --receipt $secondReceipt --expected-source-sha $SourceSha --expected-source-tree $SourceTree --expected-kit-sha256 $expectedKitSha
if ($LASTEXITCODE -ne 0) { throw 'second fixed NSIS repack failed' }

$receiptPairs = @(
  [PSCustomObject]@{ Receipt = $firstReceipt; Output = $first },
  [PSCustomObject]@{ Receipt = $secondReceipt; Output = $second }
)
foreach ($pair in $receiptPairs) {
  & $python scripts\nsis_repack_contract.py verify-receipt `
    --receipt $pair.Receipt --kit $Kit --output $pair.Output `
    --expected-source-sha $SourceSha --expected-source-tree $SourceTree `
    --expected-kit-sha256 $expectedKitSha
  if ($LASTEXITCODE -ne 0) { throw "fixed NSIS repack receipt verification failed: $($pair.Receipt)" }
}
Confirm-PrivateDirectory $privateRepack

$expectedHash = Get-Sha256 $original
$firstHash = Get-Sha256 $first
$secondHash = Get-Sha256 $second
if ($firstHash -cne $secondHash) { throw 'independent fixed NSIS repacks are not byte-identical' }
if ($firstHash -cne $expectedHash) { throw 'fixed NSIS repack does not reproduce the original unsigned candidate' }
Remove-Item -LiteralPath $captureRoot -Recurse -Force
