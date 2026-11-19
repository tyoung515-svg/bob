//! E11 — generated TS protocol + drift check (A-10 / AT-S5-03 / EXEC-49).
//!
//! The claim this file proves is narrow and checkable: `shell/src/protocol.ts`
//! is exactly what the engine's normative wire model
//! (`engine/src/wire_schema.rs`) produces, and that model describes the frames
//! the engine actually emits.
//!
//! - [`generated_shell_module_matches_the_model`] — the drift check. It
//!   regenerates the module in `cargo test` and fails on any byte difference, so
//!   a hand edit of `protocol.ts` (the EXEC-49 hand mirror) or a wire change
//!   without regeneration is a red suite.
//! - [`generator_is_reproducible`] — same input, identical bytes.
//! - [`schema_hash_is_unchanged_by_the_model_move`] — `hello`'s `schema_hash`
//!   still equals the value the committed transcripts carry, i.e. the model
//!   move was wire-neutral, and the description really is derived from the
//!   model rather than restated beside it.
//! - [`model_matches_every_frame_the_engine_emits`] — a LIVE allow run and a
//!   LIVE deny run (plus both committed transcripts) are validated field by
//!   field against the model: envelope key set, payload key set, presence and
//!   value type, recursion into every nested shape. A model that drifts from
//!   the Rust types fails here, which is what makes the generated TS trustworthy
//!   rather than merely well-formatted.
//! - [`generated_module_carries_the_closed_error_vocabulary`] — the
//!   `WireErrorCode` union is the A-4 table, canonical spellings only.
//! - [`custom_shapes_match_the_engine_serialization`] — the three shapes the
//!   model describes as a literal TS body are pinned to what the engine
//!   serializes.

use std::collections::BTreeSet;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError};
use std::time::Duration;

use serde_json::{json, Value};

use bob_engine::wire_schema::{self, ArrTy, Field, Presence, ShapeKind, Ty};
use bob_engine::{exec, protocol, tsgen};

const TRANSCRIPT_DIR: &str = "tests/fixtures/transcripts";
const CLI: &str = env!("CARGO_BIN_EXE_bob-slice-cli");
const FX_READ: &str = env!("CARGO_BIN_EXE_fx-read");

/// The `hello` schema hash: pinned here, in the generated module and in both
/// committed transcripts. Repinning it is a deliberate wire change (A-10).
const PINNED_SCHEMA_HASH: &str =
    "sha256:efb6ed414ae8eace49db4622d91c0b02eb81166fa3af1a8195a7915968af3555";

fn root_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).to_path_buf()
}

// ---------- item 2: the drift check ----------

#[test]
fn generated_shell_module_matches_the_model() {
    let path = tsgen::generated_path();
    let committed = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("{path:?} is the committed artifact and must exist: {e}"));

    assert!(
        committed.starts_with(tsgen::HEADER_MARKER),
        "{} does not start with {:?}: it is not the generated artifact (A-10 forbids a hand-written mirror)",
        path.display(),
        tsgen::HEADER_MARKER
    );
    assert!(
        !committed.contains('\u{feff}'),
        "{} starts with a byte-order mark; the artifact is plain UTF-8",
        path.display()
    );

    let generated = tsgen::generate();
    // The repository stores text as LF and checks out native (core.autocrlf),
    // so a CRLF worktree file is not wire drift; content is what is compared.
    let a = tsgen::normalize_newlines(&committed);
    let b = tsgen::normalize_newlines(&generated);

    if a != b {
        let (line, committed_line) = first_difference(&a, &b, 0);
        let (_, generated_line) = first_difference(&a, &b, 1);
        panic!(
            "{} has drifted from the engine's wire model (A-10 / AT-S5-03).\n\
             first difference at line {line}: committed {committed_line:?} vs generated {generated_line:?}\n\
             regenerate it: `bob-wiregen --out <repo>/shell/src/protocol.ts` (or `npm run gen:protocol` in shell/).\n\
             Never edit the file by hand.",
            path.display()
        );
    }

    // The committed artifact must also name the hash the engine is serving.
    assert!(
        a.contains(&format!(
            "export const WIRE_SCHEMA_HASH = \"{}\";",
            protocol::schema_hash()
        )),
        "the generated module does not carry the engine's schema hash"
    );
}

/// The line number (1-based) and text of the first differing line.
fn first_difference(a: &str, b: &str, side: usize) -> (usize, String) {
    let la: Vec<&str> = a.lines().collect();
    let lb: Vec<&str> = b.lines().collect();
    for i in 0..la.len().max(lb.len()) {
        let (x, y) = (
            la.get(i).copied().unwrap_or("<missing>"),
            lb.get(i).copied().unwrap_or("<missing>"),
        );
        if x != y {
            return (i + 1, if side == 0 { x } else { y }.to_owned());
        }
    }
    (0, String::new())
}

#[test]
fn generator_is_reproducible() {
    let a = tsgen::generate();
    let b = tsgen::generate();
    assert_eq!(a, b, "two generations differ: the generator is not deterministic");

    // A second thread must see the same bytes (no hidden global state).
    let c = std::thread::spawn(tsgen::generate)
        .join()
        .expect("generator thread");
    assert_eq!(a, c, "generation in another thread differs");

    assert!(
        !a.contains('\r'),
        "the generated module must be LF-only; git normalizes on checkout, the artifact does not"
    );
    assert!(a.ends_with("}\n"), "the module must end with a closing brace + newline");
    assert!(
        a.len() > 12_000,
        "the module is suspiciously small ({} bytes) — a truncated render?",
        a.len()
    );
    // Every volatile value is content-derived: the only hash in the file is the
    // schema hash, and re-generating does not move it.
    assert_eq!(
        a.matches("sha256:").count(),
        1,
        "expected exactly one hash in the module (the schema hash constant)"
    );
    assert!(a.contains(PINNED_SCHEMA_HASH));
}

// ---------- item 1: one model, hash-neutral move ----------

#[test]
fn schema_hash_is_unchanged_by_the_model_move() {
    // The model is now the description's owner; the hash must not move, because
    // `hello` is what both ends agree on before any frame flows.
    assert_eq!(
        protocol::schema_hash(),
        PINNED_SCHEMA_HASH,
        "the schema hash moved: that is a wire change, not a refactor"
    );
    assert_eq!(protocol::hello_result()["schema_hash"], PINNED_SCHEMA_HASH);

    // The description is the model's field lists, not a parallel literal.
    let desc = protocol::schema_description();
    assert_eq!(desc["event_set"], json!(protocol::EVENT_SET));
    let envelope: Vec<&str> = wire_schema::ENVELOPE.iter().map(|f| f.name).collect();
    assert_eq!(desc["envelope"], json!(envelope));
    for p in wire_schema::FRAME_PAYLOADS {
        assert_eq!(
            desc["payloads"][p.event],
            json!(wire_schema::field_names(p.shape)),
            "payload field list for {} is not the model's",
            p.event
        );
    }
    assert_eq!(
        desc["statuses"]["tool.result"],
        json!(wire_schema::union_variants("ToolResultStatus"))
    );
    assert_eq!(
        desc["statuses"]["turn.completed"],
        json!(wire_schema::union_variants("TurnStatus"))
    );
    assert_eq!(desc["payloads"].as_object().map(|o| o.len()), Some(8));

    // The bytes the shell actually received agree: both committed transcripts
    // carry this hash in the HelloAck.
    for file in ["v0_allow.wire.jsonl", "v0_deny.wire.jsonl"] {
        let text = read_fixture(file);
        let hello: Value =
            serde_json::from_str(text.lines().next().expect("transcript has a first line"))
                .expect("first transcript line is JSON");
        assert_eq!(
            hello["result"]["schema_hash"], PINNED_SCHEMA_HASH,
            "{file}: HelloAck schema_hash"
        );
    }
}

// ---------- item 1: the model describes the frames the engine emits ----------

#[test]
fn model_matches_every_frame_the_engine_emits() {
    let mut c = ModelCheck::default();

    // Live runs: the engine's own bytes, both terminal paths.
    for (tag, deny) in [("allow", false), ("deny", true)] {
        let frames = live_frames(tag, deny);
        assert!(!frames.is_empty(), "{tag}: the run emitted no frames");
        for f in &frames {
            c.frame(&format!("live-{tag}"), f);
        }
    }
    // And the committed transcripts (the bytes a consumer of the binding got).
    for file in ["v0_allow.wire.jsonl", "v0_deny.wire.jsonl"] {
        for f in transcript_frames(file) {
            c.frame(file, &f);
        }
    }

    assert!(
        c.errors.is_empty(),
        "the wire model does not describe what the engine emits ({} finding(s)):\n{}",
        c.errors.len(),
        c.errors.join("\n")
    );

    // Every frame kind was exercised…
    let expected: BTreeSet<&str> = wire_schema::EVENTS.iter().copied().collect();
    assert_eq!(
        c.seen_events, expected,
        "the live runs did not cover every slice-v1 event"
    );
    // …and every required payload field of every event was observed at least
    // once, so the model cannot declare a field the engine never sends.
    for p in wire_schema::FRAME_PAYLOADS {
        let shape = wire_schema::shape(p.shape).expect("payload shape exists");
        for f in shape.fields {
            if f.presence == Presence::Required {
                assert!(
                    c.seen_required.contains(&(p.event.to_owned(), f.name.to_owned())),
                    "required field {}.{} was never observed on the wire",
                    p.event,
                    f.name
                );
            }
        }
    }
}

/// Accumulates model violations across runs, plus what was covered.
#[derive(Default)]
struct ModelCheck {
    errors: Vec<String>,
    seen_events: BTreeSet<&'static str>,
    seen_required: BTreeSet<(String, String)>,
}

impl ModelCheck {
    fn err(&mut self, case: &str, path: &str, msg: impl Into<String>) {
        self.errors.push(format!("[{case}] {path}: {}", msg.into()));
    }

    fn frame(&mut self, case: &str, frame: &Value) {
        let Some(obj) = frame.as_object() else {
            self.err(case, "$", "frame is not a JSON object");
            return;
        };

        // Envelope: the model's exact key set, in any order.
        let envelope: Vec<&str> = wire_schema::ENVELOPE.iter().map(|f| f.name).collect();
        for name in &envelope {
            if !obj.contains_key(*name) {
                self.err(case, "$", format!("envelope field '{name}' is missing"));
            }
        }
        for key in obj.keys() {
            if !envelope.contains(&key.as_str()) {
                self.err(case, "$", format!("undeclared envelope field '{key}'"));
            }
        }
        for f in wire_schema::ENVELOPE {
            self.field(case, "$", obj.get(f.name), f);
        }

        let Some(event) = obj.get("event").and_then(Value::as_str) else {
            return;
        };
        let known: Option<&'static str> = wire_schema::EVENTS
            .iter()
            .copied()
            .find(|e| *e == event);
        let Some(known) = known else {
            self.err(case, "$.event", format!("'{event}' is not a slice-v1 event"));
            return;
        };
        self.seen_events.insert(known);

        let Some(spec) = wire_schema::frame_payload(event) else {
            self.err(case, "$.payload", format!("no payload spec for '{event}'"));
            return;
        };
        if let Some(payload) = obj.get("payload") {
            self.shape_value(case, "$.payload", payload, spec.shape);
        }
        if let Some(shape) = wire_schema::shape(spec.shape) {
            for f in shape.fields {
                if f.presence == Presence::Required {
                    self.seen_required
                        .insert((event.to_owned(), f.name.to_owned()));
                }
            }
        }
    }

    fn field(&mut self, case: &str, path: &str, v: Option<&Value>, f: &Field) {
        match v {
            None => {
                if f.presence != Presence::Optional {
                    self.err(case, path, format!("required field '{}' is absent", f.name));
                }
            }
            Some(Value::Null) => {
                if f.presence == Presence::Required {
                    self.err(case, path, format!("required field '{}' is null", f.name));
                }
            }
            Some(value) => self.ty(case, &format!("{path}.{}", f.name), value, f.ty),
        }
    }

    fn ty(&mut self, case: &str, path: &str, v: &Value, ty: Ty) {
        match ty {
            Ty::Str => {
                if !v.is_string() {
                    self.err(case, path, format!("expected a string, got {v}"));
                }
            }
            Ty::Bool => {
                if !v.is_boolean() {
                    self.err(case, path, format!("expected a boolean, got {v}"));
                }
            }
            Ty::Num => {
                if !v.is_number() {
                    self.err(case, path, format!("expected a number, got {v}"));
                }
            }
            Ty::U64Str => match v.as_str() {
                Some(s) if s.parse::<u64>().is_ok() => {}
                _ => self.err(
                    case,
                    path,
                    format!("ordinal-bearing field must be a base-10 u64 string, got {v}"),
                ),
            },
            Ty::Json => {}
            Ty::Ref(name) => self.ref_value(case, path, v, name),
            Ty::Arr(ArrTy::Str) => match v.as_array() {
                Some(a) => {
                    for (i, e) in a.iter().enumerate() {
                        if !e.is_string() {
                            let at = format!("{path}[{i}]");
                            self.err(case, &at, format!("expected a string, got {e}"));
                        }
                    }
                }
                None => self.err(case, path, format!("expected an array, got {v}")),
            },
            Ty::Arr(ArrTy::Ref(name)) => match v.as_array() {
                Some(a) => {
                    for (i, e) in a.iter().enumerate() {
                        self.ref_value(case, &format!("{path}[{i}]"), e, name);
                    }
                }
                None => self.err(case, path, format!("expected an array, got {v}")),
            },
        }
    }

    fn ref_value(&mut self, case: &str, path: &str, v: &Value, name: &'static str) {
        let shape = wire_schema::shape(name)
            .unwrap_or_else(|| panic!("model references an undeclared shape '{name}'"));
        match shape.kind {
            ShapeKind::Object => self.shape_value(case, path, v, name),
            ShapeKind::Union(variants) => match v.as_str() {
                Some(s) if variants.contains(&s) => {}
                _ => self.err(
                    case,
                    path,
                    format!("expected one of {variants:?} ({name}), got {v}"),
                ),
            },
            ShapeKind::Custom(_) => self.custom(case, path, v, name),
            ShapeKind::ErrorCode => match v.as_str() {
                Some(s) if bob_engine::errcode::is_known(s) => {}
                _ => self.err(
                    case,
                    path,
                    format!("expected a canonical A-4 error code, got {v}"),
                ),
            },
        }
    }

    fn shape_value(&mut self, case: &str, path: &str, v: &Value, name: &'static str) {
        let shape = wire_schema::shape(name)
            .unwrap_or_else(|| panic!("model references an undeclared shape '{name}'"));
        let Some(obj) = v.as_object() else {
            self.err(case, path, format!("expected an object ({name}), got {v}"));
            return;
        };
        for f in shape.fields {
            self.field(case, &format!("{path}({name})"), obj.get(f.name), f);
        }
        for key in obj.keys() {
            if !shape.fields.iter().any(|f| f.name == key) {
                self.err(
                    case,
                    path,
                    format!("undeclared field '{key}' for {name}"),
                );
            }
        }
    }

    /// The three shapes described by a literal TS body: validate the JSON the
    /// engine actually produces for them.
    fn custom(&mut self, case: &str, path: &str, v: &Value, name: &'static str) {
        match name {
            "ContentValue" => {
                let kind = v.get("kind").and_then(Value::as_str);
                match kind {
                    Some("inline") => {
                        if !v.get("text").map(Value::is_string).unwrap_or(false) {
                            self.err(case, path, "inline ContentValue needs a string 'text'");
                        }
                    }
                    Some("stored") => {
                        if !v.get("digest").map(Value::is_string).unwrap_or(false)
                            || !v.get("byte_len").map(Value::is_number).unwrap_or(false)
                        {
                            self.err(
                                case,
                                path,
                                "stored ContentValue needs 'digest' + 'byte_len'",
                            );
                        }
                    }
                    _ => self.err(case, path, format!("unknown ContentValue kind: {v}")),
                }
            }
            "ChildSpawned" => {
                if !(v.is_boolean() || v.as_str() == Some("unknown")) {
                    self.err(case, path, format!("child_spawned must be true|false|unknown, got {v}"));
                }
            }
            "ResolveResponse" => {
                let ok = v.as_str() == Some("allow_once")
                    || v.get("deny").map(Value::is_object).unwrap_or(false);
                if !ok {
                    self.err(case, path, format!("not an interaction.resolve response: {v}"));
                }
            }
            other => panic!("unhandled custom shape '{other}' in the model"),
        }
    }
}

// ---------- item 1: the error vocabulary ----------

#[test]
fn generated_module_carries_the_closed_error_vocabulary() {
    let g = tsgen::generate();
    let union = union_block(&g, "WireErrorCode");

    for code in bob_engine::errcode::all_codes() {
        assert!(
            union.contains(&format!("\"{code}\"")),
            "canonical code '{code}' is missing from the generated WireErrorCode union"
        );
    }
    for (alias, canonical) in bob_engine::errcode::all_aliases() {
        assert!(
            !union.contains(&format!("\"{alias}\"")),
            "alias '{alias}' (→ {canonical}) must not be a wire member: the engine maps it before the wire (A-4)"
        );
    }
    // `code` fields are typed by that union, not by a bare string.
    assert!(g.contains("code: WireErrorCode;"), "WireError.code is untyped");
}

/// The text of one generated `export type X = ...;` union.
fn union_block(g: &str, name: &str) -> String {
    let start = g
        .find(&format!("export type {name} ="))
        .unwrap_or_else(|| panic!("{name} is not generated"));
    let rest = &g[start..];
    let end = rest.find(";\n").expect("union terminates");
    rest[..end].to_owned()
}

// ---------- item 1: the literal-bodied shapes ----------

#[test]
fn custom_shapes_match_the_engine_serialization() {
    let g = tsgen::generate();

    // ContentValue: tagged on `kind`, inline above/below the 64 KiB cap.
    let inline = serde_json::to_value(exec::ContentValue::from_bytes(b"bob-slice-fixture\n"))
        .expect("ContentValue serializes");
    assert_eq!(inline["kind"], "inline");
    assert_eq!(inline["text"], "bob-slice-fixture\n");
    assert!(
        g.contains("{ kind: \"inline\"; text: string }"),
        "the generated ContentValue does not describe the engine's inline form"
    );

    let big = vec![b'x'; exec::INLINE_CAP_BYTES + 1];
    let stored = serde_json::to_value(exec::ContentValue::from_bytes(&big))
        .expect("ContentValue serializes");
    assert_eq!(stored["kind"], "stored");
    assert_eq!(stored["byte_len"], exec::INLINE_CAP_BYTES + 1);
    assert!(stored["digest"].as_str().unwrap_or_default().starts_with("sha256:"));
    assert!(
        g.contains("{ kind: \"stored\"; digest: string; byte_len: number }"),
        "the generated ContentValue does not describe the engine's stored form"
    );

    // ChildSpawned: the JSON booleans plus the string "unknown".
    assert_eq!(
        serde_json::to_value(exec::ChildSpawned::True).unwrap(),
        json!(true)
    );
    assert_eq!(
        serde_json::to_value(exec::ChildSpawned::False).unwrap(),
        json!(false)
    );
    assert_eq!(
        serde_json::to_value(exec::ChildSpawned::Unknown).unwrap(),
        json!("unknown")
    );
    assert!(
        g.contains("export type ChildSpawned = true | false | \"unknown\";"),
        "the generated ChildSpawned does not describe the engine's serialization"
    );

    // ResolveResponse: the two accepted wire forms, and nothing boolean.
    let ok = |v: Value| {
        protocol::parse_resolve(&json!({
            "call_id": "call-1",
            "decision_id": "dec-1",
            "args_digest": "sha256:aa",
            "response": v,
        }))
        .is_ok()
    };
    assert!(ok(json!("allow_once")), "allow_once must parse");
    assert!(ok(json!({"deny": {"reason": "not today"}})), "deny must parse");
    assert!(g.contains("export type ResolveResponse = \"allow_once\" | { deny: DenyResponse };"));
    assert!(
        !g.contains("response: boolean"),
        "the affordance is never a boolean (S10 C-1)"
    );
}

// ---------- helpers ----------

fn read_fixture(name: &str) -> String {
    let p = root_path().join(TRANSCRIPT_DIR).join(name);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("transcript {p:?}: {e}"))
}

/// The frames of a committed transcript (each `{"seq":…,"event":…}` line).
fn transcript_frames(name: &str) -> Vec<Value> {
    read_fixture(name)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str::<Value>(l).expect("transcript line is JSON"))
        .filter(|v| v.get("seq").is_some() && v.get("event").is_some())
        .collect()
}

fn tmp_root(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e11-codegen-{tag}-{}-{nanos}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
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
            .stderr(Stdio::null())
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

/// Drive the real handshake + one turn and return the frames the engine
/// offered. Same script the webview runs: `r1` hello, `r2` session.create,
/// `r3` turn.start, every frame acked with its own `through_seq`, `r4`
/// answered from the preview.
fn live_frames(tag: &str, deny: bool) -> Vec<Value> {
    let root = tmp_root(tag);
    let mut cli = Cli::spawn(&root);
    cli.send(&json!({"id": "r1", "method": "hello", "params": {"version": {"major": 2}}}));
    let hello = cli.recv();
    assert_eq!(
        hello["result"]["schema_hash"], PINNED_SCHEMA_HASH,
        "{tag}: the live HelloAck carries a different schema hash"
    );
    cli.send(&json!({"id": "r2", "method": "session.create", "params": {}}));
    let _ = cli.recv();
    cli.send(&json!({"id": "r3", "method": "turn.start", "params": {"model": "slice-model"}}));

    let mut frames = Vec::new();
    let mut seq = 0_u64;
    let mut answered = false;
    loop {
        let v = cli.recv();
        let Some(event) = v.get("event").and_then(Value::as_str) else {
            continue; // a response line (e.g. the turn.start result at the end)
        };
        let event = event.to_owned();
        seq += 1;
        assert_eq!(v["seq"], json!(seq), "{tag}: seq must be gap-free from 1");
        cli.send(&json!({
            "id": format!("ack-{seq}"),
            "method": "event.ack",
            "params": {"through_seq": seq},
        }));
        let last = event == "turn.completed";
        let preview = if event == "approval.requested" {
            Some(v["payload"].clone())
        } else {
            None
        };
        frames.push(v);
        if let (Some(p), false) = (preview, answered) {
            answered = true;
            let response = if deny {
                json!({"deny": {"reason": "not today"}})
            } else {
                json!("allow_once")
            };
            cli.send(&json!({
                "id": "r4",
                "method": "interaction.resolve",
                "params": {
                    "call_id": p["call_id"],
                    "decision_id": p["decision_id"],
                    "args_digest": p["args_digest"],
                    "response": response,
                }
            }));
        }
        if last {
            break;
        }
    }
    assert!(cli.finish().success(), "{tag}: run exits cleanly");
    frames
}
