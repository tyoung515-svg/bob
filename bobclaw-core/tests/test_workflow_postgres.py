"""Opt-in real DDL/Store gate, isolated in a rolled-back PostgreSQL schema.

WORKFLOW_TEST_POSTGRES_URL must identify an explicitly disposable test database.
No default, production URL or secret file is consulted.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import os
from types import SimpleNamespace
import uuid

import pytest

from core.execution.store import Store
from core.execution.models import ExecutionError
from core.execution.workflows import WorkflowService
from test_workflow_authority import Runtime, call, proposed, start, setup


@pytest.fixture(autouse=True)
def mock_redis():
    yield


@pytest.mark.asyncio
async def test_fresh_upgrade_repeat_and_atomic_start(setup):
    url=os.environ.get('WORKFLOW_TEST_POSTGRES_URL')
    if not url: pytest.skip('WORKFLOW_TEST_POSTGRES_URL not configured; real PostgreSQL gate not run')
    import asyncpg
    conn=await asyncpg.connect(url)
    outer=conn.transaction(); await outer.start()
    try:
        name='wb1_'+uuid.uuid4().hex
        await conn.execute('CREATE SCHEMA '+name)
        await conn.execute('SET LOCAL search_path TO '+name)
        root=Path(__file__).resolve().parents[2]
        initial=(root/'init.sql').read_text(encoding='utf-8')
        initial=initial.replace('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";','')
        await conn.execute(initial)
        class Pool:
            @asynccontextmanager
            async def acquire(self): yield conn
        store=Store(Pool(),'lane')
        await store.initialize()
        await store.initialize()  # Upgrade/reapply leaves old rows/contracts intact.
        service,auth,click=setup
        # The real Store checks grant expiry against clock_timestamp(), unlike
        # MemoryStore. Anchor this gate's service clock AND proposal deadlines to
        # that database, leaving the golden fixture's fixed clock untouched.
        database_now = await store.wf_clock(conn)
        service=WorkflowService(SimpleNamespace(store=store),runtime=Runtime(),
                                test_only_runner_available=True,clock=lambda:database_now)
        grant_end = (database_now + timedelta(days=1)).isoformat()
        p=await proposed(service,auth,expires_at=grant_end)
        first=await start(service,click,p)
        await store.initialize()  # Restart with an already persisted matcher4 grant.
        assert await start(service,click,p)==first
        assert await start(service,click,p,key='second-click')==first
        assert await conn.fetchval("SELECT count(*) FROM agent_runs WHERE topology='workflow/v1'")==1
        assert await conn.fetchval('SELECT count(*) FROM agent_execution_grants WHERE matcher_version=4')==1
        assert await conn.fetchval("SELECT count(*) FROM agent_budget_allocations WHERE id='root'")==1
        assert await store.claim('legacy-pump') is None
        # New controller migration is repeatable and supports multiple model
        # requests on one expanded leaf without the legacy four-step limit.
        assert await conn.fetchval("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='builder_modes' AND column_name IN ('workflow_run_id','workflow_generation')")==2
        row=await store.claim('workflow-pump',workflows=True)
        assert row['topology']=='workflow/v1'
        stage={'key':'work/build','ordinal':40,'kind':'agent','state':'running','agent_alias':'builder',
               'conversation_id':None,'request_id':None,'actual_models':[]}
        async with store.transaction() as c:
            await store.wf_stage(c,auth,row['id'],stage)
            binding=p['coverage']['bindings']['builder']
            one=await store.wf_charge(c,row,stage,binding,'first model round',{})
            stage['calls']=1
            two=await store.wf_charge(c,row,stage,binding,'second model round',{})
            assert one!=two
            await store.wf_model_result(c,row,one)
            await store.wf_model_result(c,row,two)
            wait={'wait_id':'controller-recovery','record_id':'work/build','kind':'recovery','state':'open',
                  'stage_key':'work/build','expires_at':first['run']['grant']['expires_at']}
            await store.wf_open_wait(c,auth,row['id'],stage,wait)
            wait['state']='answered'
            await store.wf_wait(c,auth,row['id'],wait,{'decision':'Resume this stage'})
        assert await conn.fetchval('SELECT consumed FROM agent_budget_accounts')==2
        assert await conn.fetchval('SELECT count(*) FROM agent_attempts WHERE ordinal=40')==2
        assert await conn.fetchval("SELECT state FROM workflow_waits WHERE id='controller-recovery'")=='answered'
        assert not (await store.runs(auth,None,20))['items']
        w=(await call(service,auth,'stop',run_id=first['run']['id'],request_key='stop'))['run']
        assert w['status']=='stopped'
        assert await conn.fetchval('SELECT reserved FROM agent_call_pools')==0
        assert await conn.fetchval('SELECT revoked FROM agent_execution_grants')
        assert len((await call(service,auth,'events',run_id=w['id'],after_seq=0))['events'])==3
        # BU13: the internal pre-summary artifact is not posted. Regenerating a
        # published report updates one stable origin message and its outbox row.
        origin=uuid.uuid4()
        await conn.execute("INSERT INTO conversations(id,user_id,title) VALUES($1,$2,$3)",origin,auth.owner,'BU13 test report')
        record=await store.wf_get(conn,auth,'run',w['id'])
        record['run']['origin_conversation_id']=str(origin)
        await service.make_report(conn,auth,record,publish=False)
        assert await conn.fetchval("SELECT count(*) FROM messages WHERE conversation_id=$1",origin)==0
        record['run']['budget']['consumed_calls']=11
        await service.make_report(conn,auth,record)
        first_digest=record['run']['report']['digest']
        record['run']['budget']['consumed_calls']=12
        await service.make_report(conn,auth,record)
        await service.make_report(conn,auth,record)
        await store.wf_run(conn,auth,record)
        assert record['run']['report']['digest']!=first_digest
        posted=await conn.fetch("SELECT id,content,metadata FROM messages WHERE conversation_id=$1",origin)
        assert len(posted)==1 and 'Calls used: 12.' in posted[0]['content']
        outbox=await conn.fetchrow("SELECT message_id,report_digest FROM workflow_report_outbox WHERE lane=$1 AND owner=$2 AND run_id=$3",
                                  auth.lane,auth.owner,w['id'])
        assert outbox['message_id']==posted[0]['id']
        assert outbox['report_digest']==record['run']['report']['digest']
        # BU3 exercises the real JSONB history query, timestamp cursor, durable
        # links and normal Start with actual transaction rollback on refusal.
        from core.workflow_surface import WorkflowSurface
        old = await proposed(service,auth,request_key='bu3-old',expires_at=grant_end)
        another = await proposed(service,auth,request_key='bu3-another',expires_at=grant_end)
        clock = service.clock
        surface = WorkflowSurface(SimpleNamespace(store=store),runtime=service.runtime,
            test_only_runner_available=True,clock=lambda:clock()+timedelta(days=2))
        page = await call(surface,auth,'proposals',include_expired=True,limit=1)
        assert len(page['items'])==1 and page['items'][0]['state']=='expired'
        assert page['next_cursor']
        next_page = await call(surface,auth,'proposals',include_expired=True,limit=1,cursor=page['next_cursor'])
        assert {page['items'][0]['id'],next_page['items'][0]['id']}=={old['id'],another['id']}
        fresh = (await call(surface,auth,'propose_again',proposal_id=old['id']))['proposal']
        await store.initialize()
        assert fresh['previous_proposal_id']==old['id']
        old_record = (await call(surface,auth,'proposal',proposal_id=old['id']))['proposal']
        assert old_record['state']=='expired' and old_record['proposed_again_ids']==[fresh['id']]
        assert [item['id'] for item in (await call(surface,auth,'proposals'))['items']]==[fresh['id']]
        await start(surface,click,fresh,key='bu3-start')
        with pytest.raises(ExecutionError,match='proposal_expired'):
            await start(surface,click,old,key='bu3-old-start')
        count = await conn.fetchval('SELECT count(*) FROM agent_admission_intents')
        surface.test_only_runner_available=False
        # A renewed proposal that carries a caveat (here: no runner) is returned
        # like a first-time one. It starts nothing; Start stays the owner's click.
        caveated = (await call(surface,auth,'propose_again',proposal_id=another['id']))['proposal']
        assert caveated['state']=='proposed' and caveated['reasons']
        assert caveated['previous_proposal_id']==another['id']
        # One new proposal record, and no run.
        assert await conn.fetchval('SELECT count(*) FROM agent_admission_intents')==count+1
        assert not caveated.get('run_id')
        # BU14: a restart can discover the original consumed proposal even for
        # old JSON snapshots, using the original selected grant's stored link.
        await conn.execute("UPDATE agent_runs SET workflow_state=workflow_state #- '{run,proposal_id}' WHERE lane=$1 AND owner=$2 AND id=$3",
                           auth.lane,auth.owner,w['id'])
        await store.initialize()
        renewed_service = WorkflowService(SimpleNamespace(store=store),runtime=Runtime(),
            test_only_runner_available=True,clock=lambda:database_now+timedelta(minutes=1))
        detail = (await call(renewed_service,auth,'run',run_id=w['id']))['run']
        summary = next(r for r in (await call(renewed_service,auth,'runs'))['items'] if r['id']==w['id'])
        assert detail['proposal_id']==summary['proposal_id']==p['id']
        foreign = replace(auth,owner='other',actor='other')
        with pytest.raises(ExecutionError,match='not_found'):
            await call(renewed_service,foreign,'run',run_id=w['id'])
        assert (await call(renewed_service,foreign,'runs'))['items']==[]
        with pytest.raises(ExecutionError,match='proposal_not_found'):
            await call(renewed_service,foreign,'propose_again',proposal_id=p['id'])
        run_count = await conn.fetchval('SELECT count(*) FROM agent_runs')
        grant_count = await conn.fetchval('SELECT count(*) FROM agent_execution_grants')
        fresh_run = (await call(renewed_service,auth,'propose_again',proposal_id=p['id']))['proposal']
        assert fresh_run['previous_proposal_id']==p['id']
        assert fresh_run['coverage']['project_id']==w['project_id']
        assert not fresh_run['coverage']['workspace'].get('create')
        assert fresh_run['run_id'] is None
        await store.initialize()
        assert (await call(renewed_service,auth,'propose_again',proposal_id=p['id']))['proposal']==fresh_run
        consumed = await conn.fetchrow('SELECT consumed,grant_id,workflow_coverage FROM agent_admission_intents WHERE lane=$1 AND owner=$2 AND id=$3',
                                       auth.lane,auth.owner,p['id'])
        from core.execution.workflow_store import decoded
        original = decoded(consumed['workflow_coverage'])
        assert consumed['consumed'] and consumed['grant_id']==first['grant_id']
        assert original['proposal']['state']=='granted'
        assert original['proposal']['proposed_again_ids']==[fresh_run['id']]
        assert await conn.fetchval('SELECT count(*) FROM agent_runs')==run_count
        assert await conn.fetchval('SELECT count(*) FROM agent_execution_grants')==grant_count
        started_again = (await start(renewed_service,click,fresh_run,key='bu14-postgres-start'))['run']
        assert started_again['proposal_id']==fresh_run['id']
        assert started_again['grant']['id']!=first['grant_id']
        # Explicit cancelled records use the same terminal definition in JSON
        # persistence and propose_again; normal owner Stop remains stopped.
        async with store.transaction() as c:
            cancelled = await store.wf_get(c,auth,'run',started_again['id'])
            cancelled['run']['status']='cancelled'
            cancelled['run']['grant']['active']=False
            await store.wf_stop(c,auth,cancelled)
            await store.wf_run(c,auth,cancelled)
        assert await conn.fetchval('SELECT terminal_outcome FROM agent_runs WHERE id=$1',started_again['id'])=='cancelled'
        assert (await call(renewed_service,auth,'propose_again',proposal_id=fresh_run['id']))['proposal']['previous_proposal_id']==fresh_run['id']
    finally:
        await outer.rollback()
        await conn.close()
