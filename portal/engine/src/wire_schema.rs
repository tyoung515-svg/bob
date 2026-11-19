//! The single normative wire schema (A-10 / AT-S5-03, EXEC-49).
//!
//! One source of truth for the slice wire. This module is the machine-readable
//! description of the S5 frame envelope, the eight `slice-v1` payloads, the
//! request/response messages and the closed error-code vocabulary; every
//! exported wire surface is DERIVED from it:
//!
//! - [`schema_description`] — the canonical JSON that `hello`'s `schema_hash`
//!   is taken over (both ends compare it before a frame flows), and
//! - `crate::tsgen::generate` — the committed TypeScript wire module
//!   `shell/src/protocol.ts`, whose drift check is
//!   `tests/e11_codegen.rs`.
//!
//! Nothing else restates a field list: `EVENTS`, `schema_description()` and the
//! generated TS all read from here. The model is bound to the Rust types by
//! `tests/e11_codegen.rs`, which validates the frames of a LIVE run against it
//! (envelope key set, payload key set, presence and value type per field), so
//! a model that drifts from `envelope::Record` / the payload builders fails a
//! test rather than silently generating a wrong client.
//!
//! Scope: slice `schema_version` "1.0" only (RULING-8). Additive payload
//! fields are allowed by the protocol (N1) but must land here first — this
//! model is what "the wire" means in this repository.

use serde_json::{json, Value};

/// A wire value type as JSON/TS carries it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ty {
    /// JSON string.
    Str,
    /// JSON boolean.
    Bool,
    /// JSON number (an integer the wire does not treat as an ordinal).
    Num,
    /// Ordinal-bearing u64 carried as a base-10 JSON **string** (A-2): the
    /// field class of `ordinal`, `call_index` and `through_ordinal`. A plain
    /// number is a wire violation.
    U64Str,
    /// JSON carried verbatim; the inner shape is owned by another seat
    /// (S4 `targets`, canonical `args`, `reason.params`, `capability_set`).
    Json,
    /// A named shape in [`SHAPES`].
    Ref(&'static str),
    /// A JSON array of the given element type.
    Arr(ArrTy),
}

/// Element type of a wire array.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArrTy {
    Str,
    Ref(&'static str),
}

/// Whether a field is always present, present-but-nullable, or absent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Presence {
    /// Always present; never null.
    Required,
    /// Always present; may be `null` (serde `Option` without a skip).
    Nullable,
    /// Absent from the JSON object when it has no value, and may also be
    /// `null` (serde `skip_serializing_if`). Renders as `?` in
    /// [`schema_description`] and as a TS optional property.
    Optional,
}

/// One field of a shape or envelope.
#[derive(Debug, Clone, Copy)]
pub struct Field {
    pub name: &'static str,
    pub ty: Ty,
    pub presence: Presence,
}

/// A required, non-null field.
pub const fn req(name: &'static str, ty: Ty) -> Field {
    Field {
        name,
        ty,
        presence: Presence::Required,
    }
}

/// A present-but-nullable field.
pub const fn null(name: &'static str, ty: Ty) -> Field {
    Field {
        name,
        ty,
        presence: Presence::Nullable,
    }
}

/// A field that is absent when it has no value.
pub const fn opt(name: &'static str, ty: Ty) -> Field {
    Field {
        name,
        ty,
        presence: Presence::Optional,
    }
}

/// How a named shape is rendered.
#[derive(Debug, Clone, Copy)]
pub enum ShapeKind {
    /// An object with the given fields.
    Object,
    /// A closed set of string literals (a serde `rename_all` enum).
    Union(&'static [&'static str]),
    /// A documented TS body emitted verbatim; used only where the JSON form is
    /// not expressible as a plain object or a string-literal union (a tagged
    /// union and a bool-or-string enum). Each use is bound to the engine's own
    /// serialization by `tests/e11_codegen.rs`.
    Custom(&'static str),
    /// The closed A-4 error-code vocabulary: a string-literal union built from
    /// the one normative table ([`crate::errcode::TABLE`]) at generation time,
    /// so a code the engine can emit cannot be missing from the client.
    ErrorCode,
}

/// A named wire shape.
#[derive(Debug, Clone, Copy)]
pub struct Shape {
    /// The TypeScript type name; the object it declares is what the engine
    /// serializes under that role.
    pub name: &'static str,
    pub doc: &'static str,
    pub kind: ShapeKind,
    pub fields: &'static [Field],
}

const ACTOR_VARIANTS: &[&str] = &["engine", "model", "user", "hook", "subagent"];
const TOOL_RESULT_STATUS_VARIANTS: &[&str] = &[
    "succeeded",
    "failed",
    "denied",
    "aborted",
    "indeterminate",
];
const TURN_STATUS_VARIANTS: &[&str] = &["succeeded", "failed", "aborted", "incomplete"];
const ABORT_CAUSE_VARIANTS: &[&str] = &[
    "cancelled",
    "witness_write_failed",
    "preflight_failed",
    "recovery_no_witness",
];
const ACTOR_KIND_VARIANTS: &[&str] = &["policy_engine", "user"];
const DECISION_STATE_VARIANTS: &[&str] = &[
    "proposed",
    "approved",
    "denied",
    "approval_required",
    "merged",
    "refused",
];
const DECISION_OUTCOME_VARIANTS: &[&str] = &["allow", "deny", "ask"];
const DECISION_SOURCE_VARIANTS: &[&str] = &["rule", "mode_default"];
const EFFECT_CLASS_VARIANTS: &[&str] = &["read", "write", "exec", "network"];
const RESOLUTION_VARIANTS: &[&str] = &["allow", "deny", "cancelled"];
const INDETERMINATE_ORIGIN_VARIANTS: &[&str] = &["live", "recovery"];
const START_STATE_VARIANTS: &[&str] = &["not_started", "started", "unknown"];
const MODEL_DELIVERY_VARIANTS: &[&str] = &["deliver", "fatal", "none"];
const CONSTRUCTION_STAGE_VARIANTS: &[&str] = &["glob_expansion", "argv_build", "spawn"];
const RULE_EFFECT_VARIANTS: &[&str] = &["allow", "ask", "deny"];
const RESOLVE_RESOLUTION_VARIANTS: &[&str] = &["allow_once", "deny"];

/// Every named wire shape, in declaration order (the generated TS keeps this
/// order, so the artifact is reproducible byte-for-byte).
pub const SHAPES: &[Shape] = &[
    Shape {
        name: "U64String",
        doc: "An ordinal-bearing u64 as a base-10 JSON string (A-2). `ordinal`,\n * `call_index` and `through_ordinal` are all this type; a plain JSON number\n * is a wire violation.",
        kind: ShapeKind::Custom("string"),
        fields: &[],
    },
    Shape {
        name: "Json",
        doc: "JSON carried verbatim. The inner shape is owned elsewhere (S4\n * `targets`, canonical `args`, `reason.params`, `capability_set`); the\n * renderer/consumer treats it as opaque.",
        kind: ShapeKind::Custom("unknown"),
        fields: &[],
    },
    Shape {
        name: "SliceEvent",
        doc: "The `slice-v1` event set (RULING-4 + the `turn.*` carriers, AUD-8).\n * `lineage`, `turn.start`, `step.context` and `turn.end` are S1 kinds with no\n * 1:1 frame. Retired names are rejected, never aliased (D-43).",
        kind: ShapeKind::Union(&EVENTS_TABLE),
        fields: &[],
    },
    Shape {
        name: "ToolResultStatus",
        doc: "`tool.result.status` — the five call statuses (RULING-3 as amended).",
        kind: ShapeKind::Union(TOOL_RESULT_STATUS_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "TurnStatus",
        doc: "`turn.completed.status` (MGR-3 reduction, S5 §5).",
        kind: ShapeKind::Union(TURN_STATUS_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "Actor",
        doc: "Envelope `actor` vocabulary (S1.3).",
        kind: ShapeKind::Union(ACTOR_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "AbortCause",
        doc: "`tool.result.abort_cause` (IN-3): present only on `aborted`.",
        kind: ShapeKind::Union(ABORT_CAUSE_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "ActorKind",
        doc: "Authority-record actor kind (MGR-7).",
        kind: ShapeKind::Union(ACTOR_KIND_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "DecisionState",
        doc: "`DecisionRecord.state` (S2 lifecycle).",
        kind: ShapeKind::Union(DECISION_STATE_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "DecisionOutcome",
        doc: "`authority.decided.outcome` (RULING-4).",
        kind: ShapeKind::Union(DECISION_OUTCOME_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "DecisionSource",
        doc: "`authority.decided.source` (RULING-4).",
        kind: ShapeKind::Union(DECISION_SOURCE_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "EffectClass",
        doc: "RULING-7 / S4 effect classes.",
        kind: ShapeKind::Union(EFFECT_CLASS_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "Resolution",
        doc: "`approval.resolved.resolution` (RATIFY-3 as amended).",
        kind: ShapeKind::Union(RESOLUTION_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "IndeterminateOrigin",
        doc: "Where an `indeterminate` result was closed.",
        kind: ShapeKind::Union(INDETERMINATE_ORIGIN_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "StartState",
        doc: "Executor-evidenced start state (not witness-derived).",
        kind: ShapeKind::Union(START_STATE_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "ModelDelivery",
        doc: "The A-3 delivery column value carried on a result.",
        kind: ShapeKind::Union(MODEL_DELIVERY_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "ConstructionStage",
        doc: "`ExecutorEvidence.preflight.construction_stage` (MGR-13).",
        kind: ShapeKind::Union(CONSTRUCTION_STAGE_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "RuleEffect",
        doc: "Rule effects (RULING-1).",
        kind: ShapeKind::Union(RULE_EFFECT_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "WireErrorCode",
        doc: "The closed wire error-code vocabulary (A-4). Canonical spellings\n\
         * only: S1 §4a and S2 authority names are aliases and are mapped by the\n\
         * engine before they reach the wire, so a client never sees one.",
        kind: ShapeKind::ErrorCode,
        fields: &[],
    },
    Shape {
        name: "ChildSpawned",
        doc: "`ExecutorEvidence.child_spawned` (MGR-13): the JSON booleans, or\n * the string `\"unknown\"` when the engine cannot tell whether the child\n * started.",
        kind: ShapeKind::Custom("true | false | \"unknown\""),
        fields: &[],
    },
    Shape {
        name: "ContentValue",
        doc: "`ContentValue` (MGR-13): inline text up to 64 KiB, or a digest ref\n * above it (EXEC-39).",
        kind: ShapeKind::Custom(
            "{ kind: \"inline\"; text: string } | { kind: \"stored\"; digest: string; byte_len: number }",
        ),
        fields: &[],
    },
    Shape {
        name: "Explanation",
        doc: "Compiled explanation (S2: the reason catalog is the sole source).",
        kind: ShapeKind::Object,
        fields: &[req("text", Ty::Str)],
    },
    Shape {
        name: "DecisionReason",
        doc: "`DecisionRecord.reason`. `code` stays a free string: the S2 reason\n * catalog is not a wire error-code union (EXEC-14, A-4).",
        kind: ShapeKind::Object,
        fields: &[
            req("code", Ty::Str),
            req("params", Ty::Json),
            req("explanation", Ty::Ref("Explanation")),
        ],
    },
    Shape {
        name: "ActorRef",
        doc: "Actor reference (MGR-7). A resolution's reviewer is `stable_id`.",
        kind: ShapeKind::Object,
        fields: &[
            req("kind", Ty::Ref("ActorKind")),
            req("stable_id", Ty::Str),
        ],
    },
    Shape {
        name: "DecisionRecord",
        doc: "The S2 DecisionRecord, embedded verbatim in `authority.decided`,\n * `approval.resolved` and `tool.result` (AUD-3). `decision_id` is stable\n * across the chain (IN-7, A-7); `previous_decision_id` is a decision-scoped\n * grouping marker, not a predecessor link.",
        kind: ShapeKind::Object,
        fields: &[
            req("decision_id", Ty::Str),
            req("state", Ty::Ref("DecisionState")),
            req("reason", Ty::Ref("DecisionReason")),
            req("actor", Ty::Ref("ActorRef")),
            null("matched_rule_ref", Ty::Str),
            null("previous_decision_id", Ty::Str),
            req("occurred_at", Ty::Str),
            req("route", Ty::Str),
            req("approval_mode", Ty::Str),
            req("action_id", Ty::Str),
            null("policy_ref", Ty::Str),
        ],
    },
    Shape {
        name: "MatchedRulePreview",
        doc: "`ApprovalPreview.matched_rule`: ref plus source file (RULING-7 #8).",
        kind: ShapeKind::Object,
        fields: &[req("rule_ref", Ty::Str), req("source", Ty::Str)],
    },
    Shape {
        name: "ApprovalPreview",
        doc: "The RULING-7 `ApprovalPreview`, stored verbatim and rendered from\n * the record only: `call_id`, `decision_id`, `tool_name`, `action_summary`,\n * `args_preview`, `args_truncated`, `args_digest`, `effect_class`, `targets`,\n * `matched_rule`, `requested_at`. `targets` shape is owned by S4.",
        kind: ShapeKind::Object,
        fields: &[
            req("call_id", Ty::Str),
            req("decision_id", Ty::Str),
            req("tool_name", Ty::Str),
            req("action_summary", Ty::Str),
            req("args_preview", Ty::Str),
            req("args_truncated", Ty::Bool),
            req("args_digest", Ty::Str),
            req("effect_class", Ty::Ref("EffectClass")),
            req("targets", Ty::Json),
            null("matched_rule", Ty::Ref("MatchedRulePreview")),
            req("requested_at", Ty::Str),
        ],
    },
    Shape {
        name: "RuleMatch",
        doc: "A rule match retained as executor evidence (S4 §6.2).",
        kind: ShapeKind::Object,
        fields: &[
            req("rule_ref", Ty::Str),
            req("effect", Ty::Ref("RuleEffect")),
            req("source", Ty::Str),
        ],
    },
    Shape {
        name: "EvidencePolicy",
        doc: "`ExecutorEvidence.policy`: the configured constants as they applied.\n * `seccomp` is a label, never a claim of enforcement.",
        kind: ShapeKind::Object,
        fields: &[
            req("read_roots", Ty::Arr(ArrTy::Str)),
            req("network", Ty::Bool),
            req("seccomp", Ty::Str),
        ],
    },
    Shape {
        name: "HelperEvidence",
        doc: "`ExecutorEvidence.helper`. Absent under `platform_deferred`.",
        kind: ShapeKind::Object,
        fields: &[req("identity", Ty::Str), req("verified", Ty::Bool)],
    },
    Shape {
        name: "PreflightEvidence",
        doc: "`ExecutorEvidence.preflight`: populated only on a preflight failure.",
        kind: ShapeKind::Object,
        fields: &[
            opt("construction_stage", Ty::Ref("ConstructionStage")),
            opt("failure_class", Ty::Str),
        ],
    },
    Shape {
        name: "ChildOutcome",
        doc: "`ChildOutcome` (MGR-13): branch on the code, never on display text.",
        kind: ShapeKind::Object,
        fields: &[
            null("exit_code", Ty::Num),
            null("signal", Ty::Num),
            req("stdout", Ty::Ref("ContentValue")),
            req("stderr", Ty::Ref("ContentValue")),
        ],
    },
    Shape {
        name: "ExecutorEvidence",
        doc: "`ExecutorEvidence` (MGR-13) — the executor half of\n * `GoverningDecision`, riding in `tool.result.executor_evidence` (S4 §4).\n * Inapplicable fields are encoded absent, never faked.",
        kind: ShapeKind::Object,
        fields: &[
            req("backend", Ty::Str),
            req("capability_set", Ty::Json),
            req("policy", Ty::Ref("EvidencePolicy")),
            opt("helper", Ty::Ref("HelperEvidence")),
            req("protected_path_violations", Ty::Arr(ArrTy::Str)),
            req("matches", Ty::Arr(ArrTy::Ref("RuleMatch"))),
            req("preflight", Ty::Ref("PreflightEvidence")),
            req("child_spawned", Ty::Ref("ChildSpawned")),
            opt("child", Ty::Ref("ChildOutcome")),
        ],
    },
    Shape {
        name: "OperationError",
        doc: "`tool.result.error` / `turn.completed.error`. `code` is the A-4\n * wire vocabulary (aliases are rejected, never passed through);\n * `related_ids` cite the causal predecessor event ids.",
        kind: ShapeKind::Object,
        fields: &[
            req("code", Ty::Ref("WireErrorCode")),
            req("message", Ty::Str),
            req("retryable", Ty::Bool),
            req("related_ids", Ty::Arr(ArrTy::Str)),
        ],
    },
    Shape {
        name: "TurnStartedPayload",
        doc: "`turn.started` payload (S5 §3): backed by `turn.start`, offered after\n * `step.context` is durable.",
        kind: ShapeKind::Object,
        fields: &[
            req("input_item_count", Ty::Num),
            req("model", Ty::Str),
            req("step_id", Ty::Str),
        ],
    },
    Shape {
        name: "ToolCallPayload",
        doc: "`tool.call` payload: `{call_id, tool_name, args}` — the record's\n * canonical arguments verbatim.",
        kind: ShapeKind::Object,
        fields: &[
            req("call_id", Ty::Str),
            req("tool_name", Ty::Str),
            req("args", Ty::Json),
        ],
    },
    Shape {
        name: "AuthorityDecidedPayload",
        doc: "`authority.decided` payload: outcome, source and the ordered\n * `decision_chain` (proposed first, verbatim, S5 §3b).",
        kind: ShapeKind::Object,
        fields: &[
            req("outcome", Ty::Ref("DecisionOutcome")),
            req("source", Ty::Ref("DecisionSource")),
            req("decision_chain", Ty::Arr(ArrTy::Ref("DecisionRecord"))),
        ],
    },
    Shape {
        name: "ApprovalResolvedPayload",
        doc: "`approval.resolved` payload: resolution, actor and the follow-up\n * `decision_chain` (S5 §3b). `reason` is absent when the reviewer gave none.",
        kind: ShapeKind::Object,
        fields: &[
            req("resolution", Ty::Ref("Resolution")),
            req("actor", Ty::Ref("ActorRef")),
            opt("reason", Ty::Str),
            req("decision_chain", Ty::Arr(ArrTy::Ref("DecisionRecord"))),
        ],
    },
    Shape {
        name: "ToolStartedPayload",
        doc: "`tool.started` payload — the durable start witness: authorized to\n * attempt, never proof that execution began (I-05b).",
        kind: ShapeKind::Object,
        fields: &[req("call_id", Ty::Str)],
    },
    Shape {
        name: "ToolResultPayload",
        doc: "`tool.result` payload (S5 §3 nullability): `succeeded` ⇒ `error=null`\n * and `output` a `ContentValue`; any other status ⇒ `output=null` plus an\n * `OperationError`. `abort_cause` only on `aborted`, `executor_evidence`\n * only when the record carries it.",
        kind: ShapeKind::Object,
        fields: &[
            req("status", Ty::Ref("ToolResultStatus")),
            opt("abort_cause", Ty::Ref("AbortCause")),
            null("output", Ty::Json),
            null("error", Ty::Ref("OperationError")),
            opt("executor_evidence", Ty::Ref("ExecutorEvidence")),
            null("authority_terminal", Ty::Ref("DecisionRecord")),
        ],
    },
    Shape {
        name: "TurnCompletedPayload",
        doc: "`turn.completed` payload: `error` is null iff `succeeded`.",
        kind: ShapeKind::Object,
        fields: &[
            req("status", Ty::Ref("TurnStatus")),
            null("error", Ty::Ref("OperationError")),
        ],
    },
    // ---- requests / responses (S5 §2) ----
    Shape {
        name: "WireVersion",
        doc: "The negotiated protocol version (`selected_version`).",
        kind: ShapeKind::Object,
        fields: &[req("major", Ty::Num), opt("minor", Ty::Num)],
    },
    Shape {
        name: "HelloLimits",
        doc: "Server-advertised limits (S5 §1).",
        kind: ShapeKind::Object,
        fields: &[
            req("max_frame_bytes", Ty::Num),
            req("max_json_depth", Ty::Num),
            req("id_max_bytes", Ty::Num),
            req("deny_reason_max_bytes", Ty::Num),
            req("reserved_slots", Ty::Num),
        ],
    },
    Shape {
        name: "HelloParams",
        doc: "`hello` params: `version.major` must equal the advertised major\n * (unknown minor fields are ignored, N1).",
        kind: ShapeKind::Object,
        fields: &[
            req("version", Ty::Ref("WireVersion")),
            opt("limits", Ty::Ref("HelloLimits")),
        ],
    },
    Shape {
        name: "HelloAck",
        doc: "The `hello` result (S5 §1): no `seq`, no credit; `features` is empty\n * in the slice.",
        kind: ShapeKind::Object,
        fields: &[
            req("selected_version", Ty::Ref("WireVersion")),
            req("event_set", Ty::Str),
            req("schema_hash", Ty::Str),
            req("features", Ty::Arr(ArrTy::Str)),
            req("limits", Ty::Ref("HelloLimits")),
        ],
    },
    Shape {
        name: "SessionCreateParams",
        doc: "`session.create` params: none in the slice (the data root is a CLI\n * argument, EXEC-53).",
        kind: ShapeKind::Object,
        fields: &[],
    },
    Shape {
        name: "SessionCreateResult",
        doc: "`session.create` result.",
        kind: ShapeKind::Object,
        fields: &[req("session_id", Ty::Str), req("ledger_path", Ty::Str)],
    },
    Shape {
        name: "TurnStartParams",
        doc: "`turn.start` params.",
        kind: ShapeKind::Object,
        fields: &[opt("model", Ty::Str)],
    },
    Shape {
        name: "TurnStartResult",
        doc: "The `turn.start` result (A-9): emitted at TURN END, carrying the\n * turn's terminal status — frames flow while the request is processing.",
        kind: ShapeKind::Object,
        fields: &[
            req("turn_id", Ty::Str),
            req("step_id", Ty::Str),
            req("status", Ty::Ref("TurnStatus")),
            opt("error", Ty::Ref("OperationError")),
        ],
    },
    Shape {
        name: "DenyResponse",
        doc: "`interaction.resolve` deny response; the optional free text is\n * carried into `approval.resolved.reason`.",
        kind: ShapeKind::Object,
        fields: &[opt("reason", Ty::Str)],
    },
    Shape {
        name: "ResolveResponse",
        doc: "The `interaction.resolve` affordance (RULING-7): `allow_once` or\n * `{deny: {reason?}}` — never a boolean.",
        kind: ShapeKind::Custom("\"allow_once\" | { deny: DenyResponse }"),
        fields: &[],
    },
    Shape {
        name: "ResolveResolution",
        doc: "`interaction.resolve` result `resolution`.",
        kind: ShapeKind::Union(RESOLVE_RESOLUTION_VARIANTS),
        fields: &[],
    },
    Shape {
        name: "InteractionResolveParams",
        doc: "`interaction.resolve` params (S5 §2, MGR-13): the echo triple plus the\n * response. A mismatched echo → `approval_mismatch`, nothing appended.",
        kind: ShapeKind::Object,
        fields: &[
            req("call_id", Ty::Str),
            req("decision_id", Ty::Str),
            req("args_digest", Ty::Str),
            req("response", Ty::Ref("ResolveResponse")),
        ],
    },
    Shape {
        name: "InteractionResolveResult",
        doc: "`interaction.resolve` result.",
        kind: ShapeKind::Object,
        fields: &[req("resolution", Ty::Ref("ResolveResolution"))],
    },
    Shape {
        name: "EventAckParams",
        doc: "`event.ack` params: the highest contiguous `seq` acked.",
        kind: ShapeKind::Object,
        fields: &[req("through_seq", Ty::Num)],
    },
    Shape {
        name: "EventAckResult",
        doc: "`event.ack` result.",
        kind: ShapeKind::Object,
        fields: &[req("through_seq", Ty::Num)],
    },
    Shape {
        name: "WireError",
        doc: "The `error` half of a response (S5 §2).",
        kind: ShapeKind::Object,
        fields: &[
            req("code", Ty::Ref("WireErrorCode")),
            req("message", Ty::Str),
            opt("data", Ty::Json),
        ],
    },
];

/// The `slice-v1` event set, in wire order. Single owner: `protocol::EVENTS`,
/// [`SHAPES`]'s `SliceEvent` union and the generated TS all read this table.
static EVENTS_TABLE: [&str; 8] = [
    "turn.started",
    "tool.call",
    "authority.decided",
    "approval.requested",
    "approval.resolved",
    "tool.started",
    "tool.result",
    "turn.completed",
];

/// The `slice-v1` event set, in wire order (a copy of the table above so
/// `protocol::EVENTS` keeps its `const` shape).
pub const EVENTS: [&str; 8] = EVENTS_TABLE;

/// The frame envelope (S5 §1). `payload` is overridden per event by the
/// generated TS `FrameOf<E>`; every other field is the same on every frame.
pub const ENVELOPE: &[Field] = &[
    req("seq", Ty::Num),
    req("event", Ty::Ref("SliceEvent")),
    req("schema_version", Ty::Str),
    req("event_id", Ty::Str),
    req("ts", Ty::Str),
    req("ordinal", Ty::U64Str),
    null("correlation_id", Ty::Str),
    null("caused_by", Ty::Str),
    req("payload", Ty::Json),
];

/// One frame's payload: the event name and the shape of its payload object.
#[derive(Debug, Clone, Copy)]
pub struct FramePayload {
    pub event: &'static str,
    pub shape: &'static str,
}

/// Every frame the slice can emit, in wire order (== [`EVENTS`]).
pub const FRAME_PAYLOADS: &[FramePayload] = &[
    FramePayload {
        event: "turn.started",
        shape: "TurnStartedPayload",
    },
    FramePayload {
        event: "tool.call",
        shape: "ToolCallPayload",
    },
    FramePayload {
        event: "authority.decided",
        shape: "AuthorityDecidedPayload",
    },
    // The approval.requested payload IS the RULING-7 ApprovalPreview, stored
    // verbatim (T3a S1 §3) — one shape, no second field list.
    FramePayload {
        event: "approval.requested",
        shape: "ApprovalPreview",
    },
    FramePayload {
        event: "approval.resolved",
        shape: "ApprovalResolvedPayload",
    },
    FramePayload {
        event: "tool.started",
        shape: "ToolStartedPayload",
    },
    FramePayload {
        event: "tool.result",
        shape: "ToolResultPayload",
    },
    FramePayload {
        event: "turn.completed",
        shape: "TurnCompletedPayload",
    },
];

/// One request/response message: method name, params shape and result shape.
#[derive(Debug, Clone, Copy)]
pub struct Message {
    pub method: &'static str,
    pub params: &'static str,
    pub result: &'static str,
}

/// The request set of the slice (S5 §2). `content.read` is not reachable in
/// the one-call slice (no `Stored` output is read back by the webview), so it
/// is not part of this binding's request set.
pub const REQUESTS: &[Message] = &[
    Message {
        method: "hello",
        params: "HelloParams",
        result: "HelloAck",
    },
    Message {
        method: "session.create",
        params: "SessionCreateParams",
        result: "SessionCreateResult",
    },
    Message {
        method: "turn.start",
        params: "TurnStartParams",
        result: "TurnStartResult",
    },
    Message {
        method: "interaction.resolve",
        params: "InteractionResolveParams",
        result: "InteractionResolveResult",
    },
    Message {
        method: "event.ack",
        params: "EventAckParams",
        result: "EventAckResult",
    },
];

/// Look up a shape by its TS name.
pub fn shape(name: &str) -> Option<&'static Shape> {
    SHAPES.iter().find(|s| s.name == name)
}

/// Look up the payload spec for an event.
pub fn frame_payload(event: &str) -> Option<&'static FramePayload> {
    FRAME_PAYLOADS.iter().find(|p| p.event == event)
}

/// The field names of a shape, with the `?` suffix A-4's schema description
/// uses for an absent-when-empty field.
pub fn field_names(shape_name: &str) -> Vec<String> {
    shape(shape_name)
        .unwrap_or_else(|| panic!("unknown shape {shape_name}"))
        .fields
        .iter()
        .map(|f| match f.presence {
            Presence::Optional => format!("{}?", f.name),
            _ => f.name.to_owned(),
        })
        .collect()
}

/// The canonical JSON description of the slice-v1 frame schema that `hello`
/// carries a hash of (both ends compare it before a frame flows).
///
/// Derived from this module — the field lists are the model's, not a second
/// literal — so a model edit moves the hash and both sides must renegotiate.
pub fn schema_description() -> Value {
    let mut payloads = serde_json::Map::new();
    for p in FRAME_PAYLOADS {
        payloads.insert(p.event.to_owned(), json!(field_names(p.shape)));
    }
    json!({
        "event_set": crate::protocol::EVENT_SET,
        "envelope": ENVELOPE.iter().map(|f| f.name).collect::<Vec<_>>(),
        "payloads": Value::Object(payloads),
        "operation_error": field_names("OperationError"),
        "statuses": {
            "tool.result": union_variants("ToolResultStatus"),
            "turn.completed": union_variants("TurnStatus"),
        },
        "retired": crate::protocol::RETIRED_EVENTS,
    })
}

/// The string-literal variants of a union shape.
pub fn union_variants(name: &str) -> Vec<&'static str> {
    match shape(name).unwrap_or_else(|| panic!("unknown shape {name}")).kind {
        ShapeKind::Union(v) => v.to_vec(),
        _ => panic!("{name} is not a string-literal union"),
    }
}
