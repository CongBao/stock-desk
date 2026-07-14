use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use semver::Version;
use serde::{de::DeserializeOwned, Deserialize, Serialize};

const JOURNAL_SCHEMA: &str = "stock-desk-updater-journal-v1";
const MAX_JOURNAL_BYTES: u64 = 4 * 1024;
const TEMP_ATTEMPTS: usize = 8;

pub(crate) const PENDING_INSTALL_FILE: &str = "pending-install.json";
pub(crate) const INSTALLED_WATERMARK_FILE: &str = "installed-watermark.json";
pub(crate) const FAILED_INSTALL_FILE: &str = "failed-install.json";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PendingInstall {
    schema_version: String,
    pub(crate) from_version: String,
    pub(crate) from_source_sha: String,
    pub(crate) target_version: String,
    pub(crate) target_source_sha: String,
    pub(crate) sha256: String,
}

impl PendingInstall {
    pub(crate) fn new(
        from_version: impl Into<String>,
        from_source_sha: impl Into<String>,
        target_version: impl Into<String>,
        target_source_sha: impl Into<String>,
        sha256: impl Into<String>,
    ) -> Result<Self, &'static str> {
        let pending = Self {
            schema_version: JOURNAL_SCHEMA.to_owned(),
            from_version: from_version.into(),
            from_source_sha: from_source_sha.into(),
            target_version: target_version.into(),
            target_source_sha: target_source_sha.into(),
            sha256: sha256.into(),
        };
        validate_pending(&pending)?;
        Ok(pending)
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct InstalledWatermark {
    schema_version: String,
    pub(crate) version: String,
    pub(crate) source_sha: String,
    pub(crate) sha256: String,
}

impl InstalledWatermark {
    #[cfg(test)]
    pub(crate) fn new(
        version: impl Into<String>,
        source_sha: impl Into<String>,
        sha256: impl Into<String>,
    ) -> Result<Self, &'static str> {
        let watermark = Self {
            schema_version: JOURNAL_SCHEMA.to_owned(),
            version: version.into(),
            source_sha: source_sha.into(),
            sha256: sha256.into(),
        };
        validate_installed(&watermark)?;
        Ok(watermark)
    }

    fn from_pending(pending: &PendingInstall) -> Self {
        Self {
            schema_version: JOURNAL_SCHEMA.to_owned(),
            version: pending.target_version.clone(),
            source_sha: pending.target_source_sha.clone(),
            sha256: pending.sha256.clone(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum StartupReconcile {
    NoPending,
    CommitInstalled { watermark: InstalledWatermark },
    PreviousInstallFailed { target_version: String },
}

/// Decide what startup may do with a durable pending-install record.
///
/// The function is deliberately pure. Callers persist `CommitInstalled` before
/// removing the pending record. Both terminal pending decisions allow the
/// caller to remove the pending record only after it has durably recorded the
/// resulting state or failure.
pub(crate) fn reconcile_startup(
    current_version: &str,
    current_source_sha: &str,
    installed: Option<&InstalledWatermark>,
    pending: Option<&PendingInstall>,
) -> Result<StartupReconcile, &'static str> {
    parse_current_version(current_version)?;
    validate_source_sha(current_source_sha, "desktop_updater_current_source_invalid")?;
    if let Some(installed) = installed {
        validate_installed(installed)?;
    }
    let Some(pending) = pending else {
        return Ok(StartupReconcile::NoPending);
    };
    validate_pending(pending)?;

    let target = parse_stable_version(&pending.target_version)?;
    if let Some(installed) = installed {
        let installed_version = parse_stable_version(&installed.version)
            .map_err(|_| "desktop_updater_watermark_invalid")?;
        if installed_version > target {
            return Err("desktop_updater_pending_replay_rejected");
        }
        if installed_version == target
            && (installed.source_sha != pending.target_source_sha
                || installed.sha256 != pending.sha256)
        {
            return Err("desktop_updater_pending_identity_mismatch");
        }
    }

    if current_version == pending.target_version {
        if current_source_sha != pending.target_source_sha {
            return Err("desktop_updater_pending_identity_mismatch");
        }
        return Ok(StartupReconcile::CommitInstalled {
            watermark: InstalledWatermark::from_pending(pending),
        });
    }

    if current_version == pending.from_version {
        if current_source_sha != pending.from_source_sha {
            return Err("desktop_updater_pending_identity_mismatch");
        }
        return Ok(StartupReconcile::PreviousInstallFailed {
            target_version: pending.target_version.clone(),
        });
    }

    Err("desktop_updater_pending_identity_mismatch")
}

pub(crate) fn load_pending_install(path: &Path) -> Result<Option<PendingInstall>, &'static str> {
    let pending = read_json(path, "desktop_updater_pending")?;
    if let Some(pending) = pending.as_ref() {
        validate_pending(pending)?;
    }
    Ok(pending)
}

pub(crate) fn load_installed_watermark(
    path: &Path,
) -> Result<Option<InstalledWatermark>, &'static str> {
    let watermark = read_json(path, "desktop_updater_watermark")?;
    if let Some(watermark) = watermark.as_ref() {
        validate_installed(watermark)?;
    }
    Ok(watermark)
}

pub(crate) fn load_failed_install(path: &Path) -> Result<Option<PendingInstall>, &'static str> {
    let pending = read_json(path, "desktop_updater_failed")?;
    if let Some(pending) = pending.as_ref() {
        validate_pending(pending).map_err(|_| "desktop_updater_failed_invalid")?;
    }
    Ok(pending)
}

pub(crate) fn persist_pending_install(
    path: &Path,
    pending: &PendingInstall,
) -> Result<(), &'static str> {
    validate_pending(pending)?;
    write_json_atomically(path, pending, "desktop_updater_pending")
}

pub(crate) fn persist_installed_watermark(
    path: &Path,
    watermark: &InstalledWatermark,
) -> Result<(), &'static str> {
    validate_installed(watermark)?;
    write_json_atomically(path, watermark, "desktop_updater_watermark")
}

pub(crate) fn persist_failed_install(
    path: &Path,
    pending: &PendingInstall,
) -> Result<(), &'static str> {
    validate_pending(pending).map_err(|_| "desktop_updater_failed_invalid")?;
    write_json_atomically(path, pending, "desktop_updater_failed")
}

pub(crate) fn remove_pending_install(path: &Path) -> Result<(), &'static str> {
    remove_journal_file(path, "desktop_updater_pending")
}

pub(crate) fn remove_failed_install(path: &Path) -> Result<(), &'static str> {
    remove_journal_file(path, "desktop_updater_failed")
}

#[cfg(not(windows))]
fn remove_journal_file(path: &Path, code_prefix: &'static str) -> Result<(), &'static str> {
    match fs::remove_file(path) {
        Ok(()) => sync_parent(path).map_err(|_| write_error(code_prefix)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(write_error(code_prefix)),
    }
}

#[cfg(windows)]
fn remove_journal_file(path: &Path, code_prefix: &'static str) -> Result<(), &'static str> {
    remove_journal_file_windows(path, code_prefix)
}

fn validate_pending(pending: &PendingInstall) -> Result<(), &'static str> {
    if pending.schema_version != JOURNAL_SCHEMA {
        return Err("desktop_updater_pending_invalid");
    }
    let from = parse_current_version(&pending.from_version)
        .map_err(|_| "desktop_updater_pending_invalid")?;
    let target = parse_stable_version(&pending.target_version)
        .map_err(|_| "desktop_updater_pending_invalid")?;
    if target <= from {
        return Err("desktop_updater_pending_invalid");
    }
    validate_source_sha(&pending.from_source_sha, "desktop_updater_pending_invalid")?;
    validate_source_sha(
        &pending.target_source_sha,
        "desktop_updater_pending_invalid",
    )?;
    if !is_lower_hex(&pending.sha256, 64) {
        return Err("desktop_updater_pending_invalid");
    }
    Ok(())
}

fn validate_installed(watermark: &InstalledWatermark) -> Result<(), &'static str> {
    if watermark.schema_version != JOURNAL_SCHEMA
        || parse_stable_version(&watermark.version).is_err()
        || !is_lower_hex(&watermark.source_sha, 40)
        || !is_lower_hex(&watermark.sha256, 64)
    {
        return Err("desktop_updater_watermark_invalid");
    }
    Ok(())
}

fn parse_stable_version(value: &str) -> Result<Version, &'static str> {
    let parsed = Version::parse(value).map_err(|_| "desktop_updater_version_invalid")?;
    if !parsed.pre.is_empty()
        || !parsed.build.is_empty()
        || parsed.to_string() != value
        || value.split('.').count() != 3
    {
        return Err("desktop_updater_version_invalid");
    }
    Ok(parsed)
}

fn parse_current_version(value: &str) -> Result<Version, &'static str> {
    let parsed = Version::parse(value).map_err(|_| "desktop_updater_version_invalid")?;
    if !parsed.build.is_empty() || parsed.to_string() != value {
        return Err("desktop_updater_version_invalid");
    }
    Ok(parsed)
}

fn validate_source_sha(value: &str, code: &'static str) -> Result<(), &'static str> {
    is_lower_hex(value, 40).then_some(()).ok_or(code)
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn read_json<T: DeserializeOwned>(
    path: &Path,
    code_prefix: &'static str,
) -> Result<Option<T>, &'static str> {
    #[cfg(windows)]
    let mut file = match open_journal_for_read_windows(path, code_prefix)? {
        Some(file) => file,
        None => return Ok(None),
    };

    #[cfg(not(windows))]
    let mut file = {
        reject_unsafe_ancestors(path, code_prefix)?;
        let link_metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(read_error(code_prefix)),
        };
        if !link_metadata.file_type().is_file() || link_metadata.file_type().is_symlink() {
            return Err(invalid_error(code_prefix));
        }

        File::open(path).map_err(|_| read_error(code_prefix))?
    };
    if file.metadata().map_err(|_| read_error(code_prefix))?.len() > MAX_JOURNAL_BYTES {
        return Err(too_large_error(code_prefix));
    }
    let mut payload = Vec::new();
    Read::by_ref(&mut file)
        .take(MAX_JOURNAL_BYTES + 1)
        .read_to_end(&mut payload)
        .map_err(|_| read_error(code_prefix))?;
    if payload.len() as u64 > MAX_JOURNAL_BYTES {
        return Err(too_large_error(code_prefix));
    }

    let mut deserializer = serde_json::Deserializer::from_slice(&payload);
    let value = T::deserialize(&mut deserializer).map_err(|_| invalid_error(code_prefix))?;
    deserializer.end().map_err(|_| invalid_error(code_prefix))?;
    Ok(Some(value))
}

fn write_json_atomically<T: Serialize>(
    path: &Path,
    value: &T,
    code_prefix: &'static str,
) -> Result<(), &'static str> {
    let payload = serde_json::to_vec(value).map_err(|_| write_error(code_prefix))?;
    if payload.len() as u64 > MAX_JOURNAL_BYTES {
        return Err(write_error(code_prefix));
    }

    #[cfg(windows)]
    return write_json_atomically_windows(path, &payload, code_prefix);

    #[cfg(not(windows))]
    {
        let parent = path.parent().ok_or(write_error(code_prefix))?;
        reject_unsafe_ancestors(path, code_prefix)?;
        fs::create_dir_all(parent).map_err(|_| write_error(code_prefix))?;
        reject_unsafe_ancestors(path, code_prefix)?;

        let (temporary, mut file) = create_temporary(path, code_prefix)?;
        let write_result = file
            .write_all(&payload)
            .and_then(|()| file.sync_all())
            .map_err(|_| write_error(code_prefix));
        drop(file);
        if let Err(error) = write_result {
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
        if replace_file(&temporary, path).is_err() {
            let _ = fs::remove_file(&temporary);
            return Err(write_error(code_prefix));
        }
        Ok(())
    }
}

fn reject_unsafe_ancestors(path: &Path, code_prefix: &'static str) -> Result<(), &'static str> {
    let parent = path.parent().ok_or(invalid_error(code_prefix))?;

    #[cfg(windows)]
    for ancestor in parent.ancestors() {
        reject_unsafe_ancestor(ancestor, code_prefix)?;
    }

    // The updater is Windows-only. On Unix test hosts, checking the immediate
    // application-controlled parent preserves the link-attack regression test
    // without rejecting macOS' trusted `/var -> /private/var` system prefix.
    #[cfg(not(windows))]
    reject_unsafe_ancestor(parent, code_prefix)?;

    Ok(())
}

fn reject_unsafe_ancestor(ancestor: &Path, code_prefix: &'static str) -> Result<(), &'static str> {
    match fs::symlink_metadata(ancestor) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(invalid_error(code_prefix));
            }
            #[cfg(windows)]
            reject_windows_reparse_point(ancestor, code_prefix)?;
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(read_error(code_prefix)),
    }
}

#[cfg(windows)]
fn reject_windows_reparse_point(
    path: &Path,
    code_prefix: &'static str,
) -> Result<(), &'static str> {
    use std::os::windows::ffi::OsStrExt as _;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileAttributesW, FILE_ATTRIBUTE_REPARSE_POINT, INVALID_FILE_ATTRIBUTES,
    };

    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: `wide` is NUL terminated and remains live for the call.
    let attributes = unsafe { GetFileAttributesW(wide.as_ptr()) };
    if attributes == INVALID_FILE_ATTRIBUTES || attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(invalid_error(code_prefix));
    }
    Ok(())
}

#[cfg(windows)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct WindowsObjectIdentity {
    volume_serial_number: u32,
    file_index: u64,
}

#[cfg(windows)]
struct WindowsDirectoryGuard {
    path: PathBuf,
    file: File,
    identity: WindowsObjectIdentity,
}

#[cfg(windows)]
fn wide_path(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt as _;

    path.as_os_str().encode_wide().chain(Some(0)).collect()
}

#[cfg(windows)]
fn windows_object_information(
    file: &File,
    code_prefix: &'static str,
) -> Result<windows_sys::Win32::Storage::FileSystem::BY_HANDLE_FILE_INFORMATION, &'static str> {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    // SAFETY: `file` owns a valid handle and `information` is a correctly sized
    // writable output buffer for the duration of the call.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0 {
        return Err(read_error(code_prefix));
    }
    Ok(information)
}

#[cfg(windows)]
fn windows_object_identity(
    file: &File,
    code_prefix: &'static str,
) -> Result<WindowsObjectIdentity, &'static str> {
    let information = windows_object_information(file, code_prefix)?;
    Ok(WindowsObjectIdentity {
        volume_serial_number: information.dwVolumeSerialNumber,
        file_index: ((information.nFileIndexHigh as u64) << 32) | information.nFileIndexLow as u64,
    })
}

#[cfg(windows)]
fn reject_windows_object_kind(
    file: &File,
    expect_directory: bool,
    code_prefix: &'static str,
) -> Result<(), &'static str> {
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
    };

    let attributes = windows_object_information(file, code_prefix)?.dwFileAttributes;
    let is_directory = attributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 || is_directory != expect_directory {
        return Err(invalid_error(code_prefix));
    }
    Ok(())
}

#[cfg(windows)]
fn open_windows_directory(path: &Path, _code_prefix: &'static str) -> Result<File, std::io::Error> {
    use std::os::windows::io::FromRawHandle as _;
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_READ_ATTRIBUTES, FILE_SHARE_READ, FILE_TRAVERSE, OPEN_EXISTING,
    };

    let wide = wide_path(path);
    // SAFETY: `wide` is NUL terminated and remains live for the synchronous
    // call. The returned handle is transferred exactly once into `File`.
    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            FILE_TRAVERSE | FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: ownership of the valid CreateFileW handle transfers to File.
    let file = unsafe { File::from_raw_handle(handle) };
    bind_windows_directory_to_lexical_path(&file, path)?;
    Ok(file)
}

#[cfg(windows)]
fn bind_windows_directory_to_lexical_path(file: &File, expected: &Path) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED, VOLUME_NAME_DOS,
    };

    let expected = long_windows_lexical_path(expected)?;
    // SAFETY: a null/zero-capacity probe returns the required UTF-16 size for
    // this live directory handle.
    let required = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            std::ptr::null_mut(),
            0,
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
        )
    };
    if required == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let mut resolved = vec![0_u16; required as usize + 1];
    // SAFETY: `resolved` has the probed capacity and remains writable for the
    // synchronous query over the same open handle.
    let written = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            resolved.as_mut_ptr(),
            resolved.len() as u32,
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
        )
    };
    if written == 0 || written as usize >= resolved.len() {
        return Err(std::io::Error::last_os_error());
    }
    resolved.truncate(written as usize);
    let resolved = strip_windows_dos_prefix(&resolved).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "journal directory must resolve to a local DOS path",
        )
    })?;
    if !windows_path_units_equal(&expected, resolved) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "journal directory identity does not match its lexical path",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn long_windows_lexical_path(path: &Path) -> std::io::Result<Vec<u16>> {
    use std::os::windows::ffi::OsStrExt as _;
    use windows_sys::Win32::Storage::FileSystem::GetLongPathNameW;

    let absolute = std::path::absolute(path)?;
    let input: Vec<u16> = absolute.as_os_str().encode_wide().chain(Some(0)).collect();
    // Windows runners can expose the temp root through an 8.3 alias such as
    // RUNNER~1 while GetFinalPathNameByHandleW returns the long spelling. Expand
    // only that lexical spelling before comparing it with the already-open,
    // non-reparse directory handle.
    let required = unsafe { GetLongPathNameW(input.as_ptr(), std::ptr::null_mut(), 0) };
    if required == 0 {
        return Err(std::io::Error::last_os_error());
    }
    let mut expanded = vec![0_u16; required as usize];
    // SAFETY: `input` is NUL terminated and `expanded` has the capacity probed
    // by the preceding call. Both buffers remain live for this call.
    let written =
        unsafe { GetLongPathNameW(input.as_ptr(), expanded.as_mut_ptr(), expanded.len() as u32) };
    if written == 0 || written as usize >= expanded.len() {
        return Err(std::io::Error::last_os_error());
    }
    expanded.truncate(written as usize);
    Ok(expanded)
}

#[cfg(windows)]
fn strip_windows_dos_prefix(path: &[u16]) -> Option<&[u16]> {
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
fn open_windows_parent_guard(
    path: &Path,
    create: bool,
    code_prefix: &'static str,
) -> Result<Option<WindowsDirectoryGuard>, &'static str> {
    let parent = path.parent().ok_or(invalid_error(code_prefix))?;
    reject_unsafe_ancestors(path, code_prefix)?;
    if create {
        fs::create_dir_all(parent).map_err(|_| write_error(code_prefix))?;
        reject_unsafe_ancestors(path, code_prefix)?;
    }

    let file = match open_windows_directory(parent, code_prefix) {
        Ok(file) => file,
        Err(error) if !create && error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => {
            return Err(if create {
                write_error(code_prefix)
            } else {
                read_error(code_prefix)
            });
        }
    };
    reject_windows_object_kind(&file, true, code_prefix)?;
    let identity = windows_object_identity(&file, code_prefix)?;
    let guard = WindowsDirectoryGuard {
        path: parent.to_path_buf(),
        file,
        identity,
    };
    revalidate_windows_parent_guard(&guard, code_prefix)?;
    Ok(Some(guard))
}

#[cfg(windows)]
fn revalidate_windows_parent_guard(
    guard: &WindowsDirectoryGuard,
    code_prefix: &'static str,
) -> Result<(), &'static str> {
    let current =
        open_windows_directory(&guard.path, code_prefix).map_err(|_| invalid_error(code_prefix))?;
    reject_windows_object_kind(&current, true, code_prefix)?;
    if windows_object_identity(&current, code_prefix)? != guard.identity {
        return Err(invalid_error(code_prefix));
    }
    Ok(())
}

#[cfg(windows)]
fn open_windows_file(
    path: &Path,
    access: u32,
    share: u32,
    create_new: bool,
) -> std::io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt as _;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_FLAG_SEQUENTIAL_SCAN, FILE_FLAG_WRITE_THROUGH,
    };

    let mut options = OpenOptions::new();
    options
        .read(access & windows_sys::Win32::Foundation::GENERIC_READ != 0)
        .write(access & windows_sys::Win32::Foundation::GENERIC_WRITE != 0)
        .access_mode(access)
        .share_mode(share)
        .custom_flags(
            FILE_FLAG_OPEN_REPARSE_POINT
                | FILE_FLAG_SEQUENTIAL_SCAN
                | if create_new {
                    FILE_FLAG_WRITE_THROUGH
                } else {
                    0
                },
        );
    if create_new {
        options.create_new(true);
    }
    options.open(path)
}

#[cfg(windows)]
fn open_journal_for_read_windows(
    path: &Path,
    code_prefix: &'static str,
) -> Result<Option<File>, &'static str> {
    use windows_sys::Win32::Foundation::GENERIC_READ;
    use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ;

    let Some(parent) = open_windows_parent_guard(path, false, code_prefix)? else {
        return Ok(None);
    };
    let file = match open_windows_file(path, GENERIC_READ, FILE_SHARE_READ, false) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            revalidate_windows_parent_guard(&parent, code_prefix)?;
            return Ok(None);
        }
        Err(_) => return Err(read_error(code_prefix)),
    };
    reject_windows_object_kind(&file, false, code_prefix)?;
    let identity = windows_object_identity(&file, code_prefix)?;
    revalidate_windows_parent_guard(&parent, code_prefix)?;

    // Reopen while the first read-only/no-write/no-delete-share handle is live.
    // This proves that the fixed path still resolves to the exact object that
    // will be parsed, rather than only to a path checked before opening.
    let current = open_windows_file(path, GENERIC_READ, FILE_SHARE_READ, false)
        .map_err(|_| read_error(code_prefix))?;
    reject_windows_object_kind(&current, false, code_prefix)?;
    if windows_object_identity(&current, code_prefix)? != identity {
        return Err(invalid_error(code_prefix));
    }
    Ok(Some(file))
}

#[cfg(windows)]
fn create_temporary_windows(
    path: &Path,
    parent_guard: &WindowsDirectoryGuard,
    code_prefix: &'static str,
) -> Result<(PathBuf, File, WindowsObjectIdentity), &'static str> {
    use windows_sys::Win32::Foundation::{GENERIC_READ, GENERIC_WRITE};
    use windows_sys::Win32::Storage::FileSystem::{DELETE, FILE_SHARE_READ};

    let parent = path.parent().ok_or(write_error(code_prefix))?;
    let file_name = path.file_name().ok_or(write_error(code_prefix))?;
    for _ in 0..TEMP_ATTEMPTS {
        let mut random = [0_u8; 16];
        getrandom::fill(&mut random).map_err(|_| write_error(code_prefix))?;
        let mut suffix = String::with_capacity(random.len() * 2);
        for byte in random {
            use std::fmt::Write as _;
            write!(&mut suffix, "{byte:02x}").expect("writing to String cannot fail");
        }
        let mut temporary_name = OsString::from(".");
        temporary_name.push(file_name);
        temporary_name.push(".");
        temporary_name.push(suffix);
        temporary_name.push(".tmp");
        let temporary = parent.join(temporary_name);
        match open_windows_file(
            &temporary,
            GENERIC_READ | GENERIC_WRITE | DELETE,
            FILE_SHARE_READ,
            true,
        ) {
            Ok(file) => {
                if reject_windows_object_kind(&file, false, code_prefix).is_err()
                    || revalidate_windows_parent_guard(parent_guard, code_prefix).is_err()
                {
                    discard_windows_temporary(file);
                    return Err(write_error(code_prefix));
                }
                let identity = match windows_object_identity(&file, code_prefix) {
                    Ok(identity) => identity,
                    Err(_) => {
                        discard_windows_temporary(file);
                        return Err(write_error(code_prefix));
                    }
                };
                return Ok((temporary, file, identity));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(write_error(code_prefix)),
        }
    }
    Err(write_error(code_prefix))
}

#[cfg(windows)]
fn reject_existing_destination_reparse(
    path: &Path,
    parent_guard: &WindowsDirectoryGuard,
    code_prefix: &'static str,
) -> Result<(), &'static str> {
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };

    match open_windows_file(
        path,
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        false,
    ) {
        Ok(file) => reject_windows_object_kind(&file, false, code_prefix)?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(write_error(code_prefix)),
    }
    revalidate_windows_parent_guard(parent_guard, code_prefix)
}

#[cfg(windows)]
fn rename_windows_file_by_handle(
    file: &File,
    destination_name: &std::ffi::OsStr,
    parent_guard: &WindowsDirectoryGuard,
) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt as _;
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        FileRenameInfo, SetFileInformationByHandle, FILE_RENAME_INFO,
    };

    let destination: Vec<u16> = destination_name.encode_wide().collect();
    // Windows requires at least the complete fixed FILE_RENAME_INFO structure
    // plus FileNameLength bytes, not merely the offset of the trailing field.
    let byte_length =
        std::mem::size_of::<FILE_RENAME_INFO>() + destination.len() * std::mem::size_of::<u16>();
    let word_length = byte_length.div_ceil(std::mem::size_of::<usize>());
    let mut buffer = vec![0_usize; word_length];
    let information = buffer.as_mut_ptr().cast::<FILE_RENAME_INFO>();
    // SAFETY: `buffer` is usize-aligned and large enough for the fixed header
    // plus every UTF-16 code unit copied into the trailing FileName storage.
    unsafe {
        (*information).Anonymous.ReplaceIfExists = true;
        (*information).RootDirectory = parent_guard.file.as_raw_handle();
        (*information).FileNameLength = (destination.len() * std::mem::size_of::<u16>()) as u32;
        std::ptr::copy_nonoverlapping(
            destination.as_ptr(),
            (*information).FileName.as_mut_ptr(),
            destination.len(),
        );
    }
    // SAFETY: the source handle owns DELETE access, RootDirectory remains live,
    // and `buffer` contains a valid FILE_RENAME_INFO for the synchronous call.
    if unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileRenameInfo,
            information.cast(),
            byte_length as u32,
        )
    } == 0
    {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(windows)]
fn mark_windows_file_delete_on_close(file: &File) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{
        FileDispositionInfo, SetFileInformationByHandle, FILE_DISPOSITION_INFO,
    };

    let disposition = FILE_DISPOSITION_INFO { DeleteFile: true };
    // SAFETY: callers open the exact handle with DELETE access and keep the
    // fixed-size disposition structure alive for the synchronous call.
    if unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileDispositionInfo,
            (&raw const disposition).cast(),
            std::mem::size_of::<FILE_DISPOSITION_INFO>() as u32,
        )
    } == 0
    {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(windows)]
fn discard_windows_temporary(file: File) {
    let _ = mark_windows_file_delete_on_close(&file);
    drop(file);
}

#[cfg(windows)]
fn write_json_atomically_windows(
    path: &Path,
    payload: &[u8],
    code_prefix: &'static str,
) -> Result<(), &'static str> {
    let parent_guard =
        open_windows_parent_guard(path, true, code_prefix)?.ok_or(write_error(code_prefix))?;
    let destination_name = path.file_name().ok_or(write_error(code_prefix))?;
    reject_existing_destination_reparse(path, &parent_guard, code_prefix)?;
    let (_temporary, mut file, identity) =
        create_temporary_windows(path, &parent_guard, code_prefix)?;

    if file
        .write_all(payload)
        .and_then(|()| file.sync_all())
        .is_err()
    {
        discard_windows_temporary(file);
        return Err(write_error(code_prefix));
    }
    if windows_object_identity(&file, code_prefix).ok() != Some(identity)
        || revalidate_windows_parent_guard(&parent_guard, code_prefix).is_err()
    {
        discard_windows_temporary(file);
        return Err(write_error(code_prefix));
    }
    if rename_windows_file_by_handle(&file, destination_name, &parent_guard).is_err() {
        discard_windows_temporary(file);
        return Err(write_error(code_prefix));
    }
    if file.sync_all().is_err()
        || windows_object_identity(&file, code_prefix).ok() != Some(identity)
        || revalidate_windows_parent_guard(&parent_guard, code_prefix).is_err()
    {
        // The exact file is already published. Fail closed and leave the valid
        // journal in place; deleting by path here would reintroduce the race.
        return Err(write_error(code_prefix));
    }
    Ok(())
}

#[cfg(windows)]
fn remove_journal_file_windows(path: &Path, code_prefix: &'static str) -> Result<(), &'static str> {
    use windows_sys::Win32::Foundation::GENERIC_READ;
    use windows_sys::Win32::Storage::FileSystem::{DELETE, FILE_SHARE_READ};

    let Some(parent_guard) = open_windows_parent_guard(path, false, code_prefix)? else {
        return Ok(());
    };
    let file = match open_windows_file(path, GENERIC_READ | DELETE, FILE_SHARE_READ, false) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            revalidate_windows_parent_guard(&parent_guard, code_prefix)?;
            return Ok(());
        }
        Err(_) => return Err(write_error(code_prefix)),
    };
    reject_windows_object_kind(&file, false, code_prefix)?;
    let identity = windows_object_identity(&file, code_prefix)?;
    revalidate_windows_parent_guard(&parent_guard, code_prefix)?;
    if windows_object_identity(&file, code_prefix)? != identity {
        return Err(invalid_error(code_prefix));
    }
    mark_windows_file_delete_on_close(&file).map_err(|_| write_error(code_prefix))?;
    drop(file);
    revalidate_windows_parent_guard(&parent_guard, code_prefix)
        .map_err(|_| write_error(code_prefix))
}

#[cfg(not(windows))]
fn create_temporary(
    path: &Path,
    code_prefix: &'static str,
) -> Result<(PathBuf, File), &'static str> {
    let parent = path.parent().ok_or(write_error(code_prefix))?;
    let file_name = path.file_name().ok_or(write_error(code_prefix))?;
    for _ in 0..TEMP_ATTEMPTS {
        let mut random = [0_u8; 16];
        getrandom::fill(&mut random).map_err(|_| write_error(code_prefix))?;
        let mut suffix = String::with_capacity(random.len() * 2);
        for byte in random {
            use std::fmt::Write as _;
            write!(&mut suffix, "{byte:02x}").expect("writing to String cannot fail");
        }
        let mut temporary_name = OsString::from(".");
        temporary_name.push(file_name);
        temporary_name.push(".");
        temporary_name.push(suffix);
        temporary_name.push(".tmp");
        let temporary = parent.join(temporary_name);
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        match options.open(&temporary) {
            Ok(file) => return Ok((temporary, file)),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(write_error(code_prefix)),
        }
    }
    Err(write_error(code_prefix))
}

#[cfg(not(windows))]
fn replace_file(temporary: &Path, destination: &Path) -> std::io::Result<()> {
    fs::rename(temporary, destination)?;
    sync_parent(destination)
}

#[cfg(not(windows))]
fn sync_parent(path: &Path) -> std::io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| std::io::Error::other("journal parent is unavailable"))?;
    File::open(parent)?.sync_all()
}

fn read_error(prefix: &'static str) -> &'static str {
    match prefix {
        "desktop_updater_pending" => "desktop_updater_pending_unreadable",
        "desktop_updater_failed" => "desktop_updater_failed_unreadable",
        _ => "desktop_updater_watermark_unreadable",
    }
}

fn invalid_error(prefix: &'static str) -> &'static str {
    match prefix {
        "desktop_updater_pending" => "desktop_updater_pending_invalid",
        "desktop_updater_failed" => "desktop_updater_failed_invalid",
        _ => "desktop_updater_watermark_invalid",
    }
}

fn too_large_error(prefix: &'static str) -> &'static str {
    match prefix {
        "desktop_updater_pending" => "desktop_updater_pending_too_large",
        "desktop_updater_failed" => "desktop_updater_failed_too_large",
        _ => "desktop_updater_watermark_too_large",
    }
}

fn write_error(prefix: &'static str) -> &'static str {
    match prefix {
        "desktop_updater_pending" => "desktop_updater_pending_unwritable",
        "desktop_updater_failed" => "desktop_updater_failed_unwritable",
        _ => "desktop_updater_watermark_unwritable",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sha(byte: char) -> String {
        byte.to_string().repeat(40)
    }

    fn digest(byte: char) -> String {
        byte.to_string().repeat(64)
    }

    fn pending() -> PendingInstall {
        PendingInstall::new("1.1.0", sha('a'), "1.2.0", sha('b'), digest('c')).unwrap()
    }

    fn temp_root(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "stock-desk-updater-journal-{label}-{}-{}",
            std::process::id(),
            getrandom::u64().unwrap()
        ))
    }

    #[test]
    fn models_reject_unknown_duplicate_trailing_and_oversized_payloads() {
        let root = temp_root("strict");
        let path = root.join(PENDING_INSTALL_FILE);
        persist_pending_install(&path, &pending()).unwrap();

        let valid = fs::read_to_string(&path).unwrap();
        for invalid in [
            valid.replacen("{", "{\"claimed_success\":true,", 1),
            valid.replacen(
                "\"schema_version\"",
                "\"schema_version\":\"wrong\",\"schema_version\"",
                1,
            ),
            format!("{valid} true"),
        ] {
            fs::write(&path, invalid).unwrap();
            assert_eq!(
                load_pending_install(&path).unwrap_err(),
                "desktop_updater_pending_invalid"
            );
        }

        fs::write(&path, vec![b' '; MAX_JOURNAL_BYTES as usize + 1]).unwrap();
        assert_eq!(
            load_pending_install(&path).unwrap_err(),
            "desktop_updater_pending_too_large"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn atomic_round_trip_replaces_exactly_and_leaves_no_temporary_file() {
        let root = temp_root("round-trip");
        let pending_path = root.join(PENDING_INSTALL_FILE);
        let watermark_path = root.join(INSTALLED_WATERMARK_FILE);
        let first = pending();
        persist_pending_install(&pending_path, &first).unwrap();
        assert_eq!(load_pending_install(&pending_path).unwrap(), Some(first));

        let second =
            PendingInstall::new("1.2.0", sha('b'), "1.3.0", sha('d'), digest('e')).unwrap();
        persist_pending_install(&pending_path, &second).unwrap();
        assert_eq!(
            load_pending_install(&pending_path).unwrap(),
            Some(second.clone())
        );

        let watermark = InstalledWatermark::from_pending(&second);
        persist_installed_watermark(&watermark_path, &watermark).unwrap();
        assert_eq!(
            load_installed_watermark(&watermark_path).unwrap(),
            Some(watermark)
        );
        assert!(fs::read_dir(&root).unwrap().all(|entry| {
            !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .ends_with(".tmp")
        }));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn failed_install_evidence_is_strict_and_durable() {
        let root = temp_root("failed-install");
        let path = root.join(FAILED_INSTALL_FILE);
        let expected = pending();
        persist_failed_install(&path, &expected).unwrap();
        assert_eq!(load_failed_install(&path).unwrap(), Some(expected.clone()));
        fs::write(&path, b"{\"claimed_failure\":true}").unwrap();
        assert_eq!(
            load_failed_install(&path).unwrap_err(),
            "desktop_updater_failed_invalid"
        );
        fs::write(&path, serde_json::to_vec(&expected).unwrap()).unwrap();
        remove_failed_install(&path).unwrap();
        assert_eq!(load_failed_install(&path).unwrap(), None);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn successful_new_binary_commits_only_the_pending_identity() {
        let pending = pending();
        let decision = reconcile_startup(
            &pending.target_version,
            &pending.target_source_sha,
            None,
            Some(&pending),
        )
        .unwrap();
        assert_eq!(
            decision,
            StartupReconcile::CommitInstalled {
                watermark: InstalledWatermark::from_pending(&pending)
            }
        );
    }

    #[test]
    fn old_binary_restart_reports_failure_without_advancing_watermark() {
        let pending = pending();
        assert_eq!(
            reconcile_startup(
                &pending.from_version,
                &pending.from_source_sha,
                None,
                Some(&pending),
            )
            .unwrap(),
            StartupReconcile::PreviousInstallFailed {
                target_version: "1.2.0".to_owned()
            }
        );
    }

    #[test]
    fn source_version_and_watermark_mismatches_fail_closed() {
        let pending = pending();
        assert_eq!(
            reconcile_startup("1.2.0", &sha('f'), None, Some(&pending)).unwrap_err(),
            "desktop_updater_pending_identity_mismatch"
        );
        assert_eq!(
            reconcile_startup("1.1.5", &sha('a'), None, Some(&pending)).unwrap_err(),
            "desktop_updater_pending_identity_mismatch"
        );

        let newer = InstalledWatermark {
            schema_version: JOURNAL_SCHEMA.to_owned(),
            version: "1.3.0".to_owned(),
            source_sha: sha('d'),
            sha256: digest('e'),
        };
        assert_eq!(
            reconcile_startup(
                &pending.target_version,
                &pending.target_source_sha,
                Some(&newer),
                Some(&pending),
            )
            .unwrap_err(),
            "desktop_updater_pending_replay_rejected"
        );
    }

    #[test]
    fn commit_is_idempotent_across_crash_before_pending_removal() {
        let pending = pending();
        let installed = InstalledWatermark::from_pending(&pending);
        assert!(matches!(
            reconcile_startup(
                &pending.target_version,
                &pending.target_source_sha,
                Some(&installed),
                Some(&pending),
            )
            .unwrap(),
            StartupReconcile::CommitInstalled { .. }
        ));
    }

    #[test]
    fn missing_and_removed_pending_records_are_safe_and_idempotent() {
        let root = temp_root("remove");
        let path = root.join(PENDING_INSTALL_FILE);
        assert_eq!(load_pending_install(&path).unwrap(), None);
        remove_pending_install(&path).unwrap();
        persist_pending_install(&path, &pending()).unwrap();
        remove_pending_install(&path).unwrap();
        remove_pending_install(&path).unwrap();
        assert_eq!(load_pending_install(&path).unwrap(), None);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn constructors_and_loaders_reject_noncanonical_identifiers() {
        for (from, target, source, expected) in [
            ("1.1", "1.2.0", sha('a'), "desktop_updater_pending_invalid"),
            (
                "1.1.0",
                "1.2.0-beta.1",
                sha('a'),
                "desktop_updater_pending_invalid",
            ),
            (
                "1.2.0",
                "1.1.0",
                sha('a'),
                "desktop_updater_pending_invalid",
            ),
            (
                "1.1.0",
                "1.2.0",
                "A".repeat(40),
                "desktop_updater_pending_invalid",
            ),
        ] {
            assert_eq!(
                PendingInstall::new(from, source, target, sha('b'), digest('c')).unwrap_err(),
                expected
            );
        }

        let root = temp_root("watermark-invalid");
        let path = root.join(INSTALLED_WATERMARK_FILE);
        fs::create_dir_all(&root).unwrap();
        fs::write(
            &path,
            r#"{"schema_version":"stock-desk-updater-journal-v1","version":"1.2.0","source_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","extra":true}"#,
        )
        .unwrap();
        assert_eq!(
            load_installed_watermark(&path).unwrap_err(),
            "desktop_updater_watermark_invalid"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn prerelease_client_can_journal_an_upgrade_to_the_first_stable_release() {
        let pending =
            PendingInstall::new("1.1.0-beta.2", sha('a'), "1.1.0", sha('b'), digest('c')).unwrap();
        assert_eq!(pending.from_version, "1.1.0-beta.2");
        assert_eq!(pending.target_version, "1.1.0");
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_journal_is_rejected_instead_of_followed() {
        use std::os::unix::fs::symlink;

        let root = temp_root("symlink");
        fs::create_dir_all(&root).unwrap();
        let target = root.join("target.json");
        let link = root.join(PENDING_INSTALL_FILE);
        fs::write(&target, serde_json::to_vec(&pending()).unwrap()).unwrap();
        symlink(&target, &link).unwrap();
        assert_eq!(
            load_pending_install(&link).unwrap_err(),
            "desktop_updater_pending_invalid"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_parent_is_rejected_for_reads_and_atomic_writes() {
        use std::os::unix::fs::symlink;

        let root = temp_root("symlink-parent");
        let outside = temp_root("symlink-parent-outside");
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let linked = root.join("updater");
        symlink(&outside, &linked).unwrap();
        let path = linked.join(PENDING_INSTALL_FILE);
        assert_eq!(
            persist_pending_install(&path, &pending()).unwrap_err(),
            "desktop_updater_pending_invalid"
        );
        assert_eq!(
            load_pending_install(&path).unwrap_err(),
            "desktop_updater_pending_invalid"
        );
        assert!(!outside.join(PENDING_INSTALL_FILE).exists());
        let _ = fs::remove_dir_all(root);
        let _ = fs::remove_dir_all(outside);
    }

    #[cfg(windows)]
    #[test]
    fn windows_read_handle_blocks_concurrent_write_and_delete() {
        use std::os::windows::fs::OpenOptionsExt as _;
        use windows_sys::Win32::Foundation::GENERIC_WRITE;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };

        let root = temp_root("windows-read-lock");
        let path = root.join(PENDING_INSTALL_FILE);
        persist_pending_install(&path, &pending()).unwrap();
        let locked = open_journal_for_read_windows(&path, "desktop_updater_pending")
            .unwrap()
            .unwrap();

        let mut writer = OpenOptions::new();
        writer
            .write(true)
            .access_mode(GENERIC_WRITE)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE);
        assert!(writer.open(&path).is_err());
        assert!(fs::remove_file(&path).is_err());
        drop(locked);

        remove_pending_install(&path).unwrap();
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_temporary_is_exclusive_and_handle_rename_preserves_identity() {
        use std::io::Write as _;
        use std::os::windows::fs::OpenOptionsExt as _;
        use windows_sys::Win32::Foundation::GENERIC_WRITE;
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        };

        let root = temp_root("windows-temp-lock");
        let path = root.join(PENDING_INSTALL_FILE);
        let guard = open_windows_parent_guard(&path, true, "desktop_updater_pending")
            .unwrap()
            .unwrap();
        let (temporary, mut file, identity) =
            create_temporary_windows(&path, &guard, "desktop_updater_pending").unwrap();

        let mut writer = OpenOptions::new();
        writer
            .write(true)
            .access_mode(GENERIC_WRITE)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE);
        assert!(writer.open(&temporary).is_err());
        assert!(fs::remove_file(&temporary).is_err());

        file.write_all(&serde_json::to_vec(&pending()).unwrap())
            .unwrap();
        file.sync_all().unwrap();
        rename_windows_file_by_handle(&file, path.file_name().unwrap(), &guard).unwrap();
        file.sync_all().unwrap();
        assert_eq!(
            windows_object_identity(&file, "desktop_updater_pending").unwrap(),
            identity
        );
        drop(file);

        let published = open_journal_for_read_windows(&path, "desktop_updater_pending")
            .unwrap()
            .unwrap();
        assert_eq!(
            windows_object_identity(&published, "desktop_updater_pending").unwrap(),
            identity
        );
        drop(published);
        assert_eq!(load_pending_install(&path).unwrap(), Some(pending()));
        remove_pending_install(&path).unwrap();
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_delete_marks_the_exact_open_object_before_releasing_its_name() {
        use windows_sys::Win32::Foundation::GENERIC_READ;
        use windows_sys::Win32::Storage::FileSystem::{DELETE, FILE_SHARE_READ};

        let root = temp_root("windows-handle-delete");
        let path = root.join(PENDING_INSTALL_FILE);
        persist_pending_install(&path, &pending()).unwrap();
        let guard = open_windows_parent_guard(&path, false, "desktop_updater_pending")
            .unwrap()
            .unwrap();
        let file = open_windows_file(&path, GENERIC_READ | DELETE, FILE_SHARE_READ, false).unwrap();
        reject_windows_object_kind(&file, false, "desktop_updater_pending").unwrap();
        let identity = windows_object_identity(&file, "desktop_updater_pending").unwrap();
        revalidate_windows_parent_guard(&guard, "desktop_updater_pending").unwrap();
        assert_eq!(
            windows_object_identity(&file, "desktop_updater_pending").unwrap(),
            identity
        );

        mark_windows_file_delete_on_close(&file).unwrap();
        assert!(fs::write(&path, b"replacement").is_err());
        drop(file);
        assert!(!path.exists());
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn windows_reparse_file_and_parent_are_rejected_when_symlinks_are_available() {
        use std::os::windows::fs::{symlink_dir, symlink_file};

        let root = temp_root("windows-reparse");
        let outside = temp_root("windows-reparse-outside");
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let target = outside.join("target.json");
        let expected = serde_json::to_vec(&pending()).unwrap();
        fs::write(&target, &expected).unwrap();

        let linked_file = root.join(PENDING_INSTALL_FILE);
        if symlink_file(&target, &linked_file).is_ok() {
            assert_eq!(
                load_pending_install(&linked_file).unwrap_err(),
                "desktop_updater_pending_invalid"
            );
            assert_eq!(
                persist_pending_install(&linked_file, &pending()).unwrap_err(),
                "desktop_updater_pending_invalid"
            );
            assert_eq!(fs::read(&target).unwrap(), expected);
            fs::remove_file(&linked_file).unwrap();
        }

        let linked_parent = root.join("updater");
        if symlink_dir(&outside, &linked_parent).is_ok() {
            let linked_path = linked_parent.join(PENDING_INSTALL_FILE);
            assert_eq!(
                load_pending_install(&linked_path).unwrap_err(),
                "desktop_updater_pending_invalid"
            );
            assert_eq!(
                persist_pending_install(&linked_path, &pending()).unwrap_err(),
                "desktop_updater_pending_invalid"
            );
            assert!(!outside.join(PENDING_INSTALL_FILE).exists());
            fs::remove_dir(&linked_parent).unwrap();
        }

        let _ = fs::remove_dir_all(root);
        let _ = fs::remove_dir_all(outside);
    }
}
