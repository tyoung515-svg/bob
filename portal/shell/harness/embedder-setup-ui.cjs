'use strict';
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { EmbedderSetup } = require('../src/portal/EmbedderSetup.tsx');
const { EmbedderWarning } = require('../src/portal/EmbedderWarning.tsx');
let calls, status, fail, complete, continued, root;
const fresh = (ram = 8) => ({ selected_model: '4b', ready: false, file_present: false, sha_ok: false, service_active: false,
  embedding_dimension: 2560, ram_gb: ram, recommended_model: '0.6b', configured: false, installed_models: [],
  model_loaded: false, alias: 'qwen3-embedding-4b', setup: null });
const installed = (model, extra = {}) => ({ ...fresh(), selected_model: model, embedding_dimension: model === '4b' ? 2560 : 1024,
  alias: model === '4b' ? 'qwen3-embedding-4b' : 'qwen3-embedding-0.6b', recommended_model: model, configured: true,
  installed_models: [model], file_present: true, sha_ok: true, ...extra });
let statusFails = false;
async function invoke(cmd, args) {
  calls.push({ cmd, args });
  if (cmd === 'runtime_embedder_status') { if (statusFails) throw 'Packaged embedder setup is available for Bob on this PC, not for a remote server connection'; return { ...status }; }
  assert.equal(cmd, 'runtime_embedder_install');
  if (fail) throw 'Cannot switch embedder: existing memory vectors require re-embedding.';
  return new Promise(resolve => { complete = resolve; });
}
async function flush(fn) { await act(async () => { if (fn) await fn(); await Promise.resolve(); }); }
function button(text) { return [...document.querySelectorAll('button')].find(b => b.textContent === text); }
async function click(text) { assert.ok(button(text), text); await flush(() => button(text).click()); }
async function mount(ram = 8) {
  calls = []; status = fresh(ram); fail = false; continued = [];
  root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(EmbedderSetup, { invoke, onContinue: skipped => continued.push(skipped) })));
}
async function unmount() { await flush(() => root.unmount()); }
(async () => {
  // Fresh install: explicit, honestly labelled choices; 0.6B is the starting recommendation, never 4B by default.
  await mount();
  assert.equal(document.querySelector('input:checked').value, '0.6b');
  const text = () => document.body.textContent;
  assert.match(text(), /0\.6B — Lightweight/); assert.match(text(), /4B — Quality-focused/);
  assert.match(text(), /Recommended starting option for most PCs/);
  assert.match(text(), /1024-dimension vectors/); assert.match(text(), /2560-dimension vectors/);
  assert.doesNotMatch(text(), /12 GB|single-user|multi-user|Suggested:/, 'no unsupported threshold promises');
  assert.doesNotMatch(text(), /tested|guarantee/i, 'no promised BoB recall quality');
  assert.match(text(), /keyword-only/); assert.match(text(), /re-embedding/);
  assert.equal(calls.filter(c => c.cmd === 'runtime_embedder_install').length, 0, 'rendering never installs');
  await click('Skip for now'); assert.deepEqual(continued, [true]);
  assert.match(text(), /Skipped for now/); assert.match(text(), /Settings → Models/);
  await flush(() => document.querySelector('input[value="4b"]').click());
  assert.ok(button('Install embedder'), 'a fresh 4B pick is an install, not a switch');
  await click('Install embedder');
  assert.deepEqual(calls.at(-1), { cmd: 'runtime_embedder_install', args: { model: '4b' } });
  assert.ok(button('Skip for now').disabled);
  assert.ok(document.querySelector('progress[aria-label="Embedder installation progress"]'));
  status = installed('4b', { ready: true, service_active: true, model_loaded: true, setup: { model: '4b', phase: 'ready', detail: 'Embedder ready.' } });
  await flush(() => complete({ ...status }));
  assert.equal(document.querySelector('input:checked').value, '4b', 'the recommendation never replaces the user choice');
  assert.match(text(), /Embedder ready · 4B · qwen3-embedding-4b · 2560-dimension vectors/);
  await click('Continue'); assert.deepEqual(continued, [true, false]);
  await unmount();

  // Existing 4B install: shown as installed and selected; a stale 0.6B recommendation never flips it.
  await mount(); status = installed('4b', { recommended_model: '0.6b', ready: true, model_loaded: true, service_active: true });
  await click('Refresh embedder status');
  assert.equal(document.querySelector('input:checked').value, '4b');
  assert.match(text(), /Installed/); assert.doesNotMatch(text(), /Recommended starting option/);
  assert.equal(calls.filter(c => c.cmd === 'runtime_embedder_install').length, 0, 'no download from a stale render');
  await flush(() => document.querySelector('input[value="0.6b"]').click());
  assert.ok(!button('Install embedder')); assert.ok(button('Switch to 0.6B'));
  assert.match(text(), /existing memories .* re-embedding|re-embedding .* existing memories/i);
  fail = true; await click('Switch to 0.6B');
  assert.match(document.querySelector('[role="alert"]').textContent, /re-embedding/);
  assert.equal(button('Switch to 0.6B').disabled, false);
  await unmount();

  // SETUP-SOL-REV-002: missing weights retain configured and legacy choices after reopen.
  for (const configured of [true, undefined, null]) {
    status = installed('4b', { file_present: false, sha_ok: false, installed_models: [], recommended_model: '0.6b', configured });
    calls = []; root = createRoot(document.getElementById('root'));
    await flush(() => root.render(React.createElement(EmbedderSetup, { invoke })));
    assert.equal(document.querySelector('input:checked')?.value, '4b', 'missing configured/legacy 4B stays selected');
    assert.match(text(), /4B.*weights.*missing/i);
    assert.doesNotMatch(text(), /Recommended starting option/);
    assert.equal(calls.some(c => c.cmd === 'runtime_embedder_install'), false);
    await flush(() => document.querySelector('input[value="0.6b"]').click());
    assert.equal(!!button('Switch to 0.6B'), true);
    assert.match(text(), /Switching from 4B to 0\.6B.*re-embedding/);
    await unmount();
  }

  // Downloaded+verified is not loaded, and loaded is not memory-accepted: each state says only what is known.
  await mount(); status = installed('0.6b', { service_active: true, model_loaded: false, ready: false });
  await click('Refresh embedder status');
  assert.match(text(), /downloaded and verified, but not loaded/i); assert.match(text(), /keyword-only/);
  status = installed('0.6b', { service_active: true, model_loaded: true, ready: true });
  await click('Refresh embedder status');
  assert.match(text(), /Embedder ready · 0\.6B · qwen3-embedding-0\.6b · 1024-dimension vectors/);
  await unmount();

  // Unavailable status (remote connection / runtime down): no preselected model, no install, honest reason.
  statusFails = true; await mount();
  assert.equal(!!document.querySelector('input:checked'), false);
  assert.match(document.querySelector('[role="alert"]').textContent, /not for a remote server/);
  assert.ok(button('Install embedder').disabled);
  await unmount(); statusFails = false;

  // A durable in-flight install is polled after remount; no repeated mutation.
  status = { ...fresh(), setup: { model: '0.6b', phase: 'downloading', detail: 'Downloading…' } };
  calls = []; root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(EmbedderSetup, { invoke })));
  assert.ok(button('Install embedder').disabled);
  assert.match(document.body.textContent, /Downloading/);
  await unmount();
  assert.equal(calls.filter(c => c.cmd === 'runtime_embedder_install').length, 0);
  let opened = false;
  status = fresh(); root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(EmbedderWarning, { invoke, scope: 'local', onSetup: () => { opened = true; } })));
  assert.match(document.body.textContent, /keyword-only/);
  await click('Set up the embedder'); assert.ok(opened);
  status = { ...status, ready: true };
  await flush(() => window.dispatchEvent(new Event('bob:embedder-status')));
  assert.equal(!!document.querySelector('[role="status"]'), false);
  await unmount();
  console.log('embedder setup UI: explicit 0.6B/4B choice, fresh-only recommendation, installed model preserved, switch guard, verified/loaded/ready distinct, unavailable honesty, skip, progress, remount, warning PASS');
})().catch(error => { console.error(error); process.exit(1); });
