"""Claude review/refusal/format repair through the real workflow controller."""
import json
from unittest.mock import AsyncMock
import pytest
from core.backends.claude_turn import ClaudeTurn
from core.execution.workflow_context import current
from test_workflow_controller import harness, drive, mock_redis
from test_cc2_claude_turn import FakeLink
from test_bu15_stage_shell import shell_transport


@pytest.mark.asyncio
async def test_claude_review_refusal_and_bu13_repair(harness, monkeypatch):
    h = harness
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'worker')
    monkeypatch.setattr('core.lane_config.require_backend', lambda *a: None)
    original = h.service.runtime.bindings
    async def bindings(auth):
        rows = await original(auth)
        # Premade alias 2 is the reviewer; keep build/plan and their real tools.
        for row in rows:
            if row['binding_id'] == '2':
                row.update(backend='claude_code', model='sonnet', model_identity='anthropic/claude', family='anthropic')
        return rows
    monkeypatch.setattr(h.service.runtime, 'bindings', bindings)
    turns, links, records = [], [], []
    def factory(**kwargs):
        ctx = current.get()
        if ctx.repairing:
            answer = json.dumps({'verdict': 'pass', 'summary': 'Read the frozen workspace.',
                'findings': [], 'question': None, 'snapshot_digest': ctx.stage['base_snapshot']['digest']})
            operations = [{'text': answer}]
            assert not kwargs['tools']
        else:
            # The forbidden tool enters the real StageContext refusal handler;
            # it must not execute, and Claude may recover to an answer.
            operations = [{'tools': [{'name': 'fs_write', 'args': {'path': 'forbidden.py', 'content': 'no'}}]},
                          {'text': 'Wrong answer format'}]
            assert {t.name for t in kwargs['tools']} == ctx.bound_names
            assert not {t.name for t in kwargs['tools']} & {'fs_write', 'shell_run', 'ask_operator'}
        link = FakeLink(operations)
        turn = ClaudeTurn(**kwargs, connector=link.connect, recorder=lambda *r: records.append(r))
        turns.append(turn); links.append(link)
        return turn
    monkeypatch.setattr('core.backends.claude_turn.ClaudeTurn', factory)
    row = await h.new()
    result = await drive(h, row)
    assert result['status'] == 'completed', result['reason']
    review = next(s for s in result['stages'] if '/review-' in s['key'])
    assert review['calls'] == 3  # refused round, answer, repair
    assert len(review['format_repairs']) == 1
    assert review['receipts'][0]['result']['status'] == 'refused'
    assert review['actual_models'][0]['provider_reported_model'] == 'claude-sonnet-test-full'
    assert not (h.roots[result['project_id']] / 'forbidden.py').exists()
    assert len(turns) == 2 and all(t.closed for t in turns)
    assert any(kind == 'usage' for _, kind, _ in records)
    assert result['budget']['consumed_calls'] == len(h.calls) + 3


def test_claude_review_identity_is_conservative_across_aliases():
    from core.execution.workflows import review_facts, PREMADES
    definition = PREMADES['premade-plan-build-review-fix']
    bindings = {name: {'model_identity': 'anthropic/claude', 'family': 'anthropic', 'reasons': []}
                for name in definition['agents']}
    result = review_facts({**definition, 'review_policy': 'different_model'}, bindings)
    assert not result['policy_met'] and result['same_model_choice_required']


@pytest.fixture(autouse=True)
def _lane_configured_by_bob(monkeypatch):
    # The confined Claude path exists only for a lane configured through Bob's
    # own sign-in; the suite default (conftest) is the legacy roster.
    monkeypatch.delenv('BOB_ROSTER_FILE', raising=False)


@pytest.mark.asyncio
async def test_claude_build_shell_and_one_refusal_count_per_assistant_reply(harness, shell_transport, monkeypatch):
    """Several MCP calls from one Claude message share one refusal allowance."""
    h = harness
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'worker')
    monkeypatch.setattr('core.lane_config.require_backend', lambda *a: None)
    original_bindings = h.service.runtime.bindings

    async def bindings(auth):
        rows = await original_bindings(auth)
        for row in rows:
            if row['binding_id'] == '1':
                row.update(backend='claude_code', model='sonnet', model_identity='anthropic/claude', family='anthropic')
        return rows

    monkeypatch.setattr(h.service.runtime, 'bindings', bindings)
    contexts, links, turns, counts = [], [], [], []
    original_before_tool = __import__('core.execution.agent_turn', fromlist=['StageContext']).StageContext.before_tool

    async def before_tool(ctx, name, args):
        if name == 'shell_run' and args.get('command') == 'pwd' and args.get('cwd') == './':
            counts.append(ctx.refusals)
        return await original_before_tool(ctx, name, args)

    monkeypatch.setattr('core.execution.agent_turn.StageContext.before_tool', before_tool)

    def factory(**kwargs):
        ctx = current.get()
        contexts.append(ctx)
        root = str(ctx.controller.root(ctx.record))
        operations = [
            {'tools': [{'name': 'shell_run', 'args': {'command': 'ls', 'cwd': root}},
                       {'name': 'shell_run', 'args': {'command': 'pwd'}}]},
            {'tools': [{'name': 'work_catalog', 'args': {}}, {'name': 'work_prepare', 'args': {}}]},
            {'tools': [{'name': 'shell_run', 'args': {'command': 'pwd', 'cwd': './'}}]},
            {'text': 'The workspace commands ran; no deterministic check was requested.'},
        ]
        assert 'shell_run' in ctx.bound_names
        link = FakeLink(operations)
        turn = ClaudeTurn(**kwargs, connector=link.connect, recorder=lambda *args: None)
        turns.append(turn)
        links.append(link)
        return turn

    monkeypatch.setattr('core.backends.claude_turn.ClaudeTurn', factory)
    row = await h.new(checks=False)
    run = await drive(h, row)
    assert run['status'] == 'completed', run['reason']
    build = next(s for s in run['stages'] if s.get('access') == 'write')
    assert build['calls'] == 4  # three assistant tool messages and the answer
    refused = [r for r in build['receipts'] if r['result'].get('status') == 'refused']
    assert [r['tool'] for r in refused] == ['work_catalog', 'work_prepare']
    assert counts == [1]  # the second gate sees the already normalized cwd
    assert contexts[0].refusals == 0
    assert len(shell_transport.dispatched) == 3
    assert all(d['payload']['args']['cwd'] == '' for d in shell_transport.dispatched)
    assert not run['report']['checks']
    assert 'No deterministic check ran.' in run['report']['caveats']
    assert len(turns) == 1 and turns[0].closed
    replies = [r['result'] for r in links[0].responses if 'result' in r and 'content' in r['result']]
    assert len(replies) == 5
