//! First-run WSL provisioning. Fixed argv, bounded children, no shell or password diagnostics.
#![cfg_attr(not(windows), allow(dead_code, unused_imports))]
use crate::portal::{self, PortalState};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};

/// Automatic install/upgrade/lane creation drive WSL. On Linux the runtime is a container the
/// operator starts; say so instead of surfacing a raw platform error code.
const WINDOWS_ONLY: &str = "Automatic runtime install, upgrade and lane creation are available on Windows. On Linux, start the Bob runtime container and select its lane in Settings → Connection.";
pub(crate) static PROVISIONING: Mutex<()> = Mutex::new(());
const SHORT: Duration = Duration::from_secs(20);
const FIRSTBOOT: Duration = Duration::from_secs(180);
/// After each boot WSL opens a login session for a uid's first command and waits up to its
/// `boot.initTimeout` (10 s) for `user@<uid>.service`, one client at a time. Killing a `wsl.exe`
/// that is still inside that wait while another session (the broker) holds the distro crashes
/// WSL init: every later command fails with Wsl/Service/E_UNEXPECTED until the distro is
/// terminated (reports/wsl-coldboot-trials.jsonl, `kill_pending_held`). A session command is
/// therefore protected for a full minute, including boot and queued uid waits.
/// This is also the single age threshold used by host, portal and sign-in cleanup.
pub(crate) const SESSION_FLOOR: Duration = Duration::from_secs(60);
pub(crate) fn session_budget(args: &[String], budget: Duration) -> Duration {
    if args.iter().any(|arg| arg == "--exec") {
        budget.max(SESSION_FLOOR)
    } else {
        budget
    }
}
#[path = "runtime_upgrade.rs"]
mod upgrade;

#[tauri::command]
pub async fn runtime_upgrade_status(app: AppHandle, window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    #[cfg(windows)]
    { tauri::async_runtime::spawn_blocking(move || upgrade::status(&app)).await.map_err(|_| "runtime upgrade status task failed")? }
    #[cfg(not(windows))]
    { let _ = app; Err(WINDOWS_ONLY.into()) }
}
#[tauri::command]
pub async fn runtime_upgrade_start(app: AppHandle, window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    #[cfg(windows)]
    { tauri::async_runtime::spawn_blocking(move || upgrade::start(&app)).await.map_err(|_| "runtime upgrade start task failed")? }
    #[cfg(not(windows))]
    { let _ = app; Err(WINDOWS_ONLY.into()) }
}

fn distro_names(bytes: &[u8]) -> Result<Vec<String>, String> {
    if bytes.len() % 2 != 0 {
        return Err("invalid UTF-16 WSL distro list".into());
    }
    let words: Vec<u16> = bytes
        .chunks_exact(2)
        .map(|b| u16::from_le_bytes([b[0], b[1]]))
        .collect();
    let text = String::from_utf16(&words).map_err(|_| "invalid UTF-16 WSL distro list")?;
    Ok(text
        .trim_start_matches('\u{feff}')
        .lines()
        .map(|s| s.trim_matches(|c: char| c.is_whitespace() || c == '\0'))
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect())
}
fn wsl_state(ok: bool, code: Option<i32>, output: &[u8]) -> &'static str {
    if ok {
        return "ok";
    }
    match code.map(|c| c as u32) {
        Some(0x8007019e | 0x80070002) => return "missing",
        Some(0x80370102 | 0x800701bc | 0x8007013a) => return "needs_wsl2",
        _ => {}
    }
    let words: Vec<u16> = output
        .chunks_exact(2)
        .map(|b| u16::from_le_bytes([b[0], b[1]]))
        .collect();
    let text = format!(
        "{} {}",
        String::from_utf16_lossy(&words),
        String::from_utf8_lossy(output)
    )
    .to_ascii_lowercase();
    if text.contains("0x8007019e") || text.contains("wsl --install") {
        "missing"
    } else if ["0x80370102", "0x800701bc", "0x8007013a", "wsl2", "wsl 2"]
        .iter()
        .any(|s| text.contains(s))
    {
        "needs_wsl2"
    } else {
        "error"
    }
}

fn exec_args(user: Option<&str>, command: &[&str]) -> Vec<String> {
    let mut args = vec!["-d".into(), crate::self_repair::distro().into()];
    if let Some(user) = user {
        args.extend(["-u".into(), user.into()]);
    }
    args.push("--exec".into());
    args.extend(command.iter().map(|s| s.to_string()));
    args
}
fn import_args(destination: &str, tar: &str) -> Vec<String> {
    ["--import", "BoB", destination, tar, "--version", "2"]
        .map(String::from)
        .to_vec()
}
fn lane_args(name: &str) -> Result<Vec<String>, String> {
    if !portal::valid_lane(name) {
        return Err("invalid lane: use [a-z][a-z0-9-]{0,31}".into());
    }
    Ok(exec_args(
        Some("root"),
        &["bob-lane", "add", name, "--password-stdin"],
    ))
}
fn parse_lane(text: &str, name: &str) -> Result<Value, String> {
    let uris: Vec<_> = text
        .lines()
        .filter(|s| s.starts_with("otpauth://"))
        .collect();
    // Runtimes since 32a631e print "Configured" (add is idempotent); older ones print "Added".
    let prefixes = [format!("Configured {name}: "), format!("Added {name}: ")];
    let summaries: Vec<_> = text
        .lines()
        .filter_map(|s| prefixes.iter().find_map(|p| s.strip_prefix(p.as_str())))
        .collect();
    if uris.len() != 1
        || summaries.len() != 1
        || uris[0].len() <= "otpauth://".len()
        || uris[0].chars().any(char::is_whitespace)
    {
        return Err(
            "lane command returned an invalid receipt; inspect the runtime before retrying".into(),
        );
    }
    let port = |field: &str| -> Result<u16, String> {
        summaries[0]
            .split_whitespace()
            .find_map(|s| s.strip_prefix(field))
            .and_then(|s| s.parse::<u16>().ok())
            .filter(|p| *p != 0)
            .ok_or_else(|| {
                "lane command returned invalid ports; inspect the runtime before retrying".into()
            })
    };
    let core = port("core=")?;
    let gateway = port("gateway=")?;
    port("bridge=")?;
    Ok(json!({"otpauth": uris[0], "core": core, "gateway": gateway}))
}

// Wipe the owned stdin buffer on success, write failure, or spawn failure.
struct SecretInput(Vec<u8>);
impl Drop for SecretInput {
    fn drop(&mut self) {
        self.0.fill(0);
        self.0.clear();
    }
}
pub(crate) fn bounded_output(
    mut command: std::process::Command,
    secret: Option<String>,
    budget: Duration,
) -> Result<(bool, Vec<u8>, Option<i32>), String> {
    use std::{
        io::{Read, Write},
        process::Stdio,
        sync::mpsc,
    };
    let secret = secret.map(|s| SecretInput(s.into_bytes()));
    #[cfg(windows)]
    let started = (crate::windows::is_wsl_program(command.get_program()) && command.get_args().any(|a|a == "--exec")).then(Instant::now);
    let mut child = command
        .stdin(if secret.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| {
            match (e.kind() == std::io::ErrorKind::NotFound, cfg!(windows)) {
                (true, true) => "wsl_missing",
                (false, true) => "WSL process could not start",
                (true, false) => "The container engine (docker or podman) was not found on this PC",
                (false, false) => "The container engine process could not start",
            }
        })?;
    let deadline = Instant::now() + budget;
    if let Some(mut secret) = secret {
        let mut stdin = child.stdin.take().ok_or("WSL stdin unavailable")?;
        std::thread::spawn(move || {
            let _ = stdin
                .write_all(&secret.0)
                .and_then(|_| stdin.write_all(b"\n"));
            secret.0.fill(0);
            secret.0.clear();
        });
    }
    let mut stdout = child.stdout.take().ok_or("WSL stdout unavailable")?;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout
            .by_ref()
            .take(1024 * 1024 + 1)
            .read_to_end(&mut bytes);
        let _ = tx.send(if result.is_ok() && bytes.len() <= 1024 * 1024 {
            Ok(bytes)
        } else {
            Err("invalid WSL output")
        });
    });
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return rx
                    .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                    .map_err(|_| format!("{} output timed out", if cfg!(windows) { "WSL" } else { "Runtime" }))?
                    .map(|bytes| (status.success(), bytes, status.code()))
                    .map_err(String::from)
            }
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(100)),
            _ => {
                #[cfg(windows)]
                crate::windows::end_session(&mut Some(child), started, Duration::ZERO);
                #[cfg(not(windows))]
                { let _ = child.kill(); let _ = child.wait(); }
                return Err(
                    "WSL command failed or timed out; inspect runtime status before retrying"
                        .into(),
                );
            }
        }
    }
}

/// Run one fixed root operation in the local runtime: WSL on Windows, the validated
/// container engine on Linux. Callers pass words from the closed builders only.
fn admin_run(words: &[String], secret: Option<String>, budget: Duration) -> Result<(bool, Vec<u8>, Option<i32>), String> {
    #[cfg(windows)]
    {
        let refs: Vec<&str> = words.iter().map(String::as_str).collect();
        platform::wsl(&exec_args(Some("root"), &refs), secret, budget)
    }
    #[cfg(not(windows))]
    {
        let (engine, container) = portal::runtime_engine_container()?;
        let mut command = std::process::Command::new(engine);
        command.args(unix_admin_args(&container, words)?);
        bounded_output(command, secret, budget)
    }
}

#[cfg(windows)]
pub(crate) mod platform {
    use super::*;
    use std::os::windows::process::CommandExt;
    use std::process::Command;

    pub fn wsl(
        args: &[String],
        secret: Option<String>,
        budget: Duration,
    ) -> Result<(bool, Vec<u8>, Option<i32>), String> {
        let program = std::path::PathBuf::from(
            std::env::var_os("SystemRoot").ok_or("SystemRoot unavailable")?,
        )
        .join("System32/wsl.exe");
        let mut command = Command::new(program);
        command.args(args).creation_flags(0x0800_0000);
        bounded_output(command, secret, session_budget(args, budget))
    }
    fn checked(args: Vec<String>, budget: Duration) -> Result<Vec<u8>, String> {
        let (ok, bytes, _) = wsl(&args, None, budget)?;
        if ok {
            Ok(bytes)
        } else {
            Err("WSL command failed; inspect runtime status before retrying".into())
        }
    }
    fn firstboot(budget: Duration) -> Result<bool, String> {
        wsl(
            &exec_args(
                Some("root"),
                &["test", "-f", "/var/lib/bob/.firstboot-complete"],
            ),
            None,
            budget,
        )
        .map(|r| r.0)
    }
    pub fn status() -> Result<Value, String> {
        let (ok, output, code) = match wsl(&["--status".into()], None, SHORT) {
            Ok(value) => value,
            Err(e) => {
                return Ok(
                    json!({"wsl": if e == "wsl_missing" { "missing" } else { "error" },
                "distro_installed": false, "firstboot": false, "lanes": [], "healthy": false, "detail": e}),
                )
            }
        };
        let wsl_state = wsl_state(ok, code, &output);
        let mut result = json!({"wsl": wsl_state, "distro_installed": false, "firstboot": false,
            "lanes": [], "healthy": false, "detail": ""});
        if wsl_state != "ok" {
            result["detail"] =
                json!("WSL is not ready. Install/update WSL 2 and reboot, then Retry.");
            return Ok(result);
        }
        let names = match checked(vec!["-l".into(), "-q".into()], SHORT)
            .and_then(|bytes| distro_names(&bytes))
        {
            Ok(names) => names,
            Err(error) => {
                result["wsl"] = json!("error");
                result["detail"] = json!(error);
                return Ok(result);
            }
        };
        if !names.iter().any(|s| s.eq_ignore_ascii_case("BoB")) {
            return Ok(result);
        }
        result["distro_installed"] = json!(true);
        match firstboot(SHORT) {
            Ok(complete) => result["firstboot"] = json!(complete),
            Err(error) => {
                result["detail"] = json!(error);
                return Ok(result);
            }
        }
        match checked(exec_args(Some("root"), &["bob-health"]), SHORT).and_then(|bytes| {
            serde_json::from_slice::<Value>(&bytes)
                .map_err(|_| "bob-health returned invalid JSON".into())
        }) {
            Ok(health) if health["healthy"].is_boolean() && health["lanes"].is_object() => {
                result["healthy"] = health["healthy"].clone();
                result["lanes"] = json!(health["lanes"]
                    .as_object()
                    .unwrap()
                    .keys()
                    .collect::<Vec<_>>());
            }
            Ok(_) => result["detail"] = json!("bob-health returned an invalid health receipt"),
            Err(e) => result["detail"] = json!(e),
        }
        Ok(result)
    }
    pub fn install(app: &AppHandle) -> Result<Value, String> {
        let _guard = PROVISIONING
            .try_lock()
            .map_err(|_| "runtime provisioning already in progress")?;
        let progress = |step: &str, detail: &str| {
            let _ = app.emit(
                "runtime://progress",
                json!({"step": step, "detail": detail}),
            );
        };
        progress("check", "Checking WSL and existing distributions");
        let before = status()?;
        if before["distro_installed"] == true {
            return Err("BoB already exists; refusing to overwrite it".into());
        }
        if before["wsl"] != "ok" {
            return Err("WSL 2 must be ready before installation".into());
        }
        progress("verify", "Checking the bundled runtime for damage");
        let (tar, bundled) = upgrade::bundle(app).map_err(|_| upgrade::DAMAGED_INSTALL.to_string())?;
        upgrade::verify_install_archive(&tar, &bundled)?;
        let destination = std::path::PathBuf::from(
            std::env::var_os("LOCALAPPDATA").ok_or("LOCALAPPDATA unavailable")?,
        )
        .join(crate::APP_DATA_DIR)
        .join("wsl")
        .join("BoB");
        std::fs::create_dir_all(destination.parent().unwrap())
            .map_err(|_| "cannot create runtime parent directory")?;
        progress("import", "Installing Bob on this PC");
        // Repeat the list immediately before import; --import itself also refuses an existing name.
        if distro_names(&checked(vec!["-l".into(), "-q".into()], SHORT)?)?
            .iter()
            .any(|s| s.eq_ignore_ascii_case("BoB"))
        {
            return Err("BoB already exists; refusing to overwrite it".into());
        }
        checked(
            import_args(
                destination.to_str().ok_or("runtime path must be UTF-8")?,
                tar.to_str().ok_or("resource path must be UTF-8")?,
            ),
            Duration::from_secs(180),
        )?;
        progress("start", "Starting Bob for the first time");
        checked(exec_args(None, &["true"]), SHORT)?;
        progress("firstboot", "Waiting for Bob's first boot");
        let deadline = Instant::now() + FIRSTBOOT;
        while Instant::now() < deadline {
            if firstboot(SHORT.min(deadline.saturating_duration_since(Instant::now())))? {
                progress("ready", "Bob is ready for your name and password");
                return status();
            }
            std::thread::sleep(
                Duration::from_secs(2).min(deadline.saturating_duration_since(Instant::now())),
            );
        }
        Err("Bob first boot timed out after 180 seconds. The distribution is preserved; Retry status to inspect it.".into())
    }
    pub fn add_lane(app: &AppHandle, name: String, password: String) -> Result<Value, String> {
        let _guard = PROVISIONING
            .try_lock()
            .map_err(|_| "runtime provisioning already in progress")?;
        let args = lane_args(&name)?;
        if password.chars().count() < 10 || password.contains(['\r', '\n', '\0']) {
            return Err(
                "password must be at least 10 characters without line breaks or NUL".into(),
            );
        }
        let (ok, output, _) = wsl(&args, Some(password), Duration::from_secs(120))?;
        if !ok {
            return Err("lane creation failed; inspect runtime status before retrying".into());
        }
        let receipt = parse_lane(
            std::str::from_utf8(&output).map_err(|_| "invalid lane receipt encoding")?,
            &name,
        )?;
        app.state::<Arc<PortalState>>().set_connection(&portal::data_root(), &json!({"mode": "this_pc", "lane": name}))
            .map_err(|_| "lane was created but connection settings could not be saved; select this lane in Settings")?;
        Ok(receipt)
    }
}

pub(crate) fn main_window(window: &tauri::WebviewWindow) -> Result<(), String> {
    main_window_label(window.label())
}
pub(crate) fn main_window_label(label: &str) -> Result<(), String> {
    if label == "main" {
        Ok(())
    } else {
        Err("only the main webview may provision the runtime".into())
    }
}
#[tauri::command]
pub async fn runtime_status(window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    #[cfg(windows)]
    {
        tauri::async_runtime::spawn_blocking(platform::status)
            .await
            .map_err(|_| "runtime status task failed")?
    }
    #[cfg(not(windows))]
    {
        Err(WINDOWS_ONLY.into())
    }
}
#[tauri::command]
pub async fn runtime_install(
    app: AppHandle,
    window: tauri::WebviewWindow,
) -> Result<Value, String> {
    main_window(&window)?;
    #[cfg(windows)]
    {
        tauri::async_runtime::spawn_blocking(move || platform::install(&app))
            .await
            .map_err(|_| "runtime install task failed")?
    }
    #[cfg(not(windows))]
    {
        let _ = app;
        Err(WINDOWS_ONLY.into())
    }
}
#[tauri::command]
pub async fn runtime_add_lane(
    app: AppHandle,
    window: tauri::WebviewWindow,
    name: String,
    password: String,
) -> Result<Value, String> {
    main_window(&window)?;
    #[cfg(windows)]
    {
        tauri::async_runtime::spawn_blocking(move || platform::add_lane(&app, name, password))
            .await
            .map_err(|_| "runtime lane task failed")?
    }
    #[cfg(not(windows))]
    {
        let _ = (app, name, password);
        Err(WINDOWS_ONLY.into())
    }
}

// Model setup uses the selected local lane. Never accept a lane or executable from the webview.
fn model_lane() -> Result<String, String> {
    portal::local_model_lane()
}
const MODEL_PROVIDERS: &[(&str, &str, &str)] = &[
    ("openai", "OpenAI (ChatGPT)", "signin"),
    ("claude", "Claude (subscription)", "signin"),
    ("openai", "OpenAI", "key"),
    ("anthropic", "Anthropic", "key"),
    ("deepseek", "DeepSeek", "key"),
    ("google", "Google", "key"),
    ("glm", "GLM (Z.ai)", "key"),
    ("kimi", "Kimi (Moonshot)", "key"),
    ("qwen", "Qwen (Alibaba Model Studio)", "key"),
];
fn model_providers_receipt(rows: &[(&str, &str, &str)]) -> Result<Value, String> {
    if rows.is_empty() || rows.len() > 16 {
        return Err("Invalid provider metadata size".into());
    }
    let mut seen = std::collections::HashSet::new();
    let mut providers = Vec::new();
    for &(id, label, method) in rows {
        if id.is_empty()
            || id.len() > 40
            || !id
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"_-".contains(&c))
            || label.is_empty()
            || label.len() > 128
            || label.chars().any(char::is_control)
            || !matches!(method, "signin" | "key")
            || !seen.insert((id, method))
            || (method == "signin" && !matches!(id, "openai" | "claude"))
        {
            return Err("Invalid provider metadata".into());
        }
        providers.push(json!({"id":id,"label":label,"method":method}));
    }
    Ok(json!(providers))
}
#[tauri::command]
pub fn runtime_model_providers(window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    model_providers_receipt(MODEL_PROVIDERS)
}
fn model_args(action: &str, lane: &str, value: Option<&str>) -> Result<Vec<String>, String> {
    model_words(action, lane, value).map(|words| exec_args(Some("root"), &words.iter().map(String::as_str).collect::<Vec<_>>()))
}
fn model_words(action: &str, lane: &str, value: Option<&str>) -> Result<Vec<String>, String> {
    if !portal::valid_lane(lane) {
        return Err("invalid model lane".into());
    }
    let mut command = vec!["bob-model", action, lane];
    match (action, value) {
        ("status" | "openai-login" | "openai-models" | "claude-login" | "claude-models" | "claude-logout", None) => {}
        ("set-key", Some(provider))
            if MODEL_PROVIDERS
                .iter()
                .any(|&(id, _, method)| id == provider && method == "key") =>
        {
            model_providers_receipt(MODEL_PROVIDERS)?;
            command.push(provider)
        }
        ("claude-set-model", Some(model @ ("default" | "fable" | "opus" | "sonnet"))) => command.push(model),
        ("set-default" | "openai-set-model", Some(backend))
            if !backend.is_empty()
                && backend.len() <= 128
                && !backend.starts_with('-')
                && backend
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c)) =>
        {
            command.push(backend)
        }
        _ => return Err("invalid model action or provider".into()),
    }
    Ok(command.into_iter().map(String::from).collect())
}
fn valid_signin_url(url: &str) -> bool {
    if tauri::Url::parse(url).ok().is_some_and(|p| matches!(p.host_str(), Some("claude.com" | "claude.ai"))) {
        valid_claude_signin_url(url)
    } else { valid_chatgpt_signin_url(url) }
}
fn valid_chatgpt_signin_url(url: &str) -> bool {
    let Ok(parsed) = tauri::Url::parse(url) else {
        return false;
    };
    parsed.scheme() == "https"
        && url.starts_with("https://")
        && parsed.host_str() == Some("auth.openai.com")
        && parsed.username().is_empty()
        && parsed.password().is_none()
        && url.len() <= 2048
        && url
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b":/._-?=+~".contains(&c))
}

fn valid_claude_signin_url(url: &str) -> bool {
    let Ok(parsed) = tauri::Url::parse(url) else { return false; };
    url.starts_with("https://") && parsed.scheme() == "https"
        && matches!(parsed.host_str(), Some("claude.com" | "claude.ai"))
        && parsed.username().is_empty() && parsed.password().is_none()
        && parsed.port().is_none() && parsed.fragment().is_none()
        && matches!(url[8..].split(['/', '?', '#']).next(), Some("claude.com" | "claude.ai"))
        && url.len() <= 8192
        && url.bytes().all(|c| c.is_ascii_graphic() && ![34, 60, 62, 92, 96].contains(&c))
}
fn valid_claude_code(code: &str) -> bool {
    !code.is_empty() && code.len() <= 4096
        && code.bytes().all(|c| c.is_ascii_alphanumeric() || b"._~+/#=%:-".contains(&c))
}
// Never forward helper prose that could echo the sensitive input.
fn claude_model_event(line: &[u8]) -> Result<Value, String> {
    let value: Value = serde_json::from_slice(line).map_err(|_| "Invalid model sign-in JSON line")?;
    match value["event"].as_str() {
        Some("signin_link") => {
            let url = value["url"].as_str().filter(|s| valid_claude_signin_url(s)).ok_or("Invalid sign-in URL")?;
            Ok(json!({"event":"signin_link", "url":url}))
        }
        Some("done") if value["ok"] == true => Ok(json!({"event":"done", "ok":true})),
        Some("error") => {
            let code = value["code"].as_str().unwrap_or("");
            let detail = match code {
                "signin_unreachable" => "Could not reach the sign-in service through the lane proxy.",
                "signin_refused" => "The sign-in service refused the request.",
                "signin_timeout" => "The sign-in timed out.",
                "signin_cancelled" => "The sign-in was cancelled.",
                "signin_not_stored" => "The sign-in finished but no usable sign-in was stored.",
                "signin_unavailable" => "The sign-in helper or lane proxy is unavailable.",
                "signin_failed" => "The sign-in failed; try again.",
                "signin_code_rejected" => "The sign-in code was wrong or expired. Start sign-in again.",
                "signin_in_progress" => "A sign-in is already in progress for this lane.",
                _ => return Ok(json!({"event":"error", "detail":"Sign-in stopped."})),
            };
            Ok(json!({"event":"error", "code":code, "detail":detail}))
        }
        _ => Err("Invalid model sign-in event".into()),
    }
}

fn model_event(line: &[u8]) -> Result<Value, String> {
    let value: Value =
        serde_json::from_slice(line).map_err(|_| "Invalid model sign-in JSON line")?;
    match value["event"].as_str() {
        Some("device_code") => {
            let url = value["url"]
                .as_str()
                .filter(|s| valid_chatgpt_signin_url(s))
                .ok_or("Invalid sign-in URL")?;
            let code = value["code"]
                .as_str()
                .filter(|s| {
                    !s.is_empty()
                        && s.len() <= 128
                        && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
                })
                .ok_or("Invalid device code")?;
            Ok(json!({"event":"device_code", "url":url, "code":code}))
        }
        Some("done") if value["ok"] == true => {
            let mut receipt = json!({"event":"done", "ok":true});
            if let Some(state) = value.get("chatgpt") { receipt["chatgpt"] = chatgpt_state_receipt(state)?; }
            if value.get("model_notice").is_some() { receipt["model_notice"] = json!("Signed in. Choose a ChatGPT model in Settings."); }
            Ok(receipt)
        },
        Some("error") => {
            let mut receipt = json!({"event":"error", "detail":value["detail"].as_str().filter(|s| s.len() <= 2048).unwrap_or("Sign-in stopped.")});
            if let Some(code @ ("signin_unreachable" | "signin_refused" | "signin_timeout"
                | "signin_cancelled" | "signin_not_stored" | "signin_unavailable" | "signin_failed"
                | "signin_code_rejected" | "signin_in_progress")) = value["code"].as_str() {
                receipt["code"] = json!(code);
            }
            Ok(receipt)
        },
        _ => Err("Invalid model sign-in event".into()),
    }
}
fn model_status_receipt(bytes: &[u8], lane: &str) -> Result<Value, String> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| "Invalid model status JSON")?;
    if value["lane"] != lane
        || !value["ready"].is_boolean()
        || !(value["default_backend"].is_string() || value["default_backend"].is_null())
    {
        return Err("Invalid model status receipt".into());
    }
    let mut providers = serde_json::Map::new();
    for id in [
        "openai_signin",
        "openai_key",
        "anthropic_key",
        "deepseek_key",
        "google_key",
    ] {
        let present = value["providers"][id]
            .as_bool()
            .ok_or("Invalid model provider status")?;
        providers.insert(id.into(), json!(present));
    }
    // Older runtimes omit these providers; an explicit malformed value is refused.
    for id in ["glm_key", "kimi_key", "qwen_key"] {
        let present = match value["providers"].get(id) {
            None => false,
            Some(value) => value.as_bool().ok_or("Invalid model provider status")?,
        };
        providers.insert(id.into(), json!(present));
    }
    let mut receipt = json!({"lane":lane, "ready":value["ready"], "providers":providers, "default_backend":value["default_backend"]});
    if let Some(state) = value.get("chatgpt") { receipt["chatgpt"] = chatgpt_state_receipt(state)?; }
    if let Some(state) = value.get("claude") { receipt["claude"] = claude_state_receipt(state)?; }
    if let Some(progress) = value.get("signin_in_progress") {
        receipt["signin_in_progress"] = json!(progress.as_bool().ok_or("Invalid sign-in status")?);
    }
    Ok(receipt)
}
// BU8: allow-list every field crossing the native boundary; never forward helper errors.
fn valid_model_slug(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128 && !value.starts_with('-')
        && value.bytes().all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}
fn chatgpt_state_receipt(value: &Value) -> Result<Value, String> {
    let signed_in = value["signed_in"].as_bool().ok_or("Invalid ChatGPT state")?;
    let required = value["model_selection_required"].as_bool().ok_or("Invalid ChatGPT state")?;
    let selected = match &value["selected_model"] {
        Value::Null => None,
        Value::String(s) if valid_model_slug(s) => Some(s.as_str()),
        _ => return Err("Invalid ChatGPT model".into()),
    };
    let refreshed = value["last_refresh"].as_str()
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|time| time.with_timezone(&chrono::Utc).to_rfc3339());
    Ok(json!({"signed_in":signed_in,"selected_model":selected,"last_refresh":refreshed,"model_selection_required":required}))
}
fn chatgpt_models_receipt(bytes: &[u8], lane: &str) -> Result<Value, String> {
    let mut value: Value = serde_json::from_slice(bytes).map_err(|_| "Invalid ChatGPT models JSON")?;
    if value["lane"] != lane || value["backend"] != "openai_codex" || !value["ok"].is_boolean() {
        return Err("Invalid ChatGPT models receipt".into());
    }
    // An unsupported catalog slug must not blank the usable native model list.
    if value["selected_model"].as_str().is_some_and(|s| !valid_model_slug(s)) {
        value["selected_model"] = Value::Null;
    }
    let mut receipt = chatgpt_state_receipt(&value)?;
    let default = match &value["account_default"] {
        Value::Null => None,
        Value::String(s) => valid_model_slug(s).then_some(s.as_str()),
        _ => return Err("Invalid ChatGPT account default".into()),
    };
    let rows = value["models"].as_array().ok_or("Invalid ChatGPT models")?;
    let mut models = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for row in rows {
        let slug = row["slug"].as_str().ok_or("Invalid ChatGPT model")?;
        if !seen.insert(slug) || !row["account_default"].is_boolean() || !row["selected"].is_boolean() {
            return Err("Invalid ChatGPT model flags".into());
        }
        if !valid_model_slug(slug) { continue; }
        models.push(json!({"slug":slug,"account_default":row["account_default"],"selected":row["selected"]}));
    }
    receipt["ok"] = value["ok"].clone();
    receipt["lane"] = json!(lane);
    receipt["backend"] = json!("openai_codex");
    receipt["account_default"] = json!(default);
    receipt["models"] = json!(models);
    if value["ok"] == false {
        let code = value["error"]["code"].as_str().ok_or("Invalid ChatGPT model error")?;
        let detail = match code {
            "chatgpt_signin_required" => "Sign in with ChatGPT to choose a model.",
            "chatgpt_models_unavailable" => "ChatGPT models could not be read. Try refreshing or sign in again.",
            "chatgpt_model_not_visible" => "That model is no longer available to this account. Choose a listed model.",
            _ => return Err("ChatGPT model request failed".into()),
        };
        receipt["error"] = json!({"code":code,"detail":detail});
    }
    Ok(receipt)
}
fn chatgpt_models_request(model: Option<&str>) -> Result<Value, String> {
    let lane = model_lane()?;
    let action = if model.is_some() { "openai-set-model" } else { "openai-models" };
    let (ok, bytes, _) = admin_run(&model_words(action, &lane, model)?, None, SHORT)?;
    let receipt = chatgpt_models_receipt(&bytes, &lane)?;
    if ok != (receipt["ok"] == true) { return Err("ChatGPT model request failed".into()); }
    Ok(receipt)
}
#[tauri::command]
pub async fn runtime_model_openai_models(window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    tauri::async_runtime::spawn_blocking(|| chatgpt_models_request(None))
        .await.map_err(|_| "ChatGPT model list task failed")?
}
#[tauri::command]
pub async fn runtime_model_openai_set_model(window: tauri::WebviewWindow, model: String) -> Result<Value, String> {
    main_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || chatgpt_models_request(Some(&model)))
        .await.map_err(|_| "ChatGPT model selection task failed")?
}

fn claude_state_receipt(value: &Value) -> Result<Value, String> {
    let signed_in = value["signed_in"].as_bool().ok_or("Invalid Claude state")?;
    let required = value["model_selection_required"].as_bool().ok_or("Invalid Claude state")?;
    let selected = match &value["selected_model"] {
        Value::Null => None,
        Value::String(s) if matches!(s.as_str(), "default" | "fable" | "opus" | "sonnet") => Some(s.as_str()),
        _ => return Err("Invalid Claude model".into()),
    };
    let checked = value["last_checked"].as_str()
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|t| t.with_timezone(&chrono::Utc).to_rfc3339());
    Ok(json!({"signed_in":signed_in, "selected_model":selected, "last_checked":checked, "model_selection_required":required}))
}
fn claude_request_receipt(bytes: &[u8], action: &str) -> Result<Value, String> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| "Invalid Claude receipt")?;
    if value["ok"] == false {
        let code = value["error"]["code"].as_str().or_else(|| value["code"].as_str()).unwrap_or("");
        return Err(match code {
            "claude_model_not_declared" => "Choose a model from the Claude program's list.",
            "claude_signin_required" => "Sign in with Claude to choose a model.",
            "signin_in_progress" => "A sign-in is in progress",
            _ => "The Claude request failed. Refresh status and try again.",
        }.into());
    }
    if value["ok"] != true { return Err("Invalid Claude receipt".into()); }
    if action != "claude-models" { return Ok(json!({"ok":true})); }
    if value["list_source"] != "declared_cli_options" || value["account_default"] != "default" {
        return Err("Invalid Claude model list".into());
    }
    let rows = value["models"].as_array().filter(|r| r.len() == 4).ok_or("Invalid Claude model list")?;
    let mut models = Vec::new();
    for slug in ["default", "fable", "opus", "sonnet"] {
        let row = rows.iter().find(|r| r["slug"] == slug).ok_or("Invalid Claude model choice")?;
        let label = row["label"].as_str().filter(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control)).ok_or("Invalid Claude model label")?;
        models.push(json!({"slug":slug, "label":label}));
    }
    Ok(json!({"ok":true, "models":models, "account_default":"default", "list_source":"declared_cli_options"}))
}
async fn claude_request(window: tauri::WebviewWindow, action: &'static str, model: Option<String>) -> Result<Value, String> {
    main_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let (ok, bytes, _) = admin_run(&model_words(action, &model_lane()?, model.as_deref())?, None, SHORT)?;
        let receipt = claude_request_receipt(&bytes, action)?;
        if !ok { return Err("The Claude request failed. Refresh status and try again.".into()); }
        Ok(receipt)
    }).await.map_err(|_| "Claude setup task failed")?
}
#[tauri::command]
pub async fn runtime_model_claude_models(window: tauri::WebviewWindow) -> Result<Value, String> {
    claude_request(window, "claude-models", None).await
}
#[tauri::command]
pub async fn runtime_model_claude_set_model(window: tauri::WebviewWindow, model: String) -> Result<Value, String> {
    claude_request(window, "claude-set-model", Some(model)).await
}
#[tauri::command]
pub async fn runtime_model_claude_logout(window: tauri::WebviewWindow) -> Result<Value, String> {
    claude_request(window, "claude-logout", None).await
}

fn model_saved_receipt(bytes: &[u8]) -> Result<Value, String> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| "Invalid model save receipt")?;
    if value["ok"] != true {
        return Err("Model setting was not saved".into());
    }
    // Write-only credentials: never return raw helper output, including its errors or extra fields.
    Ok(json!({"ok":true,"default_backend":value["default_backend"].as_str()}))
}

#[derive(Default)]
struct ModelLogin {
    child: Mutex<Option<std::process::Child>>,
    claude: bool,
    awaiting_code: std::sync::atomic::AtomicBool,
    code_submitted: std::sync::atomic::AtomicBool,
    #[cfg(windows)]
    wsl_started: Mutex<Option<Instant>>,
    cancelled: std::sync::atomic::AtomicBool,
}
impl ModelLogin {
    fn submit_code(&self, mut code: String) -> Result<(), String> {
        use std::{io::Write, sync::atomic::Ordering};
        if !valid_claude_code(&code) {
            code.clear();
            return Err("Use the code from Claude: at most 4096 ASCII letters, digits or . _ ~ + / # = % : - without spaces.".into());
        }
        let input = {
            let mut slot = self.child.lock().unwrap_or_else(|e| e.into_inner());
            if !self.claude || self.cancelled.load(Ordering::SeqCst)
                || !self.awaiting_code.load(Ordering::SeqCst)
                || self.code_submitted.swap(true, Ordering::SeqCst) {
                code.clear();
                return Err("This sign-in cannot accept another code. Start again.".into());
            }
            slot.as_mut().and_then(|c| c.stdin.take())
        };
        // Do not hold the child lock during pipe I/O: cancel/deadline must be able to end it.
        // Taking the writer closes stdin after this single line, including on failure.
        let result = (|| {
            let mut input = input.ok_or("The sign-in is no longer waiting for a code.")?;
            if self.cancelled.load(Ordering::SeqCst) { return Err("The sign-in was cancelled.".into()); }
            input.write_all(code.as_bytes()).and_then(|_| input.write_all(b"\n")).and_then(|_| input.flush())
                .map_err(|_| "The code could not be sent. Start sign-in again.".to_string())
        })();
        code.clear();
        result
    }
    fn cancel(&self) {
        use std::sync::atomic::Ordering;
        let mut child = self.child.lock().unwrap_or_else(|e| e.into_inner());
        self.cancelled.store(true, Ordering::SeqCst);
        if let Some(child) = child.as_mut() { drop(child.stdin.take()); }
        #[cfg(windows)]
        crate::windows::end_session(&mut child, *self.wsl_started.lock().unwrap_or_else(|e|e.into_inner()), Duration::ZERO);
        #[cfg(not(windows))]
        if let Some(child) = child.as_mut() {
            let _ = child.kill();
        }
    }
    fn stop(&self) {
        if let Some(child) = self.child.lock().unwrap_or_else(|e| e.into_inner()).as_mut() { drop(child.stdin.take()); }
        #[cfg(windows)]
        { crate::windows::end_session(&mut self.child.lock().unwrap_or_else(|e|e.into_inner()), *self.wsl_started.lock().unwrap_or_else(|e|e.into_inner()), Duration::ZERO); }
        #[cfg(not(windows))]
        if let Some(mut child) = self.child.lock().unwrap_or_else(|e| e.into_inner()).take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
    #[cfg(windows)]
    fn close(&self, grace:Duration) {
        let mut child=self.child.lock().unwrap_or_else(|e|e.into_inner());
        self.cancelled.store(true,std::sync::atomic::Ordering::SeqCst);
        crate::windows::end_session(&mut child,*self.wsl_started.lock().unwrap_or_else(|e|e.into_inner()),grace);
    }
}
static MODEL_LOGIN: Mutex<Option<Arc<ModelLogin>>> = Mutex::new(None);
// Use the same startup protection and EOF grace as the other WSL sessions.
#[cfg(windows)]
pub(crate) fn close_model_login_on_exit() {
    if let Some(login) = MODEL_LOGIN.lock().unwrap_or_else(|e|e.into_inner()).as_ref() {
        login.close(crate::windows::SESSION_CLOSE_GRACE);
    }
}
pub(crate) fn cancel_model_login() {
    if let Some(login) = MODEL_LOGIN
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .as_ref()
    {
        login.cancel();
    }
}
struct LoginGuard(Arc<ModelLogin>);
impl Drop for LoginGuard {
    fn drop(&mut self) {
        self.0.stop();
        let mut slot = MODEL_LOGIN.lock().unwrap_or_else(|e| e.into_inner());
        if slot
            .as_ref()
            .is_some_and(|login| Arc::ptr_eq(login, &self.0))
        {
            *slot = None;
        }
    }
}
fn run_model_login(
    login: &ModelLogin,
    mut command: std::process::Command,
    budget: Duration,
    mut emit: impl FnMut(Value),
) -> Result<Value, String> {
    use std::{
        io::{BufRead, BufReader, Read},
        process::Stdio,
        sync::{atomic::Ordering, mpsc},
    };
    let mut failure = None;
    let result = (|| {
        let stdout = {
            let mut slot = login.child.lock().unwrap_or_else(|e| e.into_inner());
            if login.cancelled.load(Ordering::SeqCst) {
                return Err("The sign-in was cancelled.".into());
            }
            #[cfg(windows)]
            let started = crate::windows::is_wsl_program(command.get_program()).then(Instant::now);
            // Only Claude waits for one sensitive line. ChatGPT retains EOF stdin.
            let mut child = command
                .stdin(if login.claude { Stdio::piped() } else { Stdio::null() })
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .map_err(|_| "Model sign-in could not start")?;
            let stdout = child.stdout.take().ok_or("Model stdout unavailable")?;
            #[cfg(windows)]
            { *login.wsl_started.lock().unwrap_or_else(|e|e.into_inner())=started; }
            *slot = Some(child);
            stdout
        };
        let (tx, rx) = mpsc::sync_channel(16);
        let claude = login.claude;
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut total = 0;
            loop {
                let mut line = Vec::new();
                let read = (&mut reader).take(16385).read_until(b'\n', &mut line);
                match read {
                    Ok(0) => break,
                    Ok(n) if n <= 16384 && total + n <= 1024 * 1024 => {
                        total += n;
                        if tx.send(if claude { claude_model_event(&line) } else { model_event(&line) }).is_err() {
                            break;
                        }
                    }
                    _ => {
                        let _ = tx.send(Err("Invalid or oversized model sign-in output".into()));
                        break;
                    }
                }
            }
        });
        let deadline = Instant::now() + budget;
        let mut done: Option<Value> = None;
        loop {
            if login.cancelled.load(Ordering::SeqCst) {
                return Err("The sign-in was cancelled.".into());
            }
            if Instant::now() >= deadline {
                failure = Some(json!({"event":"error", "code":"signin_timeout", "detail":"The sign-in timed out."}));
                return Err("The sign-in timed out.".into());
            }
            match rx.recv_timeout(Duration::from_millis(50)) {
                Ok(Ok(event)) => {
                    if done.is_some() {
                        return Err("Model sign-in sent events after completion".into());
                    }
                    match event["event"].as_str() {
                        Some("done") => {
                            done = Some(event);
                        }
                        Some("error") => {
                            let detail = event["detail"].as_str().unwrap_or("Sign-in stopped.").to_string();
                            failure = Some(event);
                            return Err(detail);
                        }
                        Some("signin_link") => {
                            // No second link (especially after input) may cross the boundary.
                            if login.awaiting_code.swap(true, Ordering::SeqCst) || login.code_submitted.load(Ordering::SeqCst) {
                                return Err("Invalid model sign-in event".into());
                            }
                            emit(event);
                        }
                        _ => emit(event),
                    }
                }
                Ok(Err(error)) => return Err(error),
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    let status = login
                        .child
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .as_mut()
                        .ok_or("Model sign-in child missing")?
                        .try_wait()
                        .map_err(|_| "Cannot inspect model sign-in child")?;
                    if let Some(status) = status {
                        if let Some(event) = done.take().filter(|_| status.success()) {
                            emit(event);
                            return Ok(json!({"ok":true}));
                        }
                        return Err("The sign-in was interrupted.".into());
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
        }
    })();
    let result = if login.cancelled.load(Ordering::SeqCst) {
        failure = Some(json!({"event":"error", "code":"signin_cancelled", "detail":"The sign-in was cancelled."}));
        Err("The sign-in was cancelled.".into())
    } else {
        result
    };
    login.stop();
    if let Err(error) = &result {
        emit(failure.unwrap_or_else(|| json!({"event":"error", "detail":error})));
    }
    result
}

fn model_command(words: &[String]) -> Result<std::process::Command, String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let program =
            std::path::PathBuf::from(std::env::var_os("SystemRoot").ok_or("SystemRoot unavailable")?)
                .join("System32/wsl.exe");
        let refs: Vec<&str> = words.iter().map(String::as_str).collect();
        let mut command = std::process::Command::new(program);
        command.args(exec_args(Some("root"), &refs)).creation_flags(0x0800_0000);
        Ok(command)
    }
    #[cfg(not(windows))]
    {
        let (engine, container) = portal::runtime_engine_container()?;
        let mut command = std::process::Command::new(engine);
        command.args(unix_admin_args(&container, words)?);
        Ok(command)
    }
}
#[tauri::command]
pub async fn runtime_model_status(window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    tauri::async_runtime::spawn_blocking(|| {
        let lane = model_lane()?;
        let (ok, bytes, _) = admin_run(&model_words("status", &lane, None)?, None, SHORT)?;
        if !ok {
            return Err("Model status failed".into());
        }
        model_status_receipt(&bytes, &lane)
    })
    .await
    .map_err(|_| "Model status task failed")?
}

fn embedder_args(operation: &str, model: Option<&str>) -> Result<Vec<String>, String> {
    embedder_words(operation, model).map(|words| exec_args(Some("root"), &words.iter().map(String::as_str).collect::<Vec<_>>()))
}
fn embedder_words(operation: &str, model: Option<&str>) -> Result<Vec<String>, String> {
    let mut command = vec!["bob-embedder", "settings", operation];
    match (operation, model) {
        ("embedder.status", None) => {},
        ("embedder.install", Some(model @ ("4b" | "0.6b"))) => command.extend(["--model", model]),
        _ => return Err("Invalid embedder operation or model".into()),
    }
    Ok(command.into_iter().map(String::from).collect())
}
/// Linux/macOS transport for the same fixed root operations the Windows build sends to WSL:
/// `<engine> exec -i -u root <container> <fixed words>`. Words come from the closed builders.
fn unix_admin_args(container: &str, words: &[String]) -> Result<Vec<String>, String> {
    let valid = !container.is_empty() && container.len() <= 64
        && container.bytes().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && container.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'));
    if !valid || words.is_empty() || !matches!(words[0].as_str(), "bob-embedder" | "bob-model") {
        return Err("Invalid runtime container or operation".into());
    }
    let mut args: Vec<String> = ["exec", "-i", "-u", "root", container].map(String::from).to_vec();
    args.extend(words.iter().cloned());
    Ok(args)
}

fn embedder_receipt(bytes: &[u8]) -> Result<Value, String> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| "Invalid embedder receipt")?;
    if let Some(error) = value["error"].as_str() {
        if error.len() <= 512 && !error.chars().any(char::is_control) {
            return Err(error.into());
        }
        return Err("Embedder setup failed".into());
    }
    let dimension = match value["selected_model"].as_str() {
        Some("4b") => 2560, Some("0.6b") => 1024,
        _ => return Err("Invalid embedder model receipt".into()),
    };
    if value["embedding_dimension"] != dimension || !value["ready"].is_boolean()
        || !value["file_present"].is_boolean() || !value["service_active"].is_boolean()
        || !value["sha_ok"].is_boolean()
        || !matches!(value["recommended_model"].as_str(), Some("4b" | "0.6b")) {
        return Err("Invalid embedder status receipt".into());
    }
    if !value["setup"].is_null() && (!matches!(value["setup"]["phase"].as_str(),
        Some("queued" | "downloading" | "verified" | "starting" | "ready" | "failed"))
        || value["setup"]["detail"].as_str().map_or(true, |s| s.len() > 1024)) {
        return Err("Invalid embedder progress receipt".into());
    }
    // Newer runtimes also say whether the weights are loaded and whether any model is installed.
    // Absent on older runtimes (null). A present alias must belong to the selected model.
    for key in ["model_loaded", "configured"] {
        if !value[key].is_null() && !value[key].is_boolean() { return Err("Invalid embedder status receipt".into()); }
    }
    if !value["alias"].is_null() && value["alias"].as_str() != Some(if dimension == 2560 { "qwen3-embedding-4b" } else { "qwen3-embedding-0.6b" }) {
        return Err("Invalid embedder model receipt".into());
    }
    if !value["installed_models"].is_null() && !value["installed_models"].as_array()
        .is_some_and(|rows| rows.len() <= 2 && rows.iter().all(|row| matches!(row.as_str(), Some("4b" | "0.6b")))) {
        return Err("Invalid embedder status receipt".into());
    }
    let mut result = serde_json::Map::new();
    for key in ["selected_model", "embedding_dimension", "ready", "file_present", "sha_ok",
                "service_active", "recommended_model", "ram_gb", "setup",
                "model_loaded", "alias", "configured", "installed_models"] {
        result.insert(key.into(), value[key].clone());
    }
    Ok(Value::Object(result))
}

fn embedder_request(operation: &str, model: Option<&str>) -> Result<Value, String> {
    // Same trusted local-lane guard as model enrollment, usable before sign-in. A remote
    // server connection is refused here: this build never runs admin operations against it.
    model_lane().map_err(|_| "Packaged embedder setup is available for Bob on this PC, not for a remote server connection")?;
    let (ok, bytes, _) = admin_run(&embedder_words(operation, model)?, None, SHORT)?;
    let receipt = embedder_receipt(&bytes)?;
    if !ok { return Err("Embedder helper failed".into()); }
    Ok(receipt)
}

#[tauri::command]
pub async fn runtime_embedder_status(window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    tauri::async_runtime::spawn_blocking(|| embedder_request("embedder.status", None))
        .await.map_err(|_| "Embedder status task failed")?
}

#[tauri::command]
pub async fn runtime_embedder_install(window: tauri::WebviewWindow, model: String) -> Result<Value, String> {
    main_window(&window)?;
    embedder_words("embedder.install", Some(&model))?;
    tauri::async_runtime::spawn_blocking(move || embedder_request("embedder.install", Some(&model)))
        .await.map_err(|_| "Embedder install task failed")?
}
#[tauri::command]
pub async fn runtime_model_openai_login(
    app: AppHandle,
    window: tauri::WebviewWindow,
) -> Result<Value, String> {
    start_model_login(app, window, false).await
}
#[tauri::command]
pub async fn runtime_model_claude_login(app: AppHandle, window: tauri::WebviewWindow) -> Result<Value, String> {
    start_model_login(app, window, true).await
}
async fn start_model_login(app: AppHandle, window: tauri::WebviewWindow, claude: bool) -> Result<Value, String> {
    main_window(&window)?;
    let lane = model_lane()?;
    let command = model_command(&model_words(if claude { "claude-login" } else { "openai-login" }, &lane, None)?)?;
    let login = Arc::new(ModelLogin { claude, ..Default::default() });
    {
        let mut slot = MODEL_LOGIN.lock().unwrap_or_else(|e| e.into_inner());
        if slot.is_some() {
            let event = if claude {
                claude_model_event(br#"{"event":"error","code":"signin_in_progress"}"#)?
            } else {
                model_event(br#"{"event":"error","code":"signin_in_progress","detail":"A ChatGPT sign-in is already in progress for this lane."}"#)?
            };
            // Refuse only this invocation; do not inject an error into the active sign-in stream.
            return Err(json!({"code":"signin_in_progress", "detail":event["detail"]}).to_string());
        }
        *slot = Some(login.clone());
    }
    let guard = LoginGuard(login.clone());
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = guard;
        run_model_login(&login, command, Duration::from_secs(15 * 60), |event| {
            let _ = app.emit("runtime://model", event);
        })
    })
    .await
    .map_err(|_| "Model sign-in task failed")?
}
#[tauri::command]
pub async fn runtime_model_claude_submit_code(window: tauri::WebviewWindow, code: String) -> Result<Value, String> {
    main_window(&window)?;
    let login = MODEL_LOGIN.lock().unwrap_or_else(|e| e.into_inner()).clone().ok_or("No Claude sign-in is waiting for a code.")?;
    tauri::async_runtime::spawn_blocking(move || {
        login.submit_code(code)?;
        Ok(json!({"ok":true}))
    }).await.map_err(|_| "Claude code submission task failed")?
}
#[tauri::command]
pub async fn runtime_model_cancel(window: tauri::WebviewWindow) -> Result<Value, String> {
    main_window(&window)?;
    cancel_model_login();
    Ok(json!({"ok":true}))
}
#[tauri::command]
pub async fn runtime_model_set_key(
    window: tauri::WebviewWindow,
    provider: String,
    mut key: String,
) -> Result<Value, String> {
    main_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let lane = model_lane()?;
        let words = model_words("set-key", &lane, Some(&provider))?;
        if key.trim().is_empty() || key.len() > 16384 || key.contains(['\r', '\n', '\0']) {
            key.clear();
            return Err("Invalid API key".into());
        }
        let (ok, bytes, _) = admin_run(&words, Some(std::mem::take(&mut key)), SHORT)?;
        if !ok {
            return Err("API key was not saved".into());
        }
        model_saved_receipt(&bytes)
    })
    .await
    .map_err(|_| "Model key task failed")?
}
#[tauri::command]
pub async fn runtime_model_set_default(
    window: tauri::WebviewWindow,
    backend: String,
) -> Result<Value, String> {
    main_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let words = model_words("set-default", &model_lane()?, Some(&backend))?;
        let (ok, _, _) = admin_run(&words, None, SHORT)?;
        if !ok {
            return Err("Default model was not saved".into());
        }
        Ok(json!({"ok":true, "default_backend":backend}))
    })
    .await
    .map_err(|_| "Default model task failed")?
}
#[tauri::command]
pub async fn runtime_model_open_url(
    window: tauri::WebviewWindow,
    url: String,
) -> Result<(), String> {
    main_window(&window)?;
    open_signin_url_with(&url, open_signin_page)
}

// Keep validation ahead of OS dispatch, including in the headless native tests.
fn open_signin_url_with(url: &str, open: impl FnOnce(&str) -> Result<(), String>) -> Result<(), String> {
    if !valid_signin_url(&url) {
        return Err("Invalid HTTPS sign-in URL".into());
    }
    open(url)
}

#[cfg(any(windows, test))]
fn signin_open_result(result: isize) -> Result<(), String> {
    if result <= 32 { return Err("Cannot open sign-in page".into()); }
    Ok(())
}

fn open_signin_page(url: &str) -> Result<(), String> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::Shell::ShellExecuteW;
        let wide: Vec<u16> = url.encode_utf16().chain(Some(0)).collect();
        let result = unsafe {
            ShellExecuteW(std::ptr::null_mut(), windows_sys::core::w!("open"), wide.as_ptr(),
                std::ptr::null(), std::ptr::null(), 1)
        };
        signin_open_result(result as isize)
    }
    #[cfg(not(windows))]
    {
        // valid_signin_url admits only a bare https URL, so it cannot be parsed as an option.
        std::process::Command::new("xdg-open")
            .arg(&url)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .map_err(|_| "Cannot open sign-in page; open the URL shown on screen in a browser")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn au7_open_refuses_invalid_urls_before_os_dispatch_and_checks_shell_result() {
        for url in ["http://example.com", "https://example.com/a&calc", "file:///test", "https://user@example.com", ""] {
            assert_eq!(open_signin_url_with(url, |_| panic!("invalid URL reached OS dispatch")).unwrap_err(), "Invalid HTTPS sign-in URL");
        }
        let url = "https://auth.openai.com/codex/device";
        assert!(open_signin_url_with(url, |received| { assert_eq!(received, url); Ok(()) }).is_ok());
        assert_eq!(open_signin_url_with(url, |_| Err("OS refused".into())).unwrap_err(), "OS refused");
        for result in [-1, 0, 2, 5, 8, 26, 27, 28, 29, 30, 31, 32] { assert!(signin_open_result(result).is_err()); }
        for result in [33, 42, isize::MAX] { assert!(signin_open_result(result).is_ok()); }
    }
    #[test]
    fn au7_error_filter_keeps_only_fixed_reason_codes() {
        for code in ["signin_unreachable", "signin_refused", "signin_timeout", "signin_cancelled", "signin_not_stored", "signin_unavailable", "signin_failed"] {
            let event = model_event(&serde_json::to_vec(&json!({"event":"error", "code":code, "detail":"A fixed reason.", "output":"fake-secret"})).unwrap()).unwrap();
            assert_eq!(event, json!({"event":"error", "code":code, "detail":"A fixed reason."}));
        }
        for code in [json!("unknown"), json!("signin_failed\n"), json!(7), Value::Null] {
            assert_eq!(model_event(&serde_json::to_vec(&json!({"event":"error", "code":code, "detail":"A fixed reason."})).unwrap()).unwrap(), json!({"event":"error", "detail":"A fixed reason."}));
        }
        assert_eq!(model_event(br#"{"event":"error"}"#).unwrap()["detail"], "Sign-in stopped.");
    }
    // Synthetic children only: no WSL, provider or browser is contacted.
    fn au7_model_child(line: Option<&Value>, exit: i32, wait: bool) -> std::process::Command {
        #[cfg(windows)] {
            let mut command = std::process::Command::new(std::path::PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join("System32/WindowsPowerShell/v1.0/powershell.exe"));
            let output = line.map(|value| format!("Write-Output '{}'; ", value)).unwrap_or_default();
            command.args(["-NoProfile", "-NonInteractive", "-Command", &format!("{}{}exit {}", output, if wait { "Start-Sleep -Seconds 30; " } else { "" }, exit)]);
            command
        }
        #[cfg(unix)] {
            let output = line.map(|value| format!("print({:?}, flush=True); ", value.to_string())).unwrap_or_default();
            fake_model(&format!("import time; {}{}raise SystemExit({})", output, if wait { "time.sleep(30); " } else { "" }, exit))
        }
    }
    #[test]
    fn au7_login_preserves_filtered_failure_through_the_real_reader() {
        for code in ["signin_refused", "unknown"] {
            let event = json!({"event":"error", "code":code, "detail":"The sign-in service refused the request.", "output":"fake-secret"});
            let mut events = Vec::new();
            let result = run_model_login(&ModelLogin::default(), au7_model_child(Some(&event), 1, false), Duration::from_secs(5), |event| events.push(event));
            assert_eq!(result.unwrap_err(), event["detail"]);
            assert_eq!(events, vec![model_event(&serde_json::to_vec(&event).unwrap()).unwrap()]);
        }
    }
    #[test]
    fn au7_login_reports_interrupted_eof_and_cancel_without_a_helper_final_event() {
        for exit in [0, 1] {
            let mut events = Vec::new();
            let result = run_model_login(&ModelLogin::default(), au7_model_child(None, exit, false), Duration::from_secs(5), |event| events.push(event));
            assert_eq!(result.unwrap_err(), "The sign-in was interrupted.");
            assert_eq!(events, vec![json!({"event":"error", "detail":"The sign-in was interrupted."})]);
        }
        for before_spawn in [true, false] {
            let login = ModelLogin::default();
            if before_spawn { login.cancel(); }
            let device = json!({"event":"device_code", "url":"https://auth.openai.com/codex/device", "code":"TEST-CODE"});
            let mut events = Vec::new();
            let result = run_model_login(&login, au7_model_child(Some(&device), 0, true), Duration::from_secs(5), |event| {
                if event["event"] == "device_code" { login.cancel(); }
                events.push(event);
            });
            assert_eq!(result.unwrap_err(), "The sign-in was cancelled.");
            assert_eq!(events.last().unwrap(), &json!({"event":"error", "code":"signin_cancelled", "detail":"The sign-in was cancelled."}));
            assert!(login.child.lock().unwrap().is_none());
        }
    }
    #[test]
    fn session_commands_are_never_killed_inside_the_wsl_session_wait() {
        let words = |items: &[&str]| items.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        // The 8 s watchdog probe and the 20 s status probes both open a session.
        for budget in [8, 20] {
            assert_eq!(
                session_budget(&words(&["-d", "BoB", "-u", "root", "--exec", "/bin/true"]), Duration::from_secs(budget)),
                SESSION_FLOOR
            );
        }
        assert!(SESSION_FLOOR >= Duration::from_secs(40), "boot plus three 10 s uid waits");
        // Longer budgets and service queries that open no session keep their own budget.
        assert_eq!(
            session_budget(&words(&["-d", "BoB", "--exec", "bob-lane"]), Duration::from_secs(120)),
            Duration::from_secs(120)
        );
        assert_eq!(session_budget(&words(&["--list", "--running", "--quiet"]), Duration::from_secs(8)), Duration::from_secs(8));
        assert_eq!(session_budget(&words(&["--terminate", "BoB"]), Duration::from_secs(8)), Duration::from_secs(8));
    }
    #[test]
    fn embedder_boundary_is_closed_and_checks_model_dimensions() {
        assert_eq!(embedder_args("embedder.install", Some("0.6b")).unwrap(),
            ["-d", "BoB", "-u", "root", "--exec", "bob-embedder", "settings", "embedder.install", "--model", "0.6b"]);
        for (operation, model) in [("fetch", Some("4b")), ("embedder.install", Some("4b;id")),
            ("embedder.install", None), ("embedder.status", Some("4b"))] {
            assert!(embedder_args(operation, model).is_err());
        }
        let value = json!({"selected_model":"0.6b","embedding_dimension":1024,"ready":false,
            "file_present":false,"sha_ok":false,"service_active":false,"recommended_model":"0.6b",
            "ram_gb":8,"setup":null,"extra":"ignored"});
        let receipt = embedder_receipt(&serde_json::to_vec(&value).unwrap()).unwrap();
        assert!(receipt.get("extra").is_none());
        let mut wrong = value.clone(); wrong["embedding_dimension"] = json!(2560);
        assert!(embedder_receipt(&serde_json::to_vec(&wrong).unwrap()).is_err());
        assert!(embedder_receipt(br#"{"error":"Existing vectors require re-embedding"}"#).unwrap_err().contains("re-embedding"));
    }
    #[test]
    fn unix_transport_is_a_fixed_root_exec_into_the_validated_container_only() {
        let words = embedder_words("embedder.install", Some("0.6b")).unwrap();
        assert_eq!(unix_admin_args("bob-runtime", &words).unwrap(),
            ["exec", "-i", "-u", "root", "bob-runtime", "bob-embedder", "settings", "embedder.install", "--model", "0.6b"]);
        let model = model_words("set-default", "guest", Some("claude_api")).unwrap();
        assert_eq!(unix_admin_args("bob-runtime-setup-repair-20261113", &model).unwrap(),
            ["exec", "-i", "-u", "root", "bob-runtime-setup-repair-20261113", "bob-model", "set-default", "guest", "claude_api"]);
        for bad_container in ["", "-d", "a b", "a;id", "../x", &"a".repeat(65)] {
            assert!(unix_admin_args(bad_container, &words).is_err(), "{bad_container}");
        }
        assert!(embedder_words("fetch", Some("4b")).is_err() && embedder_words("embedder.install", Some("4b;id")).is_err());
        assert!(model_words("set-default", "guest", Some("--privileged")).is_err());
        assert!(model_words("set-key", "Bad Lane", Some("openai")).is_err());
    }
    #[test]
    fn embedder_receipt_carries_loaded_alias_and_rejects_a_mismatched_model() {
        let value = json!({"selected_model":"0.6b","embedding_dimension":1024,"ready":true,
            "file_present":true,"sha_ok":true,"service_active":true,"recommended_model":"0.6b",
            "ram_gb":8,"setup":null,"model_loaded":true,"alias":"qwen3-embedding-0.6b",
            "configured":true,"installed_models":["0.6b"]});
        let receipt = embedder_receipt(&serde_json::to_vec(&value).unwrap()).unwrap();
        assert_eq!(receipt["model_loaded"], true);
        assert_eq!(receipt["alias"], "qwen3-embedding-0.6b");
        assert_eq!(receipt["installed_models"], json!(["0.6b"]));
        for (key, bad) in [("alias", json!("qwen3-embedding-4b")), ("model_loaded", json!("yes")),
                           ("installed_models", json!(["../etc"]))] {
            let mut wrong = value.clone(); wrong[key] = bad;
            assert!(embedder_receipt(&serde_json::to_vec(&wrong).unwrap()).is_err(), "{key}");
        }
    }
    #[test]
    fn model_providers_are_bounded_validated_and_match_key_admission() {
        let rows = model_providers_receipt(MODEL_PROVIDERS).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 9);
        assert_eq!(
            rows[0],
            json!({"id":"openai","label":"OpenAI (ChatGPT)","method":"signin"})
        );
        for row in rows
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["method"] == "key")
        {
            assert!(model_args("set-key", "guest", row["id"].as_str()).is_ok());
        }
        for provider in [
            "moonshot",
            "zai",
            "minimax",
            "openrouter",
            "openai_signin",
            "unknown",
            "openai;echo",
        ] {
            assert!(model_args("set-key", "guest", Some(provider)).is_err());
        }
        assert!(model_providers_receipt(&[]).is_err());
        assert!(model_providers_receipt(&[("openai", "OpenAI", "key"); 17]).is_err());
        assert!(model_providers_receipt(&[("openai", "OpenAI", "key"); 2]).is_err());
        for row in [
            ("", "Name", "key"),
            ("bad;id", "Name", "key"),
            ("openai", "", "key"),
            ("openai", "bad\nlabel", "key"),
            ("openai", "OpenAI", "shell"),
            ("other", "Other", "signin"),
        ] {
            assert!(model_providers_receipt(&[row]).is_err());
        }
        assert!(model_providers_receipt(&[(&"a".repeat(41), "Name", "key")]).is_err());
        assert!(model_providers_receipt(&[("openai", &"a".repeat(129), "key")]).is_err());
    }
    #[test]
    fn au5_login_emits_filtered_completion_only_after_successful_exit() {
        let completion = json!({"event":"done","ok":true,"chatgpt":{"signed_in":true,"selected_model":"visible-gpt","last_refresh":null,"model_selection_required":false,"token":"fake-secret"},"model_notice":"fake-secret","credentials":"fake-secret"});
        for exit in [0, 1] {
            #[cfg(windows)]
            let command = {
                let mut command = std::process::Command::new(std::path::PathBuf::from(std::env::var_os("SystemRoot").unwrap()).join("System32/WindowsPowerShell/v1.0/powershell.exe"));
                command.args(["-NoProfile", "-NonInteractive", "-Command", &format!("Write-Output '{}'; exit {}", completion, exit)]);
                command
            };
            #[cfg(unix)]
            let command = fake_model(&format!("print({:?}); raise SystemExit({})", completion.to_string(), exit));
            let mut events = Vec::new();
            let result = run_model_login(&ModelLogin::default(), command, Duration::from_secs(5), |event| events.push(event));
            assert_eq!(result.is_ok(), exit == 0);
            assert!(!serde_json::to_string(&events).unwrap().contains("fake-secret"));
            if exit == 0 {
                assert_eq!(events.len(), 1);
                assert_eq!(events[0]["event"], "done");
                assert_eq!(events[0]["chatgpt"]["selected_model"], "visible-gpt");
                assert!(events[0]["model_notice"].as_str().unwrap().contains("Choose"));
            } else { assert!(events.iter().all(|event| event["event"] != "done")); }
        }
    }
    #[test]
    fn au5_chatgpt_model_commands_validate_fixed_argv() {
        assert_eq!(model_words("openai-models", "owner", None).unwrap(), ["bob-model", "openai-models", "owner"]);
        assert_eq!(model_words("openai-set-model", "owner", Some("gpt-6.1-sol")).unwrap(), ["bob-model", "openai-set-model", "owner", "gpt-6.1-sol"]);
        assert_eq!(model_args("openai-set-model", "owner", Some("gpt-6.1-sol")).unwrap(), ["-d","BoB","-u","root","--exec","bob-model","openai-set-model","owner","gpt-6.1-sol"]);
        for bad in ["", "-x", "a b", "a;b", "a&b", "a/b", "a\nb", "a\rb", "a\0b", "模型"] {
            assert!(model_words("openai-set-model", "owner", Some(bad)).is_err(), "{bad:?}");
        }
        assert!(model_words("openai-set-model", "owner", Some(&"a".repeat(129))).is_err());
        assert!(model_words("openai-set-model", "../other", Some("gpt")).is_err());
        assert!(model_words("openai-set-model", "owner", None).is_err());
        assert!(model_words("openai-models", "owner", Some("gpt")).is_err());
        assert!(model_words("openai-logout", "owner", None).is_err());
    }
    #[test]
    fn au5_chatgpt_receipts_allow_only_public_model_metadata() {
        let state = json!({"signed_in":true,"selected_model":"visible-gpt","last_refresh":"2026-11-17T12:34:56Z","model_selection_required":false,"access_token":"fake-secret","tokens":{"refresh_token":"fake-secret"}});
        let mut value = state.clone();
        value["ok"] = json!(true); value["lane"] = json!("owner"); value["backend"] = json!("openai_codex"); value["account_default"] = json!("visible-gpt");
        value["models"] = json!([{"slug":"visible-gpt","account_default":true,"selected":true,"api_key":"fake-secret"}]);
        value["account"] = json!("fake-secret");
        let read = |v: &Value| chatgpt_models_receipt(&serde_json::to_vec(v).unwrap(), "owner");
        let clean = read(&value).unwrap();
        assert!(!clean.to_string().contains("fake-secret"));
        assert_eq!(clean["models"][0], json!({"slug":"visible-gpt","account_default":true,"selected":true}));
        assert_eq!(clean["selected_model"], "visible-gpt");
        let mut status = json!({"lane":"owner","ready":true,"default_backend":"deepseek_api","providers":{"openai_signin":true,"openai_key":false,"anthropic_key":false,"deepseek_key":true,"google_key":false},"chatgpt":state});
        let filtered = model_status_receipt(&serde_json::to_vec(&status).unwrap(), "owner").unwrap();
        assert_eq!(filtered["chatgpt"]["selected_model"], "visible-gpt");
        assert!(!filtered.to_string().contains("fake-secret"));
        let done = json!({"event":"done","ok":true,"chatgpt":status["chatgpt"],"model_notice":"fake-secret","token":"fake-secret"});
        let event = model_event(&serde_json::to_vec(&done).unwrap()).unwrap();
        assert_eq!(event["chatgpt"], filtered["chatgpt"]);
        assert!(!event.to_string().contains("fake-secret"));
        for code in ["chatgpt_signin_required","chatgpt_models_unavailable","chatgpt_model_not_visible"] {
            let mut error = value.clone(); error["ok"]=json!(false); error["error"]=json!({"code":code,"detail":"fake-secret","token":"fake-secret"});
            let clean = read(&error).unwrap(); assert_eq!(clean["error"]["code"],code); assert!(!clean.to_string().contains("fake-secret"));
        }
        let mut bad = value.clone(); bad["error"]=json!({"code":"fake-secret"}); bad["ok"]=json!(false); assert!(read(&bad).is_err());
        for (field, bad_value) in [("lane",json!("other")),("backend",json!("other")),("ok",json!(null)),("selected_model",json!(7)),("signed_in",json!("true")),("model_selection_required",json!(null)),("models",json!({})),("account_default",json!(false))] {
            let mut bad=value.clone(); bad[field]=bad_value; assert!(read(&bad).is_err(),"{field}");
        }
        let mut bad=value.clone(); bad["models"][0]["slug"]=json!(null); assert!(read(&bad).is_err());
        let mut bad=value.clone(); bad["models"][0]["selected"]=json!("true"); assert!(read(&bad).is_err());
        let mut duplicate=value.clone(); duplicate["models"].as_array_mut().unwrap().push(value["models"][0].clone()); assert!(read(&duplicate).is_err());
        status["chatgpt"]["last_refresh"]=json!("fake-secret");
        assert!(model_status_receipt(&serde_json::to_vec(&status).unwrap(), "owner").unwrap()["chatgpt"]["last_refresh"].is_null());
        let mut missing=value.clone(); missing["selected_model"]=Value::Null; missing["account_default"]=Value::Null; missing["models"]=json!([]); missing["model_selection_required"]=json!(true);
        assert!(read(&missing).unwrap()["model_selection_required"] == true);
    }
    #[test]
    fn au6_chatgpt_models_skip_unsupported_slugs_without_weakening_validation() {
        let value = json!({"ok":true,"lane":"owner","backend":"openai_codex",
            "signed_in":true,"selected_model":"vendor/model","account_default":"vendor:model",
            "last_refresh":null,"model_selection_required":false,"token":"fake-secret",
            "models":[
                {"slug":"visible-gpt","account_default":false,"selected":false,"token":"fake-secret"},
                {"slug":"vendor/model","account_default":false,"selected":true},
                {"slug":"vendor:model","account_default":true,"selected":false},
                {"slug":"second-gpt","account_default":false,"selected":false}
            ]});
        let read = |v: &Value| chatgpt_models_receipt(&serde_json::to_vec(v).unwrap(), "owner");
        let clean = read(&value).unwrap();
        assert_eq!(clean["models"], json!([
            {"slug":"visible-gpt","account_default":false,"selected":false},
            {"slug":"second-gpt","account_default":false,"selected":false}
        ]));
        assert!(clean["selected_model"].is_null());
        assert!(clean["account_default"].is_null());
        assert!(!clean.to_string().contains("fake-secret"));
        assert_eq!(clean["model_selection_required"], false);
        for slug in ["", "-bad", "bad model", "模型", &"a".repeat(129)] {
            let mut odd = value.clone();
            odd["models"][1]["slug"] = json!(slug);
            odd["selected_model"] = json!(slug); odd["account_default"] = json!(slug);
            let clean = read(&odd).unwrap();
            assert_eq!(clean["models"].as_array().unwrap().len(), 2);
            assert!(clean["selected_model"].is_null() && clean["account_default"].is_null());
        }
        for field in ["selected", "account_default"] {
            for bad_flag in [Value::Null, json!("true"), json!(1)] {
                let mut bad = value.clone(); bad["models"][1][field] = bad_flag;
                assert_eq!(read(&bad).unwrap_err(), "Invalid ChatGPT model flags");
            }
        }
        for index in [0, 1] {
            let mut duplicate = value.clone();
            duplicate["models"].as_array_mut().unwrap().push(value["models"][index].clone());
            assert_eq!(read(&duplicate).unwrap_err(), "Invalid ChatGPT model flags");
        }
        // Tolerance is confined to the list receipt, never set-model or other state receipts.
        assert!(chatgpt_state_receipt(&value).is_err());
        for slug in ["vendor/model", "vendor:model"] {
            assert!(model_words("openai-set-model", "owner", Some(slug)).is_err());
        }
    }
    #[test]
    fn model_argv_and_provider_validation_keep_keys_off_the_command_line() {
        for action in ["status", "openai-login"] {
            assert_eq!(
                model_args(action, "guest", None).unwrap(),
                [
                    "-d",
                    "BoB",
                    "-u",
                    "root",
                    "--exec",
                    "bob-model",
                    action,
                    "guest"
                ]
            );
        }
        for &(provider, _, method) in MODEL_PROVIDERS {
            if method != "key" {
                continue;
            }
            assert_eq!(
                model_args("set-key", "guest", Some(provider)).unwrap(),
                [
                    "-d",
                    "BoB",
                    "-u",
                    "root",
                    "--exec",
                    "bob-model",
                    "set-key",
                    "guest",
                    provider
                ]
            );
            assert!(!model_args("set-key", "guest", Some(provider))
                .unwrap()
                .iter()
                .any(|arg| arg.contains("fake-secret-key")));
        }
        assert_eq!(
            model_args("set-default", "guest", Some("openai_signin"))
                .unwrap()
                .last()
                .unwrap(),
            "openai_signin"
        );
        for (action, lane, value) in [
            ("set-key", "guest", Some("x;echo")),
            ("set-key", "guest", Some("moonshot")),
            ("status", "-bad", None),
            ("status", "guest", Some("extra")),
            ("unknown", "guest", None),
            ("set-default", "guest", Some("a b")),
        ] {
            assert!(model_args(action, lane, value).is_err());
        }
    }
    #[test]
    fn model_json_lines_validate_urls_codes_and_terminal_events() {
        assert_eq!(model_event(br#"{"event":"device_code","url":"https://auth.openai.com/device","code":"ABCD-1234","extra":"ignored"}"#).unwrap(), json!({"event":"device_code","url":"https://auth.openai.com/device","code":"ABCD-1234"}));
        assert_eq!(
            model_event(b"{\"event\":\"done\",\"ok\":true}\r\n").unwrap(),
            json!({"event":"done","ok":true})
        );
        assert_eq!(
            model_event(br#"{"event":"error","detail":"denied"}"#).unwrap()["detail"],
            "denied"
        );
        for line in [
            b"not-json".as_slice(),
            br#"{"event":"done","ok":false}"#,
            br#"{"event":"unexpected"}"#,
            br#"{"event":"device_code","url":"http://example.com","code":"AB"}"#,
            br#"{"event":"device_code","url":"https://example.com","code":""}"#,
        ] {
            assert!(model_event(line).is_err());
        }
        for url in [
            "http://example.com",
            "https://example.com/a&calc",
            "https://example.com/%TEMP%",
            "https://example.com/\"",
            "https://example.com/\n",
            "https://example.com/$(id)",
            "https://user@example.com/",
            "https://",
        ] {
            assert!(!valid_signin_url(url), "{url}");
        }
    }
    #[test]
    fn model_receipts_strip_extra_fields_and_refuse_malformed_status() {
        let value = json!({"lane":"guest","providers":{"openai_signin":false,"openai_key":true,"anthropic_key":false,"deepseek_key":false,"google_key":false,"key":"fake-secret"},"default_backend":"openai_key","ready":true,"key":"fake-secret"});
        let receipt = model_status_receipt(&serde_json::to_vec(&value).unwrap(), "guest").unwrap();
        assert!(!receipt.to_string().contains("fake-secret"));
        for provider in ["glm", "kimi", "qwen"] {
            let field = format!("{provider}_key");
            assert_eq!(receipt["providers"][&field], false); // old runtime
            let mut next = value.clone(); next["providers"][&field] = json!(true);
            let saved = model_status_receipt(&serde_json::to_vec(&next).unwrap(), "guest").unwrap();
            assert_eq!(saved["providers"][&field], true);
            next["providers"][&field] = json!("not a boolean");
            assert!(model_status_receipt(&serde_json::to_vec(&next).unwrap(), "guest").is_err());
        }
        assert!(model_status_receipt(&serde_json::to_vec(&value).unwrap(), "dad").is_err());
        assert!(model_status_receipt(br#"{"lane":"guest","ready":true}"#, "guest").is_err());
        assert_eq!(
            model_saved_receipt(
                br#"{"ok":true,"default_backend":"openai_key","key":"fake-secret"}"#
            )
            .unwrap(),
            json!({"ok":true,"default_backend":"openai_key"})
        );
        assert!(
            !model_saved_receipt(br#"{"ok":false,"detail":"fake-secret"}"#)
                .unwrap_err()
                .contains("fake-secret")
        );
    }
    #[cfg(unix)]
    fn fake_model(script: &str) -> std::process::Command {
        let mut command = std::process::Command::new("python3");
        command.args(["-u", "-c", script]);
        command
    }
    #[cfg(unix)]
    #[test]
    fn fake_model_key_receives_secret_only_on_stdin_and_returns_safe_receipt() {
        let args = model_args("set-key", "guest", Some("anthropic")).unwrap();
        let mut command = fake_model("import sys,json\nassert sys.stdin.read() == 'fake-secret-key\\n'\nassert 'fake-secret-key' not in str(sys.argv)\nprint(json.dumps({'ok':True,'default_backend':'anthropic_key','argv':sys.argv[1:]}))");
        command.args(&args);
        let (ok, bytes, code) = bounded_output(
            command,
            Some("fake-secret-key".into()),
            Duration::from_secs(3),
        )
        .unwrap();
        assert!(ok);
        assert_eq!(code, Some(0));
        let value: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(value["argv"], json!(args));
        assert!(!String::from_utf8_lossy(&bytes).contains("fake-secret-key"));
        assert_eq!(
            model_saved_receipt(&bytes).unwrap(),
            json!({"ok":true,"default_backend":"anthropic_key"})
        );
    }
    #[cfg(unix)]
    #[test]
    fn fake_model_streams_device_code_then_verifies_completion_and_exit() {
        let mut events = Vec::new();
        let login = ModelLogin::default();
        let command = fake_model("import time\nprint('{\"event\":\"device_code\",\"url\":\"https://auth.openai.com/device\",\"code\":\"ABCD-1234\"}')\ntime.sleep(.05)\nprint('{\"event\":\"done\",\"ok\":true}')");
        assert_eq!(
            run_model_login(&login, command, Duration::from_secs(3), |v| events.push(v)).unwrap(),
            json!({"ok":true})
        );
        assert_eq!(
            events
                .iter()
                .map(|v| v["event"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["device_code", "done"]
        );
        assert!(login.child.lock().unwrap().is_none());
        for script in [
            "print('bad JSON')",
            "print('{\"event\":\"done\",\"ok\":true}');raise SystemExit(1)",
            "print('{\"event\":\"error\",\"detail\":\"denied\"}')",
            "print('x'*17000)",
            "pass",
        ] {
            let login = ModelLogin::default();
            let mut events = Vec::new();
            assert!(
                run_model_login(&login, fake_model(script), Duration::from_secs(3), |v| {
                    events.push(v)
                })
                .is_err()
            );
            assert_eq!(events.last().unwrap()["event"], "error");
            assert!(login.child.lock().unwrap().is_none());
        }
        assert!(run_model_login(
            &ModelLogin::default(),
            fake_model("import time;time.sleep(2)"),
            Duration::from_millis(50),
            |_| {}
        )
        .unwrap_err()
        .contains("timed out"));
    }
    #[cfg(unix)]
    #[test]
    fn fake_model_cancel_kills_only_the_owned_child_including_before_spawn() {
        let login = Arc::new(ModelLogin::default());
        let mut bystander = fake_model("import time;time.sleep(20)").spawn().unwrap();
        let worker = login.clone();
        let handle = std::thread::spawn(move || {
            run_model_login(
                &worker,
                fake_model("import time;time.sleep(20)"),
                Duration::from_secs(3),
                |_| {},
            )
        });
        let deadline = Instant::now() + Duration::from_secs(2);
        while login.child.lock().unwrap().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(login.child.lock().unwrap().is_some());
        login.cancel();
        let result = handle.join().unwrap();
        assert!(result.unwrap_err().contains("cancelled"));
        assert!(login.child.lock().unwrap().is_none());
        let untouched = bystander.try_wait().unwrap().is_none();
        let _ = bystander.kill();
        let _ = bystander.wait();
        assert!(untouched, "cancellation must not kill an unrelated process");
        let cancelled = ModelLogin::default();
        cancelled.cancel();
        assert!(run_model_login(
            &cancelled,
            fake_model("raise Exception('must not spawn')"),
            Duration::from_secs(1),
            |_| {}
        )
        .unwrap_err()
        .contains("cancelled"));
    }
    #[cfg(windows)]
    #[test]
    fn au4_model_sign_in_obeys_age_on_exit_and_cancel() {
        use std::os::windows::io::{AsRawHandle,FromRawHandle,OwnedHandle};
        use std::process::{Command,Stdio};
        use windows_sys::Win32::System::Threading::*;
        for old in [false,true] { for close in [false,true] {
            let child=Command::new("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
                .args(["-NoProfile","-Command","Start-Sleep -Seconds 30"])
                .stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap();
            let observer=unsafe{OpenProcess(PROCESS_TERMINATE|PROCESS_SYNCHRONIZE,0,child.id())};
            assert!(!observer.is_null());let observer=unsafe{OwnedHandle::from_raw_handle(observer)};
            let login=ModelLogin::default();
            *login.child.lock().unwrap()=Some(child);
            *login.wsl_started.lock().unwrap()=Some(Instant::now()-if old{SESSION_FLOOR}else{Duration::ZERO});
            if close { login.close(crate::windows::SESSION_CLOSE_GRACE); } else { login.cancel(); }
            let alive=unsafe{WaitForSingleObject(observer.as_raw_handle(),0)}==windows_sys::Win32::Foundation::WAIT_TIMEOUT;
            unsafe{TerminateProcess(observer.as_raw_handle(),0);WaitForSingleObject(observer.as_raw_handle(),2000);}
            assert_eq!(alive,!old,"old={old}, close={close}");
            assert!(login.child.lock().unwrap().is_none());
        }}
    }
    #[test]
    fn au8_claude_url_code_and_reason_boundaries() {
        let url = "https://claude.com/cai/oauth/authorize?client_id=x&redirect_uri=https%3A%2F%2Fclaude.ai&state=A_B-1";
        assert!(valid_claude_signin_url(url));
        assert!(valid_signin_url(url));
        open_signin_url_with(url, |received| { assert_eq!(received, url); Ok(()) }).unwrap();
        assert!(valid_claude_signin_url("https://claude.ai/oauth?x=%23&y=+/.~"));
        for bad in ["http://claude.com/a", "https://claude.com.evil/a?x=1&y=2", "https://platform.claude.com/a", "https://claude.com:443/a", "https://u@claude.ai/a", "https://claude.ai/a#secret", "https://claude.ai/a\n", "https://claude.ai/\\evil"] {
            assert!(!valid_claude_signin_url(bad), "{bad}");
            assert!(claude_model_event(&serde_json::to_vec(&json!({"event":"signin_link", "url":bad})).unwrap()).is_err());
        }
        assert!(!valid_signin_url("https://claude.ai:443/a"));
        assert!(!valid_signin_url("https://not-listed.example/signin"));
        assert!(!valid_signin_url("https://claude.com.evil/a?x=1&y=2"));
        assert!(!valid_chatgpt_signin_url("https://auth.openai.com/device?x=1&y=%23"));
        assert!(valid_chatgpt_signin_url("https://auth.openai.com/device"));
        for bad in [String::new(), "a b".into(), "a\n".into(), "a\r".into(), "é".into(), "a".repeat(4097), "a&b".into()] { assert!(!valid_claude_code(&bad)); }
        assert!(valid_claude_code(&"a".repeat(4096)));
        assert!(valid_claude_code("Ab9._~+/#=%:-"));
        for (code, sentence) in [
            ("signin_code_rejected", "The sign-in code was wrong or expired. Start sign-in again."),
            ("signin_in_progress", "A sign-in is already in progress for this lane."),
        ] {
            let line = serde_json::to_vec(&json!({"event":"error", "code":code, "detail":"OBVIOUS_FAKE_CODE", "extra":"OBVIOUS_FAKE_CODE"})).unwrap();
            let receipt = claude_model_event(&line).unwrap();
            assert_eq!(receipt, json!({"event":"error", "code":code, "detail":sentence}));
            assert_eq!(model_event(&line).unwrap()["code"], code);
        }
        assert_eq!(model_event(br#"{"event":"error","code":"signin_in_progress","detail":"A ChatGPT sign-in is already in progress for this lane."}"#).unwrap()["detail"], "A ChatGPT sign-in is already in progress for this lane.");
        for event in [json!({"event":"done","ok":true,"code":"OBVIOUS_FAKE_CODE"}), json!({"event":"error","code":"unknown","detail":"OBVIOUS_FAKE_CODE"})] {
            assert!(!claude_model_event(&serde_json::to_vec(&event).unwrap()).unwrap().to_string().contains("OBVIOUS_FAKE_CODE"));
        }
        for action in ["claude-login", "claude-models", "claude-logout"] {
            assert_eq!(model_words(action, "owner", None).unwrap(), ["bob-model", action, "owner"]);
            assert!(model_words(action, "../owner", None).is_err());
        }
        assert!(model_words("claude-set-model", "owner", Some("opus")).is_ok());
        assert!(model_words("claude-set-model", "owner", Some("invented")).is_err());
    }
    fn au8_claude_child(after_submit: bool) -> std::process::Command {
        #[cfg(windows)]
        {
            let script = if after_submit {
                r#"[Console]::Out.WriteLine('{"event":"signin_link","url":"https://claude.com/cai/oauth/authorize?x=1&y=%23"}'); $line=[Console]::In.ReadLine(); if ($line.Length -ne 18 -or [Console]::In.Read() -ne -1) {exit 9}; [Console]::Error.WriteLine($line); [Console]::Out.WriteLine(('{"event":"error","code":"signin_code_rejected","detail":"'+$line+'","extra":"'+$line+'"}')); exit 1"#
            } else { "Start-Sleep -Seconds 10" };
            let mut command = std::process::Command::new("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe");
            command.args(["-NoProfile", "-Command", script]); command
        }
        #[cfg(not(windows))]
        {
            let script = if after_submit {
                r#"import sys,json
print(json.dumps({'event':'signin_link','url':'https://claude.com/cai/oauth/authorize?x=1&y=%23'}),flush=True)
line=sys.stdin.readline()
assert len(line)==19 and sys.stdin.read()==''
print(line,file=sys.stderr)
print(json.dumps({'event':'error','code':'signin_code_rejected','detail':line,'extra':line}),flush=True)
sys.exit(1)"#
            } else { "import time;time.sleep(10)" };
            let mut command = std::process::Command::new("python3");
            command.args(["-u", "-c", script]); command
        }
    }
    #[test]
    fn au8_sensitive_stdin_once_and_no_echo_in_captured_receipts() {
        let login = ModelLogin { claude:true, ..Default::default() };
        assert!(login.submit_code("OBVIOUS_FAKE_CODE1".into()).is_err());
        let mut events = Vec::new(); let mut submit_errors = Vec::new();
        let command = au8_claude_child(true);
        assert!(!format!("{:?}", command).contains("OBVIOUS_FAKE_CODE1"));
        let result = run_model_login(&login, command, Duration::from_secs(5), |event| {
            if event["event"] == "signin_link" {
                assert!(login.submit_code("OBVIOUS_FAKE_CODE1".into()).is_ok());
                submit_errors.push(login.submit_code("OBVIOUS_FAKE_CODE1".into()).unwrap_err());
            }
            events.push(event);
        });
        assert!(result.is_err());
        assert_eq!(events.last().unwrap()["code"], "signin_code_rejected");
        for text in [serde_json::to_string(&events).unwrap(), format!("{result:?}"), format!("{submit_errors:?}")] { assert!(!text.contains("OBVIOUS_FAKE_CODE1")); }
        assert!(login.child.lock().unwrap().is_none());
    }
    #[test]
    fn au8_cancel_before_link_after_link_and_after_submit() {
        let before = ModelLogin { claude:true, ..Default::default() };
        before.cancel();
        assert!(run_model_login(&before, au8_claude_child(false), Duration::from_secs(2), |_| {}).unwrap_err().contains("cancelled"));
        // Cancel an actually running child before a link arrives.
        let login = Arc::new(ModelLogin { claude:true, ..Default::default() });
        let worker = login.clone();
        let handle = std::thread::spawn(move || run_model_login(&worker, au8_claude_child(false), Duration::from_secs(5), |_| {}));
        let deadline = Instant::now() + Duration::from_secs(2);
        while login.child.lock().unwrap().is_none() && Instant::now() < deadline { std::thread::sleep(Duration::from_millis(10)); }
        login.cancel(); assert!(handle.join().unwrap().unwrap_err().contains("cancelled"));
        for submit in [false, true] {
            let login = ModelLogin { claude:true, ..Default::default() }; let mut events = Vec::new();
            let result = run_model_login(&login, au8_claude_child(true), Duration::from_secs(5), |event| {
                if event["event"] == "signin_link" {
                    if submit { login.submit_code("OBVIOUS_FAKE_CODE1".into()).unwrap(); }
                    login.cancel();
                }
                events.push(event);
            });
            assert!(result.unwrap_err().contains("cancelled"));
            assert_eq!(events.last().unwrap()["code"], "signin_cancelled");
            assert!(login.child.lock().unwrap().is_none());
        }
    }
    #[test]
    fn au8_claude_status_and_declared_models_are_filtered() {
        let state = json!({"signed_in":true,"selected_model":"default","last_checked":"2026-11-18T10:00:00Z","model_selection_required":true,"token":"FAKE_SECRET"});
        assert!(!claude_state_receipt(&state).unwrap().to_string().contains("FAKE_SECRET"));
        let status = json!({"lane":"owner","ready":false,"default_backend":null,"providers":{"openai_signin":false,"openai_key":false,"anthropic_key":false,"deepseek_key":false,"google_key":false},"claude":state,"signin_in_progress":true});
        let receipt = model_status_receipt(&serde_json::to_vec(&status).unwrap(), "owner").unwrap();
        assert_eq!(receipt["signin_in_progress"], true); assert_eq!(receipt["claude"]["selected_model"], "default");
        let models = json!({"ok":true,"list_source":"declared_cli_options","account_default":"default","models":[{"slug":"sonnet","label":"sonnet"},{"slug":"default","label":"Account default"},{"slug":"opus","label":"opus"},{"slug":"fable","label":"fable"}],"token":"FAKE_SECRET"});
        let receipt = claude_request_receipt(&serde_json::to_vec(&models).unwrap(), "claude-models").unwrap();
        assert_eq!(receipt["models"][0]["label"], "Account default"); assert!(!receipt.to_string().contains("FAKE_SECRET"));
    }
    #[test]
    fn wsl_status_uses_hresult_and_handles_utf16_errors() {
        assert_eq!(wsl_state(true, Some(0), &[]), "ok");
        assert_eq!(wsl_state(false, Some(0x8007019eu32 as i32), &[]), "missing");
        for code in [0x80370102u32, 0x800701bc, 0x8007013a] {
            assert_eq!(wsl_state(false, Some(code as i32), &[]), "needs_wsl2");
        }
        let bytes: Vec<u8> = "Error: 0x800701bc"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        assert_eq!(wsl_state(false, Some(1), &bytes), "needs_wsl2");
        assert_eq!(wsl_state(false, Some(1), b"unknown error"), "error");
    }
    #[test]
    fn parses_utf16_wsl_list_bom_crlf_and_empty_lines() {
        let bytes: Vec<u8> = "\u{feff}Ubuntu\r\nBoB\r\n\r\n日本語\r\n\0"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        assert_eq!(
            distro_names(&bytes).unwrap(),
            vec!["Ubuntu", "BoB", "日本語"]
        );
        assert!(distro_names(&[0]).is_err());
        assert!(distro_names(&[0, 0xd8]).is_err());
        assert!(distro_names(&[]).unwrap().is_empty());
    }
    #[test]
    fn fixed_argv_builders_never_include_password() {
        let password = "Never-argv-secret!";
        let args = lane_args("dad").unwrap();
        assert_eq!(
            args,
            [
                "-d",
                "BoB",
                "-u",
                "root",
                "--exec",
                "bob-lane",
                "add",
                "dad",
                "--password-stdin"
            ]
        );
        assert!(!args.iter().any(|s| s.contains(password)));
        assert_eq!(
            import_args(r"C:\User data\BoB", r"C:\Bob App\runtime.tar.gz"),
            [
                "--import",
                "BoB",
                r"C:\User data\BoB",
                r"C:\Bob App\runtime.tar.gz",
                "--version",
                "2"
            ]
        );
        assert_eq!(exec_args(None, &["true"]), ["-d", "BoB", "--exec", "true"]);
        for name in [
            "",
            "Dad",
            "a b",
            "-x",
            "dad;echo",
            "a\n",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ] {
            assert!(lane_args(name).is_err());
        }
    }
    #[test]
    fn lane_receipt_requires_one_uri_and_matching_valid_ports() {
        let text = "otpauth://totp/Bob:dad?secret=ABC&issuer=Bob\nAdded dad: core=7845 gateway=7846 bridge=7847\n";
        let result = parse_lane(text, "dad").unwrap();
        assert_eq!(result["core"], 7845);
        assert_eq!(result["gateway"], 7846);
        assert!(parse_lane(text, "other").is_err());
        assert!(parse_lane(&format!("{text}{text}"), "dad").is_err());
        assert!(parse_lane(&text.replace("core=7845", "core=0"), "dad").is_err());
        // Current runtimes say "Configured"; both words parse, but only one summary line is allowed.
        let current = text.replace("Added dad:", "Configured dad:");
        assert_eq!(parse_lane(&current, "dad").unwrap()["gateway"], 7846);
        assert!(parse_lane(&current, "other").is_err());
        let both = format!("{current}Added dad: core=7845 gateway=7846 bridge=7847
");
        assert!(parse_lane(&both, "dad").is_err());
    }
}
