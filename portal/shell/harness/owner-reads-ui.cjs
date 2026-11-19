"use strict";
// Offline owner-read boundary tests. Real PortalApp/MemoryExplorer/MemoryBrain/
// IdeEditor; only native IO, WebGL, Monaco and xterm are doubles.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'KeyboardEvent']) global[name] = dom.window[name];
global.self = global.window;
global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = class { observe() {} disconnect() {} };
global.requestAnimationFrame = (fn) => { fn(); return 0; };

let graphData;
const modelRows = [];
const editors = [];
const disposable = { dispose() {} };
const monaco = {
  languages: { getLanguages: () => [], register() {}, setMonarchTokensProvider() {} },
  Uri: { file: (path) => path, parse: (path) => path },
  editor: {
    createModel(content) {
      const row = { content, disposed: false, getValue() { return this.content; }, getValueInRange() { return this.content; },
        dispose() { this.disposed = true; }, getAlternativeVersionId() { return 1; }, onDidChangeContent() { return disposable; } };
      modelRows.push(row); return row;
    },
    create() {
      const row = { options: {}, model: null, updateOptions(v) { Object.assign(this.options, v); }, setModel(v) { this.model = v; },
        onDidChangeCursorPosition() { return disposable; }, getSelection() { return null; }, focus() {}, dispose() {} };
      editors.push(row); return row;
    },
    createDiffEditor: () => ({ setModel() {}, dispose() {} }),
  },
};
const graph = new Proxy({}, { get: (_, key) => (...args) => { if (key === 'graphData') graphData = args[0]; return graph; } });
const Module = require('node:module');
const originalLoad = Module._load;
Module._load = function(request, ...args) {
  if (request.includes('monaco-editor')) return request.endsWith('?worker') ? class {} : monaco;
  if (request === '3d-force-graph') return () => () => graph;
  if (request === '@tauri-apps/api/event') return { listen: async () => () => {} };
  if (request === '@xterm/xterm') return { Terminal: class {
    cols = 100; rows = 24; loadAddon() {} open() {} write() {} dispose() {}
    onData() { return disposable; } onResize() { return disposable; }
  } };
  if (request === '@xterm/addon-fit') return { FitAddon: class { fit() {} } };
  return originalLoad.call(this, request, ...args);
};

const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { loadAll, describeAll, buildGraph, buildTree } = require('../src/portal/memory.ts');
const { createIdeClient, readServerText } = require('../src/portal/ide-client.ts');
const fail = (code) => JSON.stringify({ code, detail: code, hint: '' });
const operations = Object.fromEntries(['memory.list', 'files.roots', 'files.list', 'files.read'].map((op) => [op,
  { version: 1, implemented: true, readable: true, writable: false, runnable: false }]));
const memories = (owner, count = 57) => Array.from({ length: count }, (_, n) => ({
  source_id: `${owner}:${n}`, owner, kind: 'family_memory', revision: `r${n}`, truncated: false,
  provenance: { path: 'MEMORY.md', paragraph: n }, excerpt: n === 0 ? '!!!' : n === 1 ? '你好' : `${owner} memory ${n}`,
}));
const coverage = { complete_corpus: false, enumeration_complete: true, sources: ['family_memory'], counts_by_kind: { family_memory: 57 }, excluded: ['other owners', 'project/corpus files'] };
const button = (text) => [...document.querySelectorAll('button')].find((n) => n.textContent.trim() === text);
async function flush(fn) { await act(async () => { if (fn) await fn(); await Promise.resolve(); }); }
async function settle() { await act(async () => { await new Promise((r) => setTimeout(r, 20)); }); }
async function until(fn, name) { for (let n = 0; n < 100; n++) { if (fn()) return; await settle(); } throw new Error(`Timed out: ${name}`); }
async function click(text) { assert.ok(!!button(text), `button ${text}`); await flush(() => button(text).click()); await settle(); }
async function choose(label, value) { await flush(() => {
  const node = document.querySelector(`select[aria-label="${label}"]`);
  assert.ok(!!node, label); node.value = value; node.dispatchEvent(new Event('change', { bubbles: true }));
}); }

async function pureContracts() {
  const rows = memories('orchid-7');
  const cursors = [];
  const result = await loadAll({ memoryList: async (limit, cursor) => {
    assert.equal(limit, 50); cursors.push(cursor);
    return { items: cursor ? rows.slice(50) : rows.slice(0, 50), next_cursor: cursor ? null : 'opaque', snapshot: 'stable', coverage };
  }, memorySearch: async () => assert.fail('no letter search with memory.list') }, 1, () => {}, operations);
  assert.equal(result.hits.length, 57, 'list does not stop at a stale status count');
  assert.deepEqual(cursors, [undefined, 'opaque']);
  assert.equal(result.coverage, coverage);
  const items = describeAll(result.hits);
  assert.equal(buildTree(items).count, 57);
  assert.deepEqual(buildGraph(items).nodes.filter((n) => !n.hub).map((n) => n.id).sort(), rows.map((r) => r.source_id).sort());
  for (const advertised of [{}, undefined]) {
    let probes = 0, searches = 0;
    const old = await loadAll({ memoryList: async () => { probes++; throw fail('unknown_op'); }, memorySearch: async () => { searches++; return { items: [rows[2]] }; } }, null, () => {}, advertised);
    assert.equal(probes, advertised ? 0 : 1);
    assert.equal(searches, 8); assert.equal(old.mode, 'search'); assert.equal(old.hits.length, 1); assert.equal(old.coverage, null);
  }
  for (const code of ['snapshot_expired', 'scope_denied', 'transport_unavailable', 'invalid_cursor']) {
    await assert.rejects(loadAll({ memoryList: async () => { throw fail(code); }, memorySearch: async () => assert.fail('no fallback for a real error') }, null, () => {}, operations), (e) => e === fail(code));
  }
  await assert.rejects(loadAll({ memoryList: async () => ({ items: [], next_cursor: 'repeat', snapshot: 's', coverage }) }, null, () => {}, operations), /cursor repeated/);
  const textClient = createIdeClient(async (_, args) => args.offset === 0
    ? { content: Buffer.from([97, 226]).toString('base64'), encoding: 'base64', revision: 'r', truncated: true, next_offset: 2 }
    : { content: Buffer.from([130, 172, 122]).toString('base64'), encoding: 'base64', revision: 'r', truncated: false, next_offset: null });
  assert.equal((await readServerText(textClient, 'a', 'text')).content, 'a€z');
  await assert.rejects(readServerText(createIdeClient(async () => ({ content: 'AP8=', encoding: 'base64', revision: 'r', truncated: false, next_offset: null })), 'a', 'binary'), /Binary/);
  let read = 0;
  await assert.rejects(readServerText(createIdeClient(async () => ({ content: 'x', encoding: 'utf-8', revision: String(read++), truncated: true, next_offset: read })), 'a', 'changing'), /changed during reading/);
  console.log('PASS: enumeration, coverage, >50 records, punctuation/non-Latin, flat/graph parity, fallback, expiry and byte decoding');
}

async function mountedContracts() {
  const state = { owner: 'orchid-7', deferredFile: null, deferFile: false, deferredMemory: null, deferMemory: false, ops: operations };
  const calls = [];
  const connection = () => ({ connection_configured: true, ...Object.fromEntries(Object.entries({ mode: 'server', lane: '', user: 'owner', host: state.owner, broker: '/broker.py' }).map(([k, value]) => [k, { value, source: 'settings' }])) });
  const hello = () => ({ identity: { sub: state.owner, gateway: 'http://127.0.0.1:7836', faces: [], may_touch: [], may_not_touch: [], expires_at: '2027-01-01' }, server: { boot: 'b', version: 'v' }, gaps: [], operations: state.ops });
  const invoke = async (cmd, args) => {
    calls.push({ cmd, args, owner: state.owner });
    switch (cmd) {
      case 'portal_connection_get': return connection();
      case 'portal_connection_set': state.owner = args.host; return connection();
      case 'portal_hello': return hello();
      case 'portal_reconnect': return { target: state.owner, hello: hello() };
      case 'portal_catalog': return { faces: [], models: [], backends: [], capabilities: { faces: 0, backends: 0, actions: [], warnings: [] } };
      case 'portal_conversations': case 'portal_approvals': return { items: [], excluded: 0 };
      case 'portal_memory_status': return { counts_by_kind: { family_memory: 1 }, complete_corpus: false };
      case 'portal_memory_list': {
        if (state.deferMemory) { state.deferMemory = false; return new Promise((resolve) => { state.deferredMemory = resolve; }); }
        const rows = memories(state.owner);
        return { items: args.cursor ? rows.slice(50) : rows.slice(0, 50), next_cursor: args.cursor ? null : 'opaque', snapshot: state.owner, coverage };
      }
      case 'portal_memory_read': return { source_id: args.source_id, content: 'owner read', offset: 0 };
      case 'portal_memory_search': assert.fail('mounted app must enumerate'); break;
      case 'portal_files_roots': return { items: [{ attachment_id: state.owner, project_id: 'p', label: `${state.owner} project`, read_only: true }] };
      case 'portal_files_list': return args.cursor ? { items: [{ name: 'slow.txt', path: 'slow.txt', dir: false, size: 5 }], next_cursor: null }
        : { items: [{ name: 'hello.txt', path: 'hello.txt', dir: false, size: 5 }], next_cursor: 'directory-page-2' };
      case 'portal_files_read':
        if (state.deferFile) { state.deferFile = false; return new Promise((resolve) => { state.deferredFile = resolve; }); }
        return { content: 'server text', encoding: 'utf-8', revision: 'r', truncated: false, next_offset: null };
      case 'ide_pick_folder': return '/pc';
      case 'ide_list_dir': return [{ name: 'local.txt', path: '/pc/local.txt', dir: false, size: 5 }];
      case 'ide_read_file': return { path: args.path, content: 'local text', binary: false, too_large: false };
      case 'ide_write_file': case 'ide_term_close': case 'ide_term_resize': return null;
      case 'ide_git_status': return { repo: false, branch: '', changes: {}, top: '' };
      case 'ide_term_open': return 'pc-term';
      case 'portal_jobs_attachments': return { items: [], roles: [], caveats: [] };
      case 'portal_jobs_list': return { items: [], total: 0, truncated: false };
      case 'store_load': return null;
      default: throw fail('unknown_op');
    }
  };
  const PortalApp = require('../src/portal/PortalApp.tsx').default;
  const root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(React.StrictMode, null, React.createElement(PortalApp, { invoke, profiles: null, onProfiles() {} }))));
  await click('Memory');
  await until(() => document.querySelectorAll('.mem-row').length === 57, 'all memory pages');
  assert.ok(document.body.textContent.includes('!!!') && document.body.textContent.includes('你好'));
  assert.ok(document.body.textContent.includes('All records in the inspected sources'));
  assert.ok(document.body.textContent.includes('partial corpus'));
  const flat = [...document.querySelectorAll('.mem-row input')].map((n) => n.getAttribute('aria-label')).sort();
  await click('Brain');
  await until(() => !!graphData, 'real MemoryBrain graph');
  assert.equal(graphData.nodes.filter((n) => !n.hub).length, 57);
  assert.deepEqual(graphData.nodes.filter((n) => !n.hub).map((n) => `Pick ${n.label}`).sort(), flat);
  await click('Flat');
  const memoryCalls = calls.filter((c) => c.cmd === 'portal_memory_list').length;
  await click('Workspace');
  await until(() => !!document.querySelector('[aria-label="File source"]'), 'real IdeEditor');
  await click('Open…'); await click('local.txt');
  assert.equal(editors.at(-1).options.readOnly, false);
  await flush(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 's', ctrlKey: true, bubbles: true })));
  assert.equal(calls.filter((c) => c.cmd === 'ide_write_file').length, 1, 'PC save still works');
  assert.equal(calls.filter((c) => c.cmd === 'ide_term_open').length, 1, 'PC PTY unchanged');
  await choose('File source', 'server');
  await until(() => !!document.querySelector('[aria-label="Server project"]'), 'server roots');
  await choose('Server project', 'orchid-7');
  await until(() => !!button('hello.txt') && !!button('slow.txt'), 'all directory pages');
  await click('hello.txt');
  assert.equal(editors.at(-1).options.readOnly, true, 'server Monaco model read-only');
  assert.ok(document.body.textContent.includes('Read-only server file'));
  assert.ok(document.body.textContent.includes('No file open to include'), 'no server file attached to agent');
  await flush(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 's', ctrlKey: true, bubbles: true })));
  assert.equal(calls.filter((c) => c.cmd === 'ide_write_file').length, 1, 'Ctrl+S cannot save server tab');
  assert.ok(document.querySelector('button[title="New terminal in the open PC folder"]').disabled);
  assert.equal(calls.filter((c) => c.cmd === 'ide_term_open').length, 1, 'no server PTY');
  state.deferFile = true; await click('slow.txt');
  await click('Memory');
  assert.equal(calls.filter((c) => c.cmd === 'portal_memory_list').length, memoryCalls, 'same connection cache reused');
  state.deferMemory = true; await click('Refresh');
  assert.ok(!!state.deferredMemory && !!state.deferredFile);
  await click('Settings'); await click('Connection');
  await flush(() => {
    const node = document.getElementById('connection-host');
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(node, 'fern-8');
    node.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await flush(() => document.querySelector('[aria-label="Server connection"] form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  await settle();
  assert.ok(!button('hello.txt (server)'), 'connection save clears server tabs while workspace is hidden');
  assert.ok(modelRows.some((m) => m.content === 'server text' && m.disposed), 'old server model disposed');
  assert.ok(!!button('local.txt'), 'PC tab survives connection change');
  await flush(() => {
    state.deferredMemory({ items: memories('old-late'), next_cursor: 'old-cursor', snapshot: 'old', coverage });
    state.deferredFile({ content: 'late', encoding: 'utf-8', revision: 'r', truncated: true, next_offset: 4 });
  });
  await click('Memory');
  await until(() => document.body.textContent.includes('fern-8 memory 56'), 'new owner memory');
  assert.ok(!document.body.textContent.includes('orchid-7 memory') && !document.body.textContent.includes('old-late memory'));
  assert.ok(!calls.some((c) => c.args?.cursor === 'old-cursor'), 'late memory page cannot continue on new connection');
  assert.ok(!calls.some((c) => c.cmd === 'portal_files_read' && c.args.offset > 0), 'late file chunk cannot continue on new connection');
  await click('Workspace');
  await until(() => !!document.querySelector('[aria-label="Server project"] option[value="fern-8"]'), 'new server sources');
  assert.ok(!document.querySelector('[aria-label="Server project"] option[value="orchid-7"]'));
  assert.ok(!button('slow.txt (server)'));
  // Older installations show an explicit unavailable state and dispatch no file op.
  state.ops = {};
  const fileCalls = calls.filter((c) => c.cmd.startsWith('portal_files_')).length;
  await click('Settings'); await click('Connection'); await click('Reconnect');
  assert.ok(document.body.textContent.includes('not available on this server'));
  assert.equal(calls.filter((c) => c.cmd.startsWith('portal_files_')).length, fileCalls);
  await flush(() => root.unmount());
  console.log('PASS: real flat/3D views, server source paging, read-only tab/no save/PTY/agent context, PC behavior, connection save cache/tab/late-response invalidation, old-server availability');
}

(async () => { await pureContracts(); await mountedContracts(); })().catch((error) => { console.error(error); process.exit(1); });
