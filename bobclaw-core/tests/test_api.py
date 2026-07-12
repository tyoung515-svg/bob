"""
BoBClaw Core — Unit tests for the REST plumbing (B1a)

Covers ``api.server.build_app`` and its handlers for /health, /api/faces,
/api/faces/{id}, /api/models/local, and the 501 placeholders for
/api/chat and /api/chat/approval (which B1b/B1c will replace).

No Postgres or SQLite required: the app factory accepts injected stubs,
and the Postgres pool is omitted (only /api/chat* needs it, and those
are placeholders here).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from langgraph.checkpoint.memory import MemorySaver

from api.server import ROUTER_KEY, build_app
from core.backends.local_router import LocalBackendInfo, LocalModelRouter
from core.faces.registry import FaceRegistry
from core.graph import build_graph


# ─── Fixtures ─────────────────────────────────────────────────────────────────

PROFILES_DIR = Path(__file__).parent.parent / "core" / "faces" / "profiles"
EXPECTED_FACE_IDS = {
    "builder-bob",
    "researcher",
    "reviewer",
    "council-lite",
    "council-max",
    "assistant",
    "assistant-actions",
    "assistant-tools",
    "assistant-tools-mcp",
    "planner-claude",
    "planner-cc-edit",
    "planner-cc-edit-codex",
    "planner-minimax",
    "planner-kimi",
    "worker-kimi",
    "worker-kimi-bulk",
    "worker-deepseek",
    "worker-opencode",
    "planner-gemini",
    "worker-agy",
    "planner-codex",
    "planner-gpt",
    "worker-codex",
    "worker-kimi-cli",
}


@pytest.fixture
def faces() -> FaceRegistry:
    return FaceRegistry(profiles_dir=PROFILES_DIR)


@pytest.fixture
def router_stub() -> LocalModelRouter:
    """A router whose ``discover`` is an AsyncMock returning one fake backend."""
    router = LocalModelRouter()
    router.discover = AsyncMock(  # type: ignore[assignment]
        return_value=[
            LocalBackendInfo(
                name="ollama",
                url="http://localhost:11434",
                models=["gemma-4-27b", "llama3-8b"],
            )
        ]
    )
    return router


@pytest.fixture
async def client(
    faces: FaceRegistry, router_stub: LocalModelRouter
) -> Any:
    """Base client without a LangGraph attached (B1a endpoints only)."""
    app = build_app(faces=faces, router=router_stub)
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.fixture
async def chat_client(
    faces: FaceRegistry, router_stub: LocalModelRouter
) -> Any:
    """Client with a MemorySaver-backed graph for /api/chat tests."""
    graph = build_graph(checkpointer=MemorySaver())
    app = build_app(faces=faces, router=router_stub, graph=graph)
    async with TestClient(TestServer(app)) as c:
        yield c


def _parse_sse(body: bytes) -> list[dict]:
    """Split an SSE body into event dicts for assertion."""
    events: list[dict] = []
    for block in body.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data:"):
            continue
        try:
            events.append(json.loads(block[5:].strip()))
        except json.JSONDecodeError:
            continue
    return events


# ─── /health ──────────────────────────────────────────────────────────────────

async def test_health_returns_ok(client):
    resp = await client.get("/health")
    assert resp.status == 200
    assert await resp.json() == {"status": "ok"}


async def test_health_surfaces_degraded_write_fence(client, monkeypatch):
    import api.server as server_mod
    from types import SimpleNamespace

    monkeypatch.setattr(server_mod.config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(
        server_mod,
        "get_memory",
        lambda: SimpleNamespace(
            write_fence=SimpleNamespace(
                degraded=True,
                degraded_reason="permission",
                resource_identity="http://localhost:6333|bobclaw_",
            )
        ),
    )

    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["memory_write_fence_degraded"] is True
    assert body["memory_write_fence"]["writes_refused"] is True
    assert body["memory_write_fence"]["resource"] == (
        "http://localhost:6333|bobclaw_"
    )

    assert body["memory_write_fence"]["reason"] == "permission"


async def test_health_reports_closed_fence_as_writes_refused(
    client, monkeypatch, tmp_path: Path,
):
    """A released lock is closed/read-only even though it is not degraded."""
    import api.server as server_mod
    from core.ledger.federation import FederationRegistry
    from core.memory.write_fence import WriteFence

    fence = WriteFence(
        FederationRegistry(tmp_path / "registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
        lock_dir=tmp_path / "locks",
    )
    fence.close()
    assert fence.degraded is False
    assert fence.lock_held is False
    monkeypatch.setattr(server_mod.config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(
        server_mod,
        "get_memory",
        lambda: SimpleNamespace(write_fence=fence),
    )

    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["memory_write_fence_degraded"] is False
    assert body["memory_write_fence"]["writes_refused"] is True
    assert body["memory_write_fence"]["reason"] == "lock_not_held"


# ─── /api/faces ───────────────────────────────────────────────────────────────

async def test_list_faces_returns_all_profiles(client):
    resp = await client.get("/api/faces")
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list)
    ids = {f["id"] for f in body}
    assert ids == EXPECTED_FACE_IDS


async def test_list_faces_items_have_summary_fields(client):
    resp = await client.get("/api/faces")
    body = await resp.json()
    for item in body:
        assert {"id", "name", "avatar", "preferred_backend", "ui_theme"} <= set(item)


async def test_get_face_returns_full_profile(client):
    resp = await client.get("/api/faces/builder-bob")
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == "builder-bob"
    assert body["name"] == "Builder Bob"
    # full profile includes system_prompt and allowed_tools
    assert body["system_prompt"].strip()
    assert isinstance(body["allowed_tools"], list)


async def test_get_face_unknown_returns_404(client):
    resp = await client.get("/api/faces/phantom")
    assert resp.status == 404
    body = await resp.json()
    assert body["type"] == "error"
    assert body["code"] == "face_not_found"
    assert "phantom" in body["message"]


# ─── /api/models/local ────────────────────────────────────────────────────────

async def test_list_local_models_calls_discover(client, router_stub):
    resp = await client.get("/api/models/local")
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["name"] == "ollama"
    assert "gemma-4-27b" in body[0]["models"]
    router_stub.discover.assert_awaited_once()


async def test_list_local_models_empty_when_nothing_discovered(faces):
    empty_router = LocalModelRouter()
    empty_router.discover = AsyncMock(return_value=[])  # type: ignore[assignment]
    app = build_app(faces=faces, router=empty_router)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/api/models/local")
        assert resp.status == 200
        assert await resp.json() == []


# ─── /api/models/available ────────────────────────────────────────────────────

async def test_available_models_lists_local_and_cloud_backends(client, router_stub):
    """/api/models/available returns local (available via discover) + every
    wired cloud backend, each with a boolean `available` flag."""
    resp = await client.get("/api/models/available")
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body, list)

    by_backend = {e["backend"]: e for e in body}
    # Local is available (router stub discovers ollama) and carries its models.
    assert by_backend["local"]["available"] is True
    assert "gemma-4-27b" in by_backend["local"]["models"]

    # Every wired backend is present, each with a boolean availability flag.
    for name in (
        "claude_api", "deepseek_v4_flash", "kimi_code", "kimi_platform",
        "minimax", "gemini_flash", "gemini_pro", "gemini_deep_research",
        "opencode_serve",
    ):
        assert name in by_backend, f"{name} missing from /models/available"
        assert isinstance(by_backend[name]["available"], bool)
    router_stub.discover.assert_awaited()


async def test_available_models_cloud_available_tracks_key_presence(faces, router_stub):
    """A cloud backend is `available` iff its API key is configured."""
    import api.server as server_mod

    app = build_app(faces=faces, router=router_stub)
    async with TestClient(TestServer(app)) as c:
        # Force a known key state without touching the real environment.
        original = server_mod.config.DEEPSEEK_API_KEY
        try:
            server_mod.config.DEEPSEEK_API_KEY = "sk-test"
            body = await (await c.get("/api/models/available")).json()
            ds = {e["backend"]: e for e in body}["deepseek_v4_flash"]
            assert ds["available"] is True
            assert ds["model"]  # default model id surfaced

            server_mod.config.DEEPSEEK_API_KEY = ""
            body2 = await (await c.get("/api/models/available")).json()
            ds2 = {e["backend"]: e for e in body2}["deepseek_v4_flash"]
            assert ds2["available"] is False
        finally:
            server_mod.config.DEEPSEEK_API_KEY = original


async def test_available_models_local_unavailable_when_nothing_discovered(faces):
    empty_router = LocalModelRouter()
    empty_router.discover = AsyncMock(return_value=[])  # type: ignore[assignment]
    app = build_app(faces=faces, router=empty_router)
    async with TestClient(TestServer(app)) as c:
        body = await (await c.get("/api/models/available")).json()
        local = {e["backend"]: e for e in body}["local"]
        assert local["available"] is False
        assert local["models"] == []


# ─── /api/memory/facts (T4) ───────────────────────────────────────────────────

from core.memory.exceptions import L1ValidationFailed  # noqa: E402
from core.memory.models import ConfidenceStub, Fact  # noqa: E402


def _l1_fact(fid: str, text: str, gen: str = "extract_facts_from_event") -> Fact:
    return Fact(
        fact_id=fid,
        generation_method=gen,
        body={"text": text, "subject": "subj", "predicate": "pred"},
        source_event_id=f"evt-{fid}",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(),
        ts="2026-06-15T00:00:00+00:00",
    )


class _FakeFactStore:
    def __init__(self, facts, order):
        self._facts = {f.fact_id: f for f in facts}
        self.deleted: list[str] = []
        self._order = order

    async def query(self, filters):
        gm = filters.get("generation_method")
        return [
            f for f in self._facts.values()
            if gm is None or f.generation_method == gm
        ]

    async def get(self, fid):
        if fid not in self._facts:
            raise L1ValidationFailed(fid, ["fact not found"])
        return self._facts[fid]

    async def delete(self, fid):
        self._order.append("sqlite_delete")
        self.deleted.append(fid)
        self._facts.pop(fid, None)


class _FakeIndexer:
    def __init__(self, order):
        self.dropped: list[list[str]] = []
        self._order = order

    async def drop_facts(self, fids):
        self._order.append("vector_drop")
        self.dropped.append(list(fids))
        return len(fids)


class _FakeMem:
    def __init__(self, facts):
        self.order: list[str] = []
        self.fact_store = _FakeFactStore(facts, self.order)
        self.indexer = _FakeIndexer(self.order)


def _enable_memory(monkeypatch, mem):
    import api.server as server_mod
    monkeypatch.setattr(server_mod.config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(server_mod, "get_memory", lambda: mem)


async def test_list_memory_facts_returns_only_l1(client, monkeypatch):
    mem = _FakeMem([
        _l1_fact("f1", "user likes tea"),
        _l1_fact("f2", "user lives in Thailand"),
        _l1_fact("m1", "manual fact", gen="manual_seed"),  # not L1
    ])
    _enable_memory(monkeypatch, mem)

    resp = await client.get("/api/memory/facts")
    assert resp.status == 200
    body = await resp.json()
    ids = {row["fact_id"] for row in body}
    assert ids == {"f1", "f2"}  # manual_seed filtered out
    row = next(r for r in body if r["fact_id"] == "f1")
    assert {"fact_id", "text", "subject", "predicate", "ts",
            "source_event_id", "confidence"} <= set(row)
    assert row["text"] == "user likes tea"


async def test_list_memory_facts_paginates(client, monkeypatch):
    mem = _FakeMem([_l1_fact(f"f{i}", f"fact {i}") for i in range(5)])
    _enable_memory(monkeypatch, mem)
    resp = await client.get("/api/memory/facts?limit=2&offset=0")
    assert len(await resp.json()) == 2


async def test_list_memory_facts_empty_when_disabled(client):
    # MEMORY_ENABLED defaults to False in the test env → graceful [], no 500.
    resp = await client.get("/api/memory/facts")
    assert resp.status == 200
    assert await resp.json() == []


async def test_forget_fact_deletes_vector_then_sqlite(client, monkeypatch):
    mem = _FakeMem([_l1_fact("f1", "junk meta-fact")])
    _enable_memory(monkeypatch, mem)

    resp = await client.delete("/api/memory/facts/f1")
    assert resp.status == 200
    assert await resp.json() == {"status": "forgotten", "fact_id": "f1"}
    # Both stores hit, vector FIRST (so a SQLite failure can't strand a vector).
    assert mem.indexer.dropped == [["f1"]]
    assert mem.fact_store.deleted == ["f1"]
    assert mem.order == ["vector_drop", "sqlite_delete"]


async def test_forget_fact_write_fence_exception_mapping_remains_backstop(
    client, monkeypatch,
):
    from core.memory.write_fence import WriteFenceViolation

    mem = _FakeMem([_l1_fact("f1", "locked")])
    mem.write_fence = SimpleNamespace(
        degraded=False,
        degraded_reason="",
        lock_held=True,
    )

    async def refuse(_fact_ids):
        raise WriteFenceViolation("bobclaw__768", "lock lost during write")

    mem.indexer.drop_facts = refuse
    _enable_memory(monkeypatch, mem)

    resp = await client.delete("/api/memory/facts/f1")
    assert resp.status == 423
    body = await resp.json()
    assert body["reason"] == "write_fence_violation"
    assert mem.fact_store.deleted == []


def _build_real_degraded_memory(tmp_path: Path, monkeypatch, *, points):
    from core.ledger.federation import FederationRegistry
    from core.memory.acl import ACLRegistry
    from core.memory.indexer import MemoryIndexer
    from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
    from core.memory.write_fence import WriteFence

    monkeypatch.setenv("BOBCLAW_WRITE_FENCE_LOCK_DIR", str(tmp_path / "locks"))
    holder = WriteFence(
        FederationRegistry(tmp_path / "holder-registry.json"),
        qdrant_url="http://localhost:6353",
        collection_prefix="bobclaw_",
    )
    degraded = WriteFence(
        FederationRegistry(tmp_path / "contender-registry.json"),
        qdrant_url="http://127.0.0.1:6353",
        collection_prefix="bobclaw_",
    )
    assert degraded.degraded_reason == "contention"

    acl_path = tmp_path / "stores.toml"
    acl_path.write_text(
        "[store.s]\n"
        'allowed_locality = ["local"]\n'
        'allowed_provider_ids = ["p"]\n'
        'allowed_capability_classes = ["text_dense"]\n',
        encoding="utf-8",
    )
    qdrant = MagicMock()
    qdrant.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name="bobclaw__768")]
    )
    qdrant.scroll.return_value = (points, None)
    provider = QdrantRetrievalProvider(
        provider_id="p",
        locality="local",
        collection_prefix="bobclaw_",
        acl_registry=ACLRegistry(acl_path),
        client=qdrant,
        write_fence=degraded,
    )
    fact_store = _FakeFactStore([_l1_fact("f1", "locked")], [])
    indexer = MemoryIndexer(
        fact_store=fact_store,
        embedder=MagicMock(),
        provider=provider,
        store_id="s",
        slot_resolver=MagicMock(),
    )
    mem = SimpleNamespace(
        fact_store=fact_store,
        indexer=indexer,
        write_fence=degraded,
    )
    return holder, degraded, mem, qdrant, fact_store


async def test_forget_fact_with_real_degraded_fence_returns_423(
    client, monkeypatch, tmp_path: Path,
):
    holder, degraded, mem, qdrant, fact_store = _build_real_degraded_memory(
        tmp_path,
        monkeypatch,
        points=[SimpleNamespace(id="chunk:f1:old")],
    )
    _enable_memory(monkeypatch, mem)

    try:
        resp = await client.delete("/api/memory/facts/f1")
        assert resp.status == 423
        body = await resp.json()
        assert body["code"] == "memory_write_locked"
        assert body["reason"] == "contention"
        assert fact_store.deleted == []
        qdrant.delete.assert_not_called()
    finally:
        degraded.close()
        holder.close()


async def test_forget_fact_degraded_fence_zero_vector_hits_refuses_before_sqlite(
    client, monkeypatch, tmp_path: Path,
):
    """Auditor premise: zero matching vectors cannot bypass degraded read-only state."""
    holder, degraded, mem, qdrant, fact_store = _build_real_degraded_memory(
        tmp_path,
        monkeypatch,
        points=[],
    )
    _enable_memory(monkeypatch, mem)

    try:
        resp = await client.delete("/api/memory/facts/f1")
        assert resp.status == 423
        body = await resp.json()
        assert body["reason"] == "contention"
        assert fact_store.deleted == []
        qdrant.scroll.assert_not_called()
        qdrant.delete.assert_not_called()
    finally:
        degraded.close()
        holder.close()


async def test_forget_unknown_fact_returns_404(client, monkeypatch):
    mem = _FakeMem([_l1_fact("f1", "exists")])
    _enable_memory(monkeypatch, mem)
    resp = await client.delete("/api/memory/facts/does-not-exist")
    assert resp.status == 404
    assert (await resp.json())["code"] == "fact_not_found"
    assert mem.indexer.dropped == []  # never touched the vector store


async def test_forget_when_disabled_returns_503(client):
    resp = await client.delete("/api/memory/facts/f1")
    assert resp.status == 503
    assert (await resp.json())["code"] == "memory_unavailable"


# ─── /api/memory/graph (U4a) ──────────────────────────────────────────────────


async def test_memory_graph_empty_when_disabled(client):
    # MEMORY_ENABLED defaults to False in the test env → empty graph, no 500.
    resp = await client.get("/api/memory/graph")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"nodes": [], "edges": [], "meta": {"node_count": 0, "edge_count": 0}}


class _GraphFakeEventLog:
    async def replay(self, *a, **k):
        for eid in ("e1",):
            body = {"user_message": "hi", "assistant_response": "yo",
                    "face_id": "assistant", "turn_id": "t1"}
            yield type("E", (), {"event_id": eid, "kind": "agent_turn",
                                 "body": body, "ts": "2026-07-08T00:00:00+00:00"})()


class _GraphFakeProvider:
    _client = None  # no qdrant in this unit test → facts+conversations only
    collection_prefix = "bobclaw_"


class _GraphFakeMem:
    def __init__(self, facts):
        self.fact_store = _FakeFactStore(facts, [])
        self.event_log = _GraphFakeEventLog()
        self.retriever = type("R", (), {"_provider": _GraphFakeProvider()})()


async def test_memory_graph_returns_assembled_shape(client, monkeypatch):
    mem = _GraphFakeMem([_l1_fact("f1", "user likes tea")])
    # _l1_fact stamps source_event_id="evt-f1"; point the conversation at it so a
    # provenance edge forms only if ids line up. Use e1 for the conversation and a
    # fact whose source_event_id is e1.
    from core.memory.models import ConfidenceStub, Fact
    mem.fact_store._facts = {
        "f1": Fact(fact_id="f1", generation_method="extract_facts_from_event",
                   body={"text": "user likes tea", "subject": "user", "predicate": "likes"},
                   source_event_id="e1", input_hash="blake3:" + "a" * 64,
                   confidence=ConfidenceStub(), ts="2026-07-08T00:00:00+00:00")
    }
    _enable_memory(monkeypatch, mem)

    resp = await client.get("/api/memory/graph?nodes=100&k=5")
    assert resp.status == 200
    body = await resp.json()
    types = {n["type"] for n in body["nodes"]}
    assert types == {"fact", "conversation"}
    prov = [e for e in body["edges"] if e["type"] == "provenance"]
    assert prov == [{"source": "fact:f1", "target": "conversation:e1", "type": "provenance"}]
    assert body["meta"]["node_cap"] == 100


async def test_memory_graph_rejects_bad_params(client, monkeypatch):
    _enable_memory(monkeypatch, _GraphFakeMem([]))
    resp = await client.get("/api/memory/graph?nodes=notanint")
    assert resp.status == 400
    assert (await resp.json())["code"] == "invalid_request"


# ─── /api/chat (B1b) ──────────────────────────────────────────────────────────

async def test_chat_returns_503_when_graph_missing(client):
    """When no graph is attached (B1a client has none), chat is unavailable."""
    resp = await client.post(
        "/api/chat", json={"conversation_id": "c1", "content": "hi"}
    )
    assert resp.status == 503
    body = await resp.json()
    assert body["code"] == "graph_unavailable"


async def test_chat_rejects_invalid_json(chat_client):
    resp = await chat_client.post(
        "/api/chat", data="not-json", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["code"] == "invalid_json"


async def test_chat_rejects_missing_fields(chat_client):
    resp = await chat_client.post("/api/chat", json={"conversation_id": "c1"})
    assert resp.status == 400
    body = await resp.json()
    assert body["code"] == "invalid_request"


async def test_chat_streams_chunks_and_completes(chat_client, monkeypatch):
    """Happy path: safe task flows through execute_node's streaming branch."""

    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        for token in ["Hello", ", ", "world", "!"]:
            yield token

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    # route_node has its own module-level router — patch it too so the
    # graph resolves backend="ollama" instead of falling back to claude_api.
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c1",
            "content": "Say hello",
            "face_id": "reviewer",
        },
    )
    assert resp.status == 200
    assert resp.headers.get("Content-Type", "").startswith("text/event-stream")

    body = await resp.read()
    events = _parse_sse(body)

    chunk_events = [e for e in events if e.get("type") == "chunk"]
    complete_events = [e for e in events if e.get("type") == "message_complete"]

    assert chunk_events, "expected at least one chunk event"
    # True per-token streaming: the 4 router deltas arrive as 4 separate chunk
    # events, not one re-chunked end-of-turn blob.
    assert len(chunk_events) >= 2, "expected per-token chunks, not a single blob"
    joined = "".join(e.get("content", "") for e in chunk_events)
    assert "Hello, world!" in joined
    assert len(complete_events) == 1
    assert complete_events[0]["tokens_out"] >= 1
    assert complete_events[0]["elapsed_ms"] >= 0


async def test_chunk_backend_is_resolved_in_auto_mode(chat_client, monkeypatch):
    """E-OBS: an unpinned (AUTO) turn must emit chunk.backend = the backend the
    router actually resolved, not blank. The UI uses this to show who answered."""

    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        yield "hi"

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    # No "backend"/"model" in the request → AUTO. reviewer prefers local, so
    # route_node resolves backend to the discovered local backend ("ollama").
    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-auto",
            "content": "Say hi",
            "face_id": "reviewer",
        },
    )
    assert resp.status == 200
    events = _parse_sse(await resp.read())
    chunk_events = [e for e in events if e.get("type") == "chunk"]
    assert chunk_events
    assert all(e.get("backend") == "ollama" for e in chunk_events), (
        "chunk.backend must be the resolved backend in AUTO mode, not blank"
    )


async def test_chat_emits_approval_request_for_dangerous_task(chat_client):
    """Dangerous tasks hit the execute_node approval branch → SSE approval_request."""
    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c2",
            "content": "send email to boss@example.com about the budget",
            "face_id": "builder-bob",
        },
    )
    assert resp.status == 200
    body = await resp.read()
    events = _parse_sse(body)

    approvals = [e for e in events if e.get("type") == "approval_request"]
    completes = [e for e in events if e.get("type") == "message_complete"]

    assert len(approvals) == 1
    assert approvals[0]["approval_id"]
    assert approvals[0]["details"]["thread_id"].startswith("c2:")
    # When approval is emitted we do NOT send message_complete — it will come
    # from the resumed turn after /api/chat/approval (B1c).
    assert completes == []


# ─── /api/chat/approval (B1c) ─────────────────────────────────────────────────

async def test_approval_returns_503_when_graph_missing(client):
    """No graph attached → 503, consistent with /api/chat."""
    resp = await client.post(
        "/api/chat/approval",
        json={"approval_id": "x", "decision": "approve"},
    )
    assert resp.status == 503
    body = await resp.json()
    assert body["code"] == "graph_unavailable"


async def test_approval_rejects_invalid_json(chat_client):
    resp = await chat_client.post(
        "/api/chat/approval",
        data="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["code"] == "invalid_json"


async def test_approval_rejects_bad_decision(chat_client):
    resp = await chat_client.post(
        "/api/chat/approval",
        json={"approval_id": "abc", "decision": "maybe"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["code"] == "invalid_request"


async def test_approval_unknown_id_returns_404(chat_client):
    resp = await chat_client.post(
        "/api/chat/approval",
        json={"approval_id": "does-not-exist", "decision": "approve"},
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["code"] == "approval_not_found"


async def test_approval_approve_resumes_and_completes(chat_client, monkeypatch):
    """Dangerous task → approval_request; then /api/chat/approval(approve)
    resumes the graph, runs the backend, and streams message_complete."""

    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        for token in ["Email ", "sent."]:
            yield token

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    # Turn 1: dangerous task triggers approval_request
    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-approve",
            "content": "send email to boss@example.com about the budget",
            "face_id": "builder-bob",
        },
    )
    assert resp.status == 200
    events = _parse_sse(await resp.read())
    approvals = [e for e in events if e.get("type") == "approval_request"]
    assert len(approvals) == 1
    approval_id = approvals[0]["approval_id"]

    # Turn 2: approve → graph resumes, backend runs, message_complete emitted
    resp2 = await chat_client.post(
        "/api/chat/approval",
        json={"approval_id": approval_id, "decision": "approve"},
    )
    assert resp2.status == 200
    assert resp2.headers.get("Content-Type", "").startswith("text/event-stream")
    events2 = _parse_sse(await resp2.read())

    chunks = [e for e in events2 if e.get("type") == "chunk"]
    completes = [e for e in events2 if e.get("type") == "message_complete"]
    approvals2 = [e for e in events2 if e.get("type") == "approval_request"]

    joined = "".join(e.get("content", "") for e in chunks)
    assert "Email sent." in joined
    assert len(completes) == 1
    assert approvals2 == []  # no re-prompt once approved


async def test_approval_id_is_single_use(chat_client, monkeypatch):
    """Using the same approval_id twice → 404 on second attempt."""

    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        for token in ["ok"]:
            yield token

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-replay",
            "content": "send email to vendor@example.com with po",
            "face_id": "builder-bob",
        },
    )
    events = _parse_sse(await resp.read())
    approval_id = next(
        e["approval_id"] for e in events if e.get("type") == "approval_request"
    )

    # First consume — succeeds
    first = await chat_client.post(
        "/api/chat/approval",
        json={"approval_id": approval_id, "decision": "approve"},
    )
    assert first.status == 200
    await first.read()

    # Replay — 404
    second = await chat_client.post(
        "/api/chat/approval",
        json={"approval_id": approval_id, "decision": "approve"},
    )
    assert second.status == 404
    assert (await second.json())["code"] == "approval_not_found"


async def test_approval_reject_terminates_without_backend_call(
    chat_client, monkeypatch
):
    """Reject → execute_node sees approval_response='rejected' and ends
    the turn with a system message; the backend must not be called."""

    call_count = {"n": 0}

    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        call_count["n"] += 1
        yield "should-not-run"

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-reject",
            "content": "send email to ceo@example.com urgently",
            "face_id": "builder-bob",
        },
    )
    events = _parse_sse(await resp.read())
    approval_id = next(
        e["approval_id"] for e in events if e.get("type") == "approval_request"
    )

    resp2 = await chat_client.post(
        "/api/chat/approval",
        json={"approval_id": approval_id, "decision": "reject"},
    )
    assert resp2.status == 200
    events2 = _parse_sse(await resp2.read())

    completes = [e for e in events2 if e.get("type") == "message_complete"]
    assert len(completes) == 1
    # Backend must NOT have been called on reject
    assert call_count["n"] == 0


# ─── build_app wiring ─────────────────────────────────────────────────────────

def test_build_app_injects_state(faces, router_stub):
    from api.server import FACES_KEY

    app = build_app(faces=faces, router=router_stub)
    assert app[FACES_KEY] is faces
    assert app[ROUTER_KEY] is router_stub


def test_build_app_defaults_construct_real_registry():
    """Without injection, the factory constructs a FaceRegistry from disk."""
    app = build_app()
    from api.server import FACES_KEY

    assert isinstance(app[FACES_KEY], FaceRegistry)
    assert len(app[FACES_KEY]) == 24


# ─── Stream disconnect robustness ──────────────────────────────────────────────

async def test_chat_handles_midstream_disconnect_cleanly(chat_client, monkeypatch, caplog):
    """When the client disconnects mid-stream, no exception propagates and
    an INFO log records the disconnect (not an ERROR traceback)."""
    import logging

    disconnect_count = [0]

    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        for token in ["Hello", ", ", "world", "!"]:
            yield token

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    # Simulate disconnect: the first response.write raises ConnectionResetError
    from api import server as server_mod

    _orig_safe_write = server_mod._safe_write

    async def _fake_safe_write(response, data, conversation_id):
        disconnect_count[0] += 1
        return False

    monkeypatch.setattr(server_mod, "_safe_write", _fake_safe_write)

    caplog.set_level(logging.INFO, logger="api.server")

    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-disconnect",
            "content": "Say hello",
            "face_id": "reviewer",
        },
    )
    assert resp.status == 200

    body = await resp.read()
    events = _parse_sse(body)
    completes = [e for e in events if e.get("type") == "message_complete"]

    # No message_complete — stream was interrupted
    assert completes == []
    # Exception must not propagate — status 200, not 500
    assert disconnect_count[0] >= 1


# ─── History replay (conversation continuity) ──────────────────────────────────

async def test_history_injected_as_system_message_not_re_emitted_as_chunks(
    chat_client, monkeypatch,
):
    """Trap 2: history messages must NOT appear in SSE chunk events.
    Only the NEW assistant output should be streamed."""
    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        for token in ["response"]:
            yield token

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    history = [
        {"role": "user", "content": "what is 2+2"},
        {"role": "assistant", "content": "4"},
    ]

    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-history",
            "content": "what is 3+3",
            "face_id": "reviewer",
            "history": history,
        },
    )
    assert resp.status == 200
    events = _parse_sse(await resp.read())
    chunks = [e for e in events if e.get("type") == "chunk"]

    chunk_text = " ".join(e.get("content", "") for e in chunks)
    assert "response" in chunk_text
    assert "what is 2+2" not in chunk_text
    assert "4" not in chunk_text


async def test_history_does_not_block_duplicate_user_task(
    chat_client, monkeypatch,
):
    """Trap 1: when history contains a user message with the same text as the
    current task, the current task must still be appended as a new user message.
    History is injected as role=system so it can never collide with the
    execute_node duplicate check (which only scans role=user messages)."""
    captured_messages = []

    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        captured_messages.extend(messages)
        yield "ok"

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-dup",
            "content": "hello",
            "face_id": "reviewer",
            "history": history,
        },
    )
    assert resp.status == 200
    await resp.read()

    user_msgs = [m for m in captured_messages if m.get("role") == "user"]
    assert any(
        m.get("content") == "hello" for m in user_msgs
    ), "current user task must be present in model prompt"


async def test_history_injection_still_produces_message_complete(
    chat_client, monkeypatch,
):
    """History must not interfere with normal turn completion.
    The model receives context and still produces a response."""
    async def fake_discover():
        return [LocalBackendInfo("ollama", "http://x", ["gemma-4-27b"])]

    async def fake_chat(messages, model=None, backend=None):
        yield "acknowledged"

    from core.nodes import execute as execute_mod
    from core.nodes import route as route_mod

    monkeypatch.setattr(execute_mod._router, "discover", fake_discover)
    monkeypatch.setattr(execute_mod._router, "chat", fake_chat)
    monkeypatch.setattr(route_mod._router, "discover", fake_discover)

    history = [
        {"role": "user", "content": "set x=1"},
        {"role": "assistant", "content": "done"},
    ]

    resp = await chat_client.post(
        "/api/chat",
        json={
            "conversation_id": "c-ctx",
            "content": "what is x",
            "face_id": "reviewer",
            "history": history,
        },
    )
    assert resp.status == 200
    events = _parse_sse(await resp.read())
    completes = [e for e in events if e.get("type") == "message_complete"]
    assert len(completes) == 1
    assert completes[0]["tokens_out"] >= 1

