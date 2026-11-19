"""Tests for the face-callable memory tools (F2, 2026-10-01).

Covers: registry wiring, legible failure when memory is not bootstrapped,
the save path (L0 event + awaited projection + fact row with provenance),
and the recall path (hits / no-hits / upstream failure).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from core.tools import memory_tools
from core.tools.memory_tools import memory_recall, memory_save
from core.tools.registry import NATIVE_TOOLS


# ── Fakes ────────────────────────────────────────────────────────────────────

@dataclass
class _FakeEvent:
    event_id: str = "evt-test-1"


class _FakeEventLog:
    def __init__(self):
        self.bodies: list[dict] = []

    async def atomic_append(self, body: dict):
        self.bodies.append(body)
        return _FakeEvent()


@dataclass
class _FakeProjectResult:
    event_id: str
    status: str = "COMPLETED"
    chunk_count: int = 2


class _FakeWriter:
    def __init__(self):
        self.projected: list[str] = []

    async def project(self, event):
        self.projected.append(event.event_id)
        return _FakeProjectResult(event.event_id)


class _FakeFactStore:
    def __init__(self):
        self.facts: list = []

    async def put(self, fact):
        self.facts.append(fact)
        return fact.fact_id


@dataclass
class _FakeChunk:
    content: str
    source_fact_id: str | None = None


class _FakeRetriever:
    def __init__(self, chunks=None, raises=None):
        self._chunks = chunks or []
        self._raises = raises

    async def search(self, query, top_k=3, **kwargs):
        if self._raises:
            raise self._raises
        return self._chunks[:top_k]


class _FakeSingletons:
    def __init__(self, retriever=None):
        self.event_log = _FakeEventLog()
        self.writer = _FakeWriter()
        self.fact_store = _FakeFactStore()
        self.retriever = retriever or _FakeRetriever()


# ── Registry wiring ──────────────────────────────────────────────────────────

def test_memory_tools_registered():
    assert "memory_save" in NATIVE_TOOLS
    assert "memory_recall" in NATIVE_TOOLS


# ── memory_save ──────────────────────────────────────────────────────────────

async def test_memory_save_not_bootstrapped(monkeypatch):
    def _boom():
        raise RuntimeError("memory not bootstrapped")

    monkeypatch.setattr(memory_tools, "_singletons", _boom)
    out = await memory_save.ainvoke({"text": "remember the milk"})
    assert out.startswith("[error:memory_unavailable]")


async def test_memory_save_empty_text():
    out = await memory_save.ainvoke({"text": "   "})
    assert out.startswith("[error:empty]")


async def test_memory_save_happy_path(monkeypatch):
    fake = _FakeSingletons()
    monkeypatch.setattr(memory_tools, "_singletons", lambda: fake)

    face_tok = memory_tools.memory_tool_face_id.set("assistant")
    conv_tok = memory_tools.memory_tool_conversation_id.set("conv-42")
    try:
        out = await memory_save.ainvoke({"text": "operator prefers flash slugs"})
    finally:
        memory_tools.memory_tool_face_id.reset(face_tok)
        memory_tools.memory_tool_conversation_id.reset(conv_tok)

    assert "Saved to long-term memory" in out
    assert "event_id=evt-test-1" in out
    assert "index=COMPLETED chunks=2" in out
    assert "fact=stored" in out

    # L0 event carries the note + provenance; BOTH text fields carry the note
    # because the T0 writer only embeds assistant_response (retrieval contract).
    body = fake.event_log.bodies[0]
    assert "operator prefers flash slugs" in body["user_message"]
    assert "operator prefers flash slugs" in body["assistant_response"]
    assert body["source"] == "memory_save_tool"
    assert body["face_id"] == "assistant"
    assert body["conversation_id"] == "conv-42"

    # Projection was awaited (not fire-and-forget) on this event
    assert fake.writer.projected == ["evt-test-1"]

    # Fact row references the event and uses the blake3 input-hash format
    fact = fake.fact_store.facts[0]
    assert fact.source_event_id == "evt-test-1"
    assert fact.generation_method == "face_memory_save:v1"
    assert fact.input_hash.startswith("blake3:")
    assert len(fact.input_hash) == len("blake3:") + 64
    assert fact.body["text"] == "operator prefers flash slugs"


async def test_memory_save_append_failure_is_legible(monkeypatch):
    fake = _FakeSingletons()

    async def _fail(body):
        raise OSError("disk full")

    fake.event_log.atomic_append = _fail
    monkeypatch.setattr(memory_tools, "_singletons", lambda: fake)
    out = await memory_save.ainvoke({"text": "won't stick"})
    assert out.startswith("[error:l0_append]")
    assert fake.fact_store.facts == []


# ── memory_recall ────────────────────────────────────────────────────────────

async def test_memory_recall_hits(monkeypatch):
    fake = _FakeSingletons(
        retriever=_FakeRetriever(chunks=[_FakeChunk("note A"), _FakeChunk("note B")])
    )
    monkeypatch.setattr(memory_tools, "_singletons", lambda: fake)
    out = await memory_recall.ainvoke({"query": "notes"})
    assert "Memory hits:" in out
    assert "- note A" in out
    assert "- note B" in out


async def test_memory_recall_no_hits(monkeypatch):
    fake = _FakeSingletons(retriever=_FakeRetriever(chunks=[]))
    monkeypatch.setattr(memory_tools, "_singletons", lambda: fake)
    out = await memory_recall.ainvoke({"query": "nothing"})
    assert out == "No memory found for that query."


async def test_memory_recall_upstream_failure(monkeypatch):
    fake = _FakeSingletons(retriever=_FakeRetriever(raises=TimeoutError("embedder down")))
    monkeypatch.setattr(memory_tools, "_singletons", lambda: fake)
    out = await memory_recall.ainvoke({"query": "anything"})
    assert out.startswith("[error:recall]")
