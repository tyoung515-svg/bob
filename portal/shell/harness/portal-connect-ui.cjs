"use strict";
// UI-level regression harness for the portal connect step: a catalogue the gateway
// answers with an HTTP error must not discard a successful hello (observed
// 2026-11-01 on the candidate: "server unreachable" with a working link), while a
// transport fault on the catalogue still marks the link dead.
//
// No Tauri, no server, no model call: every reply is a fixture.
//
// Run from shell/:  node -r ./harness/workbench-ui-register.cjs harness/portal-connect-ui.cjs

const assert = require("node:assert/strict");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
for (const name of ["window", "document", "HTMLElement", "Event", "MouseEvent", "KeyboardEvent"]) {
  global[name] = dom.window[name];
}
global.IS_REACT_ACT_ENVIRONMENT = true;

const eventModule = require.resolve("@tauri-apps/api/event", { paths: [path.resolve(__dirname, "..")] });
require.cache[eventModule] = {
  id: eventModule,
  filename: eventModule,
  loaded: true,
  children: [],
  paths: [],
  exports: { listen: async () => () => {} },
};

const React = require("react");
const { act } = React;
const { createRoot } = require("react-dom/client");

const HELLO = {
  identity: {
    sub: "dad",
    gateway: "http://127.0.0.1:7846",
    faces: [],
    may_touch: [],
    may_not_touch: [],
    expires_at: "2026-11-13T00:00:00Z",
  },
  server: { boot: null, version: null },
  gaps: [],
};
const EMPTY_CATALOG = { faces: [], models: [], backends: [], capabilities: { faces: 0, backends: 0, actions: [], warnings: [] } };

const fail = (code, detail) => JSON.stringify({ code, detail, hint: "harness" });

function makeInvoke(catalogReply) {
  return async (cmd) => {
    switch (cmd) {
      case "portal_connection_get": return { connection_configured: true };
      case "portal_hello":
        return HELLO;
      case "portal_catalog":
        return catalogReply();
      case "portal_conversations":
        return { items: [], limit: 50, offset: 0, excluded: 0 };
      case "portal_pins":
        return { face_id: null, backend: null, model: null, profile: null, locale: null };
      default:
        throw fail("bad_request", `unexpected ${cmd}`);
    }
  };
}

async function flush(fn) {
  await act(async () => {
    if (fn) await fn();
    await Promise.resolve();
  });
}

async function mount(invoke) {
  document.getElementById("root").innerHTML = "";
  const root = createRoot(document.getElementById("root"));
  const PortalApp = require("../src/portal/PortalApp.tsx").default;
  await flush(() => root.render(React.createElement(PortalApp, { invoke, profiles: null, onProfiles: () => {} })));
  await flush();
  return root;
}

const originalNow = Date.now, originalTimeout = global.setTimeout, originalClear = global.clearTimeout;
let fakeNow, fakeTimers, fakeId = 0;
function retryClock() {
  fakeNow = originalNow(); fakeTimers = new Map(); Date.now = () => fakeNow;
  global.setTimeout = (fn, ms = 0) => { const id = ++fakeId; fakeTimers.set(id, { fn, at: fakeNow + Number(ms) }); return id; };
  global.clearTimeout = id => fakeTimers.delete(id);
}
function restoreClock() { Date.now = originalNow; global.setTimeout = originalTimeout; global.clearTimeout = originalClear; }
async function exhaustCatalog() {
  const end = fakeNow + 60000;
  for (let i = 0; i < 100; i++) {
    const next = [...fakeTimers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
    if (!next) { fakeNow = end; await flush(); return; }
    fakeNow = next[1].at; fakeTimers.delete(next[0]); await flush(next[1].fn);
  }
  throw Error('catalog retry did not finish');
}
(async () => {
  // 1. The gateway answers the catalogue with an HTTP error: the link is fine.
  retryClock();
  let root = await mount(
    makeInvoke(() => {
      throw fail("gateway_http_500", "catalog: gateway answered with errors");
    }),
  );
  assert.ok(!document.querySelector(".window-fault-notice"), "transient catalog errors stay quiet during retries");
  await exhaustCatalog();
  assert.ok(document.body.textContent.includes("identity dad"), "the successful hello is kept when the catalogue fails over HTTP");
  assert.ok(!document.body.textContent.includes("server unreachable"), "an HTTP-error catalogue is not 'server unreachable'");
  assert.ok(document.body.textContent.includes("catalogue could not be refreshed"), "the notice says what is missing");
  await flush(async () => root.unmount());

  restoreClock();
  // 2. A persistent catalogue transport fault becomes visible after the retry budget.
  retryClock();
  root = await mount(
    makeInvoke(() => {
      throw fail("transport_unavailable", "gateway catalog endpoints unreachable");
    }),
  );
  await exhaustCatalog();
  assert.ok(document.querySelector(".rail-link.dead"), "a persistent transport fault marks the link offline");
  assert.ok(document.body.textContent.includes("gateway catalog endpoints unreachable"));
  await flush(async () => root.unmount());

  restoreClock();
  // 3. A healthy (even empty) catalogue is unchanged.
  root = await mount(makeInvoke(() => EMPTY_CATALOG));
  assert.ok(document.body.textContent.includes("identity dad"), "an empty catalogue connects");
  assert.ok(!document.body.textContent.includes("catalogue could not be refreshed"), "no catalogue notice when it loaded");
  await flush(async () => root.unmount());

  // Settings uses the real native-command boundary with supplied fixtures.
  const { SettingsView, DEFAULT_APPEARANCE } = require("../src/portal/SettingsView.tsx");
  const connection = {
    mode: { value: "server", source: "default" },
    lane: { value: "", source: "default" },
    user: { value: "operator", source: "default" },
    host: { value: "env-server", source: "env" },
    broker: { value: "/saved/broker.py", source: "settings" },
  };
  const calls = [];
  let refuseSave = false;
  let effectiveConnection = connection;
  let laneOverride = null;
  let modeOverride = null;
  const invoke = async (cmd, args) => {
    calls.push({ cmd, args });
    if (cmd === "portal_connection_get") return effectiveConnection;
    assert.equal(cmd, "portal_connection_set");
    if (refuseSave) throw fail("bad_request", "invalid connection host: use a bare host name or address without shell syntax");
    if (args.mode === "this_pc" && !/^[a-z][a-z0-9-]{0,31}$/.test(args.lane))
      throw fail("bad_request", "invalid connection lane: use [a-z][a-z0-9-]{0,31}");
    effectiveConnection = {
      mode: { value: modeOverride ?? args.mode, source: modeOverride ? "env" : "settings" },
      lane: { value: laneOverride ?? args.lane ?? "", source: laneOverride ? "env" : args.mode === "this_pc" ? "settings" : "default" },
      user: args.mode === "server" ? { value: args.user, source: "settings" } : connection.user,
      host: connection.host, // Environment still wins after a successful save.
      broker: args.mode === "server" ? { value: args.broker, source: "settings" } : connection.broker,
    };
    return effectiveConnection;
  };
  root = createRoot(document.getElementById("root"));
  await flush(() => root.render(React.createElement(SettingsView, {
    invoke, client: {}, hello: null, catalog: null, profiles: null,
    onProfiles: () => {}, onReconnect: () => {}, appearance: DEFAULT_APPEARANCE,
    onAppearance: () => {}, pendingApprovals: 0,
  })));
  const button = (text) => [...document.querySelectorAll("button")].find((node) => node.textContent === text);
  await flush(() => button("Connection").click());
  const section = document.querySelector('[aria-label="Server connection"]');
  assert.ok(section, "Server connection section renders in Settings");
  const switchTo = async (mode) => flush(() => section.querySelector(`input[name="connection-mode"][value="${mode}"]`).click());
  assert.ok(section.querySelector('input[value="server"]').checked, "existing installs default to A Bob server");
  assert.ok(section.textContent.includes("This PC") && section.textContent.includes("A Bob server"));
  assert.equal(document.getElementById("connection-lane"), null);
  for (const field of ["user", "host", "broker"]) {
    assert.equal(document.getElementById(`connection-${field}`).value, connection[field].value);
    assert.ok(section.textContent.includes(`Source: ${connection[field].source}`));
  }
  async function change(field, value) {
    await flush(() => {
      const input = document.getElementById(`connection-${field}`);
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
  await change("user", "dad");
  await change("host", "saved-server");
  await change("broker", "/srv/portal/broker.py");
  await flush(() => section.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  assert.deepEqual(calls.find((call) => call.cmd === "portal_connection_set").args, {
    mode: "server", user: "dad", host: "saved-server", broker: "/srv/portal/broker.py",
  });
  assert.match(section.querySelector('[role="status"]').textContent, /Saved/);
  assert.equal(document.getElementById("connection-host").value, "env-server", "effective environment override remains visible");
  assert.match(document.getElementById("connection-user").parentElement.textContent, /Source: settings/);
  refuseSave = true;
  await change("host", "bad host");
  await flush(() => button("Save").click());
  assert.match(section.querySelector('[role="alert"]').textContent, /invalid connection host/);
  assert.equal(section.querySelector('[role="status"]'), null, "a refusal clears the prior success notice");
  assert.equal(document.getElementById("connection-host").value, "bad host", "refused input stays editable");
  refuseSave = false;
  await switchTo("this_pc");
  assert.equal(section.querySelector('[role="alert"]'), null, "switching clears the old refusal");
  for (const field of ["user", "host", "broker"]) assert.equal(document.getElementById(`connection-${field}`), null);
  assert.equal(document.getElementById("connection-lane").value, "");
  await flush(() => button("Save").click());
  assert.match(section.querySelector('[role="alert"]').textContent, /invalid connection lane/);
  await change("lane", "dad");
  await flush(() => button("Save").click());
  assert.deepEqual(calls.filter((call) => call.cmd === "portal_connection_set").at(-1).args, { mode: "this_pc", lane: "dad" });
  assert.match(section.querySelector('[role="status"]').textContent, /Saved/);
  assert.match(document.getElementById("connection-lane").parentElement.textContent, /Source: settings/);
  await switchTo("server");
  assert.equal(document.getElementById("connection-lane"), null);
  assert.ok(document.getElementById("connection-host"));
  await switchTo("this_pc");
  assert.equal(document.getElementById("connection-lane").value, "dad", "switching keeps the draft lane");
  laneOverride = "env-lane";
  await change("lane", "saved-lane");
  await flush(() => button("Save").click());
  assert.equal(document.getElementById("connection-lane").value, "env-lane");
  assert.match(document.getElementById("connection-lane").parentElement.textContent, /Source: env/);
  await flush(() => root.unmount());

  root = createRoot(document.getElementById("root"));
  await flush(() => root.render(React.createElement(SettingsView, {
    invoke, client: {}, hello: null, catalog: null, profiles: null,
    onProfiles: () => {}, onReconnect: () => {}, appearance: DEFAULT_APPEARANCE,
    onAppearance: () => {}, pendingApprovals: 0,
  })));
  await flush(() => button("Connection").click());
  assert.ok(document.querySelector('input[value="this_pc"]').checked, "saved This PC loads selected");
  assert.equal(document.getElementById("connection-lane").value, "env-lane");
  assert.equal(document.getElementById("connection-user"), null);
  modeOverride = "server";
  await flush(() => button("Save").click());
  assert.ok(document.querySelector('input[value="server"]').checked, "effective mode environment override remains visible after saving This PC");
  assert.match(document.querySelector('[aria-label="Server connection"] fieldset').textContent, /Source: env/);
  assert.equal(document.getElementById("connection-lane"), null);
  assert.ok(document.getElementById("connection-user"));
  await flush(() => root.unmount());

  // Invalid saved settings are visible on load, and can be corrected by saving.
  root = createRoot(document.getElementById("root"));
  await flush(() => root.render(React.createElement(SettingsView, {
    invoke: async () => { throw fail("bad_request", "saved connection user must be a string"); },
    client: {}, hello: null, catalog: null, profiles: null, onProfiles: () => {},
    onReconnect: () => {}, appearance: DEFAULT_APPEARANCE, onAppearance: () => {}, pendingApprovals: 0,
  })));
  await flush(() => button("Connection").click());
  assert.match(document.querySelector('[aria-label="Server connection"] [role="alert"]').textContent, /saved connection user must be a string/);
  await flush(() => root.unmount());
  console.log("portal Settings server/This PC switch, relevant fields, mode-specific save payloads, reload, environment precedence, and visible refusals PASS");

  console.log(
    "portal-connect-ui: an HTTP-error catalogue keeps the hello and says the catalogue is missing; a transport fault on the catalogue still reads as unreachable; an empty catalogue connects unchanged PASS",
  );
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
