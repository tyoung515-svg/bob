from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest
from aiohttp.test_utils import make_mocked_request

from tools import bob_bridge


def _body(model: str = "bob", *, stream: bool = False, messages=None) -> dict:
    return {
        "model": model,
        "stream": stream,
        "messages": messages or [
            {"role": "system", "content": "Keep it short."},
            {"role": "user", "content": "Hello"},
        ],
    }


def _request(app, path: str, body: dict, headers: dict | None = None):
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    request = make_mocked_request("POST", path, headers=all_headers, app=app)
    request._read_bytes = json.dumps(body).encode()
    return request


def _json_response(response) -> dict:
    return json.loads(response.body)


def _written(request) -> str:
    return b"".join(call.args[0] for call in request._payload_writer.write.call_args_list).decode()


def _sse_events(raw: str) -> list[object]:
    events: list[object] = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            payload = line[6:]
            events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


class FakeContent:
    def __init__(self, chunks: list[bytes]):
        self.chunks = deque(chunks)

    async def readline(self) -> bytes:
        return self.chunks.popleft() if self.chunks else b""

    async def iter_any(self):
        while self.chunks:
            yield self.chunks.popleft()


class FakeUpstreamResponse:
    def __init__(self, *, status=200, chunks=None, body=b"", content_type="application/json"):
        self.status = status
        self.content = FakeContent(chunks or [])
        self._body = body
        self.content_type = content_type
        self.headers = {"Content-Type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode()


class FakeSession:
    def __init__(self, owner):
        self.owner = owner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def post(self, url, **kwargs):
        self.owner.calls.append((url, kwargs))
        return self.owner.responses.popleft()


class FakeHTTP:
    def __init__(self, responses: list[FakeUpstreamResponse]):
        self.responses = deque(responses)
        self.calls: list[tuple[str, dict]] = []

    def session(self, **_):
        return FakeSession(self)


def _bob_response() -> FakeUpstreamResponse:
    frames = [
        {"type": "chunk", "content": "hel"},
        {"type": "chunk", "content": "lo"},
        {"type": "message_complete", "tokens_in": 7, "tokens_out": 2},
    ]
    return FakeUpstreamResponse(
        chunks=[f"data: {json.dumps(frame)}\n\n".encode() for frame in frames],
        content_type="text/event-stream",
    )


@pytest.fixture
def app(monkeypatch):
    fake = FakeHTTP([_bob_response() for _ in range(8)])
    monkeypatch.setattr(bob_bridge.aiohttp, "ClientSession", fake.session)
    return bob_bridge.build_app(engine_url="http://engine.invalid"), fake


async def test_models_are_bob_prefixed_and_face_derived(app):
    bridge, _ = app
    request = make_mocked_request("POST", "/v1/models", app=bridge)
    ids = {item["id"] for item in _json_response(await bob_bridge.models(request))["data"]}
    assert {"bob", "bob-assistant", "bob-researcher"} <= ids
    assert all(model.startswith("bob") for model in ids)
    assert "bob-planner-codex" not in ids


@pytest.mark.parametrize(("model", "face"), [("bob", "assistant"), ("bob-assistant", "assistant")])
async def test_bob_models_route_to_engine_and_select_face(app, model, face):
    bridge, fake = app
    response = await bob_bridge.chat_completions(_request(bridge, "/v1/chat/completions", _body(model)))
    assert response.status == 200
    assert _json_response(response)["choices"][0]["message"]["content"] == "hello"
    engine_body = fake.calls[-1][1]["json"]
    assert engine_body["face_id"] == face
    assert engine_body["pin_authoritative"] is True


async def test_non_bob_model_routes_to_passthrough(monkeypatch):
    fake = FakeHTTP([FakeUpstreamResponse(body=json.dumps({"upstream": True}).encode())])
    monkeypatch.setattr(bob_bridge.aiohttp, "ClientSession", fake.session)
    bridge = bob_bridge.build_app(engine_url="http://engine.invalid", upstream="http://upstream.invalid/v1")
    response = await bob_bridge.chat_completions(
        _request(bridge, "/v1/chat/completions", _body("gpt-5.x"))
    )
    assert response.status == 200
    assert _json_response(response) == {"upstream": True}
    assert fake.calls[0][0] == "http://upstream.invalid/v1/chat/completions"
    assert fake.calls[0][1]["json"]["model"] == "gpt-5.x"


async def test_missing_upstream_is_clear_501(app):
    bridge, fake = app
    response = await bob_bridge.chat_completions(
        _request(bridge, "/v1/chat/completions", _body("gpt-5.x"))
    )
    assert response.status == 501
    error = _json_response(response)["error"]
    assert error["code"] == "upstream_not_configured"
    assert "BOB_BRIDGE_UPSTREAM" in error["message"]
    assert fake.calls == []


@pytest.mark.parametrize(
    ("model", "code"),
    [("bob-planner-codex", "face_role_blocked"), ("bob-no-such-face", "face_unavailable")],
)
async def test_face_guard_rejects_planner_and_unknown(app, model, code):
    bridge, fake = app
    response = await bob_bridge.chat_completions(
        _request(bridge, "/v1/chat/completions", _body(model))
    )
    assert response.status == 503
    assert _json_response(response)["error"]["code"] == code
    assert fake.calls == []


async def test_stream_converts_bob_sse_to_openai_chunks(app):
    bridge, _ = app
    request = _request(bridge, "/v1/chat/completions", _body(stream=True))
    response = await bob_bridge.chat_completions(request)
    assert response.status == 200
    events = _sse_events(_written(request))
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert [event["choices"][0]["delta"]["content"] for event in events[1:3]] == ["hel", "lo"]
    assert events[3]["choices"][0]["finish_reason"] == "stop"
    assert events[3]["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    assert events[4] == "[DONE]"


async def test_non_stream_aggregates_and_maps_usage(app):
    bridge, _ = app
    result = _json_response(await bob_bridge.chat_completions(
        _request(bridge, "/v1/chat/completions", _body())
    ))
    assert result["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}


async def test_same_session_reuses_conversation_id_and_flattens_history(app):
    bridge, fake = app
    headers = {"session-id": "cli-session-42"}
    first = _body(messages=[{"role": "user", "content": "One"}])
    second = _body(messages=[
        {"role": "user", "content": "One"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Two"},
    ])
    await bob_bridge.chat_completions(_request(bridge, "/v1/chat/completions", first, headers))
    await bob_bridge.chat_completions(_request(bridge, "/v1/chat/completions", second, headers))
    first_call, second_call = fake.calls[0][1]["json"], fake.calls[1][1]["json"]
    assert first_call["conversation_id"] == second_call["conversation_id"]
    assert "User: One" in second_call["content"]
    assert "Assistant: First answer" in second_call["content"]
    assert "User: Two" in second_call["content"]


async def test_fallback_session_hash_is_stable(app):
    bridge, fake = app
    await bob_bridge.chat_completions(_request(bridge, "/v1/chat/completions", _body()))
    await bob_bridge.chat_completions(_request(bridge, "/v1/chat/completions", _body()))
    assert fake.calls[0][1]["json"]["conversation_id"] == fake.calls[1][1]["json"]["conversation_id"]


async def test_message_count_guard_fires_at_n_plus_one(monkeypatch):
    fake = FakeHTTP([_bob_response()])
    monkeypatch.setattr(bob_bridge.aiohttp, "ClientSession", fake.session)
    bridge = bob_bridge.build_app(engine_url="http://engine.invalid", max_history=2)
    response = await bob_bridge.chat_completions(_request(
        bridge,
        "/v1/chat/completions",
        _body(messages=[
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]),
    ))
    assert response.status == 400
    error = _json_response(response)["error"]
    assert error["code"] == "history_limit"
    assert "fresh CLI session" in error["message"]
    assert fake.calls == []


async def test_codex_responses_compatibility_uses_session_header(app):
    bridge, fake = app
    body = {
        "model": "bob-assistant",
        "stream": True,
        "instructions": "Be useful.",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "Hello"}]}],
    }
    request = _request(bridge, "/v1/responses", body, {"session-id": "codex-thread"})
    response = await bob_bridge.responses(request)
    assert response.status == 200
    events = _sse_events(_written(request))
    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_item.done",
        "response.completed",
    ]
    usage = events[-1]["response"]["usage"]
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 2
    assert fake.calls[0][1]["json"]["conversation_id"]


async def test_parallel_bob_turns_do_not_serialize_or_leak_content(app, capsys):
    bridge, fake = app
    marker = "PROMPT-MUST-NOT-BE-LOGGED"
    requests = [
        _request(
            bridge,
            "/v1/chat/completions",
            _body(messages=[{"role": "user", "content": f"{marker}-{index}"}]),
            {"authorization": "Bearer SECRET-MUST-NOT-BE-LOGGED", "session-id": f"s-{index}"},
        )
        for index in range(3)
    ]
    responses = await asyncio.gather(*(bob_bridge.chat_completions(request) for request in requests))
    assert [response.status for response in responses] == [200, 200, 200]
    assert len(fake.calls) == 3
    logs = capsys.readouterr().out
    assert "chunks=2" in logs
    assert "turn=1" in logs
    assert marker not in logs
    assert "SECRET-MUST-NOT-BE-LOGGED" not in logs
