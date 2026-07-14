[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputPath)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $IsWindows) { throw 'UIA driver runtime integration requires Windows' }

function Wait-File {
  param([string]$Path, [int]$TimeoutSeconds = 15)
  $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
  while (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    if ([DateTimeOffset]::UtcNow -ge $deadline) { throw "Timed out waiting for runtime fixture: $Path" }
    Start-Sleep -Milliseconds 25
  }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$driverPath = Join-Path $root 'scripts\windows_desktop_uia_driver.ps1'
if (-not (Test-Path -LiteralPath $driverPath -PathType Leaf)) {
  throw 'Reviewed UIA driver is missing'
}
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $driverPath,
  [ref]$tokens,
  [ref]$errors
)
if ($null -eq $ast -or @($errors).Count -ne 0) {
  throw 'Reviewed UIA driver does not parse on Windows PowerShell'
}
$source = [IO.File]::ReadAllText($driverPath)
$required = @(
  'GetWindowDpiAwarenessContext(IntPtr hwnd)',
  'GetWindowDpiAwarenessContext($hwnd)',
  "Send-Key -Keys '{TAB}'",
  'Observed Tab order differs from the visual control order',
  "Send-Key -Keys '{ESC}'",
  'Escape did not safely close',
  '$script:KeyboardMatrixCheckCount -eq 26',
  '$script:EscapeBehaviorCheckCount -eq 14',
  'Move-FocusToElementByTab',
  'Save-ElementFocusRegion',
  'Write-FocusRegionContactSheet',
  'uia-focused-element-after-real-tab',
  'RuntimeProbe',
  'PrintWindow(IntPtr hwnd, IntPtr dc, uint flags)',
  'ExpectedExecutableSha256'
)
foreach ($text in $required) {
  if (-not $source.Contains($text)) { throw "Reviewed UIA boundary is missing: $text" }
}
foreach ($forbidden in @(
    'GetThreadDpiAwarenessContext',
    'tab_sequence = $visualSequence',
    'pure_keyboard_journey = $true',
    'focus_visible = $true',
    'focus_region_changed = $true',
    '.SetFocus()',
    'CopyFromScreen'
  )) {
  if ($source.Contains($forbidden)) { throw "Reviewed UIA boundary contains a shortcut: $forbidden" }
}

$runtimeRoot = Join-Path $env:RUNNER_TEMP "stock-desk-uia-runtime-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force $runtimeRoot | Out-Null
$parent = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$fixtureSourcePath = Join-Path $runtimeRoot 'uia-fixture.cs'
$fixturePath = Join-Path $runtimeRoot 'uia-fixture.exe'
$readyPath = Join-Path $runtimeRoot 'ready.txt'
$activatedPath = Join-Path $runtimeRoot 'activated.txt'
$stopPath = Join-Path $runtimeRoot 'stop.txt'
$probeRoot = Join-Path $parent 'uia-runtime-probe'
New-Item -ItemType Directory -Path $probeRoot -Force | Out-Null
$fixtureProcess = $null
$fixtureSource = @'
using System;
using System.IO;
using System.Windows.Forms;

internal static class StockDeskUiaRuntimeFixture {
  [STAThread]
  public static int Main(string[] arguments) {
    if (arguments.Length != 3) { return 64; }
    string readyPath = arguments[0];
    string activatedPath = arguments[1];
    string stopPath = arguments[2];
    Application.EnableVisualStyles();
    Form form = new Form {
      Text = "Stock Desk UIA runtime fixture",
      Width = 640,
      Height = 360,
      StartPosition = FormStartPosition.Manual,
      Left = 40,
      Top = 40
    };
    Button initial = new Button { Text = "Runtime probe initial", Left = 40, Top = 60, Width = 220, TabIndex = 0 };
    Button target = new Button { Text = "Runtime probe target", Left = 40, Top = 120, Width = 220, TabIndex = 1 };
    target.Click += delegate {
      target.Text = "Runtime probe activated";
      File.WriteAllText(activatedPath, "activated");
      Form dialog = new Form {
        Text = "Runtime probe dialog",
        Width = 360,
        Height = 180,
        StartPosition = FormStartPosition.CenterParent,
        KeyPreview = true
      };
      Button cancel = new Button { Text = "Runtime probe cancel", Left = 90, Top = 60, Width = 160, TabIndex = 0 };
      dialog.Controls.Add(cancel);
      dialog.KeyDown += delegate(object sender, KeyEventArgs keyEvent) {
        if (keyEvent.KeyCode == Keys.Escape) { dialog.Close(); }
      };
      dialog.Shown += delegate { cancel.Select(); };
      dialog.Show(form);
    };
    form.Controls.Add(initial);
    form.Controls.Add(target);
    Timer timer = new Timer { Interval = 50 };
    timer.Tick += delegate { if (File.Exists(stopPath)) { timer.Stop(); form.Close(); } };
    form.Shown += delegate {
      initial.Select();
      File.WriteAllText(readyPath, form.Handle.ToInt64().ToString());
      timer.Start();
    };
    Application.Run(form);
    return 0;
  }
}
'@

$runtimeResult = $null
try {
  $compilerPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
  if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) { throw 'Pinned Windows C# compiler is unavailable' }
  [IO.File]::WriteAllText($fixtureSourcePath, $fixtureSource, [Text.UTF8Encoding]::new($false))
  & $compilerPath @(
    '/nologo', '/target:winexe',
    '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
    "/out:$fixturePath", $fixtureSourcePath
  )
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $fixturePath -PathType Leaf)) {
    throw 'Controlled UIA runtime fixture did not compile'
  }
  $fixtureArguments = @($readyPath, $activatedPath, $stopPath) | ForEach-Object { '"' + $_ + '"' }
  $fixtureProcess = Start-Process -FilePath $fixturePath -ArgumentList $fixtureArguments -PassThru
  Wait-File -Path $readyPath
  $fixtureHwnd = [long](Get-Content -LiteralPath $readyPath -Raw)
  if ($fixtureHwnd -le 0) { throw 'Controlled UIA fixture HWND is invalid' }
  & $driverPath `
    -WindowHandle $fixtureHwnd `
    -ExpectedProcessId $fixtureProcess.Id `
    -ExpectedExecutableSha256 ((Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash.ToLowerInvariant()) `
    -ExpectedDpiPercent 100 -DataPath primary -ExpectedProvider akshare `
    -NetworkObservationPath (Join-Path $runtimeRoot 'unused-network.json') `
    -OutputRoot $probeRoot -RuntimeProbe
  if ($LASTEXITCODE -ne 0) { throw 'Reviewed UIA driver runtime probe failed' }
  Wait-File -Path $activatedPath
  $runtimeResultPath = Join-Path $probeRoot 'driver-result.json'
  $runtimeActionsPath = Join-Path $probeRoot 'uia-actions.json'
  $runtimeTreePath = Join-Path $probeRoot 'uia-tree.json'
  $runtimeCapturePath = Join-Path $probeRoot 'runtime-probe-window.png'
  $runtimeFocusContactPath = Join-Path $probeRoot 'focus-region-contact-sheet.png'
  foreach ($path in @($runtimeResultPath, $runtimeActionsPath, $runtimeTreePath, $runtimeCapturePath, $runtimeFocusContactPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Runtime probe output is missing: $path" }
  }
  $runtimeResult = Get-Content -LiteralPath $runtimeResultPath -Raw | ConvertFrom-Json
  $runtimeActions = @(Get-Content -LiteralPath $runtimeActionsPath -Raw | ConvertFrom-Json)
  if (
    $runtimeResult.schema -ne 'stock-desk-windows-uia-driver-runtime-probe-v1' -or
    $runtimeResult.raw_only -ne $true -or $runtimeResult.real_vm_acceptance -ne $false -or
    $runtimeResult.candidate.pid -ne $fixtureProcess.Id -or
    $runtimeResult.candidate.hwnd -ne $fixtureHwnd -or
    $runtimeResult.target_hwnd_dpi_context_observed -ne $true -or
    $runtimeResult.actual_tab_activation_observed -ne $true -or
    $runtimeResult.actual_escape_close_observed -ne $true -or
    $runtimeResult.focus_observation_method -ne 'uia-focused-element-after-real-tab' -or
    $runtimeResult.focus_path.tab_input_count -lt 1 -or
    $runtimeResult.focus_path.target_has_keyboard_focus -ne $true -or
    $runtimeResult.focus_path.focus_region_changed -ne $true -or
    $runtimeResult.focus_path.focused_region_id -ceq $runtimeResult.focus_path.unfocused_region_id -or
    $runtimeResult.focus_region_contact_sheet_sha256 -cne ((Get-FileHash -LiteralPath $runtimeFocusContactPath -Algorithm SHA256).Hash.ToLowerInvariant()) -or
    $runtimeResult.focus_path.activated -ne $true -or
    $runtimeActions.Count -ne 1 -or $runtimeActions[0].action -ne 'keyboard-enter'
  ) { throw 'UIA runtime probe did not execute the reviewed real-Tab focus path' }
} finally {
  [IO.File]::WriteAllText($stopPath, 'stop')
  if ($fixtureProcess -and -not $fixtureProcess.HasExited) {
    if (-not $fixtureProcess.WaitForExit(5000)) { Stop-Process -Id $fixtureProcess.Id -Force }
  }
}

$tree = (git -C $root rev-parse 'HEAD^{tree}').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve exact source tree' }
$receipt = [ordered]@{
  schema = 'stock-desk-windows-uia-driver-integration-v1'
  evidence_kind = 'github-hosted-contract-not-real-vm'
  source_sha = $env:SOURCE_SHA
  source_tree = $tree
  workflow_ref = $env:GITHUB_WORKFLOW_REF
  workflow_sha = $env:GITHUB_WORKFLOW_SHA
  event_name = $env:GITHUB_EVENT_NAME
  run_id = [long]$env:GITHUB_RUN_ID
  run_attempt = [int]$env:GITHUB_RUN_ATTEMPT
  job_id = $env:GITHUB_JOB
  uia_driver_sha256 = (Get-FileHash -LiteralPath $driverPath -Algorithm SHA256).Hash.ToLowerInvariant()
  parsed_on_windows = $true
  executed_on_windows = $true
  controlled_uia_fixture = $true
  target_hwnd_dpi_contract = $true
  real_tab_input_contract = [bool]$runtimeResult.actual_tab_activation_observed
  real_escape_input_contract = [bool]$runtimeResult.actual_escape_close_observed
  actual_tab_activation_observed = [bool]$runtimeResult.actual_tab_activation_observed
  actual_escape_close_observed = [bool]$runtimeResult.actual_escape_close_observed
  focus_observation_method = [string]$runtimeResult.focus_observation_method
  focus_region_changed = [bool]$runtimeResult.focus_path.focus_region_changed
  unfocused_region_id = [string]$runtimeResult.focus_path.unfocused_region_id
  focused_region_id = [string]$runtimeResult.focus_path.focused_region_id
  focus_region_contact_sheet_sha256 = (Get-FileHash -LiteralPath (Join-Path $probeRoot 'focus-region-contact-sheet.png') -Algorithm SHA256).Hash.ToLowerInvariant()
  runtime_probe_sha256 = (Get-FileHash -LiteralPath (Join-Path $probeRoot 'driver-result.json') -Algorithm SHA256).Hash.ToLowerInvariant()
  runtime_actions_sha256 = (Get-FileHash -LiteralPath (Join-Path $probeRoot 'uia-actions.json') -Algorithm SHA256).Hash.ToLowerInvariant()
  runtime_tree_sha256 = (Get-FileHash -LiteralPath (Join-Path $probeRoot 'uia-tree.json') -Algorithm SHA256).Hash.ToLowerInvariant()
  target_window_capture_sha256 = (Get-FileHash -LiteralPath (Join-Path $probeRoot 'runtime-probe-window.png') -Algorithm SHA256).Hash.ToLowerInvariant()
  raw_only = $true
  real_vm_acceptance = $false
}
[IO.File]::WriteAllText(
  $OutputPath,
  (($receipt | ConvertTo-Json -Depth 5) + [Environment]::NewLine),
  [Text.UTF8Encoding]::new($false)
)

Remove-Item -LiteralPath $runtimeRoot -Recurse -Force -ErrorAction SilentlyContinue

exit 0
