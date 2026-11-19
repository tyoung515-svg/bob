"""Expired proposal history and owner-only renewal through real service logic."""
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.execution.models import ExecutionError
from core.execution.workflows import utc
from core.workflow_surface import WorkflowSurface, capabilities
from test_workflow_authority import setup, proposal_args, call, start


@pytest.fixture(autouse=True)
def mock_redis():
    yield


@pytest.fixture
def surface(setup):
    service, auth, click = setup
    surface = WorkflowSurface(SimpleNamespace(store=service.store), runtime=service.runtime,
        clock=service.clock, test_only_runner_available=True)
    original = service.store.transaction
    class Query:
        async def fetch(self, sql, lane, owner, *args):
            rows = [deepcopy(r['proposal']) for (l,o,k,_),r in service.store.data.items()
                if (l,o,k) == (lane,owner,'proposal')]
            rows = [p for p in rows if p['state'] != 'granted']  # consumed column
            if 'AS expires_at' in sql:
                cutoff, before, before_id = args
                assert "IN ('proposed','expired')" in sql and 'AND NOT consumed' in sql
                rows = [p for p in rows if p['state'] in {'proposed','expired'} and not p.get('run_id')
                    and utc(p['expires_at']) >= cutoff and (before is None
                    or (utc(p['expires_at']),p['id']) < (before,before_id))]
                rows.sort(key=lambda p:(utc(p['expires_at']),p['id']), reverse=True)
                return [{'id':p['id'],'expires_at':utc(p['expires_at'])} for p in rows[:51]]
            cursor, = args
            return [{'id':p['id']} for p in sorted(rows,key=lambda p:p['id']) if p['id'] > cursor][:51]
    @asynccontextmanager
    async def transaction(**kwargs):
        async with original(**kwargs):
            yield Query()
    surface.store.transaction = transaction
    return surface, auth, click


async def propose(s, auth, **kwargs):
    args = proposal_args(); args.update(kwargs)
    return (await call(s,auth,'propose',**args))['proposal']


async def again(s, auth, p):
    return (await call(s,auth,'propose_again',proposal_id=p['id']))['proposal']


def record(s, auth, p):
    return s.store.data[auth.lane,auth.owner,'proposal',p['id']]


async def test_listing_opt_in_window_states_order_and_owner(surface):
    s,a,click = surface; now = s.clock()
    waiting = await propose(s,a)
    recent = await propose(s,a,request_key='recent')
    edge = await propose(s,a,request_key='edge')
    old = await propose(s,a,request_key='old')
    cancelled = await propose(s,a,request_key='cancelled')
    started = await propose(s,a,request_key='started')
    await start(s,click,started)
    for p,delta in [(recent,timedelta(hours=1)),(edge,timedelta(days=7)),
                    (old,timedelta(days=7,microseconds=1)),(cancelled,timedelta(hours=2))]:
        record(s,a,p)['proposal']['expires_at'] = (now-delta).isoformat()
    record(s,a,cancelled)['proposal']['state'] = 'cancelled'
    foreign = replace(a,owner='foreign',actor='foreign')
    await propose(s,foreign,request_key='foreign')
    baseline = await call(s,a,'proposals')
    assert [p['id'] for p in baseline['items']] == [waiting['id']]
    assert await call(s,a,'proposals',include_expired=False) == baseline
    result = await call(s,a,'proposals',include_expired=True)
    assert [p['id'] for p in result['items']] == [waiting['id'],recent['id'],edge['id']]
    assert [p['state'] for p in result['items']] == ['proposed','expired','expired']
    assert all(p['kind'] == 'workflow_proposal' and p['created_at'] and p['proposed_by'] for p in result['items'])


async def test_history_paging_ties_scoped_scan_and_cursor_validation(surface):
    s,a,_ = surface
    agent = replace(a,token_type='agent',actor='outside',scope={'may_touch':['fixture/**']})
    # More than a SQL batch of hidden boundary rows must not end the scan early.
    hidden = await propose(s,agent)
    base = deepcopy(record(s,a,hidden))
    for i in range(60):
        r = deepcopy(base); r['proposal']['id'] = f'z-hidden-{i:03}'
        s.store.data[a.lane,a.owner,'proposal',r['proposal']['id']] = r
    stranger = replace(agent,actor='another-agent')
    visible = [await propose(s,stranger,request_key=f'visible-{i}') for i in range(4)]
    found = []; cursor = ''; cursors = set()
    while True:
        page = await call(s,stranger,'proposals',include_expired=True,limit=2,cursor=cursor)
        found.extend(p['id'] for p in page['items'])
        cursor = page['next_cursor']
        if not cursor: break
        assert cursor not in cursors; cursors.add(cursor)
    assert found == sorted([p['id'] for p in visible],reverse=True)
    for cursor in ('bad!', 'W10=', 'e30='):
        with pytest.raises(ExecutionError,match='invalid_cursor'):
            await call(s,a,'proposals',include_expired=True,cursor=cursor)
    for bad in ('true',1,None):
        with pytest.raises(ExecutionError,match='invalid_args'):
            await call(s,a,'proposals',include_expired=bad)


async def test_again_preserves_request_history_and_separate_start(surface):
    s,a,click = surface; original_time = s.clock()
    p = await propose(s,a,origin_conversation_id='fixture-conversation')
    original = deepcopy(record(s,a,p))
    # Works next day, after the originally requested grant deadline has passed.
    s.clock = lambda: original_time+timedelta(days=2)
    fresh = await again(s,a,p)
    assert fresh['id'] != p['id'] and fresh['state'] == 'proposed'
    assert fresh['previous_proposal_id'] == p['id']
    assert fresh['definition_changed'] is False and fresh['definition_change_notice'] is None
    assert utc(fresh['expires_at']) == s.clock()+timedelta(minutes=15)
    assert utc(fresh['created_at']) == s.clock()
    assert fresh['origin_conversation_id'] == 'fixture-conversation'
    assert fresh['proposed_by'] == {'actor':a.owner,'token_type':a.token_type,'via':'owner'}
    renewed = record(s,a,fresh)
    for key in ('inputs','workspace','project_id','origin_conversation_id'):
        assert renewed['args'][key] == original['args'][key]
    assert fresh['coverage']['bindings'] == p['coverage']['bindings']
    assert fresh['coverage']['review'] == p['coverage']['review']
    assert utc(renewed['args']['expires_at'])-s.clock() == utc(original['args']['expires_at'])-original_time
    assert record(s,a,p)['proposal']['state'] == 'expired'
    assert record(s,a,p)['proposal']['proposed_again_ids'] == [fresh['id']]
    assert not s.store.starts and not s.store.events and not s.runtime.created
    with pytest.raises(ExecutionError,match='proposal_expired'):
        await start(s,click,p,key='old-start')
    assert (await start(s,click,fresh))['run']['status'] == 'accepted'
    assert s.store.starts == 1
    # Repeated explicit asks preserve all links and the old frozen evidence.
    second = await again(s,a,p)
    assert record(s,a,p)['proposal']['proposed_again_ids'] == [fresh['id'],second['id']]
    assert record(s,a,p)['frozen'] == original['frozen']


@pytest.mark.parametrize('state',['proposed','granted','cancelled'])
async def test_again_refuses_nonexpired(surface,state):
    s,a,click = surface; p = await propose(s,a)
    if state == 'granted': await start(s,click,p)
    elif state == 'cancelled': record(s,a,p)['proposal']['state'] = state
    code = 'run_still_active' if state == 'granted' else 'proposal_not_expired'
    with pytest.raises(ExecutionError,match=code) as exc:
        await again(s,a,p)
    assert ('still going' if state == 'granted' else 'expired proposal') in exc.value.detail


async def test_again_foreign_missing_agent_model_and_owner_session_refusals(surface):
    s,a,click = surface; p = await propose(s,a); now = s.clock()
    s.clock = lambda: now+timedelta(minutes=16)
    for caller in (replace(a,owner='other',actor='other'),a):
        with pytest.raises(ExecutionError,match='proposal_not_found'):
            await again(s,caller,{'id':p['id'] if caller.owner != a.owner else 'missing'})
    for caller in (replace(a,token_type='agent'),replace(a,actor='someone-else')):
        with pytest.raises(ExecutionError,match='proposal_again_not_permitted'):
            await again(s,caller,p)
    with pytest.raises(ExecutionError,match='owner_audience_denied'):
        await again(s,click,p)
    s.assistant_origin = 'fixture-conversation'
    with pytest.raises(ExecutionError,match='proposal_again_not_permitted'):
        await again(s,a,p)
    assert len([k for k in s.store.data if k[2]=='proposal']) == 1


async def test_current_definition_change_deleted_and_missing(surface):
    s,a,_ = surface
    saved = (await call(s,a,'copy',id='premade-plan-build-review-fix',revision=1,
        name='Editable',request_key='copy'))['definition']
    p = await propose(s,a,definition_ref={'id':saved['id'],'revision':1})
    changed = {**saved['definition'],'summary':'The current definition.'}
    await call(s,a,'save',id=saved['id'],definition=changed,expected_revision=1,request_key='edit')
    now = s.clock(); s.clock = lambda: now+timedelta(minutes=16)
    fresh = await again(s,a,p)
    assert fresh['definition_ref'] == {'id':saved['id'],'revision':2}
    assert fresh['coverage']['definition'] == changed
    assert fresh['definition_changed'] is True
    assert 'uses the current definition' in fresh['definition_change_notice']
    await call(s,a,'delete',id=saved['id'],expected_revision=2,request_key='delete')
    with pytest.raises(ExecutionError,match='workflow_deleted') as exc:
        await again(s,a,p)
    assert 'deleted' in exc.value.detail
    del s.store.data[a.lane,a.owner,'definition',saved['id']]
    with pytest.raises(ExecutionError,match='workflow_deleted'):
        await again(s,a,p)


@pytest.mark.parametrize('failure',['model','budget','review','runner','inputs'])
async def test_failed_fresh_checks_leave_no_new_proposal_or_links(surface,failure):
    s,a,_ = surface; p = await propose(s,a); now = s.clock()
    # Materialize expiry first so rollback is compared against durable history.
    s.clock = lambda: now+timedelta(minutes=16)
    await call(s,a,'proposal',proposal_id=p['id'])
    if failure == 'model':
        s.runtime.rows[0]['runnable'] = False
        reason = 'selected model is unavailable'
    elif failure == 'review':
        for row in s.runtime.rows: row['model_identity'] = 'same'
        reason = 'review'
    elif failure == 'runner':
        s.test_only_runner_available = False
        reason = 'runner not installed'
    else:
        r = record(s,a,p)
        if failure == 'budget':
            async def limits(auth):
                from core.execution.workflows import LIMITS
                return {**LIMITS,'max_calls':1}
            s.runtime.limits = limits
            reason = 'budget limit'
        else:
            r['args']['inputs'] = {}
            reason = 'input'
    before = deepcopy(s.store.data)
    if failure in ('review','runner'):
        # A caveat is not a refusal: the fresh proposal comes back the way a
        # first-time one would, carrying its reasons, and nothing is started.
        fresh = await again(s,a,p)
        assert fresh['state'] == 'proposed' and fresh['reasons'] and not fresh.get('run_id')
        assert fresh['previous_proposal_id'] == p['id'] and not s.store.starts
        return
    with pytest.raises(ExecutionError,match='proposal_recheck_failed') as exc:
        await again(s,a,p)
    assert reason in exc.value.detail
    assert s.store.data == before and not s.store.starts


async def test_inline_and_legacy_proposals_and_default_model_choices(surface):
    s,a,_ = surface
    args = proposal_args(); ref = args.pop('definition_ref')
    from core.execution.workflows import PREMADES
    args['definition'] = deepcopy(PREMADES[ref['id']])
    for alias,slot in args['definition']['agents'].items():
        slot['binding_id'] = args['bindings'][alias]
    args['bindings'] = {}
    p = (await call(s,a,'propose',**args))['proposal']
    r = record(s,a,p); r['proposal'].pop('created_at')
    r['proposal'].pop('origin_conversation_id')
    now = s.clock(); s.clock = lambda: now+timedelta(days=1)
    fresh = await again(s,a,p)
    assert fresh['definition_ref'] is None
    assert fresh['coverage']['bindings'] == p['coverage']['bindings']
    assert fresh['definition_changed'] is False


async def test_portal_renews_agent_proposal_and_legacy_origin_is_forwarded(surface):
    s,a,_ = surface
    agent = replace(a,token_type='agent',actor='outside-token',scope={'may_touch':['fixture/**']})
    p = await propose(s,agent,origin_conversation_id='fixture-conversation')
    record(s,a,p)['proposal'].pop('origin_conversation_id')
    assert (await call(s,a,'proposal',proposal_id=p['id']))['proposal']['origin_conversation_id']=='fixture-conversation'
    now = s.clock(); s.clock = lambda:now+timedelta(minutes=16)
    portal = replace(a,token_type='portal')
    fresh = await again(s,portal,p)
    assert fresh['proposed_by']=={'actor':a.owner,'token_type':'portal','via':'owner'}
    history = await call(s,portal,'proposals',include_expired=True)
    old = next(item for item in history['items'] if item['id']==p['id'])
    assert old['proposed_by']['actor']=='outside-token'
    assert old['origin_conversation_id']=='fixture-conversation'


def test_capability_wire_and_no_model_or_mcp_exports():
    operations = capabilities(True,True)
    assert operations['workflows.proposals']['include_expired'] is True
    assert operations['workflows.propose_again']['available'] is True
    assert operations['workflows.propose_again']['implemented'] is True
    assert operations['workflows.propose_again']['request_key_required'] is False
    assert not capabilities(False)['workflows.propose_again']['available']
    from core.tools.workflows import WORKFLOW_TOOLS
    from core.tools.workflow_context import ASSISTANT_OPERATIONS, READ_OPERATIONS
    from core.mcp.workflows import register
    from mcp.server.fastmcp import FastMCP
    assert 'propose_again' not in ASSISTANT_OPERATIONS | READ_OPERATIONS
    assert all('again' not in tool.name for tool in WORKFLOW_TOOLS)
    mcp = FastMCP('bu3')
    register(mcp,None)
    assert all('again' not in t.name for t in mcp._tool_manager.list_tools())


async def test_signed_model_dispatch_cannot_reach_again(surface):
    from core.tools.workflow_context import dispatch
    s,a,_ = surface
    for face in ('assistant','orchestrator','reviewer'):
        with pytest.raises(ExecutionError,match='not_found'):
            await dispatch(SimpleNamespace(store=s.store),{'face_id':face,
                'conversation_id':'fixture','authority':a.as_dict()},'propose_again',{'proposal_id':'fixture'})
