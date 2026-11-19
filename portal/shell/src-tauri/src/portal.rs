//! Native portal transport for the Bob broker over SSH or the local runtime.
//!
//! CONTRACT §2: the shell spawns `ssh -o BatchMode=yes -o
//! StrictHostKeyChecking=yes -o ConnectTimeout=8 bob@<target> python3 -u
//! /opt/bob/portal-broker.py` as an argv array (never a shell
//! string), speaks one UTF-8 JSON object per line, and forwards asynchronous
//! `{"event":...}` frames to the webview as `portal://event`.
//!
//! The transport is bounded: <=8 requests in flight, <=256 KiB request line,
//! <=1 MiB reply line, 30 s reply deadline (145 s for writes and for the reads
//! the broker serves with serial fanout -- catalog, projects.list, messages.page
//! -- which the contract lets block on gateway work). A protocol/EOF fault marks
//! the link dead, fails pending callers with a typed error, and refuses later
//! sends; nothing is auto-retried and the link is never auto-respawned mid-send.
//! A single request's deadline is abandoned alone: it never kills the shared
//! link, because an unrelated `chat.send` may be streaming on it. `chat.send`
//! replies with its accepted request ID immediately; streamed approval waits do
//! not hold a 145 s reply channel. The broker pauses its active work budget.
#![allow(dead_code)]

use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
#[cfg(windows)]
use std::os::windows::io::OwnedHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::process::Child;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter};

pub const EVENT_TOPIC: &str = "portal://event";
const MAX_REQUEST: usize = 256 * 1024;
// A 5 MiB Builder preview expands to ~7 MiB in base64 plus the JSON frame.
const MAX_REPLY: usize = 8 * 1024 * 1024;
const MAX_IN_FLIGHT: usize = 8;
const DEADLINE: Duration = Duration::from_secs(30);
const LONG_DEADLINE: Duration = Duration::from_secs(145);
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub const PROFILE_IDS: &[&str] = &["bob_server", "bob_team", "local_bob_model", "standalone_http"];
const SETTINGS_SCHEMA: &str = "portal-settings-v1";

/// Commands implemented by this module. Deliberately kept OUT of the WSL
/// `COMMANDS` proxy list in `windows.rs`: the WSL host must never see a
/// portal command, and the existing inventory test stays unchanged.
pub const PORTAL_COMMANDS: &[&str] = &[
    "portal_workflow_catalog_v1",
    "portal_workflow_list_v1",
    "portal_workflow_get_v1",
    "portal_workflow_save_v1",
    "portal_workflow_copy_v1",
    "portal_workflow_delete_v1",
    "portal_workflow_propose_v1",
    "portal_workflow_proposal_v1",
    "portal_workflow_proposals_v1",
    "portal_workflow_propose_again_v1",
    "portal_workflow_start_v1",
    "portal_workflow_runs_v1",
    "portal_workflow_run_v1",
    "portal_workflow_events_v1",
    "portal_workflow_pause_v1",
    "portal_workflow_resume_v1",
    "portal_workflow_stop_v1",
    "portal_workflow_message_v1",
    "portal_workflow_gate_v1",
    "portal_workflow_artifact_v1",
    "portal_workflow_watch_v1",

    "portal_builder_network_set_v2",
    "portal_builder_network_log_v2",
    "portal_workspace_browse_v2",
    "portal_builder_mode_get_v2",
    "portal_builder_task_end_v2",
    "portal_builder_project_result_v2",
    "portal_builder_mode_set_v2",
    "portal_builder_approval_get_v2",
    "portal_builder_approve_v2",
    "portal_builder_preview_v2",
    "portal_builder_open_temp_copy_v2",
    "portal_recovery_capability_v2",
    "portal_recovery_status_v2",
    "portal_recovery_start_v2",
    "portal_recovery_act_v2",
    "portal_password_pending_v1",
    "portal_auth_change_password_v2",
    "portal_secret_set_v2",
    "portal_settings_status_v1",
    "portal_policy_get_v2",
    "portal_policy_set_v2",
    "portal_owner_begin_v2",
    "portal_owner_confirm_v2",
    "portal_owner_status_v2",
    "portal_owner_lock_v2",
    "portal_owner_logout_v2",
    "portal_flags_list_v2",
    "portal_flags_get_v2",
    "portal_flags_resolve_v2",
    "portal_grants_prepare_v2",
    "portal_grants_issue_v2",
    "portal_work_catalog_v2",
    "portal_work_preparation_v2",
    "portal_work_status_v2",
    "portal_work_workspace_create_v2",
    "portal_work_publish_v2",
    "portal_work_start_v2",

    "portal_grants_list_v2",
    "portal_grants_get_v2",
    "portal_grants_revoke_v2",
    "portal_budgets_get_v2",
    "portal_budgets_list_v2",
    "portal_budgets_remove_v2",

    "portal_teams_list_v2",
    "portal_teams_save_v2",
    "portal_runs_start_v2",
    "portal_runs_list_v2",
    "portal_run_get_v2",
    "portal_run_cancel_v2",
    "portal_run_resume_v2",
    "portal_budget_set_v2",
    "portal_hello",
    "portal_catalog",
    "portal_conversations",
    "portal_conversation_new",
    "portal_conversation_open",
    "portal_conversation_rename",
    "portal_conversation_assign_project",
    "portal_messages_more",
    "portal_send",
    "portal_stop",
    "portal_answer",
    "portal_pins",
    "portal_approvals",
    "portal_approval_get",
    "portal_approval_decide",
    "portal_projects",
    "portal_project_save",
    "portal_memory_status",
    "portal_memory_list",
    "portal_files_roots",
    "portal_files_list",
    "portal_files_read",
    "portal_memory_search",
    "portal_memory_read",
    "portal_memory_save",
    "portal_secret_set",
    "portal_auth_change_password",
    "portal_runs_start",
    "portal_runs_list",
    "portal_run_get",
    "portal_run_cancel",
    "portal_run_followup",
    "portal_jobs_attachments",
    "portal_jobs_attachments_v2",
    "portal_jobs_list",
    "portal_jobs_list_v2",
    "portal_jobs_start",
    "portal_jobs_start_v2",
    "portal_jobs_audit",
    "portal_jobs_audit_v2",
    "portal_jobs_cancel",
    "portal_jobs_cancel_v2",
    "portal_jobs_proposal",
    "portal_jobs_proposal_v2",
    "portal_jobs_decide",
    "portal_jobs_decide_v2",
    "portal_jobs_artifact",
    "portal_jobs_artifact_v2",
    "portal_connection_get",
    "portal_connection_set",
    "portal_profiles",
    "portal_select_profile",
    "portal_reconnect",
];

// ---------------------------------------------------------------- typed errors

/// One-line, actionable, secret-free hint for every code the portal can raise.
pub fn hint_for(code: &str) -> &'static str {
    if code == "rate_limited" || code == "gateway_http_429" {
        return "This request was not applied. Try again in a few seconds.";
    }
    if code.starts_with("gateway_http_") {
        return "Bob's gateway answered an HTTP error AFTER the request was sent, so the outcome \
                is unknown and the write may have been applied. Server-side condition, not an app \
                fault — nothing was retried. Inspect this job's decision/read-back before sending again.";
    }
    match code {
        "transport_unavailable" => {
            "The PC could not keep a portal link open, or the broker could not reach the \
             gateway at all. Check the selected connection in Settings and the transport \
             details below, then press Reconnect. A gateway HTTP error is not this case."
        }
        "not_dispatched" => {
            "Nothing was written to the portal link, so nothing was sent and the draft is \
             safe to retry. If the link itself is closed, press Reconnect first."
        }
        "auth_expired" => {
            "The server rejected the portal token. Ask the person who runs the server to re-issue the portal token file \
             (mode 0600) on the server and reconnect. The token never lives in this app."
        }
        "protocol_invalid" => {
            "The broker sent a line this app cannot read. The link is closed; nothing was retried."
        }
        "timeout_no_reply" => {
            "No reply arrived before the deadline. The outcome is unknown: inspect the \
             conversation history before sending anything again."
        }
        "approval_foreign" => "That approval belongs to another owner; this portal will not touch it.",
        "approval_stale" => "That approval is no longer pending (already decided or expired). Re-read the queue.",
        "approval_not_found" => "The approval id is not on the server for this identity.",
        "approval_not_permitted" => {
            "The server's staged approval guard has not been activated, so decisions are refused. \
             A recorded decision is never proof of effect."
        }
        "bad_request" => "The app refused to send a malformed or oversized request; nothing was sent.",
        _ => "No server refusal was recorded, so the outcome is unconfirmed — a write may have been \
              applied. The detail is verbatim from the server; check the read-back before retrying.",
    }
}

pub fn error_json(code: &str, detail: impl Into<String>) -> String {
    json!({"code": code, "detail": detail.into(), "hint": hint_for(code)}).to_string()
}

fn bad(detail: &str) -> String {
    error_json("bad_request", detail)
}

fn with_transport_context(error: String, context: &str) -> String {
    if context.is_empty() { return error; }
    let Ok(mut value) = serde_json::from_str::<Value>(&error) else { return error; };
    if !matches!(value["code"].as_str(), Some("transport_unavailable" | "not_dispatched" | "protocol_invalid" | "timeout_no_reply")) {
        return error;
    }
    let detail = value["detail"].as_str().unwrap_or("");
    if !detail.starts_with(context) {
        value["detail"] = json!(format!("{context} {detail}"));
    }
    value.to_string()
}

// ------------------------------------------------------------- frame handling

/// Why a reader loop stopped. The distinction is the whole point of the
/// event-frame fix: an event line must never be a fault.
#[derive(Debug, PartialEq, Eq)]
pub enum End {
    Eof,
    Truncated,
    Oversize,
    NotUtf8,
    Invalid(&'static str),
    Io(String),
}

impl End {
    fn code(&self) -> &'static str {
        match self {
            End::Invalid(_) => "protocol_invalid",
            _ => "transport_unavailable",
        }
    }
    fn detail(&self) -> String {
        match self {
            End::Eof => "broker closed the portal link (transport exited); outcome of anything in flight is unknown".into(),
            End::Truncated => "broker closed the link mid-line".into(),
            End::Oversize => "broker line exceeded 1 MiB; refused".into(),
            End::NotUtf8 => "broker line was not UTF-8".into(),
            End::Invalid(why) => format!("broker frame invalid: {why}"),
            End::Io(e) => format!("portal read failed: {e}"),
        }
    }
}

#[derive(Debug)]
pub enum Frame {
    Reply(u64, Value),
    Event(String),
    Invalid(&'static str),
}

/// Classify one protocol line. A reply carries `id` + boolean `ok`; an event
/// carries `event` and NO id. Anything else is a protocol fault.
pub fn classify(line: &str) -> Frame {
    match serde_json::from_str::<Value>(line) {
        Ok(value) => {
            if let Some(id) = value.get("id").and_then(Value::as_u64) {
                match value.get("ok").and_then(Value::as_bool) {
                    Some(_) => Frame::Reply(id, value),
                    None => Frame::Invalid("reply frame without boolean ok"),
                }
            } else if value.get("event").and_then(Value::as_str).is_some() {
                Frame::Event(line.to_string())
            } else {
                Frame::Invalid("frame without id or event")
            }
        }
        Err(_) => Frame::Invalid("line is not a JSON object"),
    }
}

/// Read protocol lines until the link ends. Events are handed to `on_event`
/// and never terminate the loop; replies go to `on_reply`.
pub fn read_loop<R: BufRead>(
    mut reader: R,
    mut on_reply: impl FnMut(u64, Value),
    mut on_event: impl FnMut(String),
) -> End {
    loop {
        let mut bytes = Vec::new();
        let read = {
            let mut limited = (&mut reader).take((MAX_REPLY + 1) as u64);
            limited.read_until(b'\n', &mut bytes)
        };
        match read {
            Ok(0) => return End::Eof,
            Ok(_) if bytes.last() != Some(&b'\n') => return End::Truncated,
            Ok(_) => {}
            Err(e) => return End::Io(e.to_string()),
        }
        if bytes.len() > MAX_REPLY {
            return End::Oversize;
        }
        let text = match std::str::from_utf8(&bytes) {
            Ok(text) => text.trim_end_matches(['\n', '\r']),
            Err(_) => return End::NotUtf8,
        };
        if text.trim().is_empty() {
            return End::Invalid("blank line");
        }
        match classify(text) {
            Frame::Reply(id, value) => on_reply(id, value),
            Frame::Event(line) => on_event(line),
            Frame::Invalid(why) => return End::Invalid(why),
        }
    }
}

// ----------------------------------------------------------------------- link

type Reply = Result<Value, String>;
type Sink = Arc<dyn Fn(String) + Send + Sync>;

#[derive(Default)]
struct Pending {
    dead: bool,
    replies: HashMap<u64, mpsc::Sender<Reply>>,
}

#[cfg(windows)]
use std::time::Instant;

pub struct Link {
    queue: mpsc::SyncSender<Option<Vec<u8>>>,
    #[cfg(windows)]
    child: Mutex<Option<Child>>,
    #[cfg(unix)]
    child: Mutex<super::portal_unix::OwnedChild>,
    #[cfg(windows)]
    wsl_started: Option<Instant>,
    closing: AtomicBool,
    pending: Mutex<Pending>,
    seq: AtomicU64,
    target: String,
    /// Actionable transport description; supervision and JSONL remain generic.
    context: String,
    /// The webview's event stream, kept so a dead link can say so once.
    sink: Sink,
    /// Only a link the app has accepted may report a fault: a target attempt
    /// that never answered is a fallback, not a dead conversation.
    armed: AtomicBool,
    deadline: Duration,
    long_deadline: Duration,
    #[cfg(windows)]
    _job: Option<OwnedHandle>,
}

impl Drop for Link {
    fn drop(&mut self) { self.shutdown(); }
}

fn reply_result(value: Value) -> Reply {
    if value.get("ok").and_then(Value::as_bool) == Some(true) {
        Ok(value.get("value").cloned().unwrap_or(Value::Null))
    } else {
        let detail = value
            .get("error")
            .and_then(|e| e.get("detail"))
            .and_then(Value::as_str)
            .unwrap_or("broker refused the request without a detail")
            .to_string();
        let code = value
            .get("error")
            .and_then(|e| e.get("code"))
            .and_then(Value::as_str)
            .unwrap_or("protocol_invalid")
            .to_string();
        let mut error: Value = serde_json::from_str(&error_json(&code, detail)).unwrap();
        if let Some(seconds) = value["error"]["retry_after"].as_u64().filter(|n| *n <= 86400) { error["retry_after"] = json!(seconds); }
        if code == "rate_limited" || code == "gateway_http_429" {
            error["applied"] = json!(false);
        } else if let Some(applied) = value["error"]["applied"].as_bool() {
            error["applied"] = json!(applied);
        }
        if let Some(seconds) = value["error"]["retry_after_seconds"].as_u64().filter(|n| *n <= 86400) {
            error["retry_after_seconds"] = json!(seconds);
            error["retry_after"] = json!(seconds);
        }
        Err(error.to_string())
    }
}

/// The single frame that tells the webview the link died. `reason` is the same
/// typed error the pending callers get, so the code cannot drift from theirs.
fn transport_fault_frame(reason: &str) -> String {
    let value: Value = serde_json::from_str(reason).unwrap_or(Value::Null);
    let code = value
        .get("code")
        .and_then(Value::as_str)
        .unwrap_or("transport_unavailable");
    let detail = value
        .get("detail")
        .and_then(Value::as_str)
        .unwrap_or("the portal link closed; outcome of anything in flight is unknown");
    json!({"event": "transport_fault", "data": {"code": code, "detail": detail}}).to_string()
}

#[cfg(unix)]
fn spawn_portal_child(command: Command) -> std::io::Result<super::portal_unix::OwnedChild> {
    super::portal_unix::OwnedChild::spawn(command)
}

#[cfg(windows)]
fn spawn_portal_child(mut command: Command) -> std::io::Result<Child> {
    command.spawn()
}

impl Link {
    /// Spawn a program as an argv array (no shell) and wire the reader/writer.
    fn spawn_argv(
        program: &str,
        args: &[String],
        label: &str,
        context: &str,
        data_root: &Path,
        sink: Sink,
        deadline: Duration,
        long_deadline: Duration,
    ) -> Result<Arc<Link>, String> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            std::fs::DirBuilder::new().recursive(true).mode(0o700).create(data_root)
                .map_err(|e| error_json("transport_unavailable", format!("cannot create portal directory: {e}")))?;
            if !harden(data_root) { return Err(bad("cannot secure portal directory")); }
        }
        let _ = std::fs::create_dir_all(data_root.join("logs"));
        let stderr = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(data_root.join("logs").join("portal-stderr.log"))
            .map(Stdio::from)
            .unwrap_or_else(|_| Stdio::null());
        let mut command = Command::new(program);
        command.args(args);
        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(stderr);
        #[cfg(windows)]
        let started = Instant::now();
        let mut child = spawn_portal_child(command)
            .map_err(|e| {
                with_transport_context(error_json(
                    "transport_unavailable",
                    format!("cannot start {program} for portal target {label}: {e}"),
                ), context)
            })?;
        #[cfg(windows)]
        let wsl_started = crate::windows::is_wsl_program(std::ffi::OsStr::new(program)).then_some(started);
        #[cfg(windows)]
        let job = if wsl_started.is_some() { None } else { match crate::windows::owned_job(&child) {
            Ok(job) => Some(job),
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error_json(
                    "transport_unavailable",
                    format!("cannot supervise portal link {label}: {e}"),
                ));
            }
        }};
        #[cfg(unix)]
        let pipes = (child.take_stdin(), child.take_stdout());
        #[cfg(windows)]
        let pipes = (child.stdin.take(), child.stdout.take());
        let (mut pipe, output) = match pipes {
            (Some(input), Some(output)) => (input, output),
            _ => {
                #[cfg(unix)]
                child.cleanup();
                #[cfg(windows)]
                { crate::windows::end_session(&mut Some(child), wsl_started, Duration::ZERO); }
                return Err(error_json("transport_unavailable", "portal stdio missing"));
            }
        };
        let (queue, inbox) = mpsc::sync_channel::<Option<Vec<u8>>>(MAX_IN_FLIGHT);
        let reader_sink = sink.clone();
        let link = Arc::new(Link {
            queue,
            #[cfg(windows)]
            child: Mutex::new(Some(child)),
            #[cfg(unix)]
            child: Mutex::new(child),
            closing: AtomicBool::new(false),
            pending: Mutex::new(Pending::default()),
            seq: AtomicU64::new(1),
            target: label.to_string(),
            context: context.to_string(),
            sink,
            armed: AtomicBool::new(false),
            deadline,
            long_deadline,
            #[cfg(windows)]
            _job: job,
            #[cfg(windows)]
            wsl_started,
        });
        // Writer: bounded queue, blocking pipe writes kept off the IPC path.
        let weak_writer = Arc::downgrade(&link);
        std::thread::spawn(move || {
            while let Ok(Some(mut bytes)) = inbox.recv() {
                if weak_writer.upgrade().is_none_or(|link|link.pending.lock().unwrap_or_else(|e|e.into_inner()).dead){break}
                bytes.push(b'\n');
                let sent=pipe.write_all(&bytes).and_then(|_| pipe.flush());
                for byte in &mut bytes {unsafe{std::ptr::write_volatile(byte,0);}}
                if sent.is_err() {
                    if let Some(link) = weak_writer.upgrade() {
                        link.fail(
                            error_json(
                                "transport_unavailable",
                                "portal write failed; link closed, outcome of in-flight requests unknown",
                            ),
                            link.is_armed(),
                        );
                    }
                    break;
                }
            }
        });
        // Reader: replies resolve callers, events go to the webview, everything
        // else is a typed fault that closes the link.
        let weak_reader = Arc::downgrade(&link);
        std::thread::spawn(move || {
            let end = read_loop(
                BufReader::new(output),
                |id, value| {
                    if let Some(link) = weak_reader.upgrade() {
                        link.deliver(id, value);
                    }
                },
                |line| reader_sink(line),
            );
            if let Some(link) = weak_reader.upgrade() {
                link.fail(error_json(end.code(), end.detail()), link.is_armed());
            }
        });
        Ok(link)
    }

    fn deliver(&self, id: u64, value: Value) {
        let sender = {
            let mut pending = self.pending.lock().unwrap_or_else(|e| e.into_inner());
            pending.replies.remove(&id)
        };
        if let Some(sender) = sender {
            let _ = sender.send(reply_result(value));
        }
    }

    /// Arm fault reporting. Called only once the link has answered its ping,
    /// so a target that never came up stays silent for the window.
    fn arm(&self) {
        self.armed.store(true, Ordering::SeqCst);
    }

    fn is_armed(&self) -> bool {
        self.armed.load(Ordering::SeqCst)
    }

    fn is_dead(&self) -> bool {
        self.pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .dead
    }

    /// Mark the link dead exactly once, fail every pending caller with the
    /// same typed reason, and kill the child. Never respawns.
    fn fail(&self, reason: String, notify: bool) {
        let reason = with_transport_context(reason, &self.context);
        let drained = {
            let mut pending = self.pending.lock().unwrap_or_else(|e| e.into_inner());
            if pending.dead {
                return;
            }
            pending.dead = true;
            std::mem::take(&mut pending.replies)
        };
        // One frame, once: a streaming turn must never stay "live" in the
        // webview after the link that was feeding it is gone (R7-1).
        // An intentional close (Reconnect / Refresh) is not a fault: announcing
        // it would raise a "link closed" banner over a link that is about to be
        // live again, which is worse than saying nothing.
        if notify {
            (self.sink)(transport_fault_frame(&reason));
        }
        for (_, sender) in drained {
            let _ = sender.send(Err(reason.clone()));
        }
        // Stop the writer so stdin closes. Old sessions still terminate on faults;
        // app close allows a short EOF grace within the global cleanup budget.
        let _ = self.queue.try_send(None);
        let mut child = self.child.lock().unwrap_or_else(|e| e.into_inner());
        #[cfg(unix)]
        child.cleanup();
        #[cfg(windows)]
        { crate::windows::end_session(&mut child, self.wsl_started, if self.closing.load(Ordering::SeqCst) { crate::windows::SESSION_CLOSE_GRACE } else { Duration::ZERO }); }
    }

    fn shutdown(&self) {
        self.fail(
            error_json(
                "transport_unavailable",
                "portal link closed by the app; anything in flight has an unknown outcome",
            ),
            false,
        );
        let _ = self.queue.try_send(None);
    }

    fn call(&self, op: &str, args: Value, long: bool) -> Reply {
        self.call_inner(op, args, long).map_err(|error| with_transport_context(error, &self.context))
    }

    fn call_inner(&self, op: &str, args: Value, long: bool) -> Reply {
        let id = self.seq.fetch_add(1, Ordering::SeqCst);
        let mut frame=json!({"id":id,"op":op,"args":args});
        let encoded=serde_json::to_vec(&frame);
        crate::recovery::scrub(&mut frame);
        let mut bytes = encoded
            .map_err(|e| error_json("bad_request", format!("cannot encode `{op}`: {e}")))?;
        if bytes.len() > MAX_REQUEST {
            return Err(bad(&format!(
                "`{op}` request would be {} bytes; the 256 KiB cap refuses it before dispatch",
                bytes.len()
            )));
        }
        bytes.push(b'\n');
        let (sender, receiver) = mpsc::channel();
        {
            let mut pending = self
                .pending
                .lock()
                .map_err(|_| error_json("transport_unavailable", "portal link state poisoned"))?;
            if pending.dead {
                return Err(error_json(
                    "not_dispatched",
                    format!("portal link to {} is closed; nothing was sent", self.target),
                ));
            }
            if pending.replies.len() >= MAX_IN_FLIGHT {
                return Err(error_json(
                    "not_dispatched",
                    "portal link busy: 8 requests already in flight; nothing was sent",
                ));
            }
            pending.replies.insert(id, sender);
            if self.queue.try_send(Some(bytes)).is_err() {
                pending.replies.remove(&id);
                return Err(error_json(
                    "not_dispatched",
                    "portal writer queue is full; nothing was sent",
                ));
            }
        }
        // An approved Builder command can spend up to 600 seconds in the
        // isolated worker. Keep its receipt channel alive through that bound.
        let deadline = if op == "builder.approve" { Duration::from_secs(660) } else if long { self.long_deadline } else { self.deadline };
        match receiver.recv_timeout(deadline) {
            Ok(reply) => reply,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                // Abandon THIS request and keep the link. A shared link may be
                // streaming an unrelated chat.send; killing it here would cut
                // that turn mid-answer and turn a slow read into a lost reply.
                // A reply that arrives later finds no sender and is dropped.
                {
                    let mut pending = self.pending.lock().unwrap_or_else(|e| e.into_inner());
                    pending.replies.remove(&id);
                }
                let detail = if is_write_op(op) {
                    format!(
                        "no reply to `{op}` within {}s; the request may still have landed — outcome unknown, inspect history",
                        deadline.as_secs()
                    )
                } else {
                    format!(
                        "no reply to `{op}` within {}s; the link is still open and this read changed nothing — retry it",
                        deadline.as_secs()
                    )
                };
                Err(error_json("timeout_no_reply", detail))
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let detail = format!("`{op}` lost its reply channel: the portal link closed");
                self.fail(error_json("transport_unavailable", detail.clone()), self.is_armed());
                Err(error_json("transport_unavailable", detail))
            }
        }
    }
}

/// Ops that can change server state. Their deadline wording must not claim that
/// nothing happened; a read-only timeout says the link is still usable.
fn is_write_op(op: &str) -> bool {
    matches!(
        op,
        "workflows.save" | "workflows.copy" | "workflows.delete" | "workflows.propose" | "workflows.propose_again" | "workflows.grant_start" | "workflows.pause" | "workflows.resume" | "workflows.stop" | "workflows.message" | "workflows.gate"
            | "chat.send"
            | "builder.task.end"
            | "chat.stop"
            | "chat.answer"
            | "conversations.create"
            | "conversations.rename"
            | "conversations.assign_project"
            | "approvals.decide"
            | "projects.create"
            | "projects.update"
            | "projects.delete"
            | "memory.save"
            | "flags.resolve"
            | "runs.start"
            | "teams.save"
            | "runs.cancel"
            | "runs.resume"
            | "owner.begin" | "owner.confirm" | "owner.lock" | "owner.logout"
            | "grants.prepare" | "grants.issue" | "grants.revoke" | "budgets.remove"
            | "budgets.set"
            | "auth.change_password" | "secrets.set" | "policy.set"
            | "auth.factors.prepare" | "auth.factors.commit" | "auth.factors.cancel"
            | "runs.followup"
            | "jobs.start"
            | "jobs.cancel"
            | "jobs.decide"
            | "work.workspace.create" | "work.publish" | "work.start"
            | "builder.mode.set" | "builder.approve" | "builder.network.set"
    )
}

// ------------------------------------------------------------------ transport

pub fn data_root() -> PathBuf {
    if let Ok(root) = std::env::var("BOB_PORTAL_DATA_ROOT") {
        if !root.trim().is_empty() {
            return PathBuf::from(root.trim());
        }
    }
    #[cfg(unix)]
    return unix_data_root(std::env::var_os("XDG_DATA_HOME"), std::env::var_os("HOME"));
    #[cfg(windows)]
    let base = std::env::var("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."));
    #[cfg(windows)]
    { base.join("bob-portal") }
}

#[cfg(unix)]
fn unix_data_root(xdg: Option<std::ffi::OsString>, home: Option<std::ffi::OsString>) -> PathBuf {
    if let Some(base) = xdg.map(PathBuf::from).filter(|p| p.is_absolute()) {
        return base.join("bob-portal");
    }
    home.map(PathBuf::from).filter(|p| p.is_absolute())
        .expect("portal requires absolute HOME or XDG_DATA_HOME or BOB_PORTAL_DATA_ROOT")
        .join(".local/share/bob-portal")
}

/// Resolve from explicit inputs so precedence and the legacy argv can be tested
/// without mutating process-wide environment variables.
#[derive(Debug)]
struct Connection {
    user: String,
    host: String,
    broker: String,
    mode: String,
    lane: String,
    sources: [&'static str; 5],
    configured: bool,
}

impl Connection {
    fn require_server(&self) -> Result<(), String> {
        let missing: Vec<_> = [("SSH user", &self.user), ("server host", &self.host), ("broker path", &self.broker)]
            .into_iter().filter_map(|(name, value)| value.is_empty().then_some(name)).collect();
        if missing.is_empty() { Ok(()) } else {
            Err(bad(&format!("Server connection is missing {}. Open Connection settings and enter the values from the person who runs the server.", missing.join(", "))))
        }
    }

    fn ssh_context(&self, target: &str) -> String {
        format!("SSH server transport: check `ssh {}@{target}` is reachable and broker {} exists.", self.user, self.broker)
    }

    fn targets(&self) -> Vec<String> {
        if self.mode == "this_pc" { return vec![self.lane.clone()]; }
        if self.host.is_empty() {
            vec![]
        } else {
            vec![self.host.clone()]
        }
    }

    fn payload(&self) -> Value {
        json!({
            "connection_configured": self.configured,
            "user": {"value": self.user, "source": self.sources[0]},
            "host": {"value": self.host, "source": self.sources[1]},
            "broker": {"value": self.broker, "source": self.sources[2]},
            "mode": {"value": self.mode, "source": self.sources[3]},
            "lane": {"value": self.lane, "source": self.sources[4]},
        })
    }
}

fn valid_user(user: &str) -> bool {
    !user.is_empty() && user.len() <= 32 && !user.starts_with('-')
        && user.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

fn validate_connection_field(field: &str, value: &str) -> Result<(), String> {
    let (valid, rule) = match field {
        "user" => (valid_user(value), "use 1–32 ASCII letters, digits, '.', '_' or '-', without a leading '-'") ,
        "host" => (valid_target(value), "use a bare host name or address without shell syntax"),
        "broker" => (valid_broker_path(value), "use an absolute path without shell syntax"),
        "mode" => (matches!(value, "server" | "this_pc"), "use 'server' or 'this_pc'"),
        "lane" => (valid_lane(value), "use [a-z][a-z0-9-]{0,31}"),
        _ => unreachable!(),
    };
    if valid { Ok(()) } else { Err(bad(&format!("invalid connection {field}: {rule}"))) }
}

fn resolve_connection(document: &Value, env: [Option<String>; 5]) -> Result<Connection, String> {
    let saved = document.get("connection");
    if saved.is_some_and(|value| !value.is_object()) {
        return Err(bad("saved connection must be an object"));
    }
    let mut values = Vec::new();
    let mut sources = ["default"; 5];
    for (index, (field, default)) in [
        ("user", ""), ("host", ""),
        ("broker", ""),
        ("mode", "server"), ("lane", ""),
    ].iter().enumerate() {
        // Refuse invalid saved values even when an environment override masks
        // them: a corrupt setting must never become a silent fallback later.
        let saved_value = saved.and_then(|value| value.get(*field));
        let saved_text = match saved_value {
            Some(value) => {
                let text = value.as_str().ok_or_else(|| bad(&format!("saved connection {field} must be a string")))?;
                validate_connection_field(field, text)?;
                Some(text)
            }
            None => None,
        };
        let value = if let Some(value) = &env[index] {
            sources[index] = "env";
            value.as_str()
        } else if let Some(value) = saved_text {
            sources[index] = "settings";
            value
        } else { default };
        // No lane is assumed: selecting This PC requires an explicit lane.
        if !value.is_empty() || sources[index] != "default" {
            validate_connection_field(field, value)?;
        }
        values.push(value.to_string());
    }
    if values[3] == "this_pc" { validate_connection_field("lane", &values[4])?; }
    Ok(Connection {
        user: values[0].clone(), host: values[1].clone(), broker: values[2].clone(),
        mode: values[3].clone(), lane: values[4].clone(), sources,
        configured: saved.is_some() || env.iter().any(Option::is_some),
    })
}

fn connection_env() -> Result<[Option<String>; 5], String> {
    let mut values: [Option<String>; 5] = Default::default();
    for (index, key) in ["BOB_PORTAL_USER", "BOB_PORTAL_TARGET", "BOB_PORTAL_BROKER", "BOB_PORTAL_MODE", "BOB_PORTAL_LANE"].iter().enumerate() {
        // Preserve the legacy SSH override normalization. New fields use the
        // exact validation rules, including refusing empty overrides.
        values[index] = match std::env::var(key) {
            Ok(value) if index < 3 && value.trim().is_empty() => None,
            Ok(value) if index < 3 => Some(value.trim().to_string()),
            Ok(value) => Some(value),
            Err(std::env::VarError::NotPresent) => None,
            Err(_) => return Err(bad(&format!("{key} must be UTF-8"))),
        };
    }
    Ok(values)
}

pub(crate) fn valid_lane(value: &str) -> bool {
    !value.is_empty() && value.len() <= 32 && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-')
}

fn valid_runtime_name(value: &str) -> bool {
    !value.is_empty() && value.len() <= 64
        && value.bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'_' | b'-'))
}

#[derive(Debug)]
struct RuntimeCommand {
    program: String,
    args: Vec<String>,
    context: String,
}

/// Both platform argv forms are testable on either build host. No shell parsing.
fn runtime_command(
    windows: bool, system_root: &str, lane: &str, distro: Option<&str>,
    engine: Option<&str>, container: Option<&str>, podman_on_path: bool,
) -> Result<RuntimeCommand, String> {
    validate_connection_field("lane", lane)?;
    let distro = distro.unwrap_or("BoB");
    let container = container.unwrap_or("bob-runtime");
    for (field, value) in [("distro", distro), ("container", container)] {
        if !valid_runtime_name(value) {
            return Err(bad(&format!("invalid runtime {field}: use [A-Za-z0-9._-]{{1,64}}")));
        }
    }
    let engine = engine.unwrap_or(if podman_on_path { "podman" } else { "docker" });
    if !matches!(engine, "podman" | "docker") { return Err(bad("invalid runtime engine: use podman or docker")); }
    if windows {
        Ok(RuntimeCommand {
            program: format!("{}\\System32\\wsl.exe", system_root.trim_end_matches(['\\', '/'])),
            args: ["-d", distro, "-u", "bobapp", "--exec", "/usr/local/bin/bob-broker", lane].map(String::from).to_vec(),
            context: format!("This PC WSL transport (distro {distro}, lane {lane}): check that the WSL distro is installed and the lane exists."),
        })
    } else {
        Ok(RuntimeCommand {
            program: engine.into(),
            // The broker runs only as bobapp: portal tokens act as the human (they can
            // decide approvals), so Bob's own agents, which run as bob, must not read them.
            args: ["exec", "-i", "-u", "bobapp", container, "bob-broker", lane].map(String::from).to_vec(),
            context: format!("This PC {engine} transport (container {container}, lane {lane}): check that the runtime container is running and the lane exists."),
        })
    }
}

/// Open bobapp's first WSL session of this boot with a command that is allowed to wait out
/// WSL's session setup (`runtime::SESSION_FLOOR`) before the broker child exists. A broker
/// killed by its ping deadline while WSL is still creating its session wedges the distro.
#[cfg(all(windows, not(test)))]
fn open_runtime_session(command: &RuntimeCommand) -> Result<(), String> {
    let Some(at) = command.args.iter().position(|arg| arg == "--exec") else { return Ok(()); };
    let mut args = command.args[..=at].to_vec();
    args.push("/bin/true".into());
    let failed = |detail: String| with_transport_context(error_json("transport_unavailable", detail), &command.context);
    match crate::runtime::platform::wsl(&args, None, crate::runtime::SESSION_FLOOR) {
        Ok((true, _, _)) => Ok(()),
        Ok(_) => Err(failed("WSL could not open a session in the Bob distro".into())),
        Err(error) => Err(failed(error)),
    }
}

fn runtime_env(key: &str) -> Result<Option<String>, String> {
    match std::env::var(key) {
        Ok(value) => Ok(Some(value)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(_) => Err(bad(&format!("{key} must be UTF-8"))),
    }
}

fn configured_runtime_command(lane: &str) -> Result<RuntimeCommand, String> {
    #[cfg(windows)]
    {
        let root = runtime_env("SystemRoot")?.ok_or_else(|| bad("This PC WSL transport requires SystemRoot"))?;
        runtime_command(true, &root, lane, runtime_env("BOB_RUNTIME_DISTRO")?.as_deref(), None, None, false)
    }
    #[cfg(unix)]
    {
        let (engine, container) = runtime_engine_container()?;
        runtime_command(false, "", lane, None, Some(&engine), Some(&container), false)
    }
}

/// The local container engine and runtime container, from the same operator-only overrides
/// the broker transport uses. Never taken from IPC; both values are validated names.
#[cfg(unix)]
pub(crate) fn runtime_engine_container() -> Result<(String, String), String> {
    use std::os::unix::fs::PermissionsExt;
    let podman_on_path = std::env::var_os("PATH").is_some_and(|paths| {
        std::env::split_paths(&paths).any(|path| {
            let program = path.join("podman");
            program.is_file() && program.metadata().is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
        })
    });
    let engine = runtime_env("BOB_RUNTIME_ENGINE")?.unwrap_or_else(|| if podman_on_path { "podman" } else { "docker" }.into());
    let container = runtime_env("BOB_RUNTIME_CONTAINER")?.unwrap_or_else(|| "bob-runtime".into());
    if !matches!(engine.as_str(), "podman" | "docker") { return Err(bad("invalid runtime engine: use podman or docker")); }
    if !valid_runtime_name(&container) { return Err(bad("invalid runtime container: use [A-Za-z0-9._-]{1,64}")); }
    Ok((engine, container))
}

fn read_settings_document(root: &Path) -> Result<Value, String> {
    match std::fs::read(settings_path(root)) {
        Ok(bytes) => {
            let value: Value = serde_json::from_slice(&bytes)
                .map_err(|e| bad(&format!("cannot parse portal settings: {e}")))?;
            if !value.is_object() { return Err(bad("portal settings must be an object")); }
            Ok(value)
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(json!({})),
        Err(e) => Err(bad(&format!("cannot read portal settings: {e}"))),
    }
}

pub(crate) fn local_model_lane() -> Result<String, String> {
    let connection = read_connection(&data_root())?;
    if connection.mode != "this_pc" || !valid_lane(&connection.lane) {
        return Err(bad("Select This PC and a valid lane in Connection settings first"));
    }
    Ok(connection.lane)
}

fn read_connection(root: &Path) -> Result<Connection, String> {
    resolve_connection(&read_settings_document(root)?, connection_env()?)
}

/// Bare host name / address only: ssh joins remote args into one remote shell
/// command, so the target must never be able to carry shell syntax.
fn valid_target(target: &str) -> bool {
    !target.is_empty()
        && target.len() <= 253
        && !target.starts_with('-')
        && target
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | ':'))
}

fn ssh_program() -> String {
    #[cfg(windows)]
    {
        let bundled = Path::new(r"C:\Windows\System32\OpenSSH\ssh.exe");
        if bundled.is_file() {
            bundled.to_string_lossy().into_owned()
        } else {
            "ssh".to_string()
        }
    }
    #[cfg(unix)]
    { "ssh".to_string() }
}

fn valid_broker_path(path: &str) -> bool {
    path.starts_with('/') && path.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '.' | '_' | '-'))
}

fn ssh_args(target: &str, connection: &Connection) -> Vec<String> {
    vec![
        "-o".into(),
        "BatchMode=yes".into(),
        "-o".into(),
        "StrictHostKeyChecking=yes".into(),
        "-o".into(),
        "ConnectTimeout=8".into(),
        format!("{}@{target}", connection.user),
        "python3".into(),
        "-u".into(),
        connection.broker.clone(),
        // r11: the server-side guard (BOB_PORTAL_APPROVALS=1 on the main gateway,
        // activated 2026-10-24) admits this token's own pending approvals, so the
        // broker's own refusal switch can come off. Server-side checks stay the
        // authority: ownership, pending-only, and the guard itself all still apply.
        "--portal-approvals".into(),
    ]
}

fn configured_ssh_args(target: &str, config: Option<&Path>, connection: &Connection) -> Result<Vec<String>, String> {
    let mut args = ssh_args(target, connection);
    if let Some(config) = config {
        if !config.is_absolute() || !config.is_file() {
            return Err(bad("portal SSH config must be an existing absolute file"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let metadata = std::fs::symlink_metadata(config)
                .map_err(|_| bad("portal SSH config metadata unavailable"))?;
            let owner = unsafe { libc::geteuid() };
            if !metadata.is_file() || metadata.mode() & 0o022 != 0
                || (metadata.uid() != owner && metadata.uid() != 0) {
                return Err(bad("portal SSH config must be a trusted regular file, not writable by others"));
            }
        }
        let name = config.to_str().ok_or_else(|| bad("portal SSH config must be UTF-8"))?;
        args.splice(0..0, ["-F".to_string(), name.to_string()]);
    }
    Ok(args)
}

// ------------------------------------------------------------- profile store

#[derive(Debug, Clone)]
struct PortalSettings {
    active: String,
    hardened: bool,
    path: PathBuf,
}

fn settings_path(root: &Path) -> PathBuf {
    root.join("portal-settings.json")
}

fn settings_are_private(root: &Path, path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        return [(root, 0o700), (path, 0o600)].iter().all(|(path, mode)| {
            std::fs::symlink_metadata(path).map(|m| !m.file_type().is_symlink() && m.permissions().mode() & 0o777 == *mode).unwrap_or(false)
        });
    }
    #[cfg(windows)]
    { windows_settings_are_private(root, path) }
}

// The native ACL reader supplies SID bytes; this decision is also exercised on
// Linux. Unknown ACE types fail closed rather than hide a possible grant.
#[derive(Debug)]
enum PrivacyAce {
    Allow(Vec<u8>),
    Deny,
    Unsupported,
}

fn private_dacl(protected: bool, aces: &[(bool, PrivacyAce)], trusted_sids: &[Vec<u8>]) -> bool {
    protected && aces.iter().all(|(inherited, ace)| {
        !inherited && match ace {
            PrivacyAce::Allow(sid) => trusted_sids.contains(sid),
            PrivacyAce::Deny => true,
            PrivacyAce::Unsupported => false,
        }
    })
}

#[cfg(windows)]
fn windows_settings_are_private(root: &Path, path: &Path) -> bool {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::Security::*;
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    unsafe {
        let mut token = std::ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 { return false; }
        let mut length = 0;
        GetTokenInformation(token, TokenUser, std::ptr::null_mut(), 0, &mut length);
        // usize storage gives TOKEN_USER the required pointer alignment.
        let mut buffer = vec![0usize; (length as usize).div_ceil(std::mem::size_of::<usize>())];
        let ok = length as usize >= std::mem::size_of::<TOKEN_USER>()
            && GetTokenInformation(token, TokenUser, buffer.as_mut_ptr().cast(), length, &mut length) != 0;
        CloseHandle(token);
        if !ok { return false; }
        let user = &*(buffer.as_ptr().cast::<TOKEN_USER>());
        if IsValidSid(user.User.Sid) == 0 { return false; }
        let mut trusted = vec![std::slice::from_raw_parts(
            user.User.Sid.cast::<u8>(), GetLengthSid(user.User.Sid) as usize,
        ).to_vec()];
        for kind in [WinLocalSystemSid, WinBuiltinAdministratorsSid] {
            let mut sid = [0u32; (SECURITY_MAX_SID_SIZE / 4) as usize];
            let mut size = SECURITY_MAX_SID_SIZE;
            if CreateWellKnownSid(kind, std::ptr::null_mut(), sid.as_mut_ptr().cast(), &mut size) == 0 {
                return false;
            }
            trusted.push(std::slice::from_raw_parts(sid.as_ptr().cast::<u8>(), size as usize).to_vec());
        }
        [root, path].iter().all(|path| {
            std::fs::symlink_metadata(path).map(|m| !m.file_type().is_symlink()).unwrap_or(false)
                && windows_path_is_private(path, &trusted)
        })
    }
}

#[cfg(windows)]
fn windows_path_is_private(path: &Path, trusted: &[Vec<u8>]) -> bool {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Authorization::{GetNamedSecurityInfoW, SE_FILE_OBJECT};
    use windows_sys::Win32::Security::*;
    use windows_sys::Win32::System::SystemServices::{ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE};

    let name: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    if name[..name.len() - 1].contains(&0) { return false; }
    unsafe {
        let mut descriptor = std::ptr::null_mut();
        let mut dacl = std::ptr::null_mut();
        let status = GetNamedSecurityInfoW(name.as_ptr(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
            std::ptr::null_mut(), std::ptr::null_mut(), &mut dacl, std::ptr::null_mut(), &mut descriptor);
        let private = (|| {
            // A null DACL grants everyone access. An empty DACL grants nobody.
            if status != 0 || descriptor.is_null() || dacl.is_null() || IsValidAcl(dacl) == 0 { return false; }
            let mut control = 0;
            let mut revision = 0;
            if GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) == 0 { return false; }
            let mut aces = Vec::new();
            for index in 0..(*dacl).AceCount as u32 {
                let mut ace = std::ptr::null_mut();
                if GetAce(dacl, index, &mut ace) == 0 || ace.is_null() { return false; }
                let header = &*ace.cast::<ACE_HEADER>();
                let decision = match header.AceType as u32 {
                    ACCESS_ALLOWED_ACE_TYPE => {
                        let offset = std::mem::offset_of!(ACCESS_ALLOWED_ACE, SidStart);
                        if (header.AceSize as usize) < offset + 8 { return false; }
                        let sid = ace.cast::<u8>().add(offset);
                        let size = 8 + 4 * (*sid.add(1) as usize);
                        if size > header.AceSize as usize - offset || IsValidSid(sid.cast()) == 0 { return false; }
                        PrivacyAce::Allow(std::slice::from_raw_parts(sid, size).to_vec())
                    }
                    ACCESS_DENIED_ACE_TYPE => PrivacyAce::Deny,
                    _ => PrivacyAce::Unsupported,
                };
                aces.push((header.AceFlags as u32 & INHERITED_ACE != 0, decision));
            }
            private_dacl(control & SE_DACL_PROTECTED != 0, &aces, trusted)
        })();
        if !descriptor.is_null() { LocalFree(descriptor); }
        private
    }
}

fn read_settings(root: &Path) -> PortalSettings {
    let path = settings_path(root);
    let active = std::fs::read_to_string(&path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| {
            value
                .get("active")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .filter(|id| PROFILE_IDS.contains(&id.as_str()))
        .unwrap_or_else(|| "bob_server".to_string());
    PortalSettings {
        active,
        hardened: settings_are_private(root, &path),
        path,
    }
}

/// Windows equivalent of the 0600 hardening: drop inherited ACEs and grant the
/// owning user full control only. Best effort — reported, never assumed.
#[cfg(windows)]
fn harden(path: &Path) -> bool {
    let user = std::env::var("USERNAME").unwrap_or_default();
    if user.is_empty() {
        return false;
    }
    Command::new(r"C:\Windows\System32\icacls.exe")
        .arg(path)
        .arg("/inheritance:r")
        .arg("/grant:r")
        .arg(format!("{user}:F"))
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(unix)]
fn harden(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    let mode = if path.is_dir() { 0o700 } else { 0o600 };
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).is_ok()
}

fn write_settings(root: &Path, id: &str) -> Result<PortalSettings, String> {
    if !PROFILE_IDS.contains(&id) {
        return Err(bad(&format!(
            "unknown runtime profile `{id}`; refusing a silent fallback"
        )));
    }
    let mut document = read_settings_document(root)?;
    document["schema"] = json!(SETTINGS_SCHEMA);
    document["active"] = json!(id);
    write_settings_document(root, &document)
}

fn write_settings_document(root: &Path, document: &Value) -> Result<PortalSettings, String> {
    std::fs::create_dir_all(root)
        .map_err(|e| error_json("bad_request", format!("cannot create data root: {e}")))?;
    let _root_hardened = harden(root);
    #[cfg(unix)]
    if !_root_hardened { return Err(bad("cannot secure portal data directory")); }
    let path = settings_path(root);
    let temp = root.join(format!("portal-settings-{}.tmp", uuid::Uuid::new_v4()));
    let body = document.to_string();
    #[cfg(unix)]
    let write = {
        use std::os::unix::fs::OpenOptionsExt;
        // create_new refuses symlinks and pre-existing temporary files.
        std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600)
            .open(&temp).and_then(|mut file| file.write_all(body.as_bytes()))
    };
    #[cfg(windows)]
    let write = std::fs::OpenOptions::new().write(true).create_new(true)
        .open(&temp).and_then(|mut file| file.write_all(body.as_bytes()));
    if let Err(e) = write {
        let _ = std::fs::remove_file(&temp);
        return Err(bad(&format!("cannot write settings: {e}")));
    }
    let hardened = harden(&temp);
    #[cfg(unix)]
    if !hardened { let _ = std::fs::remove_file(&temp); return Err(bad("cannot secure settings")); }
    if let Err(e) = std::fs::rename(&temp, &path) {
        let _ = std::fs::remove_file(&temp);
        return Err(bad(&format!("cannot commit settings: {e}")));
    }
    Ok(PortalSettings {
        active: document.get("active").and_then(Value::as_str).unwrap_or("bob_server").to_string(),
        hardened: hardened && settings_are_private(root, &path),
        path,
    })
}

pub(crate) fn developer_mode() -> bool {
    std::env::var("BOB_DEVELOPER_MODE").as_deref() == Ok("1")
        || std::env::var("BOB_DEVELOPER_FIXTURE").as_deref() == Ok("1")
}
fn portal_profile(id: &str) -> bool { matches!(id, "bob_server" | "bob_team") }
fn select_profile(root: &Path, id: &str, developer: bool) -> Result<PortalSettings, String> {
    if !developer && !portal_profile(id) {
        return Err(bad("legacy runtime profiles require developer mode"));
    }
    write_settings(root, id)
}
fn recover_profile(root: &Path, developer: bool) -> Result<(PortalSettings, Option<&'static str>), String> {
    let document = read_settings_document(root)?;
    if !developer && document.get("active").and_then(Value::as_str).is_some_and(|id| !portal_profile(id)) {
        return Ok((write_settings(root, "bob_server")?, Some("A retired runtime profile was reset to BoB.")));
    }
    Ok((read_settings(root), None))
}
fn profile_rows_for(developer: bool) -> Value {
    #[allow(unused_mut)]
    let mut rows = json!([
        {
            "id": "bob_server",
            "label": "Bob server — full portal",
            "transport": "native_ssh_portal",
            "execution_host": "server",
            "effect_owner": "bob_server",
            "available": true,
            "reason": null,
            "note": "Conversations, chat, projects, memory, approvals and read-only runs live on the server."
        },
        {
            "id": "bob_team",
            "label": "Bob read-only team (portal)",
            "transport": "native_ssh_portal",
            "execution_host": "server",
            "effect_owner": "bob_server",
            "available": true,
            "reason": null,
            "note": "Same configured portal transport."
        },
        {
            "id": "local_bob_model",
            "label": "Local Bob model (WSL)",
            "transport": "wsl_local",
            "execution_host": "this PC (WSL host)",
            "effect_owner": "cleanroom_local",
            "available": true,
            "reason": null,
            "note": "Unchanged local runtime: it does not talk to the server Bob."
        },
        {
            "id": "standalone_http",
            "label": "Standalone text endpoint (WSL)",
            "transport": "wsl_local",
            "execution_host": "this PC (WSL host)",
            "effect_owner": "cleanroom_local",
            "available": true,
            "reason": null,
            "note": "Unchanged explicit HTTP text endpoint; never a silent Bob fallback."
        }
    ]);
    #[cfg(unix)]
    for row in rows.as_array_mut().unwrap().iter_mut().skip(2) {
        row["label"] = json!(if row["id"] == "local_bob_model" { "Local Bob model (Linux)" } else { "Standalone text endpoint (Linux)" });
        row["note"] = json!("Existing native Linux workbench; execution and its explicit endpoint settings remain local.");
        row["transport"] = json!("native_local");
        row["execution_host"] = json!("this PC (native Linux)");
    }
    if !developer { rows.as_array_mut().unwrap().retain(|row| row["id"].as_str().is_some_and(portal_profile)); }
    rows
}

// Retain this process's startup notice across StrictMode's duplicate profile reads.
static PROFILE_NOTICE: Mutex<Option<&'static str>> = Mutex::new(None);
fn profiles_payload() -> Result<Value, String> {
    let root = data_root();
    let developer = developer_mode();
    let (settings, recovered) = recover_profile(&root, developer)?;
    let notice = {
        let mut notice = PROFILE_NOTICE.lock().unwrap_or_else(|e| e.into_inner());
        if recovered.is_some() { *notice = recovered; }
        *notice
    };
    let connection = read_connection(&root)?;
    let transport = if connection.mode == "this_pc" {
        let runtime = configured_runtime_command(&connection.lane)?;
        json!({
            "kind": if cfg!(windows) { "wsl_stdio_jsonl" } else { "container_stdio_jsonl" },
            "targets": connection.targets(), "ssh": runtime.program,
            "broker": "bob-broker", "mode": connection.mode, "lane": connection.lane,
        })
    } else {
        json!({"kind": "ssh_stdio_jsonl", "targets": connection.targets(),
            "ssh": ssh_program(), "broker": connection.broker, "mode": connection.mode})
    };
    Ok(json!({
        "schema": "portal-profiles-v1",
        "active": settings.active,
        "profiles": profile_rows_for(developer),
        "developer_mode": developer, "developer_fixture": std::env::var("BOB_DEVELOPER_FIXTURE").as_deref() == Ok("1"), "notice": notice,
        "data_root": root.to_string_lossy(),
        "settings": {
            "path": settings.path.to_string_lossy(),
            "hardened": settings.hardened,
            "schema": SETTINGS_SCHEMA,
        },
        "transport": transport,
    }))
}

// --------------------------------------------------------------- app state

#[derive(Default)]
pub struct PortalState {
    recovery: Mutex<crate::recovery::Recovery>,
    recovery_reconnect_required: AtomicBool,
    link: Mutex<Option<Arc<Link>>>,
    app: Mutex<Option<AppHandle>>,
    /// Serialises link creation (audit W-7). The UI issues several portal
    /// commands at once on mount (`Promise.all([portal_hello, portal_catalog])`)
    /// and each command runs on its own thread, so without this guard every
    /// caller saw an empty slot and spawned its own ssh child: one window ended
    /// up with two live brokers (observed 2026-10-24 13:12, PIDs 3861675 and
    /// 3863271). Held across the spawn AND the ping probe, so a waiter reuses
    /// the link the first caller proved live instead of creating a second one.
    spawn: Mutex<()>,
    closed: AtomicBool,
}

impl PortalState {
    pub(crate) fn broker_failed(&self)->bool{
        self.link.lock().map(|slot|slot.as_ref().is_some_and(|link|link.is_dead())).unwrap_or(true)
    }
    pub(crate) fn watchdog_broker_ok(&self)->bool{
        if self.broker_failed(){return false;}
        let existing=self.link.lock().ok().and_then(|slot|slot.clone());
        existing.map(|link|link.call("hello",json!({}),false).is_ok()).unwrap_or(true)
    }
    pub(crate) fn repair_broker_probe(&self)->bool{
        let Ok(link)=self.link_for(true,false)else{return false;};
        if link.call("hello",json!({}),false).is_ok(){return true;}
        // Only an explicit Repair may recycle a stalled native broker child.
        link.shutdown();
        if let Ok(mut slot)=self.link.lock(){if slot.as_ref().is_some_and(|old|Arc::ptr_eq(old,&link)){*slot=None;}}
        self.link_for(true,false).and_then(|next|next.call("hello",json!({}),false)).is_ok()
    }
    pub(crate) fn pc_call(&self,op:&str,args:Value,write:bool)->Reply{
        if !matches!(op,"pc.pending"|"pc.claim"|"pc.receipt"|"pc.end"){return Err(bad("No PC approval operation exists in the broker"));}
        self.link_for(false,write)?.call(op,args,false)
    }
    pub fn attach(&self, app: &AppHandle) {
        let mut slot = self.app.lock().unwrap_or_else(|e| e.into_inner());
        if slot.is_none() {
            *slot = Some(app.clone());
        }
    }

    fn sink(&self) -> Result<Sink, String> {
        let app = self
            .app
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
            .ok_or_else(|| {
                error_json(
                    "transport_unavailable",
                    "portal transport is not attached to a window yet",
                )
            })?;
        Ok(Arc::new(move |line: String| {
            let _ = app.emit(EVENT_TOPIC, line);
        }))
    }

    /// Return a live link, spawning one only when there is none or the caller
    /// explicitly asked for a reconnect (`portal_hello` / `portal_reconnect`).
    /// A dead link never respawns behind an ordinary send.
    fn ensure(&self, reconnect: bool, write: bool) -> Result<Arc<Link>, String> {
        // Single flight (audit W-7): one creation at a time, probed under the
        // guard so concurrent first commands cannot each spawn a broker.
        let _single_flight = self.spawn.lock().unwrap_or_else(|e| e.into_inner());
        if self.closed.load(Ordering::SeqCst) {
            return Err(error_json("not_dispatched", "portal window closed; nothing was sent"));
        }
        if self.recovery_reconnect_required.load(Ordering::SeqCst) && !reconnect {return Err(error_json(if write {"not_dispatched"} else {"transport_unavailable"},"Recovery ended all sessions; explicitly reconnect after receipt reconciliation."));}
        let root = data_root();
        let connection = read_connection(&root)?;
        let existing = {
            let slot = self.link.lock().unwrap_or_else(|e| e.into_inner());
            slot.clone()
        };
        if let Some(link) = existing {
            if !link.is_dead() {
                return Ok(link);
            }
            if !reconnect {
                // A write refused here proves nothing was written; a read is just a
                // dead-link fault. The two must not share a code (R7-4).
                return Err(with_transport_context(error_json(
                    if write { "not_dispatched" } else { "transport_unavailable" },
                    format!(
                        "portal link to {} is closed; {}",
                        link.target,
                        if write {
                            "nothing was sent — press Reconnect first"
                        } else {
                            "press Reconnect and inspect history before sending"
                        }
                    ),
                ), &link.context));
            }
            link.shutdown();
        }
        let attempts = if connection.mode == "this_pc" {
            vec![(format!("this_pc:{}", connection.lane), configured_runtime_command(&connection.lane)?)]
        } else {
            connection.require_server()?;
            let config = std::env::var_os("BOB_PORTAL_SSH_CONFIG")
                .filter(|value| !value.is_empty()).map(PathBuf::from);
            connection.targets().into_iter().map(|target| {
                let command = RuntimeCommand {
                    program: ssh_program(),
                    args: configured_ssh_args(&target, config.as_deref(), &connection)?,
                    context: connection.ssh_context(&target),
                };
                Ok((target, command))
            }).collect::<Result<Vec<_>, String>>()?
        };
        let mut last: Option<String> = None;
        for (target, command) in attempts {
            #[cfg(all(windows, not(test)))]
            if connection.mode == "this_pc" {
                if let Err(error) = open_runtime_session(&command) { last = Some(error); continue; }
            }
            let sink = self.sink()?;
            match Link::spawn_argv(&command.program, &command.args, &target, &command.context, &root, sink, DEADLINE, LONG_DEADLINE) {
                Ok(link) => match link.call("ping", json!({}), false) {
                    Ok(_) => {
                        // The app accepts this link now: from here on its death
                        // must reach the window (R7-1).
                        link.arm();
                        let mut slot = self.link.lock().unwrap_or_else(|e| e.into_inner());
                        *slot = Some(link.clone());
                        self.recovery_reconnect_required.store(false,Ordering::SeqCst);
                        return Ok(link);
                    }
                    Err(error) => {
                        link.shutdown();
                        last = Some(with_transport_context(error, &command.context));
                    }
                },
                Err(error) => last = Some(with_transport_context(error, &command.context)),
            }
        }
        let last = last.unwrap_or_else(|| {
            error_json("transport_unavailable", "no portal target is configured")
        });
        if write {
            // R9-2: the link slot was empty and no target could be reached, so no
            // byte of this write ever left the machine. Reporting the spawn error as
            // an unknown outcome told the owner "unconfirmed text" and locked the
            // composer for a request that was provably never sent (R7-4 regressed on
            // the empty-slot path).
            // R9b-NIT: splice the inner *detail*, not the whole JSON error blob, so the
            // banner reads like a sentence.
            let why = serde_json::from_str::<Value>(&last)
                .ok()
                .and_then(|v| v.get("detail").and_then(|d| d.as_str()).map(|s| s.to_string()))
                .unwrap_or_else(|| last.clone());
            return Err(error_json(
                "not_dispatched",
                format!("nothing was sent — press Reconnect first. {why}"),
            ));
        }
        Err(last)
    }

    /// Permanent window teardown, serialized with startup so a pending IPC
    /// cannot leave a newly spawned transport behind after destruction.
    pub fn close(&self) {
        let _guard = self.spawn.lock().unwrap_or_else(|e| e.into_inner());
        self.closed.store(true, Ordering::SeqCst);
        self.shutdown();
    }

    fn invalidate_after_recovery(&self) {
        let _single_flight=self.spawn.lock().unwrap_or_else(|e|e.into_inner());
        self.recovery_reconnect_required.store(true,Ordering::SeqCst);
        let link=self.link.lock().unwrap_or_else(|e|e.into_inner()).take();
        if let Some(link)=link {
            link.fail(error_json("credential_epoch_changed","Recovery ended all sessions. Reconnect after receipt reconciliation."),true);
            let _=link.queue.try_send(None);
        }
    }

    pub fn shutdown(&self) {
        let link = {
            let mut slot = self.link.lock().unwrap_or_else(|e| e.into_inner());
            slot.take()
        };
        if let Some(link) = link {
            link.closing.store(self.closed.load(Ordering::SeqCst), Ordering::SeqCst);
            link.shutdown();
        }
    }

    pub(crate) fn set_connection(&self, root: &Path, args: &Value) -> Reply {
        let mut recovery = self.recovery.lock().map_err(|_| bad("recovery busy"))?;
        let _guard = self.spawn.lock().unwrap_or_else(|e| e.into_inner());
        let mut saved = json!({});
        let mode = args.get("mode").and_then(Value::as_str)
            .ok_or_else(|| bad("connection mode is required and must be a string"))?;
        validate_connection_field("mode", mode)?;
        saved["mode"] = json!(mode);
        let fields: &[&str] = if mode == "this_pc" { &["lane"] } else { &["user", "host", "broker"] };
        for &field in fields {
            let value = args.get(field).and_then(Value::as_str)
                .ok_or_else(|| bad(&format!("connection {field} is required and must be a string")))?;
            validate_connection_field(field, value)?;
            saved[field] = json!(value);
        }
        let mut document = read_settings_document(root)?;
        document["schema"] = json!(SETTINGS_SCHEMA);
        document["connection"] = saved;
        let connection = resolve_connection(&document, connection_env()?)?;
        write_settings_document(root, &document)?;
        recovery.forget();
        self.shutdown();
        Ok(connection.payload())
    }

    fn link_for(&self, reconnect: bool, write: bool) -> Result<Arc<Link>, String> {
        self.ensure(reconnect, write)
    }
}

// --------------------------------------------------------------- arg plumbing

fn object_from(args: &Value, keys: &[&str]) -> Value {
    let mut map = serde_json::Map::new();
    if let Some(source) = args.as_object() {
        for key in keys {
            if let Some(value) = source.get(*key) {
                if !value.is_null() {
                    map.insert((*key).to_string(), value.clone());
                }
            }
        }
    }
    Value::Object(map)
}

fn require_str(args: &Value, key: &str) -> Result<String, String> {
    args.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| bad(&format!("`{key}` is required")))
}

fn clamp_u64(args: &mut Value, key: &str, default: u64, min: u64, max: u64) {
    let current = args.get(key).and_then(Value::as_u64).unwrap_or(default);
    let clamped = current.clamp(min, max);
    if let Some(map) = args.as_object_mut() {
        map.insert(key.to_string(), json!(clamped));
    }
}

fn op_for(command: &str) -> Option<(&'static str, bool, &'static [&'static str])> {
    Some(match command {
        "portal_workflow_catalog_v1" => ("workflows.catalog", true, &["contract_version"]),
        "portal_workflow_list_v1" => ("workflows.list", true, &["contract_version", "cursor", "limit"]),
        "portal_workflow_get_v1" => ("workflows.get", true, &["contract_version", "id", "revision"]),
        "portal_workflow_save_v1" => ("workflows.save", true, &["contract_version", "id", "definition", "expected_revision", "request_key"]),
        "portal_workflow_copy_v1" => ("workflows.copy", true, &["contract_version", "id", "revision", "name", "request_key"]),
        "portal_workflow_delete_v1" => ("workflows.delete", true, &["contract_version", "id", "expected_revision", "request_key"]),
        "portal_workflow_propose_v1" => ("workflows.propose", true, &["contract_version", "definition_ref", "definition", "inputs", "bindings", "project_id", "origin_conversation_id", "expires_at", "request_key", "workspace", "run_mode"]),
        "portal_workflow_proposal_v1" => ("workflows.proposal", true, &["contract_version", "proposal_id"]),
        "portal_workflow_proposals_v1" => ("workflows.proposals", true, &["contract_version", "cursor", "limit", "include_expired"]),
        "portal_workflow_propose_again_v1" => ("workflows.propose_again", true, &["contract_version", "proposal_id"]),
        "portal_workflow_start_v1" => ("workflows.grant_start", true, &["contract_version", "proposal_id", "proposal_revision", "proposal_digest", "request_key", "allow_same_model_review"]),
        "portal_workflow_runs_v1" => ("workflows.runs", true, &["contract_version", "cursor", "limit", "project_id", "status"]),
        "portal_workflow_run_v1" => ("workflows.run", true, &["contract_version", "run_id", "stage_cursor"]),
        "portal_workflow_events_v1" => ("workflows.events", true, &["contract_version", "run_id", "after_seq", "limit"]),
        "portal_workflow_pause_v1" => ("workflows.pause", true, &["contract_version", "run_id", "expected_revision", "request_key"]),
        "portal_workflow_resume_v1" => ("workflows.resume", true, &["contract_version", "run_id", "expected_revision", "wait_id", "request_key"]),
        "portal_workflow_stop_v1" => ("workflows.stop", true, &["contract_version", "run_id", "request_key"]),
        "portal_workflow_message_v1" => ("workflows.message", true, &["contract_version", "run_id", "stage_key", "text", "request_key"]),
        "portal_workflow_gate_v1" => ("workflows.gate", true, &["contract_version", "run_id", "wait_id", "decision", "expected_revision", "request_key"]),
        "portal_workflow_artifact_v1" => ("workflows.artifact", true, &["contract_version", "run_id", "artifact_id", "offset", "max_bytes"]),
        "portal_workflow_watch_v1" => ("workflows.watch", true, &["contract_version", "run_id", "after_seq", "watch"]),

        "portal_secret_set_v2" => ("secrets.set", true, &["provider", "key"]),
        "portal_auth_change_password_v2" => ("auth.change_password", true, &["current", "new", "totp"]),
        "portal_settings_status_v1" => ("settings.status", false, &["contract_version", "request_key", "operation_id"]),
        "portal_policy_get_v2" => ("policy.get", true, &["contract_version"]),
        "portal_policy_set_v2" => ("policy.set", true, &["contract_version", "policy", "expected_revision", "request_key"]),
        "portal_workspace_browse_v2" => ("workspace.browse", false, &["contract_version", "project_id", "path"]),
        "portal_builder_mode_get_v2" => ("builder.mode.get", false, &["contract_version", "conversation_id"]),
        "portal_builder_task_end_v2" => ("builder.task.end", true, &["contract_version", "conversation_id"]),
        "portal_builder_project_result_v2" => ("builder.project.result", false, &["contract_version", "project_id"]),
        "portal_builder_network_set_v2" => ("builder.network.set", true, &["contract_version", "project_id", "add", "remove", "expected_revision", "request_key"]),
        "portal_builder_network_log_v2" => ("builder.network_log", false, &["contract_version", "conversation_id", "job_id"]),
        "portal_builder_mode_set_v2" => ("builder.mode.set", true, &["contract_version", "conversation_id", "mode", "expected_revision", "request_key", "yolo_acknowledged"]),
        "portal_builder_approval_get_v2" => ("builder.approval.get", false, &["contract_version", "approval_id"]),
        "portal_builder_approve_v2" => ("builder.approve", true, &["contract_version", "approval_id", "decision", "request_key"]),
        "portal_builder_preview_v2" | "portal_builder_open_temp_copy_v2" => ("builder.preview", false, &["contract_version", "conversation_id", "path"]),
        "portal_owner_begin_v2" => ("owner.begin", true, &["contract_version", "operation", "resource", "proposal_revision"]),
        "portal_owner_confirm_v2" => ("owner.confirm", true, &["contract_version", "challenge_id", "password", "totp", "confirmation_mode"]),
        "portal_owner_status_v2" => ("owner.status", true, &["contract_version"]),
        "portal_owner_lock_v2" => ("owner.lock", true, &["contract_version"]),
        "portal_owner_logout_v2" => ("owner.logout", true, &["contract_version"]),
        "portal_flags_list_v2" => ("flags.list", true, &["contract_version", "run_id", "cursor", "limit", "status"]),
        "portal_flags_get_v2" => ("flags.get", true, &["contract_version", "flag_id", "after_event_seq", "job_cursor"]),
        "portal_flags_resolve_v2" => ("flags.resolve", true, &["contract_version", "flag_id", "expected_revision", "request_key", "decision", "guidance"]),
        "portal_grants_prepare_v2" => ("grants.prepare", true, &["contract_version", "request", "run_id", "expected_revision", "request_key"]),
        "portal_work_catalog_v2" => ("work.catalog", false, &["contract_version"]),
        "portal_work_preparation_v2" => ("work.preparation", false, &["contract_version","preparation_id"]),
        "portal_work_status_v2" => ("work.status", false, &["contract_version","job_id"]),
        "portal_work_workspace_create_v2" => ("work.workspace.create", true, &["contract_version","project_id","request_key"]),
        "portal_work_publish_v2" => ("work.publish", true, &["contract_version","job_id","proposal_digest","expected_revision","request_key"]),
        "portal_work_start_v2" => ("work.start", true, &["contract_version","preparation_id","request_key"]),
        "portal_grants_issue_v2" => ("grants.issue", true, &["contract_version", "proposal_id", "proposal_revision", "expected_revision", "request_key", "coverage_kind", "allow_long_runs", "auto_escalate", "call_ceiling", "expires_at", "allow_extended_jobs", "allow_custom_job_limit", "job_seconds_ceiling", "job_override_seconds", "job_auto_escalate"]),
        "portal_grants_list_v2" => ("grants.list", true, &["contract_version", "cursor", "limit"]),
        "portal_grants_get_v2" => ("grants.get", true, &["contract_version", "grant_id"]),
        "portal_grants_revoke_v2" => ("grants.revoke", true, &["contract_version", "grant_id", "expected_revision", "request_key"]),
        "portal_budgets_get_v2" => ("budgets.get", true, &["contract_version", "project_id"]),
        "portal_budgets_list_v2" => ("budgets.list", true, &["contract_version", "cursor", "limit", "kind"]),
        "portal_budgets_remove_v2" => ("budgets.remove", true, &["contract_version", "project_id", "expected_revision", "request_key"]),

        "portal_teams_list_v2" => ("teams.list", true, &["contract_version", "cursor", "limit"]),
        "portal_teams_save_v2" => ("teams.save", true, &["contract_version", "definition", "expected_revision", "request_key"]),
        "portal_runs_start_v2" => ("runs.start", true, &["contract_version", "team", "team_revision", "task", "request_key", "budget", "project_id", "conversation_id", "intent_id", "sprint_id", "occurrence_id", "evidence_ids", "acceptance", "job_coverage"]),
        "portal_runs_list_v2" => ("runs.list", true, &["contract_version", "cursor", "limit", "project_id", "status"]),
        "portal_run_get_v2" => ("runs.get", true, &["contract_version", "run_id", "after_event_seq", "receipt_cursor"]),
        "portal_run_cancel_v2" => ("runs.cancel", true, &["contract_version", "run_id", "request_key"]),
        "portal_run_resume_v2" => ("runs.resume", true, &["contract_version", "run_id", "expected_revision", "request_key", "budget"]),
        "portal_budget_set_v2" => ("budgets.set", true, &["contract_version", "project_id", "call_limit", "expected_revision", "request_key"]),
        "portal_hello" => ("hello", false, &[]),
        // `catalog` and `projects.list` are served by the broker with serial
        // gateway fanout (4 REST calls / up to 25 per-project GETs), so they get
        // the patient deadline instead of 30 s.
        "portal_catalog" => ("catalog", true, &[]),
        "portal_conversations" => ("conversations.list", false, &["limit", "offset"]),
        "portal_conversation_new" => (
            "conversations.create",
            false,
            &["title", "face_id", "model_preference", "project_id"],
        ),
        "portal_conversation_rename" => ("conversations.rename", false, &["id", "title"]),
        "portal_conversation_assign_project" => {
            ("conversations.assign_project", false, &["id", "project_id"])
        }
        // messages.page is a paginated read the broker may have to enrich; it
        // gets the patient deadline like the other fanout reads.
        "portal_messages_more" => ("messages.page", true, &["id", "limit", "before"]),
        "portal_send" => (
            "chat.send",
            true,
            &[
                "conversation_id",
                "content",
                "face_id",
                "model",
                "backend",
                "profile",
                "locale",
                "research",
            ],
        ),
        "portal_stop" => ("chat.stop", true, &["conversation_id"]),
        "portal_answer" => ("chat.answer", true, &["question_id", "answer"]),
        "portal_pins" => ("chat.pins", false, &["conversation_id"]),
        "portal_approvals" => ("approvals.list", false, &["status", "limit", "offset"]),
        "portal_approval_get" => ("approvals.get", false, &["approval_id"]),
        "portal_approval_decide" => (
            "approvals.decide",
            true,
            &["approval_id", "decision", "edit_content"],
        ),
        "portal_projects" => ("projects.list", true, &[]),
        "portal_memory_status" => ("memory.status", false, &[]),
        "portal_memory_list" => ("memory.list", false, &["limit", "cursor", "kind"]),
        "portal_files_roots" => ("files.roots", false, &[]),
        "portal_files_list" => ("files.list", false, &["attachment_id", "path", "cursor"]),
        "portal_files_read" => ("files.read", false, &["attachment_id", "path", "offset", "max_bytes"]),
        "portal_memory_search" => ("memory.search", false, &["query", "limit", "offset"]),
        "portal_memory_read" => ("memory.read", false, &["source_id", "offset", "max_chars"]),
        "portal_memory_save" => ("memory.save", true, &["content", "category"]),
        // Write-only provider key (owner decision 2026-11-02: keys live on the server only).
        // The key rides the broker link to the server's secret store; nothing is kept here.
        "portal_secret_set" => ("secrets.set", true, &["provider", "key"]),
        // Owner sign-in change from Settings (owner request 2026-11-02). The server must check the
        // current password and a fresh two-factor code before it changes anything.
        "portal_auth_change_password" => ("auth.change_password", true, &["current", "new", "totp"]),
        "portal_runs_start" => (
            "runs.start",
            true,
            &["team", "task", "max_model_calls", "max_seconds", "worker"],
        ),
        "portal_run_get" => ("runs.get", false, &["run_id"]),
        "portal_runs_list" => ("runs.list", false, &["limit", "offset"]),
        "portal_run_cancel" => ("runs.cancel", true, &["run_id"]),
        "portal_run_followup" => ("runs.followup", true, &["run_id", "content"]),
        // Workspace tool jobs. Start/decide carry a client request key so an
        // unknown outcome can be resent without a second job or decision; cancel
        // records intent; audit establishes cleanup and terminal state.
        "portal_jobs_attachments_v2" => ("jobs.attachments", true, &["contract_version"]),
        "portal_jobs_list_v2" => ("jobs.list", true, &["contract_version", "cursor", "limit"]),
        "portal_jobs_start_v2" => ("jobs.start", true, &["contract_version", "role", "task", "attachment", "request_key", "team", "team_revision", "budget", "job_policy", "job_coverage", "intent_id", "project_id", "worker_contract", "acceptance"]),
        "portal_jobs_audit_v2" => ("jobs.audit", true, &["contract_version", "job_id", "after_event_seq"]),
        "portal_jobs_cancel_v2" => ("jobs.cancel", true, &["contract_version", "job_id", "request_key"]),
        "portal_jobs_proposal_v2" => ("jobs.proposal", true, &["contract_version", "job_id"]),
        "portal_jobs_decide_v2" => ("jobs.decide", true, &["contract_version", "job_id", "decision", "proposal_digest", "request_key", "expected_revision"]),
        "portal_jobs_artifact_v2" => ("jobs.artifact", true, &["contract_version", "job_id", "filename", "offset", "max_bytes"]),
        "portal_jobs_attachments" => ("jobs.attachments", false, &[]),
        "portal_jobs_list" => ("jobs.list", false, &[]),
        "portal_jobs_start" => (
            "jobs.start",
            true,
            &["role", "task", "attachment", "request_key", "model_calls", "tool_calls", "wall_seconds"],
        ),
        "portal_jobs_audit" => ("jobs.audit", false, &["job_id"]),
        "portal_jobs_cancel" => ("jobs.cancel", true, &["job_id"]),
        "portal_jobs_proposal" => ("jobs.proposal", false, &["job_id"]),
        "portal_jobs_decide" => (
            "jobs.decide",
            true,
            &["job_id", "decision", "proposal_digest", "request_key"],
        ),
        "portal_jobs_artifact" => ("jobs.artifact", false, &["job_id", "filename"]),
        _ => return None,
    })
}

/// Validate/clamp every payload before it reaches the wire. Refusals are typed
/// and never reach the socket.
fn validate_owner_payload(op: &str, args: &Value) -> Result<(), String> {
    if op.starts_with("work.") {
        let required: &[&str] = match op {
            "work.catalog" => &[], "work.preparation" => &["preparation_id"], "work.status" => &["job_id"],
            "work.workspace.create" => &["project_id","request_key"], "work.start" => &["preparation_id","request_key"],
            "work.publish" => &["job_id","proposal_digest","request_key","expected_revision"], _ => return Err(bad("unknown work operation")),
        };
        if required.iter().any(|k| args.get(k).is_none()) { return Err(bad("required work field missing")); }
        for field in required.iter().filter(|k| **k != "expected_revision") {
            if !args[*field].as_str().is_some_and(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control)) {return Err(bad("invalid work identifier"));}
        }
    }
    if args.get("cursor").is_some_and(|v| !v.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 4096 && !s.chars().any(char::is_control))) { return Err(bad("invalid cursor")); }
    if args.get("kind").is_some_and(|v| v.as_str() != Some("project")) { return Err(bad("unsupported budget kind")); }
    for field in ["request_key", "project_id", "grant_id", "run_id", "proposal_id", "challenge_id"] {
        if let Some(v) = args.get(field) {
            if !v.as_str().is_some_and(|s| !s.trim().is_empty() && s.len() <= 128 && !s.chars().any(char::is_control)) { return Err(bad("invalid owner identifier")); }
        }
    }
    for field in ["proposal_revision", "expected_revision", "call_ceiling", "job_seconds_ceiling", "job_override_seconds", "limit"] {
        if let Some(v) = args.get(field) {
            if !(v.is_null() && matches!((op, field), ("grants.issue", "expected_revision" | "call_ceiling" | "job_seconds_ceiling" | "job_override_seconds"))) && !wire_integer(v, 1, if field == "limit" {50} else {i64::MAX as u64}) { return Err(bad("invalid owner integer")); }
        }
    }
    let required: &[&str] = match op {
        "owner.begin" => &["operation", "resource"], "owner.confirm" => &["challenge_id"],
        "grants.prepare" => &["request_key"],
        "grants.issue" => &["proposal_id", "proposal_revision", "expected_revision", "request_key", "coverage_kind", "allow_long_runs", "auto_escalate", "call_ceiling", "expires_at"],
        "grants.get" => &["grant_id"], "grants.revoke" => &["grant_id", "expected_revision", "request_key"],
        "budgets.get" => &["project_id"], "budgets.remove" => &["project_id", "expected_revision", "request_key"], _ => &[],
    };
    if required.iter().any(|key| args.get(key).is_none()) { return Err(bad("required owner field missing")); }
    if op == "owner.confirm" {
        if let Some(mode) = args.get("confirmation_mode") {
            if mode.as_str() != Some("signed_in_user") || args.get("password").is_some() || args.get("totp").is_some() {
                return Err(bad("invalid owner confirmation mode"));
            }
        } else {
            let password = args["password"].as_str().ok_or_else(|| bad("password must be text"))?;
            let code = args["totp"].as_str().ok_or_else(|| bad("totp must be text"))?;
            if password.is_empty() || password.len() > 1024 || code.len() != 6 || !code.bytes().all(|b| b.is_ascii_digit()) { return Err(bad("invalid owner factors")); }
        }
    }
    if op == "owner.begin" {
        if !matches!(args["operation"].as_str(), Some("workflows.grant_start" | "workflows.resume" | "workflows.gate" | "secrets.set" | "policy.get" | "policy.set" | "runs.resume" | "budgets.get" | "budgets.list" | "budgets.set" | "budgets.remove" | "grants.issue" | "grants.list" | "grants.get" | "grants.revoke" | "flags.resolve" | "jobs.decide" | "work.workspace.create" | "work.publish" | "builder.mode.set" | "builder.approve" | "builder.network.set")) { return Err(bad("invalid owner operation")); }
        let resource = args["resource"].as_object().ok_or_else(|| bad("resource must be an object"))?;
        if let Some(operation) = args["operation"].as_str().filter(|op| op.starts_with("workflows.")) {
            let required: &[&str] = match operation { "workflows.grant_start" => &["proposal_id"], "workflows.resume" => &["run_id"], "workflows.gate" => &["run_id","wait_id"], _ => return Err(bad("invalid workflow owner operation")) };
            if resource.len() != required.len() || required.iter().any(|k| !resource.get(*k).is_some_and(safe_settings_id)) { return Err(bad("workflow owner action requires its exact resource")); }
        } else if resource.contains_key("wait_id") { return Err(bad("wait resource requires a workflow gate")); }
        if args["operation"].as_str() == Some("jobs.decide") && (resource.len() != 1 || !resource.get("job_id").and_then(Value::as_str).is_some_and(|s| s.len() == 32 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))) { return Err(bad("job decision requires one server job resource")); }
        if resource.iter().any(|(k,v)| !["run_id", "project_id", "grant_id", "proposal_id", "provider", "flag_id", "job_id", "conversation_id", "approval_id", "requested_mode", "wait_id"].contains(&k.as_str()) || !v.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 128)) { return Err(bad("invalid owner resource")); }
        if let Some(mode) = resource.get("requested_mode") {
            if args["operation"] != "builder.mode.set" || !matches!(mode.as_str(), Some("suggest" | "approve" | "auto" | "yolo")) { return Err(bad("invalid requested Builder mode")); }
        }
        if let Some(field) = match args["operation"].as_str() { Some("builder.mode.set") => Some("conversation_id"), Some("builder.approve") => Some("approval_id"), Some("builder.network.set") => Some("project_id"), _ => None } {
            let expected = if resource.contains_key("requested_mode") { 2 } else { 1 };
            if resource.len() != expected || !safe_settings_id(&args["resource"][field]) { return Err(bad("builder action requires one scoped resource")); }
        }
        if (args["operation"] == "secrets.set" && (resource.len() != 1 || !settings_provider(&args["resource"]["provider"]))) || (matches!(args["operation"].as_str(), Some("policy.get" | "policy.set")) && !resource.is_empty()) { return Err(bad("invalid settings resource")); }
    }
    if op == "grants.prepare" {
        if args.get("request").is_some() == args.get("run_id").is_some() { return Err(bad("prepare requires request or run_id")); }
        if args.get("run_id").is_some() && args.get("expected_revision").is_none() { return Err(bad("prepare run requires revision")); }
        if let Some(request) = args.get("request") {
            if request.get("intent_id").is_some() { return Err(bad("nested admission intent")); }
            normalize("portal_runs_start_v2", request.clone())?;
        }
    }
    if op == "grants.issue" {
        if !args["expected_revision"].is_null() || !matches!(args["coverage_kind"].as_str(), Some("task" | "project")) { return Err(bad("invalid grant coverage or revision")); }
        for field in ["allow_long_runs", "auto_escalate"] { if !args[field].is_boolean() { return Err(bad("explicit boolean grant policy required")); } }
        if !args["expires_at"].is_null() && !args["expires_at"].as_str().is_some_and(|s| s.len() <= 64 && s.contains('T')) { return Err(bad("invalid expiry")); }
        for field in ["allow_extended_jobs", "allow_custom_job_limit", "job_auto_escalate"] {
            if args.get(field).is_some_and(|v| !v.is_boolean()) { return Err(bad("invalid job grant boolean")); }
        }
    }
    Ok(())
}

fn validate_job_coverage(value: &Value) -> Result<(), String> {
    let scope = value.as_object().ok_or_else(|| bad("job coverage must be an object"))?;
    if scope.keys().any(|k| !["profiles", "workspaces", "attachments", "max_job_attempts", "job_seconds_budget", "allow_cli"].contains(&k.as_str())) { return Err(bad("unknown job coverage field")); }
    if !scope.get("max_job_attempts").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("finite job coverage required")); }
    if scope.get("job_seconds_budget").is_some_and(|v| !v.is_null() && !wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("invalid job coverage budget")); }
    if scope.get("allow_cli") != Some(&json!(false)) { return Err(bad("CLI jobs unavailable")); }
    let profiles = scope.get("profiles").and_then(Value::as_object).ok_or_else(|| bad("job profiles required"))?;
    if profiles.is_empty() || profiles.len() > 16 || profiles.iter().any(|(k,v)| k.is_empty() || !v.as_str().is_some_and(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))) { return Err(bad("invalid job profiles")); }
    let workspaces = scope.get("workspaces").and_then(Value::as_object).ok_or_else(|| bad("job workspaces required"))?;
    if workspaces.is_empty() || workspaces.len() > 32 { return Err(bad("invalid job workspaces")); }
    for (id, paths) in workspaces {
        if id.is_empty() || id.len() > 64 || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') { return Err(bad("invalid registered workspace")); }
        let paths = paths.as_array().ok_or_else(|| bad("job paths required"))?;
        if paths.is_empty() || paths.len() > 32 || paths.iter().any(|v| !v.as_str().is_some_and(|s| !s.is_empty() && !s.starts_with('/') && !s.contains('\\') && !s.split('/').any(|p| p.is_empty() || p == ".."))) { return Err(bad("invalid job path")); }
    }
    if scope.get("attachments").is_some_and(|v| !v.as_array().is_some_and(|a| a.len() <= 32 && a.iter().all(|v| v.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 128)))) { return Err(bad("invalid job attachments")); }
    Ok(())
}

// tool-jobs/2 has its own native commands; legacy ceilings are never widened.
fn validate_checked_acceptance(value: &Value) -> Result<(), String> {
    let fail = || bad("invalid checked acceptance");
    let object = value.as_object().ok_or_else(fail)?;
    if object.len() != 1 { return Err(fail()); }
    let criteria = value["criteria"].as_array().ok_or_else(fail)?;
    if criteria.is_empty() || criteria.len() > 100 { return Err(fail()); }
    let identifier = |v: &Value| v.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 128 && s.as_bytes()[0].is_ascii_alphanumeric() && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b)));
    let mut seen = std::collections::HashSet::new();
    for c in criteria {
        let fields = c.as_object().ok_or_else(fail)?;
        if fields.len() != 4 || fields.keys().any(|k| !["id","required","output_kind","checks"].contains(&k.as_str())) || !identifier(&c["id"]) || !seen.insert(c["id"].as_str().unwrap()) || !c["required"].is_boolean() || c["output_kind"] != "proposal" { return Err(fail()); }
        let checks = c["checks"].as_object().ok_or_else(fail)?;
        let ids = c["checks"]["required_check_ids"].as_array().ok_or_else(fail)?;
        if checks.len() != 1 || ids.is_empty() || ids.len() > 100 || !ids.iter().all(identifier) { return Err(fail()); }
        let unique: std::collections::HashSet<_> = ids.iter().map(|i| i.as_str().unwrap()).collect();
        if unique.len() != ids.len() { return Err(fail()); }
    }
    if !criteria.iter().any(|c| c["required"] == true) { return Err(fail()); }
    Ok(())
}

fn validate_jobs_payload(op: &str, args: &Value) -> Result<(), String> {
    let hex = |v: &Value, len: usize| v.as_str().is_some_and(|s| s.len() == len && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)));
    if matches!(op, "jobs.audit" | "jobs.cancel" | "jobs.proposal" | "jobs.decide" | "jobs.artifact") && !hex(&args["job_id"], 32) { return Err(bad("invalid job id")); }
    for (field, min, max) in [("limit", 1, 50), ("after_event_seq", 0, i64::MAX as u64), ("offset", 0, 4_194_304), ("max_bytes", 1, 65_536), ("expected_revision", 1, i64::MAX as u64), ("team_revision", 1, i64::MAX as u64)] {
        if args.get(field).is_some_and(|v| !wire_integer(v, min, max)) { return Err(bad("invalid Jobs integer; no clamping")); }
    }
    if args.get("cursor").is_some_and(|v| !hex(v, 32)) { return Err(bad("invalid Jobs cursor")); }
    if matches!(op, "jobs.start" | "jobs.decide") || args.get("request_key").is_some() {
        let k = require_str(args, "request_key")?;
        if k.trim().is_empty() || k.len() > 128 || k.chars().any(char::is_control) { return Err(bad("invalid Jobs request key")); }
    }
    if op == "jobs.decide" {
        if !matches!(args["decision"].as_str(), Some("approve" | "deny")) || !hex(&args["proposal_digest"], 64) || args.get("expected_revision").is_none() { return Err(bad("digest-bound decision and revision required")); }
    }
    if op == "jobs.artifact" {
        let name = require_str(args, "filename")?;
        let valid = name.strip_suffix(".json").and_then(|s| s.split_once('-')).is_some_and(|(prefix, id)| !prefix.is_empty() && prefix.bytes().all(|b| b.is_ascii_lowercase()) && hex(&json!(id), 32));
        if !valid || name.len() > 80 { return Err(bad("invalid artifact name")); }
    }
    if op == "jobs.start" {
        if !matches!(args["role"].as_str(), Some("researcher" | "reviewer" | "proposer" | "coder")) { return Err(bad("invalid job role")); }
        let task = require_str(args, "task")?;
        if task.trim().is_empty() || task.len() > 16_384 { return Err(bad("job task exceeds 16 KiB")); }
        let attachment = require_str(args, "attachment")?;
        if attachment.is_empty() || attachment.len() > 64 || !attachment.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b)) { return Err(bad("invalid registered attachment")); }
        if require_str(args, "team")?.len() > 64 || args.get("team_revision").is_none() { return Err(bad("saved team revision required")); }
        for field in ["project_id", "intent_id"] { if args.get(field).is_some() && { let text = require_str(args, field)?; text.len() > 128 || text.chars().any(char::is_control) } { return Err(bad("invalid Jobs identifier")); } }
        let checked = args["role"] == "coder";
        if checked {
            if args["worker_contract"] != "checked-edits/1" { return Err(bad("coder requires checked-edits/1")); }
            validate_checked_acceptance(&args["acceptance"])?;
        } else if args.get("worker_contract").is_some() || args.get("acceptance").is_some() { return Err(bad("checked contract requires coder")); }
        let budget = args.get("budget").and_then(Value::as_object).ok_or_else(|| bad("job budget required"))?;
        if budget.keys().any(|k| !["start_calls", "wall_mode", "wall_seconds", "max_job_attempts", "job_seconds_budget"].contains(&k.as_str())) { return Err(bad("unknown Jobs budget field")); }
        if !budget.get("start_calls").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("finite model budget required")); }
        let no_wall = checked && budget.get("wall_mode").and_then(Value::as_str) == Some("none");
        if no_wall { if budget.get("wall_seconds") != Some(&Value::Null) { return Err(bad("no wall requires null seconds")); } }
        else if !budget.get("wall_seconds").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("finite job wall required")); }
        if checked && !budget.get("max_job_attempts").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("finite checked attempts required")); }
        if budget.get("max_job_attempts").is_some_and(|v| !wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("invalid job attempts")); }
        if (!no_wall && !matches!(budget.get("wall_mode").and_then(Value::as_str), Some("fixed" | "follow_budget"))) || budget.get("job_seconds_budget").is_some_and(|v| !v.is_null() && !wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("invalid job wall or seconds budget")); }
        let policy = args.get("job_policy").and_then(Value::as_object).ok_or_else(|| bad("job policy required"))?;
        if policy.keys().any(|k| !["proposed_seconds", "rationale", "auto_escalate"].contains(&k.as_str())) || !policy.get("proposed_seconds").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) || !policy.get("rationale").and_then(Value::as_str).is_some_and(|s| !s.trim().is_empty() && s.len() <= 2000) || (if checked { !policy.get("auto_escalate").is_some_and(Value::is_boolean) } else { policy.get("auto_escalate") != Some(&json!(false)) }) { return Err(bad("invalid proposal-only job policy")); }
        if let Some(coverage) = args.get("job_coverage") {
            validate_job_coverage(coverage)?;
            // Tighten the Jobs-specific envelope without changing grant payloads.
            let registered_id = |s: &str| !s.is_empty() && s.len() <= 64 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b));
            if coverage["profiles"].as_object().unwrap().keys().any(|k| !registered_id(k)) { return Err(bad("invalid Jobs profile id")); }
            if coverage["workspaces"].as_object().unwrap().values().any(|paths| paths.as_array().unwrap().iter().any(|v| {
                let path = v.as_str().unwrap(); path.len() > 2048 || path.contains(':') || path.chars().any(char::is_control)
            })) { return Err(bad("invalid Jobs workspace path")); }
            if !coverage["attachments"].as_array().is_some_and(|a| !a.is_empty() && a.iter().all(|v| v.as_str().is_some_and(registered_id))) { return Err(bad("invalid Jobs attachment ids")); }
            if !coverage["attachments"].as_array().is_some_and(|a| a.contains(&json!(attachment))) { return Err(bad("attachment must be in reviewed job coverage")); }
        }
    }
    Ok(())
}

fn wire_integer(value: &Value, min: u64, max: u64) -> bool {
    match value {
        Value::Number(_) => value.as_u64().is_some_and(|n| n >= min && n <= max && n <= 9_007_199_254_740_991),
        Value::String(text) => !text.is_empty() && (text == "0" || !text.starts_with('0')) && text.bytes().all(|b| b.is_ascii_digit()) && text.parse::<u64>().is_ok_and(|n| n >= min && n <= max),
        _ => false,
    }
}
fn safe_settings_id(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 128 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'))
}
fn settings_provider(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 64 && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-'))
}

fn validate_approval_policy(value: &Value) -> Result<(), String> {
    let fields = value.as_object().ok_or_else(|| bad("policy must be an object"))?;
    if fields.len() != 2 || fields.keys().any(|k| !["posture", "scope"].contains(&k.as_str())) || !matches!(value["posture"].as_str(), Some("inherit" | "scoped" | "confirm")) { return Err(bad("invalid policy posture or fields")); }
    let scope = value["scope"].as_object().ok_or_else(|| bad("policy scope required"))?;
    if scope.len() != 4 || scope.keys().any(|k| !["auto_actions", "escalate_actions", "may_touch", "may_not_touch"].contains(&k.as_str())) { return Err(bad("invalid policy scope fields")); }
    const ACTIONS: &[&str] = &["read", "write", "cc_edit", "model_call", "shell", "python", "git", "codex", "mcp", "email_send", "email_reply", "form_submit", "purchase", "file_delete", "shell_dangerous", "merge_to_main"];
    for name in ["auto_actions", "escalate_actions", "may_touch", "may_not_touch"] {
        let items = scope[name].as_array().ok_or_else(|| bad("policy rules must be arrays"))?;
        if items.len() > (if name.ends_with("actions") { ACTIONS.len() } else { 100 }) || (value["posture"] == "inherit" && !items.is_empty()) { return Err(bad("invalid policy rules")); }
        for item in items {
            if name.ends_with("actions") {
                if !item.as_str().is_some_and(|s| ACTIONS.contains(&s)) { return Err(bad("invalid policy action")); }
            } else {
                let rule = item.as_object().ok_or_else(|| bad("attachment rule required"))?;
                if rule.len() != 2 || rule.keys().any(|k| !["attachment_id", "path"].contains(&k.as_str())) || !safe_settings_id(&item["attachment_id"]) { return Err(bad("invalid policy attachment")); }
                let path = item["path"].as_str().ok_or_else(|| bad("relative policy path required"))?;
                let plain = if path == "**" { "" } else { path.strip_suffix("/**").unwrap_or(path) };
                if plain.len() > 2048 || plain.split('/').count() > 32 || path.chars().any(|c| c.is_control() || c == '\\' || c == ':' || c == '%') || plain.starts_with('/') || plain.split('/').any(|p| p == ".." || p == "." || p.contains(['*', '?', '[', ']']) || (p.is_empty() && !plain.is_empty())) { return Err(bad("invalid relative policy path")); }
            }
        }
    }
    Ok(())
}

fn builder_relative_path(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 4096 &&
        !s.contains(['\\', ':']) && !s.chars().any(char::is_control) &&
        s.split('/').all(|part| !part.is_empty() && part != "." && part != ".."))
}

fn builder_hostname(s: &str) -> bool {
    s.len() <= 253 && s.contains('.') && !s.rsplit('.').next().unwrap_or("").bytes().all(|b| b.is_ascii_digit()) &&
        s.split('.').all(|label| !label.is_empty() && label.len() <= 63 &&
            label.as_bytes().first().is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit()) &&
            label.as_bytes().last().is_some_and(|b| b.is_ascii_lowercase() || b.is_ascii_digit()) &&
            label.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'))
}

fn validate_builder_payload(op: &str, args: &Value) -> Result<(), String> {
    let required: &[&str] = match op {
        "builder.mode.get" => &["conversation_id"],
        "builder.task.end" => &["conversation_id"],
        "builder.project.result" => &["project_id"],
        "builder.mode.set" => &["conversation_id", "mode", "expected_revision", "request_key"],
        "builder.approval.get" => &["approval_id"],
        "builder.approve" => &["approval_id", "decision", "request_key"],
        "builder.preview" => &["conversation_id", "path"],
        "builder.network.set" => &["project_id", "expected_revision", "request_key"],
        "builder.network_log" => &[],
        _ => return Err(bad("unsupported builder operation")),
    };
    if required.iter().any(|k| args.get(k).is_none()) { return Err(bad("missing builder field")); }
    for field in ["conversation_id", "approval_id", "project_id", "job_id", "request_key"] {
        if args.get(field).is_some_and(|v| !safe_settings_id(v)) { return Err(bad("invalid builder identifier")); }
    }
    if op == "builder.mode.set" {
        if !matches!(args["mode"].as_str(), Some("suggest" | "approve" | "auto" | "yolo")) ||
            !wire_integer(&args["expected_revision"], 0, i64::MAX as u64) { return Err(bad("invalid builder mode or revision")); }
        if args["mode"] == "yolo" {
            if args["yolo_acknowledged"] != true { return Err(error_json("yolo_ack_required", "Explicit yolo risk confirmation required")); }
        } else if args.get("yolo_acknowledged").is_some() { return Err(bad("yolo acknowledgement only applies to yolo")); }
    }
    if op == "builder.network_log" && args.get("conversation_id").is_some() == args.get("job_id").is_some() { return Err(bad("choose one conversation or job")); }
    if op == "builder.network.set" {
        if !wire_integer(&args["expected_revision"], 0, i64::MAX as u64) { return Err(bad("invalid network revision")); }
        let mut add = Vec::new(); let mut remove = Vec::new();
        for (field, domains) in [("add", &mut add), ("remove", &mut remove)] {
            if let Some(value) = args.get(field) {
                let rows = value.as_array().ok_or_else(|| bad("domain array required"))?;
                if rows.len() > 256 { return Err(bad("too many domains")); }
                for row in rows {
                    let domain = row.as_str().filter(|s| builder_hostname(s)).ok_or_else(|| bad("canonical DNS hostname required"))?;
                    if domains.contains(&domain) { return Err(bad("duplicate domain")); }
                    domains.push(domain);
                }
            }
        }
        if add.is_empty() && remove.is_empty() || add.iter().any(|d| remove.contains(d)) { return Err(bad("nonempty disjoint domain changes required")); }
    }
    if op == "builder.approve" && !matches!(args["decision"].as_str(), Some("allow_once" | "allow_session" | "allow_project" | "deny")) { return Err(bad("invalid builder decision")); }
    if op == "builder.preview" && !builder_relative_path(&args["path"]) { return Err(bad("preview needs a relative workspace path")); }
    Ok(())
}

const BUILDER_PREVIEW_CAP: usize = 5 * 1024 * 1024;
const RUNNABLE_EXTERNAL_PREVIEW_CSP: &str = "default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";

fn builder_decode_base64(text: &str) -> Result<Vec<u8>, String> {
    if text.len() > BUILDER_PREVIEW_CAP.div_ceil(3) * 4 || text.len() % 4 != 0 { return Err(bad("invalid preview encoding")); }
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let sextet = |byte: u8| -> Result<u8, String> { match byte {
        b'A'..=b'Z' => Ok(byte-b'A'), b'a'..=b'z' => Ok(byte-b'a'+26), b'0'..=b'9' => Ok(byte-b'0'+52), b'+' => Ok(62), b'/' => Ok(63), _ => Err(bad("invalid preview encoding")),
    }};
    for (i, chunk) in text.as_bytes().chunks_exact(4).enumerate() {
        let a = sextet(chunk[0])?; let b = sextet(chunk[1])?;
        let last = (i+1)*4 == text.len();
        out.push(a<<2 | b>>4);
        if chunk[2] == b'=' {
            if !last || chunk[3] != b'=' || b & 15 != 0 { return Err(bad("invalid preview padding")); }
        } else {
            let c = sextet(chunk[2])?; out.push(b<<4 | c>>2);
            if chunk[3] == b'=' {
                if !last || c & 3 != 0 { return Err(bad("invalid preview padding")); }
            } else { out.push(c<<6 | sextet(chunk[3])?); }
        }
    }
    if out.len() > BUILDER_PREVIEW_CAP { return Err(bad("preview exceeds 5 MiB")); }
    Ok(out)
}

fn builder_html_escape(value: &str) -> String {
    value.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#39;")
}

// Only viewer extensions can reach an OS handler (HTML is sandbox-wrapped). Caller bytes, filenames,
// local paths and executable extensions are never accepted through native IPC.
// Installed before any document scripts, with separate stores for each frame/tab.
const PREVIEW_STORAGE_SHIM: &str = r#"<script>(function(){for(const name of ['localStorage','sessionStorage']){try{const s=window[name];s.setItem('__bob_probe__','1');s.removeItem('__bob_probe__');continue;}catch(_){}const data=new Map();const api={getItem:k=>data.has(String(k))?data.get(String(k)):null,setItem:(k,v)=>{data.set(String(k),String(v));},removeItem:k=>{data.delete(String(k));},clear:()=>data.clear(),key:i=>Array.from(data.keys())[Number(i)]??null,get length(){return data.size;}};const store=new Proxy(api,{get:(o,k)=>k in o?Reflect.get(o,k):data.get(String(k)),set:(o,k,v)=>{data.set(String(k),String(v));return true;},deleteProperty:(o,k)=>data.delete(String(k))||true,ownKeys:()=>Array.from(data.keys()),getOwnPropertyDescriptor:(o,k)=>data.has(String(k))?{configurable:true,enumerable:true,value:data.get(String(k)),writable:true}:undefined});Object.defineProperty(window,name,{value:store,configurable:true});}})();</script>"#;
fn builder_external_html(text: &str) -> String {
    let csp = builder_html_escape(RUNNABLE_EXTERNAL_PREVIEW_CSP);
    // Keep an initial doctype ahead of the shim so standards mode is preserved.
    let trimmed = text.trim_start();
    let at = if trimmed.to_ascii_lowercase().starts_with("<!doctype") {
        trimmed.find('>').map(|i| text.len() - trimmed.len() + i + 1).unwrap_or(0)
    } else { 0 };
    let framed = format!("{}{}{}", &text[..at], PREVIEW_STORAGE_SHIM, &text[at..]);
    format!("<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"{csp}\"><title>BoB workspace preview</title></head><body><p>Preview: scripts, forms and dialogs run here. Nothing the page saves is kept after this tab closes, and it has no network access.</p><iframe title=\"Workspace preview\" sandbox=\"allow-scripts allow-forms allow-modals\" referrerpolicy=\"no-referrer\" style=\"width:100%;height:90vh;border:0\" srcdoc=\"{}\"></iframe></body></html>", builder_html_escape(&framed))
}

fn builder_temp_content(preview: &Value, requested: &str) -> Result<(String, Vec<u8>), String> {
    if preview["path"].as_str() != Some(requested) || preview["truncated"] != false { return Err(bad("preview changed or is truncated")); }
    let encoded = preview["bytes_b64"].as_str().ok_or_else(|| bad("preview bytes unavailable"))?;
    let bytes = builder_decode_base64(encoded)?;
    if preview["size"].as_u64() != Some(bytes.len() as u64) { return Err(bad("invalid preview size")); }
    let mime = preview["mime"].as_str().ok_or_else(|| bad("invalid preview MIME"))?;
    let image = match mime {
        "image/png" if bytes.starts_with(b"\x89PNG\r\n\x1a\n") => Some("png"),
        "image/jpeg" if bytes.starts_with(b"\xff\xd8\xff") => Some("jpg"),
        "image/gif" if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") => Some("gif"),
        "image/webp" if bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP".as_slice()) => Some("webp"),
        _ => None,
    };
    if let Some(extension) = image { return Ok((extension.to_string(), bytes)); }
    if !(mime.starts_with("text/") || matches!(mime, "application/json" | "application/xml" | "application/javascript")) { return Err(bad("unsupported preview type")); }
    let text = String::from_utf8(bytes).map_err(|_| bad("preview is not UTF-8 text"))?;
    if mime == "text/html" {
        let html = builder_external_html(&text);
        return Ok(("html".into(), html.into_bytes()));
    }
    Ok(("txt".into(), text.into_bytes()))
}

fn write_builder_temp(extension: &str, bytes: &[u8]) -> Result<PathBuf, String> {
    if !["txt", "html", "png", "jpg", "gif", "webp"].contains(&extension) || bytes.len() > BUILDER_PREVIEW_CAP * 6 { return Err(bad("invalid preview copy")); }
    // UUID directory + create_new prevents pre-existing files/symlinks from
    // redirecting this write. No user-supplied path components are used.
    let dir = std::env::temp_dir().join(format!("bob-builder-preview-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir(&dir).map_err(|_| bad("temporary preview unavailable"))?;
    let path = dir.join(format!("preview.{extension}"));
    let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&path).map_err(|_| bad("temporary preview unavailable"))?;
    file.write_all(bytes).map_err(|_| bad("temporary preview unavailable"))?;
    Ok(path)
}

fn open_builder_temp(path: &Path) -> Result<(), String> {
    #[cfg(windows)] {
        use std::os::windows::ffi::OsStrExt;
        use std::ffi::c_void;
        #[link(name = "shell32")]
        extern "system" { fn ShellExecuteW(hwnd: *mut c_void, operation: *const u16, file: *const u16, parameters: *const u16, directory: *const u16, show: i32) -> *mut c_void; }
        let file: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let operation: Vec<u16> = "open".encode_utf16().chain(Some(0)).collect();
        let result = unsafe { ShellExecuteW(std::ptr::null_mut(), operation.as_ptr(), file.as_ptr(), std::ptr::null(), std::ptr::null(), 1) };
        if result as isize <= 32 { return Err(bad("default preview handler unavailable")); }
    }
    #[cfg(not(windows))] {
        Command::new("xdg-open").arg(path).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().map_err(|_| bad("default preview handler unavailable"))?;
    }
    Ok(())
}

fn normalize(command: &str, raw: Value) -> Result<(&'static str, bool, Value), String> {
    let (op, long, keys) = op_for(command).ok_or_else(|| {
        error_json(
            "bad_request",
            format!("unknown portal command `{command}`; no dispatch"),
        )
    })?;
    let mut args = object_from(&raw, keys);
    // Sensitive IPC has a closed public schema. The key is never journaled or
    // retried; only the fresh native request identifier may leave on failure.
    if command == "portal_auth_change_password_v2" {
        let fields = raw.as_object().ok_or_else(|| bad("password arguments must be an object"))?;
        if fields.len() != 3 || fields.keys().any(|k| !keys.contains(&k.as_str())) { return Err(bad("invalid password fields")); }
        // Transport bounds only. Password policy belongs to the server.
        for field in ["current", "new"] {
            if !raw[field].as_str().is_some_and(|s| !s.is_empty() && s.len() <= 4096) { return Err(bad("invalid password input")); }
        }
        if !raw["totp"].as_str().is_some_and(|s| s.len() == 6 && s.bytes().all(|b| b.is_ascii_digit())) { return Err(bad("invalid authenticator input")); }
        args["contract_version"] = json!(2);
        args["request_key"] = json!(uuid::Uuid::new_v4().to_string());
        return Ok((op, long, args));
    }
    if op == "secrets.set" {
        let fields = raw.as_object().ok_or_else(|| bad("key arguments must be an object"))?;
        if fields.len() != 2 || fields.keys().any(|k| !["provider", "key"].contains(&k.as_str())) { return Err(bad("invalid key fields")); }
        let key = require_str(&raw, "key")?;
        if !settings_provider(&raw["provider"]) || key.len() > 16384 || key.chars().any(|c| c.is_control() || c.is_whitespace()) { return Err(bad("invalid provider or key")); }
        args["contract_version"] = json!(2);
        args["request_key"] = json!(uuid::Uuid::new_v4().to_string());
        return Ok((op, long, args));
    }
    if op == "settings.status" {
        let fields = raw.as_object().ok_or_else(|| bad("status arguments must be an object"))?;
        if raw["contract_version"].as_u64() != Some(1) || fields.len() != 2 || fields.keys().any(|k| !keys.contains(&k.as_str())) || !["request_key", "operation_id"].iter().any(|k| raw.get(k).is_some_and(safe_settings_id)) { return Err(bad("status requires exactly one safe receipt identifier")); }
        args.as_object_mut().unwrap().remove("contract_version");
        return Ok((op, long, args));
    }
    if command.starts_with("portal_workflow_") {
        let fields = raw.as_object().ok_or_else(|| bad("workflow arguments must be an object"))?;
        if raw["contract_version"].as_u64() != Some(1) || fields.keys().any(|k| !keys.contains(&k.as_str())) { return Err(bad("invalid workflow contract or fields")); }
        let required: &[&str] = match op {
            "workflows.get" => &["id"], "workflows.save" => &["definition","expected_revision","request_key"],
            "workflows.copy" => &["id","revision","name","request_key"], "workflows.delete" => &["id","expected_revision","request_key"],
            "workflows.propose" => &["inputs","bindings","project_id","origin_conversation_id","expires_at","request_key"],
            "workflows.proposal" | "workflows.propose_again" => &["proposal_id"], "workflows.grant_start" => &["proposal_id","proposal_revision","proposal_digest","request_key"],
            "workflows.run" => &["run_id"], "workflows.events" => &["run_id","after_seq"],
            "workflows.pause" | "workflows.resume" => &["run_id","expected_revision","request_key"],
            "workflows.stop" => &["run_id","request_key"], "workflows.message" => &["run_id","stage_key","text","request_key"],
            "workflows.gate" => &["run_id","wait_id","decision","expected_revision","request_key"],
            "workflows.artifact" => &["run_id","artifact_id"], "workflows.watch" => &["run_id","after_seq","watch"], _ => &[],
        };
        if required.iter().any(|k| raw.get(k).is_none()) { return Err(bad("missing workflow field")); }
        for key in ["id","run_id","proposal_id","artifact_id","wait_id","request_key","project_id","origin_conversation_id","cursor","stage_cursor"] {
            if let Some(v) = raw.get(key) {
                if v.is_null() && ["project_id","origin_conversation_id"].contains(&key) { continue; }
                if !v.as_str().is_some_and(|v| !v.is_empty() && v.len() <= if ["cursor","stage_cursor"].contains(&key) {4096} else {128} && !v.chars().any(char::is_control)) { return Err(bad("invalid workflow identifier")); }
            }
        }
        for key in ["revision","proposal_revision","expected_revision","after_seq","limit","offset","max_bytes"] {
            if let Some(v) = raw.get(key) {
                if key == "expected_revision" && op == "workflows.save" && v.is_null() { continue; }
                let min = if ["after_seq","offset"].contains(&key) {0} else {1};
                let max = match key {"limit" => if op == "workflows.events" {100} else {50}, "max_bytes" => 65536, _ => i64::MAX as u64};
                if !wire_integer(v,min,max) { return Err(bad("invalid workflow integer")); }
            }
        }
        if let Some(v) = raw.get("proposal_digest") { if !v.as_str().is_some_and(|v|v.len()==64 && v.bytes().all(|b|b.is_ascii_digit() || (b'a'..=b'f').contains(&b))) {return Err(bad("invalid workflow digest"));} }
        for key in ["watch","allow_same_model_review","include_expired"] {if raw.get(key).is_some_and(|v|!v.is_boolean()) {return Err(bad("invalid workflow choice"));}}
        if op == "workflows.gate" && !matches!(raw["decision"].as_str(),Some("approve"|"reject")) {return Err(bad("invalid gate decision"));}
        if op == "workflows.message" && !raw["text"].as_str().is_some_and(|v|!v.trim().is_empty() && v.chars().count()<=12000) {return Err(bad("invalid workflow message"));}
        if op == "workflows.propose" && (raw.get("definition").is_some() == raw.get("definition_ref").is_some() || !raw["inputs"].is_object() || !raw["bindings"].is_object()) {return Err(bad("invalid workflow proposal"));}
        if raw.get("run_mode").is_some_and(|v|!matches!(v.as_str(),Some("auto"|"approve"))) {return Err(bad("invalid workflow mode"));}
        return Ok((op,long,raw));
    }
    if command.ends_with("_v2") {
        let fields = raw.as_object().ok_or_else(|| bad("arguments must be an object"))?;
        if fields.keys().any(|key| !keys.contains(&key.as_str())) {
            return Err(bad("unknown execution v2 field"));
        }
        if raw.get("contract_version").and_then(Value::as_u64) != Some(2) {
            return Err(bad("execution requires contract_version 2"));
        }
        args = raw.clone(); // v2 nulls are explicit policy choices, never omitted.
        if op == "workspace.browse" {
            if !safe_settings_id(&args["project_id"]) || args.get("path").is_some_and(|v| v.as_str() != Some("") && !builder_relative_path(v)) {
                return Err(bad("browse needs a project and relative workspace path"));
            }
            args.as_object_mut().unwrap().remove("contract_version");
            return Ok((op, false, args));
        }
        if op.starts_with("builder.") {
            validate_builder_payload(op, &args)?;
            args.as_object_mut().unwrap().remove("contract_version");
            return Ok((op, long, args));
        }
        if op == "policy.get" || op == "policy.set" {
            if op == "policy.get" { args.as_object_mut().unwrap().remove("contract_version"); }
            else {
                if !safe_settings_id(&raw["request_key"]) || !wire_integer(&raw["expected_revision"], 0, i64::MAX as u64) { return Err(bad("invalid policy revision or request key")); }
                validate_approval_policy(&raw["policy"])?;
            }
            return Ok((op, long, args));
        }
        if op.starts_with("owner.") || op.starts_with("grants.") || op.starts_with("work.") || matches!(op, "budgets.get" | "budgets.list" | "budgets.remove") {
            validate_owner_payload(op, &args)?;
            if matches!(op, "owner.status" | "owner.lock" | "owner.logout") { args.as_object_mut().unwrap().remove("contract_version"); }
            return Ok((op, long, args));
        }
        if op.starts_with("jobs.") {
            validate_jobs_payload(op, &args)?;
            return Ok((op, long, args));
        }
        if op.starts_with("flags.") {
            for field in ["flag_id", "run_id", "request_key", "cursor", "job_cursor"] {
                if let Some(value) = raw.get(field) {
                    if !value.as_str().is_some_and(|v| !v.trim().is_empty() && v.len() <= (if field == "cursor" {4096} else {128}) && !v.chars().any(char::is_control)) { return Err(bad("invalid flag identifier")); }
                }
            }
            if matches!(op, "flags.get" | "flags.resolve") { require_str(&args, "flag_id")?; }
            for field in ["after_event_seq", "expected_revision", "limit"] {
                if let Some(value) = raw.get(field) {
                    if !wire_integer(value, if field == "after_event_seq" {0} else {1}, if field == "limit" {50} else {i64::MAX as u64}) { return Err(bad("invalid flag integer")); }
                }
            }
            if op == "flags.list" && raw.get("status").is_some_and(|v| !matches!(v.as_str(), Some("open" | "resolved" | "all"))) { return Err(bad("invalid flag status")); }
            if op == "flags.resolve" {
                require_str(&args, "request_key")?;
                if !raw.get("expected_revision").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) { return Err(bad("flag revision required")); }
                if !matches!(args["decision"].as_str(), Some("inspected" | "resume_with_guidance" | "authorize_budget" | "cancel")) { return Err(bad("invalid flag decision")); }
                let guidance = require_str(&args, "guidance")?;
                if guidance.trim().is_empty() || guidance.chars().count() > 4000 { return Err(bad("guidance must contain 1..4000 characters")); }
            }
            return Ok((op, long, args));
        }
        for key in ["team_revision", "limit", "after_event_seq", "receipt_cursor"] {
            if let Some(value) = raw.get(key) {
                let min = if key == "after_event_seq" || key == "receipt_cursor" { 0 } else { 1 };
                let max = if key == "limit" { 50 } else { i64::MAX as u64 };
                if !wire_integer(value, min, max) {
                    return Err(bad("invalid execution v2 integer; no clamping"));
                }
            }
        }
        if matches!(op, "teams.save" | "runs.start" | "runs.cancel" | "runs.resume" | "budgets.set") {
            if require_str(&args, "request_key")?.len() > 128 { return Err(bad("request_key exceeds 128 bytes")); }
        }
        if matches!(op, "teams.save" | "runs.resume" | "budgets.set") {
            if op == "teams.save" && !raw.get("definition").is_some_and(Value::is_object) { return Err(bad("definition is required")); }
            let revision = raw.get("expected_revision").ok_or_else(|| bad("expected_revision is required"))?;
            if !(revision.is_null() && op != "runs.resume") && !wire_integer(revision, 1, i64::MAX as u64) {
                return Err(bad("invalid expected_revision"));
            }
            args["expected_revision"] = revision.clone();
        }
        if op == "budgets.set" {
            if require_str(&args, "project_id")?.len() > 128 { return Err(bad("project_id exceeds 128 bytes")); }
            if !raw.get("call_limit").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) {
                return Err(bad("call_limit must be a positive integer; no clamping"));
            }
        }
        if op == "runs.start" {
            require_str(&args, "team")?;
            require_str(&args, "task")?;
            if args.get("team_revision").is_none() { return Err(bad("team_revision is required")); }
            if let Some(coverage) = args.get("job_coverage") { validate_job_coverage(coverage)?; }
        }
        if op == "runs.start" || (op == "runs.resume" && raw.get("budget").is_some()) {
            let budget = raw.get("budget").and_then(Value::as_object).ok_or_else(|| bad("budget is required"))?;
            if budget.keys().any(|key| !["start_calls", "wall_mode", "wall_seconds", "max_job_attempts", "job_seconds_budget"].contains(&key.as_str())) {
                return Err(bad("unknown budget field"));
            }
            for field in ["max_job_attempts", "job_seconds_budget"] {
                if let Some(v) = budget.get(field) {
                    if !(field == "job_seconds_budget" && v.is_null()) && !wire_integer(v, 1, i64::MAX as u64) { return Err(bad("invalid job budget integer")); }
                }
            }
            if (op == "runs.start" || budget.contains_key("start_calls")) && !budget.get("start_calls").is_some_and(|v| wire_integer(v, 1, i64::MAX as u64)) {
                return Err(bad("start_calls must be a positive integer; no clamping"));
            }
            if op == "runs.start" || budget.contains_key("wall_mode") || budget.contains_key("wall_seconds") {
                let seconds = budget.get("wall_seconds").ok_or_else(|| bad("wall_seconds is required"))?;
                match budget.get("wall_mode").and_then(Value::as_str) {
                    Some("none") if seconds.is_null() => {},
                    Some("fixed" | "follow_budget") if wire_integer(seconds, 1, i64::MAX as u64) => {},
                    _ => return Err(bad("wall_mode requires positive integral seconds, or none with null seconds")),
                }
            }
        }
        if matches!(op, "runs.get" | "runs.cancel" | "runs.resume") { require_str(&args, "run_id")?; }
        return Ok((op, long, args));
    }
    if matches!(op, "memory.list" | "files.roots" | "files.list" | "files.read") {
        let fields = raw.as_object().ok_or_else(|| bad("arguments must be an object"))?;
        if fields.keys().any(|key| !keys.contains(&key.as_str())) {
            return Err(bad("unknown owner read field"));
        }
        for (key, max) in [("cursor", 200), ("kind", 200), ("attachment_id", 100), ("path", 2048)] {
            if let Some(value) = args.get(key) {
                let text = value.as_str().ok_or_else(|| bad("owner read field must be a string"))?;
                if text.len() > max || (key != "path" && text.trim().is_empty()) {
                    return Err(bad("invalid owner read string"));
                }
            }
        }
        for (key, default, min, max) in if op == "memory.list" {
            vec![("limit", 50, 1, 50)]
        } else if op == "files.read" {
            vec![("offset", 0, 0, 16_777_216), ("max_bytes", 65_536, 1, 65_536)]
        } else { vec![] } {
            let n = match args.get(key) {
                None => default,
                Some(v) => v.as_u64().filter(|n| *n >= min && *n <= max)
                    .ok_or_else(|| bad("owner read integer out of bounds"))?,
            };
            args[key] = json!(n);
        }
        if op == "memory.list" {
            if let Some(kind) = args.get("kind").and_then(Value::as_str) {
                if !["family_memory", "bob_event", "bob_fact"].contains(&kind) { return Err(bad("invalid kind")); }
            }
        }
        if op == "files.list" || op == "files.read" {
            require_str(&args, "attachment_id")?;
            let path = args.get("path").and_then(Value::as_str).unwrap_or("");
            if (op == "files.read" && path.is_empty()) || (!path.is_empty() &&
                (path.split('/').count() > 32 || path.split('/').any(|p| ["", ".", ".."].contains(&p)) ||
                 path.chars().any(|c| c < ' ' || ['\\', ':', '%'].contains(&c)))) {
                return Err(bad("invalid relative path"));
            }
            args["path"] = json!(path);
        }
    }
    match command {
        "portal_conversations" => clamp_u64(&mut args, "limit", 30, 1, 50),
        "portal_approvals" => {
            clamp_u64(&mut args, "limit", 30, 1, 50);
            if args.get("status").is_none() {
                args["status"] = json!("pending");
            }
        }
        "portal_project_save" => {} // handled separately
        "portal_send" => {
            require_str(&args, "conversation_id")?;
            let content = require_str(&args, "content")?;
            if content.len() > 200_000 {
                return Err(bad("message content exceeds the 256 KiB request budget"));
            }
        }
        "portal_stop" => {
            require_str(&args, "conversation_id")?;
        }
        "portal_answer" => {
            require_str(&args, "question_id")?;
            require_str(&args, "answer")?;
            if args["answer"].as_str().map(|v| v.len()).unwrap_or(0) > 200_000 {
                return Err(bad("answer exceeds the 256 KiB request budget"));
            }
        }
        "portal_pins" => {
            require_str(&args, "conversation_id")?;
        }
        "portal_messages_more" => {
            require_str(&args, "id")?;
            clamp_u64(&mut args, "limit", 50, 1, 100);
        }
        "portal_conversation_new" => {
            require_str(&args, "title")?;
        }
        "portal_conversation_rename" => {
            require_str(&args, "id")?;
            require_str(&args, "title")?;
        }
        "portal_conversation_assign_project" => {
            require_str(&args, "id")?;
            if args.get("project_id").is_none() {
                args["project_id"] = Value::Null;
            }
        }
        "portal_approval_get" => {
            require_str(&args, "approval_id")?;
        }
        "portal_approval_decide" => {
            require_str(&args, "approval_id")?;
            let decision = require_str(&args, "decision")?;
            if decision != "approve" && decision != "reject" {
                return Err(bad("decision must be `approve` or `reject`"));
            }
        }
        "portal_memory_search" => {
            require_str(&args, "query")?;
            clamp_u64(&mut args, "limit", 20, 1, 50);
        }
        "portal_memory_read" => {
            require_str(&args, "source_id")?;
            clamp_u64(&mut args, "max_chars", 8000, 1, 12000);
        }
        "portal_memory_save" => {
            let content = require_str(&args, "content")?;
            if content.chars().count() > 12_000 {
                return Err(bad("memory.save content exceeds the 12000 character contract cap"));
            }
            let category = args
                .get("category")
                .and_then(Value::as_str)
                .unwrap_or("note")
                .to_string();
            if !["fact", "preference", "project", "note"].contains(&category.as_str()) {
                return Err(bad("category must be fact, preference, project or note"));
            }
            args["category"] = json!(category);
        }
        "portal_runs_start" => {
            require_str(&args, "team")?;
            require_str(&args, "task")?;
            clamp_u64(&mut args, "max_model_calls", 4, 1, 4);
            clamp_u64(&mut args, "max_seconds", 900, 30, 900);
        }
        "portal_run_get" | "portal_run_cancel" => {
            require_str(&args, "run_id")?;
        }
        "portal_runs_list" => {
            // A ledger page is a local read; clamp it exactly like every other
            // list so the broker never has to refuse an out-of-range limit.
            clamp_u64(&mut args, "limit", 20, 1, 50);
        }
        "portal_run_followup" => {
            require_str(&args, "run_id")?;
            require_str(&args, "content")?;
        }
        "portal_jobs_start" => {
            let role = require_str(&args, "role")?;
            if !["researcher", "coder", "reviewer"].contains(&role.as_str()) {
                return Err(bad("role must be researcher, coder or reviewer"));
            }
            if require_str(&args, "task")?.len() > 16_384 {
                return Err(bad("job task exceeds the 16 KiB server cap"));
            }
            require_str(&args, "request_key")?;
            for (key, default, max) in [("model_calls", 4, 16), ("tool_calls", 12, 64), ("wall_seconds", 60, 120)] {
                if args.get(key).is_some() {
                    clamp_u64(&mut args, key, default, 1, max);
                }
            }
        }
        "portal_jobs_audit" | "portal_jobs_cancel" | "portal_jobs_proposal" => {
            require_str(&args, "job_id")?;
        }
        "portal_jobs_decide" => {
            require_str(&args, "job_id")?;
            require_str(&args, "proposal_digest")?;
            require_str(&args, "request_key")?;
            let decision = require_str(&args, "decision")?;
            if decision != "approve" && decision != "deny" {
                return Err(bad("decision must be `approve` or `deny`"));
            }
        }
        "portal_jobs_artifact" => {
            require_str(&args, "job_id")?;
            require_str(&args, "filename")?;
        }
        "portal_auth_change_password" => {
            // Refusals here never quote a password: an error text can end up on screen or in a log.
            let current = require_str(&args, "current")?;
            let new = require_str(&args, "new")?;
            let totp = require_str(&args, "totp")?;
            for value in [&current, &new] {
                if value.len() > 256 || value.chars().any(|c| c.is_control()) {
                    return Err(bad("passwords must be at most 256 characters with no line breaks"));
                }
            }
            if new.chars().count() < 10 {
                return Err(bad("the new password must be at least 10 characters"));
            }
            if new == current {
                return Err(bad("the new password must differ from the current one"));
            }
            if totp.len() != 6 || !totp.chars().all(|c| c.is_ascii_digit()) {
                return Err(bad("the two-factor code is 6 digits"));
            }
        }
        _ => {}
    }
    if op == "auth.change_password" {
        args["contract_version"] = json!(2);
        args["request_key"] = json!(uuid::Uuid::new_v4().to_string());
    }
    Ok((op, long, args))
}

fn password_result(value: Value) -> Reply {
    if !value["changed"].is_boolean() || !value["restarted"].is_boolean()
        || (value["restarted"] == true && value["changed"] != true) { return Err(error_json("invalid_receipt", "Safe password receipt required")); }
    Ok(object_from(&value, &["changed", "restarted"]))
}

fn sensitive_password_result(result: Reply, request_key: &Value) -> Reply {
    result.and_then(password_result).map_err(|error| {
        let parsed: Value = serde_json::from_str(&error).unwrap_or(Value::Null);
        let raw = parsed["code"].as_str().unwrap_or("");
        if raw == "rate_limited" || raw == "gateway_http_429" {
            return safe_rate_limit(&parsed, request_key);
        }
        let code = if ["invalid_args", "invalid_password", "invalid_receipt", "request_key_used", "authentication_throttled", "owner_portal_required", "owner_context_invalid", "owner_session_invalid", "owner_session_expired", "client_upgrade_required", "operation_unavailable", "not_dispatched", "bad_request", "timeout_no_reply", "transport_unavailable"].contains(&raw) { raw } else { "password_outcome_unknown" };
        let mut safe = json!({"code":code,"detail":code,"hint":"Check the safe receipt; credentials were not retried.","request_key":request_key});
        if let Some(seconds) = parsed["retry_after"].as_u64().filter(|n| *n <= 301) { safe["retry_after"] = json!(seconds); }
        safe.to_string()
    })
}


fn password_pending_path(root: &Path) -> Result<PathBuf, String> {
    use std::hash::{Hash, Hasher};
    let connection = read_connection(root)?;
    let mut scope = std::collections::hash_map::DefaultHasher::new();
    (&connection.mode, &connection.lane, &connection.user, &connection.host, &connection.broker).hash(&mut scope);
    Ok(root.join(format!("password-pending-{:016x}.json", scope.finish())))
}

fn read_password_pending(path: &Path) -> Reply {
    match std::fs::symlink_metadata(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Value::Null),
        Ok(meta) if meta.is_file() && !meta.file_type().is_symlink() && meta.len() <= 1024 => (),
        _ => return Err(error_json("password_outcome_unknown", "Safe pending metadata unavailable; do not resubmit")),
    }
    let value: Value = std::fs::read(path).ok().and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .ok_or_else(|| error_json("password_outcome_unknown", "Safe pending metadata unavailable; do not resubmit"))?;
    if value["operation"] != "auth.change_password" || value["status"] != "unknown" || !safe_settings_id(&value["request_key"]) {
        return Err(error_json("password_outcome_unknown", "Safe pending metadata invalid; do not resubmit"));
    }
    Ok(object_from(&value, &["operation", "status", "request_key"]))
}

fn password_refused(code: &str) -> bool {
    if code == "rate_limited" || code == "gateway_http_429" { return true; }
    matches!(code, "invalid_args" | "invalid_password" | "authentication_throttled" |
        "owner_portal_required" | "owner_context_invalid" | "owner_session_invalid" | "owner_session_expired" |
        "client_upgrade_required" | "operation_unavailable" | "not_dispatched" | "bad_request")
}

fn submit_password_once(path: &Path, payload: Value, send: impl FnOnce(Value) -> Reply) -> Reply {
    // Persist only safe metadata BEFORE dispatch. Exclusive creation also
    // fences concurrent submissions and uncertain outcomes across app restarts.
    let key = payload["request_key"].clone();
    if let Some(root) = path.parent() {
        std::fs::create_dir_all(root).map_err(|_| error_json("not_dispatched", "Cannot persist safe receipt metadata"))?;
        let _ = harden(root);
    }
    let metadata = json!({"operation":"auth.change_password","request_key":key,"status":"unknown"});
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
    let mut file = match options.open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
            let prior = read_password_pending(path)?;
            return Err(json!({"code":"password_outcome_unknown","detail":"Check the pending receipt; credentials were not sent.","request_key":prior["request_key"]}).to_string());
        }
        Err(_) => return Err(error_json("not_dispatched", "Cannot persist safe receipt metadata")),
    };
    if file.write_all(metadata.to_string().as_bytes()).and_then(|_| file.sync_all()).is_err() {
        drop(file); let _ = std::fs::remove_file(path);
        return Err(error_json("not_dispatched", "Cannot persist safe receipt metadata"));
    }
    drop(file); let _ = harden(path);
    let result = sensitive_password_result(send(payload), &key);
    let refused = result.as_ref().err().and_then(|error| serde_json::from_str::<Value>(error).ok())
        .is_some_and(|error| password_refused(error["code"].as_str().unwrap_or("")));
    if result.is_ok() || refused { let _ = std::fs::remove_file(path); }
    result
}

fn reconcile_password_pending(path: &Path, lookup: &Value, receipt: &Value) {
    if receipt["result"].get("changed").is_some() && matches!(receipt["phase"].as_str(), Some("ready" | "failed"))
        && read_password_pending(path).ok().is_some_and(|pending| !pending.is_null() && pending["request_key"] == lookup["request_key"]) {
        let _ = std::fs::remove_file(path);
    }
}

fn secret_receipt(value: Value, provider: Option<&str>) -> Reply {
    let p = value["provider"].as_str().unwrap_or("");
    if !settings_provider(&value["provider"]) || provider.is_some_and(|expected| expected != p)
        || value["stored"] != true || !safe_settings_id(&value["operation_id"])
        || !value["fingerprint"].as_str().is_some_and(|s| s.len() == 16 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))) { return Err(error_json("invalid_receipt", "Safe storage receipt required")); }
    Ok(object_from(&value, &["provider", "stored", "fingerprint", "operation_id"]))
}

fn sensitive_settings_result(result: Reply, provider: &str, request_key: &Value) -> Reply {
    result.and_then(|value| secret_receipt(value, Some(provider))).map_err(|error| {
        let parsed: Value = serde_json::from_str(&error).unwrap_or(Value::Null);
        let raw = parsed["code"].as_str().unwrap_or("");
        if raw == "rate_limited" || raw == "gateway_http_429" {
            return safe_rate_limit(&parsed, request_key);
        }
        let code = if ["invalid_args", "invalid_receipt", "invalid_key", "unsupported_provider", "request_key_used", "owner_confirmation_required", "owner_session_invalid", "owner_session_expired", "client_upgrade_required", "operation_unavailable", "not_dispatched", "bad_request", "timeout_no_reply", "transport_unavailable"].contains(&raw) { raw } else { "settings_unavailable" };
        json!({"code":code,"detail":"Check the safe receipt before a new submission. Credentials were not retried.","hint":"","request_key":request_key}).to_string()
    })
}

// Credential sanitizers keep refusal metadata, never the upstream body.
fn safe_rate_limit(parsed: &Value, request_key: &Value) -> String {
    let mut safe = json!({"code":"rate_limited","detail":"This request was not applied. Try again in a few seconds.","hint":"","applied":false,"request_key":request_key});
    if let Some(seconds) = parsed["retry_after_seconds"].as_u64().or_else(|| parsed["retry_after"].as_u64()).filter(|n| *n <= 86400) {
        safe["retry_after_seconds"] = json!(seconds);
        safe["retry_after"] = json!(seconds);
    }
    safe.to_string()
}

fn settings_status_result(value: Value) -> Reply {
    if !safe_settings_id(&value["operation_id"]) { return Err(error_json("invalid_receipt", "Safe receipt required")); }
    let mut safe = object_from(&value, &["operation_id", "phase"]);
    match value["phase"].as_str() {
        Some("ready" | "failed") if value["result"].get("changed").is_some() => {
            safe["result"] = password_result(value["result"].clone())?;
        }
        Some("ready") if value["result"]["state"] == "proof_verified" => {
            let v = &value["result"];
            if !safe_settings_id(&v["challenge_id"]) || !v["expires_at"].is_u64() { return Err(error_json("invalid_receipt", "Safe challenge required")); }
            safe["result"] = object_from(v, &["challenge_id", "expires_at", "state"]);
        }
        Some("ready") => {
            let receipt = secret_receipt(value["result"].clone(), None)?;
            if receipt["operation_id"] != value["operation_id"] { return Err(error_json("invalid_receipt", "Receipt mismatch")); }
            safe["result"] = receipt;
        }
        Some("failed") => safe["error"] = json!("settings_failed"),
        Some("prepared" | "committed" | "restart-pending") => (),
        _ => return Err(error_json("invalid_receipt", "Unknown receipt phase")),
    }
    Ok(safe)
}

fn policy_result(value: Value, mutation: bool) -> Reply {
    let invalid = || error_json("invalid_receipt", "A complete policy projection and mutation receipt are required");
    if !wire_integer(&value["revision"], 0, i64::MAX as u64) || !value["immutable_restrictions"].is_object()
        || value.get("policy").is_none() { return Err(invalid()); }
    if !value["policy"].is_null() {
        let policy = &value["policy"];
        if !matches!(policy["posture"].as_str(), Some("inherit" | "scoped" | "confirm")) { return Err(invalid()); }
        for field in ["auto_actions", "escalate_actions", "may_touch", "may_not_touch"] {
            if !policy["scope"][field].as_array().is_some_and(|items| items.iter().all(Value::is_string)) { return Err(invalid()); }
        }
    }
    if mutation && (!safe_settings_id(&value["operation_id"]) || value["policy"].is_null()
        || !wire_integer(&value["revision"], 1, i64::MAX as u64)
        || !value["paused_runs"].as_array().is_some_and(|rows| rows.iter().all(safe_settings_id))) { return Err(invalid()); }
    let mut safe = object_from(&value, &["policy", "revision", "immutable_restrictions", "operation_id", "paused_runs"]);
    safe["policy"] = value["policy"].clone(); // revision zero explicitly carries null.
    Ok(safe)
}

fn validate_recovery_args(command:&str,args:&Value)->Result<(),String>{
    let o=args.as_object().ok_or_else(||bad("invalid recovery command"))?;
    let fields:&[&str]=match command {
        "portal_recovery_capability_v2"|"portal_recovery_status_v2"=>&["contract_version"],
        "portal_recovery_start_v2"=>&["contract_version","pair","printed"],
        "portal_recovery_act_v2"=>&["contract_version","handle","action"],
        _=>return Err(bad("invalid recovery command")),
    };
    if args["contract_version"].as_u64()!=Some(2)||o.len()!=fields.len()||o.keys().any(|k|!fields.contains(&k.as_str())) {return Err(bad("invalid recovery command"));}
    if command=="portal_recovery_start_v2" && (!matches!(args["pair"].as_str(),Some("setup"|"password_stick"|"password_totp"|"totp_stick"))||!args["printed"].is_boolean()){return Err(bad("invalid recovery choice"));}
    if command=="portal_recovery_act_v2" && (!safe_settings_id(&args["handle"])||!matches!(args["action"].as_str(),Some("seed"|"usb"|"print"|"commit"|"cancel"|"check"|"finish"))){return Err(bad("invalid recovery choice"));}
    Ok(())
}

// ------------------------------------------------------------------ dispatch

pub fn dispatch(state: &PortalState, app: &AppHandle, command: &str, args: Value) -> Reply {
    if matches!(command,"portal_owner_logout_v2"|"portal_owner_lock_v2"|"portal_connection_set"|"portal_auth_change_password_v2") {crate::pc::revoke_all();}
    if matches!(command,"portal_stop"|"portal_builder_task_end_v2"|"portal_builder_mode_set_v2"){
        if let Some(id)=args["conversation_id"].as_str(){crate::pc::revoke_conversation(id);}
        else if command=="portal_stop"{crate::pc::revoke_all();}
    }
    if !PORTAL_COMMANDS.contains(&command) {
        return Err(error_json(
            "bad_request",
            format!("unknown portal command `{command}`; no dispatch"),
        ));
    }
    state.attach(app);
    if command.starts_with("portal_recovery_") {
        validate_recovery_args(command,&args)?;
        if command=="portal_recovery_capability_v2" {return Ok(json!({"available":cfg!(windows),"reason":if cfg!(windows){""}else{"Native USB and print recovery require Windows."}}));}
        let mut recovery=state.recovery.lock().map_err(|_|bad("recovery busy"))?;
        let root=data_root();std::fs::create_dir_all(&root).map_err(|_|bad("safe journal unavailable"))?;
        let path=password_pending_path(&root)?.with_file_name(password_pending_path(&root)?.file_name().unwrap().to_string_lossy().replace("password-pending-","recovery-pending-"));
        if command=="portal_recovery_status_v2" {return recovery.status(&path).map_err(|_|error_json("recovery_pending_receipt","Check the original recovery receipt"));}
        let send=|op:&str, frame:Value|state.link_for(false,is_write_op(op)).and_then(|link|link.call(op,frame,op!="settings.status"&&op!="auth.factors.binding"));
        let result=if command=="portal_recovery_start_v2" {
            let pair=args["pair"].as_str().ok_or_else(||bad("invalid recovery pair"))?;
            let printed=args["printed"].as_bool().ok_or_else(||bad("invalid recovery source"))?;
            recovery.start(pair,printed,&path,send)
        } else {
            let handle=args["handle"].as_str().ok_or_else(||bad("invalid recovery handle"))?;
            let action=args["action"].as_str().ok_or_else(||bad("invalid recovery action"))?;
            recovery.act(handle,action,&path,send)
        };
        if result.as_ref().ok().is_some_and(|v|v["phase"]=="activated") {state.invalidate_after_recovery();}
        return result.map_err(|e|error_json(&e,&e));
    }
    match command {
        "portal_password_pending_v1" => {
            if args != json!({"contract_version":1}) { return Err(bad("pending receipt requires version 1")); }
            return read_password_pending(&password_pending_path(&data_root())?);
        }
        "portal_connection_get" => return Ok(read_connection(&data_root())?.payload()),
        "portal_connection_set" => return state.set_connection(&data_root(), &args),
        "portal_profiles" => return profiles_payload(),
        "portal_select_profile" => {
            let _guard = state.spawn.lock().unwrap_or_else(|e| e.into_inner());
            let id = require_str(&args, "profileId")?;
            let root = data_root();
            let settings = select_profile(&root, &id, developer_mode())?;
            let mut payload = profiles_payload()?;
            payload["settings"]["hardened"] = json!(settings.hardened);
            return Ok(payload);
        }
        "portal_reconnect" => {
            state.shutdown();
            let link = state.link_for(true, false)?;
            return link
                .call("hello", json!({}), false)
                .map(|value| json!({"target": link.target, "hello": value}));
        }
        _ => {}
    }
    if command == "portal_conversation_open" {
        let id = require_str(&args, "id")?;
        let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(50).clamp(1, 100);
        let link = state.link_for(false, false)?;
        let conversation = link.call("conversations.get", json!({"id": id}), false)?;
        let page = link.call("messages.page", json!({"id": id, "limit": limit}), true)?;
        return Ok(json!({
            "conversation": conversation.get("conversation").cloned().unwrap_or(Value::Null),
            "messages": page.get("items").cloned().unwrap_or_else(|| json!([])),
            "has_more": page.get("has_more").cloned().unwrap_or_else(|| json!(false)),
        }));
    }
    if command == "portal_project_save" {
        let link = state.link_for(false, true)?;
        if args.get("delete").and_then(Value::as_bool) == Some(true) {
            let id = require_str(&args, "id")?;
            return link.call("projects.delete", json!({"id": id}), false);
        }
        if args.get("id").and_then(Value::as_str).is_some() {
            let payload = object_from(
                &args,
                &[
                    "id",
                    "name",
                    "description",
                    "instructions",
                    "default_face_id",
                    "default_backend",
                ],
            );
            return link.call("projects.update", payload, false);
        }
        require_str(&args, "name")?;
        let payload = object_from(
            &args,
            &["name", "description", "instructions", "default_face_id", "default_backend"],
        );
        return link.call("projects.create", payload, false);
    }
    let (op, long, payload) = normalize(command, args)?;
    if command == "portal_builder_open_temp_copy_v2" {
        let preview = state.link_for(false, false)?.call(op, payload.clone(), false)?;
        let (extension, bytes) = builder_temp_content(&preview, payload["path"].as_str().unwrap())?;
        let path = write_builder_temp(&extension, &bytes)?;
        open_builder_temp(&path)?;
        return Ok(json!({"opened":true}));
    }
    if op == "auth.change_password" {
        let path = password_pending_path(&data_root())?;
        return submit_password_once(&path, payload, |frame| state.link_for(false, true).and_then(|link| link.call(op, frame, long)));
    }
    if op == "secrets.set" {
        let provider = payload["provider"].as_str().unwrap_or("").to_owned();
        let request_key = payload["request_key"].clone();
        let result = state.link_for(false, true).and_then(|link| link.call(op, payload, long));
        return sensitive_settings_result(result, &provider, &request_key);
    }
    let link = state.link_for(command == "portal_hello", is_write_op(op))?;
    if op == "settings.status" {
        let lookup = payload.clone();
        return link.call(op, payload, long).and_then(settings_status_result).map(|receipt| {
            if let Ok(path) = password_pending_path(&data_root()) { reconcile_password_pending(&path, &lookup, &receipt); }
            receipt
        });
    }
    if matches!(op, "policy.get" | "policy.set") {
        return link.call(op, payload, long).and_then(|value| policy_result(value, op == "policy.set"));
    }
    link.call(op, payload, long)
}
#[cfg(test)]
#[path = "builder_network_tests.rs"]
mod builder_network_tests;

#[cfg(test)]
#[path = "workspace_browse_tests.rs"]
mod workspace_browse_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::time::Instant;

    #[test]
    fn au3_expired_history_and_reproposal_are_closed_and_separate_from_start() {
        let history = json!({"contract_version":1,"include_expired":true,"limit":20});
        let (op, _, payload) = normalize("portal_workflow_proposals_v1", history.clone()).unwrap();
        assert_eq!(op, "workflows.proposals");
        assert_eq!(payload, history);
        assert!(!is_write_op(op));
        assert!(normalize("portal_workflow_proposals_v1", json!({"contract_version":1,"include_expired":"true"})).is_err());
        let command = "portal_workflow_propose_again_v1";
        assert!(PORTAL_COMMANDS.contains(&command));
        let again = json!({"contract_version":1,"proposal_id":"expired-proposal"});
        let (op, _, payload) = normalize(command, again.clone()).unwrap();
        assert_eq!(op, "workflows.propose_again");
        assert_eq!(payload, again);
        assert!(is_write_op(op));
        for invalid in [json!({"contract_version":1}), json!({"contract_version":1,"proposal_id":""}),
            json!({"contract_version":1,"proposal_id":"p","start":true}),
            json!({"contract_version":1,"proposal_id":"p","request_key":"not-in-this-wire"})] {
            assert!(normalize(command, invalid).is_err());
        }
    }

    #[test]
    fn wa2_pending_proposals_is_a_closed_read() {
        let command = "portal_workflow_proposals_v1";
        assert!(PORTAL_COMMANDS.contains(&command));
        let args = json!({"contract_version":1,"cursor":"next-page","limit":20});
        let (op, _, payload) = normalize(command,args.clone()).unwrap();
        assert_eq!(op,"workflows.proposals"); assert_eq!(payload,args); assert!(!is_write_op(op));
        assert!(normalize(command,json!({"contract_version":1})).is_ok());
        for invalid in [json!({"contract_version":2}),json!({"contract_version":1,"limit":51}),json!({"contract_version":1,"limit":0}),json!({"contract_version":1,"limit":true}),json!({"contract_version":1,"start":true}),json!({"contract_version":1,"cursor":""})] {
            assert!(normalize(command,invalid).is_err());
        }
    }

    #[test]
    fn wa1_workflow_contract_is_closed_exact_and_write_classified() {
        let digest = "a".repeat(64);
        let start = json!({"contract_version":1,"proposal_id":"proposal","proposal_revision":"9007199254740993","proposal_digest":digest,"request_key":"same-key","allow_same_model_review":true});
        let (op, _, payload) = normalize("portal_workflow_start_v1",start.clone()).unwrap();
        assert_eq!(op,"workflows.grant_start"); assert_eq!(payload,start); assert!(is_write_op(op));
        for suffix in ["catalog","list","get","save","copy","delete","propose","proposal","start","runs","run","events","pause","resume","stop","message","gate","artifact","watch"] {
            let command = format!("portal_workflow_{suffix}_v1"); assert!(PORTAL_COMMANDS.contains(&command.as_str())); assert!(op_for(&command).is_some());
        }
        for command in ["portal_workflow_catalog_v1","portal_workflow_run_v1","portal_workflow_events_v1","portal_workflow_artifact_v1","portal_workflow_watch_v1"] { assert!(!is_write_op(op_for(command).unwrap().0)); }
        for command in ["portal_workflow_pause_v1","portal_workflow_resume_v1","portal_workflow_stop_v1","portal_workflow_message_v1","portal_workflow_gate_v1"] { assert!(is_write_op(op_for(command).unwrap().0)); }
        let mut invalid = start.clone(); invalid["contract_version"] = json!(2); assert!(normalize("portal_workflow_start_v1",invalid).is_err());
        let mut invalid = start.clone(); invalid["surprise"] = json!(true); assert!(normalize("portal_workflow_start_v1",invalid).is_err());
        let mut invalid = start.clone(); invalid["proposal_revision"] = json!(9007199254740992u64); assert!(normalize("portal_workflow_start_v1",invalid).is_err());
        let events = json!({"contract_version":1,"run_id":"run","after_seq":"9007199254740993","limit":100}); assert_eq!(normalize("portal_workflow_events_v1",events.clone()).unwrap().2,events);
        assert!(normalize("portal_workflow_events_v1",json!({"contract_version":1,"run_id":"run","after_seq":0,"limit":101})).is_err());
        assert!(normalize("portal_workflow_gate_v1",json!({"contract_version":1,"run_id":"run","wait_id":"wait","expected_revision":1,"request_key":"k","decision":"maybe"})).is_err());
        for (operation,resource) in [("workflows.grant_start",json!({"proposal_id":"p"})),("workflows.resume",json!({"run_id":"r"})),("workflows.gate",json!({"run_id":"r","wait_id":"w"}))] {
            assert!(normalize("portal_owner_begin_v2",json!({"contract_version":2,"operation":operation,"resource":resource})).is_ok());
        }
    }

    #[test]
    fn builder_commands_validate_exact_wire_shapes_and_owner_resources() {
        for (command, raw, op, write) in [
            ("portal_builder_mode_get_v2", json!({"contract_version":2,"conversation_id":"c"}), "builder.mode.get", false),
            ("portal_builder_task_end_v2", json!({"contract_version":2,"conversation_id":"c"}), "builder.task.end", true),
            ("portal_builder_mode_set_v2", json!({"contract_version":2,"conversation_id":"c","mode":"approve","expected_revision":0,"request_key":"k"}), "builder.mode.set", true),
            ("portal_builder_approval_get_v2", json!({"contract_version":2,"approval_id":"a"}), "builder.approval.get", false),
            ("portal_builder_approve_v2", json!({"contract_version":2,"approval_id":"a","decision":"allow_once","request_key":"k"}), "builder.approve", true),
            ("portal_builder_preview_v2", json!({"contract_version":2,"conversation_id":"c","path":"app/index.html"}), "builder.preview", false),
            ("portal_builder_open_temp_copy_v2", json!({"contract_version":2,"conversation_id":"c","path":"app/index.html"}), "builder.preview", false),
        ] {
            assert!(PORTAL_COMMANDS.contains(&command));
            let (actual, _, payload) = normalize(command, raw.clone()).unwrap();
            assert_eq!(actual, op); assert_eq!(is_write_op(op), write); assert!(payload.get("contract_version").is_none());
            let mut injected = raw.clone(); injected["owner"] = json!("CANARY");
            assert!(!normalize(command, injected).unwrap_err().contains("CANARY"));
            let mut missing = raw.clone(); missing.as_object_mut().unwrap().remove(if matches!(op, "builder.approve" | "builder.approval.get") {"approval_id"} else {"conversation_id"});
            assert!(normalize(command, missing).is_err());
            let mut version = raw.clone(); version["contract_version"] = json!(1); assert!(normalize(command, version).is_err());
        }
        for (op, resource) in [("builder.mode.set", json!({"conversation_id":"c"})), ("builder.approve", json!({"approval_id":"a"}))] {
            assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":op,"resource":resource})).is_ok());
            assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":op,"resource":{"project_id":"p"}})).is_err());
        }
        for mode in ["suggest", "approve", "auto", "yolo"] {
            assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":"builder.mode.set","resource":{"conversation_id":"c","requested_mode":mode}})).is_ok());
        }
        for (op, resource) in [
            ("builder.mode.set", json!({"conversation_id":"c","requested_mode":"invalid"})),
            ("builder.mode.set", json!({"conversation_id":"c","requested_mode":true})),
            ("builder.mode.set", json!({"project_id":"p","requested_mode":"auto"})),
            ("builder.approve", json!({"approval_id":"a","requested_mode":"auto"})),
        ] {
            assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":op,"resource":resource})).is_err());
        }
        for decision in ["allow_once", "allow_session", "deny"] {
            assert!(normalize("portal_builder_approve_v2", json!({"contract_version":2,"approval_id":"a","decision":decision,"request_key":"k"})).is_ok());
        }
        for decision in ["approve", "yolo", "", "CANARY"] {
            assert!(normalize("portal_builder_approve_v2", json!({"contract_version":2,"approval_id":"a","decision":decision,"request_key":"k"})).is_err());
        }
    }

    #[test]
    fn builder_yolo_requires_explicit_true_and_revision_is_never_clamped() {
        let mut raw = json!({"contract_version":2,"conversation_id":"c","mode":"yolo","expected_revision":"9007199254740993","request_key":"k"});
        assert!(normalize("portal_builder_mode_set_v2", raw.clone()).unwrap_err().contains("yolo_ack_required"));
        for ack in [json!(false), json!("true"), Value::Null, json!(1)] { raw["yolo_acknowledged"] = ack; assert!(normalize("portal_builder_mode_set_v2", raw.clone()).is_err()); }
        raw["yolo_acknowledged"] = json!(true);
        assert_eq!(normalize("portal_builder_mode_set_v2", raw.clone()).unwrap().2["expected_revision"], "9007199254740993");
        for revision in [json!(-1), json!(0.5), json!(9007199254740992_u64), json!("01"), Value::Null] {
            raw["expected_revision"] = revision; assert!(normalize("portal_builder_mode_set_v2", raw.clone()).is_err());
        }
        for mode in ["suggest", "approve", "auto"] {
            raw["expected_revision"] = json!(0); raw["mode"] = json!(mode); raw.as_object_mut().unwrap().remove("yolo_acknowledged");
            assert!(normalize("portal_builder_mode_set_v2", raw.clone()).is_ok());
        }
    }

    #[test]
    fn builder_preview_paths_and_temp_copies_cannot_execute_workspace_files() {
        for path in ["../x", "/etc/passwd", "a/../x", "a//b", "a/./b", "C:/x", "\\\\server\\x", "a\\b", "a\n"] {
            assert!(normalize("portal_builder_open_temp_copy_v2", json!({"contract_version":2,"conversation_id":"c","path":path})).is_err());
        }
        let preview = |mime: &str, b64: &str, size: u64| json!({"path":"run.exe","mime":mime,"bytes_b64":b64,"size":size,"truncated":false});
        // A text-labelled executable name can only become .txt, never .exe/.ps1.
        assert_eq!(builder_temp_content(&preview("text/plain", "TVouLi4=", 5), "run.exe").unwrap().0, "txt");
        assert!(builder_temp_content(&preview("application/octet-stream", "TVouLi4=", 5), "run.exe").is_err());
        assert!(builder_temp_content(&preview("image/png", "TVouLi4=", 5), "run.exe").is_err());
        let html = builder_temp_content(&preview("text/html", "PHNjcmlwdD4=", 8), "run.exe").unwrap();
        assert_eq!(html.0, "html");
        let content = String::from_utf8(html.1).unwrap();
        assert!(content.contains("sandbox=\"allow-scripts allow-forms allow-modals\"")); assert!(content.contains("frame-src &#39;none&#39;"));
        assert!(!content.contains("<script>")); assert!(content.contains("&lt;script&gt;"));
        let mut invalid = preview("text/plain", "aGk=", 2); invalid["truncated"] = json!(true); assert!(builder_temp_content(&invalid, "run.exe").is_err());
        assert!(builder_temp_content(&preview("text/plain", "aGk=", 3), "run.exe").is_err());
        assert!(builder_temp_content(&preview("text/plain", "aGk=", 2), "other").is_err());
        assert!(builder_temp_content(&preview("image/svg+xml", "aGk=", 2), "run.exe").is_err());
        for bad in ["A", "!!!!", "aGk=AAAA", "Zh==", "Zm9=", "===="] { assert!(builder_decode_base64(bad).is_err(), "{bad}"); }
        assert_eq!(builder_decode_base64("aGk=").unwrap(), b"hi");
        assert_eq!(builder_decode_base64("YWJj").unwrap(), b"abc");
        assert_eq!(builder_decode_base64("Zg==").unwrap(), b"f");
        assert!(builder_decode_base64(&"A".repeat(BUILDER_PREVIEW_CAP.div_ceil(3)*4+4)).is_err());
        assert!(MAX_REPLY > BUILDER_PREVIEW_CAP.div_ceil(3)*4);
    }


    #[test]
    fn external_preview_script_sandbox_csp_and_attribute_escape() {
        let page = "\"><script>parent.document.body.innerHTML='escaped'</script><img onerror=\"alert(1)\" src=\"x\">&";
        let html = builder_external_html(page);
        assert!(html.contains("sandbox=\"allow-scripts allow-forms allow-modals\""));
        assert!(html.contains("referrerpolicy=\"no-referrer\""));
        assert!(!html.contains("allow-same-origin"));
        assert!(!html.contains("<script"));
        assert!(html.contains(&builder_html_escape(PREVIEW_STORAGE_SHIM)));
        assert!(html.find(&builder_html_escape(PREVIEW_STORAGE_SHIM)).unwrap() < html.find(&builder_html_escape(page)).unwrap());
        for token in ["allow-popups", "allow-downloads", "allow-top-navigation"] { assert!(!html.contains(token)); }
        assert!(html.contains("&quot;&gt;&lt;script&gt;"));
        assert!(html.contains("&amp;"));
        for directive in ["default-src 'none'", "script-src 'unsafe-inline'", "connect-src 'none'", "img-src data:", "style-src 'unsafe-inline'", "font-src 'none'", "media-src 'none'", "frame-src 'none'", "object-src 'none'", "base-uri 'none'", "form-action 'none'"] {
            assert!(html.contains(&builder_html_escape(directive)), "{directive}");
        }
        assert_eq!(RUNNABLE_EXTERNAL_PREVIEW_CSP, "default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src 'none'; media-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'");
        assert!(builder_external_html("<!doctype html><html><script>localStorage.x=1</script>").contains("&lt;!doctype html&gt;&lt;script&gt;"));
        assert_eq!(RUNNABLE_EXTERNAL_PREVIEW_CSP.split(';').count(), 11);
    }

    #[test]
    fn recovery_requires_explicit_reconnection_before_any_further_dispatch(){let state=PortalState::default();state.invalidate_after_recovery();for write in [false,true]{let error=state.ensure(false,write).err().expect("no implicit broker spawn");let value:Value=serde_json::from_str(&error).unwrap();assert_eq!(value["code"],if write{"not_dispatched"}else{"transport_unavailable"});}}
    #[test]
    fn recovery_ipc_accepts_only_choices_handles_and_safe_actions(){
        for (command,args) in [
            ("portal_recovery_start_v2",json!({"contract_version":2,"pair":"setup","printed":false})),
            ("portal_recovery_act_v2",json!({"contract_version":2,"handle":"opaque-1","action":"check"})),
            ("portal_recovery_status_v2",json!({"contract_version":2})),
            ("portal_recovery_capability_v2",json!({"contract_version":2})),
        ] {assert!(PORTAL_COMMANDS.contains(&command));assert!(validate_recovery_args(command,&args).is_ok());
            for field in ["password","new_seed","secret","path","request_key","challenge_id","owner","material_saved"]{let mut extra=args.clone();extra[field]=json!("CANARY");let error=validate_recovery_args(command,&extra).unwrap_err();assert!(!error.contains("CANARY"));}
            for v in [json!(true),json!(1),json!("2"),Value::Null]{let mut old=args.clone();old["contract_version"]=v;assert!(validate_recovery_args(command,&old).is_err());}
        }
        for op in ["auth.factors.prepare","auth.factors.commit","auth.factors.cancel"]{assert!(is_write_op(op));}
        let safe=settings_status_result(json!({"operation_id":"op-1","phase":"ready","result":{"challenge_id":"challenge-1","expires_at":123,"state":"proof_verified","secret":"CANARY"}})).unwrap();assert!(!safe.to_string().contains("CANARY"));
    }

    #[test]
    fn password_v2_has_closed_public_factors_native_keys_and_server_policy() {
        let raw = json!({"current":"old-canary","new":"short","totp":"654321"});
        let (op, long, first) = normalize("portal_auth_change_password_v2", raw.clone()).unwrap();
        let second = normalize("portal_auth_change_password_v2", raw.clone()).unwrap().2;
        assert_eq!(op, "auth.change_password"); assert!(long); assert!(is_write_op(op));
        assert_eq!(first["new"], "short"); // server owns password policy
        assert_eq!(first["contract_version"], 2);
        assert!(safe_settings_id(&first["request_key"])); assert_ne!(first["request_key"], second["request_key"]);
        for invalid in [
            json!({"current":"old-canary","new":"new-canary","totp":"654321","request_key":"chosen"}),
            json!({"current":"old-canary","new":"new-canary","totp":"654321","owner":"forged"}),
            json!({"current":"old-canary","new":"new-canary","totp":"654321","new-canary":true}),
            json!({"current":false,"new":"new-canary","totp":"654321"}),
            json!({"current":"old-canary","new":"x".repeat(4097),"totp":"654321"}),
            json!({"current":"old-canary","new":"new-canary","totp":"65432a"}),
            json!({"current":"old-canary","new":"new-canary"}),
        ] {
            let error = normalize("portal_auth_change_password_v2", invalid).unwrap_err();
            for canary in ["old-canary","new-canary","65432"] { assert!(!error.contains(canary)); }
        }
    }

    #[test]
    fn password_results_and_errors_project_exact_safe_fields() {
        for (changed, restarted) in [(false,false),(true,false),(true,true)] {
            let safe = sensitive_password_result(Ok(json!({"changed":changed,"restarted":restarted,"password":"canary","owner_bearer":"bos1.canary","totp":"654321"})), &json!("request-1")).unwrap();
            assert_eq!(safe, json!({"changed":changed,"restarted":restarted}));
            for phase in ["ready","failed"] {
                let receipt = settings_status_result(json!({"operation_id":"op-1","phase":phase,"result":safe,"password":"canary"})).unwrap();
                assert_eq!(receipt, json!({"operation_id":"op-1","phase":phase,"result":{"changed":changed,"restarted":restarted}}));
            }
        }
        for invalid in [json!({}),json!({"changed":false,"restarted":true}),json!({"changed":1,"restarted":false}),json!({"changed":true})] {
            let error = sensitive_password_result(Ok(invalid), &json!("request-1")).unwrap_err();
            let value: Value = serde_json::from_str(&error).unwrap();
            assert_eq!(value["code"], "invalid_receipt"); assert_eq!(value["request_key"], "request-1");
        }
        for code in ["canary", "timeout_no_reply", "transport_unavailable", "invalid_password", "owner_session_expired", "authentication_throttled"] {
            let error = sensitive_password_result(Err(json!({"code":code,"detail":"canary 654321 bos1.canary","retry_after":30}).to_string()), &json!("request-1")).unwrap_err();
            assert!(!error.contains("canary")); assert!(!error.contains("654321"));
            let safe: Value = serde_json::from_str(&error).unwrap();
            assert_eq!(safe["retry_after"], 30);
            assert_eq!(safe["code"], if code == "canary" {"password_outcome_unknown"} else {code});
        }
        let receipt = settings_status_result(json!({"operation_id":"op-1","phase":"restart-pending","result":{"changed":true,"restarted":true},"totp":"654321"})).unwrap();
        assert_eq!(receipt, json!({"operation_id":"op-1","phase":"restart-pending"}));
    }

    #[test]
    fn password_journal_fences_resend_and_recovers_dropped_commit_reply() {
        let root = double_dir(&format!("password-journal-{}", uuid::Uuid::new_v4()));
        let path = root.join("pending.json");
        let payload = normalize("portal_auth_change_password_v2", json!({"current":"old-canary","new":"new-canary","totp":"654321"})).unwrap().2;
        let key = payload["request_key"].clone();
        let error = submit_password_once(&path, payload.clone(), |_| Err(error_json("timeout_no_reply", "canary"))).unwrap_err();
        assert!(!error.contains("canary"));
        let bytes = std::fs::read_to_string(&path).unwrap();
        for canary in ["old-canary","new-canary","654321"] { assert!(!bytes.contains(canary)); }
        assert_eq!(read_password_pending(&path).unwrap(), json!({"operation":"auth.change_password","status":"unknown","request_key":key}));
        let error = submit_password_once(&path, payload.clone(), |_| panic!("credentials must never be resent")).unwrap_err();
        let safe: Value = serde_json::from_str(&error).unwrap();
        assert_eq!(safe["request_key"], key);
        let receipt = settings_status_result(json!({"operation_id":"op-1","phase":"failed","result":{"changed":true,"restarted":false}})).unwrap();
        reconcile_password_pending(&path, &json!({"request_key":"foreign"}), &receipt);
        assert!(path.exists());
        reconcile_password_pending(&path, &json!({"request_key":key}), &receipt);
        assert_eq!(read_password_pending(&path).unwrap(), Value::Null);
        for result in [
            Ok(json!({"changed":false,"restarted":false})),
            Ok(json!({"changed":true,"restarted":true})),
            Err(error_json("invalid_password", "safe")),
            Err(error_json("owner_session_expired", "safe")),
        ] {
            let _ = submit_password_once(&path, payload.clone(), |_| result);
            assert!(!path.exists(), "terminal outcomes release safe pending metadata");
        }
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn settings_key_ipc_is_closed_native_single_use_and_write_classified() {
        let key = "synthetic-S3e-key";
        let raw = json!({"provider":"configured-provider","key":key});
        let (op, _, first) = normalize("portal_secret_set_v2", raw.clone()).unwrap();
        let second = normalize("portal_secret_set_v2", raw.clone()).unwrap().2;
        assert_eq!(op, "secrets.set"); assert!(is_write_op(op));
        assert_eq!(first["contract_version"], 2); assert_eq!(first["key"], key);
        assert_ne!(first["request_key"], second["request_key"]);
        for extra in ["contract_version", "request_key", "path", "owner"] {
            let mut bad_args = raw.clone(); bad_args[extra] = json!(key);
            let error = normalize("portal_secret_set_v2", bad_args).unwrap_err();
            assert!(!error.contains(key));
        }
        for invalid in ["", "space key", "key\nline"] {
            assert!(normalize("portal_secret_set_v2", json!({"provider":"provider","key":invalid})).is_err());
        }
        for provider in ["glm", "kimi", "qwen"] {
            assert!(normalize("portal_secret_set_v2", json!({"provider":provider,"key":key})).is_ok());
            assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":"secrets.set","resource":{"provider":provider}})).is_ok());
        }
        let receipt = json!({"provider":"configured-provider","stored":true,"fingerprint":"0123456789abcdef","operation_id":"op-1","echo":key});
        let safe = sensitive_settings_result(Ok(receipt.clone()), "configured-provider", &first["request_key"]).unwrap();
        assert_eq!(safe.as_object().unwrap().len(), 4); assert!(!safe.to_string().contains(key));
        let error = sensitive_settings_result(Err(json!({"code":key,"detail":key}).to_string()), "configured-provider", &first["request_key"]).unwrap_err();
        assert!(!error.contains(key)); assert!(error.contains(first["request_key"].as_str().unwrap()));
        for field in ["provider", "stored", "fingerprint", "operation_id"] {
            let mut missing = receipt.clone(); missing.as_object_mut().unwrap().remove(field);
            assert!(secret_receipt(missing, Some("configured-provider")).is_err());
        }
        let mut false_receipt = receipt; false_receipt["stored"] = json!(false);
        assert!(secret_receipt(false_receipt, None).is_err());
    }

    #[test]
    fn settings_receipts_use_v1_safe_lookup_without_owner_or_key() {
        assert!(!is_write_op("settings.status"));
        for field in ["request_key", "operation_id"] {
            let raw = json!({"contract_version":1,field:"safe-id"});
            assert_eq!(normalize("portal_settings_status_v1", raw).unwrap().2, json!({field:"safe-id"}));
        }
        for raw in [json!({"contract_version":2,"request_key":"k"}), json!({"contract_version":1}), json!({"contract_version":1,"request_key":"k","operation_id":"op"}), json!({"contract_version":1,"key":"secret"}), json!({"contract_version":1,"request_key":"../path"})] {
            assert!(normalize("portal_settings_status_v1", raw).is_err());
        }
        let safe = settings_status_result(json!({"operation_id":"op-1","phase":"failed","error":"secret-canary","key":"secret-canary"})).unwrap();
        assert_eq!(safe, json!({"operation_id":"op-1","phase":"failed","error":"settings_failed"}));
        assert!(settings_status_result(json!({"operation_id":"op-1","phase":"ready","result":{"stored":false}})).is_err());
    }

    #[test]
    fn settings_policy_version_revision_scope_and_owner_resources_are_strict() {
        let policy = json!({"posture":"scoped","scope":{"auto_actions":["read"],"escalate_actions":["write"],"may_touch":[{"attachment_id":"registered","path":"src/**"}],"may_not_touch":[]}});
        let raw = json!({"contract_version":2,"policy":policy,"expected_revision":0,"request_key":"policy-key"});
        assert_eq!(normalize("portal_policy_set_v2", raw.clone()).unwrap().2, raw);
        assert!(is_write_op("policy.set")); assert!(!is_write_op("policy.get"));
        assert_eq!(normalize("portal_policy_get_v2", json!({"contract_version":2})).unwrap().2, json!({}));
        for revision in [json!(-1), json!(true), json!(1.5), json!("01")] {
            let mut invalid = raw.clone(); invalid["expected_revision"] = revision;
            assert!(normalize("portal_policy_set_v2", invalid).is_err());
        }
        for path in ["/absolute", "../escape", "src/../escape", "src/*/file", "C:\\private", "src//file", "src/%2e%2e/private"] {
            let mut invalid = raw.clone(); invalid["policy"]["scope"]["may_touch"][0]["path"] = json!(path);
            assert!(normalize("portal_policy_set_v2", invalid).is_err());
        }
        let mut invalid = raw.clone(); invalid["policy"]["scope"]["auto_actions"] = json!(["mint_token"]);
        assert!(normalize("portal_policy_set_v2", invalid).is_err());
        for (op, resource) in [("secrets.set", json!({"provider":"configured"})), ("policy.get", json!({})), ("policy.set", json!({}))] {
            assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":op,"resource":resource})).is_ok());
        }
        assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":"policy.set","resource":{"provider":"configured"}})).is_err());
        for provider in ["UPPER", "../provider", &"p".repeat(65)] {
            assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":"secrets.set","resource":{"provider":provider}})).is_err());
        }
        let mut exact = raw.clone(); exact["expected_revision"] = json!("9223372036854775807");
        assert_eq!(normalize("portal_policy_set_v2", exact.clone()).unwrap().2, exact);
        exact["expected_revision"] = json!(9_007_199_254_740_992u64);
        assert!(normalize("portal_policy_set_v2", exact).is_err());
    }

    #[test]
    fn policy_results_require_authoritative_revision_scope_and_mutation_receipt() {
        let initial = json!({"policy":null,"revision":0,"immutable_restrictions":{"scope_intersection":true}});
        assert_eq!(policy_result(initial.clone(), false).unwrap(), initial);
        assert!(policy_result(initial, true).is_err());
        let result = json!({"policy":{"posture":"scoped","scope":{"auto_actions":["read"],"escalate_actions":[],"may_touch":["/server/root/src/**"],"may_not_touch":[]}},"revision":"9007199254740993","immutable_restrictions":{},"operation_id":"op-1","paused_runs":["run-1"]});
        assert_eq!(policy_result(result.clone(), true).unwrap(), result);
        for field in ["policy", "revision", "immutable_restrictions", "operation_id", "paused_runs"] {
            let mut missing = result.clone(); missing.as_object_mut().unwrap().remove(field);
            assert!(policy_result(missing, true).is_err());
        }
        let mut invalid = result.clone(); invalid["policy"]["scope"]["may_touch"] = json!([{"attachment_id":"not-an-effective-path","path":"src/**"}]);
        assert!(policy_result(invalid, false).is_err());
        let mut extra = result.clone(); extra["unexpected"] = json!("untrusted");
        assert_eq!(policy_result(extra, true).unwrap(), result);
    }

    fn v2_start() -> Value {
        json!({"contract_version": 2, "team": "configured-team", "team_revision": 3,
               "task": "supplied evidence", "request_key": "keep-across-reconnect",
               "budget": {"start_calls": 1000, "wall_mode": "none", "wall_seconds": null}})
    }

    #[test]
    fn execution_v2_preserves_allowance_and_null_without_clamping() {
        for calls in [1, 10, 50, 100, 200, 1000] {
            let mut raw = v2_start(); raw["budget"]["start_calls"] = json!(calls);
            let (op, long, normalized) = normalize("portal_runs_start_v2", raw.clone()).unwrap();
            assert_eq!(op, "runs.start"); assert!(long); assert_eq!(normalized, raw);
        }
        for mode in ["fixed", "follow_budget"] {
            let mut raw = v2_start(); raw["budget"] = json!({"start_calls": 100, "wall_mode": mode, "wall_seconds": 72000});
            assert_eq!(normalize("portal_runs_start_v2", raw.clone()).unwrap().2, raw);
        }
    }

    #[test]
    fn execution_v2_rejects_invalid_budgets_and_versions_before_dispatch() {
        for calls in [json!(0), json!(-1), json!(1.5), json!(true), json!("050"), Value::Null] {
            let mut raw = v2_start(); raw["budget"]["start_calls"] = calls;
            assert!(normalize("portal_runs_start_v2", raw).is_err());
        }
        for (mode, seconds) in [("none", json!(900)), ("fixed", Value::Null), ("follow_budget", json!(0)),
            ("fixed", json!(2.5)), ("fixed", json!(true)), ("fixed", json!("060")), ("bad", Value::Null)] {
            let mut raw = v2_start(); raw["budget"]["wall_mode"] = json!(mode); raw["budget"]["wall_seconds"] = seconds;
            assert!(normalize("portal_runs_start_v2", raw).is_err());
        }
        for version in [Value::Null, json!(true), json!(1), json!("2")] {
            let mut raw = v2_start(); raw["contract_version"] = version;
            assert!(normalize("portal_runs_start_v2", raw).is_err());
        }
        let mut raw = v2_start(); raw["max_model_calls"] = json!(4);
        assert!(normalize("portal_runs_start_v2", raw).is_err());
    }

    #[test]
    fn execution_v2_team_create_preserves_null_revision_and_caller_key() {
        let raw = json!({"contract_version":2, "definition":{"id":"new-team"}, "expected_revision":null, "request_key":"same-key"});
        assert_eq!(normalize("portal_teams_save_v2", raw.clone()).unwrap().2, raw);
        assert!(is_write_op("teams.save"));
        for command in ["portal_teams_list_v2", "portal_teams_save_v2", "portal_runs_start_v2", "portal_runs_list_v2", "portal_run_get_v2", "portal_run_cancel_v2"] {
            assert!(PORTAL_COMMANDS.contains(&command)); assert!(op_for(command).is_some());
        }
        let (_, _, legacy) = normalize("portal_runs_start", json!({"team":"legacy", "task":"evidence", "max_model_calls":1000,"max_seconds":72000})).unwrap();
        assert_eq!(legacy["max_model_calls"], json!(4)); assert_eq!(legacy["max_seconds"], json!(900));
    }

    #[test]
    fn owner_v2_commands_preserve_partial_budgets_revisions_and_large_cursors() {
        for budget in [json!({}), json!({"start_calls":100}), json!({"wall_mode":"none","wall_seconds":null}),
            json!({"start_calls":1000,"wall_mode":"fixed","wall_seconds":72000})] {
            let raw = json!({"contract_version":2,"run_id":"run-1","expected_revision":5,"request_key":"same-key","budget":budget});
            let (op, long, args) = normalize("portal_run_resume_v2", raw.clone()).unwrap();
            assert_eq!(op, "runs.resume"); assert!(long); assert!(is_write_op(op)); assert_eq!(args, raw);
        }
        let raw = json!({"contract_version":2,"run_id":"run-1","expected_revision":5,"request_key":"same-key"});
        assert_eq!(normalize("portal_run_resume_v2", raw.clone()).unwrap().2, raw);
        for revision in [Value::Null, json!(4)] {
            let raw = json!({"contract_version":2,"project_id":"project-1","expected_revision":revision,"request_key":"same-key","call_limit":1000});
            let (op, _, args) = normalize("portal_budget_set_v2", raw.clone()).unwrap();
            assert_eq!(op, "budgets.set"); assert!(is_write_op(op)); assert_eq!(args, raw);
        }
        let raw = json!({"contract_version":2,"run_id":"run-1","receipt_cursor":1200});
        assert_eq!(normalize("portal_run_get_v2", raw.clone()).unwrap().2, raw);
        for command in ["portal_run_resume_v2", "portal_budget_set_v2"] { assert!(PORTAL_COMMANDS.contains(&command)); }
    }

    #[test]
    fn owner_v2_validation_refuses_invalid_fields_without_clamping() {
        let resume = json!({"contract_version":2,"run_id":"run-1","expected_revision":5,"request_key":"same-key","budget":{"start_calls":100}});
        let pool = json!({"contract_version":2,"project_id":"project-1","expected_revision":null,"request_key":"same-key","call_limit":100});
        for (command, base) in [("portal_run_resume_v2", &resume), ("portal_budget_set_v2", &pool)] {
            for value in [json!(true), json!(0), json!(-1), json!(1.5), json!("02"), json!(9223372036854775808_u64)] {
                let mut raw = base.clone(); raw["expected_revision"] = value.clone();
                assert!(normalize(command, raw).is_err());
                let mut raw = base.clone();
                if command == "portal_run_resume_v2" { raw["budget"]["start_calls"] = value; }
                else { raw["call_limit"] = value; }
                assert!(normalize(command, raw).is_err());
            }
            for field in ["contract_version", "request_key", "expected_revision"] {
                let mut raw = base.clone(); raw.as_object_mut().unwrap().remove(field);
                assert!(normalize(command, raw).is_err());
            }
            let mut raw = base.clone(); raw["approval"] = json!(true); assert!(normalize(command, raw).is_err());
        }
        let mut raw = resume.clone(); raw["expected_revision"] = Value::Null; assert!(normalize("portal_run_resume_v2", raw).is_err());
        let mut raw = pool.clone(); raw["call_limit"] = Value::Null; assert!(normalize("portal_budget_set_v2", raw).is_err());
        for budget in [Value::Null, json!({"start_calls":null}), json!({"wall_mode":"fixed"}), json!({"wall_seconds":900}),
            json!({"wall_mode":"none","wall_seconds":900}), json!({"wall_mode":"fixed","wall_seconds":true}),
            json!({"wall_mode":"fixed","wall_seconds":0}), json!({"extra":true})] {
            let mut raw = resume.clone(); raw["budget"] = budget; assert!(normalize("portal_run_resume_v2", raw).is_err());
        }
    }

    #[test]
    fn owner_session_commands_are_versioned_strict_and_write_classified() {
        for name in ["begin", "confirm", "status", "lock", "logout"] {
            let command = format!("portal_owner_{name}_v2");
            assert!(PORTAL_COMMANDS.contains(&command.as_str()));
            let mut args = json!({"contract_version":2});
            if name == "begin" { args["operation"] = json!("runs.resume"); args["resource"] = json!({"run_id":"r"}); }
            if name == "confirm" { args["challenge_id"] = json!("c"); args["password"] = json!("synthetic-password"); args["totp"] = json!("123456"); }
            let (op, _, result) = normalize(&command, args.clone()).unwrap();
            assert_eq!(is_write_op(op), name != "status");
            if matches!(name, "status" | "lock" | "logout") { assert_eq!(result, json!({})); }
            let mut extra = args.clone(); extra["owner"] = json!("foreign"); assert!(normalize(&command, extra).is_err());
            args["contract_version"] = json!(1); assert!(normalize(&command, args).is_err());
        }
        for code in ["12345", "1234567", "12x456", "１２３４５６"] {
            let args = json!({"contract_version":2,"challenge_id":"c","password":"synthetic-password","totp":code});
            let error = normalize("portal_owner_confirm_v2", args).unwrap_err();
            assert!(!error.contains("synthetic-password")); assert!(!error.contains(code));
        }
        for password in [String::new(), "x".repeat(1025), "é".repeat(513)] {
            assert!(normalize("portal_owner_confirm_v2", json!({"contract_version":2,"challenge_id":"c","password":password,"totp":"123456"})).is_err());
        }
    }

    #[test]
    fn signed_in_owner_confirmation_is_explicit_and_excludes_factors() {
        let args = json!({"contract_version":2,"challenge_id":"c","confirmation_mode":"signed_in_user"});
        assert!(normalize("portal_owner_confirm_v2", args.clone()).is_ok());
        for (field, value) in [("password", json!("unused")), ("totp", json!("123456")), ("confirmation_mode", json!("other"))] {
            let mut mixed = args.clone(); mixed[field] = value;
            assert!(normalize("portal_owner_confirm_v2", mixed).is_err());
        }
        assert!(normalize("portal_owner_confirm_v2", json!({"contract_version":2,"challenge_id":"c"})).is_err());
    }

    #[test]
    fn grants_and_budget_commands_keep_explicit_coverage_nulls_and_exact_integers() {
        let cases = [
            ("portal_grants_prepare_v2", json!({"contract_version":2,"run_id":"r","expected_revision":"9007199254740993","request_key":"prepare"})),
            ("portal_grants_prepare_v2", json!({"contract_version":2,"request":v2_start(),"request_key":"prepare"})),
            ("portal_grants_issue_v2", json!({"contract_version":2,"proposal_id":"p","proposal_revision":"9007199254740993","expected_revision":null,"request_key":"issue","coverage_kind":"project","allow_long_runs":true,"auto_escalate":false,"call_ceiling":null,"expires_at":null,"allow_extended_jobs":false,"allow_custom_job_limit":false,"job_seconds_ceiling":null})),
            ("portal_grants_list_v2", json!({"contract_version":2,"cursor":"opaque","limit":50})),
            ("portal_grants_get_v2", json!({"contract_version":2,"grant_id":"g"})),
            ("portal_grants_revoke_v2", json!({"contract_version":2,"grant_id":"g","expected_revision":"9223372036854775807","request_key":"revoke"})),
            ("portal_budgets_get_v2", json!({"contract_version":2,"project_id":"p"})),
            ("portal_budgets_list_v2", json!({"contract_version":2,"cursor":"opaque","limit":50})),
            ("portal_budgets_remove_v2", json!({"contract_version":2,"project_id":"p","expected_revision":"9007199254740993","request_key":"remove"})),
        ];
        for (command, args) in cases {
            assert!(PORTAL_COMMANDS.contains(&command));
            assert_eq!(normalize(command, args.clone()).unwrap().2, args);
            let mut extra = args.clone(); extra["bearer"] = json!("forbidden"); assert!(normalize(command, extra).is_err());
            let mut old = args.clone(); old["contract_version"] = json!(1); assert!(normalize(command, old).is_err());
        }
        for value in [json!("9007199254740993"), json!("9223372036854775807")] {
            let mut start = v2_start(); start["budget"]["start_calls"] = value.clone(); start["intent_id"] = json!("server-intent");
            assert_eq!(normalize("portal_runs_start_v2", start.clone()).unwrap().2, start);
            let pool = json!({"contract_version":2,"project_id":"p","expected_revision":null,"call_limit":value,"request_key":"set"});
            assert_eq!(normalize("portal_budget_set_v2", pool.clone()).unwrap().2, pool);
        }
        for value in [json!(9007199254740992_u64), json!("01"), json!("+1"), json!("1e3"), json!("9223372036854775808"), json!(true)] {
            assert!(!wire_integer(&value, 1, i64::MAX as u64));
        }
        let mut issue = json!({"contract_version":2,"proposal_id":"p","proposal_revision":1,"expected_revision":null,"request_key":"issue","coverage_kind":"task","allow_long_runs":true,"auto_escalate":false,"call_ceiling":null,"expires_at":null});
        for field in ["call_ceiling", "expires_at", "auto_escalate", "coverage_kind"] {
            let mut missing = issue.clone(); missing.as_object_mut().unwrap().remove(field); assert!(normalize("portal_grants_issue_v2", missing).is_err());
        }
        issue["allow_extended_jobs"] = json!(true); assert!(normalize("portal_grants_issue_v2", issue).is_ok());
        let err = reply_result(json!({"ok":false,"error":{"code":"gateway_http_429","detail":"safe","retry_after":30,"password":"never-project"}})).unwrap_err();
        let parsed: Value = serde_json::from_str(&err).unwrap(); assert_eq!(parsed["retry_after"], 30); assert!(!err.contains("never-project"));
    }

    #[test]
    fn gl19_rate_limits_preserve_refusal_and_safe_retry_metadata() {
        for code in ["rate_limited", "gateway_http_429"] {
            let err = reply_result(json!({"ok":false,"error":{"code":code,"applied":false,"retry_after_seconds":7,"detail":"rate limit exceeded","password":"never-project"}})).unwrap_err();
            let parsed: Value = serde_json::from_str(&err).unwrap();
            assert_eq!(parsed["applied"], false);
            assert_eq!(parsed["retry_after_seconds"], 7);
            assert!(!hint_for(code).contains("unknown"));
            assert!(!err.contains("never-project"));
            for safe in [sensitive_password_result(Err(err.clone()), &json!("request-1")), sensitive_settings_result(Err(err.clone()), "provider", &json!("request-1"))] {
                let safe: Value = serde_json::from_str(&safe.unwrap_err()).unwrap();
                assert_eq!(safe["code"], "rate_limited");
                assert_eq!(safe["applied"], false);
                assert_eq!(safe["retry_after_seconds"], 7);
                assert!(password_refused(safe["code"].as_str().unwrap()));
            }
        }
        assert!(hint_for("gateway_http_500").contains("unknown"));
    }

    #[test]
    fn job_ladder_fields_survive_native_ipc_without_rounding_or_narrowing() {
        let mut start = v2_start();
        start["budget"]["max_job_attempts"] = json!("9007199254740993");
        start["budget"]["job_seconds_budget"] = json!("9223372036854775807");
        start["job_coverage"] = json!({"profiles":{"acceptance":"a".repeat(64)},"workspaces":{"workspace-1":[".","src"]},"attachments":["a"],"max_job_attempts":"9007199254740993","job_seconds_budget":"9223372036854775807","allow_cli":false});
        assert_eq!(normalize("portal_runs_start_v2", start.clone()).unwrap().2, start);
        let prepare = json!({"contract_version":2,"request":start,"request_key":"same-prepare"});
        assert_eq!(normalize("portal_grants_prepare_v2", prepare.clone()).unwrap().2, prepare);
        for value in [json!(true), json!(0), json!(1.5), json!("01"), json!(9007199254740992_u64), json!("9223372036854775808")] {
            let mut bad = start.clone(); bad["budget"]["max_job_attempts"] = value.clone();
            assert!(normalize("portal_runs_start_v2", bad).is_err());
            let mut bad = start.clone(); bad["job_coverage"]["max_job_attempts"] = value;
            assert!(normalize("portal_runs_start_v2", bad).is_err());
        }
        let mut bad = start.clone(); bad["job_coverage"]["workspaces"]["workspace-1"] = json!(["../outside"]);
        assert!(normalize("portal_runs_start_v2", bad).is_err());
        let mut bad = start.clone(); bad["job_coverage"]["allow_cli"] = json!(true);
        assert!(normalize("portal_runs_start_v2", bad).is_err());
        let issue = json!({"contract_version":2,"proposal_id":"p","proposal_revision":"9007199254740993","expected_revision":null,"request_key":"same-issue","coverage_kind":"task","allow_long_runs":true,"auto_escalate":false,"call_ceiling":null,"expires_at":null,"allow_extended_jobs":true,"allow_custom_job_limit":true,"job_seconds_ceiling":"9223372036854775807","job_override_seconds":7200,"job_auto_escalate":false});
        assert_eq!(normalize("portal_grants_issue_v2", issue.clone()).unwrap().2, issue);
        for field in ["allow_extended_jobs", "allow_custom_job_limit", "job_auto_escalate"] {
            let mut bad = issue.clone(); bad[field] = json!(1); assert!(normalize("portal_grants_issue_v2", bad).is_err());
        }
        let mut bad = issue; bad["job_override_seconds"] = json!("1e3");
        assert!(normalize("portal_grants_issue_v2", bad).is_err());
    }

    #[test]
    fn flag_commands_keep_exact_cursors_revisions_and_owner_action_boundary() {
        let cases = [
            ("portal_flags_list_v2", json!({"contract_version":2,"run_id":"r","cursor":"opaque","limit":50,"status":"all"})),
            ("portal_flags_get_v2", json!({"contract_version":2,"flag_id":"f","after_event_seq":"9007199254740993","job_cursor":"execution-1"})),
            ("portal_flags_resolve_v2", json!({"contract_version":2,"flag_id":"f","expected_revision":"9223372036854775807","request_key":"same-resolution","decision":"resume_with_guidance","guidance":"Separate resume must revalidate authority."})),
        ];
        for (command, raw) in cases {
            assert!(PORTAL_COMMANDS.contains(&command));
            let (op, long, args) = normalize(command, raw.clone()).unwrap();
            assert_eq!(args, raw); assert!(long); assert_eq!(is_write_op(op), op == "flags.resolve");
            let mut old = raw.clone(); old["contract_version"] = json!(1); assert!(normalize(command, old).is_err());
            let mut extra = raw.clone(); extra["owner_bearer"] = json!("never-forward"); assert!(normalize(command, extra).is_err());
        }
        let begin = json!({"contract_version":2,"operation":"flags.resolve","resource":{"flag_id":"f"}});
        assert_eq!(normalize("portal_owner_begin_v2", begin.clone()).unwrap().2, begin);
        let base = json!({"contract_version":2,"flag_id":"f","expected_revision":1,"request_key":"same-resolution","decision":"inspected","guidance":"Inspected receipts"});
        for (field, value) in [("guidance",json!(" ")), ("guidance",json!("x".repeat(4001))), ("expected_revision",json!("01")), ("decision",json!("resume")), ("flag_id",json!(""))] {
            let mut raw = base.clone(); raw[field] = value; assert!(normalize("portal_flags_resolve_v2", raw).is_err());
        }
        let mut raw = base; raw.as_object_mut().unwrap().remove("expected_revision");
        assert!(normalize("portal_flags_resolve_v2", raw).is_err());
        assert!(normalize("portal_flags_list_v2", json!({"contract_version":2,"limit":51})).is_err());
        assert!(normalize("portal_flags_get_v2", json!({"contract_version":2,"flag_id":"f","after_event_seq":9007199254740992_u64})).is_err());
    }

    fn example_connection() -> Connection {
        resolve_connection(&json!({"connection":{"user":"bob","host":"example-server","broker":"/opt/bob/portal-broker.py"}}), Default::default()).unwrap()
    }

    #[test]
    fn unconfigured_connection_has_no_server_or_targets() {
        let connection = resolve_connection(&json!({}), Default::default()).unwrap();
        assert!(connection.targets().is_empty());
        assert!(connection.user.is_empty() && connection.host.is_empty() && connection.broker.is_empty());
        assert_eq!(connection.sources, ["default"; 5]);
        assert!(!connection.configured);
        let error = connection.require_server().unwrap_err();
        for field in ["SSH user", "server host", "broker path"] { assert!(error.contains(field)); }
        let saved = example_connection();
        assert!(saved.require_server().is_ok());
        assert_eq!(ssh_args("example-server", &saved), vec![
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
            "bob@example-server", "python3", "-u", "/opt/bob/portal-broker.py", "--portal-approvals",
        ]);
    }

    #[test]
    fn configured_connection_requires_settings_or_environment_not_just_a_profile() {
        assert_eq!(resolve_connection(&json!({"active": "bob_team"}), Default::default()).unwrap().payload()["connection_configured"], false);
        for document in [json!({"connection": {"mode": "server"}}), json!({"connection": {"mode": "this_pc", "lane": "dad"}})] {
            assert_eq!(resolve_connection(&document, Default::default()).unwrap().payload()["connection_configured"], true);
        }
        for index in 0..5 {
            let mut env: [Option<String>; 5] = Default::default();
            env[index] = Some(["dad", "server", "/broker.py", "server", "dad"][index].into());
            assert_eq!(resolve_connection(&json!({}), env).unwrap().payload()["connection_configured"], true);
        }
    }

    #[test]
    fn connection_precedence_is_per_field_and_reports_sources() {
        let document = json!({"connection": {"user": "saved-user", "host": "saved-host", "broker": "/saved.py"}});
        let saved = resolve_connection(&document, [None, None, None, None, None]).unwrap();
        assert_eq!(saved.sources, ["settings", "settings", "settings", "default", "default"]);
        assert_eq!(saved.targets(), vec!["saved-host"]);
        assert_eq!(saved.user, "saved-user");
        assert_eq!(saved.broker, "/saved.py");
        let env = resolve_connection(&document, [Some("env-user".into()), Some("env-host".into()), Some("/env.py".into()), None, None]).unwrap();
        assert_eq!(env.payload(), json!({
            "connection_configured": true,
            "user": {"value": "env-user", "source": "env"},
            "host": {"value": "env-host", "source": "env"},
            "broker": {"value": "/env.py", "source": "env"},
            "mode": {"value": "server", "source": "default"},
            "lane": {"value": "", "source": "default"},
        }));
        let mixed = resolve_connection(&json!({"connection": {"user": "saved-user"}}), [None, Some("env-host".into()), None, None, None]).unwrap();
        assert_eq!(mixed.sources, ["settings", "env", "default", "default", "default"]);
        assert_eq!(ssh_args("env-host", &mixed)[6], "saved-user@env-host");
    }

    #[test]
    fn this_pc_windows_argv_is_exact_with_system_root_and_distro_override() {
        let command = runtime_command(true, r"C:\Windows", "dad", None, None, None, false).unwrap();
        assert_eq!(command.program, r"C:\Windows\System32\wsl.exe");
        assert_eq!(command.args, ["-d", "BoB", "-u", "bobapp", "--exec", "/usr/local/bin/bob-broker", "dad"]);
        let custom = runtime_command(true, r"D:\Windows Root\", "lane-2", Some("BoB.test_2"), None, None, false).unwrap();
        assert_eq!(custom.program, r"D:\Windows Root\System32\wsl.exe");
        assert_eq!(custom.args, ["-d", "BoB.test_2", "-u", "bobapp", "--exec", "/usr/local/bin/bob-broker", "lane-2"]);
    }

    #[test]
    fn this_pc_linux_argv_is_exact_and_engine_override_wins_over_path() {
        for (on_path, override_engine, expected) in [
            (true, None, "podman"), (false, None, "docker"),
            (true, Some("docker"), "docker"), (false, Some("podman"), "podman"),
        ] {
            let command = runtime_command(false, "", "dad", None, override_engine, None, on_path).unwrap();
            assert_eq!(command.program, expected);
            assert_eq!(command.args, ["exec", "-i", "-u", "bobapp", "bob-runtime", "bob-broker", "dad"]);
        }
        let custom = runtime_command(false, "", "lane-2", None, Some("docker"), Some("BoB.test_2"), true).unwrap();
        assert_eq!(custom.args, ["exec", "-i", "-u", "bobapp", "BoB.test_2", "bob-broker", "lane-2"]);
    }

    #[test]
    fn invalid_runtime_lane_names_and_engine_are_refused() {
        for lane in ["", "Dad", "1dad", "-dad", "dad_name", "dad.name", "dad name", "dad;id", "dad\n", "é", &"a".repeat(33)] {
            for windows in [false, true] {
                assert!(runtime_command(windows, r"C:\Windows", lane, None, None, None, false).is_err(), "{lane:?}");
            }
        }
        for valid in ["a", "lane-2", &"a".repeat(32)] { assert!(valid_lane(valid)); }
        for name in ["", "bad name", "bad/name", "x;id", "x\n", "é", &"a".repeat(65)] {
            assert!(runtime_command(true, r"C:\Windows", "dad", Some(name), None, None, false).is_err(), "{name:?}");
            assert!(runtime_command(false, "", "dad", None, None, Some(name), false).is_err(), "{name:?}");
        }
        for valid in ["A", "BoB.test_2-3", &"a".repeat(64)] { assert!(valid_runtime_name(valid)); }
        for engine in ["", "nerdctl", "Podman", "/usr/bin/podman", "docker exec", "docker;id"] {
            assert!(runtime_command(false, "", "dad", None, Some(engine), None, false).is_err(), "{engine:?}");
        }
    }

    #[test]
    fn mode_and_lane_precedence_sources_and_validation() {
        let document = json!({"connection": {"mode": "this_pc", "lane": "saved-lane"}});
        let saved = resolve_connection(&document, Default::default()).unwrap();
        assert_eq!(saved.mode, "this_pc");
        assert_eq!(saved.targets(), ["saved-lane"]);
        assert_eq!(saved.sources, ["default", "default", "default", "settings", "settings"]);
        let env = resolve_connection(&document, [None, None, None, Some("server".into()), Some("env-lane".into())]).unwrap();
        assert_eq!(env.payload()["mode"], json!({"value": "server", "source": "env"}));
        assert_eq!(env.payload()["lane"], json!({"value": "env-lane", "source": "env"}));
        let mixed = resolve_connection(&document, [None, None, None, None, Some("env-lane".into())]).unwrap();
        assert_eq!(mixed.mode, "this_pc");
        assert_eq!(mixed.targets(), ["env-lane"]);
        assert!(resolve_connection(&json!({"connection": {"mode": "this_pc"}}), Default::default()).is_err());
        for (field, invalid) in [("mode", json!("local")), ("mode", json!(null)), ("lane", json!("Bad")), ("lane", json!(2))] {
            let mut document = document.clone();
            document["connection"][field] = invalid;
            assert!(resolve_connection(&document, [None, None, None, Some("server".into()), Some("valid".into())]).is_err());
        }
        for (mode, lane) in [("bad", "dad"), ("this_pc", ""), ("this_pc", " dad"), ("server", "Dad")] {
            assert!(resolve_connection(&json!({}), [None, None, None, Some(mode.into()), Some(lane.into())]).is_err());
        }
    }

    #[test]
    fn transport_error_context_is_actionable_and_never_suggests_ssh_for_this_pc() {
        for windows in [true, false] {
            let command = runtime_command(windows, r"C:\Windows", "dad", None, None, None, false).unwrap();
            let error = with_transport_context(error_json(End::Eof.code(), End::Eof.detail()), &command.context);
            assert!(error.contains(if windows { "WSL distro is installed" } else { "runtime container is running" }));
            assert!(error.contains("lane exists"));
            assert!(!error.contains("ssh"));
            assert_eq!(with_transport_context(error.clone(), &command.context), error);
        }
        let connection = resolve_connection(&json!({"connection": {"user": "configured-user"}}), Default::default()).unwrap();
        let error = with_transport_context(error_json("transport_unavailable", "cannot start ssh"), &connection.ssh_context("server"));
        assert!(error.contains("ssh configured-user@server"));
        assert!(!error.contains("bob@"));
    }

    #[test]
    fn this_pc_transport_context_reaches_spawn_errors_pending_calls_and_fault_events() {
        // TRANSPORT_SIMULATED: no WSL/container is started, only the JSONL double.
        let context = runtime_command(false, "", "dad", None, None, None, false).unwrap().context;
        let (sink, _events) = sink_channel();
        let root = double_dir("missing-runtime");
        let error = Link::spawn_argv(
            root.join("missing-executable").to_str().unwrap(), &[], "this_pc:dad", &context,
            &root, sink, DEADLINE, LONG_DEADLINE,
        ).err().expect("missing executable must fail");
        assert!(error.contains(&context));
        assert!(!error.contains("ssh"));
        std::fs::remove_dir_all(root).unwrap();
        let (link, frames) = spawn_double_with_context("this-pc-fault", DEADLINE, LONG_DEADLINE, &context);
        link.arm();
        let error = link.call("crash", json!({}), false).unwrap_err();
        let value: Value = serde_json::from_str(&error).unwrap();
        assert_eq!(value["code"], "transport_unavailable");
        assert!(value["detail"].as_str().unwrap().starts_with(&context));
        assert!(!error.contains("ssh"));
        let frame: Value = serde_json::from_str(&frames.recv_timeout(Duration::from_secs(5)).unwrap()).unwrap();
        assert_eq!(frame["data"]["detail"], value["detail"]);
        let later = link.call("ping", json!({}), false).unwrap_err();
        assert!(later.contains(&context));
        assert!(!later.contains("ssh"));
    }

    #[test]
    fn this_pc_store_preserves_other_keys_and_disconnects_only_after_valid_save() {
        let root = double_dir("this-pc-save");
        let initial = json!({"schema": SETTINGS_SCHEMA, "active": "bob_team", "other": {"keep": true}});
        write_settings_document(&root, &initial).unwrap();
        let before = std::fs::read(settings_path(&root)).unwrap();
        let state = PortalState::default();
        let (link, _events) = spawn_double("this-pc-save-link");
        *state.link.lock().unwrap() = Some(link.clone());
        for invalid in [json!({"mode": "this_pc"}), json!({"mode": "this_pc", "lane": "Bad"}),
            json!({"mode": "this_pc", "lane": null}), json!({"mode": "local", "lane": "dad"}),
            json!({"user": "dad", "host": "server", "broker": "/broker.py"})] {
            assert!(state.set_connection(&root, &invalid).is_err());
            assert_eq!(std::fs::read(settings_path(&root)).unwrap(), before);
            assert!(!link.is_dead());
            assert!(state.link.lock().unwrap().is_some());
        }
        let saved = state.set_connection(&root, &json!({"mode": "this_pc", "lane": "dad", "password": "must-not-store"})).unwrap();
        assert_eq!(saved["mode"], json!({"value": "this_pc", "source": "settings"}));
        assert_eq!(saved["lane"], json!({"value": "dad", "source": "settings"}));
        assert!(link.is_dead());
        assert!(state.link.lock().unwrap().is_none());
        let document = read_settings_document(&root).unwrap();
        assert_eq!(document["connection"], json!({"mode": "this_pc", "lane": "dad"}));
        assert_eq!(document["other"], initial["other"]);
        assert_eq!(read_settings(&root).active, "bob_team");
        write_settings(&root, "bob_server").unwrap();
        assert_eq!(read_settings_document(&root).unwrap()["connection"], document["connection"]);
        state.set_connection(&root, &json!({"mode": "server", "user": "dad", "host": "server", "broker": "/broker.py"})).unwrap();
        assert_eq!(read_connection(&root).unwrap().mode, "server");
        assert_eq!(read_settings_document(&root).unwrap()["other"], initial["other"]);
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 1);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn invalid_saved_connection_is_refused_even_under_env_override() {
        for (field, invalid) in [
            ("user", json!("")), ("user", json!("-option")), ("user", json!("a".repeat(33))),
            ("user", json!("user@host")), ("user", json!("é")), ("user", json!("two words")),
            ("host", json!("host;id")), ("host", json!("")), ("broker", json!("relative.py")),
            ("broker", json!("/a b.py")), ("user", json!(null)), ("host", json!(123)),
        ] {
            let mut document = json!({"connection": {}});
            document["connection"][field] = invalid;
            let error = resolve_connection(&document, [Some("valid".into()), Some("valid".into()), Some("/valid".into()), None, None]).unwrap_err();
            assert!(error.contains(field), "{error}");
        }
        for connection in [json!(null), json!([]), json!("wrong")] {
            assert!(resolve_connection(&json!({"connection": connection}), [None, None, None, None, None]).is_err());
        }
        for user in ["a", "User.name_1-2", &"a".repeat(32)] { assert!(valid_user(user)); }
        assert!(resolve_connection(&json!({}), [Some("".into()), None, None, None, None]).is_err());
    }

    #[test]
    fn connection_store_preserves_profile_and_disconnects_only_after_valid_save() {
        let root = double_dir("connection-save");
        write_settings(&root, "bob_team").unwrap();
        let before = std::fs::read(settings_path(&root)).unwrap();
        let state = PortalState::default();
        let (link, _events) = spawn_double("connection-save-link");
        *state.link.lock().unwrap() = Some(link.clone());
        for invalid in [json!({}), json!({"mode": "server", "user": "-bad", "host": "server", "broker": "/broker.py"}),
            json!({"mode": "server", "user": "dad", "host": "bad host", "broker": "/broker.py"}),
            json!({"mode": "server", "user": "dad", "host": "server", "broker": "/bad;id"})] {
            assert!(state.set_connection(&root, &invalid).is_err());
            assert_eq!(std::fs::read(settings_path(&root)).unwrap(), before);
            assert!(!link.is_dead());
            assert!(state.link.lock().unwrap().is_some());
        }
        let saved = json!({"mode": "server", "user": "dad", "host": "server", "broker": "/broker.py", "password": "must-not-store"});
        state.set_connection(&root, &saved).unwrap();
        assert!(state.link.lock().unwrap().is_none());
        assert!(link.is_dead());
        assert_eq!(read_settings(&root).active, "bob_team");
        let document = read_settings_document(&root).unwrap();
        assert_eq!(document["connection"], json!({"mode": "server", "user": "dad", "host": "server", "broker": "/broker.py"}));
        write_settings(&root, "bob_server").unwrap();
        assert_eq!(read_settings_document(&root).unwrap()["connection"], document["connection"]);
        #[cfg(unix)]
        assert!(settings_are_private(&root, &settings_path(&root)));
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 1, "atomic temporary file was removed");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn malformed_settings_are_refused_without_overwriting_evidence() {
        let root = double_dir("connection-malformed");
        std::fs::write(settings_path(&root), "{broken").unwrap();
        assert!(read_connection(&root).unwrap_err().contains("cannot parse portal settings"));
        assert!(PortalState::default().set_connection(&root, &json!({"mode": "server", "user": "dad", "host": "server", "broker": "/broker.py"})).is_err());
        assert_eq!(std::fs::read_to_string(settings_path(&root)).unwrap(), "{broken");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn privacy_decision_requires_protection_and_only_trusted_explicit_grants() {
        // Construct binary SIDs for a user, SYSTEM, Administrators, and Everyone.
        let user = vec![1, 2, 0, 0, 0, 0, 0, 5, 21, 0, 0, 0, 42, 0, 0, 0];
        let system = vec![1, 1, 0, 0, 0, 0, 0, 5, 18, 0, 0, 0];
        let admins = vec![1, 2, 0, 0, 0, 0, 0, 5, 32, 0, 0, 0, 32, 2, 0, 0];
        let everyone = vec![1, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0];
        let trusted = vec![user.clone(), system.clone(), admins.clone()];
        let allowed = vec![(false, PrivacyAce::Allow(user)), (false, PrivacyAce::Allow(system)), (false, PrivacyAce::Allow(admins))];
        assert!(private_dacl(true, &allowed, &trusted));
        assert!(!private_dacl(false, &allowed, &trusted));
        assert!(!private_dacl(true, &[(false, PrivacyAce::Allow(everyone))], &trusted));
        assert!(!private_dacl(true, &[(true, PrivacyAce::Allow(trusted[0].clone()))], &trusted));
        assert!(!private_dacl(true, &[(true, PrivacyAce::Deny)], &trusted));
        assert!(!private_dacl(true, &[(false, PrivacyAce::Unsupported)], &trusted));
        assert!(private_dacl(true, &[(false, PrivacyAce::Deny)], &trusted));
        assert!(private_dacl(true, &[], &trusted), "empty DACL grants nobody access");
    }

    /// LIVE, opt-in: the app's real this_pc argv + Link against a running Bob runtime
    /// (Linux container `bob-runtime` with lane `BOB_RUNTIME_E2E_LANE`, default `owner`).
    /// Skipped unless BOB_RUNTIME_E2E=1, so ordinary test runs never need a runtime.
    #[cfg(unix)]
    #[test]
    fn live_this_pc_runtime_hello_and_catalog_through_the_real_link() {
        if std::env::var("BOB_RUNTIME_E2E").as_deref() != Ok("1") {
            return;
        }
        let lane = std::env::var("BOB_RUNTIME_E2E_LANE").unwrap_or_else(|_| "owner".into());
        let engine = std::env::var("BOB_RUNTIME_ENGINE").ok();
        let command = runtime_command(false, "", &lane, None, engine.as_deref(), None, false).unwrap();
        let dir = double_dir("live-runtime");
        let (sink, _events) = sink_channel();
        let link = Link::spawn_argv(&command.program, &command.args, "this_pc", &command.context, &dir, sink,
            Duration::from_secs(20), Duration::from_secs(30)).expect("runtime broker must start");
        let hello = link.call("hello", json!({}), false).expect("hello through the runtime broker");
        assert_eq!(hello["identity"]["sub"], json!(lane));
        let catalog = link.call("catalog", json!({}), false).expect("catalog through the runtime broker");
        assert!(catalog["faces"].as_array().is_some_and(|faces| !faces.is_empty()), "{catalog}");
        link.shutdown();
    }

    fn sink_channel() -> (Sink, mpsc::Receiver<String>) {
        let (tx, rx) = mpsc::channel::<String>();
        let sink: Sink = Arc::new(move |line| {
            let _ = tx.send(line);
        });
        (sink, rx)
    }

    fn double_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("bob-portal-test-{}-{}", std::process::id(), tag));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// Spawn the stdio protocol double. TRANSPORT_SIMULATED: in-memory fixture,
    /// no server, no token, no model call.
    fn spawn_double(tag: &str) -> (Arc<Link>, mpsc::Receiver<String>) {
        spawn_double_with(tag, Duration::from_secs(20), Duration::from_secs(30))
    }

    fn spawn_double_with(tag: &str, deadline: Duration, long_deadline: Duration) -> (Arc<Link>, mpsc::Receiver<String>) {
        spawn_double_with_context(tag, deadline, long_deadline, "")
    }

    fn spawn_double_with_context(tag: &str, deadline: Duration, long_deadline: Duration, context: &str) -> (Arc<Link>, mpsc::Receiver<String>) {
        let dir = double_dir(tag);
        let script = dir.join("portal-double.py");
        std::fs::write(&script, include_str!("../tests/support/portal-double.py")).unwrap();
        let (sink, events) = sink_channel();
        let link = Link::spawn_argv(
            if cfg!(windows) { "python" } else { "python3" },
            &["-u".into(), script.to_string_lossy().into_owned()],
            "double",
            context,
            &dir,
            sink,
            deadline,
            long_deadline,
        )
        .expect("python must be available for the simulated transport test");
        (link, events)
    }

    #[cfg(unix)]
    #[test]
    fn linux_data_root_prefers_absolute_xdg_and_falls_back_to_home() {
        assert_eq!(unix_data_root(Some("/private/xdg".into()), Some("/home/owner".into())), PathBuf::from("/private/xdg/bob-portal"));
        for xdg in [None, Some("relative".into()), Some("".into())] {
            assert_eq!(unix_data_root(xdg, Some("/home/owner".into())), PathBuf::from("/home/owner/.local/share/bob-portal"));
        }
    }

    #[cfg(unix)]
    #[test]
    fn linux_settings_are_actually_private_and_reported() {
        use std::os::unix::fs::PermissionsExt;
        let root = double_dir("unix-modes");
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        let settings = write_settings(&root, "bob_server").unwrap();
        assert!(settings.hardened);
        assert_eq!(std::fs::metadata(&root).unwrap().permissions().mode() & 0o777, 0o700);
        assert_eq!(std::fs::metadata(&settings.path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(read_settings(&root).hardened);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn final_link_drop_closes_and_reaps_the_transport() {
        let (sink, _events) = sink_channel();
        let link = Link::spawn_argv("sleep", &["30".into()], "drop-test", "", &double_dir("drop-test"), sink, DEADLINE, LONG_DEADLINE).unwrap();
        let pid = link.child.lock().unwrap().id().unwrap();
        drop(link);
        assert!(!Path::new(&format!("/proc/{pid}")).exists());
    }

    #[test]
    fn broker_paths_cannot_inject_remote_shell_arguments() {
        assert!(valid_broker_path("/opt/bob/portal-broker.py"));
        for path in ["relative.py", "/tmp/a b", "/tmp/a;id", "/tmp/$(id)", "/tmp/a\n"] {
            assert!(!valid_broker_path(path), "{path}");
        }
    }

    #[test]
    fn event_frames_are_forwarded_and_do_not_end_the_link() {
        let transcript = concat!(
            "{\"event\":\"pins\",\"data\":{\"conversation_id\":\"c1\"}}\n",
            "{\"event\":\"chunk\",\"data\":{\"request_id\":\"r1\",\"content\":\"hi\"}}\n",
            "{\"id\":7,\"ok\":true,\"value\":{\"request_id\":\"r1\",\"accepted\":true}}\n",
            "{\"event\":\"complete\",\"data\":{\"request_id\":\"r1\",\"message_id\":\"m1\"}}\n"
        );
        let mut events = Vec::new();
        let mut replies = Vec::new();
        let end = read_loop(
            Cursor::new(transcript.as_bytes().to_vec()),
            |id, value| replies.push((id, value)),
            |line| events.push(line),
        );
        // The link ends only at EOF: an id-less event frame is not a fault.
        assert_eq!(end, End::Eof);
        assert_eq!(events.len(), 3, "every event frame must be forwarded verbatim");
        assert!(events[1].contains("\"chunk\""));
        assert_eq!(replies.len(), 1);
        assert_eq!(replies[0].0, 7);
        assert_eq!(replies[0].1["ok"], json!(true));
    }

    #[test]
    fn frame_without_id_or_event_is_protocol_invalid() {
        assert!(matches!(classify("{\"hello\":1}"), Frame::Invalid(_)));
        assert!(matches!(classify("not json at all"), Frame::Invalid(_)));
        assert!(matches!(classify("{\"id\":3,\"value\":1}"), Frame::Invalid(_)));
        let end = read_loop(
            Cursor::new(b"{\"id\":1,\"ok\":true,\"value\":{}}\n{\"event\":\"chunk\",\"data\":{}}\n{\"oops\":1}\n".to_vec()),
            |_, _| {},
            |_| {},
        );
        assert!(matches!(end, End::Invalid(_)), "a line with neither id nor event must be typed, not ignored");
    }

    #[test]
    fn protocol_double_end_to_end_transport_is_simulated() {
        // TRANSPORT_SIMULATED — the double is an in-memory fixture, never the server.
        let (link, events) = spawn_double("e2e");
        let hello = link.call("hello", json!({}), false).expect("hello reply");
        assert_eq!(hello["identity"]["sub"], json!("dad"));
        assert_eq!(hello["gaps"].as_array().map(|g| g.len()), Some(7));

        let catalog = link.call("catalog", json!({}), false).expect("catalog reply");
        assert!(catalog["faces"].as_array().map(|f| !f.is_empty()).unwrap_or(false));
        let unavailable = catalog["models"]
            .as_array()
            .unwrap()
            .iter()
            .find(|m| m["available"] == json!(false))
            .expect("catalog must carry an honestly unavailable model");
        assert!(unavailable["reason"].as_str().map(|r| !r.is_empty()).unwrap_or(false));

        let listed = link
            .call("conversations.list", json!({"limit": 5, "offset": 0}), false)
            .expect("conversations.list reply");
        assert_eq!(listed["limit"], json!(5));
        let conversation_id = listed["items"][0]["id"].as_str().unwrap().to_string();

        let opened = link
            .call("messages.page", json!({"id": conversation_id, "limit": 10}), false)
            .expect("messages.page reply");
        assert!(opened["items"].as_array().map(|i| !i.is_empty()).unwrap_or(false));

        let accepted = link
            .call(
                "chat.send",
                json!({"conversation_id": conversation_id, "content": "portal transport probe"}),
                true,
            )
            .expect("chat.send reply");
        assert_eq!(accepted["accepted"], json!(true));
        let request_id = accepted["request_id"].as_str().unwrap_or_default().to_string();
        assert!(!request_id.is_empty(), "chat.send must return a request id");

        let mut seen: Vec<String> = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            match events.recv_timeout(Duration::from_millis(500)) {
                Ok(line) => {
                    let frame: Value = serde_json::from_str(&line).expect("event line is JSON");
                    let name = frame["event"].as_str().unwrap_or_default().to_string();
                    assert!(!name.is_empty(), "forwarded frames must be events");
                    let terminal = name == "complete";
                    seen.push(name);
                    if terminal {
                        break;
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
        assert!(seen.contains(&"chunk".to_string()), "chunk events must reach the webview sink: {seen:?}");
        assert!(seen.contains(&"complete".to_string()), "terminal complete event missing: {seen:?}");
        assert!(!link.is_dead(), "event frames must never kill the portal link");

        let pins = link
            .call("chat.pins", json!({"conversation_id": conversation_id}), false)
            .expect("chat.pins reply");
        assert!(pins.get("face_id").is_some());

        let queue = link
            .call("approvals.list", json!({"status": "pending"}), false)
            .expect("approvals.list reply");
        assert_eq!(queue["excluded"], json!(1), "a foreign approval must be excluded and counted");
        let owned_approval = queue["items"][0]["id"].as_str().unwrap().to_string();

        let decided = link
            .call(
                "approvals.decide",
                json!({"approval_id": owned_approval, "decision": "approve"}),
                true,
            )
            .expect("approval decision");
        assert_eq!(decided["decision"], json!("recorded"));
        assert!(decided.get("agent_resume").is_some());

        let stale = link
            .call(
                "approvals.decide",
                json!({"approval_id": owned_approval, "decision": "approve"}),
                true,
            )
            .expect_err("a decided approval must be refused as stale");
        let stale_value: Value = serde_json::from_str(&stale).unwrap();
        assert_eq!(stale_value["code"], json!("approval_stale"));
        assert!(stale_value["hint"].as_str().map(|h| !h.is_empty()).unwrap_or(false));

        let foreign = link
            .call(
                "approvals.decide",
                json!({"approval_id": "appr-foreign-1", "decision": "approve"}),
                true,
            )
            .expect_err("a foreign approval must be refused");
        let foreign_value: Value = serde_json::from_str(&foreign).unwrap();
        assert_eq!(foreign_value["code"], json!("approval_foreign"));

        let run = link
            .call(
                "runs.start",
                json!({"team": "balance", "task": "read-only fixture", "max_model_calls": 2, "max_seconds": 60}),
                true,
            )
            .expect("runs.start reply");
        assert!(run["run"]["receipts"].as_array().map(|r| r.len() == 2).unwrap_or(false));
        assert_eq!(run["run"]["independently_verified"], json!(false));

        link.shutdown();
        assert!(link.is_dead());
    }

    #[test]
    fn a_read_timeout_abandons_one_request_and_keeps_the_link() {
        // The double answers `slow` after 1.5 s; the deadline is 400 ms.
        let (link, _events) = spawn_double_with("slow-read", Duration::from_millis(400), Duration::from_secs(5));
        let started = Instant::now();
        let error = link
            .call("slow", json!({}), false)
            .expect_err("a reply after the deadline must be a typed timeout");
        assert!(started.elapsed() < Duration::from_secs(3), "the deadline must fire, not hang");

        let value: Value = serde_json::from_str(&error).unwrap();
        assert_eq!(value["code"], json!("timeout_no_reply"));
        let detail = value["detail"].as_str().unwrap_or_default();
        assert!(detail.contains("slow"), "the timed-out op must be named: {detail}");
        assert!(
            !detail.contains("outcome unknown"),
            "a read-only timeout must not claim a write outcome is unknown: {detail}"
        );
        assert!(!link.is_dead(), "a request deadline must never kill a shared link under a live turn");

        // The double is single-threaded: the abandoned `slow` reply is written
        // before this ping can be answered. So a pong proves the stale reply was
        // dropped without a fault and the link still serves requests.
        let pong = link
            .call("ping", json!({}), true)
            .expect("the link must still serve requests after an abandoned read");
        assert!(pong.get("ts").is_some());
        assert!(!link.is_dead(), "a late reply must not be a protocol fault");
        link.shutdown();
    }

    #[test]
    fn write_and_fanout_read_wording_matches_the_op() {
        for op in ["chat.send", "chat.stop", "runs.start", "approvals.decide", "memory.save"] {
            assert!(is_write_op(op), "{op} changes server state");
        }
        for op in ["catalog", "projects.list", "messages.page", "conversations.list", "hello", "ping"] {
            assert!(!is_write_op(op), "{op} is a read");
        }
        // Fanout reads and every write get the patient deadline; cheap reads do not.
        for command in [
            "portal_catalog",
            "portal_projects",
            "portal_messages_more",
            "portal_send",
            "portal_stop",
            "portal_runs_start",
        ] {
            let (_, long, _) = op_for(command).expect(command);
            assert!(long, "{command} must not be killed by the 30 s deadline");
        }
        for command in ["portal_hello", "portal_conversations", "portal_run_get", "portal_pins",
                        "portal_runs_list"] {
            let (_, long, _) = op_for(command).expect(command);
            assert!(!long, "{command} stays on the short deadline");
        }
        // runs.list is a pure local ledger read: it must never be treated as a write.
        assert!(!is_write_op("runs.list"));
        let (op, _, keys) = op_for("portal_runs_list").expect("portal_runs_list");
        assert_eq!(op, "runs.list");
        assert_eq!(keys, &["limit", "offset"]);
        let (_, _, clamped) = normalize("portal_runs_list", json!({"limit": 500})).unwrap();
        assert_eq!(clamped["limit"], json!(50), "the ledger page is clamped like every list");
        let (_, _, defaulted) = normalize("portal_runs_list", json!({})).unwrap();
        assert_eq!(defaulted["limit"], json!(20));
    }

    #[test]
    fn runs_accept_one_to_four_model_calls_without_rewriting_them() {
        for calls in [1u64, 2, 3, 4] {
            let (_, _, runs) = normalize(
                "portal_runs_start",
                json!({"team": "balance-budget", "task": "t", "max_model_calls": calls}),
            )
            .unwrap();
            assert_eq!(runs["max_model_calls"], json!(calls), "the client must pass {calls} through unchanged");
        }
        let (_, _, low) = normalize("portal_runs_start", json!({"team": "balance-budget", "task": "t", "max_model_calls": 0})).unwrap();
        assert_eq!(low["max_model_calls"], json!(1), "below the contract floor the client clamps, it does not refuse");
        let (_, _, high) = normalize("portal_runs_start", json!({"team": "balance-budget", "task": "t", "max_model_calls": 9})).unwrap();
        assert_eq!(high["max_model_calls"], json!(4), "the contract caps team runs at 4 model calls");
        let (_, _, seconds) = normalize("portal_runs_start", json!({"team": "balance-budget", "task": "t", "max_seconds": 10})).unwrap();
        assert_eq!(seconds["max_seconds"], json!(30), "max_seconds keeps its contract floor");
    }

    #[test]
    fn a_link_that_ends_reports_exactly_one_typed_transport_fault_frame() {
        // TRANSPORT_SIMULATED: the double exits, so the reader end hits EOF —
        // the path that used to fail the callers and tell the webview nothing.
        let (link, frames) = spawn_double("linkdeath");
        link.arm(); // an accepted link: the app is using it
        let err = link
            .call("crash", json!({}), false)
            .expect_err("a broker that exits mid-request fails its caller");
        let value: Value = serde_json::from_str(&err).unwrap();
        assert_eq!(value["code"], json!(End::Eof.code()));

        let line = frames
            .recv_timeout(Duration::from_secs(5))
            .expect("the webview must be told the link died");
        let frame: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(frame["event"], json!("transport_fault"), "the fault is one frame on the event topic");
        assert_eq!(
            frame["data"]["code"],
            json!(End::Eof.code()),
            "the frame carries the same End-derived code the pending callers got"
        );
        assert!(
            !frame["data"]["detail"].as_str().unwrap_or("").is_empty(),
            "the frame says what happened"
        );
        assert!(
            frames.recv_timeout(Duration::from_millis(500)).is_err(),
            "a link death is reported once, not once per waiting caller"
        );
        assert!(link.is_dead());
        link.shutdown();
    }

    #[test]
    fn a_target_that_never_answered_reports_no_fault() {
        // ensure() walks the configured targets; the first one here dies before
        // answering its ping (on the PC that is the unpinned `example-server` name).
        // The window must not paint a fault over an attempt it never accepted.
        let (link, frames) = spawn_double("unarmed");
        let err = link
            .call("crash", json!({}), false)
            .expect_err("the attempt dies without answering");
        let value: Value = serde_json::from_str(&err).unwrap();
        assert!(!value["code"].as_str().unwrap_or("").is_empty());
        assert!(link.is_dead());
        assert!(
            frames.recv_timeout(Duration::from_millis(600)).is_err(),
            "an unarmed attempt must stay silent; the caller's typed error explains the fallback"
        );
        link.shutdown();
    }

    #[test]
    fn an_intentional_close_reports_no_transport_fault() {
        // Reconnect (and Refresh) shut the old link down before opening the new
        // one. If that emitted transport_fault the webview would paint "link
        // closed" over a link that is about to be live — the regression this
        // test exists to prevent.
        let (link, frames) = spawn_double("intentional");
        link.shutdown();
        assert!(link.is_dead(), "shutdown still kills the link");
        assert!(
            frames.recv_timeout(Duration::from_millis(700)).is_err(),
            "an intentional close must be silent; a real link death is the only case that reports"
        );
    }

    #[test]
    fn broker_exit_fails_pending_and_refuses_later_sends_without_retry() {
        let (link, _events) = spawn_double("crash");
        let first = link.call("crash", json!({}), false);
        assert!(first.is_err(), "a broker that exits mid-request fails its caller");
        let started = Instant::now();
        let later = link.call("hello", json!({}), false).expect_err("a dead link must refuse later sends");
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "a dead link must refuse immediately, not wait for a model deadline"
        );
        let value: Value = serde_json::from_str(&later).unwrap();
        assert_eq!(
            value["code"],
            json!("not_dispatched"),
            "a dead link proves nothing was written: the UI must not treat this as an unknown send outcome"
        );
        assert!(value["detail"].as_str().unwrap_or("").contains("nothing was sent"));
        assert!(value["hint"].as_str().unwrap_or("").contains("Nothing was written"));
        link.shutdown();
    }

    #[test]
    fn oversized_request_is_refused_before_dispatch_and_link_stays_up() {
        let (link, _events) = spawn_double("oversize");
        let huge = "x".repeat(300_000);
        let error = link
            .call("chat.send", json!({"conversation_id": "c1", "content": huge}), false)
            .expect_err("a request over 256 KiB must be refused locally");
        let value: Value = serde_json::from_str(&error).unwrap();
        assert_eq!(value["code"], json!("bad_request"));
        assert!(!link.is_dead(), "a refused local request must not kill the link");
        assert!(link.call("ping", json!({}), false).is_ok());
        link.shutdown();
    }

    #[test]
    fn portal_commands_never_reach_the_wsl_proxy_inventory() {
        let proxy_commands: Vec<String> = serde_json::from_str(include_str!("../../../host/commands.json")).unwrap();
        for command in PORTAL_COMMANDS {
            assert!(!proxy_commands.iter().any(|proxy| proxy == command), "{command} collides with the local proxy contract");
            assert!(command.starts_with("portal_"), "{command} must carry the portal prefix");
            #[cfg(windows)]
            assert!(
                !crate::windows::COMMANDS.contains(command),
                "{command} must not be proxied to the WSL local-runtime host"
            );
        }
        for required in [
            "portal_hello",
            "portal_catalog",
            "portal_connection_get",
            "portal_connection_set",
            "portal_conversations",
            "portal_conversation_new",
            "portal_conversation_open",
            "portal_conversation_rename",
            "portal_conversation_assign_project",
            "portal_messages_more",
            "portal_send",
            "portal_stop",
            "portal_pins",
            "portal_approvals",
            "portal_approval_decide",
            "portal_projects",
            "portal_project_save",
            "portal_memory_status",
            "portal_memory_search",
            "portal_memory_save",
            "portal_runs_start",
            "portal_run_get",
            "portal_run_cancel",
        ] {
            assert!(PORTAL_COMMANDS.contains(&required), "missing briefed command {required}");
        }
    }

    #[test]
    fn a_refused_key_is_never_quoted_back() {
        let key = "sk-test-SECRET-VALUE-1234";
        for args in [
            json!({"provider": "Bad Provider", "key": key}),
            json!({"provider": "openai", "key": format!("{key}\nINJECT=1")}),
            json!({"provider": "openai", "key": format!("{key} {key}")}),
        ] {
            let error = normalize("portal_secret_set", args).unwrap_err();
            assert!(!error.contains("SECRET-VALUE"), "a refusal quoted the key: {error}");
        }
        assert!(normalize("portal_secret_set", json!({"provider": "openai", "key": key})).is_ok());
    }

    #[test]
    fn a_refused_password_change_never_quotes_a_password() {
        let old = "OLD-SECRET-pass";
        let new = "NEW-SECRET-pass-long";
        for args in [
            json!({"current": old, "new": "SECRET-9c", "totp": "123456"}),
            json!({"current": old, "new": old, "totp": "123456"}),
            json!({"current": old, "new": format!("{new}\nX"), "totp": "123456"}),
            json!({"current": old, "new": new, "totp": "12345a"}),
        ] {
            let error = normalize("portal_auth_change_password", args).unwrap_err();
            assert!(!error.contains("SECRET"), "a refusal quoted a password: {error}");
        }
        assert!(normalize("portal_auth_change_password", json!({"current": old, "new": new, "totp": "123456"})).is_ok());
    }

    #[test]
    fn payload_validation_refuses_before_the_wire() {
        assert!(normalize("portal_send", json!({"conversation_id": "c1"})).is_err());
        assert!(normalize("portal_send", json!({"content": "hi"})).is_err());
        assert!(normalize("portal_approval_decide", json!({"approval_id": "a", "decision": "maybe"})).is_err());
        assert!(normalize("portal_memory_save", json!({"content": "x".repeat(12_001)})).is_err());
        assert!(normalize("portal_runs_start", json!({"team": "balance", "task": "t", "max_model_calls": 99})).is_ok());

        let (_, _, send) = normalize(
            "portal_send",
            json!({"conversation_id": "c1", "content": "hi", "bounds": "ignored", "research": true}),
        )
        .unwrap();
        assert_eq!(send["conversation_id"], json!("c1"));
        assert_eq!(send["research"], json!(true));
        assert!(send.get("bounds").is_none(), "unknown fields must not reach the broker");

        let (_, _, runs) =
            normalize("portal_runs_start", json!({"team": "balance", "task": "t", "max_model_calls": 99, "max_seconds": 5000}))
                .unwrap();
        assert_eq!(runs["max_model_calls"], json!(4), "the contract caps team runs at 4 model calls");
        assert_eq!(runs["max_seconds"], json!(900));

        let (_, _, memory) = normalize("portal_memory_read", json!({"source_id": "s1", "max_chars": 999999})).unwrap();
        assert_eq!(memory["max_chars"], json!(12000));

        let (_, _, approvals) = normalize("portal_approvals", json!({})).unwrap();
        assert_eq!(approvals["status"], json!("pending"));
    }

    #[test]
    fn owner_reads_match_shared_fixtures_and_never_become_writes() {
        let fixtures: Vec<Value> = serde_json::from_str(include_str!("../../../tests/owner_read_broker_fixtures.json")).unwrap();
        for fixture in fixtures {
            let op = fixture["op"].as_str().unwrap();
            let command = format!("portal_{}", op.replace('.', "_"));
            assert!(PORTAL_COMMANDS.contains(&command.as_str()));
            assert!(!is_write_op(op));
            let result = normalize(&command, fixture["args"].clone());
            if fixture.get("error").is_some() {
                assert!(result.is_err(), "{}", fixture["name"]);
            } else {
                let (actual_op, long, args) = result.unwrap();
                assert_eq!(actual_op, op);
                assert!(!long);
                let expected = &fixture["call"][2];
                assert_eq!(args, if expected.is_null() { json!({}) } else { expected.clone() });
            }
        }
        for (command, args) in [
            ("portal_memory_list", json!({"cursor": 123})),
            ("portal_memory_list", json!({"kind": "unknown"})),
            ("portal_files_list", json!({"attachment_id": "a", "path": "a//b"})),
            ("portal_files_read", json!({"attachment_id": "a", "path": "a", "offset": true})),
            ("portal_files_read", json!({"attachment_id": "a", "path": "a", "max_bytes": 0})),
        ] { assert!(normalize(command, args).is_err()); }
    }

    #[test]
    fn workspace_job_commands_validate_and_forward_only_contract_keys() {
        assert!(normalize("portal_jobs_start", json!({"role": "manager", "task": "t", "request_key": "k1234567"})).is_err());
        assert!(normalize("portal_jobs_start", json!({"role": "coder", "task": "t"})).is_err(), "start needs a request key");
        assert!(normalize("portal_jobs_start", json!({"role": "coder", "task": "x".repeat(16_385), "request_key": "k1234567"})).is_err());
        assert!(normalize("portal_jobs_decide", json!({"job_id": "j", "decision": "apply", "proposal_digest": "d", "request_key": "k1234567"})).is_err());
        assert!(normalize("portal_jobs_decide", json!({"job_id": "j", "decision": "approve", "request_key": "k1234567"})).is_err());
        assert!(normalize("portal_jobs_audit", json!({})).is_err());

        let (op, long, start) = normalize(
            "portal_jobs_start",
            json!({"role": "coder", "task": "fix", "attachment": "pilot", "request_key": "k1234567",
                   "owner": "other", "workspace": "/etc"}),
        )
        .unwrap();
        assert_eq!((op, long), ("jobs.start", true));
        assert_eq!(start, json!({"role": "coder", "task": "fix", "attachment": "pilot", "request_key": "k1234567"}));
        let (_, _, budget) = normalize(
            "portal_jobs_start",
            json!({"role": "coder", "task": "fix", "request_key": "k1234567", "model_calls": 99}),
        )
        .unwrap();
        assert_eq!(budget["model_calls"], json!(16), "the server maximum bounds the requested budget");
        assert!(budget.get("wall_seconds").is_none(), "absent budget fields are not invented");
        let (op, _, decide) = normalize(
            "portal_jobs_decide",
            json!({"job_id": "j", "decision": "deny", "proposal_digest": "d", "request_key": "k1234567"}),
        )
        .unwrap();
        assert_eq!(op, "jobs.decide");
        assert_eq!(decide["decision"], json!("deny"));
        for op in ["jobs.start", "jobs.cancel", "jobs.decide"] {
            assert!(is_write_op(op), "{op} changes server state");
        }
        for op in ["jobs.list", "jobs.audit", "jobs.proposal", "jobs.attachments", "jobs.artifact"] {
            assert!(!is_write_op(op), "{op} is a read");
        }
        for command in PORTAL_COMMANDS.iter().filter(|c| c.starts_with("portal_jobs_")) {
            assert!(op_for(command).is_some(), "{command} has a broker op");
        }
    }

    #[test]
    fn checked_jobs_require_exact_contract_acceptance_and_finite_attempts() {
        let raw = json!({"contract_version":2,"role":"coder","task":"Edit supplied fixture","attachment":"fixture","request_key":"checked-key","team":"saved-loop","team_revision":"9007199254740993",
            "worker_contract":"checked-edits/1","acceptance":{"criteria":[{"id":"checked-proposal","required":true,"output_kind":"proposal","checks":{"required_check_ids":["files.nonempty"]}}]},
            "budget":{"start_calls":10,"wall_mode":"none","wall_seconds":null,"max_job_attempts":"9007199254740993","job_seconds_budget":"9223372036854775807"},
            "job_policy":{"proposed_seconds":"900","rationale":"Registered checks","auto_escalate":true},
            "job_coverage":{"profiles":{"checked":"d".repeat(64)},"workspaces":{"projects":["fixture"]},"attachments":["fixture"],"max_job_attempts":"9007199254740993","job_seconds_budget":"9223372036854775807","allow_cli":false}});
        let (_, _, got) = normalize("portal_jobs_start_v2", raw.clone()).unwrap();
        assert_eq!(got, raw, "preserve decimal strings, criteria and continuation policy exactly");
        for key in ["worker_contract","acceptance"] {
            let mut bad = raw.clone(); bad.as_object_mut().unwrap().remove(key);
            assert!(normalize("portal_jobs_start_v2", bad).is_err());
        }
        for value in [json!([]), json!(["files.nonempty","files.nonempty"]), json!([true]), json!(["bad\n"])] {
            let mut bad = raw.clone(); bad["acceptance"]["criteria"][0]["checks"]["required_check_ids"] = value;
            assert!(normalize("portal_jobs_start_v2", bad).is_err());
        }
        let mut bad = raw.clone(); bad["acceptance"]["criteria"][0]["checks"]["argv"] = json!(["unregistered"]);
        assert!(normalize("portal_jobs_start_v2", bad).is_err());
        for value in [json!(true), json!("01"), json!("9223372036854775808"), json!(9007199254740992u64), Value::Null] {
            let mut bad = raw.clone(); bad["budget"]["max_job_attempts"] = value;
            assert!(normalize("portal_jobs_start_v2", bad).is_err());
        }
        let mut bad = raw.clone(); bad["role"] = json!("proposer");
        assert!(normalize("portal_jobs_start_v2", bad).is_err());
        let mut bad = raw.clone(); bad["budget"]["wall_seconds"] = json!(900);
        assert!(normalize("portal_jobs_start_v2", bad).is_err());
        let id = "a".repeat(32);
        let artifact = json!({"contract_version":2,"job_id":id,"filename":format!("worker-{}.json", "b".repeat(32)),"offset":"0","max_bytes":"65536"});
        let (_, _, got) = normalize("portal_jobs_artifact_v2", artifact.clone()).unwrap();
        assert_eq!(got, artifact);
        let mut bad = artifact; bad["filename"] = json!(format!("worker-{}.json\n", "b".repeat(32)));
        assert!(normalize("portal_jobs_artifact_v2", bad).is_err());
    }

    #[test]
    fn jobs_v2_commands_preserve_exact_fields_and_decimal_integers() {
        let id = "a".repeat(32);
        let start = json!({"contract_version":2,"role":"proposer","task":"Supplied evidence","attachment":"attachment-1","request_key":"same-start-key","team":"saved-team","team_revision":"9007199254740993",
            "budget":{"start_calls":10,"wall_mode":"fixed","wall_seconds":1800,"max_job_attempts":"9007199254740993","job_seconds_budget":"9223372036854775807"},
            "job_policy":{"proposed_seconds":900,"rationale":"Bounded report","auto_escalate":false},
            "job_coverage":{"profiles":{"proposal":"d".repeat(64)},"workspaces":{"projects":["fixture"]},"attachments":["attachment-1"],"max_job_attempts":"9007199254740993","job_seconds_budget":"9223372036854775807","allow_cli":false}});
        for (command, args, operation) in [
            ("portal_jobs_attachments_v2", json!({"contract_version":2}), "jobs.attachments"),
            ("portal_jobs_list_v2", json!({"contract_version":2,"cursor":id,"limit":50}), "jobs.list"),
            ("portal_jobs_start_v2", start.clone(), "jobs.start"),
            ("portal_jobs_audit_v2", json!({"contract_version":2,"job_id":id,"after_event_seq":"9007199254740993"}), "jobs.audit"),
            ("portal_jobs_cancel_v2", json!({"contract_version":2,"job_id":id,"request_key":"same-cancel-key"}), "jobs.cancel"),
            ("portal_jobs_proposal_v2", json!({"contract_version":2,"job_id":id}), "jobs.proposal"),
            ("portal_jobs_decide_v2", json!({"contract_version":2,"job_id":id,"decision":"approve","proposal_digest":"d".repeat(64),"request_key":"same-decision-key","expected_revision":"9223372036854775807"}), "jobs.decide"),
            ("portal_jobs_artifact_v2", json!({"contract_version":2,"job_id":id,"filename":format!("report-{id}.json"),"offset":0,"max_bytes":65536}), "jobs.artifact"),
        ] {
            assert!(PORTAL_COMMANDS.contains(&command));
            let (op, _, forwarded) = normalize(command, args.clone()).unwrap();
            assert_eq!(op, operation); assert_eq!(forwarded, args);
            let mut unknown = args.clone(); unknown["owner_bearer"] = json!("forbidden");
            assert!(normalize(command, unknown).is_err());
            let mut old = args; old["contract_version"] = json!(1);
            assert!(normalize(command, old).is_err(), "no reduced request or legacy retry");
        }
        for value in [json!(true), json!(0), json!(1.5), json!("01"), json!("1e3"), json!("9223372036854775808"), json!(9007199254740992u64)] {
            let mut bad = start.clone(); bad["team_revision"] = value.clone();
            assert!(normalize("portal_jobs_start_v2", bad).is_err());
            let mut bad = start.clone(); bad["budget"]["max_job_attempts"] = value;
            assert!(normalize("portal_jobs_start_v2", bad).is_err());
        }
        let mut bad = start.clone(); bad["job_policy"]["auto_escalate"] = json!(true);
        assert!(normalize("portal_jobs_start_v2", bad).is_err());
        let mut bad = start.clone(); bad["wall_seconds"] = json!(120);
        assert!(normalize("portal_jobs_start_v2", bad).is_err());
        for job_id in ["../outside", "not-a-job", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"] {
            assert!(normalize("portal_jobs_audit_v2", json!({"contract_version":2,"job_id":job_id})).is_err());
        }
        for name in ["../secret", "report.json", "report-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json/other"] {
            assert!(normalize("portal_jobs_artifact_v2", json!({"contract_version":2,"job_id":id,"filename":name})).is_err());
        }
        for maximum in [json!(0), json!(65537), json!(true)] {
            assert!(normalize("portal_jobs_artifact_v2", json!({"contract_version":2,"job_id":id,"filename":format!("report-{id}.json"),"max_bytes":maximum})).is_err());
        }
        assert!(normalize("portal_owner_begin_v2", json!({"contract_version":2,"operation":"jobs.decide","resource":{"job_id":id}})).is_ok());
        assert!(op_for("portal_jobs_apply_v2").is_none());
        for path in ["drive:outside", "control\npath", "../outside", "/absolute"] {
            let mut bad = start.clone(); bad["job_coverage"]["workspaces"]["projects"] = json!([path]);
            assert!(normalize("portal_jobs_start_v2", bad).is_err());
        }
    }

    #[test]
    fn work_commands_preserve_bounded_contract_and_owner_controls() {
        let cases = [
            ("portal_work_catalog_v2",json!({"contract_version":2})),
            ("portal_work_preparation_v2",json!({"contract_version":2,"preparation_id":"a".repeat(64)})),
            ("portal_work_start_v2",json!({"contract_version":2,"preparation_id":"a".repeat(64),"request_key":"start"})),
            ("portal_work_workspace_create_v2",json!({"contract_version":2,"project_id":"project","request_key":"setup"})),
            ("portal_work_publish_v2",json!({"contract_version":2,"job_id":"a".repeat(32),"proposal_digest":"b".repeat(64),"request_key":"publish","expected_revision":1})),
        ];
        for (command, payload) in cases {
            assert!(PORTAL_COMMANDS.contains(&command));
            assert_eq!(normalize(command,payload.clone()).unwrap().2,payload);
            let mut injected=payload.clone();injected["authority"]=json!({"owner":"other"});
            assert!(normalize(command,injected).is_err());
            let mut legacy=payload;legacy["contract_version"]=json!(1);
            assert!(normalize(command,legacy).is_err());
        }
        assert!(normalize("portal_work_start_v2",json!({"contract_version":2,"request_key":"start"})).is_err());
        assert!(normalize("portal_owner_begin_v2",json!({"contract_version":2,"operation":"work.publish","resource":{"job_id":"a".repeat(32)}})).is_ok());
        assert!(is_write_op("work.publish"));
        assert!(!is_write_op("work.status"));
    }

    #[test]
    fn legacy_local_profile_is_rewritten_preserving_connection_and_developer_escape() {
        let root = double_dir("r6b-legacy");
        let original = r#"{"active":"local_bob_model","connection":{"lane":"guest","mode":"this_pc"},"schema":"portal-settings-v1"}"#;
        std::fs::write(settings_path(&root), original).unwrap();
        let (settings, notice) = recover_profile(&root, false).unwrap();
        assert_eq!(settings.active, "bob_server"); assert!(notice.is_some());
        let saved = read_settings_document(&root).unwrap();
        assert_eq!(saved["connection"], json!({"lane":"guest","mode":"this_pc"}));
        assert_eq!(saved["active"], "bob_server");
        assert!(recover_profile(&root, false).unwrap().1.is_none());
        for id in ["local_bob_model", "standalone_http"] {
            assert!(select_profile(&root, id, false).is_err());
            select_profile(&root, id, true).unwrap();
            assert_eq!(recover_profile(&root, true).unwrap().0.active, id);
        }
        assert_eq!(profile_rows_for(false).as_array().unwrap().len(), 2);
        assert_eq!(profile_rows_for(true).as_array().unwrap().len(), 4);
    }

    #[test]
    fn profile_store_refuses_unknown_ids_and_records_active_choice() {
        let root = double_dir("settings");
        assert!(write_settings(&root, "bogus").is_err(), "no silent profile fallback");
        let written = write_settings(&root, "bob_server").expect("known profile persists");
        assert!(written.path.is_file());
        assert_eq!(read_settings(&root).active, "bob_server");
        assert_eq!(read_settings(&double_dir("settings-empty")).active, "bob_server");
        let payload = profiles_payload().unwrap();
        let ids: Vec<&str> = payload["profiles"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["id"].as_str().unwrap_or_default())
            .collect();
        assert_eq!(ids, if developer_mode() { vec!["bob_server", "bob_team", "local_bob_model", "standalone_http"] } else { vec!["bob_server", "bob_team"] });
    }

    #[test]
    fn explicit_ssh_config_is_one_argument_and_keeps_strict_checks() {
        let root = double_dir("ssh config");
        let path = root.join("candidate config");
        std::fs::write(&path, "Host bob-server\n HostName 192.0.2.1\n").unwrap();
        let args = configured_ssh_args("bob-server", Some(&path), &example_connection()).unwrap();
        assert_eq!(&args[..2], &["-F".to_string(), path.to_string_lossy().to_string()]);
        assert!(args.contains(&"StrictHostKeyChecking=yes".to_string()));
        assert_eq!(args.iter().filter(|arg| *arg == "bob@bob-server").count(), 1);
        assert!(configured_ssh_args("bob-server", Some(Path::new("relative-config")), &example_connection()).is_err());
        assert_eq!(configured_ssh_args("bob-server", None, &example_connection()).unwrap(), ssh_args("bob-server", &example_connection()));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o666)).unwrap();
            assert!(configured_ssh_args("bob-server", Some(&path), &example_connection()).is_err(), "writable-by-others config must be refused");
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
    }

    #[cfg(unix)]
    #[test]
    fn unix_ssh_program_probe() {
        if std::env::var_os("BOB_PORTAL_UNIX_SSH_PROBE").is_some() {
            assert_eq!(ssh_program(), "ssh", "a Windows-looking filename must not select a Unix transport executable");
        }
    }

    #[cfg(unix)]
    #[test]
    fn unix_transport_ignores_a_windows_named_file_in_the_working_directory() {
        let root = double_dir("windows-file");
        std::fs::write(root.join(r"C:\Windows\System32\OpenSSH\ssh.exe"), b"not an executable").unwrap();
        let status = Command::new(std::env::current_exe().unwrap())
            .args(["unix_ssh_program_probe", "--nocapture", "--test-threads=1"])
            .env("BOB_PORTAL_UNIX_SSH_PROBE", "1").current_dir(&root).status().unwrap();
        assert!(status.success());
    }

    #[test]
    fn portal_targets_are_bare_host_names() {
        assert!(valid_target("example-server"));
        assert!(valid_target("192.0.2.35"));
        assert!(valid_target("192.0.2.1"));
        for bad_target in ["example-server; rm -rf /", "host name", "-oProxyCommand=x", "user@host", "$(id)", ""] {
            assert!(!valid_target(bad_target), "{bad_target}");
        }
        let args = ssh_args("example-server", &example_connection());
        assert_eq!(args[0], "-o");
        assert_eq!(args[1], "BatchMode=yes");
        assert!(args.contains(&"bob@example-server".to_string()));
        assert!(args.contains(&"python3".to_string()));
        assert!(args.contains(&"--portal-approvals".to_string()),
                "the portal must ask the broker for approval decisions; the server guard is the authority");
    }
}
