"use strict";
// WSF1 (Windows setup acceptance, 2026-11-13): regressions for what a real GUI run showed.
//   F1  a dead portal link always has a visible Reconnect control (with and without a session),
//       the update card's finished note carries one too, and This PC never says "server Bob";
//   F3  the rail says "stuck" only for a wedged runtime; a dropped desktop link is "offline";
//   F4  Settings → Connection lists only what is true for the effective connection mode.
// F2, F5 and F6 are covered in runtime-upgrade-ui.cjs, model-routing-entry-ui.cjs and
// portal-first-run-ui.cjs.
//
// No Tauri, no runtime, no model call: every native reply is a fixture, and the fixture counts
// what was dispatched so "reconnect is explicit, nothing is replayed" is checked, not assumed.
//
// Run from shell/:  node -r ./harness/workbench-ui-register.cjs harness/windows-setup-fixes-ui.cjs
const assert = require("node:assert/strict");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost", pretendToBeVisual: true });
for (const name of ["window", "document", "HTMLElement", "Event", "MouseEvent", "KeyboardEvent", "localStorage"]) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = [];
const eventModule = require.resolve("@tauri-apps/api/event", { paths: [path.resolve(__dirname, "..")] });
require.cache[eventModule] = { id: eventModule, filename: eventModule, loaded: true, children: [], paths: [], exports: {
  listen: async (_topic, cb) => { listeners.push(cb); return () => { const i = listeners.indexOf(cb); if (i >= 0) listeners.splice(i, 1); }; } } };
const React = require("react"), { act } = React, { createRoot } = require("react-dom/client");
const PortalApp = require("../src/portal/PortalApp.tsx").default;
const { SettingsView, DEFAULT_APPEARANCE, connectionFacts } = require("../src/portal/SettingsView.tsx");
const { createPortalClient } = require("../src/portal/portal-client.ts");

const CONVERSATION = { id: "c1", title: "First", face_id: "assistant", backend: null, model: null, project_id: null };
const HELLO = { identity: { sub: "dad", gateway: "http://127.0.0.1:7846", faces: [{ id: "assistant", name: "Assistant" }], may_touch: [], may_not_touch: [], expires_at: "2027-01-01T00:00:00Z" }, server: { boot: "b", version: "t" }, gaps: [] };
const CATALOG = { faces: [{ id: "assistant", name: "Assistant", allowed: true }], models: [], backends: [], capabilities: { faces: 1, backends: 0, actions: [], warnings: [] } };
const LINK_DOWN = "BoB services passed health checks, but this desktop's broker connection is down.";
// What the native layer reports (src-tauri/src/portal.rs `Connection::payload` / `profiles_payload`).
const SERVER_TRANSPORT = { kind: "ssh_stdio_jsonl", targets: ["example-server", "192.0.2.35"], ssh: "C:\\Windows\\System32\\OpenSSH\\ssh.exe", broker: "/opt/bob/portal/portal-broker.py", mode: "server" };
const PC_TRANSPORT = { kind: "wsl_stdio_jsonl", targets: ["dad"], ssh: "C:\\Windows\\System32\\wsl.exe", broker: "bob-broker", mode: "this_pc", lane: "dad" };
const SSH_DETAILS = [/ssh_stdio_jsonl/, /example-server/, /192\.0\.2\.35/, /ssh\.exe/i, /portal-broker\.py/, /operator/];
const profilesWith = (transport) => ({ schema: "portal-profiles-v1", active: "bob_server", developer_mode: false,
  profiles: [{ id: "bob_server", label: "BoB", available: true, reason: null }], data_root: "C:\\data",
  settings: { path: "C:\\data\\portal-settings.json", hardened: true, schema: "portal-settings-v1" }, transport });
const connectionPayload = (mode, lane) => ({ connection_configured: true,
  mode: { value: mode, source: "settings" }, lane: { value: lane, source: lane ? "settings" : "default" },
  user: { value: "operator", source: "default" }, host: { value: "example-server", source: "default" },
  broker: { value: "/opt/bob/portal/portal-broker.py", source: "default" } });

let calls, mode, lane, conversations, repairState, upgrade, profilesReply, reconnectFails, openFails;
function reset() {
  calls = []; mode = "this_pc"; lane = "dad"; conversations = []; repairState = "healthy"; upgrade = null;
  profilesReply = () => profilesWith(mode === "this_pc" ? PC_TRANSPORT : SERVER_TRANSPORT); reconnectFails = false; openFails = false;
  localStorage.clear();
}
const fail = (code, detail) => JSON.stringify({ code, detail, hint: "harness" });
async function invoke(cmd, args) {
  calls.push({ cmd, args });
  switch (cmd) {
    case "portal_connection_get": return connectionPayload(mode, lane);
    case "portal_connection_set": mode = args.mode; lane = args.mode === "this_pc" ? args.lane : ""; return connectionPayload(mode, lane);
    case "portal_profiles": return profilesReply();
    case "portal_hello": return HELLO;
    case "portal_catalog": return CATALOG;
    case "portal_conversations": return { items: conversations, limit: 50, offset: 0, excluded: 0 };
    case "portal_conversation_open":
      if (openFails) throw fail("transport_unavailable", "the portal link closed");
      return { conversation: CONVERSATION, messages: [{ id: "m1", role: "assistant", content: "hello", created_at: "2026-11-13T00:00:00Z" }], has_more: false };
    case "portal_pins": return { face_id: null, backend: null, model: null };
    case "portal_reconnect":
      if (reconnectFails) throw fail("transport_unavailable", "broker did not answer");
      return { target: mode === "this_pc" ? lane : "example-server", hello: HELLO };
    case "pc_poll": return { requests: [], outcomes: {} };
    case "runtime_repair_status": return { repairable: true, observation: { state: repairState, observed: repairState === "healthy" ? "BoB health checks passed." : repairState === "wedged" ? "WSL session failed with E_UNEXPECTED." : LINK_DOWN } };
    case "runtime_upgrade_status": if (!upgrade) throw "no local upgrade surface"; return { ...upgrade };
    default: throw fail("bad_request", `unexpected ${cmd}`);
  }
}
const flush = async (fn) => act(async () => { if (fn) await fn(); await Promise.resolve(); });
const emit = (frame) => flush(async () => { for (const cb of [...listeners]) cb({ payload: JSON.stringify(frame) }); });
const text = () => document.body.textContent;
const count = (cmd) => calls.filter((c) => c.cmd === cmd).length;
const rail = () => document.querySelector(".rail-link");
const linkDown = () => document.querySelector("[data-testid='link-down']");
const linkReconnect = () => document.querySelector("[data-testid='link-reconnect']");
const reconnectButtons = () => [...document.querySelectorAll("button")].filter((b) => /^Reconnect/.test(b.textContent.trim()));
const upgradeCard = () => document.querySelector('[aria-label="Runtime update"]');
const noReplay = () => { assert.equal(count("portal_send"), 0, "nothing is ever sent or replayed"); assert.equal(count("portal_conversation_new"), 0); };
let root;
async function mount() {
  document.getElementById("root").innerHTML = "";
  root = createRoot(document.getElementById("root"));
  await flush(() => root.render(React.createElement(PortalApp, { invoke, profiles: null, onProfiles: () => {} })));
  await flush(); await flush();
}
const unmount = () => flush(() => root.unmount());
const dropLink = () => emit({ event: "transport_fault", data: { code: "transport_unavailable", detail: "the portal link closed" } });

(async () => {
  // ---- F1 + F3: This PC, no session open, link dropped by an in-app runtime upgrade ----------
  reset();
  upgrade = { installed: { build_id: "c4" }, bundled: { build_id: "c5" }, available: true, running: true, upgrade: { id: "c4-c5", phase: "health", outcome: "running" } };
  await mount();
  assert.equal(rail().textContent, "online"); assert.equal(linkDown(), null, "no Reconnect banner on a healthy link");
  assert.equal(document.getElementById("composer").disabled, true, "no session is open");
  // The upgrade finishes, the runtime reports healthy services with this desktop's link down, and
  // the native link announces its own death with no call in flight: exactly the observed state.
  upgrade = { ...upgrade, available: false, running: false, installed: { build_id: "c5" }, upgrade: { id: "c4-c5", phase: "done", outcome: "completed" } };
  await flush(() => window.dispatchEvent(new Event("focus")));
  await dropLink();
  const notice = document.querySelector(".window-fault-notice");
  assert.match(notice.textContent, /The link to Bob on this PC is down/);
  assert.equal(document.querySelector(".fault-inline"), null, "link faults have one window notice");
  assert.equal(rail().textContent, "offline", "a dropped link reads offline");
  assert.ok(linkDown(), "the dead link has a Reconnect banner although no fault banner was raised");
  assert.equal(document.querySelectorAll(".window-fault-notice").length, 1);
  assert.ok(linkReconnect() && !linkReconnect().disabled, "with a Reconnect button, no session open");
  assert.doesNotMatch(text(), /server Bob/, "This PC mode never says 'server Bob'");
  assert.match(upgradeCard().textContent, /updated successfully\. Reconnect to continue\./);
  const inCard = [...upgradeCard().querySelectorAll("button")].find((b) => b.textContent === "Reconnect");
  assert.ok(inCard, "the update card's finished note carries its own Reconnect button");
  assert.equal(count("portal_reconnect"), 0, "reconnect is never automatic");
  // A failed reconnect keeps both the control and the finished note.
  reconnectFails = true;
  await flush(() => inCard.click()); await flush();
  assert.equal(count("portal_reconnect"), 1, "the card's button runs the reconnect path");
  assert.equal(rail().textContent, "offline");
  assert.ok(reconnectButtons().length >= 1, "a failed reconnect still leaves a Reconnect control");
  assert.match(upgradeCard().textContent, /updated successfully/, "and the finished note");
  assert.doesNotMatch(text(), /server Bob/);
  // A successful one clears the offline state and retires the finished note.
  reconnectFails = false;
  await flush(() => reconnectButtons()[0].click()); await flush(); await flush();
  assert.equal(count("portal_reconnect"), 2);
  assert.equal(rail().textContent, "online");
  assert.equal(linkDown(), null); assert.equal(reconnectButtons().length, 0, "no Reconnect control once the link is back");
  assert.match(text(), /Reconnected to dad\. No conversation was open/);
  assert.equal(upgradeCard(), null, "the completed note goes away after a successful reconnect");
  await flush(() => window.dispatchEvent(new Event("focus")));
  assert.equal(upgradeCard(), null, "and a later status read does not bring it back");
  noReplay(); assert.equal(count("portal_conversation_open"), 0, "no session was opened behind the user's back");
  await unmount();

  // ---- F3: "stuck" is the wedged state only -------------------------------------------------
  reset(); repairState = "unreachable";
  await mount();
  assert.equal(rail().textContent, "online", "healthy services + a live link is not 'stuck'");
  assert.ok(!rail().classList.contains("stuck"));
  assert.match(document.getElementById("repair-card").textContent, /broker connection is down/, "Repair stays offered in Activity");
  await dropLink();
  assert.equal(rail().textContent, "offline", "healthy runtime with the desktop link down shows the normal offline state");
  assert.ok(rail().classList.contains("dead")); assert.ok(!rail().classList.contains("stuck"));
  assert.doesNotMatch(rail().getAttribute("title") ?? "", /broker connection is down/);
  assert.ok(linkReconnect(), "and the F1 Reconnect control");
  await unmount();
  reset(); repairState = "wedged";
  await mount();
  assert.equal(rail().textContent, "stuck", "a wedged runtime still reads stuck");
  assert.ok(rail().classList.contains("stuck")); assert.match(rail().getAttribute("title"), /E_UNEXPECTED/);
  await dropLink();
  assert.equal(rail().textContent, "stuck", "wedged outranks offline"); assert.ok(linkReconnect(), "Reconnect is still offered");
  await unmount();

  // ---- F1: server mode with a session open --------------------------------------------------
  reset(); mode = "server"; lane = ""; conversations = [CONVERSATION];
  await mount();
  await flush(async () => document.querySelector(".conversation-list button").dispatchEvent(new MouseEvent("click", { bubbles: true })));
  assert.equal(document.getElementById("composer").disabled, false, "a session is open");
  const opensBefore = count("portal_conversation_open");
  await dropLink();
  assert.match(document.querySelector(".window-fault-notice").textContent, /The link to the server Bob is down/, "server wording stays specific");
  assert.equal(document.querySelector(".fault-inline"), null);
  assert.ok(linkReconnect() && !linkReconnect().disabled, "Reconnect is offered with a session open");
  assert.equal(count("portal_reconnect"), 0);
  await flush(() => linkReconnect().click()); await flush(); await flush();
  assert.equal(count("portal_reconnect"), 1);
  assert.equal(count("portal_conversation_open"), opensBefore + 1, "the existing path re-reads the open session's history");
  assert.match(text(), /Reconnected to example-server\. History re-read: 1 message\(s\)\. Nothing was replayed\./);
  assert.equal(rail().textContent, "online"); assert.equal(linkDown(), null);
  noReplay();
  // A fault raised by a call keeps its own banner; the two never stack.
  openFails = true;
  await flush(async () => document.querySelector(".conversation-list button").dispatchEvent(new MouseEvent("click", { bubbles: true })));
  assert.equal(rail().textContent, "offline");
  assert.equal(linkDown(), null, "the fault banner already carries the control");
  assert.deepEqual(reconnectButtons().map((b) => b.textContent.trim()), ["Reconnect and inspect history"]);
  await unmount();

  // ---- F4: Settings → Connection ------------------------------------------------------------
  const pane = () => document.querySelector(".settings-pane");
  const facts = () => document.querySelector("[data-testid='connection-facts']");
  const factRows = () => { const out = {}; const kids = [...facts().children]; for (let i = 0; i < kids.length; i += 2) out[kids[i].textContent] = kids[i + 1].textContent; return out; };
  const button = (label) => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === label);
  async function settings(profiles) {
    document.getElementById("root").innerHTML = "";
    root = createRoot(document.getElementById("root"));
    await flush(() => root.render(React.createElement(SettingsView, { invoke, client: createPortalClient(invoke), hello: HELLO, catalog: null, profiles,
      onProfiles: () => {}, onReconnect: () => {}, appearance: DEFAULT_APPEARANCE, onAppearance: () => {}, pendingApprovals: 0 })));
    await flush(() => button("Connection").click()); await flush(); await flush();
  }
  // This PC, with the startup snapshot still describing the server (it is read before first-run
  // setup saves This PC): the observed state. The stale SSH description must not be listed.
  reset();
  await settings(profilesWith(SERVER_TRANSPORT));
  assert.ok(document.querySelector('input[name="connection-mode"][value="this_pc"]').checked);
  assert.deepEqual(factRows(), { mode: "This PC", lane: "dad", "runtime transport": "wsl_stdio_jsonl",
    "runtime program": "C:\\Windows\\System32\\wsl.exe", "settings file": "C:\\data\\portal-settings.json · user-only permissions" });
  for (const detail of SSH_DETAILS) assert.doesNotMatch(pane().textContent, detail, `This PC never lists ${detail}`);
  assert.doesNotMatch(facts().textContent, /broker/i, "no broker row in This PC mode");
  assert.match(pane().textContent, /Bob on this PC\s*version t/);
  await unmount();
  // Same, but the native layer cannot re-describe the transport: say so, do not guess.
  reset(); profilesReply = () => { throw fail("bad_request", "profiles unavailable"); };
  await settings(profilesWith(SERVER_TRANSPORT));
  assert.deepEqual(factRows(), { mode: "This PC", lane: "dad", "runtime transport": "not reported",
    "settings file": "C:\\data\\portal-settings.json · user-only permissions" });
  for (const detail of SSH_DETAILS) assert.doesNotMatch(pane().textContent, detail);
  await unmount();
  // An older native build that sends no `mode` in the transport: its kind still decides.
  reset(); profilesReply = () => profilesWith({ kind: "wsl_stdio_jsonl", targets: ["dad"], ssh: "C:\\Windows\\System32\\wsl.exe", broker: "bob-broker" });
  await settings(null);
  assert.equal(factRows()["runtime transport"], "wsl_stdio_jsonl"); assert.equal(factRows().lane, "dad");
  await unmount();
  // Server mode keeps its SSH details exactly as before.
  reset(); mode = "server"; lane = "";
  await settings(profilesWith(SERVER_TRANSPORT));
  assert.deepEqual(factRows(), { mode: "A Bob server", transport: "ssh_stdio_jsonl · example-server → 192.0.2.35",
    ssh: "C:\\Windows\\System32\\OpenSSH\\ssh.exe", broker: "/opt/bob/portal/portal-broker.py",
    "settings file": "C:\\data\\portal-settings.json · user-only permissions" });
  assert.match(pane().textContent, /server\s*version t/);
  // Saving This PC from here switches the details with it (same command first run uses).
  await flush(() => document.querySelector('input[name="connection-mode"][value="this_pc"]').click());
  await flush(() => { const node = document.getElementById("connection-lane");
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(node, "luna");
    node.dispatchEvent(new Event("input", { bubbles: true })); });
  profilesReply = () => profilesWith({ ...PC_TRANSPORT, targets: ["luna"], lane: "luna" });
  await flush(() => button("Save").click()); await flush(); await flush();
  assert.deepEqual(calls.filter((c) => c.cmd === "portal_connection_set").at(-1).args, { mode: "this_pc", lane: "luna" });
  assert.equal(factRows().mode, "This PC"); assert.equal(factRows().lane, "luna"); assert.equal(factRows()["runtime transport"], "wsl_stdio_jsonl");
  for (const detail of SSH_DETAILS.slice(0, 5)) assert.doesNotMatch(facts().textContent, detail, "the server details leave with the mode");
  await unmount();
  // The rule itself, for the cases a screen cannot reach.
  assert.deepEqual(connectionFacts(null, "", SERVER_TRANSPORT), [], "no mode reported: nothing is claimed");
  assert.deepEqual(connectionFacts("this_pc", "", null), [["mode", "This PC", false], ["lane", "not reported", false], ["runtime transport", "not reported", false]]);
  assert.deepEqual(connectionFacts("server", "", PC_TRANSPORT), [["mode", "A Bob server", false], ["transport", "not reported", false]],
    "a This PC description is never shown as the server's");
  dom.window.close();
  console.log("windows-setup-fixes-ui: dead link always offers Reconnect (no session / open session / after a failed reconnect), explicit reconnect only and no replay, update-card Reconnect and retirement, This PC wording, stuck only when wedged, Connection details by mode (stale snapshot, unavailable profiles, older native, server unchanged, switch on save) PASS");
})().catch(async (error) => { console.error(error); try { await unmount(); } catch { /* already gone */ } dom.window.close(); process.exitCode = 1; });
