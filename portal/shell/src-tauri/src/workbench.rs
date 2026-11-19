//! Native workbench view/settings. Session/effect ownership stays in functional::AppState.
use crate::functional::AppState;
use bob_engine::{
    functional::{Event, Session},
    workbench::*,
};
use serde::{Deserialize, Serialize};
use std::{
    io::{Read, Write},
    path::Path,
    sync::{
        atomic::Ordering,
        mpsc::{channel, Receiver, Sender},
        Mutex,
    },
};
use tauri::State;
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(super) struct Settings {
    pub active: String,
    pub standalone: Option<StandaloneConfig>,
    pub dispatch_id: Option<String>,
    pub task_digest: Option<String>,
    pub bob_run: Option<serde_json::Value>,
    pub bob_cancel_requested: bool,
    pub bob_error: Option<String>,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            active: "local_bob_model".into(),
            standalone: None,
            dispatch_id: None,
            task_digest: None,
            bob_run: None,
            bob_cancel_requested: false,
            bob_error: None,
        }
    }
}
pub struct WorkbenchState {
    models: Mutex<Vec<ModelChoice>>,
    pub(super) settings: Mutex<Result<Settings, String>>,
    pub(super) cached: Mutex<Option<Session>>,
    pub(super) tx: Sender<Event>,
    pub(super) rx: Mutex<Receiver<Event>>,
    pub(super) phase: Mutex<Option<(String, Lifecycle)>>,
    connection: Mutex<ConnectionState>,
    start_dispatching: std::sync::atomic::AtomicBool,
}
fn read_settings(root: &Path) -> Result<Settings, String> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let path = root.join(".workbench-settings.json");
    let file = match std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
    {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Settings::default()),
        Err(_) => return Err("workbench settings inaccessible".into()),
    };
    let meta = file
        .metadata()
        .map_err(|_| "settings metadata unavailable")?;
    if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
        return Err("settings must be a private regular file".into());
    }
    let mut bytes = vec![];
    file.take(2097153)
        .read_to_end(&mut bytes)
        .map_err(|_| "settings read failed")?;
    if bytes.len() > 2097152 {
        return Err("settings oversized".into());
    }
    let s: Settings = serde_json::from_slice(&bytes)
        .map_err(|_| "settings invalid; refusing silent profile fallback")?;
    if !["local_bob_model", "standalone_http", "bob_team"].contains(&s.active.as_str()) {
        return Err("invalid saved runtime profile".into());
    }
    if let Some(c) = &s.standalone {
        StandaloneConfig::new(&c.endpoint, &c.model, c.credential_env.clone())?;
    }
    Ok(s)
}
fn save_settings(root: &Path, s: &Settings) -> Result<(), String> {
    use std::os::unix::fs::OpenOptionsExt;
    let tmp = root.join(format!(".workbench-settings-{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&tmp)
            .map_err(|_| "settings create failed")?;
        f.write_all(&serde_json::to_vec(s).map_err(|_| "settings encoding failed")?)
            .map_err(|_| "settings write failed")?;
        f.sync_all().map_err(|_| "settings sync failed")?;
        std::fs::rename(&tmp, root.join(".workbench-settings.json"))
            .map_err(|_| "settings replace failed")?;
        bob_engine::sync_directory(root).map_err(|_| "settings directory sync failed")?;
        Ok(())
    })();
    let _ = std::fs::remove_file(tmp);
    result
}
impl WorkbenchState {
    pub(super) fn new(root: &Path) -> Self {
        let (tx, rx) = channel();
        Self {
            models: Mutex::new(vec![]),
            settings: Mutex::new(read_settings(root)),
            cached: Mutex::new(None),
            tx,
            rx: Mutex::new(rx),
            phase: Mutex::new(None),
            connection: Mutex::new(ConnectionState::Unknown),
            start_dispatching: std::sync::atomic::AtomicBool::new(false),
        }
    }
}
fn lock_error<T>(_: std::sync::PoisonError<T>) -> String {
    "workbench state faulted".into()
}
impl AppState {
    fn run_project_idle(&self,operation:impl FnOnce(&mut Session)->Result<(),String>)->Result<bob_engine::functional::AppSnapshot,String> {
        self.run_idle(|s|{
            if s.pending.is_some(){return Err("Project context locked during pending approval".into())}
            let guard=self.workbench.settings.lock().map_err(lock_error)?;
            let settings=guard.as_ref().map_err(Clone::clone)?;
            if !["local_bob_model","standalone_http"].contains(&settings.active.as_str()) || bob_locked(settings) {
                return Err("Organizational context belongs to the local conversation, not Bob-owned runs; select an idle local runtime first".into())
            }
            drop(guard);operation(s)
        })
    }
    fn save_project(&self,id:Option<&str>,expected_version:Option<u32>,name:&str,description:&str,instructions:&str)->Result<WorkbenchSnapshot,String> {
        self.run_project_idle(|s|{
            if s.pending.is_some(){return Err("Project editing locked during pending approval".into())}
            let mut projects=bob_engine::organizational::Projects::read(&self.root)?;
            if let Some(id)=id {
                Session::ensure_no_pending_group(&self.root,id)?;
                projects.edit(&self.root,id,expected_version.ok_or("Expected project version required")?,name,description,instructions)?;
            } else {
                if expected_version.is_some(){return Err("New projects cannot choose a version or ID".into())}
                projects.create(&self.root,name,description,instructions)?;
            } Ok(())
        })?;self.workbench_snapshot()
    }
    fn collapse_project(&self,id:&str,collapsed:bool)->Result<WorkbenchSnapshot,String> {
        self.run_project_idle(|_|bob_engine::organizational::Projects::read(&self.root)?.collapse(&self.root,id,collapsed))?;self.workbench_snapshot()
    }
    fn new_project_session(&self,group_id:Option<&str>)->Result<WorkbenchSnapshot,String> {
        self.run_project_idle(|s|{
            if s.pending.is_some(){return Err("New conversation locked during pending approval".into())}
            if let Some(id)=group_id {bob_engine::organizational::Projects::read(&self.root)?.get(id)?;}
            let mut next=Session::create(&self.root)?;
            if group_id.is_some(){next.move_to_group(group_id)?;}
            *s=next;Ok(())
        })?;self.workbench_snapshot()
    }
    fn move_session_group(&self,session_id:&str,group_id:Option<&str>)->Result<WorkbenchSnapshot,String> {
        self.run_project_idle(|s|{
            if s.pending.is_some(){return Err("Project membership locked during pending approval".into())}
            if s.id==session_id {s.move_to_group(group_id)}else{Session::move_saved_to_group(&self.root,session_id,group_id)}
        })?;self.workbench_snapshot()
    }
    pub(super) fn workbench_snapshot(&self) -> Result<WorkbenchSnapshot, String> {
        let request = self.request.lock().map_err(lock_error)?.clone();
        let app = match self.session.try_lock() {
            Ok(guard) => match guard.as_ref() {
                Ok(s) => Some(self.snapshot(s)?),
                Err(_) => None,
            },
            Err(_) => None,
        };
        let mut cache = self.workbench.cached.lock().map_err(lock_error)?;
        let rx = self.workbench.rx.lock().map_err(lock_error)?;
        if let Some(app) = &app {
            *cache = Some(app.session.clone());
            for _ in rx.try_iter() {}
        } else if let Some(s) = cache.as_mut() {
            for e in rx.try_iter() {
                if !s.events.iter().any(|old| old.event_id == e.event_id) {
                    s.events.push(e);
                }
            }
        }
        let settings = self
            .workbench
            .settings
            .lock()
            .map_err(lock_error)?
            .as_ref()
            .map_err(Clone::clone)?
            .clone();
        let phase = if request.is_some() {
            Some(if self.cancelled.load(Ordering::SeqCst) {
                Lifecycle::CancelRequested
            } else {
                Lifecycle::Running
            })
        } else {
            self.workbench
                .phase
                .lock()
                .map_err(lock_error)?
                .as_ref()
                .filter(|(id, _)| cache.as_ref().is_some_and(|s| &s.id == id))
                .map(|(_, p)| p.clone())
        };
        let (agents, workflows, outputs) = if settings.active == "bob_team" {
            settings
                .bob_run
                .as_ref()
                .map(|r| project_bob(r, settings.bob_cancel_requested))
                .transpose()?
                .unwrap_or_default()
        } else {
            cache
                .as_ref()
                .map(|s| project_local(s, &settings.active, phase.clone()))
                .unwrap_or_default()
        };
        let bound=if std::env::var("BOB_WSL_ENGINE").as_deref()==Ok("1"){145}else{125};
        let mut notices=vec![format!("Local model transport cancellation is cooperative, bounded to {bound} seconds; completed effects are not rolled back.")];
        if settings.dispatch_id.is_some() && settings.bob_run.is_none() {
            notices.push("Bob dispatch outcome unknown; do not retry. Attach the exact owned run ID after server inspection.".into());
        }
        if let Some(e) = &settings.bob_error {
            notices.push(e.clone());
        }
        if settings.active == "bob_team" {
            notices.push("Bob run receipts are sequential observations, not active worker inference; no tools/writes or independent verification. Full Bob sessions/memory/artifacts remain deferred.".into());
        }
        if app.is_none() {
            notices.push("Session busy/unavailable; retaining observed event projection. No session absence is implied.".into());
        }
        for model in self.workbench.models.lock().map_err(lock_error)?.iter().filter(|m| !m.configured) {
            notices.push(format!("{}: not set up; no fallback", model.label));
        }
        Ok(WorkbenchSnapshot {
            local_observation: if settings.active == "bob_team" {None} else {cache.as_ref().map(|s|LocalObservation {session_id:s.id.clone(),request_id:request.as_ref().map(|(id,_)|id.clone()),events:s.events.clone()})},
            models: self.workbench.models.lock().map_err(lock_error)?.iter().filter(|m| m.configured).cloned().collect(),
            schema_version: "workbench-view-v1".into(),
            app,
            profiles: {
                let mut choices=profiles(&settings);
                if let Some(p)=choices.iter_mut().find(|p|p.id=="local_bob_model"){p.model_label=cache.as_ref().map(|s|s.selected_model().to_owned())}
                choices
            },
            active_profile_id: settings.active.clone(),
            connection_state: if settings.active == "bob_team" {
                self.workbench
                    .connection
                    .lock()
                    .map_err(lock_error)?
                    .clone()
            } else if request.is_some() {
                ConnectionState::Connecting
            } else if phase == Some(Lifecycle::Failed) {
                ConnectionState::Disconnected
            } else if matches!(
                phase,
                Some(Lifecycle::Completed | Lifecycle::WaitingApproval | Lifecycle::Cancelled)
            ) {
                ConnectionState::Ready
            } else {
                ConnectionState::Unknown
            },
            agents,
            workflows,
            outputs,
            notices,
        })
    }
    fn update_settings(
        &self,
        change: impl FnOnce(&mut Settings) -> Result<(), String>,
    ) -> Result<WorkbenchSnapshot, String> {
        {
            let request = self.request.lock().map_err(lock_error)?;
            if request.is_some() {
                return Err("runtime settings locked to prepared/in-flight request".into());
            }
            let session = self.session.try_lock().map_err(|_| "App busy")?;
            let s = session.as_ref().map_err(Clone::clone)?;
            if s.pending.is_some() {
                return Err(
                    "runtime settings locked to pending approval; resolve/cancel first".into(),
                );
            }
            let mut guard = self.workbench.settings.lock().map_err(lock_error)?;
            let current = guard.as_ref().map_err(Clone::clone)?;
            let mut next = current.clone();
            change(&mut next)?;
            save_settings(&self.root, &next)?;
            *guard = Ok(next);
            *self.workbench.phase.lock().map_err(lock_error)? = None;
        }
        self.workbench_snapshot()
    }
    pub(super) fn select_profile(&self, id: &str) -> Result<WorkbenchSnapshot, String> {
        self.update_settings(|s| {
            if bob_locked(s) {
                return Err("Bob run active/unknown; runtime ownership cannot move".into());
            }
            if !["local_bob_model", "standalone_http", "bob_team"].contains(&id) {
                return Err("unknown runtime profile".into());
            }
            s.active = id.into();
            Ok(())
        })
    }
    pub(super) fn configure_standalone(
        &self,
        endpoint: &str,
        model: &str,
        credential_env: Option<String>,
    ) -> Result<WorkbenchSnapshot, String> {
        let c = StandaloneConfig::new(endpoint, model, credential_env)?;
        self.update_settings(|s| {
            if bob_locked(s) {
                return Err("Bob run active/unknown".into());
            }
            s.standalone = Some(c);
            Ok(())
        })
    }
    fn refresh_models_with(&self,fetch:impl FnOnce()->Result<serde_json::Value,String>)->Result<WorkbenchSnapshot,String>{
        let value=fetch()?;
        let models:Vec<ModelChoice>=serde_json::from_value(value["models"].clone()).map_err(|_|"Invalid native model catalog")?;
        if models.len()>2||models.iter().any(|m|m.image_input||m.reachability!="unverified"||!matches!((m.id.as_str(),m.model_pin.as_str()),("bob-ds-flash","deepseek-v4-flash")|("bob-gpt-sol-61","gpt-6.1-sol"))){return Err("Unapproved model catalog; no fallback".into())}
        *self.workbench.models.lock().map_err(lock_error)?=models;
        self.workbench_snapshot()
    }
    fn select_chat_model(&self,id:&str)->Result<WorkbenchSnapshot,String>{
        self.run_idle(|s|{
            if s.pending.is_some(){return Err("Model selection locked during pending approval".into())}
            let settings=self.workbench.settings.lock().map_err(lock_error)?;
            if settings.as_ref().map_err(Clone::clone)?.active!="local_bob_model"{return Err("Model picker belongs to local Bob-text inference only; runtime ownership unchanged".into())}
            if !self.workbench.models.lock().map_err(lock_error)?.iter().any(|m|m.id==id&&m.configured){return Err("Selected model unavailable in authenticated catalog; refresh choices; no fallback".into())}
            s.select_model(id)
        })?;
        *self.workbench.connection.lock().map_err(lock_error)?=ConnectionState::Unknown;
        *self.workbench.phase.lock().map_err(lock_error)?=None;
        self.workbench_snapshot()
    }
    pub(super) fn rename_session(
        &self,
        id: &str,
        title: &str,
    ) -> Result<WorkbenchSnapshot, String> {
        self.run_idle(|s| {
            if s.id == id {
                s.rename(title)
            } else {
                Session::rename_saved(&self.root, id, title)
            }
        })?;
        self.workbench_snapshot()
    }
}
fn bob_locked(s: &Settings) -> bool {
    // A deliberate start with an unknown outcome owns the runtime until the exact
    // run is inspected. An inspect-only attach never creates that marker, so a
    // failed attach (e.g. 404) cannot lock the profile.
    if s.dispatch_id.is_some() && s.bob_run.is_none() {
        return true;
    }
    s.bob_run.as_ref().is_some_and(|r| {
        !matches!(
            r["status"].as_str(),
            Some("completed" | "failed" | "cancelled" | "interrupted")
        )
    })
}
impl AppState {
    fn store_bob_result(
        &self,
        result: Result<serde_json::Value, String>,
        expected_id: Option<&str>,
    ) -> Result<WorkbenchSnapshot, String> {
        *self.workbench.connection.lock().map_err(lock_error)? = ConnectionState::Disconnected;
        {
            let mut guard = self.workbench.settings.lock().map_err(lock_error)?;
            let s = guard.as_mut().map_err(|e| e.clone())?;
            match result {
                Ok(run) => {
                    let id = run["id"]
                        .as_str()
                        .filter(|s| valid_run_id(s))
                        .ok_or("invalid Bob run receipt ID; dispatch remains unknown")?;
                    if expected_id.is_some_and(|expected| id != expected) {
                        return Err("Bob run receipt mismatch".into());
                    }
                    if s.bob_run.as_ref().is_some_and(|old| old["id"] != id) {
                        return Err("Bob run binding changed; observation rejected".into());
                    }
                    let old_terminal = s.bob_run.as_ref().is_some_and(|r| {
                        matches!(
                            r["status"].as_str(),
                            Some("completed" | "failed" | "cancelled" | "interrupted")
                        )
                    });
                    let incoming_active =
                        matches!(run["status"].as_str(), Some("running" | "accepted"));
                    if !(old_terminal && incoming_active) {
                        s.bob_run = Some(run);
                    }
                    s.bob_error = None;
                }
                Err(error) => {
                    s.bob_error = Some(error.clone());
                    save_settings(&self.root, s)?;
                    return Err(error);
                }
            }
            save_settings(&self.root, s)?;
        }
        *self.workbench.connection.lock().map_err(lock_error)? = ConnectionState::Ready;
        self.workbench_snapshot()
    }
    pub(super) fn start_bob_with(
        &self,
        task: &str,
        bridge: impl FnOnce(serde_json::Value) -> Result<serde_json::Value, String>,
    ) -> Result<WorkbenchSnapshot, String> {
        if task.trim().is_empty() || task.len() > 16000 {
            return Err("Bob task must contain 1..16000 bytes".into());
        }
        if self
            .workbench
            .start_dispatching
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("Bob start already dispatching".into());
        }
        struct DispatchGuard<'a>(&'a std::sync::atomic::AtomicBool);
        impl Drop for DispatchGuard<'_> {
            fn drop(&mut self) {
                self.0.store(false, Ordering::SeqCst);
            }
        }
        let _dispatch_guard = DispatchGuard(&self.workbench.start_dispatching);
        let digest = bob_engine::sha256_digest(task.as_bytes());
        let request_id = uuid::Uuid::new_v4().to_string();
        self.update_settings(|s|{
            if s.active!="bob_team" {return Err("Select Bob runtime before deliberate start".into());}
            if bob_locked(s) || s.task_digest.as_ref()==Some(&digest){return Err("duplicate/active/unknown Bob dispatch; attach/poll exact run, never auto-retry".into());}
            s.dispatch_id=Some(request_id.clone());s.task_digest=Some(digest);s.bob_run=None;s.bob_cancel_requested=false;s.bob_error=None;Ok(())
        })?;
        *self.workbench.connection.lock().map_err(lock_error)? = ConnectionState::Connecting;
        let result = bridge(serde_json::json!({"op":"start","request_id":request_id,"task":task}));
        self.store_bob_result(result, None)
    }
    pub(super) fn attach_bob_with(
        &self,
        id: &str,
        bridge: impl FnOnce(serde_json::Value) -> Result<serde_json::Value, String>,
    ) -> Result<WorkbenchSnapshot, String> {
        if !valid_run_id(id) {
            return Err("invalid run identifier".into());
        }
        if self.workbench.start_dispatching.load(Ordering::SeqCst) {
            return Err("Bob start is still in flight; wait for observed result/unknown outcome before explicit attach".into());
        }
        // Freeze exact target before the network call, never use selected text as authority.
        self.update_settings(|s| {
            if self.workbench.start_dispatching.load(Ordering::SeqCst) {
                return Err("Bob start still dispatching; attach cannot replace it".into());
            }
            if s.active != "bob_team" {
                return Err("Select Bob runtime before attach".into());
            }
            if let Some(r) = &s.bob_run {
                if r["id"] != id {
                    if !matches!(
                        r["status"].as_str(),
                        Some("completed" | "failed" | "cancelled" | "interrupted")
                    ) {
                        return Err(
                            "Already attached to an active run; no implicit control rebinding"
                                .into(),
                        );
                    }
                    s.bob_run = None;
                    s.bob_cancel_requested = false;
                    s.bob_error = None;
                }
            }
            Ok(())
        })?;
        *self.workbench.connection.lock().map_err(lock_error)? = ConnectionState::Connecting;
        self.store_bob_result(
            bridge(serde_json::json!({"op":"get","run_id":id})),
            Some(id),
        )
    }
    pub(super) fn cancel_bob_with(
        &self,
        id: &str,
        bridge: impl FnOnce(serde_json::Value) -> Result<serde_json::Value, String>,
    ) -> Result<WorkbenchSnapshot, String> {
        if !self.owns_store() {
            return Err("Native store is not exclusively owned; Bob control unavailable".into());
        }
        if !valid_run_id(id) {
            return Err("invalid run identifier".into());
        }
        {
            let mut guard = self.workbench.settings.lock().map_err(lock_error)?;
            let s = guard.as_mut().map_err(|e| e.clone())?;
            if s.active != "bob_team" || !s.bob_run.as_ref().is_some_and(|r| r["id"] == id) {
                return Err("cancel target is not the exact attached owned run".into());
            }
            s.bob_cancel_requested = true;
            save_settings(&self.root, s)?;
        }
        *self.workbench.connection.lock().map_err(lock_error)? = ConnectionState::Connecting;
        self.store_bob_result(
            bridge(serde_json::json!({"op":"cancel","run_id":id})),
            Some(id),
        )
    }
}
fn runtime_bridge(request: serde_json::Value) -> Result<serde_json::Value, String> {
    use std::process::{Command, Stdio};
    let target = std::env::var("BOB_WORKBENCH_SSH_TARGET").map_err(|_| "Bob team relay unavailable: set BOB_WORKBENCH_SSH_TARGET")?;
    let relay = std::env::var("BOB_WORKBENCH_RELAY").map_err(|_| "Bob team relay unavailable: set BOB_WORKBENCH_RELAY")?;
    if target.starts_with('-') || target.is_empty() || !target.bytes().all(|b| b.is_ascii_alphanumeric() || b"@._-:".contains(&b))
        || !relay.starts_with('/') || !relay.bytes().all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b)) {
        return Err("Bob team relay configuration is invalid".into());
    }
    let mut child = Command::new("timeout")
        .args([
            "--kill-after=2",
            "25",
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=8",
            &target,
            "python3",
            &relay,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "Bob runtime SSH unavailable")?;
    let bytes = serde_json::to_vec(&request).map_err(|_| "invalid bridge request")?;
    if child.stdin.take().unwrap().write_all(&bytes).is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err("Bob dispatch transport unknown; no retry".into());
    }
    let mut bytes = vec![];
    let read = child
        .stdout
        .take()
        .unwrap()
        .take(1049000)
        .read_to_end(&mut bytes);
    if read.is_err() || bytes.len() >= 1049000 {
        let _ = child.kill();
        let _ = child.wait();
        return Err("Bob response unreadable/oversized; no dispatch retry".into());
    }
    let status = child.wait().map_err(|_| "Bob bridge process unknown")?;
    let response: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| "Bob response unknown; no dispatch retry")?;
    if !status.success() || response["ok"] != true {
        let code = response["error"]
            .as_str()
            .filter(|s| s.len() < 120 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_'))
            .unwrap_or("bridge_unavailable");
        return Err(format!(
            "Bob runtime unavailable: {code}; no automatic dispatch retry"
        ));
    }
    Ok(response["run"].clone())
}
#[tauri::command(async)]
pub fn workbench_start_bob_run(window: tauri::WebviewWindow, 
    state: State<AppState>,
    task: String,
) -> Result<WorkbenchSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.start_bob_with(&task, runtime_bridge)
}
#[tauri::command(async)]
pub fn workbench_attach_bob_run(window: tauri::WebviewWindow, 
    state: State<AppState>,
    run_id: String,
) -> Result<WorkbenchSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.attach_bob_with(&run_id, runtime_bridge)
}
#[tauri::command(async)]
pub fn workbench_cancel_bob_run(window: tauri::WebviewWindow, 
    state: State<AppState>,
    run_id: String,
) -> Result<WorkbenchSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.cancel_bob_with(&run_id, runtime_bridge)
}
fn profiles(s: &Settings) -> Vec<RuntimeProfile> {
    [
        (
            "local_bob_model",
            "Local agent + Bob model",
            true,
            Some("bob-ds-flash".to_owned()),
        ),
        (
            "standalone_http",
            "Local agent + own endpoint",
            s.standalone.is_some(),
            s.standalone.as_ref().map(|c| c.model.clone()),
        ),
        ("bob_team", "Bob read-only team", true, None),
    ]
    .into_iter()
    .map(|(id, label, configured, model_label)| {
        let bob = id == "bob_team";
        RuntimeProfile {
            id: id.into(),
            label: label.into(),
            adapter_kind: if bob {
                AdapterKind::AgentRuntime
            } else {
                AdapterKind::ModelOnly
            },
            execution_host: if bob { "Bob server" } else { "local" }.into(),
            model_label,
            memory_owner: if bob {
                "Bob-owned run"
            } else {
                "functional-app-v2 local session"
            }
            .into(),
            effect_owner: if bob {
                EffectOwner::BobRuntime
            } else {
                EffectOwner::CleanroomLocal
            },
            configured,
            capabilities: Capabilities {
                chat: !bob,
                project_tools: !bob,
                writes: !bob,
                multi_agent: bob,
                workflows: true,
                cancel: true,
                steer: false,
                reconnect: true,
            },
            unavailable_reason: if !configured {
                Some("Explicit own endpoint not configured; no Bob fallback".into())
            } else {
                None
            },
        }
    })
    .collect()
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;
    fn root() -> PathBuf {
        std::env::temp_dir().join(format!("workbench-bob-native-{}", uuid_for_test()))
    }
    fn uuid_for_test() -> String {
        format!(
            "{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        )
    }
    #[test]
    #[ignore = "exactly one owner-authorized bounded live Bob acceptance; explicit environment required"]
    fn owner_authorized_single_live_bob_run() {
        assert_eq!(
            std::env::var("BOB_P1_LIVE_ACCEPT").as_deref(),
            Ok("allow-one-run")
        );
        let root = PathBuf::from(
            std::env::var("BOB_P1_LIVE_DATA_ROOT")
                .expect("disposable explicitly scoped root required"),
        );
        assert!(
            !root.exists(),
            "fresh disposable root only; never retry live start"
        );
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        let task="Synthetic P1 runtime acceptance only. Supplied evidence: alpha is declared red; beta is declared blue. Summarize these declarations and note no filesystem or tools were inspected. Keep each role output below 100 words. Manager: plan exactly one short worker task. No actions or external research.";
        let started = state.start_bob_with(task, runtime_bridge).unwrap();
        let rid = started.workflows[0].run_id.clone();
        std::fs::write(
            root.join("start-snapshot.json"),
            serde_json::to_vec_pretty(&started).unwrap(),
        )
        .unwrap();
        let mut snapshots = vec![];
        let mut last;
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(205);
        loop {
            last = state
                .attach_bob_with(&rid, runtime_bridge)
                .expect("read back exact owned run");
            snapshots.push(serde_json::to_value(&last).unwrap());
            std::fs::write(
                root.join("observed-snapshots.json"),
                serde_json::to_vec_pretty(&snapshots).unwrap(),
            )
            .unwrap();
            if matches!(
                last.workflows[0].state,
                Lifecycle::Completed | Lifecycle::Failed | Lifecycle::Cancelled
            ) {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "bounded live poll deadline; never restart run"
            );
            std::thread::sleep(std::time::Duration::from_secs(2));
        }
        let run = state
            .workbench
            .settings
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .bob_run
            .clone()
            .unwrap();
        std::fs::write(
            root.join("live-run.json"),
            serde_json::to_vec_pretty(&run).unwrap(),
        )
        .unwrap();
        std::fs::write(
            root.join("final-snapshot.json"),
            serde_json::to_vec_pretty(&last).unwrap(),
        )
        .unwrap();
        println!(
            "LIVE_RUN_ID={rid} STATUS={} RECEIPTS={}",
            run["status"],
            last.agents.len()
        );
        assert!(state
            .start_bob_with(task, |_| panic!("live duplicate dispatch forbidden"))
            .is_err());
        let cancel = state.cancel_bob_with(&rid, runtime_bridge).unwrap();
        assert_eq!(
            cancel.workflows[0].state, last.workflows[0].state,
            "terminal run must not be relabelled cancelled"
        );
        assert_eq!(
            last.workflows[0].state,
            Lifecycle::Completed,
            "save real failure evidence; do not retry a second run"
        );
        assert_eq!(last.agents.len(), 4);
    }
    #[test]
    #[ignore = "read-only final acceptance against the already-authorized existing run; no start"]
    fn final_existing_run_attach_only() {
        assert_eq!(
            std::env::var("BOB_P1_ATTACH_ONLY").as_deref(),
            Ok("read-only")
        );
        let rid = std::env::var("BOB_P1_EXISTING_RUN_ID").unwrap();
        let root = PathBuf::from(std::env::var("BOB_P1_ATTACH_DATA_ROOT").unwrap());
        assert!(!root.exists(), "disposable fresh store required");
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        let snap = state.attach_bob_with(&rid, runtime_bridge).unwrap();
        assert_eq!(snap.workflows[0].state, Lifecycle::Completed);
        assert_eq!(snap.agents.len(), 4);
        assert!(matches!(snap.connection_state, ConnectionState::Ready));
        drop(state);
        let reopened = AppState::at_root(root.clone());
        assert!(matches!(
            reopened.workbench_snapshot().unwrap().connection_state,
            ConnectionState::Unknown
        ));
        let snap = reopened.attach_bob_with(&rid, runtime_bridge).unwrap();
        assert_eq!(snap.agents.len(), 4);
        assert_eq!(snap.workflows[0].state, Lifecycle::Completed);
        std::fs::write(
            root.join("final-existing-run-snapshot.json"),
            serde_json::to_vec_pretty(&snap).unwrap(),
        )
        .unwrap();
        println!("READ_ONLY_FINAL_ATTACH={rid}, four actual receipts, reopened unknown->measured ready; zero start calls");
    }
    #[test]
    fn organizational_context_never_edits_or_implies_next_turn_on_bob_owned_runtime() {
        let root=root();let state=AppState::at_root(root.clone());state.save_project(None,None,"Local only","","Synthetic").unwrap();
        let id=bob_engine::organizational::Projects::read(&root).unwrap().items[0].id.clone();let sid=state.run(|_|Ok(())).unwrap().session.id;
        state.select_profile("bob_team").unwrap();
        assert!(state.save_project(Some(&id),Some(1),"Not Bob context","","changed").is_err());
        assert!(state.new_project_session(Some(&id)).is_err());
        assert!(state.move_session_group(&sid,Some(&id)).is_err());
        assert!(state.collapse_project(&id,true).is_err());
        let snap=state.workbench_snapshot().unwrap();assert_eq!(snap.active_profile_id,"bob_team");assert!(snap.workflows.is_empty());
        state.select_profile("local_bob_model").unwrap();state.move_session_group(&sid,Some(&id)).unwrap();
        drop(state);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn project_context_edit_cannot_cross_an_unselected_saved_pending_approval() {
        let root=root();let state=AppState::at_root(root.clone());state.save_project(None,None,"Pending group","","Synthetic").unwrap();
        let id=bob_engine::organizational::Projects::read(&root).unwrap().items[0].id.clone();
        let mut saved=Session::create(&root).unwrap();saved.move_to_group(Some(&id)).unwrap();
        let folder=root.join("synthetic-other");std::fs::create_dir(&folder).unwrap();saved.attach(&folder).unwrap();
        saved.turn("Pending fixture",&mut |_|Ok(json!({"ok":true,"content":r#"{"type":"action","action":{"tool":"write","path":"p.txt","content":"not approved"}}"#}))).unwrap();
        let prior=std::fs::read(root.join(format!("{}.json",saved.id))).unwrap();
        assert!(state.save_project(Some(&id),Some(1),"Changed","","Not allowed").is_err());
        assert!(state.move_session_group(&saved.id,None).is_err());
        assert_eq!(std::fs::read(root.join(format!("{}.json",saved.id))).unwrap(),prior);
        assert!(!folder.join("p.txt").exists());drop(state);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn organizational_projects_are_native_durable_and_locked_to_request_and_pending_approval() {
        let root=root();let state=AppState::at_root(root.clone());
        let snap=state.save_project(None,None,"Native Alpha","Synthetic","marker NATIVE-ALPHA").unwrap();
        let value=serde_json::to_value(snap.app.unwrap()).unwrap();
        let group=value["organizational_projects"][0]["id"].as_str().unwrap();
        let id=group.to_owned();state.new_project_session(Some(&id)).unwrap();
        let before=state.run(|_|Ok(())).unwrap();
        assert_eq!(serde_json::to_value(&before).unwrap()["group_id"],id);
        let token=state.begin().unwrap();
        assert!(state.save_project(Some(&id),Some(1),"Blocked","","changed").is_err());
        assert!(state.new_project_session(None).is_err());
        assert!(state.move_session_group(&before.session.id,None).is_err());
        *state.request.lock().unwrap()=None;drop(token);
        let folder=root.join("synthetic-attached");std::fs::create_dir(&folder).unwrap();
        state.run(|s|{s.attach(&folder)?;s.turn("Propose synthetic write",&mut |_|Ok(json!({"ok":true,"content":r#"{"type":"action","action":{"tool":"write","path":"pending.txt","content":"unapproved"}}"#})))}).unwrap();
        let pending=state.run(|_|Ok(())).unwrap().session.pending.unwrap();
        assert!(state.save_project(Some(&id),Some(1),"Blocked","","changed").is_err());
        assert!(state.move_session_group(&before.session.id,None).is_err());
        assert_eq!(state.run(|_|Ok(())).unwrap().session.pending.unwrap().digest,pending.digest);
        assert!(!folder.join("pending.txt").exists());
        state.run(|s|s.cancel()).unwrap();state.move_session_group(&before.session.id,None).unwrap();
        assert_eq!(state.run(|_|Ok(())).unwrap().session.project.unwrap().root,folder);
        state.collapse_project(&id,true).unwrap();drop(state);
        let state=AppState::at_root(root.clone());let v=serde_json::to_value(state.run(|_|Ok(())).unwrap()).unwrap();
        assert_eq!(v["organizational_projects"][0]["collapsed"],true);
        assert_eq!(v["organizational_projects"][0]["version"],1);
        drop(state);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn model_catalog_hides_unconfigured_choices_reports_them_and_keeps_exact_pins() {
        let root = root();
        let state = AppState::at_root(root.clone());
        let view = state.refresh_models_with(|| Ok(json!({"ok":true,"models":[
            {"id":"bob-ds-flash","label":"DSv4.1 Flash","model_pin":"deepseek-v4-flash","configured":false,"reachability":"unverified","image_input":false,"reason":"Not set up; no fallback"},
            {"id":"bob-gpt-sol-61","label":"GPT SOL 6.1","model_pin":"gpt-6.1-sol","configured":true,"reachability":"unverified","image_input":false,"reason":null}
        ]}))).unwrap();
        assert_eq!(view.models.len(), 1);
        assert_eq!(view.models[0].id, "bob-gpt-sol-61");
        assert!(view.notices.iter().any(|s| s == "DSv4.1 Flash: not set up; no fallback"));
        assert!(state.select_chat_model("bob-ds-flash").is_err());
        assert_eq!(state.run(|_| Ok(())).unwrap().selected_model_id, "bob-ds-flash");
        for (id, pin) in [("bob-qwen-strata", "qwen"), ("bob-ds-flash", "deepseek-v4-pro"), ("bob-gpt-sol-61", "other")] {
            assert!(state.refresh_models_with(|| Ok(json!({"models":[
                {"id":id,"label":"Invalid","model_pin":pin,"configured":true,"reachability":"unverified","image_input":false,"reason":null}
            ]}))).is_err());
        }
        assert_eq!(state.workbench_snapshot().unwrap().models[0].id, "bob-gpt-sol-61");
        drop(state); std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn chat_model_selection_is_native_session_bound_and_frozen_for_request_and_approval(){
        let root=root();let state=AppState::at_root(root.clone());
        state.refresh_models_with(||Ok(serde_json::json!({"ok":true,"models":[{"id":"bob-gpt-sol-61","label":"GPT SOL 6.1","model_pin":"gpt-6.1-sol","configured":true,"reachability":"unverified","image_input":false,"reason":null}]}))).unwrap();
        let before=state.run(|_|Ok(())).unwrap().session;
        state.select_chat_model("bob-gpt-sol-61").unwrap();
        assert_eq!(state.run(|_|Ok(())).unwrap().selected_model_id,"bob-gpt-sol-61");
        assert_eq!(state.run(|_|Ok(())).unwrap().session.id,before.id);
        let token=state.begin().unwrap();assert!(state.select_chat_model("bob-gpt-sol-61").is_err());*state.request.lock().unwrap()=None;drop(token);
        let project=root.join("synthetic-project");std::fs::create_dir(&project).unwrap();
        state.run(|s|{s.attach(&project)?;s.turn("propose",&mut |_|Ok(serde_json::json!({"ok":true,"content":r#"{"type":"action","action":{"tool":"write","path":"pending.txt","content":"not approved"}}"#})))}).unwrap();
        assert!(state.select_chat_model("bob-gpt-sol-61").is_err());
        assert!(!project.join("pending.txt").exists());
        drop(state);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn attach_cannot_replace_a_start_that_is_still_on_the_wire() {
        let root = root();
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        let rid = "00000000-0000-0000-0000-000000000001";
        assert!(state
            .start_bob_with("synthetic", |_| {
                assert!(state
                    .attach_bob_with(rid, |_| panic!("must not replace in-flight dispatch"))
                    .is_err());
                assert!(matches!(
                    state.workbench_snapshot().unwrap().connection_state,
                    ConnectionState::Connecting
                ));
                Err("unknown transport outcome".into())
            })
            .is_err());
        state
            .attach_bob_with(rid, |_| {
                Ok(json!({"id":rid,"status":"completed","receipts":[]}))
            })
            .unwrap();
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn failed_attach_leaves_no_phantom_dispatch_and_profile_stays_movable() {
        let root = root();
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        let rid = "11111111-1111-1111-1111-111111111111";
        assert!(state
            .attach_bob_with(rid, |_| Err(
                "Bob runtime unavailable: bob_http_404; no automatic dispatch retry".into()
            ))
            .is_err());
        assert!(
            state
                .workbench
                .settings
                .lock()
                .unwrap()
                .as_ref()
                .unwrap()
                .dispatch_id
                .is_none(),
            "inspect-only attach must not create a start dispatch marker"
        );
        let snap = state
            .select_profile("local_bob_model")
            .expect("a failed inspect-only attach must not lock runtime ownership");
        assert_eq!(snap.active_profile_id, "local_bob_model");
        assert!(matches!(snap.connection_state, ConnectionState::Unknown));
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn changing_profile_cannot_reuse_another_endpoints_connection_measurement() {
        let root = root();
        let state = AppState::at_root(root.clone());
        let rid = "00000000-0000-0000-0000-000000000001";
        state.select_profile("bob_team").unwrap();
        state
            .attach_bob_with(rid, |_| {
                Ok(json!({"id":rid,"status":"completed","receipts":[]}))
            })
            .unwrap();
        assert!(state
            .attach_bob_with(rid, |_| Err("old Bob link lost".into()))
            .is_err());
        let snap = state.select_profile("standalone_http").unwrap();
        assert!(
            matches!(snap.connection_state, ConnectionState::Unknown),
            "Bob failure cannot diagnose a different own endpoint"
        );
        state.run(|_| Ok(())).unwrap();
        let sid = state
            .workbench
            .cached
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .id
            .clone();
        *state.workbench.phase.lock().unwrap() = Some((sid, Lifecycle::Completed));
        let snap = state
            .configure_standalone("http://127.0.0.1:1/v1/chat/completions", "new-model", None)
            .unwrap();
        assert!(
            matches!(snap.connection_state, ConnectionState::Unknown),
            "new endpoint is not proven live by an earlier completion"
        );
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn persisted_receipts_do_not_claim_a_live_connection_after_restart() {
        let root = root();
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        let rid = "00000000-0000-0000-0000-000000000001";
        let snap = state
            .attach_bob_with(rid, |_| {
                Ok(json!({"id":rid,"status":"completed","receipts":[]}))
            })
            .unwrap();
        assert!(matches!(snap.connection_state, ConnectionState::Ready));
        drop(state);
        let reopened = AppState::at_root(root.clone());
        let snap = reopened.workbench_snapshot().unwrap();
        assert!(
            matches!(snap.connection_state, ConnectionState::Unknown),
            "persisted observation is not liveness"
        );
        assert_eq!(snap.workflows[0].state, Lifecycle::Completed);
        drop(reopened);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn attach_can_select_another_owned_terminal_run_but_never_rebind_an_active_run() {
        let root = root();
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        let a = "00000000-0000-0000-0000-000000000001";
        let b = "00000000-0000-0000-0000-000000000002";
        state
            .attach_bob_with(a, |_| {
                Ok(json!({"id":a,"status":"completed","receipts":[]}))
            })
            .unwrap();
        state
            .attach_bob_with(b, |_| Ok(json!({"id":b,"status":"running","receipts":[]})))
            .expect("explicit read-only attach after terminal must be usable");
        assert!(state
            .attach_bob_with(a, |_| panic!("must not change active target"))
            .is_err());
        assert_eq!(state.workbench_snapshot().unwrap().workflows[0].run_id, b);
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn second_process_without_store_lock_cannot_cancel_bob() {
        let root = root();
        let owner = AppState::at_root(root.clone());
        owner.select_profile("bob_team").unwrap();
        let rid = "00000000-0000-0000-0000-000000000001";
        owner
            .start_bob_with("synthetic", |_| {
                Ok(json!({"id":rid,"status":"running","receipts":[]}))
            })
            .unwrap();
        let second = AppState::at_root(root.clone());
        assert!(second
            .cancel_bob_with(rid, |_| panic!(
                "unowned native store cannot dispatch cancellation"
            ))
            .is_err());
        drop(second);
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn settings_special_files_fail_closed_without_blocking() {
        use std::os::unix::{ffi::OsStrExt, fs::symlink};
        let root = root();
        let state = AppState::at_root(root.clone());
        drop(state);
        let path = root.join(".workbench-settings.json");
        symlink("/etc/passwd", &path).unwrap();
        assert!(read_settings(&root).is_err());
        std::fs::remove_file(&path).unwrap();
        let raw = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(raw.as_ptr(), 0o600) }, 0);
        let start = std::time::Instant::now();
        assert!(read_settings(&root).is_err());
        assert!(start.elapsed() < std::time::Duration::from_secs(1));
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn unknown_dispatch_is_durable_and_never_replayed() {
        let root = root();
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        assert!(state
            .start_bob_with("synthetic", |_| Err("transport timed out".into()))
            .is_err());
        assert!(state
            .start_bob_with("synthetic", |_| panic!("duplicate dispatch"))
            .is_err());
        drop(state);
        let reopened = AppState::at_root(root.clone());
        assert!(reopened
            .start_bob_with("synthetic", |_| panic!("restart replay"))
            .is_err());
        assert!(reopened
            .workbench_snapshot()
            .unwrap()
            .notices
            .iter()
            .any(|s| s.contains("unknown")));
        drop(reopened);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn bob_cancel_is_exact_bound_requested_until_terminal_observation() {
        let root = root();
        let state = AppState::at_root(root.clone());
        state.select_profile("bob_team").unwrap();
        let rid = "00000000-0000-0000-0000-000000000001";
        state
            .start_bob_with("synthetic", |request| {
                assert_eq!(request["op"], "start");
                Ok(json!({"id":rid,"status":"running","receipts":[]}))
            })
            .unwrap();
        assert!(state
            .start_bob_with("synthetic", |_| panic!("duplicate"))
            .is_err());
        assert!(state
            .cancel_bob_with("00000000-0000-0000-0000-000000000002", |_| panic!(
                "wrong run"
            ))
            .is_err());
        assert!(state.select_profile("local_bob_model").is_err());
        state
            .cancel_bob_with(rid, |_| {
                Ok(json!({"id":rid,"status":"running","receipts":[]}))
            })
            .unwrap();
        assert_eq!(
            state.workbench_snapshot().unwrap().workflows[0].state,
            Lifecycle::CancelRequested
        );
        state
            .attach_bob_with(rid, |request| {
                assert_eq!(request["op"], "get");
                Ok(json!({"id":rid,"status":"cancelled","receipts":[]}))
            })
            .unwrap();
        assert_eq!(
            state.workbench_snapshot().unwrap().workflows[0].state,
            Lifecycle::Cancelled
        );
        state.select_profile("local_bob_model").unwrap();
        assert!(state.begin().is_ok());
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
}
#[tauri::command]
pub fn workbench_save_project(window: tauri::WebviewWindow, state:State<AppState>,project_id:Option<String>,expected_version:Option<u32>,name:String,description:String,instructions:String)->Result<WorkbenchSnapshot,String>{
    crate::runtime::main_window(&window)?;state.save_project(project_id.as_deref(),expected_version,&name,&description,&instructions)}
#[tauri::command]
pub fn workbench_collapse_project(window: tauri::WebviewWindow, state:State<AppState>,project_id:String,collapsed:bool)->Result<WorkbenchSnapshot,String>{
    crate::runtime::main_window(&window)?;state.collapse_project(&project_id,collapsed)}
#[tauri::command]
pub fn workbench_new_project_session(window: tauri::WebviewWindow, state:State<AppState>,group_id:Option<String>)->Result<WorkbenchSnapshot,String>{
    crate::runtime::main_window(&window)?;state.new_project_session(group_id.as_deref())}
#[tauri::command]
pub fn workbench_move_session_group(window: tauri::WebviewWindow, state:State<AppState>,session_id:String,group_id:Option<String>)->Result<WorkbenchSnapshot,String>{
    crate::runtime::main_window(&window)?;state.move_session_group(&session_id,group_id.as_deref())}
#[tauri::command]
pub fn workbench_snapshot(window: tauri::WebviewWindow, state: State<AppState>) -> Result<WorkbenchSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.workbench_snapshot()
}
#[tauri::command]
pub fn workbench_select_profile(window: tauri::WebviewWindow, 
    state: State<AppState>,
    profile_id: String,
) -> Result<WorkbenchSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.select_profile(&profile_id)
}
#[tauri::command]
pub fn workbench_configure_standalone(window: tauri::WebviewWindow, 
    state: State<AppState>,
    endpoint: String,
    model: String,
    credential_env: Option<String>,
) -> Result<WorkbenchSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.configure_standalone(&endpoint, &model, credential_env)
}
#[tauri::command]
pub fn workbench_rename_session(window: tauri::WebviewWindow, 
    state: State<AppState>,
    session_id: String,
    title: String,
) -> Result<WorkbenchSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.rename_session(&session_id, &title)
}
#[tauri::command]
pub fn workbench_pick_project(window: tauri::WebviewWindow, ) -> Result<Option<String>, String> {
    crate::runtime::main_window(&window)?;
    Err("Native folder chooser unavailable in P1; use explicit path entry and app_attach validation".into())
}

#[tauri::command]
pub fn workbench_organize_session(window: tauri::WebviewWindow, state:State<AppState>,session_id:String,pinned:bool,archived:bool)->Result<WorkbenchSnapshot,String>{
    crate::runtime::main_window(&window)?;
    state.run_idle(|s|if s.id==session_id{s.organize(pinned,archived)}else{Session::organize_saved(&state.root,&session_id,pinned,archived)})?;
    state.workbench_snapshot()
}

#[tauri::command(async)]
pub fn workbench_refresh_models(window: tauri::WebviewWindow, state:State<AppState>)->Result<WorkbenchSnapshot,String>{
    crate::runtime::main_window(&window)?;
    state.refresh_models_with(||bob_engine::model_transport::request(&serde_json::json!({"op":"catalog"})))
}
#[tauri::command]
pub fn workbench_select_model(window: tauri::WebviewWindow, state:State<AppState>,model_id:String)->Result<WorkbenchSnapshot,String>{
    crate::runtime::main_window(&window)?;state.select_chat_model(&model_id)}
