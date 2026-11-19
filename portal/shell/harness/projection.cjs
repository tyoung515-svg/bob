"use strict";
// AT-1 projection + checksum (EXEC-48), the shape both the Rust engine test
// and this harness compute:
//
//   projection = {
//     frames: [ {seq, event, ordinal, correlation_id, caused_by, chain, status} ],
//     call:   {call_id, decided_outcome, decided_source, resolution, status,
//              error_code, terminal_state, output_digest, witness_present,
//              n_results}
//   }
//   checksum = "sha256:" + sha256(canonicalJson(projection))
//
// Volatile wire values (event_id, decision_id, ts, requested_at, absolute
// paths) are deliberately NOT in the projection: `event_id`s are uuidv4 and
// timestamps are wall-clock, so a checksum over them could never be pinned.
// What IS covered is everything that must survive the bindings verbatim --
// event name, ordinal, correlation, the R8 causal link (resolved to the
// ordinals it cites, "external" for the one legitimately framed-less
// predecessor), the DecisionRecord chain (state + reason code + matched rule
// ref per record) and the terminal status/output digest.
//
// The Rust side (engine/tests/e10_at1.rs) derives the same rows from the
// projection DB's `assertion` view; the Tauri IPC leg (shell/src-tauri
// bridge tests) derives them from the event-channel lines. All three must
// agree on the pinned checksum.

const crypto = require("node:crypto");

/** RFC 8785-shaped canonical JSON: sorted keys, no whitespace. Every value in
 * the projection is an object/array/string/number/bool/null, so this is
 * exactly JCS for this data. */
function canonicalJson(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "string") return JSON.stringify(v);
  if (typeof v === "number") {
    if (!Number.isFinite(v)) throw new Error("non-finite number in projection");
    return JSON.stringify(v);
  }
  if (typeof v === "boolean") return v ? "true" : "false";
  if (Array.isArray(v)) return "[" + v.map(canonicalJson).join(",") + "]";
  if (typeof v === "object") {
    const keys = Object.keys(v).sort();
    return (
      "{" +
      keys.map((k) => JSON.stringify(k) + ":" + canonicalJson(v[k])).join(",") +
      "}"
    );
  }
  throw new Error("unsupported value in projection: " + typeof v);
}

function checksum(projection) {
  return "sha256:" + crypto.createHash("sha256").update(canonicalJson(projection)).digest("hex");
}

function obj(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v) ? v : null;
}

function str(v) {
  return typeof v === "string" ? v : null;
}

/** One DecisionRecord -> the chain row both sides compare. */
function chainEntry(rec) {
  const r = obj(rec);
  if (r === null) return null;
  const reason = obj(r.reason);
  return {
    state: str(r.state),
    reason: reason === null ? null : str(reason.code),
    rule: str(r.matched_rule_ref),
  };
}

function chainOf(frame) {
  const p = obj(frame.payload) || {};
  if (frame.event === "authority.decided" || frame.event === "approval.resolved") {
    const chain = Array.isArray(p.decision_chain) ? p.decision_chain : [];
    return chain.map(chainEntry).filter((c) => c !== null);
  }
  if (frame.event === "tool.result") {
    const entry = chainEntry(p.authority_terminal);
    return entry === null ? [] : [entry];
  }
  return [];
}

function statusOf(frame) {
  const p = obj(frame.payload) || {};
  return frame.event === "tool.result" || frame.event === "turn.completed"
    ? str(p.status)
    : null;
}

/** Project an ordered frame list (S10 §2 reducer input) into the AT-1 shape. */
function projectFrames(frames) {
  const ordinalOf = new Map();
  for (const f of frames) ordinalOf.set(f.event_id, f.ordinal);

  const rows = frames.map((f) => {
    let causedBy = null;
    if (f.caused_by !== null && f.caused_by !== undefined) {
      causedBy = ordinalOf.has(f.caused_by) ? ordinalOf.get(f.caused_by) : "external";
    }
    return {
      seq: f.seq,
      event: f.event,
      ordinal: f.ordinal,
      correlation_id: f.correlation_id === undefined ? null : f.correlation_id,
      caused_by: causedBy,
      chain: chainOf(f),
      status: statusOf(f),
    };
  });

  const firstOf = (event) => frames.find((f) => f.event === event) || null;
  const callFrame = firstOf("tool.call");
  const decidedFrame = firstOf("authority.decided");
  const resolvedFrame = firstOf("approval.resolved");
  const resultFrame = firstOf("tool.result");
  const resultPayload = resultFrame ? obj(resultFrame.payload) || {} : {};
  const terminal = obj(resultPayload.authority_terminal);
  const terminalReason = terminal ? obj(terminal.reason) : null;
  const terminalParams = terminalReason ? obj(terminalReason.params) : null;
  const decidedPayload = decidedFrame ? obj(decidedFrame.payload) || {} : {};
  const resolvedPayload = resolvedFrame ? obj(resolvedFrame.payload) || {} : {};
  const callPayload = callFrame ? obj(callFrame.payload) || {} : {};
  const error = resultFrame ? obj(resultPayload.error) : null;

  const call = {
    call_id: callFrame ? str(callPayload.call_id) : null,
    decided_outcome: decidedFrame ? str(decidedPayload.outcome) : null,
    decided_source: decidedFrame ? str(decidedPayload.source) : null,
    resolution: resolvedFrame ? str(resolvedPayload.resolution) : null,
    status: resultFrame ? str(resultPayload.status) : null,
    error_code: error === null ? null : str(error.code),
    terminal_state: terminal === null ? null : str(terminal.state),
    // A-8: the merged target is the result's output_digest.
    output_digest: terminalParams === null ? null : str(terminalParams.target_id),
    witness_present: frames.some((f) => f.event === "tool.started"),
    n_results: frames.filter((f) => f.event === "tool.result").length,
  };

  return { frames: rows, call };
}

module.exports = { canonicalJson, checksum, projectFrames, chainOf, statusOf };
