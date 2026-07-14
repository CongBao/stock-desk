[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $IsWindows) {
  Write-Host 'SKIP: Windows browser observer integration requires Windows.'
  exit 0
}

function Wait-Path {
  param([Parameter(Mandatory = $true)][string]$Path, [int]$TimeoutSeconds = 15)
  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  while (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    if ([DateTime]::UtcNow -ge $deadline) { throw "Timed out waiting for fixture path: $Path" }
    Start-Sleep -Milliseconds 25
  }
}

function Get-FileSha256 {
  param([Parameter(Mandatory = $true)][string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-StringSha256 {
  param([Parameter(Mandatory = $true)][string]$Value)
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    return [BitConverter]::ToString(
      $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))
    ).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
  }
}

$expectedSha = if ($env:SOURCE_SHA) { $env:SOURCE_SHA } else { $env:GITHUB_SHA }
if ($expectedSha -cnotmatch '^[0-9a-f]{40}$') { throw 'SOURCE_SHA must be an exact 40-hex commit' }
$workflowSha = [string]$env:GITHUB_WORKFLOW_SHA
$workflowRef = [string]$env:GITHUB_WORKFLOW_REF
if ($workflowSha -cnotmatch '^[0-9a-f]{40}$' -or [string]::IsNullOrWhiteSpace($workflowRef)) {
  throw 'GitHub workflow identity is unavailable'
}
if ($env:GITHUB_EVENT_NAME -eq 'push' -and $workflowSha -cne $expectedSha) {
  throw 'Main observer integration workflow is not the exact source SHA'
}
if ($env:GITHUB_RUN_ID -cnotmatch '^[1-9][0-9]*$' -or $env:GITHUB_RUN_ATTEMPT -cnotmatch '^[1-9][0-9]*$') {
  throw 'Observer integration requires an exact GitHub run identity'
}
if ($env:GITHUB_JOB -cne 'windows-browser-observer') { throw 'Observer integration job identity is invalid' }
$actualSha = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualSha -cne $expectedSha) { throw 'Observer integration checkout is not SOURCE_SHA' }
$sourceTree = (git rev-parse 'HEAD^{tree}').Trim()
if ($LASTEXITCODE -ne 0 -or $sourceTree -cnotmatch '^[0-9a-f]{40}$') { throw 'Observer integration source tree is invalid' }

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$guestHarness = Join-Path $repoRoot 'scripts\windows_installed_guest_harness.ps1'
$harnessText = Get-Content -LiteralPath $guestHarness -Raw
$sourceMatch = [regex]::Match(
  $harnessText,
  "(?s)# STOCK_DESK_BROWSER_OBSERVER_CSHARP_BEGIN\r?\n" +
    "Add-Type -TypeDefinition @'\r?\n(?<source>.*?)\r?\n'@\r?\n" +
    "# STOCK_DESK_BROWSER_OBSERVER_CSHARP_END"
)
if (-not $sourceMatch.Success) { throw 'Could not extract production inline observer C# from guest harness' }
$observerTypeDefinition = $sourceMatch.Groups['source'].Value
$observerTypeDefinitionSha256 = Get-StringSha256 -Value $observerTypeDefinition
Add-Type -TypeDefinition $observerTypeDefinition

$fixtureSource = @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

internal static class BrowserFixture {
  private const uint WS_OVERLAPPEDWINDOW = 0x00CF0000;
  private const int SW_HIDE = 0;
  private const int SW_SHOW = 5;
  private const uint PM_REMOVE = 0x0001;

  private delegate IntPtr WindowProc(IntPtr window, uint message, UIntPtr wParam, IntPtr lParam);

  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  private struct WNDCLASS {
    public uint style;
    public IntPtr windowProc;
    public int classExtra;
    public int windowExtra;
    public IntPtr instance;
    public IntPtr icon;
    public IntPtr cursor;
    public IntPtr background;
    public string menuName;
    public string className;
  }

  [StructLayout(LayoutKind.Sequential)]
  private struct POINT { public int x; public int y; }

  [StructLayout(LayoutKind.Sequential)]
  private struct MSG {
    public IntPtr window;
    public uint message;
    public UIntPtr wParam;
    public IntPtr lParam;
    public uint time;
    public POINT point;
  }

  [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
  private static extern IntPtr GetModuleHandle(string moduleName);
  [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  private static extern ushort RegisterClass(ref WNDCLASS windowClass);
  [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  private static extern IntPtr CreateWindowEx(
    uint extendedStyle, string className, string title, uint style,
    int x, int y, int width, int height, IntPtr parent, IntPtr menu,
    IntPtr instance, IntPtr parameter
  );
  [DllImport("user32.dll")]
  private static extern bool ShowWindow(IntPtr window, int command);
  [DllImport("user32.dll")]
  private static extern bool UpdateWindow(IntPtr window);
  [DllImport("user32.dll")]
  private static extern bool DestroyWindow(IntPtr window);
  [DllImport("user32.dll")]
  private static extern IntPtr DefWindowProc(IntPtr window, uint message, UIntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")]
  private static extern bool PeekMessage(out MSG message, IntPtr window, uint minimum, uint maximum, uint remove);
  [DllImport("user32.dll")]
  private static extern bool TranslateMessage(ref MSG message);
  [DllImport("user32.dll")]
  private static extern IntPtr DispatchMessage(ref MSG message);

  private static readonly WindowProc Callback = HandleWindowMessage;
  private static string ClassName;
  private static IntPtr Instance;

  private static IntPtr HandleWindowMessage(IntPtr window, uint message, UIntPtr wParam, IntPtr lParam) {
    return DefWindowProc(window, message, wParam, lParam);
  }

  private static void Pump() {
    MSG message;
    while (PeekMessage(out message, IntPtr.Zero, 0, 0, PM_REMOVE)) {
      TranslateMessage(ref message);
      DispatchMessage(ref message);
    }
  }

  private static IntPtr Create(string title, int x) {
    IntPtr window = CreateWindowEx(
      0, ClassName, title, WS_OVERLAPPEDWINDOW, x, 100, 360, 240,
      IntPtr.Zero, IntPtr.Zero, Instance, IntPtr.Zero
    );
    if (window == IntPtr.Zero) { throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()); }
    ShowWindow(window, SW_SHOW);
    UpdateWindow(window);
    Pump();
    return window;
  }

  public static int Main(string[] arguments) {
    if (arguments.Length != 6) { return 64; }
    string readyPath = arguments[0];
    string triggerPath = arguments[1];
    string transientReadyPath = arguments[2];
    string hidePath = arguments[3];
    string donePath = arguments[4];
    string stopPath = arguments[5];
    Instance = GetModuleHandle(null);
    ClassName = "StockDeskBrowserFixture_" + System.Diagnostics.Process.GetCurrentProcess().Id;
    WNDCLASS windowClass = new WNDCLASS {
      windowProc = Marshal.GetFunctionPointerForDelegate(Callback),
      instance = Instance,
      className = ClassName
    };
    if (RegisterClass(ref windowClass) == 0) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    IntPtr first = Create("Stock Desk browser observer baseline A", 100);
    IntPtr second = Create("Stock Desk browser observer baseline B", 500);
    File.WriteAllText(readyPath, first.ToInt64() + "|" + second.ToInt64());
    while (!File.Exists(triggerPath) && !File.Exists(stopPath)) { Pump(); Thread.Sleep(10); }
    if (File.Exists(stopPath)) {
      DestroyWindow(second);
      DestroyWindow(first);
      Pump();
      return 0;
    }
    IntPtr transient = Create("Stock Desk browser observer transient", 900);
    File.WriteAllText(transientReadyPath, transient.ToInt64().ToString());
    while (!File.Exists(hidePath) && !File.Exists(stopPath)) { Pump(); Thread.Sleep(2); }
    if (File.Exists(stopPath)) {
      DestroyWindow(transient);
      DestroyWindow(second);
      DestroyWindow(first);
      Pump();
      return 0;
    }
    ShowWindow(transient, SW_HIDE);
    Pump();
    DateTime hiddenDeadline = DateTime.UtcNow.AddMilliseconds(40);
    while (DateTime.UtcNow < hiddenDeadline) { Pump(); Thread.Sleep(2); }
    DestroyWindow(transient);
    Pump();
    File.WriteAllText(donePath, transient.ToInt64().ToString());
    while (!File.Exists(stopPath)) { Pump(); Thread.Sleep(10); }
    DestroyWindow(second);
    DestroyWindow(first);
    Pump();
    return 0;
  }
}
'@

$temporaryRoot = Join-Path $env:RUNNER_TEMP "stock-desk-browser-observer-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force $temporaryRoot | Out-Null
$fixturePath = Join-Path $temporaryRoot 'chrome.exe'
$fixtureSourcePath = Join-Path $temporaryRoot 'browser-fixture.cs'
$readyPath = Join-Path $temporaryRoot 'ready.txt'
$triggerPath = Join-Path $temporaryRoot 'trigger.txt'
$transientReadyPath = Join-Path $temporaryRoot 'transient-ready.txt'
$hidePath = Join-Path $temporaryRoot 'hide.txt'
$donePath = Join-Path $temporaryRoot 'done.txt'
$stopPath = Join-Path $temporaryRoot 'stop.txt'
$fixtureProcess = $null

try {
  $compilerPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
  if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) {
    throw 'The pinned .NET Framework C# compiler is unavailable'
  }
  [IO.File]::WriteAllText($fixtureSourcePath, $fixtureSource, [Text.UTF8Encoding]::new($false))
  & $compilerPath @('/nologo', '/target:winexe', "/out:$fixturePath", $fixtureSourcePath)
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $fixturePath -PathType Leaf)) {
    throw 'Failed to compile the Windows browser observer fixture'
  }
  $quotedArguments = @(
    $readyPath, $triggerPath, $transientReadyPath, $hidePath, $donePath, $stopPath
  ) | ForEach-Object { '"' + $_ + '"' }
  $fixtureProcess = Start-Process -FilePath $fixturePath -ArgumentList $quotedArguments -PassThru
  Wait-Path -Path $readyPath

  [StockDeskBrowserWindowObserver]::Start()
  $baseline = @([StockDeskBrowserWindowObserver]::CaptureBaseline())
  $fixtureBaseline = @($baseline | Where-Object { $_.process_id -eq $fixtureProcess.Id })
  if ($fixtureBaseline.Count -ne 2) { throw 'EnumWindows did not return both visible baseline HWNDs for one PID' }
  $baselineHandles = @($fixtureBaseline | ForEach-Object { [long]$_.window_handle } | Sort-Object)
  if (@($baselineHandles | Select-Object -Unique).Count -ne 2) { throw 'Baseline HWNDs are not distinct' }

  [IO.File]::WriteAllText($triggerPath, 'go')
  Wait-Path -Path $transientReadyPath
  $transientHandle = [long](Get-Content -LiteralPath $transientReadyPath -Raw)
  # Do not call EnumWindows here: the helper keeps the transient HWND visible
  # until the production hook has proved that it observed the SHOW callback.
  # This removes runner-speed flakiness without turning the transient into a
  # polling sample; baseline and final remain the only inventory boundaries.
  $eventDeadline = [DateTime]::UtcNow.AddSeconds(5)
  do {
    $hookEvents = @([StockDeskBrowserWindowObserver]::GetEvents() | Where-Object {
        $_.process_id -eq $fixtureProcess.Id -and $_.window_handle -eq $transientHandle
      })
    $hookEventNames = @($hookEvents | ForEach-Object { [string]$_.event_name })
    if ($hookEventNames -contains 'show') { break }
    Start-Sleep -Milliseconds 25
  } while ([DateTime]::UtcNow -lt $eventDeadline)
  if ($hookEventNames -notcontains 'show') { throw 'Timed out waiting for transient WinEvent SHOW' }
  [IO.File]::WriteAllText($hidePath, 'hide-and-destroy')
  Wait-Path -Path $donePath
  $completedTransientHandle = [long](Get-Content -LiteralPath $donePath -Raw)
  if ($completedTransientHandle -ne $transientHandle) { throw 'Fixture transient HWND identity changed' }
  $eventDeadline = [DateTime]::UtcNow.AddSeconds(5)
  do {
    $hookEvents = @([StockDeskBrowserWindowObserver]::GetEvents() | Where-Object {
        $_.process_id -eq $fixtureProcess.Id -and $_.window_handle -eq $transientHandle
      })
    $hookEventNames = @($hookEvents | ForEach-Object { [string]$_.event_name })
    $hookComplete = @('show', 'hide', 'destroy') | Where-Object { $hookEventNames -notcontains $_ }
    if (@($hookComplete).Count -eq 0) { break }
    Start-Sleep -Milliseconds 25
  } while ([DateTime]::UtcNow -lt $eventDeadline)
  if (@($hookComplete).Count -ne 0) { throw 'Timed out waiting for transient WinEvent lifecycle' }
  $final = @([StockDeskBrowserWindowObserver]::CaptureFinal())
  $fixtureFinalHandles = @(
    $final | Where-Object { $_.process_id -eq $fixtureProcess.Id } |
      ForEach-Object { [long]$_.window_handle } | Sort-Object
  )
  if (($fixtureFinalHandles | ConvertTo-Json -Compress) -cne ($baselineHandles | ConvertTo-Json -Compress)) {
    throw 'Final EnumWindows inventory differs after the between-polls transient HWND'
  }
  [StockDeskBrowserWindowObserver]::Stop()
  $events = @([StockDeskBrowserWindowObserver]::GetEvents())
  $transientEvents = @($events | Where-Object {
      $_.process_id -eq $fixtureProcess.Id -and $_.window_handle -eq $transientHandle
    })
  $transientNames = @($transientEvents | ForEach-Object { [string]$_.event_name })
  foreach ($requiredEvent in @('show', 'hide', 'destroy')) {
    if ($transientNames -notcontains $requiredEvent) { throw "SetWinEventHook missed transient $requiredEvent" }
  }
  if ($baselineHandles -contains $transientHandle) { throw 'Transient HWND was incorrectly part of baseline' }
  $eventDigestLines = @($events | ForEach-Object {
      "$($_.sequence)|$($_.captured_at_utc)|$($_.event_name)|$($_.process_name)|$($_.process_id)|$($_.window_handle)"
    })
  $eventStreamSha256 = Get-StringSha256 -Value ($eventDigestLines -join "`n")

  [StockDeskBrowserWindowObserver]::Start()
  [void][StockDeskBrowserWindowObserver]::CaptureBaseline()
  [void][StockDeskBrowserWindowObserver]::CaptureFinal()
  [StockDeskBrowserWindowObserver]::ForceUnhookFailureForTest = $true
  $unhookFailedClosed = $false
  try {
    [StockDeskBrowserWindowObserver]::Stop()
  } catch {
    if ($_.Exception.Message -notmatch 'failed to unhook') { throw }
    $unhookFailedClosed = $true
  }
  if (-not $unhookFailedClosed) { throw 'Injected UnhookWinEvent failure did not fail closed' }
  if ($null -ne [StockDeskBrowserWindowObserver]::HookStoppedAtUtc) {
    throw 'UnhookWinEvent failure incorrectly emitted a stopped timestamp'
  }

  $outputDirectory = Split-Path -Parent $OutputPath
  New-Item -ItemType Directory -Force $outputDirectory | Out-Null
  $evidence = [ordered]@{
    schema = 'stock-desk-windows-browser-observer-integration-v1'
    source_sha = $actualSha
    source_tree = $sourceTree
    workflow_ref = $workflowRef
    workflow_sha = $workflowSha
    event_name = [string]$env:GITHUB_EVENT_NAME
    run_id = [long]$env:GITHUB_RUN_ID
    run_attempt = [int]$env:GITHUB_RUN_ATTEMPT
    job_id = [string]$env:GITHUB_JOB
    guest_harness_sha256 = Get-FileSha256 -Path $guestHarness
    observer_type_definition_sha256 = $observerTypeDefinitionSha256
    integration_driver_sha256 = Get-FileSha256 -Path $PSCommandPath
    windows_version = [Environment]::OSVersion.VersionString
    powershell_version = [string]$PSVersionTable.PSVersion
    fixture_process_name = 'chrome'
    fixture_process_id = $fixtureProcess.Id
    baseline_window_handles = $baselineHandles
    final_window_handles = $fixtureFinalHandles
    transient_window_handle = $transientHandle
    transient_events = $transientNames
    lifecycle_event_stream_sha256 = $eventStreamSha256
    enum_windows_same_pid_window_count = $fixtureBaseline.Count
    between_poll_transient_observed = $true
    unhook_failure_failed_closed = $unhookFailedClosed
    unhook_failure_stopped_timestamp = $null
  }
  [IO.File]::WriteAllText(
    $OutputPath,
    (($evidence | ConvertTo-Json -Depth 8) + "`n"),
    [Text.UTF8Encoding]::new($false)
  )
} finally {
  [IO.File]::WriteAllText($stopPath, 'stop')
  if ($fixtureProcess -and -not $fixtureProcess.HasExited) {
    if (-not $fixtureProcess.WaitForExit(5000)) { Stop-Process -Id $fixtureProcess.Id -Force }
  }
  Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
