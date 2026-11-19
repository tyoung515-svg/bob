//! Slice SQLite projection (I-24/I-25, E8): a READ-ONLY derived index over
//! the JSONL ledger segments. The engine's source of truth stays the JSONL
//! ledger; the database is disposable and rebuildable solely from segment
//! bytes at any time (I-25, S1.10) — `delete_db_and_rebuild_is_identical`
//! in `tests/s1_projection.rs` proves it by deleting the DB and rebuilding
//! from the segment, asserting view equality.
//!
//! Views (S1.8):
//! - `assertion` — the `assertions` view: every parseable record in recorded
//!   order (unknown kinds included, I-26/LT-1), one row per complete line,
//!   carrying the projection cursor triple (`segment_id`, `ordinal`, byte
//!   offset) per row plus the pinned head cursor in `projection_cursor`.
//! - `joined_call` — the I-24 joined call view: one row per `tool.call`
//!   joining the call, decision chain, approval request/resolve, witness and
//!   terminal result with their causal refs (`decision_id`,
//!   `correlation_id = call_id`, `authority_snapshot_event_id`) and the
//!   terminal status per the five-status table (S1 §4a).
//! - `accepted` — the subset of calls whose terminal outcome is
//!   durable-accepted: state derivable only after schema, chain and pairing
//!   validation (EXEC-59: validated terminals of ANY of the five statuses —
//!   accepted truth is not success-only; a success that fails validation is
//!   `invalid_success`, excluded, never implied).
//!
//! Crash-consistency (I-23/RULING-6): the projection advances only through
//! complete records. The shared replay scanner (`crate::replay`) yields the
//! clean prefix; a torn/corrupt tail projects the prefix and records the
//! `ledger_tail_corrupt` diagnostic — never a partial record. Mid-file
//! integrity failures (corrupt line, non-object payload, unsupported schema
//! major, duplicate/regressing ordinal) make pairing unprovable — a second
//! result for a call could lie in the unreadable region — so NO call is
//! accepted from a segment with such a diagnostic (strictest reading,
//! EXEC-60); the calls stay visible in `joined_call` with
//! `invalid_reason = corrupt_region`. Permitted-but-diagnosed findings
//! (ordinal gap, unknown kind, torn tail) do not invalidate.
//!
//! `gap` synthesis stays out of scope (D-7): the engine never writes `gap`
//! and neither does the projection.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, params};

use crate::authority::{ApprovalResolved, AuthorityDecided, ToolStarted};
use crate::envelope::{EventKind, Record};
use crate::payload::{ToolCall, ToolResult, TurnEnd};
use crate::replay::{ReplayDiagnostic, ReplayOutcome, scan_bytes};

#[derive(Debug, thiserror::Error)]
pub enum ProjectionError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
}

/// Summary of one projection build.
#[derive(Debug, Clone)]
pub struct ProjectionReport {
    pub db_path: PathBuf,
    pub segment_path: PathBuf,
    /// Segment id pinned by the cursor (empty when the segment has no
    /// complete records).
    pub segment_id: String,
    pub n_assertions: usize,
    pub n_calls: usize,
    pub n_accepted: usize,
    /// The byte-level scan diagnostics, verbatim (they are also persisted in
    /// the `diagnostic` table).
    pub diagnostics: Vec<ReplayDiagnostic>,
}

const SCHEMA_SQL: &str = r#"
DROP VIEW IF EXISTS accepted;
DROP VIEW IF EXISTS joined_call;
DROP TABLE IF EXISTS assertion;
DROP TABLE IF EXISTS diagnostic;
DROP TABLE IF EXISTS projection_diagnostic;
DROP TABLE IF EXISTS tool_call;
DROP TABLE IF EXISTS authority_decided;
DROP TABLE IF EXISTS approval_request;
DROP TABLE IF EXISTS approval_resolve;
DROP TABLE IF EXISTS witness;
DROP TABLE IF EXISTS tool_result;
DROP TABLE IF EXISTS call_validation;
DROP TABLE IF EXISTS turn;
DROP TABLE IF EXISTS projection_cursor;

CREATE TABLE assertion (
    line_no       INTEGER PRIMARY KEY,
    byte_offset   INTEGER NOT NULL,
    segment_id    TEXT NOT NULL,
    ordinal       INTEGER NOT NULL,
    event_id      TEXT NOT NULL,
    ts            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    supported     INTEGER NOT NULL,
    actor         TEXT NOT NULL,
    turn_id       TEXT,
    step_id       TEXT,
    correlation_id TEXT,
    caused_by     TEXT,
    payload_json  TEXT NOT NULL
);
CREATE TABLE diagnostic (
    idx    INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
-- Pairing/chain/validation findings of the projection itself (beyond the
-- byte-level scan): duplicates, orphans, chain violations, invalid_success.
CREATE TABLE projection_diagnostic (
    idx     INTEGER PRIMARY KEY,
    call_id TEXT,
    kind    TEXT NOT NULL,
    detail  TEXT NOT NULL
);
CREATE TABLE tool_call (
    call_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    step_id TEXT,
    tool_name TEXT NOT NULL,
    call_index INTEGER NOT NULL,
    authority_snapshot_event_id TEXT NOT NULL,
    disposition TEXT NOT NULL,
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    caused_by TEXT
);
CREATE TABLE authority_decided (
    call_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT NOT NULL,
    matched_rule_ref TEXT,
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    caused_by TEXT
);
CREATE TABLE approval_request (
    call_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    args_digest TEXT NOT NULL,
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    caused_by TEXT
);
CREATE TABLE approval_resolve (
    call_id TEXT PRIMARY KEY,
    resolution TEXT NOT NULL,
    decision_id TEXT,
    user_response TEXT,
    reason TEXT,
    cancel_cause TEXT,
    actor_kind TEXT,
    actor_id TEXT,
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    caused_by TEXT
);
CREATE TABLE witness (
    call_id TEXT PRIMARY KEY,
    governing_decision_id TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    args_digest TEXT NOT NULL,
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    caused_by TEXT
);
-- No PK: raw projection of result assertions; the FIRST durable result wins
-- (I-12) and a duplicate is flagged by call_validation (LT-5).
CREATE TABLE tool_result (
    call_id TEXT NOT NULL,
    status TEXT NOT NULL,
    start_state TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    output_digest TEXT,
    governing_decision_id TEXT,
    terminal_decision_state TEXT,
    terminal_target_id TEXT,
    recovered_by TEXT,
    model_delivery TEXT NOT NULL,
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    caused_by TEXT
);
CREATE TABLE call_validation (
    call_id TEXT PRIMARY KEY,
    valid INTEGER NOT NULL,
    invalid_reason TEXT,
    accepted INTEGER NOT NULL,
    status TEXT,
    n_results INTEGER NOT NULL
);
CREATE TABLE turn (
    turn_id TEXT PRIMARY KEY,
    start_event_id TEXT,
    start_ordinal INTEGER,
    end_event_id TEXT,
    end_ordinal INTEGER,
    end_status TEXT,
    end_open_calls TEXT,
    projected_open_calls TEXT,
    valid INTEGER NOT NULL,
    invalid_reason TEXT
);
-- I-25: the projection cursor pins segment_id, ordinal and byte offset.
CREATE TABLE projection_cursor (
    segment_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL
);

-- I-24 joined call view: one row per tool.call, LEFT-joining the decision
-- chain, approval request/resolve, witness and the first (winning) terminal
-- result, with their causal refs and the five-status terminal status.
CREATE VIEW joined_call AS
SELECT
    tc.call_id,
    tc.call_id AS correlation_id,
    tc.turn_id, tc.step_id, tc.tool_name, tc.call_index,
    tc.authority_snapshot_event_id,
    tc.disposition,
    tc.event_id AS call_event_id, tc.ordinal AS call_ordinal,
    ad.decision_id,
    ad.outcome AS decided_outcome, ad.source AS decided_source,
    ad.event_id AS decided_event_id, ad.ordinal AS decided_ordinal,
    arq.decision_id AS requested_decision_id,
    arq.event_id AS requested_event_id, arq.ordinal AS requested_ordinal,
    arq.args_digest AS requested_args_digest,
    ars.resolution, ars.decision_id AS resolved_decision_id,
    ars.event_id AS resolved_event_id, ars.ordinal AS resolved_ordinal,
    w.governing_decision_id AS witness_governing_decision_id,
    w.executor_id AS witness_executor_id,
    w.event_id AS witness_event_id, w.ordinal AS witness_ordinal,
    tr.status, tr.start_state, tr.error_code, tr.error_message,
    tr.output_digest,
    tr.governing_decision_id, tr.terminal_decision_state,
    tr.recovered_by, tr.model_delivery,
    tr.event_id AS result_event_id, tr.ordinal AS result_ordinal,
    cv.valid, cv.invalid_reason, cv.accepted, cv.n_results
FROM tool_call tc
LEFT JOIN authority_decided ad ON ad.call_id = tc.call_id
LEFT JOIN approval_request arq ON arq.call_id = tc.call_id
LEFT JOIN approval_resolve ars ON ars.call_id = tc.call_id
LEFT JOIN witness w ON w.call_id = tc.call_id
LEFT JOIN tool_result tr ON tr.event_id = (
    SELECT t2.event_id FROM tool_result t2
    WHERE t2.call_id = tc.call_id ORDER BY t2.ordinal ASC LIMIT 1)
LEFT JOIN call_validation cv ON cv.call_id = tc.call_id;

-- I-25 accepted view: the subset of calls whose terminal outcome is
-- durable-accepted (validated; EXEC-59).
CREATE VIEW accepted AS
SELECT
    call_id, correlation_id, decision_id, status,
    result_event_id, result_ordinal, output_digest, recovered_by,
    witness_event_id, terminal_decision_state
FROM joined_call WHERE accepted = 1;
"#;

fn i64v(u: u64) -> i64 {
    i64::try_from(u).expect("u64 fits SQLite INTEGER")
}

fn opt(s: &Option<String>) -> Option<&str> {
    s.as_deref()
}

/// A record's identity in the fold: event id + cursor triple + causal ref.
struct RecRef {
    event_id: String,
    ordinal: u64,
    byte_offset: u64,
    caused_by: Option<String>,
}

impl RecRef {
    fn link_ok(&self, expected: Option<&str>) -> bool {
        self.caused_by.as_deref() == expected
    }
}

#[derive(Default)]
struct CallFold {
    turn_id: Option<String>,
    step_id: Option<String>,
    call: Option<(RecRef, ToolCall)>,
    decided: Option<(RecRef, AuthorityDecided)>,
    requested: Option<RecRef>,
    requested_args_digest: Option<String>,
    requested_decision_id: Option<String>,
    resolved: Option<(RecRef, ApprovalResolved)>,
    witness: Option<(RecRef, ToolStarted)>,
    results: Vec<(RecRef, ToolResult)>,
}

#[derive(Default)]
struct TurnFold {
    start: Option<RecRef>,
    end: Option<(RecRef, TurnEnd)>,
}

struct Finding {
    call_id: Option<String>,
    kind: &'static str,
    detail: String,
}

/// Which byte-level scan diagnostics make pairing unprovable (I-23: an
/// unreadable region keeps the calls around it unresolved; a duplicate or
/// regressing ordinal breaks the append order the pairing rests on).
/// `LedgerTailCorrupt`, `OrdinalGap` and `UnsupportedKind` are
/// permitted-but-diagnosed and do NOT invalidate (RULING-6, I-03, I-26).
fn invalidates(diag: &ReplayDiagnostic) -> bool {
    matches!(
        diag,
        ReplayDiagnostic::CorruptLine { .. }
            | ReplayDiagnostic::PayloadNotObject { .. }
            | ReplayDiagnostic::UnsupportedSchemaMajor { .. }
            | ReplayDiagnostic::OrdinalDuplicate { .. }
            | ReplayDiagnostic::OrdinalRegression { .. }
    )
}

fn diag_kind(d: &ReplayDiagnostic) -> &'static str {
    match d {
        ReplayDiagnostic::LedgerTailCorrupt { .. } => "ledger_tail_corrupt",
        ReplayDiagnostic::CorruptLine { .. } => "corrupt_line",
        ReplayDiagnostic::PayloadNotObject { .. } => "payload_not_object",
        ReplayDiagnostic::UnsupportedKind { .. } => "unsupported_kind",
        ReplayDiagnostic::UnsupportedSchemaMajor { .. } => "unsupported_schema_major",
        ReplayDiagnostic::OrdinalDuplicate { .. } => "ordinal_duplicate",
        ReplayDiagnostic::OrdinalRegression { .. } => "ordinal_regression",
        ReplayDiagnostic::OrdinalGap { .. } => "ordinal_gap",
    }
}

fn diag_detail(d: &ReplayDiagnostic) -> String {
    match d {
        ReplayDiagnostic::LedgerTailCorrupt { byte_offset, last_ordinal } => {
            format!("{{\"byte_offset\":{byte_offset},\"last_ordinal\":{last_ordinal:?}}}")
        }
        ReplayDiagnostic::CorruptLine { line, error } => {
            format!("{{\"line\":{line},\"error\":{error:?}}}")
        }
        ReplayDiagnostic::PayloadNotObject { line } => format!("{{\"line\":{line}}}"),
        ReplayDiagnostic::UnsupportedKind { line, kind } => {
            format!("{{\"line\":{line},\"kind\":{kind:?}}}")
        }
        ReplayDiagnostic::UnsupportedSchemaMajor { line, found } => {
            format!("{{\"line\":{line},\"found\":{found:?}}}")
        }
        ReplayDiagnostic::OrdinalDuplicate { line, ordinal } => {
            format!("{{\"line\":{line},\"ordinal\":{ordinal}}}")
        }
        ReplayDiagnostic::OrdinalRegression { line, prev, found } => {
            format!("{{\"line\":{line},\"prev\":{prev},\"found\":{found}}}")
        }
        ReplayDiagnostic::OrdinalGap { after, before } => {
            format!("{{\"after\":{after},\"before\":{before}}}")
        }
    }
}

fn disposition_str(d: &crate::payload::Dispatch) -> &'static str {
    match d.disposition {
        crate::payload::Disposition::AdmittedToDispatch => "admitted_to_dispatch",
        crate::payload::Disposition::RejectedBeforeStart => "rejected_before_start",
    }
}

/// Build (or rebuild — the DB is disposable) the projection database at
/// `db_path` from the segment bytes at `segment_path`, read-only over the
/// segment. The scan honors replay semantics (I-23): only complete records
/// project; a torn/corrupt tail yields the clean prefix plus a diagnostic.
pub fn project_segment(segment_path: &Path, db_path: &Path) -> Result<ProjectionReport, ProjectionError> {
    let bytes = std::fs::read(segment_path)?;
    let outcome = scan_bytes(&bytes);
    let conn = Connection::open(db_path)?;
    conn.execute_batch(SCHEMA_SQL)?;
    let report = build(conn, &outcome, segment_path, db_path)?;
    Ok(report)
}

fn build(
    conn: Connection,
    outcome: &ReplayOutcome,
    segment_path: &Path,
    db_path: &Path,
) -> Result<ProjectionReport, ProjectionError> {
    // ---- assertions view: parseable records in recorded order ----
    for (idx, rec) in outcome.assertions.iter().enumerate() {
        let offset = outcome
            .assertion_offsets
            .get(idx)
            .copied()
            .unwrap_or(0);
        conn.execute(
            "INSERT INTO assertion (line_no, byte_offset, segment_id, ordinal, event_id, ts,
                                    kind, supported, actor, turn_id, step_id, correlation_id,
                                    caused_by, payload_json)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
            params![
                (idx + 1) as i64,
                i64v(offset),
                rec.segment_id,
                i64v(rec.ordinal),
                rec.event_id,
                rec.timestamp,
                rec.kind.as_str(),
                rec.kind.is_supported() as i64,
                serde_json::to_value(&rec.actor)?.as_str().unwrap_or("engine"),
                opt(&rec.turn_id),
                opt(&rec.step_id),
                opt(&rec.correlation_id),
                opt(&rec.caused_by),
                serde_json::to_string(&rec.payload)?,
            ],
        )?;
    }

    // ---- byte-level scan diagnostics, verbatim ----
    for (idx, d) in outcome.diagnostics.iter().enumerate() {
        conn.execute(
            "INSERT INTO diagnostic (idx, kind, detail) VALUES (?1, ?2, ?3)",
            params![(idx + 1) as i64, diag_kind(d), diag_detail(d)],
        )?;
    }

    // ---- fold the six call kinds + turn carriers ----
    let mut calls: BTreeMap<String, CallFold> = BTreeMap::new();
    let mut turns: BTreeMap<String, TurnFold> = BTreeMap::new();
    let mut findings: Vec<Finding> = Vec::new();

    for (idx, rec) in outcome.assertions.iter().enumerate() {
        let rref = RecRef {
            event_id: rec.event_id.clone(),
            ordinal: rec.ordinal,
            byte_offset: outcome.assertion_offsets.get(idx).copied().unwrap_or(0),
            caused_by: rec.caused_by.clone(),
        };
        if let Some(t) = &rec.turn_id {
            turns.entry(t.clone()).or_default();
        }
        match rec.kind {
            EventKind::TurnStart => {
                let t = rec.turn_id.clone().unwrap_or_default();
                let entry = turns.entry(t).or_default();
                if entry.start.is_some() {
                    findings.push(Finding {
                        call_id: None,
                        kind: "duplicate_turn_start",
                        detail: rec.event_id.clone(),
                    });
                } else {
                    entry.start = Some(rref);
                }
            }
            EventKind::TurnEnd => {
                let payload: Result<TurnEnd, _> = serde_json::from_value(rec.payload.clone());
                match payload {
                    Ok(end) => {
                        let t = rec.turn_id.clone().unwrap_or_default();
                        let entry = turns.entry(t).or_default();
                        if entry.end.is_some() {
                            findings.push(Finding {
                                call_id: None,
                                kind: "duplicate_turn_end",
                                detail: rec.event_id.clone(),
                            });
                        } else {
                            entry.end = Some((rref, end));
                        }
                    }
                    Err(e) => findings.push(Finding {
                        call_id: None,
                        kind: "payload_schema",
                        detail: format!("turn.end: {e}"),
                    }),
                }
            }
            EventKind::ToolCall => {
                match serde_json::from_value::<ToolCall>(rec.payload.clone()) {
                    Ok(tc) => {
                        let turn_id = rec.turn_id.clone();
                        let step_id = rec.step_id.clone();
                        let entry = calls.entry(tc.call_id.clone()).or_default();
                        if entry.call.is_some() {
                            findings.push(Finding {
                                call_id: Some(tc.call_id.clone()),
                                kind: "duplicate_call",
                                detail: rec.event_id.clone(),
                            });
                        } else {
                            entry.turn_id = turn_id;
                            entry.step_id = step_id;
                            entry.call = Some((rref, tc));
                        }
                    }
                    Err(e) => findings.push(Finding {
                        call_id: None,
                        kind: "payload_schema",
                        detail: format!("tool.call: {e}"),
                    }),
                }
            }
            EventKind::AuthorityDecided => {
                if let (Some(call_id), Some(payload)) = call_payload::<AuthorityDecided>(rec, &mut findings) {
                    let entry = calls.entry(call_id.clone()).or_default();
                    if entry.decided.is_some() {
                        findings.push(Finding {
                            call_id: Some(call_id),
                            kind: "duplicate_event",
                            detail: "authority.decided".into(),
                        });
                    } else {
                        entry.decided = Some((rref, payload));
                    }
                }
            }
            EventKind::ApprovalRequested => {
                if let Some(call_id) = rec.correlation_id.clone() {
                    let entry = calls.entry(call_id.clone()).or_default();
                    if entry.requested.is_some() {
                        findings.push(Finding {
                            call_id: Some(call_id),
                            kind: "duplicate_event",
                            detail: "approval.requested".into(),
                        });
                    } else {
                        entry.requested_args_digest = rec
                            .payload
                            .get("args_digest")
                            .and_then(|v| v.as_str())
                            .map(str::to_owned);
                        entry.requested_decision_id = rec
                            .payload
                            .get("decision_id")
                            .and_then(|v| v.as_str())
                            .map(str::to_owned);
                        entry.requested = Some(rref);
                    }
                }
            }
            EventKind::ApprovalResolved => {
                if let (Some(call_id), Some(payload)) = call_payload::<ApprovalResolved>(rec, &mut findings) {
                    let entry = calls.entry(call_id.clone()).or_default();
                    if entry.resolved.is_some() {
                        findings.push(Finding {
                            call_id: Some(call_id),
                            kind: "duplicate_event",
                            detail: "approval.resolved".into(),
                        });
                    } else {
                        entry.resolved = Some((rref, payload));
                    }
                }
            }
            EventKind::ToolStarted => {
                if let (Some(call_id), Some(payload)) = call_payload::<ToolStarted>(rec, &mut findings) {
                    let entry = calls.entry(call_id.clone()).or_default();
                    if entry.witness.is_some() {
                        findings.push(Finding {
                            call_id: Some(call_id),
                            kind: "duplicate_event",
                            detail: "tool.started".into(),
                        });
                    } else {
                        entry.witness = Some((rref, payload));
                    }
                }
            }
            EventKind::ToolResult => {
                if let (Some(call_id), Some(payload)) = call_payload::<ToolResult>(rec, &mut findings) {
                    calls.entry(call_id).or_default().results.push((rref, payload));
                }
            }
            EventKind::Lineage | EventKind::StepContext | EventKind::Unsupported(_) => {}
        }
    }

    // Orphan detection (I-17: rejected from accepted truth and reported).
    let known: Vec<String> = calls.keys().cloned().collect();
    for rec in &outcome.assertions {
        if rec.kind.carries_call_correlation() {
            if let Some(cid) = &rec.correlation_id {
                if !known.contains(cid) {
                    findings.push(Finding {
                        call_id: Some(cid.clone()),
                        kind: "orphan_event",
                        detail: format!("{} references unknown call", rec.kind.as_str()),
                    });
                }
            } else {
                findings.push(Finding {
                    call_id: None,
                    kind: "correlation_mismatch",
                    detail: format!("{} carries no correlation_id", rec.kind.as_str()),
                });
            }
        }
    }

    let corrupt_region = outcome.diagnostics.iter().any(invalidates);

    // ---- per-kind tables + validation ----
    let mut n_accepted = 0usize;
    for (call_id, fold) in &calls {
        if let Some((r, tc)) = &fold.call {
            conn.execute(
                "INSERT INTO tool_call (call_id, turn_id, step_id, tool_name, call_index,
                                        authority_snapshot_event_id, disposition, event_id,
                                        ordinal, byte_offset, caused_by)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
                params![
                    call_id,
                    fold.turn_id.clone().unwrap_or_default(),
                    opt(&fold.step_id),
                    tc.tool_name,
                    i64v(tc.call_index),
                    tc.authority_snapshot_event_id,
                    disposition_str(&tc.dispatch),
                    r.event_id,
                    i64v(r.ordinal),
                    i64v(r.byte_offset),
                    opt(&r.caused_by),
                ],
            )?;
        }
        if let Some((r, ad)) = &fold.decided {
            conn.execute(
                "INSERT INTO authority_decided (call_id, decision_id, outcome, source,
                                                matched_rule_ref, event_id, ordinal,
                                                byte_offset, caused_by)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                params![
                    call_id,
                    ad.decision.decision_id,
                    serde_json::to_value(ad.outcome)?.as_str().unwrap_or(""),
                    serde_json::to_value(ad.source)?.as_str().unwrap_or(""),
                    opt(&ad.matched_rule_ref),
                    r.event_id,
                    i64v(r.ordinal),
                    i64v(r.byte_offset),
                    opt(&r.caused_by),
                ],
            )?;
        }
        if let Some(r) = &fold.requested {
            conn.execute(
                "INSERT INTO approval_request (call_id, decision_id, args_digest, event_id,
                                               ordinal, byte_offset, caused_by)
                 VALUES (?1,?2,?3,?4,?5,?6,?7)",
                params![
                    call_id,
                    fold.requested_decision_id.clone().unwrap_or_default(),
                    fold.requested_args_digest.clone().unwrap_or_default(),
                    r.event_id,
                    i64v(r.ordinal),
                    i64v(r.byte_offset),
                    opt(&r.caused_by),
                ],
            )?;
        }
        if let Some((r, ar)) = &fold.resolved {
            conn.execute(
                "INSERT INTO approval_resolve (call_id, resolution, decision_id, user_response,
                                              reason, cancel_cause, actor_kind, actor_id,
                                              event_id, ordinal, byte_offset, caused_by)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
                params![
                    call_id,
                    serde_json::to_value(ar.resolution)?.as_str().unwrap_or(""),
                    ar.decision.as_ref().map(|d| d.decision_id.clone()),
                    ar.user_response
                        .map(|u| serde_json::to_value(u).unwrap().as_str().unwrap().to_owned()),
                    opt(&ar.reason),
                    ar.cancel_cause
                        .map(|c| serde_json::to_value(c).unwrap().as_str().unwrap().to_owned()),
                    serde_json::to_value(ar.actor.kind)?.as_str().unwrap_or(""),
                    ar.actor.stable_id,
                    r.event_id,
                    i64v(r.ordinal),
                    i64v(r.byte_offset),
                    opt(&r.caused_by),
                ],
            )?;
        }
        if let Some((r, ts)) = &fold.witness {
            conn.execute(
                "INSERT INTO witness (call_id, governing_decision_id, executor_id, args_digest,
                                      event_id, ordinal, byte_offset, caused_by)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",
                params![
                    call_id,
                    ts.governing_decision_id,
                    ts.executor_id,
                    ts.args_digest,
                    r.event_id,
                    i64v(r.ordinal),
                    i64v(r.byte_offset),
                    opt(&r.caused_by),
                ],
            )?;
        }
        for (r, tr) in &fold.results {
            let (td_state, td_target) = tr
                .terminal_decision
                .as_ref()
                .map(|d| {
                    let state = serde_json::to_value(d.state).unwrap().as_str().unwrap().to_owned();
                    let target = d
                        .reason
                        .params
                        .get("target_id")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_owned();
                    (Some(state), Some(target))
                })
                .unwrap_or((None, None));
            conn.execute(
                "INSERT INTO tool_result (call_id, status, start_state, error_code,
                                         error_message, output_digest,
                                         governing_decision_id, terminal_decision_state,
                                         terminal_target_id, recovered_by, model_delivery,
                                         event_id, ordinal, byte_offset, caused_by)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15)",
                params![
                    call_id,
                    tr.status.as_str(),
                    serde_json::to_value(tr.start_state)?.as_str().unwrap_or(""),
                    tr.error.as_ref().map(|e| e.code.clone()),
                    tr.error.as_ref().map(|e| e.message.clone()),
                    opt(&tr.output_digest),
                    opt(&tr.governing_decision_id),
                    td_state,
                    td_target,
                    opt(&tr.recovered_by),
                    serde_json::to_value(tr.model_delivery)?.as_str().unwrap_or(""),
                    r.event_id,
                    i64v(r.ordinal),
                    i64v(r.byte_offset),
                    opt(&r.caused_by),
                ],
            )?;
        }

        // ---- validation (I-24 / EXEC-59 / EXEC-60) ----
        let mut call_findings: Vec<&'static str> = Vec::new();
        validate_chain(call_id, fold, &mut call_findings, &mut findings);
        if corrupt_region {
            call_findings.push("corrupt_region");
            findings.push(Finding {
                call_id: Some(call_id.clone()),
                kind: "corrupt_region",
                detail: "segment has an unreadable region: pairing unprovable (I-23)".into(),
            });
        }
        let has_result = !fold.results.is_empty();
        if !has_result {
            call_findings.push("open_call");
        }
        let valid = call_findings.is_empty();
        let accepted = valid && has_result && !corrupt_region;
        if accepted {
            n_accepted += 1;
        }
        let reason = call_findings.first().copied();
        conn.execute(
            "INSERT INTO call_validation (call_id, valid, invalid_reason, accepted, status, n_results)
             VALUES (?1,?2,?3,?4,?5,?6)",
            params![
                call_id,
                valid as i64,
                reason,
                accepted as i64,
                fold.results.first().map(|(_, tr)| tr.status.as_str()),
                fold.results.len() as i64,
            ],
        )?;
    }

    // ---- turn projections ----
    for (turn_id, tf) in &turns {
        let projected_open: Vec<&String> = calls
            .iter()
            .filter(|(_, f)| f.turn_id.as_deref() == Some(turn_id.as_str()) && f.results.is_empty())
            .map(|(id, _)| id)
            .collect();
        let end_open: Vec<String> = tf
            .end
            .as_ref()
            .map(|(_, e)| e.open_calls.clone())
            .unwrap_or_default();
        let reason = if tf.end.is_none() {
            Some("missing_turn_end")
        } else if !projected_open.is_empty() {
            Some("open_calls")
        } else if !end_open.is_empty() {
            Some("open_calls")
        } else {
            None
        };
        let (end_event, end_ordinal, end_status) = tf
            .end
            .as_ref()
            .map(|(r, e)| {
                (
                    Some(r.event_id.clone()),
                    Some(i64v(r.ordinal)),
                    Some(serde_json::to_value(e.status).unwrap().as_str().unwrap().to_owned()),
                )
            })
            .unwrap_or((None, None, None));
        conn.execute(
            "INSERT INTO turn (turn_id, start_event_id, start_ordinal, end_event_id,
                               end_ordinal, end_status, end_open_calls, projected_open_calls,
                               valid, invalid_reason)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            params![
                turn_id,
                tf.start.as_ref().map(|r| r.event_id.clone()),
                tf.start.as_ref().map(|r| i64v(r.ordinal)),
                end_event,
                end_ordinal,
                end_status,
                serde_json::to_string(&end_open)?,
                serde_json::to_string(&projected_open)?,
                reason.is_none() as i64,
                reason,
            ],
        )?;
    }

    // ---- projection findings ----
    for (idx, f) in findings.iter().enumerate() {
        conn.execute(
            "INSERT INTO projection_diagnostic (idx, call_id, kind, detail) VALUES (?1,?2,?3,?4)",
            params![(idx + 1) as i64, opt(&f.call_id), f.kind, f.detail],
        )?;
    }

    // ---- cursor (I-25) ----
    let last = outcome.assertions.len().saturating_sub(1);
    let (seg_id, ordinal, offset) = if outcome.assertions.is_empty() {
        (String::new(), 0, 0)
    } else {
        (
            outcome.assertions[last].segment_id.clone(),
            i64v(outcome.assertions[last].ordinal),
            i64v(outcome.assertion_offsets.get(last).copied().unwrap_or(0)),
        )
    };
    conn.execute(
        "INSERT INTO projection_cursor (segment_id, ordinal, byte_offset) VALUES (?1,?2,?3)",
        params![seg_id, ordinal, offset],
    )?;

    Ok(ProjectionReport {
        db_path: db_path.to_owned(),
        segment_path: segment_path.to_owned(),
        segment_id: outcome
            .assertions
            .last()
            .map(|r| r.segment_id.clone())
            .unwrap_or_default(),
        n_assertions: outcome.assertions.len(),
        n_calls: calls.len(),
        n_accepted,
        diagnostics: outcome.diagnostics.clone(),
    })
}

/// Decode a correlated call-event payload, using the envelope's
/// `correlation_id` as the call key (RULING-8: it IS the call_id).
fn call_payload<T: serde::de::DeserializeOwned>(
    rec: &Record,
    findings: &mut Vec<Finding>,
) -> (Option<String>, Option<T>) {
    let call_id = rec.correlation_id.clone();
    match serde_json::from_value::<T>(rec.payload.clone()) {
        Ok(p) => (call_id, Some(p)),
        Err(e) => {
            findings.push(Finding {
                call_id,
                kind: "payload_schema",
                detail: format!("{}: {e}", rec.kind.as_str()),
            });
            (None, None)
        }
    }
}

/// RULING-8 / MGR-5 chain and five-status consistency checks. Appends
/// per-call reasons (deduped, in check order) and per-finding records.
fn validate_chain(
    call_id: &str,
    fold: &CallFold,
    reasons: &mut Vec<&'static str>,
    findings: &mut Vec<Finding>,
) {
    let mut note = |kind: &'static str, detail: String| {
        findings.push(Finding {
            call_id: Some(call_id.to_owned()),
            kind,
            detail,
        });
    };
    let push = |r: &'static str, reasons: &mut Vec<&'static str>| {
        if !reasons.contains(&r) {
            reasons.push(r);
        }
    };

    let Some((call_ref, tc)) = &fold.call else {
        push("missing_tool_call", reasons);
        return;
    };

    // MGR-5: tool.call.caused_by == authority_snapshot_event_id.
    if !call_ref.link_ok(Some(&tc.authority_snapshot_event_id)) {
        push("snapshot_chain", reasons);
        note(
            "snapshot_chain",
            format!(
                "tool.call caused_by {:?} != authority_snapshot_event_id {:?}",
                call_ref.caused_by, tc.authority_snapshot_event_id
            ),
        );
    }

    // RULING-8 links.
    if let Some((r, _)) = &fold.decided {
        if !r.link_ok(Some(&call_ref.event_id)) {
            push("caused_by_chain", reasons);
            note(
                "caused_by_chain",
                format!("authority.decided caused_by {:?} != tool.call {}", r.caused_by, call_ref.event_id),
            );
        }
    }
    if let Some(r) = &fold.requested {
        let expected = fold.decided.as_ref().map(|(d, _)| d.event_id.clone());
        if !r.link_ok(expected.as_deref()) {
            push("caused_by_chain", reasons);
            note(
                "caused_by_chain",
                format!("approval.requested caused_by {:?} != authority.decided {expected:?}", r.caused_by),
            );
        }
    }
    if let Some((r, _)) = &fold.resolved {
        let expected = fold.requested.as_ref().map(|q| q.event_id.clone());
        if !r.link_ok(expected.as_deref()) {
            push("caused_by_chain", reasons);
            note(
                "caused_by_chain",
                format!("approval.resolved caused_by {:?} != approval.requested {expected:?}", r.caused_by),
            );
        }
    }
    if let Some((r, _)) = &fold.witness {
        let ok = fold
            .decided
            .as_ref()
            .map(|(d, _)| r.link_ok(Some(&d.event_id)))
            .unwrap_or(false)
            || fold
                .resolved
                .as_ref()
                .map(|(a, _)| r.link_ok(Some(&a.event_id)))
                .unwrap_or(false);
        if !ok {
            push("caused_by_chain", reasons);
            note(
                "caused_by_chain",
                "tool.started caused_by neither authority.decided nor approval.resolved".into(),
            );
        }
    }

    // Duplicate result (I-12/I-17, LT-5).
    if fold.results.len() > 1 {
        push("duplicate_result", reasons);
        note(
            "duplicate_result",
            format!("{} results for one call; first durable wins", fold.results.len()),
        );
    }

    let Some((result_ref, tr)) = fold.results.first() else {
        return;
    };

    // Result link per the five-status table (S1 §4a).
    let link_expected: Option<String> = match tr.status {
        crate::payload::ResultStatus::Succeeded
        | crate::payload::ResultStatus::Indeterminate => fold.witness.as_ref().map(|(w, _)| w.event_id.clone()),
        crate::payload::ResultStatus::Failed
            if tr.start_state == crate::payload::StartState::Started => {
                fold.witness.as_ref().map(|(w, _)| w.event_id.clone())
            }
        crate::payload::ResultStatus::Failed => Some(call_ref.event_id.clone()),
        crate::payload::ResultStatus::Denied => {
            // The deny record: decided{denied} or resolved{deny}; else the
            // last gate event (E3 refusal, row 4).
            let decided_deny = fold
                .decided
                .as_ref()
                .filter(|(_, ad)| ad.decision.state == crate::authority::DecisionState::Denied)
                .map(|(r, _)| r.event_id.clone());
            let resolved_deny = fold
                .resolved
                .as_ref()
                .filter(|(_, ar)| {
                    ar.decision
                        .as_ref()
                        .map(|d| d.state == crate::authority::DecisionState::Denied)
                        .unwrap_or(false)
                })
                .map(|(r, _)| r.event_id.clone());
            decided_deny
                .or(resolved_deny)
                .or_else(|| fold.decided.as_ref().map(|(r, _)| r.event_id.clone()))
                .or_else(|| fold.resolved.as_ref().map(|(r, _)| r.event_id.clone()))
        }
        crate::payload::ResultStatus::Aborted => {
            // Last durable event of the call before the result.
            let mut best: Option<(u64, String)> = None;
            for cand in [
                fold.call.as_ref().map(|(r, _)| (r.ordinal, r.event_id.clone())),
                fold.decided.as_ref().map(|(r, _)| (r.ordinal, r.event_id.clone())),
                fold.requested.as_ref().map(|r| (r.ordinal, r.event_id.clone())),
                fold.resolved.as_ref().map(|(r, _)| (r.ordinal, r.event_id.clone())),
                fold.witness.as_ref().map(|(r, _)| (r.ordinal, r.event_id.clone())),
            ]
            .into_iter()
            .flatten()
            {
                if best.as_ref().map(|(o, _)| cand.0 >= *o).unwrap_or(true) {
                    best = Some(cand);
                }
            }
            best.map(|(_, id)| id)
        }
    };
    if !result_ref.link_ok(link_expected.as_deref()) {
        push("caused_by_chain", reasons);
        note(
            "caused_by_chain",
            format!(
                "tool.result({}) caused_by {:?} != expected {link_expected:?}",
                tr.status.as_str(),
                result_ref.caused_by
            ),
        );
    }

    // Status-specific read-side validation (mirror of the write-side rules).
    use crate::payload::{ResultStatus, StartState};
    match tr.status {
        ResultStatus::Succeeded => {
            // I-24/V2: witness + merged terminal + digest match + resolved
            // governing decision + start_state=started, else invalid_success.
            let merged = tr
                .terminal_decision
                .as_ref()
                .filter(|d| d.state == crate::authority::DecisionState::Merged);
            let target = merged
                .and_then(|d| d.reason.params.get("target_id"))
                .and_then(|v| v.as_str());
            let digest_ok = target.is_some() && tr.output_digest.as_deref() == target;
            let governing_ok = tr
                .governing_decision_id
                .as_deref()
                .map(|g| {
                    merged.map(|d| d.decision_id.as_str() == g).unwrap_or(false)
                        || fold
                            .decided
                            .as_ref()
                            .map(|(_, ad)| ad.decision.decision_id == g)
                            .unwrap_or(false)
                        || fold
                            .resolved
                            .as_ref()
                            .and_then(|(_, ar)| ar.decision.as_ref())
                            .map(|d| d.decision_id == g)
                            .unwrap_or(false)
                })
                .unwrap_or(false);
            if fold.witness.is_none()
                || merged.is_none()
                || !digest_ok
                || !governing_ok
                || tr.start_state != StartState::Started
            {
                push("invalid_success", reasons);
                note(
                    "invalid_success",
                    "succeeded without witness / merged terminal / digest match / resolved governing decision / started"
                        .into(),
                );
            }
        }
        ResultStatus::Denied => {
            let denied_lineage = fold
                .decided
                .as_ref()
                .map(|(_, ad)| ad.decision.state == crate::authority::DecisionState::Denied)
                .unwrap_or(false)
                || fold
                    .resolved
                    .as_ref()
                    .and_then(|(_, ar)| ar.decision.as_ref())
                    .map(|d| d.state == crate::authority::DecisionState::Denied)
                    .unwrap_or(false)
                || tr
                    .terminal_decision
                    .as_ref()
                    .map(|d| d.state == crate::authority::DecisionState::Refused)
                    .unwrap_or(false);
            let typed = tr.error.as_ref().map(|e| !e.code.is_empty()).unwrap_or(false);
            if !denied_lineage || !typed || tr.start_state != StartState::NotStarted {
                push("status_path", reasons);
                note("status_path", "denied row inconsistent with S1 §4a rows 2-4/16".into());
            }
        }
        ResultStatus::Aborted => {
            if tr.start_state != StartState::NotStarted
                || tr.error.as_ref().map(|e| e.code.is_empty()).unwrap_or(true)
            {
                push("status_path", reasons);
                note("status_path", "aborted requires not_started + typed error".into());
            }
        }
        ResultStatus::Failed => {
            if tr.error.as_ref().map(|e| e.code.is_empty()).unwrap_or(true) {
                push("status_path", reasons);
                note("status_path", "failed requires a typed error.code (row 1/8/9/10)".into());
            }
        }
        ResultStatus::Indeterminate => {
            if tr.start_state != StartState::Unknown || tr.origin.is_none() {
                push("status_path", reasons);
                note("status_path", "indeterminate requires unknown start_state + origin".into());
            }
        }
    }
}
