"""Real temporary Postgres: recovery fences every originating authority class."""
from dataclasses import replace
import pytest

from core import credential_state
from core.execution.models import ExecutionError
from tests.test_agent_loop_postgres import pg,loop_h,MODEL,CONTRACT,TEAM,ready_steps


@pytest.mark.parametrize('token_type',['human','portal','agent'])
async def test_recovery_fences_durable_reservation_and_dispatch(loop_h,monkeypatch,token_type):
    h=loop_h
    active={'epoch':1,'revocation_epoch':0}
    monkeypatch.setenv('BOB_DEFAULT_OWNER','owner')
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY','worker')
    monkeypatch.setenv('BOB_SETTINGS_ID','a'*12)
    monkeypatch.setattr(credential_state,'active',lambda:dict(active))
    authority=replace(h.owner,credential_epoch=1,token_type=token_type)
    team_id='loop-team'
    if token_type=='agent':
        # Agent authorities only see teams saved under their own boundary; heads are per owner.
        team_id='agent-loop-team'
        await h.service.save(authority,{'definition':{**TEAM,'id':team_id},'expected_revision':None,'request_key':'agent-save'})
    team=await h.store.team(authority,team_id)
    result=await h.store.start(authority,dict(team=team_id,team_revision=1,task='S1',evidence_ids=['S1'],
        acceptance=CONTRACT,request_key='recovery-run',budget=dict(start_calls=10,wall_mode='none',wall_seconds=None)),
        dict(team=team,models=MODEL,review_independence='same_model'))
    row=await h.store.claim('core')
    assert row['run_authority']['credential_epoch']==1
    await ready_steps(h,row)
    attempt=await h.store.reserve_attempt(row,0,MODEL['one'],'S1')
    active.update(epoch=2,revocation_epoch=2)
    for operation in (h.store.charge_attempt(row,attempt),h.store.reserve_attempt(row,1,MODEL['one'],'S1')):
        with pytest.raises(ExecutionError,match='credential_epoch_changed'):
            await operation
    assert await h.pg.fetchval('SELECT consumed FROM agent_budget_accounts WHERE run_id=$1',row['id'])==0
