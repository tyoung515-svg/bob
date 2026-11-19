"""S10 policy, server cards, and actual approval persistence; no model calls."""
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.builder.policy import (allowance_keys, command_scope_reason, decide,
                                 read_only_command, session_allowed)
from core.builder.display import approval_display, mode_display
from core.builder.service import Builder
from core.builder.store import BuilderStore
from core.execution.models import Authority, ExecutionError


@pytest.fixture(autouse=True)
def mock_redis():
    yield


@pytest.fixture
def tmp_path(tmp_path_factory):
    import os
    from pathlib import Path
    if os.name != 'nt':
        return tmp_path_factory.mktemp('s10')
    path = Path(__file__).parents[1]/'test-db'/('s10-'+uuid.uuid4().hex)
    path.mkdir(mode=0o777, parents=True)
    return path


@pytest.mark.parametrize('kind,operation', [('write','fs_write'),('edit','fs_edit')])
def test_file_scope_ignores_diff_and_path_but_not_operation_or_root(kind, operation):
    detail = dict(path='app/hello.html',diff='+first',workspace='/project',operation=operation)
    keys = allowance_keys(kind,detail)
    for n in range(4):
        assert decide('approve',kind,{**detail,'path':f'web/file{n}.html','diff':f'+edit {n}'},keys) == 'run'
    assert not session_allowed(kind,{**detail,'workspace':'/other'},keys)
    assert not session_allowed(kind,{**detail,'operation':'checkpoint_restore'},keys)
    other = 'fs_delete' if operation != 'fs_delete' else 'fs_edit'
    assert not session_allowed(kind,{**detail,'operation':other},keys)
    for path in ['../escape','/etc/x','a/../x','.git/config','.bob-state','a/.env','secrets/key','x.pem']:
        assert not session_allowed(kind,{**detail,'path':path},keys)
    assert decide('suggest',kind,detail,keys) == 'propose'


@pytest.mark.parametrize('command', [
    'pwd; ls -la; echo "---"; ls -la app 2>&1 | head -20',
    'pwd\nls -la', '\npwd;\n\nls -la\n', 'pwd &&\nls -la',
    'cat app/a.txt | grep hello && wc -l app/a.txt || echo missing',
    'find . -name "*.html"; stat app/a.txt; file app/a.txt; diff a b',
    'git status --short && git log --oneline -5; git diff; git show',
])
def test_compound_read_only_free_in_approve_auto_but_proposed_in_suggest(command):
    assert read_only_command(command)
    detail = {'command':command,'network':False}
    assert decide('approve','command',detail) == decide('auto','command',detail) == 'run'
    assert decide('suggest','command',detail) == 'propose'
    assert decide('approve','command',{**detail,'network':True}) == 'ask'


def test_all_command_segments_require_independent_program_subcommand_context():
    detail = dict(command='npm test',cwd='app',network=False,workspace='/project')
    keys = allowance_keys('command',detail)
    combined = {**detail,'command':'npm test; npm run build'}
    assert not session_allowed('command',combined,keys)
    keys += allowance_keys('command',{**detail,'command':'npm run build'})
    for command in ['npm test -- --watch; npm run build', 'npm run build && npm test', 'npm test | npm run build']:
        assert session_allowed('command',{**detail,'command':command},keys)
    for changes in [{'cwd':''},{'network':True},{'workspace':'/other'}, {'command':'npm run deploy'}, {'command':'npm test; rm a'}]:
        assert not session_allowed('command',{**combined,**changes},keys)
    assert session_allowed('command',combined,allowance_keys('command',combined))
    module = dict(command='python -m pytest',cwd='',network=False)
    assert not session_allowed('command',{**module,'command':'python -m pip install x'},allowance_keys('command',module))


@pytest.mark.parametrize('command', [
    'echo hi > /etc/x', 'cat ../secret', 'cat /etc/passwd', 'ls $(id)', 'echo `id`',
    '(pwd)', 'eval "pwd"', 'cat <<EOF\nhi\nEOF', 'find . -exec rm x ;',
    'find . -delete', 'rg --pre=evil x', 'git diff --output=x', 'pwd & rm x',
    'echo hi > .bob-state', 'cat .git/config',
    'pwd &&\n', 'pwd; ; ls', 'echo hello > | rm x',
    'if true; then pwd; fi', 'cd app; npm test',
])
def test_unsafe_commands_do_not_inherit_a_program_scope_or_read_only_status(command):
    detail = {'command':command}
    assert not read_only_command(command)
    assert decide('approve','command',detail) == 'ask'
    assert not session_allowed('command',detail,allowance_keys('command',{'command':'echo hi'}))


def test_exact_only_command_requires_a_new_grant_when_changed():
    detail = dict(command='echo $(pwd)',exact_only_reason='Unsupported syntax',workspace='/project')
    keys = allowance_keys('command',detail)
    assert session_allowed('command',detail,keys)
    assert not session_allowed('command',{**detail,'command':'echo $(id)'},keys)
    assert 'Unsupported syntax' in approval_display({'kind':'command'},detail)['scope']


def test_command_redirection_and_symlink_admission(tmp_path):
    detail = dict(command='npm test > output.txt',cwd='',workspace=str(tmp_path))
    assert command_scope_reason(tmp_path,detail) is None
    assert session_allowed('command',detail,allowance_keys('command',{**detail,'command':'npm test'}))
    assert command_scope_reason(tmp_path,{'command':'echo hi > /etc/x'})
    try:
        (tmp_path/'link').symlink_to(tmp_path/'outside')
    except OSError:
        pytest.skip('Host does not permit symlink creation')
    assert 'symlink' in command_scope_reason(tmp_path,{'command':'cat link'})
    assert 'symlink' in command_scope_reason(tmp_path,{'command':'echo hi > link'})


def test_server_display_matches_requested_action_and_has_no_generic_yolo_warning():
    display = approval_display({'kind':'workspace'},{'name':'hello','action':'create'})
    assert display['title'] == 'Create and switch to workspace “hello” (empty folder in your lane)'
    assert 'arbitrary' not in json.dumps(display)
    display = mode_display('approve','auto')
    assert display['title'] == 'Change Builder mode: Approve → Auto' and not display['lines']
    assert 'mistakes' in mode_display('auto','yolo')['lines'][0]
    with pytest.raises(ExecutionError):
        mode_display('approve','model-invented-mode')
    card = BuilderStore.card(dict(id='approval',conversation_id='conversation',kind='write',summary='fs_write',
        detail={'path':'app/hello.py','operation':'fs_write','workspace':'hello','diff':'--- a\n+++ b\n+hello\n+world\n'},
        mode='approve',expires_at=1))
    assert card['display']['title'] == 'Write app/hello.py (+2/−0 lines)'
    assert 'Allow writes, edits and bounded checks in this workspace for this task' in card['display']['scope']
    assert '/project' not in card['display']['scope']


async def test_owner_allow_session_persists_every_segment_and_cannot_migrate_sessions(monkeypatch):
    from core.execution import owner
    monkeypatch.setattr(owner,'require_owner',AsyncMock())
    mode = dict(conversation_id=uuid.uuid4(),project_id=uuid.uuid4(),session_id='session')
    row = dict(id='approval',kind='command',payload={},detail={'command':'npm test; npm run build','workspace':'/project'},
               state='pending',expires_at=time.time()+600,decision_key=None,**mode)
    class Connection:
        def transaction(self): return self
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def fetchrow(self,*args): return row.copy()
        execute = AsyncMock()
    c = Connection()
    builder = Builder(SimpleNamespace(acquire=lambda:c))
    builder.store.approval = AsyncMock(return_value=row)
    builder.store.mode = AsyncMock(return_value=mode)
    builder.execute = AsyncMock(return_value={'exit_code':0})
    auth = Authority('lane','owner','owner',{},'portal')
    await builder.approve(auth,{'approval_id':'approval','decision':'allow_session','request_key':'request'})
    inserts = [call.args for call in c.execute.call_args_list if 'INSERT INTO builder_allowances' in call.args[0]]
    assert len(inserts) == 2
    assert {i[-1] for i in inserts} == set(allowance_keys('command',row['detail']))
    assert all(i[1:5] == ('lane','owner',mode['conversation_id'],'session') for i in inserts)
    mode['session_id'] = 'new-session'
    with pytest.raises(ExecutionError,match='builder_session_expired'):
        await builder.approve(auth,{'approval_id':'approval','decision':'allow_session','request_key':'next'})


@pytest.mark.parametrize('requested',['auto','yolo'])
async def test_actual_summary_route_describes_requested_mode_from_scoped_server_state(monkeypatch,requested):
    from dataclasses import asdict
    from core.execution import api, owner
    from core import permissions
    monkeypatch.setattr(permissions,'verify_execution_vouch',lambda *_:True)
    monkeypatch.setattr(api,'lane_id',lambda:'lane')
    monkeypatch.setattr(api,'policy_epoch',lambda *_:'epoch')
    monkeypatch.setattr(owner,'owner_mapping',lambda *_:True)
    monkeypatch.setattr(owner,'check_epoch',AsyncMock())
    conversation=str(uuid.uuid4())
    auth=Authority('lane','owner','owner',{},'portal',policy_epoch='epoch')
    resource={'conversation_id':conversation,'requested_mode':requested}
    read=AsyncMock(return_value={'mode':'approve','conversation_id':conversation})
    monkeypatch.setattr(Builder,'command',read)
    pool=SimpleNamespace(fetchrow=AsyncMock(return_value={'project_id':uuid.uuid4()}))
    class Request:
        match_info={'operation':'owner.summary'}
        app={api.SERVICE_KEY:SimpleNamespace(store=SimpleNamespace(pool=pool))}
        async def json(self):
            return {'vouch':'fixture','envelope':{'operation':'owner.summary','authority':asdict(auth),
                'args':{'operation':'builder.mode.set','resource':resource}}}
    response=await api.command(Request())
    assert response.status==200,response.text
    result=json.loads(response.text)
    assert result['display']==mode_display('approve',requested)
    read.assert_awaited_once_with(auth,'builder.mode.get',{'conversation_id':conversation})
    assert 'risk_notice' not in result
