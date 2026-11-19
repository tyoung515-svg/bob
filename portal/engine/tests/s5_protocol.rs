//! S5 protocol + headless end-to-end tests (E5): the NDJSON frame envelope
//! of the `slice-v1` event set, the request/response wire, and the
//! `bob-slice-cli` binary driving ONE read-only FX-READ-01 call end-to-end
//! S1(ledger) → S2(authority) → S4(exec) → S5(protocol) — the overnight
//! MVP's guaranteed deliverable.
//!
//! Unit battery (library): schema hash stability, retired-frame rejection
//! (D-43), frame envelope fields, hello/resolve/ack parsing (N1 unknown
//! minor field ignored, reason cap), abort_cause mapping, tool.result frame
//! nullability, SegmentReader read-back.
//!
//! End-to-end battery (spawned binary + committed fixtures):
//! - run (a) happy path — `allow_once`, cat the fixture file, success
//!   result: full §4 frame order, gap-free `seq`, RULING-8 causal chain,
//!   then read-only ledger replay asserting the expected record sequence
//!   (ordinals, kinds, caused_by chain, digests).
//! - run (b) denial path — user denies with a reason: no execution, correct
//!   terminal records, `deny.reason` lands in `approval.resolved.reason`.
//! - AT-S5-06 binding: mismatched echo → `approval_mismatch`, nothing
//!   appended, request stays pending; late resolve → `approval_closed`.
//! - `version_mismatch` closes the connection pre-handshake.
//!
//! E10 negatives (item 2): duplicate `request_id` replay vs the EXEC-43
//! uncached request-shape errors, `invalid_limits`, and the malformed
//! pre-handshake shapes (not JSON, not an object, not `hello`, no version,
//! empty stdin) - each asserted on the wire AND on the exit code, with no
//! ledger created behind a failed handshake.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::time::Duration;

use serde_json::{json, Value};

use bob_engine::digest::{jcs_sha256, sha256_digest};
use bob_engine::envelope::Record;
use bob_engine::protocol::{
    self as s5, error_line, parse_event_ack, parse_hello, parse_request, parse_resolve,
    result_line, schema_hash, Frame, SegmentReader, WireError,
};
use bob_engine::replay::{replay_segment, ReplayOutcome};

const FIXTURE_DIR: &str = "tests/fixtures/s5";
const CLI: &str = env!("CARGO_BIN_EXE_bob-slice-cli");
const FX_READ: &str = env!("CARGO_BIN_EXE_fx-read");
const FIXTURE_BYTES: &[u8] = b"bob-slice-fixture\n";

fn tmp_root(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e5-{tag}-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn fixture(name: &str) -> String {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join(FIXTURE_DIR).join(name);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("fixture {p:?}: {e}"))
}

fn expected(name: &str) -> Value {
    serde_json::from_str(&fixture(name)).unwrap()
}

// ---------- unit battery ----------

#[test]
fn schema_hash_is_stable_sha256() {
    let h = schema_hash();
    assert!(h.starts_with("sha256:"));
    assert_eq!(h.len(), "sha256:".len() + 64);
    assert!(h[7..].chars().all(|c| c.is_ascii_hexdigit()));
    // Deterministic: the description is a pure function.
    assert_eq!(h, schema_hash());
    // And it actually binds the description: mutating it changes the hash.
    let mut d = s5::schema_description();
    d["event_set"] = json!("other-v9");
    assert_ne!(
        sha256_digest(&bob_engine::digest::jcs_bytes(&d).unwrap()),
        h
    );
}

#[test]
fn retired_frames_are_not_slice_events() {
    for name in s5::RETIRED_EVENTS {
        assert!(!s5::is_slice_event(name), "{name} must be retired (D-43)");
    }
    for name in s5::EVENTS {
        assert!(s5::is_slice_event(name));
    }
}

#[test]
fn frame_envelope_carries_the_full_field_set() {
    let rec: Record = serde_json::from_value(json!({
        "schema_version": "1.0",
        "event_id": "0b9f6bd2-1c1e-4d3f-8a1e-3f2c9b7d5a11",
        "timestamp": "2026-10-17T00:00:00.000Z",
        "ordinal": "7",
        "session_id": "sess", "thread_id": "thr", "lineage_id": "lin", "segment_id": "seg",
        "turn_id": "turn-1", "step_id": "step-1",
        "actor": "engine", "kind": "tool.result",
        "correlation_id": "call-1", "caused_by": "prev-1",
        "payload": {"status": "succeeded"}
    }))
    .unwrap();
    let f = Frame::from_record(3, &rec, "tool.result", json!({"status": "succeeded"}));
    let line = f.to_line().unwrap();
    assert!(line.ends_with('\n'));
    let v: Value = serde_json::from_str(line.trim_end()).unwrap();
    // serde_json serializes objects in sorted key order; compare as sets.
    let mut keys = v
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect::<Vec<_>>();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "caused_by", "correlation_id", "event", "event_id", "ordinal", "payload",
            "schema_version", "seq", "ts"
        ]
    );
    assert_eq!(v["seq"], 3);
    assert_eq!(v["ordinal"], "7"); // u64-string, base 1
    assert_eq!(v["ts"], "2026-10-17T00:00:00.000Z");
    assert_eq!(v["correlation_id"], "call-1"); // = call_id on call events
    assert_eq!(v["caused_by"], "prev-1");
}

#[test]
fn hello_parsing_ignores_unknown_minor_and_rejects_major() {
    // N1: unknown minor fields are ignored.
    parse_hello(&json!({
        "version": {"major": 2, "minor": 9, "future_field": true},
        "limits": {"max_frame_bytes": 1048576},
        "some_future_section": {"x": 1}
    }))
    .unwrap();
    // N2-equivalent on the wire: wrong major is a terminal mismatch.
    let e = parse_hello(&json!({"version": {"major": 3, "minor": 0}})).unwrap_err();
    assert_eq!(e.code, s5::wire_code::VERSION_MISMATCH);
    // Non-object limits → invalid_limits.
    let e = parse_hello(&json!({"version": {"major": 2}, "limits": 5})).unwrap_err();
    assert_eq!(e.code, s5::wire_code::INVALID_LIMITS);
    // Missing version → invalid_request.
    assert!(parse_hello(&json!({})).is_err());
}

#[test]
fn resolve_parsing_forms_and_reason_cap() {
    let p = parse_resolve(&json!({
        "call_id": "call-1", "decision_id": "d", "args_digest": "sha256:x",
        "response": "allow_once"
    }))
    .unwrap();
    assert_eq!(p.response, s5::ResolveResponse::AllowOnce);

    let p = parse_resolve(&json!({
        "call_id": "call-1", "decision_id": "d", "args_digest": "sha256:x",
        "response": {"deny": {"reason": "not today"}}
    }))
    .unwrap();
    assert_eq!(
        p.response,
        s5::ResolveResponse::Deny {
            reason: Some("not today".into())
        }
    );

    // Shorthand {"deny": "<reason>"} and reasonless deny are both legal.
    assert!(matches!(
        parse_resolve(&json!({
            "call_id": "c", "decision_id": "d", "args_digest": "sha256:x",
            "response": {"deny": "why"}
        }))
        .unwrap()
        .response,
        s5::ResolveResponse::Deny { reason: Some(_) }
    ));
    assert!(matches!(
        parse_resolve(&json!({
            "call_id": "c", "decision_id": "d", "args_digest": "sha256:x",
            "response": {"deny": {}}
        }))
        .unwrap()
        .response,
        s5::ResolveResponse::Deny { reason: None }
    ));

    // RULING-7: the reason is capped at 1024 bytes.
    let long = "x".repeat(1025);
    let e = parse_resolve(&json!({
        "call_id": "c", "decision_id": "d", "args_digest": "sha256:x",
        "response": {"deny": {"reason": long}}
    }))
    .unwrap_err();
    assert_eq!(e.code, s5::wire_code::INVALID_REQUEST);

    // Malformed response shape.
    assert!(parse_resolve(&json!({
        "call_id": "c", "decision_id": "d", "args_digest": "sha256:x",
        "response": "always_allow"
    }))
    .is_err());
}

#[test]
fn event_ack_parsing() {
    assert_eq!(parse_event_ack(&json!({"through_seq": 3})).unwrap(), 3);
    assert!(parse_event_ack(&json!({"through_seq": -1})).is_err());
    assert!(parse_event_ack(&json!({})).is_err());
}

#[test]
fn abort_cause_maps_the_in3_set() {
    assert_eq!(s5::abort_cause("cancelled"), Some("cancelled"));
    assert_eq!(
        s5::abort_cause("witness_write_failed"),
        Some("witness_write_failed")
    );
    assert_eq!(
        s5::abort_cause("isolation_unavailable"),
        Some("preflight_failed")
    );
    assert_eq!(s5::abort_cause("execution_failed"), None);
}

#[test]
fn tool_result_frame_nullability() {
    // succeeded ⇒ error=null, output a ContentValue, no abort_cause key.
    let ok = s5::tool_result_frame_payload(
        &json!({
            "status": "succeeded",
            "output": {"kind": "inline", "text": "bob-slice-fixture\n"},
            "error": null,
            "output_digest": "sha256:abc",
            "terminal_decision": {"decision_id": "d", "state": "merged"}
        }),
        Some("witness-1"),
    );
    assert_eq!(ok["status"], "succeeded");
    assert_eq!(ok["error"], Value::Null);
    assert_eq!(ok["output"]["text"], "bob-slice-fixture\n");
    assert!(ok.get("abort_cause").is_none());
    assert_eq!(ok["authority_terminal"]["state"], "merged");

    // denied ⇒ output=null + OperationError with related_ids = caused_by.
    let denied = s5::tool_result_frame_payload(
        &json!({
            "status": "denied",
            "output": null,
            "error": {"code": "user_denied", "message": "denied by the user", "retryable": false}
        }),
        Some("resolved-9"),
    );
    assert_eq!(denied["output"], Value::Null);
    assert_eq!(denied["error"]["code"], "user_denied");
    assert_eq!(denied["error"]["retryable"], false);
    assert_eq!(denied["error"]["related_ids"], json!(["resolved-9"]));

    // aborted ⇒ abort_cause present, error kept.
    let aborted = s5::tool_result_frame_payload(
        &json!({
            "status": "aborted",
            "output": null,
            "error": {"code": "cancelled", "message": "cancelled", "retryable": true}
        }),
        None,
    );
    assert_eq!(aborted["abort_cause"], "cancelled");
    assert_eq!(aborted["error"]["retryable"], true);
}

#[test]
fn request_response_line_roundtrip() {
    let r = parse_request("{\"id\":\"r1\",\"method\":\"hello\",\"params\":{\"version\":{\"major\":2}}}\r\n").unwrap();
    assert_eq!(r.id, "r1");
    assert_eq!(r.method, "hello");
    assert_eq!(r.params["version"]["major"], 2);
    // id too long → invalid_request.
    assert!(parse_request(&json!({"id": "x".repeat(129), "method": "event.ack"}).to_string()).is_err());
    // Exactly one of result/error on every response line.
    let ok: Value = serde_json::from_str(result_line("r1", json!({"a": 1})).trim()).unwrap();
    assert!(ok.get("result").is_some() && ok.get("error").is_none());
    let err: Value = serde_json::from_str(
        error_line(Some("r1"), &WireError::new(s5::wire_code::APPROVAL_CLOSED, "no")).trim(),
    )
    .unwrap();
    assert!(err.get("error").is_some() && err.get("result").is_none());
    assert_eq!(err["id"], "r1");
}

#[test]
fn segment_reader_reads_back_only_complete_lines() {
    let root = tmp_root("segreader");
    let ids = bob_engine::ledger::SessionIds {
        session_id: "sess".into(),
        thread_id: "thr".into(),
        lineage_id: "lin".into(),
        segment_id: "seg".into(),
    };
    let (mut ledger, lineage_ack) =
        bob_engine::ledger::Ledger::open_new(&root, ids).unwrap();
    let turn_ack = ledger
        .append_turn_start("turn-1", bob_engine::payload::TurnStart {
            input_digest: "sha256:x".into(),
            parent_turn_id: None,
        })
        .unwrap();

    let mut reader = SegmentReader::new(ledger.path().to_path_buf());
    reader.refresh().unwrap();
    assert_eq!(reader.len(), 2);
    assert_eq!(
        reader.by_event_id(&lineage_ack.event_id).unwrap().kind.as_str(),
        "lineage"
    );
    assert_eq!(
        reader.by_event_id(&turn_ack.event_id).unwrap().kind.as_str(),
        "turn.start"
    );
    assert!(reader.by_event_id("nonexistent").is_none());

    // A torn tail (unterminated final line) is not a record: append raw
    // partial bytes to the file, refresh, and nothing new is picked up.
    use std::io::Write as _;
    let mut f = std::fs::OpenOptions::new()
        .append(true)
        .open(ledger.path())
        .unwrap();
    f.write_all(br#"{"schema_version":"1.0""#).unwrap();
    drop(f);
    reader.refresh().unwrap();
    assert_eq!(reader.len(), 2);
}

// ---------- end-to-end battery ----------

/// A spawned bob-slice-cli with line-at-a-time stdout reading on a thread
/// (30 s stall guard) and stdin held for scripted writes.
struct Cli {
    child: Child,
    stdin: Option<ChildStdin>,
    rx: Receiver<Value>,
}

impl Drop for Cli {
    fn drop(&mut self) {
        // Kill on panic-drop so a hung or failed assertion cannot leave an
        // orphan holding the pipes (or locking the test binary on rebuild).
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

    /// Next stdout value (frame or response); panics on a 30 s stall.
    fn recv(&self) -> Value {
        match self.rx.recv_timeout(Duration::from_secs(30)) {
            Ok(v) => v,
            Err(RecvTimeoutError::Timeout) => panic!("bob-slice-cli stalled (no output for 30s)"),
            Err(RecvTimeoutError::Disconnected) => panic!("bob-slice-cli stdout closed early"),
        }
    }

    /// Next value, asserting it is a response with this id.
    fn recv_response(&self, id: &str) -> Value {
        let v = self.recv();
        assert!(
            v.get("seq").is_none(),
            "expected the {id} response, got a frame: {v}"
        );
        assert_eq!(v["id"], id);
        v
    }

    fn finish(&mut self) -> std::process::ExitStatus {
        // Close OUR end of the stdin pipe first: the CLI reads EOF and
        // exits. (Leaving it to struct drop would be too late — that runs
        // only after `wait` returns, which would deadlock.)
        drop(self.stdin.take());
        while self.rx.try_recv().is_ok() {} // late lines drained, not asserted
        self.child.wait().expect("child exited")
    }
}

/// The stdout of a completed run: ordered frames and responses.
#[derive(Default, Debug)]
struct RunLog {
    frames: Vec<Value>,
    responses: Vec<Value>,
}

impl RunLog {
    fn frames_of(&self, event: &str) -> Vec<&Value> {
        self.frames
            .iter()
            .filter(|f| f["event"] == event)
            .collect()
    }
    fn response_of(&self, id: &str) -> &Value {
        self.responses.iter().find(|r| r["id"] == id).unwrap()
    }
}

/// Drive one scripted run: send the fixture prefix; when the
/// `approval.requested` frame arrives, let `resolve_for` build the
/// interaction.resolve params from the preview payload; collect everything
/// until the `turn.completed` frame AND the req-3 response have arrived.
fn scripted_run(tag: &str, fixture_name: &str, resolve_for: fn(&Value) -> Value) -> (RunLog, PathBuf) {
    let root = tmp_root(tag);
    let mut cli = Cli::spawn(&root);
    for line in fixture(fixture_name).lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        cli.send(&v);
    }
    let mut log = RunLog::default();
    let mut preview: Option<Value> = None;
    let mut sent_resolve = false;
    loop {
        let v = cli.recv();
        if v.get("seq").is_some() {
            let event = v["event"].as_str().unwrap().to_owned();
            if event == "approval.requested" {
                preview = Some(v["payload"].clone());
            }
            let is_terminal = event == "turn.completed";
            log.frames.push(v);
            if is_terminal {
                // The turn.start response follows immediately; take it too.
                let resp = cli.recv();
                assert!(resp.get("seq").is_none());
                log.responses.push(resp);
                break;
            }
        } else {
            log.responses.push(v);
        }
        if let (Some(p), false) = (&preview, sent_resolve) {
            cli.send(&resolve_for(p));
            sent_resolve = true;
        }
    }
    let status = cli.finish();
    assert!(
        status.success(),
        "bob-slice-cli exited with {status} on run {tag}"
    );
    // Locate the single ledger segment under the data root.
    let mut seg = None;
    for entry in walk(&root) {
        if entry.extension().map(|e| e == "jsonl").unwrap_or(false)
            && entry
                .file_name()
                .map(|n| n.to_string_lossy().ends_with(".audit.jsonl"))
                .unwrap_or(false)
        {
            assert!(seg.is_none(), "one segment per run");
            seg = Some(entry);
        }
    }
    (log, seg.expect("a segment file exists"))
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

/// Shared post-run verification: envelope discipline, §4 order, gap-free
/// seq, RULING-8 causal chain, then read-only ledger replay asserting the
/// expected record sequence (ordinals, kinds, caused_by, digests).
fn verify_run(log: &RunLog, seg: &Path, exp: &Value, resolve_response: Value) {
    let frame_names: Vec<&str> = log
        .frames
        .iter()
        .map(|f| f["event"].as_str().unwrap())
        .collect();
    let exp_frames: Vec<&str> = exp["frames"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(frame_names, exp_frames, "frame sequence (S5 §4 order)");

    // Gap-free seq starting at 1 (S5 §1); envelope fields on every frame.
    for (i, f) in log.frames.iter().enumerate() {
        assert_eq!(f["seq"].as_u64().unwrap(), (i + 1) as u64, "seq must be gap-free from 1");
        assert_eq!(f["schema_version"], "1.0");
        let eid = f["event_id"].as_str().unwrap();
        assert_eq!(eid.len(), 36, "uuidv4 event_id");
        assert_eq!(eid.matches('-').count(), 4);
        let ts = f["ts"].as_str().unwrap();
        assert_eq!(ts.len(), 24, "fixed-width ms-Z timestamp");
        assert!(ts.ends_with('Z'));
        assert!(f["ordinal"].as_str().is_some(), "ordinal is a u64-string");
    }

    // The six call events carry correlation_id = call_id; turn frames null.
    for f in &log.frames {
        let event = f["event"].as_str().unwrap();
        let call_events = [
            "tool.call",
            "authority.decided",
            "approval.requested",
            "approval.resolved",
            "tool.started",
            "tool.result",
        ];
        if call_events.contains(&event) {
            assert_eq!(f["correlation_id"], "call-1", "{event} carries call_id");
        } else {
            assert!(f["correlation_id"].is_null(), "{event} carries no correlation_id");
        }
    }

    // turn.started payload (S5 §3): input_item_count, model, step_id.
    let ts0 = &log.frames[0];
    assert_eq!(ts0["payload"]["input_item_count"], 1);
    assert_eq!(ts0["payload"]["model"], "slice-model");
    assert_eq!(ts0["payload"]["step_id"], "step-1");

    // tool.call payload: canonical args verbatim.
    let tc = log.frames_of("tool.call")[0];
    assert_eq!(tc["payload"]["call_id"], "call-1");
    assert_eq!(tc["payload"]["tool_name"], "exec_command");
    assert_eq!(
        tc["payload"]["args"],
        json!({"argv": ["fx-read", "./hello.txt"]})
    );

    // authority.decided: ask from the fx-read rule, with the chain
    // proposed-first (S5 §3b).
    let ad = log.frames_of("authority.decided")[0];
    assert_eq!(ad["payload"]["outcome"], "ask");
    assert_eq!(ad["payload"]["source"], "rule");
    let chain = ad["payload"]["decision_chain"].as_array().unwrap();
    assert_eq!(chain.len(), 2);
    assert_eq!(chain[0]["state"], "proposed");
    assert_eq!(chain[1]["state"], "approval_required");
    assert_eq!(chain[0]["decision_id"], chain[1]["decision_id"]); // IN-7 stable

    // approval.requested: the RULING-7 nine-field preview, verbatim.
    let ar = log.frames_of("approval.requested")[0];
    let preview = &ar["payload"];
    for field in [
        "call_id", "decision_id", "tool_name", "action_summary", "args_preview",
        "args_truncated", "args_digest", "effect_class", "targets", "matched_rule",
        "requested_at",
    ] {
        assert!(preview.get(field).is_some(), "preview field {field} present");
    }
    assert_eq!(preview["args_digest"], jcs_sha256(&tc["payload"]["args"]).unwrap());
    assert_eq!(preview["effect_class"], "read");
    assert_eq!(preview["matched_rule"]["source"], "rules/default.rules");

    // approval.resolved: the user's response with the chain.
    let ares = log.frames_of("approval.resolved")[0];
    match &resolve_response {
        Value::String(s) if s == "allow_once" => {
            assert_eq!(ares["payload"]["resolution"], "allow");
            assert_eq!(ares["payload"]["decision_chain"][0]["state"], "approved");
        }
        _ => {
            assert_eq!(ares["payload"]["resolution"], "deny");
            assert_eq!(
                ares["payload"]["reason"],
                exp["deny_reason"],
                "deny.reason lands in approval.resolved.reason (AT-S5-06)"
            );
            assert_eq!(ares["payload"]["decision_chain"][0]["state"], "denied");
        }
    }

    // tool.result frame: status + nullability per S5 §3.
    let tr = log.frames_of("tool.result")[0];
    assert_eq!(tr["payload"]["status"], exp["tool_result_status"]);
    if exp["tool_result_status"] == "succeeded" {
        assert_eq!(tr["payload"]["error"], Value::Null);
        assert_eq!(tr["payload"]["output"]["kind"], "inline");
        assert_eq!(tr["payload"]["output"]["text"], "bob-slice-fixture\n");
        assert!(tr["payload"].get("abort_cause").is_none());
        assert_eq!(tr["payload"]["authority_terminal"]["state"], "merged");
        assert!(tr["payload"]["executor_evidence"].get("backend").is_some());
    } else {
        assert_eq!(tr["payload"]["output"], Value::Null);
        assert_eq!(tr["payload"]["error"]["code"], exp["error_code"]);
        // A-4: the deny row's wire code is a known code, and the message is
        // the governing explanation text verbatim.
        assert!(
            bob_engine::errcode::is_known(exp["error_code"].as_str().unwrap()),
            "deny code is in the normative table"
        );
        assert_eq!(
            tr["payload"]["error"]["message"],
            ares["payload"]["decision_chain"][0]["reason"]["explanation"]["text"],
            "error.message is the governing explanation.text verbatim (A-4)"
        );
    }

    // turn.completed: last frame, status agrees, error null iff succeeded.
    let tcend = log.frames.last().unwrap();
    assert_eq!(tcend["event"], "turn.completed");
    assert_eq!(tcend["payload"]["status"], exp["turn_status"]);
    if exp["turn_status"] == "succeeded" {
        assert_eq!(tcend["payload"]["error"], Value::Null);
    } else {
        assert_eq!(tcend["payload"]["error"]["code"], exp["error_code"]);
    }
    // The turn.start response agrees with the frame.
    let r3 = log.response_of("req-3");
    assert_eq!(r3["result"]["status"], exp["turn_status"]);

    // ---------- ledger verification: read-only replay ----------
    let outcome: ReplayOutcome = replay_segment(seg).unwrap();
    assert!(
        !outcome.has_integrity_failures(),
        "no integrity failures: {:?}",
        outcome.diagnostics
    );
    assert!(
        outcome.diagnostics.is_empty(),
        "no gaps or diagnostics: {:?}",
        outcome.diagnostics
    );
    let recs = &outcome.assertions;
    let kinds: Vec<&str> = recs.iter().map(|r| r.kind.as_str()).collect();
    let exp_kinds: Vec<&str> = exp["ledger_kinds"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(kinds, exp_kinds, "expected record sequence (kinds)");
    for (i, r) in recs.iter().enumerate() {
        assert_eq!(r.ordinal, (i + 1) as u64, "contiguous ordinals base 1");
        assert_eq!(r.schema_version, "1.0");
    }

    // The RULING-8 causal chain: every caused_by cites an earlier event of
    // this segment, with the exact §4 predecessors.
    let id_of = |k: usize| recs[k].event_id.as_str();
    let by_kind = |k: &str| recs.iter().position(|r| r.kind.as_str() == k).unwrap();    for (i, r) in recs.iter().enumerate() {
        if let Some(c) = &r.caused_by {
            assert!(
                recs[..i].iter().any(|p| &p.event_id == c),
                "{} caused_by {} cites an earlier durable event",
                r.kind.as_str(),
                c
            );
        }
    }
    let idx = by_kind;
    assert_eq!(recs[idx("tool.call")].caused_by.as_deref(), Some(id_of(idx("step.context"))));
    assert_eq!(
        recs[idx("authority.decided")].caused_by.as_deref(),
        Some(id_of(idx("tool.call")))
    );
    assert_eq!(
        recs[idx("approval.requested")].caused_by.as_deref(),
        Some(id_of(idx("authority.decided")))
    );
    assert_eq!(
        recs[idx("approval.resolved")].caused_by.as_deref(),
        Some(id_of(idx("approval.requested")))
    );
    if kinds.contains(&"tool.started") {
        assert_eq!(
            recs[idx("tool.started")].caused_by.as_deref(),
            Some(id_of(idx("approval.resolved")))
        );
        assert_eq!(
            recs[idx("tool.result")].caused_by.as_deref(),
            Some(id_of(idx("tool.started")))
        );
        // RULING-5: witness present and BEFORE the result.
        assert!(idx("tool.started") < idx("tool.result"));
    } else {
        // denied result points at the deny record (approval.resolved{deny}).
        assert_eq!(
            recs[idx("tool.result")].caused_by.as_deref(),
            Some(id_of(idx("approval.resolved")))
        );
    }
    assert_eq!(
        recs[idx("turn.end")].caused_by.as_deref(),
        Some(id_of(idx("tool.result")))
    );

    // correlation_id = call_id on the six call records, null elsewhere.
    for r in recs {
        let call_kinds = [
            "tool.call",
            "authority.decided",
            "approval.requested",
            "approval.resolved",
            "tool.started",
            "tool.result",
        ];
        if call_kinds.contains(&r.kind.as_str()) {
            assert_eq!(r.correlation_id.as_deref(), Some("call-1"));
        } else {
            assert!(r.correlation_id.is_none());
        }
    }

    // Digests bind: args_digest is the JCS/sha256 of the call args; on
    // success output_digest is the sha256 of the fixture bytes and equals
    // the merged target_id (I-14, ledger-enforced — recompute here).
    let call_rec = &recs[idx("tool.call")];
    let args = &call_rec.payload["arguments"];
    assert_eq!(recs[idx("approval.requested")].payload["args_digest"], jcs_sha256(args).unwrap());
    if exp["tool_result_status"] == "succeeded" {
        let result = &recs[idx("tool.result")].payload;
        assert_eq!(result["output_digest"], sha256_digest(FIXTURE_BYTES));
        assert_eq!(
            result["terminal_decision"]["reason"]["params"]["target_id"],
            result["output_digest"]
        );
        assert_eq!(result["terminal_decision"]["state"], "merged");
        // A-8: the incorporation is the merged reason, targeting the digest.
        assert_eq!(
            result["terminal_decision"]["reason"]["code"],
            "effect_incorporated"
        );
        assert_eq!(
            result["terminal_decision"]["reason"]["params"]["target_kind"],
            "tool_result"
        );
        // The child really read the fixture file.
        assert_eq!(result["executor_evidence"]["child"]["exit_code"], 0);
        assert_eq!(
            result["executor_evidence"]["child"]["stdout"]["text"],
            "bob-slice-fixture\n"
        );
    } else {
        let result = &recs[idx("tool.result")].payload;
        assert_eq!(result["status"], "denied");
        assert_eq!(result["error"]["code"], exp["error_code"]);
        assert!(result.get("executor_evidence").is_none());
        assert_eq!(result["start_state"], "not_started");
    }
    // turn.end status agrees with the wire.
    assert_eq!(recs[idx("turn.end")].payload["status"], exp["turn_status"]);
    // The ledger's own replay is the read-only fallback source (S5 §2).
    let mut reader = SegmentReader::new(seg.to_path_buf());
    reader.refresh().unwrap();
    assert_eq!(reader.len(), recs.len());
}

#[test]
fn e2e_run_a_allow_once_succeeds() {
    let (log, seg) = scripted_run("allow", "allow.requests.jsonl", |preview| {
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
    verify_run(&log, &seg, &expected("allow.expected.json"), json!("allow_once"));
    // 8 frames (IN-9 V0: 10 ledger records, 8 frames seq 1-8).
    assert_eq!(log.frames.len(), 8);
}

#[test]
fn e2e_run_b_user_deny_with_reason() {
    let (log, seg) = scripted_run("deny", "deny.requests.jsonl", |preview| {
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
    verify_run(&log, &seg, &expected("deny.expected.json"), json!({"deny": {}}));
    assert_eq!(log.frames.len(), 7);
    // No witness: the tool never ran (RULING-5/RATIFY-2).
    assert!(log.frames_of("tool.started").is_empty());
}

/// AT-S5-06: a mismatched echo → `approval_mismatch`, NOTHING appended
/// (the request stays pending), and the correct echo then resolves.
#[test]
fn e2e_approval_mismatch_then_correct_resolve() {
    let root = tmp_root("mismatch");
    let mut cli = Cli::spawn(&root);
    for line in fixture("allow.requests.jsonl").lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        cli.send(&v);
    }
    let preview = loop {
        let v = cli.recv();
        if v.get("seq").is_some() && v["event"] == "approval.requested" {
            break v["payload"].clone();
        }
    };

    // Wrong decision_id echo.
    cli.send(&json!({
        "id": "req-bad",
        "method": "interaction.resolve",
        "params": {
            "call_id": preview["call_id"],
            "decision_id": "00000000-0000-4000-8000-000000000000",
            "args_digest": preview["args_digest"],
            "response": "allow_once"
        }
    }));
    let bad = cli.recv_response("req-bad");
    assert_eq!(bad["error"]["code"], "approval_mismatch");
    assert_eq!(bad["error"]["data"]["expected"]["decision_id"], preview["decision_id"]);

    // Nothing was appended: still exactly 6 records in the segment.
    let seg = find_segment(&root);
    let outcome = replay_segment(&seg).unwrap();
    assert_eq!(
        outcome.assertions.len(),
        6,
        "lineage,turn.start,step.context,tool.call,authority.decided,approval.requested — nothing appended on mismatch"
    );

    // The request stays pending: the correct echo still resolves.
    cli.send(&json!({
        "id": "req-good",
        "method": "interaction.resolve",
        "params": {
            "call_id": preview["call_id"],
            "decision_id": preview["decision_id"],
            "args_digest": preview["args_digest"],
            "response": "allow_once"
        }
    }));
    let good = cli.recv_response("req-good");
    assert_eq!(good["result"]["resolution"], "allow_once");
    // And the run completes.
    let mut log = RunLog::default();
    loop {
        let v = cli.recv();
        if v.get("seq").is_some() {
            let is_terminal = v["event"] == "turn.completed";
            log.frames.push(v);
            if is_terminal {
                log.responses.push(cli.recv());
                break;
            }
        } else {
            log.responses.push(v);
        }
    }
    assert_eq!(cli.finish().success(), true);
    // This log only captured frames from approval.resolved onward (the
    // prefix up to approval.requested was consumed by the first loop).
    assert_eq!(log.frames.len(), 4);
    // Exactly one approval.resolved in the ledger.
    let outcome = replay_segment(&find_segment(&root)).unwrap();
    assert_eq!(
        outcome
            .assertions
            .iter()
            .filter(|r| r.kind.as_str() == "approval.resolved")
            .count(),
        1
    );
}

/// AT-S5-06: a response after the request closed → `approval_closed`.
#[test]
fn e2e_late_resolve_is_approval_closed() {
    let root = tmp_root("closed");
    let mut cli = Cli::spawn(&root);
    for line in fixture("deny.requests.jsonl").lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        cli.send(&v);
    }
    let mut preview = None;
    let mut sent = false;
    let mut log = RunLog::default();
    loop {
        let v = cli.recv();
        if v.get("seq").is_some() {
            let event = v["event"].as_str().unwrap().to_owned();
            if event == "approval.requested" {
                preview = Some(v["payload"].clone());
            }
            let is_terminal = event == "turn.completed";
            log.frames.push(v);
            if is_terminal {
                log.responses.push(cli.recv());
                break;
            }
        } else {
            log.responses.push(v);
        }
        if let (Some(p), false) = (&preview, sent) {
            sent = true;
            cli.send(&json!({
                "id": "req-4",
                "method": "interaction.resolve",
                "params": {
                    "call_id": p["call_id"],
                    "decision_id": p["decision_id"],
                    "args_digest": p["args_digest"],
                    "response": {"deny": {"reason": "not today"}}
                }
            }));
        }
    }
    // Turn is over; the request is closed. A late resolve now:
    let p = preview.unwrap();
    cli.send(&json!({
        "id": "req-late",
        "method": "interaction.resolve",
        "params": {
            "call_id": p["call_id"],
            "decision_id": p["decision_id"],
            "args_digest": p["args_digest"],
            "response": "allow_once"
        }
    }));
    let late = cli.recv_response("req-late");
    assert_eq!(late["error"]["code"], "approval_closed");
    // Duplicate event.ack id replays the cached response (idempotency).
    cli.send(&json!({"id": "ack-1", "method": "event.ack", "params": {"through_seq": 3}}));
    let a1 = cli.recv_response("ack-1");
    assert_eq!(a1["result"]["through_seq"], 3);
    cli.send(&json!({"id": "ack-1", "method": "event.ack", "params": {"through_seq": 3}}));
    let a2 = cli.recv_response("ack-1");
    assert_eq!(a1, a2, "duplicate request_id replays");
    assert!(cli.finish().success());
}

/// `version_mismatch` is terminal: error, no frames, non-zero exit.
#[test]
fn e2e_hello_version_mismatch_closes() {
    let root = tmp_root("vermix");
    let mut child = Command::new(CLI)
        .arg("--data-root")
        .arg(&root)
        .arg("--fx-read")
        .arg(FX_READ)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    writeln!(
        stdin,
        "{}",
        json!({"id": "r1", "method": "hello", "params": {"version": {"major": 3, "minor": 0}}})
    )
    .unwrap();
    stdin.flush().unwrap();
    drop(stdin);
    let out = child.wait_with_output().unwrap();
    assert_eq!(out.status.code(), Some(4), "version_mismatch exit code");
    let text = String::from_utf8_lossy(&out.stdout);
    let line = text.lines().next().unwrap_or_default();
    let v: Value = serde_json::from_str(line).expect("one error response line");
    assert_eq!(v["error"]["code"], "version_mismatch");
    assert!(text.lines().count() == 1, "nothing flows after the mismatch");
}

fn find_segment(root: &Path) -> PathBuf {
    let mut seg = None;
    for entry in walk(root) {
        if entry
            .file_name()
            .map(|n| n.to_string_lossy().ends_with(".audit.jsonl"))
            .unwrap_or(false)
        {
            assert!(seg.is_none());
            seg = Some(entry);
        }
    }
    seg.expect("segment exists")
}

// ---- E9: A-9 `turn.start` response timing, A-11 C-5 first-frame exemption ----

/// SPEC-ERRATA-1 A-9: the `turn.start` response is emitted at TURN END
/// carrying `{turn_id, step_id, status, error?}`, and frames flow during the
/// request's processing. Normative for every client, so it is pinned on the
/// wire rather than in a status reducer.
#[test]
fn a9_turn_start_response_is_emitted_at_turn_end() {
    let root = tmp_root("a9");
    let mut cli = Cli::spawn(&root);
    for line in fixture("allow.requests.jsonl").lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        cli.send(&v);
    }

    // Consume the mid-request stream up to the approval prompt. The turn
    // request is still open here, yet four frames have already arrived.
    let mut frames_before_resolve: Vec<String> = Vec::new();
    let mut responses_before_resolve: Vec<Value> = Vec::new();
    let preview = loop {
        let v = cli.recv();
        if v.get("seq").is_some() {
            let event = v["event"].as_str().unwrap().to_owned();
            frames_before_resolve.push(event.clone());
            if event == "approval.requested" {
                break v["payload"].clone();
            }
        } else {
            responses_before_resolve.push(v);
        }
    };
    assert_eq!(
        frames_before_resolve,
        [
            "turn.started",
            "tool.call",
            "authority.decided",
            "approval.requested"
        ],
        "A-9: frames flow during the request's processing"
    );
    let answered: Vec<&str> = responses_before_resolve
        .iter()
        .map(|r| r["id"].as_str().unwrap())
        .collect();
    assert_eq!(
        answered,
        ["req-1", "req-2"],
        "A-9: no turn.start response exists before the turn ends (only hello + session.create)"
    );

    // Resolve, then drain: the turn.start response must be the LAST thing on
    // the wire, after every frame including turn.completed.
    cli.send(&json!({
        "id": "req-4",
        "method": "interaction.resolve",
        "params": {
            "call_id": preview["call_id"],
            "decision_id": preview["decision_id"],
            "args_digest": preview["args_digest"],
            "response": "allow_once"
        }
    }));
    let mut tail: Vec<Value> = Vec::new();
    loop {
        let v = cli.recv();
        let done = v["event"] == "turn.completed";
        tail.push(v);
        if done {
            tail.push(cli.recv()); // the turn.start response
            break;
        }
    }
    let last = tail.last().unwrap();
    assert!(
        last.get("seq").is_none(),
        "A-9: the turn.start response is the last line on the wire"
    );
    assert_eq!(last["id"], "req-3");
    let result = &last["result"];
    assert_eq!(result["turn_id"], "turn-1");
    assert_eq!(result["step_id"], "step-1");
    assert_eq!(result["status"], "succeeded", "it reports the turn's status");
    // Exactly the four A-9 fields on a success: no `error` key at all.
    let keys: Vec<&str> = result.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["status", "step_id", "turn_id"], "success carries no error");
    // The response is not merely reshuffled: it agrees with turn.completed.
    let completed = tail
        .iter()
        .find(|v| v["event"] == "turn.completed")
        .expect("turn.completed frame");
    assert_eq!(result["status"], completed["payload"]["status"]);
    assert!(cli.finish().success());

    // A failing turn does carry `error` — same response shape, non-success.
    let root2 = tmp_root("a9deny");
    let mut cli = Cli::spawn(&root2);
    for line in fixture("deny.requests.jsonl").lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        cli.send(&v);
    }
    let preview = loop {
        let v = cli.recv();
        if v.get("seq").is_some() && v["event"] == "approval.requested" {
            break v["payload"].clone();
        }
    };
    cli.send(&json!({
        "id": "req-4",
        "method": "interaction.resolve",
        "params": {
            "call_id": preview["call_id"],
            "decision_id": preview["decision_id"],
            "args_digest": preview["args_digest"],
            "response": {"deny": {"reason": "not today"}}
        }
    }));
    let resp = loop {
        let v = cli.recv();
        if v.get("seq").is_some() && v["event"] == "turn.completed" {
            break cli.recv();
        }
    };
    assert_eq!(resp["id"], "req-3");
    assert_eq!(resp["result"]["status"], "failed");
    assert_eq!(resp["result"]["error"]["code"], "user_denied");
    assert!(cli.finish().success());
}

/// SPEC-ERRATA-1 A-11: the C-5 `caused_by` chain check exempts a
/// correlation's FIRST frame — `tool.call` cites the `step.context` record,
/// which has no wire frame (IN-1) — while every later frame of the correlation
/// chains to an already-seen frame. Violations after the first frame remain
/// fatal, so the exemption is exactly one frame wide.
#[test]
fn a11_c5_check_exempts_only_the_first_frame_of_a_correlation() {
    let (log, seg) = scripted_run("a11", "allow.requests.jsonl", |preview| {
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
    let call_frames: Vec<&Value> = log
        .frames
        .iter()
        .filter(|f| f["correlation_id"] == "call-1")
        .collect();
    assert_eq!(call_frames.len(), 6, "the six call events carry the correlation");
    let seen: Vec<&str> = call_frames
        .iter()
        .map(|f| f["event_id"].as_str().unwrap())
        .collect();

    // First frame: cites step.context, which IS a durable record but has no
    // frame — the exemption A-11 repairs.
    let first = call_frames[0];
    assert_eq!(first["event"], "tool.call");
    let first_caused = first["caused_by"].as_str().expect("tool.call has a cause");
    assert!(
        !seen.contains(&first_caused),
        "the first frame of a correlation cites a non-frame event (IN-1)"
    );
    let outcome = replay_segment(&seg).unwrap();
    let step_ctx = outcome
        .assertions
        .iter()
        .find(|r| r.kind.as_str() == "step.context")
        .expect("a durable step.context record");
    assert_eq!(
        first_caused, step_ctx.event_id,
        "the first frame cites the MGR-5 step.context event"
    );
    assert!(
        !log.frames.iter().any(|f| f["event_id"] == step_ctx.event_id),
        "and that record has no frame of its own"
    );

    // Every LATER frame chains to an earlier frame of the same correlation:
    // the check is live from frame 2 on.
    for i in 1..call_frames.len() {
        let caused = call_frames[i]["caused_by"]
            .as_str()
            .unwrap_or_else(|| panic!("frame {i} carries caused_by"));
        assert!(
            seen[..i].contains(&caused),
            "frame {i} ({}) caused_by {caused} is not an already-seen frame",
            call_frames[i]["event"]
        );
    }
    // The chain is exactly the S5 §3b predecessor order.
    assert_eq!(
        call_frames
            .iter()
            .map(|f| f["event"].as_str().unwrap())
            .collect::<Vec<_>>(),
        [
            "tool.call",
            "authority.decided",
            "approval.requested",
            "approval.resolved",
            "tool.started",
            "tool.result"
        ]
    );
}

// ---- E10: S5 negative tests (duplicate request_id, invalid_limits,
// malformed pre-handshake) ----

/// Spawn `bob-slice-cli` with a fixed stdin script and collect
/// (stdout, stderr, exit code, data root). Used by the pre-handshake
/// negatives, where the connection is expected to close.
fn raw_run(tag: &str, input: &str) -> (String, String, Option<i32>, PathBuf) {
    let root = tmp_root(tag);
    let mut child = Command::new(CLI)
        .arg("--data-root")
        .arg(&root)
        .arg("--fx-read")
        .arg(FX_READ)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("bob-slice-cli spawns");
    {
        let mut stdin = child.stdin.take().expect("stdin piped");
        stdin.write_all(input.as_bytes()).expect("write stdin script");
        stdin.flush().expect("flush stdin script");
    }
    let out = child.wait_with_output().expect("child exits");
    (
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
        out.status.code(),
        root,
    )
}

fn stdout_lines(out: &str) -> Vec<Value> {
    out.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap_or_else(|e| panic!("stdout line {l:?} is JSON: {e}")))
        .collect()
}

fn count_segments(root: &Path) -> usize {
    walk(root)
        .into_iter()
        .filter(|p| {
            p.file_name()
                .map(|n| n.to_string_lossy().ends_with(".audit.jsonl"))
                .unwrap_or(false)
        })
        .count()
}

/// AT-S5-05 / EXEC-43: a malformed pre-handshake frame closes the connection
/// with one typed `invalid_request` line and no ledger. Four shapes: not JSON,
/// JSON that is not an object, a first request that is not `hello`, and a
/// `hello` with no version — plus the empty-stdin case (exit 2, no output).
#[test]
fn s5_malformed_pre_handshake_frames_close_the_connection() {
    // (a) not JSON at all — no id to answer, so the error line carries null.
    let (out, _err, code, root) = raw_run("pre-malformed", "this is not json\n");
    let lines = stdout_lines(&out);
    assert_eq!(lines.len(), 1, "exactly one line, nothing flows after it: {out}");
    assert!(lines[0]["id"].is_null(), "an unparseable frame has no request id");
    assert_eq!(lines[0]["error"]["code"], "invalid_request");
    assert!(lines[0].get("result").is_none());
    assert_eq!(code, Some(3), "malformed pre-handshake exit code");
    assert_eq!(count_segments(&root), 0, "no ledger exists before a handshake");
    std::fs::remove_dir_all(&root).ok();

    // (b) JSON that is not a request object.
    let (out, _err, code, root) = raw_run("pre-array", "[1,2,3]\n");
    let lines = stdout_lines(&out);
    assert_eq!(lines.len(), 1, "one line: {out}");
    assert!(lines[0]["id"].is_null());
    assert_eq!(lines[0]["error"]["code"], "invalid_request");
    assert_eq!(code, Some(3));
    std::fs::remove_dir_all(&root).ok();

    // (c) the first request is not `hello` (S5 §1: hello is first).
    let (out, _err, code, root) = raw_run(
        "pre-nothello",
        "{\"id\":\"r1\",\"method\":\"session.create\",\"params\":{}}\n",
    );
    let lines = stdout_lines(&out);
    assert_eq!(lines.len(), 1, "one line: {out}");
    assert_eq!(lines[0]["id"], "r1");
    assert_eq!(lines[0]["error"]["code"], "invalid_request");
    assert!(
        lines[0]["error"]["message"]
            .as_str()
            .unwrap()
            .contains("hello must be the first request"),
        "the error names the rule: {}",
        lines[0]["error"]["message"]
    );
    assert_eq!(code, Some(3));
    assert_eq!(count_segments(&root), 0, "no session was created");
    std::fs::remove_dir_all(&root).ok();

    // (d) `hello` with no version.
    let (out, _err, code, root) = raw_run("pre-noversion", "{\"id\":\"r1\",\"method\":\"hello\",\"params\":{}}\n");
    let lines = stdout_lines(&out);
    assert_eq!(lines.len(), 1, "one line: {out}");
    assert_eq!(lines[0]["id"], "r1");
    assert_eq!(lines[0]["error"]["code"], "invalid_request");
    assert_eq!(code, Some(3));
    std::fs::remove_dir_all(&root).ok();

    // (e) EOF before any handshake: nothing on stdout, exit 2.
    let (out, err, code, _root) = raw_run("pre-eof", "");
    assert!(out.trim().is_empty(), "no line is written for an absent handshake");
    assert_eq!(code, Some(2), "EOF pre-hello exit code");
    assert!(err.contains("stdin closed before hello"), "stderr names the cause: {err}");
}

/// S5 §2 / AT-S5-05: `invalid_limits` is a typed, terminal pre-handshake error
/// — one line, no HelloAck, no session, and the limits the server advertises on
/// the success path are the ones LT-4 requires (1 MiB+ frames).
#[test]
fn s5_invalid_limits_is_typed_and_terminal() {
    for (tag, limits) in [
        ("limits-num", json!(5)),
        ("limits-arr", json!([{"max_frame_bytes": 1048576}])),
        ("limits-str", json!("big")),
    ] {
        let input = json!({
            "id": "r1",
            "method": "hello",
            "params": {"version": {"major": 2}, "limits": limits}
        })
        .to_string()
            + "\n";
        let (out, _err, code, root) = raw_run(tag, &input);
        let lines = stdout_lines(&out);
        assert_eq!(lines.len(), 1, "{tag}: exactly one line: {out}");
        assert_eq!(lines[0]["id"], "r1", "{tag}: the error answers the request id");
        assert_eq!(lines[0]["error"]["code"], "invalid_limits", "{tag}");
        assert!(lines[0].get("result").is_none(), "{tag}: no HelloAck");
        assert!(lines[0]["error"]["message"].as_str().unwrap().contains("limits"));
        assert_eq!(code, Some(3), "{tag}: the handshake cannot continue");
        assert_eq!(count_segments(&root), 0, "{tag}: no ledger");
        std::fs::remove_dir_all(&root).ok();
    }

    // The success path advertises limits that satisfy LT-4 (>= 1 MiB frames).
    let input = json!({
        "id": "r1",
        "method": "hello",
        "params": {"version": {"major": 2}, "limits": {"max_frame_bytes": 1048576}}
    })
    .to_string()
        + "\n";
    let (out, _err, code, root) = raw_run("limits-ok", &input);
    let lines = stdout_lines(&out);
    assert_eq!(lines.len(), 1, "one HelloAck: {out}");
    assert_eq!(lines[0]["id"], "r1");
    assert!(lines[0].get("error").is_none(), "no error on a legal hello");
    assert_eq!(lines[0]["result"]["selected_version"]["major"], 2);
    assert_eq!(lines[0]["result"]["limits"]["max_frame_bytes"], 1048576);
    assert!(lines[0]["result"]["limits"]["max_frame_bytes"].as_u64().unwrap() >= 1024 * 1024);
    assert_eq!(lines[0]["result"]["features"], json!([]), "features=[] (S5 §1)");
    assert_eq!(code, Some(0), "EOF after a completed handshake is a clean exit");
    std::fs::remove_dir_all(&root).ok();
}

fn resolve_params(preview: &Value, response: Value) -> Value {
    json!({
        "id": "r4",
        "method": "interaction.resolve",
        "params": {
            "call_id": preview["call_id"],
            "decision_id": preview["decision_id"],
            "args_digest": preview["args_digest"],
            "response": response
        }
    })
}

/// EXEC-43: a duplicate `request_id` replays the cached response verbatim for
/// the life of the connection — for `hello`, `session.create`, `turn.start` and
/// `event.ack` — and the id is the idempotency key, not the (id, params) pair.
/// Request-shape errors (`invalid_request`, `invalid_limits`,
/// `approval_mismatch`) are NOT cached, so the same id may be retried with
/// fixed params.
#[test]
fn s5_duplicate_request_id_replays_the_cached_response() {
    let root = tmp_root("dup");
    let mut cli = Cli::spawn(&root);

    // hello
    cli.send(&json!({"id": "r1", "method": "hello", "params": {"version": {"major": 2}}}));
    let hello = cli.recv_response("r1");
    assert!(hello["result"]["schema_hash"].is_string());
    cli.send(&json!({"id": "r1", "method": "hello", "params": {"version": {"major": 2}}}));
    let hello_again = cli.recv_response("r1");
    assert_eq!(hello, hello_again, "duplicate hello replays the cached HelloAck");
    // Different params under the same id: the cache is keyed by id alone.
    cli.send(&json!({"id": "r1", "method": "hello", "params": {"version": {"major": 1}}}));
    let hello_third = cli.recv_response("r1");
    assert_eq!(hello, hello_third, "the cached response wins over new params");
    assert!(hello_third.get("error").is_none(), "no version_mismatch on a replay");

    // session.create: replayed, and still exactly one ledger segment.
    cli.send(&json!({"id": "r2", "method": "session.create", "params": {}}));
    let created = cli.recv_response("r2");
    assert!(created["result"]["ledger_path"].is_string());
    cli.send(&json!({"id": "r2", "method": "session.create", "params": {}}));
    let created_again = cli.recv_response("r2");
    assert_eq!(created, created_again, "duplicate session.create replays");
    assert_eq!(count_segments(&root), 1, "one session per connection: no second ledger");

    // turn.start: run the turn, then replay the id.
    cli.send(&json!({"id": "r3", "method": "turn.start", "params": {"model": "slice-model"}}));
    let mut preview: Option<Value> = None;
    let mut sent_resolve = false;
    let turn_result = loop {
        let v = cli.recv();
        if v.get("seq").is_some() {
            if v["event"] == "approval.requested" {
                preview = Some(v["payload"].clone());
            }
        } else if v["id"] == "r3" {
            break v;
        }
        if let (Some(p), false) = (&preview, sent_resolve) {
            sent_resolve = true;
            cli.send(&resolve_params(p, json!("allow_once")));
        }
    };
    assert_eq!(turn_result["result"]["status"], "succeeded");
    // Nothing else is queued before the replay.
    assert!(cli.rx.try_recv().is_err(), "no trailing frames after the turn.start response");

    cli.send(&json!({"id": "r3", "method": "turn.start", "params": {"model": "slice-model"}}));
    let turn_replay = cli.recv_response("r3");
    assert_eq!(
        turn_result, turn_replay,
        "a duplicate turn.start replays instead of 'one turn per session'"
    );
    assert!(
        turn_replay.get("error").is_none(),
        "the replay is the cached result, not an error"
    );
    // The replay appended nothing: the turn's record count is unchanged.
    let seg = find_segment(&root);
    let n_records = replay_segment(&seg).unwrap().assertions.len();
    assert_eq!(n_records, 10, "V0 = 10 ledger records; a replay appends nothing");

    // event.ack: cached, and a duplicate is byte-identical.
    cli.send(&json!({"id": "ack-dup", "method": "event.ack", "params": {"through_seq": 3}}));
    let a1 = cli.recv_response("ack-dup");
    cli.send(&json!({"id": "ack-dup", "method": "event.ack", "params": {"through_seq": 3}}));
    let a2 = cli.recv_response("ack-dup");
    assert_eq!(a1, a2, "duplicate event.ack replays");
    assert_eq!(a1["result"]["through_seq"], 3);

    // EXEC-43 transience: an `invalid_request` is not cached, so the same id
    // may be retried with fixed params.
    cli.send(&json!({"id": "ack-fix", "method": "event.ack", "params": {}}));
    let bad = cli.recv_response("ack-fix");
    assert_eq!(bad["error"]["code"], "invalid_request");
    cli.send(&json!({"id": "ack-fix", "method": "event.ack", "params": {"through_seq": 2}}));
    let fixed = cli.recv_response("ack-fix");
    assert_eq!(
        fixed["result"]["through_seq"], 2,
        "invalid_request is uncached: the same id is reprocessed with fixed params (EXEC-43)"
    );

    assert!(cli.finish().success());
    std::fs::remove_dir_all(&root).ok();
}

/// EXEC-43 + AT-S5-06: `approval_mismatch` is uncached and appends nothing, so
/// the SAME request id can be retried with the correct echo and then resolves.
#[test]
fn s5_approval_mismatch_is_uncached_and_the_same_id_resolves() {
    let root = tmp_root("dupmismatch");
    let mut cli = Cli::spawn(&root);
    cli.send(&json!({"id": "r1", "method": "hello", "params": {"version": {"major": 2}}}));
    cli.recv_response("r1");
    cli.send(&json!({"id": "r2", "method": "session.create", "params": {}}));
    cli.recv_response("r2");
    cli.send(&json!({"id": "r3", "method": "turn.start", "params": {"model": "slice-model"}}));
    let preview = loop {
        let v = cli.recv();
        if v.get("seq").is_some() && v["event"] == "approval.requested" {
            break v["payload"].clone();
        }
    };
    let records_before = replay_segment(&find_segment(&root)).unwrap().assertions.len();

    // Same id, corrupted digest.
    let mut bad_params = resolve_params(&preview, json!("allow_once"));
    let digest = preview["args_digest"].as_str().unwrap();
    let mut corrupted = digest.to_string();
    let last = corrupted.pop().unwrap();
    corrupted.push(if last == '0' { '1' } else { '0' });
    bad_params["params"]["args_digest"] = json!(corrupted);
    cli.send(&bad_params);
    let mismatch = cli.recv_response("r4");
    assert_eq!(mismatch["error"]["code"], "approval_mismatch");
    assert_eq!(
        replay_segment(&find_segment(&root)).unwrap().assertions.len(),
        records_before,
        "nothing is appended on a mismatch"
    );

    // Same id, correct echo: processed (not replayed from a cache) and resolves.
    cli.send(&resolve_params(&preview, json!("allow_once")));
    let resolved = cli.recv_response("r4");
    assert_eq!(
        resolved["result"]["resolution"], "allow_once",
        "approval_mismatch is uncached (EXEC-43): the same id is reprocessed"
    );
    loop {
        let v = cli.recv();
        if v.get("seq").is_some() && v["event"] == "turn.completed" {
            break;
        }
    }
    assert!(cli.finish().success());
    let outcome = replay_segment(&find_segment(&root)).unwrap();
    assert_eq!(
        outcome
            .assertions
            .iter()
            .filter(|r| r.kind.as_str() == "approval.resolved")
            .count(),
        1,
        "exactly one approval.resolved for the one request"
    );
    std::fs::remove_dir_all(&root).ok();
}
