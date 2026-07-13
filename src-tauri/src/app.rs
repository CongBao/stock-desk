use std::{
    fmt,
    fs::OpenOptions,
    io,
    io::Write as _,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use reqwest::redirect::Policy;
use serde::{Deserialize, Serialize};
use tauri::{App, AppHandle, Emitter, Manager};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

use crate::{exit::DesktopExitController, sidecar::SidecarAuthority, windows_job::WindowsJob};

const STARTUP_TIMEOUT: Duration = Duration::from_secs(45);
const BOOTSTRAP_RELEASE_BYTE: &[u8] = b"\x01";
const ABNORMAL_SETUP_EXIT_CODE: u32 = 70;
const MAX_USER_RESTARTS: u8 = 3;
const MAX_CONSECUTIVE_HEALTH_FAILURES: u8 = 3;
const HEALTH_CHECK_INTERVAL: Duration = Duration::from_secs(5);
const RUNTIME_EVENT: &str = "desktop-runtime-state";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SidecarSetupError {
    ProtectionUnavailable,
    LaunchFailed,
    AssignmentFailed,
    BootstrapFailed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum StartupFailure {
    PermissionDenied,
    SidecarUnavailable,
}

fn startup_failure_state(failure: StartupFailure) -> DesktopRuntimeState {
    DesktopRuntimeState::Recovery {
        reason: match failure {
            StartupFailure::PermissionDenied => "permission_denied",
            StartupFailure::SidecarUnavailable => "sidecar_unavailable",
        },
        can_restart: true,
    }
}

#[derive(Default)]
struct HealthFailureMonitor {
    consecutive_failures: u8,
}

impl HealthFailureMonitor {
    fn record(&mut self, healthy: bool) -> bool {
        if healthy {
            self.consecutive_failures = 0;
            return false;
        }
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        self.consecutive_failures >= MAX_CONSECUTIVE_HEALTH_FAILURES
    }
}

impl SidecarSetupError {
    const fn code(self) -> &'static str {
        match self {
            Self::ProtectionUnavailable => "desktop_sidecar_protection_unavailable",
            Self::LaunchFailed => "desktop_sidecar_launch_failed",
            Self::AssignmentFailed => "desktop_sidecar_assignment_failed",
            Self::BootstrapFailed => "desktop_sidecar_bootstrap_failed",
        }
    }
}

impl fmt::Display for SidecarSetupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for SidecarSetupError {}

trait JobControl {
    fn is_enforced(&self) -> bool;
    fn assign_pid(&self, process_id: u32) -> Result<(), ()>;
    fn terminate(&self, exit_code: u32) -> Result<(), ()>;
}

impl JobControl for WindowsJob {
    fn is_enforced(&self) -> bool {
        self.is_enforced()
    }

    fn assign_pid(&self, process_id: u32) -> Result<(), ()> {
        self.assign_pid(process_id).map_err(|_| ())
    }

    fn terminate(&self, exit_code: u32) -> Result<(), ()> {
        self.terminate(exit_code).map_err(|_| ())
    }
}

trait BootstrapChild: Sized {
    fn pid(&self) -> u32;
    fn write_gate(&mut self, byte: &[u8]) -> Result<(), ()>;
    fn kill_fallback(self);
}

impl BootstrapChild for CommandChild {
    fn pid(&self) -> u32 {
        self.pid()
    }

    fn write_gate(&mut self, byte: &[u8]) -> Result<(), ()> {
        self.write(byte).map_err(|_| ())
    }

    fn kill_fallback(self) {
        let _ = self.kill();
    }
}

fn cleanup_failed_setup<J: JobControl, C: BootstrapChild>(job: &J, child: C) {
    if !job.is_enforced() || job.terminate(ABNORMAL_SETUP_EXIT_CODE).is_err() {
        child.kill_fallback();
    }
}

fn protect_and_release_sidecar<J: JobControl, C: BootstrapChild>(
    job: &J,
    mut child: C,
    require_enforcement: bool,
) -> Result<C, SidecarSetupError> {
    if require_enforcement && !job.is_enforced() {
        cleanup_failed_setup(job, child);
        return Err(SidecarSetupError::ProtectionUnavailable);
    }
    if job.is_enforced() && job.assign_pid(child.pid()).is_err() {
        cleanup_failed_setup(job, child);
        return Err(SidecarSetupError::AssignmentFailed);
    }
    if child.write_gate(BOOTSTRAP_RELEASE_BYTE).is_err() {
        cleanup_failed_setup(job, child);
        return Err(SidecarSetupError::BootstrapFailed);
    }
    Ok(child)
}

fn create_protected_sidecar<J, C, E>(
    create_job: impl FnOnce() -> Result<J, SidecarSetupError>,
    spawn: impl FnOnce() -> Result<(E, C), SidecarSetupError>,
    require_enforcement: bool,
) -> Result<(E, C, J), SidecarSetupError>
where
    J: JobControl,
    C: BootstrapChild,
{
    let job = create_job()?;
    if require_enforcement && !job.is_enforced() {
        return Err(SidecarSetupError::ProtectionUnavailable);
    }
    let (events, child) = spawn()?;
    let child = protect_and_release_sidecar(&job, child, require_enforcement)?;
    Ok((events, child, job))
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum DesktopRuntimeState {
    Starting,
    Ready,
    Recovery {
        reason: &'static str,
        can_restart: bool,
    },
}

pub struct DesktopRuntime {
    inner: Mutex<DesktopRuntimeInner>,
    client: reqwest::Client,
    local_data_root: PathBuf,
}

struct DesktopRuntimeInner {
    slot: GenerationSlot,
    restart_attempts: u8,
}

struct GenerationSlot {
    generation: u64,
    state: DesktopRuntimeState,
    authority: Option<Arc<SidecarAuthority>>,
    child: Option<CommandChild>,
    job: Option<WindowsJob>,
}

struct GenerationResources {
    authority: Arc<SidecarAuthority>,
    child: Option<CommandChild>,
    job: WindowsJob,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct RuntimeMachine {
    state: DesktopRuntimeState,
    generation: u64,
    restart_attempts: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RestartDecision {
    Begin {
        generation: u64,
    },
    Denied {
        code: &'static str,
        state_changed: bool,
    },
}

impl RuntimeMachine {
    fn begin_user_restart(&mut self, exit_idle: bool) -> RestartDecision {
        if !exit_idle {
            return RestartDecision::Denied {
                code: "desktop_exit_in_progress",
                state_changed: false,
            };
        }
        if !matches!(
            self.state,
            DesktopRuntimeState::Recovery {
                can_restart: true,
                ..
            }
        ) {
            return RestartDecision::Denied {
                code: "desktop_restart_not_available",
                state_changed: false,
            };
        }
        if self.restart_attempts >= MAX_USER_RESTARTS {
            let next = DesktopRuntimeState::Recovery {
                reason: "restart_limit_reached",
                can_restart: false,
            };
            let state_changed = self.state != next;
            self.state = next;
            return RestartDecision::Denied {
                code: "desktop_restart_limit_reached",
                state_changed,
            };
        }
        self.restart_attempts += 1;
        self.generation = self.generation.saturating_add(1);
        self.state = DesktopRuntimeState::Starting;
        RestartDecision::Begin {
            generation: self.generation,
        }
    }
}

#[derive(Clone)]
pub(crate) struct ReadySession {
    pub(crate) generation: u64,
    pub(crate) authority: Arc<SidecarAuthority>,
    pub(crate) client: reqwest::Client,
}

impl DesktopRuntime {
    fn new(
        child: CommandChild,
        authority: Arc<SidecarAuthority>,
        job: WindowsJob,
        client: reqwest::Client,
        local_data_root: PathBuf,
    ) -> Self {
        Self {
            inner: Mutex::new(DesktopRuntimeInner {
                slot: GenerationSlot {
                    generation: 1,
                    state: DesktopRuntimeState::Starting,
                    authority: Some(authority),
                    child: Some(child),
                    job: Some(job),
                },
                restart_attempts: 0,
            }),
            client,
            local_data_root,
        }
    }

    fn recovery(
        state: DesktopRuntimeState,
        client: reqwest::Client,
        local_data_root: PathBuf,
    ) -> Self {
        Self {
            inner: Mutex::new(DesktopRuntimeInner {
                slot: GenerationSlot {
                    generation: 1,
                    state,
                    authority: None,
                    child: None,
                    job: None,
                },
                restart_attempts: 0,
            }),
            client,
            local_data_root,
        }
    }

    fn current(&self) -> DesktopRuntimeState {
        self.inner
            .lock()
            .expect("runtime state poisoned")
            .slot
            .state
            .clone()
    }

    fn transition_for_generation(
        &self,
        app: &AppHandle,
        generation: u64,
        state: DesktopRuntimeState,
    ) {
        let mut inner = self.inner.lock().expect("runtime state poisoned");
        if !runtime_transition_allowed(inner.slot.generation, &inner.slot.state, generation, &state)
        {
            return;
        }
        inner.slot.state = state.clone();
        drop(inner);
        let _ = app.emit(RUNTIME_EVENT, state);
    }

    fn transition_startup_for_generation(
        &self,
        app: &AppHandle,
        generation: u64,
        state: DesktopRuntimeState,
    ) {
        let mut inner = self.inner.lock().expect("runtime state poisoned");
        if !startup_transition_allowed(inner.slot.generation, &inner.slot.state, generation) {
            return;
        }
        inner.slot.state = state.clone();
        drop(inner);
        let _ = app.emit(RUNTIME_EVENT, state);
    }

    pub(crate) fn ready_session(&self) -> Result<ReadySession, &'static str> {
        let inner = self.inner.lock().expect("runtime state poisoned");
        if !state_allows_proxy(&inner.slot.state) {
            return Err("desktop_runtime_not_ready");
        }
        let authority = inner
            .slot
            .authority
            .as_ref()
            .ok_or("desktop_runtime_not_ready")?;
        Ok(ReadySession {
            generation: inner.slot.generation,
            authority: Arc::clone(authority),
            client: self.client.clone(),
        })
    }

    pub(crate) fn is_same_ready_generation(&self, generation: u64) -> bool {
        let inner = self.inner.lock().expect("runtime state poisoned");
        state_allows_proxy(&inner.slot.state) && inner.slot.generation == generation
    }

    pub(crate) fn is_same_generation(&self, generation: u64) -> bool {
        self.inner
            .lock()
            .expect("runtime state poisoned")
            .slot
            .generation
            == generation
    }

    pub(crate) fn is_ready(&self) -> bool {
        matches!(self.current(), DesktopRuntimeState::Ready)
    }

    pub(crate) fn terminate_non_ready_for_exit(&self) {
        let (child, job) = {
            let mut inner = self.inner.lock().expect("runtime state poisoned");
            if matches!(inner.slot.state, DesktopRuntimeState::Ready) {
                return;
            }
            // Close the generation before releasing its resources so a late
            // startup handshake cannot reinstall or promote it while the host
            // is committing the user-confirmed exit.
            inner.slot.state = DesktopRuntimeState::Recovery {
                reason: "exit_committed",
                can_restart: false,
            };
            inner.slot.authority = None;
            (inner.slot.child.take(), inner.slot.job.take())
        };
        if let Some(child) = child {
            if let Some(job) = job {
                cleanup_failed_setup(&job, child);
            } else {
                child.kill_fallback();
            }
        }
    }

    pub(crate) fn transition_recovery_for_generation(
        &self,
        app: &AppHandle,
        generation: u64,
        reason: &'static str,
        can_restart: bool,
    ) {
        self.transition_for_generation(
            app,
            generation,
            DesktopRuntimeState::Recovery {
                reason,
                can_restart,
            },
        );
    }

    fn begin_user_restart(
        &self,
        app: &AppHandle,
        exit_idle: bool,
    ) -> Result<(u64, Option<GenerationResources>), &'static str> {
        let (decision, state, old_resources) = {
            let mut inner = self.inner.lock().expect("runtime state poisoned");
            let mut machine = RuntimeMachine {
                state: inner.slot.state.clone(),
                generation: inner.slot.generation,
                restart_attempts: inner.restart_attempts,
            };
            let decision = machine.begin_user_restart(exit_idle);
            inner.restart_attempts = machine.restart_attempts;
            let old_resources = if matches!(decision, RestartDecision::Begin { .. }) {
                match (
                    inner.slot.authority.take(),
                    inner.slot.child.take(),
                    inner.slot.job.take(),
                ) {
                    (Some(authority), child, Some(job)) => Some(GenerationResources {
                        authority,
                        child,
                        job,
                    }),
                    _ => None,
                }
            } else {
                None
            };
            inner.slot.generation = machine.generation;
            inner.slot.state = machine.state.clone();
            (decision, machine.state, old_resources)
        };
        match decision {
            RestartDecision::Begin { generation } => {
                let _ = app.emit(RUNTIME_EVENT, state);
                Ok((generation, old_resources))
            }
            RestartDecision::Denied {
                code,
                state_changed,
            } => {
                if state_changed {
                    let _ = app.emit(RUNTIME_EVENT, state);
                }
                Err(code)
            }
        }
    }

    fn install_generation(
        &self,
        generation: u64,
        resources: GenerationResources,
    ) -> Result<(), GenerationResources> {
        let mut inner = self.inner.lock().expect("runtime state poisoned");
        if inner.slot.generation != generation
            || !matches!(inner.slot.state, DesktopRuntimeState::Starting)
            || inner.slot.authority.is_some()
            || inner.slot.child.is_some()
            || inner.slot.job.is_some()
        {
            return Err(resources);
        }
        inner.slot.authority = Some(resources.authority);
        inner.slot.child = resources.child;
        inner.slot.job = Some(resources.job);
        Ok(())
    }

    fn can_restart_after_failure(&self, generation: u64) -> bool {
        let inner = self.inner.lock().expect("runtime state poisoned");
        inner.slot.generation == generation && inner.restart_attempts < MAX_USER_RESTARTS
    }

    fn restart_failure_state(
        &self,
        generation: u64,
        default_reason: &'static str,
    ) -> DesktopRuntimeState {
        if self.can_restart_after_failure(generation) {
            DesktopRuntimeState::Recovery {
                reason: default_reason,
                can_restart: true,
            }
        } else {
            DesktopRuntimeState::Recovery {
                reason: "restart_limit_reached",
                can_restart: false,
            }
        }
    }

    fn local_data_root(&self) -> &PathBuf {
        &self.local_data_root
    }
}

impl GenerationResources {
    fn cleanup_abnormal(mut self) {
        if let Some(child) = self.child.take() {
            cleanup_failed_setup(&self.job, child);
        }
    }
}

fn runtime_transition_allowed(
    current_generation: u64,
    current_state: &DesktopRuntimeState,
    event_generation: u64,
    next_state: &DesktopRuntimeState,
) -> bool {
    current_generation == event_generation
        && !matches!(current_state, DesktopRuntimeState::Recovery { .. })
        && (!matches!(next_state, DesktopRuntimeState::Ready)
            || matches!(current_state, DesktopRuntimeState::Starting))
}

fn startup_transition_allowed(
    current_generation: u64,
    current_state: &DesktopRuntimeState,
    event_generation: u64,
) -> bool {
    current_generation == event_generation && matches!(current_state, DesktopRuntimeState::Starting)
}

fn build_proxy_client() -> Result<reqwest::Client, reqwest::Error> {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(2))
        .timeout(Duration::from_secs(30))
        .redirect(Policy::none())
        .build()
}

fn state_allows_proxy(state: &DesktopRuntimeState) -> bool {
    matches!(state, DesktopRuntimeState::Ready)
}

#[tauri::command]
pub fn desktop_runtime_state(runtime: tauri::State<'_, DesktopRuntime>) -> DesktopRuntimeState {
    runtime.current()
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Handshake {
    status: String,
    api_version: String,
    host_version: String,
    frontend_version: String,
    sidecar_version: String,
    source_revision: String,
    storage: String,
}

impl Handshake {
    fn matches(&self, authority: &SidecarAuthority) -> bool {
        self.status == "ready"
            && self.api_version == "v1"
            && self.host_version == "1.1.0"
            && self.frontend_version == "1.1.0"
            && self.sidecar_version == "1.1.0"
            && self.source_revision == authority.source_revision()
            && self.storage == "ready"
    }
}

pub fn setup(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
    let local_data_root = app.path().local_data_dir()?;
    // Finish all fallible host-only initialization before the gated process is
    // spawned. No error may strand a released child outside managed runtime.
    let client = build_proxy_client()?;
    let data_root = local_data_root.join("Stock Desk").join("v1.1");
    if ensure_user_data_root(&data_root).is_err() {
        app.manage(DesktopRuntime::recovery(
            startup_failure_state(StartupFailure::PermissionDenied),
            client,
            local_data_root,
        ));
        return Ok(());
    }
    let authority =
        match SidecarAuthority::new(&local_data_root, env!("STOCK_DESK_SOURCE_REVISION")) {
            Ok(authority) => Arc::new(authority),
            Err(_) => {
                app.manage(DesktopRuntime::recovery(
                    startup_failure_state(StartupFailure::SidecarUnavailable),
                    client,
                    local_data_root,
                ));
                return Ok(());
            }
        };
    let (events, child, job) = match spawn_generation(app.handle(), &authority) {
        Ok(resources) => resources,
        Err(_) => {
            app.manage(DesktopRuntime::recovery(
                startup_failure_state(StartupFailure::SidecarUnavailable),
                client,
                local_data_root,
            ));
            return Ok(());
        }
    };
    app.manage(DesktopRuntime::new(
        child,
        Arc::clone(&authority),
        job,
        client,
        local_data_root,
    ));
    start_generation_watchers(app.handle().clone(), 1, authority, events);
    Ok(())
}

fn ensure_user_data_root(path: &Path) -> io::Result<()> {
    std::fs::create_dir_all(path)?;
    let probe = path.join(format!(".write-probe-{}", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&probe)?;
    let write_result = file.write_all(b"stock-desk");
    drop(file);
    let cleanup_result = std::fs::remove_file(probe);
    write_result.and(cleanup_result)
}

fn spawn_generation(
    app: &AppHandle,
    authority: &SidecarAuthority,
) -> Result<
    (
        tauri::async_runtime::Receiver<CommandEvent>,
        CommandChild,
        WindowsJob,
    ),
    SidecarSetupError,
> {
    // This abstraction makes the security order explicit and testable:
    // Job -> spawn gated process -> assign to Job -> release bootstrap gate.
    // Port reservation still has an OS-level bind-to-spawn race; startup
    // failure is surfaced to bounded user recovery rather than hidden.
    create_protected_sidecar(
        || WindowsJob::new_kill_on_close().map_err(|_| SidecarSetupError::ProtectionUnavailable),
        || {
            app.shell()
                .sidecar("stock-desk-sidecar")
                .map_err(|_| SidecarSetupError::LaunchFailed)?
                .envs(authority.environment())
                .spawn()
                .map_err(|_| SidecarSetupError::LaunchFailed)
        },
        cfg!(windows),
    )
}

fn start_generation_watchers(
    app: AppHandle,
    generation: u64,
    authority: Arc<SidecarAuthority>,
    mut events: tauri::async_runtime::Receiver<CommandEvent>,
) {
    let event_handle = app.clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Terminated(payload) => {
                    let exit_owned = event_handle
                        .try_state::<DesktopExitController>()
                        .is_some_and(|controller| {
                            controller.sidecar_terminated(&event_handle, generation, payload.code)
                        });
                    if exit_owned {
                        break;
                    }
                    if let Some(runtime) = event_handle.try_state::<DesktopRuntime>() {
                        let state =
                            runtime.restart_failure_state(generation, "sidecar_unavailable");
                        runtime.transition_for_generation(&event_handle, generation, state);
                    }
                    break;
                }
                // A shell stream error is not proof that the process exited.
                // Only a matching Terminated event may commit or fail exit.
                CommandEvent::Error(_) => {}
                CommandEvent::Stdout(_) | CommandEvent::Stderr(_) => {}
                _ => {}
            }
        }
    });

    tauri::async_runtime::spawn(async move {
        let state = wait_for_handshake(&authority).await;
        let ready = matches!(state, DesktopRuntimeState::Ready);
        if let Some(runtime) = app.try_state::<DesktopRuntime>() {
            runtime.transition_startup_for_generation(&app, generation, state);
        }
        if ready {
            monitor_ready_health(app, generation, authority).await;
        }
    });
}

#[tauri::command]
pub async fn desktop_restart_service(app: AppHandle) -> Result<(), String> {
    let runtime = app
        .try_state::<DesktopRuntime>()
        .ok_or_else(|| "desktop_runtime_not_ready".to_owned())?;
    let exit_idle = app
        .try_state::<DesktopExitController>()
        .is_some_and(|controller| controller.allows_service_restart());
    let (generation, old_slot) = runtime
        .begin_user_restart(&app, exit_idle)
        .map_err(str::to_owned)?;
    if let Some(slot) = old_slot {
        slot.cleanup_abnormal();
    }

    let authority = match SidecarAuthority::new(
        runtime.local_data_root(),
        env!("STOCK_DESK_SOURCE_REVISION"),
    ) {
        Ok(authority) => Arc::new(authority),
        Err(_) => {
            let state = runtime.restart_failure_state(generation, "sidecar_unavailable");
            runtime.transition_for_generation(&app, generation, state);
            return Err("desktop_restart_failed".to_owned());
        }
    };
    let (events, child, job) = match spawn_generation(&app, &authority) {
        Ok(spawned) => spawned,
        Err(_) => {
            let state = runtime.restart_failure_state(generation, "sidecar_unavailable");
            runtime.transition_for_generation(&app, generation, state);
            return Err("desktop_restart_failed".to_owned());
        }
    };
    let resources = GenerationResources {
        authority: Arc::clone(&authority),
        child: Some(child),
        job,
    };
    if let Err(resources) = runtime.install_generation(generation, resources) {
        resources.cleanup_abnormal();
        return Err("desktop_restart_superseded".to_owned());
    }
    start_generation_watchers(app, generation, authority, events);
    Ok(())
}

async fn wait_for_handshake(authority: &SidecarAuthority) -> DesktopRuntimeState {
    let client = match build_health_client() {
        Ok(client) => client,
        Err(_) => {
            return startup_failure_state(StartupFailure::SidecarUnavailable);
        }
    };
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if let Ok(handshake) = request_handshake(&client, authority).await {
            if handshake.matches(authority) {
                return DesktopRuntimeState::Ready;
            }
            return DesktopRuntimeState::Recovery {
                reason: "version_mismatch",
                can_restart: false,
            };
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    DesktopRuntimeState::Recovery {
        reason: "startup_timeout",
        can_restart: true,
    }
}

fn build_health_client() -> Result<reqwest::Client, reqwest::Error> {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_millis(500))
        .timeout(Duration::from_secs(2))
        .redirect(Policy::none())
        .build()
}

async fn request_handshake(
    client: &reqwest::Client,
    authority: &SidecarAuthority,
) -> Result<Handshake, ()> {
    let response = client
        .get(authority.handshake_url())
        .header("Origin", authority.origin())
        .header("Authorization", authority.authorization_header())
        .send()
        .await
        .map_err(|_| ())?;
    if !response.status().is_success() {
        return Err(());
    }
    response.json::<Handshake>().await.map_err(|_| ())
}

async fn monitor_ready_health(app: AppHandle, generation: u64, authority: Arc<SidecarAuthority>) {
    let client = match build_health_client() {
        Ok(client) => client,
        Err(_) => return,
    };
    let mut monitor = HealthFailureMonitor::default();
    loop {
        tokio::time::sleep(HEALTH_CHECK_INTERVAL).await;
        let Some(runtime) = app.try_state::<DesktopRuntime>() else {
            return;
        };
        if !runtime.is_same_ready_generation(generation) {
            return;
        }
        match request_handshake(&client, &authority).await {
            Ok(handshake) if handshake.matches(&authority) => {
                monitor.record(true);
            }
            Ok(_) => {
                runtime.transition_recovery_for_generation(
                    &app,
                    generation,
                    "version_mismatch",
                    false,
                );
                return;
            }
            Err(()) if monitor.record(false) => {
                let state = runtime.restart_failure_state(generation, "sidecar_unavailable");
                runtime.transition_for_generation(&app, generation, state);
                return;
            }
            Err(()) => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        cell::RefCell,
        io::{Read, Write},
        rc::Rc,
    };

    struct FakeJob {
        enforced: bool,
        assign_ok: bool,
        terminate_ok: bool,
        events: Rc<RefCell<Vec<&'static str>>>,
    }

    impl JobControl for FakeJob {
        fn is_enforced(&self) -> bool {
            self.enforced
        }

        fn assign_pid(&self, _process_id: u32) -> Result<(), ()> {
            self.events.borrow_mut().push("assign");
            self.assign_ok.then_some(()).ok_or(())
        }

        fn terminate(&self, _exit_code: u32) -> Result<(), ()> {
            self.events.borrow_mut().push("terminate");
            self.terminate_ok.then_some(()).ok_or(())
        }
    }

    #[derive(Debug)]
    struct FakeChild {
        write_ok: bool,
        events: Rc<RefCell<Vec<&'static str>>>,
    }

    impl BootstrapChild for FakeChild {
        fn pid(&self) -> u32 {
            42
        }

        fn write_gate(&mut self, byte: &[u8]) -> Result<(), ()> {
            assert_eq!(byte, b"\x01");
            self.events.borrow_mut().push("write_gate");
            self.write_ok.then_some(()).ok_or(())
        }

        fn kill_fallback(self) {
            self.events.borrow_mut().push("kill");
        }
    }

    fn protection_fixture(
        enforced: bool,
        assign_ok: bool,
        terminate_ok: bool,
        write_ok: bool,
    ) -> (FakeJob, FakeChild, Rc<RefCell<Vec<&'static str>>>) {
        let events = Rc::new(RefCell::new(Vec::new()));
        (
            FakeJob {
                enforced,
                assign_ok,
                terminate_ok,
                events: Rc::clone(&events),
            },
            FakeChild {
                write_ok,
                events: Rc::clone(&events),
            },
            events,
        )
    }

    #[test]
    fn bootstrap_gate_is_written_only_after_job_assignment() {
        let (job, child, events) = protection_fixture(true, true, true, true);

        protect_and_release_sidecar(&job, child, true).unwrap();

        assert_eq!(*events.borrow(), ["assign", "write_gate"]);
    }

    #[test]
    fn generation_spawner_enforces_job_spawn_assign_gate_order() {
        let events = Rc::new(RefCell::new(Vec::new()));
        let create_events = Rc::clone(&events);
        let spawn_events = Rc::clone(&events);
        let job_events = Rc::clone(&events);
        let child_events = Rc::clone(&events);

        let result = create_protected_sidecar(
            move || {
                create_events.borrow_mut().push("job");
                Ok(FakeJob {
                    enforced: true,
                    assign_ok: true,
                    terminate_ok: true,
                    events: job_events,
                })
            },
            move || {
                spawn_events.borrow_mut().push("spawn");
                Ok((
                    (),
                    FakeChild {
                        write_ok: true,
                        events: child_events,
                    },
                ))
            },
            true,
        );

        assert!(result.is_ok());
        assert_eq!(*events.borrow(), ["job", "spawn", "assign", "write_gate"]);
    }

    #[test]
    fn generation_spawner_never_launches_when_job_protection_is_unavailable() {
        let events = Rc::new(RefCell::new(Vec::new()));
        let create_events = Rc::clone(&events);
        let job_events = Rc::clone(&events);
        let spawn_events = Rc::clone(&events);

        let result = create_protected_sidecar(
            move || {
                create_events.borrow_mut().push("job");
                Ok(FakeJob {
                    enforced: false,
                    assign_ok: true,
                    terminate_ok: true,
                    events: job_events,
                })
            },
            move || {
                spawn_events.borrow_mut().push("spawn");
                Err::<((), FakeChild), _>(SidecarSetupError::LaunchFailed)
            },
            true,
        );

        assert!(matches!(
            result,
            Err(SidecarSetupError::ProtectionUnavailable)
        ));
        assert_eq!(*events.borrow(), ["job"]);
    }

    #[test]
    fn production_fails_closed_before_gate_when_protection_is_not_enforced() {
        let (job, child, events) = protection_fixture(false, true, true, true);

        let error = protect_and_release_sidecar(&job, child, true).unwrap_err();

        assert_eq!(error, SidecarSetupError::ProtectionUnavailable);
        assert_eq!(*events.borrow(), ["kill"]);
    }

    #[test]
    fn assignment_failure_terminates_job_without_releasing_gate() {
        let (job, child, events) = protection_fixture(true, false, true, true);

        let error = protect_and_release_sidecar(&job, child, true).unwrap_err();

        assert_eq!(error, SidecarSetupError::AssignmentFailed);
        assert_eq!(*events.borrow(), ["assign", "terminate"]);
    }

    #[test]
    fn failed_job_termination_falls_back_to_direct_child_kill() {
        let (job, child, events) = protection_fixture(true, false, false, true);

        let error = protect_and_release_sidecar(&job, child, true).unwrap_err();

        assert_eq!(error, SidecarSetupError::AssignmentFailed);
        assert_eq!(*events.borrow(), ["assign", "terminate", "kill"]);
    }

    #[test]
    fn gate_write_failure_is_abnormal_cleanup_and_errors_are_stable() {
        let (job, child, events) = protection_fixture(true, true, true, false);

        let error = protect_and_release_sidecar(&job, child, true).unwrap_err();

        assert_eq!(error, SidecarSetupError::BootstrapFailed);
        assert_eq!(error.to_string(), "desktop_sidecar_bootstrap_failed");
        assert_eq!(*events.borrow(), ["assign", "write_gate", "terminate"]);
    }

    #[cfg(not(windows))]
    #[test]
    fn non_windows_test_host_releases_gate_without_claiming_job_protection() {
        let (job, child, events) = protection_fixture(false, true, true, true);

        protect_and_release_sidecar(&job, child, false).unwrap();

        assert_eq!(*events.borrow(), ["write_gate"]);
    }

    #[test]
    fn runtime_state_wire_format_is_closed_and_contains_no_authority() {
        assert_eq!(
            serde_json::to_value(DesktopRuntimeState::Starting).unwrap(),
            serde_json::json!({"state": "starting"})
        );
        assert_eq!(
            serde_json::to_value(DesktopRuntimeState::Ready).unwrap(),
            serde_json::json!({"state": "ready"})
        );
        assert_eq!(
            serde_json::to_value(DesktopRuntimeState::Recovery {
                reason: "sidecar_unavailable",
                can_restart: true,
            })
            .unwrap(),
            serde_json::json!({
                "state": "recovery",
                "reason": "sidecar_unavailable",
                "can_restart": true
            })
        );
        let wire = serde_json::to_string(&DesktopRuntimeState::Recovery {
            reason: "startup_timeout",
            can_restart: true,
        })
        .unwrap();
        assert!(!wire.contains("127.0.0.1"));
        assert!(!wire.contains("Stock Desk"));
        assert!(!wire.contains("secret"));
    }

    #[test]
    fn restart_reducer_is_recovery_only_exit_safe_and_bounded() {
        let mut machine = RuntimeMachine {
            state: DesktopRuntimeState::Recovery {
                reason: "sidecar_unavailable",
                can_restart: true,
            },
            generation: 4,
            restart_attempts: 0,
        };
        assert_eq!(
            machine.begin_user_restart(false),
            RestartDecision::Denied {
                code: "desktop_exit_in_progress",
                state_changed: false,
            }
        );
        assert_eq!(machine.generation, 4);
        for expected_generation in 5..=7 {
            assert_eq!(
                machine.begin_user_restart(true),
                RestartDecision::Begin {
                    generation: expected_generation,
                }
            );
            machine.state = DesktopRuntimeState::Recovery {
                reason: "startup_timeout",
                can_restart: true,
            };
        }
        assert_eq!(
            machine.begin_user_restart(true),
            RestartDecision::Denied {
                code: "desktop_restart_limit_reached",
                state_changed: true,
            }
        );
        assert_eq!(
            machine.state,
            DesktopRuntimeState::Recovery {
                reason: "restart_limit_reached",
                can_restart: false,
            }
        );
    }

    #[test]
    fn version_mismatch_and_non_recovery_states_refuse_restart() {
        for state in [
            DesktopRuntimeState::Starting,
            DesktopRuntimeState::Ready,
            DesktopRuntimeState::Recovery {
                reason: "version_mismatch",
                can_restart: false,
            },
        ] {
            let mut machine = RuntimeMachine {
                state,
                generation: 8,
                restart_attempts: 0,
            };
            assert_eq!(
                machine.begin_user_restart(true),
                RestartDecision::Denied {
                    code: "desktop_restart_not_available",
                    state_changed: false,
                }
            );
            assert_eq!(machine.generation, 8);
        }
    }

    #[test]
    fn handshake_requires_every_exact_identity() {
        let authority = SidecarAuthority::new(
            &std::env::temp_dir().join("stock-desk-user-root"),
            &"c".repeat(40),
        )
        .unwrap();
        let mut handshake = Handshake {
            status: "ready".into(),
            api_version: "v1".into(),
            host_version: "1.1.0".into(),
            frontend_version: "1.1.0".into(),
            sidecar_version: "1.1.0".into(),
            source_revision: "c".repeat(40),
            storage: "ready".into(),
        };
        assert!(handshake.matches(&authority));
        handshake.source_revision = "d".repeat(40);
        assert!(!handshake.matches(&authority));
    }

    #[test]
    fn proxy_fails_closed_until_ready_and_during_recovery() {
        assert!(!state_allows_proxy(&DesktopRuntimeState::Starting));
        assert!(state_allows_proxy(&DesktopRuntimeState::Ready));
        assert!(!state_allows_proxy(&DesktopRuntimeState::Recovery {
            reason: "sidecar_unavailable",
            can_restart: true,
        }));
    }

    #[test]
    fn late_generation_and_late_handshake_cannot_overwrite_runtime_state() {
        assert!(!runtime_transition_allowed(
            2,
            &DesktopRuntimeState::Starting,
            1,
            &DesktopRuntimeState::Ready,
        ));
        assert!(!runtime_transition_allowed(
            2,
            &DesktopRuntimeState::Recovery {
                reason: "sidecar_unavailable",
                can_restart: true,
            },
            2,
            &DesktopRuntimeState::Ready,
        ));
        assert!(!runtime_transition_allowed(
            2,
            &DesktopRuntimeState::Recovery {
                reason: "version_mismatch",
                can_restart: false,
            },
            2,
            &DesktopRuntimeState::Recovery {
                reason: "sidecar_unavailable",
                can_restart: true,
            },
        ));
        assert!(runtime_transition_allowed(
            2,
            &DesktopRuntimeState::Starting,
            2,
            &DesktopRuntimeState::Ready,
        ));
        assert!(!startup_transition_allowed(
            2,
            &DesktopRuntimeState::Recovery {
                reason: "sidecar_unavailable",
                can_restart: true,
            },
            2,
        ));
        assert!(startup_transition_allowed(
            2,
            &DesktopRuntimeState::Starting,
            2,
        ));
    }

    #[test]
    fn proxy_client_never_follows_redirects() {
        let listener = std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 1024];
            let _ = stream.read(&mut request).unwrap();
            stream
                .write_all(
                    b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/exfiltrate\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .unwrap();
        });
        let response = tauri::async_runtime::block_on(async {
            build_proxy_client()
                .unwrap()
                .get(format!("http://{address}/api/test"))
                .send()
                .await
                .unwrap()
        });
        assert_eq!(response.status(), reqwest::StatusCode::FOUND);
        server.join().unwrap();
    }

    #[test]
    fn running_health_loss_requires_three_consecutive_failures() {
        let mut monitor = HealthFailureMonitor::default();
        assert!(!monitor.record(false));
        assert!(!monitor.record(false));
        monitor.record(true);
        assert!(!monitor.record(false));
        assert!(!monitor.record(false));
        assert!(monitor.record(false));
    }

    #[test]
    fn startup_failures_are_recoverable_and_stably_categorized() {
        assert_eq!(
            startup_failure_state(StartupFailure::PermissionDenied),
            DesktopRuntimeState::Recovery {
                reason: "permission_denied",
                can_restart: true,
            }
        );
        assert_eq!(
            startup_failure_state(StartupFailure::SidecarUnavailable),
            DesktopRuntimeState::Recovery {
                reason: "sidecar_unavailable",
                can_restart: true,
            }
        );
    }
}
