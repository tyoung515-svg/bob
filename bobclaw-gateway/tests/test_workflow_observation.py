"""Gateway long-poll, signed workflow context and broker rate-limit recovery."""
import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from routers import workflows
from tests.test_workflows import broker, client


@pytest.fixture
async def observation(monkeypatch):
    events = []
    calls = []
    async def proxy(request, operation, args):
        calls.append((operation, args))
        return web.json_response({'events': events.copy(), 'next_seq': events[-1]['seq'] if events else args.get('after_seq', 0), 'has_more': False})
    monkeypatch.setattr(workflows, 'proxy', proxy)
    monkeypatch.setattr(workflows, 'EVENT_CHECK_INTERVAL', 0.01)
    app = web.Application()
    app.add_routes(workflows.router)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c, events, calls
    finally:
        await c.close()


async def test_long_poll_returns_early_on_new_event(observation):
    c, events, calls = observation
    async def emit():
        await asyncio.sleep(0.04)
        events.append({'seq': 8, 'kind': 'run_state'})
    task = asyncio.create_task(emit())
    before = time.monotonic()
    response = await c.get('/workflow-runs/run-1/events', params={'contract_version': 1, 'after_seq': 7, 'wait': 1})
    page = await response.json()
    await task
    assert response.status == 200 and page['events'][0]['seq'] == 8
    assert 0.02 <= time.monotonic()-before < 0.8
    assert len(calls) > 1 and all('wait' not in args for _, args in calls)


async def test_long_poll_times_out_empty_without_mutation(observation):
    c, _, calls = observation
    before = time.monotonic()
    response = await c.get('/workflow-runs/run-1/events', params={'contract_version': 1, 'after_seq': 7, 'wait': 0.05})
    assert response.status == 200 and not (await response.json())['events']
    assert 0.04 <= time.monotonic()-before < 0.5
    assert all(op == 'workflows.events' for op, _ in calls)


@pytest.mark.parametrize('wait', ['-1', '26', 'nan', 'inf', 'true'])
async def test_wait_bounds(observation, wait):
    c, _, calls = observation
    response = await c.get('/workflow-runs/run-1/events', params={'contract_version': 1, 'after_seq': 0, 'wait': wait})
    assert response.status == 422 and not calls


async def test_long_poll_returns_refusal_immediately(observation, monkeypatch):
    c, _, _ = observation
    denied = AsyncMock(return_value=web.json_response({'error': {'code': 'not_found'}}, status=404))
    monkeypatch.setattr(workflows, 'proxy', denied)
    before = time.monotonic()
    response = await c.get('/workflow-runs/run-1/events', params={'contract_version': 1, 'after_seq': 0, 'wait': 25})
    assert response.status == 404 and time.monotonic()-before < 0.5
    denied.assert_awaited_once()


@pytest.mark.parametrize('face', ['assistant', 'planner'])
async def test_gateway_issues_separate_signed_workflow_context(monkeypatch, face):
    from routers.execution import chat_work_fields
    from core.tools.workflow_context import valid
    from config import config
    from core.execution.chat_context import valid as valid_work
    monkeypatch.setattr(config, 'BOBCLAW_SECRET', 'wb2b-gateway-signature')
    monkeypatch.setattr('core.execution.owner.owner_mapping', lambda owner: False)
    claims = {'sub': 'owner', 'token_type': 'portal', 'exp': int(time.time())+600}
    fields = await chat_work_fields(claims, 'conversation', face)
    assert 'work_context' not in fields
    value = fields['workflow_context']
    assert valid(value, fields['workflow_context_vouch'], config.BOBCLAW_SECRET,
        owner='owner', conversation='conversation', face=face)
    assert not valid_work(value, fields['workflow_context_vouch'], config.BOBCLAW_SECRET,
        owner='owner', conversation='conversation', face=face)
    assert not valid(value, fields['workflow_context_vouch'], config.BOBCLAW_SECRET,
        owner='owner', conversation='forged', face=face)
    orchestrator = await chat_work_fields(claims, 'conversation', 'orchestrator')
    assert 'work_context' in orchestrator and 'workflow_context' in orchestrator
    for other_face in ('builder', 'fixer', 'researcher', 'reviewer'):
        assert 'workflow_context' not in await chat_work_fields(claims, 'conversation', other_face)
    assert not await chat_work_fields({**claims, 'session_kind': 'owner_stepup'}, 'conversation', face)


def test_watch_long_polls_and_survives_rate_limit_without_cancelling(broker):
    module, b, sink = broker
    waits = []
    class Stop:
        stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds):
            waits.append(seconds)
            return self.stopped
    stop = Stop()
    event = {'run_id': 'run-1', 'seq': '9007199254740993', 'kind': 'run_state', 'payload': {}}
    responses = [module.GatewayRateLimited(retry_after=3), {'events': [event], 'has_more': False}]
    seen = []
    def get(operation, args):
        seen.append((operation, args.copy()))
        result = responses.pop(0)
        if isinstance(result, Exception): raise result
        return result
    b.op_workflows = get
    def emitted(name, payload):
        if name == 'workflow_event': stop.stopped = True
    sink.event.side_effect = emitted
    b._workflow_watches['run-1'] = stop
    b._watch_workflow('run-1', 9007199254740992, stop)
    assert waits == [3]
    assert len(seen) == 2 and all(op == 'events' and args['wait'] == 25 for op, args in seen)
    assert seen[0][1]['after_seq'] == seen[1][1]['after_seq']
    assert [call.args[0] for call in sink.event.call_args_list] == ['workflow_event']
    b._owner_rest.assert_not_called()


def test_unwatch_interrupts_retry_delay_without_stopping_run(broker):
    module, b, sink = broker
    class Stop:
        stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds): self.stopped = True; return True
    stop = Stop()
    def limited(*args): raise module.GatewayRateLimited(retry_after=30)
    b.op_workflows = limited
    b._workflow_watches['r'] = stop
    b._watch_workflow('r', 0, stop)
    b._rest.assert_not_called()
    b._owner_rest.assert_not_called()
    sink.event.assert_not_called()


def test_event_transport_has_timeout_above_wait(broker):
    _, b, _ = broker
    b.op_workflows('events', {'contract_version': 1, 'run_id': 'r', 'after_seq': 0, 'wait': 25})
    assert b._rest.call_args.kwargs['params']['wait'] == 25
    assert b._rest.call_args.kwargs['timeout'] >= 35
