// AU2: real PortalApp completion, owner questions and list presentation, without a server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost', pretendToBeVisual: true });
for (const k of ['window','document','HTMLElement','Event','MouseEvent','KeyboardEvent','CustomEvent','localStorage']) global[k] = dom.window[k];
global.IS_REACT_ACT_ENVIRONMENT = true;
const listeners = new Set(), events = require.resolve('@tauri-apps/api/event');
require.cache[events] = { id: events, filename: events, loaded: true, exports: { listen: async (_, fn) => { listeners.add(fn); return () => listeners.delete(fn); } } };
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const PortalApp = require('../src/portal/PortalApp.tsx').default;
const { DEFAULT_APPEARANCE } = require('../src/portal/SettingsView.tsx');
const realTimeout = global.setTimeout, realClear = global.clearTimeout, realInterval = global.setInterval, realClearInterval = global.clearInterval, realNow = Date.now;
let now = Date.parse('2026-11-16T12:00:00Z'); Date.now = () => now;
const timers = new Map();
function timeout(fn, ms = 0, ...args) { const id = { due: now + Math.max(1, ms), ms, interval: false }; timers.set(id, () => fn(...args)); return id; }
function interval(fn, ms = 0, ...args) { const id = { due: now + Math.max(1, ms), ms, interval: true }; timers.set(id, () => fn(...args)); return id; }
global.setTimeout = window.setTimeout = timeout; global.clearTimeout = window.clearTimeout = id => timers.delete(id);
global.setInterval = window.setInterval = interval; global.clearInterval = window.clearInterval = id => timers.delete(id);
global.requestAnimationFrame = fn => timeout(fn, 16);
const clone = value => JSON.parse(JSON.stringify(value));
const faces = ['assistant','builder','fixer'].map(id => ({ id, name: id[0].toUpperCase() + id.slice(1) }));
let root, sessions, history, calls, requests, serial, experience = 'simple', sendError = null, openError = null;
const row = (id, role, content, metadata = {}) => ({ id, role, content, metadata, created_at: new Date(now + (++serial) * 1000).toISOString() });
const count = cmd => calls.filter(c => c.cmd === cmd).length;
async function invoke(cmd, args) {
  calls.push({ cmd, args });
  switch (cmd) {
    case 'portal_connection_get': return { connection_configured: true };
    case 'portal_hello': return { identity: { sub: 'au2', gateway: 'fixture', faces, may_touch: [], may_not_touch: [] }, server: {}, gaps: [] };
    case 'portal_catalog': return { faces, models: [], backends: [], capabilities: { actions: [], warnings: [] } };
    case 'portal_conversations': return { items: clone(sessions), limit: 50, offset: 0 };
    case 'portal_conversation_open': if (openError) throw openError; return { conversation: clone(sessions.find(c => c.id === args.id)), messages: clone(history[args.id] ?? []), has_more: false };
    case 'portal_pins': return {};
    case 'portal_approvals': return { items: [] };
    case 'portal_runs_list': return { items: [] };
    case 'portal_teams_list_v2': return { items: [], next_cursor: null };
    case 'calendar_read': return [];
    case 'pc_poll': return { requests: [], outcomes: {} };
    case 'runtime_repair_status': return { repairable: true, observation: { state: 'healthy', observed: 'Healthy' } };
    case 'portal_answer': return { resolved: true, question_id: args.question_id, delivered_to_turn: true };
    case 'portal_messages_more': return { items: clone(history[args.id] ?? []), has_more: false };
    case 'portal_send': if (sendError) throw sendError; history[args.conversation_id].push(row('prompt-' + (++requests), 'user', args.content)); return { request_id: 'r' + requests, accepted: true };
    case 'portal_builder_mode_get_v2': return { conversation_id: args.conversation_id, project_id: null, mode: 'approve', revision: 0, expires_at: null, network: { allowlist: [], project_extra: [] }, approval_records: [], approvals: [] };
    default: throw Error('Unexpected AU2 fixture call ' + cmd);
  }
}
const flush = async (fn = () => {}) => act(async () => { await fn(); await new Promise(r => realTimeout(r, 0)); });
async function advance(ms) {
  const end = now + ms; let ticks = 0;
  while (true) {
    const next = [...timers].sort((a,b) => a[0].due - b[0].due)[0];
    if (!next || next[0].due > end) break;
    assert.ok(++ticks < 10000, 'bounded timers'); const [id, fn] = next; now = id.due;
    if (id.interval) id.due += Math.max(1, id.ms); else timers.delete(id);
    await flush(fn);
  }
  now = end; await flush();
}
async function click(node) { assert.ok(node, 'click target'); await flush(() => node.click()); }
async function type(node, value) {
  await flush(() => { Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), 'value').set.call(node, value); node.dispatchEvent(new Event('input', { bubbles: true })); });
}
async function emit(event, data) { await flush(() => { for (const fn of listeners) fn({ payload: JSON.stringify({ event, data }) }); }); }
const agentRow = (name = 'Assistant') => [...document.querySelectorAll('.agent-row')].find(b => b.querySelector('strong').textContent === name);
const contact = name => agentRow(name).closest('.agent-contact');
const expander = (name = 'Assistant') => contact(name).querySelector('.agent-expander');
const button = label => [...document.querySelectorAll('button')].find(b => b.textContent === label);
const transcript = () => document.querySelector('.transcript');
const messages = () => [...transcript().querySelectorAll('article.msg')];
const notice = () => document.querySelector('.composer .notice');
async function mount() { root = createRoot(document.getElementById('root')); await flush(() => root.render(React.createElement(PortalApp, { invoke, profiles: null, onProfiles: () => {} }))); }
async function unmount() { if (root) { await flush(() => root.unmount()); root = null; } }
function reset() {
  localStorage.clear(); timers.clear(); calls = []; requests = 0; serial = 0; sendError = null; openError = null;
  localStorage.setItem('bobportal.appearance', JSON.stringify({ ...DEFAULT_APPEARANCE, experience }));
  sessions = Array.from({ length: 13 }, (_, i) => ({ id: 'a' + i, face_id: 'assistant', title: i === 11 ? 'Assistant · 2026-11-16 09:04' : i === 12 ? 'Assistant · Nov 16, 2026, 04:47 PM' : 'Planning ' + i, created_at: new Date(now - (i + 1) * 60000).toISOString() }));
  sessions.push({ id: 'b', face_id: 'builder', title: 'Builder work', created_at: '2026-11-15T00:00:00Z' }, { id: 'b2', face_id: 'builder', title: 'Earlier build', created_at: '2026-11-14T00:00:00Z' });
  history = Object.fromEntries(sessions.map(s => [s.id, []]));
}

const stopped = 'tool_round_limit';
const reply = 'I reached the limit of steps for one reply before finishing. Reply "continue" to carry on.';
async function start() {
  await mount(); await click(agentRow());
  await type(document.getElementById('composer'), 'Please work on this.'); await click(button('Send to Bob'));
}
function assertUnfinished(extensions = 0, latest = true) {
  assert.equal(document.querySelectorAll('.reply-status').length, 1);
  assert.match(document.querySelector('.reply-status').textContent, /Not finished: it reached the step limit for one reply\./);
  assert.equal(document.querySelectorAll('.reply-status button').length, latest ? 1 : 0);
  if (extensions) assert.match(document.querySelector('.reply-status').textContent, new RegExp(`Extended ${extensions} times\\.`));
}
async function replyChecks() {
  // The frame is authoritative for live display even without durable metadata.
  for (const historyMode of ['missing-metadata', 'different-id', 'stored', 'pending', 'read-failed']) {
    reset(); await start();
    if (historyMode !== 'pending') history.a0.push(row('final', 'assistant', reply, historyMode === 'stored' ? { stopped_reason: stopped, step_extensions: 2 } : {}));
    if (historyMode === 'read-failed') openError = { code: 'unavailable', detail: 'Fixture history is unavailable' };
    await emit('chunk', { request_id: 'r1', content: reply });
    await emit('complete', { request_id: 'r1', message_id: historyMode === 'different-id' ? 'completion-only' : 'final', stopped_reason: stopped, step_extensions: 2 });
    assertUnfinished(2);
    assert.equal(transcript().querySelectorAll('.msg.assistant').length, 1, historyMode + ' keeps one reply');
    assert.equal(count('portal_send'), 1, 'no automatic continue');
    if (historyMode === 'pending') { await advance(1500); assertUnfinished(2); }
    else if (historyMode !== 'read-failed') {
      if (historyMode === 'missing-metadata') {
        await click(button('Continue')); assertUnfinished(2, false);
        assert.equal(count('portal_send'), 2, 'observed marker survives the next send');
      }
      // Remount discards live completion state; only durable metadata counts.
      await unmount(); await mount(); await click(agentRow());
      assert.equal(!!button('Continue'), historyMode === 'stored');
    }
    openError = null; await unmount();
  }
  // Text alone never supplies a stop reason, live or after reopening.
  reset(); await start(); history.a0.push(row('ordinary', 'assistant', reply));
  await emit('chunk', { request_id: 'r1', content: reply });
  await emit('complete', { request_id: 'r1', message_id: 'ordinary' });
  assert.equal(document.querySelector('.reply-status'), null);
  await unmount(); await mount(); await click(agentRow()); assert.equal(document.querySelector('.reply-status'), null); await unmount();

  reset(); history.a0.push(row('old', 'assistant', reply, { stopped_reason: stopped }));
  await mount(); await click(agentRow());
  await type(document.getElementById('composer'), 'An unrelated draft');
  const continueButton = button('Continue');
  await flush(() => { continueButton.click(); continueButton.click(); });
  const sends = calls.filter(c => c.cmd === 'portal_send');
  assert.equal(sends.length, 1, 'a double click dispatches once');
  assert.deepEqual(sends[0].args, { conversation_id: 'a0', content: 'continue' });
  assert.equal(button('Continue'), undefined, 'the newer pending message hides Continue');
  await unmount();

  reset(); history.a0.push(row('old', 'assistant', reply, { stopped_reason: stopped }));
  await mount(); await click(agentRow()); sendError = { code: 'rate_limited', detail: 'Fixture rate limit' };
  await click(button('Continue'));
  assert.equal(count('portal_send'), 1); assert.equal(document.getElementById('composer').value, 'continue', 'normal refusal restores submitted draft');
  assert.equal(notice().textContent, require('../src/portal/portal-protocol.ts').RATE_LIMIT_SEND_LINE); await advance(1000); assert.equal(count('portal_send'), 1, 'no retry'); await unmount();
}
async function questionChecks() {
  for (const platform of [false, true]) {
    for (const choice of ['Keep going', 'Stop here']) {
      reset(); await start();
      await emit('question', { request_id: 'r1', question_id: 'step-1', question: 'The agent has used 50 steps and is not finished. May it keep going?', choices: ['Keep going', 'Stop here'], ...(platform ? { origin: 'step_limit' } : {}) });
      assert.equal(document.querySelector('.ask-card h2').textContent, platform ? 'BoB asks' : 'Assistant is asking');
      assert.match(agentRow().textContent, /needs you/);
      assert.match(document.querySelector('#activity-panel').textContent, /Assistant needs you/);
      await advance(1000); assert.equal(count('portal_answer'), 0, 'mode never answers automatically');
      assert.equal(count('portal_send'), 1);
      await click([...document.querySelectorAll('.ask-card button')].find(b => b.textContent === choice));
      assert.deepEqual(calls.filter(c => c.cmd === 'portal_answer').map(c => c.args), [{ question_id: 'step-1', answer: choice }]);
      assert.equal(document.querySelector('.ask-card'), null);
      await unmount();
    }
  }
  for (const extensions of [1, 2]) {
    reset(); await start();
    for (let i = 0; i < extensions; i++) {
      const part = `Progress before extension ${i + 1}.\n`;
      await emit('chunk', { request_id: 'r1', content: part });
      history.a0.push(row('pre-' + i, 'assistant', part, { request_id: 'r1', kind: 'pre_question', question_id: 'q-' + i, origin: 'step_limit' }));
      await emit('question', { request_id: 'r1', question_id: 'q-' + i, question: 'May it keep going?', choices: ['Keep going', 'Stop here'], origin: 'step_limit' });
      assert.equal(count('portal_answer'), i);
      history.a0.push(row('answer-' + i, 'user', 'Keep going', { request_id: 'r1', kind: 'operator_answer', question_id: 'q-' + i }));
      await click([...document.querySelectorAll('.ask-card button')].find(b => b.textContent === 'Keep going'));
    }
    const tail = 'Finished the work.';
    history.a0.push(row('final', 'assistant', tail, { request_id: 'r1', step_extensions: extensions }));
    await emit('chunk', { request_id: 'r1', content: tail });
    await emit('complete', { request_id: 'r1', message_id: 'final', step_extensions: extensions });
    assert.equal(messages().length, 2 + 2 * extensions);
    assert.equal(transcript().querySelectorAll('.msg.assistant').length, extensions + 1);
    assert.equal(count('portal_answer'), extensions);
    assert.equal(document.querySelectorAll('.reply-status').length, 1);
    assert.match(document.querySelector('.reply-status').textContent, new RegExp(`Extended ${extensions} time`));
    assert.doesNotMatch(document.querySelector('.reply-status').textContent, /Not finished/);
    assert.equal(button('Continue'), undefined);
    await unmount(); await mount(); await click(agentRow());
    assert.equal(messages().length, 2 + 2 * extensions, 'reopened split transcript keeps each stored row once');
    await unmount();
  }
}
async function listChecks() {
  reset(); sessions[0].title = 'A long conversation title that uses all the available width before the time and wraps to a second line';
  await mount();
  assert.equal(expander().tagName, 'BUTTON'); assert.equal(expander().closest('.agent-row'), null);
  assert.equal(expander().getAttribute('aria-expanded'), 'false');
  const closedPath = expander().querySelector('path').getAttribute('d');
  expander().focus(); assert.equal(document.activeElement, expander());
  const reads = calls.length;
  // JSDOM lacks native key activation. This is the native button's keyboard
  // click shape; physical browser geometry/activation is a separate check.
  await flush(() => document.activeElement.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 0 })));
  assert.equal(expander().getAttribute('aria-expanded'), 'true');
  assert.notEqual(expander().querySelector('path').getAttribute('d'), closedPath);
  assert.equal(calls.length, reads);
  const style = document.createElement('style'); style.textContent = fs.readFileSync(require.resolve('../src/portal/portal.css'), 'utf8'); document.head.append(style);
  const css = window.getComputedStyle(expander());
  assert.ok(parseFloat(css.minWidth) >= 28); assert.ok(parseFloat(css.minHeight) >= 28);
  const title = document.querySelector('.agent-sessions button > span'), titleCss = window.getComputedStyle(title);
  assert.equal(title.textContent, sessions[0].title);
  assert.equal(titleCss.whiteSpace, 'normal'); assert.equal(titleCss.getPropertyValue('-webkit-line-clamp'), '2');
  assert.equal(window.getComputedStyle(title.parentElement).gridTemplateColumns, 'minmax(0, 1fr) auto');
  assert.equal(window.getComputedStyle(document.querySelector('.agent-preview')).whiteSpace, 'nowrap');
  assert.match(style.textContent, /\.agent-expander:hover[^}]*background:/);
  assert.match(style.textContent, /\.agent-expander:focus-visible[^}]*outline:/);
  style.remove(); await unmount();
}
(async () => {
  for (experience of ['simple', 'pro']) { await replyChecks(); await questionChecks(); await listChecks(); }
  console.log('AU2: PASS (Simple/Pro live/stored/legacy/pending/failed-read replies and differing IDs; click-only Continue, double-click/locks/refusals; platform/agent questions, both choices, attention, one/two extensions; expander size/focus/button semantics and two-line CSS)');
})().catch(e => { console.error(e); process.exitCode = 1; }).finally(async () => { await unmount(); global.setTimeout=realTimeout;global.clearTimeout=realClear;global.setInterval=realInterval;global.clearInterval=realClearInterval;Date.now=realNow;dom.window.close(); });
