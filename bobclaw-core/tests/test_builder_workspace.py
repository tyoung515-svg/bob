"""Workspace metadata confinement and approval admission; no provider calls."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock
import uuid

import pytest

from core.builder.files import browse
from core.builder.policy import decide, allowance_key
from core.builder.service import Builder
from core.builder.workspace import proposal
from core.execution.models import Authority, ExecutionError
from core.portal_resources import ResourceError


@pytest.fixture(autouse=True)
def mock_redis():
    yield


@pytest.fixture
def tmp_path(tmp_path_factory):
    if os.name != 'nt':
        return tmp_path_factory.mktemp('workspace')
    parent = Path(__file__).parents[1]/'test-db'
    parent.mkdir(mode=0o777, exist_ok=True)
    path = parent/('workspace-'+uuid.uuid4().hex)
    path.mkdir(mode=0o777)
    return path


@pytest.mark.parametrize('path', ['..', '../outside', 'a/../b', '/absolute', 'C:/host', 'a\\b', 'a//b', '%2e%2e'])
def test_browse_rejects_unconfined_paths(tmp_path, path):
    with pytest.raises(ResourceError):
        browse(tmp_path, path)


def test_browse_dot_entries_metadata_navigation_and_cap(tmp_path):
    (tmp_path/'.hidden').mkdir()
    (tmp_path/'.hidden/note.txt').write_text('hello')
    (tmp_path/'index.html').write_text('<h1>Hi</h1>')
    entries = browse(tmp_path)['entries']
    assert entries[0]['name'] == '.hidden' and entries[0]['kind'] == 'dir'
    assert entries[1]['name'] == 'index.html' and entries[1]['size'] == 11
    assert entries[1]['mtime'] > 0
    assert browse(tmp_path, '.hidden')['entries'][0]['name'] == 'note.txt'
    for n in range(1001):
        (tmp_path/f'file{n:04d}').touch()
    value = browse(tmp_path)
    assert len(value['entries']) == 1000 and value['truncated']


def test_browse_lists_links_without_following_escapes(tmp_path):
    root = tmp_path/'project'; root.mkdir()
    outside = tmp_path/'outside'; outside.mkdir()
    (outside/'private.txt').write_text('must not read')
    try:
        (root/'escape').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Host does not permit symlink creation')
    assert browse(root)['entries'][0]['kind'] == 'symlink'
    for path in ('escape', 'escape/private.txt'):
        with pytest.raises(ResourceError):
            browse(root, path)


@pytest.mark.parametrize('mode', ['suggest','approve','auto','yolo'])
@pytest.mark.parametrize('operation', ['workspace_create','workspace_mount'])
async def test_workspace_tools_durably_request_approval_in_every_mode(mode, operation, monkeypatch, tmp_path):
    auth = Authority('lane','owner','owner',{},'portal')
    row = dict(project_id=uuid.uuid4(), conversation_id=uuid.uuid4(), session_id='session',
               session_until=time.time()+7200, expires_at=time.time()+7200, mode=mode)
    pool = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(return_value={'id':uuid.uuid4(),'name':'Mounted'}))
    builder = Builder(pool)
    monkeypatch.setattr(builder,'workspace',lambda *_: tmp_path)
    builder.store.mode = AsyncMock(return_value=row)
    builder.store.allowances = AsyncMock(return_value=['forged-session-allowance'])
    approval = {}
    async def get_approval(*_):
        if not approval:
            args = pool.execute.call_args.args
            assert 'INSERT INTO builder_approvals' in args[0]
            approval.update(id=args[1],conversation_id=row['conversation_id'],kind=args[8],mode=args[9],summary=args[10],
                detail=json.loads(args[11]),payload=json.loads(args[12]),expires_at=args[13],state=args[14],
                execution_state='pending',result=None)
        return approval
    builder.store.approval = get_approval
    builder.execute = AsyncMock(return_value={'status':'completed'})
    frames = []
    def writer(value):
        frames.append(value)
        assert approval['state'] == 'pending'
        # Simulate a subsequent owner decision, so the suspended call can finish.
        approval.update(execution_state='completed', result={'status':'completed'})
    args = {'name':'New workspace','purpose':'UI test'} if operation == 'workspace_create' else {'project_id':str(uuid.uuid4())}
    result = await builder.tool(auth,str(row['conversation_id']),operation,args,writer=writer)
    assert approval['kind'] == 'workspace'
    assert approval['detail']['from_project_id'] == str(row['project_id'])
    if mode == 'yolo':
        assert approval['state'] == 'allowed' and not frames
        builder.execute.assert_awaited_once()
    else:
        assert approval['state'] == 'pending' and 'bob-builder-approval' in frames[0]['content']
        builder.execute.assert_not_awaited()
    if mode == 'suggest':
        assert result['status'] == 'proposed'


async def test_mount_refuses_unknown_foreign_and_unregistered_projects(tmp_path, monkeypatch):
    builder = Builder(SimpleNamespace(fetchrow=AsyncMock(return_value=None)))
    auth = Authority('this-lane','owner','owner',{},'portal')
    row = {'project_id':uuid.uuid4()}
    with pytest.raises(ExecutionError,match='project_not_found'):
        await proposal(builder,auth,row,'workspace_mount',{'project_id':str(uuid.uuid4())})
    builder.pool.fetchrow.return_value = {'id':uuid.uuid4(),'name':'Same UUID in another lane'}
    monkeypatch.setattr(builder,'workspace',lambda *_: None)
    with pytest.raises(ExecutionError,match='workspace_unavailable'):
        await proposal(builder,auth,row,'workspace_mount',{'project_id':str(uuid.uuid4())})


@pytest.mark.parametrize('decision', ['allow_project'])
async def test_workspace_refuses_broader_allowances(decision, monkeypatch):
    from core.execution import owner
    monkeypatch.setattr(owner,'require_owner',AsyncMock())
    builder = Builder(None)
    builder.store.approval = AsyncMock(return_value={'kind':'workspace','payload':{},'conversation_id':uuid.uuid4()})
    builder.store.mode = AsyncMock(return_value={})
    with pytest.raises(ExecutionError,match='workspace_requires_allow_once'):
        await builder.approve(Authority('lane','owner','owner',{},'portal'),
                              {'approval_id':'a','request_key':'decision','decision':decision})


def test_workspace_policy_cannot_inherit_session_allowance():
    detail = {'action':'mount','project_id':str(uuid.uuid4())}
    allowance = [allowance_key('workspace',detail)]
    for mode, expected in [('suggest','propose'),('approve','ask'),('auto','ask'),('yolo','run')]:
        assert decide(mode,'workspace',detail,allowance) == expected


async def test_unbound_read_points_to_workspace_tools_instead_of_invalid_project(monkeypatch):
    builder=Builder(None)
    builder.store.mode=AsyncMock(return_value={'project_id':None})
    monkeypatch.setattr(builder,'workspace',lambda *_:None)
    result=await builder.tool(Authority('lane','owner','owner',{},'portal'),'conversation','fs_list',{})
    assert result['tools']==['workspace_create','workspace_mount'] and 'action' not in result
