"""Builder policy and actual file operations; no provider/model calls."""
import base64
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest

from core.builder.policy import decide, safe_command, allowance_key
from core.builder.files import read, preview
from core.builder.service import Builder, invoke
from core.builder.summary import summary
from core.execution.models import Authority, ExecutionError
from core.portal_resources import ResourceError
from core.worker.builder_runner import execute
from core.worker.builder_profile import project_root


@pytest.fixture(autouse=True)
def mock_redis():
    # These units exercise Builder directly; the suite-wide Redis fixture
    # otherwise imports unrelated model transports (optional on this PC).
    yield


@pytest.fixture
def tmp_path(tmp_path_factory):
    import os
    if os.name != 'nt':
        return tmp_path_factory.mktemp('builder')
    # Python 3.13's mode=0700 temp DACL is inaccessible to this managed
    # Windows runner. A fresh repo-local directory inherits the workspace ACL.
    parent=Path(__file__).parents[1]/'test-db'
    parent.mkdir(mode=0o777,exist_ok=True)
    path=parent/('builder-'+uuid.uuid4().hex)
    path.mkdir(mode=0o777)
    return path


@pytest.mark.parametrize('command',['ls','ls -al','cat a.txt','python -m pytest','python -m pytest -q tests',
                                    'npm test','git status','git status --short','git diff','git diff --stat'])
def test_safe_auto_commands(command):
    assert safe_command(command)
    assert decide('auto','command',{'command':command,'network':False})=='run'


@pytest.mark.parametrize('command',['curl example.com','pip install a','npm install','rm -rf /','sudo ls',
    'systemctl status a','ls; curl a','cat $(id)','git -c core.pager=sh diff','git diff --output=outside',
    'python -c "pass"','python -m pytest -p evil','npm test -- --install','ls /','cat ../secret',
    'cat > target','ls\nwhoami','ls | sh','ls `id`','cat "/etc/passwd"'])
def test_unsafe_auto_commands_ask(command):
    assert not safe_command(command)
    assert decide('auto','command',{'command':command})=='ask'


def test_modes_network_and_expiry(monkeypatch):
    detail={'command':'ls','network':False}
    assert decide('suggest','command',detail)=='propose'
    assert decide('approve','write',{'path':'x'})=='ask'
    assert decide('auto','edit',{'path':'x'})=='run'
    assert decide('auto','command',{**detail,'network':True})=='ask'
    assert decide({'mode':'yolo','expires_at':1},'command',{'command':'curl x'})=='ask'
    assert decide({'mode':'yolo','expires_at':10**12},'command',detail)=='run'
    assert decide('invalid','write',{})=='refuse'
    allowed=[allowance_key('command',detail)]
    assert decide('approve','command',detail,allowed)=='run'
    assert decide('approve','command',{**detail,'command':'ls -al'},allowed)=='run'
    assert decide('approve','command',{**detail,'command':'ls; rm a'},allowed)=='ask'
    # Read-only commands now run in approve regardless of a session grant.
    assert decide('approve','command',{**detail,'cwd':'other'},allowed)=='run'
    assert decide('suggest','command',detail,allowed)=='propose'


def run(root, operation, **args):
    return execute({'operation':operation,'args':args,'network':False},root)


def test_any_file_type_unique_edit_delete_and_binary_preview(tmp_path):
    run(tmp_path,'fs_write',path='web/index.html',content='<h1>Hello</h1>')
    run(tmp_path,'fs_edit',path='web/index.html',old='Hello',new='World')
    assert (tmp_path/'web/index.html').read_text()=='<h1>World</h1>'
    assert run(tmp_path,'fs_search',pattern='World',glob='*.html')['matches'][0]['line']==1
    assert read(tmp_path,'web/index.html',4,5)[0]==b'World'
    value=preview(tmp_path,'web/index.html')
    assert value['mime']=='text/html' and base64.b64decode(value['bytes_b64'])==b'<h1>World</h1>'
    run(tmp_path,'fs_write',path='dup.txt',content='xx')
    for old in ('x','missing',''):
        with pytest.raises(ExecutionError,match='edit_match_not_unique'):
            run(tmp_path,'fs_edit',path='dup.txt',old=old,new='z')
    run(tmp_path,'fs_delete',path='dup.txt')
    assert not (tmp_path/'dup.txt').exists()


@pytest.mark.parametrize('path',['/absolute','../escape','a/../escape','C:/bad','a\\b','.env','a/.git/config'])
def test_mutations_cannot_escape_project(tmp_path,path):
    with pytest.raises((ResourceError,ExecutionError)):
        run(tmp_path,'fs_write',path=path,content='bad')


def test_symlink_read_and_mutation_refused(tmp_path):
    outside=tmp_path/'outside'; outside.mkdir()
    project=tmp_path/'project';project.mkdir()
    (outside/'file').write_text('protected')
    try:
        (project/'link').symlink_to(outside,target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable on this test host')
    for op,args in [('fs_write',{'path':'link/file','content':'bad'}),('fs_delete',{'path':'link/file'}),
                    ('fs_edit',{'path':'link/file','old':'protected','new':'bad'})]:
        with pytest.raises((ResourceError,ExecutionError)):
            run(project,op,**args)
    assert (outside/'file').read_text()=='protected'


def test_undo_restores_saved_bytes_and_removes_new_files(tmp_path):
    from core.execution.workspaces import snapshot
    run(tmp_path,'fs_write',path='index.html',content='before')
    (tmp_path/'empty').mkdir()
    frozen=snapshot(tmp_path)
    frozen['blobs']={sha:base64.b64encode(data).decode() for sha,data in frozen['blobs'].items()}
    run(tmp_path,'fs_write',path='index.html',content='after')
    run(tmp_path,'fs_write',path='added/deep.txt',content='new')
    (tmp_path/'empty').rmdir()
    run(tmp_path,'checkpoint_restore',snapshot=frozen)
    assert (tmp_path/'index.html').read_text()=='before'
    assert (tmp_path/'empty').is_dir()
    assert not (tmp_path/'added').exists()


def test_worker_rechecks_preimage_at_dispatch(tmp_path):
    run(tmp_path,'fs_write',path='index.html',content='new owner content')
    with pytest.raises(ExecutionError,match='builder_preimage_changed'):
        run(tmp_path,'fs_write',path='index.html',content='stale proposal',_preimage=None)
    assert (tmp_path/'index.html').read_text()=='new owner content'


def test_compressed_checkpoint_restores_exact_bytes(tmp_path):
    import zlib
    from core.execution.workspaces import snapshot
    original=b'abcd'*200000
    (tmp_path/'data.bin').write_bytes(original)
    frozen=snapshot(tmp_path)
    frozen['blobs']={sha:base64.b64encode(zlib.compress(data)).decode() for sha,data in frozen['blobs'].items()}
    frozen['encoding']='zlib-base64'
    (tmp_path/'data.bin').write_bytes(b'after')
    run(tmp_path,'checkpoint_restore',snapshot=frozen)
    assert (tmp_path/'data.bin').read_bytes()==original


def test_checkpoint_ignores_reserved_test_cache_and_validates_before_delete(tmp_path):
    from core.builder.files import checkpoint_snapshot
    (tmp_path/'.pytest_cache').mkdir()
    (tmp_path/'.pytest_cache/state').write_text('cache')
    (tmp_path/'index.html').write_text('original')
    frozen=checkpoint_snapshot(tmp_path)
    assert [item['path'] for item in frozen['manifest']]==['index.html']
    frozen['blobs']={sha:base64.b64encode(b'corrupted').decode() for sha in frozen['blobs']}
    (tmp_path/'new.txt').write_text('must remain')
    with pytest.raises(ExecutionError,match='checkpoint_digest_conflict'):
        run(tmp_path,'checkpoint_restore',snapshot=frozen)
    assert (tmp_path/'new.txt').read_text()=='must remain'


def test_preview_cap_and_hardlink_refusal(tmp_path):
    (tmp_path/'big.bin').write_bytes(b'x'*(5*1024*1024+1))
    value=preview(tmp_path,'big.bin')
    assert value['truncated'] and value['bytes_b64'] is None
    (tmp_path/'base.txt').write_text('x')
    import os
    os.link(tmp_path/'base.txt',tmp_path/'linked.txt')
    with pytest.raises(ExecutionError,match='shared_file_refused'):
        read(tmp_path,'linked.txt')


def test_project_landlock_root_is_exact_conversation_project(tmp_path):
    project=str(uuid.uuid4());(tmp_path/project).mkdir()
    cfg={'workspaces':{'projects':str(tmp_path)}}
    assert project_root(cfg,{'workspace':'projects','cwd':project})==tmp_path/project
    for workspace,cwd in [('scratch',project),('projects',''),('projects',project+'/sub'),('projects','../'+project)]:
        with pytest.raises(Exception):
            project_root(cfg,{'workspace':workspace,'cwd':cwd})


async def test_worker_dispatch_and_network_options_are_server_selected(monkeypatch,tmp_path):
    import core.worker.client as client
    from core.builder import network_client
    leases=[]
    class Lease:
        def __init__(self,*args):
            self.token='a'*64
            self.url='http://bob:'+self.token+'@127.0.0.1:21004'
            self.events,self.approvals,self.job_id=[],[],args[4]
            self.closed=False
            leases.append(self)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):self.closed=True
    monkeypatch.setattr(network_client,'JobNetwork',Lease)
    monkeypatch.setattr(client,'configuration',lambda:{})
    execute_mock=AsyncMock(return_value={'returncode':0,'stdout':b'ok','stderr':b'',
                        'cleanup':'idle_proved','execution_seconds':.5,'execution_id':'receipt'})
    monkeypatch.setattr(client,'execute_async',execute_mock)
    from core.builder.service import execution
    context=execution.set({'row':{},'approval_id':'job-one'})
    try:
        result=await Builder(None).worker(Authority('lane','owner','owner',{},'portal'),tmp_path,'shell_run',
                                         {'command':'npm test','timeout_s':600})
        assert result['exit_code']==0 and result['stdout']=='ok' and result['network']
        call=execute_mock.call_args
        assert call.args==('builder',) and call.kwargs['timeout']==600
        payload=json.loads(call.kwargs['input'])
        assert payload['network'] is True and payload['proxy_url']==leases[0].url
        assert result['job_id']=='job-one' and leases[0].closed
        execute_mock.return_value['cleanup']='unknown'
        with pytest.raises(ExecutionError,match='builder_cleanup_unproved'):
            await Builder(None).worker(Authority('lane','owner','owner',{},'portal'),tmp_path,'shell_run',{'command':'ls'})
        assert leases[-1].closed
    finally:
        execution.reset(context)


async def test_model_tool_cannot_select_conversation_or_mode():
    from core.tools.builder_tools import BUILDER_TOOLS
    for tool in BUILDER_TOOLS:
        assert not {'conversation_id','mode','owner','yolo_acknowledged'} & tool.args.keys()
        assert 'project_id' not in tool.args or tool.name == 'workspace_mount'
    assert json.loads(await invoke('fs_write',{'path':'x','content':'x'}))['error']['code']=='builder_context_unavailable'


@pytest.mark.parametrize('outcome', ['denied', 'completed', 'unknown', 'expired'])
async def test_builder_cards_records_and_waits_keep_conversation_and_agent(monkeypatch, outcome):
    from core.builder import service, workspace
    conversation = str(uuid.uuid4())
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    pool = SimpleNamespace(execute=AsyncMock())
    builder = Builder(pool)
    mode = dict(conversation_id=conversation, project_id=None, session_id='session',
                session_until=100000, mode='approve')
    approval = dict(id='approval-a', conversation_id=conversation, kind='workspace',
                    summary='workspace_create', detail={'action': 'create', 'name': 'test'},
                    mode='approve', expires_at=10600, session_until=100000,
                    state='pending', execution_state='pending', result=None)
    final = {**approval, 'state': outcome if outcome in {'denied', 'expired'} else 'allowed',
             'execution_state': outcome if outcome in {'completed', 'unknown'} else 'pending',
             'result': {'status': outcome}}
    builder.store.mode = AsyncMock(return_value=mode)
    builder.store.allowances = AsyncMock(return_value=[])
    builder.store.approval = AsyncMock(side_effect=[approval, approval, final, final, final])
    monkeypatch.setattr(builder, 'workspace', lambda *args: None)
    monkeypatch.setattr(workspace, 'proposal', AsyncMock(return_value=(
        {'name': 'test'}, {'action': 'create', 'name': 'test'})))
    monkeypatch.setattr(service, 'time', SimpleNamespace(time=lambda: 10000, monotonic=lambda: 10000))
    monkeypatch.setattr(service.asyncio, 'sleep', AsyncMock())
    events = []
    await builder.tool(auth, conversation, 'workspace_create', {'name': 'test'}, writer=events.append)
    card = json.loads(events[0]['content'].split('\n')[1])
    record = await builder.command(auth, 'builder.approval.get', {'approval_id': 'approval-a'})
    for value in (card, record, *[e for e in events if e['type'] == 'builder_wait']):
        assert value['conversation_id'] == conversation
        assert value['agent'] == 'builder'
    waits = [e['waiting'] for e in events if e['type'] == 'builder_wait']
    assert waits == [True, False]


def test_nonempty_summary_is_factual_and_preserves_apply_card():
    assert summary([]).strip()
    text=summary([('shell_run',json.dumps({'exit_code':1,'truncated':True}))])
    assert 'exit code 1' in text and 'truncated' in text
    assert 'approval expired' in summary([('fs_write','{"error":{"code":"approval_expired"}}')])
    assert 'bob-builder-approval' in summary([('fs_write',json.dumps({'status':'proposed','block':'```bob-builder-approval\n{}\n```'}))])


def test_builder_face_keeps_guide_and_checked_tools():
    import yaml
    from core.faces.registry import Face
    path=Path(__file__).parents[1]/'core/faces/templates/builder.yaml'
    face=Face.model_validate(yaml.safe_load(path.read_text()))
    assert face.tool_loop_max_rounds==100
    assert {'bob.builder'}==set(face.allowed_skills)
    assert 'bob_guide' in face.allowed_tools
    assert {'fs_write','fs_edit','shell_run','checkpoint_restore','web_search','work_prepare','work_start'}<=set(face.allowed_tools)


@pytest.mark.parametrize('name', ['builder', 'fixer'])
def test_preview_and_plain_language_contract(name, monkeypatch):
    import yaml
    import sys
    from core.faces.prompt import interactive_messages
    from core.faces import registry
    from core.tools.builder_tools import preview_file
    data = yaml.safe_load((Path(__file__).parents[1]/f'core/faces/templates/{name}.yaml').read_text())
    prompt = data['system_prompt']
    for phrase in ('static', 'Open on this PC', 'tool', 'cannot read code', 'Details', 'single-purpose'):
        assert phrase in prompt
    if name == 'builder':
        body = ' '.join(next(s['body'] for s in data['skills'] if s['id'] == 'bob.builder').split())
        for phrase in ('bob_guide', 'single-purpose', 'tool result', 'cannot read code'):
            assert phrase in body
        assert 'never execute' in preview_file.description or 'executes JavaScript' in preview_file.description
    monkeypatch.setenv('BOB_FACE_PROMPTS', '1')
    # Card rendering does not need optional MCP transports.
    monkeypatch.setitem(sys.modules, 'core.tools.registry', SimpleNamespace(NATIVE_TOOLS={}, PIPE_OPERATIONS={}))
    monkeypatch.setattr(registry, 'get_default_registry', lambda: SimpleNamespace(
        get_allowed_tools=lambda _: [], get_system_prompt=lambda _: prompt))
    messages = interactive_messages([], name, 'fixture')
    card = json.loads(messages[1]['content'].split('\n', 1)[1])
    assert 'bob_guide' in card['guide']
    guide = Path(__file__).parents[1]/'core/guide/topics'
    workspace = (guide/'workspaces-and-files.md').read_text()
    assert 'does not execute JavaScript' in workspace
    assert 'inline scripts, forms and dialogs' in workspace
    assert 'no-network sandbox' in workspace
    assert 'storage failures' in workspace and 'keep state in memory' in workspace
    assert "Save to Library is the user's click" in workspace



@pytest.mark.parametrize('expiry',[False,True])
async def test_actual_tool_loop_exceeds_25_rounds_and_ends_nonempty(monkeypatch,expiry):
    # Load the actual function AST without importing unrelated optional backend
    # SDKs. Every model invocation is a local fake; no provider calls occur.
    import ast
    import logging
    import sys
    from contextvars import ContextVar
    from time import perf_counter
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool
    import core.faces.registry as registry_module
    import core.faces.prompt as prompt_module
    import core.context as context_module
    source=(Path(__file__).parents[1]/'core/nodes/execute.py').read_text(encoding='utf-8')
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_run_tool_loop')
    helpers=[n for n in ast.parse(source).body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in {'_finish_tool_reply','ToolLoopReply','ToolRoundLimitReply'}]
    @tool
    async def receipt_tool() -> str:
        """Return a local receipt."""
        return '{"error":{"code":"approval_expired"},"end_turn":true}' if expiry else '{"exit_code":0}'
    registry=SimpleNamespace(get_allowed_tools=lambda face:['receipt_tool'],list_skills=lambda face:[],
                             get_face=lambda face:SimpleNamespace(tool_loop_max_rounds=100))
    monkeypatch.setattr(registry_module,'get_default_registry',lambda:registry)
    monkeypatch.setattr(prompt_module,'interactive_messages',lambda messages,*args:messages)
    monkeypatch.setattr(context_module,'enforce_backend_limits',lambda messages,*args:messages)
    monkeypatch.setitem(sys.modules,'core.tools.registry',SimpleNamespace(_skill_face_id=ContextVar('test-face')))
    calls=[]
    class Model:
        def bind_tools(self,tools):return self
        async def ainvoke(self,messages):
            calls.append(1)
            if len(calls)>30:return AIMessage(content=[{'type':'text','text':''}])
            return AIMessage(content='',tool_calls=[{'name':'receipt_tool','args':{},'id':str(len(calls))}])
    async def get_tools(allowed):return [receipt_tool]
    namespace={'json':json,'uuid':uuid,'logger':logging.getLogger(__name__),'perf_counter':perf_counter,
        'ToolMessage':ToolMessage,'config':SimpleNamespace(TOOL_LOOP_MAX_ROUNDS=1),
        'check_tool_access':lambda *args:True,'get_all_tools':get_tools,'_build_tool_model':lambda backend:Model(),
        'require_backend':lambda *args:None,'_dict_messages_to_lc':lambda messages:messages,
        '_emit_tool_activity':lambda *args,**kwargs:None,'_tool_preview':lambda value:value}
    exec(compile(ast.Module(body=[*helpers,node],type_ignores=[]),'<actual Builder tool loop>','exec'),namespace)
    frames=[]
    result=await namespace['_run_tool_loop']([], 'fixture', 'builder', frames.append)
    assert result.strip() and frames[-1]['content']==result
    assert len(calls)==(1 if expiry else 31)
    assert ('approval expired' if expiry else 'exit code 0') in result
