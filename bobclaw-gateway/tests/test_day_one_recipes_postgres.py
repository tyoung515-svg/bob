"""Real disposable Postgres gates; deterministic models/transport, actual checks."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_durable_runs_v2 import pg
from tests.test_tool_jobs_postgres import jobs_api
from tests.test_checked_edits_postgres import checked_api
from tests.test_day_one_postgres import work, Transport
from core.execution.executor import Executor
from core.execution.job_store import JobStore
from core.execution.models import ExecutionError, canonical
from core.execution.store import decoded
from core.execution.worker_queue import WorkerQueue
from core.execution.workspaces import snapshot


async def prepare(h, mode='builder', recipe='python-basic', **extra):
    return await h.work.prepare(h.auth, {'project_id': h.pid, 'task': 'Focused addition change',
        'mode': mode, 'recipe': recipe, 'request_key': 'prepare', **extra})


async def grant_start(h, prepared):
    proposal = prepared['proposal']
    await h.store.issue_grant(h.owner, {'proposal_id': proposal['proposal_id'],
        'proposal_revision': proposal['revision'], 'expected_revision': None, 'request_key': 'grant',
        'coverage_kind': 'task', 'allow_long_runs': True, 'auto_escalate': False, 'call_ceiling': 10,
        'expires_at': None, 'allow_extended_jobs': False, 'allow_custom_job_limit': False,
        'job_seconds_ceiling': 900, 'job_auto_escalate': False})
    return await h.work.start(h.auth, {'preparation_id': prepared['preparation_id'], 'request_key': 'start'})


async def drive(h, edits):
    async def model(binding, prompt):
        return {'text': canonical({'edits': edits}), 'usage': {}}
    worker = JobStore(h.pg, 'lane'); await worker.initialize()
    transport = Transport(h)
    queue = WorkerQueue(worker, transport, poll=.01); queue.start()
    executor = Executor(h.store, h.admission, model, poll=.01)
    try:
        row = await h.store.claim(executor.controller); assert row
        await asyncio.wait_for(executor.execute(row), 20)
    finally:
        await queue.close()
    return transport


@pytest.mark.asyncio
async def test_compile_only_recipe_is_pinned_and_runs_without_tests(work):
    h = work
    save = {'name': 'compile-only', 'checks': ['python.compile'], 'expected_revision': None, 'request_key': 'save'}
    await h.work.save_recipe(h.auth, save)
    prepared = await prepare(h, recipe='compile-only')
    # A new recipe revision must have no effect on an existing preparation.
    await h.work.save_recipe(h.auth, {**save, 'checks': ['python.compile', 'python.unittest'],
                                    'expected_revision': 1, 'request_key': 'revise'})
    started = await grant_start(h, prepared)
    transport = await drive(h, [{'operation': 'create', 'path': 'app/calc.py',
                                 'pre_sha256': None, 'content': 'def add(a,b): return a+b\n'}])
    view = await h.store.get(h.auth, started['run_id'])
    assert view['run']['status'] == 'completed', view['run']
    prop = await h.jobs.command(h.auth, 'jobs.proposal', {'job_id': started['id']})
    assert prop['required_checks'] == ['python.compile']
    assert [r['check_id'] for r in prop['check_runs']] == ['python.compile']
    assert prop['recipe']['revision'] == 1
    assert prop['recipe']['definition']['checks'] == ['python.compile']
    assert len(transport.launches) == 2  # edit plus compile; no unittest
    assert not any('test_' in f['path'] for f in prop['files'])


@pytest.mark.asyncio
async def test_recipe_check_outside_workspace_registration_is_refused(work):
    h = work
    path = h.project.parents[1] / 'project-folders.json'
    folders = json.loads(path.read_text()); folders[0]['checked_edits']['checks'] = ['python.compile']
    path.write_text(json.dumps(folders))
    prepared = await prepare(h)
    with pytest.raises(ExecutionError, match='recipe_checks_unregistered'):
        await grant_start(h, prepared)
    assert await h.pg.fetchval('SELECT count(*) FROM tool_jobs') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason', [('repaired', None), ('already-passing', None),
    ('fewer', 'baseline_tests_missing'), ('replaced', 'baseline_regression_not_repaired'),
    ('still-failing', 'baseline_regression_not_repaired')])
async def test_fixer_core_compares_baseline_and_marks_proposals(work, case, reason):
    h = work; app = h.project / 'app'; app.mkdir()
    initial = 'def add(a,b): return a+b\n' if case == 'already-passing' else 'def add(a,b): return a-b\n'
    (app / 'calc.py').write_text(initial)
    (app / 'test_existing.py').write_text('import unittest\nfrom calc import add\nclass Cases(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n def test_good(self): pass\n')
    prepared = await prepare(h, mode='fixer'); started = await grant_start(h, prepared)
    sha = next(i['sha256'] for i in snapshot(h.project)['manifest'] if i['path'] == 'app/calc.py')
    content = initial + '# change\n' if case in {'fewer', 'replaced', 'still-failing'} else 'def add(a,b): return a+b\n# changed\n'
    edits = [{'operation': 'replace', 'path': 'app/calc.py', 'pre_sha256': sha, 'content': content}]
    if case in {'fewer', 'replaced'}:
        extra = 'import unittest, test_existing\ntest_existing.Cases.test_add = None\n'
        if case == 'replaced':
            extra += 'test_existing.Cases.test_replacement = lambda self: None\n'
        edits.append({'operation': 'create', 'path': 'app/test_aaa.py', 'pre_sha256': None, 'content': extra})
    await drive(h, edits)
    view = await h.store.get(h.auth, started['run_id'])
    raw = await h.pg.fetch("SELECT payload FROM checked_step_receipts WHERE run_id=$1 ORDER BY ordinal", started['run_id'])
    receipts = [decoded(r['payload']) for r in raw]
    baseline = next(r['baseline'] for r in receipts if 'baseline' in r)
    assert baseline['tests_run'] == 2
    check = next(r for r in receipts if r.get('check_id') == 'python.unittest')
    assert check['baseline_already_passing'] == (case == 'already-passing')
    if reason:
        assert not check['passed'] and check['assertion_summary'] == reason
        assert view['run']['status'] != 'completed'
        assert await h.pg.fetchval('SELECT count(*) FROM job_proposals') == 0
    else:
        assert view['run']['status'] == 'completed', view['run']
        prop = await h.jobs.command(h.auth, 'jobs.proposal', {'job_id': started['id']})
        assert prop['baseline_already_passing'] == (case == 'already-passing')


@pytest.mark.asyncio
async def test_prepare_uses_configured_default_on_multimodel_backend(work, monkeypatch):
    h = work
    import core.lane_config as lane
    monkeypatch.setattr(lane, 'get_lane', lambda: SimpleNamespace(default_backend='deepseek_v4_flash',
        backends={'deepseek_v4_flash': SimpleNamespace(model='second')}))
    models = await h.admission.models()
    h.admission.catalog = AsyncMock(return_value={**models, 'two': {**models['one'], 'model': 'second'}})
    prepared = await prepare(h)
    request = decoded(await h.pg.fetchval('SELECT job_request FROM work_preparations WHERE id=$1', prepared['preparation_id']))
    team = await h.store.team(h.auth, request['team'], request['team_revision'])
    assert set(team['roles'].values()) == {'two'}


@pytest.mark.asyncio
async def test_prepare_ambiguous_backend_refuses_with_candidates(work):
    h = work; models = await h.admission.models()
    h.admission.catalog = AsyncMock(return_value={**models, 'two': {**models['one'], 'model': 'second'}})
    with pytest.raises(ExecutionError) as error:
        await prepare(h)
    assert error.value.code == 'configured_model_binding_required'
    assert error.value.detail['candidate_binding_ids'] == ['one', 'two']
    assert await h.pg.fetchval('SELECT count(*) FROM work_preparations') == 0


@pytest.mark.asyncio
async def test_prepare_prefers_owned_conversation_model(work, monkeypatch):
    import uuid
    import core.lane_config as lane
    h = work
    await h.pg.execute('ALTER TABLE conversations ADD COLUMN backend_preference TEXT, ADD COLUMN model_preference TEXT')
    cid = uuid.uuid4()
    await h.pg.execute('INSERT INTO conversations(id,user_id,project_id,backend_preference,model_preference) VALUES($1,$2,$3,$4,$5)',
                       cid, h.auth.owner, uuid.UUID(h.pid), 'deepseek_v4_flash', 'second')
    models = await h.admission.models()
    h.admission.catalog = AsyncMock(return_value={**models, 'two': {**models['one'], 'model': 'second'}})
    monkeypatch.setattr(lane, 'get_lane', lambda: SimpleNamespace(default_backend='deepseek_v4_flash',
        backends={'deepseek_v4_flash': SimpleNamespace(model='fixture-model')}))
    prepared = await prepare(h, conversation_id=str(cid))
    request = decoded(await h.pg.fetchval('SELECT job_request FROM work_preparations WHERE id=$1', prepared['preparation_id']))
    team = await h.store.team(h.auth, request['team'], request['team_revision'])
    assert set(team['roles'].values()) == {'two'}
