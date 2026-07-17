use std::{
    fs::OpenOptions,
    io::Write as _,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::data_root::LocalDataRoot;

const MAX_DIAGNOSTIC_BYTES: usize = 256 * 1024;
const HOST_DIAGNOSTIC_NAME: &str = "stock-desk-host-diagnostic.json";

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticSnapshot {
    schema_version: String,
    created_at: String,
    application: DiagnosticApplication,
    platform: DiagnosticPlatform,
    service_health: DiagnosticServiceHealth,
    configuration: DiagnosticConfiguration,
    events: Vec<DiagnosticEvent>,
    failure_ids: Vec<String>,
    privacy: DiagnosticPrivacy,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticApplication {
    version: String,
    source_revision: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticPlatform {
    system: String,
    architecture: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticServiceHealth {
    sidecar: String,
    storage: String,
    market_worker: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticConfiguration {
    available: bool,
    daily_sources: Vec<String>,
    weekly_sources: Vec<String>,
    minute_sources: Vec<String>,
    instrument_sources: Vec<String>,
    tushare_token_configured: bool,
    local_tdx_configured: bool,
    model_providers: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticEvent {
    timestamp: String,
    level: String,
    component: String,
    event_code: String,
    failure_id: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DiagnosticPrivacy {
    telemetry_enabled: bool,
    automatic_crash_upload: bool,
    automatic_diagnostic_upload: bool,
    stable_device_identifier: bool,
}

impl DiagnosticSnapshot {
    fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != "stock-desk-diagnostic-snapshot-v1"
            || !safe_timestamp(&self.created_at)
            || !safe_version(&self.application.version)
            || self
                .application
                .source_revision
                .as_deref()
                .is_some_and(|revision| !safe_revision(revision))
            || !matches!(self.platform.system.as_str(), "windows" | "other")
            || !matches!(self.platform.architecture.as_str(), "x86_64" | "other")
            || self.service_health.sidecar != "ready"
            || !safe_health(&self.service_health.storage)
            || !safe_health(&self.service_health.market_worker)
            || self.events.len() > 200
            || self.failure_ids.len() > 32
            || self.failure_ids.iter().any(|value| !safe_id(value))
            || has_duplicates(&self.failure_ids)
            || self.privacy.telemetry_enabled
            || self.privacy.automatic_crash_upload
            || self.privacy.automatic_diagnostic_upload
            || self.privacy.stable_device_identifier
        {
            return Err("desktop_diagnostics_invalid_snapshot");
        }
        for sources in [
            &self.configuration.daily_sources,
            &self.configuration.weekly_sources,
            &self.configuration.minute_sources,
            &self.configuration.instrument_sources,
        ] {
            if sources.len() > 8 || sources.iter().any(|value| !safe_id(value)) {
                return Err("desktop_diagnostics_invalid_snapshot");
            }
        }
        if self.configuration.model_providers.len() > 8
            || self.configuration.model_providers.iter().any(|provider| {
                !matches!(
                    provider.as_str(),
                    "deepseek" | "openai_compatible" | "ollama"
                )
            })
            || self.events.iter().any(|event| {
                !safe_timestamp(&event.timestamp)
                    || !matches!(event.level.as_str(), "info" | "warning" | "error")
                    || !safe_id(&event.component)
                    || !safe_id(&event.event_code)
                    || event
                        .failure_id
                        .as_deref()
                        .is_some_and(|value| !safe_id(value))
            })
        {
            return Err("desktop_diagnostics_invalid_snapshot");
        }
        Ok(())
    }
}

fn safe_health(value: &str) -> bool {
    matches!(value, "ready" | "unavailable")
}

fn safe_id(value: &str) -> bool {
    let mut bytes = value.bytes();
    matches!(bytes.next(), Some(b'a'..=b'z'))
        && value.len() <= 96
        && bytes.all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(&byte)
        })
}

fn safe_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b".+-".contains(&byte))
}

fn safe_revision(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn safe_timestamp(value: &str) -> bool {
    value.len() >= 20
        && value.len() <= 32
        && value.contains('T')
        && (value.ends_with('Z') || value.ends_with("+00:00"))
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || b"-:.+TZ".contains(&byte))
}

fn has_duplicates(values: &[String]) -> bool {
    values
        .iter()
        .enumerate()
        .any(|(index, value)| values[..index].contains(value))
}

fn validate_final_bytes(bytes: &[u8]) -> Result<(), &'static str> {
    if bytes.len() > MAX_DIAGNOSTIC_BYTES {
        return Err("desktop_diagnostics_invalid_snapshot");
    }
    let text = std::str::from_utf8(bytes)
        .map_err(|_| "desktop_diagnostics_invalid_snapshot")?
        .to_ascii_lowercase();
    let private_path =
        text.contains("\\users\\") || text.contains("/users/") || text.contains("/home/");
    let credential = text.contains("authorization=")
        || text.contains("authorization:")
        || text.contains("\"password\"")
        || text.contains("\"secret\"")
        || text.contains("\"private_prompt\"")
        || text.contains("\"username\"")
        || text.contains("ghp_")
        || text.contains("sk-proj-")
        || text.contains("sk-ant-");
    if private_path || credential {
        return Err("desktop_diagnostics_invalid_snapshot");
    }
    Ok(())
}

fn validated_snapshot(value: Value) -> Result<String, String> {
    let snapshot: DiagnosticSnapshot = serde_json::from_value(value)
        .map_err(|_| "desktop_diagnostics_invalid_snapshot".to_owned())?;
    snapshot.validate().map_err(str::to_owned)?;
    let mut rendered = serde_json::to_string_pretty(&snapshot)
        .map_err(|_| "desktop_diagnostics_invalid_snapshot".to_owned())?;
    rendered.push('\n');
    validate_final_bytes(rendered.as_bytes()).map_err(str::to_owned)?;
    Ok(rendered)
}

#[tauri::command]
pub fn desktop_validate_diagnostics(snapshot: Value) -> Result<String, String> {
    validated_snapshot(snapshot)
}

#[tauri::command]
pub fn desktop_open_diagnostics(
    local_data_root: tauri::State<'_, LocalDataRoot>,
) -> Result<(), String> {
    let diagnostics = local_data_root
        .path()
        .join("Stock Desk")
        .join("v1.1")
        .join("diagnostics");
    write_host_only_diagnostic(&diagnostics)?;
    open_diagnostics_directory(diagnostics)
}

fn write_host_only_diagnostic(directory: &Path) -> Result<(), String> {
    std::fs::create_dir_all(directory).map_err(|_| "desktop_diagnostics_unavailable".to_owned())?;
    let directory_metadata = std::fs::symlink_metadata(directory)
        .map_err(|_| "desktop_diagnostics_unavailable".to_owned())?;
    if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
        return Err("desktop_diagnostics_unavailable".to_owned());
    }
    let payload = format!(
        concat!(
            "{{\n",
            "  \"schema_version\": \"stock-desk-host-diagnostic-v1\",\n",
            "  \"application_version\": \"{}\",\n",
            "  \"platform\": \"windows-x86_64\",\n",
            "  \"service_health\": \"sidecar_unavailable\",\n",
            "  \"failure_ids\": [\"desktop_sidecar_unavailable\"],\n",
            "  \"privacy\": {{\n",
            "    \"telemetry_enabled\": false,\n",
            "    \"automatic_crash_upload\": false,\n",
            "    \"automatic_diagnostic_upload\": false,\n",
            "    \"stable_device_identifier\": false\n",
            "  }}\n",
            "}}\n"
        ),
        env!("CARGO_PKG_VERSION")
    );
    validate_final_bytes(payload.as_bytes()).map_err(str::to_owned)?;
    let path = directory.join(HOST_DIAGNOSTIC_NAME);
    let temporary = directory.join(format!(
        ".{HOST_DIAGNOSTIC_NAME}.{}.tmp",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&temporary);
    let result = (|| -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(payload.as_bytes())?;
        file.sync_all()?;
        drop(file);
        match std::fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.is_file() || metadata.file_type().is_symlink() => {
                std::fs::remove_file(&path)?;
            }
            Ok(_) => return Err(std::io::Error::other("unsafe diagnostic destination")),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        std::fs::rename(&temporary, &path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result.map_err(|_| "desktop_diagnostics_unavailable".to_owned())
}

#[cfg(windows)]
fn open_diagnostics_directory(path: PathBuf) -> Result<(), String> {
    std::process::Command::new("explorer.exe")
        .arg(path)
        .spawn()
        .map(|_| ())
        .map_err(|_| "desktop_diagnostics_unavailable".to_owned())
}

#[cfg(not(windows))]
fn open_diagnostics_directory(_path: PathBuf) -> Result<(), String> {
    Err("desktop_diagnostics_unsupported".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snapshot() -> Value {
        serde_json::json!({
            "schema_version": "stock-desk-diagnostic-snapshot-v1",
            "created_at": "2026-07-13T08:00:00Z",
            "application": {"version": "1.1.0", "source_revision": "a".repeat(40)},
            "platform": {"system": "windows", "architecture": "x86_64"},
            "service_health": {"sidecar": "ready", "storage": "ready", "market_worker": "unavailable"},
            "configuration": {
                "available": true,
                "daily_sources": ["akshare"],
                "weekly_sources": [],
                "minute_sources": [],
                "instrument_sources": ["akshare"],
                "tushare_token_configured": false,
                "local_tdx_configured": false,
                "model_providers": ["deepseek"]
            },
            "events": [{
                "timestamp": "2026-07-13T08:00:00+00:00",
                "level": "error",
                "component": "sidecar",
                "event_code": "sidecar.unavailable",
                "failure_id": "sidecar_unavailable"
            }],
            "failure_ids": ["sidecar_unavailable"],
            "privacy": {
                "telemetry_enabled": false,
                "automatic_crash_upload": false,
                "automatic_diagnostic_upload": false,
                "stable_device_identifier": false
            }
        })
    }

    #[test]
    fn validates_and_canonically_renders_the_exact_snapshot_contract() {
        let rendered = validated_snapshot(snapshot()).expect("valid snapshot");
        assert!(rendered.ends_with('\n'));
        assert!(rendered.contains("stock-desk-diagnostic-snapshot-v1"));
        assert!(!rendered.contains("Users"));
    }

    #[test]
    fn rejects_expanded_private_or_upload_enabled_snapshots() {
        let mut expanded = snapshot();
        expanded["private_path"] = Value::String(r"C:\Users\Bao\secret".to_owned());
        assert_eq!(
            validated_snapshot(expanded).unwrap_err(),
            "desktop_diagnostics_invalid_snapshot"
        );

        let mut upload = snapshot();
        upload["privacy"]["automatic_diagnostic_upload"] = Value::Bool(true);
        assert_eq!(
            validated_snapshot(upload).unwrap_err(),
            "desktop_diagnostics_invalid_snapshot"
        );
    }

    #[test]
    fn host_only_bundle_contains_no_identity_path_log_or_session_fields() {
        let directory = std::env::temp_dir().join(format!(
            "stock-desk-diagnostics-test-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&directory);
        write_host_only_diagnostic(&directory).expect("host diagnostic");
        let payload = std::fs::read_to_string(directory.join(HOST_DIAGNOSTIC_NAME))
            .expect("host diagnostic bytes");
        assert!(payload.contains("stock-desk-host-diagnostic-v1"));
        for forbidden in ["Users", "username", "session", "logs", "path"] {
            assert!(!payload.contains(forbidden), "found {forbidden}");
        }
        assert!(std::fs::read_dir(&directory)
            .expect("diagnostic directory")
            .all(|entry| !entry
                .expect("diagnostic entry")
                .file_name()
                .to_string_lossy()
                .ends_with(".tmp")));
        let _ = std::fs::remove_dir_all(directory);
    }

    #[cfg(unix)]
    #[test]
    fn host_only_bundle_rejects_a_linked_diagnostics_directory() {
        let root = std::env::temp_dir().join(format!(
            "stock-desk-diagnostics-link-test-{}",
            std::process::id()
        ));
        let target = root.join("target");
        let linked = root.join("diagnostics");
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&target).expect("diagnostic target");
        std::os::unix::fs::symlink(&target, &linked).expect("diagnostic link");

        assert_eq!(
            write_host_only_diagnostic(&linked).unwrap_err(),
            "desktop_diagnostics_unavailable"
        );
        assert!(!target.join(HOST_DIAGNOSTIC_NAME).exists());
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(not(windows))]
    #[test]
    fn opening_diagnostics_is_stably_unsupported_without_disclosing_its_path() {
        let private = std::env::temp_dir().join("private-user").join("Stock Desk");
        let error = open_diagnostics_directory(private.clone()).unwrap_err();
        assert_eq!(error, "desktop_diagnostics_unsupported");
        assert!(!error.contains(private.to_string_lossy().as_ref()));
    }
}
