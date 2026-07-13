//! Narrow, fail-closed removal of the Stock Desk v1.1 per-user data tree.
//!
//! This module is intentionally independent from Tauri. The dedicated CLI
//! mode is dispatched before the application builder, single-instance plugin,
//! or sidecar can start.

use std::{
    ffi::OsString,
    path::{Path, PathBuf},
};

#[cfg(any(not(windows), test))]
use std::fs;
#[cfg(not(windows))]
use std::{fs::Metadata, io};

const UNINSTALL_ARGUMENT: &str = "--stock-desk-uninstall-v11-data";
const PRODUCT_DIRECTORY: &str = "Stock Desk";
const V11_DIRECTORY: &str = "v1.1";

const EXIT_SUCCESS: i32 = 0;
const EXIT_INVALID_ARGUMENTS: i32 = 20;
const EXIT_UNSUPPORTED_PLATFORM: i32 = 21;
const EXIT_ROOT_UNAVAILABLE: i32 = 22;
const EXIT_UNSAFE_LAYOUT: i32 = 23;
const EXIT_RENAME_FAILED: i32 = 24;
const EXIT_DELETE_FAILED: i32 = 25;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CliAction {
    ContinueApplication,
    UninstallV11Data,
    RejectArguments,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum UninstallError {
    #[cfg_attr(windows, allow(dead_code))]
    UnsupportedPlatform,
    RootUnavailable,
    UnsafeLayout,
    RenameFailed,
    DeleteFailed,
}

impl UninstallError {
    const fn exit_code(self) -> i32 {
        match self {
            Self::UnsupportedPlatform => EXIT_UNSUPPORTED_PLATFORM,
            Self::RootUnavailable => EXIT_ROOT_UNAVAILABLE,
            Self::UnsafeLayout => EXIT_UNSAFE_LAYOUT,
            Self::RenameFailed => EXIT_RENAME_FAILED,
            Self::DeleteFailed => EXIT_DELETE_FAILED,
        }
    }
}

/// Dispatches the private uninstall mode without initializing any Tauri state.
///
/// `None` means this is an ordinary application launch. Every uninstall-mode
/// invocation returns a stable, path-free process exit code.
pub(crate) fn dispatch_from_env() -> Option<i32> {
    match classify_arguments(std::env::args_os().skip(1)) {
        CliAction::ContinueApplication => None,
        CliAction::RejectArguments => Some(EXIT_INVALID_ARGUMENTS),
        CliAction::UninstallV11Data => Some(
            uninstall_from_platform_root()
                .map(|_| EXIT_SUCCESS)
                .unwrap_or_else(UninstallError::exit_code),
        ),
    }
}

fn classify_arguments(arguments: impl IntoIterator<Item = OsString>) -> CliAction {
    let arguments = arguments.into_iter().collect::<Vec<_>>();
    let uninstall = OsString::from(UNINSTALL_ARGUMENT);
    if arguments.as_slice() == [uninstall.as_os_str()] {
        CliAction::UninstallV11Data
    } else if arguments.iter().any(|argument| argument == &uninstall) {
        CliAction::RejectArguments
    } else {
        CliAction::ContinueApplication
    }
}

fn uninstall_from_platform_root() -> Result<(), UninstallError> {
    let local_app_data = platform_local_app_data()?;
    uninstall_v11_data_at(&local_app_data)
}

fn uninstall_v11_data_at(local_app_data: &Path) -> Result<(), UninstallError> {
    uninstall_v11_data_at_with_hook(local_app_data, || {})
}

#[cfg(windows)]
fn uninstall_v11_data_at_with_hook(
    local_app_data: &Path,
    before_quarantine: impl FnOnce(),
) -> Result<(), UninstallError> {
    windows_uninstall::uninstall(local_app_data, before_quarantine)
}

#[cfg(not(windows))]
fn uninstall_v11_data_at_with_hook(
    local_app_data: &Path,
    before_quarantine: impl FnOnce(),
) -> Result<(), UninstallError> {
    require_safe_directory(local_app_data, UninstallError::RootUnavailable)?;

    let product_root = local_app_data.join(PRODUCT_DIRECTORY);
    let product_metadata = match safe_metadata(&product_root)? {
        Some(metadata) => metadata,
        None => return Ok(()),
    };
    require_directory_metadata(&product_metadata)?;

    let v11_root = product_root.join(V11_DIRECTORY);
    let v11_metadata = match safe_metadata(&v11_root)? {
        Some(metadata) => metadata,
        None => return Ok(()),
    };
    require_directory_metadata(&v11_metadata)?;

    // The complete tree is validated before its canonical path changes. An
    // unsafe entry therefore leaves the whole v1.1 tree untouched.
    validate_tree(&v11_root)?;
    let product_identity = portable_directory_identity(&product_metadata);
    let v11_identity = portable_directory_identity(&v11_metadata);

    before_quarantine();

    let current_product = safe_metadata(&product_root)?.ok_or(UninstallError::UnsafeLayout)?;
    let current_v11 = safe_metadata(&v11_root)?.ok_or(UninstallError::UnsafeLayout)?;
    require_directory_metadata(&current_product)?;
    require_directory_metadata(&current_v11)?;
    if portable_directory_identity(&current_product) != product_identity
        || portable_directory_identity(&current_v11) != v11_identity
    {
        return Err(UninstallError::UnsafeLayout);
    }

    let tombstone = product_root.join(format!(".stock-desk-v1.1-uninstall-{}", std::process::id()));
    match fs::symlink_metadata(&tombstone) {
        Ok(_) => return Err(UninstallError::UnsafeLayout),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(_) => return Err(UninstallError::UnsafeLayout),
    }

    fs::rename(&v11_root, &tombstone).map_err(|_| UninstallError::RenameFailed)?;

    // Rename is same-parent and atomic. Revalidate the detached tree before
    // deleting any entry so a raced link or reparse point fails without a
    // partial cleanup. If possible, restore the canonical name on rejection.
    if validate_tree(&tombstone).is_err() {
        let _ = fs::rename(&tombstone, &v11_root);
        return Err(UninstallError::UnsafeLayout);
    }

    remove_tree_no_follow(&tombstone).map_err(|_| UninstallError::DeleteFailed)
}

#[cfg(unix)]
fn portable_directory_identity(metadata: &Metadata) -> (u64, u64) {
    use std::os::unix::fs::MetadataExt;

    (metadata.dev(), metadata.ino())
}

#[cfg(not(any(unix, windows)))]
fn portable_directory_identity(metadata: &Metadata) -> (u64, u64) {
    (
        metadata.len(),
        metadata
            .modified()
            .ok()
            .and_then(|time| time.elapsed().ok())
            .map_or(0, |age| age.as_nanos() as u64),
    )
}

#[cfg(windows)]
mod windows_uninstall {
    use super::{UninstallError, PRODUCT_DIRECTORY, V11_DIRECTORY};
    use std::{
        collections::HashSet,
        ffi::{c_void, OsStr},
        fs::{File, OpenOptions},
        mem::{offset_of, size_of},
        os::windows::{
            ffi::OsStrExt,
            fs::OpenOptionsExt,
            io::{AsRawHandle, FromRawHandle},
        },
        path::Path,
        ptr,
    };

    type Handle = *mut c_void;
    type NtStatus = i32;

    const DELETE: u32 = 0x0001_0000;
    const SYNCHRONIZE: u32 = 0x0010_0000;
    const FILE_LIST_DIRECTORY: u32 = 0x0000_0001;
    const FILE_READ_ATTRIBUTES: u32 = 0x0000_0080;
    const FILE_TRAVERSE: u32 = 0x0000_0020;
    const FILE_SHARE_READ: u32 = 0x0000_0001;
    const FILE_SHARE_WRITE: u32 = 0x0000_0002;
    const FILE_SHARE_DELETE: u32 = 0x0000_0004;
    const FILE_ATTRIBUTE_READONLY: u32 = 0x0000_0001;
    const FILE_ATTRIBUTE_DIRECTORY: u32 = 0x0000_0010;
    const FILE_ATTRIBUTE_DEVICE: u32 = 0x0000_0040;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
    const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    const FILE_DIRECTORY_FILE: u32 = 0x0000_0001;
    const FILE_NON_DIRECTORY_FILE: u32 = 0x0000_0040;
    const FILE_SYNCHRONOUS_IO_NONALERT: u32 = 0x0000_0020;
    const FILE_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    const OBJ_CASE_INSENSITIVE: u32 = 0x0000_0040;
    const FILE_ID_BOTH_DIRECTORY_INFORMATION: i32 = 37;
    const FILE_RENAME_INFORMATION_CLASS: i32 = 10;
    const FILE_DISPOSITION_INFO_CLASS: i32 = 4;
    const STATUS_NO_MORE_FILES: NtStatus = 0x8000_0006_u32 as NtStatus;
    const STATUS_NO_SUCH_FILE: NtStatus = 0xC000_000F_u32 as NtStatus;
    const STATUS_OBJECT_NAME_NOT_FOUND: NtStatus = 0xC000_0034_u32 as NtStatus;
    const STATUS_OBJECT_PATH_NOT_FOUND: NtStatus = 0xC000_003A_u32 as NtStatus;
    const MAX_TREE_DEPTH: usize = 128;
    const MAX_TREE_ENTRIES: usize = 100_000;

    #[repr(C)]
    struct UnicodeString {
        length: u16,
        maximum_length: u16,
        buffer: *mut u16,
    }

    #[repr(C)]
    struct ObjectAttributes {
        length: u32,
        root_directory: Handle,
        object_name: *const UnicodeString,
        attributes: u32,
        security_descriptor: *const c_void,
        security_quality_of_service: *const c_void,
    }

    #[repr(C)]
    union IoStatusValue {
        status: NtStatus,
        pointer: *mut c_void,
    }

    #[repr(C)]
    struct IoStatusBlock {
        value: IoStatusValue,
        information: usize,
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct FileIdBothDirInformation {
        next_entry_offset: u32,
        file_index: u32,
        creation_time: i64,
        last_access_time: i64,
        last_write_time: i64,
        change_time: i64,
        end_of_file: i64,
        allocation_size: i64,
        file_attributes: u32,
        file_name_length: u32,
        ea_size: u32,
        short_name_length: i8,
        reserved: i8,
        short_name: [u16; 12],
        file_id: i64,
        file_name: [u16; 1],
    }

    #[repr(C)]
    struct FileTime {
        low: u32,
        high: u32,
    }

    #[repr(C)]
    struct ByHandleFileInformation {
        file_attributes: u32,
        creation_time: FileTime,
        last_access_time: FileTime,
        last_write_time: FileTime,
        volume_serial_number: u32,
        file_size_high: u32,
        file_size_low: u32,
        number_of_links: u32,
        file_index_high: u32,
        file_index_low: u32,
    }

    #[repr(C)]
    union RenameFlags {
        replace_if_exists: u8,
        flags: u32,
    }

    #[repr(C)]
    struct FileRenameInformation {
        flags: RenameFlags,
        root_directory: Handle,
        file_name_length: u32,
        file_name: [u16; 1],
    }

    #[repr(C)]
    struct FileDispositionInformation {
        delete_file: u8,
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct FileIdentity {
        volume_serial_number: u32,
        file_id: u64,
    }

    #[derive(Clone, Debug)]
    struct DirectoryEntry {
        name: Vec<u16>,
        attributes: u32,
        file_id: u64,
    }

    struct DeleteNode {
        handle: File,
        children: Vec<DeleteNode>,
    }

    #[link(name = "ntdll")]
    unsafe extern "system" {
        fn NtOpenFile(
            file_handle: *mut Handle,
            desired_access: u32,
            object_attributes: *const ObjectAttributes,
            io_status_block: *mut IoStatusBlock,
            share_access: u32,
            open_options: u32,
        ) -> NtStatus;
        fn NtQueryDirectoryFile(
            file_handle: Handle,
            event: Handle,
            apc_routine: *const c_void,
            apc_context: *const c_void,
            io_status_block: *mut IoStatusBlock,
            file_information: *mut c_void,
            length: u32,
            file_information_class: i32,
            return_single_entry: bool,
            file_name: *const UnicodeString,
            restart_scan: bool,
        ) -> NtStatus;
        fn NtSetInformationFile(
            file_handle: Handle,
            io_status_block: *mut IoStatusBlock,
            file_information: *mut c_void,
            length: u32,
            file_information_class: i32,
        ) -> NtStatus;
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn GetFileInformationByHandle(
            file: Handle,
            information: *mut ByHandleFileInformation,
        ) -> i32;
        fn SetFileInformationByHandle(
            file: Handle,
            information_class: i32,
            information: *const c_void,
            buffer_size: u32,
        ) -> i32;
    }

    pub(super) fn uninstall(
        local_app_data: &Path,
        before_quarantine: impl FnOnce(),
    ) -> Result<(), UninstallError> {
        trace_stage("begin");
        let local = open_absolute_directory(local_app_data, false)
            .map_err(|_| UninstallError::RootUnavailable)?;
        let local_identity = checked_identity(&local, true, UninstallError::RootUnavailable)?;

        let Some(product) = open_relative(&local, PRODUCT_DIRECTORY, true, false)? else {
            return Ok(());
        };
        let product_identity = checked_identity(&product, true, UninstallError::UnsafeLayout)?;

        let Some(v11) = open_relative(&product, V11_DIRECTORY, true, true)? else {
            return Ok(());
        };
        let v11_identity = checked_identity(&v11, true, UninstallError::UnsafeLayout)?;
        validate_tree(&v11)?;
        trace_stage("initial-tree-validated");

        let tombstone = format!(".stock-desk-v1.1-uninstall-{}", std::process::id());
        if probe_relative(&product, &tombstone, true)?.is_some() {
            return Err(UninstallError::UnsafeLayout);
        }

        before_quarantine();

        // Reopen every path component relative to the already-trusted parent
        // handles. Product and LocalAppData handles deny FILE_SHARE_DELETE, so
        // path replacement is blocked; the identity checks also fail closed
        // if a filesystem violates that sharing contract.
        if checked_identity(&local, true, UninstallError::UnsafeLayout)? != local_identity {
            return Err(UninstallError::UnsafeLayout);
        }
        let current_product = open_relative(&local, PRODUCT_DIRECTORY, true, false)?
            .ok_or(UninstallError::UnsafeLayout)?;
        if checked_identity(&current_product, true, UninstallError::UnsafeLayout)?
            != product_identity
        {
            return Err(UninstallError::UnsafeLayout);
        }
        let current_v11 =
            probe_relative(&product, V11_DIRECTORY, true)?.ok_or(UninstallError::UnsafeLayout)?;
        if checked_identity(&current_v11, true, UninstallError::UnsafeLayout)? != v11_identity {
            return Err(UninstallError::UnsafeLayout);
        }
        drop(current_v11);
        if checked_identity(&v11, true, UninstallError::UnsafeLayout)? != v11_identity {
            return Err(UninstallError::UnsafeLayout);
        }
        trace_stage("pre-rename-identities-verified");

        rename_relative(&v11, &product, &tombstone)?;
        trace_stage("quarantine-renamed");

        let quarantined =
            probe_relative(&product, &tombstone, true)?.ok_or(UninstallError::RenameFailed)?;
        if checked_identity(&quarantined, true, UninstallError::UnsafeLayout)? != v11_identity
            || probe_relative(&product, V11_DIRECTORY, true)?.is_some()
        {
            let _ = rename_relative(&v11, &product, V11_DIRECTORY);
            return Err(UninstallError::UnsafeLayout);
        }

        // The retained v11 handle is the object that was validated and
        // renamed. Traversal opens every child relative to an already-open
        // parent with FILE_OPEN_REPARSE_POINT and compares file IDs before
        // deletion, so no later path lookup can redirect cleanup.
        if validate_tree(&quarantined).is_err() {
            drop(quarantined);
            let _ = rename_relative(&v11, &product, V11_DIRECTORY);
            return Err(UninstallError::UnsafeLayout);
        }
        drop(quarantined);
        trace_stage("quarantine-validated");
        let tombstone_units = encode_component(OsStr::new(&tombstone))?;
        let (root_guard, delete_plan) = match build_delete_plan(&v11, &product, &tombstone_units) {
            Ok(plan) => plan,
            Err(error) => {
                let _ = rename_relative(&v11, &product, V11_DIRECTORY);
                return Err(error);
            }
        };
        trace_stage("delete-plan-built");
        // From this point onward a rare filesystem disposition failure can
        // leave a partially deleted quarantine. Never rename such a tree back
        // to the canonical v1.1 name. The strict preflight handles eliminate
        // ordinary sharing, permission, read-only, replacement, and writer
        // failures before the first mutation.
        execute_delete_plan(delete_plan)?;
        trace_stage("contents-removed");
        if mark_delete(&v11).is_err() {
            return Err(UninstallError::DeleteFailed);
        }
        drop(v11);
        drop(root_guard);
        trace_stage("root-handle-closed");

        match probe_relative(&product, &tombstone, true) {
            Ok(None) => Ok(()),
            Ok(Some(_)) | Err(_) => Err(UninstallError::DeleteFailed),
        }
    }

    fn open_absolute_directory(path: &Path, delete_access: bool) -> std::io::Result<File> {
        let mut options = OpenOptions::new();
        let access = FILE_LIST_DIRECTORY
            | FILE_TRAVERSE
            | FILE_READ_ATTRIBUTES
            | SYNCHRONIZE
            | if delete_access { DELETE } else { 0 };
        options
            .access_mode(access)
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
    }

    fn open_relative(
        parent: &File,
        name: impl AsRef<OsStr>,
        directory: bool,
        delete_access: bool,
    ) -> Result<Option<File>, UninstallError> {
        let name = encode_component(name.as_ref())?;
        open_relative_units(parent, &name, directory, delete_access)
    }

    fn open_relative_units(
        parent: &File,
        name: &[u16],
        directory: bool,
        delete_access: bool,
    ) -> Result<Option<File>, UninstallError> {
        open_relative_units_with_sharing(
            parent,
            name,
            directory,
            delete_access,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
        )
    }

    fn probe_relative(
        parent: &File,
        name: impl AsRef<OsStr>,
        directory: bool,
    ) -> Result<Option<File>, UninstallError> {
        let name = encode_component(name.as_ref())?;
        probe_relative_units(parent, &name, directory)
    }

    fn probe_relative_units(
        parent: &File,
        name: &[u16],
        directory: bool,
    ) -> Result<Option<File>, UninstallError> {
        open_relative_units_with_sharing(
            parent,
            name,
            directory,
            false,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        )
    }

    fn open_relative_units_with_sharing(
        parent: &File,
        name: &[u16],
        directory: bool,
        delete_access: bool,
        share_access: u32,
    ) -> Result<Option<File>, UninstallError> {
        if name.is_empty() || name.len() > (u16::MAX as usize / 2) {
            return Err(UninstallError::UnsafeLayout);
        }
        let mut name = name.to_vec();
        let unicode = UnicodeString {
            length: (name.len() * 2) as u16,
            maximum_length: (name.len() * 2) as u16,
            buffer: name.as_mut_ptr(),
        };
        let attributes = ObjectAttributes {
            length: size_of::<ObjectAttributes>() as u32,
            root_directory: parent.as_raw_handle(),
            object_name: &raw const unicode,
            attributes: OBJ_CASE_INSENSITIVE,
            security_descriptor: ptr::null(),
            security_quality_of_service: ptr::null(),
        };
        let mut io_status = IoStatusBlock {
            value: IoStatusValue { status: 0 },
            information: 0,
        };
        let mut raw = ptr::null_mut();
        let desired_access = FILE_READ_ATTRIBUTES
            | SYNCHRONIZE
            | if directory {
                FILE_LIST_DIRECTORY | FILE_TRAVERSE
            } else {
                0
            }
            | if delete_access { DELETE } else { 0 };
        let open_options = FILE_SYNCHRONOUS_IO_NONALERT
            | FILE_OPEN_REPARSE_POINT
            | if directory {
                FILE_DIRECTORY_FILE
            } else {
                FILE_NON_DIRECTORY_FILE
            };
        // SAFETY: every pointer references a live stack value for the duration
        // of the synchronous call, and the returned handle is owned below.
        let status = unsafe {
            NtOpenFile(
                &raw mut raw,
                desired_access,
                &raw const attributes,
                &raw mut io_status,
                share_access,
                open_options,
            )
        };
        if matches!(
            status,
            STATUS_NO_SUCH_FILE | STATUS_OBJECT_NAME_NOT_FOUND | STATUS_OBJECT_PATH_NOT_FOUND
        ) {
            return Ok(None);
        }
        if status < 0 || raw.is_null() {
            return Err(UninstallError::UnsafeLayout);
        }
        // SAFETY: NtOpenFile returned a new owned kernel handle on success.
        Ok(Some(unsafe { File::from_raw_handle(raw) }))
    }

    fn encode_component(name: &OsStr) -> Result<Vec<u16>, UninstallError> {
        let units = name.encode_wide().collect::<Vec<_>>();
        if units.is_empty()
            || units == [b'.' as u16]
            || units == [b'.' as u16, b'.' as u16]
            || units
                .iter()
                .any(|unit| *unit == 0 || *unit == u16::from(b'/') || *unit == u16::from(b'\\'))
        {
            return Err(UninstallError::UnsafeLayout);
        }
        Ok(units)
    }

    fn checked_identity(
        file: &File,
        expect_directory: bool,
        error: UninstallError,
    ) -> Result<FileIdentity, UninstallError> {
        checked_identity_and_attributes(file, expect_directory, error).map(|(identity, _)| identity)
    }

    fn checked_deletable_identity(
        file: &File,
        expect_directory: bool,
    ) -> Result<FileIdentity, UninstallError> {
        let (identity, attributes) =
            checked_identity_and_attributes(file, expect_directory, UninstallError::UnsafeLayout)?;
        if attributes & FILE_ATTRIBUTE_READONLY != 0 {
            return Err(UninstallError::UnsafeLayout);
        }
        Ok(identity)
    }

    fn checked_identity_and_attributes(
        file: &File,
        expect_directory: bool,
        error: UninstallError,
    ) -> Result<(FileIdentity, u32), UninstallError> {
        let mut information = ByHandleFileInformation {
            file_attributes: 0,
            creation_time: FileTime { low: 0, high: 0 },
            last_access_time: FileTime { low: 0, high: 0 },
            last_write_time: FileTime { low: 0, high: 0 },
            volume_serial_number: 0,
            file_size_high: 0,
            file_size_low: 0,
            number_of_links: 0,
            file_index_high: 0,
            file_index_low: 0,
        };
        // SAFETY: `information` is a correctly-sized writable output buffer
        // and `file` keeps the kernel handle alive for the call.
        if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &raw mut information) } == 0 {
            return Err(error);
        }
        let is_directory = information.file_attributes & FILE_ATTRIBUTE_DIRECTORY != 0;
        if information.file_attributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE) != 0
            || is_directory != expect_directory
        {
            return Err(error);
        }
        let identity = FileIdentity {
            volume_serial_number: information.volume_serial_number,
            file_id: (u64::from(information.file_index_high) << 32)
                | u64::from(information.file_index_low),
        };
        if identity.volume_serial_number == 0 || identity.file_id == 0 {
            return Err(error);
        }
        Ok((identity, information.file_attributes))
    }

    fn query_entries(directory: &File) -> Result<Vec<DirectoryEntry>, UninstallError> {
        let mut entries = Vec::new();
        let mut seen = HashSet::new();
        let mut restart = true;
        loop {
            let mut buffer = vec![0u8; 64 * 1024];
            let mut io_status = IoStatusBlock {
                value: IoStatusValue { status: 0 },
                information: 0,
            };
            // SAFETY: the directory handle remains live, the output buffer is
            // writable, and no asynchronous APC/event is requested.
            let status = unsafe {
                NtQueryDirectoryFile(
                    directory.as_raw_handle(),
                    ptr::null_mut(),
                    ptr::null(),
                    ptr::null(),
                    &raw mut io_status,
                    buffer.as_mut_ptr().cast(),
                    buffer.len() as u32,
                    FILE_ID_BOTH_DIRECTORY_INFORMATION,
                    false,
                    ptr::null(),
                    restart,
                )
            };
            restart = false;
            if status == STATUS_NO_MORE_FILES {
                break;
            }
            if status < 0 || io_status.information == 0 || io_status.information > buffer.len() {
                return Err(UninstallError::UnsafeLayout);
            }
            parse_directory_buffer(&buffer[..io_status.information], &mut entries, &mut seen)?;
            if entries.len() > MAX_TREE_ENTRIES {
                return Err(UninstallError::UnsafeLayout);
            }
        }
        Ok(entries)
    }

    fn parse_directory_buffer(
        buffer: &[u8],
        entries: &mut Vec<DirectoryEntry>,
        seen: &mut HashSet<Vec<u16>>,
    ) -> Result<(), UninstallError> {
        let name_offset = offset_of!(FileIdBothDirInformation, file_name);
        let mut offset = 0usize;
        loop {
            if buffer.len().saturating_sub(offset) < size_of::<FileIdBothDirInformation>() {
                return Err(UninstallError::UnsafeLayout);
            }
            // SAFETY: the bounds above cover the fixed prefix; read_unaligned
            // is used because the byte buffer does not promise struct alignment.
            let header = unsafe {
                ptr::read_unaligned(
                    buffer
                        .as_ptr()
                        .add(offset)
                        .cast::<FileIdBothDirInformation>(),
                )
            };
            let name_bytes = header.file_name_length as usize;
            if name_bytes % 2 != 0 || name_bytes > buffer.len().saturating_sub(offset + name_offset)
            {
                return Err(UninstallError::UnsafeLayout);
            }
            let name_start = offset + name_offset;
            let name = buffer[name_start..name_start + name_bytes]
                .chunks_exact(2)
                .map(|unit| u16::from_le_bytes([unit[0], unit[1]]))
                .collect::<Vec<_>>();
            if name != [b'.' as u16] && name != [b'.' as u16, b'.' as u16] {
                if name.is_empty()
                    || header.file_id == 0
                    || name.iter().any(|unit| {
                        *unit == 0 || *unit == u16::from(b'/') || *unit == u16::from(b'\\')
                    })
                    || !seen.insert(name.clone())
                {
                    return Err(UninstallError::UnsafeLayout);
                }
                entries.push(DirectoryEntry {
                    name,
                    attributes: header.file_attributes,
                    file_id: header.file_id as u64,
                });
            }
            if header.next_entry_offset == 0 {
                break;
            }
            let next = header.next_entry_offset as usize;
            if next < name_offset || next > buffer.len().saturating_sub(offset) {
                return Err(UninstallError::UnsafeLayout);
            }
            offset += next;
        }
        Ok(())
    }

    fn validate_tree(root: &File) -> Result<(), UninstallError> {
        let mut visited = 0usize;
        validate_directory(root, 0, &mut visited)
    }

    fn validate_directory(
        directory: &File,
        depth: usize,
        visited: &mut usize,
    ) -> Result<(), UninstallError> {
        if depth > MAX_TREE_DEPTH {
            return Err(UninstallError::UnsafeLayout);
        }
        for entry in query_entries(directory)? {
            *visited += 1;
            if *visited > MAX_TREE_ENTRIES
                || entry.attributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DEVICE) != 0
            {
                return Err(UninstallError::UnsafeLayout);
            }
            let is_directory = entry.attributes & FILE_ATTRIBUTE_DIRECTORY != 0;
            let child = open_relative_units(directory, &entry.name, is_directory, false)?
                .ok_or(UninstallError::UnsafeLayout)?;
            let identity = checked_identity(&child, is_directory, UninstallError::UnsafeLayout)?;
            if identity.file_id != entry.file_id {
                return Err(UninstallError::UnsafeLayout);
            }
            if is_directory {
                validate_directory(&child, depth + 1, visited)?;
            }
        }
        Ok(())
    }

    fn rename_relative(
        source: &File,
        trusted_parent: &File,
        destination_name: &str,
    ) -> Result<(), UninstallError> {
        let name = encode_component(OsStr::new(destination_name))?;
        let name_offset = offset_of!(FileRenameInformation, file_name);
        let byte_length = name
            .len()
            .checked_mul(2)
            .ok_or(UninstallError::RenameFailed)?;
        // NtSetInformationFile requires the supplied buffer to cover a
        // complete FILE_RENAME_INFORMATION followed by FileNameLength bytes.
        // Using only `file_name`'s offset under-allocates the native structure
        // on 64-bit Windows (20 + name bytes instead of 24 + name bytes).
        let total = size_of::<FileRenameInformation>()
            .checked_add(byte_length)
            .ok_or(UninstallError::RenameFailed)?;
        let mut buffer = vec![0u8; total];
        let information = buffer.as_mut_ptr().cast::<FileRenameInformation>();
        // SAFETY: the allocated buffer covers the fixed prefix and the exact
        // UTF-16 payload copied below. FILE_RENAME_INFORMATION is counted by
        // FileNameLength and does not require a NUL terminator.
        unsafe {
            ptr::addr_of_mut!((*information).flags).write(RenameFlags { flags: 0 });
            ptr::addr_of_mut!((*information).root_directory).write(trusted_parent.as_raw_handle());
            ptr::addr_of_mut!((*information).file_name_length).write(byte_length as u32);
            ptr::copy_nonoverlapping(
                name.as_ptr().cast::<u8>(),
                buffer.as_mut_ptr().add(name_offset),
                byte_length,
            );
        }
        let mut io_status = IoStatusBlock {
            value: IoStatusValue { status: 0 },
            information: 0,
        };
        // SAFETY: source and parent handles remain live, the buffer uses the
        // documented variable-length FILE_RENAME_INFORMATION layout, and the
        // synchronous call owns `io_status` for its entire duration.  Calling
        // the native class directly preserves RootDirectory-relative lookup;
        // Win32 FileRenameInfo has different class and path semantics.
        let status = unsafe {
            NtSetInformationFile(
                source.as_raw_handle(),
                &raw mut io_status,
                buffer.as_mut_ptr().cast(),
                buffer.len() as u32,
                FILE_RENAME_INFORMATION_CLASS,
            )
        };
        if status < 0 {
            return Err(UninstallError::RenameFailed);
        }
        Ok(())
    }

    fn build_delete_plan(
        root: &File,
        trusted_parent: &File,
        root_name: &[u16],
    ) -> Result<(File, Vec<DeleteNode>), UninstallError> {
        // The guard is compatible with the retained root DELETE handle but
        // deliberately omits FILE_SHARE_WRITE. Existing writers make this
        // preflight fail, and future writers cannot enter before disposition.
        let root_guard = open_relative_units_with_sharing(
            trusted_parent,
            root_name,
            true,
            false,
            FILE_SHARE_READ | FILE_SHARE_DELETE,
        )?
        .ok_or(UninstallError::UnsafeLayout)?;
        if checked_deletable_identity(&root_guard, true)? != checked_deletable_identity(root, true)?
        {
            return Err(UninstallError::UnsafeLayout);
        }
        let mut planned = 0usize;
        let children = plan_directory_children(root, &root_guard, 0, &mut planned)?;
        Ok((root_guard, children))
    }

    fn plan_directory_children(
        directory: &File,
        enumeration_handle: &File,
        depth: usize,
        planned: &mut usize,
    ) -> Result<Vec<DeleteNode>, UninstallError> {
        if depth > MAX_TREE_DEPTH {
            return Err(UninstallError::UnsafeLayout);
        }
        trace_stage("remove-probe-verified");
        let entries = query_entries(enumeration_handle)?;
        trace_stage("remove-snapshot-read");
        let mut nodes = Vec::with_capacity(entries.len());
        for entry in entries {
            *planned += 1;
            if *planned > MAX_TREE_ENTRIES
                || entry.attributes
                    & (FILE_ATTRIBUTE_READONLY
                        | FILE_ATTRIBUTE_REPARSE_POINT
                        | FILE_ATTRIBUTE_DEVICE)
                    != 0
            {
                return Err(UninstallError::UnsafeLayout);
            }
            let is_directory = entry.attributes & FILE_ATTRIBUTE_DIRECTORY != 0;
            // Each planned object requests DELETE and omits both write and
            // delete sharing. Acquiring every handle before mutation proves
            // there are no incompatible users and prevents new ones.
            let child = open_relative_units_with_sharing(
                directory,
                &entry.name,
                is_directory,
                true,
                FILE_SHARE_READ,
            )?
            .ok_or(UninstallError::UnsafeLayout)?;
            let identity = checked_deletable_identity(&child, is_directory)?;
            if identity.file_id != entry.file_id {
                return Err(UninstallError::UnsafeLayout);
            }
            trace_stage("remove-child-verified");
            let children = if is_directory {
                plan_directory_children(&child, &child, depth + 1, planned)?
            } else {
                Vec::new()
            };
            nodes.push(DeleteNode {
                handle: child,
                children,
            });
        }
        Ok(nodes)
    }

    fn execute_delete_plan(nodes: Vec<DeleteNode>) -> Result<(), UninstallError> {
        for node in nodes {
            execute_delete_node(node)?;
        }
        Ok(())
    }

    fn execute_delete_node(node: DeleteNode) -> Result<(), UninstallError> {
        execute_delete_plan(node.children)?;
        mark_delete(&node.handle).map_err(|_| UninstallError::DeleteFailed)?;
        drop(node.handle);
        trace_stage("remove-child-closed");
        Ok(())
    }

    #[cfg(test)]
    fn trace_stage(stage: &'static str) {
        eprintln!("stock-desk-uninstall-stage={stage}");
    }

    #[cfg(not(test))]
    const fn trace_stage(_stage: &'static str) {}

    fn mark_delete(file: &File) -> std::io::Result<()> {
        let information = FileDispositionInformation { delete_file: 1 };
        // SAFETY: the handle was opened with DELETE and the input structure is
        // the documented FILE_DISPOSITION_INFO layout.
        if unsafe {
            SetFileInformationByHandle(
                file.as_raw_handle(),
                FILE_DISPOSITION_INFO_CLASS,
                (&raw const information).cast(),
                size_of::<FileDispositionInformation>() as u32,
            )
        } == 0
        {
            Err(std::io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn native_information_layouts_match_the_documented_abi() {
            assert_eq!(FILE_RENAME_INFORMATION_CLASS, 10);
            assert_eq!(offset_of!(FileRenameInformation, file_name), 20);
            assert_eq!(size_of::<FileRenameInformation>(), 24);
            assert_eq!(offset_of!(FileIdBothDirInformation, file_name), 104);
            assert_eq!(size_of::<FileDispositionInformation>(), 1);
        }
    }
}

#[cfg(not(windows))]
fn require_safe_directory(path: &Path, error: UninstallError) -> Result<(), UninstallError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| error)?;
    if metadata_is_link_or_reparse(&metadata) || !metadata.is_dir() {
        return Err(error);
    }
    Ok(())
}

#[cfg(not(windows))]
fn safe_metadata(path: &Path) -> Result<Option<Metadata>, UninstallError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if metadata_is_link_or_reparse(&metadata) {
                Err(UninstallError::UnsafeLayout)
            } else {
                Ok(Some(metadata))
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(UninstallError::UnsafeLayout),
    }
}

#[cfg(not(windows))]
fn require_directory_metadata(metadata: &Metadata) -> Result<(), UninstallError> {
    if metadata_is_link_or_reparse(metadata) || !metadata.is_dir() {
        Err(UninstallError::UnsafeLayout)
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn validate_tree(root: &Path) -> Result<(), UninstallError> {
    require_safe_directory(root, UninstallError::UnsafeLayout)?;
    validate_directory_entries(root)
}

#[cfg(not(windows))]
fn validate_directory_entries(directory: &Path) -> Result<(), UninstallError> {
    let entries = fs::read_dir(directory).map_err(|_| UninstallError::UnsafeLayout)?;
    for entry in entries {
        let entry = entry.map_err(|_| UninstallError::UnsafeLayout)?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path).map_err(|_| UninstallError::UnsafeLayout)?;
        if metadata_is_link_or_reparse(&metadata) {
            return Err(UninstallError::UnsafeLayout);
        }
        if metadata.is_dir() {
            validate_directory_entries(&path)?;
        } else if !metadata.is_file() {
            return Err(UninstallError::UnsafeLayout);
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn remove_tree_no_follow(root: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(root)?;
    if metadata_is_link_or_reparse(&metadata) || !metadata.is_dir() {
        return Err(io::Error::other("unsafe uninstall root"));
    }
    // Rust 1.88's Windows implementation opens the root with
    // FILE_FLAG_OPEN_REPARSE_POINT and traverses children with NtOpenFile
    // relative to already-open parent handles. It was specifically hardened
    // against CVE-2022-21658, so a same-user junction swap cannot redirect
    // deletion outside this tombstone. The repository pins that toolchain.
    fs::remove_dir_all(root)
}

#[cfg(not(windows))]
fn metadata_is_link_or_reparse(metadata: &Metadata) -> bool {
    metadata.file_type().is_symlink()
}

#[cfg(windows)]
fn platform_local_app_data() -> Result<PathBuf, UninstallError> {
    use std::{ffi::OsString, os::windows::ffi::OsStringExt, ptr};
    use windows_sys::Win32::{
        System::Com::CoTaskMemFree,
        UI::Shell::{FOLDERID_LocalAppData, SHGetKnownFolderPath},
    };

    const MAX_KNOWN_FOLDER_UNITS: usize = 32_768;
    let folder_id = FOLDERID_LocalAppData;
    let mut raw_path = ptr::null_mut();
    // SAFETY: `raw_path` is a valid output pointer, the documented folder ID
    // is static, flags request the current path, and no impersonation token is
    // supplied. A successful allocation is released with CoTaskMemFree below.
    let status = unsafe {
        SHGetKnownFolderPath(&raw const folder_id, 0, ptr::null_mut(), &raw mut raw_path)
    };
    if status < 0 || raw_path.is_null() {
        if !raw_path.is_null() {
            // SAFETY: SHGetKnownFolderPath returned this allocation.
            unsafe { CoTaskMemFree(raw_path.cast()) };
        }
        return Err(UninstallError::RootUnavailable);
    }

    let mut length = 0usize;
    // SAFETY: SHGetKnownFolderPath returns a NUL-terminated UTF-16 string.
    // The explicit bound prevents an unbounded read if that contract is broken.
    while length < MAX_KNOWN_FOLDER_UNITS && unsafe { *raw_path.add(length) } != 0 {
        length += 1;
    }
    if length == MAX_KNOWN_FOLDER_UNITS {
        // SAFETY: SHGetKnownFolderPath returned this allocation.
        unsafe { CoTaskMemFree(raw_path.cast()) };
        return Err(UninstallError::RootUnavailable);
    }
    // SAFETY: The preceding bounded scan established `length` initialized
    // UTF-16 units before the terminator.
    let units = unsafe { std::slice::from_raw_parts(raw_path, length) };
    let path = PathBuf::from(OsString::from_wide(units));
    // SAFETY: SHGetKnownFolderPath returned this allocation and it is no longer
    // referenced after conversion to an owned OsString.
    unsafe { CoTaskMemFree(raw_path.cast()) };
    if path.as_os_str().is_empty() {
        Err(UninstallError::RootUnavailable)
    } else {
        Ok(path)
    }
}

#[cfg(not(windows))]
fn platform_local_app_data() -> Result<PathBuf, UninstallError> {
    Err(UninstallError::UnsupportedPlatform)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(1);

    struct Fixture {
        root: PathBuf,
        local_app_data: PathBuf,
    }

    impl Fixture {
        fn new(label: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "stock-desk-uninstall-{label}-{}-{}",
                std::process::id(),
                NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
            ));
            let _ = fs::remove_dir_all(&root);
            let local_app_data = root.join("fixed-local-app-data");
            fs::create_dir_all(&local_app_data).expect("fixture local app data");
            Self {
                root,
                local_app_data,
            }
        }

        fn v11_root(&self) -> PathBuf {
            self.local_app_data
                .join(PRODUCT_DIRECTORY)
                .join(V11_DIRECTORY)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[cfg(windows)]
    fn create_windows_junction(link: &Path, target: &Path) {
        use std::process::Command;

        let status = Command::new("cmd.exe")
            .args(["/d", "/c", "mklink", "/J"])
            .arg(link)
            .arg(target)
            .status()
            .expect("execute mklink");
        assert!(status.success(), "create Windows junction");
    }

    #[test]
    fn removes_only_the_normal_v11_tree() {
        let fixture = Fixture::new("normal");
        let v11 = fixture.v11_root();
        fs::create_dir_all(v11.join("market/cache")).expect("v1.1 tree");
        fs::write(v11.join("market/cache/bars.db"), b"bars").expect("v1.1 data");

        uninstall_v11_data_at(&fixture.local_app_data).expect("safe uninstall");

        assert!(!v11.exists());
        let product = fixture.local_app_data.join(PRODUCT_DIRECTORY);
        assert!(fs::read_dir(product)
            .expect("product parent")
            .next()
            .is_none());
    }

    #[test]
    fn missing_v11_root_is_idempotent() {
        let fixture = Fixture::new("idempotent");
        uninstall_v11_data_at(&fixture.local_app_data).expect("first no-op");
        fs::create_dir_all(fixture.local_app_data.join(PRODUCT_DIRECTORY)).expect("product parent");
        uninstall_v11_data_at(&fixture.local_app_data).expect("second no-op");
    }

    #[test]
    fn preserves_the_legacy_sibling_canary() {
        let fixture = Fixture::new("legacy");
        let v11 = fixture.v11_root();
        fs::create_dir_all(&v11).expect("v1.1 tree");
        fs::write(v11.join("state.db"), b"v1.1").expect("v1.1 data");
        let legacy = fixture.local_app_data.join("stock-desk");
        fs::create_dir_all(&legacy).expect("legacy tree");
        let canary = legacy.join("v1-canary.txt");
        fs::write(&canary, b"do not touch").expect("legacy canary");

        uninstall_v11_data_at(&fixture.local_app_data).expect("safe uninstall");

        assert_eq!(
            fs::read(&canary).expect("legacy canary remains"),
            b"do not touch"
        );
    }

    #[cfg(windows)]
    #[test]
    #[allow(
        clippy::permissions_set_readonly_false,
        reason = "this Windows-only test must clear the DOS read-only attribute for fixture cleanup"
    )]
    fn windows_preflight_failure_preserves_the_complete_v11_tree() {
        let fixture = Fixture::new("windows-delete-preflight");
        let v11 = fixture.v11_root();
        fs::create_dir_all(&v11).expect("v1.1 tree");
        let first = v11.join("a-first.txt");
        let protected = v11.join("z-read-only.txt");
        fs::write(&first, b"first").expect("first data");
        fs::write(&protected, b"protected").expect("protected data");
        let mut permissions = fs::metadata(&protected)
            .expect("protected metadata")
            .permissions();
        permissions.set_readonly(true);
        fs::set_permissions(&protected, permissions).expect("make child read-only");

        assert_eq!(
            uninstall_v11_data_at(&fixture.local_app_data),
            Err(UninstallError::UnsafeLayout)
        );
        assert_eq!(fs::read(&first).expect("first remains"), b"first");
        assert_eq!(
            fs::read(&protected).expect("protected remains"),
            b"protected"
        );
        assert!(v11.is_dir());
        assert_eq!(
            fs::read_dir(fixture.local_app_data.join(PRODUCT_DIRECTORY))
                .expect("product entries")
                .count(),
            1
        );

        let mut permissions = fs::metadata(&protected)
            .expect("restored protected metadata")
            .permissions();
        permissions.set_readonly(false);
        fs::set_permissions(&protected, permissions).expect("clear child read-only flag");
    }

    #[cfg(unix)]
    #[test]
    fn rejects_a_symlinked_v11_root_without_touching_its_target() {
        use std::os::unix::fs::symlink;

        let fixture = Fixture::new("root-link");
        let outside = fixture.root.join("outside-root");
        fs::create_dir_all(&outside).expect("outside root");
        let canary = outside.join("canary.txt");
        fs::write(&canary, b"safe").expect("outside canary");
        let product = fixture.local_app_data.join(PRODUCT_DIRECTORY);
        fs::create_dir_all(&product).expect("product parent");
        symlink(&outside, product.join(V11_DIRECTORY)).expect("v1.1 root symlink");

        assert_eq!(
            uninstall_v11_data_at(&fixture.local_app_data),
            Err(UninstallError::UnsafeLayout)
        );
        assert_eq!(fs::read(&canary).expect("outside remains"), b"safe");
    }

    #[cfg(unix)]
    #[test]
    fn rejects_a_symlinked_product_parent_without_touching_its_target() {
        use std::os::unix::fs::symlink;

        let fixture = Fixture::new("parent-link");
        let outside = fixture.root.join("outside-product");
        let outside_v11 = outside.join(V11_DIRECTORY);
        fs::create_dir_all(&outside_v11).expect("outside v1.1 root");
        let canary = outside_v11.join("canary.txt");
        fs::write(&canary, b"safe").expect("outside canary");
        symlink(&outside, fixture.local_app_data.join(PRODUCT_DIRECTORY))
            .expect("product parent symlink");

        assert_eq!(
            uninstall_v11_data_at(&fixture.local_app_data),
            Err(UninstallError::UnsafeLayout)
        );
        assert_eq!(fs::read(&canary).expect("outside remains"), b"safe");
    }

    #[cfg(unix)]
    #[test]
    fn rejects_a_descendant_symlink_without_partial_deletion() {
        use std::os::unix::fs::symlink;

        let fixture = Fixture::new("child-link");
        let v11 = fixture.v11_root();
        fs::create_dir_all(v11.join("cache")).expect("v1.1 tree");
        let retained = v11.join("state.db");
        fs::write(&retained, b"retain me").expect("retained data");
        let outside = fixture.root.join("outside-child");
        fs::create_dir_all(&outside).expect("outside child");
        let canary = outside.join("canary.txt");
        fs::write(&canary, b"safe").expect("outside canary");
        symlink(&outside, v11.join("cache/linked")).expect("descendant symlink");

        assert_eq!(
            uninstall_v11_data_at(&fixture.local_app_data),
            Err(UninstallError::UnsafeLayout)
        );
        assert_eq!(
            fs::read(&retained).expect("v1.1 data remains"),
            b"retain me"
        );
        assert_eq!(fs::read(&canary).expect("outside remains"), b"safe");
    }

    #[cfg(unix)]
    #[test]
    fn rejects_an_injected_product_identity_swap_before_quarantine() {
        let fixture = Fixture::new("injected-parent-swap");
        let product = fixture.local_app_data.join(PRODUCT_DIRECTORY);
        let v11 = fixture.v11_root();
        fs::create_dir_all(&v11).expect("v1.1 tree");
        let original = v11.join("original.txt");
        fs::write(&original, b"original").expect("original data");
        let displaced = fixture.root.join("displaced-product");
        let replacement_v11 = product.join(V11_DIRECTORY);
        let replacement = replacement_v11.join("replacement.txt");

        let result = uninstall_v11_data_at_with_hook(&fixture.local_app_data, || {
            fs::rename(&product, &displaced).expect("replace validated product identity");
            fs::create_dir_all(&replacement_v11).expect("replacement v1.1 tree");
            fs::write(&replacement, b"replacement").expect("replacement data");
        });

        assert_eq!(result, Err(UninstallError::UnsafeLayout));
        assert_eq!(
            fs::read(displaced.join(V11_DIRECTORY).join("original.txt"))
                .expect("original tree remains"),
            b"original"
        );
        assert_eq!(
            fs::read(&replacement).expect("replacement tree remains"),
            b"replacement"
        );
    }

    #[cfg(windows)]
    #[test]
    fn windows_product_handle_blocks_or_rejects_injected_parent_swap() {
        use std::cell::Cell;

        let fixture = Fixture::new("windows-parent-swap");
        let product = fixture.local_app_data.join(PRODUCT_DIRECTORY);
        let v11 = fixture.v11_root();
        fs::create_dir_all(&v11).expect("v1.1 tree");
        fs::write(v11.join("state.db"), b"state").expect("v1.1 data");
        let displaced = fixture.root.join("displaced-product");
        let replacement_v11 = product.join(V11_DIRECTORY);
        let replacement = replacement_v11.join("replacement.txt");
        let swap_blocked = Cell::new(false);

        let result = uninstall_v11_data_at_with_hook(&fixture.local_app_data, || {
            if fs::rename(&product, &displaced).is_err() {
                swap_blocked.set(true);
                return;
            }
            fs::create_dir_all(&replacement_v11).expect("replacement v1.1 tree");
            fs::write(&replacement, b"replacement").expect("replacement data");
        });

        if swap_blocked.get() {
            assert_eq!(result, Ok(()));
            assert!(!v11.exists());
        } else {
            assert_eq!(result, Err(UninstallError::UnsafeLayout));
            assert_eq!(
                fs::read(displaced.join(V11_DIRECTORY).join("state.db"))
                    .expect("original tree remains"),
                b"state"
            );
            assert_eq!(
                fs::read(&replacement).expect("replacement tree remains"),
                b"replacement"
            );
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_rejects_a_product_junction_without_touching_its_target() {
        let fixture = Fixture::new("windows-parent-junction");
        let outside = fixture.root.join("outside-product");
        let outside_v11 = outside.join(V11_DIRECTORY);
        fs::create_dir_all(&outside_v11).expect("outside v1.1 root");
        let canary = outside_v11.join("canary.txt");
        fs::write(&canary, b"safe").expect("outside canary");
        let product = fixture.local_app_data.join(PRODUCT_DIRECTORY);
        create_windows_junction(&product, &outside);

        assert_eq!(
            uninstall_v11_data_at(&fixture.local_app_data),
            Err(UninstallError::UnsafeLayout)
        );
        assert_eq!(fs::read(&canary).expect("outside remains"), b"safe");
    }

    #[cfg(windows)]
    #[test]
    fn windows_rejects_an_injected_descendant_junction_after_validation() {
        let fixture = Fixture::new("windows-injected-child-junction");
        let v11 = fixture.v11_root();
        let cache = v11.join("cache");
        fs::create_dir_all(&cache).expect("v1.1 cache");
        fs::write(v11.join("state.db"), b"state").expect("v1.1 data");
        let outside = fixture.root.join("outside-child");
        fs::create_dir_all(&outside).expect("outside child");
        let canary = outside.join("canary.txt");
        fs::write(&canary, b"safe").expect("outside canary");

        let result = uninstall_v11_data_at_with_hook(&fixture.local_app_data, || {
            fs::remove_dir(&cache).expect("remove empty validated cache");
            create_windows_junction(&cache, &outside);
        });

        assert_eq!(result, Err(UninstallError::UnsafeLayout));
        assert_eq!(fs::read(&canary).expect("outside remains"), b"safe");
        assert!(
            v11.exists(),
            "quarantined tree was restored by bound handle"
        );
    }

    #[test]
    fn rejects_extra_uninstall_arguments() {
        assert_eq!(
            classify_arguments([
                OsString::from(UNINSTALL_ARGUMENT),
                OsString::from("unexpected"),
            ]),
            CliAction::RejectArguments
        );
        assert_eq!(
            classify_arguments([
                OsString::from("unexpected"),
                OsString::from(UNINSTALL_ARGUMENT)
            ]),
            CliAction::RejectArguments
        );
        assert_eq!(
            classify_arguments([OsString::from(UNINSTALL_ARGUMENT)]),
            CliAction::UninstallV11Data
        );
    }

    #[test]
    fn exit_codes_are_stable_and_path_free() {
        let cases = [
            (
                UninstallError::UnsupportedPlatform,
                EXIT_UNSUPPORTED_PLATFORM,
            ),
            (UninstallError::RootUnavailable, EXIT_ROOT_UNAVAILABLE),
            (UninstallError::UnsafeLayout, EXIT_UNSAFE_LAYOUT),
            (UninstallError::RenameFailed, EXIT_RENAME_FAILED),
            (UninstallError::DeleteFailed, EXIT_DELETE_FAILED),
        ];
        for (error, expected) in cases {
            assert_eq!(error.exit_code(), expected);
            assert!((20..=25).contains(&expected));
        }
    }

    #[cfg(not(windows))]
    #[test]
    fn production_non_windows_resolution_is_fixed_and_unsupported() {
        assert_eq!(
            platform_local_app_data(),
            Err(UninstallError::UnsupportedPlatform)
        );
    }
}
