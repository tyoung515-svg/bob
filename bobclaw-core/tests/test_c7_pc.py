"""PC catalog, context and native-verifier negatives; no provider/host calls."""
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from core.execution.models import Authority,ExecutionError
from core.execution import pc,pc_context
from core.tools import pc_tools


@pytest.fixture(autouse=True)
def mock_redis():
    # These PC boundary tests never enter the model/tool loop or use Redis.
    yield


async def test_pc_info_is_a_fixed_native_request(monkeypatch):
    request = AsyncMock(return_value='native hardware receipt')
    monkeypatch.setattr(pc_tools, 'invoke', request)
    assert await pc_tools.pc_info.ainvoke({'request_key':'hardware'}) == 'native hardware receipt'
    request.assert_awaited_once_with('pc.info', {}, 'hardware')
    assert 'pc_info' in pc_tools.PC_TOOL_NAMES
    assert pc_tools.pc_info in pc_tools.PC_TOOLS
    assert 'pc.info' in pc.OPERATIONS
    assert pc.validate('pc.info', {}) == {}


@pytest.mark.parametrize('args', [{'path':r'C:\task'}, {'command':'whoami'}, {'approved':True},
                                 {'lane':'other'}, {'request_key':'key'}, [], None])
def test_pc_info_rejects_all_operation_arguments(args):
    with pytest.raises(ExecutionError, match='unsupported_fields'):
        pc.validate('pc.info', args)


async def test_pc_info_persists_empty_args_hash_and_native_receipt(monkeypatch):
    receipt = {'payload':{'result':{'status':'done','summary':{'memory':{'total':16}}}},
               'signature':'fixture-native'}

    async def insert(sql, *args):
        assert args[2] == 'pc.info'
        assert json.loads(args[3]) == {}
        assert args[4] == pc.digest(['pc.info', {}])
        return dict(id=args[0], task_id=args[1], args_sha256=args[4], state='done',
                    receipt=json.dumps(receipt), expires_at=time.time()+60)

    pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=insert))
    monkeypatch.setattr('core.execution.chat_records.POOL', pool)
    monkeypatch.setattr('core.execution.owner.check_epoch', AsyncMock())
    monkeypatch.setattr(pc.PC, 'task', AsyncMock(return_value={'id':'test-task'}))
    auth = Authority('lane','owner','owner',{},'portal',credential_epoch=1)
    token = pc_context.current.set(dict(authority=auth.as_dict(), face_id='assistant',
        conversation_id=str(uuid.uuid4()), exp=time.time()+60))
    try:
        result = json.loads(await pc_tools.pc_info.ainvoke({'request_key':'hardware'}))
    finally:
        pc_context.current.reset(token)
    assert result['status'] == 'done'
    assert result['result'] == receipt
    assert result['host_execution'] == 'native_windows_only'


@pytest.mark.parametrize('shell,argv,expected', [
    ('powershell', [name, '-Command', '$c=1; Write-Output $c'],
     ['-Command', '$c=1; Write-Output $c'])
    for name in ['powershell', 'PoWeRsHeLl', 'powershell.exe', 'PowerShell.EXE',
                 'pwsh', 'PwSh.EXE', r'C:\Windows\System32\WindowsPowerShell\v1.0\PowerShell.EXE',
                 'C:/Program Files/PowerShell/7/pwsh.exe']
] + [
    ('cmd', [name, '/C', 'echo hello'], ['/C', 'echo hello'])
    for name in ['cmd', 'CmD', 'cmd.exe', 'CMD.EXE', r'C:\Windows\System32\CMD.EXE']
] + [
    ('powershell', ['-Command', '$c=1; Write-Output $c'], ['-Command', '$c=1; Write-Output $c']),
    ('cmd', ['/C', 'echo hello'], ['/C', 'echo hello']),
    ('powershell', ['cmd.exe', '/C', 'echo hello'], ['cmd.exe', '/C', 'echo hello']),
    ('cmd', ['pwsh.exe', '-Command', 'echo hello'], ['pwsh.exe', '-Command', 'echo hello']),
    ('powershell', ['powershell-helper.exe'], ['powershell-helper.exe']),
])
async def test_pc_run_normalizes_before_request(monkeypatch, shell, argv, expected):
    original = argv.copy()
    request = AsyncMock(return_value='native request result')
    monkeypatch.setattr(pc_tools, 'invoke', request)
    result = await pc_tools.pc_run.ainvoke(dict(
        shell=shell, argv=argv, cwd=r'C:\task', timeout_s=30, request_key='command'))
    request.assert_awaited_once_with('pc.run', dict(
        shell=shell, argv=expected, cwd=r'C:\task', timeout_s=30), 'command')
    assert argv == original
    assert result == 'native request result'


@pytest.mark.parametrize('shell,argv', [
    ('powershell', []), ('powershell', ['powershell']),
    ('powershell', [r'C:\Program Files\PowerShell\7\PWSH.EXE']),
    ('cmd', []), ('cmd', ['CMD.EXE']),
])
async def test_pc_run_refuses_empty_normalized_argv(monkeypatch, shell, argv):
    request = AsyncMock()
    monkeypatch.setattr(pc_tools, 'invoke', request)
    result = json.loads(await pc_tools.pc_run.ainvoke(dict(
        shell=shell, argv=argv, cwd=r'C:\task', timeout_s=30, request_key='empty')))
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'invalid_pc_command'
    assert 'argv must contain arguments' in result['error']['detail']
    request.assert_not_awaited()


async def test_pc_run_persists_normalized_args_and_hash(monkeypatch):
    """Exercise the real request path with a fake store; no native command runs."""
    async def insert(sql, *args):
        stored = json.loads(args[3])
        assert stored == dict(shell='powershell', argv=['-Command', '$c=1; Write-Output $c'],
                              cwd=r'C:\task', timeout_s=30)
        assert args[4] == pc.digest(['pc.run', stored])
        return dict(id=args[0], args_sha256=args[4], state='done', receipt=None,
                    expires_at=time.time()+60)

    pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=insert))
    monkeypatch.setattr('core.execution.chat_records.POOL', pool)
    check = AsyncMock()
    monkeypatch.setattr('core.execution.owner.check_epoch', check)
    monkeypatch.setattr(pc.PC, 'task', AsyncMock(return_value={'id':'test-task'}))
    auth = Authority('lane','owner','owner',{},'portal',credential_epoch=1)
    token = pc_context.current.set(dict(authority=auth.as_dict(), face_id='assistant',
        conversation_id=str(uuid.uuid4()), exp=time.time()+60))
    try:
        await pc_tools.pc_run.ainvoke(dict(shell='powershell',
            argv=['powershell.exe','-Command','$c=1; Write-Output $c'],
            cwd=r'C:\task',timeout_s=30,request_key='stored-command'))
    finally:
        pc_context.current.reset(token)
    check.assert_awaited_once_with(auth)
    pool.fetchrow.assert_awaited_once()


def test_pc_args_cannot_contain_grants_or_caller_identity():
    for extra in ['grant','approved','agent','task_id','lane','credential_epoch']:
        with pytest.raises(ExecutionError):
            pc.validate('pc.write',{'path':r'C:\task\hello.txt','content':'hello',extra:True})
    for bad in [{'shell':'bash','argv':['x'],'cwd':r'C:\task','timeout_s':10},
                {'shell':'powershell','argv':'command','cwd':r'C:\task','timeout_s':10},
                {'shell':'cmd','argv':['/c','echo hello'],'cwd':r'C:\task','timeout_s':True}]:
        with pytest.raises(ExecutionError):pc.validate('pc.run',bad)


def test_pc_context_is_vouched_and_face_conversation_epoch_bound(monkeypatch):
    from core.config import config
    monkeypatch.setattr(config,'BOBCLAW_SECRET','fixture-pc-context-secret')
    tick=int(time.time());conversation=str(uuid.uuid4())
    value={'authority':Authority('lane','owner','owner',{},'portal',credential_epoch=7).as_dict(),
        'conversation_id':conversation,'face_id':'assistant','iat':tick,'exp':tick+60}
    state={'user_id':'owner','conversation_id':conversation,'face_id':'assistant',
        'pc_context':value,'pc_context_vouch':pc_context.signature(value,config.BOBCLAW_SECRET)}
    token=pc_context.bind(state)
    try:assert pc_context.current.get()==value
    finally:pc_context.current.reset(token)
    auto={**value,'face_id':'*'}
    token=pc_context.bind({**state,'face_id':'reviewer','pc_context':auto,'pc_context_vouch':pc_context.signature(auto,config.BOBCLAW_SECRET)})
    try:assert pc_context.current.get()['face_id']=='reviewer'
    finally:pc_context.current.reset(token)
    for change in [{'face_id':'builder'},{'conversation_id':str(uuid.uuid4())},{'user_id':'other'},
                   {'pc_context_vouch':'forged'}]:
        token=pc_context.bind({**state,**change})
        try:assert pc_context.current.get() is None
        finally:pc_context.current.reset(token)


def test_missing_or_untrusted_native_verifier_never_accepts_a_receipt(tmp_path):
    for value in [None,{}, {'host_id':'invented','iat':int(time.time()),'exp':int(time.time())+60}]:
        assert not pc.verify_native(value,'00',key_path=tmp_path/'missing.json')
    key=tmp_path/'public.json';key.write_text('{"public_blob":"00"}')
    assert not pc.verify_native({},'00',key_path=key)


@pytest.mark.parametrize('kind',['agent','human'])
async def test_delegated_callers_cannot_enumerate_or_control_native_pc_queue(kind):
    auth=Authority('lane','owner','owner' if kind=='human' else 'delegated-agent',{},kind,credential_epoch=1)
    for operation in ['pc.pending','pc.claim','pc.receipt','pc.end']:
        with pytest.raises(ExecutionError,match='^native_portal_required$'):
            await pc.PC(None).command(auth,operation,{})


@pytest.mark.parametrize('expiry,proof_valid,expected', [
    (1000, True, 'pc_turn_window_ended'),
    (1000, False, 'pc_context_unavailable'),
    (1601, True, 'pc_context_unavailable'),  # future-issued context
])
async def test_pc_expired_context_is_diagnostic_only(monkeypatch, expiry, proof_valid, expected):
    from core.config import config
    monkeypatch.setattr(config, 'BOBCLAW_SECRET', 'fixture-pc-context-secret')
    monkeypatch.setattr(pc_context.time, 'time', lambda: 1000)
    value = dict(authority=Authority('lane','owner','owner',{},'portal',credential_epoch=7).as_dict(),
                 conversation_id=str(uuid.uuid4()), face_id='*', iat=expiry-600, exp=expiry)
    state = dict(user_id='owner', conversation_id=value['conversation_id'], face_id='assistant',
                 pc_context=value, pc_context_vouch=pc_context.signature(value, config.BOBCLAW_SECRET)
                 if proof_valid else 'forged')
    monkeypatch.setattr('core.execution.chat_records.POOL', SimpleNamespace())
    request, check = AsyncMock(), AsyncMock()
    monkeypatch.setattr(pc.PC, 'request', request)
    monkeypatch.setattr('core.execution.owner.check_epoch', check)
    token = pc_context.bind(state)
    try:
        bound = pc_context.current.get()
        result = json.loads(await pc_tools.invoke('pc.info', {}, 'expired-context'))
        assert result['error']['code'] == expected
        if expected == 'pc_turn_window_ended':
            assert bound['exp'] == 1000 and bound['face_id'] == 'assistant'
            assert 'Nothing is wedged' in result['error']['detail']
            assert 'new message' in result['error']['detail']
        else:
            assert bound is None
    finally:
        pc_context.current.reset(token)
    request.assert_not_awaited()
    check.assert_not_awaited()


async def test_missing_pc_context_retains_unavailable_code(monkeypatch):
    monkeypatch.setattr('core.execution.chat_records.POOL', SimpleNamespace())
    token = pc_context.current.set(None)
    try:
        result = json.loads(await pc_tools.invoke('pc.info', {}, 'missing-context'))
        assert result['error']['code'] == 'pc_context_unavailable'
    finally:
        pc_context.current.reset(token)


@pytest.mark.parametrize('initial_state,expected', [('pending','expired'), ('running','unknown')])
async def test_pc_approval_timeout_explains_what_ran(monkeypatch, initial_state, expected):
    clock = SimpleNamespace(now=1000)
    monkeypatch.setattr(pc.time, 'time', lambda: clock.now)
    args = {}
    row = dict(id='test-request', task_id='test-task', args_sha256=pc.digest(['pc.info',args]),
               state=initial_state, receipt=None, expires_at=1600,
               host_id=None if initial_state == 'pending' else 'native-host')
    async def fetchrow(sql, *params):
        if sql.startswith('INSERT'):
            assert params[-1] == 1600  # unchanged request lifetime
        if sql.startswith('UPDATE'):
            row['state'] = expected
        return row
    async def wait(_):
        clock.now = 1600
    monkeypatch.setattr(pc.asyncio, 'sleep', wait)
    monkeypatch.setattr(pc.PC, 'task', AsyncMock(return_value={'id':'test-task'}))
    pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=fetchrow))
    context = dict(authority=Authority('lane','owner','owner',{},'portal',credential_epoch=1).as_dict(),
                   face_id='assistant', conversation_id=str(uuid.uuid4()))
    events = []
    result = await pc.PC(pool).request(context, 'pc.info', args, 'timeout-request', writer=events.append)
    assert result['status'] == expected
    if expected == 'expired':
        assert 'did not approve' in result['detail'] and '10 minutes' in result['detail']
        assert 'Nothing ran; nothing is wedged' in result['detail']
        assert 'Windows app, under Needs you' in result['detail']
    else:
        assert 'Unknown mutations are never replayed' in result['detail']
        assert 'Nothing ran' not in result['detail']
    assert events[0]['waiting'] is True and events[-1]['waiting'] is False
    for event in events:
        assert event['conversation_id'] == context['conversation_id']
        assert event['agent'] == 'assistant'


async def test_pc_pending_queue_keeps_conversation_and_agent():
    conversation = uuid.uuid4()
    row = dict(id='request-a', task_id='task-a', operation='pc.info', args='{}',
               args_sha256='hash', expires_at=1000, conversation_id=conversation,
               agent='assistant', principal='owner', credential_epoch=1)
    pool = SimpleNamespace(execute=AsyncMock(), fetch=AsyncMock(side_effect=[[row], []]))
    auth = Authority('lane', 'owner', 'owner', {}, 'portal', credential_epoch=1)
    result = await pc.PC(pool).command(auth, 'pc.pending', {})
    assert result['requests'][0]['conversation_id'] == str(conversation)
    assert result['requests'][0]['agent'] == 'assistant'
    assert result['requests'][0]['args'] == {}
    assert pool.fetch.await_args_list[0].args[1:] == ('lane', 'owner', 1)
