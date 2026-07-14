#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

mod app;
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

fn main() {
    if let Some(exit_code) = uninstall::dispatch_from_env() {
        std::process::exit(exit_code);
    }

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
            exit::setup(app);
            updater::setup(app);
            app::setup(app)?;
            focus_main_window(app.handle());
            Ok(())
        })
        .build(tauri::generate_context!())
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
