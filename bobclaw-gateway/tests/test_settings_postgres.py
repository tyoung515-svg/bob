"""Real temporary PostgreSQL policy versions, conflicts and dispatch races.

No DB substitute; requires the existing local-only S4B_TEST_POSTGRES_DSN fixture.
"""
import asyncio
from dataclasses import replace

import pytest

from core.execution.models import ExecutionError
from tests.test_agent_loop_postgres import pg, loop_h, MODEL, ready_steps, invariants


def change(key='policy', revision=0, actions=None):
    return {'request_key': key, 'expected_revision': revision,
            'policy': {'posture': 'scoped', 'scope': {'auto_actions': actions or []}}}


@pytest.mark.asyncio
async def test_policy_revisions_audit_conflict_and_foreign_owner(loop_h, monkeypatch):
    h = loop_h
    assert (await h.store.get_policy(h.owner, {}))['revision'] == 0
    first = await h.store.set_policy(h.owner, change(actions=['model_call']))
    assert first['revision'] == 1
    assert await h.store.set_policy(h.owner, change(actions=['model_call'])) == first
    with pytest.raises(ExecutionError, match='idempotency_conflict'):
        await h.store.set_policy(h.owner, change(actions=['read']))
    with pytest.raises(ExecutionError, match='revision_conflict'):
        await h.store.set_policy(h.owner, change('stale'))
    assert (await h.store.get_policy(replace(h.owner, owner='foreign'), {}))['revision'] == 0
    assert await h.pg.fetchval('SELECT count(*) FROM owner_policy_versions') == 1
    assert await h.pg.fetchval("SELECT count(*) FROM agent_mutations WHERE operation='policy.set'") == 1


@pytest.mark.asyncio
async def test_narrowing_races_charge_and_widening_never_resumes(loop_h, monkeypatch):
    h = loop_h
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a'*12)
    await h.store.set_policy(h.owner, change(actions=['model_call']))
    rid = await h.start()
    row = await h.store.claim('core')
    await ready_steps(h, row)
    attempt = await h.store.reserve_attempt(row, 0, MODEL['one'], 'S1')
    results = await asyncio.gather(h.store.charge_attempt(row, attempt),
                                   h.store.set_policy(h.owner, change('narrow', 1)), return_exceptions=True)
    assert not isinstance(results[1], Exception)
    run = (await h.store.get(h.owner, rid))['run']
    assert run['status'] == 'paused' and run['reason'] == 'policy_narrowed'
    assert run['calls_used'] in {0, 1}
    with pytest.raises(ExecutionError):
        await h.store.reserve_attempt(row, 0, MODEL['one'], 'S1')
    await h.store.set_policy(h.owner, change('widen', 2, ['model_call']))
    assert (await h.store.get(h.owner, rid))['run']['status'] == 'paused'
    await invariants(h)


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', [{'auto_actions': ['token.mint']}, {'may_touch': ['/tmp/**']},
                                 {'may_touch': [{'attachment_id': 'unknown', 'path': '../escape'}]}])
async def test_policy_rejects_authority_or_path_invention(loop_h, scope):
    with pytest.raises((ExecutionError, ValueError)):
        await loop_h.store.set_policy(loop_h.owner, {'request_key': 'invalid', 'expected_revision': 0,
                                                   'policy': {'posture': 'scoped', 'scope': scope}})
    assert (await loop_h.store.get_policy(loop_h.owner, {}))['revision'] == 0
