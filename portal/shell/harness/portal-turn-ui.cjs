"use strict";
// UI-level regression harness for the portal chat surface (W-1): mounts the real
// PortalApp in jsdom with a scripted fake `invoke`, runs a turn to completion as
// `stopped` (the broker's terminal frame for every non-complete turn), and
// asserts that the composer, the Send button and the conversation list all come
// back and that the transcript carries an honest terminal line.
//
// No Tauri, no server, no model call: the event bus is a fake listener and every
// reply is a fixture.
//
// Run from shell/:  node -r ./harness/workbench-ui-register.cjs harness/portal-turn-ui.cjs

const assert = require("node:assert/strict");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
for (const name of ["window", "document", "HTMLElement", "Event", "MouseEvent", "KeyboardEvent"]) {
  global[name] = dom.window[name];
}
global.IS_REACT_ACT_ENVIRONMENT = true;

// The portal subscribes to `portal://event`; capture the callbacks so the test
// can emit broker frames exactly as the native layer would.
const listeners = [];
const eventModule = require.resolve("@tauri-apps/api/event", { paths: [path.resolve(__dirname, "..")] });
require.cache[eventModule] = {
  id: eventModule,
  filename: eventModule,
  loaded: true,
  children: [],
  paths: [],
  exports: {
    listen: async (_topic, cb) => {
      listeners.push(cb);
      return () => {
        const at = listeners.indexOf(cb);
        if (at >= 0) listeners.splice(at, 1);
      };
    },
  },
};

const React = require("react");
const { act } = React;
const { createRoot } = require("react-dom/client");

const CONVERSATION = { id: "c1", title: "First conversation", face_id: null, backend: null, model: null, project_id: null };
const HELLO = {
  identity: {
    sub: "dad",
    gateway: "http://127.0.0.1:7836",
    faces: [{ id: "assistant", name: "Assistant" }],
    may_touch: [],
    may_not_touch: [],
    expires_at: "2026-10-25T00:00:00Z",
  },
  server: { boot: "boot", version: "test" },
  gaps: [],
};
const CATALOG = {
  // W-5 fixture: the registry lists a face this portal token may not use.
  faces: [{ id: "assistant", name: "Assistant", allowed: true },
          { id: "reviewer", name: "Reviewer", allowed: false }],
  models: [{ id: "kimi_code", backend: "kimi_code", model: "kimi-for-coding", available: true, reason: null }],
  backends: [{ id: "kimi_code", kind: "openai_compatible", available: true, unavailable_reason: null }],
  capabilities: { faces: 1, backends: 1, actions: [], warnings: [] },
};

const calls = [];
let sendBehaviour = "accept"; // accept | transport | refused | not_dispatched
let stopBehaviour = "stopped"; // stopped | idle
let history = [];
let openFails = false;

const invoke = async (cmd, args) => {
  calls.push({ cmd, args });
  switch (cmd) {
    case "portal_connection_get": return { connection_configured: true };
    case "portal_hello":
      return HELLO;
    case "portal_catalog":
      return CATALOG;
    case "portal_conversations":
      return { items: [CONVERSATION], limit: 50, offset: 0, excluded: 0 };
    case "portal_conversation_open":
      if (openFails) throw JSON.stringify({ code: "timeout_no_reply", detail: "no reply to conversations.get", hint: "the link is still open" });
      return { conversation: CONVERSATION, messages: history, has_more: false };
    case "portal_pins":
      return { face_id: null, backend: null, model: null, profile: null, locale: null };
    case "portal_send":
      if (sendBehaviour === "transport") throw JSON.stringify({ code: "transport_unavailable", detail: "ssh exited", hint: "link closed" });
      if (sendBehaviour === "refused") throw JSON.stringify({ code: "gateway_http_500", detail: "gateway said no", hint: "retry later" });
      if (sendBehaviour === "not_dispatched")
        throw JSON.stringify({
          code: "not_dispatched",
          detail: "portal link to example-server is closed; nothing was sent — press Reconnect first",
          hint: "Nothing was written to the portal link, so nothing was sent and the draft is safe to retry.",
        });
      return { request_id: "req-1", accepted: true };
    case "portal_stop":
      return stopBehaviour === "idle" ? { stopped: false } : { stopped: true, code: "stopped" };
    case "portal_reconnect":
      return { target: "example-server", hello: HELLO };
    default:
      throw JSON.stringify({ code: "bad_request", detail: `unexpected ${cmd}`, hint: "harness" });
  }
};

function emit(frame) {
  const line = JSON.stringify(frame);
  for (const cb of listeners) cb({ payload: line });
}

const byText = (selector, text) =>
  [...document.querySelectorAll(selector)].find((node) => (node.textContent || "").includes(text));

const sendButton = () => byText("button", "Send to Bob");
const sidebarButton = () => document.querySelector(".conversation-list button");

async function flush(fn) {
  await act(async () => {
    if (fn) await fn();
    await Promise.resolve();
  });
}

async function type(text) {
  const box = document.querySelector("#composer");
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, "value").set;
  await flush(async () => {
    setter.call(box, text);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

(async () => {
  const PortalApp = require("../src/portal/PortalApp.tsx").default;
  const root = createRoot(document.getElementById("root"));
  await flush(() => root.render(React.createElement(PortalApp, { invoke, profiles: null, onProfiles: () => {} })));

  assert.ok(calls.some((c) => c.cmd === "portal_hello"), "the portal connected");
  assert.equal(listeners.length, 1, "the portal subscribed to portal://event once");

  // S-1: the conversation list must not claim per-row ownership filtering it
  // cannot substantiate (the rows carry no owner field).
  const sidebarText = document.querySelector(".portal-sidebar").textContent;
  assert.ok(sidebarText.includes("scoped to your account"), "the real mechanism is named");
  assert.ok(sidebarText.includes("cannot show or vouch for per-row ownership"), "the limit is stated");
  assert.ok(!sidebarText.includes("excluded by the broker"), "no ownership exclusion count is claimed");

  // Open the conversation and choose a model pin (W-2).
  await flush(async () => sidebarButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  // ---- W-5: a face the token may not use must be listed but never offered as
  // selectable, because the send path refuses it (`face_forbidden`).
  const faceSelect = document.querySelector("#portal-face");
  assert.ok(faceSelect, "the face picker is present");
  const faceOptions = [...faceSelect.options].map((option) => ({ text: option.textContent || "", disabled: option.disabled }));
  const forbiddenFace = faceOptions.find((option) => option.text.includes("reviewer"));
  assert.ok(forbiddenFace, `the registered face is still listed: ${faceOptions.map((o) => o.text).join(" | ")}`);
  assert.equal(forbiddenFace.disabled, true, "W-5: a face outside this token's allow-list is not selectable");
  assert.ok(forbiddenFace.text.includes("not permitted by this portal token"), "W-5: the option says why");
  const permittedFace = faceOptions.find((option) => option.text.includes("assistant"));
  assert.equal(permittedFace.disabled, false, "W-5: an allowed face stays selectable");
  assert.ok(document.body.textContent.includes("outside this portal token"), "W-5: the picker states that some registered faces are not usable");

  const modelSelect = document.querySelector("#portal-model");
  const modelOption = [...modelSelect.options].find((option) => option.value === "kimi_code");
  assert.ok(modelOption, `the picker labels the real model string: ${[...modelSelect.options].map((o) => o.textContent).join(" | ")}`);
  assert.ok(modelOption.textContent.includes("kimi_code"), "the option names its backend too");
  await flush(async () => {
    modelSelect.value = modelOption.value;
    modelSelect.dispatchEvent(new Event("change", { bubbles: true }));
  });
  assert.equal(document.getElementById("portal-model-choice").value, "kimi-for-coding", "the Model control labels the real model string");
  await type("hello bob");
  assert.equal(sendButton().disabled, false, "Send is enabled before a turn starts");
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  const sends = calls.filter((c) => c.cmd === "portal_send");
  assert.equal(sends.length, 1, "the turn was dispatched once");
  assert.equal(sends[0].args.backend, "kimi_code", "W-2: the backend is sent with the model");
  assert.equal(sends[0].args.model, "kimi-for-coding", "W-2: the model string is sent, not the backend id");
  assert.equal(sends[0].args.face_id, undefined, "no face pin was chosen, so none is invented");

  await flush(async () => {
    emit({ event: "chunk", data: { request_id: "req-1", content: "partial " } });
    emit({ event: "chunk", data: { request_id: "req-1", content: "answer" } });
  });
  assert.ok(document.body.textContent.includes("partial answer"), "streamed text is shown");
  await type("second question");
  assert.equal(sendButton().disabled, true, "Send is disabled while a turn is in flight");
  assert.equal(sidebarButton().disabled, false, "conversation switching stays available while a turn is live");

  // The broker ends every non-complete turn with `stopped`.
  await flush(async () => emit({ event: "stopped", data: { request_id: "req-1", code: "upstream_closed" } }));

  assert.ok(
    document.body.textContent.includes("the turn ended without a complete frame"),
    `an honest terminal line must appear in the transcript: ${document.body.textContent}`,
  );
  assert.ok(document.body.textContent.includes("partial answer"), "the partial output is kept on screen");
  assert.equal(sidebarButton().disabled, false, "W-1: the conversation list must not stay locked after a stopped turn");
  assert.equal(sendButton().disabled, false, "W-1: the composer must come back after a stopped turn");
  assert.equal(calls.filter((c) => c.cmd === "portal_send").length, 1, "nothing was resent after the terminal frame");

  // A user-requested stop is labelled as such.
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await flush(async () => {
    emit({ event: "chunk", data: { request_id: "req-1", content: "second answer" } });
  });
  const stopButton = byText("button", "Stop turn");
  await flush(async () => stopButton.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await flush(async () => emit({ event: "stopped", data: { request_id: "req-1", code: "stopped" } }));
  assert.ok(document.body.textContent.includes("stopped by you — partial output kept"), "a user stop says so");
  await type("third question");
  assert.equal(sendButton().disabled, false, "W-1: a user stop must also release the composer");
  assert.equal(sidebarButton().disabled, false, "W-1: a user stop must also release the sidebar");

  // ---- W-3: an unknown send outcome survives Refresh and is only released by
  // a successful history re-read.
  sendBehaviour = "transport";
  history = [];
  await type("ambiguous send");
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  assert.ok(document.body.textContent.includes("Unconfirmed text:"), "the unconfirmed text is named in the banner");
  assert.equal(sendButton().disabled, true, "W-3: Send is locked while the outcome is unknown");
  assert.equal(byText("button", "Refresh").disabled, true, "W-3: Refresh is disabled — it cannot clear the unknown outcome");

  await flush(async () => byText("button", "Reconnect and inspect history").dispatchEvent(new MouseEvent("click", { bubbles: true })));
  assert.ok(document.body.textContent.includes("not stored on the server"), "history was re-read and the send was not accepted");
  assert.ok(!document.body.textContent.includes("Unconfirmed text:"), "W-3: the lock is released by the history read");
  assert.equal(document.querySelector("#composer").value, "ambiguous send", "the text is restored, not resent");
  assert.equal(sendButton().disabled, false, "Send is usable again once history resolved the outcome");
  assert.equal(calls.filter((c) => c.cmd === "portal_send").length, 3, "no automatic replay happened");

  // Stored but unanswered: the outcome stays unknown and no resend is offered.
  sendBehaviour = "transport";
  history = [{ id: "m-user-1", role: "user", content: "stored but unanswered", created_at: "2026-10-24T12:00:00Z" }];
  await type("stored but unanswered");
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await flush(async () => byText("button", "Reconnect and inspect history").dispatchEvent(new MouseEvent("click", { bubbles: true })));
  assert.ok(document.body.textContent.includes("still unknown"), "the unknown outcome is kept when no reply follows");
  assert.ok(document.body.textContent.includes("no assistant reply after it"), "the reason is stated");
  await type("another try");
  assert.equal(sendButton().disabled, true, "W-3: no resend is offered while the outcome is unknown");
  assert.equal(calls.filter((c) => c.cmd === "portal_send").length, 4, "still exactly one send per click");

  // ---- R2-1: a `complete` turn must not render the answer twice. Release the
  // open gate through history first, then drive a full complete turn whose
  // readback carries the stored assistant message.
  sendBehaviour = "accept";
  history = [
    { id: "m-user-1", role: "user", content: "stored but unanswered", created_at: "2026-10-24T12:00:00Z" },
    { id: "m-assist-1", role: "assistant", content: "the earlier answer", created_at: "2026-10-24T12:00:01Z" },
  ];
  await flush(async () => byText("button", "Reconnect and inspect history").dispatchEvent(new MouseEvent("click", { bubbles: true })));
  assert.equal(sendButton().disabled, false, "the answering row released the gate");

  const transcript = () => document.querySelector(".transcript").textContent;
  const occurrences = (haystack, needle) => haystack.split(needle).length - 1;
  const ANSWER = "the complete answer body";

  history = [
    { id: "m-user-1", role: "user", content: "stored but unanswered", created_at: "2026-10-24T12:00:00Z" },
    { id: "m-assist-1", role: "assistant", content: "the earlier answer", created_at: "2026-10-24T12:00:01Z" },
    { id: "m-user-2", role: "user", content: "complete question", created_at: "2026-10-24T12:05:00Z" },
    { id: "m-assist-2", role: "assistant", content: ANSWER, created_at: "2026-10-24T12:05:01Z" },
  ];
  await type("complete question");
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await flush(async () => {
    emit({ event: "chunk", data: { request_id: "req-1", content: ANSWER } });
    emit({ event: "complete", data: { request_id: "req-1", message_id: "m-assist-2" } });
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
  assert.equal(occurrences(transcript(), ANSWER), 1, `R2-1: the answer must render exactly once: ${transcript()}`);
  assert.ok(!transcript().includes("Turn ended · complete"), "no second article repeats a confirmed answer");
  assert.ok(!transcript().includes("this text is not a stored message"), "the false provenance line is gone");
  await type("next question");
  assert.equal(sendButton().disabled, false, "the composer is back after a complete turn");

  // R2-1b: when the readback fails nothing is confirmed, so the streamed copy is
  // kept once — under a line that says exactly that.
  openFails = true;
  const UNCONFIRMED = "an answer the readback never confirmed";
  await type("unconfirmed question");
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await flush(async () => {
    emit({ event: "chunk", data: { request_id: "req-1", content: UNCONFIRMED } });
    emit({ event: "complete", data: { request_id: "req-1", message_id: "m-assist-3" } });
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
  assert.equal(occurrences(transcript(), UNCONFIRMED), 1, "the unconfirmed streamed copy is kept exactly once");
  assert.ok(transcript().includes("did not confirm this text as a stored message"), `provenance says what happened: ${transcript()}`);
  assert.ok(!transcript().includes("this text is not a stored message"), "the old absolute claim is gone");

  // ---- R4-1: the completion id is NOT the stored row's id. Real evidence from
  // the app: `complete` named message_id 0660b76fcc1345ddbf3bdda2af818dc5, the
  // history page held the same text under 9b221ff8-29fd-4aea-a609-1fcce02b4b64,
  // and the transcript rendered the answer twice — the stored row plus a
  // chunk-derived copy marked "not yet readable from history". The copy has to
  // be dropped by matching the TURN, not the id.
  openFails = false;
  const REAL_ANSWER = "UI_GATE_OK";
  const COMPLETION_ID = "0660b76fcc1345ddbf3bdda2af818dc5";
  const STORED_ID = "9b221ff8-29fd-4aea-a609-1fcce02b4b64";
  const REAL_QUESTION = "Reply with exactly: UI_GATE_OK";
  const assistantBodies = () =>
    [...document.querySelectorAll(".transcript article.msg.assistant .content")].map((node) => (node.textContent || "").trim());

  // Sent from an empty transcript (the live shape: the turn's own prompt is the
  // first row the readback holds), so the send mark has no pre-send row.
  history = [];
  await flush(async () => sidebarButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await type(REAL_QUESTION);
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  history = [
    { id: "m-user-9", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:20:00Z" },
    { id: STORED_ID, role: "assistant", content: REAL_ANSWER, created_at: "2026-10-24T12:20:01Z" },
  ];
  await flush(async () => {
    emit({ event: "chunk", data: { request_id: "req-1", content: REAL_ANSWER } });
    emit({ event: "complete", data: { request_id: "req-1", message_id: COMPLETION_ID } });
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
  assert.equal(
    assistantBodies().filter((body) => body === REAL_ANSWER).length,
    1,
    `R4-1: a completion id that differs from the stored row's id must not render the answer twice: ${transcript()}`,
  );
  assert.equal(document.querySelectorAll(".transcript article.msg.assistant").length, 1, "exactly one assistant article for the turn");
  assert.ok(transcript().includes(`message:${STORED_ID}`), `the answer is shown from the stored row: ${transcript()}`);
  assert.ok(!transcript().includes(COMPLETION_ID), "the completion id is never rendered as a message id");
  assert.ok(!transcript().includes("not yet readable from history"), `the copy is dropped, so no pending marker: ${transcript()}`);
  assert.ok(
    !document.body.textContent.includes("The answer has finished and is shown here while we check that it was saved."),
    "the server row is readable, so the pending notice must not appear",
  );

  // R4-1 negative control: the same shape with the answer genuinely absent from
  // the readback keeps exactly one copy under an honest pending marker, and the
  // next re-read that does hold the row drops it.
  const UNREAD = "an answer the readback has not committed yet";
  await type(REAL_QUESTION);
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  history = [
    { id: "m-user-9", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:20:00Z" },
    { id: STORED_ID, role: "assistant", content: REAL_ANSWER, created_at: "2026-10-24T12:20:01Z" },
    { id: "m-user-10", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:21:00Z" },
  ];
  await flush(async () => {
    emit({ event: "chunk", data: { request_id: "req-1", content: UNREAD } });
    emit({ event: "complete", data: { request_id: "req-1", message_id: "0660b76f00000000000000000000beef" } });
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
  assert.equal(
    assistantBodies().filter((body) => body === UNREAD).length,
    1,
    `R4-1 control: an unread answer is still shown exactly once: ${transcript()}`,
  );
  assert.ok(transcript().includes("not yet readable from history"), `R4-1 control: the pending marker is honest: ${transcript()}`);
  assert.ok(
    document.body.textContent.includes("The answer has finished and is shown here while we check that it was saved."),
    "R4-1 control: the notice is kept only while history lacks the row",
  );

  // The same turn, one re-read later: the server has committed the row, so the
  // chunk-derived article and its notice must be retired. Without the retry the
  // copy and the note stayed on screen for good (the real-app symptom).
  history = [
    { id: "m-user-9", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:20:00Z" },
    { id: STORED_ID, role: "assistant", content: REAL_ANSWER, created_at: "2026-10-24T12:20:01Z" },
    { id: "m-user-10", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:21:00Z" },
    { id: "m-assist-10", role: "assistant", content: UNREAD, created_at: "2026-10-24T12:21:01Z" },
  ];
  await flush(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1700));
  });
  assert.equal(
    assistantBodies().filter((body) => body === UNREAD).length,
    1,
    `R4-1: the committed row replaces the chunk-derived copy: ${transcript()}`,
  );
  assert.ok(transcript().includes("message:m-assist-10"), `R4-1: the stored row is the one shown: ${transcript()}`);
  assert.ok(!transcript().includes("not yet readable from history"), "R4-1: the pending marker is retired with the copy");
  assert.ok(
    !document.body.textContent.includes("The answer has finished and is shown here while we check that it was saved."),
    "R4-1: the notice stops claiming the row is unreadable once it lands",
  );

  // ---- R3-1 (reviewer probe P8): turn 1's answer landed in history while the
  // SAME question was sent again, so at send time the transcript still ended at
  // turn 1's user row. The matcher used to accept turn 1's answer as this
  // turn's, drop this turn's streamed copy, and leave the new answer nowhere on
  // screen. It must instead keep the honest pending article, then retire it on
  // the re-read that holds this turn's own row.
  history = [{ id: "m-user-11", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:22:00Z" }];
  await flush(async () => sidebarButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await type(REAL_QUESTION);
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  history = [
    { id: "m-user-11", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:22:00Z" },
    { id: "m-assist-11", role: "assistant", content: REAL_ANSWER, created_at: "2026-10-24T12:22:01Z" },
    { id: "m-user-12", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:23:00Z" },
  ];
  await flush(async () => {
    emit({ event: "chunk", data: { request_id: "req-1", content: REAL_ANSWER } });
    emit({ event: "complete", data: { request_id: "req-1", message_id: "0660b76f00000000000000000000cafe" } });
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
  assert.equal(
    assistantBodies().filter((body) => body === REAL_ANSWER).length,
    2,
    `R3-1: turn 1's stored answer and this turn's own answer must both be rendered: ${transcript()}`,
  );
  assert.ok(transcript().includes("message:m-assist-11"), `R3-1: turn 1's answer comes from its stored row: ${transcript()}`);
  assert.ok(transcript().includes("not yet readable from history"), `R3-1: this turn's answer is kept honestly: ${transcript()}`);

  history = [
    { id: "m-user-11", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:22:00Z" },
    { id: "m-assist-11", role: "assistant", content: REAL_ANSWER, created_at: "2026-10-24T12:22:01Z" },
    { id: "m-user-12", role: "user", content: REAL_QUESTION, created_at: "2026-10-24T12:23:00Z" },
    { id: "m-assist-12", role: "assistant", content: REAL_ANSWER, created_at: "2026-10-24T12:23:01Z" },
  ];
  await flush(async () => {
    await new Promise((resolve) => setTimeout(resolve, 1700));
  });
  assert.equal(
    assistantBodies().filter((body) => body === REAL_ANSWER).length,
    2,
    `R3-1: the committed row replaces the pending copy, never adds to it: ${transcript()}`,
  );
  assert.ok(transcript().includes("message:m-assist-12"), `R3-1: the repeat question's own row is the one shown: ${transcript()}`);
  assert.ok(!transcript().includes("not yet readable from history"), "R3-1: the pending marker is retired with the copy");

  // ---- R7-1/R7-3: when the link dies mid-turn the stream is gone but the
  // server turn may not be. The fault frame must release the locks, and Stop
  // must stay reachable for the OPEN conversation — reporting a `stopped:false`
  // reply honestly instead of turning it into a success.
  sendBehaviour = "accept";
  openFails = false;
  history = [];
  await flush(async () => sidebarButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await type("a turn whose link dies");
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await flush(async () => emit({ event: "chunk", data: { request_id: "req-1", content: "half an answer" } }));
  assert.ok(
    document.querySelector(".portal-sidebar .agent-row").classList.contains("working"),
    `the turn is live before the fault: ${document.querySelector(".portal-sidebar").textContent}`,
  );
  await flush(async () =>
    emit({ event: "transport_fault", data: { code: "transport_unavailable", detail: "broker closed the portal link (ssh exited)" } }),
  );
  assert.ok(
    !document.querySelector(".portal-sidebar .agent-row").classList.contains("working"),
    "R7-1: no live stream is claimed after the fault frame",
  );
  assert.ok(document.body.textContent.includes("transport_unavailable"), "R7-1: the typed fault is shown");
  assert.ok(document.body.textContent.includes("broker closed the portal link"), "R7-1: the native detail is shown verbatim");
  assert.ok(document.body.textContent.includes("half an answer"), "R7-1: the partial text stays on screen");
  assert.ok(document.querySelector(".transcript .terminal.transport"), "R7-1: the turn is closed in the transcript");
  await type("can I send again");
  assert.equal(sendButton().disabled, false, "R7-1: the composer is released by the fault frame");
  assert.equal(sidebarButton().disabled, false, "R7-1: the conversation list is released too");

  const stopAfterFault = byText("button", "Stop server turn");
  assert.ok(stopAfterFault, `R7-3: Stop stays available after the local stream is gone: ${document.body.textContent}`);
  assert.equal(stopAfterFault.disabled, false, "R7-3: a fault does not disable Stop");
  assert.ok(
    !document.querySelector(".conversation-header").textContent.includes("Stop turn"),
    "R7-3: the label says it stops the server turn, not a local stream",
  );
  const stopsBefore = calls.filter((c) => c.cmd === "portal_stop").length;
  await flush(async () => stopAfterFault.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  const stopCalls = calls.filter((c) => c.cmd === "portal_stop");
  assert.equal(stopCalls.length, stopsBefore + 1, "R7-3: Stop reaches the native stop command");
  assert.equal(stopCalls[stopCalls.length - 1].args.conversation_id, "c1", "R7-3: Stop names the open conversation");
  assert.ok(
    document.querySelector(".composer .notice").textContent.includes("the server reported stopped"),
    "R7-3: a real stop names what the server said",
  );

  // A `stopped:false` reply must stay honest, and the offer must not outlive it.
  stopBehaviour = "idle";
  const stillStop = byText("button", "Stop server turn");
  assert.ok(stillStop && !stillStop.disabled, "R7-3: Stop is still offered while the server turn may be running");
  await flush(async () => stillStop.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  const idleNotice = document.querySelector(".composer .notice").textContent;
  assert.ok(idleNotice.includes("no active generation"), `R7-3: a stopped:false reply is reported as such: ${idleNotice}`);
  assert.ok(!idleNotice.includes("the server reported stopped"), "R7-3: no false success is claimed");
  const retiredStop = byText("button", "Stop server turn");
  assert.ok(retiredStop && retiredStop.disabled === true, "R7-3: Stop retires once the server says nothing is generating");
  stopBehaviour = "stopped";

  // ---- R7-4: a refusal that proves nothing was written must not be reported
  // as an unknown send outcome (the old code used transport_unavailable for a
  // dead link, a full writer queue and 8 in-flight requests).
  sendBehaviour = "not_dispatched";
  const reconnectsBefore = calls.filter((c) => c.cmd === "portal_reconnect").length;
  await type("a send that never left");
  await flush(async () => sendButton().dispatchEvent(new MouseEvent("click", { bubbles: true })));
  assert.ok(!document.body.textContent.includes("Unconfirmed text:"), "R7-4: no unknown-outcome banner for a pre-dispatch refusal");
  assert.ok(document.body.textContent.includes("not_dispatched"), "R7-4: the refusal carries its own code");
  assert.ok(
    document.querySelector(".composer .notice").textContent.includes("refused before dispatch"),
    "R7-4: the notice says nothing was sent",
  );
  assert.equal(document.querySelector("#composer").value, "a send that never left", "R7-4: the draft is restored");
  assert.equal(
    calls.filter((c) => c.cmd === "portal_reconnect").length,
    reconnectsBefore,
    "R7-4: no reconnect-and-inspect round trip is demanded for a clean refusal",
  );
  await type("and usable again");
  assert.equal(sendButton().disabled, false, "R7-4: no unknown-outcome lock is left behind");
  sendBehaviour = "accept";

  await flush(async () => root.unmount());
  console.log(
    "portal-turn-ui: a face outside the token allow-list is not selectable (W-5); terminals release the composer and the sidebar; a complete turn renders its answer once; an identical repeat question renders its own answer (R3-1); unknown send outcomes survive Refresh and are resolved only by history; a transport fault releases the locks and keeps Stop on the server turn (R7-1/R7-3); a pre-dispatch refusal restores the draft and locks nothing (R7-4) PASS",
  );
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
