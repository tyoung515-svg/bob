'use strict';
// Drives real screens and the native/catalog boundary with offline lane fixtures.
const assert = require('node:assert/strict'), path = require('node:path');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'KeyboardEvent']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const eventModule = require.resolve('@tauri-apps/api/event', { paths: [path.resolve(__dirname, '..')] });
require.cache[eventModule] = { id: eventModule, filename: eventModule, loaded: true, children: [], paths: [], exports: { listen: async () => () => {} } };
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const { SettingsView, DEFAULT_APPEARANCE } = require('../src/portal/SettingsView.tsx');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { pickFromRow, sendPinArgs } = require('../src/portal/portal-state.ts');
const providers = require('./model-provider-fixture.cjs');
const conversation = { id: 'c1', title: 'Fixture chat', face_id: null, model: null, backend: null, project_id: null };
const hello = { identity: { sub: 'fixture', gateway: 'http://127.0.0.1:1', faces: [], may_touch: [], may_not_touch: [], expires_at: '2027-01-01' }, server: {}, gaps: [] };
const empty = () => ({ faces: [], models: [], backends: [], capabilities: { faces: 0, backends: 0, actions: [], warnings: [] } });
function catalog(rows) {
  return { ...empty(), faces: [{ id: 'assistant', name: 'Assistant', allowed: true }],
    models: rows.map((row) => ({ ...row, backend: row.id, reason: row.unavailable_reason ?? null })),
    backends: rows.map((row) => ({ ...row, kind: 'http', unavailable_reason: row.unavailable_reason ?? null })) };
}
const signedIn = () => catalog([
  { id: 'openai_codex', label: 'GPT (ChatGPT sign-in)', model: null, available: true },
  { id: 'codex_code', label: 'Codex configured default', model: null, available: false, unavailable_reason: 'Codex CLI not installed' },
]);
const keysLane = () => catalog([
  { id: 'claude_api', label: 'Anthropic', model: 'lane-claude', available: true },
  { id: 'deepseek_v4_flash', label: 'DeepSeek', model: 'lane-deepseek', available: false, unavailable_reason: 'Key expired' },
]);
let current, status, calls, saveResolve, providerRows, reloadFailure;
function reset() {
  current = empty(); calls = []; providerRows = providers; reloadFailure = false;
  status = { lane: 'fixture', providers: {}, default_backend: null, ready: false };
}
async function invoke(cmd, args) {
  calls.push({ cmd, args });
  switch (cmd) {
    case 'portal_connection_get': return { connection_configured: true };
    case 'portal_hello': return hello;
    case 'portal_catalog': if (reloadFailure) throw 'catalog offline'; return current;
    case 'portal_conversations': return { items: [conversation], limit: 50, offset: 0, excluded: 0 };
    case 'portal_conversation_open': return { conversation, messages: [], has_more: false };
    case 'portal_pins': return { face_id: null, backend: null, model: null };
    case 'runtime_model_providers': return providerRows;
    case 'runtime_model_status': return { ...status };
    case 'runtime_model_openai_login':
      current = signedIn(); status = { ...status, providers: { openai_signin: true }, default_backend: 'openai_codex', ready: true }; return { ok: true };
    case 'runtime_model_set_key':
      return new Promise((resolve) => { saveResolve = () => { current = keysLane(); status = { ...status, providers: { anthropic_key: true, deepseek_key: true }, default_backend: 'claude_api', ready: false }; resolve({ ok: true }); }; });
    case 'runtime_model_set_default': status = { ...status, default_backend: args.backend }; return { ok: true };
    case 'local_auth_status': return [];
    default: throw `Unexpected fixture command ${cmd}`;
  }
}
async function flush(fn) { await act(async () => { if (fn) await fn(); await Promise.resolve(); }); }
async function mount(Component, props) {
  const root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(Component, props))); await flush(); return root;
}
const text = () => document.body.textContent;
function button(label) { return [...document.querySelectorAll('button')].find((row) => row.textContent === label); }
async function click(label) { assert.ok(button(label), `button ${label} exists`); await flush(() => button(label).click()); }
async function select(id, value) { await flush(() => { const node = document.getElementById(id); node.value = value; node.dispatchEvent(new Event('change', { bubbles: true })); }); }
async function input(id, value) { await flush(() => { const node = document.getElementById(id); Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(node, value); node.dispatchEvent(new Event('input', { bubbles: true })); }); }
const countCatalog = () => calls.filter((row) => row.cmd === 'portal_catalog').length;
const settingsProps = () => ({ invoke, client: createPortalClient(invoke), catalog: current, hello, profiles: null, onProfiles: () => {}, onReconnect: () => {}, appearance: DEFAULT_APPEARANCE, onAppearance: () => {}, pendingApprovals: 0, modelSetupRequest: 1 });
(async () => {
  reset(); let root = await mount(PortalApp, { invoke, profiles: null, onProfiles: () => {} });
  assert.equal(document.querySelectorAll('#portal-model option').length, 1, 'empty lane invents no model rows');
  assert.match(text(), /No models set up yet/); await click('Add a provider');
  assert.ok(document.querySelector('[aria-label="Connect a model"]'));
  assert.match(text(), /No models set up yet/); await click('Add a provider');
  assert.equal(document.activeElement.textContent, 'Sign in with OpenAI (ChatGPT)');
  assert.equal(document.getElementById('model-default'), null);
  let before = countCatalog(); await click('Sign in with OpenAI (ChatGPT)');
  assert.equal(countCatalog(), before + 1, 'sign-in reloads parent catalog exactly once');
  assert.equal(document.querySelectorAll('#model-default option:not([disabled])').length, 1);
  assert.ok([...document.querySelectorAll('#model-default option')].find(row => row.value === 'codex_code').disabled);
  assert.equal(document.getElementById('model-default').value, 'openai_codex');
  assert.match(text(), /GPT \(ChatGPT sign-in\), chat with Bob's tools/); assert.match(text(), /Codex \(coding agent\)/);
  assert.match(text(), /Codex default/); assert.match(text(), /Codex CLI not installed/);
  assert.ok([...document.querySelectorAll('#portal-model option')].filter((row) => /Codex configured default/.test(row.textContent)).every((row) => row.disabled));
  assert.equal(pickFromRow(current.models[0]), null); assert.deepEqual(sendPinArgs(pickFromRow(current.models[0])), {});
  before = countCatalog(); await select('model-default', 'codex_code');
  assert.equal(calls.filter((row) => row.cmd === 'runtime_model_set_default').length, 0, 'unavailable defaults cannot be selected, including a forged DOM change');
  await select('model-default', 'openai_codex');
  assert.equal(calls.filter((row) => row.cmd === 'runtime_model_set_default').at(-1).args.backend, 'openai_codex');
  assert.equal(countCatalog(), before + 1, 'default change reloads parent catalog exactly once');
  assert.equal(document.getElementById('model-default').value, 'openai_codex');
  await click('Use an API key');
  assert.deepEqual([...document.querySelectorAll('#model-provider option')].map((row) => row.value), ['openai', 'anthropic', 'deepseek', 'google', 'glm', 'kimi', 'qwen']);
  await select('model-provider', 'anthropic'); const secret = 'fake-secret-NEVER-RENDER';
  await input('model-key', secret); before = countCatalog();
  await flush(() => document.querySelector('.model-setup form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  assert.equal(document.getElementById('model-key').value, ''); assert.ok(!document.body.innerHTML.includes(secret));
  await flush(() => saveResolve());
  assert.equal(countCatalog(), before + 1, 'key save reloads parent catalog exactly once');
  assert.ok(!document.body.innerHTML.includes(secret)); assert.match(text(), /Anthropic saved/);
  assert.equal(document.getElementById('model-default').value, 'claude_api');
  assert.deepEqual([...document.querySelectorAll('#model-default option:not([disabled])')].map((row) => row.value), ['claude_api']);
  assert.ok([...document.querySelectorAll('#model-default option')].find(row => row.value === 'deepseek_v4_flash').disabled);
  assert.match(text(), /lane-claude/); assert.match(text(), /lane-deepseek/); assert.match(text(), /Key expired/);
  assert.ok(!/A model is ready/.test(text()), 'saved credentials do not imply working');
  assert.match(text(), /Chat needs a model/);
  // A failure to reload preserves the last good picker/default choices.
  reloadFailure = true; await select('model-default', 'claude_api');
  assert.equal(document.getElementById('model-default').value, 'claude_api'); assert.ok(button('Try loading models again'));
  assert.ok([...document.querySelectorAll('#portal-model option')].some(row => row.textContent.includes('Anthropic')));
  await flush(() => root.unmount());

  // Enrollment uses native metadata. Server key storage has its own owner gate
  // and discovery contract; local adapters must never populate that form.
  reset(); providerRows = [{ id: 'deepseek', label: 'Native fixture label', method: 'key' }];
  root = await mount(SettingsView, settingsProps()); await click('Use an API key');
  assert.deepEqual([...document.querySelectorAll('#model-provider option')].map((row) => row.textContent), ['Native fixture label']);
  assert.ok(!button('Sign in with OpenAI (ChatGPT)'));
  await click('Keys & sign-ins');
  assert.equal(document.querySelector('select[aria-label="Provider"]'), null);
  assert.equal(document.querySelector('.key-entry'), null, 'unverified identity has no server key controls');
  assert.ok(!text().includes('Native fixture label')); await flush(() => root.unmount());

  // Existing deployment rows and exact model pins stay intact.
  reset(); current = catalog([{ id: 'deployment_backend', label: 'Deployment label', model: 'deployment-pin', available: true }]);
  root = await mount(PortalApp, { invoke, profiles: null, onProfiles: () => {} });
  const option = [...document.querySelectorAll('#portal-model option')].find((row) => row.textContent.includes('Deployment label'));
  assert.ok(option && !option.disabled); await flush(() => document.querySelector('.conversation-list button').click()); await select('portal-model', option.value);
  assert.equal(document.getElementById('portal-model-choice').value, 'deployment-pin');
  assert.ok(document.getElementById('portal-model-choice').disabled, 'one exact legacy model');
  assert.deepEqual(sendPinArgs(pickFromRow(current.models[0])), { backend: 'deployment_backend', model: 'deployment-pin' });
  await flush(() => root.unmount());
  console.log('portal-configured-models-ui: empty/Add provider, sign-in two/null rows/default/backend selection, key/default catalog reload, two-provider reasons, no key rendering, runtime metadata, stale catalog and deployment pins PASS');
})().catch((error) => { console.error(error); process.exit(1); });
