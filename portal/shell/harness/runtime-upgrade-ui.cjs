'use strict';
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { RuntimeUpgrade } = require('../src/portal/RuntimeUpgrade.tsx');
let status, calls, root, finish, fail;
const initial = () => ({ installed: { build_id: 'c4' }, bundled: { build_id: 'c5' }, available: true, running: false, upgrade: null });
async function invoke(command, args) {
  calls.push({ command, args });
  if (command === 'runtime_upgrade_status') return { ...status };
  assert.equal(command, 'runtime_upgrade_start'); assert.equal(args, undefined);
  if (fail) throw 'Runtime archive digest mismatch';
  return new Promise(resolve => { finish = resolve; });
}
async function flush(fn) { await act(async () => { if (fn) await fn(); await Promise.resolve(); }); }
function button(text) { return [...document.querySelectorAll('button')].find(b => b.textContent === text); }
async function click(text) { assert.ok(button(text)); await flush(() => button(text).click()); }
async function render(props = {}) { await flush(() => root.render(React.createElement(RuntimeUpgrade, { invoke, scope: 'local', ...props }))); }
async function mount(props) { calls = []; root = createRoot(document.getElementById('root')); await render(props); }
const text = () => document.body.textContent;
const card = () => document.querySelector('section');
async function unmount() { await flush(() => root.unmount()); }
(async () => {
  status = initial(); await mount();
  assert.match(document.body.textContent, /BoB update available/);
  assert.equal(calls.filter(c => c.command === 'runtime_upgrade_start').length, 0, 'launch is read-only');
  await click('Upgrade'); assert.ok(button('Upgrade').disabled);
  assert.match(document.body.textContent, /Copying update/);
  assert.match(document.body.textContent, /Keep this window open/);
  await click('Upgrade'); assert.equal(calls.filter(c => c.command === 'runtime_upgrade_start').length, 1);
  status = { ...status, running: true, upgrade: { id: 'c4-c5', phase: 'backup', outcome: 'running' } };
  await flush(() => finish(status));
  assert.match(document.body.textContent, /Backing up runtime/);
  assert.doesNotMatch(document.body.textContent, /Copying update/);
  assert.ok(document.querySelector('progress'));
  await unmount();
  await mount(); assert.ok(button('Upgrade').disabled, 'reconnect resumes status without resubmission');
  assert.equal(calls.filter(c => c.command === 'runtime_upgrade_start').length, 0);
  status = { ...status, upgrade: { ...status.upgrade, phase: 'migrations' } };
  await click('Refresh update status'); assert.match(document.body.textContent, /Updating the database/);
  status = { ...status, available: false, running: false, installed: { build_id: 'c5' }, upgrade: { ...status.upgrade, phase: 'done', outcome: 'completed' } };
  await click('Refresh update status'); assert.match(document.body.textContent, /updated successfully/);
  assert.equal(button('Upgrade'), undefined); await unmount();
  status = { ...initial(), upgrade: { id: 'c4-c5', phase: 'rolled_back', outcome: 'rolled_back' } };
  await mount(); assert.match(document.querySelector('[role="alert"]').textContent, /previous runtime was restored/);
  fail = true; await click('Upgrade'); assert.match(document.body.textContent, /digest mismatch/);
  assert.equal(button('Upgrade').disabled, false); await unmount(); fail = false;
  status = { ...initial(), upgrade: { id: 'c4-c5', phase: 'rollback', outcome: 'rollback_failed' } };
  await mount(); assert.match(document.body.textContent, /bob-runtime upgrade --rollback c4-c5/); await unmount();
  status = { ...initial(), available: false }; await mount(); assert.equal(document.querySelector('section'), null); await unmount();
  status = { ...initial(), available: false, installed: { build_id: 'c5' }, upgrade: { id: 'c4-c5', phase: 'publish', outcome: 'running' } };
  await mount(); assert.equal(button('Upgrade').disabled, false, 'an interrupted publication resumes even when version already changed');
  await click('Upgrade');
  status = { ...status, upgrade: { ...status.upgrade, phase: 'done', outcome: 'completed' } };
  await flush(() => finish(status)); assert.match(document.body.textContent, /updated successfully/); await unmount();

  // WSF1 F2: `upgrade` is the runtime's LAST journal entry. A completed entry from an earlier
  // app session (every fresh mount here is one) is not news and must not keep a card on screen.
  const done = { id: 'c4-c5', phase: 'done', outcome: 'completed' };
  status = { ...initial(), available: false, installed: { build_id: 'c5' }, upgrade: done };
  await mount(); assert.equal(card(), null, 'a completed journal from an earlier session renders nothing');
  await flush(() => window.dispatchEvent(new Event('focus'))); assert.equal(card(), null, 'a later status read keeps it hidden');
  await unmount();
  // A newer bundle next to that stale journal: the offer only, never the old phase or success text.
  status = { ...initial(), installed: { build_id: 'c5' }, bundled: { build_id: 'c6' }, upgrade: done };
  await mount(); assert.match(text(), /BoB update available/); assert.ok(button('Upgrade'));
  assert.doesNotMatch(text(), /Update complete|updated successfully|Reconnect to continue/);
  assert.equal(button('Reconnect'), undefined);
  // Pressing Upgrade before the new job has written its own journal still shows no stale success.
  await click('Upgrade'); status = { ...status, running: true }; await flush(() => finish(status));
  assert.match(text(), /Update in progress/); assert.doesNotMatch(text(), /Update complete|updated successfully/);
  status = { ...status, upgrade: { id: 'c5-c6', phase: 'swap', outcome: 'running' } };
  await click('Refresh update status'); assert.match(text(), /Installing runtime files/);
  status = { ...status, available: false, running: false, installed: { build_id: 'c6' }, upgrade: { id: 'c5-c6', phase: 'done', outcome: 'completed' } };
  await click('Refresh update status'); assert.match(text(), /updated successfully/, 'the upgrade this session started reports its own success');
  await unmount();

  // WSF1 F1+F2: an upgrade this session watched running offers Reconnect on completion, keeps the
  // note while reconnecting fails, and retires it after the next successful reconnect.
  let reconnects = 0; const props = { onReconnect: () => { reconnects += 1; }, connected: 1 };
  status = { ...initial(), running: true, upgrade: { id: 'c4-c5', phase: 'health', outcome: 'running' } };
  await mount(props); assert.match(text(), /Checking BoB health/); assert.equal(button('Reconnect'), undefined, 'no Reconnect while the update runs');
  status = { ...status, available: false, running: false, installed: { build_id: 'c5' }, upgrade: done };
  await click('Refresh update status');
  assert.match(text(), /Update complete/); assert.match(text(), /updated successfully\. Reconnect to continue\./);
  assert.ok(button('Reconnect'), 'the completed note carries a Reconnect button');
  await click('Reconnect'); assert.equal(reconnects, 1, 'Reconnect runs the supplied path once, only on a press');
  assert.equal(calls.filter(c => c.command === 'runtime_upgrade_start').length, 0, 'Reconnect never restarts an upgrade');
  await render({ ...props, reconnecting: true }); assert.ok(button('Reconnect').disabled, 'disabled while a reconnect is in flight');
  // The identity scope resets on every reconnect attempt; a failed one leaves `connected` unchanged.
  await render({ ...props, scope: 'no-identity' });
  assert.match(text(), /updated successfully/, 'a failed reconnect keeps the note'); assert.ok(button('Reconnect'));
  await render({ ...props, scope: 'identity-after-reconnect', connected: 2 });
  assert.equal(card(), null, 'a successful reconnect retires the completed note');
  await unmount();
  await mount(props); assert.equal(card(), null, 'and it stays gone after an app restart'); await unmount();
  // Without an onReconnect handler the note is unchanged from before (text only).
  status = { ...initial(), running: true, upgrade: { id: 'c4-c5', phase: 'health', outcome: 'running' } }; await mount();
  status = { ...status, available: false, running: false, installed: { build_id: 'c5' }, upgrade: done };
  await click('Refresh update status'); assert.match(text(), /updated successfully/); assert.equal(button('Reconnect'), undefined); await unmount();

  // Unfinished outcomes are not session news: each still shows after an app restart (fresh mount).
  for (const [outcome, phase, expected, available] of [
    ['rolled_back', 'rolled_back', /previous runtime was restored/, false], ['failed', 'preflight', /could not be prepared/, true],
    ['rollback_failed', 'rollback', /bob-runtime upgrade --rollback c4-c5/, false], ['rolling_back', 'rollback', /interrupted/, false],
    ['running', 'swap', /interrupted/, false]]) {
    status = { ...initial(), available, upgrade: { id: 'c4-c5', phase, outcome } };
    await mount(); assert.ok(card(), `${outcome} survives a restart`); assert.match(text(), expected); await unmount();
  }
  console.log('runtime upgrade UI: explicit click, progress, duplicate prevention, reconnect, success, rollback, errors, no newer release, stale completion hidden, Reconnect on watched completion, unfinished outcomes kept PASS');
})().catch(error => { console.error(error); process.exit(1); });
