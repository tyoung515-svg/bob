// Offline integration: real components, client and mutation journal; no server.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'localStorage']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
global.requestAnimationFrame = callback => setTimeout(callback, 0);
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { RunsPanel } = require('../src/portal/DurableRunsPanel.tsx');
const { TeamProposalCard } = require('../src/portal/TeamProposalCard.tsx');
const { checkedBudget } = require('../src/portal/durable-runs.ts');
const catalog = { faces: [], models: [], backends: [], capabilities: { faces: 0, backends: 0, actions: [], warnings: [], execution_v2: {
  contract_versions: [2], execution_enabled: true, confirmation_threshold: 200, above_threshold_reason: 'owner_grant_service_unavailable',
  topologies: ['four-step-readonly/v1', 'agent-loop/v1']
} } };
const team = { schema_version: 1, id: 'configured-text', state: 'saved', topology: 'four-step-readonly/v1', roles: { manager: 'configured-one', worker: 'configured-one', critic: 'configured-one' }, revision: 1, eligibility: 'runnable', runnable: true, blocked_reasons: [] };
const proposal = { definition: { schema_version: 1, id: team.id, topology: team.topology, roles: team.roles }, expected_revision: null, task: 'Review source evidence', budget: { start_calls: 100, wall_mode: 'none', wall_seconds: null } };
const run = { id: 'server-run', contract_version: 2, team: team.id, status: 'running', phase: 'dispatch', task: proposal.task, budget: proposal.budget, requested_start_calls: 100, effective_call_limit: 200, calls_used: 3, reserved_calls: 0, unknown_calls: 0, estimated_remaining_calls: 1, deadline: null, review_independence: 'same_model' };
const detail = (overrides = {}) => ({ run: { ...run, ...overrides }, receipts: [{ role: 'worker', model_face: 'configured-one', output: 'evidence', usage: { estimated_tokens: 12 } }], events: [{ seq: 1, kind: 'interrupted', detail: { reason: 'core_shutdown' }, at: '2026-11-03' }], next_cursor: null });
const calls = [];
let state = {};
const client = createPortalClient(async (cmd, args) => {
  calls.push({ cmd, args });
  if (state.oldNative && cmd.endsWith('_v2')) throw `Command ${cmd} not found`;
  if (state.oldBroker && cmd.endsWith('_v2')) throw JSON.stringify({ code: 'unknown_op', detail: 'old broker' });
  switch (cmd) {
    case 'portal_teams_save_v2': state.saved = true; return { saved: true, revision: 1, team, operation_id: 'save-receipt' };
    case 'portal_teams_list_v2': return { items: state.hideUntilSaved && !state.saved ? [] : [{ ...team, ...(state.blocked ? { runnable: false, eligibility: 'blocked', blocked_reasons: ['opaque_call_accounting:worker'] } : {}) }], next_cursor: null };
    case 'portal_runs_list_v2':
      if (state.readError) throw JSON.stringify({ code: 'execution_storage_unavailable', detail: 'database read failed' });
      return { items: [{ ...run, id: args.cursor ? 'other-pc-page-2' : run.id }], source: 'server', coverage: 'authorized durable v2 runs', next_cursor: args.cursor ? null : 'page-2' };
    case 'portal_run_get_v2': return detail({ status: state.cancelled ? 'cancelled' : 'running' });
    case 'portal_runs_start_v2':
      if (state.loseStart) { state.loseStart = false; throw JSON.stringify({ code: 'transport_unavailable', detail: 'response lost' }); }
      if (args.budget.start_calls > 200) throw JSON.stringify({ code: 'gateway_http_409', detail: 'confirmation_required' });
      return detail({ status: 'accepted' });
    case 'portal_run_cancel_v2': state.cancelled = true; return detail({ status: 'cancelling' });
    case 'portal_runs_list': return { items: [], total: 0, caveats: ['legacy_portal_ledger'] };
    case 'portal_runs_start': return { run: { id: 'legacy', status: 'accepted' } };
    default: throw new Error(`unexpected ${cmd}`);
  }
});
async function flush(fn = () => {}) { await act(async () => { await fn(); await new Promise(resolve => setTimeout(resolve, 0)); }); }
const text = () => document.body.textContent;
const button = label => [...document.querySelectorAll('button')].find(b => b.textContent === label);
async function click(label) { const b = button(label); assert.ok(b, `button ${label}`); await flush(() => b.click()); }
async function input(label, value) {
  const el = document.querySelector(`[aria-label="${label}"]`);
  assert.ok(el, label);
  const proto = el.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : el.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  await flush(() => { Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value); el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); });
}
let root;
async function mount(element) { root = createRoot(document.getElementById('root')); await flush(() => root.render(element)); }
async function unmount() { await flush(() => root.unmount()); }
function panel(scope, seed = { ...proposal, team: team.id, at: 1 }) {
  function Host() { const [runs, setRuns] = React.useState([]); return React.createElement(RunsPanel, { client, catalog, scope, runs, onRuns: setRuns, seed }); }
  return React.createElement(Host);
}
(async () => {
  for (const n of [0, -1, 1.5, NaN]) assert.throws(() => checkedBudget(n, 'none', null));
  assert.throws(() => checkedBudget(50, 'fixed', null));
  assert.throws(() => checkedBudget(50, 'none', 60));
  assert.equal(checkedBudget(1000, 'fixed', 72000).start_calls, 1000);

  let proposed;
  state = { hideUntilSaved: true };
  const card = () => React.createElement(TeamProposalCard, { content: '```bob-team-proposal\n' + JSON.stringify(proposal) + '\n```', client, catalog, scope: 'setup', onRun: value => { proposed = value; } });
  await mount(card()); await click('Save team');
  assert.match(text(), /Saved and read back/); assert.match(text(), /runnable/);
  assert.equal(calls.filter(c => c.cmd === 'portal_teams_save_v2').length, 1);
  assert.ok(calls.find(c => c.cmd === 'portal_teams_list_v2'));
  assert.ok(!calls.some(c => /runs_start/.test(c.cmd)), 'save never starts');
  await click('Review run budget'); assert.deepEqual(proposed.budget, proposal.budget);
  await unmount();
  state = { blocked: true, hideUntilSaved: true }; await mount(card()); await click('Save team');
  assert.match(text(), /Saved revision 1 · blocked/); assert.match(text(), /opaque_call_accounting:worker/);
  assert.ok(button('Review run budget').disabled); await unmount(); state = {};

  await mount(panel('first'));
  assert.equal(document.querySelector('[aria-label="Call budget preset"]').value, '100');
  assert.equal(document.querySelector('[aria-label="Wall time mode"]').value, 'none');
  await click('More runs'); assert.match(text(), /other-pc-page-2/);
  assert.equal(calls.find(c => c.cmd === 'portal_runs_list_v2' && c.args.cursor).args.cursor, 'page-2');
  await click('Read from server');
  assert.match(text(), /Requested 100 calls · effective 200 · used 3 \/ 200/);
  assert.match(text(), /effective deadline: No time limit/); assert.match(text(), /Estimated usage/); assert.match(text(), /core_shutdown/);
  await click('Request cancellation'); assert.match(text(), /cancelling/);
  await click('Poll run'); assert.match(text(), /cancelled/);
  await input('Call budget preset', '50'); await click('Start run');
  assert.equal(calls.filter(c => c.cmd === 'portal_runs_start_v2').at(-1).args.budget.start_calls, 50);
  await input('Call budget preset', 'custom'); await input('Custom calls', '75'); await click('Start run');
  assert.equal(calls.filter(c => c.cmd === 'portal_runs_start_v2').at(-1).args.budget.start_calls, 75);
  await input('Custom calls', '1000'); await click('Start run');
  assert.match(text(), /confirmation_required/); assert.match(text(), /needs your OK/); assert.match(text(), /owner_grant_service_unavailable/);
  assert.equal(calls.filter(c => c.cmd === 'portal_runs_start_v2').at(-1).args.budget.start_calls, 1000, 'no clamp');
  assert.ok(!calls.some(c => c.cmd === 'portal_runs_start'), 'no reduced retry');
  await unmount();

  state = { loseStart: true }; await mount(panel('reconnect')); await click('Start run');
  const lost = calls.filter(c => c.cmd === 'portal_runs_start_v2').at(-1).args;
  assert.match(text(), /Previous start outcome unknown/); await unmount();
  await mount(panel('reconnect')); await click('Reconcile start');
  assert.deepEqual(calls.filter(c => c.cmd === 'portal_runs_start_v2').at(-1).args, lost, 'key and exact payload survive remount');
  await unmount();

  state = { readError: true }; await mount(panel('read-fault')); assert.match(text(), /database read failed/); assert.match(text(), /execution_storage_unavailable/); await unmount();
  state = { oldBroker: true }; await mount(panel('old-broker')); assert.match(text(), /Update Bob on the server/); assert.ok(button('Start run').disabled); await unmount();
  state = { oldNative: true }; await mount(panel('old-native')); assert.match(text(), /Update Bob on the server/); assert.ok(button('Start run').disabled); await unmount();
  state = {};
  const oldCatalog = { ...catalog, capabilities: { ...catalog.capabilities, execution_v2: undefined } };
  const mark = calls.length;
  await mount(React.createElement(RunsPanel, { client, catalog: oldCatalog, scope: 'old', runs: [], onRuns: () => {} }));
  assert.match(text(), /Update Bob on the server/);
  assert.ok(!calls.slice(mark).some(c => c.cmd.endsWith('_v2')));
  assert.ok(calls.slice(mark).some(c => c.cmd === 'portal_runs_list'));
  await unmount();
  // The real app/calendar path must use v2 and surface list read errors.
  const eventModule = require.resolve('@tauri-apps/api/event');
  require.cache[eventModule] = { id: eventModule, filename: eventModule, loaded: true, exports: { listen: async () => () => {} } };
  const PortalApp = require('../src/portal/PortalApp.tsx').default;
  const { newEntry, ymd } = require('../src/portal/calendar.ts');
  const entry = { ...newEntry(ymd(new Date()), 'agent'), id: 'manual-calendar', title: 'Review source', start: null,
    agent: { team: team.id, task: proposal.task, max_calls: 100, budget: proposal.budget } };
  const conversation = { id: 'conversation', title: 'Team setup', face_id: null, backend: null, model: null, project_id: null };
  const appCalls = [];
  let calendarReadError = false;
  let calendarLoop = false;
  const invokeApp = async (cmd, args) => {
    appCalls.push({ cmd, args });
    switch (cmd) {
      case 'portal_connection_get': return { connection_configured: true };
      case 'portal_hello': return { identity: { sub: 'test-owner', gateway: 'test-gateway', faces: [{ id: 'assistant', name: 'Assistant' }], may_touch: [], may_not_touch: [] }, gaps: [], server: { version: 'test', boot: 'test' } };
      case 'portal_catalog': return { ...catalog, faces: [{ id: 'assistant', name: 'Assistant' }] };
      case 'portal_conversations': return { items: [conversation] };
      case 'portal_conversation_open': return { conversation, messages: [{ id: 'proposal-message', role: 'assistant', content: '```bob-team-proposal\n' + JSON.stringify(proposal) + '\n```' }], has_more: false };
      case 'portal_conversation_new': return { conversation };
      case 'portal_pins': return { face_id: null, backend: null, model: null, profile: null, locale: null };
      case 'portal_approvals': return { items: [] };
      case 'store_load': return JSON.stringify(args.name === 'calendar' ? [entry] : []);
      case 'store_save': return 'saved';
      case 'portal_runs_list_v2':
        if (calendarReadError) throw JSON.stringify({ code: 'execution_storage_unavailable', detail: 'calendar server read failed' });
        return { items: [], source: 'server', coverage: 'v2', next_cursor: null };
      case 'portal_teams_list_v2': return { items: [{ ...team, topology: calendarLoop ? 'agent-loop/v1' : team.topology }], next_cursor: null };
      case 'portal_runs_start_v2': return detail({ status: 'accepted' });
      default: throw JSON.stringify({ code: 'bad_request', detail: `unexpected ${cmd}` });
    }
  };
  await mount(React.createElement(PortalApp, { invoke: invokeApp, profiles: null, onProfiles: () => {} }));
  await flush(() => document.querySelector('.conversation-list button').click());
  assert.ok(document.querySelector('[aria-label="Team proposal"]'), 'proposal is reviewable in agent thread');
  await click('Tasks'); await click('open today'); await click('Run now');
  const calendarStart = appCalls.find(c => c.cmd === 'portal_runs_start_v2');
  assert.ok(calendarStart); assert.equal(calendarStart.args.budget.start_calls, 100); assert.equal(calendarStart.args.budget.wall_seconds, null);
  assert.ok(!appCalls.some(c => c.cmd === 'portal_runs_start'));
  calendarReadError = true; await click('Agents'); await click('Tasks');
  assert.match(document.querySelector('.tasks-slot [role="alert"]').textContent, /calendar server read failed/, 'calendar read error is visible in Tasks');
  await click('Set up a team…');
  const setupPrompt = document.getElementById('composer').value;
  assert.match(setupPrompt, /Propose agent-loop\/v1 for multi-part work when available, four-step-readonly\/v1 for a quick reviewed answer/);
  assert.match(setupPrompt, /draft acceptance/); assert.match(setupPrompt, /Do not always choose 10/);
  assert.match(setupPrompt, /Available topologies: four-step-readonly\/v1, agent-loop\/v1/);
  calendarLoop = true; await click('Tasks'); await click('open today');
  const beforeLoop = appCalls.filter(c => c.cmd === 'portal_runs_start_v2').length;
  await click('Run now');
  assert.match(text(), /What done looks like/); assert.match(text(), /at least one required/);
  assert.equal(appCalls.filter(c => c.cmd === 'portal_runs_start_v2').length, beforeLoop, 'calendar loop requires acceptance review before dispatch');
  await unmount();
  console.log('durable runs UI: PASS (thread save/readback, blocked, proposal budget, presets/custom/null wall, threshold, paging, receipts, cancel, errors, compatibility, reconnect keys, calendar v2/read errors)');
})().catch(e => { console.error(e); process.exit(1); });
