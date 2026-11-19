"""Failed core turns must leave visible history with diagnostic metadata."""
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from config import config
from routers import chat


@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('legacy', [False, True])
async def test_core_error_persists_text_and_metadata(monkeypatch, partial, legacy):
    error = {'type': 'error', 'message': 'AttributeError', 'code': 'state_error'}
    if not legacy:
        error.update(error_type='AttributeError', turn_id='turn-9')

    async def core_chat(request):
        response = web.StreamResponse()
        await response.prepare(request)
        events = ([{'type': 'chunk', 'content': 'partial answer'}] if partial else [])
        events += [error, {'type': 'message_complete', 'tokens_out': 0}]
        for event in events:
            await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
        return response

    app = web.Application()
    app.router.add_post('/api/chat', core_chat)
    save = AsyncMock(return_value={'id': 'saved'})
    monkeypatch.setattr(chat, '_save_message', save)
    from routers import execution
    monkeypatch.setattr(execution, 'chat_work_fields', AsyncMock(return_value={}))
    ws = type('Socket', (), {'send_json': AsyncMock()})()
    async with TestServer(app) as server:
        monkeypatch.setattr(config, 'CORE_URL', str(server.make_url('/')).rstrip('/'))
        await chat._stream_chat_to_client(ws, 'conv-9', {'content': 'hello'}, {}, None,
                                          user_id='owner', history=[], token_claims={})
    call = save.call_args
    text, metadata = call.args[3:5]
    assert text.strip()
    if partial:
        assert text == 'partial answer'
    else:
        assert metadata['turn_id'] in text
    assert metadata['error'] == 'AttributeError'
    assert metadata['error_type'] == ('state_error' if legacy else 'AttributeError')
    assert metadata['turn_id']
