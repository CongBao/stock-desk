//! Windows-owned security boundary for trusted updater installation.
//!
//! The WebView can request an update confirmation through its command, but it
//! cannot supply a confirmation result, an installer path, or an Authenticode
//! result to this module.  A consent token is minted only by the host-owned
//! native dialog.  Likewise, WinVerifyTrust only accepts an opaque staged file
//! created here from bytes bound to an expected SHA-256 digest.

#![cfg_attr(not(windows), allow(dead_code))]

use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

const MAX_INSTALLER_BYTES: u64 = crate::updater_transport::MAX_ASSET_BYTES as u64;
const STAGING_ATTEMPTS: usize = 16;
#[cfg(windows)]
const STAGE_REOPEN_ATTEMPTS: usize = 10;
#[cfg(windows)]
const STAGE_REOPEN_DELAY_MILLIS: u64 = 50;

// MessageBoxW values are kept here so the safe-default contract remains
// testable on every development platform without importing Windows bindings.
const DIALOG_RESULT_OK: i32 = 1;
const DIALOG_RESULT_FAILURE: i32 = 0;
const MESSAGE_BOX_OK_CANCEL: u32 = 0x0000_0001;
const MESSAGE_BOX_ICON_WARNING: u32 = 0x0000_0030;
const MESSAGE_BOX_DEFAULT_BUTTON_2: u32 = 0x0000_0100;
const MESSAGE_BOX_TASK_MODAL: u32 = 0x0000_2000;
const MESSAGE_BOX_SET_FOREGROUND: u32 = 0x0001_0000;
const NATIVE_CONFIRMATION_STYLE: u32 = MESSAGE_BOX_OK_CANCEL
    | MESSAGE_BOX_ICON_WARNING
    | MESSAGE_BOX_DEFAULT_BUTTON_2
    | MESSAGE_BOX_TASK_MODAL
    | MESSAGE_BOX_SET_FOREGROUND;

/// Capability proving that the desktop host received an affirmative native
/// decision.  Its field is private, and the type is neither serializable nor
/// constructible by a WebView command argument.
#[derive(Debug)]
pub(crate) struct NativeUpdateConsent(());

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NativeDialogDecision {
    Confirm,
    Cancel,
    Failed,
}

/// A staged installer remains open without write/delete sharing for its whole
/// verification and installation hand-off.  Callers can read its fixed path,
/// but cannot construct this type around an arbitrary path.
#[derive(Debug)]
pub(crate) struct SecureStagedInstaller {
    path: PathBuf,
    file: Option<File>,
    sha256: String,
    authenticode_verified: bool,
    lifecycle: StageLifecycle,
    #[cfg(windows)]
    directory_guard: Option<SecureDirectoryGuard>,
    #[cfg(windows)]
    identity: WindowsFileIdentity,
}

impl SecureStagedInstaller {
    fn file_mut(&mut self) -> Result<&mut File, &'static str> {
        self.file
            .as_mut()
            .ok_or("desktop_updater_staged_installer_closed")
    }
}

impl Drop for SecureStagedInstaller {
    fn drop(&mut self) {
        cleanup_open_stage(&mut self.file, &self.path, self.lifecycle.delete_on_drop());
        #[cfg(windows)]
        drop(self.directory_guard.take());
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum StageLifecycle {
    Pending,
    Launched,
}

impl StageLifecycle {
    fn delete_on_drop(self) -> bool {
        self == Self::Pending
    }

    fn mark_launched(&mut self) {
        *self = Self::Launched;
    }
}

#[cfg(windows)]
#[derive(Debug)]
struct SecureDirectoryGuard {
    path: PathBuf,
    _file: File,
    identity: WindowsFileIdentity,
}

#[cfg(windows)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WindowsFileIdentity {
    volume_serial_number: u32,
    file_index: u64,
}

struct PendingStage {
    path: PathBuf,
    file: Option<File>,
    delete_on_drop: bool,
}

impl Drop for PendingStage {
    fn drop(&mut self) {
        cleanup_open_stage(&mut self.file, &self.path, self.delete_on_drop);
    }
}

/// Show the host-owned confirmation dialog.
///
/// The only argument is the trusted host handle used to bind the fixed main
/// window as owner. Web IPC cannot choose the wording, default button, owner
/// result, or affirmative decision. Closing the dialog, pressing Escape, or
/// accepting the default second button all cancel.
#[cfg(not(windows))]
pub(crate) fn request_native_update_confirmation(
    _app: &tauri::AppHandle,
) -> Result<NativeUpdateConsent, &'static str> {
    Err("desktop_updater_native_confirmation_unavailable")
}

#[cfg(windows)]
pub(crate) fn request_native_update_confirmation(
    app: &tauri::AppHandle,
) -> Result<NativeUpdateConsent, &'static str> {
    use tauri::Manager as _;
    use windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW;

    let owner = app
        .get_webview_window("main")
        .ok_or("desktop_updater_native_confirmation_failed")?
        .hwnd()
        .map_err(|_| "desktop_updater_native_confirmation_failed")?;
    let title = wide("Stock Desk 安全更新");
    let message = wide(
        "Stock Desk 发现了新的正式版本。\r\n\r\n是否下载并验证更新？只有 Tauri 签名、SHA-256 与 Windows 可信签名全部通过后才会进入安全退出和安装；默认选择为取消。",
    );
    // SAFETY: both UTF-16 buffers are NUL terminated and remain alive for the
    // duration of the synchronous call. The owner comes only from the fixed
    // host main window and can never be supplied by Web IPC.
    let result = unsafe {
        MessageBoxW(
            owner.0 as _,
            message.as_ptr(),
            title.as_ptr(),
            NATIVE_CONFIRMATION_STYLE,
        )
    };
    consent_from_native_result(result)
}

fn consent_from_native_result(result: i32) -> Result<NativeUpdateConsent, &'static str> {
    match classify_native_dialog_result(result) {
        NativeDialogDecision::Confirm => Ok(NativeUpdateConsent(())),
        NativeDialogDecision::Cancel => Err("desktop_updater_confirmation_cancelled"),
        NativeDialogDecision::Failed => Err("desktop_updater_native_confirmation_failed"),
    }
}

fn classify_native_dialog_result(result: i32) -> NativeDialogDecision {
    match result {
        DIALOG_RESULT_OK => NativeDialogDecision::Confirm,
        DIALOG_RESULT_FAILURE => NativeDialogDecision::Failed,
        _ => NativeDialogDecision::Cancel,
    }
}

/// Copy the installer into a locked, uniquely-created staging file.
///
/// `payload`, `staging_directory`, and `expected_sha256` must come from the
/// trusted host update controller, never directly from Web IPC.  Non-Windows
/// builds fail before touching either path.
#[cfg(not(windows))]
pub(crate) fn stage_installer(
    _payload: &[u8],
    _staging_directory: &Path,
    _expected_sha256: &str,
) -> Result<SecureStagedInstaller, &'static str> {
    Err("desktop_updater_secure_staging_unavailable")
}

#[cfg(windows)]
pub(crate) fn stage_installer(
    payload: &[u8],
    staging_directory: &Path,
    expected_sha256: &str,
) -> Result<SecureStagedInstaller, &'static str> {
    use std::fs::{self, OpenOptions};
    use std::os::windows::fs::OpenOptionsExt as _;
    use windows_sys::Win32::Foundation::{GENERIC_READ, GENERIC_WRITE};
    use windows_sys::Win32::Storage::FileSystem::{
        DELETE, FILE_FLAG_SEQUENTIAL_SCAN, FILE_FLAG_WRITE_THROUGH, FILE_SHARE_READ,
    };

    validate_expected_sha256(expected_sha256)?;
    if payload.is_empty() {
        return Err("desktop_updater_payload_empty");
    }
    if payload.len() as u64 > MAX_INSTALLER_BYTES {
        return Err("desktop_updater_payload_too_large");
    }
    reject_reparse_ancestors(staging_directory)?;
    fs::create_dir_all(staging_directory)
        .map_err(|_| "desktop_updater_staging_directory_unavailable")?;
    reject_reparse_ancestors(staging_directory)?;
    reject_reparse_point(
        staging_directory,
        "desktop_updater_staging_directory_unsafe",
    )?;
    if !staging_directory
        .metadata()
        .map_err(|_| "desktop_updater_staging_directory_unavailable")?
        .is_dir()
    {
        return Err("desktop_updater_staging_directory_unsafe");
    }
    let directory_guard = open_directory_guard(staging_directory)?;
    revalidate_directory_guard(&directory_guard)?;

    let (path, output) = (0..STAGING_ATTEMPTS)
        .find_map(|_| {
            let path = staging_directory.join(staging_name(random_nonce().ok()?));
            let output = OpenOptions::new()
                .access_mode(GENERIC_READ | GENERIC_WRITE | DELETE)
                .create_new(true)
                .share_mode(FILE_SHARE_READ)
                .custom_flags(FILE_FLAG_WRITE_THROUGH | FILE_FLAG_SEQUENTIAL_SCAN)
                .open(&path)
                .ok()?;
            Some((path, output))
        })
        .ok_or("desktop_updater_staging_file_unavailable")?;
    let mut pending = PendingStage {
        path,
        file: Some(output),
        delete_on_drop: true,
    };
    let copied_digest = write_and_hash(
        payload,
        pending
            .file
            .as_mut()
            .ok_or("desktop_updater_staging_file_unavailable")?,
    )?;
    if copied_digest != expected_sha256 {
        return Err("desktop_updater_sha256_mismatch");
    }
    let output = pending
        .file
        .as_mut()
        .ok_or("desktop_updater_staging_file_unavailable")?;
    output
        .flush()
        .and_then(|()| output.sync_all())
        .map_err(|_| "desktop_updater_staging_write_failed")?;
    if hash_open_file(output)? != expected_sha256 {
        return Err("desktop_updater_staged_identity_mismatch");
    }
    let written_identity = file_identity(output)?;
    let path = pending.path.clone();
    let writable = pending
        .file
        .take()
        .ok_or("desktop_updater_staging_file_unavailable")?;
    drop(writable);

    // The writable/delete-capable handle cannot remain open while Windows
    // creates a process from this image: file sharing compatibility is
    // bidirectional. Reopen the same file read-only and compare the stable
    // Windows file identity before trusting the new handle.
    let readonly = open_readonly_stage(&path)?;
    pending.file = Some(readonly);
    let readonly = pending
        .file
        .as_mut()
        .ok_or("desktop_updater_staging_file_unavailable")?;
    if file_identity(readonly)? != written_identity {
        return Err("desktop_updater_staged_identity_mismatch");
    }
    reject_reparse_handle(readonly, "desktop_updater_staging_file_unsafe")?;
    if hash_open_file(readonly)? != expected_sha256 {
        return Err("desktop_updater_staged_identity_mismatch");
    }
    revalidate_directory_guard(&directory_guard)?;
    let file = pending
        .file
        .take()
        .ok_or("desktop_updater_staging_file_unavailable")?;
    pending.delete_on_drop = false;
    Ok(SecureStagedInstaller {
        path,
        file: Some(file),
        sha256: copied_digest,
        authenticode_verified: false,
        lifecycle: StageLifecycle::Pending,
        directory_guard: Some(directory_guard),
        identity: written_identity,
    })
}

/// Verify Authenticode trust over the exact locked staged object.
#[cfg(not(windows))]
pub(crate) fn verify_authenticode(
    _installer: &mut SecureStagedInstaller,
) -> Result<(), &'static str> {
    Err("desktop_updater_winverifytrust_unavailable")
}

#[cfg(windows)]
pub(crate) fn verify_authenticode(
    installer: &mut SecureStagedInstaller,
) -> Result<(), &'static str> {
    if hash_open_file(installer.file_mut()?)? != installer.sha256 {
        return Err("desktop_updater_staged_identity_mismatch");
    }
    verify_authenticode_windows(installer)?;
    if hash_open_file(installer.file_mut()?)? != installer.sha256 {
        return Err("desktop_updater_staged_identity_mismatch");
    }
    installer.authenticode_verified = true;
    Ok(())
}

/// Launch the exact locked NSIS file that passed SHA-256 and WinVerifyTrust.
///
/// The argument vector is fixed by the host and mirrors Tauri's passive NSIS
/// update mode. No path or argument can originate from Web IPC.
#[cfg(not(windows))]
pub(crate) fn launch_verified_installer(
    _app: &tauri::AppHandle,
    _installer: &mut SecureStagedInstaller,
) -> Result<(), &'static str> {
    Err("desktop_updater_installer_launch_unavailable")
}

#[cfg(windows)]
pub(crate) fn launch_verified_installer(
    _app: &tauri::AppHandle,
    installer: &mut SecureStagedInstaller,
) -> Result<(), &'static str> {
    use std::os::windows::ffi::OsStrExt as _;
    use windows_sys::Win32::System::Threading::{
        CreateProcessW, PROCESS_INFORMATION, STARTUPINFOW,
    };

    if !installer.authenticode_verified {
        return Err("desktop_updater_authenticode_not_verified");
    }
    if hash_open_file(installer.file_mut()?)? != installer.sha256 {
        return Err("desktop_updater_staged_identity_mismatch");
    }
    let directory_guard = installer
        .directory_guard
        .as_ref()
        .ok_or("desktop_updater_staging_directory_unavailable")?;
    revalidate_directory_guard(directory_guard)?;
    let mut resolved_path_guard = open_readonly_stage(&installer.path)?;
    if file_identity(&resolved_path_guard)? != installer.identity
        || hash_open_file(&mut resolved_path_guard)? != installer.sha256
    {
        return Err("desktop_updater_staged_identity_mismatch");
    }
    let application_path: Vec<u16> = installer
        .path
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let mut command_line = Vec::with_capacity(application_path.len() + 32);
    command_line.push(b'"' as u16);
    command_line.extend_from_slice(&application_path[..application_path.len() - 1]);
    command_line.extend("\" /P /R /UPDATE /ARGS".encode_utf16());
    command_line.push(0);
    let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
    startup.cb = std::mem::size_of::<STARTUPINFOW>() as u32;
    let mut process: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: `application_path` and mutable `command_line` are NUL terminated
    // and remain alive for the synchronous call. The application path belongs
    // to the still-open read-only/no-write/no-delete-share staged file, while
    // the parent directory guard prevents a path swap during process creation.
    let launched = unsafe {
        CreateProcessW(
            application_path.as_ptr(),
            command_line.as_mut_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            0,
            0,
            std::ptr::null(),
            std::ptr::null(),
            &startup,
            &mut process,
        )
    };
    if launched == 0 || process.hProcess.is_null() || process.hThread.is_null() {
        close_process_information(process);
        return Err("desktop_updater_installer_launch_failed");
    }
    let process_guard = ProcessInformationGuard(process);

    // CreateProcessW returned real process and primary-thread handles, so
    // Windows has created the child from the selected image. The installer
    // must remain on disk for NSIS to read its own payload after this host
    // exits. A future startup owns best-effort cleanup of this strict name.
    installer.lifecycle.mark_launched();
    drop(installer.directory_guard.take());
    drop(resolved_path_guard);
    drop(process_guard);
    Ok(())
}

#[cfg(windows)]
struct ProcessInformationGuard(windows_sys::Win32::System::Threading::PROCESS_INFORMATION);

#[cfg(windows)]
impl Drop for ProcessInformationGuard {
    fn drop(&mut self) {
        close_process_information(self.0);
    }
}

#[cfg(windows)]
fn close_process_information(process: windows_sys::Win32::System::Threading::PROCESS_INFORMATION) {
    // SAFETY: CreateProcessW transfers both non-null handles to the caller.
    // Failure paths may leave either field null, which must not be closed.
    unsafe {
        if !process.hThread.is_null() {
            let _ = windows_sys::Win32::Foundation::CloseHandle(process.hThread);
        }
        if !process.hProcess.is_null() {
            let _ = windows_sys::Win32::Foundation::CloseHandle(process.hProcess);
        }
    }
}

#[cfg(windows)]
fn verify_authenticode_windows(installer: &SecureStagedInstaller) -> Result<(), &'static str> {
    use std::os::windows::ffi::OsStrExt as _;
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Security::WinTrust::{
        WinVerifyTrustEx, WINTRUST_ACTION_GENERIC_VERIFY_V2, WINTRUST_DATA, WINTRUST_DATA_0,
        WINTRUST_FILE_INFO, WTD_CHOICE_FILE, WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT,
        WTD_REVOKE_WHOLECHAIN, WTD_STATEACTION_CLOSE, WTD_STATEACTION_VERIFY,
        WTD_UICONTEXT_INSTALL, WTD_UI_NONE,
    };

    let file = installer
        .file
        .as_ref()
        .ok_or("desktop_updater_staged_installer_closed")?;
    let wide_path: Vec<u16> = installer
        .path
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let mut file_info = WINTRUST_FILE_INFO {
        cbStruct: std::mem::size_of::<WINTRUST_FILE_INFO>() as u32,
        pcwszFilePath: wide_path.as_ptr(),
        hFile: file.as_raw_handle(),
        pgKnownSubject: std::ptr::null_mut(),
    };
    let mut trust_data = WINTRUST_DATA {
        cbStruct: std::mem::size_of::<WINTRUST_DATA>() as u32,
        pPolicyCallbackData: std::ptr::null_mut(),
        pSIPClientData: std::ptr::null_mut(),
        dwUIChoice: WTD_UI_NONE,
        fdwRevocationChecks: WTD_REVOKE_WHOLECHAIN,
        dwUnionChoice: WTD_CHOICE_FILE,
        Anonymous: WINTRUST_DATA_0 {
            pFile: &mut file_info,
        },
        dwStateAction: WTD_STATEACTION_VERIFY,
        hWVTStateData: std::ptr::null_mut(),
        pwszURLReference: std::ptr::null_mut(),
        dwProvFlags: WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT,
        dwUIContext: WTD_UICONTEXT_INSTALL,
        pSignatureSettings: std::ptr::null_mut(),
    };
    let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    // SAFETY: every pointer refers to a live, correctly-sized Windows struct;
    // the file handle remains locked for this call and the mandatory CLOSE.
    let verify_status =
        unsafe { WinVerifyTrustEx(std::ptr::null_mut(), &mut action, &mut trust_data) };
    trust_data.dwStateAction = WTD_STATEACTION_CLOSE;
    // SAFETY: this reuses the exact state returned by VERIFY and closes it
    // before any Rust value referenced by WINTRUST_DATA is dropped.
    let close_status =
        unsafe { WinVerifyTrustEx(std::ptr::null_mut(), &mut action, &mut trust_data) };
    evaluate_winverifytrust_status(verify_status, close_status)
}

fn evaluate_winverifytrust_status(
    verify_status: i32,
    close_status: i32,
) -> Result<(), &'static str> {
    if close_status != 0 {
        return Err("desktop_updater_winverifytrust_close_failed");
    }
    if verify_status != 0 {
        return Err("desktop_updater_authenticode_invalid");
    }
    Ok(())
}

fn validate_expected_sha256(expected: &str) -> Result<(), &'static str> {
    if expected.len() == 64
        && expected
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err("desktop_updater_sha256_invalid")
    }
}

fn write_and_hash(payload: &[u8], output: &mut File) -> Result<String, &'static str> {
    if payload.is_empty() {
        return Err("desktop_updater_payload_empty");
    }
    if payload.len() as u64 > MAX_INSTALLER_BYTES {
        return Err("desktop_updater_payload_too_large");
    }
    let mut digest = Sha256::new();
    output
        .write_all(payload)
        .map_err(|_| "desktop_updater_staging_write_failed")?;
    digest.update(payload);
    Ok(hex_digest(digest.finalize()))
}

fn hash_open_file(file: &mut File) -> Result<String, &'static str> {
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "desktop_updater_staged_installer_unreadable")?;
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|_| "desktop_updater_staged_installer_unreadable")?;
        if read == 0 {
            break;
        }
        total = total
            .checked_add(read as u64)
            .ok_or("desktop_updater_payload_too_large")?;
        if total > MAX_INSTALLER_BYTES {
            return Err("desktop_updater_payload_too_large");
        }
        digest.update(&buffer[..read]);
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "desktop_updater_staged_installer_unreadable")?;
    if total == 0 {
        return Err("desktop_updater_payload_empty");
    }
    Ok(hex_digest(digest.finalize()))
}

fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    let bytes = bytes.as_ref();
    let mut encoded = String::with_capacity(bytes.len() * 2);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for &byte in bytes {
        encoded.push(HEX[(byte >> 4) as usize] as char);
        encoded.push(HEX[(byte & 0x0f) as usize] as char);
    }
    encoded
}

fn staging_name(nonce: [u8; 16]) -> String {
    format!("stock-desk-update-{}.exe", hex_digest(nonce))
}

fn random_nonce() -> Result<[u8; 16], &'static str> {
    let mut nonce = [0_u8; 16];
    getrandom::fill(&mut nonce).map_err(|_| "desktop_updater_staging_random_unavailable")?;
    Ok(nonce)
}

fn is_strict_staging_name(name: &std::ffi::OsStr) -> bool {
    let Some(name) = name.to_str() else {
        return false;
    };
    let Some(nonce) = name
        .strip_prefix("stock-desk-update-")
        .and_then(|value| value.strip_suffix(".exe"))
    else {
        return false;
    };
    nonce.len() == 32
        && nonce
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Best-effort cleanup for installers deliberately retained after a successful
/// process launch. Only exact generated names are considered, and reparse
/// points or files that are still in use are always left untouched.
#[cfg(not(windows))]
#[allow(dead_code)]
pub(crate) fn cleanup_staging_directory(_staging_directory: &Path) {}

#[cfg(windows)]
#[allow(dead_code)]
pub(crate) fn cleanup_staging_directory(staging_directory: &Path) {
    use std::fs::OpenOptions;
    use std::os::windows::fs::OpenOptionsExt as _;
    use windows_sys::Win32::Foundation::GENERIC_READ;
    use windows_sys::Win32::Storage::FileSystem::{
        DELETE, FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_SEQUENTIAL_SCAN, FILE_SHARE_READ,
    };

    if reject_reparse_ancestors(staging_directory).is_err() {
        return;
    }
    let Ok(directory_guard) = open_directory_guard(staging_directory) else {
        return;
    };
    if revalidate_directory_guard(&directory_guard).is_err() {
        return;
    }
    let Ok(entries) = std::fs::read_dir(staging_directory) else {
        return;
    };
    for entry in entries.flatten() {
        if !is_strict_staging_name(&entry.file_name()) {
            continue;
        }
        let path = entry.path();
        let Ok(file) = OpenOptions::new()
            .access_mode(GENERIC_READ | DELETE)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN)
            .open(&path)
        else {
            continue;
        };
        if reject_reparse_handle(&file, "desktop_updater_staging_file_unsafe").is_err() {
            continue;
        }
        let mut file = Some(file);
        // This handle owns DELETE access and denies delete sharing, so marking
        // the exact opened object avoids deleting a path replacement.
        let _ = mark_delete_on_close(&file);
        drop(file.take());
    }
}

#[cfg(windows)]
fn open_directory_guard(path: &Path) -> Result<SecureDirectoryGuard, &'static str> {
    use std::os::windows::ffi::OsStrExt as _;
    use std::os::windows::io::FromRawHandle as _;
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
        OPEN_EXISTING,
    };

    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: `wide` is NUL terminated. Desired access zero is sufficient for
    // identity queries; FILE_SHARE_READ deliberately denies write/delete opens
    // of this directory while the updater resolves and launches its child.
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            0,
            FILE_SHARE_READ,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err("desktop_updater_staging_directory_unavailable");
    }
    // SAFETY: ownership of the valid CreateFileW handle transfers to File.
    let file = unsafe { File::from_raw_handle(handle) };
    reject_reparse_handle(&file, "desktop_updater_staging_directory_unsafe")?;
    bind_handle_to_lexical_path(&file, path, "desktop_updater_staging_directory_unsafe")?;
    let identity = file_identity(&file)?;
    Ok(SecureDirectoryGuard {
        path: path.to_path_buf(),
        _file: file,
        identity,
    })
}

#[cfg(windows)]
fn open_readonly_stage(path: &Path) -> Result<File, &'static str> {
    use std::fs::OpenOptions;
    use std::os::windows::fs::OpenOptionsExt as _;
    use windows_sys::Win32::Foundation::GENERIC_READ;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_SEQUENTIAL_SCAN, FILE_SHARE_READ,
    };

    for attempt in 0..STAGE_REOPEN_ATTEMPTS {
        let opened = OpenOptions::new()
            .access_mode(GENERIC_READ)
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN)
            .open(path);
        match opened {
            Ok(file) => {
                reject_reparse_handle(&file, "desktop_updater_staging_file_unsafe")?;
                return Ok(file);
            }
            Err(error)
                if is_transient_stage_reopen_error(&error)
                    && attempt + 1 < STAGE_REOPEN_ATTEMPTS =>
            {
                std::thread::sleep(std::time::Duration::from_millis(STAGE_REOPEN_DELAY_MILLIS));
            }
            Err(_) => return Err("desktop_updater_staging_file_unavailable"),
        }
    }
    Err("desktop_updater_staging_file_unavailable")
}

#[cfg(windows)]
fn is_transient_stage_reopen_error(error: &std::io::Error) -> bool {
    matches!(error.raw_os_error(), Some(32) | Some(33))
}

#[cfg(windows)]
fn revalidate_directory_guard(guard: &SecureDirectoryGuard) -> Result<(), &'static str> {
    let current = open_directory_guard_once(&guard.path)?;
    if file_identity(&current)? != guard.identity {
        return Err("desktop_updater_staging_directory_unsafe");
    }
    Ok(())
}

#[cfg(windows)]
fn open_directory_guard_once(path: &Path) -> Result<File, &'static str> {
    use std::os::windows::ffi::OsStrExt as _;
    use std::os::windows::io::FromRawHandle as _;
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
        OPEN_EXISTING,
    };

    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: identical to open_directory_guard; this temporary handle proves
    // the path still resolves to the guarded directory identity.
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            0,
            FILE_SHARE_READ,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err("desktop_updater_staging_directory_unavailable");
    }
    // SAFETY: ownership of the valid CreateFileW handle transfers to File.
    let file = unsafe { File::from_raw_handle(handle) };
    reject_reparse_handle(&file, "desktop_updater_staging_directory_unsafe")?;
    bind_handle_to_lexical_path(&file, path, "desktop_updater_staging_directory_unsafe")?;
    Ok(file)
}

#[cfg(windows)]
fn bind_handle_to_lexical_path(
    file: &File,
    expected: &Path,
    error: &'static str,
) -> Result<(), &'static str> {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED, VOLUME_NAME_DOS,
    };

    let expected = long_windows_lexical_path(expected).map_err(|_| error)?;
    // SAFETY: a zero-length probe with a null buffer returns the required
    // UTF-16 capacity for the live directory handle.
    let required = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            std::ptr::null_mut(),
            0,
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
        )
    };
    if required == 0 {
        return Err(error);
    }
    let mut resolved = vec![0_u16; required as usize + 1];
    // SAFETY: `resolved` is a writable buffer with the probed capacity and the
    // exact handle remains open for the synchronous call.
    let written = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            resolved.as_mut_ptr(),
            resolved.len() as u32,
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
        )
    };
    if written == 0 || written as usize >= resolved.len() {
        return Err(error);
    }
    resolved.truncate(written as usize);
    let resolved = strip_windows_extended_prefix(&resolved).ok_or(error)?;
    if !windows_path_units_equal(&expected, resolved) {
        return Err(error);
    }
    Ok(())
}

#[cfg(windows)]
fn long_windows_lexical_path(path: &Path) -> std::io::Result<Vec<u16>> {
    use std::os::windows::ffi::OsStrExt as _;
    use windows_sys::Win32::Storage::FileSystem::GetLongPathNameW;

    let absolute = std::path::absolute(path)?;
    let input: Vec<u16> = absolute.as_os_str().encode_wide().chain(Some(0)).collect();
    // GitHub's Windows temp directory may be supplied through an 8.3 alias,
    // while the final-path API reports the long spelling. Expand only the
    // lexical spelling before binding it to the already-open directory handle.
    let required = unsafe { GetLongPathNameW(input.as_ptr(), std::ptr::null_mut(), 0) };
    if required == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let mut expanded = vec![0_u16; required as usize];
    // SAFETY: `input` is NUL terminated and `expanded` has the probed capacity;
    // both buffers remain live for the synchronous call.
    let written =
        unsafe { GetLongPathNameW(input.as_ptr(), expanded.as_mut_ptr(), expanded.len() as u32) };
    if written == 0 || written as usize >= expanded.len() {
        return Err(std::io::Error::last_os_error());
    }
    expanded.truncate(written as usize);
    Ok(expanded)
}

#[cfg(windows)]
fn strip_windows_extended_prefix(path: &[u16]) -> Option<&[u16]> {
    const DOS_PREFIX: &[u16] = &[b'\\' as u16, b'\\' as u16, b'?' as u16, b'\\' as u16];
    const UNC_PREFIX: &[u16] = &[
        b'\\' as u16,
        b'\\' as u16,
        b'?' as u16,
        b'\\' as u16,
        b'U' as u16,
        b'N' as u16,
        b'C' as u16,
        b'\\' as u16,
    ];
    if path.starts_with(UNC_PREFIX) {
        // The updater state root must be local. Refuse UNC rather than
        // broadening the trust boundary to a remote filesystem.
        return None;
    }
    path.strip_prefix(DOS_PREFIX)
}

#[cfg(windows)]
fn windows_path_units_equal(left: &[u16], right: &[u16]) -> bool {
    left.len() == right.len()
        && left.iter().zip(right).all(|(&left, &right)| {
            let fold = |unit: u16| {
                if (b'A' as u16..=b'Z' as u16).contains(&unit) {
                    unit + (b'a' - b'A') as u16
                } else {
                    unit
                }
            };
            fold(left) == fold(right)
        })
}

#[cfg(windows)]
fn file_identity(file: &File) -> Result<WindowsFileIdentity, &'static str> {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: the File owns a valid handle and `information` is a live output
    // buffer of the exact size required by GetFileInformationByHandle.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0 {
        return Err("desktop_updater_staged_identity_unavailable");
    }
    Ok(WindowsFileIdentity {
        volume_serial_number: information.dwVolumeSerialNumber,
        file_index: ((information.nFileIndexHigh as u64) << 32) | information.nFileIndexLow as u64,
    })
}

#[cfg(windows)]
fn reject_reparse_handle(file: &File, error: &'static str) -> Result<(), &'static str> {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_REPARSE_POINT,
    };

    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: the File owns a valid handle and `information` is a live output
    // buffer of the exact size required by GetFileInformationByHandle.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0
        || information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
    {
        return Err(error);
    }
    Ok(())
}

#[cfg(windows)]
fn reject_reparse_point(path: &Path, error: &'static str) -> Result<(), &'static str> {
    use std::os::windows::ffi::OsStrExt as _;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileAttributesW, FILE_ATTRIBUTE_REPARSE_POINT, INVALID_FILE_ATTRIBUTES,
    };

    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: `wide` is NUL terminated and remains alive through the call.
    let attributes = unsafe { GetFileAttributesW(wide.as_ptr()) };
    if attributes == INVALID_FILE_ATTRIBUTES || attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(error);
    }
    Ok(())
}

#[cfg(windows)]
fn reject_reparse_ancestors(path: &Path) -> Result<(), &'static str> {
    for ancestor in path.ancestors() {
        match ancestor.symlink_metadata() {
            Ok(metadata) => {
                if metadata.file_type().is_symlink() {
                    return Err("desktop_updater_staging_directory_unsafe");
                }
                reject_reparse_point(ancestor, "desktop_updater_staging_directory_unsafe")?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err("desktop_updater_staging_directory_unavailable"),
        }
    }
    Ok(())
}

#[cfg(windows)]
fn mark_delete_on_close(file: &Option<File>) -> bool {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        FileDispositionInfo, SetFileInformationByHandle, FILE_DISPOSITION_INFO,
    };

    if let Some(open) = file.as_ref() {
        let disposition = FILE_DISPOSITION_INFO { DeleteFile: true };
        // SAFETY: the handle was opened with DELETE access, and the fixed-size
        // structure remains live for the synchronous call.  Marking the open
        // object for deletion avoids a path-replacement cleanup race.
        return unsafe {
            SetFileInformationByHandle(
                open.as_raw_handle(),
                FileDispositionInfo,
                &disposition as *const FILE_DISPOSITION_INFO as *const _,
                std::mem::size_of::<FILE_DISPOSITION_INFO>() as u32,
            )
        } != 0;
    }
    false
}

#[cfg(windows)]
fn cleanup_open_stage(file: &mut Option<File>, _path: &Path, delete: bool) {
    if !delete {
        drop(file.take());
        return;
    }
    // A read-only verified stage intentionally lacks DELETE access. If the
    // exact handle cannot be marked, close it and leave the strict random name
    // for the next startup's guarded handle-based cleanup. Never fall back to
    // deleting a path after the identity-bound handle has closed.
    let _ = mark_delete_on_close(file);
    drop(file.take());
}

#[cfg(not(windows))]
fn cleanup_open_stage(file: &mut Option<File>, path: &Path, delete: bool) {
    drop(file.take());
    if delete {
        let _ = std::fs::remove_file(path);
    }
}

#[cfg(windows)]
fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_dialog_has_cancel_as_the_default_and_only_ok_confirms() {
        assert_eq!(NATIVE_CONFIRMATION_STYLE & 0x0f, MESSAGE_BOX_OK_CANCEL);
        assert_ne!(NATIVE_CONFIRMATION_STYLE & MESSAGE_BOX_DEFAULT_BUTTON_2, 0);
        assert_eq!(
            classify_native_dialog_result(DIALOG_RESULT_OK),
            NativeDialogDecision::Confirm
        );
        for result in [2, 3, 6, 7, -1] {
            assert_eq!(
                classify_native_dialog_result(result),
                NativeDialogDecision::Cancel
            );
        }
        assert_eq!(
            classify_native_dialog_result(DIALOG_RESULT_FAILURE),
            NativeDialogDecision::Failed
        );
    }

    #[test]
    fn confirmation_token_is_minted_only_for_the_native_affirmative_result() {
        assert!(consent_from_native_result(DIALOG_RESULT_OK).is_ok());
        assert_eq!(
            consent_from_native_result(2).unwrap_err(),
            "desktop_updater_confirmation_cancelled"
        );
        assert_eq!(
            consent_from_native_result(DIALOG_RESULT_FAILURE).unwrap_err(),
            "desktop_updater_native_confirmation_failed"
        );
    }

    #[test]
    fn winverifytrust_requires_both_verify_and_close_success() {
        assert_eq!(evaluate_winverifytrust_status(0, 0), Ok(()));
        assert_eq!(
            evaluate_winverifytrust_status(-1, 0),
            Err("desktop_updater_authenticode_invalid")
        );
        assert_eq!(
            evaluate_winverifytrust_status(0, -1),
            Err("desktop_updater_winverifytrust_close_failed")
        );
        assert_eq!(
            evaluate_winverifytrust_status(-1, -1),
            Err("desktop_updater_winverifytrust_close_failed")
        );
    }

    #[test]
    fn expected_digest_is_exact_lowercase_sha256() {
        assert!(validate_expected_sha256(&"a".repeat(64)).is_ok());
        for value in ["a".repeat(63), "A".repeat(64), "g".repeat(64)] {
            assert_eq!(
                validate_expected_sha256(&value),
                Err("desktop_updater_sha256_invalid")
            );
        }
    }

    #[test]
    fn staging_names_are_fixed_executable_names_with_full_nonce_entropy() {
        assert_eq!(
            staging_name([0xab; 16]),
            "stock-desk-update-abababababababababababababababab.exe"
        );
    }

    #[test]
    fn cleanup_name_filter_accepts_only_exact_generated_names() {
        use std::ffi::OsStr;

        assert!(is_strict_staging_name(OsStr::new(
            "stock-desk-update-0123456789abcdef0123456789abcdef.exe"
        )));
        for value in [
            "stock-desk-update-0123456789abcdef0123456789abcde.exe",
            "stock-desk-update-0123456789abcdef0123456789abcdef.EXE",
            "stock-desk-update-0123456789ABCDEF0123456789ABCDEF.exe",
            "stock-desk-update-0123456789abcdef0123456789abcdef.exe.bak",
            "other-0123456789abcdef0123456789abcdef.exe",
        ] {
            assert!(!is_strict_staging_name(OsStr::new(value)), "{value}");
        }
    }

    #[test]
    fn launched_stage_is_the_only_state_that_disarms_drop_cleanup() {
        let mut lifecycle = StageLifecycle::Pending;
        assert!(lifecycle.delete_on_drop());
        lifecycle.mark_launched();
        assert_eq!(lifecycle, StageLifecycle::Launched);
        assert!(!lifecycle.delete_on_drop());
    }

    #[cfg(not(windows))]
    #[test]
    fn pending_drop_deletes_but_launched_drop_preserves_the_stage() {
        fn staged(path: PathBuf, lifecycle: StageLifecycle) -> SecureStagedInstaller {
            let file = File::create(&path).unwrap();
            SecureStagedInstaller {
                path,
                file: Some(file),
                sha256: "a".repeat(64),
                authenticode_verified: false,
                lifecycle,
            }
        }

        let root = std::env::temp_dir().join(format!(
            "stock-desk-stage-lifecycle-{}-{}",
            std::process::id(),
            hex_digest(random_nonce().unwrap())
        ));
        std::fs::create_dir_all(&root).unwrap();
        let pending = root.join("pending.exe");
        drop(staged(pending.clone(), StageLifecycle::Pending));
        assert!(!pending.exists());

        let launched = root.join("launched.exe");
        drop(staged(launched.clone(), StageLifecycle::Launched));
        assert!(launched.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn staged_file_reopen_retries_only_transient_windows_locks() {
        assert!(is_transient_stage_reopen_error(
            &std::io::Error::from_raw_os_error(32)
        ));
        assert!(is_transient_stage_reopen_error(
            &std::io::Error::from_raw_os_error(33)
        ));
        for code in [2, 5, 87] {
            assert!(!is_transient_stage_reopen_error(
                &std::io::Error::from_raw_os_error(code)
            ));
        }
    }

    #[cfg(windows)]
    #[test]
    fn directory_handle_is_bound_to_the_expected_lexical_path() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-directory-binding-{}-{}",
            std::process::id(),
            hex_digest(random_nonce().unwrap())
        ));
        let expected = root.join("expected");
        let other = root.join("other");
        std::fs::create_dir_all(&expected).unwrap();
        std::fs::create_dir_all(&other).unwrap();
        let guard = open_directory_guard(&expected).unwrap();

        assert!(bind_handle_to_lexical_path(
            &guard._file,
            &expected,
            "desktop_updater_staging_directory_unsafe"
        )
        .is_ok());
        assert_eq!(
            bind_handle_to_lexical_path(
                &guard._file,
                &other,
                "desktop_updater_staging_directory_unsafe"
            ),
            Err("desktop_updater_staging_directory_unsafe")
        );

        drop(guard);
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn staged_file_reopens_as_read_only_and_directory_is_guarded() {
        use std::fs::OpenOptions;

        let root = std::env::temp_dir().join(format!(
            "stock-desk-secure-stage-{}-{}",
            std::process::id(),
            hex_digest(random_nonce().unwrap())
        ));
        let staging = root.join("staging");
        let payload = b"trusted updater test payload";
        let digest = hex_digest(Sha256::digest(payload));
        let staged = stage_installer(payload, &staging, &digest).unwrap();
        let path = staged.path.clone();

        let mut readback = staged.file.as_ref().unwrap().try_clone().unwrap();
        assert_eq!(hash_open_file(&mut readback).unwrap(), digest);
        assert!(OpenOptions::new().write(true).open(&path).is_err());
        assert!(std::fs::remove_file(&path).is_err());
        assert!(std::fs::rename(&staging, root.join("moved")).is_err());

        drop(readback);
        drop(staged);
        assert!(path.exists());
        cleanup_staging_directory(&staging);
        assert!(!path.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn stale_cleanup_removes_only_strict_non_reparse_files() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-stale-stage-{}-{}",
            std::process::id(),
            hex_digest(random_nonce().unwrap())
        ));
        std::fs::create_dir_all(&root).unwrap();
        let valid = root.join("stock-desk-update-0123456789abcdef0123456789abcdef.exe");
        let invalid = root.join("stock-desk-update-not-a-generated-name.exe");
        std::fs::write(&valid, b"stale").unwrap();
        std::fs::write(&invalid, b"keep").unwrap();

        cleanup_staging_directory(&root);
        assert!(!valid.exists());
        assert!(invalid.exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(not(windows))]
    #[test]
    fn non_windows_security_interfaces_fail_before_touching_paths() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-updater-windows-boundary-{}",
            std::process::id()
        ));
        let staging = root.join("must-not-be-created");
        assert_eq!(
            stage_installer(b"payload", &staging, &"a".repeat(64)).unwrap_err(),
            "desktop_updater_secure_staging_unavailable"
        );
        assert!(!root.exists());
    }
}
