//! Authority, approval and witness payload types for the six-event slice
//! vocabulary (RULING-4, T3a S1 §3; RULING-3 as amended by R2).
//!
//! These are ledger record payloads, not an evaluator: the engine records the
//! DecisionRecords it is given and validates internal consistency only
//! (outcome ↔ decision state, source ↔ matched_rule_ref, resolution ↔
//! decision presence/state, actor invariants). No policy evaluation lives
//! here (S2's evaluator is out of slice scope for this crate).

use serde::{Deserialize, Serialize};

/// `DecisionRecord.state` (S2 lifecycle). `Merged`/`Refused` appear only as a
/// `tool.result.terminal_decision`; `Proposed` only as
/// `AuthorityDecided.proposed`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionState {
    Proposed,
    Approved,
    Denied,
    ApprovalRequired,
    Merged,
    Refused,
}

/// Compiled explanation (S2: the reason catalog is the sole explanation
/// source). A `denied` result's `error.message` is this `text` verbatim.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Explanation {
    pub text: String,
}

/// `DecisionRecord.reason`. `code` is carried as a string: the closed S2
/// reason catalog is generated upstream and this crate must not invent it
/// (EXEC-14).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecisionReason {
    pub code: String,
    pub params: serde_json::Value,
    pub explanation: Explanation,
}

/// Actor kinds for authority records (MGR-7).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActorKind {
    PolicyEngine,
    User,
}

/// Actor reference (MGR-7). Engine-caused records use
/// `{policy_engine, "engine"}`; user records use `{user, <stable_id>}`. The
/// reviewer identity of an approval resolution IS `actor.stable_id`, so a
/// `cancelled` resolution — which carries no reviewer — must have an engine
/// actor (RATIFY-3 as amended).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActorRef {
    pub kind: ActorKind,
    pub stable_id: String,
}

/// The S2 DecisionRecord as embedded verbatim in `authority.decided`,
/// `approval.resolved` and `tool.result` (AUD-3, T3a S1 §3). Field set is the
/// one cited by the rulings and repair packages (EXEC-14): `decision_id` is
/// stable across the chain (IN-7); `expires_at` is removed (RULING-2,
/// DEFERRED-EXT-1); `matched_rule_ref` is added (RULING-1).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecisionRecord {
    pub decision_id: String,
    pub state: DecisionState,
    pub reason: DecisionReason,
    pub actor: ActorRef,
    /// RULING-1: present iff the decision came from a rule match.
    pub matched_rule_ref: Option<String>,
    /// Chain link proposed(null) → routed → resolved → terminal (S2 §4).
    /// Carried, not validated, in this crate.
    pub previous_decision_id: Option<String>,
    /// Evaluation clock value (S2 §2: read once per evaluation).
    pub occurred_at: String,
    /// MGR-9: `user` only in the slice.
    pub route: String,
    /// MGR-9: `default` only in the slice.
    pub approval_mode: String,
    /// MGR-9: uuidv4 per call, distinct from `call_id`.
    pub action_id: String,
    pub policy_ref: Option<String>,
}

/// `authority.decided.outcome` (RULING-4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionOutcome {
    Allow,
    Deny,
    Ask,
}

/// `authority.decided.source` (RULING-4). `source=rule` ⇔
/// `matched_rule_ref ≠ null` (T3a S1 §3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionSource {
    Rule,
    ModeDefault,
}

/// `authority.decided` payload (T3a S1 §3): always emitted for every call
/// that enters the gate; embeds two DecisionRecords in one atomic append.
/// `decision_ref` must equal `decision.decision_id`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorityDecided {
    pub outcome: DecisionOutcome,
    pub source: DecisionSource,
    pub matched_rule_ref: Option<String>,
    pub proposed: DecisionRecord,
    pub decision: DecisionRecord,
    pub decision_ref: String,
}

/// RULING-7 / S4 effect classes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectClass {
    Read,
    Write,
    Exec,
    Network,
}

/// `ApprovalPreview.matched_rule`: ref plus source file, or `null` with
/// `mode_default` (RULING-7 #8).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MatchedRulePreview {
    pub rule_ref: String,
    pub source: String,
}

/// `approval.requested` payload = the RULING-7 nine-field `ApprovalPreview`,
/// stored verbatim. The UI renders from this record and never re-derives it
/// (T3a S1 §3). `targets` is carried as a JSON value: its shape is owned by
/// S4 (`{cwd, paths:[{given, resolved|null}]}`, EXEC-14).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApprovalPreview {
    pub call_id: String,
    pub decision_id: String,
    pub tool_name: String,
    /// Engine-derived, deterministic, never model-generated (RULING-7 #4).
    pub action_summary: String,
    pub args_preview: String,
    pub args_truncated: bool,
    /// `sha256:<64 lowercase hex>` of the full canonical args (MGR-1).
    pub args_digest: String,
    pub effect_class: EffectClass,
    pub targets: serde_json::Value,
    pub matched_rule: Option<MatchedRulePreview>,
    pub requested_at: String,
}

/// `approval.resolved.resolution` (RATIFY-3 as amended).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Resolution {
    Allow,
    Deny,
    Cancelled,
}

/// `approval.resolved.user_response` (T3a S1 §3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UserResponse {
    AllowOnce,
    Deny,
}

/// Why a pending approval was cancelled (T3a S1 §3, CP-3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CancelCause {
    TurnCancelled,
    Recovery,
}

/// `approval.resolved` payload (T3a S1 §3, RATIFY-3 as amended). Invariants
/// enforced by the ledger:
/// - `decision` is null ⇔ `resolution = cancelled`.
/// - `cancelled` carries no reviewer: the actor must be the engine, and it is
///   never mapped to `user_denied` (structural: no DecisionRecord exists).
/// - `allow` ⇔ `decision.state = approved`; `deny` ⇔ `decision.state =
///   denied` (derived label, validated — A-19).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApprovalResolved {
    pub resolution: Resolution,
    pub actor: ActorRef,
    pub user_response: Option<UserResponse>,
    pub reason: Option<String>,
    pub cancel_cause: Option<CancelCause>,
    pub decision: Option<DecisionRecord>,
}

/// `tool.started` payload (T3a S1 §3) — the durable start-witness (RULING-5).
/// It is appended and fdatasynced BEFORE any executor side effect and means
/// **authorization to attempt execution only** (I-05b, RATIFY-4): it is not,
/// and may never be described as, proof that execution began.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolStarted {
    pub call_id: String,
    pub governing_decision_id: String,
    pub executor_id: String,
    pub args_digest: String,
}
