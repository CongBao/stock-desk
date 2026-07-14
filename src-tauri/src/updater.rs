//! Trusted desktop updater runtime.
//!
//! The complete host-owned path is compiled here, while the source-bound
//! runtime configuration remains disabled until the public key, protected
//! signing flow, and fresh Windows 10/11 evidence are all approved. Web IPC can
//! request a check or prompt but cannot provide metadata, bytes, a native
//! consent result, an Authenticode result, an installer path, or install state.
#[cfg(test)]
use std::fs;
#[cfg(test)]
use std::path::Path;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use minisign_verify::{PublicKey, Signature};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{App, AppHandle, Emitter, Manager};
use url::Url;

use crate::updater_journal::{
    self, PendingInstall, StartupReconcile, FAILED_INSTALL_FILE, INSTALLED_WATERMARK_FILE,
    PENDING_INSTALL_FILE,
};
use crate::updater_transport::{self, RequestKind};
use crate::updater_windows::{self, NativeUpdateConsent, SecureStagedInstaller};

pub const UPDATE_ENDPOINT: &str =
    "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json";
pub const UPDATE_TARGET: &str = "windows-x86_64-nsis";
pub const UPDATE_ARCH: &str = "x86_64";

const CURRENT_VERSION: &str = env!("CARGO_PKG_VERSION");
const CURRENT_SOURCE_SHA: &str = env!("STOCK_DESK_SOURCE_REVISION");
const UPDATE_EVENT: &str = "desktop-update-state";
const TRUSTED_METADATA_SCHEMA: &str = "stock-desk-trusted-updater-v1";
const MAX_METADATA_BYTES: usize = 32 * 1024;
const MAX_URL_BYTES: usize = 512;
const MIN_SIGNATURE_BYTES: usize = 16;
const MAX_SIGNATURE_BYTES: usize = 16 * 1024;
const RUNTIME_CONFIG_JSON: &str = include_str!("../../config/tauri-updater-runtime.json");

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
struct RuntimeConfig {
    schema_version: u8,
    enabled: bool,
    channel: String,
    endpoint: String,
    target: String,
    arch: String,
    public_key: Option<String>,
    public_key_sha256: Option<String>,
}

fn runtime_config() -> Result<RuntimeConfig, &'static str> {
    let config: RuntimeConfig =
        serde_json::from_str(RUNTIME_CONFIG_JSON).map_err(|_| "desktop_updater_config_invalid")?;
    if config.schema_version != 1
        || config.channel != "stable"
        || config.endpoint != UPDATE_ENDPOINT
        || config.target != UPDATE_TARGET
        || config.arch != UPDATE_ARCH
    {
        return Err("desktop_updater_config_invalid");
    }
    match (&config.public_key, &config.public_key_sha256) {
        (Some(key), Some(expected))
            if !key.is_empty()
                && is_lower_hex(expected, 64)
                && sha256_hex(key.as_bytes()) == *expected => {}
        (None, None) if !config.enabled => {}
        _ => return Err("desktop_updater_config_invalid"),
    }
    Ok(config)
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum DesktopUpdateState {
    Disabled {
        current_version: String,
    },
    Idle {
        current_version: String,
    },
    Checking {
        current_version: String,
    },
    Available {
        current_version: String,
        version: String,
        notes: Option<String>,
    },
    Downloading {
        current_version: String,
        version: String,
    },
    Verifying {
        current_version: String,
        version: String,
    },
    ReadyToInstall {
        current_version: String,
        version: String,
    },
    Installing {
        current_version: String,
        version: String,
    },
    Failed {
        current_version: String,
        code: &'static str,
        can_retry: bool,
    },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
struct TrustedUpdaterMetadata {
    schema_version: String,
    channel: String,
    version: String,
    target: String,
    arch: String,
    source_sha: String,
    url: String,
    sha256: String,
    signature: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReleaseCandidate {
    pub version: String,
    pub channel: String,
    pub target: String,
    pub arch: String,
    pub download_url: String,
    pub sha256: String,
    pub source_sha: String,
    pub signature: String,
    pub notes: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InstalledWatermark {
    version: String,
    source_sha: String,
    sha256: String,
}

#[derive(Debug)]
struct VerifiedDownload {
    version: String,
    source_sha: String,
    sha256: String,
    signature_sha256: String,
}

#[derive(Debug)]
pub struct TrustedUpdateMachine {
    current_version: Version,
    state: DesktopUpdateState,
    candidate: Option<ReleaseCandidate>,
    installed_watermark: Option<InstalledWatermark>,
    verified_pending: Option<VerifiedDownload>,
}

impl TrustedUpdateMachine {
    pub fn new(
        current_version: &str,
        enabled: bool,
        installed_watermark: Option<InstalledWatermark>,
    ) -> Result<Self, &'static str> {
        let parsed = parse_current_version(current_version)
            .map_err(|_| "desktop_updater_version_invalid")?;
        if let Some(watermark) = installed_watermark.as_ref() {
            validate_watermark(watermark, &parsed)?;
        }
        let state = if enabled {
            DesktopUpdateState::Idle {
                current_version: current_version.to_owned(),
            }
        } else {
            DesktopUpdateState::Disabled {
                current_version: current_version.to_owned(),
            }
        };
        Ok(Self {
            current_version: parsed.clone(),
            state,
            candidate: None,
            installed_watermark,
            verified_pending: None,
        })
    }

    pub fn state(&self) -> &DesktopUpdateState {
        &self.state
    }

    pub fn begin_check(&mut self) -> Result<(), &'static str> {
        if !matches!(self.state, DesktopUpdateState::Idle { .. }) {
            return Err(
                if matches!(self.state, DesktopUpdateState::Disabled { .. }) {
                    "desktop_updater_disabled"
                } else {
                    "desktop_updater_busy"
                },
            );
        }
        self.state = DesktopUpdateState::Checking {
            current_version: self.current_version.to_string(),
        };
        Ok(())
    }

    pub fn offer(&mut self, candidate: ReleaseCandidate) -> Result<(), &'static str> {
        if !matches!(self.state, DesktopUpdateState::Checking { .. }) {
            return Err("desktop_updater_not_checking");
        }
        validate_candidate(
            &candidate,
            &self.current_version,
            self.installed_watermark.as_ref(),
        )
        .inspect_err(|code| self.fail(code, true))?;
        self.state = DesktopUpdateState::Available {
            current_version: self.current_version.to_string(),
            version: candidate.version.clone(),
            notes: candidate.notes.clone(),
        };
        self.candidate = Some(candidate);
        Ok(())
    }

    pub fn dismiss(&mut self) -> Result<(), &'static str> {
        if !matches!(self.state, DesktopUpdateState::Available { .. }) {
            return Err("desktop_updater_nothing_to_dismiss");
        }
        self.candidate = None;
        self.state = DesktopUpdateState::Idle {
            current_version: self.current_version.to_string(),
        };
        Ok(())
    }

    pub fn confirm_download(&mut self) -> Result<(), &'static str> {
        let version = self.candidate_version("desktop_updater_confirmation_required")?;
        if !matches!(self.state, DesktopUpdateState::Available { .. }) {
            return Err("desktop_updater_confirmation_required");
        }
        self.state = DesktopUpdateState::Downloading {
            current_version: self.current_version.to_string(),
            version,
        };
        Ok(())
    }

    pub fn begin_verification(&mut self) -> Result<(), &'static str> {
        if !matches!(self.state, DesktopUpdateState::Downloading { .. }) {
            return Err("desktop_updater_not_downloaded");
        }
        let version = self.candidate_version("desktop_updater_candidate_missing")?;
        self.state = DesktopUpdateState::Verifying {
            current_version: self.current_version.to_string(),
            version,
        };
        Ok(())
    }

    fn finish_verification(&mut self, verified: &VerifiedDownload) -> Result<(), &'static str> {
        if !matches!(self.state, DesktopUpdateState::Verifying { .. }) {
            return Err("desktop_updater_not_verifying");
        }
        let candidate = self
            .candidate
            .as_ref()
            .ok_or("desktop_updater_candidate_missing")?;
        if candidate.version != verified.version
            || candidate.source_sha != verified.source_sha
            || candidate.sha256 != verified.sha256
            || sha256_hex(candidate.signature.as_bytes()) != verified.signature_sha256
        {
            self.fail("desktop_updater_verified_identity_mismatch", false);
            return Err("desktop_updater_verified_identity_mismatch");
        }
        let version = verified.version.clone();
        self.verified_pending = Some(VerifiedDownload {
            version: verified.version.clone(),
            source_sha: verified.source_sha.clone(),
            sha256: verified.sha256.clone(),
            signature_sha256: verified.signature_sha256.clone(),
        });
        self.state = DesktopUpdateState::ReadyToInstall {
            current_version: self.current_version.to_string(),
            version,
        };
        Ok(())
    }

    fn matches_verified_identity(&self, verified: &VerifiedDownload) -> bool {
        let Some(candidate) = self.candidate.as_ref() else {
            return false;
        };
        let Some(pending) = self.verified_pending.as_ref() else {
            return false;
        };
        candidate.version == verified.version
            && candidate.source_sha == verified.source_sha
            && candidate.sha256 == verified.sha256
            && sha256_hex(candidate.signature.as_bytes()) == verified.signature_sha256
            && pending.version == verified.version
            && pending.source_sha == verified.source_sha
            && pending.sha256 == verified.sha256
            && pending.signature_sha256 == verified.signature_sha256
    }

    pub fn begin_install(&mut self) -> Result<(), &'static str> {
        if !matches!(self.state, DesktopUpdateState::ReadyToInstall { .. }) {
            return Err("desktop_updater_not_verified");
        }
        let version = self.candidate_version("desktop_updater_candidate_missing")?;
        self.state = DesktopUpdateState::Installing {
            current_version: self.current_version.to_string(),
            version,
        };
        Ok(())
    }

    fn pending_install_watermark(&self) -> Result<InstalledWatermark, &'static str> {
        if !matches!(self.state, DesktopUpdateState::Installing { .. }) {
            return Err("desktop_updater_not_installing");
        }
        let verified = self
            .verified_pending
            .as_ref()
            .ok_or("desktop_updater_verified_identity_missing")?;
        let candidate = self
            .candidate
            .as_ref()
            .ok_or("desktop_updater_candidate_missing")?;
        if candidate.version != verified.version
            || candidate.source_sha != verified.source_sha
            || candidate.sha256 != verified.sha256
            || sha256_hex(candidate.signature.as_bytes()) != verified.signature_sha256
        {
            return Err("desktop_updater_verified_identity_mismatch");
        }
        Ok(InstalledWatermark {
            version: verified.version.clone(),
            source_sha: verified.source_sha.clone(),
            sha256: verified.sha256.clone(),
        })
    }

    #[cfg(test)]
    fn commit_install_success(
        &mut self,
        installed: &InstalledWatermark,
    ) -> Result<(), &'static str> {
        let pending = self.pending_install_watermark()?;
        if pending != *installed {
            return Err("desktop_updater_verified_identity_mismatch");
        }
        self.installed_watermark = Some(installed.clone());
        self.verified_pending = None;
        Ok(())
    }

    fn retry_after_install_persistence_failure(&mut self) -> Result<(), &'static str> {
        let version = self.pending_install_watermark()?.version;
        self.state = DesktopUpdateState::ReadyToInstall {
            current_version: self.current_version.to_string(),
            version,
        };
        Ok(())
    }

    pub fn fail(&mut self, code: &'static str, can_retry: bool) {
        self.candidate = None;
        self.verified_pending = None;
        self.state = DesktopUpdateState::Failed {
            current_version: self.current_version.to_string(),
            code,
            can_retry,
        };
    }

    pub fn recover_after_failed_check(&mut self) {
        self.candidate = None;
        self.verified_pending = None;
        self.state = DesktopUpdateState::Idle {
            current_version: self.current_version.to_string(),
        };
    }

    #[cfg(test)]
    fn installed_watermark(&self) -> Option<&InstalledWatermark> {
        self.installed_watermark.as_ref()
    }

    fn candidate_version(&self, error: &'static str) -> Result<String, &'static str> {
        self.candidate
            .as_ref()
            .map(|candidate| candidate.version.clone())
            .ok_or(error)
    }
}

#[derive(Clone, Debug)]
struct UpdaterPaths {
    installed_watermark: PathBuf,
    pending_install: PathBuf,
    failed_install: PathBuf,
    staging_directory: PathBuf,
}

#[derive(Debug)]
struct ReconciledUpdaterState {
    installed: Option<InstalledWatermark>,
    previous_install_failed: bool,
}

struct VerifiedInstall {
    _staged_installer: SecureStagedInstaller,
    identity: VerifiedDownload,
}

struct UpdaterRuntimeState {
    machine: TrustedUpdateMachine,
    verified_install: Option<VerifiedInstall>,
}

pub struct DesktopUpdateController {
    runtime: Mutex<UpdaterRuntimeState>,
    config: RuntimeConfig,
    paths: Option<UpdaterPaths>,
}

pub fn plugin<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R, tauri_plugin_updater::Config> {
    // Endpoints and key remain empty in tauri.conf.json. The plugin stays
    // inert; the host-owned bounded transport and Tauri-compatible Minisign
    // verifier are the only runtime path.
    tauri_plugin_updater::Builder::new().build()
}

impl DesktopUpdateController {
    fn new(config: RuntimeConfig, paths: Option<UpdaterPaths>) -> Self {
        let persisted = paths
            .as_ref()
            .map(reconcile_persisted_state)
            .unwrap_or(Err("desktop_updater_state_path_unavailable"));
        let enabled = config.enabled && cfg!(windows) && paths.is_some() && persisted.is_ok();
        let startup_error = persisted.as_ref().err().copied();
        let reconciled = persisted.unwrap_or(ReconciledUpdaterState {
            installed: None,
            previous_install_failed: false,
        });
        let mut machine = TrustedUpdateMachine::new(CURRENT_VERSION, enabled, reconciled.installed)
            .expect("Cargo package version must be valid SemVer");
        if config.enabled {
            if let Some(code) = startup_error {
                machine.fail(code, false);
            } else if reconciled.previous_install_failed {
                machine.fail("desktop_updater_previous_install_failed", true);
            }
        }
        Self {
            runtime: Mutex::new(UpdaterRuntimeState {
                machine,
                verified_install: None,
            }),
            config,
            paths,
        }
    }

    fn accept_verified_install(
        &self,
        verified: VerifiedDownload,
        staged: SecureStagedInstaller,
    ) -> Result<(), &'static str> {
        let mut runtime = self
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        runtime.machine.finish_verification(&verified)?;
        runtime.verified_install = Some(VerifiedInstall {
            _staged_installer: staged,
            identity: verified,
        });
        Ok(())
    }

    #[cfg(test)]
    fn record_install_success(&self) -> Result<(), &'static str> {
        self.record_install_success_with(persist_installed_watermark)
    }

    #[cfg(test)]
    fn record_install_success_with(
        &self,
        persist: impl FnOnce(&Path, &InstalledWatermark) -> Result<(), &'static str>,
    ) -> Result<(), &'static str> {
        let path = self
            .paths
            .as_ref()
            .map(|paths| paths.installed_watermark.as_path())
            .ok_or("desktop_updater_state_path_unavailable")?;
        let mut runtime = self
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        let installed = runtime.machine.pending_install_watermark()?;
        if let Err(code) = persist(path, &installed) {
            runtime.machine.retry_after_install_persistence_failure()?;
            return Err(code);
        }
        runtime.machine.commit_install_success(&installed)?;
        Ok(())
    }
}

fn reconcile_persisted_state(paths: &UpdaterPaths) -> Result<ReconciledUpdaterState, &'static str> {
    reconcile_persisted_state_for(paths, CURRENT_VERSION, CURRENT_SOURCE_SHA)
}

fn reconcile_persisted_state_for(
    paths: &UpdaterPaths,
    current_version: &str,
    current_source_sha: &str,
) -> Result<ReconciledUpdaterState, &'static str> {
    let installed = updater_journal::load_installed_watermark(&paths.installed_watermark)?;
    let pending = updater_journal::load_pending_install(&paths.pending_install)?;
    let failed = updater_journal::load_failed_install(&paths.failed_install)?;
    let convert_installed = |watermark: updater_journal::InstalledWatermark| {
        let converted = InstalledWatermark {
            version: watermark.version,
            source_sha: watermark.source_sha,
            sha256: watermark.sha256,
        };
        validate_watermark(&converted, &parse_current_version(current_version)?)?;
        Ok::<InstalledWatermark, &'static str>(converted)
    };
    match updater_journal::reconcile_startup(
        current_version,
        current_source_sha,
        installed.as_ref(),
        pending.as_ref(),
    )? {
        StartupReconcile::NoPending => {
            let previous_install_failed = if let Some(failed) = failed.as_ref() {
                let unresolved = failed_install_is_unresolved(
                    current_version,
                    current_source_sha,
                    installed.as_ref(),
                    failed,
                )?;
                if !unresolved {
                    updater_journal::remove_failed_install(&paths.failed_install)?;
                }
                unresolved
            } else {
                false
            };
            Ok(ReconciledUpdaterState {
                installed: installed.map(convert_installed).transpose()?,
                previous_install_failed,
            })
        }
        StartupReconcile::CommitInstalled { watermark } => {
            updater_journal::persist_installed_watermark(&paths.installed_watermark, &watermark)?;
            updater_journal::remove_pending_install(&paths.pending_install)?;
            updater_journal::remove_failed_install(&paths.failed_install)?;
            Ok(ReconciledUpdaterState {
                installed: Some(convert_installed(watermark)?),
                previous_install_failed: false,
            })
        }
        StartupReconcile::PreviousInstallFailed { .. } => {
            let failed = pending
                .as_ref()
                .ok_or("desktop_updater_pending_identity_mismatch")?;
            updater_journal::persist_failed_install(&paths.failed_install, failed)?;
            updater_journal::remove_pending_install(&paths.pending_install)?;
            Ok(ReconciledUpdaterState {
                installed: installed.map(convert_installed).transpose()?,
                previous_install_failed: true,
            })
        }
    }
}

fn failed_install_is_unresolved(
    current_version: &str,
    current_source_sha: &str,
    installed: Option<&updater_journal::InstalledWatermark>,
    failed: &PendingInstall,
) -> Result<bool, &'static str> {
    let current = parse_current_version(current_version)?;
    let target = parse_exact_version(&failed.target_version)
        .map_err(|_| "desktop_updater_failed_invalid")?;
    if current == target {
        if current_source_sha != failed.target_source_sha {
            return Err("desktop_updater_failed_identity_mismatch");
        }
        return Ok(false);
    }
    if current > target {
        return Ok(false);
    }
    if let Some(installed) = installed {
        let installed_version = parse_exact_version(&installed.version)
            .map_err(|_| "desktop_updater_watermark_invalid")?;
        if installed_version == target
            && (installed.source_sha != failed.target_source_sha
                || installed.sha256 != failed.sha256)
        {
            return Err("desktop_updater_failed_identity_mismatch");
        }
        if installed_version >= target {
            return Ok(false);
        }
    }
    Ok(true)
}

pub fn setup(app: &mut App) {
    let paths = app.path().local_data_dir().ok().map(|root| {
        let updater = root.join("Stock Desk").join("v1.1").join("updater");
        UpdaterPaths {
            installed_watermark: updater.join(INSTALLED_WATERMARK_FILE),
            pending_install: updater.join(PENDING_INSTALL_FILE),
            failed_install: updater.join(FAILED_INSTALL_FILE),
            staging_directory: updater.join("staging"),
        }
    });
    let config = runtime_config().expect("source-bound updater configuration must be valid");
    let controller = DesktopUpdateController::new(config, paths);
    // Reconcile the durable install journal before touching launched staging
    // files.  This preserves the exact pending identity across a crash and
    // leaves cleanup as a strictly best-effort post-reconciliation action.
    if let Some(paths) = controller.paths.as_ref() {
        updater_windows::cleanup_staging_directory(&paths.staging_directory);
    }
    app.manage(controller);
}

pub(crate) fn cancel_verified_install(app: &AppHandle) {
    // The verified payload remains resident and the updater state remains
    // ReadyToInstall. Cancellation never falls through to install or exit.
    let _ = app.emit(
        UPDATE_EVENT,
        desktop_update_state(app.clone()).unwrap_or(DesktopUpdateState::Failed {
            current_version: CURRENT_VERSION.to_owned(),
            code: "desktop_updater_unavailable",
            can_retry: false,
        }),
    );
}

pub(crate) fn recover_verified_install(app: &AppHandle) {
    // A sidecar shutdown failure retains the exact verified capability for a
    // later user retry; it never launches the installer from a timeout path.
    cancel_verified_install(app);
}

pub(crate) fn launch_verified_install(app: &AppHandle) -> Result<(), &'static str> {
    launch_verified_install_inner(app)
}

fn launch_verified_install_inner(app: &AppHandle) -> Result<(), &'static str> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or("desktop_updater_unavailable")?;
    let paths = controller
        .paths
        .as_ref()
        .ok_or("desktop_updater_state_path_unavailable")?;
    let mut verified = {
        let mut runtime = controller
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        if runtime.verified_install.is_none() {
            return Err("desktop_updater_verified_identity_missing");
        }
        runtime.machine.begin_install()?;
        runtime
            .verified_install
            .take()
            .expect("verified install checked before transition")
    };

    let pending = PendingInstall::new(
        CURRENT_VERSION,
        CURRENT_SOURCE_SHA,
        &verified.identity.version,
        &verified.identity.source_sha,
        &verified.identity.sha256,
    )?;
    if let Err(code) = updater_journal::persist_pending_install(&paths.pending_install, &pending) {
        let mut runtime = controller
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        runtime.machine.retry_after_install_persistence_failure()?;
        runtime.verified_install = Some(verified);
        return Err(code);
    }

    if let Err(code) =
        updater_windows::launch_verified_installer(app, &mut verified._staged_installer)
    {
        let pending_removed = updater_journal::remove_pending_install(&paths.pending_install);
        let mut runtime = controller
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        if pending_removed.is_err() {
            runtime
                .machine
                .fail("desktop_updater_pending_repair_required", false);
            return Err("desktop_updater_pending_repair_required");
        }
        runtime.machine.retry_after_install_persistence_failure()?;
        runtime.verified_install = Some(verified);
        return Err(code);
    }
    Ok(())
}

#[tauri::command]
pub fn desktop_update_state(app: AppHandle) -> Result<DesktopUpdateState, String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    let state = controller
        .runtime
        .lock()
        .map_err(|_| "desktop_updater_unavailable".to_owned())?
        .machine
        .state()
        .clone();
    Ok(state)
}

#[tauri::command]
pub async fn desktop_check_for_updates(app: AppHandle) -> Result<DesktopUpdateState, String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    if !controller.config.enabled {
        return desktop_update_state(app);
    }
    mutate_and_emit(&app, |machine| {
        if matches!(
            machine.state(),
            DesktopUpdateState::Failed {
                can_retry: true,
                ..
            }
        ) {
            machine.recover_after_failed_check();
        }
        machine.begin_check()
    })?;

    let candidate = match fetch_release_candidate().await {
        Ok(Some(candidate)) => candidate,
        Ok(None) => {
            mutate_and_emit(&app, |machine| {
                machine.recover_after_failed_check();
                Ok(())
            })?;
            return desktop_update_state(app);
        }
        Err(code) => {
            let _ = fail_and_emit(&app, code, true);
            return Err(code.to_owned());
        }
    };
    mutate_and_emit(&app, |machine| machine.offer(candidate))?;
    desktop_update_state(app)
}

#[tauri::command]
pub fn desktop_dismiss_update(app: AppHandle) -> Result<(), String> {
    mutate_and_emit(&app, |machine| machine.dismiss())?;
    Ok(())
}

enum ConfirmedUpdateAction {
    Download(Box<ReleaseCandidate>),
    InstallVerified,
}

#[tauri::command]
pub async fn desktop_confirm_update(app: AppHandle) -> Result<(), String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    // A Web IPC call may request the host prompt, but it can never stand in for
    // the host-native user decision or construct NativeUpdateConsent itself.
    let consent = match gate_native_confirmation(controller.config.enabled, || {
        updater_windows::request_native_update_confirmation(&app)
    }) {
        Ok(consent) => consent,
        Err("desktop_updater_confirmation_cancelled") => return Ok(()),
        Err(code) => return Err(code.to_owned()),
    };
    let action = {
        let mut runtime = controller
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable".to_owned())?;
        if matches!(
            runtime.machine.state(),
            DesktopUpdateState::ReadyToInstall { .. }
        ) {
            let verified = runtime
                .verified_install
                .as_ref()
                .ok_or_else(|| "desktop_updater_verified_identity_missing".to_owned())?;
            if !runtime
                .machine
                .matches_verified_identity(&verified.identity)
            {
                runtime
                    .machine
                    .fail("desktop_updater_verified_identity_mismatch", false);
                return Err("desktop_updater_verified_identity_mismatch".to_owned());
            }
            ConfirmedUpdateAction::InstallVerified
        } else {
            confirm_download_after_native_consent(&mut runtime.machine, consent)
                .map_err(str::to_owned)?;
            let candidate = runtime
                .machine
                .candidate
                .clone()
                .ok_or_else(|| "desktop_updater_candidate_missing".to_owned())?;
            ConfirmedUpdateAction::Download(Box::new(candidate))
        }
    };
    let state = desktop_update_state(app.clone())?;
    let _ = app.emit(UPDATE_EVENT, state);
    let candidate = match action {
        ConfirmedUpdateAction::Download(candidate) => *candidate,
        ConfirmedUpdateAction::InstallVerified => {
            if let Err(code) = refresh_verified_authenticode(app.clone()).await {
                let _ = fail_and_emit(&app, "desktop_updater_verification_failed", false);
                return Err(code);
            }
            return crate::exit::begin_update_install(app).await;
        }
    };

    let bytes = match download_trusted_asset(&candidate).await {
        Ok(bytes) => bytes,
        Err(code) => {
            let _ = fail_and_emit(&app, "desktop_updater_signature_or_download_failed", true);
            return Err(code.to_owned());
        }
    };
    if updater_transport::validate_complete_body(RequestKind::Asset, bytes.len()).is_err()
        || !verify_sha256(&bytes, &candidate.sha256)
    {
        let _ = fail_and_emit(&app, "desktop_updater_sha256_mismatch", false);
        return Err("desktop_updater_sha256_mismatch".to_owned());
    }
    let public_key = controller
        .config
        .public_key
        .as_deref()
        .ok_or_else(|| "desktop_updater_public_key_not_configured".to_owned())?;
    if let Err(code) = verify_tauri_signature(&bytes, &candidate, public_key) {
        let _ = fail_and_emit(&app, code, false);
        return Err(code.to_owned());
    }
    mutate_and_emit(&app, TrustedUpdateMachine::begin_verification)?;

    let staging_directory = controller
        .paths
        .as_ref()
        .map(|paths| paths.staging_directory.clone())
        .ok_or_else(|| "desktop_updater_state_path_unavailable".to_owned())?;
    let expected_sha256 = candidate.sha256.clone();
    let staged = tauri::async_runtime::spawn_blocking(move || {
        let mut staged =
            updater_windows::stage_installer(&bytes, &staging_directory, &expected_sha256)?;
        updater_windows::verify_authenticode(&mut staged)?;
        Ok::<SecureStagedInstaller, &'static str>(staged)
    })
    .await
    .map_err(|_| "desktop_updater_verification_task_failed".to_owned())?
    .map_err(|code| code.to_owned());
    let staged = match staged {
        Ok(staged) => staged,
        Err(code) => {
            let stable = match code.as_str() {
                "desktop_updater_staging_failed" => "desktop_updater_staging_failed",
                "desktop_updater_sha256_mismatch" => "desktop_updater_sha256_mismatch",
                "desktop_updater_authenticode_rejected" => "desktop_updater_authenticode_rejected",
                _ => "desktop_updater_verification_failed",
            };
            let _ = fail_and_emit(&app, stable, false);
            return Err(code);
        }
    };
    let verified = VerifiedDownload {
        version: candidate.version,
        source_sha: candidate.source_sha,
        sha256: candidate.sha256,
        signature_sha256: sha256_hex(candidate.signature.as_bytes()),
    };
    if let Err(code) = controller.accept_verified_install(verified, staged) {
        let _ = fail_and_emit(&app, code, false);
        return Err(code.to_owned());
    }
    let state = desktop_update_state(app.clone())?;
    let _ = app.emit(UPDATE_EVENT, state);
    crate::exit::begin_update_install(app).await
}

async fn refresh_verified_authenticode(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let controller = app
            .try_state::<DesktopUpdateController>()
            .ok_or("desktop_updater_unavailable")?;
        let mut runtime = controller
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        let verified_identity_matches = runtime
            .verified_install
            .as_ref()
            .map(|verified| {
                runtime
                    .machine
                    .matches_verified_identity(&verified.identity)
            })
            .ok_or("desktop_updater_verified_identity_missing")?;
        if !verified_identity_matches {
            return Err("desktop_updater_verified_identity_mismatch");
        }
        let verified = runtime
            .verified_install
            .as_mut()
            .ok_or("desktop_updater_verified_identity_missing")?;
        // Refresh certificate-chain and revocation evidence before stopping
        // the sidecar. The later launch still rehashes and launches the same
        // identity-locked object without doing network work after shutdown.
        updater_windows::verify_authenticode(&mut verified._staged_installer)
    })
    .await
    .map_err(|_| "desktop_updater_verification_task_failed".to_owned())?
    .map_err(str::to_owned)
}

fn gate_native_confirmation<T>(
    enabled: bool,
    prompt: impl FnOnce() -> Result<T, &'static str>,
) -> Result<T, &'static str> {
    if !enabled {
        return Err("desktop_updater_disabled");
    }
    prompt()
}

fn confirm_download_after_native_consent(
    machine: &mut TrustedUpdateMachine,
    _consent: NativeUpdateConsent,
) -> Result<(), &'static str> {
    machine.confirm_download()
}

fn fail_and_emit(app: &AppHandle, code: &'static str, can_retry: bool) -> Result<(), String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    let state = {
        let mut runtime = controller
            .runtime
            .lock()
            .map_err(|_| "desktop_updater_unavailable".to_owned())?;
        runtime.machine.fail(code, can_retry);
        runtime.verified_install = None;
        runtime.machine.state().clone()
    };
    let _ = app.emit(UPDATE_EVENT, state);
    Ok(())
}

async fn fetch_release_candidate() -> Result<Option<ReleaseCandidate>, &'static str> {
    let (redirect_version, payload) = fetch_trusted_metadata().await?;
    bind_fetched_metadata(&redirect_version, &payload, CURRENT_VERSION)
}

fn bind_fetched_metadata(
    redirect_version: &str,
    payload: &[u8],
    current_version: &str,
) -> Result<Option<ReleaseCandidate>, &'static str> {
    let candidate = parse_trusted_updater_metadata(payload)?;
    if candidate.version != redirect_version {
        return Err("desktop_updater_metadata_identity_mismatch");
    }
    let offered = parse_exact_version(&candidate.version)
        .map_err(|_| "desktop_updater_release_version_invalid")?;
    let current = parse_current_version(current_version)?;
    if offered <= current {
        return Ok(None);
    }
    Ok(Some(candidate))
}

async fn fetch_trusted_metadata() -> Result<(String, Vec<u8>), &'static str> {
    let headers = updater_transport::anonymous_headers(RequestKind::Metadata);
    updater_transport::validate_anonymous_headers(RequestKind::Metadata, &headers)?;
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .default_headers(headers)
        .timeout(Duration::from_secs(15))
        .no_proxy()
        .build()
        .map_err(|_| "desktop_updater_unavailable")?;

    let first = client
        .get(UPDATE_ENDPOINT)
        .send()
        .await
        .map_err(|_| "desktop_updater_metadata_unavailable")?;
    let exact_url = redirect_location(&first)?;
    let version = metadata_version_from_url(&exact_url)?;
    updater_transport::validate_metadata_redirect(UPDATE_ENDPOINT, &exact_url, &version, 0)?;

    let second = client
        .get(&exact_url)
        .send()
        .await
        .map_err(|_| "desktop_updater_metadata_unavailable")?;
    let cdn_url = redirect_location(&second)?;
    updater_transport::validate_metadata_redirect(&exact_url, &cdn_url, &version, 1)?;

    let response = client
        .get(&cdn_url)
        .send()
        .await
        .map_err(|_| "desktop_updater_metadata_unavailable")?;
    let payload = read_bounded_response(
        response,
        RequestKind::Metadata,
        "desktop_updater_metadata_unavailable",
    )
    .await?;
    Ok((version, payload))
}

async fn download_trusted_asset(candidate: &ReleaseCandidate) -> Result<Vec<u8>, &'static str> {
    let headers = updater_transport::anonymous_headers(RequestKind::Asset);
    updater_transport::validate_anonymous_headers(RequestKind::Asset, &headers)?;
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .default_headers(headers)
        .timeout(Duration::from_secs(120))
        .no_proxy()
        .build()
        .map_err(|_| "desktop_updater_unavailable")?;
    let first = client
        .get(&candidate.download_url)
        .send()
        .await
        .map_err(|_| "desktop_updater_download_failed")?;
    let cdn_url = redirect_location_for_asset(&first)?;
    updater_transport::validate_asset_redirect(
        &candidate.download_url,
        &cdn_url,
        &candidate.version,
        0,
    )?;
    let response = client
        .get(&cdn_url)
        .send()
        .await
        .map_err(|_| "desktop_updater_download_failed")?;
    read_bounded_response(
        response,
        RequestKind::Asset,
        "desktop_updater_download_failed",
    )
    .await
}

async fn read_bounded_response(
    mut response: reqwest::Response,
    kind: RequestKind,
    unavailable: &'static str,
) -> Result<Vec<u8>, &'static str> {
    if !response.status().is_success() {
        return Err(unavailable);
    }
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok());
    updater_transport::validate_response_headers(kind, content_type, response.content_length())?;
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| unavailable)? {
        let length = updater_transport::checked_body_length(kind, bytes.len(), chunk.len())?;
        bytes.reserve(length.saturating_sub(bytes.len()));
        bytes.extend_from_slice(&chunk);
    }
    updater_transport::validate_complete_body(kind, bytes.len())?;
    Ok(bytes)
}

fn redirect_location(response: &reqwest::Response) -> Result<String, &'static str> {
    if !response.status().is_redirection() {
        return Err("desktop_updater_metadata_redirect_rejected");
    }
    let raw = response
        .headers()
        .get(reqwest::header::LOCATION)
        .and_then(|value| value.to_str().ok())
        .ok_or("desktop_updater_metadata_redirect_rejected")?;
    response
        .url()
        .join(raw)
        .map(|url| url.to_string())
        .map_err(|_| "desktop_updater_metadata_redirect_rejected")
}

fn redirect_location_for_asset(response: &reqwest::Response) -> Result<String, &'static str> {
    redirect_location(response).map_err(|_| "desktop_updater_asset_redirect_rejected")
}

fn metadata_version_from_url(value: &str) -> Result<String, &'static str> {
    let url = Url::parse(value).map_err(|_| "desktop_updater_metadata_redirect_rejected")?;
    let prefix = "/CongBao/stock-desk/releases/download/v";
    let suffix = "/latest.json";
    let version = url
        .path()
        .strip_prefix(prefix)
        .and_then(|path| path.strip_suffix(suffix))
        .ok_or("desktop_updater_metadata_redirect_rejected")?;
    parse_exact_version(version).map_err(|_| "desktop_updater_metadata_redirect_rejected")?;
    Ok(version.to_owned())
}

fn mutate_and_emit(
    app: &AppHandle,
    operation: impl FnOnce(&mut TrustedUpdateMachine) -> Result<(), &'static str>,
) -> Result<(), String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    let mut runtime = controller
        .runtime
        .lock()
        .map_err(|_| "desktop_updater_unavailable".to_owned())?;
    let result = operation(&mut runtime.machine).map_err(str::to_owned);
    let state = runtime.machine.state().clone();
    drop(runtime);
    let _ = app.emit(UPDATE_EVENT, state);
    result
}

fn validate_candidate(
    candidate: &ReleaseCandidate,
    current: &Version,
    installed_watermark: Option<&InstalledWatermark>,
) -> Result<Version, &'static str> {
    if candidate.channel != "stable" {
        return Err("desktop_updater_channel_rejected");
    }
    if candidate.target != UPDATE_TARGET || candidate.arch != UPDATE_ARCH {
        return Err("desktop_updater_platform_rejected");
    }
    let version = parse_exact_version(&candidate.version)
        .map_err(|_| "desktop_updater_release_version_invalid")?;
    if version <= *current {
        return Err("desktop_updater_downgrade_rejected");
    }
    if let Some(watermark) = installed_watermark {
        let watermark_version = parse_exact_version(&watermark.version)
            .map_err(|_| "desktop_updater_watermark_invalid")?;
        if version <= watermark_version {
            return Err("desktop_updater_replay_rejected");
        }
    }
    if !is_lower_hex(&candidate.sha256, 64) {
        return Err("desktop_updater_sha256_invalid");
    }
    if !is_lower_hex(&candidate.source_sha, 40) {
        return Err("desktop_updater_source_invalid");
    }
    validate_signature_text(&candidate.signature)?;
    validate_asset_url(&candidate.download_url, &candidate.version)?;
    Ok(version)
}

fn parse_trusted_updater_metadata(payload: &[u8]) -> Result<ReleaseCandidate, &'static str> {
    if payload.len() > MAX_METADATA_BYTES {
        return Err("desktop_updater_metadata_too_large");
    }
    let mut deserializer = serde_json::Deserializer::from_slice(payload);
    let metadata = TrustedUpdaterMetadata::deserialize(&mut deserializer)
        .map_err(|_| "desktop_updater_metadata_invalid")?;
    deserializer
        .end()
        .map_err(|_| "desktop_updater_metadata_invalid")?;
    if metadata.schema_version != TRUSTED_METADATA_SCHEMA {
        return Err("desktop_updater_schema_rejected");
    }
    let candidate = ReleaseCandidate {
        version: metadata.version,
        channel: metadata.channel,
        target: metadata.target,
        arch: metadata.arch,
        download_url: metadata.url,
        sha256: metadata.sha256,
        source_sha: metadata.source_sha,
        signature: metadata.signature,
        notes: None,
    };
    // Parser-level validation intentionally excludes only comparison with the
    // installed version/watermark, which remains the state machine's decision.
    if candidate.channel != "stable" {
        return Err("desktop_updater_channel_rejected");
    }
    if candidate.target != UPDATE_TARGET || candidate.arch != UPDATE_ARCH {
        return Err("desktop_updater_platform_rejected");
    }
    parse_exact_version(&candidate.version)
        .map_err(|_| "desktop_updater_release_version_invalid")?;
    if !is_lower_hex(&candidate.sha256, 64) {
        return Err("desktop_updater_sha256_invalid");
    }
    if !is_lower_hex(&candidate.source_sha, 40) {
        return Err("desktop_updater_source_invalid");
    }
    validate_signature_text(&candidate.signature)?;
    if candidate.download_url.len() > MAX_URL_BYTES {
        return Err("desktop_updater_asset_url_rejected");
    }
    validate_asset_url(&candidate.download_url, &candidate.version)?;
    Ok(candidate)
}

fn validate_signature_text(signature: &str) -> Result<(), &'static str> {
    if !(MIN_SIGNATURE_BYTES..=MAX_SIGNATURE_BYTES).contains(&signature.len())
        || signature.trim() != signature
        || signature.as_bytes().contains(&0)
    {
        return Err("desktop_updater_signature_invalid");
    }
    Ok(())
}

fn validate_asset_url(value: &str, version: &str) -> Result<(), &'static str> {
    let url = strict_https_url(value)?;
    let expected_path = format!(
        "/CongBao/stock-desk/releases/download/v{version}/stock-desk-{version}-windows-x64-setup.exe"
    );
    if url.host_str() != Some("github.com")
        || url.path() != expected_path
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("desktop_updater_asset_url_rejected");
    }
    Ok(())
}

fn parse_exact_version(value: &str) -> Result<Version, &'static str> {
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

fn validate_watermark(
    watermark: &InstalledWatermark,
    current: &Version,
) -> Result<(), &'static str> {
    let version =
        parse_exact_version(&watermark.version).map_err(|_| "desktop_updater_watermark_invalid")?;
    if version > *current
        || !is_lower_hex(&watermark.source_sha, 40)
        || !is_lower_hex(&watermark.sha256, 64)
    {
        return Err("desktop_updater_watermark_invalid");
    }
    Ok(())
}

#[cfg(test)]
fn load_installed_watermark(path: &Path) -> Result<Option<InstalledWatermark>, &'static str> {
    Ok(
        updater_journal::load_installed_watermark(path)?.map(|watermark| InstalledWatermark {
            version: watermark.version,
            source_sha: watermark.source_sha,
            sha256: watermark.sha256,
        }),
    )
}

#[cfg(test)]
fn persist_installed_watermark(
    path: &Path,
    watermark: &InstalledWatermark,
) -> Result<(), &'static str> {
    let journal = updater_journal::InstalledWatermark::new(
        &watermark.version,
        &watermark.source_sha,
        &watermark.sha256,
    )?;
    updater_journal::persist_installed_watermark(path, &journal)
}

fn verify_tauri_signature(
    payload: &[u8],
    candidate: &ReleaseCandidate,
    public_key_text: &str,
) -> Result<(), &'static str> {
    let public_key =
        PublicKey::decode(public_key_text).map_err(|_| "desktop_updater_public_key_invalid")?;
    let signature =
        Signature::decode(&candidate.signature).map_err(|_| "desktop_updater_signature_invalid")?;
    public_key
        .verify(payload, &signature, false)
        .map_err(|_| "desktop_updater_signature_invalid")?;
    Ok(())
}

fn strict_https_url(value: &str) -> Result<Url, &'static str> {
    let url = Url::parse(value).map_err(|_| "desktop_updater_url_invalid")?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
    {
        return Err("desktop_updater_url_rejected");
    }
    Ok(url)
}

pub fn sha256_hex(payload: &[u8]) -> String {
    let digest = Sha256::digest(payload);
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

pub fn verify_sha256(payload: &[u8], expected: &str) -> bool {
    is_lower_hex(expected, 64) && sha256_hex(payload).as_bytes() == expected.as_bytes()
}

fn is_lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn serve_once(response: Vec<u8>) -> (String, std::sync::mpsc::Receiver<String>) {
        use std::io::{Read as _, Write as _};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            while request.len() < 8 * 1024 {
                let read = stream.read(&mut buffer).unwrap_or(0);
                if read == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..read]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let _ = sender.send(String::from_utf8_lossy(&request).into_owned());
            stream.write_all(&response).unwrap();
        });
        (format!("http://{address}/payload"), receiver)
    }

    async fn request_local(url: &str, kind: RequestKind) -> reqwest::Response {
        let headers = updater_transport::anonymous_headers(kind);
        reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .default_headers(headers)
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap()
            .get(url)
            .send()
            .await
            .unwrap()
    }

    #[test]
    fn foundation_is_closed_and_uses_the_fixed_stable_windows_contract() {
        let config = runtime_config().unwrap();
        let machine = TrustedUpdateMachine::new("1.1.0", config.enabled, None).unwrap();
        assert!(matches!(
            machine.state(),
            DesktopUpdateState::Disabled { .. }
        ));
        assert!(!config.enabled);
        assert!(config.public_key.is_none());
        assert!(config.public_key_sha256.is_none());
        assert_eq!(UPDATE_TARGET, "windows-x86_64-nsis");
        assert_eq!(UPDATE_ARCH, "x86_64");
        assert_eq!(
            UPDATE_ENDPOINT,
            "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json"
        );
    }

    #[test]
    fn packaged_plugin_config_is_inert_and_deserializes_with_the_locked_plugin() {
        let application: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let updater = application
            .get("plugins")
            .and_then(|plugins| plugins.get("updater"))
            .cloned()
            .expect("packaged updater config must be explicit");
        let config: tauri_plugin_updater::Config = serde_json::from_value(updater).unwrap();

        assert!(config.endpoints.is_empty());
        assert!(config.pubkey.is_empty());
        assert!(!config.dangerous_insecure_transport_protocol);
        assert!(!config.dangerous_accept_invalid_certs);
        assert!(!config.dangerous_accept_invalid_hostnames);
    }

    #[test]
    fn disabled_confirmation_gate_has_no_prompt_or_state_side_effect() {
        let mut prompt_called = false;
        let result = gate_native_confirmation(runtime_config().unwrap().enabled, || {
            prompt_called = true;
            Ok(())
        });
        assert_eq!(result.unwrap_err(), "desktop_updater_disabled");
        assert!(!prompt_called);
    }

    #[test]
    fn custom_metadata_is_strict_and_binds_signature_to_candidate_identity() {
        let payload = valid_metadata();
        let candidate = parse_trusted_updater_metadata(payload.as_bytes()).unwrap();
        assert_eq!(candidate.version, "1.2.0");
        assert_eq!(candidate.signature, candidate_signature());

        let unknown = payload.replacen(
            r#"{"schema_version"#,
            r#"{"claimed_verified":true,"schema_version"#,
            1,
        );
        assert_eq!(
            parse_trusted_updater_metadata(unknown.as_bytes()).unwrap_err(),
            "desktop_updater_metadata_invalid"
        );
        let missing = payload.replace(r#""arch":"x86_64","#, "");
        assert_eq!(
            parse_trusted_updater_metadata(missing.as_bytes()).unwrap_err(),
            "desktop_updater_metadata_invalid"
        );
        let duplicate = payload.replace(
            r#""channel":"stable""#,
            r#""channel":"stable","channel":"stable""#,
        );
        assert_eq!(
            parse_trusted_updater_metadata(duplicate.as_bytes()).unwrap_err(),
            "desktop_updater_metadata_invalid"
        );
        assert_eq!(
            parse_trusted_updater_metadata(format!("{payload} true").as_bytes()).unwrap_err(),
            "desktop_updater_metadata_invalid"
        );
        assert_eq!(
            parse_trusted_updater_metadata(&vec![b' '; MAX_METADATA_BYTES + 1]).unwrap_err(),
            "desktop_updater_metadata_too_large"
        );
    }

    #[test]
    fn immutable_redirect_version_is_bound_to_body_and_current_version() {
        let payload = valid_metadata();
        assert_eq!(
            bind_fetched_metadata("1.3.0", payload.as_bytes(), "1.1.0").unwrap_err(),
            "desktop_updater_metadata_identity_mismatch"
        );
        assert_eq!(
            bind_fetched_metadata("1.2.0", payload.as_bytes(), "1.2.0").unwrap(),
            None
        );
        assert_eq!(
            bind_fetched_metadata("1.2.0", payload.as_bytes(), "1.1.0")
                .unwrap()
                .expect("newer exact metadata")
                .version,
            "1.2.0"
        );
    }

    #[test]
    fn real_async_response_path_enforces_headers_stream_limits_and_anonymity() {
        tauri::async_runtime::block_on(async {
            let valid = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}".to_vec();
            let (url, request) = serve_once(valid);
            let response = request_local(&url, RequestKind::Metadata).await;
            assert_eq!(
                read_bounded_response(
                    response,
                    RequestKind::Metadata,
                    "desktop_updater_metadata_unavailable"
                )
                .await
                .unwrap(),
                b"{}"
            );
            let observed = request.recv_timeout(Duration::from_secs(5)).unwrap();
            let observed_lower = observed.to_ascii_lowercase();
            assert!(observed_lower.contains("accept-encoding: identity\r\n"));
            assert!(observed_lower.contains("user-agent: stock-desk-updater\r\n"));
            for forbidden in ["authorization:", "cookie:", "referer:", "x-device"] {
                assert!(!observed_lower.contains(forbidden));
            }

            let oversized_body = vec![b'a'; updater_transport::MAX_METADATA_BYTES + 1];
            let mut chunked = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:x}\r\n",
                oversized_body.len()
            )
            .into_bytes();
            chunked.extend_from_slice(&oversized_body);
            chunked.extend_from_slice(b"\r\n0\r\n\r\n");
            let (url, _) = serve_once(chunked);
            let response = request_local(&url, RequestKind::Metadata).await;
            assert_eq!(
                read_bounded_response(
                    response,
                    RequestKind::Metadata,
                    "desktop_updater_metadata_unavailable"
                )
                .await
                .unwrap_err(),
                "desktop_updater_response_size_rejected"
            );

            let invalid_type = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}".to_vec();
            let (url, _) = serve_once(invalid_type);
            let response = request_local(&url, RequestKind::Metadata).await;
            assert_eq!(
                read_bounded_response(
                    response,
                    RequestKind::Metadata,
                    "desktop_updater_metadata_unavailable"
                )
                .await
                .unwrap_err(),
                "desktop_updater_content_type_rejected"
            );
        });
    }

    #[test]
    fn custom_metadata_rejects_nonstable_platform_digest_and_repository_values() {
        for (needle, replacement, expected) in [
            (
                r#""channel":"stable""#.to_owned(),
                r#""channel":"beta""#.to_owned(),
                "desktop_updater_channel_rejected",
            ),
            (
                r#""version":"1.2.0""#.to_owned(),
                r#""version":"1.2.0-beta.1""#.to_owned(),
                "desktop_updater_release_version_invalid",
            ),
            (
                r#""arch":"x86_64""#.to_owned(),
                r#""arch":"aarch64""#.to_owned(),
                "desktop_updater_platform_rejected",
            ),
            (
                r#""target":"windows-x86_64-nsis""#.to_owned(),
                r#""target":"windows-aarch64-nsis""#.to_owned(),
                "desktop_updater_platform_rejected",
            ),
            (
                format!(r#""source_sha":"{}""#, "b".repeat(40)),
                r#""source_sha":"BBBB""#.to_owned(),
                "desktop_updater_source_invalid",
            ),
            (
                format!(r#""sha256":"{}""#, "a".repeat(64)),
                r#""sha256":"AAAA""#.to_owned(),
                "desktop_updater_sha256_invalid",
            ),
            (
                "github.com/CongBao/stock-desk".to_owned(),
                "example.com/CongBao/stock-desk".to_owned(),
                "desktop_updater_asset_url_rejected",
            ),
            (
                "windows-x64-setup.exe".to_owned(),
                "windows-x64-setup.exe?mirror=1".to_owned(),
                "desktop_updater_asset_url_rejected",
            ),
        ] {
            let mutated = valid_metadata().replace(&needle, &replacement);
            assert_eq!(
                parse_trusted_updater_metadata(mutated.as_bytes()).unwrap_err(),
                expected
            );
        }
    }

    #[test]
    fn unverified_higher_offer_does_not_advance_the_watermark() {
        let mut machine = TrustedUpdateMachine::new("1.1.0", true, None).unwrap();
        machine.begin_check().unwrap();
        machine
            .offer(candidate("9.0.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .unwrap();
        machine.dismiss().unwrap();
        assert!(machine.installed_watermark().is_none());
        machine.begin_check().unwrap();
        machine
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .unwrap();
    }

    #[test]
    fn persisted_installed_watermark_rejects_replay_after_restart() {
        let watermark = InstalledWatermark {
            version: "1.2.0".to_owned(),
            source_sha: "b".repeat(40),
            sha256: "a".repeat(64),
        };
        for version in ["1.1.1", "1.2.0"] {
            let mut machine =
                TrustedUpdateMachine::new("1.2.0", true, Some(watermark.clone())).unwrap();
            machine.begin_check().unwrap();
            assert!(machine
                .offer(candidate(version, "stable", UPDATE_TARGET, UPDATE_ARCH))
                .is_err());
        }
    }

    #[test]
    fn verified_but_failed_install_can_retry_the_exact_same_identity() {
        let mut machine = available_machine();
        machine.confirm_download().unwrap();
        machine.begin_verification().unwrap();
        let verified = VerifiedDownload {
            version: "1.2.0".to_owned(),
            source_sha: "b".repeat(40),
            sha256: "a".repeat(64),
            signature_sha256: sha256_hex(candidate_signature().as_bytes()),
        };
        machine.finish_verification(&verified).unwrap();
        assert!(machine.installed_watermark().is_none());
        machine.begin_install().unwrap();
        machine.fail("desktop_updater_install_failed", true);
        machine.recover_after_failed_check();
        machine.begin_check().unwrap();
        machine
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .unwrap();
    }

    #[test]
    fn only_confirmed_install_advances_the_rollback_watermark() {
        let mut machine = available_machine();
        machine.confirm_download().unwrap();
        machine.begin_verification().unwrap();
        let verified = VerifiedDownload {
            version: "1.2.0".to_owned(),
            source_sha: "b".repeat(40),
            sha256: "a".repeat(64),
            signature_sha256: sha256_hex(candidate_signature().as_bytes()),
        };
        machine.finish_verification(&verified).unwrap();
        machine.begin_install().unwrap();
        let installed = machine.pending_install_watermark().unwrap();
        machine.commit_install_success(&installed).unwrap();
        assert_eq!(machine.installed_watermark(), Some(&installed));

        let mut restarted = TrustedUpdateMachine::new("1.2.0", true, Some(installed)).unwrap();
        restarted.begin_check().unwrap();
        assert!(restarted
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .is_err());
    }

    #[test]
    fn unavailable_local_data_path_never_creates_relative_updater_state() {
        let controller = DesktopUpdateController::new(runtime_config().unwrap(), None);
        assert!(controller.paths.is_none());
        assert!(matches!(
            controller.runtime.lock().unwrap().machine.state(),
            DesktopUpdateState::Disabled { .. }
        ));
        assert_eq!(
            controller.record_install_success().unwrap_err(),
            "desktop_updater_state_path_unavailable"
        );
    }

    #[test]
    fn controller_persistence_failure_keeps_old_watermark_and_retryable_pending_install() {
        let old = InstalledWatermark {
            version: "1.1.5".to_owned(),
            source_sha: "c".repeat(40),
            sha256: "d".repeat(64),
        };
        let root = std::env::temp_dir().join(format!(
            "stock-desk-updater-controller-failure-{}",
            std::process::id()
        ));
        let path = root.join("installed-watermark.json");
        persist_installed_watermark(&path, &old).unwrap();
        let mut machine = TrustedUpdateMachine::new("1.1.5", true, Some(old.clone())).unwrap();
        machine.begin_check().unwrap();
        machine
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .unwrap();
        advance_to_installing(&mut machine);
        let controller = DesktopUpdateController {
            runtime: Mutex::new(UpdaterRuntimeState {
                machine,
                verified_install: None,
            }),
            config: runtime_config().unwrap(),
            paths: Some(test_paths(&root)),
        };

        assert_eq!(
            controller
                .record_install_success_with(|_, _| { Err("desktop_updater_watermark_unwritable") })
                .unwrap_err(),
            "desktop_updater_watermark_unwritable"
        );
        let mut runtime = controller.runtime.lock().unwrap();
        assert_eq!(runtime.machine.installed_watermark(), Some(&old));
        assert!(matches!(
            runtime.machine.state(),
            DesktopUpdateState::ReadyToInstall { version, .. } if version == "1.2.0"
        ));
        runtime.machine.begin_install().unwrap();
        drop(runtime);
        assert_eq!(load_installed_watermark(&path).unwrap(), Some(old));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn controller_commits_only_after_durable_watermark_and_real_reload_rejects_replay() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-updater-controller-success-{}",
            std::process::id()
        ));
        let path = root.join("installed-watermark.json");
        let mut machine = available_machine();
        advance_to_installing(&mut machine);
        let controller = DesktopUpdateController {
            runtime: Mutex::new(UpdaterRuntimeState {
                machine,
                verified_install: None,
            }),
            config: runtime_config().unwrap(),
            paths: Some(test_paths(&root)),
        };

        controller.record_install_success().unwrap();
        let reloaded = load_installed_watermark(&path).unwrap().unwrap();
        assert_eq!(
            controller
                .runtime
                .lock()
                .unwrap()
                .machine
                .installed_watermark(),
            Some(&reloaded)
        );
        let mut restarted = TrustedUpdateMachine::new("1.2.0", true, Some(reloaded)).unwrap();
        restarted.begin_check().unwrap();
        assert!(restarted
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn production_reconciliation_closes_failure_and_commit_crash_windows() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-updater-reconcile-{}-{}",
            std::process::id(),
            getrandom::u64().unwrap()
        ));
        let paths = test_paths(&root);
        let pending = PendingInstall::new(
            "1.1.0",
            "a".repeat(40),
            "1.2.0",
            "b".repeat(40),
            "c".repeat(64),
        )
        .unwrap();
        updater_journal::persist_pending_install(&paths.pending_install, &pending).unwrap();

        let failed = reconcile_persisted_state_for(&paths, "1.1.0", &"a".repeat(40)).unwrap();
        assert!(failed.previous_install_failed);
        assert!(failed.installed.is_none());
        assert_eq!(
            updater_journal::load_pending_install(&paths.pending_install).unwrap(),
            None
        );
        assert_eq!(
            updater_journal::load_failed_install(&paths.failed_install).unwrap(),
            Some(pending.clone())
        );

        let repeated = reconcile_persisted_state_for(&paths, "1.1.0", &"a".repeat(40)).unwrap();
        assert!(repeated.previous_install_failed);
        assert!(repeated.installed.is_none());

        // Simulate a later successful launch of the exact pending target after
        // a crash between process hand-off and watermark commit.
        updater_journal::persist_pending_install(&paths.pending_install, &pending).unwrap();
        let committed = reconcile_persisted_state_for(&paths, "1.2.0", &"b".repeat(40)).unwrap();
        assert!(!committed.previous_install_failed);
        assert_eq!(committed.installed.unwrap().version, "1.2.0");
        assert_eq!(
            updater_journal::load_pending_install(&paths.pending_install).unwrap(),
            None
        );
        assert_eq!(
            updater_journal::load_failed_install(&paths.failed_install).unwrap(),
            None
        );
        assert_eq!(
            updater_journal::load_installed_watermark(&paths.installed_watermark)
                .unwrap()
                .unwrap()
                .version,
            "1.2.0"
        );

        let idempotent = reconcile_persisted_state_for(&paths, "1.2.0", &"b".repeat(40)).unwrap();
        assert!(!idempotent.previous_install_failed);
        assert_eq!(idempotent.installed.unwrap().version, "1.2.0");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn failed_install_marker_cannot_claim_a_different_installed_binary() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-updater-failed-identity-{}-{}",
            std::process::id(),
            getrandom::u64().unwrap()
        ));
        let paths = test_paths(&root);
        let failed = PendingInstall::new(
            "1.1.0",
            "a".repeat(40),
            "1.2.0",
            "b".repeat(40),
            "c".repeat(64),
        )
        .unwrap();
        updater_journal::persist_failed_install(&paths.failed_install, &failed).unwrap();
        assert_eq!(
            reconcile_persisted_state_for(&paths, "1.2.0", &"d".repeat(40)).unwrap_err(),
            "desktop_updater_failed_identity_mismatch"
        );
        assert_eq!(
            updater_journal::load_failed_install(&paths.failed_install).unwrap(),
            Some(failed)
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn exact_stable_version_architecture_and_channel_are_mandatory() {
        for (version, channel, target, arch) in [
            ("1.2.0-beta.1", "stable", UPDATE_TARGET, UPDATE_ARCH),
            ("1.2.0+build.1", "stable", UPDATE_TARGET, UPDATE_ARCH),
            ("1.2.0", "beta", UPDATE_TARGET, UPDATE_ARCH),
            ("1.2.0", "stable", "windows-aarch64-nsis", UPDATE_ARCH),
            ("1.2.0", "stable", UPDATE_TARGET, "aarch64"),
        ] {
            let mut machine = TrustedUpdateMachine::new("1.1.0", true, None).unwrap();
            machine.begin_check().unwrap();
            assert!(machine
                .offer(candidate(version, channel, target, arch))
                .is_err());
        }
    }

    #[test]
    fn verification_requires_actual_payload_and_cannot_accept_claimed_success() {
        let candidate = candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH);
        assert!(!verify_sha256(
            b"not the expected installer",
            &candidate.sha256
        ));
    }

    #[test]
    fn watermark_round_trip_is_exact_and_rejects_expansion() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-updater-watermark-{}",
            std::process::id()
        ));
        let path = root.join("installed-watermark.json");
        let watermark = InstalledWatermark {
            version: "1.2.0".to_owned(),
            source_sha: "b".repeat(40),
            sha256: "a".repeat(64),
        };
        persist_installed_watermark(&path, &watermark).unwrap();
        assert_eq!(load_installed_watermark(&path).unwrap(), Some(watermark));
        let replacement = InstalledWatermark {
            version: "1.3.0".to_owned(),
            source_sha: "c".repeat(40),
            sha256: "d".repeat(64),
        };
        persist_installed_watermark(&path, &replacement).unwrap();
        assert_eq!(load_installed_watermark(&path).unwrap(), Some(replacement));
        fs::write(
            &path,
            r#"{"version":"1.2.0","source_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","claimed_verified":true}"#,
        )
        .unwrap();
        assert!(load_installed_watermark(&path).is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn sha256_is_checked_over_the_exact_downloaded_bytes() {
        let bytes = b"signed updater payload";
        let digest = sha256_hex(bytes);
        assert!(verify_sha256(bytes, &digest));
        assert!(!verify_sha256(b"tampered updater payload", &digest));
    }

    #[test]
    fn failures_keep_the_current_version_authoritative() {
        let mut machine = available_machine();
        machine.confirm_download().unwrap();
        machine.begin_verification().unwrap();
        machine.fail("desktop_updater_sha256_mismatch", true);
        assert!(matches!(
            machine.state(),
            DesktopUpdateState::Failed {
                current_version,
                code: "desktop_updater_sha256_mismatch",
                can_retry: true,
            } if current_version == "1.1.0"
        ));
    }

    fn candidate(version: &str, channel: &str, target: &str, arch: &str) -> ReleaseCandidate {
        ReleaseCandidate {
            version: version.to_owned(),
            channel: channel.to_owned(),
            target: target.to_owned(),
            arch: arch.to_owned(),
            download_url: format!(
                "https://github.com/CongBao/stock-desk/releases/download/v{version}/stock-desk-{version}-windows-x64-setup.exe"
            ),
            sha256: "a".repeat(64),
            source_sha: "b".repeat(40),
            signature: candidate_signature(),
            notes: Some("Security update".to_owned()),
        }
    }

    fn test_paths(root: &Path) -> UpdaterPaths {
        UpdaterPaths {
            installed_watermark: root.join(INSTALLED_WATERMARK_FILE),
            pending_install: root.join(PENDING_INSTALL_FILE),
            failed_install: root.join(FAILED_INSTALL_FILE),
            staging_directory: root.join("staging"),
        }
    }

    fn advance_to_installing(machine: &mut TrustedUpdateMachine) {
        machine.confirm_download().unwrap();
        machine.begin_verification().unwrap();
        machine
            .finish_verification(&VerifiedDownload {
                version: "1.2.0".to_owned(),
                source_sha: "b".repeat(40),
                sha256: "a".repeat(64),
                signature_sha256: sha256_hex(candidate_signature().as_bytes()),
            })
            .unwrap();
        machine.begin_install().unwrap();
    }

    fn available_machine() -> TrustedUpdateMachine {
        let mut machine = TrustedUpdateMachine::new("1.1.0", true, None).unwrap();
        machine.begin_check().unwrap();
        machine
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .unwrap();
        machine
    }

    fn candidate_signature() -> String {
        "untrusted-signature-bound-to-candidate".to_owned()
    }

    fn valid_metadata() -> String {
        format!(
            r#"{{"schema_version":"{TRUSTED_METADATA_SCHEMA}","channel":"stable","version":"1.2.0","target":"{UPDATE_TARGET}","arch":"{UPDATE_ARCH}","source_sha":"{}","url":"https://github.com/CongBao/stock-desk/releases/download/v1.2.0/stock-desk-1.2.0-windows-x64-setup.exe","sha256":"{}","signature":"{}"}}"#,
            "b".repeat(40),
            "a".repeat(64),
            candidate_signature()
        )
    }
}
