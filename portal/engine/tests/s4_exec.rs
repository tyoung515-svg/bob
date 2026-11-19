//! S4 executor tests (E4): the slice policy constants, the FX-READ-01
//! read-only executor (argv spawn, typed MGR-13 ChildOutcome), the ordering
//! contract authority → approval → E3 → preflight → durable witness → spawn →
//! result driven through the E2 token APIs, the honest `platform_deferred`
//! sandbox posture on the Windows build host (D-18), and the S4 §2b status
//! table — one test per constructible row:
//!
//! | §2b row | path | test |
//! |---|---|---|
//! | 1 | rule deny | `rule_deny_terminal_before_executor` |
//! | 2 | user deny | `user_deny_terminal` |
//! | 3 | revocation at the response (E2) | `revocation_at_response_denies` |
//! | 4 | cancel pending / after allow pre-witness | `cancel_while_pending_aborts`, `cancel_after_allow_before_witness` |
//! | 5 | witness write/fsync failure | `witness_write_failure_ledger_fatal_no_spawn` |
//! | 6 | preflight failure ×3 codes | `preflight_failure_aborts_no_witness_no_spawn` |
//! | 7 | spawn error after witness | `spawn_error_after_witness` |
//! | 8 | child exit ≠ 0 | `child_error_exit_failed` |
//! | 9 | cancel after witness, child reaped | `cancel_after_witness_reaps_child` |
//! | 10 | supervision loss (live + recovery) | `supervision_lost_live_indeterminate`, `recovery_after_witness_indeterminate` |
//! | 10 (S1 §4a) | E4 refusal after completed_ok | `e4_digest_inconsistency_refused` (FX-23) |
//! | 11 | exit 0 → merged | `happy_path_ask_allow_succeeds`, `rule_allow_skips_approval_succeeds` |

use std::path::PathBuf;

use serde_json::{json, Value};

use bob_engine::authority::*;
use bob_engine::digest::{jcs_sha256, sha256_digest};
use bob_engine::evaluator::{reason, Evaluator, PolicyConfig, ToolEntry, ToolManifest};
use bob_engine::exec::{
    self, CancelToken, CallTerminal, ChildSpawned, ConstructionStage, ContentValue, Executor,
    GatePass, Platform, PreflightFailure, RejectionCode, SliceExec, INLINE_CAP_BYTES,
};
use bob_engine::gate::{ApprovalOutcome, ApprovalResponse, Authorization, Gate, PendingDecision, PreWitness};
use bob_engine::ledger::{AckedCall, AckedStart, Ledger, LedgerError, SessionIds};
use bob_engine::payload::*;
use bob_engine::replay::{replay_segment, ReplayOutcome};
use bob_engine::{policy, FX_READ_COMMAND, SANDBOXABLE_PREFERENCE, SANDBOX_POLICY};

const EVAL_TIME: &str = "2026-10-17T00:00:00.000Z";
const RULES_SRC: &str = "rules/default.rules";
const USER: &str = "user-1";

fn clock() -> String {
    EVAL_TIME.to_string()
}

fn tmp_root(tag: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e4-{tag}-{}-{nanos}-{}",
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

/// The FX-READ-01 fixture policy (S4 §1): mode default + the three-rule
/// stack; the production command (`fx-read`) matches `ask`, so approval runs
/// end to end.
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
    .unwrap()
}

/// Variant with an `allow` rule for the fixture command (S2 §1: the
/// `allow`-rule path skips the approval events).
fn fx_config_allow() -> PolicyConfig {
    PolicyConfig::from_json(&json!({
        "mode": "default",
        "revision": "rules/default.rules#r2",
        "rules": [
            {"effect": "deny", "matcher": {"tool": "exec_command", "argv_prefix": ["rm"]}, "source": RULES_SRC},
            {"effect": "allow","matcher": {"tool": "exec_command", "argv_prefix": [FX_READ_COMMAND]}, "source": RULES_SRC}
        ]
    }))
    .unwrap()
}

fn fx_manifest() -> ToolManifest {
    let mut m = ToolManifest::default();
    m.tools.insert(
        "exec_command".to_string(),
        ToolEntry {
            effect_class: EffectClass::Read,
            read_roots: vec!["/bin".into(), "/lib".into(), "/lib64".into(), "/usr".into()],
            revoked: false,
        },
    );
    m
}

fn fx_gate(config: PolicyConfig, cwd: &std::path::Path) -> Gate {
    Gate {
        evaluator: Evaluator {
            config,
            manifest: {
                let mut manifest = fx_manifest();
                manifest.tools.get_mut("exec_command").unwrap().read_roots.insert(0, cwd.to_string_lossy().into_owned());
                manifest
            },
            sandbox_available: true,
            revocation_epoch: 1,
            fail_verdict: false,
            fail_integration: None,
        },
    }
}

/// The executor with the provisioned FX-READ-01 fixture binary (EXEC-32).
fn fx_executor() -> Executor {
    Executor::new("exec-1", PathBuf::from(env!("CARGO_BIN_EXE_fx-read")))
}

fn auth_snapshot() -> Value {
    json!({"approval_policy": {"mode": "default"}, "sandbox_policy": {"policy": "read_only"}})
}

struct Fx {
    ledger: Ledger,
    step_ev: String,
    cwd: PathBuf,
}

fn mk_fx(tag: &str) -> Fx {
    let root = tmp_root(tag);
    let (mut ledger, _) = Ledger::open_new(&root, ids(tag)).unwrap();
    ledger
        .append_turn_start(
            "turn-1",
            TurnStart {
                input_digest: "sha256:fixture".into(),
                parent_turn_id: None,
            },
        )
        .unwrap();
    let step = ledger
        .append_step_context(
            "turn-1",
            "step-1",
            StepContext {
                model: "slice-model".into(),
                tool_router_revision: "r1".into(),
                model_visible_tools: vec!["exec_command".into()],
                mcp_bindings: vec![],
                approval_policy: json!({"mode": "default"}),
                sandbox_policy: serde_json::to_value(SANDBOX_POLICY).unwrap(),
                settings_revision: "s1".into(),
            },
        )
        .unwrap();
    let cwd = root.join("slice");
    std::fs::create_dir_all(&cwd).unwrap();
    std::fs::write(cwd.join("hello.txt"), "bob-slice-fixture\n").unwrap();
    Fx {
        ledger,
        step_ev: step.event_id,
        cwd,
    }
}

fn tool_call(call_id: &str, snapshot_event_id: &str, args: Value) -> ToolCall {
    ToolCall {
        call_id: call_id.into(),
        model_item_id: "item-1".into(),
        call_index: 0,
        tool_name: "exec_command".into(),
        handler_kind: "builtin".into(),
        arguments: args,
        execution_class: ExecutionClass::Serial,
        exposure: "model".into(),
        authority_snapshot_event_id: snapshot_event_id.into(),
        dispatch: Dispatch {
            disposition: Disposition::AdmittedToDispatch,
            gate: bob_engine::payload::Gate::None,
            executor_id: None,
            reason_code: None,
        },
    }
}

fn fx_args(target: &str) -> Value {
    json!({"argv": [FX_READ_COMMAND, target]})
}

fn call(fx: &mut Fx, call_id: &str, args: Value) -> AckedCall {
    let step_ev = fx.step_ev.clone();
    fx.ledger
        .append_tool_call("turn-1", "step-1", tool_call(call_id, &step_ev, args))
        .unwrap()
}

fn allow_once(preview: &ApprovalPreview) -> ApprovalResponse {
    ApprovalResponse::AllowOnce {
        decision_id: preview.decision_id.clone(),
        args_digest: preview.args_digest.clone(),
    }
}

fn last_of_kind<'a>(outcome: &'a ReplayOutcome, kind: &str) -> &'a bob_engine::envelope::Record {
    outcome
        .assertions
        .iter()
        .rev()
        .find(|r| r.kind.as_str() == kind)
        .unwrap()
}

fn kinds(outcome: &ReplayOutcome) -> Vec<&str> {
    outcome.assertions.iter().map(|r| r.kind.as_str()).collect()
}

fn result_payload(outcome: &ReplayOutcome) -> ToolResult {
    serde_json::from_value(last_of_kind(outcome, "tool.result").payload.clone()).unwrap()
}

fn evidence_json(outcome: &ReplayOutcome) -> Value {
    last_of_kind(outcome, "tool.result").payload["executor_evidence"].clone()
}

/// Manually drive the ask path up to a durable witness; returns the pieces
/// phase 2 needs. Used by the fault-injection rows that must interpose
/// between steps (witness failure, E4 inconsistency, supervision loss).
fn reach_witness(
    fx: &mut Fx,
    gate: &Gate,
    exec: &Executor,
    call_id: &str,
    args: Value,
) -> (AckedStart, PendingDecision, Vec<bob_engine::evaluator::RuleMatch>, exec::PreflightRecord) {
    let acked = call(fx, call_id, args.clone());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let (acked, pending) = match gate
        .authorize_call(&mut fx.ledger, acked, &tool_call(call_id, &fx.step_ev, args.clone()), &snap, EVAL_TIME, &cwd)
        .unwrap()
    {
        Authorization::Ask {
            acked, pending, preview, ..
        } => {
            let response = allow_once(&preview);
            match gate
                .resolve_approval(&mut fx.ledger, acked, pending, response, USER, EVAL_TIME, &snap)
                .unwrap()
            {
                ApprovalOutcome::Allowed { acked, pending, .. } => (acked, pending),
                _ => panic!("allow_once must approve"),
            }
        }
        _ => panic!("fx-read must route to ask"),
    };
    let acked = match gate
        .pre_witness_check(&mut fx.ledger, acked, pending.clone(), EVAL_TIME, &snap)
        .unwrap()
    {
        PreWitness::Cleared { acked } => acked,
        _ => panic!("clean E3 must clear"),
    };
    let argv = vec![FX_READ_COMMAND.to_string(), "./hello.txt".to_string()];
    let preflight = exec.preflight(&argv, &cwd).unwrap();
    let matches = gate.evaluator.rule_matches("exec_command", &args);
    let started = fx
        .ledger
        .record_tool_started(
            &acked,
            ToolStarted {
                call_id: call_id.into(),
                governing_decision_id: pending.decision_id.clone(),
                executor_id: exec.id.clone(),
                args_digest: pending.digests.args_digest.clone(),
            },
        )
        .unwrap();
    (started, pending, matches, preflight)
}

// ---- Policy constants (S4 §1) ----

#[test]
fn slice_policy_constants_are_fixed() {
    assert_eq!(
        serde_json::to_value(SANDBOX_POLICY).unwrap(),
        json!({"policy": "read_only", "network_access": false})
    );
    assert_eq!(serde_json::to_value(SANDBOXABLE_PREFERENCE).unwrap(), json!("auto"));
    assert_eq!(policy::BACKEND_ID, "linux_bubblewrap");
    assert_eq!(policy::BACKEND_PLATFORM_DEFERRED, "platform_deferred");
    assert_eq!(policy::APPROVAL_ACTION, "exec_command");
    assert_eq!(policy::TOOL_NAME, "exec_command");
    assert_eq!(policy::EFFECT_CLASS, EffectClass::Read);
    assert!(!policy::NETWORK_ACCESS);
    let roots = policy::slice_read_roots(std::path::Path::new("/fx"));
    assert_eq!(roots.len(), 5);
    assert!(roots[0].ends_with("slice"));
}

#[test]
fn rejection_code_closed_set_and_reachability() {
    let spellings: Vec<&str> = RejectionCode::CLOSED_SET.iter().map(|c| c.as_str()).collect();
    assert_eq!(
        spellings,
        [
            "exec_forbidden",
            "approval_rejected",
            "isolation_unavailable",
            "sandbox_launch_failed",
            "helper_verification_failed",
            "policy_unknown",
            "preflight_unavailable",
            "platform_deferred"
        ]
    );
    let reachable: Vec<&str> = RejectionCode::CLOSED_SET
        .iter()
        .filter(|c| c.is_call_level_reachable())
        .map(|c| c.as_str())
        .collect();
    assert_eq!(
        reachable,
        [
            "exec_forbidden",
            "approval_rejected",
            "isolation_unavailable",
            "sandbox_launch_failed",
            "helper_verification_failed"
        ]
    );
    // The authority-class mapping (EXEC-35): ledger error.code carries the
    // S2 reason; the RejectionCode is the executor-side vocabulary.
    assert_eq!(RejectionCode::ExecForbidden.s2_reason(), Some(reason::RULE_FORBIDDEN));
    assert_eq!(RejectionCode::ApprovalRejected.s2_reason(), Some(reason::USER_DENIED));
    assert_eq!(RejectionCode::PlatformDeferred.s2_reason(), None);
}

#[test]
fn content_value_cap_and_child_spawned_encoding() {
    let small = ContentValue::from_bytes(b"hello");
    assert_eq!(small, ContentValue::Inline { text: "hello".into() });
    let big = vec![b'x'; INLINE_CAP_BYTES + 1];
    let stored = ContentValue::from_bytes(&big);
    match stored {
        ContentValue::Stored { digest, byte_len } => {
            assert_eq!(digest, sha256_digest(&big));
            assert_eq!(byte_len, (INLINE_CAP_BYTES + 1) as u64);
        }
        _ => panic!("oversized content must be a Stored digest ref"),
    }
    assert_eq!(serde_json::to_value(ChildSpawned::True).unwrap(), json!(true));
    assert_eq!(serde_json::to_value(ChildSpawned::False).unwrap(), json!(false));
    assert_eq!(serde_json::to_value(ChildSpawned::Unknown).unwrap(), json!("unknown"));
    assert_eq!(
        serde_json::from_value::<ChildSpawned>(json!("unknown")).unwrap(),
        ChildSpawned::Unknown
    );
}

#[test]
fn preflight_fails_closed_for_unprovisioned_command() {
    let exec = fx_executor();
    let cwd = std::env::temp_dir();
    let err = exec
        .preflight(&["cat".to_string(), "./x".to_string()], &cwd)
        .unwrap_err();
    assert_eq!(err.stage, ConstructionStage::ArgvBuild);
    assert_eq!(err.failure_class, "unprovisioned_command");
    assert_eq!(err.code, exec::code::ISOLATION_UNAVAILABLE);
    let err = exec.preflight(&[], &cwd).unwrap_err();
    assert_eq!(err.failure_class, "empty_argv");
}

// ---- §2b row 11: happy paths ----

#[test]
fn happy_path_ask_allow_succeeds() {
    let mut fx = mk_fx("happy");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &fx.cwd.clone(),
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let args_digest = jcs_sha256(&fx_args("./hello.txt")).unwrap();
    let cancel = CancelToken::new();
    let responder = |preview: &ApprovalPreview, _p: &PendingDecision| allow_once(preview);
    let term = dx
        .run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &clock,
            &responder,
            &cancel,
        )
        .unwrap();
    let CallTerminal::Succeeded { output_digest, .. } = term else {
        panic!("expected success, got {term:?}")
    };
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 1);

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(outcome.diagnostics, vec![], "clean replay");
    // The S4 §3 order, durable end to end.
    assert_eq!(
        kinds(&outcome),
        [
            "lineage",
            "turn.start",
            "step.context",
            "tool.call",
            "authority.decided",
            "approval.requested",
            "approval.resolved",
            "tool.started",
            "tool.result"
        ]
    );
    // The witness binds the authorized args and is caused_by the resolution.
    let witness = last_of_kind(&outcome, "tool.started");
    assert_eq!(witness.payload["executor_id"], json!("exec-1"));
    assert_eq!(witness.payload["args_digest"], json!(args_digest));
    assert_eq!(
        witness.caused_by.as_deref(),
        Some(last_of_kind(&outcome, "approval.resolved").event_id.as_str())
    );
    assert_eq!(
        last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
        Some(witness.event_id.as_str())
    );

    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Succeeded);
    assert_eq!(result.start_state, StartState::Started);
    assert_eq!(
        result.output.unwrap(),
        json!({"kind": "inline", "text": "bob-slice-fixture\n"})
    );
    let expect_digest = sha256_digest(b"bob-slice-fixture\n");
    assert_eq!(result.output_digest.as_deref(), Some(expect_digest.as_str()));
    assert_eq!(output_digest, expect_digest);
    // The merged terminal: target_id = output_digest (T3a S2 §4, EXEC-33).
    let merged = result.terminal_decision.unwrap();
    assert_eq!(merged.state, DecisionState::Merged);
    assert_eq!(merged.reason.code, exec::code::EFFECT_INCORPORATED);
    assert_eq!(merged.reason.params["target_kind"], json!("tool_result"));
    assert_eq!(merged.reason.params["target_id"], json!(expect_digest));

    // MGR-13 evidence: honest platform posture, typed child outcome.
    let ev = evidence_json(&outcome);
    assert_eq!(ev["backend"], json!(Platform::current().backend_label()));
    assert_eq!(ev["child_spawned"], json!(true));
    assert_eq!(ev["child"]["exit_code"], json!(0));
    assert_eq!(ev["child"]["signal"], Value::Null);
    assert_eq!(
        ev["child"]["stdout"],
        json!({"kind": "inline", "text": "bob-slice-fixture\n"})
    );
    assert_eq!(ev["policy"]["network"], json!(false));
    let mut expected_roots = vec!["/bin".to_string(), "/lib".into(), "/lib64".into(), "/usr".into()];
    if Platform::current() == Platform::Linux { expected_roots.insert(0, fx.cwd.to_string_lossy().into_owned()); }
    assert_eq!(ev["policy"]["read_roots"], json!(expected_roots));
    assert_eq!(ev["protected_path_violations"], json!([]));
    assert_eq!(ev["matches"][0]["effect"], json!("ask"));
    assert_eq!(ev["matches"][0]["rule_ref"], json!(format!("{RULES_SRC}#rule-1")));
    if Platform::current() == Platform::Linux {
        assert_eq!(ev["helper"]["verified"], true);
    } else {
        assert!(ev.get("helper").is_none(), "no helper claim under the deferred backend");
    }
    assert!(ev["preflight"].get("construction_stage").is_none());
}

#[test]
fn rule_allow_skips_approval_succeeds() {
    let mut fx = mk_fx("allow");
    let gate = fx_gate(fx_config_allow(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &fx.cwd.clone(),
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let cancel = CancelToken::new();
    let responder = |_preview: &ApprovalPreview, _p: &PendingDecision| {
        panic!("no approval may be requested on the rule-allow path")
    };
    let term = dx
        .run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &clock,
            &responder,
            &cancel,
        )
        .unwrap();
    assert!(matches!(term, CallTerminal::Succeeded { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(
        kinds(&outcome),
        [
            "lineage",
            "turn.start",
            "step.context",
            "tool.call",
            "authority.decided",
            "tool.started",
            "tool.result"
        ]
    );
    // The witness is caused_by authority.decided on the rule-allow path.
    assert_eq!(
        last_of_kind(&outcome, "tool.started").caused_by.as_deref(),
        Some(last_of_kind(&outcome, "authority.decided").event_id.as_str())
    );
    let decided: AuthorityDecided =
        serde_json::from_value(last_of_kind(&outcome, "authority.decided").payload.clone()).unwrap();
    assert_eq!(decided.outcome, DecisionOutcome::Allow);
    assert_eq!(decided.source, DecisionSource::Rule);
}

// ---- §2b rows 1-3: authority-class terminals (no executor, no evidence) ----

#[test]
fn rule_deny_terminal_before_executor() {
    let mut fx = mk_fx("deny");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &fx.cwd.clone(),
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", json!({"argv": ["rm", "-rf", "./hello.txt"]}));
    let cancel = CancelToken::new();
    let responder = |_p: &ApprovalPreview, _p2: &PendingDecision| panic!("deny never asks");
    let term = dx
        .run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, json!({"argv": ["rm", "-rf", "./hello.txt"]})),
            &clock,
            &responder,
            &cancel,
        )
        .unwrap();
    assert!(matches!(term, CallTerminal::Denied { .. }));
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.started"));
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Denied);
    assert_eq!(result.start_state, StartState::NotStarted);
    assert_eq!(result.error.as_ref().unwrap().code, reason::RULE_FORBIDDEN);
    assert!(result.executor_evidence.is_none(), "authority-class terminal: evidence absent");
    assert_eq!(
        last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
        Some(last_of_kind(&outcome, "authority.decided").event_id.as_str())
    );
}

#[test]
fn user_deny_terminal() {
    let mut fx = mk_fx("userdeny");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &fx.cwd.clone(),
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let cancel = CancelToken::new();
    let responder = |preview: &ApprovalPreview, _p: &PendingDecision| ApprovalResponse::Deny {
        decision_id: preview.decision_id.clone(),
        args_digest: preview.args_digest.clone(),
        reason: Some("not today".into()),
    };
    let term = dx
        .run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &clock,
            &responder,
            &cancel,
        )
        .unwrap();
    assert!(matches!(term, CallTerminal::Denied { .. }));
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.started"));
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Denied);
    // RULING-7: the free-text reason becomes the denial reason, verbatim.
    assert_eq!(result.error.as_ref().unwrap().message, "not today");
    assert_eq!(result.error.as_ref().unwrap().code, reason::USER_DENIED);
    assert!(result.executor_evidence.is_none());
}

#[test]
fn revocation_at_response_denies() {
    // §2b row 3 at E2: the epoch moves while the request is pending; the
    // response-time recompute denies (capability_revoked), engine actor.
    let mut fx = mk_fx("revoke-e2");
    let mut gate = fx_gate(fx_config(), &fx.cwd);
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let (acked, pending, preview) = match gate
        .authorize_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &snap,
            EVAL_TIME,
            &cwd,
        )
        .unwrap()
    {
        Authorization::Ask { acked, pending, preview, .. } => (acked, pending, preview),
        _ => panic!("fx-read must route to ask"),
    };
    gate.evaluator.revocation_epoch = 2; // revocation lands while pending
    let outcome = gate
        .resolve_approval(&mut fx.ledger, acked, pending, allow_once(&preview), USER, EVAL_TIME, &snap)
        .unwrap();
    assert!(matches!(outcome, ApprovalOutcome::Denied { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.started"));
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Denied);
    assert_eq!(result.error.as_ref().unwrap().code, reason::CAPABILITY_REVOKED);
}

// ---- §2b row 4: cancel before the witness ----

#[test]
fn cancel_while_pending_aborts() {
    let mut fx = mk_fx("cancel-pending");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &fx.cwd.clone(),
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let cancel = CancelToken::new();
    let token = cancel.clone();
    // The harness fires the engine-side cancel token while the request is
    // pending (the responder returns after the cancel).
    let responder = move |preview: &ApprovalPreview, _p: &PendingDecision| {
        token.cancel();
        allow_once(preview)
    };
    let term = dx
        .run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &clock,
            &responder,
            &cancel,
        )
        .unwrap();
    let CallTerminal::Aborted { code, .. } = term else {
        panic!("expected aborted, got {term:?}")
    };
    assert_eq!(code, exec::code::CANCELLED);
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    // RATIFY-3 as amended: the pending approval closes with
    // approval.resolved{cancelled}, engine actor, no DecisionRecord, never a
    // denial (AUD-6).
    let resolved: ApprovalResolved =
        serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.resolution, Resolution::Cancelled);
    assert!(resolved.decision.is_none());
    assert_eq!(resolved.cancel_cause, Some(CancelCause::TurnCancelled));
    assert_eq!(resolved.actor.kind, ActorKind::PolicyEngine);
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Aborted);
    assert_eq!(result.start_state, StartState::NotStarted);
    assert_eq!(result.error.as_ref().unwrap().code, exec::code::CANCELLED);
    assert!(result.error.as_ref().unwrap().retryable, "retryable only for cancelled (S5 §5)");
    assert_eq!(result.model_delivery, ModelDelivery::None);
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.started"));
    assert_eq!(
        last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
        Some(last_of_kind(&outcome, "approval.resolved").event_id.as_str())
    );
}

#[test]
fn cancel_after_allow_before_witness() {
    // S1 §4a row 6 / FX-21: the approval already resolved allow; the cancel
    // lands before the witness. No approval.resolved{cancelled} is written
    // (RATIFY-3 closes only PENDING approvals).
    let mut fx = mk_fx("cancel-allowed");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &cwd,
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let (acked, pending) = match gate
        .authorize_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &snap,
            EVAL_TIME,
            &cwd,
        )
        .unwrap()
    {
        Authorization::Ask { acked, pending, preview, .. } => {
            match gate
                .resolve_approval(&mut fx.ledger, acked, pending, allow_once(&preview), USER, EVAL_TIME, &snap)
                .unwrap()
            {
                ApprovalOutcome::Allowed { acked, pending, .. } => (acked, pending),
                _ => panic!("allow_once must approve"),
            }
        }
        _ => panic!("fx-read must route to ask"),
    };
    let ack = dx
        .abort_pre_witness(&mut fx.ledger, acked, &pending, exec::code::CANCELLED, None)
        .unwrap();
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(kinds(&outcome).iter().filter(|k| **k == "approval.resolved").count(), 1);
    let resolved: ApprovalResolved =
        serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.resolution, Resolution::Allow);
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Aborted);
    assert_eq!(result.error.as_ref().unwrap().code, exec::code::CANCELLED);
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.started"));
    assert_eq!(
        last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
        Some(last_of_kind(&outcome, "approval.resolved").event_id.as_str())
    );
    assert_eq!(last_of_kind(&outcome, "tool.result").event_id, ack.event_id);
}

// ---- §2b row 5: witness write/fsync failure ----

#[test]
fn witness_write_failure_ledger_fatal_no_spawn() {
    let mut fx = mk_fx("witfail");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &cwd,
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let (acked, pending) = match gate
        .authorize_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &snap,
            EVAL_TIME,
            &cwd,
        )
        .unwrap()
    {
        Authorization::Ask { acked, pending, preview, .. } => {
            match gate
                .resolve_approval(&mut fx.ledger, acked, pending, allow_once(&preview), USER, EVAL_TIME, &snap)
                .unwrap()
            {
                ApprovalOutcome::Allowed { acked, pending, .. } => (acked, pending),
                _ => panic!("allow_once must approve"),
            }
        }
        _ => panic!("fx-read must route to ask"),
    };
    let acked = match gate
        .pre_witness_check(&mut fx.ledger, acked, pending.clone(), EVAL_TIME, &snap)
        .unwrap()
    {
        PreWitness::Cleared { acked } => acked,
        _ => panic!("clean E3 must clear"),
    };
    let preflight = exec
        .preflight(&[FX_READ_COMMAND.to_string(), "./hello.txt".to_string()], &cwd)
        .unwrap();
    let matches = gate.evaluator.rule_matches("exec_command", &fx_args("./hello.txt"));
    // The witness append fails (fsync/write failure); the segment faults.
    fx.ledger.debug_fail_next_append();
    let pass = dx.witness(&mut fx.ledger, acked, &pending, matches, preflight).unwrap();
    let GatePass::Terminal(CallTerminal::WitnessWriteFailed { witness, best_effort }) = pass else {
        panic!("expected witness-write failure, got {pass:?}")
    };
    assert!(matches!(witness, LedgerError::Io(_)));
    // MGR-2: the faulted segment refuses the best-effort aborted record too
    // → ledger-fatal (S4 §2b row 5).
    assert!(matches!(best_effort, Some(LedgerError::SegmentFaulted)));
    // S4 §6.1: spawn count 0 — the tool never ran (RULING-5).
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.started"));
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.result"));
}

// ---- §2b row 6: preflight failure after approval, before the witness ----

#[test]
fn preflight_failure_aborts_no_witness_no_spawn() {
    for (stage, class, expected) in [
        (ConstructionStage::Spawn, "backend_probe", exec::code::ISOLATION_UNAVAILABLE),
        (ConstructionStage::Spawn, "helper_signature", exec::code::HELPER_VERIFICATION_FAILED),
        (ConstructionStage::Spawn, "launch_prepare", exec::code::SANDBOX_LAUNCH_FAILED),
    ] {
        let mut fx = mk_fx(&format!("preflight-{class}"));
        let gate = fx_gate(fx_config(), &fx.cwd);
        let mut exec = fx_executor();
        exec.fail_preflight = Some(PreflightFailure {
            stage,
            failure_class: class.into(),
            code: expected,
        });
        let snap = auth_snapshot();
        let dx = SliceExec {
            gate: &gate,
            executor: &exec,
            authority_snapshot: &snap,
            cwd: &fx.cwd.clone(),
            user_id: USER,
        };
        let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
        let cancel = CancelToken::new();
        let responder = |preview: &ApprovalPreview, _p: &PendingDecision| allow_once(preview);
        let term = dx
            .run_call(
                &mut fx.ledger,
                acked,
                &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
                &clock,
                &responder,
                &cancel,
            )
            .unwrap();
        let CallTerminal::Aborted { code, .. } = term else {
            panic!("expected aborted, got {term:?}")
        };
        assert_eq!(code, expected);
        assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
        let outcome = replay_segment(fx.ledger.path()).unwrap();
        // No tool.started (§2b row 6); the result is caused_by the allow
        // resolution (the last durable event, RULING-8).
        assert!(kinds(&outcome).iter().all(|k| *k != "tool.started"));
        assert_eq!(
            last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
            Some(last_of_kind(&outcome, "approval.resolved").event_id.as_str())
        );
        let result = result_payload(&outcome);
        assert_eq!(result.status, ResultStatus::Aborted);
        assert_eq!(result.start_state, StartState::NotStarted);
        let err = result.error.unwrap();
        assert_eq!(err.code, expected);
        assert!(!err.retryable);
        assert_eq!(result.model_delivery, ModelDelivery::Deliver);
        // The evidence names the failing construction stage/class.
        let ev = result.executor_evidence.unwrap();
        assert_eq!(ev.preflight.construction_stage, Some(stage));
        assert_eq!(ev.preflight.failure_class.as_deref(), Some(class));
        assert_eq!(ev.child_spawned, ChildSpawned::False);
        assert!(ev.child.is_none());
    }
}

// ---- §2b row 7: spawn error after the witness ----

#[test]
fn spawn_error_after_witness() {
    let mut fx = mk_fx("spawnerr");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &fx.cwd.clone(),
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let cancel = CancelToken::new();
    let responder = |preview: &ApprovalPreview, _p: &PendingDecision| allow_once(preview);
    let pass = dx.gate_and_witness(&mut fx.ledger, acked,
        &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
        &clock, &responder, &cancel).unwrap();
    let GatePass::Authorized(auth) = pass else { panic!("expected witness") };
    // Actual OS spawn failure AFTER successful preflight and witness.
    std::fs::remove_file(fx.cwd.join("hello.txt")).unwrap();
    std::fs::remove_dir(&fx.cwd).unwrap();
    let term = dx.execute_witnessed(&mut fx.ledger, *auth, &clock, &cancel).unwrap();
    let CallTerminal::Failed { code, .. } = term else {
        panic!("expected failed, got {term:?}")
    };
    assert_eq!(code, exec::code::SANDBOX_LAUNCH_FAILED);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    // The witness IS durable; the result is caused_by it (RULING-8).
    assert_eq!(
        last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
        Some(last_of_kind(&outcome, "tool.started").event_id.as_str())
    );
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Failed);
    assert_eq!(result.start_state, StartState::NotStarted);
    assert_eq!(result.error.as_ref().unwrap().code, exec::code::SANDBOX_LAUNCH_FAILED);
    let ev = result.executor_evidence.unwrap();
    assert_eq!(ev.child_spawned, ChildSpawned::False, "child_spawned carries ran-state");
    assert!(ev.child.is_none());
}

// ---- §2b row 8: child exit ≠ 0 ----

#[test]
fn child_error_exit_failed() {
    let mut fx = mk_fx("childerr");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &fx.cwd.clone(),
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./missing.txt"));
    let cancel = CancelToken::new();
    let responder = |preview: &ApprovalPreview, _p: &PendingDecision| allow_once(preview);
    let term = dx
        .run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./missing.txt")),
            &clock,
            &responder,
            &cancel,
        )
        .unwrap();
    let CallTerminal::Failed { code, .. } = term else {
        panic!("expected failed, got {term:?}")
    };
    assert_eq!(code, exec::code::EXECUTION_FAILED);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Failed);
    assert_eq!(result.start_state, StartState::Started);
    assert_eq!(result.error.as_ref().unwrap().code, exec::code::EXECUTION_FAILED);
    assert!(result.terminal_decision.is_none(), "no merged/refused on a plain run failure");
    // stderr and the exit code live in executor_evidence.child (S5 §3).
    let ev = result.executor_evidence.unwrap();
    assert_eq!(ev.child_spawned, ChildSpawned::True);
    let child = ev.child.unwrap();
    assert_eq!(child.exit_code, Some(1));
    match child.stderr {
        ContentValue::Inline { text } => assert!(text.contains("fx-read")),
        _ => panic!("stderr must be inline"),
    }
    match child.stdout {
        ContentValue::Inline { text } => assert_eq!(text, ""),
        _ => panic!("stdout must be inline"),
    }
}

// ---- §2b row 9: cancel after the witness, child reaped ----

#[test]
fn cancel_after_witness_reaps_child() {
    let mut fx = mk_fx("cancel-after");
    let path = fx.ledger.path().to_path_buf();
    let cancel = CancelToken::new();
    let token = cancel.clone();
    let handle = std::thread::spawn(move || {
        let gate = fx_gate(fx_config(), &fx.cwd);
        let exec = fx_executor();
        let snap = auth_snapshot();
        let dx = SliceExec {
            gate: &gate,
            executor: &exec,
            authority_snapshot: &snap,
            cwd: &fx.cwd.clone(),
            user_id: USER,
        };
        let step_ev = fx.step_ev.clone();
        let acked = fx
            .ledger
            .append_tool_call(
                "turn-1",
                "step-1",
                tool_call("call-1", &step_ev, json!({"argv": [FX_READ_COMMAND, "--fx-sleep-ms=30000", "./hello.txt"]})),
            )
            .unwrap();
        let responder = |preview: &ApprovalPreview, _p: &PendingDecision| allow_once(preview);
        let term = dx
            .run_call(
                &mut fx.ledger,
                acked,
                &tool_call("call-1", &step_ev, json!({"argv": [FX_READ_COMMAND, "--fx-sleep-ms=30000", "./hello.txt"]})),
                &clock,
                &responder,
                &cancel,
            )
            .unwrap();
        (term, exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst))
    });
    std::thread::sleep(std::time::Duration::from_millis(500));
    token.cancel();
    let (term, spawns) = handle.join().unwrap();
    assert_eq!(spawns, 1, "the child was spawned before the cancel");
    let CallTerminal::Failed { code, .. } = term else {
        panic!("expected failed, got {term:?}")
    };
    assert_eq!(code, exec::code::CANCELLED_AFTER_START);
    let outcome = replay_segment(&path).unwrap();
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Failed);
    assert_eq!(result.start_state, StartState::Started);
    assert_eq!(result.error.as_ref().unwrap().code, exec::code::CANCELLED_AFTER_START);
    let ev = result.executor_evidence.unwrap();
    assert_eq!(ev.child_spawned, ChildSpawned::True);
    assert!(ev.child.is_some(), "the reaped child's outcome is recorded");
}

// ---- §2b row 10: supervision loss (live + recovery) ----

#[test]
fn supervision_lost_live_indeterminate() {
    let mut fx = mk_fx("suplost");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &cwd,
        user_id: USER,
    };
    let (started, pending, matches, preflight) =
        reach_witness(&mut fx, &gate, &exec, "call-1", fx_args("./hello.txt"));
    // The executor is lost after the witness without a typed report
    // (dropped future / panic / channel closure): the live closer writes
    // indeterminate{origin: live} (RATIFY-1 as amended, §2b row 10).
    dx.supervision_lost(&mut fx.ledger, started, &pending, &matches, &preflight)
        .unwrap();
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Indeterminate);
    assert_eq!(result.start_state, StartState::Unknown);
    assert_eq!(result.origin, Some(IndeterminateOrigin::Live));
    let err = result.error.unwrap();
    assert_eq!(err.code, exec::code::EXECUTION_SUPERVISION_LOST);
    assert!(!err.retryable, "indeterminate is never auto-retried (RULING-3)");
    assert_eq!(
        last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
        Some(last_of_kind(&outcome, "tool.started").event_id.as_str())
    );
    let ev = evidence_json(&outcome);
    assert_eq!(ev["child_spawned"], json!("unknown"));
}

#[test]
fn recovery_after_witness_indeterminate() {
    let tag = "suprec";
    let root = tmp_root(tag);
    let path;
    let witness_event;
    {
        let mut fx = {
            let (mut ledger, _) = Ledger::open_new(&root, ids(tag)).unwrap();
            ledger
                .append_turn_start(
                    "turn-1",
                    TurnStart { input_digest: "sha256:fixture".into(), parent_turn_id: None },
                )
                .unwrap();
            let step = ledger
                .append_step_context(
                    "turn-1",
                    "step-1",
                    StepContext {
                        model: "slice-model".into(),
                        tool_router_revision: "r1".into(),
                        model_visible_tools: vec!["exec_command".into()],
                        mcp_bindings: vec![],
                        approval_policy: json!({"mode": "default"}),
                        sandbox_policy: serde_json::to_value(SANDBOX_POLICY).unwrap(),
                        settings_revision: "s1".into(),
                    },
                )
                .unwrap();
            let cwd = root.join("slice");
            std::fs::create_dir_all(&cwd).unwrap();
            std::fs::write(cwd.join("hello.txt"), "bob-slice-fixture\n").unwrap();
            Fx { ledger, step_ev: step.event_id, cwd }
        };
        let gate = fx_gate(fx_config(), &fx.cwd);
        let exec = fx_executor();
        let (started, _pending, _m, _p) =
            reach_witness(&mut fx, &gate, &exec, "call-1", fx_args("./hello.txt"));
        witness_event = started.witness().event_id.clone();
        path = fx.ledger.path().to_path_buf();
        fx.ledger.flush().unwrap();
        // Crash: the ledger is dropped with the witness durable and no
        // result (kill before vs after invocation are byte-identical,
        // CP-6/CP-7, I-05b).
    }
    let (mut ledger, resume) = Ledger::open_resume(&root, ids(tag)).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 1);
    let outcome = replay_segment(&path).unwrap();
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Indeterminate);
    assert_eq!(result.origin, Some(IndeterminateOrigin::Recovery));
    assert_eq!(result.error.as_ref().unwrap().code, "outcome_unknown");
    assert_eq!(result.recovered_by.as_deref(), Some(resume.event_id.as_str()));
    assert_eq!(
        last_of_kind(&outcome, "tool.result").caused_by.as_deref(),
        Some(witness_event.as_str())
    );
}

// ---- S1 §4a row 10 / FX-23: E4 refusal after completed_ok ----

#[test]
fn e4_digest_inconsistency_refused() {
    let mut fx = mk_fx("e4refuse");
    let mut gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let (started, pending, matches, preflight) =
        reach_witness(&mut fx, &gate, &exec, "call-1", fx_args("./hello.txt"));
    // The revocation epoch moves after the witness; the child still runs and
    // returns exit 0, but the E4 incorporation check refuses (approved→refused).
    gate.evaluator.revocation_epoch = 2;
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &cwd,
        user_id: USER,
    };
    let cancel = CancelToken::new();
    let term = dx
        .execute_witnessed(
            &mut fx.ledger,
            exec::ExecAuthorized {
                started,
                pending,
                matches,
                preflight,
            },
            &clock,
            &cancel,
        )
        .unwrap();
    let CallTerminal::Failed { code, .. } = term else {
        panic!("expected failed, got {term:?}")
    };
    assert_eq!(code, exec::code::INCORPORATION_REFUSED);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Failed);
    // It ran and returned ok, but nothing was incorporated (S1 §4a row 10).
    assert_eq!(result.start_state, StartState::Started);
    assert!(result.output.is_none());
    let err = result.error.unwrap();
    assert_eq!(err.code, exec::code::INCORPORATION_REFUSED);
    let terminal = result.terminal_decision.unwrap();
    assert_eq!(terminal.state, DecisionState::Refused);
    assert_eq!(terminal.reason.code, reason::CAPABILITY_REVOKED);
    let ev = result.executor_evidence.unwrap();
    assert_eq!(ev.child_spawned, ChildSpawned::True);
    assert_eq!(ev.child.unwrap().exit_code, Some(0));
}

// ---- A-8 (E9): `merged.target_id` = output_digest ----

/// SPEC-ERRATA-1 A-8: `merged.effect_incorporated.target_id` IS the result's
/// `output_digest` (then also `merged.reason.params.target_id`). The withdrawn
/// T3b S4 §5 form — the result record's own `event_id` — is refused by the
/// ledger, so the two spellings cannot diverge.
#[test]
fn a8_merged_effect_incorporated_target_id_is_the_output_digest() {
    // (1) The real path: exit 0 → merged, target_id = output_digest.
    let mut fx = mk_fx("a8");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &cwd,
        user_id: USER,
    };
    let acked = call(&mut fx, "call-a8", fx_args("./hello.txt"));
    let cancel = CancelToken::new();
    let responder = |preview: &ApprovalPreview, _p: &PendingDecision| allow_once(preview);
    let term = dx
        .run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-a8", &fx.step_ev, fx_args("./hello.txt")),
            &clock,
            &responder,
            &cancel,
        )
        .unwrap();
    assert!(matches!(term, CallTerminal::Succeeded { .. }), "{term:?}");

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let result = result_payload(&outcome);
    let digest = result.output_digest.clone().expect("a success carries a digest");
    assert_eq!(digest, sha256_digest(b"bob-slice-fixture\n"));
    let merged = result.terminal_decision.clone().expect("merged terminal");
    assert_eq!(merged.state, DecisionState::Merged);
    // The incorporation IS the merged reason: effect_incorporated with the
    // result's own digest as target (A-8, T3a S2 §4 form).
    assert_eq!(merged.reason.code, exec::code::EFFECT_INCORPORATED);
    assert_eq!(merged.reason.params["target_kind"], json!("tool_result"));
    assert_eq!(
        merged.reason.params["target_id"],
        json!(digest),
        "A-8: merged.effect_incorporated.target_id == output_digest"
    );
    // Both spellings A-8 names are the same value.
    assert_eq!(
        merged.reason.params["target_id"],
        json!(result.output_digest.clone().unwrap()),
        "merged.reason.params.target_id == output_digest"
    );

    // (2) The withdrawn T3b S4 §5 form — target_id = the result record's own
    // event_id — is refused: the digest is the only value the ledger accepts.
    let (started, pending, _matches, _preflight) =
        reach_witness(&mut fx, &gate, &exec, "call-a8bad", fx_args("./hello.txt"));
    let own_event_id = "0b9f6bd2-1c1e-4d3f-8a1e-3f2c9b7d5a11";
    let bad_merged = DecisionRecord {
        decision_id: pending.decision_id.clone(),
        state: DecisionState::Merged,
        reason: DecisionReason {
            code: exec::code::EFFECT_INCORPORATED.into(),
            params: json!({"target_kind": "tool_result", "target_id": own_event_id}),
            explanation: Explanation {
                text: "the call's output was incorporated".into(),
            },
        },
        actor: ActorRef {
            kind: ActorKind::PolicyEngine,
            stable_id: "engine".into(),
        },
        matched_rule_ref: None,
        previous_decision_id: Some(pending.decision_id.clone()),
        occurred_at: EVAL_TIME.into(),
        route: "user".into(),
        approval_mode: "default".into(),
        action_id: pending.action_id.clone(),
        policy_ref: None,
    };
    let bad = ToolResult {
        call_id: "call-a8bad".into(),
        status: ResultStatus::Succeeded,
        output: Some(json!({"kind": "inline", "text": "bob-slice-fixture\n"})),
        output_digest: Some(sha256_digest(b"bob-slice-fixture\n")),
        error: None,
        effect_ids: vec![],
        governing_decision_id: Some(pending.decision_id.clone()),
        terminal_decision: Some(bad_merged),
        start_state: StartState::Started,
        model_delivery: ModelDelivery::Deliver,
        origin: None,
        recovered_by: None,
        executor_evidence: None,
    };
    let err = fx
        .ledger
        .record_post_witness_result(started, bad)
        .expect_err("own-event_id target must be refused (A-8)");
    assert!(
        matches!(err, LedgerError::SuccessOutputDigestMismatch),
        "expected SuccessOutputDigestMismatch, got {err:?}"
    );
}

#[cfg(target_os = "linux")]
#[test]
fn e12_prohibited_host_read_is_not_exposed() {
    let mut fx = mk_fx("linux-host-read");
    let secret = fx.cwd.parent().unwrap().join("host-secret");
    std::fs::write(&secret, "E12_HOST_SECRET_MUST_NOT_LEAK").unwrap();
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args(secret.to_str().unwrap());
    let acked = call(&mut fx, "call-1", args.clone());
    let term = dx.run_call(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock,
        &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    assert!(!matches!(term, CallTerminal::Succeeded { .. }), "host read escaped confinement");
    let replay = replay_segment(fx.ledger.path()).unwrap();
    let result = result_payload(&replay);
    assert_ne!(result.status, ResultStatus::Succeeded);
    assert!(!serde_json::to_string(&result).unwrap().contains("E12_HOST_SECRET_MUST_NOT_LEAK"));
}

#[cfg(target_os = "linux")]
fn e12_helper_fault(fault: &str) {
    use std::os::unix::fs::PermissionsExt;
    {
        let mut fx = mk_fx(fault);
        let helper = fx.cwd.parent().unwrap().join("bwrap-helper");
        std::fs::copy("/usr/bin/bwrap", &helper).unwrap();
        let mut exec = fx_executor();
        exec.bwrap_path = helper.clone();
        exec.bwrap_digest = Some(sha256_digest(&std::fs::read(&helper).unwrap()));
        let expected = match fault {
            "missing" => { std::fs::remove_file(&helper).unwrap(); exec::code::ISOLATION_UNAVAILABLE },
            "digest" => { exec.bwrap_digest = Some("sha256:bad".into()); exec::code::HELPER_VERIFICATION_FAILED },
            _ => { std::fs::set_permissions(&helper, std::fs::Permissions::from_mode(0o600)).unwrap(); exec::code::SANDBOX_LAUNCH_FAILED },
        };
        let gate = fx_gate(fx_config(), &fx.cwd); let snap = auth_snapshot(); let cwd = fx.cwd.clone();
        let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
        let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
        let term = dx.run_call(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
        assert!(matches!(term, CallTerminal::Aborted { code, .. } if code == expected), "{fault}: {term:?}");
        let replay = replay_segment(fx.ledger.path()).unwrap();
        assert!(!kinds(&replay).contains(&"tool.started"));
        let result = result_payload(&replay);
        assert_eq!(result.start_state, StartState::NotStarted);
        let evidence = result.executor_evidence.unwrap();
        assert_eq!(evidence.backend, "linux_bubblewrap");
        assert_eq!(evidence.child_spawned, ChildSpawned::False);
        assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    }
}

#[cfg(target_os = "linux")]
#[test] fn e12_helper_missing() { e12_helper_fault("missing"); }
#[cfg(target_os = "linux")]
#[test] fn e12_helper_digest() { e12_helper_fault("digest"); }
#[cfg(target_os = "linux")]
#[test] fn e12_helper_unexecutable() { e12_helper_fault("unexecutable"); }

#[cfg(target_os = "linux")]
#[test]
fn e12_confinement_negative_controls_and_seccomp() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let mut fx = mk_fx("confinement");
    let secret = fx.cwd.parent().unwrap().join("host-secret");
    std::fs::write(&secret, "HOST_SECRET").unwrap();
    symlink(&secret, fx.cwd.join("escape-link")).unwrap();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let namespaces: Vec<String> = ["user", "pid", "ipc", "net", "mnt"].iter()
        .map(|n| std::fs::read_link(format!("/proc/self/ns/{n}")).unwrap().to_string_lossy().into()).collect();
    let script = format!(r##"#!/bin/sh
printf '\036BOB_SLICE_PAYLOAD_V1\n'
exec /usr/bin/python3 - <<'PROBE'
import os, errno, ctypes, socket, json
assert open('hello.txt').read() == 'bob-slice-fixture\n'
for path in [{secret:?}, 'escape-link', '/etc/passwd']:
    try: open(path).read()
    except OSError: pass
    else: raise AssertionError('host read exposed: '+path)
for path in ['new-write', 'hello.txt', {secret:?}, '/etc/new-write']:
    try: open(path, 'w').write('escaped')
    except OSError: pass
    else: raise AssertionError('write escaped: '+path)
s = socket.socket(); s.settimeout(.3)
assert s.connect_ex(('127.0.0.1', {port})) != 0, 'host network reachable'
ns = [os.readlink('/proc/self/ns/'+n) for n in ['user','pid','ipc','net','mnt']]
assert all(a != b for a,b in zip(ns, {namespaces:?})), ns
status = open('/proc/self/status').read()
assert 'NoNewPrivs:\t1' in status, status
assert 'Seccomp:\t2' in status, status
libc=ctypes.CDLL(None, use_errno=True)
# x86_64 syscall ABI; the backend must reject unsupported architectures.
for nr in [101, 310, 311, 425, 426, 427]:
    ctypes.set_errno(0)
    rc=libc.syscall(nr, 0, 0, 0, 0, 0, 0)
    assert rc == -1 and ctypes.get_errno() == errno.EPERM, (nr, rc, ctypes.get_errno())
print(json.dumps({{'allowed_read':True,'host_reads_denied':True,'writes_denied':True,'host_network_denied':True,'namespaces_isolated':True,'no_new_privs':True,'seccomp_denies':True}}))
PROBE
"##);
    let probe = fx.cwd.parent().unwrap().join("confinement-probe");
    std::fs::write(&probe, script).unwrap();
    std::fs::set_permissions(&probe, std::fs::Permissions::from_mode(0o700)).unwrap();
    let exec = Executor::new("confinement", probe);
    let gate = fx_gate(fx_config(), &fx.cwd); let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let term = dx.run_call(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    let replay = replay_segment(fx.ledger.path()).unwrap();
    let evidence = evidence_json(&replay);
    println!("confinement evidence: {evidence}");
    assert!(matches!(term, CallTerminal::Succeeded { .. }), "{term:?}, {evidence}");
    assert_eq!(std::fs::read_to_string(&secret).unwrap(), "HOST_SECRET");
    assert!(!fx.cwd.join("new-write").exists());
}

#[cfg(target_os = "linux")]
#[test]
fn e12_preflight_pins_helper_and_payload_bytes_across_replacement() {
    use std::os::unix::fs::PermissionsExt;
    let mut fx = mk_fx("pinned");
    let dir = fx.cwd.parent().unwrap();
    let helper = dir.join("bwrap"); let payload = dir.join("fx-read");
    std::fs::copy("/usr/bin/bwrap", &helper).unwrap();
    std::fs::copy(env!("CARGO_BIN_EXE_fx-read"), &payload).unwrap();
    let mut exec = Executor::new("pinned", payload.clone());
    exec.bwrap_path = helper.clone(); exec.bwrap_digest = Some(sha256_digest(&std::fs::read(&helper).unwrap()));
    let gate = fx_gate(fx_config(), &fx.cwd); let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let pass = dx.gate_and_witness(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    let GatePass::Authorized(auth) = pass else { panic!("expected witness") };
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    assert!(!format!("{auth:?}").contains("127, 69, 76, 70"), "debug must not dump provisioned executable bytes");
    for file in [&helper, &payload] {
        std::fs::remove_file(file).unwrap();
        std::fs::write(file, "#!/bin/sh\nprintf tampered\\n\n").unwrap();
        std::fs::set_permissions(file, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let term = dx.execute_witnessed(&mut fx.ledger, *auth, &clock, &CancelToken::new()).unwrap();
    assert!(matches!(term, CallTerminal::Succeeded { .. }), "{term:?}");
    let result = result_payload(&replay_segment(fx.ledger.path()).unwrap());
    assert_eq!(result.output.unwrap(), json!({"kind":"inline","text":"bob-slice-fixture\n"}));
    let evidence = result.executor_evidence.unwrap();
    assert!(evidence.helper.unwrap().verified);
    assert_eq!(evidence.policy.seccomp, "deny_ptrace_process_vm_io_uring_x86_64");
    assert!(evidence.policy.read_roots.contains(&cwd.to_string_lossy().into_owned()));
}

#[cfg(target_os = "linux")]
#[test]
fn e12_helper_exit_without_payload_proof_is_indeterminate() {
    let mut fx = mk_fx("helper-unknown");
    let mut exec = fx_executor();
    exec.bwrap_path = PathBuf::from("/usr/bin/false");
    exec.bwrap_digest = Some(sha256_digest(&std::fs::read(&exec.bwrap_path).unwrap()));
    let gate = fx_gate(fx_config(), &fx.cwd); let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let _ = dx.run_call(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    let replay = replay_segment(fx.ledger.path()).unwrap();
    assert!(kinds(&replay).contains(&"tool.started"));
    let result = result_payload(&replay);
    assert_eq!(result.status, ResultStatus::Indeterminate);
    assert_eq!(result.start_state, StartState::Unknown);
    assert_eq!(result.executor_evidence.unwrap().child_spawned, ChildSpawned::Unknown);
}

#[cfg(target_os = "linux")]
#[test]
fn e12_manifest_cannot_widen_read_roots() {
    let mut fx = mk_fx("widen-root");
    let mut gate = fx_gate(fx_config(), &fx.cwd);
    gate.evaluator.manifest.tools.get_mut("exec_command").unwrap().read_roots.push("/".into());
    let exec = fx_executor(); let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let term = dx.run_call(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    assert!(matches!(term, CallTerminal::Aborted { code: exec::code::ISOLATION_UNAVAILABLE, .. }), "{term:?}");
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    assert!(!kinds(&replay_segment(fx.ledger.path()).unwrap()).contains(&"tool.started"));
}

#[test]
fn e12_witness_sync_failure_no_spawn() {
    let mut fx = mk_fx("syncfail");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let exec = fx_executor();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let dx = SliceExec {
        gate: &gate,
        executor: &exec,
        authority_snapshot: &snap,
        cwd: &cwd,
        user_id: USER,
    };
    let acked = call(&mut fx, "call-1", fx_args("./hello.txt"));
    let (acked, pending) = match gate
        .authorize_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &fx.step_ev, fx_args("./hello.txt")),
            &snap,
            EVAL_TIME,
            &cwd,
        )
        .unwrap()
    {
        Authorization::Ask { acked, pending, preview, .. } => {
            match gate
                .resolve_approval(&mut fx.ledger, acked, pending, allow_once(&preview), USER, EVAL_TIME, &snap)
                .unwrap()
            {
                ApprovalOutcome::Allowed { acked, pending, .. } => (acked, pending),
                _ => panic!("allow_once must approve"),
            }
        }
        _ => panic!("fx-read must route to ask"),
    };
    let acked = match gate
        .pre_witness_check(&mut fx.ledger, acked, pending.clone(), EVAL_TIME, &snap)
        .unwrap()
    {
        PreWitness::Cleared { acked } => acked,
        _ => panic!("clean E3 must clear"),
    };
    let preflight = exec
        .preflight(&[FX_READ_COMMAND.to_string(), "./hello.txt".to_string()], &cwd)
        .unwrap();
    let matches = gate.evaluator.rule_matches("exec_command", &fx_args("./hello.txt"));
    // The witness append fails (fsync/write failure); the segment faults.
    fx.ledger.debug_fail_next_sync();
    let pass = dx.witness(&mut fx.ledger, acked, &pending, matches, preflight).unwrap();
    let GatePass::Terminal(CallTerminal::WitnessWriteFailed { witness, best_effort }) = pass else {
        panic!("expected witness-write failure, got {pass:?}")
    };
    assert!(matches!(witness, LedgerError::Io(_)));
    // MGR-2: the faulted segment refuses the best-effort aborted record too
    // → ledger-fatal (S4 §2b row 5).
    assert!(matches!(best_effort, Some(LedgerError::SegmentFaulted)));
    // S4 §6.1: spawn count 0 — the tool never ran (RULING-5).
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert!(kinds(&outcome).contains(&"tool.started"));
    assert!(kinds(&outcome).iter().all(|k| *k != "tool.result"));
}


#[cfg(target_os = "linux")]
#[test]
fn e12_payload_digest_is_checked_before_witness() {
    let mut fx = mk_fx("payload-digest");
    let payload = fx.cwd.parent().unwrap().join("payload");
    std::fs::copy(env!("CARGO_BIN_EXE_fx-read"), &payload).unwrap();
    let exec = Executor::new("digest", payload.clone());
    std::fs::write(&payload, b"tampered").unwrap();
    let gate = fx_gate(fx_config(), &fx.cwd); let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let term = dx.run_call(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    assert!(matches!(term, CallTerminal::Aborted { code: exec::code::HELPER_VERIFICATION_FAILED, .. }), "unexpected terminal");
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    assert!(!kinds(&replay_segment(fx.ledger.path()).unwrap()).contains(&"tool.started"));
}

#[cfg(target_os = "linux")]
#[test]
fn e12_fixture_root_is_pinned_across_symlink_swap() {
    use std::os::unix::fs::symlink;
    let mut fx = mk_fx("root-swap");
    let exec = fx_executor(); let gate = fx_gate(fx_config(), &fx.cwd);
    let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let pass = dx.gate_and_witness(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    let GatePass::Authorized(auth) = pass else { panic!("expected witness") };
    let saved = cwd.with_file_name("saved-slice");
    let hostile = cwd.with_file_name("hostile"); std::fs::create_dir(&hostile).unwrap();
    std::fs::write(hostile.join("hello.txt"), "HOSTILE_ROOT").unwrap();
    std::fs::rename(&cwd, &saved).unwrap(); symlink(&hostile, &cwd).unwrap();
    let term = dx.execute_witnessed(&mut fx.ledger, *auth, &clock, &CancelToken::new()).unwrap();
    assert!(matches!(term, CallTerminal::Succeeded { .. }), "unexpected terminal");
    let result = result_payload(&replay_segment(fx.ledger.path()).unwrap());
    assert_eq!(result.output.unwrap(), json!({"kind":"inline","text":"bob-slice-fixture\n"}));
}

#[test]
fn e12_supervision_io_loss_never_claims_no_spawn() {
    let mut fx = mk_fx("io-loss");
    let mut exec = fx_executor(); exec.debug_supervision_loss = true;
    let gate = fx_gate(fx_config(), &fx.cwd); let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let _ = dx.run_call(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    let result = result_payload(&replay_segment(fx.ledger.path()).unwrap());
    assert_eq!(result.status, ResultStatus::Indeterminate);
    assert_eq!(result.start_state, StartState::Unknown);
    assert_eq!(result.executor_evidence.unwrap().child_spawned, ChildSpawned::Unknown);
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 1);
}

#[cfg(target_os = "linux")]
#[test]
fn e12_fixture_root_cannot_be_root_or_a_symlink() {
    let exec = fx_executor();
    let argv = vec![FX_READ_COMMAND.into(), "./hello.txt".into()];
    assert!(exec.preflight(&argv, std::path::Path::new("/")).is_err(), "must never bind host root");
    let fx = mk_fx("alias");
    let alias_parent = tmp_root("alias-parent");
    let alias = alias_parent.join("slice");
    std::os::unix::fs::symlink(&fx.cwd, &alias).unwrap();
    assert!(exec.preflight(&argv, &alias).is_err(), "fixture root must be canonical before authorization-to-attempt");
}

#[test]
fn e12_witnessed_argv_cannot_be_rebound_before_spawn() {
    let mut fx = mk_fx("argv-binding");
    std::fs::write(fx.cwd.join("other.txt"), "WRONG_ACTION").unwrap();
    let exec = fx_executor(); let gate = fx_gate(fx_config(), &fx.cwd);
    let snap = auth_snapshot(); let cwd = fx.cwd.clone();
    let dx = SliceExec { gate: &gate, executor: &exec, authority_snapshot: &snap, cwd: &cwd, user_id: USER };
    let args = fx_args("./hello.txt"); let acked = call(&mut fx, "call-1", args.clone());
    let pass = dx.gate_and_witness(&mut fx.ledger, acked, &tool_call("call-1", &fx.step_ev, args), &clock, &|p, _| allow_once(p), &CancelToken::new()).unwrap();
    let GatePass::Authorized(mut auth) = pass else { panic!("expected witness") };
    auth.preflight.argv[1] = "./other.txt".into();
    let term = dx.execute_witnessed(&mut fx.ledger, *auth, &clock, &CancelToken::new()).unwrap();
    assert!(matches!(term, CallTerminal::Failed { code: exec::code::SANDBOX_LAUNCH_FAILED, .. }), "must fail before spawn on a rebound plan");
    assert_eq!(exec.spawn_attempts.load(std::sync::atomic::Ordering::SeqCst), 0);
    let result = result_payload(&replay_segment(fx.ledger.path()).unwrap());
    assert_eq!(result.start_state, StartState::NotStarted);
    assert_eq!(result.executor_evidence.unwrap().child_spawned, ChildSpawned::False);
}

// ---- F-H1 (MVP3): bounded supervision of the child's output pipes ----------
//
// Regression for the independent review's HIGH finding: the supervisor used to
// poll `try_wait()` and only drain the pipes after exit, so a payload writing
// more than the pipe buffer blocked forever and the call never reached a
// terminal. These tests fix the public executor boundary: a >pipe-capacity
// payload must reach a typed terminal inside the bound, and a payload that
// outlives the bound must be killed, reaped and reported `indeterminate`.

/// Write an executable payload script and return its path.
fn payload_script(tag: &str, body: &str) -> PathBuf {
    let path = tmp_root(tag).join("payload.sh");
    std::fs::write(&path, body).unwrap();
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    path
}

/// A fresh canonical fixture root (basename `slice`, as preflight requires).
fn slice_root(tag: &str) -> PathBuf {
    let root = tmp_root(tag).join("slice");
    std::fs::create_dir_all(&root).unwrap();
    root.canonicalize().unwrap()
}

/// Run `spawn_supervise` under a watchdog so a regression fails this test
/// instead of wedging the suite: the review's bug was an unbounded wait.
fn supervise_with_watchdog(
    payload: PathBuf,
    cwd: PathBuf,
    timeout: std::time::Duration,
    budget: std::time::Duration,
) -> exec::Supervised {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut executor = Executor::new("exec-fh1", payload);
        executor.execution_timeout = timeout;
        let argv = vec![FX_READ_COMMAND.to_string(), "./hello.txt".to_string()];
        let sent = match executor.preflight(&argv, &cwd) {
            Err(failure) => Err(format!("preflight failed: {failure:?}")),
            Ok(preflight) => executor
                .spawn_supervise(&preflight, &CancelToken::new())
                .map_err(|failure| format!("spawn failed: {failure}")),
        };
        let _ = tx.send(sent);
    });
    match rx.recv_timeout(budget) {
        Ok(Ok(supervised)) => supervised,
        Ok(Err(failure)) => panic!("executor boundary rejected the payload: {failure}"),
        Err(_) => panic!(
            "the executor did not return within {budget:?}: supervision is wedged (F-H1)"
        ),
    }
}

/// 4 MiB is far above any pipe capacity (64 KiB on Linux), so this payload
/// cannot finish unless the supervisor drains while it runs.
const BIG_OUTPUT_BYTES: usize = 4 * 1024 * 1024;

#[test]
fn payload_output_larger_than_the_pipe_buffer_reaches_a_typed_terminal() {
    let body = format!(
        "#!/bin/sh\nprintf '\\036BOB_SLICE_PAYLOAD_V1\\n'\nhead -c {BIG_OUTPUT_BYTES} /dev/zero | tr '\\0' 'x'\nexit 0\n"
    );
    let payload = payload_script("fh1-stdout", &body);
    let cwd = slice_root("fh1-stdout");
    let supervised = supervise_with_watchdog(
        payload,
        cwd,
        std::time::Duration::from_secs(30),
        std::time::Duration::from_secs(60),
    );
    assert!(supervised.payload_started, "the payload's startup proof is required");
    assert!(!supervised.timed_out, "a draining supervisor must not time out");
    assert!(!supervised.cancelled);
    assert_eq!(supervised.outcome.exit_code, Some(0));
    let expected = vec![b'x'; BIG_OUTPUT_BYTES];
    let expected_digest = sha256_digest(&expected);
    match &supervised.outcome.stdout {
        ContentValue::Stored { digest, byte_len } => {
            assert_eq!(*byte_len, BIG_OUTPUT_BYTES as u64, "byte_len is the whole stream");
            assert_eq!(digest, &expected_digest, "the digest covers the whole stream");
        }
        other => panic!("expected a Stored digest ref above the inline cap, got {other:?}"),
    }
    assert_eq!(supervised.stdout_digest, expected_digest);
    assert_eq!(
        supervised.stdout_bytes.len(),
        bob_engine::exec::RETAINED_OUTPUT_CAP_BYTES,
        "retained bytes are capped, so a hostile payload cannot exhaust memory"
    );
}

#[test]
fn payload_stderr_larger_than_the_pipe_buffer_reaches_a_typed_terminal() {
    let body = format!(
        "#!/bin/sh\nprintf '\\036BOB_SLICE_PAYLOAD_V1\\n'\nhead -c {BIG_OUTPUT_BYTES} /dev/zero | tr '\\0' 'y' >&2\nexit 0\n"
    );
    let payload = payload_script("fh1-stderr", &body);
    let cwd = slice_root("fh1-stderr");
    let supervised = supervise_with_watchdog(
        payload,
        cwd,
        std::time::Duration::from_secs(30),
        std::time::Duration::from_secs(60),
    );
    assert!(supervised.payload_started);
    assert!(!supervised.timed_out);
    assert_eq!(supervised.outcome.exit_code, Some(0));
    let expected = vec![b'y'; BIG_OUTPUT_BYTES];
    match &supervised.outcome.stderr {
        ContentValue::Stored { digest, byte_len } => {
            assert_eq!(*byte_len, BIG_OUTPUT_BYTES as u64);
            assert_eq!(digest, &sha256_digest(&expected));
        }
        other => panic!("expected a Stored digest ref for stderr, got {other:?}"),
    }
    // stdout carried only the startup proof, which is transport evidence.
    assert_eq!(supervised.outcome.stdout, ContentValue::Inline { text: String::new() });
}

#[test]
fn payload_that_outlives_the_bound_is_killed_and_reported_indeterminate() {
    let mut fx = mk_fx("fh1-timeout");
    let gate = fx_gate(fx_config(), &fx.cwd);
    let payload = payload_script(
        "fh1-timeout",
        "#!/bin/sh\nprintf '\\036BOB_SLICE_PAYLOAD_V1\\n'\nsleep 30\nexit 0\n",
    );
    let mut executor = Executor::new("exec-1", payload);
    executor.execution_timeout = std::time::Duration::from_secs(1);
    let args = fx_args("./hello.txt");
    let acked = call(&mut fx, "call-1", args.clone());
    let step_ev = fx.step_ev.clone();
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let dx = SliceExec {
            gate: &gate,
            executor: &executor,
            authority_snapshot: &snap,
            cwd: &cwd,
            user_id: USER,
        };
        let cancel = CancelToken::new();
        let responder = |preview: &ApprovalPreview, _p: &PendingDecision| allow_once(preview);
        let terminal = dx.run_call(
            &mut fx.ledger,
            acked,
            &tool_call("call-1", &step_ev, args),
            &clock,
            &responder,
            &cancel,
        );
        let _ = tx.send((terminal, fx.ledger));
    });
    let (terminal, ledger) = match rx.recv_timeout(std::time::Duration::from_secs(30)) {
        Ok(pair) => pair,
        Err(_) => panic!("the call never reached a terminal inside 30s (F-H1)"),
    };
    let terminal = terminal.unwrap();
    assert!(
        matches!(terminal, CallTerminal::Indeterminate { .. }),
        "an outliving payload is indeterminate, got {terminal:?}"
    );
    let outcome = replay_segment(ledger.path()).unwrap();
    assert_eq!(outcome.diagnostics, vec![], "clean replay");
    assert_eq!(*kinds(&outcome).last().unwrap(), "tool.result");
    let result = result_payload(&outcome);
    assert_eq!(result.status, ResultStatus::Indeterminate);
    assert_eq!(result.start_state, StartState::Unknown);
    assert_eq!(result.origin, Some(IndeterminateOrigin::Live));
    let error = result.error.expect("indeterminate carries the catalog error");
    assert_eq!(error.code, "execution_supervision_lost");
    assert!(!error.retryable, "indeterminate is never auto-retried (RULING-3)");
}
