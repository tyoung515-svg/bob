'use strict';
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const key of ['window','document','HTMLElement','Event','MouseEvent']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { RuntimeUpgrade, upgradeReason } = require('../src/portal/RuntimeUpgrade.tsx');
const { SettingsView, DEFAULT_APPEARANCE, ServerConnection, ServerSshHint, serverSshHint } = require('../src/portal/SettingsView.tsx');
const payload = (user = '', host = '', broker = '') => ({
  mode: { value: 'server', source: 'default' }, lane: { value: '', source: 'default' },
  user: { value: user, source: 'settings' }, host: { value: host, source: 'settings' }, broker: { value: broker, source: 'settings' },
});
const initial = () => ({ installed: { build_id: 'legacy' }, bundled: { build_id: 'new' }, available: true, running: false, upgrade: null });
let root, calls = [];
const flush = async (fn = () => {}) => act(async () => { await fn(); await Promise.resolve(); });
async function mount(component) { root = createRoot(document.getElementById('root')); await flush(() => root.render(component)); }
async function unmount() { await flush(() => root.unmount()); }
const text = () => document.body.textContent;
const button = label => [...document.querySelectorAll('button')].find(node => node.textContent === label);
async function input(id, value) { await flush(() => {
  const node = document.getElementById(id);
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(node, value);
  node.dispatchEvent(new Event('input', { bubbles: true }));
}); }
(async () => {
  assert.equal(serverSshHint(null), null);
  assert.equal(serverSshHint(payload()), null);
  assert.equal(serverSshHint(payload('bob', 'example-server')), 'ssh bob@example-server');
  assert.equal(serverSshHint(payload('operator', '192.0.2.4')), 'ssh operator@192.0.2.4');
  assert.equal(serverSshHint({ ...payload('bob', 'example-server'), mode: { value: 'this_pc' } }), null);
  await mount(React.createElement(ServerSshHint, { connection: payload('operator', '192.0.2.4') }));
  assert.equal(document.querySelector('code').textContent, 'ssh operator@192.0.2.4');
  await unmount();
  await mount(React.createElement(ServerSshHint, { connection: payload() }));
  assert.match(text(), /Set the SSH user and host/); assert.equal(document.querySelector('code'), null); await unmount();
  await mount(React.createElement(SettingsView, {
    invoke: async command => command === 'portal_connection_get' ? payload('operator', '192.0.2.4') : null,
    client: {}, hello: { identity: { sub: 'owner', token_type: 'portal', owner_controls_eligible: true } }, catalog: null,
    profiles: null, onProfiles: () => {}, onReconnect: () => {}, appearance: DEFAULT_APPEARANCE, onAppearance: () => {}, pendingApprovals: 0,
  }));
  await flush(() => button('Account & sign-in').click());
  assert.equal(document.querySelector('.set-steps code').textContent, 'ssh operator@192.0.2.4', 'Account loads the saved connection before Connection is opened');
  await unmount();
  let saved;
  const connectionInvoke = async (command, args) => {
    calls.push({ command, args });
    if (command === 'portal_connection_get') return payload();
    assert.equal(command, 'portal_connection_set'); saved = args; return payload(args.user, args.host, args.broker);
  };
  await mount(React.createElement(ServerConnection, { invoke: connectionInvoke, serverOnly: true }));
  for (const field of ['user','host','broker']) assert.equal(document.getElementById('connection-' + field).value, '');
  await flush(() => document.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  assert.match(text(), /missing SSH user, server host, broker path/); assert.equal(saved, undefined);
  await input('connection-user', 'bob'); await input('connection-host', 'example-server'); await input('connection-broker', '/opt/bob/portal-broker.py');
  await flush(() => document.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  assert.deepEqual(saved, { mode: 'server', user: 'bob', host: 'example-server', broker: '/opt/bob/portal-broker.py' });
  await unmount();
  let status = { ...initial(), blocked: 'runtime_too_old' };
  const invoke = async command => { calls.push({ command }); assert.equal(command, 'runtime_upgrade_status'); return status; };
  await mount(React.createElement(RuntimeUpgrade, { invoke, scope: 'test' }));
  assert.match(text(), /too old to update in place/); assert.match(text(), /Nothing was changed/);
  assert.match(text(), /back up your data, remove the old runtime and set up BoB again/);
  assert.equal(button('Upgrade'), undefined); assert.doesNotMatch(text(), /credentials are retained/); await unmount();
  for (const [reason, expected] of [
    ['runtime_too_old', /too old to update in place/], ['runtime_unhealthy', /health check did not pass/],
    ['insufficient_space', /not enough free space/], ['unsupported_layout', /layout.*does not support/],
  ]) {
    assert.match(upgradeReason(reason), expected);
    for (const outcome of ['rolled_back', 'failed']) {
      status = { ...initial(), upgrade: { id: 'job-1', phase: outcome === 'failed' ? 'preflight' : 'rolled_back', outcome, reason } };
      await mount(React.createElement(RuntimeUpgrade, { invoke, scope: 'test' }));
      assert.match(text(), expected); assert.doesNotMatch(text(), /The update failed./);
      if (outcome === 'rolled_back') assert.match(text(), /previous runtime was restored/);
      await unmount();
    }
  }
  for (const reason of [undefined, 'unknown-detail', 'constructor', '__proto__']) {
    assert.equal(upgradeReason(reason), null);
    status = { ...initial(), upgrade: { id: 'job-1', phase: 'rolled_back', outcome: 'rolled_back', reason } };
    await mount(React.createElement(RuntimeUpgrade, { invoke, scope: 'test' }));
    assert.match(text(), /The update failed. The previous runtime was restored./);
    assert.doesNotMatch(text(), /unknown-detail|constructor|__proto__/); await unmount();
  }
  assert.equal(calls.filter(call => call.command === 'runtime_upgrade_start').length, 0);
  console.log('AU11 UI: empty server setup, missing details, saved SSH hint, blocked upgrade and closed refusal reasons PASS');
})().catch(error => { console.error(error); process.exit(1); });
