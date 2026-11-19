//! Append-only S1 ledger: one append sequencer (I-04) writing one UTF-8 JSON
//! object plus `\n` per record (I-01) to
//! `$DATA_ROOT/ledgers/<session_id>/segments/<segment_id>.audit.jsonl` (S1.10).
//!
//! Post-ruling slice contract (E2):
//!
//! - **Six-event call vocabulary (RULING-4):** `tool.call` →
//!   `authority.decided` (always) → `approval.requested`/`approval.resolved`
//!   (only on `ask`) → `tool.started` → `tool.result`.
//! - **Durable start-witness (RULING-5, I-05a):** `tool.started` is appended
//!   and fdatasynced BEFORE any executor side effect; its ack is the only gate
//!   to invocation. The witness proves **authorization to attempt execution**,
//!   never that execution began (I-05b, RATIFY-4). A witness-write failure
//!   yields `aborted` and the tool does not run — structurally: no
//!   [`AckedStart`] token is minted, so no post-witness result can be
//!   recorded, and the faulted segment refuses further appends (MGR-2).
//! - **Five call statuses (RULING-3 as amended by R2):** `succeeded`,
//!   `failed`, `denied`, `aborted`, `indeterminate`. The durable witness
//!   separates `aborted` from {`failed`, `indeterminate`}; a typed executor
//!   outcome separates `failed` from `indeterminate` (RATIFY-2). The API makes
//!   the boundary structural: pre-witness results (consume [`AckedCall`]) can
//!   only be `denied`/`aborted`/`failed`(row 1); post-witness results
//!   (consume [`AckedStart`]) can only be `succeeded`/`failed`/`indeterminate`.
//! - **`caused_by` causal chain (RULING-8, overriding E1's EXEC-5):** every
//!   event points at its immediate causal predecessor; `correlation_id` =
//!   `call_id` on all six call events.
//! - **Torn tail (RULING-6):** writable resume refuses with typed
//!   [`LedgerError::LedgerTailCorrupt`] (byte offset + last valid ordinal);
//!   read-only replay surfaces it. No auto-truncate, no fuse, no synthesized
//!   gap (repair = DEFERRED-EXT-2).

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use time::OffsetDateTime;
use uuid::Uuid;

use crate::authority::{
    ActorKind, ApprovalPreview, ApprovalResolved, AuthorityDecided, DecisionOutcome, DecisionSource,
    DecisionState, Resolution, ToolStarted,
};
use crate::envelope::{Actor, EventKind, Record, SCHEMA_VERSION};
use crate::payload::{
    Disposition, IndeterminateOrigin, Lineage, LineageOperation, ResultStatus,
    StartState, StepContext, ToolCall, ToolResult, TurnEnd, TurnError, TurnStart, TurnStatus,
};
use crate::replay::{is_valid_timestamp, replay_segment, ReplayDiagnostic, TS_FORMAT};

/// Ordinals are base 1 (RULING-8 ratified; the ordinal is the sole ordering
/// authority — timestamps never order events).
pub const ORDINAL_BASE: u64 = 1;

/// A-1 (SPEC-ERRATA-1): the typed diagnostic code for "this platform has no
/// directory fsync". Emitted exactly once per segment creation where the
/// durability step cannot be performed, so the gap is recorded instead of
/// silently claiming the guarantee.
pub const DIR_SYNC_UNAVAILABLE: &str = "dir_sync_unavailable";

/// A typed diagnostic attached to segment creation (A-1, MGR-2/IMPL-1).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CreationDiagnostic {
    /// A-1: the creating platform cannot fsync a directory, so the segment
    /// file's own write+flush+`sync_data` boundary (MGR-2) is intact but the
    /// directory entry that names the new segment is not additionally
    /// fsynced. Carries the platform label so the gap is never silent.
    DirSyncUnavailable { platform: &'static str },
}

impl CreationDiagnostic {
    /// The wire/diagnostic code (A-1). One code, one owner: the same string
    /// the errata names.
    pub fn code(&self) -> &'static str {
        DIR_SYNC_UNAVAILABLE
    }
}

impl std::fmt::Display for CreationDiagnostic {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CreationDiagnostic::DirSyncUnavailable { platform } => write!(
                f,
                "{DIR_SYNC_UNAVAILABLE}: directory fsync is unavailable on \
                 platform '{platform}'; the segment file is fdatasynced per \
                 record (MGR-2) but its directory entry is not fsynced (A-1)"
            ),
        }
    }
}

/// fsync a directory (POSIX). Opening a directory read-only and calling
/// `sync_all` issues `fsync(2)` on it, which is what makes a freshly created
/// file's directory entry durable.
#[cfg(unix)]
fn sync_dir(path: &Path) -> std::io::Result<()> {
    let dir = std::fs::File::open(path)?;
    dir.sync_all()
}

/// Public POSIX helper (A-1): fsync a directory, exposed so the durability
/// step is directly testable on POSIX hosts.
#[cfg(unix)]
pub fn sync_directory(path: &Path) -> std::io::Result<()> {
    sync_dir(path)
}

/// The ancestors of `parent` that do not exist yet, topmost first — the
/// directories this call is about to create (MGR-2: fsync every directory
/// created in this call, then the parent of the topmost).
fn missing_ancestors(parent: &Path) -> Vec<PathBuf> {
    let mut missing = Vec::new();
    let mut cur = Some(parent);
    while let Some(p) = cur {
        if p.exists() {
            break;
        }
        missing.push(p.to_path_buf());
        cur = p.parent();
    }
    missing.reverse();
    missing
}

/// A-1: make the new segment's directory entry durable before the first
/// record is acknowledged. POSIX: fsync the containing directory (and every
/// directory this call created, topmost first); a real fsync error is
/// fail-closed (MGR-2 — no weaker durability claim). Platforms without
/// directory fsync record the gap as a typed [`CreationDiagnostic`].
fn sync_created_dirs(parent: &Path, created: &[PathBuf]) -> Result<Vec<CreationDiagnostic>, LedgerError> {
    #[cfg(unix)]
    {
        for dir in created {
            sync_dir(dir)?;
        }
        // The containing directory itself, if it predated this call (when it
        // was created above it is already the deepest entry of `created`).
        if !created.iter().any(|d| d == parent) {
            sync_dir(parent)?;
        }
        Ok(Vec::new())
    }
    #[cfg(not(unix))]
    {
        let _ = (parent, created);
        Ok(vec![CreationDiagnostic::DirSyncUnavailable {
            platform: std::env::consts::OS,
        }])
    }
}

/// Identity set stamped onto every record of one segment.
#[derive(Debug, Clone)]
pub struct SessionIds {
    pub session_id: String,
    pub thread_id: String,
    pub lineage_id: String,
    pub segment_id: String,
}

/// Proof that one record is durable: the full line plus `\n` was written and
/// `fdatasync`ed before this value existed (MGR-2 ack).
#[derive(Debug, Clone)]
pub struct DurableAck {
    pub segment_id: String,
    pub ordinal: u64,
    pub event_id: String,
}

/// Token proving a specific `tool.call` is durably appended. The gate events
/// and the pre-witness terminal result are recorded against it. Not `Clone`.
#[derive(Debug)]
pub struct AckedCall {
    call_id: String,
    disposition: Disposition,
    ack: DurableAck,
}

impl AckedCall {
    pub fn call_id(&self) -> &str {
        &self.call_id
    }
    pub fn disposition(&self) -> Disposition {
        self.disposition
    }
    /// The durable ack of this call's `tool.call` record.
    pub fn ack(&self) -> &DurableAck {
        &self.ack
    }
}

/// Token proving the durable start-witness (`tool.started`) for a call: the
/// call is authorized to attempt execution (I-05b — NOT proof that execution
/// began). The only way to record a `succeeded`, post-witness `failed` or
/// live `indeterminate` result is to consume this token, so those statuses
/// can never be witnessed for a call whose start-witness is not durable
/// (RATIFY-2). Not `Clone`: one token, one result (I-12).
#[derive(Debug)]
pub struct AckedStart {
    call_id: String,
    witness: DurableAck,
}

impl AckedStart {
    pub fn call_id(&self) -> &str {
        &self.call_id
    }
    /// The durable ack of this call's `tool.started` witness.
    pub fn witness(&self) -> &DurableAck {
        &self.witness
    }
}

/// Typed, fail-closed ledger errors. No ledger API panics on bad input.
#[derive(Debug, thiserror::Error)]
pub enum LedgerError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("timestamp formatting failed: {0}")]
    TimestampFormat(String),
    #[error("id fields must be non-empty")]
    EmptyId,
    #[error("payload for kind '{kind}' must be a JSON object")]
    PayloadNotObject { kind: String },
    #[error("duplicate call_id '{0}' (I-17)")]
    DuplicateCallId(String),
    #[error("unknown call_id '{0}'")]
    UnknownCallId(String),
    #[error("call '{0}' already has a terminal tool.result (I-12/I-18)")]
    CallAlreadyClosed(String),
    #[error("record call_id '{record}' does not match acked call '{acked}'")]
    CallIdMismatch { acked: String, record: String },
    #[error("step.context requires a prior turn.start for turn '{turn_id}' (MGR-5)")]
    MissingTurnStart { turn_id: String },
    #[error("tool.call requires a prior step.context for turn '{turn_id}' step '{step_id}' (I-08/MGR-5)")]
    MissingStepContext { turn_id: String, step_id: String },
    #[error(
        "tool.call authority_snapshot_event_id '{found}' must be the governing step.context event id '{expected}' (MGR-5)"
    )]
    AuthoritySnapshotMismatch { expected: String, found: String },
    #[error("call '{0}' already has authority.decided (one gate decision per call)")]
    DecisionAlreadyRecorded(String),
    #[error("invalid authority.decided: {0}")]
    InvalidAuthorityDecided(String),
    #[error("approval.requested requires outcome=ask for call '{0}' (RULING-4)")]
    ApprovalOutcomeNotAsk(String),
    #[error("call '{0}' already has approval.requested")]
    ApprovalAlreadyRequested(String),
    #[error("approval.resolved requires a pending approval.requested for call '{0}'")]
    ApprovalNotRequested(String),
    #[error("call '{0}' already has approval.resolved")]
    ApprovalAlreadyResolved(String),
    #[error("invalid approval.requested: {0}")]
    InvalidApprovalRequested(String),
    #[error("invalid approval.resolved: {0}")]
    InvalidApprovalResolved(String),
    #[error("tool.started without authorization for call '{0}' (needs outcome=allow or resolution=allow)")]
    NotAuthorized(String),
    #[error("call '{0}' already has a durable start-witness")]
    WitnessAlreadyDurable(String),
    #[error("invalid tool.started: {0}")]
    InvalidToolStarted(String),
    #[error("status {status} requires the durable start-witness for call '{call_id}'; use the post-witness path (RATIFY-2)")]
    WitnessRequired { call_id: String, status: String },
    #[error("invalid tool.result: {0}")]
    InvalidToolResult(String),
    #[error("failed requires a typed executor outcome (error.code) for call '{0}' (RATIFY-2)")]
    FailedRequiresTypedError(String),
    #[error("indeterminate requires origin live|recovery for call '{0}' (RATIFY-1)")]
    IndeterminateRequiresOrigin(String),
    #[error("indeterminate is never auto-retried: error.retryable must be false for call '{0}' (RULING-3)")]
    IndeterminateNotRetryable(String),
    #[error("denied requires a durable authority refusal in the lineage of call '{0}' (S1 §4a rows 2-4)")]
    DeniedWithoutRefusal(String),
    #[error("denied error.message must be the governing explanation.text verbatim: expected '{expected}', found '{found}' (S2 §4)")]
    DeniedExplanationMismatch { expected: String, found: String },
    #[error("status=succeeded requires start_state=started (I-14)")]
    SuccessRequiresStarted,
    #[error("status=succeeded requires terminal_decision state=merged (I-14, T3a S1 §3)")]
    SuccessRequiresMergedTerminal,
    #[error("status=succeeded requires output_digest = merged.reason.params.target_id (T3a S1 §3)")]
    SuccessOutputDigestMismatch,
    #[error("start_state=unknown is permitted only on an indeterminate result (T3a S1 §3)")]
    UnknownStartStateNotIndeterminate,
    #[error(
        "success is invalid while any effect id is referenced and no durable effect exists (I-16; slice defers `effect`, D-2)"
    )]
    SuccessWithUnresolvedEffects,
    #[error("rejected_before_start requires status=failed and start_state=not_started (S1 §4a row 1)")]
    RejectedRequiresFailedNotStarted,
    #[error("turn.end with open calls requires a non-success status (I-19); open: {0:?}")]
    OpenCallsOnSuccess(Vec<String>),
    #[error("segment is faulted: a prior append/fsync failed; no further appends or invocations (MGR-2)")]
    SegmentFaulted,
    #[error("ledger_tail_corrupt: final line fails parse/validation at byte offset {byte_offset} (last valid ordinal {last_ordinal:?}); writable resume refused, no auto-truncate/fuse (RULING-6)")]
    LedgerTailCorrupt {
        byte_offset: u64,
        last_ordinal: Option<u64>,
    },
    #[error("ledger_corrupt: mid-file corruption; writable resume refused (I-23/MGR-6)")]
    LedgerCorrupt,
    #[error("segment replay found duplicate or regressing ordinal; refusing writable resume")]
    OrdinalNotMonotonic,
}

/// Per-call durable-evidence state, maintained live and rebuilt on resume.
#[derive(Debug)]
struct CallState {
    turn_id: String,
    step_id: String,
    disposition: Disposition,
    call_event_id: String,
    decided: Option<DecidedState>,
    requested_event_id: Option<String>,
    resolved: Option<ResolvedState>,
    started_event_id: Option<String>,
    closed: bool,
}

#[derive(Debug, Clone)]
struct DecidedState {
    event_id: String,
    outcome: DecisionOutcome,
    decision_id: String,
    reason_code: String,
    explanation_text: String,
}

#[derive(Debug, Clone)]
struct ResolvedState {
    event_id: String,
    resolution: Resolution,
    decision_id: Option<String>,
    reason_code: Option<String>,
    explanation_text: Option<String>,
}

impl CallState {
    /// The last durable event of the call, in chain order (RULING-8:
    /// `aborted` results point here).
    fn last_durable_event(&self) -> &str {
        if let Some(s) = &self.started_event_id {
            return s;
        }
        if let Some(r) = &self.resolved {
            return &r.event_id;
        }
        if let Some(req) = &self.requested_event_id {
            return req;
        }
        if let Some(d) = &self.decided {
            return &d.event_id;
        }
        &self.call_event_id
    }

    /// The latest DecisionRecord id in the call's lineage (excluding any
    /// terminal decision, which riders add themselves).
    fn latest_decision_id(&self) -> Option<&str> {
        if let Some(r) = &self.resolved {
            if let Some(id) = &r.decision_id {
                return Some(id);
            }
        }
        if let Some(d) = &self.decided {
            return Some(&d.decision_id);
        }
        None
    }

    /// The last gate event (`approval.resolved` or `authority.decided`).
    fn last_gate_event(&self) -> Option<&str> {
        if let Some(r) = &self.resolved {
            return Some(&r.event_id);
        }
        self.decided.as_ref().map(|d| d.event_id.as_str())
    }

    /// The durable authority refusal in this call's lineage, if any:
    /// (event_id, reason_code, explanation_text, decision_id).
    fn deny_record(&self) -> Option<(&str, &str, &str, &str)> {
        if let Some(d) = &self.decided {
            if d.outcome == DecisionOutcome::Deny {
                return Some((&d.event_id, &d.reason_code, &d.explanation_text, &d.decision_id));
            }
        }
        if let Some(r) = &self.resolved {
            if r.resolution == Resolution::Deny {
                if let (Some(code), Some(text), Some(id)) =
                    (&r.reason_code, &r.explanation_text, &r.decision_id)
                {
                    return Some((&r.event_id, code, text, id));
                }
            }
        }
        None
    }
}

/// The ONE append sequencer for a segment (I-04). `&mut self` on every append
/// serializes all producers through it.
pub struct Ledger {
    file: File,
    path: PathBuf,
    ids: SessionIds,
    next_ordinal: u64,
    /// Sequencer-owned per-call index (I-17). A call opens at durable
    /// `tool.call` and closes at durable `tool.result`.
    calls: BTreeMap<String, CallState>,
    /// MGR-5 chain state: `turn.start` event per turn, `step.context` event
    /// per (turn, step), and the last durable event per turn (for
    /// `turn.end.caused_by`).
    turn_start_event: BTreeMap<String, String>,
    step_context_event: BTreeMap<(String, String), String>,
    turn_last_event: BTreeMap<String, String>,
    /// The `lineage{resume}` event id of this writable session, if any:
    /// `recovered_by` of synthesized results (STALE-6).
    resume_event_id: Option<String>,
    /// MGR-2: any append/fsync error faults the segment — no further append
    /// or invocation, live calls go to the closer.
    faulted: bool,
    /// Test hook: make the next append fail before write (see
    /// [`Ledger::debug_fail_next_append`]).
    fail_next_append: bool,
    fail_next_sync: bool,
    /// A-1 creation diagnostics (exactly one per segment creation when the
    /// platform has no directory fsync; empty otherwise).
    creation_diagnostics: Vec<CreationDiagnostic>,
}

impl Ledger {
    /// Create a new segment file and append `lineage{new}` as its first record
    /// (slice contract §1: `new` as first record). Fails if the file exists.
    pub fn open_new(data_root: &Path, ids: SessionIds) -> Result<(Ledger, DurableAck), LedgerError> {
        check_ids(&ids)?;
        let path = segment_path(data_root, &ids);
        let mut created_dirs: Vec<PathBuf> = Vec::new();
        if let Some(parent) = path.parent() {
            created_dirs = missing_ancestors(parent);
            std::fs::create_dir_all(parent)?;
        }
        let file = OpenOptions::new().write(true).create_new(true).open(&path)?;
        // A-1: the directory entry that names the new segment is made durable
        // BEFORE the first record is acknowledged. Where the platform has no
        // directory fsync the gap is recorded as a typed diagnostic, never
        // silently claimed.
        let creation_diagnostics = match path.parent() {
            Some(parent) => sync_created_dirs(parent, &created_dirs)?,
            None => Vec::new(),
        };
        let mut ledger = Ledger {
            file,
            path,
            ids,
            next_ordinal: ORDINAL_BASE,
            calls: BTreeMap::new(),
            turn_start_event: BTreeMap::new(),
            step_context_event: BTreeMap::new(),
            turn_last_event: BTreeMap::new(),
            resume_event_id: None,
            faulted: false,
            fail_next_append: false,
            fail_next_sync: false,
            creation_diagnostics,
        };
        let ack = ledger.append_lineage(Lineage {
            operation: LineageOperation::New,
            source_lineage_id: None,
            source_segment_id: None,
            through_ordinal: None,
            through_byte_offset: None,
            persistence: None,
        })?;
        Ok((ledger, ack))
    }

    /// Open an existing segment for writable resume (I-20 step 0/1): replay it
    /// read-only, rebuild the per-call durable-evidence index (I-17, §4b),
    /// then append `lineage{resume}`. RULING-6: a corrupt final line or torn
    /// tail refuses with typed `ledger_tail_corrupt` (byte offset + last valid
    /// ordinal); mid-file corruption refuses as `ledger_corrupt`; no
    /// auto-truncate, no fuse.
    pub fn open_resume(data_root: &Path, ids: SessionIds) -> Result<(Ledger, DurableAck), LedgerError> {
        check_ids(&ids)?;
        let path = segment_path(data_root, &ids);
        let outcome = replay_segment(&path)?;
        for diag in &outcome.diagnostics {
            match diag {
                ReplayDiagnostic::LedgerTailCorrupt {
                    byte_offset,
                    last_ordinal,
                } => {
                    return Err(LedgerError::LedgerTailCorrupt {
                        byte_offset: *byte_offset,
                        last_ordinal: *last_ordinal,
                    })
                }
                ReplayDiagnostic::OrdinalDuplicate { .. }
                | ReplayDiagnostic::OrdinalRegression { .. } => {
                    return Err(LedgerError::OrdinalNotMonotonic)
                }
                ReplayDiagnostic::CorruptLine { .. }
                | ReplayDiagnostic::PayloadNotObject { .. } => {
                    return Err(LedgerError::LedgerCorrupt)
                }
                _ => {}
            }
        }
        let mut max_ordinal = ORDINAL_BASE - 1;
        let mut calls: BTreeMap<String, CallState> = BTreeMap::new();
        let mut turn_start_event: BTreeMap<String, String> = BTreeMap::new();
        let mut step_context_event: BTreeMap<(String, String), String> = BTreeMap::new();
        let mut turn_last_event: BTreeMap<String, String> = BTreeMap::new();
        for rec in &outcome.assertions {
            max_ordinal = max_ordinal.max(rec.ordinal);
            if let Some(t) = &rec.turn_id {
                turn_last_event.insert(t.clone(), rec.event_id.clone());
            }
            let turn_id = rec.turn_id.clone().unwrap_or_default();
            let step_id = rec.step_id.clone().unwrap_or_default();
            match rec.kind {
                EventKind::TurnStart => {
                    turn_start_event.insert(turn_id, rec.event_id.clone());
                }
                EventKind::StepContext => {
                    step_context_event.insert((turn_id, step_id), rec.event_id.clone());
                }
                EventKind::ToolCall => {
                    let call: ToolCall = serde_json::from_value(rec.payload.clone())
                        .map_err(|_| LedgerError::LedgerCorrupt)?;
                    calls.insert(
                        call.call_id.clone(),
                        CallState {
                            turn_id,
                            step_id,
                            disposition: call.dispatch.disposition,
                            call_event_id: rec.event_id.clone(),
                            decided: None,
                            requested_event_id: None,
                            resolved: None,
                            started_event_id: None,
                            closed: false,
                        },
                    );
                }
                EventKind::AuthorityDecided => {
                    let decided: AuthorityDecided = serde_json::from_value(rec.payload.clone())
                        .map_err(|_| LedgerError::LedgerCorrupt)?;
                    let call_id = rec
                        .correlation_id
                        .clone()
                        .ok_or(LedgerError::LedgerCorrupt)?;
                    let state = calls.get_mut(&call_id).ok_or(LedgerError::LedgerCorrupt)?;
                    state.decided = Some(DecidedState {
                        event_id: rec.event_id.clone(),
                        outcome: decided.outcome,
                        decision_id: decided.decision.decision_id.clone(),
                        reason_code: decided.decision.reason.code.clone(),
                        explanation_text: decided.decision.reason.explanation.text.clone(),
                    });
                }
                EventKind::ApprovalRequested => {
                    let call_id = rec
                        .correlation_id
                        .clone()
                        .ok_or(LedgerError::LedgerCorrupt)?;
                    let state = calls.get_mut(&call_id).ok_or(LedgerError::LedgerCorrupt)?;
                    state.requested_event_id = Some(rec.event_id.clone());
                }
                EventKind::ApprovalResolved => {
                    let resolved: ApprovalResolved = serde_json::from_value(rec.payload.clone())
                        .map_err(|_| LedgerError::LedgerCorrupt)?;
                    let call_id = rec
                        .correlation_id
                        .clone()
                        .ok_or(LedgerError::LedgerCorrupt)?;
                    let state = calls.get_mut(&call_id).ok_or(LedgerError::LedgerCorrupt)?;
                    state.resolved = Some(ResolvedState {
                        event_id: rec.event_id.clone(),
                        resolution: resolved.resolution,
                        decision_id: resolved
                            .decision
                            .as_ref()
                            .map(|d| d.decision_id.clone()),
                        reason_code: resolved
                            .decision
                            .as_ref()
                            .map(|d| d.reason.code.clone()),
                        explanation_text: resolved
                            .decision
                            .as_ref()
                            .map(|d| d.reason.explanation.text.clone()),
                    });
                }
                EventKind::ToolStarted => {
                    let call_id = rec
                        .correlation_id
                        .clone()
                        .ok_or(LedgerError::LedgerCorrupt)?;
                    let state = calls.get_mut(&call_id).ok_or(LedgerError::LedgerCorrupt)?;
                    state.started_event_id = Some(rec.event_id.clone());
                }
                EventKind::ToolResult => {
                    let call_id = rec
                        .correlation_id
                        .clone()
                        .ok_or(LedgerError::LedgerCorrupt)?;
                    if let Some(state) = calls.get_mut(&call_id) {
                        state.closed = true;
                    }
                }
                _ => {}
            }
        }
        let through_byte_offset = std::fs::metadata(&path)?.len();
        let file = OpenOptions::new().append(true).open(&path)?;
        let mut ledger = Ledger {
            file,
            path,
            ids,
            next_ordinal: max_ordinal + 1,
            calls,
            turn_start_event,
            step_context_event,
            turn_last_event,
            resume_event_id: None,
            faulted: false,
            fail_next_append: false,
            fail_next_sync: false,
            // A-1 is a CREATE-time step: a resumed segment was created by an
            // earlier call, so this call reports no creation diagnostic.
            creation_diagnostics: Vec::new(),
        };
        // Resume keeps lineage_id/segment_id (S1.2 implication). The resume
        // payload's source fields name this same lineage; `through_*` pin the
        // replayed prefix. `persistence` stays null (nothing copied).
        let lineage_id = ledger.ids.lineage_id.clone();
        let segment_id = ledger.ids.segment_id.clone();
        let ack = ledger.append_lineage(Lineage {
            operation: LineageOperation::Resume,
            source_lineage_id: Some(lineage_id),
            source_segment_id: Some(segment_id),
            through_ordinal: if max_ordinal >= ORDINAL_BASE {
                Some(max_ordinal)
            } else {
                None
            },
            through_byte_offset: Some(through_byte_offset),
            persistence: None,
        })?;
        ledger.resume_event_id = Some(ack.event_id.clone());
        Ok((ledger, ack))
    }

    /// Append `turn.start` (turn-level: `step_id` is null — no step exists
    /// yet). `caused_by` is null (MGR-5).
    pub fn append_turn_start(
        &mut self,
        turn_id: &str,
        payload: TurnStart,
    ) -> Result<DurableAck, LedgerError> {
        check_nonempty(turn_id)?;
        let ack = self.append(
            EventKind::TurnStart,
            Actor::Engine,
            Some(turn_id),
            None,
            None,
            None,
            serde_json::to_value(payload)?,
        )?;
        self.turn_start_event
            .insert(turn_id.to_owned(), ack.event_id.clone());
        Ok(ack)
    }

    /// Append `step.context`. Exactly one begins each step, before any call in
    /// it (I-08). `caused_by` is the `turn.start` event (MGR-5); the turn must
    /// have been started first.
    pub fn append_step_context(
        &mut self,
        turn_id: &str,
        step_id: &str,
        payload: StepContext,
    ) -> Result<DurableAck, LedgerError> {
        check_nonempty(turn_id)?;
        check_nonempty(step_id)?;
        let turn_start = self
            .turn_start_event
            .get(turn_id)
            .cloned()
            .ok_or_else(|| LedgerError::MissingTurnStart {
                turn_id: turn_id.to_owned(),
            })?;
        let ack = self.append(
            EventKind::StepContext,
            Actor::Engine,
            Some(turn_id),
            Some(step_id),
            None,
            Some(&turn_start),
            serde_json::to_value(payload)?,
        )?;
        self.step_context_event.insert(
            (turn_id.to_owned(), step_id.to_owned()),
            ack.event_id.clone(),
        );
        Ok(ack)
    }

    /// Durably append `tool.call` BEFORE the call's execution future is
    /// spawned or its handler invoked (I-05). `caused_by` is the governing
    /// `step.context` event (MGR-5), and the payload's
    /// `authority_snapshot_event_id` must equal it (closes A-5).
    /// `correlation_id` = `call_id` (RULING-8). Returns the [`AckedCall`]
    /// token; an admitted call enters the sequencer-owned open-calls index
    /// (I-17).
    pub fn append_tool_call(
        &mut self,
        turn_id: &str,
        step_id: &str,
        payload: ToolCall,
    ) -> Result<AckedCall, LedgerError> {
        check_nonempty(turn_id)?;
        check_nonempty(step_id)?;
        check_nonempty(&payload.call_id)?;
        if self.calls.contains_key(&payload.call_id) {
            return Err(LedgerError::DuplicateCallId(payload.call_id));
        }
        let step_ctx = self
            .step_context_event
            .get(&(turn_id.to_owned(), step_id.to_owned()))
            .cloned()
            .ok_or_else(|| LedgerError::MissingStepContext {
                turn_id: turn_id.to_owned(),
                step_id: step_id.to_owned(),
            })?;
        if payload.authority_snapshot_event_id != step_ctx {
            return Err(LedgerError::AuthoritySnapshotMismatch {
                expected: step_ctx,
                found: payload.authority_snapshot_event_id.clone(),
            });
        }
        let disposition = payload.dispatch.disposition;
        let call_id = payload.call_id.clone();
        let ack = self.append(
            EventKind::ToolCall,
            Actor::Engine,
            Some(turn_id),
            Some(step_id),
            Some(&call_id),
            Some(&step_ctx),
            serde_json::to_value(payload)?,
        )?;
        self.calls.insert(
            call_id.clone(),
            CallState {
                turn_id: turn_id.to_owned(),
                step_id: step_id.to_owned(),
                disposition,
                call_event_id: ack.event_id.clone(),
                decided: None,
                requested_event_id: None,
                resolved: None,
                started_event_id: None,
                closed: false,
            },
        );
        Ok(AckedCall {
            call_id,
            disposition,
            ack,
        })
    }

    /// Append `authority.decided` for a call (RULING-4: always emitted for a
    /// call that enters the gate). One atomic append embedding `proposed` and
    /// `decision`. `caused_by` = the `tool.call` event (RULING-8). Consistency
    /// enforced (T3a S1 §3): `decision_ref` = `decision.decision_id`;
    /// outcome ↔ `decision.state` (allow ⇔ approved, deny ⇔ denied, ask ⇔
    /// approval_required); `source=rule` ⇔ `matched_rule_ref ≠ null`.
    pub fn record_authority_decided(
        &mut self,
        acked: &AckedCall,
        payload: AuthorityDecided,
    ) -> Result<DurableAck, LedgerError> {
        let call_id = acked.call_id.clone();
        let state = self.call_state(&call_id)?;
        if state.closed {
            return Err(LedgerError::CallAlreadyClosed(call_id));
        }
        if state.decided.is_some() {
            return Err(LedgerError::DecisionAlreadyRecorded(call_id));
        }
        validate_authority_decided(&payload)?;
        let caused_by = state.call_event_id.clone();
        let ack = self.append(
            EventKind::AuthorityDecided,
            Actor::Engine,
            Some(&state.turn_id.clone()),
            Some(&state.step_id.clone()),
            Some(&call_id),
            Some(&caused_by),
            serde_json::to_value(&payload)?,
        )?;
        self.call_state_mut(&call_id)?.decided = Some(DecidedState {
            event_id: ack.event_id.clone(),
            outcome: payload.outcome,
            decision_id: payload.decision.decision_id.clone(),
            reason_code: payload.decision.reason.code.clone(),
            explanation_text: payload.decision.reason.explanation.text.clone(),
        });
        Ok(ack)
    }

    /// Append `approval.requested` = the RULING-7 `ApprovalPreview`, stored
    /// verbatim (only on `ask`). `caused_by` = the `authority.decided` event.
    pub fn record_approval_requested(
        &mut self,
        acked: &AckedCall,
        payload: ApprovalPreview,
    ) -> Result<DurableAck, LedgerError> {
        let call_id = acked.call_id.clone();
        let state = self.call_state(&call_id)?;
        if state.closed {
            return Err(LedgerError::CallAlreadyClosed(call_id));
        }
        let decided = state
            .decided
            .as_ref()
            .ok_or_else(|| LedgerError::ApprovalOutcomeNotAsk(call_id.clone()))?;
        if decided.outcome != DecisionOutcome::Ask {
            return Err(LedgerError::ApprovalOutcomeNotAsk(call_id));
        }
        if state.requested_event_id.is_some() {
            return Err(LedgerError::ApprovalAlreadyRequested(call_id));
        }
        if payload.call_id != call_id {
            return Err(LedgerError::InvalidApprovalRequested(format!(
                "preview call_id '{}' != '{}'",
                payload.call_id, call_id
            )));
        }
        if payload.decision_id != decided.decision_id {
            return Err(LedgerError::InvalidApprovalRequested(format!(
                "preview decision_id '{}' must be the decided decision '{}'",
                payload.decision_id, decided.decision_id
            )));
        }
        let caused_by = decided.event_id.clone();
        let turn_id = state.turn_id.clone();
        let step_id = state.step_id.clone();
        let ack = self.append(
            EventKind::ApprovalRequested,
            Actor::Engine,
            Some(&turn_id),
            Some(&step_id),
            Some(&call_id),
            Some(&caused_by),
            serde_json::to_value(payload)?,
        )?;
        self.call_state_mut(&call_id)?.requested_event_id = Some(ack.event_id.clone());
        Ok(ack)
    }

    /// Append `approval.resolved` (only on `ask`). `caused_by` = the
    /// `approval.requested` event. Enforces RATIFY-3 as amended:
    /// `resolution ∈ {allow, deny, cancelled}`; `cancelled` carries no
    /// reviewer (engine actor), no DecisionRecord, and is never mapped to
    /// `user_denied`; `allow` ⇔ approved / `deny` ⇔ denied.
    pub fn record_approval_resolved(
        &mut self,
        acked: &AckedCall,
        payload: ApprovalResolved,
    ) -> Result<DurableAck, LedgerError> {
        let call_id = acked.call_id.clone();
        let state = self.call_state(&call_id)?;
        if state.closed {
            return Err(LedgerError::CallAlreadyClosed(call_id));
        }
        let requested = state
            .requested_event_id
            .clone()
            .ok_or_else(|| LedgerError::ApprovalNotRequested(call_id.clone()))?;
        if state.resolved.is_some() {
            return Err(LedgerError::ApprovalAlreadyResolved(call_id));
        }
        validate_approval_resolved(&payload)?;
        let turn_id = state.turn_id.clone();
        let step_id = state.step_id.clone();
        let ack = self.append(
            EventKind::ApprovalResolved,
            Actor::Engine,
            Some(&turn_id),
            Some(&step_id),
            Some(&call_id),
            Some(&requested),
            serde_json::to_value(&payload)?,
        )?;
        self.call_state_mut(&call_id)?.resolved = Some(ResolvedState {
            event_id: ack.event_id.clone(),
            resolution: payload.resolution,
            decision_id: payload.decision.as_ref().map(|d| d.decision_id.clone()),
            reason_code: payload.decision.as_ref().map(|d| d.reason.code.clone()),
            explanation_text: payload
                .decision
                .as_ref()
                .map(|d| d.reason.explanation.text.clone()),
        });
        Ok(ack)
    }

    /// Append the durable start-witness `tool.started` (RULING-5, I-05a): the
    /// record is fdatasynced BEFORE any executor side effect, and the returned
    /// [`AckedStart`] token is the only gate to invocation and to post-witness
    /// statuses. The witness proves authorization to attempt execution, never
    /// that execution began (I-05b, RATIFY-4).
    ///
    /// Authorization required: `authority.decided.outcome = allow`, or
    /// `ask` + `approval.resolved.resolution = allow`. On a write/fsync
    /// failure the segment is faulted (MGR-2), no token is minted, and the
    /// tool must not run (the call's terminal is `aborted` /
    /// `witness_write_failed`, S1 §4a row 7 — best-effort, since the faulted
    /// segment may refuse that append too).
    ///
    /// `caused_by` = the `authority.decided` (rule allow) or
    /// `approval.resolved` (allow) event (RULING-8).
    pub fn record_tool_started(
        &mut self,
        acked: &AckedCall,
        payload: ToolStarted,
    ) -> Result<AckedStart, LedgerError> {
        let call_id = acked.call_id.clone();
        let state = self.call_state(&call_id)?;
        if state.closed {
            return Err(LedgerError::CallAlreadyClosed(call_id));
        }
        if state.started_event_id.is_some() {
            return Err(LedgerError::WitnessAlreadyDurable(call_id));
        }
        let decided = state
            .decided
            .as_ref()
            .ok_or_else(|| LedgerError::NotAuthorized(call_id.clone()))?;
        let authorized = match decided.outcome {
            DecisionOutcome::Allow => true,
            DecisionOutcome::Ask => matches!(
                state.resolved.as_ref(),
                Some(r) if r.resolution == Resolution::Allow
            ),
            DecisionOutcome::Deny => false,
        };
        if !authorized {
            return Err(LedgerError::NotAuthorized(call_id));
        }
        if payload.call_id != call_id {
            return Err(LedgerError::InvalidToolStarted(format!(
                "call_id '{}' != '{}'",
                payload.call_id, call_id
            )));
        }
        let latest = state
            .latest_decision_id()
            .map(str::to_owned)
            .ok_or_else(|| LedgerError::NotAuthorized(call_id.clone()))?;
        if payload.governing_decision_id != latest {
            return Err(LedgerError::InvalidToolStarted(format!(
                "governing_decision_id '{}' must be the latest lineage decision '{}'",
                payload.governing_decision_id, latest
            )));
        }
        let caused_by = state
            .last_gate_event()
            .map(str::to_owned)
            .ok_or_else(|| LedgerError::NotAuthorized(call_id.clone()))?;
        let turn_id = state.turn_id.clone();
        let step_id = state.step_id.clone();
        let ack = self.append(
            EventKind::ToolStarted,
            Actor::Engine,
            Some(&turn_id),
            Some(&step_id),
            Some(&call_id),
            Some(&caused_by),
            serde_json::to_value(payload)?,
        )?;
        self.call_state_mut(&call_id)?.started_event_id = Some(ack.event_id.clone());
        Ok(AckedStart {
            call_id,
            witness: ack,
        })
    }

    /// Record the terminal `tool.result` for a call that has NO durable
    /// start-witness: statuses `denied`, `aborted`, or `failed` (S1 §4a rows
    /// 1-7 only). Consumes the [`AckedCall`] token (I-12/I-18). The durable
    /// witness separates `aborted` from {`failed`, `indeterminate`}
    /// (RATIFY-2), so `succeeded` and `indeterminate` are rejected here — they
    /// require the post-witness path.
    ///
    /// `caused_by` per RULING-8 / S1 §4a: `denied` → the deny record
    /// (`authority.decided` or `approval.resolved`), or the last gate event
    /// for an E3 refusal (row 4); `aborted` → the last durable event of the
    /// call; `failed` (row 1) → the `tool.call` event.
    pub fn record_pre_witness_result(
        &mut self,
        acked: AckedCall,
        mut payload: ToolResult,
    ) -> Result<DurableAck, LedgerError> {
        let call_id = acked.call_id.clone();
        let state = self.call_state(&call_id)?;
        if state.closed {
            return Err(LedgerError::CallAlreadyClosed(call_id));
        }
        if state.started_event_id.is_some() {
            return Err(LedgerError::WitnessAlreadyDurable(call_id));
        }
        if payload.call_id != call_id {
            return Err(LedgerError::CallIdMismatch {
                acked: call_id,
                record: payload.call_id,
            });
        }
        if payload.status.is_success() || payload.status == ResultStatus::Indeterminate {
            return Err(LedgerError::WitnessRequired {
                call_id,
                status: payload.status.as_str().to_owned(),
            });
        }
        self.validate_common_result(&call_id, &payload)?;
        let caused_by = match payload.status {
            ResultStatus::Denied => {
                if payload.start_state != StartState::NotStarted {
                    return Err(LedgerError::InvalidToolResult(
                        "denied requires start_state=not_started".into(),
                    ));
                }
                let err = payload.error.as_ref().ok_or_else(|| {
                    LedgerError::InvalidToolResult("denied requires a typed error".into())
                })?;
                match state.deny_record() {
                    Some((event, code, text, _decision_id)) => {
                        if err.message != text {
                            return Err(LedgerError::DeniedExplanationMismatch {
                                expected: text.to_owned(),
                                found: err.message.clone(),
                            });
                        }
                        if err.code != code {
                            return Err(LedgerError::InvalidToolResult(format!(
                                "denied error.code '{}' must be the governing reason code '{}' (S1 §4a)",
                                err.code, code
                            )));
                        }
                        event.to_owned()
                    }
                    None => {
                        // E3 refusal after approval, before the witness (row
                        // 4): terminal_decision state=refused rides in the
                        // result; caused_by = the last gate event.
                        let terminal = payload.terminal_decision.as_ref().ok_or_else(|| {
                            LedgerError::DeniedWithoutRefusal(call_id.clone())
                        })?;
                        if terminal.state != DecisionState::Refused {
                            return Err(LedgerError::DeniedWithoutRefusal(call_id));
                        }
                        if err.message != terminal.reason.explanation.text {
                            return Err(LedgerError::DeniedExplanationMismatch {
                                expected: terminal.reason.explanation.text.clone(),
                                found: err.message.clone(),
                            });
                        }
                        if err.code != terminal.reason.code {
                            return Err(LedgerError::InvalidToolResult(format!(
                                "denied error.code '{}' must be the refusal reason code '{}' (S1 §4a)",
                                err.code, terminal.reason.code
                            )));
                        }
                        state
                            .last_gate_event()
                            .map(str::to_owned)
                            .ok_or_else(|| LedgerError::DeniedWithoutRefusal(call_id.clone()))?
                    }
                }
            }
            ResultStatus::Aborted => {
                if payload.start_state != StartState::NotStarted {
                    return Err(LedgerError::InvalidToolResult(
                        "aborted requires start_state=not_started (never invoked, RATIFY-2)"
                            .into(),
                    ));
                }
                state.last_durable_event().to_owned()
            }
            ResultStatus::Failed => {
                // Row 1: gate not entered (unknown tool, router/readiness/hook
                // reject) — the only pre-witness `failed` path.
                if acked.disposition != Disposition::RejectedBeforeStart {
                    return Err(LedgerError::InvalidToolResult(
                        "pre-witness failed is only valid for rejected_before_start (S1 §4a row 1)"
                            .into(),
                    ));
                }
                if payload.start_state != StartState::NotStarted {
                    return Err(LedgerError::RejectedRequiresFailedNotStarted);
                }
                if payload.error.as_ref().map(|e| e.code.is_empty()) != Some(false) {
                    return Err(LedgerError::FailedRequiresTypedError(call_id));
                }
                state.call_event_id.clone()
            }
            other => {
                return Err(LedgerError::WitnessRequired {
                    call_id,
                    status: other.as_str().to_owned(),
                })
            }
        };
        if acked.disposition == Disposition::RejectedBeforeStart
            && payload.status != ResultStatus::Failed
        {
            return Err(LedgerError::RejectedRequiresFailedNotStarted);
        }
        payload.call_id = call_id.clone();
        let turn_id = state.turn_id.clone();
        let step_id = state.step_id.clone();
        let ack = self.append(
            EventKind::ToolResult,
            Actor::Engine,
            Some(&turn_id),
            Some(&step_id),
            Some(&call_id),
            Some(&caused_by),
            serde_json::to_value(payload)?,
        )?;
        self.call_state_mut(&call_id)?.closed = true;
        Ok(ack)
    }

    /// Record the terminal `tool.result` for a call WITH a durable
    /// start-witness, by consuming the [`AckedStart`] token: statuses
    /// `succeeded`, `failed`, `indeterminate` (S1 §4a rows 8-13). `caused_by`
    /// is always the `tool.started` event (RULING-8).
    ///
    /// - `failed` requires a typed executor outcome (`error.code`): the typed
    ///   outcome is what separates `failed` from `indeterminate` (RATIFY-2).
    /// - `indeterminate` requires `origin` (RATIFY-1 as amended: the live
    ///   closer may write it when the witness is durable and no typed outcome
    ///   was received), carries `start_state=unknown`, and is never
    ///   auto-retried (`error.retryable` must be false). It is terminal.
    /// - `succeeded` requires `start_state=started`, an empty `effect_ids`
    ///   (I-16, D-2), and `terminal_decision state=merged` with
    ///   `output_digest` = `merged.reason.params.target_id` (T3a S1 §3).
    pub fn record_post_witness_result(
        &mut self,
        started: AckedStart,
        mut payload: ToolResult,
    ) -> Result<DurableAck, LedgerError> {
        let call_id = started.call_id.clone();
        let witness_event = started.witness.event_id.clone();
        let state = self.call_state(&call_id)?;
        if state.closed {
            return Err(LedgerError::CallAlreadyClosed(call_id));
        }
        if state.started_event_id.as_deref() != Some(witness_event.as_str()) {
            return Err(LedgerError::InvalidToolResult(format!(
                "witness token does not match the durable start-witness of call '{call_id}'"
            )));
        }
        if payload.call_id != call_id {
            return Err(LedgerError::CallIdMismatch {
                acked: call_id,
                record: payload.call_id,
            });
        }
        match payload.status {
            ResultStatus::Denied | ResultStatus::Aborted => {
                // The durable witness separates aborted from {failed,
                // indeterminate} (RATIFY-2): once the witness exists the call
                // can no longer terminate denied/aborted through the live
                // closer.
                return Err(LedgerError::InvalidToolResult(format!(
                    "status {} is not reachable after the durable start-witness (RATIFY-2)",
                    payload.status.as_str()
                )));
            }
            ResultStatus::Succeeded => {
                if payload.start_state != StartState::Started {
                    return Err(LedgerError::SuccessRequiresStarted);
                }
                if !payload.effect_ids.is_empty() {
                    return Err(LedgerError::SuccessWithUnresolvedEffects);
                }
                let terminal = payload.terminal_decision.as_ref().ok_or_else(|| {
                    LedgerError::SuccessRequiresMergedTerminal
                })?;
                if terminal.state != DecisionState::Merged {
                    return Err(LedgerError::SuccessRequiresMergedTerminal);
                }
                let target = terminal
                    .reason
                    .params
                    .get("target_id")
                    .and_then(|v| v.as_str());
                match (payload.output_digest.as_deref(), target) {
                    (Some(digest), Some(target)) if digest == target => {}
                    _ => return Err(LedgerError::SuccessOutputDigestMismatch),
                }
            }
            ResultStatus::Failed => {
                if payload.start_state == StartState::Unknown {
                    return Err(LedgerError::UnknownStartStateNotIndeterminate);
                }
                if payload.error.as_ref().map(|e| e.code.is_empty()) != Some(false) {
                    return Err(LedgerError::FailedRequiresTypedError(call_id));
                }
            }
            ResultStatus::Indeterminate => {
                if payload.origin.is_none() {
                    return Err(LedgerError::IndeterminateRequiresOrigin(call_id));
                }
                if payload.start_state != StartState::Unknown {
                    return Err(LedgerError::InvalidToolResult(
                        "indeterminate requires start_state=unknown (T3a S1 §3)".into(),
                    ));
                }
                if payload.output.is_some() {
                    return Err(LedgerError::InvalidToolResult(
                        "indeterminate carries no typed outcome: output must be null (RATIFY-1)"
                            .into(),
                    ));
                }
                if let Some(err) = &payload.error {
                    if err.retryable {
                        return Err(LedgerError::IndeterminateNotRetryable(call_id));
                    }
                }
            }
        }
        self.validate_common_result(&call_id, &payload)?;
        payload.call_id = call_id.clone();
        let turn_id = state.turn_id.clone();
        let step_id = state.step_id.clone();
        let ack = self.append(
            EventKind::ToolResult,
            Actor::Engine,
            Some(&turn_id),
            Some(&step_id),
            Some(&call_id),
            Some(&witness_event),
            serde_json::to_value(payload)?,
        )?;
        self.call_state_mut(&call_id)?.closed = true;
        Ok(ack)
    }

    /// Recovery classification for every open call (I-20 step 2, S1 §4a rows
    /// 14-16, §4b CP-1..CP-7), computed from durable bytes only:
    ///
    /// - deny durable, no result → `denied`, `caused_by` the deny record (row
    ///   16, CP-4);
    /// - witness durable, no result → `indeterminate` /
    ///   `outcome_unknown`, `origin=recovery`, `caused_by` the witness (row
    ///   14, CP-6/CP-7 — kill before vs after invocation are byte-identical
    ///   and classified identically; the witness is authorization-to-attempt,
    ///   never proof of invocation, I-05b);
    /// - pending approval → `approval.resolved{cancelled, recovery}` first
    ///   (caused_by the request), then `aborted` caused_by that resolve
    ///   (CP-3);
    /// - no witness, no durable deny → `aborted` / `recovered_unwitnessed`,
    ///   `caused_by` the last durable event (row 15, CP-1/CP-2/CP-5).
    ///
    /// Synthesized results carry `recovered_by` = this session's
    /// `lineage{resume}` event id (STALE-6) and are never retried (I-20).
    pub fn synthesize_recovery_results(&mut self) -> Result<Vec<DurableAck>, LedgerError> {
        let recovered_by = self
            .resume_event_id
            .clone()
            .ok_or(LedgerError::LedgerCorrupt)?;
        let open: Vec<String> = self
            .calls
            .iter()
            .filter(|(_, c)| !c.closed && c.disposition == Disposition::AdmittedToDispatch)
            .map(|(id, _)| id.clone())
            .collect();
        let mut acks = Vec::new();
        for call_id in open {
            let state = self.call_state(&call_id)?;
            let turn_id = state.turn_id.clone();
            let step_id = state.step_id.clone();
            let governing = state.latest_decision_id().map(str::to_owned);

            if let Some((deny_event, code, text, decision_id)) = state.deny_record() {
                // Row 16 / CP-4: denied, caused_by the deny record.
                let (deny_event, code, text, decision_id) = (
                    deny_event.to_owned(),
                    code.to_owned(),
                    text.to_owned(),
                    decision_id.to_owned(),
                );
                let result = ToolResult {
                    call_id: call_id.clone(),
                    status: ResultStatus::Denied,
                    output: None,
                    output_digest: None,
                    error: Some(crate::payload::ResultError {
                        code: code.clone(),
                        message: text,
                        retryable: crate::delivery::retryable_for(&code),
                    }),
                    effect_ids: vec![],
                    governing_decision_id: Some(decision_id),
                    terminal_decision: None,
                    start_state: StartState::NotStarted,
                    model_delivery: crate::delivery::recovery(crate::delivery::Recovery::Denied),
                    origin: None,
                    recovered_by: Some(recovered_by.clone()),
            executor_evidence: None,
                };
                let ack = self.append_call_result(&call_id, &deny_event, result)?;
                acks.push(ack);
                continue;
            }

            if state.started_event_id.is_some() {
                // Row 14 / CP-6, CP-7: witness, no result → indeterminate.
                let witness = state.started_event_id.clone().unwrap();
                let result = ToolResult {
                    call_id: call_id.clone(),
                    status: ResultStatus::Indeterminate,
                    output: None,
                    output_digest: None,
                    error: Some(crate::payload::ResultError {
                        code: crate::errcode::OUTCOME_UNKNOWN.into(),
                        message: "the durable start-witness exists but no result was recorded; \
                                  the outcome is unknown and this call is never auto-retried"
                            .into(),
                        retryable: crate::delivery::retryable_for(crate::errcode::OUTCOME_UNKNOWN),
                    }),
                    effect_ids: vec![],
                    governing_decision_id: governing,
                    terminal_decision: None,
                    start_state: StartState::Unknown,
                    model_delivery: crate::delivery::recovery(
                        crate::delivery::Recovery::WitnessNoResult,
                    ),
                    origin: Some(IndeterminateOrigin::Recovery),
                    recovered_by: Some(recovered_by.clone()),
            executor_evidence: None,
                };
                let ack = self.append_call_result(&call_id, &witness, result)?;
                acks.push(ack);
                continue;
            }

            let mut last = state.last_durable_event().to_owned();
            if state.requested_event_id.is_some() && state.resolved.is_none() {
                // CP-3: close the pending approval with
                // approval.resolved{cancelled, recovery}; no DecisionRecord,
                // never recorded as denial (AUD-6).
                let requested = state.requested_event_id.clone().unwrap();
                let resolved = ApprovalResolved {
                    resolution: Resolution::Cancelled,
                    actor: crate::authority::ActorRef {
                        kind: ActorKind::PolicyEngine,
                        stable_id: "engine".into(),
                    },
                    user_response: None,
                    reason: None,
                    cancel_cause: Some(crate::authority::CancelCause::Recovery),
                    decision: None,
                };
                let ack = self.append(
                    EventKind::ApprovalResolved,
                    Actor::Engine,
                    Some(&turn_id),
                    Some(&step_id),
                    Some(&call_id),
                    Some(&requested),
                    serde_json::to_value(resolved)?,
                )?;
                self.call_state_mut(&call_id)?.resolved = Some(ResolvedState {
                    event_id: ack.event_id.clone(),
                    resolution: Resolution::Cancelled,
                    decision_id: None,
                    reason_code: None,
                    explanation_text: None,
                });
                acks.push(ack.clone());
                last = ack.event_id;
            }

            // Row 15 / CP-1, CP-2, CP-3, CP-5: aborted, caused_by the last
            // durable event of the call.
            let result = ToolResult {
                call_id: call_id.clone(),
                status: ResultStatus::Aborted,
                output: None,
                output_digest: None,
                error: Some(crate::payload::ResultError {
                    code: crate::errcode::RECOVERED_UNWITNESSED.into(),
                    message: "no durable start-witness: the call provably never ran".into(),
                    // A-3 (S5 §5): retryable=true only for `cancelled`. A
                    // recovery row is never retried (I-20).
                    retryable: crate::delivery::retryable_for(crate::errcode::RECOVERED_UNWITNESSED),
                }),
                effect_ids: vec![],
                governing_decision_id: governing,
                terminal_decision: None,
                start_state: StartState::NotStarted,
                model_delivery: crate::delivery::recovery(
                    crate::delivery::Recovery::NoWitnessNoDeny,
                ),
                origin: None,
                recovered_by: Some(recovered_by.clone()),
            executor_evidence: None,
            };
            let ack = self.append_call_result(&call_id, &last, result)?;
            acks.push(ack);
        }
        Ok(acks)
    }

    /// Append terminal `turn.end`. `open_calls` is injected from the
    /// sequencer's index (callers never supply it); a success terminal with
    /// any open call is refused (I-19). `caused_by` = the last durable event
    /// of the turn (MGR-5); for an interrupted turn closed on resume that is
    /// the last synthetic result (I-20 step 3, MGR-3).
    pub fn append_turn_end(
        &mut self,
        turn_id: &str,
        status: TurnStatus,
        last_step_id: Option<&str>,
        error: Option<TurnError>,
    ) -> Result<DurableAck, LedgerError> {
        check_nonempty(turn_id)?;
        let open: Vec<String> = self
            .calls
            .iter()
            .filter(|(_, c)| !c.closed && c.disposition == Disposition::AdmittedToDispatch)
            .map(|(id, _)| id.clone())
            .collect();
        if status == TurnStatus::Succeeded && !open.is_empty() {
            return Err(LedgerError::OpenCallsOnSuccess(open));
        }
        let caused_by = self.turn_last_event.get(turn_id).cloned();
        let payload = TurnEnd {
            status,
            last_step_id: last_step_id.map(str::to_owned),
            error,
            open_calls: open,
        };
        self.append(
            EventKind::TurnEnd,
            Actor::Engine,
            Some(turn_id),
            None,
            None,
            caused_by.as_deref(),
            serde_json::to_value(payload)?,
        )
    }

    /// I-07: every acked event is already durable (each append fdatasyncs
    /// before acking); `flush` re-asserts durability and returns the ordinal
    /// through which all events are durable (LT-7).
    pub fn flush(&mut self) -> Result<u64, LedgerError> {
        if self.faulted {
            return Err(LedgerError::SegmentFaulted);
        }
        self.file.flush()?;
        self.file.sync_data()?;
        Ok(self.next_ordinal - 1)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// A-1: the typed diagnostics produced when this segment was CREATED.
    /// Exactly one `dir_sync_unavailable` on platforms without directory
    /// fsync (Windows), empty on POSIX where the directory was fsynced. A
    /// resumed segment reports none (creation happened in an earlier call).
    pub fn creation_diagnostics(&self) -> &[CreationDiagnostic] {
        &self.creation_diagnostics
    }
    pub fn segment_id(&self) -> &str {
        &self.ids.segment_id
    }
    pub fn last_ordinal(&self) -> u64 {
        self.next_ordinal - 1
    }
    pub fn open_calls(&self) -> impl Iterator<Item = &String> {
        self.calls
            .iter()
            .filter(|(_, c)| !c.closed && c.disposition == Disposition::AdmittedToDispatch)
            .map(|(id, _)| id)
    }

    /// Test hook (crash-point / witness-failure matrix, RULING-5 row 7): the
    /// next append fails before writing, faulting the segment (MGR-2). Not
    /// part of the engine API.
    #[doc(hidden)]
    pub fn debug_fail_next_append(&mut self) {
        self.fail_next_append = true;
    }

    #[doc(hidden)]
    pub fn debug_fail_next_sync(&mut self) { self.fail_next_sync = true; }

    fn call_state(&self, call_id: &str) -> Result<&CallState, LedgerError> {
        self.calls
            .get(call_id)
            .ok_or_else(|| LedgerError::UnknownCallId(call_id.to_owned()))
    }

    fn call_state_mut(&mut self, call_id: &str) -> Result<&mut CallState, LedgerError> {
        self.calls
            .get_mut(call_id)
            .ok_or_else(|| LedgerError::UnknownCallId(call_id.to_owned()))
    }

    /// Result checks shared by the pre- and post-witness paths: output
    /// nullability (S5 §3), the `unknown` ⇔ `indeterminate` start-state rule,
    /// and `governing_decision_id` = the latest DecisionRecord in the call's
    /// lineage including `terminal_decision`, null only when none exists
    /// (T3a S1 §3).
    fn validate_common_result(
        &self,
        call_id: &str,
        payload: &ToolResult,
    ) -> Result<(), LedgerError> {
        if payload.status.is_success() != payload.output.is_some() {
            return Err(LedgerError::InvalidToolResult(format!(
                "output is some iff status=succeeded (S5 §3); call '{call_id}'"
            )));
        }
        if !payload.status.is_success() && payload.error.is_none() {
            return Err(LedgerError::InvalidToolResult(format!(
                "non-success requires a typed error (S5 §3); call '{call_id}'"
            )));
        }
        if payload.start_state == StartState::Unknown
            && payload.status != ResultStatus::Indeterminate
        {
            return Err(LedgerError::UnknownStartStateNotIndeterminate);
        }
        if let Some(terminal) = &payload.terminal_decision {
            if !matches!(terminal.state, DecisionState::Merged | DecisionState::Refused) {
                return Err(LedgerError::InvalidToolResult(format!(
                    "terminal_decision must be merged|refused; call '{call_id}'"
                )));
            }
        }
        let state = self.call_state(call_id)?;
        let expected = payload
            .terminal_decision
            .as_ref()
            .map(|d| d.decision_id.clone())
            .or_else(|| state.latest_decision_id().map(str::to_owned));
        if payload.governing_decision_id != expected {
            return Err(LedgerError::InvalidToolResult(format!(
                "governing_decision_id {:?} must be the latest lineage decision {:?} (T3a S1 §3); call '{call_id}'",
                payload.governing_decision_id, expected
            )));
        }
        Ok(())
    }

    /// Internal append of a `tool.result` for recovery synthesis (the closer
    /// path, I-18): no token exists after resume, so the sequencer appends
    /// directly with an explicit `caused_by`.
    fn append_call_result(
        &mut self,
        call_id: &str,
        caused_by: &str,
        payload: ToolResult,
    ) -> Result<DurableAck, LedgerError> {
        let (turn_id, step_id) = {
            let state = self.call_state(call_id)?;
            (state.turn_id.clone(), state.step_id.clone())
        };
        let ack = self.append(
            EventKind::ToolResult,
            Actor::Engine,
            Some(&turn_id),
            Some(&step_id),
            Some(call_id),
            Some(caused_by),
            serde_json::to_value(payload)?,
        )?;
        self.call_state_mut(call_id)?.closed = true;
        Ok(ack)
    }

    fn append_lineage(&mut self, payload: Lineage) -> Result<DurableAck, LedgerError> {
        // `lineage` is session-level: turn_id and step_id are null (S1.3), and
        // `caused_by` is null (MGR-5).
        self.append(
            EventKind::Lineage,
            Actor::Engine,
            None,
            None,
            None,
            None,
            serde_json::to_value(payload)?,
        )
    }

    /// The single append path (MGR-2): assigns ordinal and event id, stamps
    /// the frame, writes the full line plus `\n`, flushes, fdatasyncs — then
    /// returns the ack. Any write/fsync error faults the segment: no further
    /// append or invocation, live calls go to the closer; a mid-write kill
    /// leaves a torn tail → `ledger_tail_corrupt`, never classified (RULING-6).
    fn append(
        &mut self,
        kind: EventKind,
        actor: Actor,
        turn_id: Option<&str>,
        step_id: Option<&str>,
        correlation_id: Option<&str>,
        caused_by: Option<&str>,
        payload: serde_json::Value,
    ) -> Result<DurableAck, LedgerError> {
        if self.faulted {
            return Err(LedgerError::SegmentFaulted);
        }
        if !payload.is_object() {
            return Err(LedgerError::PayloadNotObject {
                kind: kind.as_str().to_owned(),
            });
        }
        let ordinal = self.next_ordinal;
        let event_id = Uuid::new_v4().to_string();
        let timestamp = OffsetDateTime::now_utc()
            .format(TS_FORMAT)
            .map_err(|e| LedgerError::TimestampFormat(e.to_string()))?;
        debug_assert!(is_valid_timestamp(&timestamp));
        let record = Record {
            schema_version: SCHEMA_VERSION.to_owned(),
            event_id: event_id.clone(),
            timestamp,
            ordinal,
            session_id: self.ids.session_id.clone(),
            thread_id: self.ids.thread_id.clone(),
            lineage_id: self.ids.lineage_id.clone(),
            segment_id: self.ids.segment_id.clone(),
            turn_id: turn_id.map(str::to_owned),
            step_id: step_id.map(str::to_owned),
            actor,
            kind,
            correlation_id: correlation_id.map(str::to_owned),
            caused_by: caused_by.map(str::to_owned),
            payload,
        };
        let line = record.to_line()?;
        if self.fail_next_append {
            self.fail_next_append = false;
            self.faulted = true;
            return Err(LedgerError::Io(std::io::Error::new(
                std::io::ErrorKind::Other,
                "injected append failure (witness-write failure test hook)",
            )));
        }
        if let Err(e) = self.file.write_all(line.as_bytes()) {
            self.faulted = true;
            return Err(e.into());
        }
        if let Err(e) = self.file.flush() {
            self.faulted = true;
            return Err(e.into());
        }
        // Inject at sync_data, after write+flush: replay may see an unacked
        // witness, but the executor never receives authorization to spawn.
        if self.fail_next_sync {
            self.fail_next_sync = false;
            self.faulted = true;
            return Err(LedgerError::Io(std::io::Error::other("injected sync_data failure")));
        }
        if let Err(e) = self.file.sync_data() {
            self.faulted = true;
            return Err(e.into());
        }
        self.next_ordinal += 1;
        if let Some(t) = turn_id {
            self.turn_last_event.insert(t.to_owned(), event_id.clone());
        }
        Ok(DurableAck {
            segment_id: self.ids.segment_id.clone(),
            ordinal,
            event_id,
        })
    }
}

/// Consistency of an `authority.decided` payload (T3a S1 §3).
fn validate_authority_decided(p: &AuthorityDecided) -> Result<(), LedgerError> {
    if p.decision_ref != p.decision.decision_id {
        return Err(LedgerError::InvalidAuthorityDecided(format!(
            "decision_ref '{}' must equal decision.decision_id '{}'",
            p.decision_ref, p.decision.decision_id
        )));
    }
    let expected = match p.outcome {
        DecisionOutcome::Allow => DecisionState::Approved,
        DecisionOutcome::Deny => DecisionState::Denied,
        DecisionOutcome::Ask => DecisionState::ApprovalRequired,
    };
    if p.decision.state != expected {
        return Err(LedgerError::InvalidAuthorityDecided(format!(
            "outcome {:?} requires decision.state {:?}, found {:?}",
            p.outcome, expected, p.decision.state
        )));
    }
    if p.proposed.state != DecisionState::Proposed {
        return Err(LedgerError::InvalidAuthorityDecided(
            "proposed.state must be proposed".into(),
        ));
    }
    match p.source {
        DecisionSource::Rule if p.matched_rule_ref.is_none() => {
            return Err(LedgerError::InvalidAuthorityDecided(
                "source=rule requires matched_rule_ref".into(),
            ))
        }
        DecisionSource::ModeDefault if p.matched_rule_ref.is_some() => {
            return Err(LedgerError::InvalidAuthorityDecided(
                "source=mode_default requires matched_rule_ref=null".into(),
            ))
        }
        _ => {}
    }
    Ok(())
}

/// Consistency of an `approval.resolved` payload (T3a S1 §3, RATIFY-3 as
/// amended).
fn validate_approval_resolved(p: &ApprovalResolved) -> Result<(), LedgerError> {
    match p.resolution {
        Resolution::Cancelled => {
            if p.decision.is_some() {
                return Err(LedgerError::InvalidApprovalResolved(
                    "cancelled carries no DecisionRecord (decision=null ⇔ cancelled)".into(),
                ));
            }
            if p.actor.kind != ActorKind::PolicyEngine {
                return Err(LedgerError::InvalidApprovalResolved(
                    "cancelled carries no reviewer: actor must be the engine (RATIFY-3)".into(),
                ));
            }
            if p.user_response.is_some() {
                return Err(LedgerError::InvalidApprovalResolved(
                    "cancelled carries no user_response".into(),
                ));
            }
            if p.cancel_cause.is_none() {
                return Err(LedgerError::InvalidApprovalResolved(
                    "cancelled requires cancel_cause turn_cancelled|recovery".into(),
                ));
            }
        }
        Resolution::Allow | Resolution::Deny => {
            let decision = p.decision.as_ref().ok_or_else(|| {
                LedgerError::InvalidApprovalResolved(
                    "allow/deny requires an embedded DecisionRecord".into(),
                )
            })?;
            let expected = match p.resolution {
                Resolution::Allow => DecisionState::Approved,
                Resolution::Deny => DecisionState::Denied,
                Resolution::Cancelled => unreachable!(),
            };
            if decision.state != expected {
                return Err(LedgerError::InvalidApprovalResolved(format!(
                    "resolution {:?} requires decision.state {:?}, found {:?} (derived label, validated)",
                    p.resolution, expected, decision.state
                )));
            }
            if p.cancel_cause.is_some() {
                return Err(LedgerError::InvalidApprovalResolved(
                    "cancel_cause is set only on cancelled".into(),
                ));
            }
        }
    }
    Ok(())
}

/// S1.10 canonical layout:
/// `$DATA_ROOT/ledgers/<session_id>/segments/<segment_id>.audit.jsonl`.
pub fn segment_path(data_root: &Path, ids: &SessionIds) -> PathBuf {
    data_root
        .join("ledgers")
        .join(&ids.session_id)
        .join("segments")
        .join(format!("{}.audit.jsonl", ids.segment_id))
}

fn check_ids(ids: &SessionIds) -> Result<(), LedgerError> {
    check_nonempty(&ids.session_id)?;
    check_nonempty(&ids.thread_id)?;
    check_nonempty(&ids.lineage_id)?;
    check_nonempty(&ids.segment_id)?;
    Ok(())
}

fn check_nonempty(s: &str) -> Result<(), LedgerError> {
    if s.is_empty() {
        Err(LedgerError::EmptyId)
    } else {
        Ok(())
    }
}
