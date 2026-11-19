"use strict";
// Workspace UI harness over the REAL mounted app.
//
// Mounts the product PortalApp in jsdom with a scripted native `invoke` (the
// exact boundary to Tauri: command name + args), opens the Workspace tab and
// drives the owner workflow: project/role from the server registry, start,
// live status, Stop, proposal diff and evidence bound to its digest, approve,
// deny, stale refusal, resend-with-same-key after an unknown outcome, reopen
// from the job list, and Chat state surviving a tab switch.
//
// No Tauri, no server, no model call: payload shapes are asserted exactly.
// Run from shell/:  node -r ./harness/workbench-ui-register.cjs harness/workspace-ui.cjs

const assert = require("node:assert/strict");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: "http://localhost" });
for (const name of ["window", "document", "HTMLElement", "Event", "MouseEvent", "KeyboardEvent"]) {
  global[name] = dom.window[name];
}
global.IS_REACT_ACT_ENVIRONMENT = true;

const eventModule = require.resolve("@tauri-apps/api/event", { paths: [path.resolve(__dirname, "..")] });
require.cache[eventModule] = {
  id: eventModule, filename: eventModule, loaded: true, children: [], paths: [],
  exports: { listen: async () => () => {} },
};

const React = require("react");
const { act } = React;
const { createRoot } = require("react-dom/client");

const CONVERSATION = { id: "c1", title: "First conversation", face_id: null, backend: null, model: null, project_id: null };
const HELLO = {
  identity: { token_type: "human", actor: "dad", owner: "dad", sub: "dad", gateway: "http://127.0.0.1:7836", faces: [], may_touch: [], may_not_touch: [], expires_at: "2027-01-01T00:00:00Z" },
  server: { boot: "boot", version: "test" },
  gaps: [],
};
const OPERATIONS = Object.fromEntries([...['attachments','list','start','audit','cancel','proposal','decide','artifact'].map(op => 'jobs.' + op), 'teams.list', 'owner.status', 'owner.begin', 'owner.confirm'].map(op => [op, { contract_versions: [2], implemented: true, available: true }]));
const APPLY_REASON = 'managed_apply_unavailable_until_4.3e';
const JOB_CAPS = { protocols: ['tool-jobs/2'], operations: OPERATIONS, roles: ['coder','reviewer','proposer','researcher'], profile_revisions: { proposal: 'd'.repeat(64), checked: 'd'.repeat(64) }, coder: { available: true, reason: null, worker_contract: 'checked-edits/1' }, checks: { available: true, reason: null, selection: 'frozen_attachment_registration' }, acceptance_revision: 'checked-edits/1', apply: { available: false, reason: APPLY_REASON } };
const TEAM = { id: 'saved-fixture-team', revision: 1, runnable: true, eligibility: 'runnable', topology: 'agent-loop/v1' };
const CATALOG = { faces: [], models: [], backends: [], capabilities: { faces: 0, backends: 0, actions: [], warnings: [], operations: OPERATIONS } };
const JOB = "a".repeat(32);
const JOB2 = "b".repeat(32);
const DIGEST = "d".repeat(64);
const PRE = "1".repeat(64);
const POST = "2".repeat(64);
const AUTHORITY = { principal: "agent:dad:jti", project: "bob-portal", grants: ["project_write"], depth: 0, expires_at: 1 };
const RECEIPTS = (over) => ({
  model_attempts: 3, model_states: { returned: 3 }, usage: { total_tokens: 1234 }, reported_models: ["deepseek-v4-flash"],
  tool_calls: 2, tool_failures: 0,
  tools: [{ name: "project_write", ok: true, error: "" }, { name: "run_check", ok: true, error: "" }],
  check_runs: [],
  checks_passed: false, caveats: [], ...over,
});
function auditRow(id, status, over = {}) {
  const terminal = !["accepted", "queued", "dispatch_intent", "running", "checking", "cancelling", "unknown"].includes(status);
  return {
    run_id: "run-" + id, revision: "9007199254740993", base_digest: PRE, output_digest: POST, execution_id: null, queue: null, flags: [], next_cursor: null,
    id, status, operation_id: "op-" + id.slice(0, 4), authority: AUTHORITY, role: "coder", model_pin: "deepseek-v4-flash",
    backend: "deepseek_v4_flash", terminal, task: "Fix the activity count", attachment: "bob-portal",
    principal: { kind: "agent" }, created_at: 1793380000, error: null,
    content: terminal ? "Changed the counter; tsc passed." : "", completion: terminal ? "model_output" : null,
    artifacts: [], evidence: {}, receipts: RECEIPTS(terminal ? {} : { model_attempts: 1, tool_calls: 0, tools: [], check_runs: [] }),
    history: [{ at: 1793380000, status: "accepted", detail: "authority_admitted" }, { at: 1793380001, status, detail: "worker_started" }],
    proposal: terminal && status === "completed" ? { digest: DIGEST, file: "proposal-x.json", files: [], diff_truncated: false } : null,
    decision: null, ...over,
  };
}
const PROPOSAL = (over = {}) => ({
  job_id: JOB, digest: DIGEST, base_digest: PRE, output_digest: POST, revision: "9007199254740993", output_manifest: [], independently_verified: false, apply_reason: APPLY_REASON,
  files: [{ path: "src/portal/PortalApp.tsx", pre_sha256: PRE, post_sha256: POST, bytes: 10, created: false, operation: "replace" }],
  diff: "--- a/src/portal/PortalApp.tsx\n+++ b/src/portal/PortalApp.tsx\n@@ -1 +1 @@\n-old count\n+new count\n",
  diff_truncated: false, check_runs: [], checks_passed: false, stale_paths: [], applicable: false, decision: null, ...over,
});

function makeInvoke(state) {
  const calls = [];
  const invoke = async (cmd, args) => {
    calls.push({ cmd, args });
    switch (cmd) {
      case "portal_connection_get": return { connection_configured: true };
      case "portal_hello": return { ...HELLO, operations: (state.catalog || CATALOG).capabilities.operations, ...state.hello };
      case "portal_catalog": return state.catalog || CATALOG;
      case "portal_teams_list_v2": return { items: state.teams || [TEAM], next_cursor: null };
      case "portal_owner_status_v2": return { owner_session: { active: !state.needsProof, expires_at: 9999999999, allowed_operations: ['jobs.decide'] } };
      case "portal_owner_begin_v2": return { challenge_id: 'job-challenge', summary: { operation: 'jobs.decide', resource: args.resource }, expires_at: 9999999999 };
      case "portal_owner_confirm_v2": state.needsProof = false; return { owner_session: { active: true, allowed_operations: ['jobs.decide'] } };
      case "portal_conversations": return { items: [CONVERSATION], limit: 50, offset: 0, excluded: 0 };
      case "portal_conversation_open": return { conversation: CONVERSATION, messages: [], has_more: false };
      case "portal_pins": return { face_id: null, model: null, backend: null };
      case "portal_jobs_attachments_v2":
        if (state.attachmentFault) throw JSON.stringify(state.attachmentFault);
        return { ...state.attachments, items: state.attachments.items.map(a => ({ project_id: a.id, registration_revision: DIGEST, coverage_target: { workspace: 'projects', cwd: a.id, attachment_id: a.id }, ...a })), capabilities: JOB_CAPS };
      case "portal_jobs_list_v2":
        return { items: state.jobs, total: state.jobs.length, truncated: false, total_scope: "page", next_cursor: null };
      case "portal_jobs_start_v2": {
        if (state.startFaults > 0) {
          state.startFaults -= 1;
          const f = state.startFault || { code: "timeout_no_reply", detail: "no reply before the deadline", hint: "outcome unknown" };
          throw JSON.stringify({ code: f.code, detail: f.detail, hint: f.hint || "" });
        }
        state.audits[JOB] = [auditRow(JOB, "running"), auditRow(JOB, "completed")];
        state.jobs = [{ id: JOB, status: "running", role: "coder", task: args.task, attachment: "bob-portal", created_at: 1793380000, terminal: false, proposal: null, decision: null }];
        return auditRow(JOB, "accepted");
      }
      case "portal_jobs_audit_v2": {
        state.auditCalls = (state.auditCalls || 0) + 1;
        if (state.auditFailAt && state.auditCalls === state.auditFailAt) {
          throw JSON.stringify({ code: "timeout_no_reply", detail: "audit read timed out", hint: "the link dropped once" });
        }
        if (state.foreignJob === args.job_id) throw JSON.stringify({ code: "not_found", detail: "not_found" });
        const queue = state.audits[args.job_id];
        assert.ok(queue && queue.length, `audit for ${args.job_id}`);
        return queue.length > 1 ? queue.shift() : queue[0];
      }
      case "portal_jobs_proposal_v2":
        return state.proposals[args.job_id];
      case "portal_jobs_artifact_v2":
        // Deferred: the test decides when (and how) each read answers.
        if (state.deferred) return new Promise((resolve, reject) => state.deferred.push({ args, resolve, reject }));
        return artifactChunk(args.filename, `${args.job_id}:${args.filename}`);
      case "portal_jobs_cancel_v2":
        state.audits[args.job_id] = state.cancelQueue || [auditRow(args.job_id, "cancelled", { error: "cancelled_before_runtime_receipt" })];
        return auditRow(args.job_id, state.cancelQueue ? "cancelling" : "cancelled");
      case "portal_jobs_decide_v2": {
        // Scripted write faults: {code, detail, hint, land} — `land` applies the
        // decision server-side and still loses the reply (unknown outcome).
        const fault = state.decideFaults && state.decideFaults.length ? state.decideFaults.shift() : null;
        if (fault && !fault.land) throw JSON.stringify({ code: fault.code, detail: fault.detail, hint: fault.hint || "" });
        const state_ = args.decision === "approve" ? "approved" : "denied";
        const echoedKey = state.decideEchoKey === undefined ? args.request_key : state.decideEchoKey;
        const decision = { id: "decision-1", applied: false, apply_reason: APPLY_REASON, decision: args.decision, state: state_, proposal_digest: args.proposal_digest, key: echoedKey, at: 1793380100,
        }; // An owner decision is never an apply receipt.
        state.audits[args.job_id] = [auditRow(args.job_id, "completed", { decision })];
        state.proposals[args.job_id] = PROPOSAL({ decision, ...(state_ === "approved" ? { stale_paths: ["src/portal/PortalApp.tsx"], applicable: false } : {}) });
        if (fault) throw JSON.stringify({ code: fault.code, detail: fault.detail, hint: fault.hint || "" });
        return { decision, replayed: false };
      }
      default:
        throw JSON.stringify({ code: "bad_request", detail: `unexpected ${cmd}`, hint: "harness" });
    }
  };
  return { invoke, calls };
}

const byText = (selector, text) => [...document.querySelectorAll(selector)].find((n) => (n.textContent || "").trim() === text);
// Never hand a live DOM node to node:assert as `actual`: on failure the
// AssertionError inspects it deeply, walking jsdom + React fibers. In this
// mounted app that grew past 50 GB RSS and the OOM killer took the session
// (evidence/repair/opus/DIAG-*). Assert presence/identity as booleans instead.
const absent = (selector, text) => byText(selector, text) === undefined;
async function flush(fn) {
  await act(async () => { if (fn) await fn(); await Promise.resolve(); });
}
async function settle(ms = 20) {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });
}
async function click(node) {
  assert.ok(!!(node), "clickable node exists");
  assert.equal(node.disabled, false, `${node.textContent} is enabled`);
  await flush(() => node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })));
}
async function fill(element, value) {
  const proto = element.tagName === "TEXTAREA" ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  await flush(() => {
    Object.getOwnPropertyDescriptor(proto, "value").set.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function choose(select, value) {
  await flush(() => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, "value").set.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
async function submit(form) {
  await flush(() => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
}
async function until(predicate, label, ms = 4000) {
  const end = Date.now() + ms;
  while (!predicate()) {
    if (Date.now() > end) throw new Error(`timed out: ${label}`);
    await settle(50);
  }
}
const workspace = () => document.querySelector('section[aria-label="Workspace"]');
const artifactChunk = (filename, content, extra = {}) => ({ filename, content: Buffer.from(content).toString('base64'), encoding: 'base64', sha256: DIGEST, offset: 0, total_bytes: Buffer.byteLength(content), truncated: false, next_offset: null, ...extra });
const pass = (name) => console.log(`PASS: ${name}`);

let PortalApp;
async function mount(state) {
  const container = document.getElementById("root");
  const root = createRoot(container);
  const client = makeInvoke(state);
  await flush(() => root.render(React.createElement(PortalApp, { invoke: client.invoke, profiles: null, onProfiles: () => {} })));
  await settle();
  return { root, calls: client.calls };
}

(async () => {
  PortalApp = require("../src/portal/PortalApp.tsx").default;
  const ATTACHMENTS = { items: [{ id: "bob-portal", label: "Bob portal shell", roles: ["coder", "reviewer"], checks: ["tsc"], write_paths: null, sandbox_registered: true }], research: false };

  // ---- 1: tab, registry-driven start, live status, proposal, approve ----
  const state = { attachments: ATTACHMENTS, jobs: [], audits: {}, proposals: { [JOB]: PROPOSAL() }, startFaults: 1 };
  const app = await mount(state);
  const chatTab = byText('[role="tab"]', "Agents");
  const workspaceTab = byText('[role="tab"]', "Workspace");
  assert.ok(!!(chatTab && workspaceTab), "Chat and Workspace are sibling tabs in the real app");
  assert.equal(chatTab.getAttribute("aria-selected"), "true");
  assert.ok(workspace() === null, "Workspace is not mounted before it is opened");
  const draft = document.querySelector(".portal-body #composer");
  assert.ok(!!(draft), "the chat composer exists");
  await fill(draft, "half-written chat draft");
  await click(workspaceTab);
  await settle();
  assert.ok(!!(workspace()), "the Workspace mounts inside the existing app");
  // Workspace v2 (2026-11-02): the agent thread stays visible as the IDE's chat column.
  assert.ok(!!(document.querySelector(".portal-body.mode-workspace")), "the body switches to the Workspace layout, not unmounted");
  assert.ok(app.calls.some((c) => c.cmd === "portal_jobs_attachments_v2"), "projects come from the server registry");
  const project = document.getElementById("workspace-project");
  assert.equal(project.value, "bob-portal");
  assert.ok(project.textContent.includes("Bob portal shell"));
  const role = document.getElementById("workspace-role");
  assert.deepEqual([...role.options].map((o) => o.value), ["coder", "reviewer"], "roles are the project's registered roles");
  assert.ok(workspace().textContent.includes("checks: tsc"), "registered checks are shown");
  pass("Workspace is a sibling tab fed by the server registry");

  // C8: a fresh form defaults to the written four-call budget (README approval
  // brief, native and core defaults); larger budgets are an explicit choice.
  const budgetSelect = document.getElementById("workspace-budget");
  const shownBudget = budgetSelect.options[budgetSelect.selectedIndex].textContent;
  assert.equal(shownBudget, "4 model calls, 60 s root wall", "the fresh form shows the four-call budget");

  await fill(document.getElementById("workspace-task"), "Fix the activity count");
  await submit(document.getElementById("workspace-task").closest("form"));
  await settle();
  const firstStart = app.calls.filter((c) => c.cmd === "portal_jobs_start_v2");
  assert.equal(firstStart.length, 1);
  const key = firstStart[0].args.request_key;
  assert.deepEqual(firstStart[0].args, { contract_version: 2, role: "coder", task: "Fix the activity count", attachment: "bob-portal", request_key: key, team: TEAM.id, team_revision: 1, project_id: 'bob-portal', worker_contract: 'checked-edits/1', acceptance: { criteria: [{ id: 'checked-proposal', required: true, output_kind: 'proposal', checks: { required_check_ids: ['tsc'] } }] }, budget: { start_calls: 4, wall_mode: 'fixed', wall_seconds: 60, max_job_attempts: '16', job_seconds_budget: '14400' }, job_policy: { proposed_seconds: '900', rationale: 'Registered checks and exact file edits', auto_escalate: false }, job_coverage: { profiles: { checked: JOB_CAPS.profile_revisions.checked }, workspaces: { projects: ['bob-portal'] }, attachments: ['bob-portal'], max_job_attempts: '16', job_seconds_budget: '14400', allow_cli: false } },
    "start sends only contract keys and the default four-call budget; no owner, path or principal");
  pass("a fresh form starts with the written four-call budget (C8)");
  assert.ok(/^ws-[A-Za-z0-9]{8,}$/.test(key));
  assert.ok(workspace().textContent.includes("timeout_no_reply"), "unknown start outcome is shown verbatim");
  const resend = byText("button", "Resend same request");
  assert.ok(!!(resend), "resend is offered after an unknown outcome");
  await click(resend);
  await settle();
  const starts = app.calls.filter((c) => c.cmd === "portal_jobs_start_v2");
  assert.equal(starts.length, 2);
  assert.equal(starts[1].args.request_key, key, "resend reuses the SAME request key (server dedupes)");
  pass("unknown start outcome resends with the same key");

  await until(() => workspace().textContent.includes("Status: running"), "running status");
  assert.ok(!!(byText("button", "Stop")), "a running job offers Stop");
  assert.ok(workspace().textContent.includes("records model and tool receipts when the job ends"),
    "a running job does not show zero receipts as if nothing were in flight");
  assert.ok(!workspace().textContent.includes("Model attempts reserved: 1"), "no partial count is presented");
  await until(() => workspace().textContent.includes("Status: completed"), "polled to completed");
  assert.ok(absent("button", "Stop"), "a finished job has no Stop");
  await until(() => document.querySelector('[aria-label="Proposed diff"]'), "proposal shown");
  const text = workspace().textContent;
  assert.ok(text.includes("Changed the counter; tsc passed."), "the model result is shown");
  assert.ok(text.includes("not independent verification"));
  assert.ok(text.includes("Model attempts reserved: 3") && text.includes("1234 tokens"), "receipt counts from the server");
  assert.ok(text.includes("Tool calls: 2 (0 failed)"));
  assert.ok(text.includes("No check ran on these exact bytes."), "proposal-only capability does not claim executed checks");
  assert.ok(text.includes(`digest ${DIGEST.slice(0, 12)}`));
  const diff = document.querySelector('[aria-label="Proposed diff"]');
  assert.equal(diff.querySelector(".del").textContent, "-old count\n");
  assert.equal(diff.querySelector(".add").textContent, "+new count\n");
  pass("live status, result, receipts and proposal diff render");

  await click(byText("button", "Approve"));
  await settle();
  const decide = app.calls.filter((c) => c.cmd === "portal_jobs_decide_v2");
  assert.equal(decide.length, 1);
  assert.deepEqual(Object.keys(decide[0].args).sort(), ["contract_version", "decision", "expected_revision", "job_id", "proposal_digest", "request_key"]);
  assert.equal(decide[0].args.decision, "approve");
  assert.equal(decide[0].args.proposal_digest, DIGEST, "approval binds the exact digest shown");
  await until(() => workspace().textContent.includes("Decision: approved"), "approved decision");
  assert.ok(workspace().textContent.includes(APPLY_REASON) && workspace().textContent.includes("applied: false"), "decision read-back states apply is unavailable with the server reason");
  assert.ok(!workspace().textContent.includes("approval would be refused"),
    "after a decision the now-changed files are not reported as stale");
  assert.ok(absent("button", "Approve"), "no second decision is offered");
  pass("approve sends the bound digest and shows the recorded decision read-back");

  await click(chatTab);
  assert.ok(!document.querySelector(".portal-body.mode-workspace"), "back to the Agents layout");
  assert.ok(document.querySelector(".portal-body #composer") === draft, "the same composer element is still mounted");
  assert.equal(draft.value, "half-written chat draft", "the chat draft survived the tab switch");
  pass("Chat state survives switching to the Workspace and back");
  await flush(() => app.root.unmount());

  // ---- 2: reopen from list, stale proposal, deny ----------------------
  const state2 = {
    attachments: ATTACHMENTS,
    jobs: [{ id: JOB2, status: "completed", role: "coder", task: "Older job", attachment: "bob-portal", created_at: 1793370000, terminal: true, proposal: { digest: DIGEST, files: 1 }, decision: null }],
    audits: { [JOB2]: [auditRow(JOB2, "completed")] },
    proposals: { [JOB2]: PROPOSAL({ job_id: JOB2, stale_paths: ["src/portal/PortalApp.tsx"], applicable: false }) },
    startFaults: 0,
  };
  const reopened = await mount(state2);
  await click(byText('[role="tab"]', "Workspace"));
  await settle();
  const listed = [...document.querySelectorAll('[aria-label="Jobs"] button')];
  assert.equal(listed.length, 1);
  assert.ok(listed[0].textContent.includes("Older job") && listed[0].textContent.includes("proposal (1 file)"));
  await click(listed[0]);
  await until(() => document.querySelector('[aria-label="Proposed diff"]'), "reopened proposal");
  assert.ok(workspace().textContent.includes("approval would be refused"), "stale files are named");
  assert.equal(byText("button", "Approve").disabled, true, "approve is disabled for a stale proposal");
  await click(byText("button", "Deny"));
  await settle();
  const deny = reopened.calls.find((c) => c.cmd === "portal_jobs_decide_v2");
  assert.equal(deny.args.decision, "deny");
  await until(() => workspace().textContent.includes("Decision: denied"), "denied decision");
  pass("reopened job shows a stale proposal, refuses approve, records deny");
  await flush(() => reopened.root.unmount());

  // ---- 3: Stop a running job ------------------------------------------
  const state3 = {
    attachments: ATTACHMENTS,
    jobs: [{ id: JOB, status: "running", role: "coder", task: "Long job", attachment: "bob-portal", created_at: 1, terminal: false, proposal: null, decision: null }],
    audits: { [JOB]: [auditRow(JOB, "running")] }, proposals: {}, startFaults: 0,
  };
  const stopping = await mount(state3);
  await click(byText('[role="tab"]', "Workspace"));
  await settle();
  await click(document.querySelector('[aria-label="Jobs"] button'));
  await until(() => byText("button", "Stop"), "Stop offered");
  await click(byText("button", "Stop"));
  await settle();
  assert.deepEqual(stopping.calls.filter((c) => c.cmd === "portal_jobs_cancel_v2").map((c) => c.args), [{ contract_version: 2, job_id: JOB, request_key: stopping.calls.find(c => c.cmd === "portal_jobs_cancel_v2").args.request_key }]);
  await until(() => workspace().textContent.includes("Status: cancelled"), "cancelled after server confirmation");
  assert.ok(absent("button", "Stop"), "a cancelled job has no Stop");
  pass("Stop calls cancel and shows the server-confirmed cancelled state");
  await flush(() => stopping.root.unmount());

  // ---- 4: nothing registered -------------------------------------------
  const empty = await mount({ attachments: { items: [], research: false }, jobs: [], audits: {}, proposals: {}, startFaults: 0 });
  await click(byText('[role="tab"]', "Workspace"));
  await settle();
  assert.ok(workspace().textContent.includes("registered no project for this identity"));
  assert.equal(byText("button", "Start job").disabled, true, "no start without a registered project");
  assert.ok(document.querySelector('[aria-label="Workspace gaps"]').textContent.includes("no pushed event stream"));
  pass("no registered project is stated honestly and Start stays disabled");
  await flush(() => empty.root.unmount());

  // ---- C3: ONE failed audit read must not stop live polling for good ----
  {
    const state3 = { attachments: ATTACHMENTS, jobs: [], audits: {}, proposals: {}, startFaults: 0, auditFailAt: 2 };
    const app3 = await mount(state3);
    await click(byText('[role="tab"]', "Workspace"));
    await settle();
    await fill(document.getElementById("workspace-task"), "Fix the activity count");
    await submit(document.getElementById("workspace-task").closest("form"));
    await settle();
    await until(() => workspace().textContent.includes("Status: running"), "running status before the failed read");
    const before = app3.calls.filter((c) => c.cmd === "portal_jobs_audit_v2").length;
    assert.ok(before >= 1, "the first audit read was attempted");
    // The second audit read (state.auditFailAt) rejects; polling must survive it.
    await until(() => workspace().textContent.includes("Status: completed"),
      "polling continued after one failed audit read", 8000);
    const after = app3.calls.filter((c) => c.cmd === "portal_jobs_audit_v2").length;
    assert.ok(after > 2, `audit reads continued after the failure (got ${after})`);
    pass("one failed audit read does not stop polling");
    await flush(() => app3.root.unmount());
  }

  // ---- C4: an uncertain start keeps its exact original tuple ----------
  {
    const TWO = { items: [ATTACHMENTS.items[0], { id: "bob-site", label: "Bob site", roles: ["coder", "reviewer"], checks: ["tsc"], write_paths: null, sandbox_registered: true }], research: false };
    const state4 = { attachments: TWO, jobs: [], audits: {}, proposals: { [JOB]: PROPOSAL() }, startFaults: 2 };
    const app4 = await mount(state4);
    const starts = () => app4.calls.filter((c) => c.cmd === "portal_jobs_start_v2").map((c) => c.args);
    await click(byText('[role="tab"]', "Workspace"));
    await settle();
    const form = document.getElementById("workspace-task").closest("form");
    await fill(document.getElementById("workspace-task"), "Fix the activity count");
    await submit(form);
    await settle();
    assert.equal(starts().length, 1, "the uncertain start was attempted once");
    const original = { ...starts()[0] };
    assert.equal(original.role, "coder");
    assert.equal(original.attachment, "bob-portal");
    assert.ok(!!(byText("button", "Resend same request")), "an unchanged form offers the same-request resend");

    // A role change, then a project change: neither may be sent under the old key.
    await choose(document.getElementById("workspace-role"), "reviewer");
    assert.ok(absent("button", "Resend same request"), "after a role change the form no longer promises the same request");
    await choose(document.getElementById("workspace-project"), "bob-site");
    assert.ok(absent("button", "Resend same request"), "after a project change the form no longer promises the same request");
    assert.ok(!!(byText("button", "Start as a new request")), "the changed form is labelled an explicit new request");
    assert.ok(workspace().textContent.includes("may start a second job"), "a new request warns about the unresolved original");
    const pending = [...document.querySelectorAll('[aria-label="Unconfirmed starts"] li')];
    assert.equal(pending.length, 1, "the unresolved original stays listed while the form is edited");
    assert.ok(pending[0].textContent.includes("coder") && pending[0].textContent.includes("Bob portal shell") &&
      pending[0].textContent.includes("Fix the activity count") && pending[0].textContent.includes("4 model"),
      "the listed original shows its own role, project, task and budget, not the edited form's");

    await submit(form);
    await settle();
    assert.equal(starts().length, 2, "the explicit new request was submitted");
    assert.equal(starts()[1].role, "reviewer");
    assert.equal(starts()[1].attachment, "bob-site");
    assert.notEqual(starts()[1].request_key, original.request_key, "a changed tuple is sent with a NEW key");
    assert.deepEqual(original, starts()[0], "the original payload record was not mutated");

    // The inspection/retry route resends the ORIGINAL tuple exactly, whatever the form says.
    const listed = [...document.querySelectorAll('[aria-label="Unconfirmed starts"] li')];
    assert.equal(listed.length, 2, "both unconfirmed starts are listed");
    const resendOriginal = [...listed[0].querySelectorAll("button")].find((b) => b.textContent.trim() === "Resend this exact request");
    await click(resendOriginal);
    await settle();
    assert.equal(starts().length, 3);
    assert.deepEqual(starts()[2], original, "the original is resent with its exact role, project, task, budget and key");
    await until(() => workspace().textContent.includes("Status: running") || workspace().textContent.includes("Status: completed"), "resent original opened");
    const left = [...document.querySelectorAll('[aria-label="Unconfirmed starts"] li')];
    assert.equal(left.length, 1, "the confirmed original leaves the list; the other stays unresolved");
    assert.ok(left[0].textContent.includes("reviewer") && left[0].textContent.includes("Bob site"));
    pass("an uncertain start keeps its exact tuple; edited fields are an explicit new request (C4)");
    await flush(() => app4.root.unmount());
  }

  // ---- C5: a delayed artifact answer belongs to ITS job and request ----
  {
    const art = (tag) => [{ name: `notes-${tag}`, file: `report-${tag === "a" ? JOB : JOB2}.json`, sha256: "7".repeat(64) }];
    const summary = (id, task) => ({ id, status: "completed", role: "coder", task, attachment: "bob-portal", created_at: 1, terminal: true, proposal: null, decision: null });
    const state5 = {
      attachments: ATTACHMENTS,
      jobs: [summary(JOB, "Job A"), summary(JOB2, "Job B")],
      audits: {
        [JOB]: [auditRow(JOB, "completed", { task: "Job A", artifacts: art("a"), proposal: null })],
        [JOB2]: [auditRow(JOB2, "completed", { task: "Job B", artifacts: art("b"), proposal: null })],
      },
      proposals: {}, startFaults: 0, deferred: [],
    };
    const app5 = await mount(state5);
    await click(byText('[role="tab"]', "Workspace"));
    await settle();
    const openJob = async (label) => {
      await click([...document.querySelectorAll('[aria-label="Jobs"] button')].find((b) => b.textContent.includes(label)));
      await until(() => (document.querySelector('article[aria-label="Job"] h2')?.textContent || "") === label, `${label} open`);
    };
    const shown = () => document.querySelector('[aria-label="Artifact text"]')?.textContent || "";
    const resolveRead = async (index, content) => { await flush(() => state5.deferred[index].resolve(artifactChunk(state5.deferred[index].args.filename, content))); await settle(); };
    const rejectRead = async (index, code) => { await flush(() => state5.deferred[index].reject(JSON.stringify({ code, detail: "artifact read failed", hint: "" }))); await settle(); };

    // A's read answers after B is open: B must not show A's name or bytes.
    await openJob("Job A");
    await click(byText("button", "notes-a"));
    await openJob("Job B");
    await resolveRead(0, "A-CONTENT");
    assert.ok(!workspace().textContent.includes("A-CONTENT") && !shown().includes("notes-a"), "A's late artifact is not rendered under B");

    // B's read FAILS after A is open: A must not show B's error.
    await click(byText("button", "notes-b"));
    await openJob("Job A");
    await rejectRead(1, "artifact_read_failed");
    assert.ok(!workspace().textContent.includes("artifact_read_failed"), "B's late artifact error is not rendered under A");

    // A -> B -> A: the superseded read of A must not land in the re-opened A.
    await click(byText("button", "notes-a"));
    await openJob("Job B");
    await openJob("Job A");
    await resolveRead(2, "A-STALE");
    assert.ok(!workspace().textContent.includes("A-STALE"), "a superseded read is ignored even when the same job is open again");

    // A fresh read of the open job is shown, with its provenance.
    await click(byText("button", "notes-a"));
    await resolveRead(3, "A-FRESH");
    assert.ok(shown().includes("notes-a") && shown().includes("A-FRESH"), "the current read of the open job is shown");
    assert.equal(state5.deferred.length, 4, "every click issued exactly one read");
    pass("artifact success and errors are bound to job + request generation, incl. A->B->A (C5)");
    await flush(() => app5.root.unmount());
  }

  // ---- C6: a decision's refusal / unknown outcome survives read-back ----
  {
    const summary = (id, task, proposal) => ({ id, status: "completed", role: "coder", task, attachment: "bob-portal", created_at: 1, terminal: true, proposal, decision: null });
    const state6 = {
      attachments: ATTACHMENTS,
      jobs: [summary(JOB, "Job A", { digest: DIGEST, files: 1 }), summary(JOB2, "Job B", null)],
      audits: { [JOB]: [auditRow(JOB, "completed", { task: "Job A" })], [JOB2]: [auditRow(JOB2, "completed", { task: "Job B", proposal: null })] },
      proposals: { [JOB]: PROPOSAL() }, startFaults: 0,
      decideFaults: [
        { code: "proposal_preimage_changed", detail: "src/portal/PortalApp.tsx changed since the job read it" },
        { code: "timeout_no_reply", detail: "no reply to `jobs.decide` within 145s", hint: "outcome unknown" },
        { code: "timeout_no_reply", detail: "no reply to `jobs.decide` within 145s", hint: "outcome unknown", land: true },
      ],
    };
    const app6 = await mount(state6);
    await click(byText('[role="tab"]', "Workspace"));
    await settle();
    const openJob = async (label) => {
      await click([...document.querySelectorAll('[aria-label="Jobs"] button')].find((b) => b.textContent.includes(label)));
      await until(() => (document.querySelector('article[aria-label="Job"] h2')?.textContent || "") === label, `${label} open`);
    };
    const action = () => document.querySelector('[aria-label="Decision request"]')?.textContent || "";
    const decides = () => app6.calls.filter((c) => c.cmd === "portal_jobs_decide_v2").map((c) => c.args);
    await openJob("Job A");
    await until(() => byText("button", "Approve"), "proposal shown");

    // 1. Refused, and the read-back itself fails: two different faults, both shown.
    state6.auditFailAt = (state6.auditCalls || 0) + 1;
    await click(byText("button", "Approve"));
    await settle(60);
    assert.ok(workspace().textContent.includes("proposal_preimage_changed"), "the approve refusal is still visible after its read-back");
    assert.ok(action().includes("refused") && action().includes("proposal_preimage_changed"), "the approve refusal is shown as the action's outcome");
    assert.ok(workspace().textContent.includes("audit read timed out"), "the failed read-back is reported separately");
    // 2. A successful read-back clears the READ fault, never the action fault.
    await click(byText("button", "Retry"));
    await settle(60);
    assert.ok(!workspace().textContent.includes("audit read timed out"), "the read fault clears on a successful read");
    assert.ok(action().includes("proposal_preimage_changed"), "a successful read-back does not erase the rejected approval");
    // 3. Bound to the job: not shown under B, still there back on A.
    await openJob("Job B");
    assert.ok(!workspace().textContent.includes("proposal_preimage_changed"), "A's decision outcome is not shown under B");
    await openJob("Job A");
    await until(() => byText("button", "Approve"), "proposal shown again");
    assert.ok(action().includes("proposal_preimage_changed"), "A's write receipt is retained across navigation");

    // 4. Unknown outcome, read-back still undecided: the uncertainty stays.
    await click(byText("button", "Approve"));
    await settle(60);
    assert.ok(action().includes("outcome unknown") && action().includes("timeout_no_reply"), "an unknown write outcome is shown");
    assert.ok(action().includes("no decision"), "an undecided read-back does not resolve the unknown outcome");
    assert.ok(!action().includes("proposal_preimage_changed"), "the receipt is the latest attempt's");
    assert.equal(decides()[1].request_key, decides()[0].request_key, "the retry of the same decision reuses its key");

    // 5. Unknown outcome that DID land: resolved only by the matching decision.
    await click(byText("button", "Approve"));
    await settle(60);
    await until(() => workspace().textContent.includes("Decision: approved"), "read-back shows the approved decision");
    assert.ok(action().includes("read-back confirms") && action().includes("approved"), "the lost reply is resolved by the matching read-back");
    assert.equal(decides().length, 3);
    assert.equal(decides()[2].request_key, decides()[0].request_key);
    pass("decision refusals/unknown outcomes are kept apart from reads and resolved only by their own decision (C6)");
    await flush(() => app6.root.unmount());
  }

  // ---- R1/R2: ambiguous transports are never rendered as refusals --------
  // Cases, wording and assertions are the DS author's proposed block; the
  // coordinator adapted only the call shape to this file's real helpers
  // (the author used node-style choose(root,sel,val)/assert(cond,msg)/mount().root).
  {
    const jobRow = () => ({
      id: JOB,
      status: "completed",
      role: "coder",
      task: "do the thing",
      attachment: "bob-portal",
      created_at: 1,
      terminal: true,
      proposal: { digest: DIGEST, files: 1 },
      decision: null,
    });
    const startState = (fault) => ({
      attachments: ATTACHMENTS,
      jobs: [jobRow()],
      audits: { [JOB]: [auditRow(JOB, "completed", { task: "do the thing" })] },
      proposals: { [JOB]: PROPOSAL() },
      startFaults: 1,
      startFault: fault,
    });
    const driveStart = async (state) => {
      const app = await mount(state);
      await click(byText('[role="tab"]', "Workspace"));
      await settle();
      await fill(document.getElementById("workspace-task"), "start the thing");
      await submit(document.getElementById("workspace-task").closest("form"));
      await settle();
      const row = document.querySelector('[aria-label="Unconfirmed starts"]');
      return { app, text: row ? row.textContent : "" };
    };

    // (a) a gateway 502 after dispatch is ambiguous, not a refusal.
    const r1 = await driveStart(startState({ code: "gateway_http_502", detail: "gateway answered 502", hint: "" }));
    assert.ok(r1.text.includes("Outcome unknown"), "a gateway_http_502 start is shown as outcome unknown");
    assert.ok(r1.text.includes("gateway_http_502"), "the ambiguous start names its code");
    assert.ok(!r1.text.includes("Refused"), "a gateway_http_502 start is not labelled a refusal");
    await flush(() => r1.app.root.unmount());

    // (b) a broker-shaped definite refusal still reads as a refusal.
    const r1b = await driveStart(startState({ code: "path_outside_token_scope", detail: "outside scope", hint: "" }));
    assert.ok(r1b.text.includes("Refused"), "a definite refusal start is labelled refused");
    assert.ok(!r1b.text.includes("Outcome unknown"), "a definite refusal is not called unknown");
    await flush(() => r1b.app.root.unmount());

    // (c) a lost-but-landed approve whose read-back carries a DIFFERENT key must not confirm.
    const r2 = await mount({
      attachments: ATTACHMENTS,
      jobs: [jobRow()],
      audits: { [JOB]: [auditRow(JOB, "completed", { task: "do the thing" })] },
      proposals: { [JOB]: PROPOSAL() },
      decideFaults: [{ code: "timeout_no_reply", detail: "no reply", hint: "outcome unknown", land: true }],
      decideEchoKey: "9".repeat(64),
    });
    await click(byText('[role="tab"]', "Workspace"));
    await settle();
    await click(document.querySelector('[aria-label="Jobs"] button'));
    await until(() => byText("button", "Approve"), "proposal shown");
    await click(byText("button", "Approve"));
    await settle(60);
    const foreign = document.querySelector('[aria-label="Decision request"]');
    const ftext = foreign ? foreign.textContent : "";
    assert.ok(ftext.includes("outcome unknown"), "a foreign-key read-back leaves the receipt unknown");
    assert.ok(ftext.includes("different decision"), "a foreign-key read-back is named as a different decision");
    assert.ok(!ftext.includes("read-back confirms"), "a foreign-key read-back never confirms this request");
    await flush(() => r2.root.unmount());

    // (d) positive control: a plain approve reports the server answer.
    const r2b = await mount({
      attachments: ATTACHMENTS,
      jobs: [jobRow()],
      audits: { [JOB]: [auditRow(JOB, "completed", { task: "do the thing" })] },
      proposals: { [JOB]: PROPOSAL() },
    });
    await click(byText('[role="tab"]', "Workspace"));
    await settle();
    await click(document.querySelector('[aria-label="Jobs"] button'));
    await until(() => byText("button", "Approve"), "proposal shown");
    await click(byText("button", "Approve"));
    await settle(60);
    const answered = document.querySelector('[aria-label="Decision request"]');
    const atext = answered ? answered.textContent : "";
    assert.ok(atext.includes("answered"), "a plain approve shows the server answer");
    assert.ok(atext.includes("approved"), "a plain approve reports the approved state");
    await flush(() => r2b.root.unmount());

    pass("R1/R2: ambiguous transports render as outcome unknown, refusals as refused, and a mismatched key never confirms");
  }

  // ---- C7: bytes the UI never displayed cannot be approved here ---------
  {
    const TRUNC = PROPOSAL({
      files: [PROPOSAL().files[0], { path: "src/portal/hidden.ts", pre_sha256: PRE, post_sha256: POST, bytes: 200000, created: false, operation: "replace" }],
      diff_truncated: true, applicable: false,
    });
    const state7 = {
      attachments: ATTACHMENTS,
      jobs: [{ id: JOB, status: "completed", role: "coder", task: "Big change", attachment: "bob-portal", created_at: 1, terminal: true, proposal: { digest: DIGEST, files: 2 }, decision: null }],
      audits: { [JOB]: [auditRow(JOB, "completed", { task: "Big change", proposal: { digest: DIGEST, file: "proposal-x.json", files: TRUNC.files, diff_truncated: true } })] },
      proposals: { [JOB]: TRUNC }, startFaults: 0,
    };
    const app7 = await mount(state7);
    await click(byText('[role="tab"]', "Workspace"));
    await settle();
    await click(document.querySelector('[aria-label="Jobs"] button'));
    await until(() => document.querySelector('[aria-label="Proposed diff"]'), "truncated proposal shown");
    const approve = byText("button", "Approve");
    assert.ok(approve && approve.disabled === true, "approve is disabled while the displayed diff is truncated");
    assert.ok(workspace().textContent.includes("cannot be approved in this app"), "the refusal to approve is explained");
    await flush(() => approve.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })));
    await settle();
    assert.equal(app7.calls.filter((c) => c.cmd === "portal_jobs_decide_v2").length, 0, "no approval request is sent for undisplayed bytes");
    await click(byText("button", "Deny"));
    await until(() => workspace().textContent.includes("Decision: denied"), "deny still works");
    assert.deepEqual(app7.calls.filter((c) => c.cmd === "portal_jobs_decide_v2").map((c) => c.args.decision), ["deny"]);
    pass("a truncated proposal fails closed for Approve with an explanation; Deny still works (C7)");
    await flush(() => app7.root.unmount());
  }


  // ---- S43f: negotiated states, bounded evidence and owner-only decisions ----
  const jobSummary = (status = 'completed') => ({ id: JOB, run_id: 'run-' + JOB, status, role: 'proposer', task: 'Scoped proposal', attachment: 'bob-portal', created_at: 1, terminal: auditRow(JOB, status).terminal, proposal: { digest: DIGEST, files: 1 }, decision: null });
  const freshState = (over = {}) => ({ attachments: ATTACHMENTS, jobs: [jobSummary()], audits: { [JOB]: [auditRow(JOB, 'completed')] }, proposals: { [JOB]: PROPOSAL() }, ...over });
  const openWorkspaceJob = async (state) => {
    const app = await mount(state); await click(byText('[role="tab"]', 'Workspace')); await settle();
    const listed = document.querySelector('[aria-label="Jobs"] button');
    if (listed && !listed.disabled) { await click(listed); await settle(); }
    return app;
  };
  for (const status of ['accepted', 'queued', 'dispatch_intent', 'running', 'checking', 'completed', 'succeeded', 'failed', 'timed_out', 'cancelled', 'cancelling', 'paused', 'unknown', 'unavailable']) {
    const app = await openWorkspaceJob(freshState({ jobs: [jobSummary(status)], audits: { [JOB]: [auditRow(JOB, status, { proposal: null })] } }));
    assert.ok(workspace().textContent.includes('Status: ' + status), status + ' is shown from the persisted audit');
    assert.equal(absent('button', 'Stop'), auditRow(JOB, status).terminal, status + ' uses the server terminal flag');
    await flush(() => app.root.unmount());
  }
  pass('S43f: all job states, including unavailable, queued, cancelling and unknown');

  {
    const state = freshState({ needsProof: true });
    const app = await openWorkspaceJob(state);
    await click(byText('button', 'Approve')); await settle();
    assert.ok(!!document.querySelector('[role="dialog"][aria-label="Confirm it\'s you"]'), 'Decide uses the existing owner confirmation');
    assert.equal(app.calls.filter(c => c.cmd === 'portal_jobs_decide_v2').length, 0, 'no decision before owner proof');
    const begin = app.calls.find(c => c.cmd === 'portal_owner_begin_v2');
    assert.deepEqual(begin.args, { contract_version: 2, operation: 'jobs.decide', resource: { job_id: JOB } });
    await fill(document.querySelector('[aria-label="Owner password"]'), 'fixture-owner-password');
    await fill(document.querySelector('[aria-label="Owner authenticator code"]'), '123456');
    await click(byText('button', 'Confirm and continue')); await settle();
    assert.ok(workspace().textContent.includes('Decision: approved') && workspace().textContent.includes(APPLY_REASON));
    assert.ok(![...document.querySelectorAll('button')].some(b => /apply/i.test(b.textContent)), 'there is no Apply button');
    const decision = app.calls.find(c => c.cmd === 'portal_jobs_decide_v2');
    assert.equal(decision.args.expected_revision, '9007199254740993', 'decision revision stays an exact decimal string');
    assert.ok(!JSON.stringify(decision).includes('fixture-owner-password') && !JSON.stringify(decision).includes('123456'), 'owner factors stay in the owner confirmation IPC');
    await flush(() => app.root.unmount());
  }
  for (const identity of [{ ...HELLO.identity, token_type: 'agent' }, { ...HELLO.identity, token_type: 'human', actor: 'other', sub: 'other' }, { ...HELLO.identity, token_type: 'portal', owner_controls_eligible: false }]) {
    const app = await openWorkspaceJob(freshState({ hello: { identity } }));
    assert.ok(absent('button', 'Approve') && absent('button', 'Deny'), 'agents and non-owner identities have no owner controls');
    assert.ok(!app.calls.some(c => c.cmd === 'portal_owner_begin_v2' || c.cmd === 'portal_jobs_decide_v2'));
    await flush(() => app.root.unmount());
  }
  {
    const app = await openWorkspaceJob(freshState({ decideFaults: [{ code: 'owner_portal_required', detail: 'owner_portal_required' }] }));
    await click(byText('button', 'Approve')); await settle();
    assert.ok(workspace().textContent.includes('owner_portal_required') && document.querySelector('[aria-label="Decision request"]').textContent.includes('refused'), 'server owner denial survives read-back');
    await flush(() => app.root.unmount());
  }
  pass('S43f: owner proof, recorded decision, no apply, non-owner/agent controls and server denial');

  {
    const app = await openWorkspaceJob(freshState({ foreignJob: JOB }));
    assert.ok(workspace().textContent.includes('not_found'), 'foreign-resource denial is shown without a fabricated row');
    assert.equal(document.querySelector('article[aria-label="Job"]') === null, true);
    await flush(() => app.root.unmount());
  }
  {
    const state = freshState({ jobs: [jobSummary('running')], audits: { [JOB]: [auditRow(JOB, 'running')] }, cancelQueue: [auditRow(JOB, 'cancelling'), auditRow(JOB, 'cancelled')] });
    const app = await openWorkspaceJob(state); await click(byText('button', 'Stop')); await settle();
    assert.ok(workspace().textContent.includes('Status: cancelling'), 'cancellation intent does not claim cleanup');
    await until(() => workspace().textContent.includes('Status: cancelled'), 'cleanup is confirmed by a later audit');
    await flush(() => app.root.unmount());
  }
  {
    const state = freshState({ attachmentFault: { code: 'not_found', detail: 'registration unavailable' } });
    const app = await openWorkspaceJob(state);
    assert.ok(workspace().textContent.includes('registration unavailable') && workspace().textContent.includes('Status: completed'), 'an attachment failure does not hide readable existing jobs');
    await flush(() => app.root.unmount());
  }
  {
    const art = { name: 'Bounded evidence', file: 'report-' + JOB + '.json', sha256: DIGEST };
    const state = freshState({ audits: { [JOB]: [auditRow(JOB, 'completed', { artifacts: [art] })] }, deferred: [] });
    const app = await openWorkspaceJob(state); await click(byText('button', 'Bounded evidence'));
    await flush(() => state.deferred[0].resolve(artifactChunk(art.file, 'first chunk', { total_bytes: 100, truncated: true, next_offset: 11 })));
    assert.ok(document.querySelector('[aria-label="Artifact text"]').textContent.includes('readback truncated'), 'bounded artifact readback states that bytes remain');
    assert.equal(state.deferred.length, 1, 'bounded display never downloads the rest automatically');
    assert.ok(workspace().textContent.includes(PRE) && workspace().textContent.includes(POST), 'base and output digests are visible');
    await flush(() => app.root.unmount());
  }
  pass('S43f: foreign denial, cancellation intent, independent reads and bounded artifact evidence');

  const { jobsReason } = require('../src/portal/workspace-v2-client.ts');
  for (const missing of Object.keys(OPERATIONS).filter(op => op.startsWith('jobs.'))) {
    const operations = { ...OPERATIONS }; delete operations[missing];
    const catalog = { ...CATALOG, capabilities: { ...CATALOG.capabilities, operations } };
    const app = await openWorkspaceJob(freshState({ catalog }));
    assert.ok(workspace().textContent.includes('Update Bob on the server to use ' + missing), missing + ' names its unavailable feature');
    assert.ok(!app.calls.some(c => c.cmd === 'portal_' + missing.replace('.', '_') + '_v2'), missing + ' is never dispatched');
    for (const other of Object.keys(operations).filter(op => op.startsWith('jobs.'))) assert.equal(jobsReason(catalog, other), '', missing + ' does not disable ' + other);
    if (missing !== 'jobs.attachments') assert.ok(app.calls.some(c => c.cmd === 'portal_jobs_attachments_v2'));
    if (missing !== 'jobs.list') assert.ok(app.calls.some(c => c.cmd === 'portal_jobs_list_v2'));
    if (missing === 'jobs.start') assert.equal(byText('button', 'Start job').disabled, true);
    if (missing === 'jobs.audit') assert.equal(document.querySelector('[aria-label="Jobs"] button').disabled, true);
    if (missing === 'jobs.decide') assert.equal(byText('button', 'Approve').disabled, true);
    await flush(() => app.root.unmount());
  }
  pass('S43f: each missing server operation disables only that feature');

  // Client compatibility and exact request-key semantics at the real native boundary.
  {
    const { createWorkspaceClient, BUDGETS } = require('../src/portal/workspace-client.ts');
    const native = [];
    const client = createWorkspaceClient(async (cmd, args) => { native.push({cmd,args}); return auditRow(JOB, 'accepted'); });
    await client.start('researcher', 'Legacy bounded request', null, 'original-key', BUDGETS[0]);
    assert.deepEqual(native[0], { cmd: 'portal_jobs_start', args: { role: 'researcher', task: 'Legacy bounded request', request_key: 'original-key', model_calls: 4, tool_calls: 12, wall_seconds: 60 } }, 'legacy client API preserves its literal ceilings');
    const original = { contract_version: 2, role: 'proposer', task: 'Exact request', attachment: 'bob-portal', request_key: 'single-key', team: TEAM.id, team_revision: '9007199254740993', budget: { start_calls: 4, wall_mode: 'fixed', wall_seconds: 60, max_job_attempts: 1, job_seconds_budget: '900' }, job_policy: { proposed_seconds: '900', rationale: 'Bounded report', auto_escalate: false } };
    const receipts = new Map(); let submits = 0;
    const replayClient = createWorkspaceClient(async (cmd, args) => {
      assert.equal(cmd, 'portal_jobs_start_v2'); submits++;
      const prior = receipts.get(args.request_key), body = JSON.stringify(args);
      if (prior && prior !== body) throw JSON.stringify({ code: 'idempotency_conflict', detail: 'changed payload' });
      receipts.set(args.request_key, body); return { ...auditRow(JOB, 'queued'), replayed: !!prior };
    }).v2;
    assert.equal((await replayClient.start(original)).replayed, false);
    assert.equal((await replayClient.start(original)).replayed, true, 'same key and payload recover the first job');
    await assert.rejects(() => replayClient.start({ ...original, task: 'changed' }), e => e.code === 'idempotency_conflict');
    for (const bad of [true, 1.5, '01', '9223372036854775808', 9007199254740992]) {
      const before = submits;
      await assert.rejects(async () => replayClient.start({ ...original, team_revision: bad }));
      assert.equal(submits, before, 'invalid integers never reach native IPC');
    }
  }
  pass('S43f: backward-compatible client API, replay conflict and lossless integer validation');


  // Additional v2 fields belong to the same immutable request tuple as C4.
  {
    const state = freshState({ jobs: [], audits: {}, startFaults: 2, teams: [TEAM, { ...TEAM, id: 'second-fixture-team', revision: 2 }] });
    const app = await mount(state); await click(byText('[role="tab"]', 'Workspace')); await settle();
    await fill(document.getElementById('workspace-task'), 'Keep original v2 fields');
    const form = document.getElementById('workspace-task').closest('form'); await submit(form); await settle();
    const starts = () => app.calls.filter(c => c.cmd === 'portal_jobs_start_v2').map(c => c.args);
    const original = JSON.parse(JSON.stringify(starts()[0]));
    await choose(document.getElementById('workspace-job-seconds'), '1800');
    await choose(document.getElementById('workspace-team'), 'second-fixture-team');
    assert.ok(absent('button', 'Resend same request'), 'team/job-time edits are explicit new requests');
    await submit(form); await settle();
    assert.notEqual(starts()[1].request_key, original.request_key);
    const pending = document.querySelector('[aria-label="Unconfirmed starts"] li');
    await click([...pending.querySelectorAll('button')].find(b => b.textContent === 'Resend this exact request')); await settle();
    assert.deepEqual(starts()[2], original, 'resend preserves original team, team revision, root/job budgets, coverage and key');
    await flush(() => app.root.unmount());
  }
  {
    const state = freshState({ needsProof: true }); const app = await openWorkspaceJob(state);
    await click(byText('button', 'Approve')); await settle(); await click(byText('button', 'Cancel')); await settle();
    assert.equal(app.calls.filter(c => c.cmd === 'portal_jobs_decide_v2').length, 0, 'cancelled owner proof sends no decision');
    assert.ok(document.querySelector('[aria-label="Decision request"]').textContent.includes('was not sent'), 'unsent owner cancellation never claims an unknown write');
    await flush(() => app.root.unmount());
  }
  {
    const deleted = { path: 'removed.txt', operation: 'delete', pre_sha256: PRE, post_sha256: null, bytes: 0, created: false };
    const app = await openWorkspaceJob(freshState({ proposals: { [JOB]: PROPOSAL({ files: [deleted] }) } }));
    assert.ok(document.querySelector('.workspace-files').textContent.includes('removed.txt (delete)'), 'deletion and its null output reference are displayed');
    await flush(() => app.root.unmount());
    const { createWorkspaceClient } = require('../src/portal/workspace-client.ts');
    const api = createWorkspaceClient(async () => PROPOSAL({ files: [{ ...deleted, post_sha256: POST }] })).v2;
    await assert.rejects(() => api.proposal(JOB), e => e.code === 'protocol_invalid', 'contradictory deletion is rejected');
  }
  for (const [op, mode] of [['jobs.list','readable'], ['jobs.cancel','writable'], ['jobs.start','runnable']]) {
    const catalog = { ...CATALOG, capabilities: { ...CATALOG.capabilities, operations: { ...OPERATIONS, [op]: { ...OPERATIONS[op], [mode]: false, reason: 'fixture_mode_denied' } } } };
    assert.equal(jobsReason(catalog, op), 'fixture_mode_denied');
    assert.equal(jobsReason(catalog, 'jobs.proposal'), '', 'mode denial remains feature-local');
  }
  pass('S43f: immutable team/job-time tuple, unsent owner cancellation, deletion and operation modes');

  console.log("workspace-ui: all checks passed");
  process.exit(0);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
