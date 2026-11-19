"""BU15: real stage loop/Builder policy with scripted models and worker transport.

No provider, native PC, egress daemon or isolated Linux worker is claimed here.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from core.builder.service import Builder
from core.execution.agent_turn import (
    StageContext, ToolRefusal, TOOL_REFUSALS, TOOL_REFUSAL_HINTS,
    REFUSAL_LIMIT, IDENTICAL_REFUSAL_LIMIT,
)
from core.execution.models import Authority, ExecutionError
from core.execution.workflow_context import current
from test_workflow_controller import harness, drive, ScriptedModel, mock_redis


@pytest.fixture
def shell_transport(harness, monkeypatch):
    """Keep real Builder approvals/dispatch; replace only worker/proxy I/O."""
    from core.worker import client
    from core.builder import network_client
    dispatched, leases = [], []
    original = client.execute_async

    class Lease:
        def __init__(self, builder, auth, row, command, job_id, config, timeout):
            self.token = 'fixture-token'
            self.url = 'http://fixture.invalid'
            self.job_id, self.events, self.approvals = job_id, [], []
            self.closed = False
            leases.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    async def worker(profile, *, cwd, input, **kwargs):
        payload = json.loads(input)
        if payload['operation'] != 'shell_run':
            return await original(profile, cwd=cwd, input=input, **kwargs)
        assert profile == 'builder'
        ctx = current.get()
        dispatched.append({'stage': ctx.stage['key'], 'spec': ctx.spec.copy(),
                           'root': cwd, 'payload': payload})
        # The harness already compiles controller checks in a real scratch copy.
        if ctx.spec.get('deterministic_check'):
            return await original(profile, cwd=cwd, input=input, **kwargs)
        return {'returncode': 0, 'stdout': b'workspace command completed', 'stderr': b'',
                'cleanup': 'idle_proved', 'execution_id': 'bu15-command', 'execution_seconds': .01}

    monkeypatch.setattr(network_client, 'JobNetwork', Lease)
    monkeypatch.setattr(client, 'execute_async', worker)
    return SimpleNamespace(dispatched=dispatched, leases=leases)


def shell_requests(ctx):
    root = str(ctx.controller.root(ctx.record))
    spellings = [None, '', '.', './', root, root+'/', root+'//./', root.replace('/', chr(92))]
    return [{'name': 'shell_run', 'args': {'command': command, **({'cwd': cwd} if cwd is not None else {})},
             'id': command+'-'+str(i)} for command in ('ls', 'pwd') for i, cwd in enumerate(spellings)]


@pytest.mark.parametrize('checks', [False, True])
async def test_build_and_fix_shell_root_aliases_and_check_attribution(harness, shell_transport, monkeypatch, checks):
    h = harness
    h.fail_review = True  # exercise both the Build and Fix write stages
    original = ScriptedModel.ainvoke

    async def invoke(model, messages):
        ctx = current.get()
        if ctx.face == 'builder':
            replies = [m for m in messages if isinstance(m, ToolMessage) and m.name == 'shell_run']
            if not replies:
                return AIMessage(content='', tool_calls=shell_requests(ctx))
            assert all(json.loads(m.content).get('status') != 'refused' for m in replies)
        return await original(model, messages)

    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await h.new(checks=checks)
    run = await drive(h, row)
    assert run['status'] == 'completed', run['reason']
    model_commands = [d for d in shell_transport.dispatched if not d['spec'].get('deterministic_check')]
    assert len(model_commands) == 32  # 16 root spellings/commands in each write stage
    assert any('/fix-' in d['stage'] for d in model_commands)
    assert all(d['payload']['args']['cwd'] == '' for d in model_commands)
    assert all(d['root'] == h.roots[run['project_id']] for d in model_commands)
    assert all(lease.closed for lease in shell_transport.leases)
    deterministic = [d for d in shell_transport.dispatched if d['spec'].get('deterministic_check')]
    assert len(run['report']['checks']) == len(deterministic)
    assert all(c['check_id'] == 'compile' and c['command'] == 'python -m compileall -q .'
               for c in run['report']['checks'])
    assert bool(deterministic) is checks
    assert ('No deterministic check ran.' in run['report']['caveats']) is (not checks)
    receipts = [r for s in run['report']['stages'] for r in s.get('receipts', []) if r['tool'] == 'shell_run']
    assert len(receipts) == 32


@pytest.mark.parametrize('mode', ['auto', 'approve'])
@pytest.mark.parametrize('args', [{'command': 'echo ready > hello.txt'}, {'command': 'ls', 'network': True}])
async def test_nonautomatic_shell_uses_existing_approval_wait(harness, shell_transport, monkeypatch, mode, args):
    original = ScriptedModel.ainvoke
    waits = []

    async def invoke(model, messages):
        if current.get().face == 'builder' and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content='', tool_calls=[{'name': 'shell_run',
                'args': args.copy(), 'id': 'write-command'}])
        return await original(model, messages)

    async def on_wait(row, run, wait):
        if wait['kind'] == 'approval':
            card = harness.store.pool.approvals[wait['record_id']]
            # The driver polls: only the first sight of this wait is before the owner's click.
            if json.loads(card['payload'])['operation'] == 'shell_run' and not waits:
                assert not shell_transport.dispatched
                assert card['state'] == 'pending' and card['mode'] == mode
                waits.append(wait.copy())

    harness.on_wait = on_wait
    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await harness.new(mode=mode, checks=False)
    run = await drive(harness, row)
    assert run['status'] == 'completed', run['reason']
    assert len(waits) == 1
    assert len(shell_transport.dispatched) == 1
    assert not run['report']['checks']


@pytest.fixture
def scope_context(harness, monkeypatch):
    controller = harness.controller
    record = {'coverage': {'limits': {'workflow_tool_rounds': 12},
              'authority': {'workspace_ops': ['read', 'write', 'edit', 'check']}, 'checks': []}}
    ctx = StageContext(controller, {}, record, {'key': 'build'}, {'access': 'write'}, {}, 'builder')
    monkeypatch.setattr(controller, 'validate', AsyncMock())
    monkeypatch.setattr(controller, 'root', lambda record: Path('/workspace'))
    monkeypatch.setattr(controller, 'auth', lambda record: Authority('lane', 'owner', 'owner',
        {'may_touch': ['/workspace/**'], 'may_not_touch': []}, 'human'))
    monkeypatch.setattr(controller, 'receipt', AsyncMock())
    return ctx


@pytest.mark.parametrize('cwd', ['/outside', '/workspace-other', '../workspace', '/workspace/../workspace',
                                 'src/../', 'C:/outside', None, 42, '/workspace/\x00'])
async def test_outside_or_invalid_shell_cwd_is_actionable(scope_context, cwd):
    args = {'command': 'ls', 'cwd': cwd}
    with pytest.raises(ToolRefusal) as exc:
        await scope_context.before_tool('shell_run', args)
    assert exc.value.code == 'scope_denied'
    assert 'omit cwd or pass cwd=""' in exc.value.detail


@pytest.mark.parametrize('cwd', ['', '.', './', './/', '/workspace', '/workspace/', '/workspace//./', '//workspace', 'src'])
async def test_shell_cwd_normalization(scope_context, cwd):
    args = {'command': 'pwd', 'cwd': cwd}
    await scope_context.before_tool('shell_run', args)
    assert args['cwd'] == ('src' if cwd == 'src' else '')


def test_windows_root_alias_is_case_insensitive(scope_context, monkeypatch):
    monkeypatch.setattr(scope_context.controller, 'root', lambda record: 'C:/Work/Project')
    assert scope_context.shell_cwd('c:/work/project/./') == ''
    assert scope_context.shell_cwd('C:'+chr(92)+'Work'+chr(92)+'Project'+chr(92)) == ''
    with pytest.raises(ToolRefusal):
        scope_context.shell_cwd('C:/Work/Other')


@pytest.mark.parametrize('face', ['planner', 'reviewer'])
async def test_read_stages_retain_no_shell_and_offer_file_tools(scope_context, face):
    ctx = scope_context
    ctx.face, ctx.spec = face, {'access': 'read'}
    ctx.bound_names = {'fs_list', 'fs_read', 'fs_search', 'bob_guide'}
    with pytest.raises(ToolRefusal) as exc:
        await ctx.before_tool('shell_run', {'command': 'ls'})
    ctx.begin_tool_reply()
    result = json.loads(await ctx.refuse('shell_run', exc.value, {'command': 'ls'}))
    assert result['error']['code'] == 'workflow_effect_denied'
    assert 'fs_list, fs_read, fs_search' in result['error']['detail']
    assert 'shell_run' not in ctx.bound_names


async def test_missing_check_authority_still_disables_shell(scope_context):
    ctx = scope_context
    ctx.record['coverage']['authority']['workspace_ops'].remove('check')
    with pytest.raises(ToolRefusal, match='workflow_effect_denied'):
        await ctx.before_tool('shell_run', {'command': 'ls'})


@pytest.mark.parametrize('mode,expected', [('suggest', 'proposed'), ('approve', 'executed'),
                                          ('auto', 'executed'), ('yolo', 'executed')])
async def test_all_builder_mode_branches_without_widening_workflow_grants(scope_context, harness, monkeypatch, mode, expected):
    """Suggest/Yolo remain policy-level fixtures: workflow Start still accepts only Auto/Approve."""
    ctx = scope_context
    builder = Builder(harness.store.pool)
    row = {'conversation_id': 'fixture', 'project_id': 'project', 'session_id': 'session',
           'mode': mode, 'expires_at': 10**12, 'session_until': 10**12}
    monkeypatch.setattr(builder.store, 'mode', AsyncMock(return_value=row))
    monkeypatch.setattr(builder.store, 'allowances', AsyncMock(return_value=[]))
    monkeypatch.setattr(builder, 'workspace', lambda auth, row: Path('/workspace'))
    cards = []

    async def approval(auth, identifier):
        card = harness.store.pool.approvals[identifier].copy()
        cards.append(card)
        if len(cards) > 1:
            card['state'] = 'allowed'  # simulate the separate owner's answer
        return card

    monkeypatch.setattr(builder.store, 'approval', approval)
    monkeypatch.setattr(builder, 'execute', AsyncMock(return_value={'status': 'executed'}))
    monkeypatch.setattr(ctx, 'open_wait', AsyncMock(return_value='wait'))
    monkeypatch.setattr(ctx, 'close_wait', AsyncMock())
    # Static pure policy check does not need a real /workspace in this fixture.
    monkeypatch.setattr('core.builder.policy.command_scope_reason', lambda *args: None)
    ctx.record['run'] = {'grant': {'expires_at': '2099-01-01T00:00:00Z'}}
    args = {'command': 'echo ready > hello.txt'}
    await ctx.before_tool('shell_run', args)
    token = current.set(ctx)
    try:
        result = await builder.tool(ctx.controller.auth(ctx.record), 'fixture', 'shell_run', args)
    finally:
        current.reset(token)
    assert result['status'] == expected
    assert builder.execute.await_count == (0 if mode == 'suggest' else 1)
    assert ctx.open_wait.await_count == (0 if mode == 'yolo' else 1)
    assert ctx.close_wait.await_count == (1 if mode in {'auto', 'approve'} else 0)
    assert cards[0]['state'] == ('allowed' if mode == 'yolo' else 'pending')


async def test_two_refused_calls_in_one_reply_then_recovery(harness, monkeypatch):
    original = ScriptedModel.ainvoke
    counts = []

    async def invoke(model, messages):
        ctx = current.get()
        if ctx.stage['key'] == 'plan':
            replies = [m for m in messages if isinstance(m, ToolMessage)]
            if not replies:
                return AIMessage(content='', tool_calls=[
                    {'name': 'work_catalog', 'args': {}, 'id': 'one'},
                    {'name': 'work_prepare', 'args': {}, 'id': 'two'}])
            counts.append(ctx.refusals)
            assert len(replies) == 2
            return AIMessage(content='The plan uses the supplied evidence.')
        return await original(model, messages)

    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await harness.new()
    run = await drive(harness, row)
    assert run['status'] == 'completed', run['reason']
    assert counts == [1]


@pytest.mark.parametrize('rounds,terminal', [(5, 'completed'), (6, 'failed')])
async def test_six_distinct_refusal_replies_are_the_limit(harness, monkeypatch, rounds, terminal):
    original = ScriptedModel.ainvoke

    async def invoke(model, messages):
        if current.get().stage['key'] == 'plan':
            count = sum(isinstance(m, ToolMessage) for m in messages)
            if count < rounds:
                return AIMessage(content='', tool_calls=[{'name': 'work_catalog',
                    'args': {'different': count}, 'id': str(count)}])
            return AIMessage(content='The plan is ready.')
        return await original(model, messages)

    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await harness.new()
    run = await drive(harness, row)
    assert REFUSAL_LIMIT == 6
    assert run['status'] == terminal, run['reason']
    plan = next(s for s in run['stages'] if s['key'] == 'plan')
    assert plan['calls'] == (rounds if terminal == 'failed' else rounds+1)
    if terminal == 'failed':
        assert run['reason_code'] == 'tool_refusal_limit'


async def test_identical_arguments_guard_and_mixed_reply_success(scope_context):
    ctx = scope_context
    denial = ToolRefusal('scope_denied', 403)
    ctx.begin_tool_reply()
    await ctx.refuse('fs_read', denial, {'path': '../x'})
    ctx.tool_succeeded()
    ctx.finish_tool_reply()
    assert ctx.refusals == 1  # a success beside the refusal does not erase it
    ctx.begin_tool_reply()
    ctx.tool_succeeded()
    ctx.finish_tool_reply()
    assert ctx.refusals == 0
    for i in range(IDENTICAL_REFUSAL_LIMIT):
        ctx.begin_tool_reply()
        if i < IDENTICAL_REFUSAL_LIMIT-1:
            await ctx.refuse('fs_read', denial, {'path': '../same'})
        else:
            with pytest.raises(ExecutionError, match='tool_refusal_limit'):
                await ctx.refuse('fs_read', denial, {'path': '../same'})
        ctx.finish_tool_reply()
    assert ctx.refusals == 3 and IDENTICAL_REFUSAL_LIMIT == 3


async def test_step_limit_still_bounds_refusal_recovery(harness, monkeypatch):
    original = ScriptedModel.ainvoke
    async def invoke(model, messages):
        ctx = current.get()
        if ctx.stage['key'] == 'plan':
            ctx.round_limit = 2
            return AIMessage(content='', tool_calls=[{'name': 'work_catalog',
                'args': {'different': ctx.model_calls}, 'id': str(ctx.model_calls)}])
        return await original(model, messages)
    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await harness.new()
    run = await drive(harness, row)
    # The controller reports an exhausted step limit as 'expired' (unchanged behaviour).
    assert run['status'] == 'expired' and run['reason_code'] == 'tool_round_limit'
    assert next(s for s in run['stages'] if s['key'] == 'plan')['calls'] == 2


@pytest.mark.parametrize('code,sentence', [
    ("summary_tools_forbidden", "Return the summary using the supplied evidence without calling tools."),
    ("scope_denied", "Paths are relative to the workspace root; use a relative path such as `src/app.py`."),
    ("workflow_effect_denied", "Use the tools available to this stage and report any work that needs separate authority."),
    ("check_not_granted", "Leave deterministic checks to the controller using the checks frozen on the grant card."),
    ("skill_unavailable", "Choose a skill listed as available for this stage or continue with its available tools."),
    ("tool_not_bound", "Choose a tool from the available tools listed for this stage."),
    ("builder_policy_refused", "Use an operation permitted by the chosen Builder mode or report that the owner must change the mode."),
    ("invalid_tool_arguments", "Match the tool schema and include its required arguments."),
    ("invalid_pc_command", "Use a command accepted by the native PC request schema and its existing owner approval."),
    ("invalid_pc_path", "Use a path within the folder granted through the native PC request."),
    ("pc_write_too_large", "Split the PC write into smaller requests within the native size limit."),
    ("invalid_pc_preimage", "Read the PC file again and use its current version for the change."),
    ("unsupported_fields", "Remove fields that are not listed in the tool schema."),
    ("unsupported_builder_operation", "Choose one of the Builder tools available to this stage."),
    ("invalid_task_report", "Pass a nonempty task report of at most 6000 characters."),
    ("invalid_command", "Pass a nonempty command of at most 65536 UTF-8 bytes."),
    ("invalid_command_options", "Pass an integer timeout_s from 1 to 600 and a Boolean network value."),
    ("change_too_large", "Split the change into smaller writes or edits within the tool size limit."),
    ("edit_match_not_unique", "Read the file and supply old text that matches exactly once."),
    ("shared_file_refused", "Choose an ordinary workspace file without shared links."),
    ("listing_too_large", "List a smaller subdirectory inside the workspace."),
    ("search_too_large", "Narrow the search pattern or file glob inside the workspace."),
    ("builder_request_too_large", "Split the request into smaller tool calls within the worker transport limit."),
    ("builder_preimage_changed", "Read the file again before proposing a change against its current content."),
    ("preview_changed", "Read or preview the file again to obtain its current content."),
    ("checkpoint_not_found", "Call checkpoint_list and choose an existing checkpoint."),
])
async def test_every_refusal_has_recovery_sentence_and_specific_detail(scope_context, code, sentence):
    ctx = scope_context
    assert set(TOOL_REFUSAL_HINTS) == set(TOOL_REFUSALS)
    ctx.begin_tool_reply()
    result = json.loads(await ctx.refuse('fixture_tool', ToolRefusal(code, 403, 'Specific recovery detail.'), {}))
    detail = result['error']['detail']
    assert detail == ('The tool fixture_tool was refused because '+TOOL_REFUSALS[code]+'. '
                      +sentence+' Specific recovery detail.')
    assert TOOL_REFUSAL_HINTS[code] == sentence
    assert ctx.last_refusal == ('fixture_tool', TOOL_REFUSALS[code])


async def test_encoded_specific_detail_is_not_dropped(scope_context):
    ctx = scope_context
    with pytest.raises(ToolRefusal) as exc:
        await ctx.tool_result('shell_run', json.dumps({'status': 'refused',
            'error': {'code': 'scope_denied', 'detail': 'This tool needs a scope covering the complete workspace.'}}))
    result = json.loads(await ctx.refuse('shell_run', exc.value, {'command': 'ls'}))
    assert 'This tool needs a scope covering the complete workspace.' in result['error']['detail']


async def test_malformed_arguments_do_not_break_refusal_fingerprinting(scope_context):
    result = json.loads(await scope_context.refuse('shell_run', ToolRefusal('invalid_tool_arguments', 403),
                                                 {'timeout_s': float('nan')}))
    assert result['status'] == 'refused'
    assert scope_context.refusals == 1


@pytest.mark.parametrize('checks', [False, True])
async def test_model_check_command_is_only_tool_activity(harness, shell_transport, monkeypatch, checks):
    """Even exact frozen-command text does not promote a model call to a check."""
    original = ScriptedModel.ainvoke
    command = 'python -m compileall -q .'

    async def invoke(model, messages):
        if current.get().face == 'builder' and not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content='', tool_calls=[{'name': 'shell_run',
                'args': {'command': command}, 'id': 'model-check'}])
        return await original(model, messages)

    monkeypatch.setattr(ScriptedModel, 'ainvoke', invoke)
    row = await harness.new(checks=checks)
    run = await drive(harness, row)
    assert run['status'] == 'completed', run['reason']
    model_calls = [d for d in shell_transport.dispatched if not d['spec'].get('deterministic_check')]
    check_calls = [d for d in shell_transport.dispatched if d['spec'].get('deterministic_check')]
    assert len(model_calls) == 1 and model_calls[0]['payload']['args']['command'] == command
    assert len(check_calls) == (1 if checks else 0)
    assert len(run['report']['checks']) == len(check_calls)
    assert all(c['check_id'] == 'compile' and c['snapshot_digest'] for c in run['report']['checks'])
    assert ('No deterministic check ran.' in run['report']['caveats']) is (not checks)
