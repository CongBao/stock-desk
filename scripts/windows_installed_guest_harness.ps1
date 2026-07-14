# Reviewed in-guest observer for Stock Desk installed-Windows evidence.
#
# A protected VM adapter copies this file and the verified controller bundle to
# a clean guest, then invokes this script as the guest's ordinary interactive
# user.  The script writes raw observations only.  It has no `passed` field and
# does not decide whether release acceptance succeeded.

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('win10-22h2', 'win11')]
  [string]$GuestProfile,

  [Parameter(Mandatory = $true)]
  [ValidateSet('installed-first-use', 'webview-install-failure')]
  [string]$Scenario,

  [Parameter(Mandatory = $true)]
  [string]$CaseId,

  [Parameter(Mandatory = $true)]
  [ValidateSet(100, 125, 150, 175, 200)]
  [int]$DpiPercent,

  [Parameter(Mandatory = $true)]
  [ValidateSet('present', 'absent')]
  [string]$WebViewInitialState,

  [Parameter(Mandatory = $true)]
  [ValidateSet('primary', 'primary-blocked-fallback')]
  [string]$DataPath,

  [Parameter(Mandatory = $true)]
  [string]$UiaDriverPath,

  [Parameter(Mandatory = $true)]
  [string]$UiaDriverSha256,

  [Parameter(Mandatory = $true)]
  [string]$NetworkObservationPath,

  [Parameter(Mandatory = $true)]
  [string]$HardwareObservationPath,

  [Parameter(Mandatory = $true)]
  [string]$FilesystemObservationPath,

  [ValidateRange(1, 1)]
  [int]$ScenarioAttempt = 1,

  [Parameter(Mandatory = $true)]
  [string]$ActionsWorkflow,

  [Parameter(Mandatory = $true)]
  [string]$ActionsRepository,

  [Parameter(Mandatory = $true)]
  [string]$ActionsWorkflowRef,

  [Parameter(Mandatory = $true)]
  [string]$ActionsWorkflowSha,

  [Parameter(Mandatory = $true)]
  [string]$ActionsWorkflowPath,

  [Parameter(Mandatory = $true)]
  [string]$ActionsWorkflowSha256,

  [Parameter(Mandatory = $true)]
  [long]$ActionsRunId,

  [ValidateRange(1, 1)]
  [int]$ActionsRunAttempt = 1,

  [Parameter(Mandatory = $true)]
  [string]$ActionsJobId,

  [Parameter(Mandatory = $true)]
  [string]$ActionsJobName,

  [Parameter(Mandatory = $true)]
  [string]$ControllerRequestPath,

  [Parameter(Mandatory = $true)]
  [string]$ControllerRequestSha256,

  [Parameter(Mandatory = $true)]
  [string]$GuestHarnessSha256,

  [Parameter(Mandatory = $true)]
  [string]$CleanSnapshotSha256,

  [Parameter(Mandatory = $true)]
  [string]$SnapshotPolicySha256,

  [Parameter(Mandatory = $true)]
  [string]$ImageSha256,

  [Parameter(Mandatory = $true)]
  [string]$ControllerLabel,

  [string]$FailureInjectionIdentity,

  [string]$FailureInjectionSha256,

  [Parameter(Mandatory = $true)]
  [string]$EvidenceRoot
)

$ErrorActionPreference = 'Stop'
$WebView2ProductionGuid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$MinimumWebView2Version = [version]'120.0.2210.91'
Set-StrictMode -Version Latest

# STOCK_DESK_BROWSER_OBSERVER_CSHARP_BEGIN
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class StockDeskTokenObservation {
  [StructLayout(LayoutKind.Sequential)]
  private struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [StructLayout(LayoutKind.Sequential)]
  private struct SID_AND_ATTRIBUTES { public IntPtr Sid; public uint Attributes; }
  [StructLayout(LayoutKind.Sequential)]
  private struct TOKEN_MANDATORY_LABEL { public SID_AND_ATTRIBUTES Label; }
  [DllImport("advapi32.dll", SetLastError = true)]
  private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
  [DllImport("advapi32.dll", SetLastError = true)]
  private static extern bool GetTokenInformation(IntPtr token, int tokenClass, out int value, int length, out int returned);
  [DllImport("advapi32.dll", SetLastError = true)]
  private static extern bool GetTokenInformation(IntPtr token, int tokenClass, IntPtr value, int length, out int returned);
  [DllImport("advapi32.dll")]
  private static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
  [DllImport("advapi32.dll")]
  private static extern IntPtr GetSidSubAuthority(IntPtr sid, uint subAuthority);
  [DllImport("kernel32.dll")]
  private static extern bool CloseHandle(IntPtr handle);
  [DllImport("user32.dll", SetLastError = true)]
  private static extern bool GetWindowRect(IntPtr window, out RECT bounds);
  [DllImport("user32.dll", EntryPoint = "PrintWindow", SetLastError = true)]
  private static extern bool NativePrintWindow(IntPtr window, IntPtr targetDc, uint flags);

  public static bool IsElevated(IntPtr processHandle) {
    IntPtr token;
    if (!OpenProcessToken(processHandle, 0x0008, out token)) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    try {
      int elevated;
      int returned;
      if (!GetTokenInformation(token, 20, out elevated, sizeof(int), out returned)) {
        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      }
      return elevated != 0;
    } finally {
      CloseHandle(token);
    }
  }

  public static int ElevationType(IntPtr processHandle) {
    IntPtr token;
    if (!OpenProcessToken(processHandle, 0x0008, out token)) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    try {
      int value;
      int returned;
      if (!GetTokenInformation(token, 18, out value, sizeof(int), out returned)) {
        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      }
      return value;
    } finally { CloseHandle(token); }
  }

  public static int IntegrityRid(IntPtr processHandle) {
    IntPtr token;
    if (!OpenProcessToken(processHandle, 0x0008, out token)) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    try {
      int required;
      GetTokenInformation(token, 25, IntPtr.Zero, 0, out required);
      IntPtr buffer = Marshal.AllocHGlobal(required);
      try {
        if (!GetTokenInformation(token, 25, buffer, required, out required)) {
          throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        TOKEN_MANDATORY_LABEL label = Marshal.PtrToStructure<TOKEN_MANDATORY_LABEL>(buffer);
        int count = Marshal.ReadByte(GetSidSubAuthorityCount(label.Label.Sid));
        return Marshal.ReadInt32(GetSidSubAuthority(label.Label.Sid, (uint)(count - 1)));
      } finally { Marshal.FreeHGlobal(buffer); }
    } finally { CloseHandle(token); }
  }

  public static int[] WindowBounds(IntPtr windowHandle) {
    RECT bounds;
    if (!GetWindowRect(windowHandle, out bounds)) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    return new int[] { bounds.Left, bounds.Top, bounds.Right - bounds.Left, bounds.Bottom - bounds.Top };
  }

  public static void PrintWindowContent(IntPtr windowHandle, IntPtr targetDc) {
    const uint PW_RENDERFULLCONTENT = 0x00000002;
    if (!NativePrintWindow(windowHandle, targetDc, PW_RENDERFULLCONTENT)) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
  }
}

public sealed class StockDeskBrowserWindowIdentity {
  public string process_name { get; set; }
  public int process_id { get; set; }
  public long window_handle { get; set; }
}

public sealed class StockDeskBrowserWindowEvent {
  public int sequence { get; set; }
  public string captured_at_utc { get; set; }
  public string event_name { get; set; }
  public string process_name { get; set; }
  public int process_id { get; set; }
  public long window_handle { get; set; }
}

public static class StockDeskBrowserWindowObserver {
  private const uint EVENT_OBJECT_CREATE = 0x8000;
  private const uint EVENT_OBJECT_DESTROY = 0x8001;
  private const uint EVENT_OBJECT_SHOW = 0x8002;
  private const uint EVENT_OBJECT_HIDE = 0x8003;
  private const int OBJID_WINDOW = 0;
  private const int CHILDID_SELF = 0;
  private const uint WINEVENT_OUTOFCONTEXT = 0x0000;
  private const uint WM_QUIT = 0x0012;
  private const uint PM_NOREMOVE = 0x0000;

  private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);
  private delegate void WinEventProc(
    IntPtr hook,
    uint eventType,
    IntPtr window,
    int objectId,
    int childId,
    uint eventThread,
    uint eventTime
  );

  [StructLayout(LayoutKind.Sequential)]
  private struct POINT { public int X; public int Y; }

  [StructLayout(LayoutKind.Sequential)]
  private struct MSG {
    public IntPtr hwnd;
    public uint message;
    public UIntPtr wParam;
    public IntPtr lParam;
    public uint time;
    public POINT point;
  }

  [DllImport("user32.dll", SetLastError = true)]
  private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
  [DllImport("user32.dll")]
  private static extern bool IsWindowVisible(IntPtr window);
  [DllImport("user32.dll", SetLastError = true)]
  private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
  [DllImport("user32.dll", SetLastError = true)]
  private static extern IntPtr SetWinEventHook(
    uint eventMin,
    uint eventMax,
    IntPtr eventHookModule,
    WinEventProc eventProc,
    uint processId,
    uint threadId,
    uint flags
  );
  [DllImport("user32.dll", SetLastError = true)]
  private static extern bool UnhookWinEvent(IntPtr hook);
  [DllImport("user32.dll", SetLastError = true)]
  private static extern bool PostThreadMessage(uint threadId, uint message, UIntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")]
  private static extern int GetMessage(out MSG message, IntPtr window, uint minimum, uint maximum);
  [DllImport("user32.dll")]
  private static extern bool PeekMessage(out MSG message, IntPtr window, uint minimum, uint maximum, uint remove);
  [DllImport("kernel32.dll")]
  private static extern uint GetCurrentThreadId();

  private static readonly object Gate = new object();
  private static readonly System.Collections.Generic.List<StockDeskBrowserWindowEvent> Events =
    new System.Collections.Generic.List<StockDeskBrowserWindowEvent>();
  private static readonly System.Collections.Generic.Dictionary<long, StockDeskBrowserWindowIdentity> KnownVisibleBrowsers =
    new System.Collections.Generic.Dictionary<long, StockDeskBrowserWindowIdentity>();
  private static readonly System.Collections.Generic.HashSet<string> BrowserNames =
    new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase) {
      "chrome", "msedge", "firefox", "brave"
    };
  private static readonly System.Threading.ManualResetEvent Started =
    new System.Threading.ManualResetEvent(false);
  private static System.Threading.Thread observerThread;
  private static WinEventProc callback;
  private static IntPtr hookHandle = IntPtr.Zero;
  private static uint observerThreadId;
  private static string startupError;
  private static string shutdownError;
  private static int nextSequence;

  public static string HookStartedAtUtc { get; private set; }
  public static string BaselineCapturedAtUtc { get; private set; }
  public static string FinalCapturedAtUtc { get; private set; }
  public static string HookStoppedAtUtc { get; private set; }
  public static int BaselineEventSequence { get; private set; }
  public static int FinalEventSequence { get; private set; }
  public static bool ForceUnhookFailureForTest { get; set; }

  private static string UtcNow() {
    return DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");
  }

  private static StockDeskBrowserWindowIdentity ResolveBrowser(IntPtr window) {
    uint processId;
    if (window == IntPtr.Zero || GetWindowThreadProcessId(window, out processId) == 0 || processId == 0) {
      return null;
    }
    try {
      using (System.Diagnostics.Process process = System.Diagnostics.Process.GetProcessById((int)processId)) {
        string name = process.ProcessName.ToLowerInvariant();
        if (!BrowserNames.Contains(name)) { return null; }
        return new StockDeskBrowserWindowIdentity {
          process_name = name,
          process_id = (int)processId,
          window_handle = window.ToInt64()
        };
      }
    } catch {
      return null;
    }
  }

  public static StockDeskBrowserWindowIdentity[] EnumerateVisibleBrowsers() {
    System.Collections.Generic.List<StockDeskBrowserWindowIdentity> windows =
      new System.Collections.Generic.List<StockDeskBrowserWindowIdentity>();
    EnumWindowsProc enumerator = delegate(IntPtr window, IntPtr parameter) {
      if (!IsWindowVisible(window)) { return true; }
      StockDeskBrowserWindowIdentity identity = ResolveBrowser(window);
      if (identity != null) { windows.Add(identity); }
      return true;
    };
    if (!EnumWindows(enumerator, IntPtr.Zero)) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    windows.Sort(delegate(StockDeskBrowserWindowIdentity left, StockDeskBrowserWindowIdentity right) {
      int byName = StringComparer.Ordinal.Compare(left.process_name, right.process_name);
      if (byName != 0) { return byName; }
      int byProcess = left.process_id.CompareTo(right.process_id);
      return byProcess != 0 ? byProcess : left.window_handle.CompareTo(right.window_handle);
    });
    return windows.ToArray();
  }

  private static void ObserveWindowEvent(
    IntPtr ignoredHook,
    uint eventType,
    IntPtr window,
    int objectId,
    int childId,
    uint ignoredEventThread,
    uint ignoredEventTime
  ) {
    if (objectId != OBJID_WINDOW || childId != CHILDID_SELF || window == IntPtr.Zero) { return; }
    string eventName;
    if (eventType == EVENT_OBJECT_CREATE) { eventName = "create"; }
    else if (eventType == EVENT_OBJECT_DESTROY) { eventName = "destroy"; }
    else if (eventType == EVENT_OBJECT_SHOW) { eventName = "show"; }
    else if (eventType == EVENT_OBJECT_HIDE) { eventName = "hide"; }
    else { return; }

    StockDeskBrowserWindowIdentity identity = ResolveBrowser(window);
    lock (Gate) {
      StockDeskBrowserWindowIdentity known;
      bool wasKnown = KnownVisibleBrowsers.TryGetValue(window.ToInt64(), out known);
      bool isVisibleLifecycle = eventType == EVENT_OBJECT_SHOW || IsWindowVisible(window);
      if (identity == null && wasKnown) { identity = known; }
      if (identity == null || (!wasKnown && !isVisibleLifecycle)) { return; }
      if (isVisibleLifecycle) { KnownVisibleBrowsers[identity.window_handle] = identity; }
      Events.Add(new StockDeskBrowserWindowEvent {
        sequence = ++nextSequence,
        captured_at_utc = UtcNow(),
        event_name = eventName,
        process_name = identity.process_name,
        process_id = identity.process_id,
        window_handle = identity.window_handle
      });
    }
  }

  private static void RunObserver() {
    try {
      observerThreadId = GetCurrentThreadId();
      MSG ignoredMessage;
      PeekMessage(out ignoredMessage, IntPtr.Zero, 0, 0, PM_NOREMOVE);
      callback = new WinEventProc(ObserveWindowEvent);
      hookHandle = SetWinEventHook(
        EVENT_OBJECT_CREATE,
        EVENT_OBJECT_HIDE,
        IntPtr.Zero,
        callback,
        0,
        0,
        WINEVENT_OUTOFCONTEXT
      );
      if (hookHandle == IntPtr.Zero) {
        startupError = new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()).Message;
        Started.Set();
        return;
      }
      HookStartedAtUtc = UtcNow();
      Started.Set();
      MSG message;
      int messageResult;
      while ((messageResult = GetMessage(out message, IntPtr.Zero, 0, 0)) > 0) { }
      if (messageResult < 0) {
        shutdownError = new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()).Message;
      }
    } catch (Exception error) {
      if (HookStartedAtUtc == null) { startupError = error.Message; }
      else { shutdownError = error.Message; }
      Started.Set();
    } finally {
      if (hookHandle != IntPtr.Zero) {
        bool unhooked = UnhookWinEvent(hookHandle);
        int unhookError = unhooked ? 0 : Marshal.GetLastWin32Error();
        hookHandle = IntPtr.Zero;
        if (!unhooked) {
          shutdownError = new System.ComponentModel.Win32Exception(unhookError).Message;
        } else if (ForceUnhookFailureForTest) {
          shutdownError = "injected UnhookWinEvent failure";
        }
      }
    }
  }

  public static void Start() {
    lock (Gate) {
      if (observerThread != null) { throw new InvalidOperationException("Browser window observer is already running"); }
      Events.Clear();
      KnownVisibleBrowsers.Clear();
      nextSequence = 0;
      startupError = null;
      shutdownError = null;
      ForceUnhookFailureForTest = false;
      HookStartedAtUtc = null;
      BaselineCapturedAtUtc = null;
      FinalCapturedAtUtc = null;
      HookStoppedAtUtc = null;
      BaselineEventSequence = 0;
      FinalEventSequence = 0;
      Started.Reset();
      observerThread = new System.Threading.Thread(RunObserver);
      observerThread.IsBackground = true;
      observerThread.Name = "StockDeskBrowserWindowObserver";
      observerThread.Start();
    }
    if (!Started.WaitOne(10000)) { throw new TimeoutException("Browser window event hook did not start"); }
    if (startupError != null || hookHandle == IntPtr.Zero) {
      throw new InvalidOperationException("Browser window event hook failed: " + startupError);
    }
  }

  public static StockDeskBrowserWindowIdentity[] CaptureBaseline() {
    StockDeskBrowserWindowIdentity[] windows = EnumerateVisibleBrowsers();
    lock (Gate) {
      foreach (StockDeskBrowserWindowIdentity identity in windows) {
        KnownVisibleBrowsers[identity.window_handle] = identity;
      }
      BaselineCapturedAtUtc = UtcNow();
      BaselineEventSequence = nextSequence;
    }
    return windows;
  }

  public static StockDeskBrowserWindowIdentity[] CaptureFinal() {
    StockDeskBrowserWindowIdentity[] windows = EnumerateVisibleBrowsers();
    lock (Gate) {
      FinalCapturedAtUtc = UtcNow();
      FinalEventSequence = nextSequence;
    }
    return windows;
  }

  public static StockDeskBrowserWindowEvent[] GetEvents() {
    lock (Gate) { return Events.ToArray(); }
  }

  public static void Stop() {
    System.Threading.Thread thread;
    lock (Gate) { thread = observerThread; }
    if (thread == null) { throw new InvalidOperationException("Browser window observer is not running"); }
    if (!PostThreadMessage(observerThreadId, WM_QUIT, UIntPtr.Zero, IntPtr.Zero)) {
      throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    if (!thread.Join(10000)) { throw new TimeoutException("Browser window event hook did not stop"); }
    lock (Gate) { observerThread = null; }
    if (shutdownError != null) {
      throw new InvalidOperationException("Browser window event hook failed to unhook: " + shutdownError);
    }
    HookStoppedAtUtc = UtcNow();
  }
}
'@
# STOCK_DESK_BROWSER_OBSERVER_CSHARP_END

function Assert-RegularFile {
  param([Parameter(Mandatory = $true)][string]$Path, [string]$Label)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label is missing" }
  if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "$Label cannot be a reparse point"
  }
}

function Assert-HexDigest {
  param([object]$Value, [string]$Label)
  if ($Value -isnot [string] -or $Value -cnotmatch '^[0-9a-f]{64}$') {
    throw "$Label is invalid"
  }
}

function Get-FileDigest {
  param([Parameter(Mandatory = $true)][string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-StringDigest {
  param([Parameter(Mandatory = $true)][string]$Value)
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))).Replace('-', '').ToLowerInvariant()
  } finally { $sha.Dispose() }
}

function Write-Utf8NoBom {
  param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Text)
  [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-WebViewRuntime {
  $candidates = @()
  foreach ($registryCandidate in @(
      [pscustomobject]@{ Path = "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$WebView2ProductionGuid"; Scope = 'machine' },
      [pscustomobject]@{ Path = "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$WebView2ProductionGuid"; Scope = 'current-user' }
    )) {
    if (Test-Path -LiteralPath $registryCandidate.Path) {
      $item = Get-ItemProperty -LiteralPath $registryCandidate.Path -ErrorAction Stop
      $versionText = [string]$item.pv
      if ($versionText -notmatch '^\d+(\.\d+){3}$' -or $versionText -eq '0.0.0.0') {
        throw 'Production WebView2 registry version is malformed'
      }
      $candidates += [pscustomobject]@{ Version = $versionText; Scope = $registryCandidate.Scope }
    }
  }
  if ($candidates.Count -eq 0) {
    return [ordered]@{ state = 'absent'; product_guid = $null; version = $null; channel = $null; signer = $null; scope = $null }
  }
  # Mirror the x64 NSIS lookup precedence exactly; do not let a later user
  # registration hide an unsupported machine registration.
  $runtime = $candidates | Select-Object -First 1
  if ([version]$runtime.Version -lt $MinimumWebView2Version) {
    throw 'Production WebView2 runtime is below the locked minimum version'
  }
  $binaryCandidates = @(
    [pscustomobject]@{ Path = "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application\$($runtime.Version)\msedgewebview2.exe"; Scope = 'machine' },
    [pscustomobject]@{ Path = "$env:ProgramFiles\Microsoft\EdgeWebView\Application\$($runtime.Version)\msedgewebview2.exe"; Scope = 'machine' },
    [pscustomobject]@{ Path = "$env:LOCALAPPDATA\Microsoft\EdgeWebView\Application\$($runtime.Version)\msedgewebview2.exe"; Scope = 'current-user' }
  )
  $binary = $binaryCandidates | Where-Object {
    $_.Scope -eq $runtime.Scope -and (Test-Path -LiteralPath $_.Path -PathType Leaf)
  } | Select-Object -First 1
  if (-not $binary) { throw 'WebView2 registry state has no production runtime binary' }
  $signature = Get-AuthenticodeSignature -LiteralPath $binary.Path
  if (
    $signature.Status -ne 'Valid' -or
    $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)'
  ) {
    throw 'WebView2 runtime has no valid Microsoft Authenticode signature'
  }
  $certificateDigest = [BitConverter]::ToString(
    $signature.SignerCertificate.GetCertHash([Security.Cryptography.HashAlgorithmName]::SHA256)
  ).Replace('-', '').ToLowerInvariant()
  return [ordered]@{
    state = 'present'
    product_guid = $WebView2ProductionGuid
    version = $runtime.Version
    channel = 'evergreen'
    signer = [ordered]@{
      status = 'Valid'
      subject = 'CN=Microsoft Corporation'
      certificate_sha256 = $certificateDigest
    }
    scope = $runtime.Scope
  }
}

function Get-ProcessTokenObservation {
  param([string]$Role, [Diagnostics.Process]$Process, [bool]$Started)
  $elevated = $null
  $integrityRid = $null
  if ($Started) {
    $elevated = [StockDeskTokenObservation]::IsElevated($Process.Handle)
    $integrityRid = [StockDeskTokenObservation]::IntegrityRid($Process.Handle)
  }
  return [ordered]@{
    role = $Role
    started = $Started
    elevated = $elevated
    integrity_level = if ($integrityRid -eq 8192) { 'medium' } elseif ($integrityRid -ge 12288) { 'high' } elseif ($Started) { 'unsupported' } else { $null }
    integrity_rid = $integrityRid
  }
}

function Get-CanarySnapshot {
  param([Parameter(Mandatory = $true)][string]$Root)
  $entries = @(Get-ChildItem -LiteralPath $Root -File -Recurse -Force | Sort-Object FullName)
  $stream = [IO.MemoryStream]::new()
  $writer = [IO.BinaryWriter]::new($stream, [Text.UTF8Encoding]::new($false), $true)
  try {
    foreach ($entry in $entries) {
      $relative = $entry.FullName.Substring($Root.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
      $writer.Write($relative)
      $hex = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash
      $contentDigest = [byte[]]::new(32)
      for ($index = 0; $index -lt $contentDigest.Length; $index += 1) {
        $contentDigest[$index] = [Convert]::ToByte($hex.Substring($index * 2, 2), 16)
      }
      $writer.Write([int]$contentDigest.Length)
      $writer.Write($contentDigest)
    }
    $writer.Flush()
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
      $digestBytes = $sha.ComputeHash($stream.ToArray())
      $digest = [BitConverter]::ToString($digestBytes).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
  } finally {
    $writer.Dispose()
    $stream.Dispose()
  }
  return [ordered]@{ entry_count = $entries.Count; content_sha256 = $digest }
}

function Add-Observation {
  param([string]$Kind, [string]$Producer, [object]$Value)
  $script:Sequence += 1
  $event = [ordered]@{
    sequence = $script:Sequence
    captured_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    kind = $Kind
    producer = $Producer
    value = $Value
  }
  $script:Events.Add(($event | ConvertTo-Json -Depth 12 -Compress))
}

function Test-ShortcutPresent {
  $paths = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Stock Desk.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Stock Desk.lnk')
  )
  return @($paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }).Count -gt 0
}

function Request-GracefulExit {
  param([Parameter(Mandatory = $true)][Diagnostics.Process]$Process)

  [void]$Process.CloseMainWindow()
  Start-Sleep -Milliseconds 750
  $Process.Refresh()
  if ($Process.HasExited) { return }
  Add-Type -AssemblyName UIAutomationTypes
  Add-Type -AssemblyName UIAutomationClient
  $root = [Windows.Automation.AutomationElement]::FromHandle($Process.MainWindowHandle)
  if ($null -eq $root) { throw 'Desktop exit confirmation is not exposed to UI Automation' }
  $buttons = $root.FindAll(
    [Windows.Automation.TreeScope]::Descendants,
    [Windows.Automation.Condition]::TrueCondition
  ) | Where-Object {
    $_.Current.ControlType -eq [Windows.Automation.ControlType]::Button -and
    $_.Current.Name -match '^(退出应用|保存检查点并退出|Exit|Save checkpoint and exit)$'
  }
  if (@($buttons).Count -ne 1) { throw 'Desktop exit confirmation has no unique Exit button' }
  $pattern = $buttons[0].GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
  ([Windows.Automation.InvokePattern]$pattern).Invoke()
  if (-not $Process.WaitForExit(15000)) {
    throw 'Desktop host did not exit gracefully after explicit confirmation'
  }
}

function Wait-ProcessAndObserveUac {
  param(
    [Parameter(Mandatory = $true)][Diagnostics.Process]$Process,
    [Parameter(Mandatory = $true)][int]$ConsentBaseline,
    [switch]$ObserveBrowser
  )

  do {
    if ($ObserveBrowser) { Add-BrowserObservation -Phase 'installer' }
    $consentNow = @(Get-Process -Name consent -ErrorAction SilentlyContinue).Count
    if ($consentNow -gt $ConsentBaseline) { $script:UacPromptCount += 1 }
    if ($null -eq $script:WebViewChildProcess) {
      $child = Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | Where-Object {
        $_.ParentProcessId -eq $Process.Id -and $_.Name -ieq 'MicrosoftEdgeWebView2RuntimeInstaller.exe'
      } | Select-Object -First 1
      if ($child) {
        $script:WebViewChildExecutablePath = [string]$child.ExecutablePath
        Assert-RegularFile -Path $script:WebViewChildExecutablePath -Label 'Observed WebView2 child executable'
        $script:WebViewChildProcess = Get-Process -Id $child.ProcessId -ErrorAction Stop
        $script:WebViewChildExecutableSha256 = Get-FileDigest -Path $script:WebViewChildExecutablePath
        $childSignature = Get-AuthenticodeSignature -LiteralPath $script:WebViewChildExecutablePath
        if ($childSignature.Status -ne 'Valid' -or $childSignature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Microsoft Corporation(,|$)') {
          throw 'Observed WebView2 child has no valid Microsoft signature'
        }
        $script:WebViewChildCertificateSha256 = [BitConverter]::ToString(
          $childSignature.SignerCertificate.GetCertHash([Security.Cryptography.HashAlgorithmName]::SHA256)
        ).Replace('-', '').ToLowerInvariant()
        $script:WebViewChildElevated = [StockDeskTokenObservation]::IsElevated($script:WebViewChildProcess.Handle)
        $script:WebViewChildIntegrityRid = [StockDeskTokenObservation]::IntegrityRid($script:WebViewChildProcess.Handle)
      }
    }
    if (-not $Process.HasExited) { Start-Sleep -Milliseconds 100 }
    $Process.Refresh()
  } while (-not $Process.HasExited)
  $Process.WaitForExit()
}

function Get-BrowserTopLevelWindowIdentities {
  return @(
    [StockDeskBrowserWindowObserver]::EnumerateVisibleBrowsers() | ForEach-Object {
      [ordered]@{
        process_name = [string]$_.process_name
        process_id = [int]$_.process_id
        window_handle = [long]$_.window_handle
      }
    } | Sort-Object process_name, process_id, window_handle
  )
}

function Add-BrowserObservation {
  param([ValidateSet('baseline', 'installer', 'app-readiness', 'stable', 'final')][string]$Phase)
  if ($Phase -eq 'baseline') {
    $nativeWindows = @([StockDeskBrowserWindowObserver]::CaptureBaseline())
    $capturedAt = [StockDeskBrowserWindowObserver]::BaselineCapturedAtUtc
  } elseif ($Phase -eq 'final') {
    $nativeWindows = @([StockDeskBrowserWindowObserver]::CaptureFinal())
    $capturedAt = [StockDeskBrowserWindowObserver]::FinalCapturedAtUtc
  } else {
    $nativeWindows = @([StockDeskBrowserWindowObserver]::EnumerateVisibleBrowsers())
    $capturedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
  }
  $windows = @(
    $nativeWindows | ForEach-Object {
      [ordered]@{
        process_name = [string]$_.process_name
        process_id = [int]$_.process_id
        window_handle = [long]$_.window_handle
      }
    } | Sort-Object process_name, process_id, window_handle
  )
  $snapshot = [ordered]@{
    captured_at_utc = $capturedAt
    phase = $Phase
    windows = $windows
  }
  $identity = $windows | ConvertTo-Json -Depth 4 -Compress
  if ($script:BrowserTimeline.Count -eq 0 -or $identity -cne $script:LastBrowserIdentity -or $Phase -cne $script:LastBrowserPhase) {
    $script:BrowserTimeline.Add($snapshot)
    $script:LastBrowserIdentity = $identity
    $script:LastBrowserPhase = $Phase
  }
}

function Complete-BrowserWindowObservation {
  Add-BrowserObservation -Phase 'final'
  [StockDeskBrowserWindowObserver]::Stop()
  $events = @(
    [StockDeskBrowserWindowObserver]::GetEvents() | ForEach-Object {
      [ordered]@{
        sequence = [int]$_.sequence
        captured_at_utc = [string]$_.captured_at_utc
        event = [string]$_.event_name
        process_name = [string]$_.process_name
        process_id = [int]$_.process_id
        window_handle = [long]$_.window_handle
      }
    }
  )
  $digestLines = @(
    $events | ForEach-Object {
      "$($_.sequence)|$($_.captured_at_utc)|$($_.event)|$($_.process_name)|$($_.process_id)|$($_.window_handle)"
    }
  )
  $script:BrowserWindowEvents = $events
  $script:BrowserObserverSummary = [ordered]@{
    schema = 'stock-desk-browser-window-observer-v1'
    api = 'Win32 EnumWindows + SetWinEventHook'
    hook_started_at_utc = [StockDeskBrowserWindowObserver]::HookStartedAtUtc
    baseline_captured_at_utc = [StockDeskBrowserWindowObserver]::BaselineCapturedAtUtc
    baseline_event_sequence = [StockDeskBrowserWindowObserver]::BaselineEventSequence
    final_captured_at_utc = [StockDeskBrowserWindowObserver]::FinalCapturedAtUtc
    final_event_sequence = [StockDeskBrowserWindowObserver]::FinalEventSequence
    hook_stopped_at_utc = [StockDeskBrowserWindowObserver]::HookStoppedAtUtc
    subscribed_events = @('create', 'show', 'hide', 'destroy')
    lifecycle_event_count = $events.Count
    lifecycle_events_sha256 = Get-StringDigest -Value ($digestLines -join "`n")
  }
}

function Get-WindowAutomationText {
  param([Parameter(Mandatory = $true)][IntPtr]$WindowHandle)
  Add-Type -AssemblyName UIAutomationTypes
  Add-Type -AssemblyName UIAutomationClient
  $root = [Windows.Automation.AutomationElement]::FromHandle($WindowHandle)
  if ($null -eq $root) { throw 'Target window is not exposed to UI Automation' }
  $values = [Collections.Generic.List[string]]::new()
  foreach ($element in $root.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)) {
    $name = [string]$element.Current.Name
    if (-not [string]::IsNullOrWhiteSpace($name)) { $values.Add($name.Trim()) }
  }
  return @($values | Sort-Object -Unique)
}

function Save-TargetWindowCapture {
  param([Parameter(Mandatory = $true)][IntPtr]$WindowHandle, [Parameter(Mandatory = $true)][string]$Path)
  Add-Type -AssemblyName System.Drawing
  $rawBounds = [StockDeskTokenObservation]::WindowBounds($WindowHandle)
  if ($rawBounds[2] -lt 320 -or $rawBounds[3] -lt 180) { throw 'Observed target window is too small for evidence' }
  $bounds = [Drawing.Rectangle]::new($rawBounds[0], $rawBounds[1], $rawBounds[2], $rawBounds[3])
  $bitmap = [Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
  try {
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try {
      $targetDc = $graphics.GetHdc()
      try { [StockDeskTokenObservation]::PrintWindowContent($WindowHandle, $targetDc) }
      finally { $graphics.ReleaseHdc($targetDc) }
    }
    finally { $graphics.Dispose() }
    $bitmap.Save($Path, [Drawing.Imaging.ImageFormat]::Png)
  } finally { $bitmap.Dispose() }
}

Assert-RegularFile -Path $ControllerRequestPath -Label 'Controller request'
foreach ($pair in @(
    @($ControllerRequestSha256, 'controller request digest'),
    @($GuestHarnessSha256, 'guest harness digest'),
    @($CleanSnapshotSha256, 'clean snapshot digest'),
    @($SnapshotPolicySha256, 'snapshot policy digest'),
    @($ImageSha256, 'image digest'),
    @($ActionsWorkflowSha256, 'workflow digest'),
    @($UiaDriverSha256, 'UI Automation driver digest')
  )) {
  Assert-HexDigest -Value $pair[0] -Label $pair[1]
}
foreach ($requiredPath in @($UiaDriverPath, $NetworkObservationPath, $HardwareObservationPath, $FilesystemObservationPath)) {
  Assert-RegularFile -Path $requiredPath -Label 'Protected desktop evidence input'
}
if ((Get-FileDigest -Path $UiaDriverPath) -cne $UiaDriverSha256) {
  throw 'Executed UI Automation driver differs from the reviewed file'
}
if ($CaseId -cnotmatch '^(win10-22h2|win11)-dpi-(100|125|150|175|200)(-webview-offline)?$') {
  throw 'Desktop evidence case identity is invalid'
}
if ((Get-FileDigest -Path $ControllerRequestPath) -ne $ControllerRequestSha256) {
  throw 'Controller request digest changed inside the guest'
}
$guestSelfSha256 = Get-FileDigest -Path $PSCommandPath
if ($guestSelfSha256 -cne $GuestHarnessSha256) {
  throw 'Executed guest harness differs from the controller-reviewed file'
}
if ($ActionsRunId -lt 1 -or $ActionsRunAttempt -ne 1 -or $ScenarioAttempt -ne 1) {
  throw 'Only first-attempt execution identity is accepted'
}

$request = Get-Content -LiteralPath $ControllerRequestPath -Raw | ConvertFrom-Json
if (
  $request.schema -ne 'stock-desk-windows-installed-controller-request-v2' -or
  $request.evidence_kind -ne 'observed-windows-vm'
) {
  throw 'Controller request is not an authorized observed-VM request'
}
if ($request.status -ne 'authorized' -or $request.raw_only -ne $true) {
  throw 'Controller request is not an authorized raw-only VM request'
}
if (
  $request.source_sha -ne $ActionsWorkflowSha -or
  $request.case_ids -notcontains $CaseId -or
  $request.guest_harness_sha256 -ne $GuestHarnessSha256 -or
  $request.uia_driver_sha256 -ne $UiaDriverSha256
) {
  throw 'Controller request does not bind this exact source, case, guest harness, and UIA driver'
}
$controllerRoot = Split-Path -Parent $ControllerRequestPath
$installerPath = [IO.Path]::GetFullPath((Join-Path $controllerRoot $request.installer.path))
$controllerRootPrefix = [IO.Path]::GetFullPath($controllerRoot).TrimEnd('\') + '\'
if (-not $installerPath.StartsWith($controllerRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw 'Installer path escapes the controller bundle'
}
Assert-RegularFile -Path $installerPath -Label 'Verified Stock Desk installer'
if ((Get-FileDigest -Path $installerPath) -ne $request.candidate_sha256) {
  throw 'Installer digest changed inside the guest'
}

$publicRoot = Join-Path $EvidenceRoot 'public'
$rawRoot = Join-Path $publicRoot 'raw'
if (Test-Path -LiteralPath $publicRoot) { Remove-Item -LiteralPath $publicRoot -Recurse -Force }
New-Item -ItemType Directory -Path $rawRoot -Force | Out-Null
$startedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$script:Sequence = 0
$script:Events = [Collections.Generic.List[string]]::new()

$operatingSystem = Get-CimInstance -ClassName Win32_OperatingSystem
$displayVersion = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').DisplayVersion
$build = [int]$operatingSystem.BuildNumber
$family = if ($build -eq 19045) { 'windows-10' } elseif ($build -ge 22000) { 'windows-11' } else { 'unsupported' }
Add-Observation -Kind 'system' -Producer 'powershell-cim' -Value ([ordered]@{
    family = $family
    display_version = [string]$displayVersion
    build_number = $build
    update_build_revision = [int](Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').UBR
    architecture = if ([Environment]::Is64BitOperatingSystem) { 'x86_64' } else { 'unsupported' }
    image_sha256 = $ImageSha256
  })

$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$administratorSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$administratorGroupMember = @($identity.Groups | Where-Object { $_ -eq $administratorSid }).Count -gt 0
$elevationType = [StockDeskTokenObservation]::ElevationType([Diagnostics.Process]::GetCurrentProcess().Handle)
$integrityRid = [StockDeskTokenObservation]::IntegrityRid([Diagnostics.Process]::GetCurrentProcess().Handle)
$username = [Environment]::UserName
$profile = [Environment]::GetFolderPath('UserProfile')
Add-Observation -Kind 'account-token' -Producer 'windows-token' -Value ([ordered]@{
    account_type = if ($isAdmin) { 'administrator' } else { 'standard' }
    is_admin = $isAdmin
    administrator_group_member = $administratorGroupMember
    linked_token_present = $elevationType -eq 3
    token_elevation_type = if ($elevationType -eq 1) { 'default' } elseif ($elevationType -eq 2) { 'full' } else { 'limited' }
    integrity_level = if ($integrityRid -eq 8192) { 'medium' } else { 'unsupported' }
    integrity_rid = $integrityRid
    username_contains_non_ascii = $username -match '[^\u0000-\u007f]'
    profile_path_contains_space = $profile.Contains(' ')
  })

$v1Root = Join-Path $env:LOCALAPPDATA 'stock-desk'
$canaryPath = Join-Path $v1Root 'v1.1-installed-acceptance-canary.txt'
if (-not (Test-Path -LiteralPath $canaryPath -PathType Leaf)) {
  throw 'Protected clean snapshot lacks the pre-seeded legacy v1 canary'
}
$canaryBefore = Get-CanarySnapshot -Root $v1Root
$webviewBefore = Get-WebViewRuntime
Add-Observation -Kind 'webview-before' -Producer 'windows-registry-authenticode' -Value $webviewBefore

$consentBefore = @(Get-Process -Name consent -ErrorAction SilentlyContinue).Count
$script:UacPromptCount = 0
$script:WebViewChildProcess = $null
$script:WebViewChildExecutablePath = $null
$script:WebViewChildExecutableSha256 = $null
$script:WebViewChildCertificateSha256 = $null
$script:WebViewChildElevated = $null
$script:WebViewChildIntegrityRid = $null
$script:WebViewCaptureTextPath = Join-Path $rawRoot 'capture.txt'
$script:BrowserTimeline = [Collections.Generic.List[object]]::new()
$script:LastBrowserIdentity = $null
$script:LastBrowserPhase = $null
$script:BrowserWindowEvents = $null
$script:BrowserObserverSummary = $null
[StockDeskBrowserWindowObserver]::Start()
Add-BrowserObservation -Phase 'baseline'
if (@($script:BrowserTimeline[0].windows).Count -ne 0) {
  throw 'Protected clean snapshot must have an empty external-browser baseline'
}
$installLogPath = Join-Path $rawRoot 'install.log'
$installerProcess = Start-Process -FilePath $installerPath -ArgumentList '/S' -PassThru
$installerToken = Get-ProcessTokenObservation -Role 'installer' -Process $installerProcess -Started $true
Wait-ProcessAndObserveUac -Process $installerProcess -ConsentBaseline $consentBefore -ObserveBrowser
$installExitCode = $installerProcess.ExitCode
$installRoot = Join-Path $env:LOCALAPPDATA 'Programs\Stock Desk'
$appPath = Join-Path $installRoot 'stock-desk-desktop.exe'
$uninstallPath = Join-Path $installRoot 'uninstall.exe'
$applicationFilesPresent = (Test-Path -LiteralPath $appPath -PathType Leaf)
$shortcutPresent = Test-ShortcutPresent
$webviewAfter = Get-WebViewRuntime
$webviewChildObservation = [ordered]@{
  observed = $false
  executable_name = $null
  executable_path_sha256 = $null
  executable_sha256 = $null
  signer = $null
  elevated = $null
  integrity_level = $null
  integrity_rid = $null
  exit_code = $null
}
if ($script:WebViewChildProcess) {
  $script:WebViewChildProcess.WaitForExit()
  $webviewChildObservation = [ordered]@{
    observed = $true
    executable_name = 'MicrosoftEdgeWebView2RuntimeInstaller.exe'
    executable_path_sha256 = Get-StringDigest -Value ([IO.Path]::GetFullPath($script:WebViewChildExecutablePath).ToLowerInvariant())
    executable_sha256 = $script:WebViewChildExecutableSha256
    signer = [ordered]@{ status = 'Valid'; subject = 'CN=Microsoft Corporation'; certificate_sha256 = $script:WebViewChildCertificateSha256 }
    elevated = $script:WebViewChildElevated
    integrity_level = if ($script:WebViewChildIntegrityRid -eq 8192) { 'medium' } elseif ($script:WebViewChildIntegrityRid -ge 12288) { 'high' } else { 'unsupported' }
    integrity_rid = $script:WebViewChildIntegrityRid
    exit_code = $script:WebViewChildProcess.ExitCode
  }
  if ($webviewChildObservation.executable_sha256 -ne [string]$request.webview_installer_sha256) {
    throw 'Observed WebView2 child is not the proved offline installer'
  }
}
$webviewAttempted = [bool]$webviewChildObservation.observed
$failureInjection = if ($Scenario -eq 'webview-install-failure') {
  if ($FailureInjectionIdentity -ne 'stock-desk-webview2-offline-install-failure-v1') { throw 'Failure injection identity is not fixed' }
  Assert-HexDigest -Value $FailureInjectionSha256 -Label 'failure injection digest'
  [ordered]@{ identity = $FailureInjectionIdentity; sha256 = $FailureInjectionSha256 }
} else { $null }
if ($WebViewInitialState -eq 'present' -and $webviewAttempted) { throw 'Preinstalled scenario launched a WebView2 child' }
if ($WebViewInitialState -eq 'absent' -and -not $webviewAttempted) { throw 'Required WebView2 child process was not observed' }
if ($Scenario -eq 'installed-first-use' -and $WebViewInitialState -eq 'absent' -and $webviewChildObservation.exit_code -ne 0) { throw 'WebView2 child installation failed unexpectedly' }
if ($Scenario -eq 'webview-install-failure' -and $webviewChildObservation.exit_code -eq 0) { throw 'Fixed WebView2 fault injection did not cause a child failure' }
Add-Observation -Kind 'webview-installation' -Producer 'windows-process' -Value ([ordered]@{
    attempted = $webviewAttempted
    exit_code = if ($webviewAttempted) { $webviewChildObservation.exit_code } else { $null }
    installer_sha256 = [string]$request.webview_installer_sha256
    fault_injection = $failureInjection
  })
Add-Observation -Kind 'webview-child-process-token' -Producer 'windows-process-token' -Value $webviewChildObservation
Add-Observation -Kind 'webview-after' -Producer 'windows-registry-authenticode' -Value $webviewAfter
Add-Observation -Kind 'installer-process-token' -Producer 'windows-process-token' -Value $installerToken

$desktopProcess = $null
$sidecarProcess = $null
$desktopToken = [ordered]@{ role = 'desktop_host'; started = $false; elevated = $null; integrity_level = $null; integrity_rid = $null }
$sidecarToken = [ordered]@{ role = 'sidecar'; started = $false; elevated = $null; integrity_level = $null; integrity_rid = $null }
$windowObservation = [ordered]@{
  observed = $false
  main_window_count = 0
  title = $null
  external_browser_window_count = 0
  rendered_content_sha256 = $null
  capture_scope = if ($Scenario -eq 'webview-install-failure') { 'none' } else { 'target-window-only' }
  ready_marker = $null
  uia_text_sha256 = $null
  uia_entry_count = 0
  external_browser_observations = $null
  external_browser_window_events = $null
  external_browser_observer = $null
}
$captureFileName = 'window.png'
$capturePath = Join-Path $rawRoot $captureFileName
$captureTextPath = $script:WebViewCaptureTextPath
$failureDiagnosticPath = Join-Path $rawRoot 'failure-diagnostic.txt'
$driverRoot = Join-Path $EvidenceRoot 'driver'
$driverResult = $null
$driverActionPath = Join-Path $driverRoot 'uia-actions.json'
$driverTreePath = Join-Path $driverRoot 'uia-tree.json'

if ($Scenario -ne 'webview-install-failure' -and $installExitCode -eq 0 -and $applicationFilesPresent) {
  $desktopProcess = Start-Process -FilePath $appPath -PassThru
  $desktopToken = Get-ProcessTokenObservation -Role 'desktop_host' -Process $desktopProcess -Started $true
  $deadline = [DateTime]::UtcNow.AddSeconds(45)
  do {
    Add-BrowserObservation -Phase 'app-readiness'
    Start-Sleep -Milliseconds 250
    $desktopProcess.Refresh()
  } while ($desktopProcess.MainWindowHandle -eq [IntPtr]::Zero -and [DateTime]::UtcNow -lt $deadline -and -not $desktopProcess.HasExited)
  $sidecarProcess = Get-CimInstance -ClassName Win32_Process | Where-Object {
    $_.ParentProcessId -eq $desktopProcess.Id -and $_.Name -like 'stock-desk-sidecar*'
  } | Select-Object -First 1
  if ($sidecarProcess) {
    $sidecarNative = Get-Process -Id $sidecarProcess.ProcessId -ErrorAction Stop
    $sidecarToken = Get-ProcessTokenObservation -Role 'sidecar' -Process $sidecarNative -Started $true
  }
  $mainWindowCount = if ($desktopProcess.MainWindowHandle -ne [IntPtr]::Zero) { 1 } else { 0 }
  if ($mainWindowCount -eq 1) {
    if (Test-Path -LiteralPath $driverRoot) { Remove-Item -LiteralPath $driverRoot -Recurse -Force }
    $expectedProvider = if ($DataPath -eq 'primary-blocked-fallback') { 'baostock' } else { 'akshare' }
    & $UiaDriverPath `
      -WindowHandle ([long]$desktopProcess.MainWindowHandle) `
      -ExpectedProcessId $desktopProcess.Id `
      -ExpectedExecutableSha256 (Get-FileDigest -Path $appPath) `
      -ExpectedDpiPercent $DpiPercent `
      -DataPath $DataPath `
      -ExpectedProvider $expectedProvider `
      -NetworkObservationPath $NetworkObservationPath `
      -OutputRoot $driverRoot
    if ($LASTEXITCODE -ne 0) { throw 'Reviewed UI Automation driver failed' }
    foreach ($driverFile in @((Join-Path $driverRoot 'driver-result.json'), $driverActionPath, $driverTreePath)) {
      Assert-RegularFile -Path $driverFile -Label 'UI Automation driver output'
    }
    $driverResult = Get-Content -LiteralPath (Join-Path $driverRoot 'driver-result.json') -Raw | ConvertFrom-Json
    if (
      $driverResult.schema -ne 'stock-desk-windows-uia-driver-result-v1' -or
      $driverResult.candidate.pid -ne $desktopProcess.Id -or
      $driverResult.candidate.hwnd -ne [long]$desktopProcess.MainWindowHandle -or
      $driverResult.uia.driver_sha256 -ne $UiaDriverSha256
    ) { throw 'UI Automation driver output is not bound to the installed candidate' }
    Save-TargetWindowCapture -WindowHandle $desktopProcess.MainWindowHandle -Path $capturePath
    $uiaText = @(Get-WindowAutomationText -WindowHandle $desktopProcess.MainWindowHandle)
    $readyMarker = @($uiaText | Where-Object { $_ -match '(上证指数|000001(?:\.SS)?|欢迎使用|Welcome to|数据源|Data source)' } | Select-Object -First 1)
    if ($readyMarker.Count -ne 1) { throw 'Desktop window has no verified ready marker' }
    for ($stableSample = 0; $stableSample -lt 20; $stableSample += 1) {
      Add-BrowserObservation -Phase 'stable'
      Start-Sleep -Milliseconds 250
    }
    Complete-BrowserWindowObservation
    Write-Utf8NoBom -Path $captureTextPath -Text (($uiaText -join "`n") + "`n")
    $windowObservation = [ordered]@{
      observed = $true
      main_window_count = 1
      title = [string]$desktopProcess.MainWindowTitle
      external_browser_window_count = 0
      rendered_content_sha256 = Get-FileDigest -Path $capturePath
      capture_scope = 'target-window-only'
      ready_marker = [string]$readyMarker[0]
      uia_text_sha256 = Get-FileDigest -Path $captureTextPath
      uia_entry_count = $uiaText.Count
      external_browser_observations = @($script:BrowserTimeline)
      external_browser_window_events = @($script:BrowserWindowEvents)
      external_browser_observer = $script:BrowserObserverSummary
    }
  } else {
    throw 'Desktop host did not expose a unique top-level application window'
  }
} else {
  if ($Scenario -ne 'webview-install-failure' -or $webviewChildObservation.exit_code -eq 0 -or $installExitCode -eq 0) {
    throw 'Failure scenario lacks a nonzero WebView2 child exit and NSIS parent abort'
  }
  Complete-BrowserWindowObservation
  $windowObservation.external_browser_observations = @($script:BrowserTimeline)
  $windowObservation.external_browser_window_events = @($script:BrowserWindowEvents)
  $windowObservation.external_browser_observer = $script:BrowserObserverSummary
  Write-Utf8NoBom -Path $failureDiagnosticPath -Text ((@(
        'Stock Desk WebView2 install failure observation',
        "webview_child_exit_code=$($webviewChildObservation.exit_code)",
        "nsis_parent_exit_code=$installExitCode",
        "failure_injection_identity=$FailureInjectionIdentity",
        "application_files_present=$applicationFilesPresent",
        "shortcut_present=$shortcutPresent"
      ) -join "`n") + "`n")
}

$baselineBrowserIdentity = @($script:BrowserTimeline[0].windows) | ConvertTo-Json -Depth 4 -Compress
foreach ($browserSample in $script:BrowserTimeline) {
  if ((@($browserSample.windows) | ConvertTo-Json -Depth 4 -Compress) -cne $baselineBrowserIdentity) {
    throw 'An external browser top-level window appeared, disappeared, or was replaced during the installed journey'
  }
}
$baselineBrowserHandles = @($script:BrowserTimeline[0].windows | ForEach-Object { [long]$_.window_handle })
foreach ($browserEvent in $script:BrowserWindowEvents) {
  if ($baselineBrowserHandles -notcontains [long]$browserEvent.window_handle) {
    throw 'A non-baseline external browser HWND emitted a lifecycle event during the installed journey'
  }
  if (
    $browserEvent.sequence -gt $script:BrowserObserverSummary.baseline_event_sequence -and
    $browserEvent.event -in @('create', 'hide', 'destroy')
  ) {
    throw 'A baseline external browser HWND changed lifecycle after baseline capture'
  }
}

Add-Observation -Kind 'desktop-host-process-token' -Producer 'windows-process-token' -Value $desktopToken
Add-Observation -Kind 'sidecar-process-token' -Producer 'windows-process-token' -Value $sidecarToken
if ($Scenario -eq 'installed-first-use') {
  Add-Observation -Kind 'hardware-observation' -Producer 'protected-vm-hardware-observer' -Value (Get-Content -LiteralPath $HardwareObservationPath -Raw | ConvertFrom-Json)
  Add-Observation -Kind 'network-observation' -Producer 'protected-vm-network-observer' -Value (Get-Content -LiteralPath $NetworkObservationPath -Raw | ConvertFrom-Json)
  Add-Observation -Kind 'display-observation' -Producer 'windows-uia-win32-driver' -Value $driverResult.display
  Add-Observation -Kind 'first-use-journey' -Producer 'windows-uia-win32-driver' -Value $driverResult.journey
  Add-Observation -Kind 'uia-matrix' -Producer 'windows-uia-win32-driver' -Value $driverResult.uia
} else {
  Add-Observation -Kind 'hardware-observation' -Producer 'protected-vm-hardware-observer' -Value (Get-Content -LiteralPath $HardwareObservationPath -Raw | ConvertFrom-Json)
  Add-Observation -Kind 'network-observation' -Producer 'protected-vm-network-observer' -Value (Get-Content -LiteralPath $NetworkObservationPath -Raw | ConvertFrom-Json)
}

$uninstallerToken = [ordered]@{ role = 'uninstaller'; started = $false; elevated = $null; integrity_level = $null; integrity_rid = $null }
$uninstallObservation = [ordered]@{
  attempted = $false
  exit_code = $null
  application_files_removed = $null
  shortcuts_removed = $null
}
if ($Scenario -ne 'webview-install-failure' -and (Test-Path -LiteralPath $uninstallPath -PathType Leaf)) {
  if ($desktopProcess -and -not $desktopProcess.HasExited) {
    Request-GracefulExit -Process $desktopProcess
  }
  $uninstaller = Start-Process -FilePath $uninstallPath -ArgumentList '/S' -PassThru
  $uninstallerToken = Get-ProcessTokenObservation -Role 'uninstaller' -Process $uninstaller -Started $true
  Wait-ProcessAndObserveUac -Process $uninstaller -ConsentBaseline $consentBefore
  $uninstallObservation = [ordered]@{
    attempted = $true
    exit_code = $uninstaller.ExitCode
    application_files_removed = -not (Test-Path -LiteralPath $appPath -PathType Leaf)
    shortcuts_removed = -not (Test-ShortcutPresent)
  }
}
Add-Observation -Kind 'uninstaller-process-token' -Producer 'windows-process-token' -Value $uninstallerToken
Add-Observation -Kind 'filesystem-observation' -Producer 'protected-vm-filesystem-observer' -Value (Get-Content -LiteralPath $FilesystemObservationPath -Raw | ConvertFrom-Json)

Add-Observation -Kind 'uac-observation' -Producer 'windows-event-observer' -Value ([ordered]@{
    uac_prompt_count = $script:UacPromptCount
    elevation_requested = [bool]($installerToken.elevated -or $desktopToken.elevated -or $sidecarToken.elevated -or $uninstallerToken.elevated)
  })
Add-Observation -Kind 'install-observation' -Producer 'windows-filesystem' -Value ([ordered]@{
    exit_code = $installExitCode
    application_files_present = $applicationFilesPresent
    shortcut_present = $shortcutPresent
    launchable = [bool]($desktopToken.started -and $windowObservation.observed)
  })
Add-Observation -Kind 'window-observation' -Producer 'windows-window-observer' -Value $windowObservation
Add-Observation -Kind 'v1-canary-before' -Producer 'windows-filesystem' -Value $canaryBefore
Add-Observation -Kind 'v1-canary-after' -Producer 'windows-filesystem' -Value (Get-CanarySnapshot -Root $v1Root)

$logLines = @(
  'Stock Desk installed-Windows raw observation log',
  "scenario=$Scenario",
  "installer_exit_code=$installExitCode",
  "application_files_present=$applicationFilesPresent",
  "shortcut_present=$shortcutPresent",
  "window_observed=$($windowObservation.observed)"
)
Write-Utf8NoBom -Path $installLogPath -Text (($logLines -join "`n") + "`n")
$detailPath = if ($Scenario -eq 'webview-install-failure') { $failureDiagnosticPath } else { $captureTextPath }
$publicText = ($script:Events -join "`n") + "`n" + (Get-Content -LiteralPath $installLogPath -Raw) + "`n" + (Get-Content -LiteralPath $detailPath -Raw)
$secretPattern = '(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,]+'
$usernamePattern = [regex]::Escape($username)
$absolutePathPattern = '(?i)[a-z]:\\users\\'
$redaction = [ordered]@{
  secret_match_count = ([regex]::Matches($publicText, $secretPattern)).Count
  username_match_count = ([regex]::Matches($publicText, $usernamePattern)).Count
  absolute_path_match_count = ([regex]::Matches($publicText, $absolutePathPattern)).Count
}
Add-Observation -Kind 'redaction-scan' -Producer 'stock-desk-redaction-scan' -Value $redaction
Add-Observation -Kind 'uninstall-observation' -Producer 'windows-filesystem' -Value $uninstallObservation
if ($redaction.secret_match_count -ne 0 -or $redaction.username_match_count -ne 0 -or $redaction.absolute_path_match_count -ne 0) {
  Remove-Item -LiteralPath $publicRoot -Recurse -Force
  throw 'Public raw evidence failed the in-guest redaction preflight'
}

$observationPath = Join-Path $rawRoot 'observations.jsonl'
Write-Utf8NoBom -Path $observationPath -Text (($script:Events -join "`n") + "`n")
$completedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
$records = @(
  [ordered]@{
    kind = 'observation-stream'
    path = 'raw/observations.jsonl'
    sha256 = Get-FileDigest -Path $observationPath
    size_bytes = (Get-Item -LiteralPath $observationPath).Length
    media_type = 'application/x-ndjson'
  },
  [ordered]@{
    kind = 'install-log'
    path = 'raw/install.log'
    sha256 = Get-FileDigest -Path $installLogPath
    size_bytes = (Get-Item -LiteralPath $installLogPath).Length
    media_type = 'text/plain; charset=utf-8'
  }
)
if ($Scenario -eq 'webview-install-failure') {
  $records += [ordered]@{
    kind = 'failure-diagnostic'
    path = 'raw/failure-diagnostic.txt'
    sha256 = Get-FileDigest -Path $failureDiagnosticPath
    size_bytes = (Get-Item -LiteralPath $failureDiagnosticPath).Length
    media_type = 'text/plain; charset=utf-8'
  }
} else {
  $driverCopies = @(
    @('uia-action-trace', $driverActionPath, 'uia-actions.json', 'application/json'),
    @('uia-tree', $driverTreePath, 'uia-tree.json', 'application/json'),
    @('focus-region-contact-sheet', (Join-Path $driverRoot 'focus-region-contact-sheet.png'), 'focus-region-contact-sheet.png', 'image/png'),
    @('window-capture-standard', (Join-Path $driverRoot 'window-standard.png'), 'window-standard.png', 'image/png'),
    @('window-capture-narrow', (Join-Path $driverRoot 'window-narrow.png'), 'window-narrow.png', 'image/png')
  )
  foreach ($copy in $driverCopies) {
    Assert-RegularFile -Path $copy[1] -Label 'UI Automation public record'
    $destination = Join-Path $rawRoot $copy[2]
    Copy-Item -LiteralPath $copy[1] -Destination $destination
    $records += [ordered]@{
      kind = $copy[0]
      path = "raw/$($copy[2])"
      sha256 = Get-FileDigest -Path $destination
      size_bytes = (Get-Item -LiteralPath $destination).Length
      media_type = $copy[3]
    }
  }
  Remove-Item -LiteralPath $captureTextPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $capturePath -Force -ErrorAction SilentlyContinue
}
$manifest = [ordered]@{
  schema_version = 2
  artifact = 'windows-installed-raw-evidence'
  case_id = $CaseId
  scenario = $Scenario
  identity = [ordered]@{
    source_sha = [string]$request.source_sha
    source_tree = [string]$request.tree_sha
    main_proof_sha256 = [string]$request.main_proof_sha256
    candidate_sha256 = [string]$request.candidate_sha256
    webview_installer_sha256 = [string]$request.webview_installer_sha256
  }
  execution = [ordered]@{
    repository = $ActionsRepository
    workflow = $ActionsWorkflow
    workflow_ref = $ActionsWorkflowRef
    workflow_sha = $ActionsWorkflowSha
    workflow_path = $ActionsWorkflowPath
    workflow_sha256 = $ActionsWorkflowSha256
    run_id = $ActionsRunId
    run_attempt = $ActionsRunAttempt
    job_id = $ActionsJobId
    job_name = $ActionsJobName
    matrix_case_id = $CaseId
    matrix_guest_profile = $GuestProfile
    matrix_scenario = $Scenario
    matrix_dpi_percent = $DpiPercent
    matrix_controller_label = $ControllerLabel
    scenario_attempt = $ScenarioAttempt
    attempt_id = "$Scenario-first-$ActionsRunId"
  }
  capture = [ordered]@{
    started_at_utc = $startedAt
    completed_at_utc = $completedAt
    guest_profile = $GuestProfile
    controller_label = $ControllerLabel
    dpi_percent = $DpiPercent
    guest_harness_sha256 = $guestSelfSha256
    uia_driver_sha256 = $UiaDriverSha256
    controller_request_sha256 = $ControllerRequestSha256
    snapshot_policy_sha256 = $SnapshotPolicySha256
    clean_snapshot_sha256 = $CleanSnapshotSha256
    image_sha256 = $ImageSha256
    webview_product_guid = $WebView2ProductionGuid
    minimum_webview_version = $MinimumWebView2Version.ToString()
    failure_injection = $failureInjection
    data_path = $DataPath
    redaction_version = 'stock-desk-public-redaction-v2'
  }
  records = $records
}
$manifestPath = Join-Path $publicRoot 'raw-manifest.json'
Write-Utf8NoBom -Path $manifestPath -Text (($manifest | ConvertTo-Json -Depth 12) + "`n")

exit 0
