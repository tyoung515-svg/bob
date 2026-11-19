"""S11 scoped batch reads and workspace/snapshot provenance; no providers."""
import base64
import hashlib
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.builder.files import preview
from core.builder.service import Builder
from core.execution.models import ExecutionError
from core.execution.workspaces import snapshot
from core.tool_jobs.checked_runner import apply_edits, read_journal
from core.execution.http_cache import conditional_json
from aiohttp.test_utils import make_mocked_request


@pytest.fixture(autouse=True)
def mock_redis():
    yield


@pytest.fixture
def tmp_path(tmp_path_factory):
    import os
    from pathlib import Path
    if os.name != 'nt':
        return tmp_path_factory.mktemp('s11')
    parent = Path(__file__).parents[1] / 'test-db'
    parent.mkdir(exist_ok=True)
    root = parent / ('s11-' + uuid.uuid4().hex)
    root.mkdir(mode=0o777)
    return root


def setup_batch():
    conversation = uuid.uuid4()
    auth = SimpleNamespace(lane='lane', owner='owner')
    rows = [dict(id='a', conversation_id=conversation, kind='command', summary='Check',
                 detail={'command':'python -m unittest'}, mode='approve', expires_at=time.time()+60,
                 state='pending', execution_state='pending', result=None)]
    pool = SimpleNamespace(fetch=AsyncMock(), fetchval=AsyncMock(return_value=None),execute=AsyncMock())
    async def fetch(sql, *args):
        assert args[:3] == (auth.lane, auth.owner, conversation)
        if 'id=ANY' in sql:
            return [r for r in rows if r['id'] in args[3]]
        if "state='pending'" in sql:
            return [r for r in rows if r['state']=='pending' and r['expires_at']>time.time()]
        return [r for r in rows if r['state']!='pending' or r['expires_at']<=time.time()]
    pool.fetch.side_effect = fetch
    builder = Builder(pool)
    builder.store.mode = AsyncMock(return_value={'conversation_id':conversation, 'project_id':None,
        'mode':'approve', 'expires_at':None, 'revision':0,'session_id':'session','session_until':time.time()+7200})
    builder.network.project = AsyncMock(return_value={'allowlist':[], 'project_extra':[]})
    return builder, auth, str(conversation), rows


@pytest.mark.asyncio
async def test_batch_is_scoped_and_deduplicated():
    builder, auth, conversation, rows = setup_batch()
    reply = await builder.command(auth, 'builder.approvals.get', {'conversation_id':conversation, 'ids':['a','a']})
    assert reply['conversation_id'] == conversation
    assert reply['mode']['approval_records'][0]['state']=='pending'
    assert [r['approval_id'] for r in reply['records']]==['a']
    assert builder.pool.fetch.await_count==3  # pending, recent, one exact-ID batch
    for missing in ('foreign-owner-id', 'foreign-conversation-id'):
        with pytest.raises(ExecutionError, match='approval_not_found'):
            await builder.command(auth, 'builder.approvals.get', {'conversation_id':conversation,'ids':[missing]})
    rows[0]['expires_at']=1
    reply=await builder.command(auth,'builder.approvals.get',{'conversation_id':conversation,'ids':['a']})
    assert reply['records'][0]['state']=='expired'
    assert reply['mode']['approval_records'][0]['state']=='expired'


@pytest.mark.asyncio
@pytest.mark.parametrize('ids', [None, 'a', [True], [''], ['a'*129], ['a']*101])
async def test_batch_has_closed_bounded_schema(ids):
    builder, auth, conversation, _ = setup_batch()
    with pytest.raises(ExecutionError, match='invalid_args'):
        await builder.command(auth,'builder.approvals.get',{'conversation_id':conversation,'ids':ids})
    builder.pool.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatible_mode_read_resolves_pending_without_card_poll():
    builder, auth, conversation, rows = setup_batch()
    first=await builder.command(auth,'builder.mode.get',{'conversation_id':conversation})
    assert first['approvals'][0]['approval_id']=='a'
    rows[0]['state']='allowed'
    after=await builder.command(auth,'builder.mode.get',{'conversation_id':conversation})
    assert after['approvals']==[]
    assert after['approval_records'][0]['state']=='allowed'


def test_calculator_preview_checks_and_publication_are_different_revisions(tmp_path):
    app=tmp_path/'app'
    app.mkdir()
    old='def add(a, b):\n    return a - b\n'
    new=old.replace('a - b','a + b')
    file=app/'calc.py'
    file.write_text(old, encoding='utf-8', newline='\n')
    before=snapshot(tmp_path)
    # A disposable checked snapshot does not mutate the mounted lane workspace.
    checked=tmp_path.parent/('checked-'+uuid.uuid4().hex)
    (checked/'app').mkdir(parents=True)
    (checked/'app/calc.py').write_text(new, encoding='utf-8', newline='\n')
    output=snapshot(checked)
    current=preview(tmp_path,'app/calc.py')
    assert base64.b64decode(current['bytes_b64']).decode()==old
    assert current['provenance']=={'source':'workspace', 'modified_at':file.stat().st_mtime,
                                   'sha256':hashlib.sha256(old.encode()).hexdigest()}
    assert output['digest']!=before['digest']
    result=apply_edits(tmp_path, {'edits':[{'path':'app/calc.py','operation':'replace',
        'pre_sha256':hashlib.sha256(old.encode()).hexdigest(),'content':new}]}, ['app'],
        operation_key='s11-publication', input_digest=before['digest'])
    assert file.read_text(encoding='utf-8')==new
    assert result['output_digest']==output['digest']==snapshot(tmp_path)['digest']
    journal=read_journal(tmp_path)
    assert journal['completed'] and journal['output_digest']==output['digest']
    assert '.bob-checked-edit.json' not in {i['path'] for i in snapshot(tmp_path)['manifest']}


def test_capability_etag_revalidates_and_changes_with_availability():
    request=make_mocked_request('GET','/capabilities')
    response=conditional_json(request, {'available':True})
    assert response.status==200
    etag=response.headers['ETag']
    cached=make_mocked_request('GET','/capabilities',headers={'If-None-Match':etag})
    assert conditional_json(cached,{'available':True}).status==304
    assert conditional_json(cached,{'available':False}).status==200
    assert response.headers['Cache-Control']=='private, no-cache'
