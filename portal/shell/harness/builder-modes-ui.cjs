const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'MouseEvent', 'CustomEvent', 'DOMParser', 'localStorage']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react'), { act } = React, { createRoot } = require('react-dom/client');
const { createPortalClient } = require('../src/portal/portal-client.ts');
const { BuilderModeBar } = require('../src/portal/BuilderModeBar.tsx');
const { BuilderApprovalCards, builderApprovalIds } = require('../src/portal/BuilderApprovalCard.tsx');
const { BuilderPreview, previewHtml, decodePreview } = require('../src/portal/BuilderPreview.tsx');
// The Linux broker imports fcntl. This test stubs only that unavailable import
// on Windows; no locks/filesystem operation or live server/provider is exercised.
const brokerTest = String.raw`
import importlib.util, sys, types, time
try:
    import fcntl
except ImportError:
    sys.modules['fcntl'] = types.ModuleType('fcntl')
spec = importlib.util.spec_from_file_location('builder_broker_test', sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
b = m.Broker.__new__(m.Broker)
b.identity = {'token_type': 'portal'}
b.fanout_seconds = 30
b._owner_session = {'active': True, 'expires_at': time.time() + 250}
calls = []
class Transport:
    def __init__(self, name): self.name = name
    def rest(self, method, path, **kw):
        calls.append((self.name, method, path, kw))
        return {'ok': True}
b.transport = Transport('normal')
b._owner_transport = Transport('owner')
b._require_owner_operation = lambda op: None
examples = [
    ('mode.get', {'conversation_id': 'c'}, 'normal'),
    ('mode.set', {'conversation_id': 'c', 'mode': 'approve', 'expected_revision': 0, 'request_key': 'k'}, 'owner'),
    ('mode.set', {'conversation_id': 'c', 'mode': 'yolo', 'expected_revision': '9007199254740993', 'request_key': 'k', 'yolo_acknowledged': True}, 'owner'),
    ('approval.get', {'approval_id': 'a'}, 'normal'),
    ('approve', {'approval_id': 'a', 'decision': 'allow_session', 'request_key': 'k'}, 'owner'),
    ('preview', {'conversation_id': 'c', 'path': 'app/index.html'}, 'normal'),
]
for op, args, transport in examples:
    b.op_builder(op, args)
    lane, method, route, kw = calls[-1]
    assert (lane, method, route) == (transport, 'POST', '/api/builder/' + op)
    assert kw['body'] == {**args, 'contract_version': 2}
    assert kw['timeout'] == (650 if op == 'approve' else 30)
    try: b.op_builder(op, {**args, 'owner': 'forged'})
    except m.BrokerError: pass
    else: raise AssertionError('unknown fields must be rejected')
bad = [
    ('mode.get', {'conversation_id': ' c '}),
    ('mode.get', {'conversation_id': 'c', 'contract_version': True}),
    ('mode.set', {'conversation_id': 'c', 'mode': 'yolo', 'expected_revision': 0, 'request_key': 'k'}),
    ('mode.set', {'conversation_id': 'c', 'mode': [], 'expected_revision': 0, 'request_key': 'k'}),
    ('mode.set', {'conversation_id': 'c', 'mode': 'auto', 'expected_revision': -1, 'request_key': 'k'}),
    ('mode.set', {'conversation_id': 'c', 'mode': 'auto', 'expected_revision': 9007199254740992, 'request_key': 'k'}),
    ('approve', {'approval_id': 'a', 'decision': [], 'request_key': 'k'}),
    ('approve', {'approval_id': 'a', 'decision': 'approve', 'request_key': 'k'}),
    ('approval.get', {'approval_id': 'a', 'conversation_id': 'c'}),
]
for path in ['../x', '/x', 'a/../x', 'a//b', 'a/./b', 'C:/x', 'a'+chr(10), 'a'+chr(128), 'a'+chr(92)+'b']:
    bad.append(('preview', {'conversation_id': 'c', 'path': path}))
for op, args in bad:
    before = len(calls)
    try: b.op_builder(op, args)
    except m.BrokerError: pass
    else: raise AssertionError((op, args))
    assert len(calls) == before
assert m.MAX_REPLY_BYTES > 5 * 1024 * 1024 * 4 / 3
print('builder broker: closed schemas, owner transport, exact revisions, yolo ack, paths and preview cap passed')
`;
// Run separately with `node -r ./harness/workbench-ui-register.cjs
// harness/builder-modes-ui.cjs --broker-source | python - ../portal-broker.py`.
// Keep the ordinary UI harness independent of Python and process-spawn rights.
if (process.argv.includes('--broker-source')) { process.stdout.write(brokerTest); process.exit(0); }
const root = createRoot(document.getElementById('root'));
const ops = ['builder.mode.get', 'builder.mode.set', 'builder.approval.get', 'builder.approve', 'owner.status', 'owner.begin', 'owner.confirm'];
const catalog = { capabilities: { operations: Object.fromEntries(ops.map(op => [op, { contract_versions: [2], available: true, implemented: true }])) } };
let hits = [], records = {}, mount = 0, loseDecision = false, spoof = false, modeFailure = '';
let mode = { conversation_id: 'c', project_id: 'p', mode: 'approve', expires_at: null, network: { allowlist: ['pypi.org'], project_extra: [] }, revision: 0 };
let preview = { path: 'index.html', mime: 'text/html', bytes_b64: Buffer.from('<h1>Hello</h1><script>alert(1)</script><img src="https://evil.test/x"><a href="https://evil.test">Link</a><meta http-equiv="refresh" content="0;url=https://evil.test"><style>body{background:url(https://evil.test/x)}</style>').toString('base64'), truncated: false };
preview.size = Buffer.from(preview.bytes_b64, 'base64').length;
const clone = value => JSON.parse(JSON.stringify(value));
const client = createPortalClient(async (command, args) => {
  hits.push([command, clone(args)]);
  assert.equal(args.contract_version, 2);
  switch (command) {
    case 'portal_builder_network_log_v2': return { items: [], truncated: false };
    case 'portal_builder_mode_get_v2': return clone(mode);
    case 'portal_builder_mode_set_v2':
      assert.ok(args.request_key);
      assert.equal(args.expected_revision, mode.revision);
      if (args.mode === 'yolo') assert.equal(args.yolo_acknowledged, true);
      else assert.ok(!('yolo_acknowledged' in args));
      if (modeFailure === 'refuse') { modeFailure = ''; throw JSON.stringify({ code: 'revision_conflict', detail: 'Refresh mode.' }); }
      mode = { ...mode, mode: args.mode, revision: mode.revision + 1, expires_at: args.mode === 'yolo' ? Date.now() / 1000 + 7200 : null };
      if (modeFailure === 'lost') { modeFailure = ''; throw JSON.stringify({ code: 'transport_unavailable', detail: 'Lost mode receipt.' }); }
      return clone(mode);
    case 'portal_owner_status_v2': return { owner_session: { active: false } };
    case 'portal_owner_begin_v2': return { challenge_id: 'challenge-' + hits.length, confirmation_mode: 'signed_in_user', summary: { operation: args.operation, resource: args.resource } };
    case 'portal_owner_confirm_v2':
      assert.equal(args.confirmation_mode, 'signed_in_user');
      assert.ok(!('password' in args) && !('totp' in args));
      return { owner_session: { active: true } };
    case 'portal_builder_approval_get_v2':
      assert.deepEqual(Object.keys(args).sort(), ['approval_id', 'contract_version']);
      if (!records[args.approval_id]) throw JSON.stringify({ code: 'approval_not_found', detail: 'No server record.' });
      return clone(spoof ? { ...records[args.approval_id], conversation_id: 'foreign' } : records[args.approval_id]);
    case 'portal_builder_approve_v2': {
      assert.deepEqual(Object.keys(args).sort(), ['approval_id', 'contract_version', 'decision', 'request_key']);
      const record = records[args.approval_id];
      assert.equal(record.state, 'pending');
      record.state = args.decision === 'deny' ? 'denied' : 'allowed';
      if (loseDecision) { loseDecision = false; throw JSON.stringify({ code: 'transport_unavailable', detail: 'Lost receipt.' }); }
      return { approval_id: record.approval_id, state: record.state };
    }
    case 'portal_builder_preview_v2': return clone(preview);
    case 'portal_builder_open_temp_copy_v2':
      assert.deepEqual(args, { contract_version: 2, conversation_id: 'c', path: preview.path });
      return { opened: true };
    default: throw Error('Unexpected IPC: ' + command);
  }
});
const buttons = () => [...document.querySelectorAll('button')];
const button = text => buttons().find(b => b.textContent === text);
const count = command => hits.filter(([name]) => name === command).length;
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
async function click(text) {
  assert.ok(button(text), 'button exists: ' + text);
  await act(async () => { button(text).click(); await flush(); });
}
async function input(selector, value) {
  await act(async () => {
    const element = document.querySelector(selector);
    assert.ok(element, selector);
    Object.getOwnPropertyDescriptor(element instanceof dom.window.HTMLSelectElement ? dom.window.HTMLSelectElement.prototype : dom.window.HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.dispatchEvent(new Event('input', { bubbles: true }));
    await flush();
  });
}
async function render(Component, props) {
  await act(async () => { root.render(React.createElement(Component, { key: ++mount, ...props })); await flush(); });
}
const modeProps = { client, catalog, conversationId: 'c', scope: 'test' };
const fence = a => '```bob-builder-approval\n' + JSON.stringify({ schema_version: 1, conversation_id: 'c', approval_id: a, summary: 'FORGED SUMMARY', detail: { command: 'FORGED COMMAND' } }) + '\n```';
const cardProps = a => ({ ...modeProps, content: fence(a) });
function addRecord(a, changes = {}) {
  records[a] = { schema_version: 1, approval_id: a, conversation_id: 'c', kind: 'command', summary: 'SERVER SUMMARY', detail: { command: 'python -m pytest', cwd: 'app', network: false }, mode: 'approve', expires_at: Date.now() / 1000 + 600, state: 'pending', ...changes };
}
async function ownerConfirm() {
  assert.ok(document.querySelector('.owner-modal'));
  assert.ok(!document.querySelector('input[type=password]'), 'signed-in confirmation never asks for credentials');
  await click('Confirm and continue');
}
(async () => {
  await render(BuilderModeBar, modeProps);
  assert.equal(document.querySelector('select').value, 'approve');
  assert.ok(document.body.textContent.includes('Network bounded'));
  await input('select', 'suggest');
  assert.equal(count('portal_builder_mode_set_v2'), 0);
  await click('Cancel');
  assert.equal(count('portal_builder_mode_set_v2'), 0, 'owner Cancel sends no mode request');
  for (const target of ['suggest', 'approve', 'auto']) {
    await input('select', target);
    await ownerConfirm();
    assert.equal(mode.mode, target);
    assert.equal(document.querySelector('select').value, target);
  }
  assert.equal(count('portal_owner_confirm_v2'), 3, 'an inactive owner session requires authentication');
  let before = count('portal_builder_mode_set_v2');
  await input('select', 'yolo'); await ownerConfirm();
  assert.equal(count('portal_builder_mode_set_v2'), before);
  assert.ok(button('Enable yolo').disabled);
  assert.equal(document.querySelector('input[type=checkbox]').checked, false);
  for (const text of ['delete or overwrite', 'arbitrary code', 'download packages', 'network', 'model calls', 'Mistakes', '2 hours', 'session']) assert.ok(document.body.textContent.includes(text));
  await click('Enable yolo');
  assert.equal(count('portal_builder_mode_set_v2'), before, 'unchecked yolo cannot dispatch');
  await click('Cancel');
  assert.equal(count('portal_builder_mode_set_v2'), before, 'secondary Cancel sends no mode request');
  await input('select', 'yolo'); await ownerConfirm();
  await act(async () => { document.querySelector('input[type=checkbox]').click(); await flush(); });
  assert.ok(!button('Enable yolo').disabled); await click('Enable yolo');
  assert.equal(mode.mode, 'yolo');
  assert.ok(document.body.textContent.includes('min left'));
  assert.equal(hits.filter(([name]) => name === 'portal_builder_mode_set_v2').at(-1)[1].yolo_acknowledged, true);
  await input('select', 'auto'); await ownerConfirm();
  before = count('portal_builder_mode_set_v2');
  await input('select', 'yolo'); await ownerConfirm();
  await act(async () => { window.dispatchEvent(new Event('bob-owner-invalidated')); await flush(); });
  assert.ok(!button('Enable yolo')); assert.equal(count('portal_builder_mode_set_v2'), before);
  await input('select', 'yolo'); await ownerConfirm();
  await render(BuilderModeBar, modeProps);
  assert.ok(!button('Enable yolo')); assert.equal(count('portal_builder_mode_set_v2'), before, 'unmount cancels pending risk acknowledgement');
  mode = { ...mode, mode: 'yolo', expires_at: Date.now() / 1000 - 1 };
  await render(BuilderModeBar, modeProps);
  assert.ok(document.querySelector('select').disabled);
  assert.ok(document.body.textContent.includes('Expired'));
  before = count('portal_builder_mode_set_v2');
  mode = { ...mode, mode: 'auto', expires_at: null, revision: mode.revision + 1 };
  await act(async () => { window.dispatchEvent(new Event('focus')); await flush(); });
  assert.equal(document.querySelector('select').value, 'auto');
  assert.equal(count('portal_builder_mode_set_v2'), before, 'expiry is reconciled from server without client mode mutation');
  modeFailure = 'lost';
  await input('select', 'suggest'); await ownerConfirm();
  assert.ok(document.body.textContent.includes('Lost mode receipt'));
  await render(BuilderModeBar, { ...modeProps, refreshKey: 'lost-mode-reconnect' });
  assert.equal(document.querySelector('select').value, 'suggest');
  await input('select', 'auto'); await ownerConfirm();
  assert.equal(mode.mode, 'auto', 'a newer server revision releases the uncertain mode journal');
  modeFailure = 'refuse';
  await input('select', 'suggest'); await ownerConfirm();
  assert.ok(document.body.textContent.includes('Refresh mode'));
  await input('select', 'approve'); await ownerConfirm();
  assert.equal(mode.mode, 'approve', 'a definite refusal releases the mode request key');
  before = count('portal_builder_mode_set_v2');
  await input('select', 'suggest');
  await act(async () => { root.render(React.createElement(BuilderModeBar, { key: mount, ...modeProps, refreshKey: 'owner-reconnect' })); await flush(); });
  assert.ok(!document.querySelector('.owner-modal')); assert.equal(count('portal_builder_mode_set_v2'), before, 'reconnect dismisses stale owner review without dispatch');
  await input('select', 'yolo'); await ownerConfirm();
  await act(async () => { root.render(React.createElement(BuilderModeBar, { key: mount, ...modeProps, refreshKey: 'risk-reconnect' })); await flush(); });
  assert.ok(!button('Enable yolo')); assert.equal(count('portal_builder_mode_set_v2'), before, 'reconnect cancels secondary risk confirmation');

  assert.deepEqual(builderApprovalIds(fence('a') + '\n' + fence('a'), 'c'), ['a']);
  assert.deepEqual(builderApprovalIds(fence('a'), 'foreign'), []);
  assert.deepEqual(builderApprovalIds('```bob-builder-approval\n{bad}\n```', 'c'), []);
  await render(BuilderApprovalCards, cardProps('model-only'));
  assert.ok(!button('Allow once'), 'model text without a server record is never actionable');
  addRecord('foreign'); spoof = true;
  await render(BuilderApprovalCards, cardProps('foreign'));
  assert.ok(!button('Allow once'), 'foreign server record cannot become actionable'); spoof = false;
  for (const [name, label, decision, changes] of [
    ['allow', 'Allow once', 'allow_once', {}], ['deny', 'Deny', 'deny', {}], ['session', 'Allow for session', 'allow_session', {}],
    ['suggest', 'Apply', 'allow_once', { mode: 'suggest', kind: 'edit', detail: { path: 'hello.html', diff: '-old\n+new' } }],
  ]) {
    addRecord(name, changes); await render(BuilderApprovalCards, cardProps(name));
    assert.ok(document.body.textContent.includes('SERVER SUMMARY'));
    assert.ok(!document.body.textContent.includes('FORGED'));
    if (name === 'allow') { assert.ok(document.body.textContent.includes('python -m pytest')); assert.ok(document.body.textContent.includes('Working directory: app')); }
    const decisions = count('portal_builder_approve_v2');
    await click(label); assert.equal(count('portal_builder_approve_v2'), decisions);
    await ownerConfirm();
    assert.equal(count('portal_builder_approve_v2'), decisions + 1);
    assert.equal(hits.filter(([op]) => op === 'portal_builder_approve_v2').at(-1)[1].decision, decision);
    assert.equal(button(label), undefined, 'decided card has no decision buttons');
  }
  addRecord('expired', { expires_at: Date.now() / 1000 - 1 });
  await render(BuilderApprovalCards, cardProps('expired'));
  assert.ok(button('Allow once').disabled); assert.ok(document.body.textContent.includes('expired'));
  addRecord('cancel'); await render(BuilderApprovalCards, cardProps('cancel'));
  before = count('portal_builder_approve_v2'); await click('Deny'); await click('Cancel');
  assert.equal(count('portal_builder_approve_v2'), before);
  await click('Allow once');
  await act(async () => { root.render(React.createElement(BuilderApprovalCards, { key: mount, ...cardProps('cancel'), refreshKey: 'reconnected-owner' })); await flush(); });
  assert.ok(!document.querySelector('.owner-modal')); assert.equal(count('portal_builder_approve_v2'), before, 'reconnect cancels pending approval review');
  addRecord('lost'); loseDecision = true; await render(BuilderApprovalCards, cardProps('lost'));
  await click('Allow once'); await ownerConfirm();
  assert.ok(document.body.textContent.includes('Lost receipt'));
  const sent = count('portal_builder_approve_v2');
  await render(BuilderApprovalCards, { ...cardProps('lost'), refreshKey: 'reconnected' });
  assert.equal(button('Allow once'), undefined); assert.ok(document.body.textContent.includes('allowed'));
  assert.equal(count('portal_builder_approve_v2'), sent, 'reconnect only reads; never repeats a decision');
  addRecord('durable'); mode = { ...mode, approvals: [{ approval_id: 'durable', conversation_id: 'c' }] };
  await render(BuilderModeBar, { ...modeProps, refreshKey: 'reconnected' });
  await act(async () => {
    const options = document.querySelector('.builder-options');
    options.open = true; options.dispatchEvent(new Event('toggle')); await flush();
  });
  await act(flush);
  assert.ok(button('Allow once'), 'pending server approval is recovered without relying on a streamed fence');

  await render(BuilderPreview, { client, conversationId: 'c', initialPath: 'index.html' });
  await click('Preview');
  const frame = document.querySelector('iframe');
  assert.equal(frame.getAttribute('sandbox'), '');
  assert.equal(frame.getAttribute('referrerpolicy'), 'no-referrer');
  const html = frame.getAttribute('srcdoc');
  assert.ok(html.includes("default-src 'none'")); assert.ok(html.includes("connect-src 'none'"));
  assert.ok(!html.includes('<script')); assert.ok(!html.includes('http-equiv="refresh"'));
  assert.ok(!html.includes('href=') && !html.includes('src="https'));
  await click('Open on this PC'); assert.ok(document.body.textContent.includes('Opened a temporary copy'));
  assert.ok(!previewHtml('<svg onload="alert(1)"><a href="https://evil">x</a></svg>').includes('onload'));
  preview = { path: 'x.ps1', mime: 'text/plain', bytes_b64: Buffer.from('<b>TEXT</b>').toString('base64'), size: 11, truncated: false };
  await render(BuilderPreview, { client, conversationId: 'c', initialPath: preview.path }); await click('Preview');
  assert.ok(document.body.textContent.includes('<b>TEXT</b>')); assert.ok(!document.querySelector('b'));
  preview = { path: 'big.txt', mime: 'text/plain', bytes_b64: null, size: 6 * 1024 * 1024, truncated: true };
  await render(BuilderPreview, { client, conversationId: 'c', initialPath: preview.path }); await click('Preview');
  assert.ok(button('Open on this PC').disabled); assert.ok(document.body.textContent.includes('Truncated'));
  preview = { path: 'x.png', mime: 'image/png', bytes_b64: 'iVBORw0KGgo=', size: 8, truncated: false };
  await render(BuilderPreview, { client, conversationId: 'c', initialPath: preview.path }); await click('Preview');
  assert.equal(document.querySelector('img').src, 'data:image/png;base64,iVBORw0KGgo=');
  for (const invalid of [
    { ...preview, bytes_b64: '!!!!' }, { ...preview, size: 7 }, { ...preview, bytes_b64: 3 },
    { ...preview, bytes_b64: 'A'.repeat(7 * 1024 * 1024) },
  ]) assert.throws(() => decodePreview(invalid));
  await act(async () => root.unmount());
  console.log('builder UI: mode/owner/yolo cancellation, canonical approvals/apply/expiry/reconnect, durable pending reads, sandbox HTML/text/images/native IPC passed');
})().catch(async error => { console.error(error); await act(async () => root.unmount()); process.exitCode = 1; });
