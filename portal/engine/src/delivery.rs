//! A-3 (SPEC-ERRATA-1): the NORMATIVE status → `model_delivery` table.
//!
//! One owner for every `tool.result.model_delivery` value the engine can
//! write, plus the single retryability rule. The table is not advisory: the
//! result builders in [`crate::exec`], [`crate::gate`] and [`crate::ledger`]
//! call these functions, so changing a row changes emitted bytes and the E9
//! battery fails.
//!
//! The three rows the ruling calls out explicitly:
//!
//! | Terminal / abort row | `model_delivery` |
//! |---|---|
//! | preflight abort (S1 §4a row 6 / S4 §2b row 6) | `Deliver` |
//! | cancel rows, any cancel point (S1 §4a rows 5, 6) | `None` |
//! | witness-write failure (S1 §4a row 7) | `Fatal` |
//!
//! `retryable = true` ONLY for `cancelled` (S5 §5, A-3). No other row may set
//! it — including the recovery rows and the post-witness rows.
//!
//! The remaining rows are carried so the table is the whole S1 §4a column,
//! not a fragment; `PRODUCIBLE_ROWS` records which rows have a code path in
//! the slice today.

use crate::exec::code as exec_code;
use crate::payload::ModelDelivery;

/// The single code that may set `error.retryable = true` (S5 §5, A-3).
pub const RETRYABLE_ONLY_FOR: &str = exec_code::CANCELLED;

/// One row of the normative table: the model-delivery column and the
/// retryability for that path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Row {
    /// The row number within [`Row::source`]'s table.
    pub row: u8,
    /// The table the row number belongs to: `S1 §4a` for the 16 call-status
    /// rows, `S4 §2b` for the executor rows A-3 also legislates (the
    /// preflight abort has no S1 §4a row of its own).
    pub source: &'static str,
    /// The row's path, abridged from the source table.
    pub path: &'static str,
    pub delivery: ModelDelivery,
    pub retryable: bool,
}

const fn row(row: u8, path: &'static str, delivery: ModelDelivery, retryable: bool) -> Row {
    Row {
        row,
        source: "S1 §4a",
        path,
        delivery,
        retryable,
    }
}

/// The normative table (S1 §4a `model_delivery` / `retryable` columns, as
/// ratified by A-3). `retryable` is `true` only on the cancel rows.
pub const TABLE: [Row; 16] = [
    row(1, "gate not entered (unknown tool/router/readiness)", ModelDelivery::Deliver, false),
    row(2, "E1 deny (rule or evaluator)", ModelDelivery::Deliver, false),
    row(3, "user deny / engine denial at response", ModelDelivery::Deliver, false),
    row(4, "E3 refusal after approval, before witness", ModelDelivery::Deliver, false),
    row(5, "turn cancel while approval pending", ModelDelivery::None, true),
    row(6, "cancel after allow, before witness", ModelDelivery::None, true),
    row(7, "witness write/fsync failed", ModelDelivery::Fatal, false),
    row(8, "post-witness launch/helper failure", ModelDelivery::Deliver, false),
    row(9, "tool ran and returned an error", ModelDelivery::Deliver, false),
    row(10, "completed_ok, E4 refused", ModelDelivery::Deliver, false),
    row(11, "completed_ok, E4 merged (success)", ModelDelivery::Deliver, false),
    row(12, "cancel after witness, no report in grace", ModelDelivery::None, false),
    row(13, "after witness: supervision lost, no report", ModelDelivery::Deliver, false),
    row(14, "recovery: witness, no result", ModelDelivery::Deliver, false),
    row(15, "recovery: no witness, no durable deny", ModelDelivery::None, false),
    row(16, "recovery: deny durable, no result", ModelDelivery::Deliver, false),
];

/// **S4 §2b row 6 — preflight abort** (`isolation_unavailable` /
/// `helper_verification_failed` / `sandbox_launch_failed`, after approval and
/// before the witness). A-3 legislates this row explicitly: `Deliver`, so the
/// model learns the tool could not be isolated. S1 §4a has no row of its own
/// for it, which is why it is carried as an S4-sourced row rather than
/// silently folded into a cancel row.
pub const S4_PREFLIGHT_ABORT: Row = Row {
    row: 6,
    source: "S4 §2b",
    path: "preflight fails after approval, before witness",
    delivery: ModelDelivery::Deliver,
    retryable: false,
};

/// Rows with a live code path in the slice. Row 12 (`cancelled_after_witness`
/// as `indeterminate`) is not constructible: cancellation in the slice is an
/// engine-side token that is supervised, so a cancel past the witness where
/// the child was killed and reaped is S4 §2b row 9 (`failed`,
/// `cancelled_after_start`, row 9 here), and a missing report is row 13.
pub const PRODUCIBLE_ROWS: [u8; 15] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16];

/// The S1 §4a table row for a row number (1..=16).
pub fn table_row(n: u8) -> Option<&'static Row> {
    TABLE.iter().find(|r| r.row == n)
}

/// Retryability is a property of the CODE, not of the caller: `true` only for
/// `cancelled` (A-3, S5 §5). Everything else — `witness_write_failed`,
/// `cancelled_after_start`, `execution_supervision_lost`,
/// `recovered_unwitnessed`, the deny reasons — is never retryable.
pub fn retryable_for(code: &str) -> bool {
    code == RETRYABLE_ONLY_FOR
}

// ---- Production helpers: the only way the engine picks a delivery value ----

/// Rows 2-4: an authority-class `denied` result (the authority decided; the
/// tool never ran). `Deliver` — the model must see why.
pub fn denied() -> ModelDelivery {
    ModelDelivery::Deliver
}

/// Row 1: the gate was never entered (`rejected_before_start`).
pub fn rejected_before_start() -> ModelDelivery {
    ModelDelivery::Deliver
}

/// Rows 4-6: a pre-witness `aborted` result. Cancels deliver nothing
/// (`None`), a witness-write failure is `Fatal`, and a preflight abort is
/// `Deliver` (A-3: the preflight-abort row is the one that must not be
/// swallowed).
pub fn pre_witness_abort(abort_code: &str) -> ModelDelivery {
    match abort_code {
        exec_code::CANCELLED => ModelDelivery::None,
        exec_code::WITNESS_WRITE_FAILED => ModelDelivery::Fatal,
        _ => ModelDelivery::Deliver,
    }
}

/// Rows 8-10: a post-witness `failed` result — the executor reported a
/// definitive error, so the model sees it.
pub fn post_witness_failed() -> ModelDelivery {
    ModelDelivery::Deliver
}

/// Row 11: success.
pub fn succeeded() -> ModelDelivery {
    ModelDelivery::Deliver
}

/// Row 13: the live closer's `indeterminate`.
pub fn indeterminate_live() -> ModelDelivery {
    ModelDelivery::Deliver
}

/// The three recovery rows the ledger synthesizes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Recovery {
    /// Row 16: deny durable, no result.
    Denied,
    /// Row 14: witness durable, no result.
    WitnessNoResult,
    /// Row 15: no witness, no durable deny.
    NoWitnessNoDeny,
}

/// Row 14/15/16 delivery. Recovery never retries (I-20), so none of these is
/// retryable — the `retryable` flag on the synthesized result is
/// `retryable_for(code)`, which is false for every recovery code.
pub fn recovery(r: Recovery) -> ModelDelivery {
    match r {
        Recovery::Denied | Recovery::WitnessNoResult => ModelDelivery::Deliver,
        Recovery::NoWitnessNoDeny => ModelDelivery::None,
    }
}

/// The S1 §4a row a produced result belongs to, for the enforcement test: the
/// table and the emission helpers must agree row-for-row.
pub fn delivery_of_row(n: u8) -> Option<ModelDelivery> {
    table_row(n).map(|r| r.delivery)
}
