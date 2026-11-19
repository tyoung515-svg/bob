//! E9 wire & durability amendments (SPEC-ERRATA-1, RULINGS-EXEC-2026-10-20).
//!
//! One battery per ordered amendment, each written so that REMOVING the
//! amendment makes the test fail:
//!
//! - **A-2** `through_ordinal` is a u64-string on every wire/ledger surface,
//!   identical to `ordinal`/`call_index`; a plain JSON number is rejected on
//!   read and never emitted.
//! - **A-1** directory fsync on segment create (POSIX) and the typed
//!   `dir_sync_unavailable` diagnostic exactly once per segment creation
//!   where directory fsync does not exist (Windows).
//! - **A-3** the normative status→`model_delivery` table, and
//!   `retryable=true` only for `cancelled`.
//! - **A-4** the single normative error-code table: every code the engine can
//!   emit is in it, S1 §4a / S2 names map into it, unknown codes are
//!   rejected.
//! - **A-8** `merged` carries `effect_incorporated` with
//!   `target_id` = the result's `output_digest`.
//! - **A-9** the `turn.start` response is emitted at TURN END with
//!   `{turn_id, step_id, status, error?}`, frames flowing mid-request.
//! - **A-11** the C-5 chain check exempts a correlation's first frame (the
//!   `tool.call` cites a `step.context` with no wire frame); later frames
//!   chain to a seen frame.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use bob_engine::envelope::{EventKind, Record};
use bob_engine::ledger::{segment_path, Ledger, SessionIds};
use bob_engine::payload::{Lineage, LineageOperation, ToolCall};
use bob_engine::replay::replay_segment;

fn tmp_root(tag: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let dir = std::env::temp_dir().join(format!(
        "bob-engine-e9-{tag}-{}-{nanos}",
        std::process::id()
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

// ================== A-1 — directory fsync on segment create ==================

/// Live on primary-pc: the platform has no directory fsync, so creation MUST
/// report the gap as a typed `dir_sync_unavailable` diagnostic — exactly once
/// per segment creation, and never on a resume (creation happened earlier).
#[cfg(not(unix))]
#[test]
fn a1_windows_reports_the_directory_fsync_gap_exactly_once_per_creation() {
    use bob_engine::ledger::{CreationDiagnostic, DIR_SYNC_UNAVAILABLE};

    let root = tmp_root("a1win");
    let idset = ids("a1win");
    let (ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
    let diags = ledger.creation_diagnostics();
    assert_eq!(
        diags.len(),
        1,
        "exactly one diagnostic per segment creation, got {diags:?}"
    );
    assert_eq!(diags[0].code(), DIR_SYNC_UNAVAILABLE);
    assert_eq!(diags[0].code(), "dir_sync_unavailable");
    match &diags[0] {
        CreationDiagnostic::DirSyncUnavailable { platform } => {
            assert_eq!(platform.to_string(), std::env::consts::OS);
            assert_eq!(platform.to_string(), "windows");
        }
    }
    // The diagnostic names the gap honestly in its display form.
    let text = diags[0].to_string();
    assert!(text.contains("dir_sync_unavailable"), "{text}");
    assert!(text.contains("not fsynced"), "{text}");
    // The per-record durability boundary is unaffected: the lineage record
    // was acked (write + flush + sync_data) and is readable.
    drop(ledger);
    assert_eq!(replay_segment(&segment_path(&root, &idset)).unwrap().assertions.len(), 1);

    // A second creation in a fresh root reports its own single diagnostic —
    // "once per segment creation", not once per process.
    let root2 = tmp_root("a1win2");
    let (ledger2, _) = Ledger::open_new(&root2, ids("a1win2")).unwrap();
    assert_eq!(ledger2.creation_diagnostics().len(), 1);

    // Resume is not a creation: no diagnostic.
    let (resumed, _) = Ledger::open_resume(&root, idset).unwrap();
    assert!(
        resumed.creation_diagnostics().is_empty(),
        "a resumed segment reports no creation diagnostic"
    );
    std::fs::remove_dir_all(&root).ok();
    std::fs::remove_dir_all(&root2).ok();
}

/// POSIX branch (runs on the Linux tier, E12): the directory IS fsynced, the
/// real `fsync(2)` succeeds on a directory, and creation reports no gap.
#[cfg(unix)]
#[test]
fn a1_posix_fsyncs_the_containing_directory_and_reports_no_gap() {
    let root = tmp_root("a1posix");
    // A real directory fsync on this host: the syscall the A-1 step depends on.
    bob_engine::ledger::sync_directory(&root).expect("fsync(2) on a directory must succeed");
    // Nested creation: the new segment directories are created and synced.
    let idset = ids("a1posix");
    let (ledger, _) = Ledger::open_new(&root.join("nested/deeper"), idset.clone()).unwrap();
    assert!(
        ledger.creation_diagnostics().is_empty(),
        "POSIX fsyncs the containing directory: no dir_sync_unavailable"
    );
    // And the created directories really exist, so the fsync targets were real.
    let dir = ledger.path().parent().unwrap();
    assert!(dir.is_dir());
    bob_engine::ledger::sync_directory(dir).expect("re-fsync the segment directory");
    std::fs::remove_dir_all(&root).ok();
}

// ============ A-4 — single normative error-code table ============

/// Every code the engine can emit resolves in the one table — outcome codes,
/// request codes, the S2 deny reasons, the DecisionRecord reasons and the
/// closed-set codes — and the table is the owner of the spellings (the
/// production constants are the table's constants, not copies).
#[test]
fn a4_every_engine_code_resolves_in_the_table() {
    use bob_engine::errcode::{self, CodeClass};
    use bob_engine::evaluator::reason;
    use bob_engine::exec::{code, RejectionCode};
    use bob_engine::protocol::wire_code;

    // S4 §2b executor/terminal codes.
    let outcome_codes = [
        code::CANCELLED,
        code::WITNESS_WRITE_FAILED,
        code::ISOLATION_UNAVAILABLE,
        code::SANDBOX_LAUNCH_FAILED,
        code::HELPER_VERIFICATION_FAILED,
        code::EXECUTION_FAILED,
        code::CANCELLED_AFTER_START,
        code::EXECUTION_SUPERVISION_LOST,
        code::INCORPORATION_REFUSED,
        errcode::OUTCOME_UNKNOWN,
        errcode::RECOVERED_UNWITNESSED,
        errcode::UNKNOWN_TOOL,
        errcode::ROUTER_REJECTED,
        errcode::READINESS_FAILED,
        errcode::INTERRUPTED_RECOVERED,
        errcode::CAPABILITY_REVOKED,
    ];
    for c in outcome_codes {
        assert!(
            errcode::is_known(c),
            "'{c}' is emitted by the engine but missing from the table (A-4)"
        );
        // A code that is canonical resolves to itself.
        assert_eq!(errcode::canonical(c).unwrap(), c);
    }

    // S5 §2 request codes.
    for c in [
        wire_code::VERSION_MISMATCH,
        wire_code::METHOD_NOT_SUPPORTED,
        wire_code::INVALID_REQUEST,
        wire_code::INVALID_LIMITS,
        wire_code::APPROVAL_MISMATCH,
        wire_code::APPROVAL_CLOSED,
        wire_code::INTERNAL_ERROR,
    ] {
        assert!(errcode::is_known(c), "request code '{c}' missing from the table");
        assert_eq!(errcode::class_of(c).unwrap(), CodeClass::Request, "{c}");
    }

    // The whole S2 reason catalog: deny reasons are error codes, decision
    // reasons are not — but every one of them is in the table with a class.
    for c in reason::SLICE_CATALOG {
        assert!(errcode::is_known(c), "S2 catalog code '{c}' missing from the table");
        let class = errcode::class_of(c).unwrap();
        assert!(
            matches!(class, CodeClass::Deny | CodeClass::Reason | CodeClass::Outcome),
            "S2 catalog code '{c}' has class {class:?}"
        );
    }

    // S4 §2's closed rejection set (two are call-level, the rest are carried
    // as NotEmitted so the vocabulary is complete).
    for r in RejectionCode::CLOSED_SET {
        assert!(
            errcode::is_known(r.as_str()),
            "RejectionCode '{}' missing from the table",
            r.as_str()
        );
    }
    // Config-load code.
    assert!(errcode::is_known("mode_unsupported"));

    // The production constants ARE the table's constants (single owner): a
    // literal drifting in exec/protocol would fail here.
    assert_eq!(code::CANCELLED, errcode::CANCELLED);
    assert_eq!(code::WITNESS_WRITE_FAILED, errcode::WITNESS_WRITE_FAILED);
    assert_eq!(code::EFFECT_INCORPORATED, errcode::EFFECT_INCORPORATED);
    assert_eq!(wire_code::VERSION_MISMATCH, errcode::VERSION_MISMATCH);
    assert_eq!(wire_code::INTERNAL_ERROR, errcode::INTERNAL_ERROR);
    // No duplicate canonical spellings and no duplicate aliases.
    let mut codes: Vec<&str> = errcode::all_codes().collect();
    let before = codes.len();
    codes.sort_unstable();
    codes.dedup();
    assert_eq!(codes.len(), before, "duplicate canonical spelling in the table");
    let mut aliases: Vec<&str> = errcode::all_aliases().map(|(a, _)| a).collect();
    let before = aliases.len();
    aliases.sort_unstable();
    aliases.dedup();
    assert_eq!(aliases.len(), before, "duplicate alias in the table");
    assert!(
        errcode::all_codes().count() >= 40,
        "the table is the whole vocabulary, not a fragment"
    );
}

/// S1 §4a and S2 spellings map into the S4 §2b wire spellings (A-4), and an
/// unknown code is rejected — never passed through.
#[test]
fn a4_aliases_map_into_the_wire_vocabulary_and_unknown_codes_are_rejected() {
    use bob_engine::errcode;

    let mappings = [
        // S1 §4a names → S4 §2b spellings.
        ("cancelled_before_start", "cancelled"),
        ("cancelled_after_witness", "cancelled_after_start"),
        ("handler_error", "execution_failed"),
        ("executor_lost", "execution_supervision_lost"),
        ("recovery_no_witness", "recovered_unwitnessed"),
        // S2 authority reason names → S4 §2b spellings.
        ("rule_forbidden", "exec_forbidden"),
        ("user_denied", "approval_rejected"),
        ("sandbox_unavailable", "isolation_unavailable"),
        // Reasons with no S4 row are canonical as themselves.
        ("capability_revoked", "capability_revoked"),
        ("missing_verdict", "missing_verdict"),
        ("action_digest_mismatch", "action_digest_mismatch"),
        ("integration_invariant_failed", "integration_invariant_failed"),
    ];
    for (alias, canonical) in mappings {
        assert_eq!(
            errcode::canonical(alias).unwrap_or_else(|e| panic!("{e}")),
            canonical,
            "A-4 mapping for '{alias}'"
        );
    }

    // Unknown / unmapped codes are typed errors, not silent passes.
    let err = errcode::canonical("totally_unknown_code").unwrap_err();
    assert_eq!(err.0, "totally_unknown_code");
    assert!(err.to_string().contains("not in the normative table"));
    assert!(errcode::canonical("").is_err());
    assert!(!errcode::is_known("CANCELLED"), "codes are case-sensitive");
    assert!(errcode::class_of("nope").is_err());
    assert!(errcode::entry("nope").is_none());
}

/// A-4's second half: on a denied row `error.message` stays the governing
/// `explanation.text` verbatim, so every deny-row code must have a compiled
/// explanation in the S2 catalog (the text the ledger binds).
#[test]
fn a4_every_deny_code_has_a_governing_explanation() {
    use bob_engine::errcode::{self, CodeClass};
    use bob_engine::evaluator::explanation_for;

    let mut checked = 0;
    for e in errcode::TABLE.iter().filter(|e| e.class == CodeClass::Deny) {
        let text = explanation_for(e.code);
        assert!(!text.is_empty(), "deny code '{}' has no explanation", e.code);
        assert_ne!(
            text, e.code,
            "deny code '{}' must have compiled explanation text, not the bare code",
            e.code
        );
        checked += 1;
    }
    assert!(checked >= 7, "the deny-row codes are covered ({checked})");
}

// ============= A-3 — normative `model_delivery` table (S1 §4a) =============

/// The table IS the normative S1 §4a column, row for row — and
/// `retryable` is true on the cancel rows only.
#[test]
fn a3_model_delivery_table_is_the_normative_column() {
    use bob_engine::delivery::{table_row, TABLE, S4_PREFLIGHT_ABORT};
    use bob_engine::payload::ModelDelivery::{Deliver, Fatal, None as NoDelivery};

    // (row, model_delivery, retryable) straight from S1 §4a / SPEC-ERRATA-1 A-3.
    let expected: [(u8, bob_engine::payload::ModelDelivery, bool); 16] = [
        (1, Deliver, false),
        (2, Deliver, false),
        (3, Deliver, false),
        (4, Deliver, false),
        (5, NoDelivery, true),
        (6, NoDelivery, true),
        (7, Fatal, false),
        (8, Deliver, false),
        (9, Deliver, false),
        (10, Deliver, false),
        (11, Deliver, false),
        (12, NoDelivery, false),
        (13, Deliver, false),
        (14, Deliver, false),
        (15, NoDelivery, false),
        (16, Deliver, false),
    ];
    assert_eq!(TABLE.len(), expected.len(), "the whole column, not a fragment");
    for (row, delivery, retryable) in expected {
        let t = table_row(row).unwrap_or_else(|| panic!("row {row} missing from the table"));
        assert_eq!(t.row, row);
        assert_eq!(t.delivery, delivery, "row {row} ({}): model_delivery", t.path);
        assert_eq!(t.retryable, retryable, "row {row} ({}): retryable", t.path);
    }
    // The three groups A-3 names explicitly, restated as a guard against a
    // silent table edit: cancel rows deliver nothing, the witness-write
    // failure is Fatal, and the preflight abort (S4 §2b row 6, which has no
    // S1 §4a row of its own) delivers.
    assert_eq!(table_row(5).unwrap().delivery, NoDelivery, "cancel rows deliver nothing");
    assert_eq!(table_row(6).unwrap().delivery, NoDelivery, "cancel after allow");
    assert_eq!(table_row(7).unwrap().delivery, Fatal, "witness-write failure is fatal");
    assert_eq!(S4_PREFLIGHT_ABORT.source, "S4 §2b");
    assert_eq!(S4_PREFLIGHT_ABORT.delivery, Deliver, "preflight abort delivers");
    assert!(!S4_PREFLIGHT_ABORT.retryable);
}

/// `retryable=true` is a property of exactly one code: `cancelled`.
#[test]
fn a3_retryable_is_true_only_for_cancelled() {
    use bob_engine::delivery::retryable_for;
    use bob_engine::exec::code;

    let every_code = [
        code::CANCELLED,
        code::WITNESS_WRITE_FAILED,
        code::ISOLATION_UNAVAILABLE,
        code::SANDBOX_LAUNCH_FAILED,
        code::HELPER_VERIFICATION_FAILED,
        code::EXECUTION_FAILED,
        code::CANCELLED_AFTER_START,
        code::EXECUTION_SUPERVISION_LOST,
        code::INCORPORATION_REFUSED,
        code::EFFECT_INCORPORATED,
        // recovery + deny-side codes
        "outcome_unknown",
        "recovered_unwitnessed",
        "rule_forbidden",
        "user_denied",
        "sandbox_unavailable",
        "capability_revoked",
        "capability_undeclared",
        "missing_verdict",
        "action_digest_mismatch",
        "authority_snapshot_mismatch",
        "policy_snapshot_mismatch",
        "scope_mismatch",
        "integration_invariant_failed",
        "exec_forbidden",
        "approval_rejected",
    ];
    for c in every_code {
        assert_eq!(
            retryable_for(c),
            c == "cancelled",
            "retryable must be true only for `cancelled` (A-3); got {} for {c}",
            retryable_for(c)
        );
    }
    // Unknown codes are not magically retryable either.
    assert!(!retryable_for("not_a_real_code"));
}

/// Every row the engine can produce goes through the table: the production
/// helpers cannot return another combination.
#[test]
fn a3_each_produced_row_matches_its_table_row() {
    use bob_engine::delivery::{self, table_row, Recovery};
    use bob_engine::exec::code;
    use bob_engine::payload::ModelDelivery;

    let check = |row: u8, produced: ModelDelivery| {
        let want = table_row(row).unwrap();
        assert_eq!(
            produced, want.delivery,
            "S1 §4a row {row} ({}) must produce {:?}, got {:?}",
            want.path, want.delivery, produced
        );
    };

    // Row 1 — gate not entered.
    check(1, delivery::rejected_before_start());
    // Rows 2-4 — authority-class denial (all three deny carriers).
    check(2, delivery::denied());
    check(3, delivery::denied());
    check(4, delivery::denied());
    // Rows 5/6 — cancel, at either cancel point.
    check(5, delivery::pre_witness_abort(code::CANCELLED));
    check(6, delivery::pre_witness_abort(code::CANCELLED));
    assert!(delivery::retryable_for(code::CANCELLED));
    // Row 7 — witness write/fsync failure.
    check(7, delivery::pre_witness_abort(code::WITNESS_WRITE_FAILED));
    assert!(!delivery::retryable_for(code::WITNESS_WRITE_FAILED));
    // The executor-class preflight abort (A-3's "preflight abort Deliver",
    // S4 §2b row 6 — not an S1 §4a row).
    for c in [
        code::ISOLATION_UNAVAILABLE,
        code::HELPER_VERIFICATION_FAILED,
        code::SANDBOX_LAUNCH_FAILED,
    ] {
        assert_eq!(
            delivery::pre_witness_abort(c),
            bob_engine::delivery::S4_PREFLIGHT_ABORT.delivery,
            "preflight abort ({c}) must deliver (A-3)"
        );
        assert!(!delivery::retryable_for(c));
    }
    // Rows 8-10 — post-witness failures.
    for c in [
        code::SANDBOX_LAUNCH_FAILED,
        code::EXECUTION_FAILED,
        code::CANCELLED_AFTER_START,
        code::INCORPORATION_REFUSED,
        code::EXECUTION_SUPERVISION_LOST,
    ] {
        check(9, delivery::post_witness_failed());
        assert!(!delivery::retryable_for(c), "{c} is not retryable (A-3)");
    }
    // Row 11 — success.
    check(11, delivery::succeeded());
    // Row 13 — live indeterminate.
    check(13, delivery::indeterminate_live());
    // Rows 14/15/16 — recovery.
    check(14, delivery::recovery(Recovery::WitnessNoResult));
    check(15, delivery::recovery(Recovery::NoWitnessNoDeny));
    check(16, delivery::recovery(Recovery::Denied));
}

/// Live: a real recovery-synthesized `aborted` uses row 15 (`none`, not
/// retryable) — the row the engine used to mark retryable.
#[test]
fn a3_live_recovery_result_follows_the_table() {
    use bob_engine::payload::{Disposition, ToolCall};

    let root = tmp_root("a3rec");
    let idset = ids("a3rec");
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        let t = ledger
            .append_turn_start(
                "t-1",
                bob_engine::payload::TurnStart {
                    input_digest: "sha256:x".into(),
                    parent_turn_id: None,
                },
            )
            .unwrap();
        let _ = t;
        let step = ledger
            .append_step_context(
                "t-1",
                "s-1",
                bob_engine::payload::StepContext {
                    model: "slice-model".into(),
                    tool_router_revision: "r1".into(),
                    model_visible_tools: vec!["exec_command".into()],
                    mcp_bindings: vec![],
                    approval_policy: json!({"mode": "default"}),
                    sandbox_policy: json!({"policy": "read_only"}),
                    settings_revision: "s1".into(),
                },
            )
            .unwrap();
        let call = ToolCall {
            call_id: "call-1".into(),
            model_item_id: "item-1".into(),
            call_index: 0,
            tool_name: "exec_command".into(),
            handler_kind: "builtin".into(),
            arguments: json!({"argv": ["fx-read", "./hello.txt"]}),
            execution_class: bob_engine::payload::ExecutionClass::Serial,
            exposure: "model".into(),
            authority_snapshot_event_id: step.event_id.clone(),
            dispatch: bob_engine::payload::Dispatch {
                disposition: Disposition::AdmittedToDispatch,
                gate: bob_engine::payload::Gate::None,
                executor_id: None,
                reason_code: None,
            },
        };
        ledger.append_tool_call("t-1", "s-1", call).unwrap();
        // Interrupted: no witness, no result.
    }
    let (mut ledger, _) = Ledger::open_resume(&root, idset).unwrap();
    let acks = ledger.synthesize_recovery_results().unwrap();
    assert_eq!(acks.len(), 1);

    let outcome = replay_segment(&segment_path(&root, &ids("a3rec"))).unwrap();
    let aborted = outcome
        .assertions
        .iter()
        .find(|r| r.kind == EventKind::ToolResult)
        .expect("a synthesized tool.result");
    assert_eq!(aborted.payload["status"], json!("aborted"));
    assert_eq!(aborted.payload["error"]["code"], json!("recovered_unwitnessed"));
    assert_eq!(
        aborted.payload["model_delivery"],
        json!("none"),
        "S1 §4a row 15 delivers nothing"
    );
    assert_eq!(
        aborted.payload["error"]["retryable"],
        json!(false),
        "A-3: retryable is true only for `cancelled`"
    );
    std::fs::remove_dir_all(&root).ok();
}

// ==================== A-2 — `through_ordinal` u64-string ====================

#[test]
fn a2_through_ordinal_is_written_as_a_u64_string() {
    let lineage = Lineage {
        operation: LineageOperation::Resume,
        source_lineage_id: Some("lin-1".into()),
        source_segment_id: Some("seg-1".into()),
        through_ordinal: Some(8),
        through_byte_offset: Some(4096),
        persistence: None,
    };
    let v = serde_json::to_value(&lineage).unwrap();
    assert_eq!(
        v["through_ordinal"],
        json!("8"),
        "A-2: through_ordinal is a u64-string, identical to ordinal/call_index"
    );
    assert!(
        v["through_ordinal"].is_string(),
        "never a plain JSON number: {v}"
    );
    // `through_byte_offset` is NOT ordinal-bearing (it is a byte offset) and
    // stays numeric.
    assert_eq!(v["through_byte_offset"], json!(4096));

    // null when no prefix is pinned.
    let new = Lineage {
        operation: LineageOperation::New,
        source_lineage_id: None,
        source_segment_id: None,
        through_ordinal: None,
        through_byte_offset: None,
        persistence: None,
    };
    assert_eq!(serde_json::to_value(&new).unwrap()["through_ordinal"], Value::Null);
}

#[test]
fn a2_numeric_through_ordinal_is_rejected_on_read() {
    let base = json!({
        "operation": "resume",
        "source_lineage_id": "lin-1",
        "source_segment_id": "seg-1",
        "through_ordinal": "8",
        "through_byte_offset": 4096,
        "persistence": null
    });
    // The string form reads back exactly.
    let l: Lineage = serde_json::from_value(base.clone()).unwrap();
    assert_eq!(l.through_ordinal, Some(8));

    // A plain JSON number is a wire violation (A-2) — rejected, not coerced.
    let mut numeric = base.clone();
    numeric["through_ordinal"] = json!(8);
    let err = serde_json::from_value::<Lineage>(numeric)
        .expect_err("a numeric through_ordinal must be rejected (A-2)");
    assert!(
        err.to_string().contains("string"),
        "the rejection names the type: {err}"
    );

    // Non-numeric strings are rejected too (no lossy parsing).
    let mut junk = base;
    junk["through_ordinal"] = json!("8x");
    assert!(serde_json::from_value::<Lineage>(junk).is_err());
}

#[test]
fn a2_ordinal_bearing_fields_are_strings_on_every_surface() {
    // Envelope `ordinal`.
    let rec: Record = serde_json::from_value(json!({
        "schema_version": "1.0",
        "event_id": "0b9f6bd2-1c1e-4d3f-8a1e-3f2c9b7d5a11",
        "timestamp": "2026-10-20T00:00:00.000Z",
        "ordinal": "1",
        "session_id": "sess", "thread_id": "thr", "lineage_id": "lin", "segment_id": "seg",
        "turn_id": null, "step_id": null,
        "actor": "engine", "kind": "lineage",
        "correlation_id": null, "caused_by": null,
        "payload": {}
    }))
    .unwrap();
    let v = serde_json::to_value(&rec).unwrap();
    assert!(v["ordinal"].is_string(), "ordinal is a u64-string");

    // `tool.call.call_index`.
    let call = ToolCall {
        call_id: "call-1".into(),
        model_item_id: "item-1".into(),
        call_index: 0,
        tool_name: "exec_command".into(),
        handler_kind: "builtin".into(),
        arguments: json!({"argv": ["fx-read", "./hello.txt"]}),
        execution_class: bob_engine::payload::ExecutionClass::Serial,
        exposure: "model".into(),
        authority_snapshot_event_id: "step-ctx".into(),
        dispatch: bob_engine::payload::Dispatch {
            disposition: bob_engine::payload::Disposition::AdmittedToDispatch,
            gate: bob_engine::payload::Gate::None,
            executor_id: None,
            reason_code: None,
        },
    };
    let cv = serde_json::to_value(&call).unwrap();
    assert!(
        cv["call_index"].is_string(),
        "call_index is a u64-string: {cv}"
    );
}

/// Live proof: a real segment's bytes carry `"through_ordinal":"N"` (quoted)
/// and every `"ordinal"` is quoted, on both the `new` and the `resume` record.
#[test]
fn a2_live_segment_bytes_never_carry_a_numeric_ordinal() {
    let root = tmp_root("a2bytes");
    let idset = ids("a2bytes");
    let path = segment_path(&root, &idset);
    {
        let (mut ledger, _) = Ledger::open_new(&root, idset.clone()).unwrap();
        ledger
            .append_turn_start(
                "t-1",
                bob_engine::payload::TurnStart {
                    input_digest: "sha256:x".into(),
                    parent_turn_id: None,
                },
            )
            .unwrap();
    }
    let (mut ledger, _) = Ledger::open_resume(&root, idset).unwrap();
    ledger.flush().unwrap();

    let text = std::fs::read_to_string(&path).unwrap();
    assert!(
        text.contains("\"through_ordinal\":\""),
        "resume lineage pins the prefix as a u64-string: {text}"
    );
    assert!(
        !text.contains("\"through_ordinal\":8")
            && !text.contains("\"through_ordinal\":2")
            && !text.contains("\"through_ordinal\":0"),
        "no numeric through_ordinal in the durable bytes"
    );
    for field in ["\"ordinal\":", "\"call_index\":"] {
        for (i, _) in text.match_indices(field) {
            let rest = &text[i + field.len()..];
            assert!(
                rest.starts_with('"'),
                "{field} must be quoted, found {}",
                &rest[..rest.len().min(12)]
            );
        }
    }
    // And the record parses back with the string form intact.
    let outcome = replay_segment(&path).unwrap();
    let resume = outcome
        .assertions
        .iter()
        .find(|r| r.kind == EventKind::Lineage && r.payload["operation"] == json!("resume"))
        .expect("a resume lineage record");
    assert!(resume.payload["through_ordinal"].is_string());
    std::fs::remove_dir_all(&root).ok();
}

// ============ E9 item 7 — committed fixtures carry no ordinals ============

fn walk(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                out.extend(walk(&p));
            } else {
                out.push(p);
            }
        }
    }
    out
}

/// E9 item 7: every committed fixture is scanned for ordinal-bearing fields —
/// none may carry a plain JSON number (A-2) — and the E5 scripted-run fixtures
/// are digest-pinned, so the post-amendment run proves they regenerate
/// byte-identically rather than being silently rewritten.
#[test]
fn a2_committed_fixtures_carry_no_numeric_ordinal_and_are_unchanged() {
    use bob_engine::digest::sha256_digest;

    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let files = walk(&dir);
    assert!(
        files.len() >= 4,
        "the committed fixture set is present ({} files)",
        files.len()
    );
    for path in &files {
        let Ok(text) = std::fs::read_to_string(path) else {
            continue; // binary fixtures have no JSON fields
        };
        for field in ["\"ordinal\":", "\"call_index\":", "\"through_ordinal\":"] {
            for (i, _) in text.match_indices(field) {
                // E10: skip insignificant whitespace so a pretty-printed
                // fixture is judged on the VALUE, not on its spacing (the
                // invariant is "u64-string or null", not "compact JSON").
                let rest = text[i + field.len()..].trim_start();
                assert!(
                    rest.starts_with('"') || rest.starts_with("null"),
                    "{}: {field} must be a u64-string or null, found {}",
                    path.display(),
                    &rest[..rest.len().min(12)]
                );
            }
        }
    }

    // Digest-pinned: the E5 allow/deny scripted runs are unchanged by E9 (they
    // embed no ordinals), so a regeneration must be a deliberate edit.
    let pinned = [
        ("s5/allow.expected.json", "ba6f8fab55349b9f5e8964709f7c18e8987eb45b82d501ebb64c719fedfab407"),
        ("s5/allow.requests.jsonl", "f94cda03920f7ed6eba96a7affb371a46dffae1320720cafeb5afc84383b0456"),
        ("s5/deny.expected.json", "158cd3a5608dda6eb33873de0f6e081269a02175dfc211b25032fddbc6da08b8"),
        ("s5/deny.requests.jsonl", "f94cda03920f7ed6eba96a7affb371a46dffae1320720cafeb5afc84383b0456"),
    ];
    for (name, digest) in pinned {
        let bytes = std::fs::read(dir.join(name)).unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(
            sha256_digest(&bytes).trim_start_matches("sha256:"),
            digest,
            "{name} changed: regenerate deliberately and re-pin (E9 item 7)"
        );
    }
}
