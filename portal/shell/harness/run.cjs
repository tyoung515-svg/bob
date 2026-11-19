#!/usr/bin/env node
"use strict";
// E10 reducer / webview harness (item 1) + AT-1 projection-checksum leg
// (item 3) + fixture integrity (item 4).
//
// Mechanism chosen: a JS harness over the REAL reducer (shell/src/reducer.ts,
// loaded by workbench-ui-register.cjs) fed by committed wire transcripts captured
// from live bob-slice-cli runs, plus a fake Tauri event bus that enforces the
// real capability file. It is deterministic, runs headless in the normal
// verification battery, and fails when either E7 defect is reintroduced.
// A live-UI smoke (the E7 scheduled-task + UIA pattern) is not used: WebView2
// cannot initialise in a non-interactive Windows session (EXEC-51), so it
// cannot be part of the suite that must stay green.
//
// Run from shell/:  npm run test:harness
//   tsc --noEmit -p tsconfig.harness.json
//   node -r ./harness/workbench-ui-register.cjs harness/run.cjs

const fs = require("node:fs");
const path = require("node:path");

const { reducer, initial } = require("../src/reducer.ts");
const { parseLine } = require("../src/protocol.ts");
const { checksum, projectFrames, canonicalJson } = require("./projection.cjs");
const acl = require("./acl.cjs");

const REPO = path.resolve(__dirname, "..", "..");
const TRANSCRIPT_DIR = path.join(REPO, "engine", "tests", "fixtures", "transcripts");

// Every committed wire transcript, the engine fixture that produced it, and
// the cases that consume it. `fixture_integrity` fails when a committed
// transcript is missing here (unconsumed = failure) or an entry has no file.
const TRANSCRIPTS = [
  {
    file: "v0_allow.wire.jsonl",
    fixture: "allow.requests.jsonl",
    frames: 8,
    projection: "v0_allow.projection.json",
    checksum: "sha256:63f1ac4ffa07d329145475a0c6d4c0904a10e9d98e6106466250e153b9ff0df6",
    consumers: [
      "generated_protocol_matches_the_hello_schema_hash",
      "conn_becomes_connected_on_helloack",
      "first_frame_does_not_freeze_the_row",
      "at1_projection_checksum_allow",
      "acl_denied_listen_delivers_no_rows",
    ],
  },
  {
    file: "v0_deny.wire.jsonl",
    fixture: "deny.requests.jsonl",
    frames: 7,
    projection: "v0_deny.projection.json",
    checksum: "sha256:8b8827a725a580946950e97e9b4428e7b59d8d4233d52a88c69955fa8d4d09a7",
    consumers: [
      "generated_protocol_matches_the_hello_schema_hash",
      "deny_transcript_is_not_success_shaped",
      "at1_projection_checksum_deny",
    ],
  },
];

const cases = [];
function test(name, fn) {
  cases.push({ name, fn });
}

function eq(actual, expected, what) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error(`${what}: got ${a}, want ${b}`);
}
function ok(cond, what) {
  if (!cond) throw new Error(what);
}

function loadTranscript(entry) {
  const p = path.join(TRANSCRIPT_DIR, entry.file);
  const text = fs.readFileSync(p, "utf8");
  const items = [];
  for (const raw of text.split("\n")) {
    if (raw.trim() === "") continue;
    const parsed = parseLine(raw);
    ok(parsed !== null, `${entry.file}: unparseable line ${raw.slice(0, 60)}`);
    items.push(parsed);
  }
  const frames = items.filter((i) => i.kind === "frame").map((i) => i.frame);
  eq(frames.length, entry.frames, `${entry.file}: committed frame count`);
  return { items, frames };
}

/** Feed a transcript through the real reducer, exactly as App.tsx does:
 * spawn, then responses and frames in arrival order. */
function drive(items, opts = {}) {
  let s = reducer(initial, { t: "spawned", dataRoot: "C:\\temp\\e10-harness" });
  const connAfterResponse = [];
  let index = 0;
  for (const it of items) {
    if (it.kind === "response") {
      s = reducer(s, { t: "response", response: it.response });
      connAfterResponse.push({ id: it.response.id, conn: s.conn });
    } else {
      const frame = opts.mutateFrame ? opts.mutateFrame(it.frame, index) : it.frame;
      s = reducer(s, { t: "frame", frame });
    }
    index += 1;
  }
  return { state: s, connAfterResponse };
}

// ---------- item 4: fixture integrity ----------

test("fixture_integrity", () => {
  const files = fs
    .readdirSync(TRANSCRIPT_DIR)
    .filter((f) => f.endsWith(".jsonl") || f.endsWith(".wire.jsonl"));
  ok(files.length > 0, "no committed frame transcripts found");
  const known = new Set(TRANSCRIPTS.map((t) => t.file));
  const unconsumed = files.filter((f) => !known.has(f));
  if (unconsumed.length > 0) {
    throw new Error(
      `committed transcript(s) with no consuming test: ${unconsumed.join(", ")} — a transcript no test replays is a failure`
    );
  }
  const names = new Set(cases.map((c) => c.name));
  for (const t of TRANSCRIPTS) {
    ok(fs.existsSync(path.join(TRANSCRIPT_DIR, t.file)), `missing transcript ${t.file}`);
    ok(
      t.projection && fs.existsSync(path.join(TRANSCRIPT_DIR, t.projection)),
      `${t.file} has no committed AT-1 projection fixture`
    );
    ok(
      fs.existsSync(path.join(REPO, "engine", "tests", "fixtures", "s5", t.fixture)),
      `transcript ${t.file} names a fixture that does not exist: ${t.fixture}`
    );
    ok(t.consumers.length > 0, `${t.file} declares no consumer`);
    for (const c of t.consumers) {
      ok(names.has(c), `${t.file} declares consumer '${c}' but no such case exists`);
    }
  }
});

// ---------- item 1a: ACL (E7 defect #1) ----------

test("acl_grants_listen", () => {
  acl.assertCapabilitiesGrantListen(REPO);
  const wired = acl.assertClientWiresListen(REPO);
  eq(wired, acl.CHANNELS, "client.ts listen() channels");
});

test("acl_denied_listen_delivers_no_rows", async () => {
  const entry = TRANSCRIPTS[0];
  const { items, frames } = loadTranscript(entry);

  // Positive: the committed capability file is enough for the subscription and
  // every frame line reaches the reducer.
  const real = acl.assertCapabilitiesGrantListen(REPO);
  const goodBus = new acl.FakeEventBus(real);
  const delivered = [];
  await goodBus.listen("slice://line", (e) => delivered.push(e.payload));
  for (const it of items) {
    if (it.kind === "frame") goodBus.emit("slice://line", JSON.stringify(it.frame));
  }
  eq(delivered.length, frames.length, "frames delivered through the granted event bus");

  // Negative control: the same bus built from the capability file with the
  // listen grant removed must reject — this is the exact ACL denial that made
  // E7 render nothing, and it now fails the suite instead of the UI silently.
  const stripped = {
    ...real,
    permissions: real.permissions.filter((p) => p !== acl.LISTEN),
  };
  const badBus = new acl.FakeEventBus(stripped);
  let rejected = null;
  try {
    await badBus.listen("slice://line", () => {});
  } catch (e) {
    rejected = e;
  }
  ok(rejected !== null, "an ACL-denied listen() must reject");
  ok(
    String(rejected.message).includes(acl.LISTEN),
    `the rejection must name the missing grant: ${String(rejected.message)}`
  );
  eq(acl.listenChannels(REPO).includes("slice://line"), true, "client still subscribes to slice://line");
});

// ---------- item 1b: conn on HelloAck (EXEC-56) ----------

test("conn_becomes_connected_on_helloack", () => {
  const { items } = loadTranscript(TRANSCRIPTS[0]);
  const { state, connAfterResponse } = drive(items);
  eq(initial.conn, "idle", "initial connection state");
  eq(connAfterResponse[0], { id: "r1", conn: "connected" }, "hello result -> connected");
  eq(connAfterResponse[1].conn, "connected", "session.create keeps connected");
  const r3 = connAfterResponse.find((c) => c.id === "r3");
  eq(r3, { id: "r3", conn: "connected" }, "turn.start response keeps connected");
  eq(
    connAfterResponse.every((c) => c.conn === "connected"),
    true,
    "no response drops the connection back to connecting"
  );
  eq(state.conn, "connected", "final connection state");

  // A HelloAck error is not a connection: version_mismatch is terminal and
  // must never be overwritten by a later assignment.
  let s = reducer(initial, { t: "spawned", dataRoot: "x" });
  s = reducer(s, {
    t: "response",
    response: { id: "r1", error: { code: "version_mismatch", message: "major mismatch" } },
  });
  eq(s.conn, "version_mismatch", "hello error -> version_mismatch");
  s = reducer(s, { t: "exit", code: 4 });
  eq(s.conn, "version_mismatch", "exit keeps the terminal state");
});

// ---------- item 1c: C-5 first-frame exemption (E7 defect #2, A-11) ----------

test("first_frame_does_not_freeze_the_row", () => {
  const entry = TRANSCRIPTS[0];
  const { items, frames } = loadTranscript(entry);
  const { state } = drive(items);
  eq(state.rows.length, frames.length, "every frame renders a row (nothing frozen)");
  eq(state.frozen, false, "no freeze on a conformant run");
  eq(
    state.rows.filter((r) => r.violation).length,
    0,
    "no row is a protocol_violation"
  );

  // The exemption is exactly one frame wide: tool.call cites step.context,
  // which has no frame, so its cause resolves to "external".
  const callFrames = frames.filter((f) => f.correlation_id === "call-1");
  ok(callFrames.length === 6, "six call frames carry correlation_id call-1");
  const projection = projectFrames(frames);
  eq(projection.frames[1].caused_by, "external", "tool.call cites the framed-less step.context");
  for (let i = 2; i < 7; i += 1) {
    const caused = projection.frames[i].caused_by;
    ok(
      caused !== "external" && caused !== null,
      `frame ${i} (${projection.frames[i].event}) must cite a seen frame of the same correlation`
    );
  }
});

// ---------- item 1d: a real chain violation still freezes ----------

test("c5_violation_freezes_only_the_offending_row", () => {
  const entry = TRANSCRIPTS[0];
  const { items, frames } = loadTranscript(entry);
  const bogus = "00000000-0000-4000-8000-000000000000";
  const { state } = drive(items, {
    mutateFrame: (f) =>
      f.event === "authority.decided" ? { ...f, caused_by: bogus } : f,
  });
  eq(state.rows.length, 3, "rendering stops at the offending row (C-5 freeze)");
  eq(state.frozen, true, "the reducer freezes");
  eq(state.rows[2].violation, true, "the offending row carries the typed violation");
  eq(state.rows[2].frame.caused_by, bogus, "the row keeps its own caused_by verbatim");
  eq(state.rows[1].violation, false, "earlier rows stay clean");
  eq(state.toolResultStatus, null, "nothing falls back to success");
  eq(state.turnStatus, null, "no turn status is invented after a violation");
  eq(state.pending, null, "no approval preview exists yet at the offending row");

  // A post-first-frame violation of a LATER frame freezes too (the exemption is
  // not "the whole correlation is exempt").
  const { state: late } = drive(items, {
    mutateFrame: (f) => (f.event === "tool.started" ? { ...f, caused_by: bogus } : f),
  });
  eq(late.frozen, true, "a violation after the first frame is still fatal");
  eq(late.rows.length, 6, "rendering stops at the tool.started row");
  eq(late.toolResultStatus, null, "no result renders after the freeze");
  eq(late.pending !== null, true, "the preview seen before the violation stays rendered");
  void frames;
});

// ---------- item 1e: the deny run is not success-shaped ----------

test("deny_transcript_is_not_success_shaped", () => {
  const entry = TRANSCRIPTS[1];
  const { items, frames } = loadTranscript(entry);
  const { state } = drive(items);
  eq(state.rows.length, 7, "seven rows");
  eq(state.frozen, false, "no violation");
  eq(state.toolResultStatus, "denied", "tool.result status verbatim");
  eq(state.turnStatus, "failed", "turn.completed status verbatim");
  eq(frames.some((f) => f.event === "tool.started"), false, "no witness on a deny");
  eq(state.requestClosed, true, "the approval request is closed");
  ok(state.pending !== null, "the preview was shown");
  const preview = state.pending;
  const nine = [
    "call_id",
    "decision_id",
    "tool_name",
    "action_summary",
    "args_preview",
    "args_truncated",
    "args_digest",
    "effect_class",
    "targets",
    "matched_rule",
    "requested_at",
  ];
  for (const k of nine) ok(k in preview, `preview carries ${k}`);
  eq(preview.effect_class, "read", "effect_class verbatim");
  eq(preview.call_id, "call-1", "preview call_id verbatim");
});

// ---------- item 3: AT-1 projection checksum over the committed transcripts ----------

test("at1_projection_checksum_allow", () => {
  const entry = TRANSCRIPTS[0];
  const { frames } = loadTranscript(entry);
  const projection = projectFrames(frames);
  const sum = checksum(projection);
  eq(sum, entry.checksum, `${entry.file}: AT-1 projection checksum`);
  // The committed projection fixture (the view rows themselves, shared with the
  // engine and Tauri IPC legs) must be the same object.
  const fixture = JSON.parse(fs.readFileSync(path.join(TRANSCRIPT_DIR, entry.projection), "utf8"));
  eq(canonicalJson(fixture), canonicalJson(projection), `${entry.projection}: committed projection rows`);
  ok(canonicalJson(projection).length > 500, "projection is non-trivial");
  eq(projection.call.status, "succeeded", "projected call status");
  eq(projection.call.witness_present, true, "projected witness");
  ok(projection.call.output_digest.startsWith("sha256:"), "projected output digest (A-8 target_id)");
  eq(projection.call.terminal_state, "merged", "projected terminal state");
  eq(projection.call.n_results, 1, "one result");
});

test("at1_projection_checksum_deny", () => {
  const entry = TRANSCRIPTS[1];
  const { frames } = loadTranscript(entry);
  const projection = projectFrames(frames);
  eq(checksum(projection), entry.checksum, `${entry.file}: AT-1 projection checksum`);
  const fixture = JSON.parse(fs.readFileSync(path.join(TRANSCRIPT_DIR, entry.projection), "utf8"));
  eq(canonicalJson(fixture), canonicalJson(projection), `${entry.projection}: committed projection rows`);
  eq(projection.call.status, "denied", "projected call status");
  eq(projection.call.error_code, "user_denied", "projected error code");
  eq(projection.call.witness_present, false, "no witness");
  eq(projection.call.output_digest, null, "no output digest on a deny");
});

// ---------- E11: the generated protocol module (A-10 / AT-S5-03) ----------

// The shell's wire module is generated from the engine's schema, not written
// by hand (EXEC-49). This case ties that artifact to the bytes the engine
// actually served: `WIRE_SCHEMA_HASH` and the limits constants must equal the
// HelloAck in the committed transcripts, and every committed frame must be a
// declared slice-v1 event (no retired name, no unknown event). A stale
// `protocol.ts` — generated from an older wire model — fails here in `npm run
// test:harness`; the byte-exact regeneration check runs in `cargo test`
// (`engine/tests/e11_codegen.rs`).
test("generated_protocol_matches_the_hello_schema_hash", () => {
  const proto = require("../src/protocol.ts");
  ok(
    typeof proto.WIRE_SCHEMA_HASH === "string" && proto.WIRE_SCHEMA_HASH.startsWith("sha256:"),
    "the generated module exports WIRE_SCHEMA_HASH"
  );
  const events = new Set(proto.SLICE_EVENTS);
  const retired = new Set(proto.RETIRED_EVENTS);
  eq(events.size, 8, "declared slice-v1 events");

  for (const t of TRANSCRIPTS) {
    const { items, frames } = loadTranscript(t);
    const hello = items.find((i) => i.kind === "response" && i.response.id === "r1");
    ok(hello && hello.response.result, `${t.file}: HelloAck present`);
    const ack = hello.response.result;
    eq(
      ack.schema_hash,
      proto.WIRE_SCHEMA_HASH,
      `${t.file}: engine schema_hash vs the generated module (regenerate protocol.ts)`
    );
    eq(ack.event_set, proto.EVENT_SET, `${t.file}: event_set`);
    eq(ack.limits.max_frame_bytes, proto.MAX_FRAME_BYTES, `${t.file}: max_frame_bytes`);
    eq(ack.limits.max_json_depth, proto.MAX_JSON_DEPTH, `${t.file}: max_json_depth`);
    eq(ack.limits.id_max_bytes, proto.ID_MAX_BYTES, `${t.file}: id_max_bytes`);
    eq(ack.limits.deny_reason_max_bytes, proto.DENY_REASON_MAX_BYTES, `${t.file}: deny_reason_max_bytes`);
    for (const f of frames) {
      ok(events.has(f.event), `${t.file}: '${f.event}' is not a declared slice-v1 event`);
      ok(!retired.has(f.event), `${t.file}: retired event '${f.event}' on the wire (D-43)`);
      eq(f.schema_version, proto.SCHEMA_VERSION, `${t.file}: frame schema_version`);
    }
  }
});

// ---------- runner ----------

(async () => {
  let failed = 0;
  for (const c of cases) {
    try {
      await c.fn();
      console.log(`PASS ${c.name}`);
    } catch (e) {
      failed += 1;
      console.log(`FAIL ${c.name}: ${e && e.message ? e.message : e}`);
    }
  }
  console.log(`\nharness: ${cases.length - failed} passed, ${failed} failed, ${cases.length} total`);
  if (failed > 0) process.exit(1);
})();
