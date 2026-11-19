//! Small JSON stores on this PC for things the server has no API for yet:
//! the Tasks calendar, the Projects board columns, the Library and workflow
//! sketches (owner requests 2026-11-02).
//! The app only reads and writes them; nothing here runs a task.

use std::path::PathBuf;
use crate::ide::home_dir;

/// The only stores the webview may name.
const NAMES: [&str; 4] = ["calendar", "board", "library", "sketches"];

fn data_root_from(
    override_root: Option<std::ffi::OsString>,
    appdata: Option<std::ffi::OsString>,
    home: Option<std::ffi::OsString>,
    windows: bool,
) -> Result<PathBuf, String> {
    if let Some(root) = override_root {
        return Ok(PathBuf::from(root));
    }
    if windows {
        return appdata.map(|root| PathBuf::from(root).join(crate::APP_DATA_DIR)).ok_or_else(|| "APPDATA is not set".into());
    }
    home.map(|home| PathBuf::from(home).join(".local/share/bob-app")).ok_or_else(|| "HOME is not set".into())
}

fn store(name: &str) -> Result<PathBuf, String> {
    if !NAMES.contains(&name) {
        return Err(format!("unknown store: {name}"));
    }
    let root = data_root_from(
        std::env::var_os("BOB_PORTAL_DATA_ROOT"),
        std::env::var_os("APPDATA"),
        std::env::var_os("HOME"),
        cfg!(windows),
    )?;
    std::fs::create_dir_all(&root).map_err(|e| format!("cannot create {}: {e}", root.display()))?;
    Ok(root.join(format!("{name}.json")))
}

/// The saved JSON text, or "null" when nothing has been saved yet.
#[tauri::command(async)]
pub fn store_load(window: tauri::WebviewWindow, name: String) -> Result<String, String> {
    crate::runtime::main_window(&window)?;
    let path = store(&name)?;
    match std::fs::read_to_string(&path) {
        Ok(text) => Ok(text),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok("null".into()),
        Err(e) => Err(format!("cannot read {}: {e}", path.display())),
    }
}

/// Replaces the store. Written to a sibling file first so a crash never leaves half a file.
#[tauri::command(async)]
pub fn store_save(window: tauri::WebviewWindow, name: String, data: String) -> Result<String, String> {
    crate::runtime::main_window(&window)?;
    serde_json::from_str::<serde_json::Value>(&data).map_err(|e| format!("not valid JSON: {e}"))?;
    let path = store(&name)?;
    save_store(&path, &data)?;
    Ok(path.to_string_lossy().into_owned())
}

fn save_store(path: &std::path::Path, data: &str) -> Result<(), String> {
    let temp = path.with_extension("json.tmp");
    std::fs::write(&temp, data).map_err(|e| format!("cannot write {}: {e}", temp.display()))?;
    std::fs::rename(&temp, path).map_err(|e| format!("cannot save {}: {e}", path.display()))
}

/// Starts a Library app's own launch command, detached from this window. The
/// command is the owner's, typed into the Library entry; nothing else runs it.
#[tauri::command(async)]
pub fn library_launch(window: tauri::WebviewWindow, command: String, cwd: Option<String>) -> Result<u32, String> {
    crate::runtime::main_window(&window)?;
    if command.trim().is_empty() {
        return Err("this app has no launch command".into());
    }
    #[cfg(not(windows))]
    let mut cmd = {
        let mut cmd = std::process::Command::new("sh");
        cmd.arg("-c").arg(&command);
        cmd
    };
    #[cfg(windows)]
    let mut cmd = {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::{DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW};
        let mut cmd = std::process::Command::new("cmd.exe");
        cmd.arg("/C").raw_arg(&command);
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
        cmd
    };
    if let Some(dir) = cwd.filter(|dir| !dir.trim().is_empty()) {
        let dir = match dir.strip_prefix("~/") {
            Some(rest) => home_dir()?.join(rest),
            None => PathBuf::from(dir),
        };
        cmd.current_dir(dir);
    }
    cmd.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Its own session, so closing Bob does not take the app down with it.
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
    }
    let child = cmd.spawn().map_err(|e| format!("could not start it: {e}"))?;
    Ok(child.id())
}

#[derive(serde::Serialize)]
pub struct LocalAuth {
    name: &'static str,
    how: &'static str,
    present: bool,
    modified: Option<u64>,
}

/// Which sign-ins this PC holds, by file presence and age only. The files are
/// never opened: their contents are credentials.
#[tauri::command(async)]
pub fn local_auth_status(window: tauri::WebviewWindow, ) -> Result<Vec<LocalAuth>, String> {
    crate::runtime::main_window(&window)?;
    let home = home_dir()?;
    let probe = |name: &'static str, how: &'static str, paths: &[&str]| {
        let found = paths.iter().map(|rel| home.join(rel)).find(|path| path.exists());
        let modified = found
            .as_ref()
            .and_then(|path| std::fs::metadata(path).ok())
            .and_then(|meta| meta.modified().ok())
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|age| age.as_secs());
        LocalAuth { name, how, present: found.is_some(), modified }
    };
    Ok(vec![
        probe("SSH key for the Bob server link", "ssh-keygen / ssh-copy-id (key-only login to the server)", &[".ssh/id_ed25519", ".ssh/id_ecdsa", ".ssh/id_rsa"]),
        probe("Claude Code CLI (Pro subscription)", "run `claude` then /login in a terminal", &[".claude/.credentials.json"]),
        probe("Codex CLI (subscription)", "run `codex login` in a terminal", &[".codex/auth.json"]),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_data_root_wins_on_both_platforms() {
        for windows in [false, true] {
            assert_eq!(data_root_from(Some("chosen".into()), Some("roaming".into()), Some("home".into()), windows).unwrap(), PathBuf::from("chosen"));
            assert_eq!(data_root_from(Some("chosen".into()), None, None, windows).unwrap(), PathBuf::from("chosen"));
        }
    }

    #[test]
    fn windows_data_root_uses_roaming_appdata() {
        assert_eq!(data_root_from(None, Some("roaming".into()), Some("home".into()), true).unwrap(), PathBuf::from("roaming").join(crate::APP_DATA_DIR));
        assert!(data_root_from(None, None, Some("home".into()), true).is_err());
    }

    #[test]
    fn linux_data_root_is_unchanged() {
        assert_eq!(data_root_from(None, Some("roaming".into()), Some("home".into()), false).unwrap(), PathBuf::from("home/.local/share/bob-app"));
        assert!(data_root_from(None, Some("roaming".into()), None, false).is_err());
    }

    // This same test runs on Windows: the second rename must replace an existing file.
    #[test]
    fn saving_twice_atomically_replaces_the_existing_store() {
        let root = std::env::temp_dir().join(format!("bob-store-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("board.json");
        save_store(&path, r#"{"version":1}"#).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), r#"{"version":1}"#);
        save_store(&path, r#"{"version":2}"#).unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), r#"{"version":2}"#);
        assert!(!path.with_extension("json.tmp").exists());
        std::fs::remove_dir_all(root).unwrap();
    }
}
