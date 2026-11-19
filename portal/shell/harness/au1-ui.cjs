// AU1: real PortalApp navigation/readback and escaped Markdown, without a server.
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
const { PortalMarkdown } = require('../src/portal/PortalMarkdown.tsx');
const { ExperienceContext } = require('../src/portal/experience.ts');
const { DEFAULT_APPEARANCE } = require('../src/portal/SettingsView.tsx');
const { hasTurnRow } = require('../src/portal/transcript-copies.ts');
const { turnAnswerRowId } = require('../src/portal/portal-state.ts');
const { displaySessionTitle, rememberDefaultTitle } = require('../src/portal/session-names.ts');
const { agentConversationsKey } = require('../src/portal/panel-collapse.ts');
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
let root, sessions, history, calls, requests, serial, experience = 'simple';
const row = (id, role, content, metadata = {}) => ({ id, role, content, metadata, created_at: new Date(now + (++serial) * 1000).toISOString() });
const count = cmd => calls.filter(c => c.cmd === cmd).length;
const viewer = JSON.stringify(['fixture', 'au1']);
async function invoke(cmd, args) {
  calls.push({ cmd, args });
  switch (cmd) {
    case 'portal_connection_get': return { connection_configured: true };
    case 'portal_hello': return { identity: { sub: 'au1', gateway: 'fixture', faces, may_touch: [], may_not_touch: [] }, server: {}, gaps: [] };
    case 'portal_catalog': return { faces, models: [], backends: [], capabilities: { actions: [], warnings: [] } };
    case 'portal_conversations': return { items: clone(sessions), limit: 50, offset: 0 };
    case 'portal_conversation_open': return { conversation: clone(sessions.find(c => c.id === args.id)), messages: clone(history[args.id] ?? []), has_more: false };
    case 'portal_pins': return {};
    case 'portal_approvals': return { items: [] };
    case 'portal_runs_list': return { items: [] };
    case 'portal_teams_list_v2': return { items: [], next_cursor: null };
    case 'calendar_read': return [];
    case 'pc_poll': return { requests: [], outcomes: {} };
    case 'runtime_repair_status': return { repairable: true, observation: { state: 'healthy', observed: 'Healthy' } };
    case 'portal_send': history[args.conversation_id].push(row('prompt-' + (++requests), 'user', args.content)); return { request_id: 'r' + requests, accepted: true };
    case 'portal_builder_mode_get_v2': return { conversation_id: args.conversation_id, project_id: null, mode: 'approve', revision: 0, expires_at: null, network: { allowlist: [], project_extra: [] }, approval_records: [], approvals: [] };
    default: throw Error('Unexpected AU1 fixture call ' + cmd);
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
  localStorage.clear(); timers.clear(); calls = []; requests = 0; serial = 0;
  localStorage.setItem('bobportal.appearance', JSON.stringify({ ...DEFAULT_APPEARANCE, experience }));
  sessions = Array.from({ length: 13 }, (_, i) => ({ id: 'a' + i, face_id: 'assistant', title: i === 11 ? 'Assistant · 2026-11-16 09:04' : i === 12 ? 'Assistant · Nov 16, 2026, 04:47 PM' : 'Planning ' + i, created_at: new Date(now - (i + 1) * 60000).toISOString() }));
  sessions.push({ id: 'b', face_id: 'builder', title: 'Builder work', created_at: '2026-11-15T00:00:00Z' }, { id: 'b2', face_id: 'builder', title: 'Earlier build', created_at: '2026-11-14T00:00:00Z' });
  history = Object.fromEntries(sessions.map(s => [s.id, []]));
}
async function listChecks() {
  reset(); await mount();
  assert.equal(document.querySelectorAll('.agent-row').length, 3);
  assert.equal(document.querySelectorAll('.agent-sessions').length, 0, 'one contact row, no sessions by default');
  assert.equal(agentRow().querySelector('.agent-preview').textContent, 'Planning 0');
  assert.ok(agentRow().querySelector('.avatar')); assert.equal(agentRow().querySelector('time').textContent, '1m');
  assert.equal(expander().getAttribute('aria-label'), 'Show all 13 conversations with Assistant');
  assert.equal(expander().closest('.agent-row'), null, 'separate button, never nested');
  assert.equal(expander().tagName, 'BUTTON'); assert.ok(expander().tabIndex >= 0);
  expander().focus(); assert.equal(document.activeElement, expander(), 'keyboard focus reaches the control');
  const initial = calls.length; await click(document.activeElement);
  assert.equal(calls.length, initial, 'expansion adds no request and opens no conversation');
  assert.equal(document.querySelector('.session-divider'), null);
  let list = document.getElementById(expander().getAttribute('aria-controls'));
  assert.equal(list.querySelectorAll('button').length, 13);
  assert.equal(list.querySelector('button').textContent, 'Planning 01m');
  assert.equal(list.querySelectorAll('button')[11].querySelector('span').textContent, 'New conversation');
  assert.equal(list.querySelectorAll('button')[12].querySelector('span').textContent, 'New conversation');
  assert.ok([...list.querySelectorAll('time')].every(n => n.textContent));
  await click(list.querySelectorAll('button')[2]); await click(expander());
  assert.equal(agentRow().querySelector('.agent-preview').textContent, 'Planning 2', 'closed preview follows the older open conversation');
  await click(agentRow()); assert.match(document.querySelector('.session-divider').textContent, /Planning 0/);
  await click(expander()); list = document.querySelector('.agent-sessions');
  assert.equal(list.querySelector('[aria-current="true"] span').textContent, 'Planning 0');
  await click(expander('Builder')); await click(expander());
  await unmount(); await mount();
  assert.equal(expander().getAttribute('aria-expanded'), 'false');
  assert.equal(expander('Builder').getAttribute('aria-expanded'), 'true', 'preferences survive reload independently per agent');
  const saved = localStorage.getItem(agentConversationsKey(viewer)), reads = calls.length;
  await type(document.getElementById('conversation-search'), 'Planning 7');
  assert.equal(document.querySelectorAll('.agent-row').length, 1);
  assert.equal(document.querySelectorAll('.agent-sessions button').length, 1);
  assert.equal(document.querySelector('.agent-sessions button span').textContent, 'Planning 7');
  await type(document.getElementById('conversation-search'), 'New conversation');
  assert.equal(document.querySelectorAll('.agent-sessions button').length, 2, 'search uses displayed default titles');
  await type(document.getElementById('conversation-search'), '');
  assert.equal(localStorage.getItem(agentConversationsKey(viewer)), saved);
  assert.equal(expander().getAttribute('aria-expanded'), 'false'); assert.equal(expander('Builder').getAttribute('aria-expanded'), 'true');
  assert.equal(calls.length, reads, 'search and expansion do not introduce background reads');
  await click(expander()); list = document.getElementById(expander().getAttribute('aria-controls'));
  await click(list.querySelectorAll('button')[11]);
  assert.match(document.querySelector('.conversation-header').textContent, /New conversation/);
  assert.doesNotMatch(document.querySelector('.conversation-header').textContent, /Assistant · 2026/);
  assert.match(document.querySelector('.session-list').textContent, /New conversation/);
  assert.equal(sessions[11].title, 'Assistant · 2026-11-16 09:04', 'display never renames stored rows');
  await click(expander()); await click(agentRow()); await type(document.getElementById('composer'), 'Ask me.'); await click(button('Send to Bob'));
  await click(contact('Builder').querySelector('.agent-row'));
  await emit('question', { conversation_id: 'a0', request_id: 'r1', question_id: 'q', question: 'Which colour?', choices: ['Blue'] });
  assert.equal(expander().getAttribute('aria-expanded'), 'false'); assert.ok(agentRow().querySelector('.chip.needs_you'));
  assert.ok(agentRow().querySelector('.chip.working'), 'a closed list keeps both needs-you and working signals');
  await unmount();
  // Bad/disabled local storage has the same safe, closed default as panel collapse.
  reset(); localStorage.setItem(agentConversationsKey(viewer), 'broken JSON'); await mount(); assert.equal(document.querySelector('.agent-sessions'), null); await unmount();
}
const prompt = 'Please explain the architecture.';
const before = 'First part before the question.\n\n' + 'Architecture evidence. '.repeat(190);
const after = 'Second part after the answer.\n\n' + 'Verified conclusions. '.repeat(190);
function splitRows(requestId) {
  const meta = requestId ? { request_id: requestId } : {};
  return [row('prompt-1','user',prompt), row('pre','assistant',before,{...meta,kind:'pre_question',question_id:'84a3-352d'}), row('answer','user','Use the detailed explanation.',{...meta,kind:'operator_answer',question_id:'84a3-352d'}), row('final','assistant',after,{...meta,tokens_in:19,tokens_out:2083})];
}
async function transcriptChecks() {
  for (const tagged of [false, true]) {
    reset(); await mount(); await click(agentRow());
    await type(document.getElementById('composer'), prompt); await click(button('Send to Bob'));
    history.a0 = splitRows(tagged ? 'r1' : null);
    await emit('chunk', { request_id: 'r1', content: before + after });
    await emit('complete', { request_id: 'r1', message_id: 'not-a-stored-id' });
    assert.equal(messages().length, 4, 'split turn has four stored messages, never a fifth');
    assert.deepEqual(messages().map(n => n.classList.contains('assistant') ? 'assistant' : 'user'), ['user','assistant','user','assistant']);
    assert.equal(transcript().querySelectorAll('.msg.assistant').length, 2); assert.equal(notice(), null);
    assert.doesNotMatch(document.body.textContent, /not readable from history|check that it was saved/);
    const reads = count('portal_conversation_open'); await advance(1600); assert.equal(count('portal_conversation_open'), reads, 'stored split answer schedules no reread');
    // Repeating an identical answer after a fresh prompt remains a distinct turn.
    await type(document.getElementById('composer'), 'A fresh prompt'); await click(button('Send to Bob'));
    history.a0.push(row('second-final','assistant',after, tagged ? {request_id:'r2'} : {}));
    await emit('chunk', {request_id:'r2',content:after}); await emit('complete', {request_id:'r2',message_id:'second-completion'});
    assert.equal(messages().length, 6); assert.equal(transcript().querySelectorAll('.msg.assistant').length, 3); assert.equal(notice(), null);
    await unmount();
  }
  // Platform step questions split the same turn repeatedly, just like agent questions.
  for (const extensions of [1, 2]) for (const tagged of [false, true]) {
    reset(); await mount(); await click(agentRow());
    await type(document.getElementById('composer'), prompt); await click(button('Send to Bob'));
    const meta = tagged ? { request_id: 'r1' } : {};
    const parts = [];
    history.a0 = [row('prompt-1', 'user', prompt, meta)];
    for (let n = 0; n < extensions; n++) {
      const text = `Progress before extension ${n + 1}.`; parts.push(text);
      history.a0.push(row('pre-' + n, 'assistant', text, { ...meta, kind: 'pre_question', question_id: 'step-' + n, origin: 'step_limit' }),
        row('answer-' + n, 'user', 'Keep going', { ...meta, kind: 'operator_answer', question_id: 'step-' + n }));
    }
    parts.push('Finished after the extensions.');
    history.a0.push(row('final', 'assistant', parts.at(-1), { ...meta, step_extensions: extensions }));
    await emit('chunk', { request_id: 'r1', content: parts.join('') });
    await emit('complete', { request_id: 'r1', message_id: 'completion-only', step_extensions: extensions });
    assert.equal(messages().length, 2 + 2 * extensions, 'each extension row appears once');
    assert.equal(transcript().querySelectorAll('.msg.assistant').length, extensions + 1);
    assert.equal(transcript().querySelectorAll('.reply-status').length, 1, 'only the final segment has an extension footer');
    assert.equal(notice(), null);
    await unmount();
  }
  for (const eventuallyStored of [true, false]) {
    reset(); await mount(); await click(agentRow()); await type(document.getElementById('composer'), prompt); await click(button('Send to Bob'));
    await emit('chunk', {request_id:'r1',content:after}); await emit('complete', {request_id:'r1',message_id:'pending-id'});
    assert.equal(messages().length, 2); assert.equal(transcript().querySelectorAll('.msg.assistant').length, 1);
    assert.match(notice().textContent, /check that it was saved/);
    const reads = count('portal_conversation_open');
    if (eventuallyStored) history.a0.push(row('stored-id','assistant',after));
    await advance(1500); assert.equal(count('portal_conversation_open'), reads + 1);
    assert.equal(messages().length, 2);
    if (eventuallyStored) { assert.equal(notice(), null); assert.match(transcript().textContent, /message:stored-id/); }
    else assert.equal(notice().textContent, 'The answer is shown here, but it is still missing from saved history after checking again.');
    await advance(10000); assert.equal(count('portal_conversation_open'), reads + 1, 'only one scheduled reread'); await unmount();
  }
  const mark = {conversation_id:'a0',count:0,last_id:null,text:prompt};
  serial = 0; const rows = splitRows(null);
  assert.equal(turnAnswerRowId(rows,mark,before + '\n' + after,'a0'),'final');
  assert.equal(turnAnswerRowId(rows,mark,before + after,'a0'),'final');
  assert.equal(turnAnswerRowId(rows.slice(0,2),mark,before,'a0'),null,'a pre-question row is not the completed answer');
  assert.equal(turnAnswerRowId(rows.map(r=>r.id==='answer'?{...r,metadata:{kind:'operator_answer',question_id:'wrong'}}:r),mark,before+after,'a0'),null,'question IDs must correlate');
  assert.equal(turnAnswerRowId([...rows.slice(0,2),row('new','user','Another dispatch'),rows[3]],mark,before+after,'a0'),null,'a fresh prompt ends the turn window');
  assert.equal(hasTurnRow(splitRows('r1'),'a0','r1','other',before+after+'\n',null),true,'joined request segments match without an anchor');
  assert.equal(hasTurnRow(splitRows('r1').slice(0,2),'a0','r1','other',before,null),false,'request ID on the question prefix alone is insufficient');
  assert.equal(hasTurnRow(rows,'a0','r2','other',before+after,null),false,'unanchored identical older text proves nothing');
}
async function markdownChecks() {
  root = createRoot(document.getElementById('root')); let key = 0;
  async function render(text) { await flush(() => root.render(React.createElement(ExperienceContext.Provider,{value:experience,key:++key},React.createElement(PortalMarkdown,{text,role:'assistant'})))); }
  for (const heading of ['Details','Details:','**Details**','**Details:**','**Details**:','# Details','### **Details:**','###### Details:']) {
    await render('The readable answer.\n'+heading+'\nInternal `model-name`.\n\n- Evidence');
    const fold = document.querySelector('.md > details'); assert.ok(fold,heading); assert.equal(fold.open,false); assert.equal(fold.querySelector('summary').textContent,'Details'); assert.equal(fold.querySelector('code').textContent,'model-name'); assert.ok(fold.querySelector('ul'));
  }
  for (const text of ['The answer.\n<details>\n<summary>Details</summary>\nInternal **evidence**.\n</details>\nAfter the fold.', '<details>Internal **evidence**.</details>', '<details><summary>Other label</summary>Internal **evidence**.</details>']) {
    await render(text); const fold=document.querySelector('.md > details'); assert.ok(fold); assert.equal(fold.open,false); assert.equal(fold.querySelector('summary').textContent,'Details'); assert.equal(fold.querySelector('strong').textContent,'evidence'); assert.doesNotMatch(document.querySelector('.md').textContent, /<\/?(?:details|summary)>/);
  }
  await render('Keep the details in this sentence.'); assert.equal(document.querySelector('.md details'),null);
  await render('You picked B.\n\nDetails: I used the ask_operator tool.'); { const fold=document.querySelector('.md > details'); assert.ok(fold,'one-paragraph Details folds'); assert.equal(fold.open,false); assert.match(fold.textContent,/ask_operator/); assert.doesNotMatch(document.querySelector('.md > p').textContent,/ask_operator/); }
  await render('First line.\nDetails: still the same paragraph.'); assert.equal(document.querySelector('.md details'),null,'mid-paragraph Details: is untouched');
  await render('```text\nDetails\n<details>Example</details>\n```'); assert.equal(document.querySelector('.md-subtask'),null,'fenced examples are untouched');
  await render('<details>\n<summary>Details</summary>\n<script>window.au1Unsafe = true</script>\n<img src=x onerror=alert(1)>\n</details>');
  assert.equal(document.querySelector('.md script, .md img'),null); assert.match(document.querySelector('.md').textContent,/<script>/); assert.equal(window.au1Unsafe,undefined);
  await render('<details>\nOuter\n<details>Nested</details>\n```text\n</details>\n```\n</details>'); assert.equal(document.querySelectorAll('.md-subtask').length,2,'nested folds and fenced closing tags remain balanced');
  await render('Unmatched\n<details>literal'); assert.match(document.querySelector('.md').textContent,/<details>literal/);
  await unmount();
}
async function budgetChecks() {
  // These exact 60-second totals were also measured with the original HEAD
  // modules and GL19's fixture; keep both native and gateway counts unchanged.
  for (const [label, expected, gateway] of [['idle',6,0],['one turn',8,2],['two turns',10,4],['needs',6,0],['runs',6,0]]) {
    reset(); sessions.find(s=>s.id==='b').face_id='fixer'; await mount(); await click(agentRow());
    if (label==='one turn'||label==='two turns') { await type(document.getElementById('composer'),'Budget prompt A'); await click(button('Send to Bob')); }
    if (label==='two turns') { await click(agentRow('Fixer')); await type(document.getElementById('composer'),'Budget prompt B'); await click(button('Send to Bob')); }
    if (label==='needs') await click([...document.querySelectorAll('button')].find(b=>b.title.startsWith('Needs you')));
    if (label==='runs') { await click(button('Tasks')); await click(button('Run history')); }
    await advance(1000); calls=[]; await advance(60000);
    assert.equal(calls.length,expected,label+' keeps the GL19 total');
    assert.equal(calls.filter(c=>c.cmd.startsWith('portal_')).length,gateway,label+' keeps the GL19 gateway total');
    await unmount();
  }
}
(async () => {
  const css = fs.readFileSync(require.resolve('../src/portal/portal.css'),'utf8');
  assert.match(css,/\.agent-sessions\s*\{[^}]*max-height:[^}]*overflow-y:\s*auto/s,'long lists scroll within a capped height');
  assert.equal(displaySessionTitle({id:'custom',title:'Assistant · Architecture'},'Assistant'),'Assistant · Architecture');
  rememberDefaultTitle('named',null); assert.equal(displaySessionTitle({id:'named',title:'Assistant · 2026-11-16 09:04'},'Assistant'),'Assistant · 2026-11-16 09:04','explicitly named titles stay intact');
  for (experience of ['simple','pro']) { await listChecks(); await transcriptChecks(); await markdownChecks(); await budgetChecks(); }
  console.log('AU1: PASS (Simple/Pro compact contacts, isolated accessible expanders, per-agent persistence, older preview, closed attention, search, default titles, split readback with/without request IDs, repeated answers, pending reread bounds, escaped Details folds; no navigation/search RPCs; exact GL19 60-second totals 6/8/10/6/6)');
})().catch(e => { console.error(e); process.exitCode = 1; }).finally(async () => { await unmount(); global.setTimeout=realTimeout;global.clearTimeout=realClear;global.setInterval=realInterval;global.clearInterval=realClearInterval;Date.now=realNow;dom.window.close(); });
