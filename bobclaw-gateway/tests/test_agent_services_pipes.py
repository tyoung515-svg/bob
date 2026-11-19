import hashlib
import json as jsonlib
from unittest.mock import AsyncMock
import time
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from auth import auth_middleware
from config import config
from core.permissions import canonical_pipe_json, compute_pipe_vouch
from routers import agent_services as svc
from routers import chat


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    # These handlers never use SQLite; avoid unrelated DB initialization.
    yield


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOBCLAW_SECRET", "test-secret")
    app = web.Application()
    app.router.add_routes(chat.router)
    svc.setup(app, root=tmp_path)

    class Client:
        async def post(self, path, *, json):
            request = make_mocked_request("POST", path, app=app)
            request.json = AsyncMock(return_value=json)
            response = await auth_middleware(request, chat.pipes)
            class Response:
                status = response.status
                async def json(self):
                    return jsonlib.loads(response.text)
            return Response()
    instance = Client()
    instance.app = app
    return instance


def envelope(**changes):
    args = {"run_id": str(uuid.uuid4())}
    now = int(time.time())
    claims = dict(iss="core", aud="gateway", sub="alice", face="assistant",
                  conversation_id="conversation", op="runs.get",
                  args_sha256=hashlib.sha256(canonical_pipe_json(args).encode()).hexdigest(),
                  iat=now, exp=now + 60, nonce=uuid.uuid4().hex)
    claims.update(changes)
    return dict(claims=claims, args=args, vouch=compute_pipe_vouch(claims, "test-secret"))


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [
    ("bad", "bad_envelope"), ("expired", "envelope_expired"),
    ("args", "args_mismatch"), ("unknown", "unknown_operation"),
    ("aud", "bad_envelope"), ("missing_secret", "bad_envelope"),
    ("extra", "bad_envelope"), ("future", "bad_envelope"),
    ("extra_args", "args_mismatch"), ("unicode_vouch", "bad_envelope"),
])
async def test_envelope_rejection(client, monkeypatch, case, code):
    body = envelope()
    if case == "bad":
        body["vouch"] = "invalid"
    elif case == "expired":
        body = envelope(iat=int(time.time()) - 60, exp=int(time.time()) - 1)
    elif case == "args":
        body["args"]["run_id"] = str(uuid.uuid4())
    elif case == "unknown":
        body = envelope(op="runs.start")
    elif case == "aud":
        body = envelope(aud="core")
    elif case == "missing_secret":
        monkeypatch.setattr(config, "BOBCLAW_SECRET", " ")
    elif case == "extra":
        body["claims"]["extra"] = True
    elif case == "future":
        body = envelope(iat=int(time.time()) + 60, exp=int(time.time()) + 120)
    elif case == "extra_args":
        body["args"]["extra"] = True
    elif case == "unicode_vouch":
        body["vouch"] = "é"
    def no_read(*args, **kwargs):
        pytest.fail("rejected envelope reached run read")
    monkeypatch.setattr(svc, "_read", no_read)
    response = await client.post("/chat/pipes", json=body)
    assert (await response.json())["error"]["code"] == code


@pytest.mark.asyncio
async def test_owned_receipt_replay_foreign_and_missing(client):
    body = envelope()
    rid = body["args"]["run_id"]
    svc._save(client.app, dict(id=rid, owner="alice:human", status="completed",
                               conversation_id="conversation"))
    response = await client.post("/chat/pipes", json=body)
    result = await response.json()
    assert response.status == 200
    assert result == dict(ok=True, operation_id=body["claims"]["nonce"],
                         status="completed", warnings=[],
                         receipt=dict(kind="run", id=rid, independently_verified=False))
    response = await client.post("/chat/pipes", json=body)
    assert (await response.json())["error"]["code"] == "envelope_replayed"
    fresh = envelope()
    fresh["args"]["run_id"] = rid
    fresh["claims"]["args_sha256"] = body["claims"]["args_sha256"]
    fresh["vouch"] = compute_pipe_vouch(fresh["claims"], "test-secret")
    svc._save(client.app, dict(id=rid, owner="other:human", status="completed",
                               conversation_id="conversation"))
    response = await client.post("/chat/pipes", json=fresh)
    assert (await response.json())["error"]["code"] == "run_foreign"
    response = await client.post("/chat/pipes", json=envelope())
    assert (await response.json())["error"]["code"] == "run_not_found"


@pytest.mark.asyncio
async def test_other_auth_behavior_unchanged():
    handler = AsyncMock()
    for method, path in [("GET", "/chat/pipes"), ("POST", "/chat/other")]:
        response = await auth_middleware(make_mocked_request(method, path), handler)
        assert response.status == 401
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_owned_run_is_readable_only_from_its_own_conversation(client):
    """The portal runs are agent-owned; the envelope authorizes on (sub, conversation)."""
    body = envelope()
    rid = body["args"]["run_id"]
    svc._save(client.app, dict(id=rid, owner="alice:" + "a" * 64, status="completed",
                               conversation_id="conversation"))

    response = await client.post("/chat/pipes", json=body)
    result = await response.json()
    assert response.status == 200
    assert result["receipt"] == dict(kind="run", id=rid, independently_verified=False)
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_a_run_from_another_conversation_is_not_readable(client):
    body = envelope()
    rid = body["args"]["run_id"]
    svc._save(client.app, dict(id=rid, owner="alice:" + "a" * 64, status="completed",
                               conversation_id="a-different-conversation"))
    response = await client.post("/chat/pipes", json=body)
    assert (await response.json())["error"]["code"] == "run_foreign"


@pytest.mark.asyncio
async def test_a_run_without_a_conversation_is_not_readable_from_one(client):
    body = envelope()
    rid = body["args"]["run_id"]
    svc._save(client.app, dict(id=rid, owner="alice:" + "a" * 64, status="completed"))
    response = await client.post("/chat/pipes", json=body)
    assert (await response.json())["error"]["code"] == "run_foreign"
