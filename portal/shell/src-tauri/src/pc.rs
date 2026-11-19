//! PC authority belongs to this native process. WSL has a request queue only.
use crate::{
    pc_policy,
    pc_sign::{digest, Signer},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, OnceLock,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::State;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub id: String,
    pub task_id: String,
    pub operation: String,
    pub args: Value,
    pub args_sha256: String,
    pub expires_at: f64,
    pub lane: String,
    pub owner: String,
    pub agent: String,
    pub principal: String,
    pub conversation_id: String,
    pub credential_epoch: i64,
}
impl Request {
    fn key(&self) -> String {
        json!([
            self.agent,
            self.principal,
            self.conversation_id,
            self.task_id,
            self.lane,
            self.owner,
            self.credential_epoch
        ])
        .to_string()
    }
    fn validate(&self) -> bool {
        let id = |s: &str| s.len() == 32 && s.bytes().all(|c| c.is_ascii_hexdigit());
        let label = |s: &str| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control);
        id(&self.id)
            && id(&self.task_id)
            && label(&self.lane)
            && label(&self.owner)
            && label(&self.agent)
            && label(&self.principal)
            && uuid::Uuid::parse_str(&self.conversation_id).is_ok()
            && self.credential_epoch >= 0
            && self.expires_at.is_finite()
            && self.args_sha256
                == digest(&serde_json::to_vec(&json!([self.operation, self.args])).unwrap())
            && matches!(
                self.operation.as_str(),
                "pc.run" | "pc.read" | "pc.write" | "pc.list" | "pc.open" | "pc.info"
            )
            && self.args.as_object().is_some_and(|o| o.len() <= 5)
            && (self.operation != "pc.info" || self.args.as_object().is_some_and(|o| o.is_empty()))
            && self.args.to_string().len() < 192 * 1024
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Suggest,
    Approve,
    Auto,
    Yolo,
}
#[derive(Clone, Serialize)]
struct Grant {
    mode: Mode,
    root: PathBuf,
    until: u64,
    #[serde(skip)]
    deadline: Instant,
    actions: HashSet<String>,
}
impl Grant {
    fn active(&self, tick: u64) -> bool {
        tick < self.until && Instant::now() < self.deadline
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
#[derive(Default)]
struct Host {
    pending: HashMap<String, Request>,
    grants: HashMap<String, Grant>,
    ended: HashSet<String>,
    tainted: HashSet<String>,
    running: HashMap<String, (String, Arc<AtomicBool>)>,
    outcomes: HashMap<String, Value>,
    signer: Option<Signer>,
    recovered_scopes: HashSet<String>,
}
static HOST: OnceLock<Mutex<Host>> = OnceLock::new();
static SIGNER_INIT: Mutex<()> = Mutex::new(());
fn host() -> &'static Mutex<Host> {
    HOST.get_or_init(|| Mutex::new(Host::default()))
}
fn directory() -> PathBuf {
    crate::portal::data_root().join("pc-receipts")
}
fn save(path: &Path, value: &Value) -> Result<(), String> {
    fs::create_dir_all(path.parent().ok_or("Missing receipt directory")?)
        .map_err(|_| "Cannot create native receipt directory")?;
    let temp = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temp)
        .map_err(|_| "Cannot create native receipt")?;
    file.write_all(&serde_json::to_vec_pretty(value).map_err(|_| "Invalid native receipt")?)
        .and_then(|_| file.sync_all())
        .map_err(|_| "Cannot persist native receipt")?;
    drop(file);
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::*;
        let wide = |p: &Path| {
            p.as_os_str()
                .encode_wide()
                .chain(Some(0))
                .collect::<Vec<_>>()
        };
        if unsafe {
            MoveFileExW(
                wide(&temp).as_ptr(),
                wide(path).as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            )
        } == 0
        {
            return Err("Cannot commit native receipt".into());
        }
    }
    #[cfg(not(windows))]
    fs::rename(temp, path).map_err(|_| "Cannot commit native receipt")?;
    Ok(())
}
fn signed(h: &Host, kind: &str, r: &Request, result: Option<Value>) -> Result<Value, String> {
    let signer = h
        .signer
        .as_ref()
        .ok_or("Native signer has not been initialized")?;
    let mut payload = json!({"kind":kind,"request_id":r.id,"task_id":r.task_id,"args_sha256":r.args_sha256,
        "host_id":digest(&signer.public_blob),"iat":now(),"exp":now()+60,"nonce":uuid::Uuid::new_v4().simple().to_string()});
    if let Some(result) = result {
        payload["result"] = result;
    }
    Ok(json!({"contract_version":2,"signature":signer.sign(&payload)?,"payload":payload}))
}
fn action_key(r: &Request) -> String {
    r.operation.clone()
}
impl Host {
    fn revoke_task(&mut self, task: &str) {
        self.ended.insert(task.into());
        self.grants.retain(|key, _| {
            self.pending
                .values()
                .find(|r| r.key() == *key)
                .is_some_and(|r| r.task_id != task)
        });
        for (id, cancel) in self.running.values() {
            if id == task {
                cancel.store(true, Ordering::SeqCst);
            }
        }
    }
    fn automatic(&self, r: &Request, tick: u64) -> bool {
        if self.ended.contains(&r.task_id) || self.tainted.contains(&r.task_id) {
            return false;
        }
        let Some(g) = self.grants.get(&r.key()).filter(|g| g.active(tick)) else {
            return false;
        };
        if r.operation == "pc.info" {
            return r.args.as_object().is_some_and(|o| o.is_empty())
                && (matches!(g.mode, Mode::Auto | Mode::Yolo)
                    || g.mode == Mode::Approve && g.actions.contains("pc.info"));
        }
        // A hardware-only grant never supplies filesystem authority.
        if g.root.as_os_str().is_empty() { return false; }
        if pc_policy::protected(&r.operation, &r.args, Some(&g.root)).is_some() {
            return false;
        }
        let raw = r.args[if r.operation == "pc.run" {
            "cwd"
        } else {
            "path"
        }]
        .as_str()
        .unwrap_or("");
        if !raw.starts_with("https://") && !raw.starts_with("http://") {
            let Ok(path) = pc_policy::checked_path(raw) else {
                return false;
            };
            if !pc_policy::in_root(&path, &g.root) {
                return false;
            }
            let mut resolved = r.args.clone();
            resolved["path"] = json!(path);
            if pc_policy::protected(&r.operation, &resolved, Some(&g.root)).is_some() {
                return false;
            }
        }
        g.mode == Mode::Yolo
            || g.mode == Mode::Auto
                && (matches!(r.operation.as_str(), "pc.read" | "pc.list")
                    || g.actions.contains(&action_key(r)))
    }
    fn mode(&mut self, r: &Request, mode: Mode, root: &str, ack: bool) -> Result<(), String> {
        if self.ended.contains(&r.task_id) {
            return Err("This task ended. Request a new task.".into());
        }
        if mode == Mode::Yolo && !ack {
            return Err("PC yolo needs its own native risk acknowledgement".into());
        }
        let root = if r.operation == "pc.info" { PathBuf::new() } else { pc_policy::checked_path(root)? };
        if r.operation != "pc.info" && !root.is_dir() {
            return Err("Choose an existing task folder".into());
        }
        self.grants.insert(
            r.key(),
            Grant {
                mode,
                root,
                until: now() + if mode == Mode::Yolo { 1800 } else { 7200 },
                deadline: Instant::now()
                    + Duration::from_secs(if mode == Mode::Yolo { 1800 } else { 7200 }),
                actions: HashSet::new(),
            },
        );
        self.tainted.remove(&r.task_id);
        Ok(())
    }
}
pub(crate) fn revoke_all() {
    if let Ok(mut h) = host().lock() {
        for (_, cancel) in h.running.values() {
            cancel.store(true, Ordering::SeqCst);
        }
        h.grants.clear();
        let tasks: Vec<_> = h.pending.values().map(|r| r.task_id.clone()).collect();
        h.ended.extend(tasks);
    }
}
pub(crate) fn revoke_conversation(conversation: &str) {
    if let Ok(mut h) = host().lock() {
        let tasks: Vec<_> = h
            .pending
            .values()
            .filter(|r| r.conversation_id == conversation)
            .map(|r| r.task_id.clone())
            .collect();
        for task in tasks {
            h.revoke_task(&task);
        }
    }
}
fn native_action(
    state: &crate::portal::PortalState,
    id: &str,
    decision: &str,
    root_override: Option<&str>,
) -> Result<Value, String> {
    crate::portal::local_model_lane()?;
    {
        let h = host().lock().map_err(|_| "Native PC state is busy")?;
        let r = h.pending.get(id).ok_or("PC request is no longer pending")?;
        if !r.validate()
            || h.ended.contains(&r.task_id)
            || !matches!(decision, "allow_once" | "allow_task" | "deny" | "automatic")
        {
            return Err("A valid native PC decision on an active task is required".into());
        }
    }
    // Enrollment may wait for WSL; never hold the grant/cancellation mutex then.
    {
        let _initializing = SIGNER_INIT
            .lock()
            .map_err(|_| "Native signer initialization failed")?;
        let needs = host()
            .lock()
            .map_err(|_| "Native PC state is busy")?
            .signer
            .is_none();
        if needs {
            let signer = Signer::new()?;
            crate::pc_sign::enroll(&signer)?;
            host().lock().map_err(|_| "Native PC state is busy")?.signer = Some(signer);
        }
    }
    let (r, root, cancel, claim, authority) = {
        let mut h = host().lock().map_err(|_| "Native PC state is busy")?;
        let r = h
            .pending
            .get(id)
            .ok_or("PC request is no longer pending")?
            .clone();
        if !r.validate() || h.ended.contains(&r.task_id) {
            return Err("PC task ended or request is invalid".into());
        }
        if h.outcomes.contains_key(id) || h.running.contains_key(id) {
            return Err("This request already has a native receipt. It cannot run again.".into());
        }
        let automatic = decision == "automatic";
        if automatic && !h.automatic(&r, now()) {
            return Err("A native approval is required".into());
        }
        if !matches!(decision, "allow_once" | "allow_task" | "deny" | "automatic") {
            return Err("Invalid native PC decision".into());
        }
        let root = if r.operation == "pc.info" {
            h.grants.get(&r.key()).map(|g| g.root.clone()).unwrap_or_default()
        } else if decision == "deny" {
            fs::canonicalize(std::env::temp_dir())
                .map_err(|_| "Native temp directory unavailable")?
        } else if let Some(raw) = root_override {
            pc_policy::checked_path(raw)?
        } else {
            h.grants
                .get(&r.key())
                .map(|g| g.root.clone())
                .ok_or("Choose the task folder in the native card")?
        };
        if r.operation != "pc.info" && !root.is_dir() {
            return Err("Choose an existing task folder".into());
        }
        let path = directory().join(format!("{}.json", r.id));
        if path.exists() {
            h.tainted.insert(r.task_id.clone());
            return Err(
                "A native receipt already exists. Inspect it; this mutation is never replayed."
                    .into(),
            );
        }
        let claim = signed(&h, "pc.claim", &r, None)?;
        let authority = json!({"decision":decision,"mode":h.grants.get(&r.key()).map(|g|g.mode).unwrap_or(Mode::Approve),
            "grant_until":h.grants.get(&r.key()).map(|g|g.until),"agent":r.agent,"task_id":r.task_id,"task_folder":root});
        let cancel = Arc::new(AtomicBool::new(false));
        save(
            &path,
            &json!({"request":r,"status":"unknown","stage":"claiming","time":now(),"note":"Crash or lost reply never authorizes replay"}),
        )?;
        h.running
            .insert(id.into(), (r.task_id.clone(), cancel.clone()));
        if decision == "allow_task"
            && pc_policy::protected(&r.operation, &r.args, Some(&root)).is_none()
        {
            if r.operation == "pc.info" && !h.grants.contains_key(&r.key()) {
                h.mode(&r, Mode::Approve, "", false)?;
            }
            if let Some(grant) = h.grants.get_mut(&r.key()).filter(|g| g.mode == Mode::Auto || r.operation == "pc.info") {
                if grant.root != root {
                    grant.root = root.clone();
                    grant.actions.clear();
                }
                grant.actions.insert(action_key(&r));
            }
        }
        (r, root, cancel, claim, authority)
    };
    let claim_result = state.pc_call("pc.claim", claim.clone(), true);
    // WSL reply latency cannot extend a host grant. Recheck after the signed
    // claim round trip, immediately before any Windows effect.
    let automatic_current = decision != "automatic"
        || host()
            .lock()
            .map(|h| h.automatic(&r, now()))
            .unwrap_or(false);
    let mut result = if !matches!(claim_result,Ok(ref v) if v["claimed"]==true&&v["request_id"]==r.id)
    {
        json!({"status":"unknown","reason":"Native claim was not established. No PC action was dispatched; inspect the queue before proposing another action."})
    } else if r.expires_at <= now() as f64 {
        json!({"status":"expired","reason":"The request expired before execution"})
    } else if cancel.load(Ordering::SeqCst) {
        json!({"status":"stopped","reason":"The native task was stopped before dispatch"})
    } else if !automatic_current {
        json!({"status":"expired","reason":"The native PC grant ended before dispatch. Request a fresh native grant."})
    } else if decision == "deny" {
        json!({"status":"denied","reason":"Denied in the native Windows app"})
    } else {
        crate::pc_execute::execute(&r.id, &r.operation, &r.args, &root, &directory(), cancel)
    };
    result["agent"] = json!(r.agent);
    result["operation"] = json!(r.operation);
    result["request_id"] = json!(r.id);
    result["task_id"] = json!(r.task_id);
    result["conversation_id"] = json!(r.conversation_id);
    result["time"] = json!(now());
    let path = directory().join(format!("{}.json", r.id));
    let frame = {
        let mut h = host().lock().map_err(|_| "Native PC state unavailable")?;
        h.running.remove(&r.id);
        if result["status"] == "unknown"
            || result["reason"]
                .as_str()
                .is_some_and(|s| s.starts_with("preimage_conflict"))
        {
            h.tainted.insert(r.task_id.clone());
            h.grants.remove(&r.key());
        }
        let mut wire = result.clone();
        if serde_json::to_vec(&wire)
            .map_err(|_| "Invalid native output")?
            .len()
            > 128 * 1024
        {
            if let Some(content) = wire["content"].as_str() {
                wire["content"] = json!(content.chars().take(8192).collect::<String>());
            }
            if let Some(entries) = wire["entries"].as_array() {
                wire["entries"] = json!(entries.iter().take(100).cloned().collect::<Vec<_>>());
            }
            wire["truncated_for_broker"] = json!(true);
            wire["full_native_receipt"] = json!(path);
        }
        let frame = signed(&h, "pc.receipt", &r, Some(wire))?;
        save(
            &path,
            &json!({"request":r,"authorization":authority,"decision":decision,"task_folder":root,"claim":claim,"signed_receipt":frame,
            "public_blob":crate::pc_sign::hex(&h.signer.as_ref().unwrap().public_blob),"result":result,"time":now()}),
        )?;
        result["receipt_path"] = json!(path);
        h.outcomes.insert(r.id.clone(), result.clone());
        frame
    };
    if claim_result.is_ok() {
        let recorded = matches!(state.pc_call("pc.receipt", frame, true),Ok(ref value) if value["recorded"]==true && value["request_id"]==r.id);
        result["server_receipt_recorded"] = json!(recorded);
        if let Ok(mut h) = host().lock() {
            h.outcomes.insert(r.id.clone(), result.clone());
        }
    }
    Ok(result)
}
fn poll(state: &crate::portal::PortalState) -> Result<Value, String> {
    crate::portal::local_model_lane()?;
    let value = state.pc_call("pc.pending", json!({"contract_version":2}), false)?;
    let rows = value["requests"]
        .as_array()
        .filter(|v| v.len() <= 50)
        .ok_or("Invalid PC queue")?;
    let mut automatic = vec![];
    {
        let mut h = host().lock().map_err(|_| "Native PC state unavailable")?;
        let scope = json!([value["lane"], value["owner"]]).to_string();
        if h.recovered_scopes.insert(scope) {
            if let Ok(files) = fs::read_dir(directory()) {
                for file in files.flatten().take(1000) {
                    let path = file.path();
                    if path.extension().and_then(|s| s.to_str()) != Some("json")
                        || file
                            .metadata()
                            .map(|m| m.len() > 2 * 1024 * 1024)
                            .unwrap_or(true)
                    {
                        continue;
                    }
                    let Some(saved) = fs::read(&path)
                        .ok()
                        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                    else {
                        continue;
                    };
                    let Ok(r) = serde_json::from_value::<Request>(saved["request"].clone()) else {
                        continue;
                    };
                    if !r.validate()
                        || saved["request"]["lane"] != value["lane"]
                        || saved["request"]["owner"] != value["owner"]
                    {
                        continue;
                    }
                    let mut result = if saved["result"].is_object() {
                        saved["result"].clone()
                    } else {
                        json!({"status":"unknown","reason":"Interrupted native request. Inspect this receipt; nothing will be replayed."})
                    };
                    result["receipt_path"] = json!(path);
                    result["agent"] = json!(r.agent);
                    result["operation"] = json!(r.operation);
                    result["conversation_id"] = json!(r.conversation_id);
                    result["task_id"] = json!(r.task_id);
                    result["time"] = saved["time"].clone();
                    if result["status"] == "unknown" {
                        h.tainted.insert(r.task_id);
                    }
                    h.outcomes.entry(r.id).or_insert(result);
                }
            }
        }
        let expired: Vec<_> = h
            .pending
            .values()
            .filter(|r| {
                r.expires_at <= now() as f64
                    && !h.running.contains_key(&r.id)
                    && !h.outcomes.contains_key(&r.id)
            })
            .cloned()
            .collect();
        for r in expired {
            h.outcomes.insert(r.id.clone(),json!({"status":"expired","agent":r.agent,"conversation_id":r.conversation_id,"task_id":r.task_id,"time":now(),"operation":r.operation,"reason":"Request expired; it was not dispatched."}));
        }
        if let Some(ended) = value["ended_tasks"].as_array() {
            for task in ended.iter().filter_map(Value::as_str) {
                h.revoke_task(task);
                let stopped: Vec<_> = h
                    .pending
                    .values()
                    .filter(|r| {
                        r.task_id == task
                            && !h.outcomes.contains_key(&r.id)
                            && !h.running.contains_key(&r.id)
                    })
                    .cloned()
                    .collect();
                for r in stopped {
                    h.outcomes.insert(r.id.clone(),json!({"status":"stopped","agent":r.agent,"operation":r.operation,"conversation_id":r.conversation_id,"task_id":r.task_id,"time":now(),"reason":"Task ended before dispatch."}));
                }
            }
        }
        for raw in rows {
            let r: Request =
                serde_json::from_value(raw.clone()).map_err(|_| "Invalid native PC request")?;
            if !r.validate()
                || raw["lane"] != value["lane"]
                || raw["owner"] != value["owner"]
                || raw["credential_epoch"] != value["credential_epoch"]
            {
                return Err("PC identity binding is invalid".into());
            }
            if r.expires_at <= now() as f64
                || h.ended.contains(&r.task_id)
                || h.outcomes.contains_key(&r.id)
            {
                continue;
            }
            if h.running.contains_key(&r.id) {
                continue;
            }
            let receipt = directory().join(format!("{}.json", r.id));
            if receipt.exists() {
                let saved = fs::read(&receipt)
                    .ok()
                    .and_then(|v| serde_json::from_slice::<Value>(&v).ok());
                let outcome=saved.and_then(|v|v.get("result").cloned()).unwrap_or(json!({"status":"unknown","reason":"Interrupted native request; inspect its receipt. Nothing will be replayed."}));
                if outcome["status"] == "unknown" {
                    h.tainted.insert(r.task_id.clone());
                    h.grants.remove(&r.key());
                }
                h.outcomes.insert(r.id.clone(), outcome);
                continue;
            }
            if h.automatic(&r, now()) {
                automatic.push(r.id.clone());
            }
            h.pending.insert(r.id.clone(), r);
        }
    }
    for id in automatic {
        let _ = native_action(state, &id, "automatic", None);
    }
    let h = host().lock().map_err(|_| "Native PC state unavailable")?;
    let rows:Vec<_>=h.pending.values().filter(|r|!h.outcomes.contains_key(&r.id)&&!h.ended.contains(&r.task_id)).map(|r|{
        let grant=h.grants.get(&r.key()).filter(|g|g.active(now()));
        json!({"request":r,"mode":grant.map(|g|g.mode).unwrap_or(Mode::Approve),"root":grant.map(|g|g.root.clone()),
            "until":grant.map(|g|g.until),"protected_reason":pc_policy::protected(&r.operation,&r.args,grant.map(|g|g.root.as_path())),"running":h.running.contains_key(&r.id)})
    }).collect();
    let mut seen = HashSet::new();
    let tasks: Vec<_> = h.pending.values().filter(|r| !h.ended.contains(&r.task_id) && seen.insert(r.key())).map(|r| {
        let g=h.grants.get(&r.key()).filter(|g|g.active(now()));
        json!({"request":r,"mode":g.map(|g|g.mode).unwrap_or(Mode::Approve),"root":g.map(|g|g.root.clone()),"until":g.map(|g|g.until),"protected_reason":null,"running":false})
    }).collect();
    // UI-only association data: signed broker receipts remain unchanged.
    let outcomes: HashMap<_, _> = h.outcomes.iter().map(|(id, result)| {
        let mut row = result.clone();
        if let Some(request) = h.pending.get(id) { row["request_args"] = request.args.clone(); }
        (id.clone(), row)
    }).collect();
    Ok(json!({"requests":rows,"outcomes":outcomes,"tasks":tasks}))
}
#[tauri::command]
pub async fn pc_poll(
    window: tauri::WebviewWindow,
    state: State<'_, Arc<crate::portal::PortalState>>,
) -> Result<Value, String> {
    crate::runtime::main_window(&window)?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || poll(&state))
        .await
        .map_err(|_| "PC poll failed")?
}
#[tauri::command]
pub async fn pc_decide(
    window: tauri::WebviewWindow,
    state: State<'_, Arc<crate::portal::PortalState>>,
    request_id: String,
    decision: String,
    task_folder: String,
) -> Result<Value, String> {
    crate::runtime::main_window(&window)?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        native_action(&state, &request_id, &decision, Some(&task_folder))
    })
    .await
    .map_err(|_| "PC action failed")?
}
#[tauri::command]
pub fn pc_set_mode(
    window: tauri::WebviewWindow,
    request_id: String,
    mode: Mode,
    task_folder: String,
    yolo_acknowledged: bool,
) -> Result<Value, String> {
    crate::runtime::main_window(&window)?;
    let mut h = host().lock().map_err(|_| "Native PC state unavailable")?;
    let r = h
        .pending
        .get(&request_id)
        .ok_or("Choose a current native PC request")?
        .clone();
    // A native mode change revokes the previous grant. Only this explicit IPC sets it.
    h.grants.remove(&r.key());
    for (task, cancel) in h.running.values() {
        if *task == r.task_id {
            cancel.store(true, Ordering::SeqCst);
        }
    }
    h.mode(&r, mode, &task_folder, yolo_acknowledged)?;
    Ok(json!({"mode":mode,"until":h.grants[&r.key()].until}))
}
#[tauri::command]
pub async fn pc_revoke(
    window: tauri::WebviewWindow,
    state: State<'_, Arc<crate::portal::PortalState>>,
    conversation_id: Option<String>,
) -> Result<Value, String> {
    crate::runtime::main_window(&window)?;
    if let Some(conversation) = conversation_id {
        revoke_conversation(&conversation);
        let state = state.inner().clone();
        let _ = tauri::async_runtime::spawn_blocking(move || {
            state.pc_call(
                "pc.end",
                json!({"contract_version":2,"conversation_id":conversation}),
                true,
            )
        })
        .await;
    } else {
        revoke_all();
    }
    Ok(json!({"revoked":true}))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request(path: &Path) -> Request {
        let args = json!({"path":path,"content":"hello","preimage_sha256":null});
        let operation = "pc.write".to_string();
        Request {
            id: "a".repeat(32),
            task_id: "b".repeat(32),
            args_sha256: digest(&serde_json::to_vec(&json!([operation, args])).unwrap()),
            args,
            operation,
            expires_at: (now() + 600) as f64,
            lane: "lane".into(),
            owner: "dad".into(),
            agent: "assistant".into(),
            principal: "dad".into(),
            conversation_id: uuid::Uuid::new_v4().to_string(),
            credential_epoch: 1,
        }
    }
    #[test]
    fn hardware_validation_and_scoped_authority() {
        let mut r = request(&std::env::temp_dir());
        r.operation = "pc.info".into();
        for (args, valid) in [(json!({}), true), (json!({"path":"C:\\"}), false), (json!([]), false), (Value::Null, false)] {
            r.args = args;
            r.args_sha256 = digest(&serde_json::to_vec(&json!([r.operation, r.args])).unwrap());
            assert_eq!(r.validate(), valid);
        }
        r.args = json!({});
        r.args_sha256 = digest(&serde_json::to_vec(&json!([r.operation, r.args])).unwrap());
        let mut h = Host::default();
        assert!(!h.automatic(&r, now()), "no grant");
        for (mode, automatic) in [(Mode::Suggest,false),(Mode::Approve,false),(Mode::Auto,true),(Mode::Yolo,true)] {
            h.mode(&r, mode, "", mode == Mode::Yolo).unwrap();
            assert_eq!(h.automatic(&r, now()), automatic, "{mode:?}");
            let mut other = r.clone(); other.agent = "builder".into();
            assert!(!h.automatic(&other, now()), "another agent");
            other = r.clone(); other.task_id = "f".repeat(32);
            assert!(!h.automatic(&other, now()), "another task");
            other = r.clone(); other.conversation_id = uuid::Uuid::new_v4().to_string();
            assert!(!h.automatic(&other, now()), "another conversation");
            other = r.clone(); other.operation = "pc.list".into(); other.args = json!({"path":std::env::temp_dir()});
            assert!(!h.automatic(&other, now()), "hardware-only grant cannot authorize paths");
            assert!(!h.automatic(&r, now()+7201), "expired");
        }
        h.mode(&r, Mode::Approve, "", false).unwrap();
        h.grants.get_mut(&r.key()).unwrap().actions.insert("pc.info".into());
        assert!(h.automatic(&r, now()), "explicit Allow for this task covers hardware only");
        h.tainted.insert(r.task_id.clone()); assert!(!h.automatic(&r, now()));
        h.tainted.clear(); h.pending.insert(r.id.clone(), r.clone()); h.revoke_task(&r.task_id);
        assert!(!h.automatic(&r, now()), "revoked");
    }
    #[test]
    fn pc_grants_are_agent_task_lane_conversation_epoch_bound_and_expire() {
        let root = std::env::temp_dir();
        let r = request(&root.join("pc.txt"));
        let mut h = Host::default();
        h.mode(&r, Mode::Yolo, root.to_str().unwrap(), false)
            .unwrap_err();
        h.mode(&r, Mode::Yolo, root.to_str().unwrap(), true)
            .unwrap();
        assert!(h.automatic(&r, now()));
        assert!(!h.automatic(&r, now() + 1801));
        h.grants.get_mut(&r.key()).unwrap().deadline = Instant::now() - Duration::from_secs(1);
        assert!(
            !h.automatic(&r, now()),
            "a wall-clock rollback cannot extend the monotonic PC lease"
        );
        h.mode(&r, Mode::Yolo, root.to_str().unwrap(), true)
            .unwrap();
        for field in ["agent", "task", "lane", "conversation", "epoch"] {
            let mut other = r.clone();
            match field {
                "agent" => other.agent = "builder".into(),
                "task" => other.task_id = "c".repeat(32),
                "lane" => other.lane = "other".into(),
                "conversation" => other.conversation_id = uuid::Uuid::new_v4().to_string(),
                _ => other.credential_epoch += 1,
            }
            assert!(!h.automatic(&other, now()), "{field}");
        }
        let mut secret = r.clone();
        secret.args["path"] = json!(root.join(".ssh/id_rsa"));
        assert!(!h.automatic(&secret, now()));
        h.pending.insert(r.id.clone(), r.clone());
        h.revoke_task(&r.task_id);
        assert!(!h.automatic(&r, now()));
    }
    #[test]
    fn auto_reads_are_free_writes_need_scope_and_unknown_closes_grants() {
        let root = std::env::temp_dir();
        let mut r = request(&root.join("pc.txt"));
        let mut h = Host::default();
        h.mode(&r, Mode::Auto, root.to_str().unwrap(), false)
            .unwrap();
        assert!(!h.automatic(&r, now()));
        h.grants
            .get_mut(&r.key())
            .unwrap()
            .actions
            .insert("pc.write".into());
        assert!(h.automatic(&r, now()));
        r.operation = "pc.list".into();
        r.args = json!({"path":root});
        assert!(h.automatic(&r, now()));
        h.tainted.insert(r.task_id.clone());
        assert!(!h.automatic(&r, now()));
        h.mode(&r, Mode::Suggest, root.to_str().unwrap(), false)
            .unwrap();
        assert!(!h.automatic(&r, now()));
    }
    #[test]
    fn command_and_open_auto_scope_and_yolo_parity() {
        let root = std::env::temp_dir();
        let mut r = request(&root.join("pc.txt"));
        let mut h = Host::default();
        r.operation = "pc.run".into();
        r.args = json!({"shell":"cmd","argv":["/C","echo","hello"],"cwd":root});
        h.mode(&r, Mode::Yolo, root.to_str().unwrap(), true).unwrap();
        assert!(h.automatic(&r, now()));
        let mut screened = r.clone();
        screened.args["argv"] = json!(["/C","reg","delete",r"HKLM\Software\Test"]);
        assert!(!h.automatic(&screened, now()));
        h.mode(&r, Mode::Auto, root.to_str().unwrap(), false).unwrap();
        assert!(!h.automatic(&r, now()));
        h.grants.get_mut(&r.key()).unwrap().actions.insert(action_key(&r));
        r.args["argv"] = json!(["/C","echo","second"]);
        assert!(h.automatic(&r, now()), "one reviewed operation scope covers another command");
        let mut next = r.clone(); next.task_id = "e".repeat(32);
        assert!(!h.automatic(&next, now()), "new task asks");
        r.operation = "pc.open".into(); r.args = json!({"path":root.join("one.txt")});
        assert!(!h.automatic(&r, now()));
        h.grants.get_mut(&r.key()).unwrap().actions.insert(action_key(&r));
        r.args["path"] = json!(root.join("two.txt"));
        assert!(h.automatic(&r, now()));
        r.args["path"] = json!("https://example.test/another");
        assert!(h.automatic(&r, now()), "URLs share the reviewed open scope");
        h.mode(&r, Mode::Yolo, root.to_str().unwrap(), true).unwrap();
        r.args["path"] = json!(root.join("two.txt")); assert!(h.automatic(&r, now()));
        h.pending.insert(r.id.clone(), r.clone()); h.revoke_task(&r.task_id);
        assert!(!h.automatic(&r, now()));
    }
}
