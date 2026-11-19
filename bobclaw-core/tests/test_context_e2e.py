"""End to end through /api/chat: history is fitted, the backend call is capped, turn state is stamped.

The backend call itself is stubbed at the module's own seam (``execute._stream_to_backend``), so these run
without network. They use a tool-less face (``reviewer``): faces with native tools go through the tool loop,
which is covered separately in test_context_budget.py. They stand in for the stale history tests in test_api.py,
which stub only the local router and now reach for a real cloud backend.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langgraph.checkpoint.memory import MemorySaver

from api.server import build_app
from core.backends.local_router import LocalBackendInfo, LocalModelRouter
from core.faces.registry import FaceRegistry
from core.graph import build_graph
from core.nodes import execute as execute_mod

PROFILES_DIR = Path(__file__).resolve().parents[2] / "bobclaw-core" / "tests" / "fixtures" / "deployment" / "faces"
FACE = "reviewer"


@pytest.fixture(autouse=True)
def _isolated_context_store(tmp_path, monkeypatch):
    """Chat turns index their history and may schedule compaction: keep both away from the real store and model."""
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "ctx_e2e.db"))
    monkeypatch.setenv("BOB_COMPACTOR_ENABLED", "false")


@pytest.fixture
def faces() -> FaceRegistry:
    return FaceRegistry(profiles_dir=PROFILES_DIR)


@pytest.fixture
def router_stub() -> LocalModelRouter:
    router = LocalModelRouter()
    router.discover = AsyncMock(  # type: ignore[assignment]
        return_value=[LocalBackendInfo(name="ollama", url="http://localhost:11434", models=["gemma-4-27b"])]
    )
    return router


@pytest.fixture
async def chat_client(faces: FaceRegistry, router_stub: LocalModelRouter) -> Any:
    graph = build_graph(checkpointer=MemorySaver())
    app = build_app(faces=faces, router=router_stub, graph=graph)
    async with TestClient(TestServer(app)) as c:
        yield c


def _events(body: bytes) -> list[dict]:
    out = []
    for block in body.decode("utf-8").split("\n\n"):
        block = block.strip()
        if block.startswith("data:"):
            try:
                out.append(json.loads(block[5:].strip()))
            except json.JSONDecodeError:
                pass
    return out


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    async def fake_stream(messages, backend, model_override):
        seen["messages"], seen["backend"] = messages, backend
        yield "ok"

    monkeypatch.setattr(execute_mod, "_stream_to_backend", fake_stream)
    return seen


def _prior(seen: dict) -> str:
    return next(m["content"] for m in seen["messages"] if str(m.get("content", "")).startswith("Prior conversation:"))


async def test_history_reaches_the_backend_whole_on_a_big_window_model(chat_client, monkeypatch):
    seen = _capture(monkeypatch)
    history = [
        {"role": "user", "content": "what is 2+2", "id": "1"},
        {"role": "assistant", "content": "4", "id": "2"},
    ]
    resp = await chat_client.post(
        "/api/chat",
        json={"conversation_id": "c-ctx", "content": "and 3+3?", "face_id": FACE, "history": history},
    )
    assert resp.status == 200
    chunk_text = "".join(e.get("content", "") for e in _events(await resp.read()) if e.get("type") == "chunk")
    assert "ok" in chunk_text and "what is 2+2" not in chunk_text                      # history is not re-emitted
    assert _prior(seen) == "Prior conversation:\nuser: what is 2+2\nassistant: 4"    # no marker, nothing lost


async def test_long_history_to_a_local_model_is_fitted_marked_and_capped(chat_client, monkeypatch):
    seen = _capture(monkeypatch)
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 2_000,
         "id": str(i + 1), "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00"}
        for i in range(200)
    ]
    resp = await chat_client.post(
        "/api/chat",
        json={"conversation_id": "c-ctx", "content": "continue", "face_id": FACE,
              "backend": "local", "history": history},
    )
    events = _events(await resp.read())
    assert resp.status == 200 and seen, f"backend was never called; stream said: {str(events)[:600]}"
    assert seen["backend"] == "local"
    prior = _prior(seen)
    assert prior.startswith("Prior conversation:\nuser: m0 ")                         # opening exchange kept
    assert "[context note:" in prior and "message ids 3.." in prior                   # the gap is announced
    assert "m199 " in prior                                                           # newest turn survives
    assert "user: m2 " not in prior                                                   # the middle is gone
    assert sum(len(str(m.get("content", ""))) for m in seen["messages"]) <= 24_000    # 8K-token local cap


async def test_kill_switch_restores_the_old_flatten(chat_client, monkeypatch):
    monkeypatch.setenv("BOB_CONTEXT_FIT_ENABLED", "false")
    seen = _capture(monkeypatch)
    history = [{"role": "user", "content": "a" * 50_000, "id": "1"}, {"role": "assistant", "content": "b", "id": "2"}]
    resp = await chat_client.post(
        "/api/chat",
        json={"conversation_id": "c-ctx", "content": "q", "face_id": FACE, "history": history},
    )
    events = _events(await resp.read())
    assert resp.status == 200 and seen, f"backend was never called; stream said: {str(events)[:600]}"
    assert _prior(seen) == "Prior conversation:\nuser: " + "a" * 50_000 + "\nassistant: b"


async def test_turn_state_carries_conversation_owner_and_fit_to_the_event_writer(chat_client, monkeypatch):
    _capture(monkeypatch)
    append = AsyncMock()
    monkeypatch.setattr(execute_mod, "_append_agent_turn_event", append)
    resp = await chat_client.post(
        "/api/chat",
        json={"conversation_id": "c-ctx", "content": "hello", "face_id": FACE, "user_id": "dad",
              "history": [{"role": "user", "content": "earlier", "id": "7"}]},
    )
    assert resp.status == 200
    await resp.read()
    state = append.call_args[0][0]
    assert state["conversation_id"] == "c-ctx" and state["user_id"] == "dad"
    fit = state["history_fit"]
    assert fit["messages_in"] == 1 and fit["messages_omitted"] == 0 and fit["backend"] == "deepseek_v4_flash"


async def test_history_from_the_chat_path_is_indexed_so_ids_can_be_expanded(chat_client, monkeypatch, tmp_path):
    from core.context import store

    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "ctx.db"))
    _capture(monkeypatch)
    history = [
        {"role": "user", "content": "first question", "id": "u1", "created_at": "2026-10-23T00:00:00+00:00"},
        {"role": "assistant", "content": "first answer", "id": "a1", "created_at": "2026-10-23T00:00:05+00:00"},
    ]
    resp = await chat_client.post(
        "/api/chat",
        json={"conversation_id": "c-idx", "content": "second question", "face_id": FACE, "user_id": "dad",
              "history": history},
    )
    assert resp.status == 200
    await resp.read()
    rows, missing = store.get_by_ids("c-idx", "dad", ["u1", "a1"])
    assert [r["content"] for r in rows] == ["first question", "first answer"] and missing == []
    assert store.get_by_ids("c-idx", "someone-else", ["u1"])[0] == []             # scoped to the owner


async def test_stored_claims_reach_the_backend_in_place_of_the_omitted_history(chat_client, monkeypatch):
    """The whole live path: history over the budget -> a trusted chain of claims -> /api/chat -> the backend prompt."""
    import re

    from core.context import compactor, store

    monkeypatch.setenv("BOB_CTX_WINDOW_DEEPSEEK_V4_FLASH", "20000")                    # history budget 10,000 tokens
    history = [
        {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant",
         "content": (f"Fact {i}: the setting number {i} is {5000 + i}. " if i % 2 == 0 else f"Noted setting {i - 1}. ") + "x" * 1_100,
         "created_at": f"2026-10-23T00:{i % 60:02d}:00+00:00"}
        for i in range(100)
    ]
    store.record_history("c-claims", "dad", history)

    async def seat(messages):                                                         # offline stand-in for the compactor seat
        found = re.findall(r"^### (\S+) \| user \|[^\n]*\n(.*?)(?=^### |\Z)", messages[-1]["content"], re.M | re.S)
        claims = [{"id": f"c{n}", "type": "fact", "importance": 3, "text": body.strip().split(".")[0], "cites": [mid],
                   "supersedes": None} for n, (mid, body) in enumerate(found[:3], start=1)]
        return json.dumps({"claims": claims, "narrative": {}})

    seat.backend = "fake"
    for _ in range(3):
        await compactor.run_compaction("c-claims", "dad", 20_000, seat)

    seen = _capture(monkeypatch)
    append = AsyncMock()
    monkeypatch.setattr(execute_mod, "_append_agent_turn_event", append)
    resp = await chat_client.post(
        "/api/chat",
        json={"conversation_id": "c-claims", "content": "what is setting 6?", "face_id": FACE, "user_id": "dad",
              "history": history},
    )
    events = _events(await resp.read())
    assert resp.status == 200 and seen, f"backend was never called; stream said: {str(events)[:600]}"
    prior = _prior(seen)
    assert "[compacted history:" in prior and "Fact 6: the setting number 6 is 5006" in prior
    assert prior.startswith("Prior conversation:\nuser: Fact 0:") and "assistant: Noted setting 98." in prior
    assert "user: Fact 6:" not in prior                                                # the raw message was replaced by its claim
    claims_meta = append.call_args[0][0]["history_fit"]["claims"]                       # the audit trail on the turn event
    assert claims_meta["regions"] >= 1 and claims_meta["claims_shown"] >= 3
    # the same request with the feature off is the plain Phase 0 prompt
    monkeypatch.setenv("BOB_CLAIMS_IN_PROMPT", "false")
    seen.clear()
    resp = await chat_client.post(
        "/api/chat",
        json={"conversation_id": "c-claims", "content": "what is setting 6?", "face_id": FACE, "user_id": "dad",
              "history": history},
    )
    await resp.read()
    assert "[compacted history:" not in _prior(seen) and "[context note:" in _prior(seen)
