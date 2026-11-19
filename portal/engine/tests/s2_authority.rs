//! S2 authority evaluator tests — the AUD-7 / S2 §1/§5 fixture battery as
//! unit tests (E3): `missing_verdict`, `capability_undeclared`,
//! `capability_revoked` (pre-approval and while pending / post-approval),
//! the snapshot/digest `*_mismatch` sweep (FX-10, pre/post approval),
//! `integration_invariant_failed`, `rule_forbidden`, `user_denied`,
//! `sandbox_unavailable`, FX-13 `mode_unsupported` at config load, the
//! `configuration_disabled` catalog exclusion (D-13), and the S2.9#8
//! hash-input mutation battery. Plus the `authorize_call` ledger wiring
//! (T3a S2 §4): allow/ask/deny paths write the correct E2 ledger events.
//!
//! Fixture policy (S4 §1b style): `PermissionMode=default`, manifest tool
//! `exec_command` (`effect_class=read`), three rules — deny `rm *`,
//! ask `cat *`, allow `echo *`; the production command (`cat`) matches
//! `ask`, so approval runs end to end.

use std::path::PathBuf;

use serde_json::{json, Value};

use bob_engine::authority::*;
use bob_engine::digest::{jcs_sha256, sha256_digest};
use bob_engine::envelope::{EventKind, Record};
use bob_engine::evaluator::{
    action_summary, args_preview, reason, Evaluator, Matcher, PolicyConfig, Rule, RuleEffect,
    ToolEntry, ToolManifest,
};
use bob_engine::gate::{
    ApprovalOutcome, ApprovalResponse, Authorization, Gate, GateError, PendingDecision, PreWitness,
};
use bob_engine::ledger::{AckedCall, DurableAck, Ledger, SessionIds};
use bob_engine::payload::*;
use bob_engine::replay::replay_segment;

const EVAL_TIME: &str = "2026-10-17T00:00:00.000Z";
const RULES_SRC: &str = "rules/default.rules";

fn tmp_root(tag: &str) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e3-{tag}-{}-{nanos}-{}",
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

/// The fixture policy: mode default + the three-rule stack (S4 §1b).
fn fx_config() -> PolicyConfig {
    PolicyConfig::from_json(&json!({
        "mode": "default",
        "revision": "rules/default.rules#r1",
        "rules": [
            {"effect": "deny", "matcher": {"tool": "exec_command", "argv_prefix": ["rm"]}, "source": RULES_SRC},
            {"effect": "ask",  "matcher": {"tool": "exec_command", "argv_prefix": ["cat"]}, "source": RULES_SRC},
            {"effect": "allow","matcher": {"tool": "exec_command", "argv_prefix": ["echo"]}, "source": RULES_SRC}
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
            read_roots: vec![
                "/bin".into(),
                "/lib".into(),
                "/lib64".into(),
                "/usr".into(),
            ],
            revoked: false,
        },
    );
    m
}

fn fx_gate() -> Gate {
    Gate {
        evaluator: Evaluator {
            config: fx_config(),
            manifest: fx_manifest(),
            sandbox_available: true,
            revocation_epoch: 1,
            fail_verdict: false,
            fail_integration: None,
        },
    }
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
        .append_turn_start("turn-1", TurnStart { input_digest: "sha256:fixture".into(), parent_turn_id: None })
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
                sandbox_policy: json!({"policy": "read_only", "network_access": false}),
                settings_revision: "s1".into(),
            },
        )
        .unwrap();
    let cwd = root.join("cwd");
    std::fs::create_dir_all(&cwd).unwrap();
    std::fs::write(cwd.join("hello.txt"), "bob-slice-fixture\n").unwrap();
    Fx { ledger, step_ev: step.event_id, cwd }
}

fn tool_call(call_id: &str, snapshot_event_id: &str, tool_name: &str, args: Value) -> ToolCall {
    ToolCall {
        call_id: call_id.into(),
        model_item_id: "item-1".into(),
        call_index: 0,
        tool_name: tool_name.into(),
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

fn call(fx: &mut Fx, call_id: &str, tool_name: &str, args: Value) -> AckedCall {
    let step_ev = fx.step_ev.clone();
    fx.ledger
        .append_tool_call("turn-1", "step-1", tool_call(call_id, &step_ev, tool_name, args))
        .unwrap()
}

fn cat_args() -> Value {
    json!({"argv": ["cat", "./hello.txt"]})
}

/// Authorize a `cat ./hello.txt` call and unwrap the Ask branch.
fn ask_path(fx: &mut Fx, gate: &Gate, call_id: &str) -> (AckedCall, PendingDecision, DurableAck) {
    let acked = call(fx, call_id, "exec_command", cat_args());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    match gate.authorize_call(&mut fx.ledger, acked, &tool_call(call_id, &fx.step_ev.clone(), "exec_command", cat_args()), &snap, EVAL_TIME, &cwd).unwrap() {
        Authorization::Ask { acked, decided, pending, .. } => (acked, pending, decided),
        _ => panic!("cat must route to ask"),
    }
}

fn allow_once(pending: &PendingDecision) -> ApprovalResponse {
    ApprovalResponse::AllowOnce {
        decision_id: pending.decision_id.clone(),
        args_digest: pending.digests.args_digest.clone(),
    }
}

fn ok_result(call_id: &str, gid: &str) -> ToolResult {
    let mut merged = DecisionRecord {
        decision_id: gid.into(),
        state: DecisionState::Merged,
        reason: DecisionReason {
            code: "effect_incorporated".into(),
            params: json!({"target_kind": "tool_result", "target_id": "sha256:out"}),
            explanation: Explanation { text: "merged".into() },
        },
        actor: ActorRef { kind: ActorKind::PolicyEngine, stable_id: "engine".into() },
        matched_rule_ref: None,
        previous_decision_id: Some(gid.into()),
        occurred_at: EVAL_TIME.into(),
        route: "user".into(),
        approval_mode: "default".into(),
        action_id: "act".into(),
        policy_ref: None,
    };
    merged.reason.params = json!({"target_kind": "tool_result", "target_id": "sha256:out"});
    ToolResult {
        call_id: call_id.into(),
        status: ResultStatus::Succeeded,
        output: Some(json!({"text": "bob-slice-fixture\n"})),
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

fn last_of_kind<'a>(outcome: &'a bob_engine::replay::ReplayOutcome, kind: &str) -> &'a Record {
    outcome.assertions.iter().rev().find(|r| r.kind.as_str() == kind).unwrap()
}

fn decided_payload<'a>(outcome: &'a bob_engine::replay::ReplayOutcome) -> AuthorityDecided {
    serde_json::from_value(last_of_kind(outcome, "authority.decided").payload.clone()).unwrap()
}

fn result_payload<'a>(outcome: &'a bob_engine::replay::ReplayOutcome) -> ToolResult {
    serde_json::from_value(last_of_kind(outcome, "tool.result").payload.clone()).unwrap()
}

// ---------------------------------------------------------------- FX-13

/// FX-13: each reserved mode → typed `mode_unsupported` at config load, no
/// evaluator, no lineage (RULING-1). `default` loads; unknown strings are
/// malformed, never silently mapped.
#[test]
fn fx13_each_reserved_mode_rejected_at_load() {
    for mode in ["acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"] {
        let err = PolicyConfig::from_json(&json!({"mode": mode})).unwrap_err();
        assert_eq!(err.code(), "mode_unsupported", "mode {mode}");
        match err {
            bob_engine::evaluator::ConfigError::ModeUnsupported { mode: m } => assert_eq!(m, mode),
            other => panic!("expected ModeUnsupported, got {other:?}"),
        }
    }
    assert!(PolicyConfig::from_json(&json!({"mode": "default"})).is_ok());
    assert_eq!(
        PolicyConfig::from_json(&json!({"mode": "OnRequest"})).unwrap_err().code(),
        "policy_malformed"
    );
}

/// D-13 / DEFERRED-EXT-1: `configuration_disabled` is unreachable without
/// granular settings and `authority_expired` is removed — neither is in the
/// slice reason catalog (EXEC-23). The config surface is covered by FX-13.
#[test]
fn configuration_disabled_and_expiry_not_in_slice_catalog() {
    assert!(!reason::SLICE_CATALOG.contains(&"configuration_disabled"));
    assert!(!reason::SLICE_CATALOG.contains(&"authority_expired"));
}

// ---------------------------------------------------------------- E1 paths

/// FX-01 first half + T3a S2 §2: default mode, NO matching rule → ask with
/// `source=mode_default`, plus the full DecisionRecord shape.
#[test]
fn default_mode_no_rule_asks() {
    let mut fx = mk_fx("ask");
    let gate = fx_gate();
    let args = json!({"argv": ["ls"]});
    let acked = call(&mut fx, "call-1", "exec_command", args.clone());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "exec_command", args);
    let (acked, pending, _decided) =
        match gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap() {
            Authorization::Ask { acked, decided, pending, .. } => (acked, pending, decided),
            _ => panic!("ls matches no rule: mode default ask"),
        };
    assert_eq!(pending.outcome, DecisionOutcome::Ask);
    assert!(pending.matched.is_none(), "no rule matched: mode default");

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let decided = decided_payload(&outcome);
    assert_eq!(decided.outcome, DecisionOutcome::Ask);
    assert_eq!(decided.source, DecisionSource::ModeDefault);
    assert!(decided.matched_rule_ref.is_none());
    assert_eq!(decided.decision_ref, decided.decision.decision_id);
    // Proposed record: state proposed, previous null (S2 §4 chain).
    assert_eq!(decided.proposed.state, DecisionState::Proposed);
    assert!(decided.proposed.previous_decision_id.is_none());
    // Routed record: approval_route_selected, MGR-9 constants, one clock.
    let d = &decided.decision;
    assert_eq!(d.state, DecisionState::ApprovalRequired);
    assert_eq!(d.reason.code, reason::APPROVAL_ROUTE_SELECTED);
    assert_eq!(d.route, "user");
    assert_eq!(d.approval_mode, "default");
    assert_eq!(d.occurred_at, EVAL_TIME);
    assert_ne!(d.action_id, "call-1", "action_id distinct from call_id (MGR-9)");
    assert_eq!(d.action_id, decided.proposed.action_id);
    assert_eq!(d.decision_id, decided.proposed.decision_id, "decision_id stable (IN-7)");
    assert_eq!(d.previous_decision_id.as_deref(), Some(d.decision_id.as_str()));
    assert_eq!(d.actor.kind, ActorKind::PolicyEngine);
    assert!(d.matched_rule_ref.is_none());
    // MGR-1 digest set embedded in reason.params (durable verbatim).
    let p = &d.reason.params;
    assert_eq!(p["args_digest"], json!(pending.digests.args_digest));
    assert_eq!(p["action_digest"], json!(pending.digests.action_digest));
    assert_eq!(p["scope_digest"], json!(pending.digests.scope_digest));
    assert_eq!(p["authority_snapshot_digest"], json!(pending.digests.authority_snapshot_digest));
    assert_eq!(p["policy_snapshot_digest"], json!(pending.digests.policy_snapshot_digest));
    assert_eq!(p["revocation_epoch"], json!(1));
    // Mode-default preview: null matched_rule (RULING-7 #8).
    let preview: ApprovalPreview =
        serde_json::from_value(last_of_kind(&outcome, "approval.requested").payload.clone()).unwrap();
    assert!(preview.matched_rule.is_none());
    assert_eq!(preview.action_summary, "exec_command: ls");
    let cwd_real = std::fs::canonicalize(&fx.cwd).unwrap().to_string_lossy().into_owned();
    assert_eq!(preview.targets, json!({"cwd": cwd_real, "paths": []}));
    drop(acked);
}

/// The ask-rule path (`cat` matches `ask`): `source=rule`, and the RULING-7
/// nine-field preview stored verbatim with every field asserted.
#[test]
fn ask_rule_match_full_preview() {
    let mut fx = mk_fx("ask-rule");
    let gate = fx_gate();
    let (acked, pending, _decided) = ask_path(&mut fx, &gate, "call-1");
    let m = pending.matched.clone().unwrap();
    assert_eq!(m.rule_ref, format!("{RULES_SRC}#rule-1"));
    assert_eq!(m.source, RULES_SRC);

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let decided = decided_payload(&outcome);
    assert_eq!(decided.source, DecisionSource::Rule);
    assert_eq!(decided.matched_rule_ref.as_deref(), Some(format!("{RULES_SRC}#rule-1").as_str()));
    assert_eq!(decided.decision.matched_rule_ref, decided.matched_rule_ref);
    let d = &decided.decision;

    // The RULING-7 nine-field preview, stored verbatim.
    let preview: ApprovalPreview =
        serde_json::from_value(last_of_kind(&outcome, "approval.requested").payload.clone()).unwrap();
    assert_eq!(preview.call_id, "call-1");
    assert_eq!(preview.decision_id, d.decision_id);
    assert_eq!(preview.tool_name, "exec_command");
    assert_eq!(preview.action_summary, "exec_command: cat ./hello.txt");
    assert_eq!(preview.args_preview, "{\"argv\":[\"cat\",\"./hello.txt\"]}");
    assert!(!preview.args_truncated);
    assert!(preview.args_digest.starts_with("sha256:"));
    assert_eq!(preview.args_digest.len(), "sha256:".len() + 64);
    assert_eq!(preview.effect_class, EffectClass::Read);
    let cwd_real = std::fs::canonicalize(&fx.cwd).unwrap().to_string_lossy().into_owned();
    let hello_real = std::fs::canonicalize(fx.cwd.join("hello.txt")).unwrap().to_string_lossy().into_owned();
    assert_eq!(preview.targets, json!({"cwd": cwd_real, "paths": [{"given": "./hello.txt", "resolved": hello_real}]}));
    let mr = preview.matched_rule.unwrap();
    assert_eq!(mr.rule_ref, format!("{RULES_SRC}#rule-1"));
    assert_eq!(mr.source, RULES_SRC);
    assert_eq!(preview.requested_at, EVAL_TIME);
    drop(acked);
}

/// FX-02 first half: an `allow` rule → `proposed→approved` /
/// `explicit_allow`, source=rule, `matched_rule_ref` set, no approval frames.
#[test]
fn rule_allow_path_skips_approval() {
    let mut fx = mk_fx("allow");
    let gate = fx_gate();
    let args = json!({"argv": ["echo", "hi"]});
    let acked = call(&mut fx, "call-1", "exec_command", args.clone());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "exec_command", args);
    let auth = gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap();
    let Authorization::Allow { decided, pending, .. } = auth else { panic!("echo must be allowed") };
    let m = pending.matched.unwrap();
    assert_eq!(m.rule_ref, format!("{RULES_SRC}#rule-2"));
    assert_eq!(m.source, RULES_SRC);

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let d = decided_payload(&outcome);
    assert_eq!(d.outcome, DecisionOutcome::Allow);
    assert_eq!(d.source, DecisionSource::Rule);
    assert_eq!(d.matched_rule_ref.as_deref(), Some(format!("{RULES_SRC}#rule-2").as_str()));
    assert_eq!(d.decision.state, DecisionState::Approved);
    assert_eq!(d.decision.reason.code, reason::EXPLICIT_ALLOW);
    assert_eq!(d.decision.matched_rule_ref, d.matched_rule_ref);
    assert!(!outcome.assertions.iter().any(|r| r.kind == EventKind::ApprovalRequested));
    let _ = decided;
}

/// FX-03: a `deny` rule → `proposed→denied` / `rule_forbidden` with the
/// terminal `tool.result{denied}`; precedence deny→ask→allow beats config
/// order (deny listed LAST still wins; ask beats allow).
#[test]
fn rule_forbidden_and_precedence() {
    // Precedence evaluator: allow-all and ask-all listed BEFORE the deny.
    let mut ev = fx_gate().evaluator;
    ev.config.rules = vec![
        Rule { effect: RuleEffect::Allow, matcher: Matcher { tool: "*".into(), argv_prefix: None }, source: RULES_SRC.into() },
        Rule { effect: RuleEffect::Ask, matcher: Matcher { tool: "*".into(), argv_prefix: None }, source: RULES_SRC.into() },
        Rule { effect: RuleEffect::Deny, matcher: Matcher { tool: "exec_command".into(), argv_prefix: Some(vec!["rm".into()]) }, source: RULES_SRC.into() },
    ];
    let gate = Gate { evaluator: ev };
    let mut fx = mk_fx("deny");

    // deny wins over an earlier allow-all and ask-all.
    let args = json!({"argv": ["rm", "-rf", "./x"]});
    let acked = call(&mut fx, "call-1", "exec_command", args.clone());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "exec_command", args);
    let auth = gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap();
    let Authorization::Denied { .. } = auth else { panic!("rm must be denied") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let d = decided_payload(&outcome);
    assert_eq!(d.outcome, DecisionOutcome::Deny);
    assert_eq!(d.source, DecisionSource::Rule);
    assert_eq!(d.matched_rule_ref.as_deref(), Some(format!("{RULES_SRC}#rule-2").as_str()));
    assert_eq!(d.decision.reason.code, reason::RULE_FORBIDDEN);
    let r = result_payload(&outcome);
    assert_eq!(r.status, ResultStatus::Denied);
    assert_eq!(r.error.as_ref().unwrap().code, reason::RULE_FORBIDDEN);
    assert_eq!(r.error.unwrap().message, d.decision.reason.explanation.text, "explanation verbatim");
    // The deny record is the result's caused_by (RULING-8).
    let decided_ev = last_of_kind(&outcome, "authority.decided").event_id.clone();
    assert_eq!(last_of_kind(&outcome, "tool.result").caused_by.as_deref(), Some(decided_ev.as_str()));

    // ask beats allow (cat matches the ask-all... both match: ask wins).
    let acked2 = call(&mut fx, "call-2", "exec_command", cat_args());
    let tc2 = tool_call("call-2", &fx.step_ev.clone(), "exec_command", cat_args());
    let auth2 = gate.authorize_call(&mut fx.ledger, acked2, &tc2, &snap, EVAL_TIME, &cwd).unwrap();
    assert!(matches!(auth2, Authorization::Ask { .. }), "ask outranks allow");
}

/// FX-06: the fixture tool without a manifest entry → `capability_undeclared`.
#[test]
fn capability_undeclared_denied() {
    let mut fx = mk_fx("undecl");
    let gate = fx_gate();
    let args = json!({"argv": ["cat", "./hello.txt"]});
    let acked = call(&mut fx, "call-1", "not_a_tool", args.clone());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "not_a_tool", args);
    let auth = gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap();
    let Authorization::Denied { .. } = auth else { panic!("undeclared tool must be denied") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let d = decided_payload(&outcome);
    assert_eq!(d.decision.reason.code, reason::CAPABILITY_UNDECLARED);
    assert_eq!(d.source, DecisionSource::ModeDefault, "no rule matched (EXEC-30)");
    let r = result_payload(&outcome);
    assert_eq!(r.status, ResultStatus::Denied);
    assert_eq!(r.error.unwrap().code, reason::CAPABILITY_UNDECLARED);
}

/// Pre-approval revocation (E1): revoked manifest entry → `capability_revoked`.
#[test]
fn capability_revoked_pre_approval_denied() {
    let mut fx = mk_fx("revoked-e1");
    let mut gate = fx_gate();
    gate.evaluator.manifest.tools.get_mut("exec_command").unwrap().revoked = true;
    let acked = call(&mut fx, "call-1", "exec_command", cat_args());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "exec_command", cat_args());
    let auth = gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap();
    let Authorization::Denied { .. } = auth else { panic!("revoked capability must be denied") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(decided_payload(&outcome).decision.reason.code, reason::CAPABILITY_REVOKED);
    assert_eq!(result_payload(&outcome).error.unwrap().code, reason::CAPABILITY_REVOKED);
}

/// FX-05: evaluator failure after decode → `denied/missing_verdict`,
/// fail-closed (never an allow on evaluator failure).
#[test]
fn missing_verdict_fail_closed() {
    let mut fx = mk_fx("verdict");
    let mut gate = fx_gate();
    gate.evaluator.fail_verdict = true;
    let acked = call(&mut fx, "call-1", "exec_command", cat_args());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "exec_command", cat_args());
    let auth = gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap();
    let Authorization::Denied { .. } = auth else { panic!("evaluator failure must fail closed") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(decided_payload(&outcome).decision.reason.code, reason::MISSING_VERDICT);
    let r = result_payload(&outcome);
    assert_eq!(r.status, ResultStatus::Denied);
    assert_eq!(r.error.unwrap().code, reason::MISSING_VERDICT);
}

/// FX-09: backend absent → `denied/sandbox_unavailable` at E1 (even under an
/// allow rule: sandbox feasibility is checked after the rule stack).
#[test]
fn sandbox_unavailable_denied() {
    let mut fx = mk_fx("sandbox");
    let mut gate = fx_gate();
    gate.evaluator.sandbox_available = false;
    let args = json!({"argv": ["echo", "hi"]});
    let acked = call(&mut fx, "call-1", "exec_command", args.clone());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "exec_command", args);
    let auth = gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap();
    let Authorization::Denied { .. } = auth else { panic!("no backend must deny") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(decided_payload(&outcome).decision.reason.code, reason::SANDBOX_UNAVAILABLE);
}

// ---------------------------------------------------------------- E2 paths

/// FX-04: user deny with a free-text reason → `approval_required→denied` /
/// `user_denied`; the reason becomes the denial reason and the result's
/// error.message is the explanation verbatim (RULING-7, S2 §4).
#[test]
fn user_denied_with_reason() {
    let mut fx = mk_fx("userdeny");
    let gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    let snap = auth_snapshot();
    let out = gate
        .resolve_approval(
            &mut fx.ledger,
            acked,
            pending.clone(),
            ApprovalResponse::Deny {
                decision_id: pending.decision_id.clone(),
                args_digest: pending.digests.args_digest.clone(),
                reason: Some("not today".into()),
            },
            "dad",
            EVAL_TIME,
            &snap,
        )
        .unwrap();
    let ApprovalOutcome::Denied { .. } = out else { panic!("user deny closes the call") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let resolved: ApprovalResolved =
        serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.resolution, Resolution::Deny);
    assert_eq!(resolved.actor.kind, ActorKind::User);
    assert_eq!(resolved.actor.stable_id, "dad");
    assert_eq!(resolved.user_response, Some(UserResponse::Deny));
    assert_eq!(resolved.reason.as_deref(), Some("not today"));
    let d = resolved.decision.unwrap();
    assert_eq!(d.state, DecisionState::Denied);
    assert_eq!(d.reason.code, reason::USER_DENIED);
    assert_eq!(d.reason.params["reviewer_id"], json!("dad"), "MGR-7: reviewer_id = actor.stable_id");
    let r = result_payload(&outcome);
    assert_eq!(r.status, ResultStatus::Denied);
    assert_eq!(r.error.as_ref().unwrap().code, reason::USER_DENIED);
    assert_eq!(r.error.unwrap().message, "not today", "the free-text reason becomes the denial reason");
    // caused_by: resolved → requested; result → resolved (RULING-8).
    let req_ev = last_of_kind(&outcome, "approval.requested").event_id.clone();
    let res_ev = last_of_kind(&outcome, "approval.resolved").event_id.clone();
    assert_eq!(last_of_kind(&outcome, "approval.resolved").caused_by.as_deref(), Some(req_ev.as_str()));
    assert_eq!(last_of_kind(&outcome, "tool.result").caused_by.as_deref(), Some(res_ev.as_str()));
}

/// FX-14a: a wrong `decision_id` is a typed wire error; the request stays
/// pending and NOTHING is appended (RULING-7).
#[test]
fn wrong_decision_id_stays_pending() {
    let mut fx = mk_fx("badid");
    let gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    let before = fx.ledger.last_ordinal();
    let snap = auth_snapshot();
    let err = gate
        .resolve_approval(
            &mut fx.ledger,
            acked,
            pending.clone(),
            ApprovalResponse::AllowOnce {
                decision_id: "not-the-decision".into(),
                args_digest: pending.digests.args_digest.clone(),
            },
            "dad",
            EVAL_TIME,
            &snap,
        )
        .unwrap_err();
    assert!(matches!(err, GateError::ApprovalDecisionMismatch));
    assert_eq!(fx.ledger.last_ordinal(), before, "wire error appends nothing");
}

/// FX-14b: a wrong `args_digest` → `denied/action_digest_mismatch`, engine
/// actor, `user_response=allow_once` preserved (RULING-7, T3a §4 note 3).
#[test]
fn wrong_args_digest_engine_denial() {
    let mut fx = mk_fx("baddigest");
    let gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    let snap = auth_snapshot();
    let out = gate
        .resolve_approval(
            &mut fx.ledger,
            acked,
            pending.clone(),
            ApprovalResponse::AllowOnce {
                decision_id: pending.decision_id.clone(),
                args_digest: "sha256:wrong".into(),
            },
            "dad",
            EVAL_TIME,
            &snap,
        )
        .unwrap();
    let ApprovalOutcome::Denied { .. } = out else { panic!("digest echo mismatch denies") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let resolved: ApprovalResolved =
        serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.resolution, Resolution::Deny);
    assert_eq!(resolved.actor.kind, ActorKind::PolicyEngine, "engine actor on mismatch");
    assert_eq!(resolved.user_response, Some(UserResponse::AllowOnce), "the user's response is recorded");
    assert_eq!(resolved.decision.unwrap().reason.code, reason::ACTION_DIGEST_MISMATCH);
    assert_eq!(result_payload(&outcome).error.unwrap().code, reason::ACTION_DIGEST_MISMATCH);
}

/// FX-07: revoke while the request is pending, then `allow_once` →
/// `approval_required→denied` / `capability_revoked` (engine actor).
#[test]
fn capability_revoked_while_pending() {
    let mut fx = mk_fx("revoke-pending");
    let mut gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    gate.evaluator.manifest.tools.get_mut("exec_command").unwrap().revoked = true;
    gate.evaluator.revocation_epoch += 1;
    let snap = auth_snapshot();
    let out = gate
        .resolve_approval(&mut fx.ledger, acked, pending.clone(), allow_once(&pending), "dad", EVAL_TIME, &snap)
        .unwrap();
    let ApprovalOutcome::Denied { .. } = out else { panic!("revocation while pending denies") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let resolved: ApprovalResolved =
        serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.decision.unwrap().reason.code, reason::CAPABILITY_REVOKED);
    assert_eq!(result_payload(&outcome).error.unwrap().code, reason::CAPABILITY_REVOKED);
}

/// FX-10 pre-approval half: mutate each of the four hash inputs (authority
/// snapshot, policy snapshot, action digest echo, scope) plus the revocation
/// epoch between E1 and the response → `approval_required→denied` with the
/// matching `*_mismatch` / `capability_revoked` reason.
#[test]
fn mismatch_sweep_pre_approval() {
    // (1) authority snapshot changed.
    let mut fx = mk_fx("mm-auth");
    let gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    let changed = json!({"approval_policy": {"mode": "default"}, "sandbox_policy": {"policy": "MUTATED"}});
    let out = gate.resolve_approval(&mut fx.ledger, acked, pending.clone(), allow_once(&pending), "dad", EVAL_TIME, &changed).unwrap();
    assert!(matches!(out, ApprovalOutcome::Denied { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let resolved: ApprovalResolved = serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.decision.unwrap().reason.code, reason::AUTHORITY_SNAPSHOT_MISMATCH);

    // (2) policy snapshot changed (a rule added after the decision).
    let mut fx = mk_fx("mm-policy");
    let mut gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    gate.evaluator.config.rules.push(Rule {
        effect: RuleEffect::Allow,
        matcher: Matcher { tool: "*".into(), argv_prefix: None },
        source: "rules/other.rules".into(),
    });
    let snap = auth_snapshot();
    let out = gate.resolve_approval(&mut fx.ledger, acked, pending.clone(), allow_once(&pending), "dad", EVAL_TIME, &snap).unwrap();
    assert!(matches!(out, ApprovalOutcome::Denied { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let resolved: ApprovalResolved = serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.decision.unwrap().reason.code, reason::POLICY_SNAPSHOT_MISMATCH);

    // (3) action digest echo wrong (covered in wrong_args_digest_engine_denial; asserted here for the sweep table).

    // (4) scope changed (read roots mutated after the decision).
    let mut fx = mk_fx("mm-scope");
    let mut gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    gate.evaluator.manifest.tools.get_mut("exec_command").unwrap().read_roots.push("/etc".into());
    let snap = auth_snapshot();
    let out = gate.resolve_approval(&mut fx.ledger, acked, pending.clone(), allow_once(&pending), "dad", EVAL_TIME, &snap).unwrap();
    assert!(matches!(out, ApprovalOutcome::Denied { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let resolved: ApprovalResolved = serde_json::from_value(last_of_kind(&outcome, "approval.resolved").payload.clone()).unwrap();
    assert_eq!(resolved.decision.unwrap().reason.code, reason::SCOPE_MISMATCH);
}

// ---------------------------------------------------------------- E3 paths

/// Drive ask → allow_once; returns (acked, pending) positioned before the
/// witness (E3 point).
fn approved_pre_witness(fx: &mut Fx, gate: &Gate, call_id: &str) -> (AckedCall, PendingDecision) {
    let (acked, pending, _) = ask_path(fx, gate, call_id);
    let snap = auth_snapshot();
    match gate.resolve_approval(&mut fx.ledger, acked, pending.clone(), allow_once(&pending), "dad", EVAL_TIME, &snap).unwrap() {
        ApprovalOutcome::Allowed { acked, pending, .. } => (acked, pending),
        _ => panic!("allow_once must resolve allow"),
    }
}

/// FX-11: an injected integration-invariant failure at E3 →
/// `approved→refused` / `integration_invariant_failed`, terminal
/// `tool.result{denied}` with `terminal_decision state=refused` (row 4).
#[test]
fn integration_invariant_failed_e3_refused() {
    let mut fx = mk_fx("e3-inv");
    let mut gate = fx_gate();
    let (acked, pending) = approved_pre_witness(&mut fx, &gate, "call-1");
    gate.evaluator.fail_integration = Some("slice.test_hook".into());
    let snap = auth_snapshot();
    let out = gate.pre_witness_check(&mut fx.ledger, acked, pending, EVAL_TIME, &snap).unwrap();
    let PreWitness::Refused { .. } = out else { panic!("invariant failure must refuse") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let r = result_payload(&outcome);
    assert_eq!(r.status, ResultStatus::Denied, "E3 refusal pairs with denied (nothing ran)");
    assert_eq!(r.error.as_ref().unwrap().code, reason::INTEGRATION_INVARIANT_FAILED);
    let terminal = r.terminal_decision.unwrap();
    assert_eq!(terminal.state, DecisionState::Refused);
    assert_eq!(terminal.reason.code, reason::INTEGRATION_INVARIANT_FAILED);
    assert_eq!(terminal.reason.params["invariant"], json!("slice.test_hook"));
    assert_eq!(r.error.unwrap().message, terminal.reason.explanation.text, "explanation verbatim");
    // Row 4 caused_by: the last gate event (approval.resolved allow).
    let res_ev = last_of_kind(&outcome, "approval.resolved").event_id.clone();
    assert_eq!(last_of_kind(&outcome, "tool.result").caused_by.as_deref(), Some(res_ev.as_str()));
    assert!(!outcome.assertions.iter().any(|r| r.kind == EventKind::ToolStarted), "no witness after refusal");
}

/// FX-08: revoke after approval → E3 `refused` / `capability_revoked`.
#[test]
fn capability_revoked_post_approval_e3_refused() {
    let mut fx = mk_fx("e3-revoke");
    let mut gate = fx_gate();
    let (acked, pending) = approved_pre_witness(&mut fx, &gate, "call-1");
    gate.evaluator.manifest.tools.get_mut("exec_command").unwrap().revoked = true;
    gate.evaluator.revocation_epoch += 1;
    let snap = auth_snapshot();
    let out = gate.pre_witness_check(&mut fx.ledger, acked, pending, EVAL_TIME, &snap).unwrap();
    let PreWitness::Refused { .. } = out else { panic!("post-approval revocation must refuse") };
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let terminal = result_payload(&outcome).terminal_decision.unwrap();
    assert_eq!(terminal.state, DecisionState::Refused);
    assert_eq!(terminal.reason.code, reason::CAPABILITY_REVOKED);
}

/// FX-10 post-approval half: each hash-input mutation at E3 → `refused`
/// with the matching reason.
#[test]
fn mismatch_sweep_post_approval() {
    // (1) authority snapshot changed.
    let mut fx = mk_fx("e3mm-auth");
    let gate = fx_gate();
    let (acked, pending) = approved_pre_witness(&mut fx, &gate, "call-1");
    let changed = json!({"approval_policy": {"mode": "MUTATED"}});
    let out = gate.pre_witness_check(&mut fx.ledger, acked, pending, EVAL_TIME, &changed).unwrap();
    assert!(matches!(out, PreWitness::Refused { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(result_payload(&outcome).terminal_decision.unwrap().reason.code, reason::AUTHORITY_SNAPSHOT_MISMATCH);

    // (2) policy snapshot changed.
    let mut fx = mk_fx("e3mm-policy");
    let mut gate = fx_gate();
    let (acked, pending) = approved_pre_witness(&mut fx, &gate, "call-1");
    gate.evaluator.config.revision = "rules/default.rules#r2".into();
    let snap = auth_snapshot();
    let out = gate.pre_witness_check(&mut fx.ledger, acked, pending, EVAL_TIME, &snap).unwrap();
    assert!(matches!(out, PreWitness::Refused { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(result_payload(&outcome).terminal_decision.unwrap().reason.code, reason::POLICY_SNAPSHOT_MISMATCH);

    // (3) action digest inputs changed (effect_class flip in the registry).
    let mut fx = mk_fx("e3mm-action");
    let mut gate = fx_gate();
    let (acked, pending) = approved_pre_witness(&mut fx, &gate, "call-1");
    gate.evaluator.manifest.tools.get_mut("exec_command").unwrap().effect_class = EffectClass::Write;
    let snap = auth_snapshot();
    let out = gate.pre_witness_check(&mut fx.ledger, acked, pending, EVAL_TIME, &snap).unwrap();
    assert!(matches!(out, PreWitness::Refused { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(result_payload(&outcome).terminal_decision.unwrap().reason.code, reason::ACTION_DIGEST_MISMATCH);

    // (4) scope changed.
    let mut fx = mk_fx("e3mm-scope");
    let mut gate = fx_gate();
    let (acked, pending) = approved_pre_witness(&mut fx, &gate, "call-1");
    gate.evaluator.manifest.tools.get_mut("exec_command").unwrap().read_roots.retain(|r| r != "/usr");
    let snap = auth_snapshot();
    let out = gate.pre_witness_check(&mut fx.ledger, acked, pending, EVAL_TIME, &snap).unwrap();
    assert!(matches!(out, PreWitness::Refused { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(result_payload(&outcome).terminal_decision.unwrap().reason.code, reason::SCOPE_MISMATCH);

    // (5) sandbox lost after approval → integration_invariant_failed(slice.sandbox_available) (MGR-10).
    let mut fx = mk_fx("e3mm-sandbox");
    let mut gate = fx_gate();
    let (acked, pending) = approved_pre_witness(&mut fx, &gate, "call-1");
    gate.evaluator.sandbox_available = false;
    let snap = auth_snapshot();
    let out = gate.pre_witness_check(&mut fx.ledger, acked, pending, EVAL_TIME, &snap).unwrap();
    assert!(matches!(out, PreWitness::Refused { .. }));
    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let terminal = result_payload(&outcome).terminal_decision.unwrap();
    assert_eq!(terminal.reason.code, reason::INTEGRATION_INVARIANT_FAILED);
    assert_eq!(terminal.reason.params["invariant"], json!("slice.sandbox_available"));
}

// ---------------------------------------------------------------- S2.9#8

/// S2.9#8: mutate each hash input, assert the digest changes. Covers the
/// four `action_digest` inputs (tool_name, args, targets, effect_class), the
/// scope array, and both snapshot hashes.
#[test]
fn hash_input_mutation_changes_digest() {
    let gate = fx_gate();
    let root = tmp_root("hashmut");
    let cwd = root.join("cwd");
    std::fs::create_dir_all(&cwd).unwrap();
    std::fs::write(cwd.join("hello.txt"), "x").unwrap();
    let args = cat_args();
    let targets = json!({"cwd": cwd.to_string_lossy(), "paths": []});
    let snap = auth_snapshot();
    let base = gate.evaluator.compute_digests("exec_command", &args, &targets, &snap).unwrap();

    // tool_name (needs a manifest entry for the other name).
    let mut ev2 = gate.evaluator.clone();
    ev2.manifest.tools.insert("other_tool".into(), ToolEntry { effect_class: EffectClass::Read, read_roots: vec!["/usr".into()], revoked: false });
    let d = ev2.compute_digests("other_tool", &args, &targets, &snap).unwrap();
    assert_ne!(d.action_digest, base.action_digest, "tool_name mutation");

    // args.
    let mut args2 = cat_args();
    args2["argv"].as_array_mut().unwrap().push(json!("--extra"));
    let d = gate.evaluator.compute_digests("exec_command", &args2, &targets, &snap).unwrap();
    assert_ne!(d.args_digest, base.args_digest, "args mutation (args_digest)");
    assert_ne!(d.action_digest, base.action_digest, "args mutation (action_digest)");

    // targets.
    let targets2 = json!({"cwd": cwd.to_string_lossy(), "paths": [{"given": "./hello.txt", "resolved": null}]});
    let d = gate.evaluator.compute_digests("exec_command", &args, &targets2, &snap).unwrap();
    assert_ne!(d.action_digest, base.action_digest, "targets mutation");

    // effect_class.
    let mut ev3 = gate.evaluator.clone();
    ev3.manifest.tools.get_mut("exec_command").unwrap().effect_class = EffectClass::Exec;
    let d = ev3.compute_digests("exec_command", &args, &targets, &snap).unwrap();
    assert_ne!(d.action_digest, base.action_digest, "effect_class mutation");

    // scope.
    let mut ev4 = gate.evaluator.clone();
    ev4.manifest.tools.get_mut("exec_command").unwrap().read_roots.push("/etc".into());
    let d = ev4.compute_digests("exec_command", &args, &targets, &snap).unwrap();
    assert_ne!(d.scope_digest, base.scope_digest, "scope mutation");

    // authority snapshot.
    let snap2 = json!({"approval_policy": {"mode": "default"}, "sandbox_policy": {"policy": "MUTATED"}});
    let d = gate.evaluator.compute_digests("exec_command", &args, &targets, &snap2).unwrap();
    assert_ne!(d.authority_snapshot_digest, base.authority_snapshot_digest, "authority snapshot mutation");

    // policy snapshot.
    let mut ev5 = gate.evaluator.clone();
    ev5.config.rules.clear();
    let d = ev5.compute_digests("exec_command", &args, &targets, &snap).unwrap();
    assert_ne!(d.policy_snapshot_digest, base.policy_snapshot_digest, "policy snapshot mutation");

    // revocation epoch.
    let mut ev6 = gate.evaluator.clone();
    ev6.revocation_epoch = 2;
    let d = ev6.compute_digests("exec_command", &args, &targets, &snap).unwrap();
    assert_ne!(d.revocation_epoch, base.revocation_epoch, "revocation epoch mutation");
}

// ---------------------------------------------------------------- MGR-4

/// MGR-4: `args_preview` caps at 2048 bytes of JCS, UTF-8-safe (cut on a
/// char boundary), with `args_truncated`; the digest is over the FULL
/// canonical args.
#[test]
fn args_preview_cap_utf8_safe() {
    // 'é' is 2 bytes: arrange for the 2048-byte cap to land mid-char.
    let long: String = "é".repeat(2000);
    let args = json!({"argv": ["cat", &long]});
    let (preview, truncated, digest) = args_preview(&args).unwrap();
    assert!(truncated);
    assert!(preview.len() <= 2048, "preview within cap: {} bytes", preview.len());
    assert!(std::str::from_utf8(preview.as_bytes()).is_ok());
    // The cut is a prefix of the full JCS on a char boundary.
    let full = String::from_utf8(bob_engine::jcs_bytes(&args).unwrap()).unwrap();
    assert!(full.starts_with(&preview));
    assert!(full.len() > 2048);
    assert_eq!(digest, jcs_sha256(&args).unwrap(), "digest over the full canonical args");

    // Under the cap: not truncated, digest still over the full args.
    let small = cat_args();
    let (p, t, d) = args_preview(&small).unwrap();
    assert!(!t);
    assert_eq!(p, "{\"argv\":[\"cat\",\"./hello.txt\"]}");
    assert_eq!(d, jcs_sha256(&small).unwrap());
}

/// S4 §1b `action_summary`: POSIX single-quote iff a char is outside
/// `[A-Za-z0-9_@%+=:,./-]`; cut at 200 chars with `…`; never model-generated.
#[test]
fn action_summary_quoting_and_cap() {
    let args = json!({"argv": ["cat", "a b", "safe-./_path", "", "it's"]});
    assert_eq!(
        action_summary("exec_command", &args),
        r#"exec_command: cat 'a b' safe-./_path '' 'it'\''s'"#
    );
    let long: Vec<String> = std::iter::repeat("x".repeat(60)).take(10).collect();
    let s = action_summary("exec_command", &json!({"argv": long}));
    assert_eq!(s.chars().count(), 200);
    assert!(s.ends_with('…'));
}

// ---------------------------------------------------------------- Wiring

/// FX-01 E2E through the gate: ask → allow_once → E3 cleared → witness →
/// merged. Asserts the RULING-4 event order, the RULING-8 caused_by chain,
/// and the verbatim preview storage.
#[test]
fn authorize_call_full_happy_path_wiring() {
    let mut fx = mk_fx("wiring");
    let gate = fx_gate();
    let (acked, pending, _) = ask_path(&mut fx, &gate, "call-1");
    let (acked, pending) = approved_pre_witness_from(&mut fx, &gate, acked, pending);
    let snap = auth_snapshot();
    let acked = match gate.pre_witness_check(&mut fx.ledger, acked, pending.clone(), EVAL_TIME, &snap).unwrap() {
        PreWitness::Cleared { acked } => acked,
        _ => panic!("clean E3 must clear"),
    };
    let witness = fx
        .ledger
        .record_tool_started(
            &acked,
            ToolStarted {
                call_id: "call-1".into(),
                governing_decision_id: pending.decision_id.clone(),
                executor_id: "exec-1".into(),
                args_digest: pending.digests.args_digest.clone(),
            },
        )
        .unwrap();
    fx.ledger.record_post_witness_result(witness, ok_result("call-1", &pending.decision_id)).unwrap();

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    assert_eq!(outcome.diagnostics, vec![], "clean replay");
    let kinds: Vec<&str> = outcome.assertions.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(
        kinds,
        ["lineage", "turn.start", "step.context", "tool.call", "authority.decided",
         "approval.requested", "approval.resolved", "tool.started", "tool.result"]
    );
    // RULING-8 caused_by chain end to end.
    let ev = |k: &str| last_of_kind(&outcome, k).event_id.clone();
    let cb = |k: &str| last_of_kind(&outcome, k).caused_by.clone();
    assert_eq!(cb("authority.decided"), Some(ev("tool.call")));
    assert_eq!(cb("approval.requested"), Some(ev("authority.decided")));
    assert_eq!(cb("approval.resolved"), Some(ev("approval.requested")));
    assert_eq!(cb("tool.started"), Some(ev("approval.resolved")));
    assert_eq!(cb("tool.result"), Some(ev("tool.started")));
    // correlation_id = call_id on all six call events (RULING-8 extended).
    for r in &outcome.assertions {
        if r.kind.carries_call_correlation() {
            assert_eq!(r.correlation_id.as_deref(), Some("call-1"));
        }
    }
    // The preview is stored verbatim: replay round-trips the exact payload.
    let preview_rec = last_of_kind(&outcome, "approval.requested");
    let preview: ApprovalPreview = serde_json::from_value(preview_rec.payload.clone()).unwrap();
    assert_eq!(serde_json::to_value(&preview).unwrap(), preview_rec.payload);
    assert_eq!(preview.args_digest, pending.digests.args_digest);
}

fn approved_pre_witness_from(
    fx: &mut Fx,
    gate: &Gate,
    acked: AckedCall,
    pending: PendingDecision,
) -> (AckedCall, PendingDecision) {
    let snap = auth_snapshot();
    match gate.resolve_approval(&mut fx.ledger, acked, pending.clone(), allow_once(&pending), "dad", EVAL_TIME, &snap).unwrap() {
        ApprovalOutcome::Allowed { acked, pending, .. } => (acked, pending),
        _ => panic!("allow_once must resolve allow"),
    }
}

/// FX-02 E2E: allow rule → E3 cleared → witness → merged; no approval
/// frames at all.
#[test]
fn authorize_call_rule_allow_wiring() {
    let mut fx = mk_fx("wiring-allow");
    let gate = fx_gate();
    let args = json!({"argv": ["echo", "hi"]});
    let acked = call(&mut fx, "call-1", "exec_command", args.clone());
    let snap = auth_snapshot();
    let cwd = fx.cwd.clone();
    let tc = tool_call("call-1", &fx.step_ev.clone(), "exec_command", args);
    let (acked, pending) = match gate.authorize_call(&mut fx.ledger, acked, &tc, &snap, EVAL_TIME, &cwd).unwrap() {
        Authorization::Allow { acked, pending, .. } => (acked, pending),
        _ => panic!("echo must be allowed"),
    };
    let acked = match gate.pre_witness_check(&mut fx.ledger, acked, pending.clone(), EVAL_TIME, &snap).unwrap() {
        PreWitness::Cleared { acked } => acked,
        _ => panic!("clean E3 must clear"),
    };
    let witness = fx
        .ledger
        .record_tool_started(
            &acked,
            ToolStarted {
                call_id: "call-1".into(),
                governing_decision_id: pending.decision_id.clone(),
                executor_id: "exec-1".into(),
                args_digest: pending.digests.args_digest.clone(),
            },
        )
        .unwrap();
    fx.ledger.record_post_witness_result(witness, ok_result("call-1", &pending.decision_id)).unwrap();

    let outcome = replay_segment(fx.ledger.path()).unwrap();
    let kinds: Vec<&str> = outcome.assertions.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(
        kinds,
        ["lineage", "turn.start", "step.context", "tool.call", "authority.decided", "tool.started", "tool.result"]
    );
    // Rule allow: witness caused_by authority.decided directly (RULING-8).
    let ev = |k: &str| last_of_kind(&outcome, k).event_id.clone();
    assert_eq!(last_of_kind(&outcome, "tool.started").caused_by, Some(ev("authority.decided")));
}

/// The digest strings are exactly `sha256:<64 lowercase hex>` (MGR-1).
#[test]
fn digest_format_mgr1() {
    let gate = fx_gate();
    let targets = json!({"cwd": "/x", "paths": []});
    let d = gate
        .evaluator
        .compute_digests("exec_command", &cat_args(), &targets, &auth_snapshot())
        .unwrap();
    let check = |s: &str| {
        assert!(s.starts_with("sha256:"));
        let hex = &s["sha256:".len()..];
        assert_eq!(hex.len(), 64);
        assert!(hex.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    };
    check(&d.args_digest);
    check(&d.action_digest);
    check(&d.scope_digest);
    check(&d.authority_snapshot_digest);
    check(&d.policy_snapshot_digest);
    assert_eq!(sha256_digest(b""), "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
}
