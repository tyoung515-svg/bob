//! Native app-owned capability boundary; webview never gets model credentials.
use bob_engine::functional::{relay, AppSnapshot, Message, Session};
use std::{
    fs::File,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Mutex,
    },
};
use tauri::State;
static NEXT_REQUEST: AtomicU64 = AtomicU64::new(1);
pub struct AppState {
    pub(super) session: Mutex<Result<Session, String>>,
    pub(super) root: PathBuf,
    pub(super) cancelled: AtomicBool,
    pub(super) request: Mutex<Option<(String, bool)>>,
    pub(super) workbench: crate::workbench::WorkbenchState,
    _lock: Option<File>,
}
impl Default for AppState {
    fn default() -> Self {
        let root = std::env::var_os("BOB_FUNCTIONAL_DATA_ROOT")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                std::env::var_os("XDG_DATA_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| {
                        PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
                            .join(".local/share")
                    })
                    .join("bob-functional-v2")
            });
        if app_developer_mode() {
            return Self {
                session: Mutex::new(Err(
                    "Developer fixture mode: functional store not opened".into()
                )),
                workbench: crate::workbench::WorkbenchState::new(&root),
                root,
                cancelled: AtomicBool::new(false),
                request: Mutex::new(None),
                _lock: None,
            };
        }
        Self::at_root(root)
    }
}
impl AppState {
    pub(super) fn at_root(root: PathBuf) -> Self {
        use std::os::{fd::AsRawFd, unix::fs::OpenOptionsExt};
        let mut lock = None;
        let session = (|| {
            Session::saved(&root)?;
            let f = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(root.join(".app.lock"))
                .map_err(|e| e.to_string())?;
            if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                return Err(
                    "This private session store is already open in another app process".into(),
                );
            }
            lock = Some(f);
            Session::create(&root)
        })();
        Self {
            session: Mutex::new(session),
            workbench: crate::workbench::WorkbenchState::new(&root),
            root,
            cancelled: AtomicBool::new(false),
            request: Mutex::new(None),
            _lock: lock,
        }
    }
    pub(super) fn owns_store(&self) -> bool {
        self._lock.is_some()
    }
    pub(super) fn snapshot(&self, session: &Session) -> Result<AppSnapshot, String> {
        Ok(AppSnapshot {
            group_id: session.group_id().map(str::to_owned),
            organizational_projects: bob_engine::organizational::Projects::read(&self.root)?.items,
            selected_model_id: session.selected_model().to_owned(),
            message_provenance: session.message_provenance(),
            session: session.clone(),
            saved: Session::saved(&self.root)?,
            data_root: self.root.clone(),
        })
    }
    pub(super) fn run(
        &self,
        operation: impl FnOnce(&mut Session) -> Result<(), String>,
    ) -> Result<AppSnapshot, String> {
        let mut guard = self
            .session
            .try_lock()
            .map_err(|_| "App busy; wait or request cancellation")?;
        let session = guard.as_mut().map_err(|e| e.clone())?;
        session.observe(self.workbench.tx.clone());
        for _ in self
            .workbench
            .rx
            .lock()
            .map_err(|_| "observer fault")?
            .try_iter()
        {}
        *self.workbench.cached.lock().map_err(|_| "observer fault")? = Some(session.clone());
        let result = operation(session);
        *self.workbench.cached.lock().map_err(|_| "observer fault")? = Some(session.clone());
        result?;
        self.snapshot(session)
    }
    /// Mutating commands cannot rebind a prepared request to another session/project.
    pub(super) fn run_idle(
        &self,
        operation: impl FnOnce(&mut Session) -> Result<(), String>,
    ) -> Result<AppSnapshot, String> {
        let active = self.request.lock().map_err(|_| "request state faulted")?;
        if active.is_some() {
            return Err("Session/project metadata locked to prepared/running request".into());
        }
        self.run(operation)
    }
    /// A short synchronous reservation closes the UI-to-async-dispatch cancel race.
    /// This id is a correlation value, not a credential or an approval grant.
    pub(super) fn begin(&self) -> Result<String, String> {
        let mut active = self.request.lock().map_err(|e| e.to_string())?;
        if active.is_some() {
            return Err("A model request is already prepared/running".into());
        }
        let _guard = self.session.try_lock().map_err(|_| "App busy")?;
        let settings = self
            .workbench
            .settings
            .lock()
            .map_err(|_| "settings fault")?;
        let settings = settings.as_ref().map_err(Clone::clone)?;
        if settings.active == "bob_team" {
            return Err("Bob owns team execution; use explicit workbench_start_bob_run".into());
        }
        self.cancelled.store(false, Ordering::SeqCst);
        let id = format!(
            "request-{}-{}",
            std::process::id(),
            NEXT_REQUEST.fetch_add(1, Ordering::SeqCst)
        );
        *active = Some((id.clone(), false));
        Ok(id)
    }
    fn run_model(
        &self,
        token: &str,
        operation: impl FnOnce(&mut Session) -> Result<(), String>,
    ) -> Result<AppSnapshot, String> {
        {
            let mut active = self.request.lock().map_err(|e| e.to_string())?;
            match active.as_mut() {
                Some((id, running)) if id == token && !*running => *running = true,
                _ => return Err("model request reservation mismatch or already dispatched".into()),
            }
        }
        let result=self.run(|session|{
   if self.cancelled.load(Ordering::SeqCst){session.cancel()?;return Err("Cancelled before dispatch; no action attempted".into())}
   let result=operation(session);
   if self.cancelled.load(Ordering::SeqCst){session.cancel()?;return Err("Cancelled. Inspect history for any already-completed file effect; no response action is dispatched after cancellation.".into())}result
  });
        if let Some(s) = self
            .workbench
            .cached
            .lock()
            .map_err(|_| "observer fault")?
            .as_ref()
        {
            let phase = if self.cancelled.load(Ordering::SeqCst) {
                bob_engine::workbench::Lifecycle::Cancelled
            } else if result.is_err() {
                bob_engine::workbench::Lifecycle::Failed
            } else if s.pending.is_some() {
                bob_engine::workbench::Lifecycle::WaitingApproval
            } else {
                bob_engine::workbench::Lifecycle::Completed
            };
            *self.workbench.phase.lock().map_err(|_| "phase fault")? = Some((s.id.clone(), phase));
        }
        *self.request.lock().map_err(|e| e.to_string())? = None;
        self.cancelled.store(false, Ordering::SeqCst);
        result
    }
    fn request_cancel(&self) -> Result<String, String> {
        let active = self.request.lock().map_err(|e| e.to_string())?;
        self.cancelled.store(true, Ordering::SeqCst);
        if active.is_none() {
            let mut guard = self.session.try_lock().map_err(|_| "App busy")?;
            guard.as_mut().map_err(|e| e.clone())?.cancel()?;
            self.cancelled.store(false, Ordering::SeqCst);
        }
        let bound=if std::env::var("BOB_WSL_ENGINE").as_deref()==Ok("1"){145}else{125};
        Ok(format!("Cancellation requested. An in-flight SSH completion may take up to {bound} seconds to stop. Already-completed file effects are not rolled back."))
    }
    #[cfg(test)]
    fn model(&self,messages:&[Message])->Result<serde_json::Value,String>{self.model_for(messages,"bob-ds-flash")}
    fn model_for(&self, messages: &[Message],selected:&str) -> Result<serde_json::Value, String> {
        if self.cancelled.load(Ordering::SeqCst) {
            return Err("cancelled before model call".into());
        }
        let settings = self
            .workbench
            .settings
            .lock()
            .map_err(|_| "settings fault")?
            .as_ref()
            .map_err(Clone::clone)?
            .clone();
        let response = match settings.active.as_str() {
            "local_bob_model" => {
                let mut value=match selected {
                    "bob-ds-flash"=>relay(messages)?,
                    "bob-gpt-sol-61"=>bob_engine::model_transport::complete(selected,messages)?,
                    _=>return Err("Selected model unavailable; no fallback".into()),
                };
                value["requested_model"]=serde_json::json!(selected);value
            },
            "standalone_http" => settings
                .standalone
                .ok_or("Own endpoint not configured; no fallback")?
                .complete(messages)?,
            _ => return Err("Bob team profile is not a local model transport".into()),
        };
        if self.cancelled.load(Ordering::SeqCst) {
            return Err("cancelled; model response discarded before action dispatch".into());
        }
        Ok(response)
    }
}
#[tauri::command]
pub fn app_boot(window: tauri::WebviewWindow, state: State<AppState>) -> Result<AppSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.run(|_| Ok(()))
}
#[tauri::command]
pub fn app_begin(window: tauri::WebviewWindow, state: State<AppState>) -> Result<String, String> {
    crate::runtime::main_window(&window)?;
    state.begin()
}
#[tauri::command]
pub fn app_release(window: tauri::WebviewWindow, state: State<AppState>, token: String) -> Result<(), String> {
    crate::runtime::main_window(&window)?;
    let mut active = state.request.lock().map_err(|e| e.to_string())?;
    if active
        .as_ref()
        .is_some_and(|(id, running)| id == &token && !*running)
    {
        *active = None;
        state.cancelled.store(false, Ordering::SeqCst);
    }
    Ok(())
}
#[tauri::command(async)]
pub fn app_send(window: tauri::WebviewWindow, 
    state: State<AppState>,
    token: String,
    input: String,
    attachments: Option<Vec<bob_engine::attachments::Upload>>,
    session_id: Option<String>,
) -> Result<AppSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.run_model(&token, |s| {
        let uploads=attachments.unwrap_or_default();let selected=s.selected_model().to_owned();
        if uploads.is_empty(){s.turn(&input,&mut |m|state.model_for(m,&selected))}
        else {
            if session_id.as_deref()!=Some(s.id.as_str()){return Err("Attachment draft/session mismatch; no dispatch".into())}
            s.turn_with_attachments(&input,uploads,&mut |m|state.model_for(m,&selected))
        }
    })
}
#[tauri::command]
pub fn app_attach(window: tauri::WebviewWindow, state: State<AppState>, path: String) -> Result<AppSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.run_idle(|s| s.attach(Path::new(&path)))
}
#[tauri::command(async)]
pub fn app_resolve(window: tauri::WebviewWindow, 
    state: State<AppState>,
    token: String,
    id: String,
    digest: String,
    allow: bool,
) -> Result<AppSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.run_model(&token, |s| {
        let selected=s.selected_model().to_owned();
        s.resolve(&id, &digest, allow, &mut |m| state.model_for(m,&selected))
    })
}
#[tauri::command]
pub fn app_cancel(window: tauri::WebviewWindow, state: State<AppState>) -> Result<String, String> {
    crate::runtime::main_window(&window)?;
    state.request_cancel()
}
#[tauri::command]
pub fn app_new(window: tauri::WebviewWindow, state: State<AppState>) -> Result<AppSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.run_idle(|s| {
        if s.pending.is_some() {
            s.cancel()?
        }
        *s = Session::create(&state.root)?;
        Ok(())
    })
}
#[tauri::command]
pub fn app_open(window: tauri::WebviewWindow, state: State<AppState>, id: String) -> Result<AppSnapshot, String> {
    crate::runtime::main_window(&window)?;
    state.run_idle(|s| {
        if s.pending.is_some() {
            s.cancel()?
        }
        *s = Session::load(&state.root, &id)?;
        Ok(())
    })
}
#[tauri::command]
pub fn app_developer_mode(window: tauri::WebviewWindow) -> Result<bool, String> {
    crate::runtime::main_window(&window)?;
    Ok(crate::portal::developer_mode())
}
#[cfg(test)]
mod tests {
    use super::*;
    fn root() -> PathBuf {
        std::env::temp_dir().join(format!(
            "bob-functional-native-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }
    #[test]
    fn provenance_comes_from_events_not_user_prefix_and_old_snapshot_bytes_survive() {
        let root = root();
        let state = AppState::at_root(root.clone());
        let mut round=0;
        let snap=state.run(|s| {
            s.turn("APP: Turn cancelled; human typed this", &mut |_| {
                round+=1;
                Ok(if round==1 {serde_json::json!({"ok":true,"content":"broken json"})}
                else {serde_json::json!({"ok":true,"content":r#"{"type":"action","action":{"tool":"list"}}"#})})
            }).ok(); // round budget ends after the fixture repeatedly asks to list
            s.cancel()
        }).unwrap();
        let value=serde_json::to_value(&snap).unwrap();
        let provenance=&value["message_provenance"];
        assert_eq!(provenance[0], "human", "prefix collision remains actual human");
        assert_eq!(provenance[1], "assistant_control");
        assert_eq!(provenance[2], "system");
        assert_eq!(provenance[3], "assistant_control");
        assert_eq!(provenance[4], "tool");
        assert_eq!(provenance.as_array().unwrap().last().unwrap(), "system");
        let id=snap.session.id.clone();
        let path=root.join(format!("{id}.json"));
        let before=std::fs::read(&path).unwrap();
        // Snapshot projection alone never modifies old history or stored checksums.
        let reopened=Session::saved(&root).unwrap();assert!(!reopened.iter().any(|s|s.error.is_some()));
        state.run(|_|Ok(())).unwrap();
        assert_eq!(before,std::fs::read(&path).unwrap());
        snap.session.validate().unwrap();
        drop(state);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn observer_never_projects_events_from_a_previous_session() {
        let root = root();
        let state = AppState::at_root(root.clone());
        let old=state.run(|s|s.turn("old",&mut |_|Ok(serde_json::json!({"ok":true,"content":r#"{"type":"message","content":"old answer"}"#})))).unwrap();
        state
            .run(|s| {
                *s = Session::create(&root)?;
                Ok(())
            })
            .unwrap();
        let token = state.begin().unwrap();
        state.run_model(&token,|s|s.turn("new",&mut |_|{
            let snap=state.workbench_snapshot().unwrap();
            assert!(!snap.agents[0].source_event_ids.iter().any(|id|old.session.events.iter().any(|e|&e.event_id==id)),"old session event leaked into current projection");
            Ok(serde_json::json!({"ok":true,"content":r#"{"type":"message","content":"new answer"}"#}))
        })).unwrap();
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn prepared_request_blocks_session_and_project_mutation_until_release() {
        let root = root();
        let state = AppState::at_root(root.clone());
        let before = state.run(|_| Ok(())).unwrap().session.id;
        let token = state.begin().unwrap();
        let mut ran = false;
        assert!(state
            .run_idle(|_| {
                ran = true;
                Ok(())
            })
            .is_err());
        assert!(!ran);
        assert_eq!(state.run(|_| Ok(())).unwrap().session.id, before);
        state.run_model(&token, |_| Ok(())).unwrap();
        state.run_idle(|s| s.rename("after release")).unwrap();
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn workbench_snapshot_and_cancel_do_not_wait_for_model_mutex() {
        let root = root();
        let state = std::sync::Arc::new(AppState::at_root(root.clone()));
        let token = state.begin().unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        let (release, wait) = std::sync::mpsc::channel();
        let worker = state.clone();
        let thread = std::thread::spawn(move || {
            worker.run_model(&token,|s|s.turn("actual observed turn",&mut |_|{tx.send(()).unwrap();wait.recv().unwrap();Ok(serde_json::json!({"ok":true,"content":r#"{"type":"message","content":"hello"}"#}))}))
        });
        rx.recv_timeout(std::time::Duration::from_secs(2)).unwrap();
        let start = std::time::Instant::now();
        let snap = state.workbench_snapshot().unwrap();
        assert!(snap.app.is_none());
        let observed=serde_json::to_value(&snap).unwrap();
        assert_eq!(observed["local_observation"]["session_id"],snap.workflows[0].session_id.as_ref().unwrap().as_str());
        assert!(observed["local_observation"]["request_id"].as_str().is_some());
        let events=observed["local_observation"]["events"].as_array().expect("in-flight durable events must reach chat without locking model");
        assert!(events.iter().any(|e|e["kind"]=="user.message"&&e["data"]["content"]=="actual observed turn"));
        assert!(events.iter().any(|e|e["kind"]=="model.requested"));
        assert_eq!(snap.agents.len(), 1);
        assert_eq!(
            snap.agents[0].state,
            bob_engine::workbench::Lifecycle::Running
        );
        assert!(snap.workflows[0]
            .steps
            .iter()
            .any(|s| s.label == "Model request"));
        state.request_cancel().unwrap();
        assert_eq!(
            state.workbench_snapshot().unwrap().agents[0].state,
            bob_engine::workbench::Lifecycle::CancelRequested
        );
        assert!(start.elapsed() < std::time::Duration::from_secs(1));
        assert!(state.select_profile("standalone_http").is_err());
        release.send(()).unwrap();
        assert!(thread.join().unwrap().is_err());
        assert_eq!(
            state.workbench_snapshot().unwrap().agents[0].state,
            bob_engine::workbench::Lifecycle::Cancelled
        );
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn workbench_settings_persist_and_cannot_move_pending_approval() {
        let root = root();
        let state = AppState::at_root(root.clone());
        state
            .configure_standalone(
                "http://127.0.0.1:1/v1/chat/completions",
                "m",
                Some("BOB_STANDALONE_MISSING_TEST_KEY".into()),
            )
            .unwrap();
        state.select_profile("standalone_http").unwrap();
        assert!(state
            .model(&[Message {
                role: "user".into(),
                content: "hello".into()
            }])
            .unwrap_err()
            .contains("reference unavailable"));
        let project = root.join("project");
        std::fs::create_dir(&project).unwrap();
        state.run(|s|{s.attach(&project)?;s.turn("propose",&mut |_|Ok(serde_json::json!({"ok":true,"content":r#"{"type":"action","action":{"tool":"write","path":"test.txt","content":"unapproved"}}"#}))) }).unwrap();
        let before = state.run(|_| Ok(())).unwrap().session.pending.unwrap();
        assert!(state.select_profile("local_bob_model").is_err());
        assert!(state
            .configure_standalone("http://127.0.0.1:2/v1/chat/completions", "n", None)
            .is_err());
        state
            .rename_session(
                &state.run(|_| Ok(())).unwrap().session.id,
                "Named without rebinding",
            )
            .unwrap();
        assert_eq!(
            state
                .run(|_| Ok(()))
                .unwrap()
                .session
                .pending
                .unwrap()
                .digest,
            before.digest
        );
        drop(state);
        let reopened = AppState::at_root(root.clone());
        assert_eq!(
            reopened.workbench_snapshot().unwrap().active_profile_id,
            "standalone_http"
        );
        assert!(!project.join("test.txt").exists());
        drop(reopened);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn cancellation_between_prepare_and_dispatch_cannot_run_operation() {
        let root = root();
        let state = AppState::at_root(root.clone());
        let token = state.begin().unwrap();
        state.request_cancel().unwrap();
        let mut ran = false;
        let error = state
            .run_model(&token, |_| {
                ran = true;
                Ok(())
            })
            .unwrap_err();
        assert!(error.to_lowercase().contains("cancel"));
        assert!(!ran);
        drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn session_store_is_single_process_locked_and_reopens_after_close() {
        let root = root();
        let a = AppState::at_root(root.clone());
        assert!(a.run(|_| Ok(())).is_ok());
        let b = AppState::at_root(root.clone());
        assert!(b.run(|_| Ok(())).unwrap_err().contains("already open"));
        drop(b);
        drop(a);
        let c = AppState::at_root(root.clone());
        assert!(c.run(|_| Ok(())).is_ok());
        drop(c);
        std::fs::remove_dir_all(root).unwrap();
    }
}
