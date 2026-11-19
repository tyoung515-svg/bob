"""Offline Responses protocol and real LangChain/native tool-loop integration."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.tools import tool

from core import lane_config as lc
from core.backends.openai_codex import (OpenAICodexClient, ChatOpenAICodex, _MODEL_CACHE,
                                       responses_input, wire_tools)
from core.backends.openai_codex_auth import CodexProviderError, TOKEN_URL
from test_openai_codex_auth import auth_lane, jwt, NOW, set_tokens


def sse(output, *, extra=(), status="completed", deltas=(), model="lane-selected-model"):
    events = [*extra, *({"type": "response.output_text.delta", "delta": t} for t in deltas),
              {"type": "response.completed", "response": {"status": status, "model": model, "output": output, "usage": {"input_tokens": 3}}}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          text="".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n")


def text_item(text="Final answer"):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def call_item(name="memory_recall", arguments='{"query":"test"}'):
    return {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": name, "arguments": arguments}


def pinned(lane, model="lane-selected-model"):
    lane.values["OPENAI_CODEX_MODEL"] = model
    lane.backends["openai_codex"] = lc.LaneBackend("openai_codex", "cloud", model, models=(model,))
    return lane


@pytest.mark.asyncio
async def test_request_headers_body_aliases_and_reasoning(auth_lane):
    lane, _ = auth_lane
    seen = []
    tools = [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object", "properties": {}}}}
             for n in ("web_search", "bob_web_search")]
    def endpoint(request):
        seen.append(request)
        headers = request.headers
        assert headers["chatgpt-account-id"] == "fake-account"
        assert headers["originator"] == "bob" and headers["user-agent"].startswith("Bob/")
        assert headers["x-openai-internal-codex-residency"] == "eu"
        assert headers["authorization"] == "Bearer " + jwt()
        body = json.loads(request.content)
        assert body["stream"] is True and body["store"] is False
        assert body["model"] == "lane-selected-model"
        assert body["instructions"] == "first\n\nsecond"
        assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
        assert [t["name"] for t in body["tools"]] == ["bob_web_search_2", "bob_web_search"]
        return sse([{"type": "reasoning", "id": "rs_1", "encrypted_content": "fake-encrypted-state", "summary": []},
                    text_item(), call_item("bob_web_search_2", '{}')],
                   extra=[{"type": "response.reasoning_summary_text.delta", "delta": "PRIVATE REASONING"}])
    client = OpenAICodexClient(lane=pinned(lane), transport=httpx.MockTransport(endpoint), clock=lambda: NOW)
    result = await client.complete([{"role": "system", "content": "first"}, {"role": "user", "content": "hello"},
                                    {"role": "system", "content": "second"}], tools=tools)
    assert len(seen) == 1 and result["content"] == "Final answer"
    assert result["tool_calls"][0] == {"id": "call_1", "name": "web_search", "args": {}, "type": "tool_call"}
    assert "PRIVATE REASONING" not in json.dumps(result)
    assert result["reasoning"][0]["encrypted_content"] == "fake-encrypted-state"
    assert "secret" not in repr(client)


@pytest.mark.asyncio
async def test_stream_deltas_and_fragmented_function_arguments(auth_lane):
    lane, _ = auth_lane
    events = [{"type": "response.output_item.added", "output_index": 0,
               "item": {"type": "function_call", "name": "memory_recall", "call_id": "call_1", "arguments": ""}},
              {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"query":'},
              {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '"test"}'},
              {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"query":"test"}'}]
    result = await OpenAICodexClient(lane=pinned(lane), clock=lambda: NOW,
        transport=httpx.MockTransport(lambda r: sse([], extra=events, deltas=("hello", " world")))).complete([])
    assert result["content"] == "hello world" and result["tool_calls"][0]["args"] == {"query": "test"}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["missing", "failed", "incomplete", "malformed"])
async def test_stream_failure_never_returns_partial_answer(auth_lane, terminal):
    lane, _ = auth_lane
    if terminal == "missing":
        response = httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
    elif terminal == "malformed":
        response = httpx.Response(200, text='data: {"secret":"fake"\n\n')
    else:
        response = sse([text_item("partial")], status=terminal)
    with pytest.raises(CodexProviderError):
        await OpenAICodexClient(lane=pinned(lane), clock=lambda: NOW,
                               transport=httpx.MockTransport(lambda r: response)).complete([])


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_again", [False, True])
async def test_401_forces_one_refresh_and_one_retry(auth_lane, rejected_again):
    lane, path = auth_lane
    calls = []
    def endpoint(request):
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": jwt(NOW + 7200), "refresh_token": "fake-next-secret"})
        if len(calls) == 1 or rejected_again:
            return httpx.Response(401, text="fake-provider-secret")
        assert request.headers["authorization"] == "Bearer " + jwt(NOW + 7200)
        return sse([text_item()])
    client = OpenAICodexClient(lane=pinned(lane), transport=httpx.MockTransport(endpoint), clock=lambda: NOW)
    if rejected_again:
        with pytest.raises(CodexProviderError, match="sign in again") as error:
            await client.complete([])
        assert error.value.status == 401 and "secret" not in str(error.value)
    else:
        assert (await client.chat([]))["choices"][0]["message"]["content"] == "Final answer"
    assert len(calls) == 3 and calls[1] == TOKEN_URL
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == "fake-next-secret"


@pytest.mark.asyncio
async def test_durable_dispatch_disables_hidden_401_generation_retry(auth_lane):
    lane, _ = auth_lane
    calls = []
    def endpoint(request):
        calls.append(str(request.url))
        return httpx.Response(401, text="rejected")
    client = OpenAICodexClient(lane=pinned(lane), transport=httpx.MockTransport(endpoint), clock=lambda: NOW)
    with pytest.raises(CodexProviderError):
        await client.complete([], retry_unauthorized=False)
    assert len(calls) == 1 and TOKEN_URL not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 429, 500, 503])
async def test_provider_errors_have_status_no_retry_or_token_disclosure(auth_lane, status, caplog):
    caplog.set_level("DEBUG")
    lane, _ = auth_lane
    calls = []
    def endpoint(request):
        calls.append(1)
        return httpx.Response(status, text="fake-provider-secret " + jwt())
    client = OpenAICodexClient(lane=pinned(lane), transport=httpx.MockTransport(endpoint), clock=lambda: NOW)
    with pytest.raises(CodexProviderError) as error:
        await client.complete([])
    assert error.value.status == status and calls == [1]
    assert "secret" not in str(error.value) + repr(error.value) + caplog.text
    assert jwt() not in caplog.text


@pytest.mark.asyncio
async def test_unpinned_catalog_resolution_cached_but_listing_stays_null(auth_lane):
    lane, _ = auth_lane
    _MODEL_CACHE.clear()
    paths = []
    def endpoint(request):
        paths.append(request.url.path)
        assert request.headers["chatgpt-account-id"] == "fake-account"
        if request.method == "GET":
            return httpx.Response(200, json={"models": [
                {"slug": "hidden-model", "visibility": "hide", "priority": 0},
                {"slug": "account-second", "priority": 20},
                {"slug": "account-first", "priority": 10, "supported_in_api": False}]})
        assert json.loads(request.content)["model"] == "account-first"
        return sse([text_item()], model="account-first")
    for _ in range(2):
        client = OpenAICodexClient(lane=lane, transport=httpx.MockTransport(endpoint), clock=lambda: NOW)
        assert (await client.complete([]))["model"] == "account-first"
    assert paths.count("/backend-api/codex/models") == 1
    row = lane.backends["openai_codex"].row()
    assert row["model"] is None and "choose a model" in row["label"]
    assert lc.agent_service_definitions([row])["models"] == {}


@pytest.mark.asyncio
async def test_empty_catalog_is_failure_no_synthetic_model(auth_lane):
    lane, _ = auth_lane
    _MODEL_CACHE.clear()
    calls = []
    def endpoint(request):
        calls.append(1)
        return httpx.Response(200, json={"models": []})
    with pytest.raises(CodexProviderError, match="no visible model"):
        await OpenAICodexClient(lane=lane, transport=httpx.MockTransport(endpoint), clock=lambda: NOW).complete([])
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, "gpt-6.1-sol"])
async def test_assistant_executes_memory_tool_and_replays_result(auth_lane, monkeypatch, override):
    from core.faces.registry import get_default_registry
    from core.nodes import execute as ex
    from core.backends._lc_openai import build_chat_openai, TOOL_CAPABLE_BACKENDS
    lane, _ = auth_lane
    pinned(lane)
    lane.backends["openai_codex"] = lc.LaneBackend("openai_codex", "cloud", "lane-selected-model",
        models=("lane-selected-model", "gpt-6.1-sol"))
    monkeypatch.delenv("BOB_ROSTER_FILE", raising=False)
    monkeypatch.setattr(lc, "get_lane", lambda: lane)
    monkeypatch.setattr(ex, "get_lane", lambda: lane)
    invoked, requests = [], []
    @tool
    async def memory_recall(query: str) -> str:
        """Recall lane memory."""
        invoked.append(query)
        return "fake memory evidence"
    registry = get_default_registry()
    assert {"memory_recall", "web_search", "ask_operator", "list_backends"} <= set(registry.get_allowed_tools("assistant"))
    assert ex._is_tool_enabled("assistant", "openai_codex")
    assert "openai_codex" in TOOL_CAPABLE_BACKENDS
    assert isinstance(build_chat_openai("openai_codex"), ChatOpenAICodex)
    monkeypatch.setattr(ex, "get_all_tools", AsyncMock(return_value=[memory_recall]))
    monkeypatch.setattr(ex, "_append_agent_turn_event", AsyncMock())
    monkeypatch.setattr(ex, "_get_stream_writer", lambda: None)
    def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == (override or "lane-selected-model")
        if len(requests) == 1:
            assert body["tools"][0]["name"] == "memory_recall"
            return sse([call_item()])
        assert {"type": "function_call_output", "call_id": "call_1", "output": "fake memory evidence"} in body["input"]
        assert call_item()["call_id"] == next(i["call_id"] for i in body["input"] if i.get("type") == "function_call")
        return sse([text_item("Memory checked.")])
    client = OpenAICodexClient(lane=lane, clock=lambda: NOW, transport=httpx.MockTransport(endpoint))
    monkeypatch.setattr(ex, "_build_tool_model", lambda backend, model_override=None:
        ChatOpenAICodex(client=client, model_override=model_override))
    result = await ex.execute_node({"backend": "openai_codex", "face_id": "assistant", "task": "Recall test",
                                    "messages": [], "model_override": override})
    assert result.get("error") is None, result
    assert result["messages"][-1]["content"] == "Memory checked."
    assert invoked == ["test"] and len(requests) == 2


@pytest.mark.asyncio
async def test_normal_dispatch_stream_and_configured_failure_has_no_fallback(auth_lane, monkeypatch):
    from core.nodes import execute as ex
    lane, _ = auth_lane
    pinned(lane)
    monkeypatch.delenv("BOB_ROSTER_FILE", raising=False)
    monkeypatch.setattr(lc, "get_lane", lambda: lane)
    monkeypatch.setattr(ex, "get_lane", lambda: lane)
    calls = []
    status = [200]
    def endpoint(request):
        calls.append(1)
        return sse([text_item()]) if status[0] == 200 else httpx.Response(503, text="provider-secret")
    client = OpenAICodexClient(lane=lane, clock=lambda: NOW, transport=httpx.MockTransport(endpoint))
    monkeypatch.setattr("core.backends.openai_codex.OpenAICodexClient", lambda: client)
    assert await ex._default_send_to_backend([], "openai_codex") == "Final answer"
    assert [s async for s in ex._default_stream_to_backend([], "openai_codex")] == ["Final answer"]
    status[0] = 503
    monkeypatch.setattr("core.execution.provider_retry.sleep", AsyncMock())
    monkeypatch.setattr(ex, "_is_tool_enabled", lambda *a: False)
    monkeypatch.setattr(ex, "_append_agent_turn_event", AsyncMock())
    monkeypatch.setattr(ex, "_get_stream_writer", lambda: None)
    out = await ex.execute_node({"backend": "openai_codex", "messages": []})
    assert out["error_type"] == "model_provider_error" and len(calls) == 5


def test_reserved_names_history_uses_same_alias_and_call_id():
    tools, aliases = wire_tools([{"name": "web_search", "parameters": {"type": "object"}}])
    _, history = responses_input([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "web_search", "args": {"query": "test"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "fake result"}], aliases)
    assert history[0]["name"] == tools[0]["name"] == "bob_web_search"
    assert history[0]["call_id"] == history[1]["call_id"] == "call_1"
    assert history[1]["type"] == "function_call_output"


@pytest.mark.asyncio
async def test_401_adopts_cli_rotation_without_spending_refresh(auth_lane):
    lane, path = auth_lane
    calls = []
    def endpoint(request):
        assert str(request.url) != TOKEN_URL
        calls.append(1)
        if len(calls) == 1:
            set_tokens(path, access_token=jwt(NOW + 7200), refresh_token="fake-peer-next")
            return httpx.Response(401)
        assert request.headers["authorization"] == "Bearer " + jwt(NOW + 7200)
        return sse([text_item()])
    result = await OpenAICodexClient(lane=pinned(lane), clock=lambda: NOW,
        transport=httpx.MockTransport(endpoint)).complete([])
    assert result["content"] == "Final answer" and calls == [1, 1]


@pytest.mark.asyncio
async def test_disabled_lane_is_rejected_before_refresh(auth_lane):
    lane, path = auth_lane
    set_tokens(path, access_token=jwt(NOW - 1))
    lane.backends["openai_codex"] = lc.LaneBackend("openai_codex", "cloud", available=False, reason="entitlement_lapsed")
    with pytest.raises(lc.LaneConfigurationError, match="entitlement_lapsed"):
        await OpenAICodexClient(lane=lane, clock=lambda: NOW,
            transport=httpx.MockTransport(lambda r: pytest.fail("no request from unavailable lane"))).complete([])


@pytest.mark.asyncio
async def test_deployment_catalog_exposes_lane_signin_without_changing_roster(auth_lane, monkeypatch):
    from api import server
    from core.config import KNOWN_BACKENDS
    lane, _ = auth_lane
    monkeypatch.setenv("BOB_ROSTER_FILE", str(lane.root / "unused-roster-selector.yaml"))
    monkeypatch.setattr(lc, "get_lane", lambda: lane)
    monkeypatch.setattr(server, "get_lane", lambda: lane)
    assert "openai_codex" in KNOWN_BACKENDS and "openai_codex" in lc.selectable_backends()
    request = SimpleNamespace(app={server.ROUTER_KEY: SimpleNamespace(discover=AsyncMock(return_value=[]))})
    for handler in (server.backends_catalog, server.list_available_models):
        result = json.loads((await handler(request)).text)
        row = next(r for r in result if r["backend"] == "openai_codex")
        assert row["available"] and row["model"] is None and row["models"] == []
    lane.backends.pop("openai_codex")
    assert "openai_codex" not in lc.selectable_backends()


@pytest.mark.asyncio
async def test_team_tool_preference_floats_http_above_cli(monkeypatch):
    from core import teams
    from core.faces.registry import Face
    monkeypatch.setattr(teams, "configured_mode", lambda: False)
    monkeypatch.setattr(teams, "_team_role_cfg", lambda *a: {
        "backend": "codex_code", "escalation_chain": ["openai_codex"]})
    monkeypatch.setattr(teams, "_is_available", AsyncMock(return_value=True))
    face = Face(id="test", name="Test", system_prompt="Test", role="worker",
                preferred_backend="codex_code", escalation_backend="openai_codex")
    assert await teams.resolve("worker", face=face, team="test", want_tools=False) == "codex_code"
    assert await teams.resolve("worker", face=face, team="test", want_tools=True) == "openai_codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, 503])
async def test_account_list_is_read_only_and_never_refreshes_or_generates(auth_lane, status):
    lane, path = auth_lane
    before = path.read_bytes()
    requests = []
    def endpoint(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path.endswith('/models')
        return httpx.Response(status, json={'models': [
            {'slug': 'gpt-6.1-sol', 'is_default': True}, {'slug': 'account-cheaper'}]})
    client = OpenAICodexClient(lane=lane, clock=lambda: NOW,
                              transport=httpx.MockTransport(endpoint))
    if status == 200:
        assert await client.visible_models() == ['gpt-6.1-sol', 'account-cheaper']
    else:
        with pytest.raises(CodexProviderError): await client.visible_models()
    assert len(requests) == 1 and path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [None, "provider-reported-name", "gpt-6.1-sol"])
async def test_observed_model_name_is_not_replaced_by_requested_pin(auth_lane, reported):
    lane, _ = auth_lane
    client = OpenAICodexClient(lane=pinned(lane, 'gpt-6.1-sol'), clock=lambda: NOW,
        transport=httpx.MockTransport(lambda r: sse([text_item()], model=reported)))
    raw = await client.complete([], model='gpt-6.1-sol')
    assert raw['model'] == reported and raw['requested_model'] == 'gpt-6.1-sol'
    response = await ChatOpenAICodex(client=client, model_override='gpt-6.1-sol').ainvoke('test')
    assert response.response_metadata['model_name'] == reported


@pytest.mark.asyncio
async def test_workflow_agent_turn_uses_selected_pin_and_real_tool_loop(auth_lane, monkeypatch):
    from core.execution.agent_turn import AgentTurn, StageContext
    from core.execution.models import digest
    from core.faces.registry import get_default_registry
    from core.nodes import execute as ex
    lane, _ = auth_lane
    pinned(lane, 'lane-default')
    lane.backends['openai_codex'] = lc.LaneBackend('openai_codex', 'cloud', 'lane-default',
        models=('lane-default', 'gpt-6.1-sol'))
    monkeypatch.delenv('BOB_ROSTER_FILE', raising=False)
    monkeypatch.setattr(lc, 'get_lane', lambda: lane)
    monkeypatch.setattr(ex, 'get_lane', lambda: lane)
    called, bodies = [], []
    @tool
    async def memory_recall(query: str) -> str:
        """Recall permitted supplied evidence."""
        called.append(query)
        return '{"evidence":"offline fixture"}'
    monkeypatch.setattr(ex, 'get_all_tools', AsyncMock(return_value=[memory_recall]))
    def endpoint(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert request.method == 'POST' and body['model'] == 'gpt-6.1-sol'
        if len(bodies) == 1:
            return sse([call_item()], model='observed-sol-form')
        assert any(i.get('type') == 'function_call_output' for i in body['input'])
        return sse([text_item('Workflow evidence checked.')], model='observed-sol-form')
    client = OpenAICodexClient(lane=lane, clock=lambda: NOW, transport=httpx.MockTransport(endpoint))
    monkeypatch.setattr(ex, '_build_tool_model', lambda backend, model_override=None:
        ChatOpenAICodex(client=client, model_override=model_override))
    registry = get_default_registry()
    face = registry.get_face('assistant').model_dump(mode='json')
    controller = SimpleNamespace(messages=AsyncMock(return_value=[]), remaining=AsyncMock(return_value=180),
        charge=AsyncMock(side_effect=['attempt-1', 'attempt-2']), model_result=AsyncMock(return_value=None),
        stream=AsyncMock(), validate=AsyncMock(), receipt=AsyncMock())
    record = {'initiator': {'owner': 'owner'}, 'coverage': {'limits': {'workflow_tool_rounds': 3}},
              'resolved': {'faces': {'assistant': {'definition': face, 'digest': digest(face)}}}}
    context = StageContext(controller, {}, record, {'key': 'research', 'conversation_id': 'fixture-conversation'},
        {'brief': 'Read evidence', 'skills': [], 'access': 'read'},
        {'backend': 'openai_codex', 'model': 'gpt-6.1-sol'}, 'assistant')
    assert await AgentTurn().start(context, {'source': 'fixture'}) == 'Workflow evidence checked.'
    assert called == ['test'] and len(bodies) == 2
    assert controller.charge.await_count == controller.model_result.await_count == 2
    assert [c.args[2] for c in controller.model_result.await_args_list] == ['observed-sol-form'] * 2
    controller.receipt.assert_awaited_once_with(context, 'memory_recall', {'evidence': 'offline fixture'})
