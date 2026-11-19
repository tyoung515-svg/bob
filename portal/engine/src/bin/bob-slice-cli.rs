//! bob-slice-cli — the headless S5 NDJSON driver for the BoB vertical slice
//! (E5, the overnight MVP's guaranteed deliverable).
//!
//! Runs ONE read-only FX-READ-01 tool call end-to-end
//! S1(ledger) → S2(authority) → S4(exec) → S5(protocol):
//!
//! - stdin: NDJSON requests (`hello` first, then `session.create`,
//!   `turn.start`; the approval response arrives as `interaction.resolve`
//!   echoing the `approval.requested` preview's `call_id`/`decision_id`/
//!   `args_digest` — AT-S5-06 binding).
//! - stdout: NDJSON — one line per response (no `seq`) and one line per
//!   event frame (`seq` gap-free from 1). Every frame is built from the
//!   durable, fdatasync'd backing record read back from the segment
//!   (ledger-before-offer, S5 §2).
//! - The scripted upstream is the fixture itself: after `turn.start` the
//!   driver plays the model's one call (`fx-read ./hello.txt`, A-47).
//! - Closing stdin never cancels the turn (S5 §1): an EOF while an approval
//!   is pending leaves the call open (recovery's business, S4 §2c) and the
//!   process exits non-zero without a `turn.end`.
//!
//! Reproduce run (a) — happy path, allow_once:
//!   bob-slice-cli --data-root %TMP%\bob-e5-allow --fx-read <path-to-fx-read>
//! with stdin:
//!   {"id":"r1","method":"hello","params":{"version":{"major":2,"minor":0}}}
//!   {"id":"r2","method":"session.create","params":{}}
//!   {"id":"r3","method":"turn.start","params":{"model":"slice-model"}}
//!   {"id":"r4","method":"interaction.resolve","params":{"call_id":"call-1",
//!     "decision_id":"<from the approval.requested frame>","args_digest":"<from
//!     the frame>","response":"allow_once"}}
//! (the committed fixture scripts under engine/tests/fixtures/s5/ carry the
//! request prefix; the harness echoes the resolve ids from the frame).
//!
//! Reproduce run (b) — denial path: same, with the r4 response
//!   {"deny":{"reason":"not today"}}.

use std::collections::BTreeMap;
use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use serde_json::{json, Value};
use uuid::Uuid;

use bob_engine::authority::{ApprovalPreview, EffectClass};
use bob_engine::evaluator::{Evaluator, PolicyConfig, ToolEntry, ToolManifest};
use bob_engine::exec::{CancelToken, CallTerminal, Executor, GatePass, SliceExec};
use bob_engine::gate::{ApprovalResponse, Authorization, Gate, PendingDecision, PreWitness};
use bob_engine::ledger::{AckedCall, Ledger, SessionIds};
use bob_engine::payload::*;
use bob_engine::policy::{FX_READ_COMMAND, READ_ROOTS, SANDBOX_POLICY};
use bob_engine::protocol::{
    self as s5, error_line, parse_event_ack, parse_hello, parse_request, parse_resolve, result_line,
    Frame, IncomingRequest, ResolveResponse, SegmentReader, WireError,
};

const RULES_SRC: &str = "rules/default.rules";
const USER: &str = "user-1";
const TURN_ID: &str = "turn-1";
const STEP_ID: &str = "step-1";
const CALL_ID: &str = "call-1";
const FIXTURE_NAME: &str = "hello.txt";
const FIXTURE_BYTES: &[u8] = b"bob-slice-fixture\n";
const TARGET_ARG: &str = "./hello.txt";

// Exit codes (scriptable harnesses branch on them).
const EXIT_OK: u8 = 0;
const EXIT_EOF_PRE_HELLO: u8 = 2;
const EXIT_MALFORMED_PRE_HELLO: u8 = 3;
const EXIT_VERSION_MISMATCH: u8 = 4;
const EXIT_EOF_PENDING_APPROVAL: u8 = 5;
const EXIT_ENGINE_ERROR: u8 = 6;

/// The FX-READ-01 fixture policy (S4 §1, identical to the E4 battery):
/// mode default + the three-rule stack; `fx-read` routes to `ask`.
fn fx_config() -> PolicyConfig {
    PolicyConfig::from_json(&json!({
        "mode": "default",
        "revision": "rules/default.rules#r1",
        "rules": [
            {"effect": "deny", "matcher": {"tool": "exec_command", "argv_prefix": ["rm"]}, "source": RULES_SRC},
            {"effect": "ask",  "matcher": {"tool": "exec_command", "argv_prefix": [FX_READ_COMMAND]}, "source": RULES_SRC},
            {"effect": "allow","matcher": {"tool": "exec_command", "argv_prefix": ["echo"]}, "source": RULES_SRC}
        ]
    }))
    .expect("fixed slice policy parses")
}

fn fx_manifest(cwd: &std::path::Path) -> ToolManifest {
    let mut m = ToolManifest::default();
    m.tools.insert(
        "exec_command".to_string(),
        ToolEntry {
            effect_class: EffectClass::Read,
            read_roots: std::iter::once(cwd.to_string_lossy().into_owned()).chain(READ_ROOTS.iter().map(|s| s.to_string())).collect(),
            revoked: false,
        },
    );
    m
}

fn auth_snapshot() -> Value {
    json!({"approval_policy": {"mode": "default"}, "sandbox_policy": {"policy": "read_only"}})
}

struct Session {
    ids: SessionIds,
    ledger: Ledger,
    gate: Gate,
    executor: Executor,
    snapshot: Value,
    cwd: PathBuf,
    reader: SegmentReader,
}

/// Connection state: seq counter, cached responses (idempotent replay for
/// the connection's life, S5 §2) and the session.
struct Conn<'a> {
    out: io::StdoutLock<'a>,
    seq: u64,
    responses: BTreeMap<String, String>,
    session: Option<Session>,
    turn_done: bool,
}

impl Conn<'_> {
    fn write_line(&mut self, line: &str) {
        let _ = self.out.write_all(line.as_bytes());
        let _ = self.out.flush();
    }

    fn respond_result(&mut self, id: &str, result: Value) {
        let line = result_line(id, result);
        self.responses.insert(id.to_owned(), line.clone());
        self.write_line(&line);
    }

    fn respond_error(&mut self, id: &str, err: &WireError) {
        // Transient request-shape errors stay uncached so the client may
        // retry the same id with fixed params; definitive outcomes replay.
        let line = error_line(Some(id), err);
        if err.code != s5::wire_code::INVALID_REQUEST
            && err.code != s5::wire_code::INVALID_LIMITS
            && err.code != s5::wire_code::APPROVAL_MISMATCH
        {
            self.responses.insert(id.to_owned(), line.clone());
        }
        self.write_line(&line);
    }

    /// Refresh the segment read-back and return a clone of the durable
    /// record for an ack (ledger-before-offer: no record, no frame).
    fn record(&mut self, event_id: &str) -> Result<bob_engine::envelope::Record, u8> {
        let session = self.session.as_mut().expect("session exists");
        if let Err(e) = session.reader.refresh() {
            eprintln!("bob-slice-cli: segment read-back failed: {e}");
            return Err(EXIT_ENGINE_ERROR);
        }
        session
            .reader
            .by_event_id(event_id)
            .cloned()
            .ok_or_else(|| {
                eprintln!(
                    "bob-slice-cli: durable record {event_id} not found; refusing to offer a frame"
                );
                EXIT_ENGINE_ERROR
            })
    }

    /// Offer one frame for a durable ack's record.
    fn emit(&mut self, record: bob_engine::envelope::Record, event: &str, payload: Value) {
        self.seq += 1;
        let frame = Frame::from_record(self.seq, &record, event, payload);
        match frame.to_line() {
            Ok(line) => self.write_line(&line),
            Err(e) => {
                eprintln!("bob-slice-cli: frame serialization failed: {e}");
                std::process::exit(EXIT_ENGINE_ERROR as i32);
            }
        }
    }
}

fn main() -> ExitCode {
    let mut data_root: Option<PathBuf> = None;
    let mut fx_read: Option<PathBuf> = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--data-root" => {
                data_root = args.next().map(PathBuf::from);
            }
            "--fx-read" => {
                fx_read = args.next().map(PathBuf::from);
            }
            "--help" | "-h" => {
                println!("bob-slice-cli --data-root DIR [--fx-read PATH]");
                return ExitCode::SUCCESS;
            }
            other => {
                eprintln!("bob-slice-cli: unknown argument {other}");
                return ExitCode::from(2);
            }
        }
    }
    let Some(data_root) = data_root else {
        eprintln!("bob-slice-cli: --data-root DIR is required");
        return ExitCode::from(2);
    };
    let fx_read = fx_read.unwrap_or_else(default_fx_read);

    let stdin = io::stdin();
    let mut lines = stdin.lock();
    let stdout = io::stdout();
    let mut conn = Conn {
        out: stdout.lock(),
        seq: 0,
        responses: BTreeMap::new(),
        session: None,
        turn_done: false,
    };

    // ---- Handshake: hello is first (S5 §1). A malformed pre-handshake
    // line closes the connection (AT-S5-05).
    let first = match read_line(&mut lines) {
        None => {
            eprintln!("bob-slice-cli: stdin closed before hello");
            return ExitCode::from(EXIT_EOF_PRE_HELLO);
        }
        Some(Err(e)) => {
            eprintln!("bob-slice-cli: stdin read error: {e}");
            return ExitCode::from(EXIT_MALFORMED_PRE_HELLO);
        }
        Some(Ok(l)) => l,
    };
    let req = match parse_request(l_trim(&first)) {
        Ok(r) => r,
        Err(e) => {
            conn.write_line(&error_line(None, &e));
            eprintln!("bob-slice-cli: malformed pre-handshake request");
            return ExitCode::from(EXIT_MALFORMED_PRE_HELLO);
        }
    };
    if req.method != "hello" {
        let e = WireError::new(
            s5::wire_code::INVALID_REQUEST,
            "hello must be the first request (S5 §1)",
        );
        conn.write_line(&error_line(Some(&req.id), &e));
        return ExitCode::from(EXIT_MALFORMED_PRE_HELLO);
    }
    if let Err(e) = parse_hello(&req.params) {
        conn.write_line(&error_line(Some(&req.id), &e));
        if e.code == s5::wire_code::VERSION_MISMATCH {
            // Terminal (S10 6.1): nothing further may flow.
            return ExitCode::from(EXIT_VERSION_MISMATCH);
        }
        return ExitCode::from(EXIT_MALFORMED_PRE_HELLO);
    }
    conn.respond_result(&req.id, s5::hello_result());

    // ---- Post-handshake dispatch: one ordered per-session queue, one line
    // at a time (S5 §2).
    loop {
        let line = match read_line(&mut lines) {
            None => break,
            Some(Err(e)) => {
                eprintln!("bob-slice-cli: stdin read error: {e}");
                break;
            }
            Some(Ok(l)) => l,
        };
        let req = match parse_request(l_trim(&line)) {
            Ok(r) => r,
            Err(e) => {
                conn.write_line(&error_line(None, &e));
                continue;
            }
        };
        if let Some(cached) = conn.responses.get(&req.id).cloned() {
            conn.write_line(&cached);
            continue;
        }
        match req.method.as_str() {
            "event.ack" => match parse_event_ack(&req.params) {
                Ok(through) if through <= conn.seq => {
                    conn.respond_result(&req.id, json!({"through_seq": through}));
                }
                Ok(_) => {
                    conn.respond_error(
                        &req.id,
                        &WireError::new(
                            s5::wire_code::INVALID_REQUEST,
                            "event.ack.through_seq must be <= the last offered seq",
                        ),
                    );
                }
                Err(e) => conn.respond_error(&req.id, &e),
            },
            "hello" => {
                conn.respond_error(
                    &req.id,
                    &WireError::new(s5::wire_code::INVALID_REQUEST, "already handshook"),
                );
            }
            "session.create" => {
                if conn.session.is_some() {
                    conn.respond_error(
                        &req.id,
                        &WireError::new(
                            s5::wire_code::INVALID_REQUEST,
                            "one session per connection (S5 scope)",
                        ),
                    );
                } else {
                    match create_session(&data_root, &fx_read) {
                        Ok(session) => {
                            let result = json!({
                                "session_id": session.ids.session_id,
                                "ledger_path": session.ledger.path().to_string_lossy(),
                            });
                            conn.session = Some(session);
                            conn.respond_result(&req.id, result);
                        }
                        Err(e) => {
                            conn.respond_error(
                                &req.id,
                                &WireError::new(
                                    s5::wire_code::INTERNAL_ERROR,
                                    format!("session.create failed: {e}"),
                                ),
                            );
                        }
                    }
                }
            }
            "turn.start" => {
                if conn.session.is_none() {
                    conn.respond_error(
                        &req.id,
                        &WireError::new(
                            s5::wire_code::INVALID_REQUEST,
                            "session.create before turn.start",
                        ),
                    );
                } else if conn.turn_done {
                    conn.respond_error(
                        &req.id,
                        &WireError::new(
                            s5::wire_code::INVALID_REQUEST,
                            "one turn per session (S5 scope)",
                        ),
                    );
                } else {
                    match run_turn(&mut conn, &mut lines, &req) {
                        Ok(result) => {
                            conn.turn_done = true;
                            conn.respond_result(&req.id, result);
                        }
                        Err(code) => return ExitCode::from(code),
                    }
                }
            }
            "interaction.resolve" => {
                // Outside the approval wait there is never a pending
                // request: late or unsolicited resolves are closed
                // (AT-S5-06).
                conn.respond_error(
                    &req.id,
                    &WireError::new(
                        s5::wire_code::APPROVAL_CLOSED,
                        "no pending approval request (already resolved or none asked)",
                    ),
                );
            }
            _ => {
                conn.respond_error(
                    &req.id,
                    &WireError::new(
                        s5::wire_code::METHOD_NOT_SUPPORTED,
                        "method not in the slice request set",
                    ),
                );
            }
        }
    }
    ExitCode::from(EXIT_OK)
}

fn l_trim(s: &str) -> &str {
    s.trim_end_matches(['\r', '\n'])
}

/// One stdin line, `None` on EOF (BufRead-based; `StdinLock` is not an
/// Iterator by itself).
fn read_line(lines: &mut io::StdinLock) -> Option<io::Result<String>> {
    let mut buf = String::new();
    match lines.read_line(&mut buf) {
        Ok(0) => None,
        Ok(_) => Some(Ok(buf)),
        Err(e) => Some(Err(e)),
    }
}

/// Default the fx-read host program to the sibling of this executable
/// (EXEC-32 provisioning: `fx-read(.exe)` beside `bob-slice-cli(.exe)`).
fn default_fx_read() -> PathBuf {
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    let dir = exe.parent().map(PathBuf::from).unwrap_or_default();
    let with_suffix = dir.join(format!("fx-read{}", std::env::consts::EXE_SUFFIX));
    if with_suffix.exists() {
        return with_suffix;
    }
    dir.join("fx-read")
}

/// F-L4: refuse a fixture root that cannot be trusted *before* anything is
/// written into it. Preflight repeats these checks, but a symlinked
/// `<data-root>/slice` must not receive the fixture file first and be refused
/// afterwards.
fn validate_fixture_root(cwd: &std::path::Path) -> Result<(), String> {
    match std::fs::symlink_metadata(cwd) {
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(format!("fixture root {} cannot be inspected: {err}", cwd.display())),
        Ok(meta) => {
            if meta.file_type().is_symlink() {
                return Err(format!(
                    "fixture root {} must not be a symlink",
                    cwd.display()
                ));
            }
            if !meta.is_dir() {
                return Err(format!("fixture root {} is not a directory", cwd.display()));
            }
            if cwd.canonicalize().ok().as_deref() != Some(cwd) {
                return Err(format!("fixture root {} must be canonical", cwd.display()));
            }
            Ok(())
        }
    }
}

fn create_session(data_root: &PathBuf, fx_read: &PathBuf) -> Result<Session, String> {
    std::fs::create_dir_all(data_root).map_err(|e| e.to_string())?;
    // F-L4: validate before provisioning, and work from the canonical root so
    // the fixture root the executor later pins is the same path we wrote.
    let data_root = data_root.canonicalize().map_err(|e| e.to_string())?;
    let cwd = data_root.join("slice");
    validate_fixture_root(&cwd)?;
    std::fs::create_dir_all(&cwd).map_err(|e| e.to_string())?;
    if cwd.canonicalize().ok().as_deref() != Some(cwd.as_path()) {
        return Err(format!(
            "fixture root {} must be canonical before the fixture is written",
            cwd.display()
        ));
    }
    // The FX-READ-01 fixture file the scripted call reads.
    std::fs::write(cwd.join(FIXTURE_NAME), FIXTURE_BYTES).map_err(|e| e.to_string())?;
    let ids = SessionIds {
        session_id: format!("sess-{}", Uuid::new_v4()),
        thread_id: format!("thr-{}", Uuid::new_v4()),
        lineage_id: format!("lin-{}", Uuid::new_v4()),
        segment_id: format!("seg-{}", Uuid::new_v4()),
    };
    let (ledger, _lineage) = Ledger::open_new(&data_root, ids.clone()).map_err(|e| e.to_string())?;
    // A-1: surface the segment-creation diagnostics instead of letting them
    // die with the Ledger value. `dir_sync_unavailable` is the platform gap on
    // Windows: the per-record fdatasync boundary holds, the directory entry is
    // not fsynced.
    for diag in ledger.creation_diagnostics() {
        eprintln!("bob-slice-cli: diagnostic {} ({})", diag.code(), diag);
    }
    let reader = SegmentReader::new(ledger.path().to_path_buf());
    let gate = Gate {
        evaluator: Evaluator {
            config: fx_config(),
            manifest: fx_manifest(&cwd),
            sandbox_available: true,
            revocation_epoch: 1,
            fail_verdict: false,
            fail_integration: None,
        },
    };
    let executor = Executor::new("exec-1", fx_read.clone());
    Ok(Session {
        ids,
        ledger,
        gate,
        executor,
        snapshot: auth_snapshot(),
        cwd,
        reader,
    })
}

fn tool_call_payload(step_event_id: &str) -> ToolCall {
    ToolCall {
        call_id: CALL_ID.to_owned(),
        model_item_id: "item-1".to_owned(),
        call_index: 0,
        tool_name: "exec_command".to_owned(),
        handler_kind: "builtin".to_owned(),
        arguments: json!({"argv": [FX_READ_COMMAND, TARGET_ARG]}),
        execution_class: ExecutionClass::Serial,
        exposure: "model".to_owned(),
        authority_snapshot_event_id: step_event_id.to_owned(),
        dispatch: Dispatch {
            disposition: Disposition::AdmittedToDispatch,
            gate: bob_engine::payload::Gate::None,
            executor_id: None,
            reason_code: None,
        },
    }
}

/// Borrow the session's gate/executor/snapshot/cwd into the S4 driver.
fn slice_exec<'a>(
    gate: &'a Gate,
    executor: &'a Executor,
    snapshot: &'a Value,
    cwd: &'a PathBuf,
) -> SliceExec<'a> {
    SliceExec {
        gate,
        executor,
        authority_snapshot: snapshot,
        cwd,
        user_id: USER,
    }
}

fn engine_fatal(e: impl std::fmt::Display) -> u8 {
    eprintln!("bob-slice-cli: engine error: {e}");
    EXIT_ENGINE_ERROR
}

/// The approval wait (inside the turn): the `approval.requested` frame has
/// been offered; consume requests until a correctly-bound
/// `interaction.resolve` arrives. A mismatched echo → `approval_mismatch`,
/// NOTHING appended, the request stays pending (AT-S5-06).
fn wait_resolve(
    conn: &mut Conn,
    lines: &mut io::StdinLock,
    preview: &ApprovalPreview,
) -> ApprovalResponse {
    loop {
        let line = match read_line(lines) {
            Some(Ok(l)) => l,
            Some(Err(e)) => {
                eprintln!("bob-slice-cli: stdin read error while approval pending: {e}");
                std::process::exit(EXIT_EOF_PENDING_APPROVAL as i32);
            }
            None => {
                // Closing the transport never cancels the turn (S5 §1): the
                // call stays open; recovery owns it (S4 §2c). No turn.end.
                eprintln!(
                    "bob-slice-cli: stdin closed while approval pending; call left open for recovery"
                );
                std::process::exit(EXIT_EOF_PENDING_APPROVAL as i32);
            }
        };
        let req = match parse_request(l_trim(&line)) {
            Ok(r) => r,
            Err(e) => {
                conn.write_line(&error_line(None, &e));
                continue;
            }
        };
        if let Some(cached) = conn.responses.get(&req.id).cloned() {
            conn.write_line(&cached);
            continue;
        }
        match req.method.as_str() {
            "event.ack" => match parse_event_ack(&req.params) {
                Ok(through) if through <= conn.seq => {
                    conn.respond_result(&req.id, json!({"through_seq": through}));
                }
                Ok(_) => conn.respond_error(
                    &req.id,
                    &WireError::new(
                        s5::wire_code::INVALID_REQUEST,
                        "event.ack.through_seq must be <= the last offered seq",
                    ),
                ),
                Err(e) => conn.respond_error(&req.id, &e),
            },
            "interaction.resolve" => {
                let params = match parse_resolve(&req.params) {
                    Ok(p) => p,
                    Err(e) => {
                        conn.respond_error(&req.id, &e);
                        continue;
                    }
                };
                if params.call_id != preview.call_id
                    || params.decision_id != preview.decision_id
                    || params.args_digest != preview.args_digest
                {
                    conn.respond_error(
                        &req.id,
                        &WireError::new(
                            s5::wire_code::APPROVAL_MISMATCH,
                            "echoed call_id/decision_id/args_digest do not match the pending approval.requested; nothing appended, the request stays pending (AT-S5-06)",
                        )
                        .with_data(json!({
                            "expected": {
                                "call_id": preview.call_id,
                                "decision_id": preview.decision_id,
                                "args_digest": preview.args_digest,
                            }
                        })),
                    );
                    continue;
                }
                let response = match params.response {
                    ResolveResponse::AllowOnce => ApprovalResponse::AllowOnce {
                        decision_id: params.decision_id.clone(),
                        args_digest: params.args_digest.clone(),
                    },
                    ResolveResponse::Deny { reason } => ApprovalResponse::Deny {
                        decision_id: params.decision_id.clone(),
                        args_digest: params.args_digest.clone(),
                        reason,
                    },
                };
                let resolution = match &response {
                    ApprovalResponse::AllowOnce { .. } => "allow_once",
                    ApprovalResponse::Deny { .. } => "deny",
                };
                conn.respond_result(&req.id, json!({"resolution": resolution}));
                return response;
            }
            _ => {
                conn.respond_error(
                    &req.id,
                    &WireError::new(
                        s5::wire_code::METHOD_NOT_SUPPORTED,
                        "method not in the slice request set",
                    ),
                );
            }
        }
    }
}

/// Drive the one turn: turn.start + step.context → tool.call → authority →
/// (ask: approval wait) → E3 → preflight → witness → spawn → tool.result →
/// turn.end, offering a frame after each durable record (S5 §3/§4).
fn run_turn(conn: &mut Conn, lines: &mut io::StdinLock, req: &IncomingRequest) -> Result<Value, u8> {
    let model = req
        .params
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("slice-model")
        .to_owned();

    // turn.start + step.context durable, then the turn.started frame is
    // offered (backed by turn.start; step.context must already be durable).
    let (turn_ack, step) = {
        let Session { ledger, .. } = conn.session.as_mut().unwrap();
        let turn_ack = ledger
            .append_turn_start(
                TURN_ID,
                TurnStart {
                    input_digest: "sha256:slice-input-fixture".into(),
                    parent_turn_id: None,
                },
            )
            .map_err(engine_fatal)?;
        let step = ledger
            .append_step_context(
                TURN_ID,
                STEP_ID,
                StepContext {
                    model: model.clone(),
                    tool_router_revision: "r1".into(),
                    model_visible_tools: vec!["exec_command".into()],
                    mcp_bindings: vec![],
                    approval_policy: json!({"mode": "default"}),
                    sandbox_policy: serde_json::to_value(SANDBOX_POLICY).expect("policy serializes"),
                    settings_revision: "s1".into(),
                },
            )
            .map_err(engine_fatal)?;
        (turn_ack, step)
    };
    let turn_rec = conn.record(&turn_ack.event_id)?;
    conn.emit(
        turn_rec,
        "turn.started",
        json!({"input_item_count": 1, "model": model, "step_id": STEP_ID}),
    );

    // tool.call durable (I-05) BEFORE anything else happens.
    let call = tool_call_payload(&step.event_id);
    let acked = {
        let Session { ledger, .. } = conn.session.as_mut().unwrap();
        ledger
            .append_tool_call(TURN_ID, STEP_ID, call.clone())
            .map_err(engine_fatal)?
    };
    let call_rec = conn.record(&acked.ack().event_id)?;
    let call_payload = s5::tool_call_frame_payload(&call_rec.payload);
    conn.emit(call_rec, "tool.call", call_payload);

    // E1: authority.decided (always emitted, RULING-4).
    let authz = {
        let Session {
            ledger,
            gate,
            snapshot,
            cwd,
            ..
        } = conn.session.as_mut().unwrap();
        gate.authorize_call(ledger, acked, &call, snapshot, &s5::now_ts(), cwd)
            .map_err(engine_fatal)?
    };

    let acked_pending: Option<(AckedCall, PendingDecision)> = match authz {
        Authorization::Allow {
            acked,
            decided,
            pending,
        } => {
            let rec = conn.record(&decided.event_id)?;
            let payload = s5::authority_decided_frame_payload(&rec.payload);
            conn.emit(rec, "authority.decided", payload);
            Some((acked, pending))
        }
        Authorization::Ask {
            acked,
            decided,
            requested,
            preview,
            pending,
        } => {
            let rec = conn.record(&decided.event_id)?;
            let payload = s5::authority_decided_frame_payload(&rec.payload);
            conn.emit(rec, "authority.decided", payload);
            // The frame the UI/harness renders from — offered only now that
            // the ApprovalPreview record is durable (S5 §3).
            let rec = conn.record(&requested.event_id)?;
            let payload = rec.payload.clone();
            conn.emit(rec, "approval.requested", payload);

            let response = wait_resolve(conn, lines, &preview);
            let outcome = {
                let Session {
                    ledger,
                    gate,
                    snapshot,
                    ..
                } = conn.session.as_mut().unwrap();
                gate.resolve_approval(ledger, acked, pending, response, USER, &s5::now_ts(), snapshot)
                    .map_err(engine_fatal)?
            };
            match outcome {
                bob_engine::gate::ApprovalOutcome::Allowed {
                    acked,
                    resolved,
                    pending,
                } => {
                    let rec = conn.record(&resolved.event_id)?;
                    let payload = s5::approval_resolved_frame_payload(&rec.payload);
                    conn.emit(rec, "approval.resolved", payload);
                    Some((acked, pending))
                }
                bob_engine::gate::ApprovalOutcome::Denied { resolved, result } => {
                    let rec = conn.record(&resolved.event_id)?;
                    let payload = s5::approval_resolved_frame_payload(&rec.payload);
                    conn.emit(rec, "approval.resolved", payload);
                    emit_result_frame(conn, &result.event_id)?;
                    None
                }
            }
        }
        Authorization::Denied { decided, result } => {
            let rec = conn.record(&decided.event_id)?;
            let payload = s5::authority_decided_frame_payload(&rec.payload);
            conn.emit(rec, "authority.decided", payload);
            emit_result_frame(conn, &result.event_id)?;
            None
        }
    };

    let (acked, pending) = match acked_pending {
        None => return finish_turn(conn),
        Some(pair) => pair,
    };

    // E3 pre-witness check.
    let cleared = {
        let Session {
            ledger,
            gate,
            snapshot,
            ..
        } = conn.session.as_mut().unwrap();
        gate.pre_witness_check(ledger, acked, pending.clone(), &s5::now_ts(), snapshot)
            .map_err(engine_fatal)?
    };
    let acked = match cleared {
        PreWitness::Cleared { acked } => acked,
        PreWitness::Refused { result } => {
            emit_result_frame(conn, &result.event_id)?;
            return finish_turn(conn);
        }
    };

    // Preflight (read-only, no child; S4 §3 step 6).
    let argv = vec![FX_READ_COMMAND.to_owned(), TARGET_ARG.to_owned()];
    let preflight = {
        let Session { executor, cwd, gate, .. } = conn.session.as_ref().unwrap();
        executor.preflight_with_read_roots(&argv, cwd, &gate.evaluator.manifest.tools["exec_command"].read_roots)
    };
    let preflight = match preflight {
        Ok(p) => p,
        Err(f) => {
            let ack = {
                let Session {
                    ledger,
                    gate,
                    executor,
                    snapshot,
                    cwd,
                    ..
                } = conn.session.as_mut().unwrap();
                let slice = slice_exec(gate, executor, snapshot, cwd);
                slice
                    .abort_pre_witness(ledger, acked, &pending, f.code, None)
                    .map_err(engine_fatal)?
            };
            emit_result_frame(conn, &ack.event_id)?;
            return finish_turn(conn);
        }
    };

    // Durable witness → spawn → result (S4 §3 steps 7-9 through the token
    // APIs). Cancel stays engine-side only (D-28): the scripted runs never
    // fire the token.
    let cancel = CancelToken::new();
    let pass = {
        let Session {
            ledger,
            gate,
            executor,
            snapshot,
            cwd,
            ..
        } = conn.session.as_mut().unwrap();
        let matches = gate.evaluator.rule_matches(&pending.tool_name, &pending.args);
        let slice = slice_exec(gate, executor, snapshot, cwd);
        slice
            .witness(ledger, acked, &pending, matches, preflight)
            .map_err(engine_fatal)?
    };
    let auth = match pass {
        GatePass::Authorized(auth) => {
            let started = auth.started.witness().clone();
            let rec = conn.record(&started.event_id)?;
            conn.emit(rec, "tool.started", json!({"call_id": CALL_ID}));
            *auth
        }
        GatePass::Terminal(CallTerminal::WitnessWriteFailed { best_effort, .. }) => {
            // Ledger-fatal posture (MGR-2). `best_effort` is None iff the
            // best-effort `aborted/witness_write_failed` record landed: in
            // that case offer its frame (found by kind — the terminal
            // carries no ack for it); a faulted segment takes no turn.end.
            if best_effort.is_none() {
                if let Some(rec) = conn
                    .session
                    .as_mut()
                    .and_then(|s| s.reader.refresh().ok().map(|_| s))
                    .and_then(|s| s.reader.records_last_of_kind("tool.result").cloned())
                {
                    let payload =
                        s5::tool_result_frame_payload(&rec.payload, rec.caused_by.as_deref());
                    conn.emit(rec, "tool.result", payload);
                }
            }
            return Err(EXIT_ENGINE_ERROR);
        }
        GatePass::Terminal(t) => {
            let ack = terminal_ack(&t).0.event_id.clone();
            emit_result_frame(conn, &ack)?;
            return finish_turn(conn);
        }
    };
    let terminal = {
        let Session {
            ledger,
            gate,
            executor,
            snapshot,
            cwd,
            ..
        } = conn.session.as_mut().unwrap();
        let slice = slice_exec(gate, executor, snapshot, cwd);
        slice
            .execute_witnessed(ledger, auth, &|| s5::now_ts(), &cancel)
            .map_err(engine_fatal)?
    };
    let ack = terminal_ack(&terminal).0.event_id.clone();
    emit_result_frame(conn, &ack)?;
    finish_turn(conn)
}

fn terminal_ack(t: &CallTerminal) -> (&bob_engine::ledger::DurableAck, Option<&'static str>) {
    match t {
        CallTerminal::Denied { ack } => (ack, None),
        CallTerminal::Indeterminate { ack } => (ack, Some(bob_engine::exec::code::EXECUTION_SUPERVISION_LOST)),
        CallTerminal::Aborted { ack, code } => (ack, Some(code)),
        CallTerminal::Failed { ack, code } => (ack, Some(code)),
        CallTerminal::Succeeded { ack, .. } => (ack, None),
        CallTerminal::WitnessWriteFailed { .. } => unreachable!("handled by the caller"),
    }
}

/// Offer the `tool.result` frame from the durable result record.
fn emit_result_frame(conn: &mut Conn, result_event_id: &str) -> Result<(), u8> {
    let rec = conn.record(result_event_id)?;
    let payload = s5::tool_result_frame_payload(&rec.payload, rec.caused_by.as_deref());
    conn.emit(rec, "tool.result", payload);
    Ok(())
}

/// Append `turn.end` (status per the S5 §5 reduction over the call's
/// terminal result), then offer the final `turn.completed` frame. Returns
/// the turn.start response result.
fn finish_turn(conn: &mut Conn) -> Result<Value, u8> {
    append_turn_end_record(conn)
}

fn append_turn_end_record(conn: &mut Conn) -> Result<Value, u8> {
    // The call's terminal result record drives the reduction.
    let (status, turn_error, wire_error) = {
        let session = conn.session.as_mut().unwrap();
        session.reader.refresh().map_err(|e| {
            eprintln!("bob-slice-cli: segment read-back failed: {e}");
            EXIT_ENGINE_ERROR
        })?;
        let result = session
            .reader
            .records_last_of_kind("tool.result")
            .cloned()
            .ok_or_else(|| {
                eprintln!("bob-slice-cli: no terminal tool.result for the turn");
                EXIT_ENGINE_ERROR
            })?;
        let rp = s5::tool_result_frame_payload(&result.payload, result.caused_by.as_deref());
        // S5 §5 reduction for a one-call turn: turn cancelled → aborted;
        // indeterminate → incomplete; any other non-success → failed with
        // the call's OperationError; else succeeded.
        let status = match rp["status"].as_str().unwrap_or_default() {
            "succeeded" => TurnStatus::Succeeded,
            "aborted" if rp.get("abort_cause").and_then(Value::as_str) == Some("cancelled") => {
                TurnStatus::Aborted
            }
            "indeterminate" => TurnStatus::Incomplete,
            _ => TurnStatus::Failed,
        };
        let wire_error = rp.get("error").cloned().unwrap_or(Value::Null);
        let turn_error = if status == TurnStatus::Succeeded {
            None
        } else {
            Some(TurnError {
                code: wire_error["code"].as_str().unwrap_or_default().to_owned(),
                message: wire_error["message"].as_str().unwrap_or_default().to_owned(),
            })
        };
        (status, turn_error, wire_error)
    };
    let end_ack = {
        let Session { ledger, .. } = conn.session.as_mut().unwrap();
        ledger
            .append_turn_end(TURN_ID, status, Some(STEP_ID), turn_error.clone())
            .map_err(engine_fatal)?
    };
    let rec = conn.record(&end_ack.event_id)?;
    conn.emit(
        rec,
        "turn.completed",
        json!({"status": status_str(status), "error": wire_error}),
    );
    let mut result = json!({
        "turn_id": TURN_ID,
        "step_id": STEP_ID,
        "status": status_str(status),
    });
    if let Some(te) = &turn_error {
        result["error"] = json!({"code": te.code, "message": te.message});
    }
    Ok(result)
}

fn status_str(s: TurnStatus) -> &'static str {
    match s {
        TurnStatus::Succeeded => "succeeded",
        TurnStatus::Failed => "failed",
        TurnStatus::Aborted => "aborted",
        TurnStatus::Incomplete => "incomplete",
    }
}

#[cfg(all(test, unix))]
mod preview_root_tests {
    use super::*;

    fn scratch(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "bob-cli-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// F-L4: a symlinked `<data-root>/slice` must be refused *before* the
    /// fixture is written, so nothing lands in the symlink target.
    #[test]
    fn symlinked_fixture_root_is_refused_before_the_fixture_is_written() {
        let base = scratch("f-l4");
        let target = base.join("symlink-target");
        let data_root = base.join("data");
        std::fs::create_dir_all(&target).unwrap();
        std::fs::create_dir_all(&data_root).unwrap();
        std::os::unix::fs::symlink(&target, data_root.join("slice")).unwrap();
        let error = create_session(&data_root, &PathBuf::from("/usr/bin/true"))
            .err()
            .expect("a symlinked fixture root must be refused");
        assert!(error.contains("symlink"), "typed refusal, got: {error}");
        assert!(
            !target.join(FIXTURE_NAME).exists(),
            "the fixture must not be written through the symlink"
        );
    }

    /// The ordinary path still provisions the fixture under a plain root.
    #[test]
    fn plain_fixture_root_is_provisioned() {
        let data_root = scratch("f-l4-ok");
        let session = create_session(&data_root, &PathBuf::from("/usr/bin/true"))
            .expect("a plain data root must provision");
        assert_eq!(
            std::fs::read(session.cwd.join(FIXTURE_NAME)).unwrap(),
            FIXTURE_BYTES
        );
        assert_eq!(session.cwd.file_name().unwrap(), "slice");
        assert_eq!(
            session.cwd.canonicalize().unwrap(),
            session.cwd,
            "the fixture root the executor pins is canonical"
        );
    }
}
