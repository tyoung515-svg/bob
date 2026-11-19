"use strict";
// UI harness for the portal's run ledger (broker 1.2.0 `runs.list`).
//
// Mounts the real PortalApp in jsdom with a scripted fake `invoke`, opens the
// Runs panel and asserts:
//   * the panel asks the broker for the ledger on mount (one `portal_runs_list`),
//   * recorded runs render with id, status, timestamps, team and receipt count,
//   * the panel labels the view as the portal's own ledger and shows the
//     broker's caveats verbatim,
//   * "Read from server" issues `portal_run_get` for THAT row and the returned
//     run appears in the live section,
//   * a ledger fault renders the typed Fault with its code and a retry, and no
//     row is invented.
//
// No Tauri, no server, no model call.
//
// Run from shell/:  node -r ./harness/workbench-ui-register.cjs harness/portal-runs-ui.cjs

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

const CAVEATS = [
  "Only runs started through this portal are recorded; the server exposes no run list.",
  "Status is the last value the server returned to this portal, not a live poll.",
];
const LEDGER = {
  items: [
    {
      run_id: "d355c512-ledger-1",
      team: "balance-budget",
      worker: "bob-ds-flash",
      task_excerpt: "summarise the contract",
      started_at: "2026-10-24T07:04:11Z",
      last_status: "completed",
      last_checked_at: "2026-10-24T07:09:02Z",
      receipts: [
        { role: "manager", model_face: "bob-glm-53", model_pin: "glm-5.3", elapsed_seconds: 12.5 },
        { role: "worker", model_face: "bob-ds-flash", model_pin: "deepseek-v4-flash", elapsed_seconds: 4.25 },
      ],
      source: "portal_run_ledger",
    },
    {
      run_id: "a1b2c3-ledger-2",
      team: "balance-budget",
      worker: null,
      task_excerpt: "second recorded run",
      started_at: "2026-10-24T06:00:00Z",
      last_status: "unknown",
      last_checked_at: "2026-10-24T06:00:00Z",
      receipts: [],
      source: "portal_run_ledger",
    },
  ],
  total: 57,
  limit: 20,
  offset: 0,
  scope: { mechanism: "portal_run_ledger", row_owner_attributable: true },
  caveats: CAVEATS,
};

const SERVER_RUN = {
  id: "d355c512-ledger-1",
  status: "completed",
  team: "balance-budget",
  max_model_calls: 4,
  max_seconds: 900,
  receipts: [{ role: "manager", model_face: "bob-glm-53", model_pin: "glm-5.3", elapsed_seconds: 12.5 }],
};

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
      case "portal_conversation_open":
        return { conversation: CONVERSATION, messages: [], has_more: false };
      case "portal_runs_list":
        if (state.ledgerFails) {
          throw JSON.stringify({
            code: "run_ledger_corrupt",
            detail: "the run ledger is not readable JSON",
            hint: "the ledger file was moved aside on the next write",
          });
        }
        return LEDGER;
      case "portal_run_get":
        return {
          run: { ...SERVER_RUN, id: args.run_id },
          ...(state.getWarnings ? { warnings: state.getWarnings } : {}),
        };
      case "portal_runs_start":
        return {
          run: { ...SERVER_RUN, id: "new-run-123", status: "accepted" },
          ...(state.startWarnings ? { warnings: state.startWarnings } : {}),
        };
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
}

async function mount(state) {
  const root = createRoot(document.getElementById("root"));
  const client = makeInvoke(state);
  await flush(() => root.render(React.createElement(PortalApp, { invoke: client.invoke, profiles: null, onProfiles: () => {} })));
  return { root, calls: client.calls };
}

let PortalApp;

(async () => {
  PortalApp = require("../src/portal/PortalApp.tsx").default;

  // ---- happy path -------------------------------------------------------
  const first = await mount({
    ledgerFails: false,
    startWarnings: ["run_ledger: run_ledger_io"],
    getWarnings: ["run_ledger: run_ledger_io"],
  });
  const runsTab = byText("button", "Tasks");
  assert.ok(runsTab, "the Runs tab exists");
  await click(runsTab);
  // Tasks is the calendar now (2026-11-02): it reads the ledger once to plot past runs,
  // and the read-only runs sit behind Run history. Count only the panel's own read.
  const calendarReads = first.calls.filter((c) => c.cmd === "portal_runs_list").length;
  await click(byText("button", "Run history"));
  assert.ok(
    first.calls.some((c) => c.cmd === "portal_runs_list"),
    "opening the Runs panel asks the broker for the ledger exactly once",
  );
  assert.equal(first.calls.filter((c) => c.cmd === "portal_runs_list").length - calendarReads, 1, "one ledger read on mount");

  const panel = document.querySelector('[aria-label="Runs"]');
  assert.ok(panel, "the Runs panel mounted");
  const text = panel.textContent;
  assert.ok(text.includes("d355c512-ledger-1"), "recorded run 1 is rendered");
  assert.ok(text.includes("a1b2c3-ledger-2"), "recorded run 2 is rendered");
  assert.ok(text.includes("completed"), "the recorded status is shown");
  assert.ok(text.includes("showing 2 of 57 recorded"), "the page size and the ledger total are both stated");
  assert.ok(text.includes("bob-ds-flash"), "the worker is shown when present");
  assert.ok(text.includes("2 receipts"), "the receipt count is shown");
  assert.ok(text.includes("2026-10-24T07:04:11Z"), "started_at is shown");
  assert.ok(text.includes("portal"), "the panel names the portal ledger as the source");
  for (const caveat of CAVEATS) {
    assert.ok(text.includes(caveat), `the broker caveat is shown verbatim: ${caveat}`);
  }
  assert.ok(
    text.includes("server exposes no run list") || text.includes("exposes no run list"),
    "the panel states the server has no run list",
  );

  // reading a recorded run from the server uses THAT row's id
  const readButton = byText("button", "Read from server");
  assert.ok(readButton, "a recorded row offers Read from server");
  await click(readButton);
  const getCalls = first.calls.filter((c) => c.cmd === "portal_run_get");
  assert.equal(getCalls.length, 1, "one server read per click");
  assert.equal(getCalls[0].args.run_id, "d355c512-ledger-1", "the clicked row's id is used");
  const afterRead = document.querySelector('[aria-label="Runs"]').textContent;
  assert.ok(
    afterRead.includes("Not recorded in the portal ledger"),
    "a read whose ledger write failed is labelled too (run.get path)",
  );
  assert.ok(afterRead.includes("Runs started in this window"), "the live section is labelled");
  assert.ok(afterRead.includes("receipts"), "the server row renders its receipts");
  // A dispatch the ledger could not record must never look normal (review #6).
  const teamInput = document.querySelector('#run-team');
  await flush(() => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(teamInput, LEDGER.items[0].team);
    teamInput.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const task = document.querySelector("#run-task");
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, "value").set;
  await flush(() => {
    setter.call(task, "run this");
    task.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click(byText("button", "Start run"));
  assert.equal(first.calls.filter(c => c.cmd === 'portal_runs_start').length, 1, 'legacy Start still dispatches once');
  const startNotice = document.querySelector('[aria-label="Runs"]').textContent;
  assert.ok(
    startNotice.includes("Not recorded in the portal ledger"),
    "a run that the ledger did not record is labelled, not shown as a clean success",
  );
  assert.ok(startNotice.includes("run_ledger_io"), "the warning code is carried through verbatim");
  await flush(() => first.root.unmount());

  // ---- ledger fault -----------------------------------------------------
  const second = await mount({ ledgerFails: true });
  await click(byText("button", "Tasks"));
  await click(byText("button", "Run history"));
  const faultPanel = document.querySelector('[aria-label="Runs"]');
  const faultText = faultPanel.textContent;
  assert.ok(faultText.includes("run_ledger_corrupt"), "the typed ledger code is shown");
  assert.ok(faultText.includes("not readable JSON"), "the broker detail is shown verbatim");
  assert.ok(!faultText.includes("d355c512-ledger-1"), "no row is invented when the ledger fails");
  assert.ok(byText("button", "Reload this panel"), "the fault offers a retry");
  await flush(() => second.root.unmount());

  console.log("portal runs ledger UI: PASS (ledger on mount, rows, caveats, server read, fault path)");
})().catch((error) => {
  console.error("portal runs ledger UI: FAIL");
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
