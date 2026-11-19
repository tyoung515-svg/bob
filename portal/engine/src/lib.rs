//! bob-engine — BoB M3 vertical slice, engine core.
//!
//! E2 scope: the post-ruling S1 ledger — the 15-field record envelope with the
//! RULING-8 ratified constants, the RULING-4 six-event call vocabulary
//! (`tool.call`, `authority.decided`, `approval.requested`,
//! `approval.resolved`, `tool.started`, `tool.result`), the durable
//! start-witness as an authorization-to-attempt boundary (RULING-5, RATIFY-4),
//! the five call statuses with the R2-amended classification rule (RULING-3,
//! RATIFY-1/2), the RULING-8 `caused_by` causal chain, typed
//! `ledger_tail_corrupt` (RULING-6), and recovery classification of open calls
//! (S1 §4b crash-point matrix).
//!
//! E3 scope: the S2 authority evaluator — `PermissionMode=default` (the other
//! five reserved modes rejected at config load with typed `mode_unsupported`,
//! RULING-1), the deny→ask→allow rule stack with `matched_rule_ref`,
//! DecisionRecord generation with MGR-1 JCS/sha256 digests, the RULING-7
//! nine-field ApprovalPreview derivation (MGR-4 caps), and the `authorize_call`
//! gate wiring the evaluator into the E2 ledger events (`authority.decided`,
//! `approval.requested`, `approval.resolved`) with the E2/E3 recompute points.
//!
//! E4 scope: the S4 slice policy constants (`SandboxPolicy
//! ReadOnly{network_access:false}`, fixed, no runtime selection surface), the
//! FX-READ-01 read-only executor (argv-form spawn, no shell, typed MGR-13
//! `ChildOutcome`/`ExecutorEvidence` riding in `tool.result`), the full
//! ordering contract — authority → approval → E3 pre-witness → preflight →
//! durable witness → spawn → result — driven through the E2 token APIs, the
//! honest `platform_deferred` sandbox posture on the Windows build host
//! (D-18), and the S4 §2b status-table classification.
//! E5 scope: the S5 protocol layer — the versioned NDJSON frame envelope of
//! the `slice-v1` event set (schema hash in hello, gap-free `seq` from 1
//! independent of `ordinal`, RULING-8 envelope fields, retired
//! `stream.*`/`item.*`/`notice.diagnostic` frames, D-43), the harness-side
//! request/response wire (`hello`/HelloAck, `session.create`, `turn.start`,
//! `interaction.resolve` with the AT-S5-06 echo binding, `event.ack`), and
//! the headless `bob-slice-cli` driver that runs the ONE FX-READ-01 call
//! end-to-end S1→S2→S4→S5 with frames offered only after their backing
//! record is durable.
//!
//! E8 scope: the S1.8–S1.10 SQLite projection (`projection`) — a read-only
//! derived index over the JSONL segments: the `assertions` view, the I-24
//! joined call view and the I-25 `accepted` view, with the pinned
//! (segment_id, ordinal, byte offset) cursor; rebuildable from segment bytes
//! alone (I-25); crash-consistent with replay semantics (I-23).
//!
//! E9 scope: the wire & durability amendments ratified by R-EXEC-1
//! (SPEC-ERRATA-1) — `through_ordinal` as a u64-string on every wire surface
//! (A-2, [`envelope::opt_u64_string`]), directory fsync on segment create with
//! the typed `dir_sync_unavailable` gap on platforms that lack it (A-1,
//! [`ledger::CreationDiagnostic`]), the normative status → `model_delivery`
//! table and single retryability rule (A-3, [`delivery`]), and the one
//! normative error-code table with its S1 §4a / S2 mappings (A-4,
//! [`errcode`]). A-8/A-9/A-11 were already conformant and are pinned by tests.
//!
//! E10 scope: test hardening, no production behaviour change. The committed
//! wire transcripts under `engine/tests/fixtures/transcripts/` are replayed by
//! the JS reducer harness (`shell/harness`, which drives the real S10 reducer
//! over the S10 §2 domains and enforces the Tauri capability grants) and by
//! `e10_at1`; the S5 negatives pin duplicate-`request_id` replay vs the
//! EXEC-43 uncached request-shape errors, `invalid_limits` and the malformed
//! pre-handshake shapes on the wire and on the exit codes; LT-4 pins records of
//! exactly 1 MiB+1 and exactly 4 MiB; and AT-1 (EXEC-48) pins one projection
//! checksum over the engine's SQLite views, the NDJSON binding, the Tauri IPC
//! binding and the committed transcripts.
//!
//! E11 scope: TS wire codegen (A-10 / AT-S5-03 / EXEC-49). [`wire_schema`] is
//! the single normative statement of the S5 envelope, the eight `slice-v1`
//! payloads, the request/response set and the closed error-code vocabulary;
//! `hello`'s `schema_hash` and the committed TypeScript client
//! (`shell/src/protocol.ts`, rendered by [`tsgen`] via the `bob-wiregen`
//! binary) are both derived from it, and `e11_codegen` regenerates that file
//! inside `cargo test` so a hand edit or an unregenerated wire change is a red
//! suite rather than silent drift.
//!
//! Everything else (real sandboxing, the Linux tier) is deliberately
//! out of scope.

pub mod authority;
pub mod delivery;
pub mod digest;
pub mod envelope;
pub mod errcode;
pub mod evaluator;
pub mod exec;
pub mod gate;
pub mod ledger;
pub mod payload;
pub mod policy;
pub mod projection;
pub mod protocol;
pub mod replay;
pub mod tsgen;
pub mod wire_schema;

pub use authority::{
    ActorKind, ActorRef, ApprovalPreview, ApprovalResolved, AuthorityDecided, CancelCause,
    DecisionOutcome, DecisionReason, DecisionRecord, DecisionSource, DecisionState, EffectClass,
    Explanation, MatchedRulePreview, Resolution, ToolStarted, UserResponse,
};
pub use delivery::{Recovery as RecoveryRow, RETRYABLE_ONLY_FOR};
pub use digest::{jcs_bytes, jcs_sha256, scope_digest, sha256_digest, DigestError};
pub use envelope::{Actor, EventKind, Record, SCHEMA_VERSION};
pub use errcode::{canonical as canonical_code, CodeClass, Entry as CodeEntry, UnmappedCode};
pub use evaluator::{
    reason, ActionDigests, ConfigError, Evaluator, Matcher, PermissionMode, PolicyConfig, Rule,
    RuleEffect, RuleMatch, ToolEntry, ToolManifest, Verdict, VerdictFailure,
};
pub use exec::{
    code as exec_code, explanation_for as exec_explanation_for, CallTerminal, CancelToken,
    ChildOutcome, ChildSpawned, ConstructionStage, ContentValue, EvidencePolicy, ExecAuthorized,
    ExecError, Executor, ExecutorEvidence, GatePass, HelperEvidence, Platform, PreflightEvidence,
    PreflightFailure, PreflightRecord, RejectionCode, SliceExec, SpawnFailure, Supervised,
    INLINE_CAP_BYTES,
};
pub use policy::{
    slice_read_roots, SandboxPolicy, SandboxablePreference, APPROVAL_ACTION, BACKEND_ID,
    BACKEND_PLATFORM_DEFERRED, EFFECT_CLASS, FX_READ_COMMAND, NETWORK_ACCESS, READ_ROOTS,
    SANDBOXABLE_PREFERENCE, SANDBOX_POLICY, TOOL_NAME,
};
pub use gate::{
    ApprovalOutcome, ApprovalResponse, Authorization, Gate, GateError, PendingDecision, PreWitness,
};
pub use protocol::{
    abort_cause, approval_resolved_frame_payload, authority_decided_frame_payload,
    is_slice_event, parse_event_ack, parse_hello, parse_request, parse_resolve, result_line,
    error_line, hello_result, now_ts, schema_hash, tool_call_frame_payload,
    tool_result_frame_payload, tool_started_frame_payload, Frame, IncomingRequest, ResolveParams,
    ResolveResponse, SegmentReader, WireError, EVENT_SET, EVENTS, PROTOCOL_MAJOR, PROTOCOL_MINOR,
    RETIRED_EVENTS,
};
pub use ledger::{
    segment_path, AckedCall, AckedStart, CreationDiagnostic, DurableAck, Ledger, LedgerError,
    SessionIds, DIR_SYNC_UNAVAILABLE, ORDINAL_BASE,
};
#[cfg(unix)]
pub use ledger::sync_directory;
pub use replay::{replay_segment, ReplayDiagnostic, ReplayOutcome};
pub use projection::{project_segment, ProjectionError, ProjectionReport};

#[cfg(target_os = "linux")]
mod linux_sandbox;

pub mod organizational;
pub mod functional;

#[cfg(target_os="linux")]
pub mod functional_project;

pub mod workbench;

pub mod attachments;

pub mod model_transport;
