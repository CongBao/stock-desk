//! Windows Job Object ownership for sidecar process containment.
//!
//! This module deliberately does not start or supervise processes. The caller
//! creates the job before spawning a child, assigns the child's PID as early as
//! possible, and keeps the [`WindowsJob`] alive for the lifetime of the host.

use std::fmt;

/// Stable failure categories exposed to the desktop lifecycle layer.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg_attr(not(windows), allow(dead_code))]
pub enum WindowsJobError {
    // This stable cross-platform error code is only emitted by the non-Windows
    // stub, but remains part of the shared diagnostic contract on Windows.
    #[cfg_attr(windows, allow(dead_code))]
    UnsupportedPlatform,
    InvalidProcessId,
    CreateFailed,
    ConfigureFailed,
    OpenProcessFailed,
    AssignProcessFailed,
    TerminateFailed,
}

impl WindowsJobError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::UnsupportedPlatform => "windows_job_unsupported",
            Self::InvalidProcessId => "windows_job_invalid_process_id",
            Self::CreateFailed => "windows_job_create_failed",
            Self::ConfigureFailed => "windows_job_configure_failed",
            Self::OpenProcessFailed => "windows_job_open_process_failed",
            Self::AssignProcessFailed => "windows_job_assign_process_failed",
            Self::TerminateFailed => "windows_job_terminate_failed",
        }
    }
}

impl fmt::Display for WindowsJobError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for WindowsJobError {}

/// Owns a kill-on-close Windows Job Object when supported.
pub struct WindowsJob {
    inner: platform::PlatformJob,
}

impl WindowsJob {
    /// Creates an unnamed job configured to terminate all assigned processes
    /// when its final handle is closed.
    pub fn new_kill_on_close() -> Result<Self, WindowsJobError> {
        platform::PlatformJob::new_kill_on_close().map(|inner| Self { inner })
    }

    /// Returns true only when this value owns a real, configured Windows job.
    pub const fn is_enforced(&self) -> bool {
        self.inner.is_enforced()
    }

    /// Assigns an existing process using only the rights Windows requires for
    /// `AssignProcessToJobObject`.
    pub fn assign_pid(&self, process_id: u32) -> Result<(), WindowsJobError> {
        if process_id == 0 {
            return Err(WindowsJobError::InvalidProcessId);
        }
        self.inner.assign_pid(process_id)
    }

    /// Immediately terminates every process in the job.
    ///
    /// This is an abnormal-shutdown fallback, not the normal exit path.
    pub fn terminate(&self, exit_code: u32) -> Result<(), WindowsJobError> {
        self.inner.terminate(exit_code)
    }
}

impl fmt::Debug for WindowsJob {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("WindowsJob")
            .field("enforced", &self.is_enforced())
            .finish()
    }
}

#[cfg(windows)]
mod platform {
    use super::WindowsJobError;
    use std::{ffi::c_void, mem::size_of, ptr};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE},
        },
    };

    pub(super) struct PlatformJob {
        handle: HANDLE,
    }

    // Job handles can be used from any thread. Ownership remains unique and
    // Drop closes the handle exactly once.
    unsafe impl Send for PlatformJob {}
    unsafe impl Sync for PlatformJob {}

    impl PlatformJob {
        pub(super) fn new_kill_on_close() -> Result<Self, WindowsJobError> {
            // SAFETY: Both pointers are null to request default security and an
            // unnamed job. A non-null returned handle is uniquely owned below.
            let handle = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
            if handle.is_null() {
                return Err(WindowsJobError::CreateFailed);
            }

            let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

            // SAFETY: `limits` is a correctly initialized structure and remains
            // alive for the complete call. `handle` is a valid job handle.
            let configured = unsafe {
                SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    (&raw const limits).cast::<c_void>(),
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
            };
            if configured == 0 {
                // SAFETY: `handle` is valid and has not been closed.
                unsafe { CloseHandle(handle) };
                return Err(WindowsJobError::ConfigureFailed);
            }

            Ok(Self { handle })
        }

        pub(super) const fn is_enforced(&self) -> bool {
            true
        }

        pub(super) fn assign_pid(&self, process_id: u32) -> Result<(), WindowsJobError> {
            // These are the two documented rights required by
            // AssignProcessToJobObject; the handle is deliberately not inherited.
            let process =
                unsafe { OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, process_id) };
            if process.is_null() {
                return Err(WindowsJobError::OpenProcessFailed);
            }

            // SAFETY: Both handles are valid for the duration of the call.
            let assigned = unsafe { AssignProcessToJobObject(self.handle, process) };
            // SAFETY: `process` is a uniquely owned OpenProcess result.
            unsafe { CloseHandle(process) };

            if assigned == 0 {
                Err(WindowsJobError::AssignProcessFailed)
            } else {
                Ok(())
            }
        }

        pub(super) fn terminate(&self, exit_code: u32) -> Result<(), WindowsJobError> {
            // SAFETY: `self.handle` remains valid for the lifetime of `self`.
            if unsafe { TerminateJobObject(self.handle, exit_code) } == 0 {
                Err(WindowsJobError::TerminateFailed)
            } else {
                Ok(())
            }
        }
    }

    impl Drop for PlatformJob {
        fn drop(&mut self) {
            // SAFETY: this type uniquely owns the handle and Drop runs once.
            unsafe { CloseHandle(self.handle) };
        }
    }
}

#[cfg(not(windows))]
mod platform {
    use super::WindowsJobError;

    pub(super) struct PlatformJob;

    impl PlatformJob {
        pub(super) const fn new_kill_on_close() -> Result<Self, WindowsJobError> {
            Ok(Self)
        }

        pub(super) const fn is_enforced(&self) -> bool {
            false
        }

        pub(super) const fn assign_pid(&self, _process_id: u32) -> Result<(), WindowsJobError> {
            Err(WindowsJobError::UnsupportedPlatform)
        }

        pub(super) const fn terminate(&self, _exit_code: u32) -> Result<(), WindowsJobError> {
            Err(WindowsJobError::UnsupportedPlatform)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{WindowsJob, WindowsJobError};

    #[test]
    fn errors_have_stable_non_sensitive_codes() {
        let cases = [
            (
                WindowsJobError::UnsupportedPlatform,
                "windows_job_unsupported",
            ),
            (
                WindowsJobError::InvalidProcessId,
                "windows_job_invalid_process_id",
            ),
            (WindowsJobError::CreateFailed, "windows_job_create_failed"),
            (
                WindowsJobError::ConfigureFailed,
                "windows_job_configure_failed",
            ),
            (
                WindowsJobError::OpenProcessFailed,
                "windows_job_open_process_failed",
            ),
            (
                WindowsJobError::AssignProcessFailed,
                "windows_job_assign_process_failed",
            ),
            (
                WindowsJobError::TerminateFailed,
                "windows_job_terminate_failed",
            ),
        ];

        for (error, code) in cases {
            assert_eq!(error.code(), code);
            assert_eq!(error.to_string(), code);
            assert_eq!(format!("{error:?}"), format!("{error:?}"));
        }
    }

    #[test]
    fn zero_is_never_accepted_as_a_process_id() {
        let job = WindowsJob::new_kill_on_close().expect("job wrapper should initialize");
        assert_eq!(job.assign_pid(0), Err(WindowsJobError::InvalidProcessId));
    }

    #[test]
    fn debug_only_reports_enforcement_state() {
        let job = WindowsJob::new_kill_on_close().expect("job wrapper should initialize");
        let debug = format!("{job:?}");
        assert!(
            debug == "WindowsJob { enforced: true }" || debug == "WindowsJob { enforced: false }"
        );
        assert!(!debug.contains("handle"));
        assert!(!debug.contains("pid"));
    }

    #[cfg(not(windows))]
    #[test]
    fn non_windows_stub_never_claims_or_performs_protection() {
        let job = WindowsJob::new_kill_on_close().expect("safe stub should initialize");
        assert!(!job.is_enforced());
        assert_eq!(job.assign_pid(1), Err(WindowsJobError::UnsupportedPlatform));
        assert_eq!(job.terminate(1), Err(WindowsJobError::UnsupportedPlatform));
    }

    #[cfg(windows)]
    #[test]
    fn windows_job_is_configured_on_creation() {
        let job = WindowsJob::new_kill_on_close().expect("Windows job should initialize");
        assert!(job.is_enforced());
    }
}
