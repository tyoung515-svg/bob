// GENERATED FILE - DO NOT EDIT.
//
// Slice-v1 wire protocol (S5 §1-§3): the frame envelope, the eight
// `slice-v1` payloads, the request/response messages and the closed
// error-code vocabulary.
//
// Source of truth: the engine's normative wire schema
// `engine/src/wire_schema.rs` — the same model `hello`'s `schema_hash`
// is computed over. `WIRE_SCHEMA_HASH` below is that hash.
//
// Regenerate (from `shell/`):
//   cargo run -q --manifest-path ../engine/Cargo.toml --bin bob-wiregen -- --out src/protocol.ts
// Verify only:
//   npm run check:protocol
//
// Drift is a test failure: `engine/tests/e11_codegen.rs` regenerates
// this file in `cargo test` and fails if the bytes differ. Do not edit
// this file by hand (A-10 / AT-S5-03, EXEC-49).

/// The slice schema version (RULING-8).
export const SCHEMA_VERSION = "1.0";
/// Negotiated protocol version: `selected_version` is major 2 (S5 §1).
export const PROTOCOL_MAJOR = 2;
export const PROTOCOL_MINOR = 0;
/// The event-set name (RULING-4): additive changes allowed, renames need a ruling.
export const EVENT_SET = "slice-v1";
/// sha256 over the JCS of the engine's schema description. Both ends
/// compare it in `hello`/`HelloAck` before a frame flows.
export const WIRE_SCHEMA_HASH = "sha256:efb6ed414ae8eace49db4622d91c0b02eb81166fa3af1a8195a7915968af3555";
/// The `slice-v1` event set, in wire order.
export const SLICE_EVENTS = [
  "turn.started",
  "tool.call",
  "authority.decided",
  "approval.requested",
  "approval.resolved",
  "tool.started",
  "tool.result",
  "turn.completed",
] as const;
/// Retired for the slice, no alias (D-43).
export const RETIRED_EVENTS = [
  "stream.started",
  "stream.completed",
  "item.started",
  "item.completed",
  "notice.diagnostic",
  "interaction.requested",
  "tool.completed",
] as const;
/// Carried limits (S5 §1/§3, slice-selected values).
export const MAX_FRAME_BYTES = 1048576;
export const MAX_JSON_DEPTH = 32;
export const ID_MAX_BYTES = 128;
export const DENY_REASON_MAX_BYTES = 1024;

/**
 * An ordinal-bearing u64 as a base-10 JSON string (A-2). `ordinal`,
 * `call_index` and `through_ordinal` are all this type; a plain JSON number
 * is a wire violation.
 */
export type U64String = string;

/**
 * JSON carried verbatim. The inner shape is owned elsewhere (S4
 * `targets`, canonical `args`, `reason.params`, `capability_set`); the
 * renderer/consumer treats it as opaque.
 */
export type Json = unknown;

/**
 * The `slice-v1` event set (RULING-4 + the `turn.*` carriers, AUD-8).
 * `lineage`, `turn.start`, `step.context` and `turn.end` are S1 kinds with no
 * 1:1 frame. Retired names are rejected, never aliased (D-43).
 */
export type SliceEvent =
  | "turn.started"
  | "tool.call"
  | "authority.decided"
  | "approval.requested"
  | "approval.resolved"
  | "tool.started"
  | "tool.result"
  | "turn.completed";

/**
 * `tool.result.status` — the five call statuses (RULING-3 as amended).
 */
export type ToolResultStatus =
  | "succeeded"
  | "failed"
  | "denied"
  | "aborted"
  | "indeterminate";

/**
 * `turn.completed.status` (MGR-3 reduction, S5 §5).
 */
export type TurnStatus = "succeeded" | "failed" | "aborted" | "incomplete";

/**
 * Envelope `actor` vocabulary (S1.3).
 */
export type Actor = "engine" | "model" | "user" | "hook" | "subagent";

/**
 * `tool.result.abort_cause` (IN-3): present only on `aborted`.
 */
export type AbortCause =
  | "cancelled"
  | "witness_write_failed"
  | "preflight_failed"
  | "recovery_no_witness";

/**
 * Authority-record actor kind (MGR-7).
 */
export type ActorKind = "policy_engine" | "user";

/**
 * `DecisionRecord.state` (S2 lifecycle).
 */
export type DecisionState =
  | "proposed"
  | "approved"
  | "denied"
  | "approval_required"
  | "merged"
  | "refused";

/**
 * `authority.decided.outcome` (RULING-4).
 */
export type DecisionOutcome = "allow" | "deny" | "ask";

/**
 * `authority.decided.source` (RULING-4).
 */
export type DecisionSource = "rule" | "mode_default";

/**
 * RULING-7 / S4 effect classes.
 */
export type EffectClass = "read" | "write" | "exec" | "network";

/**
 * `approval.resolved.resolution` (RATIFY-3 as amended).
 */
export type Resolution = "allow" | "deny" | "cancelled";

/**
 * Where an `indeterminate` result was closed.
 */
export type IndeterminateOrigin = "live" | "recovery";

/**
 * Executor-evidenced start state (not witness-derived).
 */
export type StartState = "not_started" | "started" | "unknown";

/**
 * The A-3 delivery column value carried on a result.
 */
export type ModelDelivery = "deliver" | "fatal" | "none";

/**
 * `ExecutorEvidence.preflight.construction_stage` (MGR-13).
 */
export type ConstructionStage = "glob_expansion" | "argv_build" | "spawn";

/**
 * Rule effects (RULING-1).
 */
export type RuleEffect = "allow" | "ask" | "deny";

/**
 * The closed wire error-code vocabulary (A-4). Canonical spellings
 * only: S1 §4a and S2 authority names are aliases and are mapped by the
 * engine before they reach the wire, so a client never sees one.
 */
export type WireErrorCode =
  | "version_mismatch"
  | "method_not_supported"
  | "invalid_request"
  | "invalid_limits"
  | "approval_mismatch"
  | "approval_closed"
  | "internal_error"
  | "exec_forbidden"
  | "approval_rejected"
  | "capability_revoked"
  | "cancelled"
  | "witness_write_failed"
  | "isolation_unavailable"
  | "helper_verification_failed"
  | "sandbox_launch_failed"
  | "execution_failed"
  | "cancelled_after_start"
  | "execution_supervision_lost"
  | "incorporation_refused"
  | "unknown_tool"
  | "router_rejected"
  | "readiness_failed"
  | "outcome_unknown"
  | "recovered_unwitnessed"
  | "interrupted_recovered"
  | "missing_verdict"
  | "capability_undeclared"
  | "action_digest_mismatch"
  | "authority_snapshot_mismatch"
  | "policy_snapshot_mismatch"
  | "scope_mismatch"
  | "integration_invariant_failed"
  | "proposed"
  | "approval_route_selected"
  | "explicit_allow"
  | "reviewer_approved"
  | "effect_incorporated"
  | "policy_unknown"
  | "preflight_unavailable"
  | "platform_deferred"
  | "mode_unsupported";

/**
 * `ExecutorEvidence.child_spawned` (MGR-13): the JSON booleans, or
 * the string `"unknown"` when the engine cannot tell whether the child
 * started.
 */
export type ChildSpawned = true | false | "unknown";

/**
 * `ContentValue` (MGR-13): inline text up to 64 KiB, or a digest ref
 * above it (EXEC-39).
 */
export type ContentValue = { kind: "inline"; text: string } | { kind: "stored"; digest: string; byte_len: number };

/**
 * Compiled explanation (S2: the reason catalog is the sole source).
 */
export type Explanation = {
  text: string;
};

/**
 * `DecisionRecord.reason`. `code` stays a free string: the S2 reason
 * catalog is not a wire error-code union (EXEC-14, A-4).
 */
export type DecisionReason = {
  code: string;
  params: Json;
  explanation: Explanation;
};

/**
 * Actor reference (MGR-7). A resolution's reviewer is `stable_id`.
 */
export type ActorRef = {
  kind: ActorKind;
  stable_id: string;
};

/**
 * The S2 DecisionRecord, embedded verbatim in `authority.decided`,
 * `approval.resolved` and `tool.result` (AUD-3). `decision_id` is stable
 * across the chain (IN-7, A-7); `previous_decision_id` is a decision-scoped
 * grouping marker, not a predecessor link.
 */
export type DecisionRecord = {
  decision_id: string;
  state: DecisionState;
  reason: DecisionReason;
  actor: ActorRef;
  matched_rule_ref: string | null;
  previous_decision_id: string | null;
  occurred_at: string;
  route: string;
  approval_mode: string;
  action_id: string;
  policy_ref: string | null;
};

/**
 * `ApprovalPreview.matched_rule`: ref plus source file (RULING-7 #8).
 */
export type MatchedRulePreview = {
  rule_ref: string;
  source: string;
};

/**
 * The RULING-7 `ApprovalPreview`, stored verbatim and rendered from
 * the record only: `call_id`, `decision_id`, `tool_name`, `action_summary`,
 * `args_preview`, `args_truncated`, `args_digest`, `effect_class`, `targets`,
 * `matched_rule`, `requested_at`. `targets` shape is owned by S4.
 */
export type ApprovalPreview = {
  call_id: string;
  decision_id: string;
  tool_name: string;
  action_summary: string;
  args_preview: string;
  args_truncated: boolean;
  args_digest: string;
  effect_class: EffectClass;
  targets: Json;
  matched_rule: MatchedRulePreview | null;
  requested_at: string;
};

/**
 * A rule match retained as executor evidence (S4 §6.2).
 */
export type RuleMatch = {
  rule_ref: string;
  effect: RuleEffect;
  source: string;
};

/**
 * `ExecutorEvidence.policy`: the configured constants as they applied.
 * `seccomp` is a label, never a claim of enforcement.
 */
export type EvidencePolicy = {
  read_roots: string[];
  network: boolean;
  seccomp: string;
};

/**
 * `ExecutorEvidence.helper`. Absent under `platform_deferred`.
 */
export type HelperEvidence = {
  identity: string;
  verified: boolean;
};

/**
 * `ExecutorEvidence.preflight`: populated only on a preflight failure.
 */
export type PreflightEvidence = {
  construction_stage?: ConstructionStage;
  failure_class?: string;
};

/**
 * `ChildOutcome` (MGR-13): branch on the code, never on display text.
 */
export type ChildOutcome = {
  exit_code: number | null;
  signal: number | null;
  stdout: ContentValue;
  stderr: ContentValue;
};

/**
 * `ExecutorEvidence` (MGR-13) — the executor half of
 * `GoverningDecision`, riding in `tool.result.executor_evidence` (S4 §4).
 * Inapplicable fields are encoded absent, never faked.
 */
export type ExecutorEvidence = {
  backend: string;
  capability_set: Json;
  policy: EvidencePolicy;
  helper?: HelperEvidence;
  protected_path_violations: string[];
  matches: RuleMatch[];
  preflight: PreflightEvidence;
  child_spawned: ChildSpawned;
  child?: ChildOutcome;
};

/**
 * `tool.result.error` / `turn.completed.error`. `code` is the A-4
 * wire vocabulary (aliases are rejected, never passed through);
 * `related_ids` cite the causal predecessor event ids.
 */
export type OperationError = {
  code: WireErrorCode;
  message: string;
  retryable: boolean;
  related_ids: string[];
};

/**
 * `turn.started` payload (S5 §3): backed by `turn.start`, offered after
 * `step.context` is durable.
 */
export type TurnStartedPayload = {
  input_item_count: number;
  model: string;
  step_id: string;
};

/**
 * `tool.call` payload: `{call_id, tool_name, args}` — the record's
 * canonical arguments verbatim.
 */
export type ToolCallPayload = {
  call_id: string;
  tool_name: string;
  args: Json;
};

/**
 * `authority.decided` payload: outcome, source and the ordered
 * `decision_chain` (proposed first, verbatim, S5 §3b).
 */
export type AuthorityDecidedPayload = {
  outcome: DecisionOutcome;
  source: DecisionSource;
  decision_chain: DecisionRecord[];
};

/**
 * `approval.resolved` payload: resolution, actor and the follow-up
 * `decision_chain` (S5 §3b). `reason` is absent when the reviewer gave none.
 */
export type ApprovalResolvedPayload = {
  resolution: Resolution;
  actor: ActorRef;
  reason?: string;
  decision_chain: DecisionRecord[];
};

/**
 * `tool.started` payload — the durable start witness: authorized to
 * attempt, never proof that execution began (I-05b).
 */
export type ToolStartedPayload = {
  call_id: string;
};

/**
 * `tool.result` payload (S5 §3 nullability): `succeeded` ⇒ `error=null`
 * and `output` a `ContentValue`; any other status ⇒ `output=null` plus an
 * `OperationError`. `abort_cause` only on `aborted`, `executor_evidence`
 * only when the record carries it.
 */
export type ToolResultPayload = {
  status: ToolResultStatus;
  abort_cause?: AbortCause;
  output: Json;
  error: OperationError | null;
  executor_evidence?: ExecutorEvidence;
  authority_terminal: DecisionRecord | null;
};

/**
 * `turn.completed` payload: `error` is null iff `succeeded`.
 */
export type TurnCompletedPayload = {
  status: TurnStatus;
  error: OperationError | null;
};

/**
 * The negotiated protocol version (`selected_version`).
 */
export type WireVersion = {
  major: number;
  minor?: number;
};

/**
 * Server-advertised limits (S5 §1).
 */
export type HelloLimits = {
  max_frame_bytes: number;
  max_json_depth: number;
  id_max_bytes: number;
  deny_reason_max_bytes: number;
  reserved_slots: number;
};

/**
 * `hello` params: `version.major` must equal the advertised major
 * (unknown minor fields are ignored, N1).
 */
export type HelloParams = {
  version: WireVersion;
  limits?: HelloLimits;
};

/**
 * The `hello` result (S5 §1): no `seq`, no credit; `features` is empty
 * in the slice.
 */
export type HelloAck = {
  selected_version: WireVersion;
  event_set: string;
  schema_hash: string;
  features: string[];
  limits: HelloLimits;
};

/**
 * `session.create` params: none in the slice (the data root is a CLI
 * argument, EXEC-53).
 */
export type SessionCreateParams = Record<string, never>;

/**
 * `session.create` result.
 */
export type SessionCreateResult = {
  session_id: string;
  ledger_path: string;
};

/**
 * `turn.start` params.
 */
export type TurnStartParams = {
  model?: string;
};

/**
 * The `turn.start` result (A-9): emitted at TURN END, carrying the
 * turn's terminal status — frames flow while the request is processing.
 */
export type TurnStartResult = {
  turn_id: string;
  step_id: string;
  status: TurnStatus;
  error?: OperationError;
};

/**
 * `interaction.resolve` deny response; the optional free text is
 * carried into `approval.resolved.reason`.
 */
export type DenyResponse = {
  reason?: string;
};

/**
 * The `interaction.resolve` affordance (RULING-7): `allow_once` or
 * `{deny: {reason?}}` — never a boolean.
 */
export type ResolveResponse = "allow_once" | { deny: DenyResponse };

/**
 * `interaction.resolve` result `resolution`.
 */
export type ResolveResolution = "allow_once" | "deny";

/**
 * `interaction.resolve` params (S5 §2, MGR-13): the echo triple plus the
 * response. A mismatched echo → `approval_mismatch`, nothing appended.
 */
export type InteractionResolveParams = {
  call_id: string;
  decision_id: string;
  args_digest: string;
  response: ResolveResponse;
};

/**
 * `interaction.resolve` result.
 */
export type InteractionResolveResult = {
  resolution: ResolveResolution;
};

/**
 * `event.ack` params: the highest contiguous `seq` acked.
 */
export type EventAckParams = {
  through_seq: number;
};

/**
 * `event.ack` result.
 */
export type EventAckResult = {
  through_seq: number;
};

/**
 * The `error` half of a response (S5 §2).
 */
export type WireError = {
  code: WireErrorCode;
  message: string;
  data?: Json;
};

/**
 * Payload shape per `slice-v1` event: the map a consumer switches on.
 */
export interface PayloadMap {
  "turn.started": TurnStartedPayload;
  "tool.call": ToolCallPayload;
  "authority.decided": AuthorityDecidedPayload;
  "approval.requested": ApprovalPreview;
  "approval.resolved": ApprovalResolvedPayload;
  "tool.started": ToolStartedPayload;
  "tool.result": ToolResultPayload;
  "turn.completed": TurnCompletedPayload;
}

/**
 * One frame of event `E`: the S5 §1 envelope, with that event's payload.
 */
export type FrameOf<E extends SliceEvent> = {
  seq: number;
  event: E;
  /** "1.0" (RULING-8). */
  schema_version: string;
  event_id: string;
  ts: string;
  /** u64-string, base 1 (A-2). */
  ordinal: U64String;
  correlation_id: string | null;
  caused_by: string | null;
  payload: PayloadMap[E];
};

/**
 * Any frame: a discriminated union on `event`. Comparing `f.event`
 * narrows `f.payload` to that event's shape, so a consumer needs no
 * helper and no cast.
 */
export type Frame = { [E in SliceEvent]: FrameOf<E> }[SliceEvent];

/**
 * One request's method name to its params shape (S5 §2).
 */
export interface RequestParamsMap {
  "hello": HelloParams;
  "session.create": SessionCreateParams;
  "turn.start": TurnStartParams;
  "interaction.resolve": InteractionResolveParams;
  "event.ack": EventAckParams;
}

/**
 * One request's method name to its result shape.
 */
export interface ResultMap {
  "hello": HelloAck;
  "session.create": SessionCreateResult;
  "turn.start": TurnStartResult;
  "interaction.resolve": InteractionResolveResult;
  "event.ack": EventAckResult;
}

export type Method = keyof RequestParamsMap;
export type WireResult = ResultMap[Method];

/**
 * Encode one request line. The shell never assembles a request object by
 * hand: the id is opaque, the method selects the params type.
 */
export function encodeRequest<M extends Method>(
  id: string,
  method: M,
  params: RequestParamsMap[M],
): string {
  return JSON.stringify({ id, method, params });
}

/**
 * A response line: exactly one of `result`/`error` answers a request
 * (S5 §2). Duplicate `request_id`s replay the same line (EXEC-43).
 */
export type ResponseLine =
  | { id: string; result: WireResult }
  | { id: string | null; error: WireError };

/**
 * One parsed wire line.
 */
export type ParsedLine =
  | { kind: "frame"; frame: Frame }
  | { kind: "response"; response: ResponseLine };

/**
 * Parse one NDJSON line into a frame or a response; `null` when the line
 * is not a wire object. A frame is recognised by its `event`, a response
 * by `result`/`error`; nothing is coerced or repaired.
 */
export function parseLine(raw: string): ParsedLine | null {
  let o: any;
  try {
    o = JSON.parse(raw);
  } catch {
    return null;
  }
  if (o === null || typeof o !== "object") return null;
  if (typeof o.event === "string") return { kind: "frame", frame: o as Frame };
  if (typeof o.error === "object" || "result" in o) {
    return { kind: "response", response: o as ResponseLine };
  }
  return null;
}
