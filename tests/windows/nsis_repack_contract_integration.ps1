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

function Test-RelativeChild([string]$Relative) {
  if ([IO.Path]::IsPathRooted($Relative) -or $Relative -eq '..') { return $false }
  return -not $Relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)", [StringComparison]::Ordinal)
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
$originalSourceIdentityBefore = [ordered]@{
  size=(Get-Item -LiteralPath $originalSource).Length
  sha256=(Get-Sha256 $originalSource)
}
$snapshotNumber += 1
$originalSnapshot = Join-Path $snapshots ("snapshot-{0:D4}" -f $snapshotNumber)
& $python scripts\secure_artifact_snapshot.py `
  --source-root (Split-Path $originalSource -Parent) `
  --destination $originalSnapshot `
  --entry (Split-Path $originalSource -Leaf) | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'original unsigned installer snapshot failed' }
$original = Join-Path $originalSnapshot (Split-Path $originalSource -Leaf)
$originalSnapshotIdentity = [ordered]@{
  size=(Get-Item -LiteralPath $original).Length
  sha256=(Get-Sha256 $original)
}
if (
  $originalSnapshotIdentity.size -ne $originalSourceIdentityBefore.size -or
  $originalSnapshotIdentity.sha256 -cne $originalSourceIdentityBefore.sha256
) { throw 'original unsigned candidate snapshot does not match pre-capture identity' }

$scriptText = Get-Content -LiteralPath (Join-Path $stage 'installer.nsi') -Raw
$additionalPluginDefines = @([regex]::Matches(
  $scriptText,
  '(?m)^\s*!define\s+ADDITIONALPLUGINSPATH\s+"(?<path>[^"\r\n]+)"\s*$'
))
if ($additionalPluginDefines.Count -ne 1) {
  throw "rendered installer.nsi must define ADDITIONALPLUGINSPATH exactly once, found $($additionalPluginDefines.Count)"
}
$renderedAdditionalPlugins = $additionalPluginDefines[0].Groups['path'].Value
if (-not [IO.Path]::IsPathRooted($renderedAdditionalPlugins)) {
  throw 'rendered ADDITIONALPLUGINSPATH must be absolute'
}
$expectedLiveAdditionalPlugins = Join-Path $nsis 'Plugins\x86-unicode\additional'
if (
  [IO.Path]::GetFullPath($renderedAdditionalPlugins) -ine
  [IO.Path]::GetFullPath($expectedLiveAdditionalPlugins)
) { throw 'rendered ADDITIONALPLUGINSPATH does not belong to the exact NSIS tree' }
if (
  @([regex]::Matches(
    $scriptText,
    '(?m)^\s*!addplugindir\s+"\$\{ADDITIONALPLUGINSPATH\}"\s*$'
  )).Count -ne 1 -or
  $scriptText -cnotmatch '(?m)^\s*nsis_tauri_utils::'
) { throw 'rendered installer.nsi does not bind the verified additional plugin root' }
$stagedToolchain = Join-Path $stage 'toolchain'
$stagedAdditionalPlugins = Join-Path $stagedToolchain 'Plugins\x86-unicode\additional'
Confirm-PrivateDirectory $stage
$verifiedToolchainJson = @(& $python scripts\nsis_repack_contract.py `
  verify-extracted-toolchain `
  --nsis-root $stagedToolchain `
  --additional-plugins-root $stagedAdditionalPlugins)
if ($LASTEXITCODE -ne 0) { throw 'private staged NSIS toolchain verification failed' }
try { $verifiedToolchain = ($verifiedToolchainJson -join "`n") | ConvertFrom-Json }
catch { throw 'private staged NSIS toolchain verifier returned invalid JSON' }
$verifiedCompiler = [IO.Path]::GetFullPath([string]$verifiedToolchain.compiler)
if ($verifiedCompiler -cne [IO.Path]::GetFullPath((Join-Path $stagedToolchain 'makensis.exe'))) {
  throw 'verified NSIS compiler does not belong to the private staged toolchain'
}
if (
  [string]$verifiedToolchain.tree.algorithm -cne 'stock-desk-nsis-toolchain-tree-v1' -or
  [long]$verifiedToolchain.tree.file_count -ne 442 -or
  [long]$verifiedToolchain.tree.total_size -ne 7168591 -or
  [string]$verifiedToolchain.tree.sha256 -cne '1baa63462557de9a7bdd3ef13534faf3ff38671f960de6ce30a87c5df5ec7866'
) { throw 'verified private NSIS toolchain identity is not the repository lock' }
$mainBinaryDefines = @([regex]::Matches(
  $scriptText,
  '(?m)^\s*!define\s+MAINBINARYSRCPATH\s+"(?<path>[^"\r\n]+)"\s*$'
))
if ($mainBinaryDefines.Count -ne 1) {
  throw "rendered installer.nsi must define MAINBINARYSRCPATH exactly once, found $($mainBinaryDefines.Count)"
}
$mainBinarySource = $mainBinaryDefines[0].Groups['path'].Value
if (-not [IO.Path]::IsPathRooted($mainBinarySource)) {
  throw 'rendered MAINBINARYSRCPATH must be absolute'
}
$mainBinarySource = [IO.Path]::GetFullPath($mainBinarySource)
if (-not (Test-Path -LiteralPath $mainBinarySource -PathType Leaf)) {
  throw 'rendered MAINBINARYSRCPATH does not identify one restored host binary'
}
$mainPathOccurrences = 0
$mainPathPosition = 0
while (($mainPathPosition = $scriptText.IndexOf($mainBinarySource, $mainPathPosition, [StringComparison]::Ordinal)) -ge 0) {
  $mainPathOccurrences += 1
  $mainPathPosition += $mainBinarySource.Length
}
if ($mainPathOccurrences -ne 1) {
  throw "rendered MAINBINARYSRCPATH value must occur exactly once, found $mainPathOccurrences"
}

$mainBinarySourceIdentityBefore = [ordered]@{
  size=(Get-Item -LiteralPath $mainBinarySource).Length
  sha256=(Get-Sha256 $mainBinarySource)
}
$patchedPayloadRelative = 'captured/main-binary-nss.exe'
Copy-PrivateSnapshot `
  (Split-Path $mainBinarySource -Parent) `
  @((Split-Path $mainBinarySource -Leaf)) `
  'captured/main-binary-source'
$unpatchedPayload = Join-Path $stage (Join-Path 'captured/main-binary-source' (Split-Path $mainBinarySource -Leaf))
$mainBinarySnapshotIdentity = [ordered]@{
  size=(Get-Item -LiteralPath $unpatchedPayload).Length
  sha256=(Get-Sha256 $unpatchedPayload)
}
if (
  $mainBinarySnapshotIdentity.size -ne $mainBinarySourceIdentityBefore.size -or
  $mainBinarySnapshotIdentity.sha256 -cne $mainBinarySourceIdentityBefore.sha256
) { throw 'private host snapshot does not match pre-capture identity' }
$patchedPayload = Join-Path $stage $patchedPayloadRelative
if (Test-Path -LiteralPath $patchedPayload) { throw 'private NSS-patched payload already exists' }
Copy-Item -LiteralPath $unpatchedPayload -Destination $patchedPayload
$payloadPatchJson = @(& $python scripts\nsis_repack_contract.py `
  patch-tauri-bundle-payload `
  --private-root $stage `
  --payload $patchedPayload)
if ($LASTEXITCODE -ne 0) { throw 'private Tauri NSS payload reconstruction failed' }
try { $payloadPatch = ($payloadPatchJson -join "`n") | ConvertFrom-Json }
catch { throw 'private Tauri NSS payload reconstruction returned invalid JSON' }
if (
  [string]$payloadPatch.algorithm -cne 'tauri-bundle-type-unk-to-nss-v1' -or
  [long]$payloadPatch.marker_offset -lt 0 -or
  [long]$payloadPatch.before.size -ne [long]$payloadPatch.after.size -or
  [long]$payloadPatch.after.size -ne (Get-Item -LiteralPath $patchedPayload).Length -or
  [string]$payloadPatch.after.sha256 -cne (Get-Sha256 $patchedPayload)
) { throw 'private Tauri NSS payload reconstruction identity is invalid' }
Write-Host ('Tauri NSS payload reconstruction: ' + ($payloadPatch | ConvertTo-Json -Depth 5 -Compress))
if (
  [long]$payloadPatch.before.size -ne [long]$mainBinarySourceIdentityBefore.size -or
  [string]$payloadPatch.before.sha256 -cne [string]$mainBinarySourceIdentityBefore.sha256
) {
  throw 'private patched host source does not match pre-capture identity'
}
Remove-Item -LiteralPath $unpatchedPayload -Force
$unpatchedPayloadParent = Split-Path $unpatchedPayload -Parent
if (@(Get-ChildItem -LiteralPath $unpatchedPayloadParent -Force).Count -ne 0) {
  throw 'private unpatched payload staging directory is not empty after removal'
}
Remove-Item -LiteralPath $unpatchedPayloadParent -Force

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
  } elseif ([IO.Path]::GetFullPath($source) -ieq $mainBinarySource) {
    $target = $patchedPayloadRelative
    $mappedRoles[$target] = 'payload'
  } elseif (Test-Path -LiteralPath $source -PathType Container) {
    $relativeToToolchain = [IO.Path]::GetRelativePath($nsis, $source)
    if (-not (Test-RelativeChild $relativeToToolchain)) {
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
    if (Test-RelativeChild $relativeToRender) {
      $target = $relativeToRender.Replace('\', '/')
    } elseif (Test-RelativeChild $relativeToToolchain) {
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

# Parent and child absolute paths may both be rendered. Count replacements in
# longest-source-first order so the descriptor binds each exact occurrence once.
$remainingScript = $scriptText
$orderedMappings = @($mappings | Sort-Object `
  @{Expression = { $_.source_absolute.Length }; Descending = $true}, `
  @{Expression = { $_.source_absolute }; Descending = $false})
foreach ($mapping in $orderedMappings) {
  $occurrences = 0
  $position = 0
  while (($position = $remainingScript.IndexOf($mapping.source_absolute, $position, [StringComparison]::Ordinal)) -ge 0) {
    $occurrences += 1
    $position += $mapping.source_absolute.Length
  }
  if ($occurrences -lt 1) { throw 'an absolute path mapping was shadowed before normalization' }
  $mapping.occurrences = $occurrences
  $marker = "@STOCK_DESK_PATH_MAP[$($mapping.target)]@"
  $remainingScript = $remainingScript.Replace($mapping.source_absolute, $marker, [StringComparison]::Ordinal)
}
$mappings = $orderedMappings

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

$compiler = $verifiedCompiler
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
$originalSourceIdentityAfter = [ordered]@{
  size=(Get-Item -LiteralPath $originalSource).Length
  sha256=(Get-Sha256 $originalSource)
}
if (
  $originalSourceIdentityAfter.size -ne $originalSourceIdentityBefore.size -or
  $originalSourceIdentityAfter.sha256 -cne $originalSourceIdentityBefore.sha256
) { throw 'original unsigned candidate changed during private NSIS reconstruction' }
$mainBinarySourceIdentityAfter = [ordered]@{
  size=(Get-Item -LiteralPath $mainBinarySource).Length
  sha256=(Get-Sha256 $mainBinarySource)
}
if (
  $mainBinarySourceIdentityAfter.size -ne $mainBinarySourceIdentityBefore.size -or
  $mainBinarySourceIdentityAfter.sha256 -cne $mainBinarySourceIdentityBefore.sha256
) { throw 'workspace host binary changed during NSIS kit and repack work' }
Remove-Item -LiteralPath $captureRoot -Recurse -Force
