'use strict';
// S2 truth surfaces: Linux runtime health must not offer a Windows repair, and Memory must not
// show a green health score or a stale embedder when the runtime says otherwise.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { SelfRepair } = require('../src/portal/SelfRepair.tsx');
const { MemoryExplorer } = require('../src/portal/MemoryExplorer.tsx');
async function flush(fn) { await act(async () => { if (fn) await fn(); await Promise.resolve(); }); }
const text = () => document.body.textContent;
const button = (label) => [...document.querySelectorAll('button')].find((b) => b.textContent === label);
let root;
async function mount(element) { root = createRoot(document.getElementById('root')); await flush(() => root.render(element)); await flush(); }
async function unmount() { await flush(() => root.unmount()); }
const embedder = (model, extra = {}) => ({ selected_model: model, embedding_dimension: model === '4b' ? 2560 : 1024, ready: false, file_present: true,
  sha_ok: true, service_active: true, recommended_model: model, ram_gb: 8, setup: null, model_loaded: false, configured: true,
  alias: model === '4b' ? 'qwen3-embedding-4b' : 'qwen3-embedding-0.6b', installed_models: [model], ...extra });
(async () => {
  // SETUP-SOL-REV-003: only a positive capability permits mutation.
  for (const reason of ['remote-server connection is read-only', 'invalid container configuration']) {
    let reads = 0, writes = 0;
    const rejected = async cmd => { if (cmd !== 'runtime_repair_status') { writes++; throw 'unexpected mutation'; } reads++; throw reason; };
    for (let remount = 0; remount < 2; remount++) {
      await mount(React.createElement(SelfRepair, { invoke: rejected, settings: true }));
      assert.equal(!!button('Repair'), false, 'rejected status never offers Repair');
      assert.equal(text().includes(reason), true, 'status rejection remains visible');
      await unmount();
    }
    assert.equal(reads, 2); assert.equal(writes, 0);
  }
  let resolveStatus;
  await mount(React.createElement(SelfRepair, { invoke: () => new Promise(resolve => { resolveStatus = resolve; }), settings: true }));
  assert.equal(!!button('Repair'), false, 'loading has no Repair');
  assert.match(text(), /checking.*status/i);
  await flush(() => resolveStatus({ observation: { state: 'wedged', observed: 'Unknown capability' } }));
  assert.equal(!!button('Repair'), false, 'unknown capability has no Repair');
  await unmount();

  // --- Linux: honest runtime status, never an actionable WSL/Windows repair.
  const linux = { observation: { state: 'unreachable', observed: "bob-runtime: Your lane's gateway is not answering its health check." }, repairable: false, receipts: [] };
  await mount(React.createElement(SelfRepair, { invoke: async (cmd) => { assert.equal(cmd, 'runtime_repair_status'); return linux; }, settings: true }));
  assert.match(text(), /gateway is not answering/, 'the real failure stays visible');
  assert.equal(!!button('Repair'), false, 'no Windows repair button on a container runtime');
  assert.doesNotMatch(text(), /WSL|Windows/i);
  assert.match(text(), /No automatic repair is available/);
  await unmount();
  // Windows keeps its repair button.
  const windows = { observation: { state: 'wedged', observed: 'BoB: Windows could not query WSL.' }, repairable: true, receipts: [] };
  await mount(React.createElement(SelfRepair, { invoke: async () => windows, settings: true }));
  assert.ok(button('Repair'), 'Windows behaviour is unchanged'); await unmount();

  // Existing native Windows receipts omit repairable; only a positively identified native
  // Windows host preserves those controls. Browser/Linux/unknown capability stays unavailable.
  const originalAgent = window.navigator.userAgent;
  Object.defineProperty(window.navigator, 'userAgent', { configurable: true, value: 'Windows NT 10.0' });
  window.__TAURI_INTERNALS__ = {};
  const legacyWindows = { observation: windows.observation, receipts: [] };
  await mount(React.createElement(SelfRepair, { invoke: async () => legacyWindows, settings: true }));
  assert.equal(!!button('Repair'), true, 'legacy native Windows receipt still supports repair');
  await unmount();
  await mount(React.createElement(SelfRepair, { invoke: async () => linux, settings: true }));
  assert.equal(!!button('Repair'), false, 'explicit unsupported overrides host detection');
  await unmount();
  await mount(React.createElement(SelfRepair, { invoke: async () => { throw 'remote'; }, settings: true }));
  assert.equal(!!button('Repair'), false, 'Windows host alone cannot enable repair on rejection');
  await unmount();
  delete window.__TAURI_INTERNALS__;
  Object.defineProperty(window.navigator, 'userAgent', { configurable: true, value: originalAgent });

  // --- Memory: failed load is not Health 100%; embedder state is the runtime's own receipt.
  const failing = { memoryStatus: async () => { throw JSON.stringify({ code: 'gateway_http_500', detail: 'memory unavailable', hint: 'check the gateway' }); } };
  const props = (client, invoke) => ({ client, cache: { items: null, status: null, calls: 0, at: 0 }, operations: {}, onAsk: () => {}, invoke });
  let status = embedder('0.6b', { ready: true, model_loaded: true });
  const invoke = async (cmd) => { assert.equal(cmd, 'runtime_embedder_status'); return { ...status }; };
  await mount(React.createElement(MemoryExplorer, props(failing, invoke)));
  assert.doesNotMatch(text(), /Health \d+%/, 'an error is never a 100% score');
  assert.match(text(), /Health unavailable/); assert.match(text(), /gateway_http_500/);
  assert.doesNotMatch(text(), /Semantic memory ready/);
  assert.match(text(), /Embedder ready · 0\.6B · qwen3-embedding-0\.6b · 1024-dimension vectors/);
  await unmount();
  status = embedder('4b');
  await mount(React.createElement(MemoryExplorer, props(failing, invoke)));
  assert.match(text(), /4B model files are downloaded and verified, but not loaded yet/);
  await unmount();
  const empty = { memoryStatus: async () => ({ total: 0 }), memoryList: async () => ({ items: [], total: 0, has_more: false }), memorySearch: async () => ({ hits: [] }) };
  await mount(React.createElement(MemoryExplorer, props(empty, async () => { throw 'remote'; })));
  assert.doesNotMatch(text(), /Health 100%/, 'zero memories is not a perfect score');
  assert.doesNotMatch(text(), /Semantic memory/, 'no embedder claim when the runtime cannot say');
  await unmount();
  console.log('setup truth UI: Linux status without repair, Windows repair kept, Memory error/empty not Health 100%, shared embedder receipt PASS');
})().catch((error) => { console.error(error); process.exit(1); });
