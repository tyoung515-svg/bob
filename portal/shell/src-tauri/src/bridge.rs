//! The child bridge: spawns `bob-slice-cli` (the E5 slice-v1 NDJSON
//! engine driver) and shuttles verbatim NDJSON lines between the OS pipes
//! and this process. This module is deliberately free of any `tauri`
//! dependency so the scripted e2e battery (below) can drive the REAL
//! engine binaries through exactly the code the webview commands use.
//!
//! S10/STALE-9: the Tauri IPC binding is byte-equivalent to the NDJSON
//! stdio binding because both are verbatim pass-throughs of these lines —
//! no field is added, renamed, dropped or re-ordered here.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread;

/// Everything the webview side needs to know about the child, as events.
#[derive(Debug, Clone)]
pub enum BridgeEvent {
    /// One verbatim NDJSON line from the child's stdout (frame or
    /// request-response).
    Line(String),
    /// One diagnostics line from the child's stderr (never a lifecycle
    /// source; rendered in a diagnostics pane only).
    Stderr(String),
    /// The child exited with this code. Exit-code meanings are the E5
    /// contract (0 ok, 2 EOF pre-hello, 3 malformed, 4 version_mismatch,
    /// 5 EOF while approval pending, 6 engine error).
    Exit(Option<i32>),
}

/// The write half: the child's stdin. Dropping it closes the pipe, which
/// the engine observes as transport EOF (it never cancels a pending turn).
pub struct SliceStdin {
    stdin: ChildStdin,
}

impl SliceStdin {
    /// Write one NDJSON request line (a trailing newline is appended iff
    /// the caller did not supply one).
    pub fn send_line(&mut self, line: &str) -> std::io::Result<()> {
        let mut owned = String::with_capacity(line.len() + 1);
        owned.push_str(line);
        if !owned.ends_with('\n') {
            owned.push('\n');
        }
        self.stdin.write_all(owned.as_bytes())?;
        self.stdin.flush()
    }
}

/// Locate the repo root by looking for the engine crate, walking up from
/// the current directory and from the shell executable's location.
pub fn find_repo_root() -> Option<PathBuf> {
    let mut bases: Vec<PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        bases.push(cwd);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            bases.push(dir.to_path_buf());
        }
    }
    for base in bases {
        for dir in base.ancestors() {
            if dir.join("engine").join("src").join("bin").join("bob-slice-cli.rs").is_file() {
                return Some(dir.to_path_buf());
            }
        }
    }
    None
}

fn exe_name(base: &str) -> String {
    if cfg!(windows) {
        format!("{base}.exe")
    } else {
        base.to_string()
    }
}

/// The engine binaries live in the cargo WORKSPACE root's target dir
/// (`<repo>/target/debug/`, where E5 built them), with
/// `<repo>/engine/target/debug/` tolerated as a fallback.
fn engine_bin(base: &str) -> Option<PathBuf> {
    let name = exe_name(base);
    for target in ["target", "engine/target"] {
        let p = find_repo_root()?.join(target).join("debug").join(&name);
        if p.is_file() {
            return Some(p);
        }
    }
    None
}

/// The `bob-slice-cli` binary path: `BOB_SLICE_CLI` env override first,
/// then the built engine binaries.
pub fn default_cli() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("BOB_SLICE_CLI") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    engine_bin("bob-slice-cli")
}

/// The `fx-read` fixture executor path: `BOB_FX_READ` env override first,
/// then the built engine binaries.
pub fn default_fx_read() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("BOB_FX_READ") {
        let p = PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    engine_bin("fx-read")
}

/// Spawn `bob-slice-cli --data-root <root> --fx-read <fx>` with piped
/// stdio and return the write half plus the event stream. Reader threads
/// forward verbatim lines; the reaper thread owns the child so a dropped
/// `SliceStdin` yields EOF (E5 exit-code posture) and the child can never
/// be orphaned.
pub fn spawn(cli: &Path, fx_read: &Path, data_root: &Path) -> std::io::Result<(SliceStdin, Receiver<BridgeEvent>)> {
    std::fs::create_dir_all(data_root)?;
    let mut child: Child = Command::new(cli)
        .arg("--data-root")
        .arg(data_root)
        .arg("--fx-read")
        .arg(fx_read)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    let stdin = child.stdin.take().expect("stdin piped");
    let stdout = child.stdout.take().expect("stdout piped");
    let stderr = child.stderr.take().expect("stderr piped");

    let (tx, rx) = mpsc::channel::<BridgeEvent>();

    let tx_out = tx.clone();
    thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.split(b'\n') {
            match line {
                Ok(bytes) => {
                    let mut s = String::from_utf8_lossy(&bytes).into_owned();
                    if s.ends_with('\r') {
                        s.pop();
                    }
                    if tx_out.send(BridgeEvent::Line(s)).is_err() {
                        return;
                    }
                }
                Err(_) => return,
            }
        }
    });

    let tx_err = tx.clone();
    thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.split(b'\n') {
            match line {
                Ok(bytes) => {
                    let mut s = String::from_utf8_lossy(&bytes).into_owned();
                    if s.ends_with('\r') {
                        s.pop();
                    }
                    if tx_err.send(BridgeEvent::Stderr(s)).is_err() {
                        return;
                    }
                }
                Err(_) => return,
            }
        }
    });

    thread::spawn(move || {
        // The reaper: `child` is moved here, so it is always waited on.
        let code = child.wait().ok().and_then(|s| s.code());
        let _ = tx.send(BridgeEvent::Exit(code));
    });

    Ok((SliceStdin { stdin }, rx))
}

/// Resolve the data root of a preview run.
///
/// Precedence: the caller's explicit value, then the launcher's
/// `BOB_PREVIEW_DATA_ROOT` (`scripts/linux/run-preview.sh` sets it so the run's
/// fixture/ledger land in that run's documented cache directory), then the
/// historical process-scoped temp dir. Pure, so the precedence is testable
/// without a Tauri app handle.
pub fn data_root_for(arg: Option<&str>, env_root: Option<String>, pid: u32) -> PathBuf {
    if let Some(p) = arg {
        let trimmed = p.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    if let Some(p) = env_root {
        let trimmed = p.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    std::env::temp_dir().join(format!("bob-e6-shell-{pid}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
    use std::time::Duration;

    /// A scripted client over the bridge: sends request lines, collects
    /// frames and responses verbatim, with a wall-clock timeout so a hung
    /// engine fails the test instead of the runner.
    struct Driver {
        stdin: Option<SliceStdin>,
        rx: Receiver<BridgeEvent>,
        frames: Vec<Value>,
        responses: Vec<Value>,
        stderrs: Vec<String>,
        exit: Option<Option<i32>>,
    }

    impl Driver {
        fn start(tag: &str) -> Driver {
            let cli = default_cli().unwrap_or_else(|| panic!("bob-slice-cli not found; run `cargo build` at the repo root first (tag {tag})"));
            let fx = default_fx_read().unwrap_or_else(|| panic!("fx-read not found; run `cargo build` at the repo root first (tag {tag})"));
            let root = std::env::temp_dir().join(format!("bob-e6-shell-{tag}-{}", std::process::id()));
            let (stdin, rx) = spawn(&cli, &fx, &root).expect("bridge spawns bob-slice-cli");
            Driver { stdin: Some(stdin), rx, frames: Vec::new(), responses: Vec::new(), stderrs: Vec::new(), exit: None }
        }

        fn send(&mut self, id: &str, method: &str, params: Value) {
            let line = json!({"id": id, "method": method, "params": params}).to_string();
            self.stdin.as_mut().expect("stdin still open").send_line(&line).expect("send request line");
        }

        fn resolve(&mut self, id: &str, p: &Value, digest_override: Option<&str>) {
            let mut params = p.clone();
            if let Some(d) = digest_override {
                params["args_digest"] = json!(d);
            }
            self.send(id, "interaction.resolve", params);
        }

        /// Pump events until `stop` returns true for the just-observed
        /// event summary, or the timeout (30 s) elapses.
        fn pump_until<F: FnMut(&Value, &Value, Option<Option<i32>>) -> bool>(&mut self, mut stop: F) {
            let deadline = std::time::Instant::now() + Duration::from_secs(30);
            loop {
                let ev = self.rx.recv_timeout(deadline.saturating_duration_since(std::time::Instant::now())).expect("engine event within 30s");
                match ev {
                    BridgeEvent::Line(l) => {
                        let v: Value = serde_json::from_str(&l).expect("child stdout line is JSON");
                        if v.get("event").is_some() {
                            self.frames.push(v);
                        } else {
                            self.responses.push(v);
                        }
                        let f = self.frames.last().cloned().unwrap_or(Value::Null);
                        let r = self.responses.last().cloned().unwrap_or(Value::Null);
                        if stop(&f, &r, None) {
                            return;
                        }
                    }
                    BridgeEvent::Stderr(s) => {
                        self.stderrs.push(s);
                    }
                    BridgeEvent::Exit(code) => {
                        self.exit = Some(code);
                        if stop(&Value::Null, &Value::Null, self.exit) {
                            return;
                        }
                    }
                }
            }
        }

        /// Drive handshake + session + turn, stopping at the
        /// `approval.requested` frame; returns its payload.
        fn run_to_approval(&mut self) -> Value {
            self.send("r1", "hello", json!({"version": {"major": 2}}));
            self.pump_until(|_f, r, _e| r.get("id") == Some(&json!("r1")));
            self.send("r2", "session.create", json!({}));
            self.pump_until(|_f, r, _e| r.get("id") == Some(&json!("r2")));
            self.send("r3", "turn.start", json!({"model": "slice-model"}));
            self.pump_until(|f, _r, _e| f.get("event") == Some(&json!("approval.requested")));
            self.frames.last().unwrap()["payload"].clone()
        }

        fn wait_turn_end(&mut self) {
            self.pump_until(|f, r, _e| {
                f.get("event") == Some(&json!("turn.completed"))
                    || r.get("id") == Some(&json!("r3"))
            });
        }

        /// Close the transport (EOF) and reap the child. MUST be used
        /// instead of dropping mid-test: the E5 lesson — drop stdin
        /// first, then wait, or the child never sees EOF.
        fn finish(&mut self) -> Option<i32> {
            self.stdin = None;
            self.pump_until(|_f, _r, e| e.is_some());
            self.exit.flatten()
        }
    }

    fn corrupt_digest(d: &str) -> String {
        // Flip the last hex character in a way that can never be a no-op.
        let mut s = d.to_string();
        let last = s.pop().unwrap();
        s.push(if last == '0' { '1' } else { '0' });
        s
    }

    #[test]
    fn e2e_allow_once_path_over_the_bridge() {
        let mut d = Driver::start("allow");
        let preview = d.run_to_approval();
        assert_eq!(preview["call_id"], json!("call-1"));
        assert!(preview["decision_id"].is_string());
        assert!(preview["args_digest"].as_str().unwrap().starts_with("sha256:"));

        // event.ack is honored mid-wait (S5 §2): ack the preview frame
        // before answering, exactly as the UI does.
        let seq = d.frames.last().unwrap()["seq"].as_u64().unwrap();
        d.send("ack-1", "event.ack", json!({"through_seq": seq}));
        d.pump_until(|_f, r, _e| r.get("id") == Some(&json!("ack-1")));

        let resolve = json!({
            "call_id": preview["call_id"],
            "decision_id": preview["decision_id"],
            "args_digest": preview["args_digest"],
            "response": "allow_once",
        });
        d.resolve("r4", &resolve, None);
        d.wait_turn_end();

        let events: Vec<String> = d.frames.iter().map(|f| f["event"].as_str().unwrap().to_string()).collect();
        assert_eq!(
            events,
            vec![
                "turn.started", "tool.call", "authority.decided", "approval.requested",
                "approval.resolved", "tool.started", "tool.result", "turn.completed"
            ]
        );
        let tool_result = d.frames.iter().find(|f| f["event"] == json!("tool.result")).unwrap();
        assert_eq!(tool_result["payload"]["status"], json!("succeeded"));
        // Close the transport and reap (drop stdin BEFORE waiting: the
        // E5 lesson — otherwise the child never sees EOF).
        assert_eq!(d.finish(), Some(0), "clean exit after allow_once");
    }

    #[test]
    fn e2e_deny_with_reason_over_the_bridge() {
        let mut d = Driver::start("deny");
        let preview = d.run_to_approval();
        let resolve = json!({
            "call_id": preview["call_id"],
            "decision_id": preview["decision_id"],
            "args_digest": preview["args_digest"],
            "response": {"deny": {"reason": "not today"}},
        });
        d.resolve("r4", &resolve, None);
        d.wait_turn_end();

        let events: Vec<String> = d.frames.iter().map(|f| f["event"].as_str().unwrap().to_string()).collect();
        assert_eq!(
            events,
            vec![
                "turn.started", "tool.call", "authority.decided", "approval.requested",
                "approval.resolved", "tool.result", "turn.completed"
            ]
        );
        let tool_result = d.frames.iter().find(|f| f["event"] == json!("tool.result")).unwrap();
        assert_eq!(tool_result["payload"]["status"], json!("denied"));
        let resolved = d.frames.iter().find(|f| f["event"] == json!("approval.resolved")).unwrap();
        assert_eq!(resolved["payload"]["reason"], json!("not today"), "reason verbatim");
        assert_eq!(d.finish(), Some(0));
    }

    #[test]
    fn e2e_mismatched_args_digest_echo_is_rejected_and_stays_pending() {
        let mut d = Driver::start("mismatch");
        let preview = d.run_to_approval();
        let good = json!({
            "call_id": preview["call_id"],
            "decision_id": preview["decision_id"],
            "args_digest": preview["args_digest"],
            "response": "allow_once",
        });

        // Corrupted echo -> approval_mismatch error response, request
        // stays pending (AT-S5-06): no new frame may appear.
        let frames_before = d.frames.len();
        d.resolve("r4", &good, Some(&corrupt_digest(preview["args_digest"].as_str().unwrap())));
        d.pump_until(|_f, r, _e| r.get("id") == Some(&json!("r4")));
        let err = d.responses.last().unwrap();
        assert_eq!(err["error"]["code"], json!("approval_mismatch"), "wire error code verbatim");
        assert_eq!(d.frames.len(), frames_before, "nothing appended on mismatch");

        // The SAME id may be retried with fixed params (EXEC-43: the
        // mismatch response is uncached). The turn then completes.
        d.resolve("r4", &good, None);
        d.wait_turn_end();
        let tool_result = d.frames.iter().find(|f| f["event"] == json!("tool.result")).unwrap();
        assert_eq!(tool_result["payload"]["status"], json!("succeeded"));
        assert_eq!(d.finish(), Some(0));
    }

    #[test]
    fn e2e_version_mismatch_is_terminal() {
        let mut d = Driver::start("ver");
        d.send("r1", "hello", json!({"version": {"major": 1}}));
        // Wait for the response itself: the bridge may report the child's exit
        // before its buffered stdout line reaches this thread, and treating the
        // exit as the stop condition made this test flaky (observed once in
        // MVP3 validation, 5/5 clean on rerun).
        d.pump_until(|_f, r, _e| r.get("id") == Some(&json!("r1")));
        let err = d.responses.last().unwrap();
        assert_eq!(err["error"]["code"], json!("version_mismatch"));
        assert_eq!(d.finish(), Some(4), "E5 exit code 4 = version_mismatch");
    }

    // ---- AT-1 (EXEC-48): the Tauri IPC leg of the projection-checksum harness ----

    /// One DecisionRecord -> the AT-1 chain row (state + reason code + rule ref).
    fn chain_entry(rec: &Value) -> Value {
        json!({
            "state": rec["state"].clone(),
            "reason": rec["reason"]["code"].clone(),
            "rule": rec["matched_rule_ref"].clone(),
        })
    }

    fn chain_entries(rec: &Value) -> Vec<Value> {
        if rec.is_null() { Vec::new() } else { vec![chain_entry(rec)] }
    }

    /// Project frames received over the IPC event channel into the AT-1 shape
    /// (see shell/harness/projection.cjs for the definition both languages
    /// share). Volatile ids are excluded; the R8 causal link is resolved to the
    /// ordinals it cites ("external" for the framed-less step.context).
    fn at1_projection(frames: &[Value]) -> Value {
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
        let terminal = first("tool.result")
            .map(|f| f["payload"]["authority_terminal"].clone())
            .unwrap_or(Value::Null);
        let call = json!({
            "call_id": first("tool.call").map(|f| f["payload"]["call_id"].clone()).unwrap_or(Value::Null),
            "decided_outcome": first("authority.decided").map(|f| f["payload"]["outcome"].clone()).unwrap_or(Value::Null),
            "decided_source": first("authority.decided").map(|f| f["payload"]["source"].clone()).unwrap_or(Value::Null),
            "resolution": first("approval.resolved").map(|f| f["payload"]["resolution"].clone()).unwrap_or(Value::Null),
            "status": first("tool.result").map(|f| f["payload"]["status"].clone()).unwrap_or(Value::Null),
            "error_code": first("tool.result")
                .map(|f| if f["payload"]["error"].is_null() { Value::Null } else { f["payload"]["error"]["code"].clone() })
                .unwrap_or(Value::Null),
            "terminal_state": if terminal.is_null() { Value::Null } else { terminal["state"].clone() },
            "output_digest": if terminal.is_null() { Value::Null } else { terminal["reason"]["params"]["target_id"].clone() },
            "witness_present": frames.iter().any(|f| f["event"] == json!("tool.started")),
            "n_results": frames.iter().filter(|f| f["event"] == json!("tool.result")).count(),
        });
        json!({"frames": rows, "call": call})
    }

    fn at1_fixture(name: &str) -> Value {
        let p = find_repo_root()
            .expect("repo root")
            .join("engine")
            .join("tests")
            .join("fixtures")
            .join("transcripts")
            .join(name);
        serde_json::from_str(
            &std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("fixture {p:?}: {e}")),
        )
        .expect("projection fixture is JSON")
    }

    /// EXEC-48 / S10 AT-1: the same ledger projected through the Tauri IPC
    /// binding must equal the projection the engine derives from its own views
    /// (committed as a fixture by engine/tests/e10_at1.rs), for both V0 paths.
    /// No crypto dependency is needed here: the comparison is of the projected
    /// view rows themselves, which is what the checksum is taken over.
    #[test]
    fn at1_ipc_binding_projection_matches_the_ledger_projection() {
        for (tag, fixture, response) in [
            ("at1allow", "v0_allow.projection.json", json!("allow_once")),
            ("at1deny", "v0_deny.projection.json", json!({"deny": {"reason": "not today"}})),
        ] {
            let mut d = Driver::start(tag);
            let preview = d.run_to_approval();
            let params = json!({
                "call_id": preview["call_id"],
                "decision_id": preview["decision_id"],
                "args_digest": preview["args_digest"],
                "response": response,
            });
            d.resolve("r4", &params, None);
            d.wait_turn_end();
            assert_eq!(d.finish(), Some(0), "{tag}: clean exit");

            let ipc = at1_projection(&d.frames);
            let expected = at1_fixture(fixture);
            assert_eq!(
                ipc["call"], expected["call"],
                "{tag}: IPC view row differs from the ledger-projected view row"
            );
            assert_eq!(
                ipc["frames"], expected["frames"],
                "{tag}: IPC per-frame projection differs from the ledger projection"
            );
            assert_eq!(ipc, expected, "{tag}: AT-1 projection equality over the IPC binding");
        }
    }

    /// MVP1 usability: a launcher-provided data root must be used, so the
    /// preview's fixture and ledger land in the run directory the launcher
    /// documents (and `rm -rf ~/.cache/bob-preview` resets exactly that).
    /// The caller's explicit value still wins, and a blank/absent value keeps
    /// the historical process-scoped temp dir.
    #[test]
    fn preview_data_root_prefers_explicit_then_launcher_env_then_temp() {
        let env = Some("/cache/bob-preview/runs/run-1/data".to_string());
        assert_eq!(
            data_root_for(None, env.clone(), 7),
            PathBuf::from("/cache/bob-preview/runs/run-1/data"),
            "BOB_PREVIEW_DATA_ROOT must be honoured when the caller passes no root"
        );
        assert_eq!(
            data_root_for(Some("/explicit/root"), env.clone(), 7),
            PathBuf::from("/explicit/root"),
            "an explicit caller root outranks the launcher env"
        );
        assert_eq!(
            data_root_for(Some("   "), env.clone(), 7),
            PathBuf::from("/cache/bob-preview/runs/run-1/data"),
            "a blank caller root is absent, not literal"
        );
        assert!(
            data_root_for(None, None, 7).ends_with("bob-e6-shell-7"),
            "no env and no caller root keeps the per-process temp dir"
        );
        assert!(
            data_root_for(None, Some("  ".into()), 7).ends_with("bob-e6-shell-7"),
            "a blank env value is absent, not literal"
        );
    }
}
