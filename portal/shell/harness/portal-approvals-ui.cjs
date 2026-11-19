"use strict";
// UI harness for the portal Approvals panel (R7-2).
//
// The panel's queue is the server's PENDING page. `decide()` re-reads that page
// after recording a verdict, so the decided row leaves the list. This harness
// mounts the real PortalApp in jsdom with a scripted fake `invoke` and asserts:
//   * the pending page is read on mount and each row offers Approve / Reject,
//   * an approve records the verdict, re-reads the row and then leaves the
//     pending page — and the verdict (decision, agent_resume, re-read status,
//     the "not proof of effect" caveat) is still on screen in the panel's
//     "Decided in this session" list,
//   * the same holds when the re-read fails (labelled "(re-read failed)"),
//   * a typed refusal for a row the queue no longer lists survives the reload.
//
// No Tauri, no server, no model call.
//
// Run from shell/:  node -r ./harness/workbench-ui-register.cjs harness/portal-approvals-ui.cjs

const assert = require("node:assert/strict");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
for (const name of ["window", "document", "HTMLElement", "Event", "MouseEvent", "KeyboardEvent"]) {
  global[name] = dom.window[name];
}
global.IS_REACT_ACT_ENVIRONMENT = true;

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
    faces: [{ id: "assistant", name: "Assistant", allowed: true }],
    may_touch: [],
    may_not_touch: [],
    expires_at: "2026-10-25T00:00:00Z",
  },
  server: { boot: "boot", version: "test" },
  gaps: [],
};
const CATALOG = {
  faces: [{ id: "assistant", name: "Assistant", allowed: true }],
  models: [],
  backends: [],
  capabilities: { faces: 1, backends: 0, actions: [], warnings: [] },
};

const row = (id, action_type) => ({
  id,
  conversation_id: "c1",
  action_type,
  details: { path: "/home/owner/notes.md" },
  status: "pending",
  created_at: "2026-10-24T07:04:11Z",
  user_id: "dad",
});

const ROW_A = row("appr-1", "write_file");
const ROW_B = row("appr-2", "run_command");
const ROW_C = row("appr-3", "delete_file");

function makeInvoke(state) {
  const calls = [];
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
      case "portal_conversation_open": return {conversation:CONVERSATION,messages:[],has_more:false};
      case "portal_pins": return {};
      case "pc_poll": return {requests:[],outcomes:{}};
      case "portal_approvals":
        return { items: state.pending.filter((r) => r.status === (args.status ?? "pending")), excluded: 0 };
      case "portal_approval_get":
        if (state.getFails) {
          throw JSON.stringify({ code: "timeout_no_reply", detail: "no reply to approvals.get", hint: "the link is still open" });
        }
        return { approval: { ...state.pending.concat(state.rows).find((r) => r.id === args.approval_id), status: "approved" } };
      case "portal_approval_decide": {
        const target = state.pending.concat(state.rows).find((r) => r.id === args.approval_id);
        if (target && target.id === "appr-3") {
          throw JSON.stringify({
            code: "approval_foreign",
            detail: "that approval belongs to another owner",
            hint: "this portal will not touch it",
          });
        }
        // The server records the verdict and the row stops being pending.
        state.pending = state.pending.filter((r) => r.id !== args.approval_id);
        return {
          decision: args.decision === "approve" ? "approved" : "refused",
          agent_resume: "resumed",
          approval: { ...target, status: args.decision === "approve" ? "approved" : "refused" },
        };
      }
      default:
        throw JSON.stringify({ code: "bad_request", detail: `unexpected ${cmd}`, hint: "harness" });
    }
  };
  return { invoke, calls };
}

const byText = (selector, text) =>
  [...document.querySelectorAll(selector)].find((node) => (node.textContent || "").trim() === text);

async function flush(fn) {
  await act(async () => {
    if (fn) await fn();
    await Promise.resolve();
  });
}

async function click(node) {
  await flush(() => {
    node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await flush();
}

async function mount(state) {
  const root = createRoot(document.getElementById("root"));
  const client = makeInvoke(state);
  await flush(() => root.render(React.createElement(PortalApp, { invoke: client.invoke, profiles: null, onProfiles: () => {} })));
  return { root, calls: client.calls, state };
}

const panel = () => document.querySelector('[aria-label="Approvals"]');
const sessionSection = () => document.querySelector('[aria-label="Decided in this session"]');
// Rows still in the pending page: everything outside the session list.
const pendingRowText = () =>
  [...panel().querySelectorAll("article.approval-row")]
    .filter((node) => !node.closest('[aria-label="Decided in this session"]'))
    .map((node) => node.textContent)
    .join(" | ");

let PortalApp;

(async () => {
  PortalApp = require("../src/portal/PortalApp.tsx").default;

  // ---- approve: the verdict outlives the pending page -------------------
  const first = await mount({ pending: [ROW_A, ROW_B], rows: [ROW_A, ROW_B], getFails: false });
  const tab = byText("button", "Needs you");
  assert.ok(tab, "the Needs-you tab exists");
  // The rail badge reads the pending page once at connect; count only the panel's reads.
  const beforeOpen = first.calls.filter((c) => c.cmd === "portal_approvals").length;
  await click(tab);
  assert.equal(
    first.calls.filter((c) => c.cmd === "portal_approvals").length - beforeOpen,
    0,
    "the page reuses the badge pending read",
  );
  assert.equal(first.calls.find((c) => c.cmd === "portal_approvals").args.status, "pending", "the panel asks for pending rows");
  assert.equal(document.querySelector(".needs-page .row-actions"),null,"decisions stay in the conversation");
  await click(byText("button","Open conversation"));
  assert.ok(pendingRowText().includes("appr-1"), "the pending row is rendered");
  assert.ok(pendingRowText().includes("appr-2"), "both pending rows are rendered");

  await click(byText("button", "Approve"));
  const decides = first.calls.filter((c) => c.cmd === "portal_approval_decide");
  assert.equal(decides.length, 1, "one decision per click");
  assert.deepEqual(
    { approval_id: decides[0].args.approval_id, decision: decides[0].args.decision },
    { approval_id: "appr-1", decision: "approve" },
    "the decision names the clicked row",
  );
  assert.ok(
    first.calls.some((c) => c.cmd === "portal_approval_get" && c.args.approval_id === "appr-1"),
    "the row is re-read after the decision",
  );

  assert.ok(!pendingRowText().includes("appr-1"), `the decided row left the pending page: ${pendingRowText()}`);
  const decidedText = sessionSection() ? sessionSection().textContent : "";
  assert.ok(decidedText, "a decided-in-this-session list is rendered outside the pending rows");
  assert.ok(decidedText.includes("appr-1"), "the decided row is still named");
  assert.ok(decidedText.includes("decision: approved"), `the server's decision is still shown: ${decidedText}`);
  assert.ok(decidedText.includes("agent_resume: resumed"), "agent_resume is still shown");
  assert.ok(decidedText.includes("row status: approved"), "the re-read status is still shown");
  assert.ok(decidedText.includes("(re-read from the server)"), "the status says it was re-read");
  assert.ok(decidedText.includes("A recorded decision is not proof of effect"), "the caveat survives the reload");
  const noticeText = panel().querySelector(".notice") ? panel().querySelector(".notice").textContent : "";
  assert.ok(noticeText.includes("appr-1") && noticeText.includes("approved"), `the notice repeats the verdict: ${noticeText}`);
  assert.ok(noticeText.includes("not proof of effect"), "the notice keeps the caveat too");
  assert.ok(pendingRowText().includes("appr-2"), "the still-pending row is untouched");
  // R9-3 pin: the verdict belongs to this window, not to the open tab.
  await click(byText("button", "Memory"));
  await click(byText("button", "Needs you"));
  await click(byText("button","Open conversation"));
  const afterTabs = sessionSection() ? sessionSection().textContent : "";
  assert.ok(afterTabs.includes("appr-1"), `the decided row survives a trip to another tab: ${afterTabs}`);
  assert.ok(afterTabs.includes("agent_resume: resumed"), "and so does its agent_resume");
  await flush(() => first.root.unmount());

  // ---- reject with a failed re-read: labelled, not hidden ---------------
  const second = await mount({ pending: [ROW_A, ROW_B], rows: [ROW_A, ROW_B], getFails: true });
  await click(byText("button", "Needs you"));
  await click(byText("button", "Open conversation"));
  await click(byText("button", "Reject"));
  const secondText = sessionSection() ? sessionSection().textContent : "";
  assert.ok(secondText.includes("appr-1"), "the refused row is kept in the session list");
  assert.ok(secondText.includes("decision: refused"), `the refused verdict is shown: ${secondText}`);
  assert.ok(secondText.includes("(re-read failed)"), "a failed re-read is stated, not glossed over");
  await flush(() => second.root.unmount());

  // ---- a refusal for a row the queue no longer lists --------------------
  const third = await mount({ pending: [ROW_C], rows: [ROW_C], getFails: false });
  await click(byText("button", "Needs you"));
  await click(byText("button", "Open conversation"));
  await click(byText("button", "Reject"));
  const inline = panel().textContent;
  assert.ok(inline.includes("approval_foreign"), "the refusal is typed and shown");
  // The row leaves the server's pending page (decided elsewhere / expired) while
  // this window still holds the refusal it got for it.
  third.state.pending = [];
  third.calls.length = 0;
  await click(byText("button", "Reload"));
  assert.ok(third.calls.some((c) => c.cmd === "portal_approvals"), "Reload re-reads the pending page");
  assert.ok(!pendingRowText().includes("appr-3"), "the refused row is no longer in the pending page");
  const refusalText = sessionSection() ? sessionSection().textContent : "";
  assert.ok(refusalText.includes("appr-3"), `a refusal for a row no longer listed must still be visible: ${panel().textContent}`);
  assert.ok(refusalText.includes("approval_foreign"), "the typed refusal code is kept");
  assert.ok(refusalText.includes("belongs to another owner"), "the server detail is kept verbatim");
  await flush(() => third.root.unmount());

  console.log(
    "portal approvals UI: PASS (pending page, verdict recorded and kept in a decided-in-this-session list, failed re-read labelled, refusal for a vanished row still visible)",
  );
})().catch((error) => {
  console.error("portal approvals UI: FAIL");
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
