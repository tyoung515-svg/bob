"""Real local HTTP request serialization tests; no paid provider calls."""
import json
from unittest.mock import AsyncMock
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from core.backends.glm import GLMClient
from core.nodes import execute

@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_flash_pin_reaches_serialized_http_request(monkeypatch, stream):
    received = []
    async def handler(request):
        body = await request.json(); received.append(body)
        if body['stream']:
            return web.Response(text='data: '+json.dumps({'choices':[{'delta':{'content':'fixture answer'}}]})+'\n\ndata: [DONE]\n\n',content_type='text/event-stream')
        return web.json_response({'choices':[{'message':{'content':'fixture answer'}}]})
    app=web.Application(); app.router.add_post('/chat/completions',handler)
    async with TestServer(app) as server:
        client=GLMClient(base_url=str(server.make_url('/')).rstrip('/'),api_key='fixture-only')
        monkeypatch.setattr('core.backends.glm.GLMClient',lambda:client)
        monkeypatch.setattr('core.flight.enforcement.acquire_or_escalate',AsyncMock(return_value=('glm_5_3',None)))
        monkeypatch.setattr('core.flight.enforcement.release',AsyncMock())
        messages=[{'role':'user','content':'fixture'}]
        if stream:
            answer=''.join([d async for d in execute._default_stream_to_backend(messages,'glm_5_3','glm-5.3-flash')])
        else:
            answer=await execute._default_send_to_backend(messages,'glm_5_3','glm-5.3-flash')
    assert answer=='fixture answer'
    assert received[0]['model']=='glm-5.3-flash'

@pytest.mark.asyncio
async def test_blank_timeout_reaches_core_sse_error_frame(monkeypatch):
    from aiohttp.test_utils import TestClient
    from api.server import _stream_graph_turn
    monkeypatch.setattr(execute, '_is_tool_enabled', lambda *a: False)
    monkeypatch.setattr(execute, '_get_stream_writer', lambda: None)
    monkeypatch.setattr(execute, '_stream_and_collect', AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(execute, '_append_agent_turn_event', AsyncMock())
    class Graph:
        async def astream(self, *a, **kw):
            result=await execute.execute_node({'task':'probe','backend':'glm_5_3',
                'face_id':'researcher','messages':[{'role':'user','content':'probe'}]})
            yield ('updates', {'execute':result})
    async def handler(request):
        return await _stream_graph_turn(request=request,graph=Graph(),graph_input={},
            thread_id='fixture',approvals={},face_id='researcher',user_content='probe',
            model_override='glm-5.3-flash',backend_override='glm_5_3')
    app=web.Application(); app.router.add_post('/probe',handler)
    async with TestClient(TestServer(app)) as client:
        response=await client.post('/probe'); text=await response.text()
    events=[json.loads(line[5:]) for line in text.splitlines() if line.startswith('data:')]
    errors=[e for e in events if e.get('type')=='error']
    assert errors, text
    assert 'TimeoutError' in errors[0]['message']

@pytest.mark.parametrize('module,cls', [
    ('glm','GLMClient'), ('deepseek','DeepSeekClient'), ('kimi','KimiClient'),
    ('kimi_platform','KimiPlatformClient'), ('minimax','MiniMaxClient'),
    ('gemini','GeminiClient'), ('claude','ClaudeClient'),
    ('ollama','OllamaClient'), ('lmstudio','LMStudioClient')])
def test_model_clients_allow_180_seconds(module, cls):
    import importlib
    client=getattr(importlib.import_module('core.backends.'+module),cls)()
    assert client._timeout.total == 180
    assert client._timeout.connect == 5

@pytest.mark.parametrize('backend', ['deepseek_v4_flash','glm_5_3','qwen_research'])
def test_tool_model_wrapper_allows_180_seconds(monkeypatch, backend):
    from core.backends import _lc_openai
    monkeypatch.setitem(_lc_openai._LC_OPENAI_BACKENDS, backend,
                        lambda: {'base_url':'http://fixture.invalid','api_key':'fixture-only','model':'fixture'})
    monkeypatch.setattr(_lc_openai, 'ChatOpenAI', lambda **kwargs: kwargs)
    assert _lc_openai.build_chat_openai(backend).get('timeout') == 180



