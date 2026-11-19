"""Disconnects racing control notices end the socket without an ERROR traceback."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import WSMsgType
import pytest

from app_state import CONVERSATION_STATE_KEY, POSTGRES_POOL_KEY, SESSION_STATE_KEY
from routers import chat


@pytest.mark.parametrize('initial', [False, True])
@pytest.mark.parametrize('frame', [
    {'type':'stop_generation', 'conversation_id':'conv-test'},
    {'type':'switch_model', 'conversation_id':'conv-test'},
    {'type':'stop_generation'},
    {'type':'unsupported'},
])
@pytest.mark.parametrize('error', [ConnectionResetError, aiohttp.ClientConnectionResetError])
async def test_control_notice_on_closing_transport_ends_socket(monkeypatch, initial, frame, error):
    class Socket:
        prepare = AsyncMock()
        send_json = AsyncMock(side_effect=error('Cannot write to closing transport'))

        def __aiter__(self):
            async def incoming():
                yield SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(frame))
                raise AssertionError('A disconnected socket must not read more messages')
            return incoming()

    ws = Socket()
    request = SimpleNamespace(app={POSTGRES_POOL_KEY: None, SESSION_STATE_KEY: {},
                                   CONVERSATION_STATE_KEY: {}})
    monkeypatch.setattr(chat, 'CredentialWebSocketResponse', lambda **kw: ws)
    monkeypatch.setattr(chat, 'authenticate_ws', AsyncMock(return_value=(
        {'sub':'test-user'}, frame if initial else None)))
    monkeypatch.setattr(chat, '_verify_conversation_access', AsyncMock(return_value=True))
    monkeypatch.setattr('core.builder.lifecycle.end_sessions', AsyncMock())
    assert await chat.chat_socket(request) is ws
    ws.send_json.assert_awaited_once()


async def test_control_notice_programming_error_is_not_hidden(monkeypatch):
    ws = SimpleNamespace(prepare=AsyncMock(), send_json=AsyncMock(side_effect=ValueError('bad frame')))
    request = SimpleNamespace(app={POSTGRES_POOL_KEY: None, SESSION_STATE_KEY: {},
                                   CONVERSATION_STATE_KEY: {}})
    monkeypatch.setattr(chat, 'CredentialWebSocketResponse', lambda **kw: ws)
    monkeypatch.setattr(chat, 'authenticate_ws', AsyncMock(return_value=({'sub':'test-user'}, {'type':'unsupported'})))
    with pytest.raises(ValueError, match='bad frame'):
        await chat.chat_socket(request)


async def test_failed_approval_notice_on_reset_socket_is_quiet(monkeypatch, caplog):
    class Context:
        def __init__(self, value):
            self.value = value
        async def __aenter__(self):
            return self.value
        async def __aexit__(self, *args):
            return False

    response = SimpleNamespace(status=400, text=AsyncMock(return_value='Approval forwarding failed'))
    session = SimpleNamespace(post=lambda *a, **kw: Context(response))
    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda: Context(session))
    ws = SimpleNamespace(prepare=AsyncMock(), send_json=AsyncMock(
        side_effect=aiohttp.ClientConnectionResetError('Cannot write to closing transport')))
    request = SimpleNamespace(app={POSTGRES_POOL_KEY: None, SESSION_STATE_KEY: {},
                                   CONVERSATION_STATE_KEY: {}})
    monkeypatch.setattr(chat, 'CredentialWebSocketResponse', lambda **kw: ws)
    monkeypatch.setattr(chat, 'authenticate_ws', AsyncMock(return_value=(
        {'sub':'test-user'}, {'type':'approval_response'})))
    assert await chat.chat_socket(request) is ws
    ws.send_json.assert_awaited_once()
    assert not caplog.records
