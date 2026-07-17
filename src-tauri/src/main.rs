#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

mod app;
mod data_root;
mod diagnostics;
mod exit;
mod proxy;
mod sidecar;
mod uninstall;
mod updater;
mod updater_journal;
mod updater_transport;
mod updater_windows;
mod windows_job;

fn focus_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[cfg(any(windows, test))]
const WRY_DEFAULT_BROWSER_ARGUMENTS: &str =
    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection";

#[cfg(any(windows, test))]
fn validated_webview2_remote_debugging_arguments(value: &str) -> Option<String> {
    let port = value.strip_prefix("--remote-debugging-port=")?;
    if port.is_empty() || !port.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let port = port.parse::<u16>().ok()?;
    if port == 0 {
        return None;
    }
    Some(format!("{WRY_DEFAULT_BROWSER_ARGUMENTS} {value}"))
}

#[cfg(windows)]
fn with_webview2_evidence_arguments(
    mut context: tauri::Context<tauri::Wry>,
) -> tauri::Context<tauri::Wry> {
    let Some(arguments) = std::env::var("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
        .ok()
        .and_then(|value| validated_webview2_remote_debugging_arguments(&value))
    else {
        return context;
    };
    if let Some(main_window) = context
        .config_mut()
        .app
        .windows
        .iter_mut()
        .find(|window| window.label == "main")
    {
        main_window.additional_browser_args = Some(arguments);
    }
    context
}

fn main() {
    if let Some(exit_code) = uninstall::dispatch_from_env() {
        std::process::exit(exit_code);
    }

    let context = tauri::generate_context!();
    #[cfg(windows)]
    let context = with_webview2_evidence_arguments(context);

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_main_window(app);
        }))
        .plugin(tauri_plugin_shell::init())
        // The official updater verifier is present, but the Stock Desk
        // controller remains fail-closed until the formal signing and real
        // Windows evidence contract is satisfied.
        .plugin(updater::plugin())
        .invoke_handler(tauri::generate_handler![
            app::desktop_runtime_state,
            app::desktop_restart_service,
            diagnostics::desktop_open_diagnostics,
            diagnostics::desktop_validate_diagnostics,
            proxy::desktop_api_request,
            exit::desktop_request_exit,
            exit::desktop_cancel_exit,
            exit::desktop_confirm_exit,
            updater::desktop_update_state,
            updater::desktop_check_for_updates,
            updater::desktop_dismiss_update,
            updater::desktop_confirm_update
        ])
        .setup(|app| {
            data_root::setup(app)?;
            exit::setup(app);
            updater::setup(app);
            app::setup(app)?;
            focus_main_window(app.handle());
            Ok(())
        })
        .build(context)
        .expect("Stock Desk desktop runtime failed")
        .run(|handle, event| match event {
            tauri::RunEvent::WindowEvent {
                label,
                event: tauri::WindowEvent::CloseRequested { api, .. },
                ..
            } if label == "main" => {
                api.prevent_close();
                exit::request_from_host(handle);
            }
            tauri::RunEvent::ExitRequested { api, .. } if !exit::exit_is_committed(handle) => {
                api.prevent_exit();
                exit::request_from_host(handle);
            }
            _ => {}
        });
}

#[cfg(test)]
mod tests {
    use super::validated_webview2_remote_debugging_arguments;

    #[test]
    fn webview2_remote_debugging_accepts_one_nonzero_loopback_port() {
        assert_eq!(
            validated_webview2_remote_debugging_arguments("--remote-debugging-port=9222"),
            Some(
                "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection \
                 --remote-debugging-port=9222"
                    .to_owned()
            )
        );
    }

    #[test]
    fn webview2_remote_debugging_rejects_unsafe_or_ambiguous_arguments() {
        for value in [
            "",
            "--remote-debugging-port=0",
            "--remote-debugging-port=65536",
            "--remote-debugging-port=9222 --remote-allow-origins=*",
            "--disable-web-security --remote-debugging-port=9222",
            " --remote-debugging-port=9222",
        ] {
            assert_eq!(validated_webview2_remote_debugging_arguments(value), None);
        }
    }
}
