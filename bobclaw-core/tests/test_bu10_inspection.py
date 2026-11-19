"""BU10: real read policies, signed boundaries, templates and offline git reads."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from core import inspection as reads
from core.execution.models import Authority, ExecutionError
from core.portal_resources import ResourceError
from core.inspection_api import signature, valid
from core.tools.inspection import INSPECTION_TOOLS
from test_configured_catalog import lane, configure


@pytest.fixture
def rig(lane, monkeypatch):
    configure(lane, 'c', BOB_LANE_DEFAULT_BACKEND='deepseek_v4_flash')
    from core.execution.api import lane_id, policy_epoch
    from core.config import config
    monkeypatch.setattr(config,'BOBCLAW_SECRET','bu10-test-signing-fixture')
    monkeypatch.setattr('core.execution.owner.owner_mapping',lambda owner:owner=='owner')
    monkeypatch.setattr('core.execution.owner.check_epoch',AsyncMock())
    auth = Authority(lane_id(),'owner','owner',{},'portal',policy_epoch=policy_epoch(config.BOBCLAW_SECRET))
    identifier = uuid.uuid4()
    root = lane/'projects'/str(identifier)
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    row = dict(id=identifier,name='Game',description='Existing game',instructions='Work on this game.',updated_at=now)
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row),fetch=AsyncMock(return_value=[]),execute=AsyncMock())
    admission = SimpleNamespace(store=SimpleNamespace(pool=pool))
    monkeypatch.setattr('core.builder.service.Builder.workspace',lambda self,a,r:root)
    return reads.Inspection(admission),auth,identifier,root,pool


async def command(rig, op, **args):
    service,auth,_,_,_ = rig
    return await service.command(auth,op,args,visible_faces=['assistant','planner'])


async def test_owned_projects_brief_activity_and_pagination(rig):
    _,_,id,root,pool = rig
    pool.fetch.return_value=[dict(id=uuid.uuid4(),title='Game notes',updated_at=datetime.now(timezone.utc))]
    out = await command(rig,'project_read',project_id=str(id))
    assert out['instructions']=='Work on this game.' and out['has_managed_workspace']
    assert len(out['conversations'])==1 and out['last_activity']
    pool.fetch.return_value=[{'id':id},{'id':id}]
    # A separate fetch serves the conversation metadata inside each record.
    pool.fetch.side_effect=[[{'id':id},{'id':id}],[]]
    out = await command(rig,'projects_list',limit=1)
    assert len(out['items'])==1 and out['next_offset']==1
    assert pool.fetchrow.call_args.args[2]=='owner'
    pool.execute.assert_not_called()


@pytest.mark.parametrize('case',['owner','lane','scope','missing_identity','stale_policy'])
async def test_other_owner_lane_scope_and_missing_authority_refused_before_io(rig,case):
    service,auth,id,_,pool = rig
    auth = {'owner':replace(auth,owner='foreign'), 'lane':replace(auth,lane='foreign'),
        'scope':replace(auth,token_type='agent',scope={'may_touch':['/other/*']}),
        'missing_identity':replace(auth,owner=''), 'stale_policy':replace(auth,policy_epoch='old')}[case]
    with pytest.raises(ExecutionError):
        await service.command(auth,'project_read',{'project_id':str(id)},visible_faces=['assistant'])
    pool.fetchrow.assert_not_called()


async def test_foreign_project_row_and_missing_workspace(rig,monkeypatch):
    service,auth,id,_,pool = rig
    pool.fetchrow.return_value=None
    with pytest.raises(ExecutionError,match='project_not_found'):
        await command(rig,'project_read',project_id=str(id))
    pool.fetchrow.return_value={'id':id}
    monkeypatch.setattr('core.builder.service.Builder.workspace',lambda *a:None)
    for op in ('project_files_list','project_git'):
        with pytest.raises(ExecutionError,match='workspace_unavailable'):
            await command(rig,op,project_id=str(id))


@pytest.mark.parametrize('path',['../outside','/etc/passwd','C:\\private','nested/../file',
    '.env','lane.env','providers.env','.git/config','credentials/value','vault/data.json',
    'auth.json','credential-journal.json','gateway.db','records.sqlite3','private.key','x.env.local'])
async def test_workspace_reads_refuse_traversal_credentials_and_control(rig,path):
    with pytest.raises((ExecutionError,ResourceError)):
        await command(rig,'project_files_read',project_id=str(rig[2]),path=path)


async def test_read_tools_have_no_write_mode_binding_or_approval_path(rig,monkeypatch):
    _,_,id,root,pool = rig
    (root/'game.txt').write_bytes(b'one\ntwo\n')
    (root/'auth.json').write_text('private')
    fail = AsyncMock(side_effect=AssertionError('mutation/binding path entered'))
    monkeypatch.setattr('core.builder.service.Builder.tool',fail)
    monkeypatch.setattr('core.builder.store.BuilderStore.mode',fail)
    out = await command(rig,'project_files_read',project_id=str(id),path='game.txt',max_bytes=4)
    assert out['content']=='one\n' and out['next_offset']==4 and out['truncated']
    assert (await command(rig,'project_files_list',project_id=str(id)))['items']==['game.txt']
    for extra in ({'content':'bad'},{'conversation_id':'other'},{'mode':'yolo'}):
        with pytest.raises(ExecutionError):
            await command(rig,'project_files_read',project_id=str(id),path='game.txt',**extra)
    with pytest.raises(ExecutionError):
        await command(rig,'fs_write',path='game.txt',content='bad')
    assert (root/'game.txt').read_text()=='one\ntwo\n'
    fail.assert_not_called(); pool.execute.assert_not_called()


async def test_agent_file_exclusions_apply_before_read_list_and_search(rig,monkeypatch):
    service,auth,id,root,_ = rig
    (root/'allowed.txt').write_text('needle')
    (root/'denied.txt').write_text('needle private')
    scope={'may_touch':[str(root),str(root/'*')], 'may_not_touch':[str(root/'denied.txt')]}
    agent=replace(auth,token_type='agent',scope=scope)
    with pytest.raises(ExecutionError):
        await service.command(agent,'project_files_read',{'project_id':str(id),'path':'denied.txt'},visible_faces=['assistant'])
    assert (await service.command(agent,'project_files_list',{'project_id':str(id)},visible_faces=['assistant']))['items']==['allowed.txt']
    seen=[]
    async def worker(self,a,r,op,args):
        seen.append((op,args))
        from core.worker.builder_runner import execute
        result=execute({'operation':op,'args':args},r)
        return {**result,'exit_code':0,'stdout':'must not escape'}
    monkeypatch.setattr('core.builder.service.Builder.worker',worker)
    out=await service.command(agent,'project_files_search',{'project_id':str(id),'pattern':'needle'},visible_faces=['assistant'])
    assert [m['path'] for m in out['matches']]==['allowed.txt']
    assert 'stdout' not in out and seen[0][0]=='fs_search' and seen[0][1]['_read_scope']==scope


@pytest.mark.parametrize('path',['../escape','lane.env','services/core.env','auth.json',
    'credentials.json','vault/data.md','credential-journal.json','data/facts.json',
    'family/other/MEMORY.md','gateway.db','nested/sign-in.json','nested/sign_in.json',
    'auth-store.json','oauth/session.json','.secrets/key','unknown.bin'])
def test_source_paths_exclude_owner_data_under_program_directory(tmp_path,path):
    with pytest.raises((ResourceError,ExecutionError)):
        reads.source_read(tmp_path,path)


def test_source_reads_lists_search_bounds_and_links(tmp_path,monkeypatch):
    # Deployed files are root-owned; tests may run as an ordinary Linux user.
    if os.name=='posix' and os.getuid()!=0:
        pytest.skip('installed source ownership fixture requires root')
    (tmp_path/'code.py').write_text('answer = 42\n')
    (tmp_path/'lane.env').write_text('never return this')
    (tmp_path/'credentials.json').write_text('never return this')
    assert reads.source_listing(tmp_path)['items']==['code.py']
    assert reads.source_read(tmp_path,'code.py',0,6)['content']=='answer'
    assert reads.source_search(tmp_path,'42')['matches'][0]['path']=='code.py'
    with pytest.raises(ExecutionError): reads.source_read(tmp_path,'code.py',0,300000)
    os.link(tmp_path/'code.py',tmp_path/'alias.py')
    with pytest.raises(ExecutionError,match='shared_file_refused'):
        reads.source_read(tmp_path,'alias.py')


async def test_source_components_are_fixed_and_report_version(rig,tmp_path,monkeypatch):
    service,auth,_,_,_ = rig
    (tmp_path/'code.py').write_text('answer = 42')
    monkeypatch.setattr(reads,'SOURCE_ROOTS',{'bobclaw-core':tmp_path})
    out=await command(rig,'bob_source_list',component='bobclaw-core')
    assert out['component']=='bobclaw-core' and set(out['version'])=={'build_id','git_rev'}
    assert str(tmp_path) not in json.dumps(out)
    for component in ('windows-app','../other','/opt/bob'):
        with pytest.raises(ExecutionError):
            await command(rig,'bob_source_list',component=component)
    agent=replace(auth,token_type='agent',scope={})
    with pytest.raises(ExecutionError):
        await service.command(agent,'bob_source_list',{'component':'bobclaw-core'},visible_faces=['assistant'])


@pytest.mark.parametrize('state',['repository','unborn','detached','folder'])
async def test_git_summary_uses_fixed_offline_worker_commands(rig,monkeypatch,state):
    _,auth,id,root,pool=rig
    def git(*args):
        result=subprocess.run(['git',*args],cwd=root,capture_output=True)
        assert result.returncode==0,result.stderr
        return result
    if state!='folder':
        git('init','-b','main')
        if state!='unborn':
            (root/'game.txt').write_text('initial')
            git('add','game.txt')
            git('-c','user.name=Test','-c','user.email=test@example.invalid','-c','core.hooksPath=/dev/null','commit','-m','First game')
            if state=='detached': git('checkout','--detach')
            (root/'game.txt').write_text('changed')
    seen=[]
    async def worker(profile,*,cwd,args,timeout,output_limit):
        seen.append((profile,args))
        result=subprocess.run(['git',*args],cwd=cwd,capture_output=True)
        return {'cleanup':'idle_proved','returncode':result.returncode,'stdout':result.stdout}
    monkeypatch.setattr('core.worker.client.execute_async',worker)
    out=await command(rig,'project_git',project_id=str(id))
    if state=='folder':
        assert out=={'is_repository':False} and not seen
    else:
        assert out['is_repository'] and out['branch']==('detached' if state=='detached' else 'main')
        assert out['has_uncommitted_changes']==(state!='unborn')
        assert len(out['commits'])==(0 if state=='unborn' else 1)
        if out['commits']: assert out['commits'][0]['subject']=='First game' and out['commits'][0]['date']
        assert all(p=='git' and '--no-optional-locks' in args for p,args in seen)
        assert len(seen)==4
    pool.execute.assert_not_called()


async def test_git_uncertain_cleanup_is_not_a_success(rig,monkeypatch):
    (rig[3]/'.git').mkdir()
    monkeypatch.setattr('core.worker.client.execute_async',AsyncMock(return_value={'cleanup':'unknown'}))
    with pytest.raises(ExecutionError,match='git_observation_incomplete'):
        await command(rig,'project_git',project_id=str(rig[2]))


async def test_git_cannot_turn_partial_agent_file_scope_into_worktree_access(rig,monkeypatch):
    service,auth,id,root,_=rig
    worker=AsyncMock(side_effect=AssertionError('partial scope reached git'))
    monkeypatch.setattr('core.worker.client.execute_async',worker)
    for scope in ({'may_touch':[str(root)]},
            {'may_touch':[str(root),str(root/'*')],'may_not_touch':[str(root/'denied.txt')]}):
        with pytest.raises(ExecutionError):
            await service.command(replace(auth,token_type='agent',scope=scope),'project_git',
                {'project_id':str(id)},visible_faces=['assistant'])
    worker.assert_not_called()


def test_signed_rpc_binds_faces_scope_arguments_expiry_and_operation():
    now=int(time.time()); secret='test signing material'
    envelope={'authority':{'owner':'owner'},'faces':['assistant'],'operation':'project_read',
        'args':{'project_id':'project'},'iat':now,'exp':now+30}
    proof=signature(envelope,secret)
    assert valid(envelope,proof,secret)
    for changes in ({'faces':['reviewer']},{'operation':'fs_write'},{'args':{}},{'exp':now-1},
                    {'authority':{'owner':'foreign'}},{'extra':'unsigned'}):
        assert not valid({**envelope,**changes},proof,secret)


async def test_settings_are_selected_fields_not_config_dump(rig,monkeypatch):
    from core.faces.registry import FaceRegistry
    registry=FaceRegistry()
    monkeypatch.setattr('core.faces.registry.get_default_registry',lambda:registry)
    service,auth,_,_,_=rig
    service.admission_workflows=AsyncMock(return_value={'items':[{'id':'saved','revision':1,
        'definition':{'name':'Game workflow','api_key':'secret-canary'},'account_id':'private'}]})
    from core.lane_config import get_lane
    get_lane().values['SECRET_CANARY']='never-return-this'
    out=await service.command(auth,'bob_installation',{},visible_faces=['assistant'])
    assert [f['id'] for f in out['agents']]==['assistant']
    assert out['agents'][0]['default_model']=='lane-deepseek-model'
    assert out['pc']['connected'] is None
    text=json.dumps(out)
    assert all(word not in text for word in ('secret-canary','account_id','api_key','never-return-this',str(rig[3])))
    assert out['saved_workflows']['items'][0]['name']=='Game workflow'


async def test_prompt_identity_conversation_rules_and_exact_read_inventory(lane,monkeypatch):
    configure(lane,'c')
    from core.faces.registry import FaceRegistry
    registry=FaceRegistry()
    names={t.name for t in INSPECTION_TOOLS}
    for face in registry.all_faces():
        prompt=face.system_prompt
        assert ('You are Bob,' if face.id=='assistant' else "You are Bob's "+face.name) in prompt
        assert 'another Bob install or server' in prompt
        assert (names & set(registry.get_allowed_tools(face.id)))==(names if face.id in {'assistant','planner'} else set())
    for face in ('assistant','planner'):
        prompt=registry.get_system_prompt(face)
        assert 'extending-bob' in prompt and 'State a limit plainly as a limit.' in prompt
        assert 'You cannot read' not in prompt and 'do not search the PC' not in prompt
    assistant=registry.get_system_prompt('assistant')
    assert 'ask one or two questions' not in assistant
    assert 'Do not tell the user their message was cut off or incomplete.' in assistant
    for tool in INSPECTION_TOOLS:
        assert not ({'owner','lane','mode','conversation_id','content','operation'} & set(tool.args))


async def test_new_guide_topic_is_identical_over_mcp(lane,monkeypatch):
    configure(lane,'c')
    from core.faces.registry import FaceRegistry
    from core.guide import render
    from core.mcp.server import MCPConfig, build_server
    registry=FaceRegistry()
    monkeypatch.setattr('core.faces.registry.get_default_registry',lambda:registry)
    expected=await render('extending-bob',registry=registry,face_id='assistant')
    assert 'extending-bob:' in (await render(registry=registry))['content']
    for face in registry.all_faces():
        assert (await render('extending-bob',registry=registry,face_id=face.id))['content']==expected['content']
    class Response:
        status=200
        async def text(self):return json.dumps(expected)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Session:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def request(self,*args,**kwargs):return Response()
    monkeypatch.setattr('core.mcp.server.aiohttp.ClientSession',Session)
    mcp=build_server(MCPConfig('test-credential','http://gateway',['assistant']))
    tools={t.name:t for t in mcp._tool_manager.list_tools()}
    assert {t.name for t in INSPECTION_TOOLS}<=set(tools)
    assert json.loads(await tools['bob_guide'].fn('extending-bob','assistant'))==expected
    out=json.loads(await tools['project_files_read'].fn(project_id='project',path='game.txt'))
    assert out==expected
    restricted=build_server(MCPConfig('test-credential','http://gateway',['reviewer']))
    assert not ({t.name for t in INSPECTION_TOOLS}&{t.name for t in restricted._tool_manager.list_tools()})


async def test_core_read_rpc_and_chat_context_recheck_live_authority(rig,monkeypatch):
    from core import inspection_api as api
    from core.config import config
    from core.execution.api import SERVICE_KEY
    from core.tools.workflow_context import signature as chat_signature
    service,auth,id,root,pool=rig
    (root/'game.txt').write_text('existing game')
    pool.fetchval=AsyncMock(return_value=id)
    class Request:
        app={SERVICE_KEY:service.admission}
        match_info={'operation':'project_files_read'}
        async def json(self):return self.body
    request=Request()
    now=int(time.time())
    envelope={'authority':auth.as_dict(),'faces':['assistant'],'operation':'project_files_read',
        'args':{'project_id':str(id),'path':'game.txt'},'iat':now,'exp':now+30}
    request.body={'envelope':envelope,'vouch':api.signature(envelope,config.BOBCLAW_SECRET)}
    response=await api.command(request)
    assert response.status==200 and json.loads(response.text)['content']=='existing game'
    request.body['envelope']={**envelope,'authority':{**auth.as_dict(),'owner':'foreign'}}
    assert (await api.command(request)).status==403
    value={'authority':auth.as_dict(),'conversation_id':str(uuid.uuid4()),'face_id':'assistant',
        'inspection_faces':['assistant'],'iat':now,'exp':now+60}
    request.body={'context':value,'vouch':chat_signature(value,config.BOBCLAW_SECRET),'args':envelope['args']}
    assert (await api.chat_command(request)).status==200
    # Even a correctly signed legacy-open chat context cannot bypass face scope.
    value['inspection_faces']=['reviewer']
    request.body['vouch']=chat_signature(value,config.BOBCLAW_SECRET)
    assert (await api.chat_command(request)).status==403
    pool.execute.assert_not_called()
