//! AT-1 projection-checksum harness (EXEC-48, S10 §6 AT-1 partial): the SAME
//! ledger, projected twice, must agree on a checksum AND on the view rows.
//!
//! Leg 1 — the engine's own projection: `projection::project_segment` over the
//! run's segment, then the `assertion` view (one row per durable record) and
//! the I-24 `joined_call` view.
//! Leg 2 — the NDJSON binding: the frames `bob-slice-cli` put on stdout.
//! Leg 3 — the committed wire transcript (`fixtures/transcripts/*.wire.jsonl`),
//! i.e. the bytes a consumer of the binding actually received.
//!
//! The checksum shape is documented in `shell/harness/projection.cjs` and the
//! JS reducer harness pins the same hex, so the UI's projection of the wire is
//! covered by the same value; the Tauri IPC leg lives in
//! `shell/src-tauri/src/bridge.rs`.
//!
//! Volatile wire values (event_id, decision_id, timestamps, absolute paths) are
//! excluded by construction — the projection keeps event names, ordinals,
//! correlation, the R8 causal link, the DecisionRecord chain and the terminal
//! status/output digest, which is everything that must survive the bindings
//! verbatim.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::time::Duration;

use rusqlite::Connection;
use serde_json::{json, Value};

use bob_engine::digest::{jcs_bytes, sha256_digest};
use bob_engine::projection::project_segment;

const FIXTURE_DIR: &str = "tests/fixtures/s5";
const TRANSCRIPT_DIR: &str = "tests/fixtures/transcripts";
const CLI: &str = env!("CARGO_BIN_EXE_bob-slice-cli");
const FX_READ: &str = env!("CARGO_BIN_EXE_fx-read");

fn root_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).to_path_buf()
}

fn fixture(name: &str) -> String {
    let p = root_path().join(FIXTURE_DIR).join(name);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("fixture {p:?}: {e}"))
}

fn tmp_root(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e10-at1-{tag}-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
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

fn find_segment(root: &Path) -> PathBuf {
    let segs: Vec<PathBuf> = walk(root)
        .into_iter()
        .filter(|p| {
            p.file_name()
                .map(|n| n.to_string_lossy().ends_with(".audit.jsonl"))
                .unwrap_or(false)
        })
        .collect();
    assert_eq!(segs.len(), 1, "one segment per run");
    segs[0].clone()
}

// ---------- the scripted NDJSON client ----------

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
            for line in BufReader::new(stdout).lines() {
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
            Err(RecvTimeoutError::Timeout) => panic!("bob-slice-cli stalled (30s)"),
            Err(RecvTimeoutError::Disconnected) => panic!("bob-slice-cli stdout closed early"),
        }
    }

    fn finish(&mut self) -> std::process::ExitStatus {
        drop(self.stdin.take());
        while self.rx.try_recv().is_ok() {}
        self.child.wait().expect("child exited")
    }
}

/// Drive the committed V0 script and return the ordered frames it produced.
fn scripted_frames(tag: &str, fixture_name: &str, deny: bool) -> (Vec<Value>, PathBuf, PathBuf) {
    let root = tmp_root(tag);
    let mut cli = Cli::spawn(&root);
    for line in fixture(fixture_name).lines() {
        let v: Value = serde_json::from_str(line).unwrap();
        cli.send(&v);
    }
    let mut frames = Vec::new();
    let mut preview: Option<Value> = None;
    let mut sent = false;
    loop {
        let v = cli.recv();
        if v.get("seq").is_some() {
            let event = v["event"].as_str().unwrap().to_owned();
            if event == "approval.requested" {
                preview = Some(v["payload"].clone());
            }
            let last = event == "turn.completed";
            frames.push(v);
            if last {
                break;
            }
        }
        if let (Some(p), false) = (&preview, sent) {
            sent = true;
            let response = if deny {
                json!({"deny": {"reason": "not today"}})
            } else {
                json!("allow_once")
            };
            cli.send(&json!({
                "id": "req-4",
                "method": "interaction.resolve",
                "params": {
                    "call_id": p["call_id"],
                    "decision_id": p["decision_id"],
                    "args_digest": p["args_digest"],
                    "response": response,
                }
            }));
        }
    }
    // Drain the turn.start response so the child is not blocked on the pipe.
    let _ = cli.recv();
    assert!(cli.finish().success(), "run {tag} exits cleanly");
    let seg = find_segment(&root);
    (frames, seg, root)
}

// ---------- the projection shape ----------

fn chain_entry(rec: &Value) -> Value {
    json!({
        "state": rec["state"].clone(),
        "reason": rec["reason"]["code"].clone(),
        "rule": rec["matched_rule_ref"].clone(),
    })
}

fn chain_entries(rec: &Value) -> Vec<Value> {
    if rec.is_null() {
        Vec::new()
    } else {
        vec![chain_entry(rec)]
    }
}

fn frame_event(kind: &str) -> Option<&'static str> {
    match kind {
        "turn.start" => Some("turn.started"),
        "turn.end" => Some("turn.completed"),
        "tool.call" => Some("tool.call"),
        "authority.decided" => Some("authority.decided"),
        "approval.requested" => Some("approval.requested"),
        "approval.resolved" => Some("approval.resolved"),
        "tool.started" => Some("tool.started"),
        "tool.result" => Some("tool.result"),
        _ => None,
    }
}

/// Leg 1: project the run's segment and build the same row shape from the
/// `assertion` view (durable record payloads) plus the `joined_call` view.
fn engine_projection(seg: &Path) -> Value {
    let db = seg.with_extension("at1.sqlite");
    let _ = std::fs::remove_file(&db);
    let report = project_segment(seg, &db).expect("project the segment");
    assert!(
        report.diagnostics.is_empty(),
        "clean segment: {:?}",
        report.diagnostics
    );
    let conn = Connection::open(&db).unwrap();

    // Every durable record, in ordinal order.
    struct Rec {
        ordinal: u64,
        event_id: String,
        kind: String,
        correlation_id: Option<String>,
        caused_by: Option<String>,
        payload: Value,
    }
    let mut recs: Vec<Rec> = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT ordinal, event_id, kind, correlation_id, caused_by, payload_json \
                 FROM assertion ORDER BY ordinal",
            )
            .unwrap();
        let rows = stmt
            .query_map([], |r| {
                let payload: String = r.get(5)?;
                Ok(Rec {
                    ordinal: r.get::<_, i64>(0)? as u64,
                    event_id: r.get(1)?,
                    kind: r.get(2)?,
                    correlation_id: r.get(3)?,
                    caused_by: r.get(4)?,
                    payload: serde_json::from_str(&payload).unwrap_or(Value::Null),
                })
            })
            .unwrap();
        for row in rows {
            recs.push(row.unwrap());
        }
    }

    // Frame-bearing records, in order; their event ids resolve to ordinals.
    let framed: Vec<&Rec> = recs
        .iter()
        .filter(|r| frame_event(&r.kind).is_some())
        .collect();
    let ordinal_of = |event_id: &str| {
        framed
            .iter()
            .find(|r| r.event_id == event_id)
            .map(|r| r.ordinal.to_string())
    };

    let mut rows = Vec::new();
    for (i, r) in framed.iter().enumerate() {
        let event = frame_event(&r.kind).unwrap();
        let caused_by = match &r.caused_by {
            None => Value::Null,
            Some(c) => match ordinal_of(c) {
                Some(o) => json!(o),
                None => json!("external"),
            },
        };
        let chain = match r.kind.as_str() {
            "authority.decided" => {
                let mut v = chain_entries(&r.payload["proposed"]);
                v.extend(chain_entries(&r.payload["decision"]));
                v
            }
            "approval.resolved" => chain_entries(&r.payload["decision"]),
            "tool.result" => chain_entries(&r.payload["terminal_decision"]),
            _ => Vec::new(),
        };
        let status = match r.kind.as_str() {
            "tool.result" | "turn.end" => r.payload["status"].clone(),
            _ => Value::Null,
        };
        rows.push(json!({
            "seq": (i + 1) as u64,
            "event": event,
            "ordinal": r.ordinal.to_string(),
            "correlation_id": r.correlation_id.clone(),
            "caused_by": caused_by,
            "chain": chain,
            "status": status,
        }));
    }

    // The I-24 joined call view row.
    let (call_id, decided_outcome, decided_source, resolution, status, error_code, output_digest,
         terminal_state, witness_event_id, n_results, accepted, valid): (
        Option<String>, Option<String>, Option<String>, Option<String>, Option<String>,
        Option<String>, Option<String>, Option<String>, Option<String>, i64,
        Option<i64>, Option<i64>,
    ) = conn
        .query_row(
            "SELECT call_id, decided_outcome, decided_source, resolution, status, error_code, \
                    output_digest, terminal_decision_state, witness_event_id, n_results, \
                    accepted, valid FROM joined_call",
            [],
            |r| {
                Ok((
                    r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?,
                    r.get(6)?, r.get(7)?, r.get(8)?,
                    r.get::<_, Option<i64>>(9)?.unwrap_or(0),
                    r.get::<_, Option<i64>>(10)?,
                    r.get::<_, Option<i64>>(11)?,
                ))
            },
        )
        .expect("one joined_call row");

    let call = json!({
        "call_id": call_id,
        "decided_outcome": decided_outcome,
        "decided_source": decided_source,
        "resolution": resolution,
        "status": status,
        "error_code": error_code,
        "terminal_state": terminal_state,
        "output_digest": output_digest,
        "witness_present": witness_event_id.is_some(),
        "n_results": n_results,
    });
    // View-row assertions that only the engine side can make.
    assert_eq!(valid, Some(1), "the projection validates the call");
    assert_eq!(accepted, Some(1), "the call is accepted truth (EXEC-59)");
    assert_eq!(n_results, 1, "one durable result");

    let _ = std::fs::remove_file(&db);
    json!({"frames": rows, "call": call})
}

/// Legs 2 and 3: the same shape from wire frames (live NDJSON or the committed
/// transcript — the frame objects are identical either way).
fn wire_projection(frames: &[Value]) -> Value {
    let ordinal_of = |event_id: &str| {
        frames
            .iter()
            .find(|f| f["event_id"] == json!(event_id))
            .map(|f| f["ordinal"].as_str().unwrap_or("0").to_owned())
    };

    let mut rows = Vec::new();
    for (i, f) in frames.iter().enumerate() {
        let event = f["event"].as_str().unwrap();
        let caused_by = match f["caused_by"].as_str() {
            None => Value::Null,
            Some(c) => match ordinal_of(c) {
                Some(o) => json!(o),
                None => json!("external"),
            },
        };
        let chain = match event {
            "authority.decided" | "approval.resolved" => f["payload"]["decision_chain"]
                .as_array()
                .map(|a| a.iter().filter(|v| !v.is_null()).map(chain_entry).collect::<Vec<_>>())
                .unwrap_or_default(),
            "tool.result" => chain_entries(&f["payload"]["authority_terminal"]),
            _ => Vec::new(),
        };
        let status = match event {
            "tool.result" | "turn.completed" => f["payload"]["status"].clone(),
            _ => Value::Null,
        };
        rows.push(json!({
            "seq": f["seq"].as_u64().unwrap_or((i + 1) as u64),
            "event": event,
            "ordinal": f["ordinal"].as_str().unwrap_or("0"),
            "correlation_id": if f["correlation_id"].is_null() { Value::Null } else { f["correlation_id"].clone() },
            "caused_by": caused_by,
            "chain": chain,
            "status": status,
        }));
    }

    let first = |event: &str| frames.iter().find(|f| f["event"] == json!(event));
    let call_frame = first("tool.call");
    let decided = first("authority.decided");
    let resolved = first("approval.resolved");
    let result = first("tool.result");
    let terminal = result.map(|r| r["payload"]["authority_terminal"].clone()).unwrap_or(Value::Null);
    let call = json!({
        "call_id": call_frame.map(|f| f["payload"]["call_id"].clone()).unwrap_or(Value::Null),
        "decided_outcome": decided.map(|f| f["payload"]["outcome"].clone()).unwrap_or(Value::Null),
        "decided_source": decided.map(|f| f["payload"]["source"].clone()).unwrap_or(Value::Null),
        "resolution": resolved.map(|f| f["payload"]["resolution"].clone()).unwrap_or(Value::Null),
        "status": result.map(|f| f["payload"]["status"].clone()).unwrap_or(Value::Null),
        "error_code": result
            .map(|f| if f["payload"]["error"].is_null() { Value::Null } else { f["payload"]["error"]["code"].clone() })
            .unwrap_or(Value::Null),
        "terminal_state": if terminal.is_null() { Value::Null } else { terminal["state"].clone() },
        "output_digest": if terminal.is_null() { Value::Null } else { terminal["reason"]["params"]["target_id"].clone() },
        "witness_present": frames.iter().any(|f| f["event"] == json!("tool.started")),
        "n_results": frames.iter().filter(|f| f["event"] == json!("tool.result")).count(),
    });

    json!({"frames": rows, "call": call})
}

fn checksum(projection: &Value) -> String {
    sha256_digest(&jcs_bytes(projection).expect("projection is JCS-encodable"))
}

fn transcript(path: &Path) -> (Vec<Value>, usize) {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| panic!("transcript {path:?}: {e}"));
    let mut frames = Vec::new();
    let mut lines = 0usize;
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        lines += 1;
        let v: Value = serde_json::from_str(line).expect("transcript line is JSON");
        if v.get("event").is_some() {
            frames.push(v);
        }
    }
    (frames, lines)
}

fn manifest() -> Value {
    let p = root_path().join(TRANSCRIPT_DIR).join("MANIFEST.json");
    serde_json::from_str(&std::fs::read_to_string(&p).expect("MANIFEST.json")).unwrap()
}

// ---------- the test ----------

/// EXEC-48 / S10 AT-1: one ledger, three projections — the engine's SQLite views,
/// the live NDJSON binding, and the committed wire transcript — must agree on
/// the projection checksum and on the view row, for both V0 paths.
#[test]
fn at1_both_bindings_agree_on_the_projection_checksum() {
    let man = manifest();
    let entries = man["transcripts"].as_array().expect("transcripts[]").clone();
    assert_eq!(entries.len(), 2, "two committed V0 transcripts");

    for entry in entries {
        let file = entry["file"].as_str().expect("file");
        let fixture_name = entry["s5_fixture"].as_str().expect("s5_fixture");
        let deny = fixture_name.starts_with("deny");
        let expected_frames = entry["frames"].as_u64().expect("frames") as usize;
        let expected_sum = entry["at1_checksum"].as_str().expect("at1_checksum");
        let proj_fixture = entry["projection_fixture"].as_str().expect("projection_fixture");
        let expected_proj: Value = serde_json::from_str(
            &std::fs::read_to_string(root_path().join(TRANSCRIPT_DIR).join(proj_fixture))
                .unwrap_or_else(|e| panic!("projection fixture {proj_fixture}: {e}")),
        )
        .expect("projection fixture is JSON");

        // Leg 2: live NDJSON frames + leg 1: the engine's projection of the
        // same run's segment.
        let (live_frames, seg, root) = scripted_frames(file, fixture_name, deny);
        assert_eq!(live_frames.len(), expected_frames, "{file}: frame count");
        let engine_proj = engine_projection(&seg);
        let wire_proj = wire_projection(&live_frames);

        let engine_sum = checksum(&engine_proj);
        assert_eq!(
            engine_sum,
            checksum(&wire_proj),
            "{file}: the engine's ledger projection and the NDJSON binding disagree"
        );
        assert_eq!(
            engine_sum, expected_sum,
            "{file}: the checksum moved away from the pinned value (a field was added, \
             renamed, dropped or reordered on one side)"
        );

        // The view row itself, field by field (not just its hash).
        assert_eq!(
            engine_proj["call"], wire_proj["call"],
            "{file}: joined_call view row vs the wire-derived call row"
        );
        // And the per-frame rows, in order.
        assert_eq!(
            engine_proj["frames"], wire_proj["frames"],
            "{file}: per-frame projection rows"
        );
        // Both legs equal the committed AT-1 projection fixture, which is the
        // object the Tauri IPC leg (shell/src-tauri bridge tests) also checks.
        assert_eq!(
            engine_proj, expected_proj,
            "{file}: the ledger projection differs from the committed projection fixture"
        );
        assert_eq!(
            wire_proj, expected_proj,
            "{file}: the NDJSON projection differs from the committed projection fixture"
        );

        // Leg 3: the committed transcript is the same projection again, so a
        // transcript that drifts from the engine fails here.
        let tpath = root_path().join(TRANSCRIPT_DIR).join(file);
        let (committed_frames, committed_lines) = transcript(&tpath);
        assert_eq!(committed_lines as u64, entry["lines"].as_u64().unwrap(), "{file}: transcript line count");
        assert_eq!(committed_frames.len(), expected_frames, "{file}: transcript frames");
        let committed_proj = wire_projection(&committed_frames);
        assert_eq!(
            checksum(&committed_proj),
            expected_sum,
            "{file}: the committed transcript disagrees with the pinned checksum"
        );
        assert_eq!(
            committed_proj["call"], wire_proj["call"],
            "{file}: the committed transcript's view row drifted from a live run"
        );

        // AT-1 also covers the chain fields: every non-first call frame cites a
        // frame of the same correlation, and the chain states are the S5 §3b
        // order (proposed -> routed -> resolved -> terminal).
        let chain_of = |proj: &Value, event: &str| -> Vec<String> {
            proj["frames"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|r| r["event"] == json!(event))
                .flat_map(|r| {
                    r["chain"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|c| c["state"].as_str().unwrap_or("null").to_owned())
                        .collect::<Vec<_>>()
                })
                .collect()
        };
        assert_eq!(
            chain_of(&engine_proj, "authority.decided"),
            vec!["proposed".to_string(), "approval_required".to_string()]
        );
        assert_eq!(
            chain_of(&engine_proj, "approval.resolved"),
            if deny { vec!["denied".to_string()] } else { vec!["approved".to_string()] }
        );
        assert_eq!(
            chain_of(&engine_proj, "tool.result"),
            if deny { Vec::new() } else { vec!["merged".to_string()] },
            "the terminal DecisionRecord rides in tool.result (merged only)"
        );

        std::fs::remove_dir_all(&root).ok();
    }
}
