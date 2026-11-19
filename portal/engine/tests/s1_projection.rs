//! S1 SQLite projection tests (E8, I-24/I-25, S1.8–S1.10): the read-only
//! derived index over the JSONL ledger — the `assertions` view, the I-24
//! joined call view (one row per tool call with the causal refs and the
//! five-status terminal), the I-25 `accepted` view (EXEC-59: validated
//! terminals of any of the five statuses), the pinned (segment_id, ordinal,
//! byte offset) cursor, and rebuildability from segment bytes alone.
//!
//! Coverage:
//! - the committed E5 fixtures driven as real scripted allow/deny runs
//!   through `bob-slice-cli` (segments produced live, then projected);
//! - synthetic segments covering each of the five statuses (S1 §4a rows
//!   1/2/3/5/6/7/9/10/11/12/13) reusing the E2 matrix helper style;
//! - the crash-point recoveries CP-1..CP-9 (E2 helpers: prefix → drop →
//!   open_resume → synthesize_recovery_results), CP-9 as a torn tail;
//! - view contents asserted against the replay scanner's assertions view;
//! - delete-the-DB-and-rebuild identity (I-25, AT-S1-02.10);
//! - invalid_success: mutated digest, hand-appended duplicate result,
//!   witness line removed.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{Receiver, RecvTimeoutError, channel};
use std::time::Duration;

use rusqlite::types::ValueRef;
use rusqlite::Connection;
use serde_json::{Value, json};

use bob_engine::authority::*;
use bob_engine::envelope::Record;
use bob_engine::ledger::{AckedCall, DurableAck, Ledger, SessionIds};
use bob_engine::payload::*;
use bob_engine::projection::{ProjectionReport, project_segment};
use bob_engine::replay::replay_segment;

// ---------- ledger-builder helpers (E2 matrix style, s1_ledger.rs) ----------

fn tmp_root(tag: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e8-{tag}-{}-{nanos}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn ids(tag: &str) -> SessionIds {
    SessionIds {
        session_id: format!("sess-{tag}"),
        thread_id: format!("thr-{tag}"),
        lineage_id: format!("lin-{tag}"),
        segment_id: format!("seg-{tag}"),
    }
}

fn step_context() -> StepContext {
    StepContext {
        model: "slice-model".into(),
        tool_router_revision: "r1".into(),
        model_visible_tools: vec!["slice.read".into()],
        mcp_bindings: vec![],
        approval_policy: serde_json::json!({"mode": "default"}),
        sandbox_policy: serde_json::json!({"policy": "read_only", "network_access": false}),
        settings_revision: "s1".into(),
    }
}

fn turn_start() -> TurnStart {
    TurnStart {
        input_digest: "sha256:fixture".into(),
        parent_turn_id: None,
    }
}

fn prefix(ledger: &mut Ledger, turn: &str, step: &str) -> (String, String) {
    let t = ledger.append_turn_start(turn, turn_start()).unwrap();
    let s = ledger.append_step_context(turn, step, step_context()).unwrap();
    (t.event_id, s.event_id)
}

fn tool_call(call_id: &str, disposition: Disposition, snapshot_event_id: &str) -> ToolCall {
    ToolCall {
        call_id: call_id.into(),
        model_item_id: "item-1".into(),
        call_index: 0,
        tool_name: "slice.read".into(),
        handler_kind: "builtin".into(),
        arguments: serde_json::json!({"path": "slice/hello.txt"}),
        execution_class: ExecutionClass::Serial,
        exposure: "model".into(),
        authority_snapshot_event_id: snapshot_event_id.into(),
        dispatch: Dispatch {
            disposition,
            gate: Gate::None,
            executor_id: None,
            reason_code: None,
        },
    }
}

fn admitted(ledger: &mut Ledger, turn: &str, step: &str, step_ev: &str, call_id: &str) -> AckedCall {
    ledger
        .append_tool_call(turn, step, tool_call(call_id, Disposition::AdmittedToDispatch, step_ev))
        .unwrap()
}

fn decision(id: &str, state: DecisionState, code: &str, text: &str) -> DecisionRecord {
    DecisionRecord {
        decision_id: id.into(),
        state,
        reason: DecisionReason {
            code: code.into(),
            params: serde_json::json!({}),
            explanation: Explanation { text: text.into() },
        },
        actor: ActorRef {
            kind: ActorKind::PolicyEngine,
            stable_id: "engine".into(),
        },
        matched_rule_ref: None,
        previous_decision_id: None,
        occurred_at: "2026-10-16T00:00:00.000Z".into(),
        route: "user".into(),
        approval_mode: "default".into(),
        action_id: "act-1".into(),
        policy_ref: None,
    }
}

fn decided(
    id: &str,
    outcome: DecisionOutcome,
    source: DecisionSource,
    rule_ref: Option<&str>,
) -> AuthorityDecided {
    let (state, code) = match outcome {
        DecisionOutcome::Allow => (DecisionState::Approved, "explicit_allow"),
        DecisionOutcome::Deny => (DecisionState::Denied, "rule_forbidden"),
        DecisionOutcome::Ask => (DecisionState::ApprovalRequired, "approval_route_selected"),
    };
    AuthorityDecided {
        outcome,
        source,
        matched_rule_ref: rule_ref.map(str::to_owned),
        proposed: decision(id, DecisionState::Proposed, "proposed", "call proposed"),
        decision: decision(id, state, code, &format!("explanation: {code}")),
        decision_ref: id.into(),
    }
}

fn preview(call_id: &str, decision_id: &str) -> ApprovalPreview {
    ApprovalPreview {
        call_id: call_id.into(),
        decision_id: decision_id.into(),
        tool_name: "slice.read".into(),
        action_summary: "exec_command: cat slice/hello.txt".into(),
        args_preview: "{\"path\":\"slice/hello.txt\"}".into(),
        args_truncated: false,
        args_digest: "sha256:deadbeef".into(),
        effect_class: EffectClass::Read,
        targets: serde_json::json!({"cwd": "/slice", "paths": []}),
        matched_rule: None,
        requested_at: "2026-10-16T00:00:00.000Z".into(),
    }
}

fn resolved_allow(id: &str) -> ApprovalResolved {
    ApprovalResolved {
        resolution: Resolution::Allow,
        actor: ActorRef {
            kind: ActorKind::User,
            stable_id: "dad".into(),
        },
        user_response: Some(UserResponse::AllowOnce),
        reason: None,
        cancel_cause: None,
        decision: Some(decision(id, DecisionState::Approved, "reviewer_approved", "approved by dad")),
    }
}

fn resolved_deny_user(id: &str, code: &str, text: &str) -> ApprovalResolved {
    ApprovalResolved {
        resolution: Resolution::Deny,
        actor: ActorRef {
            kind: ActorKind::User,
            stable_id: "dad".into(),
        },
        user_response: Some(UserResponse::Deny),
        reason: Some("not today".into()),
        cancel_cause: None,
        decision: Some(decision(id, DecisionState::Denied, code, text)),
    }
}

fn resolved_cancelled(cause: CancelCause) -> ApprovalResolved {
    ApprovalResolved {
        resolution: Resolution::Cancelled,
        actor: ActorRef {
            kind: ActorKind::PolicyEngine,
            stable_id: "engine".into(),
        },
        user_response: None,
        reason: None,
        cancel_cause: Some(cause),
        decision: None,
    }
}

fn started(call_id: &str, governing_decision_id: &str) -> ToolStarted {
    ToolStarted {
        call_id: call_id.into(),
        governing_decision_id: governing_decision_id.into(),
        executor_id: "exec-1".into(),
        args_digest: "sha256:deadbeef".into(),
    }
}

#[allow(clippy::type_complexity)]
fn ask_allow_started(
    ledger: &mut Ledger,
    acked: &AckedCall,
    id: &str,
) -> (DurableAck, DurableAck, DurableAck, bob_engine::ledger::AckedStart) {
    let d = ledger
        .record_authority_decided(acked, decided(id, DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    let q = ledger
        .record_approval_requested(acked, preview(acked.call_id(), id))
        .unwrap();
    let r = ledger.record_approval_resolved(acked, resolved_allow(id)).unwrap();
    let s = ledger
        .record_tool_started(acked, started(acked.call_id(), id))
        .unwrap();
    (d, q, r, s)
}

fn ok_result(call_id: &str, gid: &str) -> ToolResult {
    let mut merged = decision(gid, DecisionState::Merged, "effect_incorporated", "merged");
    merged.reason.params =
        serde_json::json!({"target_kind": "tool_result", "target_id": "sha256:out"});
    ToolResult {
        call_id: call_id.into(),
        status: ResultStatus::Succeeded,
        output: Some(serde_json::json!({"text": "bob-slice-fixture\n"})),
        output_digest: Some("sha256:out".into()),
        error: None,
        effect_ids: vec![],
        governing_decision_id: Some(gid.into()),
        terminal_decision: Some(merged),
        start_state: StartState::Started,
        model_delivery: ModelDelivery::Deliver,
        origin: None,
        recovered_by: None,
        executor_evidence: None,
    }
}

fn failed_result(call_id: &str, gid: Option<&str>, code: &str, start: StartState) -> ToolResult {
    ToolResult {
        call_id: call_id.into(),
        status: ResultStatus::Failed,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: code.into(),
            message: format!("failed: {code}"),
            retryable: false,
        }),
        effect_ids: vec![],
        governing_decision_id: gid.map(str::to_owned),
        terminal_decision: None,
        start_state: start,
        model_delivery: ModelDelivery::Deliver,
        origin: None,
        recovered_by: None,
        executor_evidence: None,
    }
}

fn denied_result(call_id: &str, gid: &str, code: &str, text: &str) -> ToolResult {
    ToolResult {
        call_id: call_id.into(),
        status: ResultStatus::Denied,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: code.into(),
            message: text.into(),
            retryable: false,
        }),
        effect_ids: vec![],
        governing_decision_id: Some(gid.into()),
        terminal_decision: None,
        start_state: StartState::NotStarted,
        model_delivery: ModelDelivery::Deliver,
        origin: None,
        recovered_by: None,
        executor_evidence: None,
    }
}

fn aborted_result(call_id: &str, gid: Option<&str>, code: &str) -> ToolResult {
    ToolResult {
        call_id: call_id.into(),
        status: ResultStatus::Aborted,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: code.into(),
            message: format!("aborted: {code}"),
            retryable: true,
        }),
        effect_ids: vec![],
        governing_decision_id: gid.map(str::to_owned),
        terminal_decision: None,
        start_state: StartState::NotStarted,
        model_delivery: ModelDelivery::None,
        origin: None,
        recovered_by: None,
        executor_evidence: None,
    }
}

fn indeterminate_result(call_id: &str, gid: &str, origin: IndeterminateOrigin, code: &str) -> ToolResult {
    ToolResult {
        call_id: call_id.into(),
        status: ResultStatus::Indeterminate,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: code.into(),
            message: "the outcome is unknown; this call is never auto-retried".into(),
            retryable: false,
        }),
        effect_ids: vec![],
        governing_decision_id: Some(gid.into()),
        terminal_decision: None,
        start_state: StartState::Unknown,
        model_delivery: ModelDelivery::Deliver,
        origin: Some(origin),
        recovered_by: None,
        executor_evidence: None,
    }
}

// ---------- scripted-run harness (E5 style, s5_protocol.rs) ----------

const FIXTURE_DIR: &str = "tests/fixtures/s5";
const CLI: &str = env!("CARGO_BIN_EXE_bob-slice-cli");
const FX_READ: &str = env!("CARGO_BIN_EXE_fx-read");

fn fixture(name: &str) -> String {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join(FIXTURE_DIR).join(name);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("fixture {p:?}: {e}"))
}

struct Cli {
    child: Child,
    stdin: Option<ChildStdin>,
    rx: Receiver<Value>,
}

impl Drop for Cli {
    fn drop(&mut self) {
        let _ = self.child.kill();
    }
}

impl Cli {
    fn spawn(data_root: &Path) -> Cli {
        let mut child = Command::new(CLI)
            .arg("--data-root")
            .arg(data_root)
            .arg("--fx-read")
            .arg(FX_READ)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("bob-slice-cli spawns");
        let stdin = child.stdin.take().expect("stdin piped");
        let stdout = child.stdout.take().expect("stdout piped");
        let (tx, rx) = channel();
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                let Ok(line) = line else { break };
                if line.trim().is_empty() {
                    continue;
                }
                match serde_json::from_str::<Value>(&line) {
                    Ok(v) => {
                        if tx.send(v).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
        Cli {
            child,
            stdin: Some(stdin),
            rx,
        }
    }

    fn send(&mut self, v: &Value) {
        let mut line = v.to_string();
        line.push('\n');
        let stdin = self.stdin.as_mut().expect("stdin still open");
        stdin.write_all(line.as_bytes()).unwrap();
        stdin.flush().unwrap();
    }

    fn recv(&self) -> Value {
        match self.rx.recv_timeout(Duration::from_secs(30)) {
            Ok(v) => v,
            Err(RecvTimeoutError::Timeout) => panic!("bob-slice-cli stalled (no output for 30s)"),
            Err(RecvTimeoutError::Disconnected) => panic!("bob-slice-cli stdout closed early"),
        }
    }

    fn finish(&mut self) -> std::process::ExitStatus {
        drop(self.stdin.take());
        while self.rx.try_recv().is_ok() {}
        self.child.wait().expect("child exited")
    }
}

fn walk(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                out.extend(walk(&p));
            } else {
                out.push(p);
            }
        }
    }
    out
}

/// Drive one scripted run and return its ledger segment path (E5 harness).
fn scripted_run(tag: &str, fixture_name: &str, resolve_for: fn(&Value) -> Value) -> PathBuf {
    let root = tmp_root(tag);
    let mut cli = Cli::spawn(&root);
    for line in fixture(fixture_name).lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        cli.send(&v);
    }
    let mut preview: Option<Value> = None;
    let mut sent_resolve = false;
    loop {
        let v = cli.recv();
        if v.get("seq").is_some() {
            let event = v["event"].as_str().unwrap().to_owned();
            if event == "approval.requested" {
                preview = Some(v["payload"].clone());
            }
            if event == "turn.completed" {
                let resp = cli.recv();
                assert!(resp.get("seq").is_none(), "turn.start response follows");
                break;
            }
        }
        if let (Some(p), false) = (&preview, sent_resolve) {
            cli.send(&resolve_for(p));
            sent_resolve = true;
        }
    }
    let status = cli.finish();
    assert!(status.success(), "bob-slice-cli exited with {status} on run {tag}");
    let mut seg = None;
    for entry in walk(&root) {
        if entry
            .file_name()
            .map(|n| n.to_string_lossy().ends_with(".audit.jsonl"))
            .unwrap_or(false)
        {
            assert!(seg.is_none(), "one segment per run");
            seg = Some(entry);
        }
    }
    seg.expect("a segment file exists")
}

// ---------- projection helpers ----------

fn proj(tag: &str, seg: &Path) -> (ProjectionReport, PathBuf) {
    let db = tmp_root(&format!("{tag}-db")).join("projection.sqlite");
    let rep = project_segment(seg, &db).unwrap();
    (rep, db)
}

fn qall(db: &Path, sql: &str) -> Vec<Vec<String>> {
    let conn = Connection::open(db).unwrap();
    let mut stmt = conn.prepare(sql).unwrap();
    let ncol = stmt.column_count();
    let rows = stmt
        .query_map([], |row| {
            Ok((0..ncol)
                .map(|i| match row.get_ref(i).unwrap() {
                    ValueRef::Null => "<NULL>".to_owned(),
                    ValueRef::Integer(n) => n.to_string(),
                    ValueRef::Real(f) => f.to_string(),
                    ValueRef::Text(t) => String::from_utf8_lossy(t).into_owned(),
                    ValueRef::Blob(b) => format!("<{} bytes>", b.len()),
                })
                .collect::<Vec<String>>())
        })
        .unwrap();
    rows.map(|r| r.unwrap()).collect()
}

fn q1(db: &Path, sql: &str) -> Vec<String> {
    let mut rows = qall(db, sql);
    assert_eq!(rows.len(), 1, "expected exactly one row for: {sql}");
    rows.remove(0)
}

fn scalar(db: &Path, sql: &str) -> String {
    q1(db, sql).remove(0)
}

/// I-24: the DB `assertion` view must equal the replay scanner's assertions
/// view row for row (order, ids, kinds, verbatim payloads, cursor offsets).
fn assert_view_matches_scanner(db: &Path, seg: &Path) {
    let outcome = replay_segment(seg).unwrap();
    let rows = qall(
        db,
        "SELECT ordinal, event_id, kind, supported, payload_json, byte_offset, segment_id
         FROM assertion ORDER BY line_no",
    );
    assert_eq!(rows.len(), outcome.assertions.len(), "assertion row count");
    for (i, r) in rows.iter().enumerate() {
        let rec = &outcome.assertions[i];
        assert_eq!(r[0], rec.ordinal.to_string(), "ordinal of assertion {i}");
        assert_eq!(r[1], rec.event_id, "event_id of assertion {i}");
        assert_eq!(r[2], rec.kind.as_str(), "kind of assertion {i}");
        assert_eq!(r[3], if rec.kind.is_supported() { "1" } else { "0" });
        assert_eq!(
            r[4],
            serde_json::to_string(&rec.payload).unwrap(),
            "verbatim payload of assertion {i}"
        );
        assert_eq!(
            r[5],
            outcome
                .assertion_offsets
                .get(i)
                .copied()
                .unwrap_or(0)
                .to_string(),
            "byte offset of assertion {i}"
        );
        assert_eq!(r[6], rec.segment_id);
    }
}

/// Deterministic dump of every table and view (I-25 rebuild identity).
fn dump_all(db: &Path) -> String {
    let targets: &[(&str, &str)] = &[
        ("assertion", "line_no"),
        ("diagnostic", "idx"),
        ("projection_diagnostic", "idx"),
        ("tool_call", "ordinal"),
        ("authority_decided", "ordinal"),
        ("approval_request", "ordinal"),
        ("approval_resolve", "ordinal"),
        ("witness", "ordinal"),
        ("tool_result", "ordinal"),
        ("call_validation", "call_id"),
        ("turn", "turn_id"),
        ("projection_cursor", "rowid"),
        ("joined_call", "call_id"),
        ("accepted", "call_id"),
    ];
    let mut out = String::new();
    for (t, order) in targets {
        out.push_str(&format!("== {t}\n"));
        for row in qall(db, &format!("SELECT * FROM {t} ORDER BY {order}")) {
            out.push_str(&row.join("|"));
            out.push('\n');
        }
    }
    out
}

/// Build one synthetic segment per the closure, return its path.
fn build_segment(tag: &str, build: impl FnOnce(&mut Ledger)) -> PathBuf {
    let root = tmp_root(tag);
    let (mut ledger, _) = Ledger::open_new(&root, ids(tag)).unwrap();
    build(&mut ledger);
    ledger.path().to_owned()
}

// ---------- E5 fixture scripted runs ----------

#[test]
fn e2e_allow_fixture_projection_joins_and_accepts() {
    let seg = scripted_run("proj-allow", "allow.requests.jsonl", |preview| {
        json!({
            "id": "req-4",
            "method": "interaction.resolve",
            "params": {
                "call_id": preview["call_id"],
                "decision_id": preview["decision_id"],
                "args_digest": preview["args_digest"],
                "response": "allow_once"
            }
        })
    });
    let (rep, db) = proj("proj-allow", &seg);
    assert!(rep.diagnostics.is_empty());
    assert_eq!(rep.n_assertions, 10, "lineage..turn.end");
    assert_eq!(rep.n_calls, 1);
    assert_eq!(rep.n_accepted, 1);

    // Joined call view: one row, full chain, terminal status per §4a row 11.
    let row = q1(
        &db,
        "SELECT status, valid, accepted, correlation_id, decision_id, witness_event_id,
                requested_event_id, resolved_event_id, result_event_id, error_code,
                output_digest, terminal_decision_state, start_state
         FROM joined_call",
    );
    assert_eq!(row[0], "succeeded");
    assert_eq!(row[1], "1");
    assert_eq!(row[2], "1");
    let call_id = scalar(&db, "SELECT call_id FROM joined_call");
    assert_eq!(row[3], call_id, "correlation_id = call_id (RULING-8)");
    for col in &row[4..9] {
        assert_ne!(col, "<NULL>", "chain links present: {row:?}");
    }
    assert_eq!(row[9], "<NULL>", "no error on success");
    assert_ne!(row[10], "<NULL>");
    assert_eq!(row[11], "merged");
    assert_eq!(row[12], "started");

    // authority_snapshot_event_id joins to the governing step.context (MGR-5).
    assert_eq!(
        scalar(&db, "SELECT authority_snapshot_event_id FROM tool_call"),
        scalar(&db, "SELECT event_id FROM assertion WHERE kind = 'step.context'")
    );

    // Accepted view (I-25): the one call, succeeded.
    let acc = q1(
        &db,
        "SELECT call_id, status, output_digest FROM accepted",
    );
    assert_eq!(acc[0], call_id);
    assert_eq!(acc[1], "succeeded");
    assert_eq!(acc[2], row[10]);

    // Turn projection.
    let turn = q1(&db, "SELECT end_status, valid FROM turn");
    assert_eq!(turn[0], "succeeded");
    assert_eq!(turn[1], "1");

    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn e2e_deny_fixture_projection_denied_is_accepted_truth() {
    let seg = scripted_run("proj-deny", "deny.requests.jsonl", |preview| {
        json!({
            "id": "req-4",
            "method": "interaction.resolve",
            "params": {
                "call_id": preview["call_id"],
                "decision_id": preview["decision_id"],
                "args_digest": preview["args_digest"],
                "response": {"deny": {"reason": "not today"}}
            }
        })
    });
    let (rep, db) = proj("proj-deny", &seg);
    assert!(rep.diagnostics.is_empty());
    assert_eq!(rep.n_assertions, 9);
    assert_eq!(rep.n_accepted, 1, "a VALIDATED denied terminal is accepted truth (EXEC-59)");

    let row = q1(
        &db,
        "SELECT status, valid, accepted, error_code, witness_event_id, resolution, start_state
         FROM joined_call",
    );
    assert_eq!(row[0], "denied");
    assert_eq!(row[1], "1");
    assert_eq!(row[2], "1");
    assert_eq!(row[3], "user_denied");
    assert_eq!(row[4], "<NULL>", "no witness: the tool never ran (RULING-5)");
    assert_eq!(row[5], "deny");
    assert_eq!(row[6], "not_started");

    let acc = q1(&db, "SELECT status FROM accepted");
    assert_eq!(acc[0], "denied");

    let turn = q1(&db, "SELECT end_status, valid FROM turn");
    assert_eq!(turn[0], "failed");
    assert_eq!(turn[1], "1");

    assert_view_matches_scanner(&db, &seg);
}

// ---------- five-status synthetic battery (S1 §4a) ----------

#[test]
fn row1_failed_not_started_unknown_tool() {
    let seg = build_segment("row1", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = l
            .append_tool_call("t", "s", tool_call("c", Disposition::RejectedBeforeStart, &sc))
            .unwrap();
        l.record_pre_witness_result(a, failed_result("c", None, "unknown_tool", StartState::NotStarted))
            .unwrap();
    });
    let (rep, db) = proj("row1", &seg);
    assert_eq!(rep.n_accepted, 1);
    let row = q1(
        &db,
        "SELECT status, start_state, error_code, decision_id, witness_event_id, valid, accepted
         FROM joined_call",
    );
    assert_eq!(row[0], "failed");
    assert_eq!(row[1], "not_started");
    assert_eq!(row[2], "unknown_tool");
    assert_eq!(row[3], "<NULL>");
    assert_eq!(row[4], "<NULL>");
    assert_eq!(row[5], "1");
    assert_eq!(row[6], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row2_denied_rule() {
    let seg = build_segment("row2", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        l.record_authority_decided(&a, decided("dec-1", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#forbid")))
            .unwrap();
        l.record_pre_witness_result(a, denied_result("c", "dec-1", "rule_forbidden", "explanation: rule_forbidden"))
            .unwrap();
    });
    let (_, db) = proj("row2", &seg);
    let row = q1(
        &db,
        "SELECT status, error_code, decided_outcome, decision_id, valid, accepted FROM joined_call",
    );
    assert_eq!(row[0], "denied");
    assert_eq!(row[1], "rule_forbidden");
    assert_eq!(row[2], "deny");
    assert_eq!(row[3], "dec-1");
    assert_eq!(row[4], "1");
    assert_eq!(row[5], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row3_denied_user() {
    let seg = build_segment("row3", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        l.record_authority_decided(&a, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
            .unwrap();
        l.record_approval_requested(&a, preview("c", "dec-1")).unwrap();
        l.record_approval_resolved(&a, resolved_deny_user("dec-1", "user_denied", "user said no"))
            .unwrap();
        l.record_pre_witness_result(a, denied_result("c", "dec-1", "user_denied", "user said no"))
            .unwrap();
    });
    let (_, db) = proj("row3", &seg);
    let row = q1(
        &db,
        "SELECT status, error_code, resolution, witness_event_id, valid, accepted FROM joined_call",
    );
    assert_eq!(row[0], "denied");
    assert_eq!(row[1], "user_denied");
    assert_eq!(row[2], "deny");
    assert_eq!(row[3], "<NULL>");
    assert_eq!(row[4], "1");
    assert_eq!(row[5], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row5_aborted_cancelled_pending() {
    let seg = build_segment("row5", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        l.record_authority_decided(&a, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
            .unwrap();
        l.record_approval_requested(&a, preview("c", "dec-1")).unwrap();
        l.record_approval_resolved(&a, resolved_cancelled(CancelCause::TurnCancelled))
            .unwrap();
        l.record_pre_witness_result(a, aborted_result("c", Some("dec-1"), "cancelled_before_start"))
            .unwrap();
    });
    let (_, db) = proj("row5", &seg);
    let row = q1(
        &db,
        "SELECT status, error_code, resolution, valid, accepted FROM joined_call",
    );
    assert_eq!(row[0], "aborted");
    assert_eq!(row[1], "cancelled_before_start");
    assert_eq!(row[2], "cancelled");
    assert_eq!(row[3], "1");
    assert_eq!(row[4], "1");
    assert_eq!(
        scalar(&db, "SELECT cancel_cause FROM approval_resolve"),
        "turn_cancelled"
    );
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row6_cancel_after_allow_before_witness() {
    let seg = build_segment("row6", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        l.record_authority_decided(&a, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
            .unwrap();
        l.record_approval_requested(&a, preview("c", "dec-1")).unwrap();
        l.record_approval_resolved(&a, resolved_allow("dec-1")).unwrap();
        l.record_pre_witness_result(a, aborted_result("c", Some("dec-1"), "cancelled_before_start"))
            .unwrap();
    });
    let (_, db) = proj("row6", &seg);
    let row = q1(
        &db,
        "SELECT status, error_code, resolution, witness_event_id, valid, accepted FROM joined_call",
    );
    assert_eq!(row[0], "aborted");
    assert_eq!(row[1], "cancelled_before_start");
    assert_eq!(row[2], "allow");
    assert_eq!(row[3], "<NULL>");
    assert_eq!(row[4], "1");
    assert_eq!(row[5], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row7_witness_write_failed_projects_clean_prefix() {
    let root = tmp_root("row7");
    let (mut ledger, _) = Ledger::open_new(&root, ids("row7")).unwrap();
    {
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let a = admitted(&mut ledger, "t", "s", &sc, "c");
        ledger
            .record_authority_decided(&a, decided("dec-1", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
            .unwrap();
        ledger.debug_fail_next_append();
        let err = ledger.record_tool_started(&a, started("c", "dec-1")).unwrap_err();
        assert!(
            matches!(err, bob_engine::ledger::LedgerError::Io(_)),
            "witness write failed"
        );
    }
    let (rep, db) = proj("row7", ledger.path());
    assert!(rep.diagnostics.is_empty(), "the faulted append wrote no bytes");
    assert_eq!(rep.n_calls, 1);
    assert_eq!(rep.n_accepted, 0, "open call is never accepted");
    let row = q1(
        &db,
        "SELECT status, valid, invalid_reason, witness_event_id FROM joined_call",
    );
    assert_eq!(row[0], "<NULL>", "no terminal: the call stays open");
    assert_eq!(row[1], "0");
    assert_eq!(row[2], "open_call");
    assert_eq!(row[3], "<NULL>");
    assert_eq!(scalar(&db, "SELECT COUNT(*) FROM accepted"), "0");
    assert_view_matches_scanner(&db, ledger.path());
    std::fs::remove_dir_all(&root).ok();
}

#[test]
fn row9_failed_handler_error_started() {
    let seg = build_segment("row9", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        l.record_post_witness_result(s, failed_result("c", Some("dec-1"), "handler_error", StartState::Started))
            .unwrap();
    });
    let (_, db) = proj("row9", &seg);
    let row = q1(
        &db,
        "SELECT status, start_state, error_code, witness_event_id, valid, accepted FROM joined_call",
    );
    assert_eq!(row[0], "failed");
    assert_eq!(row[1], "started");
    assert_eq!(row[2], "handler_error");
    assert_ne!(row[3], "<NULL>");
    assert_eq!(row[4], "1");
    assert_eq!(row[5], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row10_failed_incorporation_refused() {
    let seg = build_segment("row10", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        let mut r = failed_result("c", Some("dec-1"), "incorporation_refused", StartState::Started);
        r.terminal_decision = Some(decision("dec-1", DecisionState::Refused, "incorporation_refused", "refused"));
        l.record_post_witness_result(s, r).unwrap();
    });
    let (_, db) = proj("row10", &seg);
    let row = q1(
        &db,
        "SELECT status, error_code, terminal_decision_state, governing_decision_id, valid, accepted
         FROM joined_call",
    );
    assert_eq!(row[0], "failed");
    assert_eq!(row[1], "incorporation_refused");
    assert_eq!(row[2], "refused");
    assert_eq!(row[3], "dec-1");
    assert_eq!(row[4], "1");
    assert_eq!(row[5], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row11_succeeded() {
    let seg = build_segment("row11", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        l.record_post_witness_result(s, ok_result("c", "dec-1")).unwrap();
    });
    let (_, db) = proj("row11", &seg);
    let row = q1(
        &db,
        "SELECT joined_call.status, joined_call.output_digest,
                joined_call.terminal_decision_state, tool_result.terminal_target_id,
                valid, accepted
         FROM joined_call LEFT JOIN tool_result ON tool_result.call_id = joined_call.call_id",
    );
    assert_eq!(row[0], "succeeded");
    assert_eq!(row[1], row[3], "output_digest == merged target (I-14)");
    assert_eq!(row[2], "merged");
    assert_eq!(row[4], "1");
    assert_eq!(row[5], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row12_indeterminate_cancelled_after_witness() {
    let seg = build_segment("row12", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        l.record_post_witness_result(s, indeterminate_result("c", "dec-1", IndeterminateOrigin::Live, "cancelled_after_witness"))
            .unwrap();
    });
    let (_, db) = proj("row12", &seg);
    let row = q1(
        &db,
        "SELECT status, start_state, error_code, witness_event_id, valid, accepted FROM joined_call",
    );
    assert_eq!(row[0], "indeterminate");
    assert_eq!(row[1], "unknown");
    assert_eq!(row[2], "cancelled_after_witness");
    assert_ne!(row[3], "<NULL>");
    assert_eq!(row[4], "1");
    assert_eq!(row[5], "1");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn row13_indeterminate_executor_lost() {
    let seg = build_segment("row13", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        l.record_post_witness_result(s, indeterminate_result("c", "dec-1", IndeterminateOrigin::Live, "executor_lost"))
            .unwrap();
    });
    let (_, db) = proj("row13", &seg);
    let row = q1(&db, "SELECT status, error_code, valid, accepted FROM joined_call");
    assert_eq!(row[0], "indeterminate");
    assert_eq!(row[1], "executor_lost");
    assert_eq!(row[2], "1");
    assert_eq!(row[3], "1");
    assert_view_matches_scanner(&db, &seg);
}

// ---------- crash-point matrix (E2 helpers) ----------

#[test]
fn cp1_cp4_cp5_cp6_cp7_recovery_terminals_project_accepted() {
    // (tag, prefix-builder, expected status, expected error code)
    let cases: &[(&str, fn(&mut Ledger), &str, &str)] = &[
        // CP-1: crash after tool.call → aborted/recovered_unwitnessed.
        ("cp1", |l| {
            let (_t, sc) = prefix(l, "t", "s");
            let _ = admitted(l, "t", "s", &sc, "c");
        }, "aborted", "recovered_unwitnessed"),
        // CP-4: crash after a durable deny → denied.
        ("cp4", |l| {
            let (_t, sc) = prefix(l, "t", "s");
            let a = admitted(l, "t", "s", &sc, "c");
            l.record_authority_decided(&a, decided("dec-1", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#forbid")))
                .unwrap();
        }, "denied", "rule_forbidden"),
        // CP-5: crash after resolved{allow}, no witness → aborted.
        ("cp5", |l| {
            let (_t, sc) = prefix(l, "t", "s");
            let a = admitted(l, "t", "s", &sc, "c");
            l.record_authority_decided(&a, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
                .unwrap();
            l.record_approval_requested(&a, preview("c", "dec-1")).unwrap();
            l.record_approval_resolved(&a, resolved_allow("dec-1")).unwrap();
        }, "aborted", "recovered_unwitnessed"),
        // CP-6/CP-7: crash after the witness → indeterminate/outcome_unknown
        // (kill before vs after invocation are byte-identical, I-05b).
        ("cp67", |l| {
            let (_t, sc) = prefix(l, "t", "s");
            let a = admitted(l, "t", "s", &sc, "c");
            let _ = ask_allow_started(l, &a, "dec-1");
        }, "indeterminate", "outcome_unknown"),
    ];
    for (tag, build, status, code) in cases {
        let root = tmp_root(tag);
        let idset = ids(tag);
        {
            let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
            build(&mut ledger);
        } // kill -9
        let (mut ledger, _resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
        let acks = ledger.synthesize_recovery_results().unwrap();
        assert_eq!(acks.len(), 1, "{tag}: one synthesized result");

        let (rep, db) = proj(tag, ledger.path());
        assert_eq!(rep.n_calls, 1);
        assert_eq!(rep.n_accepted, 1, "{tag}: the recovered terminal is validated");
        let row = q1(
            &db,
            "SELECT status, error_code, valid, accepted, recovered_by FROM joined_call",
        );
        assert_eq!(row[0], *status, "{tag}");
        assert_eq!(row[1], *code, "{tag}");
        assert_eq!(row[2], "1", "{tag}");
        assert_eq!(row[3], "1", "{tag}");
        assert_ne!(row[4], "<NULL>", "{tag}: recovered_by pins the resume lineage");
        assert_view_matches_scanner(&db, ledger.path());
        std::fs::remove_dir_all(&root).ok();
    }
}

#[test]
fn cp3_pending_approval_projects_cancelled_then_aborted() {
    let root = tmp_root("cp3p");
    let idset = ids("cp3p");
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let a = admitted(&mut ledger, "t", "s", &sc, "c");
        ledger
            .record_authority_decided(&a, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
            .unwrap();
        ledger.record_approval_requested(&a, preview("c", "dec-1")).unwrap();
    }
    let (mut ledger, _resume) = Ledger::open_resume(&root, idset).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 2, "cancelled resolve + aborted result");

    let (_, db) = proj("cp3p", ledger.path());
    let row = q1(
        &db,
        "SELECT status, resolution, valid, accepted FROM joined_call",
    );
    assert_eq!(row[0], "aborted");
    assert_eq!(row[1], "cancelled");
    assert_eq!(row[2], "1");
    assert_eq!(row[3], "1");
    assert_eq!(scalar(&db, "SELECT cancel_cause FROM approval_resolve"), "recovery");
    assert_view_matches_scanner(&db, ledger.path());
    std::fs::remove_dir_all(&root).ok();
}

#[test]
fn cp8_terminal_respected_no_second_result() {
    let root = tmp_root("cp8p");
    let idset = ids("cp8p");
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let a = admitted(&mut ledger, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(&mut ledger, &a, "dec-1");
        ledger.record_post_witness_result(s, ok_result("c", "dec-1")).unwrap();
    }
    let (mut ledger, _resume) = Ledger::open_resume(&root, idset).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert!(acks.is_empty(), "closed calls synthesize nothing");

    let (rep, db) = proj("cp8p", ledger.path());
    assert_eq!(rep.n_accepted, 1);
    let row = q1(
        &db,
        "SELECT status, n_results, valid, accepted, recovered_by FROM joined_call",
    );
    assert_eq!(row[0], "succeeded");
    assert_eq!(row[1], "1", "the durable terminal is respected (I-12)");
    assert_eq!(row[2], "1");
    assert_eq!(row[3], "1");
    assert_eq!(row[4], "<NULL>", "live result carries no recovered_by");
    assert_view_matches_scanner(&db, ledger.path());
    std::fs::remove_dir_all(&root).ok();
}

#[test]
fn cp9_torn_tail_projects_clean_prefix_and_records_diagnostic() {
    let root = tmp_root("cp9p");
    let idset = ids("cp9p");
    let seg = {
        let (mut ledger, _) = Ledger::open_new(&root, idset).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let _ = admitted(&mut ledger, "t", "s", &sc, "c");
        ledger.path().to_owned()
    };
    // Kill mid-write: append an unterminated partial line (CP-9).
    let clean_len = std::fs::metadata(&seg).unwrap().len();
    let mut f = std::fs::OpenOptions::new().append(true).open(&seg).unwrap();
    f.write_all(b"{\"schema_version\":\"1.0\",\"ordi").unwrap();
    drop(f);

    let (rep, db) = proj("cp9p", &seg);
    assert_eq!(rep.n_assertions, 4, "lineage..tool.call prefix only");
    assert_eq!(
        rep.diagnostics.len(),
        1,
        "ledger_tail_corrupt and nothing else"
    );
    assert!(matches!(
        rep.diagnostics[0],
        bob_engine::replay::ReplayDiagnostic::LedgerTailCorrupt { byte_offset, last_ordinal: Some(4) }
            if byte_offset == clean_len
    ));
    // The clean prefix projects; the open call never reaches accepted truth.
    let row = q1(&db, "SELECT status, valid, invalid_reason FROM joined_call");
    assert_eq!(row[0], "<NULL>");
    assert_eq!(row[1], "0");
    assert_eq!(row[2], "open_call");
    assert_eq!(scalar(&db, "SELECT COUNT(*) FROM accepted"), "0");
    let diag = q1(&db, "SELECT kind, detail FROM diagnostic");
    assert_eq!(diag[0], "ledger_tail_corrupt");
    assert!(diag[1].contains(&format!("\"byte_offset\":{clean_len}")));
    assert_eq!(
        scalar(&db, "SELECT COUNT(*) FROM assertion WHERE kind = 'tool.result'"),
        "0",
        "never a partial record"
    );
    assert_view_matches_scanner(&db, &seg);
    std::fs::remove_dir_all(&root).ok();
}

// ---------- rebuildability (I-25, AT-S1-02.10) ----------

#[test]
fn delete_db_and_rebuild_from_bytes_is_identical() {
    // A synthetic segment with two calls (succeeded + denied) in one turn.
    let seg = build_segment("rebuild", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c1");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        l.record_post_witness_result(s, ok_result("c1", "dec-1")).unwrap();
        let b = admitted(l, "t", "s", &sc, "c2");
        l.record_authority_decided(&b, decided("dec-2", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#forbid")))
            .unwrap();
        l.record_pre_witness_result(b, denied_result("c2", "dec-2", "rule_forbidden", "explanation: rule_forbidden"))
            .unwrap();
        l.append_turn_end("t", TurnStatus::Failed, Some("s"), None).unwrap();
    });
    assert_eq!(
        scalar_proj(&seg, "t"),
        "2",
        "both calls accepted (succeeded + validated denied, EXEC-59)"
    );

    let db = tmp_root("rebuild-db").join("projection.sqlite");
    let _ = project_segment(&seg, &db).unwrap();
    let before = dump_all(&db);
    assert!(before.contains("== accepted"));

    // The DB is disposable: delete it, rebuild from the segment bytes alone,
    // and every view must be identical (I-25, AT-S1-02.10).
    std::fs::remove_file(&db).unwrap();
    let _ = project_segment(&seg, &db).unwrap();
    let after = dump_all(&db);
    assert_eq!(before, after, "rebuild from bytes is view-identical");
}

fn scalar_proj(seg: &Path, tag: &str) -> String {
    let db = tmp_root(&format!("{tag}-q")).join("projection.sqlite");
    let _ = project_segment(seg, &db).unwrap();
    scalar(&db, "SELECT COUNT(*) FROM accepted")
}

#[test]
fn rebuild_identity_also_holds_for_the_deny_fixture_run() {
    let seg = scripted_run("rebuild-deny", "deny.requests.jsonl", |preview| {
        json!({
            "id": "req-4",
            "method": "interaction.resolve",
            "params": {
                "call_id": preview["call_id"],
                "decision_id": preview["decision_id"],
                "args_digest": preview["args_digest"],
                "response": {"deny": {"reason": "not today"}}
            }
        })
    });
    let db = tmp_root("rebuild-deny-db").join("projection.sqlite");
    let _ = project_segment(&seg, &db).unwrap();
    let before = dump_all(&db);
    std::fs::remove_file(&db).unwrap();
    let _ = project_segment(&seg, &db).unwrap();
    assert_eq!(before, dump_all(&db));
}

// ---------- invalid_success / validation (I-24, LT-5) ----------

/// A fresh succeeded segment (S1 §4a row 11) as mutable text.
fn succeeded_segment_text(tag: &str) -> String {
    let seg = build_segment(tag, |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        l.record_post_witness_result(s, ok_result("c", "dec-1")).unwrap();
    });
    std::fs::read_to_string(&seg).unwrap()
}

fn write_variant(tag: &str, text: &str) -> PathBuf {
    let dir = tmp_root(tag);
    let p = dir.join("variant.audit.jsonl");
    std::fs::write(&p, text).unwrap();
    p
}

#[test]
fn mutated_output_digest_is_invalid_success() {
    let text = succeeded_segment_text("badgs");
    let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
    let idx = lines
        .iter()
        .position(|l| l.contains("\"tool.result\""))
        .expect("one result line");
    let mut rec: Record = serde_json::from_str(&lines[idx]).unwrap();
    rec.payload["output_digest"] = json!("sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff");
    lines[idx] = serde_json::to_string(&rec).unwrap();
    let seg = write_variant("badgs", &format!("{}\n", lines.join("\n")));

    let (rep, db) = proj("badgs", &seg);
    assert!(rep.diagnostics.is_empty(), "still parseable");
    let row = q1(&db, "SELECT status, valid, invalid_reason, accepted FROM joined_call");
    assert_eq!(row[0], "succeeded");
    assert_eq!(row[1], "0");
    assert_eq!(row[2], "invalid_success", "output_digest != merged target (I-24)");
    assert_eq!(row[3], "0");
    assert_eq!(scalar(&db, "SELECT COUNT(*) FROM accepted"), "0");
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn hand_appended_duplicate_result_is_not_accepted() {
    let text = succeeded_segment_text("dupres");
    let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
    let idx = lines
        .iter()
        .position(|l| l.contains("\"tool.result\""))
        .expect("one result line");
    let mut rec: Record = serde_json::from_str(&lines[idx]).unwrap();
    let next_ordinal = rec.ordinal + 1;
    rec.ordinal = next_ordinal;
    rec.event_id = "0bad0bad-0bad-40bad-80bad-0bad0bad0bad".to_owned();
    lines.push(serde_json::to_string(&rec).unwrap());
    let seg = write_variant("dupres", &format!("{}\n", lines.join("\n")));

    let (_, db) = proj("dupres", &seg);
    let row = q1(&db, "SELECT valid, invalid_reason, accepted, n_results, status FROM joined_call");
    assert_eq!(row[0], "0");
    assert_eq!(row[1], "duplicate_result", "first durable wins (I-12, LT-5)");
    assert_eq!(row[2], "0");
    assert_eq!(row[3], "2");
    assert_eq!(row[4], "succeeded", "the FIRST result is the joined terminal");
    assert_eq!(scalar(&db, "SELECT COUNT(*) FROM accepted"), "0");
    assert_eq!(
        scalar(&db, "SELECT COUNT(*) FROM tool_result"),
        "2",
        "both result assertions stay visible raw"
    );
    assert_view_matches_scanner(&db, &seg);
}

#[test]
fn witness_line_removed_invalidates_the_success() {
    let text = succeeded_segment_text("nowit");
    let filtered: Vec<&str> = text
        .lines()
        .filter(|l| !l.contains("\"tool.started\""))
        .collect();
    let seg = write_variant("nowit", &format!("{}\n", filtered.join("\n")));

    let (_, db) = proj("nowit", &seg);
    let row = q1(
        &db,
        "SELECT status, witness_event_id, valid, invalid_reason, accepted FROM joined_call",
    );
    assert_eq!(row[0], "succeeded");
    assert_eq!(row[1], "<NULL>");
    assert_eq!(row[2], "0");
    // The dangling result.caused_by is caught first; the success itself is
    // also invalid (no witness). Both are recorded.
    assert_eq!(row[3], "caused_by_chain");
    assert_eq!(row[4], "0");
    assert_eq!(scalar(&db, "SELECT COUNT(*) FROM accepted"), "0");
    // The ordinal gap is diagnosed but permitted (I-03) — it is not what
    // invalidates the call.
    assert_eq!(scalar(&db, "SELECT kind FROM diagnostic"), "ordinal_gap");
    assert!(
        scalar(&db, "SELECT COUNT(*) FROM projection_diagnostic WHERE kind = 'invalid_success'")
            == "1"
    );
    assert_view_matches_scanner(&db, &seg);
}

// ---------- cursor (I-25) ----------

#[test]
fn cursor_pins_segment_ordinal_and_byte_offset() {
    let seg = build_segment("cursor", |l| {
        let (_t, sc) = prefix(l, "t", "s");
        let a = admitted(l, "t", "s", &sc, "c");
        let (_, _, _, s) = ask_allow_started(l, &a, "dec-1");
        l.record_post_witness_result(s, ok_result("c", "dec-1")).unwrap();
    });
    let (rep, db) = proj("cursor", &seg);
    let cursor = q1(&db, "SELECT segment_id, ordinal, byte_offset FROM projection_cursor");
    assert_eq!(cursor[0], rep.segment_id);
    assert_eq!(cursor[0], "seg-cursor");
    // The cursor pins the LAST projected record: its ordinal and the byte
    // offset of its line, matching the scanner's lockstep offsets.
    let last = q1(
        &db,
        "SELECT ordinal, byte_offset FROM assertion ORDER BY line_no DESC LIMIT 1",
    );
    assert_eq!(cursor[1], last[0]);
    assert_eq!(cursor[2], last[1]);
    let outcome = replay_segment(&seg).unwrap();
    assert_eq!(
        cursor[1],
        outcome.assertions.last().unwrap().ordinal.to_string()
    );
    assert_eq!(
        cursor[2],
        outcome
            .assertion_offsets
            .last()
            .copied()
            .unwrap()
            .to_string()
    );
}
