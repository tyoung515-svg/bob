// S4e: real components/client/journal with supplied server fixtures. Offline.
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'localStorage']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { RunsPanel } = require('../src/portal/DurableRunsPanel.tsx');
const { TeamProposalCard } = require('../src/portal/TeamProposalCard.tsx');
const { ProjectBudget } = require('../src/portal/ProjectBudget.tsx');
const { ProjectsBoard } = require('../src/portal/ProjectsBoard.tsx');
const { checkedAcceptance, featureReason, humanOwner, LOOP, FOUR_STEP, PAUSE_REASONS } = require('../src/portal/durable-runs.ts');
const owner = { sub: 'fixture-owner', token_type: 'human', actor: 'fixture-owner', owner: 'fixture-owner' };
const caps = { contract_versions: [2], topologies: [FOUR_STEP, LOOP], execution_enabled: true,
  growth: { presets: [10, 50, 100, 200], topologies: [LOOP] }, project_pools: true,
  owner_operations: { 'runs.resume': 'human', 'budgets.set': 'human' } };
const catalog = { capabilities: { execution_v2: caps, operations: Object.fromEntries(['owner.status', 'owner.begin', 'owner.confirm', 'runs.resume', 'budgets.set'].map(op => [op, { contract_versions: [2], available: true }])) } };
const acceptance = { criteria: [{ id: 'answer', required: true, output_kind: 'answer', checks: { min_length: 200, max_length: 2000, required_sections: ['Conclusion'], require_citations: true } }] };
const budget = { start_calls: 50, wall_mode: 'follow_budget', wall_seconds: 18000 };
const baseTeam = { schema_version: 1, roles: { manager: 'configured-model', worker: 'configured-model', critic: 'configured-model' }, revision: 2, runnable: true, eligibility: 'runnable', blocked_reasons: [] };
const teams = [{ ...baseTeam, id: 'loop-team', topology: LOOP }, { ...baseTeam, id: 'quick-team', topology: FOUR_STEP }];
const proposal = { definition: { schema_version: 1, id: 'draft-team', topology: LOOP, roles: baseTeam.roles }, expected_revision: null, task: 'Compare evidence and draft a report', evidence_ids: ['S1'], budget, acceptance };
const run = { id: 'loop-run', revision: 5, contract_version: 2, topology: LOOP, team: 'loop-team', task: proposal.task,
  status: 'paused', phase: 'paused', pause_reason: 'budget_exhausted_with_output', owner_flag: true, project_id: 'project-1',
  budget, requested_start_calls: 50, effective_call_limit: 50, calls_used: 50, reserved_calls: 0, unknown_calls: 0,
  estimated_remaining_calls: 1, remaining_calls_basis: 'unmet_required_criteria_proxy_excludes_planning_may_share_one_call', deadline: null,
  accepted_outputs: [{ output_id: 'accepted-1', digest: 'sha-exact', step: 1, step_lineage: ['plan-1', 'step-1'], criteria_ids: ['answer'], check_receipts: { passed: true, section: 'Conclusion' } }],
  growth_events: [{ seq: 2, detail: { old_limit: 50, new_limit: 100, reason: 'no_accepted_output' } }],
  pool_holds: [{ kind: 'project', pool_id: 'project-1', remaining: 25 }] };
let state = {};
const calls = [];
function detail(overrides = {}) { return { run: { ...run, ...state.run, ...overrides }, receipts: [], events: [], outputs: [{ id: 'accepted-1', ordinal: 1, digest: 'sha-exact', content: 'Conclusion: supplied evidence [source:S1]', criteria_ids: ['answer'], check_receipts: { passed: true } }], next_cursor: null, next_receipt_cursor: state.moreReceipts ? 1200 : null }; }
const client = createPortalClient(async (cmd, args) => {
  calls.push({ cmd, args });
  switch (cmd) {
    case 'portal_owner_status_v2': return { owner_session: { active: true, expires_at: Date.now()/1000 + 240, allowed_operations: ['runs.resume', 'budgets.set'] } };
    case 'portal_teams_list_v2': return { items: state.saved ? [state.saved, ...teams] : teams, next_cursor: null };
    case 'portal_teams_save_v2': state.saved = { ...baseTeam, ...args.definition, revision: 3 }; return { team: state.saved, revision: 3, saved: true };
    case 'portal_runs_list_v2': return { items: [{ ...run, ...state.run }], source: 'server', coverage: 'server scope', next_cursor: null };
    case 'portal_run_get_v2': return detail();
    case 'portal_runs_start_v2': return detail({ status: 'accepted' });
    case 'portal_run_resume_v2':
      if (state.loseResume) { state.loseResume = false; throw JSON.stringify({ code: 'timeout_no_reply', detail: 'reply lost' }); }
      if (state.resumeError) throw JSON.stringify({ code: 'gateway_http_409', detail: state.resumeError });
      return detail({ status: 'recovering', pause_reason: null, owner_flag: false, effective_call_limit: args.budget.start_calls });
    case 'portal_budget_set_v2':
      if (state.loseBudget) { state.loseBudget = false; throw JSON.stringify({ code: 'timeout_no_reply', detail: 'reply lost' }); }
      if (state.budgetError) throw JSON.stringify({ code: 'gateway_http_409', detail: state.budgetError });
      return { project_id: args.project_id, call_limit: args.call_limit, revision: (args.expected_revision ?? 0) + 1 };
    case 'portal_projects': return { items: [{ id: 'project-1', name: 'Fixture project', description: '', instructions: '' }] };
    default: throw new Error(`unexpected ${cmd}`);
  }
});
const text = () => document.body.textContent;
const button = label => [...document.querySelectorAll('button')].find(b => b.textContent === label);
async function flush(fn = () => {}) { await act(async () => { await fn(); await new Promise(resolve => setTimeout(resolve, 0)); }); }
async function click(label) { const el = button(label); assert.ok(el, label); assert.ok(!el.disabled, label + ' enabled'); await flush(() => el.click()); }
async function input(label, value) {
  const el = document.querySelector(`[aria-label="${label}"]`); assert.ok(el, label);
  const proto = el.tagName === 'SELECT' ? dom.window.HTMLSelectElement.prototype : el.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  await flush(() => { Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value); el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); });
}
async function check(label) { await flush(() => document.querySelector(`[aria-label="${label}"]`).click()); }
let root;
async function mount(element) { root = createRoot(document.getElementById('root')); await flush(() => root.render(element)); }
async function unmount() { await flush(() => root.unmount()); }
function panel(scope, options = {}) {
  return React.createElement(RunsPanel, { client, catalog, scope, identity: owner, runs: [], onRuns: () => {}, seed: { ...proposal, team: 'loop-team', at: 1 }, ...options });
}
async function open(scope, options) { await mount(panel(scope, options)); await click('Read from server'); }
const budgetEditor = scope => React.createElement(ProjectBudget, { client, catalog, scope, projectId: 'project-1', owner: true });
(async () => {
  assert.equal(humanOwner(owner), true);
  for (const identity of [null, { ...owner, token_type: 'agent' }, { ...owner, token_type: 'portal' }, { ...owner, actor: 'another' }]) assert.equal(humanOwner(identity), false);
  for (const contract of [{ criteria: [] }, { criteria: [{ ...acceptance.criteria[0], required: false }] }, { criteria: [{ ...acceptance.criteria[0], checks: { min_length: 500, max_length: 200 } }] }]) assert.throws(() => checkedAcceptance(contract));
  assert.throws(() => checkedAcceptance({ ...acceptance, weaken_checks: true }));
  assert.throws(() => checkedAcceptance({ criteria: [{ ...acceptance.criteria[0], checks: { shell_command: 'unavailable' } }] }));
  let seed;
  await mount(React.createElement(TeamProposalCard, { client, catalog, scope: 'proposal', content: '```bob-team-proposal\n' + JSON.stringify(proposal) + '\n```', onRun: value => { seed = value; } }));
  assert.equal(document.querySelector('[aria-label="Team approach"]').value, LOOP, 'agent proposes loop for supplied multi-part work');
  assert.match(text(), /Conclusion/); assert.match(text(), /50 calls/);
  await input('Team approach', FOUR_STEP); await click('Save team');
  assert.equal(calls.filter(c => c.cmd === 'portal_teams_save_v2').at(-1).args.definition.topology, FOUR_STEP);
  await click('Review run budget'); assert.equal(seed.budget.start_calls, 50);
  await input('Team approach', LOOP); await click('Save team');
  assert.equal(calls.filter(c => c.cmd === 'portal_teams_save_v2').at(-1).args.expected_revision, 3);
  await click('Review run budget'); assert.deepEqual(seed.acceptance, acceptance); await unmount();
  const quick = { ...proposal, definition: { ...proposal.definition, id: 'quick-draft', topology: FOUR_STEP }, task: 'Quick reviewed answer' };
  await mount(React.createElement(TeamProposalCard, { client, catalog, scope: 'quick-proposal', content: '```bob-team-proposal\n' + JSON.stringify(quick) + '\n```', onRun: () => {} }));
  assert.equal(document.querySelector('[aria-label="Team approach"]').value, FOUR_STEP);
  await input('Team approach', LOOP); assert.equal(document.querySelector('[aria-label="Team approach"]').value, LOOP); await unmount();

  state = {}; await mount(panel('criteria'));
  assert.match(text(), /What done looks like/); assert.match(text(), /200–2000 chars/);
  await input('Criterion 1 kind', 'report'); await input('Criterion 1 sections', 'Conclusion\n');
  assert.equal(document.querySelector('[aria-label="Criterion 1 sections"]').value, 'Conclusion\n', 'a typed newline is retained for the next section');
  assert.ok(button('Start run').disabled); await input('Criterion 1 sections', 'Conclusion\nEvidence');
  await input('Criterion 1 min_length', '300'); await input('Criterion 1 max_length', '3000'); await check('Criterion 1 citations');
  await check('Criterion 1 required'); assert.ok(button('Start run').disabled); assert.match(text(), /at least one required/);
  await check('Criterion 1 required'); await click('Add criterion'); await click('Remove criterion 2'); await click('Start run');
  const start = calls.filter(c => c.cmd === 'portal_runs_start_v2').at(-1).args;
  assert.deepEqual(start.acceptance.criteria[0], { id: 'answer', required: true, output_kind: 'report', checks: { min_length: 300, max_length: 3000, required_sections: ['Conclusion', 'Evidence'], require_citations: false } });
  assert.deepEqual(start.evidence_ids, ['S1']);
  await click('Remove criterion 1'); assert.ok(button('Start run').disabled);
  await input('Run team', 'quick-team'); assert.ok(!button('Start run').disabled, 'four-step remains available');
  await input('Run team', 'loop-team'); assert.ok(button('Start run').disabled); await unmount();

  state = { moreReceipts: true }; await open('details');
  assert.match(text(), /sha-exact/); assert.match(text(), /Conclusion: supplied evidence/); assert.match(text(), /Check receipts.*passed/);
  assert.match(text(), /50 → 100: no accepted output yet/); assert.match(text(), /Rough estimate.*excludes planning/);
  assert.match(text(), /25 calls held/); assert.match(text(), /effective deadline: No time limit/);
  await click('More receipts'); assert.equal(calls.filter(c => c.cmd === 'portal_run_get_v2').at(-1).args.receipt_cursor, 1200);
  await input('Resume call allowance', '201'); assert.ok(button('Resume run').disabled); assert.match(text(), /Update Bob on the server to use grants.prepare/);
  const mark = calls.filter(c => c.cmd === 'portal_run_resume_v2').length; await flush(() => button('Resume run').click());
  assert.equal(calls.filter(c => c.cmd === 'portal_run_resume_v2').length, mark);
  await input('Resume call allowance', '100'); await check('Change resume time limit'); await input('Resume time mode', 'none'); await click('Resume run');
  const resume = calls.filter(c => c.cmd === 'portal_run_resume_v2').at(-1).args;
  assert.deepEqual(resume.budget, { start_calls: 100, wall_mode: 'none', wall_seconds: null }); assert.equal(resume.expected_revision, 5);
  assert.match(text(), /recovering/); await unmount();

  for (const reason of ['call_budget_exhausted', 'budget_exhausted_with_output', 'confirmation_required', 'owner_ceiling', 'pool_shortfall', 'no_progress', 'wall_time_expired']) {
    state = { run: { pause_reason: reason } }; await open('pause-' + reason);
    assert.ok(text().includes(PAUSE_REASONS[reason]), reason); assert.match(text(), /Owner flag/); assert.ok(button('Resume run')); await unmount();
  }
  state = { run: { pause_reason: 'unknown_effect' } }; await open('unknown'); assert.equal(button('Resume run'), undefined); await unmount();
  state = { resumeError: 'revision_conflict' }; await open('conflict'); await input('Resume call allowance', '100'); await click('Resume run');
  assert.match(text(), /This run changed, refresh/); state.run = { revision: 8 }; state.resumeError = null; await click('Refresh run'); await click('Resume run');
  assert.equal(calls.filter(c => c.cmd === 'portal_run_resume_v2').at(-1).args.expected_revision, 8); await unmount();
  state = { loseResume: true }; await open('resume-reconnect'); await input('Resume call allowance', '100'); await click('Resume run');
  const lost = calls.filter(c => c.cmd === 'portal_run_resume_v2').at(-1).args; await unmount();
  state = { run: { revision: 9 } }; await open('resume-reconnect'); await click('Reconcile resume');
  assert.deepEqual(calls.filter(c => c.cmd === 'portal_run_resume_v2').at(-1).args, lost); await unmount();
  state = { resumeError: 'not_found' }; await open('denied'); await input('Resume call allowance', '100'); await click('Resume run'); assert.match(text(), /not_found/); await unmount();
  state = { resumeError: 'client_upgrade_required' }; await open('resume-unadvertised'); await input('Resume call allowance', '100'); await click('Resume run');
  assert.ok(button('Resume run').disabled); assert.ok(!button('Poll run').disabled); assert.ok(!button('Start run').disabled); await unmount();
  state = {}; await open('extend-time'); await input('Resume call allowance', '100'); await check('Change resume time limit'); await input('Resume seconds', '7200'); await click('Resume run');
  assert.deepEqual(calls.filter(c => c.cmd === 'portal_run_resume_v2').at(-1).args.budget, { start_calls: 100, wall_mode: 'fixed', wall_seconds: 7200 }); await unmount();
  for (const token_type of ['agent', 'portal']) {
    state = {}; await open('identity-' + token_type, { identity: { ...owner, token_type } });
    assert.equal(button('Resume run'), undefined); assert.equal(button('Set project budget'), undefined); await unmount();
  }

  state = {}; await mount(budgetEditor('project'));
  assert.equal(document.querySelector('[aria-label="Project budget mode"]').value, 'none'); assert.ok(button('Set project budget').disabled);
  assert.match(text(), /Existing server limits and usage are unknown/); assert.ok(button('Sprint budgets').disabled); assert.match(text(), /Needs sprints \(coming soon\)/);
  await input('Project budget mode', 'limit'); await input('Project call limit', '500'); await click('Set project budget');
  assert.match(text(), /Used: not reported · held: not reported · limit: 500/);
  let set = calls.filter(c => c.cmd === 'portal_budget_set_v2').at(-1).args; assert.equal(set.expected_revision, null); assert.equal(set.call_limit, 500);
  state.budgetError = 'below_committed_budget'; await input('Project call limit', '1'); await click('Set project budget');
  assert.match(text(), /below calls already used and held/); assert.equal(calls.filter(c => c.cmd === 'portal_budget_set_v2').at(-1).args.expected_revision, 1);
  await unmount();
  state = { loseBudget: true }; await mount(budgetEditor('project-reconnect')); await input('Project budget mode', 'limit'); await click('Set project budget');
  const lostBudget = calls.filter(c => c.cmd === 'portal_budget_set_v2').at(-1).args; await unmount();
  await mount(budgetEditor('project-reconnect')); await click('Reconcile project budget');
  assert.deepEqual(calls.filter(c => c.cmd === 'portal_budget_set_v2').at(-1).args, lostBudget); await unmount();
  await mount(React.createElement(ProjectsBoard, { client, catalog, identity: owner, scope: 'board', store: { load: async () => ({ order: [], columns: {} }), save: async () => {} }, conversations: [], openConversationId: null, onPickUp: () => {}, onAskPm: () => {}, onOpenSession: () => {}, onAssigned: () => {} }));
  await flush(() => document.querySelector('.card-main').click()); assert.ok(document.querySelector('[aria-label="Project call budget"]')); await unmount();

  const oldCatalog = { capabilities: { execution_v2: { contract_versions: [2], execution_enabled: true, topologies: [FOUR_STEP] } } };
  state = {}; await open('old-owner-ops', { catalog: oldCatalog, seed: { ...proposal, team: 'quick-team', at: 1 } });
  assert.ok(button('Resume run').disabled); assert.ok(button('Set project budget').disabled);
  assert.match(text(), /Update Bob on the server to use runs.resume/); assert.match(text(), /Update Bob on the server to use budgets.set/);
  assert.ok(!button('Start run').disabled); assert.ok(!button('Poll run').disabled); assert.ok(!button('Request cancellation').disabled);
  assert.ok(document.querySelector('[aria-label="Run team"] option[value="loop-team"]').disabled); await unmount();
  assert.match(featureReason(oldCatalog, 'growth'), /Update Bob on the server/);
  console.log('loop runs UI: PASS (proposals/switching, editable acceptance, output/growth receipts, seven pauses, owner gates, resume/200/conflicts/reconnect, project budgets/none/below-committed/board, old-server isolation)');
})().catch(error => { console.error(error); process.exit(1); });
