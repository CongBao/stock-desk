use std::{sync::Mutex, time::Duration};

use serde::{Deserialize, Serialize};
use tauri::{App, AppHandle, Emitter, Manager};

use crate::app::{DesktopRuntime, ReadySession};

const EXIT_EVENT: &str = "desktop-exit-state";
const SHUTDOWN_PATH: &str = "/api/desktop/shutdown";
const SHUTDOWN_COMMIT_PATH: &str = "/api/desktop/shutdown/commit";
const SHUTDOWN_RESPONSE_LIMIT: usize = 4 * 1024;
// The sidecar owns an internal 10-second worker drain deadline. The host must
// remain authoritative beyond that boundary so a late clean Terminated event
// cannot race a host timeout and leave the desktop permanently open.
const SIDECAR_EXIT_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum DesktopExitState {
    Idle,
    Confirm,
    Checking,
    Blocked { queued: u32, running: u32 },
    CheckpointTimedOut { queued: u32, running: u32 },
    ShuttingDown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ShutdownReply {
    Accepted,
    Blocked { queued: u32, running: u32 },
    CheckpointTimedOut { queued: u32, running: u32 },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExitEffect {
    None,
    Emit,
    Recover(u64),
    Exit,
}

#[derive(Debug)]
struct ExitMachine {
    state: DesktopExitState,
    generation: Option<u64>,
    terminated_before_reply: Option<Option<i32>>,
    committed: bool,
}

impl Default for ExitMachine {
    fn default() -> Self {
        Self {
            state: DesktopExitState::Idle,
            generation: None,
            terminated_before_reply: None,
            committed: false,
        }
    }
}

impl ExitMachine {
    fn request(&mut self) -> ExitEffect {
        if self.state == DesktopExitState::Idle {
            self.state = DesktopExitState::Confirm;
            ExitEffect::Emit
        } else {
            ExitEffect::None
        }
    }

    fn cancel(&mut self) -> ExitEffect {
        if matches!(
            self.state,
            DesktopExitState::Confirm
                | DesktopExitState::Blocked { .. }
                | DesktopExitState::CheckpointTimedOut { .. }
        ) {
            self.reset();
            ExitEffect::Emit
        } else {
            ExitEffect::None
        }
    }

    fn confirm(&mut self, generation: u64) -> ExitEffect {
        if !matches!(
            self.state,
            DesktopExitState::Confirm
                | DesktopExitState::Blocked { .. }
                | DesktopExitState::CheckpointTimedOut { .. }
        ) {
            return ExitEffect::None;
        }
        self.state = DesktopExitState::Checking;
        self.generation = Some(generation);
        self.terminated_before_reply = None;
        ExitEffect::Emit
    }

    fn confirm_without_ready_session(&mut self) -> ExitEffect {
        if self.state != DesktopExitState::Confirm {
            return ExitEffect::None;
        }
        self.state = DesktopExitState::ShuttingDown;
        self.generation = None;
        self.terminated_before_reply = None;
        self.committed = true;
        ExitEffect::Exit
    }

    fn shutdown_reply(&mut self, generation: u64, reply: ShutdownReply) -> ExitEffect {
        if self.generation != Some(generation) || self.state != DesktopExitState::Checking {
            return ExitEffect::None;
        }
        match reply {
            ShutdownReply::CheckpointTimedOut { queued, running } => {
                if self.terminated_before_reply.take().is_some() {
                    let failed_generation = self.generation.expect("checked generation");
                    self.reset();
                    return ExitEffect::Recover(failed_generation);
                }
                self.state = DesktopExitState::CheckpointTimedOut { queued, running };
                self.generation = None;
                self.terminated_before_reply = None;
                ExitEffect::Emit
            }
            ShutdownReply::Blocked { queued, running } => {
                if self.terminated_before_reply.take().is_some() {
                    let failed_generation = self.generation.expect("checked generation");
                    self.reset();
                    return ExitEffect::Recover(failed_generation);
                }
                self.state = DesktopExitState::Blocked { queued, running };
                self.generation = None;
                self.terminated_before_reply = None;
                ExitEffect::Emit
            }
            ShutdownReply::Accepted => match self.terminated_before_reply.take() {
                Some(_) => {
                    self.committed = true;
                    self.state = DesktopExitState::ShuttingDown;
                    ExitEffect::Exit
                }
                None => {
                    self.state = DesktopExitState::ShuttingDown;
                    ExitEffect::Emit
                }
            },
        }
    }

    fn shutdown_failed(&mut self, generation: u64) -> ExitEffect {
        if self.generation != Some(generation) || self.state != DesktopExitState::Checking {
            return ExitEffect::None;
        }
        let failed_generation = self.generation.expect("checked generation");
        self.reset();
        ExitEffect::Recover(failed_generation)
    }

    fn shutdown_timed_out(&mut self, generation: u64) -> ExitEffect {
        if self.generation != Some(generation) || self.state != DesktopExitState::ShuttingDown {
            return ExitEffect::None;
        }
        let failed_generation = self.generation.expect("checked generation");
        self.reset();
        ExitEffect::Recover(failed_generation)
    }

    fn commit_failed(&mut self, generation: u64) -> ExitEffect {
        if self.generation != Some(generation) || self.state != DesktopExitState::ShuttingDown {
            return ExitEffect::None;
        }
        self.reset();
        ExitEffect::Recover(generation)
    }

    fn terminated(&mut self, generation: u64, code: Option<i32>) -> ExitEffect {
        if self.generation != Some(generation) {
            return ExitEffect::None;
        }
        match self.state {
            DesktopExitState::Checking => {
                self.terminated_before_reply = Some(code);
                ExitEffect::None
            }
            DesktopExitState::ShuttingDown => {
                self.committed = true;
                ExitEffect::Exit
            }
            _ => ExitEffect::None,
        }
    }

    fn owns_termination(&self, generation: u64) -> bool {
        self.generation == Some(generation)
            && matches!(
                self.state,
                DesktopExitState::Checking | DesktopExitState::ShuttingDown
            )
    }

    fn reset(&mut self) {
        self.state = DesktopExitState::Idle;
        self.generation = None;
        self.terminated_before_reply = None;
        self.committed = false;
    }
}

pub struct DesktopExitController {
    machine: Mutex<ExitMachine>,
}

impl Default for DesktopExitController {
    fn default() -> Self {
        Self {
            machine: Mutex::new(ExitMachine::default()),
        }
    }
}

impl DesktopExitController {
    pub fn is_committed(&self) -> bool {
        self.machine.lock().expect("exit state poisoned").committed
    }

    pub(crate) fn allows_service_restart(&self) -> bool {
        let machine = self.machine.lock().expect("exit state poisoned");
        machine.state == DesktopExitState::Idle && !machine.committed
    }

    fn apply(&self, app: &AppHandle, update: impl FnOnce(&mut ExitMachine) -> ExitEffect) {
        let (effect, state) = {
            let mut machine = self.machine.lock().expect("exit state poisoned");
            let effect = update(&mut machine);
            (effect, machine.state.clone())
        };
        self.finish_effect(app, effect, state);
    }

    fn finish_effect(&self, app: &AppHandle, effect: ExitEffect, state: DesktopExitState) {
        match effect {
            ExitEffect::None => {}
            ExitEffect::Emit => {
                let _ = app.emit(EXIT_EVENT, state);
            }
            ExitEffect::Recover(failed_generation) => {
                let _ = app.emit(EXIT_EVENT, DesktopExitState::Idle);
                if let Some(runtime) = app.try_state::<DesktopRuntime>() {
                    runtime.transition_recovery_for_generation(
                        app,
                        failed_generation,
                        "sidecar_unavailable",
                        true,
                    );
                }
            }
            ExitEffect::Exit => app.exit(0),
        }
    }

    pub fn request(&self, app: &AppHandle) {
        self.apply(app, ExitMachine::request);
    }

    fn cancel(&self, app: &AppHandle) {
        self.apply(app, ExitMachine::cancel);
    }

    fn begin_confirm(&self, app: &AppHandle, generation: u64) -> Option<bool> {
        let mut changed = false;
        let mut checkpoint_active = false;
        self.apply(app, |machine| {
            checkpoint_active = matches!(
                machine.state,
                DesktopExitState::Blocked { .. } | DesktopExitState::CheckpointTimedOut { .. }
            );
            let effect = machine.confirm(generation);
            changed = effect == ExitEffect::Emit;
            effect
        });
        changed.then_some(checkpoint_active)
    }

    fn confirm_without_ready_session(&self, app: &AppHandle) -> bool {
        let mut committed = false;
        self.apply(app, |machine| {
            let effect = machine.confirm_without_ready_session();
            committed = effect == ExitEffect::Exit;
            if committed {
                // The caller must first release the failed managed sidecar
                // resources; it performs the actual app exit afterwards.
                ExitEffect::Emit
            } else {
                effect
            }
        });
        committed
    }

    fn receive_reply(&self, app: &AppHandle, generation: u64, reply: ShutdownReply) {
        self.apply(app, |machine| machine.shutdown_reply(generation, reply));
    }

    fn fail(&self, app: &AppHandle, generation: u64) {
        self.apply(app, |machine| machine.shutdown_failed(generation));
    }

    fn timeout(&self, app: &AppHandle, generation: u64) {
        self.apply(app, |machine| machine.shutdown_timed_out(generation));
    }

    fn commit_failed(&self, app: &AppHandle, generation: u64) {
        self.apply(app, |machine| machine.commit_failed(generation));
    }

    pub(crate) fn sidecar_terminated(
        &self,
        app: &AppHandle,
        generation: u64,
        code: Option<i32>,
    ) -> bool {
        let owned = self
            .machine
            .lock()
            .expect("exit state poisoned")
            .owns_termination(generation);
        if owned {
            self.apply(app, |machine| machine.terminated(generation, code));
        }
        owned
    }
}

pub fn setup(app: &mut App) {
    app.manage(DesktopExitController::default());
}

pub fn request_from_host(app: &AppHandle) {
    if let Some(controller) = app.try_state::<DesktopExitController>() {
        controller.request(app);
    }
}

pub fn exit_is_committed(app: &AppHandle) -> bool {
    app.try_state::<DesktopExitController>()
        .is_some_and(|controller| controller.is_committed())
}

#[tauri::command]
pub fn desktop_request_exit(app: AppHandle) {
    request_from_host(&app);
}

#[tauri::command]
pub fn desktop_cancel_exit(app: AppHandle) {
    if let Some(controller) = app.try_state::<DesktopExitController>() {
        controller.cancel(&app);
    }
}

#[tauri::command]
pub async fn desktop_confirm_exit(app: AppHandle) -> Result<(), String> {
    let runtime = app
        .try_state::<DesktopRuntime>()
        .ok_or_else(|| "desktop_runtime_not_ready".to_owned())?;
    let controller = app
        .try_state::<DesktopExitController>()
        .ok_or_else(|| "desktop_exit_unavailable".to_owned())?;
    if !runtime.is_ready() {
        if controller.confirm_without_ready_session(&app) {
            runtime.terminate_non_ready_for_exit();
            app.exit(0);
        }
        return Ok(());
    }
    let session = runtime.ready_session().map_err(str::to_owned)?;
    let Some(checkpoint_active) = controller.begin_confirm(&app, session.generation) else {
        return Ok(());
    };

    let result = request_shutdown(&session, checkpoint_active).await;
    if !runtime.is_same_generation(session.generation) {
        controller.fail(&app, session.generation);
        return Ok(());
    }
    match result {
        Ok(reply) => controller.receive_reply(&app, session.generation, reply),
        Err(()) => controller.fail(&app, session.generation),
    }

    if matches!(result, Ok(ShutdownReply::Accepted)) {
        if commit_shutdown(&session).await.is_err()
            || !runtime.is_same_generation(session.generation)
        {
            controller.commit_failed(&app, session.generation);
            return Ok(());
        }
        let timeout_app = app.clone();
        let generation = session.generation;
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(SIDECAR_EXIT_TIMEOUT).await;
            if let Some(controller) = timeout_app.try_state::<DesktopExitController>() {
                controller.timeout(&timeout_app, generation);
            }
        });
    }
    Ok(())
}

async fn commit_shutdown(session: &ReadySession) -> Result<(), ()> {
    let url = session
        .authority
        .api_url(SHUTDOWN_COMMIT_PATH)
        .map_err(|_| ())?;
    let response = session
        .client
        .post(url)
        .header("Origin", session.authority.origin())
        .header("Authorization", session.authority.authorization_header())
        .header("Accept", "application/json")
        .timeout(Duration::from_secs(2))
        .send()
        .await
        .map_err(|_| ())?;
    if response.status() != reqwest::StatusCode::ACCEPTED
        || response.content_length().is_some_and(|length| length > 256)
        || response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.split(';').next())
            .map(str::trim)
            != Some("application/json")
    {
        return Err(());
    }
    let body = response.bytes().await.map_err(|_| ())?;
    if body.len() > 256 {
        return Err(());
    }
    let committed: CommittedShutdown = serde_json::from_slice(&body).map_err(|_| ())?;
    (committed.status == "shutdown_committed")
        .then_some(())
        .ok_or(())
}

async fn request_shutdown(
    session: &ReadySession,
    checkpoint_active: bool,
) -> Result<ShutdownReply, ()> {
    let url = session.authority.api_url(SHUTDOWN_PATH).map_err(|_| ())?;
    let mut request = session
        .client
        .post(url)
        .header("Origin", session.authority.origin())
        .header("Authorization", session.authority.authorization_header())
        .header("Accept", "application/json")
        .timeout(if checkpoint_active {
            Duration::from_secs(12)
        } else {
            Duration::from_secs(5)
        });
    if checkpoint_active {
        request = request.json(&serde_json::json!({"checkpoint_active": true}));
    }
    let mut response = request.send().await.map_err(|_| ())?;
    if response
        .content_length()
        .is_some_and(|length| length > SHUTDOWN_RESPONSE_LIMIT as u64)
    {
        return Err(());
    }
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .map(str::trim);
    if content_type != Some("application/json") {
        return Err(());
    }
    let status = response.status();
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| ())? {
        if body.len().saturating_add(chunk.len()) > SHUTDOWN_RESPONSE_LIMIT {
            return Err(());
        }
        body.extend_from_slice(&chunk);
    }
    match status {
        reqwest::StatusCode::ACCEPTED => {
            let accepted: AcceptedShutdown = serde_json::from_slice(&body).map_err(|_| ())?;
            (accepted.status == "shutdown_requested"
                && accepted.recovery_required == (accepted.queued + accepted.running > 0))
                .then_some(ShutdownReply::Accepted)
                .ok_or(())
        }
        reqwest::StatusCode::CONFLICT => {
            let error: ShutdownError = serde_json::from_slice(&body).map_err(|_| ())?;
            match error.code.as_str() {
                "desktop_tasks_active" if error.retryable.is_none() => Ok(ShutdownReply::Blocked {
                    queued: error.queued,
                    running: error.running,
                }),
                "desktop_checkpoint_timeout" if error.retryable == Some(true) => {
                    Ok(ShutdownReply::CheckpointTimedOut {
                        queued: error.queued,
                        running: error.running,
                    })
                }
                _ => Err(()),
            }
        }
        _ => Err(()),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AcceptedShutdown {
    status: String,
    queued: u32,
    running: u32,
    recovery_required: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CommittedShutdown {
    status: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ShutdownError {
    code: String,
    queued: u32,
    running: u32,
    retryable: Option<bool>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_wire_is_closed_and_matches_the_web_contract() {
        assert_eq!(
            serde_json::to_value(DesktopExitState::Idle).unwrap(),
            serde_json::json!({"state": "idle"})
        );
        assert_eq!(
            serde_json::to_value(DesktopExitState::Confirm).unwrap(),
            serde_json::json!({"state": "confirm"})
        );
        assert_eq!(
            serde_json::to_value(DesktopExitState::Checking).unwrap(),
            serde_json::json!({"state": "checking"})
        );
        assert_eq!(
            serde_json::to_value(DesktopExitState::Blocked {
                queued: 2,
                running: 1,
            })
            .unwrap(),
            serde_json::json!({"state": "blocked", "queued": 2, "running": 1})
        );
        assert_eq!(
            serde_json::to_value(DesktopExitState::CheckpointTimedOut {
                queued: 2,
                running: 1,
            })
            .unwrap(),
            serde_json::json!({
                "state": "checkpoint_timed_out",
                "queued": 2,
                "running": 1
            })
        );
        assert_eq!(
            serde_json::to_value(DesktopExitState::ShuttingDown).unwrap(),
            serde_json::json!({"state": "shutting_down"})
        );
    }

    #[test]
    fn reducer_deduplicates_requests_and_only_cancels_safe_states() {
        let mut machine = ExitMachine::default();
        assert_eq!(machine.request(), ExitEffect::Emit);
        assert_eq!(machine.request(), ExitEffect::None);
        assert_eq!(machine.confirm(7), ExitEffect::Emit);
        assert_eq!(machine.cancel(), ExitEffect::None);
        assert_eq!(machine.confirm(7), ExitEffect::None);
    }

    #[test]
    fn non_ready_exit_requires_confirmation_before_it_can_commit() {
        let mut machine = ExitMachine::default();
        assert_eq!(machine.confirm_without_ready_session(), ExitEffect::None);
        assert_eq!(machine.request(), ExitEffect::Emit);
        assert_eq!(machine.confirm_without_ready_session(), ExitEffect::Exit);
        assert!(machine.committed);
        assert_eq!(machine.state, DesktopExitState::ShuttingDown);
    }

    #[test]
    fn service_restart_is_allowed_only_without_an_exit_attempt() {
        let controller = DesktopExitController::default();
        assert!(controller.allows_service_restart());
        {
            let mut machine = controller.machine.lock().unwrap();
            machine.state = DesktopExitState::Confirm;
        }
        assert!(!controller.allows_service_restart());
        {
            let mut machine = controller.machine.lock().unwrap();
            machine.state = DesktopExitState::Checking;
        }
        assert!(!controller.allows_service_restart());
        {
            let mut machine = controller.machine.lock().unwrap();
            machine.state = DesktopExitState::ShuttingDown;
        }
        assert!(!controller.allows_service_restart());
    }

    #[test]
    fn active_tasks_are_blocked_with_exact_counts_and_can_be_cancelled() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(3);
        assert_eq!(
            machine.shutdown_reply(
                3,
                ShutdownReply::Blocked {
                    queued: 4,
                    running: 2,
                }
            ),
            ExitEffect::Emit
        );
        assert_eq!(
            machine.state,
            DesktopExitState::Blocked {
                queued: 4,
                running: 2
            }
        );
        assert_eq!(machine.cancel(), ExitEffect::Emit);
        assert_eq!(machine.state, DesktopExitState::Idle);
    }

    #[test]
    fn accepted_shutdown_commits_when_the_sidecar_terminates() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(9);
        assert_eq!(
            machine.shutdown_reply(9, ShutdownReply::Accepted),
            ExitEffect::Emit
        );
        assert_eq!(machine.state, DesktopExitState::ShuttingDown);
        assert!(!machine.committed);
        assert_eq!(machine.terminated(9, Some(0)), ExitEffect::Exit);
        assert!(machine.committed);
    }

    #[test]
    fn terminated_before_202_is_resolved_without_a_race() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(11);
        assert_eq!(machine.terminated(11, Some(0)), ExitEffect::None);
        assert_eq!(
            machine.shutdown_reply(11, ShutdownReply::Accepted),
            ExitEffect::Exit
        );
        assert!(machine.committed);
    }

    #[test]
    fn termination_before_a_conflict_cannot_leave_a_dead_sidecar_as_blocked() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(12);
        machine.terminated(12, Some(0));
        assert_eq!(
            machine.shutdown_reply(
                12,
                ShutdownReply::Blocked {
                    queued: 1,
                    running: 1,
                }
            ),
            ExitEffect::Recover(12)
        );
        assert_eq!(machine.state, DesktopExitState::Idle);
    }

    #[test]
    fn termination_before_checkpoint_timeout_recovers_the_dead_sidecar() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(18);
        machine.terminated(18, Some(1));
        assert_eq!(
            machine.shutdown_reply(
                18,
                ShutdownReply::CheckpointTimedOut {
                    queued: 1,
                    running: 1,
                }
            ),
            ExitEffect::Recover(18)
        );
        assert_eq!(machine.state, DesktopExitState::Idle);
        assert!(!machine.committed);
    }

    #[test]
    fn accepted_shutdown_still_exits_after_nonzero_or_unknown_termination() {
        for code in [Some(1), None] {
            let mut machine = ExitMachine::default();
            machine.request();
            machine.confirm(13);
            machine.shutdown_reply(13, ShutdownReply::Accepted);
            assert_eq!(machine.terminated(13, code), ExitEffect::Exit);
            assert_eq!(machine.state, DesktopExitState::ShuttingDown);
            assert!(machine.committed);
        }
    }

    #[test]
    fn accepted_shutdown_timeout_recovers_without_killing_the_sidecar() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(14);
        machine.shutdown_reply(14, ShutdownReply::Accepted);
        assert_eq!(machine.shutdown_timed_out(14), ExitEffect::Recover(14));
        assert_eq!(machine.state, DesktopExitState::Idle);
        assert!(!machine.committed);
    }

    #[test]
    fn failed_shutdown_request_recovers_without_forcing_exit() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(15);
        assert_eq!(machine.shutdown_failed(15), ExitEffect::Recover(15));
        assert_eq!(machine.state, DesktopExitState::Idle);
        assert!(!machine.committed);
    }

    #[test]
    fn late_generations_cannot_mutate_current_exit_state() {
        let mut machine = ExitMachine::default();
        machine.request();
        machine.confirm(21);
        assert_eq!(
            machine.shutdown_reply(20, ShutdownReply::Accepted),
            ExitEffect::None
        );
        assert_eq!(machine.terminated(20, Some(0)), ExitEffect::None);
        assert_eq!(machine.shutdown_failed(20), ExitEffect::None);
        assert_eq!(machine.state, DesktopExitState::Checking);
        assert!(!machine.committed);
    }

    #[test]
    fn response_parsers_are_exact_and_reject_extension_fields() {
        assert!(serde_json::from_str::<AcceptedShutdown>(
            r#"{"status":"shutdown_requested","queued":0,"running":0,"recovery_required":false}"#
        )
        .is_ok());
        assert!(serde_json::from_str::<AcceptedShutdown>(
            r#"{"status":"shutdown_requested","queued":0,"running":0,"recovery_required":false,"secret":"leak"}"#
        )
        .is_err());
        assert!(serde_json::from_str::<ShutdownError>(
            r#"{"code":"desktop_tasks_active","queued":1,"running":2}"#
        )
        .is_ok());
        assert!(serde_json::from_str::<ShutdownError>(
            r#"{"code":"desktop_tasks_active","queued":-1,"running":2}"#
        )
        .is_err());
    }
}
