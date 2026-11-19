"""Real disposable PostgreSQL authority persistence and pre-dispatch fencing."""
from dataclasses import replace

import pytest

from core import credential_state
from core.execution.models import ExecutionError
from tests.test_agent_loop_postgres import pg, loop_h, MODEL, ready_steps


async def test_password_epoch_stored_in_authority_and_fences_reserved_dispatch(loop_h, monkeypatch):
    h = loop_h
    epoch = [1]
    monkeypatch.setenv('BOB_DEFAULT_OWNER', 'owner')
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'worker')
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a'*12)
    monkeypatch.setattr(credential_state, 'active', lambda: {'epoch': epoch[0]})
    # Exercise storage directly with the originating authority, then the actual
    # reserve/charge path after a credential commit while core stays running.
    owner = replace(h.owner, credential_epoch=1)
    team = await h.store.team(owner, 'loop-team')
    from tests.test_agent_loop_postgres import CONTRACT
    result = await h.store.start(owner, dict(team='loop-team', team_revision=1, task='S1', evidence_ids=['S1'],
        acceptance=CONTRACT, request_key='epoch-run', budget=dict(start_calls=10, wall_mode='none', wall_seconds=None)),
        {'team': team, 'models': MODEL, 'review_independence': 'same_model'})
    row = await h.store.claim('core')
    assert row['run_authority']['credential_epoch'] == 1
    await ready_steps(h, row)
    attempt = await h.store.reserve_attempt(row, 0, MODEL['one'], 'S1')
    epoch[0] = 2
    with pytest.raises(ExecutionError, match='credential_epoch_changed'):
        await h.store.charge_attempt(row, attempt)
    with pytest.raises(ExecutionError, match='credential_epoch_changed'):
        await h.store.reserve_attempt(row, 1, MODEL['one'], 'S1')
    assert await h.pg.fetchval('SELECT consumed FROM agent_budget_accounts WHERE run_id=$1', row['id']) == 0
