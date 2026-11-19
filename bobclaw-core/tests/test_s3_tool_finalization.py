"""No provider calls: saved reply and streamed reply must both be visible."""
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from core.nodes import execute
from tests.test_tool_loop import _FakeModel, _tool_call_response


@pytest.mark.parametrize('content', ['', '  \n', [{'type': 'reasoning', 'reasoning': 'private'}]])
async def test_empty_or_reasoning_only_reply_gets_visible_fallback(monkeypatch, content):
    fake = _FakeModel([AIMessage(content=content)])
    monkeypatch.setattr(execute, '_build_tool_model', lambda _: fake)
    events = []
    reply = await execute._run_tool_loop([], 'deepseek_v4_flash', 'assistant-tools', events.append)
    assert reply.strip()
    assert 'no visible answer' in reply
    assert 'private' not in reply
    assert [e['content'] for e in events if e['type'] == 'token'] == [reply]


@pytest.mark.parametrize('fails', [False, True])
async def test_capped_tool_only_reply_is_streamed_and_persisted(monkeypatch, fails):
    from langchain_core.tools import tool

    @tool
    async def last_result() -> str:
        """Return an offline fixture receipt."""
        if fails:
            raise RuntimeError('fixture failure')
        return 'receipt: completed offline check'

    fake = _FakeModel([_tool_call_response('last_result', 'call-1')])
    monkeypatch.setattr(execute, '_build_tool_model', lambda _: fake)
    monkeypatch.setattr(execute, 'get_all_tools', AsyncMock(return_value=[last_result]))
    monkeypatch.setattr(execute.config, 'TOOL_LOOP_MAX_ROUNDS', 2)
    save = AsyncMock()
    monkeypatch.setattr(execute, '_append_agent_turn_event', save)
    events = []
    monkeypatch.setattr(execute, '_get_stream_writer', lambda: events.append)
    result = await execute.execute_node({'task': 'check', 'backend': 'deepseek_v4_flash',
                                         'face_id': 'assistant-tools', 'messages': []})
    reply = result['messages'][-1]['content']
    # The stop is one plain sentence; raw tool results stay out of the visible reply.
    assert reply == 'I reached the limit of steps for one reply before finishing. Reply "continue" to carry on.'
    assert result.get('stopped_reason') == 'tool_round_limit'
    assert result['error'] is None
    assert save.call_args.kwargs['assistant_response'] == reply
    assert [e['content'] for e in events if e['type'] == 'token'] == [reply]
    assert len(fake.bound_model.invocations) == 2


def test_visible_structured_text_excludes_reasoning():
    assert execute._finish_tool_reply(
        [{'type': 'reasoning', 'text': 'private'}, {'type': 'text', 'text': 'answer'}],
        'fixture', None, 'fallback') == 'answer'
