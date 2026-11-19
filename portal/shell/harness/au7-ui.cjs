// AU7: device sign-in recovery and terminal reasons in the shared setup card.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
for (const name of ['window', 'document', 'HTMLElement', 'Event', 'CustomEvent', 'localStorage']) global[name] = dom.window[name];
global.IS_REACT_ACT_ENVIRONMENT = true;
let copied = '', copyFails = false;
Object.defineProperty(global, 'navigator', { configurable: true, value: { userAgent: 'node', clipboard: { writeText: async text => { if (copyFails) throw Error('clipboard unavailable'); copied = text; } } } });
const listeners = new Set(), events = require.resolve('@tauri-apps/api/event');
require.cache[events] = { id: events, filename: events, loaded: true, exports: { listen: async (topic, fn) => {
  assert.equal(topic, 'runtime://model'); listeners.add(fn); return () => listeners.delete(fn);
} } };
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { ModelSetup } = require('../src/portal/ModelSetup.tsx');
let root, calls = [], login, opening;
const deferred = () => { let resolve, reject; const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; }); return { promise, resolve, reject }; };
async function invoke(cmd, args) {
  calls.push({ cmd, args });
  switch (cmd) {
    case 'runtime_model_providers': return require('./model-provider-fixture.cjs');
    case 'runtime_model_status': return { lane: 'owner', providers: { openai_signin: false }, default_backend: null, ready: false };
    case 'runtime_model_openai_login': login = deferred(); return login.promise;
    case 'runtime_model_open_url': opening = deferred(); return opening.promise;
    case 'runtime_model_cancel':
      login.reject('Model sign-in exited without successful completion'); return { ok: true };
    default: throw Error('Unexpected ' + cmd);
  }
}
const flush = async (fn = () => {}) => act(async () => { await fn(); await Promise.resolve(); });
const text = () => document.body.textContent;
const button = label => [...document.querySelectorAll('button')].find(b => b.textContent === label);
const click = async label => { assert.ok(button(label), label); await flush(() => button(label).click()); };
const event = async payload => { assert.equal(listeners.size, 1); await flush(() => { for (const listener of listeners) listener({ payload }); }); };
const URL = 'https://auth.openai.com/codex/device';
async function start() {
  await click('Sign in with OpenAI (ChatGPT)');
  await event({ event: 'device_code', url: URL, code: 'ABCD-1234' });
}
function deviceVisible() {
  assert.equal(document.querySelector('[aria-label="Device code"]').textContent, 'ABCD-1234');
  assert.equal(document.querySelector('[aria-label="Sign-in address"]').textContent, URL);
}
async function unmount() { await flush(() => root.unmount()); root = null; assert.equal(listeners.size, 0); }
const reasons = {
  signin_unreachable: 'Could not reach the sign-in service through the lane proxy.',
  signin_refused: 'The sign-in service refused the request.',
  signin_timeout: 'The sign-in timed out.',
  signin_cancelled: 'The sign-in was cancelled.',
  signin_not_stored: 'The sign-in finished but no usable sign-in was stored.',
  signin_unavailable: 'The sign-in helper or lane proxy is unavailable.',
  signin_failed: 'The sign-in failed; try again.',
};
(async () => {
  for (const signinOnly of [false, true]) {
    localStorage.clear(); calls = [];
    root = createRoot(document.getElementById('root'));
    await flush(() => root.render(React.createElement(ModelSetup, { invoke, signinOnly })));
    await start(); deviceVisible();
    assert.ok(button('Copy link'));
    const address = document.querySelector('[aria-label="Sign-in address"]');
    const range = document.createRange(); range.selectNodeContents(address);
    window.getSelection().removeAllRanges(); window.getSelection().addRange(range);
    assert.equal(window.getSelection().toString(), URL, 'address is ordinary selectable text');
    assert.match(text(), /If the sign-in page says device code sign-in is not enabled, turn it on in ChatGPT under Settings, Security, then press Sign in again for a new code\./);
    assert.match(text(), /whichever ChatGPT account your browser is using/);
    assert.match(text(), /account shown at the top of that page is the one with your subscription/);
    await click('Copy code'); assert.equal(copied, 'ABCD-1234');
    copyFails = true; await click('Copy link');
    assert.match(text(), /Select the sign-in address and copy it/); deviceVisible();
    copyFails = false; await click('Copy link'); assert.equal(copied, URL); assert.ok(button('Link copied'));
    await click('Open sign-in page'); assert.ok(button('Open sign-in page').disabled);
    assert.deepEqual(calls.find(c => c.cmd === 'runtime_model_open_url').args, { url: URL });
    await flush(() => opening.reject('fake-secret OS error'));
    const failure = 'The sign-in page could not be opened from here. Copy the link and open it in your browser.';
    assert.ok([...document.querySelectorAll('[role="alert"]')].some(n => n.textContent === failure));
    assert.ok(!text().includes('fake-secret')); deviceVisible();
    await click('Open sign-in page'); await flush(() => opening.resolve());
    assert.match(text(), /The sign-in page was opened in your default browser\./);
    assert.ok(!text().includes(failure)); deviceVisible();
    await click('Cancel sign-in');
    assert.match(text(), /The sign-in was cancelled\./); assert.ok(!text().includes('Sign-in stopped.'));
    assert.equal(listeners.size, 0); assert.ok(!document.querySelector('[aria-label="Device code"]'));
    assert.ok(!button('Sign in with OpenAI (ChatGPT)').disabled);
    for (const [code, detail] of Object.entries(reasons)) {
      await start(); assert.ok(!text().includes('was opened in your default browser'));
      await event({ event: 'error', code, detail });
      await flush(() => login.reject('fake-secret later command rejection'));
      const alert = document.querySelector('[data-signin-code]');
      assert.equal(alert.dataset.signinCode, code); assert.equal(alert.textContent, detail);
      assert.ok(!text().includes('Sign-in stopped.')); assert.ok(!text().includes('fake-secret'));
      assert.ok(!button('Sign in with OpenAI (ChatGPT)').disabled); assert.equal(listeners.size, 0);
    }
    await start(); await event({ event: 'error', detail: '' }); await flush(() => login.reject('later failure'));
    assert.match(text(), /Sign-in stopped\./); assert.ok(!text().includes('later failure'));
    await start(); await flush(() => login.reject('The sign-in was interrupted.'));
    assert.match(text(), /The sign-in was interrupted\./); assert.ok(!text().includes('Sign-in stopped.'));
    assert.equal(listeners.size, 0); assert.ok(!button('Sign in with OpenAI (ChatGPT)').disabled);
    await start(); await event({ event: 'error', code: 'signin_failed', detail: reasons.signin_failed });
    await flush(() => login.resolve({ ok: true }));
    assert.match(text(), /The sign-in failed; try again\./); assert.ok(!text().includes('OpenAI sign-in saved'));
    // A late browser receipt must not appear in a replacement sign-in attempt.
    await start(); await click('Open sign-in page'); const staleOpening = opening;
    await click('Cancel sign-in'); await start(); await flush(() => staleOpening.resolve());
    assert.ok(!text().includes('was opened in your default browser')); deviceVisible();
    await click('Cancel sign-in'); await unmount();
  }
  console.log('AU7 sign-in card: PASS (both setup surfaces, selectable URL, copy code/link and fallback, opened/failed/pending, seven reasons, cancellation race, interrupted EOF, fallback, stale receipt, retry)');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (root) await unmount(); dom.window.close(); });
