//! Trusted updater foundation.
//!
//! Stock Desk's release service uses a repository-specific metadata envelope;
//! it is not the JSON format consumed directly by the official Tauri updater
//! plugin. A future activation must have the Rust host fetch and strictly parse
//! that envelope, bind its signature to the candidate, and complete SHA-256,
//! Minisign/Ed25519, and Authenticode verification before invoking the plugin's
//! installation path. Network checks and installs intentionally remain disabled
//! in this stage. Formal activation additionally requires the checked-in public
//! key, protected private-key signing, SignPath evidence, and fresh Windows
//! 10/11 installation evidence.

// The closed foundation deliberately compiles the future state transitions
// without making them reachable from production commands. Tests exercise the
// complete pure machine until formal network and install activation lands.
#![allow(dead_code)]

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use minisign_verify::{PublicKey, Signature};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{App, AppHandle, Emitter, Manager};
use url::Url;

pub const UPDATE_ENDPOINT: &str =
    "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json";
pub const UPDATE_TARGET: &str = "windows-x86_64-nsis";
pub const UPDATE_ARCH: &str = "x86_64";
pub const UPDATE_RUNTIME_ENABLED: bool = false;

const CURRENT_VERSION: &str = env!("CARGO_PKG_VERSION");
const UPDATE_EVENT: &str = "desktop-update-state";
const REPOSITORY_RELEASE_PREFIX: &str = "/CongBao/stock-desk/releases/download/v";
const TRUSTED_METADATA_SCHEMA: &str = "stock-desk-trusted-updater-v1";
const MAX_METADATA_BYTES: usize = 32 * 1024;
const MAX_URL_BYTES: usize = 512;
const MIN_SIGNATURE_BYTES: usize = 16;
const MAX_SIGNATURE_BYTES: usize = 16 * 1024;
// No production updater key has been approved yet. Formal activation must
// replace this with a source-bound include_str! of the reviewed public key.
const TRUSTED_TAURI_PUBLIC_KEY: Option<&str> = None;
const _: () = assert!(!UPDATE_RUNTIME_ENABLED);

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

// This capability is deliberately private, non-serializable, and impossible to
// construct from a WebView command argument. Formal activation must mint it only
// after the desktop host's native confirmation UI returns an affirmative result.
#[derive(Debug)]
struct NativeUpdateConsent(());

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

pub struct DesktopUpdateController {
    machine: Mutex<TrustedUpdateMachine>,
    installed_watermark_path: Option<PathBuf>,
}

pub fn plugin<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R, tauri_plugin_updater::Config> {
    // Registration does not make the plugin a parser for Stock Desk's custom
    // metadata envelope and does not activate its network/install path.
    tauri_plugin_updater::Builder::new().build()
}

impl DesktopUpdateController {
    fn new(installed_watermark_path: Option<PathBuf>) -> Self {
        let persisted = installed_watermark_path
            .as_deref()
            .map(load_installed_watermark)
            .unwrap_or(Err("desktop_updater_state_path_unavailable"));
        let enabled =
            UPDATE_RUNTIME_ENABLED && installed_watermark_path.is_some() && persisted.is_ok();
        Self {
            machine: Mutex::new(
                TrustedUpdateMachine::new(CURRENT_VERSION, enabled, persisted.unwrap_or(None))
                    .expect("Cargo package version must be valid SemVer"),
            ),
            installed_watermark_path,
        }
    }

    fn accept_verified_download(&self, verified: &VerifiedDownload) -> Result<(), &'static str> {
        let mut machine = self
            .machine
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        machine.finish_verification(verified)?;
        Ok(())
    }

    fn record_install_success(&self) -> Result<(), &'static str> {
        self.record_install_success_with(persist_installed_watermark)
    }

    fn record_install_success_with(
        &self,
        persist: impl FnOnce(&Path, &InstalledWatermark) -> Result<(), &'static str>,
    ) -> Result<(), &'static str> {
        let path = self
            .installed_watermark_path
            .as_deref()
            .ok_or("desktop_updater_state_path_unavailable")?;
        let mut machine = self
            .machine
            .lock()
            .map_err(|_| "desktop_updater_unavailable")?;
        let installed = machine.pending_install_watermark()?;
        if let Err(code) = persist(path, &installed) {
            machine.retry_after_install_persistence_failure()?;
            return Err(code);
        }
        machine.commit_install_success(&installed)?;
        Ok(())
    }
}

pub fn setup(app: &mut App) {
    let installed_watermark_path = app.path().local_data_dir().ok().map(|root| {
        root.join("Stock Desk")
            .join("v1.1")
            .join("updater")
            .join("installed-watermark.json")
    });
    app.manage(DesktopUpdateController::new(installed_watermark_path));
}

#[tauri::command]
pub fn desktop_update_state(app: AppHandle) -> Result<DesktopUpdateState, String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    let state = controller
        .machine
        .lock()
        .map_err(|_| "desktop_updater_unavailable".to_owned())?
        .state()
        .clone();
    Ok(state)
}

#[tauri::command]
pub fn desktop_check_for_updates(app: AppHandle) -> Result<DesktopUpdateState, String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    let machine = controller
        .machine
        .lock()
        .map_err(|_| "desktop_updater_unavailable".to_owned())?;
    // No request is made in the foundation stage. Activation must replace this
    // closed branch with the reviewed endpoint client and preserve this exact
    // state machine; merely changing UPDATE_RUNTIME_ENABLED is insufficient.
    if !UPDATE_RUNTIME_ENABLED {
        return Ok(machine.state().clone());
    }
    Err("desktop_updater_trust_not_activated".to_owned())
}

#[tauri::command]
pub fn desktop_dismiss_update(app: AppHandle) -> Result<(), String> {
    mutate_and_emit(&app, |machine| machine.dismiss())
}

#[tauri::command]
pub fn desktop_confirm_update(app: AppHandle) -> Result<(), String> {
    // A Web IPC call may request the host prompt, but it can never stand in for
    // the host-native user decision or construct NativeUpdateConsent itself.
    let consent = gate_native_confirmation(UPDATE_RUNTIME_ENABLED, || {
        request_host_native_confirmation()
    })
    .map_err(str::to_owned)?;
    mutate_and_emit(&app, |machine| {
        confirm_download_after_native_consent(machine, consent)?;
        machine.fail("desktop_updater_trust_not_activated", false);
        Err("desktop_updater_trust_not_activated")
    })
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

fn request_host_native_confirmation() -> Result<NativeUpdateConsent, &'static str> {
    // Formal activation must replace this fail-closed stub with a host-owned
    // native dialog. No command parameter may be converted into this token.
    Err("desktop_updater_native_confirmation_not_integrated")
}

fn confirm_download_after_native_consent(
    machine: &mut TrustedUpdateMachine,
    _consent: NativeUpdateConsent,
) -> Result<(), &'static str> {
    machine.confirm_download()
}

fn mutate_and_emit(
    app: &AppHandle,
    operation: impl FnOnce(&mut TrustedUpdateMachine) -> Result<(), &'static str>,
) -> Result<(), String> {
    let controller = app
        .try_state::<DesktopUpdateController>()
        .ok_or_else(|| "desktop_updater_unavailable".to_owned())?;
    let mut machine = controller
        .machine
        .lock()
        .map_err(|_| "desktop_updater_unavailable".to_owned())?;
    let result = operation(&mut machine).map_err(str::to_owned);
    let state = machine.state().clone();
    drop(machine);
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

pub fn validate_redirect(
    from: &str,
    to: &str,
    version: &str,
    hop: usize,
) -> Result<(), &'static str> {
    parse_exact_version(version).map_err(|_| "desktop_updater_redirect_rejected")?;
    if hop != 0 || from != UPDATE_ENDPOINT {
        return Err("desktop_updater_redirect_rejected");
    }
    let target = strict_https_url(to)?;
    let expected_path = format!("{REPOSITORY_RELEASE_PREFIX}{version}/latest.json");
    if target.host_str() != Some("github.com")
        || target.path() != expected_path
        || target.query().is_some()
        || target.fragment().is_some()
    {
        return Err("desktop_updater_redirect_rejected");
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
    if version < *current
        || !is_lower_hex(&watermark.source_sha, 40)
        || !is_lower_hex(&watermark.sha256, 64)
    {
        return Err("desktop_updater_watermark_invalid");
    }
    Ok(())
}

fn load_installed_watermark(path: &Path) -> Result<Option<InstalledWatermark>, &'static str> {
    let payload = match fs::read(path) {
        Ok(payload) => payload,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("desktop_updater_watermark_unreadable"),
    };
    if payload.len() > 4096 {
        return Err("desktop_updater_watermark_invalid");
    }
    let watermark: InstalledWatermark =
        serde_json::from_slice(&payload).map_err(|_| "desktop_updater_watermark_invalid")?;
    let current = parse_current_version(CURRENT_VERSION)?;
    validate_watermark(&watermark, &current)?;
    Ok(Some(watermark))
}

fn persist_installed_watermark(
    path: &Path,
    watermark: &InstalledWatermark,
) -> Result<(), &'static str> {
    let parent = path
        .parent()
        .ok_or("desktop_updater_watermark_unwritable")?;
    fs::create_dir_all(parent).map_err(|_| "desktop_updater_watermark_unwritable")?;
    let temporary = path.with_extension("json.tmp");
    let payload =
        serde_json::to_vec(watermark).map_err(|_| "desktop_updater_watermark_unwritable")?;
    let mut file =
        fs::File::create(&temporary).map_err(|_| "desktop_updater_watermark_unwritable")?;
    file.write_all(&payload)
        .and_then(|()| file.sync_all())
        .map_err(|_| "desktop_updater_watermark_unwritable")?;
    drop(file);
    replace_watermark(&temporary, path)
}

#[cfg(not(windows))]
fn replace_watermark(temporary: &Path, path: &Path) -> Result<(), &'static str> {
    fs::rename(temporary, path).map_err(|_| "desktop_updater_watermark_unwritable")?;
    let parent = path
        .parent()
        .ok_or("desktop_updater_watermark_unwritable")?;
    fs::File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| "desktop_updater_watermark_unwritable")
}

#[cfg(windows)]
fn replace_watermark(temporary: &Path, path: &Path) -> Result<(), &'static str> {
    use std::os::windows::ffi::OsStrExt as _;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    fn wide(path: &Path) -> Vec<u16> {
        path.as_os_str().encode_wide().chain(Some(0)).collect()
    }

    let temporary = wide(temporary);
    let destination = wide(path);
    // SAFETY: both buffers are NUL-terminated and live through the call. The
    // destination is the fixed same-parent updater watermark path.
    let replaced = unsafe {
        MoveFileExW(
            temporary.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if replaced == 0 {
        return Err("desktop_updater_watermark_unwritable");
    }
    Ok(())
}

fn verify_downloaded_candidate(
    candidate: &ReleaseCandidate,
    installer_path: &Path,
) -> Result<VerifiedDownload, &'static str> {
    let payload = fs::read(installer_path).map_err(|_| "desktop_updater_payload_unreadable")?;
    if !verify_sha256(&payload, &candidate.sha256) {
        return Err("desktop_updater_sha256_mismatch");
    }
    let public_key_text =
        TRUSTED_TAURI_PUBLIC_KEY.ok_or("desktop_updater_public_key_not_configured")?;
    let public_key =
        PublicKey::decode(public_key_text).map_err(|_| "desktop_updater_public_key_invalid")?;
    let signature =
        Signature::decode(&candidate.signature).map_err(|_| "desktop_updater_signature_invalid")?;
    public_key
        .verify(&payload, &signature, false)
        .map_err(|_| "desktop_updater_signature_invalid")?;
    verify_authenticode(installer_path)?;
    Ok(VerifiedDownload {
        version: candidate.version.clone(),
        source_sha: candidate.source_sha.clone(),
        sha256: candidate.sha256.clone(),
        signature_sha256: sha256_hex(candidate.signature.as_bytes()),
    })
}

#[cfg(not(windows))]
fn verify_authenticode(_installer_path: &Path) -> Result<(), &'static str> {
    Err("desktop_updater_winverifytrust_unavailable")
}

#[cfg(windows)]
fn verify_authenticode(_installer_path: &Path) -> Result<(), &'static str> {
    // Runtime activation is forbidden until a reviewed WinVerifyTrust binding
    // lands. This cannot be replaced with a caller-provided success flag.
    Err("desktop_updater_winverifytrust_not_integrated")
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

    #[test]
    fn foundation_is_closed_and_uses_the_fixed_stable_windows_contract() {
        let machine = TrustedUpdateMachine::new("1.1.0", UPDATE_RUNTIME_ENABLED, None).unwrap();
        assert!(matches!(
            machine.state(),
            DesktopUpdateState::Disabled { .. }
        ));
        assert!(TRUSTED_TAURI_PUBLIC_KEY.is_none());
        assert_eq!(UPDATE_TARGET, "windows-x86_64-nsis");
        assert_eq!(UPDATE_ARCH, "x86_64");
        assert_eq!(
            UPDATE_ENDPOINT,
            "https://github.com/CongBao/stock-desk/releases/latest/download/latest.json"
        );
    }

    #[test]
    fn disabled_confirmation_gate_has_no_prompt_or_state_side_effect() {
        let mut prompt_called = false;
        let result = gate_native_confirmation(UPDATE_RUNTIME_ENABLED, || {
            prompt_called = true;
            Ok(NativeUpdateConsent(()))
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
                TrustedUpdateMachine::new("1.1.0", true, Some(watermark.clone())).unwrap();
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

        let mut restarted = TrustedUpdateMachine::new("1.1.0", true, Some(installed)).unwrap();
        restarted.begin_check().unwrap();
        assert!(restarted
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .is_err());
    }

    #[test]
    fn unavailable_local_data_path_never_creates_relative_updater_state() {
        let controller = DesktopUpdateController::new(None);
        assert!(controller.installed_watermark_path.is_none());
        assert!(matches!(
            controller.machine.lock().unwrap().state(),
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
        let mut machine = TrustedUpdateMachine::new("1.1.0", true, Some(old.clone())).unwrap();
        machine.begin_check().unwrap();
        machine
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .unwrap();
        advance_to_installing(&mut machine);
        let controller = DesktopUpdateController {
            machine: Mutex::new(machine),
            installed_watermark_path: Some(path.clone()),
        };

        assert_eq!(
            controller
                .record_install_success_with(|_, _| { Err("desktop_updater_watermark_unwritable") })
                .unwrap_err(),
            "desktop_updater_watermark_unwritable"
        );
        let mut machine = controller.machine.lock().unwrap();
        assert_eq!(machine.installed_watermark(), Some(&old));
        assert!(matches!(
            machine.state(),
            DesktopUpdateState::ReadyToInstall { version, .. } if version == "1.2.0"
        ));
        machine.begin_install().unwrap();
        drop(machine);
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
            machine: Mutex::new(machine),
            installed_watermark_path: Some(path.clone()),
        };

        controller.record_install_success().unwrap();
        let reloaded = load_installed_watermark(&path).unwrap().unwrap();
        assert_eq!(
            controller.machine.lock().unwrap().installed_watermark(),
            Some(&reloaded)
        );
        let mut restarted = TrustedUpdateMachine::new("1.1.0", true, Some(reloaded)).unwrap();
        restarted.begin_check().unwrap();
        assert!(restarted
            .offer(candidate("1.2.0", "stable", UPDATE_TARGET, UPDATE_ARCH))
            .is_err());
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
        let path = std::env::temp_dir().join(format!(
            "stock-desk-updater-verification-{}",
            std::process::id()
        ));
        fs::write(&path, b"not the expected installer").unwrap();
        let result = verify_downloaded_candidate(&candidate, &path);
        let _ = fs::remove_file(path);
        assert_eq!(result.unwrap_err(), "desktop_updater_sha256_mismatch");
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
    fn redirects_are_exact_version_and_repository_confined() {
        let immutable_manifest =
            "https://github.com/CongBao/stock-desk/releases/download/v1.2.0/latest.json";
        assert!(validate_redirect(UPDATE_ENDPOINT, immutable_manifest, "1.2.0", 0).is_ok());
        assert!(validate_redirect(UPDATE_ENDPOINT, immutable_manifest, "1.2.0+build", 0).is_err());
        assert!(validate_redirect(immutable_manifest, UPDATE_ENDPOINT, "1.2.0", 1).is_err());
        assert!(
            validate_redirect(UPDATE_ENDPOINT, "http://github.com/unsafe", "1.2.0", 0).is_err()
        );
        assert!(validate_redirect(
            UPDATE_ENDPOINT,
            "https://example.com/latest.json",
            "1.2.0",
            0
        )
        .is_err());
        assert!(validate_redirect(
            UPDATE_ENDPOINT,
            "https://github.com/CongBao/stock-desk/releases/download/v1.2.1/latest.json",
            "1.2.0",
            0,
        )
        .is_err());
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
