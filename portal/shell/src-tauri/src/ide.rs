//! Workspace IDE, local side (owner decision 2026-11-02: "both, local first").
//!
//! Folders the owner opens on THIS PC: list, read, write, git status/diff, and
//! real terminals. Server project roots are not reachable from here; they come
//! through the broker once the server grows a files route.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;

use portable_pty::{native_pty_system, CommandBuilder, MasterPty, PtySize};
use serde::Serialize;
use tauri::{AppHandle, Emitter, State};

pub const TERM_EVENT: &str = "ide://term";
const MAX_READ: u64 = 4 * 1024 * 1024;

struct Term {
    master: Box<dyn MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    child: Box<dyn portable_pty::Child + Send + Sync>,
}

#[derive(Default)]
pub struct IdeState {
    terms: Mutex<HashMap<String, Term>>,
}

#[derive(Serialize)]
pub struct Entry {
    name: String,
    path: String,
    dir: bool,
    size: u64,
}

#[derive(Serialize)]
pub struct FileText {
    path: String,
    content: String,
    binary: bool,
    too_large: bool,
}

#[derive(Serialize, Clone)]
struct TermFrame {
    id: String,
    data: Option<String>,
    exit: bool,
}

fn home_dir_from(home: Option<std::ffi::OsString>, userprofile: Option<std::ffi::OsString>) -> Result<PathBuf, String> {
    home.or(userprofile).map(PathBuf::from).ok_or_else(|| "home directory is not set".into())
}

pub(crate) fn home_dir() -> Result<PathBuf, String> {
    home_dir_from(
        std::env::var_os("HOME"),
        if cfg!(windows) { std::env::var_os("USERPROFILE") } else { None },
    )
}

fn absolute(path: &str) -> Result<PathBuf, String> {
    let expanded = if let Some(rest) = path.strip_prefix("~/") {
        home_dir()?.join(rest)
    } else {
        PathBuf::from(path)
    };
    if !expanded.is_absolute() {
        return Err(format!("not an absolute path: {path}"));
    }
    Ok(expanded)
}

/// The desktop's own folder chooser. None = the owner cancelled.
#[tauri::command(async)]
pub fn ide_pick_folder(window: tauri::WebviewWindow, ) -> Result<Option<String>, String> {
    crate::runtime::main_window(&window)?;
    pick_folder()
}

#[cfg(windows)]
fn pick_folder() -> Result<Option<String>, String> {
    use std::os::windows::process::CommandExt;
    use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
    let out = Command::new("powershell.exe")
        .args(["-NoProfile", "-STA", "-Command", r#"
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Open folder in Bob Workspace'
# Without an owner window the chooser can open behind Bob; a topmost owner keeps it in front.
$owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true; ShowInTaskbar = $false }
try {
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::WriteLine($dialog.SelectedPath)
    }
} finally { $dialog.Dispose(); $owner.Dispose() }
"#])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| format!("cannot open folder chooser: {e}"))?;
    if !out.status.success() {
        return Err("Windows folder chooser failed; type the path instead".into());
    }
    let chosen = String::from_utf8_lossy(&out.stdout).trim().to_string();
    Ok((!chosen.is_empty()).then_some(chosen))
}

#[cfg(not(windows))]
fn pick_folder() -> Result<Option<String>, String> {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/".into());
    let tries: [(&str, Vec<&str>); 2] = [
        ("kdialog", vec!["--getexistingdirectory", &home, "--title", "Open folder in Bob Workspace"]),
        ("zenity", vec!["--file-selection", "--directory", "--title=Open folder in Bob Workspace"]),
    ];
    for (program, args) in tries {
        match Command::new(program).args(&args).output() {
            Ok(out) if out.status.success() => {
                let chosen = String::from_utf8_lossy(&out.stdout).trim().to_string();
                return Ok(if chosen.is_empty() { None } else { Some(chosen) });
            }
            Ok(_) => return Ok(None),
            Err(_) => continue,
        }
    }
    Err("no folder chooser (kdialog or zenity) is installed; type the path instead".into())
}

#[tauri::command(async)]
pub fn ide_list_dir(window: tauri::WebviewWindow, path: String) -> Result<Vec<Entry>, String> {
    crate::runtime::main_window(&window)?;
    let root = absolute(&path)?;
    let mut entries: Vec<Entry> = std::fs::read_dir(&root)
        .map_err(|e| format!("cannot list {}: {e}", root.display()))?
        .filter_map(|item| item.ok())
        .filter(|item| item.file_name() != ".git")
        .map(|item| {
            let meta = item.metadata().ok();
            Entry {
                name: item.file_name().to_string_lossy().into_owned(),
                path: item.path().to_string_lossy().into_owned(),
                dir: meta.as_ref().map(|m| m.is_dir()).unwrap_or(false),
                size: meta.map(|m| m.len()).unwrap_or(0),
            }
        })
        .collect();
    entries.sort_by(|a, b| b.dir.cmp(&a.dir).then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase())));
    Ok(entries)
}

#[tauri::command(async)]
pub fn ide_read_file(window: tauri::WebviewWindow, path: String) -> Result<FileText, String> {
    crate::runtime::main_window(&window)?;
    let file = absolute(&path)?;
    let meta = std::fs::metadata(&file).map_err(|e| format!("cannot read {}: {e}", file.display()))?;
    if meta.len() > MAX_READ {
        return Ok(FileText { path, content: String::new(), binary: false, too_large: true });
    }
    let bytes = std::fs::read(&file).map_err(|e| format!("cannot read {}: {e}", file.display()))?;
    match String::from_utf8(bytes) {
        Ok(content) if !content.contains('\0') => Ok(FileText { path, content, binary: false, too_large: false }),
        _ => Ok(FileText { path, content: String::new(), binary: true, too_large: false }),
    }
}

#[tauri::command(async)]
pub fn ide_write_file(window: tauri::WebviewWindow, path: String, content: String) -> Result<(), String> {
    crate::runtime::main_window(&window)?;
    let file = absolute(&path)?;
    std::fs::write(&file, content).map_err(|e| format!("cannot save {}: {e}", file.display()))
}

#[derive(Serialize)]
pub struct GitStatus {
    repo: bool,
    branch: String,
    /// Path relative to the repo root -> porcelain XY code.
    changes: HashMap<String, String>,
    top: String,
}

fn git(root: &Path, args: &[&str]) -> Option<String> {
    // quotepath=false: non-ASCII file names come back as text, not octal escapes.
    let mut cmd = Command::new("git");
    cmd.arg("-C").arg(root).args(["-c", "core.quotepath=false"]).args(args);
    // A GUI-subsystem app would otherwise flash a console window for every git call.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    }
    let out = cmd.output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).into_owned())
}

#[tauri::command(async)]
pub fn ide_git_status(window: tauri::WebviewWindow, root: String) -> Result<GitStatus, String> {
    crate::runtime::main_window(&window)?;
    let dir = absolute(&root)?;
    let Some(top) = git(&dir, &["rev-parse", "--show-toplevel"]) else {
        return Ok(GitStatus { repo: false, branch: String::new(), changes: HashMap::new(), top: String::new() });
    };
    let branch = git(&dir, &["branch", "--show-current"]).unwrap_or_default().trim().to_string();
    let mut changes = HashMap::new();
    for line in git(&dir, &["status", "--porcelain=v1", "-uall"]).unwrap_or_default().lines() {
        if line.len() > 3 {
            let path = line[3..].rsplit(" -> ").next().unwrap_or(&line[3..]).to_string();
            changes.insert(path, line[..2].to_string());
        }
    }
    Ok(GitStatus { repo: true, branch, changes, top: top.trim().to_string() })
}

/// The file as committed at HEAD, for a side-by-side diff. None = untracked or not a repo.
#[tauri::command(async)]
pub fn ide_git_head(window: tauri::WebviewWindow, path: String) -> Result<Option<String>, String> {
    crate::runtime::main_window(&window)?;
    let file = absolute(&path)?;
    let dir = file.parent().ok_or("no parent folder")?.to_path_buf();
    let name = file.file_name().ok_or("no file name")?.to_string_lossy().into_owned();
    // Git's revision paths use forward slashes even when the filesystem does not.
    #[cfg(windows)]
    let name = name.replace('\\', "/");
    Ok(git(&dir, &["show", &format!("HEAD:./{name}")]))
}

#[cfg(unix)]
fn login_shell() -> Option<String> {
    // SAFETY: getpwuid returns a pointer into static storage or null; it is read once, here.
    unsafe {
        let entry = libc::getpwuid(libc::getuid());
        if entry.is_null() || (*entry).pw_shell.is_null() {
            return None;
        }
        let shell = std::ffi::CStr::from_ptr((*entry).pw_shell).to_string_lossy().into_owned();
        (!shell.is_empty()).then_some(shell)
    }
}

#[cfg(windows)]
fn login_shell() -> Option<String> {
    if let Some(path) = std::env::var_os("PATH") {
        if let Some(pwsh) = std::env::split_paths(&path)
            .map(|dir| dir.join("pwsh.exe"))
            .find(|path| path.is_file())
        {
            return Some(pwsh.to_string_lossy().into_owned());
        }
    }
    let root = std::env::var_os("SystemRoot").unwrap_or_else(|| r"C:\Windows".into());
    Some(PathBuf::from(root).join(r"System32\WindowsPowerShell\v1.0\powershell.exe").to_string_lossy().into_owned())
}

#[cfg(not(any(unix, windows)))]
fn login_shell() -> Option<String> {
    None
}

#[tauri::command(async)]
pub fn ide_term_open(window: tauri::WebviewWindow, app: AppHandle, state: State<IdeState>, cwd: String, cols: u16, rows: u16) -> Result<String, String> {
    crate::runtime::main_window(&window)?;
    let dir = absolute(&cwd)?;
    let pair = native_pty_system()
        .openpty(PtySize { rows: rows.max(2), cols: cols.max(10), pixel_width: 0, pixel_height: 0 })
        .map_err(|e| format!("cannot open a terminal: {e}"))?;
    // The owner's login shell, not whatever $SHELL the launcher inherited: a
    // launch from a zsh tool shell otherwise opened zsh and its setup wizard.
    let mut cmd = match login_shell() {
        Some(shell) => {
            let mut built = CommandBuilder::new(&shell);
            built.env("SHELL", &shell);
            built
        }
        None => CommandBuilder::new_default_prog(),
    };
    cmd.cwd(&dir);
    // ConPTY/PowerShell detects its own terminal capabilities; keep Unix hints on Unix.
    #[cfg(not(windows))]
    {
        cmd.env("TERM", "xterm-256color");
        cmd.env("COLORTERM", "truecolor");
    }
    let child = pair.slave.spawn_command(cmd).map_err(|e| format!("cannot start the shell: {e}"))?;
    drop(pair.slave);
    let mut reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;
    let writer = pair.master.take_writer().map_err(|e| e.to_string())?;
    let id = uuid::Uuid::new_v4().simple().to_string();
    state
        .terms
        .lock()
        .map_err(|_| "terminal table poisoned")?
        .insert(id.clone(), Term { master: pair.master, writer, child });
    let tid = id.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 8192];
        // Bytes of a UTF-8 sequence split across reads wait for the next read.
        let mut carry: Vec<u8> = Vec::new();
        loop {
            match reader.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    carry.extend_from_slice(&buf[..n]);
                    let valid = match std::str::from_utf8(&carry) {
                        Ok(_) => carry.len(),
                        Err(e) if e.error_len().is_none() => e.valid_up_to(),
                        Err(_) => carry.len(),
                    };
                    let text = String::from_utf8_lossy(&carry[..valid]).into_owned();
                    carry.drain(..valid);
                    let _ = app.emit(TERM_EVENT, TermFrame { id: tid.clone(), data: Some(text), exit: false });
                }
            }
        }
        let _ = app.emit(TERM_EVENT, TermFrame { id: tid, data: None, exit: true });
    });
    Ok(id)
}

#[tauri::command]
pub fn ide_term_write(window: tauri::WebviewWindow, state: State<IdeState>, id: String, data: String) -> Result<(), String> {
    crate::runtime::main_window(&window)?;
    let mut terms = state.terms.lock().map_err(|_| "terminal table poisoned")?;
    let term = terms.get_mut(&id).ok_or("terminal is closed")?;
    term.writer.write_all(data.as_bytes()).map_err(|e| e.to_string())?;
    term.writer.flush().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn ide_term_resize(window: tauri::WebviewWindow, state: State<IdeState>, id: String, cols: u16, rows: u16) -> Result<(), String> {
    crate::runtime::main_window(&window)?;
    let terms = state.terms.lock().map_err(|_| "terminal table poisoned")?;
    let term = terms.get(&id).ok_or("terminal is closed")?;
    term.master
        .resize(PtySize { rows: rows.max(2), cols: cols.max(10), pixel_width: 0, pixel_height: 0 })
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn ide_term_close(window: tauri::WebviewWindow, state: State<IdeState>, id: String) -> Result<(), String> {
    crate::runtime::main_window(&window)?;
    if let Some(mut term) = state.terms.lock().map_err(|_| "terminal table poisoned")?.remove(&id) {
        let _ = term.child.kill();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn home_resolution_prefers_home_then_userprofile() {
        assert_eq!(home_dir_from(Some("/home/owner".into()), Some("C:\\Users\\owner".into())).unwrap(), PathBuf::from("/home/owner"));
        assert_eq!(home_dir_from(None, Some("C:\\Users\\owner".into())).unwrap(), PathBuf::from("C:\\Users\\owner"));
        assert!(home_dir_from(None, None).is_err());
    }
}
