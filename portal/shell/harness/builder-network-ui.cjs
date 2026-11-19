const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'CustomEvent', 'localStorage']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { BuilderModeBar } = require('../src/portal/BuilderModeBar.tsx');
const { BuilderApprovalCard } = require('../src/portal/BuilderApprovalCard.tsx');
const { BuilderNetworkSettings } = require('../src/portal/BuilderNetworkSettings.tsx');
const { BuilderNetworkLog } = require('../src/portal/BuilderNetworkLog.tsx');
const { builderHostname, builderRevision, builderMethod } = require('../src/portal/builder-network.ts');

// Emit for a separate Python run; no live servers, keys or providers.
if (process.argv.includes('--broker-source')) {
  process.stdout.write(String.raw`
import importlib.util, sys, types, time
try: import fcntl
except ImportError: sys.modules['fcntl'] = types.ModuleType('fcntl')
spec = importlib.util.spec_from_file_location('network_broker_test', sys.argv[1])
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
b = m.Broker.__new__(m.Broker); b.identity = {'token_type':'portal'}; b.fanout_seconds = 30
b._owner_session = {'active':True,'expires_at':time.time()+250}
calls = []; gates = []
class Transport:
    def __init__(self,name): self.name = name
    def rest(self,method,path,**kw):
        calls.append((self.name,method,path,kw)); return {'ok':True}
b.transport = Transport('normal'); b._owner_transport = Transport('owner')
b._require_owner_operation = gates.append
base = {'project_id':'p','add':['example.com'],'expected_revision':'9007199254740993','request_key':'k'}
for op,args,transport in [('network.set',base,'owner'),('network.set',{**base,'remove':['old.com']},'owner'),('network_log',{'conversation_id':'c'},'normal'),('network_log',{'job_id':'j'},'normal'),('approve',{'approval_id':'a','decision':'allow_project','request_key':'k'},'owner')]:
    b.op_builder(op,args)
    assert calls[-1][:3] == (transport,'POST','/api/builder/'+op)
    assert calls[-1][3]['body'] == {**args,'contract_version':2}
    assert gates[-1] == 'builder.'+op
bad = [('network_log',{}),('network_log',{'conversation_id':'c','job_id':'j'}),('network_log',{'job_id':None}),('network_log',{'job_id':' bad '}),('network.set',{**base,'owner':True})]
for key in ['project_id','request_key','expected_revision']:
    bad.append(('network.set',{k:v for k,v in base.items() if k!=key}))
for value in ['https://a.com','*.a.com','a.com:443','A.com','a..com','-a.com','a-.com','a.com.','localhost','127.0.0.1','[::1]','a/b.com','a.com'+chr(10),'é.com','a.123']:
    bad.append(('network.set',{**base,'add':[value]}))
for changes in [{'add':None},{'add':'x.com'},{'add':[]},{'add':[True]},{'add':[{}]},{'add':['a.com','a.com']},{'add':['a.com'],'remove':['a.com']},{'add':['x%d.com'%i for i in range(257)]}]:
    bad.append(('network.set',{**base,**changes}))
for rev in [True,-1,0.5,9007199254740992,'01','9223372036854775808']:
    bad.append(('network.set',{**base,'expected_revision':rev}))
for op,args in bad:
    before = len(calls)
    try: b.op_builder(op,args)
    except m.BrokerError: pass
    else: raise AssertionError((op,args))
    assert len(calls)==before
b.identity = {'token_type':'human'}
before = len(calls)
try: b.op_builder('network.set',base)
except m.BrokerError as e: assert e.code == 'owner_portal_required'
else: raise AssertionError('owner portal required')
assert len(calls)==before
b.sub = 'owner'
metadata = {'active':True,'expires_at':time.time()+250,'owner':'owner','actor':'owner','lane':'a'*64,'credential_epoch':1,'allowed_operations':['builder.network.set']}
assert b._safe_owner_metadata({'owner_session':metadata})['allowed_operations'] == ['builder.network.set']
print('network broker: exact routing, owner isolation, revisions, decisions, closed schemas and host validation passed')
`);
  process.exit(0);
}
const root = createRoot(document.getElementById('root'));
const ops = ['builder.mode.get', 'builder.network.set', 'builder.network_log', 'builder.approval.get', 'builder.approve', 'owner.status', 'owner.begin', 'owner.confirm'];
const catalog = { capabilities: { operations: Object.fromEntries(ops.map(op => [op, { contract_versions: [2], available: true, implemented: true }])) } };
const clone = v => JSON.parse(JSON.stringify(v));
let hits = [], mount = 0, record, failure = '', wrongProject = false, logFailure = false;
let mode = { conversation_id: 'c', project_id: 'p', mode: 'approve', expires_at: null, revision: 10, network: { allowlist: ['pypi.org'], project_extra: ['old.example.com'], revision: '9007199254740993' } };
let logs = { items: [
  { conversation_id: 'c', job_id: 'j1', approval_id: 'a', domain: 'example.com', method: 'GET', bytes_in: '9007199254740993', bytes_out: 10, decision: 'allow' },
  { conversation_id: 'c', job_id: 'j2', domain: 'other.example.com', method: null, bytes_in: 0, bytes_out: 0, decision: 'ask' },
], truncated: false };
const client = createPortalClient(async (command, args) => {
  hits.push([command, clone(args)]); assert.equal(args.contract_version, 2);
  switch (command) {
    case 'portal_builder_mode_get_v2': return clone(wrongProject ? { ...mode, project_id: 'foreign' } : mode);
    case 'portal_builder_network_set_v2':
      assert.equal(args.expected_revision, mode.network.revision, 'uses network revision, not mode revision');
      assert.ok(args.request_key); assert.equal(args.project_id, 'p');
      if (failure === 'conflict') { failure = ''; throw JSON.stringify({ code: 'revision_conflict', detail: 'Network revision changed.' }); }
      mode.network.project_extra = [...new Set([...mode.network.project_extra, ...(args.add ?? [])])].filter(d => !(args.remove ?? []).includes(d));
      mode.network.revision = String(BigInt(mode.network.revision) + 1n);
      if (failure === 'lost') { failure = ''; throw JSON.stringify({ code: 'transport_unavailable', detail: 'Lost network receipt.' }); }
      return { project_id: 'p', ...clone(mode.network) };
    case 'portal_owner_status_v2': return { owner_session: { active: false } };
    case 'portal_owner_begin_v2': return { challenge_id: 'challenge-' + hits.length, confirmation_mode: 'signed_in_user', summary: { operation: args.operation, resource: args.resource } };
    case 'portal_owner_confirm_v2': return { owner_session: { active: true } };
    case 'portal_builder_approval_get_v2': return clone(record);
    case 'portal_builder_approve_v2':
      record.state = args.decision === 'deny' ? 'denied' : 'allowed';
      return { approval_id: record.approval_id, state: record.state };
    case 'portal_builder_network_log_v2':
      if (logFailure) throw JSON.stringify({ code: 'operation_unavailable', detail: 'Network log unavailable.' });
      return clone(args.job_id ? { ...logs, items: logs.items.filter(r => r.job_id === args.job_id) } : logs);
    default: throw Error(command);
  }
});
const count = op => hits.filter(([name]) => name === op).length;
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent === text);
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
async function render(Component, props) { await act(async () => { root.render(React.createElement(Component, { key: ++mount, ...props })); await flush(); }); }
async function click(text) { assert.ok(button(text), text); await act(async () => { button(text).click(); await flush(); }); }
async function input(selector, value) {
  await act(async () => {
    const e = document.querySelector(selector); assert.ok(e, selector);
    Object.getOwnPropertyDescriptor(e instanceof dom.window.HTMLSelectElement ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype, 'value').set.call(e, value);
    e.dispatchEvent(new Event('input', { bubbles: true })); e.dispatchEvent(new Event('change', { bubbles: true })); await flush();
  });
}
const settings = { client, catalog, scope: 'network-test', projectId: 'p', conversationId: 'c', owner: true };
let approvalSequence = 0;
function approval(detail = { domain: 'example.com', method: 'POST', command: 'curl example.com' }) {
  record = { schema_version: 1, approval_id: 'network-' + ++approvalSequence, conversation_id: 'c', kind: 'network', summary: 'SERVER network', detail, mode: 'approve', state: 'pending', expires_at: Date.now() / 1000 + 600 };
  // Terminal server records are immutable; each scenario is a new action.
  card.approvalId = record.approval_id;
}
const card = { client, catalog, scope: 'network-card', conversationId: 'c', approvalId: 'a' };
(async () => {
  for (const d of ['https://a.com', 'a.com:80', '*.a.com', 'A.com', '127.0.0.1', 'localhost', 'a.com.', 'a..com', '-a.com', 'a.123', 'é.com', 'a.com\n', 'a.com\r', 'a.com\t']) assert.equal(builderHostname(d), false, d);
  for (const revision of ['1\n', '1 ', '01', -1, true, '9223372036854775808']) assert.equal(builderRevision(revision), false);
  for (const method of ['GET\n', 'get', '', 123]) assert.equal(builderMethod(method), false);
  assert.ok(builderHostname('xn--caf-dma.example.com'));
  await render(BuilderModeBar, { client, catalog, conversationId: 'c', scope: 'mode-network' });
  assert.ok(document.body.textContent.includes('Network bounded'));
  await act(async () => {
    const options = document.querySelector('.builder-options');
    options.open = true; options.dispatchEvent(new Event('toggle')); await flush();
  });
  assert.ok(document.body.textContent.includes('pypi.org') && document.body.textContent.includes('old.example.com'));
  for (const [label, decision] of [['Allow once', 'allow_once'], ['Allow for session', 'allow_session'], ['Always for this project', 'allow_project'], ['Deny', 'deny']]) {
    approval(); await render(BuilderApprovalCard, card);
    assert.ok(document.body.textContent.includes('example.com') && document.body.textContent.includes('POST') && document.body.textContent.includes('curl example.com'));
    const before = count('portal_builder_approve_v2'); await click(label);
    assert.equal(count('portal_builder_approve_v2'), before); await click('Confirm and continue');
    assert.equal(hits.filter(([op]) => op === 'portal_builder_approve_v2').at(-1)[1].decision, decision);
    assert.equal(button(label), undefined, 'decided network cards have no decision buttons');
  }
  approval({ domain: 'example.com' }); record.mode = 'suggest';
  await render(BuilderApprovalCard, card); assert.ok(button('Allow once'), 'network domain without optional command/method remains actionable');
  assert.ok(document.body.textContent.includes('not visible')); await click('Always for this project'); await click('Cancel');
  assert.equal(record.state, 'pending');
  approval({ domain: '*.example.com' }); await render(BuilderApprovalCard, card); assert.ok(!button('Allow once'));
  approval(); record.expires_at = Date.now() / 1000 - 1; await render(BuilderApprovalCard, card); assert.ok(button('Always for this project').disabled);

  let before = count('portal_builder_mode_get_v2');
  await render(BuilderNetworkSettings, { ...settings, owner: false }); assert.equal(document.body.textContent, ''); assert.equal(count('portal_builder_mode_get_v2'), before);
  await render(BuilderNetworkSettings, { ...settings, conversationId: undefined }); assert.ok(button('Add domain').disabled);
  wrongProject = true; await render(BuilderNetworkSettings, settings); assert.ok(button('Add domain').disabled); wrongProject = false;
  await render(BuilderNetworkSettings, settings);
  await input('input', 'https://bad.com'); assert.ok(button('Add domain').disabled);
  await input('input', 'new.example.com'); assert.ok(!button('Add domain').disabled);
  before = count('portal_builder_network_set_v2'); await click('Add domain');
  assert.equal(count('portal_builder_network_set_v2'), before); await click('Cancel'); assert.equal(count('portal_builder_network_set_v2'), before);
  await click('Add domain'); await click('Confirm and continue');
  assert.ok(mode.network.project_extra.includes('new.example.com'));
  assert.equal(hits.filter(([op]) => op === 'portal_owner_begin_v2').at(-1)[1].resource.project_id, 'p');
  assert.ok(!button('Remove pypi.org'), 'default allowlist cannot be removed');
  await click('Remove old.example.com'); await click('Confirm and continue'); assert.ok(!mode.network.project_extra.includes('old.example.com'));
  await input('input', 'stale.example.com'); await click('Add domain');
  mode.network.revision = String(BigInt(mode.network.revision) + 1n);
  before = count('portal_builder_network_set_v2'); await click('Confirm and continue'); assert.equal(count('portal_builder_network_set_v2'), before, 'stale reviewed state never dispatches');
  await click('Refresh network settings');
  await input('input', 'conflict.example.com'); failure = 'conflict'; await click('Add domain'); await click('Confirm and continue');
  assert.ok(document.body.textContent.includes('Network revision changed'));
  await click('Refresh network settings');
  await input('input', 'lost.example.com'); failure = 'lost'; await click('Add domain'); await click('Confirm and continue');
  assert.ok(document.body.textContent.includes('Lost network receipt'));
  before = count('portal_builder_network_set_v2'); await render(BuilderNetworkSettings, settings);
  assert.equal(count('portal_builder_network_set_v2'), before, 'reconnect only reads'); assert.ok(button('Remove lost.example.com'));
  await input('input', 'next.example.com'); await click('Add domain'); await click('Confirm and continue'); assert.ok(button('Remove next.example.com'), 'new revision releases uncertain mutation');
  await input('input', 'cancel.example.com'); await click('Add domain');
  before = count('portal_builder_network_set_v2'); await render(BuilderNetworkSettings, { ...settings, projectId: 'other' });
  assert.ok(!document.querySelector('.owner-modal')); assert.equal(count('portal_builder_network_set_v2'), before, 'navigation cancels owner review');

  await render(BuilderNetworkLog, { client, conversationId: 'c' });
  assert.ok(document.body.textContent.includes('9007199254740993') && document.body.textContent.includes('not visible'));
  await input('select', 'j1'); assert.equal(document.querySelectorAll('tbody tr').length, 1);
  await render(BuilderNetworkLog, { client, jobId: 'j2' });
  assert.deepEqual(hits.filter(([op]) => op === 'portal_builder_network_log_v2').at(-1)[1], { contract_version: 2, job_id: 'j2' });
  assert.equal(document.querySelectorAll('tbody tr').length, 1);
  logs.truncated = true; await click('Refresh network log'); assert.ok(document.body.textContent.includes('Older requests are omitted'));
  logs.items[0].conversation_id = 'foreign'; await render(BuilderNetworkLog, { client, conversationId: 'c' }); assert.ok(document.body.textContent.includes('Invalid server network log')); logs.items[0].conversation_id = 'c';
  logFailure = true; await click('Refresh network log'); assert.ok(document.body.textContent.includes('Network log unavailable')); assert.ok(!document.querySelector('tbody'));
  logFailure = false; logs.items = []; await click('Refresh network log'); assert.ok(document.body.textContent.includes('No proxied requests'));
  await act(async () => root.unmount());
  console.log('builder network UI: decisions, server records, bounded indicator, owner-only edits, exact network revisions, conflict/lost-receipt/navigation reconciliation, scoped logs and filtering passed');
})().catch(async error => { console.error(error); await act(async () => root.unmount()); process.exitCode = 1; });
