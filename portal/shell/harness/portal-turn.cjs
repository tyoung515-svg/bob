"use strict";
// Turn-state regression harness for the portal chat surface (W-1/W-3).
//
// Drives the REAL pure reducer (shell/src/portal/portal-state.ts, type checked by
// tsconfig.harness.json and loaded in memory) through every terminal outcome the broker can produce:
// `complete`, `stopped` (user-requested and broker-side), `error`, and a
// transport fault. Each one must release the in-flight lock so the composer and
// the conversation list come back, keep the partial text as a local note, and
// stage nothing for a resend.
//
// Run from shell/: node -r ./harness/workbench-ui-register.cjs harness/portal-turn.cjs

const assert = require("node:assert/strict");
const {
  INITIAL_TURN,
  STOPPED_BY_USER_LINE,
  STOPPED_REMOTE_LINE,
  TRANSPORT_LINE,
  SEND_TRANSPORT_LINE,
  COMPLETE_LINE,
  composerLocked,
  errorLine,
  inFlight,
  inspectUnknownSend,
  liveStream,
  modelKey,
  navigationLocked,
  pickFromRow,
  pinLabel,
  pinRefusal,
  sendPinArgs,
  terminalProvenance,
  turnAnswerRowId,
  turnReducer,
} = require("../src/portal/portal-state.ts");

const CONV = "conv-1";
let passed = 0;

function test(name, fn) {
  fn();
  passed += 1;
  console.log(`PASS ${name}`);
}

function running() {
  let state = turnReducer(INITIAL_TURN, { type: "send", conversation_id: CONV, text: "hello bob", pin: null });
  state = turnReducer(state, { type: "accepted", request_id: "req-1" });
  state = turnReducer(state, { type: "chunk", request_id: "req-1", content: "partial ", model: "m-1", backend: "b-1" });
  state = turnReducer(state, { type: "chunk", request_id: "req-1", content: "answer", model: null, backend: null });
  return state;
}

function locks(state, unknownOutcome) {
  return {
    composer: composerLocked({
      sending: false,
      stopping: false,
      approvalBusy: false,
      inFlight: inFlight(state),
      unknownOutcome,
    }),
    navigation: navigationLocked({ busy: false, inFlight: inFlight(state) }),
  };
}

// ---------------------------------------------------------------- live turn

test("a live turn locks its composer and keeps conversation navigation available", () => {
  const state = running();
  assert.equal(state.stream.content, "partial answer");
  assert.equal(state.stream.model, "m-1", "the last reported model sticks");
  assert.ok(liveStream(state, CONV), "the stream is live in its own conversation");
  assert.equal(liveStream(state, "conv-2"), null, "a stream never leaks into another conversation");
  assert.equal(inFlight(state), true);
  assert.deepEqual(locks(state, false), { composer: true, navigation: false });
});

test("a chunk for another request id is not appended", () => {
  const state = turnReducer(running(), { type: "chunk", request_id: "req-other", content: "foreign", model: null, backend: null });
  assert.equal(state.stream.content, "partial answer");
});

// ------------------------------------------------------------ terminal kinds

for (const terminal of [
  { label: "complete", event: { type: "terminal", request_id: "req-1", kind: "complete", code: null, detail: null }, line: COMPLETE_LINE },
  { label: "stopped by the user", event: { type: "terminal", request_id: "req-1", kind: "stopped", code: "stopped", detail: null, by_user: true }, line: STOPPED_BY_USER_LINE },
  { label: "stopped by the broker", event: { type: "terminal", request_id: "req-1", kind: "stopped", code: "upstream_closed", detail: null }, line: STOPPED_REMOTE_LINE },
  { label: "error", event: { type: "terminal", request_id: "req-1", kind: "error", code: "gateway_error", detail: "boom" }, line: errorLine("gateway_error") },
]) {
  test(`terminal ${terminal.label} releases the in-flight lock and keeps the partial text`, () => {
    const state = turnReducer(running(), terminal.event);
    assert.equal(state.stream, null, "no stream may survive a terminal event");
    assert.equal(state.pending, null, "nothing stays staged for a resend");
    assert.equal(inFlight(state), false);
    assert.equal(liveStream(state, CONV), null);
    assert.deepEqual(locks(state, false), { composer: false, navigation: false }, "composer and sidebar must both come back");
    assert.equal(state.terminal.kind, terminal.event.kind);
    assert.equal(state.terminal.partial, "partial answer", "the streamed text is kept, not dropped");
    assert.equal(state.terminal.line, terminal.line);
  });
}

test("a transport fault releases the lock but leaves the unknown-outcome gate to history", () => {
  const state = turnReducer(running(), { type: "transport_fault", code: "transport_unavailable", detail: "ssh exited" });
  assert.equal(state.stream, null);
  assert.equal(state.pending, null);
  assert.equal(inFlight(state), false);
  assert.equal(state.terminal.partial, "partial answer");
  assert.equal(state.terminal.line, TRANSPORT_LINE);
  // The honest separate gate: no resend until the server history is read.
  assert.deepEqual(locks(state, true), { composer: true, navigation: false });
});

test("a send that never returned gets an honest send-phase note", () => {
  let state = turnReducer(INITIAL_TURN, { type: "send", conversation_id: CONV, text: "hello bob", pin: null });
  assert.equal(inFlight(state), false, "a pending text alone is not a stream");
  state = turnReducer(state, { type: "transport_fault", code: "timeout_no_reply", detail: "no reply", conversation_id: CONV });
  assert.equal(state.pending, null);
  assert.equal(state.terminal.line, SEND_TRANSPORT_LINE);
  assert.equal(state.terminal.conversation_id, CONV);
  assert.deepEqual(locks(state, true), { composer: true, navigation: false });
});

test("a refused send clears the pending row and locks nothing", () => {
  let state = turnReducer(INITIAL_TURN, { type: "send", conversation_id: CONV, text: "hello bob", pin: null });
  state = turnReducer(state, { type: "refused" });
  assert.equal(state.pending, null);
  assert.equal(state.stream, null);
  assert.deepEqual(locks(state, false), { composer: false, navigation: false });
});

test("the broker's error-then-stopped pair keeps one note and stays unlocked", () => {
  const state = turnReducer(running(), { type: "terminal", request_id: "req-1", kind: "error", code: "gateway_error", detail: "boom" });
  const after = turnReducer(state, { type: "terminal", request_id: "req-1", kind: "stopped", code: "upstream_closed", detail: null });
  assert.equal(after.terminal.kind, "error", "the informative terminal wins");
  assert.equal(after.terminal.code, "gateway_error");
  assert.equal(after.terminal.partial, "partial answer");
  assert.deepEqual(locks(after, false), { composer: false, navigation: false });
});

test("a stopped frame with no local stream never re-opens a lock", () => {
  const state = turnReducer(INITIAL_TURN, { type: "terminal", request_id: "", kind: "stopped", code: "no_active_stream_in_broker", detail: null });
  assert.equal(state.stream, null);
  assert.equal(state.pending, null);
  assert.deepEqual(locks(state, false), { composer: false, navigation: false });
});

test("a note rewrites the terminal line without unlocking anything extra", () => {
  let state = turnReducer(running(), { type: "terminal", request_id: "req-1", kind: "complete", code: null, detail: null });
  state = turnReducer(state, { type: "note", line: "answered — history readback failed (gateway_http_500)." });
  assert.equal(state.terminal.line, "answered — history readback failed (gateway_http_500).");
  assert.deepEqual(locks(state, false), { composer: false, navigation: false });
});

test("reset (conversation switch) clears the turn slice", () => {
  const state = turnReducer(running(), { type: "reset" });
  assert.deepEqual(state, INITIAL_TURN);
  assert.deepEqual(locks(state, false), { composer: false, navigation: false });
});

// ------------------------------------------- R2-1: one rendering of an answer

test("a complete turn whose readback put the text in the transcript keeps no local copy", () => {
  let state = turnReducer(running(), { type: "terminal", request_id: "req-1", kind: "complete", code: null, detail: null });
  assert.equal(state.terminal.partial, "partial answer");
  assert.equal(state.terminal.in_transcript, false, "nothing is confirmed until the readback says so");
  state = turnReducer(state, { type: "turn_in_transcript" });
  assert.equal(state.terminal.in_transcript, true);
  assert.equal(state.terminal.partial, "", "the streamed copy is dropped, so the answer renders once");
  assert.equal(state.terminal.line, COMPLETE_LINE);
  assert.deepEqual(locks(state, false), { composer: false, navigation: false });
});

test("a complete turn whose readback failed keeps the text under a true provenance line", () => {
  const state = turnReducer(running(), { type: "terminal", request_id: "req-1", kind: "complete", code: null, detail: null });
  assert.equal(state.terminal.partial, "partial answer", "nothing confirmed it, so the copy is kept");
  assert.equal(state.terminal.in_transcript, false);
  const line = terminalProvenance(state.terminal);
  assert.ok(line.includes("did not confirm this text as a stored message"), line);
  assert.ok(!line.includes("this text is not a stored message"), "the false R2-1 claim is gone");
});

test("a stopped turn's provenance never claims a stored message either way", () => {
  const state = turnReducer(running(), { type: "terminal", request_id: "req-1", kind: "stopped", code: "upstream_closed", detail: null });
  const line = terminalProvenance(state.terminal);
  assert.ok(line.includes("not confirmed it in server history"), line);
  assert.ok(line.includes("did not resend it"), line);
  assert.ok(!line.includes("this text is not a stored message"), "the old absolute claim is gone");
});

test("a turn_in_transcript event with no terminal note changes nothing", () => {
  assert.deepEqual(turnReducer(INITIAL_TURN, { type: "turn_in_transcript" }), INITIAL_TURN);
});

// ------------------------------- R2-7: the gate follows the send, not the text

const PRE_SEND = [
  { id: "m-a", role: "user", content: "continue" },
  { id: "m-b", role: "assistant", content: "older answer to the older continue" },
  { id: "m-c", role: "user", content: "previous question" },
];
const AFTER_MARK = { conversation_id: CONV, count: 3, last_id: "m-c" };

test("an older identical message cannot release the unknown-outcome gate", () => {
  // The buggy search found the older "continue" (index 0) and its reply.
  const outcome = inspectUnknownSend(PRE_SEND, AFTER_MARK, "continue", CONV);
  assert.deepEqual(outcome, { state: "not_stored" }, "rows before the pre-send mark prove nothing");
});

test("a row after the mark with a reply releases the gate", () => {
  const rows = [...PRE_SEND, { id: "m-d", role: "user", content: "continue" }, { id: "m-e", role: "assistant", content: "new answer" }];
  assert.deepEqual(inspectUnknownSend(rows, AFTER_MARK, "continue", CONV), { state: "answered", message_id: "m-d" });
});

test("a row after the mark with no reply keeps the gate", () => {
  const rows = [...PRE_SEND, { id: "m-d", role: "user", content: "continue" }];
  assert.deepEqual(inspectUnknownSend(rows, AFTER_MARK, "continue", CONV), {
    state: "stored_unanswered",
    message_id: "m-d",
  });
});

test("a readback that cannot be anchored to the send keeps the gate", () => {
  const shifted = [{ id: "m-x", role: "user", content: "continue" }];
  assert.equal(inspectUnknownSend(shifted, AFTER_MARK, "continue", CONV).state, "unknown", "the last pre-send row is gone");
  assert.equal(inspectUnknownSend(PRE_SEND, null, "continue", CONV).state, "unknown", "no mark, no release");
  assert.equal(
    inspectUnknownSend(PRE_SEND, { ...AFTER_MARK, conversation_id: "conv-2" }, "continue", CONV).state,
    "unknown",
    "a mark from another conversation proves nothing",
  );
  assert.equal(
    inspectUnknownSend(
      [{ id: "m-a", role: "user", content: "q" }],
      { conversation_id: CONV, count: 9, last_id: null },
      "q",
      CONV,
    ).state,
    "unknown",
    "a readback shorter than the pre-send transcript is not evidence",
  );
});

// ----------------------------------- R2-8: never dispatch an invented model

test("a catalog row with no model string is never pinned as model=backend", () => {
  assert.equal(pickFromRow({ id: "kimi_code", backend: "kimi_code", model: null }), null);
  assert.equal(pickFromRow({ id: "kimi_code", backend: "kimi_code", model: "   " }), null);
  assert.equal(pinRefusal({ backend: "kimi_code", model: null }), "the server reported no model string");
  assert.equal(pinRefusal({ backend: "", model: "m-1" }), "no backend reported");
  assert.equal(pinRefusal({ backend: "kimi_code", model: "kimi-for-coding" }), null);
  assert.deepEqual(sendPinArgs(pickFromRow({ backend: "kimi_code", model: null })), {}, "nothing is dispatched for an unnamed model");
});

// ----------------------------------------------------------------- chat pins

test("a model pin is always sent with its backend", () => {
  const row = { id: "kimi_code", backend: "kimi_code", model: "kimi-for-coding" };
  const pin = pickFromRow(row);
  assert.deepEqual(pin, { backend: "kimi_code", model: "kimi-for-coding" });
  assert.deepEqual(sendPinArgs(pin), { backend: "kimi_code", model: "kimi-for-coding" }, "both halves or nothing");
  assert.deepEqual(sendPinArgs(null), {}, "no selection sends no pin");
  assert.deepEqual(sendPinArgs({ backend: "b-1", model: "" }), {}, "a half pin is refused locally");
});

test("the picker label shows concrete models and names account defaults without inventing pins", () => {
  assert.equal(pinLabel({ backend: "kimi_code", model: "kimi-for-coding" }), "kimi-for-coding · kimi_code");
  assert.equal(pinLabel({ backend: "kimi_code", model: "" }), "Account default · kimi_code");
  assert.equal(pinLabel({ backend: "kimi_code", model: null, id: "kimi_code" }), "Account default · kimi_code");
  assert.equal(pinLabel({ backend: "codex_code", model: null }), "Codex default · codex_code");
  assert.equal(pinLabel({ backend: "openai_codex", model: null, label: "GPT (ChatGPT sign-in)" }), "GPT (ChatGPT sign-in) · Codex default · openai_codex");
});

test("a row without a backend cannot produce a model pin", () => {
  assert.equal(pickFromRow({ id: "x", backend: "", model: "m-1" }), null, "the broker refuses model-without-backend");
  assert.equal(pickFromRow({ backend: "b-1", model: "m-1" }).model, "m-1");
});

test("option keys are stable per backend+model pair", () => {
  assert.equal(modelKey({ backend: "b-1", model: "m-1" }), modelKey({ backend: "b-1", model: "m-1" }));
  assert.notEqual(modelKey({ backend: "b-1", model: "m-1" }), modelKey({ backend: "b-2", model: "m-1" }));
});

// ------------------------ R4-1: the same answer under a different message id

const PROMPT = "Reply with exactly: UI_GATE_OK";
// The live shape: the turn was sent from an empty transcript, so the mark has no
// last row and the prompt row is the first row the readback holds.
const R4_MARK = { conversation_id: CONV, count: 0, last_id: null, text: PROMPT };
const R4_ROWS = [
  { id: "m-q", role: "user", content: PROMPT, created_at: "2026-10-24T12:20:00Z" },
  { id: "9b221ff8-29fd-4aea-a609-1fcce02b4b64", role: "assistant", content: "UI_GATE_OK", created_at: "2026-10-24T12:20:01Z" },
];

test("a stored row whose id differs from the completion id is still this turn's answer", () => {
  // Real ids: `complete` named 0660b76f… while the stored row was 9b221ff8….
  assert.equal(turnAnswerRowId(R4_ROWS, R4_MARK, "UI_GATE_OK", CONV), "9b221ff8-29fd-4aea-a609-1fcce02b4b64");
});

test("the answer text is compared after normalization", () => {
  const rows = [R4_ROWS[0], { id: "m-a", role: "assistant", content: "  UI_GATE_OK\r\n\r\n" }];
  assert.equal(turnAnswerRowId(rows, R4_MARK, "UI_GATE_OK\n", CONV), "m-a");
  assert.equal(turnAnswerRowId(rows, R4_MARK, "UI_GATE_OK (different)", CONV), null, "different text is a different answer");
});

test("an older identical answer before the send mark is not this turn's answer", () => {
  const rows = [
    { id: "m-old", role: "assistant", content: "UI_GATE_OK" },
    { id: "m-q2", role: "user", content: PROMPT },
    { id: "m-new", role: "assistant", content: "UI_GATE_OK" },
  ];
  const mark = { conversation_id: CONV, count: 1, last_id: "m-old", text: PROMPT };
  assert.equal(turnAnswerRowId(rows, mark, "UI_GATE_OK", CONV), "m-new", "the answer after this turn's prompt wins");
  assert.equal(turnAnswerRowId(rows.slice(0, 2), mark, "UI_GATE_OK", CONV), null, "with no answer yet nothing is claimed");
});

// -------------------- R3-1: the answer is anchored to THIS turn's prompt row

test("P8/R3-1: an earlier turn's identical answer cannot confirm a repeated question", () => {
  // The reachable race: turn 1's answer landed in history (asst1) while turn 2 —
  // the same question — was sent, so the transcript at send time still ended at
  // turn 1's USER row (u1). Anchoring on "the first row after the mark" accepted
  // asst1 as turn 2's answer, turn_in_transcript dropped turn 2's streamed copy,
  // and the new answer never rendered anywhere.
  const rows = [
    { id: "u1", role: "user", content: PROMPT },
    { id: "asst1", role: "assistant", content: "UI_GATE_OK" },
    { id: "u2", role: "user", content: PROMPT },
  ];
  const mark = { conversation_id: CONV, count: 1, last_id: "u1", text: PROMPT };
  assert.equal(turnAnswerRowId(rows, mark, "UI_GATE_OK", CONV), null, "asst1 answered turn 1, so this turn is not readable yet");
});

test("R3-1: an identical repeat question still renders its own answer", () => {
  const rows = [
    { id: "u1", role: "user", content: PROMPT },
    { id: "asst1", role: "assistant", content: "UI_GATE_OK" },
    { id: "u2", role: "user", content: PROMPT },
    { id: "asst2", role: "assistant", content: "UI_GATE_OK" },
  ];
  const mark = { conversation_id: CONV, count: 1, last_id: "u1", text: PROMPT };
  assert.equal(turnAnswerRowId(rows, mark, "UI_GATE_OK", CONV), "asst2", "the repeated question's own answer is the second one");
  assert.equal(
    turnAnswerRowId(rows, { ...mark, text: "a different question" }, "UI_GATE_OK", CONV),
    null,
    "another turn's prompt does not confirm this text",
  );
});

// ------------------------- R3-2: no client-clock guess in the fallback

test("R3-2: an unanchored readback never decides by the clock", () => {
  // The answer text is in the page and its row time is comfortably after the
  // send, but this turn's prompt row is not in the readback. The old fallback
  // compared the client's `sent_at` with the server's `created_at` and claimed a
  // row a clock step (sleep/resume, NTP) decides — the same refusal
  // inspectUnknownSend() makes.
  const window = [{ id: "m-late", role: "assistant", content: "UI_GATE_OK", created_at: "2026-10-24T12:20:02Z" }];
  assert.equal(turnAnswerRowId(window, R4_MARK, "UI_GATE_OK", CONV), null, "no prompt row in the page, no confirmation");
  assert.equal(
    turnAnswerRowId(R4_ROWS, { conversation_id: CONV, count: 1, last_id: "m-gone", text: PROMPT }, "UI_GATE_OK", CONV),
    null,
    "the pre-send row is not in the page, so nothing is anchored",
  );
  assert.equal(
    turnAnswerRowId(R4_ROWS, { conversation_id: CONV, count: 5, last_id: null, text: PROMPT }, "UI_GATE_OK", CONV),
    null,
    "a page shorter than the pre-send transcript is not evidence",
  );
  assert.equal(turnAnswerRowId(R4_ROWS, { conversation_id: CONV, count: 0, last_id: null }, "UI_GATE_OK", CONV), null, "a mark with no prompt cannot anchor");
  assert.equal(turnAnswerRowId(R4_ROWS, R4_MARK, "UI_GATE_OK", "conv-2"), null, "a mark from another conversation proves nothing");
  assert.equal(turnAnswerRowId(R4_ROWS, null, "UI_GATE_OK", CONV), null, "no mark, no confirmation");
  assert.equal(turnAnswerRowId(R4_ROWS, R4_MARK, "   ", CONV), null, "empty text confirms nothing");
});

console.log(
  `\nportal-turn: ${passed} checks passed (W-1 terminal state machine, W-2/R2-8 pins, R2-1 single render, R2-7 send mark, R4-1 turn answer matching, R3-1 prompt anchoring, R3-2 no clock guess)`,
);

