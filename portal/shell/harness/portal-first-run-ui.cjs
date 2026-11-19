'use strict';
// Offline fixtures drive the real PortalApp and native command boundary.
const assert = require('node:assert/strict'), crypto = require('node:crypto'), path = require('node:path');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'KeyboardEvent']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = new Map();
const eventModule = require.resolve('@tauri-apps/api/event', { paths: [path.resolve(__dirname, '..')] });
require.cache[eventModule] = { id: eventModule, filename: eventModule, loaded: true, children: [], paths: [],
  exports: { listen: async (topic, callback) => { listeners.set(topic, callback); return () => listeners.delete(topic); } } };
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const PortalApp = require('../src/portal/PortalApp.tsx').default, { qrMatrix } = require('../src/portal/qr.ts');
const URI = 'otpauth://totp/Bob:dad?secret=JBSWY3DPEHPK3PXP&issuer=Bob';
const HELLO = { identity: { sub: 'dad', gateway: 'http://127.0.0.1:7846', faces: [], may_touch: [], may_not_touch: [], expires_at: '2027-01-01' }, server: {}, gaps: [] };
const connection = (configured) => ({ connection_configured: configured,
  mode: { value: 'server', source: 'default' }, lane: { value: '', source: 'default' },
  user: { value: 'operator', source: 'default' }, host: { value: 'example-server', source: 'default' },
  broker: { value: '/opt/bob/portal/portal-broker.py', source: 'default' } });
let calls, runtime, laneFailure, installFailure, saveFailure, configured, delayLane, completeLane;
function reset() {
  calls = []; runtime = { wsl: 'missing', distro_installed: false, firstboot: false, healthy: false, lanes: [], detail: '' };
  laneFailure = false; installFailure = false; saveFailure = false; configured = false; delayLane = false;
}
async function invoke(cmd, args) {
  calls.push({ cmd, args });
  switch (cmd) {
    case 'runtime_model_providers': return require('./model-provider-fixture.cjs');
    case 'portal_connection_get': return connection(configured);
    case 'portal_connection_set':
      if (saveFailure) throw JSON.stringify({ code: 'bad_request', detail: 'Invalid server host' });
      configured = true; return connection(true);
    case 'runtime_model_status': return { lane: 'dad', providers: {}, default_backend: null, ready: false };
    case 'runtime_embedder_status': return { selected_model: '4b', file_present: false, ready: false, sha_ok: false,
      service_active: false, embedding_dimension: 2560, recommended_model: '0.6b', ram_gb: 8, setup: null };
    case 'runtime_status': return { ...runtime };
    case 'runtime_install':
      if (installFailure) throw 'First boot timed out; runtime preserved';
      listeners.get('runtime://progress')({ payload: { step: 'import', detail: 'Installing Bob on this PC' } });
      runtime = { ...runtime, distro_installed: true, firstboot: true, healthy: true }; return runtime;
    case 'runtime_add_lane':
      if (laneFailure) throw 'Lane creation refused';
      if (delayLane) await new Promise((resolve) => { completeLane = resolve; });
      configured = true; return { otpauth: URI, core: 7845, gateway: 7846 };
    case 'portal_hello': return HELLO;
    case 'portal_catalog': return { faces: [], models: [], backends: [], capabilities: { faces: 0, backends: 0, actions: [], warnings: [] } };
    case 'portal_conversations': return { items: [], offset: 0, limit: 50, excluded: 0 };
    case 'portal_approvals': return { items: [] };
    default: throw `unexpected command ${cmd}`;
  }
}
async function flush(fn) { await act(async () => { if (fn) await fn(); await Promise.resolve(); }); }
async function mount() {
  const root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(PortalApp, { invoke, profiles: null, onProfiles: () => {} })));
  await flush(); return root;
}
function button(text) { return [...document.querySelectorAll('button')].find((b) => b.textContent === text); }
async function click(text) { assert.ok(button(text), `button ${text} exists`); await flush(() => button(text).click()); }
async function input(id, value) {
  await flush(() => { const node = document.getElementById(id);
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(node, value);
    node.dispatchEvent(new Event('input', { bubbles: true })); });
}
async function submit() { await flush(() => document.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))); }
const noHello = () => assert.equal(calls.some((c) => c.cmd === 'portal_hello'), false, 'no connection before setup completes');
(async () => {
  // Independent qrcode-terminal QR Model 2 reference, ECC L, explicit v10/v15, mask 0.
  for (const [text, digest] of [[URI, '92ae6071454973ac416aa6d7895a5e6bb862b76e152d7d890650809b9c249757'],
    ['x'.repeat(271), 'bdc73cf2798dde47b703c10736ecd7b57a150da0bdab5f473e2430b15b27f03c'],
    ['y'.repeat(272), 'e9eaddc6e7ca40cb64b926b5fddca83299071900496e121e0636168615f0924e'],
    ['z'.repeat(520), 'ef94324bf15dc36d4a643515d716456c47a7f11616a441f57c624165e86a9824']]) {
    assert.equal(crypto.createHash('sha256').update(JSON.stringify(qrMatrix(text))).digest('hex'), digest);
  }
  assert.throws(() => qrMatrix('x'.repeat(521)), /too long/);
  reset(); let root = await mount();
  assert.ok(document.querySelector('main.portal.first-run'), 'onboarding inherits the Settings palette and controls');
  assert.ok(button('Set up Bob on this PC').classList.contains('primary')); assert.ok(button('Connect to a Bob server')); noHello();
  await click('Set up Bob on this PC');
  for (const text of [/wsl --install/, /administrator/, /Reboot/]) assert.match(document.body.textContent, text);
  noHello(); runtime.wsl = 'ok'; await click('Retry'); await click('Install Bob');
  assert.match(document.querySelector('[aria-label="Installation progress"]').textContent, /Installing Bob/);
  await input('first-name', 'dad'); await input('first-password', 'correct-password'); await input('first-confirm', 'wrong-password'); await submit();
  assert.match(document.querySelector('[role="alert"]').textContent, /Passwords do not match/);
  assert.equal(calls.filter((c) => c.cmd === 'runtime_add_lane').length, 0);
  assert.equal(document.getElementById('first-password').value, ''); assert.equal(document.getElementById('first-confirm').value, '');
  await input('first-password', 'short'); await input('first-confirm', 'short'); await submit();
  assert.match(document.querySelector('[role="alert"]').textContent, /at least 10/);
  await input('first-name', 'Dad'); await input('first-password', 'correct-password'); await input('first-confirm', 'correct-password'); await submit();
  assert.match(document.querySelector('[role="alert"]').textContent, /\[a-z\]/);
  await input('first-name', 'dad'); await input('first-password', 'correct-password'); await input('first-confirm', 'correct-password');
  laneFailure = true; await submit(); assert.match(document.querySelector('[role="alert"]').textContent, /Lane creation refused/);
  assert.equal(document.getElementById('first-password').value, ''); noHello(); laneFailure = false; delayLane = true;
  await input('first-password', 'correct-password'); await input('first-confirm', 'correct-password'); await submit();
  assert.equal(document.getElementById('first-password').value, '', 'password cleared while request is pending');
  assert.equal(document.getElementById('first-confirm').value, '');
  assert.deepEqual(calls.filter((c) => c.cmd === 'runtime_add_lane').at(-1).args, { name: 'dad', password: 'correct-password' });
  await flush(() => completeLane()); assert.ok(document.querySelector('svg[aria-label="Authenticator QR code"] path'));
  assert.ok(!document.body.textContent.includes(URI)); assert.ok(button('Continue').disabled); noHello();
  await flush(() => document.querySelector('input[type="checkbox"]').click()); await click('Continue'); noHello();
  assert.match(document.body.textContent, /Embedder/); assert.match(document.body.textContent, /keyword-only/);
  assert.equal(calls.some(c => c.cmd === 'runtime_embedder_install'), false);
  await click('Skip for now');
  assert.match(document.body.textContent, /Connect a model/); await click('Skip for now');
  assert.match(document.body.textContent, /Chat needs a model/); noHello(); await click('Connect'); await flush();
  assert.match(document.body.textContent, /identity dad/); assert.ok(calls.some((c) => c.cmd === 'portal_hello'));
  assert.ok(!listeners.has('runtime://progress')); await flush(() => root.unmount());
  reset(); root = await mount(); await click('Connect to a Bob server');
  for (const field of ['user', 'host', 'broker']) assert.ok(document.getElementById(`connection-${field}`));
  assert.equal(document.querySelector('input[value="this_pc"]'), null);
  saveFailure = true; await click('Save'); assert.match(document.querySelector('[role="alert"]').textContent, /Invalid server host/); noHello();
  saveFailure = false; await click('Save'); await flush();
  assert.deepEqual(calls.find((c) => c.cmd === 'portal_connection_set').args, { mode: 'server', user: 'operator', host: 'example-server', broker: '/opt/bob/portal/portal-broker.py' });
  assert.match(document.body.textContent, /identity dad/); await flush(() => root.unmount());
  reset(); configured = true; root = await mount(); assert.ok(!button('Set up Bob on this PC')); assert.match(document.body.textContent, /identity dad/);
  await flush(() => root.unmount());
  reset(); runtime.wsl = 'needs_wsl2'; root = await mount(); await click('Set up Bob on this PC');
  assert.match(document.body.textContent, /wsl --update/); await flush(() => root.unmount());
  reset(); runtime.wsl = 'ok'; installFailure = true; root = await mount(); await click('Set up Bob on this PC'); await click('Install Bob');
  assert.match(document.querySelector('[role="alert"]').textContent, /runtime preserved/); assert.ok(!button('Install Bob'));
  runtime = { ...runtime, distro_installed: true, firstboot: false }; await click('Retry');
  assert.ok(!button('Install Bob')); await flush(() => root.unmount());
  reset(); runtime = { ...runtime, wsl: 'ok', distro_installed: true, firstboot: false };
  root = await mount(); await click('Set up Bob on this PC'); assert.ok(!button('Install Bob')); assert.match(document.body.textContent, /completing its first boot/);
  runtime.firstboot = true; await click('Retry'); assert.ok(document.getElementById('first-name'));
  // WSF1 F6: an empty lane list keeps today's flow and copy exactly.
  assert.equal(document.querySelector('h2').textContent, 'Create your first lane');
  assert.equal(document.querySelector('input[name="lane-choice"]'), null); assert.equal(document.getElementById('existing-lane'), null);
  await flush(() => root.unmount());

  // WSF1 F6: the app reinstalled over a runtime that already has lanes.
  const text = () => document.body.textContent, count = (cmd) => calls.filter((c) => c.cmd === cmd).length;
  const laneRadio = (value) => document.querySelector(`input[name="lane-choice"][value="${value}"]`);
  const existingRuntime = (lanes) => ({ ...runtime, wsl: 'ok', distro_installed: true, firstboot: true, healthy: true, lanes });
  reset(); runtime = existingRuntime(['dad', 'luna', 'work']);
  root = await mount(); await click('Set up Bob on this PC');
  assert.doesNotMatch(text(), /first lane/i, 'a runtime with lanes is never offered a "first" lane');
  assert.match(text(), /Choose a lane/); assert.match(text(), /already has 3 lanes/);
  assert.ok(laneRadio('existing').checked, 'Use an existing lane is offered and preselected');
  assert.match(laneRadio('existing').parentElement.textContent, /Use an existing lane/);
  assert.match(laneRadio('new').parentElement.textContent, /Create a new lane/);
  const select = document.getElementById('existing-lane');
  assert.deepEqual([...select.options].map((o) => o.value), ['dad', 'luna', 'work'], 'only the lanes runtime_status reported');
  assert.equal(document.querySelector('form input'), null, 'no free text on the existing-lane path');
  assert.equal(document.getElementById('first-name'), null);
  await flush(() => { select.value = 'luna'; select.dispatchEvent(new Event('change', { bubbles: true })); });
  saveFailure = true; await submit();
  assert.match(document.querySelector('[role="alert"]').textContent, /Invalid server host/, 'a refused save is shown');
  assert.ok(document.getElementById('existing-lane'), 'and the choice stays on screen'); noHello();
  saveFailure = false; await submit();
  assert.deepEqual(calls.filter((c) => c.cmd === 'portal_connection_set').at(-1).args, { mode: 'this_pc', lane: 'luna' },
    'saved through the Settings → Connection command with the This PC payload');
  assert.equal(count('runtime_add_lane'), 0, 'the existing lane is not created or modified');
  assert.equal(count('runtime_install'), 0);
  assert.equal(document.querySelector('svg[aria-label="Authenticator QR code"]'), null, 'no new authenticator secret for an existing lane');
  assert.match(text(), /Embedder/, 'continues with the same embedder step'); assert.match(text(), /keyword-only/); noHello();
  assert.ok(!button('Back'), 'no way back into lane setup once the connection is saved');
  await click('Skip for now'); assert.match(text(), /Connect a model/, 'then the same model step');
  await click('Skip for now'); assert.match(text(), /Chat needs a model/); assert.match(text(), /existing lane luna/); noHello();
  await click('Connect'); await flush();
  assert.match(text(), /identity dad/); assert.ok(calls.some((c) => c.cmd === 'portal_hello'));
  assert.equal(count('portal_connection_set'), 2, 'one refused and one accepted save, nothing else written');
  assert.equal(count('runtime_add_lane'), 0); await flush(() => root.unmount());

  // The same runtime can still take a new lane; it is just not called the first one.
  reset(); runtime = existingRuntime(['dad']);
  root = await mount(); await click('Set up Bob on this PC');
  assert.match(text(), /already has a lane/);
  await flush(() => laneRadio('new').click());
  assert.equal(document.getElementById('existing-lane'), null);
  assert.equal(document.querySelector('form h2').textContent, 'Create a new lane'); assert.doesNotMatch(text(), /first lane/i);
  await flush(() => laneRadio('existing').click()); assert.ok(document.getElementById('existing-lane'), 'the choice can be changed back');
  assert.equal(document.getElementById('first-name'), null);
  await flush(() => laneRadio('new').click());
  await input('first-name', 'kid'); await input('first-password', 'correct-password'); await input('first-confirm', 'correct-password'); await submit();
  assert.deepEqual(calls.filter((c) => c.cmd === 'runtime_add_lane').at(-1).args, { name: 'kid', password: 'correct-password' });
  assert.equal(count('portal_connection_set'), 0, 'creating a lane keeps its own native save path');
  assert.ok(document.querySelector('svg[aria-label="Authenticator QR code"] path')); noHello();
  await flush(() => root.unmount());
  console.log('portal-first-run-ui: choice, WSL instructions/retry, progress, existing distro, password validation/clearing, independent QR fixtures, scan acknowledgement, server save, configured bypass, connect, existing-lane choice/save/continue, new lane on an existing runtime, empty-lane copy unchanged PASS');
})().catch((error) => { console.error(error); process.exit(1); });
