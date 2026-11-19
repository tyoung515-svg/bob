"""BU13: real chat-workflow HTTP refusals name the field and expected shape."""
import json

import pytest

from core.config import config
from core.tools import workflows as tools, workflow_context as context
from test_workflow_tools import native, proposing_face, setup, model_args


@pytest.fixture(autouse=True)
def mock_redis():
    yield None


@pytest.mark.parametrize('change,field',[
    (lambda a:a.pop('inputs'),'inputs'),
    (lambda a:a.update(inputs={}), 'inputs.task'),
    (lambda a:a.update(inputs={'task':True}), 'inputs.task'),
    (lambda a:a.update(inputs={'task':'hello','unknown':1}), 'inputs.unknown'),
    (lambda a:a.update(bindings=[]), 'bindings'),
    (lambda a:a['bindings'].update(unknown='one'), 'bindings.unknown'),
    (lambda a:a['bindings'].update(builder=True), 'bindings.builder'),
    (lambda a:a.update(workspace='new'), 'workspace'),
    (lambda a:a.update(workspace={'name':'Hello'}), 'workspace.create'),
    (lambda a:a.update(workspace={'create':'Hello','path':'/tmp'}), 'workspace.path'),
    (lambda a:a.update(workspace={'create':' '}), 'workspace.create'),
    (lambda a:a.update(definition_ref='premade-plan-build-review-fix'), 'definition_ref'),
    (lambda a:a['definition_ref'].pop('revision'), 'definition_ref.revision'),
    (lambda a:a['definition_ref'].update(revision=True), 'definition_ref.revision'),
    (lambda a:a['definition_ref'].update(revision=2**64), 'definition_ref.revision'),
    (lambda a:a['definition_ref'].update(id='bad id'), 'definition_ref.id'),
    (lambda a:a.update(request_key=' '), 'request_key'),
    (lambda a:a.update(definition={}), 'definition_ref / definition'),
    (lambda a:a.update(expires_at='2026-11-17T19:00:00+07:00'), 'expires_at'),
    (lambda a:a.update(expires_at='2000-01-01T00:00:00Z'), 'expires_at'),
    (lambda a:a.update(run_mode='unsafe'), 'run_mode'),
    (lambda a:a.update(project_id='not-a-uuid',workspace=None), 'project_id'),
    (lambda a:a.update(unknown='x'), 'unknown'),
])
async def test_http_propose_refusal_names_field_and_expectation(native,change,field):
    s,auth,click,value,client=native
    args=model_args('bu13-refused')
    change(args)
    proof=context.signature(value,config.BOBCLAW_SECRET)
    response=await client.post('/api/chat-workflows/propose',
        json={'context':value,'vouch':proof,'args':args})
    assert response.status==422
    error=(await response.json())['error']
    assert field in error['detail'] and 'expected' in error['detail']
    assert s.store.starts==0


async def test_native_description_example_proposes_with_catalog_substitutions(native):
    s,*_=native
    description=tools.workflows_propose.description
    start=description.index('{"definition_ref"')
    args,_=json.JSONDecoder().raw_decode(description[start:])
    good=model_args()
    args.update(bindings=good['bindings'],expires_at=good['expires_at'],request_key='bu13-example')
    tools.ProposeArgs.model_validate(args)
    result=await tools.workflows_propose.ainvoke(args)
    assert 'proposal' in result,result
    assert result['proposal']['coverage']['definition']['name']=='Plan → Build → Review → Fix'
    assert s.store.starts==0


def test_mcp_description_has_the_same_minimal_new_workspace_shape():
    from mcp.server.fastmcp import FastMCP
    from core.mcp.workflows import register
    mcp=FastMCP('bu13-example')
    register(mcp,lambda *a,**kw:None)
    description=next(t.description for t in mcp._tool_manager.list_tools() if t.name=='workflows_propose')
    args,_=json.JSONDecoder().raw_decode(description[description.index('{"definition_ref"'):])
    assert args['workspace']=={'create':'Hello workflow'}
    assert args['definition_ref']=={'id':'premade-plan-build-review-fix','revision':1}
    assert set(args['bindings'])=={'planner','builder','reviewer'}
    assert args['inputs']['task']=='Create hello.txt containing hello'
    assert 'server UTC time plus one hour' in description
