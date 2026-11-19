//! S1 ledger tests — post-ruling slice contract (E2): six-event vocabulary
//! (RULING-4), durable start-witness (RULING-5, RATIFY-4), five call statuses
//! with the R2-amended classification rule (RULING-3, RATIFY-1/2),
//! `approval.resolved.resolution ∈ {allow, deny, cancelled}` (RATIFY-3),
//! RULING-8 `caused_by` chain, typed `ledger_tail_corrupt` (RULING-6), and the
//! crash-point matrix (S1 §4b CP-1..CP-9).
//!
//! Each test uses a fresh temp `$DATA_ROOT`; the ledger layout is
//! `$DATA_ROOT/ledgers/<session_id>/segments/<segment_id>.audit.jsonl` (S1.10).

use std::path::PathBuf;

use bob_engine::authority::*;
use bob_engine::envelope::{Actor, EventKind, Record, SCHEMA_VERSION};
use bob_engine::ledger::{segment_path, AckedCall, DurableAck, Ledger, LedgerError, SessionIds};
use bob_engine::payload::*;
use bob_engine::replay::{replay_segment, scan_bytes, ReplayDiagnostic, ReplayOutcome};

fn tmp_root(tag: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e2-{tag}-{}-{nanos}-{}",
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

/// Append `turn.start` + `step.context`; returns their event ids.
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

/// An admitted `tool.call` under an existing step context.
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

/// A consistent `authority.decided`: outcome ↔ decision.state, source ↔
/// matched_rule_ref (T3a S1 §3).
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

/// The RULING-7 nine-field ApprovalPreview.
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
        decision: Some(decision(
            id,
            DecisionState::Approved,
            "reviewer_approved",
            "approved by dad",
        )),
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

/// Drive a call through decided(ask) → requested → resolved(allow) → started.
/// Returns (decided, requested, resolved, witness) acks.
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

fn indeterminate_result(call_id: &str, gid: &str, origin: IndeterminateOrigin) -> ToolResult {
    ToolResult {
        call_id: call_id.into(),
        status: ResultStatus::Indeterminate,
        output: None,
        output_digest: None,
        error: Some(ResultError {
            code: "executor_lost".into(),
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

fn last_of_kind<'a>(outcome: &'a ReplayOutcome, kind: &str) -> &'a Record {
    outcome
        .assertions
        .iter()
        .rev()
        .find(|r| r.kind.as_str() == kind)
        .unwrap()
}

/// Happy path plus ordering: the full six-event flow appends in write order
/// with ordinals strictly increasing from base 1, and replays clean
/// (I-01..I-05, I-12, RULING-4).
#[test]
fn append_ordering_and_ordinal_monotonicity() {
    let root = tmp_root("order");
    let (mut ledger, ack_lineage) = Ledger::open_new(&root, ids("order")).unwrap();
    assert_eq!(ack_lineage.ordinal, 1);
    let ack_turn = ledger.append_turn_start("turn-1", turn_start()).unwrap();
    let ack_step = ledger
        .append_step_context("turn-1", "step-1", step_context())
        .unwrap();
    let acked = admitted(&mut ledger, "turn-1", "step-1", &ack_step.event_id, "call-1");
    assert_eq!(acked.ack().ordinal, 4);
    let (d, q, r, s) = ask_allow_started(&mut ledger, &acked, "dec-1");
    let ack_result = ledger
        .record_post_witness_result(s, ok_result("call-1", "dec-1"))
        .unwrap();
    let ack_end = ledger
        .append_turn_end("turn-1", TurnStatus::Succeeded, Some("step-1"), None)
        .unwrap();

    let ordinals = [
        ack_lineage.ordinal,
        ack_turn.ordinal,
        ack_step.ordinal,
        acked.ack().ordinal,
        d.ordinal,
        q.ordinal,
        r.ordinal,
        ack_result.ordinal - 1, // witness sits between resolved and result
        ack_result.ordinal,
        ack_end.ordinal,
    ];
    assert_eq!(ordinals, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    assert_eq!(ledger.flush().unwrap(), 10);

    let outcome = replay_segment(ledger.path()).unwrap();
    assert_eq!(outcome.diagnostics, vec![], "clean segment, no diagnostics");
    let kinds: Vec<&str> = outcome.assertions.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(
        kinds,
        [
            "lineage",
            "turn.start",
            "step.context",
            "tool.call",
            "authority.decided",
            "approval.requested",
            "approval.resolved",
            "tool.started",
            "tool.result",
            "turn.end"
        ],
        "RULING-4 slice-v1 event set in ledger order"
    );
    assert_eq!(
        outcome.assertions[9].payload["open_calls"],
        serde_json::json!([])
    );
    std::fs::remove_dir_all(&root).ok();
}

/// `ordinal` (and `call_index`) are u64-strings on the wire (S1.3, S1.4.2).
#[test]
fn ordinals_are_strings_on_the_wire() {
    let root = tmp_root("wire");
    let (mut ledger, _) = Ledger::open_new(&root, ids("wire")).unwrap();
    ledger.append_turn_start("turn-1", turn_start()).unwrap();
    let raw = std::fs::read_to_string(ledger.path()).unwrap();
    assert!(raw.contains("\"ordinal\":\"1\""), "ordinal serialized as string: {raw}");
    assert!(raw.contains("\"ordinal\":\"2\""));
    for line in raw.lines() {
        let v: serde_json::Value = serde_json::from_str(line).unwrap();
        for f in [
            "schema_version", "event_id", "timestamp", "ordinal", "session_id", "thread_id",
            "lineage_id", "segment_id", "turn_id", "step_id", "actor", "kind", "correlation_id",
            "caused_by", "payload",
        ] {
            assert!(v.get(f).is_some(), "missing frame field {f} in {line}");
        }
    }
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-8: timestamps are fixed-width `YYYY-MM-DDTHH:MM:SS.sssZ`.
#[test]
fn timestamps_are_fixed_width_ms_z() {
    let root = tmp_root("ts");
    let (mut ledger, _) = Ledger::open_new(&root, ids("ts")).unwrap();
    ledger.append_turn_start("turn-1", turn_start()).unwrap();
    let raw = std::fs::read_to_string(ledger.path()).unwrap();
    for line in raw.lines() {
        let v: serde_json::Value = serde_json::from_str(line).unwrap();
        let ts = v["timestamp"].as_str().unwrap();
        assert_eq!(ts.len(), 24, "fixed width: {ts}");
        assert!(ts.ends_with('Z'), "Z suffix: {ts}");
        assert_eq!(&ts[10..11], "T");
        assert_eq!(&ts[19..20], ".");
        assert!(ts[..19].chars().all(|c| c.is_ascii_digit() || "T:-".contains(c)));
        assert!(ts[20..23].chars().all(|c| c.is_ascii_digit()));
    }
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-8 reader side: a non-`Z` timestamp is rejected — as
/// `ledger_tail_corrupt` when it is the final line.
#[test]
fn bad_timestamp_rejected_at_replay() {
    let root = tmp_root("badts");
    let (mut ledger, _) = Ledger::open_new(&root, ids("badts")).unwrap();
    ledger.append_turn_start("turn-1", turn_start()).unwrap();
    let raw = std::fs::read_to_string(ledger.path()).unwrap();
    drop(ledger);
    let mut lines: Vec<String> = raw.lines().map(str::to_owned).collect();
    let last = lines.last_mut().unwrap();
    let v: serde_json::Value = serde_json::from_str(last).unwrap();
    let good_ts = v["timestamp"].as_str().unwrap().to_owned();
    *last = last.replace(&good_ts, "2026-10-16T00:00:00+07:00");
    let path = root.join("badts.audit.jsonl");
    std::fs::write(&path, lines.join("\n") + "\n").unwrap();
    let outcome = replay_segment(&path).unwrap();
    assert_eq!(outcome.assertions.len(), 1, "the corrupt final line is not a record");
    assert!(outcome.diagnostics.iter().any(|d| matches!(
        d,
        ReplayDiagnostic::LedgerTailCorrupt { last_ordinal, .. } if *last_ordinal == Some(1)
    )));
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-6 / I-01: an unterminated final line is not a record; the typed
/// diagnostic carries the byte offset of the tail and the last valid ordinal.
#[test]
fn torn_tail_surfaces_byte_offset_and_last_ordinal() {
    let root = tmp_root("torn");
    let path = root.join("torn.audit.jsonl");
    let (mut ledger, _) = Ledger::open_new(&root, ids("torn")).unwrap();
    ledger.append_turn_start("turn-1", turn_start()).unwrap();
    let mut bytes = std::fs::read(ledger.path()).unwrap();
    drop(ledger);
    let partial = b"{\"schema_version\":\"1.0\",\"event_id\":\"evt-partial";
    let expected_offset = bytes.len() as u64;
    bytes.extend_from_slice(partial);
    std::fs::write(&path, &bytes).unwrap();
    let outcome = replay_segment(&path).unwrap();
    assert_eq!(outcome.assertions.len(), 2, "partial line must not parse as a record");
    assert!(outcome.diagnostics.iter().any(|d| matches!(
        d,
        ReplayDiagnostic::LedgerTailCorrupt { byte_offset, last_ordinal }
            if *byte_offset == expected_offset && *last_ordinal == Some(2)
    )));
    std::fs::remove_dir_all(&root).ok();
}

/// S1.3/S1.9: unknown additive payload fields survive parse and re-serialize.
#[test]
fn unknown_additive_payload_fields_preserved() {
    let root = tmp_root("additive");
    let (mut ledger, _) = Ledger::open_new(&root, ids("additive")).unwrap();
    ledger.append_turn_start("turn-1", turn_start()).unwrap();
    let raw = std::fs::read_to_string(ledger.path()).unwrap();
    drop(ledger);
    let mut lines: Vec<String> = raw.lines().map(str::to_owned).collect();
    lines[1] = lines[1].replace(
        "\"input_digest\":\"sha256:fixture\"",
        "\"input_digest\":\"sha256:fixture\",\"future_field\":{\"nested\":[1,2,3]}",
    );
    let path = root.join("additive.audit.jsonl");
    std::fs::write(&path, lines.join("\n") + "\n").unwrap();

    let outcome = replay_segment(&path).unwrap();
    assert_eq!(outcome.assertions.len(), 2);
    let payload = &outcome.assertions[1].payload;
    assert_eq!(payload["future_field"], serde_json::json!({"nested": [1, 2, 3]}));
    let reserialized = outcome.assertions[1].to_line().unwrap();
    assert!(reserialized.contains("\"future_field\":{\"nested\":[1,2,3]}"));
    std::fs::remove_dir_all(&root).ok();
}

/// I-17: duplicate call ids are rejected by the sequencer.
#[test]
fn duplicate_call_id_rejected() {
    let root = tmp_root("dup");
    let (mut ledger, _) = Ledger::open_new(&root, ids("dup")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let first = admitted(&mut ledger, "t", "s", &sc, "c");
    let err = ledger
        .append_tool_call("t", "s", tool_call("c", Disposition::AdmittedToDispatch, &sc))
        .unwrap_err();
    assert!(matches!(err, LedgerError::DuplicateCallId(_)));
    // After the call closes, reuse is still rejected.
    ledger
        .record_authority_decided(
            &first,
            decided("d1", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#allow")),
        )
        .unwrap();
    ledger
        .record_pre_witness_result(first, aborted_result("c", Some("d1"), "cancelled_before_start"))
        .unwrap();
    let err = ledger
        .append_tool_call("t", "s", tool_call("c", Disposition::AdmittedToDispatch, &sc))
        .unwrap_err();
    assert!(matches!(err, LedgerError::DuplicateCallId(_)));
    std::fs::remove_dir_all(&root).ok();
}

/// S1 §4a row 1: gate not entered (unknown tool / router / readiness / hook
/// reject) → `failed / not_started`, `caused_by` the `tool.call` event.
#[test]
fn rejected_before_start_row1() {
    let root = tmp_root("rej");
    let (mut ledger, _) = Ledger::open_new(&root, ids("rej")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let acked = ledger
        .append_tool_call("t", "s", tool_call("c", Disposition::RejectedBeforeStart, &sc))
        .unwrap();
    // Wrong start_state fails closed.
    let bad = failed_result("c", None, "unknown_tool", StartState::Started);
    let err = ledger.record_pre_witness_result(acked, bad).unwrap_err();
    assert!(matches!(err, LedgerError::RejectedRequiresFailedNotStarted));

    let acked2 = ledger
        .append_tool_call("t", "s", tool_call("c2", Disposition::RejectedBeforeStart, &sc))
        .unwrap();
    ledger
        .record_pre_witness_result(acked2, failed_result("c2", None, "unknown_tool", StartState::NotStarted))
        .unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("failed"));
    assert_eq!(result.payload["error"]["code"], serde_json::json!("unknown_tool"));
    // caused_by must be the tool.call event of the same call (S1 §4a row 1).
    let call2 = outcome
        .assertions
        .iter()
        .find(|r| r.kind == EventKind::ToolCall && r.payload["call_id"] == serde_json::json!("c2"))
        .unwrap();
    assert_eq!(result.caused_by.as_deref(), Some(call2.event_id.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-8 + MGR-5: the full causal chain and correlation on all six call
/// events.
#[test]
fn caused_by_chain_ruling8() {
    let root = tmp_root("chain");
    let (mut ledger, lineage) = Ledger::open_new(&root, ids("chain")).unwrap();
    let (t, sc) = prefix(&mut ledger, "t", "s");
    let acked = admitted(&mut ledger, "t", "s", &sc, "c");
    let (d, q, r, s) = ask_allow_started(&mut ledger, &acked, "dec-1");
    let res = ledger
        .record_post_witness_result(s, ok_result("c", "dec-1"))
        .unwrap();
    let end = ledger
        .append_turn_end("t", TurnStatus::Succeeded, Some("s"), None)
        .unwrap();

    let outcome = replay_segment(ledger.path()).unwrap();
    let a = &outcome.assertions;
    assert_eq!(a[0].kind, EventKind::Lineage);
    assert_eq!(a[0].caused_by, None, "lineage caused_by null (MGR-5)");
    assert_eq!(a[1].caused_by, None, "turn.start caused_by null (MGR-5)");
    assert_eq!(a[2].caused_by.as_deref(), Some(t.as_str()), "step.context → turn.start");
    assert_eq!(a[3].caused_by.as_deref(), Some(sc.as_str()), "tool.call → step.context");
    assert_eq!(a[4].caused_by.as_deref(), Some(acked.ack().event_id.as_str()), "decided → call");
    assert_eq!(a[5].caused_by.as_deref(), Some(d.event_id.as_str()), "requested → decided");
    assert_eq!(a[6].caused_by.as_deref(), Some(q.event_id.as_str()), "resolved → requested");
    assert_eq!(a[7].caused_by.as_deref(), Some(r.event_id.as_str()), "started → resolved");
    assert_eq!(a[8].caused_by.as_deref(), Some(a[7].event_id.as_str()), "result → witness");
    assert_eq!(a[9].caused_by.as_deref(), Some(res.event_id.as_str()), "turn.end → last durable event of the turn");
    // correlation_id = call_id on ALL six call events; null elsewhere.
    for rec in &a[3..=8] {
        assert_eq!(rec.correlation_id.as_deref(), Some("c"), "{}", rec.kind.as_str());
    }
    for rec in [&a[0], &a[1], &a[2], &a[9]] {
        assert_eq!(rec.correlation_id, None, "{}", rec.kind.as_str());
    }
    let _ = (lineage, end);
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-4: a rule `allow` skips both approval events; `tool.started` points
/// at `authority.decided`.
#[test]
fn rule_allow_path_skips_approval() {
    let root = tmp_root("ruleallow");
    let (mut ledger, _) = Ledger::open_new(&root, ids("ruleallow")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let acked = admitted(&mut ledger, "t", "s", &sc, "c");
    let d = ledger
        .record_authority_decided(
            &acked,
            decided("dec-1", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#allow")),
        )
        .unwrap();
    // approval.requested is only emitted on ask.
    let err = ledger
        .record_approval_requested(&acked, preview("c", "dec-1"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::ApprovalOutcomeNotAsk(_)));
    let s = ledger
        .record_tool_started(&acked, started("c", "dec-1"))
        .unwrap();
    assert_eq!(s.witness().event_id.len(), 36);
    let witness_event = s.witness().event_id.clone();
    ledger
        .record_post_witness_result(s, ok_result("c", "dec-1"))
        .unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let started_rec = last_of_kind(&outcome, "tool.started");
    assert_eq!(started_rec.caused_by.as_deref(), Some(d.event_id.as_str()));
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.caused_by.as_deref(), Some(witness_event.as_str()));
    assert_eq!(result.payload["status"], serde_json::json!("succeeded"));
    assert!(outcome
        .assertions
        .iter()
        .all(|r| !r.kind.as_str().starts_with("approval.")));
    std::fs::remove_dir_all(&root).ok();
}

/// T3a S1 §3: `authority.decided` internal consistency is enforced.
#[test]
fn authority_decided_consistency_enforced() {
    let root = tmp_root("decided");
    let (mut ledger, _) = Ledger::open_new(&root, ids("decided")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");

    // outcome ↔ decision.state mismatch.
    let a = admitted(&mut ledger, "t", "s", &sc, "a");
    let mut bad = decided("d-a", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#x"));
    bad.decision.state = DecisionState::Denied;
    let err = ledger.record_authority_decided(&a, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidAuthorityDecided(_)));

    // source=rule requires matched_rule_ref.
    let b = admitted(&mut ledger, "t", "s", &sc, "b");
    let bad = decided("d-b", DecisionOutcome::Allow, DecisionSource::Rule, None);
    let err = ledger.record_authority_decided(&b, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidAuthorityDecided(_)));

    // decision_ref must equal decision.decision_id.
    let c = admitted(&mut ledger, "t", "s", &sc, "c");
    let mut bad = decided("d-c", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#y"));
    bad.decision_ref = "someone-else".into();
    let err = ledger.record_authority_decided(&c, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidAuthorityDecided(_)));

    // One gate decision per call.
    let d = admitted(&mut ledger, "t", "s", &sc, "d");
    ledger
        .record_authority_decided(&d, decided("d-d", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#y")))
        .unwrap();
    let err = ledger
        .record_authority_decided(&d, decided("d-d", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#y")))
        .unwrap_err();
    assert!(matches!(err, LedgerError::DecisionAlreadyRecorded(_)));
    std::fs::remove_dir_all(&root).ok();
}

/// T3a S1 §3 / RATIFY-3 as amended: `approval.resolved` consistency —
/// `cancelled` carries no reviewer, no DecisionRecord, no user response, and
/// is never a denial.
#[test]
fn approval_resolved_consistency_enforced() {
    let root = tmp_root("resolved");
    let (mut ledger, _) = Ledger::open_new(&root, ids("resolved")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");

    // resolved without requested.
    let a = admitted(&mut ledger, "t", "s", &sc, "a");
    ledger
        .record_authority_decided(&a, decided("d-a", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    let err = ledger
        .record_approval_resolved(&a, resolved_allow("d-a"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::ApprovalNotRequested(_)));

    // cancelled with a DecisionRecord.
    let b = admitted(&mut ledger, "t", "s", &sc, "b");
    ledger
        .record_authority_decided(&b, decided("d-b", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&b, preview("b", "d-b")).unwrap();
    let mut bad = resolved_cancelled(CancelCause::TurnCancelled);
    bad.decision = Some(decision("d-b", DecisionState::Denied, "user_denied", "nope"));
    let err = ledger.record_approval_resolved(&b, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidApprovalResolved(_)));

    // cancelled with a user actor (a reviewer).
    let c = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&c, decided("d-c", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&c, preview("c", "d-c")).unwrap();
    let mut bad = resolved_cancelled(CancelCause::TurnCancelled);
    bad.actor = ActorRef { kind: ActorKind::User, stable_id: "dad".into() };
    let err = ledger.record_approval_resolved(&c, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidApprovalResolved(_)));

    // cancelled without cancel_cause.
    let d = admitted(&mut ledger, "t", "s", &sc, "d");
    ledger
        .record_authority_decided(&d, decided("d-d", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&d, preview("d", "d-d")).unwrap();
    let mut bad = resolved_cancelled(CancelCause::TurnCancelled);
    bad.cancel_cause = None;
    let err = ledger.record_approval_resolved(&d, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidApprovalResolved(_)));

    // allow ⇔ decision.state=approved.
    let e = admitted(&mut ledger, "t", "s", &sc, "e");
    ledger
        .record_authority_decided(&e, decided("d-e", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&e, preview("e", "d-e")).unwrap();
    let mut bad = resolved_allow("d-e");
    bad.decision = Some(decision("d-e", DecisionState::Denied, "user_denied", "no"));
    let err = ledger.record_approval_resolved(&e, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidApprovalResolved(_)));
    std::fs::remove_dir_all(&root).ok();
}

/// RATIFY-3 as amended: cancelling a pending approval writes
/// `approval.resolved{cancelled}` (no reviewer, no DecisionRecord) and the
/// gated call terminates `aborted`, caused_by the resolve (S1 §4a row 5).
#[test]
fn cancelled_resolution_carries_no_reviewer_and_aborts_call() {
    let root = tmp_root("cancel");
    let (mut ledger, _) = Ledger::open_new(&root, ids("cancel")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let acked = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&acked, preview("c", "dec-1")).unwrap();
    let res = ledger
        .record_approval_resolved(&acked, resolved_cancelled(CancelCause::TurnCancelled))
        .unwrap();
    ledger
        .record_pre_witness_result(
            acked,
            aborted_result("c", Some("dec-1"), "cancelled_before_start"),
        )
        .unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let resolved = last_of_kind(&outcome, "approval.resolved");
    assert_eq!(resolved.payload["resolution"], serde_json::json!("cancelled"));
    assert_eq!(resolved.payload["decision"], serde_json::Value::Null, "no DecisionRecord");
    assert_eq!(resolved.payload["cancel_cause"], serde_json::json!("turn_cancelled"));
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("aborted"));
    assert_eq!(result.payload["start_state"], serde_json::json!("not_started"));
    assert_eq!(result.caused_by.as_deref(), Some(res.event_id.as_str()));
    // And it is never a denial: no user_denied anywhere in the segment.
    let raw = std::fs::read_to_string(ledger.path()).unwrap();
    assert!(!raw.contains("user_denied"));
    std::fs::remove_dir_all(&root).ok();
}

/// S1 §4a row 3 / S2 §4: user deny → `denied`, caused_by the
/// `approval.resolved` event; `error.message` is the governing
/// `explanation.text` verbatim.
#[test]
fn user_deny_binds_explanation_verbatim() {
    let root = tmp_root("userdeny");
    let (mut ledger, _) = Ledger::open_new(&root, ids("userdeny")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let acked = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&acked, preview("c", "dec-1")).unwrap();
    let res = ledger
        .record_approval_resolved(&acked, resolved_deny_user("dec-1", "user_denied", "user said no"))
        .unwrap();

    // Wrong message fails closed.
    let acked2 = admitted(&mut ledger, "t", "s", &sc, "c2");
    ledger
        .record_authority_decided(&acked2, decided("dec-2", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&acked2, preview("c2", "dec-2")).unwrap();
    ledger
        .record_approval_resolved(&acked2, resolved_deny_user("dec-2", "user_denied", "user said no"))
        .unwrap();
    let err = ledger
        .record_pre_witness_result(acked2, denied_result("c2", "dec-2", "user_denied", "paraphrased"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::DeniedExplanationMismatch { .. }));

    ledger
        .record_pre_witness_result(acked, denied_result("c", "dec-1", "user_denied", "user said no"))
        .unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("denied"));
    assert_eq!(result.payload["error"]["code"], serde_json::json!("user_denied"));
    assert_eq!(result.caused_by.as_deref(), Some(res.event_id.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// S1 §4a row 4: E3 refusal after approval, before the witness → `denied`
/// with `terminal_decision state=refused`; caused_by the last gate event.
#[test]
fn e3_refusal_pre_witness_denied() {
    let root = tmp_root("e3");
    let (mut ledger, _) = Ledger::open_new(&root, ids("e3")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let acked = admitted(&mut ledger, "t", "s", &sc, "c");
    let (_d, _q, r, _s) = {
        let d = ledger
            .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
            .unwrap();
        let q = ledger.record_approval_requested(&acked, preview("c", "dec-1")).unwrap();
        let r = ledger.record_approval_resolved(&acked, resolved_allow("dec-1")).unwrap();
        (d, q, r, ())
    };
    // Without a refusal terminal or durable deny: rejected.
    let acked2 = admitted(&mut ledger, "t", "s", &sc, "c2");
    ledger
        .record_authority_decided(&acked2, decided("dec-2", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let err = ledger
        .record_pre_witness_result(acked2, denied_result("c2", "dec-2", "integration_invariant_failed", "revoked"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::DeniedWithoutRefusal(_)));

    let mut result = denied_result("c", "dec-1", "integration_invariant_failed", "revoked before witness");
    result.terminal_decision = Some(decision(
        "dec-1",
        DecisionState::Refused,
        "integration_invariant_failed",
        "revoked before witness",
    ));
    ledger.record_pre_witness_result(acked, result).unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("denied"));
    assert_eq!(result.payload["terminal_decision"]["state"], serde_json::json!("refused"));
    assert_eq!(result.caused_by.as_deref(), Some(r.event_id.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-5: `tool.started` requires authorization (allow or approved ask).
#[test]
fn tool_started_requires_authorization() {
    let root = tmp_root("authz");
    let (mut ledger, _) = Ledger::open_new(&root, ids("authz")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");

    // No decision at all.
    let a = admitted(&mut ledger, "t", "s", &sc, "a");
    let err = ledger.record_tool_started(&a, started("a", "d-a")).unwrap_err();
    assert!(matches!(err, LedgerError::NotAuthorized(_)));

    // Rule deny.
    let b = admitted(&mut ledger, "t", "s", &sc, "b");
    ledger
        .record_authority_decided(&b, decided("d-b", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#x")))
        .unwrap();
    let err = ledger.record_tool_started(&b, started("b", "d-b")).unwrap_err();
    assert!(matches!(err, LedgerError::NotAuthorized(_)));

    // Ask + user deny.
    let c = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&c, decided("d-c", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&c, preview("c", "d-c")).unwrap();
    ledger
        .record_approval_resolved(&c, resolved_deny_user("d-c", "user_denied", "no"))
        .unwrap();
    let err = ledger.record_tool_started(&c, started("c", "d-c")).unwrap_err();
    assert!(matches!(err, LedgerError::NotAuthorized(_)));

    // Ask + cancelled.
    let d = admitted(&mut ledger, "t", "s", &sc, "d");
    ledger
        .record_authority_decided(&d, decided("d-d", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
        .unwrap();
    ledger.record_approval_requested(&d, preview("d", "d-d")).unwrap();
    ledger
        .record_approval_resolved(&d, resolved_cancelled(CancelCause::TurnCancelled))
        .unwrap();
    let err = ledger.record_tool_started(&d, started("d", "d-d")).unwrap_err();
    assert!(matches!(err, LedgerError::NotAuthorized(_)));

    // governing_decision_id must be the latest lineage decision.
    let e = admitted(&mut ledger, "t", "s", &sc, "e");
    ledger
        .record_authority_decided(&e, decided("d-e", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let err = ledger.record_tool_started(&e, started("e", "wrong-decision")).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidToolStarted(_)));
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-5 row 7: a witness write/fsync failure yields `aborted` and the
/// tool does not run. Structurally: no `AckedStart` token is minted, so no
/// post-witness status can be recorded; the segment faults (MGR-2) and the
/// best-effort `aborted` result append is itself refused.
#[test]
fn witness_write_failure_aborts_without_running() {
    let root = tmp_root("witfail");
    let (mut ledger, _) = Ledger::open_new(&root, ids("witfail")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let acked = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    ledger.debug_fail_next_append();
    let err = ledger
        .record_tool_started(&acked, started("c", "dec-1"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::Io(_)));
    // The tool must not run: no witness token exists. The segment is faulted;
    // even the best-effort aborted result cannot be appended.
    let err = ledger
        .record_pre_witness_result(acked, aborted_result("c", Some("dec-1"), "witness_write_failed"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::SegmentFaulted));
    // Nothing of the witness reached the segment.
    let outcome = replay_segment(ledger.path()).unwrap();
    assert!(outcome
        .assertions
        .iter()
        .all(|r| r.kind != EventKind::ToolStarted && r.kind != EventKind::ToolResult));
    std::fs::remove_dir_all(&root).ok();
}

/// RATIFY-2: the durable witness is the single boundary — `succeeded` and
/// `indeterminate` require it; `denied`/`aborted` are unreachable after it.
#[test]
fn witness_is_the_status_boundary() {
    let root = tmp_root("boundary");
    let (mut ledger, _) = Ledger::open_new(&root, ids("boundary")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");

    // succeeded pre-witness.
    let a = admitted(&mut ledger, "t", "s", &sc, "a");
    ledger
        .record_authority_decided(&a, decided("d-a", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let err = ledger.record_pre_witness_result(a, ok_result("a", "d-a")).unwrap_err();
    assert!(matches!(err, LedgerError::WitnessRequired { .. }));

    // indeterminate pre-witness.
    let b = admitted(&mut ledger, "t", "s", &sc, "b");
    ledger
        .record_authority_decided(&b, decided("d-b", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let err = ledger
        .record_pre_witness_result(b, indeterminate_result("b", "d-b", IndeterminateOrigin::Live))
        .unwrap_err();
    assert!(matches!(err, LedgerError::WitnessRequired { .. }));

    // denied post-witness.
    let c = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&c, decided("d-c", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&c, started("c", "d-c")).unwrap();
    let err = ledger
        .record_post_witness_result(s, denied_result("c", "d-c", "rule_forbidden", "x"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::InvalidToolResult(_)));

    // aborted post-witness.
    let d = admitted(&mut ledger, "t", "s", &sc, "d");
    ledger
        .record_authority_decided(&d, decided("d-d", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&d, started("d", "d-d")).unwrap();
    let err = ledger
        .record_post_witness_result(s, aborted_result("d", Some("d-d"), "cancelled_after_witness"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::InvalidToolResult(_)));

    // pre-witness path after the witness is durable.
    let e = admitted(&mut ledger, "t", "s", &sc, "e");
    ledger
        .record_authority_decided(&e, decided("d-e", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let _s = ledger.record_tool_started(&e, started("e", "d-e")).unwrap();
    let err = ledger
        .record_pre_witness_result(e, aborted_result("e", Some("d-e"), "cancelled_before_start"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::WitnessAlreadyDurable(_)));
    std::fs::remove_dir_all(&root).ok();
}

/// RATIFY-2: post-witness `failed` requires a typed executor outcome
/// (`error.code`); that is what separates it from `indeterminate`.
#[test]
fn failed_requires_typed_executor_outcome() {
    let root = tmp_root("typedfail");
    let (mut ledger, _) = Ledger::open_new(&root, ids("typedfail")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let a = admitted(&mut ledger, "t", "s", &sc, "a");
    ledger
        .record_authority_decided(&a, decided("d-a", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&a, started("a", "d-a")).unwrap();
    let mut bad = failed_result("a", Some("d-a"), "handler_error", StartState::Started);
    bad.error = None;
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::FailedRequiresTypedError(_)));

    // Rows 8/9: launch failure (not_started) and run failure (started).
    let b = admitted(&mut ledger, "t", "s", &sc, "b");
    ledger
        .record_authority_decided(&b, decided("d-b", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&b, started("b", "d-b")).unwrap();
    ledger
        .record_post_witness_result(s, failed_result("b", Some("d-b"), "sandbox_launch_failed", StartState::NotStarted))
        .unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("failed"));
    assert_eq!(result.payload["start_state"], serde_json::json!("not_started"));
    std::fs::remove_dir_all(&root).ok();
}

/// RATIFY-1/2 as amended: `indeterminate` carries `origin: live|recovery`,
/// `start_state=unknown`, no typed outcome, and is never auto-retried. The
/// live closer may write it (dropped future, S1 §4a row 13).
#[test]
fn indeterminate_rules() {
    let root = tmp_root("indet");
    let (mut ledger, _) = Ledger::open_new(&root, ids("indet")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");

    // Missing origin.
    let a = admitted(&mut ledger, "t", "s", &sc, "a");
    ledger
        .record_authority_decided(&a, decided("d-a", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&a, started("a", "d-a")).unwrap();
    let mut bad = indeterminate_result("a", "d-a", IndeterminateOrigin::Live);
    bad.origin = None;
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::IndeterminateRequiresOrigin(_)));

    // retryable=true: never.
    let b = admitted(&mut ledger, "t", "s", &sc, "b");
    ledger
        .record_authority_decided(&b, decided("d-b", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&b, started("b", "d-b")).unwrap();
    let mut bad = indeterminate_result("b", "d-b", IndeterminateOrigin::Live);
    bad.error.as_mut().unwrap().retryable = true;
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::IndeterminateNotRetryable(_)));

    // start_state must be unknown.
    let c = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&c, decided("d-c", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&c, started("c", "d-c")).unwrap();
    let mut bad = indeterminate_result("c", "d-c", IndeterminateOrigin::Live);
    bad.start_state = StartState::Started;
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::InvalidToolResult(_)));

    // Live origin is permitted (RATIFY-1 as amended): dropped future after
    // the witness (S1 §4a row 13).
    let d = admitted(&mut ledger, "t", "s", &sc, "d");
    ledger
        .record_authority_decided(&d, decided("d-d", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&d, started("d", "d-d")).unwrap();
    ledger
        .record_post_witness_result(s, indeterminate_result("d", "d-d", IndeterminateOrigin::Live))
        .unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("indeterminate"));
    assert_eq!(result.payload["origin"], serde_json::json!("live"));
    assert_eq!(result.payload["start_state"], serde_json::json!("unknown"));
    std::fs::remove_dir_all(&root).ok();
}

/// I-14 + T3a S1 §3: `succeeded` requires the witness, `start_state=started`,
/// empty `effect_ids` (D-2), `terminal_decision state=merged` and
/// `output_digest` = `merged.reason.params.target_id`.
#[test]
fn success_requires_witness_merged_terminal_and_digest_match() {
    let root = tmp_root("succ");
    let (mut ledger, _) = Ledger::open_new(&root, ids("succ")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");

    // start_state != started.
    let a = admitted(&mut ledger, "t", "s", &sc, "a");
    ledger
        .record_authority_decided(&a, decided("d-a", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&a, started("a", "d-a")).unwrap();
    let mut bad = ok_result("a", "d-a");
    bad.start_state = StartState::NotStarted;
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::SuccessRequiresStarted));

    // effect ids fail closed (no effect records in the slice).
    let b = admitted(&mut ledger, "t", "s", &sc, "b");
    ledger
        .record_authority_decided(&b, decided("d-b", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&b, started("b", "d-b")).unwrap();
    let mut bad = ok_result("b", "d-b");
    bad.effect_ids = vec!["eff-1".into()];
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::SuccessWithUnresolvedEffects));

    // missing merged terminal.
    let c = admitted(&mut ledger, "t", "s", &sc, "c");
    ledger
        .record_authority_decided(&c, decided("d-c", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&c, started("c", "d-c")).unwrap();
    let mut bad = ok_result("c", "d-c");
    bad.terminal_decision = None;
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::SuccessRequiresMergedTerminal));

    // output_digest ≠ merged target.
    let d = admitted(&mut ledger, "t", "s", &sc, "d");
    ledger
        .record_authority_decided(&d, decided("d-d", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let s = ledger.record_tool_started(&d, started("d", "d-d")).unwrap();
    let mut bad = ok_result("d", "d-d");
    bad.output_digest = Some("sha256:other".into());
    let err = ledger.record_post_witness_result(s, bad).unwrap_err();
    assert!(matches!(err, LedgerError::SuccessOutputDigestMismatch));
    std::fs::remove_dir_all(&root).ok();
}

/// I-19: no success terminal while a call is open; `turn.end.caused_by` is
/// the last durable event of the turn (MGR-5).
#[test]
fn turn_end_open_calls_rules() {
    let root = tmp_root("i19");
    let (mut ledger, _) = Ledger::open_new(&root, ids("i19")).unwrap();
    let (_t, sc) = prefix(&mut ledger, "t", "s");
    let live = admitted(&mut ledger, "t", "s", &sc, "live");
    let live_event = live.ack().event_id.clone();
    let err = ledger
        .append_turn_end("t", TurnStatus::Succeeded, Some("s"), None)
        .unwrap_err();
    match err {
        LedgerError::OpenCallsOnSuccess(open) => assert_eq!(open, vec!["live".to_string()]),
        other => panic!("expected OpenCallsOnSuccess, got {other:?}"),
    }
    let ack = ledger
        .append_turn_end(
            "t",
            TurnStatus::Aborted,
            Some("s"),
            Some(TurnError { code: "cancelled".into(), message: "dropped".into() }),
        )
        .unwrap();
    let outcome = replay_segment(ledger.path()).unwrap();
    let last = outcome.assertions.last().unwrap();
    assert_eq!(last.ordinal, ack.ordinal);
    assert_eq!(last.payload["open_calls"], serde_json::json!(["live"]));
    assert_eq!(last.caused_by.as_deref(), Some(live_event.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// I-26: unknown kinds surface as unsupported assertions; unsupported schema
/// majors are reported, not accepted as truth.
#[test]
fn unknown_kind_and_bad_major_reported_not_dropped() {
    let root = tmp_root("replay");
    let (mut ledger, _) = Ledger::open_new(&root, ids("replay")).unwrap();
    ledger.append_turn_start("t", turn_start()).unwrap();
    let raw = std::fs::read_to_string(ledger.path()).unwrap();
    drop(ledger);
    let unknown_kind = raw
        .lines()
        .nth(1)
        .unwrap()
        .replace("\"kind\":\"turn.start\"", "\"kind\":\"future.kind\"");
    let bad_major = raw
        .lines()
        .nth(1)
        .unwrap()
        .replace("\"schema_version\":\"1.0\"", "\"schema_version\":\"2.0\"");
    let path = root.join("mixed.audit.jsonl");
    std::fs::write(&path, format!("{raw}{unknown_kind}\n{bad_major}\n")).unwrap();

    let outcome = replay_segment(&path).unwrap();
    assert_eq!(outcome.assertions.len(), 4, "unsupported records stay visible as assertions");
    assert!(outcome.diagnostics.iter().any(|d| matches!(
        d,
        ReplayDiagnostic::UnsupportedKind { kind, .. } if kind == "future.kind"
    )));
    assert!(outcome.diagnostics.iter().any(|d| matches!(
        d,
        ReplayDiagnostic::UnsupportedSchemaMajor { found, .. } if found == "2.0"
    )));
    assert!(outcome.has_integrity_failures());
    std::fs::remove_dir_all(&root).ok();
}

/// Fail-closed, no panics: garbage input yields diagnostics, never a panic.
#[test]
fn garbage_input_never_panics() {
    let outcome = scan_bytes(b"\x00\xff not json at all \n{\"partial\":\n\n");
    assert_eq!(outcome.assertions.len(), 0);
    assert!(outcome
        .diagnostics
        .iter()
        .any(|d| matches!(d, ReplayDiagnostic::CorruptLine { .. })));
    assert_eq!(scan_bytes(b"").assertions.len(), 0);
    // >1 MiB line is handled whole (I-27, LT-4).
    let big = format!("{}\n", big_record_line(2 * 1024 * 1024));
    let outcome = scan_bytes(big.as_bytes());
    assert_eq!(outcome.assertions.len(), 1);
    assert_eq!(outcome.diagnostics, vec![]);
}

/// I-17/I-20 step 1: writable resume replays JSONL, rebuilds the per-call
/// index, continues ordinals, and appends `lineage{resume}`.
#[test]
fn resume_rebuilds_state_and_continues_ordinals() {
    let root = tmp_root("resume");
    let idset = ids("resume");
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let done = admitted(&mut ledger, "t", "s", &sc, "done");
        ledger
            .record_authority_decided(&done, decided("d-1", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
            .unwrap();
        let s = ledger.record_tool_started(&done, started("done", "d-1")).unwrap();
        ledger.record_post_witness_result(s, ok_result("done", "d-1")).unwrap();
        let _open = admitted(&mut ledger, "t", "s", &sc, "open");
    } // drop: simulates interruption after durable appends

    let (mut ledger, resume_ack) = Ledger::open_resume(&root, idset.clone()).unwrap();
    assert_eq!(resume_ack.ordinal, 9, "resume continues after the 8 durable records");
    let open: Vec<&String> = ledger.open_calls().collect();
    assert_eq!(open, vec![&"open".to_string()]);
    let err = ledger.append_turn_end("t", TurnStatus::Succeeded, Some("s"), None).unwrap_err();
    assert!(matches!(err, LedgerError::OpenCallsOnSuccess(_)));
    let err = ledger
        .append_tool_call("t", "s", tool_call("open", Disposition::AdmittedToDispatch, "irrelevant"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::DuplicateCallId(_)));

    let outcome = replay_segment(ledger.path()).unwrap();
    let resume = &outcome.assertions[8];
    assert_eq!(resume.kind, EventKind::Lineage);
    assert_eq!(resume.payload["operation"], serde_json::json!("resume"));
    assert_eq!(
        resume.payload["through_ordinal"],
        serde_json::json!("8"),
        "A-2: through_ordinal is a u64-string on the wire"
    );
    assert!(resume.payload["through_byte_offset"].as_u64().unwrap() > 0);
    std::fs::remove_dir_all(&root).ok();
}

/// RULING-6: writable resume refuses a segment whose tail fails to parse or
/// validate, with typed `ledger_tail_corrupt` (byte offset + last valid
/// ordinal); no auto-truncate, no fuse.
#[test]
fn resume_refuses_torn_tail_typed() {
    let root = tmp_root("tornresume");
    let idset = ids("tornresume");
    let path = segment_path(&root, &idset);
    let expected_offset;
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        ledger.append_turn_start("t", turn_start()).unwrap();
        let mut bytes = std::fs::read(&path).unwrap();
        expected_offset = bytes.len() as u64;
        bytes.extend_from_slice(b"{\"schema_version\":\"1.0\",PARTIAL");
        std::fs::write(&path, bytes).unwrap();
    }
    let err = match Ledger::open_resume(&root, idset) {
        Ok(_) => panic!("expected ledger_tail_corrupt refusal, resume succeeded"),
        Err(e) => e,
    };
    match err {
        LedgerError::LedgerTailCorrupt {
            byte_offset,
            last_ordinal,
        } => {
            assert_eq!(byte_offset, expected_offset);
            assert_eq!(last_ordinal, Some(2));
        }
        other => panic!("expected LedgerTailCorrupt, got {other:?}"),
    }
    std::fs::remove_dir_all(&root).ok();
}

/// Typed fail-closed errors: empty ids, snapshot mismatch, missing step
/// context, and call-id mismatch are all rejected.
#[test]
fn invalid_inputs_are_typed_errors() {
    let root = tmp_root("invalid");
    let (mut ledger, _) = Ledger::open_new(&root, ids("invalid")).unwrap();
    let err = ledger.append_turn_start("", turn_start()).unwrap_err();
    assert!(matches!(err, LedgerError::EmptyId));

    // step.context requires turn.start (MGR-5).
    let err = ledger.append_step_context("t", "s", step_context()).unwrap_err();
    assert!(matches!(err, LedgerError::MissingTurnStart { .. }));
    let (_t, sc) = prefix(&mut ledger, "t", "s");

    // tool.call requires the governing step.context event id (MGR-5).
    let err = ledger
        .append_tool_call("t", "s", tool_call("a", Disposition::AdmittedToDispatch, "wrong"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::AuthoritySnapshotMismatch { .. }));

    // tool.call in a step with no step.context.
    let err = ledger
        .append_tool_call("t", "nope", tool_call("a", Disposition::AdmittedToDispatch, &sc))
        .unwrap_err();
    assert!(matches!(err, LedgerError::MissingStepContext { .. }));

    let acked = admitted(&mut ledger, "t", "s", &sc, "b");
    ledger
        .record_authority_decided(&acked, decided("d-b", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
        .unwrap();
    let err = ledger
        .record_pre_witness_result(acked, aborted_result("SOMEONE-ELSE", Some("d-b"), "cancelled_before_start"))
        .unwrap_err();
    assert!(matches!(err, LedgerError::CallIdMismatch { .. }));
    std::fs::remove_dir_all(&root).ok();
}

// ---------------------------------------------------------------------------
// Crash-point matrix (S1 §4b): kill -9 at each boundary; resume classifies
// from durable bytes only.
// ---------------------------------------------------------------------------

/// CP-1: crash after `tool.call` → `aborted` / `recovered_unwitnessed`,
/// caused_by the `tool.call` event; the interrupted turn then closes with
/// `turn.end` caused_by the last synthetic result (I-20 step 3, MGR-3).
#[test]
fn cp1_crash_after_tool_call_recovers_aborted() {
    let root = tmp_root("cp1");
    let idset = ids("cp1");
    let call_event;
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let acked = admitted(&mut ledger, "t", "s", &sc, "c");
        call_event = acked.ack().event_id.clone();
    } // kill -9

    let (mut ledger, resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 1);
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("aborted"));
    assert_eq!(result.payload["error"]["code"], serde_json::json!("recovered_unwitnessed"));
    assert_eq!(result.payload["start_state"], serde_json::json!("not_started"));
    assert_eq!(result.payload["model_delivery"], serde_json::json!("none"));
    assert_eq!(result.payload["governing_decision_id"], serde_json::Value::Null);
    assert_eq!(result.payload["recovered_by"], serde_json::json!(resume.event_id));
    assert_eq!(result.caused_by.as_deref(), Some(call_event.as_str()));

    let end = ledger
        .append_turn_end("t", TurnStatus::Aborted, Some("s"), None)
        .unwrap();
    assert_eq!(end.event_id.len(), 36);
    let outcome = replay_segment(ledger.path()).unwrap();
    let end_rec = last_of_kind(&outcome, "turn.end");
    assert_eq!(end_rec.caused_by.as_deref(), Some(acks[0].event_id.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// CP-2: crash after `authority.decided{allow}` → `aborted` caused_by the
/// decision.
#[test]
fn cp2_crash_after_decided_allow_recovers_aborted() {
    let root = tmp_root("cp2");
    let idset = ids("cp2");
    let decided_event;
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let acked = admitted(&mut ledger, "t", "s", &sc, "c");
        let d = ledger
            .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Allow, DecisionSource::Rule, Some("r#a")))
            .unwrap();
        decided_event = d.event_id.clone();
    }
    let (mut ledger, _resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 1);
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("aborted"));
    assert_eq!(result.caused_by.as_deref(), Some(decided_event.as_str()));
    assert_eq!(result.payload["governing_decision_id"], serde_json::json!("dec-1"));
    std::fs::remove_dir_all(&root).ok();
}

/// CP-3: crash with a pending approval → `approval.resolved{cancelled,
/// recovery}` (caused_by the request; no DecisionRecord), then `aborted`
/// caused_by that resolve.
#[test]
fn cp3_crash_with_pending_approval_closes_cancelled_then_aborted() {
    let root = tmp_root("cp3");
    let idset = ids("cp3");
    let requested_event;
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let acked = admitted(&mut ledger, "t", "s", &sc, "c");
        ledger
            .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
            .unwrap();
        let q = ledger.record_approval_requested(&acked, preview("c", "dec-1")).unwrap();
        requested_event = q.event_id.clone();
    }
    let (mut ledger, _resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 2, "cancelled resolve + aborted result");
    let outcome = replay_segment(ledger.path()).unwrap();
    let resolved = last_of_kind(&outcome, "approval.resolved");
    assert_eq!(resolved.payload["resolution"], serde_json::json!("cancelled"));
    assert_eq!(resolved.payload["cancel_cause"], serde_json::json!("recovery"));
    assert_eq!(resolved.payload["decision"], serde_json::Value::Null);
    assert_eq!(resolved.payload["actor"]["kind"], serde_json::json!("policy_engine"));
    assert_eq!(resolved.caused_by.as_deref(), Some(requested_event.as_str()));
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("aborted"));
    assert_eq!(result.caused_by.as_deref(), Some(resolved.event_id.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// CP-4: crash after a durable deny → `denied`, caused_by the deny record,
/// error from the governing explanation verbatim.
#[test]
fn cp4_crash_after_deny_recovers_denied() {
    let root = tmp_root("cp4");
    let idset = ids("cp4");
    let decided_event;
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let acked = admitted(&mut ledger, "t", "s", &sc, "c");
        let d = ledger
            .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Deny, DecisionSource::Rule, Some("r#forbid")))
            .unwrap();
        decided_event = d.event_id.clone();
    }
    let (mut ledger, _resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 1);
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("denied"));
    assert_eq!(result.payload["error"]["code"], serde_json::json!("rule_forbidden"));
    assert_eq!(
        result.payload["error"]["message"],
        serde_json::json!("explanation: rule_forbidden")
    );
    assert_eq!(result.payload["model_delivery"], serde_json::json!("deliver"));
    assert_eq!(result.caused_by.as_deref(), Some(decided_event.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// CP-5: crash after `approval.resolved{allow}`, no witness → `aborted`
/// caused_by the resolve.
#[test]
fn cp5_crash_after_resolved_allow_recovers_aborted() {
    let root = tmp_root("cp5");
    let idset = ids("cp5");
    let resolved_event;
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let acked = admitted(&mut ledger, "t", "s", &sc, "c");
        ledger
            .record_authority_decided(&acked, decided("dec-1", DecisionOutcome::Ask, DecisionSource::ModeDefault, None))
            .unwrap();
        ledger.record_approval_requested(&acked, preview("c", "dec-1")).unwrap();
        let r = ledger.record_approval_resolved(&acked, resolved_allow("dec-1")).unwrap();
        resolved_event = r.event_id.clone();
    }
    let (mut ledger, _resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 1);
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("aborted"));
    assert_eq!(result.payload["error"]["code"], serde_json::json!("recovered_unwitnessed"));
    assert_eq!(result.caused_by.as_deref(), Some(resolved_event.as_str()));
    std::fs::remove_dir_all(&root).ok();
}

/// CP-6/CP-7: crash after `tool.started` (kill before OR after invocation —
/// the ledgers are byte-identical and the test asserts the classification,
/// not a distinction) → `indeterminate` / `outcome_unknown`,
/// `origin=recovery`, caused_by the witness, never retried. The witness
/// proves authorization-to-attempt, never invocation (I-05b, RATIFY-4).
#[test]
fn cp6_cp7_crash_after_witness_recovers_indeterminate() {
    let root = tmp_root("cp67");
    let idset = ids("cp67");
    let witness_event;
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let acked = admitted(&mut ledger, "t", "s", &sc, "c");
        let (_d, _q, _r, s) = ask_allow_started(&mut ledger, &acked, "dec-1");
        witness_event = s.witness().event_id.clone();
    } // kill -9: before or after invocation — indistinguishable by design
    let (mut ledger, resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 1);
    let outcome = replay_segment(ledger.path()).unwrap();
    let result = last_of_kind(&outcome, "tool.result");
    assert_eq!(result.payload["status"], serde_json::json!("indeterminate"));
    assert_eq!(result.payload["origin"], serde_json::json!("recovery"));
    assert_eq!(result.payload["error"]["code"], serde_json::json!("outcome_unknown"));
    assert_eq!(result.payload["error"]["retryable"], serde_json::json!(false));
    assert_eq!(result.payload["start_state"], serde_json::json!("unknown"));
    assert_eq!(result.payload["model_delivery"], serde_json::json!("deliver"));
    assert_eq!(result.payload["recovered_by"], serde_json::json!(resume.event_id));
    assert_eq!(result.caused_by.as_deref(), Some(witness_event.as_str()));
    // Any indeterminate ⇒ the interrupted turn closes `incomplete` (MGR-3).
    ledger
        .append_turn_end("t", TurnStatus::Incomplete, Some("s"), None)
        .unwrap();
    std::fs::remove_dir_all(&root).ok();
}

/// CP-8: crash after `tool.result` — the terminal is respected; recovery
/// appends nothing.
#[test]
fn cp8_crash_after_result_is_terminal() {
    let root = tmp_root("cp8");
    let idset = ids("cp8");
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let (_t, sc) = prefix(&mut ledger, "t", "s");
        let acked = admitted(&mut ledger, "t", "s", &sc, "c");
        let (_d, _q, _r, s) = ask_allow_started(&mut ledger, &acked, "dec-1");
        ledger.record_post_witness_result(s, ok_result("c", "dec-1")).unwrap();
    }
    let (mut ledger, resume) = Ledger::open_resume(&root, idset.clone()).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert!(acks.is_empty(), "closed calls synthesize nothing");
    let outcome = replay_segment(ledger.path()).unwrap();
    let last = outcome.assertions.last().unwrap();
    assert_eq!(last.event_id, resume.event_id, "nothing appended after lineage-resume");
    std::fs::remove_dir_all(&root).ok();
}

/// Build a valid record line whose payload string is at least `min_len` bytes.
fn big_record_line(min_len: usize) -> String {
    let pad = "x".repeat(min_len);
    let record = Record {
        schema_version: SCHEMA_VERSION.to_owned(),
        event_id: "evt-big".into(),
        timestamp: "2026-10-16T00:00:00.000Z".into(),
        ordinal: 1,
        session_id: "s".into(),
        thread_id: "t".into(),
        lineage_id: "l".into(),
        segment_id: "seg".into(),
        turn_id: None,
        step_id: None,
        actor: Actor::Engine,
        kind: EventKind::Lineage,
        correlation_id: None,
        caused_by: None,
        payload: serde_json::json!({"operation": "new", "pad": pad}),
    };
    let line = record.to_line().unwrap();
    assert!(line.len() >= min_len);
    line.trim_end().to_owned()
}

/// LT-4 (S1 §5a) + S5 §6: a record of exactly 1 MiB+1 and one of exactly 4 MiB
/// round-trip BYTE-EXACT through the segment file, and a frame carrying such a
/// record is neither truncated nor capped at `MAX_FRAME_BYTES` ("frame limits
/// must pass records of 1 MiB or more").
#[test]
fn lt4_records_of_exactly_1mib_plus_1_and_4mib_round_trip_byte_exact() {
    for target in [1024usize * 1024 + 1, 4 * 1024 * 1024] {
        let root = tmp_root(&format!("lt4-{target}"));
        let (ledger, _) = Ledger::open_new(&root, ids("lt4")).unwrap();
        let path = ledger.path().to_path_buf();
        let lineage = std::fs::read_to_string(&path).unwrap();
        assert_eq!(lineage.lines().count(), 1, "the structural new-lineage record");
        let lineage_len = lineage.trim_end().len();
        drop(ledger);

        // Size the padding so the appended line is EXACTLY `target` bytes.
        let make = |pad: usize| Record {
            schema_version: SCHEMA_VERSION.to_owned(),
            event_id: "b1c0ffee-0000-4000-8000-000000000001".into(),
            timestamp: "2026-10-20T00:00:00.000Z".into(),
            ordinal: 2,
            session_id: "sess-lt4".into(),
            thread_id: "thr-lt4".into(),
            lineage_id: "lin-lt4".into(),
            segment_id: "seg-lt4".into(),
            turn_id: Some("turn-1".into()),
            step_id: None,
            actor: Actor::Engine,
            kind: EventKind::TurnStart,
            correlation_id: None,
            caused_by: None,
            payload: serde_json::json!({
                "input_digest": "sha256:lt4",
                "parent_turn_id": null,
                "pad": "x".repeat(pad),
            }),
        };
        let base = make(0).to_line().unwrap().trim_end().len();
        assert!(base < target, "envelope is smaller than the target size");
        let pad = target - base;
        let line = make(pad).to_line().unwrap();
        assert_eq!(
            line.trim_end().len(),
            target,
            "the record line is exactly {target} bytes"
        );

        // Append it to the real segment (as the ledger would) and read it back
        // through the read-only scanner.
        {
            use std::io::Write as _;
            let mut f = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
            f.write_all(line.as_bytes()).unwrap();
            f.sync_data().unwrap();
        }
        let outcome = replay_segment(&path).unwrap();
        assert_eq!(outcome.assertions.len(), 2, "no truncation, no dropped record");
        assert!(
            outcome.diagnostics.is_empty(),
            "a {target}-byte record is not a diagnostic: {:?}",
            outcome.diagnostics
        );
        let back = &outcome.assertions[1];
        assert_eq!(back.ordinal, 2);
        assert_eq!(
            back.payload["pad"].as_str().unwrap().len(),
            pad,
            "the payload survives whole"
        );
        // Byte-exact: re-serializing the parsed record reproduces the bytes
        // that were written, so nothing is normalized or reordered on the way
        // through the file.
        assert_eq!(back.to_line().unwrap(), line, "record round-trips byte-exact");
        assert!(
            std::fs::metadata(&path).unwrap().len() > target as u64,
            "the segment physically holds the whole record"
        );
        assert!(lineage_len < 1024, "the lineage record is small");

        // The frame envelope carries the same record whole: no cap, no
        // truncation (S5 §6 requires frame limits to pass >= 1 MiB records).
        let frame = bob_engine::protocol::Frame::from_record(
            1,
            back,
            "turn.started",
            back.payload.clone(),
        );
        let frame_line = frame.to_line().unwrap();
        // The frame envelope is narrower than the record envelope (nine fields
        // vs fifteen), so the frame line is ~137 bytes shorter than the record
        // line; the claim under test is that nothing is capped or truncated at
        // MAX_FRAME_BYTES, which the payload identity above plus this bound on
        // the line length both show.
        assert!(
            frame_line.trim_end().len() + 1024 > target,
            "the frame line carries the whole payload (no MAX_FRAME_BYTES cap), got {} for a {target}-byte record",
            frame_line.len()
        );
        let parsed: serde_json::Value = serde_json::from_str(frame_line.trim_end()).unwrap();
        assert_eq!(
            parsed["payload"]["pad"].as_str().unwrap().len(),
            pad,
            "no truncation in the frame payload"
        );
        std::fs::remove_dir_all(&root).ok();
    }
}
