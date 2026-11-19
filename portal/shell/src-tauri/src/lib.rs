#[cfg(not(windows))]
mod bridge;
mod local_store;
#[cfg(not(windows))]
mod functional;
mod ide;
mod portal;
mod recovery;
mod runtime;
mod self_repair;
mod pc;
mod pc_sign;
mod pc_policy;
mod pc_execute;
#[cfg(unix)]
mod portal_unix;
#[cfg(not(windows))]
mod workbench;

/// Folder name for BoB's own data under %APPDATA% / %LOCALAPPDATA% on Windows. It is the
/// bundle identifier, NOT the product name: the per-user installer puts the program in
/// %LOCALAPPDATA%\BoB, and Windows paths ignore case, so "Bob" would mix data into the
/// install folder. The uninstaller's "delete app data" option also looks here.
pub const APP_DATA_DIR: &str = "com.canopyseed.bob";

/// Commands served on this PC on every desktop platform, without a WSL runtime.
pub const LOCAL_COMMANDS: &[&str] = &[
    "ide_pick_folder",
    "ide_list_dir",
    "ide_read_file",
    "ide_write_file",
    "ide_git_status",
    "ide_git_head",
    "ide_term_open",
    "ide_term_write",
    "ide_term_resize",
    "ide_term_close",
    "store_load",
    "store_save",
    "library_launch",
    "local_auth_status",
    "runtime_status",
    "runtime_upgrade_status",
    "runtime_upgrade_start",
    "runtime_repair_status",
    "runtime_repair_start",
    "runtime_repair_restore",
    "pc_poll",
    "pc_decide",
    "pc_set_mode",
    "pc_revoke",
    "runtime_install",
    "runtime_add_lane",
    "runtime_embedder_status",
    "runtime_embedder_install",
    "runtime_model_openai_models",
    "runtime_model_openai_set_model",
    "runtime_model_status",
    "runtime_model_providers",
    "runtime_model_openai_login",
    "runtime_model_claude_login",
    "runtime_model_claude_submit_code",
    "runtime_model_claude_models",
    "runtime_model_claude_set_model",
    "runtime_model_claude_logout",
    "runtime_model_cancel",
    "runtime_model_set_key",
    "runtime_model_set_default",
    "runtime_model_open_url",
];

#[cfg(not(windows))]
use std::sync::Mutex;

#[cfg(not(windows))]
use tauri::{Emitter, Manager, State};

/// The write half of the child bridge, owned by the app state. `None`
/// until `slice_start`, and reset by `slice_shutdown` (which closes the
/// pipe — transport EOF, never a turn cancel, S5 §1).
#[cfg(not(windows))]
type ShellState = Mutex<Option<bridge::SliceStdin>>;

/// Spawn the engine driver (`bob-slice-cli`) and start forwarding its
/// verbatim NDJSON stdout lines to the webview as `slice://line` events
/// (stderr diagnostics as `slice://stderr`, exit code as `slice://exit`).
/// Returns the data root in use (caller-supplied or a fresh temp dir).
#[cfg(not(windows))]
#[tauri::command]
fn slice_start(window: tauri::WebviewWindow, 
    app: tauri::AppHandle,
    state: State<ShellState>,
    data_root: Option<String>,
) -> Result<String, String> {
    crate::runtime::main_window(&window)?;
    {
        let guard = state.lock().expect("shell state lock");
        if guard.is_some() {
            return Err(
                "slice child already running (one connection, one session, one turn — S10)".into(),
            );
        }
    }
    let cli = bridge::default_cli().ok_or_else(|| {
        "bob-slice-cli not found: build the engine first (cargo build at the repo root), or set BOB_SLICE_CLI".to_string()
    })?;
    let fx = bridge::default_fx_read().ok_or_else(|| {
        "fx-read not found: build the engine first (cargo build at the repo root), or set BOB_FX_READ".to_string()
    })?;
    let root = bridge::data_root_for(
        data_root.as_deref(),
        std::env::var("BOB_PREVIEW_DATA_ROOT").ok(),
        std::process::id(),
    );
    let (stdin, rx) = bridge::spawn(&cli, &fx, &root).map_err(|e| format!("spawn failed: {e}"))?;
    *state.lock().expect("shell state lock") = Some(stdin);

    std::thread::spawn(move || {
        for ev in rx {
            let _ = match ev {
                bridge::BridgeEvent::Line(l) => app.emit("slice://line", l),
                bridge::BridgeEvent::Stderr(l) => app.emit("slice://stderr", l),
                bridge::BridgeEvent::Exit(c) => app.emit("slice://exit", c),
            };
        }
    });
    Ok(root.to_string_lossy().into_owned())
}

/// Send one verbatim NDJSON request line to the engine.
#[cfg(not(windows))]
#[tauri::command]
fn slice_send(window: tauri::WebviewWindow, state: State<ShellState>, line: String) -> Result<(), String> {
    crate::runtime::main_window(&window)?;
    let mut guard = state.lock().expect("shell state lock");
    guard
        .as_mut()
        .ok_or_else(|| "no slice child running".to_string())?
        .send_line(&line)
        .map_err(|e| format!("write failed: {e}"))
}

/// Close the transport (drops the child's stdin). The engine treats this
/// as EOF; a pending approval is left open for recovery (E5 exit-code 5
/// posture) — nothing is cancelled or denied here.
#[cfg(not(windows))]
#[tauri::command]
fn slice_shutdown(window: tauri::WebviewWindow, state: State<ShellState>) -> Result<(), String> {
    crate::runtime::main_window(&window)?;
    *state.lock().expect("shell state lock") = None;
    Ok(())
}

#[cfg(not(windows))]
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(std::sync::Arc::new(portal::PortalState::default()))
        .manage(Mutex::new(None::<bridge::SliceStdin>) as ShellState)
        .manage(functional::AppState::default())
        .manage(ide::IdeState::default())
        .invoke_handler({
            let local_handler: fn(tauri::ipc::Invoke<tauri::Wry>) -> bool = tauri::generate_handler![
                slice_start,
                slice_send,
                slice_shutdown,
                functional::app_boot,
                functional::app_begin,
                functional::app_release,
                functional::app_send,
                functional::app_attach,
                functional::app_resolve,
                functional::app_cancel,
                functional::app_new,
                functional::app_open,
                functional::app_developer_mode,
                workbench::workbench_save_project,
                workbench::workbench_collapse_project,
                workbench::workbench_new_project_session,
                workbench::workbench_move_session_group,
                workbench::workbench_snapshot,
                workbench::workbench_select_profile,
                workbench::workbench_configure_standalone,
                workbench::workbench_rename_session,
                workbench::workbench_refresh_models,
                workbench::workbench_select_model,
                workbench::workbench_organize_session,
                workbench::workbench_pick_project,
                workbench::workbench_start_bob_run,
                workbench::workbench_attach_bob_run,
                workbench::workbench_cancel_bob_run,
                ide::ide_pick_folder,
                ide::ide_list_dir,
                ide::ide_read_file,
                ide::ide_write_file,
                ide::ide_git_status,
                ide::ide_git_head,
                ide::ide_term_open,
                ide::ide_term_write,
                ide::ide_term_resize,
                ide::ide_term_close,
                local_store::store_load,
                local_store::store_save,
                local_store::library_launch,
                local_store::local_auth_status,
                runtime::runtime_status,
                runtime::runtime_upgrade_status,
                runtime::runtime_upgrade_start,
                self_repair::runtime_repair_status,
                self_repair::runtime_repair_start,
                self_repair::runtime_repair_restore,
                pc::pc_poll,
                pc::pc_decide,
                pc::pc_set_mode,
                pc::pc_revoke,
                runtime::runtime_install,
                runtime::runtime_add_lane,
                runtime::runtime_embedder_status,
                runtime::runtime_embedder_install,
                runtime::runtime_model_openai_models,
                runtime::runtime_model_openai_set_model,
                runtime::runtime_model_status,
                runtime::runtime_model_providers,
                runtime::runtime_model_openai_login,
                runtime::runtime_model_claude_login,
                runtime::runtime_model_claude_submit_code,
                runtime::runtime_model_claude_models,
                runtime::runtime_model_claude_set_model,
                runtime::runtime_model_claude_logout,
                runtime::runtime_model_cancel,
                runtime::runtime_model_set_key,
                runtime::runtime_model_set_default,
                runtime::runtime_model_open_url
            ];
            move |invoke: tauri::ipc::Invoke| {
                let command = invoke.message.command().to_owned();
                if !portal::PORTAL_COMMANDS.contains(&command.as_str()) {
                    return local_handler(invoke);
                }
                if invoke.message.webview_ref().label() != "main" {
                    invoke
                        .resolver
                        .reject("only the main webview may invoke portal commands");
                    return true;
                }
                let args = match invoke.message.payload() {
                    tauri::ipc::InvokeBody::Json(value) => value.clone(),
                    _ => {
                        invoke.resolver.reject("binary IPC unsupported");
                        return true;
                    }
                };
                let (app, state) = {
                    let webview = invoke.message.webview();
                    (
                        webview.app_handle().clone(),
                        webview
                            .state::<std::sync::Arc<portal::PortalState>>()
                            .inner()
                            .clone(),
                    )
                };
                std::thread::spawn(
                    move || match portal::dispatch(&state, &app, &command, args) {
                        Ok(value) => invoke.resolver.resolve(value),
                        Err(error) => invoke.resolver.reject(error),
                    },
                );
                true
            }
        })
        .on_window_event(|window, event| {
            if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
                runtime::cancel_model_login();
                window
                    .state::<std::sync::Arc<portal::PortalState>>()
                    .inner()
                    .close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running bob-slice-shell");
}

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::run;

#[cfg(test)]
mod local_command_tests {
    use super::LOCAL_COMMANDS;
    use std::collections::BTreeSet;

    fn handler_commands(source: &str) -> BTreeSet<&str> {
        source.split(concat!("tauri::", "generate_handler![")).skip(1)
            .flat_map(|handler| handler.split_once(']').expect("handler list must close").0.split(','))
            .map(str::trim)
            .filter(|entry| entry.starts_with("ide::") || entry.starts_with("local_store::") || entry.starts_with("runtime::") || entry.starts_with("self_repair::") || entry.starts_with("crate::self_repair::") || entry.starts_with("pc::") || entry.starts_with("crate::pc::"))
            .map(|entry| entry.rsplit("::").next().unwrap())
            .collect()
    }

    #[test]
    fn app_data_dir_is_the_bundle_identifier_not_the_install_folder() {
        let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        assert_eq!(config["identifier"], super::APP_DATA_DIR);
        assert_eq!(super::APP_DATA_DIR, "com.canopyseed.bob");
        assert_eq!(config["bundle"]["publisher"], "Canopy Seed");
        assert_eq!(config["version"], "0.99.1");
        let product = config["productName"].as_str().unwrap();
        assert_ne!(product.to_lowercase(), super::APP_DATA_DIR.to_lowercase());
    }

    #[test]
    fn every_local_command_is_registered_on_linux_and_windows() {
        let expected: BTreeSet<_> = LOCAL_COMMANDS.iter().copied().collect();
        assert_eq!(expected.len(), LOCAL_COMMANDS.len(), "duplicate local command");
        assert_eq!(handler_commands(include_str!("lib.rs")), expected, "Linux handlers");
        assert_eq!(handler_commands(include_str!("windows.rs")), expected, "Windows handlers");

        let declared: BTreeSet<_> = [include_str!("ide.rs"), include_str!("local_store.rs"), include_str!("runtime.rs"), include_str!("self_repair.rs"), include_str!("pc.rs")]
            .into_iter()
            .flat_map(|source| source.split("#[tauri::command").skip(1))
            .map(|command| {
                command.split_once("fn ").expect("command must be a function").1
                    .split('(').next().unwrap().trim()
            })
            .collect();
        assert_eq!(declared, expected, "every ide/local_store/runtime command must be in LOCAL_COMMANDS");
        for command in include_str!("ide.rs").split("#[tauri::command").skip(1) {
            let declaration = command.split_once("fn ").unwrap().1;
            let name = declaration.split('(').next().unwrap().trim();
            assert!(expected.contains(name), "{name} must be registered");
            let (signature, body) = declaration.split_once('{').unwrap();
            assert!(signature.contains("window: tauri::WebviewWindow"), "{name}: caller window required");
            assert!(body.trim_start().starts_with("crate::runtime::main_window(&window)?;"), "{name}: check before any side effect");
            assert!(crate::runtime::main_window_label("main").is_ok(), "{name}");
            for label in ["preview", "signin", "second", "Main", ""] {
                assert!(crate::runtime::main_window_label(label).is_err(), "{name} must refuse {label}");
            }
        }
    }
    #[test]
    fn shipped_csp_only_runs_local_scripts_and_uses_ipc_connections() {
        let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let policy = config["app"]["security"]["csp"].as_str().unwrap();
        let directives: BTreeSet<_> = policy.split(';').map(str::trim).collect();
        for rule in ["default-src 'none'", "script-src 'self'", "connect-src ipc: http://ipc.localhost", "worker-src 'self'", "frame-src 'self'", "object-src 'none'", "base-uri 'none'", "form-action 'none'"] {
            assert!(directives.contains(rule), "missing {rule}");
        }
        assert!(!policy.contains("unsafe-eval") && !policy.contains("https:") && !policy.contains("ws:"));
    }

}
