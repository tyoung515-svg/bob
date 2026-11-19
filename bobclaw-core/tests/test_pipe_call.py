from unittest.mock import AsyncMock, MagicMock
import aiohttp
import pytest
from pydantic import ValidationError
from core.config import config
from core.tools.registry import pipe_call, _skill_face_id
from core.tools.projects import _current_user_id, _current_conversation_id


@pytest.fixture
async def identity(monkeypatch):
    monkeypatch.setattr(config, "BOBCLAW_SECRET", "test-secret")
    pairs = [(v, v.set(value)) for v, value in [
        (_current_user_id, "alice"), (_current_conversation_id, "conversation"),
        (_skill_face_id, "assistant")]]
    yield
    for var, token in reversed(pairs):
        var.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"op": "runs.start", "args": {"run_id": "run"}},
    {"op": "runs.get", "args": {"run_id": "run", "extra": 1}},
    {"op": "runs.get", "args": {"run_id": "run"}, "extra": 1},
])
async def test_strict_operation(payload):
    with pytest.raises(ValidationError):
        await pipe_call.ainvoke(payload)


@pytest.mark.asyncio
async def test_missing_identity():
    result = await pipe_call.ainvoke({"op": "runs.get", "args": {"run_id": "run"}})
    assert result["status"] == "refused"
    assert result["warnings"] == ["missing_identity"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [aiohttp.ClientConnectionError(), TimeoutError()])
async def test_transport_failure(identity, monkeypatch, failure):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.side_effect = failure
    monkeypatch.setattr(aiohttp, "ClientSession", MagicMock(return_value=session))
    result = await pipe_call.ainvoke({"op": "runs.get", "args": {"run_id": "run"}})
    assert result["status"] == "unknown" and not result["ok"], result
    assert len(result["operation_id"]) == 32


@pytest.mark.asyncio
async def test_receipt(identity, monkeypatch):
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    def post(url, *, json):
        assert url.endswith("/chat/pipes")
        receipt = dict(ok=True, operation_id=json["claims"]["nonce"], status="completed",
                       receipt=dict(kind="run", id="run", independently_verified=False),
                       warnings=[])
        response.json.return_value = receipt
        return response
    session.post.side_effect = post
    monkeypatch.setattr(aiohttp, "ClientSession", MagicMock(return_value=session))
    result = await pipe_call.ainvoke({"op": "runs.get", "args": {"run_id": "run"}})
    assert set(result) == {"ok", "operation_id", "status", "receipt", "warnings"}
    assert result["receipt"] == {"kind": "run", "id": "run", "independently_verified": False}


def test_skill_availability():
    from core.faces.registry import get_default_registry
    registry = get_default_registry()
    assert registry.load_skill("assistant", "bob.run-results")["ok"]
    for skill in registry.list_skills("assistant"):
        if skill["skill_id"] != "bob.run-results":
            with pytest.raises(PermissionError):
                registry.load_skill("assistant", skill["skill_id"])


@pytest.mark.asyncio
async def test_blank_secret_refused(identity, monkeypatch):
    monkeypatch.setattr(config, "BOBCLAW_SECRET", " ")
    result = await pipe_call.ainvoke({"op": "runs.get", "args": {"run_id": "run"}})
    assert not result["ok"] and result["warnings"] == ["bad_envelope"]
