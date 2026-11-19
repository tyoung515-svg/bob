//! A-4 (SPEC-ERRATA-1): the ONE normative error-code table.
//!
//! Every error code the engine can emit is defined here, once. The rules the
//! table encodes:
//!
//! - **S4 §2b spellings are the wire vocabulary** for call outcomes
//!   (`exec_forbidden`, `cancelled`, `witness_write_failed`, …). Where S1 §4a
//!   and S4 §2b spell the same condition differently, the S4 §2b spelling is
//!   the canonical one and the S1 §4a name is carried as an **alias**
//!   (`cancelled_before_start` → `cancelled`, `handler_error` →
//!   `execution_failed`, `executor_lost` → `execution_supervision_lost`,
//!   `cancelled_after_witness` → `cancelled_after_start`).
//! - **S2 authority reason names map into it**: `rule_forbidden` →
//!   `exec_forbidden`, `user_denied` → `approval_rejected`,
//!   `sandbox_unavailable` → `isolation_unavailable`. Reasons with no S4 row
//!   (`missing_verdict`, `capability_undeclared`, the `*_mismatch` family,
//!   `integration_invariant_failed`) are canonical as themselves: S1 §4a
//!   governs their structure and S2 names them.
//! - **`error.message` stays the governing `explanation.text` verbatim on
//!   denied rows** — enforced by the ledger (`DeniedExplanationMismatch`), not
//!   by this table: the table names codes, the decision's explanation supplies
//!   the text.
//!
//! The engine emits S2 reason names as `tool.result.error.code` on denied rows
//! (EXEC-16/EXEC-35, both ratified); [`canonical`] gives the S4 §2b wire
//! spelling each of those names maps into, so a consumer or codegen pass can
//! normalise without a second table. See E9-STATUS `[EXEC-62]` for the
//! tension between A-4's "the S4 spelling governs the wire `error.code`" and
//! ratified EXEC-35.

use std::fmt;

use crate::evaluator::reason as s2;

/// Where a code appears. The class is normative: it says which surface may
/// carry the code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CodeClass {
    /// S5 §2 request/response-shape error (`error.code` of a response).
    Request,
    /// A call outcome: `tool.result.error.code` (S4 §2b / S1 §4a / S1 §4c
    /// recovery).
    Outcome,
    /// An S2 authority reason name carried as `tool.result.error.code` on a
    /// `denied` row, or nested in a DecisionRecord's `reason.code`.
    Deny,
    /// A DecisionRecord `reason.code` that is never an error code.
    Reason,
    /// A closed-set code this slice does not emit as a call outcome
    /// (config-load, launcher, posture label). Carried so the table is the
    /// whole vocabulary, not a fragment.
    NotEmitted,
}

/// One normative code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Entry {
    /// The canonical wire spelling.
    pub code: &'static str,
    pub class: CodeClass,
    /// Other names that mean this code (S1 §4a / S2 / S4 §2 spellings).
    pub aliases: &'static [&'static str],
    /// Where it is emitted (abridged).
    pub rows: &'static str,
}

// ---- The canonical spellings, as constants (single owner) ----

// S5 §2 request errors.
pub const VERSION_MISMATCH: &str = "version_mismatch";
pub const METHOD_NOT_SUPPORTED: &str = "method_not_supported";
pub const INVALID_REQUEST: &str = "invalid_request";
pub const INVALID_LIMITS: &str = "invalid_limits";
pub const APPROVAL_MISMATCH: &str = "approval_mismatch";
pub const APPROVAL_CLOSED: &str = "approval_closed";
pub const INTERNAL_ERROR: &str = "internal_error";

// S4 §2b / S1 §4a call outcomes.
pub const EXEC_FORBIDDEN: &str = "exec_forbidden";
pub const APPROVAL_REJECTED: &str = "approval_rejected";
pub const CAPABILITY_REVOKED: &str = "capability_revoked";
pub const CANCELLED: &str = "cancelled";
pub const WITNESS_WRITE_FAILED: &str = "witness_write_failed";
pub const ISOLATION_UNAVAILABLE: &str = "isolation_unavailable";
pub const HELPER_VERIFICATION_FAILED: &str = "helper_verification_failed";
pub const SANDBOX_LAUNCH_FAILED: &str = "sandbox_launch_failed";
pub const EXECUTION_FAILED: &str = "execution_failed";
pub const CANCELLED_AFTER_START: &str = "cancelled_after_start";
pub const EXECUTION_SUPERVISION_LOST: &str = "execution_supervision_lost";
pub const INCORPORATION_REFUSED: &str = "incorporation_refused";
pub const UNKNOWN_TOOL: &str = "unknown_tool";
pub const ROUTER_REJECTED: &str = "router_rejected";
pub const READINESS_FAILED: &str = "readiness_failed";
pub const OUTCOME_UNKNOWN: &str = "outcome_unknown";
pub const RECOVERED_UNWITNESSED: &str = "recovered_unwitnessed";
pub const INTERRUPTED_RECOVERED: &str = "interrupted_recovered";

// S2 authority reasons with no S4 §2b spelling (canonical as themselves).
// The S2 catalog remains their owner ([`crate::evaluator::reason`]); this
// table holds the mapping, not a second copy of the spelling.
pub const MISSING_VERDICT: &str = s2::MISSING_VERDICT;
pub const CAPABILITY_UNDECLARED: &str = s2::CAPABILITY_UNDECLARED;
pub const ACTION_DIGEST_MISMATCH: &str = s2::ACTION_DIGEST_MISMATCH;
pub const AUTHORITY_SNAPSHOT_MISMATCH: &str = s2::AUTHORITY_SNAPSHOT_MISMATCH;
pub const POLICY_SNAPSHOT_MISMATCH: &str = s2::POLICY_SNAPSHOT_MISMATCH;
pub const SCOPE_MISMATCH: &str = s2::SCOPE_MISMATCH;
pub const INTEGRATION_INVARIANT_FAILED: &str = s2::INTEGRATION_INVARIANT_FAILED;

// DecisionRecord reasons that are never error codes.
pub const PROPOSED: &str = s2::PROPOSED;
pub const APPROVAL_ROUTE_SELECTED: &str = s2::APPROVAL_ROUTE_SELECTED;
pub const EXPLICIT_ALLOW: &str = s2::EXPLICIT_ALLOW;
pub const REVIEWER_APPROVED: &str = s2::REVIEWER_APPROVED;
pub const EFFECT_INCORPORATED: &str = "effect_incorporated";

// Closed-set codes not emitted as a call outcome in the slice.
pub const POLICY_UNKNOWN: &str = "policy_unknown";
pub const PREFLIGHT_UNAVAILABLE: &str = "preflight_unavailable";
pub const PLATFORM_DEFERRED: &str = "platform_deferred";
pub const MODE_UNSUPPORTED: &str = "mode_unsupported";

const fn e(
    code: &'static str,
    class: CodeClass,
    aliases: &'static [&'static str],
    rows: &'static str,
) -> Entry {
    Entry {
        code,
        class,
        aliases,
        rows,
    }
}

/// The normative table. Every code the engine can emit is one row; every
/// alternative spelling is an alias of exactly one row.
pub const TABLE: [Entry; 41] = [
    // ---- S5 §2 request half ----
    e(VERSION_MISMATCH, CodeClass::Request, &[], "hello: wrong selected_version major (terminal)"),
    e(METHOD_NOT_SUPPORTED, CodeClass::Request, &[], "unknown request method"),
    e(INVALID_REQUEST, CodeClass::Request, &[], "malformed request, echo/params shape"),
    e(INVALID_LIMITS, CodeClass::Request, &[], "hello.params.limits not an object"),
    e(APPROVAL_MISMATCH, CodeClass::Request, &[], "interaction.resolve echo triple mismatch (nothing appended)"),
    e(APPROVAL_CLOSED, CodeClass::Request, &[], "resolve with no pending request"),
    e(INTERNAL_ERROR, CodeClass::Request, &[], "engine-internal failure surfaced on the wire"),
    // ---- Call outcomes: S4 §2b spellings ----
    e(EXEC_FORBIDDEN, CodeClass::Outcome, &["rule_forbidden"], "S4 §2b row 1 / S1 §4a row 2 (E1 deny)"),
    e(APPROVAL_REJECTED, CodeClass::Outcome, &["user_denied"], "S4 §2b row 2 / S1 §4a row 3 (user deny)"),
    e(CAPABILITY_REVOKED, CodeClass::Outcome, &[], "S4 §2b row 3 / S1 §4a row 4 (revocation)"),
    e(CANCELLED, CodeClass::Outcome, &["cancelled_before_start"], "S4 §2b row 4 / S1 §4a rows 5-6 (cancel before the witness)"),
    e(WITNESS_WRITE_FAILED, CodeClass::Outcome, &[], "S4 §2b row 5 / S1 §4a row 7"),
    e(ISOLATION_UNAVAILABLE, CodeClass::Outcome, &["sandbox_unavailable"], "S4 §2b row 6 (preflight)"),
    e(HELPER_VERIFICATION_FAILED, CodeClass::Outcome, &[], "S4 §2b row 6 (helper verify)"),
    e(SANDBOX_LAUNCH_FAILED, CodeClass::Outcome, &[], "S4 §2b rows 6-7 (launch)"),
    e(EXECUTION_FAILED, CodeClass::Outcome, &["handler_error"], "S4 §2b row 8 / S1 §4a row 9"),
    e(CANCELLED_AFTER_START, CodeClass::Outcome, &["cancelled_after_witness"], "S4 §2b row 9"),
    e(EXECUTION_SUPERVISION_LOST, CodeClass::Outcome, &["executor_lost"], "S4 §2b row 10 / S1 §4a row 13"),
    e(INCORPORATION_REFUSED, CodeClass::Outcome, &[], "S1 §4a row 10 (E4 refuses)"),
    e(UNKNOWN_TOOL, CodeClass::Outcome, &[], "S1 §4a row 1 (gate not entered)"),
    e(ROUTER_REJECTED, CodeClass::Outcome, &[], "S1 §4a row 1 (router)"),
    e(READINESS_FAILED, CodeClass::Outcome, &[], "S1 §4a row 1 (readiness)"),
    e(OUTCOME_UNKNOWN, CodeClass::Outcome, &[], "S1 §4a row 14 (recovery: witness, no result)"),
    e(RECOVERED_UNWITNESSED, CodeClass::Outcome, &["recovery_no_witness"], "S1 §4a row 15 / S4 §2c (recovery: no witness)"),
    e(INTERRUPTED_RECOVERED, CodeClass::Outcome, &[], "I-20 step 3 turn error for an interrupted turn"),
    // ---- S2 reasons that ARE error codes on denied rows ----
    e(MISSING_VERDICT, CodeClass::Deny, &[], "E1 policy_unknown → denied (fail-closed)"),
    e(CAPABILITY_UNDECLARED, CodeClass::Deny, &[], "axis denial: capability undeclared"),
    e(ACTION_DIGEST_MISMATCH, CodeClass::Deny, &[], "E2 echo args_digest mismatch"),
    e(AUTHORITY_SNAPSHOT_MISMATCH, CodeClass::Deny, &[], "E2/E3 recompute: authority snapshot"),
    e(POLICY_SNAPSHOT_MISMATCH, CodeClass::Deny, &[], "E2/E3 recompute: policy snapshot"),
    e(SCOPE_MISMATCH, CodeClass::Deny, &[], "E2/E3 recompute: scope"),
    e(INTEGRATION_INVARIANT_FAILED, CodeClass::Deny, &[], "E3 integration invariant (slice.*)"),
    // ---- DecisionRecord reasons, never an error code ----
    e(PROPOSED, CodeClass::Reason, &[], "proposed decision state reason"),
    e(APPROVAL_ROUTE_SELECTED, CodeClass::Reason, &[], "routed-to-approval reason"),
    e(EXPLICIT_ALLOW, CodeClass::Reason, &[], "rule allow reason"),
    e(REVIEWER_APPROVED, CodeClass::Reason, &[], "approval allow reason"),
    e(EFFECT_INCORPORATED, CodeClass::Reason, &[], "merged terminal reason (A-8)"),
    // ---- Closed set, not emitted as a call outcome ----
    e(POLICY_UNKNOWN, CodeClass::NotEmitted, &[], "S4 §2 closed set: config-load typed error (D-44)"),
    e(PREFLIGHT_UNAVAILABLE, CodeClass::NotEmitted, &[], "S4 §2 closed set: S10 launcher (D-20)"),
    e(PLATFORM_DEFERRED, CodeClass::NotEmitted, &[], "S4 §2 / D-18 posture label, not a rejection"),
    e(MODE_UNSUPPORTED, CodeClass::NotEmitted, &[], "reserved permission mode rejected at config load"),
];

/// A code that is not in the normative table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnmappedCode(pub String);

impl fmt::Display for UnmappedCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "unmapped code '{}': not in the normative table (A-4)",
            self.0
        )
    }
}

impl std::error::Error for UnmappedCode {}

/// The row whose canonical spelling is exactly `code`.
pub fn entry(code: &str) -> Option<&'static Entry> {
    TABLE.iter().find(|e| e.code == code)
}

/// Resolve any spelling (canonical or alias) to the canonical wire spelling.
/// An unknown spelling is a typed error, never silently passed through.
pub fn canonical(name: &str) -> Result<&'static str, UnmappedCode> {
    if let Some(e) = entry(name) {
        return Ok(e.code);
    }
    if let Some(e) = TABLE.iter().find(|e| e.aliases.contains(&name)) {
        return Ok(e.code);
    }
    Err(UnmappedCode(name.to_owned()))
}

/// True iff `name` is a canonical spelling or a known alias.
pub fn is_known(name: &str) -> bool {
    canonical(name).is_ok()
}

/// The class of a canonical code (aliases resolve first).
pub fn class_of(name: &str) -> Result<CodeClass, UnmappedCode> {
    let c = canonical(name)?;
    Ok(entry(c).expect("canonical spellings are table rows").class)
}

/// Every canonical spelling, in table order.
pub fn all_codes() -> impl Iterator<Item = &'static str> {
    TABLE.iter().map(|e| e.code)
}

/// Every alias, with the canonical spelling it maps to.
pub fn all_aliases() -> impl Iterator<Item = (&'static str, &'static str)> {
    TABLE
        .iter()
        .flat_map(|e| e.aliases.iter().map(move |a| (*a, e.code)))
}
