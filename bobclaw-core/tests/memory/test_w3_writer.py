from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.ledger.federation import FederationRegistry
from core.memory._db import init_schema
from core.memory.acl import ACLRegistry
from core.memory.event_log import SQLiteEventLog
from core.memory.models import (
    ChunkRecord,
    ConfidenceStub,
    Event,
    Fact,
    Hit,
    RankedResults,
    SlotResolution,
)
from core.memory.providers.qdrant_provider import QdrantRetrievalProvider
from core.memory.retriever import MemoryRetriever
from core.memory.write_fence import WriteFenceViolation
from core.memory.writer.ledger import CompletionLedger
from core.memory.writer.replay import replay_t0
from core.memory.writer.tasks import (
    DeriveFactsTask,
    ProjectVerbatimTask,
    ReconcileTask,
    TagTaskClassTask,
    chunk_fixed,
)
from core.nodes.recall import recall_node


class FakeEmbedder:
    embedding_dimension = 4

    async def embed(self, texts):
        return [
            [float((sum(text.encode("utf-8")) % 97) + 1), 1.0, 0.0, 0.0]
            for text in texts
        ]


class FakeProvider:
    def __init__(self):
        self.points = {}
        self.index_calls = 0
        self.delete_calls = 0

    def scroll_payload(self, store_id, payload_filter, batch_size=128):
        for point_id, item in list(self.points.items()):
            if all(item.payload.get(k) == v for k, v in payload_filter.items()):
                yield point_id

    def delete(self, store_id, item_ids):
        self.delete_calls += 1
        for point_id in item_ids:
            self.points.pop(point_id, None)

    def index(self, store_id, items):
        self.index_calls += 1
        for item in items:
            self.points[item.id] = item


def event(event_id: str, response: str, *, user: str = "") -> Event:
    return Event(
        event_id=event_id,
        kind="agent_turn",
        body={"user_message": user, "assistant_response": response},
        ts="2026-08-31T12:34:56+00:00",
        hash=f"hash-{event_id}",
        prev_hash=None,
    )


async def build_task(tmp_path, **versions):
    db_path = tmp_path / "memory.db"
    await init_schema(db_path)
    ledger = CompletionLedger(db_path)
    await ledger.initialize()
    provider = FakeProvider()
    task = ProjectVerbatimTask(
        ledger, FakeEmbedder(), provider, "test",
        **versions,
    )
    return db_path, ledger, provider, task


def test_chunk_fixed_matches_frozen_w2_a2_golden():
    records = [
        {
            "event_id": "abcdef1234567890",
            "ts": "2026-08-31T12:34:56+00:00",
            "assistant_response": "  " + ("0123456789" * 70) + "  ",
        },
        {
            "event_id": "empty00000000000",
            "ts": "2026-08-30T00:00:00+00:00",
            "assistant_response": "   ",
        },
    ]
    output = chunk_fixed(records)
    encoded = json.dumps(
        output, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    # Captured directly from w2-bench/chunkers.py::chunk_naive at a79b40a.
    assert hashlib.sha256(encoded).hexdigest() == (
        "d4aa985e4ac42846a2b789914e9d47893d028e8087be86c23fbaaf89f9a694f6"
    )
    assert [c["body_chars"] for c in output] == [486, 314]


def test_derived_task_stubs_are_off(monkeypatch):
    for name in (
        "MEMORY_WRITER_T1_ENABLED",
        "MEMORY_WRITER_T2_ENABLED",
        "MEMORY_WRITER_T3_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    assert DeriveFactsTask().enabled is False
    assert TagTaskClassTask().enabled is False
    assert ReconcileTask().enabled is False


@pytest.mark.asyncio
async def test_t0_rerun_is_zero_new_rows_and_zero_new_vectors(tmp_path):
    _, ledger, provider, task = await build_task(tmp_path)
    first = await task.project(event("evt-1", "Remember this exact answer."))
    second = await task.project(event("evt-1", "Remember this exact answer."))

    assert first.status == "completed"
    assert second.status == "already_completed"
    assert provider.index_calls == 1
    assert len(provider.points) == first.chunk_count == 1
    assert await ledger.count_completed() == 1


@pytest.mark.asyncio
async def test_normalized_duplicate_event_does_not_consume_chunk_slots(tmp_path):
    _, ledger, provider, task = await build_task(tmp_path)
    first = await task.project(event("evt-original", "PONG   now"))
    duplicate = await task.project(event("evt-copy", "  pong now  "))

    assert first.status == "completed"
    assert duplicate.status == "duplicate"
    assert duplicate.duplicate_of_event_id == "evt-original"
    assert provider.index_calls == 1
    assert len(provider.points) == 1
    assert await ledger.count_completed() == 1


@pytest.mark.asyncio
async def test_task_and_prompt_version_bumps_reprocess(tmp_path):
    _, ledger, provider, v1 = await build_task(tmp_path)
    source = event("evt-versioned", "A versioned projection.")
    assert (await v1.project(source)).status == "completed"

    v2 = ProjectVerbatimTask(
        ledger, FakeEmbedder(), provider, "test",
        task_version="project_verbatim:v2",
    )
    assert (await v2.project(source)).status == "completed"
    prompt_v2 = ProjectVerbatimTask(
        ledger, FakeEmbedder(), provider, "test",
        task_version="project_verbatim:v2",
        prompt_version="mechanical-contract:v2",
    )
    assert (await prompt_v2.project(source)).status == "completed"

    assert provider.index_calls == 3
    assert len(provider.points) == 1  # old contract points were replaced
    assert await ledger.count_completed() == 3


@pytest.mark.asyncio
async def test_reclaimed_claim_cannot_be_completed_by_old_owner(tmp_path):
    db_path = tmp_path / "claims.db"
    await init_schema(db_path)
    ledger = CompletionLedger(db_path, stale_after_seconds=0)
    await ledger.initialize()
    identity = {
        "source_content_hash": "content-hash",
        "task_name": "project_verbatim",
        "task_version": "project_verbatim:v1",
        "prompt_version": "none",
        "source_event_id": "evt",
    }
    first = await ledger.claim(**identity)
    second = await ledger.claim(**identity)
    assert first.claimed and second.claimed
    assert first.claim_token != second.claim_token

    with pytest.raises(RuntimeError, match="lost its RUNNING claim"):
        await ledger.complete(
            source_content_hash="content-hash",
            task_version="project_verbatim:v1",
            prompt_version="none",
            claim_token=first.claim_token,
            chunk_ids=[],
        )
    await ledger.complete(
        source_content_hash="content-hash",
        task_version="project_verbatim:v1",
        prompt_version="none",
        claim_token=second.claim_token,
        chunk_ids=[],
    )
    assert await ledger.count_completed() == 1


@pytest.mark.asyncio
async def test_replay_checkpoint_resumes_after_last_durable_event(tmp_path):
    db_path, ledger, provider, task = await build_task(tmp_path)
    log = SQLiteEventLog(db_path)
    for i in range(3):
        await log.atomic_append({
            "user_message": f"question {i}",
            "assistant_response": f"unique answer {i}",
        })

    first = await replay_t0(log, task, limit=2)
    second = await replay_t0(log, task)
    third = await replay_t0(log, task)

    assert (first.visited, first.projected) == (2, 2)
    assert (second.visited, second.projected) == (1, 1)
    assert third.visited == 0
    checkpoint = await ledger.get_checkpoint(task.task_version, task.prompt_version)
    assert checkpoint is not None and checkpoint.processed_count == 3
    assert len(provider.points) == 3


def _acl_registry(tmp_path):
    path = tmp_path / "stores.toml"
    path.write_text(
        '[store.s]\nallowed_locality=["local"]\n'
        'allowed_provider_ids=["p"]\n'
        'allowed_capability_classes=["text_dense"]\n',
        encoding="utf-8",
    )
    return ACLRegistry(path)


def test_fence_dynamic_registration_allowed_write_and_foreign_refusal(tmp_path, monkeypatch):
    from qdrant_client import QdrantClient
    from core.memory.bootstrap import _maybe_build_write_fence

    registry_path = tmp_path / "throwaway-registry.json"
    monkeypatch.setenv("MEMORY_WRITE_FENCE_ENABLED", "1")
    monkeypatch.setenv("BOBCLAW_LEDGER_INSTANCES", str(registry_path))
    monkeypatch.delenv("MEMORY_SINGLE_QDRANT", raising=False)
    slot = SimpleNamespace(get=lambda _: SlotResolution(
        slot_name="embed_text",
        model="test-embedder",
        backend="lmstudio",
        endpoint="http://127.0.0.1:1",
        embedding_dimension=4,
    ))
    fence = _maybe_build_write_fence(slot, "w3")
    assert fence is not None
    registered = fence._registry.get("bobclaw-memory")
    assert registered["collection"] == "w3_4"
    assert registered["meta"]["acl"] == {
        "writer": "bobclaw", "readers": ["bobclaw"], "mode": "rw",
    }

    client = QdrantClient(location=":memory:")
    owned = QdrantRetrievalProvider(
        "p", "local", "w3", _acl_registry(tmp_path),
        client=client, write_fence=fence,
    )
    receipt = owned.index(
        "s", [ChunkRecord("owned", [1.0, 0.0, 0.0, 0.0], {"text": "ok"})]
    )
    assert receipt.item_count == 1
    assert client.count("w3_4").count == 1

    fence._registry.register(
        "foreign", "/tmp/foreign", collection="foreign_4", dim=4,
        meta={"acl": {"writer": "lks", "readers": ["bobclaw"], "mode": "ro"}},
    )
    foreign = QdrantRetrievalProvider(
        "p", "local", "foreign", _acl_registry(tmp_path),
        client=client, write_fence=fence,
    )
    with pytest.raises(WriteFenceViolation):
        foreign.index(
            "s", [ChunkRecord("refused", [1.0, 0.0, 0.0, 0.0], {})]
        )
    assert not client.collection_exists("foreign_4")


class _SearchProvider:
    def __init__(self, hits):
        self.hits = hits

    def query_vector(self, store_id, vector, k, filters, *, offset=0):
        page = self.hits[offset:offset + k]
        return RankedResults(page, "p", 0)


class _FactStore:
    def __init__(self, fact):
        self.fact = fact

    async def get(self, fact_id):
        return self.fact


@pytest.mark.asyncio
async def test_t0_read_gate_backfills_to_legacy_fact_when_off():
    fact = Fact(
        "fact-1", "manual", {"text": "legacy fact"}, "evt", "h",
        ConfidenceStub(), "2026-08-31T00:00:00+00:00",
    )
    hits = [
        Hit("t0", 0.99, {"text": "new chunk", "projection_task": "project_verbatim"}),
        Hit("l1", 0.98, {"text": "legacy fact", "source_fact_id": "fact-1"}),
    ]
    slot = SimpleNamespace(get=lambda _: object())
    query_log = SimpleNamespace(append=lambda _: None)

    off = MemoryRetriever(
        FakeEmbedder(), _SearchProvider(hits), _FactStore(fact), "s", slot, query_log,
        t0_recall_enabled=False,
    )
    on = MemoryRetriever(
        FakeEmbedder(), _SearchProvider(hits), _FactStore(fact), "s", slot, query_log,
        t0_recall_enabled=True,
    )
    off_result = await off.search("query", top_k=1)
    on_result = await on.search("query", top_k=1)

    assert [c.content for c in off_result] == ["legacy fact"]
    assert [c.content for c in on_result] == ["new chunk"]


@pytest.mark.asyncio
async def test_recall_node_surfaces_t0_chunks_only_when_requested():
    from core.memory.models import RetrievedChunk

    chunk = RetrievedChunk("verbatim", 0.9, None, "event://evt", [], 0.9)
    retriever = SimpleNamespace(search=AsyncMock(return_value=[chunk]))
    fact_store = SimpleNamespace(get=AsyncMock())
    state = {"messages": [{"role": "user", "content": "query"}]}

    legacy = await recall_node(state, retriever, fact_store, enabled=True)
    enabled = await recall_node(
        state, retriever, fact_store, enabled=True, include_t0=True
    )
    assert legacy == {"recalled_facts": []}
    assert enabled == {"recalled_facts": [], "recalled_chunks": [chunk]}
    fact_store.get.assert_not_called()


@pytest.mark.asyncio
async def test_execute_splice_is_independently_gated(monkeypatch):
    from core.config import config
    from core.memory.models import RetrievedChunk
    from core.nodes.execute import execute_node

    captured = []

    async def stream(messages, backend, model_override=None):
        captured.append(messages)
        yield "reply"

    state = {
        "messages": [],
        "task": "current task",
        "face_id": "assistant",
        "backend": "local",
        "approval_response": "approved",
        "recalled_chunks": [RetrievedChunk(
            "verbatim memory", 1.0, None, "event://evt", [], 1.0,
        )],
    }
    monkeypatch.setattr(config, "MEMORY_ENABLED", False)
    monkeypatch.setattr(config, "MEMORY_T0_RECALL_ENABLED", False)
    with patch("core.nodes.execute._stream_to_backend", stream), patch(
        "core.nodes.execute._check_escalation_pin", AsyncMock(return_value=None)
    ):
        await execute_node(dict(state))
    assert all("verbatim memory" not in m.get("content", "") for m in captured[-1])

    monkeypatch.setattr(config, "MEMORY_T0_RECALL_ENABLED", True)
    with patch("core.nodes.execute._stream_to_backend", stream), patch(
        "core.nodes.execute._check_escalation_pin", AsyncMock(return_value=None)
    ):
        await execute_node(dict(state))
    assert any("verbatim memory" in m.get("content", "") for m in captured[-1])


@pytest.mark.asyncio
async def test_live_l0_append_schedules_t0_projection(monkeypatch):
    from core.config import config
    from core.nodes._l0_events import _append_agent_turn_event

    appended = event("evt-live", "live answer", user="live question")
    writer = SimpleNamespace(project=AsyncMock(return_value=SimpleNamespace(
        status="completed", chunk_count=1,
    )))
    memory = SimpleNamespace(
        event_log=SimpleNamespace(atomic_append=AsyncMock(return_value=appended)),
        writer=writer,
        pending_writer_tasks=set(),
        pending_extraction_tasks=set(),
        last_l0_append_error=None,
        last_writer_error=None,
    )
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_WRITER_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_L1_EXTRACTION_ENABLED", False)
    monkeypatch.setattr("core.memory.bootstrap.get_memory", lambda: memory)

    await _append_agent_turn_event(
        {"task": "live question", "messages": []},
        assistant_response="live answer",
    )
    await asyncio.gather(*list(memory.pending_writer_tasks))
    writer.project.assert_awaited_once_with(appended)
    assert memory.last_writer_error is None
