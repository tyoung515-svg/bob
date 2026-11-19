//! Slice record payloads (T3a S1 §3, post-ruling). All listed fields are
//! required; nullability as shown. Full enums retained per A-13/A-4
//! recommendation. Each payload serializes to a JSON object via
//! [`serde_json::to_value`].

use serde::{Deserialize, Serialize};

use crate::authority::DecisionRecord;
use crate::envelope::u64_string;
use crate::exec::ExecutorEvidence;

/// `lineage` payload (S1.4.6). Slice uses `New` and `Resume` only; the full
/// operation enum is retained.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Lineage {
    pub operation: LineageOperation,
    pub source_lineage_id: Option<String>,
    pub source_segment_id: Option<String>,
    /// A-2 (SPEC-ERRATA-1): `through_ordinal` is a u64-STRING, identical to
    /// `ordinal`/`call_index`; `null` when the operation pins no prefix. The
    /// reader is strict — a plain JSON number is rejected.
    #[serde(with = "crate::envelope::opt_u64_string")]
    pub through_ordinal: Option<u64>,
    pub through_byte_offset: Option<u64>,
    pub persistence: Option<Persistence>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LineageOperation {
    New,
    Resume,
    Fork,
    Revert,
    Compact,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Persistence {
    Copied,
    Referenced,
}

/// `turn.start` payload (S1.4.1).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TurnStart {
    pub input_digest: String,
    pub parent_turn_id: Option<String>,
}

/// `step.context` payload (S1.4.1). Exactly one begins each step (I-08).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepContext {
    pub model: String,
    pub tool_router_revision: String,
    pub model_visible_tools: Vec<String>,
    pub mcp_bindings: Vec<McpBinding>,
    pub approval_policy: serde_json::Value,
    pub sandbox_policy: serde_json::Value,
    pub settings_revision: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpBinding {
    pub server: String,
    pub binding_revision: String,
}

/// `tool.call` payload (S1.4.2, MGR-5).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCall {
    pub call_id: String,
    pub model_item_id: String,
    #[serde(with = "u64_string")]
    pub call_index: u64,
    pub tool_name: String,
    pub handler_kind: String,
    pub arguments: serde_json::Value,
    pub execution_class: ExecutionClass,
    pub exposure: String,
    /// MGR-5: the `step.context` event id governing this call. The ledger
    /// enforces the match (closes A-5).
    pub authority_snapshot_event_id: String,
    pub dispatch: Dispatch,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionClass {
    Parallel,
    Serial,
}

/// `dispatch` sub-object (S1.4.2). `gate` is the immutable declared class
/// (I-11); `admitted_to_dispatch` is not proof of approval, gate, spawn or
/// invocation (I-09). Invocation knowledge = witness + `start_state` (I-05b).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Dispatch {
    pub disposition: Disposition,
    pub gate: Gate,
    pub executor_id: Option<String>,
    pub reason_code: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Disposition {
    AdmittedToDispatch,
    RejectedBeforeStart,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Gate {
    Shared,
    Exclusive,
    None,
}

/// `tool.result.status` — the five call statuses (RULING-3 as amended by R2,
/// EXT-A10). `not_executed` is removed (T3a I-13).
///
/// Classification rule (RATIFY-1/2 as amended): the durable start-witness
/// separates `aborted` (no witness, never invoked) from {`failed`,
/// `indeterminate`}; a typed executor outcome separates `failed` from
/// `indeterminate`. `indeterminate` is terminal and never auto-retried.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResultStatus {
    Succeeded,
    Failed,
    Denied,
    Aborted,
    Indeterminate,
}

impl ResultStatus {
    pub fn is_success(self) -> bool {
        matches!(self, ResultStatus::Succeeded)
    }

    pub fn as_str(self) -> &'static str {
        match self {
            ResultStatus::Succeeded => "succeeded",
            ResultStatus::Failed => "failed",
            ResultStatus::Denied => "denied",
            ResultStatus::Aborted => "aborted",
            ResultStatus::Indeterminate => "indeterminate",
        }
    }
}

/// Where an `indeterminate` result was closed (RATIFY-1 as amended): either
/// the live closer or recovery may write it, but only when the start-witness
/// is durable and no typed outcome was received.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IndeterminateOrigin {
    Live,
    Recovery,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResultError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StartState {
    NotStarted,
    Started,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelDelivery {
    Deliver,
    Fatal,
    None,
}

/// `tool.result` payload (T3a S1 §3). Exactly one terminal result per call
/// (I-12, I-18 single-assignment closer).
///
/// - `status` ∈ the five RULING-3 statuses.
/// - `effect_ids` is always empty in the slice (D-2; the writer rejects a
///   success with non-empty IDs, I-16).
/// - `terminal_decision` ∈ {merged, refused} only (T3a S1 §3).
/// - `start_state` is executor-evidenced, not witness-derived; `unknown` ⇔
///   `indeterminate`.
/// - `governing_decision_id` is the latest DecisionRecord in the call's
///   lineage including `terminal_decision`; null only when none exists
///   (row 1 / CP-1).
/// - `origin` is required iff `status = indeterminate` (RATIFY-1 as amended).
/// - `recovered_by` carries the resume `lineage` event id for synthesized
///   results (STALE-6); null for live results.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolResult {
    pub call_id: String,
    pub status: ResultStatus,
    pub output: Option<serde_json::Value>,
    pub output_digest: Option<String>,
    pub error: Option<ResultError>,
    pub effect_ids: Vec<String>,
    pub governing_decision_id: Option<String>,
    pub terminal_decision: Option<DecisionRecord>,
    pub start_state: StartState,
    pub model_delivery: ModelDelivery,
    pub origin: Option<IndeterminateOrigin>,
    pub recovered_by: Option<String>,
    /// The MGR-13 executor evidence (S4 §4: the executor half of
    /// `GoverningDecision` rides in `tool.result`). Absent on authority-class
    /// terminals (S4 §2b rows 1-4: the executor never ran,
    /// `not_applicable_to_decision_kind`); present from preflight onward
    /// (rows 5-11). Serialized absent when `None` so pre-S4 records are
    /// byte-identical.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub executor_evidence: Option<ExecutorEvidence>,
}

/// `turn.end` payload (S1.4.5, MGR-3). `open_calls` is owned by the sequencer
/// (I-17/I-19): callers never supply it; [`crate::Ledger`] injects the live
/// index and refuses a success terminal while any call is open.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TurnEnd {
    pub status: TurnStatus,
    pub last_step_id: Option<String>,
    pub error: Option<TurnError>,
    pub open_calls: Vec<String>,
}

/// Turn status reduction (MGR-3, closes A-13): engine/ledger fatal →
/// `failed`; else turn cancelled → `aborted`; else any open call or
/// `indeterminate` result → `incomplete`; else `succeeded`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TurnStatus {
    Succeeded,
    Failed,
    Aborted,
    Incomplete,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TurnError {
    pub code: String,
    pub message: String,
}
