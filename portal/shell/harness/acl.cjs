"use strict";
// E7 defect #1 regression: the webview's `listen()` calls need the explicit
// Tauri capability grant (EXEC-54 / capabilities/default.json). Without it the
// webview renders nothing at all and no wire test notices, because the ACL is
// enforced inside Tauri, not in the engine.
//
// This module makes that failure a test failure without needing a WebView2
// session (EXEC-51: WebView2 cannot initialise in a non-interactive Windows
// session):
//   1. the capability file must grant exactly core:event:allow-listen and
//      core:event:allow-unlisten to the one window;
//   2. client.ts must wire all three engine channels through `listen`;
//   3. a fake event bus enforcing (1) delivers the committed transcript lines
//      to the reducer, and REJECTS the subscription when the grant is removed
//      -- the negative control that proves this check bites.

const fs = require("node:fs");
const path = require("node:path");

const LISTEN = "core:event:allow-listen";
const UNLISTEN = "core:event:allow-unlisten";
const CHANNELS = ["slice://line", "slice://stderr", "slice://exit"];

function readCapabilities(repoRoot) {
  const p = path.join(repoRoot, "shell", "src-tauri", "capabilities", "default.json");
  return { path: p, caps: JSON.parse(fs.readFileSync(p, "utf8")) };
}

function readClientSource(repoRoot) {
  return fs.readFileSync(path.join(repoRoot, "shell", "src", "client.ts"), "utf8");
}

/** EXEC-54: exactly the two event permissions, on the one window. */
function assertCapabilitiesGrantListen(repoRoot) {
  const { path: p, caps } = readCapabilities(repoRoot);
  const perms = Array.isArray(caps.permissions) ? caps.permissions : [];
  const missing = [LISTEN, UNLISTEN].filter((x) => !perms.includes(x));
  if (missing.length > 0) {
    throw new Error(
      `${p}: capability '${caps.identifier}' does not grant ${missing.join(", ")}; ` +
        `without it the webview's listen() is ACL-denied and no frame ever reaches the reducer (E7 defect #1)`
    );
  }
  const extra = perms.filter((x) => !x.startsWith("core:event:"));
  if (extra.length > 0) {
    throw new Error(`${p}: unexpected non-event permission(s) ${extra.join(", ")}`);
  }
  if (!Array.isArray(caps.windows) || caps.windows.length !== 1 || caps.windows[0] !== "main") {
    throw new Error(`${p}: the capability must apply to exactly the one window ["main"]`);
  }
  return caps;
}

/** client.ts must subscribe to all three channels through `listen`. */
function assertClientWiresListen(repoRoot) {
  const src = readClientSource(repoRoot);
  for (const ch of CHANNELS) {
    const call = new RegExp(`listen<[^>]*>\\(\\s*"${ch.replace(/[/:]/g, "\\$&")}"`);
    if (!call.test(src)) {
      throw new Error(
        `shell/src/client.ts does not call listen("${ch}") — that channel can never reach the reducer`
      );
    }
  }
  const wired = [...src.matchAll(/listen<[^>]*>\(\s*"([^"]+)"/g)].map((m) => m[1]);
  const unexpected = wired.filter((c) => !CHANNELS.includes(c));
  if (unexpected.length > 0) {
    throw new Error(`shell/src/client.ts listens on unexpected channel(s) ${unexpected.join(", ")}`);
  }
  return wired;
}

/** The two channels client.ts actually listens on, in source order. */
function listenChannels(repoRoot) {
  return [...readClientSource(repoRoot).matchAll(/listen<[^>]*>\(\s*"([^"]+)"/g)].map((m) => m[1]);
}

/** A stand-in for Tauri's event API that enforces the capability file. */
class FakeEventBus {
  constructor(capabilities) {
    this.capabilities = capabilities;
    this.permissions = Array.isArray(capabilities.permissions) ? capabilities.permissions : [];
    this.subs = new Map();
  }

  async listen(channel, cb) {
    if (!this.permissions.includes(LISTEN)) {
      throw new Error(
        `event.listen ACL denial: capability '${this.capabilities.identifier}' has no ${LISTEN}`
      );
    }
    const list = this.subs.get(channel) ?? [];
    list.push(cb);
    this.subs.set(channel, list);
    const self = this;
    return async function unlisten() {
      if (!self.permissions.includes(UNLISTEN)) {
        throw new Error(`event.unlisten ACL denial: no ${UNLISTEN}`);
      }
      self.subs.set(channel, (self.subs.get(channel) ?? []).filter((f) => f !== cb));
    };
  }

  emit(channel, payload) {
    for (const cb of this.subs.get(channel) ?? []) cb({ payload });
  }
}

module.exports = {
  LISTEN,
  UNLISTEN,
  CHANNELS,
  FakeEventBus,
  assertCapabilitiesGrantListen,
  assertClientWiresListen,
  listenChannels,
  readCapabilities,
};
